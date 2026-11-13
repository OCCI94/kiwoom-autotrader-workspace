"""Golden-buy guard integration; quotes, transport and credentials stay offline."""

from datetime import datetime
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from kiwoom_autotrader.api import orders as api
from kiwoom_autotrader.api.session import BrokerSession, IssuedToken, KST
from kiwoom_autotrader.config import settings
from kiwoom_autotrader.telegram.commands import orders as commands
from kiwoom_autotrader.trading.order_context import automated_orders
from kiwoom_autotrader.utils.http_queue import HTTPNotSentError
from tests.paper_reconciliation_fixture import fake_reconciliation


TOKEN = "offline-golden-budget-token"
SECRET = "private-golden-guard-detail"


class GoldenBudgetGuardTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "settings.json"
        # The optional automatic-order policy also loads settings implicitly.
        # Keep every reader on this fixture, never the user's enabled daemon.
        self.patch(settings, "settings_path", return_value=self.path)
        settings.save_settings({"gdcrs_enabled": True,
                                "paper_automation_only": False,
                                "gdcrs_stocks": [{"code": "005930", "budget": 150000}]}, self.path)
        self.expected_settings = settings.load_settings(self.path)
        self.now = datetime(2026, 10, 26, 10, tzinfo=KST)
        self.session = BrokerSession(now=lambda: self.now,
                                     credential_loader=Mock(side_effect=AssertionError("No credentials")),
                                     token_issuer=Mock(side_effect=AssertionError("No login")))
        self.session.mode = "paper"
        self.session._issued = IssuedToken(TOKEN, "20300101000000")
        self.session.paper_automation_reconcile = Mock(side_effect=fake_reconciliation)
        self.session.paper_automation_reconcile.available_buy_principal = Mock(return_value=1000000)
        self.trace = []
        self.sent = []
        self.before_transport = None
        self.payload = {"return_code": 0, "ord_no": "00024", "dmst_stex_tp": "KRX"}
        self.quote = self.patch(commands, "fetch_stock_info", side_effect=self.fetch_quote)
        self.book = self.patch(commands, "fetch_budget_limit",
                               side_effect=AssertionError("Unexpected live order-book lookup"))
        self.submit = self.patch(commands, "submit_order", wraps=api.submit_order)
        self.opener = self.patch(api.urllib.request, "build_opener")
        self.queued = self.patch(api, "queued_open", side_effect=self.transport)
        self.network = self.patch_path("socket.socket.connect", side_effect=AssertionError("Live HTTP forbidden"))
        self.keychain = self.patch_path("kiwoom_autotrader.utils.keychain._get_backend",
                                        side_effect=AssertionError("Live Keychain forbidden"))
        self.guard = Mock(side_effect=self.check_strategy)
        self.tracker = Mock()
        self.tracker.assert_no_pending_order = Mock()
        self.tracker.prepare_order.return_value = "prepared-offline-intent"

    def patch(self, target, name, **options):
        patcher = patch.object(target, name, **options)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def patch_path(self, target, **options):
        patcher = patch(target, **options)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def tearDown(self):
        self.network.assert_not_called()
        self.keychain.assert_not_called()
        self.session._credential_loader.assert_not_called()
        self.session._token_issuer.assert_not_called()
        self.opener.return_value.open.assert_not_called()

    def check_strategy(self):
        self.trace.append("guard")
        if settings.load_settings(self.path) != self.expected_settings:
            raise RuntimeError(SECRET)
        if (self.session.mode, self.session.token) != ("paper", TOKEN) or not self.session.token_usable:
            raise RuntimeError(SECRET)

    def fetch_quote(self, session, symbol):
        self.trace.append("quote")
        return {"stk_cd": symbol, "cur_prc": "-70000"}

    def transport(self, opener, request, **options):
        self.trace.append("queue")
        if self.before_transport is not None:
            self.before_transport()
        # Model the real queue's grant-time guard before its opener is called.
        try:
            options["before_send"]()
        except Exception:
            raise HTTPNotSentError("offline condition rejected") from None
        self.trace.append("transport")
        self.sent.append(request)
        return io.BytesIO(json.dumps(self.payload).encode())

    def buy(self, text="buy 005930 max 150000"):
        with automated_orders(self.tracker, "gdcrs"):
            reply = commands.handle_buy(text, self.session, before_send=self.guard)
        message = "\n".join(reply)
        self.assertNotIn(SECRET, message)
        self.assertNotIn(TOKEN, message)
        return reply, message

    def test_guard_runs_before_quote_after_quote_and_at_actual_send(self):
        reply, message = self.buy()
        self.assertEqual(self.trace, ["guard", "guard", "quote", "guard", "queue", "guard", "guard", "transport"])
        self.submit.assert_called_once_with(self.session, "buy", "005930", "2", "70000", before_send=self.guard)
        self.assertEqual(len(self.sent), 1)
        self.assertFalse(getattr(reply, "uncertain", False))
        args = self.tracker.prepare_order.call_args
        self.assertEqual(args.args, ("paper", "buy", "005930", "2", "gdcrs"))
        self.assertEqual((args.kwargs["price"], args.kwargs["require_no_pending"]), ("70000", True))
        self.assertEqual(args.kwargs["reconciliation_evidence"]["remaining_principal_krw"], 1000000)
        receipt = self.tracker.register_order.call_args.args[0]
        self.tracker.register_order.assert_called_once_with(receipt, "gdcrs", intent_id="prepared-offline-intent")
        for text in ("최대 매수대금 140,000원", "미사용 예산 10,000원", "수수료", "미체결", "00024"):
            self.assertIn(text, message)

    def test_initial_guard_failure_prevents_quote_and_journal(self):
        settings.save_settings({"gdcrs_enabled": False}, self.path)
        reply, message = self.buy()
        self.assertFalse(reply.uncertain)
        self.assertIn("실행 조건이 변경", message)
        self.quote.assert_not_called()
        self.submit.assert_not_called()
        self.tracker.prepare_order.assert_not_called()

    def test_external_settings_changes_during_quote_prevent_submission(self):
        changes = ({"gdcrs_enabled": False}, {"gdcrs_stocks": []},
                   {"gdcrs_stocks": [{"code": "005930", "budget": 100000}]},
                   {"gdcrs_short_period": 6})
        for change in changes:
            with self.subTest(change=change):
                settings.save_settings(self.expected_settings, self.path)
                def quote(session, symbol):
                    settings.save_settings(change, self.path)
                    return {"stk_cd": symbol, "cur_prc": "+70000"}
                self.quote.side_effect = quote
                reply, message = self.buy()
                self.assertFalse(reply.uncertain)
                self.assertIn("전송하지 않았습니다", message)
        self.submit.assert_not_called()
        self.queued.assert_not_called()
        self.tracker.prepare_order.assert_not_called()

    def test_settings_change_while_queued_cancels_intent_without_transport(self):
        for change in ({"gdcrs_enabled": False}, {"gdcrs_stocks": []},
                       {"gdcrs_stocks": [{"code": "005930", "budget": 99999}]}):
            with self.subTest(change=change):
                settings.save_settings(self.expected_settings, self.path)
                self.tracker.reset_mock()
                self.before_transport = lambda: settings.save_settings(change, self.path)
                reply, message = self.buy()
                self.assertFalse(reply.uncertain)
                self.assertIn("전송하지 않았습니다", message)
                self.tracker.cancel_intent.assert_called_once_with("prepared-offline-intent", "not_sent")
                self.tracker.register_order.assert_not_called()
                self.assertEqual(self.sent, [])

    def test_new_market_price_after_quote_never_changes_frozen_limit_or_exceeds_budget(self):
        quote = {"stk_cd": "005930", "cur_prc": "70000"}
        self.quote.side_effect = lambda *_: quote
        self.before_transport = lambda: quote.update(cur_prc="100000")
        self.buy()
        body = json.loads(self.sent[0].data)
        self.assertEqual((body["ord_qty"], body["ord_uv"], body["trde_tp"]), ("2", "70000", "0"))
        self.assertLessEqual(int(body["ord_qty"]) * int(body["ord_uv"]), 150000)
        self.quote.assert_called_once()

    def test_each_new_buy_quotes_again_instead_of_reusing_prior_quantity(self):
        self.quote.side_effect = [{"stk_cd": "005930", "cur_prc": "70000"},
                                  {"stk_cd": "005930", "cur_prc": "100000"}]
        self.buy()
        self.buy()
        self.assertEqual([(json.loads(item.data)["ord_qty"], json.loads(item.data)["ord_uv"])
                          for item in self.sent], [("2", "70000"), ("1", "100000")])
        self.assertEqual(self.quote.call_count, 2)

    def test_no_share_and_fractional_quote_do_not_create_intent_or_order(self):
        for price in ("150001", "70000.5"):
            with self.subTest(price=price):
                self.quote.side_effect = lambda *_, price=price: {"stk_cd": "005930", "cur_prc": price}
                reply, message = self.buy()
                self.assertFalse(reply.uncertain)
                if price == "150001":
                    self.assertEqual(message, "")
                    self.assertEqual(reply.skipped_reason, "budget_below_one_share")
                else:
                    self.assertIn("전송하지 않았습니다", message)
                    self.assertIsNone(reply.skipped_reason)
        self.submit.assert_not_called()
        self.tracker.prepare_order.assert_not_called()

    def test_mode_token_or_expiry_change_during_quote_prevents_submission(self):
        mutations = (lambda: setattr(self.session, "mode", "real"),
                     lambda: setattr(self.session, "_issued", IssuedToken("changed-offline-token", "20300101000000")),
                     lambda: setattr(self, "now", datetime(2030, 1, 1, tzinfo=KST)))
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                self.session.mode = "paper"
                self.session._issued = IssuedToken(TOKEN, "20300101000000")
                self.now = datetime(2026, 10, 26, 10, tzinfo=KST)
                def quote(*_):
                    mutate()
                    return {"stk_cd": "005930", "cur_prc": "70000"}
                self.quote.side_effect = quote
                reply, message = self.buy()
                self.assertFalse(reply.uncertain)
                self.assertIn("전송하지 않았습니다", message)
        self.submit.assert_not_called()
        self.tracker.prepare_order.assert_not_called()

    def test_mode_or_expiry_change_in_queue_cancels_before_transport(self):
        for mutate in (lambda: setattr(self.session, "mode", "real"),
                       lambda: setattr(self, "now", datetime(2030, 1, 1, tzinfo=KST))):
            with self.subTest(mutate=mutate):
                self.session.mode = "paper"
                self.now = datetime(2026, 10, 26, 10, tzinfo=KST)
                self.tracker.reset_mock()
                self.before_transport = mutate
                reply, message = self.buy()
                self.assertFalse(reply.uncertain)
                self.assertIn("전송하지 않았습니다", message)
                self.tracker.cancel_intent.assert_called_once_with("prepared-offline-intent", "not_sent")
                self.tracker.register_order.assert_not_called()
        self.assertEqual(self.sent, [])

    def test_unknown_transport_result_preserves_uncertain_flag_without_retry(self):
        self.queued.side_effect = TimeoutError(SECRET)
        reply, message = self.buy()
        self.assertTrue(reply.uncertain)
        self.assertIn("자동 재전송하지 않았습니다", message)
        self.queued.assert_called_once()
        self.quote.assert_called_once()
        self.tracker.cancel_intent.assert_not_called()
        self.tracker.register_order.assert_not_called()

    def test_legacy_buy_forwards_guard_without_quote_or_budget_calculation(self):
        reply, message = self.buy("buy 005930 2 70000")
        self.quote.assert_not_called()
        self.submit.assert_called_once_with(self.session, "buy", "005930", "2", "70000", before_send=self.guard)
        self.assertFalse(getattr(reply, "uncertain", False))
        self.assertIn("주문 접수", message)

    def test_off_grid_quote_keeps_guard_through_book_and_order_queue(self):
        self.quote.side_effect = lambda *_: {"stk_cd": "005930", "cur_prc": "71375"}
        def book(session, symbol, price, *, before_send):
            self.trace.append("book")
            self.assertIs(before_send, self.guard)
            before_send()
            return 71300
        self.book.side_effect = book
        reply, message = self.buy()
        self.assertEqual(self.trace, ["guard", "guard", "guard", "book", "guard", "guard", "queue", "guard", "guard", "transport"])
        self.assertFalse(getattr(reply, "uncertain", False))
        self.submit.assert_called_once_with(self.session, "buy", "005930", "2", "71300", before_send=self.guard)
        self.assertIn("조회가 71,375원 · 실제 지정가 71,300원", message)

    def test_settings_change_during_book_prevents_order_and_journal(self):
        self.quote.side_effect = lambda *_: {"stk_cd": "005930", "cur_prc": "71375"}
        def book(*_, **__):
            settings.save_settings({"gdcrs_enabled": False}, self.path)
            return 71300
        self.book.side_effect = book
        reply, message = self.buy()
        self.assertFalse(reply.uncertain)
        self.assertIn("전송하지 않았습니다", message)
        self.submit.assert_not_called()
        self.queued.assert_not_called()
        self.tracker.prepare_order.assert_not_called()


if __name__ == "__main__":
    unittest.main()
