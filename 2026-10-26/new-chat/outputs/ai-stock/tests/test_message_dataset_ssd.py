from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from kiwoom_autotrader.utils.message_dataset import (_output_guard, daily_output_root, _private_directory, _write_changed, MessageDatasetError)

class Archive:
    mount=Path('/Volumes/AIStockData')
    directory=mount/'telegram-channel-archive'

class Store:
    _external_archive=Archive()
    def __init__(self,fail=False): self.calls=0; self.fail=fail
    def check_storage_health(self):
        self.calls+=1
        if self.fail: raise RuntimeError('unmounted')

class DailyGuardTests(unittest.TestCase):
    def test_external_root_selection_checks_health_first(self):
        s=Store();self.assertEqual(daily_output_root(s,Path('/tmp/state')),Archive.directory/'daily');self.assertEqual(s.calls,1)
        with self.assertRaises(RuntimeError):daily_output_root(Store(True),Path('/tmp/state'))
    def test_external_path_without_guarded_store_rejected(self):
        class Local: _external_archive=None
        with self.assertRaises(MessageDatasetError):_output_guard(Local(),Path('/Volumes/AIStockData/nonexistent/daily'))
    def test_unavailable_volume_avoids_mkdir(self):
        store=Store(True)
        with patch.object(Path,'mkdir') as mkdir:
            with self.assertRaises(RuntimeError):_output_guard(store,Path('/Volumes/AIStockData/archive/daily'))
            mkdir.assert_not_called()
    def test_compatibility_symlink_cannot_skip_health_check(self):
        with tempfile.TemporaryDirectory() as d:
            link=Path(d)/'daily';link.symlink_to('/Volumes/AIStockData/archive/daily')
            with self.assertRaises(RuntimeError):_output_guard(Store(True),link)
    def test_failed_guard_prevents_write_and_mkdir(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); fail=lambda: (_ for _ in ()).throw(RuntimeError('missing'))
            with self.assertRaises(RuntimeError):_private_directory(root/'no',fail)
            self.assertFalse((root/'no').exists())
            with self.assertRaises(RuntimeError):_write_changed(root/'file',b'x',fail)
            self.assertFalse((root/'file').exists())
    def test_guard_rechecked_before_replace(self):
        with tempfile.TemporaryDirectory() as d:
            dest=Path(d)/'file';calls=[]
            def guard():
                calls.append(1)
                if len(calls)>=5: raise RuntimeError('volume lost')
            with self.assertRaises(RuntimeError):_write_changed(dest,b'x',guard)
            self.assertFalse(dest.exists())

if __name__=='__main__': unittest.main()

class AnchoredRaceTests(unittest.TestCase):
    def test_volume_path_redirect_to_local_rejected(self):
        s=Store()
        with patch.object(Path,'resolve',return_value=Path('/tmp/escape')):
            with self.assertRaises(MessageDatasetError):
                _output_guard(s,Path('/Volumes/AIStockData/telegram-channel-archive/daily'))
        self.assertEqual(s.calls,0)

    def test_replacement_after_health_check_cannot_write_fake_directory(self):
        import os
        from kiwoom_autotrader.utils.anchored_dataset import AnchoredDataset
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)/'mounted';moved=Path(d)/'detached';root.mkdir()
            obj=AnchoredDataset.__new__(AnchoredDataset);obj.fd=os.open(root,os.O_RDONLY)
            calls=[]
            def guard():
                calls.append(1)
                if len(calls)==3:
                    root.rename(moved);root.mkdir()
            obj.guard=guard
            try:
                obj.write('manifest.json',b'safe')
                self.assertFalse((root/'manifest.json').exists())
                self.assertEqual((moved/'manifest.json').read_bytes(),b'safe')
            finally:obj.close()

    def test_relative_day_mkdir_cannot_write_replacement(self):
        import os
        from kiwoom_autotrader.utils.anchored_dataset import AnchoredDataset
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)/'mounted';moved=Path(d)/'detached';root.mkdir()
            obj=AnchoredDataset.__new__(AnchoredDataset);obj.fd=os.open(root,os.O_RDONLY)
            calls=[]
            def guard():
                calls.append(1)
                if len(calls)==2:root.rename(moved);root.mkdir()
            obj.guard=guard
            try:
                obj.ensure_day('2026-10-29')
                self.assertFalse((root/'2026-10-29').exists())
                self.assertTrue((moved/'2026-10-29').is_dir())
            finally:obj.close()
