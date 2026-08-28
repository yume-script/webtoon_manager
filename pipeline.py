# -*- coding: utf-8 -*-
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


def run_scan(cfg, log=print):
    """요일별+완결+등록된 태그 목록을 긁어 titles.json을 갱신하고,
    작가/태그 자동구독 대상이면 subscribed=True로 표시한다.
    완결로 새로 바뀐(구독중이던) 작품은 finished_events 로 반환한다."""
    session = build_session_from_cfg(cfg)
    ss.save_job_state({"stage": "scanning", "message": "요일별 목록 수집 중"})
    log("요일별 연재 목록 수집 시작")
    merged = {}
    try:
        merged.update(naver_api.fetch_weekday_titles(session))
    except Exception as e:  # noqa: BLE001
        log("요일별 목록 수집 실패: %s" % e)

    ss.save_job_state({"message": "완결 목록 수집 중"})
    log("완결 목록 수집 시작")
    try:
        merged.update(naver_api.fetch_finished_titles(session))
    except Exception as e:  # noqa: BLE001
        log("완결 목록 수집 실패: %s" % e)

    at = ss.load_authors_tags()
    for tag in at.get("tags", []):
        ss.save_job_state({"message": "태그 '%s' 목록 수집 중" % tag})
        try:
            merged.update(naver_api.fetch_genre_titles(session, tag))
        except Exception as e:  # noqa: BLE001
            log("태그 '%s' 수집 실패: %s" % (tag, e))

    old_titles = ss.load_titles()
    finished_events = []
    author_names = set(a.strip() for a in at.get("authors", []) if a.strip())

    patch = {}
    for tid, item in merged.items():
        old = old_titles.get(tid, {})
        was_finished = old.get("status") == "완결"
        now_finished = item.get("status") == "완결"
        if now_finished and not was_finished and old.get("subscribed"):
            finished_events.append({"titleId": tid, "title": item.get("title") or old.get("title")})

        p = dict(item)
        # 기존 사용자 선택(구독/구독해제/제외)은 유지, 신규 발견 항목만 자동판단
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
        patch[tid] = p

    ss.upsert_title(patch)
    ss.save_job_state({"last_scan_at": time.time()})
    log("스캔 완료: 총 %d개 작품" % len(patch))

    if finished_events:
        for ev in finished_events:
            discord_notify.notify_finished(cfg, ev["title"], ev["titleId"])
    return {"scanned": len(patch), "finished_events": finished_events}


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
                # 목록 API가 이미 유료(charge=true)라고 알려주는 회차는 상세페이지
                # 요청 자체를 보내지 않고 건너뛴다 - "매일 하나씩 무료로 풀리는"
                # 방식일 수 있어 last_ok_no는 그대로 둬서 다음 스캔 때 다시 확인한다.
                log("titleId=%s %s화: 유료(charge=true) 회차, 목록 API 기준 건너뜀" % (tid, ep["no"]))
                ss.append_history({
                    "type": "skipped_paid", "title_id": tid, "title": t.get("title", tid),
                    "episode_no": ep["no"], "error": "유료 회차(목록 API charge=true)",
                })
                continue
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
                # "24시간마다 무료" 로테이션 방식일 수 있어 지금은 유료로 잠겨
                # 있어도 나중에 다시 무료로 풀릴 수 있다. 그래서 last_ok_no는
                # 건드리지 않는다(= 다음 스캔 때 이 회차부터 다시 시도됨).
                # 이번 실행에서만 건너뛰고 다음 회차로 넘어간다.
                log("titleId=%s %s화: %s (이번엔 건너뜀, 다음 스캔 때 재시도)" % (tid, ep["no"], e))
                ss.append_history({
                    "type": "skipped_paid", "title_id": tid, "title": t.get("title", tid),
                    "episode_no": ep["no"], "error": str(e),
                })
                continue

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


def run_full_cycle(cfg, log=print):
    ss.save_job_state({"running": True, "started_at": time.time(), "last_error": None})
    try:
        scan_result = run_scan(cfg, log=log)
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
