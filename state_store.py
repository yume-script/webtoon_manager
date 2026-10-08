# -*- coding: utf-8 -*-
"""
webtoon_manager 상태 저장소
---------------------------
BookOasis 플러그인은 요청마다 모듈이 새로 로드될 수 있다. 그래서 모든 상태(구독 목록/작가·태그/다운로드 이력/
잡 진행상태/입력값 복원용 임시값)를 plugins/data/webtoon_manager/ 아래
JSON 파일로 저장하고, 매 요청마다 파일을 열어 읽고 닫는다.
"""

import json
import os
import tempfile
import threading
import time

# 이 파일은 plugins/metadata/webtoon_manager/state_store.py 에 있다(core/ 서브폴더
# 없이 flat 구조). 여기서 plugins/data/webtoon_manager/ 를 데이터 폴더로 사용한다.
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))          # plugins/metadata/webtoon_manager
_METADATA_DIR = os.path.dirname(_THIS_DIR)                      # plugins/metadata
_PLUGINS_ROOT = os.path.dirname(_METADATA_DIR)                  # plugins
DATA_DIR = os.path.join(_PLUGINS_ROOT, "data", "webtoon_manager")

DOWNLOAD_DEFAULT_DIR = os.path.join(DATA_DIR, "downloads")
# 압축 전 낱장 이미지를 내려받는 임시 작업 폴더(기본값). 실제 웹툰 폴더
# (DOWNLOAD_ROOT, 원격/rclone 마운트일 수 있음)와 분리해서, 압축 전 미완성
# 상태의 파일들이 BookOasis 스캐너가 보는 실제 라이브러리 경로에 절대 노출되지
# 않게 한다. 플러그인 데이터 폴더 밑이라 항상 로컬 디스크에 있다고 가정한다.
TMP_DOWNLOAD_DEFAULT_DIR = os.path.join(DATA_DIR, "tmp_downloads")
TITLES_PATH = os.path.join(DATA_DIR, "titles.json")
AUTHORS_TAGS_PATH = os.path.join(DATA_DIR, "authors_tags.json")
HISTORY_PATH = os.path.join(DATA_DIR, "history.jsonl")
JOB_STATE_PATH = os.path.join(DATA_DIR, "job_state.json")
# 개별 작품 다운로드("지금 다운로드"/수동 다운로드)는 스캔/전체실행과는 독립된
# 락을 쓴다 - 둘 다 titles.json에 upsert_title()로 쓰지만, 그 함수는 모듈 전역
# _lock(threading.RLock)로 감싸여 있어 동시 호출돼도 파일 경합은 안전하다.
# 여기서 분리하는 건 오직 "이미 실행 중인 작업이 있습니다" 라는 상호 배제
# 안내만 큰 작업(스캔/전체실행)과 작품 단위 다운로드가 서로 막지 않게 하기 위함.
TITLE_JOB_STATE_PATH = os.path.join(DATA_DIR, "title_job_state.json")
JOB_LOG_PATH = os.path.join(DATA_DIR, "job.log")
SCHED_LOCK_PATH = os.path.join(DATA_DIR, "scheduler.lock")
# GitHub 원격 VERSION 조회 결과 캐시. Redis(self.cache_get/set)가 없는 배포에서도
# 매 대시보드 폴링(2.5~10초 간격)마다 GitHub에 요청을 보내지 않도록, Redis와
# 무관하게 항상 동작하는 파일 기반 캐시를 별도로 둔다.
UPDATE_CHECK_PATH = os.path.join(DATA_DIR, "update_check.json")

_lock = threading.RLock()

MAX_HISTORY_LINES = 2000
MAX_LOG_LINES = 500


def ensure_dirs():
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(DOWNLOAD_DEFAULT_DIR, exist_ok=True)
    os.makedirs(TMP_DOWNLOAD_DEFAULT_DIR, exist_ok=True)


def _atomic_write(path, text):
    ensure_dirs()
    fd, tmp_path = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".tmp_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp_path, path)
    finally:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass


def read_json(path, default):
    with _lock:
        if not os.path.exists(path):
            return default
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return default


# 작품 목록처럼 큰 파일은 들여쓰기 없이 저장한다. 카카오 작품이 수천 개라
# indent=2로 쓰면 파일이 크고 쓰기마다 CPU를 많이 먹는다(읽기는 둘 다 json.load로 동일).
_COMPACT_PATHS = set()


def write_json(path, data):
    with _lock:
        if path in _COMPACT_PATHS:
            text = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        else:
            text = json.dumps(data, ensure_ascii=False, indent=2)
        _atomic_write(path, text)


def titles_rev():
    """작품 목록 파일들의 변경 표식(수정 시각+크기). 화면은 이 값이 바뀌었을 때만
    전체 목록을 다시 받는다."""
    parts = []
    for p in (TITLES_PATH, KAKAO_TITLES_PATH, AUTHORS_TAGS_PATH):
        try:
            st = os.stat(p)
            parts.append("%d:%d" % (st.st_mtime_ns, st.st_size))
        except OSError:
            parts.append("0")
        except NameError:
            parts.append("0")
    return "|".join(parts)


# ---- 작품 목록(titles.json / kakao_titles.json) 메모리 캐시 + 묶음 저장 ----------
# 작품 목록 파일은 수 MB라, 예전처럼 회차 하나 받을 때마다 파일 전체를 다시 읽고
# (json 파싱) 다시 쓰면(직렬화+디스크 쓰기) 다운로드 중 CPU 대부분을 여기서 썼다.
#  - 읽기: 파일 서명(mtime+크기)이 그대로면 메모리에 있는 걸 쓴다.
#  - 쓰기: 바뀐 내용(patch)을 모아 두었다가 FLUSH_DELAY초마다 한 번만 파일에 쓴다.
#    같은 프로세스 안의 읽기는 즉시 새 값이 보인다. 다른 곳에서 파일이 바뀌었으면
#    파일을 다시 읽은 뒤 모아 둔 patch만 덮어써서 서로의 변경을 잃지 않는다.
# 모듈이 다시 로드돼도 캐시/대기 중인 변경이 사라지지 않게 sys 모듈에 붙여 둔다.
import sys as _sys

TITLES_FLUSH_DELAY = 5.0
_shared = getattr(_sys, "_wtm_titles_shared", None)
if _shared is None:
    _shared = {"cache": {}, "pending": {}, "timer": None, "lock": threading.RLock()}
    _sys._wtm_titles_shared = _shared


def _file_sig(path):
    try:
        st = os.stat(path)
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def _apply_pending(data, pend):
    for k, patch in pend.items():
        if patch is None:
            data.pop(k, None)
        else:
            cur = data.get(k)
            cur = dict(cur) if isinstance(cur, dict) else {}
            cur.update(patch)
            data[k] = cur


def _titles_data(path):
    """캐시된 원본 dict(수정 금지 - 내부용)."""
    with _shared["lock"]:
        sig = _file_sig(path)
        ent = _shared["cache"].get(path)
        if ent is None or ent[0] != sig:
            data = read_json(path, {})
            if not isinstance(data, dict):
                data = {}
            pend = _shared["pending"].get(path)
            if pend:
                _apply_pending(data, pend)
            ent = [sig, data]
            _shared["cache"][path] = ent
        return ent[1]


def _titles_load(path):
    # 바깥 dict와 작품별 dict는 복사해서 준다(호출 측이 고쳐도 캐시가 안 망가지게).
    with _shared["lock"]:
        return {k: (dict(v) if isinstance(v, dict) else v) for k, v in _titles_data(path).items()}


def _titles_get(path, key):
    with _shared["lock"]:
        v = _titles_data(path).get(str(key))
        return dict(v) if isinstance(v, dict) else v


def _titles_patch(path, patches):
    """patches: {id: patch dict | None(삭제)}"""
    with _shared["lock"]:
        data = _titles_data(path)
        pend = _shared["pending"].setdefault(path, {})
        for k, patch in patches.items():
            k = str(k)
            if patch is None:
                data.pop(k, None)
                pend[k] = None
            else:
                cur = dict(data.get(k) or {})
                cur.update(patch)
                data[k] = cur
                if pend.get(k) is None and k in pend:
                    pend[k] = dict(cur)      # 삭제 후 다시 추가 -> 전체 값으로
                else:
                    pend.setdefault(k, {}).update(patch)
        _schedule_flush()


def _schedule_flush():
    if _shared["timer"] is not None:
        return
    t = threading.Timer(TITLES_FLUSH_DELAY, flush_titles)
    t.daemon = True
    _shared["timer"] = t
    t.start()


def flush_titles():
    """모아 둔 작품 목록 변경을 지금 파일에 쓴다(작업 끝/종료 시에도 호출)."""
    with _shared["lock"]:
        _shared["timer"] = None
        for path, pend in list(_shared["pending"].items()):
            if not pend:
                continue
            ent = _shared["cache"].get(path)
            if ent is None or ent[0] != _file_sig(path):
                # 다른 곳에서 파일이 바뀜 -> 새로 읽고 내 변경만 다시 얹는다
                data = read_json(path, {})
                if not isinstance(data, dict):
                    data = {}
                _apply_pending(data, pend)
            else:
                data = ent[1]
            try:
                write_json(path, data)
            except Exception:  # noqa: BLE001
                _schedule_flush()   # 디스크 오류 등 - 다음에 다시 시도
                continue
            _shared["cache"][path] = [_file_sig(path), data]
            _shared["pending"][path] = {}


import atexit as _atexit
_atexit.register(flush_titles)


# ---- titles.json : { titleId(str): {...} } -----------------------------
def load_titles():
    return _titles_load(TITLES_PATH)


def get_title(title_id):
    return _titles_get(TITLES_PATH, title_id)


def has_title(title_id):
    with _shared["lock"]:
        return str(title_id) in _titles_data(TITLES_PATH)


def save_titles(titles):
    """목록 전체 저장(드묾). 지금 바로 쓴다."""
    with _shared["lock"]:
        flush_titles()
        data = {str(k): v for k, v in titles.items()}
        write_json(TITLES_PATH, data)
        _shared["cache"][TITLES_PATH] = [_file_sig(TITLES_PATH), data]


def upsert_title(patch_by_id):
    """patch_by_id: {titleId: {field: value, ...}} - 기존 값에 병합"""
    _titles_patch(TITLES_PATH, patch_by_id)
    return load_titles()


# ---- kakao_titles.json : 카카오페이지 작품 { series_id(str): {...} } ------------
# 네이버 titles.json과 섞지 않는다(ID 체계가 다르고, 네이버 목록 화면/스캔
# 로직이 titles.json 전체를 네이버 작품으로 가정하기 때문).
KAKAO_TITLES_PATH = os.path.join(DATA_DIR, "kakao_titles.json")
KAKAO_DOWNLOAD_DEFAULT_DIR = os.path.join(DATA_DIR, "kakao_downloads")
_COMPACT_PATHS.update({TITLES_PATH, KAKAO_TITLES_PATH})


def load_kakao_titles():
    return _titles_load(KAKAO_TITLES_PATH)


def get_kakao_title(series_id):
    return _titles_get(KAKAO_TITLES_PATH, series_id)


def has_kakao_title(series_id):
    with _shared["lock"]:
        return str(series_id) in _titles_data(KAKAO_TITLES_PATH)


def upsert_kakao_title(patch_by_id):
    _titles_patch(KAKAO_TITLES_PATH, patch_by_id)
    return None


def remove_kakao_title(series_id):
    with _shared["lock"]:
        removed = _titles_get(KAKAO_TITLES_PATH, series_id)
        _titles_patch(KAKAO_TITLES_PATH, {str(series_id): None})
        flush_titles()
    return removed


# ---- authors_tags.json ---------------------------------------------------
def load_authors_tags():
    return read_json(AUTHORS_TAGS_PATH, {"authors": [], "tags": []})


def save_authors_tags(data):
    write_json(AUTHORS_TAGS_PATH, data)


# ---- history.jsonl (append-only, capped) ---------------------------------
def _append_capped(path, line, max_lines, trim_bytes):
    """파일 끝에 한 줄만 덧붙인다(예전처럼 매번 전체를 읽고 다시 쓰지 않음).
    파일이 trim_bytes를 넘으면 그때만 마지막 max_lines줄로 줄인다."""
    ensure_dirs()
    with open(path, "a", encoding="utf-8") as f:
        f.write(line)
    try:
        if os.path.getsize(path) > trim_bytes:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
            _atomic_write(path, "".join(lines[-max_lines:]))
    except OSError:
        pass


def append_history(entry):
    with _lock:
        entry = dict(entry)
        entry.setdefault("ts", time.time())
        _append_capped(HISTORY_PATH, json.dumps(entry, ensure_ascii=False) + "\n",
                       MAX_HISTORY_LINES, 1024 * 1024)


_hist_cache = {}


def load_history(limit=200, query=None):
    # 화면 폴링마다 이력 파일 전체를 다시 읽지 않도록, 파일이 그대로면 이전 결과를 쓴다.
    sig = _file_sig(HISTORY_PATH)
    if sig is None:
        return []
    ck = (sig, limit, query)
    hit = _hist_cache.get("k")
    if hit == ck:
        return list(_hist_cache["v"])
    out = _load_history_raw(limit, query)
    _hist_cache["k"], _hist_cache["v"] = ck, out
    return list(out)


_rate_cache = {}


def download_rate(window=3600):
    """최근 window초 동안 받은 회차 수 {"total", "naver", "kakao"} (이력 파일 기준).
    이력 파일이 바뀌었을 때만 다시 센다."""
    sig = _file_sig(HISTORY_PATH)
    if sig is None:
        return {"total": 0, "naver": 0, "kakao": 0}
    now = time.time()
    hit = _rate_cache.get("v")
    if hit and _rate_cache.get("k") == (sig, window) and now - _rate_cache.get("at", 0) < 60:
        return dict(hit)
    out = {"total": 0, "naver": 0, "kakao": 0}
    cut = now - window
    try:
        with open(HISTORY_PATH, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        lines = []
    for line in reversed(lines):
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if float(e.get("ts") or 0) < cut:
            break
        if e.get("type") != "download":
            continue
        out["total"] += 1
        if str(e.get("platform") or "").startswith("kakao"):
            out["kakao"] += 1
        else:
            out["naver"] += 1
    _rate_cache.update({"k": (sig, window), "v": out, "at": now})
    return dict(out)


def _load_history_raw(limit=200, query=None):
    if not os.path.exists(HISTORY_PATH):
        return []
    with open(HISTORY_PATH, "r", encoding="utf-8") as f:
        lines = f.readlines()
    out = []
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if query and query not in json.dumps(entry, ensure_ascii=False):
            continue
        out.append(entry)
        if len(out) >= limit:
            break
    return out


# ---- job_state.json (스캔/다운로드 잡 진행상태) ---------------------------
DEFAULT_JOB_STATE = {
    "running": False,
    "pid": None,
    "stage": "idle",  # idle | scanning | downloading | notifying | done | error
    "message": "",
    "progress": 0,
    "total": 0,
    "started_at": None,
    "finished_at": None,
    "last_scan_at": None,
    "last_finished_scan_at": None,
    "last_kavita_all_at": None,
    "last_error": None,
}


def load_job_state():
    st = read_json(JOB_STATE_PATH, dict(DEFAULT_JOB_STATE))
    for k, v in DEFAULT_JOB_STATE.items():
        st.setdefault(k, v)
    return st


def save_job_state(patch):
    if patch.get("running") is False:
        flush_titles()      # 작업이 끝나면 모아 둔 작품 정보 변경을 바로 저장
    with _lock:
        st = load_job_state()
        st.update(patch)
        write_json(JOB_STATE_PATH, st)
    return st


def try_acquire_job(patch=None):
    """running이 False일 때만 원자적으로 True로 바꾸고 성공 여부를 bool로
    반환한다. "읽어서 running 확인 -> 그 다음에 running=True 저장" 을 두 번의
    호출로 나누면(예: 이전의 _act_run_bg) 그 사이에 다른 요청이 끼어들어
    작업이 이중으로 시작될 수 있다(TOCTOU 레이스). 이 함수는 확인과 저장을
    _lock 안에서 한 번에 처리해 그 틈을 없앤다."""
    with _lock:
        st = load_job_state()
        if st.get("running"):
            return False
        st.update(patch or {})
        st["running"] = True
        write_json(JOB_STATE_PATH, st)
        return True


# ---- title_job_state.json (개별 작품 다운로드 잡 진행상태, 스캔/전체실행과 독립) ---
DEFAULT_TITLE_JOB_STATE = {
    "running": False,
    "title_id": None,
    "title": None,
    "message": "",
    "progress": 0,
    "total": 0,
    "started_at": None,
    "finished_at": None,
    "cancel_requested": False,
    "last_error": None,
}


def load_title_job_state():
    st = read_json(TITLE_JOB_STATE_PATH, dict(DEFAULT_TITLE_JOB_STATE))
    for k, v in DEFAULT_TITLE_JOB_STATE.items():
        st.setdefault(k, v)
    return st


TITLE_JOB_STALE_SECONDS = 15 * 60


def save_title_job_state(patch):
    if patch.get("running") is False:
        flush_titles()
    with _lock:
        st = load_title_job_state()
        st.update(patch)
        st["updated_at"] = time.time()
        write_json(TITLE_JOB_STATE_PATH, st)
    return st


def try_acquire_title_job(patch=None):
    """try_acquire_job()과 동일한 이유로 존재하는, 개별 작품 다운로드
    (title_job_state) 전용 원자적 획득 헬퍼."""
    with _lock:
        st = load_title_job_state()
        if st.get("running"):
            # 컨테이너 재시작 등으로 스레드가 죽어 running=True로 굳은 상태면
            # (15분 넘게 진행 갱신이 없으면) 새 작업이 시작될 수 있게 풀어준다.
            last = st.get("updated_at") or st.get("started_at") or 0
            if time.time() - float(last) < TITLE_JOB_STALE_SECONDS:
                return False
        st.update(patch or {})
        st["running"] = True
        st["updated_at"] = time.time()
        write_json(TITLE_JOB_STATE_PATH, st)
        return True


# ---- kakao_dl_queue.json : 카카오 '다운로드' 버튼 대기열 -------------------
KAKAO_QUEUE_PATH = os.path.join(DATA_DIR, "kakao_dl_queue.json")


def kakao_queue_push(series_id):
    with _lock:
        q = read_json(KAKAO_QUEUE_PATH, [])
        if str(series_id) not in q:
            q.append(str(series_id))
        write_json(KAKAO_QUEUE_PATH, q)
        return len(q)


def kakao_queue_pop():
    with _lock:
        q = read_json(KAKAO_QUEUE_PATH, [])
        if not q:
            return None
        sid = q.pop(0)
        write_json(KAKAO_QUEUE_PATH, q)
        return sid


def kakao_queue_list():
    return read_json(KAKAO_QUEUE_PATH, [])


def append_log(line):
    with _lock:
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        _append_capped(JOB_LOG_PATH, "[%s] %s\n" % (ts, line), MAX_LOG_LINES, 256 * 1024)


# ---- update_check.json (GitHub 원격 버전 확인 결과 캐시) -------------------
DEFAULT_UPDATE_CHECK = {
    "checked_at": None,
    "local_version": None,
    "latest_version": None,
    "update_available": False,
    "error": None,
}


def load_update_check():
    st = read_json(UPDATE_CHECK_PATH, dict(DEFAULT_UPDATE_CHECK))
    for k, v in DEFAULT_UPDATE_CHECK.items():
        st.setdefault(k, v)
    return st


def save_update_check(patch):
    with _lock:
        st = load_update_check()
        st.update(patch)
        write_json(UPDATE_CHECK_PATH, st)
    return st


def tail_log(n=60):
    if not os.path.exists(JOB_LOG_PATH):
        return []
    with open(JOB_LOG_PATH, "r", encoding="utf-8") as f:
        lines = f.readlines()
    return [l.rstrip("\n") for l in lines[-n:]]
