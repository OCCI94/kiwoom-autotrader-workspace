"""Offline real-ledger D -> D+1 evidence scheduler regression."""
from datetime import datetime,timedelta
from decimal import Decimal
from pathlib import Path
from tests.test_orders import fake_session
from kiwoom_autotrader.api.session import IssuedToken
from unittest import TestCase
from unittest.mock import Mock,patch
import tempfile,hashlib
from zoneinfo import ZoneInfo
from kiwoom_autotrader.api.executions import Execution
from kiwoom_autotrader.api.orders import OrderReceipt
from kiwoom_autotrader.trading.execution_log import ExecutionTracker
from kiwoom_autotrader.trading import paper_prior_day_recovery as m
KST=ZoneInfo('Asia/Seoul')

class PriorDaySchedulerTests(TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name).resolve()
  self.now=datetime(2026,11,2,14,0,tzinfo=KST);self.session=fake_session(clock=lambda:self.now)
  self.tracker=ExecutionTracker(log_dir=self.root/'ledger',now=lambda:self.now,settings_loader=lambda:{'paper_experiment_enabled':True})
  self.tracker.register_order(OrderReceipt('paper','buy','005930','2','100','24','https://example.invalid'),'gdcrs')
  self.now+=timedelta(seconds=1)
  self.tracker.record_broker_observation({'schema_version':1,'source_api':'00','source_kind':'broker_observation',
   'observed_at':self.now.isoformat(),'day':'20261102','mode':'paper','event_kind':'execution_stream',
   'values':{'9203':'24','9001':'A005930','907':'2','913':'체결','908':'140001','909':'1','914':'100','915':'1','911':'1','903':'100'}})
  self.tracker._ingest('paper','20261102',(Execution('20261102','24','1','005930','fixture','buy','14:00:01',Decimal('100'),1),),source_kind='broker_observation')
  self.scheduler=m.PriorDayPendingScheduler(claim_dir=self.root/'claims',launch=lambda work:work())
  self.details=[];self.pending=[];self.incomplete=False
 def calendar(self):
  return {'start':self.now.replace(hour=9,minute=0,second=0),'end':self.now.replace(hour=15,minute=30,second=0),
   'session_day':self.now.strftime('%Y%m%d'),'calendar_source_sha256':'c'*64}
 def rows(self,session,api,body,field,*,observer,day):
  self.details.append((api,day,body));self.now+=timedelta(seconds=1)
  value={'ord_no':'24','stk_cd':'A005930','io_tp_nm':'현금매수','dmst_stex_tp':'KRX','ord_qty':'2','cntr_qty':'1','ord_remnq':'0'}
  observer(self.event(api,day,body,field,[value],continued=self.incomplete))
  if self.incomplete:raise ValueError('pagination_incomplete_fixture')
  return [value]
 def pend(self,session,*,observer,day,integrated):
  self.pending.append((day,integrated));self.now+=timedelta(seconds=1)
  observer(self.event('ka10075',day,{'all_stk_tp':'0','trde_tp':'0','stex_tp':'0'},'oso',[]));return []
 def event(self,api,day,body,field,rows,continued=False):
  return {'schema':'kiwoom-broker-page-v1','source_api':api,'source_kind':'broker_observation','mode':'paper','day':day,
   'query_id':hashlib.sha256((api+self.now.isoformat()).encode()).hexdigest()[:32],'request_started_at':(self.now-timedelta(seconds=1)).isoformat(),
   'observed_at':self.now.isoformat(),'page':1,'request_body':body,'response_body':{'return_code':0,field:rows},
   'continuation':{'request_cont_yn':'N','response_cont_yn':'Y' if continued else 'N','next_key_present':continued},'status':'success','http_status':200}
 def pending_count(self):
  with self.tracker._db() as db:return len(self.tracker._pending_order_rows(db,'paper'))
 def run_next_day(self):
  self.now=datetime(2026,11,3,9,0,tzinfo=KST)
  with patch.object(m,'_request_rows',side_effect=self.rows),patch.object(m,'_pending_rows',side_effect=self.pend):
   return self.scheduler.tick(self.session,self.tracker,self.now,self.calendar())
 def test_partial_limit_reconciles_d_plus_one_without_restart_and_preserves_fill(self):
  self.assertEqual(self.pending_count(),1);result=self.run_next_day()
  self.assertEqual(result['status'],'reconciled');self.assertFalse(result['pending']);self.assertEqual(self.pending_count(),0)
  self.assertEqual(self.details[0][1],'20261102');self.assertEqual(self.details[0][2]['qry_tp'],'1');self.assertEqual(self.pending,[('20261103',True)])
  self.now+=timedelta(minutes=10);self.assertEqual(self.pending_count(),0)
  with self.tracker._db() as db:
   self.assertEqual(db.execute('SELECT quantity FROM fills').fetchone()[0],1)
   self.assertEqual(db.execute('SELECT quantity FROM orders').fetchone()[0],2)
  # The same living process and a recreated scheduler cannot duplicate a phase.
  with patch.object(m,'_request_rows',side_effect=AssertionError('unexpected retry')):
   self.scheduler.tick(self.session,self.tracker,self.now,self.calendar())
   other=m.PriorDayPendingScheduler(claim_dir=self.root/'claims',launch=lambda work:work())
   other.tick(self.session,self.tracker,self.now,self.calendar())
 def test_incomplete_page_keeps_pending_and_durable_claim_prevents_retry(self):
  self.incomplete=True;result=self.run_next_day();self.assertEqual(result['status'],'evidence_incomplete_no_retry')
  self.assertTrue(result['pending']);self.assertEqual(self.pending_count(),1);self.assertEqual(self.pending,[])
  other=m.PriorDayPendingScheduler(claim_dir=self.root/'claims',launch=lambda work:work())
  with patch.object(m,'_request_rows',side_effect=AssertionError('unexpected retry')):
   result=other.tick(self.session,self.tracker,self.now,self.calendar());self.assertTrue(result['pending'])
 def test_no_calendar_or_real_mode_never_calls_or_claims(self):
  collector=Mock();scheduler=m.PriorDayPendingScheduler(claim_dir=self.root/'claims',launch=lambda work:work(),collector=collector)
  scheduler.tick(self.session,self.tracker,self.now,None);self.session.mode='real'
  scheduler.tick(self.session,self.tracker,self.now,self.calendar());collector.assert_not_called();self.assertFalse((self.root/'claims').exists())
 def test_changed_session_before_background_run_is_unknown_and_not_reissued(self):
  work=[];collector=Mock();scheduler=m.PriorDayPendingScheduler(claim_dir=self.root/'claims',launch=work.append,collector=collector)
  self.now=datetime(2026,11,3,9,0,tzinfo=KST);scheduler.tick(self.session,self.tracker,self.now,self.calendar())
  self.session._issued=IssuedToken('different-fixture','20300101090000');work[0]();collector.assert_not_called()
  self.assertEqual(scheduler.status()['reason'],'session_changed');self.assertTrue(scheduler.status()['pending'])
  scheduler.tick(self.session,self.tracker,self.now,self.calendar());self.assertEqual(len(work),1)
 def test_once_postclose_after_open_is_a_separate_official_phase(self):
  self.incomplete=True;self.run_next_day();self.now=self.now.replace(hour=15,minute=30)
  collector=Mock(return_value={'status':'pending','pending':True,'orders_sent':0})
  self.scheduler.collector=collector
  self.scheduler.tick(self.session,self.tracker,self.now,self.calendar());self.scheduler.tick(self.session,self.tracker,self.now,self.calendar())
  collector.assert_called_once();self.assertEqual(len(list((self.root/'claims').glob('*.claim.json'))),2)

 def general_recovery(self):
  from kiwoom_autotrader.trading import paper_experiment_recovery as recovery
  def detail(session,api,body,field,*,observer,day):
   if day=='20261102':return self.rows(session,api,body,field,observer=observer,day=day)
   self.now+=timedelta(seconds=1);observer(self.event(api,day,body,field,[]));return []
  with patch.object(recovery,'_request_rows',side_effect=detail),patch.object(recovery,'_pending_rows',side_effect=self.pend), \
    patch.object(recovery,'fetch_executions',return_value=()),patch.object(recovery,'fetch_account_report',return_value={}), \
    patch.object(self.tracker,'_holdings'),patch.object(self.tracker,'_save_paper_balance_snapshot'):
   return recovery.recover_paper_experiment(self.session,self.tracker,self.now)
 def test_general_recovery_recaptures_new_full_queries_before_old_certificate_can_expire(self):
  self.assertEqual(self.run_next_day()['status'],'reconciled');self.now+=timedelta(minutes=10)
  # A new complete prior-date query invalidates the old capture before recovery.
  self.rows(self.session,'kt00007',{'ord_dt':'20261102','qry_tp':'1','stk_bond_tp':'1','sell_tp':'0','stk_cd':'','fr_ord_no':'','dmst_stex_tp':'KRX'},
   'acnt_ord_cntr_prps_dtl',observer=self.tracker.record_broker_observation,day='20261102')
  self.assertEqual(self.pending_count(),1)
  result=self.general_recovery();self.assertTrue(result['reconciled']);self.assertEqual(result['expiry_settlements']['captured_count'],1)
  self.now+=timedelta(seconds=100);self.assertEqual(self.pending_count(),0)
 def test_partial_general_recovery_invalidates_old_capture_without_releasing_residual(self):
  self.assertEqual(self.run_next_day()['status'],'reconciled');self.now+=timedelta(minutes=10);self.incomplete=True
  # Force normal prior discovery to include an externally started partial query.
  try:self.rows(self.session,'kt00007',{'ord_dt':'20261102','qry_tp':'1','stk_bond_tp':'1','sell_tp':'0','stk_cd':'','fr_ord_no':'','dmst_stex_tp':'KRX'},
   'acnt_ord_cntr_prps_dtl',observer=self.tracker.record_broker_observation,day='20261102')
  except ValueError:pass
  result=self.general_recovery();self.assertFalse(result['reconciled']);self.assertNotIn('expiry_settlements',result)
  self.now+=timedelta(seconds=100);self.assertEqual(self.pending_count(),1)
