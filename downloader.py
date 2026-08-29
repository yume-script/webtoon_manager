# -*- coding: utf-8 -*-
import os
import re
import shutil
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed

from . import naver_api

_SAFE_RE = re.compile(r'[\\/:*?"<>|]')
_IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".gif")


def safe_name(name):
    name = _SAFE_RE.sub("_", str(name)).strip()
    return name or "untitled"


def title_dir(download_root, title, title_id):
    """작품(시리즈) 폴더 경로. 회차 압축파일들이 이 폴더 바로 밑에 별도
    하위폴더 없이 flat하게 놓인다(예: 15화#87.zip). BookOasis 라이브러리
    자동등록 시 /api/webhook/scan에 넘길 path도 이 폴더 단위로 호출한다."""
    return os.path.join(download_root, safe_name("%s (%s)" % (title, title_id)))


def episode_dir(download_root, title, title_id, episode_no, folder_zero_fill=4):
    """이미지를 내려받는 동안만 쓰는 임시 작업 폴더. 다운로드가 끝나면
    이 폴더 내용을 시리즈 폴더 바로 밑의 압축파일로 옮기고 이 폴더 자체는
    삭제된다(최종적으로는 존재하지 않음)."""
    ep_name = str(episode_no).zfill(int(folder_zero_fill or 4))
    return os.path.join(title_dir(download_root, title, title_id), "." + ep_name + ".tmp")


def _archive_prefix(title, episode_no):
    """최종 압축파일명의 접두어. 실제 파일명은 다운로드가 끝나야 알 수 있는
    이미지 수가 '#장수' 형태로 붙는다(예: '프리즈마 이리야 드라이 15화#87.zip').
    BookOasis 라이브러리 화면에서 권/화별로 제목이 그대로 보이도록 시리즈
    제목도 파일명에 포함시킨다(실제 정상 등록된 다른 시리즈들의 명명 방식과
    동일하게 맞춤)."""
    return "%s %s화" % (safe_name(title), episode_no)


def _legacy_archive_prefix(episode_no):
    """이번 변경 전(제목 없이 회차 번호만 쓰던) 명명 방식 - 이미 그 형식으로
    받아둔 파일을 재다운로드하지 않도록 하위호환용으로 남겨둔다."""
    return "%s화" % episode_no


def find_existing_episode_archive(download_root, title, title_id, episode_no):
    """이미 받아둔 회차의 압축파일을 찾는다. 파일명에 이미지 수가 포함돼
    있어 정확한 이름을 미리 알 수 없으므로 '{제목} {회차}화#' 접두어로 찾는다
    (구버전에 제목 없이 저장된 파일도 하위호환으로 함께 찾는다)."""
    series_dir = title_dir(download_root, title, title_id)
    if not os.path.isdir(series_dir):
        return None
    prefix = _archive_prefix(title, episode_no) + "#"
    legacy_prefix = _legacy_archive_prefix(episode_no) + "#"
    for fname in os.listdir(series_dir):
        if not fname.lower().endswith(".zip"):
            continue
        if fname.startswith(prefix) or fname.startswith(legacy_prefix):
            return os.path.join(series_dir, fname)
    return None


def _zip_and_cleanup_named(target_dir, download_root, title, title_id, episode_no):
    """target_dir(임시 작업 폴더) 안의 이미지들을 시리즈 폴더 바로 밑
    '{제목} {회차}화#{장수}.zip'으로 압축하고, target_dir은 삭제한다.
    반환: (압축한 이미지 수, 최종 압축파일 경로)"""
    series_dir = title_dir(download_root, title, title_id)
    os.makedirs(series_dir, exist_ok=True)
    files = sorted(f for f in os.listdir(target_dir) if f.lower().endswith(_IMAGE_EXTS))
    count = len(files)
    archive_path = os.path.join(series_dir, "%s#%d.zip" % (_archive_prefix(title, episode_no), count))
    if os.path.exists(archive_path):
        os.remove(archive_path)
    tmp_path = archive_path + ".tmp"
    with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for fname in files:
            zf.write(os.path.join(target_dir, fname), arcname=fname)
    os.replace(tmp_path, archive_path)
    shutil.rmtree(target_dir, ignore_errors=True)
    return count, archive_path


_LEGACY_FOLDER_RE = re.compile(r'^\d{3,}$')
_ZIP_EPISODE_RE = re.compile(r'^(?:.*\s)?(\d+)화#\d+\.zip$', re.I)
_CBZ_EPISODE_RE = re.compile(r'^(?:.*\s)?(\d+)화#(\d+)\.cbz$', re.I)
# 아주 초기 버전(첫 zip 구현)은 '화#장수' 없이 그냥 4자리 순번 + .cbz였다
# (예: 0089.cbz). 이것도 정리 대상으로 인식해야 한다.
_CBZ_LEGACY_RE = re.compile(r'^(\d{3,})\.cbz$', re.I)


def cleanup_legacy_artifacts(series_dir, log=None):
    """시리즈 폴더 안의 예전 형식 잔재를 정리한다:
    - `.cbz.tmp`/`.zip.tmp`/중단된 임시 폴더(`.NNNN.tmp`): 미완성 데이터라 항상 삭제
    - `.cbz` 확장자 파일(확장자를 .zip으로 바꾸기 전 버전의 산물): 같은 회차의 최신
      `.zip`이 이미 있으면 삭제, 없으면 이름만 `.zip`으로 바꿔서 보존
    - 순수 숫자 이름의 낱장 이미지 폴더(압축 기능 추가 전 버전의 산물): 같은 회차의
      최신 `.zip`이 이미 있으면 삭제, 없으면 압축해서 `.zip`으로 만들고 폴더는 삭제
    데이터 손실 없이 정리하는 게 목적이라, 이미 최신 형식으로 존재하는 회차만
    삭제 대상으로 삼고 그렇지 않으면 항상 변환(보존)한다.
    반환: {"removed": 삭제한 개수, "converted": 새 형식으로 변환한 개수, "kept": 그대로 둔 개수}
    """
    if not os.path.isdir(series_dir):
        return {"removed": 0, "converted": 0, "kept": 0}

    entries = os.listdir(series_dir)
    zip_episode_nos = set()
    for f in entries:
        m = _ZIP_EPISODE_RE.match(f)
        if m:
            zip_episode_nos.add(m.group(1))

    removed = converted = kept = 0

    for f in entries:
        fpath = os.path.join(series_dir, f)

        # 1) 미완성 임시 항목은 항상 삭제
        if f.endswith(".cbz.tmp") or f.endswith(".zip.tmp") or \
                (f.startswith(".") and f.endswith(".tmp") and os.path.isdir(fpath)):
            try:
                if os.path.isdir(fpath):
                    shutil.rmtree(fpath, ignore_errors=True)
                else:
                    os.remove(fpath)
                removed += 1
                if log:
                    log("정리: 미완성 임시 항목 삭제 - %s" % f)
            except Exception as e:  # noqa: BLE001
                if log:
                    log("정리 실패(임시 항목) %s: %s" % (f, e))
            continue

        # 2) 예전 .cbz 확장자
        m = _CBZ_EPISODE_RE.match(f)
        if m:
            ep_no = m.group(1)
            if ep_no in zip_episode_nos:
                try:
                    os.remove(fpath)
                    removed += 1
                    if log:
                        log("정리: 최신 zip이 있어 예전 cbz 삭제 - %s" % f)
                except Exception as e:  # noqa: BLE001
                    if log:
                        log("정리 실패(cbz 삭제) %s: %s" % (f, e))
            else:
                new_name = re.sub(r'\.cbz$', '.zip', f, flags=re.I)
                new_path = os.path.join(series_dir, new_name)
                try:
                    if not os.path.exists(new_path):
                        os.rename(fpath, new_path)
                        zip_episode_nos.add(ep_no)
                        converted += 1
                        if log:
                            log("정리: cbz -> zip 이름 변경 - %s -> %s" % (f, new_name))
                    else:
                        kept += 1
                except Exception as e:  # noqa: BLE001
                    if log:
                        log("정리 실패(cbz 이름변경) %s: %s" % (f, e))
            continue

        # 2-2) 더 오래된 초기 버전의 .cbz(순번만, '화#장수' 표기 없음 - 예: 0089.cbz)
        m2 = _CBZ_LEGACY_RE.match(f)
        if m2:
            ep_no = str(int(m2.group(1)))
            if ep_no in zip_episode_nos:
                try:
                    os.remove(fpath)
                    removed += 1
                    if log:
                        log("정리: 최신 zip이 있어 예전(순번만) cbz 삭제 - %s" % f)
                except Exception as e:  # noqa: BLE001
                    if log:
                        log("정리 실패(예전 cbz 삭제) %s: %s" % (f, e))
            else:
                try:
                    with zipfile.ZipFile(fpath) as zf:
                        count = len(zf.namelist())
                    new_name = "%s화#%d.zip" % (ep_no, count)
                    new_path = os.path.join(series_dir, new_name)
                    if not os.path.exists(new_path):
                        os.rename(fpath, new_path)
                        zip_episode_nos.add(ep_no)
                        converted += 1
                        if log:
                            log("정리: 예전(순번만) cbz -> 새 이름으로 변경 - %s -> %s" % (f, new_name))
                    else:
                        kept += 1
                except Exception as e:  # noqa: BLE001
                    if log:
                        log("정리 실패(예전 cbz 이름변경) %s: %s" % (f, e))
            continue

        # 3) 낡은 낱장 이미지 폴더(순수 숫자 이름)
        if os.path.isdir(fpath) and _LEGACY_FOLDER_RE.match(f):
            try:
                images = sorted(fn for fn in os.listdir(fpath) if fn.lower().endswith(_IMAGE_EXTS))
            except Exception:  # noqa: BLE001
                images = []
            if not images:
                try:
                    shutil.rmtree(fpath, ignore_errors=True)
                    removed += 1
                    if log:
                        log("정리: 빈 폴더 삭제 - %s" % f)
                except Exception as e:  # noqa: BLE001
                    if log:
                        log("정리 실패(빈 폴더) %s: %s" % (f, e))
                continue

            ep_no = str(int(f))
            if ep_no in zip_episode_nos:
                try:
                    shutil.rmtree(fpath, ignore_errors=True)
                    removed += 1
                    if log:
                        log("정리: 최신 zip이 있어 낡은 폴더 삭제 - %s" % f)
                except Exception as e:  # noqa: BLE001
                    if log:
                        log("정리 실패(낡은 폴더 삭제) %s: %s" % (f, e))
            else:
                try:
                    count = len(images)
                    archive_path = os.path.join(series_dir, "%s화#%d.zip" % (ep_no, count))
                    tmp_path = archive_path + ".tmp"
                    with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED) as zf:
                        for fn in images:
                            zf.write(os.path.join(fpath, fn), arcname=fn)
                    os.replace(tmp_path, archive_path)
                    shutil.rmtree(fpath, ignore_errors=True)
                    zip_episode_nos.add(ep_no)
                    converted += 1
                    if log:
                        log("정리: 낡은 폴더 압축 - %s -> %s" % (f, os.path.basename(archive_path)))
                except Exception as e:  # noqa: BLE001
                    if log:
                        log("정리 실패(낡은 폴더 압축) %s: %s" % (f, e))
            continue

    return {"removed": removed, "converted": converted, "kept": kept}


def download_episode(session, download_root, title, title_id, episode_no,
                      image_zero_fill=4, folder_zero_fill=4,
                      max_concurrent=5, delay_seconds=1.0, timeout=10, log=None):
    """
    이미 받아둔 회차(시리즈 폴더 바로 밑 '{회차}화#{장수}.zip')면 건너뛰고
    True(스킵)로 취급. 새로 다운로드한 회차는 임시 폴더에 이미지를 전부
    받은 뒤 그 압축파일 하나로 정리하고 임시 폴더는 삭제한다(하위 폴더로
    안 남고 시리즈 폴더 바로 밑에 압축파일만 flat하게 남음).
    반환: (ok: bool, skipped: bool, image_count: int, error: str|None)
    """
    existing = find_existing_episode_archive(download_root, title, title_id, episode_no)
    if existing and os.path.getsize(existing) > 0:
        try:
            with zipfile.ZipFile(existing) as zf:
                cnt = len(zf.namelist())
        except Exception:  # noqa: BLE001
            cnt = 0
        return True, True, cnt, None

    target_dir = episode_dir(download_root, title, title_id, episode_no, folder_zero_fill)

    # 구버전(압축 기능 추가 전)에 임시 폴더 형태로 남아있던 회차 - 새로 받지
    # 않고 그대로 압축만 해서 정리한다.
    if os.path.isdir(target_dir) and any(
            f.lower().endswith(_IMAGE_EXTS) for f in os.listdir(target_dir)):
        try:
            cnt, _ = _zip_and_cleanup_named(target_dir, download_root, title, title_id, episode_no)
            return True, True, cnt, None
        except Exception as e:  # noqa: BLE001
            if log:
                log("기존 폴더 압축 실패(폴더 그대로 둠) titleId=%s no=%s: %s" %
                    (title_id, episode_no, e))
            return True, True, len(os.listdir(target_dir)), None

    try:
        try:
            images = naver_api.fetch_episode_images(session, title_id, episode_no)
        except naver_api.NaverAuthExpired:
            raise
        except naver_api.NaverPaidEpisode:
            raise
        except Exception as e:  # noqa: BLE001
            return False, False, 0, str(e)

        os.makedirs(target_dir, exist_ok=True)
        referer = "%s?titleId=%s&no=%s" % (naver_api.DETAIL_URL, title_id, episode_no)

        def _dl_one(idx_url):
            idx, img_url = idx_url
            ext = ".jpg"
            for cand in (".jpg", ".jpeg", ".png", ".webp", ".gif"):
                if cand in img_url.lower():
                    ext = cand
                    break
            fname = str(idx + 1).zfill(int(image_zero_fill or 4)) + ext
            fpath = os.path.join(target_dir, fname)
            if os.path.exists(fpath) and os.path.getsize(fpath) > 0:
                return True
            try:
                resp = session.get(img_url, headers={"Referer": referer}, timeout=timeout)
                resp.raise_for_status()
                with open(fpath, "wb") as f:
                    f.write(resp.content)
                return True
            except Exception as e:  # noqa: BLE001
                if log:
                    log("이미지 다운로드 실패 titleId=%s no=%s idx=%s: %s" %
                        (title_id, episode_no, idx, e))
                return False

        ok_count = 0
        with ThreadPoolExecutor(max_workers=max(1, int(max_concurrent or 5))) as ex:
            futures = [ex.submit(_dl_one, pair) for pair in enumerate(images)]
            for fut in as_completed(futures):
                if fut.result():
                    ok_count += 1

        if ok_count == 0:
            shutil.rmtree(target_dir, ignore_errors=True)
            return False, False, 0, "이미지 0장 저장됨(전체 실패)"

        try:
            cnt, _ = _zip_and_cleanup_named(target_dir, download_root, title, title_id, episode_no)
            return True, False, cnt, None
        except Exception as e:  # noqa: BLE001
            # 압축 실패해도 이미지 자체는 이미 받아뒀으니 실패로 취급하지 않고,
            # 임시 폴더 상태로라도 남긴다(다음 실행 때 위 "구버전 폴더" 경로로
            # 다시 압축을 시도하게 된다).
            if log:
                log("압축 실패(폴더 그대로 둠) titleId=%s no=%s: %s" % (title_id, episode_no, e))
            return True, False, ok_count, None
    finally:
        # 성공/실패와 무관하게 다음 요청 전에 항상 쉰다. 실패 시 이 딜레이 없이
        # 바로 다음 회차로 넘어가면 텀 없는 연속 요청이 되어 네이버 쪽의
        # 레이트리밋/일시 차단을 유발하고, 그 차단 상태에서는 정상 페이지 대신
        # 빈 응답이 와서 "이미지 목록을 찾지 못함"이 연쇄적으로 계속되는
        # 악순환이 생길 수 있다(실제로 관찰된 패턴).
        if delay_seconds:
            time.sleep(float(delay_seconds))
