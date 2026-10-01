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
import os
import time

from . import discord_notify, downloader, kakao_api, kavita_yaml, state_store as ss

PLATFORM = "kakao"
# 1.16.0은 작품 HTML에서 정보를 읽어 제목이 깨지고(문자셋 오판) 표지가 사이트
# 기본 로고로 저장됐다. 이 값보다 낮은 레코드는 다음 실행 때 API로 다시 채운다.
INFO_VERSION = 3
# 예약(자동) 실행에서 볼 수 없는 회차가 이만큼 연달아 나오면 그 작품은 멈춘다
# (유료 구간에서 회차마다 요청을 보내지 않기 위함).
MAX_CONSECUTIVE_LOCKED = 3


def _num(cfg, key, default):
    try:
        return type(default)(cfg.get(key, default))
    except (TypeError, ValueError):
        return default


def kakao_root(cfg):
    return (cfg.get("KAKAO_DOWNLOAD_ROOT") or "").strip() or ss.KAKAO_DOWNLOAD_DEFAULT_DIR


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
EP_NUMBERING = "subtitle"


def _renumber_existing_files(root, title, sid, episodes, folder_zero_fill=4, log=print):
    """예전(사이트 순번) 번호로 저장된 회차 파일을 회차 제목 번호로 이름 변경.
    번호끼리 겹칠 수 있으므로(2->1, 3->2 ...) 임시 이름을 거쳐 2단계로 바꾼다."""
    series_dir = downloader.title_dir(root, title, sid)
    if not os.path.isdir(series_dir):
        return 0
    zf = int(folder_zero_fill or 4)
    prefix = downloader.safe_name(title) + " "
    mapping = {ep["order"]: ep["no"] for ep in episodes if ep.get("order") is not None}
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
    return len(moves)


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


def _refresh_info(cfg, session, sid, t, log=print):
    info = kakao_api.fetch_series_info(session, sid)
    if not info:
        return t
    patch = _info_patch(info)
    if patch.get("title") and patch["title"] != t.get("title"):
        _migrate_title_folder(kakao_root(cfg), t.get("title"), patch["title"], sid, log=log)
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
            t = ss.load_kakao_titles().get(sid)
            if t:
                _refresh_info(cfg, session, sid, t, log=ss.append_log)
                time.sleep(0.3)

    import threading
    threading.Thread(target=_run, name="webtoon_manager_kakao_repair", daemon=True).start()


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
    ss.upsert_kakao_title(patch)
    return patch


def _apply_waitfree_autosubscribe(cfg, patch, log=print):
    """'기다무 자동 구독'이 켜져 있으면 연재 중인 기다무 작품을 구독으로 올린다.
    네이버 '매일+ 자동 구독'과 같은 규칙: 사용자가 구독/구독해제/제외를 한 번도
    고르지 않은(전부 기본값인) 작품만 건드린다. 완결작은 대상이 아니다."""
    if not cfg.get("KAKAO_AUTO_SUBSCRIBE_WAITFREE", True):
        return 0
    current = ss.load_kakao_titles()
    promote = {}
    for sid in patch:
        t = current.get(sid) or {}
        if not t.get("waitfree") or t.get("status") == "완결" or _is_novel(t):
            continue
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


def run_kakao_scan_weekday(cfg, log=print, should_cancel=None):
    """월~일(tab 1~7) + 신작(tab 11) 목록을 모아 반영한다."""
    session = build_session_from_cfg(cfg)
    merged = {}
    for tab, day in sorted(kakao_api.WEEKDAY_TABS.items()):
        if should_cancel and should_cancel():
            break
        ss.save_job_state({"message": "카카오페이지 %s요일 목록 수집 중" % "월화수목금토일"[tab - 1]})
        try:
            items = kakao_api.fetch_landing_all(session, tab_uid=tab, should_cancel=should_cancel)
        except Exception as e:  # noqa: BLE001
            log("카카오페이지 %s 목록 수집 실패: %s" % (day, e))
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
    if not (should_cancel and should_cancel()):
        ss.save_job_state({"message": "카카오페이지 신작 목록 수집 중"})
        try:
            for it in kakao_api.fetch_landing_all(session, tab_uid=kakao_api.TAB_NEW,
                                                  should_cancel=should_cancel):
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
    ss.save_job_state({"message": "카카오페이지 완결 목록 수집 중"})
    try:
        items = kakao_api.fetch_landing_all(session, tab_uid=kakao_api.TAB_FINISHED,
                                            max_pages=max_pages, should_cancel=should_cancel)
    except Exception as e:  # noqa: BLE001
        log("카카오페이지 완결 목록 수집 실패: %s" % e)
        return {"scanned": 0}
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
    existing = ss.load_kakao_titles().get(sid)
    session = build_session_from_cfg(cfg)
    info = kakao_api.fetch_series_info(session, sid)
    if not info.get("title") and not existing:
        return False, "작품 정보를 찾지 못했습니다(series_id=%s). 번호를 확인해주세요." % sid, None
    if _is_novel(info):
        return False, ("'%s'은(는) 카카오페이지 웹소설입니다. 이 플러그인은 이미지 웹툰만 받을 수 "
                       "있어 등록하지 않았습니다." % info.get("title", sid)), None
    patch = _info_patch(info)
    if existing and patch.get("title") and patch["title"] != existing.get("title"):
        _migrate_title_folder(kakao_root(cfg), existing.get("title"), patch["title"], sid, log=log)
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
            if m and f.lower().endswith(".zip") and "#" in f:
                nos.add(int(m.group(1)))
    except OSError:
        pass
    return nos


def download_series(cfg, session, sid, log=print, full=False, cancel_check=None,
                    on_progress=None):
    """작품 하나의 볼 수 있는 미보유 회차를 받는다.
    full=True(수동 '지금 다운로드'): 미보유 회차를 전부 시도.
    full=False(자동): 새 회차 + 이미 대여/소장 표시된 회차 + (옵션)기다무만 시도.
    반환 dict: downloaded, locked, failures(list), auth_expired, cancelled, waitfree_used"""
    res = {"downloaded": 0, "locked": 0, "failures": [], "auth_expired": False,
           "cancelled": False, "waitfree_used": 0}
    t = dict(ss.load_kakao_titles().get(sid) or {})
    if not t:
        return res

    # 작품 정보는 하루 한 번만 갱신(예전 버전에서 잘못 저장된 정보는 즉시 갱신)
    if (int(t.get("info_version") or 0) < INFO_VERSION or
            time.time() - float(t.get("info_fetched_at") or 0) > 24 * 3600):
        t = _refresh_info(cfg, session, sid, t, log=log)

    title = t.get("title") or sid
    if _is_novel(t):
        ss.upsert_kakao_title({sid: {"last_result": "웹소설이라 받을 수 없음(웹툰만 지원) - 삭제해주세요",
                                     "last_result_at": time.time(), "subscribed": False}})
        log("%s: 웹소설이라 건너뜀(이미지 웹툰만 지원)" % title)
        return res
    root = kakao_root(cfg)
    temp_root = kakao_temp_root(cfg)
    fz = _num(cfg, "FOLDER_ZERO_FILL", 4)

    try:
        episodes = kakao_api.fetch_episode_list(session, sid, log=log)
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
    if not episodes:
        log("%s: 회차 목록이 비어 있음" % title)
        ss.upsert_kakao_title({sid: {"last_result": "회차 목록이 비어 있음", "last_result_at": time.time()}})
        return res

    if t.get("ep_numbering") != EP_NUMBERING:
        _renumber_existing_files(kakao_root(cfg), title, sid, episodes,
                                 _num(cfg, "FOLDER_ZERO_FILL", 4), log=log)
        done = _existing_nos(downloader.title_dir(kakao_root(cfg), title, sid))
        real = [n for n in done if n < kakao_api.SPECIAL_EP_BASE]
        renum = {"ep_numbering": EP_NUMBERING, "checked_no": 0,
                 "last_downloaded_no": max(real) if real else None}
        ss.upsert_kakao_title({sid: renum})
        t.update(renum)

    rd = kavita_yaml.release_date_from_episodes(episodes)
    patch = {"episode_count": len(episodes), "last_checked_at": time.time()}
    if rd and (not t.get("release_date") or rd < t["release_date"]):
        patch["release_date"] = rd
    ss.upsert_kakao_title({sid: patch})
    t.update(patch)

    series_dir = downloader.title_dir(root, title, sid)
    have = _existing_nos(series_dir)
    checked_no = int(t.get("checked_no") or 0)
    use_waitfree = bool(cfg.get("KAKAO_USE_WAITFREE", False))

    targets = []
    for ep in episodes:
        if ep["no"] in have or not ep.get("product_id"):
            continue
        # checked_no는 사이트 순번(order) 기준 - 특수 회차(9000번대 파일 번호) 때문에
        # 뒤 회차가 "이미 확인함"으로 오판되지 않게 파일 번호와 분리한다
        if full or ep["rented"] or ep["free"] or ep.get("order", ep["no"]) > checked_no or \
                (use_waitfree and ep["waitfree_ok"]):
            targets.append(ep)

    def _update_yaml():
        # 회차 zip이 하나라도 있으면 kavita.yaml 생성/갱신(내용이 같으면 파일은 그대로)
        if cfg.get("GENERATE_KAVITA_YAML", True):
            kavita_yaml.write_kavita_yaml(
                root, sid, session=session,
                embed_cover=bool(cfg.get("KAVITA_YAML_EMBED_COVER", True)),
                log=log, platform=PLATFORM)

    if not targets:
        ss.upsert_kakao_title({sid: {"last_result": "받을 새 회차 없음(보유 %d화)" % len(have),
                                     "last_result_at": time.time(), "has_locked": False,
                                     "checked_slide_dt": t.get("last_slide_added_dt") or ""}})
        # 예전에 받아둔 회차만 있고 kavita.yaml이 없던 작품도 여기서 만들어진다
        _update_yaml()
        return res
    log("%s: 시도할 회차 %d개 (보유 %d / 전체 %d)" % (title, len(targets), len(have), len(episodes)))

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
            return downloader.download_episode(
                session, root, temp_root, title, sid, no,
                image_zero_fill=_num(cfg, "IMAGE_ZERO_FILL", 4), folder_zero_fill=fz,
                max_concurrent=_num(cfg, "MAX_CONCURRENT_DOWNLOADS", 5),
                delay_seconds=_num(cfg, "DELAY_SECONDS", 1.0),
                timeout=_num(cfg, "REQUEST_TIMEOUT_SECONDS", 15), log=log,
                image_list_func=_images, referer=kakao_api.BASE + "/")

        try:
            try:
                ok, skipped, cnt, err = _try_download()
            except kakao_api.KakaoNotPurchased:
                if not (ticket_available and ep["waitfree_ok"]):
                    raise
                log("%s %d화: 기다무 대여권 사용 시도" % (title, no))
                if not kakao_api.use_waitfree_ticket(session, pid):
                    ticket_available = False  # 대기 시간 미충족 등 - 이번 실행엔 더 시도 안 함
                    raise
                ticket_available = False       # 작품당 실행 1회에 1장
                res["waitfree_used"] += 1
                ok, skipped, cnt, err = _try_download()
        except kakao_api.KakaoAuthExpired as e:
            log("%s: %s" % (title, e))
            res["auth_expired"] = True
            break
        except kakao_api.KakaoSkip as e:
            log("%s %d화(%s): 받을 수 없는 회차라 건너뜀 - %s" % (title, no, ep.get("subtitle", ""), e))
            res["skipped"] = res.get("skipped", 0) + 1
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

        c_ok, _c_path, c_msg = downloader.compress_episode(
            root, temp_root, title, sid, no, folder_zero_fill=fz, log=log,
            zip_stored=bool(cfg.get("ZIP_STORED", True)), session=session, cover_url=cover_url,
            comicinfo_meta=_comicinfo_meta(t, ep, sid) if comicinfo_on else None)
        if not c_ok:
            res["failures"].append({"title": title, "title_id": sid, "episode_no": no,
                                    "error": "압축 실패: %s" % c_msg})
            continue
        res["downloaded"] += 1
        log("%s %d화 완료 (%d장)" % (title, no, cnt))
        # 회차가 추가될 때마다 바로 갱신 - 긴 다운로드가 중간에 끊겨도(재시작 등)
        # 이미 받은 회차는 kavita.yaml에 반영돼 있도록
        _update_yaml()
        ss.append_history({"type": "download", "source": "kakao", "platform": "kakao",
                           "title_id": sid, "title": "[카카오] %s" % title, "episode_no": no,
                           "subtitle": ep.get("subtitle"), "image_count": cnt})
        if no < kakao_api.SPECIAL_EP_BASE and (last_ok is None or no > last_ok):
            last_ok = no

    summary = "신규 %d화" % res["downloaded"]
    if res["locked"]:
        summary += " / 볼 수 없는 회차 %d" % res["locked"]
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
    if not (cfg.get("KAKAO_COOKIE") or "").strip():
        log("카카오페이지: 로그인 쿠키가 없어 무료 회차만 시도합니다.")
    titles = {k: v for k, v in ss.load_kakao_titles().items()
              if v.get("subscribed") and not v.get("excluded") and not v.get("unsubscribed")
              and not _is_novel(v)}
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
    titles = {k: v for k, v in titles.items() if _needs_check(cfg, v, now)}
    log("카카오페이지: 구독 %d개 중 이번에 확인할 작품 %d개(새 회차/기다무 충전된 작품만)" %
        (all_count, len(titles)))
    items = sorted(titles.items(), key=lambda kv: str(kv[1].get("title") or ""))
    try:
        for i, (sid, t) in enumerate(items):
            if ss.load_job_state().get("cancel_requested"):
                total["cancelled"] = True
                break
            ss.save_job_state({"stage": "kakao", "progress": i, "total": len(items),
                                "message": "카카오페이지: %s" % t.get("title", sid)})
            r = download_series(cfg, session, sid, log=log,
                                cancel_check=lambda: ss.load_job_state().get("cancel_requested"))
            for k in ("downloaded", "locked", "waitfree_used"):
                total[k] += r[k]
            total["failures"].extend(r["failures"])
            if r["cancelled"]:
                total["cancelled"] = True
                break
            if r["auth_expired"]:
                total["auth_expired"] = True
                break
            time.sleep(0.5)
    finally:
        kakao_api.save_session_cookies(session)

    if total["auth_expired"]:
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


def run_kakao_series_job(cfg, sid, log=print):
    """작품 하나 '지금 다운로드'(title_job 상태 사용). 미보유 회차 전부 시도."""
    t = ss.load_kakao_titles().get(sid) or {}
    log("카카오 다운로드 시작: %s (series_id=%s) / 저장 경로 %s / 로그인 쿠키 %s" % (
        t.get("title", sid), sid, kakao_root(cfg),
        "있음" if (cfg.get("KAKAO_COOKIE") or "").strip() else "없음(무료 회차만)"))
    session = build_session_from_cfg(cfg)
    try:
        kakao_api.refresh_token(session, log=log)
    except kakao_api.KakaoAuthExpired as e:
        log("카카오페이지: %s" % e)

    def _progress(i, n, text):
        ss.save_title_job_state({"progress": i, "total": n, "message": "카카오: %s" % text})

    try:
        r = download_series(cfg, session, sid, log=log, full=True,
                            cancel_check=lambda: ss.load_title_job_state().get("cancel_requested"),
                            on_progress=_progress)
    finally:
        kakao_api.save_session_cookies(session)
    msg = "카카오 %s: 신규 %d화 / 볼 수 없는 회차 %d / 실패 %d%s" % (
        t.get("title", sid), r["downloaded"], r["locked"], len(r["failures"]),
        " / 쿠키 만료" if r["auth_expired"] else "")
    if r["failures"]:
        msg += " - 첫 실패: %s" % (r["failures"][0].get("error") or "")[:150]
    if not r["downloaded"] and not r["failures"] and not r["locked"]:
        cur = ss.load_kakao_titles().get(sid) or {}
        if cur.get("last_result"):
            msg += " (%s)" % cur["last_result"]
    log(msg)
    ss.save_title_job_state({"running": False, "finished_at": time.time(), "message": msg,
                              "last_error": "쿠키 만료" if r["auth_expired"] else None})
    return r
