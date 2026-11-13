from datetime import datetime
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock

from kiwoom_autotrader.trading.cutoff_reconciliation import classify_cost_cutoff
from kiwoom_autotrader.trading.execution_log import ExecutionTracker
from kiwoom_autotrader.api.executions import Execution, RealizedProfit, _profit_rows
from kiwoom_autotrader.api.orders import OrderReceipt
from kiwoom_autotrader.api.session import KST


class CostQueryCutoffTests(unittest.TestCase):
    def setUp(self):
        self.page={'source_api':'ka10072','mode':'paper','day':'20261028','query_id':'q1','page':1,
                   'request_started_at':'2026-10-28T12:49:55.531135+09:00',
                   'observed_at':'2026-10-28T12:49:55.584941+09:00', 'status':'success','http_status':200,
                   'request_body':{'stk_cd':'072950','strt_dt':'20261028'},'response_body':{'return_code':0},
                   'continuation':{'request_cont_yn':'N','response_cont_yn':'N'}}
        self.orders=[self.order('1',340,5068090,'124954','2026-10-28T12:49:54.9+09:00'),
                     self.order('2',63,985350,'125118','2026-10-28T12:51:19+09:00')]

    @staticmethod
    def order(number, qty, amount, broker_time, observed_at):
        return {'mode':'paper','symbol':'072950','side':'sell','order_number':number,
                'ordered_quantity':qty,'verified_quantity':qty,'rest_projection_quantity':qty,
                'verified_amount':str(amount),'verified_amount_source':'00_unit_sum_reconciled_to_cumulative_quantity',
                'unit_coverage':'quantity_reconciled','stream_gap_observed':False,'issues':[],
                'unit_executions':[{'broker_time':broker_time,'quantity':qty,'amount':str(amount),
                                    'reported_cost_observations':[{'observed_at':observed_at}]}]}

    def run_check(self):
        return classify_cost_cutoff('20261028','072950',self.orders,[self.page],broker_quantity=340,broker_amount='5068090')

    def test_post_order_and_pre_subset_match(self):
        r=self.run_check();self.assertEqual(r['status'],'post_cost_query_fills_present')
        self.assertEqual((r['pre_quantity'],r['pre_unit_amount'],r['post_quantity'],r['post_unit_amount']),
                         (340,'5068090',63,'985350'))
        self.assertTrue(r['pre_quantity_matches_broker']);self.assertTrue(r['pre_amount_matches_broker'])
        self.assertEqual(r['net_profit_status'],'unconfirmed')

    def test_late_arrival_with_earlier_trade_is_ambiguous(self):
        self.orders[1]['unit_executions'][0]['broker_time']='124954'
        r=self.run_check();self.assertEqual(r['status'],'unconfirmed');self.assertIn('2',r['ambiguous_order_ids'])

    def test_execution_same_second_as_query_is_ambiguous(self):
        self.orders[1]['unit_executions'][0]['broker_time']='124955'
        r=self.run_check();self.assertEqual(r['status'],'unconfirmed');self.assertIn('2',r['ambiguous_order_ids'])

    def test_stream_gap_and_partial_pre_order_fail_closed(self):
        self.orders[0]['stream_gap_observed']=True
        self.assertEqual(self.run_check()['status'],'unconfirmed')
        self.orders[0]['stream_gap_observed']=False;self.orders[0]['ordered_quantity']=341
        self.assertEqual(self.run_check()['status'],'unconfirmed')

    def test_post_partial_can_be_evidence_but_not_fabricated_completion(self):
        self.orders[1]['ordered_quantity']=100
        r=self.run_check();self.assertEqual(r['status'],'post_cost_query_fills_present')
        self.assertEqual(r['post_quantity'],63);self.assertEqual(r['net_profit_status'],'unconfirmed')

    def test_query_missing_or_incomplete_rejected(self):
        self.page['continuation']['response_cont_yn']='Y'; self.assertEqual(self.run_check()['status'],'unconfirmed')
        self.page['continuation']['response_cont_yn']='N';self.page['request_body']['stk_cd']='003220'
        self.assertEqual(self.run_check()['status'],'unconfirmed')

    def test_differing_pre_amount_is_not_matched(self):
        r=classify_cost_cutoff('20261028','072950',self.orders,[self.page],broker_quantity=340,broker_amount='5068089')
        self.assertEqual(r['status'],'post_cost_query_fills_present');self.assertFalse(r['pre_amount_matches_broker'])
        self.assertEqual(r['reason'],'pre_query_scope_or_totals_do_not_match')

    def test_nonfinite_values_and_naive_times_are_unconfirmed(self):
        self.page['observed_at']='2026-10-28T12:49:55'
        self.assertEqual(self.run_check()['status'],'unconfirmed')
        self.page['observed_at']='2026-10-28T12:49:55.584941+09:00'
        self.orders[1]['unit_executions'][0]['amount']='NaN'
        self.assertEqual(self.run_check()['status'],'unconfirmed')


class CostCutoffIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.now=datetime(2026,10,28,12,45,0,tzinfo=KST)
        self.tracker=ExecutionTracker(log_dir=Path(self.temp.name),now=lambda:self.now,
            account_fetcher=Mock(),executions_fetcher=Mock(),profit_fetcher=Mock())
        self.tracker.register_order(OrderReceipt('paper','sell','072950','2',None,'24','https://mockapi.kiwoom.com'),'ddcrs')
        self.unit('24','1','2','124505','2026-10-28T12:45:05+09:00')
        self.raw={'stk_cd':'072950','crd_tp':'현금잔고','cntr_qty':'2','cntr_pric':'1000','buy_uv':'900',
                  'tdy_trde_cmsn':'10','tdy_trde_tax':'4','tdy_sel_pl':'186'}
        self.tracker.record_broker_observation({'schema':'kiwoom-broker-page-v1','source_api':'ka10072',
            'source_kind':'broker_observation','mode':'paper','day':'20261028','query_id':'a'*32,
            'request_started_at':'2026-10-28T12:49:55.531135+09:00','observed_at':'2026-10-28T12:49:55.584941+09:00',
            'page':1,'request_body':{'strt_dt':'20261028','stk_cd':'072950'},
            'response_body':{'return_code':0,'dt_stk_div_rlzt_pl':[self.raw]},
            'continuation':{'request_cont_yn':'N','response_cont_yn':'N','next_key_present':False},'status':'success','http_status':200})
        self.now=datetime(2026,10,28,12,51,0,tzinfo=KST)
        self.tracker.register_order(OrderReceipt('paper','sell','072950','3',None,'25','https://mockapi.kiwoom.com'),'ddcrs')
        self.unit('25','2','3','125105','2026-10-28T12:51:05+09:00')
        self.now=datetime(2026,10,28,12,52,0,tzinfo=KST)
        self.tracker._ingest('paper','20261028',(
            Execution('20261028','24','24','072950','fixture','sell','12:45:05',Decimal(1000),2),
            Execution('20261028','25','25','072950','fixture','sell','12:51:05',Decimal(1000),3)))

    def unit(self,order,fill,quantity,broker_time,observed_at):
        values={'9203':order,'9001':'072950','907':'1','913':'체결','908':broker_time,'909':fill,
                '914':'1000','915':quantity,'911':quantity,'903':str(int(quantity)*1000)}
        self.tracker.record_broker_observation({'schema_version':1,'source_api':'00','source_kind':'broker_observation',
            'observed_at':observed_at,'day':'20261028','mode':'paper','event_kind':'execution_stream','values':values})

    def result(self,rows):
        self.tracker._profit('paper','20261028','072950',rows)
        with sqlite3.connect(self.tracker.path) as db:
            net,reason=db.execute('SELECT net,reason FROM profits WHERE symbol=?',('072950',)).fetchone()
            evidence=json.loads(db.execute('SELECT payload_json FROM profit_observations ORDER BY rowid DESC LIMIT 1').fetchone()[0])
        return net,reason,evidence['amount_reconciliation']

    def test_profit_records_matching_query_cutoff_without_confirming_net(self):
        net,reason,comparison=self.result(_profit_rows([self.raw],'072950'))
        self.assertIsNone(net);self.assertIn('post_cost_query_fills_present',reason)
        result=comparison['cost_query_cutoff'];self.assertEqual(result['pre_quantity'],2)
        self.assertEqual(result['pre_unit_amount'],'2000');self.assertEqual(result['post_quantity'],3)
        self.assertTrue(result['pre_amount_matches_broker'])

    def test_unrelated_cost_rows_never_reuse_query_as_cutoff_evidence(self):
        alternate=(RealizedProfit('072950',2,Decimal(1000),Decimal(901),Decimal(10),Decimal(4),reported_net=Decimal(184)),)
        net,reason,comparison=self.result(alternate)
        self.assertIsNone(net);self.assertNotIn('post_cost_query_fills_present',reason)
        self.assertNotIn('cost_query_cutoff',comparison)


if __name__=='__main__': unittest.main()
