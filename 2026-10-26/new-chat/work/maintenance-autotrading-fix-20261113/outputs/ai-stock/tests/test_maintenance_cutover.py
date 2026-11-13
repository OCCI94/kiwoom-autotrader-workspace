"""Actual private-ledger actors and listener share one immutable cutover lock."""
from copy import deepcopy
from datetime import timedelta
import hashlib,json,os,unittest
from types import SimpleNamespace
from unittest.mock import Mock,patch
from kiwoom_autotrader.trading import maintenance_reconciliation as m
from kiwoom_autotrader.trading import maintenance_expiry_resolution as e
from kiwoom_autotrader.telegram import listener as l
from tests.test_maintenance_expiry_resolution import ResolutionFixture

class CutoverTests(ResolutionFixture,unittest.TestCase):
 def setUp(self):
  self.initialize();self.lock=self.state_dir/m.CUTOVER_LOCK_NAME
  self.lock.write_text(json.dumps({'schema':m.CUTOVER_LOCK_SCHEMA,'nonce':'a'*32}));self.lock.chmod(0o600)
  s=self.lock.stat();self.binding={'schema':m.CUTOVER_LOCK_SCHEMA,'dev':s.st_dev,'ino':s.st_ino,'size':s.st_size,'sha256':hashlib.sha256(self.lock.read_bytes()).hexdigest()}
  value=json.loads(self.marker.read_text());value['cutover_lock']=self.binding;self.marker.write_text(json.dumps(value))
 def coordinated(self,publish=None):
  return l._maintenance_reconciliation_tick(self.session,self.tracker,self.state_dir,self.phase,self.resolver,publish)
 def completed(self):
  self.prepare();result=self.coordinated();self.assertEqual(result['status'],'complete',result)
  self.assertEqual(result['cutover_lock'],self.binding);self.assertEqual(self.result['cutover_lock'],self.binding)
  self.assertEqual(self.certificates(),9);return result
 def release(self):return l._maintenance_release_if_verified(self.session,self.tracker,self.state_dir,self.phase,self.resolver)
 def test_real_tick_and_state_publication_exclude_cutover_then_both_release(self):
  self.prepare();original=self.resolver._snapshot;seen=[]
  def snapshot(*a):
   with m.cutover_guard(self.state_dir,self.binding)as acquired:seen.append(('snapshot',acquired))
   return original(*a)
  def publish():
   with m.cutover_guard(self.state_dir,self.binding)as acquired:seen.append(('publish',acquired))
   self.assertEqual(self.resolver.status()['status'],'complete')
  before=m._watermark(self.tracker)
  with patch.object(self.resolver,'_snapshot',side_effect=snapshot):result=self.coordinated(publish)
  self.assertEqual(result['status'],'complete');self.assertTrue(seen);self.assertTrue(all(not held for _,held in seen))
  self.assertEqual(m._watermark(self.tracker),before)
  with m.cutover_guard(self.state_dir,self.binding)as acquired:
   self.assertTrue(acquired);self.marker.unlink()
   with patch.object(self.resolver,'tick',side_effect=AssertionError('tick must wait')):
    self.assertIsNone(self.coordinated())
   self.assertFalse(self.release())
  self.assertTrue(self.release());self.assertFalse(self.phase.blocked);self.assertFalse(self.resolver.blocked)
  l._maintenance_order_guard(self.state_dir,self.phase,self.resolver)
 def test_stale_paused_branch_after_tool_removal_does_not_overwrite_complete(self):
  result=self.completed();self.now+=timedelta(seconds=12)
  with m.cutover_guard(self.state_dir,self.binding):self.marker.unlink()
  with patch.object(self.resolver,'tick',side_effect=AssertionError('stale branch must not tick')):
   self.assertIsNone(self.coordinated())
  self.assertEqual(self.resolver.status(),result);self.assertTrue(self.release())
 def test_wait_does_not_extend_original_ninety_seconds_or_restore_partial_gate(self):
  self.completed()
  with m.cutover_guard(self.state_dir,self.binding):
   self.marker.unlink();self.now+=timedelta(seconds=79);self.assertFalse(self.release())
  original=m._watermark
  def scan(*a):self.now+=timedelta(seconds=12);return original(*a)
  with patch.object(m,'_watermark',side_effect=scan):self.assertFalse(self.release())
  self.assertTrue(self.phase.blocked);self.assertTrue(self.resolver.blocked)
 def test_bound_marker_field_deletion_or_change_is_sticky_no_legacy_downgrade(self):
  self.completed();saved=json.loads(self.marker.read_text());saved.pop('cutover_lock');self.marker.write_text(json.dumps(saved))
  with self.assertRaisesRegex(ValueError,'binding_changed'):self.coordinated()
  self.marker.unlink()
  with self.assertRaisesRegex(ValueError,'binding_latched_error'):self.release()
  self.assertTrue(self.phase.blocked);self.assertTrue(self.resolver.blocked)
 def test_binding_altered_rehashed_marker_cannot_replace_lock_authority(self):
  self.completed();value=json.loads(self.marker.read_text());value['cutover_lock']['sha256']='b'*64;self.marker.write_text(json.dumps(value))
  with self.assertRaisesRegex(ValueError,'binding_changed'):self.coordinated()
 def test_unbound_legacy_empty_and_plaintext_markers_keep_existing_contract(self):
  for raw in (b'',b'approved receiver-only maintenance note\n',b'{legacy malformed note'):
   self.marker.write_bytes(raw)
   actor=m.MaintenanceReadOnlyReconciliation(now=lambda:self.now)
   self.assertIsNone(m.remember_cutover_binding(actor,self.state_dir))
   self.assertNotIn('_cutover_lock_error',vars(actor))
  self.marker.write_bytes(b'{"cutover_lock":invalid')
  with self.assertRaisesRegex(ValueError,'binding_changed'):m.remember_cutover_binding(actor,self.state_dir)
 def test_bound_actor_cannot_downgrade_to_legacy_empty_marker(self):
  self.completed();self.marker.write_bytes(b'')
  with self.assertRaisesRegex(ValueError,'binding_changed'):self.coordinated()
 def test_lock_content_mode_owner_and_inode_changes_fail_closed(self):
  for kind in ('content','mode','owner','inode'):
   with self.subTest(kind=kind):
    original=self.lock.read_bytes()
    if kind=='content':self.lock.write_bytes(original+b' ')
    elif kind=='mode':self.lock.chmod(0o640)
    elif kind=='inode':
     old=self.lock.with_suffix('.old');self.lock.rename(old);self.lock.write_bytes(original);self.lock.chmod(0o600)
    with patch.object(m.os,'getuid',return_value=os.getuid()+1) if kind=='owner' else patch.object(m,'_time',wraps=m._time):
     with self.assertRaises(m.MaintenanceReadUnavailable):
      with m.cutover_guard(self.state_dir,self.binding):pass
    if kind=='inode':self.lock.unlink();old.rename(self.lock)
    else:self.lock.write_bytes(original);self.lock.chmod(0o600)
 def test_symlink_replacement_to_same_held_inode_fails_at_unlock(self):
  with self.assertRaisesRegex(m.MaintenanceReadUnavailable,'symlink'):
   with m.cutover_guard(self.state_dir,self.binding):
    saved=self.lock.with_suffix('.held');self.lock.rename(saved);self.lock.symlink_to(saved)
 def test_uncoordinated_removal_during_tick_still_fails_never_restores_old_complete(self):
  self.completed();original=self.resolver._snapshot;count=[0]
  def snapshot(*a):
   result=original(*a);count[0]+=1
   if count[0]==1:self.marker.unlink()
   return result
  with patch.object(self.resolver,'_snapshot',side_effect=snapshot):result=self.coordinated()
  self.assertEqual(result['status'],'failed');self.assertEqual(result['failure_stage'],'final_authorization')
  self.assertEqual(result['exception_type'],'FileNotFoundError');self.assertNotIn(str(self.state_dir),json.dumps(result))
  self.assertFalse(self.release());self.assertTrue(self.resolver.blocked)
 def test_failed_actor_is_not_restored_by_marker_absence(self):
  self.completed();self.resolver._status={'schema':e.SCHEMA,'status':'failed','reason':'fixture_failure'}
  self.marker.unlink();self.assertIsNone(self.coordinated());self.assertFalse(self.release())
 def test_phase2_repeat_only_changes_published_tick_count_and_not_certificates(self):
  first=self.completed();self.now+=timedelta(seconds=1);second=self.coordinated()
  self.assertEqual(second['status'],'complete');self.assertEqual(second['expiry_certificates_appended_this_tick'],0)
  self.assertEqual(first['resolutions'],second['resolutions']);self.assertEqual(self.certificates(),9)
 def test_real_listener_releases_both_actors_before_actual_fourteen_arm_monitor_initialization(self):
  from kiwoom_autotrader.trading.paper_experiment_monitor import PaperExperimentMonitor
  self.completed();self.marker.unlink();self.session.maintenance_expiry_resolution=self.resolver
  self.session.websocket_engine=Mock();self.session.websocket_engine.snapshot.return_value={}
  self.tracker.tick=Mock();self.tracker.seconds_until_next=Mock(return_value=None)
  monitor=PaperExperimentMonitor(settings_loader=lambda:{'paper_experiment_enabled':True,'paper_experiment_s5_enabled':True},
   now=lambda:self.now,store_path=self.state_dir/'normal-experiment.sqlite3',provider_factory=lambda session:SimpleNamespace(),
   calendar_reader=lambda now:None)
  def normal_tick(session,tracker,notify,stop):
   self.assertFalse(self.phase.blocked);self.assertFalse(self.resolver.blocked)
   l._maintenance_order_guard(self.state_dir,self.phase,self.resolver)
   monitor.tick(session,notify);return True
  supervisor=SimpleNamespace(tick=normal_tick,seconds_until_next=lambda:None,status=lambda:{'monitor':monitor.status()})
  strategy=Mock();strategy.status.return_value={};strategy.seconds_until_next.return_value=None
  scheduler=Mock();scheduler.claim_due.return_value=None;scheduler.seconds_until_next.return_value=None
  legacy=Mock();legacy.status.return_value={};client=Mock()
  client.get_me.return_value={'id':123,'username':'fixture'};client.get_webhook_info.return_value={'url':''};client.get_updates.return_value=[]
  config=SimpleNamespace(bot_token='private-fixture',bot_id=123,bot_username='fixture')
  state={'poll_count':0,'next_offset':None,'started_at':self.now.isoformat()}
  before=m._watermark(self.tracker)
  with patch.object(self.session,'login',return_value=SimpleNamespace(message='fixture')),patch.object(self.session,'renew_if_due',return_value=None), \
    patch.object(self.session,'seconds_until_renewal',return_value=None),patch.object(l,'terminal_report'),patch.object(l,'TelegramClient',return_value=client), \
    patch.object(l,'_LegacyRecoverySupervisor',return_value=legacy),patch.object(l,'ReceiverStabilityAudit')as audit, \
    patch.object(l,'initialize_control_audit'),patch.object(l,'_send_notification'),patch.object(l.time,'sleep'),patch.object(l,'PreopenStreamCheck')as preopen:
   audit.return_value.pulse.return_value={};preopen.return_value.tick.return_value={}
   l._receive(self.state_dir,state,config,2,self.session,scheduler,strategy,self.tracker,strategy,strategy,supervisor)
  actual=state['automation_status']['paper_experiment']['monitor']
  expected={'EXP_R','EXP_A','EXP_B','EXP_B_TP','EXP_E','EXP_C','EXP_D','EXP_ETF_B','EXP_GDR','EXP_LDR','EXP_ORB_A','EXP_ORB_B','EXP_VSS','S5_steady_uptrend_vwap'}
  self.assertEqual(set(actual['active_arms']),expected);self.assertEqual(actual['s5_integration']['status'],'ACTIVE')
  self.assertNotIn('maintenance_paused',state);self.assertEqual(m._watermark(self.tracker),before);self.assertEqual(self.certificates(),9)

if __name__=='__main__':unittest.main()
