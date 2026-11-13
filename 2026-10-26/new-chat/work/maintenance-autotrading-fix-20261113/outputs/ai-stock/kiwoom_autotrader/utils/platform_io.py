"""Small platform boundary for foreground Windows and POSIX file operations.

Windows uses its user-profile ACLs; Unix mode bits are not an ACL substitute.
Secrets are stored in the OS credential vault, never in these state files.
"""
import os as _os
import sys
import time


class _OS:
    def __getattr__(self, name):
        if name == 'O_NOFOLLOW':
            return getattr(_os, name, 0)
        if name == 'getuid':
            return getattr(_os, name, lambda: 0)
        if name == 'fchmod':
            return getattr(_os, name, lambda fd, mode: None)
        return getattr(_os, name)


os = _OS()

try:
    import fcntl
except ImportError:
    import msvcrt

    class _FileLock:
        LOCK_SH, LOCK_EX, LOCK_NB, LOCK_UN = 1, 2, 4, 8

        @staticmethod
        def flock(fd, operation):
            current = _os.lseek(fd, 0, _os.SEEK_CUR)
            try:
                _os.lseek(fd, 0, _os.SEEK_SET)
                if operation & _FileLock.LOCK_UN:
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                    return
                while True:
                    try:
                        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                        return
                    except OSError as error:
                        if getattr(error, 'winerror', None) not in (33, 36) and error.errno not in (13, 36):
                            raise
                        if operation & _FileLock.LOCK_NB:
                            raise BlockingIOError('State file is locked') from None
                        time.sleep(0.05)
            finally:
                _os.lseek(fd, current, _os.SEEK_SET)

    fcntl = _FileLock()


def sync_directory(path):
    # Windows does not expose directory fsync through os.open. Atomic replace
    # and fsync of the data file are still used by the persistence callers.
    if sys.platform == 'win32':
        return
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
