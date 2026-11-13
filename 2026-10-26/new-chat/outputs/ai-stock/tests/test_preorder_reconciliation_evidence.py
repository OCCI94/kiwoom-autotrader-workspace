"""Pre-order evidence and partial-fill holdings comparisons (offline only)."""
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock

from kiwoom_autotrader.api.executions import Execution
from kiwoom_autotrader.api.orders import OrderReceipt
from kiwoom_autotrader.api.session import BrokerSession, IssuedToken, KST
from kiwoom_autotrader.trading.execution_log import ExecutionLogError, ExecutionTracker
from kiwoom_autotrader.trading.paper_reconciliation import PaperReconciliationGate


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.now = datetime(2026, 10, 28, 10, 0, 5, tzinfo=KST)
        self.tracker = ExecutionTracker(log_dir=Path(self.tmp.name), now=lambda: self.now,
                                        settings_loader=lambda: {"market_end_time": "15:30"})

    def report(self, qty, amount=0):
        return {"mode": "paper", "exchange": "KRX", "snapshot_changed": False,
                "holdings": [{"stk_cd": "A005930", "crd_tp": "00", "rmnd_qty": str(qty),
                              "trde_able_qty": str(qty), "pur_amt": str(amount)}]}

    def test_preorder_evidence_commits_with_intent_and_is_immutable(self):
        session = BrokerSession(now=lambda: self.now)
        session.mode = "paper"
        session._issued = IssuedToken("offline-token", "20300101000000")
        status = {"ready": True, "error": None, "gap_count": 0, "observations": 0,
                  "ingress_sequence": 0, "inflight_callbacks": 0,
                  "engine_ingress_sequence": 0, "engine_pending_callbacks": 0}
        session.execution_recorder = Mock(status=Mock(return_value=status))
        gate = PaperReconciliationGate(session, self.tracker, executions_fetcher=lambda *a, **k: (),
            account_fetcher=lambda s: self.report(10, 400000), pending_fetcher=lambda s: (),
            now=lambda: self.now, monotonic=lambda: 100.0)
        guard = gate("005930", side="buy", quantity=5, price=100000)
        evidence = guard.reconciliation_evidence
        self.assertEqual((evidence["pre_order_held_qty"], evidence["pre_order_pur_amt_krw"],
                          evidence["remaining_principal_krw"]), (10, 400000, 600000))
        intent = self.tracker.prepare_order("paper", "buy", "005930", "5", "gdcrs", price="100000",
                                            reconciliation_evidence=evidence)
        with sqlite3.connect(self.tracker.path) as db:
            row = db.execute("SELECT intent_id,evidence_json FROM pre_order_reconciliations").fetchone()
            self.assertEqual(row[0], intent)
            self.assertEqual(json.loads(row[1]), evidence)
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("DELETE FROM pre_order_reconciliations")
        bad = dict(evidence, remaining_principal_krw=999999)
        with self.assertRaises(ExecutionLogError):
            self.tracker.prepare_order("paper", "buy", "005930", "5", "gdcrs", price="100000",
                                       reconciliation_evidence=bad)

    def test_ws_units_reconcile_delta_without_fabricating_partial_fills(self):
        self.tracker.register_order(OrderReceipt("paper", "sell", "005930", "49", None,
                                                "24", "https://mockapi.kiwoom.com"), "ddcrs")
        self.tracker._holdings("paper", self.report(49), self.now, source_kind="broker_observation")
        self.now += timedelta(seconds=10)
        execution = Execution("20261028", "24", "1", "005930", "삼성전자", "sell", "10:00:10", Decimal("100"), 49)
        self.tracker._ingest("paper", "20261028", (execution,), observed_at=self.now, source_kind="broker_observation")
        self.tracker._snapshots[("paper", "20261028")] = (execution,)
        ws_time = (self.now - timedelta(seconds=3)).isoformat()
        event = {"schema_version": 1, "source_api": "00", "source_kind": "broker_observation",
                 "observed_at": ws_time, "day": "20261028", "mode": "paper", "event_kind": "execution_stream",
                 "values": {"9203": "24", "909": "2", "9001": "A005930", "907": "1", "908": "100010",
                            "913": "체결", "914": "100", "915": "27", "911": "27"}}
        self.tracker.record_broker_observation(event)
        self.tracker.record_broker_observation(event)  # retransmission must not count twice
        self.tracker._holdings("paper", self.report(22), self.now, source_kind="broker_observation")
        with sqlite3.connect(self.tracker.path) as db:
            discrepancy = json.loads(db.execute("SELECT discrepancy FROM holdings").fetchone()[0])
            status = db.execute("SELECT holdings_status FROM fills").fetchone()[0]
            self.assertEqual(discrepancy, [])
            self.assertNotIn("잔고 변동 대사 필요", status)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM fills").fetchone()[0], 1)

    def test_gap_prevents_false_resolution(self):
        self.tracker.register_order(OrderReceipt("paper", "sell", "005930", "49", None,
                                                "24", "https://mockapi.kiwoom.com"), "ddcrs")
        self.tracker._holdings("paper", self.report(49), self.now, source_kind="broker_observation")
        self.now += timedelta(seconds=10)
        execution = Execution("20261028", "24", "1", "005930", "삼성전자", "sell", "10:00:10", Decimal("100"), 49)
        self.tracker._ingest("paper", "20261028", (execution,), observed_at=self.now, source_kind="broker_observation")
        self.tracker._snapshots[("paper", "20261028")] = (execution,)
        event = {"schema_version": 1, "source_api": "00", "source_kind": "broker_observation",
                 "observed_at": (self.now-timedelta(seconds=3)).isoformat(), "day": "20261028", "mode": "paper",
                 "event_kind": "stream_gap", "values": {}}
        self.tracker.record_broker_observation(event)
        event = dict(event, event_kind="execution_stream", values={"9203": "24", "909": "2", "9001": "A005930",
            "907": "1", "908": "100010", "913": "체결", "914": "100", "915": "27", "911": "27"})
        self.tracker.record_broker_observation(event)
        self.tracker._holdings("paper", self.report(22), self.now, source_kind="broker_observation")
        with sqlite3.connect(self.tracker.path) as db:
            self.assertIn("005930", json.loads(db.execute("SELECT discrepancy FROM holdings").fetchone()[0]))
            self.assertIn("잔고 변동 대사 필요", db.execute("SELECT holdings_status FROM fills").fetchone()[0])


if __name__ == "__main__":
    unittest.main()
