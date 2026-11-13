"""Offline regression for paper accounting recovery boundaries."""
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
import json
import tempfile
import unittest
from zoneinfo import ZoneInfo

from kiwoom_autotrader.api.executions import Execution
from kiwoom_autotrader.api.orders import OrderReceipt
from kiwoom_autotrader.trading.execution_log import ExecutionTracker, ConcurrentPositionLimitUnavailable

class RepeatedExecutionSnapshots(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.now=datetime(2026,11,2,10,0,tzinfo=ZoneInfo('Asia/Seoul'))
        self.tracker=ExecutionTracker(log_dir=Path(self.tmp.name),now=lambda:self.now,
            settings_loader=lambda:{'paper_dynamic_scan_enabled':True,'paper_max_concurrent_positions':2})
        self.tracker.register_order(OrderReceipt('paper','buy','005930','2',None,'24','https://example.invalid'),'gdcrs')
        self.execution=Execution('20261102','24','1','005930','삼성전자','buy','09:59:00',Decimal('100'),2)
        self.tracker._ingest('paper','20261102',(self.execution,),source_kind='broker_observation')
        self.now+=timedelta(seconds=1)
        snapshot={'schema':'paper-account-snapshot-v1','mode':'paper','exchange':'KRX',
                  'cash':{'entr':'1000000','ord_alow_amt':'1000000'},
                  'holdings':[{'stk_cd':'A005930','rmnd_qty':'2'}]}
        with self.tracker._db() as db:
            db.execute('INSERT OR REPLACE INTO account_balance_daily VALUES(?,?,?,?,?,?)',
                       ('default','paper','20261102',self.now.isoformat(),'broker_observation',json.dumps(snapshot)))
        self.now+=timedelta(seconds=1)

    def test_identical_observed_snapshot_does_not_invalidate_new_balance(self):
        self.tracker._ingest('paper','20261102',(self.execution,),source_kind='broker_observation')
        self.assertEqual(self.tracker.paper_position_capacity('000660',self.now)['occupied_symbols'],['005930'])

    def test_changed_cumulative_snapshot_still_invalidates_balance(self):
        changed=Execution('20261102','24','1','005930','삼성전자','buy','09:59:00',Decimal('101'),2)
        self.tracker._ingest('paper','20261102',(changed,),source_kind='broker_observation')
        with self.assertRaises(ConcurrentPositionLimitUnavailable):
            self.tracker.paper_position_capacity('000660',self.now)

class PriorDayPendingRecovery(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.now=datetime(2026,10,30,15,0,tzinfo=ZoneInfo('Asia/Seoul'))
        self.options={}
        self.tracker=ExecutionTracker(log_dir=Path(self.tmp.name),now=lambda:self.now,settings_loader=lambda:self.options)
        self.tracker.register_order(OrderReceipt('paper','buy','005930','2',None,'24','https://example.invalid'),'gdcrs')
        self.now=datetime(2026,11,2,9,5,tzinfo=ZoneInfo('Asia/Seoul'))
        self.row={'ord_no':'0000024','stk_cd':'A005930','io_tp_nm':'현금매수','dmst_stex_tp':'KRX',
                  'ord_qty':'2','ord_remnq':'0','cntr_qty':'0'}

    def event(self, api, query, start, rows, *, day=None, continuation='N'):
        detail=api=='kt00007'
        body=({'ord_dt':'20261030','qry_tp':'1','stk_bond_tp':'1','sell_tp':'0',
               'stk_cd':'','fr_ord_no':'','dmst_stex_tp':'KRX'} if detail else
              {'all_stk_tp':'0','trde_tp':'0','stex_tp':'0','stk_cd':''})
        return {'schema':'kiwoom-broker-page-v1','source_api':api,'source_kind':'broker_observation',
            'mode':'paper','day':day or ('20261030' if detail else '20261102'),'query_id':query*32,
            'request_started_at':start.isoformat(),'observed_at':(start+timedelta(seconds=1)).isoformat(),
            'page':1,'request_body':body,
            'response_body':{'return_code':0,('acnt_ord_cntr_prps_dtl' if detail else 'oso'):rows},
            'continuation':{'request_cont_yn':'N','response_cont_yn':continuation,
                            'next_key_present':continuation=='Y'},'status':'success','http_status':200}

    def save(self, *, pending=(), detail=None, incomplete=False):
        self.tracker.record_broker_observation(self.event('kt00007','a',self.now-timedelta(seconds=8),
                                                          [self.row] if detail is None else detail,
                                                          continuation='Y' if incomplete else 'N'))
        self.tracker.record_broker_observation(self.event('ka10075','b',self.now-timedelta(seconds=5),list(pending)))

    def check(self):
        with self.tracker._db() as db:
            order=dict(db.execute("SELECT * FROM orders WHERE ord_no='24'").fetchone());order['filled_quantity']=0
            return self.tracker._paper_prior_order_terminal(db,order,self.now)

    def test_complete_matching_detail_and_current_pending_prove_terminal(self):
        self.save();self.assertTrue(self.check())

    def test_missing_detail_or_incomplete_pages_cannot_release(self):
        self.save(incomplete=True);self.assertFalse(self.check())

    def test_still_pending_or_quantity_mismatch_cannot_release(self):
        self.save(pending=[{'ord_no':'0000024','orig_ord_no':'0000000'}]);self.assertFalse(self.check())

    def test_unrelated_pending_does_not_hide_matching_terminal_order(self):
        self.save(pending=[{'ord_no':'0000011','orig_ord_no':'0000000'}]);self.assertTrue(self.check())

    def test_broker_and_observed_fills_must_match(self):
        self.save(detail=[{**self.row,'cntr_qty':'1'}]);self.assertFalse(self.check())

    def test_current_evidence_must_be_fresh(self):
        self.save();self.now+=timedelta(seconds=100);self.assertFalse(self.check())

    def test_terminal_release_is_verified_for_legacy_and_experiment_and_raw_order_preserved(self):
        self.save()
        with self.tracker._db() as db:
            self.assertEqual(len(self.tracker._pending_order_rows(db,'paper')),0)
        self.options['paper_experiment_enabled']=True
        with self.tracker._db() as db:
            self.assertEqual(len(self.tracker._pending_order_rows(db,'paper')),0)
            self.assertEqual(db.execute('SELECT quantity FROM orders WHERE ord_no=?',('24',)).fetchone()[0],2)

    def test_experiment_symbol_pending_isolation(self):
        self.options['paper_experiment_enabled']=True
        with self.tracker._db() as db:
            self.tracker._assert_no_pending_account(db,'paper',symbol='000660')
            with self.assertRaises(Exception):
                self.tracker._assert_no_pending_account(db,'paper',symbol='005930')
        self.options['paper_experiment_enabled']=False
        with self.tracker._db() as db:
            with self.assertRaises(Exception):
                self.tracker._assert_no_pending_account(db,'paper',symbol='000660')

    def test_unknown_theme_allowed_only_under_new_policy(self):
        from kiwoom_autotrader.trading.theme_concentration import _families, UNKNOWN_POOL
        self.assertEqual(_families('005930',{},'allow_unknown'),{UNKNOWN_POOL})
        with self.assertRaises(ValueError):_families('005930',{},'block')

    def test_delayed_broker_observation_reopens_pending(self):
        self.save();self.options['paper_experiment_enabled']=True
        ws={'schema_version':1,'source_api':'00','source_kind':'broker_observation',
            'observed_at':(self.now-timedelta(seconds=3)).isoformat(),'day':'20261030','mode':'paper',
            'event_kind':'execution_stream','values':{'9203':'24','9001':'A005930'}}
        self.tracker.record_broker_observation(ws)
        with self.tracker._db() as db:
            self.assertEqual(len(self.tracker._pending_order_rows(db,'paper')),1)

    def test_broker_status_source_and_symbol_must_match(self):
        self.save(detail=[{**self.row,'stk_cd':'A000660'}]);self.assertFalse(self.check())

    def test_experiment_capacity_25_is_not_available_in_default_mode(self):
        data={'schema':'paper-account-snapshot-v1','mode':'paper','exchange':'KRX',
              'cash':{'entr':'1000000','ord_alow_amt':'1000000'},'holdings':[]}
        with self.tracker._db() as db:
            db.execute('INSERT OR REPLACE INTO account_balance_daily VALUES(?,?,?,?,?,?)',
              ('default','paper','20261102',self.now.isoformat(),'broker_observation',json.dumps(data)))
            with self.assertRaises(ConcurrentPositionLimitUnavailable):
                self.tracker._assert_position_capacity(db,'000660',self.now,25)
            self.options['paper_experiment_enabled']=True
            self.assertEqual(self.tracker._assert_position_capacity(db,'000660',self.now,25)['limit'],25)

    def test_experiment_sizing_binds_available_cash_and_principal(self):
        from kiwoom_autotrader.trading.paper_policy import experiment_order_sizing
        from kiwoom_autotrader.trading.execution_log import ExecutionLogError
        data={'schema':'paper-account-snapshot-v1','mode':'paper','exchange':'KRX',
              'cash':{'entr':'9000000','ord_alow_amt':'6000000'},'holdings':[]}
        self.options['paper_experiment_enabled']=True
        sizing=experiment_order_sizing(self.options,'paper',available_cash_krw=6000000,price=10000,
                                       existing_principal_krw=0,pending_principal_krw=0)
        proof={'paper_experiment_sizing':sizing,'observed_at':self.now.isoformat(),'pre_order_pur_amt_krw':0}
        with self.tracker._db() as db:
            db.execute('INSERT OR REPLACE INTO account_balance_daily VALUES(?,?,?,?,?,?)',
              ('default','paper','20261102',self.now.isoformat(),'broker_observation',json.dumps(data)))
            self.tracker._assert_paper_experiment_budget(db,proof,13,10000,self.now)
            altered={**proof,'paper_experiment_sizing':{**sizing,'available_cash_krw':9000000}}
            with self.assertRaises(ExecutionLogError):
                self.tracker._assert_paper_experiment_budget(db,altered,13,10000,self.now)

    def test_stream_recovery_verifies_complete_saved_query_pages_and_balance(self):
        self.options['paper_experiment_enabled']=True
        start=self.now-timedelta(seconds=9)
        event={'schema':'kiwoom-broker-page-v1','source_api':'kt00009','source_kind':'broker_observation',
          'mode':'paper','day':'20261102','query_id':'c'*32,'request_started_at':start.isoformat(),
          'observed_at':(start+timedelta(seconds=1)).isoformat(),'page':1,
          'request_body':{'ord_dt':'20261102','qry_tp':'1','stk_bond_tp':'1','sell_tp':'0','stk_cd':'','fr_ord_no':'','dmst_stex_tp':'KRX'},
          'response_body':{'return_code':0,'acnt_ord_cntr_prst_array':[]},
          'continuation':{'request_cont_yn':'N','response_cont_yn':'N','next_key_present':False},'status':'success','http_status':200}
        self.tracker.record_broker_observation(event)
        account_at=self.now-timedelta(seconds=5)
        data={'schema':'paper-account-snapshot-v1','mode':'paper','exchange':'KRX','holdings':[],
              'cash':{'entr':'1000000','ord_alow_amt':'1000000'}}
        with self.tracker._db() as db:
            db.execute('INSERT OR REPLACE INTO account_balance_daily VALUES(?,?,?,?,?,?)',
              ('default','paper','20261102',account_at.isoformat(),'broker_observation',json.dumps(data)))
        self.tracker.record_broker_observation(self.event('ka10075','b',self.now-timedelta(seconds=3),[]))
        proof={'day':'20261102','execution_query_id':'c'*32,'pending_query_id':'b'*32,'account_observed_at':account_at.isoformat()}
        self.assertTrue(self.tracker.validate_paper_stream_recovery(proof,self.now))
        self.assertTrue(self.tracker.validate_paper_stream_recovery(proof,self.now,minimum_started_at=start))
        self.assertFalse(self.tracker.validate_paper_stream_recovery(proof,self.now,minimum_started_at=start+timedelta(microseconds=1)))
        self.assertFalse(self.tracker.validate_paper_stream_recovery(proof,self.now,minimum_started_at=start.replace(tzinfo=None)))
        self.assertFalse(self.tracker.validate_paper_stream_recovery(proof,self.now,minimum_started_at=self.now+timedelta(seconds=1)))
        self.assertFalse(self.tracker.validate_paper_stream_recovery({**proof,'pending_query_id':'d'*32},self.now))

    def gap_fixture(self, *, external=False, incomplete=False):
        self.options['paper_experiment_enabled']=True
        gap=self.now-timedelta(seconds=30)
        for kind,when in [('stream_gap',gap),('stream_resumed',gap+timedelta(seconds=1))]:
            self.tracker.record_broker_observation({'schema_version':1,'source_api':'00','source_kind':'broker_observation',
                'observed_at':when.isoformat(),'day':'20261102','mode':'paper','event_kind':kind,'values':{}})
        start=self.now-timedelta(seconds=20)
        detail={'schema':'kiwoom-broker-page-v1','source_api':'kt00007','source_kind':'broker_observation',
          'mode':'paper','day':'20261102','query_id':'d'*32,'request_started_at':start.isoformat(),
          'observed_at':(start+timedelta(seconds=1)).isoformat(),'page':1,
          'request_body':{'ord_dt':'20261102','qry_tp':'1','stk_bond_tp':'1','sell_tp':'0','stk_cd':'','fr_ord_no':'','dmst_stex_tp':'%'},
          'response_body':{'return_code':0,'acnt_ord_cntr_prps_dtl':([{'ord_no':'0000025','stk_cd':'A005930'}] if external else [])},
          'continuation':{'request_cont_yn':'N','response_cont_yn':'Y' if incomplete else 'N','next_key_present':incomplete},
          'status':'success','http_status':200}
        self.tracker.record_broker_observation(detail)
        account_at=self.now-timedelta(seconds=10)
        data={'schema':'paper-account-snapshot-v1','mode':'paper','exchange':'KRX',
              'holdings':[{'stk_cd':'A005930','rmnd_qty':'5','pur_amt':'500','crd_tp':'00'}],
              'cash':{'entr':'1000000','ord_alow_amt':'1000000'}}
        with self.tracker._db() as db:
            db.execute('INSERT OR REPLACE INTO account_balance_daily VALUES(?,?,?,?,?,?)',
              ('default','paper','20261102',account_at.isoformat(),'broker_observation',json.dumps(data)))
        self.tracker.record_broker_observation(self.event('ka10075','e',self.now-timedelta(seconds=5),[]))
        return gap

    def test_post_gap_owned_validation_requires_complete_fresh_broker_evidence(self):
        gap=self.gap_fixture()
        with self.tracker._db() as db:
            self.assertTrue(self.tracker._paper_owned_after_gap(db,'005930',self.now,gap,5,500))
            self.assertFalse(self.tracker._paper_owned_after_gap(db,'005930',self.now,gap,4,500))

    def test_post_gap_external_order_blocks_attribution(self):
        gap=self.gap_fixture(external=True)
        with self.tracker._db() as db:
            self.assertFalse(self.tracker._paper_owned_after_gap(db,'005930',self.now,gap,5,500))

    def test_post_gap_partial_broker_pagination_blocks_attribution(self):
        gap=self.gap_fixture(incomplete=True)
        with self.tracker._db() as db:
            self.assertFalse(self.tracker._paper_owned_after_gap(db,'005930',self.now,gap,5,500))
