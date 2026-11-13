"""Offline login-service lifecycle tests; no real launchctl, secrets or API I/O."""

import fcntl
import json
from pathlib import Path
import plistlib
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from kiwoom_autotrader import cli
from kiwoom_autotrader import telegram_service as service


class TelegramServiceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.paths = service.ServicePaths(self.root / "home")
        self.source = self.root / "project"
        package = self.source / "kiwoom_autotrader"
        package.mkdir(parents=True)
        (package / "__main__.py").write_text("from .cli import main\nmain()\n")
        (package / "cli.py").write_text("def main():\n    return 0\n")
        self.original_state = self.source / "runs" / "telegram"
        self.original_state.mkdir(parents=True)
        self.checkpoint = {"version": 1, "next_offset": 314, "replied_count": 8}
        (self.original_state / "state.json").write_text(json.dumps(self.checkpoint))
        self.loaded = False
        self.running = False
        self.patch("kiwoom_autotrader.telegram_service.sys.platform", "darwin")
        self.config = self.patch(
            "kiwoom_autotrader.telegram_credentials.load_telegram_config",
            return_value=object(),
        )
        self.runner = self.patch(
            "kiwoom_autotrader.telegram_service.subprocess.run",
            side_effect=self.launchctl,
        )
        self.patch("kiwoom_autotrader.telegram_service.service_paths", return_value=self.paths)
        self.patch("builtins.print")

    def patch(self, target, *args, **kwargs):
        patcher = patch(target, *args, **kwargs)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def launchctl(self, argv, **kwargs):
        self.assertEqual(argv[0], "/bin/launchctl")
        self.assertEqual(kwargs, {
            "capture_output": True, "text": True, "timeout": 30, "check": False,
        })
        command = argv[1]
        if command == "print":
            if not self.loaded:
                return subprocess.CompletedProcess(argv, 113, "", "service not found")
            output = "state = running\n pid = 4242\n" if self.running else "state = not running\n"
            return subprocess.CompletedProcess(argv, 0, output, "")
        if command == "bootout":
            self.loaded = self.running = False
        elif command == "bootstrap":
            self.assertFalse(self.loaded, "Already loaded service must not be bootstrapped twice")
            self.loaded = self.running = True
        elif command == "kickstart":
            self.assertTrue(self.loaded)
            self.running = True
        elif command != "enable":
            self.fail("Unexpected launchctl operation: " + command)
        return subprocess.CompletedProcess(argv, 0, "", "")

    def commands(self):
        return [entry.args[0][1] for entry in self.runner.call_args_list]

    def install(self):
        service.install_service(self.source, self.paths)

    def test_install_migrates_checkpoint_and_deploys_only_python_code(self):
        secret = "fixture-secret-must-not-be-deployed"
        for relative in (".env", "credentials.json", "kiwoom_autotrader/token.txt",
                         "runs/telegram/listener.log"):
            path = self.source / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(secret)
        cache = self.source / "kiwoom_autotrader" / "__pycache__"
        cache.mkdir()
        (cache / "secret.pyc").write_text(secret)
        self.install()
        self.assertEqual(json.loads((self.paths.state / "state.json").read_text()), self.checkpoint)
        self.assertEqual(
            {p.relative_to(self.paths.runtime).as_posix() for p in self.paths.runtime.rglob("*") if p.is_file()},
            {"kiwoom_autotrader/__main__.py", "kiwoom_autotrader/cli.py"},
        )
        for path in self.paths.base.rglob("*"):
            if path.is_file():
                self.assertNotIn(secret.encode(), path.read_bytes())
        self.assertNotIn(secret, self.paths.plist.read_text())
        for path in (self.paths.plist, self.paths.state / "state.json", self.paths.state / "listener.log"):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_structured_runtime_deploys_nested_modules_and_only_allowlisted_settings(self):
        package = self.source / "kiwoom_autotrader"
        deployed = {
            "api/__init__.py": "# API package\n",
            "api/au10001.py": "API_ID = 'au10001'\n",
            "telegram/__init__.py": "# Telegram package\n",
            "telegram/commands/__init__.py": "# command handlers\n",
            "telegram/commands/login.py": "def command():\n    return 'login'\n",
            "config/__init__.py": "# resource package\n",
            "config/settings.py": "DEFAULT_RESOURCE = 'data/settings-default.json'\n",
            "config/data/settings-default.json": '{"login":{"renewal_time":"08:55"}}\n',
            "config/data/settings.json": '{"login":{"renewal_time":"08:50"}}\n',
        }
        for relative, content in deployed.items():
            path = package / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        sentinel = "fixture-private-data-must-not-enter-runtime"
        excluded = (
            ".env", "telegram/.env", "telegram/account-session.json",
            "config/data/private.json", "config/data/messages.jsonl",
            "config/data/settings-default.json.bak", "config/settings.json",
            "telegram/__pycache__/stale.py", "telegram/__pycache__/compiled.pyc",
            "telegram/commands/notes.txt",
        )
        for relative in excluded:
            path = package / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(sentinel)
        self.install()
        runtime = self.paths.runtime / "kiwoom_autotrader"
        expected = set(deployed) | {"__main__.py", "cli.py"}
        self.assertEqual({p.relative_to(runtime).as_posix() for p in runtime.rglob("*") if p.is_file()}, expected)
        for relative, content in deployed.items():
            target = runtime / relative
            self.assertEqual(target.read_text(), content)
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(target.parent.stat().st_mode), 0o700)
        for path in runtime.rglob("*"):
            if path.is_file():
                self.assertNotIn(sentinel.encode(), path.read_bytes())
        self.assertEqual(json.loads((self.paths.state / "state.json").read_text()), self.checkpoint)

    def test_install_registers_and_starts_failure_recovering_login_service(self):
        self.install()
        definition = plistlib.loads(self.paths.plist.read_bytes())
        self.assertTrue(definition["RunAtLoad"])
        self.assertEqual(definition["KeepAlive"], {"SuccessfulExit": False})
        self.assertGreaterEqual(definition["ThrottleInterval"], 10)
        self.assertNotIn("NetworkState", definition["KeepAlive"])
        self.assertEqual(definition["LimitLoadToSessionType"], "Aqua")
        self.assertEqual(definition["Umask"], 0o077)
        self.assertEqual(definition["WorkingDirectory"], str(self.paths.runtime))
        arguments = definition["ProgramArguments"]
        self.assertTrue(Path(arguments[0]).is_absolute())
        self.assertEqual(arguments[1:], [
            "-u", "-m", "kiwoom_autotrader", "telegram-listen", "--state-dir", str(self.paths.state),
        ])
        self.assertIn("enable", self.commands())
        self.assertEqual(self.commands().count("bootstrap"), 1)
        self.assertTrue(service.service_status(self.paths)["running"])

    def test_missing_config_performs_no_filesystem_or_launchctl_changes(self):
        self.config.return_value = None
        with self.assertRaises(service.TelegramServiceError):
            self.install()
        self.assertFalse(self.paths.home.exists())
        self.runner.assert_not_called()
        self.assertEqual(json.loads((self.original_state / "state.json").read_text()), self.checkpoint)

    def test_reinstall_keeps_newer_service_checkpoint_and_refreshes_code(self):
        self.install()
        newer = {"version": 1, "next_offset": 999, "replied_count": 99}
        (self.paths.state / "state.json").write_text(json.dumps(newer))
        (self.source / "kiwoom_autotrader" / "cli.py").write_text("# updated source\n")
        self.runner.reset_mock()
        self.install()
        self.assertEqual(json.loads((self.paths.state / "state.json").read_text()), newer)
        self.assertEqual((self.paths.runtime / "kiwoom_autotrader" / "cli.py").read_text(), "# updated source\n")
        self.assertLess(self.commands().index("bootout"), self.commands().index("bootstrap"))
        self.assertEqual(self.commands().count("bootstrap"), 1)

    def test_stop_preserves_login_registration_but_remove_deletes_it(self):
        self.install()
        service.stop_service(self.paths)
        self.assertTrue(self.paths.plist.exists())
        self.assertFalse(self.loaded)
        self.assertNotIn("disable", self.commands())
        service.start_service(self.paths)
        self.assertTrue(self.running)
        service.uninstall_service(self.paths)
        self.assertFalse(self.paths.plist.exists())
        self.assertFalse(self.loaded)
        self.assertTrue((self.paths.state / "state.json").exists())
        self.assertTrue(self.paths.runtime.exists())

    def test_stop_waits_for_and_holds_listener_lock_before_bootout(self):
        self.install()
        launchctl = self.launchctl

        def assert_exclusive_stop(argv, **kwargs):
            if argv[1] == "bootout":
                with (self.paths.state / "listener.lock").open("rb") as competing:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(competing.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return launchctl(argv, **kwargs)

        self.runner.side_effect = assert_exclusive_stop
        service.stop_service(self.paths)
        with (self.paths.state / "listener.lock").open("rb") as unlocked:
            fcntl.flock(unlocked.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.assertFalse(self.loaded)

    def test_reinstall_restores_previous_runtime_if_staged_rename_fails(self):
        self.install()
        previous_code = (self.paths.runtime / "kiwoom_autotrader" / "cli.py").read_text()
        (self.source / "kiwoom_autotrader" / "cli.py").write_text("# replacement\n")
        rename = Path.rename

        def fail_staged_rename(path, target):
            if path.name == "kiwoom_autotrader" and path.parent.name.startswith(".runtime-"):
                raise OSError("simulated staging failure")
            return rename(path, target)

        with patch.object(Path, "rename", autospec=True, side_effect=fail_staged_rename):
            with self.assertRaises(OSError):
                self.install()
        self.assertEqual((self.paths.runtime / "kiwoom_autotrader" / "cli.py").read_text(), previous_code)
        self.assertEqual(json.loads((self.paths.state / "state.json").read_text()), self.checkpoint)
        self.assertTrue(self.paths.plist.exists())

    def test_default_cli_start_delegates_to_loaded_service_without_second_listener(self):
        self.install()
        self.runner.reset_mock()
        direct = self.patch("kiwoom_autotrader.telegram_listener.run_listener")
        self.assertEqual(cli.main(["telegram-listen"]), 0)
        direct.assert_not_called()
        self.assertIn("kickstart", self.commands())
        self.assertNotIn("bootstrap", self.commands())

    def test_explicit_launchd_state_directory_runs_actual_listener(self):
        self.install()
        self.runner.reset_mock()
        direct = self.patch("kiwoom_autotrader.telegram_listener.run_listener", return_value=0)
        self.assertEqual(cli.main(["telegram-listen", "--state-dir", str(self.paths.state)]), 0)
        direct.assert_called_once_with(self.paths.state)
        self.runner.assert_not_called()

    def test_default_cli_stop_boots_out_service_and_keeps_next_login_enabled(self):
        self.install()
        self.assertEqual(cli.main(["telegram-stop"]), 0)
        self.assertFalse(self.loaded)
        self.assertTrue(self.paths.plist.exists())
        self.assertNotIn("disable", self.commands())

    def test_cli_listener_failure_returns_nonzero_for_launchd_recovery(self):
        from kiwoom_autotrader.telegram_listener import TelegramListenerError
        self.patch("kiwoom_autotrader.telegram_listener.run_listener", side_effect=TelegramListenerError("offline"))
        self.assertEqual(cli.main(["telegram-listen", "--state-dir", str(self.paths.state)]), 1)
        self.runner.assert_not_called()

    def test_start_missing_registration_fails_without_launchctl(self):
        with self.assertRaises(service.TelegramServiceError):
            service.start_service(self.paths)
        self.runner.assert_not_called()

    def test_launchctl_failures_are_sanitized(self):
        self.runner.side_effect = subprocess.TimeoutExpired("fixture-private-command", 30)
        with self.assertRaises(service.TelegramServiceError) as caught:
            service.service_status(self.paths)
        self.assertNotIn("fixture-private-command", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
