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
  네이버가 마크업을 바꾸면 `core/naver_api.py`의 `_IMG_RE` 부분만 고치면 됩니다.

## 프레임워크 관련 확인/가정

- **`category_tab`은 dict여야 좌측 메뉴에 등록됩니다.** 실제 동작 중인 `plugin_board`
  플러그인 소스로 확인함: `category_tab = {"title": ..., "icon": ..., "order": ...}`.
  (처음에 `category_tab = True`로 잘못 선언해서 사이드바에 안 떴던 적이 있었습니다.)
- **`settings.html`을 만들면 `config_schema` 자동 생성 폼을 완전히 대체합니다.**
  실제 입력 필드 없이 안내 문구만 넣었더니 설정 화면에 아무 입력창도 안 뜨는 문제가
  있었습니다 — 그래서 이 버전에는 `settings.html`이 없고, `config_schema`가 표준 폼을
  그대로 그리도록 뒀습니다.
- **데이터 조회**: `GET /api/media/dashboard/widgets/{plugin_id}/data` → `get_dashboard_data()` 호출
  (jikji_sf 등에서 확인됨)
- **쓰기 액션**: `apply(db_type, book_id, item_data)`을 범용 RPC 채널로 사용하며, `item_data`는
  `{"action": "...", "plugin_id": "...", ...}` 형태로 **평평하게(flat)** 담아 보냅니다
  (`plugin_board.py`의 `apply()`/`_dispatch_apply()`에서 확인됨). 다만 이 액션을 실제로
  호출하는 **정확한 엔드포인트 URL**은 아직 확인 못 했습니다 — `script.js`의 `callAction()`이
  여러 후보(`/api/media/dashboard/widgets/{id}/action`, `/api/media/metadata/plugins/action`,
  `/api/media/metadata/apply`, `/api/media/context-menu/book/plugins/action`)를 순서대로
  시도하도록 만들어 뒀습니다. 버튼을 눌렀는데 "백엔드 액션 엔드포인트를 찾지 못했습니다"가 뜨면,
  브라우저 개발자도구 Network 탭에서 실제 요청 URL/응답 코드를 캡처해서 알려주시면 바로
  정확한 엔드포인트로 고쳐드립니다.

## 파일 구조

core/ 하위 폴더 없이 전부 `webtoon_manager.py` 한 파일에 통합했습니다
(상태저장소 / 네이버 API / 다운로더 / 디스코드 알림 / 스케줄러 / 파이프라인 /
플러그인 클래스 전부 포함). BookOasis가 이 플러그인 모듈을 정확히 어떤 방식으로
import하는지 문서화되어 있지 않아, 상대/절대 임포트 경로 문제 자체를 아예
없애기 위한 선택입니다.

```
webtoon_manager/
  __init__.py
  webtoon_manager.py     # 플러그인 전체 (상태저장/네이버API/다운로더/디스코드/스케줄러/파이프라인/클래스)
  VERSION
  requirements.txt        # 빈 파일(코어에 requests가 이미 있다는 전제)
  index.html / style.css / script.js   # 카테고리탭 풀페이지 UI
  settings.html            # 설정 모달 안내문(세부 입력은 config_schema 표준 폼 사용)
```

## 의존성

`requests` 모듈이 필요합니다(BookOasis 코어가 이미 쓰고 있어 별도 설치가 필요 없을 가능성이 높습니다).
설치가 안 되어 있다는 오류가 나면 `requirements.txt`에 `requests` 한 줄만 추가해주세요
(과거 경험상 슬래시로 나열한 설명 주석이 패키지명으로 잘못 파싱되는 문제가 있었으니,
**실제 패키지명만 한 줄에 하나씩** 넣어야 합니다).
