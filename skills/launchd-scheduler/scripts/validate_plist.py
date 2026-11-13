#!/usr/bin/env python3
"""Validate launchd LaunchAgent plists for the kiwoom-autotrader workers.

Read-only. Python 3.9+ standard library (plistlib) plus `plutil -lint` when
available. Never prints EnvironmentVariables values; key names only.

Checks: parses; Label present, equals filename stem, starts with the project
prefix (warning otherwise); ProgramArguments[0] is an absolute existing
executable; absolute path arguments exist (warning); StandardOutPath /
StandardErrorPath parent directories exist and sit under the state root
(warning); exactly one trigger among StartInterval / StartCalendarInterval /
KeepAlive-driven (warning on combinations); ThrottleInterval >= 10; RunAtLoad
noted; EnvironmentVariables keys listed and secret-looking values flagged;
WorkingDirectory exists.

Exit codes: 0 pass, 1 warnings only, 2 violations, 3 usage/IO error.
"""
from __future__ import annotations

import argparse
import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
from pathlib import Path

LABEL_PREFIX = "com.local.kiwoom-autotrader."
DEFAULT_STATE_ROOT = Path.home() / "Library" / "Application Support" / "KiwoomAutotrader"
LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
SECRET_KEY_RE = re.compile(
    r"(TOKEN|SECRET|PASSW|APIKEY|API_KEY|APPKEY|APP_KEY|PRIVATE|CREDENTIAL|AUTH|COOKIE|SESSION)", re.I
)
TOKEN_LIKE_RE = re.compile(r"^[A-Za-z0-9_\-+/=:.]{20,}$")
SECRET_PATH_RE = re.compile(r"(/\.secrets?/|credential|api[_-]?key|/token)", re.I)
CALENDAR_KEYS = {"Minute", "Hour", "Day", "Weekday", "Month"}
CALENDAR_RANGES = {"Minute": (0, 59), "Hour": (0, 23), "Day": (1, 31), "Weekday": (0, 7), "Month": (1, 12)}
KEEPALIVE_KEYS = {"SuccessfulExit", "Crashed", "NetworkState", "PathState", "OtherJobEnabled", "AfterInitialDemand"}
KNOWN_KEYS = {
    "Label", "Program", "ProgramArguments", "WorkingDirectory", "StandardOutPath", "StandardErrorPath",
    "StandardInPath", "StartInterval", "StartCalendarInterval", "KeepAlive", "RunAtLoad", "ThrottleInterval",
    "ProcessType", "Umask", "LimitLoadToSessionType", "LimitLoadToHosts", "LimitLoadFromHosts",
    "EnvironmentVariables", "Nice", "Disabled", "ExitTimeOut", "TimeOut", "WatchPaths", "QueueDirectories",
    "StartOnMount", "AbandonProcessGroup", "EnableGlobbing", "LowPriorityIO", "LowPriorityBackgroundIO",
    "LaunchOnlyOnce", "SoftResourceLimits", "HardResourceLimits", "MachServices", "Sockets", "UserName",
    "GroupName", "InitGroups", "RootDirectory", "EnableTransactions", "EnablePressuredExit",
    "MaterializeDatalessFiles", "SessionCreate", "AssociatedBundleIdentifiers", "LegacyTimers",
}


def looks_like_secret(key: str, value) -> bool:
    """True when an EnvironmentVariables entry looks like a credential. Never echoes the value."""
    if not isinstance(value, str) or not value:
        return False
    if value.startswith(("/", "$", "~")):
        return False
    if SECRET_KEY_RE.search(key):
        return True
    if TOKEN_LIKE_RE.match(value) and re.search(r"[A-Za-z]", value) and re.search(r"[0-9]", value):
        return True
    return False


def run_plutil(path: Path):
    """Return (status, message) from `plutil -lint`; status in {'ok','fail','missing'}."""
    exe = shutil.which("plutil")
    if not exe:
        return "missing", "plutil not found"
    proc = subprocess.run([exe, "-lint", str(path)], capture_output=True, text=True)
    out = (proc.stdout + proc.stderr).strip().splitlines()
    first = out[0] if out else ""
    return ("ok" if proc.returncode == 0 else "fail"), first


def check_plist(path: Path, state_root: Path) -> dict:
    violations, warnings, notes = [], [], []
    result = {
        "path": str(path), "label": None, "trigger": None, "env_keys": [],
        "violations": violations, "warnings": warnings, "notes": notes,
    }
    if path.suffix != ".plist":
        warnings.append("filename does not end with .plist")

    status, message = run_plutil(path)
    if status == "fail":
        violations.append(f"plutil -lint failed: {message}")
    elif status == "missing":
        notes.append("plutil not available; syntax checked by plistlib only")
    else:
        notes.append("plutil: OK")

    try:
        with path.open("rb") as handle:
            data = plistlib.load(handle)
    except Exception as exc:  # noqa: BLE001 - report any parse failure as a violation
        violations.append(f"does not parse: {exc.__class__.__name__}: {exc}")
        return result
    if not isinstance(data, dict):
        violations.append("top-level object is not a dict")
        return result

    # Label
    label = data.get("Label")
    result["label"] = label if isinstance(label, str) else None
    if not isinstance(label, str) or not label:
        violations.append("Label missing")
    else:
        if not LABEL_RE.match(label):
            violations.append(f"Label has unexpected characters: {label!r}")
        if label != path.stem:
            violations.append(f"Label {label!r} differs from filename stem {path.stem!r}")
        if not label.startswith(LABEL_PREFIX):
            warnings.append(f"Label does not start with {LABEL_PREFIX!r}")

    for key in data:
        if key not in KNOWN_KEYS:
            warnings.append(f"unknown launchd key: {key}")

    # Program / ProgramArguments
    program = data.get("Program")
    args = data.get("ProgramArguments")
    executable = None
    if program is not None:
        if not isinstance(program, str) or not program:
            violations.append("Program is not a non-empty string")
        else:
            executable = program
    if args is None and program is None:
        violations.append("neither ProgramArguments nor Program present")
    elif args is not None:
        if not isinstance(args, list) or not args or not all(isinstance(a, str) for a in args):
            violations.append("ProgramArguments must be a non-empty array of strings")
            args = []
        elif executable is None:
            executable = args[0]
    if executable:
        exe_path = Path(executable)
        if not exe_path.is_absolute():
            violations.append(f"executable is not an absolute path: {executable}")
        elif not exe_path.exists():
            violations.append(f"executable does not exist: {executable}")
        elif not os.access(str(exe_path), os.X_OK):
            violations.append(f"executable is not executable: {executable}")
    rest = args[1:] if isinstance(args, list) and args else []
    for arg in rest:
        if "/" in arg and SECRET_PATH_RE.search(arg):
            warnings.append(
                f"argument references a secrets path (visible in ps/launchctl print); prefer an env var name the program resolves: {arg}"
            )
        if arg.startswith("/") and not Path(arg).exists():
            hint = " (external volume mounted?)" if arg.startswith("/Volumes/") else ""
            warnings.append(f"path argument does not exist{hint}: {arg}")

    # WorkingDirectory
    wd = data.get("WorkingDirectory")
    if wd is not None:
        if not isinstance(wd, str) or not Path(wd).is_absolute():
            violations.append("WorkingDirectory must be an absolute path string")
        elif not Path(wd).is_dir():
            violations.append(f"WorkingDirectory does not exist: {wd}")
    else:
        notes.append("WorkingDirectory not set (defaults to /)")

    # Logs
    log_paths = {}
    for key in ("StandardOutPath", "StandardErrorPath"):
        value = data.get(key)
        if value is None:
            notes.append(f"{key} not set (output discarded)")
            continue
        if not isinstance(value, str) or not Path(value).is_absolute():
            violations.append(f"{key} must be an absolute path string")
            continue
        log_paths[key] = value
        parent = Path(value).parent
        if not parent.is_dir():
            violations.append(f"{key} parent directory does not exist: {parent}")
        try:
            Path(value).relative_to(state_root)
        except ValueError:
            warnings.append(f"{key} is not under the state root {state_root}: {value}")
    if len(log_paths) == 2 and log_paths["StandardOutPath"] == log_paths["StandardErrorPath"]:
        notes.append("stdout and stderr share one file")

    # Triggers
    modes = []
    start_interval = data.get("StartInterval")
    if start_interval is not None:
        if not isinstance(start_interval, int) or isinstance(start_interval, bool) or start_interval <= 0:
            violations.append("StartInterval must be a positive integer")
        else:
            modes.append(f"StartInterval {start_interval}s")
    calendar = data.get("StartCalendarInterval")
    if calendar is not None:
        entries = calendar if isinstance(calendar, list) else [calendar]
        for entry in entries:
            if not isinstance(entry, dict) or not entry:
                violations.append("StartCalendarInterval entries must be non-empty dicts")
                continue
            for ckey, cval in entry.items():
                if ckey not in CALENDAR_KEYS:
                    violations.append(f"StartCalendarInterval unknown key: {ckey}")
                elif not isinstance(cval, int) or isinstance(cval, bool):
                    violations.append(f"StartCalendarInterval.{ckey} must be an integer")
                else:
                    low, high = CALENDAR_RANGES[ckey]
                    if not low <= cval <= high:
                        violations.append(f"StartCalendarInterval.{ckey}={cval} outside {low}..{high}")
        desc = ", ".join(
            "{" + ", ".join(f"{k}={v}" for k, v in e.items()) + "}" for e in entries if isinstance(e, dict)
        )
        modes.append(f"StartCalendarInterval {desc}")
    keepalive = data.get("KeepAlive")
    keepalive_driven = False
    if keepalive is True:
        keepalive_driven = True
        modes.append("KeepAlive always")
    elif isinstance(keepalive, dict):
        unknown = sorted(set(keepalive) - KEEPALIVE_KEYS)
        if unknown:
            warnings.append(f"KeepAlive unknown keys: {unknown}")
        if keepalive:
            keepalive_driven = True
            desc = ", ".join(f"{k}={v}" for k, v in keepalive.items())
            modes.append(f"KeepAlive {{{desc}}}")
    elif keepalive is not None and keepalive is not False:
        violations.append("KeepAlive must be a boolean or a dict")
    run_at_load = data.get("RunAtLoad")
    if run_at_load is not None and not isinstance(run_at_load, bool):
        violations.append("RunAtLoad must be a boolean")
    run_at_load = bool(run_at_load)
    notes.append(f"RunAtLoad={'yes' if run_at_load else 'no'}")
    if not modes:
        if run_at_load:
            notes.append("trigger: RunAtLoad only (runs once at bootstrap; re-run needs kickstart)")
            result["trigger"] = "RunAtLoad only"
        else:
            warnings.append("no trigger (StartInterval / StartCalendarInterval / KeepAlive / RunAtLoad): never runs unless kickstarted")
            result["trigger"] = "none"
    else:
        result["trigger"] = " + ".join(modes)
        if len(modes) > 1:
            warnings.append(f"more than one trigger: {' + '.join(modes)}; keep exactly one")
            if start_interval and keepalive_driven and isinstance(keepalive, dict) and keepalive.get("SuccessfulExit") is False:
                warnings.append(
                    "StartInterval + KeepAlive{SuccessfulExit=false}: exit 0 is relaunched every interval (runs grows ~86400/interval per day), "
                    "non-zero exit restarts after ThrottleInterval; a once-a-night job should use StartCalendarInterval"
                )
        if keepalive_driven and not run_at_load and start_interval is None and calendar is None:
            warnings.append("KeepAlive without RunAtLoad: nothing starts the job until kickstart or a demand event")

    # ThrottleInterval
    throttle = data.get("ThrottleInterval")
    if throttle is None:
        notes.append("ThrottleInterval not set (launchd default 10s)")
    elif not isinstance(throttle, int) or isinstance(throttle, bool) or throttle < 0:
        violations.append("ThrottleInterval must be a non-negative integer")
    elif throttle < 10:
        warnings.append(f"ThrottleInterval {throttle} < 10; launchd raises it to 10")
    else:
        notes.append(f"ThrottleInterval={throttle}")

    # EnvironmentVariables: names only
    env = data.get("EnvironmentVariables")
    if env is not None:
        if not isinstance(env, dict):
            violations.append("EnvironmentVariables must be a dict")
        else:
            keys = sorted(str(k) for k in env)
            result["env_keys"] = keys
            notes.append("env keys: " + (", ".join(keys) if keys else "(none)"))
            for key, value in env.items():
                if not isinstance(value, str):
                    violations.append(f"EnvironmentVariables.{key} must be a string")
                elif looks_like_secret(str(key), value):
                    warnings.append(
                        f"EnvironmentVariables.{key} looks like a secret (value not shown); remove it and let the program read the variable itself"
                    )

    # Misc notes
    if data.get("Disabled") is True:
        warnings.append("Disabled=true: launchd will not load it at login")
    for key in ("LimitLoadToSessionType", "ProcessType"):
        if key in data:
            notes.append(f"{key}={data[key]}")
    if "Umask" in data:
        notes.append(f"Umask={data['Umask']} (0o{int(data['Umask']):o})" if isinstance(data["Umask"], int) else "Umask set")
    return result


def status_of(result: dict) -> str:
    if result["violations"]:
        return "fail"
    if result["warnings"]:
        return "warn"
    return "pass"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Validate kiwoom-autotrader LaunchAgent plists (read-only).")
    parser.add_argument("plist", nargs="+", help="plist file path(s)")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--state-root", default=str(DEFAULT_STATE_ROOT),
                        help="directory logs are expected under (default: %(default)s)")
    args = parser.parse_args(argv)
    state_root = Path(args.state_root).expanduser()
    results = []
    worst = 0
    for raw in args.plist:
        path = Path(raw).expanduser()
        if not path.is_file():
            print(f"error: not a file: {path}", file=sys.stderr)
            return 3
        result = check_plist(path, state_root)
        result["status"] = status_of(result)
        worst = max(worst, {"pass": 0, "warn": 1, "fail": 2}[result["status"]])
        results.append(result)
    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    else:
        for r in results:
            print(f"[{r['status'].upper()}] {r['path']}")
            print(f"  label:     {r['label']}")
            print(f"  trigger:   {r['trigger']}")
            for v in r["violations"]:
                print(f"  violation: {v}")
            for w in r["warnings"]:
                print(f"  warning:   {w}")
            for n in r["notes"]:
                print(f"  note:      {n}")
    return worst


if __name__ == "__main__":
    sys.exit(main())
