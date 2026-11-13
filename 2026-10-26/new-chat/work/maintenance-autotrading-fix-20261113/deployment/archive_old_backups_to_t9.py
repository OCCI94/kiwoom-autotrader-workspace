#!/usr/bin/env python3
"""Optional helper to archive older (pre-2026-11-12) KiwoomAutotrader backups to /Volumes/T9.

Never touches today's (2026-11-13) or yesterday's (2026-11-12) rollback backups,
never touches live runtime/state/ledger files. Dry-run by default; pass `--execute` to move.
"""
from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import os
from pathlib import Path
import shutil
import sys
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")
BACKUPS_ROOT = Path("/Users/tr-oh/Library/Application Support/KiwoomAutotrader/backups")
T9_ARCHIVE = Path("/Volumes/T9/KiwoomAutotrader-Backups-Archive")
KEEP_RECENT_PREFIXES = ("20261112", "20261113", "2026-11-12", "2026-11-13")


def dir_size(path: Path) -> int:
    total = 0
    for root, _, files in os.walk(path, followlinks=False):
        for name in files:
            fp = Path(root) / name
            if not fp.is_symlink():
                total += fp.stat().st_size
    return total


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", help="실제로 /Volumes/T9로 복사 검증 후 원본 백업을 정리합니다.")
    args = parser.parse_args()

    if not BACKUPS_ROOT.is_dir():
        print(f"백업 폴더가 없습니다: {BACKUPS_ROOT}")
        return 0
    if not Path("/Volumes/T9").is_dir() or not os.path.ismount("/Volumes/T9"):
        print("[중단] /Volumes/T9 외장 볼륨이 마운트되어 있지 않습니다.", file=sys.stderr)
        return 1

    candidates = []
    for item in sorted(BACKUPS_ROOT.iterdir()):
        if item.is_symlink() or not item.is_dir():
            continue
        mtime_day = datetime.fromtimestamp(item.stat().st_mtime, KST).strftime("%Y%m%d")
        if item.name.startswith(KEEP_RECENT_PREFIXES) or mtime_day in ("20261112", "20261113"):
            continue
        candidates.append((item, dir_size(item), mtime_day))

    if not candidates:
        print("이관 대상 구형 백업(2026-11-11 이전)이 없습니다.")
        return 0

    total_gib = sum(sz for _, sz, _ in candidates) / (1024 ** 3)
    print(f"발견된 구형 백업 {len(candidates)}개 (총 {total_gib:.2f} GiB):")
    for item, sz, mtime_day in candidates:
        print(f"  - {item.name} (수정일 {mtime_day}, {sz / (1024 ** 3):.2f} GiB)")

    if not args.execute:
        print("\n[Dry-Run] 실제 이관하려면 `--execute` 옵션을 붙여 실행하세요.")
        return 0

    T9_ARCHIVE.mkdir(mode=0o700, parents=True, exist_ok=True)
    for item, sz, _ in candidates:
        dest = T9_ARCHIVE / item.name
        if dest.exists():
            print(f"  [건너뜀] 대상이 이미 존재합니다: {dest}")
            continue
        print(f"  → 복사 중: {item.name} -> {dest} ...", flush=True)
        shutil.copytree(item, dest, symlinks=False)
        src_files = sorted(p.relative_to(item) for p in item.rglob("*") if p.is_file() and not p.is_symlink())
        dst_files = sorted(p.relative_to(dest) for p in dest.rglob("*") if p.is_file() and not p.is_symlink())
        if src_files != dst_files:
            raise RuntimeError(f"파일 목록 불일치: {item.name}")
        for rel in src_files:
            if (item / rel).stat().st_size != (dest / rel).stat().st_size:
                raise RuntimeError(f"파일 크기 불일치: {item.name}/{rel}")
        shutil.rmtree(item)
        print(f"  ✔ 이관 및 검증 완료: {dest} ({sz / (1024 ** 3):.2f} GiB 회수)")

    free_gib = shutil.disk_usage(BACKUPS_ROOT).free / (1024 ** 3)
    print(f"\n완료! 현재 로컬 SSD 여유 공간: {free_gib:.2f} GiB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
