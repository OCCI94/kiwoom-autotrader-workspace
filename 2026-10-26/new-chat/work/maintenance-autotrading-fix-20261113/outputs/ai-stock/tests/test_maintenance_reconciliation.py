"""Real shared-reader producer through fake queued transport, private ledger."""
from datetime import datetime,timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,patch
import json,os,tempfile,unittest
from kiwoom_autotrader.api import account_report,executions
from kiwoom_autotrader.api.session import BrokerSession,IssuedToken,KST
from kiwoom_autotrader.trading.execution_log import ExecutionTracker
from kiwoom_autotrader.trading import maintenance_reconciliation as m
from kiwoom_autotrader.telegram.receiver_health import maintenance_status
from kiwoom_autotrader.telegram.listener import _maintenance_order_guard
from kiwoom_autotrader.telegram import listener
from tests import test_account_report as account_fixture

class PhaseFixture:
 def initialize(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name).resolve()
  self.state_dir=self.root/'telegram';self.state_dir.mkdir();self.now=datetime(2026,11,3,22,30,tzinfo=KST);self.mono=0.0
  self.session=BrokerSession(now=lambda:self.now);self.session.mode='paper';self.session._issued=IssuedToken('maintenance-private-fixture-token','20300101000000')
  self.stream={'ready':True,'error':None,'gap_count':0,'observations':1,'ingress_sequence':0,'engine_ingress_sequence':0,'inflight_callbacks':0,'engine_pending_callbacks':0}
  self.session.execution_recorder=SimpleNamespace(status=lambda:dict(self.stream));self.tracker=ExecutionTracker(log_dir=self.root/'ledger',now=lambda:self.now)
  with self.tracker._db():pass
  self.session.paper_automation_reconcile=SimpleNamespace(_session=self.session,_tracker=self.tracker)
  self.marker=self.state_dir/'maintenance-quiet.lock';self.renew_marker()
  self.requests=[];self.transport_hook=None;self.partial=False
  self.tracker._ingest=Mock(side_effect=AssertionError('ingest forbidden'));self.tracker._holdings=Mock(side_effect=AssertionError('holdings projection forbidden'))
  self.tracker.record_paper_expired_settlements=Mock(side_effect=AssertionError('settlement forbidden'))
  self.phase=m.MaintenanceReadOnlyReconciliation(now=lambda:self.now,monotonic=lambda:self.mono)
  self.session.maintenance_readonly_phase=self.phase
 def renew_marker(self):
  self.marker.write_text(json.dumps({'schema':m.SCHEMA,'purpose':m.PURPOSE,'marker_id':'a'*32,'owner_process_pid':12345,
   'created_at':self.now.isoformat(),'expires_at':(self.now+timedelta(seconds=180)).isoformat(),
   'maintenance_module_sha256':m.module_sha256(),'reconciliation_required':True}));self.marker.chmod(0o600)
  os.utime(self.marker,(self.now.timestamp(),self.now.timestamp()))
 def transport(self,opener,request,**options):
  api=request.get_header('Api-id')
  if self.transport_hook:self.transport_hook(api)
  options['before_send']();self.requests.append((api,json.loads(request.data)))
  if api=='kt00009':body={'return_code':0,'acnt_ord_cntr_prst_array':[]}
  elif api=='ka10075':body={'return_code':0,'oso':[]}
  elif api=='kt00001':body=account_fixture.cash_payload()
  else:body=account_fixture.holdings_payload(rows=[])
  headers={'api-id':api,'cont-yn':'N'}
  if self.partial and api=='kt00018':headers.update({'cont-yn':'Y','next-key':'NEXT'})
  if self.partial and api=='kt00018' and request.get_header('Cont-yn')=='Y':raise OSError('private provider detail')
  return account_fixture.Response(body,headers=headers)
 def run_phase(self):
  with patch.object(account_report,'queued_open',side_effect=self.transport),patch.object(executions,'queued_open',side_effect=self.transport),patch.object(account_report.time,'sleep'),patch.object(account_report,'datetime',wraps=datetime) as ac,patch.object(executions,'datetime',wraps=datetime) as ex:
   ac.now.side_effect=lambda *a,**k:self.now;ex.now.side_effect=lambda *a,**k:self.now
   return self.phase.tick(self.session,self.tracker,self.state_dir)

class MaintenancePhaseTests(PhaseFixture,unittest.TestCase):
 def setUp(self):self.initialize()
 def test_exact_read_order_real_producers_complete_hashes_and_no_financial_or_order_writes(self):
  before=m._watermark(self.tracker);result=self.run_phase()
  self.assertEqual(result['status'],'complete',result);self.assertEqual([r[0] for r in self.requests],['kt00009','kt00001','kt00018','ka10075'])
  self.assertEqual(self.requests[0][1]['qry_tp'],'0');self.assertEqual(self.requests[-1][1]['stex_tp'],'0')
  self.assertEqual(result['financial_watermark'],before);self.assertEqual(m._watermark(self.tracker),before)
  self.assertEqual(set(result['queries']),{'kt00009','kt00001','kt00018','ka10075'})
  self.assertTrue(all(v['pages']==1 for v in result['queries'].values()));self.assertEqual(result['orders_sent'],0)
  self.assertEqual(result['account']['account_snapshot_sha256'],__import__('hashlib').sha256(m._balance(self.tracker,'20261103')['payload_json'].encode()).hexdigest())
  self.assertTrue((self.state_dir/'maintenance-readonly-evidence'/result['phase_id']/'complete.json').is_file())
  raw=json.dumps(result);self.assertNotIn(self.session.token,raw);self.assertNotIn(account_fixture.PRIVATE,raw)
  self.tracker._ingest.assert_not_called();self.tracker._holdings.assert_not_called();self.tracker.record_paper_expired_settlements.assert_not_called()
 def test_partial_account_failure_preserves_pages_keeps_blocked_and_never_releases(self):
  self.partial=True;result=self.run_phase();self.assertEqual(result['status'],'failed');self.assertTrue(self.phase.blocked)
  self.assertNotIn('ka10075',[a for a,_ in self.requests]);self.assertNotIn('private',json.dumps(result))
  self.assertGreaterEqual(len(list((self.state_dir/'maintenance-readonly-evidence'/result['phase_id']).glob('*.json'))),3)
  self.marker.unlink();self.assertFalse(self.phase.release_if_verified(self.session,self.tracker))
  with self.assertRaises(ValueError):_maintenance_order_guard(self.state_dir,self.phase)
 def test_queue_delay_marker_removal_or_stream_session_change_rejects_before_transport(self):
  for change in ('marker','stream','session','deadline'):
   with self.subTest(change=change):
    self.initialize()
    def hook(api):
     if change=='marker':self.marker.unlink()
     elif change=='stream':self.stream['ingress_sequence']+=1
     elif change=='session':self.session._issued=IssuedToken('changed-private-fixture-token','20300101000000')
     else:self.now+=timedelta(seconds=61);self.mono+=61
    self.transport_hook=hook;result=self.run_phase();self.assertEqual(result['status'],'failed');self.assertEqual(self.requests,[])
    self.assertTrue(self.phase.blocked)
 def test_twelve_second_scan_does_not_extend_query_clock_and_refresh_is_real(self):
  first=self.run_phase();self.assertEqual(first['status'],'complete');count=len(self.requests)
  self.now+=timedelta(seconds=12);self.mono+=12;cached=self.run_phase()
  self.assertEqual(first['phase_id'],cached['phase_id']);self.assertEqual(first['completed_at'],cached['completed_at']);self.assertEqual(len(self.requests),count)
  self.now+=timedelta(seconds=19);self.mono+=19;second=self.run_phase()
  self.assertEqual(second['status'],'complete');self.assertNotEqual(first['phase_id'],second['phase_id']);self.assertEqual(len(self.requests),count+4)
  self.marker.unlink();self.assertTrue(self.phase.release_if_verified(self.session,self.tracker))
 def test_expired_special_marker_stays_paused_and_stale_phase_cannot_release(self):
  result=self.run_phase();self.assertEqual(result['status'],'complete');self.now+=timedelta(seconds=181);self.mono+=181
  self.assertTrue(maintenance_status(self.state_dir,now=self.now)['paused']);count=len(self.requests)
  self.assertEqual(self.run_phase()['status'],'failed');self.assertEqual(len(self.requests),count)
  self.marker.unlink();self.assertFalse(self.phase.release_if_verified(self.session,self.tracker))
 def test_not_ready_waits_without_api_or_clock_relabel(self):
  self.stream['ready']=False;self.assertEqual(self.run_phase()['status'],'waiting_stream');self.assertEqual(self.requests,[])
  self.stream['ready']=True;self.assertEqual(self.run_phase()['status'],'complete')
 def test_actual_phase_save_load_restart_discards_complete_or_failed_readiness(self):
  for failed in (False,True):
   with self.subTest(failed=failed):
    self.initialize();self.partial=failed;observed=self.run_phase()
    self.assertEqual(observed['status'],'failed' if failed else 'complete')
    config=SimpleNamespace(bot_id=123,chat_id=456);saved=listener._new_state(config,1000)
    saved.update(next_offset=17,poll_count=3,maintenance_reconciliation=observed,
     maintenance_paused=True,execution_recording={'ready':True})
    path=self.state_dir/'state.json';listener._save_state(path,saved)
    restored=listener._load_state(path,config,1001)
    self.assertEqual(restored['next_offset'],17);self.assertEqual(restored['poll_count'],3)
    self.assertEqual(restored['started_at'],1001)
    for field in ('maintenance_reconciliation','maintenance_paused','execution_recording'):
     self.assertNotIn(field,restored)
    restarted=m.MaintenanceReadOnlyReconciliation(now=lambda:self.now,monotonic=lambda:self.mono)
    self.assertEqual(restarted.status()['status'],'inactive');self.assertFalse(restarted.blocked)
    restarted.latch(self.state_dir);self.assertTrue(restarted.blocked)
    self.assertEqual(restarted.status()['status'],'waiting_stream')
    self.assertFalse(restarted.release_if_verified(self.session,self.tracker))
 def test_latch_before_first_query_blocks_marker_removal_or_expiry_fallthrough(self):
  self.assertTrue(self.phase.latch(self.state_dir));self.marker.unlink()
  self.assertFalse(self.phase.release_if_verified(self.session,self.tracker))
  with self.assertRaises(ValueError):_maintenance_order_guard(self.state_dir,self.phase)
  self.assertEqual(self.requests,[])
 def test_twelve_second_release_scan_cannot_age_evidence_past_ninety_seconds(self):
  self.assertEqual(self.run_phase()['status'],'complete');self.marker.unlink()
  self.now+=timedelta(seconds=79);self.mono+=79;real_watermark=m._watermark
  def scan(tracker):
   result=real_watermark(tracker);self.now+=timedelta(seconds=12);self.mono+=12;return result
  with patch.object(m,'_watermark',side_effect=scan):
   self.assertFalse(self.phase.release_if_verified(self.session,self.tracker))
  self.assertTrue(self.phase.blocked)
 def test_each_later_api_queue_rechecks_marker_before_actual_send(self):
  for target in ('kt00001','kt00018','ka10075'):
   with self.subTest(api=target):
    self.initialize()
    def hook(api):
     if api==target:self.marker.unlink()
    self.transport_hook=hook;result=self.run_phase()
    self.assertEqual(result['status'],'failed');self.assertNotIn(target,[a for a,_ in self.requests])
    self.assertTrue(self.phase.blocked)
 def test_receive_latches_before_login_and_marker_removal_skips_all_normal_work(self):
  self.session.websocket_engine=Mock();self.session.websocket_engine.snapshot.return_value={}
  self.tracker.tick=Mock(side_effect=AssertionError('ordinary ticker forbidden'))
  config=SimpleNamespace(bot_token='private-fixture',bot_id=123,bot_username='fixture')
  strategy=Mock();strategy.status.return_value={}
  supervisor=Mock();supervisor.status.return_value={}
  scheduler=Mock();legacy=Mock();legacy.status.return_value={}
  client=Mock();client.get_me.return_value={'id':123,'username':'fixture'};client.get_webhook_info.return_value={'url':''}
  def login(*args):
   self.marker.unlink();return SimpleNamespace(message='private fixture login result')
  with patch.object(self.session,'login',side_effect=login),patch.object(listener,'terminal_report'),patch.object(listener,'TelegramClient',return_value=client),patch.object(listener,'_LegacyRecoverySupervisor',return_value=legacy),patch.object(listener,'ReceiverStabilityAudit') as audit,patch.object(listener,'initialize_control_audit'),patch.object(listener,'_send_notification'),patch.object(listener.time,'sleep'):
   audit.return_value.pulse.return_value={}
   state={};self.assertEqual(listener._receive(self.state_dir,state,config,1,self.session,scheduler,strategy,self.tracker,strategy,strategy,supervisor),0)
  self.assertTrue(self.phase.blocked);self.assertTrue(state['maintenance_paused'])
  self.tracker.tick.assert_not_called();scheduler.claim_due.assert_not_called();client.get_updates.assert_not_called()
  supervisor.tick.assert_not_called();legacy.tick.assert_not_called();strategy.tick.assert_not_called()
