"""An authenticated personal Telegram session, stored only in macOS Keychain.

The in-memory StringSession contains an account authorization key. It must never
be written to a .session file, printed, logged or passed as a process argument.
Callers verify the account identity before saving this one atomic payload.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import re

import kiwoom_autotrader.utils.keychain as _keychain
from kiwoom_autotrader.utils.keychain import KeychainError


_SERVICE = "com.local.kiwoom-autotrader.telegram-account"
_FIELDS = frozenset({"version", "api_id", "api_hash", "session", "user_id"})
_INVALID_DATA = "텔레그램 개인 계정 키체인 설정 형식이 올바르지 않습니다."


@dataclass(frozen=True)
class AccountConfig:
    api_id: int = field(repr=False)
    api_hash: str = field(repr=False)
    session: str = field(repr=False)
    user_id: int = field(repr=False)

    def __post_init__(self) -> None:
        validate_app(self.api_id, self.api_hash)
        if (
            not isinstance(self.session, str)
            or re.fullmatch(r"1[A-Za-z0-9_=-]{100,4096}", self.session) is None
        ):
            raise ValueError("텔레그램 개인 계정 세션 형식이 올바르지 않습니다.")
        if type(self.user_id) is not int or not 0 < self.user_id < (1 << 52):
            raise ValueError("텔레그램 개인 계정 식별 정보가 올바르지 않습니다.")


def validate_app(api_id: int, api_hash: str) -> None:
    if type(api_id) is not int or not 0 < api_id < (1 << 31):
        raise ValueError("텔레그램 API ID는 양의 정수여야 합니다.")
    if not isinstance(api_hash, str) or re.fullmatch(r"[A-Fa-f0-9]{32}", api_hash) is None:
        raise ValueError("텔레그램 API Hash 형식이 올바르지 않습니다.")


def _get_backend():
    return _keychain._get_backend(service=_SERVICE, account="default")


def load_account_config() -> AccountConfig | None:
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
            not isinstance(payload, dict) or set(payload) != _FIELDS
            or type(payload["version"]) is not int or payload["version"] != 1
        ):
            raise ValueError
        return AccountConfig(**{name: payload[name] for name in _FIELDS - {"version"}})
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise KeychainError(_INVALID_DATA) from None


def save_account_config(config: AccountConfig) -> None:
    if not isinstance(config, AccountConfig):
        raise ValueError("유효한 텔레그램 개인 계정 설정이 필요합니다.")
    config.__post_init__()
    raw = json.dumps({
        "version": 1, "api_id": config.api_id, "api_hash": config.api_hash,
        "session": config.session, "user_id": config.user_id,
    }, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    if len(raw) > _keychain._MAX_PAYLOAD_BYTES:
        raise ValueError("텔레그램 개인 계정 설정이 저장 한도를 초과했습니다.")
    _keychain._write_payload(_get_backend(), raw)


def delete_account_config() -> bool:
    """Remove only this app's local account credential; not other Telegram data."""
    status = _keychain._checked_status(_keychain._call(_get_backend(), "delete"))
    if status == _keychain._NOT_FOUND:
        return False
    if status != _keychain._SUCCESS:
        raise _keychain._status_error(status)
    return True
