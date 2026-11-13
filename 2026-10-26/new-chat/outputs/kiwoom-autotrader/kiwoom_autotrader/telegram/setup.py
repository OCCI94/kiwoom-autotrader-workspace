"""Bind a bot to the user's private chat using a one-time start challenge."""
from collections.abc import Callable
import re
import secrets
import time

from kiwoom_autotrader.utils.keychain import KeychainError
from kiwoom_autotrader.telegram.client import TelegramAPIError, TelegramClient
from kiwoom_autotrader.telegram.bot_credentials import TelegramConfig, save_telegram_config


class TelegramSetupError(RuntimeError):
    """A safe user-facing setup error without bot tokens or message contents."""


def pair_private_chat(updates: list[dict], nonce: str, started_at: int) -> int | None:
    if not isinstance(nonce, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", nonce):
        raise TelegramSetupError("연결 코드 형식이 올바르지 않습니다.")
    if type(started_at) is not int or started_at < 0:
        raise TelegramSetupError("연결 시작 시각이 올바르지 않습니다.")
    if not isinstance(updates, list):
        raise TelegramSetupError("텔레그램 수신 형식이 올바르지 않습니다.")
    candidates = set()
    for update in updates:
        if not isinstance(update, dict):
            continue
        message = update.get("message")
        if not isinstance(message, dict) or message.get("text") != f"/start {nonce}":
            continue
        date, chat, sender = message.get("date"), message.get("chat"), message.get("from")
        if type(date) is not int or date < started_at:
            continue
        if not isinstance(chat, dict) or not isinstance(sender, dict):
            continue
        chat_id, sender_id = chat.get("id"), sender.get("id")
        if (chat.get("type") != "private" or type(chat_id) is not int or chat_id <= 0
                or type(sender_id) is not int or sender_id != chat_id
                or sender.get("is_bot") is not False):
            continue
        candidates.add(chat_id)
    if len(candidates) > 1:
        raise TelegramSetupError("연결 요청자가 여러 명입니다. 새 연결 코드로 다시 시작하세요.")
    return next(iter(candidates)) if candidates else None


def configure_telegram(
    token: str, emit: Callable[[str], None] = print, timeout_seconds: int = 180,
) -> TelegramConfig:
    if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 600:
        raise TelegramSetupError("연결 대기 시간은 1~600초여야 합니다.")
    try:
        client = TelegramClient(token)
        bot = client.get_me()
        webhook = client.get_webhook_info()
        if not isinstance(webhook.get("url"), str):
            raise TelegramSetupError("봇의 수신 설정을 확인하지 못했습니다.")
        if webhook["url"]:
            raise TelegramSetupError("이 봇은 기존 웹훅에 연결돼 있습니다. 기존 수신 설정은 변경하지 않았습니다.")
        nonce = secrets.token_urlsafe(16)
        started_at = int(time.time())
        deadline = time.monotonic() + timeout_seconds
        emit(f"봇 확인: @{bot['username']}")
        emit(f"https://t.me/{bot['username']}?start={nonce}")
        emit("위 링크를 열고 본인 텔레그램에서 시작(Start)을 누르세요.")
        while True:
            now = time.monotonic()
            if now >= deadline:
                break
            remaining = max(0, int(deadline - now))
            updates = client.get_updates(timeout=min(10, remaining))
            chat_id = pair_private_chat(updates, nonce, started_at)
            if chat_id is not None:
                config = TelegramConfig(
                    bot_token=token, chat_id=chat_id,
                    bot_id=bot['id'], bot_username=bot['username'],
                )
                save_telegram_config(config)
                emit("개인 채팅 확인 및 키체인 저장 완료")
                try:
                    client.send_message(chat_id, (
                        "키움 자동매매 프로그램의 텔레그램 연결이 완료됐습니다.\n"
                        "개인 채팅 테스트 메시지입니다.\n"
                        "수신 프로그램 실행 후 /help, /status, /top을 사용할 수 있습니다."
                    ))
                except TelegramAPIError:
                    raise TelegramSetupError("연결 정보는 저장됐지만 테스트 메시지 전송 결과를 확인하지 못했습니다. 자동 재전송하지 않았습니다.") from None
                emit("테스트 메시지 1건 전송 성공")
                return config
            if len(updates) >= 100:
                raise TelegramSetupError("기존 대기 메시지가 많아 새 연결 요청을 읽지 못했습니다. 전용 봇의 수신 상태를 확인하세요.")
            time.sleep(1)
        raise TelegramSetupError("연결 시간이 지났습니다. 연결 설정을 다시 실행한 뒤 새 링크의 시작 버튼을 누르세요.")
    except TelegramSetupError:
        raise
    except TelegramAPIError:
        raise TelegramSetupError("텔레그램 봇 확인 또는 수신에 실패했습니다. 토큰·네트워크·다른 수신 프로그램을 확인하세요.") from None
    except KeychainError:
        raise TelegramSetupError("텔레그램 연결 정보를 키체인에 저장하지 못했습니다.") from None
    except (ValueError, OSError, KeyError, TypeError):
        raise TelegramSetupError("텔레그램 연결 설정을 확인하지 못했습니다.") from None
