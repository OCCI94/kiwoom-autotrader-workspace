"""Local-only explicit research freeze/audit and single-index commands."""
import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
from .contracts import MAX_FILE_BYTES, audit_manifest, freeze_protocol, load_json_file
from .single_index import fit_single_index


def _write_new_json(path, result):
    """Publish a complete private file without replacing any existing path."""
    payload = (json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
    path = Path(path)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="wb", dir=path.parent, prefix=".single-index-", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        # A hard link is an atomic, exclusive publication; existing files,
        # dangling symlinks and the source input can never be overwritten.
        os.link(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description="오프라인 연구 입력·가설 고정과 SHA 재검증")
    commands = parser.add_subparsers(dest="command", required=True)
    freeze = commands.add_parser("freeze", help="명시한 입력 파일과 가설을 새 manifest에 고정")
    freeze.add_argument("--spec", type=Path, required=True)
    freeze.add_argument("--root", type=Path, required=True)
    freeze.add_argument("--file", action="append", required=True, dest="files")
    freeze.add_argument("--output", type=Path, required=True)
    audit = commands.add_parser("audit", help="저장한 manifest의 파일 무결성을 오프라인 검증")
    audit.add_argument("--manifest", type=Path, required=True)
    audit.add_argument("--root", type=Path, required=True)
    index = commands.add_parser("single-index", help="명시적인 수익률 입력으로 오프라인 단일지수 회귀")
    index.add_argument("--input", type=Path, required=True)
    index.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "freeze":
            result = freeze_protocol(load_json_file(args.spec, 65536), args.root, args.files, args.output)
            print(json.dumps({"created": True, "schema": result["schema"], "file_count": len(result["files"]), "profitability_verified": False}, ensure_ascii=False))
            return 0
        if args.command == "single-index":
            spec = load_json_file(args.input, MAX_FILE_BYTES)
            fields = {"context", "as_of", "training_end", "analysis_mode", "rows", "min_observations"}
            if not isinstance(spec, dict) or set(spec) != fields:
                raise ValueError("single-index input fields missing or unknown")
            result = fit_single_index(**spec)
            _write_new_json(args.output, result)
            print(json.dumps({"created": True, "schema": result["schema"], "fit_status": result["fit"]["status"], "automatic_orders": False}, ensure_ascii=False))
            return 0
        result = audit_manifest(args.manifest, args.root)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["source_integrity"] else 1
    except (ValueError, OSError, TypeError, KeyError, OverflowError, RecursionError):
        print("연구 파일의 스키마·경로·무결성을 확인하지 못했습니다. 원본은 변경하지 않았습니다.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
