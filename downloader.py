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


def episode_dir(download_root, title, title_id, episode_no, folder_zero_fill=4):
    folder = safe_name("%s (%s)" % (title, title_id))
    ep_name = str(episode_no).zfill(int(folder_zero_fill or 4))
    return os.path.join(download_root, folder, ep_name)


def episode_archive_path(download_root, title, title_id, episode_no, folder_zero_fill=4):
    """회차 다운로드가 끝나면 낱장 이미지 폴더 대신 이 경로의 .cbz(zip과 동일
    포맷, BookOasis가 인식하는 만화 파일 포맷) 파일 하나만 남긴다."""
    return episode_dir(download_root, title, title_id, episode_no, folder_zero_fill) + ".cbz"


def title_dir(download_root, title, title_id):
    """작품(시리즈) 단위 폴더 경로 - BookOasis 라이브러리 자동등록 시
    /api/webhook/scan 에 넘길 path는 회차 폴더가 아니라 이 시리즈 폴더
    단위로 호출하는 게 API 문서가 권장하는 방식이다."""
    return os.path.join(download_root, safe_name("%s (%s)" % (title, title_id)))


def _zip_and_cleanup(target_dir, archive_path):
    """target_dir 안의 이미지 파일들을 archive_path(.cbz)로 압축하고,
    성공하면 target_dir(낱장 폴더)는 삭제한다. 압축 파일 안의 이미지 개수를
    반환한다."""
    if os.path.exists(archive_path):
        os.remove(archive_path)
    tmp_path = archive_path + ".tmp"
    count = 0
    with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for fname in sorted(os.listdir(target_dir)):
            if not fname.lower().endswith(_IMAGE_EXTS):
                continue
            fpath = os.path.join(target_dir, fname)
            if os.path.isfile(fpath):
                zf.write(fpath, arcname=fname)
                count += 1
    os.replace(tmp_path, archive_path)
    shutil.rmtree(target_dir, ignore_errors=True)
    return count


def download_episode(session, download_root, title, title_id, episode_no,
                      image_zero_fill=4, folder_zero_fill=4,
                      max_concurrent=5, delay_seconds=1.0, timeout=10, log=None):
    """
    이미 받아둔 회차(.cbz 압축파일 또는 구버전 낱장 폴더)면 건너뛰고
    True(스킵)로 취급. 새로 다운로드한 회차는 이미지를 전부 받은 뒤
    .cbz로 압축하고 낱장 폴더는 삭제해 압축파일 하나만 남긴다.
    반환: (ok: bool, skipped: bool, image_count: int, error: str|None)
    """
    target_dir = episode_dir(download_root, title, title_id, episode_no, folder_zero_fill)
    archive_path = episode_archive_path(download_root, title, title_id, episode_no, folder_zero_fill)

    # 이미 압축까지 끝난 회차
    if os.path.isfile(archive_path) and os.path.getsize(archive_path) > 0:
        try:
            with zipfile.ZipFile(archive_path) as zf:
                cnt = len(zf.namelist())
        except Exception:  # noqa: BLE001
            cnt = 0
        return True, True, cnt, None

    # 구버전(압축 기능 추가 전)에 낱장 폴더로만 받아뒀던 회차 - 새로 받지 않고
    # 그대로 압축만 해서 정리한다.
    if os.path.isdir(target_dir) and any(
            f.lower().endswith(_IMAGE_EXTS) for f in os.listdir(target_dir)):
        try:
            cnt = _zip_and_cleanup(target_dir, archive_path)
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
            zipped_count = _zip_and_cleanup(target_dir, archive_path)
            return True, False, zipped_count, None
        except Exception as e:  # noqa: BLE001
            # 압축 실패해도 이미지 자체는 이미 받아뒀으니 실패로 취급하지 않고,
            # 낱장 폴더 상태로라도 남긴다(다음 실행 때 위 "구버전 폴더" 경로로
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
