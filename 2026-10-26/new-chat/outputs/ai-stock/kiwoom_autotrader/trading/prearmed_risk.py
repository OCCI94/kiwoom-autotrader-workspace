"""Background local evidence index. Never a broker balance or order authority."""
from copy import deepcopy
from datetime import datetime, timedelta
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import stat
import threading
import time
from types import MappingProxyType

from .steady_uptrend_vwap import moment, risk_eligible


def read_index(path, now):
    fd = None
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
        meta = os.fstat(fd)
        if (not stat.S_ISREG(meta.st_mode) or not 0 < meta.st_size <= 256 * 1024
                or meta.st_mode & 0o022 or hasattr(os, 'getuid') and meta.st_uid != os.getuid()):
            raise ValueError('risk_file_invalid')
        raw = os.read(fd, 256 * 1024 + 1)
        if len(raw) != meta.st_size: raise ValueError('risk_file_incomplete')
        def unique(pairs):
            out = {}
            for key, value in pairs:
                if key in out: raise ValueError('duplicate_key')
                out[key] = value
            return out
        data = json.loads(raw, object_pairs_hook=unique)
        if not isinstance(data, dict) or data.get('schema') != 'toss-hybrid-risk-evidence-v1':
            raise ValueError('risk_schema')
        generated, expires = moment(data.get('generated_at')), moment(data.get('expires_at'))
        rows = data.get('rows')
        if (generated is None or expires is None or not generated <= now < expires
                or expires - generated > timedelta(hours=24) or not isinstance(rows, list) or len(rows) > 1200):
            raise ValueError('risk_expired_or_unavailable')
        digest = sha256(raw).hexdigest(); contexts = {}; seen = set()
        for row in rows:
            code = row.get('symbol') if isinstance(row, dict) else None
            if not isinstance(code, str) or re.fullmatch('[0-9]{6}', code) is None or code in seen:
                raise ValueError('risk_symbols_invalid')
            seen.add(code)
            reviewed = moment(row.get('reviewed_at'))
            if reviewed is None or reviewed > generated: continue
            context = {'schema':'s5-dart-risk-v1', 'symbol':code,
                'reviewed_at':row.get('reviewed_at'), 'expires_at':expires.isoformat(),
                'coverage_complete':row.get('coverage_complete'),
                'unresolved_major_risk':row.get('unresolved_major_risk'),
                'source_sha256':row.get('source_sha256'), 'file_sha256':digest}
            if 'short_selling_evidence' in row:
                context['short_selling_evidence'] = deepcopy(row['short_selling_evidence'])
            if risk_eligible(context, code, now): contexts[code] = context
        return digest, expires, MappingProxyType(contexts)
    finally:
        if fd is not None: os.close(fd)


class BackgroundS5RiskCache:
    """One atomic snapshot; signal-side reads never stat/open/wait on a lock.

    Revocations are visible after the next .5s poll. If the worker stalls for
    >1.5s, all cached entries are unavailable. These bounded observations do
    not replace canonical atomic reservation and pre-send broker reconciliation.
    """
    def __init__(self, path, *, clock=time.monotonic, now=None, autostart=True):
        self.path = Path(path); self._clock = clock
        self._now = now or (lambda: datetime.now().astimezone())
        self._snapshot = None; self._stop = threading.Event(); self._thread = None
        if autostart:
            self._thread = threading.Thread(target=self._run, name='s5-local-risk-index', daemon=True)
            self._thread.start()

    def refresh(self):
        started = self._clock()
        try:
            now = moment(self._now())
            if now is None: raise ValueError('clock_invalid')
            digest, expires, contexts = read_index(self.path, now)
            snapshot = (started, digest, expires, contexts, now)
        except Exception:
            snapshot = None
        if not self._stop.is_set(): self._snapshot = snapshot

    def _run(self):
        while not self._stop.is_set():
            self.refresh(); self._stop.wait(.5)

    def _current(self, now):
        item = self._snapshot
        if (item is None or self._stop.is_set() or moment(now) is None
                or not 0 <= self._clock() - item[0] <= 1.5 or not item[4] <= now < item[2]): return None
        return item

    def get(self, symbol, now):
        item = self._current(now)
        return deepcopy(item[3].get(symbol)) if item else None

    def generation(self, now):
        item = self._current(now)
        return item[1] if item else None

    def symbols(self, now):
        """Fresh local risk scope for scheduling only, never order permission."""
        item = self._current(now)
        return tuple(sorted(item[3])) if item else ()

    def close(self):
        self._stop.set(); self._snapshot = None  # no join in receiver path
