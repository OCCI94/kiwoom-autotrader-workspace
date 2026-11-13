import tempfile
from pathlib import Path
import unittest

from kiwoom_autotrader.utils.external_storage import ExternalDataRoot, ExternalStorageUnavailable


class ExternalDataRootTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.mount = self.base / 'volume'
        self.mount.mkdir()
        self.root = self.mount / 'data'
        self.root.mkdir()
        self.uuid = '7440E396-8F62-4205-9EC6-16F02440C7C5'
        self.info = {'VolumeUUID':self.uuid, 'MountPoint':str(self.mount),
                     'FilesystemType':'apfs', 'WritableVolume':True}
        self.mounted = True

    def create(self, **overrides):
        params = {'mount':self.mount, 'data_root':self.root, 'volume_uuid':self.uuid,
                  'engine_root':self.base/'engine', 'control_root':self.base/'control',
                  '_inspector':lambda path:self.info, '_is_mount':lambda path:self.mounted}
        params.update(overrides)
        return ExternalDataRoot(**params)

    def test_existing_volume_resolves_without_creating_files(self):
        result = self.create().require_available()
        self.assertEqual(result.execution_logs, self.root/'logs')
        self.assertEqual(result.settings, self.root/'config/data/settings.json')
        self.assertEqual(result.telegram_account, self.root/'telegram-account')
        self.assertEqual(result.research, self.root/'research')
        self.assertEqual(list(self.root.iterdir()), [])

    def test_missing_mount_or_data_root_fails_without_fallback(self):
        self.root.rmdir()
        with self.assertRaises(ExternalStorageUnavailable): self.create()
        self.assertFalse(self.root.exists())
        self.assertFalse((self.base/'control').exists())

    def test_directory_is_not_enough_if_volume_is_unmounted(self):
        self.mounted = False
        with self.assertRaises(ExternalStorageUnavailable): self.create()

    def test_missing_or_wrong_volume_identity(self):
        for key, value in [('VolumeUUID','00000000-0000-0000-0000-000000000000'),
                           ('MountPoint',str(self.base)), ('FilesystemType','exfat'),
                           ('WritableVolume',False)]:
            original = self.info[key]
            self.info[key] = value
            with self.subTest(key=key), self.assertRaises(ExternalStorageUnavailable): self.create()
            self.info[key] = original

    def test_later_disconnect_and_wrong_uuid_fail(self):
        root = self.create()
        self.mounted = False
        with self.assertRaises(ExternalStorageUnavailable): root.require_available()
        self.mounted = True
        with self.assertRaisesRegex(ExternalStorageUnavailable, 'latched'): root.require_available()
        root = self.create()
        self.info['VolumeUUID'] = '00000000-0000-0000-0000-000000000000'
        with self.assertRaises(ExternalStorageUnavailable): root.require_available()

    def test_recreated_root_is_not_silently_accepted(self):
        root = self.create()
        old = self.mount/'old'
        self.root.rename(old)
        self.root.mkdir()
        with self.assertRaises(ExternalStorageUnavailable): root.require_available()

    def test_rejects_symlinks_and_paths_outside_mount(self):
        link = self.mount/'link'
        link.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(ExternalStorageUnavailable): self.create(data_root=link)
        with self.assertRaises(ExternalStorageUnavailable): self.create(data_root=self.base)

    def test_rejects_internal_paths_inside_external_volume(self):
        with self.assertRaises(ExternalStorageUnavailable): self.create(engine_root=self.mount)
        with self.assertRaises(ExternalStorageUnavailable): self.create(control_root=self.root/'control')

    def test_requires_explicit_valid_uuid_and_absolute_paths(self):
        with self.assertRaises(ExternalStorageUnavailable): self.create(volume_uuid='')
        with self.assertRaises(ExternalStorageUnavailable): self.create(mount=Path('relative'))
        with self.assertRaises(ExternalStorageUnavailable): self.create(control_root=Path('relative'))


if __name__ == '__main__': unittest.main()
