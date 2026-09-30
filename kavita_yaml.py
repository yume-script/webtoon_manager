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
import json
import os
import re
import time

from . import downloader, naver_api, state_store as ss

YAML_NAME = "kavita.yaml"
PUBLISHER = "네이버 웹툰"
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

_ARCHIVE_RE = re.compile(r"(\d+)화#(\d+)\.(zip|cbz)$", re.I)
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


def _list_archives(series_dir):
    """시리즈 폴더의 회차 압축파일 [(파일명, 회차번호, 장수)] 을 회차순으로."""
    out = []
    try:
        names = os.listdir(series_dir)
    except OSError:
        return out
    for fname in names:
        if fname.endswith(".tmp"):
            continue
        m = _ARCHIVE_RE.search(fname)
        if not m:
            continue
        out.append((fname, int(m.group(1)), int(m.group(2))))
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


def build_data(t, title_id, archives, cover_b64=None):
    """titles.json 레코드(t)와 폴더의 압축파일 목록으로 yaml dict를 만든다."""
    title = t.get("title") or str(title_id)
    code = "BNW%s" % title_id
    link = "https://comic.naver.com/webtoon/list?titleId=%s" % title_id

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
    tag_str = ",".join(_dedupe([PUBLISHER] + tags))

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
        files[fname] = {
            "cover": (cover_b64 if (idx == 0 and cover_b64) else "FIRST"),
            "page": count,
            "wordcount": 0,
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
        "Person Publisher": PUBLISHER,
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
        "publisher": PUBLISHER,
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
    titles = ss.upsert_title({str(title_id): patch})
    return titles.get(str(title_id), dict(t, **patch))


def write_kavita_yaml(download_root, title_id, session=None, embed_cover=True,
                      refresh_info=True, force_info=False, log=None):
    """시리즈 폴더에 kavita.yaml을 생성/갱신한다.
    반환: "written" | "unchanged" | "skipped" | "error"
    """
    tid = str(title_id)
    try:
        t = ss.load_titles().get(tid)
        if not t:
            return "skipped"
        title = t.get("title") or tid
        series_dir = downloader.title_dir(download_root, title, tid)
        archives = _list_archives(series_dir)
        if not archives:
            return "skipped"  # 받은 회차가 없는 작품은 만들지 않음

        if refresh_info:
            try:
                t = refresh_title_info(session, tid, t, force=force_info, log=log)
            except naver_api.NaverAuthExpired as e:
                if log:
                    log("titleId=%s: 상세정보 조회 중 인증 만료 - 기존 정보로 yaml 생성 (%s)" % (tid, e))

        cover = _cover_b64(session, t.get("thumbnail"), log=log) if embed_cover else None
        path = os.path.join(series_dir, YAML_NAME)

        # 썸네일을 이번에 못 받았으면(네트워크 오류 등) 기존 파일의 표지 값을
        # 그대로 유지해서, 표지 하나 때문에 파일이 계속 바뀌지 않게 한다.
        if embed_cover and not cover and os.path.exists(path):
            cover = _existing_first_cover(path)

        text = dump_yaml(build_data(t, tid, archives, cover_b64=cover))

        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    if f.read() == text:
                        return "unchanged"
            except OSError:
                pass

        tmp_path = path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        os.replace(tmp_path, path)
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
