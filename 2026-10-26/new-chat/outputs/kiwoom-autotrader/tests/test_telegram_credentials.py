"""Offline tests: every Keychain operation is mocked; no Telegram requests."""

import json
import traceback
import unittest
from unittest.mock import MagicMock, patch

from kiwoom_autotrader import credentials as keychain
from kiwoom_autotrader import telegram_credentials as storage
from kiwoom_autotrader.telegram_credentials import (
    KeychainError, TelegramConfig, delete_telegram_config,
    load_telegram_config, save_telegram_config,
)


class TelegramConfigTests(unittest.TestCase):
    def setUp(self):
        self.values = {
            "bot_token": "123456789:" + "test_only_not_a_real_token_1234567890",
            "chat_id": 987654321,
            "bot_id": 123456789,
            "bot_username": "offline_test_bot",
        }

    def test_all_repr_fields_are_hidden(self):
        config = TelegramConfig(**self.values)
        for displayed in (repr(config), str(config)):
            for value in self.values.values():
                self.assertNotIn(str(value), displayed)

    def test_positive_token_prefix_does_not_require_an_arbitrary_minimum_length(self):
        values = dict(self.values, bot_token="1:" + "x" * 20, bot_id=1)
        self.assertEqual(TelegramConfig(**values).bot_id, 1)

    def test_invalid_fields_are_rejected_without_echoing_inputs(self):
        invalid = {
            "bot_token": (None, "", "1234:short", "123456:with space", "123456:" + "x" * 201,
                          self.values["bot_token"] + "/", self.values["bot_token"] + "\n"),
            "chat_id": (None, True, False, 0, -123, "987654321", 1.5),
            "bot_id": (None, True, 0, -1, "123456789", 123456788),
            "bot_username": (None, "", "abcd", "x" * 33, "@unsafe_bot", "bad/bot", "한글봇", "valid_bot\n"),
        }
        for field, values in invalid.items():
            for value in values:
                config_values = dict(self.values, **{field: value})
                with self.subTest(field=field, value_type=type(value).__name__):
                    with self.assertRaises(ValueError) as caught:
                        TelegramConfig(**config_values)
                    for secret in self.values.values():
                        self.assertNotIn(str(secret), str(caught.exception))


class TelegramStorageTests(unittest.TestCase):
    def setUp(self):
        backend_patch = patch.object(storage, "_get_backend")
        self.addCleanup(backend_patch.stop)
        self.backend_factory = backend_patch.start()
        self.backend = self.backend_factory.return_value
        self.values = {
            "bot_token": "123456789:" + "test_only_not_a_real_token_1234567890",
            "chat_id": 987654321,
            "bot_id": 123456789,
            "bot_username": "offline_test_bot",
        }
        self.config = TelegramConfig(**self.values)

    def encoded(self, **changes):
        return json.dumps({"version": 1, **self.values, **changes}).encode("utf-8")

    def assert_redacted(self, text):
        for value in self.values.values():
            self.assertNotIn(str(value), text)

    def test_load_valid_config(self):
        self.backend.load.return_value = (0, self.encoded())
        self.assertEqual(load_telegram_config(), self.config)
        self.backend.load.assert_called_once_with()

    def test_only_not_found_returns_none(self):
        self.backend.load.return_value = (-25300, None)
        self.assertIsNone(load_telegram_config())
        for status in (-25293, -25308, -128, -50):
            self.backend.load.return_value = (status, self.encoded())
            with self.assertRaises(KeychainError) as caught:
                load_telegram_config()
            self.assertIn(f"OSStatus={status}", str(caught.exception))
            self.assert_redacted(str(caught.exception))

    def test_malformed_or_partial_data_fail_closed(self):
        malformed = (
            None, b"", b"[]", b"{", b"\xff", self.encoded(version=True), self.encoded(version=2),
            self.encoded(chat_id=-1), self.encoded(bot_id=123456780),
            self.encoded(bot_username="unsafe/name"), self.encoded(extra="unexpected"),
            json.dumps({"version": 1, "bot_token": self.values["bot_token"]}).encode(),
            b"x" * (keychain._MAX_PAYLOAD_BYTES + 1),
        )
        for raw in malformed:
            self.backend.load.return_value = (0, raw)
            try:
                load_telegram_config()
            except KeychainError as error:
                self.assert_redacted("".join(traceback.format_exception(error)))
            else:
                self.fail("Malformed Telegram item must fail closed")

    def test_save_is_one_stable_complete_json_payload(self):
        self.backend.update.return_value = 0
        save_telegram_config(self.config)
        raw = self.backend.update.call_args.args[0]
        self.assertIsInstance(raw, bytes)
        self.assertEqual(json.loads(raw), {"version": 1, **self.values})
        self.backend.load.return_value = (0, raw)
        self.assertEqual(load_telegram_config(), self.config)
        self.backend.add.assert_not_called()
        self.backend.delete.assert_not_called()

    def test_missing_item_is_added_without_delete(self):
        self.backend.update.return_value = -25300
        self.backend.add.return_value = 0
        save_telegram_config(self.config)
        self.backend.add.assert_called_once_with(self.backend.update.call_args.args[0])
        self.backend.delete.assert_not_called()

    def test_concurrent_add_retries_complete_update(self):
        self.backend.update.side_effect = [-25300, 0]
        self.backend.add.return_value = -25299
        save_telegram_config(self.config)
        self.assertEqual(self.backend.update.call_count, 2)
        self.assertEqual(self.backend.update.call_args_list[0], self.backend.update.call_args_list[1])
        self.backend.add.assert_called_once()
        self.backend.delete.assert_not_called()

    def test_race_retries_are_bounded(self):
        self.backend.update.return_value = -25300
        self.backend.add.return_value = -25299
        with self.assertRaises(KeychainError):
            save_telegram_config(self.config)
        self.assertEqual(self.backend.update.call_count, 3)
        self.assertEqual(self.backend.add.call_count, 3)
        self.backend.delete.assert_not_called()

    def test_update_and_add_failures_do_not_delete_or_fallback(self):
        self.backend.update.return_value = -25308
        with self.assertRaisesRegex(KeychainError, "OSStatus=-25308"):
            save_telegram_config(self.config)
        self.backend.add.assert_not_called()
        self.backend.update.return_value = -25300
        self.backend.add.return_value = -25293
        with self.assertRaisesRegex(KeychainError, "OSStatus=-25293"):
            save_telegram_config(self.config)
        self.backend.delete.assert_not_called()

    def test_invalid_save_input_never_accesses_keychain(self):
        for value in (None, {}, self.values["bot_token"]):
            with self.assertRaises(ValueError) as caught:
                save_telegram_config(value)
            self.assert_redacted(str(caught.exception))
        self.backend_factory.assert_not_called()

    def test_delete_distinguishes_absent_from_denied(self):
        self.backend.delete.return_value = 0
        self.assertTrue(delete_telegram_config())
        self.backend.delete.return_value = -25300
        self.assertFalse(delete_telegram_config())
        self.backend.delete.return_value = -128
        with self.assertRaisesRegex(KeychainError, "OSStatus=-128"):
            delete_telegram_config()

    def test_native_exception_detail_is_suppressed(self):
        for method, action in (
            ("load", load_telegram_config),
            ("update", lambda: save_telegram_config(self.config)),
            ("delete", delete_telegram_config),
        ):
            getattr(self.backend, method).side_effect = OSError(self.values["bot_token"])
            try:
                action()
            except KeychainError as error:
                self.assert_redacted("".join(traceback.format_exception(error)))
            else:
                self.fail("Native error must be sanitized")


class TelegramStoreIsolationTests(unittest.TestCase):
    def test_factory_selects_telegram_service_and_default_account(self):
        with patch.object(keychain, "_get_backend") as create:
            self.assertIs(storage._get_backend(), create.return_value)
            create.assert_called_once_with(
                service="com.local.kiwoom-autotrader.telegram", account="default"
            )

    def test_telegram_query_uses_instance_store_without_acl_changes(self):
        backend = keychain._NativeKeychain.__new__(keychain._NativeKeychain)
        backend._service = "com.local.kiwoom-autotrader.telegram"
        backend._account = "default"
        backend.cf = MagicMock()
        backend._constants = {"kSecClassGenericPassword": 20}
        backend._string = MagicMock(side_effect=[21, 22])
        backend._dictionary = MagicMock(return_value=23)
        with backend._query() as query:
            self.assertEqual(query, 23)
        self.assertEqual([call.args[0] for call in backend._string.call_args_list],
                         ["com.local.kiwoom-autotrader.telegram", "default"])
        self.assertEqual(backend._dictionary.call_args.args[0],
                         [("kSecClass", 20), ("kSecAttrService", 21), ("kSecAttrAccount", 22)])

    def test_native_constructor_rejects_uncontrolled_stores_before_loading(self):
        with patch.object(keychain.ctypes, "CDLL") as loader:
            for service, account in (("unrelated-service", "default"),
                                     (storage._SERVICE, "other-account"), (None, "default")):
                with self.assertRaises(ValueError):
                    keychain._NativeKeychain(service=service, account=account)
            loader.assert_not_called()

    def test_non_macos_fails_without_loading_frameworks(self):
        with patch.object(keychain.sys, "platform", "linux"), patch.object(keychain.ctypes, "CDLL") as loader:
            for action in (load_telegram_config, delete_telegram_config):
                with self.assertRaisesRegex(KeychainError, "macOS"):
                    action()
            loader.assert_not_called()

    def test_keychain_error_is_the_existing_public_exception(self):
        self.assertIs(KeychainError, keychain.KeychainError)


if __name__ == "__main__":
    unittest.main()
