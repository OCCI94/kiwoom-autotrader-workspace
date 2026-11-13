"""Durable automatic-order attribution with fake transport and listener clients."""

import contextlib
from datetime import datetime
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import Mock, call, patch

from kiwoom_autotrader.api import orders
from kiwoom_autotrader.api.session import BrokerSession, IssuedToken, KST
from kiwoom_autotrader.config import settings
from kiwoom_autotrader.telegram import listener
from kiwoom_autotrader.telegram.bot_credentials import TelegramConfig
from kiwoom_autotrader.trading import order_context
from kiwoom_autotrader.trading.execution_log import ExecutionTracker
from kiwoom_autotrader.utils.http_queue import HTTPNotSentError
from kiwoom_autotrader.utils.keychain import Credentials
from tests.paper_reconciliation_fixture import fake_reconciliation


SECRET = "private-order-tracking-secret"
TOKEN = "offline-order-tracking-token"
NOW = datetime(2026, 10, 26, 10, 0, 5, tzinfo=KST)
CONFIG = TelegramConfig("123456789:order_tracking_fixture_abcdefghijklmnopqrstuvwxyz", 778899001, 123456789, "tracking_test_bot")


def update(identifier, text):
    return {"update_id": identifier, "message": {
        "date": 1000, "text": text, "chat": {"id": CONFIG.chat_id, "type": "private"},
        "from": {"id": CONFIG.chat_id, "is_bot": False},
    }}


class TrackingTestCase(unittest.TestCase):
    def setUp(self):
        recorder_patch = patch("kiwoom_autotrader.telegram.listener.ExecutionStreamRecorder")
        self.addCleanup(recorder_patch.stop)
        self.execution_recorder = recorder_patch.start().return_value
        self.execution_recorder.status.return_value = {"ready": True, "error": None, "coverage_complete": True}
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        reset_token = order_context._current.set(None)
        self.addCleanup(order_context._current.reset, reset_token)
        self.network = self.patch_path("socket.socket.connect", side_effect=AssertionError("Live network forbidden"))
        self.keychain = self.patch_path("kiwoom_autotrader.utils.keychain._get_backend", side_effect=AssertionError("Live Keychain forbidden"))
        self.session = BrokerSession(now=lambda: NOW,
                                     credential_loader=Mock(return_value=Credentials("offline-key", SECRET)),
                                     token_issuer=Mock(return_value=IssuedToken(TOKEN, "20300101090000")))
        self.session.mode = "paper"
        self.session._issued = IssuedToken(TOKEN, "20300101090000")

    def patch(self, owner, name, **kwargs):
        patcher = patch.object(owner, name, **kwargs)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def patch_path(self, target, **kwargs):
        patcher = patch(target, **kwargs)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def tearDown(self):
        self.network.assert_not_called()
        self.keychain.assert_not_called()


class OrderTrackingTests(TrackingTestCase):
    def setUp(self):
        super().setUp()
        # Tests exercise intent/transport behavior without reading a running
        # user's active policy or broker account.
        self.patch_path("kiwoom_autotrader.trading.paper_policy.load_settings",
                        return_value={"paper_automation_only": False})
        self.session.paper_automation_reconcile = Mock(side_effect=fake_reconciliation)
        forbidden = Mock(side_effect=AssertionError("No broker reads for tracking registration"))
        self.tracker = ExecutionTracker(log_dir=self.root / "logs", now=lambda: NOW,
                                        settings_loader=lambda: {"market_end_time": "15:30"},
                                        account_fetcher=forbidden, executions_fetcher=forbidden,
                                        profit_fetcher=forbidden)
        self.opener = self.patch(orders.urllib.request, "build_opener")
        self.queued = self.patch(orders, "queued_open", side_effect=self.transport)
        self.payload = {"return_code": 0, "ord_no": "00024", "dmst_stex_tp": "KRX"}
        self.sent = 0

    def transport(self, opener, request, **options):
        try:
            options["before_send"]()
        except Exception:
            raise HTTPNotSentError("offline guard rejected before transport") from None
        self.sent += 1
        return io.BytesIO(json.dumps(self.payload).encode())

    def rows(self, table):
        if not self.tracker.path.exists():
            return []
        with sqlite3.connect(self.tracker.path) as connection:
            connection.row_factory = sqlite3.Row
            return [dict(row) for row in connection.execute("SELECT * FROM " + table)]

    def submit(self, *, strategy="ddcrs", before_send=None):
        with order_context.automated_orders(self.tracker, strategy):
            return orders.submit_order(self.session, "sell", "005930", "0002", before_send=before_send)

    def test_intent_is_committed_before_transport_and_accepted_receipt_links_exact_strategy_once(self):
        def transport(opener, request, **options):
            prepared = self.rows("intents")  # independent SQLite reader proves committed state
            self.assertEqual(len(prepared), 1)
            self.assertEqual((prepared[0]["state"], prepared[0]["strategy"], prepared[0]["quantity"]),
                             ("prepared", "rsv#7", 2))
            self.assertIsNone(prepared[0]["order_number"])
            self.assertEqual(self.rows("orders"), [])
            return self.transport(opener, request, **options)
        self.queued.side_effect = transport
        with patch.object(self.tracker, "register_order", wraps=self.tracker.register_order) as register:
            receipt = self.submit(strategy="rsv#7")
        self.queued.assert_called_once()
        self.assertEqual(self.sent, 1)
        intents, accepted = self.rows("intents"), self.rows("orders")
        register.assert_called_once_with(receipt, "rsv#7", intent_id=intents[0]["id"])
        self.assertEqual((intents[0]["state"], intents[0]["order_number"]), ("accepted", "24"))
        self.assertEqual(len(accepted), 1)
        self.assertEqual((accepted[0]["strategy"], accepted[0]["side"], accepted[0]["quantity"]), ("rsv#7", "sell", 2))
        self.assertEqual(self.rows("fills"), [])
        self.assertFalse((self.root / "logs" / "20261026.csv").exists())
        self.assertNotIn(TOKEN.encode(), self.tracker.path.read_bytes())
        self.assertNotIn(SECRET.encode(), self.tracker.path.read_bytes())

    def test_manual_order_without_context_does_not_create_automatic_journal(self):
        with patch.object(self.tracker, "prepare_order") as prepare, patch.object(self.tracker, "register_order") as register:
            receipt = orders.submit_order(self.session, "buy", "005930", "1", "70000")
        self.assertEqual(receipt.side, "buy")
        prepare.assert_not_called()
        register.assert_not_called()
        self.assertFalse(self.tracker.path.exists())
        self.assertEqual(self.sent, 1)

    def test_preparation_failure_prevents_even_queued_http_and_is_sanitized(self):
        with patch.object(self.tracker, "prepare_order", side_effect=OSError(SECRET)):
            with self.assertRaises(orders.OrderError) as caught:
                self.submit()
        self.assertFalse(caught.exception.uncertain)
        self.assertIn("사전 기록", str(caught.exception))
        self.assertNotIn(SECRET, str(caught.exception))
        self.queued.assert_not_called()
        self.opener.assert_not_called()
        self.assertEqual(self.sent, 0)

    def test_theme_rejections_keep_exact_reason_before_and_after_queue(self):
        from kiwoom_autotrader.trading.execution_log import ThemeConcentrationLimitReached
        for stage in ('prepare_order','assert_paper_theme_dispatch'):
            with self.subTest(stage=stage),patch.object(self.tracker,stage,side_effect=ThemeConcentrationLimitReached('full')):
                with order_context.automated_orders(self.tracker,'gdcrs'),self.assertRaises(orders.OrderError) as caught:
                    orders.submit_order(self.session,'buy','005930','1','1000')
                self.assertEqual(caught.exception.reason_code,'theme_concentration_limit_reached',str(caught.exception))
                self.assertFalse(caught.exception.uncertain)
        self.assertEqual(self.sent,0)

    def test_receipt_link_failure_is_uncertain_and_never_resubmits_or_cancels_intent(self):
        with patch.object(self.tracker, "register_order", side_effect=OSError(SECRET)) as register, \
                patch.object(self.tracker, "cancel_intent", wraps=self.tracker.cancel_intent) as cancel:
            with self.assertRaises(orders.OrderError) as caught:
                self.submit()
        self.assertTrue(caught.exception.uncertain)
        self.assertIn("접수됐지만", str(caught.exception))
        self.assertNotIn(SECRET, str(caught.exception))
        self.queued.assert_called_once()
        register.assert_called_once()
        cancel.assert_not_called()
        self.assertEqual(self.sent, 1)
        self.assertEqual(self.rows("intents")[0]["state"], "prepared")
        self.assertEqual(self.rows("orders"), [])

    def test_http_timeout_leaves_prepared_intent_unresolved_without_retry(self):
        self.queued.side_effect = TimeoutError(SECRET)
        with patch.object(self.tracker, "cancel_intent", wraps=self.tracker.cancel_intent) as cancel:
            with self.assertRaises(orders.OrderError) as caught:
                self.submit()
        self.assertTrue(caught.exception.uncertain)
        self.assertNotIn(SECRET, str(caught.exception))
        self.queued.assert_called_once()
        cancel.assert_not_called()
        self.assertEqual(self.rows("intents")[0]["state"], "prepared")
        self.assertIsNone(self.rows("intents")[0]["order_number"])

    def test_guard_rejection_cancels_prepared_intent_without_transmitting(self):
        guard = Mock(side_effect=RuntimeError(SECRET))
        with self.assertRaises(orders.OrderError) as caught:
            self.submit(before_send=guard)
        self.assertFalse(caught.exception.uncertain)
        guard.assert_called_once_with()
        self.assertEqual(self.sent, 0)
        row = self.rows("intents")[0]
        self.assertEqual((row["state"], row["reason"], row["order_number"]), ("cancelled", "not_sent", None))
        self.assertEqual(self.rows("orders"), [])
        self.assertNotIn(SECRET, str(caught.exception))

    def test_recording_guard_invalidating_strategy_is_checked_again_before_send(self):
        valid = {"strategy": True}
        def strategy_guard():
            if not valid["strategy"]:
                raise RuntimeError("late strategy")
        def recorder_guard():
            # One check occurs before the intent; this one is in the send guard.
            if recorder.call_count >= 2:
                valid["strategy"] = False
        recorder = Mock(side_effect=recorder_guard)
        self.session.execution_recording_guard = recorder
        with self.assertRaises(orders.OrderError) as caught:
            self.submit(before_send=strategy_guard)
        self.assertFalse(caught.exception.uncertain)
        self.assertEqual(self.sent, 0)
        self.assertEqual(self.rows("intents")[0]["state"], "cancelled")

    def test_late_automatic_guard_invalidating_strategy_is_checked_again(self):
        valid = {"strategy": True}
        class AutomaticGuard:
            def __init__(self): self.calls = 0
            def before_prepare(self): return False
            def __call__(self):
                self.calls += 1
                if self.calls == 2: valid["strategy"] = False
        def strategy_guard():
            if not valid["strategy"]: raise RuntimeError("late strategy")
        automatic = AutomaticGuard()
        with patch.object(orders, "capture_automatic_paper_guard", return_value=automatic):
            with self.assertRaises(orders.OrderError) as caught:
                self.submit(before_send=strategy_guard)
        self.assertFalse(caught.exception.uncertain)
        self.assertEqual(automatic.calls, 2)
        self.assertEqual(self.sent, 0)
        self.assertEqual(self.rows("intents")[0]["state"], "cancelled")

    def test_session_change_during_final_strategy_guard_blocks_transport(self):
        guard = Mock()
        def check():
            if guard.call_count == 2: self.session.mode = "real"
        guard.side_effect = check
        with self.assertRaises(orders.OrderError) as caught:
            self.submit(before_send=guard)
        self.assertFalse(caught.exception.uncertain)
        self.assertEqual(guard.call_count, 2)
        self.assertEqual(self.sent, 0)
        self.assertEqual(self.rows("intents")[0]["state"], "cancelled")

    def test_known_broker_rejection_cancels_intent_once(self):
        self.payload = {"return_code": 123, "return_msg": SECRET, "ord_no": "00024"}
        with patch.object(self.tracker, "cancel_intent", wraps=self.tracker.cancel_intent) as cancel:
            with self.assertRaises(orders.OrderError) as caught:
                self.submit()
        self.assertFalse(caught.exception.uncertain)
        self.assertNotIn(SECRET, str(caught.exception))
        self.queued.assert_called_once()
        self.assertEqual(self.sent, 1)
        row = self.rows("intents")[0]
        cancel.assert_called_once_with(row["id"], "rejected")
        self.assertEqual((row["state"], row["reason"]), ("cancelled", "rejected"))
        self.assertEqual(self.rows("orders"), [])

    def test_unverifiable_success_never_cancels_or_fabricates_an_order_number(self):
        self.payload = {"return_code": 0, "ord_no": SECRET}
        with patch.object(self.tracker, "cancel_intent", wraps=self.tracker.cancel_intent) as cancel:
            with self.assertRaises(orders.OrderError) as caught:
                self.submit()
        self.assertTrue(caught.exception.uncertain)
        cancel.assert_not_called()
        self.assertEqual(self.rows("intents")[0]["state"], "prepared")
        self.assertEqual(self.rows("orders"), [])
        self.assertNotIn(SECRET, str(caught.exception))


class AutomaticOrderContextTests(TrackingTestCase):
    def test_nested_context_and_exception_restore_the_original_context(self):
        first, second = Mock(), Mock()
        first.prepare_order.return_value = "outer-intent"
        second.prepare_order.return_value = "inner-intent"
        args = ("paper", "buy", "005930", "1")
        self.assertIsNone(order_context.prepare_tracking(*args))
        with order_context.automated_orders(first, "rsv#1"):
            outer = order_context.prepare_tracking(*args)
            with self.assertRaisesRegex(RuntimeError, "test interruption"):
                with order_context.automated_orders(second, "ddcrs"):
                    inner = order_context.prepare_tracking(*args)
                    self.assertEqual(inner[0].strategy, "ddcrs")
                    raise RuntimeError("test interruption")
            self.assertEqual(order_context.prepare_tracking(*args)[0].strategy, "rsv#1")
        self.assertIsNone(order_context.prepare_tracking(*args))
        self.assertEqual(outer[0].tracker, first)
        self.assertEqual(outer[1], "outer-intent")

    def test_completed_receipt_uses_captured_context_after_scope_has_exited(self):
        tracker = Mock()
        tracker.prepare_order.return_value = "intent-1"
        receipt = orders.OrderReceipt("paper", "buy", "005930", "1", "70000", "00024", "https://mockapi.kiwoom.com")
        with order_context.automated_orders(tracker, "rsv#12"):
            captured = order_context.prepare_tracking("paper", "buy", "005930", "1")
        order_context.complete_tracking(captured, receipt)
        tracker.register_order.assert_called_once_with(receipt, "rsv#12", intent_id="intent-1")
        self.assertIsNone(order_context._current.get())

    def test_context_is_not_inherited_by_unrelated_background_threads(self):
        tracker = Mock()
        observed = []
        with order_context.automated_orders(tracker, "ddcrs"):
            worker = threading.Thread(target=lambda: observed.append(order_context.prepare_tracking("paper", "sell", "005930", "1")))
            worker.start()
            worker.join(timeout=1)
            self.assertFalse(worker.is_alive())
        self.assertEqual(observed, [None])
        tracker.prepare_order.assert_not_called()

    def test_invalid_context_cannot_change_existing_manual_scope(self):
        for tracker, strategy in ((None, "ddcrs"), (Mock(), None), (Mock(), ""), (Mock(), 7)):
            with self.subTest(strategy=strategy), self.assertRaises(ValueError):
                with order_context.automated_orders(tracker, strategy):
                    self.fail("invalid context was entered")
            self.assertIsNone(order_context._current.get())


class TrackingListenerTests(TrackingTestCase):
    def setUp(self):
        super().setUp()
        self.receiver = self.root / "receiver"
        self.patch(settings, "settings_path", return_value=self.root / "settings.json")
        self.patch(listener, "load_telegram_config", return_value=CONFIG)
        self.patch(listener.time, "time", return_value=1000)
        self.telegram = self.patch(listener, "TelegramClient").return_value
        self.telegram.get_me.return_value = {"id": CONFIG.bot_id, "username": CONFIG.bot_username}
        self.telegram.get_webhook_info.return_value = {"url": ""}
        self.telegram.get_updates.return_value = [update(1, "buy 005930 1")]
        self.scheduler = Mock()
        self.scheduler.claim_due.side_effect = [{"id": 7, "time": "10:00", "command": "buy 005930 1"}, None]
        self.scheduler.seconds_until_next.return_value = None
        self.monitor = Mock()
        self.monitor.seconds_until_next.return_value = None
        self.websocket = Mock()
        self.websocket.snapshot.return_value = {"error": None}
        self.tracker = Mock()
        self.tracker.seconds_until_next.return_value = None
        self.observations = []
        def observe(kind):
            context = order_context._current.get()
            self.observations.append((kind, context.strategy if context else None, context.tracker if context else None))
        def buy(text, session, *, before_send):
            before_send()
            observe("buy")
            return ["offline buy result"]
        self.buy = self.patch(listener, "handle_buy", side_effect=buy)
        self.monitor.tick.side_effect = lambda *args: observe("monitor")
        self.tracker.tick.side_effect = lambda *args: observe("tracker")

    def receive(self):
        with contextlib.redirect_stdout(io.StringIO()):
            return listener.run_listener(self.receiver, max_polls=1, broker_session=self.session,
                                         scheduler=self.scheduler, monitor=self.monitor,
                                         websocket_engine=self.websocket, execution_tracker=self.tracker)

    def test_reserved_and_monitor_commands_have_explicit_context_but_manual_and_tracker_do_not(self):
        self.assertEqual(self.receive(), 0)
        self.assertEqual(self.observations, [
            ("tracker", None, None),
            ("buy", "rsv#7", self.tracker),
            ("buy", None, None),
            ("monitor", "ddcrs", self.tracker),
            ("tracker", None, None),
        ])
        self.assertEqual(self.buy.call_count, 2)
        self.assertIsNone(order_context._current.get())
        self.websocket.close.assert_called_once_with()
        self.assertIsNone(self.session.token)

    def test_exception_in_reserved_dispatch_does_not_leak_context_into_later_work(self):
        self.buy.side_effect = RuntimeError(SECRET)
        with self.assertRaises(listener.TelegramListenerError) as caught:
            self.receive()
        self.assertNotIn(SECRET, str(caught.exception))
        self.assertIsNone(order_context._current.get())
        self.monitor.tick.assert_not_called()
        self.websocket.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
