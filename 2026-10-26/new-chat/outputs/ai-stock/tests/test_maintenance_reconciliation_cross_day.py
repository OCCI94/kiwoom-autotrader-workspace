"""Current query clocks and prior order dates stay distinct, using real readers."""
from copy import deepcopy
from datetime import datetime,timedelta
import hashlib,json,os,unittest
from unittest.mock import patch
from kiwoom_autotrader.api.session import KST
from kiwoom_autotrader.trading import maintenance_reconciliation as m
from tests.test_maintenance_reconciliation import PhaseFixture
from tests import test_account_report as account_fixture


class CrossDayFixture(PhaseFixture):
 def initialize(self):
  super().initialize();self.now=datetime(2026,11,4,9,30,tzinfo=KST)
  self.history_multi=False;self.history_partial=False;self.history_rows=[];self.today_rows=[];self.renew_marker()
  self.set_days()
 def set_days(self,prior=None,current='20261104'):
  value=json.loads(self.marker.read_text());value.update(current_day=current,prior_order_days=['20261103'] if prior is None else prior)
  self.marker.write_text(json.dumps(value));self.marker.chmod(0o600);os.utime(self.marker,(self.now.timestamp(),self.now.timestamp()))
 def transport(self,opener,request,**options):
  api=request.get_header('Api-id')
  if api=='kt00009' and self.today_rows:
   if self.transport_hook:self.transport_hook(api)
   options['before_send']();self.requests.append((api,json.loads(request.data)))
   return account_fixture.Response({'return_code':0,'acnt_ord_cntr_prst_array':self.today_rows},headers={'api-id':api,'cont-yn':'N'})
  if api!='kt00007':return super().transport(opener,request,**options)
  if self.transport_hook:self.transport_hook(api)
  options['before_send']();self.requests.append((api,json.loads(request.data)))
  continued=request.get_header('Cont-yn')=='Y'
  if self.history_partial and continued:raise OSError('private history provider detail')
  headers={'api-id':api,'cont-yn':'N'}
  if (self.history_multi or self.history_partial) and not continued:headers.update({'cont-yn':'Y','next-key':'PRIVATE-CURSOR'})
  return account_fixture.Response({'return_code':0,'acnt_ord_cntr_prps_dtl':self.history_rows},headers=headers)


class CrossDayMaintenanceTests(CrossDayFixture,unittest.TestCase):
 def setUp(self):self.initialize()
 def test_real_prior_history_then_current_queries_have_exact_dates_ids_hashes_and_no_core_change(self):
  self.history_multi=True;before=m._watermark(self.tracker);result=self.run_phase()
  self.assertEqual(result['status'],'complete',result)
  self.assertEqual([a for a,_ in self.requests],['kt00007','kt00007','kt00009','kt00001','kt00018','ka10075'])
  self.assertEqual(self.requests[0][1],m._history_request('20261103'))
  self.assertEqual(self.requests[2][1]['ord_dt'],'20261104');self.assertEqual(self.requests[2][1]['qry_tp'],'0')
  self.assertEqual(result['current_day'],'20261104');self.assertEqual(result['prior_order_days'],['20261103'])
  history=result['history_queries']['20261103'];self.assertEqual(history['pages'],2)
  self.assertEqual(history['current_orders_query_id'],result['queries']['kt00009']['query_id'])
  self.assertEqual(history['current_pending_query_id'],result['queries']['ka10075']['query_id'])
  self.assertTrue(history['request_started_at'].startswith('2026-11-04'))
  self.assertEqual(history['day'],'20261103');self.assertEqual(history['request_body']['ord_dt'],'20261103')
  directory=self.state_dir/'maintenance-readonly-evidence'/result['phase_id']
  for n,digest in enumerate(history['page_sha256'],1):
   event=json.loads((directory/('kt00007-'+str(n).zfill(2)+'.json')).read_text())
   self.assertEqual(m._sha(event),digest);self.assertEqual(event['day'],'20261103')
   self.assertTrue(event['observed_at'].startswith('2026-11-04'))
  self.assertEqual(m._watermark(self.tracker),before)
  self.tracker._ingest.assert_not_called();self.tracker._holdings.assert_not_called();self.tracker.record_paper_expired_settlements.assert_not_called()
  self.assertFalse(result['settlements_written']);self.assertEqual(result['orders_sent'],0)
 def test_marker_day_contract_refuses_future_wrong_invalid_excessive_and_stale_dates_without_queries(self):
  cases=[('20261103',['20261102']),('20261104',['20261104']),('20261104',['20261105']),
   ('20261104',['20260230']),('20261104',['20261003']),('20261104',['20261102','20261103']),
   ('20261104','20261103'),('20261104',[20261103])]
  for current,prior in cases:
   with self.subTest(current=current,prior=prior):
    self.initialize();self.set_days(prior,current);result=self.run_phase()
    self.assertEqual(result['status'],'failed');self.assertEqual(self.requests,[]);self.assertTrue(self.phase.blocked)
 def test_same_day_omitted_contract_remains_four_query_compatible(self):
  self.renew_marker();result=self.run_phase();self.assertEqual(result['status'],'complete',result)
  self.assertEqual(result['history_queries'],{});self.assertEqual(result['prior_order_days'],[])
  self.assertEqual([a for a,_ in self.requests],['kt00009','kt00001','kt00018','ka10075'])
 def test_missing_history_cannot_be_replaced_by_empty_current_pending(self):
  self.phase.history=lambda *a,**k:[];result=self.run_phase()
  self.assertEqual(result['status'],'failed');self.assertEqual(result['reason'],'maintenance_history_pages_incomplete')
  self.assertEqual(self.requests,[]);self.assertTrue(self.phase.blocked)
 def test_partial_history_retains_actual_pages_but_never_reaches_current_queries(self):
  self.history_partial=True;result=self.run_phase();self.assertEqual(result['status'],'failed')
  self.assertEqual([a for a,_ in self.requests],['kt00007','kt00007'])
  directory=self.state_dir/'maintenance-readonly-evidence'/result['phase_id']
  self.assertTrue((directory/'kt00007-01.json').is_file());self.assertTrue((directory/'failed.json').is_file())
  self.assertFalse((directory/'complete.json').exists());self.assertTrue(self.phase.blocked)
 def test_history_wrong_day_request_and_query_identity_mixing_are_rejected(self):
  for corrupt in ('day','request','query_id'):
   with self.subTest(corrupt=corrupt):
    self.initialize();self.history_multi=True
    def fetch(session,*,observer,day,before_send):
     def observe(event):
      event=deepcopy(event)
      if corrupt=='day':event['day']='20261104'
      elif corrupt=='request':event['request_body']['ord_dt']='20261104'
      elif event['page']==2:event['query_id']='b'*32
      observer(event)
     return m._fetch_history(session,observer=observe,day=day,before_send=before_send)
    self.phase.history=fetch;result=self.run_phase();self.assertEqual(result['status'],'failed')
    self.assertNotIn('ka10075',[a for a,_ in self.requests]);self.assertTrue(self.phase.blocked)
 def test_history_page_timestamp_is_actual_query_clock_not_prior_day_clock(self):
  def fetch(session,*,observer,day,before_send):
   def observe(event):
    event=deepcopy(event);event['request_started_at']='2026-11-03T09:30:00+09:00';observer(event)
   return m._fetch_history(session,observer=observe,day=day,before_send=before_send)
  self.phase.history=fetch;result=self.run_phase();self.assertEqual(result['status'],'failed');self.assertTrue(self.phase.blocked)
 def test_same_number_on_history_and_current_rows_stays_date_bound_without_settlement(self):
  self.history_rows=[{'ord_no':'123456','stk_cd':'005930','ord_qty':'2','cntr_qty':'0','ord_remnq':'0',
   'io_tp_nm':'현금매수','dmst_stex_tp':'KRX'}]
  self.today_rows=[{'ord_no':'123456','stk_cd':'005930','ord_qty':'99','cntr_qty':'0','io_tp_nm':'현금매수','dmst_stex_tp':'KRX'}]
  result=self.run_phase();self.assertEqual(result['status'],'complete',result)
  event=json.loads((self.state_dir/'maintenance-readonly-evidence'/result['phase_id']/'kt00007-01.json').read_text())
  self.assertEqual(event['day'],'20261103');self.assertEqual(event['response_body']['acnt_ord_cntr_prps_dtl'][0]['ord_no'],'123456')
  current=json.loads((self.state_dir/'maintenance-readonly-evidence'/result['phase_id']/'kt00009-01.json').read_text())
  self.assertEqual(current['day'],'20261104');self.assertEqual(current['response_body']['acnt_ord_cntr_prst_array'][0]['ord_no'],'123456')
  self.assertNotEqual(current['query_id'],event['query_id'])
  self.assertEqual(m.MaintenanceReadOnlyReconciliation._validate_history_pages([event],'20261103',self.now,self.now),self.history_rows)
  with self.tracker._db() as db:
   self.assertEqual(db.execute('SELECT COUNT(*) FROM fills').fetchone()[0],0)
   self.assertEqual(db.execute('SELECT COUNT(*) FROM orders').fetchone()[0],0)
  self.assertFalse(result['settlements_written']);self.tracker.record_paper_expired_settlements.assert_not_called()
 def test_exported_history_validator_independently_rejects_current_or_future_target(self):
  result=self.run_phase();self.assertEqual(result['status'],'complete')
  event=json.loads((self.state_dir/'maintenance-readonly-evidence'/result['phase_id']/'kt00007-01.json').read_text())
  for target in ('20261104','20261105','20260230','20261003'):
   with self.subTest(day=target):
    changed=deepcopy(event);changed['day']=target;changed['request_body']['ord_dt']=target
    with self.assertRaisesRegex(m.MaintenanceReadUnavailable,'maintenance_history_day_invalid'):
     m.MaintenanceReadOnlyReconciliation._validate_history_pages([changed],target,self.now,self.now)
 def test_current_kt9_query_id_cannot_reuse_history_query_id(self):
  original=self.phase.orders;history_id=[]
  history_fetch=self.phase.history
  def historical(session,*,observer,**kwargs):
   def save(event):history_id.append(event['query_id']);observer(event)
   return history_fetch(session,observer=save,**kwargs)
  def orders(session,*,observer,**kwargs):
   def save(event):event=deepcopy(event);event['query_id']=history_id[0];observer(event)
   return original(session,observer=save,**kwargs)
  self.phase.history=historical;self.phase.orders=orders
  result=self.run_phase();self.assertEqual(result['status'],'failed');self.assertEqual(result['reason'],'maintenance_query_identity_reused')
 def test_history_each_page_queue_guard_blocks_marker_session_stream_or_deadline_change(self):
  for change in ('marker','session','stream','deadline'):
   with self.subTest(change=change):
    self.initialize();self.history_multi=True
    def hook(api):
     if api!='kt00007' or len(self.requests)!=1:return
     if change=='marker':self.marker.unlink()
     elif change=='session':self.session.mode='real'
     elif change=='stream':self.stream['ingress_sequence']+=1
     else:self.now+=timedelta(seconds=61);self.mono+=61
    self.transport_hook=hook;result=self.run_phase()
    self.assertEqual(result['status'],'failed');self.assertEqual([a for a,_ in self.requests],['kt00007'])
    self.assertTrue(self.phase.blocked)
 def test_empty_pending_with_prior_day_tag_is_not_a_current_query_pair(self):
  original=self.phase.pending
  def pending(session,*,observer,**kwargs):
   def save(event):event=deepcopy(event);event['day']='20261103';observer(event)
   return original(session,observer=save,**kwargs)
  self.phase.pending=pending;result=self.run_phase();self.assertEqual(result['status'],'failed')
  self.assertTrue(self.phase.blocked);self.assertFalse(result['settlements_written'])
 def test_explicit_marker_crossing_midnight_cannot_reuse_cache_or_release(self):
  self.now=datetime(2026,11,4,23,59,50,tzinfo=KST);self.renew_marker();self.set_days()
  result=self.run_phase();self.assertEqual(result['status'],'complete',result);count=len(self.requests)
  self.now+=timedelta(seconds=12);self.mono+=12
  self.assertFalse(self.phase.release_if_verified(self.session,self.tracker));self.assertEqual(self.run_phase()['status'],'failed')
  self.assertEqual(len(self.requests),count);self.assertTrue(self.phase.blocked)
 def test_legacy_marker_crossing_midnight_starts_real_new_day_batch(self):
  self.now=datetime(2026,11,4,23,59,50,tzinfo=KST);self.renew_marker()
  first=self.run_phase();self.assertEqual(first['status'],'complete',first)
  self.now+=timedelta(seconds=12);self.mono+=12;self.assertFalse(self.phase.release_if_verified(self.session,self.tracker))
  second=self.run_phase();self.assertEqual(second['status'],'complete',second)
  self.assertNotEqual(first['phase_id'],second['phase_id']);self.assertEqual(second['current_day'],'20261105')
  self.assertEqual(len(self.requests),8)
 def test_twelve_second_final_scan_keeps_ninety_second_limit_and_original_query_times(self):
  first=self.run_phase();self.assertEqual(first['status'],'complete');self.now+=timedelta(seconds=12);self.mono+=12
  cached=self.run_phase();self.assertEqual(cached['phase_id'],first['phase_id']);self.assertEqual(cached['completed_at'],first['completed_at'])
  self.marker.unlink();self.now+=timedelta(seconds=67);self.mono+=67;original=m._watermark
  def scan(tracker):
   result=original(tracker);self.now+=timedelta(seconds=12);self.mono+=12;return result
  with patch.object(m,'_watermark',side_effect=scan):self.assertFalse(self.phase.release_if_verified(self.session,self.tracker))
  self.assertTrue(self.phase.blocked)


if __name__=='__main__':unittest.main()
