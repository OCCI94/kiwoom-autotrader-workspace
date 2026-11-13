"""Real production ledger + real history readers; private fixtures only."""
from copy import deepcopy
from datetime import timedelta
from pathlib import Path
import hashlib,json,unittest
from unittest.mock import patch
from kiwoom_autotrader.trading import maintenance_expiry_resolution as e
from kiwoom_autotrader.trading import maintenance_reconciliation as m
from kiwoom_autotrader.trading.expired_order_certificates import TABLE
from kiwoom_autotrader.trading.paper_experiment_store import ExperimentStore
from tests.test_maintenance_reconciliation_cross_day import CrossDayFixture
from tests.test_cancellation_settlement_production_labels import CASES

class ResolutionFixture(CrossDayFixture):
 def initialize(self):
  super().initialize();ExperimentStore(self.state_dir/'paper-experiment.sqlite3')
  with self.tracker._db() as db:
   for case in CASES:
    o=case['order'];key='fixture-'+o['ord_no']
    db.execute('INSERT INTO orders VALUES (?,?,?,?,?,?,?,?,?)',(self.tracker.scope,o['day'],'paper',o['ord_no'],o['symbol'],'buy','EXP_D',o['quantity'],o['registered_at']))
    db.execute('INSERT INTO intents VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',(self.tracker.scope,key,o['day'],'paper','buy',o['symbol'],o['quantity'],'EXP_D',o['created_at'],o['ord_no'],'accepted',None))
    db.execute('INSERT INTO order_decisions VALUES (?,?,?,?,?,?,?)',(self.tracker.scope,key,o['created_at'],o['order_price'],'limit',None,None))
    self.history_rows.append({'ord_no':o['ord_no'],'stk_cd':o['symbol'],'io_tp_nm':'현금매수','dmst_stex_tp':'KRX',
      'ord_qty':str(o['quantity']),'cntr_qty':'0','ord_remnq':'0'})
  for case in CASES:
   for ws in case['ws']:
    self.tracker.record_broker_observation({'schema_version':1,'source_api':'00','source_kind':'broker_observation',
     'observed_at':ws['observed_at'],'day':e.INCIDENT_DAY,'mode':'paper','event_kind':'execution_stream','values':ws['values']})
  self.resolver=e.MaintenanceExpiryResolution(now=lambda:self.now)
  self.result=None
 def prepare(self):
  self.result=self.run_phase();self.assertEqual(self.result['status'],'complete',self.result)
  marker=json.loads(self.marker.read_text());marker['marker_id']='b'*32
  marker['prior_expiry_resolution']={'schema':e.AUTH_SCHEMA,'readonly_phase_id':self.result['phase_id'],
   'readonly_phase_sha256':self.result['evidence_sha256'],'parent_marker_sha256':self.result['marker_sha256'],
   'orders':[{'day':e.INCIDENT_DAY,'ord_no':n} for n in e.ORDER_NUMBERS],'expiry_module_sha256':e.module_sha256()}
  self.marker.write_text(json.dumps(marker));self.marker.chmod(0o600)
 def resolve(self):return self.resolver.tick(self.session,self.tracker,self.state_dir,self.phase)
 def certificates(self):
  with self.tracker._db() as db:return db.execute('SELECT COUNT(*) FROM '+TABLE).fetchone()[0]
 def assert_closed(self):
  result=self.resolve();self.assertEqual(result['status'],'failed',result);self.assertTrue(self.resolver.blocked);self.assertEqual(self.certificates(),0)
  return result

class MaintenanceExpiryTests(ResolutionFixture,unittest.TestCase):
 def setUp(self):self.initialize()
 def test_nine_actual_quantities_expire_append_only_without_cancellation_publication_or_new_orders(self):
  before=m._watermark(self.tracker);self.prepare();result=self.resolve()
  self.assertEqual(result['status'],'complete',result);self.assertEqual(self.certificates(),9)
  self.assertEqual(m._watermark(self.tracker),before);self.assertEqual(result['remaining_registered_pending'],0)
  self.assertEqual(result['principal_reservations_released'],9);self.assertEqual(result['orders_sent'],0)
  self.assertEqual(result['expiry_certificates_appended_this_tick'],9);self.assertEqual(result['verified_released_order_count'],9)
  self.assertEqual(result['cancellation_publications_written'],0)
  self.assertEqual([r['ord_no'] for r in result['resolutions']],list(e.ORDER_NUMBERS))
  for row in result['resolutions']:
   self.assertEqual(row['expired_quantity'],row['quantity']);self.assertEqual(row['filled_quantity'],0);self.assertEqual(row['cancelled_quantity'],0)
  with self.tracker._db() as db:
   self.assertEqual(db.execute('SELECT COUNT(*) FROM fills').fetchone()[0],0)
   table=db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='buy_ttl_cancellation_settlements'").fetchone()
   self.assertTrue(table is None or db.execute('SELECT COUNT(*) FROM buy_ttl_cancellation_settlements').fetchone()[0]==0)
   self.assertEqual(len(self.tracker._pending_order_rows(db,'paper')),0)
  self.assertTrue(self.resolver.blocked);self.assertFalse(self.resolver.release_if_verified(self.session,self.tracker))
  duplicate=self.resolve();self.assertEqual(duplicate['status'],'complete');self.assertEqual(self.certificates(),9)
  self.assertEqual(duplicate['expiry_certificates_appended_this_tick'],0);self.assertEqual(duplicate['verified_released_order_count'],9)
  self.marker.unlink();self.assertTrue(self.resolver.release_if_verified(self.session,self.tracker))
 def test_same_day_default_and_sticky_latch_cannot_fall_through_after_marker_removed(self):
  plain=e.MaintenanceExpiryResolution(now=lambda:self.now);self.assertFalse(plain.blocked);plain.assert_orders_allowed()
  self.assertTrue(plain.latch(self.state_dir));self.marker.unlink();self.assertTrue(plain.latch(self.state_dir))
  self.assertFalse(plain.release_if_verified(self.session,self.tracker))
  with self.assertRaisesRegex(ValueError,'orders_blocked'):plain.assert_orders_allowed()
 def test_stage_two_requested_is_sticky_after_plain_invalid_or_removed_marker(self):
  self.prepare();self.assertTrue(self.resolver.requested(self.state_dir));self.resolver.latch(self.state_dir)
  marker=json.loads(self.marker.read_text());marker.pop('prior_expiry_resolution');self.marker.write_text(json.dumps(marker))
  self.assertTrue(self.resolver.requested(self.state_dir));self.assert_closed()
  self.marker.write_text('{invalid');self.assertTrue(self.resolver.requested(self.state_dir));self.assert_closed()
  self.marker.unlink();self.assertTrue(self.resolver.requested(self.state_dir));self.assert_closed()
 def test_dangling_marker_symlink_or_nonprivate_marker_never_releases(self):
  self.prepare();self.assertEqual(self.resolve()['status'],'complete');self.marker.unlink();self.marker.symlink_to(self.root/'absent')
  self.assertFalse(self.resolver.release_if_verified(self.session,self.tracker));self.assertTrue(self.resolver.blocked)
  self.initialize();self.prepare();self.marker.chmod(0o644);self.assert_closed()
 def test_bad_parent_phase_module_and_exact_order_manifest_never_append(self):
  for field in ('parent_marker_sha256','readonly_phase_sha256','expiry_module_sha256','orders'):
   with self.subTest(field=field):
    self.initialize();self.prepare();marker=json.loads(self.marker.read_text());auth=marker['prior_expiry_resolution']
    auth[field]=auth[field][:-1] if field=='orders' else '0'*64
    self.marker.write_text(json.dumps(marker));self.assertTrue(self.resolver.requested(self.state_dir));self.assert_closed()
 def test_stale_clock_and_cross_midnight_and_future_phase_are_rejected(self):
  self.prepare();self.now+=timedelta(seconds=91);self.assert_closed()
  self.initialize();self.prepare();self.now+=timedelta(days=1);self.assert_closed()
  self.initialize();self.prepare();self.now-=timedelta(seconds=1);self.assert_closed()
 def test_partial_missing_failed_or_changed_latest_history_never_falls_back(self):
  for kind in ('partial','missing','wrong_quantity','error'):
   with self.subTest(kind=kind):
    self.initialize();self.prepare();path=self.state_dir/'maintenance-readonly-evidence'/self.result['phase_id']/'kt00007-01.json'
    event=json.loads(path.read_text());event['query_id']='d'*32
    if kind=='partial':event['continuation'].update(response_cont_yn='Y',next_key_present=True)
    elif kind=='missing':event['response_body']['acnt_ord_cntr_prps_dtl']=[]
    elif kind=='wrong_quantity':event['response_body']['acnt_ord_cntr_prps_dtl'][0]['ord_qty']='999'
    else:event.update(status='transport_error',http_status=None,response_body={})
    self.tracker.record_broker_observation(event);self.assert_closed()
 def test_current_pending_not_empty_or_reused_today_original_or_child_is_ambiguous(self):
  for kind in ('pending','rest-original','rest-child','00-original','00-child'):
   with self.subTest(kind=kind):
    self.initialize();self.prepare();number=e.ORDER_NUMBERS[0] if kind.endswith('original') else CASES[0]['child']
    if kind.startswith('00'):
     self.tracker.record_broker_observation({'schema_version':1,'source_api':'00','source_kind':'broker_observation','observed_at':self.now.isoformat(),
       'day':'20261104','mode':'paper','event_kind':'execution_stream','values':{'9203':number,'904':'0000000'}})
    else:
     api='ka10075' if kind=='pending' else 'kt00009';path=self.state_dir/'maintenance-readonly-evidence'/self.result['phase_id']/(api+'-01.json')
     event=json.loads(path.read_text());event['query_id']='d'*32
     field='oso' if kind=='pending' else 'acnt_ord_cntr_prst_array';event['response_body'][field]=[{'ord_no':number,'orig_ord_no':'0'}]
     self.tracker.record_broker_observation(event)
    self.assert_closed()
 def test_original_set_missing_extra_partial_fill_or_strategy_change_fails(self):
  for kind in ('missing','extra','strategy','partial'):
   with self.subTest(kind=kind):
    self.initialize()
    with self.tracker._db() as db:
     if kind=='missing':db.execute('DELETE FROM orders WHERE ord_no=?',(e.ORDER_NUMBERS[0],))
     elif kind=='extra':db.execute('INSERT INTO orders VALUES (?,?,?,?,?,?,?,?,?)',(self.tracker.scope,e.INCIDENT_DAY,'paper','777777','005930','buy','EXP_D',2,'2026-11-03T15:20:00+09:00'))
     elif kind=='strategy':db.execute("UPDATE orders SET strategy='EXP_R' WHERE ord_no=?",(e.ORDER_NUMBERS[0],))
     else:db.execute('INSERT INTO fills VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(self.tracker.scope,e.INCIDENT_DAY,'paper',e.ORDER_NUMBERS[0],'fixture-fill','003220','fixture','buy','15:20:00','8030',1,'EXP_D','25',None,None))
    self.prepare();self.assert_closed()
 def test_last_moment_stream_change_rolls_back_all_nine_certificates(self):
  self.prepare();original=e.capture_certificates
  def capture(*args):
   result=original(*args);self.stream['observations']+=1;return result
  with patch.object(e,'capture_certificates',side_effect=capture):self.assert_closed()
 def test_late_financial_write_attempt_is_denied_and_certificate_transaction_rolls_back(self):
  self.prepare();original=e.capture_certificates
  def capture(tracker,db,now):
   original(tracker,db,now);db.execute('UPDATE orders SET quantity=999')
  with patch.object(e,'capture_certificates',side_effect=capture):self.assert_closed()
 def test_assignment_changed_during_capture_rejects_commit(self):
  self.prepare();original=e.capture_certificates
  def capture(*args):
   result=original(*args)
   import sqlite3
   with sqlite3.connect(self.state_dir/'paper-experiment.sqlite3') as db:db.execute("INSERT INTO selections VALUES ('20261104','fixture','{}')")
   return result
  with patch.object(e,'capture_certificates',side_effect=capture):self.assert_closed()
 def test_final_scan_crossing_ninety_seconds_rolls_back_and_never_relabels_query_times(self):
  self.prepare();self.now+=timedelta(seconds=79);original=e.capture_certificates
  def capture(*args):
   result=original(*args);self.now+=timedelta(seconds=12);return result
  with patch.object(e,'capture_certificates',side_effect=capture):self.assert_closed()
 def test_completed_evidence_later_new_query_account_or_stream_change_cannot_release(self):
  for kind in ('query','account','stream'):
   with self.subTest(kind=kind):
    self.initialize();self.prepare();self.assertEqual(self.resolve()['status'],'complete');self.marker.unlink()
    if kind=='stream':self.stream['observations']+=1
    elif kind=='account':
     with self.tracker._db() as db:db.execute("UPDATE account_balance_daily SET observed_at='2026-11-04T09:30:01+09:00'")
    else:
     event=json.loads((self.state_dir/'maintenance-readonly-evidence'/self.result['phase_id']/'ka10075-01.json').read_text());event['query_id']='e'*32;event.update(status='transport_error',http_status=None,response_body={})
     self.tracker.record_broker_observation(event)
    self.assertFalse(self.resolver.release_if_verified(self.session,self.tracker));self.assertTrue(self.resolver.blocked)
 def test_no_marker_normal_start_without_posix_nofollow_or_getuid(self):
  self.marker.unlink()
  from types import SimpleNamespace
  import os
  fake=SimpleNamespace(**{name:getattr(os,name) for name in dir(os) if name not in ('O_NOFOLLOW','getuid')})
  with patch.object(e,'os',fake):
   actor=e.MaintenanceExpiryResolution(now=lambda:self.now)
   self.assertFalse(actor.latch(self.state_dir));self.assertFalse(actor.requested(self.state_dir));actor.assert_orders_allowed()
 def test_named_but_noop_immutability_trigger_cannot_authorize_append(self):
  self.prepare()
  with self.tracker._db() as db:
   name=TABLE+'_no_update';db.execute('DROP TRIGGER '+name)
   db.execute('CREATE TRIGGER '+name+' BEFORE UPDATE ON '+TABLE+' BEGIN SELECT 1; END')
  self.assert_closed()

if __name__=='__main__':unittest.main()
