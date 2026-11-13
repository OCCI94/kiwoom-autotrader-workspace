"""Offline cache/export regression checks; every SQLite ledger is temporary."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from datetime import datetime
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

from kiwoom_autotrader.api.executions import RealizedProfit
from kiwoom_autotrader.api.session import KST
from kiwoom_autotrader.trading import execution_log as module


DAY = "20261026"


class ExecutionObservationCacheTests(unittest.TestCase):
    def test_reading_another_day_evicts_previous_raw_page_cache(self):
        self.insert(self.event())
        self.snapshot()
        self.assertIn((self.tracker.scope, DAY), self.tracker._broker_cache)
        self.snapshot(day='20261027')
        self.assertNotIn((self.tracker.scope, DAY), self.tracker._broker_cache)
        self.assertEqual(len(self.snapshot()), 1)
        self.assertLessEqual(len(self.tracker._broker_cache), 1)

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.now = datetime(2026, 10, 26, 10, 0, 5, tzinfo=KST)
        self.tracker = self.make_tracker(self.directory)
        with self.tracker._db():
            pass

    def make_tracker(self, directory, **kwargs):
        return module.ExecutionTracker(log_dir=directory, now=lambda: self.now,
            account_fetcher=Mock(), executions_fetcher=Mock(), profit_fetcher=Mock(), **kwargs)

    def event(self, *, mode="paper", day=DAY, unit="1"):
        return {"schema_version": 1, "source_api": "00", "source_kind": "broker_observation",
            "observed_at": self.now.isoformat(), "day": day, "mode": mode,
            "event_kind": "execution_stream", "values": {"9203": "24", "9001": "005930",
                "907": "2", "913": "체결", "908": "100005", "909": unit,
                "914": "100", "915": "1", "911": "1", "903": "100"}}

    def insert(self, event, tracker=None, identifier=None):
        tracker = tracker or self.tracker
        payload = module.canonical_json(event)
        values = (tracker.scope, event["day"], event["mode"], event["observed_at"], event["source_api"],
            hashlib.sha256(payload.encode()).hexdigest(), payload)
        with tracker._db() as db:
            if identifier is None:
                db.execute("INSERT INTO broker_observations(scope,day,mode,observed_at,source_api,digest,payload_json) VALUES (?,?,?,?,?,?,?)", values)
            else:
                db.execute("INSERT INTO broker_observations(id,scope,day,mode,observed_at,source_api,digest,payload_json) VALUES (?,?,?,?,?,?,?,?)", (identifier, *values))

    def snapshot(self, tracker=None, day=DAY, **kwargs):
        tracker = tracker or self.tracker
        with tracker._db() as db:
            return tracker._broker_observations_snapshot(db, day, **kwargs)

    def backup(self, target):
        with sqlite3.connect(self.tracker.path) as source, sqlite3.connect(target) as destination:
            source.backup(destination)

    def restore(self, backup):
        with sqlite3.connect(backup) as source, sqlite3.connect(self.tracker.path) as destination:
            source.backup(destination)

    def test_append_parses_only_new_payload_and_never_selects_old_payload_prefix(self):
        for unit in ("1", "2", "3"):
            self.insert(self.event(unit=unit))
        with patch.object(module.json, "loads", wraps=json.loads) as loads:
            first = self.snapshot()
            self.assertEqual(loads.call_count, 3)
            self.assertIs(self.snapshot(), first)
            self.assertEqual(loads.call_count, 3)
            self.insert(self.event(unit="4"))
            statements = []
            with self.tracker._db() as db:
                db.set_trace_callback(statements.append)
                second = self.tracker._broker_observations_snapshot(db, DAY)
            self.assertEqual(loads.call_count, 4)
        self.assertEqual(len(second), 4)
        self.assertIs(second[0], first[0])
        payload_reads = [sql for sql in statements if "SELECT id,digest,payload_json" in sql]
        self.assertEqual(len(payload_reads), 1)
        self.assertIn("id>3 ORDER BY id", payload_reads[0])
        self.assertTrue(any("COUNT(*),COALESCE(MAX(id),0)" in sql for sql in statements))

    def test_snapshot_and_nested_tree_cannot_be_mutated_or_grow_after_append(self):
        self.insert(self.event())
        first = self.snapshot()
        with self.assertRaises(TypeError):
            first[0].event["values"]["914"] = "999"
        with self.assertRaises(TypeError):
            first[0].event.update(mode="real")
        with self.assertRaises(FrozenInstanceError):
            first[0].id = 999
        self.insert(self.event(unit="2"))
        second = self.snapshot()
        self.assertEqual((len(first), len(second)), (1, 2))
        self.assertEqual(first[0].event["values"]["914"], "100")
        nested = module._immutable_json({"rows": [{"amount": "100"}]})
        with self.assertRaises(TypeError):
            nested["rows"].append({})
        with self.assertRaises(TypeError):
            nested["rows"][0].pop("amount")
        self.assertEqual(module.canonical_json(nested), '{"rows":[{"amount":"100"}]}')

    def test_scope_day_and_mode_filters_never_share_another_account_snapshot(self):
        self.insert(self.event())
        self.insert(self.event(mode="real", unit="2"))
        self.insert(self.event(day="20261027", unit="3"))
        other = self.make_tracker(self.directory, account_scope="other")
        self.insert(self.event(unit="4"), other)
        self.assertEqual([row.event["values"]["909"] for row in self.snapshot(mode="paper")], ["1"])
        self.assertEqual([row.event["values"]["909"] for row in self.snapshot(other)], ["4"])
        self.assertEqual([row.event["values"]["909"] for row in self.snapshot(day="20261027")], ["3"])
        self.assertEqual(self.snapshot(apis=("ka10072",)), ())

    def test_in_place_restore_count_regression_rebuilds_cache_and_history(self):
        self.insert(self.event())
        backup = self.directory / "earlier.sqlite3"
        self.backup(backup)
        self.insert(self.event(unit="2"))
        first = self.snapshot()
        self.tracker._materialize_history(DAY)
        inode = self.tracker.path.stat().st_ino
        self.restore(backup)
        self.assertEqual(self.tracker.path.stat().st_ino, inode)
        second = self.snapshot()
        self.assertEqual((len(first), len(second)), (2, 1))
        self.assertIsNot(first[0], second[0])
        self.tracker._materialize_history(DAY)
        history = self.directory / f"{DAY}.broker-observations.jsonl"
        self.assertEqual([json.loads(line)["id"] for line in history.read_text().splitlines()], [1])
        self.insert(self.event(unit="3"))
        self.assertEqual([row.event["values"]["909"] for row in self.snapshot()], ["1", "3"])

    def test_restore_empty_ledger_removes_stale_jsonl_without_inventing_rows(self):
        backup = self.directory / "empty.sqlite3"
        self.backup(backup)
        self.insert(self.event())
        self.tracker._materialize_history(DAY)
        self.restore(backup)
        self.tracker._materialize_history(DAY)
        self.assertEqual(self.snapshot(), ())
        self.assertEqual((self.directory / f"{DAY}.broker-observations.jsonl").read_bytes(), b"")

    def test_atomic_database_replacement_invalidates_same_count_and_max_ids(self):
        self.insert(self.event())
        first = self.snapshot()
        self.tracker._materialize_history(DAY)
        replacement = self.make_tracker(self.directory / "replacement")
        self.insert(self.event(unit="9"), replacement)
        replacement.path.replace(self.tracker.path)
        second = self.snapshot()
        self.assertIsNot(first[0], second[0])
        self.assertEqual(second[0].event["values"]["909"], "9")
        self.tracker._materialize_history(DAY)
        history = self.directory / f"{DAY}.broker-observations.jsonl"
        self.assertEqual(json.loads(history.read_text())["payload"]["values"]["909"], "9")

    def test_restore_changed_tail_or_same_count_different_max_forces_reload(self):
        self.insert(self.event())
        self.snapshot()
        replacement = self.make_tracker(self.directory / "replacement")
        self.insert(self.event(unit="8"), replacement)
        with sqlite3.connect(replacement.path) as source, sqlite3.connect(self.tracker.path) as destination:
            source.backup(destination)
        self.assertEqual(self.snapshot()[0].event["values"]["909"], "8")
        sparse = self.make_tracker(self.directory / "sparse")
        self.insert(self.event(unit="9"), sparse, identifier=10)
        with sqlite3.connect(sparse.path) as source, sqlite3.connect(self.tracker.path) as destination:
            source.backup(destination)
        self.assertEqual(self.snapshot()[0].id, 10)

    def test_uncommitted_snapshot_never_poison_shared_cache_after_rollback(self):
        self.insert(self.event())
        first = self.snapshot()
        event = self.event(unit="2")
        payload = module.canonical_json(event)
        with self.tracker._db() as db:
            db.execute("INSERT INTO broker_observations(scope,day,mode,observed_at,source_api,digest,payload_json) VALUES (?,?,?,?,?,?,?)",
                (self.tracker.scope, DAY, "paper", self.now.isoformat(), "00", hashlib.sha256(payload.encode()).hexdigest(), payload))
            self.assertEqual(len(self.tracker._broker_observations_snapshot(db, DAY)), 2)
            db.rollback()
        self.assertEqual(len(first), 1)
        self.assertEqual(len(self.snapshot()), 1)

    def test_novel_sell_materializes_each_sidecar_once_and_same_bytes_skip_atomic_write(self):
        event = self.event()
        event["values"]["907"] = "1"
        with patch.object(self.tracker, "_materialize_history", wraps=self.tracker._materialize_history) as history, \
                patch.object(self.tracker, "_materialize_accounting", wraps=self.tracker._materialize_accounting) as accounting:
            self.tracker.record_broker_observation(event)
            self.assertEqual((history.call_count, accounting.call_count), (1, 1))
        expected = {path.name: path.read_bytes() for path in self.directory.glob(f"{DAY}.*")}
        with patch.object(module.os, "replace", wraps=module.os.replace) as replace, \
                patch.object(module.os, "fsync", wraps=module.os.fsync) as fsync:
            self.tracker._materialize(DAY)
            self.assertEqual((replace.call_count, fsync.call_count), (0, 0))
        self.assertEqual({path.name: path.read_bytes() for path in self.directory.glob(f"{DAY}.*")}, expected)

    def test_cached_and_uncached_accounting_have_exact_same_canonical_bytes(self):
        self.insert(self.event())
        self.insert(self.event(unit="2"))
        cached = self.snapshot()
        with self.tracker._db() as db:
            raw = list(db.execute("SELECT id,payload_json FROM broker_observations ORDER BY id"))
        order = {"mode": "paper", "ord_no": "24", "symbol": "005930", "side": "buy", "quantity": 1,
            "strategy": "fixture", "registered_at": self.now.isoformat()}
        self.assertEqual(module.canonical_json(module._execution_accounting(DAY, [order], [], cached)),
            module.canonical_json(module._execution_accounting(DAY, [order], [], raw)))

    def test_concurrent_snapshots_do_not_reparse_or_alias_mutable_values(self):
        for unit in map(str, range(1, 21)):
            self.insert(self.event(unit=unit))
        with patch.object(module.json, "loads", wraps=json.loads) as loads:
            with ThreadPoolExecutor(max_workers=4) as pool:
                snapshots = list(pool.map(lambda _: self.snapshot(), range(12)))
        self.assertEqual(loads.call_count, 20)
        self.assertTrue(all(snapshot is snapshots[0] for snapshot in snapshots))

    def test_profit_reuses_one_accounting_for_period_and_conservative_checks(self):
        # Reuse the established complete-unit/independent-report fixture, not
        # a second implementation of the accounting or freshness rules.
        from tests.test_execution_accounting import ExecutionAccountingTests
        fixture = ExecutionAccountingTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.unit(1, 49, 49, 20392, 999208, 100)
        raw = {"stk_cd": "097520", "crd_tp": "현금잔고", "cntr_qty": "49", "buy_uv": "20150",
            "cntr_pric": "20392", "tdy_trde_cmsn": "100", "tdy_trde_tax": "50", "tdy_sel_pl": "11708"}
        fixture.cost("ka10072", [raw])
        fixture.cost("ka10073", [{**raw, "dt": DAY}])
        rows = (RealizedProfit("097520", 49, Decimal(20392), Decimal(20150), Decimal(100), Decimal(50), reported_net=Decimal(11708)),)
        with patch.object(fixture.tracker, "_materialize"), \
                patch.object(module, "_execution_accounting", wraps=module._execution_accounting) as accounting:
            fixture.tracker._profit("paper", DAY, "097520", rows)
        self.assertEqual(accounting.call_count, 1)
        with sqlite3.connect(fixture.tracker.path) as db:
            self.assertEqual(db.execute("SELECT net FROM profits").fetchone()[0], "11708")


if __name__ == "__main__":
    unittest.main()
