# -*- coding: utf-8 -*-
"""
webtoon_manager
---------------
GitHub murianwind/webtoon-manager(네이버웹툰 무료 회차 자동 구독/다운로드
독립 웹앱)를 BookOasis 카테고리탭 플러그인으로 이식.

기능: 작가/태그 자동 구독, 신작 자동 다운로드, 완결 감지/쿠키 만료 디스코드
알림, 주기 실행 스케줄러, 구독해제/제외 관리, 수동 다운로드, 다운로드 이력.

화면: index.html(카테고리탭 풀페이지) + script.js + style.css.

데이터: get_dashboard_data()가 전체 상태를 한 번에 반환하고, 화면에서는
탭별로 클라이언트 사이드 필터링만 한다(작품 수가 매우 많지 않다는 전제).

액션(구독/다운로드/설정 등)은 apply(db_type, book_id=0, item_data)를
범용 RPC 채널로 사용한다 (rclone_g2g_copy 플러그인과 동일한 패턴).
"""
import json
import threading
import time

from plugins.metadata.base import BaseMetadataProvider

# NOTE: 이 저장소(yume-script/webtoon_manager)는 core/ 서브패키지 없이
# 모든 모듈이 플러그인 루트에 flat 하게 있음. pipeline.py 등 다른 모듈들도
# 전부 `from . import ...` 형태로 서로를 참조하므로 여기도 동일하게 맞춤.
from . import state_store as ss
from . import pipeline
from . import scheduler
from . import discord_notify
from . import naver_api

PLUGIN_ID = "webtoon_manager"

DEFAULTS = {
    "ENABLE_SCHEDULER": False,
    "INTERVAL_MINUTES": 240,
    "MAX_NEW_EPISODES_PER_TITLE": 10,
    "BATCH_REST_MINUTES": 5.0,
    "MAX_CONCURRENT_DOWNLOADS": 5,
    "DELAY_SECONDS": 1.0,
    "REQUEST_TIMEOUT_SECONDS": 10,
    "FOLDER_ZERO_FILL": 4,
    "IMAGE_ZERO_FILL": 4,
}


class WebtoonManagerMetadataProvider(BaseMetadataProvider):
    id = PLUGIN_ID
    name = "웹툰 관리"
    is_searchable = False

    config_schema = [
        {"key": "NAVER_ID", "label": "네이버 아이디", "type": "text"},
        {"key": "NAVER_PW", "label": "네이버 비밀번호", "type": "password"},
        {"key": "NAVER_COOKIE_JSON", "label": "네이버 쿠키(JSON, storage_state 형식)",
         "type": "password",
         "required": False},
        {"key": "DOWNLOAD_ROOT", "label": "다운로드 저장 경로(비우면 플러그인 기본 경로)",
         "type": "text"},
        {"key": "ENABLE_SCHEDULER", "label": "자동 실행(스케줄러) 사용", "type": "checkbox",
         "default": False},
        {"key": "INTERVAL_MINUTES", "label": "실행 주기(분, 최소 10)", "type": "number",
         "default": 240},
        {"key": "MAX_NEW_EPISODES_PER_TITLE", "label": "1회 실행당 작품별 최대 신규 다운로드 회차 수(0=무제한)",
         "type": "number", "default": 10},
        {"key": "BATCH_REST_MINUTES", "label": "상한 도달 시 휴식(분)", "type": "number", "default": 5},
        {"key": "MAX_CONCURRENT_DOWNLOADS", "label": "이미지 동시 다운로드 수", "type": "number", "default": 5},
        {"key": "DELAY_SECONDS", "label": "회차 간 대기(초)", "type": "number", "default": 1.0},
        {"key": "REQUEST_TIMEOUT_SECONDS", "label": "요청 타임아웃(초)", "type": "number", "default": 10},
        {"key": "FOLDER_ZERO_FILL", "label": "회차 폴더명 자릿수", "type": "number", "default": 4},
        {"key": "IMAGE_ZERO_FILL", "label": "이미지 파일명 자릿수", "type": "number", "default": 4},
        {"key": "DISCORD_WEBHOOK_URL", "label": "디스코드 웹훅 URL(선택)", "type": "text"},
        {"key": "DISCORD_BOT_TOKEN", "label": "디스코드 봇 토큰(선택, 완결확인용)", "type": "password"},
        {"key": "DISCORD_CHANNEL_ID", "label": "디스코드 채널 ID(선택)", "type": "text"},
    ]

    # plugin_board(실제 동작 중인 참조 플러그인) 기준: 좌측 사이드바 1등 시민
    # 탭으로 등록하려면 category_tab이 True가 아니라 dict여야 한다
    # (title/icon/order 필드로 사이드바 메뉴 항목을 구성). dashboard_widget은
    # "공통 데스크" 카드용 별개 메커니즘이라 category_tab과 병행 선언하지 않는다.
    category_tab = {
        "title": "웹툰 관리",
        "icon": "fa-solid fa-book-open-reader",
        "order": 50,
    }

    update_manifest = {
        "enabled": True,
        "provider": "github-raw",
        "raw_base_url": "https://raw.githubusercontent.com/yume-script/webtoon_manager/main",
        "files": ["webtoon_manager.py", "__init__.py", "VERSION",
                  "index.html", "style.css", "script.js", "settings.html",
                  "requirements.txt",
                  "state_store.py", "naver_api.py",
                  "downloader.py", "discord_notify.py", "scheduler.py",
                  "pipeline.py"],
        "version_file": "VERSION",
        "version_key": "plugin version",
        "show_sample_update_button": True,
    }

    # ------------------------------------------------------------------
    # 필수 계약
    # ------------------------------------------------------------------
    def search(self, db_type, query):
        return {"success": True, "items": []}

    def apply(self, db_type, book_id, item_data):
        """plugin_board(실제 동작 확인된 참조 플러그인) 기준: apply()가 카테고리탭
        script.js의 실제 액션 RPC 진입점이다. book_id는 이 플러그인에서 의미
        없는 값(0 등)으로 넘어오며, 실제 라우팅은 item_data['action']으로 한다."""
        try:
            item_data = item_data or {}
            return self._dispatch(db_type, item_data.get("action"), item_data)
        except Exception as e:  # noqa: BLE001
            return False, "예상치 못한 오류가 발생했습니다: %s" % e

    # ------------------------------------------------------------------
    # 설정 헬퍼
    # ------------------------------------------------------------------
    def _get_cfg(self, db_type):
        cfg = self.get_plugin_config(db_type, default={}) or {}
        merged = dict(DEFAULTS)
        merged.update({k: v for k, v in cfg.items() if v not in (None, "")})
        if not merged.get("DOWNLOAD_ROOT"):
            merged["DOWNLOAD_ROOT"] = ss.DOWNLOAD_DEFAULT_DIR
        return merged

    def _save_cfg_patch(self, db_type, patch):
        cfg = self.get_plugin_config(db_type, default={}) or {}
        cfg.update(patch)
        try:
            self.set_plugin_config(db_type, cfg)
            return True
        except AttributeError:
            # 게이트웨이에 set_plugin_config가 없는 코어 버전 - db_gateway로 직접 저장 시도
            gw = self.get_db_gateway(db_type)
            gw.set_setting("PLUGIN_CONFIG_%s" % self.id, json.dumps(cfg, ensure_ascii=False))
            return True

    # ------------------------------------------------------------------
    # 대시보드/카테고리탭 데이터
    # ------------------------------------------------------------------
    def get_dashboard_data(self, db_type, limit=10):
        cfg = self._get_cfg(db_type)

        # 매 폴링마다 스케줄러가 떠 있는지 확인하고 없으면 기동
        try:
            scheduler.ensure_started(lambda: self._get_cfg(db_type), pipeline.run_full_cycle)
        except Exception:  # noqa: BLE001
            pass

        titles = ss.load_titles()
        items_list = []
        for tid, t in titles.items():
            item = dict(t)
            item["titleId"] = tid
            items_list.append(item)
        items_list.sort(key=lambda x: x.get("last_seen_at", 0), reverse=True)

        bundle = {
            "titles": items_list,
            "authors_tags": ss.load_authors_tags(),
            "history": ss.load_history(limit=200),
            "job": ss.load_job_state(),
            "log_tail": ss.tail_log(60),
            "config_public": {
                "NAVER_ID": cfg.get("NAVER_ID", ""),
                "DOWNLOAD_ROOT": cfg.get("DOWNLOAD_ROOT", ""),
                "ENABLE_SCHEDULER": bool(cfg.get("ENABLE_SCHEDULER")),
                "INTERVAL_MINUTES": cfg.get("INTERVAL_MINUTES"),
                "MAX_NEW_EPISODES_PER_TITLE": cfg.get("MAX_NEW_EPISODES_PER_TITLE"),
                "BATCH_REST_MINUTES": cfg.get("BATCH_REST_MINUTES"),
                "MAX_CONCURRENT_DOWNLOADS": cfg.get("MAX_CONCURRENT_DOWNLOADS"),
                "DELAY_SECONDS": cfg.get("DELAY_SECONDS"),
                "has_cookie": bool(cfg.get("NAVER_COOKIE_JSON")),
                "has_discord": bool(cfg.get("DISCORD_WEBHOOK_URL") or
                                     (cfg.get("DISCORD_BOT_TOKEN") and cfg.get("DISCORD_CHANNEL_ID"))),
            },
        }
        return {"success": True, "items": [bundle]}

    # ------------------------------------------------------------------
    # 범용 액션 채널 (index.html/script.js -> apply(book_id=0, item_data) 대체 경로)
    # 코어 apply()가 book_id 컨텍스트를 요구해 문제가 생기면, run_context_menu_action도
    # 동일 dispatch로 노출해 둔다(둘 중 실제로 라우팅되는 쪽을 프론트에서 쓰면 됨).
    # ------------------------------------------------------------------
    def get_context_menu_items(self, db_type, context):
        return []

    def run_context_menu_action(self, db_type, action_id, context):
        return self._dispatch(db_type, action_id, context or {})

    def _dispatch(self, db_type, action, payload):
        try:
            if action == "save_settings":
                return self._act_save_settings(db_type, payload)
            if action == "scan_now":
                return self._act_run_bg(db_type, pipeline.run_scan, "스캔")
            if action == "run_full_cycle_now":
                return self._act_run_bg(db_type, pipeline.run_full_cycle, "전체 실행")
            if action == "cancel_job":
                ss.save_job_state({"cancel_requested": True})
                return True, "취소 요청됨"
            if action == "subscribe":
                return self._act_set_flags(payload.get("titleId"), subscribed=True,
                                            excluded=False, unsubscribed=False)
            if action == "unsubscribe":
                return self._act_set_flags(payload.get("titleId"), subscribed=False,
                                            unsubscribed=True)
            if action == "exclude":
                return self._act_set_flags(payload.get("titleId"), subscribed=False,
                                            excluded=True)
            if action == "restore":
                return self._act_set_flags(payload.get("titleId"), subscribed=True,
                                            excluded=False, unsubscribed=False)
            if action == "add_author":
                return self._act_authors_tags("authors", payload.get("value"), add=True)
            if action == "remove_author":
                return self._act_authors_tags("authors", payload.get("value"), add=False)
            if action == "add_tag":
                return self._act_authors_tags("tags", payload.get("value"), add=True)
            if action == "remove_tag":
                return self._act_authors_tags("tags", payload.get("value"), add=False)
            if action == "manual_lookup":
                return self._act_manual_lookup(db_type, payload.get("titleId"))
            if action == "manual_download":
                return self._act_manual_download(db_type, payload)
            if action == "test_discord":
                return self._act_test_discord(db_type)
            return False, "알 수 없는 action: %s" % action
        except Exception as e:  # noqa: BLE001
            return False, "오류: %s" % e

    def _act_save_settings(self, db_type, payload):
        patch = {k: v for k, v in payload.items() if k not in ("action",)}
        self._save_cfg_patch(db_type, patch)
        return True, "설정 저장됨"

    def _act_run_bg(self, db_type, func, label):
        job = ss.load_job_state()
        if job.get("running"):
            return False, "이미 실행 중인 작업이 있습니다"

        cfg = self._get_cfg(db_type)
        ss.save_job_state({"running": True, "stage": "starting", "message": "%s 시작" % label,
                            "started_at": time.time(), "cancel_requested": False,
                            "last_error": None})

        def _runner():
            try:
                func(cfg, log=ss.append_log)
            except Exception as e:  # noqa: BLE001
                ss.append_log("%s 실행 실패: %s" % (label, e))
                ss.save_job_state({"running": False, "stage": "error", "last_error": str(e)})

        t = threading.Thread(target=_runner, name="webtoon_manager_%s" % action_slug(label),
                              daemon=True)
        t.start()
        return True, "%s 시작됨(백그라운드)" % label

    def _act_set_flags(self, title_id, **flags):
        if not title_id:
            return False, "titleId 필요"
        ss.upsert_title({str(title_id): flags})
        return True, "적용됨"

    def _act_authors_tags(self, key, value, add):
        value = (value or "").strip()
        if not value:
            return False, "값을 입력하세요"
        at = ss.load_authors_tags()
        items = at.get(key, [])
        if add:
            if value not in items:
                items.append(value)
        else:
            items = [i for i in items if i != value]
        at[key] = items
        ss.save_authors_tags(at)
        return True, json.dumps(at, ensure_ascii=False)

    def _act_manual_lookup(self, db_type, title_id):
        if not title_id:
            return False, "titleId 필요"
        cfg = self._get_cfg(db_type)
        session = pipeline.build_session_from_cfg(cfg)
        try:
            meta = naver_api.guess_title_meta(session, title_id)
            episodes = naver_api.fetch_episode_list(session, title_id, max_pages=3)
            meta["episodes"] = episodes[:60]
            return True, json.dumps(meta, ensure_ascii=False)
        except Exception as e:  # noqa: BLE001
            return False, "조회 실패: %s" % e

    def _act_manual_download(self, db_type, payload):
        title_id = payload.get("titleId")
        title = payload.get("title") or title_id
        episode_nos = payload.get("episodeNos") or []
        if not title_id or not episode_nos:
            return False, "titleId/episodeNos 필요"

        job = ss.load_job_state()
        if job.get("running"):
            return False, "이미 실행 중인 작업이 있습니다"

        cfg = self._get_cfg(db_type)
        ss.save_job_state({"running": True, "stage": "downloading",
                            "message": "수동 다운로드 시작", "started_at": time.time(),
                            "cancel_requested": False, "last_error": None,
                            "progress": 0, "total": len(episode_nos)})

        def _runner():
            from . import downloader as dl
            session = pipeline.build_session_from_cfg(cfg)
            ok_count = 0
            for i, no in enumerate(episode_nos):
                if ss.load_job_state().get("cancel_requested"):
                    ss.append_log("수동 다운로드 취소됨")
                    break
                ss.save_job_state({"progress": i, "message": "%s %s화 다운로드 중" % (title, no)})
                try:
                    ok, skipped, cnt, err = dl.download_episode(
                        session, cfg.get("DOWNLOAD_ROOT") or ss.DOWNLOAD_DEFAULT_DIR,
                        title, title_id, no,
                        image_zero_fill=int(cfg.get("IMAGE_ZERO_FILL", 4)),
                        folder_zero_fill=int(cfg.get("FOLDER_ZERO_FILL", 4)),
                        max_concurrent=int(cfg.get("MAX_CONCURRENT_DOWNLOADS", 5)),
                        delay_seconds=float(cfg.get("DELAY_SECONDS", 1.0)),
                        timeout=int(cfg.get("REQUEST_TIMEOUT_SECONDS", 10)),
                        log=ss.append_log)
                    if ok:
                        ok_count += 1 if not skipped else 0
                        ss.append_history({"type": "manual_download", "title_id": title_id,
                                            "title": title, "episode_no": no,
                                            "image_count": cnt})
                    else:
                        ss.append_history({"type": "manual_download_fail", "title_id": title_id,
                                            "title": title, "episode_no": no, "error": err})
                except naver_api.NaverAuthExpired as e:
                    ss.append_log("인증 만료: %s" % e)
                    discord_notify.notify_cookie_expired(cfg)
                    break
            ss.save_job_state({"running": False, "stage": "done", "finished_at": time.time(),
                                "message": "수동 다운로드 완료(%d화)" % ok_count})

        t = threading.Thread(target=_runner, name="webtoon_manager_manual_dl", daemon=True)
        t.start()
        return True, "수동 다운로드 시작됨(백그라운드)"

    def _act_test_discord(self, db_type):
        cfg = self._get_cfg(db_type)
        ok, msg = discord_notify.notify(cfg, "🔔 웹툰 관리 플러그인 테스트",
                                         "이 메시지가 보이면 디스코드 알림 설정이 정상입니다.")
        return ok, msg


def action_slug(label):
    return "".join(c for c in label if c.isalnum()) or "job"
