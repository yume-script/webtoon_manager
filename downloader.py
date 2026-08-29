# -*- coding: utf-8 -*-
import os
import re
import shutil
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from . import naver_api

_SAFE_RE = re.compile(r'[\\/:*?"<>|]')
_IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".gif")


def safe_name(name):
    name = _SAFE_RE.sub("_", str(name)).strip()
    return name or "untitled"


def title_dir(download_root, title, title_id):
    """작품(시리즈) 폴더 경로. BookOasis 라이브러리 자동등록 시
    /api/webhook/scan 에 넘길 path도 이 폴더 단위로 호출한다."""
    return os.path.join(download_root, safe_name("%s (%s)" % (title, title_id)))


def episode_dir(download_root, title, title_id, episode_no, folder_zero_fill=4):
    """회차 하나의 이미지가 저장되는 폴더. 압축하지 않고 이미지 파일 그대로
    이 폴더 안에 낱장으로 남는다(BookOasis의 imgdir 포맷)."""
    ep_name = str(episode_no).zfill(int(folder_zero_fill or 4))
    return os.path.join(title_dir(download_root, title, title_id), ep_name)


def download_episode(session, download_root, title, title_id, episode_no,
                      image_zero_fill=4, folder_zero_fill=4,
                      max_concurrent=5, delay_seconds=1.0, timeout=10, log=None):
    """
    이미 받아둔 회차(회차 폴더에 이미지 파일이 하나라도 있음)면 건너뛰고
    True(스킵)로 취급. 새로 다운로드한 이미지는 압축하지 않고 회차 폴더
    안에 낱장 파일 그대로 남는다.
    반환: (ok: bool, skipped: bool, image_count: int, error: str|None)
    """
    target_dir = episode_dir(download_root, title, title_id, episode_no, folder_zero_fill)
    if os.path.isdir(target_dir) and any(
            f.lower().endswith(_IMAGE_EXTS) for f in os.listdir(target_dir)):
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
