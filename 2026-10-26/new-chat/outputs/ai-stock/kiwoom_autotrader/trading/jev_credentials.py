"""Bounded local Jev credential loader; never logs, writes or sends credentials.

An environment key takes precedence. Otherwise only the fixed application
secret file is considered. No configuration/CLI path can redirect this loader.
POSIX file loading is fail closed: owned regular0600, no symlink ancestors,
directory-FD traversal and a stable bounded read. No credential is cached here.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import stat

from kiwoom_autotrader.utils.paths import app_data_root

SCHEMA = 'jev-api-credentials-v1'
FILENAME = 'jev_api_credentials.json'
MAX_FILE_BYTES = 4096


class JevCredentialUnavailable(ValueError):
    """Only a public enum is stored; no path, parsed document or private value."""
    def __init__(self, reason):
        self.reason = reason if reason in {'credentials_invalid', 'credentials_unsafe'} else 'credentials_unsafe'
        super().__init__(self.reason)


def _key(value):
    if not isinstance(value, str) or len(value) > 4096 or any(not 33 <= ord(c) <= 126 for c in value):
        raise JevCredentialUnavailable('credentials_invalid')
    return value


def _unique(pairs):
    result = {}
    for name, value in pairs:
        if name in result:
            raise JevCredentialUnavailable('credentials_invalid')
        result[name] = value
    return result


def _read_fixed_file():
    path = Path(app_data_root()) / 'config' / 'secrets' / FILENAME
    if not path.is_absolute() or '..' in path.parts:
        raise JevCredentialUnavailable('credentials_unsafe')
    # Unsupported platforms may still use the environment, but must never
    # silently downgrade filesystem checks for this optional fallback.
    if not all(hasattr(os, attr) for attr in ('O_NOFOLLOW', 'O_DIRECTORY', 'getuid')):
        raise JevCredentialUnavailable('credentials_unsafe')
    parent_fd = file_fd = None
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, 'O_CLOEXEC', 0)
    try:
        parent_fd = os.open(path.anchor, directory_flags)
        for component in path.parts[1:-1]:
            next_fd = os.open(component, directory_flags, dir_fd=parent_fd)
            os.close(parent_fd); parent_fd = next_fd
        file_fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
                          | getattr(os, 'O_CLOEXEC', 0), dir_fd=parent_fd)
        before = os.fstat(file_fd)
        if (not stat.S_ISREG(before.st_mode) or stat.S_IMODE(before.st_mode) != 0o600
                or before.st_uid != os.getuid() or before.st_nlink != 1):
            raise JevCredentialUnavailable('credentials_unsafe')
        if not 0 <= before.st_size <= MAX_FILE_BYTES:
            raise JevCredentialUnavailable('credentials_invalid')
        raw = bytearray()
        while len(raw) <= MAX_FILE_BYTES:
            chunk = os.read(file_fd, MAX_FILE_BYTES + 1 - len(raw))
            if not chunk:
                break
            raw.extend(chunk)
        after = os.fstat(file_fd)
        identity = lambda s: (s.st_dev, s.st_ino, s.st_mode, s.st_uid,
                              s.st_nlink, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
        if identity(before) != identity(after) or len(raw) != before.st_size:
            raise JevCredentialUnavailable('credentials_unsafe')
        return bytes(raw)
    except FileNotFoundError:
        return None
    except JevCredentialUnavailable:
        raise
    except (OSError, TypeError, ValueError):
        raise JevCredentialUnavailable('credentials_unsafe') from None
    finally:
        if file_fd is not None:
            os.close(file_fd)
        if parent_fd is not None:
            os.close(parent_fd)


def load_jev_api_key(*, environ=None):
    """Return a key or empty string; malformed/unsafe inputs raise safe enums.

    ``environ`` is injectable for isolated tests. It cannot change the fixed
    fallback path. An explicitly configured malformed environment key does not
    fall through to another key.
    """
    environment = os.environ if environ is None else environ
    value = environment.get('TYPESAFE_API_KEY', '')
    if value != '':
        return _key(value)
    raw = _read_fixed_file()
    if raw is None or not raw.strip():
        return ''
    try:
        document = json.loads(raw, object_pairs_hook=_unique,
                              parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        if (type(document) is not dict or set(document) != {'schema', 'api_key'}
                or document['schema'] != SCHEMA):
            raise ValueError
        return _key(document['api_key'])
    except JevCredentialUnavailable:
        raise
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise JevCredentialUnavailable('credentials_invalid') from None
