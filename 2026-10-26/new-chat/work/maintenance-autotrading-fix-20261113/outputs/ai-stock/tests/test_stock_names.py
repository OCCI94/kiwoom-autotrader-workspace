"""Offline exact-name resolution, pagination, session caching and secret safety."""

from datetime import datetime
import gc
import io
import json
import traceback
from types import SimpleNamespace
import unittest
import urllib.error
import weakref
from unittest.mock import MagicMock, Mock, patch

from kiwoom_autotrader.api import stock_names as names
from kiwoom_autotrader.api.session import BrokerSession, IssuedToken, KST
from kiwoom_autotrader.utils.keychain import Credentials


TOKEN = "name-fixture-private-token"
SECRET = "name-fixture-private-secret"


def make_session(mode="paper"):
    session = BrokerSession(
        now=lambda: datetime(2026, 10, 26, 9, 0, tzinfo=KST),
        credential_loader=Mock(return_value=Credentials("name-fixture-key", SECRET)),
        token_issuer=Mock(return_value=IssuedToken(TOKEN, "20300101085500")),
    )
    if mode is not None:
        assert session.login(mode).success
    return session


def record(code, name, market=None):
    result = {"code": code, "name": name}
    if market is not None:
        result["marketCode"] = market
    return result


class StockNameTests(unittest.TestCase):
    def setUp(self):
        self.session = make_session()
        self.clock = 100.0
        self.starts = []
        self.catalog = {
            "0": [record("005930", "삼성전자", "0"), record("005935", "삼성전자우", "0"),
                  record("000660", "SK하이닉스", "0")],
            "10": [record("035900", "JYP Ent.", "10")],
        }
        patches = [
            patch.object(names, "_CACHES", weakref.WeakKeyDictionary()),
            patch.object(names.time, "monotonic", side_effect=lambda: self.clock),
            patch.object(names.time, "sleep", side_effect=self.advance),
            patch("kiwoom_autotrader.api.stock_names.urllib.request.build_opener"),
        ]
        results = []
        for patcher in patches:
            results.append(patcher.start())
            self.addCleanup(patcher.stop)
        self.sleep, self.factory = results[2], results[3]
        self.opener = self.factory.return_value
        self.opener.open.side_effect = self.route

    def advance(self, seconds):
        self.clock += seconds

    @staticmethod
    def response(rows=None, *, payload=None, headers=None, raw=None):
        response = MagicMock()
        response.__enter__.return_value = response
        response.headers = {} if headers is None else headers
        if payload is None:
            payload = {"return_code": 0, "list": rows or []}
        response.read.return_value = json.dumps(payload).encode("utf-8") if raw is None else raw
        return response

    def route(self, request, timeout):
        self.starts.append(self.clock)
        market = json.loads(request.data)["mrkt_tp"]
        return self.response(self.catalog[market])

    def assert_safe(self, error):
        rendered = "".join(traceback.format_exception(error))
        for secret in (TOKEN, SECRET, "name-fixture-key"):
            self.assertNotIn(secret, rendered)

    def test_only_fixed_origins_current_tokens_and_ka10099_markets_are_used(self):
        for mode, origin in (("paper", "https://mockapi.kiwoom.com"), ("real", "https://api.kiwoom.com")):
            session = make_session(mode)
            before = self.opener.open.call_count
            self.assertEqual(names.resolve_stock_name(session, "삼성전자"), "005930")
            calls = self.opener.open.call_args_list[before:]
            self.assertEqual(len(calls), 2)
            self.assertEqual([json.loads(call.args[0].data) for call in calls], [{"mrkt_tp": "0"}, {"mrkt_tp": "10"}])
            for call in calls:
                request = call.args[0]
                self.assertEqual(request.full_url, origin + "/api/dostk/stkinfo")
                self.assertEqual(request.method, "POST")
                self.assertEqual(request.get_header("Api-id"), "ka10099")
                self.assertEqual(request.get_header("Authorization"), f"Bearer {TOKEN}")
                self.assertEqual(call.kwargs, {"timeout": 15})
            self.assertEqual(session._credential_loader.call_count, 1)
            self.assertEqual(session._token_issuer.call_count, 1)
        self.assertIsInstance(self.factory.call_args.args[0], names._RejectRedirects)
        self.assertIsNone(self.factory.call_args.args[0].redirect_request(None, None, 302, "", {}, "https://api.kiwoom.com"))

    def test_exact_name_uses_nfkc_casefold_and_ignores_internal_whitespace(self):
        for query, code in ((" 삼 성 전 자 ", "005930"), ("ｓｋ 하 이 닉 스", "000660"),
                            ("jypent.", "035900"), ("JYP\tEnt.", "035900")):
            with self.subTest(query=query):
                self.assertEqual(names.resolve_stock_name(self.session, query), code)
        self.assertEqual(self.opener.open.call_count, 2)

    def test_exact_match_wins_over_longer_partial_names(self):
        self.assertEqual(names.resolve_stock_name(self.session, "삼성전자"), "005930")
        self.assertEqual(names.resolve_stock_name(self.session, "삼성전자우"), "005935")

    def test_partial_names_never_auto_select_even_a_single_candidate(self):
        for query, candidates in (("삼성", 2), ("하이닉", 1)):
            with self.subTest(query=query), self.assertRaises(names.StockNameError) as caught:
                names.resolve_stock_name(self.session, query)
            self.assertEqual(len(caught.exception.candidates), candidates)
            self.assertIn("정확히 일치", str(caught.exception))
            for name, code in caught.exception.candidates:
                self.assertIn(f"{name} ({code})", str(caught.exception))

    def test_same_name_in_both_markets_is_ambiguous_instead_of_first_match(self):
        self.catalog["10"].append(record("123456", "삼성전자", "10"))
        with self.assertRaises(names.StockNameError) as caught:
            names.resolve_stock_name(self.session, "삼성전자")
        self.assertEqual(set(caught.exception.candidates), {("삼성전자", "005930"), ("삼성전자", "123456")})
        self.assertIn("같은 이름", str(caught.exception))
        self.assertEqual(self.opener.open.call_count, 2)

    def test_no_match_does_not_echo_input_and_candidates_are_bounded(self):
        self.catalog["0"] = [record(f"{index:06d}", f"검색기업{index:02d}", "0") for index in range(20)]
        with self.assertRaises(names.StockNameError) as caught:
            names.resolve_stock_name(self.session, "검색기업")
        self.assertEqual(len(caught.exception.candidates), 8)
        self.assertLess(len(str(caught.exception).encode("utf-8")), 3900)
        with self.assertRaises(names.StockNameError) as caught:
            names.resolve_stock_name(self.session, SECRET)
        self.assertEqual(caught.exception.candidates, ())
        self.assert_safe(caught.exception)

    def test_market_metadata_filters_other_products_and_alphanumeric_codes_are_skipped(self):
        self.catalog["0"] += [record("069500", "KODEX 200", "8"), record("00680K", "문자코드우", "0")]
        self.catalog["10"] += [record("123450", "리츠목록", "6")]
        for query in ("KODEX 200", "문자코드우", "리츠목록"):
            with self.assertRaises(names.StockNameError) as caught:
                names.resolve_stock_name(self.session, query)
            self.assertEqual(caught.exception.candidates, ())
        self.assertEqual(names.resolve_stock_name(self.session, "삼성전자"), "005930")

    def test_same_code_duplicate_pages_is_one_match_but_conflicting_names_fail(self):
        self.catalog["0"].append(record("005930", "삼 성 전 자", 0))
        self.assertEqual(names.resolve_stock_name(self.session, "삼성전자"), "005930")
        self.catalog["0"].append(record("005930", "다른기업", "0"))
        with self.assertRaisesRegex(names.StockNameError, "상충"):
            names.resolve_stock_name(make_session(), "삼성전자")

    def test_cache_is_per_session_mode_and_survives_token_renewal_without_storing_token(self):
        self.assertEqual(names.resolve_stock_name(self.session, "삼성전자"), "005930")
        self.session._token_issuer.return_value = IssuedToken("renewed-fixture-token", "20300101085500")
        self.assertTrue(self.session.login("paper").success)
        self.assertEqual(names.resolve_stock_name(self.session, "삼성전자"), "005930")
        self.assertEqual(self.opener.open.call_count, 2)
        self.assertTrue(self.session.login("real").success)
        self.assertEqual(names.resolve_stock_name(self.session, "삼성전자"), "005930")
        self.assertEqual(self.opener.open.call_count, 4)
        self.assertTrue(self.session.login("paper").success)
        self.assertEqual(names.resolve_stock_name(self.session, "삼성전자"), "005930")
        self.assertEqual(self.opener.open.call_count, 4)
        other = make_session()
        names.resolve_stock_name(other, "삼성전자")
        self.assertEqual(self.opener.open.call_count, 6)
        for state in names._CACHES.values():
            self.assertNotIn(TOKEN, repr(state))
            self.assertNotIn("renewed-fixture-token", repr(state))
        reference = weakref.ref(other)
        del other
        gc.collect()
        self.assertIsNone(reference())
        self.assertEqual(len(names._CACHES), 1)

    def test_cache_expires_and_refetches_both_markets(self):
        names.resolve_stock_name(self.session, "삼성전자")
        self.clock += names._CACHE_TTL_SECONDS + 1
        self.catalog["0"][0] = record("005930", "새이름", "0")
        self.assertEqual(names.resolve_stock_name(self.session, "새이름"), "005930")
        self.assertEqual(self.opener.open.call_count, 4)
        with self.assertRaises(names.StockNameError):
            names.resolve_stock_name(self.session, "삼성전자")
        self.assertEqual(self.opener.open.call_count, 4)

    def test_requests_and_following_lookup_have_one_second_spacing(self):
        names.resolve_stock_name(self.session, "삼성전자")
        self.assertGreaterEqual(self.starts[1] - self.starts[0], 1.049)
        self.assertGreaterEqual(self.clock - self.starts[-1], 1.049)

    def test_continuation_is_sent_and_every_market_must_complete_before_resolution(self):
        self.opener.open.side_effect = [
            self.response([record("005930", "삼성전자", "0")], headers={"Cont-Yn": "Y", "Next-Key": "page-two"}),
            self.response([record("000660", "SK하이닉스", "0")], headers={"cont-yn": "N"}),
            self.response([record("035900", "JYP Ent.", "10")]),
        ]
        self.assertEqual(names.resolve_stock_name(self.session, "삼성전자"), "005930")
        calls = self.opener.open.call_args_list
        self.assertEqual(len(calls), 3)
        self.assertIsNone(calls[0].args[0].get_header("Cont-yn"))
        self.assertEqual(calls[1].args[0].get_header("Cont-yn"), "Y")
        self.assertEqual(calls[1].args[0].get_header("Next-key"), "page-two")
        self.assertEqual(json.loads(calls[2].args[0].data), {"mrkt_tp": "10"})
        self.assertIsNone(calls[2].args[0].get_header("Next-key"))

    def test_failed_second_market_never_returns_partial_match_and_retry_reuses_first(self):
        self.opener.open.side_effect = [
            self.response(self.catalog["0"]),
            self.response(payload={"return_code": 8005, "return_msg": TOKEN}),
        ]
        with self.assertRaises(names.StockNameError):
            names.resolve_stock_name(self.session, "삼성전자")
        self.opener.open.side_effect = self.route
        self.assertEqual(names.resolve_stock_name(self.session, "삼성전자"), "005930")
        self.assertEqual(self.opener.open.call_count, 3)
        self.assertEqual(json.loads(self.opener.open.call_args.args[0].data), {"mrkt_tp": "10"})

    def test_incomplete_paginated_market_is_not_cached(self):
        self.opener.open.side_effect = [
            self.response([record("005930", "삼성전자", "0")], headers={"cont-yn": "Y", "next-key": "next"}),
            self.response(payload={"return_code": 8005}),
        ]
        with self.assertRaises(names.StockNameError):
            names.resolve_stock_name(self.session, "삼성전자")
        self.assertNotIn(("paper", "0"), names._CACHES[self.session].markets)
        self.opener.open.side_effect = self.route
        names.resolve_stock_name(self.session, "삼성전자")
        self.assertEqual(self.opener.open.call_count, 4)

    def test_missing_expired_and_invalid_sessions_block_requests_even_with_cache(self):
        names.resolve_stock_name(self.session, "삼성전자")
        self.opener.open.reset_mock()
        self.session._now = lambda: datetime(2030, 1, 1, 8, 55, tzinfo=KST)
        for session in (None, make_session(None), self.session):
            with self.assertRaises(names.StockNameError):
                names.resolve_stock_name(session, "삼성전자")
        session = make_session()
        for mode in ("live", "https://example.com", [], True):
            session.mode = mode
            with self.assertRaises(names.StockNameError):
                names.resolve_stock_name(session, "삼성전자")
        session.mode = "paper"
        session._issued = SimpleNamespace(token="bad\r\nheader", expires_dt=None)
        with self.assertRaises(names.StockNameError):
            names.resolve_stock_name(session, "삼성전자")
        self.opener.open.assert_not_called()

    def test_changed_session_during_fetch_aborts_without_caching_or_wrong_host_request(self):
        def switch(request, timeout):
            response = self.route(request, timeout)
            self.session.login("real")
            return response
        self.opener.open.side_effect = switch
        with self.assertRaisesRegex(names.StockNameError, "로그인 상태가 변경"):
            names.resolve_stock_name(self.session, "삼성전자")
        self.assertEqual(self.opener.open.call_count, 1)
        self.assertEqual(names._CACHES[self.session].markets, {})

    def test_bad_pagination_never_echoes_headers_or_follows_unbounded_cursors(self):
        for headers in ({"cont-yn": "Y"}, {"cont-yn": "Y", "next-key": " "},
                        {"cont-yn": True}, {"cont-yn": TOKEN}, {"api-id": "ka10001"},
                        {"cont-yn": "Y", "next-key": "x" * 51},
                        {"cont-yn": "Y", "next-key": "bad\r\nheader"},
                        {"cont-yn": "Y", "Cont-Yn": "N"}, 123):
            self.opener.open.side_effect = None
            self.opener.open.return_value = self.response(self.catalog["0"], headers=headers)
            with self.subTest(headers_type=type(headers)), self.assertRaises(names.StockNameError) as caught:
                names.resolve_stock_name(make_session(), "삼성전자")
            self.assert_safe(caught.exception)
        self.opener.open.return_value = self.response(self.catalog["0"], headers={"cont-yn": "Y", "next-key": "repeat"})
        with self.assertRaisesRegex(names.StockNameError, "연속조회"):
            names.resolve_stock_name(make_session(), "삼성전자")
        self.opener.open.return_value = self.response([], headers={"cont-yn": "Y", "next-key": "next"})
        with self.assertRaisesRegex(names.StockNameError, "연속조회"):
            names.resolve_stock_name(make_session(), "삼성전자")

    def test_page_limit_and_record_limit_fail_closed(self):
        self.opener.open.side_effect = [
            self.response(self.catalog["0"], headers={"cont-yn": "Y", "next-key": "one"}),
            self.response(self.catalog["0"], headers={"cont-yn": "Y", "next-key": "two"}),
        ]
        with patch.object(names, "_MAX_PAGES", 2), self.assertRaisesRegex(names.StockNameError, "최대 페이지"):
            names.resolve_stock_name(self.session, "삼성전자")
        self.opener.open.side_effect = self.route
        with patch.object(names, "_MAX_ROWS", 2), self.assertRaises(names.StockNameError):
            names.resolve_stock_name(make_session(), "삼성전자")

    def test_invalid_name_inputs_never_fetch_or_echo_values(self):
        for value in (None, "", " \t\n", "x" * 201, "숨김\x00문자", "방향\u202e문자"):
            with self.assertRaises(names.StockNameError):
                names.resolve_stock_name(self.session, value)
        self.opener.open.assert_not_called()

    def test_invalid_response_shapes_types_and_names_fail_without_secret_echo(self):
        bad_rows = [None, {}, record(None, "이름"), record("005930", TOKEN),
                    record("005930", "bad\nname"), record("005930", "x" * 41),
                    record("005930", None), record("bad/url", SECRET),
                    record("005930", "이름", True), record("005930", "이름", [])]
        payloads = [[], {}, {"return_code": False}, {"return_code": True},
                    {"return_code": 0.0}, {"return_code": 8005, "return_msg": TOKEN + SECRET},
                    {"return_code": TOKEN}, {"return_code": 0, "list": {}},
                    *[{"return_code": 0, "list": [row]} for row in bad_rows]]
        self.opener.open.side_effect = None
        for payload in payloads:
            self.opener.open.return_value = self.response(payload=payload)
            with self.assertRaises(names.StockNameError) as caught:
                names.resolve_stock_name(make_session(), "삼성전자")
            self.assert_safe(caught.exception)

    def test_malformed_json_oversize_and_transport_errors_are_safe(self):
        self.opener.open.side_effect = None
        for raw in (b"[", b"\xff", b"null", b"x" * (names._MAX_RESPONSE_BYTES + 1), "bad"):
            self.opener.open.return_value = self.response(raw=raw)
            with self.assertRaises(names.StockNameError) as caught:
                names.resolve_stock_name(make_session(), "삼성전자")
            self.assert_safe(caught.exception)
        for failure in (
            urllib.error.HTTPError("https://invalid/" + TOKEN, 401, SECRET, {}, io.BytesIO(TOKEN.encode())),
            urllib.error.URLError(TOKEN), TimeoutError(SECRET),
        ):
            self.opener.open.side_effect = failure
            with self.assertRaises(names.StockNameError) as caught:
                names.resolve_stock_name(make_session(), "삼성전자")
            self.assert_safe(caught.exception)


if __name__ == "__main__":
    unittest.main()
