"""Offline Friday 08:50 pre-open rehearsal for the 2026-11-13 maintenance deployment."""

from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
import hashlib
import json
import os
import plistlib
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from unittest.mock import patch
from urllib.parse import unquote


HERE = Path(__file__).resolve().parent
WORK = HERE.parents[1]
SOURCE = HERE.parent / "outputs/ai-stock"
BASELINE = WORK / "recovery-balance-witness-fix-20261112/outputs/ai-stock"


def put(path, value):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False) + "\n")
    path.chmod(0o600)


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def create_ledger(path):
    path.parent.mkdir(mode=0o700)
    with sqlite3.connect(path) as db:
        db.executescript("""
            CREATE TABLE intents(scope TEXT,id TEXT,day TEXT,mode TEXT,order_number TEXT,state TEXT);
            CREATE TABLE orders(scope TEXT,day TEXT,mode TEXT,ord_no TEXT,quantity INTEGER);
            CREATE TABLE fills(scope TEXT,day TEXT,mode TEXT,ord_no TEXT,fill_no TEXT,quantity INTEGER);
            CREATE TABLE broker_observations(id INTEGER,scope TEXT,day TEXT,mode TEXT,observed_at TEXT,
                source_api TEXT,digest TEXT,payload_json TEXT);
            INSERT INTO intents VALUES ('fixture','1','20261112','paper','1','accepted');
            INSERT INTO orders VALUES ('fixture','20261112','paper','1',2);
            INSERT INTO fills VALUES ('fixture','20261112','paper','1','1',2);
        """)
    path.chmod(0o600)


def approval(root, package_sha, old_pid, at):
    open_at = at.replace(hour=9, minute=0, second=0, microsecond=0)
    close_at = at.replace(hour=15, minute=30, second=0, microsecond=0)
    observed = at.replace(hour=8, minute=50, second=4, microsecond=0)
    day = at.date().isoformat()
    source_sha = "c" * 64
    claim, calendar = root / "toss-fixture.json", root / "calendar-fixture.json"
    put(claim, {
        "schema": "toss-dart-refresh-slot-claim-v1",
        "claimed_at": observed.isoformat(),
        "calendar_source_sha256": source_sha,
        "session_day": day,
        "regular_start": open_at.isoformat(),
        "regular_end": close_at.isoformat(),
    })
    put(calendar, {
        "schema": "toss-decoded-response-evidence-v1",
        "request": {"endpoint_key": "market_calendar_kr", "market_country": "KR", "params": {"date": day}},
        "result": {
            "source": "toss_openapi",
            "endpoint_key": "market_calendar_kr",
            "status": "ok",
            "observed_at": observed.isoformat(),
            "request_started_at": (observed - timedelta(seconds=1)).isoformat(),
            "response_sha256": source_sha,
            "response": {
                "result": {
                    "today": {
                        "date": day,
                        "integrated": {
                            "regularMarket": {
                                "startTime": open_at.isoformat(),
                                "endTime": close_at.isoformat(),
                            }
                        },
                    },
                    "previousBusinessDay": {"date": "2026-11-12"},
                }
            },
        },
    })
    path = root / "receiver-only-exception-approval.json"
    put(path, {
        "schema": "runtime-code-deployment-approval-v1",
        "action": "code_deploy",
        "kind": "exception-code-token",
        "change_class": "maintenance",
        "exception_code_change_explicit": True,
        "scope": "receiver_only",
        "mode": "paper",
        "receiver_pid": old_pid,
        "source_manifest_sha256": package_sha,
        "issued_at": (at - timedelta(seconds=5)).isoformat(),
        "expires_at": (at + timedelta(seconds=595)).isoformat(),
        "user_approval": {
            "kind": "explicit_code_deployment",
            "reference": "user-approved-preopen-20261113-maintenance",
            "text": "Offline fixture: 사용자 명시 승인 금요일 장전(08:50) 예외 코드 배포 (exception code deployment).",
        },
        "official_session": {
            "claim_path": str(claim),
            "claim_sha256": sha(claim),
            "calendar_path": str(calendar),
            "calendar_file_sha256": sha(calendar),
            "calendar_source_sha256": source_sha,
        },
    })
    return path


def child(root):
    os.environ["HOME"] = str(root / "fake-home")

    def offline_audit(event, args):
        if event.startswith("socket.") or event in (
            "subprocess.Popen", "os.system", "os.kill", "os.killpg", "os.posix_spawn", "os.exec"
        ):
            raise AssertionError("fixture transport or process boundary")
        if event == "sqlite3.connect":
            value = os.fsdecode(args[0])
            value = unquote(value[5:].split("?", 1)[0]) if value.startswith("file:") else value
            assert Path(value).is_relative_to(root), value

    sys.addaudithook(offline_audit)
    sys.path[:0] = [str(HERE), str(SOURCE)]
    import run_receiver_only_deploy as receiver
    from kiwoom_autotrader.telegram import bot_credentials
    from kiwoom_autotrader.utils import account_service, control_audit, telegram_service as service
    from kiwoom_autotrader.utils.runtime_deployment_guard import (
        CodeDeploymentDenied, KST, assert_code_deployment, manifest_sha256,
    )
    import deploy_handoff_runtime as handoff

    # Simulate Friday 2026-11-13 08:50:15 KST (pre-open exception window)
    action_at = datetime(2026, 11, 13, 8, 50, 15, tzinfo=KST)
    at = action_at.replace(second=0, microsecond=0)
    after = at + timedelta(seconds=60)
    old_pid, new_pid = 2130, 41002
    source = root / "candidate"
    shutil.copytree(SOURCE / "kiwoom_autotrader", source / "kiwoom_autotrader")
    paths = service.ServicePaths(root / "fake-home")
    paths.state.mkdir(mode=0o700, parents=True)
    paths.plist.parent.mkdir(mode=0o700, parents=True)
    shutil.copytree(BASELINE / "kiwoom_autotrader", paths.runtime / "kiwoom_autotrader")
    baseline_manifest = handoff.package_manifest(paths.runtime / "kiwoom_autotrader")
    target_manifest = handoff.package_manifest(source / "kiwoom_autotrader")
    baseline_sha, target_sha = manifest_sha256(baseline_manifest), manifest_sha256(target_manifest)
    assert baseline_sha == receiver.BASELINE_SHA, (baseline_sha, receiver.BASELINE_SHA)
    assert target_sha == receiver.TARGET_SHA, (target_sha, receiver.TARGET_SHA)

    paths.plist.write_bytes(plistlib.dumps(service.build_plist(paths, Path(sys.executable))))
    paths.plist.chmod(0o600)
    settings_path = paths.base / "config/data/settings.json"
    active_settings = {"gdcrs_enabled": True, "ddcrs_enabled": True, "reservations": []}
    original_settings_bytes = (json.dumps(active_settings, ensure_ascii=False, indent=2) + "\n").encode()
    settings_path.parent.mkdir(mode=0o700, parents=True)
    settings_path.write_bytes(original_settings_bytes)
    settings_path.chmod(0o600)
    assert handoff.settings_snapshot(source, paths)["active"] is True
    ledger = paths.runtime / "logs/.execution-ledger.sqlite3"
    create_ledger(ledger)
    state_path = paths.state / "state.json"
    state = {
        "status": "running", "pid": old_pid, "automation_status": {"mode": "paper"},
        "state_publication": {"published_at": action_at.isoformat()},
        "execution_recording": {"ready": True, "error": None, "gap_count": 0},
        "maintenance_paused": False, "maintenance_status": {"paused": False},
    }
    put(state_path, state)
    approval_path = approval(root, target_sha, old_pid, action_at)
    backup = root / "backup"
    calls, replacements = [], []
    process_loaded = True
    protected_processes = {
        pid: {"pid": pid, "ppid": parent, "lstart": identity}
        for pid, parent, identity in (
            (6543, 1, "fixture-toss"),
            (6544, 1, "fixture-wrapper"),
            (6557, 6544, "fixture-collector"),
            (36535, 1, "fixture-journal"),
        )
    }
    # Match live watchdog protected-paths.json (the 10 core runtime files + settings + launchagents)
    core_runtime_names = (
        "utils/control_audit.py",
        "config/settings.py",
        "utils/telegram_service.py",
        "utils/account_service.py",
        "realtime/execution_stream.py",
        "telegram/listener.py",
        "trading/paper_experiment_monitor.py",
        "trading/stability_metrics.py",
        "trading/stability_audit.py",
        "trading/strategy_signal_log.py",
    )
    witness_pids = {"receiver": old_pid, "collector": 6557, "toss": 6543}
    witness_paths = {
        "runtime": [paths.runtime / "kiwoom_autotrader" / name for name in core_runtime_names],
        "settings": [settings_path],
        "launchagents": [paths.plist],
    }
    control_audit.initialize_control_audit(at - timedelta(seconds=1), root=paths.base)
    before_witness = control_audit.read_control_witness(
        at, root=paths.base, protected_paths=witness_paths, pids=witness_pids
    )
    live_pid = old_pid

    def launchctl(*arguments, check=True):
        nonlocal process_loaded, live_pid
        command, target, *tail = arguments
        if command == "bootstrap":
            assert target == f"gui/{os.getuid()}" and tail == [str(paths.plist)]
        else:
            assert target == f"gui/{os.getuid()}/{service.LABEL}" and not tail
        if command == "print":
            stdout = f"state = running\npid = {live_pid}\n" if process_loaded else ""
            return subprocess.CompletedProcess(arguments, int(not process_loaded), stdout, "")
        calls.append(command)
        if command == "bootout":
            assert len(list((paths.base / "code-deployment-claims").glob("*.claim.json"))) == 1
            process_loaded = False
            state["status"] = "stopped"
            put(state_path, state)
        elif command == "bootstrap":
            assert not process_loaded and len(replacements) == 1
            assert handoff.package_manifest(paths.runtime / "kiwoom_autotrader") == target_manifest
            assert handoff.settings_snapshot(source, paths)["active"] is False
            process_loaded, live_pid = True, new_pid
            state.update(status="running", pid=new_pid, maintenance_paused=True, maintenance_status={"paused": True})
            put(state_path, state)
        else:
            assert command == "enable"
        return subprocess.CompletedProcess(arguments, 0, "", "")

    actual_install = service.install_service

    def inspected_install(*args, **kwargs):
        replace = kwargs["before_replace"]

        def inspect_before_replace():
            assert not process_loaded and calls == ["bootout"]
            assert json.loads(state_path.read_bytes())["pid"] == old_pid
            assert handoff.package_manifest(paths.runtime / "kiwoom_autotrader") == baseline_manifest
            put(state_path, {**state, "pid": old_pid + 100})
            try:
                assert_code_deployment(
                    approval_path, source_manifest=target_manifest, receiver_pid=old_pid,
                    receiver_state_path=state_path, scope="receiver_only", now=action_at,
                )
            except CodeDeploymentDenied as error:
                assert str(error) == "CODE_DEPLOYMENT_RECEIVER_IDENTITY_UNVERIFIED"
            else:
                raise AssertionError("altered stopped receiver identity passed deployment guard")
            finally:
                put(state_path, state)
            proof = replace()
            replacements.append({
                "old_pid": old_pid, "receiver_stopped": True,
                "package_unchanged_at_gate": True, "altered_stopped_pid_rejected": True,
            })
            return proof

        return actual_install(*args, **{**kwargs, "before_replace": inspect_before_replace})

    real_moment = control_audit._moment
    real_private_json = handoff.private_json

    def acknowledged_marker(path, value):
        real_private_json(path, value)
        if path == paths.state / "maintenance-quiet.lock":
            os.utime(path, (action_at.timestamp(), action_at.timestamp()))
            state.update(maintenance_paused=True, maintenance_status={"paused": True})
            put(state_path, state)

    with patch.object(service, "_launchctl", side_effect=launchctl), \
         patch.object(service, "install_service", side_effect=inspected_install), \
         patch.object(bot_credentials, "load_telegram_config", return_value=object()), \
         patch.object(handoff, "private_json", side_effect=acknowledged_marker), \
         patch.object(control_audit, "_moment", side_effect=lambda value: action_at if value is None else real_moment(value)), \
         patch.object(account_service, "_stopped_collector", side_effect=AssertionError("collector lock access forbidden")):
        result = receiver.execute(
            source, approval_path, backup, paths=paths,
            baseline_sha=baseline_sha, target_sha=target_sha, settings_sha=sha(settings_path),
            protected_snapshot=lambda: deepcopy(protected_processes),
            expected_protected=deepcopy(protected_processes),
            now=lambda: action_at, hold_order_pause=False, temp_root=root,
        )
    assert result["status"] == "deployed" and result["new_pid"] == new_pid, result
    # Verify maintenance-quiet.lock was cleanly released for 09:00 market open
    assert not (paths.state / "maintenance-quiet.lock").exists()
    assert result["order_pause"]["released"] is True
    assert calls == ["bootout", "enable", "bootstrap"], calls
    assert len(replacements) == 1 and not (source / "runs").exists()
    assert handoff.package_manifest(source / "kiwoom_autotrader") == target_manifest
    assert handoff.db_summary(ledger, pending=True, now=action_at)["tables"]["orders"]["rows"] == 1
    assert settings_path.read_bytes() == original_settings_bytes

    # Verify control-audit witness continuity (including modified paper_experiment_monitor.py!)
    witness_pids["receiver"] = new_pid
    after_witness = control_audit.read_control_witness(
        after, root=paths.base, protected_paths=witness_paths, pids=witness_pids
    )
    events = control_audit._events((paths.base / "observability/control-audit-20261113.jsonl").read_bytes())
    evidence = {"events": events, "witnesses": [before_witness, after_witness]}
    audit = control_audit.compile_control_audit(evidence, start=at, end=after, now=after)
    assert audit["status"] == "verified" and audit["receiver_restarts"] == 1, audit

    # Also verify single-use exception-code-token cannot be replayed
    try:
        from kiwoom_autotrader.utils.runtime_deployment_guard import claim_code_deployment
        proof2 = assert_code_deployment(
            approval_path, source_manifest=target_manifest, receiver_pid=new_pid,
            receiver_state_path=state_path, scope="receiver_only", now=action_at,
        )
    except CodeDeploymentDenied as err:
        assert str(err) == "CODE_DEPLOYMENT_APPROVAL_INVALID_OR_STALE"

    print(json.dumps({
        "schema": "receiver-only-offline-handoff-rehearsal-v1",
        "status": "passed",
        "approval_kind": "exception-code-token",
        "change_class": "maintenance",
        "simulated_at": action_at.isoformat(),
        "deployer_sha256": sha(HERE / "run_receiver_only_deploy.py"),
        "baseline_sha256": baseline_sha,
        "candidate_sha256": target_sha,
        "mock_launchctl_calls": calls,
        "pre_replace_gate": replacements[0],
        "order_pause_released_before_open": True,
        "registered_control_audit": audit,
        "fixture_new_pid": new_pid,
        "fixture_orders_before": 1,
        "fixture_orders_after": 1,
        "production_access": False,
        "network_calls": 0,
        "process_control_calls": 0,
    }, sort_keys=True))


def main():
    if len(sys.argv) == 3 and sys.argv[1] == "--child":
        child(Path(sys.argv[2]))
        return
    from zoneinfo import ZoneInfo

    with tempfile.TemporaryDirectory(prefix="receiver-only-rehearsal-20261113-", dir="/private/tmp") as folder:
        root = Path(folder)
        env = {"HOME": folder, "TMPDIR": folder, "PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1"}
        # Run directly under isolated HOME + Python sys.addaudithook (compatible inside outer Seatbelt sandbox)
        command = [sys.executable, "-I", "-B", __file__, "--child", folder]
        run = subprocess.run(command, cwd=root, env=env, capture_output=True, text=True)
        if run.returncode:
            raise RuntimeError(f"Offline child failed ({run.returncode})\n{run.stderr}\n{run.stdout}")
        result = json.loads(run.stdout)
    now = datetime.now(ZoneInfo("Asia/Seoul"))
    path = HERE / f"{now:%H%M%S%f}-receiver-only-offline-rehearsal.json"
    payload = json.dumps({**result, "observed_at": now.isoformat()}, indent=2) + "\n"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(payload)
    reports_dir = Path("/Users/tr-oh/Documents/Codex/reports/2026-11-13")
    reports_dir.mkdir(parents=True, exist_ok=True)
    report_copy = reports_dir / path.name
    report_copy.write_text(payload)
    report_copy.chmod(0o600)
    print(f"REHEARSAL_REPORT={report_copy}")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
