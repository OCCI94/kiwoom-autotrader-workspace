"""PAPER automation limits with a fake HTTP queue and fake durable tracker."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from decimal import Decimal
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from kiwoom_autotrader.api import orders
from kiwoom_autotrader.api.session import BrokerSession, IssuedToken, KST
from kiwoom_autotrader.api.executions import Execution
from kiwoom_autotrader.trading import order_context, paper_policy
from kiwoom_autotrader.trading.execution_log import ExecutionTracker
from kiwoom_autotrader.utils.http_queue import HTTPNotSentError
from tests.paper_reconciliation_fixture import fake_reconciliation


class PaperPolicyTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 27, 13, 0, tzinfo=KST)
        self.session = BrokerSession(now=lambda: self.now)
        self.session.mode = "paper"
        self.session._issued = IssuedToken("offline-policy-token", "20300101000000")
        self.session.paper_automation_reconcile = Mock(side_effect=fake_reconciliation)
        self.settings = {"paper_automation_only": True, "paper_automation_buy_cap": 1000000}
        self.tracker = Mock(spec=["assert_no_pending_order", "prepare_order", "register_order", "cancel_intent"])
        self.tracker.prepare_order.return_value = "prepared-intent"
        self.sent = []
        self.after_enqueue = None
        self.worker_contexts = []
        self.settings_mock = self.patch(paper_policy, "load_settings", side_effect=lambda **kwargs: dict(self.settings))
        self.queue = self.patch(orders, "queued_open", side_effect=self.transport)
        self.opener = self.patch(orders.urllib.request, "build_opener")
        context = order_context._current.set(None)
        self.addCleanup(order_context._current.reset, context)
        for target in ("socket.socket.connect", "kiwoom_autotrader.utils.keychain._get_backend"):
            patched = patch(target, side_effect=AssertionError("Live access forbidden"))
            mock = patched.start(); self.addCleanup(patched.stop); self.addCleanup(mock.assert_not_called)

    def patch(self, owner, name, **kwargs):
        patched = patch.object(owner, name, **kwargs)
        self.addCleanup(patched.stop)
        return patched.start()

    def transport(self, opener, request, **options):
        if self.after_enqueue is not None: self.after_enqueue()
        def dispatch():
            self.worker_contexts.append(order_context._current.get())
            try: options["before_send"]()
            except Exception: raise HTTPNotSentError("offline no-send") from None
            self.sent.append(request)
            return io.BytesIO(json.dumps({"return_code":0,"ord_no":str(100+len(self.sent))}).encode())
        # The production queue executes its guard outside the caller ContextVar.
        with ThreadPoolExecutor(max_workers=1) as worker:
            return worker.submit(dispatch).result()

    def automatic(self, side="buy", quantity="10", price="100000", *, before_send=None, strategy="gdcrs"):
        with order_context.automated_orders(self.tracker, strategy):
            return orders.submit_order(self.session, side, "097520", quantity, price, before_send=before_send)

    def assert_initial_refusal(self, callback):
        with self.assertRaises(orders.OrderError) as caught: callback()
        self.assertFalse(caught.exception.uncertain)
        self.queue.assert_not_called(); self.tracker.prepare_order.assert_not_called()
        self.assertEqual(self.sent, [])
        return caught.exception

    def test_exact_cap_is_accepted_and_context_is_captured_for_queue_worker(self):
        receipt = self.automatic()
        self.assertEqual(receipt.origin, "https://mockapi.kiwoom.com")
        self.assertEqual(len(self.sent), 1); self.assertEqual(self.worker_contexts, [None])
        self.tracker.assert_no_pending_order.assert_called_once_with("paper", "097520")
        args = self.tracker.prepare_order.call_args
        self.assertEqual(args.args, ("paper", "buy", "097520", "10", "gdcrs"))
        self.assertEqual((args.kwargs["price"], args.kwargs["require_no_pending"]), ("100000", True))
        self.assertEqual(args.kwargs["reconciliation_evidence"]["remaining_principal_krw"], 1000000)
        self.tracker.register_order.assert_called_once_with(receipt, "gdcrs", intent_id="prepared-intent")

    def test_multiple_completed_orders_are_not_a_daily_one_order_cap(self):
        self.automatic(); self.automatic()
        self.assertEqual(len(self.sent), 2)
        self.assertEqual(self.tracker.assert_no_pending_order.call_count, 2)

    def test_over_cap_and_market_automatic_buys_are_known_unsent(self):
        self.assert_initial_refusal(lambda: self.automatic(quantity="11"))
        self.queue.reset_mock(); self.tracker.reset_mock()
        self.assert_initial_refusal(lambda: self.automatic(price=None))

    def test_default_false_still_enforces_paper_cap_and_reconciliation(self):
        self.settings = {"paper_automation_only": False}
        self.assert_initial_refusal(lambda: self.automatic(quantity="11"))
        self.assert_initial_refusal(lambda: self.automatic(price=None))
        self.automatic()
        self.session.paper_automation_reconcile.assert_called_once_with("097520", side="buy", quantity="10", price="100000")
        self.tracker.assert_no_pending_order.assert_called_once_with("paper", "097520")

    def test_default_false_cannot_skip_missing_reconciliation(self):
        self.settings = {}
        self.session.paper_automation_reconcile = None
        self.assert_initial_refusal(lambda: self.automatic())

    def test_increased_cap_during_queue_is_also_rejected(self):
        self.settings["paper_automation_buy_cap"] = 900000
        self.after_enqueue = lambda: self.settings.update(paper_automation_buy_cap=1000000)
        with self.assertRaises(orders.OrderError): self.automatic(quantity="9")
        self.assertEqual(self.sent, [])
        self.tracker.cancel_intent.assert_called_once_with("prepared-intent", "not_sent")

    def test_real_automatic_buy_and_sell_are_blocked_before_intent(self):
        self.session.mode = "real"
        for side, price in (("buy", "100000"), ("sell", None)):
            with self.subTest(side=side):
                self.assert_initial_refusal(lambda: self.automatic(side, price=price))

    def test_paper_market_sell_is_allowed_with_pending_check_and_atomic_option(self):
        receipt = self.automatic("sell", "49", None, strategy="ddcrs")
        self.assertEqual(receipt.side, "sell")
        self.tracker.prepare_order.assert_called_once_with("paper", "sell", "097520", "49", "ddcrs", price=None, require_no_pending=True)

    def test_manual_orders_do_not_read_policy_or_apply_paper_cap(self):
        self.settings_mock.side_effect = AssertionError("Manual policy access forbidden")
        self.session.mode = "real"
        orders.submit_order(self.session, "buy", "097520", "10", None)
        orders.submit_order(self.session, "buy", "097520", "999999999999", "999999999999")
        self.settings_mock.assert_not_called(); self.tracker.prepare_order.assert_not_called()
        self.assertEqual(len(self.sent), 2)

    def test_real_requires_explicit_activation_and_bounded_price(self):
        self.session.mode = "real"
        for values in ({}, {"paper_automation_only": False}):
            self.settings = values
            with self.assertRaises(orders.OrderError) as caught:
                self.automatic(price=None)
            self.assertFalse(caught.exception.uncertain)
        self.tracker.prepare_order.assert_not_called()
        self.assertEqual(self.sent, [])

    def test_lowered_cap_during_queue_wait_cancels_own_intent_without_send(self):
        self.after_enqueue = lambda: self.settings.update(paper_automation_buy_cap=500000)
        with self.assertRaises(orders.OrderError) as caught: self.automatic()
        self.assertFalse(caught.exception.uncertain); self.assertEqual(self.sent, [])
        self.tracker.cancel_intent.assert_called_once_with("prepared-intent", "not_sent")
        self.tracker.assert_no_pending_order.assert_called_once()  # never rejects our own intent in worker

    def test_policy_is_rechecked_after_strategy_callback_changes_cap(self):
        callback = Mock(side_effect=lambda: self.settings.update(paper_automation_buy_cap=1))
        with self.assertRaises(orders.OrderError) as caught: self.automatic(before_send=callback)
        self.assertFalse(caught.exception.uncertain); callback.assert_called_once()
        self.assertEqual(self.sent, []); self.tracker.cancel_intent.assert_called_once()

    def test_session_mode_or_token_change_in_callback_cannot_escape_final_guard(self):
        for changed in (lambda: setattr(self.session, "mode", "real"),
                        lambda: setattr(self.session, "_issued", IssuedToken("different-token", "20300101000000"))):
            with self.subTest(change=changed):
                self.session.mode = "paper"; self.session._issued = IssuedToken("offline-policy-token", "20300101000000")
                with self.assertRaises(orders.OrderError) as caught: self.automatic(before_send=changed)
                self.assertFalse(caught.exception.uncertain); self.assertEqual(self.sent, [])

    def test_enabled_during_queue_without_initial_pending_gate_is_refused(self):
        self.settings["paper_automation_only"] = False
        self.after_enqueue = lambda: self.settings.update(paper_automation_only=True)
        with self.assertRaises(orders.OrderError) as caught: self.automatic()
        self.assertFalse(caught.exception.uncertain); self.assertEqual(self.sent, [])
        self.tracker.assert_no_pending_order.assert_called_once_with("paper", "097520")
        self.tracker.cancel_intent.assert_called_once_with("prepared-intent", "not_sent")

    def test_disabling_policy_after_reconciliation_does_not_bypass_send_guard(self):
        self.after_enqueue = lambda: self.settings.update(paper_automation_only=False)
        with self.assertRaises(orders.OrderError) as caught:
            self.automatic()
        self.assertFalse(caught.exception.uncertain)
        self.assertEqual(self.sent, [])
        self.tracker.cancel_intent.assert_called_once_with("prepared-intent", "not_sent")

    def test_invalid_policy_values_fail_closed_without_echoing_values(self):
        cases = ({"paper_automation_only":"secret-setting"}, {"paper_automation_buy_cap":True},
                 {"paper_automation_buy_cap":0}, {"paper_automation_buy_cap":3000001},
                 {"paper_automation_buy_cap":1000000.0}, {"paper_automation_buy_cap":"1000000"})
        for values in cases:
            with self.subTest(values=values):
                self.settings = {"paper_automation_only":True,"paper_automation_buy_cap":1000000,**values}
                error = self.assert_initial_refusal(lambda: self.automatic())
                self.assertNotIn("secret-setting", str(error))

    def test_settings_read_failure_and_pending_rejection_prevent_submission(self):
        self.settings_mock.side_effect = OSError("private-path-secret")
        error = self.assert_initial_refusal(lambda: self.automatic())
        self.assertNotIn("private-path-secret", str(error))
        self.settings_mock.side_effect = lambda: dict(self.settings)
        self.tracker.assert_no_pending_order.side_effect = RuntimeError("private-order-secret")
        error = self.assert_initial_refusal(lambda: self.automatic())
        self.assertNotIn("private-order-secret", str(error))

    def test_atomic_pending_race_rejection_prevents_queue_and_is_not_uncertain(self):
        self.tracker.prepare_order.side_effect = RuntimeError("pending inserted by another caller")
        with self.assertRaises(orders.OrderError) as caught: self.automatic()
        self.assertFalse(caught.exception.uncertain); self.queue.assert_not_called()
        self.tracker.prepare_order.assert_called_once()
        self.assertTrue(self.tracker.prepare_order.call_args.kwargs["require_no_pending"])
        self.assertEqual(self.sent, [])

    def test_reconciliation_runs_once_before_prepare_and_only_local_closure_runs_in_worker(self):
        contexts=[]
        local_guard=Mock(side_effect=lambda: contexts.append(order_context._current.get()))
        def reconcile(symbol, *, side, quantity, price):
            self.assertEqual(symbol,"097520")
            self.assertEqual(side,"buy")
            self.assertEqual(quantity,"10")
            self.assertEqual(price,"100000")
            self.assertIsNotNone(order_context._current.get())
            self.tracker.prepare_order.assert_not_called()
            self.tracker.assert_no_pending_order.assert_not_called()
            return fake_reconciliation(symbol, side=side, quantity=quantity, price=price, guard=local_guard)
        self.session.paper_automation_reconcile=Mock(side_effect=reconcile)
        self.automatic()
        self.session.paper_automation_reconcile.assert_called_once_with("097520", side="buy", quantity="10", price="100000")
        self.assertEqual(local_guard.call_count,2);self.assertEqual(contexts,[None,None])

    def test_reconciliation_failure_prevents_intent_and_is_sanitized(self):
        self.session.paper_automation_reconcile=Mock(side_effect=RuntimeError("private-account-detail"))
        error=self.assert_initial_refusal(lambda:self.automatic())
        self.assertNotIn("private-account-detail",str(error))
        self.tracker.assert_no_pending_order.assert_not_called()

    def test_reconciliation_guard_expiring_in_queue_cancels_without_second_api_call(self):
        local_guard=Mock(side_effect=RuntimeError("expired account reconciliation"))
        self.session.paper_automation_reconcile=Mock(side_effect=lambda symbol, **kwargs: fake_reconciliation(symbol, guard=local_guard, **kwargs))
        with self.assertRaises(orders.OrderError) as caught:self.automatic()
        self.assertFalse(caught.exception.uncertain);self.assertEqual(self.sent,[])
        self.session.paper_automation_reconcile.assert_called_once_with("097520", side="buy", quantity="10", price="100000")
        self.tracker.cancel_intent.assert_called_once_with("prepared-intent","not_sent")

    def test_invalid_reconciliation_hook_or_return_cannot_silently_skip_validation(self):
        for reconcile in (None,"invalid",Mock(return_value=None),Mock(return_value=Mock())):
            with self.subTest(reconcile=reconcile):
                self.session.paper_automation_reconcile=reconcile
                self.assert_initial_refusal(lambda:self.automatic())

    def test_missing_reconciliation_hook_also_blocks_automatic_sell(self):
        self.session.paper_automation_reconcile = None
        self.assert_initial_refusal(lambda: self.automatic("sell", "1", None, strategy="ddcrs"))

    def test_real_ledger_blocks_unfilled_and_partial_orders_then_allows_next_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            forbidden=Mock(side_effect=AssertionError("No broker reads in ledger policy test"))
            self.tracker=ExecutionTracker(settings_loader=lambda:{'max_positions_per_theme':0}, log_dir=Path(directory),now=lambda:self.now,
                account_fetcher=forbidden,executions_fetcher=forbidden,profit_fetcher=forbidden)
            buy=self.automatic()
            with self.assertRaises(orders.OrderError):self.automatic()
            self.assertEqual(len(self.sent),1)
            buy_fill=Execution("20261027",str(int(buy.order_number)),"1","097520","엠씨넥스","buy","13:00:00",Decimal(100000),10)
            self.tracker._ingest("paper","20261027",(buy_fill,))
            sell=self.automatic("sell","10",None,strategy="ddcrs")
            partial=Execution("20261027",str(int(sell.order_number)),"2","097520","엠씨넥스","sell","13:00:00",Decimal(100100),5)
            self.tracker._ingest("paper","20261027",(buy_fill,partial))
            with self.assertRaises(orders.OrderError):self.automatic()
            self.assertEqual(len(self.sent),2)
            full=Execution("20261027",str(int(sell.order_number)),"2","097520","엠씨넥스","sell","13:00:00",Decimal(100100),10)
            self.tracker._ingest("paper","20261027",(buy_fill,full))
            self.automatic()
            self.assertEqual(len(self.sent),3)
            forbidden.assert_not_called()


if __name__ == "__main__": unittest.main()
