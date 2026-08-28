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

## 프레임워크 관련 가정(중요, 오류 나면 여기부터 확인)

BookOasis 플러그인 가이드 문서에는 `get_dashboard_data()`(대시보드/카테고리탭 데이터 폴링)까지만
명시되어 있고, 카테고리탭에서 "구독/제외/작가 등록" 같은 **쓰기 액션을 어떤 URL로 호출하는지는
문서화되어 있지 않습니다.** 이번 코드는 이전에 만든 다른 카테고리탭 플러그인(예: jikji_sf,
gd_poller4bookoasis, rclone_g2g_copy)에서 확인된 아래 두 가지 관례를 그대로 따랐습니다.

- 데이터 조회: `GET /api/media/dashboard/widgets/{plugin_id}/data` → `get_dashboard_data()` 호출
- 쓰기 액션: `apply(db_type, book_id=0, item_data)`를 범용 RPC 채널로 사용(rclone_g2g_copy 방식)
  + 동일 로직을 `run_context_menu_action()`으로도 노출(컨텍스트 메뉴 엔드포인트로도 호출되게)

`script.js`의 `callAction()`은 실제 설치된 BookOasis 버전에서 어느 엔드포인트가 맞는지
**여러 후보를 순서대로 시도**하도록 만들어 뒀습니다(`/api/media/context-menu/book/plugins/action`
→ `/api/media/dashboard/widgets/{id}/action` → `/api/media/metadata/apply`). 버튼을 눌렀는데
"백엔드 액션 엔드포인트를 찾지 못했습니다" 라는 알림이 뜨면, 브라우저 개발자도구 Network 탭에서
실제 어떤 요청이 실패했는지(404/405 등) 캡처해서 알려주시면 정확한 엔드포인트로 바로 고쳐드립니다.

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
