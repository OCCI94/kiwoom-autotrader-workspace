"""Read selected Telegram broadcast channels with a personal-account session.

History pages and forward checkpoints are committed together by ChannelStore.
The collector never sends messages, joins chats, marks messages read, or downloads
media. Authentication remains an explicit local setup step.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
import errno
from kiwoom_autotrader.utils.platform_io import fcntl
import json
import logging
import secrets
import sqlite3
from kiwoom_autotrader.utils.platform_io import os
from pathlib import Path
import stat
import tempfile
import time
from typing import Callable
from zoneinfo import ZoneInfo


_PAGE_SIZE = 100
_POLL_INTERVAL = 60
_STATUS_LIMIT = 16 * 1024
_CONNECT_TIMEOUT = 60


def _transient_connection(error):
    """Do not reconnect authentication, validation or local-storage failures."""
    current = error
    for _ in range(4):
        if isinstance(current, (TimeoutError, ConnectionError)):
            return True
        if isinstance(current, OSError) and current.errno in {
                errno.ENETDOWN, errno.ENETUNREACH, errno.EHOSTUNREACH, errno.ECONNRESET,
                errno.ECONNREFUSED, errno.ETIMEDOUT, errno.EPIPE}:
            return True
        current = getattr(current, '__cause__', None) or getattr(current, '__context__', None)
        if current is None: break
    return False


def _collector_log(message):
    # The CLI also has a KSTLineWriter. Avoid a duplicate prefix there while
    # preserving KST provenance for direct/library collector entrypoints.
    import sys
    from kiwoom_autotrader.utils.kst_logging import KSTLineWriter
    try:
        prefix = '' if isinstance(sys.stdout, KSTLineWriter) else datetime.now(ZoneInfo('Asia/Seoul')).strftime('[%Y-%m-%d %H:%M:%S KST] ')
        print(f'{prefix}pid={os.getpid()} {message}', flush=True)
    except Exception:
        # A broken log sink must not replace the original failure.
        pass


class CollectorError(RuntimeError):
    """Sanitized collector failure with no remote response or account secrets."""


def failure_code(error):
    """Stable diagnostic category; never serialize exception messages/accounts."""
    # Failure classification must work even if importing a dependency failed.
    domain_codes = {
        ('kiwoom_autotrader.telegram.channel_store', 'ExternalStorageUnavailable'): 'storage_unavailable',
        ('kiwoom_autotrader.telegram.channel_store', 'ChannelStoreError'): 'local_database_failure',
        ('kiwoom_autotrader.telegram.account', 'AccountConnectionError'): 'account_connection_failure',
        ('kiwoom_autotrader.utils.keychain', 'KeychainError'): 'account_credentials_failure',
    }
    for cls in type(error).__mro__:
        code = domain_codes.get((cls.__module__, cls.__name__))
        if code:
            return code
    if isinstance(error, PermissionError): return 'local_permission_denied'
    if isinstance(error, FileNotFoundError): return 'local_file_missing'
    if isinstance(error, sqlite3.DatabaseError): return 'local_database_failure'
    if isinstance(error, TimeoutError): return 'connection_timeout'
    if isinstance(error, ConnectionError): return 'connection_failure'
    if isinstance(error, ImportError): return 'dependency_import_failure'
    if isinstance(error, OSError): return 'local_os_failure'
    if isinstance(error, CollectorError): return 'collector_validation_failed'
    return 'unexpected_failure'


def log_background_failure(_loop, context):
    """Record asyncio failures without printing a task, future or raw exception."""
    try:
        code = failure_code(context.get('exception'))
        _collector_log(f'텔레그램 수집 비동기 오류: phase=asyncio_background code={code}')
    except Exception:
        # Raising here would send the original context to asyncio's default logger.
        pass


def make_client(config):
    from kiwoom_autotrader.utils.dependencies import enable_account_dependencies
    enable_account_dependencies()
    from kiwoom_autotrader.telegram.account import make_client as factory
    return factory(config)


def load_account_config():
    from kiwoom_autotrader.telegram.account_credentials import load_account_config as load
    return load()


def _store(state_dir, account_id):
    from kiwoom_autotrader.telegram.channel_store import ChannelStore
    return ChannelStore(state_dir, account_id)


def _input_peer(channel):
    from kiwoom_autotrader.utils.dependencies import enable_account_dependencies
    enable_account_dependencies()
    from telethon.tl.types import InputPeerChannel
    return InputPeerChannel(channel["channel_id"], channel["access_hash"])


def _quiet_library() -> None:
    logger = logging.getLogger("telethon")
    logger.handlers = [logging.NullHandler()]
    logger.propagate = False
    logger.setLevel(logging.CRITICAL + 1)


async def _verify_identity(client, config) -> None:
    from kiwoom_autotrader.telegram.account import verify_account
    await verify_account(client, config)


def _channel_metadata(entity) -> dict | None:
    if (
        getattr(entity, "broadcast", None) is not True
        or getattr(entity, "megagroup", False) is True
        or getattr(entity, "left", False) is True
        or type(getattr(entity, "id", None)) is not int or entity.id <= 0
        or type(getattr(entity, "access_hash", None)) is not int
        or not isinstance(getattr(entity, "title", None), str)
    ):
        return None
    return {
        "channel_id": entity.id, "access_hash": entity.access_hash,
        "title": entity.title, "username": getattr(entity, "username", None),
    }


async def list_account_channels(config) -> list[dict]:
    """List joined broadcast channels for local explicit selection only."""
    _quiet_library()
    client = make_client(config)
    try:
        await client.connect()
        await _verify_identity(client, config)
        channels = []
        async for dialog in client.iter_dialogs():
            value = _channel_metadata(getattr(dialog, "entity", None))
            if value is not None:
                channels.append(value)
        return sorted(channels, key=lambda item: (item["title"].casefold(), item["channel_id"]))
    except CollectorError:
        raise
    except Exception as error:
        delay = _flood_delay(error)
        if delay is not None:
            raise CollectorError(f"텔레그램 요청 제한으로 {delay}초 뒤 채널 목록을 다시 조회하세요.") from None
        raise CollectorError("텔레그램 개인 계정의 채널 목록을 조회하지 못했습니다.") from None
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


def _timestamp(value) -> int:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise CollectorError("텔레그램 채널 글의 날짜 형식이 올바르지 않습니다.")
    timestamp = int(value.timestamp())
    if timestamp < 0:
        raise CollectorError("텔레그램 채널 글의 날짜 형식이 올바르지 않습니다.")
    return timestamp


def _message_row(message, channel_id: int) -> dict:
    message_id = getattr(message, "id", None)
    peer = getattr(message, "peer_id", None)
    if type(message_id) is not int or message_id <= 0 or getattr(peer, "channel_id", None) != channel_id:
        raise CollectorError("등록된 채널의 글인지 확인하지 못했습니다.")
    text = getattr(message, "message", None)
    if text is None:
        text = ""
    if not isinstance(text, str):
        raise CollectorError("텔레그램 채널 글의 본문 형식이 올바르지 않습니다.")
    edited = getattr(message, "edit_date", None)
    media = getattr(message, "media", None)
    media_name = type(media).__name__ if media is not None else None
    media_type = {
        "MessageMediaPhoto": "photo", "MessageMediaDocument": "document",
        "MessageMediaWebPage": "web_page", "MessageMediaPoll": "poll",
        "MessageMediaContact": "contact", "MessageMediaGeo": "geo",
        "MessageMediaGeoLive": "geo", "MessageMediaVenue": "geo",
    }.get(media_name, "other" if media is not None else None)
    return {
        "message_id": message_id, "published_at": _timestamp(message.date),
        "edited_at": _timestamp(edited) if edited is not None else None,
        "text": text, "media_type": media_type,
        "grouped_id": getattr(message, "grouped_id", None),
    }


async def _page(client, peer, channel_id, **kwargs) -> list[dict]:
    # Keep page requests modest; Telegram's explicit FloodWait takes precedence.
    await asyncio.sleep(1)
    rows = []
    async for message in client.iter_messages(peer, limit=_PAGE_SIZE, **kwargs):
        rows.append(_message_row(message, channel_id))
        if len(rows) > _PAGE_SIZE:
            raise CollectorError("텔레그램 채널 조회 범위를 초과했습니다.")
    return rows


def _commit_page(store, channel_id, rows, **kwargs) -> bool:
    from kiwoom_autotrader.telegram.channel_store import ChannelStoreError
    try:
        store.commit_page(channel_id, rows, **kwargs)
        return True
    except ChannelStoreError:
        # A local channel selection change can race with an in-flight read.
        # Do not retain its page or fail other selected sources in that case.
        if channel_id not in {channel["channel_id"] for channel in store.channels()}:
            return False
        raise


async def collect_once(client, store, *, stop_requested: Callable[[], bool] | None = None) -> int:
    """Fetch bounded pages without skipping large forward backlogs.

    New channels start with their most recent 100 accessible posts. Each later
    cycle fetches up to 100 new posts oldest-first and one older history page.
    Deleted/inaccessible history cannot be recovered. Previously archived edits
    are updated only when their message is fetched again, not by a global scan.
    """
    stopped = stop_requested or (lambda: False)
    fetched = 0
    for channel in store.channels():
        if stopped():
            break
        channel_id = channel["channel_id"]
        peer = _input_peer(channel)
        entity = await client.get_entity(peer)
        metadata = _channel_metadata(entity)
        if metadata is None or metadata["channel_id"] != channel_id:
            raise CollectorError("선택한 채널에 접근할 수 없거나 채널 종류가 변경됐습니다.")
        latest_id = channel["latest_id"]
        if latest_id == 0:
            rows = await _page(client, peer, channel_id)
            ids = [row["message_id"] for row in rows]
            committed = _commit_page(
                store, channel_id, rows, latest_id=max(ids) if ids else 0,
                history_cursor=min(ids) if ids else None,
                history_complete=len(rows) < _PAGE_SIZE,
                collection_kind="latest_page", page_index=1, batch_id=secrets.token_hex(16),
            )
            if committed:
                fetched += len(rows)
            continue
        rows = await _page(client, peer, channel_id, min_id=latest_id, reverse=True)
        if any(row["message_id"] <= latest_id for row in rows):
            raise CollectorError("텔레그램 새 글 조회 위치를 확인하지 못했습니다.")
        if rows:
            if not _commit_page(store, channel_id, rows, latest_id=max(row["message_id"] for row in rows),
                                collection_kind="forward_page", page_index=1, batch_id=secrets.token_hex(16)):
                continue
            fetched += len(rows)
        if not channel["history_complete"] and not stopped():
            if channel_id not in {source["channel_id"] for source in store.channels()}:
                continue
            cursor = channel["history_cursor"]
            if type(cursor) is not int or cursor <= 0:
                raise CollectorError("텔레그램 과거 글 조회 위치를 확인하지 못했습니다.")
            older = await _page(client, peer, channel_id, offset_id=cursor)
            if any(row["message_id"] >= cursor for row in older):
                raise CollectorError("텔레그램 과거 글 조회 위치를 확인하지 못했습니다.")
            committed = _commit_page(
                store, channel_id, older,
                history_cursor=min(row["message_id"] for row in older) if older else None,
                history_complete=len(older) < _PAGE_SIZE,
                collection_kind="backfill_page", page_index=1, batch_id=secrets.token_hex(16),
            )
            if committed:
                fetched += len(older)
    return fetched


def _flood_delay(error) -> int | None:
    if type(error).__name__ == "AccountConnectionError":
        seconds = getattr(error, "retry_after_seconds", None)
        return seconds if type(seconds) is int and 0 < seconds <= 365 * 24 * 3600 else None
    # Telethon 1.x reports FloodWait/SlowModeWait as seconds; do not expose str(error).
    if type(error).__name__ not in {"FloodWaitError", "FloodTestPhoneWaitError", "SlowModeWaitError"}:
        return None
    seconds = getattr(error, "seconds", None)
    return seconds if type(seconds) is int and 0 < seconds <= 365 * 24 * 3600 else None


def _write_status(path: Path, status: dict) -> None:
    temporary = None
    try:
        fd, temporary = tempfile.mkstemp(prefix=".collector-", dir=path.parent)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(status, stream, ensure_ascii=True, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except OSError:
        status['phase'] = 'write_status'
        raise
    finally:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)


def _retry_after(path: Path) -> int:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return 0
    try:
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                raise ValueError
            raw = stream.read(_STATUS_LIMIT + 1)
        saved = json.loads(raw)
        value = saved.get("retry_after", 0)
        if len(raw) > _STATUS_LIMIT or type(value) is not int or value < 0:
            raise ValueError
        return value
    except (OSError, ValueError, TypeError, AttributeError):
        raise CollectorError("채널 수집 대기 상태를 읽지 못했습니다.") from None


async def _wait_until(deadline: float, stop_path: Path, health_check=None) -> bool:
    while time.time() < deadline:
        if stop_path.exists():
            return False
        if health_check is not None:
            health_check()
        await asyncio.sleep(min(1, max(0, deadline - time.time())))
    return not stop_path.exists()


async def run_collector(state_dir: Path, *, max_cycles: int | None = None) -> int:
    """Run read-only channel polling until explicitly stopped; never auto-login."""
    phase = 'validate_arguments'
    lock_fd = None
    client = None
    status = None
    try:
        if max_cycles is not None and (type(max_cycles) is not int or max_cycles < 0):
            raise CollectorError("채널 수집 횟수는 0 이상 정수여야 합니다.")
        _quiet_library()
        phase = 'prepare_state'
        state_dir = Path(state_dir)
        if state_dir.is_absolute() and state_dir.parts[:2] == ('/', 'Volumes'):
            _collector_log('텔레그램 수집 실패: phase=prepare_state code=state_path_not_local')
            return 75
        state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        state_dir.chmod(0o700)
        status_path, stop_path = state_dir / "collector-status.json", state_dir / "collector-stop.request"
        phase = 'acquire_lock'
        lock_fd = os.open(state_dir / "collector.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        info = os.fstat(lock_fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise CollectorError("채널 수집 잠금 파일이 올바르지 않습니다.")
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise CollectorError("텔레그램 채널 수집기가 이미 실행 중입니다.") from None
        os.fchmod(lock_fd, 0o600)
        phase = 'read_previous_status'
        retry_after = _retry_after(status_path)
        stop_path.unlink(missing_ok=True)
        status = {
            "version": 1, "pid": os.getpid(), "status": "starting",
            "started_at": int(time.time()), "cycle_count": 0, "fetched_count": 0,
            "last_success_at": None, "retry_after": retry_after,
            "phase": "account_configuration", "error_code": None,
            "reconnect_count": 0, "last_connection_failure": None,
        }
        _write_status(status_path, status)
        config = load_account_config()
        if config is None:
            raise CollectorError("텔레그램 개인 계정 연결을 먼저 완료하세요.")
        if retry_after > time.time():
            status['phase'] = 'wait_rate_limit'
            status["status"] = "rate_limited"
            _write_status(status_path, status)
            if not await _wait_until(retry_after, stop_path):
                status["status"] = "stopped"
                _write_status(status_path, status)
                return 0
        status['phase'] = 'open_storage'
        with _store(state_dir, config.user_id) as store:
            store.check_storage_health()
            status['phase'] = 'create_client'
            client = make_client(config)
            status['phase'] = 'load_channels'
            if not store.channels():
                raise CollectorError("수집할 텔레그램 채널을 먼저 선택하세요.")
            attempts = 0
            connected = False
            consecutive_connection_failures = 0
            status['phase'] = 'load_settings'
            from kiwoom_autotrader.config.settings import load_settings
            from kiwoom_autotrader.utils.message_dataset import daily_output_root, export_daily_dataset
            while not stop_path.exists() and (max_cycles is None or attempts < max_cycles):
                attempts += 1
                try:
                    status['phase'] = 'check_storage'
                    store.check_storage_health()
                    if not connected:
                        status['phase'] = 'connect'
                        _write_status(status_path, status)
                        await asyncio.wait_for(client.connect(), timeout=_CONNECT_TIMEOUT)
                        status['phase'] = 'verify_identity'
                        await asyncio.wait_for(_verify_identity(client, config), timeout=_CONNECT_TIMEOUT)
                        connected = True
                    status['phase'] = 'load_settings'
                    settings = load_settings()
                    status["status"] = "running"
                    status["retry_after"] = 0
                    status['error_code'] = None
                    status['phase'] = 'collect_messages'
                    _write_status(status_path, status)
                    fetched = await collect_once(client, store, stop_requested=stop_path.exists)
                    if settings["daily_message_export"]:
                        status['phase'] = 'export_messages'
                        export_daily_dataset(store, daily_output_root(store, state_dir))
                    status["cycle_count"] += 1
                    status["fetched_count"] += fetched
                    status["last_success_at"] = int(time.time())
                    consecutive_connection_failures = 0
                    status['phase'] = 'waiting_next_cycle'
                    _write_status(status_path, status)
                except Exception as error:
                    delay = _flood_delay(error)
                    if delay is None and _transient_connection(error):
                        connected = False
                        consecutive_connection_failures += 1
                        status['reconnect_count'] += 1
                        delay = min(300, 5 * 2 ** min(consecutive_connection_failures - 1, 6))
                        at = datetime.now(ZoneInfo('Asia/Seoul')).isoformat()
                        code = 'connection_timeout' if isinstance(error, TimeoutError) else 'connection_failure'
                        status.update(status='reconnecting', error_code=code,
                            retry_after=int(time.time()) + delay,
                            last_connection_failure={'at':at, 'phase':status['phase'], 'code':code,
                                'retry_delay_seconds':delay, 'same_process':True})
                        _write_status(status_path, status)
                        _collector_log('텔레그램 수집 연결 재시도: phase=' + status['phase'] +
                                       ' code=' + code + ' delay_seconds=' + str(delay))
                        try:
                            await asyncio.wait_for(client.disconnect(), timeout=10)
                        except Exception:
                            pass
                        if max_cycles is not None and attempts >= max_cycles: break
                        if not await _wait_until(status['retry_after'], stop_path, store.check_storage_health): break
                        continue
                    if delay is None:
                        raise
                    status["status"] = "rate_limited"
                    status['error_code'] = 'telegram_rate_limited'
                    status["retry_after"] = int(time.time()) + delay + 1
                    _write_status(status_path, status)
                    _collector_log('텔레그램 요청 제한으로 수집 대기: phase=' + status['phase'] +
                                   ' code=telegram_rate_limited')
                    if max_cycles is not None and attempts >= max_cycles:
                        break
                    if not await _wait_until(status["retry_after"], stop_path, store.check_storage_health):
                        break
                    continue
                if max_cycles is None or attempts < max_cycles:
                    if not await _wait_until(time.time() + settings["channel_poll_interval_seconds"], stop_path, store.check_storage_health):
                        break
        status['phase'] = 'write_stopped_status'
        status["status"] = "stopped"
        _write_status(status_path, status)
        return 0
    except Exception as error:
        delay = _flood_delay(error)
        code = 'telegram_rate_limited' if delay else failure_code(error)
        failed_phase = status['phase'] if status is not None else phase
        _collector_log(f'텔레그램 수집 실패: phase={failed_phase} code={code}')
        if status is not None:
            status["status"] = "rate_limited" if delay else "failed"
            status['error_code'] = code
            if delay:
                status["retry_after"] = int(time.time()) + delay + 1
            try:
                _write_status(status_path, status)
            except OSError:
                pass
        from kiwoom_autotrader.telegram.channel_store import ExternalStorageUnavailable
        if isinstance(error, ExternalStorageUnavailable):
            from kiwoom_autotrader.telegram.channel_storage import attempt_mount
            attempt_mount(state_dir)
            # Never reuse an open SQLite handle after a storage fault, even if
            # mounting succeeded. launchd can retry with a fresh connection.
            print('텔레그램 SSD 원장을 확인하지 못해 수집을 중단합니다.', flush=True)
            return 75
        if isinstance(error, CollectorError):
            raise
        raise CollectorError("텔레그램 채널 수집 연결에 실패했습니다. 계정 연결과 선택한 채널을 확인하세요.") from None
    finally:
        if client is not None:
            try:
                await client.disconnect()
            except Exception:
                pass
        if lock_fd is not None:
            os.close(lock_fd)
