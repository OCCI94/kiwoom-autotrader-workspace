"""Full listener lifecycle with a stateful mock monitor and no external I/O."""

import contextlib
from datetime import datetime
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, call, patch

from kiwoom_autotrader.api.session import BrokerSession, IssuedToken, KST
from kiwoom_autotrader.config import settings
from kiwoom_autotrader.telegram import listener
from kiwoom_autotrader.telegram.bot_credentials import TelegramConfig
from kiwoom_autotrader.telegram.commands import crosses
from kiwoom_autotrader.utils.keychain import Credentials


BOT_TOKEN = "123456789:monitor_listener_fixture_abcdefghijklmnopqrstuvwxyz"
BROKER_TOKEN = "monitor-listener-offline-private-token"
CONFIG = TelegramConfig(BOT_TOKEN, 778899001, 123456789, "monitor_listener_bot")


def update(identifier, text, *, sender=None):
    return {"update_id": identifier, "message": {
        "date": 1000, "text": text,
        "chat": {"id": CONFIG.chat_id, "type": "private"},
        "from": {"id": CONFIG.chat_id if sender is None else sender, "is_bot": False},
    }}


class MonitorListenerTests(unittest.TestCase):
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
        self.root = Path(temporary.name)
        self.receiver = self.root / "receiver"
        self.settings_file = self.root / "config" / "settings.json"
        self.patch(settings, "settings_path", return_value=self.settings_file)
        self.patch(listener, "load_telegram_config", return_value=CONFIG)
        self.patch(listener.time, "time", return_value=1000)
        self.patch(listener.time, "sleep")
        self.telegram = self.patch(listener, "TelegramClient").return_value
        self.telegram.get_me.return_value = {"id": CONFIG.bot_id, "username": CONFIG.bot_username}
        self.telegram.get_webhook_info.return_value = {"url": ""}
        self.telegram.get_updates.return_value = []
        self.stop_channels = self.patch(listener, "stop_channels", return_value=True)
        self.buy = self.patch(listener, "handle_buy", return_value=["offline buy result"])
        self.sell = self.patch(listener, "handle_sell", return_value=["offline sell result"])
        self.session = BrokerSession(
            now=lambda: datetime(2026, 10, 26, 10, tzinfo=KST),
            credential_loader=Mock(return_value=Credentials("offline-key", "offline-secret")),
            token_issuer=Mock(return_value=IssuedToken(BROKER_TOKEN, "20300101090000")),
        )
        self.scheduler = Mock()
        self.scheduler.claim_due.return_value = None
        self.scheduler.seconds_until_next.return_value = None
        self.monitor = Mock(spec=["reset", "tick", "seconds_until_next"])
        self.monitor.seconds_until_next.return_value = None
        self.monitor_state = {"halted": True, "warmup": True}
        self.events = []
        self.observations = []
        def reset(*, clear_halt=False):
            self.events.append(("reset", clear_halt))
            self.monitor_state["warmup"] = False
            if clear_halt:
                self.monitor_state["halted"] = False
        def tick(session, notify):
            self.assertIs(session, self.session)
            self.events.append(("tick", None))
            self.observations.append({**self.monitor_state, **settings.load_settings()})
        self.monitor.reset.side_effect = reset
        self.monitor.tick.side_effect = tick
        actual_handler = listener.handle_cross_command
        def cross_command(text):
            self.events.append(("command", text.strip().casefold()))
            return actual_handler(text)
        self.cross_command = self.patch(listener, "handle_cross_command", side_effect=cross_command)
        self.output = io.StringIO()
        self.addCleanup(self.output.close)
        self.network = self.patch_path("socket.socket.connect", side_effect=AssertionError("Live network forbidden"))
        self.http = self.patch_path("urllib.request.build_opener", side_effect=AssertionError("Live HTTP forbidden"))
        self.keychain = self.patch_path("kiwoom_autotrader.utils.keychain._get_backend", side_effect=AssertionError("Live Keychain forbidden"))

    def patch(self, owner, name, **kwargs):
        patcher = patch.object(owner, name, **kwargs)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def patch_path(self, target, **kwargs):
        patcher = patch(target, **kwargs)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def receive(self, updates=None, *, polls=1):
        if updates is not None:
            self.telegram.get_updates.return_value = updates
        with contextlib.redirect_stdout(self.output):
            result = listener.run_listener(self.receiver, max_polls=polls,
                                           broker_session=self.session, scheduler=self.scheduler,
                                           monitor=self.monitor)
        self.assertEqual(result, 0)
        self.network.assert_not_called()
        self.http.assert_not_called()
        self.keychain.assert_not_called()
        self.assertIsNone(self.session.token)
        self.assertIsNone(self.session.mode)
        self.assertEqual(self.monitor.reset.call_args, call())
        return result

    def replies(self):
        return [row.args[1] for row in self.telegram.send_message.call_args_list]

    def test_successful_explicit_start_rearms_halt_after_persistence_before_tick(self):
        self.receive([update(1, " START DDCRS ")])
        self.assertTrue(settings.load_settings()["ddcrs_enabled"])
        self.assertIn(call(clear_halt=True), self.monitor.reset.call_args_list)
        self.assertFalse(self.observations[0]["halted"])
        self.assertFalse(self.observations[0]["warmup"])
        self.assertTrue(self.observations[0]["ddcrs_enabled"])
        command_index = self.events.index(("command", "start ddcrs"))
        reset_index = self.events.index(("reset", True))
        tick_index = self.events.index(("tick", None))
        self.assertLess(command_index, reset_index)
        self.assertLess(reset_index, tick_index)
        self.buy.assert_not_called()
        self.sell.assert_not_called()

    def test_failed_start_cannot_clear_halt_even_if_existing_saved_flag_is_true(self):
        settings.save_settings({"ddcrs_enabled": True})
        with patch.object(crosses, "save_settings", side_effect=settings.SettingsError("설정 저장 완료를 확인하지 못했습니다.")):
            self.receive([update(1, "start ddcrs")])
        self.assertNotIn(call(clear_halt=True), self.monitor.reset.call_args_list)
        self.assertTrue(self.observations[0]["halted"])
        self.assertTrue(settings.load_settings()["ddcrs_enabled"])
        self.assertTrue(any("확인하지 못했습니다" in reply for reply in self.replies()))

    def test_bad_periods_and_malformed_start_never_rearm_either_strategy(self):
        self.receive([update(1, "gdcrs intv 5 5"), update(2, "start ddcrs"),
                      update(3, "start ddcrs extra"), update(4, "start gdcrs")])
        self.assertNotIn(call(clear_halt=True), self.monitor.reset.call_args_list)
        self.assertTrue(self.observations[0]["halted"])
        self.assertFalse(settings.load_settings()["ddcrs_enabled"])
        self.assertFalse(settings.load_settings()["gdcrs_enabled"])
        self.buy.assert_not_called()
        self.sell.assert_not_called()

    def test_stop_period_hours_login_and_default_reset_clear_warmup_preserve_halt(self):
        settings.save_settings({"ddcrs_enabled": True})
        messages = [update(1, "stop ddcrs"), update(2, "gdcrs intv 10 30"),
                    update(3, "mkhr 9:30 15:00"), update(4, "login real"),
                    update(5, "stts default")]
        self.receive(messages)
        self.assertNotIn(call(clear_halt=True), self.monitor.reset.call_args_list)
        self.assertGreaterEqual(self.monitor.reset.call_count, len(messages) + 1)
        self.assertFalse(self.observations[0]["warmup"])
        self.assertTrue(self.observations[0]["halted"])
        self.assertEqual(settings.load_settings(), settings.default_settings())
        self.buy.assert_not_called()
        self.sell.assert_not_called()

    def test_all_incoming_commands_finish_before_the_single_monitor_tick(self):
        self.receive([update(1, "gdcrs intv 10 30"), update(2, "start ddcrs"), update(3, "stop ddcrs")])
        self.assertEqual(self.monitor.tick.call_count, 1)
        tick_index = self.events.index(("tick", None))
        command_positions = [index for index, (kind, _) in enumerate(self.events) if kind == "command"]
        self.assertEqual(len(command_positions), 3)
        self.assertTrue(all(index < tick_index for index in command_positions))
        self.assertFalse(self.observations[0]["ddcrs_enabled"])
        self.assertEqual((self.observations[0]["gdcrs_short_period"], self.observations[0]["gdcrs_long_period"]), (10, 30))

    def test_unauthorized_start_cannot_reset_or_rearm_monitor(self):
        self.receive([update(1, "start ddcrs", sender=CONFIG.chat_id + 1)])
        self.cross_command.assert_not_called()
        self.assertEqual(self.monitor.reset.call_args_list, [call()])  # final cleanup only
        self.assertTrue(self.observations[0]["warmup"])
        self.assertTrue(self.observations[0]["halted"])

    def test_power_off_skips_remaining_commands_and_monitor_work(self):
        self.receive([update(1, "power off"), update(2, "sell 005930 1"), update(3, "start ddcrs")], polls=3)
        self.monitor.tick.assert_not_called()
        self.cross_command.assert_not_called()
        self.sell.assert_not_called()
        self.buy.assert_not_called()
        self.stop_channels.assert_called_once_with()
        self.assertTrue((self.receiver / "stop.request").exists())
        state = json.loads((self.receiver / "state.json").read_text())
        self.assertEqual(state["status"], "stopped")
        self.assertEqual(state["next_offset"], 2)

    def test_external_stop_arriving_during_poll_blocks_commands_and_tick(self):
        def poll(**kwargs):
            (self.receiver / "stop.request").touch()
            return [update(1, "sell 005930 1")]
        self.telegram.get_updates.side_effect = poll
        self.receive(polls=3)
        self.monitor.tick.assert_not_called()
        self.sell.assert_not_called()
        self.buy.assert_not_called()
        self.telegram.get_updates.assert_called_once()

    def test_monitor_deadline_shortens_poll_and_notification_uses_regular_accounting(self):
        self.monitor.seconds_until_next.return_value = 1.2
        def tick(session, notify):
            self.assertIs(session, self.session)
            notify("ddcrs offline monitor status")
        self.monitor.tick.side_effect = tick
        self.receive()
        self.telegram.get_updates.assert_called_once_with(timeout=2, offset=None)
        self.assertEqual(self.replies()[-1], "ddcrs offline monitor status")
        state = json.loads((self.receiver / "state.json").read_text())
        self.assertEqual(state["replied_count"], 3)
        self.assertEqual(state["poll_count"], 1)

    def test_zero_poll_shutdown_does_no_monitor_work_and_preserves_ambiguity_halt(self):
        self.receive(polls=0)
        self.monitor.tick.assert_not_called()
        self.telegram.get_updates.assert_not_called()
        self.assertEqual(self.monitor.reset.call_args_list, [call()])
        self.assertTrue(self.monitor_state["halted"])
        self.buy.assert_not_called()
        self.sell.assert_not_called()
        visible = self.output.getvalue() + "\n".join(self.replies()) + (self.receiver / "state.json").read_text()
        for secret in (BOT_TOKEN, BROKER_TOKEN, "offline-key", "offline-secret"):
            self.assertNotIn(secret, visible)


if __name__ == "__main__":
    unittest.main()
