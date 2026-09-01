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
    """작품(시리즈) 폴더 경로."""
    return os.path.join(download_root, safe_name("%s (%s)" % (title, title_id)))


def episode_dir(download_root, title, title_id, episode_no, folder_zero_fill=4):
    """회차 이미지를 내려받는 동안 쓰는 폴더. 다운로드가 끝나면(별도 단계인
    compress_episode()가 호출되면) 이 폴더 안 이미지들이 압축파일로 옮겨지고
    이 폴더 자체는 삭제된다."""
    ep_name = str(episode_no).zfill(int(folder_zero_fill or 4))
    return os.path.join(title_dir(download_root, title, title_id), ep_name)


def _archive_prefix(title, episode_no, folder_zero_fill=4):
    """압축파일명의 접두어(제목 + 회차). 실제 파일명은 여기에 ' 장수.zip'이
    붙는다(장수는 압축해봐야 알 수 있어 접두어만 미리 정한다)."""
    ep_name = str(episode_no).zfill(int(folder_zero_fill or 4))
    return "%s %s화" % (safe_name(title), ep_name)


def find_existing_episode_archive(download_root, title, title_id, episode_no, folder_zero_fill=4):
    """이미 압축까지 끝난 회차의 zip 파일을 찾는다. 파일명에 페이지 수가
    포함돼 있어 정확한 이름을 미리 알 수 없으므로 '제목 00xx화#' 접두어로
    찾는다."""
    series_dir = title_dir(download_root, title, title_id)
    if not os.path.isdir(series_dir):
        return None
    prefix = _archive_prefix(title, episode_no, folder_zero_fill) + "#"
    for fname in os.listdir(series_dir):
        if fname.startswith(prefix) and fname.lower().endswith(".zip"):
            return os.path.join(series_dir, fname)
    return None


def download_episode(session, download_root, title, title_id, episode_no,
                      image_zero_fill=4, folder_zero_fill=4,
                      max_concurrent=5, delay_seconds=1.0, timeout=10, log=None):
    """
    1단계: 이미지만 내려받는다(압축은 하지 않음 - compress_episode()가 별도
    단계로 처리). 이미 압축까지 끝난 회차는 네트워크 요청 없이 즉시 스킵한다.
    압축 전 낱장 폴더가 있는 경우, 기대 이미지 장수(원본에서 조회)와 실제
    받은 장수를 비교해서 완전히 받아져 있을 때만 스킵하고, 중단된 채 남은
    폴더라면 빠진 이미지만 이어받는다(resume). 이어받은 뒤에도 장수가
    모자라면 ok=False를 반환해 호출 측이 compress_episode()를 부르지 않도록
    한다(불완전 압축 방지).
    반환: (ok: bool, skipped: bool, image_count: int, error: str|None)
    """
    existing_archive = find_existing_episode_archive(
        download_root, title, title_id, episode_no, folder_zero_fill)
    if existing_archive and os.path.getsize(existing_archive) > 0:
        try:
            with zipfile.ZipFile(existing_archive) as zf:
                cnt = len(zf.namelist())
        except Exception:  # noqa: BLE001
            cnt = 0
        return True, True, cnt, None

    target_dir = episode_dir(download_root, title, title_id, episode_no, folder_zero_fill)

    try:
        # 버그 수정: 예전에는 target_dir에 이미지가 "하나라도" 있으면 무조건
        # 완료로 간주하고 곧바로 압축 단계로 넘겼다. 컨테이너 재시작/네트워크
        # 중단 등으로 다운로드가 중간에 끊긴 폴더도 이 조건을 그대로 만족해서,
        # 불완전한 이미지 묶음이 "제목 00xx화#N.zip"이라는 정상 파일명으로
        # 압축되어 버리면 find_existing_episode_archive()가 그걸 완료로 인식해
        # 다시는 재다운로드되지 않는 문제가 있었다.
        # 이제는 항상 먼저 원본 이미지 목록(기대 장수)을 조회해서, 이미 받은
        # 파일 수와 비교한 뒤에만 완료로 판정한다. 이미 완전히 받은 회차라도
        # HTML 상세페이지 요청 1회는 매번 발생하지만(성능 트레이드오프),
        # 이미 압축이 끝난 회차는 위쪽의 find_existing_episode_archive()
        # 단계에서 이 네트워크 요청 없이 걸러지므로 실제 영향 범위는 "다운로드는
        # 됐는데 아직 압축 전인" 좁은 구간뿐이다.
        try:
            images = naver_api.fetch_episode_images(session, title_id, episode_no)
        except naver_api.NaverAuthExpired:
            raise
        except naver_api.NaverPaidEpisode:
            raise
        except Exception as e:  # noqa: BLE001
            return False, False, 0, str(e)

        expected_count = len(images)

        if os.path.isdir(target_dir):
            existing_files = [f for f in os.listdir(target_dir) if f.lower().endswith(_IMAGE_EXTS)]
            if expected_count > 0 and len(existing_files) >= expected_count:
                # 이미 기대 장수만큼 다 받아져 있음 - 압축만 안 됐을 뿐 완료 상태.
                return True, True, len(existing_files), None
            if existing_files and log:
                log("titleId=%s no=%s: 이전에 중단된 다운로드로 보임(%d/%d장) - 이어받기 시도" %
                    (title_id, episode_no, len(existing_files), expected_count))

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
                # 이미 받아둔 파일 - 이어받기(resume)의 핵심: 중단됐던 다운로드도
                # 이미 받은 낱장은 다시 받지 않고 빠진 것만 채운다.
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

        if expected_count > 0 and ok_count < expected_count:
            # 이번 시도로도 다 못 채움 - 폴더는 지우지 않고 그대로 둬서 다음
            # 재시도 때 이어받을 수 있게 한다. ok=False라서 호출 측이
            # compress_episode()를 부르지 않으므로 불완전 압축도 방지된다.
            return False, False, ok_count, ("이미지 %d장 중 %d장만 받음(불완전, 다음 시도 때 이어받음)" %
                                             (expected_count, ok_count))

        return True, False, ok_count, None
    finally:
        # 성공/실패와 무관하게 다음 요청 전에 항상 쉰다. 실패 시 이 딜레이 없이
        # 바로 다음 회차로 넘어가면 텀 없는 연속 요청이 되어 네이버 쪽의
        # 레이트리밋/일시 차단을 유발하고, 그 차단 상태에서는 정상 페이지 대신
        # 빈 응답이 와서 "이미지 목록을 찾지 못함"이 연쇄적으로 계속되는
        # 악순환이 생길 수 있다(실제로 관찰된 패턴).
        if delay_seconds:
            time.sleep(float(delay_seconds))


def compress_episode(download_root, title, title_id, episode_no,
                      folder_zero_fill=4, log=None):
    """2단계(별도 단계): download_episode()로 완전히 다 받아진 회차 폴더의
    이미지를 '제목 00xx화#장수.zip'으로 압축하고, 원본 낱장 폴더는 삭제한다.
    다운로드 자체와 완전히 분리된 단계라, 다운로드 도중에는 호출하지 않고
    download_episode()가 성공적으로 끝난 뒤에만 호출한다.

    이미 압축된 파일이 있으면 아무 것도 안 하고 그 경로를 반환한다(스킵).
    압축할 낱장 폴더 자체가 없으면(예: 애초에 이미지 다운로드가 실패한 경우)
    실패로 처리한다.
    반환: (ok: bool, archive_path: str|None, message: str)
    """
    existing = find_existing_episode_archive(download_root, title, title_id, episode_no, folder_zero_fill)
    if existing and os.path.getsize(existing) > 0:
        return True, existing, "이미 압축되어 있음"

    target_dir = episode_dir(download_root, title, title_id, episode_no, folder_zero_fill)
    if not os.path.isdir(target_dir):
        return False, None, "압축할 폴더가 없음(다운로드가 안 된 상태)"

    files = sorted(f for f in os.listdir(target_dir) if f.lower().endswith(_IMAGE_EXTS))
    if not files:
        return False, None, "압축할 이미지가 없음"

    series_dir = title_dir(download_root, title, title_id)
    os.makedirs(series_dir, exist_ok=True)
    count = len(files)
    archive_name = "%s#%d.zip" % (_archive_prefix(title, episode_no, folder_zero_fill), count)
    archive_path = os.path.join(series_dir, archive_name)
    if os.path.exists(archive_path):
        os.remove(archive_path)
    tmp_path = archive_path + ".tmp"
    try:
        with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for fname in files:
                zf.write(os.path.join(target_dir, fname), arcname=fname)
        os.replace(tmp_path, archive_path)
        shutil.rmtree(target_dir, ignore_errors=True)
        return True, archive_path, "압축 완료(%d장)" % count
    except Exception as e:  # noqa: BLE001
        if log:
            log("압축 실패 titleId=%s no=%s: %s (낱장 폴더는 그대로 둠)" % (title_id, episode_no, e))
        return False, None, "압축 실패: %s" % e
