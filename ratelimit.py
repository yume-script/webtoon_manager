# -*- coding: utf-8 -*-
"""
자동 속도 조절(백오프)
----------------------
동시에 받는 작품/이미지 수를 늘리면 빨라지지만, 서버가 "요청이 너무 많다"(HTTP 429)나
일시 장애(503)를 돌려주기 시작하면 계속 밀어붙이는 순간 차단될 수 있다.

같은 플랫폼(네이버/카카오)의 모든 세션·스레드가 하나의 '쉬는 시간'을 공유한다:
- 429/503 또는 연결 끊김이 오면 그 플랫폼 전체가 잠깐 쉰다(2초부터 두 배씩, 최대 2분).
- 정상 응답이 계속되면 단계를 다시 낮춘다.
그래서 고속 모드로 설정해도 서버가 버거워하면 자동으로 느려졌다가 다시 빨라진다.
"""
import threading
import time

import requests

_THROTTLE_STATUS = (429, 503)
_MAX_LEVEL = 6          # 2,4,8,16,32,64초 (+ 최대 120초)
_RECOVER_AFTER = 30     # 정상 응답 이만큼 연속이면 한 단계 낮춤

_state = {}
_lock = threading.Lock()


def _st(key):
    with _lock:
        return _state.setdefault(key, {"until": 0.0, "level": 0, "ok": 0, "hits": 0,
                                       "last_hit": 0.0})


def status():
    """화면/로그 표시용: {플랫폼: {"level", "hits", "cooling"(초)}}"""
    now = time.time()
    with _lock:
        return {k: {"level": v["level"], "hits": v["hits"],
                    "cooling": max(0, int(v["until"] - now))} for k, v in _state.items()}


def _wait(key):
    st = _st(key)
    while True:
        with _lock:
            remain = st["until"] - time.time()
        if remain <= 0:
            return
        time.sleep(min(remain, 5))


def _hit(key, reason, log=None):
    st = _st(key)
    with _lock:
        st["level"] = min(_MAX_LEVEL, st["level"] + 1)
        pause = min(120, 2 ** st["level"])
        st["until"] = max(st["until"], time.time() + pause)
        st["ok"] = 0
        st["hits"] += 1
        st["last_hit"] = time.time()
        lvl = st["level"]
    if log:
        log("[%s] 서버가 요청을 제한함(%s) - 전체 %d초 쉬고 다시 시도(속도 단계 %d)" % (key, reason, pause, lvl))


def _ok(key):
    st = _st(key)
    with _lock:
        if st["level"] > 0:
            st["ok"] += 1
            if st["ok"] >= _RECOVER_AFTER:
                st["level"] -= 1
                st["ok"] = 0


def install(session, key, log=None, retries=3):
    """session.request를 감싸 공유 백오프를 적용한다. 제한 응답이면 쉬었다가 재시도."""
    if getattr(session, "_wtm_rate_guard", None) == key:
        return session
    # requests 기본 연결 풀(호스트당 10개)보다 동시 요청이 많으면 연결을 버리고 새로 맺느라
    # 느려지므로 넉넉하게 잡는다.
    try:
        from requests.adapters import HTTPAdapter
        ad = HTTPAdapter(pool_connections=16, pool_maxsize=48)
        session.mount("https://", ad)
        session.mount("http://", ad)
    except Exception:  # noqa: BLE001
        pass
    orig = session.request

    def _request(method, url, *a, **kw):
        last_exc = None
        for attempt in range(retries + 1):
            _wait(key)
            try:
                r = orig(method, url, *a, **kw)
            except (requests.ConnectionError, requests.Timeout) as e:
                last_exc = e
                if attempt >= retries:
                    raise
                time.sleep(1 + attempt)      # 이 요청만 잠깐 쉬고 재시도(전체는 안 멈춤)
                continue
            if r.status_code in _THROTTLE_STATUS and attempt < retries:
                _hit(key, "HTTP %d" % r.status_code, log=log)
                continue
            _ok(key)
            return r
        if last_exc:
            raise last_exc
        return r

    session.request = _request
    session._wtm_rate_guard = key
    return session
