import json
import os
import sys
from kiwoom_autotrader.utils.platform_io import fcntl
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from kiwoom_autotrader.telegram.channel_store import ChannelStore, ChannelStoreError
from kiwoom_autotrader.telegram.channel_storage import MARKER


class ExternalChannelStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.state = self.root / 'state'; self.state.mkdir()
        self.volume = self.root / 'volume'; self.volume.mkdir()
        self.dest = self.volume / 'archive'; self.dest.mkdir()
        self.host = self.root / 'host'; self.host.mkdir()
        self.image = self.host / 'bundle'; self.image.mkdir()
        # Create a valid existing archive separately; no external marker yet.
        with ChannelStore(self.dest, 123) as store:
            store.register_channel(10, 15, 'test', None)
        self.external = self.dest / 'channels-123.sqlite3'
        self.marker = self.state / MARKER
        self.marker.write_text(json.dumps({'version':1, 'mount':str(self.volume),
            'volume_uuid':'TEST-UUID', 'directory':str(self.dest), 'host':str(self.host), 'host_uuid':'HOST-UUID', 'image':str(self.image)}))
        self.info = {'MountPoint':str(self.volume), 'VolumeUUID':'TEST-UUID', 'WritableVolume':True}
        self.mock = patch('kiwoom_autotrader.telegram.channel_storage._volume', side_effect=lambda p: {'MountPoint':str(self.host), 'VolumeUUID':'HOST-UUID', 'WritableVolume':True} if p == self.host else dict(self.info))
        self.images = patch('kiwoom_autotrader.telegram.channel_storage._image_info', return_value={'images':[{'image-path':str(self.image),'system-entities':[{'mount-point':str(self.volume)}]}]})
        self.mount = patch('kiwoom_autotrader.telegram.channel_storage._mount_identity')
        self.mock.start(); self.images.start(); self.mount.start()

    def tearDown(self):
        self.mock.stop(); self.images.stop(); self.mount.stop(); self.temp.cleanup()

    def test_all_store_instances_read_same_external_archive_without_local_db(self):
        with ChannelStore(self.state, 123) as one, ChannelStore(self.state, 123) as two:
            self.assertEqual(one.path, self.external)
            self.assertEqual(len(two.channels()), 1)
        self.assertFalse((self.state / 'channels-123.sqlite3').exists())

    def test_detached_or_wrong_uuid_prevents_later_reads_and_writes(self):
        with ChannelStore(self.state, 123) as store:
            self.info['VolumeUUID'] = 'WRONG'
            with self.assertRaises(ChannelStoreError): store.channels()
            with self.assertRaises(ChannelStoreError): store.register_channel(11, 16, 'x', None)
        with self.assertRaises(ChannelStoreError): ChannelStore(self.state, 123)
        self.assertFalse((self.state / 'channels-123.sqlite3').exists())

    def test_reconnection_requires_same_volume_and_preserves_archive(self):
        with ChannelStore(self.state, 123) as store:
            self.info['VolumeUUID'] = 'WRONG'
            with self.assertRaises(ChannelStoreError): store.channels()
        with self.assertRaises(ChannelStoreError): ChannelStore(self.state, 123)
        self.info['VolumeUUID'] = 'TEST-UUID'
        with ChannelStore(self.state, 123) as reconnected:
            self.assertEqual(reconnected.path, self.external)
            self.assertEqual(len(reconnected.channels()), 1)
        self.assertFalse((self.state / 'channels-123.sqlite3').exists())

    def test_missing_db_never_creates_file_or_falls_back(self):
        self.external.unlink()
        with self.assertRaises(ChannelStoreError): ChannelStore(self.state, 123)
        self.assertFalse(self.external.exists())
        self.assertFalse((self.state / 'channels-123.sqlite3').exists())

    def test_detached_mount_stops_idle_health_check(self):
        with ChannelStore(self.state, 123) as store:
            from kiwoom_autotrader.telegram.channel_storage import ExternalArchiveError
            with patch('kiwoom_autotrader.telegram.channel_storage._mount_identity', side_effect=ExternalArchiveError()):
                with self.assertRaises(ChannelStoreError): store.check_storage_health()

    def test_marker_removed_while_open_blocks_operations(self):
        with ChannelStore(self.state, 123) as store:
            self.marker.unlink()
            with self.assertRaises(ChannelStoreError): store.channels()

    def test_database_replaced_while_open_blocks_operations(self):
        with ChannelStore(self.state, 123) as store:
            self.external.rename(self.external.with_suffix('.old'))
            sqlite3.connect(self.external).close()
            with self.assertRaises(ChannelStoreError): store.channels()

    @unittest.skipIf(sys.platform == "win32", "shared lock requires POSIX")
    def test_cutover_exclusive_lock_refuses_while_shared_user_active(self):
        with ChannelStore(self.state, 123):
            fd = os.open(self.state / 'channel-storage.lock', os.O_RDWR)
            try:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                os.close(fd)
        fd = os.open(self.state / 'channel-storage.lock', os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(fd)

    def test_unconfigured_legacy_local_store_still_works(self):
        self.marker.unlink()
        with ChannelStore(self.state, 123) as store:
            self.assertEqual(store.path, self.state / 'channels-123.sqlite3')
            self.assertEqual(store.channels(), [])

if __name__ == '__main__': unittest.main()
