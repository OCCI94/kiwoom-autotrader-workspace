"""Offline regression for reconciliation notifications; no real broker."""
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from kiwoom_autotrader.api.minute_chart import session_identity
from kiwoom_autotrader.api.session import BrokerSession, IssuedToken, KST
from kiwoom_autotrader.trading import execution_log as mod


class CooldownTests(unittest.TestCase):
    def setUp(self):
        self.base = datetime(2026, 11, 5, 18, 0, tzinfo=KST)
        self.now = self.base
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.session = BrokerSession(now=lambda: self.now)
        self.session.mode = "paper"
        self.session._issued = IssuedToken("offline-test-token", "20300101000000")
        self.tracker = mod.ExecutionTracker(log_dir=Path(temp.name), now=lambda: self.now,
            settings_loader=lambda: {"market_end_time": "15:30"}, account_fetcher=Mock(return_value={"holdings": []}),
            executions_fetcher=Mock(return_value=()), profit_fetcher=Mock(return_value=()),
            closed_day_backoff=False)
        self.tracker._identity = session_identity(self.session)
        self.notify = Mock()

    def task(self, action, elapsed, error=None):
        self.now = self.base + timedelta(seconds=elapsed)
        self.tracker._tasks = deque([(action, self.now.strftime("%Y%m%d"), None)])
        self.tracker._cycle_started_at = self.now
        self.tracker._next_at = None
        self.tracker._balance_refresh_at = self.now + timedelta(minutes=1)
        with (
            patch.object(mod, "monotonic", return_value=float(elapsed), create=True),
            patch.object(self.tracker, "_closed_finish", return_value=True, side_effect=error),
            patch.object(self.tracker, "_holdings"),
            patch.object(self.tracker, "_save_paper_balance_snapshot"),
        ):
            self.tracker.tick(self.session, self.notify)

    def test_same_error_survives_regular_success_and_balance_refresh(self):
        self.task("closed_complete", 0, OSError("synthetic secret"))
        self.task("closed_complete", 1)
        self.task("balance_refresh", 2)
        self.task("closed_complete", 3, OSError("different synthetic secret"))
        self.notify.assert_called_once()
        self.assertNotIn("synthetic secret", self.notify.call_args.args[0])
        self.task("closed_complete", 599, OSError())
        self.notify.assert_called_once()
        self.task("closed_complete", 600, OSError())
        self.assertEqual(self.notify.call_count, 2)

    def test_distinct_warning_is_immediate_and_alternation_is_deduplicated(self):
        self.task("closed_complete", 0, OSError())
        self.task("closed_complete", 1, mod.ExecutionLogError("explicit safe error"))
        self.task("closed_complete", 2, OSError())
        self.task("closed_complete", 3, mod.ExecutionLogError("explicit safe error"))
        self.assertEqual(self.notify.call_count, 2)
        self.assertEqual(self.notify.call_args.args, ("explicit safe error",))

    def test_delivery_failure_does_not_make_retry_burst(self):
        self.notify.side_effect = OSError("delivery uncertain")
        self.task("closed_complete", 0, OSError())
        self.task("closed_complete", 1, OSError())
        self.notify.assert_called_once()
