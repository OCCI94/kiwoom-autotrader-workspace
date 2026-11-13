"""Full receiver plus real temporary scheduler; all external effects are fake."""

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
from kiwoom_autotrader.utils.command_scheduler import CommandScheduler, SchedulerError
from kiwoom_autotrader.utils.keychain import Credentials


TOKEN = "123456789:extended_listener_fixture_abcdefghijklmnopqrstuvwxyz"
BROKER_TOKEN = "extended-listener-private-broker-token"
CONFIG = TelegramConfig(TOKEN, 778899001, 123456789, "extended_test_bot")


def update(identifier, text, **overrides):
    body = {"date": 1000, "text": text, "chat": {"id": CONFIG.chat_id, "type": "private"},
            "from": {"id": CONFIG.chat_id, "is_bot": False}}
    body.update(overrides)
    return {"update_id": identifier, "message": body}


class ExtendedListenerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.receiver_path = self.root / "receiver"
        self.settings_path = self.root / "config" / "data" / "settings.json"
        self.marker_path = self.settings_path.parent / "reservation-state.json"
        self.now = datetime(2026, 10, 26, 9, tzinfo=KST)  # Monday.
        self.patch(settings, "settings_path", return_value=self.settings_path)
        self.patch(listener, "load_telegram_config", return_value=CONFIG)
        self.telegram = self.patch(listener, "TelegramClient").return_value
        self.telegram.get_me.return_value = {"id": CONFIG.bot_id, "username": CONFIG.bot_username}
        self.telegram.get_webhook_info.return_value = {"url": ""}
        self.telegram.get_updates.return_value = []
        self.patch(listener.time, "time", return_value=1000)
        self.patch(listener.time, "sleep")
        self.buy = self.patch(listener, "handle_buy", return_value=["offline buy result"])
        self.sell = self.patch(listener, "handle_sell", return_value=["offline sell result"])
        self.report = self.patch(listener, "handle_report", return_value=["offline report result"])
        self.market_hours = self.patch(listener, "handle_market_hours", wraps=listener.handle_market_hours)
        self.show_settings = self.patch(listener, "handle_settings", wraps=listener.handle_settings)
        self.session = BrokerSession(
            now=lambda: self.now,
            credential_loader=Mock(return_value=Credentials("offline-key", "offline-secret")),
            token_issuer=Mock(return_value=IssuedToken(BROKER_TOKEN, "20300101090000")),
            save_renewal=Mock(side_effect=AssertionError("unexpected persistence callback")),
        )
        self.scheduler = self.new_scheduler()
        self.output = io.StringIO()
        self.addCleanup(self.output.close)
        # These guards make accidental integration I/O fail before leaving the process.
        self.native_keychain = self.patch_path("kiwoom_autotrader.utils.keychain._get_backend",
                                               side_effect=AssertionError("live Keychain forbidden"))
        self.network = self.patch_path("socket.socket.connect", side_effect=AssertionError("live network forbidden"))

    def patch(self, obj, name, **kwargs):
        patcher = patch.object(obj, name, **kwargs)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def patch_path(self, target, **kwargs):
        patcher = patch(target, **kwargs)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def new_scheduler(self):
        return CommandScheduler(self.settings_path, self.marker_path, now=lambda: self.now)

    def add_morning_reservation(self, command="buy 005930 1"):
        self.now = self.now.replace(hour=8, minute=0)
        creator = self.new_scheduler()
        row = creator.add("10:00", command)
        self.now = self.now.replace(hour=9)
        self.scheduler = self.new_scheduler()
        return row

    def receive(self, *, updates=None, polls=1):
        if updates is not None:
            self.telegram.get_updates.return_value = updates
        with contextlib.redirect_stdout(self.output):
            result = listener.run_listener(self.receiver_path, max_polls=polls,
                                           broker_session=self.session, scheduler=self.scheduler)
        self.network.assert_not_called()
        self.native_keychain.assert_not_called()
        return result

    def replies(self):
        return [entry.args[1] for entry in self.telegram.send_message.call_args_list]

    def receiver_state(self):
        return json.loads((self.receiver_path / "state.json").read_text())

    def assert_no_secrets(self):
        text = self.output.getvalue() + "\n".join(self.replies()) + (self.receiver_path / "state.json").read_text()
        for secret in (TOKEN, BROKER_TOKEN, "offline-key", "offline-secret"):
            self.assertNotIn(secret, text)

    def test_new_commands_dispatch_to_handlers_and_real_temporary_settings(self):
        messages = [update(1, " \tBuY 005930 1 "), update(2, "SELL 005930 2"),
                    update(3, "report"), update(4, " R "), update(5, "MkHr 9:30 15:00"),
                    update(6, " stts "), update(7, "HeLp")]
        self.assertEqual(self.receive(updates=messages), 0)
        self.buy.assert_called_once_with(" \tBuY 005930 1 ", self.session)
        self.sell.assert_called_once_with("SELL 005930 2", self.session)
        self.assertEqual(self.report.call_args_list, [call(self.session), call(self.session)])
        self.market_hours.assert_called_once_with("MkHr 9:30 15:00")
        self.show_settings.assert_called_once_with(" stts ", self.session)
        saved = settings.load_settings()
        self.assertEqual((saved["market_start_time"], saved["market_end_time"]), ("09:30", "15:00"))
        self.assertEqual(self.replies()[-1], listener._HELP)
        self.assertIn('"market_start_time": "09:30"', self.replies()[-2])
        self.assertEqual(self.receiver_state()["received_count"], 7)
        self.assertEqual(self.receiver_state()["replied_count"], 9)
        self.assertEqual(self.receiver_state()["next_offset"], 8)
        self.assert_no_secrets()

    def test_sender_restrictions_and_duplicate_updates_block_extra_order_effects(self):
        messages = [
            update(1, "buy 005930 1", chat={"id": CONFIG.chat_id + 1, "type": "private"}),
            update(2, "sell 005930 1", **{"from": {"id": CONFIG.chat_id + 1, "is_bot": False}}),
            update(3, "report", chat={"id": CONFIG.chat_id, "type": "group"}),
            update(4, "mkhr 8:30 15:00", date=999),
            update(5, "stts default", **{"from": {"id": CONFIG.chat_id, "is_bot": True}}),
            update(6, "buy 005930 1"), update(6, "buy 005930 1"),
        ]
        self.assertEqual(self.receive(updates=messages), 0)
        self.buy.assert_called_once_with("buy 005930 1", self.session)
        self.sell.assert_not_called()
        self.report.assert_not_called()
        self.market_hours.assert_not_called()
        self.show_settings.assert_not_called()
        self.assertEqual(self.receiver_state()["received_count"], 1)
        self.assertEqual(self.receiver_state()["next_offset"], 7)
        self.assertEqual(self.receiver_state()["replied_count"], 3)

    def test_actual_reservation_handler_registers_lists_and_removes_without_orders(self):
        self.assertEqual(self.receive(updates=[update(1, "rsv 10:00 buy 005930 1"),
                                               update(2, "rsv list"), update(3, "rsv remove 1")]), 0)
        self.assertIn("예약 1번 등록", self.replies()[2])
        self.assertIn("10:00 — buy 005930 1", self.replies()[3])
        self.assertIn("삭제했습니다", self.replies()[4])
        self.assertEqual(self.scheduler.list(), [])
        self.assertEqual(settings.load_settings()["next_reservation_id"], 2)
        self.buy.assert_not_called()

    def test_due_reservation_uses_same_buy_handler_after_durable_marker_and_notice(self):
        row = self.add_morning_reservation()
        events = []
        original_save = self.scheduler._save_state
        def save_marker(claimed):
            original_save(claimed)
            events.append("marker")
        self.patch(self.scheduler, "_save_state", side_effect=save_marker)
        def send(_, text):
            if "예약 #1 실행" in text:
                marker = json.loads(self.marker_path.read_text())
                self.assertEqual(marker["claimed"], {f"1:{row['created_at']}": "2026-10-26"})
                events.append("notice")
        self.telegram.send_message.side_effect = send
        def perform_buy(text, session):
            self.assertEqual(text, "buy 005930 1")
            self.assertIs(session, self.session)
            if not events:
                self.assertEqual(self.receiver_state()["next_offset"], 2)
                events.append("direct-buy")
            else:
                self.assertEqual(events, ["direct-buy", "marker", "notice"])
                events.append("scheduled-buy")
            return ["offline buy result"]
        self.buy.side_effect = perform_buy
        polls = 0
        def poll(**_):
            nonlocal polls
            polls += 1
            self.now = self.now.replace(hour=10)
            return [update(1, "buy 005930 1")] if polls == 1 else []
        self.telegram.get_updates.side_effect = poll
        self.assertEqual(self.receive(polls=4), 0)
        self.assertEqual(events, ["direct-buy", "marker", "notice", "scheduled-buy"])
        self.assertEqual(self.buy.call_args_list, [call("buy 005930 1", self.session)] * 2)
        self.assertEqual(sum("예약 #1 실행" in text for text in self.replies()), 1)
        self.assertEqual(self.telegram.get_updates.call_count, 4)
        self.assertEqual(self.receiver_state()["received_count"], 1)
        self.assert_no_secrets()

    def test_restart_after_deadline_does_not_catch_up_a_never_claimed_order(self):
        self.add_morning_reservation()
        self.now = self.now.replace(hour=10, minute=1)
        self.scheduler = self.new_scheduler()
        self.assertEqual(self.receive(polls=3), 0)
        self.buy.assert_not_called()
        self.assertFalse(self.marker_path.exists())
        self.assertFalse(any("예약 #1 실행" in text for text in self.replies()))

    def test_restart_after_successful_claim_does_not_repeat_order(self):
        row = self.add_morning_reservation()
        def poll(**_):
            self.now = self.now.replace(hour=10)
            return []
        self.telegram.get_updates.side_effect = poll
        self.assertEqual(self.receive(polls=2), 0)
        self.buy.assert_called_once()
        self.now = self.now.replace(hour=10, minute=1)
        self.scheduler = self.new_scheduler()
        self.telegram.get_updates.side_effect = None
        self.telegram.get_updates.return_value = []
        self.buy.reset_mock()
        self.telegram.send_message.reset_mock()
        self.assertEqual(self.receive(polls=3), 0)
        self.buy.assert_not_called()
        self.assertFalse(any("예약 #1 실행" in text for text in self.replies()))
        self.assertEqual(json.loads(self.marker_path.read_text())["claimed"],
                         {f"1:{row['created_at']}": "2026-10-26"})

    def test_marker_save_failure_blocks_order_keeps_polling_and_warns_once(self):
        self.add_morning_reservation()
        def poll(**_):
            self.now = self.now.replace(hour=10)
            return []
        self.telegram.get_updates.side_effect = poll
        save = self.patch(self.scheduler, "_save_state", side_effect=SchedulerError("offline private error"))
        self.assertEqual(self.receive(polls=5), 0)
        save.assert_called_once()
        self.buy.assert_not_called()
        self.assertEqual(self.telegram.get_updates.call_count, 5)
        self.assertFalse(self.marker_path.exists())
        warnings = [text for text in self.replies() if "예약 실행을 보류" in text]
        self.assertEqual(len(warnings), 1)
        self.assertFalse(any("예약 #1 실행" in text for text in self.replies()))
        self.assertNotIn("offline private error", self.output.getvalue() + "\n".join(self.replies()))

    def test_stts_default_removes_pending_schedule_before_later_poll_deadline(self):
        self.add_morning_reservation()
        self.session.set_renewal("10:30", persist=False)
        calls = 0
        def poll(**_):
            nonlocal calls
            calls += 1
            if calls == 1:
                return [update(1, "stts default")]
            self.now = self.now.replace(hour=10)
            return []
        self.telegram.get_updates.side_effect = poll
        self.assertEqual(self.receive(polls=4), 0)
        self.assertEqual(settings.load_settings(), settings.default_settings())
        self.assertEqual(self.scheduler.list(), [])
        self.assertEqual(self.session.renewal_time, "08:55")
        self.session._save_renewal.assert_not_called()
        self.buy.assert_not_called()
        self.assertFalse(any("예약 #1 실행" in text for text in self.replies()))


if __name__ == "__main__":
    unittest.main()
