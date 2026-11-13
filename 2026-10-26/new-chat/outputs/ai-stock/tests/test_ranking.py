"""Offline contracts for ka10032; all HTTP transport is replaced by a fake."""

import json
import unittest
from unittest.mock import patch

from kiwoom_autotrader.kiwoom import KiwoomAPIError, KiwoomMockClient


class FakeResponse:
    def __init__(self, payload, headers=None):
        self.body = json.dumps(payload).encode("utf-8")
        self.headers = headers

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def read(self, limit):
        return self.body[:limit]


def page(rows, headers=None):
    return FakeResponse({"return_code": 0, "trde_prica_upper": rows}, headers)


def row(rank):
    return {"now_rank": str(rank), "stk_cd": f"{rank:06d}", "trde_prica": "12345"}


class TopTurnoverTests(unittest.TestCase):
    def setUp(self):
        opener_patch = patch("kiwoom_autotrader.kiwoom.urllib.request.build_opener")
        self.addCleanup(opener_patch.stop)
        self.opener = opener_patch.start().return_value
        sleep_patch = patch("kiwoom_autotrader.kiwoom.time.sleep")
        self.addCleanup(sleep_patch.stop)
        sleep_patch.start()
        self.client = KiwoomMockClient("fixture-key", "fixture-secret")
        self.opener.open.return_value = FakeResponse(
            {"return_code": 0, "token": "fixture-token", "token_type": "Bearer"}
        )
        self.client.authenticate()
        self.opener.open.reset_mock()

    def requests(self):
        return [call.args[0] for call in self.opener.open.call_args_list]

    def test_default_request_is_authenticated_mock_combined_ranking(self):
        self.opener.open.return_value = page([row(1)])
        self.assertEqual(self.client.top_turnover(), [row(1)])
        request = self.requests()[0]
        self.assertEqual(request.full_url, "https://mockapi.kiwoom.com/api/dostk/rkinfo")
        self.assertEqual(request.method, "POST")
        self.assertEqual(request.get_header("Api-id"), "ka10032")
        self.assertEqual(request.get_header("Authorization"), "Bearer fixture-token")
        self.assertEqual(json.loads(request.data), {
            "mrkt_tp": "001", "mang_stk_incls": "1", "stex_tp": "3"
        })
        self.assertIsNone(request.get_header("Cont-yn"))
        self.assertIsNone(request.get_header("Next-key"))

    def test_market_and_managed_flag_are_mapped(self):
        self.opener.open.return_value = page([])
        for market, exchange in zip(("000", "001", "101"), ("1", "2", "3")):
            with self.subTest(market=market, exchange=exchange):
                self.assertEqual(self.client.top_turnover(market, False, 100, exchange), [])
                self.assertEqual(json.loads(self.requests()[-1].data), {
                    "mrkt_tp": market, "mang_stk_incls": "0", "stex_tp": exchange
                })

    def test_each_page_uses_the_latest_exact_key_and_stops_at_limit(self):
        self.opener.open.side_effect = [
            page([row(1)], {"Cont-Yn": "Y", "Next-Key": "page-key-one"}),
            page([row(2)], {"cont-yn": "Y", "next-key": "page-key-two"}),
            page([row(3), row(4)], {"cont-yn": "Y", "next-key": "page-key-three"}),
        ]
        self.assertEqual(self.client.top_turnover(limit=3), [row(1), row(2), row(3)])
        requests = self.requests()
        self.assertEqual(len(requests), 3)
        self.assertEqual(requests[1].get_header("Cont-yn"), "Y")
        self.assertEqual(requests[1].get_header("Next-key"), "page-key-one")
        self.assertEqual(requests[2].get_header("Next-key"), "page-key-two")
        self.assertEqual([request.data for request in requests], [requests[0].data] * 3)
        self.assertTrue(all(request.full_url.startswith("https://mockapi.kiwoom.com/") for request in requests))

    def test_limit_met_on_first_page_prevents_next_request(self):
        self.opener.open.return_value = page(
            [row(i) for i in range(1, 26)], {"cont-yn": "Y", "next-key": "unused-key"}
        )
        self.assertEqual(len(self.client.top_turnover()), 20)
        self.opener.open.assert_called_once()

    def test_final_page_returns_fewer_than_limit(self):
        self.opener.open.side_effect = [
            page([row(1)], {"cont-yn": "Y", "next-key": "second-page"}),
            page([row(2)], {"cont-yn": "N", "next-key": ""}),
        ]
        self.assertEqual(self.client.top_turnover(limit=20), [row(1), row(2)])

    def test_missing_or_blank_cursor_is_an_error(self):
        for headers in ({"cont-yn": "Y"}, {"cont-yn": "Y", "next-key": ""},
                        {"cont-yn": "Y", "next-key": "  "}):
            with self.subTest(headers=headers):
                self.opener.open.return_value = page([row(1)], headers)
                with self.assertRaises(KiwoomAPIError):
                    self.client.top_turnover()

    def test_repeated_cursor_fails_without_leaking_its_value(self):
        self.opener.open.side_effect = [
            page([row(1)], {"cont-yn": "Y", "next-key": "sensitive-cursor"}),
            page([row(2)], {"cont-yn": "Y", "next-key": "sensitive-cursor"}),
        ]
        with self.assertRaises(KiwoomAPIError) as caught:
            self.client.top_turnover()
        self.assertNotIn("sensitive-cursor", str(caught.exception))
        self.assertEqual(self.opener.open.call_count, 2)

    def test_malformed_rows_and_missing_output_fail(self):
        for payload in ({"return_code": 0}, {"return_code": 0, "trde_prica_upper": {}},
                        {"return_code": 0, "trde_prica_upper": ["sensitive-data"]}):
            with self.subTest(payload=payload):
                self.opener.open.return_value = FakeResponse(payload)
                with self.assertRaises(KiwoomAPIError) as caught:
                    self.client.top_turnover()
                self.assertNotIn("sensitive-data", str(caught.exception))

    def test_bad_continuation_headers_are_sanitized(self):
        for headers in ({"cont-yn": "sensitive-data"},
                        {"cont-yn": "Y", "next-key": "sensitive-data\r\nheader: injected"},
                        {"cont-yn": "Y", "next-key": ["sensitive-data"]}):
            with self.subTest(headers=headers):
                self.opener.open.return_value = page([row(1)], headers)
                with self.assertRaises(KiwoomAPIError) as caught:
                    self.client.top_turnover()
                self.assertNotIn("sensitive-data", str(caught.exception))

    def test_invalid_arguments_do_not_make_requests(self):
        invalid = [("999", False, 20), (None, False, 20), ("001", "1", 20),
                   ("001", False, 0), ("001", False, 101), ("001", False, True),
                   ("001", False, 1.5), ("001", True, 20, "4"),
                   ("001", True, 20, 3)]
        for args in invalid:
            with self.subTest(args=args), self.assertRaises(ValueError):
                self.client.top_turnover(*args)
        self.opener.open.assert_not_called()

    def test_ten_page_bound_is_an_error_if_more_rows_are_needed(self):
        self.opener.open.side_effect = [
            page([row(i)], {"cont-yn": "Y", "next-key": f"page-{i}"})
            for i in range(1, 11)
        ]
        with self.assertRaises(KiwoomAPIError):
            self.client.top_turnover(limit=20)
        self.assertEqual(self.opener.open.call_count, 10)

    def test_authentication_required_and_broker_error_preserved(self):
        unauthenticated = KiwoomMockClient("fixture-key", "fixture-secret")
        with self.assertRaises(KiwoomAPIError):
            unauthenticated.top_turnover()
        self.opener.open.assert_not_called()
        self.opener.open.return_value = FakeResponse(
            {"return_code": 8005, "return_msg": "fixture-secret"}
        )
        with self.assertRaises(KiwoomAPIError) as caught:
            self.client.top_turnover()
        self.assertIn("8005", str(caught.exception))
        self.assertNotIn("fixture-secret", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
