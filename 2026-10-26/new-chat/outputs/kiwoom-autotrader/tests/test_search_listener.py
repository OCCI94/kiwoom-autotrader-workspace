"""Exercise search dispatch with startup login and the paired-chat boundary."""

import contextlib
from datetime import datetime
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from kiwoom_autotrader.api.session import BrokerSession, IssuedToken, KST
from kiwoom_autotrader.telegram import listener
from kiwoom_autotrader.telegram.bot_credentials import TelegramConfig
from kiwoom_autotrader.telegram.client import TelegramAPIError
from kiwoom_autotrader.utils.keychain import Credentials


CONFIG = TelegramConfig("123456789:offline_bot_token_abcdefghijklmnopqrstuvwxyz", 998877, 123456789, "offline_bot")


def update(identifier, text, *, chat_id=CONFIG.chat_id, sender=CONFIG.chat_id, date=1000):
    return {"update_id": identifier, "message": {
        "text": text, "date": date, "chat": {"id": chat_id, "type": "private"},
        "from": {"id": sender, "is_bot": False},
    }}


class SearchListenerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.issuer = Mock(return_value=IssuedToken("offline-search-token"))
        self.session = BrokerSession(
            now=lambda: datetime(2026, 10, 26, 9, tzinfo=KST),
            credential_loader=lambda _: Credentials("fixture-key", "fixture-secret"),
            token_issuer=self.issuer,
        )

    def receive(self, updates, handler=None, send=None):
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(listener, "load_telegram_config", return_value=CONFIG))
            stack.enter_context(patch.object(listener.time, "time", return_value=1000))
            scheduler = stack.enter_context(patch.object(listener, "CommandScheduler")).return_value
            scheduler.claim_due.return_value = None
            scheduler.seconds_until_next.return_value = None
            telegram = stack.enter_context(patch.object(listener, "TelegramClient")).return_value
            telegram.get_me.return_value = {"id": CONFIG.bot_id, "username": CONFIG.bot_username}
            telegram.get_webhook_info.return_value = {"url": ""}
            telegram.get_updates.return_value = updates
            telegram.send_message.side_effect = send
            search = stack.enter_context(patch.object(listener, "handle_search", side_effect=handler,
                                                       return_value=["종목 기본정보", "추가 지표"]))
            output = stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            result = listener.run_listener(self.directory, max_polls=1, broker_session=self.session)
            state = json.loads((self.directory / "state.json").read_text())
            replies = [call.args[1] for call in telegram.send_message.call_args_list]
            self.assertNotIn("offline-search-token", output.getvalue() + json.dumps(state) + "".join(replies))
            return result, state, replies, search

    def test_search_uses_startup_session_without_reauthentication_and_sends_all_parts(self):
        seen = []
        def search(text, session):
            seen.append((text, session is self.session, session.mode, session.token_usable))
            return ["종목 기본정보", "추가 지표"]
        result, state, replies, handler = self.receive([update(1, "  SrCh 005930  "), update(1, "srch 005930")], search)
        self.assertEqual(result, 0)
        self.assertEqual(seen, [("  SrCh 005930  ", True, "paper", True)])
        self.issuer.assert_called_once()
        handler.assert_called_once()
        self.assertEqual(replies[-2:], ["종목 기본정보", "추가 지표"])
        self.assertEqual(state["last_command"], "srch")
        self.assertEqual(state["next_offset"], 2)
        self.assertEqual(state["replied_count"], 4)
        self.assertIsNone(self.session.token)

    def test_unpaired_sender_and_old_search_do_not_reach_api(self):
        _, state, replies, handler = self.receive([
            update(1, "srch 005930", chat_id=44),
            update(2, "srch 005930", sender=44),
            update(3, "srch 005930", date=999),
        ])
        handler.assert_not_called()
        self.assertEqual(len(replies), 2)
        self.assertEqual(state["received_count"], 0)

    def test_missing_argument_reaches_search_help_and_never_falls_back_to_top(self):
        with patch.object(listener, "_reply") as fallback:
            _, state, _, handler = self.receive([update(1, "  SRCH  ")])
        handler.assert_called_once_with("  SRCH  ", self.session)
        fallback.assert_not_called()
        self.assertEqual(state["last_command"], "srch")

    def test_uncertain_first_part_stops_remaining_parts_without_replaying_command(self):
        def send(_, text):
            if text == "종목 기본정보":
                raise TelegramAPIError("uncertain send")
        _, state, replies, handler = self.receive([update(1, "srch 005930"), update(1, "srch 005930")], send=send)
        handler.assert_called_once()
        self.assertNotIn("추가 지표", replies)
        self.assertEqual(state["next_offset"], 2)
        self.assertEqual(state["replied_count"], 2)
        self.assertEqual(state["status"], "stopped")


if __name__ == "__main__":
    unittest.main()
