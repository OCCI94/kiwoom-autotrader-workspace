"""Explicit offline CLI; never decrypts archives or selects live credentials."""
import argparse
import json
import sys

from .core import COHORTS, HistoryError, HistoryConflict, validate_bundle, import_bundle, report_history


def main(argv=None):
    parser = argparse.ArgumentParser(description="Offline preserved-history import; canonical ai-stock-history-v1 only")
    commands = parser.add_subparsers(dest="command", required=True)
    validate = commands.add_parser("validate", help="Validate explicit canonical manifest and JSONL, without writing a DB")
    validate.add_argument("--bundle", required=True)
    importer = commands.add_parser("import", help="Import only into an isolated historical SQLite file")
    importer.add_argument("--bundle", required=True)
    importer.add_argument("--database", required=True)
    report = commands.add_parser("report", help="Read-only, separate reports for each lineage/cohort/currency")
    report.add_argument("--database", required=True)
    report.add_argument("--cohort", choices=tuple(COHORTS))
    args = parser.parse_args(argv)
    try:
        if args.command == "validate":
            result = validate_bundle(args.bundle).summary
        elif args.command == "import":
            result = import_bundle(args.bundle, args.database)
        else:
            result = report_history(args.database, cohort=args.cohort)
    except HistoryError as error:
        result = {"historical_import_verified": False, "authoritative_history_complete": False,
            "current_account_verified": False, "deployed": False, "error": str(error)}
        if isinstance(error, HistoryConflict):
            result["quarantined_conflicts"] = error.conflicts
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 2
    except Exception:
        print(json.dumps({"historical_import_verified": False, "error": "Historical file/database operation failed; no external details disclosed."}))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
