"""Private Telegram command receiver with durable at-most-once handling.

Only the previously paired human/private chat can request commands. Ordinary
updates are checkpointed before a command or reply; a crash or an
ambiguous send can therefore lose a reply but cannot automatically duplicate it.
An authorized shutdown still exits when its checkpoint cannot be written.
State and logs contain counters and allowlisted command names, never messages,
tokens, credential values, or plaintext Telegram identifiers.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import time
import math
from typing import Any
from .commands import channels as channels_command, status as status_command, top as top_command
from .commands.help import HELP as _HELP, UNKNOWN as _UNKNOWN
from .commands.login import handle_login
from .commands.power import stop_channels
from .commands.search import handle_search
from .commands.orders import handle_buy, handle_sell
from .commands.report import handle_report
from .commands.settings import handle_market_hours, handle_settings
from .commands.reservations import handle_reservation
from kiwoom_autotrader.utils.command_scheduler import CommandScheduler, SchedulerError

from kiwoom_autotrader.utils.keychain import KeychainError, load_mock_credentials
from kiwoom_autotrader.api.equity_rankings import get_top_equities
from kiwoom_autotrader.api.client import KiwoomAPIError, KiwoomMockClient
from kiwoom_autotrader.telegram.client import TelegramAPIError, TelegramClient
from kiwoom_autotrader.telegram.bot_credentials import TelegramConfig, load_telegram_config
from kiwoom_autotrader.api.session import BrokerSession, LoginSessionError, parse_login_command, terminal_report
from kiwoom_autotrader.config.settings import load_settings, save_renewal_time


_STATE_LIMIT = 16 * 1024
_COMMANDS = frozenset({"help", "start", "status", "top", "channels", "posts", "unknown", "srch",
                       "buy", "sell", "report", "mkhr", "stts", "rsv",
                       "login_paper", "login_real", "login_renewal", "login_invalid", "power_off"})
_SLASH_COMMANDS = frozenset({"help", "start", "status", "top", "channels", "posts"})
_COMMAND_ALIASES = {"상태": "status", "상위20": "top", "거래대금": "top", "도움말": "help",
                    "채널": "channels", "채널글": "posts"}
_POLL_RETRY_DELAYS = (1, 2, 4)
_QUERY_ERROR = "키움 모의투자 조회에 실패했습니다. 저장된 모의투자 키와 연결 상태를 확인하세요."
_STORAGE_ERROR = "텔레그램 수신 상태를 안전하게 저장하거나 읽지 못했습니다."
_STARTUP_MESSAGE = "자동매매 프로그램을 시작했습니다. 텔레그램 명령 수신을 준비하고 모의투자에 자동 로그인합니다."


class TelegramListenerError(RuntimeError):
    """A safe user-facing receiver error, without external exception details."""


def _identity(config: TelegramConfig) -> str:
    return hashlib.sha256(f"{config.bot_id}:{config.chat_id}".encode("ascii")).hexdigest()


def _new_state(config: TelegramConfig, started_at: int) -> dict[str, Any]:
    return {
        "version": 1, "config_identity": _identity(config), "next_offset": None,
        "poll_count": 0, "received_count": 0, "replied_count": 0,
        "last_command": None, "last_reply_at": None,
        "started_at": started_at, "pid": os.getpid(), "status": "running",
    }


def _nonnegative_int(value: Any) -> bool:
    return type(value) is int and value >= 0


def _load_state(path: Path, config: TelegramConfig, started_at: int) -> dict[str, Any]:
    fresh = _new_state(config, started_at)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return fresh
    except OSError:
        raise TelegramListenerError(_STORAGE_ERROR) from None
    try:
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                raise ValueError
            raw = stream.read(_STATE_LIMIT + 1)
        if len(raw) > _STATE_LIMIT:
            raise ValueError
        saved = json.loads(raw.decode("utf-8"))
        if not isinstance(saved, dict):
            raise ValueError
        if saved.get("config_identity") != fresh["config_identity"]:
            return fresh
        if (
            set(saved) != set(fresh)
            or type(saved.get("version")) is not int or saved["version"] != 1
            or (saved["next_offset"] is not None and not _nonnegative_int(saved["next_offset"]))
            or any(not _nonnegative_int(saved[field]) for field in (
                "poll_count", "received_count", "replied_count", "started_at",
            ))
            or saved["last_command"] not in _COMMANDS | {None}
            or (saved["last_reply_at"] is not None and not _nonnegative_int(saved["last_reply_at"]))
            or type(saved["pid"]) is not int or saved["pid"] <= 0
            or saved["status"] not in {"running", "stopped", "failed"}
        ):
            raise ValueError
        saved.update(started_at=started_at, pid=os.getpid(), status="running")
        return saved
    except (OSError, ValueError, TypeError, KeyError, UnicodeError, RecursionError):
        raise TelegramListenerError(_STORAGE_ERROR) from None


def _save_state(path: Path, state: dict[str, Any]) -> None:
    """Replace one private JSON checkpoint, flushing it before any side effect."""
    temporary: str | None = None
    try:
        fd, temporary = tempfile.mkstemp(prefix=".state-", suffix=".tmp", dir=path.parent)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(state, stream, ensure_ascii=True, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except (OSError, ValueError, TypeError):
        raise TelegramListenerError(_STORAGE_ERROR) from None
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass


def _open_lock(path: Path) -> int:
    try:
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    except OSError:
        raise TelegramListenerError("텔레그램 수신 잠금 파일을 열지 못했습니다.") from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise TelegramListenerError("텔레그램 수신 잠금 파일이 올바르지 않습니다.")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.fchmod(fd, 0o600)
        return fd
    except BlockingIOError:
        os.close(fd)
        raise TelegramListenerError("텔레그램 수신 프로그램이 이미 실행 중입니다.") from None
    except Exception:
        os.close(fd)
        raise TelegramListenerError("텔레그램 수신 잠금을 얻지 못했습니다.") from None


def _command(update: dict[str, Any], config: TelegramConfig, started_at: int) -> str | None:
    message = update.get("message")
    if not isinstance(message, dict):
        return None
    chat, sender, date = message.get("chat"), message.get("from"), message.get("date")
    if (
        not isinstance(chat, dict) or not isinstance(sender, dict)
        or chat.get("type") != "private"
        or type(chat.get("id")) is not int or chat["id"] != config.chat_id
        or type(sender.get("id")) is not int or sender["id"] != config.chat_id
        or sender.get("is_bot") is not False
        or type(date) is not int or date < started_at
        or not isinstance(message.get("text"), str)
    ):
        return None
    return _text_command(message["text"], config)


def _text_command(text: str, config: TelegramConfig) -> str | None:
    """Classify text after sender checks, or after a durable scheduled claim."""
    text = text.strip().casefold()
    if " ".join(text.split()) == "power off":
        return "power_off"
    try:
        login = parse_login_command(text)
    except ValueError:
        return "login_invalid"
    if login:
        return "login_renewal" if login.action == "renewal" else "login_" + login.value
    if text in {"help", "report", "r", "status", "top", "channels", "posts"}:
        return "report" if text == "r" else text
    prefix = text.split(maxsplit=1)[:1]
    if prefix and prefix[0] in {"srch", "buy", "sell", "mkhr", "stts", "rsv"}:
        return prefix[0]
    if text in _COMMAND_ALIASES:
        return _COMMAND_ALIASES[text]
    words = text.split(maxsplit=1)
    if not words or not words[0].startswith("/"):
        return "unknown"
    command, separator, username = words[0][1:].partition("@")
    if separator and username.lower() != config.bot_username.lower():
        return None
    command = command.lower()
    return command if command in _SLASH_COMMANDS else "unknown"


def _status_reply(client):
    return status_command.build_reply(client)


def _top_reply(client):
    return top_command.build_reply(client, ranking_loader=get_top_equities)


def _channel_reply(command, config):
    return channels_command.build_reply(command, config)


def _reply(command: str, config: TelegramConfig) -> str:
    if command in {"help", "start"}:
        return _HELP
    if command == "unknown":
        return _UNKNOWN
    if command in {"channels", "posts"}:
        return _channel_reply(command, config)
    try:
        credentials = load_mock_credentials()
        if credentials is None:
            return "키움 모의투자 키가 저장되지 않았습니다. Mac에서 모의투자 연결 설정을 먼저 실행하세요."
        client = KiwoomMockClient(credentials.app_key, credentials.app_secret)
        client.authenticate()
        text = _status_reply(client) if command == "status" else _top_reply(client)
        # Never forward credential-like values even if a remote field echoes one.
        if any(secret and secret in text for secret in (
            config.bot_token, credentials.app_key, credentials.app_secret,
        )):
            return _QUERY_ERROR
        return text
    except Exception:
        # External descriptions and native Keychain details must never be replies.
        return _QUERY_ERROR


def _finish(path: Path, state: dict[str, Any], status: str) -> None:
    state["status"] = status
    _save_state(path, state)


def _send_notification(client: TelegramClient, config: TelegramConfig, path: Path,
                       state: dict[str, Any], text: str, failure_message: str) -> None:
    try:
        client.send_message(config.chat_id, text)
    except TelegramAPIError:
        print(failure_message, flush=True)
    else:
        state["replied_count"] += 1
        state["last_reply_at"] = int(time.time())
        _save_state(path, state)


def _dispatch(command: str, text: str, config: TelegramConfig,
              session: BrokerSession, scheduler: CommandScheduler) -> list[str]:
    if command.startswith("login_"):
        return [handle_login(command, text, session, reporter=terminal_report)]
    if command == "srch":
        return handle_search(text, session)
    if command == "buy":
        return handle_buy(text, session)
    if command == "sell":
        return handle_sell(text, session)
    if command == "report":
        return handle_report(session)
    if command == "mkhr":
        return handle_market_hours(text)
    if command == "stts":
        return handle_settings(text, session)
    if command == "rsv":
        return handle_reservation(text, scheduler)
    return [_reply(command, config)]


def _send_replies(client, config, path, stop_path, state, replies):
    for text in replies:
        if stop_path.exists():
            break
        try:
            client.send_message(config.chat_id, text)
        except TelegramAPIError:
            print("텔레그램 응답 전송 결과를 확인하지 못했습니다. 자동 재전송하지 않습니다.", flush=True)
            break
        else:
            state["replied_count"] += 1
            state["last_reply_at"] = int(time.time())
            _save_state(path, state)
            print("텔레그램 응답 1건 전송 성공", flush=True)


def _power_off(client: TelegramClient, config: TelegramConfig, path: Path,
               stop_path: Path, state: dict[str, Any], broker_session: BrokerSession) -> int:
    """Honor an authorized shutdown even when acknowledgements cannot be saved.

    A requested shutdown must exit successfully so launchd does not restart it.
    Checkpoints and notifications are best effort here; ordinary commands keep
    their stricter checkpoint-before-side-effect behavior.
    """
    def report(text: str) -> None:
        try:
            print(text, flush=True)
        except OSError:
            pass

    def checkpoint() -> None:
        try:
            _save_state(path, state)
        except Exception:
            report("종료 상태를 파일에 기록하지 못했지만 요청에 따라 프로그램을 종료합니다.")

    def notify(text: str) -> None:
        try:
            client.send_message(config.chat_id, text)
        except Exception:
            report("종료 응답 전송을 확인하지 못했지만 요청에 따라 프로그램을 종료합니다.")
        else:
            state["replied_count"] += 1
            state["last_reply_at"] = int(time.time())
            checkpoint()

    checkpoint()
    report("텔레그램 명령 수신: power_off")
    try:
        notify("프로그램을 종료합니다. 텔레그램 명령 수신을 중지하고 채널 수집 종료를 요청합니다.")
        try:
            collector_stopped = stop_channels()
        except Exception:
            collector_stopped = False
        if not collector_stopped:
            failure = "텔레그램 명령 수신은 종료합니다. 채널 수집 종료를 확인하지 못했습니다. Mac에서 수집기 상태를 확인하세요."
            report(failure)
            notify(failure)
    finally:
        broker_session.clear()
        try:
            stop_path.touch(mode=0o600)
        except OSError:
            report("종료 요청 파일을 기록하지 못했지만 프로그램은 종료합니다.")
        state["status"] = "stopped"
        checkpoint()
    report("텔레그램 수신을 중지했습니다.")
    return 0


def _receive(
    state_dir: Path, state: dict[str, Any], config: TelegramConfig,
    max_polls: int | None, broker_session: BrokerSession, scheduler: CommandScheduler,
) -> int:
    path, stop_path = state_dir / "state.json", state_dir / "stop.request"
    stop_path.unlink(missing_ok=True)
    _save_state(path, state)
    client = TelegramClient(config.bot_token)
    try:
        bot = client.get_me()
        if (
            bot.get("id") != config.bot_id
            or not isinstance(bot.get("username"), str)
            or bot["username"].lower() != config.bot_username.lower()
        ):
            raise TelegramListenerError("저장된 텔레그램 봇 정보를 다시 확인해야 합니다.")
        webhook = client.get_webhook_info()
        if not isinstance(webhook.get("url"), str) or webhook["url"]:
            raise TelegramListenerError("기존 텔레그램 웹훅이 있어 수신을 시작하지 않았습니다.")
    except TelegramAPIError:
        raise TelegramListenerError("텔레그램 봇과 수신 설정을 확인하지 못했습니다.") from None
    print("텔레그램 개인 채팅 수신을 시작했습니다. /help로 로그인·조회·종료 명령을 확인하세요.", flush=True)
    _send_notification(client, config, path, state, _STARTUP_MESSAGE,
                       "프로그램 시작 알림의 텔레그램 전송을 확인하지 못했습니다.")
    startup_login = broker_session.login("paper")
    terminal_report(startup_login, broker_session)
    _send_notification(client, config, path, state, startup_login.message,
                       "모의투자 자동 로그인 결과의 텔레그램 전송을 확인하지 못했습니다.")
    attempts, failures, scheduler_error_reported = 0, 0, False
    while max_polls is None or attempts < max_polls:
        if stop_path.exists():
            break
        renewal = broker_session.renew_if_due()
        if renewal is not None:
            terminal_report(renewal, broker_session)
            _send_notification(client, config, path, state, renewal.message,
                               "토큰 갱신 결과의 텔레그램 전송을 확인하지 못했습니다.")
        try:
            for _ in range(100):
                if stop_path.exists():
                    break
                reserved = scheduler.claim_due()
                if reserved is None:
                    break
                command = _text_command(reserved["command"], config) or "unknown"
                notice = f"⏰ 예약 #{reserved['id']} 실행 ({reserved['time']}, 한국 시간)\n{reserved['command']}"
                _send_notification(client, config, path, state, notice,
                                   "예약 실행 알림의 텔레그램 전송을 확인하지 못했습니다.")
                print(f"예약 명령 실행: id={reserved['id']} command={command}", flush=True)
                if command == "power_off":
                    return _power_off(client, config, path, stop_path, state, broker_session)
                replies = _dispatch(command, reserved["command"], config, broker_session, scheduler)
                _send_replies(client, config, path, stop_path, state, replies)
            schedule_in = scheduler.seconds_until_next()
            scheduler_error_reported = False
        except SchedulerError:
            schedule_in = None
            if not scheduler_error_reported:
                _send_notification(client, config, path, state,
                                   "예약 설정·실행 기록을 확인하지 못했습니다. 예약 실행을 보류합니다. Mac에서 설정 파일을 확인하세요.",
                                   "예약 오류 알림 전송을 확인하지 못했습니다.")
                scheduler_error_reported = True
        if stop_path.exists():
            break
        attempts += 1
        state["poll_count"] += 1
        _save_state(path, state)
        try:
            due_in = broker_session.seconds_until_renewal()
            deadlines = [25, *[math.ceil(value) for value in (due_in, schedule_in) if value is not None]]
            timeout = max(1, min(deadlines))
            updates = client.get_updates(timeout=timeout, offset=state["next_offset"])
        except TelegramAPIError as error:
            if re.search(r"code=(401|404|409)\b", str(error)):
                raise TelegramListenerError("텔레그램 수신 인증 또는 다른 수신 프로그램과의 충돌을 확인하세요.") from None
            if failures >= len(_POLL_RETRY_DELAYS):
                raise TelegramListenerError("텔레그램 수신 연결이 반복해서 실패해 중지했습니다.") from None
            delay = _POLL_RETRY_DELAYS[failures]
            failures += 1
            print("텔레그램 수신 연결을 확인한 뒤 다시 대기합니다.", flush=True)
            time.sleep(delay)
            continue
        failures = 0
        if stop_path.exists():
            break
        if not isinstance(updates, list) or any(
            not isinstance(update, dict) or not _nonnegative_int(update.get("update_id"))
            for update in updates
        ):
            raise TelegramListenerError("텔레그램 수신 응답 형식이 올바르지 않습니다.")
        for update in sorted(updates, key=lambda item: item["update_id"]):
            if stop_path.exists():
                break
            update_id = update["update_id"]
            if state["next_offset"] is not None and update_id < state["next_offset"]:
                continue
            command = _command(update, config, state["started_at"])
            state["next_offset"] = update_id + 1
            if command is not None:
                state["received_count"] += 1
                state["last_command"] = command
            if command == "power_off":
                return _power_off(client, config, path, stop_path, state, broker_session)
            # Commit before querying or sending. Restart never replays this update.
            _save_state(path, state)
            if command is None:
                continue
            print(f"텔레그램 명령 수신: {command}", flush=True)
            replies = _dispatch(command, update["message"]["text"], config, broker_session, scheduler)
            _send_replies(client, config, path, stop_path, state, replies)
    _finish(path, state, "stopped")
    print("텔레그램 수신을 중지했습니다.", flush=True)
    return 0


def run_listener(state_dir: Path, *, max_polls: int | None = None, broker_session: BrokerSession | None = None,
                 scheduler: CommandScheduler | None = None) -> int:
    """Receive paired private commands until stop.request or a bounded test run.

    ``max_polls`` bounds HTTP polling attempts for tests; None runs continuously.
    One process holds listener.lock for its entire lifetime. State is written
    atomically as mode 0600 to state.json; no message text is persisted.
    """
    if max_polls is not None and (type(max_polls) is not int or max_polls < 0):
        raise TelegramListenerError("수신 대기 횟수는 0 이상 정수여야 합니다.")
    started_at = int(time.time())
    try:
        state_dir = Path(state_dir)
        state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    except (OSError, TypeError, ValueError):
        raise TelegramListenerError(_STORAGE_ERROR) from None
    lock_fd = _open_lock(state_dir / "listener.lock")
    state: dict[str, Any] | None = None
    try:
        broker_session = broker_session or BrokerSession(
            renewal_time=load_settings()["login_renewal_time"], save_renewal=save_renewal_time,
        )
        scheduler = scheduler or CommandScheduler()
        try:
            config = load_telegram_config()
        except KeychainError:
            raise TelegramListenerError("저장된 텔레그램 연결 설정을 읽지 못했습니다.") from None
        if config is None:
            raise TelegramListenerError("저장된 텔레그램 개인 채팅 설정이 없습니다. 먼저 연결 설정을 실행하세요.")
        if not isinstance(config, TelegramConfig):
            raise TelegramListenerError("저장된 텔레그램 개인 채팅 설정이 올바르지 않습니다.")
        config.__post_init__()
        state = _load_state(state_dir / "state.json", config, started_at)
        return _receive(state_dir, state, config, max_polls, broker_session, scheduler)
    except KeyboardInterrupt:
        if state is not None:
            _finish(state_dir / "state.json", state, "stopped")
        print("텔레그램 수신을 중지했습니다.", flush=True)
        return 0
    except Exception as error:
        if state is not None:
            try:
                _finish(state_dir / "state.json", state, "failed")
            except TelegramListenerError:
                pass
        if isinstance(error, TelegramListenerError):
            raise
        raise TelegramListenerError("텔레그램 수신을 안전하게 계속할 수 없어 중지했습니다.") from None
    finally:
        if broker_session is not None:
            broker_session.clear()
        os.close(lock_fd)
