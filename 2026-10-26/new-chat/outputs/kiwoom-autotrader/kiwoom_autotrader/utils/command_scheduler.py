"""Weekday command reservations with durable claims before external actions.

This module returns claimed commands to the existing dispatcher. It does not
execute commands or contact any broker, Telegram service or credential store.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime, timedelta
import fcntl
import json
import os
from pathlib import Path
import re
import tempfile
import threading
from zoneinfo import ZoneInfo

from kiwoom_autotrader.config.settings import (
    SettingsError, load_settings, save_settings, settings_path,
)


KST = ZoneInfo("Asia/Seoul")
_MAX_STATE_BYTES = 1024 * 1024
_PREFIXES = {
    "help", "/help", "/start", "status", "/status", "top", "/top",
    "channels", "/channels", "posts", "/posts", "도움말", "상태", "상위20",
    "거래대금", "채널", "채널글", "login", "srch", "buy", "sell", "report",
    "r", "mkhr", "stts", "power",
}


class CommandSchedulerError(RuntimeError):
    """Safe scheduler or persistence failure, without arbitrary command details."""


SchedulerError = CommandSchedulerError


def _localnow() -> datetime:
    return datetime.now(KST)


def normalize_time(value: str) -> str:
    if not isinstance(value, str):
        raise CommandSchedulerError("예약 시각은 24시간제 H:MM 또는 HH:MM으로 입력하세요.")
    match = re.fullmatch(r"([0-9]{1,2}):([0-9]{2})", value.strip())
    if not match or int(match[1]) > 23 or int(match[2]) > 59:
        raise CommandSchedulerError("예약 시각은 00:00~23:59 범위의 H:MM 또는 HH:MM이어야 합니다.")
    return f"{int(match[1]):02d}:{int(match[2]):02d}"


def validate_command(value: str) -> str:
    if not isinstance(value, str):
        raise CommandSchedulerError("예약할 명령어를 입력하세요.")
    command = value.strip()
    if not 1 <= len(command) <= 500 or not command.isprintable():
        raise CommandSchedulerError("예약 명령어는 한 줄, 500자 이내로 입력하세요.")
    prefix = command.split(maxsplit=1)[0].casefold()
    if prefix == "rsv":
        raise CommandSchedulerError("rsv 명령 안에 다른 rsv 명령을 예약할 수 없습니다.")
    if prefix not in _PREFIXES:
        raise CommandSchedulerError("지원되는 텔레그램 명령만 예약할 수 있습니다. help로 확인하세요.")
    try:
        # These parsers only check syntax. Name resolution, credentials and
        # broker/Telegram calls remain exclusively in the eventual dispatcher.
        if prefix == "login":
            from kiwoom_autotrader.api.session import parse_login_command
            if parse_login_command(command) is None:
                raise ValueError
        elif prefix in {"buy", "sell"}:
            from kiwoom_autotrader.telegram.commands.orders import parse_buy_command, parse_sell_command
            parser = parse_buy_command if prefix == "buy" else parse_sell_command
            if parser(command) is None:
                raise ValueError
        elif prefix == "srch":
            from kiwoom_autotrader.telegram.commands.search import parse_search_command
            if parse_search_command(command) is None:
                raise ValueError
        else:
            words = command.casefold().split()
            if prefix == "mkhr":
                if len(words) != 3 or normalize_time(words[1]) >= normalize_time(words[2]):
                    raise ValueError
            elif prefix == "stts":
                if words not in (["stts"], ["stts", "default"]):
                    raise ValueError
            elif prefix == "power":
                if words != ["power", "off"]:
                    raise ValueError
            elif len(words) != 1:
                raise ValueError
    except Exception:
        # Never persist or repeat malformed commands or their private arguments.
        raise CommandSchedulerError("예약할 명령의 형식이 올바르지 않습니다. help에서 사용법을 확인하세요.") from None
    return command


def _identity(row: dict) -> str:
    return f"{row['id']}:{row['created_at']}"


def _created(row: dict) -> datetime:
    return datetime.fromisoformat(row["created_at"]).astimezone(KST)


class CommandScheduler:
    def __init__(self, settings_file=None, state_file=None, now=_localnow):
        self.settings_file = Path(settings_file) if settings_file is not None else settings_path()
        self.state_file = Path(state_file) if state_file is not None else self.settings_file.parent / "reservation-state.json"
        self._now = now
        self.started_at = self._time()
        self._mutex = threading.RLock()
        self._blocked: dict[str, date] = {}
        self._created_identities: set[str] = set()

    def _time(self) -> datetime:
        value = self._now()
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise CommandSchedulerError("예약 시계에는 시간대가 포함되어야 합니다.")
        return value.astimezone(KST)

    @contextmanager
    def _locked(self):
        with self._mutex:
            fd = None
            try:
                self.state_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                lock_path = self.state_file.with_suffix(self.state_file.suffix + ".lock")
                fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
                os.fchmod(fd, 0o600)
                fcntl.flock(fd, fcntl.LOCK_EX)
                yield
            except OSError:
                raise CommandSchedulerError("예약 상태 잠금이나 파일을 확인하지 못했습니다.") from None
            finally:
                if fd is not None:
                    os.close(fd)

    def _settings(self) -> dict:
        try:
            return load_settings(self.settings_file)
        except SettingsError:
            raise CommandSchedulerError("예약 설정을 읽지 못했습니다. 설정 파일을 확인하세요.") from None

    def _save_settings(self, updates: dict):
        try:
            save_settings(updates, self.settings_file)
        except SettingsError:
            raise CommandSchedulerError("예약 설정의 저장 결과를 확인하지 못했습니다. rsv list로 확인하세요.") from None

    def _state(self) -> dict[str, str]:
        if not self.state_file.exists():
            return {}
        try:
            with self.state_file.open("rb") as stream:
                raw = stream.read(_MAX_STATE_BYTES + 1)
            if len(raw) > _MAX_STATE_BYTES:
                raise ValueError
            value = json.loads(raw.decode("utf-8"))
            if (not isinstance(value, dict) or set(value) != {"version", "claimed"}
                    or type(value["version"]) is not int or value["version"] != 1
                    or not isinstance(value["claimed"], dict)):
                raise ValueError
            for key, day in value["claimed"].items():
                if not isinstance(key, str):
                    raise ValueError
                match = re.fullmatch(r"([1-9][0-9]*):(.+)", key)
                if not match:
                    raise ValueError
                moment = datetime.fromisoformat(match[2])
                if moment.tzinfo is None or moment.utcoffset() is None:
                    raise ValueError
                if not isinstance(day, str) or re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", day) is None:
                    raise ValueError
                date.fromisoformat(day)
            return value["claimed"]
        except (OSError, ValueError, TypeError, UnicodeError, RecursionError):
            raise CommandSchedulerError("예약 실행 기록을 읽지 못했습니다. 안전을 위해 예약을 실행하지 않습니다.") from None

    def _save_state(self, claimed: dict[str, str]):
        temporary = None
        try:
            fd, temporary = tempfile.mkstemp(prefix=".reservation-state-", dir=self.state_file.parent)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                os.fchmod(stream.fileno(), 0o600)
                json.dump({"version": 1, "claimed": claimed}, stream, ensure_ascii=True, separators=(",", ":"))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.state_file)
            temporary = None
            directory = os.open(self.state_file.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except (OSError, ValueError, TypeError):
            raise CommandSchedulerError("예약 실행 기록을 저장하지 못해 이번 실행을 중단했습니다.") from None
        finally:
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass

    def add(self, time: str, command: str) -> dict:
        time, command = normalize_time(time), validate_command(command)
        with self._locked():
            settings = self._settings()
            rows = settings["reservations"]
            if len(rows) >= 100 or settings["next_reservation_id"] >= 10**12:
                raise CommandSchedulerError("예약을 더 등록할 수 없습니다. 기존 예약을 정리하세요.")
            created = self._time()
            row = {"id": settings["next_reservation_id"], "time": time, "command": command,
                   "created_at": created.isoformat(timespec="microseconds")}
            used = set(self._state()) | self._created_identities | {_identity(item) for item in rows}
            while _identity(row) in used:
                created += timedelta(microseconds=1)
                row["created_at"] = created.isoformat(timespec="microseconds")
            self._save_settings({"reservations": rows + [row], "next_reservation_id": row["id"] + 1})
            self._created_identities.add(_identity(row))
            return dict(row)

    def list(self) -> list[dict]:
        with self._locked():
            return [dict(row) for row in sorted(self._settings()["reservations"], key=lambda row: row["id"])]

    def remove(self, id: int) -> bool:
        if type(id) is not int or id <= 0:
            raise CommandSchedulerError("삭제할 예약 번호는 양의 정수로 입력하세요.")
        with self._locked():
            settings = self._settings()
            rows = settings["reservations"]
            kept = [row for row in rows if row["id"] != id]
            if len(kept) == len(rows):
                return False
            self._save_settings({"reservations": kept})
            return True

    def _last_day(self, row: dict, claimed: dict) -> date | None:
        key = _identity(row)
        saved = date.fromisoformat(claimed[key]) if key in claimed else None
        blocked = self._blocked.get(key)
        return max(saved, blocked) if saved is not None and blocked is not None else saved or blocked

    def _due_target(self, row: dict, now: datetime, claimed: dict) -> datetime | None:
        hour, minute = map(int, row["time"].split(":"))
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        last = self._last_day(row, claimed)
        if (now.weekday() >= 5 or target <= self.started_at or target <= _created(row)
                or target > now or (last is not None and last >= now.date())):
            return None
        return target

    def claim_due(self) -> dict | None:
        with self._locked():
            rows, claimed, now = self._settings()["reservations"], self._state(), self._time()
            due = [(target, row["id"], row) for row in rows
                   if (target := self._due_target(row, now, claimed)) is not None]
            if not due:
                return None
            _, _, row = min(due, key=lambda item: (item[0], item[1]))
            key = _identity(row)
            # Block in this process before attempting storage. Any uncertain
            # write outcome must never make this same command eligible again.
            self._blocked[key] = now.date()
            validate_command(row["command"])
            active = {_identity(item) for item in rows}
            updated = {key: day for key, day in claimed.items() if key in active}
            updated[key] = now.date().isoformat()
            self._save_state(updated)
            return dict(row)

    def seconds_until_next(self) -> float | None:
        with self._locked():
            rows, claimed, now = self._settings()["reservations"], self._state(), self._time()
            if not rows:
                return None
            candidates = []
            for row in rows:
                if self._due_target(row, now, claimed) is not None:
                    return 0.0
                created, last = _created(row), self._last_day(row, claimed)
                day = max(now.date(), self.started_at.date(), created.date())
                if last is not None:
                    day = max(day, last + timedelta(days=1))
                hour, minute = map(int, row["time"].split(":"))
                target = datetime(day.year, day.month, day.day, hour, minute, tzinfo=KST)
                while (target.weekday() >= 5 or target <= self.started_at
                       or target <= created or target < now):
                    target += timedelta(days=1)
                candidates.append((target - now).total_seconds())
            return min(candidates)
