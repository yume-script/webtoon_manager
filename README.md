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
- ⚠️ **목록 수집 방식(중요)**: 예전 `/api/webtoon/titlelist/...` JSON API 대신, 사용자가 확인해준
  실제 URL(`https://comic.naver.com/webtoon?tab=mon` 등 요일 7개 + `dailyPlus`(일일플러스) +
  `finish`(완결))을 그대로 사용합니다. 이 페이지들은 Next.js 기반으로 보여서, HTML 안에 박힌
  `<script id="__NEXT_DATA__">...JSON...</script>`을 파싱해 목록을 추출합니다. 다만 그 JSON의
  정확한 키 구조(중첩 깊이 등)는 실제로 받아보지 못한 채 만들었기 때문에, `titleId`+`title` 계열
  키를 가진 dict를 트리 전체에서 재귀적으로 훑어 찾는 **범용/방어적 방식**으로 짰습니다.
  스캔을 실행했는데 작품이 하나도 안 잡히면, `job.log`(설정 화면 하단 "실행 로그")에서
  "__NEXT_DATA__를 찾지 못함" 류의 메시지가 있는지 확인해주시고, 안 되면 해당 페이지의
  `<script id="__NEXT_DATA__">` 내용 일부(또는 전체)를 보여주시면 정확한 키에 맞춰 파서를 고쳐드립니다.
- ⚠️ 완결 알림의 디스코드 버튼(그 자리에서 구독해제/알람끄기)은 원본이 상시 접속 봇으로 처리하는 건데,
  이 플러그인은 요청-응답형이라 상시 접속이 불가능해 버튼 대신 안내 임베드만 보냅니다.

## 프레임워크 관련 확인/가정

- **`category_tab`은 dict여야 좌측 메뉴에 등록됩니다.** 실제 동작 중인 `plugin_board`
  플러그인 소스로 확인함: `category_tab = {"title": ..., "icon": ..., "order": ...}`.
- **`settings.html`을 만들면 `config_schema` 자동 생성 폼을 완전히 대체합니다.**
  그래서 이 버전에는 `settings.html`이 없고, `config_schema`가 표준 폼을 그대로 그리도록 뒀습니다.
- **데이터 조회**: `GET /api/media/dashboard/widgets/{plugin_id}/data?type={dbType}`
  (쿼리 파라미터 이름이 `db_type`이 아니라 `type`) — `plugin_board`의 실제 `script.js`에서 확인.
- **쓰기 액션**: `POST /api/media/books/0/apply-metadata`,
  body `{ type: dbType, source: plugin_id, item_data: {action, ...} }`.
  `book_id=0`이 URL 경로(`/books/0/...`)에 고정되어 있고, `item_data`가 파이썬
  `apply(db_type, book_id, item_data)`의 `item_data` 인자로 그대로 전달됩니다.
  (역시 `plugin_board`의 실제 `script.js`에서 확인 — 더 이상 추측이 아닙니다.)

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
