"""S5 v4 + broker-zero v5 -> observed unit fill -> timed owned sell.

Only isolated SQLite and injected observations are used, never a provider.
"""
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest
import uuid

from kiwoom_autotrader.api.executions import Execution
from kiwoom_autotrader.api.orders import OrderReceipt
from kiwoom_autotrader.trading.execution_log import ExecutionTracker, ExecutionLogError
from kiwoom_autotrader.trading.multi_strategy_evidence import multi_strategy_evidence
from tests.test_s5_source_bound_evidence import evidence, fixture

S5='S5_steady_uptrend_vwap'


class S5LedgerRoundtripTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.now,self.bars,self.market=fixture()
        self.settings={'paper_multi_strategy_enabled':True,'max_positions_per_theme':0}
        self.tracker=ExecutionTracker(log_dir=Path(self.temp.name),now=lambda:self.now,
                                      settings_loader=lambda:self.settings)
        with self.tracker._db():pass
        self.event('stream_started',self.now-timedelta(seconds=10),{})

    def event(self,kind,at,values):
        self.tracker.record_broker_observation({'schema_version':1,'source_api':'00',
            'source_kind':'broker_observation','observed_at':at.isoformat(),'day':at.strftime('%Y%m%d'),
            'mode':'paper','event_kind':kind,'values':values})

    def zero_proof(self,qty=10):
        at=self.now-timedelta(seconds=3);query=uuid.uuid4().hex
        holding=self.tracker.record_strategy_holding_observation('003220',0,0,0,at,source_kind='broker_observation')
        anchor,sha,anchor_at=self.tracker.stream_anchor(at)
        self.tracker.record_broker_observation({'schema':'kiwoom-broker-page-v1','source_api':'ka10075',
            'source_kind':'broker_observation','mode':'paper','day':self.now.strftime('%Y%m%d'),
            'query_id':query,'request_started_at':(self.now-timedelta(seconds=2)).isoformat(),
            'observed_at':(self.now-timedelta(seconds=1)).isoformat(),'page':1,
            'request_body':{'all_stk_tp':'0','trde_tp':'0','stex_tp':'0'},
            'response_body':{'return_code':0,'oso':[]},'status':'success','http_status':200,
            'continuation':{'request_cont_yn':'N','response_cont_yn':'N','next_key_present':False}})
        return {'schema_version':5,'symbol':'003220','pre_order_held_qty':0,'pre_order_pur_amt_krw':0,
            'remaining_principal_krw':1000000,'order_quantity':qty,'limit_price_krw':self.market['current_price'],
            'observed_at':at.isoformat(),'holdings_source':'broker_observation','symbol_cap_krw':1000000,
            'per_buy_cap_krw':1000000,'multi_strategy_enabled':True,
            'symbol_ledger_sha256':self.tracker.symbol_order_watermark('paper','003220'),
            'holding_observation_id':holding,'integrated_pending_clear':True,'pending_query_id':query,
            'recorder_observations':0,'recorder_ingress_sequence':0,'engine_ingress_sequence':0,
            'recorder_gap_count':0,'reconciliation_completed_at':(self.now-timedelta(microseconds=1)).isoformat(),
            'stream_anchor_id':anchor,'stream_anchor_sha256':sha,'stream_anchor_observed_at':anchor_at}

    def buy(self,qty=10,minutes=60):
        proof=self.zero_proof(qty)
        return self.tracker.prepare_order('paper','buy','003220',qty,S5,price=self.market['current_price'],
            evidence=evidence(minutes),reconciliation_evidence=proof)

    def test_same_boundary_cannot_reenter_after_unsent_intent_and_tracker_restart(self):
        ident=self.buy()
        self.tracker.cancel_intent(ident,'not_sent')
        self.tracker=ExecutionTracker(log_dir=Path(self.temp.name),now=lambda:self.now,
                                      settings_loader=lambda:self.settings)
        self.now,self.bars,self.market=fixture(64)
        with self.assertRaisesRegex(ExecutionLogError,'s5_boundary_already_claimed'):
            self.buy(minutes=64)
        with self.tracker._db() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM intents').fetchone()[0],1)

    def test_next_boundary_has_independent_claim_and_all_v5_checks(self):
        ident=self.buy();self.tracker.cancel_intent(ident,'not_sent')
        self.now,self.bars,self.market=fixture(65)
        next_ident=self.buy(minutes=65)
        self.assertNotEqual(ident,next_ident)
        self.tracker.assert_multi_buy_dispatch('paper','003220',next_ident)

    def test_corrupt_previous_boundary_evidence_fails_closed(self):
        ident=self.buy();self.tracker.cancel_intent(ident,'not_sent')
        with self.tracker._db() as db:
            # Simulate offline tampering of this private temporary fixture.
            db.execute('DROP TRIGGER order_decisions_no_update')
            db.execute("UPDATE order_decisions SET evidence_sha256=? WHERE intent_id=?",('0'*64,ident))
            db.execute("CREATE TRIGGER order_decisions_no_update BEFORE UPDATE ON order_decisions "
                       "BEGIN SELECT RAISE(ABORT, 'order decisions are immutable'); END")
        self.now,self.bars,self.market=fixture(65)
        with self.assertRaisesRegex(ExecutionLogError,'무결성'):
            self.buy(minutes=65)

    def test_full_v4_and_v5_initial_dispatch_unit_fill_restart_and_time_stop_sell(self):
        ident=self.buy();self.tracker.assert_multi_buy_dispatch('paper','003220',ident)
        buy_price=self.market['current_price']
        self.tracker.register_order(OrderReceipt('paper','buy','003220','10',str(buy_price),'24',
            'https://mockapi.kiwoom.com'),S5,intent_id=ident)
        self.now+=timedelta(seconds=1);first=self.now
        self.event('execution_stream',first,{'9203':'24','9001':'A003220','907':'2','913':'체결',
            '908':first.strftime('%H%M%S'),'909':'1','914':str(buy_price),'915':'10','911':'10','903':str(buy_price*10)})
        buy=Execution('20261102','24','1','003220','fixture','buy',first.strftime('%H:%M:%S'),Decimal(buy_price),10)
        self.tracker._ingest('paper','20261102',(buy,))
        self.tracker=ExecutionTracker(log_dir=Path(self.temp.name),now=lambda:self.now,settings_loader=lambda:self.settings)
        self.now+=timedelta(minutes=30)
        position=self.tracker.multi_strategy_position('paper','003220',S5,self.now)
        self.assertEqual(position['first_fill_at'],first.isoformat())
        _,bars,market=fixture(90)
        ev=multi_strategy_evidence(strategy=S5,symbol='003220',side='sell',reason='time_stop_30m',
            budget=None,position=position,bars=bars[-21:],market=market,observed_at=self.now,
            quantity=10,exit_kind='full',source_hash='f'*64)
        holding=self.tracker.record_strategy_holding_observation('003220',10,10,buy_price*10,self.now,source_kind='broker_observation')
        proof={'schema_version':4,'symbol':'003220','pre_order_held_qty':10,'pre_order_pur_amt_krw':buy_price*10,
            'order_quantity':10,'observed_at':self.now.isoformat(),'holdings_source':'broker_observation',
            'generation_id':ident,'exit_kind':'full','strategy':S5,
            'symbol_ledger_sha256':self.tracker.symbol_order_watermark('paper','003220'),'holding_observation_id':holding}
        sell_id=self.tracker.prepare_order('paper','sell','003220',10,S5,price=None,evidence=ev,reconciliation_evidence=proof)
        self.tracker.assert_multi_exit_dispatch('paper','003220',sell_id)
        self.tracker.register_order(OrderReceipt('paper','sell','003220','10',None,'25','https://mockapi.kiwoom.com'),S5,intent_id=sell_id)
        self.now+=timedelta(seconds=1);price=market['current_price']
        self.event('execution_stream',self.now,{'9203':'25','9001':'A003220','907':'1','913':'체결',
            '908':self.now.strftime('%H%M%S'),'909':'2','914':str(price),'915':'10','911':'10','903':str(price*10)})
        self.tracker._ingest('paper','20261102',(buy,Execution('20261102','25','2','003220','fixture','sell',
            self.now.strftime('%H:%M:%S'),Decimal(price),10)))
        self.assertEqual(self.tracker.multi_strategy_position('paper','003220',S5,self.now)['status'],'flat')

    def test_initial_half_budget_cap_and_missing_zero_v5_rejected(self):
        with self.assertRaises(ExecutionLogError):self.buy(qty=50)  # 508,750 > 500,000
        proof=self.zero_proof()
        for key in ('integrated_pending_clear','pending_query_id','recorder_observations','recorder_ingress_sequence',
                    'engine_ingress_sequence','recorder_gap_count','reconciliation_completed_at',
                    'stream_anchor_id','stream_anchor_sha256','stream_anchor_observed_at'):proof.pop(key)
        proof['schema_version']=3
        with self.assertRaises(ExecutionLogError):
            self.tracker.prepare_order('paper','buy','003220',10,S5,price=self.market['current_price'],
                evidence=evidence(),reconciliation_evidence=proof)

    def test_entry_source_expiry_before_send_never_authorizes_transport(self):
        ident=self.buy();self.now+=timedelta(seconds=6)
        with self.assertRaises(ExecutionLogError):self.tracker.assert_multi_buy_dispatch('paper','003220',ident)

