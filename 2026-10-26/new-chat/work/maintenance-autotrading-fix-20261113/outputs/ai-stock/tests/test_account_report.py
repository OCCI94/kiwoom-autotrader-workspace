"""Offline account report contracts; no real HTTP, account, or Keychain calls."""

from copy import deepcopy
from datetime import datetime
import io
import json
import traceback
import unittest
import urllib.error
from unittest.mock import Mock, patch

from kiwoom_autotrader.api import account_report as account, kt00001, kt00018
from kiwoom_autotrader.api.session import BrokerSession, IssuedToken, KST
from kiwoom_autotrader.telegram.commands import report
from kiwoom_autotrader.utils.keychain import Credentials


TOKEN = "account-report-fixture-private-token"
PRIVATE = "private-account-owner-and-number"


def session(mode="paper", expiry="20300101085500"):
    result = BrokerSession(
        now=lambda: datetime(2026, 10, 26, 9, 0, tzinfo=KST),
        credential_loader=Mock(return_value=Credentials("account-fixture-key", "account-fixture-secret")),
        token_issuer=Mock(return_value=IssuedToken(TOKEN, expiry)),
    )
    if mode is not None:
        assert result.login(mode).success
    return result


def cash_payload(**changes):
    result = {"return_code": 0, "entr": "000000001000000", "pymn_alow_amt": "000000000900000",
              "ord_alow_amt": "000000000800000", "d1_entra": "000000000850000", "d2_entra": "-00000000000100",
              "acnt_nm": PRIVATE, "account_no": PRIVATE, "return_msg": PRIVATE + TOKEN}
    result.update(changes)
    return result


def holding(**changes):
    result = {"stk_cd": "A005930", "stk_nm": "삼성전자", "rmnd_qty": "000000000000003",
              "trde_able_qty": "000000000000002", "pur_pric": "000000000070000",
              "cur_prc": "000000071000", "pur_amt": "000000000210000", "evlt_amt": "000000000213000",
              "evltv_prft": "+00000000003000", "prft_rt": "+1.43", "crd_tp": "00", "crd_loan_dt": ""}
    result.update(changes)
    return result


def holdings_payload(rows=None, **changes):
    result = {"return_code": "0", "tot_pur_amt": "000000000210000", "tot_evlt_amt": "000000000213000",
              "tot_evlt_pl": "+00000000003000", "tot_prft_rt": "+1.43",
              "prsm_dpst_aset_amt": "000000001213000", "tot_loan_amt": "000000000000000",
              "acnt_evlt_remn_indv_tot": [holding()] if rows is None else rows,
              "return_msg": PRIVATE, "account_no": PRIVATE}
    result.update(changes)
    return result


class Response:
    def __init__(self, payload=None, headers=None, raw=None):
        self.raw = json.dumps(payload, ensure_ascii=False).encode("utf-8") if raw is None else raw
        self.headers = headers or {}
        self.read_sizes = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, size):
        self.read_sizes.append(size)
        return self.raw


class AccountTransportTests(unittest.TestCase):
    def setUp(self):
        self.active = session()
        factory_patch = patch.object(account.urllib.request, "build_opener")
        sleep_patch = patch.object(account.time, "sleep")
        self.addCleanup(factory_patch.stop)
        self.addCleanup(sleep_patch.stop)
        self.factory = factory_patch.start()
        self.sleep = sleep_patch.start()
        self.opener = self.factory.return_value

    def pages(self, *pages):
        self.opener.open.side_effect = list(pages)

    def test_documented_tr_bodies_and_domains_use_current_session_only(self):
        self.assertEqual(kt00001.request_body(), {"qry_tp": "3"})
        self.assertEqual(kt00001.request_body("2"), {"qry_tp": "2"})
        self.assertEqual(kt00018.request_body(), {"qry_tp": "1", "dmst_stex_tp": "KRX"})
        self.assertEqual(kt00018.request_body("2", "NXT"), {"qry_tp": "2", "dmst_stex_tp": "NXT"})
        for mode, origin in (("paper", "https://mockapi.kiwoom.com"), ("real", "https://api.kiwoom.com")):
            active = session(mode)
            responses = [Response(cash_payload()), Response(holdings_payload())]
            self.opener.reset_mock()
            self.pages(*responses)
            result = account.fetch_account_report(active)
            self.assertEqual(result["mode"], mode)
            self.assertEqual(result["exchange"], "KRX")
            self.assertEqual(result["cash"]["d2_entra"], "-00000000000100")
            self.assertEqual(result["pages"], {"kt00001": 1, "kt00018": 1})
            for call, api_id, body in zip(self.opener.open.call_args_list, ("kt00001", "kt00018"),
                                         ({"qry_tp": "3"}, {"qry_tp": "1", "dmst_stex_tp": "KRX"})):
                request = call.args[0]
                self.assertEqual(request.full_url, origin + "/api/dostk/acnt")
                self.assertEqual(request.method, "POST")
                self.assertEqual(request.get_header("Authorization"), "Bearer " + TOKEN)
                self.assertEqual(request.get_header("Api-id"), api_id)
                self.assertEqual(request.get_header("Content-type"), "application/json;charset=UTF-8")
                self.assertEqual(request.get_header("Cont-yn"), "N")
                self.assertEqual(request.get_header("Next-key"), "")
                self.assertEqual(json.loads(request.data), body)
                self.assertEqual(call.kwargs, {"timeout": 15})
            self.assertTrue(all(r.read_sizes == [account._MAX_RESPONSE_BYTES + 1] for r in responses))
            self.assertEqual(active._credential_loader.call_count, 1)
            self.assertEqual(active._token_issuer.call_count, 1)
            self.assertNotIn(PRIVATE, repr(result))
            self.assertNotIn(TOKEN, repr(result))
            self.assertIsInstance(self.factory.call_args.args[0], account._RejectRedirects)
        self.assertTrue(self.sleep.called)
        for args in (("1",), ("bogus",)):
            with self.assertRaises(ValueError):
                kt00001.request_body(*args)
        for args in (("3", "KRX"), ("1", "ALL")):
            with self.assertRaises(ValueError):
                kt00018.request_body(*args)

    def test_all_continuation_pages_keep_first_totals_and_separate_positions(self):
        first = holding()
        second = holding(stk_cd="A000660", stk_nm="다른종목")
        same_symbol_lot = holding(rmnd_qty="000000000000008", pur_pric="000000000050000")
        distinct_unshown_field = holding(tdy_buyq="1")
        self.pages(
            Response(cash_payload(), {"cont-yn": "Y", "next-key": "cash-next"}),
            Response(cash_payload(entr="000000002000000")),
            Response(holdings_payload([first, same_symbol_lot]), {"cont-yn": "Y", "next-key": "stock-next"}),
            Response(holdings_payload([first, second, distinct_unshown_field], tot_evlt_amt="000000000999999")),
        )
        result = account.fetch_account_report(self.active)
        self.assertEqual(result["cash"]["entr"], "000000001000000")
        self.assertEqual(result["totals"]["tot_evlt_amt"], "000000000213000")
        self.assertTrue(result["snapshot_changed"])
        self.assertEqual(len(result["holdings"]), 4)
        self.assertEqual(sum(r["stk_cd"] == "A005930" for r in result["holdings"]), 3)
        requests = [c.args[0] for c in self.opener.open.call_args_list]
        self.assertEqual([r.get_header("Next-key") for r in requests], ["", "cash-next", "", "stock-next"])
        self.assertEqual([r.get_header("Cont-yn") for r in requests], ["N", "Y", "N", "Y"])
        self.assertEqual(result["pages"], {"kt00001": 2, "kt00018": 2})

    def test_same_page_duplicate_positions_are_not_merged_by_symbol(self):
        self.pages(Response(cash_payload()), Response(holdings_payload([holding(), holding()])))
        self.assertEqual(len(account.fetch_account_report(self.active)["holdings"]), 2)

    def test_missing_values_stay_missing_and_missing_list_is_not_empty_account(self):
        self.pages(Response({"return_code": 0}), Response({"return_code": 0}))
        result = account.fetch_account_report(self.active)
        self.assertTrue(all(v is None for v in result["cash"].values()))
        self.assertTrue(all(v is None for v in result["totals"].values()))
        self.assertIsNone(result["holdings"])
        text = "\n".join(report.format_account_report(result))
        self.assertIn("미제공", text)
        self.assertNotIn("보유종목 없음", text)
        self.assertNotIn("예수금: 0원", text)

    def test_expired_missing_corrupt_and_switched_sessions_do_not_fetch(self):
        expired = session(expiry="20261026090001")
        expired._now = lambda: datetime(2026, 10, 26, 9, 0, 1, tzinfo=KST)
        corrupt = session()
        corrupt.mode = "https://untrusted.example"
        for active in (None, session(None), expired, corrupt):
            with self.subTest(active=repr(active)), self.assertRaises(account.AccountReportError):
                account.fetch_account_report(active)
        self.factory.assert_not_called()
        self.active._issued = None
        with self.assertRaises(account.AccountReportError):
            account.fetch_account_report(self.active)

    def test_session_change_between_trs_rejects_report(self):
        def open_request(*args, **kwargs):
            self.active.mode = "real"
            return Response(cash_payload())
        self.opener.open.side_effect = open_request
        with self.assertRaisesRegex(account.AccountReportError, "변경"):
            account.fetch_account_report(self.active)
        self.assertEqual(self.opener.open.call_count, 1)

    def test_repeated_or_cycle_continuation_key_rejects_partial_result(self):
        for keys in (("same", "same"), ("first", "second", "first")):
            self.pages(*[Response(cash_payload(), {"cont-yn": "Y", "next-key": key}) for key in keys])
            with self.subTest(keys=keys), self.assertRaisesRegex(account.AccountReportError, "반복"):
                account.fetch_account_report(self.active)

    def test_invalid_continuation_headers_fail_without_echo(self):
        for headers in ({"cont-yn": "Y"}, {"cont-yn": "Y", "next-key": " "},
                        {"cont-yn": "Y", "next-key": "x" * 51}, {"cont-yn": "Y", "next-key": "bad\r\n"},
                        {"cont-yn": "Y", "next-key": "비ASCII"}, {"cont-yn": "unexpected"},
                        {"api-id": "kt00004"}):
            self.pages(Response(cash_payload(), headers))
            with self.subTest(headers=headers), self.assertRaises(account.AccountReportError) as caught:
                account.fetch_account_report(self.active)
            self.assertNotIn(TOKEN, str(caught.exception))

    def test_page_and_row_caps_fail_instead_of_truncating(self):
        self.pages(Response(cash_payload(), {"cont-yn": "Y", "next-key": "one"}),
                   Response(cash_payload(), {"cont-yn": "Y", "next-key": "two"}))
        with patch.object(account, "_MAX_PAGES", 2), self.assertRaisesRegex(account.AccountReportError, "한도"):
            account.fetch_account_report(self.active)
        for responses in (
            [Response(cash_payload()), Response(holdings_payload([holding(), holding(), holding()]))],
            [Response(cash_payload()), Response(holdings_payload([holding(), holding()]), {"cont-yn": "Y", "next-key": "more"}),
             Response(holdings_payload([holding(stk_cd="A000660")]))],
        ):
            self.pages(*responses)
            with patch.object(account, "_MAX_HOLDINGS", 2), self.assertRaisesRegex(account.AccountReportError, "한도"):
                account.fetch_account_report(self.active)

    def test_later_page_failure_does_not_return_partial_holdings(self):
        self.pages(Response(cash_payload()), Response(holdings_payload(), {"cont-yn": "Y", "next-key": "more"}),
                   Response({"return_code": -100, "return_msg": PRIVATE + TOKEN}))
        result = report.handle_report(self.active)
        self.assertEqual(len(result), 1)
        self.assertIn("조회 실패", result[0])
        self.assertNotIn("삼성전자", result[0])
        self.assertNotIn(PRIVATE, result[0])
        self.assertNotIn(TOKEN, result[0])

    def test_remote_errors_and_malformed_payloads_do_not_leak_details(self):
        for payload in ({"return_code": False}, {"return_code": True}, {"return_code": PRIVATE}, {}, [],
                        {"return_code": -20, "return_msg": PRIVATE + TOKEN}):
            self.pages(Response(payload))
            with self.subTest(payload=payload), self.assertRaises(account.AccountReportError) as caught:
                account.fetch_account_report(self.active)
            self.assertNotIn(PRIVATE, str(caught.exception))
            self.assertNotIn(TOKEN, str(caught.exception))
        for raw in (b"not-json", b"\xff", b"x" * (account._MAX_RESPONSE_BYTES + 1), "not bytes"):
            self.pages(Response(raw=raw))
            with self.assertRaises(account.AccountReportError):
                account.fetch_account_report(self.active)

    def test_invalid_numeric_fields_and_stock_identifiers_are_rejected(self):
        for bad in (True, 42, "NaN", "1e3", "1,000", "1.5", "1" * 16, TOKEN):
            self.pages(Response(cash_payload(entr=bad)))
            with self.subTest(bad=bad), self.assertRaises(account.AccountReportError):
                account.fetch_account_report(self.active)
        for bad_row in (None, [], holding(stk_nm=TOKEN), holding(stk_nm="bad\nline"),
                        holding(stk_cd="account-number-123"), holding(prft_rt="1.234"),
                        holding(crd_loan_dt="20260230"), holding(rmnd_qty="1.25")):
            self.pages(Response(cash_payload()), Response(holdings_payload([bad_row])))
            with self.subTest(bad_row=bad_row), self.assertRaises(account.AccountReportError):
                account.fetch_account_report(self.active)
        self.pages(Response(cash_payload()), Response(holdings_payload(acnt_evlt_remn_indv_tot={})))
        with self.assertRaises(account.AccountReportError):
            account.fetch_account_report(self.active)

    def test_redirects_and_transport_errors_are_safe(self):
        handler = account._RejectRedirects()
        self.assertIsNone(handler.redirect_request(None, None, 302, "redirect", {}, "https://example.com"))
        for error in (urllib.error.HTTPError("https://example.com/" + TOKEN, 302, PRIVATE, {}, io.BytesIO()),
                      urllib.error.URLError(PRIVATE + TOKEN), OSError(PRIVATE + TOKEN)):
            self.opener.open.side_effect = error
            with self.assertRaises(account.AccountReportError) as caught:
                account.fetch_account_report(self.active)
            self.assertNotIn(PRIVATE, str(caught.exception))
            self.assertNotIn(TOKEN, str(caught.exception))
            rendered = "".join(traceback.format_exception(caught.exception))
            self.assertNotIn(PRIVATE, rendered)
            self.assertNotIn(TOKEN, rendered)


class ReportPresentationTests(unittest.TestCase):
    def make_data(self, rows=None):
        return {"mode": "paper", "cash": {k: cash_payload()[k] for k in account.CASH_FIELDS},
                "totals": {k: holdings_payload()[k] for k in account.TOTAL_FIELDS},
                "holdings": [account._holding(holding(), TOKEN)] if rows is None else rows,
                "snapshot_changed": False}

    def test_money_percent_signs_precision_and_zeros(self):
        data = self.make_data()
        text = "\n".join(report.format_account_report(data))
        for expected in ("모의투자", "국내 계좌 · KRX 기준", "예수금: 1,000,000원", "D+2 추정예수금: -100원",
                         "총대출금: 0원", "평가손익 +3,000원 (+1.43%)", "보유 3주", "삼성전자 (A005930)"):
            self.assertIn(expected, text)
        self.assertEqual(report._number("+000001.50", "%"), "+1.50%")
        self.assertEqual(report._number("000000"), "0원")
        self.assertEqual(report._number(None), "미제공")
        data["mode"] = "real"
        self.assertIn("실전투자", "\n".join(report.format_account_report(data)))

    def test_explicit_empty_holdings_and_missing_optional_values(self):
        self.assertIn("보유종목 없음", "\n".join(report.format_account_report(self.make_data([]))))
        data = self.make_data([account._holding({}, TOKEN)])
        data["cash"]["entr"] = None
        data["snapshot_changed"] = True
        text = "\n".join(report.format_account_report(data))
        self.assertIn("예수금: 미제공", text)
        self.assertIn("종목명 미제공 (코드 미제공)", text)
        self.assertIn("합계는 최초 제공값", text)
        self.assertNotIn("보유 0주", text)

    def test_credit_lot_and_loan_date_are_identifiable(self):
        row = account._holding(holding(crd_tp="03", crd_loan_dt="20261001"), TOKEN)
        text = "\n".join(report.format_account_report(self.make_data([row])))
        self.assertIn("자기융자 · 대출일 2026-10-01", text)

    def test_all_large_report_rows_fit_len_and_utf16_limits_with_emoji(self):
        rows = [account._holding(holding(stk_nm="🚀" * 40, stk_cd=f"A{n:06d}"), TOKEN) for n in range(200)]
        chunks = report.format_account_report(self.make_data(rows))
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(0 < len(c) <= 4096 and len(c.encode("utf-16-le")) // 2 <= 4096 for c in chunks))
        combined = "\n".join(chunks)
        for n in range(200):
            self.assertEqual(combined.count(f"(A{n:06d})"), 1)
        long = "🧪" * 5000
        pieces = report._chunks([long])
        self.assertEqual("".join(pieces), long)
        self.assertTrue(all(report._units(p) <= 3900 for p in pieces))

    def test_handle_report_uses_session_and_conceals_unexpected_errors(self):
        active = session()
        with patch.object(report, "fetch_account_report", return_value=self.make_data()) as fetch:
            replies = report.handle_report(active)
            fetch.assert_called_once_with(active)
            self.assertTrue(replies)
        with patch.object(report, "fetch_account_report", side_effect=RuntimeError(PRIVATE + TOKEN)):
            replies = report.handle_report(active)
            self.assertEqual(len(replies), 1)
            self.assertNotIn(PRIVATE, replies[0])
            self.assertNotIn(TOKEN, replies[0])


if __name__ == "__main__":
    unittest.main()
