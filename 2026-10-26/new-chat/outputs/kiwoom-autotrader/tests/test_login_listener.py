"""Offline end-to-end command handling with fake Telegram, issuer and settings."""

import contextlib
from datetime import datetime, timedelta
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from kiwoom_autotrader.credentials import Credentials
from kiwoom_autotrader.login_session import BrokerSession, IssuedToken, KST
from kiwoom_autotrader.telegram import TelegramAPIError
from kiwoom_autotrader.telegram_credentials import TelegramConfig
from kiwoom_autotrader import telegram_listener as listener


BOT_TOKEN = "123456789:offline_fixture_bot_abcdefghijklmnopqrstuvwxyz"
CHAT_ID = 778899001
BROKER_TOKEN = "offline-fixture-broker-token-private"
CONFIG = TelegramConfig(BOT_TOKEN, CHAT_ID, 123456789, "offline_test_bot")


def message(identifier, text, **changes):
    value = {"update_id": identifier, "message": {
        "date": 1000, "text": text, "chat": {"id": CHAT_ID, "type": "private"},
        "from": {"id": CHAT_ID, "is_bot": False},
    }}
    value["message"].update(changes)
    return value


class LoginListenerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.now = datetime(2026, 10, 26, 8, 54, tzinfo=KST)
        self.loader = Mock(return_value=Credentials("private-key", "private-secret"))
        self.issuer = Mock(return_value=IssuedToken(BROKER_TOKEN))
        self.save_settings = Mock()
        self.session = BrokerSession(now=lambda: self.now, credential_loader=self.loader,
                                     token_issuer=self.issuer, save_renewal=self.save_settings)
        self.patch("kiwoom_autotrader.telegram_listener.load_telegram_config", return_value=CONFIG)
        self.telegram = self.patch("kiwoom_autotrader.telegram_listener.TelegramClient").return_value
        self.telegram.get_me.return_value = {"id": CONFIG.bot_id, "username": CONFIG.bot_username}
        self.telegram.get_webhook_info.return_value = {"url": ""}
        self.telegram.get_updates.return_value = []
        self.patch("kiwoom_autotrader.telegram_listener.time.time", return_value=1000)
        self.patch("kiwoom_autotrader.telegram_listener.time.sleep")
        self.load_settings = self.patch("kiwoom_autotrader.telegram_listener.load_settings")
        self.root_save = self.patch("kiwoom_autotrader.telegram_listener.save_renewal_time")
        self.scheduler = self.patch("kiwoom_autotrader.telegram_listener.CommandScheduler").return_value
        self.scheduler.claim_due.return_value = None
        self.scheduler.seconds_until_next.return_value = None
        self.stop_collector = self.patch("kiwoom_autotrader.telegram_account_service.stop_collector_service")
        self.fallback_reply = self.patch("kiwoom_autotrader.telegram_listener._reply", return_value="help")
        self.terminal = self.patch("kiwoom_autotrader.telegram_listener.terminal_report",
                                   wraps=listener.terminal_report)
        self.output = io.StringIO()
        self.addCleanup(self.output.close)

    def patch(self, target, **kwargs):
        patcher = patch(target, **kwargs)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def receive(self, updates=None, polls=1):
        if updates is not None:
            self.telegram.get_updates.return_value = updates
        with contextlib.redirect_stdout(self.output):
            result = listener.run_listener(self.directory, max_polls=polls, broker_session=self.session)
        self.load_settings.assert_not_called()
        self.root_save.assert_not_called()
        return result

    def state(self):
        return json.loads((self.directory / "state.json").read_text())

    def replies(self):
        return [entry.args[1] for entry in self.telegram.send_message.call_args_list]

    def assert_secret_free(self):
        text = self.output.getvalue() + (self.directory / "state.json").read_text() + "\n".join(self.replies())
        for secret in (BOT_TOKEN, BROKER_TOKEN, "private-key", "private-secret", str(CHAT_ID)):
            self.assertNotIn(secret, text)

    def test_login_normalization_preserves_runtime_session_until_receiver_ends(self):
        seen = []
        self.telegram.send_message.side_effect = lambda *_: seen.append((self.session.mode, self.session.token))
        self.assertEqual(self.receive([message(1, " \nLOGIN   PaPeR\t ")]), 0)
        self.assertEqual(seen, [(None, None), ("paper", BROKER_TOKEN), ("paper", BROKER_TOKEN)])
        self.assertEqual([entry.args[0] for entry in self.loader.call_args_list], ["paper", "paper"])
        self.assertEqual(self.issuer.call_count, 2)
        self.assertIn("모의투자 로그인 성공", self.replies()[-1])
        self.assertEqual(self.state()["last_command"], "login_paper")
        self.assertIsNone(self.session.token)
        self.assert_secret_free()

    def test_missing_real_credentials_does_not_replace_previous_paper_session(self):
        self.loader.side_effect = lambda mode: Credentials("private-key", "private-secret") if mode == "paper" else None
        seen = []
        self.telegram.send_message.side_effect = lambda *_: seen.append((self.session.mode, self.session.token))
        self.receive([message(1, "LOGIN REAL")])
        self.assertEqual(seen, [(None, None), ("paper", BROKER_TOKEN), ("paper", BROKER_TOKEN)])
        self.issuer.assert_called_once()
        self.assertIn("실전투자 로그인 실패", self.replies()[-1])
        self.assertIn("기존 모의투자", self.replies()[-1])
        self.assert_secret_free()

    def test_renewal_command_persists_normalized_time_without_reissuing_startup_token(self):
        self.receive([message(1, " Login  Renewal   8:50 ")])
        self.save_settings.assert_called_once_with("08:50")
        self.assertEqual(self.session.renewal_time, "08:50")
        self.loader.assert_called_once_with("paper")
        self.issuer.assert_called_once()
        self.assertIn("08:50", self.replies()[-1])
        self.assertIn("08:50", self.output.getvalue())

    def test_invalid_time_and_internal_slash_names_do_not_crash_receiver(self):
        self.assertEqual(self.receive([message(1, "login renewal 25:00"), message(2, "/login_renewal")]), 0)
        self.save_settings.assert_not_called()
        self.issuer.assert_called_once()
        self.assertIn("사용법", self.replies()[2])
        self.assertEqual(self.state()["status"], "stopped")

    def test_unauthorized_login_and_power_off_are_ignored(self):
        updates = [
            message(1, "login real", chat={"id": CHAT_ID + 1, "type": "private"}),
            message(2, "power off", **{"from": {"id": CHAT_ID + 1, "is_bot": False}}),
            message(3, "login paper", date=999),
            message(4, "login renewal 8:50", chat={"id": CHAT_ID, "type": "group"}),
        ]
        self.receive(updates)
        self.loader.assert_called_once_with("paper")
        self.save_settings.assert_not_called()
        self.assertEqual(len(self.replies()), 2)
        self.assertEqual(self.state()["received_count"], 0)
        self.stop_collector.assert_not_called()
        self.assertEqual(self.state()["next_offset"], 5)

    def test_scheduled_renewal_reports_to_telegram_and_terminal_once(self):
        def poll(**_):
            self.now += timedelta(minutes=1)
            return []
        self.telegram.get_updates.side_effect = poll
        self.assertEqual(self.receive([], polls=2), 0)
        self.assertEqual(self.issuer.call_count, 2)
        self.assertEqual(self.telegram.send_message.call_count, 3)
        self.assertIn("토큰 갱신 성공", self.replies()[-1])
        self.assertIn("토큰 갱신 성공", self.output.getvalue())
        self.assertEqual(self.terminal.call_count, 2)
        self.assertEqual(self.state()["replied_count"], 3)
        self.assert_secret_free()

    def test_renewal_failure_is_reported_once_and_polling_continues(self):
        self.issuer.side_effect = [IssuedToken(BROKER_TOKEN), RuntimeError("private-secret")]
        def poll(**_):
            self.now += timedelta(minutes=1)
            return []
        self.telegram.get_updates.side_effect = poll
        self.assertEqual(self.receive([], polls=3), 0)
        self.assertEqual(self.telegram.get_updates.call_count, 3)
        self.assertEqual(self.telegram.send_message.call_count, 3)
        self.assertIn("토큰 갱신 실패", self.replies()[-1])
        self.assertEqual(self.issuer.call_count, 2)
        self.assert_secret_free()

    def test_long_poll_is_shortened_to_the_next_renewal_deadline(self):
        self.now = datetime(2026, 10, 26, 8, 54, 50, tzinfo=KST)
        self.receive([])
        self.telegram.get_updates.assert_called_once_with(timeout=10, offset=None)

    def test_power_off_acknowledges_before_stopping_collector_and_skips_remaining_updates(self):
        events = []
        self.telegram.send_message.side_effect = lambda _, text: events.append(
            "off-ack" if "프로그램을 종료" in text else "startup" if text == listener._STARTUP_MESSAGE else "login-ack")
        self.stop_collector.side_effect = lambda: events.append("collector-stop")
        self.assertEqual(self.receive([
            message(1, "login paper"), message(2, "  PoWeR   OFF \n"), message(3, "login real"),
        ], polls=10), 0)
        self.assertEqual(events, ["startup", "login-ack", "login-ack", "off-ack", "collector-stop"])
        self.stop_collector.assert_called_once_with()
        self.assertEqual(self.issuer.call_count, 2)
        self.telegram.get_updates.assert_called_once()
        self.assertIsNone(self.session.token)
        self.assertEqual(self.state()["status"], "stopped")
        self.assertEqual(self.state()["next_offset"], 3)
        self.assertTrue((self.directory / "stop.request").exists())
        self.assert_secret_free()

    def test_power_off_still_stops_when_acknowledgement_cannot_be_confirmed(self):
        self.telegram.send_message.side_effect = TelegramAPIError("send failed")
        self.assertEqual(self.receive([message(1, "POWER OFF"), message(2, "login real")], polls=10), 0)
        self.stop_collector.assert_called_once_with()
        self.assertEqual(self.telegram.send_message.call_count, 3)
        self.assertIsNone(self.session.token)
        self.assertEqual(self.state()["status"], "stopped")
        self.assertEqual(self.state()["replied_count"], 0)
        self.assert_secret_free()

    def test_power_off_exits_successfully_even_if_its_initial_checkpoint_cannot_be_saved(self):
        save = listener._save_state
        def fail_shutdown_checkpoints(path, state):
            if state["last_command"] == "power_off":
                raise listener.TelegramListenerError("private-secret")
            return save(path, state)
        with patch.object(listener, "_save_state", side_effect=fail_shutdown_checkpoints):
            self.assertEqual(self.receive([message(1, "power off"), message(2, "login real")], polls=10), 0)
        self.stop_collector.assert_called_once_with()
        self.telegram.get_updates.assert_called_once()
        self.loader.assert_called_once_with("paper")
        self.assertIsNone(self.session.token)
        self.assertTrue((self.directory / "stop.request").exists())
        self.assertIn("프로그램을 종료", self.replies()[-1])
        self.assert_secret_free()

    def test_power_off_does_not_restart_if_acknowledgement_checkpoint_fails(self):
        save = listener._save_state
        failed = []
        def fail_reply_checkpoint(path, state):
            if state["last_command"] == "power_off" and state["replied_count"] == 3 and state["status"] == "running":
                failed.append(True)
                raise listener.TelegramListenerError("private-secret")
            return save(path, state)
        with patch.object(listener, "_save_state", side_effect=fail_reply_checkpoint):
            self.assertEqual(self.receive([message(1, "power off"), message(2, "login real")], polls=10), 0)
        self.assertEqual(failed, [True])
        self.stop_collector.assert_called_once_with()
        self.assertIsNone(self.session.token)
        self.assertEqual(self.state()["status"], "stopped")
        self.assertEqual(self.state()["next_offset"], 2)
        self.assert_secret_free()

    def test_power_off_exits_successfully_if_stop_file_and_final_state_writes_fail(self):
        save = listener._save_state
        def fail_final_state(path, state):
            if state["status"] == "stopped":
                raise listener.TelegramListenerError("private-secret")
            return save(path, state)
        with patch.object(listener, "_save_state", side_effect=fail_final_state), \
                patch.object(Path, "touch", side_effect=OSError("private-secret")) as touch:
            self.assertEqual(self.receive([message(1, "power off")], polls=10), 0)
        touch.assert_called_once_with(mode=0o600)
        self.stop_collector.assert_called_once_with()
        self.telegram.get_updates.assert_called_once()
        self.assertIsNone(self.session.token)
        self.assertIn("프로그램은 종료", self.output.getvalue())
        self.assert_secret_free()

    def test_collector_stop_failure_is_reported_without_restarting_the_receiver(self):
        self.stop_collector.side_effect = RuntimeError("private-secret")
        self.assertEqual(self.receive([message(1, "power off"), message(2, "login real")], polls=10), 0)
        self.stop_collector.assert_called_once_with()
        self.assertIn("채널 수집 종료를 확인하지 못했습니다", self.replies()[-1])
        self.assertEqual(self.telegram.send_message.call_count, 4)
        self.assertEqual(self.state()["status"], "stopped")
        self.assertIsNone(self.session.token)
        self.assert_secret_free()

    def test_collector_stop_failure_and_lost_replies_still_clear_session_and_exit_zero(self):
        self.stop_collector.side_effect = RuntimeError("private-secret")
        self.telegram.send_message.side_effect = [None, None, TelegramAPIError("private-secret"), TelegramAPIError("private-secret")]
        self.assertEqual(self.receive([message(1, "power off")], polls=10), 0)
        self.stop_collector.assert_called_once_with()
        self.assertIsNone(self.session.token)
        self.assertEqual(self.state()["status"], "stopped")
        self.assertEqual(self.state()["replied_count"], 2)
        self.assert_secret_free()

    def test_startup_notifies_then_logs_in_paper_once_before_polling(self):
        events = []
        self.telegram.get_me.side_effect = lambda: events.append("bot") or {
            "id": CONFIG.bot_id, "username": CONFIG.bot_username}
        self.telegram.get_webhook_info.side_effect = lambda: events.append("webhook") or {"url": ""}
        self.telegram.send_message.side_effect = lambda _, text: events.append(
            "startup" if text == listener._STARTUP_MESSAGE else "result")
        self.issuer.side_effect = lambda mode, _: events.append(mode) or IssuedToken(BROKER_TOKEN)
        self.telegram.get_updates.side_effect = lambda **_: events.append("poll") or []
        self.assertEqual(self.receive(polls=2), 0)
        self.assertEqual(events, ["bot", "webhook", "startup", "paper", "result", "poll", "poll"])
        self.loader.assert_called_once_with("paper")
        self.assertEqual(self.state()["replied_count"], 2)
        self.assertEqual(self.state()["received_count"], 0)
        self.assertIn("로그인 성공", self.output.getvalue())
        self.assert_secret_free()

    def test_startup_notice_failure_does_not_block_login_or_polling(self):
        self.telegram.send_message.side_effect = [TelegramAPIError("private-secret"), None]
        self.assertEqual(self.receive(polls=2), 0)
        self.loader.assert_called_once_with("paper")
        self.assertEqual(self.telegram.get_updates.call_count, 2)
        self.assertEqual(self.state()["replied_count"], 1)
        self.assertIn("로그인 성공", self.replies()[-1])
        self.assert_secret_free()

    def test_failed_startup_login_is_reported_without_automatic_busy_retry(self):
        self.loader.return_value = None
        self.assertEqual(self.receive(polls=3), 0)
        self.loader.assert_called_once_with("paper")
        self.issuer.assert_not_called()
        self.assertEqual(self.telegram.get_updates.call_count, 3)
        self.assertEqual(len(self.replies()), 2)
        self.assertIn("모의투자 로그인 실패", self.replies()[-1])
        self.assertIn("로그인 실패", self.output.getvalue())
        self.assertIsNone(self.session.token)
        self.assert_secret_free()

    def test_invalid_bot_or_webhook_prevents_startup_side_effects(self):
        for mismatch in ("bot", "webhook"):
            with self.subTest(mismatch=mismatch):
                self.telegram.get_me.return_value = {"id": CONFIG.bot_id if mismatch == "webhook" else 1,
                                                      "username": CONFIG.bot_username}
                self.telegram.get_webhook_info.return_value = {"url": "https://example.invalid/" if mismatch == "webhook" else ""}
                with self.assertRaises(listener.TelegramListenerError):
                    self.receive()
                self.loader.assert_not_called()
                self.telegram.send_message.assert_not_called()
                self.telegram.get_updates.assert_not_called()


if __name__ == "__main__":
    unittest.main()
