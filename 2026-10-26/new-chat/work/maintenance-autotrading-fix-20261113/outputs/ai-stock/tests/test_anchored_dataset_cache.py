import hashlib, os, tempfile, unittest
from pathlib import Path
from unittest.mock import patch
from kiwoom_autotrader.utils.anchored_dataset import AnchoredDataset, _VERIFY_CACHE

class AnchoredHashCacheTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory(); self.root=Path(self.tmp.name)
  self.out=AnchoredDataset.__new__(AnchoredDataset);self.out.fd=os.open(self.root,os.O_RDONLY);self.out.guard=lambda:None
  self.file=self.root/'daily_factors.csv';self.file.write_bytes(b'original')
  self.manifest={'files':[{'path':'daily_factors.csv','sha256':hashlib.sha256(b'original').hexdigest()}]}
  _VERIFY_CACHE.clear()
 def tearDown(self):self.out.close();self.tmp.cleanup();_VERIFY_CACHE.clear()
 def test_revalidates_changed_same_size_and_missing(self):
  self.assertTrue(self.out.files_exist(self.manifest));self.assertTrue(self.out.files_exist(self.manifest))
  self.file.write_bytes(b'modified');self.assertFalse(self.out.files_exist(self.manifest))
  self.file.unlink();self.assertFalse(self.out.files_exist(self.manifest))
 def test_periodic_full_and_replace(self):
  with patch('kiwoom_autotrader.utils.anchored_dataset.time.monotonic',side_effect=[1,2,400,400]):
   self.assertTrue(self.out.files_exist(self.manifest));self.assertTrue(self.out.files_exist(self.manifest));self.assertTrue(self.out.files_exist(self.manifest))
  replacement=self.root/'other';replacement.write_bytes(b'corrupt!');replacement.replace(self.file)
  self.assertFalse(self.out.files_exist(self.manifest))
 def test_reads_content_only_initial_and_at_deadline(self):
  with patch('kiwoom_autotrader.utils.anchored_dataset.time.monotonic',side_effect=[1,2,400,400]), patch.object(self.out,'_verify_file',wraps=self.out._verify_file) as verify:
   self.assertTrue(self.out.files_exist(self.manifest))
   self.assertTrue(self.out.files_exist(self.manifest))
   self.assertTrue(self.out.files_exist(self.manifest))
   self.assertEqual([x.args[2] for x in verify.call_args_list],[True,False,True])
 def test_guard_failure_and_duplicate_manifest_fail_closed(self):
  self.assertTrue(self.out.files_exist(self.manifest))
  self.out.guard=lambda: (_ for _ in ()).throw(RuntimeError('mount unavailable'))
  with self.assertRaises(RuntimeError):self.out.files_exist(self.manifest)
  self.out.guard=lambda:None
  self.assertFalse(self.out.files_exist({'files':self.manifest['files']*2}))
if __name__=='__main__':unittest.main()
