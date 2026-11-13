"""Offline auction sell remainder tests, no credentials or network."""
from datetime import datetime,timedelta
from decimal import Decimal
import hashlib,json,sqlite3,tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock,patch
from zoneinfo import ZoneInfo
from kiwoom_autotrader.api.cancellations import CancellationReceipt,CancellationError
from kiwoom_autotrader.api.executions import Execution
from kiwoom_autotrader.trading.execution_log import ExecutionTracker
from kiwoom_autotrader.trading.unfilled_sell_auction import SellAuctionMonitor,settle_sell_auction

KST=ZoneInfo('Asia/Seoul')
class AuctionSellTests(TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.now=datetime(2026,11,2,15,30,tzinfo=KST)
        self.tracker=ExecutionTracker(log_dir=Path(self.temp.name),now=lambda:self.now)
        with self.tracker._db():pass
        self.generation='a'*32;self.intent='b'*32
        evidence={'strategy':'EXP_ORB_A','strategy_version':'paper-experiment-v1','source_kind':'broker_observation',
            'config':{'multi_exit_kind':'full','multi_position_generation_id':self.generation,
                      'experiment_order_window_end_at':self.now.isoformat()}}
        self.raw=json.dumps(evidence,separators=(',',':'));digest=hashlib.sha256(self.raw.encode()).hexdigest()
        self.registered='2026-11-02T15:29:30+09:00'
        with sqlite3.connect(self.tracker.path) as db:
            db.execute('INSERT INTO orders VALUES (?,?,?,?,?,?,?,?,?)',('default','20261102','paper','1','005930','sell','EXP_ORB_A',10,self.registered))
            db.execute('INSERT INTO intents VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',('default',self.intent,'20261102','paper','sell','005930',10,'EXP_ORB_A',self.registered,'1','accepted',None))
            db.execute('INSERT INTO order_decisions VALUES (?,?,?,?,?,?,?)',('default',self.intent,self.registered,None,'market',self.raw,digest))
            rec={'schema_version':4,'symbol':'005930','strategy':'EXP_ORB_A','generation_id':self.generation,
                 'holdings_source':'broker_observation','order_quantity':10,'pre_order_held_qty':10,'exit_kind':'full',
                 'observed_at':'2026-11-02T15:29:29+09:00'}
            recraw=json.dumps(rec,separators=(',',':'))
            db.execute('INSERT INTO pre_order_reconciliations VALUES (?,?,?,?,?)',('default',self.intent,self.registered,recraw,hashlib.sha256(recraw.encode()).hexdigest()))
            db.execute('INSERT INTO multi_strategy_exit_claims VALUES (?,?,?,?,?)',('default',self.generation,'full',self.intent,self.registered))
        self.recorder=SimpleNamespace(status=lambda:{'ready':True,'error':None,'gap_count':0,'observations':1,
            'ingress_sequence':0,'engine_ingress_sequence':0,'inflight_callbacks':0,'engine_pending_callbacks':0})
        self.session=SimpleNamespace(mode='paper',execution_recorder=self.recorder)
        self.session.paper_automation_reconcile=SimpleNamespace(_session=self.session,_tracker=self.tracker)
        self.pending=[{'ord_no':'0000001','stk_cd':'A005930','oso_qty':'10','io_tp_nm':'-매도','stex_tp':'1',
                       'sor_yn':'N','orig_ord_no':'0000000','ord_stt':'접수','ord_qty':'10','tm':'152930'}]
        self.executions=[];self.fetch=Mock(side_effect=lambda *a,**k:self.executions)
        self.pending_fetch=Mock(side_effect=lambda *a,**k:self.pending)
        self.cancel=Mock(side_effect=self.ack)
        self.identity=patch('kiwoom_autotrader.trading.unfilled_sell_auction.session_identity',return_value=('paper','fake'))
        self.identity.start();self.addCleanup(self.identity.stop)
        self.monitor=SellAuctionMonitor(now=lambda:self.now,pending_fetcher=self.pending_fetch,cancel_handler=self.cancel,
            monotonic=lambda:10,execution_fetcher=self.fetch)
    def ack(self,session,symbol,number,*,before_send):
        before_send();return CancellationReceipt(number,'2',0)
    def counts(self):
        with sqlite3.connect(self.tracker.path) as db:
            return [db.execute('SELECT COUNT(*) FROM '+t).fetchone()[0] for t in
                    ('orders','fills','sell_auction_cancellation_attempts','sell_auction_cancellation_outcomes')]
    def test_cancel_once_ack_does_not_release_or_change_position(self):
        self.monitor.tick(self.session,self.tracker)
        self.assertEqual(self.counts(),[1,0,1,1]);self.cancel.assert_called_once()
        self.assertEqual(self.monitor.last_status['status'],'cancel_input_ack_unconfirmed_settlement')
        self.monitor.tick(self.session,self.tracker);self.cancel.assert_called_once()
    def test_uncertain_result_never_resends(self):
        self.cancel.side_effect=CancellationError('uncertain',uncertain=True)
        self.monitor.tick(self.session,self.tracker);self.monitor.tick(self.session,self.tracker)
        self.cancel.assert_called_once();self.assertEqual(self.counts(),[1,0,1,1])

    def test_next_poll_attempts_confirmation_without_resending_cancel(self):
        self.monitor.tick(self.session,self.tracker)
        with patch('kiwoom_autotrader.trading.unfilled_sell_auction.settle_sell_auction',
                   return_value={'status':'cancellation_confirmed'}) as settle:
            self.monitor.tick(self.session,self.tracker)
        settle.assert_called_once();self.cancel.assert_called_once()
        self.assertEqual(self.monitor.last_status['status'],'cancellation_confirmed')
    def test_wrong_owner_or_pending_mismatch_blocks_before_claim(self):
        with sqlite3.connect(self.tracker.path) as db:
            db.execute('DROP TRIGGER multi_strategy_exit_claims_no_delete')
            db.execute('DELETE FROM multi_strategy_exit_claims')
        self.monitor.tick(self.session,self.tracker);self.cancel.assert_not_called()
        with sqlite3.connect(self.tracker.path) as db:
            db.execute('INSERT INTO multi_strategy_exit_claims VALUES (?,?,?,?,?)',('default',self.generation,'full',self.intent,self.registered))
        self.pending[0]['oso_qty']='9';self.monitor.tick(self.session,self.tracker);self.cancel.assert_not_called()

    def test_owner_change_at_dispatch_keeps_claim_and_prevents_resend(self):
        def changed(session,symbol,number,*,before_send):
            self.tracker.record_broker_observation({'schema_version':1,'source_api':'00','source_kind':'broker_observation',
                'observed_at':'2026-11-02T15:29:40+09:00','day':'20261102','mode':'paper','event_kind':'stream_gap','values':{}})
            before_send()
        self.cancel.side_effect=changed
        self.monitor.tick(self.session,self.tracker)
        self.assertEqual(self.counts(),[1,0,1,1])
        self.assertEqual(self.monitor.last_status['reason'],'sell_cancel_ledger_changed')
        self.monitor.tick(self.session,self.tracker)
        self.cancel.assert_called_once()
    def test_wrong_evidence_window_or_hash_never_queries_broker(self):
        with sqlite3.connect(self.tracker.path) as db:
            db.execute('DROP TRIGGER order_decisions_no_update')
            db.execute('UPDATE order_decisions SET evidence_sha256=?',('0'*64,))
        self.monitor.tick(self.session,self.tracker);self.fetch.assert_not_called();self.cancel.assert_not_called()
    def test_recorded_00_gap_blocks_cancel(self):
        self.tracker.record_broker_observation({'schema_version':1,'source_api':'00','source_kind':'broker_observation',
          'observed_at':'2026-11-02T15:29:40+09:00','day':'20261102','mode':'paper','event_kind':'stream_gap','values':{}})
        self.monitor.tick(self.session,self.tracker);self.cancel.assert_not_called()
    def test_partial_fill_must_match_00_rest_and_broker_remaining(self):
        self.tracker.record_broker_observation({'schema_version':1,'source_api':'00','source_kind':'broker_observation',
          'observed_at':'2026-11-02T15:29:40+09:00','day':'20261102','mode':'paper','event_kind':'execution_stream',
          'values':{'9203':'1','9001':'A005930','907':'1','913':'체결','908':'152940','909':'1','914':'1000','915':'3','911':'3','903':'3000'}})
        execution=Execution('20261102','1','1','005930','fixture','sell','15:29:40',Decimal('1000'),3)
        self.tracker._ingest('paper','20261102',(execution,),source_kind='broker_observation')
        self.executions=[execution];self.pending[0]['oso_qty']='7'
        self.monitor.tick(self.session,self.tracker)
        self.assertEqual(self.counts(),[1,1,1,1]);self.cancel.assert_called_once()

    def settlement_observations(self,*,bad_child=False,pending=False,filled=0,full_fill=False):
        self.now+=timedelta(seconds=1)
        if not full_fill:self.tracker.record_broker_observation({'schema_version':1,'source_api':'00','source_kind':'broker_observation',
          'observed_at':self.now.isoformat(),'day':'20261102','mode':'paper','event_kind':'execution_stream',
          'values':{'9203':'2','9001':'A005930','900':str(9-filled if bad_child else 10-filled),'902':'0','903':'0',
                    '904':'1','905':'-매도취소','907':'1','908':'153001','909':'','913':'확인',
                    '2134':'1','2136':'N','922':'00'}})
        original={'ord_no':'1','orig_ord_no':'0','stk_cd':'A005930','stk_bond_tp':'1','io_tp_nm':'현금매도',
                  'ord_qty':'10','ord_uv':'0','cnfm_qty':'0','cntr_no':'1' if filled else '0','cntr_qty':str(filled),'cntr_uv':'1000' if filled else '0',
                  'cntr_tm':'','mdfy_cncl_tp':'','dmst_stex_tp':'KRX'}
        child={'ord_no':'2','orig_ord_no':'1','stk_cd':'A005930','stk_bond_tp':'1','io_tp_nm':'현금매도',
               'ord_qty':str(10-filled),'ord_uv':'0','cnfm_qty':str(10-filled),'cntr_no':'0','cntr_qty':'0','cntr_uv':'0',
               'cntr_tm':'','mdfy_cncl_tp':'취소주문','dmst_stex_tp':'KRX'}
        def fetch(api,rows):
            def call(session,*,observer,day):
                ispending=api=='ka10075'
                event={'schema':'kiwoom-broker-page-v1','source_api':api,'source_kind':'broker_observation',
                  'mode':'paper','day':day,'query_id':('a' if ispending else 'b')*32,
                  'request_started_at':self.now.isoformat(),'observed_at':self.now.isoformat(),'page':1,
                  'request_body':({'all_stk_tp':'0','trde_tp':'0','stex_tp':'0'} if ispending else
                    {'ord_dt':day,'stk_bond_tp':'1','mrkt_tp':'0','sell_tp':'0','qry_tp':'0','stk_cd':'','fr_ord_no':'','dmst_stex_tp':'KRX'}),
                  'response_body':{'return_code':0,('oso' if ispending else 'acnt_ord_cntr_prst_array'):rows},
                  'continuation':{'request_cont_yn':'N','response_cont_yn':'N','next_key_present':False},'status':'success','http_status':200}
                observer(event);return rows
            return call
        pending_rows=[{'ord_no':'1','orig_ord_no':'0','oso_qty':'10'}] if pending else []
        return fetch('ka10075',pending_rows),fetch('kt00009',[original] if full_fill else [original,child])

    def test_confirmed_cancel_releases_only_remainder_and_late_fill_invalidates(self):
        self.monitor.tick(self.session,self.tracker)
        pending,orders=self.settlement_observations()
        result=settle_sell_auction(self.session,self.tracker,{'original_ord_no':'1'},now=lambda:self.now,
             monotonic=lambda:10,pending_fetcher=pending,order_fetcher=orders)
        self.assertEqual((result['status'],result['filled_quantity'],result['cancelled_quantity']),('cancellation_confirmed',0,10))
        with self.tracker._db() as db:
            self.assertEqual(self.tracker._pending_order_rows(db,'paper'),[])
            db.execute('INSERT INTO fills VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',('default','20261102','paper','1','99','005930','fixture','sell','15:30:02','1000',1,'EXP_ORB_A',None,None,None))
        from kiwoom_autotrader.trading.execution_log import ExecutionLogError
        with self.tracker._db() as db:
            with self.assertRaises(ExecutionLogError):self.tracker._pending_order_rows(db,'paper')

    def test_wrong_terminal_or_nonzero_broker_pending_cannot_release(self):
        self.monitor.tick(self.session,self.tracker)
        pending,orders=self.settlement_observations(bad_child=True)
        with self.assertRaises(Exception):
            settle_sell_auction(self.session,self.tracker,{'original_ord_no':'1'},now=lambda:self.now,
                monotonic=lambda:10,pending_fetcher=pending,order_fetcher=orders)
        with self.tracker._db() as db:self.assertEqual(len(self.tracker._pending_order_rows(db,'paper')),1)

    def test_pending_remainder_cannot_be_declared_cancelled(self):
        self.monitor.tick(self.session,self.tracker)
        pending,orders=self.settlement_observations(pending=True)
        with self.assertRaises(Exception):
            settle_sell_auction(self.session,self.tracker,{'original_ord_no':'1'},now=lambda:self.now,
                monotonic=lambda:10,pending_fetcher=pending,order_fetcher=orders)
        with self.tracker._db() as db:self.assertEqual(len(self.tracker._pending_order_rows(db,'paper')),1)

    def test_partial_fill_settlement_preserves_fills_and_b1_does_not_release_twice(self):
        self.tracker.record_broker_observation({'schema_version':1,'source_api':'00','source_kind':'broker_observation',
          'observed_at':'2026-11-02T15:29:40+09:00','day':'20261102','mode':'paper','event_kind':'execution_stream',
          'values':{'9203':'1','9001':'A005930','907':'1','913':'체결','908':'152940','909':'1','914':'1000','915':'3','911':'3','903':'3000'}})
        execution=Execution('20261102','1','1','005930','fixture','sell','15:29:40',Decimal('1000'),3)
        self.tracker._ingest('paper','20261102',(execution,),source_kind='broker_observation')
        self.executions=[execution];self.pending[0]['oso_qty']='7';self.monitor.tick(self.session,self.tracker)
        pending,orders=self.settlement_observations(filled=3)
        result=settle_sell_auction(self.session,self.tracker,{'original_ord_no':'1'},now=lambda:self.now,
             monotonic=lambda:10,pending_fetcher=pending,order_fetcher=orders)
        self.assertEqual((result['filled_quantity'],result['cancelled_quantity']),(3,7))
        with self.tracker._db() as db:
            self.assertEqual(self.tracker._pending_order_rows(db,'paper'),[])
            self.assertEqual(db.execute('SELECT quantity FROM fills').fetchone()[0],3)
            order=db.execute("SELECT * FROM orders WHERE ord_no='1'").fetchone()
            self.tracker.settings_loader=lambda:{'paper_experiment_enabled':True}
            self.tracker._paper_prior_order_terminal=Mock(return_value=True)
            self.assertEqual(self.tracker._confirmed_expired_quantity(db,order,self.now+timedelta(days=1)),0)

    def test_full_fill_race_resolves_without_inventing_cancel(self):
        self.monitor.tick(self.session,self.tracker)
        self.now+=timedelta(seconds=1)
        self.tracker.record_broker_observation({'schema_version':1,'source_api':'00','source_kind':'broker_observation',
          'observed_at':self.now.isoformat(),'day':'20261102','mode':'paper','event_kind':'execution_stream',
          'values':{'9203':'1','9001':'A005930','907':'1','913':'체결','908':'153001','909':'1','914':'1000','915':'10','911':'10','903':'10000'}})
        execution=Execution('20261102','1','1','005930','fixture','sell','15:30:01',Decimal('1000'),10)
        self.tracker._ingest('paper','20261102',(execution,),source_kind='broker_observation')
        pending,orders=self.settlement_observations(filled=10,full_fill=True)
        result=settle_sell_auction(self.session,self.tracker,{'original_ord_no':'1'},now=lambda:self.now,
            monotonic=lambda:10,pending_fetcher=pending,order_fetcher=orders)
        self.assertEqual((result['status'],result['filled_quantity'],result['cancelled_quantity']),('fill_resolved',10,0))
        with self.tracker._db() as db:
            self.assertEqual(self.tracker._pending_order_rows(db,'paper'),[])
            self.assertEqual(db.execute('SELECT quantity FROM fills').fetchone()[0],10)
