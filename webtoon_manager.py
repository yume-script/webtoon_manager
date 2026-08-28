# -*- coding: utf-8 -*-
"""
webtoon_manager
---------------
GitHub murianwind/webtoon-manager(네이버웹툰 무료 회차 자동 구독/다운로드
독립 웹앱)를 BookOasis 카테고리탭 플러그인으로 이식한 단일 파일 버전.

임포트 경로 문제를 피하기 위해 core/ 하위 폴더 없이 이 파일 하나에
상태저장소 / 네이버 API 접근 / 다운로더 / 디스코드 알림 / 스케줄러 /
파이프라인 / 플러그인 클래스를 전부 담았다.

화면: index.html(카테고리탭 풀페이지) + script.js + style.css.
데이터: get_dashboard_data()가 전체 상태를 한 번에 반환하고, 화면에서는
탭별로 클라이언트 사이드 필터링만 한다.
액션(구독/다운로드/설정 등)은 apply(db_type, book_id=0, item_data)와
run_context_menu_action()을 범용 RPC 채널로 사용한다.
"""
import json
import os
import re
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

from plugins.metadata.base import BaseMetadataProvider

PLUGIN_ID = "webtoon_manager"

# ============================================================================
# 0. 경로/상수
# ============================================================================
# 이 파일은 plugins/metadata/webtoon_manager/webtoon_manager.py 에 위치.
_PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))          # .../webtoon_manager
_METADATA_DIR = os.path.dirname(_PLUGIN_DIR)                       # .../plugins/metadata
_PLUGINS_ROOT = os.path.dirname(_METADATA_DIR)                     # .../plugins
DATA_DIR = os.path.join(_PLUGINS_ROOT, "data", "webtoon_manager")
DOWNLOAD_DEFAULT_DIR = os.path.join(DATA_DIR, "downloads")

TITLES_PATH = os.path.join(DATA_DIR, "titles.json")
AUTHORS_TAGS_PATH = os.path.join(DATA_DIR, "authors_tags.json")
HISTORY_PATH = os.path.join(DATA_DIR, "history.jsonl")
JOB_STATE_PATH = os.path.join(DATA_DIR, "job_state.json")
JOB_LOG_PATH = os.path.join(DATA_DIR, "job.log")
SCHED_LOCK_PATH = os.path.join(DATA_DIR, "scheduler.lock")

MAX_HISTORY_LINES = 2000
MAX_LOG_LINES = 500

DEFAULTS = {
    "ENABLE_SCHEDULER": False,
    "INTERVAL_MINUTES": 240,
    "MAX_NEW_EPISODES_PER_TITLE": 10,
    "BATCH_REST_MINUTES": 5.0,
    "MAX_CONCURRENT_DOWNLOADS": 5,
    "DELAY_SECONDS": 1.0,
    "REQUEST_TIMEOUT_SECONDS": 10,
    "FOLDER_ZERO_FILL": 4,
    "IMAGE_ZERO_FILL": 4,
}

_state_lock = threading.RLock()


# ============================================================================
# 1. 상태 저장소 (파일 기반) - 요청마다 모듈이 새로 로드될 수 있어 메모리에
#    값을 들고 있지 않고 전부 plugins/data/webtoon_manager/ 아래 JSON으로 저장
# ============================================================================
def ensure_dirs():
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(DOWNLOAD_DEFAULT_DIR, exist_ok=True)


def _atomic_write(path, text):
    ensure_dirs()
    fd, tmp_path = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".tmp_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp_path, path)
    finally:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass


def read_json(path, default):
    with _state_lock:
        if not os.path.exists(path):
            return default
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return default


def write_json(path, data):
    with _state_lock:
        _atomic_write(path, json.dumps(data, ensure_ascii=False, indent=2))


def load_titles():
    return read_json(TITLES_PATH, {})


def save_titles(titles):
    write_json(TITLES_PATH, {str(k): v for k, v in titles.items()})


def upsert_title(patch_by_id):
    with _state_lock:
        titles = load_titles()
        for tid, patch in patch_by_id.items():
            tid = str(tid)
            cur = titles.get(tid, {})
            cur.update(patch)
            titles[tid] = cur
        save_titles(titles)
        return titles


def load_authors_tags():
    return read_json(AUTHORS_TAGS_PATH, {"authors": [], "tags": []})


def save_authors_tags(data):
    write_json(AUTHORS_TAGS_PATH, data)


def append_history(entry):
    with _state_lock:
        ensure_dirs()
        entry = dict(entry)
        entry.setdefault("ts", time.time())
        lines = []
        if os.path.exists(HISTORY_PATH):
            with open(HISTORY_PATH, "r", encoding="utf-8") as f:
                lines = f.readlines()
        lines.append(json.dumps(entry, ensure_ascii=False) + "\n")
        if len(lines) > MAX_HISTORY_LINES:
            lines = lines[-MAX_HISTORY_LINES:]
        _atomic_write(HISTORY_PATH, "".join(lines))


def load_history(limit=200, query=None):
    if not os.path.exists(HISTORY_PATH):
        return []
    with open(HISTORY_PATH, "r", encoding="utf-8") as f:
        lines = f.readlines()
    out = []
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if query and query not in json.dumps(entry, ensure_ascii=False):
            continue
        out.append(entry)
        if len(out) >= limit:
            break
    return out


DEFAULT_JOB_STATE = {
    "running": False,
    "pid": None,
    "stage": "idle",
    "message": "",
    "progress": 0,
    "total": 0,
    "started_at": None,
    "finished_at": None,
    "last_scan_at": None,
    "last_error": None,
    "cancel_requested": False,
}


def load_job_state():
    st = read_json(JOB_STATE_PATH, dict(DEFAULT_JOB_STATE))
    for k, v in DEFAULT_JOB_STATE.items():
        st.setdefault(k, v)
    return st


def save_job_state(patch):
    with _state_lock:
        st = load_job_state()
        st.update(patch)
        write_json(JOB_STATE_PATH, st)
        return st


def append_log(line):
    with _state_lock:
        ensure_dirs()
        lines = []
        if os.path.exists(JOB_LOG_PATH):
            with open(JOB_LOG_PATH, "r", encoding="utf-8") as f:
                lines = f.readlines()
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        lines.append("[%s] %s\n" % (ts, line))
        if len(lines) > MAX_LOG_LINES:
            lines = lines[-MAX_LOG_LINES:]
        _atomic_write(JOB_LOG_PATH, "".join(lines))


def tail_log(n=60):
    if not os.path.exists(JOB_LOG_PATH):
        return []
    with open(JOB_LOG_PATH, "r", encoding="utf-8") as f:
        lines = f.readlines()
    return [l.rstrip("\n") for l in lines[-n:]]


# ============================================================================
# 2. 네이버웹툰 목록/회차/이미지 접근
# ============================================================================
NAVER_BASE = "https://comic.naver.com"
NAVER_WEBTOON_LIST_URL = NAVER_BASE + "/webtoon"
NAVER_ARTICLE_LIST_API = NAVER_BASE + "/api/article/list"
NAVER_DETAIL_URL = NAVER_BASE + "/webtoon/detail"

# 사용자가 실제로 확인해준 탭 파라미터. 요일 7개 + dailyPlus(일일플러스) + finish(완결).
NAVER_WEEKDAY_TABS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun", "dailyPlus"]
NAVER_FINISH_TAB = "finish"

_IMG_RE = re.compile(r'<img[^>]+class="[^"]*wt_viewer[^"]*"[^>]+src="([^"]+)"', re.I)
_TITLE_RE = re.compile(r'"titleName"\s*:\s*"([^"]*)"')
_NEXT_DATA_RE = re.compile(
    r'<script[^>]+id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.I | re.S)


class NaverAuthExpired(Exception):
    """쿠키(성인/로그인)가 만료되어 인증이 필요한 콘텐츠 접근이 막힌 경우"""


def build_session(cookie_storage_state_json=None, naver_id=None, naver_pw=None, timeout=10):
    """
    cookie_storage_state_json: Playwright storage_state 형식(JSON 문자열 또는 dict)
      {"cookies": [{"name": "NID_AUT", "domain": ".naver.com", "value": "..."}, ...]}
    naver_id/naver_pw는 자동 로그인에는 쓰지 않는다(캡차 등으로 불안정) -
    사용자가 브라우저 확장(Cookie-Editor 등)으로 내보낸 쿠키를 붙여넣는 방식을 권장.
    """
    sess = requests.Session()
    sess.headers.update({
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/126.0.0.0 Safari/537.36"),
        "Referer": NAVER_BASE + "/",
    })
    sess.request_timeout = timeout

    if cookie_storage_state_json:
        data = cookie_storage_state_json
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except json.JSONDecodeError:
                data = None
        if isinstance(data, dict):
            for c in data.get("cookies", []):
                name = c.get("name")
                value = c.get("value")
                domain = (c.get("domain") or ".naver.com").lstrip(".")
                if name and value is not None:
                    sess.cookies.set(name, value, domain="." + domain)
    return sess


def _naver_get(session, url, params=None, referer=None):
    headers = {}
    if referer:
        headers["Referer"] = referer
    resp = session.get(url, params=params, headers=headers,
                        timeout=getattr(session, "request_timeout", 10))
    resp.raise_for_status()
    return resp


def _extract_next_data(html):
    """페이지 HTML에 박혀있는 <script id="__NEXT_DATA__">...JSON...</script>를 파싱한다.
    없으면 None."""
    m = _NEXT_DATA_RE.search(html)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except (json.JSONDecodeError, ValueError):
        return None


def _walk_title_dicts(node, out, seen_ids):
    """__NEXT_DATA__ JSON 트리를 재귀적으로 훑어 titleId를 가진 dict를 전부 모은다.
    정확한 스키마(중첩 깊이/키 이름)를 알 수 없어, 있을 법한 키 이름 후보들을
    폭넓게 시도하는 방어적 방식이다."""
    if isinstance(node, dict):
        title_id = node.get("titleId") or node.get("id")
        title_name = node.get("titleName") or node.get("title") or node.get("name")
        if title_id and title_name and isinstance(title_name, str):
            tid = str(title_id)
            if tid not in seen_ids:
                seen_ids.add(tid)
                author = node.get("author") or node.get("writer") or node.get("authorName") or ""
                if isinstance(author, list):
                    author = ", ".join(
                        a.get("name", str(a)) if isinstance(a, dict) else str(a) for a in author)
                thumbnail = (node.get("thumbnailUrl") or node.get("thumbnail") or
                             node.get("imageUrl") or node.get("img") or "")
                if isinstance(thumbnail, dict):
                    thumbnail = thumbnail.get("url", "")
                out.append({
                    "titleId": tid,
                    "title": title_name,
                    "author": author,
                    "thumbnail": thumbnail,
                    "is_adult": bool(node.get("adult") or node.get("isAdult") or node.get("age19")),
                    "rest": bool(node.get("rest") or node.get("isRest") or node.get("hiatus")),
                    "new": bool(node.get("new") or node.get("isNew")),
                    "tags": node.get("tags") or node.get("genres") or [],
                })
        for v in node.values():
            _walk_title_dicts(v, out, seen_ids)
    elif isinstance(node, list):
        for v in node:
            _walk_title_dicts(v, out, seen_ids)


def _fetch_tab_titles(session, tab, page=None):
    """comic.naver.com/webtoon?tab={tab}(&page=N) 페이지를 받아 __NEXT_DATA__에서
    작품 목록을 추출한다. __NEXT_DATA__를 못 찾으면 빈 리스트(호출부에서 로그로
    남기고 다음 탭으로 넘어가도록)."""
    params = {"tab": tab}
    if page:
        params["page"] = page
    resp = _naver_get(session, NAVER_WEBTOON_LIST_URL, params=params, referer=NAVER_BASE + "/")
    data = _extract_next_data(resp.text)
    if data is None:
        return []
    out = []
    _walk_title_dicts(data, out, set())
    return out


def fetch_weekday_titles(session):
    """요일별(mon~sun) + dailyPlus(일일플러스) 탭을 모두 훑어 병합한다."""
    out = {}
    for tab in NAVER_WEEKDAY_TABS:
        try:
            items = _fetch_tab_titles(session, tab)
        except (requests.RequestException, ValueError):
            continue
        for item in items:
            item["status"] = "연재"
            item.setdefault("weekdays", [])
            if tab not in item["weekdays"]:
                item["weekdays"].append(tab)
            out[item["titleId"]] = _merge_title(out.get(item["titleId"]), item)
        time.sleep(0.2)
    return out


def fetch_finished_titles(session, max_pages=200):
    """완결(finish) 탭을 페이지네이션하며 훑는다. 새로 나오는 titleId가 없어지면 중단."""
    out = {}
    page = 1
    while page <= max_pages:
        try:
            items = _fetch_tab_titles(session, NAVER_FINISH_TAB, page=page)
        except (requests.RequestException, ValueError):
            break
        new_items = [it for it in items if it["titleId"] not in out]
        if not items or not new_items:
            break
        for item in items:
            item["status"] = "완결"
            out[item["titleId"]] = _merge_title(out.get(item["titleId"]), item)
        page += 1
        time.sleep(0.2)
    return out


def fetch_genre_titles(session, genre, max_pages=50):
    """장르/태그 탭도 같은 URL 패턴(tab=장르코드)을 쓴다고 가정하고 동일하게 처리한다.
    실제 장르 탭 파라미터 이름이 다르면(예: genreTab, genre=...) 이 함수만 고치면 된다."""
    out = {}
    page = 1
    while page <= max_pages:
        try:
            items = _fetch_tab_titles(session, genre, page=page if page > 1 else None)
        except (requests.RequestException, ValueError):
            break
        new_items = [it for it in items if it["titleId"] not in out]
        if not items or not new_items:
            break
        for item in items:
            tags = set(item.get("tags", []))
            tags.add(genre)
            item["tags"] = sorted(tags)
            out[item["titleId"]] = _merge_title(out.get(item["titleId"]), item)
        page += 1
        time.sleep(0.2)
    return out


def _merge_title(old, new):
    if not old:
        return new
    merged = dict(old)
    merged.update({k: v for k, v in new.items() if v not in (None, "", [])})
    old_wd = set(old.get("weekdays", []))
    new_wd = set(new.get("weekdays", []))
    if old_wd or new_wd:
        merged["weekdays"] = sorted(old_wd | new_wd)
    old_tags = set(old.get("tags", []))
    new_tags = set(new.get("tags", []))
    if old_tags or new_tags:
        merged["tags"] = sorted(old_tags | new_tags)
    return merged


def fetch_episode_list(session, title_id, max_pages=50):
    episodes = []
    page = 1
    while page <= max_pages:
        try:
            resp = _naver_get(session, NAVER_ARTICLE_LIST_API,
                               params={"titleId": title_id, "page": page},
                               referer="%s?titleId=%s" % (NAVER_DETAIL_URL, title_id))
            body = resp.json()
        except (requests.RequestException, ValueError):
            break
        items = None
        if isinstance(body, dict):
            for path in (("articleList",), ("result", "articleList")):
                cur = body
                ok = True
                for p in path:
                    if isinstance(cur, dict) and p in cur:
                        cur = cur[p]
                    else:
                        ok = False
                        break
                if ok and isinstance(cur, list):
                    items = cur
                    break
        if not items:
            break
        for it in items:
            episodes.append({
                "no": it.get("no"),
                "subtitle": it.get("subtitle", ""),
                "thumbnail": it.get("thumbnailUrl", ""),
                "charge": bool(it.get("charge")),
                "up_type": it.get("serviceUpType", ""),
            })
        is_last = body.get("result", {}).get("isLastPage") if isinstance(body.get("result"), dict) else None
        if is_last is True or len(items) == 0:
            break
        page += 1
        time.sleep(0.15)
    return episodes


def fetch_episode_images(session, title_id, episode_no):
    url = "%s?titleId=%s&no=%s" % (NAVER_DETAIL_URL, title_id, episode_no)
    resp = _naver_get(session, url, referer=NAVER_BASE + "/")
    html = resp.text

    if "성인인증" in html or "adult_ok" in html or "만 19세" in html:
        imgs = _IMG_RE.findall(html)
        if not imgs:
            raise NaverAuthExpired(
                "titleId=%s no=%s: 성인 인증이 필요하거나 쿠키가 만료된 것으로 보임" %
                (title_id, episode_no))

    imgs = _IMG_RE.findall(html)
    imgs = [u for u in imgs if "image-comic" in u or "comicimage" in u or "cptoon" in u or u.startswith("http")]
    if not imgs:
        raise ValueError("titleId=%s no=%s: 이미지 목록을 찾지 못함(페이지 구조 변경 가능성)" %
                          (title_id, episode_no))
    return imgs


def guess_title_meta(session, title_id):
    url = "%s?titleId=%s" % (NAVER_DETAIL_URL, title_id)
    resp = _naver_get(session, url, referer=NAVER_BASE + "/")
    m = _TITLE_RE.search(resp.text)
    return {"titleId": str(title_id), "title": m.group(1) if m else ("titleId %s" % title_id)}


# ============================================================================
# 3. 이미지 다운로드
# ============================================================================
_SAFE_RE = re.compile(r'[\\/:*?"<>|]')


def safe_name(name):
    name = _SAFE_RE.sub("_", str(name)).strip()
    return name or "untitled"


def episode_dir(download_root, title, title_id, episode_no, folder_zero_fill=4):
    folder = safe_name("%s (%s)" % (title, title_id))
    ep_name = str(episode_no).zfill(int(folder_zero_fill or 4))
    return os.path.join(download_root, folder, ep_name)


def download_episode(session, download_root, title, title_id, episode_no,
                      image_zero_fill=4, folder_zero_fill=4,
                      max_concurrent=5, delay_seconds=1.0, timeout=10, log=None):
    """반환: (ok: bool, skipped: bool, image_count: int, error: str|None)"""
    target_dir = episode_dir(download_root, title, title_id, episode_no, folder_zero_fill)
    if os.path.isdir(target_dir) and any(
            f.lower().endswith((".jpg", ".jpeg", ".png", ".webp")) for f in os.listdir(target_dir)):
        return True, True, len(os.listdir(target_dir)), None

    try:
        images = fetch_episode_images(session, title_id, episode_no)
    except NaverAuthExpired:
        raise
    except Exception as e:  # noqa: BLE001
        return False, False, 0, str(e)

    os.makedirs(target_dir, exist_ok=True)
    referer = "%s?titleId=%s&no=%s" % (NAVER_DETAIL_URL, title_id, episode_no)

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
                log("이미지 다운로드 실패 titleId=%s no=%s idx=%s: %s" % (title_id, episode_no, idx, e))
            return False

    ok_count = 0
    with ThreadPoolExecutor(max_workers=max(1, int(max_concurrent or 5))) as ex:
        futures = [ex.submit(_dl_one, pair) for pair in enumerate(images)]
        for fut in as_completed(futures):
            if fut.result():
                ok_count += 1

    if delay_seconds:
        time.sleep(float(delay_seconds))

    if ok_count == 0:
        return False, False, 0, "이미지 0장 저장됨(전체 실패)"
    return True, False, ok_count, None


# ============================================================================
# 4. 디스코드 알림
# ============================================================================
COLOR_INFO = 0x3498DB
COLOR_WARN = 0xE67E22
COLOR_DONE = 0x2ECC71
COLOR_ERROR = 0xE74C3C


def _send_webhook(webhook_url, embed, timeout=10):
    if not webhook_url:
        return False, "웹훅 URL 없음"
    try:
        resp = requests.post(webhook_url, json={"embeds": [embed]}, timeout=timeout)
        if resp.status_code >= 300:
            return False, "웹훅 응답 코드 %s: %s" % (resp.status_code, resp.text[:200])
        return True, "ok"
    except requests.RequestException as e:
        return False, str(e)


def _send_bot_message(bot_token, channel_id, content, embed=None, timeout=10):
    if not bot_token or not channel_id:
        return False, "봇 토큰/채널ID 없음"
    url = "https://discord.com/api/v10/channels/%s/messages" % channel_id
    headers = {"Authorization": "Bot %s" % bot_token}
    payload = {"content": content}
    if embed:
        payload["embeds"] = [embed]
    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=timeout)
        if resp.status_code >= 300:
            return False, "봇 응답 코드 %s: %s" % (resp.status_code, resp.text[:200])
        return True, "ok"
    except requests.RequestException as e:
        return False, str(e)


def discord_notify(cfg, title, description, color=COLOR_INFO, fields=None, mention_manage_tab=True):
    embed = {"title": title, "description": description, "color": color}
    if fields:
        embed["fields"] = [{"name": k, "value": str(v), "inline": True} for k, v in fields.items()]
    if mention_manage_tab:
        embed.setdefault("footer", {"text": "BookOasis > 웹툰 관리 카테고리탭에서 확인/처리하세요."})

    ok_any = False
    errs = []
    webhook_url = cfg.get("DISCORD_WEBHOOK_URL")
    if webhook_url:
        ok, msg = _send_webhook(webhook_url, embed)
        ok_any = ok_any or ok
        if not ok:
            errs.append("webhook: %s" % msg)

    bot_token = cfg.get("DISCORD_BOT_TOKEN")
    channel_id = cfg.get("DISCORD_CHANNEL_ID")
    if bot_token and channel_id:
        ok, msg = _send_bot_message(bot_token, channel_id, content="", embed=embed)
        ok_any = ok_any or ok
        if not ok:
            errs.append("bot: %s" % msg)

    if not webhook_url and not (bot_token and channel_id):
        return False, "디스코드 알림 대상이 설정되지 않음"
    return ok_any, ("; ".join(errs) if errs else "ok")


def notify_finished(cfg, title, title_id):
    return discord_notify(
        cfg, "📗 완결 감지: %s" % title,
        "titleId=%s 작품이 완결로 확인되었습니다. 카테고리탭의 '구독중' 목록에서 "
        "구독해제 또는 알람만 끄기를 선택해주세요." % title_id,
        color=COLOR_DONE, fields={"titleId": title_id})


def notify_cookie_expired(cfg):
    return discord_notify(
        cfg, "🍪 네이버 쿠키 만료",
        "성인 인증 쿠키(로그인 세션)가 만료된 것으로 보입니다. "
        "플러그인 설정에서 쿠키(JSON)를 새로 발급해 넣어주세요.",
        color=COLOR_WARN)


def notify_failures(cfg, failures):
    if not failures:
        return True, "no failures"
    lines = ["- %s (titleId=%s, no=%s): %s" % (f.get("title"), f.get("title_id"),
                                                  f.get("episode_no"), f.get("error"))
              for f in failures[:20]]
    more = "" if len(failures) <= 20 else "\n...외 %d건" % (len(failures) - 20)
    return discord_notify(
        cfg, "⚠️ 다운로드 실패 요약 (%d건)" % len(failures),
        "\n".join(lines) + more, color=COLOR_ERROR)


# ============================================================================
# 5. 스캔 -> 자동구독 -> 다운로드 -> 알림 파이프라인
# ============================================================================
def _cfg_num(cfg, key, default):
    try:
        v = cfg.get(key)
        return float(v) if v not in (None, "") else default
    except (TypeError, ValueError):
        return default


def build_session_from_cfg(cfg):
    return build_session(
        cookie_storage_state_json=cfg.get("NAVER_COOKIE_JSON"),
        naver_id=cfg.get("NAVER_ID"),
        naver_pw=cfg.get("NAVER_PW"),
        timeout=int(_cfg_num(cfg, "REQUEST_TIMEOUT_SECONDS", 10)),
    )


def run_scan(cfg, log=print):
    session = build_session_from_cfg(cfg)
    save_job_state({"stage": "scanning", "message": "요일별 목록 수집 중"})
    log("요일별 연재 목록 수집 시작")
    merged = {}
    try:
        merged.update(fetch_weekday_titles(session))
    except Exception as e:  # noqa: BLE001
        log("요일별 목록 수집 실패: %s" % e)

    save_job_state({"message": "완결 목록 수집 중"})
    log("완결 목록 수집 시작")
    try:
        merged.update(fetch_finished_titles(session))
    except Exception as e:  # noqa: BLE001
        log("완결 목록 수집 실패: %s" % e)

    at = load_authors_tags()
    for tag in at.get("tags", []):
        save_job_state({"message": "태그 '%s' 목록 수집 중" % tag})
        try:
            merged.update(fetch_genre_titles(session, tag))
        except Exception as e:  # noqa: BLE001
            log("태그 '%s' 수집 실패: %s" % (tag, e))

    old_titles = load_titles()
    finished_events = []
    author_names = set(a.strip() for a in at.get("authors", []) if a.strip())

    patch = {}
    for tid, item in merged.items():
        old = old_titles.get(tid, {})
        was_finished = old.get("status") == "완결"
        now_finished = item.get("status") == "완결"
        if now_finished and not was_finished and old.get("subscribed"):
            finished_events.append({"titleId": tid, "title": item.get("title") or old.get("title")})

        p = dict(item)
        if "subscribed" in old or "excluded" in old:
            p["subscribed"] = old.get("subscribed", False)
            p["excluded"] = old.get("excluded", False)
            p["unsubscribed"] = old.get("unsubscribed", False)
        else:
            auto = bool(item.get("author") and any(a in item.get("author", "") for a in author_names))
            p["subscribed"] = auto
            p["excluded"] = False
            p["unsubscribed"] = False
        p["last_seen_at"] = time.time()
        patch[tid] = p

    upsert_title(patch)
    save_job_state({"last_scan_at": time.time()})
    log("스캔 완료: 총 %d개 작품" % len(patch))

    for ev in finished_events:
        notify_finished(cfg, ev["title"], ev["titleId"])
    return {"scanned": len(patch), "finished_events": finished_events}


def _episodes_to_download(session, title_id, known_last_no):
    episodes = fetch_episode_list(session, title_id)
    new_eps = [e for e in episodes if isinstance(e.get("no"), int) and e["no"] > (known_last_no or 0)]
    new_eps.sort(key=lambda e: e["no"])
    return new_eps


def run_download_cycle(cfg, log=print):
    session = build_session_from_cfg(cfg)
    titles = load_titles()
    subscribed = {tid: t for tid, t in titles.items()
                  if t.get("subscribed") and not t.get("excluded") and not t.get("unsubscribed")}

    download_root = cfg.get("DOWNLOAD_ROOT") or DOWNLOAD_DEFAULT_DIR
    max_new = int(_cfg_num(cfg, "MAX_NEW_EPISODES_PER_TITLE", 10))
    batch_rest_min = _cfg_num(cfg, "BATCH_REST_MINUTES", 5.0)
    max_concurrent = int(_cfg_num(cfg, "MAX_CONCURRENT_DOWNLOADS", 5))
    delay_seconds = _cfg_num(cfg, "DELAY_SECONDS", 1.0)
    image_zero_fill = int(_cfg_num(cfg, "IMAGE_ZERO_FILL", 4))
    folder_zero_fill = int(_cfg_num(cfg, "FOLDER_ZERO_FILL", 4))
    timeout = int(_cfg_num(cfg, "REQUEST_TIMEOUT_SECONDS", 10))

    save_job_state({"stage": "downloading", "message": "구독 작품 회차 확인 중",
                     "progress": 0, "total": len(subscribed)})

    failures = []
    downloaded_count = 0
    cookie_expired = False

    for i, (tid, t) in enumerate(subscribed.items()):
        if load_job_state().get("cancel_requested"):
            log("사용자 요청으로 다운로드 사이클 취소")
            break

        save_job_state({"progress": i, "message": "%s 새 회차 확인 중" % t.get("title", tid)})
        try:
            new_eps = _episodes_to_download(session, tid, t.get("last_downloaded_no"))
        except Exception as e:  # noqa: BLE001
            log("회차 목록 조회 실패 titleId=%s: %s" % (tid, e))
            continue

        if not new_eps:
            continue

        capped = new_eps if max_new <= 0 else new_eps[:max_new]
        rest_needed = max_new > 0 and len(new_eps) > max_new

        last_ok_no = t.get("last_downloaded_no")
        for ep in capped:
            try:
                ok, skipped, img_count, err = download_episode(
                    session, download_root, t.get("title", tid), tid, ep["no"],
                    image_zero_fill=image_zero_fill, folder_zero_fill=folder_zero_fill,
                    max_concurrent=max_concurrent, delay_seconds=delay_seconds,
                    timeout=timeout, log=log)
            except NaverAuthExpired as e:
                log("인증 만료: %s" % e)
                cookie_expired = True
                break

            if ok:
                last_ok_no = ep["no"]
                if not skipped:
                    downloaded_count += 1
                    append_history({
                        "type": "download", "title_id": tid, "title": t.get("title", tid),
                        "episode_no": ep["no"], "subtitle": ep.get("subtitle"),
                        "image_count": img_count,
                    })
            else:
                failures.append({"title_id": tid, "title": t.get("title", tid),
                                  "episode_no": ep["no"], "error": err})
                append_history({
                    "type": "download_fail", "title_id": tid, "title": t.get("title", tid),
                    "episode_no": ep["no"], "error": err,
                })

        if last_ok_no != t.get("last_downloaded_no"):
            upsert_title({tid: {"last_downloaded_no": last_ok_no, "up_flag": rest_needed}})

        if cookie_expired:
            break

        if rest_needed and batch_rest_min > 0:
            log("titleId=%s 회차가 많이 밀려 다음 주기로 이어받음(휴식)" % tid)
            time.sleep(min(batch_rest_min * 60, 60))

    save_job_state({"progress": len(subscribed), "message": "다운로드 사이클 종료"})

    if cookie_expired:
        notify_cookie_expired(cfg)
    if failures:
        notify_failures(cfg, failures)

    log("다운로드 사이클 완료: 신규 %d화, 실패 %d건" % (downloaded_count, len(failures)))
    return {"downloaded": downloaded_count, "failures": failures, "cookie_expired": cookie_expired}


def run_full_cycle(cfg, log=print):
    save_job_state({"running": True, "started_at": time.time(), "last_error": None,
                     "cancel_requested": False})
    try:
        scan_result = run_scan(cfg, log=log)
        dl_result = run_download_cycle(cfg, log=log)
        save_job_state({"running": False, "stage": "done", "finished_at": time.time(),
                         "message": "완료: 스캔 %d개 / 신규 %d화 / 실패 %d건" % (
                             scan_result["scanned"], dl_result["downloaded"],
                             len(dl_result["failures"]))})
        return {"scan": scan_result, "download": dl_result}
    except Exception as e:  # noqa: BLE001
        log("파이프라인 실행 중 오류: %s" % e)
        save_job_state({"running": False, "stage": "error", "finished_at": time.time(),
                         "last_error": str(e)})
        raise


# ============================================================================
# 6. 백그라운드 스케줄러 (프로세스당 1개만 뜨도록 PID 락 파일로 가드)
# ============================================================================
_sched_started_in_process = False
_sched_lock = threading.Lock()


def _pid_alive(pid):
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
    except (OSError, ValueError, TypeError):
        return False
    return True


def _sched_write_lock():
    ensure_dirs()
    with open(SCHED_LOCK_PATH, "w", encoding="utf-8") as f:
        f.write(str(os.getpid()))


def _sched_read_lock():
    if not os.path.exists(SCHED_LOCK_PATH):
        return None
    try:
        with open(SCHED_LOCK_PATH, "r", encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return None


def _sched_loop(get_cfg_func):
    while True:
        cfg = get_cfg_func() or {}
        enabled = str(cfg.get("ENABLE_SCHEDULER", "")).lower() in ("1", "true", "on", "y", "yes")
        try:
            interval_min = float(cfg.get("INTERVAL_MINUTES"))
        except (TypeError, ValueError):
            interval_min = 240.0
        interval_min = max(10.0, interval_min)

        if enabled:
            job = load_job_state()
            due = (not job.get("running")) and (
                job.get("last_scan_at") is None or
                (time.time() - job.get("last_scan_at", 0)) >= interval_min * 60)
            if due:
                try:
                    run_full_cycle(cfg, log=append_log)
                except Exception as e:  # noqa: BLE001
                    append_log("스케줄러 실행 오류: %s" % e)

        time.sleep(60)


def scheduler_ensure_started(get_cfg_func):
    global _sched_started_in_process
    with _sched_lock:
        if _sched_started_in_process:
            return False
        existing_pid = _sched_read_lock()
        if _pid_alive(existing_pid) and existing_pid != str(os.getpid()):
            _sched_started_in_process = True
            return False
        _sched_write_lock()
        t = threading.Thread(target=_sched_loop, args=(get_cfg_func,),
                              name="webtoon_manager_scheduler", daemon=True)
        t.start()
        _sched_started_in_process = True
        return True


def _action_slug(label):
    return "".join(c for c in label if c.isalnum()) or "job"


# ============================================================================
# 7. BookOasis 플러그인 클래스
# ============================================================================
class WebtoonManagerMetadataProvider(BaseMetadataProvider):
    id = PLUGIN_ID
    name = "웹툰 관리"
    is_searchable = False

    config_schema = [
        {"key": "NAVER_ID", "label": "네이버 아이디", "type": "text"},
        {"key": "NAVER_PW", "label": "네이버 비밀번호", "type": "password"},
        {"key": "NAVER_COOKIE_JSON", "label": "네이버 쿠키(JSON, storage_state 형식)",
         "type": "password", "required": False},
        {"key": "DOWNLOAD_ROOT", "label": "다운로드 저장 경로(비우면 플러그인 기본 경로)", "type": "text"},
        {"key": "ENABLE_SCHEDULER", "label": "자동 실행(스케줄러) 사용", "type": "checkbox", "default": False},
        {"key": "INTERVAL_MINUTES", "label": "실행 주기(분, 최소 10)", "type": "number", "default": 240},
        {"key": "MAX_NEW_EPISODES_PER_TITLE", "label": "1회 실행당 작품별 최대 신규 다운로드 회차 수(0=무제한)",
         "type": "number", "default": 10},
        {"key": "BATCH_REST_MINUTES", "label": "상한 도달 시 휴식(분)", "type": "number", "default": 5},
        {"key": "MAX_CONCURRENT_DOWNLOADS", "label": "이미지 동시 다운로드 수", "type": "number", "default": 5},
        {"key": "DELAY_SECONDS", "label": "회차 간 대기(초)", "type": "number", "default": 1.0},
        {"key": "REQUEST_TIMEOUT_SECONDS", "label": "요청 타임아웃(초)", "type": "number", "default": 10},
        {"key": "FOLDER_ZERO_FILL", "label": "회차 폴더명 자릿수", "type": "number", "default": 4},
        {"key": "IMAGE_ZERO_FILL", "label": "이미지 파일명 자릿수", "type": "number", "default": 4},
        {"key": "DISCORD_WEBHOOK_URL", "label": "디스코드 웹훅 URL(선택)", "type": "text"},
        {"key": "DISCORD_BOT_TOKEN", "label": "디스코드 봇 토큰(선택, 완결확인용)", "type": "password"},
        {"key": "DISCORD_CHANNEL_ID", "label": "디스코드 채널 ID(선택)", "type": "text"},
    ]

    # 좌측 사이드바 카테고리 메뉴로 등록 (plugin_board 등 기존 카테고리탭
    # 플러그인에서 확인된 실제 규격 — dict여야 사이드바에 항목이 생긴다)
    category_tab = {
        "title": "웹툰 관리",
        "icon": "fa-solid fa-book-open-reader",
        "order": 50,
    }

    update_manifest = {
        "enabled": True,
        "provider": "github-raw",
        "raw_base_url": "https://raw.githubusercontent.com/yume-script/webtoon_manager/main",
        "files": ["webtoon_manager.py", "__init__.py", "VERSION",
                   "index.html", "style.css", "script.js"],
        "version_file": "VERSION",
        "version_key": "plugin version",
        "show_sample_update_button": True,
    }

    # ---- 필수 계약 ----
    def search(self, db_type, query):
        return {"success": True, "items": []}

    def apply(self, db_type, book_id, item_data):
        item_data = item_data or {}
        action = item_data.get("action")
        if action:
            return self._dispatch(db_type, action, item_data)
        return False, "이 플러그인은 메타데이터 적용 대상이 아닙니다. 카테고리탭에서 사용하세요."

    def get_context_menu_items(self, db_type, context):
        return []

    def run_context_menu_action(self, db_type, action_id, context):
        return self._dispatch(db_type, action_id, context or {})

    def run_action(self, db_type, item_data):
        item_data = item_data or {}
        return self._dispatch(db_type, item_data.get("action"), item_data)

    # ---- 설정 헬퍼 ----
    def _get_cfg(self, db_type):
        cfg = self.get_plugin_config(db_type, default={}) or {}
        merged = dict(DEFAULTS)
        merged.update({k: v for k, v in cfg.items() if v not in (None, "")})
        if not merged.get("DOWNLOAD_ROOT"):
            merged["DOWNLOAD_ROOT"] = DOWNLOAD_DEFAULT_DIR
        return merged

    def _save_cfg_patch(self, db_type, patch):
        cfg = self.get_plugin_config(db_type, default={}) or {}
        cfg.update(patch)
        try:
            self.set_plugin_config(db_type, cfg)
        except AttributeError:
            gw = self.get_db_gateway(db_type)
            gw.set_setting("PLUGIN_CONFIG_%s" % self.id, json.dumps(cfg, ensure_ascii=False))
        return True

    # ---- 대시보드/카테고리탭 데이터 ----
    def get_dashboard_data(self, db_type, limit=10):
        cfg = self._get_cfg(db_type)
        try:
            scheduler_ensure_started(lambda: self._get_cfg(db_type))
        except Exception:  # noqa: BLE001
            pass

        titles = load_titles()
        items_list = []
        for tid, t in titles.items():
            item = dict(t)
            item["titleId"] = tid
            items_list.append(item)
        items_list.sort(key=lambda x: x.get("last_seen_at", 0), reverse=True)

        bundle = {
            "titles": items_list,
            "authors_tags": load_authors_tags(),
            "history": load_history(limit=200),
            "job": load_job_state(),
            "log_tail": tail_log(60),
            "config_public": {
                "NAVER_ID": cfg.get("NAVER_ID", ""),
                "DOWNLOAD_ROOT": cfg.get("DOWNLOAD_ROOT", ""),
                "ENABLE_SCHEDULER": bool(cfg.get("ENABLE_SCHEDULER")),
                "INTERVAL_MINUTES": cfg.get("INTERVAL_MINUTES"),
                "MAX_NEW_EPISODES_PER_TITLE": cfg.get("MAX_NEW_EPISODES_PER_TITLE"),
                "BATCH_REST_MINUTES": cfg.get("BATCH_REST_MINUTES"),
                "MAX_CONCURRENT_DOWNLOADS": cfg.get("MAX_CONCURRENT_DOWNLOADS"),
                "DELAY_SECONDS": cfg.get("DELAY_SECONDS"),
                "has_cookie": bool(cfg.get("NAVER_COOKIE_JSON")),
                "has_discord": bool(cfg.get("DISCORD_WEBHOOK_URL") or
                                     (cfg.get("DISCORD_BOT_TOKEN") and cfg.get("DISCORD_CHANNEL_ID"))),
            },
        }
        return {"success": True, "items": [bundle]}

    # ---- 액션 dispatch ----
    def _dispatch(self, db_type, action, payload):
        try:
            if action == "save_settings":
                return self._act_save_settings(db_type, payload)
            if action == "scan_now":
                return self._act_run_bg(db_type, run_scan, "스캔")
            if action == "run_full_cycle_now":
                return self._act_run_bg(db_type, run_full_cycle, "전체 실행")
            if action == "cancel_job":
                save_job_state({"cancel_requested": True})
                return True, "취소 요청됨"
            if action == "subscribe":
                return self._act_set_flags(payload.get("titleId"), subscribed=True,
                                            excluded=False, unsubscribed=False)
            if action == "unsubscribe":
                return self._act_set_flags(payload.get("titleId"), subscribed=False, unsubscribed=True)
            if action == "exclude":
                return self._act_set_flags(payload.get("titleId"), subscribed=False, excluded=True)
            if action == "restore":
                return self._act_set_flags(payload.get("titleId"), subscribed=True,
                                            excluded=False, unsubscribed=False)
            if action == "add_author":
                return self._act_authors_tags("authors", payload.get("value"), add=True)
            if action == "remove_author":
                return self._act_authors_tags("authors", payload.get("value"), add=False)
            if action == "add_tag":
                return self._act_authors_tags("tags", payload.get("value"), add=True)
            if action == "remove_tag":
                return self._act_authors_tags("tags", payload.get("value"), add=False)
            if action == "manual_lookup":
                return self._act_manual_lookup(db_type, payload.get("titleId"))
            if action == "manual_download":
                return self._act_manual_download(db_type, payload)
            if action == "test_discord":
                return self._act_test_discord(db_type)
            return False, "알 수 없는 action: %s" % action
        except Exception as e:  # noqa: BLE001
            return False, "오류: %s" % e

    def _act_save_settings(self, db_type, payload):
        patch = {k: v for k, v in payload.items() if k not in ("action",)}
        self._save_cfg_patch(db_type, patch)
        return True, "설정 저장됨"

    def _act_run_bg(self, db_type, func, label):
        job = load_job_state()
        if job.get("running"):
            return False, "이미 실행 중인 작업이 있습니다"
        cfg = self._get_cfg(db_type)
        save_job_state({"running": True, "stage": "starting", "message": "%s 시작" % label,
                         "started_at": time.time(), "cancel_requested": False, "last_error": None})

        def _runner():
            try:
                func(cfg, log=append_log)
            except Exception as e:  # noqa: BLE001
                append_log("%s 실행 실패: %s" % (label, e))
                save_job_state({"running": False, "stage": "error", "last_error": str(e)})

        t = threading.Thread(target=_runner, name="webtoon_manager_%s" % _action_slug(label), daemon=True)
        t.start()
        return True, "%s 시작됨(백그라운드)" % label

    def _act_set_flags(self, title_id, **flags):
        if not title_id:
            return False, "titleId 필요"
        upsert_title({str(title_id): flags})
        return True, "적용됨"

    def _act_authors_tags(self, key, value, add):
        value = (value or "").strip()
        if not value:
            return False, "값을 입력하세요"
        at = load_authors_tags()
        items = at.get(key, [])
        if add:
            if value not in items:
                items.append(value)
        else:
            items = [i for i in items if i != value]
        at[key] = items
        save_authors_tags(at)
        return True, json.dumps(at, ensure_ascii=False)

    def _act_manual_lookup(self, db_type, title_id):
        if not title_id:
            return False, "titleId 필요"
        cfg = self._get_cfg(db_type)
        session = build_session_from_cfg(cfg)
        try:
            meta = guess_title_meta(session, title_id)
            episodes = fetch_episode_list(session, title_id, max_pages=3)
            meta["episodes"] = episodes[:60]
            return True, json.dumps(meta, ensure_ascii=False)
        except Exception as e:  # noqa: BLE001
            return False, "조회 실패: %s" % e

    def _act_manual_download(self, db_type, payload):
        title_id = payload.get("titleId")
        title = payload.get("title") or title_id
        episode_nos = payload.get("episodeNos") or []
        if not title_id or not episode_nos:
            return False, "titleId/episodeNos 필요"

        job = load_job_state()
        if job.get("running"):
            return False, "이미 실행 중인 작업이 있습니다"

        cfg = self._get_cfg(db_type)
        save_job_state({"running": True, "stage": "downloading", "message": "수동 다운로드 시작",
                         "started_at": time.time(), "cancel_requested": False, "last_error": None,
                         "progress": 0, "total": len(episode_nos)})

        def _runner():
            session = build_session_from_cfg(cfg)
            ok_count = 0
            for i, no in enumerate(episode_nos):
                if load_job_state().get("cancel_requested"):
                    append_log("수동 다운로드 취소됨")
                    break
                save_job_state({"progress": i, "message": "%s %s화 다운로드 중" % (title, no)})
                try:
                    ok, skipped, cnt, err = download_episode(
                        session, cfg.get("DOWNLOAD_ROOT") or DOWNLOAD_DEFAULT_DIR,
                        title, title_id, no,
                        image_zero_fill=int(cfg.get("IMAGE_ZERO_FILL", 4)),
                        folder_zero_fill=int(cfg.get("FOLDER_ZERO_FILL", 4)),
                        max_concurrent=int(cfg.get("MAX_CONCURRENT_DOWNLOADS", 5)),
                        delay_seconds=float(cfg.get("DELAY_SECONDS", 1.0)),
                        timeout=int(cfg.get("REQUEST_TIMEOUT_SECONDS", 10)),
                        log=append_log)
                    if ok:
                        ok_count += 1 if not skipped else 0
                        append_history({"type": "manual_download", "title_id": title_id,
                                         "title": title, "episode_no": no, "image_count": cnt})
                    else:
                        append_history({"type": "manual_download_fail", "title_id": title_id,
                                         "title": title, "episode_no": no, "error": err})
                except NaverAuthExpired as e:
                    append_log("인증 만료: %s" % e)
                    notify_cookie_expired(cfg)
                    break
            save_job_state({"running": False, "stage": "done", "finished_at": time.time(),
                             "message": "수동 다운로드 완료(%d화)" % ok_count})

        t = threading.Thread(target=_runner, name="webtoon_manager_manual_dl", daemon=True)
        t.start()
        return True, "수동 다운로드 시작됨(백그라운드)"

    def _act_test_discord(self, db_type):
        cfg = self._get_cfg(db_type)
        return discord_notify(cfg, "🔔 웹툰 관리 플러그인 테스트",
                               "이 메시지가 보이면 디스코드 알림 설정이 정상입니다.")
