"""Private Telegram command receiver with durable at-most-once handling.

Only the previously paired human/private chat can request commands. Ordinary
updates are checkpointed before a command or reply; a crash or an
ambiguous send can therefore lose a reply but cannot automatically duplicate it.
An authorized shutdown still exits when its checkpoint cannot be written.
State and logs contain counters and allowlisted command names, never messages,
tokens, credential values, or plaintext Telegram identifiers.
"""

from __future__ import annotations

from kiwoom_autotrader.utils.platform_io import fcntl
import hashlib
import json
from kiwoom_autotrader.utils.platform_io import os, sync_directory
from pathlib import Path
import re
import stat
import tempfile
import time
import threading
import math
from datetime import datetime
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
from .commands.crosses import handle_cross_command
from .commands.rank_batch import handle_rank_batch
from .commands.ip import handle_ip
from kiwoom_autotrader.trading.cross_monitor import CrossMonitor
from kiwoom_autotrader.trading.golden_cross import GoldenCrossMonitor
from kiwoom_autotrader.trading.multi_strategy import MultiStrategyMonitor, execute_paper_strategy
from kiwoom_autotrader.trading.execution_log import ExecutionTracker
from kiwoom_autotrader.trading.paper_reconciliation import PaperReconciliationGate
from kiwoom_autotrader.trading.paper_experiment_monitor import PaperExperimentMonitor
from kiwoom_autotrader.trading.paper_experiment_recovery import recover_paper_experiment
from kiwoom_autotrader.trading.legacy_resume_gate import LegacyResumeGate,legacy_loop_description
from kiwoom_autotrader.trading.paper_prior_day_recovery import PriorDayPendingScheduler
from kiwoom_autotrader.trading.stability_audit import ReceiverStabilityAudit
from kiwoom_autotrader.utils.control_audit import initialize_control_audit,control_origin
from kiwoom_autotrader.trading.unfilled_buy_ttl import UnfilledBuyTTLMonitor
from kiwoom_autotrader.trading.maintenance_reconciliation import (MaintenanceReadOnlyReconciliation,
    cutover_guard,remember_cutover_binding,MaintenanceReadUnavailable)
from kiwoom_autotrader.trading.maintenance_expiry_resolution import MaintenanceExpiryResolution
from kiwoom_autotrader.research.shared_server import PaperResearchServer
from kiwoom_autotrader.trading.order_context import automated_orders
from kiwoom_autotrader.realtime import SharedWebSocketEngine
from kiwoom_autotrader.realtime.execution_stream import ExecutionStreamRecorder
from kiwoom_autotrader.utils.command_scheduler import CommandScheduler, SchedulerError
from kiwoom_autotrader.utils.kst_logging import kst_listener_logs
from .receiver_health import maintenance_status, ErrorReminder, PreopenStreamCheck

from kiwoom_autotrader.utils.keychain import KeychainError, load_mock_credentials
from kiwoom_autotrader.api.equity_rankings import get_top_equities
from kiwoom_autotrader.api.client import KiwoomAPIError, KiwoomMockClient
from kiwoom_autotrader.telegram.client import TelegramAPIError, TelegramClient
from kiwoom_autotrader.telegram.bot_credentials import TelegramConfig, load_telegram_config
from kiwoom_autotrader.api.session import BrokerSession, LoginSessionError, parse_login_command, terminal_report, KST
from kiwoom_autotrader.config.settings import load_settings, save_renewal_time, effective_paper_experiment


# Reader and writer share one bounded checkpoint contract. Multi-strategy
# account diagnostics can legitimately exceed the former16KiB read-only cap.
_STATE_LIMIT = 256 * 1024
_COMMANDS = frozenset({"help", "start", "status", "top", "channels", "posts", "unknown", "srch",
                       "buy", "sell", "report", "mkhr", "stts", "rsv", "gdcrs", "cross_control", "rank", "ip",
                       "login_paper", "login_real", "login_renewal", "login_invalid", "power_off", "multi_resume"})
_SLASH_COMMANDS = frozenset({"help", "start", "status", "top", "channels", "posts"})
_COMMAND_ALIASES = {"상태": "status", "상위20": "top", "거래대금": "top", "도움말": "help",
                    "채널": "channels", "채널글": "posts"}
_POLL_RETRY_DELAYS = (1, 2, 4)
_QUERY_ERROR = "키움 모의투자 조회에 실패했습니다. 저장된 모의투자 키와 연결 상태를 확인하세요."
_STORAGE_ERROR = "텔레그램 수신 상태를 안전하게 저장하거나 읽지 못했습니다."
_STARTUP_MESSAGE = "자동매매 프로그램을 시작했습니다. 텔레그램 명령 수신을 준비하고 모의투자에 자동 로그인합니다."


class TelegramListenerError(RuntimeError):
    """A safe user-facing receiver error, without external exception details."""

    def __init__(self, message: str, *, diagnostic_code: str | None = None):
        super().__init__(message)
        self.diagnostic_code = diagnostic_code


# Only exact, locally authored messages are classified. Never print an
# arbitrary exception's text: provider errors can contain tokens or URLs.
_LISTENER_ERROR_CODES = {
    _STORAGE_ERROR: "state_storage_failure",
    "텔레그램 수신 잠금 파일을 열지 못했습니다.": "lock_open_failure",
    "텔레그램 수신 잠금 파일이 올바르지 않습니다.": "lock_file_invalid",
    "텔레그램 수신 프로그램이 이미 실행 중입니다.": "listener_already_running",
    "텔레그램 수신 잠금을 얻지 못했습니다.": "lock_acquire_failure",
    "저장된 텔레그램 봇 정보를 다시 확인해야 합니다.": "bot_identity_mismatch",
    "기존 텔레그램 웹훅이 있어 수신을 시작하지 않았습니다.": "webhook_conflict",
    "텔레그램 봇과 수신 설정을 확인하지 못했습니다.": "bot_preflight_failed",
    "텔레그램 수신 인증 또는 다른 수신 프로그램과의 충돌을 확인하세요.": "poll_auth_or_conflict",
    "텔레그램 수신 연결이 반복해서 실패해 중지했습니다.": "poll_retry_exhausted",
    "텔레그램 수신 응답 형식이 올바르지 않습니다.": "poll_response_invalid",
    "저장된 텔레그램 연결 설정을 읽지 못했습니다.": "credentials_read_failure",
    "저장된 텔레그램 개인 채팅 설정이 없습니다. 먼저 연결 설정을 실행하세요.": "credentials_missing",
    "저장된 텔레그램 개인 채팅 설정이 올바르지 않습니다.": "credentials_invalid",
    "텔레그램 수신을 안전하게 계속할 수 없어 중지했습니다.": "unexpected_listener_failure",
}


def listener_error_code(error: BaseException) -> str:
    """Return a fixed category, without formatting or exposing exceptions."""
    if type(error) is not TelegramListenerError or len(error.args) != 1 or type(error.args[0]) is not str:
        return "unknown_listener_failure"
    if type(error.diagnostic_code) is str and error.diagnostic_code in {"poll_http_401", "poll_http_404", "poll_http_409"}:
        return error.diagnostic_code
    return _LISTENER_ERROR_CODES.get(error.args[0], "unknown_listener_failure")


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
        # Diagnostic only: never restore capture readiness across a restart.
        # Older checkpoints have no such field; the new recorder recomputes it.
        saved.pop("execution_recording", None)
        saved.pop("automation_status", None)
        saved.pop("maintenance_paused", None)
        saved.pop("maintenance_status", None)
        saved.pop("maintenance_reconciliation", None)
        saved.pop("maintenance_expiry_resolution", None)
        saved.pop("preopen_stream_check", None)
        saved.pop("stability_observation", None)
        saved.pop("stability_audit", None)
        saved.pop("stability_control_audit", None)
        saved.pop("state_publication", None)
        saved.pop("tracker_diagnostics", None)
        saved.pop("telemetry_publish_error", None)
        saved.pop("recorder_snapshot_observed_at", None)
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
        raw = json.dumps(state, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
        if len(raw) > _STATE_LIMIT:
            raise ValueError
        fd, temporary = tempfile.mkstemp(prefix=".state-", suffix=".tmp", dir=path.parent)
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
        sync_directory(path.parent)
    except (OSError, ValueError, TypeError, RecursionError):
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
    if " ".join(text.split()) == "multi resume":
        return "multi_resume"
    if " ".join(text.split()) == "power off":
        return "power_off"
    try:
        login = parse_login_command(text)
    except ValueError:
        return "login_invalid"
    if login:
        return "login_renewal" if login.action == "renewal" else "login_" + login.value
    if len(text.split()) >= 2 and text.split()[0] in {"start", "stop"}:
        return "cross_control"
    if text in {"help", "report", "r", "status", "top", "channels", "posts", "ip"}:
        return "report" if text == "r" else text
    prefix = text.split(maxsplit=1)[:1]
    if prefix and prefix[0] in {"srch", "buy", "sell", "mkhr", "stts", "rsv", "gdcrs", "rank"}:
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
    except KiwoomAPIError as error:
        return str(error) if error.ip_related else _QUERY_ERROR
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
              session: BrokerSession, scheduler: CommandScheduler, *, monitor=None, golden_monitor=None,
              multi_monitor=None, should_stop=lambda: False) -> list[str]:
    if should_stop():
        return ["종료 요청을 확인하여 작업을 실행하지 않았습니다."]
    def before_send():
        if should_stop():
            raise ValueError("종료 요청을 확인하여 주문을 전송하지 않았습니다.")
    if command == "multi_resume":
        if session.mode != "paper" or multi_monitor is None:
            return ["모의 다중 전략 감시에서만 재개를 요청할 수 있습니다."]
        if multi_monitor.request_resume():
            return ["다중 전략 재개를 요청했습니다. 다음 공유 세션 대사가 성공한 뒤 재개하며 불확실 주문은 재전송하지 않습니다."]
        return ["다중 전략은 현재 주문 불확실성으로 정지된 상태가 아닙니다."]
    words = text.casefold().split()
    reset_both = (command.startswith("login_") or command == "mkhr"
                  or words == ["stts", "default"] or words[:2] == ["gdcrs", "intv"])
    if monitor is not None and (reset_both or words == ["stop", "ddcrs"]):
        monitor.reset()
    if golden_monitor is not None and (reset_both or words == ["stop", "gdcrs"]
            or words[:2] in (["gdcrs", "add"], ["gdcrs", "remove"], ["gdcrs", "clear"])):
        golden_monitor.reset()
    if command.startswith("login_"):
        return [handle_login(command, text, session, reporter=terminal_report)]
    if command == "srch":
        return handle_search(text, session)
    if command == "buy":
        return handle_buy(text, session, before_send=before_send)
    if command == "sell":
        return handle_sell(text, session, before_send=before_send)
    if command == "report":
        return handle_report(session)
    if command == "mkhr":
        return handle_market_hours(text)
    if command == "stts":
        return handle_settings(text, session)
    if command == "rsv":
        return handle_reservation(text, scheduler)
    if command in {"gdcrs", "cross_control"}:
        replies = handle_cross_command(text)
        if monitor is not None and getattr(replies, "started_ddcrs", False):
            monitor.reset(clear_halt=True)
        if golden_monitor is not None and getattr(replies, "started_gdcrs", False):
            golden_monitor.reset(clear_halt=True)
        return replies
    if command == "ip":
        return handle_ip()
    if command == "rank":
        # A protective liquidation must be a direct explicit symbol command.
        if re.search(r'\bsell\s+protect\b',text,re.IGNORECASE):
            return ['sell protect는 순위 일괄 실행을 지원하지 않습니다. sell protect {6자리 종목코드}를 직접 입력하세요.']
        return handle_rank_batch(text, session,
                                 lambda expanded: _dispatch(_text_command(expanded, config), expanded,
                                                            config, session, scheduler, monitor=monitor,
                                                            golden_monitor=golden_monitor, multi_monitor=multi_monitor,
                                                            should_stop=should_stop), should_stop=should_stop)
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
        # Keep the session identity alive until queued execution observations
        # have been persisted. Clearing it first would reject those callbacks.
        recorder = getattr(broker_session, "execution_recorder", None)
        if recorder is not None:
            try:
                recorder.stop()
            except Exception:
                report("종료 전 체결 기록 저장을 확인하지 못했습니다. 원장 대사가 필요합니다.")
        broker_session.clear()
        try:
            stop_path.touch(mode=0o600)
        except OSError:
            report("종료 요청 파일을 기록하지 못했지만 프로그램은 종료합니다.")
        state["status"] = "stopped"
        checkpoint()
    report("텔레그램 수신을 중지했습니다.")
    return 0


def _maintenance_paused(state_dir: Path) -> bool:
    return maintenance_status(state_dir)['paused']


def _maintenance_order_guard(state_dir: Path,reconciliation=None,expiry_resolution=None) -> None:
    if _maintenance_paused(state_dir):
        raise ValueError("receiver maintenance pause")
    if reconciliation is not None:reconciliation.assert_orders_allowed()
    if expiry_resolution is not None:expiry_resolution.assert_orders_allowed()


def _maintenance_binding(state_dir,readonly_phase,expiry_resolution):
    first=remember_cutover_binding(readonly_phase,state_dir)
    second=remember_cutover_binding(expiry_resolution,state_dir)
    if first!=second:raise MaintenanceReadUnavailable('maintenance_cutover_actor_binding_mismatch')
    return first


def _maintenance_reconciliation_tick(session,tracker,state_dir,readonly_phase,expiry_resolution,publish=None):
    """A separately bound expiry stage cannot restart or overwrite its read phase.

    Both stages retain their order latch until their own current-session proof
    is verified after the operator's marker is removed.
    """
    binding=_maintenance_binding(state_dir,readonly_phase,expiry_resolution)
    with cutover_guard(state_dir,binding)as acquired:
        if not acquired:return None
        # The caller's earlier paused branch can become obsolete while waiting
        # for the operator. Never enter tick with a removed bound marker.
        marker=Path(state_dir)/'maintenance-quiet.lock'
        if binding is not None:
            if marker.is_symlink():raise MaintenanceReadUnavailable('maintenance_cutover_marker_symlink')
            if not marker.exists():return None
        expiry_resolution.latch(state_dir)
        if expiry_resolution.requested(state_dir):
            result=expiry_resolution.tick(session,tracker,state_dir,readonly_phase)
        else:result=readonly_phase.tick(session,tracker,state_dir)
        if publish is not None:publish()
        return result


def _maintenance_release_if_verified(session,tracker,state_dir,readonly_phase,expiry_resolution):
    binding=_maintenance_binding(state_dir,readonly_phase,expiry_resolution)
    with cutover_guard(state_dir,binding)as acquired:
        marker=Path(state_dir)/'maintenance-quiet.lock'
        if not acquired or marker.exists()or marker.is_symlink():return False
        prior=(readonly_phase.blocked,expiry_resolution.blocked)
        if expiry_resolution.blocked and not expiry_resolution.release_if_verified(session,tracker):return False
        if readonly_phase.blocked and not readonly_phase.release_if_verified(session,tracker):
            # Partial success cannot open one gate independently of the other.
            readonly_phase.blocked=prior[0];expiry_resolution.blocked=prior[1]
            return False
        return True


class _TTLSettlementLocalLog:
    """One process cadence for unresolved settlements, never a Telegram send.

    The receiver's existing KST stdout wrapper owns the actual listener log.
    Order rotation, reason changes and brief recovery do not reset this timer.
    """
    _REASONS=frozenset(('cancellation_rest_original_identity_conflict','cancellation_ack_unconfirmed',
        'ttl_settlement_unavailable','cancellation_stream_unverified','cancellation_reconciliation_changed_or_stale',
        'cancellation_original_rest_missing','cancellation_rest_terminal_missing',
        'cancellation_rest_terminal_unverified','cancellation_source_hash_mismatch',
        'cancellation_broker_remainder_not_zero','cancellation_query_completion_missing'))

    def __init__(self, *, clock=None, emit=None):
        self.clock=clock or time.monotonic
        self.emit=emit or (lambda line:print(line,flush=True))
        self.last_at=None

    @staticmethod
    def pending(status):
        if not isinstance(status,dict):return None
        nested=status.get('settlement_status')
        if isinstance(nested,dict) and nested.get('status')=='settlement_unconfirmed':return nested
        return status if status.get('status')=='settlement_unconfirmed' else None

    def observe(self, status):
        pending=self.pending(status)
        if pending is None:return False
        try:at=self.clock()
        except Exception:return False
        if type(at) not in (int,float) or not math.isfinite(at) or at<0:return False
        if self.last_at is not None and at-self.last_at<600:return False
        self.last_at=at
        number=pending.get('original_order_number')
        if not isinstance(number,str) or re.fullmatch('[0-9]{1,7}',number) is None or int(number)<=0:number=None
        reason=pending.get('reason')
        safe_reason=reason if type(reason) is str and reason in self._REASONS else 'settlement_reason_unclassified'
        line={'event':'ttl_cancellation_settlement_retry','mode':'paper','status':'settlement_unconfirmed',
            'reason_code':safe_reason,'original_order_number':number,'principal_reservation_released':False,
            'log_interval_seconds':600}
        try:self.emit(json.dumps(line,ensure_ascii=True,sort_keys=True,separators=(',',':')))
        except Exception:return False
        return True


class _ExperimentSupervisor:
    """Listener-owned scheduling; all broker work uses the existing session."""
    def __init__(self, monitor, *, ttl=None, recovery=None, clock=None, settings=None, now=None, launcher=None, transition_gate=None, prior_day_scheduler=None, ttl_settlement_log=None):
        self.monitor=monitor
        self.ttl=ttl or UnfilledBuyTTLMonitor()
        self.recovery=recovery or recover_paper_experiment
        self.clock=clock or time.monotonic
        self._ttl_settlement_log=ttl_settlement_log or _TTLSettlementLocalLog(clock=self.clock)
        self.settings=settings or load_settings
        self.now=now or (lambda:datetime.now(KST))
        self.launcher=launcher or self._launch
        self._recovery_lock=threading.Lock()
        self._recovery_inflight=False
        self._recovery_notices=[]
        self.next_recovery=0.0
        self.startup_pending=True
        self.last_recovery=None
        self.enabled=False
        self.failure=None
        self.last_ttl_issue=None
        self.last_sell_ttl_issue=None
        self.last_recovery_issue=None
        self.last_monitor_issue=None
        self.transition_gate=transition_gate or LegacyResumeGate()
        self.prior_day_scheduler=prior_day_scheduler or PriorDayPendingScheduler(launch=self.launcher)
        self._transition_notice=False

    @staticmethod
    def _launch(function):
        thread=threading.Thread(target=function,name='paper-experiment-recovery',daemon=True)
        thread.start()

    def _active(self, session):
        if getattr(session,'mode',None)!='paper':return False
        return effective_paper_experiment(self.settings(),'paper')['enabled']

    def tick(self, session, tracker, notify, stop):
        if getattr(session,'mode',None)!='paper':
            self.enabled=False
            return False
        try:
            config=self.settings();active=effective_paper_experiment(config,'paper')['enabled']
            legacy_allowed=self.transition_gate.observe(config)
        except Exception:
            self.enabled=False;self.failure='experiment_settings_unavailable'
            return True  # A malformed paper setting cannot silently enable legacy strategies.
        if not active:
            self.enabled=False;self.startup_pending=True
            if not legacy_allowed:
                self.failure='legacy_resume_confirmation_required'
                if not self._transition_notice:
                    notify('모의 실험이 꺼졌습니다. 확인하면 재개될 루프: '+legacy_loop_description(config)+'. 자동 재개하지 않으며 stts의 확인 명령이 필요합니다.')
                    self._transition_notice=True
                return True
            self.failure=None;self._transition_notice=False
            return False
        self.enabled=True;self.failure=None;self._transition_notice=False
        with self._recovery_lock:
            notices=self._recovery_notices[:];self._recovery_notices.clear()
        for notice in notices:notify(notice)
        if stop():return True
        try:
            self.ttl.tick(session,tracker,180,enabled_check=lambda: not stop() and self._active(session))
        except Exception:
            self.failure='ttl_retry_pending'
        ttl_status=getattr(self.ttl,'last_status',None)
        self._ttl_settlement_log.observe(ttl_status)
        issue_status=self._ttl_settlement_log.pending(ttl_status) or ttl_status
        issue=(issue_status.get('status'),issue_status.get('reason')) if isinstance(issue_status,dict) and issue_status.get('status') in ('retry_pending','cancel_unconfirmed','settlement_unconfirmed') else None
        if issue and issue!=self.last_ttl_issue:
            notify('모의 자동매수 미체결 대사를 다음 주기에 다시 확인합니다. 불확실 주문은 재전송하지 않습니다.')
        self.last_ttl_issue=issue
        sell_status=getattr(self.ttl,'last_sell_status',None)
        sell_issue=(sell_status.get('status'),sell_status.get('reason')) if isinstance(sell_status,dict) and sell_status.get('status') in ('retry_pending','cancel_unconfirmed','settlement_unconfirmed','cancel_input_ack_unconfirmed_settlement') else None
        if sell_issue and sell_issue!=self.last_sell_ttl_issue:
            notify('모의 동시호가 매도 취소·잔량을 확인 중입니다. 불확실 취소는 재전송하지 않습니다.')
        self.last_sell_ttl_issue=sell_issue
        try:status=self.monitor.status()
        except Exception:status={}
        status=status if isinstance(status,dict) else {}
        recorder=getattr(session,'execution_recorder',None)
        try:recorder_state=recorder.status() if recorder is not None else {}
        except Exception:recorder_state={'error':'recorder_status_unavailable'}
        diagnostics=status.get('symbol_diagnostics',{}) if isinstance(status,dict) else {}
        ownership=any(isinstance(x,dict) and (x.get('reason')=='ownership_unavailable' or x.get('status')=='unsupported')
                      for x in diagnostics.values()) if isinstance(diagnostics,dict) else False
        needs=(self.startup_pending or bool(recorder_state.get('error'))
               or bool(status.get('quarantined')) or ownership)
        at=self.clock()
        with self._recovery_lock:
            start_recovery=needs and at>=self.next_recovery and not self._recovery_inflight and not stop()
            if start_recovery:
                self.next_recovery=at+60.0;self.startup_pending=False;self._recovery_inflight=True
        if start_recovery:
            def work():
                notices=[]
                try:result=self.recovery(session,tracker,self.now(),lambda message:notices.append(message))
                except Exception:result={'status':'reconciliation_unavailable','reconciled':False}
                with self._recovery_lock:
                    self.last_recovery=result
                    if isinstance(result,dict) and result.get('reconciled') is True:
                        self.last_recovery_issue=None
                    elif notices:
                        if notices[-1]!=self.last_recovery_issue:
                            self._recovery_notices.append(notices[-1]);self.last_recovery_issue=notices[-1]
                    if (not isinstance(result,dict) or result.get('reconciled') is not True
                            or result.get('remaining_registered_pending',0)>0):
                        self.startup_pending=True
                    self._recovery_inflight=False
            try:self.launcher(work)
            except Exception:
                with self._recovery_lock:
                    self._recovery_inflight=False;self.startup_pending=True
                    self.last_recovery={'status':'recovery_worker_unavailable','reconciled':False}
        if not stop():
            with self._recovery_lock:prior_allowed=not self._recovery_inflight
            if prior_allowed:
                try:
                    now=self.now()
                    self.prior_day_scheduler.tick(session,tracker,now,self.monitor.calendar(now),notify)
                except Exception:
                    self.failure='prior_day_reconciliation_unavailable'
            try:
                self.monitor.tick(session,notify);self.last_monitor_issue=None
            except Exception:
                self.failure='experiment_monitor_retry_pending'
                if self.last_monitor_issue is None:
                    notify('모의 실험 감시 오류를 기록했으며 다음 주기에 다시 확인합니다.')
                self.last_monitor_issue='experiment_monitor_retry_pending'
        return True

    def status(self):
        try:result=self.monitor.status()
        except Exception:result=None
        return {'enabled':self.enabled,'failure':self.failure,
                'monitor':result if isinstance(result,dict) else None,
                'recovery':self.last_recovery,'recovery_inflight':self._recovery_inflight,
                'prior_day_reconciliation':self.prior_day_scheduler.status(),
                'ttl':getattr(self.ttl,'last_status',None),
                'sell_auction_cancellation':getattr(self.ttl,'last_sell_status',None)}

    def seconds_until_next(self):
        if not self.enabled:return None
        values=[]
        try:due=self.monitor.seconds_until_next()
        except Exception:due=1
        if isinstance(due,(int,float)) and not isinstance(due,bool):values.append(max(0,due))
        ttl_at=getattr(self.ttl,'next_at',None)
        if isinstance(ttl_at,datetime) and ttl_at.tzinfo is not None:
            values.append(max(0,(ttl_at-self.now()).total_seconds()))
        if self.startup_pending:
            values.append(max(0,self.next_recovery-self.clock()))
        return min(values) if values else None


class _LegacyRecoverySupervisor:
    """Recovery/TTL continues independently of gdcrs; explicit resume only."""
    def __init__(self, monitor, *, ttl=None, recovery=None, settings=None, now=None, clock=None, launcher=None, ttl_settlement_log=None):
        self.monitor=monitor; self.ttl=ttl or UnfilledBuyTTLMonitor()
        self.recovery=recovery or recover_paper_experiment
        self.settings=settings or load_settings; self.now=now or (lambda:datetime.now(KST))
        self.clock=clock or time.monotonic; self.launcher=launcher or _ExperimentSupervisor._launch
        self._ttl_settlement_log=ttl_settlement_log or _TTLSettlementLocalLog(clock=self.clock)
        self.next_recovery=0.0; self.startup=True; self.inflight=False; self.result=None
        self._lock=threading.Lock(); self._notices=[]; self._last_issue=None; self._last_ttl=None

    def status(self):
        with self._lock:
            ttl_status=getattr(self.ttl,'last_status',None)
            return {'recovery_inflight':self.inflight,'recovery':self.result,
                    'ttl':ttl_status if isinstance(ttl_status,dict) else None}

    def tick(self,session,tracker,notify,stop):
        if getattr(session,'mode',None)!='paper' or stop():return
        try:
            status=self.monitor.status()
            if not isinstance(status,dict):return
            cfg=self.settings()
            if effective_paper_experiment(cfg,'paper')['enabled']:return
            from kiwoom_autotrader.config.settings import effective_unfilled_buy_ttl
            ttl=effective_unfilled_buy_ttl(cfg,'paper')
        except Exception:return
        try:
            self.ttl.tick(session,tracker,ttl,enabled_check=lambda:not stop()
                and getattr(session,'mode',None)=='paper'
                and not effective_paper_experiment(self.settings(),'paper')['enabled'])
        except Exception:
            if self._last_ttl!='ttl_retry_pending':notify('모의 자동매수 TTL 점검 오류: 다음 주기에 재대사합니다. 불확실 주문은 재전송하지 않습니다.')
            self._last_ttl='ttl_retry_pending'
        else:
            ttl_status=getattr(self.ttl,'last_status',{})
            self._ttl_settlement_log.observe(ttl_status)
            issue_status=self._ttl_settlement_log.pending(ttl_status) or ttl_status
            issue=issue_status.get('status') if isinstance(issue_status,dict) else None
            if issue in ('retry_pending','cancel_unconfirmed','settlement_unconfirmed') and issue!=self._last_ttl:
                notify('모의 자동매수 미체결 대사가 미확정입니다. 다음 주기에 다시 확인합니다.')
            self._last_ttl=issue
        with self._lock:
            notices=self._notices[:];self._notices.clear();result=self.result
        for notice in notices:notify(notice)
        if status.get('resume_requested') is True and isinstance(result,dict) and result.get('reconciled') is True:
            if self.monitor.resume_after_reconciliation(session,tracker,proof=result.get('proof')):
                notify('신선한 공유 세션 대사로 다중 전략 감시를 재개했습니다. 이전 주문은 재전송하지 않습니다.')
                status=self.monitor.status()
        recorder=getattr(session,'execution_recorder',None)
        try:stream=recorder.status() if recorder is not None else {}
        except Exception:stream={'error':'unavailable'}
        errors=status.get('symbol_errors',{})
        needs=(self.startup and (ttl>0 or cfg.get('gdcrs_enabled') is True or cfg.get('paper_multi_strategy_enabled') is True)
               or bool(stream.get('error')) or status.get('resume_requested') is True
               or bool(status.get('overnight_unobserved_positions'))
               or any('ownership' in str(v) for v in errors.values()))
        at=self.clock()
        with self._lock:
            launch=needs and not self.inflight and at>=self.next_recovery and not stop()
            if launch:self.inflight=True;self.next_recovery=at+60;self.startup=False
        if not launch:return
        def work():
            notices=[]
            try:
                if stop() or getattr(session,'mode',None)!='paper':return
                result=self.recovery(session,tracker,self.now(),notices.append,allow_legacy=True)
                with self._lock:
                    self.result=result
                    self.startup=not isinstance(result,dict) or result.get('reconciled') is not True or result.get('remaining_registered_pending',0)>0
                    issue=notices[-1] if notices else None
                    if issue and issue!=self._last_issue:self._notices.append(issue)
                    self._last_issue=issue
            except Exception:
                with self._lock:self.result={'status':'reconciliation_unavailable','reconciled':False};self.startup=True
            finally:
                with self._lock:self.inflight=False
        try:self.launcher(work)
        except Exception:
            with self._lock:self.inflight=False;self.startup=True


class _TrackerCheckpointPublisher:
    """Publish tracker progress without claiming a new monitor tick.

    The tracker owns its diagnostic clock and bounded stages. Its callback is
    installed before the bootstrap tick; ordinary monitor status is refreshed
    before/after tracker work, while stage callbacks reuse that exact status.
    Telemetry failures are explicit but never authorize or suppress an order.
    """
    def __init__(self, path, state, tracker, supervisor, session, *, now=None):
        self.path, self.state, self.tracker = path, state, tracker
        self.supervisor, self.session = supervisor, session
        self.now = now or (lambda: datetime.now(KST))
        self.owner_thread = threading.get_ident()
        self._publishing = False

    def _error(self, code, error):
        previous = self.state.get('telemetry_publish_error') or {}
        self.state['telemetry_publish_error'] = {
            'schema':'receiver-telemetry-publication-error-v1', 'code':code,
            'exception_type':type(error).__name__, 'observed_at':self.now().isoformat(),
            'failure_count':min(10**9, previous.get('failure_count', 0) + 1),
            'financial_policy_changed':False,
        }
        print('receiver telemetry publication failed: '+code, flush=True)

    def bind(self):
        setter = getattr(self.tracker, 'set_diagnostics_callback', None)
        if callable(setter):
            try:setter(self.progress)
            except Exception as error:self._error('tracker_diagnostics_bind_failed', error)

    def progress(self, snapshot):
        # Worker/00 callbacks never write the listener's shared state dictionary.
        if threading.get_ident() != self.owner_thread or self._publishing:
            return
        if not isinstance(snapshot, dict):
            error=ValueError('tracker_progress_snapshot_unverified')
            self._error('tracker_progress_snapshot_unverified', error)
            raise error
        self.publish('tracker_stage_progress', snapshot=snapshot, propagate=True)

    def publish(self, reason, *, refresh_monitor=False, snapshot=None, propagate=False):
        try:
            self._publishing = True
            if refresh_monitor:
                observed = self.supervisor.status()
                if not isinstance(observed, dict):raise ValueError('monitor_status_unverified')
                self.state.setdefault('automation_status', {})['paper_experiment'] = observed
            if snapshot is None:
                reader = getattr(self.tracker, 'diagnostics_snapshot', None)
                snapshot = reader() if callable(reader) else None
            if isinstance(snapshot, dict):self.state['tracker_diagnostics'] = snapshot
            # Substage callbacks can run under the tracker's history lock.
            # Re-entering the recorder mutex there could invert the WS path
            # (recorder -> history). Only the outer unlocked boundary refreshes
            # recorder/monitor state; callbacks publish the frozen snapshots.
            if refresh_monitor:
                recorder = getattr(self.session, 'execution_recorder', None)
                if recorder is not None:
                    observed = recorder.status()
                    if isinstance(observed, dict):
                        self.state['execution_recording'] = observed
                        self.state['recorder_snapshot_observed_at'] = self.now().isoformat()
            experiment = self.state.get('automation_status', {}).get('paper_experiment') or {}
            monitor = experiment.get('monitor') or {}
            health = monitor.get('tick_health') or {}
            self.state['state_publication'] = {
                'schema':'receiver-state-publication-v1', 'published_at':self.now().isoformat(),
                'reason':reason, 'scope':'current_process_only',
                'publication_clock_basis':'immediately_before_atomic_save',
                'monitor_last_completed_at':health.get('last_tick_completed_at'),
                'monitor_tick_time_synthesized':False,
            }
            _save_state(self.path, self.state)
        except Exception as error:
            self._error('tracker_checkpoint_publication_failed', error)
            if propagate:raise
        finally:self._publishing = False

    def tick(self, notify):
        self.publish('before_tracker_tick', refresh_monitor=True)
        try:return self.tracker.tick(self.session, notify)
        finally:self.publish('after_tracker_tick', refresh_monitor=True)


def _receive(
    state_dir: Path, state: dict[str, Any], config: TelegramConfig,
    max_polls: int | None, broker_session: BrokerSession, scheduler: CommandScheduler, monitor: CrossMonitor,
    execution_tracker: ExecutionTracker, golden_monitor: GoldenCrossMonitor, multi_monitor: MultiStrategyMonitor,
    experiment_supervisor: _ExperimentSupervisor,
) -> int:
    path, stop_path = state_dir / "state.json", state_dir / "stop.request"
    tracker_checkpoints = _TrackerCheckpointPublisher(path, state, execution_tracker,
                                                     experiment_supervisor, broker_session)
    tracker_checkpoints.bind()
    def maintenance_progress(observed):
        state['maintenance_reconciliation']=observed
        recorder=getattr(broker_session,'execution_recorder',None)
        if recorder is not None:state['execution_recording']=recorder.status()
        _save_state(path,state)
    maintenance_reads=getattr(broker_session,'maintenance_readonly_phase',None)
    if not isinstance(maintenance_reads,MaintenanceReadOnlyReconciliation):maintenance_reads=MaintenanceReadOnlyReconciliation()
    maintenance_reads.progress=maintenance_progress;maintenance_reads.latch(state_dir)
    broker_session.maintenance_readonly_phase=maintenance_reads
    expiry_resolution=getattr(broker_session,'maintenance_expiry_resolution',None)
    if not isinstance(expiry_resolution,MaintenanceExpiryResolution):expiry_resolution=MaintenanceExpiryResolution()
    expiry_resolution.latch(state_dir)
    broker_session.maintenance_expiry_resolution=expiry_resolution
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
    if not _maintenance_paused(state_dir):
        _send_notification(client, config, path, state, _STARTUP_MESSAGE,
                           "프로그램 시작 알림의 텔레그램 전송을 확인하지 못했습니다.")
    startup_login = broker_session.login("paper")
    terminal_report(startup_login, broker_session)
    if not _maintenance_paused(state_dir):
        _send_notification(client, config, path, state, startup_login.message,
                           "모의투자 자동 로그인 결과의 텔레그램 전송을 확인하지 못했습니다.")
    def notify_execution(message):
        if not _maintenance_paused(state_dir):
            _send_notification(client, config, path, state, message, "자동매매 원장 알림 전송을 확인하지 못했습니다.")
    # A dedicated bootstrap must not spend its fresh-query lease on ordinary
    # qry1/profit/account ticker work before the required qry0 batch starts.
    if (not maintenance_reads.blocked and not expiry_resolution.blocked
            and not maintenance_status(state_dir).get('reconciliation_required')):
        tracker_checkpoints.tick(notify_execution)
    legacy_supervisor = _LegacyRecoverySupervisor(multi_monitor, ttl=getattr(golden_monitor,'_ttl_monitor',None),
        ttl_settlement_log=getattr(experiment_supervisor,'_ttl_settlement_log',None))
    attempts, failures, scheduler_error_reported = 0, 0, False
    websocket_reminder = ErrorReminder()
    preopen_check = PreopenStreamCheck()
    stability_audit = ReceiverStabilityAudit(state_dir / 'stability-audit')
    maintenance_notice = None
    while max_polls is None or attempts < max_polls:
        if stop_path.exists():
            break
        broker_session.websocket_engine.sync_session()
        snapshot = broker_session.websocket_engine.snapshot()
        recorder = getattr(broker_session, "execution_recorder", None)
        if recorder is not None:
            state["execution_recording"] = recorder.status()
        state["automation_status"] = {"mode": broker_session.mode}
        for name, strategy in (("gdcrs", golden_monitor), ("ddcrs", monitor), ("paper_multi_strategy", multi_monitor)):
            status_reader = getattr(strategy, "status", None)
            observed = status_reader() if callable(status_reader) else None
            state["automation_status"][name] = observed if isinstance(observed, dict) else None
        state['automation_status']['paper_experiment']=experiment_supervisor.status()
        control_day=datetime.now(KST).strftime('%Y%m%d')
        if state.get('stability_control_audit',{}).get('day')!=control_day:
            try:
                initialize_control_audit(datetime.now(KST),root=(state_dir.parent if state_dir.name=='telegram'
                    else state_dir/'.control-audit-root'))
                state['stability_control_audit']={'day':control_day,'genesis_ready':True}
            except Exception:
                state['stability_control_audit']={'day':control_day,'genesis_ready':False}
        experiment_health = state['automation_status']['paper_experiment'].get('monitor') or {}
        state['stability_audit'] = stability_audit.pulse(datetime.now(KST),
            state.get('execution_recording') or {}, experiment_health.get('tick_health') or {},
            getattr(execution_tracker,'research_signal_log_status',None))
        # Observation time is independent of last 00 execution (an idle stream
        # may be healthy). This is current-process telemetry, never daily zeroes.
        state['stability_observation'] = {
            'schema':'receiver-stability-observation-v1',
            'observed_at':datetime.now(KST).isoformat(),
            'scope':'current_process_only',
            'daily_kpi_status':'requires_separate_complete_session_audit',
        }
        state['automation_status']['legacy_recovery']=legacy_supervisor.status()
        maintenance = maintenance_status(state_dir)
        expiry_resolution.latch(state_dir)
        state['maintenance_status'] = maintenance
        if maintenance['paused']:
            # No message polling, offset advancement, reservation claim, order
            # decision or Telegram send while the operator validates restart.
            # Keep the single paper session, execution recorder and account
            # snapshot ticker alive for read-only reconciliation and IPC.
            state["maintenance_paused"] = True
            if maintenance.get('reconciliation_required') or maintenance_reads.blocked:
                def publish_maintenance():
                    state['maintenance_reconciliation']=maintenance_reads.status()
                    state['maintenance_expiry_resolution']=expiry_resolution.status()
                    _save_state(path,state)
                _maintenance_reconciliation_tick(broker_session,execution_tracker,state_dir,
                                                maintenance_reads,expiry_resolution,publish_maintenance)
            else:
                _save_state(path,state);tracker_checkpoints.tick(notify_execution)
            attempts += 1
            time.sleep(1)
            continue
        if (expiry_resolution.blocked or maintenance_reads.blocked) and not _maintenance_release_if_verified(
                broker_session,execution_tracker,state_dir,maintenance_reads,expiry_resolution):
            state['maintenance_paused']=True
            state['maintenance_expiry_resolution']=expiry_resolution.status()
            state['maintenance_reconciliation']=maintenance_reads.status()
            _save_state(path,state);attempts+=1;time.sleep(1);continue
        state.pop("maintenance_paused", None)
        if maintenance['reason'] == 'maintenance_expired' and maintenance.get('expires_at') != maintenance_notice:
            _send_notification(client, config, path, state,
                'maintenance-quiet 잠금이 만료되어 무시했습니다. 원본 마커는 보존했으며 기존 안전 게이트로 감시를 계속합니다.',
                '정비 잠금 만료 알림 전송을 확인하지 못했습니다.')
            maintenance_notice = maintenance.get('expires_at')
        state['preopen_stream_check'] = preopen_check.tick(broker_session, execution_tracker,
                                                          datetime.now(KST), notify_execution)
        if recorder is not None:
            state['execution_recording'] = recorder.status()
        error = snapshot.get("error")
        if recorder is not None:
            error = error or state["execution_recording"].get("error")
        if websocket_reminder.due(error, time.monotonic()):
            _send_notification(client, config, path, state, error, "WebSocket 상태 알림 전송을 확인하지 못했습니다.")
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
                if stop_path.exists():
                    break
                print(f"예약 명령 실행: id={reserved['id']} command={command}", flush=True)
                if command == "power_off":
                    return _power_off(client, config, path, stop_path, state, broker_session)
                with automated_orders(execution_tracker, f"rsv#{reserved['id']}"):
                    replies = _dispatch(command, reserved["command"], config, broker_session, scheduler,
                                        monitor=monitor, golden_monitor=golden_monitor, multi_monitor=multi_monitor, should_stop=stop_path.exists)
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
            monitor_in = monitor.seconds_until_next()
            golden_in = golden_monitor.seconds_until_next()
            multi_in = multi_monitor.seconds_until_next()
            experiment_in = experiment_supervisor.seconds_until_next()
            execution_in = execution_tracker.seconds_until_next()
            deadlines = [25, *[math.ceil(value) for value in (due_in, schedule_in, monitor_in, golden_in, multi_in, experiment_in, execution_in) if value is not None]]
            timeout = max(1, min(deadlines))
            updates = client.get_updates(timeout=timeout, offset=state["next_offset"])
        except TelegramAPIError as error:
            fatal = re.search(r"code=(401|404|409)\b", str(error))
            if fatal:
                raise TelegramListenerError("텔레그램 수신 인증 또는 다른 수신 프로그램과의 충돌을 확인하세요.",
                                            diagnostic_code="poll_http_" + fatal.group(1)) from None
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
                stability_audit.command(command)
                state["received_count"] += 1
                state["last_command"] = command
            if command == "power_off":
                with control_origin('authenticated_user'):
                    return _power_off(client, config, path, stop_path, state, broker_session)
            # Commit before querying or sending. Restart never replays this update.
            _save_state(path, state)
            if command is None:
                continue
            print(f"텔레그램 명령 수신: {command}", flush=True)
            with control_origin('authenticated_user'):
                replies = _dispatch(command, update["message"]["text"], config, broker_session, scheduler,
                                    monitor=monitor, golden_monitor=golden_monitor, multi_monitor=multi_monitor, should_stop=stop_path.exists)
            _send_replies(client, config, path, stop_path, state, replies)
        experimental = False
        if not stop_path.exists():
            experimental=experiment_supervisor.tick(broker_session,execution_tracker,lambda message: _send_notification(
                client,config,path,state,message,"모의 실험 알림 전송을 확인하지 못했습니다."),
                lambda:stop_path.exists() or _maintenance_paused(state_dir))
        if not experimental:
            legacy_supervisor.tick(broker_session,execution_tracker,notify_execution,
                lambda:stop_path.exists() or _maintenance_paused(state_dir))
            if not stop_path.exists():
                with automated_orders(execution_tracker, "ddcrs"):
                    monitor.tick(broker_session, lambda message: _send_notification(
                        client, config, path, state, message, "데드크로스 알림 전송을 확인하지 못했습니다."))
            if not stop_path.exists():
                with automated_orders(execution_tracker, "gdcrs"):
                    golden_monitor.tick(broker_session, lambda message: _send_notification(
                        client, config, path, state, message, "골든크로스 알림 전송을 확인하지 못했습니다."))
            if not stop_path.exists():
                multi_monitor.tick(broker_session, lambda message: _send_notification(
                    client, config, path, state, message, "모의 다중 전략 알림 전송을 확인하지 못했습니다."))
        if not stop_path.exists():
            tracker_checkpoints.tick(notify_execution)
    _finish(path, state, "stopped")
    print("텔레그램 수신을 중지했습니다.", flush=True)
    return 0


@kst_listener_logs
def run_listener(state_dir: Path, *, max_polls: int | None = None, broker_session: BrokerSession | None = None,
                 scheduler: CommandScheduler | None = None, monitor: CrossMonitor | None = None,
                 golden_monitor: GoldenCrossMonitor | None = None,
                 multi_monitor: MultiStrategyMonitor | None = None,
                 websocket_engine: SharedWebSocketEngine | None = None,
                 execution_tracker: ExecutionTracker | None = None,
                 execution_recorder=None, experiment_monitor=None, experiment_ttl=None) -> int:
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
    research_server = None
    try:
        broker_session = broker_session or BrokerSession(
            renewal_time=load_settings()["login_renewal_time"], save_renewal=save_renewal_time,
        )
        broker_session.maintenance_readonly_phase=MaintenanceReadOnlyReconciliation()
        broker_session.maintenance_readonly_phase.latch(state_dir)
        broker_session.maintenance_expiry_resolution=MaintenanceExpiryResolution()
        broker_session.maintenance_expiry_resolution.latch(state_dir)
        broker_session.maintenance_order_guard = lambda: _maintenance_order_guard(
            state_dir,getattr(broker_session,'maintenance_readonly_phase',None),
            getattr(broker_session,'maintenance_expiry_resolution',None))
        websocket_engine = websocket_engine or SharedWebSocketEngine(lambda: broker_session)
        broker_session.websocket_engine = websocket_engine
        execution_tracker = execution_tracker or ExecutionTracker()
        execution_recorder = execution_recorder or ExecutionStreamRecorder(broker_session, execution_tracker)
        broker_session.execution_recorder = execution_recorder
        def recording_guard():
            if execution_recorder.status().get("ready") is not True:
                raise ValueError("execution recording unavailable")
        broker_session.execution_recording_guard = recording_guard
        broker_session.paper_automation_reconcile = PaperReconciliationGate(broker_session, execution_tracker)
        scheduler = scheduler or CommandScheduler()
        monitor = monitor or CrossMonitor(
            state_path=state_dir / "ddcrs-order-markers.sqlite3",
            stop_requested=(state_dir / "stop.request").exists,
        )
        golden_monitor = golden_monitor or GoldenCrossMonitor(
            state_path=state_dir / "gdcrs-order-markers.sqlite3",
            stop_requested=(state_dir / "stop.request").exists,
        )
        multi_monitor = multi_monitor or MultiStrategyMonitor(
            settings_loader=load_settings, stop_requested=(state_dir / "stop.request").exists,
            executor=execute_paper_strategy,
        )
        experiment_monitor = experiment_monitor or PaperExperimentMonitor(
            settings_loader=load_settings, stop_requested=(state_dir / 'stop.request').exists)
        experiment_supervisor = _ExperimentSupervisor(experiment_monitor, ttl=experiment_ttl,
            transition_gate=LegacyResumeGate(state_dir/'experiment-legacy-resume.json'))
        broker_session.legacy_resume_gate=experiment_supervisor.transition_gate
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
        execution_recorder.start()
        # Research is a separate single-worker local service. Failure must not
        # stop trading or silently fall back to a second broker login. Bounded
        # unit-test listeners skip binding; server behavior is tested directly.
        if max_polls is None:
            try:
                research_server = PaperResearchServer(broker_session, state_dir)
                research_server.start()
            except Exception:
                research_server = None
                print("모의 연구 조회 연결을 시작하지 못했습니다. 매매 수신은 계속합니다.", flush=True)
        return _receive(state_dir, state, config, max_polls, broker_session, scheduler, monitor,
                        execution_tracker, golden_monitor, multi_monitor, experiment_supervisor)
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
        cleanup_failed = False
        if execution_tracker is not None:
            setter = getattr(execution_tracker, 'set_diagnostics_callback', None)
            if callable(setter):
                try:setter(None)
                except Exception:cleanup_failed = True
        for resource, method in ((research_server, "stop"), (execution_recorder, "stop"), (websocket_engine, "close"), (monitor, "reset"),
                                 (golden_monitor, "reset"), (multi_monitor, "reset"), (experiment_monitor, "reset"), (broker_session, "clear")):
            if resource is not None:
                try:
                    getattr(resource, method)()
                except Exception:
                    cleanup_failed = True
        if broker_session is not None:
            broker_session.execution_recording_guard = None
            broker_session.execution_recorder = None
            broker_session.paper_automation_reconcile = None
        try:
            os.close(lock_fd)
        except OSError:
            cleanup_failed = True
        if cleanup_failed:
            try:
                print("일부 연결의 종료 확인에 실패했습니다. 프로그램 실행 잠금과 로그인 세션 정리를 모두 시도했습니다.", flush=True)
            except OSError:
                pass
