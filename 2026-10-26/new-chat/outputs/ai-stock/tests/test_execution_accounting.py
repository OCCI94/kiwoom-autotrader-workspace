"""Offline evidence-only accounting: units, cumulative totals and TR conflicts."""
from datetime import datetime, timedelta
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock

from kiwoom_autotrader.api.executions import Execution, RealizedProfit
from kiwoom_autotrader.api.orders import OrderReceipt
from kiwoom_autotrader.api.session import KST
from kiwoom_autotrader.trading.execution_log import ExecutionTracker


class ExecutionAccountingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.now = datetime(2026, 10, 26, 10, 0, tzinfo=KST)
        self.tracker = ExecutionTracker(log_dir=self.root, now=lambda:self.now,
            account_fetcher=Mock(), executions_fetcher=Mock(), profit_fetcher=Mock())
        self.tracker.register_order(OrderReceipt("paper", "sell", "097520", "49", None, "24", "https://mockapi.kiwoom.com"), "ddcrs")
        self.tracker._ingest("paper", "20261026", (Execution("20261026", "24", "24", "097520", "fixture", "sell", "10:00:10", Decimal(20392), 49),))

    def view(self):
        return json.loads((self.root / "20261026.execution-accounting.json").read_text())

    def unit(self, unit_id, size, cumulative, price, amount, fee, *, updates=None):
        self.now += timedelta(milliseconds=1)
        values = {"9203":"0000024", "9001":"097520", "907":"1", "913":"체결", "908":"100010",
            "909":str(unit_id), "914":str(price), "915":str(size), "911":str(cumulative), "903":str(amount),
            "938":str(fee), "939":"1994"}
        values.update(updates or {})
        self.tracker.record_broker_observation({"schema_version":1, "source_api":"00", "source_kind":"broker_observation",
            "observed_at":self.now.isoformat(), "day":"20261026", "mode":"paper", "event_kind":"execution_stream", "values":values})

    def cost(self, api, rows, *, query="a"*32, page=1, continuation="N", top=None, status="success"):
        row_name = {"ka10072":"dt_stk_div_rlzt_pl", "ka10073":"dt_stk_rlzt_pl", "ka10074":"dt_rlzt_pl"}[api]
        request = {"strt_dt":"20261026"}
        if api != "ka10074": request["stk_cd"] = "097520"
        if api != "ka10072": request["end_dt"] = "20261026"
        self.tracker.record_broker_observation({"schema":"kiwoom-broker-page-v1", "source_api":api,
            "source_kind":"broker_observation", "mode":"paper", "day":"20261026", "query_id":query,
            "request_started_at":self.now.isoformat(), "observed_at":self.now.isoformat(), "page":page,
            "request_body":request, "response_body":{"return_code":0 if status=="success" else 9, row_name:rows, **(top or {})},
            "continuation":{"request_cont_yn":"N" if page==1 else "Y", "response_cont_yn":continuation, "next_key_present":continuation=="Y"},
            "status":status, "http_status":200})

    def test_exact_units_distinguish_eight_won_rounding_and_preserve_unscoped_fee_reports(self):
        total_qty = total_amount = 0
        for unit_id, size, price, fee in [(1,7,20400,980),(2,5,20350,1680),(3,3,20350,2100),(4,12,20400,3790),(5,22,20400,6910)]:
            total_qty += size; total_amount += size * price
            self.unit(unit_id, size, total_qty, price, total_amount, fee)
        order = self.view()["orders"][0]
        self.assertEqual((order["verified_amount"], order["rest_reference_amount"], order["rest_reference_minus_verified"]), ("999200", "999208", "8"))
        self.assertEqual([row["quantity"] for row in order["unit_executions"]], [7,5,3,12,22])
        self.assertEqual(order["latest_reported_cost"]["commission"], "6910")
        self.assertNotIn("commission",order["latest_cumulative"])
        self.assertEqual(order["unit_coverage"], "quantity_reconciled")
        self.assertEqual(order["avg_price_reconciliation_status"], "reconciled_with_unit_rounding")
        self.assertEqual(order["avg_price_rounding_diff_krw"], "8")
        self.assertEqual(order["cost_status"], "aggregation_scope_unverified")
        with sqlite3.connect(self.tracker.path) as db:
            self.assertEqual(db.execute("SELECT price,quantity FROM fills").fetchall(), [("20392",49)])
            self.assertEqual(db.execute("SELECT COUNT(*) FROM broker_observations").fetchone()[0], 5)
        sidecar = self.root / "20261026.execution-accounting.json"
        self.assertEqual(sidecar.stat().st_mode & 0o777, 0o600)
        self.assertIsNone(self.view()["final_realized_net"])

    def test_rounding_profit_requires_complete_units_same_scope_and_matching_profit_amount(self):
        # 25*20400 + 24*20383 = 999192, while REST's whole-won
        # average 20392 multiplied by 49 is 999208 (16 won difference).
        self.unit(1,25,25,20400,510000,1000)
        self.unit(2,24,49,20383,999192,2000)
        rows=(RealizedProfit("097520",25,Decimal(20400),Decimal(20150),Decimal(1000),Decimal(500),reported_net=Decimal(4750)),
              RealizedProfit("097520",24,Decimal(20383),Decimal(20150),Decimal(1000),Decimal(500),reported_net=Decimal(4092)))
        self.tracker._profit("paper","20261026","097520",rows)
        with sqlite3.connect(self.tracker.path) as db:
            net,reason=db.execute("SELECT net,reason FROM profits").fetchone()
            evidence=json.loads(db.execute("SELECT payload_json FROM profit_observations ORDER BY rowid DESC LIMIT 1").fetchone()[0])
        self.assertEqual(net,"8842")
        self.assertIn("다른 손익 TR 비용 대사",reason)
        self.assertEqual(evidence["amount_reconciliation"]["unit_execution_gross"],"999192")
        self.assertEqual(evidence["amount_reconciliation"]["avg_price_rounding_diff_krw"],"16")
        self.assertEqual(evidence["amount_reconciliation"]["reconciliation_status"],"reconciled_with_unit_rounding")
        self.assertEqual(evidence["attribution_status"],"reconciled_with_unit_rounding")

        bad_cost=(rows[0],RealizedProfit("097520",24,Decimal(20383),Decimal(20150),Decimal(1000),Decimal(500),reported_net=Decimal(4093)))
        self.tracker._profit("paper","20261026","097520",bad_cost)
        with sqlite3.connect(self.tracker.path) as db:
            net,reason=db.execute("SELECT net,reason FROM profits").fetchone()
        self.assertIsNone(net)
        self.assertIn("보고손익",reason)

        # A one-won disagreement with the profit TR still cannot be promoted
        # from the quantity bound alone: identity and exact money are unknown.
        bad=(rows[0],RealizedProfit("097520",24,Decimal(20384),Decimal(20150),Decimal(1000),Decimal(500),reported_net=Decimal(4116)))
        self.tracker._profit("paper","20261026","097520",bad)
        with sqlite3.connect(self.tracker.path) as db:
            self.assertIsNone(db.execute("SELECT net FROM profits").fetchone()[0])

    def test_rounding_profit_rejects_missing_units_and_conflicting_reported_net(self):
        self.unit(2,24,49,20383,999192,2000)
        rows=(RealizedProfit("097520",25,Decimal(20400),Decimal(20150),Decimal(1000),Decimal(500),reported_net=Decimal(4750)),
              RealizedProfit("097520",24,Decimal(20383),Decimal(20150),Decimal(1000),Decimal(500),reported_net=Decimal(4092)))
        self.tracker._profit("paper","20261026","097520",rows)
        with sqlite3.connect(self.tracker.path) as db:
            self.assertIsNone(db.execute("SELECT net FROM profits").fetchone()[0])
        self.unit(1,25,25,20400,510000,1000)
        conflict=(rows[0],RealizedProfit("097520",24,Decimal(20383),Decimal(20150),Decimal(1000),Decimal(500),reported_net=Decimal(4093)))
        self.tracker._profit("paper","20261026","097520",conflict)
        with sqlite3.connect(self.tracker.path) as db:
            self.assertIsNone(db.execute("SELECT net FROM profits").fetchone()[0])

    def test_separate_broker_period_cost_conflict_blocks_profit_attribution(self):
        self.unit(1,49,49,20392,999208,100)
        rows=(RealizedProfit("097520",49,Decimal(20392),Decimal(20150),Decimal(100),Decimal(50),reported_net=Decimal(11708)),)
        period={"stk_cd":"097520", "dt":"20261026", "crd_tp":"현금잔고", "cntr_qty":"49",
                "buy_uv":"20150", "cntr_pric":"20392", "tdy_trde_cmsn":"100", "tdy_trde_tax":"50", "tdy_sel_pl":"11708"}
        self.cost("ka10073",[period],query="1"*32)
        self.tracker._profit("paper","20261026","097520",rows)
        with sqlite3.connect(self.tracker.path) as db:
            self.assertEqual(db.execute("SELECT net FROM profits").fetchone()[0],"11708")
        self.cost("ka10073",[{**period,"tdy_trde_cmsn":"101","tdy_sel_pl":"11707"}],query="2"*32)
        self.tracker._profit("paper","20261026","097520",rows)
        with sqlite3.connect(self.tracker.path) as db:
            net,reason=db.execute("SELECT net,reason FROM profits").fetchone()
            evidence=json.loads(db.execute("SELECT payload_json FROM profit_observations ORDER BY rowid DESC LIMIT 1").fetchone()[0])
        self.assertIsNone(net)
        self.assertIn("불일치",reason)
        self.assertTrue(evidence["amount_reconciliation"]["period_cost_comparison"]["scope_matches"])
        self.cost("ka10073",[{**period,"cntr_qty":"48"}],query="3"*32)
        self.tracker._profit("paper","20261026","097520",rows)
        with sqlite3.connect(self.tracker.path) as db:
            net,reason=db.execute("SELECT net,reason FROM profits").fetchone()
        self.assertIsNone(net)
        self.assertIn("범위",reason)

    def test_sub_won_hypothesis_is_recorded_but_not_confirmed(self):
        self.unit(1,49,49,20392,999208,100)
        # Displayed purchase price produces a 0.49-won mismatch. It does not
        # prove the broker's rounding rule, nor a confirmed economic result.
        rows=(RealizedProfit("097520",49,Decimal(20392),Decimal("20150.01"),
                            Decimal(100),Decimal(50),reported_net=Decimal("11707")),)
        self.tracker._profit("paper","20261026","097520",rows)
        with sqlite3.connect(self.tracker.path) as db:
            self.assertIsNone(db.execute("SELECT net FROM profits").fetchone()[0])
            evidence=json.loads(db.execute("SELECT payload_json FROM profit_observations ORDER BY rowid DESC LIMIT 1").fetchone()[0])
        hypothesis=evidence["amount_reconciliation"]["sub_won_basis_rounding"]
        self.assertEqual(hypothesis["status"],"possible_sub_won_basis_rounding")
        self.assertEqual(hypothesis["reported_minus_calculated"],"-0.51")
        self.assertFalse(hypothesis["cause_confirmed"])
        self.assertFalse(hypothesis["net_confirmed_by_this_test"])

        # A discrepancy of one won or more is not in this candidate class.
        bad=(RealizedProfit("097520",49,Decimal(20392),Decimal("20150.01"),
                           Decimal(100),Decimal(50),reported_net=Decimal("11706")),)
        self.tracker._profit("paper","20261026","097520",bad)
        with sqlite3.connect(self.tracker.path) as db:
            evidence=json.loads(db.execute("SELECT payload_json FROM profit_observations ORDER BY rowid DESC LIMIT 1").fetchone()[0])
        self.assertEqual(evidence["amount_reconciliation"]["sub_won_basis_rounding"]["status"],"not_established")

    def test_row_fee_difference_is_candidate_not_final_reconciliation(self):
        self.unit(1,49,49,20392,999208,100)
        rows=(RealizedProfit("097520",49,Decimal(20392),Decimal(20150),Decimal(100),Decimal(50),reported_net=Decimal(11708)),)
        period={"stk_cd":"097520","dt":"20261026","crd_tp":"현금잔고","cntr_qty":"49",
                "buy_uv":"20150","cntr_pric":"20392","tdy_trde_cmsn":"110","tdy_trde_tax":"50","tdy_sel_pl":"11698"}
        self.cost("ka10073",[period],query="1"*32)
        self.tracker._profit("paper","20261026","097520",rows)
        with sqlite3.connect(self.tracker.path) as db:
            self.assertIsNone(db.execute("SELECT net FROM profits").fetchone()[0])
            evidence=json.loads(db.execute("SELECT payload_json FROM profit_observations ORDER BY rowid DESC LIMIT 1").fetchone()[0])
        candidate=evidence["amount_reconciliation"]["period_cost_comparison"]["row_fee_rounding"]
        self.assertEqual(candidate["status"],"possible_row_fee_rounding")
        self.assertEqual((candidate["ka10072_commission"],candidate["ka10073_commission"]),("100","110"))
        self.assertFalse(candidate["cause_confirmed"])
        self.assertFalse(candidate["net_confirmed_by_this_test"])

        # Larger-than-bound differences and tax changes are not candidates.
        self.cost("ka10073",[{**period,"tdy_trde_cmsn":"120","tdy_sel_pl":"11688"}],query="2"*32)
        self.tracker._profit("paper","20261026","097520",rows)
        with sqlite3.connect(self.tracker.path) as db:
            evidence=json.loads(db.execute("SELECT payload_json FROM profit_observations ORDER BY rowid DESC LIMIT 1").fetchone()[0])
        self.assertEqual(evidence["amount_reconciliation"]["period_cost_comparison"]["row_fee_rounding"]["status"],"not_established")

    def test_conservative_range_requires_complete_later_broker_evidence(self):
        self.tracker._profit_source_kind = 'broker_observation'
        self.unit(1,49,49,20392,999208,100)
        self.now += timedelta(seconds=1)
        base={"stk_cd":"097520","crd_tp":"현금잔고","cntr_qty":"49", "buy_uv":"20150", "cntr_pric":"20392", "tdy_trde_cmsn":"100", "tdy_trde_tax":"50", "tdy_sel_pl":"11708"}
        self.cost('ka10072',[base],query='a'*32)
        self.cost('ka10073',[{**base,'dt':'20261026','tdy_trde_cmsn':'110','tdy_sel_pl':'11698'}],query='b'*32)
        rows=(RealizedProfit('097520',49,Decimal(20392),Decimal(20150),Decimal(100),Decimal(50),reported_net=Decimal(11708)),)
        self.tracker._profit('paper','20261026','097520',rows)
        with sqlite3.connect(self.tracker.path) as db:
            self.assertIsNone(db.execute('SELECT net FROM profits').fetchone()[0])
            doc=json.loads(db.execute('SELECT payload_json FROM profit_observations ORDER BY rowid DESC LIMIT 1').fetchone()[0])
        result=doc['amount_reconciliation']['conservative_estimate']
        self.assertEqual((result['min_net'],result['max_net'],result['width_krw']),('11698','11708','10'))
        self.assertFalse(result['rounding_cause_confirmed'])
        self.assertFalse(result['final_net_confirmed'])

        # Later observed fill makes the older reports unusable as bounds.
        self.unit(1,49,49,20392,999208,100)
        self.tracker._profit('paper','20261026','097520',rows)
        with sqlite3.connect(self.tracker.path) as db:
            doc=json.loads(db.execute('SELECT payload_json FROM profit_observations ORDER BY rowid DESC LIMIT 1').fetchone()[0])
        self.assertNotIn('conservative_estimate',doc['amount_reconciliation'])

    def test_conservative_range_sub_won_needs_two_complete_matching_queries(self):
        self.tracker._profit_source_kind = 'broker_observation'
        self.unit(1,49,49,20392,999208,100)
        self.now += timedelta(seconds=1)
        row={"stk_cd":"097520","crd_tp":"현금잔고","cntr_qty":"49", "buy_uv":"20150.01", "cntr_pric":"20392", "tdy_trde_cmsn":"100", "tdy_trde_tax":"50", "tdy_sel_pl":"11707"}
        self.cost('ka10072',[row],query='c'*32)
        self.cost('ka10073',[{**row,'dt':'20261026'}],query='d'*32)
        rows=(RealizedProfit('097520',49,Decimal(20392),Decimal('20150.01'),Decimal(100),Decimal(50),reported_net=Decimal(11707)),)
        self.tracker._profit('paper','20261026','097520',rows)
        with sqlite3.connect(self.tracker.path) as db:
            doc=json.loads(db.execute('SELECT payload_json FROM profit_observations ORDER BY rowid DESC LIMIT 1').fetchone()[0])
        result=doc['amount_reconciliation']['conservative_estimate']
        self.assertEqual(result['reason'],'possible_sub_won_basis_rounding')
        self.assertEqual(result['min_net'],'11707')
        self.assertFalse(result['final_net_confirmed'])

    def test_stale_or_pending_period_cannot_publish_previous_conservative_range(self):
        self.tracker._profit_source_kind = 'broker_observation'
        self.unit(1,49,49,20392,999208,100)
        self.now += timedelta(seconds=1)
        row={"stk_cd":"097520","crd_tp":"현금잔고","cntr_qty":"49", "buy_uv":"20150", "cntr_pric":"20392", "tdy_trde_cmsn":"100", "tdy_trde_tax":"50", "tdy_sel_pl":"11708"}
        self.cost('ka10072',[row],query='a'*32)
        self.cost('ka10073',[{**row,'dt':'20261026','tdy_trde_cmsn':'110','tdy_sel_pl':'11698'}],query='b'*32)
        rows=(RealizedProfit('097520',49,Decimal(20392),Decimal(20150),Decimal(100),Decimal(50),reported_net=Decimal(11708)),)
        self.tracker._profit('paper','20261026','097520',rows)
        with sqlite3.connect(self.tracker.path) as db:
            doc=json.loads(db.execute('SELECT payload_json FROM profit_observations ORDER BY rowid DESC LIMIT 1').fetchone()[0])
        self.assertIn('conservative_estimate',doc['amount_reconciliation'])
        for kw in ({'await_period':True}, {'sell_watermark':0}):
            self.tracker._profit('paper','20261026','097520',rows,**kw)
            with sqlite3.connect(self.tracker.path) as db:
                doc=json.loads(db.execute('SELECT payload_json FROM profit_observations ORDER BY rowid DESC LIMIT 1').fetchone()[0])
                self.assertIsNone(db.execute('SELECT net FROM profits').fetchone()[0])
            self.assertNotIn('conservative_estimate',doc['amount_reconciliation'])
            self.assertEqual(doc['amount_reconciliation']['cross_source_projection_status'],'stale_or_period_pending')

    def test_conservative_range_supports_independently_bounded_combined_disagreements(self):
        self.tracker._profit_source_kind = 'broker_observation'
        self.unit(1,49,49,20392,999208,100)
        self.now += timedelta(seconds=1)
        row={"stk_cd":"097520","crd_tp":"현금잔고","cntr_qty":"49", "buy_uv":"20150.01", "cntr_pric":"20392", "tdy_trde_cmsn":"100", "tdy_trde_tax":"50", "tdy_sel_pl":"11707"}
        self.cost('ka10072',[row],query='e'*32)
        period={**row,'dt':'20261026','tdy_trde_cmsn':'110','tdy_sel_pl':'11697'}
        self.cost('ka10073',[period],query='f'*32)
        rows=(RealizedProfit('097520',49,Decimal(20392),Decimal('20150.01'),Decimal(100),Decimal(50),reported_net=Decimal(11707)),)
        self.tracker._profit('paper','20261026','097520',rows)
        with sqlite3.connect(self.tracker.path) as db:
            doc=json.loads(db.execute('SELECT payload_json FROM profit_observations ORDER BY rowid DESC LIMIT 1').fetchone()[0])
        result=doc['amount_reconciliation']['conservative_estimate']
        self.assertEqual((result['min_net'],result['max_net'],result['width_krw']),('11697','11707','10'))
        self.assertEqual(result['reason'],'possible_row_fee_and_sub_won_rounding')
        self.assertIsNone(doc['net'])
        # A tax or basis change cannot be explained by either tolerated difference.
        self.cost('ka10073',[{**period,'tdy_trde_tax':'51'}],query='0'*32)
        self.tracker._profit('paper','20261026','097520',rows)
        with sqlite3.connect(self.tracker.path) as db:
            doc=json.loads(db.execute('SELECT payload_json FROM profit_observations ORDER BY rowid DESC LIMIT 1').fetchone()[0])
        self.assertNotIn('conservative_estimate',doc['amount_reconciliation'])

    def test_duplicate_unit_is_counted_once_and_conflicting_id_clears_verification(self):
        self.unit(1,49,49,20400,999600,6950)
        self.unit(1,49,49,20400,999600,6950)
        order = self.view()["orders"][0]
        self.assertEqual(order["observed_unit_quantity"],49)
        self.assertEqual(len(order["unit_executions"][0]["observation_ids"]),2)
        self.unit(1,49,49,20350,997150,6950)
        order = self.view()["orders"][0]
        self.assertIsNone(order["verified_amount"])
        self.assertIn("conflicting_unit_id",order["issues"])
        self.assertEqual(len(order["unit_executions"][0]["conflicting_observation_ids"]),1)

    def test_missing_units_can_preserve_reported_aggregate_without_inventing_partial_fills(self):
        self.unit(5,22,49,20400,999200,6910)
        order = self.view()["orders"][0]
        self.assertEqual(order["verified_amount"],"999200")
        self.assertEqual(order["verified_amount_source"],"00_reported_cumulative_amount_only")
        self.assertEqual((order["unit_coverage"],order["observed_unit_quantity"]),("unconfirmed",22))
        self.assertEqual(len(order["unit_executions"]),1)

    def test_distinct_full_quantity_unit_ids_are_contradictions_not_missing_coverage(self):
        self.unit(1,49,49,20300,994700,3480)
        self.unit(2,49,49,20300,994700,3480)
        order=self.view()["orders"][0]
        self.assertEqual(order["observed_unit_quantity"],98)
        self.assertIn("observed_unit_quantity_exceeds_order_quantity",order["issues"])
        self.assertIn("observed_unit_quantity_exceeds_cumulative_quantity",order["issues"])
        self.assertIsNone(order["verified_amount"])
        self.assertIsNone(order["verified_amount_source"])
        self.assertEqual(order["unit_coverage"],"unconfirmed")
        totals=self.view()["verified_totals"]["paper"]["sell"]
        self.assertIsNone(totals["fully_verified_amount"])
        self.assertIsNone(totals["verified_observed_amount"])
        self.assertEqual(totals["unconfirmed_order_count"],1)

    def test_unit_amount_exceeding_reported_scope_blocks_aggregate_fallback(self):
        self.unit(1,22,49,50000,999200,3480)
        order=self.view()["orders"][0]
        self.assertIn("unit_amount_exceeds_own_cumulative_amount",order["issues"])
        self.assertIn("observed_unit_amount_exceeds_cumulative_amount",order["issues"])
        self.assertIsNone(order["verified_amount"])

    def test_unit_quantity_exceeding_its_reported_cumulative_scope_is_not_a_gap(self):
        self.unit(1,20,10,20300,406000,710)
        order=self.view()["orders"][0]
        self.assertIn("unit_quantity_exceeds_own_cumulative_quantity",order["issues"])
        self.assertIsNone(order["verified_amount"])

    def test_invalid_oversized_unit_cannot_be_discarded_then_confirmed_by_aggregate(self):
        self.unit(1,50,49,20300,994700,3480)
        order=self.view()["orders"][0]
        self.assertIn("unit_quantity_exceeds_order_quantity",order["issues"])
        self.assertIsNone(order["verified_amount"])
        self.assertIsNone(self.view()["verified_totals"]["paper"]["sell"]["fully_verified_amount"])

    def test_missing_aggregate_and_unit_gap_never_use_rest_average_as_exact_amount(self):
        self.unit(5,22,49,20400,999200,6910,updates={"903":""})
        order = self.view()["orders"][0]
        self.assertIsNone(order["verified_amount"])
        self.assertEqual(order["rest_reference_amount"],"999208")

    def test_conflicting_amount_identity_or_later_quantity_regression_remain_unconfirmed(self):
        self.unit(1,49,49,20400,999600,6950)
        self.unit(1,49,49,20400,999200,6950)
        self.assertIsNone(self.view()["orders"][0]["verified_amount"])
        self.assertIn("conflicting_cumulative_amount",self.view()["orders"][0]["issues"])
        self.unit(2,1,1,20400,20400,70)
        self.assertIn("out_of_order_cumulative_quantity",self.view()["orders"][0]["issues"])
        self.unit(3,1,1,20400,20400,70,updates={"907":"2"})
        self.assertIn("order_identity_mismatch",self.view()["orders"][0]["issues"])

    def test_profit_rows_are_not_units_and_three_sources_retain_conflicting_values(self):
        rows=[]
        for size,price,fee,tax,net in [(7,20400,980,285,485),(5,20350,700,202,98),(3,20350,420,121,59),(12,20400,1690,489,821),(22,20400,3120,897,1483)]:
            rows.append({"stk_cd":"097520", "crd_tp":"현금잔고", "cntr_qty":str(size), "buy_uv":"20150", "cntr_pric":str(price),
                "tdy_trde_cmsn":str(fee), "tdy_trde_tax":str(tax), "tdy_sel_pl":str(net)})
        self.cost("ka10072", rows)
        period = [{**row,"dt":"20261026"} for row in rows]
        for index in (0,3):
            period[index]["tdy_trde_cmsn"]=str(int(period[index]["tdy_trde_cmsn"])+10)
            period[index]["tdy_sel_pl"]=str(int(period[index]["tdy_sel_pl"])-10)
        self.cost("ka10073", period)
        self.cost("ka10074", [], top={"tot_buy_amt":"987350","tot_sell_amt":"999200","rlzt_pl":"2916","trde_cmsn":"6950","trde_tax":"1994"})
        view=self.view(); reports={report["source_api"]:report for report in view["cost_reports"]}
        self.assertEqual([reports[api]["totals"]["reported_net"] for api in ("ka10072","ka10073","ka10074")],["2946","2926","2916"])
        self.assertEqual(reports["ka10074"]["totals"]["cashflow_after_reported_costs"],"2906")
        self.assertEqual(reports["ka10074"]["totals"]["reported_realized_net_minus_cashflow"],"10")
        self.assertIsNone(reports["ka10074"]["totals"]["calculated_net"])
        self.assertIsNone(reports["ka10074"]["arithmetic_matches"])
        self.assertEqual(reports["ka10074"]["realized_comparability"],"unverified")
        self.assertEqual(view["orders"][0]["unit_executions"],[])
        self.assertIsNone(view["orders"][0]["verified_amount"])
        self.assertEqual(view["comparisons"][0]["differences"]["commission"],{"ka10072":"6910","ka10073":"6930"})
        self.assertEqual(view["comparisons"][1]["scope"],"account_vs_selected_symbols_not_proven_equal")
        self.assertIsNone(view["final_realized_net"])

    def test_account_day_cashflow_is_not_a_realized_loss_when_position_remains_open(self):
        # Real observed ka10074 figures after a third purchase, with 49 shares
        # still held. A roughly one-million-won cash outflow is not a loss.
        self.tracker._holdings("paper", {"mode":"paper","exchange":"KRX","snapshot_changed":False,
            "holdings":[{"stk_cd":"A097520","rmnd_qty":"49","crd_tp":"00"}]}, self.now)
        self.cost("ka10074", [], top={"tot_buy_amt":"2974450","tot_sell_amt":"1995200",
            "trde_cmsn":"17390","trde_tax":"3982","rlzt_pl":"-2282"})
        report=self.view()["cost_reports"][0]
        self.assertEqual(report["totals"]["reported_net"],"-2282")
        self.assertEqual(report["totals"]["cashflow_after_reported_costs"],"-1000622")
        self.assertEqual(report["totals"]["reported_realized_net_minus_cashflow"],"998340")
        self.assertEqual(report["comparison_scope"],"account_cashflow_not_realized_cost_basis")
        self.assertEqual(report["realized_comparability"],"unverified")
        self.assertIsNone(report["totals"]["calculated_net"])
        self.assertIsNone(report["arithmetic_matches"])
        self.assertNotIn("reported_minus_calculated_net",report["totals"])
        self.assertFalse(report["final_net_confirmed"])

    def test_latest_failed_or_incomplete_query_does_not_reuse_earlier_success(self):
        row={"stk_cd":"097520","crd_tp":"현금잔고","cntr_qty":"1","buy_uv":"100","cntr_pric":"110","tdy_trde_cmsn":"1","tdy_trde_tax":"1","tdy_sel_pl":"8"}
        self.cost("ka10072",[row])
        self.assertTrue(self.view()["cost_reports"][0]["complete_query"])
        self.cost("ka10072",[row],query="b"*32,continuation="Y")
        self.assertFalse(self.view()["cost_reports"][0]["complete_query"])
        self.cost("ka10072",[],query="c"*32,status="broker_error")
        report=self.view()["cost_reports"][0]
        self.assertFalse(report["complete_query"])
        self.assertIsNone(report["totals"]["reported_net"])
        self.assertEqual(report["query_id"],"c"*32)

    def test_restart_rebuild_keeps_raw_observations_and_projection_unchanged(self):
        self.unit(1,49,49,20400,999600,6950)
        raw=(self.root/"20261026.broker-observations.jsonl").read_bytes()
        first=self.view()
        tracker=ExecutionTracker(log_dir=self.root, now=lambda:self.now,account_fetcher=Mock(),executions_fetcher=Mock(),profit_fetcher=Mock())
        self.assertIsNone(tracker.period_profit_fetcher)
        self.assertIsNone(tracker.daily_profit_fetcher)
        tracker._materialize_accounting("20261026")
        self.assertEqual(self.view(),first)
        self.assertEqual((self.root/"20261026.broker-observations.jsonl").read_bytes(),raw)

    def test_summary_shows_verified_partial_amount_without_claiming_whole_day_total(self):
        self.tracker._ingest("paper", "20261026", (Execution("20261026","24","24","097520","fixture","sell","10:00:10",Decimal(20392),27),))
        self.unit(1,27,27,20350,549450,1920)
        self.tracker.register_order(OrderReceipt("paper","sell","097520","2",None,"25","https://mockapi.kiwoom.com"),"ddcrs")
        self.tracker._materialize_accounting("20261026")
        totals=self.view()["verified_totals"]["paper"]["sell"]
        self.assertEqual(totals["verified_observed_amount"],"549450")
        self.assertEqual((totals["verified_order_count"],totals["unconfirmed_order_count"],totals["partially_filled_order_count"]),(1,1,1))
        self.assertIsNone(totals["fully_verified_amount"])
        notify=Mock()
        self.tracker._summary("paper","20261026",notify)
        message=notify.call_args.args[0]
        self.assertIn("매도 검증된 관측분: 549450원",message)
        self.assertIn("미확인 1건",message)
        self.assertIn("등록주문 전체금액 미확정",message)
        with sqlite3.connect(self.tracker.path) as db:
            saved=json.loads(db.execute("SELECT payload FROM summaries").fetchone()[0])
        self.assertEqual(saved["verified_totals"]["sell"],totals)

    def test_observed_143956_replay_tax_reports_are_not_order_total_or_unit_conflicts(self):
        # Field values reproduced from real read-only observations of order
        # 0143956, 2026-10-27; use an isolated fixture ledger, never the live DB.
        self.tracker._ingest("paper","20261026",(Execution("20261026","24","24","097520","fixture","sell","13:14:54",Decimal(20327),49),))
        rows=[(902935,10,10,20300,203000,710,405,"131423"),
              (903735,26,36,20325,731450,2560,1056,"131435"),
              (903736,4,40,20350,812850,2840,162,"131435"),
              (904247,9,49,20350,996000,3480,365,"131454")]
        for unit_id,size,cumulative,price,amount,fee,tax,moment in rows:
            self.unit(unit_id,size,cumulative,price,amount,fee,updates={"939":str(tax),"908":moment})
        order=self.view()["orders"][0]
        self.assertEqual((order["verified_amount"],order["rest_reference_amount"],order["rest_reference_minus_verified"]),("996000","996023","23"))
        self.assertEqual([row["tax"] for row in order["reported_cost_observations"]],["405","1056","162","365"])
        self.assertEqual(order["latest_reported_cost"]["tax"],"365")
        self.assertIsNone(order["order_total_tax"])
        self.assertIsNone(order["order_total_commission"])
        self.assertEqual([row["reported_cost_observations"][0]["values"]["938"] for row in order["unit_executions"]],["710","2560","2840","3480"])
        self.assertTrue(all(row["aggregation_scope"]=="unverified" for row in order["reported_cost_observations"]))
        # A fee/tax revision for the same price/quantity/unit identity is a new
        # cost observation, not a new execution or an execution-ID conflict.
        self.unit(904247,9,49,20350,996000,3490,updates={"939":"366","908":"131454"})
        order=self.view()["orders"][0]
        self.assertEqual(order["verified_amount"],"996000")
        self.assertNotIn("conflicting_unit_id",order["issues"])
        self.assertEqual(len(order["unit_executions"]),4)
        self.assertEqual(len(order["unit_executions"][-1]["reported_cost_observations"]),2)
        self.assertEqual(order["latest_reported_cost"]["commission"],"3490")


if __name__ == "__main__":
    unittest.main()
