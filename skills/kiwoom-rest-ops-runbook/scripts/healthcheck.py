#!/usr/bin/env python3
"""Read-only health check for the Kiwoom REST paper-trading bot.

Inspects launchd status, state.json, listener.log token expiry, ledger and log
freshness, settings/runtime mtimes, helper workers and disk space. No network,
no broker calls, no token issuance, no secrets read. Python 3.9+, stdlib only.

Exit codes: 0 all ok, 1 warnings, 2 failures, 3 runtime error.
"""
from __future__ import annotations

import argparse
import datetime as dt
import glob
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")
DEFAULT_STATE_DIR = Path.home() / "Library" / "Application Support" / "KiwoomAutotrader"
RECEIVER_LABEL = "com.local.kiwoom-autotrader.telegram"
WORKER_LABELS = [
    "com.local.kiwoom-autotrader.telegram-account",
    "com.local.kiwoom-autotrader.telegram-candidates",
    "com.local.kiwoom-autotrader.toss-readonly",
    "com.local.kiwoom-autotrader.stability-watchdog",
    "com.local.kiwoom-autotrader.telegram-nightly",
]
EXPECTED_ARMS = 14
SECRET_DIR_NAMES = {"secrets"}


class Report:
    def __init__(self):
        self.checks = []

    def add(self, name, status, detail, **data):
        entry = {"check": name, "status": status, "detail": detail}
        entry.update(data)
        self.checks.append(entry)

    def worst(self):
        order = {"ok": 0, "warn": 1, "fail": 2}
        return max((order[c["status"]] for c in self.checks), default=0)


def now_kst():
    return dt.datetime.now(KST)


def session_state(at, holidays):
    weekday = at.weekday() < 5
    trading_day = weekday and at.strftime("%Y-%m-%d") not in holidays
    hm = at.hour * 60 + at.minute
    in_session = trading_day and 9 * 60 <= hm < 15 * 60 + 30
    return trading_day, in_session


def launchctl_print(label):
    """Read-only query. Returns dict with state/pid/last_exit/runs or error."""
    uid = os.getuid()
    try:
        proc = subprocess.run(["launchctl", "print", f"gui/{uid}/{label}"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as error:
        return {"error": str(error)}
    if proc.returncode != 0:
        return {"error": f"not loaded (rc={proc.returncode})"}
    info = {}
    for key, pattern in (("state", r"^\s*state = (.+)$"), ("pid", r"^\s*pid = (\d+)"), ("last_exit", r"^\s*last exit code = (.+)$"), ("runs", r"^\s*runs = (\d+)")):
        match = re.search(pattern, proc.stdout, re.M)
        if match:
            info[key] = match.group(1).strip()
    return info


def pids_for(pattern):
    try:
        proc = subprocess.run(["ps", "-axo", "pid=,lstart=,command="], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return []
    found = []
    for line in proc.stdout.splitlines():
        if pattern in line and "grep" not in line:
            parts = line.strip().split(None, 1)
            found.append({"pid": int(parts[0]), "line": parts[1][:140]})
    return found


def age_minutes(path, at):
    try:
        mtime = dt.datetime.fromtimestamp(path.stat().st_mtime, KST)
    except OSError:
        return None, None
    return mtime, (at - mtime).total_seconds() / 60


def parse_iso(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = dt.datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=KST)
    return parsed


def check_receiver(report, state_dir, label, at, in_session):
    info = launchctl_print(label)
    procs = pids_for("kiwoom_autotrader telegram-listen")
    if "error" in info:
        report.add("receiver.launchd", "fail", f"{label}: {info['error']}")
    else:
        status = "ok" if info.get("state") == "running" else "fail"
        report.add("receiver.launchd", status, f"state={info.get('state')} pid={info.get('pid')} last_exit={info.get('last_exit')}", launchd=info)
    if len(procs) == 1:
        report.add("receiver.single_instance", "ok", f"one telegram-listen process (pid {procs[0]['pid']})")
    elif not procs:
        report.add("receiver.single_instance", "fail", "no telegram-listen process found")
    else:
        report.add("receiver.single_instance", "fail", f"{len(procs)} telegram-listen processes: {[p['pid'] for p in procs]}")
    state_path = state_dir / "telegram" / "state.json"
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        report.add("state.json", "fail", f"cannot read {state_path}: {error}")
        return None
    launchd_pid = info.get("pid") if "error" not in info else None
    if launchd_pid and str(state.get("pid")) != str(launchd_pid):
        report.add("state.pid", "warn", f"state.json pid {state.get('pid')} != launchd pid {launchd_pid}")
    else:
        report.add("state.pid", "ok", f"pid {state.get('pid')} status={state.get('status')} started_at={state.get('started_at')}")
    rec = state.get("execution_recording") or {}
    last_obs = parse_iso(rec.get("last_observed_at"))
    obs_age = (at - last_obs).total_seconds() / 60 if last_obs else None
    stream_status = "ok"
    if rec.get("error") or rec.get("ready") is not True or rec.get("active") is not True:
        stream_status = "fail"
    elif in_session and (obs_age is None or obs_age > 10):
        stream_status = "warn"
    report.add("stream", stream_status, f"ready={rec.get('ready')} active={rec.get('active')} error={rec.get('error')} gap_count={rec.get('gap_count')} last_observed={rec.get('last_observed_at')} ({None if obs_age is None else round(obs_age)} min ago)")
    auto = state.get("automation_status") or {}
    pe = auto.get("paper_experiment") or {}
    monitor = pe.get("monitor") or {}
    arms = monitor.get("active_arms") or []
    pe_status = "ok"
    if pe.get("enabled") is not True or pe.get("failure"):
        pe_status = "fail"
    elif len(arms) != EXPECTED_ARMS:
        pe_status = "warn"
    report.add("paper_experiment", pe_status, f"enabled={pe.get('enabled')} failure={pe.get('failure')} arms={len(arms)} reason={monitor.get('reason')} next_check={monitor.get('next_check_at')}", active_arms=arms)
    recovery = pe.get("recovery") or {}
    pending = recovery.get("remaining_registered_pending")
    report.add("recovery", "warn" if pending else "ok", f"status={recovery.get('status')} remaining_registered_pending={pending} observed_at={recovery.get('observed_at')}")
    ttl = pe.get("ttl") or {}
    ttl_status = "ok" if ttl.get("status") in (None, "no_due_owned_limit_buy", "complete", "idle") else "warn"
    if ttl.get("status") == "settlement_unconfirmed":
        ttl_status = "warn"
    report.add("ttl", ttl_status, f"status={ttl.get('status')} reason={ttl.get('reason')} released={ttl.get('principal_reservation_released')} order={ttl.get('original_order_number')} retry_at={ttl.get('retry_at')}")
    prior = pe.get("prior_day_reconciliation") or {}
    report.add("prior_day_reconciliation", "ok", f"status={prior.get('status')} orders_sent={prior.get('orders_sent')}")
    return state


def check_token(report, state_dir, at, trading_day):
    log_path = state_dir / "telegram" / "listener.log"
    try:
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as error:
        report.add("token", "warn", f"cannot read listener.log: {error}")
        return
    expiry = None
    for line in reversed(lines):
        match = re.search(r"만료 시각\(KST\): (\d{14})", line)
        if match:
            expiry = dt.datetime.strptime(match.group(1), "%Y%m%d%H%M%S").replace(tzinfo=KST)
            break
    if expiry is None:
        report.add("token", "warn", "no login/expiry line found in listener.log")
        return
    hours = (expiry - at).total_seconds() / 3600
    status = "ok"
    if hours <= 0:
        status = "fail"
    elif trading_day and hours < 2 and at.hour < 9:
        status = "warn"
    report.add("token", status, f"expires {expiry.strftime('%Y-%m-%d %H:%M')} KST ({hours:.1f} h left); bot refreshes daily 08:55", expires_at=expiry.isoformat())
    stops = [l for l in lines if "수신을 중지" in l]
    starts = [l for l in lines if "수신을 시작" in l]
    report.add("listener.log", "ok", f"{len(lines)} lines, starts={len(starts)} stops={len(stops)}, last={lines[-1][:120] if lines else ''}")


def check_ledger(report, state_dir, at, day, in_session):
    logs = state_dir / "runtime" / "logs"
    ledger = logs / ".execution-ledger.sqlite3"
    mtime, age = age_minutes(ledger, at)
    if mtime is None:
        report.add("ledger.file", "fail", f"missing {ledger}")
    else:
        size_mb = ledger.stat().st_size / 1e6
        status = "warn" if (in_session and age > 10) else "ok"
        report.add("ledger.file", status, f"{size_mb:.0f} MB, mtime {mtime.strftime('%H:%M:%S')} ({age:.0f} min ago)")
    for name in (f"{day}.broker-observations.jsonl", f"{day}.execution-history.jsonl", f"strategy-signal-events-{day}.jsonl"):
        path = logs / name
        mtime, age = age_minutes(path, at)
        if mtime is None:
            report.add(f"jsonl.{name}", "warn" if in_session else "ok", "absent")
        else:
            report.add(f"jsonl.{name}", "ok", f"{path.stat().st_size / 1e3:.0f} KB, mtime {mtime.strftime('%H:%M:%S')}")
    rates = []
    for path in sorted(state_dir.glob("http-rate.json.paper-*")):
        mtime, age = age_minutes(path, at)
        rates.append((path.name.split("paper-")[-1], mtime.strftime("%H:%M") if mtime else None))
    report.add("http_rate_last_calls", "ok", ", ".join(f"{tr}@{t}" for tr, t in rates) or "none", calls=rates)


def check_settings(report, state_dir, at):
    for rel in ("config/data/settings.json", "config/data/paper-strategy-options.json", "runtime/kiwoom_autotrader"):
        path = state_dir / rel
        mtime, _ = age_minutes(path, at)
        report.add(f"mtime.{rel}", "ok" if mtime else "warn", mtime.strftime("%Y-%m-%d %H:%M:%S") if mtime else "missing")
    secrets = state_dir / "config" / "secrets"
    if secrets.exists():
        mode = oct(secrets.stat().st_mode & 0o777)
        report.add("secrets.permissions", "ok" if mode == "0o700" else "warn", f"{secrets} mode {mode} (values never read)")
    locks = [p.name for p in state_dir.glob("*.lock")] + [p.name for p in (state_dir / "telegram").glob("*.lock")]
    report.add("locks", "ok", ", ".join(locks) or "none")


def check_workers(report, state_dir, at):
    for label in WORKER_LABELS:
        info = launchctl_print(label)
        if "error" in info:
            report.add(f"worker.{label.rsplit('.', 1)[-1]}", "warn", info["error"])
            continue
        exit_code = info.get("last_exit", "")
        status = "ok"
        if info.get("state") not in ("running", "not running") or (exit_code not in ("", "0", "(never exited)")):
            status = "warn"
        report.add(f"worker.{label.rsplit('.', 1)[-1]}", status, f"state={info.get('state')} pid={info.get('pid')} runs={info.get('runs')} last_exit={exit_code}")
    status_path = state_dir / "telegram-account" / "collector-status.json"
    mtime, age = age_minutes(status_path, at)
    if mtime:
        try:
            data = json.loads(status_path.read_text(encoding="utf-8"))
            keys = {k: data[k] for k in ("status", "pid", "last_error", "channels", "updated_at") if k in data}
        except (OSError, ValueError):
            keys = {}
        report.add("collector.status", "ok" if age < 30 else "warn", f"updated {age:.0f} min ago {json.dumps(keys, ensure_ascii=False)[:200]}")
    results = sorted(glob.glob(str(state_dir / "research-readonly" / "toss-evidence-refresh-claims" / "*.result.json")))
    if results:
        latest = Path(results[-1])
        try:
            data = json.loads(latest.read_text(encoding="utf-8"))
            summary = {k: data[k] for k in ("status", "reason", "error") if k in data}
        except (OSError, ValueError):
            summary = {}
        report.add("toss.latest_slot", "ok", f"{latest.name} {json.dumps(summary, ensure_ascii=False)[:160]}")
    watch = sorted((state_dir / "observability" / "watchdog").glob("*.notification.json"))
    if watch:
        report.add("watchdog.notifications", "warn", f"{len(watch)} notification files, latest {watch[-1].name}")
    samples = sorted((state_dir / "observability" / "watchdog").glob("*.sample.json"))
    if samples:
        mtime, age = age_minutes(samples[-1], at)
        report.add("watchdog.samples", "ok" if age < 30 else "warn", f"latest {samples[-1].name} ({age:.0f} min ago)")


def check_disk(report, state_dir):
    for path in (state_dir, Path("/Volumes/AIStockData"), Path("/Volumes/T9")):
        if not path.exists():
            continue
        usage = shutil.disk_usage(path)
        free_gb = usage.free / 1e9
        report.add(f"disk.{path.name or 'root'}", "ok" if free_gb > 10 else ("warn" if free_gb > 3 else "fail"), f"{free_gb:.1f} GB free of {usage.total / 1e9:.0f} GB")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Read-only health check for the Kiwoom paper-trading bot.")
    parser.add_argument("--state-dir", default=str(DEFAULT_STATE_DIR))
    parser.add_argument("--label", default=RECEIVER_LABEL)
    parser.add_argument("--day", help="YYYYMMDD for JSONL freshness (default today KST)")
    parser.add_argument("--holidays", help="JSON array of YYYY-MM-DD market holidays")
    parser.add_argument("--format", choices=["json", "table"], default="json")
    args = parser.parse_args(argv)
    state_dir = Path(args.state_dir).expanduser()
    if not state_dir.is_dir():
        print(f"error: state dir not found: {state_dir}", file=sys.stderr)
        return 3
    at = now_kst()
    day = args.day or at.strftime("%Y%m%d")
    holidays = set()
    if args.holidays:
        try:
            holidays = set(json.loads(Path(args.holidays).read_text(encoding="utf-8")))
        except (OSError, ValueError) as error:
            print(f"error: cannot read holidays: {error}", file=sys.stderr)
            return 3
    else:
        print("note: holidays not checked (--holidays not given)", file=sys.stderr)
    trading_day, in_session = session_state(at, holidays)
    report = Report()
    try:
        check_receiver(report, state_dir, args.label, at, in_session)
        check_token(report, state_dir, at, trading_day)
        check_ledger(report, state_dir, at, day, in_session)
        check_settings(report, state_dir, at)
        check_workers(report, state_dir, at)
        check_disk(report, state_dir)
    except Exception as error:  # noqa: BLE001 - report instead of crashing
        print(f"error: {type(error).__name__}: {error}", file=sys.stderr)
        return 3
    worst = report.worst()
    result = {
        "checked_at": at.isoformat(),
        "trading_day": trading_day,
        "in_session": in_session,
        "overall": ["ok", "warn", "fail"][worst],
        "checks": report.checks,
    }
    if args.format == "json":
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"checked_at={result['checked_at']} trading_day={trading_day} in_session={in_session} overall={result['overall']}")
        for c in report.checks:
            print(f"[{c['status']:4}] {c['check']:36} {c['detail']}")
    return worst


if __name__ == "__main__":
    sys.exit(main())
