"""Production schema, immutable raw/repeat reconstruction and simulated hour."""
from copy import deepcopy
from datetime import datetime,timedelta
import hashlib,json,sqlite3,tempfile,unittest
from pathlib import Path
from kiwoom_autotrader.api.session import KST
from kiwoom_autotrader.trading.execution_log import ExecutionTracker,ExecutionLogError
from kiwoom_autotrader.trading.broker_observation_repeats import install_evidence_view

class RepeatEvidenceTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
  self.now=datetime(2026,11,3,9,30,tzinfo=KST)
  self.tracker=ExecutionTracker(log_dir=Path(self.tmp.name),now=lambda:self.now,settings_loader=lambda:{})
 def event(self,*,api='ka10075',day='20261103',page=1,rows=None,body=None):
  return {'schema':'kiwoom-broker-page-v1','source_api':api,'source_kind':'broker_observation','mode':'paper','day':day,
   'query_id':format(int(self.now.timestamp()*1000000),'032x'),'request_started_at':self.now.isoformat(),
   'observed_at':self.now.isoformat(),'page':page,'request_body':body or {'all_stk_tp':'0','trde_tp':'0','stex_tp':'0'},
   'response_body':{'return_code':0,'oso':rows or []},'continuation':{'request_cont_yn':'N','response_cont_yn':'N','next_key_present':False},'status':'success','http_status':200}
 def put(self,**kw):
  value=self.event(**kw);self.tracker.record_broker_observation(value);self.now+=timedelta(seconds=10);return value
 def counts(self):
  with self.tracker._db() as db:return tuple(db.execute('SELECT (SELECT COUNT(*) FROM broker_observations),(SELECT COUNT(*) FROM broker_observation_repeats)').fetchone())
 def test_original_immutable_latest_time_hash_query_complete_and_ro_trusted_off(self):
  first=self.put();second=self.put();self.assertEqual(self.counts(),(1,1))
  with sqlite3.connect(self.tracker.path.as_uri()+'?mode=ro',uri=True) as db:
   db.row_factory=sqlite3.Row;db.execute('PRAGMA trusted_schema=OFF');install_evidence_view(db);db.execute('PRAGMA query_only=ON');db.execute('BEGIN')
   rows=db.execute('SELECT * FROM broker_observation_evidence ORDER BY id').fetchall()
   self.assertEqual([json.loads(r['payload_json']) for r in rows],[first,second])
   self.assertTrue(all(hashlib.sha256(r['payload_json'].encode()).hexdigest()==r['digest'] for r in rows))
   self.assertEqual([r['id'] for r in rows],[1,2]);self.assertNotEqual(rows[0]['observed_at'],rows[1]['observed_at'])
   with self.assertRaises(sqlite3.OperationalError):db.execute('DELETE FROM broker_observations')
  with self.tracker._db() as db:
   with self.assertRaises(sqlite3.IntegrityError):db.execute('UPDATE broker_observation_repeats SET observed_at=?',(self.now.isoformat(),))
 def test_body_scope_page_epoch_changes_create_new_original(self):
  self.put();self.put(body={'all_stk_tp':'0','trde_tp':'0','stex_tp':'1'});self.put(page=2)
  self.put(rows=[{'ord_no':'24','oso_qty':'1'}]);self.tracker._broker_dedup_epoch='new-test-session';self.put()
  self.assertEqual(self.counts(),(5,0))
 def test_ws00_never_shared(self):
  for i in range(2):self.tracker.record_broker_observation({'schema_version':1,'source_api':'00','source_kind':'broker_observation',
   'day':'20261103','mode':'paper','observed_at':self.now.isoformat(),'event_kind':'stream_started','values':{}})
  self.assertEqual(self.counts(),(2,0))
 def test_tampered_repeat_row_cannot_supply_freshness(self):
  self.put();self.put()
  with sqlite3.connect(self.tracker.path) as db:
   db.execute('DROP TRIGGER broker_observation_repeats_no_update');db.execute("UPDATE broker_observation_repeats SET observed_at='2030-01-01T09:00:00+09:00'")
  with self.tracker._db() as db:
   with self.assertRaises(sqlite3.OperationalError):db.execute('SELECT payload_json FROM broker_observation_evidence ORDER BY id').fetchall()
 def test_partial_continuation_not_merged_with_complete_page(self):
  self.put();event=self.event();event['continuation'].update(response_cont_yn='Y',next_key_present=True)
  self.tracker.record_broker_observation(event);self.assertEqual(self.counts(),(2,0))
 def test_simulated_hour_raw_rows_shrink_but_all_observations_preserved(self):
  body=[{'ord_no':str(n+1),'oso_qty':'1','stk_cd':'A005930'} for n in range(200)]
  baseline_bytes=0
  for _ in range(360):
   event=self.put(rows=body);baseline_bytes+=len(json.dumps(event,ensure_ascii=True,sort_keys=True,separators=(',',':')).encode())
  self.assertEqual(self.counts(),(1,359))
  with self.tracker._db() as db:
   stored=db.execute('SELECT (SELECT SUM(LENGTH(payload_json)) FROM broker_observations)+(SELECT SUM(LENGTH(receipt_json)) FROM broker_observation_repeats)').fetchone()[0]
   self.assertEqual(db.execute('SELECT COUNT(*) FROM broker_observation_evidence').fetchone()[0],360)
   self.assertLess(stored,baseline_bytes//5)

 def test_prior_day_terminal_repeated_pages_remain_current_and_complete(self):
  from tests.test_paper_experiment_ledger_recovery import PriorDayPendingRecovery
  f=PriorDayPendingRecovery('test_complete_matching_detail_and_current_pending_prove_terminal');f.setUp();self.addCleanup(f.doCleanups)
  f.save();self.assertTrue(f.check());f.now+=timedelta(seconds=10)
  f.tracker.record_broker_observation(f.event('kt00007','c',f.now-timedelta(seconds=8),[f.row]))
  f.tracker.record_broker_observation(f.event('ka10075','d',f.now-timedelta(seconds=5),[]))
  with f.tracker._db() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM broker_observation_repeats').fetchone()[0],2)
  self.assertTrue(f.check())
 def test_cancel_settlement_reader_accepts_lossless_reobserved_rest_page(self):
  from tests.test_cancellation_settlement import SettlementFixture
  f=SettlementFixture();self.addCleanup(f.close);f.settle();self.assertEqual(f.released(),7)
  f.now+=timedelta(seconds=1);f.tracker.record_broker_observation(f.page('kt00009',f.rest))
  with f.tracker._db() as db:self.assertGreaterEqual(db.execute('SELECT COUNT(*) FROM broker_observation_repeats').fetchone()[0],1)
  self.assertEqual(f.released(),7)
