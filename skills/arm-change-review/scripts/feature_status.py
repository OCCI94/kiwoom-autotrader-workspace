#!/usr/bin/env python3
"""Print the arm/feature gate registry (LIVE / SHADOW / OFF) and warn about stale entries.

Registry: a JSON array of entries, or an object with an "entries" array. Entry fields:
  name (required), state (LIVE|SHADOW|OFF, required), owner, since (YYYY-MM-DD),
  expires (YYYY-MM-DD or ""), evidence, notes. Optional extras: scope, example.

Warnings (exit 1):
  - `expires` is in the past and state is not OFF  -> should be OFF (auto-OFF policy)
  - LIVE entry without `evidence`
  - duplicate `name`
  - SHADOW entry without `expires` (SHADOW must carry an expiry)
  - unknown field names (likely typos such as "expire")
Errors (exit 3): unreadable/invalid JSON, entry not an object, missing name,
unknown state, bad date format.

Read-only: this script never edits the registry. Flipping a state is a human action
recorded in the change record. Standard library only, Python 3.9+.
"""
from __future__ import annotations

import argparse
import json
import sys
import unicodedata
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

STATES = ("LIVE", "SHADOW", "OFF")
KNOWN_FIELDS = ("name", "state", "owner", "since", "expires", "evidence", "notes", "scope", "example")
DEFAULT_REGISTRY = Path(__file__).resolve().parent.parent / "references" / "feature-flags.example.json"


def today_kst() -> date:
    try:
        from zoneinfo import ZoneInfo

        return datetime.now(ZoneInfo("Asia/Seoul")).date()
    except Exception:
        return datetime.now(timezone(timedelta(hours=9))).date()


def parse_date(value, field: str, name: str) -> Optional[date]:
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise ValueError(f"{name}: {field} must be a 'YYYY-MM-DD' string")
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%d").date()
    except ValueError:
        raise ValueError(f"{name}: {field} {value!r} is not YYYY-MM-DD")


def load_registry(path: Path) -> Tuple[List[dict], Optional[str]]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    note = None
    if isinstance(raw, dict):
        note = raw.get("note")
        raw = raw.get("entries")
    if not isinstance(raw, list):
        raise ValueError("registry must be a JSON array of entries or an object with an 'entries' array")
    for i, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise ValueError(f"entry #{i} is not an object")
        name = entry.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"entry #{i} has no name")
        state = entry.get("state")
        if state not in STATES:
            raise ValueError(f"{name}: state {state!r} must be one of {STATES}")
        parse_date(entry.get("since"), "since", name)
        parse_date(entry.get("expires"), "expires", name)
    return raw, note


def check(entries: List[dict], at: date) -> Tuple[List[str], Dict[str, List[str]]]:
    warnings: List[str] = []
    per_entry: Dict[str, List[str]] = {}
    seen: Dict[str, int] = {}

    def warn(name: str, message: str) -> None:
        warnings.append(f"{name}: {message}")
        per_entry.setdefault(name, []).append(message)

    for entry in entries:
        name = entry["name"].strip()
        state = entry["state"]
        seen[name] = seen.get(name, 0) + 1
        if seen[name] == 2:
            warn(name, "duplicate name (registry must have one row per gate)")
        expires = parse_date(entry.get("expires"), "expires", name)
        evidence = entry.get("evidence")
        has_evidence = isinstance(evidence, str) and evidence.strip() != ""
        if expires is not None and expires < at and state != "OFF":
            warn(name, f"expires {expires.isoformat()} has passed but state is {state}; policy says it should be OFF")
        if state == "LIVE" and not has_evidence:
            warn(name, "LIVE without evidence (link the change record / isolated suite / SHADOW result)")
        if state == "SHADOW" and expires is None:
            warn(name, "SHADOW without expires (SHADOW must carry an expiry date)")
        unknown = [k for k in entry if k not in KNOWN_FIELDS]
        if unknown:
            warn(name, f"unknown field(s) {unknown} (typo? known: {list(KNOWN_FIELDS)})")
    return warnings, per_entry


def _width(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text)


def _pad(text: str, width: int) -> str:
    return text + " " * max(0, width - _width(text))


def _cell(value, limit: int) -> str:
    text = "" if value is None else str(value).replace("\n", " ")
    if _width(text) <= limit:
        return text
    out = ""
    for ch in text:
        if _width(out + ch) > limit - 1:
            break
        out += ch
    return out + "…"


def print_table(entries: List[dict], per_entry: Dict[str, List[str]], at: date) -> None:
    columns = (("name", 32), ("state", 6), ("owner", 10), ("since", 10), ("expires", 10), ("evidence", 28), ("warn", 4))
    rows = []
    for entry in entries:
        name = entry["name"].strip()
        rows.append([
            _cell(name, 32),
            entry["state"],
            _cell(entry.get("owner"), 10),
            _cell(entry.get("since"), 10),
            _cell(entry.get("expires") or "-", 10),
            _cell(entry.get("evidence") or "-", 28),
            str(len(per_entry.get(name, []))),
        ])
    widths = [max(w, max((_width(r[i]) for r in rows), default=0)) for i, (_, w) in enumerate(columns)]
    header = "  ".join(_pad(c, widths[i]) for i, (c, _) in enumerate(columns))
    print(f"registry as of {at.isoformat()} (KST)")
    print(header)
    print("  ".join("-" * w for w in widths))
    for r in rows:
        print("  ".join(_pad(r[i], widths[i]) for i in range(len(columns))))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Show LIVE/SHADOW/OFF gate registry and warn about stale entries. Read-only.")
    parser.add_argument("--registry", default=str(DEFAULT_REGISTRY), help=f"registry JSON (default: {DEFAULT_REGISTRY})")
    parser.add_argument("--at", help="date 'YYYY-MM-DD' used for expiry checks (default: today KST)")
    parser.add_argument("--json", action="store_true", help="machine-readable JSON on stdout")
    args = parser.parse_args(argv)

    path = Path(args.registry).expanduser()
    try:
        at = datetime.strptime(args.at, "%Y-%m-%d").date() if args.at else today_kst()
    except ValueError:
        print(f"error: --at must be YYYY-MM-DD, got {args.at!r}", file=sys.stderr)
        return 3
    try:
        entries, note = load_registry(path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"error: cannot load registry {path}: {exc}", file=sys.stderr)
        return 3

    warnings, per_entry = check(entries, at)
    status = "warn" if warnings else "clean"
    if args.json:
        payload = {
            "registry": str(path),
            "at": at.isoformat(),
            "status": status,
            "note": note,
            "entries": [dict(e, warnings=per_entry.get(e["name"].strip(), [])) for e in entries],
            "warnings": warnings,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        if note:
            print(f"note: {note}")
        print_table(entries, per_entry, at)
        for w in warnings:
            print(f"warning: {w}")
        print(f"status: {status} ({len(entries)} entries, {len(warnings)} warnings)")
    return 1 if warnings else 0


if __name__ == "__main__":
    sys.exit(main())
