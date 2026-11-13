"""Decision evidence tests: temporary ledgers and mocked transports only."""
from collections import deque
from datetime import datetime, timedelta
from decimal import Decimal
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

from kiwoom_autotrader.api import orders
from kiwoom_autotrader.api.executions import Execution, RealizedProfit
from kiwoom_autotrader.api.minute_chart import MinuteBar, session_identity
from kiwoom_autotrader.api.session import BrokerSession, IssuedToken, KST
from kiwoom_autotrader.trading import order_context
from kiwoom_autotrader.trading.decision_evidence import (
    DecisionEvidenceError, freeze_evidence, strategy_source_hash, json_sha256,
)
from kiwoom_autotrader.trading.execution_log import ExecutionTracker, ExecutionLogError
from kiwoom_autotrader.trading.golden_cross import GoldenCrossMonitor
from kiwoom_autotrader.trading.cross_monitor import CrossMonitor, sma_difference
from tests.paper_reconciliation_fixture import fake_reconciliation

NOW = datetime(2026, 10, 26, 10, 4, 5, tzinfo=KST)


def evidence(strategy="gdcrs"):
    return {"strategy": strategy, "strategy_version": "fixture-v1", "run_id": "offline-run-1",
        "source_kind": "synthetic", "evaluated_at": NOW.isoformat(), "observed_at": NOW.isoformat(),
        "config": {"enabled": True, "short_period": 1, "long_period": 3}, "budget": 225225}


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        # This suite tests evidence, not the account-specific automatic policy.
        policy = patch("kiwoom_autotrader.trading.paper_policy.load_settings", return_value={"paper_automation_only": False})
        policy.start()
        self.addCleanup(policy.stop)
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.now = NOW
        self.tracker = ExecutionTracker(settings_loader=lambda:{'max_positions_per_theme':0}, log_dir=self.root, now=lambda: self.now)
        self.session = BrokerSession(now=lambda: self.now)
        self.session.mode = "paper"
        self.session._issued = IssuedToken("offline-private-decision-token", "20300101000000")
        self.session.paper_automation_reconcile = Mock(side_effect=fake_reconciliation)
        for target in ("socket.socket.connect", "kiwoom_autotrader.utils.keychain._get_backend"):
            patcher = patch(target, side_effect=AssertionError("External access forbidden"))
            fake = patcher.start()
            self.addCleanup(patcher.stop)
            self.addCleanup(fake.assert_not_called)
        token = order_context._current.set(None)
        self.addCleanup(order_context._current.reset, token)

    def rows(self, table):
        with sqlite3.connect(self.tracker.path) as db:
            db.row_factory = sqlite3.Row
            return [dict(row) for row in db.execute("SELECT * FROM " + table)]

    def initialize(self):
        with self.tracker._db():
            pass

    def test_snapshot_is_detached_immutable_and_unknowns_remain_null(self):
        original = evidence()
        frozen = freeze_evidence(original)
        original["budget"] = 1
        original["config"]["short_period"] = 2
        read = frozen.as_dict()
        read["budget"] = 7
        self.assertEqual(frozen.as_dict()["budget"], 225225)
        self.assertEqual(frozen.as_dict()["config"]["short_period"], 1)
        self.assertIsNone(frozen.as_dict()["source_revision"])
        self.assertIsNone(frozen.as_dict()["source_api"])
        self.assertEqual(frozen.as_dict()["config_sha256"], json_sha256(frozen.as_dict()["config"]))
        self.assertEqual(frozen.sha256, hashlib.sha256(frozen.json.encode()).hexdigest())
        self.assertEqual(freeze_evidence({}).as_dict()["source_kind"], "unknown")

    def test_rejects_unbounded_unrecognized_nonexact_and_future_evidence(self):
        invalid = [dict(evidence(), token="secret"), dict(evidence(), budget=1.5),
            dict(evidence(), budget=True), dict(evidence(), budget=10**12),
            dict(evidence(), source_kind=[]), dict(evidence(), config_sha256="0" * 64),
            dict(evidence(), observed_at=(NOW + timedelta(seconds=1)).isoformat()),
            dict(evidence(), config={"token": "secret"}),
            dict(evidence(), strategy_version="x" * 40000),
            dict(evidence(), budget=float("nan"))]
        for item in invalid:
            with self.subTest(item=list(item)):
                with self.assertRaises(DecisionEvidenceError) as error:
                    freeze_evidence(item)
                self.assertNotIn("secret", str(error.exception))

    def test_code_hash_cached_no_per_tick_read(self):
        source = self.root / "strategy.py"
        source.write_text("version one")
        strategy_source_hash.cache_clear()
        with patch.object(Path, "open", autospec=True, wraps=Path.open) as opened:
            # Use a normal file opener via explicit side effect to count reads.
            opened.side_effect = lambda path, *a, **kw: io.BytesIO(b"version one")
            first = strategy_source_hash(str(source))
            second = strategy_source_hash(str(source))
        self.assertEqual(first, second)
        self.assertEqual(opened.call_count, 1)
        strategy_source_hash.cache_clear()
        self.assertIsNone(strategy_source_hash(str(self.root / "missing.py")))

    def test_context_nesting_restores_decision_and_no_manual_journal(self):
        self.assertIsNone(order_context.prepare_tracking("paper", "buy", "005930", "1", price="100"))
        with order_context.automated_orders(self.tracker, "gdcrs", evidence=evidence()):
            outer = order_context._current.get().evidence.sha256
            changed = evidence(); changed["budget"] = 999
            with order_context.order_decision(changed):
                self.assertNotEqual(order_context._current.get().evidence.sha256, outer)
                with order_context.automated_orders(self.tracker, "ddcrs"):
                    self.assertIsNone(order_context._current.get().evidence)
                self.assertEqual(order_context._current.get().strategy, "gdcrs")
            self.assertEqual(order_context._current.get().evidence.sha256, outer)
        self.assertIsNone(order_context._current.get())
        self.assertFalse(self.tracker.path.exists())

    def test_limit_evidence_and_quantity_committed_before_http_and_receipt(self):
        def transport(opener, request, **kwargs):
            decisions, intents = self.rows("order_decisions"), self.rows("intents")
            self.assertEqual(len(decisions), 1)
            self.assertEqual(decisions[0]["intent_id"], intents[0]["id"])
            self.assertEqual((intents[0]["quantity"], intents[0]["state"]), (1, "prepared"))
            self.assertEqual((decisions[0]["order_price"], decisions[0]["order_type"]), ("213500", "limit"))
            self.assertEqual(json.loads(decisions[0]["evidence_json"])["budget"], 225225)
            self.assertNotIn(self.session.token, str(decisions))
            kwargs["before_send"]()
            return io.BytesIO(b'{"return_code":0,"ord_no":"24","dmst_stex_tp":"KRX"}')
        with patch.object(orders, "queued_open", side_effect=transport) as queue, \
                patch.object(orders.urllib.request, "build_opener"), \
                order_context.automated_orders(self.tracker, "gdcrs", evidence=evidence()):
            orders.submit_order(self.session, "buy", "005930", "0001", "213500")
        queue.assert_called_once()
        self.assertEqual(self.rows("intents")[0]["state"], "accepted")
        self.assertEqual(len(self.rows("order_decisions")), 1)
        self.assertEqual(len(self.rows("orders")), 1)

    def test_evidence_insert_failure_rolls_back_intent_and_prevents_http(self):
        self.initialize()
        with sqlite3.connect(self.tracker.path) as db:
            db.execute("CREATE TRIGGER fail_evidence BEFORE INSERT ON order_decisions BEGIN SELECT RAISE(ABORT,'private'); END")
        with patch.object(orders, "queued_open") as queue, \
                order_context.automated_orders(self.tracker, "gdcrs", evidence=evidence()):
            with self.assertRaises(orders.OrderError) as error:
                orders.submit_order(self.session, "buy", "005930", "1", "213500")
        queue.assert_not_called()
        self.assertNotIn("private", str(error.exception))
        self.assertEqual(self.rows("intents"), [])
        self.assertEqual(self.rows("order_decisions"), [])

    def test_existing_rows_not_backfilled_market_and_unknown_are_distinct(self):
        self.root.mkdir(exist_ok=True)
        with sqlite3.connect(self.tracker.path) as db:
            db.execute("CREATE TABLE intents(scope TEXT,id TEXT,day TEXT,mode TEXT,side TEXT,symbol TEXT,quantity INTEGER,strategy TEXT,created_at TEXT,order_number TEXT,PRIMARY KEY(scope,id))")
            db.execute("INSERT INTO intents VALUES ('default','old','20261025','paper','buy','005930',2,'gdcrs','old-time',NULL)")
        self.tracker.prepare_order("paper", "sell", "005930", 1, "ddcrs", price=None)
        self.tracker.prepare_order("paper", "sell", "005930", 1, "ddcrs")
        decisions = self.rows("order_decisions")
        self.assertEqual(len(self.rows("intents")), 3)
        self.assertEqual({row["order_type"] for row in decisions}, {"market", "unknown"})
        self.assertTrue(all(row["evidence_json"] is None for row in decisions))
        self.assertFalse(any(row["intent_id"] == "old" for row in decisions))

    def test_evidence_cannot_change_or_delete_and_receipt_price_must_match(self):
        identifier = self.tracker.prepare_order("paper", "buy", "005930", 1, "gdcrs", price="100", evidence=evidence())
        for sql in ("UPDATE order_decisions SET order_price='99'", "DELETE FROM order_decisions"):
            with sqlite3.connect(self.tracker.path) as db, self.assertRaises(sqlite3.IntegrityError):
                db.execute(sql)
        receipt = orders.OrderReceipt("paper", "buy", "005930", "1", "99", "24", "offline")
        with self.assertRaises(ExecutionLogError):
            self.tracker.register_order(receipt, "gdcrs", intent_id=identifier)
        self.assertEqual(self.rows("intents")[0]["state"], "prepared")
        self.assertEqual(self.rows("orders"), [])

    def test_profit_raw_components_and_unknown_attribution_are_preserved(self):
        self.tracker = ExecutionTracker(settings_loader=lambda:{'max_positions_per_theme':0}, log_dir=self.root, now=lambda: self.now, profit_fetcher=Mock())
        costs = (RealizedProfit("005930", 2, Decimal("101"), Decimal("90"), Decimal("0.25"), Decimal("0")),)
        self.tracker._profit("paper", "20261026", "005930", costs)
        observed = self.rows("profit_observations")[0]
        payload = json.loads(observed["payload_json"])
        self.assertEqual(payload["rows"][0]["commission"], "0.25")
        self.assertEqual(payload["rows"][0]["tax"], "0")
        self.assertEqual(payload["source_kind"], "unknown")
        self.assertIsNone(payload["source_api"])
        self.assertIsNone(payload["net"])
        self.assertEqual(payload["attribution_status"], "unconfirmed")
        self.now += timedelta(seconds=10)
        self.tracker._profit("paper", "20261026", "005930", costs)
        self.assertEqual(len(self.rows("profit_observations")), 1)
        self.assertEqual(self.rows("profit_observations")[0]["observed_at"], NOW.isoformat())
        with sqlite3.connect(self.tracker.path) as db, self.assertRaises(sqlite3.IntegrityError):
            db.execute("DELETE FROM profit_observations")

    def test_confirmed_profit_provenance_links_exact_components_and_fills(self):
        receipt = orders.OrderReceipt("paper", "sell", "005930", "2", None, "24", "offline")
        self.tracker.register_order(receipt, "ddcrs")
        fill = Execution("20261026", "24", "1", "005930", "fixture", "sell", "10:04:00", Decimal("101"), 2)
        self.tracker._ingest("paper", "20261026", (fill,))
        self.tracker._snapshots[("paper", "20261026")] = (fill,)
        # A production-provenance projection now requires the independently
        # observed 00 unit and both complete post-sell cost-query responses.
        self.tracker.record_broker_observation({"schema_version": 1, "source_api": "00",
            "source_kind": "broker_observation", "day": "20261026", "mode": "paper",
            "observed_at": self.now.isoformat(), "event_kind": "execution_stream",
            "values": {"9203": "24", "9001": "005930", "907": "1", "913": "체결",
                       "908": "100400", "909": "1", "914": "101", "915": "2", "911": "2", "903": "202"}})
        row = {"stk_cd": "005930", "crd_tp": "현금잔고", "cntr_qty": "2", "cntr_pric": "101",
               "buy_uv": "90", "tdy_trde_cmsn": "1", "tdy_trde_tax": "2", "tdy_sel_pl": "19"}
        for api, field, query in (("ka10072", "dt_stk_div_rlzt_pl", "a"*32),
                                  ("ka10073", "dt_stk_rlzt_pl", "b"*32)):
            request = {"stk_cd": "005930", "strt_dt": "20261026"}
            cost_row = dict(row)
            if api == "ka10073":
                request["end_dt"] = "20261026"
                cost_row["dt"] = "20261026"
            self.tracker.record_broker_observation({"schema": "kiwoom-broker-page-v1", "source_api": api,
                "source_kind": "broker_observation", "mode": "paper", "day": "20261026", "query_id": query,
                "request_started_at": self.now.isoformat(), "observed_at": self.now.isoformat(), "page": 1,
                "request_body": request, "response_body": {"return_code": 0, field: [cost_row]},
                "continuation": {"request_cont_yn": "N", "response_cont_yn": "N", "next_key_present": False},
                "status": "success", "http_status": 200})
        self.tracker._profit("paper", "20261026", "005930",
            (RealizedProfit("005930", 2, Decimal("101"), Decimal("90"), Decimal("1"), Decimal("2"), reported_net=Decimal("19")),))
        payload = json.loads(self.rows("profit_observations")[0]["payload_json"])
        self.assertEqual((payload["net"], payload["basis"], payload["attribution_status"]), ("19", "180", "reconciled"))
        self.assertEqual(payload["source_api"], "ka10072")
        # A later malformed observation invalidates the current projection,
        # while the immutable earlier confirmed observation remains intact.
        self.tracker._profit("paper", "20261026", "005930",
            (RealizedProfit("005930", 2, Decimal("NaN"), Decimal("90"), Decimal("1"), Decimal("2")),))
        self.assertIsNone(self.rows("profits")[0]["net"])
        observations = [json.loads(row["payload_json"]) for row in self.rows("profit_observations")]
        self.assertEqual({row["attribution_status"] for row in observations}, {"reconciled", "unconfirmed"})
        self.assertTrue(any(row["rows"] is None for row in observations))
        self.assertTrue(any(row["net"] == "19" for row in observations))

    def test_failed_cost_persistence_cancels_pending_summary_without_publishing_stale_profit(self):
        self.initialize()
        with sqlite3.connect(self.tracker.path) as db:
            db.execute("INSERT INTO profits VALUES ('default','20261026','paper','005930','19','180','prior observation')")
            db.execute("CREATE TRIGGER fail_profit BEFORE INSERT ON profit_observations BEGIN SELECT RAISE(ABORT,'private failure'); END")
        self.tracker._identity = session_identity(self.session)
        self.tracker._next_at = self.now
        self.tracker._cycle_started_at = self.now
        self.tracker._tasks = deque([("profit", "20261026", "005930"), ("summary", "20261026", None)])
        self.tracker.profit_fetcher = Mock(return_value=())
        notify = Mock()
        with patch.object(self.tracker, "_summary") as summary:
            self.tracker.tick(self.session, notify)
        summary.assert_not_called()
        self.assertFalse(self.tracker._tasks)
        self.assertIsNone(self.rows("profits")[0]["net"])
        self.assertEqual(self.rows("summaries"), [])
        self.assertNotIn("private failure", str(notify.call_args))
        self.assertNotIn("일일결산", str(notify.call_args))

    def test_profit_provenance_failure_does_not_publish_new_profit_projection(self):
        self.initialize()
        with sqlite3.connect(self.tracker.path) as db:
            db.execute("CREATE TRIGGER fail_profit BEFORE INSERT ON profit_observations BEGIN SELECT RAISE(ABORT,'failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.tracker._profit("paper", "20261026", "005930", ())
        self.assertEqual(self.rows("profits"), [])

    def test_golden_monitor_captures_signal_snapshot_with_synthetic_source(self):
        settings = {"gdcrs_enabled": True, "gdcrs_short_period": 1, "gdcrs_long_period": 3,
            "gdcrs_stocks": [{"code": "005930", "budget": 225225}], "market_start_time": "09:00", "market_end_time": "15:30"}
        start = NOW.replace(minute=0, second=0)
        chart = Mock(return_value=tuple(MinuteBar(start + timedelta(minutes=i), close) for i, close in enumerate([3, 2, 1])))
        def buy(text, session, *, before_send):
            before_send()
            order_context.prepare_tracking("paper", "buy", "005930", "1", price="213500")
            return ["offline only"]
        monitor = GoldenCrossMonitor(settings_loader=lambda: settings, chart_fetcher=chart, buy_handler=buy,
            chart_source_kind="synthetic", now=lambda: self.now, state_path=self.root / "golden.sqlite")
        self.now = NOW.replace(minute=3)
        with order_context.automated_orders(self.tracker, "gdcrs"):
            monitor.tick(self.session, Mock())
            self.now = NOW
            chart.return_value = tuple(MinuteBar(start + timedelta(minutes=i), close) for i, close in enumerate([3, 2, 1, 4]))
            def evaluate(window, short, long):
                result = sma_difference(window, short, long)
                self.now += timedelta(milliseconds=1)
                return result
            with patch("kiwoom_autotrader.trading.golden_cross.sma_difference", side_effect=evaluate):
                monitor.tick(self.session, Mock())
        payload = json.loads(self.rows("order_decisions")[0]["evidence_json"])
        self.assertEqual((payload["source_kind"], payload["budget"], payload["short_period"], payload["long_period"]), ("synthetic", 225225, 1, 3))
        self.assertEqual(payload["bars"][-1]["close"], 4)
        self.assertEqual(payload["candle_end"], NOW.replace(second=0).isoformat())
        self.assertIsNone(payload["source_api"])
        self.assertIsNone(payload["source_revision"])
        self.assertEqual(len(payload["strategy_source_sha256"]), 64)
        self.assertEqual(payload["observed_at"], NOW.isoformat())
        self.assertEqual(payload["evaluated_at"], (NOW + timedelta(milliseconds=1)).isoformat())
        self.assertEqual(payload["run_scope"], "monitor_instance")
        self.assertEqual(payload["strategy_source_scope"], "monitor_module")
        self.assertEqual(payload["source_hash_scope"], "normalized_completed_window")
        settings["gdcrs_stocks"][0]["budget"] = 1
        self.assertEqual(json.loads(self.rows("order_decisions")[0]["evidence_json"])["budget"], 225225)

    def test_death_monitor_keeps_unknown_injected_source_and_no_budget(self):
        settings = {"ddcrs_enabled": True, "gdcrs_short_period": 1, "gdcrs_long_period": 3,
                    "market_start_time": "09:00", "market_end_time": "15:30"}
        report = {"mode": "paper", "exchange": "KRX", "snapshot_changed": False,
            "holdings": [{"stk_cd": "005930", "trde_able_qty": "2", "rmnd_qty": "2", "crd_tp": "00"}]}
        start = NOW.replace(minute=0, second=0)
        chart = Mock(return_value=tuple(MinuteBar(start + timedelta(minutes=i), close) for i, close in enumerate([1, 2, 3])))
        def sell(text, session, *, before_send):
            before_send()
            order_context.prepare_tracking("paper", "sell", "005930", "2", price=None)
            return ["offline only"]
        monitor = CrossMonitor(settings_loader=lambda: settings, account_fetcher=lambda _: report,
            chart_fetcher=chart, sell_handler=sell, now=lambda: self.now, state_path=self.root / "death.sqlite",
            # This test isolates evidence serialization. The ownership gate has
            # separate actual-ledger end-to-end and deny-path regressions.
            ownership_reader=lambda *_: 2)
        self.now = NOW.replace(minute=3)
        with order_context.automated_orders(self.tracker, "ddcrs"):
            monitor.tick(self.session, Mock()); self.now += timedelta(seconds=2); monitor.tick(self.session, Mock())
            self.now = NOW
            chart.return_value = tuple(MinuteBar(start + timedelta(minutes=i), close) for i, close in enumerate([1, 2, 3, 1]))
            monitor.tick(self.session, Mock()); self.now += timedelta(seconds=2); monitor.tick(self.session, Mock())
        decision = self.rows("order_decisions")[0]
        payload = json.loads(decision["evidence_json"])
        self.assertEqual(payload["source_kind"], "unknown")
        self.assertEqual(payload["strategy"], "ddcrs")
        self.assertIsNone(payload["budget"])
        self.assertEqual(decision["order_type"], "market")
        self.assertIsNone(decision["order_price"])


if __name__ == "__main__":
    unittest.main()
