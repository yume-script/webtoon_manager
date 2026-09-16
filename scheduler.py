# -*- coding: utf-8 -*-
"""
BookOasis 플러그인 모듈은 요청마다 새로 로드될 수 있다. 그래서 매 요청(주로
get_dashboard_data 폴링)마다 "스케줄러 스레드가 이미 떠 있는지" 파일 기반 PID로
확인하고, 없으면 새로 하나 띄운다. 같은 프로세스 안에서는 모듈 전역(globals)이
재로드 전까지는 유지되므로 이중 가드(전역 플래그 + PID 파일)를 쓴다.
"""
import os
import threading
import time

from . import state_store as ss

_started_in_process = False
_lock = threading.Lock()


def _pid_alive(pid):
    """PID가 살아있는지 확인한다. 실제로 신호를 보내서는 안 된다.

    ⚠️ Windows에서 절대 os.kill(pid, 0)을 쓰지 말 것 (수정 이력 있음, 되돌리지 마세요)
    ------------------------------------------------------------------------
    유닉스에서 os.kill(pid, 0)은 "신호를 보내지 않고 존재/권한만 검사"하는
    관용적인 방법이지만, Windows에서는 의미가 완전히 다르다. CPython 문서상
    Windows의 os.kill()은 sig가 signal.CTRL_C_EVENT(값이 바로 0) 또는
    CTRL_BREAK_EVENT(1)일 때 "같은 콘솔 창을 공유하는 콘솔 프로세스들"에게
    실제로 그 콘솔 제어 이벤트를 보내고, 그 외의 값이면 TerminateProcess로
    대상을 무조건 죽인다.

    즉 Windows에서 os.kill(pid, 0)은 존재 확인이 아니라 'Ctrl+C 전송'이며,
    BookOasis 본체와 scanner worker가 같은 CMD 콘솔에 붙어 있는 배포에서는
    이 한 줄 때문에 서버 전체가 KeyboardInterrupt를 받고 죽으면서
    "Terminate batch job (Y/N)?"이 뜨는 문제가 실제로 보고됐다.
    (플러그인을 끄면 재현되지 않고 켜면 재현됨으로 원인 확인됨)

    그래서 Windows에서는 신호를 전혀 보내지 않는 OpenProcess()로만 PID
    존재 여부를 확인한다.
    """
    if not pid:
        return False
    try:
        pid = int(pid)
    except (ValueError, TypeError):
        return False
    if pid <= 0:
        return False

    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.windll.kernel32
            # restype을 명시하지 않으면 기본값이 c_int라서 64비트에서 핸들
            # 값이 잘려 오판할 수 있다. HANDLE로 정확히 지정한다.
            kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
            kernel32.OpenProcess.restype = wintypes.HANDLE
            kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
            kernel32.CloseHandle.restype = wintypes.BOOL

            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            ERROR_ACCESS_DENIED = 5
            STILL_ACTIVE = 259

            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not handle:
                # 권한 부족(다른 계정/서비스로 뜬 프로세스)이면 "없다"가 아니라
                # "있는데 못 들여다본다"는 뜻이다. 살아있다고 보수적으로 판단해야
                # 스케줄러가 중복으로 뜨지 않는다.
                # (ctypes.get_last_error()는 use_last_error=True로 선언한
                #  라이브러리에서만 유효하므로 kernel32.GetLastError()를 쓴다.)
                return kernel32.GetLastError() == ERROR_ACCESS_DENIED
            try:
                # 핸들이 열렸어도 이미 종료된 프로세스일 수 있으므로 종료코드까지
                # 확인한다(STILL_ACTIVE면 아직 실행 중).
                exit_code = wintypes.DWORD()
                if kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                    return exit_code.value == STILL_ACTIVE
                return True
            finally:
                kernel32.CloseHandle(handle)
        except Exception:  # noqa: BLE001
            # ctypes를 못 쓰는 환경 등 - 신호를 보내는 위험한 경로로는 절대
            # 폴백하지 않는다. "죽었다"고 보면 스케줄러가 하나 더 뜰 뿐이라
            # 무해하지만, Ctrl+C를 보내면 서버가 죽는다.
            return False

    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _write_lock():
    ss.ensure_dirs()
    with open(ss.SCHED_LOCK_PATH, "w", encoding="utf-8") as f:
        f.write(str(os.getpid()))


def _read_lock():
    if not os.path.exists(ss.SCHED_LOCK_PATH):
        return None
    try:
        with open(ss.SCHED_LOCK_PATH, "r", encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return None


def _is_finished_scan_due(job, target_hour):
    """완결 전체 스캔은 하루 중 target_hour 시(0~23) 이후 딱 한 번만 돈다.
    최초 설치 직후에도 즉시 돌지 않고 그 시각까지 기다린다 - 초기 설치 때
    무거운 완결 스캔이 요일별 스캔을 막지 않게 하려는 게 이 분리의 목적이라,
    "한 번도 안 돌았다"는 이유만으로 바로 실행하지는 않는다."""
    try:
        now = time.localtime()
        if now.tm_hour < int(target_hour):
            return False
        last_ts = job.get("last_finished_scan_at")
        if last_ts is None:
            return True
        last = time.localtime(last_ts)
        return (now.tm_year, now.tm_yday) != (last.tm_year, last.tm_yday)
    except Exception:  # noqa: BLE001
        return False


def _loop(get_cfg_func, run_full_cycle_func, run_finished_scan_func):
    from . import state_store as _ss
    while True:
        cfg = get_cfg_func() or {}
        enabled = str(cfg.get("ENABLE_SCHEDULER", "")).lower() in ("1", "true", "on", "y", "yes")
        interval_min = cfg.get("INTERVAL_MINUTES")
        try:
            interval_min = float(interval_min)
        except (TypeError, ValueError):
            interval_min = 240.0
        interval_min = max(10.0, interval_min)

        finished_hour = cfg.get("FINISHED_SCAN_HOUR")
        try:
            finished_hour = int(finished_hour)
        except (TypeError, ValueError):
            finished_hour = 4
        finished_hour = min(23, max(0, finished_hour))

        if enabled:
            job = _ss.load_job_state()
            if not job.get("running"):
                due = (job.get("last_scan_at") is None or
                       (time.time() - job.get("last_scan_at", 0)) >= interval_min * 60)
                if due:
                    # try_acquire_job()으로 확인+저장을 원자적으로 처리한다.
                    # 예전에는 위의 job.get("running") 확인과 실제 실행 사이에
                    # 짧은 틈이 있어서, 그 사이 사용자가 수동으로 "지금 전체
                    # 실행"을 눌러도 둘 다 실행을 시작해버리는 레이스가
                    # 있었다(반대로 스케줄러가 먼저 선점하면 수동 액션 쪽이
                    # "이미 실행 중" 응답을 받게 됨 - 어느 쪽이든 이제 하나만
                    # 실제로 시작된다).
                    if _ss.try_acquire_job({
                        "stage": "starting", "message": "스케줄러: 요일별 스캔+다운로드 시작",
                        "started_at": time.time(), "cancel_requested": False, "last_error": None,
                    }):
                        try:
                            run_full_cycle_func(cfg, log=_ss.append_log)
                        except Exception as e:  # noqa: BLE001
                            _ss.append_log("스케줄러 실행 오류: %s" % e)
                elif _is_finished_scan_due(job, finished_hour):
                    # 요일별 사이클이 지금 막 안 돌아도, 정해진 시각이 됐으면
                    # 완결 스캔은 독립적으로 실행한다.
                    if _ss.try_acquire_job({
                        "stage": "starting", "message": "스케줄러: 완결 목록 수집 시작",
                        "started_at": time.time(), "cancel_requested": False, "last_error": None,
                    }):
                        try:
                            run_finished_scan_func(cfg, log=_ss.append_log)
                        except Exception as e:  # noqa: BLE001
                            _ss.append_log("스케줄러(완결 스캔) 실행 오류: %s" % e)

        time.sleep(60)


def ensure_started(get_cfg_func, run_full_cycle_func, run_finished_scan_func):
    """이미 이 프로세스에서 시작했거나, 살아있는 PID 락이 있으면 아무것도 안 함."""
    global _started_in_process
    with _lock:
        if _started_in_process:
            return False
        existing_pid = _read_lock()
        if _pid_alive(existing_pid) and existing_pid != str(os.getpid()):
            # 다른 프로세스(워커)가 이미 스케줄러를 돌리고 있다고 간주
            _started_in_process = True
            return False
        _write_lock()
        t = threading.Thread(target=_loop, args=(get_cfg_func, run_full_cycle_func, run_finished_scan_func),
                              name="webtoon_manager_scheduler", daemon=True)
        t.start()
        _started_in_process = True
        return True
