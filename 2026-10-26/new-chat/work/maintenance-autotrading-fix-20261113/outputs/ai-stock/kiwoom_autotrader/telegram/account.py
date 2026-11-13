"""Explicit personal-account authentication using a memory-only MTProto session.

No messages are sent, no chats joined, and no Telegram Desktop data is accessed.
Phone, login code and 2FA password are used in memory only. Library logs are
suppressed at this boundary because RPC errors may contain private input.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import datetime, timezone
import inspect
import logging
import re
from typing import Any, Callable

from kiwoom_autotrader.utils.keychain import KeychainError
from kiwoom_autotrader.telegram.account_credentials import AccountConfig, save_account_config, validate_app
from kiwoom_autotrader.telegram.bot_credentials import load_telegram_config


class AccountConnectionError(RuntimeError):
    """A sanitized, user-facing connection error without RPC details."""

    retry_after_seconds: int | None = None


def _telethon():
    try:
        from telethon import TelegramClient, errors
        from telethon.sessions import StringSession
    except ImportError:
        raise AccountConnectionError("텔레그램 개인 계정 연결 라이브러리를 먼저 설치해야 합니다.") from None
    return TelegramClient, StringSession, errors


def _client(api_id: int, api_hash: str, session: str | None, *, receive_updates: bool = False):
    validate_app(api_id, api_hash)
    TelegramClient, StringSession, _ = _telethon()
    # A private logger keeps Telethon output out of launchd logs and chat output.
    logger = logging.getLogger("kiwoom_autotrader.private_telegram_account")
    logger.handlers = [logging.NullHandler()]
    logger.propagate = False
    logger.setLevel(logging.CRITICAL + 1)
    try:
        memory_session = StringSession(session)
        memory_session.save_entities = False
        return TelegramClient(
            memory_session, api_id, api_hash, timeout=15,
            request_retries=0, connection_retries=0, auto_reconnect=False,
            flood_sleep_threshold=0, receive_updates=receive_updates, base_logger=logger,
            device_model="Kiwoom Channel Reader", system_version="macOS",
            app_version="0.1", lang_code="ko", system_lang_code="ko",
        )
    except Exception:
        raise AccountConnectionError("텔레그램 개인 계정 연결을 준비하지 못했습니다.") from None


def make_client(config: AccountConfig):
    """Construct, but do not connect, a reader with a StringSession instance."""
    if not isinstance(config, AccountConfig):
        raise AccountConnectionError("저장된 텔레그램 개인 계정 설정이 필요합니다.")
    config.__post_init__()
    return _client(config.api_id, config.api_hash, config.session)


async def _input(callback: Callable[[], Any], *, password: bool = False) -> str:
    value = callback()
    if inspect.isawaitable(value):
        value = await value
    if not isinstance(value, str):
        raise AccountConnectionError("텔레그램 인증 입력 형식이 올바르지 않습니다.")
    if password:
        if not 1 <= len(value) <= 4096:
            raise AccountConnectionError("텔레그램 2단계 인증 비밀번호를 입력하세요.")
    else:
        value = value.strip()
        if re.fullmatch(r"[0-9]{4,10}", value) is None:
            raise AccountConnectionError("텔레그램 앱에 도착한 숫자 인증 코드를 입력하세요.")
    return value


def _user_id(user: Any, expected: int | None = None) -> int:
    value = getattr(user, "id", None)
    if (
        type(value) is not int or not 0 < value < (1 << 52)
        or getattr(user, "bot", None) is not False
        or (expected is not None and value != expected)
    ):
        raise AccountConnectionError("연결된 텔레그램 개인 계정을 확인하지 못했습니다.")
    return value


def _paired_user() -> int | None:
    paired = load_telegram_config()
    return paired.chat_id if paired is not None else None


def _is_rpc_error(error: Exception, errors: Any, *names: str) -> bool:
    """Check known RPC classes without depending on optional error exports."""
    classes = tuple(value for name in names if isinstance(
        value := getattr(errors, name, None), type,
    ) and issubclass(value, Exception))
    return isinstance(error, classes)


def _sanitize_error(error: Exception, errors: Any) -> AccountConnectionError:
    if _is_rpc_error(error, errors, "FloodWaitError"):
        seconds = getattr(error, "seconds", None)
        if type(seconds) is int and 0 < seconds <= 31_536_000:
            safe = AccountConnectionError(f"텔레그램 요청 제한으로 {seconds}초 후 다시 시도해야 합니다.")
            safe.retry_after_seconds = seconds
            return safe
        return AccountConnectionError("텔레그램 요청 제한이 해제된 뒤 다시 시도하세요.")
    for names, message in (
        (("ApiIdInvalidError",), "텔레그램 API ID와 API Hash가 일치하지 않거나 유효하지 않습니다. API 발급 화면의 값을 확인하세요."),
        (("ApiIdPublishedFloodError",), "이 텔레그램 API ID는 사용이 제한됐습니다. 본인 계정에서 발급한 API 정보를 사용하세요."),
        (("PhoneNumberInvalidError",), "텔레그램 전화번호가 올바르지 않습니다. 국가번호를 포함한 번호를 확인하세요."),
        (("PhoneCodeInvalidError", "PhoneCodeEmptyError"), "텔레그램 인증코드가 올바르지 않습니다. 방금 받은 코드를 확인하세요."),
        (("PhoneCodeExpiredError", "PhoneCodeHashInvalidError"), "텔레그램 인증코드가 만료됐습니다. 연결을 다시 시작해 새 코드를 받으세요."),
        (("PasswordHashInvalidError",), "텔레그램 2단계 인증 비밀번호가 올바르지 않습니다."),
        (("AuthTokenExpiredError", "AuthTokenInvalidError"), "텔레그램 QR 인증이 만료되었거나 유효하지 않습니다. 연결을 다시 시작하세요."),
    ):
        if _is_rpc_error(error, errors, *names):
            return AccountConnectionError(message)
    if isinstance(error, KeychainError):
        return AccountConnectionError("텔레그램 개인 계정 정보를 키체인에서 안전하게 처리하지 못했습니다.")
    return AccountConnectionError("텔레그램 개인 계정 인증에 실패했습니다. API 설정과 인증 입력을 확인하세요.")


async def connect_account(
    api_id: int, api_hash: str, phone: str,
    code_callback: Callable[[], Any], password_callback: Callable[[], Any],
) -> AccountConfig:
    """One login attempt; save only a verified personal account in Keychain.

    Callbacks take no arguments and may return strings or awaitables. No retry
    resends a login code. An unsaved newly authenticated session is revoked on
    failure when possible; existing saved credentials are left untouched.
    """
    try:
        validate_app(api_id, api_hash)
    except ValueError:
        raise AccountConnectionError("텔레그램 API ID와 API Hash 형식을 확인하세요.") from None
    if not isinstance(phone, str) or re.fullmatch(r"\+[1-9][0-9]{6,14}", phone) is None:
        raise AccountConnectionError("국가번호를 포함한 전화번호를 입력하세요. 예: +821012345678")
    if not callable(code_callback) or not callable(password_callback):
        raise AccountConnectionError("텔레그램 인증 입력을 준비하지 못했습니다.")
    _, _, errors = _telethon()
    client = None
    authorized = saved = False
    try:
        paired_user = _paired_user()
        client = _client(api_id, api_hash, None)
        await client.connect()
        sent = await client.send_code_request(phone)
        code_hash = getattr(sent, "phone_code_hash", None)
        if not isinstance(code_hash, str) or not code_hash:
            raise AccountConnectionError("텔레그램 인증 코드 요청을 확인하지 못했습니다.")
        code = await _input(code_callback)
        try:
            await client.sign_in(phone=phone, code=code, phone_code_hash=code_hash)
        except Exception as error:
            if not _is_rpc_error(error, errors, "SessionPasswordNeededError"):
                raise
            password = await _input(password_callback, password=True)
            await client.sign_in(password=password)
        authorized = True
        if not await client.is_user_authorized():
            raise AccountConnectionError("텔레그램 개인 계정 인증이 완료되지 않았습니다.")
        user_id = _user_id(await client.get_me(), paired_user)
        config = AccountConfig(api_id, api_hash, client.session.save(), user_id)
        save_account_config(config)
        saved = True
        return config
    except AccountConnectionError:
        raise
    except Exception as error:
        raise _sanitize_error(error, errors) from None
    finally:
        if client is not None:
            if authorized and not saved:
                try:
                    await client.log_out()
                except Exception:
                    pass
            try:
                await client.disconnect()
            except Exception:
                pass


async def connect_account_qr(
    api_id: int, api_hash: str,
    qr_callback: Callable[[str, datetime], Any],
    password_callback: Callable[[], Any],
) -> AccountConfig:
    """Authenticate with a QR shown locally; at most three expiring tokens.

    ``qr_callback(url, expires_at)`` is called after the token waiter has begun,
    so scanning immediately cannot race installation of Telethon's handler.
    It may be synchronous or asynchronous. QR URLs stay in memory and are not
    printed, logged or persisted. The caller should only render them locally.
    """
    try:
        validate_app(api_id, api_hash)
    except ValueError:
        raise AccountConnectionError("텔레그램 API ID와 API Hash 형식을 확인하세요.") from None
    if not callable(qr_callback) or not callable(password_callback):
        raise AccountConnectionError("텔레그램 QR 인증 화면을 준비하지 못했습니다.")
    _, _, errors = _telethon()
    client = None
    authorized = saved = False
    try:
        paired_user = _paired_user()
        # Enable updates in the constructor. set_receive_updates(True) would
        # issue an authenticated GetState request before this new login exists.
        client = _client(api_id, api_hash, None, receive_updates=True)
        await client.connect()
        for attempt in range(3):
            qr = await client.qr_login()
            url, expires = qr.url, qr.expires
            if (
                not isinstance(url, str)
                or re.fullmatch(r"tg://login\?token=[A-Za-z0-9_-]{16,1024}", url) is None
                or not isinstance(expires, datetime) or expires.tzinfo is None
                or expires.utcoffset() is None
            ):
                raise AccountConnectionError("텔레그램 QR 인증 정보를 확인하지 못했습니다.")
            seconds = max(0.0, min(120.0, (expires - datetime.now(timezone.utc)).total_seconds()))
            waiter = asyncio.create_task(qr.wait(timeout=seconds))
            try:
                # QRLogin.wait registers its handler before its first await.
                await asyncio.sleep(0)
                result = qr_callback(url, expires)
                if inspect.isawaitable(result):
                    await result
                await waiter
                authorized = True
                break
            except asyncio.TimeoutError:
                if attempt == 2:
                    raise AccountConnectionError("텔레그램 QR 인증 대기 시간이 지났습니다. 연결을 다시 시작하세요.") from None
            except Exception as error:
                if not _is_rpc_error(error, errors, "SessionPasswordNeededError"):
                    raise
                password = await _input(password_callback, password=True)
                await client.sign_in(password=password)
                authorized = True
                break
            finally:
                if not waiter.done():
                    waiter.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await waiter
        if not await client.is_user_authorized():
            raise AccountConnectionError("텔레그램 개인 계정 인증이 완료되지 않았습니다.")
        user_id = _user_id(await client.get_me(), paired_user)
        config = AccountConfig(api_id, api_hash, client.session.save(), user_id)
        save_account_config(config)
        saved = True
        return config
    except AccountConnectionError:
        raise
    except Exception as error:
        raise _sanitize_error(error, errors) from None
    finally:
        if client is not None:
            if not saved:
                # A very fast scan can authorize before a rendering callback
                # fails. Verify this case as well so an unsaved session is not
                # deliberately left active on Telegram.
                try:
                    authorized = authorized or await client.is_user_authorized()
                    if authorized:
                        await client.log_out()
                except Exception:
                    pass
            try:
                await client.disconnect()
            except Exception:
                pass


async def verify_account(client: Any, config: AccountConfig) -> AccountConfig:
    """Verify a restored session and persist a changed session only afterwards."""
    _, _, errors = _telethon()
    try:
        if not await client.is_user_authorized():
            raise AccountConnectionError("텔레그램 개인 계정 인증이 만료되어 다시 연결해야 합니다.")
        user_id = _user_id(await client.get_me(), config.user_id)
        paired_user = _paired_user()
        if paired_user is not None and paired_user != user_id:
            raise AccountConnectionError("알림 봇과 연결된 개인 계정이 일치하지 않습니다.")
        session = client.session.save()
        if session != config.session:
            config = AccountConfig(config.api_id, config.api_hash, session, user_id)
            save_account_config(config)
        return config
    except AccountConnectionError:
        raise
    except Exception as error:
        raise _sanitize_error(error, errors) from None
