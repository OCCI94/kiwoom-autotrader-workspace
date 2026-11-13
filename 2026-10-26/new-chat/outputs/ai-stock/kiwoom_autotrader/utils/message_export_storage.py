"""Daily-export writes anchored to the collector's selected external archive.

The collector's ExternalArchive verifies its local marker, backing image, host
and volume UUID. This adapter reuses that decision and keeps directory FDs open
for writes, so losing the SSD cannot recreate a local /Volumes directory tree.
It never mounts storage, authenticates, or selects an alternative data root.
"""
from __future__ import annotations

import os
from pathlib import Path
import stat

from .anchored_dataset import AnchoredDataset


class GuardedMessageExport(AnchoredDataset):
    """Use the collector's daily file operations at a checked SSD destination."""

    def __init__(self, store, root: Path, guard):
        self.store = store
        self.fd = None
        self.mount_fd = None
        self.root = Path(root)
        archive = getattr(store, '_external_archive', None)
        if archive is None or self.root == archive.mount or not self.root.is_relative_to(archive.mount):
            raise ValueError('external message export location unavailable')
        if any(part in ('', '.', '..') for part in self.root.relative_to(archive.mount).parts):
            raise ValueError('invalid external message export path')
        self.archive = archive
        self._source_guard = guard
        self.guard = self._guard
        parent = None
        try:
            # Use the exact mount/backing/UUID decision that the collector uses,
            # rather than a generic externally configured storage root.
            archive.verify()
            guard()
            flags = os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0) | os.O_NOFOLLOW
            self.mount_fd = os.open(archive.mount, flags)
            opened = os.fstat(self.mount_fd)
            if not stat.S_ISDIR(opened.st_mode) or opened.st_dev == os.stat(archive.mount.parent).st_dev:
                raise ValueError('external message export volume unavailable')
            self.mount_identity = (opened.st_dev, opened.st_ino)
            self._guard()
            parent = os.dup(self.mount_fd)
            for part in self.root.relative_to(archive.mount).parts:
                if part in ('', '.', '..'):
                    raise ValueError('invalid external message export path')
                self._guard()
                try:
                    os.mkdir(part, 0o700, dir_fd=parent)
                except FileExistsError:
                    pass
                self._guard()
                child = os.open(part, flags, dir_fd=parent)
                try:
                    child_info = os.fstat(child)
                    if child_info.st_dev != self.mount_identity[0] or child_info.st_uid != os.getuid():
                        raise ValueError('external message export destination device or owner changed')
                    self._guard()
                    os.fchmod(child, 0o700)
                except Exception:
                    os.close(child)
                    raise
                os.close(parent)
                parent = child
            self.fd, parent = parent, None
            self._guard()
        except Exception:
            self.close()
            raise
        finally:
            if parent is not None:
                os.close(parent)

    def _guard(self):
        self._source_guard()
        opened = os.fstat(self.mount_fd)
        current = os.stat(self.archive.mount, follow_symlinks=False)
        if (not os.path.ismount(self.archive.mount)
                or (opened.st_dev, opened.st_ino) != self.mount_identity
                or (current.st_dev, current.st_ino) != self.mount_identity):
            raise ValueError('external message export mount changed')
        sentinel = os.open('.ssd-volume-sentinel', os.O_RDONLY | os.O_NOFOLLOW, dir_fd=self.mount_fd)
        try:
            info = os.fstat(sentinel)
            if (not stat.S_ISREG(info.st_mode) or info.st_size > 128
                    or os.read(sentinel, 129).decode('ascii').strip() != self.archive.config['volume_uuid']):
                raise ValueError('external message export sentinel changed')
        finally:
            os.close(sentinel)
        if self.fd is not None:
            actual = os.stat(self.root, follow_symlinks=False)
            anchored = os.fstat(self.fd)
            if not stat.S_ISDIR(actual.st_mode) or (actual.st_dev, actual.st_ino) != (anchored.st_dev, anchored.st_ino):
                raise ValueError('external message export destination changed')

    def close(self):
        super().close()
        if self.mount_fd is not None:
            os.close(self.mount_fd)
            self.mount_fd = None
