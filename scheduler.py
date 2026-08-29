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
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
    except (OSError, ValueError, TypeError):
        return False
    return True


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
                    try:
                        run_full_cycle_func(cfg, log=_ss.append_log)
                    except Exception as e:  # noqa: BLE001
                        _ss.append_log("스케줄러 실행 오류: %s" % e)
                elif _is_finished_scan_due(job, finished_hour):
                    # 요일별 사이클이 지금 막 안 돌아도, 정해진 시각이 됐으면
                    # 완결 스캔은 독립적으로 실행한다.
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
