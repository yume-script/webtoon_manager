# -*- coding: utf-8 -*-
"""
카카오페이지(page.kakao.com) 웹툰 API 클라이언트
------------------------------------------------
참고: basilro/kaka_pe_dl(FlaskFarm 플러그인, 핵심 로직은 비공개 .pyf라 README의
동작 설명만 참고) + umzi2/kakao_downloader(MIT, 공개 REST 엔드포인트).

쓰는 엔드포인트 (bff-page.kakao.com)
- GET  /api/gateway/api/v2/content/product/list   회차 목록(커서 페이지네이션, 오래된 순)
- GET  /api/gateway/api/v1/viewer/data            회차 이미지 목록(files[].secureUrl)
- POST /api/gateway/api/v1/ticket/use             기다무(대여권 RT05) 사용 - 옵션
- POST /api/refresh_token                         로그인 토큰 연장
작품 정보(제목/표지/소개/작가)는 page.kakao.com/content/<id> HTML의 og 메타와
Next.js 데이터에서 방어적으로 읽는다(형식이 바뀌면 빈 값으로 두고 계속 진행).

주의
- 로그인 쿠키로 "그 계정이 볼 수 있는" 회차만 받는다. 유료 회차 구매는 하지 않는다.
- 기다무 사용은 계정의 대여권을 실제로 소모하므로 설정에서 켠 경우에만 한다.
"""
import hashlib
import json
import os
import re
import time

import requests

from . import state_store as ss

BASE = "https://page.kakao.com"
BFF = "https://bff-page.kakao.com"
PRODUCT_LIST_API = BFF + "/api/gateway/api/v2/content/product/list"
VIEWER_DATA_API = BFF + "/api/gateway/api/v1/viewer/data"
TICKET_USE_API = BFF + "/api/gateway/api/v1/ticket/use"
REFRESH_TOKEN_API = BFF + "/api/refresh_token"
RENTAL_TICKET_TYPE = "RT05"  # 기다무 대여권
PAGE_SIZE = 25

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# refresh_token으로 갱신된 쿠키를 저장해 두는 파일. 설정의 쿠키가 바뀌면
# (해시가 달라지면) 이 파일은 무시하고 설정 값부터 다시 시작한다.
COOKIE_STATE_PATH = os.path.join(ss.DATA_DIR, "kakao_cookie_state.json")

_SERIES_ID_RE = re.compile(r"(?:content/|series_id=|seriesId=)(\d+)")


class KakaoAuthExpired(Exception):
    """401/403 - 쿠키 만료 또는 미로그인"""


class KakaoNotPurchased(Exception):
    """구매/대여하지 않은 회차(api_content_not_purchased_item 등)"""


class KakaoAdultRequired(Exception):
    """성인 인증이 필요한 작품(api_content_adult_auth_required) - 쿠키 없음/미인증 계정"""


class KakaoSkip(Exception):
    """받을 수 없는 특수 회차(동영상 트레일러 등 - 서버가 오류 코드로 응답). 건너뛴다."""


class KakaoUnsupported(Exception):
    """이미지 웹툰이 아닌 회차(웹소설 텍스트 뷰어 등)"""


def parse_series_id(text):
    """'https://page.kakao.com/content/54801072' 또는 '54801072' -> '54801072'"""
    text = str(text or "").strip()
    if text.isdigit():
        return text
    m = _SERIES_ID_RE.search(text)
    return m.group(1) if m else None


# ---------------------------------------------------------------------------
# 세션 / 쿠키
# ---------------------------------------------------------------------------
def _parse_cookie_input(raw):
    """다음 형식을 모두 받는다.
    - 브라우저 개발자도구의 Cookie 헤더 문자열: "_kpiid=..; _kpwtkn=.."
    - Cookie-Editor 내보내기 JSON: [{"name":..,"value":..,"domain":..}, ...]
    - Playwright storage_state JSON: {"cookies": [...]}
    반환: [(name, value, domain)]"""
    raw = (raw or "").strip()
    if not raw:
        return []
    if raw[0] in "[{":
        try:
            data = json.loads(raw)
        except ValueError:
            data = None
        if isinstance(data, dict):
            data = data.get("cookies") or []
        if isinstance(data, list):
            out = []
            for c in data:
                if isinstance(c, dict) and c.get("name"):
                    dom = c.get("domain") or ".kakao.com"
                    if "kakao" not in dom:
                        continue
                    out.append((c["name"], str(c.get("value", "")), dom))
            return out
    out = []
    for part in raw.replace("\n", ";").split(";"):
        if "=" not in part:
            continue
        name, value = part.split("=", 1)
        name = name.strip()
        # 헤더를 통째로 붙여넣으면 섞여 들어오는 쿠키 속성은 버린다
        if not name or name.lower() in ("domain", "path", "max-age", "expires",
                                        "secure", "httponly", "samesite", "cookie"):
            continue
        out.append((name, value.strip(), ".kakao.com"))
    return out


def _cookie_hash(raw):
    return hashlib.sha1((raw or "").encode("utf-8")).hexdigest()


def build_session(cookie_raw, timeout=15):
    s = requests.Session()
    s.headers.update({
        "User-Agent": UA,
        "Origin": BASE,
        "Referer": BASE + "/",
        "Accept": "application/json, text/plain, */*",
    })
    s.request_timeout = int(timeout or 15)

    cookies = _parse_cookie_input(cookie_raw)
    saved = ss.read_json(COOKIE_STATE_PATH, {})
    if saved.get("source_hash") == _cookie_hash(cookie_raw) and saved.get("cookies"):
        # refresh_token으로 연장된 최신 쿠키가 있으면 그걸 우선 사용
        cookies = [(c["name"], c["value"], c.get("domain") or ".kakao.com")
                   for c in saved["cookies"] if c.get("name")]
    for name, value, _dom in cookies:
        # 브라우저에서 export한 쿠키는 domain이 "page.kakao.com"(호스트 전용)인 것도
        # 섞여 있다. 그대로 넣으면 실제 API 서버(bff-page.kakao.com)로는 전송되지
        # 않아 로그인/성인 인증이 안 된 것처럼 동작하므로 전부 .kakao.com으로 맞춘다.
        s.cookies.set(name, value, domain=".kakao.com", path="/")
    s.kakao_cookie_source_hash = _cookie_hash(cookie_raw)
    s.kakao_has_cookie = bool(cookies)
    return s


REQUIRED_COOKIES = ("_kau", "_kpwtkn", "_T_ANO", "_karmt", "_kahai", "_kawlt", "_kpdid")
PROFILE_API = BFF + "/api/gateway/api/v1/user/get_profile"
# 쿠키 검증 때 성인 인증 여부를 실제로 확인할 기본 성인 작품(무료 프롤로그가 있는 작품)
ADULT_PROBE_SERIES = "66346705"


def missing_required_cookies(cookie_raw):
    names = set(n for n, _v, _d in _parse_cookie_input(cookie_raw))
    return [c for c in REQUIRED_COOKIES if c not in names]


def verify_cookie(session, adult_series_ids=None):
    """로그인 상태와 성인 인증 여부를 확인한다.
    성인 인증은 전용 API 응답 형식이 공개돼 있지 않아, 실제 성인 작품의 무료 회차
    이미지 목록을 요청해 보는 방식으로 판정한다(가장 확실한 방법).
    반환: {"logged_in": bool, "adult": True/False/None, "nickname": str, "detail": str}"""
    out = {"logged_in": False, "adult": None, "nickname": "", "detail": ""}
    try:
        r = session.post(PROFILE_API, timeout=session.request_timeout)
        body = json.loads(r.content.decode("utf-8"))
        out["logged_in"] = body.get("result_code") in (0, "0")
        res = body.get("result") or {}
        if isinstance(res, dict):
            for k in ("nickname", "nick_name", "name", "user_name"):
                if res.get(k):
                    out["nickname"] = str(res[k])
                    break
        if not out["logged_in"]:
            out["detail"] = "%s (%s)" % (body.get("message") or "", body.get("message_key") or "")
    except Exception as e:  # noqa: BLE001
        out["detail"] = "로그인 확인 실패: %s" % e

    for sid in list(adult_series_ids or []) + [ADULT_PROBE_SERIES]:
        try:
            r = session.get(PRODUCT_LIST_API, params={
                "series_id": sid, "cursor_index": 0, "cursor_direction": "NEXT",
                "window_size": 5, "sort_type": "asc"}, timeout=session.request_timeout)
            items = (json.loads(r.content.decode("utf-8")).get("result") or {}).get("list") or []
            free = next((it.get("item") for it in items
                         if (it.get("item") or {}).get("is_free")), None)
            if not free:
                continue
            fetch_episode_images(session, sid, free["product_id"])
            out["adult"] = True
            break
        except KakaoAdultRequired as e:
            out["adult"] = False
            out["detail"] = (out["detail"] + " / " if out["detail"] else "") + str(e)
            break
        except Exception:  # noqa: BLE001
            continue
    return out


def save_session_cookies(session):
    """refresh_token 등으로 바뀐 쿠키를 파일에 저장(다음 실행 때 재사용)."""
    try:
        data = [{"name": c.name, "value": c.value, "domain": c.domain or ".kakao.com"}
                for c in session.cookies if "kakao" in (c.domain or "kakao")]
        ss.write_json(COOKIE_STATE_PATH, {
            "source_hash": getattr(session, "kakao_cookie_source_hash", ""),
            "saved_at": time.time(), "cookies": data})
    except Exception:  # noqa: BLE001
        pass


def refresh_token(session, log=None):
    """로그인 토큰 연장. 실패해도 치명적이지 않으므로 bool만 반환."""
    if not getattr(session, "kakao_has_cookie", False):
        return False
    try:
        r = session.post(REFRESH_TOKEN_API, timeout=session.request_timeout)
        if r.status_code in (401, 403):
            raise KakaoAuthExpired("refresh_token 응답 %s - 쿠키가 만료된 것으로 보임" % r.status_code)
        ok = r.status_code < 300
        if ok:
            save_session_cookies(session)
        elif log:
            log("카카오페이지 토큰 연장 실패(HTTP %s) - 기존 쿠키로 계속" % r.status_code)
        return ok
    except requests.RequestException as e:
        if log:
            log("카카오페이지 토큰 연장 요청 실패(무시) - %s" % e)
        return False


def _api_error_text(resp):
    try:
        body = resp.json()
        key = body.get("message_key") or ""
        msg = body.get("message") or ""
        return key, "%s (%s, HTTP %s)" % (msg, key, resp.status_code)
    except ValueError:
        return "", "HTTP %s: %s" % (resp.status_code, resp.text[:200])


def _check_auth(resp, context):
    if resp.status_code in (401, 403):
        raise KakaoAuthExpired("%s: HTTP %s - 로그인 쿠키가 없거나 만료됨" % (context, resp.status_code))


# ---------------------------------------------------------------------------
# 작품 정보
# ---------------------------------------------------------------------------
def _meta(html, prop):
    m = re.search(r'<meta[^>]+(?:property|name)=["\']%s["\'][^>]+content=["\']([^"\']*)["\']' %
                  re.escape(prop), html)
    if not m:
        m = re.search(r'<meta[^>]+content=["\']([^"\']*)["\'][^>]+(?:property|name)=["\']%s["\']' %
                      re.escape(prop), html)
    return _unescape(m.group(1)).strip() if m else ""


def _unescape(s):
    import html as _html
    return _html.unescape(s or "")


THUMB_URL = "https://page-images.kakaoentcdn.com/download/resource?kid=%s&filename=th3"
COVER_URL = "https://page-images.kakaoentcdn.com/download/resource?kid=%s"


def _image_url(kid, fmt):
    kid = (kid or "").strip()
    if not kid:
        return ""
    if kid.startswith("http"):
        return kid
    if kid.startswith("//"):
        return "https:" + kid
    return fmt % kid


def series_info_from_item(si):
    """회차 목록/뷰어 API 응답의 series_item -> 작품 정보 dict"""
    if not isinstance(si, dict):
        return {}
    age = si.get("age_grade")
    on_issue = str(si.get("on_issue") or "").upper()
    info = {
        "title": (si.get("title") or "").strip(),
        "thumbnail": _image_url(si.get("thumbnail"), THUMB_URL),   # 카드/kavita.yaml용(작은 크기)
        "cover_url": _image_url(si.get("thumbnail"), COVER_URL),   # zip 첫 페이지용(원본)
        "synopsis": (si.get("description") or "").strip(),
        "author": ",".join(a.strip() for a in str(si.get("authors") or "").split(",") if a.strip()),
        "genre": si.get("sub_category") or "",
        "category": si.get("category") or "",
        "adult": str(age) in ("19", "Nineteen"),
        "finished": on_issue in ("N", "END"),
        "waitfree": bool(si.get("is_waitfree")),
        "release_date": str(si.get("start_sale_dt") or "")[:10].replace("-", ""),
    }
    return {k: v for k, v in info.items() if v not in ("", None)}


def fetch_series_info(session, series_id):
    """작품 정보(제목/표지/소개/작가/장르/완결 여부).

    작품 페이지 HTML은 SPA 껍데기라 og 메타가 사이트 공통값("카카오페이지",
    기본 로고)이고, requests가 문자셋을 잘못 추정하면 제목이 깨진다. 그래서
    회차 목록 API(1개만 요청)의 series_item에서 읽는다. 실패하면 빈 dict."""
    try:
        r = session.get(PRODUCT_LIST_API, params={
            "series_id": series_id, "cursor_index": 0, "cursor_direction": "NEXT",
            "window_size": 1, "sort_type": "asc"}, timeout=session.request_timeout)
        if r.status_code >= 300:
            return {}
        body = json.loads(r.content.decode("utf-8"))
    except (requests.RequestException, ValueError):
        return {}
    return series_info_from_item((body.get("result") or {}).get("series_item"))


# ---------------------------------------------------------------------------
# 요일 연재 목록 (page.kakao.com/menu/10010/screen/52 의 데이터)
# ---------------------------------------------------------------------------
LANDING_API = BFF + "/api/gateway/view/v2/landing/dayofweek"
WEBTOON_CATEGORY_UID = 10
TAB_NEW = 11
TAB_FINISHED = 12
# tab_uid 1~7 = 월~일 (네이버 쪽 weekdays 키와 맞춘다)
WEEKDAY_TABS = {1: "mon", 2: "tue", 3: "wed", 4: "thu", 5: "fri", 6: "sat", 7: "sun"}


def fetch_landing_page(session, tab_uid=None, bm=None, page=0):
    params = {"category_uid": WEBTOON_CATEGORY_UID, "page": page}
    if tab_uid is not None:
        params["tab_uid"] = tab_uid
    if bm:
        params["bm"] = bm
        params["subcategory_uid"] = 0
    r = session.get(LANDING_API, params=params, timeout=session.request_timeout)
    if r.status_code >= 300:
        raise RuntimeError("카카오 연재 목록 조회 실패: %s" % _api_error_text(r)[1])
    body = json.loads(r.content.decode("utf-8"))
    return body.get("result") or {}


def fetch_landing_all(session, tab_uid=None, bm=None, max_pages=200, should_cancel=None,
                      delay=0.2):
    """해당 탭의 전체 작품 목록(페이지를 끝까지). 반환: [landing item dict]"""
    out, seen = [], set()
    for page in range(max_pages):
        if should_cancel and should_cancel():
            break
        res = fetch_landing_page(session, tab_uid=tab_uid, bm=bm, page=page)
        items = res.get("list") or []
        for it in items:
            sid = it.get("series_id")
            if sid and sid not in seen:
                seen.add(sid)
                out.append(it)
        if not items or res.get("is_end"):
            break
        time.sleep(delay)
    return out


def landing_item_to_title(it):
    """연재 목록 항목 -> 카카오 작품 레코드(네이버 titles.json과 같은 필드 이름)."""
    ap = it.get("asset_property") or {}
    kid = ap.get("card_img") or (ap.get("card_set") or {}).get("background_img") or ""
    on_issue = str(it.get("on_issue") or "").upper()
    rec = {
        "title": (it.get("title") or "").strip(),
        "author": ",".join(a.strip() for a in str(it.get("authors") or "").split(",") if a.strip()),
        "genre": it.get("sub_category") or "",
        "tags": [it["sub_category"]] if it.get("sub_category") else [],
        "category": it.get("category") or "",
        "adult": str(it.get("age_grade")) == "19",
        "is_adult": str(it.get("age_grade")) == "19",
        "waitfree": bool(it.get("is_waitfree")),
        "status": "완결" if on_issue in ("N", "END") else "연재",
        "release_date": str(it.get("start_sale_dt") or "")[:10].replace("-", ""),
        # 새 회차가 올라왔는지 판단하는 값(바뀌었을 때만 회차 목록을 다시 조회)
        "last_slide_added_dt": str(it.get("last_slide_added_dt") or ""),
        "waitfree_period_min": int(it.get("waitfree_period_by_minute") or 0),
    }
    if kid:
        rec["thumbnail"] = _image_url(kid, THUMB_URL)
    out = {k: v for k, v in rec.items() if v not in ("", None)}
    out.update({"adult": rec["adult"], "is_adult": rec["is_adult"], "waitfree": rec["waitfree"]})
    return out


# ---------------------------------------------------------------------------
# 회차 목록
# ---------------------------------------------------------------------------
def _episode_from_item(entry):
    item = entry.get("item") if isinstance(entry, dict) and "item" in entry else entry
    if not isinstance(item, dict):
        return None
    sp = item.get("service_property") or {}
    pinfo = sp.get("purchase_info") or {}
    ptype = pinfo.get("purchase_type") if isinstance(pinfo, dict) else None
    rented = False
    if ptype in ("rent", "own", "possession", "purchase"):
        rented = True
        exp = pinfo.get("rent_expire_dt")
        if ptype == "rent" and exp:
            try:
                from datetime import datetime
                rented = datetime.fromisoformat(exp.replace("Z", "+00:00")).timestamp() > time.time()
            except (ValueError, TypeError):
                rented = True
    no = item.get("order_value")
    try:
        no = int(no)
    except (TypeError, ValueError):
        return None
    date = ""
    for k in ("start_sale_dt", "last_release_dt"):
        if item.get(k):
            date = str(item[k])[:10].replace("-", ".")
            break
    return {
        "no": no,
        "order": no,   # 사이트상 순번(트레일러/프롤로그 포함). 파일 번호는 assign_episode_numbers()가 정함
        "product_id": item.get("product_id"),
        "subtitle": item.get("title") or "",
        "rented": rented,
        "waitfree_ok": not item.get("waitfree_blocked", True),
        "free": bool(item.get("is_free") or item.get("free")),
        "date": date,
    }


def fetch_episode_list(session, series_id, max_pages=400, log=None, series_title=None,
                       newer_than_order=None, max_regular_no=None):
    """오래된 순(1화부터) 회차 목록.

    newer_than_order를 주면 최신 순(desc)으로 받다가 그 순번 이하가 나오는 페이지에서
    멈추고, 그보다 새 회차만 돌려준다(새 회차 확인용 - 보통 요청 1번). 이때 번호는
    max_regular_no(이전에 매긴 본편 최대 번호)를 기준으로 이어서 매긴다."""
    incremental = newer_than_order is not None
    out, cursor = [], 0
    for _ in range(max_pages):
        r = session.get(PRODUCT_LIST_API, params={
            "series_id": series_id, "cursor_index": cursor, "cursor_direction": "NEXT",
            "window_size": PAGE_SIZE, "sort_type": "desc" if incremental else "asc"},
            timeout=session.request_timeout)
        _check_auth(r, "회차 목록")
        if r.status_code >= 300:
            raise RuntimeError("회차 목록 조회 실패: %s" % _api_error_text(r)[1])
        body = json.loads(r.content.decode("utf-8"))
        result = body.get("result") or {}
        items = result.get("list") or []
        total = int(result.get("total_count") or 0)
        page_orders = []
        for it in items:
            ep = _episode_from_item(it)
            if ep:
                page_orders.append(ep["order"])
                if not incremental or ep["order"] > newer_than_order:
                    out.append(ep)
        cursor += len(items)
        if not items or cursor >= total:
            break
        if incremental and page_orders and min(page_orders) <= newer_than_order:
            break
        time.sleep(0.3)
    out.sort(key=lambda e: e["order"])
    assign_episode_numbers(out, series_title=series_title,
                           max_regular_no=max_regular_no if incremental else None)
    return out


# 회차 제목의 "N화". 예: "나 혼자만 레벨업 12화", "12화", "밥만 먹고 레벨업 208화 (외전 8화)" -> 208
# (예전엔 마지막 "N화"를 써서 "(외전 8화)"의 8이 잡히는 문제가 있었음 - 첫 번째를 쓴다)
_EP_TITLE_NO_RE = re.compile(r"(\d+)\s*화")
# 예전(1.27.0 이하) 규칙: 마지막 "N화" - 기존 파일 이름을 새 번호로 옮길 때만 사용
_EP_TITLE_NO_RE_V1 = re.compile(r"(\d+)\s*화(?!.*\d+\s*화)")
# 제목에 "N화"가 없는 특수 회차(트레일러/프롤로그/외전/후기 등)의 파일 번호 시작값.
# 본편 번호와 절대 겹치지 않고 목록 맨 뒤로 정렬되게 큰 값을 쓴다.
SPECIAL_EP_BASE = 9000


def episode_no_from_subtitle(subtitle, series_title=None):
    text = str(subtitle or "").strip()
    st = str(series_title or "").strip()
    if st and text.startswith(st):
        text = text[len(st):]      # 작품 제목에 숫자+화가 들어 있어도 오인하지 않게
    m = _EP_TITLE_NO_RE.search(text)
    return int(m.group(1)) if m else None


def assign_episode_numbers(episodes, series_title=None, max_regular_no=None):
    """회차 제목의 "N화" 번호를 파일 번호(no)로 쓴다.
    - 제목에 번호가 없는 회차(트레일러/프롤로그/외전 등): 9000 + 사이트 순번
    - 같은 번호가 이미 나온 경우(시즌2에서 1화부터 다시 시작 등): 9000 + 사이트 순번
    - 새 회차만 받은 경우(max_regular_no 지정): 그 번호 이하는 이미 쓰인 번호로 본다
    순번(order)은 그대로 보존해서 다른 계산(기다무 등)에 쓸 수 있게 둔다."""
    used = set()
    floor = int(max_regular_no or 0)
    for ep in episodes:
        n = episode_no_from_subtitle(ep.get("subtitle"), series_title)
        if n is None or n in used or n >= SPECIAL_EP_BASE or (max_regular_no is not None and n <= floor):
            n = SPECIAL_EP_BASE + int(ep.get("order") or 0)
        used.add(n)
        ep["no"] = n
    return episodes


def legacy_numbers_v1(episodes):
    """1.27.0 이하 번호 규칙(마지막 "N화")으로 매겼던 번호 {order: no}."""
    used, out = set(), {}
    for ep in episodes:
        m = _EP_TITLE_NO_RE_V1.search(str(ep.get("subtitle") or ""))
        n = int(m.group(1)) if m else None
        if n is None or n in used or n >= SPECIAL_EP_BASE:
            n = SPECIAL_EP_BASE + int(ep.get("order") or 0)
        used.add(n)
        out[ep.get("order")] = n
    return out


# ---------------------------------------------------------------------------
# 이미지 / 기다무
# ---------------------------------------------------------------------------
def fetch_episode_images(session, series_id, product_id):
    """회차 이미지 URL 목록(페이지 순). 볼 수 없는 회차면 KakaoNotPurchased."""
    r = session.get(VIEWER_DATA_API, params={"series_id": series_id, "product_id": product_id},
                    timeout=session.request_timeout)
    _check_auth(r, "회차 이미지")
    if r.status_code >= 300:
        key, text = _api_error_text(r)
        if "not_purchased" in key or "purchase" in key or r.status_code == 402:
            raise KakaoNotPurchased(text)
        raise RuntimeError("이미지 목록 조회 실패: %s" % text)
    body = json.loads(r.content.decode("utf-8"))
    rc = body.get("result_code")
    if rc not in (None, 0, "0"):
        key = str(body.get("message_key") or "")
        text = "%s (%s)" % (body.get("message") or "", key)
        if "purchase" in key:
            raise KakaoNotPurchased(text)
        if "adult" in key or "age" in key or "verif" in key or "login" in key:
            # 성인 인증 필요 - 이 작품의 모든 회차가 같은 이유로 막히므로 호출 측이
            # 작품 단위로 중단한다(회차마다 끝까지 확인하지 않게)
            raise KakaoAdultRequired(text)
        raise KakaoSkip(text)
    vd = body.get("viewer_data") or body.get("viewerData") or {}
    if isinstance(vd, dict) and (vd.get("contents_list") or
                                 str(vd.get("type") or "").lower().startswith("text")):
        raise KakaoUnsupported("웹소설(텍스트) 회차라 이미지로 받을 수 없음")
    idd = vd.get("imageDownloadData") or vd.get("image_download_data") or {}
    files = idd.get("files") or []
    files = sorted((f for f in files if isinstance(f, dict)), key=lambda f: f.get("no") or 0)
    urls = [f.get("secureUrl") or f.get("secure_url") for f in files]
    urls = [u for u in urls if u]
    if not urls:
        raise KakaoNotPurchased("이미지 목록이 비어 있음(구매/대여가 필요한 회차로 보임)")
    return urls


# ---------------------------------------------------------------------------
# 보유 대여권
# ---------------------------------------------------------------------------
TICKET_MY_API = BFF + "/api/gateway/api/v1/ticket/my"
TICKET_READY_API = BFF + "/api/gateway/api/v1/ticket/ready_to_use"
# 카카오페이지 웹 코드의 이용권 종류(대여권 RT**). 무료로 받은 대여권만 기본 사용.
FREE_RENT_TICKETS = ("RT00", "RT03", "RT04", "RT06", "RT07", "RT09", "RT10")
#   RT00 신규 고객 선물 / RT03 이벤트 / RT04 쿠폰 / RT06 오늘의 선물 / RT07 선물 /
#   RT09 캠페인 / RT10 기다무+
PAID_RENT_TICKETS = ("RT01", "RT02", "RT11", "RT12", "RT13")
#   RT01 낱개 대여 / RT02 패키지 / RT11 캐시 낱개 / RT12 캐시 할인 / RT13 세트
TICKET_NAMES = {
    "RT00": "신규 선물 대여권", "RT01": "대여권(구매)", "RT02": "패키지 대여권(구매)",
    "RT03": "이벤트 대여권", "RT04": "쿠폰 대여권", "RT05": "기다무 대여권",
    "RT06": "오늘의 선물 대여권", "RT07": "선물 대여권", "RT09": "캠페인 대여권",
    "RT10": "기다무+ 대여권", "RT11": "캐시 대여권", "RT12": "캐시 할인 대여권", "RT13": "세트 대여권",
}
_TICKET_TYPE_RE = re.compile(r"^(RT|TT)\d\d$")
_COUNT_KEYS = ("count", "ticket_count", "remain_count", "remain", "quantity", "own_count",
               "available_count", "total_count", "cnt")


def parse_ticket_counts(obj):
    """이용권 응답(형식 미공개)을 훑어 {이용권종류: 개수}로 모은다. 종류 코드(RT04 등)가
    들어 있는 dict를 찾고, 같은 dict의 개수 필드를 읽는다(없으면 1장으로 본다)."""
    counts = {}

    def walk(o):
        if isinstance(o, dict):
            ttype = None
            for v in o.values():
                if isinstance(v, str) and _TICKET_TYPE_RE.match(v):
                    ttype = v
                    break
            if ttype:
                n = None
                for k in _COUNT_KEYS:
                    if isinstance(o.get(k), (int, float)) and not isinstance(o.get(k), bool):
                        n = int(o[k])
                        break
                counts[ttype] = counts.get(ttype, 0) + (1 if n is None else n)
            for v in o.values():
                if isinstance(v, (dict, list)):
                    walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    walk(obj)
    return {k: v for k, v in counts.items() if v > 0}


def _get_json(session, url, params):
    r = session.get(url, params=params, timeout=session.request_timeout)
    _check_auth(r, "이용권 조회")
    return json.loads(r.content.decode("utf-8"))


def fetch_my_tickets(session, series_id):
    """작품별 보유 이용권 {종류: 개수} 와 원본 응답(로그 확인용)."""
    body = _get_json(session, TICKET_MY_API, {"series_id": series_id, "include_single": "true",
                                              "include_waitfree": "true"})
    return parse_ticket_counts(body.get("result") if isinstance(body, dict) else body), body


def ready_ticket_types(session, product_id):
    """이 회차에 바로 쓸 수 있는 이용권 종류들(응답에 나온 순서)."""
    body = _get_json(session, TICKET_READY_API, {"product_id": product_id})
    return list(parse_ticket_counts(body.get("result") if isinstance(body, dict) else body).keys()), body


def use_ticket(session, product_id, ticket_type):
    """이용권 사용. 성공 True."""
    r = session.post(TICKET_USE_API, data={"product_id": product_id, "ticket_type": ticket_type},
                     timeout=session.request_timeout)
    _check_auth(r, "이용권 사용")
    if r.status_code >= 300:
        return False
    try:
        body = json.loads(r.content.decode("utf-8"))
        return body.get("result_code") in (None, 0, "0")
    except ValueError:
        return True


def use_waitfree_ticket(session, product_id):
    """기다무 대여권 사용. 성공 True, 대여 불가(대기 시간 미충족 등) False."""
    r = session.post(TICKET_USE_API, data={"product_id": product_id,
                                           "ticket_type": RENTAL_TICKET_TYPE},
                     timeout=session.request_timeout)
    _check_auth(r, "기다무 사용")
    return r.status_code < 300
