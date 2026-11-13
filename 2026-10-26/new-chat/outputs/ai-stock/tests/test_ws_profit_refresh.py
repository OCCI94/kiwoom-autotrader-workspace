"""WS fill-triggered profit refresh remains local, bounded and isolated."""
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from kiwoom_autotrader.api.session import BrokerSession, IssuedToken, KST
from kiwoom_autotrader.api.minute_chart import session_identity
from kiwoom_autotrader.trading.execution_log import ExecutionTracker


class WebsocketProfitRefreshTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.now = datetime(2026, 10, 28, 12, 50, tzinfo=KST)
        self.session = BrokerSession(now=lambda: self.now)
        self.session.mode = "paper"
        self.session._issued = IssuedToken("fake-offline-token", "20300101000000")
        self.profit = Mock(return_value=())
        self.period = Mock()
        self.daily = Mock()
        self.tracker = ExecutionTracker(log_dir=Path(self.temp.name), now=lambda: self.now,
            account_fetcher=Mock(return_value={"mode":"paper", "exchange":"KRX", "snapshot_changed":False, "holdings":[]}),
            executions_fetcher=Mock(return_value=()), profit_fetcher=self.profit,
            period_profit_fetcher=self.period, daily_profit_fetcher=self.daily)
        self.tracker._identity = session_identity(self.session)
        self.tracker._next_at = self.now + timedelta(seconds=30)
        self.tracker._cycle_started_at = self.now
        self.tracker._tasks = deque([("holdings", "20261028", None)])
        self.tracker._profit_refresh_at[("paper", "20261028", "072950")] = self.now + timedelta(minutes=1)
        self.tracker._daily_profit_refresh_at[("paper", "20261028")] = self.now + timedelta(minutes=1)

    def event(self, **values):
        fields = {"9001": "A072950", "907": "1", "913": "체결", "9203": "158477", "909": "27", "914": "10000", "915": "63"}
        fields.update(values)
        return {"schema_version": 1, "source_api": "00", "source_kind": "broker_observation",
            "observed_at": self.now.isoformat(), "day": "20261028", "mode": "paper",
            "event_kind": "execution_stream", "values": fields}

    def test_sell_fill_invalidates_and_schedules_all_three_on_next_ticks(self):
        self.tracker.record_broker_observation(self.event())
        self.assertNotIn(("paper", "20261028", "072950"), self.tracker._profit_refresh_at)
        self.assertNotIn(("paper", "20261028"), self.tracker._daily_profit_refresh_at)
        for _ in range(5):
            self.tracker.tick(self.session, Mock())
        self.profit.assert_called_once_with(self.session, "072950", "20261028")
        self.period.assert_called_once()
        self.daily.assert_called_once()

    def test_repeated_partial_deliveries_coalesce_before_tick(self):
        self.tracker.record_broker_observation(self.event())
        self.tracker.record_broker_observation(self.event())
        self.assertEqual(self.tracker._sell_refresh_pending, {("paper", "20261028", "072950")})
        for _ in range(3):
            self.tracker.tick(self.session, Mock())
        self.profit.assert_called_once()
        self.tracker.record_broker_observation(self.event())
        self.assertEqual(self.tracker._sell_refresh_pending, set())

    def test_existing_queued_refresh_is_promoted_to_front(self):
        self.tracker._tasks.extend([("profit", "20261028", "072950"), ("daily_profit", "20261028", None)])
        self.tracker.record_broker_observation(self.event())
        for _ in range(3):
            self.tracker.tick(self.session, Mock())
        self.profit.assert_called_once()
        self.assertEqual(sum(task == ("daily_profit", "20261028", None) for task in self.tracker._tasks), 1)

    def test_cumulative_fill_without_unit_quantity_also_refreshes(self):
        event = self.event(**{"915": "", "911": "63"})
        self.tracker.record_broker_observation(event)
        self.assertEqual(self.tracker._sell_refresh_pending, {("paper", "20261028", "072950")})

    def test_buy_receipt_or_zero_unit_does_not_refresh(self):
        for event in (self.event(**{"907": "2"}), self.event(**{"913": "접수"})):
            self.tracker.record_broker_observation(event)
        self.assertEqual(self.tracker._sell_refresh_pending, set())
        self.assertIn(("paper", "20261028", "072950"), self.tracker._profit_refresh_at)
        self.assertIn(("paper", "20261028"), self.tracker._daily_profit_refresh_at)

    def test_missing_fill_id_with_cumulative_quantity_invalidates_and_coalesces(self):
        event = self.event(**{"909": "", "915": "0", "911": "3"})
        self.tracker.record_broker_observation(event)
        self.tracker.record_broker_observation(event)
        self.assertEqual(self.tracker._sell_refresh_pending, {("paper", "20261028", "072950")})
        with self.tracker._db() as db:
            result = db.execute("SELECT net FROM profits WHERE symbol='072950'").fetchone()
        self.assertIsNotNone(result)
        self.assertIsNone(result["net"])

    def test_sell_execution_with_missing_quantities_is_unconfirmed_and_coalesced(self):
        event = self.event(**{"909": "", "915": "", "911": ""})
        self.tracker.record_broker_observation(event)
        self.tracker.record_broker_observation(event)
        self.assertEqual(self.tracker._sell_refresh_pending, {("paper", "20261028", "072950")})
        with self.tracker._db() as db:
            result = db.execute("SELECT net FROM profits WHERE symbol='072950'").fetchone()
        self.assertIsNone(result["net"])
        # An identical incomplete event received after the prior work has
        # drained could be a different fill. It must invalidate again.
        self.tracker._sell_refresh_pending.clear()
        with self.tracker._db() as db:
            db.execute("INSERT OR REPLACE INTO profits VALUES (?,?,?,?,?,?,?)",
                (self.tracker.scope, "20261028", "paper", "072950", "5", "100", "reconciled"))
        self.tracker.record_broker_observation(event)
        self.assertIn(("paper", "20261028", "072950"), self.tracker._sell_refresh_pending)
        with self.tracker._db() as db:
            self.assertIsNone(db.execute("SELECT net FROM profits WHERE symbol='072950'").fetchone()[0])

    def test_other_mode_does_not_run_under_paper_session(self):
        event = self.event()
        event["mode"] = "real"
        self.tracker.record_broker_observation(event)
        self.tracker.tick(self.session, Mock())
        self.profit.assert_not_called()
        self.assertIn(("real", "20261028", "072950"), self.tracker._sell_refresh_pending)


if __name__ == "__main__":
    unittest.main()
