#!/usr/bin/env python3
"""Render a LaunchAgent plist for a kiwoom-autotrader worker into a review path.

Builds the plist with plistlib on top of assets/plist-template.xml (used as a
dictionary of defaults; its comments are documentation only). Never writes
under ~/Library/LaunchAgents or /Library/Launch*, never runs launchctl, never
writes secret-looking environment values. After writing it prints the exact
commands the human runs next.

Program arguments go after `--`:
  render_plist.py --label L --program P --interval 600 --log-dir D --out O -- --state-dir /x --flag

Exit codes: 0 written/printed, 1 written with warnings, 2 refused, 3 usage error.
"""
from __future__ import annotations

import argparse
import os
import plistlib
import re
import sys
from pathlib import Path

LABEL_PREFIX = "com.local.kiwoom-autotrader."
LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
SECRET_KEY_RE = re.compile(
    r"(TOKEN|SECRET|PASSW|APIKEY|API_KEY|APPKEY|APP_KEY|PRIVATE|CREDENTIAL|AUTH|COOKIE|SESSION)", re.I
)
TOKEN_LIKE_RE = re.compile(r"^[A-Za-z0-9_\-+/=:.]{20,}$")
CALENDAR_KEYS = {"Minute", "Hour", "Day", "Weekday", "Month"}
CALENDAR_RANGES = {"Minute": (0, 59), "Hour": (0, 23), "Day": (1, 31), "Weekday": (0, 7), "Month": (1, 12)}
TRIGGER_KEYS = ("StartInterval", "StartCalendarInterval", "KeepAlive", "RunAtLoad")
DEFAULT_TEMPLATE = Path(__file__).resolve().parent.parent / "assets" / "plist-template.xml"
DEFAULT_LAUNCH_AGENTS = Path.home() / "Library" / "LaunchAgents"
FORBIDDEN_ROOTS = [
    DEFAULT_LAUNCH_AGENTS,
    Path("/Library/LaunchAgents"),
    Path("/Library/LaunchDaemons"),
    Path("/System/Library/LaunchAgents"),
    Path("/System/Library/LaunchDaemons"),
]


def parse_calendar(spec: str) -> dict:
    """'HH:MM' -> {Hour, Minute}; 'Weekday=N,Hour=H,Minute=M' -> dict of ints."""
    spec = spec.strip()
    match = re.match(r"^(\d{1,2}):(\d{2})$", spec)
    if match:
        entry = {"Hour": int(match.group(1)), "Minute": int(match.group(2))}
    else:
        entry = {}
        for part in spec.split(","):
            key, sep, value = part.strip().partition("=")
            if not sep or key.strip() not in CALENDAR_KEYS or not value.strip().lstrip("-").isdigit():
                raise ValueError(f"bad --calendar spec {spec!r}: use 'HH:MM' or 'Weekday=N,Hour=H,Minute=M'")
            entry[key.strip()] = int(value.strip())
        if not entry:
            raise ValueError("empty --calendar spec")
    for key, value in entry.items():
        low, high = CALENDAR_RANGES[key]
        if not low <= value <= high:
            raise ValueError(f"--calendar {key}={value} outside {low}..{high}")
    if "Hour" in entry and "Minute" not in entry:
        entry["Minute"] = 0
    return entry


def looks_like_secret(key: str, value: str) -> bool:
    if value.startswith(("/", "$", "~")):
        return False
    if SECRET_KEY_RE.search(key):
        return True
    return bool(TOKEN_LIKE_RE.match(value) and re.search(r"[A-Za-z]", value) and re.search(r"[0-9]", value))


def under_forbidden_root(path: Path):
    resolved = path.expanduser().resolve()
    for root in FORBIDDEN_ROOTS:
        try:
            resolved.relative_to(root.resolve())
            return root
        except (ValueError, OSError):
            continue
    return None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Render a kiwoom-autotrader LaunchAgent plist to a review path (never into LaunchAgents).",
        epilog="Program arguments follow `--`: ... --out O -- --state-dir /path --flag",
    )
    parser.add_argument("--template", default=str(DEFAULT_TEMPLATE), help="annotated template (default: %(default)s)")
    parser.add_argument("--label", required=True, help=f"service label, normally {LABEL_PREFIX}<worker>")
    parser.add_argument("--program", required=True, help="absolute path of the executable (ProgramArguments[0])")
    parser.add_argument("--args", action="append", default=[], metavar="ARG",
                        help="one program argument (repeatable; use --args=--flag for dash-leading values)")
    trigger = parser.add_mutually_exclusive_group()
    trigger.add_argument("--interval", type=int, metavar="SECONDS", help="StartInterval")
    trigger.add_argument("--calendar", action="append", metavar="SPEC",
                         help="StartCalendarInterval: 'HH:MM' (daily) or 'Weekday=N,Hour=H,Minute=M' (repeatable)")
    parser.add_argument("--keepalive", action="store_true", help="KeepAlive {SuccessfulExit: false} + RunAtLoad true")
    parser.add_argument("--run-at-load", action="store_true", help="RunAtLoad true")
    parser.add_argument("--log-dir", required=True, help="directory for <short>.log and <short>.error.log")
    parser.add_argument("--working-dir", help="WorkingDirectory (default: template value)")
    parser.add_argument("--throttle", type=int, metavar="SECONDS", help="ThrottleInterval >= 10 (default: template value)")
    parser.add_argument("--env", action="append", default=[], metavar="KEY=VALUE",
                        help="EnvironmentVariables entry (repeatable). VALUE '$NAME' is documentation only and is not written")
    parser.add_argument("--no-session-limit", action="store_true", help="drop LimitLoadToSessionType from the template")
    parser.add_argument("--out", required=True, help="output path; refused under ~/Library/LaunchAgents")
    parser.add_argument("--dry-run", action="store_true", help="print XML to stdout, write nothing")
    parser.add_argument("--force", action="store_true", help="overwrite an existing --out file")
    parser.add_argument("extra_args", nargs="*", help="program arguments after `--`")
    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    warnings = []

    if not LABEL_RE.match(args.label):
        print(f"error: label has unexpected characters: {args.label!r}", file=sys.stderr)
        return 3
    if not args.label.startswith(LABEL_PREFIX):
        warnings.append(f"label does not start with {LABEL_PREFIX!r}")
    short = args.label[len(LABEL_PREFIX):] if args.label.startswith(LABEL_PREFIX) else args.label

    out = Path(args.out).expanduser()
    root = under_forbidden_root(out)
    if root is not None:
        print(
            f"refused: --out {out} is under {root}. This skill only renders to a review path.\n"
            f"Review the file, then copy it yourself:\n"
            f"  cp <review-path>/{args.label}.plist {DEFAULT_LAUNCH_AGENTS}/{args.label}.plist\n"
            f"  launchctl bootstrap gui/$(id -u) {DEFAULT_LAUNCH_AGENTS}/{args.label}.plist",
            file=sys.stderr,
        )
        return 2
    if out.suffix != ".plist":
        warnings.append("--out does not end with .plist")
    if out.stem != args.label:
        warnings.append(f"--out filename stem {out.stem!r} differs from label; rename to {args.label}.plist before copying")
    if out.exists() and not args.dry_run and not args.force:
        print(f"refused: {out} exists; pass --force to overwrite", file=sys.stderr)
        return 2

    template = Path(args.template).expanduser()
    if not template.is_file():
        print(f"error: template not found: {template}", file=sys.stderr)
        return 3
    try:
        with template.open("rb") as handle:
            data = plistlib.load(handle)
    except Exception as exc:  # noqa: BLE001
        print(f"error: template does not parse: {exc}", file=sys.stderr)
        return 3
    if not isinstance(data, dict):
        print("error: template top-level is not a dict", file=sys.stderr)
        return 3

    program = args.program
    if not Path(program).is_absolute():
        print(f"error: --program must be an absolute path: {program}", file=sys.stderr)
        return 3
    if not Path(program).exists():
        warnings.append(f"program does not exist on this machine: {program}")
    elif not os.access(program, os.X_OK):
        warnings.append(f"program is not executable: {program}")

    program_args = [program] + list(args.args) + list(args.extra_args)
    for arg in program_args[1:]:
        if arg.startswith("/") and not Path(arg).exists():
            hint = " (external volume mounted?)" if arg.startswith("/Volumes/") else ""
            warnings.append(f"path argument does not exist{hint}: {arg}")

    # Trigger
    for key in TRIGGER_KEYS:
        data.pop(key, None)
    if args.interval is not None:
        if args.interval <= 0:
            print("error: --interval must be positive", file=sys.stderr)
            return 3
        data["StartInterval"] = args.interval
        if args.keepalive:
            warnings.append("--interval with --keepalive: exit 0 is relaunched every interval and non-zero exit restarts "
                            "after ThrottleInterval; this is the noisy telegram-nightly pattern, keep one trigger")
    if args.calendar:
        try:
            entries = [parse_calendar(spec) for spec in args.calendar]
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 3
        data["StartCalendarInterval"] = entries[0] if len(entries) == 1 else entries
        if args.keepalive:
            warnings.append("--calendar with --keepalive: keep one trigger")
    if args.keepalive:
        data["KeepAlive"] = {"SuccessfulExit": False}
        data["RunAtLoad"] = True
    elif args.run_at_load:
        data["RunAtLoad"] = True
    if not (args.interval is not None or args.calendar or args.keepalive or args.run_at_load):
        print("error: choose a trigger: --interval, --calendar, --keepalive or --run-at-load", file=sys.stderr)
        return 3

    # Throttle
    if args.throttle is not None:
        if args.throttle < 10:
            print("error: --throttle must be >= 10 (launchd minimum)", file=sys.stderr)
            return 3
        data["ThrottleInterval"] = args.throttle
    elif not isinstance(data.get("ThrottleInterval"), int):
        data["ThrottleInterval"] = 60

    # Paths
    data["Label"] = args.label
    data["ProgramArguments"] = program_args
    log_dir = Path(args.log_dir).expanduser()
    if not log_dir.is_absolute():
        print("error: --log-dir must be absolute", file=sys.stderr)
        return 3
    if not log_dir.is_dir():
        warnings.append(f"log dir does not exist yet (launchd needs it before bootstrap): {log_dir}")
    data["StandardOutPath"] = str(log_dir / f"{short}.log")
    data["StandardErrorPath"] = str(log_dir / f"{short}.error.log")
    if args.working_dir:
        wd = Path(args.working_dir).expanduser()
        if not wd.is_absolute():
            print("error: --working-dir must be absolute", file=sys.stderr)
            return 3
        data["WorkingDirectory"] = str(wd)
    wd_value = data.get("WorkingDirectory")
    if wd_value and not Path(wd_value).is_dir():
        warnings.append(f"WorkingDirectory does not exist: {wd_value}")
    if args.no_session_limit:
        data.pop("LimitLoadToSessionType", None)

    # Environment: names only for passthrough, refuse secrets
    env = data.get("EnvironmentVariables")
    if not isinstance(env, dict):
        env = {}
    passthrough = []
    for item in args.env:
        key, sep, value = item.partition("=")
        key = key.strip()
        if not sep or not key:
            print(f"error: --env expects KEY=VALUE, got {item!r}", file=sys.stderr)
            return 3
        if value.startswith("$"):
            passthrough.append((key, value.lstrip("$").strip("{}")))
            continue
        if looks_like_secret(key, value):
            print(
                f"refused: --env {key} looks like a secret; secrets never go into a plist. "
                f"Use --env {key}='$' + NAME to document that the program reads it itself.",
                file=sys.stderr,
            )
            return 2
        env[key] = value
    if env:
        data["EnvironmentVariables"] = env
    else:
        data.pop("EnvironmentVariables", None)

    xml = plistlib.dumps(data, fmt=plistlib.FMT_XML, sort_keys=True).decode("utf-8")
    if args.dry_run:
        sys.stdout.write(xml)
        report = sys.stderr
    else:
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("wb") as handle:
            handle.write(xml.encode("utf-8"))
        os.chmod(out, 0o600)
        report = sys.stdout
        print(f"written: {out} (mode 0600)", file=report)

    for warning in warnings:
        print(f"warning: {warning}", file=report)
    for key, name in passthrough:
        print(f"env passthrough (not written): {key} <- program must read ${name} from its own environment/keychain", file=report)

    validator = Path(__file__).resolve().parent / "validate_plist.py"
    shown_out = out if out.stem == args.label else out.parent / f"{args.label}.plist"
    print("", file=report)
    print("# 검토 후 사람이 직접 실행한다. 이 스킬은 아래 명령을 실행하지 않는다.", file=report)
    if shown_out != out:
        print(f'mv "{out}" "{shown_out}"', file=report)
    print(f'plutil -lint "{shown_out}"', file=report)
    print(f'python3 "{validator}" "{shown_out}"', file=report)
    if args.label == f"{LABEL_PREFIX}telegram":
        print("# 주문 수신기 라벨: 거래일 09:00-15:30 KST 에는 bootout 하지 않는다. 명시적 승인 후 장 마감 뒤에만.", file=report)
    print(f'cp "{shown_out}" ~/Library/LaunchAgents/{args.label}.plist', file=report)
    print(f"launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/{args.label}.plist", file=report)
    print(f"launchctl print gui/$(id -u)/{args.label}", file=report)
    print("# 되돌리기 (정확한 라벨만):", file=report)
    print(f"launchctl bootout gui/$(id -u)/{args.label}", file=report)
    return 1 if warnings else 0


if __name__ == "__main__":
    sys.exit(main())
