"""Offline tests for the isolated personal-account Keychain record."""

import json
import traceback
import unittest
from unittest.mock import patch

from kiwoom_autotrader import credentials as keychain
from kiwoom_autotrader import telegram_account_credentials as storage


class AccountStorageTests(unittest.TestCase):
    def setUp(self):
        self.values = {
            "api_id": 123456, "api_hash": "abcdef0123456789" * 2,
            "session": "1" + "safe_test_session_" * 21, "user_id": 234567890,
        }
        self.config = storage.AccountConfig(**self.values)
        backend = patch.object(storage, "_get_backend")
        self.addCleanup(backend.stop)
        self.factory = backend.start()
        self.backend = self.factory.return_value

    def encoded(self, **changes):
        return json.dumps({"version": 1, **self.values, **changes}).encode()

    def assert_private(self, text):
        for value in self.values.values():
            self.assertNotIn(str(value), text)

    def test_dataclass_repr_hides_every_value(self):
        self.assert_private(repr(self.config))
        self.assert_private(str(self.config))

    def test_validation_rejects_bool_ids_and_malformed_hash_or_session(self):
        for key, invalid in {
            "api_id": [True, 0, -1, "1234", 1 << 31],
            "api_hash": [None, "", "g" * 32, "a" * 33, "a" * 32 + "\n"],
            "session": [None, "", "test.session", "1" + "a" * 99, "1" + "a" * 4100],
            "user_id": [True, 0, -1, "1234", 1 << 52],
        }.items():
            for value in invalid:
                with self.subTest(key=key, type=type(value).__name__):
                    with self.assertRaises(ValueError) as caught:
                        storage.AccountConfig(**dict(self.values, **{key: value}))
                    self.assert_private(str(caught.exception))

    def test_roundtrip_saves_one_complete_atomic_record(self):
        self.backend.update.return_value = 0
        storage.save_account_config(self.config)
        raw = self.backend.update.call_args.args[0]
        self.assertEqual(json.loads(raw), {"version": 1, **self.values})
        self.backend.load.return_value = (0, raw)
        self.assertEqual(storage.load_account_config(), self.config)
        self.backend.delete.assert_not_called()
        self.backend.add.assert_not_called()

    def test_only_not_found_means_missing(self):
        self.backend.load.return_value = (-25300, None)
        self.assertIsNone(storage.load_account_config())
        for status in (-25308, -25293, -128):
            self.backend.load.return_value = (status, None)
            with self.assertRaises(keychain.KeychainError):
                storage.load_account_config()

    def test_malformed_record_is_sanitized_and_not_partially_loaded(self):
        for raw in (b"", b"[]", b"{", b"\xff", None,
                    self.encoded(version=True), self.encoded(version=2),
                    self.encoded(extra="private"), self.encoded(user_id=True),
                    json.dumps({"version": 1, "api_hash": self.values["api_hash"]}).encode(),
                    b"x" * (keychain._MAX_PAYLOAD_BYTES + 1)):
            self.backend.load.return_value = (0, raw)
            with self.assertRaises(keychain.KeychainError) as caught:
                storage.load_account_config()
            self.assert_private("".join(traceback.format_exception(caught.exception)))

    def test_missing_record_adds_without_deleting_other_services(self):
        self.backend.update.return_value = -25300
        self.backend.add.return_value = 0
        storage.save_account_config(self.config)
        self.backend.add.assert_called_once_with(self.backend.update.call_args.args[0])
        self.backend.delete.assert_not_called()

    def test_native_error_does_not_expose_session(self):
        self.backend.update.side_effect = RuntimeError(self.config.session)
        with self.assertRaises(keychain.KeychainError) as caught:
            storage.save_account_config(self.config)
        self.assert_private("".join(traceback.format_exception(caught.exception)))

    def test_invalid_save_does_not_access_keychain(self):
        for value in (None, {}, "fixture"):
            with self.assertRaises(ValueError):
                storage.save_account_config(value)
        self.factory.assert_not_called()

    def test_delete_is_scoped_and_distinguishes_absent(self):
        self.backend.delete.return_value = 0
        self.assertTrue(storage.delete_account_config())
        self.backend.delete.return_value = -25300
        self.assertFalse(storage.delete_account_config())
        self.backend.delete.return_value = -25308
        with self.assertRaises(keychain.KeychainError):
            storage.delete_account_config()

class AccountServiceIsolationTests(unittest.TestCase):
    def test_native_adapter_is_called_with_fixed_account_service(self):
        with patch.object(keychain, "_get_backend") as native:
            self.assertIs(storage._get_backend(), native.return_value)
            native.assert_called_once_with(
                service="com.local.kiwoom-autotrader.telegram-account", account="default",
            )
        self.assertIn(storage._SERVICE, keychain._ALLOWED_SERVICES)


if __name__ == "__main__":
    unittest.main()
