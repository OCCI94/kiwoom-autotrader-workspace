"""Temporary fixtures only: no real services, keychain, broker or Telegram."""
import importlib.util
import json
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime
from unittest.mock import patch, Mock

PROJECT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("handoff_deploy", PROJECT / "deploy_handoff_runtime.py")
d = importlib.util.module_from_spec(SPEC); SPEC.loader.exec_module(d)
from kiwoom_autotrader.utils.telegram_service import ServicePaths
from kiwoom_autotrader.utils.account_service import AccountServicePaths


class FakeServices:
    def __init__(self, home):
        self.paths = ServicePaths(home); self.account_paths = AccountServicePaths(home)
        self.states = {name: {"installed": True, "loaded": True, "running": True, "pid": 123} for name in ("bot", "collector")}
        self.calls = []; self.install_hook = self.restore_hook = None

    def status(self): return {name: dict(value) for name, value in self.states.items()}
    def stop(self):
        self.calls.append("stop")
        for state in self.states.values(): state.update(loaded=False, running=False, pid=None)
    def install(self, source, held, *, before_replace=None,before_mutation=None):
        if before_mutation:before_mutation()
        self.calls.append("install")
        assert not any(state["running"] for state in self.states.values())
        assert self.paths.state.resolve() in held
        destination = self.paths.runtime / d.PACKAGE
        if before_replace: before_replace()
        shutil.rmtree(destination); shutil.copytree(source / d.PACKAGE, destination)
        self.paths.plist.write_bytes(b"installed-plist")
        if self.install_hook: self.install_hook()
    def restore(self, previous):
        self.calls.append("restore")
        self.states = {name: dict(value) for name, value in previous.items()}
        if self.restore_hook: self.restore_hook()


class DeployTests(unittest.TestCase):
    def setUp(self):
        # These existing tests isolate financial backup/rollback invariants.
        # The real permission boundary has separate integration tests below.
        self.code_guard = patch.object(d, 'assert_code_deployment', return_value={'approval_sha256':'a'*64})
        self.code_guard.start(); self.addCleanup(self.code_guard.stop)
        self.claim_guard=patch.object(d,'claim_code_deployment',return_value={'fixture_claim':True})
        self.claim_guard.start();self.addCleanup(self.claim_guard.stop)
        self.claim_verify=patch.object(d,'verify_deployment_claim',return_value={})
        self.claim_verify.start();self.addCleanup(self.claim_verify.stop)
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve(); self.source = self.root / "source"
        self.services = FakeServices(self.root / "home"); self.paths = self.services.paths
        defaults = json.loads((PROJECT / d.PACKAGE / d.RESOURCES[0]).read_text())
        for root, content in ((self.source, "# new source\n"), (self.paths.runtime, "# previous runtime\n")):
            package = root / d.PACKAGE
            (package / "config/data").mkdir(parents=True)
            (package / "__main__.py").write_text(content)
            (package / "module.py").write_text(content)
            for name in d.RESOURCES: (package / name).write_text(json.dumps(defaults))
        self.settings = self.paths.base / "config/data/settings.json"
        self.settings.parent.mkdir(parents=True); self.settings.write_text(json.dumps(defaults))
        self.paths.state.mkdir(parents=True); self.services.account_paths.state.mkdir(parents=True)
        self.paths.plist.parent.mkdir(parents=True)
        self.paths.plist.write_bytes(b"original-bot-plist"); self.services.account_paths.plist.write_bytes(b"original-collector-plist")
        self.ledger = self.paths.runtime / "logs/.execution-ledger.sqlite3"
        self.ledger.parent.mkdir()
        with sqlite3.connect(self.ledger) as db:
            db.executescript("""
            CREATE TABLE intents(scope TEXT,id TEXT,day TEXT,mode TEXT,order_number TEXT,state TEXT);
            CREATE TABLE orders(scope TEXT,day TEXT,mode TEXT,ord_no TEXT,quantity INTEGER);
            CREATE TABLE fills(scope TEXT,day TEXT,mode TEXT,ord_no TEXT,fill_no TEXT,quantity INTEGER,price TEXT,remaining TEXT,holdings_at TEXT,holdings_status TEXT);
            CREATE TABLE holdings(scope TEXT,mode TEXT,observed_at TEXT);
            CREATE TABLE profits(scope TEXT,net TEXT);
            CREATE TABLE summaries(scope TEXT,payload TEXT);
            """)
            for i in range(4):
                values = ("default", "20261026", "paper", str(i))
                db.execute("INSERT INTO intents VALUES (?,?,?,?,?,?)", ("default", str(i), "20261026", "paper", str(i), "accepted"))
                db.execute("INSERT INTO orders VALUES (?,?,?,?,?)", (*values, 10))
                db.execute("INSERT INTO fills VALUES (?,?,?,?,?,?,?,?,?,?)", (*values, str(i), 10, "100", None, None, None))
            for i in (4, 5): db.execute("INSERT INTO intents VALUES (?,?,?,?,?,?)", ("default", str(i), "20261026", "paper", None, "cancelled"))
            db.execute("INSERT INTO holdings VALUES ('default','paper','old')")
            db.execute("INSERT INTO profits VALUES ('default','10')")
            db.execute("INSERT INTO summaries VALUES ('default','old')")
        for target in ("socket.socket.connect", "kiwoom_autotrader.utils.keychain._get_backend"):
            p = patch(target, side_effect=AssertionError("External access forbidden")); fake = p.start()
            self.addCleanup(p.stop); self.addCleanup(fake.assert_not_called)

    def execute(self, deploy=True): return d.execute(self.source, deploy=deploy, services=self.services)
    def change(self, sql):
        with sqlite3.connect(self.ledger) as db: db.execute(sql)

    def test_check_only_has_no_mutation_lock_sqlite_or_credentials(self):
        before = {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        with patch.object(Path, "mkdir", side_effect=AssertionError("mkdir forbidden")), patch.object(d, "exclusive", side_effect=AssertionError("lock forbidden")), patch.object(sqlite3, "connect", side_effect=AssertionError("db forbidden")):
            result = self.execute(deploy=False)
        self.assertEqual(result["status"], "ready_for_stopped_validation")
        self.assertEqual(result["source_file_count"], 4)
        self.assertEqual(result["ledger_validation"], "deferred_until_writers_stopped")
        self.assertEqual(self.services.calls, [])
        self.assertEqual(before, {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob("*") if p.is_file()})

    def test_real_code_guard_denies_before_first_lock_stop_or_write(self):
        self.code_guard.stop()
        before = {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        with self.assertRaisesRegex(d.DeploymentError, 'EXPLICIT_CODE_DEPLOYMENT_APPROVAL_REQUIRED'), \
                patch.object(d, 'exclusive', side_effect=AssertionError('first mutation forbidden')):
            self.execute()
        self.assertEqual(self.services.calls, [])
        self.assertEqual(before, {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob('*') if p.is_file()})

    def test_shared_daily_claim_blocks_second_normal_deploy_before_process_stop(self):
        from kiwoom_autotrader.utils import runtime_deployment_guard as gate
        self.claim_guard.stop();self.claim_verify.stop()
        now=datetime(2026,11,3,16,0,tzinfo=gate.KST)
        proof={'schema':'runtime-code-deployment-guard-proof-v1','checked_at':now.isoformat(),
            'receiver_pid':123,'scope':'receiver_and_collector','kind':'after-close-code-approval',
            'window':{'normal_window':True},**{key:'a'*64 for key in ('approval_sha256',
                'source_manifest_sha256','calendar_source_sha256','calendar_file_sha256','claim_sha256')}}
        with patch.object(d,'assert_code_deployment',return_value=proof),patch.object(d,'claim_code_deployment',
                side_effect=lambda directory,permission:gate.claim_code_deployment(directory,permission,now=now)):
            first=self.execute();self.assertEqual(first['status'],'deployed')
            prior_calls=list(self.services.calls)
            second=self.execute()
        self.assertEqual(second['reason'],'DEPLOYMENT_DATE_ALREADY_CLAIMED_EXPLICIT_EXCEPTION_REQUIRED')
        self.assertEqual(second['recovery'],'no_process_change')
        self.assertEqual(self.services.calls,prior_calls)
        self.assertEqual(len(list((self.paths.base/'code-deployment-claims').glob('*.claim.json'))),1)

    def test_expired_recheck_does_not_stop_as_exception_cleanup(self):
        d.assert_code_deployment.side_effect=[{'approval_sha256':'a'*64}, d.CodeDeploymentDenied('EXPIRED')]
        result=self.execute()
        self.assertEqual(result['reason'],'EXPIRED');self.assertEqual(self.services.calls,[])

    def test_postcopy_permission_rejection_never_replaces_or_restarts(self):
        old=d.package_manifest(self.paths.runtime/d.PACKAGE)
        d.assert_code_deployment.side_effect=[{'approval_sha256':'a'*64}]*3+[d.CodeDeploymentDenied('EXPIRED_DURING_COPY')]
        result=self.execute()
        self.assertEqual(result['reason'],'EXPIRED_DURING_COPY')
        self.assertEqual(result['recovery'],'code_permission_expired_workers_left_stopped')
        self.assertNotIn('restore',self.services.calls)
        self.assertEqual(d.package_manifest(self.paths.runtime/d.PACKAGE),old)

    def test_real_services_adapter_passes_guard_into_installer(self):
        from kiwoom_autotrader.utils import telegram_service
        adapter=d.Services.__new__(d.Services);adapter.bot=telegram_service;adapter.paths=self.paths
        callback=Mock()
        with patch.object(telegram_service,'install_service') as install:
            adapter.install(self.source,{self.paths.state.resolve()},before_replace=callback)
        self.assertIs(install.call_args.kwargs['before_replace'],callback)

    def test_success_backs_up_and_restores_both_original_states(self):
        settings = self.settings.read_bytes(); old_code = (self.paths.runtime / d.PACKAGE / "module.py").read_bytes()
        result = self.execute()
        self.assertEqual(result["status"], "deployed")
        self.assertTrue(result["source_runtime_match"])
        self.assertEqual(self.settings.read_bytes(), settings)
        backup = Path(result["backup"])
        self.assertEqual((backup / "base/runtime" / d.PACKAGE / "module.py").read_bytes(), old_code)
        self.assertEqual((backup / "bot.plist").read_bytes(), b"original-bot-plist")
        self.assertEqual(backup.stat().st_mode & 0o777, 0o700)
        self.assertTrue(all(p.stat().st_mode & 0o777 == 0o600 for p in backup.rglob("*") if p.is_file()))
        summary = d.db_summary(self.ledger, pending=True)
        self.assertEqual(summary["pending_intents"], 0); self.assertEqual(summary["unfilled_orders"], 0)
        self.assertEqual(summary["tables"]["intents"]["rows"], 6)
        self.assertTrue(all(value["running"] for value in result["services"].values()))

    def test_inactive_services_stay_inactive(self):
        for row in self.services.states.values(): row.update(loaded=False, running=False, pid=None)
        result = self.execute()
        self.assertEqual(result["status"], "deployed")
        self.assertFalse(any(row["running"] for row in result["services"].values()))

    def test_active_strategy_blocks_before_stop_or_backup(self):
        data = json.loads(self.settings.read_text()); data["gdcrs_enabled"] = True; self.settings.write_text(json.dumps(data))
        result = self.execute()
        self.assertEqual(result["status"], "blocked"); self.assertEqual(self.services.calls, [])
        self.assertFalse((self.paths.base / "backups").exists())

    def test_pending_intent_and_partial_fill_block_before_install(self):
        self.change("UPDATE intents SET state='prepared' WHERE id='4'")
        result = self.execute()
        self.assertEqual(result["reason"], "UNRESOLVED_ORDER_REQUIRES_RECONCILIATION")
        self.assertNotIn("install", self.services.calls)
        self.change("UPDATE intents SET state='cancelled' WHERE id='4'")
        self.change("UPDATE fills SET quantity=9 WHERE fill_no='0'")
        result = self.execute()
        self.assertEqual(result["reason"], "UNRESOLVED_ORDER_REQUIRES_RECONCILIATION")
        self.assertNotIn("install", self.services.calls)

    def test_corrupt_or_missing_ledger_cannot_pass(self):
        self.ledger.write_bytes(b"not a sqlite database")
        result = self.execute()
        self.assertEqual(result["status"], "failed"); self.assertNotIn("install", self.services.calls)
        self.ledger.unlink()
        result = self.execute()
        self.assertIn("EXECUTION_LEDGER_MISSING_OR_UNSAFE", result["blockers"])

    def test_install_failure_rolls_back_package_plist_but_keeps_new_data(self):
        old = d.package_manifest(self.paths.runtime / d.PACKAGE)
        def fail():
            self.change("INSERT INTO fills VALUES ('new','20261027','paper','new','new',1,'200',NULL,NULL,NULL)")
            raise RuntimeError("secret token must not be printed")
        self.services.install_hook = fail
        result = self.execute()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["recovery"], "package_and_plists_restored")
        self.assertEqual(d.package_manifest(self.paths.runtime / d.PACKAGE), old)
        self.assertEqual(self.paths.plist.read_bytes(), b"original-bot-plist")
        self.assertEqual(d.db_summary(self.ledger)["tables"]["fills"]["rows"], 5)
        self.assertNotIn("secret", json.dumps(result))

    def test_external_settings_change_is_preserved_and_workers_stay_stopped(self):
        def change_settings():
            data = json.loads(self.settings.read_text()); data["gdcrs_enabled"] = True; self.settings.write_text(json.dumps(data))
        self.services.install_hook = change_settings
        result = self.execute()
        self.assertEqual(result["reason"], "SETTINGS_CHANGED")
        self.assertEqual(result["recovery"], "settings_changed_preserved_workers_left_stopped")
        self.assertTrue(json.loads(self.settings.read_text())["gdcrs_enabled"])
        self.assertFalse(any(row["running"] for row in result["services"].values()))

    def test_normal_startup_migration_and_new_observations_are_allowed(self):
        def migrate():
            with sqlite3.connect(self.ledger) as db:
                db.execute("CREATE TABLE order_decisions(id TEXT)")
                db.execute("ALTER TABLE fills ADD COLUMN future_column TEXT")
                db.execute("UPDATE holdings SET observed_at='new'")
                db.execute("UPDATE profits SET net='20'")
                db.execute("UPDATE summaries SET payload='new'")
                db.execute("UPDATE fills SET remaining='10',holdings_at='new',holdings_status='observed'")
        self.services.restore_hook = migrate
        result = self.execute()
        self.assertEqual(result["status"], "deployed")

    def test_destroyed_financial_row_stops_and_reports_manual_recovery(self):
        self.services.restore_hook = lambda: self.change("DELETE FROM fills WHERE fill_no='0'")
        result = self.execute()
        self.assertEqual(result["reason"], "EXISTING_FINANCIAL_ROW_CHANGED")
        self.assertEqual(result["recovery"], "recovery_incomplete_manual_review_required")
        self.assertFalse(any(row["running"] for row in result["services"].values()))
        self.assertEqual(d.db_summary(self.ledger)["tables"]["fills"]["rows"], 3)  # never overwrite current data

    def test_committed_wal_is_backed_up_without_corruption_assumption(self):
        db = sqlite3.connect(self.ledger); self.addCleanup(db.close)
        db.execute("PRAGMA journal_mode=WAL"); db.execute("PRAGMA wal_autocheckpoint=0")
        db.execute("UPDATE summaries SET payload='committed-wal'"); db.commit()
        self.assertTrue(Path(str(self.ledger) + "-wal").exists())
        destination = self.root / "wal-backup.sqlite3"
        d.sqlite_backup(self.ledger, destination)
        self.assertEqual(d.db_summary(self.ledger), d.db_summary(destination))
        self.assertFalse(Path(str(destination) + "-wal").exists())

    def test_installer_start_is_suppressed_and_functions_restored(self):
        backend = object.__new__(d.Services)
        backend.paths = self.paths
        module = Mock(); original_start = Mock(); original_stopped = Mock()
        module.start_service = original_start; module._stopped_directory = original_stopped
        def install(**kwargs):
            with module._stopped_directory(self.paths.state): module.start_service(self.paths)
        module.install_service.side_effect = install; backend.bot = module
        backend.install(self.source, {self.paths.state.resolve()})
        original_start.assert_not_called(); original_stopped.assert_not_called()
        self.assertIs(module.start_service, original_start); self.assertIs(module._stopped_directory, original_stopped)

    def test_stopped_waits_for_both_loaded_and_running_to_clear(self):
        clock = [0.0]; waits = []
        def wait(delay): waits.append(delay); clock[0] += delay
        def status():
            return {"bot": {"loaded": clock[0] < 0.4, "running": clock[0] < 0.2},
                    "collector": {"loaded": clock[0] < 0.6, "running": False}}
        service = Mock(); service.status.side_effect = status
        d.stopped(service, clock=lambda: clock[0], wait=wait)
        self.assertGreaterEqual(clock[0], 0.6)
        self.assertLess(clock[0], 0.81)
        self.assertTrue(all(delay <= 0.2 for delay in waits))

    def test_stopped_timeout_reports_only_phase_and_boolean_state(self):
        clock = [0.0]
        def wait(delay): clock[0] += delay
        service = Mock(); service.status.return_value = {
            "bot": {"loaded": True, "running": False, "token": "secret-never-print", "pid": 123},
            "collector": {"loaded": False, "running": False}}
        with self.assertRaises(d.DeploymentError) as caught:
            d.stopped(service, phase="before_backup", clock=lambda: clock[0], wait=wait)
        self.assertEqual(clock[0], 15)
        self.assertEqual(caught.exception.diagnostics, {"phase": "before_backup", "stop_snapshot": {
            "bot": {"loaded": True, "running": False}, "collector": {"loaded": False, "running": False}}})
        self.assertNotIn("secret", json.dumps(caught.exception.diagnostics))

    def test_missing_status_does_not_mean_stopped(self):
        clock = [0.0]
        def wait(delay): clock[0] += delay
        service = Mock(); service.status.return_value = {"bot": {"loaded": False, "running": False}}
        with self.assertRaises(d.DeploymentError) as caught:
            d.stopped(service, timeout=0.4, clock=lambda: clock[0], wait=wait)
        self.assertIsNone(caught.exception.diagnostics["stop_snapshot"]["collector"]["loaded"])

    def test_execute_preserves_stop_timeout_diagnostics_during_recovery(self):
        original = d.stopped
        diagnostics = {"phase": "after_initial_stop", "stop_snapshot": {
            "bot": {"loaded": True, "running": False}, "collector": {"loaded": False, "running": False}}}
        calls = []
        def stop_check(services, **kwargs):
            calls.append(kwargs["phase"])
            if len(calls) == 1: raise d.DeploymentError("WORKERS_NOT_STOPPED", diagnostics=diagnostics)
            return original(services, **kwargs)
        with patch.object(d, "stopped", side_effect=stop_check): result = self.execute()
        self.assertEqual(result["diagnostics"], diagnostics)
        self.assertEqual(result["recovery"], "not_required")
        self.assertNotIn("install", self.services.calls)


if __name__ == "__main__": unittest.main()
