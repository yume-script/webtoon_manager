# webtoon_manager (BookOasis 플러그인)

원본: https://github.com/murianwind/webtoon-manager (네이버웹툰 무료 회차 자동 구독/다운로드 독립 웹앱)
을 BookOasis 카테고리탭 플러그인으로 이식.

## 설치

1. 이 폴더 전체를 `plugins/metadata/webtoon_manager/` 로 복사
   (BookOasis_stable 컨테이너 기준: `~/docker/BookOasis_stable/plugins/metadata/webtoon_manager/`)
2. BookOasis 재시작
3. 환경설정 > 플러그인 설정 > **웹툰 다운로더** 활성화
4. 아래 값을 입력 후 저장
   - 네이버 아이디/비밀번호(참고용, 자동 로그인에는 쓰지 않음)
   - **네이버 쿠키(JSON)**: 브라우저 확장(Cookie-Editor 등)으로 `comic.naver.com` 로그인 상태에서
     내보낸 Playwright storage_state 형식 JSON
     `{"cookies":[{"name":"NID_AUT","domain":".naver.com","value":"..."}, ...]}`
   - 다운로드 저장 경로(비우면 `plugins/data/webtoon_manager/downloads` 사용)
   - 자동 실행 여부/주기, 회차 상한, 동시 다운로드 수 등
   - (선택) 디스코드 웹훅 URL 또는 봇 토큰+채널ID
5. 좌측 메뉴 **웹툰 다운로더** 탭에서 "지금 스캔" → "지금 전체 실행"으로 동작 확인

## 구현 범위 (원본 대비)

- ✅ 요일별 연재 목록 스캔(빠름, 스케줄러 기본 주기마다) + 완결 전체 목록 스캔(느림,
  하루 중 정해진 시각에 1번만 - `FINISHED_SCAN_HOUR` 설정) - 초기 설치 시 완결
  목록(최대 200페이지) 수집이 요일별 스캔을 막지 않도록 분리했습니다.
- ✅ 작가/태그 자동 구독
- ✅ 신규 회차 자동 다운로드(작품별 상한 + 휴식 시간 반영), 구독중 카드의
  "지금 다운로드" 버튼으로 개별 작품만 즉시 받기(스캔/전체실행과 독립된 락)
- ✅ 완결 감지 / 쿠키(성인 인증) 만료 / 다운로드 실패 요약 → 디스코드 알림
- ✅ 유료(코인 결제) 회차 감지 - 목록 API의 `charge` 필드와 페이지 내 결제 키워드
  둘 다로 판별하며, "24시간마다 무료" 로테이션일 수 있어 영구 스킵이 아니라 다음
  스캔 때 재시도합니다. 한 회차가 유료면 그 뒤 회차들도 순서대로 유료일 가능성이
  높아 그 작품은 거기서 멈추고 다음 작품으로 넘어갑니다.
- ✅ 구독/구독해제/제외/되돌리기, 수동 titleId 조회 후 회차 선택 다운로드
- ✅ 다운로드 이력, 주기 실행 스케줄러(백그라운드 스레드 + PID 락 가드)
- ⚠️ **완결 알림의 디스코드 버튼(구독해제/알람끄기 즉시 처리)**: 원본은 자체 봇이 상시
  Discord Gateway에 접속해 버튼 클릭을 처리합니다. 이 플러그인은 요청-응답형 웹 플러그인이라
  상시 접속이 불가능해 버튼 대신 "카테고리탭에서 처리하라"는 안내 임베드만 보냅니다.
- ⚠️ 이미지 목록은 공식 공개 API가 없어 회차 상세 페이지 HTML에서 추출합니다.
  `class="wt_viewer"`가 `<img>`가 아니라 감싸는 `<div>`에만 붙는 템플릿(오래된/완결작)이
  있어, 클래스 유무 대신 실제 이미지 CDN 경로(`image-comic.pstatic.net`, 연령고지 배너
  제외)로 판별합니다(`naver_api.py`의 `_extract_comic_images`). 네이버가 CDN 경로
  자체를 바꾸면 이 부분만 고치면 됩니다.

## 프레임워크 관련 가정(중요, 오류 나면 여기부터 확인)

실제 동작 중인 참조 플러그인(`plugin_board`)의 소스로 아래 계약을 확인했습니다.

- **`category_tab`은 `True`가 아니라 `dict`**여야 좌측 사이드바에 전용 탭이 등록됩니다.
  `{"title": ..., "icon": ..., "order": ...}` 형태로 선언합니다. (`dashboard_widget`의
  `all_desk_tab`은 "공통 데스크" 카드용 별개 메커니즘이라 `category_tab`과 함께 쓰지 않습니다.)
- 데이터 조회: 카테고리탭 script.js가 `get_dashboard_data()`를 호출해 화면 데이터를 받습니다.
- 쓰기 액션: `apply(db_type, book_id, item_data)`가 실제 액션 RPC 채널입니다.
  `item_data["action"]`으로 분기하며, `book_id`는 이 플러그인에서는 의미 없는 값(0 등)으로
  넘어옵니다. 동일 로직을 `run_context_menu_action()`으로도 노출해 컨텍스트 메뉴에서도
  호출할 수 있게 합니다. (`run_action()`이라는 별도 메서드는 코어가 인식하지 않는
  존재하지 않는 계약이라 삭제했습니다.)
- `settings.html`은 **존재 자체가 "커스텀 설정 화면을 쓴다"는 신호**입니다(`plugin_board.py`의
  `_has_settings_ui()` 참고: 파일이 있으면 코어가 `config_schema` 자동 폼 대신 이 파일을
  설정 화면으로 씁니다). 안내 문구만 들어있는 `settings.html`을 두면 표준 폼이 안 뜨고
  입력할 곳이 사라지므로, **`settings.html`은 아예 만들지 않고 `config_schema`가 표준 폼을
  자동 생성**하도록 둡니다. 커스텀 폼이 정말 필요해지면 그때 실제 입력 필드를 갖춘
  `settings.html`/`settings.css`/`settings.js`로 새로 만들어야 합니다.
- **`__init__.py`는 반드시 빈 파일이어야 합니다.** `from .webtoon_manager import ...`처럼
  뭔가를 넣으면, 패키지 초기화 도중 하위 모듈들이 서로를 임포트할 때 순환참조
  (`ImportError: cannot import name 'X' from partially initialized module ...`)가
  발생합니다. 코어도 이 파일 내용에 의존하지 않으므로 절대 채우지 마세요.

## 파일 구조

이 저장소는 `core/` 같은 서브패키지 없이 모든 모듈이 플러그인 루트에 flat 하게 있습니다.
`webtoon_manager.py`를 비롯한 모든 모듈은 `from . import xxx` 형태로 서로를 참조합니다.

```
webtoon_manager/
  __init__.py
  webtoon_manager.py     # 플러그인 메인 클래스
  VERSION
  requirements.txt        # 빈 파일(코어에 requests가 이미 있다는 전제)
  index.html / style.css / script.js   # 카테고리탭 풀페이지 UI
  settings.html            # (사용 안 함 — 존재하면 config_schema 자동 폼이 꺼지므로 만들지 않음)
  state_store.py            # 파일 기반 상태 저장(모듈 재로드에도 값 유지)
  naver_api.py               # 목록/회차/이미지 스크래핑
  downloader.py                # 이미지 다운로드/저장
  discord_notify.py              # 웹훅/봇 알림
  scheduler.py                     # 백그라운드 주기 실행
  pipeline.py                        # 스캔→자동구독→다운로드→알림 파이프라인
```

## 의존성

`requests` 모듈이 필요합니다(BookOasis 코어가 이미 쓰고 있어 별도 설치가 필요 없을 가능성이 높습니다).
설치가 안 되어 있다는 오류가 나면 `requirements.txt`에 `requests` 한 줄만 추가해주세요
(과거 경험상 슬래시로 나열한 설명 주석이 패키지명으로 잘못 파싱되는 문제가 있었으니, **실제 패키지명만
한 줄에 하나씩** 넣어야 합니다).
