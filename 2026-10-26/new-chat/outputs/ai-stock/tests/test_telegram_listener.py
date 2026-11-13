"""Offline receive-loop tests: all Telegram, broker and Keychain I/O is fake."""

import fcntl
from datetime import datetime
import json
import os
from pathlib import Path
import stat
import tempfile
import traceback
import unittest
from unittest.mock import Mock, call, patch

from kiwoom_autotrader.credentials import Credentials, KeychainError
from kiwoom_autotrader.kiwoom import KiwoomAPIError
from kiwoom_autotrader.telegram import TelegramAPIError
from kiwoom_autotrader.telegram_credentials import TelegramConfig
from kiwoom_autotrader import telegram_listener as listener
from kiwoom_autotrader.login_session import BrokerSession, IssuedToken, KST


_TOKEN = "123456789:unit_test_token_abcdefghijklmnopqrstuvwxyz"
_CHAT_ID = 778899001
_CONFIG = TelegramConfig(_TOKEN, _CHAT_ID, 123456789, "unit_test_bot")


def update(identifier=1, text="/help", *, date=1000, chat_id=_CHAT_ID,
           sender_id=_CHAT_ID, chat_type="private", is_bot=False):
    return {"update_id": identifier, "message": {
        "date": date, "text": text,
        "chat": {"id": chat_id, "type": chat_type},
        "from": {"id": sender_id, "is_bot": is_bot},
    }}


class TelegramListenerTests(unittest.TestCase):
    def setUp(self):
        recorder_patch = patch("kiwoom_autotrader.telegram.listener.ExecutionStreamRecorder")
        self.addCleanup(recorder_patch.stop)
        self.execution_recorder = recorder_patch.start().return_value
        self.execution_recorder.status.return_value = {"ready": True, "error": None, "coverage_complete": True}
        tracker_patch = patch.object(listener, "ExecutionTracker")
        self.addCleanup(tracker_patch.stop)
        tracker_patch.start().return_value.seconds_until_next.return_value = None
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        settings_patch = patch("kiwoom_autotrader.config.settings.settings_path",
                               return_value=self.directory / "settings.json")
        self.addCleanup(settings_patch.stop)
        settings_patch.start()
        for target in ("urllib.request.OpenerDirector.open", "socket.socket.connect"):
            network_patch = patch(target, side_effect=AssertionError("external network forbidden"))
            self.addCleanup(network_patch.stop)
            self.addCleanup(network_patch.start().assert_not_called)
        self.config_load = self.patch("load_telegram_config", return_value=_CONFIG)
        self.credentials = self.patch("load_mock_credentials", return_value=Credentials(
            "fixture-app-key", "fixture-app-secret",
        ))
        self.telegram_factory = self.patch("TelegramClient")
        self.telegram = self.telegram_factory.return_value
        self.telegram.get_me.return_value = {
            "id": _CONFIG.bot_id, "username": _CONFIG.bot_username, "is_bot": True,
        }
        self.telegram.get_webhook_info.return_value = {"url": ""}
        self.telegram.get_updates.return_value = []
        self.broker_factory = self.patch("KiwoomMockClient")
        self.broker = self.broker_factory.return_value
        self.broker.quote.return_value = {"cur_prc": "123.45"}
        self.top = self.patch("get_top_equities")
        self.clock = self.patch("time.time", return_value=1000)
        self.sleep = self.patch("time.sleep")
        self.startup_credentials = Mock(return_value=Credentials("fixture-app-key", "fixture-app-secret"))
        self.startup_issuer = Mock(return_value=IssuedToken("fixture-startup-token"))
        self.session = BrokerSession(now=lambda: datetime(2026, 10, 26, 9, tzinfo=KST),
                                     credential_loader=self.startup_credentials, token_issuer=self.startup_issuer)
        self.patch("BrokerSession", return_value=self.session)
        self.patch("load_settings", return_value={"login_renewal_time": "08:55"})
        self.patch("save_renewal_time")
        self.scheduler = self.patch("CommandScheduler").return_value
        self.scheduler.claim_due.return_value = None
        self.scheduler.seconds_until_next.return_value = None
        print_patch = patch("builtins.print")
        self.addCleanup(print_patch.stop)
        self.printer = print_patch.start()

    def patch(self, name, **kwargs):
        patcher = patch("kiwoom_autotrader.telegram_listener." + name, **kwargs)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def run_once(self, updates=None):
        if updates is not None:
            self.telegram.get_updates.return_value = updates
        return listener.run_listener(self.directory, max_polls=1)

    def state(self):
        return json.loads((self.directory / "state.json").read_text())

    def printed(self):
        return "\n".join(" ".join(map(str, entry.args)) for entry in self.printer.call_args_list)

    def assert_safe(self, text):
        for private in (_TOKEN, str(_CHAT_ID), "fixture-app-key", "fixture-app-secret", "fixture-startup-token", "secret-message"):
            self.assertNotIn(private, text)

    def test_maintenance_blocks_notifications_orders_and_polling_then_restores(self):
        marker = self.directory / "maintenance-quiet.lock"
        marker.touch()
        tracker = Mock()
        tracker.seconds_until_next.return_value = None
        # An execution notification during account refresh must not be sent.
        tracker.tick.side_effect = lambda session, notify: notify("test notice")
        sell_monitor = Mock()
        buy_monitor = Mock()
        self.telegram.get_updates.return_value = [update(7, "/buy 005930 max 1000000")]
        result = listener.run_listener(self.directory, max_polls=1, execution_tracker=tracker,
                                       monitor=sell_monitor, golden_monitor=buy_monitor)
        self.assertEqual(result, 0)
        self.telegram.send_message.assert_not_called()
        self.telegram.get_updates.assert_not_called()
        self.scheduler.claim_due.assert_not_called()
        sell_monitor.tick.assert_not_called()
        buy_monitor.tick.assert_not_called()
        self.assertIsNone(self.state()["next_offset"])
        self.assertTrue(self.state()["maintenance_paused"])
        bound_guard = tracker.tick.call_args.args[0].maintenance_order_guard
        self.assertTrue(callable(bound_guard))
        with self.assertRaises(ValueError):
            bound_guard()
        marker.unlink()
        self.assertIsNone(bound_guard())
        # Normal startup preserves the offset and removes diagnostic state.
        self.telegram.get_updates.return_value = []
        self.assertEqual(listener.run_listener(self.directory, max_polls=1), 0)
        self.assertNotIn("maintenance_paused", self.state())
        self.assertGreaterEqual(self.telegram.send_message.call_count, 2)

    def test_maintenance_marker_io_error_fails_closed(self):
        with patch("kiwoom_autotrader.telegram.listener.os.lstat", side_effect=PermissionError):
            self.assertTrue(listener._maintenance_paused(self.directory))

    def test_help_start_unknown_reply_without_broker_access_or_raw_text(self):
        updates = [update(1, "/help"), update(2, "/start"), update(3, "secret-message")]
        self.assertEqual(self.run_once(updates), 0)
        self.assertEqual(self.telegram.send_message.call_count, 5)
        self.startup_credentials.assert_called_once_with("paper")
        self.startup_issuer.assert_called_once()
        self.credentials.assert_not_called()
        self.broker_factory.assert_not_called()
        self.assertIn("help", self.telegram.send_message.call_args.args[1])
        self.assert_safe(self.telegram.send_message.call_args.args[1])
        state = self.state()
        self.assertEqual(state["received_count"], 3)
        self.assertEqual(state["replied_count"], 5)
        self.assertEqual(state["next_offset"], 4)
        self.assertEqual(state["last_command"], "unknown")
        self.assertEqual(state["status"], "stopped")
        self.assertEqual(state["pid"], os.getpid())
        self.assertEqual(state["last_reply_at"], 1000)
        self.assert_safe((self.directory / "state.json").read_text())
        self.assert_safe(self.printed())
        self.assertEqual(stat.S_IMODE((self.directory / "state.json").stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((self.directory / "listener.lock").stat().st_mode), 0o600)

    def test_status_authenticates_and_queries_fixed_kr_us_symbols(self):
        self.run_once([update(text="/status")])
        self.credentials.assert_called_once_with()
        self.broker_factory.assert_called_once_with("fixture-app-key", "fixture-app-secret")
        self.broker.authenticate.assert_called_once_with()
        self.assertEqual(self.broker.quote.call_args_list, [
            call("KR", "005930", "KRX"), call("US", "AAPL", "NASDAQ"),
        ])
        text = self.telegram.send_message.call_args.args[1]
        self.assertIn("인증: 정상", text)
        self.assertNotIn("123.45", text)
        self.assertIn("미국 애플", text)
        self.top.assert_not_called()

    def test_top_uses_twenty_equity_rows_with_explicit_unit(self):
        self.top.return_value = {
            "queried_at_kst": "2026-10-26T12:30:00+09:00",
            "rows": [{"rank": index, "name": "기업" + str(index),
                      "market": "코스피" if index % 2 else "코스닥",
                      "turnover_100million_krw": "123.4567"}
                     for index in range(1, 21)],
        }
        self.run_once([update(text="/top")])
        self.broker.authenticate.assert_called_once_with()
        self.top.assert_called_once_with(self.broker, limit=20)
        text = self.telegram.send_message.call_args.args[1]
        self.assertIn("20. 기업20 (코스닥) · 123.4567억원", text)
        self.assertIn("2026-10-26 12:30:00", text)
        self.assertLessEqual(len(text), 4096)
        self.broker.quote.assert_not_called()

    def test_unauthorized_backlog_nontext_and_wrong_bot_suffix_are_ignored(self):
        updates = [
            update(1, chat_id=555), update(2, sender_id=555),
            update(3, chat_type="group"), update(4, is_bot=True),
            update(5, is_bot=0), update(6, date=999), update(7, date=True),
            update(8, chat_id=True), update(9, text=None),
            update(10, text="/status@another_bot"), {"update_id": 11, "edited_message": {}},
        ]
        self.run_once(updates)
        self.assertEqual(self.telegram.send_message.call_count, 2)
        self.credentials.assert_not_called()
        self.assertEqual(self.state()["next_offset"], 12)
        self.assertEqual(self.state()["received_count"], 0)
        self.assertIsNone(self.state()["last_command"])

    def test_own_bot_suffix_is_case_insensitive_and_arguments_are_not_echoed(self):
        self.run_once([update(text="/help@UNIT_TEST_BOT secret-message")])
        self.assertEqual(self.telegram.send_message.call_count, 3)
        self.assert_safe(self.telegram.send_message.call_args.args[1])

    def test_korean_aliases_dispatch_to_existing_read_only_handlers(self):
        replies = self.patch("_reply", side_effect=lambda command, config: "reply:" + command)
        self.run_once([
            update(1, "상태"), update(2, "상위20"),
            update(3, "거래대금"), update(4, "  도움말\n"),
        ])
        self.assertEqual(replies.call_args_list, [
            call("status", _CONFIG), call("top", _CONFIG),
            call("top", _CONFIG), call("help", _CONFIG),
        ])
        self.assertEqual(self.telegram.send_message.call_args_list[2:], [
            call(_CHAT_ID, "reply:status"), call(_CHAT_ID, "reply:top"),
            call(_CHAT_ID, "reply:top"), call(_CHAT_ID, "reply:help"),
        ])
        self.assertEqual(self.state()["last_command"], "help")
        self.assertEqual(self.state()["replied_count"], 6)

    def test_korean_aliases_require_whole_message_and_do_not_execute_other_text(self):
        self.run_once([
            update(1, "상태 알려줘"), update(2, "상위20\n매수"),
            update(3, "거래대금; secret-message"), update(4, "매수"),
            update(5, "/상태"), update(6, "도움말@unit_test_bot"),
        ])
        self.credentials.assert_not_called()
        self.broker_factory.assert_not_called()
        self.assertEqual(self.telegram.send_message.call_count, 8)
        for sent in self.telegram.send_message.call_args_list[2:]:
            self.assertEqual(sent.args[1], listener._UNKNOWN)
            self.assert_safe(sent.args[1])
        self.assertEqual(self.state()["last_command"], "unknown")

    def test_korean_aliases_still_require_authorization_freshness_and_unique_update(self):
        self.run_once([
            update(1, "상태", chat_id=555), update(2, "상위20", sender_id=555),
            update(3, "거래대금", chat_type="group"), update(4, "상태", is_bot=True),
            update(5, "도움말", date=999), update(6, "도움말"), update(6, "도움말"),
        ])
        self.assertEqual(self.telegram.send_message.call_args_list[2:], [call(_CHAT_ID, listener._HELP)])
        self.credentials.assert_not_called()
        self.assertEqual(self.state()["received_count"], 1)
        self.assertEqual(self.state()["next_offset"], 7)

    def test_korean_help_explains_aliases_and_unknown_points_to_help(self):
        self.run_once([update(1, "도움말"), update(2, "secret-message")])
        help_reply, unknown_reply = self.telegram.send_message.call_args_list[2:]
        for command in ("도움말", "상태", "상위20", "거래대금", "/help", "/status", "/top"):
            self.assertIn(command, help_reply.args[1])
        self.assertIn("help", unknown_reply.args[1])
        for sent in (help_reply, unknown_reply):
            self.assert_safe(sent.args[1])

    def test_checkpoint_precedes_broker_call_and_send_and_skips_duplicate_ids(self):
        def authenticate():
            self.assertEqual(self.state()["next_offset"], 8)
            self.assertEqual(self.state()["received_count"], 1)

        def send(chat_id, text):
            if text != listener._STARTUP_MESSAGE and "모의투자 로그인 성공" not in text:
                self.assertEqual(self.state()["next_offset"], 8)
            self.assertEqual(chat_id, _CHAT_ID)

        self.broker.authenticate.side_effect = authenticate
        self.telegram.send_message.side_effect = send
        self.run_once([update(7, "/status"), update(7, "/status")])
        self.broker.authenticate.assert_called_once()
        self.assertEqual(self.telegram.send_message.call_count, 3)

    def test_restart_uses_saved_offset_without_replaying_or_changing_subscription(self):
        self.run_once([update(2)])
        self.telegram.get_updates.reset_mock()
        self.telegram.send_message.reset_mock()
        self.run_once([update(2), update(3)])
        self.telegram.get_updates.assert_called_once_with(timeout=25, offset=3)
        self.assertEqual(self.telegram.send_message.call_count, 3)
        self.assertEqual(self.startup_issuer.call_count, 2)
        self.assertEqual(self.state()["next_offset"], 4)
        self.assertEqual(self.state()["poll_count"], 2)

    def test_changed_configuration_starts_with_fresh_offset_and_counters(self):
        self.run_once([update(9)])
        self.config_load.return_value = TelegramConfig(_TOKEN, 88888888, 123456789, "unit_test_bot")
        self.telegram.get_updates.reset_mock()
        self.run_once([])
        self.telegram.get_updates.assert_called_once_with(timeout=25, offset=None)
        self.assertIsNone(self.state()["next_offset"])
        self.assertEqual(self.state()["received_count"], 0)

    def test_duplicate_lock_fails_before_polling_or_keychain(self):
        path = self.directory / "listener.lock"
        with path.open("w") as locked:
            fcntl.flock(locked.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(listener.TelegramListenerError, "이미 실행"):
                self.run_once()
        self.telegram.get_updates.assert_not_called()
        self.config_load.assert_not_called()

    def test_webhook_is_refused_and_never_removed(self):
        self.telegram.get_webhook_info.return_value = {"url": "https://secret-message.example/"}
        with self.assertRaises(listener.TelegramListenerError) as caught:
            self.run_once()
        self.assert_safe(str(caught.exception))
        self.assertEqual(self.state()["status"], "failed")
        self.telegram.get_updates.assert_not_called()
        self.telegram.delete_webhook.assert_not_called()
        self.telegram.send_message.assert_not_called()
        self.startup_issuer.assert_not_called()

    def test_missing_or_failed_keychain_does_not_poll_or_expose_details(self):
        for result in (None, KeychainError(_TOKEN)):
            self.config_load.side_effect = result if isinstance(result, Exception) else None
            self.config_load.return_value = result if result is None else _CONFIG
            with self.assertRaises(listener.TelegramListenerError) as caught:
                self.run_once()
            self.assert_safe(str(caught.exception))
        self.telegram.get_updates.assert_not_called()
        self.startup_issuer.assert_not_called()

    def test_missing_mock_credentials_has_safe_reply_without_authentication(self):
        self.credentials.return_value = None
        self.run_once([update(text="/status")])
        self.assertIn("키가 저장되지 않았습니다", self.telegram.send_message.call_args.args[1])
        self.broker_factory.assert_not_called()

    def test_broker_exception_and_remote_price_never_echo_secrets(self):
        self.broker.authenticate.side_effect = KiwoomAPIError(_TOKEN + " secret-message")
        self.run_once([update(1, "/status")])
        self.assert_safe(self.telegram.send_message.call_args.args[1])
        self.broker.authenticate.side_effect = None
        self.broker.quote.return_value = {"cur_prc": "secret-message " + _TOKEN}
        self.run_once([update(2, "/status")])
        self.assert_safe(self.telegram.send_message.call_args.args[1])
        self.assert_safe(self.printed())

    def test_send_error_is_never_retried_even_after_restart(self):
        self.telegram.send_message.side_effect = TelegramAPIError(_TOKEN + " secret-message")
        self.run_once([update(1)])
        self.run_once([update(1)])
        self.assertEqual(self.telegram.send_message.call_count, 5)
        self.assertEqual(sum(entry.args[1] == listener._HELP for entry in self.telegram.send_message.call_args_list), 1)
        self.assertEqual(self.state()["received_count"], 1)
        self.assertEqual(self.state()["replied_count"], 0)
        self.assert_safe(self.printed())

    def test_poll_retries_are_bounded_and_sanitize_errors(self):
        self.telegram.get_updates.side_effect = TelegramAPIError(_TOKEN + " secret-message")
        try:
            listener.run_listener(self.directory)
        except listener.TelegramListenerError as error:
            self.assert_safe("".join(traceback.format_exception(error)))
        else:
            self.fail("Expected bounded poll failure")
        self.assertEqual(self.telegram.get_updates.call_count, 4)
        self.assertEqual(self.sleep.call_args_list, [call(1), call(2), call(4)])
        self.assertEqual(self.state()["status"], "failed")
        self.assert_safe(self.printed())

    def test_poll_conflict_fails_without_retry(self):
        self.telegram.get_updates.side_effect = TelegramAPIError("Telegram HTTP 오류 (code=409).")
        with self.assertRaises(listener.TelegramListenerError) as caught:
            listener.run_listener(self.directory)
        self.assertEqual(listener.listener_error_code(caught.exception), "poll_http_409")
        self.telegram.get_updates.assert_called_once()
        self.sleep.assert_not_called()

    def test_checkpoint_failure_stops_before_command_or_send(self):
        self.telegram.get_updates.return_value = [update(text="/status")]
        save = listener._save_state

        def save_or_fail(path, state):
            if state["next_offset"] is not None:
                raise listener.TelegramListenerError("안전한 저장 실패")
            return save(path, state)

        with patch.object(listener, "_save_state", side_effect=save_or_fail):
            with self.assertRaises(listener.TelegramListenerError):
                self.run_once()
        self.credentials.assert_not_called()
        self.assertEqual(self.telegram.send_message.call_count, 2)
        self.assertEqual(self.state()["received_count"], 0)

    def test_stop_request_after_poll_stops_without_replies_and_stale_stop_is_removed(self):
        stop = self.directory / "stop.request"
        stop.touch()

        def poll(**kwargs):
            self.assertFalse(stop.exists())
            stop.touch()
            return [update()]

        self.telegram.get_updates.side_effect = poll
        self.assertEqual(listener.run_listener(self.directory), 0)
        self.assertEqual(self.telegram.send_message.call_count, 2)
        self.assertIsNone(self.state()["next_offset"])
        self.assertEqual(self.state()["status"], "stopped")

    def test_invalid_or_symlinked_state_is_rejected_without_polling(self):
        state_path = self.directory / "state.json"
        state_path.write_text("secret-message")
        with self.assertRaises(listener.TelegramListenerError) as caught:
            self.run_once()
        self.assert_safe(str(caught.exception))
        state_path.unlink()
        target = self.directory / "other.json"
        target.write_text("{}")
        state_path.symlink_to(target)
        with self.assertRaises(listener.TelegramListenerError):
            self.run_once()
        self.telegram.get_updates.assert_not_called()

    def test_invalid_poll_bound_never_loads_credentials(self):
        for limit in (True, -1, "1", 1.5):
            with self.assertRaises(listener.TelegramListenerError):
                listener.run_listener(self.directory, max_polls=limit)
        self.config_load.assert_not_called()


if __name__ == "__main__":
    unittest.main()
