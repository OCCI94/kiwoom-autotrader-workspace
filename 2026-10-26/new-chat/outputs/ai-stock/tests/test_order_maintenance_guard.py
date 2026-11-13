"""Maintenance is checked at actual dispatch; all transport/auth are fixtures."""
from datetime import datetime
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from kiwoom_autotrader.api import orders
from kiwoom_autotrader.api.session import BrokerSession, IssuedToken, KST
from kiwoom_autotrader.telegram import listener
from kiwoom_autotrader.utils.keychain import Credentials


class OrderMaintenanceGuardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.marker = self.root / "maintenance-quiet.lock"
        self.session = BrokerSession(now=lambda: datetime(2026, 10, 29, 16, tzinfo=KST),
            credential_loader=lambda _: Credentials("fixture-key", "fixture-secret"),
            token_issuer=lambda *_: IssuedToken("fixture-token", "20300101000000"))
        self.session.login("paper")
        self.session.maintenance_order_guard = lambda: listener._maintenance_order_guard(self.root)
        self.factory = patch.object(orders.urllib.request, "build_opener").start()
        self.addCleanup(patch.stopall)
        self.opener = self.factory.return_value
        self.opener.open.return_value.__enter__.return_value.read.return_value = json.dumps(
            {"return_code": 0, "ord_no": "00001"}).encode()

    def test_existing_marker_blocks_before_intent_and_transport(self):
        self.marker.touch()
        with patch.object(orders, "prepare_tracking") as prepare:
            with self.assertRaises(orders.OrderError) as raised:
                orders.submit_order(self.session, "sell", "005930", 1)
            self.assertFalse(raised.exception.uncertain)
            prepare.assert_not_called()
        self.opener.open.assert_not_called()

    def test_marker_created_in_final_strategy_check_prevents_transport(self):
        calls = []
        def check():
            calls.append(1)
            if len(calls) == 2:
                self.marker.touch()
        with patch.object(orders, "cancel_tracking") as cancel:
            with self.assertRaises(orders.OrderError) as raised:
                orders.submit_order(self.session, "sell", "005930", 1, before_send=check)
            self.assertFalse(raised.exception.uncertain)
            cancel.assert_called_once()
        self.opener.open.assert_not_called()

    def test_removed_guard_during_queue_fails_closed(self):
        def check():
            self.session.maintenance_order_guard = None
        with self.assertRaises(orders.OrderError) as raised:
            orders.submit_order(self.session, "sell", "005930", 1, before_send=check)
        self.assertFalse(raised.exception.uncertain)
        self.opener.open.assert_not_called()

    def test_io_error_and_invalid_guard_fail_closed(self):
        with patch.object(listener, "_maintenance_paused", return_value=True):
            with self.assertRaises(orders.OrderError):
                orders.submit_order(self.session, "sell", "005930", 1)
        self.session.maintenance_order_guard = "invalid"
        with self.assertRaises(orders.OrderError):
            orders.submit_order(self.session, "sell", "005930", 1)
        self.opener.open.assert_not_called()

    def test_release_allows_next_explicit_call_and_legacy_sessions_compatible(self):
        self.marker.touch()
        with self.assertRaises(orders.OrderError):
            orders.submit_order(self.session, "sell", "005930", 1)
        self.marker.unlink()
        self.assertEqual(orders.submit_order(self.session, "sell", "005930", 1).order_number, "00001")
        del self.session.maintenance_order_guard
        orders.submit_order(self.session, "sell", "005930", 1)
        self.assertEqual(self.opener.open.call_count, 2)


if __name__ == "__main__":
    unittest.main()
