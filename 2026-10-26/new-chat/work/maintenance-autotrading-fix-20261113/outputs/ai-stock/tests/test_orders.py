"""Offline order contracts. No test reads Keychain or submits an actual order."""

from datetime import datetime
import io
import json
import traceback
import unittest
import urllib.error
from unittest.mock import Mock, patch

from kiwoom_autotrader.api import orders
from kiwoom_autotrader.api.session import BrokerSession, IssuedToken, KST
from kiwoom_autotrader.api.stock_names import StockNameError
from kiwoom_autotrader.telegram.commands import orders as commands
from kiwoom_autotrader.utils.keychain import Credentials


TOKEN = "offline-order-token-private"
KEY = "offline-app-key-private"
SECRET = "offline-app-secret-private"


def fake_session(mode="paper", *, clock=None):
    session = BrokerSession(
        now=clock or (lambda: datetime(2026, 10, 26, 9, tzinfo=KST)),
        credential_loader=Mock(return_value=Credentials(KEY, SECRET)),
        token_issuer=Mock(return_value=IssuedToken(TOKEN, "20300101090000")),
    )
    session.login(mode)
    return session


class OrderTransportTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 26, 9, tzinfo=KST)
        self.session = fake_session(clock=lambda: self.now)
        patcher = patch.object(orders.urllib.request, "build_opener")
        self.addCleanup(patcher.stop)
        self.factory = patcher.start()
        self.opener = self.factory.return_value
        self.response = self.opener.open.return_value.__enter__.return_value
        self.payload = {"return_code": 0, "ord_no": "00024", "return_msg": TOKEN}
        self.response.read.side_effect = lambda size: json.dumps(self.payload).encode()

    def assert_private(self, text):
        for value in (TOKEN, KEY, SECRET):
            self.assertNotIn(value, text)

    def test_unavailable_execution_recording_blocks_before_intent_and_http(self):
        self.session.execution_recording_guard = Mock(side_effect=ValueError("not ready"))
        with patch.object(orders, "prepare_tracking") as prepare:
            with self.assertRaisesRegex(orders.OrderError, "실시간 체결 기록") as raised:
                orders.submit_order(self.session, "buy", "005930", "1", "70000")
        self.assertFalse(raised.exception.uncertain)
        prepare.assert_not_called()
        self.opener.open.assert_not_called()

    def test_recording_loss_while_queued_never_sends_order(self):
        self.session.execution_recording_guard = Mock(side_effect=[None, ValueError("disconnected")])
        with self.assertRaises(orders.OrderError) as raised:
            orders.submit_order(self.session, "sell", "005930", "1")
        self.assertFalse(raised.exception.uncertain)
        self.opener.open.assert_not_called()

    def test_contract_uses_session_origin_and_exact_market_or_limit_fields(self):
        for mode, origin in (("paper", "https://mockapi.kiwoom.com"), ("real", "https://api.kiwoom.com")):
            self.session.login(mode)
            for side, price, api_id in (("buy", None, "kt10000"), ("buy", "70000", "kt10000"),
                                        ("sell", None, "kt10001")):
                with self.subTest(mode=mode, side=side, price=price):
                    self.opener.reset_mock()
                    receipt = orders.submit_order(self.session, side, "005930", "0002", price)
                    request = self.opener.open.call_args.args[0]
                    self.assertEqual(request.full_url, origin + "/api/dostk/ordr")
                    self.assertEqual(request.method, "POST")
                    self.assertEqual(request.get_header("Authorization"), "Bearer " + TOKEN)
                    self.assertEqual(request.get_header("Api-id"), api_id)
                    self.assertEqual(request.get_header("Content-type"), "application/json;charset=UTF-8")
                    self.assertEqual(json.loads(request.data), {
                        "dmst_stex_tp": "KRX", "stk_cd": "005930", "ord_qty": "2",
                        "ord_uv": price or "", "trde_tp": "0" if price else "3", "cond_uv": "",
                    })
                    self.opener.open.assert_called_once()
                    self.assertEqual(self.opener.open.call_args.kwargs, {"timeout": 15})
                    self.assertIsInstance(self.factory.call_args.args[0], orders._RejectRedirects)
                    self.assertEqual((receipt.mode, receipt.origin, receipt.order_number), (mode, origin, "00024"))
                    self.assert_private(repr(receipt))

    def test_optional_exchange_and_string_success_code_are_supported(self):
        self.payload = {"return_code": "0", "ord_no": "0000138", "dmst_stex_tp": "KRX"}
        receipt = orders.submit_order(self.session, "sell", "005930", 1)
        self.assertEqual(receipt.order_number, "0000138")
        self.assertEqual(receipt.exchange, "KRX")

    def test_bad_inputs_never_create_a_transport(self):
        cases = [("buy", "005930", value, None) for value in
                 (None, True, False, 0, -1, 1.1, "", "null", "0", "-1", "+1", "1.0", "1,000", "１", "1" * 13)]
        cases += [("buy", "005930", "1", value) for value in
                  (True, False, 0, -1, 1.2, "", "null", "0", "-1", "+1", "1.2", "1,000", "１", "1" * 13)]
        cases += [("buy", value, "1", None) for value in (None, "5930", "005930_NX", "005930_AL", "005930 ", "００５９３０")]
        cases += [("sell", "005930", "1", "70000"), ("cancel", "005930", "1", None)]
        for side, symbol, quantity, price in cases:
            with self.subTest(side=side, symbol=symbol, quantity=quantity, price=price), self.assertRaises(ValueError):
                orders.submit_order(self.session, side, symbol, quantity, price)
        self.factory.assert_not_called()

    def test_twelve_digit_boundary_is_sent_without_float_rounding(self):
        orders.submit_order(self.session, "buy", "005930", "999999999999", "999999999999")
        body = json.loads(self.opener.open.call_args.args[0].data)
        self.assertEqual(body["ord_qty"], "999999999999")
        self.assertEqual(body["ord_uv"], "999999999999")

    def test_missing_expired_and_unknown_mode_sessions_never_send(self):
        self.session.clear()
        with self.assertRaises(orders.OrderError):
            orders.submit_order(self.session, "buy", "005930", 1)
        self.session = fake_session(clock=lambda: self.now)
        self.now = datetime(2030, 1, 1, 9, tzinfo=KST)
        with self.assertRaisesRegex(orders.OrderError, "만료"):
            orders.submit_order(self.session, "sell", "005930", 1)
        self.session.mode = "unexpected"
        with self.assertRaises(orders.OrderError):
            orders.submit_order(self.session, "buy", "005930", 1)
        self.factory.assert_not_called()

    def test_explicit_rejection_does_not_echo_remote_text_or_retry(self):
        self.payload = {"return_code": 123, "return_msg": TOKEN + SECRET, "ord_no": "00024"}
        with self.assertRaises(orders.OrderError) as caught:
            orders.submit_order(self.session, "buy", "005930", 1)
        self.assertFalse(caught.exception.uncertain)
        self.assertIn("123", str(caught.exception))
        self.assert_private("".join(traceback.format_exception(caught.exception)))
        self.opener.open.assert_called_once()

    def test_unconfirmed_responses_are_uncertain_and_never_retried(self):
        cases = [[], {}, {"return_code": True}, {"return_code": None}, {"return_code": TOKEN}]
        cases += [{"return_code": 0, "ord_no": "0"}, {"return_code": 0, "ord_no": "0000000"}]
        cases += [{"return_code": 0, "ord_no": value} for value in
                  (None, 24, "", "00000000", "00024\n", "１２３", TOKEN)]
        cases += [{"return_code": 0, "ord_no": "00024", "dmst_stex_tp": value}
                  for value in ("NXT", "SOR", [], {})]
        for payload in cases:
            with self.subTest(payload_type=type(payload).__name__):
                self.opener.reset_mock()
                self.payload = payload
                with self.assertRaises(orders.OrderError) as caught:
                    orders.submit_order(self.session, "buy", "005930", 1)
                self.assertTrue(caught.exception.uncertain)
                self.assertIn("주문내역", str(caught.exception))
                self.assert_private(str(caught.exception))
                self.opener.open.assert_called_once()

    def test_ambiguous_transport_failures_and_redirects_are_never_retried(self):
        errors = [TimeoutError(TOKEN), urllib.error.URLError(SECRET),
                  urllib.error.HTTPError("https://invalid/" + SECRET, 302, TOKEN, {}, io.BytesIO(TOKEN.encode()))]
        for error in errors:
            with self.subTest(error_type=type(error).__name__):
                self.opener.reset_mock()
                self.opener.open.side_effect = error
                with self.assertRaises(orders.OrderError) as caught:
                    orders.submit_order(self.session, "sell", "005930", 1)
                self.assertTrue(caught.exception.uncertain)
                self.assert_private("".join(traceback.format_exception(caught.exception)))
                self.opener.open.assert_called_once()
        self.assertIsNone(orders._RejectRedirects().redirect_request(None, None, 302, "", {}, "https://invalid"))

    def test_bad_json_encoding_and_oversized_response_are_uncertain(self):
        for raw in (b"[", b"\xff", b"x" * (orders._MAX_RESPONSE_BYTES + 1), "not bytes"):
            with self.subTest(raw_type=type(raw).__name__):
                self.opener.reset_mock()
                self.response.read.side_effect = None
                self.response.read.return_value = raw
                with self.assertRaises(orders.OrderError) as caught:
                    orders.submit_order(self.session, "buy", "005930", 1)
                self.assertTrue(caught.exception.uncertain)
                self.opener.open.assert_called_once()

    def test_order_is_not_paginated_even_if_continuation_headers_exist(self):
        self.response.headers = {"cont-yn": "Y", "next-key": TOKEN}
        orders.submit_order(self.session, "buy", "005930", 1)
        self.opener.open.assert_called_once()
        self.session._credential_loader.assert_called_once()
        self.session._token_issuer.assert_called_once()


class OrderCommandTests(unittest.TestCase):
    def setUp(self):
        self.session = fake_session()
        quote = patch.object(commands, "fetch_stock_info", return_value={"stk_cd": "005930", "upl_pric": "100000"})
        quote.start(); self.addCleanup(quote.stop)
        self.submit_patcher = patch.object(commands, "submit_order")
        self.submit = self.submit_patcher.start()
        self.addCleanup(self.submit_patcher.stop)
        self.resolve_patcher = patch.object(commands, "resolve_stock_name", return_value="005930")
        self.resolve = self.resolve_patcher.start()
        self.addCleanup(self.resolve_patcher.stop)
        self.submit.side_effect = lambda session, side, symbol, quantity, price: orders.OrderReceipt(
            session.mode, side, symbol, quantity, price, "00024", "https://mockapi.kiwoom.com")

    def test_buy_and_sell_normalize_whitespace_case_and_positive_integers(self):
        self.assertEqual(commands.parse_buy_command(" \nBuY  005930  0002\t"),
                         commands.ParsedOrder("buy", "005930", "2", None))
        self.assertEqual(commands.parse_sell_command(" SeLL 005930 2 "),
                         commands.ParsedOrder("sell", "005930", "2", None))
        self.assertIsNone(commands.parse_buy_command("sell 005930 2"))
        self.assertIsNone(commands.parse_sell_command("buy 005930 2"))

    def test_quoted_names_keep_internal_spaces_and_numeric_parts(self):
        parsed = commands.parse_buy_command('buy "KODEX 200" 1')
        self.assertEqual(parsed.identifier, "KODEX 200")
        self.assertIsNone(parsed.price)
        commands.handle_buy('buy "KODEX 200" 1 35000', self.session)
        self.resolve.assert_called_once_with(self.session, "KODEX 200")
        self.submit.assert_called_once_with(self.session, "buy", "005930", "1", "35000")

    def test_code_buy_skips_name_lookup_and_reports_receipt_not_execution(self):
        reply = "\n".join(commands.handle_buy("buy 005930 2 70000", self.session))
        self.resolve.assert_not_called()
        self.submit.assert_called_once_with(self.session, "buy", "005930", "2", "70000")
        for expected in ("모의투자", "매수 주문 접수", "KRX", "005930", "2주", "70,000원", "00024", "체결 여부는 확인하지 않았습니다"):
            self.assertIn(expected, reply)
        self.assertNotIn(TOKEN, reply)

    def test_sell_is_market_only_and_reports_real_session(self):
        self.session.login("real")
        reply = "\n".join(commands.handle_sell("sell 005930 1", self.session))
        self.assertIn("실전투자 매도 주문 접수", reply)
        self.assertIn("시장가", reply)
        self.resolve.assert_not_called()
        self.submit.assert_called_once_with(self.session, "sell", "005930", "1", None)

    def test_simple_exact_name_buy_uses_resolver_before_submission(self):
        commands.handle_buy("buy 삼성전자 1", self.session)
        self.resolve.assert_called_once_with(self.session, "삼성전자")
        self.submit.assert_called_once_with(self.session, "buy", "005930", "1", None)

    def test_ambiguous_or_partial_name_is_not_chosen_for_order(self):
        self.resolve.side_effect = StockNameError("정확히 일치하는 종목명을 입력하세요.", candidates=(("삼성전자", "005930"),))
        reply = commands.handle_buy("buy 삼성 1", self.session)
        self.assertIn("정확히", reply[0])
        self.submit.assert_not_called()

    def test_missing_quantity_bad_price_and_unquoted_spaced_names_never_resolve_or_order(self):
        bad_buy = ["buy", "buy 005930", "buy 005930 0", "buy 005930 -1", "buy 005930 1 null",
                   "buy 005930 1 0", "buy 005930 1 -2", "buy 005930 1 1.5", "buy 123 1",
                   "buy 005930 1 1000 extra", "buy 삼성 전자 1", 'buy "KODEX 200 1',
                   "buy 005930 1000000000000", "buy 005930 1 1000000000000", 'buy "" 1']
        for text in bad_buy:
            with self.subTest(text=text):
                self.assertEqual(commands.handle_buy(text, self.session), [commands.BUY_USAGE])
        for text in ("sell", "sell 삼성전자 1", "sell 005930 1 70000", "sell 005930 0", "sell 005930_NX 1"):
            with self.subTest(text=text):
                self.assertEqual(commands.handle_sell(text, self.session), [commands.SELL_USAGE])
        self.resolve.assert_not_called()
        self.submit.assert_not_called()

    def test_numeric_suffix_is_not_silently_joined_to_an_unquoted_name(self):
        self.resolve.side_effect = StockNameError("해당 종목명이 없습니다. 정확한 이름을 입력하세요.")
        commands.handle_buy("buy KODEX 200 1", self.session)
        self.resolve.assert_called_once_with(self.session, "KODEX")
        self.submit.assert_not_called()

    def test_unknown_exception_never_leaks_details_or_retries_order(self):
        self.submit.side_effect = RuntimeError(TOKEN + SECRET)
        reply = commands.handle_buy("buy 005930 1", self.session)
        self.assertIn("주문내역", reply[0])
        self.assertNotIn(TOKEN, reply[0])
        self.assertNotIn(SECRET, reply[0])
        self.submit.assert_called_once()

    def test_uncertain_order_result_is_returned_once_without_another_submission(self):
        self.submit.side_effect = orders.OrderError("접수 여부 확인 필요. 주문내역을 확인하세요.", uncertain=True)
        self.assertEqual(commands.handle_sell("sell 005930 1", self.session),
                         ["접수 여부 확인 필요. 주문내역을 확인하세요."])
        self.submit.assert_called_once()


if __name__ == "__main__":
    unittest.main()
