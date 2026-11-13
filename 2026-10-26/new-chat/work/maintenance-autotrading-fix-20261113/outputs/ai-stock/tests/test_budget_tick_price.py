"""Budget limits use observed KRX prices when the common grid is insufficient."""

from datetime import datetime
from decimal import Decimal
import unittest
from unittest.mock import Mock, patch

from kiwoom_autotrader.api.order_book import OrderBookError
from kiwoom_autotrader.api.orders import OrderError, OrderReceipt
from kiwoom_autotrader.api.session import BrokerSession, IssuedToken, KST
from kiwoom_autotrader.telegram.commands import orders


TOKEN = "offline-tick-budget-token"
SECRET = "private-tick-budget-detail"


class BudgetTickPriceTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 26, 14, 44, tzinfo=KST)
        self.session = BrokerSession(now=lambda: self.now,
            credential_loader=Mock(side_effect=AssertionError("No credentials")),
            token_issuer=Mock(side_effect=AssertionError("No login")))
        self.session.mode = "paper"
        self.session._issued = IssuedToken(TOKEN, "20300101000000")
        self.quote = self.patch(orders, "fetch_stock_info", return_value={"stk_cd": "005930", "cur_prc": "213750"})
        self.book = self.patch(orders, "fetch_budget_limit", return_value=213500)
        self.submit = self.patch(orders, "submit_order", side_effect=self.receipt)
        self.network = self.patch_path("urllib.request.build_opener", side_effect=AssertionError("Live HTTP forbidden"))
        self.keychain = self.patch_path("kiwoom_autotrader.utils.keychain._get_backend", side_effect=AssertionError("Live Keychain forbidden"))

    def patch(self, owner, name, **options):
        patcher = patch.object(owner, name, **options)
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

    def receipt(self, session, side, symbol, quantity, price, **options):
        if "before_send" in options:
            options["before_send"]()
        return OrderReceipt(session.mode, side, symbol, quantity, price, "00024", "https://mockapi.kiwoom.com")

    def buy(self, budget=225225, *, before_send=None):
        replies = orders.handle_buy(f"buy 005930 max {budget}", self.session, before_send=before_send)
        message = "\n".join(replies)
        self.assertNotIn(TOKEN, message)
        self.assertNotIn(SECRET, message)
        return replies, message

    def test_off_grid_213750_uses_observed_213500_and_keeps_one_share(self):
        replies, message = self.buy()
        self.book.assert_called_once_with(self.session, "005930", 213750)
        self.submit.assert_called_once_with(self.session, "buy", "005930", "1", "213500")
        self.assertFalse(getattr(replies, "uncertain", False))
        for phrase in ("조회가 213,750원 · 실제 지정가 213,500원", "최대 매수대금 213,500원",
                       "미사용 예산 11,725원", "원래 조회 현재가로 수량", "표시된 KRX 호가", "수수료", "미체결"):
            self.assertIn(phrase, message)

    def test_common_grid_boundaries_control_lookup_without_claiming_security_type(self):
        cases = ((1, False), (5, True), (1995, True), (1999, False), (2000, True),
                 (4995, True), (4999, False), (5000, True), (5005, False),
                 (19990, True), (19995, False), (20000, True), (20025, False),
                 (49950, True), (49999, False), (50000, True), (50050, False),
                 (199900, True), (199950, False), (200000, True), (200100, False),
                 (214500, True), (213750, False), (499500, True), (499750, False),
                 (500000, True), (500500, False), (501000, True))
        for price, on_grid in cases:
            with self.subTest(price=price):
                self.book.reset_mock()
                self.submit.reset_mock()
                self.quote.return_value["cur_prc"] = str(price)
                # A displayed price can be valid for its product even when the
                # shared conservative grid cannot establish that on its own.
                self.book.return_value = price
                budget = 1_000_000
                self.buy(budget)
                self.assertEqual(orders._on_common_krx_grid(price), on_grid)
                self.assertEqual(self.book.call_count, 0 if on_grid else 1)
                self.submit.assert_called_once_with(
                    self.session, "buy", "005930", str(budget // price), str(price))

    def test_lower_limit_never_increases_original_quote_based_quantity(self):
        _, message = self.buy(427000)
        # Two shares fit the lower limit exactly, but not the original quote.
        self.submit.assert_called_once_with(self.session, "buy", "005930", "1", "213500")
        self.assertIn("미사용 예산 213,500원", message)
        self.assertLessEqual(1 * 213500, 427000)

    def test_original_quote_unaffordable_never_looks_up_a_cheaper_share(self):
        replies, message = self.buy(213500)
        self.assertFalse(getattr(replies, "uncertain", False))
        self.assertIn("1주도", message)
        self.book.assert_not_called()
        self.submit.assert_not_called()

    def test_unclassified_product_uses_displayed_price_without_numeric_rounding(self):
        # No instrument metadata is guessed from the code/name. The observed
        # book may confirm a 5-KRW price below a 100-KRW stock common grid.
        self.quote.return_value = {"stk_cd": "005930", "cur_prc": "71375", "stk_nm": "Unknown product"}
        self.book.return_value = 71365
        self.buy(150000)
        self.book.assert_called_once_with(self.session, "005930", 71375)
        self.submit.assert_called_once_with(self.session, "buy", "005930", "2", "71365")

    def test_higher_or_malformed_book_candidate_cannot_submit(self):
        for limit in (214000, 0, -1, True, False, "213500", 213500.0, Decimal("213500"), None, 10**12):
            with self.subTest(limit=limit):
                self.book.return_value = limit
                replies, message = self.buy()
                self.assertFalse(getattr(replies, "uncertain", False))
                self.assertIn("전송하지 않았습니다", message)
        self.submit.assert_not_called()

    def test_book_error_is_sanitized_and_is_not_an_uncertain_order(self):
        for error in (OrderBookError(SECRET), RuntimeError(SECRET), TimeoutError(SECRET)):
            with self.subTest(error=type(error).__name__):
                self.book.side_effect = error
                replies, message = self.buy()
                self.assertFalse(getattr(replies, "uncertain", False))
                self.assertIn("호가 확인에 실패", message)
        self.submit.assert_not_called()

    def test_identified_book_ip_error_keeps_safe_guidance_without_order(self):
        with patch("kiwoom_autotrader.api.errors.annotate_ip_error", return_value="등록 IP 확인 필요"):
            self.book.side_effect = OrderBookError("safe message", code=8050)
            replies, message = self.buy()
        self.assertEqual(message, "등록 IP 확인 필요")
        self.assertFalse(getattr(replies, "uncertain", False))
        self.submit.assert_not_called()

    def test_mode_token_or_expiry_change_during_book_blocks_order(self):
        changes = (lambda: setattr(self.session, "mode", "real"),
                   lambda: setattr(self.session, "_issued", IssuedToken("changed-token", "20300101000000")),
                   lambda: setattr(self, "now", datetime(2030, 1, 1, tzinfo=KST)))
        for change in changes:
            with self.subTest(change=change):
                self.session.mode = "paper"
                self.session._issued = IssuedToken(TOKEN, "20300101000000")
                self.now = datetime(2026, 10, 26, 14, 44, tzinfo=KST)
                def book(*_, **__):
                    change()
                    return 213500
                self.book.side_effect = book
                replies, message = self.buy()
                self.assertFalse(getattr(replies, "uncertain", False))
                self.assertIn("전송하지 않았습니다", message)
        self.submit.assert_not_called()

    def test_guard_rejection_after_quote_blocks_book_and_submission(self):
        guard = Mock(side_effect=[None, RuntimeError(SECRET)])
        replies, message = self.buy(before_send=guard)
        self.assertFalse(replies.uncertain)
        self.assertIn("전송하지 않았습니다", message)
        self.book.assert_not_called()
        self.submit.assert_not_called()

    def test_guard_rejection_after_book_blocks_submission(self):
        guard = Mock(side_effect=[None, None, None, RuntimeError(SECRET)])
        replies, message = self.buy(before_send=guard)
        self.book.assert_called_once_with(self.session, "005930", 213750, before_send=guard)
        self.assertFalse(replies.uncertain)
        self.assertIn("전송하지 않았습니다", message)
        self.submit.assert_not_called()

    def test_uncertain_submission_after_book_is_still_never_retried(self):
        self.submit.side_effect = OrderError("접수 확인 필요", uncertain=True)
        replies, message = self.buy()
        self.assertTrue(replies.uncertain)
        self.assertEqual(message, "접수 확인 필요")
        self.book.assert_called_once()
        self.submit.assert_called_once()


if __name__ == "__main__":
    unittest.main()
