"""Offline ranking contracts against queued HTTP responses and local sessions."""

from datetime import datetime
import json
import unittest
from unittest.mock import Mock, patch
import urllib.error

from kiwoom_autotrader.api import rankings
from kiwoom_autotrader.api.rankings import RankingsError, fetch_ranked_stocks
from kiwoom_autotrader.api.session import BrokerSession, IssuedToken, KST
from kiwoom_autotrader.utils.keychain import Credentials


OUTPUTS = {1: "trde_prica_upper", 2: "pred_pre_flu_rt_upper", 3: "tdy_trde_qty_upper", 4: "item_inq_rank"}
METRICS = {1: "trde_prica", 2: "flu_rt", 3: "trde_qty", 4: "bigd_rank"}
TOKEN = "offline-ranking-access-token"


class FakeResponse:
    def __init__(self, payload, headers=None, raw=None):
        self.raw = json.dumps(payload, ensure_ascii=False).encode("utf-8") if raw is None else raw
        self.headers = headers if headers is not None else {}

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self, limit):
        return self.raw[:limit]


def row(number, index=1, value=None, **extra):
    defaults = {1: "12345", 2: "+12.34", 3: "1234567", 4: str(index)}
    return {"stk_cd": f"{index:06d}", "stk_nm": f"종목 {index}",
            METRICS[number]: defaults[number] if value is None else value, **extra}


def page(number, rows, headers=None, **payload):
    return FakeResponse({"return_code": 0, OUTPUTS[number]: rows, **payload}, headers)


class RankingsAPITests(unittest.TestCase):
    def setUp(self):
        self.issuer = Mock(return_value=IssuedToken(TOKEN))
        self.session = BrokerSession(
            now=lambda: datetime(2026, 10, 26, 9, tzinfo=KST),
            credential_loader=lambda _: Credentials("fixture-key", "fixture-secret"),
            token_issuer=self.issuer,
        )
        self.assertTrue(self.session.login("paper").success)
        queue_patch = patch.object(rankings, "queued_open")
        opener_patch = patch.object(rankings.urllib.request, "build_opener")
        self.addCleanup(queue_patch.stop)
        self.addCleanup(opener_patch.stop)
        self.queued = queue_patch.start()
        self.opener = opener_patch.start().return_value

    def requests(self):
        return [call.args[1] for call in self.queued.call_args_list]

    def test_four_documented_requests_use_queue_current_token_and_raw_units(self):
        expected = {
            1: {"mrkt_tp": "000", "mang_stk_incls": "1", "stex_tp": "3"},
            2: {"mrkt_tp": "000", "sort_tp": "1", "trde_qty_cnd": "0000", "stk_cnd": "0",
                "crd_cnd": "0", "updown_incls": "1", "pric_cnd": "0", "trde_prica_cnd": "0", "stex_tp": "3"},
            3: {"mrkt_tp": "000", "sort_tp": "1", "mang_stk_incls": "0", "crd_tp": "0",
                "trde_qty_tp": "0", "pric_tp": "0", "trde_prica_tp": "0", "mrkt_open_tp": "0", "stex_tp": "3"},
            4: {"qry_tp": "1"},
        }
        ids = {1: "ka10032", 2: "ka10027", 3: "ka10030", 4: "ka00198"}
        for number in range(1, 5):
            with self.subTest(number=number):
                self.queued.return_value = page(number, [row(number)])
                result = fetch_ranked_stocks(self.session, number)
                request = self.requests()[-1]
                endpoint = "/api/dostk/stkinfo" if number == 4 else "/api/dostk/rkinfo"
                self.assertEqual(request.full_url, "https://mockapi.kiwoom.com" + endpoint)
                self.assertEqual(request.get_method(), "POST")
                self.assertEqual(request.get_header("Authorization"), "Bearer " + TOKEN)
                self.assertEqual(request.get_header("Api-id"), ids[number])
                self.assertIsNone(request.get_header("Cont-yn"))
                self.assertEqual(json.loads(request.data), expected[number])
                self.assertEqual(result[0].value, row(number)[METRICS[number]])
                self.assertEqual(result[0].code, "000001")
                self.assertEqual(self.queued.call_args.kwargs["timeout"], 15)
                self.assertEqual(self.queued.call_args.kwargs["max_bytes"], rankings._MAX_RESPONSE_BYTES)
                self.assertTrue(callable(self.queued.call_args.kwargs["before_send"]))
        self.issuer.assert_called_once()
        self.opener.open.assert_not_called()

    def test_real_mode_is_used_without_mock_fallback_or_authentication(self):
        self.assertTrue(self.session.login("real").success)
        self.issuer.reset_mock()
        self.queued.return_value = page(2, [row(2)])
        fetch_ranked_stocks(self.session, 2)
        self.assertEqual(self.requests()[0].full_url, "https://api.kiwoom.com/api/dostk/rkinfo")
        self.issuer.assert_not_called()

    def test_provider_order_unique_codes_and_exact_cursor_are_preserved(self):
        self.queued.side_effect = [
            page(1, [row(1, 8, "10", now_rank="8"), row(1, 2, "99999")],
                 {"Cont-Yn": "Y", "Next-Key": "cursor/one="}),
            page(1, [row(1, 8, "20000"), row(1, 3, "1")],
                 {"cont-yn": "Y", "next-key": "cursor_two"}),
            page(1, [row(1, 4), row(1, 5)], {"cont-yn": "N"}),
        ]
        result = fetch_ranked_stocks(self.session, 1, limit=4)
        self.assertEqual([stock.code for stock in result], ["000008", "000002", "000003", "000004"])
        self.assertEqual(result[0].value, "10")
        self.assertEqual(result[0].provider_rank, "8")
        requests = self.requests()
        self.assertEqual(requests[1].get_header("Cont-yn"), "Y")
        self.assertEqual(requests[1].get_header("Next-key"), "cursor/one=")
        self.assertEqual(requests[2].get_header("Next-key"), "cursor_two")
        self.assertTrue(all(request.data == requests[0].data for request in requests))

    def test_twenty_stops_without_requesting_or_requiring_unused_cursor(self):
        self.queued.return_value = page(3, [row(3, i) for i in range(1, 26)], {"cont-yn": "Y"})
        result = fetch_ranked_stocks(self.session, 3)
        self.assertEqual(len(result), 20)
        self.assertEqual(result[-1].code, "000020")
        self.queued.assert_called_once()

    def test_final_short_and_empty_lists_are_valid(self):
        for rows in ([], [row(2)]):
            with self.subTest(rows=rows):
                self.queued.return_value = page(2, rows, {"cont-yn": "N"})
                self.assertEqual(len(fetch_ranked_stocks(self.session, 2)), len(rows))

    def test_exchange_suffixes_remain_distinct(self):
        self.queued.return_value = page(1, [row(1, stk_cd="005930"), row(1, stk_cd="005930_AL"),
                                                row(1, stk_cd="005930_NX")])
        self.assertEqual([s.code for s in fetch_ranked_stocks(self.session, 1)],
                         ["005930", "005930_AL", "005930_NX"])

    def test_missing_optional_values_and_popularity_metadata(self):
        self.queued.return_value = page(3, [{"stk_cd": "005930"}])
        stock = fetch_ranked_stocks(self.session, 3)[0]
        self.assertEqual((stock.name, stock.value), ("", ""))
        self.queued.return_value = page(4, [row(4, 1, "0", rank_chg="", rank_chg_sign="N",
                                                    dt="20261026", tm="090100")])
        stock = fetch_ranked_stocks(self.session, 4)[0]
        self.assertEqual((stock.value, stock.provider_rank, stock.basis_at), ("0", "0", "2026-10-26 09:01:00"))
        self.assertEqual(stock.rank_change, "")

    def test_camelcase_status_from_volume_spec_is_supported_but_conflicts_fail(self):
        self.queued.return_value = FakeResponse({"returnCode": 0, OUTPUTS[3]: [row(3)]})
        self.assertEqual(len(fetch_ranked_stocks(self.session, 3)), 1)
        for statuses in ({"returnCode": 8001}, {"return_code": 0, "returnCode": 8001},
                         {"return_code": True}, {"return_code": None}):
            with self.subTest(statuses=statuses):
                self.queued.return_value = FakeResponse({OUTPUTS[3]: [], **statuses})
                with self.assertRaises(RankingsError):
                    fetch_ranked_stocks(self.session, 3)

    def test_invalid_arguments_do_not_reach_queue(self):
        for number, limit in ((0, 20), (5, 20), (True, 20), ("1", 20), (1, 0), (1, 21), (1, True), (1, 1.5)):
            with self.subTest(number=number, limit=limit), self.assertRaises(ValueError):
                fetch_ranked_stocks(self.session, number, limit)
        self.queued.assert_not_called()

    def test_missing_and_expired_sessions_do_not_reach_queue(self):
        with self.assertRaisesRegex(RankingsError, "먼저 login"):
            fetch_ranked_stocks(BrokerSession(), 1)
        self.issuer.return_value = IssuedToken(TOKEN, "20261026093000")
        self.session.login("paper")
        self.session._now = lambda: datetime(2026, 10, 26, 10, tzinfo=KST)
        with self.assertRaisesRegex(RankingsError, "만료"):
            fetch_ranked_stocks(self.session, 1)
        self.queued.assert_not_called()

    def test_session_change_between_pages_fails_without_mixing_servers(self):
        def response(*_args, **_kwargs):
            self.session.login("real")
            return page(1, [row(1)], {"cont-yn": "Y", "next-key": "next"})
        self.queued.side_effect = response
        with self.assertRaisesRegex(RankingsError, "변경"):
            fetch_ranked_stocks(self.session, 1)
        self.queued.assert_called_once()

    def test_queue_guard_rechecks_session_after_wait_and_before_transport(self):
        def queued(_opener, _request, **options):
            self.session.clear()
            options["before_send"]()
            self.fail("expired queued query reached transport")
        self.queued.side_effect = queued
        with self.assertRaisesRegex(RankingsError, "먼저 login"):
            fetch_ranked_stocks(self.session, 1)
        self.opener.open.assert_not_called()

    def test_bad_continuation_headers_and_repeated_or_nonprogress_pages_fail(self):
        for headers in ({"cont-yn": "Y"}, {"cont-yn": "Y", "next-key": " "},
                        {"cont-yn": "Y", "next-key": "secret\r\ninject"},
                        {"cont-yn": "Y", "next-key": "x" * 51},
                        {"cont-yn": ["Y"]}, {"cont-yn": "unexpected"}):
            with self.subTest(headers=headers):
                self.queued.return_value = page(1, [row(1)], headers)
                with self.assertRaises(RankingsError):
                    fetch_ranked_stocks(self.session, 1)
        for second in (page(1, [row(1, 2)], {"cont-yn": "Y", "next-key": "sensitive-cursor"}),
                       page(1, [row(1)], {"cont-yn": "Y", "next-key": "new"}),
                       page(1, [], {"cont-yn": "Y", "next-key": "new"})):
            self.queued.side_effect = [page(1, [row(1)], {"cont-yn": "Y", "next-key": "sensitive-cursor"}), second]
            with self.assertRaises(RankingsError) as caught:
                fetch_ranked_stocks(self.session, 1)
            self.assertNotIn("sensitive-cursor", str(caught.exception))

    def test_page_bound_and_midstream_failure_never_return_partial_rankings(self):
        self.queued.side_effect = [page(1, [row(1, i)], {"cont-yn": "Y", "next-key": str(i)}) for i in (1, 2)]
        with patch.object(rankings, "_MAX_PAGES", 2), self.assertRaisesRegex(RankingsError, "최대 페이지"):
            fetch_ranked_stocks(self.session, 1)
        self.queued.side_effect = [page(1, [row(1)], {"cont-yn": "Y", "next-key": "next"}), OSError(TOKEN)]
        with self.assertRaises(RankingsError) as caught:
            fetch_ranked_stocks(self.session, 1)
        self.assertNotIn(TOKEN, str(caught.exception))

    def test_malformed_rows_status_body_and_secret_echo_are_rejected(self):
        invalid = [
            {}, {"return_code": 0}, {"return_code": 0, OUTPUTS[1]: {}},
            {"return_code": 0, OUTPUTS[1]: ["secret"]},
            {"return_code": 0, OUTPUTS[1]: [row(1, stk_cd="../005930")]},
            {"return_code": 0, OUTPUTS[1]: [row(1, stk_nm=TOKEN)]},
            {"return_code": 0, OUTPUTS[1]: [row(1, stk_nm="name\ncommand")]},
            {"return_code": 0, OUTPUTS[1]: [row(1, 1, "NaN")]},
            {"return_code": 0, OUTPUTS[1]: [row(1, value="-1")]},
            {"return_code": 7001, "return_msg": TOKEN, OUTPUTS[1]: []},
        ]
        for payload in invalid:
            with self.subTest(payload=payload):
                self.queued.return_value = FakeResponse(payload)
                with self.assertRaises(RankingsError) as caught:
                    fetch_ranked_stocks(self.session, 1)
                self.assertNotIn(TOKEN, str(caught.exception))
        for raw in (b"not-json", b"x" * (rankings._MAX_RESPONSE_BYTES + 1)):
            self.queued.return_value = FakeResponse({}, raw=raw)
            with self.assertRaises(RankingsError):
                fetch_ranked_stocks(self.session, 1)

    def test_http_error_is_sanitized_and_not_retried(self):
        self.queued.side_effect = urllib.error.HTTPError("https://invalid/" + TOKEN, 503, TOKEN, {}, None)
        with self.assertRaises(RankingsError) as caught:
            fetch_ranked_stocks(self.session, 4)
        self.assertIn("503", str(caught.exception))
        self.assertNotIn(TOKEN, str(caught.exception))
        self.queued.assert_called_once()


if __name__ == "__main__":
    unittest.main()
