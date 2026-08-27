# webtoon_manager (BookOasis 플러그인)

원본: https://github.com/murianwind/webtoon-manager (네이버웹툰 무료 회차 자동 구독/다운로드 독립 웹앱)
을 BookOasis 카테고리탭 플러그인으로 이식.

## 설치

1. 이 폴더 전체를 `plugins/metadata/webtoon_manager/` 로 복사
   (BookOasis_stable 컨테이너 기준: `~/docker/BookOasis_stable/plugins/metadata/webtoon_manager/`)
2. BookOasis 재시작
3. 환경설정 > 플러그인 설정 > **웹툰 관리** 활성화
4. 아래 값을 입력 후 저장
   - 네이버 아이디/비밀번호(참고용, 자동 로그인에는 쓰지 않음)
   - **네이버 쿠키(JSON)**: 브라우저 확장(Cookie-Editor 등)으로 `comic.naver.com` 로그인 상태에서
     내보낸 Playwright storage_state 형식 JSON
     `{"cookies":[{"name":"NID_AUT","domain":".naver.com","value":"..."}, ...]}`
   - 다운로드 저장 경로(비우면 `plugins/data/webtoon_manager/downloads` 사용)
   - 자동 실행 여부/주기, 회차 상한, 동시 다운로드 수 등
   - (선택) 디스코드 웹훅 URL 또는 봇 토큰+채널ID
5. 좌측 메뉴 **웹툰 관리** 탭에서 "지금 스캔" → "지금 전체 실행"으로 동작 확인

## 구현 범위 (원본 대비)

- ✅ 요일별/완결 목록 스캔, 작가/태그 자동 구독
- ✅ 신규 회차 자동 다운로드(작품별 상한 + 휴식 시간 반영)
- ✅ 완결 감지 / 쿠키(성인 인증) 만료 / 다운로드 실패 요약 → 디스코드 알림
- ✅ 구독/구독해제/제외/되돌리기, 수동 titleId 조회 후 회차 선택 다운로드
- ✅ 다운로드 이력, 주기 실행 스케줄러(백그라운드 스레드 + PID 락 가드)
- ⚠️ **완결 알림의 디스코드 버튼(구독해제/알람끄기 즉시 처리)**: 원본은 자체 봇이 상시
  Discord Gateway에 접속해 버튼 클릭을 처리합니다. 이 플러그인은 요청-응답형 웹 플러그인이라
  상시 접속이 불가능해 버튼 대신 "카테고리탭에서 처리하라"는 안내 임베드만 보냅니다.
- ⚠️ 이미지 목록은 공식 공개 API가 없어 회차 상세 페이지 HTML에서 정규식으로 추출합니다.
  네이버가 마크업을 바꾸면 `naver_api.py`의 `_IMG_RE` 부분만 고치면 됩니다.

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
- 설정 폼 커스터마이징이 필요하면 `settings.html`(+ `settings.css`/`settings.js`)을 따로
  둡니다. `index.html`/`style.css`/`script.js`는 `category_tab` 선언 시 **필수인 카테고리
  풀페이지 뷰**이고, 이 둘은 서로 다른 용도입니다.

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
  settings.html            # 설정 모달 안내문(세부 입력은 config_schema 표준 폼 사용)
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
