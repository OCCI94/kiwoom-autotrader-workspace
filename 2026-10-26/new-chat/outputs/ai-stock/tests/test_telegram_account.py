"""Account auth tests: no real Telegram client, credentials or network."""

import asyncio
from datetime import datetime, timedelta, timezone
import logging
from types import SimpleNamespace
import traceback
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from kiwoom_autotrader import telegram_account as account
from kiwoom_autotrader.credentials import KeychainError
from kiwoom_autotrader.telegram_account_credentials import AccountConfig


class PasswordNeeded(Exception):
    pass


class FloodWait(Exception):
    def __init__(self, seconds):
        self.seconds = seconds
        super().__init__("sensitive RPC contents")


ERRORS = SimpleNamespace(SessionPasswordNeededError=PasswordNeeded, FloodWaitError=FloodWait)
API_ID = 123456
API_HASH = "abcdef0123456789" * 2
SESSION = "1" + "safe_test_session_" * 21
PHONE = "+821012345678"
USER_ID = 234567890
CONFIG = AccountConfig(API_ID, API_HASH, SESSION, USER_ID)


class AccountAuthTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.telethon = self.patch("_telethon", return_value=(MagicMock(), MagicMock(), ERRORS))
        self.client = MagicMock()
        for name in ("connect", "disconnect", "log_out", "send_code_request", "sign_in",
                     "is_user_authorized", "get_me"):
            setattr(self.client, name, AsyncMock())
        self.client.send_code_request.return_value = SimpleNamespace(phone_code_hash="private-code-hash")
        self.client.is_user_authorized.return_value = True
        self.client.get_me.return_value = SimpleNamespace(id=USER_ID, bot=False)
        self.client.session.save.return_value = SESSION
        self.factory = self.patch("_client", return_value=self.client)
        self.paired = self.patch("load_telegram_config", return_value=SimpleNamespace(chat_id=USER_ID))
        self.save = self.patch("save_account_config")
        self.code = MagicMock(return_value="54321")
        self.password = MagicMock(return_value="private-password")

    def patch(self, name, **kwargs):
        patcher = patch("kiwoom_autotrader.telegram_account." + name, **kwargs)
        self.addCleanup(patcher.stop)
        return patcher.start()

    async def login(self):
        return await account.connect_account(API_ID, API_HASH, PHONE, self.code, self.password)

    def assert_private(self, text):
        for secret in (API_HASH, SESSION, PHONE, "54321", "private-password",
                       "private-code-hash", "sensitive RPC contents"):
            self.assertNotIn(secret, text)

    async def test_login_saves_only_after_server_and_paired_identity_verification(self):
        self.save.side_effect = lambda config: self.client.get_me.assert_awaited_once()
        result = await self.login()
        self.assertEqual(result, CONFIG)
        self.factory.assert_called_once_with(API_ID, API_HASH, None)
        self.client.send_code_request.assert_awaited_once_with(PHONE)
        self.client.sign_in.assert_awaited_once_with(
            phone=PHONE, code="54321", phone_code_hash="private-code-hash",
        )
        self.save.assert_called_once_with(CONFIG)
        self.password.assert_not_called()
        self.client.log_out.assert_not_awaited()
        self.client.disconnect.assert_awaited_once()

    async def test_two_factor_password_requested_only_when_required(self):
        self.client.sign_in.side_effect = [PasswordNeeded(), None]
        self.assertEqual(await self.login(), CONFIG)
        self.password.assert_called_once_with()
        self.assertEqual(self.client.sign_in.await_count, 2)
        self.assertEqual(self.client.sign_in.call_args.kwargs, {"password": "private-password"})
        self.client.send_code_request.assert_awaited_once()

    async def test_async_input_callbacks_supported(self):
        self.code = AsyncMock(return_value="54321")
        self.password = AsyncMock(return_value="private-password")
        self.client.sign_in.side_effect = [PasswordNeeded(), None]
        await self.login()
        self.code.assert_awaited_once()
        self.password.assert_awaited_once()

    async def test_wrong_account_and_bot_account_never_replace_saved_config(self):
        for user in (SimpleNamespace(id=USER_ID + 1, bot=False),
                     SimpleNamespace(id=USER_ID, bot=True)):
            self.client.get_me.return_value = user
            with self.assertRaises(account.AccountConnectionError):
                await self.login()
        self.save.assert_not_called()
        self.assertEqual(self.client.log_out.await_count, 2)

    async def test_unpaired_setup_allows_verified_personal_user(self):
        self.paired.return_value = None
        self.assertEqual(await self.login(), CONFIG)

    async def test_keychain_failure_revokes_unsaved_new_session(self):
        self.save.side_effect = KeychainError(SESSION)
        with self.assertRaises(account.AccountConnectionError) as caught:
            await self.login()
        self.assert_private("".join(traceback.format_exception(caught.exception)))
        self.client.log_out.assert_awaited_once()
        self.client.disconnect.assert_awaited_once()

    async def test_invalid_auth_does_not_retry_or_expose_rpc_details(self):
        self.client.sign_in.side_effect = RuntimeError("sensitive RPC contents")
        with self.assertRaises(account.AccountConnectionError) as caught:
            await self.login()
        self.assert_private("".join(traceback.format_exception(caught.exception)))
        self.client.send_code_request.assert_awaited_once()
        self.client.sign_in.assert_awaited_once()
        self.save.assert_not_called()
        self.client.disconnect.assert_awaited_once()

    async def test_flood_wait_is_reported_without_resending_login_code(self):
        self.client.send_code_request.side_effect = FloodWait(120)
        with self.assertRaisesRegex(account.AccountConnectionError, "120초") as caught:
            await self.login()
        self.assert_private(str(caught.exception))
        self.assertEqual(caught.exception.retry_after_seconds, 120)
        self.client.send_code_request.assert_awaited_once()
        self.code.assert_not_called()
        self.save.assert_not_called()

    async def test_invalid_input_stops_before_network(self):
        for app_id, app_hash, phone in ((True, API_HASH, PHONE), (API_ID, "bad", PHONE),
                                         (API_ID, API_HASH, "01012345678")):
            with self.assertRaises(account.AccountConnectionError):
                await account.connect_account(app_id, app_hash, phone, self.code, self.password)
        self.factory.assert_not_called()

    async def test_bad_code_callback_does_not_submit_arbitrary_input(self):
        self.code.return_value = "bot-token:private"
        with self.assertRaises(account.AccountConnectionError):
            await self.login()
        self.client.sign_in.assert_not_awaited()
        self.save.assert_not_called()

    async def test_existing_verified_session_is_not_unnecessarily_saved(self):
        self.assertEqual(await account.verify_account(self.client, CONFIG), CONFIG)
        self.save.assert_not_called()

    async def test_changed_session_saved_only_after_identity_check(self):
        rotated = "1" + "new_test_session_" * 22
        self.client.session.save.return_value = rotated
        result = await account.verify_account(self.client, CONFIG)
        self.assertEqual(result.session, rotated)
        self.save.assert_called_once_with(result)
        self.client.get_me.return_value = SimpleNamespace(id=USER_ID + 1, bot=False)
        self.save.reset_mock()
        with self.assertRaises(account.AccountConnectionError):
            await account.verify_account(self.client, CONFIG)
        self.save.assert_not_called()

    async def test_expired_session_fails_without_interactive_login(self):
        self.client.is_user_authorized.return_value = False
        with self.assertRaises(account.AccountConnectionError):
            await account.verify_account(self.client, CONFIG)
        self.client.send_code_request.assert_not_awaited()
        self.save.assert_not_called()


class AccountClientConstructionTests(unittest.TestCase):
    def test_only_memory_session_used_and_library_logging_disabled(self):
        client_factory = MagicMock()
        session_factory = MagicMock()
        with patch.object(account, "_telethon", return_value=(client_factory, session_factory, ERRORS)):
            result = account.make_client(CONFIG)
        session_factory.assert_called_once_with(SESSION)
        self.assertFalse(session_factory.return_value.save_entities)
        self.assertIs(result, client_factory.return_value)
        self.assertIs(client_factory.call_args.args[0], session_factory.return_value)
        kwargs = client_factory.call_args.kwargs
        self.assertEqual(kwargs["request_retries"], 0)
        self.assertEqual(kwargs["connection_retries"], 0)
        self.assertEqual(kwargs["flood_sleep_threshold"], 0)
        self.assertFalse(kwargs["receive_updates"])
        self.assertGreater(kwargs["base_logger"].level, logging.CRITICAL)
        self.assertFalse(kwargs["base_logger"].propagate)

    def test_session_parser_error_is_sanitized(self):
        with patch.object(account, "_telethon", return_value=(MagicMock(), MagicMock(side_effect=ValueError(SESSION)), ERRORS)):
            with self.assertRaises(account.AccountConnectionError) as caught:
                account.make_client(CONFIG)
        self.assertNotIn(SESSION, "".join(traceback.format_exception(caught.exception)))


class QRAccountAuthTests(unittest.IsolatedAsyncioTestCase):
    patch = AccountAuthTests.patch
    assert_private = AccountAuthTests.assert_private

    def setUp(self):
        AccountAuthTests.setUp(self)
        self.client.set_receive_updates = AsyncMock()
        self.client.qr_login = AsyncMock()
        self.qr = SimpleNamespace(
            url="tg://login?token=" + "test_only_token_1234567890" * 2,
            expires=datetime.now(timezone.utc) + timedelta(seconds=60),
            wait=AsyncMock(return_value=SimpleNamespace(id=USER_ID, bot=False)),
        )
        self.client.qr_login.return_value = self.qr
        self.show = MagicMock()

    async def qr_login(self):
        return await account.connect_account_qr(API_ID, API_HASH, self.show, self.password)

    async def test_qr_auth_waiter_started_before_display_and_identity_checked_before_save(self):
        waiting = []
        async def wait(**kwargs):
            waiting.append(True)
            self.assertLessEqual(kwargs["timeout"], 120)
            return self.client.get_me.return_value
        self.qr.wait.side_effect = wait
        self.show.side_effect = lambda url, expires: self.assertTrue(waiting)
        result = await self.qr_login()
        self.assertEqual(result, CONFIG)
        self.factory.assert_called_once_with(API_ID, API_HASH, None, receive_updates=True)
        self.client.set_receive_updates.assert_not_awaited()
        self.show.assert_called_once_with(self.qr.url, self.qr.expires)
        self.client.send_code_request.assert_not_awaited()
        self.client.sign_in.assert_not_awaited()
        self.save.assert_called_once_with(CONFIG)
        self.client.disconnect.assert_awaited_once()

    async def test_async_qr_display_supported(self):
        self.show = AsyncMock()
        await self.qr_login()
        self.show.assert_awaited_once_with(self.qr.url, self.qr.expires)

    async def test_qr_two_factor_password_is_requested_after_scan(self):
        self.qr.wait.side_effect = PasswordNeeded()
        await self.qr_login()
        self.password.assert_called_once()
        self.client.sign_in.assert_awaited_once_with(password="private-password")
        self.client.qr_login.assert_awaited_once()
        self.save.assert_called_once_with(CONFIG)

    async def test_qr_expiry_refresh_is_bounded_to_three_tokens(self):
        self.qr.wait.side_effect = asyncio.TimeoutError()
        self.client.is_user_authorized.return_value = False
        with self.assertRaisesRegex(account.AccountConnectionError, "대기 시간") as caught:
            await self.qr_login()
        self.assertNotIn(self.qr.url, str(caught.exception))
        self.assertEqual(self.client.qr_login.await_count, 3)
        self.assertEqual(self.show.call_count, 3)
        self.save.assert_not_called()
        self.client.log_out.assert_not_awaited()
        self.client.disconnect.assert_awaited_once()

    async def test_qr_wrong_account_revokes_unsaved_session(self):
        self.client.get_me.return_value = SimpleNamespace(id=USER_ID + 1, bot=False)
        with self.assertRaises(account.AccountConnectionError):
            await self.qr_login()
        self.save.assert_not_called()
        self.client.log_out.assert_awaited_once()

    async def test_qr_save_failure_revokes_session_without_exposing_secret(self):
        self.save.side_effect = KeychainError(SESSION)
        with self.assertRaises(account.AccountConnectionError) as caught:
            await self.qr_login()
        self.assert_private("".join(traceback.format_exception(caught.exception)))
        self.client.log_out.assert_awaited_once()

    async def test_qr_rendering_failure_cancels_waiter_and_revokes_fast_scan(self):
        completed = []
        async def wait(**_kwargs):
            try:
                await asyncio.Event().wait()
            finally:
                completed.append(True)
        self.qr.wait.side_effect = wait
        self.show.side_effect = RuntimeError(self.qr.url)
        with self.assertRaises(account.AccountConnectionError) as caught:
            await self.qr_login()
        self.assertNotIn(self.qr.url, "".join(traceback.format_exception(caught.exception)))
        self.assertTrue(completed)
        self.client.log_out.assert_awaited_once()
        self.save.assert_not_called()

    async def test_qr_invalid_link_is_never_displayed(self):
        self.qr.url = "https://untrusted.invalid/login?private-token"
        self.client.is_user_authorized.return_value = False
        with self.assertRaises(account.AccountConnectionError):
            await self.qr_login()
        self.show.assert_not_called()
        self.qr.wait.assert_not_awaited()

    async def test_qr_flood_wait_retains_retry_seconds(self):
        self.client.qr_login.side_effect = FloodWait(240)
        self.client.is_user_authorized.return_value = False
        with self.assertRaises(account.AccountConnectionError) as caught:
            await self.qr_login()
        self.assertEqual(caught.exception.retry_after_seconds, 240)
        self.assert_private("".join(traceback.format_exception(caught.exception)))
        self.show.assert_not_called()


class AuthErrorClassificationTests(unittest.TestCase):
    def test_known_rpc_categories_are_actionable_but_never_echo_inputs(self):
        for name, expected in (
            ("ApiIdInvalidError", "API ID"), ("ApiIdPublishedFloodError", "사용이 제한"),
            ("PhoneNumberInvalidError", "전화번호"), ("PhoneCodeInvalidError", "코드가 올바르지"),
            ("PhoneCodeExpiredError", "코드가 만료"), ("PasswordHashInvalidError", "비밀번호"),
        ):
            cls = type(name, (Exception,), {})
            errors = SimpleNamespace(**{name: cls})
            safe = account._sanitize_error(cls(SESSION), errors)
            self.assertIn(expected, str(safe))
            self.assertNotIn(SESSION, str(safe))

    def test_missing_or_nonclass_error_exports_fail_safely(self):
        for errors in (SimpleNamespace(), SimpleNamespace(ApiIdInvalidError="bad export")):
            safe = account._sanitize_error(ValueError(SESSION), errors)
            self.assertIsInstance(safe, account.AccountConnectionError)
            self.assertNotIn(SESSION, str(safe))


if __name__ == "__main__":
    unittest.main()
