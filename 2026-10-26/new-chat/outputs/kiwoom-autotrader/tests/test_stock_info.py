"""Offline srch contracts, environment separation and bounded safe replies."""

from datetime import datetime
import io
import json
import traceback
from types import SimpleNamespace
import unittest
import urllib.error
from unittest.mock import Mock, patch

from kiwoom_autotrader.api import stock_info as stock
from kiwoom_autotrader.api.session import BrokerSession, IssuedToken, KST
from kiwoom_autotrader.telegram.commands import search
from kiwoom_autotrader.utils.keychain import Credentials


TOKEN = "stock-fixture-private-token"
SECRET = "stock-fixture-private-secret"


def make_session(mode="paper", expiry="20300101085500"):
    session = BrokerSession(
        now=lambda: datetime(2026, 10, 26, 9, 0, tzinfo=KST),
        credential_loader=Mock(return_value=Credentials("stock-fixture-key", SECRET)),
        token_issuer=Mock(return_value=IssuedToken(TOKEN, expiry)),
    )
    if mode is not None:
        assert session.login(mode).success
    return session


def sample_payload():
    return {
        "return_code": 0, "return_msg": "정상 처리", "stk_cd": "005930", "stk_nm": "삼성전자",
        "cur_prc": "+70000", "pred_pre": "+1000", "flu_rt": "+1.45", "pre_sig": "2",
        "open_pric": "-69000", "high_pric": "+70500", "low_pric": "-68500",
        "trde_qty": "1234567", "trde_pre": "98.00", "cap": "8975", "mac": "4178850",
        "setl_mm": "12", "fav": "100", "flo_stk": "5969783", "dstr_stk": "123456",
        "dstr_rt": "72.5", "crd_rt": "+0.08", "for_exh_rt": "50.00",
        "sale_amt": "2589355", "bus_pro": "65670", "cup_nga": "-100",
        "per": "10.50", "pbr": "1.12", "roe": "8.20", "eps": "6000", "bps": "-75300",
        "ev": "", "250hgst": "+85000", "250lwst": "-50000",
        "250hgst_pric_dt": "20261015", "250lwst_pric_dt": "20260102",
        "250hgst_pric_pre_rt": "-17.65", "250lwst_pric_pre_rt": "+40.00",
    }


class SearchParsingTests(unittest.TestCase):
    def test_normalizes_case_whitespace_and_all_documented_exchanges(self):
        for text, expected in (
            (" srch 005930 ", "005930"), ("\nSrCh\t039490_nx\n", "039490_NX"),
            ("SRCH   005930_aL", "005930_AL"),
        ):
            with self.subTest(text=text):
                self.assertEqual(search.parse_search_command(text), expected)
        for text in (None, "", "login paper", "srchx 005930", "power off"):
            self.assertIsNone(search.parse_search_command(text))

    def test_invalid_input_returns_help_without_echoing_input(self):
        for text in ("srch", "srch 5930", "srch 005930 extra", "srch ００５９３０",
                     "srch 005930_US", "srch https://example.com", "srch " + "가" * 41):
            with self.subTest(text=text), self.assertRaisesRegex(ValueError, "사용법") as caught:
                search.parse_search_command(text)
            self.assertNotIn(SECRET, str(caught.exception))
        with patch.object(search, "fetch_stock_info") as fetch:
            self.assertEqual(search.handle_search("srch https://example.com", None), [search.USAGE])
            fetch.assert_not_called()


class StockTransportTests(unittest.TestCase):
    def setUp(self):
        self.session = make_session()
        self.payload = sample_payload()
        patcher = patch("kiwoom_autotrader.api.stock_info.urllib.request.build_opener")
        self.addCleanup(patcher.stop)
        self.factory = patcher.start()
        self.opener = self.factory.return_value
        self.response = self.opener.open.return_value.__enter__.return_value
        self.response.read.side_effect = lambda size: json.dumps(self.payload).encode("utf-8")

    def test_uses_current_token_and_fixed_origin_without_authentication(self):
        for mode, origin in (("paper", "https://mockapi.kiwoom.com"), ("real", "https://api.kiwoom.com")):
            session = make_session(mode)
            with self.subTest(mode=mode):
                data = stock.fetch_stock_info(session, "005930")
                self.assertEqual(data["cur_prc"], "+70000")
                self.assertNotIn("return_msg", data)
                request = self.opener.open.call_args.args[0]
                self.assertEqual(request.full_url, origin + "/api/dostk/stkinfo")
                self.assertEqual(request.method, "POST")
                self.assertEqual(request.get_header("Api-id"), "ka10001")
                self.assertEqual(request.get_header("Authorization"), f"Bearer {TOKEN}")
                self.assertEqual(request.get_header("Content-type"), "application/json;charset=UTF-8")
                self.assertEqual(json.loads(request.data), {"stk_cd": "005930"})
                self.assertEqual(self.opener.open.call_args.kwargs, {"timeout": 15})
                self.response.read.assert_called_with(stock._MAX_RESPONSE_BYTES + 1)
                self.assertEqual(session._credential_loader.call_count, 1)
                self.assertEqual(session._token_issuer.call_count, 1)
                self.assertEqual(session.mode, mode)
                self.assertEqual(session.token, TOKEN)

    def test_nxt_sor_requests_remain_on_current_mode_origin(self):
        for symbol in ("005930_NX", "005930_AL"):
            self.payload["stk_cd"] = symbol
            stock.fetch_stock_info(self.session, symbol.lower())
            request = self.opener.open.call_args.args[0]
            self.assertEqual(request.full_url, "https://mockapi.kiwoom.com/api/dostk/stkinfo")
            self.assertEqual(json.loads(request.data), {"stk_cd": symbol})
        self.payload["stk_cd"] = "005930"
        self.assertEqual(stock.fetch_stock_info(self.session, "005930_NX")["stk_cd"], "005930")

    def test_rejects_other_stock_and_explicitly_different_exchange(self):
        for returned, requested in (("000660", "005930"), ("005930_AL", "005930_NX"), ("005930_NX", "005930")):
            self.payload["stk_cd"] = returned
            with self.subTest(returned=returned), self.assertRaisesRegex(stock.StockInfoError, "종목코드"):
                stock.fetch_stock_info(self.session, requested)

    def test_missing_expired_or_corrupt_session_never_opens_a_connection(self):
        expired = make_session(expiry="20261026090001")
        expired._now = lambda: datetime(2026, 10, 26, 9, 0, 1, tzinfo=KST)
        for session in (None, make_session(None), expired):
            with self.assertRaises(stock.StockInfoError):
                stock.fetch_stock_info(session, "005930")
        for mode in ("https://example.com", "live", [], True):
            self.session.mode = mode
            with self.assertRaises(stock.StockInfoError):
                stock.fetch_stock_info(self.session, "005930")
        self.session.mode = "paper"
        for token in (None, False, [], "bad\r\nheader", "비ASCII", "x" * 8193):
            self.session._issued = SimpleNamespace(token=token, expires_dt=None)
            with self.assertRaises(stock.StockInfoError):
                stock.fetch_stock_info(self.session, "005930")
        self.factory.assert_not_called()

    def test_invalid_symbol_never_opens_a_connection(self):
        for symbol in (None, "005930/..", "005930\nInjected", "12", "005930_NY"):
            with self.assertRaises(ValueError):
                stock.fetch_stock_info(self.session, symbol)
        self.factory.assert_not_called()

    def test_redirect_handler_refuses_token_forwarding(self):
        stock.fetch_stock_info(self.session, "005930")
        handler = self.factory.call_args.args[0]
        self.assertIsInstance(handler, stock._RejectRedirects)
        self.assertIsNone(handler.redirect_request(None, None, 302, "", {}, "https://api.kiwoom.com"))

    def test_broker_errors_are_sanitized_and_do_not_clear_login(self):
        before = self.session.next_renewal_at
        for code in (8005, "8005", False, True, 0.0, None, TOKEN):
            self.payload = {"return_code": code, "return_msg": SECRET + TOKEN}
            with self.subTest(code_type=type(code)), self.assertRaises(stock.StockInfoError) as caught:
                stock.fetch_stock_info(self.session, "005930")
            rendered = "".join(traceback.format_exception(caught.exception))
            self.assertNotIn(TOKEN, rendered)
            self.assertNotIn(SECRET, rendered)
            self.assertEqual(self.session.mode, "paper")
            self.assertEqual(self.session.token, TOKEN)
            self.assertEqual(self.session.next_renewal_at, before)

    def test_zero_string_success_and_optional_missing_values(self):
        self.payload = {"return_code": "0", "stk_cd": "005930", "stk_nm": "삼성전자", "cur_prc": None}
        data = stock.fetch_stock_info(self.session, "005930")
        self.assertEqual(data["cur_prc"], "")
        self.assertEqual(data["trde_qty"], "")

    def test_rejects_bad_field_types_and_sensitive_names(self):
        cases = [
            ("stk_nm", TOKEN), ("stk_nm", "bad\nname"), ("stk_nm", "x" * 41),
            ("stk_nm", None), ("cur_prc", 123), ("cur_prc", True),
            ("cur_prc", "NaN"), ("cur_prc", SECRET), ("trde_qty", {"secret": TOKEN}),
            ("cur_prc", "1" * 21), ("fav_unit", [TOKEN]), ("stk_cd", 5930),
        ]
        for key, value in cases:
            self.payload = {**sample_payload(), key: value}
            with self.subTest(key=key), self.assertRaises(stock.StockInfoError) as caught:
                stock.fetch_stock_info(self.session, "005930")
            rendered = "".join(traceback.format_exception(caught.exception))
            self.assertNotIn(TOKEN, rendered)
            self.assertNotIn(SECRET, rendered)

    def test_http_and_network_errors_hide_urls_bodies_and_causes(self):
        for failure in (
            urllib.error.HTTPError("https://invalid/" + TOKEN, 401, SECRET, {}, io.BytesIO(TOKEN.encode())),
            urllib.error.URLError(TOKEN + SECRET), TimeoutError(TOKEN),
        ):
            self.opener.open.side_effect = failure
            with self.assertRaises(stock.StockInfoError) as caught:
                stock.fetch_stock_info(self.session, "005930")
            rendered = "".join(traceback.format_exception(caught.exception))
            self.assertNotIn(TOKEN, rendered)
            self.assertNotIn(SECRET, rendered)

    def test_malformed_json_non_objects_and_oversize_responses_are_rejected(self):
        self.response.read.side_effect = None
        for raw in (b"[", b"[]", b"null", b"false", b"\xff", b"x" * (stock._MAX_RESPONSE_BYTES + 1), "invalid"):
            self.response.read.return_value = raw
            with self.subTest(kind=type(raw)), self.assertRaises(stock.StockInfoError):
                stock.fetch_stock_info(self.session, "005930")

    def test_unknown_fields_and_server_messages_cannot_enter_replies(self):
        self.payload.update({"return_msg": SECRET, "token": TOKEN, "unknown": SECRET})
        parts = search.handle_search(" srch 005930 ", self.session)
        reply = "\n".join(parts)
        self.assertNotIn(TOKEN, reply)
        self.assertNotIn(SECRET, reply)
        self.assertIn("삼성전자 (005930)", reply)
        self.assertEqual(len(reply.splitlines()), 7)


class StockFormattingTests(unittest.TestCase):
    def validated(self, payload=None):
        return stock._validate_payload(payload or sample_payload(), "005930", TOKEN)

    def test_only_six_requested_metrics_are_displayed_with_readable_price_magnitudes(self):
        reply = "\n".join(search.format_stock_info(self.validated(), "005930", "paper"))
        for expected in (
            "💰 현재가: 70,000원", "🔺 전일대비: +1,000원", "🌅 시가: 69,000원",
            "📊 거래량: 1,234,567주", "🔼 고가: 70,500원", "🔽 저가: 68,500원",
        ):
            self.assertIn(expected, reply)
        self.assertEqual(len(reply.splitlines()), 7)
        for removed in ("등락률", "PER", "시가총액", "상장주식", "ROE"):
            self.assertNotIn(removed, reply)

    def test_missing_is_distinguished_from_zero(self):
        data = self.validated({**sample_payload(), "cur_prc": "0.00", "pred_pre": "-0",
                               "trde_qty": "0", "roe": "", "250hgst_pric_dt": "3",
                               "250lwst_pric_dt": "20260231", "setl_mm": "0"})
        reply = "\n".join(search.format_stock_info(data, "005930", "real"))
        self.assertIn("현재가: 0.00원", reply)
        self.assertIn("전일대비: -0원", reply)
        self.assertIn("거래량: 0주", reply)
        data["cur_prc"] = ""
        self.assertIn("현재가: 미제공", search.format_stock_info(data, "005930", "real")[0])

    def test_longest_documented_fields_stay_below_telegram_limit(self):
        payload = {**sample_payload(), **{key: "+1234567890123456789" for key in stock.NUMERIC_FIELDS},
                   "stk_nm": "가" * 40}
        data = self.validated(payload)
        parts = search.format_stock_info(data, "005930_NX", "paper")
        self.assertTrue(parts)
        for part in parts:
            self.assertTrue(part)
            self.assertLessEqual(len(part.encode("utf-8")), 3900)
            self.assertLessEqual(len(part.encode("utf-16-le")) // 2, 3900)
        self.assertEqual(len(parts), 1)
        self.assertEqual(len(parts[0].splitlines()), 7)
        self.assertIn("005930_NX", parts[0])

    def test_name_resolves_exactly_before_quote_and_codes_bypass_catalog(self):
        with patch.object(search, "resolve_stock_name", return_value="005930") as resolve, \
                patch.object(search, "fetch_stock_info", return_value=self.validated()) as fetch:
            session = make_session()
            reply = search.handle_search(" SrCh 삼성전자 ", session)
            resolve.assert_called_once_with(session, "삼성전자")
            fetch.assert_called_once_with(session, "005930")
            self.assertIn("삼성전자", reply[0])
            resolve.reset_mock()
            search.handle_search("srch 005930", session)
            resolve.assert_not_called()

    def test_ambiguous_name_is_not_quoted_or_chosen_arbitrarily(self):
        with patch.object(search, "resolve_stock_name", side_effect=search.StockNameError("후보를 코드로 선택하세요.")), \
                patch.object(search, "fetch_stock_info") as fetch:
            self.assertIn("코드로", search.handle_search("srch 삼성", make_session())[0])
            fetch.assert_not_called()

    def test_handler_returns_safe_errors_and_does_not_echo_unexpected_exceptions(self):
        with patch.object(search, "fetch_stock_info", side_effect=RuntimeError(TOKEN + SECRET)):
            reply = "\n".join(search.handle_search("srch 005930", make_session()))
        self.assertNotIn(TOKEN, reply)
        self.assertNotIn(SECRET, reply)
        self.assertIn("표시하지 못했습니다", reply)
        self.assertIn("먼저 login", search.handle_search("srch 005930", None)[0])


if __name__ == "__main__":
    unittest.main()
