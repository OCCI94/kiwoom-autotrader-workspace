"""Offline checks for prioritizing paper balance snapshots during backlogs."""
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from kiwoom_autotrader.api.session import BrokerSession, IssuedToken, KST
from kiwoom_autotrader.trading.execution_log import ExecutionTracker, session_identity


class BalanceRefreshScheduleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        # Historical backlog fairness is an out-of-session contract. During
        # regular hours only current-day reconciliation is eligible.
        self.now = datetime(2026, 10, 29, 16, 0, tzinfo=KST)
        self.session = BrokerSession(now=lambda: self.now)
        self.session.mode = "paper"
        self.session._issued = IssuedToken("offline-only-test-token", "20300101000000")
        self.account = Mock(return_value={"offline": True})
        self.executions = Mock(return_value=())
        self.profit = Mock(return_value=())
        self.tracker = ExecutionTracker(log_dir=Path(self.tmp.name), now=lambda: self.now,
            account_fetcher=self.account, executions_fetcher=self.executions,
            profit_fetcher=self.profit, closed_calendar_resolver=lambda now, day: {})
        self.tracker._identity = session_identity(self.session)
        self.tracker._cycle_started_at = self.now - timedelta(minutes=2)
        self.tracker._balance_refresh_at = self.now - timedelta(seconds=1)
        self.tracker._holdings = Mock()
        self.tracker._save_paper_balance_snapshot = Mock()
        self.tracker._invalidate_profit = Mock()
        self.tracker._profit = Mock()
        self.tracker._ingest = Mock()

    def tick(self):
        self.tracker.tick(self.session, Mock())

    def test_overdue_refresh_preserves_profit_backlog_and_runs_once(self):
        tasks = [("profit", "20261028", "003220"), ("profit", "20261027", "097520")]
        self.tracker._tasks = deque(tasks)
        self.tick()
        self.account.assert_called_once_with(self.session)
        self.assertEqual(list(self.tracker._tasks), tasks)
        self.assertEqual(self.tracker._balance_refresh_at, self.now + timedelta(seconds=60))
        self.tick()
        self.profit.assert_called_once_with(self.session, "003220", "20261028")
        self.assertEqual(self.account.call_count, 1)

    def test_pending_execution_is_not_overtaken(self):
        self.tracker._tasks = deque([("executions", "20261029", None),
                                     ("profit", "20261028", "003220")])
        self.tick()
        self.executions.assert_called_once()
        self.account.assert_not_called()
        self.tick()
        self.account.assert_called_once_with(self.session)

    def test_regular_holdings_task_does_not_duplicate_refresh(self):
        self.tracker._tasks = deque([("holdings", "20261029", None)])
        self.tracker._cycle_days = []
        self.tick()
        self.account.assert_called_once_with(self.session)
        self.assertEqual(self.tracker._balance_refresh_at, self.now + timedelta(seconds=60))
        self.assertFalse(self.tracker._tasks)

    def test_real_mode_does_not_get_extra_balance_reads(self):
        self.session.mode = "real"
        self.tracker._identity = session_identity(self.session)
        self.tracker._tasks = deque([("profit", "20261028", "003220")])
        self.tick()
        self.account.assert_not_called()
        self.profit.assert_called_once()

    def test_repeated_overdue_refreshes_cannot_starve_profit_backlog(self):
        tasks = [("profit", "20261028", code) for code in
                 ("003220", "097520", "072950", "005930")]
        self.tracker._tasks = deque(tasks)
        for i in range(len(tasks)):
            self.now += timedelta(seconds=70)
            self.tick()  # overdue paper balance
            self.assertEqual(self.account.call_count, i + 1)
            self.tick()  # original queued profit still advances
            self.assertEqual(self.profit.call_count, i + 1)
        self.assertFalse(self.tracker._tasks)

    def test_intraday_mixed_queue_keeps_today_account_and_defers_old_profit(self):
        self.now = self.now.replace(hour=11, minute=20)
        self.tracker._tasks = deque([("profit", "20261028", "003220"),
                                     ("holdings", "20261029", None)])
        self.tracker._cycle_days = ["20261028", "20261029"]
        self.tracker._next_at = self.now + timedelta(minutes=10)
        self.tick()
        self.account.assert_called_once_with(self.session)
        self.tracker._holdings.assert_called_once()
        self.tracker._save_paper_balance_snapshot.assert_called_once()
        self.profit.assert_not_called()
        self.executions.assert_not_called()
        self.assertNotIn("20261028", self.tracker._cycle_days)
        self.assertFalse(any(task[1] == "20261028" for task in self.tracker._tasks))
        self.assertTrue(self.tracker._historical_guard_status['deferred'])


if __name__ == "__main__":
    unittest.main()
