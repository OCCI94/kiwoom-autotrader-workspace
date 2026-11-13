"""Small Telegram Bot API client for private notifications; standard library only.

Official contract: https://core.telegram.org/bots/api (verified 2026-10-26).
Credentials live in memory. Requests use a fixed HTTPS origin, reject redirects,
and never retry, since a timed-out send may already have delivered its message.
Remote descriptions, response bodies, and token-bearing URLs are never errors.
"""

from __future__ import annotations

import http.client
import json
import re
import urllib.error
import urllib.request
from kiwoom_autotrader.utils.http_queue import queued_open
from typing import Any


_ORIGIN = "https://api.telegram.org"
_METHODS = frozenset({"getMe", "getWebhookInfo", "getUpdates", "sendMessage"})
_TOKEN_PATTERN = re.compile(r"[1-9][0-9]{0,19}:[A-Za-z0-9_-]{20,200}")
_USERNAME_PATTERN = re.compile(r"[A-Za-z0-9_]{5,32}")
_MAX_RESPONSE_BYTES = 1024 * 1024
_TIMEOUT_SECONDS = 15
_MAX_CHAT_ID = (1 << 52) - 1


class TelegramAPIError(RuntimeError):
    """A sanitized transport, protocol, or Telegram API error."""


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _valid_id(value: Any) -> bool:
    return type(value) is int and 0 < value <= _MAX_CHAT_ID


class TelegramClient:
    """Authenticate a bot, inspect incoming updates, and send a private message.

    Construction performs no network requests. Callers must not log raw update
    or webhook results, since they can contain personal data or webhook secrets.
    This client never changes webhooks. Queued updates are acknowledged only
    when the caller explicitly supplies an offset to get_updates().
    """

    def __init__(self, token: str):
        if not isinstance(token, str) or _TOKEN_PATTERN.fullmatch(token) is None:
            raise ValueError("Telegram 봇 토큰 형식이 올바르지 않습니다.")
        self._token = token
        self._opener = urllib.request.build_opener(_RejectRedirects())

    def __repr__(self) -> str:
        return "TelegramClient(token=<redacted>)"

    def get_me(self) -> dict[str, Any]:
        result = self._post("getMe", {})
        if (
            not isinstance(result, dict)
            or not _valid_id(result.get("id"))
            or result["id"] != int(self._token.split(":", 1)[0])
            or result.get("is_bot") is not True
            or not isinstance(result.get("username"), str)
            or _USERNAME_PATTERN.fullmatch(result["username"]) is None
        ):
            raise TelegramAPIError("Telegram 봇 정보 응답 형식이 올바르지 않습니다.")
        return result

    def get_webhook_info(self) -> dict[str, Any]:
        result = self._post("getWebhookInfo", {})
        if (
            not isinstance(result, dict)
            or not isinstance(result.get("url"), str)
            or type(result.get("has_custom_certificate")) is not bool
            or type(result.get("pending_update_count")) is not int
            or result["pending_update_count"] < 0
        ):
            raise TelegramAPIError("Telegram 웹훅 정보 응답 형식이 올바르지 않습니다.")
        return result

    def get_updates(
        self, timeout: int = 0, offset: int | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch up to 100 updates, acknowledging earlier ones only with offset.

        Server polling is limited to 25 seconds so the socket timeout, which
        includes five seconds of headroom, never exceeds 30 seconds. Omitting
        offset preserves queued updates for pairing inspection. An explicit
        nonnegative offset confirms updates with lower update_id values, for
        use by the application's receive loop after handling them. Negative
        offsets, which can discard queued updates, are rejected. allowed_updates
        is never sent, preserving the update subscription. Do not print results.
        """
        if type(timeout) is not int or not 0 <= timeout <= 25:
            raise ValueError("Telegram 업데이트 대기 시간은 0~25초 정수여야 합니다.")
        if offset is not None and (type(offset) is not int or offset < 0):
            raise ValueError("Telegram 업데이트 위치는 0 이상 정수여야 합니다.")
        body = {"limit": 100, "timeout": timeout}
        if offset is not None:
            body["offset"] = offset
        result = self._post(
            "getUpdates", body,
            timeout=max(_TIMEOUT_SECONDS, timeout + 5),
        )
        if (
            not isinstance(result, list)
            or len(result) > 100
            or any(
                not isinstance(update, dict)
                or type(update.get("update_id")) is not int
                or update["update_id"] < 0
                for update in result
            )
        ):
            raise TelegramAPIError("Telegram 업데이트 응답 형식이 올바르지 않습니다.")
        return result

    def send_message(self, chat_id: int, text: str) -> dict[str, Any]:
        """Send one plain-text private notification, without automatic retries."""
        if not _valid_id(chat_id):
            raise ValueError("Telegram 개인 대화 ID는 양의 정수여야 합니다.")
        if not isinstance(text, str) or not 1 <= len(text) <= 4096:
            raise ValueError("Telegram 메시지는 1~4096자 문자열이어야 합니다.")
        result = self._post(
            "sendMessage",
            {"chat_id": chat_id, "text": text,
             "link_preview_options": {"is_disabled": True}},
        )
        if (
            not isinstance(result, dict)
            or type(result.get("message_id")) is not int
            or result["message_id"] < 0
            or not isinstance(result.get("chat"), dict)
            or not _valid_id(result["chat"].get("id"))
            or result["chat"]["id"] != chat_id
            or result["chat"].get("type") != "private"
        ):
            raise TelegramAPIError("Telegram 메시지 전송 응답 형식이 올바르지 않습니다.")
        return result

    def _post(
        self, method: str, body: dict[str, Any], *, timeout: int = _TIMEOUT_SECONDS,
    ) -> Any:
        if method not in _METHODS:
            raise TelegramAPIError("지원하지 않는 Telegram 요청입니다.")
        if type(timeout) is not int or not 1 <= timeout <= 30:
            raise TelegramAPIError("Telegram 요청 대기 시간이 올바르지 않습니다.")
        request = urllib.request.Request(
            f"{_ORIGIN}/bot{self._token}/{method}",
            data=json.dumps(body, ensure_ascii=True).encode("utf-8"),
            headers={"Content-Type": "application/json;charset=UTF-8"},
            method="POST",
        )
        try:
            with queued_open(self._opener, request, timeout=timeout, max_bytes=_MAX_RESPONSE_BYTES) as response:
                raw = response.read(_MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as error:
            # Do not read the body, expose the URL/reason, or chain the error.
            code = error.code
            try:
                error.close()
            except OSError:
                pass
            if type(code) is int and 100 <= code <= 599:
                raise TelegramAPIError(f"Telegram HTTP 오류 (code={code}).") from None
            raise TelegramAPIError("Telegram HTTP 요청에 실패했습니다.") from None
        except (OSError, urllib.error.URLError, http.client.HTTPException, ValueError):
            raise TelegramAPIError(
                "Telegram 연결에 실패했습니다. 전송 여부가 불확실할 수 있으므로 자동 재시도하지 않습니다."
            ) from None
        if len(raw) > _MAX_RESPONSE_BYTES:
            raise TelegramAPIError("Telegram 응답 크기가 1MiB 제한을 초과했습니다.")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeError, RecursionError):
            raise TelegramAPIError("Telegram JSON 응답 형식이 올바르지 않습니다.") from None
        if not isinstance(payload, dict) or type(payload.get("ok")) is not bool:
            raise TelegramAPIError("Telegram 응답 형식이 올바르지 않습니다.")
        if not payload["ok"]:
            code = payload.get("error_code")
            if type(code) is int and 100 <= code <= 599:
                raise TelegramAPIError(f"Telegram API 오류 (code={code}).")
            raise TelegramAPIError("Telegram API 요청에 실패했습니다.")
        if "result" not in payload or "error_code" in payload:
            raise TelegramAPIError("Telegram 성공 응답 형식이 올바르지 않습니다.")
        return payload["result"]
