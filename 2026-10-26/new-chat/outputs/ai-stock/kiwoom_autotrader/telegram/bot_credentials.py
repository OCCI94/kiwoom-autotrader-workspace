"""Store a verified personal Telegram notification destination in Keychain.

The caller must verify the bot and the user's own private chat before saving.
This module performs no network requests and never reads the mock-trading
credential item. The token and destination are one atomic JSON value in a
separate fixed Keychain item, with no plaintext or environment fallback.

Telegram format references verified 2026-10-26:
https://core.telegram.org/bots/api#authorizing-your-bot
https://core.telegram.org/bots/features#creating-a-new-bot
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

import kiwoom_autotrader.utils.keychain as _keychain
from kiwoom_autotrader.utils.keychain import KeychainError


_SERVICE = "com.local.kiwoom-autotrader.telegram"
_ACCOUNT = "default"
_TOKEN_PATTERN = re.compile(r"[1-9][0-9]{0,19}:[A-Za-z0-9_-]{20,200}")
_USERNAME_PATTERN = re.compile(r"[A-Za-z0-9_]{5,32}")
_FIELDS = frozenset({"version", "bot_token", "chat_id", "bot_id", "bot_username"})
_INVALID_DATA = "텔레그램 키체인 설정 형식이 올바르지 않습니다."


@dataclass(frozen=True)
class TelegramConfig:
    """A verified bot/private-chat configuration with a fully redacted repr."""

    bot_token: str = field(repr=False)
    chat_id: int = field(repr=False)
    bot_id: int = field(repr=False)
    bot_username: str = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.bot_token, str) or _TOKEN_PATTERN.fullmatch(self.bot_token) is None:
            raise ValueError("텔레그램 봇 토큰 형식이 올바르지 않습니다.")
        if type(self.chat_id) is not int or self.chat_id <= 0:
            raise ValueError("텔레그램 알림에는 본인의 개인 채팅 ID가 필요합니다.")
        if (
            type(self.bot_id) is not int
            or self.bot_id <= 0
            or self.bot_id != int(self.bot_token.split(":", 1)[0])
        ):
            raise ValueError("텔레그램 봇 식별 정보가 올바르지 않습니다.")
        if (
            not isinstance(self.bot_username, str)
            or _USERNAME_PATTERN.fullmatch(self.bot_username) is None
        ):
            raise ValueError("텔레그램 봇 사용자 이름 형식이 올바르지 않습니다.")


def _get_backend():
    return _keychain._get_backend(service=_SERVICE, account=_ACCOUNT)


def load_telegram_config() -> TelegramConfig | None:
    """Load only this app's Telegram item; missing is distinct from a failure."""
    status, raw = _keychain._call(_get_backend(), "load")
    status = _keychain._checked_status(status)
    if status == _keychain._NOT_FOUND:
        return None
    if status != _keychain._SUCCESS:
        raise _keychain._status_error(status)
    try:
        if not isinstance(raw, bytes) or not 0 < len(raw) <= _keychain._MAX_PAYLOAD_BYTES:
            raise ValueError
        payload = json.loads(raw.decode("utf-8"))
        if (
            not isinstance(payload, dict)
            or set(payload) != _FIELDS
            or type(payload["version"]) is not int
            or payload["version"] != 1
        ):
            raise ValueError
        return TelegramConfig(
            bot_token=payload["bot_token"],
            chat_id=payload["chat_id"],
            bot_id=payload["bot_id"],
            bot_username=payload["bot_username"],
        )
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise KeychainError(_INVALID_DATA) from None


def save_telegram_config(config: TelegramConfig) -> None:
    """Save all fields together after caller verification of the private chat."""
    if not isinstance(config, TelegramConfig):
        raise ValueError("유효한 텔레그램 알림 설정이 필요합니다.")
    config.__post_init__()
    raw = json.dumps(
        {
            "version": 1,
            "bot_token": config.bot_token,
            "chat_id": config.chat_id,
            "bot_id": config.bot_id,
            "bot_username": config.bot_username,
        },
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(raw) > _keychain._MAX_PAYLOAD_BYTES:
        raise ValueError("텔레그램 알림 설정의 크기가 저장 한도를 초과했습니다.")
    _keychain._write_payload(_get_backend(), raw)


def delete_telegram_config() -> bool:
    """Delete only the fixed Telegram item, leaving mock credentials intact."""
    status = _keychain._checked_status(_keychain._call(_get_backend(), "delete"))
    if status == _keychain._NOT_FOUND:
        return False
    if status != _keychain._SUCCESS:
        raise _keychain._status_error(status)
    return True
