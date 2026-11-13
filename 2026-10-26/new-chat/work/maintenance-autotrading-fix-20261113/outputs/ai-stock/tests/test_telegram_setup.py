"""Telegram pairing tests use synthetic updates, never a real bot or Keychain."""

import copy
import contextlib
import itertools
import unittest
from unittest.mock import Mock, patch

from kiwoom_autotrader import telegram_setup
from kiwoom_autotrader.credentials import KeychainError
from kiwoom_autotrader.telegram import TelegramAPIError


NONCE = "synthetic-one-time-pairing-nonce"
STARTED_AT = 1800000000
PRIVATE_CHAT_ID = 123456789
BOT_ID = 987654321
FAKE_TOKEN = "987654321:AA_synthetic_bot_token_not_real_123456789"


def private_start(chat_id=PRIVATE_CHAT_ID, nonce=NONCE, date=STARTED_AT):
    return {
        "update_id": 7,
        "message": {
            "message_id": 3,
            "date": date,
            "text": f"/start {nonce}",
            "chat": {"id": chat_id, "type": "private"},
            "from": {"id": chat_id, "is_bot": False},
        },
    }


class PairPrivateChatTests(unittest.TestCase):
    def pair(self, updates):
        return telegram_setup.pair_private_chat(updates, NONCE, STARTED_AT)

    def test_matching_private_sender_is_selected(self):
        self.assertEqual(self.pair([private_start()]), PRIVATE_CHAT_ID)

    def test_old_message_is_ignored_but_boundary_time_is_allowed(self):
        self.assertIsNone(self.pair([private_start(date=STARTED_AT - 1)]))
        self.assertEqual(self.pair([private_start(date=STARTED_AT)]), PRIVATE_CHAT_ID)

    def test_exact_current_start_nonce_is_required(self):
        for text in (
            "/start", NONCE, f"/start {NONCE} ", f" /start {NONCE}",
            f"/START {NONCE}", f"/start  {NONCE}", f"/start {NONCE}\n",
            f"/start@UnitTestNotifyBot {NONCE}", "/start expired-nonce",
            None, 123, {"nonce": NONCE},
        ):
            with self.subTest(text=text):
                update = private_start()
                update["message"]["text"] = text
                self.assertIsNone(self.pair([update]))

    def test_nonprivate_chats_are_ignored(self):
        for chat_type in ("group", "supergroup", "channel", "Private", "", None, 1):
            with self.subTest(chat_type=chat_type):
                update = private_start()
                update["message"]["chat"]["type"] = chat_type
                self.assertIsNone(self.pair([update]))

    def test_sender_must_match_private_chat_and_not_be_a_bot(self):
        different_sender = private_start()
        different_sender["message"]["from"]["id"] = PRIVATE_CHAT_ID + 1
        self.assertIsNone(self.pair([different_sender]))
        for is_bot in (True, None, 0, 1, "false", "False"):
            with self.subTest(is_bot=is_bot):
                update = private_start()
                update["message"]["from"]["is_bot"] = is_bot
                self.assertIsNone(self.pair([update]))
        missing_is_bot = private_start()
        del missing_is_bot["message"]["from"]["is_bot"]
        self.assertIsNone(self.pair([missing_is_bot]))

    def test_ids_require_positive_integers_not_bool_or_coercible_strings(self):
        for invalid_id in (0, -1, True, False, "123456789", 123456789.0, None, [], {}):
            with self.subTest(invalid_id=invalid_id):
                update = private_start()
                update["message"]["chat"]["id"] = invalid_id
                update["message"]["from"]["id"] = invalid_id
                self.assertIsNone(self.pair([update]))
        sender_bool = private_start(chat_id=1)
        sender_bool["message"]["from"]["id"] = True
        self.assertIsNone(self.pair([sender_bool]))
        chat_bool = private_start(chat_id=1)
        chat_bool["message"]["chat"]["id"] = True
        self.assertIsNone(self.pair([chat_bool]))

    def test_date_requires_integer_timestamp(self):
        for invalid_date in (True, False, str(STARTED_AT), float(STARTED_AT), None, [], {}):
            with self.subTest(invalid_date=invalid_date):
                self.assertIsNone(self.pair([private_start(date=invalid_date)]))

    def test_unrelated_or_malformed_updates_are_ignored(self):
        malformed = [None, 1, "update", [], {}, {"message": None}, {"message": []}]
        for field in ("chat", "from"):
            for value in (None, [], "value", 1):
                update = private_start()
                update["message"][field] = value
                malformed.append(update)
            update = private_start()
            del update["message"][field]
            malformed.append(update)
        self.assertIsNone(self.pair(malformed))
        self.assertEqual(self.pair(malformed + [private_start()]), PRIVATE_CHAT_ID)

    def test_edited_or_channel_messages_cannot_pair(self):
        message = private_start()["message"]
        for kind in ("edited_message", "channel_post", "edited_channel_post", "callback_query"):
            with self.subTest(kind=kind):
                self.assertIsNone(self.pair([{kind: copy.deepcopy(message)}]))

    def test_repeated_matching_updates_for_same_person_are_not_ambiguous(self):
        self.assertEqual(self.pair([private_start(), private_start()]), PRIVATE_CHAT_ID)

    def test_two_distinct_matching_private_recipients_are_rejected(self):
        with self.assertRaises(telegram_setup.TelegramSetupError):
            self.pair([private_start(), private_start(chat_id=PRIVATE_CHAT_ID + 1)])

    def test_unrelated_sender_with_old_nonce_does_not_create_ambiguity(self):
        self.assertEqual(self.pair([
            private_start(), private_start(chat_id=PRIVATE_CHAT_ID + 1, nonce="expired-nonce"),
        ]), PRIVATE_CHAT_ID)


class ConfigureTelegramTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        # Every operation that might use the network or Keychain is mocked.
        self.client_class = self.stack.enter_context(patch.object(
            telegram_setup, "TelegramClient", autospec=True,
        ))
        self.client = self.client_class.return_value
        self.client.get_me.return_value = {
            "id": BOT_ID, "is_bot": True, "username": "UnitTestNotifyBot",
            "first_name": "Synthetic test bot",
        }
        self.client.get_webhook_info.return_value = {
            "url": "", "has_custom_certificate": False, "pending_update_count": 0,
        }
        self.client.get_updates.return_value = [private_start()]
        self.save = self.stack.enter_context(patch.object(
            telegram_setup, "save_telegram_config",
        ))
        self.nonce = self.stack.enter_context(patch.object(
            telegram_setup.secrets, "token_urlsafe", return_value=NONCE,
        ))
        self.stack.enter_context(patch.object(
            telegram_setup.time, "time", return_value=STARTED_AT,
        ))
        self.monotonic = self.stack.enter_context(patch.object(
            telegram_setup.time, "monotonic", side_effect=itertools.count(),
        ))
        self.stack.enter_context(patch.object(telegram_setup.time, "sleep"))
        self.stack.enter_context(patch(
            "socket.create_connection", side_effect=AssertionError("Network forbidden in setup tests"),
        ))
        self.stack.enter_context(patch(
            "builtins.input", side_effect=AssertionError("Setup must not request login codes"),
        ))
        self.stack.enter_context(patch(
            "getpass.getpass", side_effect=AssertionError("Setup already received its bot token"),
        ))
        self.emit = Mock()

    def configure(self, timeout_seconds=180):
        return telegram_setup.configure_telegram(
            FAKE_TOKEN, emit=self.emit, timeout_seconds=timeout_seconds,
        )

    def assert_no_secret_emission(self):
        for invocation in self.emit.call_args_list:
            self.assertNotIn(FAKE_TOKEN, str(invocation))

    def test_valid_pair_is_saved_before_exactly_one_test_message(self):
        events = []
        self.save.side_effect = lambda config: events.append("save")
        self.client.send_message.side_effect = lambda *args, **kwargs: events.append("send")
        config = self.configure()
        self.client_class.assert_called_once_with(FAKE_TOKEN)
        self.client.get_me.assert_called_once_with()
        self.client.get_webhook_info.assert_called_once_with()
        self.nonce.assert_called_once_with(16)
        self.assertEqual(config.bot_token, FAKE_TOKEN)
        self.assertEqual(config.chat_id, PRIVATE_CHAT_ID)
        self.assertEqual(config.bot_id, BOT_ID)
        self.assertEqual(config.bot_username, "UnitTestNotifyBot")
        self.save.assert_called_once_with(config)
        self.assertEqual(events, ["save", "send"])
        self.client.send_message.assert_called_once()
        sent_args, sent_kwargs = self.client.send_message.call_args
        self.assertEqual(sent_kwargs.get("chat_id", sent_args[0] if sent_args else None), PRIVATE_CHAT_ID)
        text = sent_kwargs.get("text", sent_args[1] if len(sent_args) > 1 else None)
        self.assertIsInstance(text, str)
        self.assertTrue(text.strip())
        self.assertNotIn(FAKE_TOKEN, text)
        self.assertNotIn(NONCE, text)
        self.assertIn(f"https://t.me/UnitTestNotifyBot?start={NONCE}", str(self.emit.call_args_list))
        self.assert_no_secret_emission()

    def test_test_message_is_fixed_across_different_pairing_challenges(self):
        self.configure()
        first_args, first_kwargs = self.client.send_message.call_args
        first_text = first_kwargs.get("text", first_args[1] if len(first_args) > 1 else None)
        fresh_nonce = "different-synthetic-one-time-nonce"
        self.nonce.return_value = fresh_nonce
        self.client.get_updates.return_value = [
            private_start(chat_id=PRIVATE_CHAT_ID + 1, nonce=fresh_nonce),
        ]
        self.configure()
        second_args, second_kwargs = self.client.send_message.call_args
        second_text = second_kwargs.get("text", second_args[1] if len(second_args) > 1 else None)
        self.assertEqual(first_text, second_text)
        self.assertEqual(self.client.send_message.call_count, 2)

    def test_updates_are_read_without_offset_or_queue_mutation(self):
        self.configure()
        self.client.get_updates.assert_called_once_with(timeout=10)
        self.assertEqual([item[0] for item in self.client.method_calls], [
            "get_me", "get_webhook_info", "get_updates", "send_message",
        ])

    def test_existing_webhook_is_not_removed_or_changed(self):
        self.client.get_webhook_info.return_value["url"] = "https://example.test/existing-bot-hook"
        with self.assertRaises(telegram_setup.TelegramSetupError):
            self.configure()
        self.save.assert_not_called()
        self.client.get_updates.assert_not_called()
        self.client.send_message.assert_not_called()
        self.assertEqual([item[0] for item in self.client.method_calls], [
            "get_me", "get_webhook_info",
        ])
        self.assert_no_secret_emission()

    def test_timeout_does_not_save_config_or_send_message(self):
        self.client.get_updates.return_value = []
        self.monotonic.side_effect = [0, 1, 181]
        with self.assertRaises(telegram_setup.TelegramSetupError):
            self.configure()
        self.client.get_updates.assert_called_once_with(timeout=10)
        self.save.assert_not_called()
        self.client.send_message.assert_not_called()
        self.assert_no_secret_emission()

    def test_previous_nonce_cannot_pair_during_new_setup(self):
        self.client.get_updates.return_value = [private_start(nonce="previous-setup-nonce")]
        self.monotonic.side_effect = [0, 1, 181]
        with self.assertRaises(telegram_setup.TelegramSetupError):
            self.configure()
        self.save.assert_not_called()
        self.client.send_message.assert_not_called()

    def test_full_existing_queue_is_not_consumed_to_find_a_new_request(self):
        self.client.get_updates.return_value = [
            private_start(nonce="previous-setup-nonce") for _ in range(100)
        ]
        with self.assertRaises(telegram_setup.TelegramSetupError):
            self.configure()
        self.client.get_updates.assert_called_once_with(timeout=10)
        self.save.assert_not_called()
        self.client.send_message.assert_not_called()

    def test_invalid_timeout_does_not_contact_telegram_or_save(self):
        for invalid in (0, -1, 601, True, 180.0, "180", None):
            with self.subTest(timeout=invalid):
                with self.assertRaises(telegram_setup.TelegramSetupError):
                    self.configure(timeout_seconds=invalid)
        self.client_class.assert_not_called()
        self.save.assert_not_called()

    def test_two_matching_people_do_not_save_or_send(self):
        self.client.get_updates.return_value = [
            private_start(), private_start(chat_id=PRIVATE_CHAT_ID + 1),
        ]
        with self.assertRaises(telegram_setup.TelegramSetupError):
            self.configure()
        self.save.assert_not_called()
        self.client.send_message.assert_not_called()

    def test_failed_storage_prevents_the_test_message(self):
        self.save.side_effect = KeychainError("Synthetic Keychain failure")
        with self.assertRaises(telegram_setup.TelegramSetupError):
            self.configure()
        self.save.assert_called_once()
        self.client.send_message.assert_not_called()

    def test_failed_test_message_is_not_retried(self):
        self.client.send_message.side_effect = TelegramAPIError(f"Synthetic error {FAKE_TOKEN}")
        with self.assertRaises(telegram_setup.TelegramSetupError) as raised:
            self.configure()
        self.save.assert_called_once()
        self.client.send_message.assert_called_once()
        self.assertNotIn(FAKE_TOKEN, str(raised.exception))
        self.assert_no_secret_emission()

    def test_api_failure_cannot_emit_token_or_save_config(self):
        self.client.get_me.side_effect = TelegramAPIError(f"Synthetic error {FAKE_TOKEN}")
        with self.assertRaises(telegram_setup.TelegramSetupError) as raised:
            self.configure()
        self.assertNotIn(FAKE_TOKEN, str(raised.exception))
        self.assert_no_secret_emission()
        self.save.assert_not_called()
        self.client.send_message.assert_not_called()


if __name__ == "__main__":
    unittest.main()
