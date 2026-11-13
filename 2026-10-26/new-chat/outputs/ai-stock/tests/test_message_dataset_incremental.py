from pathlib import Path
import json, sqlite3, tempfile, unittest, shutil, os
from unittest.mock import patch
from kiwoom_autotrader.telegram.channel_store import ChannelStore
from kiwoom_autotrader.utils.message_dataset import export_daily_dataset, MessageDatasetError
from kiwoom_autotrader.utils.message_dataset_incremental import _STATES

T=1793000000
class IncrementalDatasetTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.base=Path(self.tmp.name);self.store=ChannelStore(self.base/'state',123)
  self.store.register_channel(100,200,'Synthetic','synthetic_channel');self.out=self.base/'daily';self.full=self.base/'full'
 def tearDown(self):
  self.store.close();self.tmp.cleanup();_STATES.clear()
 def add(self,msg,published,text,edited=None):
  value={'message_id':msg,'published_at':published,'text':text}
  if edited is not None:value['edited_at']=edited
  self.store.commit_page(100,[value])
 def export(self,when,root,explicit=False):
  with patch('kiwoom_autotrader.utils.message_dataset.time.time',return_value=when):
   return export_daily_dataset(self.store,root,when if explicit else None)
 def files(self,root):return {p.relative_to(root).as_posix():p.read_bytes() for p in root.rglob('*') if p.is_file()}
 def test_validated_factor_path_matches_public_builder(self):
  from kiwoom_autotrader.trading.message_factors import normalize_versions,build_daily_factors,_build_daily_factors_normalized
  with patch('kiwoom_autotrader.telegram.channel_store.time.time',return_value=T):
   self.add(1,T-86400,'KR:005930');self.add(2,T-172800,'$AAPL')
  with patch('kiwoom_autotrader.telegram.channel_store.time.time',return_value=T+60):
   self.add(1,T-86400,'edited KR:005930',T+60)
  rows=self.store.versions(active_only=False)
  for cutoff in (T-1,T,T+60,T+86400):
   self.assertEqual(build_daily_factors(rows,cutoff),_build_daily_factors_normalized(normalize_versions(rows),cutoff))
 def test_incremental_matches_full_after_backfill_and_edit(self):
  with patch('kiwoom_autotrader.telegram.channel_store.time.time',return_value=T):
   self.add(1,T-86400,'KR:005930');self.add(2,T-172800,'KR:097520')
   self.export(T,self.out)
  with patch('kiwoom_autotrader.telegram.channel_store.time.time',return_value=T+60):
   self.add(1,T-86400,'edited KR:005930',T+60)
   self.add(3,T-259200,'backfill KR:072950')
   result=self.export(T+60,self.out)
   self.assertGreaterEqual(result['reused_raw_partitions'],1)
   self.export(T+60,self.full,explicit=True)
  self.assertEqual(self.files(self.out),self.files(self.full))
 def test_interrupted_export_recovers_to_full_baseline(self):
  import kiwoom_autotrader.utils.message_dataset as mod
  with patch('kiwoom_autotrader.telegram.channel_store.time.time',return_value=T):
   self.add(1,T-86400,'before');self.export(T,self.out)
  original=mod._write_changed
  def crash(path,data,guard=lambda:None):
   if path.name=='manifest.json':raise OSError('simulated interruption')
   return original(path,data,guard)
  with patch('kiwoom_autotrader.telegram.channel_store.time.time',return_value=T+60):
   self.add(1,T-86400,'edited',T+60)
   with patch.object(mod,'_write_changed',side_effect=crash):
    with self.assertRaises(MessageDatasetError):self.export(T+60,self.out)
   result=self.export(T+60,self.out)
   self.assertEqual(result['reused_raw_partitions'],0)
   self.export(T+60,self.full,explicit=True)
  self.assertEqual(self.files(self.out),self.files(self.full))
 def test_db_path_replacement_invalidates_whole_export_cache(self):
  with patch('kiwoom_autotrader.telegram.channel_store.time.time',return_value=T):
   self.add(1,T-86400,'before');self.export(T,self.out)
  replacement=self.base/'copy.sqlite3';shutil.copy2(self.store.path,replacement);os.replace(replacement,self.store.path)
  # An open SQLite handle refers to the old inode; refuse to export rather
  # than falsely rebuild from stale contents under the replacement path.
  with self.assertRaises(MessageDatasetError):self.export(T+60,self.out)
  with self.assertRaises(MessageDatasetError):self.export(T+120,self.out)
  self.store.close(); _STATES.clear(); self.store=ChannelStore(self.base/'state',123)
  result=self.export(T+180,self.out)
  self.assertFalse(result.get('incremental',False))
 def test_external_change_during_read_never_baselined(self):
  import kiwoom_autotrader.utils.message_dataset_incremental as inc
  with patch('kiwoom_autotrader.telegram.channel_store.time.time',return_value=T):
   self.add(1,T-86400,'before');self.export(T,self.out)
  with patch('kiwoom_autotrader.telegram.channel_store.time.time',return_value=T+60):self.add(2,T,'new')
  original=inc._factor_day
  def concurrent(*args):
   result=original(*args)
   db=sqlite3.connect(self.store.path)
   try:db.execute('UPDATE archive_meta SET dataset_revision=dataset_revision+1');db.commit()
   finally:db.close()
   return result
  with patch.object(inc,'_factor_day',side_effect=concurrent):
   with self.assertRaises(MessageDatasetError):self.export(T+60,self.out)
  # An old revision may have been published, but no trusted baseline is kept.
  result=self.export(T+120,self.out)
  self.assertFalse(result['cached'])
 def test_external_db_write_and_new_store_force_full(self):
  with patch('kiwoom_autotrader.telegram.channel_store.time.time',return_value=T):
   self.add(1,T-86400,'KR:005930');self.export(T,self.out)
  other=sqlite3.connect(self.store.path)
  try:other.execute('UPDATE archive_meta SET dataset_revision=dataset_revision+1');other.commit()
  finally:other.close()
  with patch('kiwoom_autotrader.telegram.channel_store.time.time',return_value=T+60):
   self.add(2,T,'new');out=self.export(T+60,self.out)
  self.assertEqual(out['reused_raw_partitions'],0)
 def test_pending_future_source_or_corruption_forces_full(self):
  with patch('kiwoom_autotrader.telegram.channel_store.time.time',return_value=T):
   self.add(1,T-86400,'future',T+300);self.export(T,self.out)
  with patch('kiwoom_autotrader.telegram.channel_store.time.time',return_value=T+60):
   self.add(2,T,'new');result=self.export(T+60,self.out)
   self.assertTrue(result['incremental'])
  with patch('kiwoom_autotrader.telegram.channel_store.time.time',return_value=T+400):self.export(T+400,self.out)
  manifest=json.loads((self.out/'manifest.json').read_text());raw=next(i for i in manifest['files'] if i['path'].endswith('messages.jsonl'))
  (self.out/raw['path']).write_bytes(b'corrupt')
  with patch('kiwoom_autotrader.telegram.channel_store.time.time',return_value=T+460):
   self.add(3,T+400,'new');result=self.export(T+460,self.out)
  self.assertEqual(result['reused_raw_partitions'],0)
  self.export(T+460,self.full,explicit=True)
  self.assertEqual(self.files(self.out),self.files(self.full))
 def test_restart_and_monotonic_cutoff_do_not_read_entire_archive(self):
  with patch('kiwoom_autotrader.telegram.channel_store.time.time',return_value=T):
   self.add(1,T-86400,'secret body KR:005930'); self.export(T,self.out)
  state=(self.out/'.incremental-export-state.json').read_text()
  self.assertNotIn('secret body',state)
  for shard in self.out.glob('.incremental-day-*.json'):
   self.assertNotIn('secret body',shard.read_text())
  self.store.close(); _STATES.clear(); self.store=ChannelStore(self.base/'state',123)
  with patch.object(self.store,'versions',side_effect=AssertionError('full body read')):
   result=self.export(T+60,self.out)
  self.assertTrue(result['incremental']);self.assertEqual(result['as_of'],T+60)
  self.export(T+60,self.full,explicit=True)
  self.assertEqual(self.files(self.out),self.files(self.full))
 def test_empty_cutoff_advance_does_not_read_busy_day_shards(self):
  with patch('kiwoom_autotrader.telegram.channel_store.time.time',return_value=T):
   self.add(1,T-86400,'record KR:005930');self.export(T,self.out)
  with patch('kiwoom_autotrader.utils.message_dataset_incremental._shard_read',side_effect=AssertionError('day shard read')):
   result=self.export(T+60,self.out)
  self.assertTrue(result['incremental']);self.assertEqual(result['recomputed_factor_days'],0)
  self.export(T+60,self.full,explicit=True)
  self.assertEqual(self.files(self.out),self.files(self.full))
 def test_new_backfill_edit_future_and_cutoff_match_fresh_full(self):
  with patch('kiwoom_autotrader.telegram.channel_store.time.time',return_value=T):
   self.add(1,T-86400,'a KR:005930');self.add(2,T-172800,'future $AAPL',T+300);self.export(T,self.out)
  for delta in (60,120,310,86420):
   with patch('kiwoom_autotrader.telegram.channel_store.time.time',return_value=T+delta):
    if delta==60:self.add(1,T-86400,'a edit $MSFT',T+delta)
    if delta==120:self.add(3,T-259200,'new backfill KR:072950')
    with patch.object(self.store,'versions',side_effect=AssertionError('full body read')):
     result=self.export(T+delta,self.out)
    self.assertTrue(result['incremental'],str(delta))
    shutil.rmtree(self.full,ignore_errors=True);self.export(T+delta,self.full,explicit=True)
    self.assertEqual(self.files(self.out),self.files(self.full),str(delta))
 def test_corrupt_state_and_backward_cutoff_rebuild_safely(self):
  with patch('kiwoom_autotrader.telegram.channel_store.time.time',return_value=T):
   self.add(1,T-86400,'record');self.export(T,self.out)
  (self.out/'.incremental-export-state.json').write_text('{broken')
  result=self.export(T+60,self.out)
  self.assertFalse(result.get('incremental',False))
  result=self.export(T-1,self.out,explicit=True)
  self.assertFalse(result.get('incremental',False))
  self.export(T-1,self.full,explicit=True)
  self.assertEqual(self.files(self.out),self.files(self.full))
 def test_selection_changes_keep_archived_sources_in_dataset(self):
  with patch('kiwoom_autotrader.telegram.channel_store.time.time',return_value=T):
   self.add(1,T-86400,'archived');self.export(T,self.out)
  self.store.set_selected([])
  with patch.object(self.store,'versions',side_effect=AssertionError('full body read')):
   result=self.export(T+60,self.out)
  self.assertTrue(result['incremental']);self.assertEqual(result['versions'],1)
  self.export(T+60,self.full,explicit=True)
  self.assertEqual(self.files(self.out),self.files(self.full))
 def test_new_version_in_same_second_as_prior_export_is_incremental(self):
  with patch('kiwoom_autotrader.telegram.channel_store.time.time',return_value=T):
   self.add(1,T-86400,'first KR:005930');self.export(T,self.out)
   self.add(2,T-172800,'second $AAPL')
   with patch.object(self.store,'versions',side_effect=AssertionError('full body read')):
    result=self.export(T,self.out)
   self.assertTrue(result['incremental'])
   self.export(T,self.full,explicit=True)
  self.assertEqual(self.files(self.out),self.files(self.full))
 def test_future_observation_becomes_raw_then_future_revision_becomes_factor(self):
  with patch('kiwoom_autotrader.telegram.channel_store.time.time',return_value=T+100):
   self.add(1,T-86400,'future $AAPL',T+300)
  self.export(T,self.out,explicit=True)
  for cutoff in (T+50,T+100,T+200,T+300):
   with patch.object(self.store,'versions',side_effect=AssertionError('full body read')):
    result=self.export(cutoff,self.out,explicit=True)
   self.assertTrue(result['incremental'])
   shutil.rmtree(self.full,ignore_errors=True);self.export(cutoff,self.full,explicit=True)
   self.assertEqual(self.files(self.out),self.files(self.full),str(cutoff))
 def test_replacing_immutable_triggers_invalidates_the_state(self):
  with patch('kiwoom_autotrader.telegram.channel_store.time.time',return_value=T):
   self.add(1,T-86400,'before');self.export(T,self.out)
  self.store._connection().execute('DROP TRIGGER post_versions_no_update')
  self.store._connection().commit()
  result=self.export(T+60,self.out)
  self.assertFalse(result.get('incremental',False))
  self.export(T+60,self.full,explicit=True)
  # Invalid append-only schema never establishes a new trusted state.
  self.assertEqual((self.out/'manifest.json').read_bytes(),(self.full/'manifest.json').read_bytes())
 def test_concurrent_exports_share_a_persistent_output_lock(self):
  from kiwoom_autotrader.utils.message_dataset import _dataset_lock
  from threading import Event, Thread
  attempted, acquired = Event(), Event()
  def contender():
   attempted.set()
   with _dataset_lock(self.store,self.out):acquired.set()
  with _dataset_lock(self.store,self.out):
   worker=Thread(target=contender);worker.start()
   self.assertTrue(attempted.wait(2))
   self.assertFalse(acquired.wait(0.1))
  worker.join(2)
  self.assertFalse(worker.is_alive());self.assertTrue(acquired.is_set())
  self.assertTrue((self.out/'.dataset-export.lock').exists())
 def test_deterministic_mixed_history_matches_fresh_rebuild_at_every_step(self):
  import random
  rng=random.Random(54197); published={}
  for step in range(1,26):
   when=T+step*60
   with patch('kiwoom_autotrader.telegram.channel_store.time.time',return_value=when):
    for _ in range(3):
     msg=rng.randrange(1,19)
     published.setdefault(msg,T-rng.randrange(1,10)*86400)
     self.add(msg,published[msg],'synthetic %d KR:%06d $AAPL' % (step, msg),when)
    result=self.export(when,self.out)
   if step>1:self.assertTrue(result.get('incremental'),str(step))
   shutil.rmtree(self.full,ignore_errors=True);self.export(when,self.full,explicit=True)
   self.assertEqual(self.files(self.out),self.files(self.full),str(step))
 def test_corrupt_day_shard_forces_full_rebuild_without_message_loss(self):
  with patch('kiwoom_autotrader.telegram.channel_store.time.time',return_value=T):
   self.add(1,T-86400,'first');self.export(T,self.out)
  part=next(self.out.glob('.incremental-day-*.json'));part.write_bytes(b'broken')
  with patch('kiwoom_autotrader.telegram.channel_store.time.time',return_value=T+60):
   self.add(2,T-86400,'second')
   with patch.object(self.store,'versions',wraps=self.store.versions) as read_all:
    result=self.export(T+60,self.out)
   self.assertFalse(result.get('incremental',False));self.assertGreater(read_all.call_count,0)
   self.export(T+60,self.full,explicit=True)
  self.assertEqual(self.files(self.out),self.files(self.full))
 def test_unchanged_raw_file_is_not_rehashed_and_same_size_tampering_is_detected(self):
  import kiwoom_autotrader.utils.message_dataset_incremental as inc
  inc._FILE_VERIFY_CACHE.clear()
  with patch('kiwoom_autotrader.telegram.channel_store.time.time',return_value=T):
   self.add(1,T-86400,'first');self.export(T,self.out)
  self.export(T+60,self.out)
  calls=[]; original=inc._local_verify_file
  def measured(root,name,expected,full,guard):
   calls.append((name,full));return original(root,name,expected,full,guard)
  with patch.object(inc,'_local_verify_file',side_effect=measured):self.export(T+120,self.out)
  self.assertTrue(any(name.endswith('messages.jsonl') for name,_ in calls))
  self.assertFalse(any(full for name,full in calls if name.endswith('messages.jsonl')))
  raw=next(self.out.glob('*/messages.jsonl')); old=raw.stat();data=raw.read_bytes()
  raw.write_bytes(bytes([data[0]^1])+data[1:]);os.utime(raw,ns=(old.st_atime_ns,old.st_mtime_ns))
  result=self.export(T+180,self.out)
  self.assertFalse(result.get('incremental',False))
  self.export(T+180,self.full,explicit=True)
  self.assertEqual(self.files(self.out),self.files(self.full))
if __name__=='__main__':unittest.main()
