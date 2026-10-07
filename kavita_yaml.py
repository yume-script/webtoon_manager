# -*- coding: utf-8 -*-
"""
kavita.yaml 생성기
------------------
시리즈 폴더(회차 zip들과 같은 레벨)에 kavita.yaml을 만든다. 형식은 사용자가
준 샘플(action / files / meta / search 4개 블록)을 그대로 따른다.

    action:
        all_file_is_special: false
        code: BNW<titleId>
        first_cover: false
    files:
        "<회차 파일명>":
            cover: <첫 파일만 시리즈 썸네일 base64, 나머지는 FIRST>
            page: <파일명의 #장수>
            wordcount: 0
    meta:   (Kavita 시리즈 메타데이터 - Age Rating/Publication Status 등은 Kavita enum 숫자)
    search: (검색 결과 캐시 형식 - code/title/author/poster_url 등)

동작 원칙
- 새 회차가 압축되어 폴더에 추가되었거나, 작품 정보(줄거리/완결여부 등)가
  바뀌었을 때만 실제로 파일을 쓴다. 매번 내용을 새로 만들어 기존 파일과
  비교하고 같으면 건드리지 않는다(rclone 마운트에서 불필요한 업로드/변경
  감지를 만들지 않기 위함).
- PyYAML 의존성 없이 직접 직렬화한다(BookOasis 컨테이너에 yaml 모듈이 없어도
  동작). 문자열은 전부 큰따옴표 스칼라(JSON 문자열 = YAML 호환)로 쓴다.
- 부가 기능이라 어떤 오류가 나도 예외를 올리지 않고 로그만 남긴다.
"""
import base64
import hashlib
import json
import os
import re
import time
import zipfile

from . import downloader, naver_api, state_store as ss

YAML_NAME = "kavita.yaml"
PUBLISHER = "네이버 웹툰"

# 플랫폼별 코드 접두어/링크/출판사. 카카오페이지 작품은 kakao_titles.json에서 읽는다.
PLATFORMS = {
    "naver": {"code": "BNW", "publisher": "네이버 웹툰",
              "link": "https://comic.naver.com/webtoon/list?titleId=%s"},
    "kakao": {"code": "KKP", "publisher": "카카오페이지",
              "link": "https://page.kakao.com/content/%s"},
    "kakao_novel": {"code": "KKN", "publisher": "카카오페이지",
                    "link": "https://page.kakao.com/content/%s"},
}
# 작품 상세정보(줄거리 등)는 자주 바뀌지 않으므로 하루 한 번만 다시 조회한다.
INFO_REFRESH_SECONDS = 24 * 3600

# Kavita AgeRating enum (API/Entities/Enums/AgeRating.cs)
#   Everyone=3, Teen=8, Mature15Plus=9, AdultsOnly=13
_AGE_MAP = {
    "RATE_ALL": "3", "ALL": "3",
    "RATE_12": "8", "12": "8",
    "RATE_15": "9", "15": "9",
    "RATE_18": "13", "18": "13",
}
# Kavita PublicationStatus enum: OnGoing=0, Hiatus=1, Completed=2
_STATUS_ONGOING, _STATUS_HIATUS, _STATUS_COMPLETED = "0", "1", "2"

# 네이버웹툰 장르(태그 중 이 목록에 있는 것만 Genres로 올린다. 나머지는 Tags로만)
_NAVER_GENRES = ("일상", "개그", "판타지", "액션", "드라마", "로맨스", "순정", "감성",
                 "스릴러", "무협", "사극", "시대극", "스포츠", "로맨스판타지", "호러", "공포",
                 "무협/사극", "순정/로맨스", "시대극/무협")

_ARCHIVE_EXTS = (".zip", ".cbz", ".epub")
_EP_NO_RE = re.compile(r"(\d+)화")
_LEADING_NO_RE = re.compile(r"^(\d+)")
_COUNT_RE = re.compile(r"#(\d+)\.(zip|cbz)$", re.I)
_IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif")
# 시리즈 폴더 이름: "제목 (titleId)" (downloader.title_dir 규칙)
SERIES_DIR_RE = re.compile(r"^(.*) \((\d+)\)$")
# titles.json(구독/목록)에 없는 작품의 상세정보 캐시. titles.json에 넣으면
# 카테고리탭 목록에 원치 않는 작품이 섞여 보이므로 별도 파일에 둔다.
INFO_CACHE_PATH = os.path.join(ss.DATA_DIR, "kavita_info_cache.json")
_EP_DATE_RE = re.compile(r"^(\d{2}|\d{4})[.\-/](\d{1,2})[.\-/](\d{1,2})")
_PLAIN_KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_ ]*$")


# ---------------------------------------------------------------------------
# 작은 YAML 직렬화기 (dict / list / str / int / bool / None)
# ---------------------------------------------------------------------------
def _scalar(v):
    if v is None:
        return "''"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    return json.dumps(str(v), ensure_ascii=False)


def _key(k):
    k = str(k)
    return k if _PLAIN_KEY_RE.match(k) else json.dumps(k, ensure_ascii=False)


def _dump(obj, indent=0):
    pad = " " * indent
    lines = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, dict):
                lines.append("%s%s:" % (pad, _key(k)))
                lines.extend(_dump(v, indent + 4))
            elif isinstance(v, list):
                lines.append("%s%s:" % (pad, _key(k)))
                lines.extend(_dump(v, indent))
            else:
                lines.append("%s%s: %s" % (pad, _key(k), _scalar(v)))
    elif isinstance(obj, list):
        for item in obj:
            if isinstance(item, dict) and item:
                sub = _dump(item, indent + 4)
                # 첫 줄만 "-   " 로 시작(샘플 들여쓰기와 동일)
                sub[0] = "%s-   %s" % (pad, sub[0].lstrip())
                lines.extend(sub)
            else:
                lines.append("%s-   %s" % (pad, _scalar(item)))
    return lines


def dump_yaml(data):
    return "\n".join(_dump(data)) + "\n"


# ---------------------------------------------------------------------------
# 데이터 조립
# ---------------------------------------------------------------------------
def parse_episode_date(text):
    """'19.08.19' / '2019.08.19' -> '20190819'. 모르는 형식이면 ''."""
    m = _EP_DATE_RE.match(str(text or "").strip())
    if not m:
        return ""
    y, mo, d = m.group(1), int(m.group(2)), int(m.group(3))
    if len(y) == 2:
        y = "20" + y
    if not (1 <= mo <= 12 and 1 <= d <= 31):
        return ""
    return "%s%02d%02d" % (y, mo, d)


def release_date_from_episodes(episodes):
    """회차 목록에서 가장 작은 회차 번호(보통 1화)의 공개일을 YYYYMMDD로."""
    eps = [e for e in (episodes or []) if isinstance(e.get("no"), int) and e.get("date")]
    if not eps:
        return ""
    first = min(eps, key=lambda e: e["no"])
    return parse_episode_date(first.get("date"))


def _zip_image_count(path):
    try:
        with zipfile.ZipFile(path) as zf:
            return sum(1 for n in zf.namelist()
                       if n.lower().endswith(_IMAGE_EXTS) and not os.path.basename(n).startswith("0000_cover"))
    except Exception:  # noqa: BLE001
        return 0


def _epub_word_count_raw(path):
    try:
        with zipfile.ZipFile(path) as zf:
            words = 0
            for n in zf.namelist():
                if n.lower().endswith((".xhtml", ".html")) and not n.endswith("nav.xhtml"):
                    text = re.sub(r"<[^>]+>", " ", zf.read(n).decode("utf-8", "replace"))
                    words += len(text.split())
            return words
    except Exception:  # noqa: BLE001
        return 0


# EPUB 단어 수 캐시 {절대경로: [크기, mtime_ns, 단어수]}. 예전에는 kavita.yaml을
# 갱신할 때마다 그 작품의 EPUB을 전부 열어 본문을 파싱해서(회차 수백 개 x 작품 수)
# 웹소설 처리 중 CPU를 크게 잡아먹었다. 파일이 그대로면 다시 열지 않는다.
EPUB_WORDS_PATH = os.path.join(ss.DATA_DIR, "epub_words.json")
_epub_words = None
_epub_words_dirty = False


def _epub_cache():
    global _epub_words
    if _epub_words is None:
        _epub_words = ss.read_json(EPUB_WORDS_PATH, {})
    return _epub_words


def flush_epub_cache():
    global _epub_words_dirty
    with ss._lock:
        if _epub_words_dirty and _epub_words is not None:
            ss.write_json(EPUB_WORDS_PATH, _epub_words)
            _epub_words_dirty = False


def remember_epub_words(path, words=None):
    """방금 만든 EPUB의 단어 수를 캐시에 넣어 둔다(나중에 다시 열지 않게).
    값은 항상 파일에서 직접 센 값으로 통일한다 - API 단어 수와 세는 방식이 달라서,
    캐시가 다시 만들어질 때 wordcount가 바뀌어 yaml이 다시 써지는 일이 없게."""
    global _epub_words_dirty
    words = _epub_word_count_raw(path)
    try:
        st = os.stat(path)
    except OSError:
        return
    with ss._lock:
        _epub_cache()[os.path.abspath(path)] = [st.st_size, st.st_mtime_ns, int(words or 0)]
        _epub_words_dirty = True


def _epub_word_count(path):
    """EPUB 본문 단어 수(Kavita의 wordcount용). 캐시에 있으면 파일을 열지 않는다.
    처음 여는 예전 EPUB은 이때 HTML 코드 노출 문제(이중 이스케이프)도 고친다."""
    global _epub_words_dirty
    key = os.path.abspath(path)
    try:
        st = os.stat(path)
    except OSError:
        return 0
    with ss._lock:
        rec = _epub_cache().get(key)
    if rec and rec[0] == st.st_size and rec[1] == st.st_mtime_ns:
        return rec[2]
    try:
        from . import novel_epub
        if novel_epub.repair_epub(path):
            st = os.stat(path)
    except Exception:  # noqa: BLE001
        pass
    words = _epub_word_count_raw(path)
    with ss._lock:
        _epub_cache()[key] = [st.st_size, st.st_mtime_ns, words]
        _epub_words_dirty = True
    return words


def _list_archives(series_dir):
    """시리즈 폴더의 회차 압축파일 [(파일명, 회차번호, 장수)] 을 회차순으로.

    지금 규칙("제목 0001화#79.zip")뿐 아니라 예전 형식("0089.cbz",
    "1화#246.zip", "제목 0022화 110.zip" 등)과 사용자가 직접 넣은 zip/cbz도
    모두 포함한다. 장수는 파일명의 "#장수"를 우선 쓰고, 없으면 zip을 열어
    이미지 개수를 센다(원격 마운트에서 느릴 수 있어 파일명에 있으면 열지 않음)."""
    out = []
    try:
        names = os.listdir(series_dir)
    except OSError:
        return out
    for fname in names:
        low = fname.lower()
        if not low.endswith(_ARCHIVE_EXTS):
            continue
        m = _EP_NO_RE.search(fname) or _LEADING_NO_RE.match(fname)
        no = int(m.group(1)) if m else 10 ** 9  # 번호를 모르면 맨 뒤로
        if low.endswith(".epub"):
            count = _epub_word_count(os.path.join(series_dir, fname))
        else:
            mc = _COUNT_RE.search(fname)
            count = int(mc.group(1)) if mc else _zip_image_count(os.path.join(series_dir, fname))
        out.append((fname, no, count))
    out.sort(key=lambda x: (x[1], x[0]))
    return out


def _split_names(text):
    return [p.strip() for p in re.split(r"[,/]", str(text or "")) if p.strip()]


def _dedupe(items):
    seen, out = set(), []
    for it in items:
        it = str(it).strip()
        if it and it not in seen:
            seen.add(it)
            out.append(it)
    return out


def _cover_b64(session, thumbnail_url, log=None):
    if not thumbnail_url:
        return None
    cover = downloader.fetch_cover_bytes(session, thumbnail_url, log=log)
    if not cover:
        return None
    return base64.b64encode(cover[0]).decode("ascii")


def build_data(t, title_id, archives, cover_b64=None, platform="naver"):
    """titles.json 레코드(t)와 폴더의 압축파일 목록으로 yaml dict를 만든다."""
    plat = PLATFORMS.get(platform, PLATFORMS["naver"])
    publisher = plat["publisher"]
    title = t.get("title") or str(title_id)
    code = "%s%s" % (plat["code"], title_id)
    link = plat["link"] % title_id

    writers = t.get("info_writers") or _split_names(t.get("author"))
    painters = t.get("info_painters") or []
    author_str = ", ".join(_dedupe(writers + [p for p in painters if p not in writers]))
    writers_str = ", ".join(_dedupe(writers)) or author_str
    illustrator_str = ", ".join(_dedupe(painters))

    tags = _dedupe(list(t.get("tags") or []) + list(t.get("info_tags") or []))
    genres = [g for g in tags if g in _NAVER_GENRES]
    if not genres and tags:
        genres = tags[:1]
    genre_str = ",".join(genres)
    tag_str = ",".join(_dedupe([publisher] + tags))

    finished = (t.get("status") == "완결") or bool(t.get("info_finished"))
    if finished:
        status = _STATUS_COMPLETED
    elif t.get("rest") or t.get("info_rest"):
        status = _STATUS_HIATUS
    else:
        status = _STATUS_ONGOING

    age = _AGE_MAP.get(str(t.get("info_age_type") or "").upper())
    if not age:
        age = "13" if (t.get("is_adult") or t.get("info_adult")) else "3"

    rd = t.get("release_date") or ""
    year, month, day = (rd[:4], rd[4:6], rd[6:8]) if len(rd) == 8 else ("", "", "")
    summary = t.get("synopsis") or ""

    files = {}
    for idx, (fname, _no, count) in enumerate(archives):
        is_epub = fname.lower().endswith(".epub")
        files[fname] = {
            "cover": (cover_b64 if (idx == 0 and cover_b64) else "FIRST"),
            "page": 1 if is_epub else count,
            "wordcount": count if is_epub else 0,
        }

    meta = {
        "Age Rating": age,
        "Collections": "",
        "Day": day,
        "Genres": genre_str,
        "Language": "ko",
        "Month": month,
        "Name": title,
        "Person Character": "",
        "Person Colorist": "",
        "Person CoverArtist": "",
        "Person Editor": "",
        "Person Imprint": "",
        "Person Inker": "",
        "Person Letterer": "",
        "Person Location": "",
        "Person Penciller": illustrator_str if illustrator_str and illustrator_str != writers_str else "",
        "Person Publisher": publisher,
        "Person Team": "",
        "Person Translator": "",
        "Person Writers": writers_str,
        "Publication Status": status,
        "Release Date": rd,
        "Summary": summary,
        "Tags": tag_str,
        "Web Links": link,
        "Writer": "",
        "Year": year,
    }
    search = [{
        "Day": day,
        "Month": month,
        "Publication Status": status,
        "Release Date": rd,
        "Year": year,
        "author": writers_str,
        "code": code,
        "description": summary,
        "genre": genre_str,
        "illustrator": illustrator_str,
        "link": link,
        "poster_url": t.get("thumbnail") or "",
        "publisher": publisher,
        "score": 100,
        "tag": tag_str,
        "title": title,
    }]
    return {
        "action": {"all_file_is_special": False, "code": code, "first_cover": False},
        "files": files,
        "meta": meta,
        "search": search,
    }


def refresh_title_info(session, title_id, t, force=False, log=None):
    """네이버 상세정보를 (하루 1회) 조회해 titles.json에 info_* 필드로 저장하고
    갱신된 레코드를 반환한다. status 필드는 건드리지 않는다(완결 알림 로직이
    status 변화를 기준으로 동작하므로)."""
    if session is None:
        return t
    last = t.get("info_fetched_at") or 0
    if not force and (time.time() - float(last)) < INFO_REFRESH_SECONDS:
        return t
    try:
        info = naver_api.fetch_title_info(session, title_id)
    except naver_api.NaverAuthExpired:
        raise
    except Exception as e:  # noqa: BLE001
        if log:
            log("titleId=%s: 작품 상세정보 조회 실패(무시하고 계속) - %s" % (title_id, e))
        info = None
    patch = {"info_fetched_at": time.time()}
    if info:
        patch.update({
            "synopsis": info.get("synopsis") or t.get("synopsis") or "",
            "info_writers": info.get("writers") or [],
            "info_painters": info.get("painters") or [],
            "info_tags": info.get("tags") or [],
            "info_age_type": info.get("age_type") or "",
            "info_finished": bool(info.get("finished")),
            "info_rest": bool(info.get("rest")),
            "info_adult": bool(info.get("adult")),
        })
        if not t.get("thumbnail") and info.get("thumbnail"):
            patch["thumbnail"] = info["thumbnail"]
    if ss.has_title(str(title_id)):
        titles = ss.upsert_title({str(title_id): patch})
        return titles.get(str(title_id), dict(t, **patch))
    _save_info_cache(title_id, patch)
    return dict(t, **patch)


def _load_info_cache():
    return ss.read_json(INFO_CACHE_PATH, {})


def _save_info_cache(title_id, patch):
    cache = _load_info_cache()
    cur = cache.get(str(title_id), {})
    cur.update(patch)
    cache[str(title_id)] = cur
    ss.write_json(INFO_CACHE_PATH, cache)


def _fetch_release_date(session, title_id, log=None):
    """1화 공개일을 모를 때(titles.json에 없는 작품 등) 회차 목록을 오래된 순
    (sort=ASC)으로 한 페이지만 받아 구한다. 응답이 정렬 파라미터를 무시하면
    가장 작은 번호가 1이 아닐 수 있으므로 그때는 버린다(틀린 날짜보다 빈 값이 낫다)."""
    if session is None:
        return ""
    try:
        resp = naver_api._get(session, naver_api.ARTICLE_LIST_API,
                              params={"titleId": title_id, "page": 1, "sort": "ASC"},
                              referer="%s?titleId=%s" % (naver_api.DETAIL_URL, title_id))
        body = resp.json()
        items = body.get("articleList") if isinstance(body, dict) else None
        if not items and isinstance(body, dict) and isinstance(body.get("result"), dict):
            items = body["result"].get("articleList")
        eps = [{"no": it.get("no"), "date": it.get("serviceDateDescription") or ""}
               for it in (items or []) if isinstance(it, dict)]
        eps = [e for e in eps if isinstance(e["no"], int)]
        if not eps or min(e["no"] for e in eps) != 1:
            return ""
        return release_date_from_episodes(eps)
    except Exception as e:  # noqa: BLE001
        if log:
            log("titleId=%s: 1화 공개일 조회 실패(무시) - %s" % (title_id, e))
        return ""


# ---------------------------------------------------------------------------
# "이미 최신인지" 빠른 확인
# ---------------------------------------------------------------------------
# 폴더별로 마지막으로 반영한 최종 회차 파일명/회차 수/작품정보 서명을 기억해 두고,
# 셋 다 같으면 상세정보 조회·표지 다운로드·yaml 생성을 전부 건너뛴다.
KAVITA_STATE_PATH = os.path.join(ss.DATA_DIR, "kavita_state.json")
# kavita.yaml을 다시 쓸지 판단하는 "작품 정보 서명"에 넣는 값.
# 표지(썸네일 URL/이미지), 휴재 여부, 태그 순서, 성인 표시처럼 회차와 상관없이 자주
# 흔들리는 값은 넣지 않는다 - 예전에는 이런 값 때문에 새 회차가 없어도 yaml이 계속
# 다시 써졌다(카카오는 같은 작품의 표지 kid가 목록/정보 API마다 다르고 수시로 바뀜,
# 네이버는 주간 스캔마다 휴재 플래그가 바뀜). 이런 값은 새 회차가 생겨 yaml을 다시
# 쓸 때 함께 최신으로 반영된다.
SIG_VERSION = 2


def _names(v):
    if isinstance(v, (list, tuple)):
        items = v
    else:
        items = str(v or "").split(",")
    return sorted(set(x.strip() for x in items if str(x).strip()))


def _meta_sig(t, platform, embed_cover):
    data = {
        "title": (t.get("title") or "").strip(),
        "writers": _names(t.get("info_writers") or t.get("author")),
        "painters": _names(t.get("info_painters")),
        "synopsis": " ".join(str(t.get("synopsis") or "").split()),
        "finished": (t.get("status") == "완결") or bool(t.get("info_finished")),
        "genre": (t.get("genre") or "").strip(),
        "release_date": t.get("release_date") or "",
        "_platform": platform,
        "_cover": bool(embed_cover),
        "_v": SIG_VERSION,
    }
    raw = json.dumps(data, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _state_key(series_dir):
    return os.path.abspath(series_dir)


def _load_state():
    return ss.read_json(KAVITA_STATE_PATH, {})


def _save_state(series_dir, archives, sig):
    with ss._lock:   # 여러 작품을 동시에 처리할 때 서로 덮어쓰지 않게
        st = _load_state()
        st[_state_key(series_dir)] = {"latest": archives[-1][0], "count": len(archives), "sig": sig,
                                      "v": SIG_VERSION, "at": time.time()}
        ss.write_json(KAVITA_STATE_PATH, st)


def _already_current(path, series_dir, archives, sig):
    """kavita.yaml에 최종 회차가 이미 반영돼 있고 작품 정보도 그대로면 True."""
    if not os.path.exists(path):
        return False
    latest, count = archives[-1][0], len(archives)
    rec = _load_state().get(_state_key(series_dir))
    if rec:
        if rec.get("latest") != latest or rec.get("count") != count:
            return False                    # 새 회차/회차 변경 -> 다시 씀
        if rec.get("v") != SIG_VERSION:
            # 1.32.1 이전 기록(서명 방식이 다름): 회차는 그대로이므로 다시 쓰지 않고
            # 새 방식 서명만 기록해 둔다(업데이트 직후 전체 yaml이 한꺼번에 바뀌지 않게)
            _save_state(series_dir, archives, sig)
            return True
        return rec.get("sig") == sig
    # 이 기능 이전에 만들어진 yaml: 파일 안에 최종 회차 항목이 있고 회차 수가 같으면
    # 최신으로 보고 기록만 남긴다(작품 정보 변경은 다음부터 서명으로 감지).
    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return False
    if ("    %s:\n" % json.dumps(latest, ensure_ascii=False)) in text and \
            text.count("\n        page: ") == count:
        _save_state(series_dir, archives, sig)
        return True
    return False


def _write_kavita_yaml_impl(download_root, title_id, session=None, embed_cover=True,
                      refresh_info=True, force_info=False, log=None,
                      series_dir=None, folder_title=None, platform="naver"):
    """시리즈 폴더에 kavita.yaml을 생성/갱신한다.

    series_dir를 주면(폴더 전체 스캔 시) 그 폴더를 그대로 쓴다. 제목이 나중에
    바뀌어 titles.json의 제목과 폴더명이 달라진 작품도 놓치지 않기 위함이다.
    titles.json에 없는 작품이면 폴더명 제목 + 네이버 상세정보(별도 캐시)로 만든다.
    최종 회차가 이미 반영돼 있고 작품 정보가 같으면 아무 것도 하지 않고 "unchanged"
    (force_info=True면 무조건 다시 만듦 - [설정]의 "전체 작품 kavita.yaml 생성/갱신" 버튼).
    반환: "written" | "unchanged" | "skipped" | "error"
    """
    tid = str(title_id)
    if platform in ("kakao", "kakao_novel"):
        # 카카오 작품 정보는 kakao_pipeline이 회차 목록을 볼 때 이미 갱신한다
        refresh_info = False
    try:
        if platform in ("kakao", "kakao_novel"):
            t = ss.get_kakao_title(tid)
        else:
            t = ss.get_title(tid)
        if t is not None:
            t = dict(t)
        if not t:
            if not series_dir:
                return "skipped"
            t = dict(_load_info_cache().get(tid, {}))
            t.setdefault("title", folder_title or tid)
        if not series_dir:
            series_dir = downloader.title_dir(download_root, t.get("title") or tid, tid)
        title = t.get("title") or tid
        archives = _list_archives(series_dir)
        if not archives:
            if log and folder_title is not None:
                log("%s: 회차 압축파일(zip/cbz)이 없어 건너뜀" % os.path.basename(series_dir))
            return "skipped"  # 받은 회차가 없는 작품은 만들지 않음

        path = os.path.join(series_dir, YAML_NAME)
        if not force_info and _already_current(path, series_dir, archives,
                                               _meta_sig(t, platform, embed_cover)):
            return "unchanged"

        if refresh_info:
            try:
                t = refresh_title_info(session, tid, t, force=force_info, log=log)
            except naver_api.NaverAuthExpired as e:
                if log:
                    log("titleId=%s: 상세정보 조회 중 인증 만료 - 기존 정보로 yaml 생성 (%s)" % (tid, e))
            # titles.json에 없는 작품은 상세정보의 제목/썸네일/성인 여부로 보강
            if not t.get("title") or t.get("title") == tid:
                t["title"] = folder_title or tid
            if not t.get("release_date") and (force_info or not ss.has_title(tid)):
                rd = _fetch_release_date(session, tid, log=log)
                if rd:
                    t["release_date"] = rd
                    if ss.has_title(tid):
                        ss.upsert_title({tid: {"release_date": rd}})
                    else:
                        _save_info_cache(tid, {"release_date": rd})

        # 표지는 기존 kavita.yaml에 이미 들어 있으면 그대로 쓴다. 같은 표지라도 CDN이
        # 돌려주는 이미지 바이트/주소가 수시로 달라져서, 매번 새로 받으면 새 회차가
        # 없어도 파일이 바뀌었다. 새로 받는 건 표지가 아직 없을 때와
        # [전체 작품 kavita.yaml 생성/갱신](force_info) 때뿐.
        cover = None
        if embed_cover:
            if not force_info and os.path.exists(path):
                cover = _existing_first_cover(path)
            if not cover:
                cover = _cover_b64(session, t.get("thumbnail"), log=log)
            if not cover and os.path.exists(path):
                cover = _existing_first_cover(path)

        text = dump_yaml(build_data(t, tid, archives, cover_b64=cover, platform=platform))

        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    if f.read() == text:
                        _save_state(series_dir, archives, _meta_sig(t, platform, embed_cover))
                        return "unchanged"
            except OSError:
                pass

        tmp_path = path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        os.replace(tmp_path, path)
        _save_state(series_dir, archives, _meta_sig(t, platform, embed_cover))
        if log:
            log("%s: kavita.yaml 갱신 (%d개 회차)" % (title, len(archives)))
        return "written"
    except Exception as e:  # noqa: BLE001
        if log:
            log("titleId=%s: kavita.yaml 생성 실패(무시하고 계속) - %s" % (tid, e))
        return "error"


def _existing_first_cover(path):
    """기존 kavita.yaml에서 files 블록 첫 항목의 cover 값(base64)을 읽는다."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                s = line.strip()
                if s.startswith("cover:"):
                    val = s[len("cover:"):].strip()
                    if val.startswith('"'):
                        try:
                            val = json.loads(val)
                        except ValueError:
                            return None
                    return None if val in ("", "FIRST") else val
    except OSError:
        pass
    return None


def write_kavita_yaml(*args, **kwargs):
    try:
        return _write_kavita_yaml_impl(*args, **kwargs)
    finally:
        try:
            flush_epub_cache()
        except Exception:  # noqa: BLE001
            pass
