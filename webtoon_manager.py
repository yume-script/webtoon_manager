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
import re
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
from . import compare

PLUGIN_ID = "webtoon_manager"

# 목록 데이터 캐시(작품 목록 파일/중복 확인 결과가 그대로면 재사용). 중복 확인은 compare.py
_TITLE_ITEMS_CACHE = {}


def _low_prio(fn):
    """백그라운드 작업 스레드를 낮은 CPU 우선순위로 실행(웹서버 응답 우선)."""
    def _wrapped(*a, **kw):
        try:
            downloader.lower_thread_priority(10)
        except Exception:  # noqa: BLE001
            pass
        return fn(*a, **kw)
    return _wrapped



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
    "AUTO_SUBSCRIBE_NEW_TITLES": False,
    "MAX_NEW_EPISODES_PER_TITLE": 10,
    "PARALLEL_TITLES": 2,
    "FAST_MODE": False,
    "INITIAL_EPISODES_LIMIT": 0,
    "MAX_CONCURRENT_DOWNLOADS": 5,
    "DELAY_SECONDS": 1.0,
    "REQUEST_TIMEOUT_SECONDS": 10,
    "COOKIE_KEEPALIVE_HOURS": 6,
    "FOLDER_ZERO_FILL": 4,
    "IMAGE_ZERO_FILL": 4,
    "GENERATE_COMICINFO_XML": True,
    "GENERATE_SERIES_JSON": True,
    "GENERATE_KAVITA_YAML": True,
    "KAVITA_YAML_EMBED_COVER": True,
    "KAKAO_ENABLE": False,
    "KAKAO_COOKIE": "",
    "KAKAO_DOWNLOAD_ROOT": "",
    "KAKAO_AUTO": True,
    "KAKAO_USE_WAITFREE": False,
    "NEW_EP_SCOPE": "today",
    "NAVER_TRY_OWNED_PAID": True,
    "KAKAO_USE_OWNED_TICKETS": False,
    "KAKAO_USE_PAID_TICKETS": False,
    "ALLOW_BL": False,
    "KAKAO_NOVEL_ENABLE": False,
    "KAKAO_SYNC_PURCHASED": True,
    "KAKAO_NOVEL_DOWNLOAD_ROOT": "",
    "ALLOW_GL": False,
    "COMPARE_FOLDER": "",
    "COMPARE_FOLDERS_NAVER": "",
    "COMPARE_FOLDERS_KAKAO": "",
    "COMPARE_FOLDERS_NOVEL": "",
    "ADD_COVER_AS_FIRST_PAGE": True,
    "LOW_PRIORITY_MODE": True,
    "DOWNLOAD_NICE_LEVEL": 10,
    "ZIP_STORED": True,
    "NAVER_DOWNLOAD_DAILY_PLUS": False,
    "KAKAO_DOWNLOAD_WAITFREE": False,
    "KAKAO_NOVEL_DOWNLOAD_WAITFREE": False,
    "NAVER_DOWNLOAD_OWNED": False,
    "KAKAO_DOWNLOAD_OWNED": False,
    "KAKAO_NOVEL_DOWNLOAD_OWNED": False,
}


class WebtoonManagerMetadataProvider(BaseMetadataProvider):
    id = "webtoon_manager"
    name = "웹툰 다운로더"
    is_searchable = False

    # 모든 설정은 카테고리탭 '설정' 탭에서 편집한다(get_settings/save_settings 액션).
    # 코어의 플러그인 설정 화면에는 아무것도 보이지 않도록 config_schema는 비워 둔다.
    config_schema = []

    settings_schema = [
        # ---------------- 공통 ----------------
        {"key": "ENABLE_SCHEDULER", "label": "자동 실행 사용", "type": "checkbox", "default": False,
         "hint": "켜면 아래 주기마다 요일별 스캔 + 새 회차 다운로드를 자동으로 합니다."},
        {"key": "INTERVAL_MINUTES", "label": "실행 주기(분)", "type": "number", "default": 240,
         "hint": "최소 10분."},
        {"key": "FINISHED_SCAN_HOUR", "label": "완결 목록 수집 시각(0~23시)", "type": "number", "default": 4,
         "hint": "하루 1번 완결 작품 전체 목록을 수집합니다."},
        {"key": "NEW_EP_SCOPE", "label": "새 회차 확인 범위", "type": "select", "default": "today",
         "options": [["today", "오늘 연재 요일 작품 + 매일+/기다무 + 아직 안 받은 작품"],
                     ["all", "구독작 전부(매 실행마다)"]]},
        {"key": "AUTO_SUBSCRIBE_NEW_TITLES", "label": "신간 자동 구독", "type": "checkbox", "default": False,
         "hint": "요일별 목록에 처음 나타나는 작품을 전부 구독합니다(관심 작가 작품은 이 설정과 상관없이 자동 구독)."},
        {"key": "INITIAL_EPISODES_LIMIT", "label": "새로 구독한 작품은 최신 N화만 받기", "type": "number", "default": 0,
         "hint": "0 = 전체 회차. 이전 회차는 카드의 '다운로드'/'다시 확인'으로 받을 수 있습니다."},
        {"key": "ALLOW_BL", "label": "BL 장르 받기", "type": "checkbox", "default": False,
         "hint": "끄면 BL 작품은 받지 않고 자동 구독도 하지 않습니다(전 플랫폼)."},
        {"key": "ALLOW_GL", "label": "GL 장르 받기", "type": "checkbox", "default": False,
         "hint": "끄면 GL 작품은 받지 않고 자동 구독도 하지 않습니다(전 플랫폼)."},

        {"key": "COMPARE_FOLDER", "label": "중복 확인 폴더(모든 플랫폼)", "type": "textarea", "default": "",
         "hint": "한 줄에 폴더 하나. 하위 폴더명/압축파일명을 시리즈명으로 보고 비교합니다. "
                 "플랫폼별 폴더는 각 탭에서 따로 지정합니다."},
        {"key": "COMPARE_LIBRARY_ID", "label": "중복 확인 라이브러리(모든 플랫폼)", "type": "library", "default": "",
         "hint": "고른 BookOasis 라이브러리에 같은 이름의 시리즈가 있으면 '보유중'으로 봅니다."},
        {"key": "COMPARE_LIBRARY_NAME", "label": "중복 확인 라이브러리 이름", "type": "hidden", "default": ""},

        {"key": "FAST_MODE", "label": "고속 모드(밀린 회차 몰아받기)", "type": "checkbox", "default": False,
         "hint": "동시 작품 6개↑·이미지 10개↑·회차 간 대기 0.2초↓·작품당 1회 50화까지 올립니다. "
                 "서버가 제한(429/503)하면 자동으로 쉬었다가 다시 빨라집니다."},
        {"key": "PARALLEL_TITLES", "label": "동시에 처리할 작품 수(1~10)", "type": "number", "default": 2,
         "hint": "네이버·카카오 각각 적용."},
        {"key": "MAX_CONCURRENT_DOWNLOADS", "label": "이미지 동시 다운로드 수", "type": "number", "default": 5},
        {"key": "DELAY_SECONDS", "label": "회차 간 대기(초)", "type": "number", "default": 1.0},
        {"key": "MAX_NEW_EPISODES_PER_TITLE", "label": "작품당 1회 실행에 받을 최대 회차 수", "type": "number",
         "default": 10, "hint": "0 = 무제한. 남은 회차는 다음 실행 때 이어서 받습니다."},
        {"key": "REQUEST_TIMEOUT_SECONDS", "label": "요청 타임아웃(초)", "type": "number", "default": 10},
        {"key": "LOW_PRIORITY_MODE", "label": "BookOasis에 CPU 양보", "type": "checkbox", "default": True,
         "hint": "다운로드/압축 작업의 CPU 우선순위를 낮춰 웹 화면이 느려지지 않게 합니다(리눅스)."},
        {"key": "DOWNLOAD_NICE_LEVEL", "label": "CPU 양보 정도(0~19)", "type": "number", "default": 10,
         "hint": "클수록 더 많이 양보. 위 옵션이 켜져 있을 때만 적용."},

        {"key": "TEMP_DOWNLOAD_ROOT", "label": "임시 작업 경로", "type": "text",
         "hint": "압축 전 낱장 이미지와 완성된 zip을 잠시 두는 곳. 비우면 플러그인 데이터 폴더. "
                 "저장 경로가 원격/rclone 마운트라면 반드시 로컬 디스크로 지정하세요."},
        {"key": "ADD_COVER_AS_FIRST_PAGE", "label": "표지를 회차 zip 첫 페이지로 넣기", "type": "checkbox",
         "default": True},
        {"key": "GENERATE_COMICINFO_XML", "label": "ComicInfo.xml 넣기(회차 zip 안)", "type": "checkbox",
         "default": True, "hint": "Komga/Kavita 등이 읽는 메타데이터."},
        {"key": "GENERATE_SERIES_JSON", "label": "series.json 만들기(시리즈 폴더)", "type": "checkbox",
         "default": True, "hint": "BookOasis 스캐너가 읽는 시리즈 메타데이터."},
        {"key": "GENERATE_KAVITA_YAML", "label": "kavita.yaml 만들기(시리즈 폴더)", "type": "checkbox",
         "default": True, "hint": "새 회차를 받거나 작품 정보가 바뀌었을 때만 갱신."},
        {"key": "KAVITA_YAML_EMBED_COVER", "label": "kavita.yaml에 표지(base64) 넣기", "type": "checkbox",
         "default": True, "hint": "끄면 모든 회차 cover가 FIRST."},
        {"key": "ZIP_STORED", "label": "zip 무압축 저장(권장)", "type": "checkbox", "default": True,
         "hint": "이미지는 이미 압축돼 있어 재압축해도 용량은 거의 그대로고 CPU만 씁니다."},
        {"key": "FOLDER_ZERO_FILL", "label": "회차 번호 자릿수", "type": "number", "default": 4,
         "hint": "예: 4 → '제목 0012화'."},
        {"key": "IMAGE_ZERO_FILL", "label": "이미지 파일 번호 자릿수", "type": "number", "default": 4},

        {"key": "COOKIE_KEEPALIVE_HOURS", "label": "쿠키 자동 갱신 간격(시간)", "type": "number", "default": 6,
         "hint": "0 = 끔. 자동 실행을 꺼 둬도 동작하고, 만료되면 디스코드로 알립니다."},
        {"key": "DISCORD_WEBHOOK_URL", "label": "디스코드 웹훅 URL", "type": "text"},
        {"key": "DISCORD_BOT_TOKEN", "label": "디스코드 봇 토큰", "type": "password", "hint": "선택. 완결 확인 알림용."},
        {"key": "DISCORD_CHANNEL_ID", "label": "디스코드 채널 ID", "type": "text"},

        # ---------------- 네이버웹툰 ----------------
        {"key": "NAVER_COOKIE_JSON", "label": "네이버 로그인 쿠키", "type": "password", "required": False},
        {"key": "DOWNLOAD_ROOT", "label": "저장 경로", "type": "text",
         "hint": "비우면 플러그인 데이터 폴더/downloads."},
        {"key": "NAVER_DOWNLOAD_DAILY_PLUS", "label": "매일+ 작품 받기", "type": "checkbox", "default": False,
         "hint": "켜면 매일+ 작품을 자동 구독해 받습니다. 끄면 받지 않고, 예전에 자동 구독된 매일+ 작품도 "
                 "구독 전으로 되돌립니다(직접 구독한 작품은 그대로 받음)."},
        {"key": "NAVER_TRY_OWNED_PAID", "label": "대여/소장한 유료 회차도 받기", "type": "checkbox", "default": True,
         "hint": "로그인 쿠키 필요. 결제는 하지 않습니다."},
        {"key": "COMPARE_FOLDERS_NAVER", "label": "중복 확인 폴더", "type": "textarea", "default": "",
         "hint": "한 줄에 폴더 하나. 네이버 작품하고만 비교합니다."},
        {"key": "NAVER_DOWNLOAD_OWNED", "label": "이미 갖고 있는 작품도 받기", "type": "checkbox", "default": False,
         "hint": "끄면 중복 확인 폴더/라이브러리에 있는 작품은 자동 다운로드하지 않습니다"
                 "(이 플러그인이 이미 받기 시작한 작품, 카드의 '다운로드' 버튼은 예외)."},

        # ---------------- 카카오웹툰 ----------------
        {"key": "KAKAO_ENABLE", "label": "카카오페이지 사용", "type": "checkbox", "default": False,
         "hint": "카카오 웹툰 목록 수집/다운로드. 카카오웹소설을 쓰려면 이것도 켜야 합니다."},
        {"key": "KAKAO_COOKIE", "label": "카카오페이지 로그인 쿠키", "type": "password", "required": False,
         "hint": "Cookie-Editor JSON 또는 'a=b; c=d'. 비우면 무료 회차만. 웹소설도 이 쿠키를 씁니다."},
        {"key": "KAKAO_AUTO", "label": "자동 실행에 포함", "type": "checkbox", "default": True,
         "hint": "자동 실행 때 네이버와 동시에 카카오 웹툰·웹소설 새 회차를 확인합니다."},
        {"key": "KAKAO_DOWNLOAD_ROOT", "label": "저장 경로", "type": "text",
         "hint": "비우면 플러그인 데이터 폴더/kakao_downloads."},
        {"key": "KAKAO_DOWNLOAD_WAITFREE", "label": "기다무 작품 받기", "type": "checkbox", "default": False,
         "hint": "켜면 연재 중인 기다무 웹툰을 자동 구독해 받습니다. 끄면 받지 않고, 예전에 자동 구독된 기다무 웹툰도 "
                 "구독 전으로 되돌립니다(직접 구독한 작품은 그대로 받음)."},
        {"key": "KAKAO_SYNC_PURCHASED", "label": "구매 작품 자동 동기화", "type": "checkbox", "default": True,
         "hint": "하루 1번 보관함 > 구매 목록의 작품을 등록하고 구매·대여 회차를 받습니다(로그인 쿠키 필요)."},
        {"key": "KAKAO_USE_WAITFREE", "label": "기다무 대여권 자동 사용", "type": "checkbox", "default": False,
         "hint": "작품당 실행 1회에 1장. 계정의 대여권이 실제로 소모됩니다(웹툰·웹소설 공통)."},
        {"key": "KAKAO_USE_OWNED_TICKETS", "label": "보유 대여권 자동 사용", "type": "checkbox", "default": False,
         "hint": "이벤트/선물/쿠폰으로 받은 대여권. 실제로 소모됩니다(웹툰·웹소설 공통)."},
        {"key": "KAKAO_USE_PAID_TICKETS", "label": "구매한 대여권도 사용", "type": "checkbox", "default": False,
         "hint": "돈으로 산 대여권. '보유 대여권 자동 사용'이 켜져 있을 때만 적용."},
        {"key": "COMPARE_FOLDERS_KAKAO", "label": "중복 확인 폴더", "type": "textarea", "default": "",
         "hint": "한 줄에 폴더 하나. 카카오 웹툰하고만 비교합니다."},
        {"key": "KAKAO_DOWNLOAD_OWNED", "label": "이미 갖고 있는 작품도 받기", "type": "checkbox", "default": False,
         "hint": "끄면 중복 확인 폴더/라이브러리에 있는 웹툰은 자동 다운로드하지 않습니다"
                 "(이 플러그인이 이미 받기 시작한 작품, 카드의 '다운로드' 버튼은 예외)."},

        # ---------------- 카카오웹소설 ----------------
        {"key": "KAKAO_NOVEL_ENABLE", "label": "카카오 웹소설 사용", "type": "checkbox", "default": False,
         "hint": "회차별 EPUB(삽화 포함)으로 저장합니다. [카카오웹툰] 탭의 '카카오페이지 사용'도 켜져 있어야 하고, "
                 "로그인 쿠키·대여권 설정은 그 탭 것을 같이 씁니다."},
        {"key": "KAKAO_NOVEL_DOWNLOAD_ROOT", "label": "저장 경로", "type": "text",
         "hint": "비우면 플러그인 데이터 폴더/kakao_novels."},
        {"key": "KAKAO_NOVEL_DOWNLOAD_WAITFREE", "label": "기다무 작품 받기", "type": "checkbox", "default": False,
         "hint": "켜면 연재 중인 기다무 웹소설을 자동 구독해 받습니다(작품 수가 많음). 끄면 받지 않고, 예전에 자동 구독된 "
                 "기다무 웹소설도 구독 전으로 되돌립니다(직접 구독한 작품은 그대로 받음)."},
        {"key": "COMPARE_FOLDERS_NOVEL", "label": "중복 확인 폴더", "type": "textarea", "default": "",
         "hint": "한 줄에 폴더 하나. 카카오 웹소설하고만 비교합니다."},
        {"key": "KAKAO_NOVEL_DOWNLOAD_OWNED", "label": "이미 갖고 있는 작품도 받기", "type": "checkbox",
         "default": False,
         "hint": "끄면 중복 확인 폴더/라이브러리에 있는 웹소설은 자동 다운로드하지 않습니다"
                 "(이 플러그인이 이미 받기 시작한 작품, 카드의 '다운로드' 버튼은 예외)."},
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
                  "pipeline.py", "kavita_yaml.py", "kakao_api.py", "kakao_pipeline.py", "novel_epub.py"],
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
    def _get_cfg(self, db_type, for_settings=False):
        cfg = self.get_plugin_config(db_type, default={}) or {}
        merged = dict(DEFAULTS)
        merged.update({k: v for k, v in cfg.items() if v not in (None, "")})
        if not merged.get("DOWNLOAD_ROOT"):
            merged["DOWNLOAD_ROOT"] = ss.DOWNLOAD_DEFAULT_DIR
        if not for_settings:
            merged = pipeline.apply_fast_mode(merged)   # 설정 화면에는 사용자가 넣은 값 그대로
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
        cached = dict(ss.load_update_check())
        checked_at = cached.get("checked_at")
        local_version = self._read_version()
        # 캐시는 "확인 당시의 로컬 버전" 기준으로 계산돼 있다. 그 사이 플러그인을
        # 업데이트하면(예: 1.18.5 -> 1.19.0) 최대 1시간 동안 예전 판정(원격 1.18.6이
        # 더 높음)이 그대로 보여 "현재 v1.19.0인데 업데이트 가능(v1.18.6)"이 떴다.
        # 그래서 반환할 때마다 현재 로컬 버전으로 다시 비교하고, 로컬 버전이
        # 바뀌었으면 원격도 다시 확인한다.
        is_stale = (not checked_at or
                    (time.time() - checked_at) >= UPDATE_CHECK_INTERVAL_SECONDS or
                    cached.get("local_version") != local_version)
        if is_stale:
            self._maybe_start_background_update_check()
        latest = cached.get("latest_version")
        cached["local_version"] = local_version
        cached["update_available"] = bool(
            latest and self._version_tuple(latest) > self._version_tuple(local_version))
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
        try:
            self._maybe_drain_kakao_queue(cfg)
        except Exception:  # noqa: BLE001
            pass

        compare_set, compare_status = self._build_compare_set(db_type, cfg)
        items_list = self._build_title_items(cfg, compare_set)

        bundle = {
            "titles": items_list,
            "authors_tags": ss.load_authors_tags(),
            "history": ss.load_history(limit=200),
            "job": ss.load_job_state(),
            "title_job": ss.load_title_job_state(),
            "kakao_queue": ss.kakao_queue_list(),
            "titles_rev": ss.titles_rev(),
            "log_tail": ss.tail_log(60),
            "plugin_version": self._read_version(),
            "update_status": update_status,
            "compare_status": compare_status,
            "repo_url": REPO_URL,
            "config_public": {
                "DOWNLOAD_ROOT": cfg.get("DOWNLOAD_ROOT", ""),
                "TEMP_DOWNLOAD_ROOT": cfg.get("TEMP_DOWNLOAD_ROOT") or ss.TMP_DOWNLOAD_DEFAULT_DIR,
                "ENABLE_SCHEDULER": bool(cfg.get("ENABLE_SCHEDULER")),
                "INTERVAL_MINUTES": cfg.get("INTERVAL_MINUTES"),
                "FINISHED_SCAN_HOUR": cfg.get("FINISHED_SCAN_HOUR"),
                "AUTO_SUBSCRIBE_NEW_TITLES": bool(cfg.get("AUTO_SUBSCRIBE_NEW_TITLES")),
                "NAVER_DOWNLOAD_DAILY_PLUS": bool(pipeline._truthy(cfg.get("NAVER_DOWNLOAD_DAILY_PLUS"))),
                "NAVER_DOWNLOAD_OWNED": bool(pipeline._truthy(cfg.get("NAVER_DOWNLOAD_OWNED"))),
                "KAKAO_DOWNLOAD_OWNED": bool(pipeline._truthy(cfg.get("KAKAO_DOWNLOAD_OWNED"))),
                "KAKAO_NOVEL_DOWNLOAD_OWNED": bool(pipeline._truthy(cfg.get("KAKAO_NOVEL_DOWNLOAD_OWNED"))),
                "COMPARE_LIBRARY_ID": cfg.get("COMPARE_LIBRARY_ID", ""),
                "COMPARE_LIBRARY_NAME": cfg.get("COMPARE_LIBRARY_NAME", ""),
                "COMPARE_FOLDER": cfg.get("COMPARE_FOLDER", ""),
                "COMPARE_FOLDERS_NAVER": cfg.get("COMPARE_FOLDERS_NAVER", ""),
                "COMPARE_FOLDERS_KAKAO": cfg.get("COMPARE_FOLDERS_KAKAO", ""),
                "COMPARE_FOLDERS_NOVEL": cfg.get("COMPARE_FOLDERS_NOVEL", ""),
                "ADD_COVER_AS_FIRST_PAGE": bool(cfg.get("ADD_COVER_AS_FIRST_PAGE", True)),
                "GENERATE_COMICINFO_XML": bool(cfg.get("GENERATE_COMICINFO_XML", True)),
                "GENERATE_SERIES_JSON": bool(cfg.get("GENERATE_SERIES_JSON", True)),
                "GENERATE_KAVITA_YAML": bool(cfg.get("GENERATE_KAVITA_YAML", True)),
                "KAVITA_YAML_EMBED_COVER": bool(cfg.get("KAVITA_YAML_EMBED_COVER", True)),
                "LOW_PRIORITY_MODE": bool(cfg.get("LOW_PRIORITY_MODE", True)),
                "DOWNLOAD_NICE_LEVEL": cfg.get("DOWNLOAD_NICE_LEVEL", 10),
                "ZIP_STORED": bool(cfg.get("ZIP_STORED", True)),
                "MAX_NEW_EPISODES_PER_TITLE": cfg.get("MAX_NEW_EPISODES_PER_TITLE"),
                "PARALLEL_TITLES": cfg.get("PARALLEL_TITLES", 2),
                "INITIAL_EPISODES_LIMIT": cfg.get("INITIAL_EPISODES_LIMIT", 0),
                "MAX_CONCURRENT_DOWNLOADS": cfg.get("MAX_CONCURRENT_DOWNLOADS"),
                "DELAY_SECONDS": cfg.get("DELAY_SECONDS"),
                "has_cookie": bool(cfg.get("NAVER_COOKIE_JSON")),
                "KAKAO_ENABLE": bool(cfg.get("KAKAO_ENABLE")),
                "KAKAO_AUTO": bool(cfg.get("KAKAO_AUTO", True)),
                "KAKAO_USE_WAITFREE": bool(cfg.get("KAKAO_USE_WAITFREE")),
                "ALLOW_BL": bool(cfg.get("ALLOW_BL")),
                "ALLOW_GL": bool(cfg.get("ALLOW_GL")),
                "KAKAO_NOVEL_ENABLE": bool(cfg.get("KAKAO_NOVEL_ENABLE")),
                "KAKAO_DOWNLOAD_WAITFREE": bool(pipeline._truthy(cfg.get("KAKAO_DOWNLOAD_WAITFREE"))),
                "KAKAO_NOVEL_DOWNLOAD_WAITFREE": bool(pipeline._truthy(cfg.get("KAKAO_NOVEL_DOWNLOAD_WAITFREE"))),
                "KAKAO_DOWNLOAD_ROOT": cfg.get("KAKAO_DOWNLOAD_ROOT") or ss.KAKAO_DOWNLOAD_DEFAULT_DIR,
                "has_kakao_cookie": bool((cfg.get("KAKAO_COOKIE") or "").strip()),
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
        if action_id not in ("poll_status", "get_settings"):
            # 버튼으로 바꾼 작품 정보(구독/제외 등)는 묶음 저장을 기다리지 않고 바로 반영
            try:
                ss.flush_titles()
            except Exception:  # noqa: BLE001
                pass
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
            if action == "kavita_yaml_all":
                return self._act_run_bg(db_type, pipeline.run_kavita_yaml_all, "kavita.yaml 일괄 생성")
            if action == "poll_status":
                return self._poll_status(db_type)
            if action == "get_settings":
                return self._act_get_settings(db_type)
            if action == "save_settings":
                return self._act_save_settings(db_type, payload)
            if action == "naver_verify_cookie":
                return self._act_naver_verify_cookie(self._get_cfg(db_type), payload)
            if action == "kakao_verify_cookie":
                return self._act_kakao_verify_cookie(self._get_cfg(db_type), payload)
            if action.startswith("kakao_"):
                return self._dispatch_kakao(db_type, action, payload)
            if payload.get("platform") == "kakao" and action in (
                    "subscribe", "unsubscribe", "exclude", "restore", "resync_title", "download_title"):
                return self._kakao_card_action(db_type, action, payload)
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
                                            excluded=False, unsubscribed=False,
                                            manual_subscribed=True, auto_subscribed=None)
            if action == "unsubscribe":
                return self._act_set_flags(payload.get("titleId"), subscribed=False,
                                            unsubscribed=True)
            if action == "exclude":
                return self._act_set_flags(payload.get("titleId"), subscribed=False,
                                            excluded=True)
            if action == "restore":
                return self._act_set_flags(payload.get("titleId"), subscribed=True,
                                            excluded=False, unsubscribed=False,
                                            manual_subscribed=True, auto_subscribed=None)
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
                if payload.get("platform") == "kakao":
                    return self._act_kakao_manual_lookup(db_type, payload.get("titleId"))
                return self._act_manual_lookup(db_type, payload.get("titleId"))
            if action == "manual_download":
                if payload.get("platform") == "kakao":
                    return self._act_kakao_manual_download(db_type, payload)
                return self._act_manual_download(db_type, payload)
            if action == "download_title":
                return self._act_download_title(db_type, payload.get("titleId"))
            if action == "test_discord":
                return self._act_test_discord(db_type)
            if action == "list_libraries":
                return self._act_list_libraries(db_type)
            if action == "set_compare_library":
                return self._act_set_compare_library(db_type, payload)
            return False, "알 수 없는 action: %s" % action
        except Exception as e:  # noqa: BLE001
            return False, "오류: %s" % e

    # ------------------------------------------------------------------
    # 카카오페이지
    # ------------------------------------------------------------------
    def _kakao_items(self, cfg):
        if cfg.get("KAKAO_ENABLE"):
            try:
                from . import kakao_pipeline
                kakao_pipeline.repair_old_records_async(cfg)
            except Exception:  # noqa: BLE001
                pass
        out = []
        for sid, t in ss.load_kakao_titles().items():
            item = dict(t)
            item["seriesId"] = sid
            out.append(item)
        out.sort(key=lambda x: x.get("added_at") or 0, reverse=True)
        return out

    # 화면에 필요한 필드만 보낸다. 카카오 작품이 수천 개라 줄거리/표지 원본 URL/
    # 내부 상태값까지 다 보내면 폴링 한 번에 수 MB가 오가며 서버·브라우저 CPU를 잡아먹었다.
    _UI_FIELDS = ("title", "author", "thumbnail", "weekdays", "status", "subscribed",
                  "unsubscribed", "excluded", "new", "rest", "up_flag", "rating", "waitfree",
                  "category", "last_result", "adult", "is_adult", "last_downloaded_no",
                  "episode_count", "last_seen_at", "purchased")

    def _build_title_items(self, cfg, compare_set):
        """목록 데이터. titles.json/kakao_titles.json이 바뀌지 않았으면 이전 결과를
        그대로 재사용한다(파일 수정 시각 + 비교 폴더 결과로 캐시 키)."""
        key = (ss.titles_rev(), bool(cfg.get("KAKAO_ENABLE")),
               (compare_set or {}).get("_sig") if compare_set is not None else None)
        cached = _TITLE_ITEMS_CACHE.get("v")
        if cached and cached[0] == key:
            return cached[1]

        def _slim(t, tid, platform):
            item = {k: t[k] for k in self._UI_FIELDS if k in t}
            item["titleId"] = tid
            item["platform"] = platform
            if pipeline.is_bl(t):
                item["bl"] = True
            if pipeline.is_gl(t):
                item["gl"] = True
            if platform == "kakao" and "소설" in str(t.get("category") or ""):
                item["novel"] = True
            if compare_set is None:
                item["in_library"] = None
            else:
                hits = compare.hits(compare_set, t.get("title", ""), compare.scope_of(platform, t))
                item["in_library"] = bool(hits)
                if hits:
                    item["in_library_src"] = hits[:5]
            return item

        items_list = [_slim(t, tid, "naver") for tid, t in ss.load_titles().items()]
        if cfg.get("KAKAO_ENABLE"):
            try:
                from . import kakao_pipeline
                kakao_pipeline.repair_old_records_async(cfg)
            except Exception:  # noqa: BLE001
                pass
            items_list.extend(_slim(t, sid, "kakao") for sid, t in ss.load_kakao_titles().items())
        items_list.sort(key=lambda x: x.get("last_seen_at", 0) or 0, reverse=True)
        _TITLE_ITEMS_CACHE["v"] = (key, items_list)
        return items_list

    def _poll_status(self, db_type):
        """화면의 주기적 폴링용 가벼운 상태(작업 진행/로그/이력/목록 변경 표식).
        작품 목록 자체는 titles_rev가 바뀌었을 때만 화면이 따로 다시 받는다."""
        cfg = self._get_cfg(db_type)
        try:
            scheduler.ensure_started(lambda: self._get_cfg(db_type),
                                      pipeline.run_full_cycle,
                                      pipeline.run_finished_scan_job)
        except Exception:  # noqa: BLE001
            pass
        try:
            self._maybe_drain_kakao_queue(cfg)
        except Exception:  # noqa: BLE001
            pass
        return True, json.dumps({
            "job": ss.load_job_state(),
            "title_job": ss.load_title_job_state(),
            "kakao_queue": ss.kakao_queue_list(),
            "titles_rev": ss.titles_rev(),
            "log_tail": ss.tail_log(60),
            "history": ss.load_history(limit=200),
            "speed": self._speed_info(cfg),
        }, ensure_ascii=False)

    def _speed_info(self, cfg):
        from . import ratelimit
        info = ss.download_rate(3600)
        info["fast"] = bool(pipeline._truthy(cfg.get("FAST_MODE")))
        info["parallel"] = cfg.get("PARALLEL_TITLES")
        try:
            info["throttle"] = ratelimit.status()
        except Exception:  # noqa: BLE001
            info["throttle"] = {}
        return info

    def _act_kakao_manual_lookup(self, db_type, title_id):
        from . import kakao_api, kakao_pipeline
        sid = kakao_api.parse_series_id(title_id)
        if not sid:
            return False, "카카오페이지 작품 번호 또는 URL(page.kakao.com/content/숫자)을 입력하세요"
        try:
            return True, json.dumps(kakao_pipeline.lookup_episodes(self._get_cfg(db_type), sid),
                                    ensure_ascii=False)
        except Exception as e:  # noqa: BLE001
            return False, str(e)

    def _act_kakao_manual_download(self, db_type, payload):
        from . import kakao_api, kakao_pipeline
        cfg = self._get_cfg(db_type)
        sid = kakao_api.parse_series_id(payload.get("titleId"))
        nos = [int(n) for n in (payload.get("episodeNos") or [])]
        force = bool(payload.get("force", True))
        if not sid or not nos:
            return False, "작품 번호/회차 선택 필요"
        t = ss.get_kakao_title(sid)
        if not t:
            # 목록에 없는 작품이면 먼저 등록(구독은 하지 않음)
            ok, msg, _ = kakao_pipeline.add_series(cfg, sid, log=ss.append_log)
            if not ok:
                return False, msg
            ss.upsert_kakao_title({sid: {"subscribed": False}})
            t = ss.get_kakao_title(sid) or {}
        if pipeline.bl_blocked(cfg, t):
            return False, pipeline.genre_block_msg(cfg, t)
        acquired = ss.try_acquire_title_job({
            "title_id": sid, "title": "[카카오] %s" % t.get("title", sid),
            "message": "카카오 선택 회차 다운로드 시작", "started_at": time.time(), "finished_at": None,
            "cancel_requested": False, "last_error": None, "progress": 0, "total": len(nos)})
        if not acquired:
            tj = ss.load_title_job_state()
            return False, "이미 다른 작품을 다운로드 중입니다(%s). 끝난 뒤 다시 시도해주세요." % (tj.get("title") or "")

        def _runner():
            if cfg.get("LOW_PRIORITY_MODE", True):
                downloader.lower_thread_priority(int(cfg.get("DOWNLOAD_NICE_LEVEL", 10)))
            try:
                kakao_pipeline.run_kakao_series_job(cfg, sid, log=ss.append_log, only_nos=nos, force=force)
            except Exception as e:  # noqa: BLE001
                ss.append_log("카카오 선택 회차 다운로드 실패: %s" % e)
                ss.save_title_job_state({"running": False, "finished_at": time.time(),
                                          "last_error": str(e), "message": "실패: %s" % e})

        threading.Thread(target=_low_prio(_runner), name="webtoon_manager_kakao_manual", daemon=True).start()
        return True, "카카오 %s: 선택한 %d개 회차 다운로드 시작(백그라운드)" % (t.get("title", sid), len(nos))

    def _start_kakao_worker(self, cfg, sid):
        """title_job 락을 잡을 수 있으면 sid부터 받기 시작하고, 끝나면 대기열을
        이어서 비운다. 락을 못 잡으면 False."""
        from . import kakao_pipeline

        def _acquire(cur):
            nt = ss.get_kakao_title(cur) or {}
            return ss.try_acquire_title_job({
                "title_id": cur, "title": "[카카오] %s" % nt.get("title", cur),
                "message": "%s %s 회차 확인 중" % (kakao_pipeline.platform_label(nt, with_kind=True), nt.get("title", cur)),
                "started_at": time.time(), "finished_at": None, "cancel_requested": False,
                "last_error": None, "progress": 0, "total": 0})

        if not _acquire(sid):
            return False

        def _runner():
            if cfg.get("LOW_PRIORITY_MODE", True):
                downloader.lower_thread_priority(int(cfg.get("DOWNLOAD_NICE_LEVEL", 10)))
            cur = sid
            while cur:
                try:
                    kakao_pipeline.run_kakao_series_job(cfg, cur, log=ss.append_log)
                except Exception as e:  # noqa: BLE001
                    ss.append_log("카카오 다운로드 실패(%s): %s" % (cur, e))
                    ss.upsert_kakao_title({cur: {"last_result": "다운로드 실패: %s" % e,
                                                 "last_result_at": time.time()}})
                    ss.save_title_job_state({"running": False, "finished_at": time.time(),
                                              "last_error": str(e), "message": "실패: %s" % e})
                if ss.load_title_job_state().get("cancel_requested"):
                    break
                cur = ss.kakao_queue_pop()
                if cur and not _acquire(cur):
                    ss.kakao_queue_push(cur)   # 다른 작업이 잡았으면 되돌려 두고 종료
                    break

        threading.Thread(target=_low_prio(_runner), name="webtoon_manager_kakao_dl", daemon=True).start()
        return True

    def _maybe_drain_kakao_queue(self, cfg):
        """대기열에 남은 작품이 있는데 아무것도 안 받고 있으면(재시작으로 스레드가
        죽은 경우 등) 대시보드 폴링 때 다시 시작한다."""
        q = ss.kakao_queue_list()
        if not q or not cfg.get("KAKAO_ENABLE"):
            return
        tj = ss.load_title_job_state()
        last = tj.get("updated_at") or tj.get("started_at") or 0
        if tj.get("running") and time.time() - float(last) < ss.TITLE_JOB_STALE_SECONDS:
            return   # 지금 받는 중 - 끝나면 그 스레드가 이어서 비운다
        sid = ss.kakao_queue_pop()
        if sid and not self._start_kakao_worker(cfg, sid):
            ss.kakao_queue_push(sid)

    def _act_naver_verify_cookie(self, cfg, payload):
        raw = (payload.get("cookie") or "").strip() or (cfg.get("NAVER_COOKIE_JSON") or "").strip()
        if not raw:
            return False, "네이버 쿠키가 비어 있습니다. Cookie-Editor로 comic.naver.com 쿠키를 JSON으로 내보내 붙여넣으세요."
        session = naver_api.build_session(raw, timeout=int(cfg.get("REQUEST_TIMEOUT_SECONDS", 10) or 10))
        if not list(session.cookies):
            return False, "쿠키 형식을 읽지 못했습니다(Cookie-Editor JSON 또는 'a=b; c=d' 형식)"
        adult_ids = [tid for tid, t in ss.load_titles().items() if t.get("is_adult")][:3]
        res = naver_api.verify_cookie(session, adult_ids)
        lines = ["로그인 쿠키(NID_AUT, NID_SES): %s" % ("✅ 있음" if res["login_cookies"] else
                                                       "❌ 없음 - comic.naver.com에 로그인한 상태에서 다시 내보내세요")]
        if res["login_cookies"]:
            live = naver_api.check_login(session)
            lines.append("로그인 상태: %s" % {True: "✅ 로그인 유지 중", False: "❌ 로그인 풀림(쿠키 만료) - 새로 내보내세요",
                                            None: "확인 못 함(네트워크)"}[live])
            if live and raw == (cfg.get("NAVER_COOKIE_JSON") or "").strip():
                naver_api.save_session_cookies(session)
        lines.append(self._keepalive_line(cfg, "naver"))
        if res["adult"] is True:
            lines.append("성인 인증: ✅ 됨 - 성인 작품 다운로드 가능")
            blocked = {tid: {"adult_block_hash": None} for tid, t in ss.load_titles().items()
                       if t.get("adult_block_hash")}
            if blocked:
                ss.upsert_title(blocked)
                lines.append("성인 인증 실패로 건너뛰던 작품 %d개를 다시 시도하도록 풀었습니다" % len(blocked))
        elif res["adult"] is False:
            lines.append("성인 인증: ❌ 안 됨 - 로그인 쿠키가 만료됐거나 이 네이버 계정의 성인 인증(1년마다 갱신)이 필요합니다")
        else:
            lines.append("성인 인증: 확인 못 함" + (" - 목록에 성인 작품이 없어 시험할 작품이 없음(지금 스캔 후 다시 시도)"
                                                    if not adult_ids else " (%s)" % res["detail"][:150]))
        return bool(res["login_cookies"]), " / ".join(lines)

    def _act_kakao_verify_cookie(self, cfg, payload):
        """[설정] 탭 '카카오 쿠키 검증' - 입력칸에 새로 붙여넣은 값이 있으면 그걸,
        없으면 저장된 쿠키를 검증한다."""
        from . import kakao_api
        raw = (payload.get("cookie") or "").strip() or (cfg.get("KAKAO_COOKIE") or "").strip()
        if not raw:
            return False, "카카오 쿠키가 비어 있습니다. Cookie-Editor로 page.kakao.com 쿠키를 JSON으로 내보내 붙여넣으세요."
        if not kakao_api._parse_cookie_input(raw):
            return False, "쿠키 형식을 읽지 못했습니다(Cookie-Editor JSON 또는 'a=b; c=d' 형식)"
        missing = kakao_api.missing_required_cookies(raw)
        session = kakao_api.build_session(raw, timeout=int(cfg.get("REQUEST_TIMEOUT_SECONDS", 15) or 15))
        adult_ids = [sid for sid, t in ss.load_kakao_titles().items() if t.get("adult")][:2]
        res = kakao_api.verify_cookie(session, adult_series_ids=adult_ids)
        lines = []
        lines.append("로그인: %s" % ("✅ 됨" + (" (%s)" % res["nickname"] if res["nickname"] else "")
                                    if res["logged_in"] else "❌ 안 됨 - 쿠키 만료 또는 로그인 쿠키 아님"))
        if res["adult"] is True:
            lines.append("성인 인증: ✅ 됨 - 성인 작품 다운로드 가능")
            # 예전 쿠키로 막혔던 성인 작품들을 다시 시도하도록 표시 해제
            blocked = {sid: {"adult_block_hash": None} for sid, t in ss.load_kakao_titles().items()
                       if t.get("adult_block_hash")}
            if blocked:
                ss.upsert_kakao_title(blocked)
                lines.append("성인 인증 실패로 건너뛰던 작품 %d개를 다시 시도하도록 풀었습니다" % len(blocked))
        elif res["adult"] is False:
            lines.append("성인 인증: ❌ 안 됨 - 이 카카오 계정이 성인 인증되지 않았거나 로그인 쿠키가 아님"
                         "(카카오페이지에서 성인 인증 후 쿠키를 다시 내보내세요)")
        else:
            lines.append("성인 인증: 확인 못 함")
        if missing:
            lines.append("⚠️ 필수 쿠키 누락: %s - page.kakao.com에 로그인한 상태에서 전체 쿠키를 다시 내보내세요"
                         % ", ".join(missing))
        if res.get("detail") and not res["logged_in"]:
            lines.append("상세: %s" % res["detail"])
        lines.append(self._keepalive_line(cfg, "kakao"))
        return bool(res["logged_in"]), " / ".join(lines)

    def _keepalive_line(self, cfg, platform):
        from . import cookie_keeper
        try:
            h = float(cfg.get("COOKIE_KEEPALIVE_HOURS", 6) or 0)
        except (TypeError, ValueError):
            h = 6
        if h <= 0:
            return "쿠키 자동 갱신: 꺼짐([공통] 설정)"
        st = cookie_keeper.load_state().get(platform) or {}
        if not st.get("last_at"):
            return "쿠키 자동 갱신: %g시간마다(아직 실행 전)" % h
        res = {True: "✅ 로그인 유지", False: "❌ 만료 감지", None: "확인 못 함"}.get(st.get("ok"), "확인 못 함")
        return "쿠키 자동 갱신: %g시간마다 / 마지막 %s %s" % (
            h, time.strftime("%m-%d %H:%M", time.localtime(st["last_at"])), res)

    def _kakao_card_action(self, db_type, action, payload):
        """통합 목록 카드에서 카카오 작품에 대해 누른 버튼(네이버와 같은 액션 이름)."""
        sid = str(payload.get("titleId") or "").strip()
        if not ss.has_kakao_title(sid):
            return False, "목록에 없는 카카오 작품입니다"
        flags = {
            "subscribe": {"subscribed": True, "excluded": False, "unsubscribed": False,
                          "manual_subscribed": True, "auto_subscribed": None},
            "restore": {"subscribed": True, "excluded": False, "unsubscribed": False,
                        "manual_subscribed": True, "auto_subscribed": None},
            "unsubscribe": {"subscribed": False, "unsubscribed": True},
            "exclude": {"subscribed": False, "excluded": True},
        }
        if action in flags:
            ss.upsert_kakao_title({sid: flags[action]})
            return True, "적용됨"
        if action == "resync_title":
            ss.upsert_kakao_title({sid: {"checked_no": 0, "last_downloaded_no": None, "min_order": None}})
            return True, "다음 다운로드부터 전체 회차를 다시 확인합니다(이미 있는 파일은 스킵됨)"
        return self._dispatch_kakao(db_type, "kakao_download", {"seriesId": sid})

    def _dispatch_kakao(self, db_type, action, payload):
        from . import kakao_pipeline
        cfg = self._get_cfg(db_type)
        if not cfg.get("KAKAO_ENABLE"):
            return False, "[설정] 탭 > 카카오페이지에서 '카카오페이지 웹툰 사용'을 먼저 켜고 저장해주세요."
        sid = str(payload.get("seriesId") or "").strip()

        if action == "kakao_verify_cookie":
            return self._act_kakao_verify_cookie(cfg, payload)
        if action == "kakao_add":
            ok, msg, _sid = kakao_pipeline.add_series(cfg, payload.get("value"), log=ss.append_log)
            return ok, msg
        if action in ("kakao_subscribe", "kakao_unsubscribe"):
            if not ss.has_kakao_title(sid):
                return False, "등록되지 않은 작품입니다"
            ss.upsert_kakao_title({sid: {"subscribed": action == "kakao_subscribe",
                                         "unsubscribed": action != "kakao_subscribe",
                                         "excluded": False}})
            return True, "변경됨"
        if action == "kakao_remove":
            removed = ss.remove_kakao_title(sid)
            return (True, "목록에서 삭제됨(받은 파일은 그대로 둠)") if removed else (False, "없는 작품")
        if action == "kakao_scan":
            def _scan(c, log):
                kakao_pipeline.run_kakao_scan_weekday(
                    c, log=log, should_cancel=lambda: ss.load_job_state().get("cancel_requested"))
                ss.save_job_state({"running": False, "stage": "done", "finished_at": time.time(),
                                    "message": "카카오페이지 요일별 목록 수집 완료"})
            return self._act_run_bg(db_type, _scan, "카카오페이지 목록 수집")
        if action == "kakao_sync_purchased":
            def _sync(c, log):
                return kakao_pipeline.sync_purchased(c, log=log, manage_job=True)
            return self._act_run_bg(db_type, _sync, "카카오 구매 작품 동기화")
        if action == "kakao_run_all":
            def _run(c, log):
                return kakao_pipeline.run_kakao_cycle(c, log=log, manage_job=True)
            return self._act_run_bg(db_type, _run, "카카오페이지 전체 확인")
        if action == "kakao_download":
            t = ss.get_kakao_title(sid)
            if not t:
                return False, "등록되지 않은 작품입니다"
            if pipeline.bl_blocked(cfg, t):
                return False, pipeline.genre_block_msg(cfg, t)
            if self._start_kakao_worker(cfg, sid):
                return True, "카카오 %s 다운로드 시작됨(백그라운드)" % t.get("title", sid)
            # 다른 작품을 받는 중이면 거절하지 않고 대기열에 넣는다(끝나면 이어서 받음)
            n = ss.kakao_queue_push(sid)
            tjob = ss.load_title_job_state()
            return True, "대기열에 추가됨(%d번째) - 지금 '%s' 다운로드 중" % (n, tjob.get("title") or "")
        return False, "알 수 없는 카카오 액션: %s" % action

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
                    cancelled = bool(job_now.get("cancel_requested"))
                    msg = job_now.get("message") or ""
                    # 진행 중 문구("~ 중")가 그대로 남아 완료 후에도 계속 수집 중처럼
                    # 보이던 문제 - 끝났으면 완료/취소 문구로 바꿔 둔다
                    if not msg or msg.rstrip().endswith("중"):
                        msg = "%s %s" % (label, "취소됨" if cancelled else "완료")
                    ss.save_job_state({"running": False, "finished_at": time.time(), "message": msg,
                                        "stage": "done" if not cancelled else "cancelled"})

        t = threading.Thread(target=_low_prio(_runner), name="webtoon_manager_%s" % action_slug(label),
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
            stored = ss.get_title(str(title_id)) or {}
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
        # force=True면 이미 zip이 있어도 지우고 처음부터 다시 받는다.
        # "선택 회차 다운로드"(체크박스로 특정 회차를 콕 찍어 요청)는 파일이
        # 잘못됐다고 판단해서 누르는 명시적 재다운로드 요청으로 보고 기본
        # force=True. "전체 다운로드(무료만)"는 밀린 걸 채우는 용도라 이미
        # 받은 건 그대로 스킵해야 하므로 프런트에서 force=False를 보낸다.
        force = bool(payload.get("force", True))
        if not title_id or not episode_nos:
            return False, "titleId/episodeNos 필요"

        # ComicInfo.xml용 메타데이터 - 구독 목록에 있는 작품이면 작가/장르/
        # 성인여부까지 채울 수 있고, 구독 목록에 없는(수동 조회만 한) 작품이면
        # 제목 외 필드는 빈 채로 둔다(그래도 XML 자체는 만들어짐). 회차별
        # 소제목(subtitle)은 이 함수에 안 넘어오므로 <Title> 태그는 생략된다.
        _t_for_comicinfo = ss.get_title(str(title_id)) or {"title": title}
        if pipeline.bl_blocked(self._get_cfg(db_type), _t_for_comicinfo):
            return False, pipeline.genre_block_msg(self._get_cfg(db_type), _t_for_comicinfo)

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

        if cfg.get("GENERATE_SERIES_JSON", True):
            downloader.write_series_json(
                _dl_root, title, title_id,
                pipeline._series_json_meta_for(_t_for_comicinfo, title_id), log=ss.append_log)

        def _runner():
            from . import downloader as dl
            if cfg.get("LOW_PRIORITY_MODE", True):
                dl.lower_thread_priority(int(cfg.get("DOWNLOAD_NICE_LEVEL", 10)))
            session = pipeline.build_session_from_cfg(cfg)
            ok_count = 0
            consecutive_fail = 0
            for i, no in enumerate(episode_nos):
                if ss.load_title_job_state().get("cancel_requested"):
                    ss.append_log("선택 회차 다운로드 취소됨")
                    break
                ss.save_title_job_state({"progress": i, "message": "[네이버웹툰] %s %s화 받는 중 (%d/%d화)" % (
                    title, no, i + 1, len(episode_nos))})
                try:
                    ok, skipped, cnt, err = dl.download_episode(
                        session, cfg.get("DOWNLOAD_ROOT") or ss.DOWNLOAD_DEFAULT_DIR,
                        cfg.get("TEMP_DOWNLOAD_ROOT") or ss.TMP_DOWNLOAD_DEFAULT_DIR,
                        title, title_id, no,
                        image_zero_fill=int(cfg.get("IMAGE_ZERO_FILL", 4)),
                        folder_zero_fill=int(cfg.get("FOLDER_ZERO_FILL", 4)),
                        max_concurrent=int(cfg.get("MAX_CONCURRENT_DOWNLOADS", 5)),
                        delay_seconds=float(cfg.get("DELAY_SECONDS", 1.0)),
                        timeout=int(cfg.get("REQUEST_TIMEOUT_SECONDS", 10)),
                        log=ss.append_log, force=force)
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
                            cfg.get("TEMP_DOWNLOAD_ROOT") or ss.TMP_DOWNLOAD_DEFAULT_DIR,
                            title, title_id, no,
                            folder_zero_fill=int(cfg.get("FOLDER_ZERO_FILL", 4)),
                            log=ss.append_log,
                            zip_stored=bool(cfg.get("ZIP_STORED", True)),
                            session=session,
                            cover_url=_t_for_comicinfo.get("thumbnail")
                            if cfg.get("ADD_COVER_AS_FIRST_PAGE", True) else None,
                            comicinfo_meta=pipeline._comicinfo_meta_for(
                                _t_for_comicinfo, {"no": no, "subtitle": None}, title_id)
                            if cfg.get("GENERATE_COMICINFO_XML", True) else None)
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
            # 회차 목록이 바뀌었을 수 있으니 kavita.yaml 갱신(내용이 같으면 그대로 둠)
            pipeline.update_kavita_yaml(
                cfg, session, cfg.get("DOWNLOAD_ROOT") or ss.DOWNLOAD_DEFAULT_DIR,
                title_id, log=ss.append_log)
            ss.save_title_job_state({"running": False, "finished_at": time.time(),
                                      "message": "선택 회차 다운로드 완료(%d화)" % ok_count})

        t = threading.Thread(target=_low_prio(_runner), name="webtoon_manager_manual_dl", daemon=True)
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
        if pipeline.bl_blocked(cfg, t_info):
            return False, pipeline.genre_block_msg(cfg, t_info)

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

        if cfg.get("GENERATE_SERIES_JSON", True):
            downloader.write_series_json(
                _dl_root, title_name, title_id,
                pipeline._series_json_meta_for(t_info, title_id), log=ss.append_log)

        def _runner():
            from . import downloader as dl
            if cfg.get("LOW_PRIORITY_MODE", True):
                dl.lower_thread_priority(int(cfg.get("DOWNLOAD_NICE_LEVEL", 10)))
            session = pipeline.build_session_from_cfg(cfg)
            try:
                new_eps = pipeline._episodes_to_download(
                    session, cfg, str(title_id), t_info.get("last_downloaded_no"))
            except Exception as e:  # noqa: BLE001
                ss.append_log("회차 목록 조회 실패: %s" % e)
                ss.save_title_job_state({"running": False, "last_error": str(e)})
                return

            if not new_eps:
                # 새 회차가 없어도 kavita.yaml이 없거나 낡았으면 맞춰 둔다
                pipeline.update_kavita_yaml(
                    cfg, session, cfg.get("DOWNLOAD_ROOT") or ss.DOWNLOAD_DEFAULT_DIR,
                    title_id, log=ss.append_log)
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
                if ep.get("charge") and not pipeline.naver_try_owned_paid(cfg):
                    ss.append_log("%s %s화: 유료(charge=true) 회차, 목록 API 기준 - 이후 회차도 유료로 보고 중단" % (title_name, ep["no"]))
                    ss.append_history({"type": "skipped_paid", "source": "auto",
                                        "title_id": title_id,
                                        "title": title_name, "episode_no": ep["no"],
                                        "error": "유료 회차(목록 API charge=true)"})
                    break
                ss.save_title_job_state({"progress": i, "total": len(new_eps), "message": "[네이버웹툰] %s %s화 받는 중 (%d/%d화)" % (
                    title_name, ep["no"], i + 1, len(new_eps))})
                try:
                    ok, skipped, cnt, err = dl.download_episode(
                        session, cfg.get("DOWNLOAD_ROOT") or ss.DOWNLOAD_DEFAULT_DIR,
                        cfg.get("TEMP_DOWNLOAD_ROOT") or ss.TMP_DOWNLOAD_DEFAULT_DIR,
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
                if ep.get("charge") and not ok:
                    ss.append_log("%s %s화: 유료 회차(대여/소장 안 됨) - 여기까지" % (title_name, ep["no"]))
                    break
                if ok and ep.get("charge") and not skipped:
                    ss.append_log("%s %s화: 유료 회차지만 대여/소장 중이라 받음" % (title_name, ep["no"]))
                if ok:
                    if skipped:
                        consecutive_fail = 0
                        last_ok_no = ep["no"]
                    else:
                        ok_count += 1
                        ss.append_history({"type": "download", "source": "auto",
                                            "title_id": title_id,
                                            "title": title_name, "episode_no": ep["no"],
                                            "image_count": cnt})
                        # 이미지 다운로드(1단계)와 분리된 2단계 - 별도로 압축한다.
                        c_ok, c_path, c_msg = dl.compress_episode(
                            cfg.get("DOWNLOAD_ROOT") or ss.DOWNLOAD_DEFAULT_DIR,
                            cfg.get("TEMP_DOWNLOAD_ROOT") or ss.TMP_DOWNLOAD_DEFAULT_DIR,
                            title_name, title_id, ep["no"],
                            folder_zero_fill=int(cfg.get("FOLDER_ZERO_FILL", 4)),
                            log=ss.append_log,
                            zip_stored=bool(cfg.get("ZIP_STORED", True)),
                            session=session,
                            cover_url=t_info.get("thumbnail")
                            if cfg.get("ADD_COVER_AS_FIRST_PAGE", True) else None,
                            comicinfo_meta=pipeline._comicinfo_meta_for(t_info, ep, title_id)
                            if cfg.get("GENERATE_COMICINFO_XML", True) else None)
                        if c_ok:
                            consecutive_fail = 0
                            last_ok_no = ep["no"]
                        else:
                            # pipeline.run_download_cycle과 동일한 이유로 수정:
                            # 압축 실패를 "완료"로 잘못 취급해 last_ok_no를
                            # 전진시키면, 실제로는 zip이 없는데도 다음 확인부터
                            # 이 회차가 영구히 재시도 대상에서 빠진다.
                            ss.append_log("%s %s화 압축 실패: %s (재시도 대상으로 남김)" % (title_name, ep["no"], c_msg))
                            consecutive_fail += 1
                            if consecutive_fail >= pipeline._MAX_CONSECUTIVE_FAILURES:
                                ss.append_log("titleId=%s 연속 %d회 실패(압축 포함) - 중단" %
                                               (title_id, consecutive_fail))
                                break
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
            # 새 회차가 추가됐으면 kavita.yaml 갱신(내용이 같으면 그대로 둠)
            pipeline.update_kavita_yaml(
                cfg, session, cfg.get("DOWNLOAD_ROOT") or ss.DOWNLOAD_DEFAULT_DIR,
                title_id, log=ss.append_log)
            ss.save_title_job_state({"running": False, "finished_at": time.time(),
                                      "message": "%s 다운로드 완료(%d화)" % (title_name, ok_count)})

        t = threading.Thread(target=_low_prio(_runner), name="webtoon_manager_dl_title", daemon=True)
        t.start()
        return True, "%s 다운로드 시작됨(백그라운드)" % title_name

    def _act_test_discord(self, db_type):
        cfg = self._get_cfg(db_type)
        ok, msg = discord_notify.notify(cfg, "🔔 웹툰 다운로더 플러그인 테스트",
                                         "이 메시지가 보이면 디스코드 알림 설정이 정상입니다.")
        return ok, msg

    # ------------------------------------------------------------------
    # 특정 라이브러리와 비교해서 "이미 라이브러리에 있는 웹툰"인지 알려주는 기능
    # ------------------------------------------------------------------
    def _act_list_libraries(self, db_type):
        """설정 탭의 "중복 확인 라이브러리" 드롭다운을 채우기 위해, 이
        db_type 스코프(general/adult)의 라이브러리 목록을 DB에서 직접
        조회한다. 코어의 /api/media/libraries HTTP 엔드포인트를 다시
        호출하는 대신 게이트웨이로 바로 조회하는 게 더 간단하다(같은
        프로세스 안이라 별도 HTTP 왕복이 필요 없음)."""
        try:
            gw = self.get_db_gateway(db_type)
            rows = gw.fetch_all("SELECT id, name FROM libraries ORDER BY name")
            libs = [{"id": r["id"], "name": r["name"]} for r in rows]
            return True, json.dumps({"libraries": libs}, ensure_ascii=False)
        except Exception as e:  # noqa: BLE001
            return False, "라이브러리 목록 조회 실패: %s" % e

    def _act_set_compare_library(self, db_type, payload):
        """설정 탭에서 고른 라이브러리를 "중복 확인 대상"으로 저장한다.
        library_id가 빈 값이면 비교 기능을 끈다."""
        library_id = payload.get("libraryId") or ""
        library_name = payload.get("libraryName") or ""
        ok = self._save_cfg_patch(db_type, {
            "COMPARE_LIBRARY_ID": library_id,
            "COMPARE_LIBRARY_NAME": library_name,
        })
        if not ok:
            return False, "저장 실패"
        if library_id:
            return True, "'%s' 라이브러리와 중복 확인을 시작합니다" % library_name
        return True, "중복 확인 기능을 껐습니다"

    # ------------------------------------------------------------------
    # 카테고리탭 설정 편집 (코어 플러그인 설정 화면 대신)
    # ------------------------------------------------------------------
    # (섹션, 그룹 제목, 키들) - 섹션은 설정 화면의 [공통][네이버][카카오페이지] 탭
    # (섹션, 그룹 제목, 키들[, 그룹 설명]) - 섹션은 설정 화면의 탭
    _SETTINGS_GROUPS = [
        ("common", "자동 실행", ("ENABLE_SCHEDULER", "INTERVAL_MINUTES", "FINISHED_SCAN_HOUR", "NEW_EP_SCOPE")),
        ("common", "구독 / 다운로드 대상", ("AUTO_SUBSCRIBE_NEW_TITLES", "INITIAL_EPISODES_LIMIT",
                                         "ALLOW_BL", "ALLOW_GL"),
         "매일+·기다무 작품과 이미 갖고 있는 작품을 받을지는 각 플랫폼 탭에서 정합니다."),
        ("common", "중복 확인(모든 플랫폼)", ("COMPARE_FOLDER", "COMPARE_LIBRARY_ID"),
         "이미 갖고 있는 작품에 '📚 보유중' 뱃지를 붙이고, 각 플랫폼 탭의 '이미 갖고 있는 작품도 받기'가 꺼져 있으면 "
         "자동 다운로드에서 뺍니다. 플랫폼별 폴더는 각 탭에서 지정합니다."),
        ("common", "다운로드 속도", ("FAST_MODE", "PARALLEL_TITLES", "MAX_CONCURRENT_DOWNLOADS", "DELAY_SECONDS",
                                   "MAX_NEW_EPISODES_PER_TITLE", "REQUEST_TIMEOUT_SECONDS")),
        ("common", "서버 부하", ("LOW_PRIORITY_MODE", "DOWNLOAD_NICE_LEVEL")),
        ("common", "저장 파일", ("TEMP_DOWNLOAD_ROOT", "ZIP_STORED", "ADD_COVER_AS_FIRST_PAGE",
                                "GENERATE_COMICINFO_XML", "GENERATE_SERIES_JSON", "GENERATE_KAVITA_YAML",
                                "KAVITA_YAML_EMBED_COVER", "FOLDER_ZERO_FILL", "IMAGE_ZERO_FILL")),
        ("common", "알림 / 쿠키 유지", ("COOKIE_KEEPALIVE_HOURS", "DISCORD_WEBHOOK_URL", "DISCORD_BOT_TOKEN",
                                       "DISCORD_CHANNEL_ID")),

        ("naver", "계정", ("NAVER_COOKIE_JSON",)),
        ("naver", "다운로드", ("DOWNLOAD_ROOT", "NAVER_DOWNLOAD_DAILY_PLUS", "NAVER_TRY_OWNED_PAID")),
        ("naver", "중복 확인", ("COMPARE_FOLDERS_NAVER", "NAVER_DOWNLOAD_OWNED")),

        ("kakao", "사용 / 계정(웹툰·웹소설 공통)", ("KAKAO_ENABLE", "KAKAO_COOKIE", "KAKAO_AUTO")),
        ("kakao", "다운로드", ("KAKAO_DOWNLOAD_ROOT", "KAKAO_DOWNLOAD_WAITFREE", "KAKAO_SYNC_PURCHASED")),
        ("kakao", "대여권(웹툰·웹소설 공통)", ("KAKAO_USE_WAITFREE", "KAKAO_USE_OWNED_TICKETS",
                                            "KAKAO_USE_PAID_TICKETS")),
        ("kakao", "중복 확인", ("COMPARE_FOLDERS_KAKAO", "KAKAO_DOWNLOAD_OWNED")),

        ("novel", "사용", ("KAKAO_NOVEL_ENABLE",)),
        ("novel", "다운로드", ("KAKAO_NOVEL_DOWNLOAD_ROOT", "KAKAO_NOVEL_DOWNLOAD_WAITFREE")),
        ("novel", "중복 확인", ("COMPARE_FOLDERS_NOVEL", "KAKAO_NOVEL_DOWNLOAD_OWNED")),
    ]

    _SETTINGS_SECTIONS = [("common", "공통"), ("naver", "네이버웹툰"), ("kakao", "카카오웹툰"),
                          ("novel", "카카오웹소설")]
    # 설정 탭의 다른 UI(중복 확인 라이브러리 드롭다운)가 따로 관리하는 키
    _SETTINGS_HIDDEN = ()

    def _settings_fields(self):
        by_key = {f["key"]: f for f in self.settings_schema}
        groups, seen = [], set()
        for g in self._SETTINGS_GROUPS:
            section, glabel, keys = g[0], g[1], g[2]
            fields = [dict(by_key[k]) for k in keys if k in by_key]
            seen.update(f["key"] for f in fields)
            if fields:
                groups.append({"section": section, "label": glabel, "fields": fields,
                               "desc": g[3] if len(g) > 3 else ""})
        rest = [dict(f) for f in self.settings_schema
                if f["key"] not in seen and f["key"] not in self._SETTINGS_HIDDEN
                and f.get("type") != "hidden"]
        if rest:
            groups.append({"section": "common", "label": "기타", "fields": rest})
        return groups

    def _act_get_settings(self, db_type):
        """설정 폼용 스키마 + 현재 값. 비밀번호/토큰/쿠키 값 자체는 내려보내지
        않고 '저장돼 있음' 여부만 알려준다(빈 칸으로 저장하면 기존 값 유지)."""
        cfg = self._get_cfg(db_type, for_settings=True)
        groups = self._settings_fields()
        values, secret_set = {}, {}
        for g in groups:
            for f in g["fields"]:
                k = f["key"]
                v = cfg.get(k, f.get("default"))
                if f.get("type") == "password":
                    secret_set[k] = bool(str(v or "").strip())
                    values[k] = ""
                else:
                    values[k] = v
        return True, json.dumps({"sections": [{"id": a, "label": b} for a, b in self._SETTINGS_SECTIONS],
                                 "groups": groups, "values": values, "secret_set": secret_set},
                                ensure_ascii=False)

    def _act_save_settings(self, db_type, payload):
        raw = payload.get("values")
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except ValueError:
                raw = None
        if not isinstance(raw, dict):
            return False, "저장할 값이 없습니다"
        clear = set(payload.get("clear") or [])
        types = {f["key"]: f.get("type", "text") for f in self.settings_schema}
        patch, errors = {}, []
        for k, v in raw.items():
            t = types.get(k)
            if t is None or k in self._SETTINGS_HIDDEN:
                continue
            if t == "checkbox":
                patch[k] = v if isinstance(v, bool) else str(v).lower() in ("1", "true", "on", "yes")
            elif t == "number":
                if v in ("", None):
                    patch[k] = DEFAULTS.get(k, "")
                    continue
                try:
                    num = float(v)
                    patch[k] = int(num) if num == int(num) and not isinstance(DEFAULTS.get(k), float) else num
                except (TypeError, ValueError):
                    errors.append(k)
            elif t == "password":
                if k in clear:
                    patch[k] = ""
                elif str(v or "").strip():
                    patch[k] = str(v)      # 빈 칸이면 기존 값 유지
            elif t == "select":
                opts = [o[0] for o in (next((f for f in self.settings_schema if f["key"] == k), {})
                                       .get("options") or [])]
                v = str(v or "").strip()
                if opts and v not in opts:
                    errors.append(k)
                else:
                    patch[k] = v
            else:
                patch[k] = str(v if v is not None else "").strip()
        if errors:
            return False, "잘못된 값이 있습니다: %s" % ", ".join(errors)
        if not self._save_cfg_patch(db_type, patch):
            return False, "설정 저장 실패"
        return True, "설정을 저장했습니다(%d개 항목)" % len(patch)

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
        except Exception:  # noqa: BLE001
            return False

    def _get_compare_library_series_set(self, db_type, cfg):
        """설정된 비교 라이브러리(BookOasis DB)에 등록된 시리즈명 집합을
        반환한다. 조회 실패 시 예외 메시지를 함께 돌려줘서, 화면에 "왜 비교가
        안 되는지"를 드러낼 수 있게 한다(예전에는 조용히 None만 반환해서
        사용자가 기능이 없는 줄 알았다).
        반환: (set|None, error_message|None)"""
        library_id = cfg.get("COMPARE_LIBRARY_ID")
        if not library_id:
            return None, None
        try:
            gw = self.get_db_gateway(db_type)
            # 게이트웨이는 SQLite/MariaDB 어느 엔진이든 '?' 플레이스홀더로
            # 통일해서 받는 것으로 가정한다(가이드의 다른 예시 쿼리들과 동일한
            # 관례). 코어 버전에 따라 스키마가 다르면 아래 except가 사유를
            # 문자열로 돌려주고, 그 내용이 설정 탭에 그대로 표시된다.
            rows = gw.fetch_all(
                "SELECT DISTINCT series_name FROM books WHERE library_id = ? "
                "AND COALESCE(is_deleted, 0) = 0",
                (library_id,))
            return set(compare.normalize(r["series_name"])
                       for r in rows if r.get("series_name")), None
        except Exception as e:  # noqa: BLE001
            return None, "라이브러리 조회 실패: %s" % e

    def _build_compare_set(self, db_type, cfg):
        """중복 확인 기준(compare.py). 라이브러리 조회는 이 클래스의 DB 접근을 쓴다."""
        compare.set_library_source(lambda c, _db=db_type: self._get_compare_library_series_set(_db, c))
        return compare.build(cfg)


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
    # 자동 다운로드의 "보유 작품 건너뛰기"도 라이브러리 기준을 쓸 수 있게 등록
    compare.set_library_source(
        lambda c: _bootstrap_provider._get_compare_library_series_set("general", c))
    scheduler.ensure_started(lambda: _bootstrap_provider._get_cfg("general"),
                              pipeline.run_full_cycle,
                              pipeline.run_finished_scan_job)
except Exception:  # noqa: BLE001
    pass
