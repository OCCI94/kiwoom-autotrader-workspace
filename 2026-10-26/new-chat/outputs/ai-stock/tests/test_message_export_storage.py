import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from kiwoom_autotrader.main import main
from kiwoom_autotrader.telegram.channel_store import ExternalStorageUnavailable
from kiwoom_autotrader.utils.message_dataset import _output_guard, export_daily_dataset, MessageDatasetError
from kiwoom_autotrader.utils.message_export_storage import GuardedMessageExport


class ExportCLIStorageTests(unittest.TestCase):
    def invoke(self, argv, *, external=True, unavailable=False):
        store = SimpleNamespace(_external_archive=SimpleNamespace(
            mount=Path('/Volumes/AIStockData'), directory=Path('/Volumes/AIStockData/archive')) if external else None)
        store.check_storage_health = MagicMock(side_effect=ExternalStorageUnavailable('unavailable') if unavailable else None)
        context = MagicMock()
        context.__enter__.return_value = store
        with patch('kiwoom_autotrader.telegram.account_credentials.load_account_config', return_value=SimpleNamespace(user_id=123)), \
             patch('kiwoom_autotrader.utils.account_service.account_paths', return_value=SimpleNamespace(state=Path('/tmp/explicit-test-state'))), \
             patch('kiwoom_autotrader.telegram.channel_store.ChannelStore', return_value=context), \
             patch('kiwoom_autotrader.utils.message_dataset.export_daily_dataset', return_value={}) as export, \
             patch('kiwoom_autotrader.main.load_mock_credentials') as credentials, \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as errors:
            status = main(['messages-export', *argv])
        credentials.assert_not_called()
        return status, export, errors.getvalue()

    def test_default_follows_selected_collector_ssd_not_local_project(self):
        status, export, _ = self.invoke([])
        self.assertEqual(status, 0)
        self.assertEqual(export.call_args.args[1], Path('/Volumes/AIStockData/archive/daily'))

    def test_selected_ssd_unavailable_has_no_local_fallback(self):
        status, export, error = self.invoke([], unavailable=True)
        self.assertEqual(status, 1)
        export.assert_not_called()
        self.assertIn('다른 저장소로 대체하지 않습니다', error)

    def test_explicit_local_output_remains_deliberate_operator_choice(self):
        status, export, _ = self.invoke(['--output', '/tmp/explicit-offline-export'])
        self.assertEqual(status, 0)
        self.assertEqual(export.call_args.args[1], Path('/tmp/explicit-offline-export'))

    def test_unconfigured_legacy_local_default(self):
        status, export, _ = self.invoke([], external=False)
        self.assertEqual(status, 0)
        self.assertEqual(export.call_args.args[1], Path('/tmp/explicit-test-state/daily'))


class ExportPathGuardTests(unittest.TestCase):
    def store(self):
        return SimpleNamespace(_external_archive=SimpleNamespace(mount=Path('/Volumes/AIStockData'),
            directory=Path('/Volumes/AIStockData/archive')), check_storage_health=MagicMock())

    def test_generic_verified_mount_descendant_allowed(self):
        store = self.store()
        guard = _output_guard(store, Path('/Volumes/AIStockData/exports/research-daily'))
        guard()
        self.assertEqual(store.check_storage_health.call_count, 2)

    def test_other_mount_and_mount_root_rejected(self):
        for root in ('/Volumes/Other/daily', '/Volumes/AIStockData'):
            with self.assertRaises(MessageDatasetError):
                _output_guard(self.store(), Path(root))

    def test_non_daily_compatibility_alias_is_checked_each_use(self):
        with tempfile.TemporaryDirectory() as temp:
            link = Path(temp) / 'export-alias'
            link.symlink_to('/Volumes/AIStockData/exports/research-daily')
            guard = _output_guard(self.store(), link)
            link.unlink()
            link.symlink_to('/tmp/replaced-export')
            with self.assertRaises(MessageDatasetError):
                guard()

    def test_lexical_volume_path_redirected_local_cannot_become_local_export(self):
        with patch.object(Path, 'resolve', return_value=Path('/tmp/fake-volume')):
            with self.assertRaises(MessageDatasetError):
                _output_guard(self.store(), Path('/Volumes/AIStockData/exports/daily'))


class DescriptorExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.mount = self.root / 'volume'
        self.mount.mkdir()
        (self.mount / '.ssd-volume-sentinel').write_text('TEST-UUID\n')
        self.dest = self.mount / 'exports' / 'research-daily'
        self.archive = SimpleNamespace(mount=self.mount, directory=self.mount/'archive', config={'volume_uuid': 'TEST-UUID'}, verify=MagicMock())
        self.store = SimpleNamespace(_external_archive=self.archive)
        self.health = MagicMock()
        self.store.check_storage_health = self.health
        original_stat = os.stat

        def stat_result(path, *args, **kwargs):
            value = original_stat(path, *args, **kwargs)
            if isinstance(path, (str, Path)) and Path(path) == self.mount.parent:
                fields = list(value)
                fields[2] += 1  # Only simulate the host/mounted-device boundary.
                return os.stat_result(fields)
            return value

        self.patch_stat = patch('kiwoom_autotrader.utils.message_export_storage.os.stat', side_effect=stat_result)
        self.patch_mount = patch('kiwoom_autotrader.utils.message_export_storage.os.path.ismount', return_value=True)
        self.patch_stat.start()
        self.patch_mount.start()
        self.addCleanup(self.patch_stat.stop)
        self.addCleanup(self.patch_mount.stop)

    def writer(self):
        writer = GuardedMessageExport(self.store, self.dest, self.health)
        self.addCleanup(writer.close)
        return writer

    def test_guarded_export_uses_collector_verification_and_atomic_daily_writer(self):
        writer = self.writer()
        self.archive.verify.assert_called_once()
        writer.ensure_day('2026-10-29')
        self.assertTrue(writer.write('2026-10-29/messages.jsonl', b'{}\n'))
        self.assertFalse(writer.write('2026-10-29/messages.jsonl', b'{}\n'))
        self.assertEqual((self.dest / '2026-10-29/messages.jsonl').read_bytes(), b'{}\n')

    def test_complete_export_to_new_ssd_root_and_local_alias(self):
        self.store.account_id = 123
        self.store.dataset_revision = lambda: 0
        self.store.versions = lambda active_only: []
        result = export_daily_dataset(self.store, self.dest, as_of=1793250000)
        # Lock acquisition and export both validate the SSD anchor.
        self.assertGreaterEqual(self.archive.verify.call_count, 2)
        self.assertEqual(result['output_root'], str(self.dest))
        self.assertEqual(json.loads((self.dest / 'manifest.json').read_text())['version_count'], 0)
        alias = self.root / 'compat-export'
        alias.symlink_to(self.dest)
        cached = export_daily_dataset(self.store, alias, as_of=1793250000)
        self.assertTrue(cached['cached'])
        self.assertEqual(cached['output_root'], str(self.dest))

    def test_sentinel_change_stops_before_new_file(self):
        writer = self.writer()
        (self.mount / '.ssd-volume-sentinel').write_text('WRONG\n')
        with self.assertRaises(ValueError):
            writer.write('manifest.json', b'{}')
        self.assertFalse((self.dest / 'manifest.json').exists())

    def test_mount_replacement_never_creates_local_export_tree(self):
        writer = self.writer()
        detached = self.root / 'detached'
        self.mount.rename(detached)
        self.mount.mkdir()
        with self.assertRaises(ValueError):
            writer.write('manifest.json', b'{}')
        self.assertEqual(list(self.mount.iterdir()), [])

    def test_output_root_replacement_fails_closed(self):
        writer = self.writer()
        self.dest.rename(self.dest.with_name('previous'))
        self.dest.mkdir()
        with self.assertRaises(ValueError):
            writer.write('manifest.json', b'{}')
        self.assertEqual(list(self.dest.iterdir()), [])

    def test_source_health_failure_stops_daily_mkdir(self):
        writer = self.writer()
        self.health.side_effect = RuntimeError('unavailable')
        with self.assertRaises(RuntimeError):
            writer.ensure_day('2026-10-29')
        self.assertFalse((self.dest / '2026-10-29').exists())

    def test_existing_export_directory_is_private(self):
        self.dest.mkdir(parents=True)
        self.dest.chmod(0o755)
        self.writer()
        self.assertEqual(self.dest.stat().st_mode & 0o777, 0o700)

    def test_appledouble_is_not_dataset_content(self):
        import hashlib
        writer = self.writer()
        writer.ensure_day('2026-10-29')
        writer.write('2026-10-29/messages.jsonl', b'{}\n')
        (self.dest/'2026-10-29/._messages.jsonl').write_bytes(b'AppleDouble metadata')
        manifest = {'files': [{'path': '2026-10-29/messages.jsonl', 'sha256': hashlib.sha256(b'{}\n').hexdigest()}]}
        self.assertTrue(writer.files_exist(manifest))
        manifest['files'].append({'path': '2026-10-29/._messages.jsonl', 'sha256': hashlib.sha256(b'AppleDouble metadata').hexdigest()})
        self.assertFalse(writer.files_exist(manifest))

    def test_nested_symlink_and_parent_traversal_rejected(self):
        target = self.root / 'outside'
        target.mkdir()
        (self.mount / 'exports').symlink_to(target)
        with self.assertRaises(OSError):
            self.writer()
        self.assertEqual(list(target.iterdir()), [])
        with self.assertRaises(ValueError):
            GuardedMessageExport(self.store, self.mount / 'new' / '..' / '..' / 'outside', self.health)
        self.assertFalse((self.mount / 'new').exists())


if __name__ == '__main__':
    unittest.main()
