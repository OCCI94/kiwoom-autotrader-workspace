"""Explicit offline CLI. It never imports the listener/status entry points."""
import argparse
import json
import os
from pathlib import Path
import sys

from .audit import audit_local
from .render import render_html


def _write_new(path, text):
    target = Path(path)
    fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())


def main(argv=None):
    parser = argparse.ArgumentParser(description="로컬 원장·CSV·상태 파일만 읽는 오프라인 감사")
    parser.add_argument("--ledger", type=Path, help="검사할 SQLite 원장 파일")
    parser.add_argument("--csv", type=Path, help="검사할 일별 CSV 파일")
    parser.add_argument("--state", type=Path, help="검사할 listener 상태 JSON")
    parser.add_argument("--output", type=Path, help="새 JSON 결과 파일 (기존 파일 덮어쓰기 금지)")
    parser.add_argument("--html", type=Path, help="새 정적 HTML 결과 파일 (기존 파일 덮어쓰기 금지)")
    args = parser.parse_args(argv)
    try:
        inputs = {path.resolve() for path in (args.ledger, args.csv, args.state) if path is not None}
        outputs = [path for path in (args.output, args.html) if path is not None]
        resolved = [path.resolve() for path in outputs]
        # Reserve sidecar paths too; an absent WAL is still part of the source.
        if args.ledger is not None:
            inputs.update(Path(str(args.ledger) + suffix).resolve() for suffix in ("-wal", "-shm", "-journal"))
        if (len(set(resolved)) != len(resolved) or any(path in inputs for path in resolved)
                or any(path.exists() or path.is_symlink() for path in outputs)):
            raise ValueError
        report = audit_local(ledger_path=args.ledger, csv_path=args.csv, state_path=args.state)
        payload = json.dumps(report.to_dict(), ensure_ascii=False, indent=2) + "\n"
        if args.output is not None:
            _write_new(args.output, payload)
        if args.html is not None:
            _write_new(args.html, render_html(report))
        if args.output is None:
            print(payload, end="")
        return 0
    except (OSError, ValueError, RuntimeError):
        print("감사 결과를 저장하지 못했습니다. 입력과 다른 새 출력 경로 및 쓰기 권한을 확인하세요.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
