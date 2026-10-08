# -*- coding: utf-8 -*-
"""
카카오페이지 다운로드 파이프라인
--------------------------------
네이버 쪽(pipeline.py)과 같은 저장 규칙을 그대로 쓴다.
- 시리즈 폴더: <KAKAO_DOWNLOAD_ROOT>/<제목> (<series_id>)/
- 회차 파일:   <제목> 0001화#<장수>.zip  (+ ComicInfo.xml, 표지 첫 페이지 옵션)
- series.json / kavita.yaml(code: KKP<series_id>) 동일 옵션으로 생성
- 이미지는 로컬 임시 폴더(<TEMP_DOWNLOAD_ROOT>/kakao/...)에 받은 뒤 압축해서 옮김

받는 회차: 로그인 계정으로 볼 수 있는 회차(무료 / 이미 대여·소장한 회차).
KAKAO_USE_WAITFREE를 켜면 볼 수 없는 회차 중 기다무 가능한 가장 앞 회차에
대여권을 1장 사용해 받는다(작품당 실행 1회에 1장까지). 유료 구매는 하지 않는다.
"""
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from . import discord_notify, downloader, kakao_api, kavita_yaml, state_store as ss

PLATFORM = "kakao"
# 1.16.0은 작품 HTML에서 정보를 읽어 제목이 깨지고(문자셋 오판) 표지가 사이트
# 기본 로고로 저장됐다. 이 값보다 낮은 레코드는 다음 실행 때 API로 다시 채운다.
INFO_VERSION = 3
# 예약(자동) 실행에서 볼 수 없는 회차가 이만큼 연달아 나오면 그 작품은 멈춘다
# (유료 구간에서 회차마다 요청을 보내지 않기 위함).
MAX_CONSECUTIVE_LOCKED = 3



def _low_prio(fn):
    """백그라운드 작업 스레드를 낮은 CPU 우선순위로 실행(웹서버 응답 우선)."""
    def _wrapped(*a, **kw):
        try:
            downloader.lower_thread_priority(10)
        except Exception:  # noqa: BLE001
            pass
        return fn(*a, **kw)
    return _wrapped

def _num(cfg, key, default):
    try:
        return type(default)(cfg.get(key, default))
    except (TypeError, ValueError):
        return default


def kakao_root(cfg):
    return (cfg.get("KAKAO_DOWNLOAD_ROOT") or "").strip() or ss.KAKAO_DOWNLOAD_DEFAULT_DIR


def kakao_novel_root(cfg):
    return ((cfg.get("KAKAO_NOVEL_DOWNLOAD_ROOT") or "").strip()
            or os.path.join(ss.DATA_DIR, "kakao_novels"))


def novel_enabled(cfg):
    v = cfg.get("KAKAO_NOVEL_ENABLE", False)
    return v if isinstance(v, bool) else str(v).lower() in ("1", "true", "on", "yes")


def series_root(cfg, t):
    """웹툰은 카카오 웹툰 경로, 웹소설은 웹소설 전용 경로."""
    return kakao_novel_root(cfg) if _is_novel(t) else kakao_root(cfg)


def yaml_platform(t):
    return "kakao_novel" if _is_novel(t) else PLATFORM


def kakao_temp_root(cfg):
    base = (cfg.get("TEMP_DOWNLOAD_ROOT") or "").strip() or ss.TMP_DOWNLOAD_DEFAULT_DIR
    return os.path.join(base, "kakao")


def build_session_from_cfg(cfg):
    return kakao_api.build_session(cfg.get("KAKAO_COOKIE"),
                                   timeout=_num(cfg, "REQUEST_TIMEOUT_SECONDS", 15))


def _series_json_meta(t, sid):
    genre = t.get("genre") or ""
    return {
        "author": t.get("author") or "",
        "publisher": "카카오페이지",
        "summary": t.get("synopsis") or "",
        "link": "https://page.kakao.com/content/%s" % sid,
        "genre": genre,
        "tags": genre,
        "release_date": t.get("release_date") or "",
        "cover_image_url": t.get("thumbnail") or "",
    }


def _comicinfo_meta(t, ep, sid):
    return {
        "series": t.get("title"),
        "sub_title": ep.get("subtitle"),
        "summary": t.get("synopsis") or None,
        "writer": t.get("author") or None,
        "genre": t.get("genre") or None,
        "web": "https://page.kakao.com/content/%s" % sid,
        "age_rating": "Adults Only 18+" if t.get("adult") else None,
    }


def _info_patch(info):
    patch = {}
    for k in ("title", "thumbnail", "cover_url", "synopsis", "author", "genre", "release_date",
              "category"):
        if info.get(k):
            patch[k] = info[k]
    if info.get("title"):
        patch["info_version"] = INFO_VERSION
    patch["adult"] = bool(info.get("adult"))
    patch["status"] = "완결" if info.get("finished") else "연재"
    # kavita.yaml(build_data)가 쓰는 필드 이름에 맞춰 둔다
    if info.get("author"):
        patch["info_writers"] = [a.strip() for a in info["author"].split(",") if a.strip()]
    if info.get("genre"):
        patch["tags"] = [info["genre"]]
    patch["info_fetched_at"] = time.time()
    return patch


# 파일 번호 규칙 버전. "subtitle" = 회차 제목의 "N화" 번호(1.18.5~).
# 그 전 버전은 사이트 순번(order_value)을 썼기 때문에, 트레일러가 1번인 작품은
# 실제 1화가 0002화로 저장됐다. 버전이 다르면 기존 파일 이름을 새 번호로 바꾼다.
EP_NUMBERING = "subtitle2"
# "subtitle"(1.18.5~1.27.0): 회차 제목의 마지막 "N화" - "208화 (외전 8화)"가 8로 잡히는 문제가
# 있어 "subtitle2"(첫 번째 "N화", 작품 제목 접두어 제외)로 바꿨다. 예전 번호 파일은 자동 이름 변경.
INCREMENTAL_FULL_EVERY = 7 * 24 * 3600   # 새 회차만 받는 빠른 확인을 쓰더라도 일주일에 1번은 전체 목록 확인


def _renumber_existing_files(root, title, sid, episodes, folder_zero_fill=4, log=print, old_map=None):
    """예전(사이트 순번) 번호로 저장된 회차 파일을 회차 제목 번호로 이름 변경.
    번호끼리 겹칠 수 있으므로(2->1, 3->2 ...) 임시 이름을 거쳐 2단계로 바꾼다."""
    series_dir = downloader.title_dir(root, title, sid)
    if not os.path.isdir(series_dir):
        return 0
    zf = int(folder_zero_fill or 4)
    prefix = downloader.safe_name(title) + " "
    # 예전 파일 번호 -> 새 번호. old_map({순번: 예전 번호})이 없으면 예전 번호 = 사이트 순번(1.18.4 이하)
    mapping = {}
    for ep in episodes:
        if ep.get("order") is None:
            continue
        old_no = (old_map or {}).get(ep["order"], ep["order"]) if old_map is not None else ep["order"]
        mapping[old_no] = ep["no"]
    moves = []
    for f in os.listdir(series_dir):
        if not f.startswith(prefix) or not f.lower().endswith(".zip"):
            continue
        rest = f[len(prefix):]
        m = kavita_yaml._EP_NO_RE.match(rest)
        if not m:
            continue
        old_no = int(m.group(1))
        new_no = mapping.get(old_no)
        if new_no is None or new_no == old_no:
            continue
        new_name = prefix + str(new_no).zfill(zf) + rest[m.end(1):]
        moves.append((f, new_name))
    if not moves:
        return 0
    downloader.wait_moves(series_dir)     # 옮기는 중인 zip이 다 들어간 뒤에 이름 변경
    downloader.invalidate_dir(series_dir)
    try:
        for f, _new in moves:
            os.replace(os.path.join(series_dir, f), os.path.join(series_dir, f + ".renum"))
        for f, new in moves:
            dst = os.path.join(series_dir, new)
            src = os.path.join(series_dir, f + ".renum")
            if os.path.exists(dst):
                log("%s: 번호 변경 대상이 이미 있어 예전 파일 유지 - %s" % (title, f))
                os.replace(src, os.path.join(series_dir, f))
                continue
            os.replace(src, dst)
        log("%s: 회차 파일 %d개를 회차 제목 번호로 이름 변경 (예: %s -> %s)" %
            (title, len(moves), moves[0][0], moves[0][1]))
    except OSError as e:
        log("%s: 회차 번호 이름 변경 중 오류(일부만 적용됐을 수 있음) - %s" % (title, e))
    downloader.invalidate_dir(series_dir)
    return len(moves)


def adult_block_reason(cfg, t):
    """이 작품을 성인 인증 문제로 건너뛰어야 하면 사유 문자열, 아니면 None."""
    if not t.get("adult"):
        return None
    cookie = (cfg.get("KAKAO_COOKIE") or "").strip()
    if not cookie:
        return "성인 작품 - 성인 인증된 카카오 계정의 로그인 쿠키가 없어 받을 수 없음"
    if t.get("adult_block_hash") and t.get("adult_block_hash") == kakao_api._cookie_hash(cookie):
        return "성인 인증 실패(현재 쿠키) - 쿠키를 바꾸면 다시 시도"
    return None


SPECIALS_FILE = "특별회차_목록.txt"


def _ep_label(no):
    if no >= kakao_api.SPECIAL_EP_BASE:
        return "특별회차(사이트 %d번째 회차, 파일 %d화)" % (no - kakao_api.SPECIAL_EP_BASE, no)
    return "%d화" % no


def _record_special(series_dir, title, no, subtitle):
    """9000번대로 저장한 특별 회차가 무엇인지 시리즈 폴더의 안내 파일에 남긴다."""
    path = os.path.join(series_dir, SPECIALS_FILE)
    try:
        lines = []
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                lines = [l.rstrip("\n") for l in f if l.strip()]
        header = [
            "# %s - 9000번대 회차 안내" % title,
            "# 카카오페이지 회차 제목에 'N화' 번호가 없는 회차(프롤로그/외전/후기/특별편 등)와",
            "# 앞 회차와 번호가 겹치는 회차(시즌2에서 1화부터 다시 시작 등)는 본편과 섞이지 않게",
            "# '9000 + 사이트 순번'으로 저장합니다. 아래는 파일 번호 = 실제 회차 제목입니다.",
        ]
        body = [l for l in lines if not l.startswith("#")]
        entry = "%04d화 = %s" % (no, subtitle or "(제목 없음)")
        if not any(l.startswith("%04d화 " % no) for l in body):
            body.append(entry)
        body.sort()
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(header + body) + "\n")
    except OSError:
        pass


def novel_file_name(title, no, folder_zero_fill=4):
    return "%s %s화.epub" % (downloader.safe_name(title), str(no).zfill(int(folder_zero_fill or 4)))


def _download_novel_episode(cfg, session, root, title, sid, ep, t, force=False, log=print):
    """웹소설 회차 하나를 EPUB으로 저장. 반환은 download_episode와 같은 모양
    (ok, skipped, 단어수, err). 볼 수 없는 회차 예외는 그대로 올린다."""
    from . import novel_epub
    no = ep["no"]
    fz = _num(cfg, "FOLDER_ZERO_FILL", 4)
    series_dir = downloader.title_dir(root, title, sid)
    path = os.path.join(series_dir, novel_file_name(title, no, fz))
    if os.path.exists(path) and not force:
        return True, True, 0, None
    data = kakao_api.fetch_novel_episode(session, sid, ep["product_id"], log=log)
    cover = None
    cover_src = t.get("cover_url") or t.get("thumbnail")
    if not data["images"] and cover_src:
        got = downloader.fetch_cover_bytes(session, cover_src, log=log)
        if got:
            mt = {".png": "image/png", ".gif": "image/gif", ".webp": "image/webp"}.get(got[1], "image/jpeg")
            cover = (got[0], mt)
    os.makedirs(series_dir, exist_ok=True)
    ep_title = ep.get("subtitle") or ("%s %d화" % (title, no))
    novel_epub.build_epub(
        path, ep_title, title, t.get("author") or "", data["sections"], data["images"],
        css=data["css"], cover=cover, identifier="kakaopage:%s:%s" % (sid, ep["product_id"]),
        episode_no=no)
    kavita_yaml.remember_epub_words(path, data["words"])
    time.sleep(max(0.0, float(_num(cfg, "DELAY_SECONDS", 1.0))))
    return True, False, data["words"], None


def platform_label(t, with_kind=False):
    base = "카카오웹소설" if _is_novel(t) else "카카오웹툰"
    if with_kind:
        from . import pipeline as _pl
        kind = _pl.kind_label(t, "kakao")
        if kind:
            return "[%s·%s]" % (base, kind)
    return "[%s]" % base


def _is_novel(t):
    return "소설" in str((t or {}).get("category") or "")


def _migrate_title_folder(root, old_title, new_title, sid, log=print):
    """제목이 바뀌면(예: 1.16.0에서 깨진 제목으로 저장된 경우) 기존 시리즈 폴더와
    그 안의 회차 파일 이름을 새 제목으로 옮긴다. 새 폴더가 이미 있으면 파일만
    새 폴더로 옮긴다."""
    if not old_title or old_title == new_title:
        return
    old_dir = downloader.title_dir(root, old_title, sid)
    new_dir = downloader.title_dir(root, new_title, sid)
    if not os.path.isdir(old_dir) or os.path.abspath(old_dir) == os.path.abspath(new_dir):
        return
    downloader.wait_moves(old_dir)
    try:
        os.makedirs(new_dir, exist_ok=True)
        old_prefix = downloader.safe_name(old_title) + " "
        new_prefix = downloader.safe_name(new_title) + " "
        for f in os.listdir(old_dir):
            nf = new_prefix + f[len(old_prefix):] if f.startswith(old_prefix) else f
            src, dst = os.path.join(old_dir, f), os.path.join(new_dir, nf)
            if os.path.exists(dst):
                continue
            os.replace(src, dst)
        try:
            os.rmdir(old_dir)
        except OSError:
            pass
        log("카카오 %s: 제목 변경에 맞춰 폴더/파일 이름 정리 (%s -> %s)" % (sid, old_title, new_title))
    except OSError as e:
        log("카카오 %s: 폴더 이름 정리 실패(무시) - %s" % (sid, e))
    downloader.invalidate_dir(old_dir)
    downloader.invalidate_dir(new_dir)


def _refresh_info(cfg, session, sid, t, log=print):
    info = kakao_api.fetch_series_info(session, sid)
    if not info:
        return t
    patch = _info_patch(info)
    if patch.get("title") and patch["title"] != t.get("title"):
        _migrate_title_folder(series_root(cfg, t), t.get("title"), patch["title"], sid, log=log)
    ss.upsert_kakao_title({sid: patch})
    t = dict(t)
    t.update(patch)
    return t


_repair_started = False


def repair_old_records_async(cfg):
    """예전 버전(INFO_VERSION 미만)으로 저장된 작품 정보를 백그라운드에서 한 번 고친다
    (대시보드 폴링에서 호출, 프로세스당 1회)."""
    global _repair_started
    if _repair_started:
        return
    bad = [sid for sid, t in ss.load_kakao_titles().items()
           if int(t.get("info_version") or 0) < INFO_VERSION]
    _repair_started = True
    if not bad:
        return

    def _run():
        session = build_session_from_cfg(cfg)
        for sid in bad:
            t = ss.get_kakao_title(sid)
            if t:
                _refresh_info(cfg, session, sid, t, log=ss.append_log)
                time.sleep(0.3)

    import threading
    threading.Thread(target=_low_prio(_run), name="webtoon_manager_kakao_repair", daemon=True).start()


# ---------------------------------------------------------------------------
# 연재 목록 스캔 (네이버 요일별/완결 스캔과 같은 역할)
# ---------------------------------------------------------------------------
# 목록 항목에 표지 정보가 없는 작품은 작품 정보 API로 표지를 채우는데, 한 번의
# 스캔에서 너무 많이 요청하지 않도록 상한을 둔다(나머지는 다음 스캔 때 채움).
THUMB_FILL_PER_SCAN = 60


def _merge_scan_result(cfg, merged, log=print, finished_scan=False):
    """스캔으로 모은 {sid: rec}를 kakao_titles.json에 반영한다. 사용자가 고른
    구독/구독해제/제외는 그대로 두고, 새로 발견한 작품은 네이버와 같은 규칙
    (작가 자동구독 / 신간 자동구독 설정)으로 판단한다."""
    from . import pipeline
    old = ss.load_kakao_titles()
    at = ss.load_authors_tags()
    author_names = set(a.strip() for a in at.get("authors", []) if a.strip())
    auto_new = bool(cfg.get("AUTO_SUBSCRIBE_NEW_TITLES")) and bool(old)
    if finished_scan:
        # 네이버 완결 스캔과 동일: 완결작은 작가/신간 자동구독 대상에서 뺀다
        author_names, auto_new = set(), False

    patch = {}
    for sid, rec in merged.items():
        o = old.get(sid, {})
        p = pipeline._autosubscribe_patch(rec, o, author_names, auto_new=auto_new)
        if o.get("thumbnail"):
            # 작품 정보 API로 받아둔 정식 표지를 목록의 카드 이미지로 덮어쓰지 않는다
            # (표지가 바뀌면 kavita.yaml cover도 매번 바뀌어 파일이 계속 다시 써짐)
            p["thumbnail"] = o["thumbnail"]
        if finished_scan and o.get("weekdays"):
            p["weekdays"] = o["weekdays"]   # 완결 스캔은 요일 정보를 지우지 않음
        if not o and not p.get("subscribed"):
            p.setdefault("last_downloaded_no", None)
        patch[sid] = p
    pipeline.apply_bl_policy(cfg, patch, old, log=log, label="카카오: ")
    ss.upsert_kakao_title(patch)
    return patch


def _apply_waitfree_autosubscribe(cfg, patch, log=print):
    """'기다무 자동 구독'이 켜져 있으면 연재 중인 기다무 작품을 구독으로 올린다.
    네이버 '매일+ 자동 구독'과 같은 규칙: 사용자가 구독/구독해제/제외를 한 번도
    고르지 않은(전부 기본값인) 작품만 건드린다. 완결작은 대상이 아니다."""
    from . import pipeline as _plx
    toon_on = _plx._truthy(cfg.get("KAKAO_DOWNLOAD_WAITFREE"))
    novel_on = _plx._truthy(cfg.get("KAKAO_NOVEL_DOWNLOAD_WAITFREE")) and novel_enabled(cfg)
    current = ss.load_kakao_titles()
    # '기다무 작품 다운로드'가 꺼진 쪽(웹툰/웹소설)은 예전에 자동 구독됐던 기다무 작품을
    # 구독 전 상태로 되돌린다(직접 구독한 작품은 그대로). 다시 켜면 다음 스캔 때 다시 자동 구독.
    off = {sid: {"subscribed": False, "auto_subscribed": None}
           for sid, t in current.items()
           if t.get("auto_subscribed") == "waitfree" and t.get("subscribed") and not t.get("manual_subscribed")
           and not (novel_on if _is_novel(t) else toon_on)}
    if off:
        ss.upsert_kakao_title(off)
        log("기다무 작품 받기가 꺼져 있어 자동 구독됐던 기다무 작품 %d개를 구독 전 상태로 되돌림" % len(off))
        current = ss.load_kakao_titles()
    if not (toon_on or novel_on):
        return 0
    promote = {}
    # 예전 버전이 쿠키 없이 자동 구독해 둔 성인 기다무 작품은 받을 수 없으므로
    # 자동 구독 이전 상태로 되돌린다(사용자가 직접 구독한 작품은 건드리지 않음)
    from . import pipeline as _pl
    demote = {sid: {"subscribed": False, "auto_subscribed": None}
              for sid, t in current.items()
              if t.get("auto_subscribed") and t.get("subscribed")
              and (adult_block_reason(cfg, t) or _pl.bl_blocked(cfg, t))}
    if demote:
        ss.upsert_kakao_title(demote)
        log("카카오 자동 구독 작품 중 %d개(성인 인증 쿠키 없음 / BL·GL 장르 비허용)를 자동 구독에서 뺐습니다" % len(demote))
        current = ss.load_kakao_titles()
    for sid in patch:
        t = current.get(sid) or {}
        if not t.get("waitfree") or t.get("status") == "완결":
            continue
        if not (novel_on if _is_novel(t) else toon_on):
            continue
        if adult_block_reason(cfg, t):
            continue   # 성인 인증 쿠키가 없으면 받을 수 없으니 자동 구독하지 않음
        if _pl.bl_blocked(cfg, t):
            continue   # BL 장르 다운로드를 허용하지 않으면 자동 구독하지 않음
        if t.get("subscribed") or t.get("excluded") or t.get("unsubscribed"):
            continue
        promote[sid] = {"subscribed": True, "auto_subscribed": "waitfree"}
    if promote:
        ss.upsert_kakao_title(promote)
        log("카카오 기다무 자동 구독: %d개 작품을 새로 구독 처리함" % len(promote))
    return len(promote)


def _needs_check(cfg, t, now=None):
    """자동 사이클에서 이 작품의 회차 목록을 다시 볼 필요가 있는지.
    - 새 회차가 올라왔거나(last_slide_added_dt 변경) 아직 한 번도 안 봤으면 확인
    - 기다무 사용이 켜져 있고, 볼 수 없는 회차가 남아 있고, 대여권 충전 시간이
      지났으면 확인
    그 외에는 건너뛴다(구독작이 수백~천 개일 때 매 주기 전체 조회를 피하기 위함)."""
    now = now or time.time()
    if not t.get("checked_slide_dt") or t.get("checked_slide_dt") != t.get("last_slide_added_dt"):
        return True
    if not t.get("last_slide_added_dt"):
        return True
    if (cfg.get("KAKAO_USE_OWNED_TICKETS") and t.get("has_locked")
            and now - float(t.get("last_checked_at") or 0) >= 24 * 3600):
        return True   # 보유 대여권이 새로 생겼을 수 있으니 하루 1번은 확인
    if cfg.get("KAKAO_USE_WAITFREE") and t.get("waitfree") and t.get("has_locked"):
        period = max(60, int(t.get("waitfree_period_min") or 1440)) * 60
        if now - float(t.get("last_ticket_at") or 0) >= period:
            return True
    return False


def _fill_missing_thumbnails(session, log=print, limit=THUMB_FILL_PER_SCAN, should_cancel=None):
    titles = ss.load_kakao_titles()
    todo = [sid for sid, t in titles.items() if not t.get("thumbnail")][:limit]
    filled = 0
    for sid in todo:
        if should_cancel and should_cancel():
            break
        info = kakao_api.fetch_series_info(session, sid)
        if info.get("thumbnail"):
            ss.upsert_kakao_title({sid: {"thumbnail": info["thumbnail"],
                                         "cover_url": info.get("cover_url") or info["thumbnail"],
                                         "synopsis": info.get("synopsis") or ""}})
            filled += 1
        time.sleep(0.2)
    if filled:
        log("카카오페이지: 표지 %d개 채움" % filled)


def _scan_categories(cfg):
    cats = [(kakao_api.WEBTOON_CATEGORY_UID, "웹툰")]
    if novel_enabled(cfg):
        cats.append((kakao_api.NOVEL_CATEGORY_UID, "웹소설"))
    return cats


def run_kakao_scan_weekday(cfg, log=print, should_cancel=None):
    """월~일(tab 1~7) + 신작(tab 11) 목록을 모아 반영한다."""
    session = build_session_from_cfg(cfg)
    merged = {}
    cats = _scan_categories(cfg)
    for cat_uid, cat_label in cats:
      for tab, day in sorted(kakao_api.WEEKDAY_TABS.items()):
        if should_cancel and should_cancel():
            break
        ss.save_job_state({"message": "카카오페이지 %s %s요일 목록 수집 중" % (cat_label, "월화수목금토일"[tab - 1])})
        try:
            items = kakao_api.fetch_landing_all(session, tab_uid=tab, should_cancel=should_cancel,
                                                category_uid=cat_uid)
        except Exception as e:  # noqa: BLE001
            log("카카오페이지 %s %s 목록 수집 실패: %s" % (cat_label, day, e))
            continue
        for it in items:
            sid = str(it.get("series_id"))
            rec = merged.get(sid)
            if rec is None:
                rec = kakao_api.landing_item_to_title(it)
                rec["weekdays"] = []
                rec["new"] = False
                merged[sid] = rec
            if day not in rec["weekdays"]:
                rec["weekdays"].append(day)
    for cat_uid, cat_label in cats:
      if not (should_cancel and should_cancel()):
        ss.save_job_state({"message": "카카오페이지 %s 신작 목록 수집 중" % cat_label})
        try:
            for it in kakao_api.fetch_landing_all(session, tab_uid=kakao_api.TAB_NEW,
                                                  should_cancel=should_cancel, category_uid=cat_uid):
                sid = str(it.get("series_id"))
                rec = merged.get(sid)
                if rec is None:
                    rec = kakao_api.landing_item_to_title(it)
                    rec["weekdays"] = []
                    merged[sid] = rec
                rec["new"] = True
        except Exception as e:  # noqa: BLE001
            log("카카오페이지 신작 목록 수집 실패: %s" % e)

    ss.save_job_state({"message": "카카오페이지 목록 반영 중"})
    patch = _merge_scan_result(cfg, merged, log=log)
    _apply_waitfree_autosubscribe(cfg, patch, log=log)
    log("카카오페이지 요일별 스캔 완료: %d개 작품" % len(patch))
    ss.save_job_state({"message": "카카오페이지 표지 채우는 중"})
    _fill_missing_thumbnails(session, log=log, should_cancel=should_cancel)
    ss.save_job_state({"message": "카카오페이지 요일별 목록 수집 완료: %d개 작품" % len(patch)})
    return {"scanned": len(patch)}


def run_kakao_scan_finished(cfg, log=print, should_cancel=None, max_pages=200):
    """완결(tab 12) 전체 목록. 수천 개라 네이버 완결 스캔과 같은 시각에 따로 돈다."""
    session = build_session_from_cfg(cfg)
    items = []
    for cat_uid, cat_label in _scan_categories(cfg):
        ss.save_job_state({"message": "카카오페이지 %s 완결 목록 수집 중" % cat_label})
        try:
            items += kakao_api.fetch_landing_all(session, tab_uid=kakao_api.TAB_FINISHED,
                                                 max_pages=max_pages, should_cancel=should_cancel,
                                                 category_uid=cat_uid)
        except Exception as e:  # noqa: BLE001
            log("카카오페이지 %s 완결 목록 수집 실패: %s" % (cat_label, e))
    merged = {}
    for it in items:
        rec = kakao_api.landing_item_to_title(it)
        rec["status"] = "완결"
        merged[str(it.get("series_id"))] = rec
    patch = _merge_scan_result(cfg, merged, log=log, finished_scan=True)
    log("카카오페이지 완결 스캔 완료: %d개 작품" % len(patch))
    _fill_missing_thumbnails(session, log=log, should_cancel=should_cancel)
    ss.save_job_state({"message": "카카오페이지 완결 목록 수집 완료: %d개 작품" % len(patch)})
    return {"scanned": len(patch)}


# ---------------------------------------------------------------------------
# 등록 / 해제
# ---------------------------------------------------------------------------
def add_series(cfg, text, log=print):
    """URL 또는 작품 번호로 등록. 반환: (ok, message, series_id)"""
    sid = kakao_api.parse_series_id(text)
    if not sid:
        return False, "카카오페이지 작품 URL(page.kakao.com/content/숫자) 또는 작품 번호를 입력하세요.", None
    existing = ss.get_kakao_title(sid)
    session = build_session_from_cfg(cfg)
    info = kakao_api.fetch_series_info(session, sid)
    if not info.get("title") and not existing:
        return False, "작품 정보를 찾지 못했습니다(series_id=%s). 번호를 확인해주세요." % sid, None
    if _is_novel(info) and not novel_enabled(cfg):
        return False, ("'%s'은(는) 카카오페이지 웹소설입니다. [설정] > [카카오페이지]에서 "
                       "'카카오 웹소설 사용'을 켜야 등록할 수 있습니다." % info.get("title", sid)), None
    patch = _info_patch(info)
    if existing and patch.get("title") and patch["title"] != existing.get("title"):
        _migrate_title_folder(series_root(cfg, existing), existing.get("title"), patch["title"], sid, log=log)
    patch.setdefault("title", (existing or {}).get("title") or sid)
    patch.update({"subscribed": True, "unsubscribed": False, "excluded": False,
                  "last_seen_at": time.time()})
    if not existing:
        patch.setdefault("weekdays", [])
        patch["added_at"] = time.time()
        patch["last_downloaded_no"] = None
    ss.upsert_kakao_title({sid: patch})
    log("카카오페이지 작품 등록: %s (series_id=%s)" % (patch.get("title"), sid))
    return True, "등록됨: %s" % patch.get("title"), sid


# ---------------------------------------------------------------------------
# 다운로드
# ---------------------------------------------------------------------------
def _existing_nos(series_dir):
    nos = set()
    try:
        for f in os.listdir(series_dir):
            m = kavita_yaml._EP_NO_RE.search(f)
            low = f.lower()
            if m and ((low.endswith(".zip") and "#" in f) or low.endswith(".epub")):
                nos.add(int(m.group(1)))
    except OSError:
        pass
    return nos


def download_series(cfg, session, sid, log=print, full=False, cancel_check=None,
                    on_progress=None, only_nos=None, force=False):
    """작품 하나의 볼 수 있는 미보유 회차를 받는다.
    full=True(수동 '지금 다운로드'): 미보유 회차를 전부 시도.
    full=False(자동): 새 회차 + 이미 대여/소장 표시된 회차 + (옵션)기다무만 시도.
    반환 dict: downloaded, locked, failures(list), auth_expired, cancelled, waitfree_used"""
    res = {"downloaded": 0, "locked": 0, "failures": [], "auth_expired": False,
           "cancelled": False, "waitfree_used": 0}
    t = dict(ss.get_kakao_title(sid) or {})
    if not t:
        return res

    # 작품 정보는 하루 한 번만 갱신(예전 버전에서 잘못 저장된 정보는 즉시 갱신)
    if (int(t.get("info_version") or 0) < INFO_VERSION or
            time.time() - float(t.get("info_fetched_at") or 0) > 24 * 3600):
        t = _refresh_info(cfg, session, sid, t, log=log)

    title = t.get("title") or sid
    is_novel = _is_novel(t)
    if is_novel and not novel_enabled(cfg):
        ss.upsert_kakao_title({sid: {"last_result": "웹소설 - [설정] > [카카오페이지] '카카오 웹소설 사용'이 꺼져 있어 받지 않음",
                                     "last_result_at": time.time()}})
        log("%s: 웹소설 사용이 꺼져 있어 건너뜀" % title)
        return res
    root = series_root(cfg, t)
    temp_root = kakao_temp_root(cfg)
    fz = _num(cfg, "FOLDER_ZERO_FILL", 4)

    from . import pipeline as _pl
    blocked = adult_block_reason(cfg, t) or (_pl.genre_block_msg(cfg, t) if _pl.bl_blocked(cfg, t) else None)
    if blocked:
        # 성인 작품인데 쿠키가 없거나, 지금 쿠키로 이미 성인 인증 실패를 확인한 작품 -
        # 회차 목록/이미지 요청 자체를 하지 않는다
        log("%s: %s - 건너뜀" % (title, blocked))
        ss.upsert_kakao_title({sid: {"last_result": blocked, "last_result_at": time.time()}})
        res["adult_blocked"] = True
        return res

    # 새 회차만 빠르게 확인할 수 있는 조건: 자동 실행 + 이미 전체 목록을 현재 번호 규칙으로
    # 본 적이 있음 + 앞쪽 회차를 볼 필요가 있는 이용권 옵션이 꺼져 있음 + 최근 일주일 안에 전체 확인함.
    # 이 경우 최신 순으로 받다가 이미 아는 회차에서 멈춘다(대부분 요청 1번).
    incremental = (not full and t.get("ep_numbering") == EP_NUMBERING and t.get("max_order_seen")
                   and not cfg.get("KAKAO_USE_WAITFREE") and not cfg.get("KAKAO_USE_OWNED_TICKETS")
                   and time.time() - float(t.get("last_full_list_at") or 0) < INCREMENTAL_FULL_EVERY)
    try:
        if incremental:
            episodes = kakao_api.fetch_episode_list(
                session, sid, log=log, series_title=title,
                newer_than_order=int(t["max_order_seen"]), max_regular_no=t.get("max_regular_no"))
        else:
            episodes = kakao_api.fetch_episode_list(session, sid, log=log, series_title=title)
    except kakao_api.KakaoAuthExpired as e:
        log("%s: %s" % (title, e))
        res["auth_expired"] = True
        ss.upsert_kakao_title({sid: {"last_result": "쿠키 만료로 회차 목록 조회 실패",
                                     "last_result_at": time.time()}})
        return res
    except Exception as e:  # noqa: BLE001
        log("%s: 회차 목록 조회 실패 - %s" % (title, e))
        res["failures"].append({"title": title, "title_id": sid, "episode_no": None,
                                "error": "회차 목록 조회 실패: %s" % e})
        ss.upsert_kakao_title({sid: {"last_result": "회차 목록 조회 실패: %s" % e,
                                     "last_result_at": time.time()}})
        return res
    if incremental and not episodes:
        ss.upsert_kakao_title({sid: {"last_result": "새 회차 없음", "last_result_at": time.time(),
                                     "last_checked_at": time.time(),
                                     "checked_slide_dt": t.get("last_slide_added_dt") or ""}})
        return res
    if not episodes:
        log("%s: 회차 목록이 비어 있음" % title)
        ss.upsert_kakao_title({sid: {"last_result": "회차 목록이 비어 있음", "last_result_at": time.time()}})
        return res

    # 이번에 본 최대 순번/본편 최대 번호 기록(다음 빠른 확인의 기준)
    seen_patch = {"max_order_seen": max([ep["order"] for ep in episodes] + [int(t.get("max_order_seen") or 0)])}
    regular = [ep["no"] for ep in episodes if ep["no"] < kakao_api.SPECIAL_EP_BASE]
    if regular:
        seen_patch["max_regular_no"] = max(regular + [int(t.get("max_regular_no") or 0)])
    if not incremental:
        seen_patch["last_full_list_at"] = time.time()
    ss.upsert_kakao_title({sid: seen_patch})
    t.update(seen_patch)

    if t.get("ep_numbering") != EP_NUMBERING:
        old_map = kakao_api.legacy_numbers_v1(episodes) if t.get("ep_numbering") == "subtitle" else None
        _renumber_existing_files(root, title, sid, episodes,
                                 _num(cfg, "FOLDER_ZERO_FILL", 4), log=log, old_map=old_map)
        done = _existing_nos(downloader.title_dir(root, title, sid))
        real = [n for n in done if n < kakao_api.SPECIAL_EP_BASE]
        renum = {"ep_numbering": EP_NUMBERING, "checked_no": 0,
                 "last_downloaded_no": max(real) if real else None}
        ss.upsert_kakao_title({sid: renum})
        t.update(renum)

    rd = kavita_yaml.release_date_from_episodes(episodes) if not incremental else ""
    patch = {"last_checked_at": time.time()}
    if not incremental:
        patch["episode_count"] = len(episodes)
    else:
        patch["episode_count"] = max(int(t.get("episode_count") or 0), int(t.get("max_order_seen") or 0))
    if rd and (not t.get("release_date") or rd < t["release_date"]):
        patch["release_date"] = rd
    ss.upsert_kakao_title({sid: patch})
    t.update(patch)

    series_dir = downloader.title_dir(root, title, sid)
    have = _existing_nos(series_dir)
    checked_no = int(t.get("checked_no") or 0)
    use_waitfree = bool(cfg.get("KAKAO_USE_WAITFREE", False))

    # 회차 목록 API가 회차마다 무료(is_free)/대여·소장(purchase_info) 여부를 알려주므로
    # 볼 수 있는 회차만 요청한다. 유료 구간을 회차마다 끝까지 두들기던 문제 수정.
    # - 무료 또는 이미 대여/소장한 회차: 받음
    # - 그 외(유료): 요청하지 않음. 단, 기다무 대여권 사용이 켜져 있으면 가장 앞
    #   유료 회차 1개만 대여권으로 시도
    # - 예전에 "받을 수 없는 회차"(동영상 트레일러 등)로 확인된 회차는 다시 시도 안 함
    skip_pids = set(t.get("skip_pids") or [])
    has_cookie = bool((cfg.get("KAKAO_COOKIE") or "").strip())
    # 처음 구독한 작품은 "최신 N화만 받기" 설정에 따라 볼 수 있는 회차 중 최신 N개만 받고,
    # 그 기준 순번(min_order)을 기억해 이후 실행에서도 그 이전 회차는 받지 않는다.
    # 카드의 "다운로드"(full)나 "다시 확인"은 이 제한을 무시하고 전부 받는다.
    initial_n = _num(cfg, "INITIAL_EPISODES_LIMIT", 0)
    min_order = None if full else t.get("min_order")
    if (not full and initial_n > 0 and min_order is None and t.get("last_downloaded_no") is None
            and not have):
        acc = [ep for ep in episodes if (ep["free"] or ep["rented"]) and ep.get("product_id")
               and ep["product_id"] not in skip_pids]
        if len(acc) > initial_n:
            min_order = acc[-initial_n].get("order", acc[-initial_n]["no"])
            ss.upsert_kakao_title({sid: {"min_order": min_order}})
            log("%s: 처음 구독한 작품이라 볼 수 있는 %d화 중 최신 %d화만 받음" % (title, len(acc), initial_n))
    targets, locked_eps = [], []
    for ep in episodes:
        if ep["no"] in have or not ep.get("product_id") or ep["product_id"] in skip_pids:
            continue
        if min_order is not None and ep.get("order", ep["no"]) < min_order:
            continue
        if ep["free"] or ep["rented"]:
            targets.append(ep)
        else:
            locked_eps.append(ep)
    wait_ep = None
    if use_waitfree and has_cookie:
        wait_ep = next((ep for ep in locked_eps if ep["waitfree_ok"]), None)
        if wait_ep:
            targets.append(wait_ep)
            targets.sort(key=lambda e: e.get("order", e["no"]))
    # 보유 대여권(이벤트/선물/쿠폰 등) 자동 사용 - 기본은 무료로 받은 대여권만,
    # "구매한 대여권도 사용"을 켜면 돈으로 산 대여권까지. 작품마다 보유 개수를 조회해
    # 그 수만큼 앞 회차부터 사용한다(보유 0장이면 유료 회차 요청 자체를 안 함).
    owned = {}
    ticket_eps = []
    allowed_types = list(kakao_api.FREE_RENT_TICKETS)
    if cfg.get("KAKAO_USE_PAID_TICKETS"):
        allowed_types += list(kakao_api.PAID_RENT_TICKETS)
    rest_locked = [ep for ep in locked_eps if ep is not wait_ep]
    if cfg.get("KAKAO_USE_OWNED_TICKETS") and has_cookie and rest_locked:
        try:
            counts, raw = kakao_api.fetch_my_tickets(session, sid)
            owned = {k: v for k, v in counts.items() if k in allowed_types}
            if owned:
                log("%s: 보유 대여권 %s" % (title, ", ".join(
                    "%s %d장" % (kakao_api.TICKET_NAMES.get(k, k), v) for k, v in owned.items())))
            elif counts:
                log("%s: 보유 이용권 %s - 사용 허용 대상 아님(설정 확인)" % (title, counts))
            elif raw and (raw.get("result_code") not in (0, "0", None)):
                log("%s: 대여권 조회 응답 - %s" % (title, json.dumps(raw, ensure_ascii=False)[:300]))
        except kakao_api.KakaoAuthExpired:
            raise
        except Exception as e:  # noqa: BLE001
            log("%s: 보유 대여권 조회 실패(무시) - %s" % (title, e))
        n_tickets = sum(owned.values())
        cap_t = _num(cfg, "MAX_NEW_EPISODES_PER_TITLE", 10) if not full else 0
        if cap_t:
            n_tickets = min(n_tickets, cap_t)
        for ep in rest_locked[:n_tickets]:
            ep["use_owned"] = True
            ticket_eps.append(ep)
        if ticket_eps:
            targets.extend(ticket_eps)
            targets.sort(key=lambda e: e.get("order", e["no"]))
    if only_nos:
        # [선택 회차 다운로드]: 고른 회차만. force면 이미 받은 회차도 지우고 다시 받는다.
        # 유료 표시 회차도 고르면 한 번 시도한다(앱에서 방금 대여했을 수 있음).
        only = set(int(n) for n in only_nos)
        targets = [ep for ep in episodes if ep["no"] in only and ep.get("product_id")
                   and (force or ep["no"] not in have)]
        locked_eps, ticket_eps, wait_ep = [], [], None
        ticket_available = False
    res["locked"] = len(locked_eps) - (1 if wait_ep else 0) - len(ticket_eps)
    res["paid_total"] = len(locked_eps)
    res["tickets_used"] = 0

    def _update_yaml():
        # 회차 zip이 하나라도 있으면 kavita.yaml 생성/갱신(내용이 같으면 파일은 그대로)
        if cfg.get("GENERATE_KAVITA_YAML", True):
            kavita_yaml.write_kavita_yaml(
                root, sid, session=session,
                embed_cover=bool(cfg.get("KAVITA_YAML_EMBED_COVER", True)),
                log=log, platform=yaml_platform(t))

    if not targets:
        msg = "받을 회차 없음(보유 %d화" % len(have)
        if locked_eps:
            msg += ", 유료 %d화는 건너뜀" % len(locked_eps)
        msg += ")"
        ss.upsert_kakao_title({sid: {"last_result": msg,
                                     "last_result_at": time.time(), "has_locked": bool(locked_eps),
                                     "checked_slide_dt": t.get("last_slide_added_dt") or ""}})
        # 예전에 받아둔 회차만 있고 kavita.yaml이 없던 작품도 여기서 만들어진다
        _update_yaml()
        return res
    log("%s: 받을 회차 %d개 (보유 %d / 유료라 건너뜀 %d / 전체 %d)%s" % (
        title, len(targets), len(have), res["locked"], len(episodes),
        (" + 기다무 대여권 1회 시도(%d화)" % wait_ep["no"] if wait_ep else "") +
        (" + 보유 대여권 %d장 사용 예정" % len(ticket_eps) if ticket_eps else "")))

    if cfg.get("GENERATE_SERIES_JSON", True):
        downloader.write_series_json(root, title, sid, _series_json_meta(t, sid), log=log)

    comicinfo_on = bool(cfg.get("GENERATE_COMICINFO_XML", True))
    cover_url = (t.get("cover_url") or t.get("thumbnail")) if cfg.get("ADD_COVER_AS_FIRST_PAGE", True) else None
    last_ok = t.get("last_downloaded_no")
    max_checked = checked_no
    locked_run = 0
    ticket_available = use_waitfree
    # 자동 실행은 작품당 1회 실행에 받는 회차 수를 네이버와 같은 설정으로 제한
    cap = 0 if full else _num(cfg, "MAX_NEW_EPISODES_PER_TITLE", 10)

    for i, ep in enumerate(targets):
        if cancel_check and cancel_check():
            res["cancelled"] = True
            break
        if cap and res["downloaded"] >= cap:
            log("%s: 이번 실행 상한(%d화) 도달 - 나머지는 다음 실행 때" % (title, cap))
            res["capped"] = True
            break
        if on_progress:
            on_progress(i, len(targets), "%s %d화" % (title, ep["no"]))
        no, pid = ep["no"], ep["product_id"]

        def _images(pid=pid):
            return kakao_api.fetch_episode_images(session, sid, pid)

        def _try_download():
            if is_novel:
                return _download_novel_episode(cfg, session, root, title, sid, ep, t, force=force, log=log)
            return downloader.download_episode(
                session, root, temp_root, title, sid, no,
                image_zero_fill=_num(cfg, "IMAGE_ZERO_FILL", 4), folder_zero_fill=fz,
                max_concurrent=_num(cfg, "MAX_CONCURRENT_DOWNLOADS", 5),
                delay_seconds=_num(cfg, "DELAY_SECONDS", 1.0),
                timeout=_num(cfg, "REQUEST_TIMEOUT_SECONDS", 15), log=log,
                image_list_func=_images, referer=kakao_api.BASE + "/", force=force)

        try:
            try:
                ok, skipped, cnt, err = _try_download()
            except kakao_api.KakaoNotPurchased:
                if ep is wait_ep and ticket_available:
                    log("%s %d화: 기다무 대여권 사용 시도" % (title, no))
                    if not kakao_api.use_waitfree_ticket(session, pid):
                        ticket_available = False  # 대기 시간 미충족 등 - 이번 실행엔 더 시도 안 함
                        raise
                    ticket_available = False       # 작품당 실행 1회에 1장
                    res["waitfree_used"] += 1
                elif ep.get("use_owned") and any(v > 0 for v in owned.values()):
                    try:
                        ready, _raw = kakao_api.ready_ticket_types(session, pid)
                    except kakao_api.KakaoAuthExpired:
                        raise
                    except Exception:  # noqa: BLE001
                        ready = []
                    choice = next((tt for tt in allowed_types if owned.get(tt, 0) > 0
                                   and (not ready or tt in ready)), None)
                    if not choice:
                        owned.clear()   # 이 회차에 쓸 수 있는 보유 대여권 없음 - 이후도 시도 안 함
                        raise
                    log("%s %d화: 보유 %s 사용" % (title, no, kakao_api.TICKET_NAMES.get(choice, choice)))
                    if not kakao_api.use_ticket(session, pid, choice):
                        log("%s %d화: %s 사용 실패 - 이 종류는 이번 실행에서 더 쓰지 않음" % (
                            title, no, kakao_api.TICKET_NAMES.get(choice, choice)))
                        owned.pop(choice, None)
                        raise
                    owned[choice] -= 1
                    res["tickets_used"] += 1
                else:
                    raise
                ok, skipped, cnt, err = _try_download()
        except kakao_api.KakaoAuthExpired as e:
            log("%s: %s" % (title, e))
            res["auth_expired"] = True
            break
        except kakao_api.KakaoAdultRequired as e:
            reason = ("성인 인증 필요 - 성인 인증된 카카오 계정의 로그인 쿠키가 있어야 받을 수 있음"
                      if not has_cookie else
                      "성인 인증 실패 - 현재 쿠키의 계정이 성인 인증되지 않았거나 쿠키 만료")
            log("%s: %s (%s) - 이 작품은 중단" % (title, reason, e))
            ss.upsert_kakao_title({sid: {"adult_block_hash": kakao_api._cookie_hash(cfg.get("KAKAO_COOKIE"))}})
            res["adult_blocked"] = True
            res["adult_reason"] = reason
            break
        except kakao_api.KakaoSkip as e:
            log("%s %s(%s): 받을 수 없는 회차(동영상 트레일러 등)라 건너뜀, 다음부터 시도 안 함 - %s" % (
                title, _ep_label(no), ep.get("subtitle", ""), e))
            res["skipped"] = res.get("skipped", 0) + 1
            skip_pids.add(ep["product_id"])
            ss.upsert_kakao_title({sid: {"skip_pids": sorted(skip_pids)}})
            max_checked = max(max_checked, ep.get("order", no))
            continue
        except kakao_api.KakaoUnsupported as e:
            log("%s: %s - 이 작품은 중단" % (title, e))
            res["failures"].append({"title": title, "title_id": sid, "episode_no": no, "error": str(e)})
            break
        except kakao_api.KakaoNotPurchased:
            res["locked"] += 1
            locked_run += 1
            max_checked = max(max_checked, ep.get("order", no))
            if locked_run >= (MAX_CONSECUTIVE_LOCKED if not full else 20):
                log("%s: 볼 수 없는 회차가 연속 %d개 - 이번 실행은 여기까지" % (title, locked_run))
                break
            continue
        except Exception as e:  # noqa: BLE001
            res["failures"].append({"title": title, "title_id": sid, "episode_no": no, "error": str(e)})
            log("%s %d화 실패: %s" % (title, no, e))
            continue

        locked_run = 0
        max_checked = max(max_checked, ep.get("order", no))
        if not ok:
            res["failures"].append({"title": title, "title_id": sid, "episode_no": no, "error": err})
            log("%s %d화 실패: %s" % (title, no, err))
            continue

        if is_novel or skipped:
            c_ok, c_msg = True, ""     # 웹소설은 EPUB을 바로 만들어 저장함(압축 단계 없음)
        else:
            c_ok, _c_path, c_msg = downloader.compress_episode(
                root, temp_root, title, sid, no, folder_zero_fill=fz, log=log,
                zip_stored=bool(cfg.get("ZIP_STORED", True)), session=session, cover_url=cover_url,
                comicinfo_meta=_comicinfo_meta(t, ep, sid) if comicinfo_on else None)
        if not c_ok:
            res["failures"].append({"title": title, "title_id": sid, "episode_no": no,
                                    "error": "압축 실패: %s" % c_msg})
            continue
        res["downloaded"] += 1
        if no >= kakao_api.SPECIAL_EP_BASE:
            log("%s %s 완료 (%d장) - 회차 제목 '%s'에 'N화' 번호가 없어 특별 회차 번호 %d로 저장" % (
                title, _ep_label(no), cnt, ep.get("subtitle", ""), no))
            _record_special(series_dir, title, no, ep.get("subtitle", ""))
        elif is_novel:
            log("%s %d화 EPUB 저장 (%d단어)" % (title, no, cnt))
        else:
            log("%s %d화 완료 (%d장)" % (title, no, cnt))
        # 예전엔 회차 하나 받을 때마다 갱신(표지 다운로드 포함)해서 너무 잦았다.
        # 이제는 작품 처리가 끝날 때 1번 + 긴 다운로드 대비 20화마다 1번만.
        if res["downloaded"] % 20 == 0:
            _update_yaml()
        ss.append_history({"type": "download",
                           "source": "manual" if (full or only_nos) else "auto",
                           "platform": yaml_platform(t),
                           "title_id": sid, "title": title, "episode_no": no,
                           "subtitle": ep.get("subtitle"), "image_count": cnt,
                           "unit": "words" if is_novel else "images"})
        if no < kakao_api.SPECIAL_EP_BASE and (last_ok is None or no > last_ok):
            last_ok = no

    summary = "신규 %d화" % res["downloaded"]
    if res.get("adult_blocked"):
        summary = res.get("adult_reason") or "성인 인증 필요"
    if res.get("tickets_used"):
        summary += " / 보유 대여권 %d장 사용" % res["tickets_used"]
    if res["locked"]:
        summary += " / 유료라 건너뜀 %d화" % res["locked"]
    if res["failures"]:
        summary += " / 실패 %d" % len(res["failures"])
    if res["auth_expired"]:
        summary += " / 쿠키 만료"
    done_patch = {"last_downloaded_no": last_ok, "checked_no": max_checked,
                  "last_result": summary, "last_result_at": time.time(),
                  "has_locked": bool(res["locked"])}
    if res["waitfree_used"]:
        done_patch["last_ticket_at"] = time.time()
    if not res.get("capped") and not res["cancelled"] and not res["auth_expired"]:
        # 이번에 끝까지 확인했으니, 새 회차가 올라오기 전까지는 다시 안 봐도 됨
        done_patch["checked_slide_dt"] = t.get("last_slide_added_dt") or ""
    ss.upsert_kakao_title({sid: done_patch})

    _update_yaml()   # 작품 정보(완결 등)만 바뀐 경우도 반영
    return res


def run_kakao_cycle(cfg, log=print, manage_job=False):
    """구독 중인 카카오페이지 작품 전체를 확인한다(자동 사이클/'카카오 전체 실행')."""
    total = {"downloaded": 0, "locked": 0, "failures": [], "waitfree_used": 0,
             "auth_expired": False, "cancelled": False}
    try:
        downloader.flush_outbox(kakao_temp_root(cfg), log=log)
    except Exception as e:  # noqa: BLE001
        log("카카오: 남은 zip 정리 실패(무시): %s" % e)
    if not (cfg.get("KAKAO_COOKIE") or "").strip():
        log("카카오페이지: 로그인 쿠키가 없어 무료 회차만 시도합니다.")
    titles = {k: v for k, v in ss.load_kakao_titles().items()
              if v.get("subscribed") and not v.get("excluded") and not v.get("unsubscribed")
              and (not _is_novel(v) or novel_enabled(cfg))}
    if not titles:
        log("카카오페이지: 구독 중인 작품 없음")
        if manage_job:
            ss.save_job_state({"running": False, "stage": "done", "finished_at": time.time(),
                                "message": "카카오페이지: 구독 중인 작품 없음"})
        return total

    session = build_session_from_cfg(cfg)
    try:
        kakao_api.refresh_token(session, log=log)
    except kakao_api.KakaoAuthExpired as e:
        log("카카오페이지: %s" % e)
        total["auth_expired"] = True

    now = time.time()
    all_count = len(titles)
    from . import pipeline as _pl
    titles = {k: v for k, v in titles.items()
              if _pl.in_new_episode_scope(cfg, v, "kakao", now) and _needs_check(cfg, v, now)
              and not adult_block_reason(cfg, v) and not _pl.bl_blocked(cfg, v)}
    titles = _pl.filter_auto_targets(cfg, titles, "kakao", log=log, label="카카오페이지: ")
    log("카카오페이지: 구독 %d개 중 이번에 확인할 작품 %d개(오늘 요일·기다무 중 새 회차/대여권 충전된 작품만)" %
        (all_count, len(titles)))
    items = sorted(titles.items(), key=lambda kv: str(kv[1].get("title") or ""))
    lock = threading.Lock()
    tls = threading.local()
    done = {"n": 0}

    def _stop():
        return bool(ss.load_job_state().get("cancel_requested")) or total["auth_expired"]

    def _one(sid, t):
        if _stop():
            total["cancelled"] = total["cancelled"] or bool(ss.load_job_state().get("cancel_requested"))
            return
        # 작업 스레드마다 별도 세션(토큰 연장된 쿠키는 위에서 저장해 둔 파일을 같이 씀)
        if getattr(tls, "session", None) is None:
            tls.session = build_session_from_cfg(cfg)
            if cfg.get("LOW_PRIORITY_MODE", True):
                downloader.lower_thread_priority(int(cfg.get("DOWNLOAD_NICE_LEVEL", 10)))
        with lock:
            done["n"] += 1
            n = done["n"]
        label = platform_label(t, with_kind=True)

        def _st(i, m, text, n=n, label=label):
            msg = "%s %s (%d/%d화)" % (label, text, i + 1, m) if m else "%s %s" % (label, text)
            ss.save_job_state({"kakao": {"msg": msg, "done": n, "total": len(items), "running": True}})

        _st(0, 0, "%s - 새 회차 확인 중" % t.get("title", sid))
        r = download_series(cfg, tls.session, sid, log=log, cancel_check=_stop,
                            on_progress=lambda i, m, text: _st(i, m, text + " 받는 중"))
        with lock:
            for k in ("downloaded", "locked", "waitfree_used"):
                total[k] += r[k]
            total["failures"].extend(r["failures"])
            if r["cancelled"]:
                total["cancelled"] = True
            if r["auth_expired"]:
                total["auth_expired"] = True

    workers = max(1, min(10, _num(cfg, "PARALLEL_TITLES", 2)))
    try:
        if workers == 1:
            for sid, t in items:
                _one(sid, t)
                if total["cancelled"] or total["auth_expired"]:
                    break
        else:
            log("카카오페이지: 작품 %d개를 동시에 처리합니다" % workers)
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="wtm_kakao") as ex:
                futs = [ex.submit(_one, sid, t) for sid, t in items]
                for f in as_completed(futs):
                    try:
                        f.result()
                    except Exception as e:  # noqa: BLE001
                        log("카카오 작품 처리 중 오류(다음 작품은 계속): %s" % e)
    finally:
        kakao_api.save_session_cookies(session)
        ss.save_job_state({"kakao": None})

    # 쿠키 만료 알림은 만료가 처음 감지됐을 때 1번만 보내고, 정상으로 돌아오면
    # 다음 만료 때 다시 1번 보낸다(매 사이클마다 반복 알림 방지)
    already = bool(ss.load_job_state().get("kakao_cookie_expired_notified"))
    ss.save_job_state({"kakao_cookie_expired_notified": bool(total["auth_expired"])})
    if total["auth_expired"] and not already:
        discord_notify.notify(cfg, "🍪 카카오페이지 쿠키 만료",
                              "카카오페이지 로그인 쿠키가 만료된 것으로 보입니다. "
                              "플러그인 설정에서 쿠키를 새로 넣어주세요.",
                              color=discord_notify.COLOR_WARN)
    if total["failures"]:
        discord_notify.notify_failures(cfg, total["failures"])

    msg = "카카오페이지 완료: 신규 %d화 / 기다무 %d장 사용 / 실패 %d건%s%s" % (
        total["downloaded"], total["waitfree_used"], len(total["failures"]),
        " / 쿠키 만료" if total["auth_expired"] else "",
        " (도중 취소됨)" if total["cancelled"] else "")
    log(msg)
    if manage_job:
        ss.save_job_state({"running": False, "finished_at": time.time(), "message": msg,
                            "stage": "cancelled" if total["cancelled"] else "done"})
    return total


def lookup_episodes(cfg, sid):
    """[선택 회차 다운로드] 탭용 회차 목록(받음/유료 표시 포함)."""
    t = ss.get_kakao_title(str(sid)) or {}
    session = build_session_from_cfg(cfg)
    if not t.get("title"):
        info = kakao_api.fetch_series_info(session, sid)
        t = dict(t, **_info_patch(info)) if info else t
    title = t.get("title") or str(sid)
    eps = kakao_api.fetch_episode_list(session, sid, series_title=title)
    have = _existing_nos(downloader.title_dir(series_root(cfg, t), title, sid))
    out = []
    for ep in eps:
        out.append({
            "no": ep["no"], "order": ep.get("order"), "subtitle": ep.get("subtitle", ""),
            "charge": not (ep.get("free") or ep.get("rented")),
            "rented": bool(ep.get("rented")), "downloaded": ep["no"] in have,
            "special": ep["no"] >= kakao_api.SPECIAL_EP_BASE,
        })
    return {"titleId": str(sid), "title": title, "platform": "kakao", "episodes": out}


def sync_purchased(cfg, log=print, manage_job=False):
    """카카오페이지 보관함 > 구매 목록의 작품을 자동으로 목록에 넣고, 구독 여부와 상관없이
    볼 수 있는 회차(구매·대여한 회차 + 무료 회차)를 받는다. 로그인 쿠키 필요."""
    res = {"series": 0, "added": 0, "downloaded": 0, "auth_expired": False}
    if not (cfg.get("KAKAO_COOKIE") or "").strip():
        log("카카오 구매 작품 동기화: 로그인 쿠키가 없어 건너뜀")
        return res
    session = build_session_from_cfg(cfg)
    try:
        kakao_api.refresh_token(session, log=log)
        bought = kakao_api.fetch_purchased_series(session, log=log)
    except kakao_api.KakaoAuthExpired as e:
        log("카카오 구매 작품 동기화: %s" % e)
        res["auth_expired"] = True
        return res
    except Exception as e:  # noqa: BLE001
        log("카카오 구매 목록 조회 실패: %s" % e)
        return res
    res["series"] = len(bought)
    log("카카오 구매 작품 %d개 확인" % len(bought))
    titles = ss.load_kakao_titles()
    from . import pipeline as _pl
    for i, b in enumerate(bought):
        if ss.load_job_state().get("cancel_requested"):
            break
        sid = b["series_id"]
        if sid not in titles:
            ok, msg, _sid = add_series(cfg, sid, log=log)
            if not ok:
                log("구매 작품 등록 건너뜀(%s): %s" % (b.get("title") or sid, msg))
                continue
            # 구매 작품은 자동 구독하지 않는다(구매 회차만 받으면 되므로) - 표시만
            ss.upsert_kakao_title({sid: {"subscribed": False, "purchased": True}})
            res["added"] += 1
        else:
            ss.upsert_kakao_title({sid: {"purchased": True}})
        t = ss.get_kakao_title(sid) or {}
        if _is_novel(t) and not novel_enabled(cfg):
            continue
        if _pl.bl_blocked(cfg, t):
            log("구매 작품 %s: %s" % (t.get("title", sid), _pl.genre_block_msg(cfg, t)))
            continue
        if manage_job:
            ss.save_job_state({"progress": i + 1, "total": len(bought),
                                "message": "카카오 구매 작품: %s" % t.get("title", sid)})
        r = download_series(cfg, session, sid, log=log, full=True,
                            cancel_check=lambda: ss.load_job_state().get("cancel_requested"))
        res["downloaded"] += r.get("downloaded", 0)
        if r.get("auth_expired"):
            res["auth_expired"] = True
            break
    kakao_api.save_session_cookies(session)
    ss.save_job_state({"last_kakao_purchase_sync_at": time.time()})
    msg = "카카오 구매 작품 동기화 완료: 작품 %d개(새로 등록 %d) / 신규 %d화" % (
        res["series"], res["added"], res["downloaded"])
    log(msg)
    if manage_job:
        ss.save_job_state({"running": False, "stage": "done", "finished_at": time.time(), "message": msg})
    return res


def run_kakao_series_job(cfg, sid, log=print, only_nos=None, force=False):
    """작품 하나 '지금 다운로드'(title_job 상태 사용). 미보유 회차 전부 시도."""
    t = ss.get_kakao_title(sid) or {}
    log("카카오 다운로드 시작: %s (series_id=%s) / 저장 경로 %s / 로그인 쿠키 %s" % (
        t.get("title", sid), sid, series_root(cfg, t),
        "있음" if (cfg.get("KAKAO_COOKIE") or "").strip() else "없음(무료 회차만)"))
    session = build_session_from_cfg(cfg)
    try:
        kakao_api.refresh_token(session, log=log)
    except kakao_api.KakaoAuthExpired as e:
        log("카카오페이지: %s" % e)

    label = platform_label(t, with_kind=True)

    def _progress(i, n, text):
        ss.save_title_job_state({"progress": i, "total": n,
                                  "message": "%s %s 받는 중 (%d/%d화)" % (label, text, i + 1, n)})

    try:
        r = download_series(cfg, session, sid, log=log, full=True,
                            cancel_check=lambda: ss.load_title_job_state().get("cancel_requested"),
                            on_progress=_progress, only_nos=only_nos, force=force)
    finally:
        kakao_api.save_session_cookies(session)
    msg = "카카오 %s: 신규 %d화 / 볼 수 없는 회차 %d / 실패 %d%s" % (
        t.get("title", sid), r["downloaded"], r["locked"], len(r["failures"]),
        " / 쿠키 만료" if r["auth_expired"] else "")
    if r["failures"]:
        msg += " - 첫 실패: %s" % (r["failures"][0].get("error") or "")[:150]
    if not r["downloaded"] and not r["failures"] and not r["locked"]:
        cur = ss.get_kakao_title(sid) or {}
        if cur.get("last_result"):
            msg += " (%s)" % cur["last_result"]
    log(msg)
    ss.save_title_job_state({"running": False, "finished_at": time.time(), "message": msg,
                              "last_error": "쿠키 만료" if r["auth_expired"] else None})
    return r
