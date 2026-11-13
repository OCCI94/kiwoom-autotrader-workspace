#!/usr/bin/env python3
"""Summarize the launchd state of kiwoom-autotrader LaunchAgents (read-only).

Runs `launchctl print gui/<uid>/<label>` per label and reads the matching plist
in ~/Library/LaunchAgents for the trigger type. Reports state, pid, last exit
code, runs and program path. Skips every nested block of the launchctl output
(environment, semaphores, coalitions), so environment values are never echoed.

Never bootstraps, boots out, kickstarts, enables or disables anything.

Exit codes: 0 every label is running or idle as expected, 1 any label not
loaded or with a non-zero last exit code, 3 usage/launchctl error.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
from pathlib import Path

LABEL_PREFIX = "com.local.kiwoom-autotrader."
LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
DEFAULT_LAUNCH_AGENTS = Path.home() / "Library" / "LaunchAgents"
TOP_LEVEL_KEYS = {
    "active count", "path", "type", "state", "program", "working directory", "stdout path", "stderr path",
    "runs", "pid", "last exit code", "last exit reason", "run interval", "properties", "domain", "job state",
    "forks", "execs", "minimum runtime", "exit timeout", "spawn type",
}
LIST_LINE_RE = re.compile(r"^\s*(\S+)\s+(-?\d+)\s+(\S+)\s*$")


def launchctl_print(launchctl: str, uid: int, label: str) -> dict:
    """Parse the top-level `key = value` lines of `launchctl print gui/<uid>/<label>`."""
    proc = subprocess.run([launchctl, "print", f"gui/{uid}/{label}"], capture_output=True, text=True)
    info = {"loaded": None, "fields": {}, "error": None}
    combined = proc.stdout + proc.stderr
    if proc.returncode != 0:
        if "Could not find service" in combined or proc.returncode == 113:
            info["loaded"] = False
        else:
            info["loaded"] = None
            first = combined.strip().splitlines()[0] if combined.strip() else f"launchctl exit {proc.returncode}"
            info["error"] = first
        return info
    info["loaded"] = True
    depth = 0
    for line in proc.stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.endswith("{"):
            depth += 1
            continue
        if stripped == "}":
            depth = max(0, depth - 1)
            continue
        if depth != 1 or " = " not in stripped:
            continue  # nested blocks (environment, semaphores, coalitions) are never read
        key, _, value = stripped.partition(" = ")
        key = key.strip()
        if key in TOP_LEVEL_KEYS:
            info["fields"][key] = value.strip()
    return info


def to_int(value):
    if value is None:
        return None
    match = re.match(r"^-?\d+", value)
    return int(match.group(0)) if match else None


def read_plist_trigger(path: Path) -> dict:
    """Trigger summary from the plist file. Never touches EnvironmentVariables values."""
    out = {"plist_present": path.is_file(), "trigger": None, "run_at_load": None, "throttle": None, "script": None}
    if not out["plist_present"]:
        return out
    try:
        with path.open("rb") as handle:
            data = plistlib.load(handle)
    except Exception as exc:  # noqa: BLE001
        out["trigger"] = f"unreadable plist ({exc.__class__.__name__})"
        return out
    if not isinstance(data, dict):
        out["trigger"] = "unreadable plist"
        return out
    modes = []
    if isinstance(data.get("StartInterval"), int) and not isinstance(data.get("StartInterval"), bool):
        modes.append(f"StartInterval {data['StartInterval']}s")
    cal = data.get("StartCalendarInterval")
    if cal is not None:
        entries = cal if isinstance(cal, list) else [cal]
        desc = ", ".join(
            "{" + ", ".join(f"{k}={v}" for k, v in e.items()) + "}" for e in entries if isinstance(e, dict)
        )
        modes.append(f"StartCalendarInterval {desc}")
    ka = data.get("KeepAlive")
    if ka is True:
        modes.append("KeepAlive always")
    elif isinstance(ka, dict) and ka:
        modes.append("KeepAlive{" + ", ".join(f"{k}={v}" for k, v in ka.items()) + "}")
    out["run_at_load"] = bool(data.get("RunAtLoad"))
    out["throttle"] = data.get("ThrottleInterval")
    out["trigger"] = " + ".join(modes) if modes else ("RunAtLoad only" if out["run_at_load"] else "none")
    args = data.get("ProgramArguments")
    if isinstance(args, list):
        for i, arg in enumerate(args[1:], start=1):
            if isinstance(arg, str) and arg.endswith(".py"):
                out["script"] = arg
                break
            if arg == "-m" and i + 1 < len(args):
                out["script"] = f"-m {args[i + 1]}" + (f" {args[i + 2]}" if i + 2 < len(args) else "")
                break
    return out


def discover_labels(launchctl: str, prefix: str, agents_dir: Path) -> list:
    labels = set()
    if agents_dir.is_dir():
        for path in agents_dir.glob("*.plist"):
            if path.stem.startswith(prefix):
                labels.add(path.stem)
    proc = subprocess.run([launchctl, "list"], capture_output=True, text=True)
    if proc.returncode == 0:
        for line in proc.stdout.splitlines():
            match = LIST_LINE_RE.match(line)
            if match and match.group(3).startswith(prefix):
                labels.add(match.group(3))
    return sorted(labels)


def classify(entry: dict) -> tuple:
    """Return (verdict, ok)."""
    if entry["loaded"] is None:
        return "error", False
    if entry["loaded"] is False:
        note = "plist present, not bootstrapped" if entry["plist_present"] else "no plist, not loaded"
        return f"not loaded ({note})", False
    state = entry.get("state") or "?"
    last_exit = entry.get("last_exit_code")
    trigger = entry.get("trigger") or ""
    if state == "running":
        if last_exit not in (None, 0):
            how = "restarted by KeepAlive" if trigger.startswith("KeepAlive") else "relaunched"
            return f"running, previous run exited {last_exit} ({how}; check the log)", False
        return "running", True
    if last_exit not in (None, 0):
        return f"exited {last_exit}", False
    if trigger.startswith("KeepAlive"):
        return "not running (KeepAlive job idle: exited 0, not restarted)", True
    return "idle", True


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Read-only launchd status for kiwoom-autotrader LaunchAgents.")
    parser.add_argument("labels", nargs="*", help="exact service labels")
    parser.add_argument("--all-prefix", metavar="PREFIX", help=f"every label starting with PREFIX, e.g. {LABEL_PREFIX}")
    parser.add_argument("--uid", type=int, default=os.getuid(), help="gui domain uid (default: current user)")
    parser.add_argument("--launch-agents-dir", default=str(DEFAULT_LAUNCH_AGENTS), help="default: %(default)s")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--verbose", action="store_true", help="print program/log details for every label")
    args = parser.parse_args(argv)

    launchctl = shutil.which("launchctl")
    if not launchctl:
        print("error: launchctl not found (this script is for macOS)", file=sys.stderr)
        return 3
    agents_dir = Path(args.launch_agents_dir).expanduser()
    labels = list(args.labels)
    if args.all_prefix:
        labels.extend(discover_labels(launchctl, args.all_prefix, agents_dir))
    labels = sorted(set(labels))
    if not labels:
        print("error: no labels given or discovered; pass labels or --all-prefix", file=sys.stderr)
        return 3
    for label in labels:
        if not LABEL_RE.match(label) or "*" in label:
            print(f"error: not an exact label: {label!r}", file=sys.stderr)
            return 3

    observed_at = _dt.datetime.now().astimezone().isoformat(timespec="seconds")
    entries = []
    worst = 0
    for label in labels:
        info = launchctl_print(launchctl, args.uid, label)
        fields = info["fields"]
        plist_info = read_plist_trigger(agents_dir / f"{label}.plist")
        entry = {
            "label": label,
            "loaded": info["loaded"],
            "state": fields.get("state"),
            "pid": to_int(fields.get("pid")),
            "last_exit_code": to_int(fields.get("last exit code")),
            "last_exit_raw": fields.get("last exit code"),
            "runs": to_int(fields.get("runs")),
            "program": fields.get("program"),
            "plist_path": fields.get("path") or (str(agents_dir / f"{label}.plist") if plist_info["plist_present"] else None),
            "working_directory": fields.get("working directory"),
            "stdout_path": fields.get("stdout path"),
            "stderr_path": fields.get("stderr path"),
            "run_interval": fields.get("run interval"),
            "properties": fields.get("properties"),
            "error": info["error"],
        }
        entry.update(plist_info)
        verdict, ok = classify(entry)
        entry["verdict"] = verdict
        entry["ok"] = ok
        if info["loaded"] is None:
            worst = 3
        elif not ok:
            worst = max(worst, 1)
        entries.append(entry)

    report = {"observed_at": observed_at, "domain": f"gui/{args.uid}", "entries": entries,
              "summary": {"total": len(entries), "ok": sum(1 for e in entries if e["ok"]),
                          "problems": sum(1 for e in entries if not e["ok"])}}
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return worst

    print(f"observed_at: {observed_at}   domain: gui/{args.uid}")
    width = max(len(e["label"]) for e in entries)
    header = f"{'label':<{width}}  {'state':<12} {'pid':>6}  {'last_exit':<9} {'runs':>5}  trigger"
    print(header)
    print("-" * len(header))
    for e in entries:
        state = e["state"] or ("not loaded" if e["loaded"] is False else "error")
        pid = str(e["pid"]) if e["pid"] is not None else "-"
        last_exit = "never" if e["last_exit_raw"] == "(never exited)" else (
            str(e["last_exit_code"]) if e["last_exit_code"] is not None else "-")
        runs = str(e["runs"]) if e["runs"] is not None else "-"
        print(f"{e['label']:<{width}}  {state:<12} {pid:>6}  {last_exit:<9} {runs:>5}  {e['trigger'] or '-'}")
    print()
    for e in entries:
        flag = "OK     " if e["ok"] else "PROBLEM"
        print(f"{flag} {e['label']}: {e['verdict']}")
        if args.verbose or not e["ok"]:
            if e["error"]:
                print(f"        launchctl: {e['error']}")
            if e["program"]:
                print(f"        program: {e['program']}")
            if e["script"]:
                print(f"        script:  {e['script']}")
            if e["plist_path"]:
                print(f"        plist:   {e['plist_path']}")
            if e["stdout_path"]:
                print(f"        stdout:  {e['stdout_path']}")
            if e["stderr_path"] and e["stderr_path"] != e["stdout_path"]:
                print(f"        stderr:  {e['stderr_path']}")
            if e["run_interval"]:
                print(f"        run interval: {e['run_interval']}")
            if e["throttle"] is not None:
                print(f"        ThrottleInterval: {e['throttle']}  RunAtLoad: {e['run_at_load']}")
    print()
    print(f"summary: {report['summary']['ok']} ok, {report['summary']['problems']} problem(s) of {report['summary']['total']}")
    return worst


if __name__ == "__main__":
    sys.exit(main())
