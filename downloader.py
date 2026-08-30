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
    포함돼 있어 정확한 이름을 미리 알 수 없으므로 '제목 00xx화 ' 접두어로
    찾는다."""
    series_dir = title_dir(download_root, title, title_id)
    if not os.path.isdir(series_dir):
        return None
    prefix = _archive_prefix(title, episode_no, folder_zero_fill) + " "
    for fname in os.listdir(series_dir):
        if fname.startswith(prefix) and fname.lower().endswith(".zip"):
            return os.path.join(series_dir, fname)
    return None


def download_episode(session, download_root, title, title_id, episode_no,
                      image_zero_fill=4, folder_zero_fill=4,
                      max_concurrent=5, delay_seconds=1.0, timeout=10, log=None):
    """
    1단계: 이미지만 내려받는다(압축은 하지 않음 - compress_episode()가 별도
    단계로 처리). 이미 압축까지 끝난 회차거나, 회차 폴더에 이미지가 이미
    있으면(전에 받아만 두고 아직 압축 안 한 경우 포함) 새로 받지 않고
    True(스킵)로 취급한다.
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
    if os.path.isdir(target_dir) and any(
            f.lower().endswith(_IMAGE_EXTS) for f in os.listdir(target_dir)):
        # 전에 이미지는 받아뒀는데 압축을 아직 안 한 상태 - 다시 받을 필요는
        # 없다. 압축은 이 함수를 호출한 쪽이 뒤이어 compress_episode()를
        # 불러서 처리한다.
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
    이미지를 '제목 00xx화 장수.zip'으로 압축하고, 원본 낱장 폴더는 삭제한다.
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
    archive_name = "%s %d.zip" % (_archive_prefix(title, episode_no, folder_zero_fill), count)
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
