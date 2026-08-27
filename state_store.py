# -*- coding: utf-8 -*-
"""
webtoon_manager 상태 저장소
---------------------------
BookOasis 플러그인은 요청마다 모듈이 새로 로드될 수 있어 메모리에 값을
들고 있을 수 없다. 그래서 모든 상태(구독 목록/작가·태그/다운로드 이력/
잡 진행상태/입력값 복원용 임시값)를 plugins/data/webtoon_manager/ 아래
JSON 파일로 저장하고, 매 요청마다 파일을 열어 읽고 닫는다.
"""
import json
import os
import tempfile
import threading
import time

# plugins/metadata/webtoon_manager/core/state_store.py 기준으로
# plugins/data/webtoon_manager/ 를 데이터 폴더로 사용한다.
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PLUGIN_DIR = os.path.dirname(_THIS_DIR)          # plugins/metadata/webtoon_manager
_METADATA_DIR = os.path.dirname(_PLUGIN_DIR)       # plugins/metadata
_PLUGINS_ROOT = os.path.dirname(_METADATA_DIR)     # plugins
DATA_DIR = os.path.join(_PLUGINS_ROOT, "data", "webtoon_manager")
DOWNLOAD_DEFAULT_DIR = os.path.join(DATA_DIR, "downloads")

TITLES_PATH = os.path.join(DATA_DIR, "titles.json")
AUTHORS_TAGS_PATH = os.path.join(DATA_DIR, "authors_tags.json")
HISTORY_PATH = os.path.join(DATA_DIR, "history.jsonl")
JOB_STATE_PATH = os.path.join(DATA_DIR, "job_state.json")
JOB_LOG_PATH = os.path.join(DATA_DIR, "job.log")
SCHED_LOCK_PATH = os.path.join(DATA_DIR, "scheduler.lock")

_lock = threading.RLock()

MAX_HISTORY_LINES = 2000
MAX_LOG_LINES = 500


def ensure_dirs():
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(DOWNLOAD_DEFAULT_DIR, exist_ok=True)


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


def write_json(path, data):
    with _lock:
        _atomic_write(path, json.dumps(data, ensure_ascii=False, indent=2))


# ---- titles.json : { titleId(str): {...} } -----------------------------

def load_titles():
    return read_json(TITLES_PATH, {})


def save_titles(titles):
    save_titles_map = {str(k): v for k, v in titles.items()}
    write_json(TITLES_PATH, save_titles_map)


def upsert_title(patch_by_id):
    """patch_by_id: {titleId: {field: value, ...}} - 기존 값에 병합"""
    with _lock:
        titles = load_titles()
        for tid, patch in patch_by_id.items():
            tid = str(tid)
            cur = titles.get(tid, {})
            cur.update(patch)
            titles[tid] = cur
        save_titles(titles)
        return titles


# ---- authors_tags.json ---------------------------------------------------

def load_authors_tags():
    return read_json(AUTHORS_TAGS_PATH, {"authors": [], "tags": []})


def save_authors_tags(data):
    write_json(AUTHORS_TAGS_PATH, data)


# ---- history.jsonl (append-only, capped) ---------------------------------

def append_history(entry):
    with _lock:
        ensure_dirs()
        entry = dict(entry)
        entry.setdefault("ts", time.time())
        lines = []
        if os.path.exists(HISTORY_PATH):
            with open(HISTORY_PATH, "r", encoding="utf-8") as f:
                lines = f.readlines()
        lines.append(json.dumps(entry, ensure_ascii=False) + "\n")
        if len(lines) > MAX_HISTORY_LINES:
            lines = lines[-MAX_HISTORY_LINES:]
        _atomic_write(HISTORY_PATH, "".join(lines))


def load_history(limit=200, query=None):
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
    "stage": "idle",       # idle | scanning | downloading | notifying | done | error
    "message": "",
    "progress": 0,
    "total": 0,
    "started_at": None,
    "finished_at": None,
    "last_scan_at": None,
    "last_error": None,
}


def load_job_state():
    st = read_json(JOB_STATE_PATH, dict(DEFAULT_JOB_STATE))
    for k, v in DEFAULT_JOB_STATE.items():
        st.setdefault(k, v)
    return st


def save_job_state(patch):
    with _lock:
        st = load_job_state()
        st.update(patch)
        write_json(JOB_STATE_PATH, st)
        return st


def append_log(line):
    with _lock:
        ensure_dirs()
        lines = []
        if os.path.exists(JOB_LOG_PATH):
            with open(JOB_LOG_PATH, "r", encoding="utf-8") as f:
                lines = f.readlines()
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        lines.append("[%s] %s\n" % (ts, line))
        if len(lines) > MAX_LOG_LINES:
            lines = lines[-MAX_LOG_LINES:]
        _atomic_write(JOB_LOG_PATH, "".join(lines))


def tail_log(n=60):
    if not os.path.exists(JOB_LOG_PATH):
        return []
    with open(JOB_LOG_PATH, "r", encoding="utf-8") as f:
        lines = f.readlines()
    return [l.rstrip("\n") for l in lines[-n:]]
