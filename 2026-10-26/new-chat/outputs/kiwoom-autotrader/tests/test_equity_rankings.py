"""Offline stock classification/ranking tests; no credential store or network."""

import io
import json
import traceback
import unittest
import urllib.error
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

from kiwoom_autotrader import equity_rankings as ranking
from kiwoom_autotrader.kiwoom import KiwoomAPIError, KiwoomMockClient


class EquityRankingTests(unittest.TestCase):
    def setUp(self):
        opener_patch = patch("kiwoom_autotrader.kiwoom.urllib.request.build_opener")
        self.addCleanup(opener_patch.stop)
        self.opener = opener_patch.start().return_value
        sleep_patch = patch.object(ranking.time, "sleep")
        self.addCleanup(sleep_patch.stop)
        self.sleep = sleep_patch.start()
        self.client = KiwoomMockClient("offline-key", "offline-secret")
        self.client._token = "offline-token"

    def response(self, payload, headers=None):
        response = MagicMock()
        response.__enter__.return_value = response
        response.headers = {} if headers is None else headers
        response.read.return_value = json.dumps(payload).encode("utf-8")
        return response

    @staticmethod
    def master(code, market):
        return {"code": code, "marketCode": market}

    @staticmethod
    def row(code, amount="12345.67", name="테스트기업"):
        return {"stk_cd": code, "stk_nm": name, "trde_prica": amount, "now_rank": "1"}

    def lists(self):
        return {
            "0": [self.master("005930", "0"), self.master("00680K", "0")],
            "10": [self.master("035900", "10")],
            "8": [], "60": [], "70": [], "90": [],
        }

    def run_with(self, lists, rows, limit=2):
        with patch.object(ranking, "_instrument_list", side_effect=lambda client, market: lists[market]), \
             patch.object(self.client, "top_turnover", return_value=rows) as top:
            result = ranking.get_top_equities(self.client, limit)
            top.assert_called_once_with(market="000", include_managed=True, limit=100, exchange="3")
            return result

    def assert_redacted(self, text):
        for secret in ("offline-key", "offline-secret", "offline-token"):
            self.assertNotIn(secret, text)

    def test_filters_actual_market_codes_preserving_broker_order_and_units(self):
        lists = self.lists()
        lists["0"].extend([self.master("069500", "8"), self.master("123450", "6")])
        lists["8"] = [self.master("069500", "8")]
        rows = [self.row("069500", "999999"), self.row("123450", "888888"),
                self.row("035900_AL", "100.5"), self.row("005930_NX", "500.75"),
                self.row("00680K", "999999")]
        snapshot = self.run_with(lists, rows)
        self.assertEqual(snapshot["rows"], [
            {"rank": 1, "symbol": "035900", "name": "테스트기업", "market": "코스닥", "turnover_100million_krw": "1.005"},
            {"rank": 2, "symbol": "005930", "name": "테스트기업", "market": "코스피", "turnover_100million_krw": "5.0075"},
        ])
        self.assertEqual(snapshot["environment"], "mock")
        self.assertIs(snapshot["include_managed"], True)
        self.assertEqual(snapshot["ranking_rows_scanned"], len(rows))
        self.assertEqual(snapshot["classification_counts"]["0"], 4)
        self.assertEqual(datetime.fromisoformat(snapshot["queried_at_kst"]).utcoffset(), timedelta(hours=9))
        self.assert_redacted(json.dumps(snapshot))

    def test_separate_exclusion_lists_override_equity_membership(self):
        lists = self.lists()
        # Even an exclusion-list row reporting code 0 must never be included.
        lists["60"] = [self.master("005930", "0")]
        snapshot = self.run_with(lists, [self.row("005930"), self.row("00680K"), self.row("035900")])
        self.assertEqual([row["symbol"] for row in snapshot["rows"]], ["00680K", "035900"])

    def test_normalizes_al_nx_and_counts_a_symbol_only_once(self):
        result = self.run_with(self.lists(), [self.row("005930_AL"), self.row("005930_NX"),
                                            self.row("005930"), self.row("00680K")])
        self.assertEqual([row["symbol"] for row in result["rows"]], ["005930", "00680K"])

    def test_default_top20_fails_when_filtered_result_is_incomplete(self):
        with self.assertRaises(KiwoomAPIError):
            self.run_with(self.lists(), [self.row("005930"), self.row("035900")], limit=20)

    def test_default_top20_returns_twenty_distinct_rows(self):
        lists = self.lists()
        lists["0"] = [self.master(f"{index:06d}", "0") for index in range(20)]
        rows = [self.row(f"{index:06d}") for index in range(20)]
        result = self.run_with(lists, rows, limit=20)
        self.assertEqual(len(result["rows"]), 20)
        self.assertEqual([row["rank"] for row in result["rows"]], list(range(1, 21)))

    def test_conflicting_missing_and_wrongly_typed_classification_fail_closed(self):
        for record in ({"code": "005930"}, self.master("005930", None),
                       self.master("005930", True), self.master("005930", "bad"),
                       self.master("bad/code", "0")):
            lists = self.lists()
            lists["0"] = [record]
            with self.assertRaises(KiwoomAPIError):
                self.run_with(lists, [self.row("005930"), self.row("035900")])
        lists = self.lists()
        lists["10"].append(self.master("005930", "10"))
        with self.assertRaises(KiwoomAPIError):
            self.run_with(lists, [self.row("005930"), self.row("035900")])

    def test_empty_or_non_equity_classification_does_not_produce_partial_result(self):
        for records in ([], [self.master("069500", "8")]):
            lists = self.lists()
            lists["0"] = records
            with self.assertRaises(KiwoomAPIError):
                self.run_with(lists, [self.row("035900")], limit=1)

    def test_numeric_market_codes_are_accepted_but_not_boolean(self):
        lists = self.lists()
        lists["0"][0]["marketCode"] = 0
        lists["10"][0]["marketCode"] = 10
        result = self.run_with(lists, [self.row("005930"), self.row("035900")])
        self.assertEqual([row["market"] for row in result["rows"]], ["코스피", "코스닥"])

    def test_bad_turnover_name_or_code_fails_without_payload_echo(self):
        for row in ([self.row("005930", value) for value in (None, True, 1, "NaN", "Infinity", "-1", "1e9", "1,000", "offline-token")]
                    + [self.row("005930", name=value) for value in (None, "", "name\noffline-token")]
                    + [self.row("005930_BAD")]):
            with self.assertRaises(KiwoomAPIError) as caught:
                self.run_with(self.lists(), [row], limit=1)
            self.assert_redacted(str(caught.exception))

    def test_invalid_limit_and_unauthenticated_client_do_not_request(self):
        for limit in (0, 101, True, "20"):
            with self.assertRaises(ValueError):
                ranking.get_top_equities(self.client, limit)
        self.client._token = None
        with self.assertRaises(KiwoomAPIError):
            ranking.get_top_equities(self.client)
        self.opener.open.assert_not_called()

    def test_classification_uses_only_fixed_mock_stock_list_endpoint(self):
        self.opener.open.return_value = self.response({"return_code": 0, "list": [self.master("005930", "0")]})
        rows, continuation = ranking._classification_page(self.client, "0", None)
        request = self.opener.open.call_args.args[0]
        self.assertEqual(request.full_url, "https://mockapi.kiwoom.com/api/dostk/stkinfo")
        self.assertEqual(request.get_header("Api-id"), "ka10099")
        self.assertEqual(request.get_header("Authorization"), "Bearer offline-token")
        self.assertEqual(json.loads(request.data), {"mrkt_tp": "0"})
        self.assertEqual(self.opener.open.call_args.kwargs, {"timeout": 15})
        self.assertEqual(rows, [self.master("005930", "0")])
        self.assertEqual(continuation, {})

    def test_classification_shares_client_throttle_clock(self):
        self.client._last_request_at = 10.0
        self.opener.open.return_value = self.response({"return_code": 0, "list": []})
        with patch.object(ranking.time, "monotonic", side_effect=[10.2, 11.05]):
            ranking._classification_page(self.client, "8", None)
        self.sleep.assert_called_once()
        self.assertAlmostEqual(self.sleep.call_args.args[0], 0.85)
        self.assertEqual(self.client._last_request_at, 11.05)

    def test_classification_pagination_sends_valid_continuation(self):
        self.opener.open.side_effect = [
            self.response({"return_code": 0, "list": [self.master("005930", "0")]}, {"cont-yn": "Y", "next-key": "page-two"}),
            self.response({"return_code": "0", "list": [self.master("00680K", "0")]}, {"cont-yn": "N"}),
        ]
        self.assertEqual(len(ranking._instrument_list(self.client, "0")), 2)
        request = self.opener.open.call_args.args[0]
        self.assertEqual(request.get_header("Cont-yn"), "Y")
        self.assertEqual(request.get_header("Next-key"), "page-two")

    def test_bad_pagination_and_metadata_fail_closed(self):
        for headers in ({"cont-yn": "Y"}, {"cont-yn": "Y", "next-key": " "},
                        {"cont-yn": True}, {"cont-yn": "offline-token"}, 123):
            self.opener.open.return_value = self.response({"return_code": 0, "list": [self.master("005930", "0")]}, headers)
            with self.assertRaises(KiwoomAPIError) as caught:
                ranking._instrument_list(self.client, "0")
            self.assert_redacted(str(caught.exception))
        self.opener.open.return_value = self.response(
            {"return_code": 0, "list": [self.master("005930", "0")]}, {"cont-yn": "Y", "next-key": "repeat"})
        with self.assertRaises(KiwoomAPIError):
            ranking._instrument_list(self.client, "0")

    def test_failed_and_malformed_response_status_never_echoes_payload(self):
        for payload in ([], {}, {"return_code": False, "list": []},
                        {"return_code": 5, "return_msg": "offline-token"},
                        {"return_code": 0, "list": {}}, {"return_code": 0, "list": [None]}):
            self.opener.open.return_value = self.response(payload)
            with self.assertRaises(KiwoomAPIError) as caught:
                ranking._classification_page(self.client, "0", None)
            self.assert_redacted(str(caught.exception))
        for raw in (b"offline-token", b"\xff", b"x" * (ranking._MAX_RESPONSE_BYTES + 1)):
            response = self.response({})
            response.read.return_value = raw
            self.opener.open.return_value = response
            with self.assertRaises(KiwoomAPIError) as caught:
                ranking._classification_page(self.client, "0", None)
            self.assert_redacted(str(caught.exception))

    def test_http_and_network_failures_suppress_urls_and_exception_causes(self):
        for error in (urllib.error.HTTPError("https://invalid/?offline-token", 401,
                                            "offline-secret", {}, io.BytesIO(b"offline-key")),
                      urllib.error.URLError("offline-token")):
            self.opener.open.side_effect = error
            try:
                ranking._classification_page(self.client, "0", None)
            except KiwoomAPIError as caught:
                self.assert_redacted("".join(traceback.format_exception(caught)))
            else:
                self.fail("Network failure must raise a sanitized error")


if __name__ == "__main__":
    unittest.main()
