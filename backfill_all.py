# -*- coding: utf-8 -*-
"""
backfill_all.py
---------------
구독중인 웹툰 전체를 대상으로, **이미 받아둔 회차는 건너뛰고 나머지 전 회차를
처음부터 끝까지** 내려받는 일괄 백필(backfill) 스크립트입니다.

카테고리탭의 자동 다운로드는 "마지막으로 받은 회차(last_downloaded_no) 이후의
새 회차"만 확인하기 때문에, 중간에 구독한 작품은 과거 회차가 통째로 비어 있습니다.
이 스크립트는 그 빈 구간을 한 번에 채우는 용도입니다.

사용법 (BookOasis 컨테이너 안에서 실행):

    # 1) 먼저 무엇을 받을지만 확인 (아무것도 안 받음)
    docker exec -it bookoasis python3 \
        /app/plugins/metadata/webtoon_manager/backfill_all.py --dry-run

    # 2) 실제 실행
    docker exec -it bookoasis python3 \
        /app/plugins/metadata/webtoon_manager/backfill_all.py

주요 옵션:
    --dry-run              실제로 받지 않고 대상 회차 수만 집계해서 출력
    --title-id 808389      특정 작품 하나만 (여러 번 지정 가능)
    --include-unsubscribed 구독중이 아닌 작품까지 전부 대상에 포함(제외됨은 항상 빼고)
    --limit-per-title 50   작품당 최대 N화까지만 (기본: 무제한)
    --max-titles 10        이번 실행에서 처리할 작품 수 상한
    --delay 1.5            회차 사이 대기 초 (기본: 플러그인 설정값)
    --db-type adult        성인 서재 스코프 설정을 사용
    --no-lock              플러그인 작업 잠금을 잡지 않음(권장하지 않음)

동작 규칙:
    - 이미 압축이 끝난 회차(zip 존재)는 네트워크 요청 없이 즉시 건너뜁니다.
    - 유료(코인) 회차는 건너뛰고, 그 뒤 회차는 계속 확인합니다(로테이션 무료화 대비).
    - 오래된 회차 -> 최신 회차 순서로 받습니다.
    - 쿠키 만료(NaverAuthExpired)가 감지되면 즉시 중단합니다.
    - 실행 중에는 플러그인 작업 잠금을 잡아서, 스케줄러 자동 다운로드와 동시에
      돌지 않게 합니다(Ctrl+C로 중단해도 finally에서 잠금을 풀어줍니다).
    - 카테고리탭 "취소" 버튼을 누르면 이 스크립트도 다음 회차 시작 전에 멈춥니다.
"""
import argparse
import os
import sys
import time

# 이 파일은 /app/plugins/metadata/webtoon_manager/backfill_all.py 에 있다.
# 플러그인 모듈들이 `from . import xxx` 상대 임포트를 쓰므로, 패키지 경로
# (plugins.metadata.webtoon_manager)로 임포트할 수 있도록 /app 을 sys.path에
# 넣어준다.
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_APP_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(_THIS_DIR)))
if _APP_ROOT not in sys.path:
    sys.path.insert(0, _APP_ROOT)

from plugins.metadata.webtoon_manager import downloader  # noqa: E402
from plugins.metadata.webtoon_manager import naver_api  # noqa: E402
from plugins.metadata.webtoon_manager import pipeline  # noqa: E402
from plugins.metadata.webtoon_manager import state_store as ss  # noqa: E402


def log(msg):
    print(msg, flush=True)


def load_cfg(db_type, args):
    """플러그인 설정(쿠키/경로 등)을 가져온다. DB 게이트웨이는 Flask 앱
    컨텍스트가 필요할 수 있어, 실패하면 CLI 인자/기본값으로 폴백한다."""
    cfg = {}
    try:
        from plugins.metadata.webtoon_manager.webtoon_manager import (
            WebtoonManagerMetadataProvider)
        cfg = WebtoonManagerMetadataProvider()._get_cfg(db_type) or {}
        log("플러그인 설정을 DB에서 읽었습니다 (db_type=%s)" % db_type)
    except Exception as e:  # noqa: BLE001
        log("[주의] 플러그인 설정을 DB에서 읽지 못했습니다(%s) - 기본값/CLI 인자를 사용합니다." % e)
        log("       쿠키가 필요한 성인/로그인 작품은 실패할 수 있습니다.")

    if args.download_root:
        cfg["DOWNLOAD_ROOT"] = args.download_root
    if args.temp_root:
        cfg["TEMP_DOWNLOAD_ROOT"] = args.temp_root
    if args.cookie_json:
        try:
            with open(args.cookie_json, "r", encoding="utf-8") as f:
                cfg["NAVER_COOKIE_JSON"] = f.read()
            log("쿠키 JSON 파일을 읽었습니다: %s" % args.cookie_json)
        except OSError as e:
            log("[에러] 쿠키 파일을 읽지 못했습니다: %s" % e)
            sys.exit(1)
    if args.delay is not None:
        cfg["DELAY_SECONDS"] = args.delay

    cfg.setdefault("DOWNLOAD_ROOT", ss.DOWNLOAD_DEFAULT_DIR)
    cfg.setdefault("TEMP_DOWNLOAD_ROOT", ss.TMP_DOWNLOAD_DEFAULT_DIR)
    return cfg


def pick_titles(args):
    """대상 작품 목록을 titles.json에서 고른다."""
    titles = ss.load_titles()
    wanted_ids = set(str(t) for t in (args.title_id or []))
    out = []
    for tid, t in titles.items():
        if wanted_ids:
            if str(tid) in wanted_ids:
                out.append((str(tid), t))
            continue
        if t.get("excluded"):
            continue
        if not args.include_unsubscribed:
            if not t.get("subscribed") or t.get("unsubscribed"):
                continue
        out.append((str(tid), t))
    out.sort(key=lambda kv: str(kv[1].get("title") or ""))
    return out


def backfill_title(session, cfg, tid, t, args, stats):
    """작품 하나의 미보유 회차를 전부 받는다. 반환: 'ok' | 'auth_expired' | 'cancelled'"""
    title = t.get("title", tid)
    download_root = cfg["DOWNLOAD_ROOT"]
    temp_root = cfg["TEMP_DOWNLOAD_ROOT"]
    folder_zero_fill = int(cfg.get("FOLDER_ZERO_FILL", 4))

    try:
        episodes = naver_api.fetch_episode_list(session, tid, max_pages=200)
    except naver_api.NaverAuthExpired as e:
        log("  [인증 만료] %s" % e)
        return "auth_expired"
    except Exception as e:  # noqa: BLE001
        log("  [에러] 회차 목록 조회 실패: %s" % e)
        stats["errors"] += 1
        return "ok"

    # fetch_episode_list는 최신->과거 순이라 뒤집어서 1화부터 받는다.
    episodes = sorted(episodes, key=lambda e: e.get("no") or 0)

    missing = []
    for ep in episodes:
        no = ep.get("no")
        if not isinstance(no, int):
            continue
        if downloader.find_existing_episode_archive(
                download_root, title, tid, no, folder_zero_fill):
            stats["already"] += 1
            continue
        missing.append(ep)

    if args.limit_per_title and len(missing) > args.limit_per_title:
        missing = missing[:args.limit_per_title]

    log("  전체 %d화 / 이미 보유분 제외하고 받을 대상: %d화" % (len(episodes), len(missing)))
    if not missing:
        return "ok"

    if args.dry_run:
        free = [e for e in missing if not e.get("charge")]
        paid = len(missing) - len(free)
        stats["would_download"] += len(free)
        stats["paid"] += paid
        preview = ", ".join(str(e.get("no")) for e in free[:10])
        more = "" if len(free) <= 10 else " ...외 %d화" % (len(free) - 10)
        log("  [dry-run] 받을 회차(무료 %d화): %s%s%s" %
            (len(free), preview, more,
             ("  / 유료라 건너뛸 회차 %d화" % paid) if paid else ""))
        return "ok"

    comicinfo_on = bool(cfg.get("GENERATE_COMICINFO_XML", True))
    if cfg.get("GENERATE_SERIES_JSON", True):
        downloader.write_series_json(
            download_root, title, tid, pipeline._series_json_meta_for(t, tid), log=log)

    last_ok_no = t.get("last_downloaded_no")
    for ep in missing:
        if not args.no_lock and ss.load_job_state().get("cancel_requested"):
            log("  취소 요청이 감지되어 중단합니다.")
            return "cancelled"

        no = ep["no"]
        if ep.get("charge"):
            log("  %s화: 유료 회차라 건너뜀" % no)
            stats["paid"] += 1
            continue

        try:
            ok, skipped, cnt, err = downloader.download_episode(
                session, download_root, temp_root, title, tid, no,
                image_zero_fill=int(cfg.get("IMAGE_ZERO_FILL", 4)),
                folder_zero_fill=folder_zero_fill,
                max_concurrent=int(cfg.get("MAX_CONCURRENT_DOWNLOADS", 5)),
                delay_seconds=float(cfg.get("DELAY_SECONDS", 1.0)),
                timeout=int(cfg.get("REQUEST_TIMEOUT_SECONDS", 10)),
                log=log)
        except naver_api.NaverAuthExpired as e:
            log("  [인증 만료] %s" % e)
            return "auth_expired"
        except naver_api.NaverPaidEpisode:
            log("  %s화: 유료 회차라 건너뜀" % no)
            stats["paid"] += 1
            continue
        except Exception as e:  # noqa: BLE001
            log("  %s화 실패: %s" % (no, e))
            stats["errors"] += 1
            continue

        if not ok:
            log("  %s화 실패: %s" % (no, err))
            stats["errors"] += 1
            continue

        c_ok, c_path, c_msg = downloader.compress_episode(
            download_root, temp_root, title, tid, no,
            folder_zero_fill=folder_zero_fill, log=log,
            zip_stored=bool(cfg.get("ZIP_STORED", True)),
            comicinfo_meta=pipeline._comicinfo_meta_for(t, ep, tid) if comicinfo_on else None)
        if not c_ok:
            log("  %s화 압축 실패: %s (다음 실행 때 재시도됨)" % (no, c_msg))
            stats["errors"] += 1
            continue

        stats["downloaded"] += 1
        log("  %s화 완료 (%d장)" % (no, cnt))
        ss.append_history({
            "type": "download", "source": "manual", "title_id": tid,
            "title": title, "episode_no": no, "subtitle": ep.get("subtitle"),
            "image_count": cnt,
        })
        if last_ok_no is None or no > last_ok_no:
            last_ok_no = no

    if last_ok_no != t.get("last_downloaded_no"):
        ss.upsert_title({tid: {"last_downloaded_no": last_ok_no}})
    return "ok"


def main():
    parser = argparse.ArgumentParser(
        description="구독중인 웹툰 전체의 미보유 회차를 일괄 다운로드")
    parser.add_argument("--dry-run", action="store_true",
                        help="실제로 받지 않고 대상 회차만 집계해서 출력")
    parser.add_argument("--title-id", action="append",
                        help="특정 titleId만 처리(여러 번 지정 가능)")
    parser.add_argument("--include-unsubscribed", action="store_true",
                        help="구독중이 아닌 작품도 대상에 포함(제외됨은 항상 제외)")
    parser.add_argument("--limit-per-title", type=int, default=0,
                        help="작품당 최대 다운로드 회차 수(0=무제한)")
    parser.add_argument("--max-titles", type=int, default=0,
                        help="이번 실행에서 처리할 작품 수 상한(0=무제한)")
    parser.add_argument("--delay", type=float, default=None,
                        help="회차 사이 대기 초(기본: 플러그인 설정값)")
    parser.add_argument("--db-type", default="general",
                        help="설정 스코프: general(기본) 또는 adult")
    parser.add_argument("--download-root", help="다운로드 저장 경로 직접 지정")
    parser.add_argument("--temp-root", help="임시 작업 경로 직접 지정")
    parser.add_argument("--cookie-json", help="네이버 쿠키 JSON 파일 경로(DB 설정 대신 사용)")
    parser.add_argument("--nice", type=int, default=10,
                        help="CPU 양보 정도(0~19, 기본 10). 0이면 양보하지 않음")
    parser.add_argument("--no-lock", action="store_true",
                        help="플러그인 작업 잠금을 잡지 않음(스케줄러와 동시 실행될 수 있음)")
    args = parser.parse_args()

    if args.nice:
        downloader.lower_thread_priority(args.nice)

    cfg = load_cfg(args.db_type, args)
    log("다운로드 저장 경로: %s" % cfg["DOWNLOAD_ROOT"])
    log("임시 작업 경로:     %s" % cfg["TEMP_DOWNLOAD_ROOT"])

    targets = pick_titles(args)
    if args.max_titles:
        targets = targets[:args.max_titles]
    if not targets:
        log("대상 작품이 없습니다. (구독중인 작품이 없거나 --title-id가 목록에 없음)")
        return

    log("대상 작품 %d개%s\n" % (len(targets), " (dry-run)" if args.dry_run else ""))

    acquired = False
    if not args.no_lock and not args.dry_run:
        acquired = ss.try_acquire_job({
            "stage": "downloading", "message": "전체 백필 스크립트 실행 중",
            "started_at": time.time(), "cancel_requested": False, "last_error": None,
        })
        if not acquired:
            log("[중단] 이미 실행 중인 작업이 있습니다. 카테고리탭에서 완료를 기다리거나")
            log("       취소한 뒤 다시 실행해주세요. (그래도 강행하려면 --no-lock)")
            sys.exit(1)

    session = pipeline.build_session_from_cfg(cfg)
    stats = {"downloaded": 0, "already": 0, "paid": 0, "errors": 0, "would_download": 0}
    started = time.time()

    try:
        for idx, (tid, t) in enumerate(targets, 1):
            log("=== [%d/%d] %s (titleId=%s) ===" % (idx, len(targets), t.get("title", tid), tid))
            if acquired:
                ss.save_job_state({
                    "progress": idx, "total": len(targets),
                    "message": "백필: %s" % t.get("title", tid)})
            result = backfill_title(session, cfg, tid, t, args, stats)
            if result == "auth_expired":
                log("\n[중단] 네이버 쿠키가 만료된 것으로 보입니다.")
                log("       카테고리탭 '도움말' 탭의 Cookie-Editor 안내대로 쿠키를 새로 넣고")
                log("       다시 실행해주세요.")
                break
            if result == "cancelled":
                break
    except KeyboardInterrupt:
        log("\n사용자가 중단했습니다(Ctrl+C). 받다 만 회차는 다음 실행 때 이어받습니다.")
    finally:
        if acquired:
            ss.save_job_state({
                "running": False, "stage": "done", "finished_at": time.time(),
                "message": "전체 백필 완료: 신규 %d화" % stats["downloaded"]})

    elapsed = int(time.time() - started)
    log("\n========================================")
    if args.dry_run:
        log("[dry-run] 받게 될 회차: 총 %d화 (이미 보유: %d화)" %
            (stats["would_download"], stats["already"]))
        log("실제로 받으려면 --dry-run 없이 다시 실행하세요.")
    else:
        log("완료: 신규 %d화 / 이미 보유 %d화 / 유료 건너뜀 %d화 / 실패 %d건 (%d분 %d초)" %
            (stats["downloaded"], stats["already"], stats["paid"], stats["errors"],
             elapsed // 60, elapsed % 60))
        if stats["errors"]:
            log("실패한 회차는 다시 실행하면 이어서 재시도됩니다.")


if __name__ == "__main__":
    main()
