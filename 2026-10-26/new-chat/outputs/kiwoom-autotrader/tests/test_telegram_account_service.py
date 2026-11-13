"""Collector lifecycle tests in a temporary home with mocked OS and Keychain."""

from contextlib import contextmanager
from pathlib import Path
import plistlib
import stat
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from kiwoom_autotrader import telegram_account_service as service


class AccountServiceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.paths = service.AccountServicePaths(Path(temporary.name) / "home")
        self.loaded = self.running = False
        self.launch = self.patch("kiwoom_autotrader.telegram_account_service._launchctl", side_effect=self.launchctl)
        self.deploy = self.patch("kiwoom_autotrader.telegram_account_service.install_service")
        self.load = self.patch("kiwoom_autotrader.telegram_account_credentials.load_account_config", return_value=SimpleNamespace(user_id=777))
        self.store = self.patch("kiwoom_autotrader.channel_store.ChannelStore")
        self.store.return_value.__enter__.return_value.channels.return_value = [{"channel_id": 123}]

    def patch(self, target, **kwargs):
        patcher = patch(target, **kwargs)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def launchctl(self, *arguments, **kwargs):
        action = arguments[0]
        if action == "print":
            if not self.loaded:
                return subprocess.CompletedProcess(arguments, 113, "", "missing")
            output = "state = running\n pid = 4321\n" if self.running else "state = not running\n"
            return subprocess.CompletedProcess(arguments, 0, output, "")
        if action == "bootstrap":
            self.assertFalse(self.loaded)
            self.loaded = self.running = True
        elif action == "kickstart":
            self.assertTrue(self.loaded)
            self.running = True
        elif action == "bootout":
            self.loaded = self.running = False
        elif action != "enable":
            self.fail("Unexpected launchctl mutation")
        return subprocess.CompletedProcess(arguments, 0, "", "")

    def install(self):
        service.install_collector_service(source=Path("/test-project"), paths=self.paths)

    def test_install_requires_account_before_stop_and_selection_before_deploy_or_start(self):
        self.load.return_value = None
        with self.assertRaises(service.TelegramServiceError):
            self.install()
        self.launch.assert_not_called()
        self.deploy.assert_not_called()
        self.store.assert_not_called()
        self.load.return_value = SimpleNamespace(user_id=777)
        self.store.return_value.__enter__.return_value.channels.return_value = []
        with self.assertRaises(service.TelegramServiceError):
            self.install()
        self.deploy.assert_not_called()
        self.assertFalse(self.paths.plist.exists())
        self.assertNotIn("bootstrap", [entry.args[0] for entry in self.launch.call_args_list])

    def test_existing_collector_stops_before_archive_constructor_can_migrate(self):
        events = []
        self.loaded = self.running = True
        store_context = self.store.return_value
        original_stop = service.stop_collector_service

        def load_account():
            events.append("account")
            self.assertTrue(self.running)
            self.store.assert_not_called()
            return SimpleNamespace(user_id=777)

        def stop(paths):
            events.append("stop")
            self.store.assert_not_called()
            original_stop(paths)

        def open_archive(state, user_id):
            events.append("archive")
            self.assertFalse(self.running, "The old collector must not write while schema migration starts")
            self.assertFalse(self.loaded)
            self.assertEqual(state, self.paths.state)
            self.assertEqual(user_id, 777)
            return store_context

        self.load.side_effect = load_account
        self.store.side_effect = open_archive
        self.deploy.side_effect = lambda **kwargs: events.append("deploy")
        with patch.object(service, "stop_collector_service", side_effect=stop):
            self.install()
        self.assertEqual(events, ["account", "stop", "archive", "deploy"])
        self.assertTrue(self.running)

    def test_failed_stop_cannot_open_or_migrate_archive(self):
        with patch.object(service, "stop_collector_service", side_effect=service.TelegramServiceError("stop failed")):
            with self.assertRaises(service.TelegramServiceError):
                self.install()
        self.store.assert_not_called()
        self.deploy.assert_not_called()
        self.assertFalse(self.paths.plist.exists())

    def test_installed_job_uses_separate_identity_state_and_readonly_collector_command(self):
        self.install()
        definition = plistlib.loads(self.paths.plist.read_bytes())
        self.assertEqual(definition["Label"], "com.local.kiwoom-autotrader.telegram-account")
        self.assertEqual(definition["ProgramArguments"][4:], [
            "telegram-account-listen", "--state-dir", str(self.paths.state),
        ])
        self.assertTrue(definition["RunAtLoad"])
        self.assertEqual(definition["KeepAlive"], {"SuccessfulExit": False})
        self.assertEqual(definition["StandardOutPath"], str(self.paths.state / "collector.log"))
        self.assertEqual(stat.S_IMODE(self.paths.plist.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((self.paths.state / "collector.log").stat().st_mode), 0o600)
        self.deploy.assert_called_once_with(source=Path("/test-project"))
        self.assertTrue(service.collector_status(self.paths)["running"])

    def test_repeat_start_kickstarts_existing_service_instead_of_bootstrap_duplicate(self):
        self.install()
        self.launch.reset_mock()
        service.start_collector_service(self.paths)
        actions = [entry.args[0] for entry in self.launch.call_args_list]
        self.assertIn("kickstart", actions)
        self.assertNotIn("bootstrap", actions)

    def test_stop_keeps_login_registration_remove_deletes_only_collector_plist(self):
        self.install()
        bot_plist = self.paths.plist.parent / "com.local.kiwoom-autotrader.telegram.plist"
        bot_plist.write_text("bot remains")
        service.stop_collector_service(self.paths)
        self.assertFalse(self.loaded)
        self.assertTrue(self.paths.plist.exists())
        service.start_collector_service(self.paths)
        service.remove_collector_service(self.paths)
        self.assertFalse(self.paths.plist.exists())
        self.assertEqual(bot_plist.read_text(), "bot remains")
        self.assertTrue((self.paths.state / "collector.log").exists())

    def test_shared_runtime_failure_does_not_register_collector(self):
        self.deploy.side_effect = service.TelegramServiceError("install failed")
        with self.assertRaises(service.TelegramServiceError):
            self.install()
        self.assertFalse(self.paths.plist.exists())
        self.assertNotIn("bootstrap", [entry.args[0] for entry in self.launch.call_args_list])

    def test_bootout_happens_while_collector_lock_is_held(self):
        self.install()
        held = []
        @contextmanager
        def stopped(paths):
            held.append(paths)
            try:
                yield
            finally:
                held.pop()
        original = self.launch.side_effect
        def check(*args, **kwargs):
            if args[0] == "bootout":
                self.assertEqual(held, [self.paths])
            return original(*args, **kwargs)
        self.launch.side_effect = check
        with patch.object(service, "_stopped_collector", stopped):
            service.stop_collector_service(self.paths)

    def test_missing_registration_never_starts_service(self):
        with self.assertRaises(service.TelegramServiceError):
            service.start_collector_service(self.paths)
        self.launch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
