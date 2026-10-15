# -*- coding: utf-8 -*-
"""
별도 작업 프로세스(워커)
------------------------
다운로드/스캔 같은 무거운 작업을 BookOasis 프로세스의 스레드가 아니라 별도 파이썬
프로세스에서 돌린다. BookOasis와 파이썬 전역 잠금(GIL)을 나눠 쓰지 않으므로 다운로드가
몰려도 BookOasis 화면이 느려지지 않고, 프로세스 전체를 낮은 CPU 우선순위(nice 19)로 돌린다.

구성
- 플러그인(BookOasis 안): 화면/설정/가벼운 버튼은 지금처럼 직접 처리한다. 무거운 작업
  버튼은 로컬 유닉스 소켓으로 워커에 넘기고, 워커의 실제 결과를 그대로 화면에 돌려준다.
  폴링할 때마다 워커가 살아 있는지 보고 없으면 띄운다.
- 워커(worker_main.py로 시작): 같은 플러그인 코드를 BookOasis 없이 불러와(기반 클래스만
  대역으로 바꿔 끼움) 스케줄러, 카카오 다운로드 대기열, 무거운 작업을 맡는다.
- 상태/작품 목록/로그는 원래부터 파일이라 그대로 공유된다. 설정과 BookOasis 라이브러리
  시리즈 목록(중복 확인용)은 워커가 DB를 못 보므로 플러그인이 파일로 넘겨준다.

워커에 연결할 수 없으면 무거운 작업도 예전처럼 BookOasis 안에서 돌린다(안전장치).
"""
import hashlib
import json
import os
import socket
import subprocess
import sys
import threading
import time

from . import state_store as ss

IN_WORKER = os.environ.get("WTM_WORKER") == "1"
PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
CFG_PATH = os.path.join(ss.DATA_DIR, "worker_cfg.json")
LIB_CACHE_PATH = os.path.join(ss.DATA_DIR, "compare_library_cache.json")
STATE_PATH = os.path.join(ss.DATA_DIR, "worker_state.json")
LOCK_PATH = os.path.join(ss.DATA_DIR, "worker.lock")
OUT_PATH = os.path.join(ss.DATA_DIR, "worker.out")
# 통신: 127.0.0.1의 임시 TCP 포트 + 무작위 토큰(리눅스/윈도우 공통). 작업 프로세스가 뜨면서
# 포트와 토큰을 이 파일에 적고, 플러그인은 이 파일을 읽어 접속한다. 토큰이 맞아야 요청을 받는다.
# (1.39.2까지는 유닉스 소켓/fcntl을 써서 윈도우 네이티브 BookOasis에서는 동작하지 않았음)
PORT_PATH = os.path.join(ss.DATA_DIR, "worker_port.json")
IS_WINDOWS = os.name == "nt"

# 워커로 넘기는 작업(백그라운드 스레드를 띄우는 버튼들)
WORKER_ACTIONS = {
    "scan_now", "scan_finished_now", "run_full_cycle_now", "kavita_yaml_all",
    "kakao_scan", "kakao_sync_purchased", "kakao_run_all", "kakao_download",
    "manual_download", "download_title",
}

SPAWN_INTERVAL = 15          # 워커가 안 뜰 때 다시 띄우기 시도 간격(초)
IDLE_EXIT_WAIT = 30 * 60     # 끄기/업데이트 감지 후 작업이 끝나길 기다리는 최대 시간


def _truthy(v, default=True):
    if v is None or v == "":
        return default
    return v if isinstance(v, bool) else str(v).lower() in ("1", "true", "on", "y", "yes")


def enabled(cfg):
    """설정 '별도 작업 프로세스에서 실행'(기본 켜짐)."""
    return _truthy((cfg or {}).get("WORKER_MODE"), True)


def scheduler_allowed(cfg):
    """이 프로세스가 스케줄러를 돌려도 되는지. 워커 모드면 워커만, 아니면 BookOasis만."""
    return enabled(cfg) if IN_WORKER else not enabled(cfg)


# ---------------------------------------------------------------------------
# 플러그인 쪽: 설정/라이브러리 목록 넘기기, 워커 띄우기, 요청 보내기
# ---------------------------------------------------------------------------
# 모듈이 요청마다 다시 로드돼도 '마지막으로 띄운 시각/띄운 프로세스'를 잃지 않게 sys에 붙여 둔다
# (잃으면 요청마다 작업 프로세스를 새로 띄우려 할 수 있음)
_shared = getattr(sys, "_wtm_worker_shared", None)
if _shared is None:
    _shared = {"last_written": {}, "procs": [], "spawn_lock": threading.Lock(), "last_spawn": {"t": 0.0}}
    sys._wtm_worker_shared = _shared
_last_written = _shared["last_written"]
_procs = _shared["procs"]
_spawn_lock = _shared["spawn_lock"]
_last_spawn = _shared["last_spawn"]


def _write_if_changed(path, data, private=False):
    text = json.dumps(data, ensure_ascii=False, sort_keys=True)
    h = hashlib.sha1(text.encode("utf-8")).hexdigest()
    if _last_written.get(path) == h and os.path.exists(path):
        return
    ss.ensure_dirs()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    if private:
        try:
            os.chmod(tmp, 0o600)     # 쿠키가 들어 있음
        except OSError:
            pass
    os.replace(tmp, path)
    _last_written[path] = h


def write_cfg(raw_cfg):
    _write_if_changed(CFG_PATH, raw_cfg or {}, private=True)


def read_cfg():
    return ss.read_json(CFG_PATH, {})


def write_library_cache(library_id, names):
    _write_if_changed(LIB_CACHE_PATH, {"library_id": str(library_id or ""), "names": sorted(names or [])})


def read_library_set(cfg):
    """워커용 라이브러리 시리즈명 집합(플러그인이 넘겨준 파일)."""
    lid = str((cfg or {}).get("COMPARE_LIBRARY_ID") or "")
    if not lid:
        return None, None
    d = ss.read_json(LIB_CACHE_PATH, {})
    if str(d.get("library_id") or "") != lid:
        return None, "라이브러리 목록을 아직 받지 못함(웹툰 다운로더 화면을 한 번 열면 전달됨)"
    return set(d.get("names") or []), None


def _endpoint():
    d = ss.read_json(PORT_PATH, {})
    try:
        return int(d.get("port") or 0), str(d.get("token") or "")
    except (TypeError, ValueError):
        return 0, ""


def _send(msg, timeout=5.0):
    port, token = _endpoint()
    if not port:
        raise OSError("작업 프로세스 주소 없음")
    msg = dict(msg)
    msg["token"] = token
    s = socket.create_connection(("127.0.0.1", port), timeout=timeout)
    try:
        s.settimeout(timeout)
        s.sendall((json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8"))
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
        return json.loads(buf.decode("utf-8")) if buf else None
    finally:
        s.close()


def status(timeout=1.0):
    try:
        r = _send({"op": "status"}, timeout=timeout)
        return r if r and r.get("ok") else None
    except (OSError, ValueError):
        return None


def reap():
    """끝난 작업 프로세스를 정리한다(좀비로 남으면 PID가 살아 있는 것처럼 보임)."""
    _reap()


def _reap():
    for p in list(_procs):
        if p.poll() is not None:
            _procs.remove(p)


def _python_exe():
    """작업 프로세스를 띄울 파이썬. 윈도우에서 BookOasis가 pythonw.exe(창 없는 파이썬)나
    서비스 래퍼로 돌고 있으면 같은 폴더의 python.exe를 쓴다."""
    exe = sys.executable or "python"
    if IS_WINDOWS:
        base = os.path.basename(exe).lower()
        if base == "pythonw.exe":
            cand = os.path.join(os.path.dirname(exe), "python.exe")
            if os.path.exists(cand):
                return cand
    return exe


def _try_lock(f):
    """작업 프로세스가 하나만 뜨도록 잠금 파일을 잡는다(리눅스 fcntl / 윈도우 msvcrt)."""
    try:
        import fcntl
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False
    except ImportError:
        import msvcrt
        try:
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False


def _lower_process_priority(low):
    """프로세스 전체 CPU 우선순위 낮추기. 반환: 화면 표시용 값(리눅스 nice 숫자 / 윈도우 '낮음')."""
    if not low:
        return 0
    if IS_WINDOWS:
        try:
            import ctypes
            k32 = ctypes.windll.kernel32
            k32.SetPriorityClass(k32.GetCurrentProcess(), 0x00004000)   # BELOW_NORMAL
        except Exception:  # noqa: BLE001
            pass
        return "낮음"
    try:
        os.setpriority(os.PRIO_PROCESS, 0, 19)
    except (AttributeError, OSError):
        pass
    return 19


def spawn(log=None):
    """워커 프로세스를 띄운다(여러 BookOasis 프로세스가 동시에 불러도 워커는 하나만 남음 -
    워커가 시작하자마자 잠금 파일을 잡고, 못 잡으면 스스로 끝낸다)."""
    with _spawn_lock:
        _reap()
        now = time.time()
        if now - _last_spawn["t"] < SPAWN_INTERVAL:
            return False
        _last_spawn["t"] = now
        ss.ensure_dirs()
        try:
            if os.path.exists(OUT_PATH) and os.path.getsize(OUT_PATH) > 2 * 1024 * 1024:
                os.remove(OUT_PATH)
        except OSError:
            pass
        env = dict(os.environ)
        env["WTM_WORKER"] = "1"
        main = os.path.join(PLUGIN_DIR, "worker_main.py")
        # 스크립트 폴더가 sys.path 맨 앞에 붙지 않게 -c로 실행한다(플러그인 파일 이름이
        # 표준 모듈 이름을 가리지 않도록). 작업 폴더는 데이터 폴더.
        code = "import runpy,sys; runpy.run_path(sys.argv[1], run_name='__main__')"
        try:
            out = open(OUT_PATH, "ab")
            kw = {}
            if IS_WINDOWS:
                # 창 없이, BookOasis와 분리된 프로세스 그룹, 처음부터 낮은 우선순위로
                kw["creationflags"] = (0x08000000      # CREATE_NO_WINDOW
                                       | 0x00000200    # CREATE_NEW_PROCESS_GROUP
                                       | 0x00004000)   # BELOW_NORMAL_PRIORITY_CLASS
            else:
                kw["start_new_session"] = True
            p = subprocess.Popen([_python_exe(), "-u", "-c", code, main], env=env, cwd=ss.DATA_DIR,
                                 stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                                 close_fds=True, **kw)
            out.close()
            _procs.append(p)
            if log:
                log("별도 작업 프로세스 시작(PID %d)" % p.pid)
            return True
        except Exception as e:  # noqa: BLE001
            if log:
                log("별도 작업 프로세스를 시작하지 못함(BookOasis 안에서 계속 실행): %s" % e)
            return False


def ensure_running(raw_cfg, log=None):
    """설정을 넘기고, 워커가 없으면 띄운다. 반환: 워커 상태 dict 또는 None(아직 없음)."""
    write_cfg(raw_cfg)
    st = status(timeout=0.5)
    if st:
        return st
    spawn(log=log)
    return None


def forward(raw_cfg, db_type, action_id, context, log=None):
    """무거운 작업을 워커에 넘긴다. 워커의 실제 결과 dict를 돌려주고, 연결할 수 없으면 None
    (호출 측이 BookOasis 안에서 직접 실행)."""
    write_cfg(raw_cfg)
    for attempt in range(2):
        try:
            r = _send({"op": "action", "db_type": db_type, "action": action_id,
                       "context": context or {}}, timeout=30)
            if r and r.get("ok"):
                return r.get("result")
        except (OSError, ValueError):
            pass
        if attempt == 0:
            spawn(log=log)
            for _ in range(20):            # 새로 띄웠으면 소켓이 열릴 때까지 잠깐 기다림
                time.sleep(0.25)
                if status(timeout=0.3):
                    break
    return None


# ---------------------------------------------------------------------------
# 워커 쪽: 소켓 서버 + 관리 루프
# ---------------------------------------------------------------------------
def _version():
    try:
        with open(os.path.join(PLUGIN_DIR, "VERSION"), encoding="utf-8") as f:
            return (json.load(f) or {}).get("plugin version") or ""
    except Exception:  # noqa: BLE001
        return ""


def _jobs_running():
    if ss.load_job_state().get("running"):
        return True
    tj = ss.load_title_job_state()
    return bool(tj.get("running"))


def run_worker():
    import secrets
    import socketserver

    ss.ensure_dirs()
    lock = open(LOCK_PATH, "a+")
    if not _try_lock(lock):
        print("다른 작업 프로세스가 이미 실행 중 - 종료", flush=True)
        return

    cfg0 = read_cfg()
    # 스레드를 만들기 전에 낮춰야 이후 모든 스레드가 이 우선순위를 물려받는다
    nice = _lower_process_priority(_truthy(cfg0.get("LOW_PRIORITY_MODE"), True))

    # 이전 워커가 작업 도중에 끝났으면(업데이트/재시작) 실행 중 표시가 남아 있다 - 정리
    if ss.load_job_state().get("running"):
        ss.save_job_state({"running": False, "stage": "idle",
                            "message": "작업 프로세스가 다시 시작되어 이전 작업 표시를 정리했습니다"})
    if ss.load_title_job_state().get("running"):
        ss.save_title_job_state({"running": False, "message": "작업 프로세스가 다시 시작되어 중단됨"})

    from . import webtoon_manager as wm, compare, pipeline, scheduler, ratelimit
    provider = wm.WebtoonManagerMetadataProvider()
    compare.set_library_source(read_library_set)
    started_at = time.time()
    version = _version()

    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            try:
                line = self.rfile.readline(4 * 1024 * 1024)
                msg = json.loads(line.decode("utf-8")) if line else {}
                op = msg.get("op")
                if not secrets.compare_digest(str(msg.get("token") or ""), token):
                    out = {"ok": False, "error": "토큰 불일치"}
                elif op == "status":
                    out = {"ok": True, "pid": os.getpid(), "started_at": started_at, "nice": nice,
                           "version": version, "throttle": ratelimit.status()}
                elif op == "action":
                    res = provider.run_context_menu_action(msg.get("db_type") or "general",
                                                           msg.get("action"), msg.get("context") or {})
                    out = {"ok": True, "result": res}
                else:
                    out = {"ok": False, "error": "unknown op"}
            except Exception as e:  # noqa: BLE001
                out = {"ok": False, "error": str(e)}
            try:
                self.wfile.write((json.dumps(out, ensure_ascii=False) + "\n").encode("utf-8"))
            except OSError:
                pass

    class Server(socketserver.ThreadingMixIn, socketserver.TCPServer):
        daemon_threads = True
        allow_reuse_address = False

    token = secrets.token_hex(16)
    server = Server(("127.0.0.1", 0), Handler)      # 로컬 전용, 빈 포트 자동 선택
    port = server.server_address[1]
    tmp = PORT_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"port": port, "token": token, "pid": os.getpid()}, f)
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, PORT_PATH)
    threading.Thread(target=server.serve_forever, name="wtm_worker_socket", daemon=True).start()

    def _state(extra=None):
        d = {"pid": os.getpid(), "started_at": started_at, "heartbeat": time.time(),
             "nice": nice, "version": version}
        d.update(extra or {})
        try:
            ss.write_json(STATE_PATH, d)
        except Exception:  # noqa: BLE001
            pass

    ss.append_log("별도 작업 프로세스 실행 중(PID %d, CPU 우선순위 %s, 버전 %s)" % (os.getpid(), ("nice %s" % nice) if isinstance(nice, int) else nice, version))
    _state()
    try:
        from . import kakao_pipeline
        cfg = provider._get_cfg("general")
        if cfg.get("KAKAO_ENABLE"):
            kakao_pipeline.repair_old_records_async(cfg)
    except Exception:  # noqa: BLE001
        pass

    stop_since = None
    reason = ""
    while True:
        try:
            cfg = provider._get_cfg("general")
            if scheduler_allowed(cfg):
                scheduler.ensure_started(lambda: provider._get_cfg("general"),
                                          pipeline.run_full_cycle, pipeline.run_finished_scan_job)
                try:
                    provider._maybe_drain_kakao_queue(cfg)
                except Exception:  # noqa: BLE001
                    pass
            # 끄기 / 플러그인 업데이트(버전 변경) 감지 -> 하던 작업이 끝나면 종료
            if not enabled(cfg):
                reason = "설정에서 별도 작업 프로세스를 끔"
            elif _version() != version:
                reason = "플러그인 업데이트됨(%s -> %s)" % (version, _version())
            else:
                reason = ""
            if reason:
                stop_since = stop_since or time.time()
                if not _jobs_running() or time.time() - stop_since > IDLE_EXIT_WAIT:
                    ss.append_log("별도 작업 프로세스 종료: %s" % reason)
                    break
            else:
                stop_since = None
            _state({"stopping": reason or None})
        except Exception as e:  # noqa: BLE001
            try:
                ss.append_log("작업 프로세스 관리 루프 오류(계속): %s" % e)
            except Exception:  # noqa: BLE001
                pass
        time.sleep(5)

    try:
        ss.flush_titles()
    except Exception:  # noqa: BLE001
        pass
    # 스케줄러 잠금을 내 PID로 쥐고 있으면 풀어서 BookOasis 쪽이 바로 이어받게 한다
    try:
        with open(ss.SCHED_LOCK_PATH, encoding="utf-8") as f:
            if f.read().strip() == str(os.getpid()):
                os.remove(ss.SCHED_LOCK_PATH)
    except OSError:
        pass
    try:
        server.shutdown()
        server.server_close()
    except Exception:  # noqa: BLE001
        pass
    try:
        if ss.read_json(PORT_PATH, {}).get("pid") == os.getpid():
            os.remove(PORT_PATH)
    except OSError:
        pass
    try:
        os.remove(STATE_PATH)
    except OSError:
        pass
    os._exit(0)
