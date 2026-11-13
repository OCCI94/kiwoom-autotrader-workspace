import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from kiwoom_autotrader.telegram.channel_storage import _mount_identity, ExternalArchiveError, attempt_mount, MARKER
from kiwoom_autotrader.telegram.channel_store import ChannelStore, ExternalStorageUnavailable

class SSDGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name); self.volume=self.root/'mount'; self.volume.mkdir()
        (self.volume/'.ssd-volume-sentinel').write_text('UUID\n')
    def test_mount_and_sentinel_required(self):
        original=os.stat
        def fake_stat(path,*a,**kw):
            if Path(path)==self.volume: return SimpleNamespace(st_dev=2)
            if Path(path)==self.root: return SimpleNamespace(st_dev=1)
            return original(path,*a,**kw)
        with patch('os.path.ismount',return_value=True), patch('os.stat',side_effect=fake_stat):
            _mount_identity(self.volume,'UUID')
            with self.assertRaises(ExternalArchiveError): _mount_identity(self.volume,'WRONG')
            (self.volume/'.ssd-volume-sentinel').unlink()
            with self.assertRaises(ExternalArchiveError): _mount_identity(self.volume,'UUID')
        with patch('os.path.ismount',return_value=False):
            with self.assertRaises(ExternalArchiveError): _mount_identity(self.volume,'UUID')
    def test_unverified_external_path_never_connects_or_creates(self):
        state=self.root/'state'; state.mkdir(); archive=self.volume/'archive'; archive.mkdir()
        image=self.root/'image'; image.mkdir()
        (state/MARKER).write_text(json.dumps({'version':1,'mount':str(self.volume),'volume_uuid':'UUID','directory':str(archive),'host':str(self.root),'host_uuid':'HOST','image':str(image)}))
        with patch('kiwoom_autotrader.telegram.channel_storage._mount_identity',side_effect=ExternalArchiveError('missing')), patch('sqlite3.connect') as connect:
            with self.assertRaises(ExternalStorageUnavailable): ChannelStore(state,123)
            connect.assert_not_called()
        self.assertFalse((archive/'channels-123.sqlite3').exists())
        self.assertFalse((state/'channels-123.sqlite3').exists())
    def test_attempt_mount_once_and_check(self):
        state=self.root/'state';state.mkdir(); directory=self.volume/'archive';directory.mkdir();image=self.root/'image';image.mkdir()
        (state/MARKER).write_text(json.dumps({'version':1,'mount':str(self.volume),'volume_uuid':'UUID','directory':str(directory),'host':str(self.root),'host_uuid':'HOST','image':str(image)}))
        with patch('kiwoom_autotrader.telegram.channel_storage.subprocess.run') as run, patch('kiwoom_autotrader.telegram.channel_storage._mount_identity'):
            self.assertTrue(attempt_mount(state)); run.assert_called_once(); self.assertEqual(run.call_args.args[0],['/usr/sbin/diskutil','mount','UUID'])

if __name__=='__main__': unittest.main()
