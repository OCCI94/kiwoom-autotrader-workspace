"""Mock Windows boundaries and inspect launcher text; no Windows host is used."""

import builtins
import contextlib
import errno
import importlib.util
import io
from pathlib import Path
import re
import sys
import tempfile
import traceback
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, call, patch

from kiwoom_autotrader.utils import keychain, paths, platform_io


SECRET = "private-windows-test-secret"
ROOT = Path(__file__).resolve().parents[1]


class MemoryVault:
    def __init__(self):
        self.values = {}
        self.get_password = Mock(side_effect=lambda service, account: self.values.get((service, account)))
        self.set_password = Mock(side_effect=lambda service, account, value: self.values.__setitem__((service, account), value))
        self.delete_password = Mock(side_effect=lambda service, account: self.values.pop((service, account)))


class WindowsCredentialTests(unittest.TestCase):
    def setUp(self):
        self.vault = MemoryVault()
        self.backend = keychain._WindowsCredentialVault(vault=self.vault)

    def test_native_wrapper_preserves_unicode_and_updates_one_complete_item(self):
        raw = '{"값":"한글😀\\n' + SECRET + '"}'
        self.assertEqual(self.backend.add(raw.encode()), keychain._SUCCESS)
        self.assertEqual(self.backend.load(), (keychain._SUCCESS, raw.encode()))
        replacement = '{"값":"새 비밀"}'.encode()
        self.assertEqual(self.backend.update(replacement), keychain._SUCCESS)
        self.assertEqual(self.backend.load(), (keychain._SUCCESS, replacement))
        self.vault.delete_password.assert_not_called()
        self.vault.set_password.assert_has_calls([
            call(keychain._SERVICE, "default", raw),
            call(keychain._SERVICE, "default", replacement.decode()),
        ])

    def test_windows_blob_limit_counts_utf16_bytes_including_surrogate_pairs(self):
        for allowed, rejected in (("a" * 1280, "a" * 1281),
                                  ("가" * 1280, "가" * 1281),
                                  ("😀" * 640, "😀" * 640 + "a")):
            with self.subTest(character=allowed[0]):
                self.assertEqual(self.backend._save(allowed.encode("utf-8")), keychain._SUCCESS)
                self.vault.set_password.reset_mock()
                with self.assertRaisesRegex(keychain.KeychainError, "2560"):
                    self.backend._save(rejected.encode("utf-8"))
                self.vault.set_password.assert_not_called()
                self.assertEqual(self.backend.load(), (keychain._SUCCESS, allowed.encode("utf-8")))

    def test_invalid_bytes_fail_without_writing_or_disclosing_values(self):
        for value in (None, SECRET, b"\xff" + SECRET.encode()):
            with self.subTest(value_type=type(value).__name__), self.assertRaises(keychain.KeychainError) as caught:
                self.backend._save(value)
            self.assertNotIn(SECRET, str(caught.exception))
        self.vault.set_password.assert_not_called()

    def test_missing_duplicate_and_delete_map_to_existing_storage_contract(self):
        self.assertEqual(self.backend.load(), (keychain._NOT_FOUND, None))
        self.assertEqual(self.backend.update(b"first"), keychain._NOT_FOUND)
        self.assertEqual(self.backend.delete(), keychain._NOT_FOUND)
        self.assertEqual(self.backend.add(b"first"), keychain._SUCCESS)
        self.assertEqual(self.backend.add(b"second"), keychain._DUPLICATE)
        self.assertEqual(self.backend.load(), (keychain._SUCCESS, b"first"))
        self.assertEqual(self.backend.delete(), keychain._SUCCESS)
        self.assertEqual(self.backend.load(), (keychain._NOT_FOUND, None))

    def test_fixed_services_are_separate_and_arbitrary_locations_are_rejected(self):
        for index, service in enumerate(sorted(keychain._ALLOWED_SERVICES)):
            backend = keychain._WindowsCredentialVault(service=service, vault=self.vault)
            backend.add(str(index).encode())
        self.assertEqual(len(self.vault.values), len(keychain._ALLOWED_SERVICES))
        for service, account in (("arbitrary-service", "default"), (keychain._SERVICE, "other-user")):
            with self.assertRaises(keychain.KeychainError):
                keychain._WindowsCredentialVault(service=service, account=account, vault=self.vault)

    def test_windows_selection_uses_explicit_native_backend_without_auto_selection(self):
        root, backends, windows = ModuleType("keyring"), ModuleType("keyring.backends"), ModuleType("keyring.backends.Windows")
        root.__path__, backends.__path__ = [], []
        root.get_keyring = Mock(side_effect=AssertionError("automatic backend selection forbidden"))
        windows.WinVaultKeyring = Mock(return_value=self.vault)
        with patch.dict(sys.modules, {"keyring": root, "keyring.backends": backends, "keyring.backends.Windows": windows}), \
                patch.object(keychain.sys, "platform", "win32"), patch.object(keychain, "_NativeKeychain") as native:
            result = keychain._get_backend(service="com.local.kiwoom-autotrader.real")
        self.assertIsInstance(result, keychain._WindowsCredentialVault)
        self.assertIs(result.vault, self.vault)
        self.assertEqual(result.service, "com.local.kiwoom-autotrader.real")
        windows.WinVaultKeyring.assert_called_once_with()
        root.get_keyring.assert_not_called()
        native.assert_not_called()

    def test_missing_windows_dependency_fails_closed_without_macos_or_plaintext_fallback(self):
        with patch.dict(sys.modules, {"keyring.backends.Windows": None}), \
                patch.object(keychain.sys, "platform", "win32"), patch.object(keychain, "_NativeKeychain") as native:
            with self.assertRaises(keychain.KeychainError) as caught:
                keychain._get_backend()
        self.assertIn("Windows", str(caught.exception))
        self.assertIn("start.bat", str(caught.exception))
        native.assert_not_called()

    def test_public_storage_api_preserves_unicode_without_output_or_file_fallback(self):
        credentials = keychain.Credentials('키"😀', "비밀\\value")
        output = io.StringIO()
        with patch.object(keychain, "_get_backend", return_value=self.backend), \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            keychain.save_mock_credentials(credentials)
            self.assertEqual(keychain.load_mock_credentials(), credentials)
            self.assertTrue(keychain.delete_mock_credentials())
        self.assertEqual(output.getvalue(), "")

    def test_native_failures_are_sanitized_at_public_boundary_and_do_not_erase_existing_data(self):
        original = keychain.Credentials("offline-key", SECRET)
        output = io.StringIO()
        with patch.object(keychain, "_get_backend", return_value=self.backend):
            keychain.save_mock_credentials(original)
            existing = dict(self.vault.values)
            self.vault.set_password.side_effect = RuntimeError(SECRET)
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                with self.assertRaises(keychain.KeychainError) as caught:
                    keychain.save_mock_credentials(keychain.Credentials("new-key", "new-secret"))
            self.assertNotIn(SECRET, "".join(traceback.format_exception(caught.exception)))
            self.assertEqual(self.vault.values, existing)
            self.vault.delete_password.assert_not_called()
            self.vault.get_password.side_effect = RuntimeError(SECRET)
            with self.assertRaises(keychain.KeychainError) as caught:
                keychain.load_mock_credentials()
            self.assertNotIn(SECRET, "".join(traceback.format_exception(caught.exception)))
        self.assertEqual(output.getvalue(), "")


class WindowsPlatformTests(unittest.TestCase):
    def test_windows_data_root_honors_localappdata_and_explicit_home_override(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            redirected = base / "redirected-local-profile"
            supplied_home = base / "explicit-home"
            with patch.object(paths.sys, "platform", "win32"), \
                    patch.dict(paths.os.environ, {"LOCALAPPDATA": str(redirected)}):
                self.assertEqual(paths.app_data_root(), redirected / "KiwoomAutotrader")
                self.assertEqual(paths.app_data_root(supplied_home), supplied_home / "AppData/Local/KiwoomAutotrader")
            with patch.object(paths.sys, "platform", "win32"), \
                    patch.dict(paths.os.environ, {}, clear=True), patch.object(paths.Path, "home", return_value=supplied_home):
                self.assertEqual(paths.app_data_root(), supplied_home / "AppData/Local/KiwoomAutotrader")

    def test_macos_data_root_preserves_existing_location(self):
        with patch.object(paths.sys, "platform", "darwin"):
            self.assertEqual(paths.app_data_root("/tmp/offline-home"),
                             Path("/tmp/offline-home/Library/Application Support/KiwoomAutotrader"))

    def test_windows_directory_sync_does_not_try_to_open_a_directory(self):
        with patch.object(platform_io.sys, "platform", "win32"), patch.object(platform_io.os, "open") as opener:
            platform_io.sync_directory(Path("offline-state-dir"))
        opener.assert_not_called()

    def windows_lock_module(self):
        fake_msvcrt = SimpleNamespace(LK_UNLCK=0, LK_NBLCK=2, locking=Mock())
        spec = importlib.util.spec_from_file_location("_offline_windows_platform_io", platform_io.__file__)
        module = importlib.util.module_from_spec(spec)
        real_import = builtins.__import__
        def importer(name, *args, **kwargs):
            if name == "fcntl":
                raise ImportError("mock Windows environment")
            if name == "msvcrt":
                return fake_msvcrt
            return real_import(name, *args, **kwargs)
        with patch("builtins.__import__", side_effect=importer):
            spec.loader.exec_module(module)
        return module, fake_msvcrt

    def test_windows_lock_retries_only_contention_and_restores_file_position(self):
        module, native = self.windows_lock_module()
        native.locking.side_effect = [OSError(errno.EACCES, "locked"), None]
        with patch.object(module._os, "lseek", return_value=7) as seek, patch.object(module.time, "sleep") as sleep:
            module.fcntl.flock(17, module.fcntl.LOCK_EX)
        native.locking.assert_has_calls([call(17, native.LK_NBLCK, 1)] * 2)
        sleep.assert_called_once_with(0.05)
        self.assertEqual(seek.call_args, call(17, 7, module._os.SEEK_SET))

    def test_windows_lock_bad_descriptor_is_not_retried_or_misreported_as_contention(self):
        module, native = self.windows_lock_module()
        for operation in (module.fcntl.LOCK_EX, module.fcntl.LOCK_EX | module.fcntl.LOCK_NB):
            native.locking.reset_mock()
            native.locking.side_effect = OSError(errno.EBADF, "bad descriptor")
            with self.subTest(operation=operation), patch.object(module._os, "lseek", return_value=0), \
                    patch.object(module.time, "sleep", side_effect=AssertionError("non-contention must not retry")) as sleep:
                with self.assertRaises(OSError) as caught:
                    module.fcntl.flock(17, operation)
                self.assertEqual(caught.exception.errno, errno.EBADF)
                sleep.assert_not_called()
            native.locking.assert_called_once()

    def test_windows_nonblocking_contention_and_unlock_use_one_native_attempt(self):
        module, native = self.windows_lock_module()
        native.locking.side_effect = OSError(errno.EACCES, "locked")
        with patch.object(module._os, "lseek", return_value=0), patch.object(module.time, "sleep") as sleep:
            with self.assertRaises(BlockingIOError):
                module.fcntl.flock(17, module.fcntl.LOCK_EX | module.fcntl.LOCK_NB)
            sleep.assert_not_called()
        native.locking.reset_mock(side_effect=True)
        with patch.object(module._os, "lseek", return_value=0):
            module.fcntl.flock(17, module.fcntl.LOCK_UN)
        native.locking.assert_called_once_with(17, native.LK_UNLCK, 1)


class WindowsLauncherTests(unittest.TestCase):
    def test_launcher_uses_quoted_local_venv_and_checks_python_and_install_before_main(self):
        text = (ROOT / "start.bat").read_text(encoding="utf-8-sig")
        normalized = text.replace("\r\n", "\n")
        self.assertIn('cd /d "%~dp0"', normalized)
        self.assertIn('py -3 -m venv ".venv"', normalized)
        self.assertIn('python -m venv ".venv"', normalized)
        self.assertIn('if exist ".venv\\Scripts\\python.exe" goto install', normalized)
        self.assertGreaterEqual(normalized.count("sys.version_info < (3,11)"), 3)
        pip_line = '".venv\\Scripts\\python.exe" -m pip install -r "requirements.txt"'
        main_line = '".venv\\Scripts\\python.exe" "main.py" %*'
        self.assertLess(normalized.index(pip_line), normalized.index(main_line))
        self.assertIn(pip_line + "\nif errorlevel 1 goto failed\n" + main_line, normalized)
        self.assertIn('set "task_exit=%errorlevel%"', normalized)
        self.assertIn("exit /b %task_exit%", normalized)
        self.assertRegex(normalized, r":failed\n[\s\S]*exit /b 1")
        self.assertNotRegex(normalized.casefold(), r"\b(?:runas|powershell.*executionpolicy\s+bypass)\b")

    def test_requirements_include_explicit_windows_vault_and_runtime_dependencies(self):
        text = (ROOT / "requirements.txt").read_text()
        self.assertRegex(text, r"(?m)^keyring[^\n]+;\s*sys_platform\s*==\s*['\"]win32['\"]$")
        self.assertIn("websockets==15.0.1", text)
        self.assertIn("Telethon==1.45.0", text)
        self.assertRegex(text, r"(?m)^openpyxl[><=]")


if __name__ == "__main__":
    unittest.main()
