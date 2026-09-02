# -*- coding: utf-8 -*-
"""
webtoon_manager
---------------
GitHub murianwind/webtoon-manager(네이버웹툰 무료 회차 자동 구독/다운로드
독립 웹앱)를 BookOasis 카테고리탭 플러그인으로 이식.

기능: 작가/태그 자동 구독, 신작 자동 다운로드, 완결 감지/쿠키 만료 디스코드
알림, 주기 실행 스케줄러, 구독해제/제외 관리, 선택 회차 다운로드, 다운로드 이력.

화면: index.html(카테고리탭 풀페이지) + script.js + style.css.

데이터: get_dashboard_data()가 전체 상태를 한 번에 반환하고, 화면에서는
탭별로 클라이언트 사이드 필터링만 한다(작품 수가 매우 많지 않다는 전제).

액션(구독/다운로드/설정 등)은 apply(db_type, book_id=0, item_data)를
범용 RPC 채널로 사용한다 (rclone_g2g_copy 플러그인과 동일한 패턴).
"""
import json
import os
import threading
import time

import requests

from plugins.metadata.base import BaseMetadataProvider

# NOTE: 이 저장소(yume-script/webtoon_manager)는 core/ 서브패키지 없이
# 모든 모듈이 플러그인 루트에 flat 하게 있음. pipeline.py 등 다른 모듈들도
# 전부 `from . import ...` 형태로 서로를 참조하므로 여기도 동일하게 맞춤.
from . import state_store as ss
from . import pipeline
from . import scheduler
from . import discord_notify
from . import naver_api
from . import downloader

PLUGIN_ID = "webtoon_manager"

# 업데이트 가능 여부 배지용 상수. update_manifest의 raw_base_url/version_file과
# 같은 저장소를 가리키되, 여기서는 "지금 카테고리탭 헤더에 배지를 띄울지"만
# 판단하는 용도라 update_manifest와 별개로 자체 상수를 둔다(환경설정 화면의
# 샘플 업데이트 버튼 로직과 이 배지 체크는 서로 다른 코드 경로).
REPO_RAW_VERSION_URL = "https://raw.githubusercontent.com/yume-script/webtoon_manager/main/VERSION"
REPO_URL = "https://github.com/yume-script/webtoon_manager"
UPDATE_CHECK_INTERVAL_SECONDS = 3600  # 1시간마다 한 번만 GitHub 조회

_UPDATE_CHECK_THREAD_LOCK = threading.Lock()
_update_check_thread_active = False

DEFAULTS = {
    "ENABLE_SCHEDULER": False,
    "INTERVAL_MINUTES": 240,
    "FINISHED_SCAN_HOUR": 4,
    "MAX_NEW_EPISODES_PER_TITLE": 10,
    "BATCH_REST_MINUTES": 5.0,
    "MAX_CONCURRENT_DOWNLOADS": 5,
    "DELAY_SECONDS": 1.0,
    "REQUEST_TIMEOUT_SECONDS": 10,
    "FOLDER_ZERO_FILL": 4,
    "IMAGE_ZERO_FILL": 4,
}


class WebtoonManagerMetadataProvider(BaseMetadataProvider):
    id = "webtoon_manager"
    name = "웹툰 다운로더"
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
        {"key": "INTERVAL_MINUTES", "label": "실행 주기(분, 최소 10) - 요일별 스캔+다운로드", "type": "number",
         "default": 240},
        {"key": "FINISHED_SCAN_HOUR", "label": "완결 전체 목록 수집 시각(0~23시, 하루 1번)", "type": "number",
         "default": 4},
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
        "title": "웹툰 다운로더",
        "icon": "fa-solid fa-book-open-reader",
        "order": 50,
    }

    update_manifest = {
        "enabled": True,
        "provider": "github-raw",
        "raw_base_url": "https://raw.githubusercontent.com/yume-script/webtoon_manager/main",
        "files": ["webtoon_manager.py", "__init__.py", "VERSION",
                  "index.html", "style.css", "script.js",
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
        """BaseMetadataProvider 필수 계약(코드 grep으로 실제 확인됨: 코어의
        apply_book_metadata_api(book_id)는 book_id 기반 메타데이터 검색-적용
        흐름 전용이라 이 플러그인의 실제 액션 경로가 아니다). 카테고리탭의
        진짜 액션 RPC 진입점은 run_context_menu_action()
        (/api/media/context-menu/book/plugins/action)이며, apply()는 base
        계약을 만족시키기 위한 동일 로직의 폴백일 뿐이다. base.py 계약대로
        (bool, str) 튜플을 그대로 반환한다."""
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

    # ------------------------------------------------------------------
    # 대시보드/카테고리탭 데이터
    # ------------------------------------------------------------------
    def _read_version(self):
        """VERSION 파일에서 플러그인 버전을 읽어 헤더에 표시하기 위함.
        파일이 없거나 형식이 안 맞아도 화면은 그냥 비워두면 되니 예외를
        올리지 않는다."""
        try:
            path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "VERSION")
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            return data.get("plugin version") or data.get("version") or ""
        except Exception:  # noqa: BLE001
            return ""

    @staticmethod
    def _version_tuple(v):
        """'1.6.5' -> (1, 6, 5). 파싱 불가능한 조각은 0으로 취급해서 형식이
        살짝 다르더라도(예: 'v1.6.5', '1.6') 최대한 비교 가능하게 만든다."""
        if not v:
            return (0,)
        parts = []
        for p in str(v).strip().lstrip("vV").split("."):
            digits = "".join(c for c in p if c.isdigit())
            parts.append(int(digits) if digits else 0)
        return tuple(parts) if parts else (0,)

    def _check_update_available(self):
        """GitHub 원격 VERSION 파일과 로컬 VERSION을 비교해 업데이트 가능
        여부를 판단한다. 캐시가 신선하면(1시간 이내) 그대로 반환한다.
        캐시가 오래됐으면 백그라운드 스레드로 갱신을 "시작만" 시키고, 이번
        호출 자체는 (약간 오래됐을 수 있는) 캐시값을 즉시 반환한다 - 예전
        구현은 이 자리에서 동기적으로 requests.get()을 기다려서, GitHub 응답이
        느리거나 네트워크가 막혀 있으면 캐시가 갱신되는 그 1회의 대시보드
        폴링이 최대 6초(timeout)까지 지연되는 문제가 있었다."""
        cached = ss.load_update_check()
        checked_at = cached.get("checked_at")
        is_stale = not checked_at or (time.time() - checked_at) >= UPDATE_CHECK_INTERVAL_SECONDS
        if is_stale:
            self._maybe_start_background_update_check()
        return cached

    def _maybe_start_background_update_check(self):
        """이미 백그라운드 조회가 진행 중이면 새로 띄우지 않는다. 플러그인
        모듈이 요청마다 새로 로드될 수 있어(scheduler.py와 동일한 사정)
        프로세스 전역 플래그만으로는 완벽하지 않지만, 최악의 경우에도
        "가끔 중복으로 조회 한 번 더 나감" 정도라 update_check.json 자체의
        타임스탬프 갱신으로 곧 다시 정상화된다."""
        global _update_check_thread_active
        with _UPDATE_CHECK_THREAD_LOCK:
            if _update_check_thread_active:
                return
            _update_check_thread_active = True

        def _runner():
            global _update_check_thread_active
            try:
                self._fetch_and_save_update_check()
            finally:
                with _UPDATE_CHECK_THREAD_LOCK:
                    _update_check_thread_active = False

        threading.Thread(target=_runner, name="webtoon_manager_update_check", daemon=True).start()

    def _fetch_and_save_update_check(self):
        cached = ss.load_update_check()
        local_version = self._read_version()
        result = {
            "checked_at": time.time(),
            "local_version": local_version,
            "latest_version": cached.get("latest_version"),
            "update_available": False,
            "error": None,
        }
        try:
            resp = requests.get(REPO_RAW_VERSION_URL, timeout=6)
            resp.raise_for_status()
            data = resp.json()
            latest_version = data.get("plugin version") or data.get("version") or ""
            result["latest_version"] = latest_version
            if latest_version and local_version:
                result["update_available"] = self._version_tuple(latest_version) > self._version_tuple(local_version)
        except Exception as e:  # noqa: BLE001
            # 조회 실패 시 직전에 알던 latest_version/update_available은 그대로
            # 유지하고(캐시가 있었다면), 에러 사유만 갱신해서 다음 폴링 때
            # 재시도할 수 있게 한다.
            result["latest_version"] = cached.get("latest_version")
            result["update_available"] = bool(cached.get("update_available"))
            result["error"] = str(e)

        ss.save_update_check(result)
        return result

    def get_dashboard_data(self, db_type, limit=10):
        cfg = self._get_cfg(db_type)

        # 매 폴링마다 스케줄러가 떠 있는지 확인하고 없으면 기동
        try:
            scheduler.ensure_started(lambda: self._get_cfg(db_type),
                                      pipeline.run_full_cycle,
                                      pipeline.run_finished_scan_job)
        except Exception:  # noqa: BLE001
            pass

        update_status = self._check_update_available()

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
            "title_job": ss.load_title_job_state(),
            "log_tail": ss.tail_log(60),
            "plugin_version": self._read_version(),
            "update_status": update_status,
            "repo_url": REPO_URL,
            "config_public": {
                "NAVER_ID": cfg.get("NAVER_ID", ""),
                "DOWNLOAD_ROOT": cfg.get("DOWNLOAD_ROOT", ""),
                "ENABLE_SCHEDULER": bool(cfg.get("ENABLE_SCHEDULER")),
                "INTERVAL_MINUTES": cfg.get("INTERVAL_MINUTES"),
                "FINISHED_SCAN_HOUR": cfg.get("FINISHED_SCAN_HOUR"),
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
        """코어 라우트(/api/media/context-menu/book/plugins/action)는 이 메서드가
        dict({'success': bool, 'message'|'error': str})를 반환할 것으로 기대한다
        (튜플이면 '반환값 형식이 올바르지 않습니다'로 간주하고 HTTP 400을 내려버림).
        apply()와 달리 _dispatch()의 (bool, str) 튜플을 여기서 dict로 감싸준다."""
        ok, message = self._dispatch(db_type, action_id, context or {})
        if ok:
            return {"success": True, "message": message}
        return {"success": False, "error": message}

    def _dispatch(self, db_type, action, payload):
        try:
            if action == "scan_now":
                return self._act_run_bg(db_type, pipeline.run_scan_weekday, "요일별 스캔")
            if action == "scan_finished_now":
                return self._act_run_bg(db_type, pipeline.run_finished_scan_job, "완결 목록 수집")
            if action == "run_full_cycle_now":
                return self._act_run_bg(db_type, pipeline.run_full_cycle, "전체 실행(요일별+다운로드)")
            if action == "cancel_job":
                ss.save_job_state({"cancel_requested": True})
                return True, "취소 요청됨"
            if action == "cancel_title_job":
                ss.save_title_job_state({"cancel_requested": True})
                return True, "취소 요청됨"
            if action == "force_reset_job":
                return self._act_force_reset()
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
            if action == "resync_title":
                return self._act_resync_title(payload.get("titleId"))
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
            if action == "download_title":
                return self._act_download_title(db_type, payload.get("titleId"))
            if action == "test_discord":
                return self._act_test_discord(db_type)
            return False, "알 수 없는 action: %s" % action
        except Exception as e:  # noqa: BLE001
            return False, "오류: %s" % e

    def _act_force_reset(self):
        """job_state/title_job_state가 컨테이너 재시작 등으로 running=true인
        채 멈춘 "유령 상태"일 때, 실제 스레드가 죽어있어 취소 요청도 안 먹히는
        경우를 위한 최후 수단. 무조건 대기 상태로 되돌린다."""
        ss.save_job_state({"running": False, "stage": "idle", "message": "",
                            "cancel_requested": False, "last_error": None})
        ss.save_title_job_state({"running": False, "message": "",
                                  "cancel_requested": False, "last_error": None})
        ss.append_log("작업 상태가 강제로 초기화되었습니다.")
        return True, "작업 상태를 초기화했습니다."

    def _act_run_bg(self, db_type, func, label):
        # 예전에는 "읽어서 running 확인 -> (조금 뒤에) running=True 저장"을
        # 두 번의 별도 호출로 했는데, 그 사이 짧은 틈에 다른 요청이 끼어들면
        # 같은 종류의 작업이 동시에 두 개 시작될 수 있었다(TOCTOU 레이스).
        # try_acquire_job()이 확인+저장을 lock 안에서 원자적으로 처리한다.
        acquired = ss.try_acquire_job({
            "stage": "starting", "message": "%s 시작" % label,
            "started_at": time.time(), "cancel_requested": False, "last_error": None,
        })
        if not acquired:
            return False, "이미 실행 중인 작업이 있습니다"

        cfg = self._get_cfg(db_type)

        def _runner():
            try:
                func(cfg, log=ss.append_log)
            except Exception as e:  # noqa: BLE001
                ss.append_log("%s 실행 실패: %s" % (label, e))
                ss.save_job_state({"running": False, "stage": "error", "last_error": str(e)})
            else:
                # func 중 일부(run_scan_weekday, run_scan_finished 등)는 스스로
                # running을 내리지 않으므로, 여기서 항상 안전망으로 내려준다.
                # (이게 없으면 성공적으로 끝난 뒤에도 running이 계속 true로 남아
                # 다음 실행 시 "이미 실행 중인 작업이 있습니다"만 반복되는 버그가 있었음)
                job_now = ss.load_job_state()
                if job_now.get("running"):
                    ss.save_job_state({"running": False,
                                        "stage": "done" if not job_now.get("cancel_requested") else "cancelled"})

        t = threading.Thread(target=_runner, name="webtoon_manager_%s" % action_slug(label),
                              daemon=True)
        t.start()
        return True, "%s 시작됨(백그라운드)" % label

    def _act_set_flags(self, title_id, **flags):
        if not title_id:
            return False, "titleId 필요"
        ss.upsert_title({str(title_id): flags})
        return True, "적용됨"

    def _act_resync_title(self, title_id):
        """카드의 '다시 확인' 버튼: 이 작품의 '마지막으로 받은 회차 번호'
        기록을 지운다. 사용자가 다운로드 받은 파일을 직접 지운 경우, 자동
        다운로드는 이 번호보다 큰 회차만 확인하기 때문에 지워진 옛날 회차를
        다시 잡지 못하는데, 번호를 없애면 다음 확인 때 전체 회차를 다시
        훑는다 - 이미 있는 파일(디스크에 실제로 존재)은 빠르게 스킵되고,
        지워진 파일만 실제로 다시 다운로드된다."""
        if not title_id:
            return False, "titleId 필요"
        titles = ss.load_titles()
        t = titles.get(str(title_id))
        if not t:
            return False, "구독 목록에 없는 titleId입니다"
        ss.upsert_title({str(title_id): {"last_downloaded_no": None}})
        ss.append_log("%s: '다시 확인' 요청 - 다음 다운로드 때 전체 회차를 재확인합니다." %
                       t.get("title", title_id))
        return True, "다음 다운로드부터 전체 회차를 다시 확인합니다(이미 있는 파일은 스킵됨)"

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
            # 다운로드 여부 판정은 실제 다운로드 때 폴더/파일명에 쓰이는(그리고
            # 과거에 쓰였던) 제목 문자열을 기준으로 해야 한다. guess_title_meta()가
            # 상세페이지에서 새로 파싱한 제목이 titles.json에 저장된 제목과
            # 미묘하게 다르면(네이버 쪽 표기가 나중에 바뀌는 경우 등)
            # find_existing_episode_archive()의 파일명 접두어가 어긋나서, 이미
            # 받은 회차인데도 "다운로드 안 됨"으로 잘못 표시될 수 있다. 구독
            # 목록에 이미 있는 titleId라면 그때 실제로 쓰인 제목을 우선한다.
            stored = ss.load_titles().get(str(title_id)) or {}
            title_for_check = stored.get("title") or meta.get("title")

            # 회차가 많은 장기 연재작(10페이지 이상)도 전부 가져오도록 상한을
            # 넉넉히 잡는다. 화면은 스크롤 가능한 박스라 개수 제한이 필요 없다.
            episodes = naver_api.fetch_episode_list(session, title_id, max_pages=200)

            download_root = cfg.get("DOWNLOAD_ROOT") or ss.DOWNLOAD_DEFAULT_DIR
            folder_zero_fill = int(cfg.get("FOLDER_ZERO_FILL", 4))
            # find_existing_episode_archive()를 회차마다 부르면 그때그때
            # os.listdir()을 반복하게 되어(장기 연재작은 회차가 수백 개)
            # 비효율적이다. 여기서는 시리즈 폴더를 한 번만 읽어서 이미 있는
            # 파일명 집합을 만들고, 회차별로는 메모리에서만 접두어 매칭한다.
            series_dir = downloader.title_dir(download_root, title_for_check, title_id)
            existing_files = set(os.listdir(series_dir)) if os.path.isdir(series_dir) else set()
            for ep in episodes:
                no = ep.get("no")
                if isinstance(no, int):
                    prefix = downloader._archive_prefix(title_for_check, no, folder_zero_fill) + "#"
                    ep["downloaded"] = any(
                        f.startswith(prefix) and f.lower().endswith(".zip") for f in existing_files)
                else:
                    ep["downloaded"] = False

            meta["episodes"] = episodes
            return True, json.dumps(meta, ensure_ascii=False)
        except Exception as e:  # noqa: BLE001
            return False, str(e)

    def _act_manual_download(self, db_type, payload):
        title_id = payload.get("titleId")
        title = payload.get("title") or title_id
        episode_nos = payload.get("episodeNos") or []
        if not title_id or not episode_nos:
            return False, "titleId/episodeNos 필요"

        # 스캔/전체실행(job_state)과는 독립된 락(title_job_state)을 쓴다 —
        # 큰 작업이 도는 중에도 개별 작품 다운로드는 막히지 않게 하기 위함.
        # 개별 작품 다운로드끼리는 여전히 한 번에 하나만 허용(순차 처리).
        # try_acquire_title_job()으로 확인+저장을 원자적으로 처리해 TOCTOU
        # 레이스(짧은 틈에 두 다운로드가 동시에 시작되는 문제)를 없앤다.
        cfg = self._get_cfg(db_type)
        acquired = ss.try_acquire_title_job({
            "title_id": title_id, "title": title,
            "message": "선택 회차 다운로드 시작", "started_at": time.time(),
            "cancel_requested": False, "last_error": None,
            "progress": 0, "total": len(episode_nos),
        })
        if not acquired:
            tjob = ss.load_title_job_state()
            return False, "이미 다른 작품을 다운로드 중입니다(%s). 완료 후 다시 시도해주세요." % (
                tjob.get("title") or tjob.get("title_id") or "")

        _dl_root = cfg.get("DOWNLOAD_ROOT") or ss.DOWNLOAD_DEFAULT_DIR
        if _dl_root == ss.DOWNLOAD_DEFAULT_DIR:
            ss.append_log("이번 다운로드 경로(설정 안 됨 - 기본 경로 사용): %s" % _dl_root)
        else:
            ss.append_log("이번 다운로드 경로(설정값): %s" % _dl_root)

        def _runner():
            from . import downloader as dl
            session = pipeline.build_session_from_cfg(cfg)
            ok_count = 0
            consecutive_fail = 0
            for i, no in enumerate(episode_nos):
                if ss.load_title_job_state().get("cancel_requested"):
                    ss.append_log("선택 회차 다운로드 취소됨")
                    break
                ss.save_title_job_state({"progress": i, "message": "%s %s화 다운로드 중" % (title, no)})
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
                        consecutive_fail = 0
                        ok_count += 1 if not skipped else 0
                        ss.append_history({"type": "manual_download", "source": "manual",
                                            "title_id": title_id,
                                            "title": title, "episode_no": no,
                                            "image_count": cnt})
                        # 이미지 다운로드(1단계)와 분리된 2단계 - 별도로 압축한다.
                        # (스킵된 회차라도 "받아만 두고 압축 안 한" 상태일 수
                        # 있어 압축은 스킵 여부와 무관하게 항상 시도한다.
                        # compress_episode() 자체가 이미 압축돼 있으면 스킵함.)
                        c_ok, c_path, c_msg = dl.compress_episode(
                            cfg.get("DOWNLOAD_ROOT") or ss.DOWNLOAD_DEFAULT_DIR,
                            title, title_id, no,
                            folder_zero_fill=int(cfg.get("FOLDER_ZERO_FILL", 4)),
                            log=ss.append_log)
                        if not c_ok:
                            ss.append_log("%s %s화 압축 실패: %s" % (title, no, c_msg))
                    else:
                        consecutive_fail += 1
                        ss.append_history({"type": "manual_download_fail", "source": "manual",
                                            "title_id": title_id,
                                            "title": title, "episode_no": no, "error": err})
                        if consecutive_fail >= pipeline._MAX_CONSECUTIVE_FAILURES:
                            ss.append_log("연속 %d회 실패 - 일시 차단 가능성으로 중단" % consecutive_fail)
                            break
                except naver_api.NaverPaidEpisode as e:
                    ss.append_log("%s %s화: %s (건너뜀)" % (title, no, e))
                    ss.append_history({"type": "skipped_paid", "source": "manual",
                                        "title_id": title_id,
                                        "title": title, "episode_no": no, "error": str(e)})
                    continue
                except naver_api.NaverAuthExpired as e:
                    ss.append_log("인증 만료: %s" % e)
                    discord_notify.notify_cookie_expired(cfg)
                    break
            ss.save_title_job_state({"running": False, "finished_at": time.time(),
                                      "message": "선택 회차 다운로드 완료(%d화)" % ok_count})

        t = threading.Thread(target=_runner, name="webtoon_manager_manual_dl", daemon=True)
        t.start()
        return True, "선택 회차 다운로드 시작됨(백그라운드)"

    def _act_download_title(self, db_type, title_id):
        """구독중 카드의 '새회차 다운로드' 버튼: 회차를 직접 선택하지 않고,
        그 작품의 last_downloaded_no보다 새로운 회차를 자동으로 찾아 전부
        받는다(run_download_cycle과 같은 로직을 titleId 하나로 축소한 버전).
        스캔/전체실행(job_state)과는 독립된 title_job_state 락을 쓴다."""
        if not title_id:
            return False, "titleId 필요"

        cfg = self._get_cfg(db_type)
        titles = ss.load_titles()
        t_info = titles.get(str(title_id))
        if not t_info:
            return False, "구독 목록에 없는 titleId입니다"

        title_name = t_info.get("title", title_id)
        # try_acquire_title_job()으로 확인+저장을 원자적으로 처리해 TOCTOU
        # 레이스를 없앤다(_act_manual_download와 동일한 이유).
        acquired = ss.try_acquire_title_job({
            "title_id": title_id, "title": title_name,
            "message": "%s 새 회차 확인 중" % title_name,
            "started_at": time.time(), "cancel_requested": False,
            "last_error": None, "progress": 0, "total": 0,
        })
        if not acquired:
            tjob = ss.load_title_job_state()
            return False, "이미 다른 작품을 다운로드 중입니다(%s). 완료 후 다시 시도해주세요." % (
                tjob.get("title") or tjob.get("title_id") or "")

        _dl_root = cfg.get("DOWNLOAD_ROOT") or ss.DOWNLOAD_DEFAULT_DIR
        if _dl_root == ss.DOWNLOAD_DEFAULT_DIR:
            ss.append_log("이번 다운로드 경로(설정 안 됨 - 기본 경로 사용): %s" % _dl_root)
        else:
            ss.append_log("이번 다운로드 경로(설정값): %s" % _dl_root)

        def _runner():
            from . import downloader as dl
            session = pipeline.build_session_from_cfg(cfg)
            try:
                new_eps = pipeline._episodes_to_download(
                    session, cfg, str(title_id), t_info.get("last_downloaded_no"))
            except Exception as e:  # noqa: BLE001
                ss.append_log("회차 목록 조회 실패: %s" % e)
                ss.save_title_job_state({"running": False, "last_error": str(e)})
                return

            if not new_eps:
                ss.save_title_job_state({"running": False, "finished_at": time.time(),
                                          "message": "%s: 새 회차 없음" % title_name})
                return

            ss.save_title_job_state({"total": len(new_eps)})
            last_ok_no = t_info.get("last_downloaded_no")
            ok_count = 0
            consecutive_fail = 0
            for i, ep in enumerate(new_eps):
                if ss.load_title_job_state().get("cancel_requested"):
                    ss.append_log("다운로드 취소됨")
                    break
                if ep.get("charge"):
                    ss.append_log("%s %s화: 유료(charge=true) 회차, 목록 API 기준 - 이후 회차도 유료로 보고 중단" % (title_name, ep["no"]))
                    ss.append_history({"type": "skipped_paid", "source": "auto",
                                        "title_id": title_id,
                                        "title": title_name, "episode_no": ep["no"],
                                        "error": "유료 회차(목록 API charge=true)"})
                    break
                ss.save_title_job_state({"progress": i, "message": "%s %s화 다운로드 중" % (title_name, ep["no"])})
                try:
                    ok, skipped, cnt, err = dl.download_episode(
                        session, cfg.get("DOWNLOAD_ROOT") or ss.DOWNLOAD_DEFAULT_DIR,
                        title_name, title_id, ep["no"],
                        image_zero_fill=int(cfg.get("IMAGE_ZERO_FILL", 4)),
                        folder_zero_fill=int(cfg.get("FOLDER_ZERO_FILL", 4)),
                        max_concurrent=int(cfg.get("MAX_CONCURRENT_DOWNLOADS", 5)),
                        delay_seconds=float(cfg.get("DELAY_SECONDS", 1.0)),
                        timeout=int(cfg.get("REQUEST_TIMEOUT_SECONDS", 10)),
                        log=ss.append_log)
                except naver_api.NaverPaidEpisode as e:
                    # 이후 회차도 순서대로 계속 유료일 가능성이 높아 여기서 중단
                    # (다음 스캔/실행 때 다시 이 회차부터 확인).
                    ss.append_log("%s %s화: %s (이후 회차도 유료로 보고 중단, 다음에 재시도)" % (title_name, ep["no"], e))
                    ss.append_history({"type": "skipped_paid", "source": "auto",
                                        "title_id": title_id,
                                        "title": title_name, "episode_no": ep["no"], "error": str(e)})
                    break
                except naver_api.NaverAuthExpired as e:
                    ss.append_log("인증 만료: %s" % e)
                    discord_notify.notify_cookie_expired(cfg)
                    break
                if ok:
                    consecutive_fail = 0
                    last_ok_no = ep["no"]
                    if not skipped:
                        ok_count += 1
                        ss.append_history({"type": "download", "source": "auto",
                                            "title_id": title_id,
                                            "title": title_name, "episode_no": ep["no"],
                                            "image_count": cnt})
                        # 이미지 다운로드(1단계)와 분리된 2단계 - 별도로 압축한다.
                        c_ok, c_path, c_msg = dl.compress_episode(
                            cfg.get("DOWNLOAD_ROOT") or ss.DOWNLOAD_DEFAULT_DIR,
                            title_name, title_id, ep["no"],
                            folder_zero_fill=int(cfg.get("FOLDER_ZERO_FILL", 4)),
                            log=ss.append_log)
                        if not c_ok:
                            ss.append_log("%s %s화 압축 실패: %s" % (title_name, ep["no"], c_msg))
                else:
                    consecutive_fail += 1
                    ss.append_history({"type": "download_fail", "source": "auto",
                                        "title_id": title_id,
                                        "title": title_name, "episode_no": ep["no"], "error": err})
                    if consecutive_fail >= pipeline._MAX_CONSECUTIVE_FAILURES:
                        ss.append_log("titleId=%s 연속 %d회 실패 - 일시 차단 가능성으로 중단" %
                                       (title_id, consecutive_fail))
                        break

            if last_ok_no != t_info.get("last_downloaded_no"):
                ss.upsert_title({str(title_id): {"last_downloaded_no": last_ok_no}})
            ss.save_title_job_state({"running": False, "finished_at": time.time(),
                                      "message": "%s 다운로드 완료(%d화)" % (title_name, ok_count)})

        t = threading.Thread(target=_runner, name="webtoon_manager_dl_title", daemon=True)
        t.start()
        return True, "%s 다운로드 시작됨(백그라운드)" % title_name

    def _act_test_discord(self, db_type):
        cfg = self._get_cfg(db_type)
        ok, msg = discord_notify.notify(cfg, "🔔 웹툰 다운로더 플러그인 테스트",
                                         "이 메시지가 보이면 디스코드 알림 설정이 정상입니다.")
        return ok, msg


def action_slug(label):
    return "".join(c for c in label if c.isalnum()) or "job"


# ----------------------------------------------------------------------
# 모듈 임포트 시점 스케줄러 부트스트랩
# ----------------------------------------------------------------------
# scheduler.ensure_started()는 원래 get_dashboard_data() 안에서만 호출됐다.
# 그런데 get_dashboard_data()는 카테고리탭 화면(script.js의 폴링)이 실제로
# 열려 있을 때만 코어가 호출하는 경로다. 즉 컨테이너가 재시작된 뒤 아무도
# "웹툰 다운로더" 탭을 열지 않으면 스케줄러 스레드 자체가 영영 시작되지
# 않고, ENABLE_SCHEDULER=true로 설정해놔도 자동 다운로드가 조용히 멈춰
# 있는 문제가 있었다 - 이 플러그인의 존재 이유(무인 자동 다운로드)를 깨는
# 문제라 별도 트리거를 추가한다.
#
# scheduler.py 자신의 주석대로 "BookOasis 플러그인 모듈은 요청마다 새로
# 로드될 수 있다" - 즉 이 파일이 import되는 시점 자체가, 카테고리탭을
# 열었을 때보다 훨씬 자주(플러그인 목록 조회, 사이드바 렌더링, 권한 매트릭스
# 조회 등 이 모듈을 건드리는 모든 요청마다) 찾아온다. 그 매 시점마다
# ensure_started()를 "시도"해두면, 그중 Flask 요청 컨텍스트가 살아있는
# 시점(=대부분의 요청)에 한 번만 성공해도 스레드가 뜬다.
#
# 아주 방어적으로 감싼다: 여기서 무슨 예외가 나든(예: 아직 앱 컨텍스트가
# 없는 극초기 import 시점이라 get_plugin_config()가 실패하는 경우) 클래스
# 정의 자체(이미 위에서 끝남)에는 영향이 없어야 하고, 플러그인 로딩을
# 절대 막아선 안 된다. get_dashboard_data() 쪽의 기존 ensure_started() 호출도
# 그대로 남겨둬서 이중 안전망으로 유지한다(ensure_started 자체가 중복
# 호출에 안전하도록 이미 설계돼 있음 - PID 락 + 프로세스 전역 플래그).
try:
    _bootstrap_provider = WebtoonManagerMetadataProvider()
    scheduler.ensure_started(lambda: _bootstrap_provider._get_cfg("general"),
                              pipeline.run_full_cycle,
                              pipeline.run_finished_scan_job)
except Exception:  # noqa: BLE001
    pass
