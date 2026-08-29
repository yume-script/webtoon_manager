# -*- coding: utf-8 -*-
"""
네이버웹툰 목록/회차/이미지 접근 래퍼.

주의: 네이버는 이미지 목록을 공식 공개 JSON API로 제공하지 않으므로,
회차 상세 페이지(HTML)에서 이미지 <img class="wt_viewer"> 태그를 정규식으로
추출한다. 페이지/응답 구조가 바뀌면 이 파서만 고치면 된다.
"""
import json
import re
import time

import requests

BASE = "https://comic.naver.com"
API_BASE = BASE + "/api/webtoon/titlelist"
ARTICLE_LIST_API = BASE + "/api/article/list"
DETAIL_URL = BASE + "/webtoon/detail"
MOBILE_DETAIL_URL = "https://m.comic.naver.com/webtoon/detail"

WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
# "매일+" 탭. 사용자가 확인해준 실제 네이버 페이지 URL(comic.naver.com/webtoon?tab=dailyPlus)의
# 쿼리값과 동일한 값으로 API도 호출되길 기대하고 추가함(다른 요일 파라미터와 동일 패턴).
# 목록이 비어서 오면 이 값 자체가 API에서는 다른 이름일 수 있다는 뜻이니 확인 필요.
DAILY_PLUS = "dailyPlus"
WEEKDAYS_WITH_DAILY_PLUS = WEEKDAYS + [DAILY_PLUS]

_IMG_RE = re.compile(r'<img[^>]+class="[^"]*wt_viewer[^"]*"[^>]+src="([^"]+)"', re.I)
# 실사용 예시(2026-08 확인)로 보니, 오래된/완결작 템플릿은 wt_viewer 클래스가
# <img> 자체가 아니라 이를 감싸는 <div class="wt_viewer">에만 붙고, 정작
# <img>에는 class가 전혀 없다(id="content_image_N"만 있음). 그래서 클래스
# 유무에 기대지 않고, 페이지 전체의 <img src="..."> 를 다 뽑은 뒤 실제
# 만화컷 CDN 경로(image-comic.pstatic.net)인지로 판별하는 방식을 기본으로
# 쓴다. 연령고지 배너 이미지(.../static/agerate/...)는 명시적으로 제외한다.
_ANY_IMG_SRC_RE = re.compile(r'<img\b[^>]*\bsrc="([^"]+)"', re.I)
_AGE_BANNER_RE = re.compile(r'/static/agerate/', re.I)
_COMIC_CDN_RE = re.compile(r'//image-comic\.pstatic\.net/', re.I)
_TITLE_RE = re.compile(r'"titleName"\s*:\s*"([^"]*)"')
# 유료(코인 결제) 회차는 실제 만화 이미지 대신 "미리보기/구매" 안내가 뜨는데,
# 정확한 마크업은 자주 바뀔 수 있어 페이지 전역에서 이 키워드들 중 하나라도
# 보이면 유료 회차로 의심한다(이미지가 0장일 때만 확인하는 보조 판단이라
# 오탐이어도 에러 메시지만 더 정확해질 뿐 동작이 나빠지지는 않음). 흔한 UI
# 문구와 겹칠 수 있는 애매한 단어(미리보기, 쿠키 등)는 오탐 위험이 커서 뺐다.
_PAID_MARKERS = ("코인 사용", "코인으로 보기", "구매하기", "소장하기", "결제하기", "이용권 사용")


class NaverAuthExpired(Exception):
    """쿠키(성인/로그인)가 만료되어 인증이 필요한 콘텐츠 접근이 막힌 경우"""


class NaverPaidEpisode(Exception):
    """유료(코인 결제) 회차라 미리보기만 제공되고 실제 이미지는 못 가져오는 경우.
    구조 변경/일시 차단과는 원인이 달라 재시도해도 해결되지 않으므로 별도
    예외로 구분해, 호출 측이 이 회차를 건너뛰고 계속 진행할 수 있게 한다."""


def build_session(cookie_storage_state_json=None, naver_id=None, naver_pw=None,
                   timeout=10):
    """
    cookie_storage_state_json: Playwright storage_state 형식(JSON 문자열 또는 dict)
      {"cookies": [{"name": "NID_AUT", "domain": ".naver.com", "value": "..."}, ...]}
    naver_id/naver_pw는 현재는 세션에 직접 로그인시키지 않고(캡차/보안문자 이슈로
    자동 로그인은 불안정), 쿠키가 없을 때 참고용 메타데이터로만 보관한다.
    실제 로그인은 사용자가 브라우저 확장(Cookie-Editor 등)으로 내보낸 쿠키를
    설정에 붙여넣는 방식을 권장한다(원본 webtoon-manager와 동일 방식).
    """
    sess = requests.Session()
    sess.headers.update({
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/126.0.0.0 Safari/537.36"),
        "Referer": BASE + "/",
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


def _get(session, url, params=None, referer=None):
    headers = {}
    if referer:
        headers["Referer"] = referer
    resp = session.get(url, params=params, headers=headers,
                        timeout=getattr(session, "request_timeout", 10))
    resp.raise_for_status()
    return resp


def fetch_weekday_titles(session):
    """요일별 연재중 웹툰 전체 목록 + 매일+(dailyPlus). {titleId: {...}} 형태로 병합해서 반환."""
    out = {}
    for wd in WEEKDAYS_WITH_DAILY_PLUS:
        try:
            resp = _get(session, API_BASE + "/weekday", params={"week": wd})
            body = resp.json()
        except (requests.RequestException, ValueError):
            continue
        for item in _extract_title_list(body):
            item["status"] = item.get("status") or "연재"
            item.setdefault("weekdays", [])
            if wd not in item["weekdays"]:
                item["weekdays"].append(wd)
            out[str(item["titleId"])] = _merge_title(out.get(str(item["titleId"])), item)
        time.sleep(0.2)
    return out


def fetch_finished_titles(session, max_pages=200):
    """완결 웹툰 목록(페이지 순회)."""
    out = {}
    page = 1
    while page <= max_pages:
        try:
            resp = _get(session, API_BASE + "/finished", params={"page": page})
            body = resp.json()
        except (requests.RequestException, ValueError):
            break
        items = _extract_title_list(body)
        if not items:
            break
        for item in items:
            item["status"] = "완결"
            out[str(item["titleId"])] = _merge_title(out.get(str(item["titleId"])), item)
        page += 1
        time.sleep(0.2)
    return out


def fetch_genre_titles(session, genre, max_pages=50):
    out = {}
    page = 1
    while page <= max_pages:
        try:
            resp = _get(session, API_BASE + "/genre", params={"genre": genre, "page": page})
            body = resp.json()
        except (requests.RequestException, ValueError):
            break
        items = _extract_title_list(body)
        if not items:
            break
        for item in items:
            tags = set(item.get("tags", []))
            tags.add(genre)
            item["tags"] = sorted(tags)
            out[str(item["titleId"])] = _merge_title(out.get(str(item["titleId"])), item)
        page += 1
        time.sleep(0.2)
    return out


def _extract_title_list(body):
    """응답 스키마가 body 자체가 배열이거나, titleList / result.titleList 등으로
    감싸진 형태이거나 버전에 따라 다를 수 있어 가능한 경로를 모두 시도한다."""
    candidates = []
    if isinstance(body, list):
        # 실사용 응답 예시(2026-08 확인, "신작 업뎃" 목록)로 보니 감싸는 객체 없이
        # 배열 자체로 오는 응답 형태도 있다.
        candidates = body
    elif isinstance(body, dict):
        for path in (("titleList",), ("result", "titleList"), ("titleList", "titleList")):
            cur = body
            ok = True
            for p in path:
                if isinstance(cur, dict) and p in cur:
                    cur = cur[p]
                else:
                    ok = False
                    break
            if ok and isinstance(cur, list):
                candidates = cur
                break
    out = []
    for it in candidates:
        if not isinstance(it, dict):
            continue
        title_id = it.get("titleId") or it.get("id")
        if not title_id:
            continue
        rating = None
        # 응답 스키마마다 필드명이 다를 수 있어 흔히 쓰이는 후보를 순서대로 시도
        for key in ("starScore", "star", "starAverage", "score", "rating"):
            v = it.get(key)
            if v is not None:
                try:
                    rating = round(float(v), 2)
                except (TypeError, ValueError):
                    rating = None
                break
        out.append({
            "titleId": title_id,
            "title": it.get("titleName") or it.get("title") or "",
            "author": ", ".join(it.get("author", [])) if isinstance(it.get("author"), list)
                      else (it.get("author") or ""),
            "thumbnail": it.get("thumbnailUrl") or it.get("thumbnail") or "",
            "is_adult": bool(it.get("adult") or it.get("isAdult")),
            "rest": bool(it.get("rest")),
            "new": bool(it.get("new")),
            "rating": rating,
            "tags": it.get("tags", []) or [],
        })
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
    """최신 -> 과거 순으로 반환되는 회차 목록. [{no, subtitle, thumbnail, charge}]"""
    episodes = []
    page = 1
    while page <= max_pages:
        try:
            resp = _get(session, ARTICLE_LIST_API,
                        params={"titleId": title_id, "page": page},
                        referer="%s?titleId=%s" % (DETAIL_URL, title_id))
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
        # articleList가 더 없으면 종료 (isLastPage 필드가 있으면 우선 사용)
        is_last = body.get("result", {}).get("isLastPage") if isinstance(body.get("result"), dict) else None
        if is_last is True or len(items) == 0:
            break
        page += 1
        time.sleep(0.15)
    return episodes


def _extract_comic_images(html):
    """페이지 안의 <img src="...">를 전부 뽑은 뒤, 실제 만화컷 CDN 경로
    (image-comic.pstatic.net)인 것만 남기고 연령고지 배너(/static/agerate/)는
    제외한다. class="wt_viewer"가 <img> 자체에 붙는 템플릿/<div>에만 붙는
    템플릿 둘 다 이 방식이면 구분 없이 처리된다."""
    candidates = _ANY_IMG_SRC_RE.findall(html)
    return [u for u in candidates if _COMIC_CDN_RE.search(u) and not _AGE_BANNER_RE.search(u)]


def fetch_episode_images(session, title_id, episode_no):
    """회차 상세 페이지를 긁어서 이미지 URL 목록을 반환. 성인/미성년 인증이 필요한
    작품인데 쿠키가 없거나 만료되었으면 NaverAuthExpired를 던진다."""
    url = "%s?titleId=%s&no=%s" % (DETAIL_URL, title_id, episode_no)
    resp = _get(session, url, referer=BASE + "/")
    html = resp.text

    if "성인인증" in html or "adult_ok" in html or "만 19세" in html:
        # 이미지가 하나도 안 잡히면 인증 필요로 간주
        if not _extract_comic_images(html):
            raise NaverAuthExpired(
                "titleId=%s no=%s: 성인 인증이 필요하거나 쿠키가 만료된 것으로 보임" %
                (title_id, episode_no))

    imgs = _extract_comic_images(html)
    if not imgs:
        if any(marker in html for marker in _PAID_MARKERS):
            raise NaverPaidEpisode(
                "titleId=%s no=%s: 유료(코인 결제) 회차로 보여 이미지를 못 가져옴" %
                (title_id, episode_no))
        raise ValueError("titleId=%s no=%s: 이미지 목록을 찾지 못함(페이지 구조 변경 가능성)" %
                          (title_id, episode_no))
    return imgs


def guess_title_meta(session, title_id):
    """저장된 목록에 없는 titleId를 수동 검색할 때 상세 페이지에서 제목만 대략 파싱."""
    url = "%s?titleId=%s" % (DETAIL_URL, title_id)
    resp = _get(session, url, referer=BASE + "/")
    m = _TITLE_RE.search(resp.text)
    return {"titleId": str(title_id), "title": m.group(1) if m else ("titleId %s" % title_id)}
