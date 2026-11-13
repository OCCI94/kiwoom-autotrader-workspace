"""Persist ordinary settings without storing API credentials or tokens."""

import json
import os
from pathlib import Path
import re
import tempfile
from datetime import datetime


class SettingsError(RuntimeError):
    pass


def settings_path() -> Path:
    return Path.home() / "Library" / "Application Support" / "KiwoomAutotrader" / "config" / "data" / "settings.json"


def _validate(value):
    if (not isinstance(value, dict) or set(value) != {
        "version", "login_renewal_time", "timezone", "channel_poll_interval_seconds", "daily_message_export",
        "market_start_time", "market_end_time", "reservations", "next_reservation_id",
    } or type(value["version"]) is not int or value["version"] != 1
        or not isinstance(value["login_renewal_time"], str)
        or re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", value["login_renewal_time"]) is None
        or value["timezone"] != "Asia/Seoul"
        or type(value["channel_poll_interval_seconds"]) is not int
        or not 30 <= value["channel_poll_interval_seconds"] <= 86400
        or type(value["daily_message_export"]) is not bool):
        raise SettingsError("프로그램 일반 설정 형식을 확인하세요.")
    for key in ("market_start_time", "market_end_time"):
        if not isinstance(value[key], str) or re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", value[key]) is None:
            raise SettingsError("장 시간은 HH:MM 형식이어야 합니다.")
    if value["market_start_time"] >= value["market_end_time"]:
        raise SettingsError("장 시작 시각은 종료 시각보다 빨라야 합니다.")
    if (type(value["next_reservation_id"]) is not int or not 1 <= value["next_reservation_id"] <= 10**12
            or not isinstance(value["reservations"], list) or len(value["reservations"]) > 100):
        raise SettingsError("예약 설정 형식을 확인하세요.")
    seen = set()
    for row in value["reservations"]:
        if (not isinstance(row, dict) or set(row) != {"id", "time", "command", "created_at"}
                or type(row["id"]) is not int or not 0 < row["id"] < value["next_reservation_id"]
                or row["id"] in seen or not isinstance(row["time"], str)
                or re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", row["time"]) is None
                or not isinstance(row["command"], str) or not 1 <= len(row["command"]) <= 500
                or not row["command"].isprintable() or not isinstance(row["created_at"], str)
                or not 1 <= len(row["created_at"]) <= 40):
            raise SettingsError("예약 설정 형식을 확인하세요.")
        try:
            moment = datetime.fromisoformat(row["created_at"])
            if moment.tzinfo is None or moment.utcoffset() is None:
                raise ValueError
        except ValueError:
            raise SettingsError("예약 등록 시각을 확인하세요.") from None
        seen.add(row["id"])
    return value


def default_settings() -> dict:
    try:
        return _validate(json.loads((Path(__file__).parent / "data" / "settings-default.json").read_text()))
    except (OSError, ValueError, TypeError):
        raise SettingsError("프로그램 기본 설정을 읽지 못했습니다.") from None


def load_settings(path: Path | None = None) -> dict:
    try:
        defaults = default_settings()
        path = path or settings_path()
        overrides = json.loads(path.read_text()) if path.exists() else {}
        if not isinstance(overrides, dict) or set(overrides) - set(defaults):
            raise SettingsError("일반 설정에는 지원되는 항목만 저장할 수 있습니다.")
        return _validate({**defaults, **overrides})
    except (OSError, ValueError, TypeError, RecursionError):
        raise SettingsError("프로그램 일반 설정을 읽지 못했습니다.") from None


def _write_settings(settings: dict, path: Path) -> dict:
    temporary = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, temporary = tempfile.mkstemp(prefix=".settings-", dir=path.parent)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(settings, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        return settings
    except OSError:
        raise SettingsError("설정 저장 완료를 확인하지 못했습니다. stts로 현재 설정을 확인하세요.") from None
    finally:
        if temporary is not None:
            try:
                Path(temporary).unlink(missing_ok=True)
            except OSError:
                pass


def save_settings(updates: dict, path: Path | None = None) -> dict:
    path = path or settings_path()
    if not isinstance(updates, dict):
        raise SettingsError("설정 변경 내용을 확인하세요.")
    return _write_settings(_validate({**load_settings(path), **updates}), path)


def reset_settings(path: Path | None = None) -> dict:
    return _write_settings(default_settings(), path or settings_path())


def save_renewal_time(value: str, path: Path | None = None) -> None:
    save_settings({"login_renewal_time": value}, path)
