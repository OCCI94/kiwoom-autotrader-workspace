"""Separately stored Kiwoom real-environment credentials; never reuse mock keys.

Callers verify real token issuance before saving. The native Keychain adapter
stores one atomic payload and does not change ACLs or fall back to plaintext.
"""

import json

import kiwoom_autotrader.utils.keychain as _keychain
from kiwoom_autotrader.utils.keychain import Credentials, KeychainError


_SERVICE = "com.local.kiwoom-autotrader.real"
_FIELDS = {"version", "app_key", "app_secret"}


def _get_backend():
    return _keychain._get_backend(service=_SERVICE, account="default")


def load_real_credentials() -> Credentials | None:
    status, raw = _keychain._call(_get_backend(), "load")
    status = _keychain._checked_status(status)
    if status == _keychain._NOT_FOUND:
        return None
    if status != _keychain._SUCCESS:
        raise _keychain._status_error(status)
    try:
        if not isinstance(raw, bytes) or not 0 < len(raw) <= _keychain._MAX_PAYLOAD_BYTES:
            raise ValueError
        value = json.loads(raw.decode("utf-8"))
        if (not isinstance(value, dict) or set(value) != _FIELDS
                or type(value["version"]) is not int or value["version"] != 1):
            raise ValueError
        return Credentials(value["app_key"], value["app_secret"])
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise KeychainError("실전투자 키체인 설정 형식이 올바르지 않습니다.") from None


def save_real_credentials(credentials: Credentials) -> None:
    if not isinstance(credentials, Credentials):
        raise ValueError("유효한 실전투자 자격증명이 필요합니다.")
    credentials.__post_init__()
    raw = json.dumps({"version": 1, "app_key": credentials.app_key,
                      "app_secret": credentials.app_secret},
                     ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    if len(raw) > _keychain._MAX_PAYLOAD_BYTES:
        raise ValueError("실전투자 자격증명의 크기가 저장 한도를 초과했습니다.")
    _keychain._write_payload(_get_backend(), raw)


def delete_real_credentials() -> bool:
    status = _keychain._checked_status(_keychain._call(_get_backend(), "delete"))
    if status == _keychain._NOT_FOUND:
        return False
    if status != _keychain._SUCCESS:
        raise _keychain._status_error(status)
    return True
