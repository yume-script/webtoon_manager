# -*- coding: utf-8 -*-
"""
중복 확인(이미 갖고 있는 작품) - 화면 표시와 다운로드 건너뛰기가 함께 쓴다
--------------------------------------------------------------------------
비교 기준
- 폴더: 공통 / 네이버웹툰 / 카카오웹툰 / 카카오웹소설별로 여러 개(한 줄에 하나).
  각 폴더의 하위 폴더명과 압축파일명(회차 꼬리표 제거)을 시리즈명으로 본다.
- BookOasis 라이브러리: 설정에서 고른 라이브러리의 시리즈명(모든 플랫폼과 비교).

이름은 괄호 안 내용("(완결)", "(12345)")과 공백을 지우고 소문자로 비교한다.
그래서 이 플러그인이 만든 "제목 (작품번호)" 폴더도 같은 작품으로 잡힌다 -
다운로드 건너뛰기는 이 점을 고려해 "이 플러그인이 이미 받기 시작한 작품"은
건너뛰지 않는다(should_skip_owned 참고).
"""
import os
import re
import threading
import time

_BRACKET_RE = re.compile(r'[\(\[（【].*?[\)\]）】]')
_WS_RE = re.compile(r'\s+')
# 파일명 끝의 "0012화#110", "05권", "12화" 같은 권/화 꼬리표
_EPISODE_SUFFIX_RE = re.compile(r'\s*\d+\s*(화|권|話|卷)(\s*#\s*\d+)?\s*$')

FOLDER_CACHE = {}          # {폴더: (스캔 시각, 이름 집합)}
_CACHE_TTL = 60
_lock = threading.Lock()

# (설정 키, 적용 대상, 표시 이름)
FOLDER_KEYS = (("COMPARE_FOLDER", "all", "공통"),
               ("COMPARE_FOLDERS_NAVER", "naver", "네이버웹툰"),
               ("COMPARE_FOLDERS_KAKAO", "kakao", "카카오웹툰"),
               ("COMPARE_FOLDERS_NOVEL", "novel", "카카오웹소설"))

# 다운로드 건너뛰기 설정 키(대상별). 켜져 있으면 보유 작품도 받는다.
DOWNLOAD_OWNED_KEYS = {"naver": "NAVER_DOWNLOAD_OWNED",
                       "kakao": "KAKAO_DOWNLOAD_OWNED",
                       "novel": "KAKAO_NOVEL_DOWNLOAD_OWNED"}

# BookOasis 라이브러리 시리즈명 조회 함수(DB 접근이 필요해 플러그인 클래스가 등록)
_library_source = None


def set_library_source(func):
    """func(cfg) -> (set|None, error|None)"""
    global _library_source
    _library_source = func


def normalize(name):
    if not name:
        return ""
    n = _BRACKET_RE.sub('', str(name))
    n = _WS_RE.sub('', n)
    return n.strip().lower()


def split_folders(raw):
    """여러 줄(또는 ';' 구분)로 넣은 폴더 목록 -> [경로]. 중복/빈 줄 제거."""
    out = []
    for line in str(raw or "").replace("\r", "\n").replace(";", "\n").split("\n"):
        p = line.strip().strip('"').strip()
        if p and p not in out:
            out.append(p)
    return out


def scan_folder(folder):
    """폴더 하나의 시리즈명 집합(60초 캐시). 반환: (set|None, error|None)"""
    if not os.path.isdir(folder):
        return None, "폴더를 찾을 수 없음: %s" % folder
    now = time.time()
    with _lock:
        cached = FOLDER_CACHE.get(folder)
    if cached and (now - cached[0]) < _CACHE_TTL:
        return cached[1], None
    try:
        names = set()
        with os.scandir(folder) as it:
            for entry in it:
                if entry.name.startswith("."):
                    continue
                if entry.is_dir():
                    names.add(normalize(entry.name))
                elif entry.name.lower().endswith((".zip", ".cbz", ".epub", ".pdf")):
                    stem = os.path.splitext(entry.name)[0]
                    stem = _EPISODE_SUFFIX_RE.sub("", stem)
                    names.add(normalize(stem))
        names.discard("")
        with _lock:
            FOLDER_CACHE[folder] = (now, names)
        return names, None
    except Exception as e:  # noqa: BLE001
        return None, "폴더 읽기 실패: %s (%s)" % (folder, e)


def build(cfg, with_library=True):
    """중복 확인 기준을 만든다.
    반환: (compare dict|None, status dict)
      compare = {"all"|"naver"|"kakao"|"novel": {정규화 이름: [출처, ...]}, "_sig": ...}
    아무것도 설정 안 됐으면 None('모름' - 뱃지도, 건너뛰기도 하지 않음)."""
    status = {"enabled": False, "sources": [], "count": 0, "errors": [], "folders": []}
    compare = {"all": {}, "naver": {}, "kakao": {}, "novel": {}}
    any_source = False
    for key, scope, label in FOLDER_KEYS:
        folders = split_folders(cfg.get(key))
        for idx, folder in enumerate(folders, 1):
            names, err = scan_folder(folder)
            src = "%s 폴더%s" % (label, (" %d" % idx) if len(folders) > 1 else "")
            if err:
                status["errors"].append("%s: %s" % (src, err))
                status["folders"].append({"scope": label, "path": folder, "count": None, "error": err})
                continue
            any_source = True
            status["folders"].append({"scope": label, "path": folder, "count": len(names)})
            status["sources"].append("%s(%d개)" % (src, len(names)))
            tag = "%s: %s" % (src, folder)
            bucket = compare[scope]
            for n in names:
                bucket.setdefault(n, []).append(tag)

    lib_set = None
    if with_library and _library_source is not None:
        try:
            lib_set, lib_err = _library_source(cfg)
        except Exception as e:  # noqa: BLE001
            lib_set, lib_err = None, "라이브러리 조회 실패: %s" % e
        if lib_err:
            status["errors"].append(lib_err)
        if lib_set is not None:
            any_source = True
            tag = "라이브러리: %s" % (cfg.get("COMPARE_LIBRARY_NAME") or cfg.get("COMPARE_LIBRARY_ID"))
            for n in lib_set:
                compare["all"].setdefault(n, []).append(tag)
            status["sources"].append("라이브러리(%d개)" % len(lib_set))

    if not any_source:
        return None, status
    status["enabled"] = True
    status["count"] = len(set().union(*[set(v) for v in compare.values()]))
    with _lock:
        stamps = tuple((f["path"], f.get("count"), (FOLDER_CACHE.get(f["path"]) or (0,))[0])
                       for f in status["folders"])
    compare["_sig"] = repr((stamps, len(lib_set) if lib_set is not None else None))
    return compare, status


def scope_of(platform, t):
    if platform == "naver":
        return "naver"
    return "novel" if "소설" in str((t or {}).get("category") or "") else "kakao"


def hits(compare, name, scope):
    """작품 하나의 중복 출처 목록(자기 플랫폼 폴더 + 공통 폴더 + 라이브러리)."""
    if compare is None:
        return None
    n = normalize(name)
    return list(compare["all"].get(n, [])) + list((compare.get(scope) or {}).get(n, []))


def _truthy(v):
    return v if isinstance(v, bool) else str(v).lower() in ("1", "true", "on", "y", "yes")


def should_skip_owned(cfg, compare, t, platform):
    """자동 다운로드에서 이 작품을 '이미 갖고 있음'으로 건너뛸지.
    - 해당 플랫폼의 "보유 작품도 받기"가 켜져 있으면 건너뛰지 않음
    - 이 플러그인이 이미 한 회차라도 받은 작품은 건너뛰지 않음(자기 다운로드 폴더를
      중복 확인 폴더로 지정해도 계속 이어받게)
    반환: 건너뛸 이유(출처 목록) 또는 None"""
    if compare is None:
        return None
    scope = scope_of(platform, t)
    if _truthy(cfg.get(DOWNLOAD_OWNED_KEYS[scope], False)):
        return None
    if t.get("last_downloaded_no") is not None:
        return None
    h = hits(compare, t.get("title", ""), scope)
    return h or None
