"""Deploy the 2026-11-13 maintenance defect-repair package with a single receiver-only claim."""

import argparse
from contextlib import contextmanager
from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch

sys.dont_write_bytecode = True
SOURCE = Path(__file__).resolve().parents[1] / "outputs/ai-stock"
sys.path.insert(0, str(SOURCE))

import deploy_handoff_runtime as handoff
from kiwoom_autotrader.config import settings
from kiwoom_autotrader.telegram.receiver_health import maintenance_status
from kiwoom_autotrader.utils import control_audit, telegram_service as service
from kiwoom_autotrader.utils.runtime_deployment_guard import (
    KST, assert_code_deployment, claim_code_deployment, manifest_sha256,
    verify_deployment_claim,
)


BASELINE_SHA = "a32a4a7c18a9bf9adc03eec367eb2ddcb05749aed47c709e17dbc4d47dd502bf"
TARGET_SHA = "5e13d15d0449cf9d99952ed583ac324c4929e73fa1e983cff0c9c3ad3b64bf5c"
SETTINGS_SHA = "70ada618b8d661345eac4d5be6da6232b831667392ab5de240e5fc14fbacc66b"
ADDED = {"trading/paper_experiment_qls_shadow.py"}
CHANGES = {
    "api/orders.py",
    "trading/broker_observation_repeats.py",
    "trading/execution_log.py",
    "trading/expired_order_certificates.py",
    "trading/paper_experiment_monitor.py",
    "trading/paper_policy.py",
    "trading/paper_reconciliation.py",
    "utils/http_queue.py",
}
FLAGS = {"gdcrs_enabled": False, "ddcrs_enabled": False, "reservations": []}
BASELINE_PROTECTED_CANDIDATES = (2217, 6543, 6544, 6557, 36535)
LEDGER = "runtime/logs/.execution-ledger.sqlite3"
APPROVED_DAY = "20261113"


def clock():
    return datetime.now(KST)


def require(condition, reason):
    if not condition:
        raise handoff.DeploymentError(reason)


def package_copy(source, destination, manifest):
    for name in manifest:
        handoff.private_copy(source / name, destination / name)
    require(handoff.package_manifest(destination) == manifest, "PACKAGE_COPY_HASH_MISMATCH")


def ledger_digest(path):
    path = handoff.regular(path)
    require(path.stat().st_size <= handoff.MAX_BACKUP, "FILE_SIZE_LIMIT")
    result = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            result.update(chunk)
    return result.hexdigest()


def _ps_query(pids):
    if not pids:
        return {}
    result = subprocess.run(
        ["/bin/ps", "-p", ",".join(map(str, sorted(set(pids)))), "-o", "pid=,ppid=,lstart="],
        capture_output=True, text=True, check=False, timeout=5,
    )
    rows = {}
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        pid, ppid, start = line.split(None, 2)
        rows[int(pid)] = {"pid": int(pid), "ppid": int(ppid), "lstart": " ".join(start.split())}
    return rows


def discover_protected_pids(paths, receiver_pid, explicit_pids=None):
    import os
    import re
    if explicit_pids:
        pids = tuple(sorted(set(explicit_pids)))
        rows = _ps_query(pids)
        require(set(rows) == set(pids), "PROTECTED_PID_IDENTITY_MISSING")
        require(receiver_pid not in rows, "RECEIVER_PID_IN_PROTECTED_SET")
        return pids

    required = set()
    candidates = set(BASELINE_PROTECTED_CANDIDATES)
    uid = os.getuid()
    for label, must_run in (
        ("com.local.kiwoom-autotrader.toss-readonly", True),
        ("com.local.kiwoom-autotrader.telegram-account", True),
        ("com.local.kiwoom-autotrader.stability-watchdog", False),
    ):
        res = service._launchctl("print", f"gui/{uid}/{label}", check=False)
        m_pid = re.search(r"^\s*pid = ([0-9]+)\s*$", res.stdout or "", re.MULTILINE)
        m_run = re.search(r"^\s*state = running\s*$", res.stdout or "", re.MULTILINE)
        if res.returncode == 0 and m_pid and m_run:
            pid = int(m_pid.group(1))
            candidates.add(pid)
            if must_run:
                required.add(pid)
        elif must_run:
            raise handoff.DeploymentError(f"REQUIRED_LAUNCHAGENT_NOT_RUNNING:{label}")

    candidates.discard(receiver_pid)
    rows = _ps_query(candidates)
    for pid in list(required):
        require(pid in rows, f"REQUIRED_PROTECTED_DAEMON_NOT_RUNNING:{pid}")
    require(receiver_pid not in rows, "RECEIVER_PID_IN_PROTECTED_SET")
    return tuple(sorted(rows))


def protected_snapshot(pids):
    rows = _ps_query(pids)
    require(set(rows) == set(pids), "PROTECTED_PID_IDENTITY_MISSING")
    return rows


def audit_rows(paths, at):
    _, path = control_audit._location(paths.base, at)
    rows = control_audit._events(handoff.regular(path).read_bytes())
    require(bool(rows), "CONTROL_AUDIT_GENESIS_MISSING")
    before = {row["event"]["action_id"] for row in rows if row["event"]["phase"] == "before"}
    completed = {row["event"]["action_id"] for row in rows if row["event"]["phase"] == "result"}
    require(before == completed, "CONTROL_AUDIT_UNFINISHED_ACTION")
    return rows


def stopped(paths):
    deadline = time.monotonic() + 20
    while True:
        status = service.service_status(paths)
        if status.get("running") is False and status.get("loaded") is False:
            return
        require(time.monotonic() < deadline, "RECEIVER_STILL_LOADED")
        time.sleep(0.2)


def wait_paused(paths, old_pid, now):
    created = datetime.fromisoformat(handoff.read_json(paths.state / "maintenance-quiet.lock")["created_at"])
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        state = handoff.read_json(paths.state / "state.json")
        require(state.get("pid") == old_pid, "OLD_RECEIVER_IDENTITY_CHANGED")
        if state.get("maintenance_status", {}).get("paused") is True:
            published = state.get("state_publication", {}).get("published_at")
            if isinstance(published, str) and created <= datetime.fromisoformat(published) <= now():
                return
        time.sleep(1)
    raise handoff.DeploymentError("OLD_RECEIVER_PAUSE_UNVERIFIED")


def wait_ready(paths, old_pid, started_at, now):
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        try:
            state = handoff.read_json(paths.state / "state.json")
            service_status = service.service_status(paths)
            recording = state.get("execution_recording", {})
            published = datetime.fromisoformat(state["state_publication"]["published_at"])
            if (state.get("pid") != old_pid and type(state.get("pid")) is int
                    and service_status.get("pid") == state["pid"]
                    and service_status.get("running") is True and state.get("status") == "running"
                    and state.get("automation_status", {}).get("mode") == "paper"
                    and recording.get("ready") is True and recording.get("error") is None
                    and recording.get("gap_count") == 0
                    and state.get("maintenance_status", {}).get("paused") is True
                    and started_at <= published <= now()):
                return state
        except (OSError, ValueError, KeyError, TypeError, handoff.DeploymentError):
            pass
        time.sleep(1)
    raise handoff.DeploymentError("NEW_RECEIVER_READY_UNVERIFIED")


def marker_identity(path):
    file = handoff.regular(path)
    st = file.stat()
    return {"path": str(path), "sha256": handoff.digest(path), "st_dev": st.st_dev,
            "st_ino": st.st_ino, "st_mtime_ns": st.st_mtime_ns,
            "device_inode": [st.st_dev, st.st_ino]}


def stop_receiver(paths):
    original = service._stopped_directory
    @contextmanager
    def wait_for_receiver(state, timeout=120):
        require(Path(state) == paths.state, "RECEIVER_STOP_SCOPE_CHANGED")
        with original(state, timeout=360):
            yield
    with patch.object(service, "_stopped_directory", wait_for_receiver):
        service.stop_service(paths)


def install_without_start(source, paths, check_permission):
    allowed_paths = {paths.state.resolve(), (source / "runs/telegram").resolve()}
    @contextmanager
    def existing_lock(state, timeout=120):
        require(Path(state).resolve() in allowed_paths, "INSTALLER_LOCK_SCOPE_CHANGED")
        yield

    def suppress_receiver_action(only_paths=None):
        require(only_paths == paths, "INSTALLER_RECEIVER_SCOPE_CHANGED")

    with (patch.object(service, "_stopped_directory", existing_lock),
          patch.object(service, "stop_service", suppress_receiver_action),
          patch.object(service, "start_service", suppress_receiver_action)):
        service.install_service(source, paths, before_replace=check_permission, before_mutation=check_permission)


def settings_restore_plan(source, paths, expected_sha):
    snapshot = handoff.settings_snapshot(source, paths)
    require(snapshot["sha256"] == expected_sha, "UNEXPECTED_SETTINGS_HASH")
    if not snapshot["active"]:
        return snapshot, None
    path = paths.base / "config/data/settings.json"
    original = handoff.read_json(path)
    serialized = (json.dumps(original, ensure_ascii=False, indent=2) + "\n").encode()
    require(serialized == handoff.regular(path).read_bytes(), "REGISTERED_SETTINGS_RESTORE_NOT_BYTE_EXACT")
    return snapshot, original


def execute(source, approval, backup, *, paths, baseline_sha, target_sha, settings_sha,
            protected_snapshot, expected_protected, now, hold_order_pause=False, temp_root="/private/tmp"):
    """Run once against the supplied scope, releasing order pause before market open when hold_order_pause=False."""
    package = paths.runtime / handoff.PACKAGE
    old_manifest = handoff.package_manifest(package)
    candidate = handoff.package_manifest(source / handoff.PACKAGE)
    require(len(old_manifest) == 189 and len(candidate) == 190, "UNEXPECTED_PACKAGE_FILE_COUNT")
    require(manifest_sha256(old_manifest) == baseline_sha and manifest_sha256(candidate) == target_sha,
            "APPROVED_PACKAGE_HASH_MISMATCH")
    require(set(candidate) - set(old_manifest) == ADDED
            and set(old_manifest) - set(candidate) == set()
            and {key for key in old_manifest if candidate[key] != old_manifest[key]} == CHANGES,
            "APPROVED_PACKAGE_DIFF_MISMATCH")
    before = service.service_status(paths)
    old_pid = before.get("pid")
    old_state = handoff.read_json(paths.state / "state.json")
    require(before.get("loaded") is True and before.get("running") is True
            and type(old_pid) is int and old_state.get("pid") == old_pid and old_state.get("status") == "running"
            and old_state.get("automation_status", {}).get("mode") == "paper"
            and old_state.get("execution_recording", {}).get("ready") is True, "OLD_RECEIVER_NOT_READY")
    require(protected_snapshot() == expected_protected, "PROTECTED_PROCESS_CHANGED")
    initial_audit = audit_rows(paths, now())
    settings_snapshot, restore_updates = settings_restore_plan(source, paths, settings_sha)
    settings_file = paths.base / "config/data/settings.json"
    paused_settings = {**restore_updates, **FLAGS} if restore_updates is not None else None
    paused_settings_sha = hashlib.sha256((json.dumps(
        paused_settings, ensure_ascii=False, indent=2) + "\n").encode()).hexdigest() if paused_settings is not None else None
    ledger = paths.base / LEDGER
    expected_ledger = handoff.db_summary(ledger, pending=True, now=now())
    require(expected_ledger["pending_intents"] == expected_ledger["unfilled_orders"] == 0,
            "UNRESOLVED_ORDERS_BEFORE_CLAIM")
    require(not backup.exists() and not any(p.is_symlink() for p in (backup, *backup.parents)), "BACKUP_PATH_UNSAFE")
    require(shutil.disk_usage(backup.parent).free >= handoff.regular(ledger).stat().st_size + 128 * 1024**2,
            "INSUFFICIENT_BACKUP_SPACE")
    marker = paths.state / "maintenance-quiet.lock"
    stop_request = paths.state / "stop.request"
    require(not any(p.exists() or p.is_symlink() for p in (marker, stop_request)), "RECEIVER_PAUSE_OR_STOP_MARKER_EXISTS")
    claims_dir = paths.base / "code-deployment-claims"
    first_proof = assert_code_deployment(approval, source_manifest=candidate, receiver_pid=old_pid,
        receiver_state_path=paths.state / "state.json", scope="receiver_only", now=now())
    attempt = None
    rechecks_after_stop = 0
    replacement_attempted = False
    settings_changed = False
    after_stop = False
    pause = None
    expiry = None
    new = None
    safe_backup = False

    def verify_pause(minimum_seconds=0):
        require(pause is not None and marker_identity(marker) == pause, "RECEIVER_PAUSE_MARKER_CHANGED")
        require((expiry - now()).total_seconds() > minimum_seconds
                and maintenance_status(paths.state, now=now()).get("paused") is True,
                "RECEIVER_PAUSE_LEASE_TOO_SHORT")

    def permission():
        nonlocal rechecks_after_stop
        proof = assert_code_deployment(approval, source_manifest=handoff.package_manifest(source / handoff.PACKAGE),
            receiver_pid=old_pid, receiver_state_path=paths.state / "state.json", scope="receiver_only", now=now())
        require(proof["approval_sha256"] == first_proof["approval_sha256"], "APPROVAL_CHANGED")
        if attempt is not None:
            verify_deployment_claim(attempt, proof)
        if after_stop:
            rechecks_after_stop += 1
            verify_pause()
        require(protected_snapshot() == expected_protected, "PROTECTED_PROCESS_CHANGED")
        return proof

    def check_replace():
        nonlocal replacement_attempted
        permission()
        replacement_attempted = True

    def restore_settings():
        nonlocal settings_changed
        if settings_changed:
            current = handoff.digest(settings_file)
            require(current in (settings_sha, paused_settings_sha), "SETTINGS_CHANGED_OUTSIDE_DEPLOYMENT")
            if current != settings_sha:
                settings._write_json(restore_updates, settings_file)
            require(handoff.settings_snapshot(source, paths) == settings_snapshot, "SETTINGS_RESTORE_HASH_MISMATCH")
            settings_changed = False

    with handoff.exclusive(paths.base / ".handoff-deploy.lock"), control_audit.control_origin("approved_maintenance"):
        require(service.service_status(paths) == before and handoff.settings_snapshot(source, paths) == settings_snapshot,
                "DEPLOYMENT_PREFLIGHT_DRIFT")
        require(audit_rows(paths, now()) == initial_audit, "CONTROL_AUDIT_PREFLIGHT_DRIFT")
        attempt = claim_code_deployment(claims_dir, permission(), now=now())
        try:
            with control_audit.audit_control("code_deploy", "deployment", root=paths.base,
                    paths=tuple(package / name for name in sorted(candidate))):
                created = now()
                expiry = created + timedelta(seconds=590)
                handoff.private_json(marker, {"created_at": created.isoformat(), "expires_at": expiry.isoformat(),
                    "purpose": "approved_receiver_only_deploy_order_pause", "claim_sha256": attempt["sha256"]})
                pause = marker_identity(marker)
                verify_pause(580)
                wait_paused(paths, old_pid, now)
                verify_pause()
                stop_receiver(paths)
                stopped(paths)
                after_stop = True
                permission()
                with handoff.exclusive(paths.state / "listener.lock"), tempfile.TemporaryDirectory(prefix="maint-install-", dir=temp_root) as tmp:
                    install_source = Path(tmp)
                    package_copy(source / handoff.PACKAGE, install_source / handoff.PACKAGE, candidate)
                    with handoff.exclusive(install_source / "runs/telegram/listener.lock"):
                        backup.mkdir(mode=0o700)
                        package_copy(package, backup / "base/runtime" / handoff.PACKAGE, old_manifest)
                        handoff.private_copy(paths.plist, backup / "bot.plist")
                        handoff.private_copy(settings_file, backup / "base/config/data/settings.json")
                        require(handoff.digest(backup / "base/config/data/settings.json") == settings_sha,
                                "BACKUP_SETTINGS_HASH_MISMATCH")
                        handoff.sqlite_backup(ledger, backup / "base" / LEDGER)
                        original_ledger = handoff.db_summary(backup / "base" / LEDGER, pending=True, now=now())
                        require(original_ledger["pending_intents"] == original_ledger["unfilled_orders"] == 0
                                and original_ledger["tables"]["orders"] == expected_ledger["tables"]["orders"],
                                "UNRESOLVED_OR_CHANGED_ORDERS_AT_BACKUP")
                        safe_backup = True
                        if restore_updates is not None:
                            require(handoff.digest(settings_file) == settings_sha, "SETTINGS_CHANGED_OUTSIDE_DEPLOYMENT")
                            settings_changed = True
                            settings._write_json(paused_settings, settings_file)
                            require(handoff.settings_snapshot(source, paths) == {
                                "active": False, "sha256": paused_settings_sha}, "SETTINGS_PAUSE_FAILED")
                        install_without_start(install_source, paths, check_replace)
                        stopped(paths)
                        require(handoff.package_manifest(package) == candidate, "RUNTIME_PACKAGE_HASH_MISMATCH")
                        require(handoff.db_summary(ledger, pending=True, now=now()) == original_ledger,
                                "LEDGER_CHANGED_DURING_REPLACEMENT")
                permission()
                verify_pause(245 if hold_order_pause else 95)
                started_at = now()
                service.start_service(paths)
                new = wait_ready(paths, old_pid, started_at, now)
                handoff.verify_existing_financial_rows(paths, backup, {LEDGER: original_ledger})
                final_ledger = handoff.db_summary(ledger, pending=True, now=now())
                require(final_ledger["pending_intents"] == final_ledger["unfilled_orders"] == 0
                        and final_ledger["tables"]["orders"] == expected_ledger["tables"]["orders"],
                        "UNRESOLVED_OR_CHANGED_ORDERS_AFTER_START")
                restore_settings()
                require(protected_snapshot() == expected_protected, "PROTECTED_PROCESS_CHANGED")
                verify_pause(160 if hold_order_pause else 0)
            final_audit = audit_rows(paths, now())
            require(final_audit[:len(initial_audit)] == initial_audit, "CONTROL_AUDIT_PREFIX_CHANGED")
            appended = final_audit[len(initial_audit):]
            actions = {("code_deploy", "deployment"), ("stop", "receiver"),
                       ("install", "receiver"), ("start", "receiver")}
            if restore_updates is not None:
                actions.add(("settings_write", "settings"))
            require({(x["event"]["action"], x["event"]["component"]) for x in appended} == actions
                    and all(x["event"].get("origin") == "approved_maintenance"
                        and (x["event"]["phase"] != "result" or x["event"]["status"] == "success") for x in appended),
                    "CONTROL_AUDIT_NEW_EVENTS_UNVERIFIED")
        except Exception as error:
            recovery = "receiver_left_in_observed_state"
            try:
                restore_settings()
                if replacement_attempted and safe_backup:
                    if service.service_status(paths).get("loaded") is True:
                        stop_receiver(paths)
                    stopped(paths)
                    with handoff.exclusive(paths.state / "listener.lock"), control_audit.audit_control(
                            "install", "receiver", root=paths.base, paths=(paths.plist, *tuple(package / key for key in candidate))):
                        with tempfile.TemporaryDirectory(prefix=".maint-rollback-", dir=paths.runtime) as tmp:
                            saved = Path(tmp) / handoff.PACKAGE
                            package_copy(backup / "base/runtime" / handoff.PACKAGE, saved, old_manifest)
                            retained = Path(tmp) / "failed-package"
                            package.rename(retained)
                            try:
                                saved.rename(package)
                            except OSError:
                                retained.rename(package)
                                raise
                        service._private_write(paths.plist, (backup / "bot.plist").read_bytes())
                    recovery = "old_code_and_plist_restored_receiver_left_stopped"
            except Exception:
                recovery = "recovery_incomplete_manual_review_required"
            return {"status": "failed", "reason": str(error) if isinstance(error, handoff.DeploymentError) else type(error).__name__,
                    "recovery": recovery, "claim": attempt, "backup": str(backup) if backup.exists() else None,
                    "order_pause": pause, "data_files_restored": False, "receiver_stop_confirmed": after_stop}
        if not hold_order_pause:
            verify_pause()
            marker.unlink()
        held_order_pause = {**pause, "expires_at": expiry.isoformat(), "held_for_watchdog": hold_order_pause,
                            "released": not hold_order_pause}
        identities_before = [f"{row['pid']} {row['ppid']} {row['lstart']}" for _, row in sorted(expected_protected.items())]
        return {"status": "deployed", "scope": "receiver_only", "old_pid": old_pid,
                "new_pid": new["pid"], "new_receiver_pid": new["pid"],
                "new_state_published_at": new["state_publication"]["published_at"],
                "candidate_package_sha256": target_sha, "source_runtime_match": True,
                "runtime_before_package_sha256": baseline_sha, "backup": str(backup), "claim": attempt,
                "backup_assets": {"package_sha256": baseline_sha, "plist_sha256": handoff.digest(backup / "bot.plist"),
                    "settings_sha256": settings_sha, "ledger_sha256": ledger_digest(backup / "base" / LEDGER)},
                "orders_before": expected_ledger["tables"]["orders"], "orders_after": final_ledger["tables"]["orders"],
                "pending_intents": final_ledger["pending_intents"], "unfilled_orders": final_ledger["unfilled_orders"],
                "settings_sha256": settings_sha, "protected_processes": expected_protected,
                "protected_processes_before": identities_before,
                "permission_rechecks_after_stop": rechecks_after_stop,
                "order_pause": held_order_pause, "held_order_pause": held_order_pause,
                "audit": {"before": initial_audit[-1]["event_sha256"], "after": final_audit[-1]["event_sha256"],
                          "before_sequence": initial_audit[-1]["event"]["sequence"],
                          "after_sequence": final_audit[-1]["event"]["sequence"]}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--deploy", required=True, action="store_true")
    parser.add_argument("--code-approval", required=True, type=Path)
    parser.add_argument("--backup", required=True, type=Path)
    parser.add_argument("--hold-order-pause", action="store_true", default=False)
    parser.add_argument("--protected-pids", type=str, default="",
                        help="Optional comma-separated PIDs; defaults to live toss+collector+watchdog discovery.")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    paths = service.service_paths()
    try:
        require(clock().strftime("%Y%m%d") == APPROVED_DAY and args.backup.is_absolute()
                and args.backup.parent == paths.base / "backups", "LIVE_DEPLOYMENT_TARGET_OR_DATE_INVALID")
        receiver_plist = plistlib.loads(handoff.regular(paths.plist).read_bytes())
        require(receiver_plist.get("Label") == service.LABEL and sys.executable ==
                receiver_plist.get("ProgramArguments", [None])[0], "LIVE_RECEIVER_INTERPRETER_UNVERIFIED")
        live_status = service.service_status(paths)
        old_pid = live_status.get("pid")
        require(type(old_pid) is int and old_pid > 1, "OLD_RECEIVER_NOT_READY")
        explicit = [int(x.strip()) for x in args.protected_pids.split(",") if x.strip()] if args.protected_pids else None
        locked_pids = discover_protected_pids(paths, old_pid, explicit_pids=explicit)
        snap_fn = lambda: protected_snapshot(locked_pids)
        result = execute(SOURCE, args.code_approval, args.backup, paths=paths, baseline_sha=BASELINE_SHA,
            target_sha=TARGET_SHA, settings_sha=SETTINGS_SHA, protected_snapshot=snap_fn,
            expected_protected=snap_fn(), now=clock, hold_order_pause=args.hold_order_pause)
    except Exception as error:
        result = {"status": "failed", "reason": str(error) if isinstance(error, handoff.DeploymentError) else type(error).__name__}
    if args.output is not None:
        handoff.private_json(args.output, result)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] == "deployed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
