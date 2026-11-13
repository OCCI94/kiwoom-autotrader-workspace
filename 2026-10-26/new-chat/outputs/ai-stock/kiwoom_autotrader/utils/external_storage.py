"""Opt-in path validation for a future external data layout.

This module is deliberately not wired into an active service or order path.
Callers must explicitly enable it and call ``require_available`` before every
use; path resolution alone is not an order guard or crash-recovery protocol.
It never creates a mount point, database, directory or fallback data root.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os
import plistlib
import re
import subprocess
import sys


class ExternalStorageUnavailable(RuntimeError):
    """The configured external data volume cannot safely be used."""


def _volume_info(path: Path) -> dict:
    if sys.platform != 'darwin':
        raise ExternalStorageUnavailable('external APFS storage requires macOS')
    try:
        result = subprocess.run(['/usr/sbin/diskutil', 'info', '-plist', str(path)],
                                capture_output=True, timeout=5, check=True)
        value = plistlib.loads(result.stdout)
        if not isinstance(value, dict):
            raise ValueError('invalid plist')
        return value
    except (OSError, ValueError, plistlib.InvalidFileException,
            subprocess.SubprocessError):
        raise ExternalStorageUnavailable('cannot verify external data volume') from None


def _existing_directory(path: Path) -> Path:
    # Refuse symlinks and relative paths; resolve must not silently redirect to
    # another volume or manufacture a missing mount directory.
    if not path.is_absolute():
        raise ExternalStorageUnavailable('absolute storage path required')
    try:
        if path.resolve(strict=True) != path or not path.is_dir():
            raise ExternalStorageUnavailable('storage directory unavailable or redirected')
        return path
    except (OSError, RuntimeError):
        raise ExternalStorageUnavailable('storage directory unavailable') from None


@dataclass(frozen=True)
class ExternalDataPaths:
    """Derived paths only; existing consumers are not yet redirected here."""
    engine_root: Path
    control_root: Path
    data_root: Path

    @property
    def execution_logs(self) -> Path:
        return self.data_root / 'logs'

    @property
    def settings(self) -> Path:
        return self.data_root / 'config' / 'data' / 'settings.json'

    @property
    def telegram_account(self) -> Path:
        return self.data_root / 'telegram-account'

    @property
    def research(self) -> Path:
        return self.data_root / 'research'


class ExternalDataRoot:
    """Recheck an existing APFS mount and data-root identity on every use.

    Recreating a directory, changing the volume UUID or losing a mount fails
    closed rather than falling back to a fresh internal data tree. A failed
    check permanently latches this instance; reconnecting the drive alone
    cannot rearm it. This check cannot make a later write or order transmission
    atomic with device removal; broker reconciliation and an order gate are
    still required.
    """

    def __init__(self, *, mount: Path, data_root: Path, volume_uuid: str,
                 engine_root: Path, control_root: Path,
                 _inspector=None, _is_mount=None):
        self.mount = Path(mount)
        self.data_root = Path(data_root)
        self.engine_root = Path(engine_root)
        self.control_root = Path(control_root)
        if (not isinstance(volume_uuid, str) or
                not re.fullmatch(r'[0-9A-Fa-f]{8}(?:-[0-9A-Fa-f]{4}){3}-[0-9A-Fa-f]{12}', volume_uuid)):
            raise ExternalStorageUnavailable('volume UUID required')
        self.volume_uuid = volume_uuid.upper()
        self._inspector = _inspector or _volume_info
        self._is_mount = _is_mount or os.path.ismount
        self._identity = None
        self._failed = False
        self.require_available()

    def require_available(self) -> ExternalDataPaths:
        if self._failed:
            raise ExternalStorageUnavailable('external storage failure is latched')
        try:
            return self._validate_available()
        except ExternalStorageUnavailable:
            self._failed = True
            raise
        except (OSError, ValueError, TypeError):
            self._failed = True
            raise ExternalStorageUnavailable('cannot verify external data volume') from None

    def _validate_available(self) -> ExternalDataPaths:
        mount = _existing_directory(self.mount)
        root = _existing_directory(self.data_root)
        if mount == root or mount not in root.parents:
            raise ExternalStorageUnavailable('data root must be inside the external mount')
        if not self.engine_root.is_absolute() or not self.control_root.is_absolute():
            raise ExternalStorageUnavailable('internal paths must be absolute')
        if (mount == self.engine_root or mount == self.control_root
                or mount in self.engine_root.parents or mount in self.control_root.parents):
            raise ExternalStorageUnavailable('engine and controls must remain internal')
        if not self._is_mount(str(mount)):
            raise ExternalStorageUnavailable('external volume is not mounted')
        try:
            info = self._inspector(mount)
            mount_stat, root_stat = mount.stat(), root.stat()
            if (info.get('VolumeUUID', '').upper() != self.volume_uuid
                    or info.get('MountPoint') != str(mount)
                    or info.get('FilesystemType', '').lower() != 'apfs'
                    or info.get('WritableVolume') is not True
                    or mount_stat.st_dev != root_stat.st_dev):
                raise ExternalStorageUnavailable('wrong or unavailable external volume')
            current = (mount_stat.st_dev, mount_stat.st_ino, root_stat.st_dev, root_stat.st_ino)
        except (OSError, AttributeError, TypeError):
            raise ExternalStorageUnavailable('cannot verify external data volume') from None
        if self._identity is not None and self._identity != current:
            raise ExternalStorageUnavailable('external storage identity changed')
        self._identity = current
        return ExternalDataPaths(self.engine_root, self.control_root, root)
