"""Credential fixtures live only in private temporary folders; no live key read."""
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from kiwoom_autotrader.trading import jev_credentials as credentials
from kiwoom_autotrader.trading.jev_news_client import JevNewsClient, JevUnavailable


class JevCredentialTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        # macOS /var can be a symlink. Resolve only this disposable fixture root;
        # the production loader must inspect the actual configured path itself.
        self.root = Path(self.temporary.name).resolve() / 'app'
        self.secret = self.root / 'config/secrets' / credentials.FILENAME
        self.secret.parent.mkdir(parents=True, mode=0o700)
        self.patcher = patch.object(credentials, 'app_data_root', return_value=self.root)
        self.patcher.start(); self.addCleanup(self.patcher.stop)
        env = patch.dict(os.environ, {}, clear=True)
        env.start(); self.addCleanup(env.stop)

    def write(self, raw=None, mode=0o600):
        raw = raw if raw is not None else json.dumps({'schema': credentials.SCHEMA, 'api_key': 'fixture-secret'}).encode()
        self.secret.write_bytes(raw); self.secret.chmod(mode)

    def test_environment_precedes_file_without_opening_it(self):
        with patch.object(credentials, '_read_fixed_file', side_effect=AssertionError('file touched')):
            self.assertEqual(credentials.load_jev_api_key(environ={'TYPESAFE_API_KEY': 'fixture-env'}), 'fixture-env')

    def test_invalid_environment_fails_without_fallback(self):
        self.write()
        for value in (None, 2, ' has-space', 'value\n', '비밀', '\x00', '\x7f', 'x' * 4097):
            with self.subTest(value_type=type(value).__name__):
                with self.assertRaisesRegex(credentials.JevCredentialUnavailable, '^credentials_invalid$'):
                    credentials.load_jev_api_key(environ={'TYPESAFE_API_KEY': value})

    def test_missing_and_empty_file_remain_unavailable(self):
        self.assertEqual(credentials.load_jev_api_key(), '')
        self.write(b'')
        self.assertEqual(credentials.load_jev_api_key(), '')
        self.write(json.dumps({'schema': credentials.SCHEMA, 'api_key': ''}).encode())
        self.assertEqual(credentials.load_jev_api_key(), '')

    def test_private_file_key_loads_and_client_repr_hides_it(self):
        self.write()
        self.assertEqual(credentials.load_jev_api_key(), 'fixture-secret')
        client = JevNewsClient()
        self.addCleanup(client.close)
        self.assertTrue(client.available)
        self.assertNotIn('fixture-secret', repr(client))

    def test_explicit_empty_key_never_loads_credentials(self):
        with patch('kiwoom_autotrader.trading.jev_news_client.load_jev_api_key', side_effect=AssertionError('loader touched')):
            client = JevNewsClient('')
            self.addCleanup(client.close)
            self.assertFalse(client.available)

    def test_modes_other_than_exact_0600_are_rejected(self):
        for mode in (0o000, 0o400, 0o644, 0o660, 0o700):
            self.write(mode=mode)
            with self.subTest(mode=oct(mode)):
                self.assertEqual(stat.S_IMODE(self.secret.stat().st_mode), mode)
                with self.assertRaisesRegex(credentials.JevCredentialUnavailable, '^credentials_unsafe$'):
                    credentials.load_jev_api_key()
            self.secret.chmod(0o600)

    def test_special_permission_bits_rejected_from_open_file_metadata(self):
        # macOS sandbox/filesystem policy may clear setuid after chmod succeeds.
        # Test that exact permission boundary using opened-file metadata, without
        # relying on the OS retaining a privilege bit on a disposable fixture.
        self.write()
        original = credentials.os.fstat
        for special in (stat.S_ISUID, stat.S_ISGID, stat.S_ISVTX):
            def with_special_bit(fd):
                fields = list(original(fd)); fields[stat.ST_MODE] |= special
                return os.stat_result(fields)
            with self.subTest(special=oct(special)):
                with patch.object(credentials.os, 'fstat', side_effect=with_special_bit):
                    with self.assertRaisesRegex(credentials.JevCredentialUnavailable, '^credentials_unsafe$'):
                        credentials.load_jev_api_key()

    def test_symlink_file_is_not_followed(self):
        target = self.secret.with_name('other.json')
        target.write_text('private-fixture'); target.chmod(0o600)
        self.secret.symlink_to(target)
        with self.assertRaisesRegex(credentials.JevCredentialUnavailable, '^credentials_unsafe$'):
            credentials.load_jev_api_key()

    def test_symlink_parent_even_internal_target_is_rejected(self):
        original = self.secret.parent; moved = original.with_name('other-secrets')
        original.rename(moved); original.symlink_to(moved, target_is_directory=True)
        with self.assertRaisesRegex(credentials.JevCredentialUnavailable, '^credentials_unsafe$'):
            credentials.load_jev_api_key()

    def test_nonregular_fifo_and_directory_never_block(self):
        self.secret.mkdir()
        with self.assertRaisesRegex(credentials.JevCredentialUnavailable, '^credentials_unsafe$'):
            credentials.load_jev_api_key()
        self.secret.rmdir()
        os.mkfifo(self.secret, 0o600)
        with self.assertRaisesRegex(credentials.JevCredentialUnavailable, '^credentials_unsafe$'):
            credentials.load_jev_api_key()

    def test_hardlink_and_wrong_owner_rejected(self):
        self.write(); os.link(self.secret, self.secret.with_name('hardlink.json'))
        with self.assertRaisesRegex(credentials.JevCredentialUnavailable, '^credentials_unsafe$'):
            credentials.load_jev_api_key()
        self.secret.with_name('hardlink.json').unlink()
        with patch.object(credentials.os, 'getuid', return_value=os.getuid() + 10000):
            with self.assertRaisesRegex(credentials.JevCredentialUnavailable, '^credentials_unsafe$'):
                credentials.load_jev_api_key()

    def test_invalid_json_duplicates_schema_key_type_and_size(self):
        cases = [b'{', b'[]', b'{"schema":"jev-api-credentials-v1","api_key":"a","api_key":"b"}',
                 b'{"schema":"other","api_key":"a"}',
                 b'{"schema":"jev-api-credentials-v1","api_key":true}',
                 b'{"schema":"jev-api-credentials-v1","api_key":"a","extra":0}',
                 b'{"schema":"jev-api-credentials-v1","api_key":NaN}', b'\xff', b'x' * 4097]
        for raw in cases:
            self.write(raw)
            with self.subTest(length=len(raw)):
                with self.assertRaisesRegex(credentials.JevCredentialUnavailable, '^credentials_invalid$'):
                    credentials.load_jev_api_key()

    def test_mutation_between_fstat_checks_rejected(self):
        self.write(); original = credentials.os.fstat; calls = []
        def fstat(fd):
            value = original(fd); calls.append(1)
            if len(calls) == 2:
                fields = list(value); fields[stat.ST_SIZE] += 1
                return os.stat_result(fields)
            return value
        with patch.object(credentials.os, 'fstat', side_effect=fstat):
            with self.assertRaisesRegex(credentials.JevCredentialUnavailable, '^credentials_unsafe$'):
                credentials.load_jev_api_key()

    def test_client_unsafe_reason_never_exposes_key_path_or_underlying_error(self):
        self.write(mode=0o644)
        with self.assertRaises(JevUnavailable) as caught:
            JevNewsClient()
        self.assertEqual(str(caught.exception), 'credentials_unsafe')
        self.assertNotIn(str(self.secret), repr(caught.exception))
        self.assertNotIn('fixture-secret', repr(caught.exception))

    def test_private_fallback_does_not_change_cwd_or_read_other_file(self):
        self.write()
        with patch.object(credentials, 'app_data_root', return_value=Path('relative')):
            with self.assertRaisesRegex(credentials.JevCredentialUnavailable, '^credentials_unsafe$'):
                credentials.load_jev_api_key()


if __name__ == '__main__':
    unittest.main()
