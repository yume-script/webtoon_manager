# -*- coding: utf-8 -*-
import hashlib
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
import os
import re
import time

from . import naver_api, downloader, discord_notify, kavita_yaml, state_store as ss

# 한 작품 안에서 연속으로 이 횟수만큼 다운로드가 실패하면(네이버 일시 차단/
# 레이트리밋 가능성) 남은 회차는 포기하고 다음 작품으로 넘어간다.
_MAX_CONSECUTIVE_FAILURES = 3


def _cfg_num(cfg, key, default):
    try:
        v = cfg.get(key)
        return float(v) if v not in (None, "") else default
    except (TypeError, ValueError):
        return default


def _cfg_bool(cfg, key, default=False):
    v = cfg.get(key, default)
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "on", "y", "yes")


_KST_OFFSET = 9 * 3600
_WD_KEYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def target_weekdays(now=None):
    """새 회차를 확인할 요일 키 집합(한국 시간 기준).
    - 오늘 요일
    - 어제 요일: 자정 직후 사이클이나, 지난 사이클 이후 늦게 올라온 회차 보완
    - 22시 이후에는 내일 요일도: 네이버/카카오 모두 다음날 연재분이 전날 밤 22~23시에 공개됨
    컨테이너 시간대(UTC 등)와 무관하게 KST로 계산한다."""
    t = time.gmtime((now or time.time()) + _KST_OFFSET)
    wd = t.tm_wday  # 0=월
    days = {_WD_KEYS[wd], _WD_KEYS[(wd - 1) % 7]}
    if t.tm_hour >= 22:
        days.add(_WD_KEYS[(wd + 1) % 7])
    return days


def in_new_episode_scope(cfg, t, platform="naver", now=None):
    """설정 NEW_EP_SCOPE가 'today'(기본)면 오늘(전후) 요일 연재작 + 매일+ + 기다무 +
    아직 한 번도 받지 않은 작품만 새 회차를 확인한다. 'all'이면 구독작 전부."""
    if str(cfg.get("NEW_EP_SCOPE") or "today") == "all":
        return True
    if t.get("last_downloaded_no") is None:
        return True        # 새로 구독한 작품은 요일과 상관없이 바로 첫 다운로드
    wds = set(t.get("weekdays") or [])
    if "dailyPlus" in wds:
        return True
    if platform == "kakao" and t.get("waitfree") and t.get("status") != "완결":
        return True
    return bool(wds & target_weekdays(now))


_MAX_PAID_PROBE = 30   # 작품당 한 번에 확인할 유료 회차 상한(구매 여부 확인 요청 수 제한)


def _naver_paid_sweep(cfg, session, tid, t, download_root, temp_root, log, skip_nos=None,
                      cookie_hash=""):
    """구매(소장/대여)한 유료 회차를 전체 목록에서 찾아 받는다. 받은 회차 수 반환."""
    title = t.get("title", tid)
    fz = int(cfg.get("FOLDER_ZERO_FILL", 4))
    try:
        eps = naver_api.fetch_episode_list(session, tid)
    except naver_api.NaverAuthExpired:
        return 0
    except Exception as e:  # noqa: BLE001
        log("%s: 구매 회차 확인용 목록 조회 실패 - %s" % (title, e))
        return 0
    paid = sorted([e for e in eps if e.get("charge") and e.get("no") not in (skip_nos or set())],
                  key=lambda e: e["no"])
    paid = [e for e in paid if not downloader.find_existing_episode_archive(
        download_root, title, tid, e["no"], fz)]
    if not paid:
        return 0
    got, probes = 0, 0
    for ep in paid:
        if probes >= _MAX_PAID_PROBE:
            log("%s: 구매 회차 확인 상한(%d) - 나머지는 내일" % (title, _MAX_PAID_PROBE))
            break
        probes += 1
        try:
            ok, skipped, cnt, err = downloader.download_episode(
                session, download_root, temp_root, title, tid, ep["no"],
                image_zero_fill=int(cfg.get("IMAGE_ZERO_FILL", 4)), folder_zero_fill=fz,
                max_concurrent=int(cfg.get("MAX_CONCURRENT_DOWNLOADS", 5)),
                delay_seconds=float(cfg.get("DELAY_SECONDS", 1.0)),
                timeout=int(cfg.get("REQUEST_TIMEOUT_SECONDS", 10)), log=log)
        except naver_api.NaverPaidEpisode:
            continue
        except naver_api.NaverAuthExpired:
            if t.get("is_adult"):
                ss.upsert_title({tid: {"adult_block_hash": cookie_hash}})
            break
        if not ok or skipped:
            continue
        c_ok, _p, c_msg = downloader.compress_episode(
            download_root, temp_root, title, tid, ep["no"], folder_zero_fill=fz, log=log,
            zip_stored=bool(cfg.get("ZIP_STORED", True)), session=session,
            cover_url=t.get("thumbnail") if cfg.get("ADD_COVER_AS_FIRST_PAGE", True) else None,
            comicinfo_meta=_comicinfo_meta_for(t, ep, tid) if cfg.get("GENERATE_COMICINFO_XML", True) else None)
        if c_ok:
            got += 1
            log("%s %s화: 구매(소장/대여)한 유료 회차 받음" % (title, ep["no"]))
            ss.append_history({"type": "download", "source": "purchased", "title_id": tid, "title": title,
                               "episode_no": ep["no"], "subtitle": ep.get("subtitle"), "image_count": cnt})
    if got:
        log("%s: 구매한 유료 회차 %d화 받음" % (title, got))
    return got


def naver_try_owned_paid(cfg):
    """유료(charge) 회차라도 로그인 쿠키가 있으면 한 번 열어 보고, 쿠키(결제수단)로
    이미 대여/소장한 회차면 받는다. 결제는 일어나지 않는다(볼 수 있는 회차만 받음)."""
    return (_cfg_bool(cfg, "NAVER_TRY_OWNED_PAID", True)
            and bool((cfg.get("NAVER_COOKIE_JSON") or "").strip()))


def build_session_from_cfg(cfg):
    return naver_api.build_session(
        cookie_storage_state_json=cfg.get("NAVER_COOKIE_JSON"),
        naver_id=cfg.get("NAVER_ID"),
        naver_pw=cfg.get("NAVER_PW"),
        timeout=int(_cfg_num(cfg, "REQUEST_TIMEOUT_SECONDS", 10)),
    )


def _genre_re(code):
    return re.compile(r"(^|[^A-Z])%s([^A-Z]|$)" % code)


_BL_RE = _genre_re("BL")
_GL_RE = _genre_re("GL")


def _genre_values(t):
    return [t.get("genre") or ""] + list(t.get("tags") or []) + list(t.get("info_tags") or [])


# 카카오는 GL 전용 장르가 없고(대부분 "로맨스") 작품 제목에 "[GL]" / "(GL)"을 붙여 구분한다.
_TITLE_MARK_RE = {code: re.compile(r"[\[\(]\s*%s\s*[\]\)]" % code, re.I) for code in ("BL", "GL")}


def _title_mark(t, code):
    return bool(_TITLE_MARK_RE[code].search(str(t.get("title") or "")))


def is_bl(t):
    """BL 작품인지: 장르/태그에 BL(네이버 태그·상세정보 태그, 카카오 장르) 또는 제목에 [BL]/(BL)."""
    return (any(_BL_RE.search(str(v).upper()) for v in _genre_values(t) if v)
            or _title_mark(t, "BL"))


def is_gl(t):
    """GL 작품인지: 장르/태그에 GL·백합 또는 제목에 [GL]/(GL)(카카오는 이 표기로만 구분됨)."""
    return (any(_GL_RE.search(str(v).upper()) or "백합" in str(v) for v in _genre_values(t) if v)
            or _title_mark(t, "GL"))


def blocked_genres(cfg, t):
    """설정에서 허용하지 않은(기본 비허용) BL/GL 장르 중 이 작품에 해당하는 것들."""
    out = []
    if is_bl(t) and not _cfg_bool(cfg, "ALLOW_BL", False):
        out.append("BL")
    if is_gl(t) and not _cfg_bool(cfg, "ALLOW_GL", False):
        out.append("GL")
    return out


def bl_blocked(cfg, t):
    """BL/GL 장르를 허용하지 않으면(기본) 그 작품은 받지 않는다(이름은 호환용으로 유지)."""
    return bool(blocked_genres(cfg, t))


def genre_block_msg(cfg, t):
    g = "/".join(blocked_genres(cfg, t)) or "BL/GL"
    return "%s 장르 - [설정] > [공통] '%s 장르 웹툰 다운로드 허용'이 꺼져 있어 받지 않음" % (g, g)


BL_BLOCK_MSG = "BL/GL 장르 - [설정] > [공통]에서 허용하지 않아 받지 않음"


def apply_bl_policy(cfg, patch, old_titles, log=print, label=""):
    """BL을 허용하지 않으면, 자동 구독 규칙(신간/작가/매일+/기다무)으로 구독된 BL 작품은
    구독하지 않은 상태로 되돌린다. 사용자가 직접 구독한 작품은 건드리지 않는다(다운로드만 막힘)."""
    if _cfg_bool(cfg, "ALLOW_BL", False) and _cfg_bool(cfg, "ALLOW_GL", False):
        return 0
    n = 0
    for tid, item in patch.items():
        old = old_titles.get(tid) or {}
        merged = dict(old)
        merged.update(item)
        if not merged.get("subscribed") or not bl_blocked(cfg, merged):
            continue
        is_new = not ("subscribed" in old or "excluded" in old)
        if is_new or merged.get("auto_subscribed"):
            item["subscribed"] = False
            item["auto_subscribed"] = None
            n += 1
    if n:
        log("%sBL/GL 장르 작품 %d개는 자동 구독하지 않음(설정에서 허용 안 함)" % (label, n))
    return n


def _autosubscribe_patch(item, old, author_names, auto_new=False):
    """신규 발견 항목이면 작가 자동구독 여부(+ '신간 자동 구독' 설정)를
    판단하고, 기존에 사용자가 직접 구독/제외/구독해제한 적 있는 항목이면
    그 선택을 그대로 유지한다.
    auto_new=True면 작가 매칭 여부와 무관하게 신규 발견 항목을 전부
    구독으로 올린다("설정 > 신간 자동 구독" 토글)."""
    p = dict(item)
    if "subscribed" in old or "excluded" in old:
        p["subscribed"] = old.get("subscribed", False)
        p["excluded"] = old.get("excluded", False)
        p["unsubscribed"] = old.get("unsubscribed", False)
    else:
        auto = bool(auto_new)
        if item.get("author") and any(a in item.get("author", "") for a in author_names):
            auto = True
        p["subscribed"] = auto
        p["excluded"] = False
        p["unsubscribed"] = False
    p["last_seen_at"] = time.time()
    return p


def _apply_daily_plus_autosubscribe(patch, enabled, log=print):
    """"매일+ 자동 구독" 설정이 켜져 있으면, 이번 스캔 결과(patch)에 포함된
    작품 중 '매일+(dailyPlus)' 탭에 속하면서 사용자가 구독/구독해제/제외 중
    아무것도 명시적으로 선택한 적 없는(전부 기본값인) 작품을 전부 구독으로
    올린다. _autosubscribe_patch()는 "새로 발견된 항목"일 때만 자동구독을
    판단하고 기존 항목은 그대로 유지하는데, 이 함수는 그와 달리 매 스캔마다
    이미 알고 있던 작품까지 다시 확인한다 - 그래야 이 설정을 나중에 켰을 때도
    이미 스캔되어 있던 매일+ 작품들을 곧바로 잡아낼 수 있다("신간 자동 구독"과
    달리 매일+ 탭 자체가 소수라 한꺼번에 구독되어도 폭주 위험이 낮다).
    사용자가 한 번이라도 구독해제/제외를 누른 작품은 절대 건드리지 않는다."""
    if not enabled:
        return patch
    promoted = 0
    for tid, item in patch.items():
        if naver_api.DAILY_PLUS not in (item.get("weekdays") or []):
            continue
        if item.get("subscribed") or item.get("excluded") or item.get("unsubscribed"):
            continue
        item["subscribed"] = True
        item["auto_subscribed"] = "dailyPlus"
        promoted += 1
    if promoted:
        log("매일+ 자동 구독: %d개 작품을 새로 구독 처리함" % promoted)
    return patch


def run_scan_weekday(cfg, log=print):
    """빠른 스캔: 요일별 연재중 목록 + 등록된 태그(작가/장르) 자동구독 대상만
    수집한다. 완결 전체 목록(최대 200페이지라 느림)은 포함하지 않는다 -
    그건 run_scan_finished()가 별도 스케줄로 처리한다. 초기 설치 시 이 스캔만
    먼저 빠르게 끝나서 카테고리탭이 바로 쓸만해지도록 하기 위해 분리했다."""
    if cfg.get("LOW_PRIORITY_MODE", True):
        downloader.lower_thread_priority(int(cfg.get("DOWNLOAD_NICE_LEVEL", 10)))

    def _cancelled():
        return bool(ss.load_job_state().get("cancel_requested"))

    session = build_session_from_cfg(cfg)
    ss.save_job_state({"stage": "scanning", "message": "요일별 목록 수집 중"})
    log("요일별 연재 목록 수집 시작")
    merged = {}
    try:
        merged.update(naver_api.fetch_weekday_titles(session, should_cancel=_cancelled, log=log))
    except Exception as e:  # noqa: BLE001
        log("요일별 목록 수집 실패: %s" % e)

    if not _cancelled():
        at = ss.load_authors_tags()
        for tag in at.get("tags", []):
            if _cancelled():
                break
            ss.save_job_state({"message": "태그 '%s' 목록 수집 중" % tag})
            try:
                merged.update(naver_api.fetch_genre_titles(session, tag, should_cancel=_cancelled))
            except Exception as e:  # noqa: BLE001
                log("태그 '%s' 수집 실패: %s" % (tag, e))

    old_titles = ss.load_titles()
    at = ss.load_authors_tags()
    author_names = set(a.strip() for a in at.get("authors", []) if a.strip())

    # "신간 자동 구독" 설정이 켜져 있어도, 이번이 이 카테고리탭의 첫 스캔이라
    # old_titles가 완전히 비어있으면(설치 직후) 적용하지 않는다. 그대로
    # 적용하면 현재 연재 중인 모든 작품이 전부 "처음 보는 항목"이라 한꺼번에
    # 구독되어 버려서, 다운로드 큐가 폭주하고 원치 않는 작품까지 대량으로
    # 구독되는 사고가 날 수 있다. 다음 스캔부터는 정상 적용된다.
    auto_new = bool(cfg.get("AUTO_SUBSCRIBE_NEW_TITLES"))
    if auto_new and not old_titles:
        auto_new = False
        log("신간 자동 구독이 켜져 있지만 첫 스캔이라(구독 목록이 비어있음) 이번 한 번은 "
            "적용하지 않습니다 - 현재 연재 중인 모든 작품이 한꺼번에 구독되는 걸 막기 위함. "
            "다음 스캔부터 정상 적용됩니다.")

    patch = {tid: _autosubscribe_patch(item, old_titles.get(tid, {}), author_names, auto_new=auto_new)
             for tid, item in merged.items()}
    patch = _apply_daily_plus_autosubscribe(patch, bool(cfg.get("AUTO_SUBSCRIBE_DAILY_PLUS")), log=log)
    apply_bl_policy(cfg, patch, old_titles, log=log, label="네이버: ")

    ss.upsert_title(patch)
    kakao_count = 0
    if _cfg_bool(cfg, "KAKAO_ENABLE", False) and not _cancelled():
        try:
            from . import kakao_pipeline
            kakao_count = kakao_pipeline.run_kakao_scan_weekday(
                cfg, log=log, should_cancel=_cancelled).get("scanned", 0)
        except Exception as e:  # noqa: BLE001
            log("카카오페이지 요일별 스캔 실패(네이버 결과에는 영향 없음): %s" % e)
    if _cancelled():
        log("요일별 스캔 취소됨 - 지금까지 모은 %d개 작품만 반영" % len(patch))
    else:
        msg = "요일별 스캔 완료: 네이버 %d개 / 카카오 %d개 작품" % (len(patch), kakao_count)
        ss.save_job_state({"last_scan_at": time.time(), "message": msg})
        log(msg)
    return {"scanned": len(patch) + kakao_count}


def run_scan_finished(cfg, log=print, max_pages=200):
    """느린 스캔: 완결 전체 목록(최대 200페이지, 페이지당 0.2초 대기)을 수집한다.
    구독중이던 작품이 완결로 새로 바뀌면 디스코드로 알림을 보낸다.
    자동 스케줄러는 이 함수를 하루 중 정해진 시각(FINISHED_SCAN_HOUR)에
    한 번만 호출한다(scheduler.py 참고) - 매 주기마다 돌리기엔 너무 느려서
    초기 설치 시 전체 인덱싱이 오래 걸리는 원인이었다."""
    if cfg.get("LOW_PRIORITY_MODE", True):
        downloader.lower_thread_priority(int(cfg.get("DOWNLOAD_NICE_LEVEL", 10)))
    session = build_session_from_cfg(cfg)
    ss.save_job_state({"stage": "scanning_finished", "message": "완결 목록 수집 중"})
    log("완결 목록 수집 시작")
    try:
        finished = naver_api.fetch_finished_titles(
            session, max_pages=max_pages,
            should_cancel=lambda: bool(ss.load_job_state().get("cancel_requested")))
    except Exception as e:  # noqa: BLE001
        log("완결 목록 수집 실패: %s" % e)
        finished = {}

    old_titles = ss.load_titles()
    finished_events = []
    patch = {}
    for tid, item in finished.items():
        old = old_titles.get(tid, {})
        was_finished = old.get("status") == "완결"
        now_finished = item.get("status") == "완결"
        if now_finished and not was_finished and old.get("subscribed"):
            finished_events.append({"titleId": tid, "title": item.get("title") or old.get("title")})
        patch[tid] = _autosubscribe_patch(item, old, set())

    ss.upsert_title(patch)
    if _cfg_bool(cfg, "KAKAO_ENABLE", False):
        try:
            from . import kakao_pipeline
            kakao_pipeline.run_kakao_scan_finished(
                cfg, log=log,
                should_cancel=lambda: bool(ss.load_job_state().get("cancel_requested")))
        except Exception as e:  # noqa: BLE001
            log("카카오페이지 완결 스캔 실패(네이버 결과에는 영향 없음): %s" % e)
    ss.save_job_state({"last_finished_scan_at": time.time(),
                        "message": "완결 스캔 완료: 네이버 %d개 작품" % len(patch)})
    log("완결 스캔 완료: 총 %d개 작품" % len(patch))

    if finished_events:
        dl_root = cfg.get("DOWNLOAD_ROOT") or ss.DOWNLOAD_DEFAULT_DIR
        for ev in finished_events:
            discord_notify.notify_finished(cfg, ev["title"], ev["titleId"])
            # 완결로 바뀌었으니 kavita.yaml의 Publication Status도 갱신
            update_kavita_yaml(cfg, session, dl_root, ev["titleId"], log=log, force_info=True)
    return {"scanned": len(patch), "finished_events": finished_events}


def _series_json_meta_for(t, title_id):
    """titles.json의 작품 레코드(t)로 series.json에 넣을 메타데이터 dict를
    조립한다. 네이버 목록/상세 API에서 줄거리(summary)는 현재 긁어오지 않으므로
    항상 빈 값이다(추후 상세페이지 파싱을 추가하면 채울 수 있음)."""
    tags = t.get("tags") or []
    genre_tags = ", ".join(tags) if tags else ""
    return {
        "author": t.get("author") or "",
        "summary": "",
        "link": "https://comic.naver.com/webtoon/list?titleId=%s" % title_id,
        "score": t.get("rating") or 0,
        "genre": genre_tags,
        "tags": genre_tags,
        "cover_image_url": t.get("thumbnail") or "",
    }


def _comicinfo_meta_for(t, ep, title_id):
    """titles.json의 작품 레코드(t)와 회차 정보(ep)로 ComicInfo.xml에 넣을
    메타데이터 dict를 조립한다. 값이 없는 필드는 빈 문자열/None으로 둬서
    downloader.build_comicinfo_xml()이 알아서 생략하게 한다."""
    tags = t.get("tags") or []
    return {
        "series": t.get("title"),
        "sub_title": ep.get("subtitle"),
        "writer": t.get("author") or None,
        "genre": ", ".join(tags) if tags else None,
        "web": "https://comic.naver.com/webtoon/detail?titleId=%s&no=%s" % (title_id, ep.get("no")),
        "age_rating": "Adults Only 18+" if t.get("is_adult") else None,
    }


def _remember_release_date(title_id, episodes):
    """회차 목록에서 1화 공개일을 뽑아 titles.json에 release_date(YYYYMMDD)로
    저장한다(kavita.yaml의 Release Date/Year/Month/Day용). 값이 바뀔 때만 쓴다."""
    try:
        rd = kavita_yaml.release_date_from_episodes(episodes)
        if not rd:
            return
        cur = ss.load_titles().get(str(title_id), {})
        # 목록 API가 가장 오래된 회차까지 다 주지 못한 경우를 대비해, 이미 저장된
        # 날짜보다 늦은 날짜로는 덮어쓰지 않는다.
        if not cur.get("release_date") or rd < cur.get("release_date"):
            ss.upsert_title({str(title_id): {"release_date": rd}})
    except Exception:  # noqa: BLE001
        pass


def update_kavita_yaml(cfg, session, download_root, title_id, log=print, force_info=False):
    """설정(GENERATE_KAVITA_YAML)이 켜져 있으면 시리즈 폴더의 kavita.yaml을
    생성/갱신한다. 내용이 같으면 파일을 건드리지 않는다."""
    if not cfg.get("GENERATE_KAVITA_YAML", True):
        return "disabled"
    return kavita_yaml.write_kavita_yaml(
        download_root, title_id, session=session,
        embed_cover=bool(cfg.get("KAVITA_YAML_EMBED_COVER", True)),
        force_info=force_info, log=log)


def run_kavita_yaml_all(cfg, log=print, force_info=True, manage_job=True):
    """다운로드 경로 바로 아래의 시리즈 폴더를 전부 훑어 kavita.yaml을 일괄
    생성/갱신한다(카테고리탭 '설정' 탭의 버튼용).

    예전에는 titles.json에 있는 작품 중 "현재 제목으로 계산한 폴더"가 있는
    것만 대상으로 해서, 제목이 바뀐 작품/목록에 없는 작품(수동 조회로 받은
    작품 등)/예전 파일명 형식만 있는 폴더가 빠졌다. 이제는 실제 폴더를 기준으로
    "제목 (titleId)" 이름에서 titleId를 읽어 처리한다."""
    session = build_session_from_cfg(cfg)
    download_root = cfg.get("DOWNLOAD_ROOT") or ss.DOWNLOAD_DEFAULT_DIR
    try:
        names = sorted(os.listdir(download_root))
    except OSError as e:
        msg = "kavita.yaml 일괄 생성 실패: 다운로드 경로를 읽을 수 없음 (%s)" % e
        log(msg)
        if manage_job:
            ss.save_job_state({"running": False, "stage": "error", "finished_at": time.time(),
                                "last_error": str(e), "message": msg})
        return {}

    naver_ids = set(ss.load_titles().keys())
    kakao_ids = set(ss.load_kakao_titles().keys())
    roots = [(download_root, names)]
    # 카카오페이지 저장 경로가 따로 있으면 그쪽 폴더도 함께 처리
    from . import kakao_pipeline
    k_root = kakao_pipeline.kakao_root(cfg)
    n_root = kakao_pipeline.kakao_novel_root(cfg)
    seen_roots = {os.path.abspath(download_root)}
    for extra in (k_root, n_root):
        if os.path.abspath(extra) in seen_roots or not os.path.isdir(extra):
            continue
        seen_roots.add(os.path.abspath(extra))
        try:
            roots.append((extra, sorted(os.listdir(extra))))
        except OSError:
            pass
    kakao_all = ss.load_kakao_titles()

    targets, unmatched = [], []
    for root, root_names in roots:
        for name in root_names:
            full = os.path.join(root, name)
            if not os.path.isdir(full):
                continue
            m = kavita_yaml.SERIES_DIR_RE.match(name)
            if not m:
                unmatched.append(name)
                continue
            tid = m.group(2)
            is_kakao = tid in kakao_ids and (root in (k_root, n_root) or tid not in naver_ids)
            plat = "naver"
            if is_kakao:
                plat = "kakao_novel" if kakao_pipeline._is_novel(kakao_all.get(tid) or {}) else "kakao"
            targets.append((full, m.group(1), tid, plat))

    ss.save_job_state({"stage": "kavita_yaml", "message": "kavita.yaml 일괄 생성 중",
                        "progress": 0, "total": len(targets)})
    log("kavita.yaml 일괄 생성 시작: 경로 %s / 시리즈 폴더 %d개" % (download_root, len(targets)))
    if unmatched:
        log("'제목 (titleId)' 형식이 아니라 titleId를 알 수 없어 건너뛴 폴더 %d개: %s%s" % (
            len(unmatched), ", ".join(unmatched[:20]),
            "" if len(unmatched) <= 20 else " ...외 %d개" % (len(unmatched) - 20)))

    counts = {"written": 0, "unchanged": 0, "skipped": 0, "error": 0}
    for i, (full, folder_title, tid, platform) in enumerate(targets):
        if ss.load_job_state().get("cancel_requested"):
            log("kavita.yaml 일괄 생성 취소됨")
            break
        ss.save_job_state({"progress": i, "message": "kavita.yaml: %s" % folder_title})
        r = kavita_yaml.write_kavita_yaml(
            download_root, tid, session=session,
            embed_cover=bool(cfg.get("KAVITA_YAML_EMBED_COVER", True)),
            force_info=force_info, log=log, series_dir=full, folder_title=folder_title,
            platform=platform)
        counts[r] = counts.get(r, 0) + 1
        time.sleep(0.2)
    msg = ("kavita.yaml 일괄 생성 완료: 갱신 %d / 변경없음 %d / 회차파일없음 %d / 실패 %d"
           " / 형식불일치 폴더 %d" % (counts["written"], counts["unchanged"], counts["skipped"],
                                   counts["error"], len(unmatched)))
    log(msg)
    ss.save_job_state({"last_kavita_all_at": time.time()})
    if manage_job:
        ss.save_job_state({"running": False, "stage": "done", "finished_at": time.time(),
                            "progress": len(targets), "message": msg})
    return counts


def _episodes_to_download(session, cfg, title_id, known_last_no):
    # 이미 받은 회차가 있으면 그 회차가 나오는 페이지에서 멈춘다(요청 수 대폭 감소).
    # 처음 받는 작품/"다시 확인"(known_last_no 없음)은 전체 목록.
    episodes = naver_api.fetch_episode_list(
        session, title_id, stop_at_no=known_last_no if known_last_no else None)
    if not known_last_no:
        _remember_release_date(title_id, episodes)
    # 최신 -> 과거 순으로 오므로 known_last_no보다 큰(새 회차)만, 오래된 순으로 반환
    new_eps = [e for e in episodes if isinstance(e.get("no"), int) and e["no"] > (known_last_no or 0)]
    new_eps.sort(key=lambda e: e["no"])
    return new_eps


def run_download_cycle(cfg, log=print):
    if cfg.get("LOW_PRIORITY_MODE", True):
        downloader.lower_thread_priority(int(cfg.get("DOWNLOAD_NICE_LEVEL", 10)))
    session = build_session_from_cfg(cfg)
    titles = ss.load_titles()
    subscribed = {tid: t for tid, t in titles.items()
                  if t.get("subscribed") and not t.get("excluded") and not t.get("unsubscribed")}
    all_sub = len(subscribed)
    subscribed = {tid: t for tid, t in subscribed.items() if in_new_episode_scope(cfg, t, "naver")}
    bl_skip = [tid for tid, t in subscribed.items() if bl_blocked(cfg, t)]
    if bl_skip:
        log("BL/GL 장르 %d개 작품은 설정에 따라 다운로드하지 않음" % len(bl_skip))
        subscribed = {tid: t for tid, t in subscribed.items() if tid not in bl_skip}
    log("새 회차 확인 대상: 구독 %d개 중 %d개 (%s)" % (
        all_sub, len(subscribed),
        "전체" if str(cfg.get("NEW_EP_SCOPE") or "today") == "all" else
        "요일 %s + 매일+ + 첫 다운로드" % ",".join(sorted(target_weekdays()))))

    download_root = cfg.get("DOWNLOAD_ROOT") or ss.DOWNLOAD_DEFAULT_DIR
    temp_root = cfg.get("TEMP_DOWNLOAD_ROOT") or ss.TMP_DOWNLOAD_DEFAULT_DIR
    if download_root == ss.DOWNLOAD_DEFAULT_DIR:
        log("이번 다운로드 경로(설정 안 됨 - 기본 경로 사용): %s" % download_root)
    else:
        log("이번 다운로드 경로(설정값): %s" % download_root)
    max_new = int(_cfg_num(cfg, "MAX_NEW_EPISODES_PER_TITLE", 10))
    max_concurrent = int(_cfg_num(cfg, "MAX_CONCURRENT_DOWNLOADS", 5))
    delay_seconds = _cfg_num(cfg, "DELAY_SECONDS", 1.0)
    image_zero_fill = int(_cfg_num(cfg, "IMAGE_ZERO_FILL", 4))
    folder_zero_fill = int(_cfg_num(cfg, "FOLDER_ZERO_FILL", 4))
    timeout = int(_cfg_num(cfg, "REQUEST_TIMEOUT_SECONDS", 10))

    ss.save_job_state({"stage": "downloading", "message": "구독 작품 회차 확인 중",
                        "progress": 0, "total": len(subscribed)})

    failures = []

    def _cancelled():
        return bool(ss.load_job_state().get("cancel_requested"))

    naver_cookie = (cfg.get("NAVER_COOKIE_JSON") or "").strip()
    cookie_hash = hashlib.sha1(naver_cookie.encode("utf-8")).hexdigest() if naver_cookie else ""
    state = {"downloaded": 0, "cookie_expired": False, "cancelled": False, "done": 0}
    lock = threading.Lock()
    tls = threading.local()

    def _thread_session():
        # requests.Session은 스레드 간 공유가 안전하지 않아 작업 스레드마다 따로 만든다
        if getattr(tls, "session", None) is None:
            tls.session = build_session_from_cfg(cfg)
            if cfg.get("LOW_PRIORITY_MODE", True):
                downloader.lower_thread_priority(int(cfg.get("DOWNLOAD_NICE_LEVEL", 10)))
        return tls.session

    initial_n = int(_cfg_num(cfg, "INITIAL_EPISODES_LIMIT", 0))

    def _process_title(i, tid, t):
        # 성인 작품: 쿠키가 없거나, 지금 쿠키로 성인 인증 실패를 이미 확인했으면
        # 회차를 끝까지 두들기지 않고 작품 자체를 건너뛴다
        if t.get("is_adult") and (not naver_cookie or t.get("adult_block_hash") == cookie_hash):
            return
        if _cancelled() or state["cookie_expired"]:
            state["cancelled"] = state["cancelled"] or _cancelled()
            return
        with lock:
            state["done"] += 1
            done_n = state["done"]
        ss.save_job_state({"progress": done_n, "message": "%s 새 회차 확인 중" % t.get("title", tid)})
        session = _thread_session()
        try:
            new_eps = _episodes_to_download(session, cfg, tid, t.get("last_downloaded_no"))
        except Exception as e:  # noqa: BLE001
            log("회차 목록 조회 실패 titleId=%s: %s" % (tid, e))
            return

        if not new_eps:
            # 새 회차가 없어도 작품 정보(완결/휴재/줄거리 등)가 바뀌었을 수 있으니
            # kavita.yaml을 다시 맞춰본다. 상세정보 조회는 작품당 하루 1회로
            # 제한되고, 내용이 같으면 파일은 건드리지 않는다.
            update_kavita_yaml(cfg, session, download_root, tid, log=log)
            return

        # 새로 구독한 작품(아직 한 화도 안 받음)은 설정에 따라 최신 N화만 받는다.
        # 이후로는 last_downloaded_no 다음 회차부터 이어지므로 그 이전 회차는 받지 않는다
        # (필요하면 backfill_all.py 또는 "다시 확인"으로 전체를 받을 수 있음).
        if initial_n > 0 and t.get("last_downloaded_no") is None and not t.get("initial_limit_done"):
            free_eps = [e for e in new_eps if not e.get("charge")]
            if len(free_eps) > initial_n:
                log("%s: 처음 구독한 작품이라 최신 %d화만 받음(전체 무료 %d화)" %
                    (t.get("title", tid), initial_n, len(free_eps)))
                new_eps = free_eps[-initial_n:]

        capped = new_eps if max_new <= 0 else new_eps[:max_new]
        rest_needed = max_new > 0 and len(new_eps) > max_new

        if cfg.get("GENERATE_SERIES_JSON", True):
            downloader.write_series_json(
                download_root, t.get("title", tid), tid,
                _series_json_meta_for(t, tid), log=log)

        last_ok_no = t.get("last_downloaded_no")
        consecutive_fail = 0
        owned_mode = naver_try_owned_paid(cfg)
        tried_paid = set()
        paid_probe = {"n": 0}
        for ep in capped:
            if _cancelled():
                log("titleId=%s: 취소 요청 확인됨 - 남은 회차는 다음 실행 때 이어받습니다" % tid)
                state["cancelled"] = True
                break
            if ep.get("charge") and not naver_try_owned_paid(cfg):
                # 목록 API가 이미 유료(charge=true)라고 알려주는 회차를 만나면,
                # 그 뒤 회차들도 순서대로 계속 유료일 가능성이 매우 높다("매일
                # 하나씩 풀기" 방식은 오래된 순서대로 풀리므로). 남은 회차를
                # 하나씩 다 확인하지 말고 이 작품은 여기서 접고 다음 작품으로.
                # last_ok_no는 그대로 둬서 다음 스캔 때 이 회차부터 다시 확인한다.
                log("titleId=%s %s화: 유료(charge=true) 회차, 목록 API 기준 - 이후 회차도 유료로 보고 이 작품은 중단" % (tid, ep["no"]))
                ss.append_history({
                    "type": "skipped_paid", "source": "auto", "title_id": tid,
                    "title": t.get("title", tid),
                    "episode_no": ep["no"], "error": "유료 회차(목록 API charge=true)",
                })
                break
            try:
                ok, skipped, img_count, err = downloader.download_episode(
                    session, download_root, temp_root, t.get("title", tid), tid, ep["no"],
                    image_zero_fill=image_zero_fill, folder_zero_fill=folder_zero_fill,
                    max_concurrent=max_concurrent, delay_seconds=delay_seconds,
                    timeout=timeout, log=log)
            except naver_api.NaverAuthExpired as e:
                if t.get("is_adult"):
                    # 성인 작품 하나가 인증에 막힌 것 - 다른 작품까지 멈추지 않고
                    # 이 작품만 중단하고, 같은 쿠키로는 다시 시도하지 않게 표시
                    log("%s: 성인 인증 필요/실패 - 이 작품은 건너뜀(쿠키를 바꾸면 다시 시도) - %s" %
                        (t.get("title", tid), e))
                    ss.upsert_title({tid: {"adult_block_hash": cookie_hash}})
                    break
                log("인증 만료: %s" % e)
                state["cookie_expired"] = True
                break
            except naver_api.NaverPaidEpisode as e:
                if ep.get("charge") and owned_mode:
                    # 구매(소장/대여)하지 않은 유료 회차 - 뒤쪽에 띄엄띄엄 구매한 회차가
                    # 있을 수 있으니 멈추지 않고 다음 유료 회차를 계속 확인한다(상한 있음)
                    tried_paid.add(ep["no"])
                    paid_probe["n"] += 1
                    if paid_probe["n"] >= _MAX_PAID_PROBE:
                        log("titleId=%s: 유료 회차 확인 상한(%d) 도달 - 나머지는 다음 확인 때" % (tid, _MAX_PAID_PROBE))
                        break
                    continue
                # 마찬가지로 이후 회차도 계속 유료일 가능성이 높아 이 작품은
                # 여기서 접고 다음 작품으로 넘어간다("24시간마다 무료" 로테이션이
                # 있으니 last_ok_no는 안 건드려서 다음 스캔 때 다시 확인함).
                log("titleId=%s %s화: %s (이후 회차도 유료로 보고 이 작품은 중단, 다음 스캔 때 재시도)" % (tid, ep["no"], e))
                ss.append_history({
                    "type": "skipped_paid", "source": "auto", "title_id": tid,
                    "title": t.get("title", tid),
                    "episode_no": ep["no"], "error": str(e),
                })
                break

            if ep.get("charge") and not ok:
                # 대여/소장하지 않은 유료 회차 - 다음 유료 회차 계속 확인(띄엄띄엄 구매 대응)
                tried_paid.add(ep["no"])
                paid_probe["n"] += 1
                if paid_probe["n"] >= _MAX_PAID_PROBE:
                    break
                continue
            if ok and ep.get("charge") and not skipped:
                log("titleId=%s %s화: 유료 회차지만 대여/소장 중이라 받음" % (tid, ep["no"]))
            if ok:
                if skipped:
                    consecutive_fail = 0
                    last_ok_no = ep["no"]
                else:
                    with lock:
                        state["downloaded"] += 1
                    ss.append_history({
                        "type": "download", "source": "auto", "title_id": tid,
                        "title": t.get("title", tid),
                        "episode_no": ep["no"], "subtitle": ep.get("subtitle"),
                        "image_count": img_count,
                    })
                    # 이미지 다운로드(1단계)와 완전히 분리된 2단계 - 회차 폴더가
                    # 디스크에 다 쓰인 뒤에 별도로 압축한다.
                    c_ok, c_path, c_msg = downloader.compress_episode(
                        download_root, temp_root, t.get("title", tid), tid, ep["no"],
                        folder_zero_fill=folder_zero_fill, log=log,
                        zip_stored=bool(cfg.get("ZIP_STORED", True)),
                        session=session,
                        cover_url=t.get("thumbnail") if cfg.get("ADD_COVER_AS_FIRST_PAGE", True) else None,
                        comicinfo_meta=_comicinfo_meta_for(t, ep, tid)
                        if cfg.get("GENERATE_COMICINFO_XML", True) else None)
                    if c_ok:
                        consecutive_fail = 0
                        last_ok_no = ep["no"]
                    else:
                        # 버그 수정: 예전에는 압축이 실패해도 last_ok_no를 이미
                        # 위에서 무조건 이 회차 번호로 넘겨버려서, 실제로는 zip이
                        # 없는데도 "완료"로 취급돼 다음 확인 때부터 이 회차가
                        # 영구히 재시도 대상에서 빠지는 문제가 있었다(사용자가
                        # "다시 확인" 버튼을 눌러야만 알아채고 복구 가능했음).
                        # 이제는 압축 실패를 다운로드 실패와 동일하게 취급해서
                        # last_ok_no를 전진시키지 않고, 다음 사이클에 이 회차부터
                        # 다시 시도되도록 남겨둔다.
                        log("titleId=%s %s화 압축 실패: %s (재시도 대상으로 남김)" % (tid, ep["no"], c_msg))
                        consecutive_fail += 1
                        failures.append({"title_id": tid, "title": t.get("title", tid),
                                          "episode_no": ep["no"], "error": "압축 실패: %s" % c_msg})
                        if consecutive_fail >= _MAX_CONSECUTIVE_FAILURES:
                            log("titleId=%s 연속 %d회 실패(압축 포함) - 이 작품은 중단하고 다음으로 넘어감" %
                                (tid, consecutive_fail))
                            break
            else:
                consecutive_fail += 1
                failures.append({"title_id": tid, "title": t.get("title", tid),
                                  "episode_no": ep["no"], "error": err})
                ss.append_history({
                    "type": "download_fail", "source": "auto", "title_id": tid,
                    "title": t.get("title", tid),
                    "episode_no": ep["no"], "error": err,
                })
                # 딜레이를 지켜도 연속으로 계속 실패하면(네이버 일시 차단/레이트리밋
                # 가능성) 남은 회차를 전부 두들기지 말고 이 작품은 여기서 접고
                # 다음 작품으로 넘어간다. 다음 스캔 주기에 last_downloaded_no부터
                # 다시 이어서 시도한다.
                if consecutive_fail >= _MAX_CONSECUTIVE_FAILURES:
                    log("titleId=%s 연속 %d회 다운로드 실패 - 일시 차단 가능성으로 이 작품은 중단하고 다음으로 넘어감" %
                        (tid, consecutive_fail))
                    break

        # up_flag("UP" 뱃지)는 last_ok_no 변경 여부와 무관하게 항상 최신
        # rest_needed 값으로 갱신한다. 예전에는 last_ok_no가 바뀔 때만
        # upsert했는데, 유료/연속실패로 하나도 못 받은 회차라도 밀린 회차가
        # 있으면(rest_needed=True) UP 뱃지가 떠야 하는데 안 뜨는 문제가 있었다.
        # 하루 1번: 구매(소장/대여)한 유료 회차를 목록 전체에서 찾아 받는다.
        # (위 루프는 last_downloaded_no 이후 회차만 보므로, 예전에 산 회차나
        #  중간에 건너뛴 회차는 여기서 채움)
        if (owned_mode and not state["cookie_expired"] and not _cancelled()
                and time.time() - float(t.get("paid_sweep_at") or 0) >= 24 * 3600):
            got = _naver_paid_sweep(cfg, session, tid, t, download_root, temp_root, log,
                                    skip_nos=tried_paid, cookie_hash=cookie_hash)
            with lock:
                state["downloaded"] += got
            ss.upsert_title({tid: {"paid_sweep_at": time.time()}})

        patch = {"up_flag": rest_needed, "initial_limit_done": True}
        if last_ok_no != t.get("last_downloaded_no"):
            patch["last_downloaded_no"] = last_ok_no
        ss.upsert_title({tid: patch})

        # 새 회차를 받았거나(목록 변경) 작품 정보가 바뀌었으면 kavita.yaml 갱신.
        # 내용이 같으면 파일은 건드리지 않는다.
        if not state["cookie_expired"]:
            update_kavita_yaml(cfg, session, download_root, tid, log=log)

        return

    workers = max(1, min(5, int(_cfg_num(cfg, "PARALLEL_TITLES", 2))))
    items = list(subscribed.items())
    if workers == 1:
        for i, (tid, t) in enumerate(items):
            _process_title(i, tid, t)
            if state["cancelled"] or state["cookie_expired"]:
                break
    else:
        log("작품 %d개를 동시에 처리합니다" % workers)
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="wtm_naver") as ex:
            futs = [ex.submit(_process_title, i, tid, t) for i, (tid, t) in enumerate(items)]
            for f in as_completed(futs):
                try:
                    f.result()
                except Exception as e:  # noqa: BLE001
                    log("작품 처리 중 오류(다음 작품은 계속): %s" % e)
    downloaded_count = state["downloaded"]
    cookie_expired = state["cookie_expired"]
    cancelled = state["cancelled"]

    ss.save_job_state({"progress": len(subscribed), "message": "다운로드 사이클 종료"})

    if cookie_expired:
        discord_notify.notify_cookie_expired(cfg)

    if failures:
        discord_notify.notify_failures(cfg, failures)

    log("다운로드 사이클 완료: 신규 %d화, 실패 %d건%s" %
        (downloaded_count, len(failures), " (취소됨)" if cancelled else ""))
    return {"downloaded": downloaded_count, "failures": failures,
            "cookie_expired": cookie_expired, "cancelled": cancelled}


def run_finished_scan_job(cfg, log=print):
    """완결 전체 스캔을 job_state의 running 가드 안에서 실행하는 래퍼.
    "완결 목록 지금 수집" 수동 버튼과 스케줄러(하루 중 정해진 시각 1회)
    양쪽에서 이 함수를 쓴다."""
    ss.save_job_state({"running": True, "started_at": time.time(), "last_error": None})
    try:
        result = run_scan_finished(cfg, log=log)
        ss.save_job_state({"running": False, "stage": "done", "finished_at": time.time(),
                            "message": "완결 스캔 완료: %d개" % result["scanned"]})
        return result
    except Exception as e:  # noqa: BLE001
        log("완결 스캔 중 오류: %s" % e)
        ss.save_job_state({"running": False, "stage": "error", "finished_at": time.time(),
                            "last_error": str(e)})
        raise


def run_full_cycle(cfg, log=print):
    """스케줄러의 기본 주기(INTERVAL_MINUTES)마다 도는 빠른 사이클: 요일별
    스캔 + 다운로드만 한다. 완결 전체 스캔은 무겁고 느려서 여기 포함하지
    않는다 - scheduler.py가 run_scan_finished를 별도 시각에 따로 부른다."""
    ss.save_job_state({"running": True, "started_at": time.time(), "last_error": None})
    try:
        scan_result = run_scan_weekday(cfg, log=log)
        # 네이버와 카카오는 서로 다른 사이트라 동시에 받아도 차단 위험이 늘지 않는다.
        # 카카오를 별도 스레드로 띄워 네이버 다운로드와 나란히 진행한다.
        kakao_box = {"result": None}
        kakao_thread = None
        if (_cfg_bool(cfg, "KAKAO_ENABLE", False) and _cfg_bool(cfg, "KAKAO_AUTO", True)
                and not ss.load_job_state().get("cancel_requested")):
            def _kakao_run():
                try:
                    from . import kakao_pipeline
                    kakao_box["result"] = kakao_pipeline.run_kakao_cycle(cfg, log=log)
                    # 하루 1번 구매 작품 동기화(로그인 쿠키가 있을 때)
                    if ((cfg.get("KAKAO_COOKIE") or "").strip() and _cfg_bool(cfg, "KAKAO_SYNC_PURCHASED", True)
                            and not ss.load_job_state().get("cancel_requested")
                            and time.time() - float(ss.load_job_state().get("last_kakao_purchase_sync_at") or 0)
                            >= 24 * 3600):
                        kakao_pipeline.sync_purchased(cfg, log=log)
                except Exception as e:  # noqa: BLE001
                    log("카카오페이지 사이클 오류(네이버 결과에는 영향 없음): %s" % e)
            kakao_thread = threading.Thread(target=_kakao_run, name="wtm_kakao_cycle", daemon=True)
            kakao_thread.start()
        dl_result = run_download_cycle(cfg, log=log)
        if kakao_thread is not None:
            kakao_thread.join()
        kakao_result = kakao_box["result"]
        # 하루 한 번, 다운로드 경로 전체를 훑어 kavita.yaml이 없거나 낡은 폴더를 채운다
        # (구독하지 않은 작품 폴더, 예전에 받아둔 폴더, 수동으로 넣은 폴더 등).
        if (_cfg_bool(cfg, "GENERATE_KAVITA_YAML", True)
                and not dl_result.get("cancelled")
                and not ss.load_job_state().get("cancel_requested")
                and time.time() - float(ss.load_job_state().get("last_kavita_all_at") or 0) > 24 * 3600):
            try:
                ss.save_job_state({"message": "kavita.yaml 전체 점검 중"})
                run_kavita_yaml_all(cfg, log=log, force_info=False, manage_job=False)
            except Exception as e:  # noqa: BLE001
                log("kavita.yaml 전체 점검 실패(무시): %s" % e)
        cancelled = dl_result.get("cancelled") or (kakao_result or {}).get("cancelled")
        ss.save_job_state({"running": False,
                            "stage": "cancelled" if cancelled else "done",
                            "finished_at": time.time(),
                            "message": "완료: 스캔 %d개 / 신규 %d화 / 실패 %d건%s%s" % (
                                scan_result["scanned"], dl_result["downloaded"],
                                len(dl_result["failures"]),
                                (" / 카카오 신규 %d화" % kakao_result["downloaded"]) if kakao_result else "",
                                " (도중 취소됨)" if cancelled else "")})
        return {"scan": scan_result, "download": dl_result, "kakao": kakao_result}
    except Exception as e:  # noqa: BLE001
        log("파이프라인 실행 중 오류: %s" % e)
        ss.save_job_state({"running": False, "stage": "error", "finished_at": time.time(),
                            "last_error": str(e)})
        raise
