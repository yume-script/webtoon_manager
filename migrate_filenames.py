# -*- coding: utf-8 -*-
"""
migrate_filenames.py
---------------------
webtoon_manager 플러그인이 그동안 여러 번 파일 저장 방식을 바꾸면서(낱장 폴더 →
숫자만 있는 .cbz → 제목 없는 "N화#장수.zip/cbz" → "제목 N화 장수.zip" → 지금의
"제목 00xx화#장수.zip") 다운로드 경로 안에 예전 형식이 섞여 남아있을 수 있습니다.
이 스크립트는 그런 예전 형식을 전부 지금 규칙으로 정리합니다.

사용법 (BookOasis 컨테이너 안에서 실행):

    docker exec -it bookoasis python3 \
        /app/plugins/metadata/webtoon_manager/migrate_filenames.py \
        "/mnt/zeeps_member/zeepsmember/naverwebtoon"

옵션:
    --dry-run   실제로 지우거나 이름을 바꾸지 않고 무엇을 할지만 출력합니다.
                처음엔 이 옵션으로 먼저 확인해보는 걸 권장합니다.

동작 방식 (회차 하나당):
    1) 이미 지금 규칙("제목 00xx화#장수.zip")과 정확히 일치하는 파일이 있으면
       그건 건드리지 않습니다.
    2) 그 회차의 다른 예전 형식(낱장 폴더 / 숫자만 있는 .cbz / 제목 없는
       "N화#장수.zip(.cbz)" / "제목 N화 장수.zip")을 찾아서:
       - 이미 지금 규칙 파일이 있으면: 예전 파일/폴더는 중복이므로 삭제
       - 없으면: 지금 규칙에 맞는 이름으로 압축하거나 이름만 바꿔서 보존
    낱장 이미지 폴더처럼 "새로 압축해야 하는" 경우를 제외하면 실제 이미지
    데이터를 다시 받거나 손실시키지 않습니다 - 전부 이름/포장만 바뀝니다.
"""
import argparse
import os
import re
import shutil
import sys
import zipfile

_IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".gif")
_SAFE_RE = re.compile(r'[\\/:*?"<>|]')

# 시리즈 폴더 이름 패턴: "제목 (titleId)"
_SERIES_DIR_RE = re.compile(r'^(.*) \((\d+)\)$')

# 지금 규칙: "제목 0022화#110.zip" (제목 부분은 뒤에서 별도로 안전 처리된 값과
# 대조하므로 여기서는 회차/장수 부분만 파싱)
_CURRENT_RE = re.compile(r'^(?P<title>.*) (?P<ep>\d{3,})화#(?P<count>\d+)\.zip$', re.I)

# 낱장 이미지 폴더(숫자만, 압축 기능 자체가 없던 가장 오래된 버전)
_LOOSE_FOLDER_RE = re.compile(r'^(\d{3,})$')

# 숫자만 있는 .cbz (첫 zip 구현, "화#장수" 표기 없음 - 예: 0089.cbz)
_CBZ_NUMBER_ONLY_RE = re.compile(r'^(\d{3,})\.cbz$', re.I)

# 제목 없이 "N화#장수"만 있는 .zip/.cbz (제로패딩 없을 수도 있음 - 예: 1화#246.zip)
_NO_TITLE_RE = re.compile(r'^(\d+)화#(\d+)\.(zip|cbz)$', re.I)

# 제목은 있지만 "#" 대신 공백으로 장수를 구분하던 중간 버전
# (예: "힐링사무소 0022화 110.zip")
_SPACE_COUNT_RE = re.compile(r'^(?P<title>.*) (?P<ep>\d{3,})화 (?P<count>\d+)\.zip$', re.I)


def safe_name(name):
    name = _SAFE_RE.sub("_", str(name)).strip()
    return name or "untitled"


def current_archive_name(title, episode_no, count, folder_zero_fill=4):
    ep_name = str(episode_no).zfill(int(folder_zero_fill or 4))
    return "%s %s화#%d.zip" % (safe_name(title), ep_name, count)


def find_current_archive(series_dir, title, episode_no, folder_zero_fill=4):
    ep_name = str(episode_no).zfill(int(folder_zero_fill or 4))
    prefix = "%s %s화#" % (safe_name(title), ep_name)
    for fname in os.listdir(series_dir):
        if fname.startswith(prefix) and fname.lower().endswith(".zip"):
            return fname
    return None


def zip_image_count(path):
    try:
        with zipfile.ZipFile(path) as zf:
            return len(zf.namelist())
    except Exception:
        return None


def compress_folder(folder_path, dest_path, dry_run, log):
    files = sorted(f for f in os.listdir(folder_path) if f.lower().endswith(_IMAGE_EXTS))
    if not files:
        log("  (스킵) 압축할 이미지가 없는 빈 폴더: %s" % folder_path)
        if not dry_run:
            shutil.rmtree(folder_path, ignore_errors=True)
        return 0
    if dry_run:
        log("  [dry-run] 압축 예정: %s (%d장) -> %s" % (folder_path, len(files), os.path.basename(dest_path)))
        return len(files)
    tmp_path = dest_path + ".tmp"
    with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for fname in files:
            zf.write(os.path.join(folder_path, fname), arcname=fname)
    os.replace(tmp_path, dest_path)
    shutil.rmtree(folder_path, ignore_errors=True)
    log("  압축 완료: %s -> %s (%d장)" % (folder_path, os.path.basename(dest_path), len(files)))
    return len(files)


def migrate_series_dir(series_dir, title, dry_run, log, folder_zero_fill=4):
    stats = {"converted": 0, "removed_dup": 0, "skipped": 0, "errors": 0}
    try:
        entries = sorted(os.listdir(series_dir))
    except Exception as e:
        log("[에러] 폴더 읽기 실패 %s: %s" % (series_dir, e))
        stats["errors"] += 1
        return stats

    for fname in entries:
        fpath = os.path.join(series_dir, fname)

        # 0) 이미 지금 규칙과 정확히 일치 - 그대로 둠
        if _CURRENT_RE.match(fname):
            stats["skipped"] += 1
            continue

        # 0-1) 미완성 임시 파일/폴더는 항상 삭제
        if fname.endswith(".tmp") or fname.endswith(".cbz.tmp") or fname.endswith(".zip.tmp"):
            log("  임시 항목 삭제: %s" % fname)
            if not dry_run:
                if os.path.isdir(fpath):
                    shutil.rmtree(fpath, ignore_errors=True)
                else:
                    os.remove(fpath)
            stats["converted"] += 1
            continue

        # 1) 낱장 이미지 폴더 (숫자만)
        m = _LOOSE_FOLDER_RE.match(fname) if os.path.isdir(fpath) else None
        if m:
            ep_no = int(m.group(1))
            existing = find_current_archive(series_dir, title, ep_no, folder_zero_fill)
            if existing:
                log("  중복(이미 최신 파일 있음) 낱장 폴더 삭제: %s (기존: %s)" % (fname, existing))
                if not dry_run:
                    shutil.rmtree(fpath, ignore_errors=True)
                stats["removed_dup"] += 1
            else:
                dest_name = current_archive_name(
                    title, ep_no, len([f for f in os.listdir(fpath) if f.lower().endswith(_IMAGE_EXTS)]),
                    folder_zero_fill)
                dest_path = os.path.join(series_dir, dest_name)
                compress_folder(fpath, dest_path, dry_run, log)
                stats["converted"] += 1
            continue

        # 2) 숫자만 있는 .cbz (첫 zip 구현)
        m = _CBZ_NUMBER_ONLY_RE.match(fname)
        if m:
            ep_no = int(m.group(1))
            existing = find_current_archive(series_dir, title, ep_no, folder_zero_fill)
            if existing:
                log("  중복(이미 최신 파일 있음) 예전 cbz 삭제: %s (기존: %s)" % (fname, existing))
                if not dry_run:
                    os.remove(fpath)
                stats["removed_dup"] += 1
            else:
                count = zip_image_count(fpath)
                if count is None:
                    log("  [에러] 압축 손상으로 읽기 실패, 건너뜀: %s" % fname)
                    stats["errors"] += 1
                    continue
                dest_name = current_archive_name(title, ep_no, count, folder_zero_fill)
                dest_path = os.path.join(series_dir, dest_name)
                log("  이름 변경: %s -> %s" % (fname, dest_name))
                if not dry_run:
                    os.rename(fpath, dest_path)
                stats["converted"] += 1
            continue

        # 3) 제목 없이 "N화#장수"만 있는 파일
        m = _NO_TITLE_RE.match(fname)
        if m:
            ep_no = int(m.group(1))
            count = int(m.group(2))
            existing = find_current_archive(series_dir, title, ep_no, folder_zero_fill)
            if existing:
                log("  중복(이미 최신 파일 있음) 제목없는 파일 삭제: %s (기존: %s)" % (fname, existing))
                if not dry_run:
                    os.remove(fpath)
                stats["removed_dup"] += 1
            else:
                dest_name = current_archive_name(title, ep_no, count, folder_zero_fill)
                dest_path = os.path.join(series_dir, dest_name)
                log("  이름 변경: %s -> %s" % (fname, dest_name))
                if not dry_run:
                    os.rename(fpath, dest_path)
                stats["converted"] += 1
            continue

        # 4) 제목은 있지만 "#" 대신 공백으로 장수를 구분하던 버전
        m = _SPACE_COUNT_RE.match(fname)
        if m:
            ep_no = int(m.group("ep"))
            count = int(m.group("count"))
            existing = find_current_archive(series_dir, title, ep_no, folder_zero_fill)
            if existing:
                log("  중복(이미 최신 파일 있음) 공백구분 파일 삭제: %s (기존: %s)" % (fname, existing))
                if not dry_run:
                    os.remove(fpath)
                stats["removed_dup"] += 1
            else:
                dest_name = current_archive_name(title, ep_no, count, folder_zero_fill)
                dest_path = os.path.join(series_dir, dest_name)
                log("  이름 변경: %s -> %s" % (fname, dest_name))
                if not dry_run:
                    os.rename(fpath, dest_path)
                stats["converted"] += 1
            continue

        # 그 외(README, kavita.yaml 등 우리가 만든 게 아닌 파일)는 그냥 둔다
        stats["skipped"] += 1

    return stats


def main():
    parser = argparse.ArgumentParser(description="webtoon_manager 예전 파일명을 현재 규칙으로 정리")
    parser.add_argument("download_root", help="플러그인 설정의 '다운로드 저장 경로' 값과 동일하게 입력")
    parser.add_argument("--dry-run", action="store_true", help="실제로 바꾸지 않고 무엇을 할지만 출력")
    parser.add_argument("--folder-zero-fill", type=int, default=4, help="회차 번호 자릿수(기본 4)")
    args = parser.parse_args()

    def log(msg):
        print(msg, flush=True)

    root = args.download_root
    if not os.path.isdir(root):
        log("[에러] 다운로드 경로가 존재하지 않습니다: %s" % root)
        sys.exit(1)

    log("다운로드 경로: %s (dry-run=%s)" % (root, args.dry_run))
    total = {"converted": 0, "removed_dup": 0, "skipped": 0, "errors": 0}
    series_dirs = sorted(os.listdir(root))
    checked = 0

    for name in series_dirs:
        full_path = os.path.join(root, name)
        if not os.path.isdir(full_path):
            continue
        m = _SERIES_DIR_RE.match(name)
        if not m:
            continue
        title = m.group(1)
        checked += 1
        log("\n=== [%s] ===" % name)
        stats = migrate_series_dir(full_path, title, args.dry_run, log, args.folder_zero_fill)
        for k in total:
            total[k] += stats[k]

    log("\n========================================")
    log("작품 %d개 확인 완료 (dry-run=%s)" % (checked, args.dry_run))
    log("변환: %d개 / 중복 삭제: %d개 / 그대로 둠: %d개 / 에러: %d개" %
        (total["converted"], total["removed_dup"], total["skipped"], total["errors"]))
    if args.dry_run:
        log("\n※ dry-run 모드라 실제로 아무것도 바뀌지 않았습니다.")
        log("   결과가 예상과 맞으면 --dry-run 없이 다시 실행하세요.")


if __name__ == "__main__":
    main()
