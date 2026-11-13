"""Offline Keychain boundary tests. Never inspect or mutate real keychain items."""

import ctypes
import json
import traceback
import unittest
from contextlib import nullcontext
from unittest.mock import MagicMock, patch

from kiwoom_autotrader import credentials as storage
from kiwoom_autotrader.credentials import (
    Credentials, KeychainError, delete_mock_credentials,
    load_mock_credentials, save_mock_credentials,
)


class CredentialStorageTests(unittest.TestCase):
    def setUp(self):
        backend_patch = patch.object(storage, "_get_backend")
        self.addCleanup(backend_patch.stop)
        self.backend_factory = backend_patch.start()
        self.backend = self.backend_factory.return_value
        self.pair = Credentials("offline-test-key", "offline-test-secret")

    def encoded(self, **changes):
        payload = {"version": 1, "app_key": self.pair.app_key, "app_secret": self.pair.app_secret}
        payload.update(changes)
        return json.dumps(payload).encode("utf-8")

    def assert_redacted(self, value):
        for secret in (self.pair.app_key, self.pair.app_secret):
            self.assertNotIn(secret, value)

    def test_credentials_repr_and_str_hide_values(self):
        self.assert_redacted(repr(self.pair))
        self.assert_redacted(str(self.pair))

    def test_credentials_reject_empty_or_non_string_values(self):
        for value in ("", " \t\n", None, 1, b"bytes"):
            for pair in ((value, self.pair.app_secret), (self.pair.app_key, value)):
                with self.subTest(value=value), self.assertRaises(ValueError) as caught:
                    Credentials(*pair)
                self.assert_redacted(str(caught.exception))
        self.backend_factory.assert_not_called()

    def test_load_valid_pair(self):
        self.backend.load.return_value = (0, self.encoded())
        self.assertEqual(load_mock_credentials(), self.pair)
        self.backend.load.assert_called_once_with()

    def test_load_not_found_only_returns_none(self):
        self.backend.load.return_value = (-25300, None)
        self.assertIsNone(load_mock_credentials())
        for status in (-25293, -25308, -128, -50):
            with self.subTest(status=status):
                self.backend.load.return_value = (status, self.encoded())
                with self.assertRaises(KeychainError) as caught:
                    load_mock_credentials()
                self.assertIn(f"OSStatus={status}", str(caught.exception))
                self.assert_redacted(str(caught.exception))

    def test_load_malformed_data_fails_without_revealing_payload(self):
        malformed = (
            None, b"", b"\xffoffline-test-secret", b"offline-test-secret",
            b"[]", self.encoded(version=True), self.encoded(version=2),
            self.encoded(app_key=""), self.encoded(app_secret=None),
            self.encoded(extra="offline-test-secret"),
            b"x" * (storage._MAX_PAYLOAD_BYTES + 1),
        )
        for raw in malformed:
            with self.subTest(raw_type=type(raw).__name__):
                self.backend.load.return_value = (0, raw)
                try:
                    load_mock_credentials()
                except KeychainError as error:
                    self.assert_redacted("".join(traceback.format_exception(error)))
                else:
                    self.fail("Malformed stored data must be rejected")

    def test_update_writes_both_fields_as_one_json_payload(self):
        self.backend.update.return_value = 0
        save_mock_credentials(self.pair)
        raw = self.backend.update.call_args.args[0]
        self.assertIsInstance(raw, bytes)
        self.assertEqual(json.loads(raw), json.loads(self.encoded()))
        self.backend.add.assert_not_called()
        self.backend.delete.assert_not_called()

    def test_save_round_trip_preserves_unicode_quotes_and_null(self):
        pair = Credentials('테스트"key\x00', 'secret\\한글\n')
        self.backend.update.return_value = 0
        save_mock_credentials(pair)
        self.backend.load.return_value = (0, self.backend.update.call_args.args[0])
        self.assertEqual(load_mock_credentials(), pair)

    def test_missing_item_is_added_without_deleting_anything(self):
        self.backend.update.return_value = -25300
        self.backend.add.return_value = 0
        save_mock_credentials(self.pair)
        self.backend.add.assert_called_once_with(self.backend.update.call_args.args[0])
        self.backend.delete.assert_not_called()

    def test_concurrent_add_retries_update_with_same_complete_payload(self):
        self.backend.update.side_effect = [-25300, 0]
        self.backend.add.return_value = -25299
        save_mock_credentials(self.pair)
        self.assertEqual(self.backend.update.call_count, 2)
        self.assertEqual(self.backend.add.call_count, 1)
        self.assertEqual(self.backend.update.call_args_list[0], self.backend.update.call_args_list[1])
        self.backend.delete.assert_not_called()

    def test_concurrent_delete_and_add_can_retry_but_are_bounded(self):
        self.backend.update.return_value = -25300
        self.backend.add.return_value = -25299
        with self.assertRaises(KeychainError) as caught:
            save_mock_credentials(self.pair)
        self.assertIn("OSStatus=-25299", str(caught.exception))
        self.assertEqual(self.backend.update.call_count, 3)
        self.assertEqual(self.backend.add.call_count, 3)
        self.backend.delete.assert_not_called()

    def test_failed_update_is_not_treated_as_missing(self):
        self.backend.update.return_value = -25308
        with self.assertRaises(KeychainError) as caught:
            save_mock_credentials(self.pair)
        self.assertIn("OSStatus=-25308", str(caught.exception))
        self.backend.add.assert_not_called()
        self.backend.delete.assert_not_called()

    def test_failed_add_stops_without_removing_existing_item(self):
        self.backend.update.return_value = -25300
        self.backend.add.return_value = -25293
        with self.assertRaises(KeychainError) as caught:
            save_mock_credentials(self.pair)
        self.assertIn("OSStatus=-25293", str(caught.exception))
        self.assert_redacted(str(caught.exception))
        self.backend.update.assert_called_once()
        self.backend.delete.assert_not_called()

    def test_invalid_save_input_never_accesses_keychain(self):
        for value in (None, {}, "offline-test-secret"):
            with self.assertRaises(ValueError) as caught:
                save_mock_credentials(value)
            self.assert_redacted(str(caught.exception))
        self.backend_factory.assert_not_called()

    def test_delete_reports_only_actual_missing_as_false(self):
        self.backend.delete.return_value = 0
        self.assertTrue(delete_mock_credentials())
        self.backend.delete.return_value = -25300
        self.assertFalse(delete_mock_credentials())
        self.backend.delete.return_value = -128
        with self.assertRaises(KeychainError) as caught:
            delete_mock_credentials()
        self.assertIn("OSStatus=-128", str(caught.exception))

    def test_unexpected_native_errors_suppress_sensitive_cause(self):
        for method, action in (
            ("load", load_mock_credentials),
            ("update", lambda: save_mock_credentials(self.pair)),
            ("delete", delete_mock_credentials),
        ):
            with self.subTest(method=method):
                getattr(self.backend, method).side_effect = RuntimeError(self.pair.app_secret)
                try:
                    action()
                except KeychainError as error:
                    self.assert_redacted("".join(traceback.format_exception(error)))
                else:
                    self.fail("Native boundary exception must be sanitized")


class NativeBoundaryTests(unittest.TestCase):
    def test_non_macos_fails_without_loading_frameworks(self):
        with patch.object(storage.sys, "platform", "linux"), patch.object(storage.ctypes, "CDLL") as loader:
            for action in (load_mock_credentials, delete_mock_credentials,
                           lambda: save_mock_credentials(Credentials("test-key", "test-secret"))):
                with self.assertRaisesRegex(KeychainError, "macOS"):
                    action()
            loader.assert_not_called()

    def test_framework_load_failure_has_no_raw_exception_details(self):
        with patch.object(storage.sys, "platform", "darwin"), patch.object(
            storage.ctypes, "CDLL", side_effect=OSError("private-framework-detail")
        ):
            try:
                load_mock_credentials()
            except KeychainError as error:
                self.assertNotIn("private-framework-detail", "".join(traceback.format_exception(error)))
            else:
                self.fail("Framework load failure must be reported")

    def native_double(self):
        backend = storage._NativeKeychain.__new__(storage._NativeKeychain)
        backend._service = storage._SERVICE
        backend._account = storage._ACCOUNT
        backend.cf = MagicMock()
        backend.security = MagicMock()
        backend._true = 10
        backend._constants = {"kSecMatchLimitOne": 11}
        backend._query = MagicMock(return_value=nullcontext(12))
        return backend

    def test_successful_copy_rejects_null_result_before_dereference(self):
        backend = self.native_double()
        backend.security.SecItemCopyMatching.return_value = 0
        with self.assertRaises(KeychainError):
            backend.load()
        backend.cf.CFGetTypeID.assert_not_called()
        backend.cf.CFDataGetBytePtr.assert_not_called()
        backend.cf.CFRelease.assert_not_called()

    def test_failed_copy_does_not_attempt_to_read_data(self):
        backend = self.native_double()
        backend.security.SecItemCopyMatching.return_value = -25308
        self.assertEqual(backend.load(), (-25308, None))
        backend.cf.CFGetTypeID.assert_not_called()
        backend.cf.CFDataGetLength.assert_not_called()

    def test_wrong_cf_type_is_released_without_reading_bytes(self):
        backend = self.native_double()

        def copy_result(query, result):
            ctypes.cast(result, ctypes.POINTER(ctypes.c_void_p))[0] = 123
            return 0

        backend.security.SecItemCopyMatching.side_effect = copy_result
        backend.cf.CFGetTypeID.return_value = 1
        backend.cf.CFDataGetTypeID.return_value = 2
        with self.assertRaises(KeychainError):
            backend.load()
        backend.cf.CFDataGetLength.assert_not_called()
        backend.cf.CFRelease.assert_called_once()
        self.assertEqual(backend.cf.CFRelease.call_args.args[0].value, 123)

    def test_cfdata_is_copied_before_its_reference_is_released(self):
        backend = self.native_double()
        expected = b"offline-byte-copy"
        buffer = (ctypes.c_ubyte * len(expected)).from_buffer_copy(expected)

        def copy_result(query, result):
            ctypes.cast(result, ctypes.POINTER(ctypes.c_void_p))[0] = 123
            return 0

        backend.security.SecItemCopyMatching.side_effect = copy_result
        backend.cf.CFGetTypeID.return_value = 1
        backend.cf.CFDataGetTypeID.return_value = 1
        backend.cf.CFDataGetLength.return_value = len(expected)
        backend.cf.CFDataGetBytePtr.return_value = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))
        self.assertEqual(backend.load(), (0, expected))
        backend.cf.CFRelease.assert_called_once()

    def test_cfdata_size_and_null_byte_pointer_are_checked_before_read(self):
        for length in (0, -1, storage._MAX_PAYLOAD_BYTES + 1, 16):
            with self.subTest(length=length):
                backend = self.native_double()

                def copy_result(query, result):
                    ctypes.cast(result, ctypes.POINTER(ctypes.c_void_p))[0] = 123
                    return 0

                backend.security.SecItemCopyMatching.side_effect = copy_result
                backend.cf.CFGetTypeID.return_value = 1
                backend.cf.CFDataGetTypeID.return_value = 1
                backend.cf.CFDataGetLength.return_value = length
                backend.cf.CFDataGetBytePtr.return_value = None
                with patch.object(storage.ctypes, "string_at") as read:
                    with self.assertRaises(KeychainError):
                        backend.load()
                    read.assert_not_called()
                if length != 16:
                    backend.cf.CFDataGetBytePtr.assert_not_called()
                backend.cf.CFRelease.assert_called_once()

    def test_fixed_mock_query_does_not_change_access_controls(self):
        backend = self.native_double()
        del backend._query
        backend._constants["kSecClassGenericPassword"] = 20
        backend._string = MagicMock(side_effect=[21, 22])
        backend._dictionary = MagicMock(return_value=23)
        with backend._query() as query:
            self.assertEqual(query, 23)
        self.assertEqual([call.args[0] for call in backend._string.call_args_list],
                         ["com.local.kiwoom-autotrader.mock", "default"])
        self.assertEqual(backend._dictionary.call_args.args[0],
                         [("kSecClass", 20), ("kSecAttrService", 21), ("kSecAttrAccount", 22)])
        self.assertEqual([call.args[0] for call in backend.cf.CFRelease.call_args_list], [23, 22, 21])


if __name__ == "__main__":
    unittest.main()
