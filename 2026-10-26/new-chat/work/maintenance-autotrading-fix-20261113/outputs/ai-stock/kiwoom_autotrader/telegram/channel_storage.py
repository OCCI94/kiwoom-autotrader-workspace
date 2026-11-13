"""Optional, fail-closed external location for the shared Telegram archive.

The selection marker lives on the local state volume, so disappearance of the
external disk cannot silently restore the local archive. The old local file is
never modified when the marker is selected. This module performs no mounting.
"""
from __future__ import annotations

import json
from kiwoom_autotrader.utils.platform_io import os
from pathlib import Path
import plistlib
import sqlite3
import stat
import subprocess

MARKER = 'channel-storage.json'
SENTINEL = '.ssd-volume-sentinel'

class ExternalArchiveError(RuntimeError):
    pass


def _mount_identity(mount: Path, expected_uuid: str) -> None:
    """Reject a missing mount and a same-named directory on the host disk."""
    try:
        if (not os.path.ismount(str(mount)) or
                os.stat(mount).st_dev == os.stat(mount.parent).st_dev):
            raise ValueError
        sentinel = mount / SENTINEL
        fd = os.open(sentinel, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or st.st_size > 128:
                raise ValueError
            contents = os.read(fd, 129).decode('ascii').strip()
        finally:
            os.close(fd)
        if contents != expected_uuid:
            raise ValueError
    except (OSError, ValueError, UnicodeError):
        raise ExternalArchiveError('external archive mount or sentinel unavailable') from None


def attempt_mount(state_dir: Path) -> bool:
    """One bounded mount attempt, with no directory or database creation."""
    try:
        selected_marker = _marker(Path(state_dir) / MARKER)
        if selected_marker is None:
            return False
        config, _ = selected_marker
        # A UUID mount works for an attached but unmounted APFS volume.
        subprocess.run(['/usr/sbin/diskutil', 'mount', config['volume_uuid']],
                       capture_output=True, timeout=30, check=False)
        _mount_identity(Path(config['mount']), config['volume_uuid'])
        return True
    except (ExternalArchiveError, OSError, subprocess.SubprocessError):
        return False


def _volume(path: Path) -> dict:
    try:
        proc = subprocess.run(['/usr/sbin/diskutil', 'info', '-plist', str(path)],
                              capture_output=True, timeout=8, check=True)
        value = plistlib.loads(proc.stdout)
        if not isinstance(value, dict):
            raise ValueError
        return value
    except (OSError, ValueError, subprocess.SubprocessError, plistlib.InvalidFileException):
        raise ExternalArchiveError('external archive volume identity unavailable') from None


def _image_info() -> dict:
    try:
        proc = subprocess.run(['/usr/bin/hdiutil', 'info', '-plist'],
                              capture_output=True, timeout=8, check=True)
        value = plistlib.loads(proc.stdout)
        if not isinstance(value, dict):
            raise ValueError
        return value
    except (OSError, ValueError, subprocess.SubprocessError, plistlib.InvalidFileException):
        raise ExternalArchiveError('external archive image identity unavailable') from None


def _marker(path: Path) -> tuple[dict, bytes] | None:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    except OSError:
        raise ExternalArchiveError('invalid external archive configuration') from None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or st.st_size > 8192:
            raise ValueError
        raw = os.read(fd, 8193)
        value = json.loads(raw)
        if not isinstance(value, dict) or set(value) != {'version','mount','volume_uuid','directory','host','host_uuid','image'} or value['version'] != 1:
            raise ValueError
        if not all(isinstance(value[k],str) and value[k] for k in ('mount','volume_uuid','directory','host','host_uuid','image')):
            raise ValueError
        mount, directory = Path(value['mount']), Path(value['directory'])
        host, image = Path(value['host']), Path(value['image'])
        if (not mount.is_absolute() or not directory.is_absolute() or directory.parent != mount
            or not host.is_absolute() or not image.is_absolute() or not image.is_relative_to(host)):
            raise ValueError
        return value, raw
    except (OSError, ValueError, TypeError, UnicodeError, json.JSONDecodeError):
        raise ExternalArchiveError('invalid external archive configuration') from None
    finally:
        os.close(fd)


class ExternalArchive:
    def __init__(self, state_dir: Path, filename: str):
        self.marker = Path(state_dir) / MARKER
        marker = _marker(self.marker)
        if marker is None:
            raise ExternalArchiveError('external archive configuration missing')
        self.config, self.marker_bytes = marker
        self.mount = Path(self.config['mount'])
        self.directory = Path(self.config['directory'])
        self.path = self.directory / filename
        self.identity = None
        self.verify()

    def verify(self) -> None:
        if _marker(self.marker) != (self.config, self.marker_bytes):
            raise ExternalArchiveError('external archive configuration changed')
        _mount_identity(self.mount, self.config['volume_uuid'])
        try:
            host = Path(self.config['host'])
            image = Path(self.config['image'])
            host_info = _volume(host)
            if (host_info.get('MountPoint') != str(host) or host_info.get('VolumeUUID') != self.config['host_uuid']
                or host_info.get('WritableVolume') is not True):
                raise ValueError
            current = host
            for part in image.relative_to(host).parts:
                current = current / part
                if current.is_symlink():
                    raise ValueError
            if not image.exists():
                raise ValueError
            images = _image_info().get('images', [])
            attached = [x.get('image-path') for x in images if isinstance(x, dict)
                        and any(isinstance(e, dict) and e.get('mount-point') == str(self.mount)
                                for e in x.get('system-entities', []))]
            if attached != [str(image)]:
                raise ValueError
            info = _volume(self.mount)
            if (info.get('MountPoint') != str(self.mount) or
                info.get('VolumeUUID') != self.config['volume_uuid'] or
                info.get('WritableVolume') is not True):
                raise ValueError
            for path in (self.mount, self.directory):
                st = path.lstat()
                if not stat.S_ISDIR(st.st_mode) or path.is_symlink():
                    raise ValueError
            st = self.path.lstat()
            if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid():
                raise ValueError
            if st.st_dev != self.mount.stat().st_dev or st.st_dev != self.directory.stat().st_dev:
                raise ValueError
            identity = (st.st_dev, st.st_ino)
            if self.identity is None:
                self.identity = identity
            elif identity != self.identity:
                raise ValueError
        except (OSError, ValueError, TypeError):
            raise ExternalArchiveError('external archive unavailable or changed') from None

    def quick_verify(self) -> None:
        # During poll sleeps, detect detach/replacement without starting diskutil
        # every second. A full UUID/backing check still precedes each DB access.
        try:
            if _marker(self.marker) != (self.config, self.marker_bytes):
                raise ValueError
            _mount_identity(self.mount, self.config['volume_uuid'])
            st = self.path.lstat()
            if (not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or
                (st.st_dev, st.st_ino) != self.identity or st.st_dev != self.mount.stat().st_dev):
                raise ValueError
        except (OSError, ValueError, TypeError):
            raise ExternalArchiveError('external archive unavailable or changed') from None

    def connect(self) -> sqlite3.Connection:
        self.verify()
        # mode=rw is essential: a missing database must never become an empty one.
        db = sqlite3.connect(self.path.as_uri() + '?mode=rw', uri=True, timeout=5)
        try:
            self.verify()
        except Exception:
            db.close()
            raise
        return db


def selected(state_dir: Path) -> bool:
    return _marker(Path(state_dir) / MARKER) is not None
