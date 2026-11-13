"""Per-user macOS login service. No secrets enter files or launchctl arguments."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import os
from pathlib import Path
from kiwoom_autotrader.utils.paths import project_root
import plistlib
import re
import subprocess
import sys
import tempfile
import time


LABEL = "com.local.kiwoom-autotrader.telegram"


class TelegramServiceError(RuntimeError):
    """Safe service-management failure."""


@dataclass(frozen=True)
class ServicePaths:
    home: Path

    @property
    def base(self) -> Path:
        return self.home / "Library" / "Application Support" / "KiwoomAutotrader"

    @property
    def runtime(self) -> Path:
        return self.base / "runtime"

    @property
    def state(self) -> Path:
        return self.base / "telegram"

    @property
    def plist(self) -> Path:
        return self.home / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def service_paths() -> ServicePaths:
    return ServicePaths(Path.home())


def _target() -> str:
    return f"gui/{os.getuid()}/{LABEL}"


def _launchctl(*arguments: str, check: bool = True) -> subprocess.CompletedProcess:
    if sys.platform != "darwin":
        raise TelegramServiceError("로그인 자동 실행은 macOS에서 지원합니다.")
    try:
        result = subprocess.run(
            ["/bin/launchctl", *arguments], capture_output=True, text=True,
            timeout=30, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        raise TelegramServiceError("macOS 자동 실행 서비스에 접근하지 못했습니다.") from None
    if check and result.returncode:
        raise TelegramServiceError("macOS 자동 실행 서비스를 변경하지 못했습니다.")
    return result


def service_status(paths: ServicePaths | None = None) -> dict:
    paths = paths or service_paths()
    result = _launchctl("print", _target(), check=False)
    pid = re.search(r"^\s*pid = ([0-9]+)\s*$", result.stdout, re.MULTILINE)
    running = re.search(r"^\s*state = running\s*$", result.stdout, re.MULTILINE)
    return {
        "installed": paths.plist.is_file(),
        "loaded": result.returncode == 0,
        "running": result.returncode == 0 and bool(running and pid),
        "pid": int(pid.group(1)) if result.returncode == 0 and pid else None,
    }


def build_plist(paths: ServicePaths, executable: Path) -> dict:
    return {
        "Label": LABEL,
        "ProgramArguments": [
            str(executable.absolute()), "-u", "-m", "kiwoom_autotrader",
            "telegram-listen", "--state-dir", str(paths.state),
        ],
        "WorkingDirectory": str(paths.runtime),
        "LimitLoadToSessionType": "Aqua",
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": 30,
        "ProcessType": "Background",
        "Umask": 0o077,
        "StandardOutPath": str(paths.state / "listener.log"),
        "StandardErrorPath": str(paths.state / "listener.log"),
    }


def _private_write(path: Path, content: bytes) -> None:
    fd, temporary = tempfile.mkstemp(prefix=".install-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


@contextmanager
def _stopped_directory(state: Path, timeout: float = 120):
    """Request graceful stop and hold the exact listener lock during migration."""
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(state / "listener.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    deadline = time.monotonic() + timeout
    requested = False
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if not requested:
                    _private_write(state / "stop.request", b"")
                    requested = True
                if time.monotonic() >= deadline:
                    raise TelegramServiceError("기존 텔레그램 조회가 끝나지 않아 자동 실행 전환을 중단했습니다.")
                time.sleep(0.2)
        yield
    finally:
        os.close(fd)


def stop_service(paths: ServicePaths | None = None) -> None:
    paths = paths or service_paths()
    with _stopped_directory(paths.state):
        if service_status(paths)["loaded"]:
            _launchctl("bootout", _target())


def start_service(paths: ServicePaths | None = None) -> None:
    paths = paths or service_paths()
    if not paths.plist.is_file():
        raise TelegramServiceError("자동 실행 설정이 없습니다. 먼저 자동 실행 설치를 실행하세요.")
    _launchctl("enable", _target())
    if not service_status(paths)["loaded"]:
        _launchctl("bootstrap", f"gui/{os.getuid()}", str(paths.plist))
    else:
        _launchctl("kickstart", _target())


def install_service(source: Path | None = None, paths: ServicePaths | None = None) -> None:
    """Install a private source snapshot outside Documents, then launch it."""
    from kiwoom_autotrader.telegram.bot_credentials import load_telegram_config

    if sys.platform != "darwin":
        raise TelegramServiceError("로그인 자동 실행은 macOS에서 지원합니다.")
    if load_telegram_config() is None:
        raise TelegramServiceError("텔레그램 개인 채팅 연결 설정을 먼저 완료하세요.")
    source = source or project_root()
    paths = paths or service_paths()
    if source.resolve() == paths.runtime.resolve():
        raise TelegramServiceError("프로젝트 폴더에서 자동 실행 설치를 실행하세요.")
    package = source / "kiwoom_autotrader"
    if not (package / "__main__.py").is_file():
        raise TelegramServiceError("설치할 프로그램 파일을 찾지 못했습니다.")
    for folder in (paths.base, paths.state):
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        folder.chmod(0o700)
    paths.plist.parent.mkdir(parents=True, exist_ok=True)
    if paths.plist.exists():
        try:
            existing = plistlib.loads(paths.plist.read_bytes())
            if existing.get("Label") != LABEL:
                raise ValueError
        except (ValueError, plistlib.InvalidFileException, AttributeError):
            raise TelegramServiceError("동일한 이름의 다른 자동 실행 설정이 있어 설치를 중단했습니다.") from None
    # Stop both the old login service and the original manually started listener.
    stop_service(paths)
    with _stopped_directory(source / "runs" / "telegram"):
        with _stopped_directory(paths.state):
            previous = source / "runs" / "telegram" / "state.json"
            if previous.is_file() and not (paths.state / "state.json").exists():
                data = previous.read_bytes()
                if len(data) > 16 * 1024:
                    raise TelegramServiceError("기존 수신 상태 파일을 확인할 수 없습니다.")
                _private_write(paths.state / "state.json", data)
            # Deploy Python code and the two named settings resources only.
            # Keys, logs and project output are excluded.
            with tempfile.TemporaryDirectory(prefix=".runtime-", dir=paths.base) as temp:
                staged = Path(temp) / "kiwoom_autotrader"
                staged.mkdir(mode=0o700)
                for file in package.rglob("*.py"):
                    relative = file.relative_to(package)
                    if "__pycache__" in relative.parts:
                        continue
                    if file.is_symlink():
                        raise TelegramServiceError("실행 소스에 심볼릭 링크가 있어 설치를 중단했습니다.")
                    target = staged / relative
                    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    _private_write(target, file.read_bytes())
                for name in ("settings-default.json", "settings.json"):
                    resource = package / "config" / "data" / name
                    if resource.is_file() and not resource.is_symlink():
                        target = staged / "config" / "data" / name
                        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                        _private_write(target, resource.read_bytes())
                paths.runtime.mkdir(exist_ok=True, mode=0o700)
                destination = paths.runtime / "kiwoom_autotrader"
                backup = Path(temp) / "previous-package"
                if destination.exists():
                    destination.rename(backup)
                try:
                    staged.rename(destination)
                except OSError:
                    if backup.exists():
                        backup.rename(destination)
                    raise
            log = paths.state / "listener.log"
            fd = os.open(log, os.O_APPEND | os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
            os.fchmod(fd, 0o600)
            os.close(fd)
            _private_write(paths.plist, plistlib.dumps(build_plist(paths, Path(sys.executable))))
    start_service(paths)


def uninstall_service(paths: ServicePaths | None = None) -> None:
    paths = paths or service_paths()
    stop_service(paths)
    paths.plist.unlink(missing_ok=True)


def default_state_directory(project: Path) -> Path:
    paths = service_paths()
    return paths.state if paths.plist.is_file() else project / "runs" / "telegram"
