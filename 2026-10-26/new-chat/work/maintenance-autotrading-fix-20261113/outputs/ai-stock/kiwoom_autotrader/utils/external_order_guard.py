"""Staged external-ledger guard; not connected to runtime startup.

The caller must wire *all* persistent stores, failure hooks and broker/stream
reconciliation before enabling it. This class alone does not make SSD
migration safe, and a final check cannot be atomic with a network send.
"""
from pathlib import Path
import sqlite3
import threading

from .external_storage import ExternalStorageUnavailable


class ExternalOrderGuard:
    """Initially disarmed; any storage/stream failure requires explicit rearm.

    ``root_factory`` must return a freshly checked ExternalDataRoot. The
    reconcile and stream checks must use the existing broker session and prove
    positions, outstanding orders, execution coverage and session identity.
    They are application-supplied and not implemented by this module.
    """

    def __init__(self, root_factory, ledger_relative, stream_check):
        self._factory = root_factory
        self._relative = Path(ledger_relative)
        if self._relative.is_absolute() or not self._relative.parts or '..' in self._relative.parts:
            raise ValueError('relative ledger path required')
        if not callable(root_factory) or not callable(stream_check):
            raise ValueError('storage factory and stream check required')
        self._stream_check = stream_check
        self._root = None
        self._ledger_identity = None
        self._armed = False
        self._lock = threading.RLock()

    def _check(self):
        paths = self._root.require_available()
        root = paths.data_root
        ledger = root / self._relative
        try:
            if ledger.is_symlink() or ledger.resolve(strict=True) != ledger or not ledger.is_file():
                raise ExternalStorageUnavailable('existing ledger required')
            stat = ledger.stat()
            if stat.st_dev != root.stat().st_dev or stat.st_size == 0:
                raise ExternalStorageUnavailable('ledger not on checked volume')
            identity = (stat.st_dev, stat.st_ino)
            if self._ledger_identity is not None and identity != self._ledger_identity:
                raise ExternalStorageUnavailable('ledger identity changed')
            # mode=ro never creates an empty replacement ledger. quick_check is
            # not a durability/write test; actual I/O failures must call fail.
            uri = ledger.as_uri() + '?mode=ro'
            with sqlite3.connect(uri, uri=True, timeout=1) as db:
                db.execute('PRAGMA query_only=ON')
                if db.execute('PRAGMA quick_check(1)').fetchone() != ('ok',):
                    raise ExternalStorageUnavailable('ledger integrity unavailable')
                tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if not {'orders', 'fills', 'intents', 'broker_observations'} <= tables:
                    raise ExternalStorageUnavailable('ledger schema unavailable')
            if self._stream_check() is not True:
                raise ExternalStorageUnavailable('execution stream unavailable')
            self._ledger_identity = identity
        except (OSError, sqlite3.Error, ValueError):
            raise ExternalStorageUnavailable('external ledger unavailable') from None

    def arm(self, reconcile):
        """Explicitly rearm after fresh, caller-proven reconciliation."""
        with self._lock:
            self._armed = False
            self._root = None
            self._ledger_identity = None
            try:
                self._root = self._factory()
                self._check()
                if not callable(reconcile) or reconcile() is not True:
                    raise ExternalStorageUnavailable('broker reconciliation not confirmed')
                self._check()
                self._armed = True
            except Exception:
                self._armed = False
                self._root = None
                self._ledger_identity = None
                raise ExternalStorageUnavailable('external storage could not be armed') from None

    def __call__(self):
        """Validate at order dispatch; any failure is latched."""
        with self._lock:
            if not self._armed:
                raise ExternalStorageUnavailable('external storage not armed')
            try:
                self._check()
            except Exception:
                self._armed = False
                self._root = None
                self._ledger_identity = None
                raise ExternalStorageUnavailable('external storage check failed') from None

    def fail(self):
        """Application must call on any suspected storage or stream failure."""
        with self._lock:
            self._armed = False
            self._root = None
            self._ledger_identity = None
