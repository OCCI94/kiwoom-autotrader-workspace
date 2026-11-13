"""Two-monitor listener lifecycle, with actual temp settings and no live I/O."""
import contextlib
from datetime import datetime
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, call, patch

from kiwoom_autotrader.api.orders import OrderReceipt
from kiwoom_autotrader.api.session import BrokerSession, IssuedToken, KST
from kiwoom_autotrader.config import settings
from kiwoom_autotrader.telegram import listener
from kiwoom_autotrader.telegram.bot_credentials import TelegramConfig
from kiwoom_autotrader.telegram.commands import crosses
from kiwoom_autotrader.trading.order_context import complete_tracking, prepare_tracking
from kiwoom_autotrader.utils.keychain import Credentials


BOT_TOKEN = "123456789:golden_listener_fixture_abcdefghijklmnopqrstuvwxyz"
BROKER_TOKEN = "golden-listener-offline-private-token"
CONFIG = TelegramConfig(BOT_TOKEN, 778899001, 123456789, "golden_listener_bot")


def update(identifier, text):
    return {"update_id": identifier, "message": {
        "date": 1000, "text": text,
        "chat": {"id": CONFIG.chat_id, "type": "private"},
        "from": {"id": CONFIG.chat_id, "is_bot": False},
    }}


class GoldenListenerTests(unittest.TestCase):
    def setUp(self):
        recorder_patch = patch("kiwoom_autotrader.telegram.listener.ExecutionStreamRecorder")
        self.addCleanup(recorder_patch.stop)
        self.execution_recorder = recorder_patch.start().return_value
        self.execution_recorder.status.return_value = {"ready": True, "error": None, "coverage_complete": True}
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.receiver = self.root / "receiver"
        self.settings_file = self.root / "config" / "settings.json"
        self.patch(settings, "settings_path", return_value=self.settings_file)
        self.patch(listener, "load_telegram_config", return_value=CONFIG)
        self.patch(listener.time, "time", return_value=1000)
        self.patch(listener.time, "sleep")
        self.patch(listener, "terminal_report")
        self.telegram = self.patch(listener, "TelegramClient").return_value
        self.telegram.get_me.return_value = {"id": CONFIG.bot_id, "username": CONFIG.bot_username}
        self.telegram.get_webhook_info.return_value = {"url": ""}
        self.telegram.get_updates.return_value = []
        self.buy = self.patch(listener, "handle_buy", return_value=["offline buy reply"])
        self.sell = self.patch(listener, "handle_sell", return_value=["offline sell reply"])
        self.session = BrokerSession(
            now=lambda: datetime(2026, 10, 26, 10, tzinfo=KST),
            credential_loader=Mock(return_value=Credentials("offline-key", "offline-secret")),
            token_issuer=Mock(return_value=IssuedToken(BROKER_TOKEN, "20300101090000")),
        )
        self.scheduler = Mock()
        self.scheduler.claim_due.return_value = None
        self.scheduler.seconds_until_next.return_value = None
        self.websocket = Mock()
        self.websocket.snapshot.return_value = {"error": None}
        self.tracker = Mock()
        self.tracker.seconds_until_next.return_value = None
        self.tracker.prepare_order.return_value = "offline-intent"
        self.events = []
        self.observations = {"ddcrs": [], "gdcrs": []}
        self.states = {name: {"halted": True, "warmup": True} for name in self.observations}
        self.dd = self.monitor("ddcrs")
        self.gd = self.monitor("gdcrs")
        actual_cross = listener.handle_cross_command
        def cross_command(text):
            self.events.append(("command", text.strip().casefold()))
            return actual_cross(text)
        self.patch(listener, "handle_cross_command", side_effect=cross_command)
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

    def monitor(self, strategy):
        monitor = Mock(spec=["reset", "tick", "seconds_until_next"])
        monitor.seconds_until_next.return_value = None
        def reset(*, clear_halt=False):
            self.events.append((strategy + " reset", clear_halt))
            self.states[strategy]["warmup"] = False
            if clear_halt:
                self.states[strategy]["halted"] = False
        def tick(session, notify):
            self.assertIs(session, self.session)
            self.events.append((strategy + " tick", None))
            self.observations[strategy].append({**self.states[strategy], **settings.load_settings()})
        monitor.reset.side_effect = reset
        monitor.tick.side_effect = tick
        return monitor

    def dispatch(self, text):
        with contextlib.redirect_stdout(self.output):
            return listener._dispatch(listener._text_command(text, CONFIG), text, CONFIG, self.session,
                self.scheduler, monitor=self.dd, golden_monitor=self.gd)

    def receive(self, updates=None, *, polls=1):
        if updates is not None:
            self.telegram.get_updates.return_value = updates
        with contextlib.redirect_stdout(self.output):
            result = listener.run_listener(self.receiver, max_polls=polls,
                broker_session=self.session, scheduler=self.scheduler, monitor=self.dd,
                golden_monitor=self.gd, websocket_engine=self.websocket, execution_tracker=self.tracker)
        self.assertEqual(result, 0)
        self.network.assert_not_called()
        self.http.assert_not_called()
        self.keychain.assert_not_called()
        self.assertIsNone(self.session.token)
        self.assertIsNone(self.session.mode)
        self.websocket.close.assert_called_once_with()
        self.assertEqual(self.dd.reset.call_args, call())
        self.assertEqual(self.gd.reset.call_args, call())

    def replies(self):
        return [item.args[1] for item in self.telegram.send_message.call_args_list]

    def test_successful_start_rearms_only_golden_after_persistence_before_tick(self):
        self.receive([update(1, "START GDCRS")])
        self.assertTrue(settings.load_settings()["gdcrs_enabled"])
        self.assertIn(call(clear_halt=True), self.gd.reset.call_args_list)
        self.assertNotIn(call(clear_halt=True), self.dd.reset.call_args_list)
        self.assertFalse(self.observations["gdcrs"][0]["halted"])
        self.assertTrue(self.observations["gdcrs"][0]["gdcrs_enabled"])
        self.assertTrue(self.observations["ddcrs"][0]["halted"])
        self.assertTrue(self.observations["ddcrs"][0]["warmup"])
        self.assertLess(self.events.index(("command", "start gdcrs")), self.events.index(("gdcrs reset", True)))
        self.assertLess(self.events.index(("gdcrs reset", True)), self.events.index(("gdcrs tick", None)))
        self.buy.assert_not_called()
        self.sell.assert_not_called()

    def test_failed_start_never_clears_halt_even_with_prior_saved_enable(self):
        settings.save_settings({"gdcrs_enabled": True})
        with patch.object(crosses, "save_settings", side_effect=settings.SettingsError("설정 저장 완료를 확인하지 못했습니다.")):
            self.receive([update(1, "start gdcrs")])
        self.assertNotIn(call(clear_halt=True), self.gd.reset.call_args_list)
        self.assertNotIn(call(clear_halt=True), self.dd.reset.call_args_list)
        self.assertTrue(self.observations["gdcrs"][0]["halted"])
        self.assertTrue(settings.load_settings()["gdcrs_enabled"])
        self.assertTrue(any("확인하지 못했습니다" in reply for reply in self.replies()))

    def test_all_start_stop_commands_finish_before_serial_monitor_ticks(self):
        self.receive([update(1, "start gdcrs"), update(2, "start ddcrs"),
                      update(3, "stop gdcrs"), update(4, "stop ddcrs")])
        self.dd.tick.assert_called_once()
        self.gd.tick.assert_called_once()
        dd_index = self.events.index(("ddcrs tick", None))
        gd_index = self.events.index(("gdcrs tick", None))
        self.assertLess(dd_index, gd_index)
        self.assertTrue(all(index < dd_index for index, event in enumerate(self.events) if event[0] == "command"))
        for strategy in ("ddcrs", "gdcrs"):
            self.assertFalse(self.observations[strategy][0]["gdcrs_enabled"])
            self.assertFalse(self.observations[strategy][0]["ddcrs_enabled"])

    def test_stop_resets_only_its_own_strategy_and_preserves_other_flag(self):
        settings.save_settings({"gdcrs_enabled": True, "ddcrs_enabled": True})
        self.dispatch("stop gdcrs")
        self.gd.reset.assert_called_once_with()
        self.dd.reset.assert_not_called()
        self.assertFalse(settings.load_settings()["gdcrs_enabled"])
        self.assertTrue(settings.load_settings()["ddcrs_enabled"])
        self.gd.reset.reset_mock()
        self.dispatch("stop ddcrs")
        self.dd.reset.assert_called_once_with()
        self.gd.reset.assert_not_called()
        self.assertTrue(self.states["gdcrs"]["halted"])
        self.assertTrue(self.states["ddcrs"]["halted"])

    def test_login_hours_defaults_and_shared_period_reset_both_without_rearming(self):
        for text in ("login real", "mkhr 9:30 15:00", "stts default", "gdcrs intv 10 30"):
            with self.subTest(text=text):
                self.dd.reset.reset_mock()
                self.gd.reset.reset_mock()
                self.dispatch(text)
                self.dd.reset.assert_called_once_with()
                self.gd.reset.assert_called_once_with()
                self.assertTrue(self.states["gdcrs"]["halted"])
                self.assertTrue(self.states["ddcrs"]["halted"])
        self.network.assert_not_called()
        self.http.assert_not_called()
        self.keychain.assert_not_called()

    def test_watchlist_mutations_reset_only_golden_and_list_resets_neither(self):
        for text in ("gdcrs add 005930 100000", "gdcrs remove 1", "gdcrs clear"):
            with self.subTest(text=text):
                self.gd.reset.reset_mock()
                self.dispatch(text)
                self.gd.reset.assert_called_once_with()
                self.dd.reset.assert_not_called()
        self.gd.reset.reset_mock()
        self.dispatch("gdcrs list")
        self.gd.reset.assert_not_called()
        self.dd.reset.assert_not_called()

    def test_each_monitor_tick_uses_its_own_durable_order_context(self):
        def tick(strategy, side):
            def run(session, notify):
                tracking = prepare_tracking("paper", side, "005930", "1")
                self.assertIsNotNone(tracking)
                receipt = OrderReceipt("paper", side, "005930", "1", None, "0000001", "https://mockapi.kiwoom.com")
                complete_tracking(tracking, receipt)
            return run
        self.dd.tick.side_effect = tick("ddcrs", "sell")
        self.gd.tick.side_effect = tick("gdcrs", "buy")
        self.receive()
        self.assertEqual(self.tracker.prepare_order.call_args_list,
            [call("paper", "sell", "005930", "1", "ddcrs"), call("paper", "buy", "005930", "1", "gdcrs")])
        self.assertEqual([entry.args[1] for entry in self.tracker.register_order.call_args_list], ["ddcrs", "gdcrs"])
        self.assertIsNone(prepare_tracking("paper", "buy", "005930", "1"))

    def test_golden_deadline_shortens_poll_and_its_notification_is_accounted(self):
        self.gd.seconds_until_next.return_value = 1.2
        self.dd.seconds_until_next.return_value = 8
        self.gd.tick.side_effect = lambda session, notify: notify("gdcrs offline monitoring status")
        self.receive()
        self.telegram.get_updates.assert_called_once_with(timeout=2, offset=None)
        self.assertEqual(self.replies()[-1], "gdcrs offline monitoring status")
        state = json.loads((self.receiver / "state.json").read_text())
        self.assertEqual(state["replied_count"], 3)
        self.assertEqual(state["poll_count"], 1)

    def test_external_stop_between_ticks_skips_golden_and_remaining_tracker_work(self):
        self.dd.tick.side_effect = lambda session, notify: (self.receiver / "stop.request").touch()
        self.receive(polls=3)
        self.dd.tick.assert_called_once()
        self.gd.tick.assert_not_called()
        self.tracker.tick.assert_called_once()  # startup reconciliation only
        self.telegram.get_updates.assert_called_once()
        self.assertTrue(self.states["gdcrs"]["halted"])
        self.assertTrue(self.states["ddcrs"]["halted"])

    def test_external_stop_during_poll_blocks_commands_and_both_monitors(self):
        def poll(**kwargs):
            (self.receiver / "stop.request").touch()
            return [update(1, "start gdcrs"), update(2, "buy 005930 1")]
        self.telegram.get_updates.side_effect = poll
        self.receive(polls=3)
        self.dd.tick.assert_not_called()
        self.gd.tick.assert_not_called()
        self.buy.assert_not_called()
        self.assertFalse(settings.load_settings()["gdcrs_enabled"])
        self.assertNotIn(call(clear_halt=True), self.gd.reset.call_args_list)

    def test_cleanup_resets_both_without_rearming_even_if_first_reset_fails(self):
        self.dd.reset.side_effect = RuntimeError("offline reset failure")
        self.receive(polls=0)
        self.dd.tick.assert_not_called()
        self.gd.tick.assert_not_called()
        self.dd.reset.assert_called_once_with()
        self.gd.reset.assert_called_once_with()
        self.assertTrue(self.states["gdcrs"]["halted"])
        visible = self.output.getvalue() + "\n".join(self.replies()) + (self.receiver / "state.json").read_text()
        for secret in (BOT_TOKEN, BROKER_TOKEN, "offline-key", "offline-secret"):
            self.assertNotIn(secret, visible)

    def test_rank_recursion_forwards_golden_monitor_and_stop_callback(self):
        stop = Mock(return_value=False)
        def batch(text, session, execute, *, should_stop):
            self.assertIs(should_stop, stop)
            return [*execute("gdcrs add 005930 100000"), *execute("start gdcrs")]
        with patch.object(listener, "handle_rank_batch", side_effect=batch):
            listener._dispatch("rank", "rank test", CONFIG, self.session, self.scheduler,
                monitor=self.dd, golden_monitor=self.gd, should_stop=stop)
        self.assertEqual(self.gd.reset.call_args_list, [call(), call(clear_halt=True)])
        self.dd.reset.assert_not_called()
        self.assertTrue(settings.load_settings()["gdcrs_enabled"])
        self.assertEqual(settings.load_settings()["gdcrs_stocks"], [{"code": "005930", "budget": 100000}])


if __name__ == "__main__":
    unittest.main()
