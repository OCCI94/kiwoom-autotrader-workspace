"""Execution provenance contracts with no network or credentials."""
from datetime import datetime
from decimal import Decimal
import json
import urllib.error
import unittest
from unittest.mock import Mock, patch

from kiwoom_autotrader.api import executions as api
from kiwoom_autotrader.api.session import BrokerSession, IssuedToken, KST


def fill_row(**updates):
    return {"stk_bond_tp": "1", "ord_no": "0000024", "cntr_no": "0000001", "stk_cd": "A005930",
            "stk_nm": "삼성전자", "io_tp_nm": "현금매도", "cntr_qty": "0000000002",
            "cntr_uv": "0000070000", "cntr_tm": "10:03:05", "dmst_stex_tp": "KRX", **updates}


class ExecutionsTests(unittest.TestCase):
    def setUp(self):
        self.session = BrokerSession(now=lambda: datetime(2026, 10, 26, 10, tzinfo=KST))
        self.session.mode = "paper"
        self.session._issued = IssuedToken("offline-ledger-private-token", "20300101000000")
        patcher = patch.object(api, "queued_open")
        self.queue = patcher.start()
        self.addCleanup(patcher.stop)

    def response(self, payload, headers=None):
        response = Mock()
        response.headers = headers or {}
        response.status = 200
        response.read.return_value = json.dumps(payload).encode()
        wrapper = Mock()
        wrapper.__enter__ = Mock(return_value=response)
        wrapper.__exit__ = Mock(return_value=False)
        return wrapper

    def test_actual_fill_id_time_price_and_exact_request_contract(self):
        self.queue.return_value = self.response({"return_code": 0, "acnt_ord_cntr_prst_array": [fill_row()]})
        result = api.fetch_executions(self.session, "20261026")
        self.assertEqual(result, (api.Execution("20261026", "24", "1", "005930", "삼성전자", "sell", "10:03:05", Decimal(70000), 2),))
        request = self.queue.call_args.args[1]
        self.assertEqual(request.get_header("Api-id"), "kt00009")
        self.assertEqual(request.full_url, "https://mockapi.kiwoom.com/api/dostk/acnt")
        self.assertEqual(json.loads(request.data), {"ord_dt": "20261026", "stk_bond_tp": "1", "mrkt_tp": "0",
            "sell_tp": "0", "qry_tp": "1", "stk_cd": "", "fr_ord_no": "", "dmst_stex_tp": "KRX"})
        self.queue.call_args.kwargs["before_send"]()

    def test_full_pagination_deduplicates_overlap_and_rejects_conflicts(self):
        self.queue.side_effect = [
            self.response({"return_code": 0, "acnt_ord_cntr_prst_array": [fill_row()]}, {"cont-yn": "Y", "next-key": "next"}),
            self.response({"return_code": 0, "acnt_ord_cntr_prst_array": [fill_row(), fill_row(cntr_no="0000002", cntr_qty="1")]})]
        self.assertEqual(len(api.fetch_executions(self.session, "20261026")), 2)
        self.assertEqual(self.queue.call_args.args[1].get_header("Next-key"), "next")
        self.queue.side_effect = None
        self.queue.return_value = self.response({"return_code": 0, "acnt_ord_cntr_prst_array": [fill_row(), fill_row(cntr_uv="1")]})
        with self.assertRaises(api.ExecutionsError):
            api.fetch_executions(self.session, "20261026")

    def test_order_time_is_never_substituted_for_fill_time(self):
        for field, value in (("cntr_tm", None), ("cntr_tm", "29:00:00"), ("cntr_no", ""), ("cntr_qty", "1.2"), ("cntr_uv", "0")):
            self.queue.return_value = self.response({"return_code": 0, "acnt_ord_cntr_prst_array": [fill_row(ord_tm="10:01:00", **{field: value})]})
            with self.subTest(field=field, value=value), self.assertRaises(api.ExecutionsError):
                api.fetch_executions(self.session, "20261026")

    def test_partial_or_failed_response_never_becomes_complete_fill_list(self):
        for payload in ({"return_code": True, "acnt_ord_cntr_prst_array": []}, {"return_code": 9}, {"return_code": 0}):
            self.queue.return_value = self.response(payload)
            with self.assertRaises(api.ExecutionsError):
                api.fetch_executions(self.session, "20261026")
        self.queue.side_effect = [self.response({"return_code": 0, "acnt_ord_cntr_prst_array": [fill_row()]}, {"cont-yn": "Y", "next-key": "x"}), OSError("private-secret")]
        with self.assertRaises(api.ExecutionsError) as error:
            api.fetch_executions(self.session, "20261026")
        self.assertNotIn("private-secret", str(error.exception))

    def test_cost_basis_fees_and_tax_retain_decimal_precision(self):
        self.queue.return_value = self.response({"return_code": 0, "dt_stk_div_rlzt_pl": [{
            "stk_cd": "A005930", "crd_tp": "현금잔고", "cntr_qty": "1", "cntr_pric": "158200",
            "buy_uv": "97602.9573459", "tdy_trde_cmsn": "500", "tdy_trde_tax": "284"}]})
        result = api.fetch_realized_profit(self.session, "005930", "20261026")
        self.assertEqual(result[0].buy_price, Decimal("97602.9573459"))
        self.assertEqual(json.loads(self.queue.call_args.args[1].data), {"stk_cd": "005930", "strt_dt": "20261026"})
        self.assertEqual(self.queue.call_args.args[1].get_header("Api-id"), "ka10072")

    def test_unknown_fee_is_not_zero_and_wrong_symbol_is_rejected(self):
        for update in ({"tdy_trde_cmsn": None}, {"stk_cd": "A000660"}):
            row = {"stk_cd": "A005930", "crd_tp": "현금잔고", "cntr_qty": "1", "cntr_pric": "100", "buy_uv": "80", "tdy_trde_cmsn": "0", "tdy_trde_tax": "0", **update}
            self.queue.return_value = self.response({"return_code": 0, "dt_stk_div_rlzt_pl": [row]})
            with self.assertRaises(api.ExecutionsError):
                api.fetch_realized_profit(self.session, "005930", "20261026")

    def test_period_profit_retains_separate_reported_costs_and_documented_text_field(self):
        observed = []
        row = {"dt":"20261026", "stk_cd":"A005930", "crd_tp":"현금잔고", "cntr_qty":"7",
               "cntr_pric":"20400", "buy_uv":"20150", "tdy_trde_cmsn":"990", "tdy_trde_tax":"285",
               "tdy_sel_pl":"475", "tdy_htssel_cmsn":"현금", "account_number":"private-account"}
        self.queue.return_value = self.response({"return_code":0, "dt_stk_rlzt_pl":[row]})
        result = api.fetch_period_realized_profit(self.session, "005930", "20261026", observer=observed.append)
        self.assertEqual((result[0].commission, result[0].reported_net), (Decimal(990), Decimal(475)))
        self.assertEqual(self.queue.call_args.args[1].get_header("Api-id"), "ka10073")
        self.assertEqual(json.loads(self.queue.call_args.args[1].data), {"stk_cd":"005930", "strt_dt":"20261026", "end_dt":"20261026"})
        saved = observed[0]["response_body"]["dt_stk_rlzt_pl"][0]
        self.assertEqual(saved["tdy_htssel_cmsn"], "현금")
        self.assertNotIn("account_number", saved)
        self.queue.return_value = self.response({"return_code":0, "dt_stk_rlzt_pl":[{**row, "dt":"20261025"}]})
        with self.assertRaises(api.ExecutionsError): api.fetch_period_realized_profit(self.session, "005930", "20261026")

    def test_ip_error_is_classified_without_using_untrusted_response_message(self):
        self.queue.return_value = self.response({"return_code": 8050, "return_msg": "private server detail"})
        with patch("kiwoom_autotrader.api.errors.annotate_ip_error", side_effect=lambda code, message: message + " safe IP guidance") as annotate:
            with self.assertRaises(api.ExecutionsError) as caught:
                api.fetch_executions(self.session, "20261026")
        self.assertTrue(caught.exception.ip_related)
        self.assertIn("safe IP guidance", str(caught.exception))
        self.assertNotIn("private server detail", str(caught.exception))
        self.assertEqual(annotate.call_args.args[0], 8050)

    def test_observer_preserves_page_order_original_values_and_safe_metadata(self):
        observed = []
        self.queue.side_effect = [
            self.response({"return_code": 0, "buy_engg_amt": "000000040784", "acnt_ord_cntr_prst_array": [fill_row(cntr_uv="0000020392")]},
                          {"cont-yn": "Y", "next-key": "opaque-account-continuation", "authorization": "Bearer " + self.session.token}),
            self.response({"return_code": 0, "acnt_ord_cntr_prst_array": [fill_row(cntr_no="0000002", cntr_qty="0000000003")]})]
        result = api.fetch_executions(self.session, "20261026", observer=observed.append)
        self.assertEqual(len(result), 2)
        self.assertEqual([row["page"] for row in observed], [1, 2])
        self.assertEqual(observed[0]["query_id"], observed[1]["query_id"])
        self.assertEqual(observed[0]["response_body"]["buy_engg_amt"], "000000040784")
        self.assertEqual(observed[0]["response_body"]["acnt_ord_cntr_prst_array"][0]["cntr_uv"], "0000020392")
        self.assertEqual(observed[1]["continuation"]["request_cont_yn"], "Y")
        self.assertTrue(observed[0]["continuation"]["next_key_present"])
        self.assertEqual(observed[0]["http_status"], 200)
        for event in observed:
            self.assertEqual(event["mode"], "paper")
            self.assertEqual(event["day"], "20261026")
            self.assertEqual(event["source_api"], "kt00009")
            self.assertEqual(event["source_kind"], "broker_observation")
            self.assertLessEqual(datetime.fromisoformat(event["request_started_at"]), datetime.fromisoformat(event["observed_at"]))
        text = json.dumps(observed)
        self.assertNotIn(self.session.token, text)
        self.assertNotIn("opaque-account-continuation", text)
        self.assertNotIn("authorization", text)

    def test_observer_records_changed_snapshot_before_duplicate_conflict_is_rejected(self):
        observed = []
        self.queue.side_effect = [
            self.response({"return_code": 0, "acnt_ord_cntr_prst_array": [fill_row(cntr_qty="2")]}, {"cont-yn": "Y", "next-key": "next"}),
            self.response({"return_code": 0, "acnt_ord_cntr_prst_array": [fill_row(cntr_qty="5", cntr_uv="20392")]})]
        with self.assertRaises(api.ExecutionsError):
            api.fetch_executions(self.session, "20261026", observer=observed.append)
        self.assertEqual([event["response_body"]["acnt_ord_cntr_prst_array"][0]["cntr_qty"] for event in observed], ["2", "5"])

    def test_observer_excludes_unknown_keys_credentials_and_opaque_account_data(self):
        row = fill_row(acnt_no="123456789012", token=self.session.token, stk_nm=self.session.token,
                       ignored={"appkey": "private-api-key"})
        self.queue.return_value = self.response({"return_code": 0, "return_msg": "private-provider-message", "account": "123456789012",
            "secretkey": "private-app-secret", "acnt_ord_cntr_prst_array": [row]})
        observed = []
        with self.assertRaises(api.ExecutionsError):
            api.fetch_executions(self.session, "20261026", observer=observed.append)
        text = json.dumps(observed)
        for value in ("123456789012", self.session.token, "private-provider-message", "private-app-secret", "private-api-key", "acnt_no", "return_msg"):
            self.assertNotIn(value, text)
        safe_row = observed[0]["response_body"]["acnt_ord_cntr_prst_array"][0]
        self.assertNotIn("stk_nm", safe_row)
        self.assertEqual(safe_row["ord_no"], "0000024")

    def test_observer_failure_blocks_result_and_further_pages_without_private_error(self):
        self.queue.return_value = self.response({"return_code": 0, "acnt_ord_cntr_prst_array": [fill_row()]}, {"cont-yn": "Y", "next-key": "next"})
        observer = Mock(side_effect=RuntimeError(self.session.token))
        with self.assertRaises(api.BrokerObservationError) as caught:
            api.fetch_executions(self.session, "20261026", observer=observer)
        self.assertTrue(caught.exception.observation_failed)
        self.assertNotIn(self.session.token, str(caught.exception))
        self.assertEqual(self.queue.call_count, 1)
        observer.assert_called_once()

    def test_failed_later_page_and_invalid_body_still_leave_safe_observations(self):
        observed = []
        self.queue.side_effect = [
            self.response({"return_code": 0, "acnt_ord_cntr_prst_array": [fill_row()]}, {"cont-yn": "Y", "next-key": "next"}),
            self.response({"return_code": 9, "return_msg": self.session.token})]
        with self.assertRaises(api.ExecutionsError):
            api.fetch_executions(self.session, "20261026", observer=observed.append)
        self.assertEqual([event["status"] for event in observed], ["success", "broker_error"])
        self.assertEqual(observed[-1]["response_body"], {"return_code": 9})
        self.queue.side_effect = None
        self.queue.return_value = self.response({"return_code": 0})
        with self.assertRaises(api.ExecutionsError):
            api.fetch_executions(self.session, "20261026", observer=observed.append)
        self.assertEqual(observed[-1]["status"], "invalid_response")

    def test_http_failure_observation_keeps_status_without_raw_error(self):
        observed = []
        self.queue.side_effect = urllib.error.HTTPError("https://host/" + self.session.token, 429, self.session.token, {}, None)
        with self.assertRaises(api.ExecutionsError):
            api.fetch_executions(self.session, "20261026", observer=observed.append)
        self.assertEqual(observed[0]["status"], "transport_error")
        self.assertEqual(observed[0]["http_status"], 429)
        self.assertEqual(observed[0]["response_body"], {})
        self.assertNotIn(self.session.token, json.dumps(observed))

    def test_observer_mutation_does_not_change_typed_result_or_request(self):
        self.queue.return_value = self.response({"return_code": 0, "acnt_ord_cntr_prst_array": [fill_row()]})
        def mutate(event):
            event["response_body"]["acnt_ord_cntr_prst_array"][0]["cntr_qty"] = "9999"
            event["request_body"]["ord_dt"] = "20000101"
        result = api.fetch_executions(self.session, "20261026", observer=mutate)
        self.assertEqual(result[0].quantity, 2)
        self.assertEqual(json.loads(self.queue.call_args.args[1].data)["ord_dt"], "20261026")

    def test_signed_reported_profit_is_preserved_separately_from_arithmetic(self):
        row = {"stk_cd": "A005930", "crd_tp": "현금잔고", "cntr_qty": "1", "cntr_pric": "100", "buy_uv": "100",
               "tdy_trde_cmsn": "5", "tdy_trde_tax": "2", "tdy_sel_pl": "-6.50", "pl_rt": "-6.50", "tdy_sel_pl_1": "-7.00"}
        self.queue.return_value = self.response({"return_code": 0, "dt_stk_div_rlzt_pl": [row]})
        observed = []
        result = api.fetch_realized_profit(self.session, "005930", "20261026", observer=observed.append)[0]
        self.assertEqual(result.reported_net, Decimal("-6.50"))
        self.assertEqual(result.reported_rate, Decimal("-6.50"))
        self.assertEqual(result.reported_net_secondary, Decimal("-7.00"))
        self.assertEqual(observed[0]["response_body"]["dt_stk_div_rlzt_pl"][0]["tdy_sel_pl"], "-6.50")
        self.assertNotEqual(result.reported_net, result.price - result.buy_price - result.commission - result.tax)

    def test_missing_reported_profit_is_unknown_and_malformed_signed_value_fails(self):
        row = {"stk_cd": "A005930", "crd_tp": "현금잔고", "cntr_qty": "1", "cntr_pric": "100", "buy_uv": "80",
               "tdy_trde_cmsn": "0", "tdy_trde_tax": "0"}
        self.queue.return_value = self.response({"return_code": 0, "dt_stk_div_rlzt_pl": [row]})
        self.assertIsNone(api.fetch_realized_profit(self.session, "005930", "20261026")[0].reported_net)
        for invalid in ("NaN", "Infinity", "1e3", True):
            self.queue.return_value = self.response({"return_code": 0, "dt_stk_div_rlzt_pl": [{**row, "tdy_sel_pl": invalid}]})
            with self.subTest(invalid=invalid), self.assertRaises(api.ExecutionsError):
                api.fetch_realized_profit(self.session, "005930", "20261026")

    def test_daily_account_totals_preserve_reported_net_discrepancy(self):
        self.queue.return_value = self.response({"return_code": 0, "tot_buy_amt": "987350", "tot_sell_amt": "999200",
            "rlzt_pl": "2916", "trde_cmsn": "6950", "trde_tax": "1994", "dt_rlzt_pl": [{"dt": "20261026",
            "buy_amt": "987350", "sell_amt": "999200", "tdy_sel_pl": "2916", "tdy_trde_cmsn": "6950", "tdy_trde_tax": "1994"}]})
        observed = []
        result = api.fetch_daily_realized_profit(self.session, "20261026", observer=observed.append)
        self.assertTrue(result.totals_consistent)
        top = result.page_totals[0]
        self.assertEqual(top.reported_net, Decimal("2916"))
        self.assertEqual(top.sell_amount - top.buy_amount - top.commission - top.tax, Decimal("2906"))
        self.assertEqual(result.rows[0], top)
        self.assertEqual(observed[0]["source_api"], "ka10074")
        self.assertEqual(json.loads(self.queue.call_args.args[1].data), {"strt_dt": "20261026", "end_dt": "20261026"})

    def test_daily_page_totals_are_not_summed_and_conflicts_are_retained(self):
        self.queue.side_effect = [
            self.response({"return_code": 0, "rlzt_pl": "100", "dt_rlzt_pl": []}, {"cont-yn": "Y", "next-key": "next"}),
            self.response({"return_code": 0, "rlzt_pl": "110", "dt_rlzt_pl": []})]
        result = api.fetch_daily_realized_profit(self.session, "20261026")
        self.assertFalse(result.totals_consistent)
        self.assertEqual([row.reported_net for row in result.page_totals], [Decimal(100), Decimal(110)])
        self.assertIsNone(result.page_totals[0].commission)
        self.assertEqual(result.rows, ())

    def test_daily_wrong_date_and_observer_failure_do_not_return_a_report(self):
        self.queue.return_value = self.response({"return_code": 0, "dt_rlzt_pl": [{"dt": "20261025"}]})
        with self.assertRaises(api.ExecutionsError):
            api.fetch_daily_realized_profit(self.session, "20261026")
        self.queue.return_value = self.response({"return_code": 0, "dt_rlzt_pl": []})
        with self.assertRaises(api.BrokerObservationError):
            api.fetch_daily_realized_profit(self.session, "20261026", observer=Mock(side_effect=OSError("private")))


if __name__ == "__main__":
    unittest.main()
