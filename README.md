# webtoon_manager (BookOasis 플러그인) — 표시 이름 "웹툰 다운로더"

원본: https://github.com/murianwind/webtoon-manager (네이버웹툰 무료 회차 자동 구독/다운로드 독립 웹앱)
을 BookOasis 카테고리탭 플러그인으로 이식. **버전 1.5.1**

## 설치

1. 이 폴더 전체를 `plugins/metadata/webtoon_manager/` 로 복사
   (`core/` 서브폴더 없이 전부 flat 구조로 넣을 것 — 아래 "구조" 참고)
2. BookOasis 재시작
3. 환경설정 > 플러그인 설정 > **웹툰 다운로더** 활성화
4. 아래 값을 입력 후 저장 (자세한 항목별 설명은 카테고리탭의 **"도움말"** 탭 참고)
   - 네이버 아이디/비밀번호(참고용, 자동 로그인에는 쓰지 않음)
   - **네이버 쿠키(JSON)**: 크롬 확장 **Cookie-Editor**로 comic.naver.com에서
     "Export as JSON"한 값을 그대로 붙여넣기 (감싸지 않은 배열 `[...]`,
     Playwright storage_state 형식 `{"cookies":[...]}` 둘 다 지원)
   - 다운로드 저장 경로(비우면 `plugins/data/webtoon_manager/downloads` 사용)
   - 자동 실행 여부/주기(요일별), 완결 전체 목록 수집 시각, 회차 상한, 동시 다운로드 수 등
   - (선택) 디스코드 웹훅 URL 또는 봇 토큰+채널ID
   - (선택) 다운로드 완료 시 BookOasis 라이브러리 자동 등록(`/api/webhook/scan` 토큰 인증)
5. 좌측 메뉴 **웹툰 다운로더** 탭에서 "지금 스캔(요일별)" → "지금 전체 실행"으로 동작 확인

## 구현 범위

- ✅ **요일별 스캔(빠름)** + **완결 전체 스캔(느림, 하루 중 지정 시각 1회)** 분리 —
  초기 설치 시 인덱싱이 완결 목록(최대 200페이지) 때문에 오래 걸리던 문제 해결
- ✅ 작가/태그 자동 구독, 요일/완결 필터 탭, 평점순·가나다순 정렬
- ✅ 신규 회차 자동 다운로드(작품별 상한 + 휴식 시간), 구독중 카드의 "수동 다운로드"
  버튼(자동 다운로드 대신 "수동 다운로드" 탭으로 이동 + 자동 조회)
- ✅ "수동 다운로드" 탭: titleId로 회차 조회(최대 200페이지 전부), 회차 선택 다운로드,
  "전체 다운로드(무료만)" 버튼, **유료 회차는 체크박스 자체가 비활성화**
- ✅ 다운로드 결과는 **시리즈 폴더 바로 밑에 압축파일 하나로 저장**
  (`{회차}화#{이미지장수}.zip`, 하위 폴더 없이 flat — 예: `15화#87.zip`).
  이미지 낱장으로 받았던 구버전 데이터도 다시 만나면 자동으로 압축 정리(마이그레이션)됨.
- ✅ **유료(코인 결제) 회차 감지** — 목록 API의 `charge` 필드 우선, 페이지 내 결제
  키워드로 보조 판별. "24시간마다 무료" 로테이션일 수 있어 영구 스킵하지 않고 다음
  스캔 때 재확인. 한 회차가 유료면 그 뒤도 순서대로 유료일 가능성이 높아 그 작품은
  거기서 멈추고 다음 작품으로 넘어감(불필요한 요청 최소화)
- ✅ 완결 감지 / 쿠키(성인 인증) 만료 / 다운로드 실패 요약 → 디스코드 알림
- ✅ 구독/구독해제/제외/되돌리기
- ✅ 다운로드 이력, 스케줄러(백그라운드 스레드 + PID 락 가드), 스캔/다운로드 각각
  독립된 취소·강제 초기화(카테고리탭 "도움말" 탭에 버튼 있음)
- ✅ **BookOasis 라이브러리 자동 등록**(선택) — 다운로드가 끝난 작품 폴더를
  `/api/webhook/scan`(토큰 인증)으로 즉시 스캔·등록. 관리자 세션 쿠키(비밀번호 저장)
  없이 `.env`의 `WEBHOOK_TOKEN`만 사용
- ✅ 카테고리탭 디자인은 `yume-script/plugin_board`와 동일한 전역 테마 변수
  (`var(--app-*)`) 사용 — 8종 대시보드 테마 자동 동기화
- ⚠️ **완결 알림의 디스코드 버튼(구독해제/알람끄기 즉시 처리)**: 원본은 자체 봇이 상시
  Discord Gateway에 접속해 버튼 클릭을 처리합니다. 이 플러그인은 요청-응답형 웹 플러그인이라
  상시 접속이 불가능해 버튼 대신 "카테고리탭에서 처리하라"는 안내 임베드만 보냅니다.
- ⚠️ 이미지 목록은 공식 공개 API가 없어 회차 상세 페이지 HTML에서 추출합니다.
  `class="wt_viewer"`가 `<img>`가 아니라 감싸는 `<div>`에만 붙는 템플릿(오래된/완결작)이
  있어, 클래스 유무 대신 실제 이미지 CDN 경로(`image-comic.pstatic.net`, 연령고지 배너
  제외)로 판별합니다(`naver_api.py`의 `_extract_comic_images`). 네이버가 CDN 경로
  자체를 바꾸면 이 부분만 고치면 됩니다.
- ⚠️ 쿠키 자동 재발급(로그인 자동화)은 지원하지 않습니다 — 네이버 로그인의 캡차/기기
  인증 보호를 우회하는 것이라 계정 이용제한 위험이 있어 의도적으로 구현하지 않았습니다.
  만료 시 카테고리탭에 알림이 뜨면 "도움말" 탭 안내대로 Cookie-Editor로 재발급해서
  다시 붙여넣어 주세요.

## 프레임워크 관련 가정(중요, 오류 나면 여기부터 확인)

실제 동작 중인 참조 플러그인(`plugin_board`)의 소스와 코어 라우트 파일
(`api/routes/plugin_routes.py`)을 직접 grep해서 확인한 계약입니다.

- **`category_tab`은 `True`가 아니라 `dict`**여야 좌측 사이드바에 전용 탭이 등록됩니다.
  `{"title": ..., "icon": ..., "order": ...}` 형태로 선언합니다. (`dashboard_widget`의
  `all_desk_tab`은 "공통 데스크" 카드용 별개 메커니즘이라 `category_tab`과 함께 쓰지 않습니다.)
- 데이터 조회: 카테고리탭 script.js가 `get_dashboard_data()`를 호출해 화면 데이터를 받습니다.
- 쓰기 액션: 실제 확인된 라우트는 `/api/media/context-menu/book/plugins/action`
  (`run_context_menu_action(db_type, action_id, context)` 호출)입니다. 이 라우트는
  요청 바디로 `type`/`plugin_id`/`action_id`/`context`만 읽습니다(`db_type`이 아니라
  `type`). **`run_context_menu_action()`은 `(bool, str)` 튜플이 아니라
  `{'success': bool, 'message'|'error': str}` dict를 반환해야** 합니다 — 튜플을 그대로
  반환하면 코어가 "반환값 형식이 올바르지 않습니다"로 간주해 HTTP 400을 내립니다.
  `apply()`는 base.py 계약대로 튜플을 유지하되(코어가 book_id 기반 메타데이터
  적용에만 사용하는 것으로 보임), 카테고리탭 액션의 실제 진입점은
  `run_context_menu_action()`입니다.
- `settings.html`은 **존재 자체가 "커스텀 설정 화면을 쓴다"는 신호**입니다(`plugin_board.py`의
  `_has_settings_ui()` 참고: 파일이 있으면 코어가 `config_schema` 자동 폼 대신 이 파일을
  설정 화면으로 씁니다). 안내 문구만 들어있는 `settings.html`을 두면 표준 폼이 안 뜨고
  입력할 곳이 사라지므로, **`settings.html`은 아예 만들지 않고 `config_schema`가 표준 폼을
  자동 생성**하도록 둡니다.
- **`__init__.py`는 반드시 빈 파일이어야 합니다.** `from .webtoon_manager import ...`처럼
  뭔가를 넣으면, 패키지 초기화 도중 하위 모듈들이 서로를 임포트할 때 순환참조
  (`ImportError: cannot import name 'X' from partially initialized module ...`)가
  발생합니다. 코어도 이 파일 내용에 의존하지 않으므로 절대 채우지 마세요.

## 파일 구조

`core/` 같은 서브패키지 없이 모든 모듈이 플러그인 루트에 flat 하게 있습니다.
모든 모듈은 `from . import xxx` 형태로 서로를 참조합니다.

```
webtoon_manager/
  __init__.py               # 반드시 빈 파일
  webtoon_manager.py         # 플러그인 메인 클래스
  VERSION
  requirements.txt            # 빈 파일(코어에 requests가 이미 있다는 전제)
  index.html / style.css / script.js   # 카테고리탭 풀페이지 UI (plugin_board와 동일 테마 변수)
  state_store.py              # 파일 기반 상태 저장(plugins/data/webtoon_manager/)
  naver_api.py                  # 목록/회차/이미지 스크래핑, 유료 회차 감지
  downloader.py                   # 이미지 다운로드 + 압축(zip) + 정리
  discord_notify.py                 # 웹훅/봇 알림
  scheduler.py                        # 백그라운드 주기 실행(요일별/완결 별도 스케줄)
  pipeline.py                           # 스캔→자동구독→다운로드→알림→라이브러리 등록 파이프라인
```

데이터(구독 목록/다운로드 이력/작업 상태)는 `plugins/data/webtoon_manager/`에
별도로 저장되어, 플러그인 코드(`plugins/metadata/webtoon_manager/`)를 재설치/업데이트해도
지워지지 않습니다.

## 의존성

`requests` 모듈이 필요합니다(BookOasis 코어가 이미 쓰고 있어 별도 설치가 필요 없을 가능성이 높습니다).
설치가 안 되어 있다는 오류가 나면 `requirements.txt`에 `requests` 한 줄만 추가해주세요.
