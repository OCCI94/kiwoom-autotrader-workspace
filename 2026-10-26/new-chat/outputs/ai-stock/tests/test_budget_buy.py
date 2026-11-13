"""Budget buy safety contracts; quotes and orders are always mocked."""

from datetime import datetime
from decimal import Decimal
import unittest
from unittest.mock import Mock, patch

from kiwoom_autotrader.api.orders import OrderError, OrderReceipt
from kiwoom_autotrader.api.session import BrokerSession, IssuedToken, KST
from kiwoom_autotrader.api.stock_info import StockInfoError
from kiwoom_autotrader.telegram.commands import orders as commands
from kiwoom_autotrader.utils.command_scheduler import CommandSchedulerError, validate_command
from kiwoom_autotrader.utils.keychain import Credentials


TOKEN = "private-offline-budget-token"
SECRET = "private-offline-budget-secret"


class BudgetBuyParserTests(unittest.TestCase):
    def test_max_normalizes_case_whitespace_and_budget(self):
        parsed = commands.parse_buy_command(" \nBuY\t005930  MAX  000100000  ")
        self.assertEqual(parsed, commands.ParsedOrder("buy", "005930", None, budget="100000"))
        self.assertIsNone(parsed.price)
        self.assertIsNone(parsed.quantity)

    def test_malformed_max_commands_are_rejected_without_name_inference(self):
        bad = ["buy 005930 max", "buy 005930 max 100000 extra", "buy 삼성전자 max 100000",
               'buy "삼성 전자" max 100000', "buy 5930 max 100000", "buy ００５９３０ max 100000",
               "buy 005930_NX max 100000", "buy 005930_AL max 100000"]
        bad += ["buy 005930 max " + value for value in
                ("0", "-1", "+1", "1.0", "1.5", "1,000", "1e5", "NaN", "Infinity", "１", "1000000000000")]
        for command in bad:
            with self.subTest(command=command), self.assertRaises(ValueError):
                commands.parse_buy_command(command)

    def test_max_is_also_validated_by_scheduler_without_runtime_actions(self):
        with patch.object(commands, "fetch_stock_info") as quote, patch.object(commands, "submit_order") as submit:
            self.assertEqual(validate_command("  BUY 005930 MAX 100000  "), "BUY 005930 MAX 100000")
            with self.assertRaises(CommandSchedulerError):
                validate_command("buy 삼성전자 max 100000")
            with self.assertRaises(CommandSchedulerError):
                validate_command("buy 005930 max 0")
            quote.assert_not_called()
            submit.assert_not_called()

    def test_legacy_buy_and_sell_keep_their_fields(self):
        cases = [("buy 005930 2", commands.ParsedOrder("buy", "005930", "2")),
                 ("buy 삼성전자 2 70000", commands.ParsedOrder("buy", "삼성전자", "2", "70000")),
                 ('buy "KODEX 200" 2', commands.ParsedOrder("buy", "KODEX 200", "2"))]
        for command, expected in cases:
            with self.subTest(command=command):
                self.assertEqual(commands.parse_buy_command(command), expected)
        self.assertEqual(commands.parse_sell_command("sell 005930 2"), commands.ParsedOrder("sell", "005930", "2"))


class BudgetBuyPlanTests(unittest.TestCase):
    def test_signed_current_price_means_direction_not_negative_money(self):
        for quote in ("70000", "+70000", "-70000", " +000070000 "):
            with self.subTest(quote=quote):
                self.assertEqual(commands.plan_budget_buy("150000", quote),
                                 commands.BudgetBuyPlan(150000, 70000, 2, 140000, 10000))

    def test_exact_division_and_boundary_do_not_use_float(self):
        for amount, quote, quantity, unused in (("140000", "70000", 2, 0),
                                                 ("999999999999", "1", 999999999999, 0),
                                                 ("999999999999", "999999999999", 1, 0),
                                                 ("999999999998", "333333333333", 2, 333333333332)):
            with self.subTest(amount=amount, quote=quote):
                plan = commands.plan_budget_buy(amount, quote)
                self.assertEqual((plan.quantity, plan.unused_budget), (quantity, unused))
                self.assertEqual(plan.gross_total, quantity * int(quote))
                self.assertLessEqual(plan.gross_total, int(amount))
                self.assertEqual(plan.gross_total + plan.unused_budget, int(amount))
                self.assertLess(plan.unused_budget, plan.price)

    def test_zero_quantity_and_invalid_prices_never_produce_a_plan(self):
        bad_prices = [None, True, False, 70000, 70000.0, Decimal("70000"), "", " ", "0", "+0", "-0",
                      "0.00", "70000.0", "70000.5", "-70000.5", "NaN", "Infinity", "1e5", "1,000",
                      "７００００", "++70000", "+-70000", "70000 trailing", "1000000000000", "9" * 10000]
        for value in bad_prices:
            with self.subTest(value_type=type(value).__name__), self.assertRaises(commands.BudgetBuyError):
                commands.plan_budget_buy("100000", value)
        with self.assertRaisesRegex(commands.BudgetBuyError, "1주"):
            commands.plan_budget_buy("69999", "70000")

    def test_bad_or_oversized_budget_is_rejected_before_calculation(self):
        for value in (None, True, False, 0, -1, 1.5, Decimal("100000"), "0", "-1", "+1", "1.0", "1e5",
                      "1000000000000", "1" * 10000):
            with self.subTest(value_type=type(value).__name__), self.assertRaises(ValueError):
                commands.plan_budget_buy(value, "1")


class BudgetBuyCommandTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 26, 9, tzinfo=KST)
        self.session = BrokerSession(
            now=lambda: self.now,
            credential_loader=Mock(return_value=Credentials("offline-budget-key", SECRET)),
            token_issuer=Mock(return_value=IssuedToken(TOKEN, "20300101090000")),
        )
        self.session.login("paper")
        self.session._credential_loader.reset_mock()
        self.session._token_issuer.reset_mock()
        quote_patch = patch.object(commands, "fetch_stock_info", return_value={"stk_cd": "005930", "cur_prc": "-70000"})
        submit_patch = patch.object(commands, "submit_order")
        resolve_patch = patch.object(commands, "resolve_stock_name")
        book_patch = patch.object(commands, "fetch_budget_limit", side_effect=lambda session, symbol, price: price)
        network_patch = patch("urllib.request.build_opener", side_effect=AssertionError("Network is forbidden in tests"))
        for patcher in (quote_patch, submit_patch, resolve_patch, book_patch, network_patch):
            self.addCleanup(patcher.stop)
        self.quote = quote_patch.start()
        self.submit = submit_patch.start()
        self.resolve = resolve_patch.start()
        self.book = book_patch.start()
        self.network = network_patch.start()
        self.submit.side_effect = lambda session, side, symbol, quantity, price: OrderReceipt(
            session.mode, side, symbol, quantity, price, "00024",
            "https://mockapi.kiwoom.com" if session.mode == "paper" else "https://api.kiwoom.com")

    def tearDown(self):
        self.network.assert_not_called()
        self.session._credential_loader.assert_not_called()
        self.session._token_issuer.assert_not_called()

    def reply(self, text="buy 005930 max 150000"):
        reply = "\n".join(commands.handle_buy(text, self.session))
        self.assertNotIn(TOKEN, reply)
        self.assertNotIn(SECRET, reply)
        return reply

    def test_limit_buy_caps_principal_and_explains_budget_and_fill(self):
        reply = self.reply()
        self.quote.assert_called_once_with(self.session, "005930")
        self.submit.assert_called_once_with(self.session, "buy", "005930", "2", "70000")
        self.resolve.assert_not_called()
        self.book.assert_not_called()
        for expected in ("모의투자", "매수 주문 접수", "KRX", "005930", "2주", "지정가 70,000원",
                         "예산 150,000원", "조회가/지정가 70,000원", "최대 매수대금 140,000원",
                         "미사용 예산 10,000원", "00024", "수수료", "제외", "미체결", "체결 여부는 확인하지 않았습니다"):
            self.assertIn(expected, reply)
        self.assertNotIn("시장가", reply)

    def test_real_session_is_used_without_new_login_or_credentials(self):
        self.session.mode = "real"
        reply = self.reply("BUY 005930 MAX 140000")
        self.assertIn("실전투자", reply)
        self.assertIn("미사용 예산 0원", reply)
        self.quote.assert_called_once_with(self.session, "005930")
        self.submit.assert_called_once_with(self.session, "buy", "005930", "2", "70000")

    def test_manual_budget_above_cap_is_rejected_before_any_quote_or_order(self):
        # Large integer precision belongs to BudgetBuyPlanTests and the API
        # serialization tests; the public command must enforce its safety cap.
        for mode in ("paper", "real"):
            self.session.mode = mode
            for budget in ("1000001", "999999999999"):
                with self.subTest(mode=mode, budget=budget):
                    reply = self.reply(f"buy 005930 max {budget}")
                    self.assertIn("100만원 한도를 초과", reply)
                    self.assertIn("전송하지 않았습니다", reply)
        self.quote.assert_not_called()
        self.book.assert_not_called()
        self.resolve.assert_not_called()
        self.submit.assert_not_called()

    def test_manual_budget_at_cap_keeps_exact_integer_quantity(self):
        self.quote.return_value["cur_prc"] = "+100000"
        self.reply("buy 005930 max 1000000")
        self.submit.assert_called_once_with(self.session, "buy", "005930", "10", "100000")

    def test_bad_budget_and_names_never_quote_resolve_or_order(self):
        for text in ("buy 삼성전자 max 150000", "buy 005930 max 0", "buy 005930 max 1.5",
                     "buy 005930 max 1000000000000", "buy 005930 max"):
            with self.subTest(text=text):
                self.assertEqual(self.reply(text), commands.BUY_USAGE)
        self.quote.assert_not_called()
        self.resolve.assert_not_called()
        self.submit.assert_not_called()

    def test_invalid_quote_response_never_submits(self):
        for value in (None, [], {}, {"stk_cd": "000660", "cur_prc": "70000"},
                      {"stk_cd": "005930_NX", "cur_prc": "70000"}):
            with self.subTest(value=value):
                self.quote.return_value = value
                self.assertIn("전송하지 않았습니다", self.reply())
        self.submit.assert_not_called()

    def test_missing_zero_fractional_oversized_quote_and_too_low_budget_never_order(self):
        for price in (None, "", "0", "-0", "70000.5", "70000.0", "1000000000000", "150001"):
            with self.subTest(price=price):
                self.quote.return_value = {"stk_cd": "005930", "cur_prc": price}
                self.assertIn("전송하지 않았습니다", self.reply())
        self.submit.assert_not_called()

    def test_quote_errors_are_sanitized_and_not_treated_as_unknown_order_results(self):
        for error in (StockInfoError(TOKEN + SECRET), RuntimeError(TOKEN + SECRET), TimeoutError(TOKEN)):
            with self.subTest(error_type=type(error).__name__):
                self.quote.reset_mock()
                self.quote.side_effect = error
                reply = self.reply()
                self.assertIn("현재가 조회에 실패", reply)
                self.assertIn("전송하지 않았습니다", reply)
                self.assertNotIn("주문내역", reply)
                self.quote.assert_called_once()
        self.submit.assert_not_called()

    def test_missing_expired_and_invalid_mode_sessions_do_not_quote(self):
        self.session.mode = "invalid"
        self.assertIn("전송하지 않았습니다", self.reply())
        self.session.mode = "paper"
        self.now = datetime(2030, 1, 1, 9, tzinfo=KST)
        self.assertIn("만료", self.reply())
        self.session.clear()
        self.assertIn("먼저 login", self.reply())
        self.quote.assert_not_called()
        self.submit.assert_not_called()

    def test_mode_switch_after_quote_prevents_order(self):
        def quote(session, symbol):
            session.mode = "real"
            return {"stk_cd": symbol, "cur_prc": "70000"}
        self.quote.side_effect = quote
        self.assertIn("변경", self.reply())
        self.submit.assert_not_called()

    def test_token_renewal_after_quote_prevents_order(self):
        def quote(session, symbol):
            session._issued = IssuedToken("different-offline-token", "20300101090000")
            return {"stk_cd": symbol, "cur_prc": "70000"}
        self.quote.side_effect = quote
        self.assertIn("변경", self.reply())
        self.submit.assert_not_called()

    def test_token_expiry_during_quote_prevents_order(self):
        def quote(session, symbol):
            self.now = datetime(2030, 1, 1, 9, tzinfo=KST)
            return {"stk_cd": symbol, "cur_prc": "70000"}
        self.quote.side_effect = quote
        self.assertIn("만료", self.reply())
        self.submit.assert_not_called()

    def test_quote_then_submit_happen_once_and_uncertain_orders_are_never_retried(self):
        trace = Mock()
        trace.attach_mock(self.quote, "quote")
        trace.attach_mock(self.submit, "submit")
        self.submit.side_effect = OrderError("접수 여부를 확인하지 못했습니다. 주문내역을 확인하세요.", uncertain=True)
        self.assertIn("주문내역", self.reply())
        self.assertEqual([call[0] for call in trace.mock_calls], ["quote", "submit"])
        self.submit.assert_called_once()

    def test_unexpected_submit_failure_is_sanitized_and_not_retried(self):
        self.submit.side_effect = RuntimeError(TOKEN + SECRET)
        self.assertIn("주문내역", self.reply())
        self.submit.assert_called_once()
        self.quote.assert_called_once()


if __name__ == "__main__":
    unittest.main()
