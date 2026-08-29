# -*- coding: utf-8 -*-
import os
import time

from . import naver_api, downloader, discord_notify, state_store as ss

# 한 작품 안에서 연속으로 이 횟수만큼 다운로드가 실패하면(네이버 일시 차단/
# 레이트리밋 가능성) 남은 회차는 포기하고 다음 작품으로 넘어간다.
_MAX_CONSECUTIVE_FAILURES = 3


def _cfg_num(cfg, key, default):
    try:
        v = cfg.get(key)
        return float(v) if v not in (None, "") else default
    except (TypeError, ValueError):
        return default


def build_session_from_cfg(cfg):
    return naver_api.build_session(
        cookie_storage_state_json=cfg.get("NAVER_COOKIE_JSON"),
        naver_id=cfg.get("NAVER_ID"),
        naver_pw=cfg.get("NAVER_PW"),
        timeout=int(_cfg_num(cfg, "REQUEST_TIMEOUT_SECONDS", 10)),
    )


def _autosubscribe_patch(item, old, author_names):
    """신규 발견 항목이면 작가 자동구독 여부를 판단하고, 기존에 사용자가
    직접 구독/제외/구독해제한 적 있는 항목이면 그 선택을 그대로 유지한다."""
    p = dict(item)
    if "subscribed" in old or "excluded" in old:
        p["subscribed"] = old.get("subscribed", False)
        p["excluded"] = old.get("excluded", False)
        p["unsubscribed"] = old.get("unsubscribed", False)
    else:
        auto = False
        if item.get("author") and any(a in item.get("author", "") for a in author_names):
            auto = True
        p["subscribed"] = auto
        p["excluded"] = False
        p["unsubscribed"] = False
    p["last_seen_at"] = time.time()
    return p


def run_scan_weekday(cfg, log=print):
    """빠른 스캔: 요일별 연재중 목록 + 등록된 태그(작가/장르) 자동구독 대상만
    수집한다. 완결 전체 목록(최대 200페이지라 느림)은 포함하지 않는다 -
    그건 run_scan_finished()가 별도 스케줄로 처리한다. 초기 설치 시 이 스캔만
    먼저 빠르게 끝나서 카테고리탭이 바로 쓸만해지도록 하기 위해 분리했다."""
    def _cancelled():
        return bool(ss.load_job_state().get("cancel_requested"))

    session = build_session_from_cfg(cfg)
    ss.save_job_state({"stage": "scanning", "message": "요일별 목록 수집 중"})
    log("요일별 연재 목록 수집 시작")
    merged = {}
    try:
        merged.update(naver_api.fetch_weekday_titles(session, should_cancel=_cancelled))
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
    patch = {tid: _autosubscribe_patch(item, old_titles.get(tid, {}), author_names)
             for tid, item in merged.items()}

    ss.upsert_title(patch)
    if _cancelled():
        log("요일별 스캔 취소됨 - 지금까지 모은 %d개 작품만 반영" % len(patch))
    else:
        ss.save_job_state({"last_scan_at": time.time()})
        log("요일별 스캔 완료: 총 %d개 작품" % len(patch))
    return {"scanned": len(patch)}


def run_scan_finished(cfg, log=print, max_pages=200):
    """느린 스캔: 완결 전체 목록(최대 200페이지, 페이지당 0.2초 대기)을 수집한다.
    구독중이던 작품이 완결로 새로 바뀌면 디스코드로 알림을 보낸다.
    자동 스케줄러는 이 함수를 하루 중 정해진 시각(FINISHED_SCAN_HOUR)에
    한 번만 호출한다(scheduler.py 참고) - 매 주기마다 돌리기엔 너무 느려서
    초기 설치 시 전체 인덱싱이 오래 걸리는 원인이었다."""
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
    ss.save_job_state({"last_finished_scan_at": time.time()})
    log("완결 스캔 완료: 총 %d개 작품" % len(patch))

    if finished_events:
        for ev in finished_events:
            discord_notify.notify_finished(cfg, ev["title"], ev["titleId"])
    return {"scanned": len(patch), "finished_events": finished_events}


def run_scan(cfg, log=print):
    """수동 "지금 스캔" 버튼 등 하위호환용 - 요일별+완결을 순서대로 모두 수행.
    사용자가 명시적으로 호출한 경우에만 쓰고, 자동 스케줄러는
    run_scan_weekday/run_scan_finished를 각자 다른 주기로 따로 호출한다."""
    weekday_result = run_scan_weekday(cfg, log=log)
    finished_result = run_scan_finished(cfg, log=log)
    return {
        "scanned": weekday_result["scanned"] + finished_result["scanned"],
        "finished_events": finished_result["finished_events"],
    }


def _episodes_to_download(session, cfg, title_id, known_last_no):
    episodes = naver_api.fetch_episode_list(session, title_id)
    # 최신 -> 과거 순으로 오므로 known_last_no보다 큰(새 회차)만, 오래된 순으로 반환
    new_eps = [e for e in episodes if isinstance(e.get("no"), int) and e["no"] > (known_last_no or 0)]
    new_eps.sort(key=lambda e: e["no"])
    return new_eps


def run_download_cycle(cfg, log=print):
    session = build_session_from_cfg(cfg)
    titles = ss.load_titles()
    subscribed = {tid: t for tid, t in titles.items()
                  if t.get("subscribed") and not t.get("excluded") and not t.get("unsubscribed")}

    download_root = cfg.get("DOWNLOAD_ROOT") or ss.DOWNLOAD_DEFAULT_DIR
    max_new = int(_cfg_num(cfg, "MAX_NEW_EPISODES_PER_TITLE", 10))
    batch_rest_min = _cfg_num(cfg, "BATCH_REST_MINUTES", 5.0)
    max_concurrent = int(_cfg_num(cfg, "MAX_CONCURRENT_DOWNLOADS", 5))
    delay_seconds = _cfg_num(cfg, "DELAY_SECONDS", 1.0)
    image_zero_fill = int(_cfg_num(cfg, "IMAGE_ZERO_FILL", 4))
    folder_zero_fill = int(_cfg_num(cfg, "FOLDER_ZERO_FILL", 4))
    timeout = int(_cfg_num(cfg, "REQUEST_TIMEOUT_SECONDS", 10))

    ss.save_job_state({"stage": "downloading", "message": "구독 작품 회차 확인 중",
                        "progress": 0, "total": len(subscribed)})

    failures = []
    downloaded_count = 0
    cookie_expired = False

    for i, (tid, t) in enumerate(subscribed.items()):
        ss.save_job_state({"progress": i, "message": "%s 새 회차 확인 중" % t.get("title", tid)})
        try:
            new_eps = _episodes_to_download(session, cfg, tid, t.get("last_downloaded_no"))
        except Exception as e:  # noqa: BLE001
            log("회차 목록 조회 실패 titleId=%s: %s" % (tid, e))
            continue

        if not new_eps:
            continue

        capped = new_eps if max_new <= 0 else new_eps[:max_new]
        rest_needed = max_new > 0 and len(new_eps) > max_new

        last_ok_no = t.get("last_downloaded_no")
        consecutive_fail = 0
        for ep in capped:
            if ep.get("charge"):
                # 목록 API가 이미 유료(charge=true)라고 알려주는 회차를 만나면,
                # 그 뒤 회차들도 순서대로 계속 유료일 가능성이 매우 높다("매일
                # 하나씩 풀기" 방식은 오래된 순서대로 풀리므로). 남은 회차를
                # 하나씩 다 확인하지 말고 이 작품은 여기서 접고 다음 작품으로.
                # last_ok_no는 그대로 둬서 다음 스캔 때 이 회차부터 다시 확인한다.
                log("titleId=%s %s화: 유료(charge=true) 회차, 목록 API 기준 - 이후 회차도 유료로 보고 이 작품은 중단" % (tid, ep["no"]))
                ss.append_history({
                    "type": "skipped_paid", "title_id": tid, "title": t.get("title", tid),
                    "episode_no": ep["no"], "error": "유료 회차(목록 API charge=true)",
                })
                break
            try:
                ok, skipped, img_count, err = downloader.download_episode(
                    session, download_root, t.get("title", tid), tid, ep["no"],
                    image_zero_fill=image_zero_fill, folder_zero_fill=folder_zero_fill,
                    max_concurrent=max_concurrent, delay_seconds=delay_seconds,
                    timeout=timeout, log=log)
            except naver_api.NaverAuthExpired as e:
                log("인증 만료: %s" % e)
                cookie_expired = True
                break
            except naver_api.NaverPaidEpisode as e:
                # 마찬가지로 이후 회차도 계속 유료일 가능성이 높아 이 작품은
                # 여기서 접고 다음 작품으로 넘어간다("24시간마다 무료" 로테이션이
                # 있으니 last_ok_no는 안 건드려서 다음 스캔 때 다시 확인함).
                log("titleId=%s %s화: %s (이후 회차도 유료로 보고 이 작품은 중단, 다음 스캔 때 재시도)" % (tid, ep["no"], e))
                ss.append_history({
                    "type": "skipped_paid", "title_id": tid, "title": t.get("title", tid),
                    "episode_no": ep["no"], "error": str(e),
                })
                break

            if ok:
                consecutive_fail = 0
                last_ok_no = ep["no"]
                if not skipped:
                    downloaded_count += 1
                    ss.append_history({
                        "type": "download", "title_id": tid, "title": t.get("title", tid),
                        "episode_no": ep["no"], "subtitle": ep.get("subtitle"),
                        "image_count": img_count,
                    })
            else:
                consecutive_fail += 1
                failures.append({"title_id": tid, "title": t.get("title", tid),
                                  "episode_no": ep["no"], "error": err})
                ss.append_history({
                    "type": "download_fail", "title_id": tid, "title": t.get("title", tid),
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

        if last_ok_no != t.get("last_downloaded_no"):
            ss.upsert_title({tid: {"last_downloaded_no": last_ok_no,
                                    "up_flag": rest_needed}})

        if cookie_expired:
            break

        if rest_needed and batch_rest_min > 0:
            log("titleId=%s 회차가 많이 밀려 %.1f분 휴식" % (tid, batch_rest_min))
            time.sleep(min(batch_rest_min * 60, 60))  # 실제 배포 환경에서는 스케줄러가
            # 다음 주기에 이어받는 구조이므로 여기서는 과도한 슬립을 피하고 살짝만 쉼

    ss.save_job_state({"progress": len(subscribed), "message": "다운로드 사이클 종료"})

    if cookie_expired:
        discord_notify.notify_cookie_expired(cfg)

    if failures:
        discord_notify.notify_failures(cfg, failures)

    log("다운로드 사이클 완료: 신규 %d화, 실패 %d건" % (downloaded_count, len(failures)))
    return {"downloaded": downloaded_count, "failures": failures, "cookie_expired": cookie_expired}


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
        dl_result = run_download_cycle(cfg, log=log)
        ss.save_job_state({"running": False, "stage": "done", "finished_at": time.time(),
                            "message": "완료: 스캔 %d개 / 신규 %d화 / 실패 %d건" % (
                                scan_result["scanned"], dl_result["downloaded"],
                                len(dl_result["failures"]))})
        return {"scan": scan_result, "download": dl_result}
    except Exception as e:  # noqa: BLE001
        log("파이프라인 실행 중 오류: %s" % e)
        ss.save_job_state({"running": False, "stage": "error", "finished_at": time.time(),
                            "last_error": str(e)})
        raise
