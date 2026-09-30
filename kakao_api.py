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
    for name, value, dom in cookies:
        s.cookies.set(name, value, domain=dom if dom.startswith(".") else "." + dom.lstrip("."),
                      path="/")
    s.kakao_cookie_source_hash = _cookie_hash(cookie_raw)
    s.kakao_has_cookie = bool(cookies)
    return s


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


def fetch_series_info(session, series_id):
    """작품 페이지 HTML에서 제목/표지/소개/작가를 읽는다. 실패하면 빈 dict."""
    try:
        r = session.get("%s/content/%s" % (BASE, series_id),
                        headers={"Accept": "text/html"}, timeout=session.request_timeout)
    except requests.RequestException:
        return {}
    if r.status_code >= 400:
        return {}
    html = r.text
    title = _meta(html, "og:title")
    title = re.sub(r"\s*[-|]\s*카카오페이지\s*$", "", title).strip()
    info = {
        "title": title,
        "thumbnail": _meta(html, "og:image"),
        "synopsis": _meta(html, "og:description") or _meta(html, "description"),
    }
    # Next.js 데이터 안의 작가/장르/완결 여부 (없으면 무시)
    m = re.search(r'"authors"\s*:\s*"([^"]+)"', html)
    if m:
        try:
            info["author"] = json.loads('"%s"' % m.group(1))
        except ValueError:
            info["author"] = m.group(1)
    m = re.search(r'"subcategory"\s*:\s*"([^"]+)"', html)
    if m:
        info["genre"] = m.group(1)
    if re.search(r'"onIssue"\s*:\s*"End"|"on_issue"\s*:\s*"End"', html):
        info["finished"] = True
    if re.search(r'"ageGrade"\s*:\s*"Nineteen"|"age_grade"\s*:\s*19', html):
        info["adult"] = True
    return {k: v for k, v in info.items() if v}


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
    for k in ("open_dt", "sale_open_dt", "service_start_dt", "free_change_dt"):
        if item.get(k):
            date = str(item[k])[:10].replace("-", ".")
            break
    return {
        "no": no,
        "product_id": item.get("product_id"),
        "subtitle": item.get("title") or "",
        "rented": rented,
        "waitfree_ok": not item.get("waitfree_blocked", True),
        "free": bool(item.get("is_free") or item.get("free")),
        "date": date,
    }


def fetch_episode_list(session, series_id, max_pages=400, log=None):
    """오래된 순(1화부터) 전체 회차 목록."""
    out, cursor = [], 0
    for _ in range(max_pages):
        r = session.get(PRODUCT_LIST_API, params={
            "series_id": series_id, "cursor_index": cursor, "cursor_direction": "NEXT",
            "window_size": PAGE_SIZE, "sort_type": "asc"}, timeout=session.request_timeout)
        _check_auth(r, "회차 목록")
        if r.status_code >= 300:
            raise RuntimeError("회차 목록 조회 실패: %s" % _api_error_text(r)[1])
        body = r.json()
        result = body.get("result") or {}
        items = result.get("list") or []
        total = int(result.get("total_count") or 0)
        for it in items:
            ep = _episode_from_item(it)
            if ep:
                out.append(ep)
        cursor += len(items)
        if not items or cursor >= total:
            break
        time.sleep(0.3)
    out.sort(key=lambda e: e["no"])
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
    body = r.json()
    vd = body.get("viewer_data") or body.get("viewerData") or {}
    idd = vd.get("imageDownloadData") or vd.get("image_download_data") or {}
    files = idd.get("files") or []
    files = sorted((f for f in files if isinstance(f, dict)), key=lambda f: f.get("no") or 0)
    urls = [f.get("secureUrl") or f.get("secure_url") for f in files]
    urls = [u for u in urls if u]
    if not urls:
        raise KakaoNotPurchased("이미지 목록이 비어 있음(구매/대여가 필요한 회차로 보임)")
    return urls


def use_waitfree_ticket(session, product_id):
    """기다무 대여권 사용. 성공 True, 대여 불가(대기 시간 미충족 등) False."""
    r = session.post(TICKET_USE_API, data={"product_id": product_id,
                                           "ticket_type": RENTAL_TICKET_TYPE},
                     timeout=session.request_timeout)
    _check_auth(r, "기다무 사용")
    return r.status_code < 300
