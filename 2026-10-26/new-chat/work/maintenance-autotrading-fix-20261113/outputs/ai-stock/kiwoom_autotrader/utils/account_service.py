"""Login service for the separate read-only personal-account collector."""

from contextlib import contextmanager
from kiwoom_autotrader.utils.platform_io import fcntl
from kiwoom_autotrader.utils.platform_io import os
from pathlib import Path
import plistlib
import re
import sys
import time
from kiwoom_autotrader.utils.control_audit import registered_mutator,audit_control

from kiwoom_autotrader.utils.telegram_service import (
    ServicePaths, TelegramServiceError, _launchctl, _private_write, build_plist,
    install_service,
)

LABEL = "com.local.kiwoom-autotrader.telegram-account"


class AccountServicePaths(ServicePaths):
    @property
    def state(self) -> Path:
        return self.base / "telegram-account"

    @property
    def plist(self) -> Path:
        return self.home / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def account_paths() -> AccountServicePaths:
    return AccountServicePaths(Path.home())


def _target() -> str:
    return f"gui/{os.getuid()}/{LABEL}"


def collector_status(paths=None) -> dict:
    paths = paths or account_paths()
    result = _launchctl("print", _target(), check=False)
    pid = re.search(r"^\s*pid = ([0-9]+)\s*$", result.stdout, re.MULTILINE)
    running = re.search(r"^\s*state = running\s*$", result.stdout, re.MULTILINE)
    return {"installed": paths.plist.is_file(), "loaded": result.returncode == 0,
            "running": result.returncode == 0 and bool(running and pid),
            "pid": int(pid.group(1)) if result.returncode == 0 and pid else None}


@contextmanager
def _stopped_collector(paths):
    paths.state.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(paths.state / "collector.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    deadline = time.monotonic() + 120
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                _private_write(paths.state / "collector-stop.request", b"")
                if time.monotonic() > deadline:
                    raise TelegramServiceError("채널 수집 종료를 기다리고 있습니다. 잠시 후 다시 실행하세요.")
                time.sleep(0.5)
        yield
    finally:
        os.close(fd)


@registered_mutator('stop','collector',paths_factory=account_paths,error_type=TelegramServiceError)
def stop_collector_service(paths=None):
    paths = paths or account_paths()
    with _stopped_collector(paths):
        if sys.platform == "darwin" and collector_status(paths)["loaded"]:
            _launchctl("bootout", _target())


@registered_mutator('start','collector',paths_factory=account_paths,error_type=TelegramServiceError)
def start_collector_service(paths=None):
    paths = paths or account_paths()
    if not paths.plist.is_file():
        raise TelegramServiceError("개인 계정 연결과 채널 선택을 먼저 완료하세요.")
    _launchctl("enable", _target())
    if not collector_status(paths)["loaded"]:
        _launchctl("bootstrap", f"gui/{os.getuid()}", str(paths.plist))
    else:
        _launchctl("kickstart", _target())


def install_collector_service(source=None, paths=None, *, before_replace=None, before_mutation=None):
    target=paths or account_paths()
    managed=(target.runtime/'kiwoom_autotrader').exists() or (target.base/'telegram'/'state.json').exists()
    if managed and not callable(before_replace):
        raise TelegramServiceError('기존 실행 코드 교체에는 별도 코드 배포 승인이 필요합니다.')
    permission=before_mutation or before_replace
    if permission is not None:
        if not callable(permission):raise TelegramServiceError('코드 배포 승인 검증기가 필요합니다.')
        permission()
    with audit_control('install','collector',root=target.base,paths=(target.plist,)):
        return _install_collector_service(source,paths,before_replace=before_replace,before_mutation=before_mutation)


def _install_collector_service(source=None, paths=None, *, before_replace=None, before_mutation=None):
    from kiwoom_autotrader.telegram.channel_store import ChannelStore
    from kiwoom_autotrader.telegram.account_credentials import load_account_config

    paths = paths or account_paths()
    config = load_account_config()
    if config is None:
        raise TelegramServiceError("텔레그램 개인 계정 연결을 먼저 완료하세요.")
    # Older collectors cannot write the new immutable version ledger. Stop them
    # before opening the archive because ChannelStore may migrate its schema.
    stop_collector_service(paths)
    with ChannelStore(paths.state, config.user_id) as store:
        if not store.channels():
            raise TelegramServiceError("수집할 채널을 먼저 선택하세요.")
    # Shared code snapshot is deployed by the existing, tested installer.
    if before_replace is None and before_mutation is None:
        install_service(source=source)
    else:
        install_service(source=source,before_replace=before_replace,before_mutation=before_mutation)
    paths.plist.parent.mkdir(parents=True, exist_ok=True)
    definition = build_plist(paths, Path(sys.executable))
    definition["Label"] = LABEL
    definition["ProgramArguments"][4] = "telegram-account-listen"
    definition["StandardOutPath"] = str(paths.state / "collector.log")
    definition["StandardErrorPath"] = str(paths.state / "collector.log")
    fd = os.open(paths.state / "collector.log", os.O_APPEND | os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    os.fchmod(fd, 0o600)
    os.close(fd)
    _private_write(paths.plist, plistlib.dumps(definition))
    start_collector_service(paths)


@registered_mutator('uninstall','collector',paths_factory=account_paths,error_type=TelegramServiceError)
def remove_collector_service(paths=None):
    paths = paths or account_paths()
    stop_collector_service(paths)
    paths.plist.unlink(missing_ok=True)
