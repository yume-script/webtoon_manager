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
    patch["subscribed"] = True
    if not existing:
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
        return res

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
        if full or ep["rented"] or ep["free"] or ep["no"] > checked_no or \
                (use_waitfree and ep["waitfree_ok"]):
            targets.append(ep)

    if not targets:
        ss.upsert_kakao_title({sid: {"last_result": "받을 새 회차 없음(보유 %d화)" % len(have),
                                     "last_result_at": time.time()}})
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

    for i, ep in enumerate(targets):
        if cancel_check and cancel_check():
            res["cancelled"] = True
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
        except kakao_api.KakaoUnsupported as e:
            log("%s: %s - 이 작품은 중단" % (title, e))
            res["failures"].append({"title": title, "title_id": sid, "episode_no": no, "error": str(e)})
            break
        except kakao_api.KakaoNotPurchased:
            res["locked"] += 1
            locked_run += 1
            max_checked = max(max_checked, no)
            if locked_run >= (MAX_CONSECUTIVE_LOCKED if not full else 20):
                log("%s: 볼 수 없는 회차가 연속 %d개 - 이번 실행은 여기까지" % (title, locked_run))
                break
            continue
        except Exception as e:  # noqa: BLE001
            res["failures"].append({"title": title, "title_id": sid, "episode_no": no, "error": str(e)})
            log("%s %d화 실패: %s" % (title, no, e))
            continue

        locked_run = 0
        max_checked = max(max_checked, no)
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
        ss.append_history({"type": "download", "source": "kakao", "platform": "kakao",
                           "title_id": sid, "title": "[카카오] %s" % title, "episode_no": no,
                           "subtitle": ep.get("subtitle"), "image_count": cnt})
        if last_ok is None or no > last_ok:
            last_ok = no

    summary = "신규 %d화" % res["downloaded"]
    if res["locked"]:
        summary += " / 볼 수 없는 회차 %d" % res["locked"]
    if res["failures"]:
        summary += " / 실패 %d" % len(res["failures"])
    if res["auth_expired"]:
        summary += " / 쿠키 만료"
    ss.upsert_kakao_title({sid: {"last_downloaded_no": last_ok, "checked_no": max_checked,
                                 "last_result": summary, "last_result_at": time.time()}})

    if cfg.get("GENERATE_KAVITA_YAML", True):
        kavita_yaml.write_kavita_yaml(
            root, sid, session=session, embed_cover=bool(cfg.get("KAVITA_YAML_EMBED_COVER", True)),
            log=log, platform=PLATFORM)
    return res


def run_kakao_cycle(cfg, log=print, manage_job=False):
    """구독 중인 카카오페이지 작품 전체를 확인한다(자동 사이클/'카카오 전체 실행')."""
    total = {"downloaded": 0, "locked": 0, "failures": [], "waitfree_used": 0,
             "auth_expired": False, "cancelled": False}
    if not (cfg.get("KAKAO_COOKIE") or "").strip():
        log("카카오페이지: 로그인 쿠키가 없어 무료 회차만 시도합니다.")
    titles = {k: v for k, v in ss.load_kakao_titles().items() if v.get("subscribed")}
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
    log(msg)
    ss.save_title_job_state({"running": False, "finished_at": time.time(), "message": msg,
                              "last_error": "쿠키 만료" if r["auth_expired"] else None})
    return r
