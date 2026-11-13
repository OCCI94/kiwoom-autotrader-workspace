"""Non-blocking Jev pre-scoring, keyed to an exact observed market generation.

The order path only reads an immutable published view. Missing/late/old-model
answers yield the existing deterministic decision; no probability is invented.
All returned model scores are advisory, not calibrated Korean-market win rates.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import re
import threading
import time
from types import MappingProxyType

from .jev_news_client import JevJudgment, JevUnavailable

FAST_FIELDS = ('price_vwap_bps', 'five_minute_slope_bps', 'spread_bps',
               'ask_bid_ratio', 'foreign_net_qty', 'institution_net_qty',
               'short_amount_share', 'catalyst_direction', 'catalyst_relevance',
               'catalyst_evidence', 'catalyst_importance')


def _aware(value):
    return isinstance(value, datetime) and value.tzinfo is not None and value.utcoffset() is not None


def compact_fast_state(state):
    """At most 399 ASCII bytes, not an unverified vendor tokenizer assertion.

    Missing observations stay null. The numeric payload alone is bounded; fixed
    typed questions are additional input and reported token use comes from API.
    """
    if not isinstance(state, dict) or set(state)-set(FAST_FIELDS):
        raise ValueError('invalid_fast_state')
    result = {}
    for key in FAST_FIELDS:
        value = state.get(key)
        if value is not None:
            if (type(value) not in (int, float) or not math.isfinite(value)
                    or abs(value) > 10**13):
                raise ValueError('invalid_fast_state')
            if key.startswith('catalyst_') or key == 'short_amount_share':
                if not 0 <= value <= 1:
                    raise ValueError('invalid_fast_state')
            value = round(value, 6)
        result[key] = value
    raw = json.dumps(result, separators=(',', ':'), allow_nan=False).encode('ascii')
    if len(raw) > 399:
        raise ValueError('fast_state_byte_limit')
    return result


def _snapshot(state, kind):
    if kind == 'fast':
        state = compact_fast_state(state)
    elif kind == 'news':
        if (not isinstance(state, dict) or not isinstance(state.get('text'), str)
                or not 1 <= len(state['text']) <= 4096):
            raise ValueError('invalid_news_state')
    else:
        raise ValueError('invalid_kind')
    raw = json.dumps(state, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8')
    if len(raw) > 8192:
        raise ValueError('state_byte_limit')
    return json.loads(raw), hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True)
class _Task:
    key: tuple
    state: dict
    state_sha256: str
    observed_at: datetime
    expires_at: datetime
    enqueued_at: float
    deadline: float
    valid_until: float
    epoch: int


@dataclass(frozen=True)
class _Result:
    status: str
    judgment: JevJudgment | None
    observed_at: datetime
    expires_at: datetime
    received_at: datetime
    deadline: float
    completed_at: float
    valid_until: float
    state_sha256: str


class AsyncJevDecision:
    """One bounded daemon owner. Neither submit nor lookup waits for network.

    No real-time OS guarantee is made: measure local latency on the target host.
    submit is an upstream/background operation. lookup never starts or waits for
    work, never reads a file, and never acquires the writer lock.
    """
    def __init__(self, client=None, *, deadline_ms=300, max_pending=16,
                 max_entries=256, max_ttl_seconds=900, clock=time.monotonic,
                 now=lambda: datetime.now(timezone.utc)):
        if (type(deadline_ms) not in (int, float) or not 1 <= deadline_ms <= 400
                or type(max_pending) is not int or not 1 <= max_pending <= 128
                or type(max_entries) is not int or not 1 <= max_entries <= 2048
                or type(max_ttl_seconds) not in (int, float) or not 0 < max_ttl_seconds <= 3600):
            raise ValueError('invalid_async_limits')
        self._client, self._clock, self._now = client, clock, now
        self._deadline_ms, self._max_pending = deadline_ms, max_pending
        self._max_entries, self._max_ttl = max_entries, max_ttl_seconds
        self._lock, self._wake = threading.Lock(), threading.Event()
        self._pending = deque()
        self._closed = False
        self._epoch = 0
        # Every update publishes whole immutable mappings in one assignment.
        self._view = (MappingProxyType({}), MappingProxyType({}))
        self._worker = None
        if self.available:
            self._worker = threading.Thread(target=self._run, name='jev-advisory', daemon=True)
            self._worker.start()

    @property
    def available(self):
        return bool(self._client is not None and getattr(self._client, 'available', False))

    def submit(self, *, kind, symbol, session_id, snapshot_id, state, observed_at, expires_at):
        if not self.available or self._closed:
            return False
        current, mono = self._now(), self._clock()
        if (not isinstance(symbol, str) or not re.fullmatch(r'[0-9]{6}', symbol)
                or not isinstance(session_id, str) or not 1 <= len(session_id) <= 128
                or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', session_id) or not isinstance(snapshot_id, str)
                or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', snapshot_id)
                or not all(_aware(t) for t in (observed_at, expires_at, current))
                or not observed_at <= current < expires_at
                or not 0 < (expires_at-observed_at).total_seconds() <= self._max_ttl):
            return False
        try:
            frozen_state, state_sha = _snapshot(state, kind)
            if kind == 'news' and frozen_state.get('symbol') != symbol:
                return False
        except (ValueError, TypeError, RecursionError, OverflowError):
            return False
        if not self._lock.acquire(blocking=False):
            return False
        try:
            if self._closed or len(self._pending) >= self._max_pending:
                return False
            key = (kind, symbol, session_id, snapshot_id)
            slot = (kind, symbol)
            latest, results = self._view
            old = latest.get(slot)
            if old is not None and (observed_at < old[2] or old[:2] == (key, state_sha)):
                return False  # pending/success/failure: same generation is not retried
            new_latest, new_results = dict(latest), dict(results)
            if slot not in latest and len(latest) >= self._max_entries:
                oldest = next(iter(new_latest))
                displaced = new_latest.pop(oldest)
                new_results.pop(displaced[0], None)
            old = new_latest.get(slot)
            if old:
                new_results.pop(old[0], None)
            new_latest[slot] = (key, state_sha, observed_at)
            # Never let a reused snapshot ID silently change the numeric state.
            if old and old[0] == key and old[1] != state_sha:
                return False
            self._view = (MappingProxyType(new_latest), MappingProxyType(new_results))
            ttl_remaining = (expires_at-current).total_seconds()
            task = _Task(key, frozen_state, state_sha, observed_at, expires_at,
                         mono, mono+min(self._deadline_ms/1000, ttl_remaining),
                         mono+ttl_remaining, self._epoch)
            self._pending.append(task)
            self._wake.set()
            return True
        finally:
            self._lock.release()

    def lookup(self, *, kind, symbol, session_id, snapshot_id, now=None):
        """O(1) exact-key read. deterministic_fallback carries no AI probability."""
        fallback = {'source': 'deterministic_fallback', 'status': 'credentials_missing' if not self.available else 'cache_miss',
                    'probabilities': None, 'model': None, 'advisory_only': True}
        if self._closed:
            fallback['status'] = 'closed'
            return fallback
        if any(not isinstance(value, str) or not 1 <= len(value) <= 128
               for value in (kind, symbol, session_id, snapshot_id)):
            fallback['status'] = 'invalid_identity'
            return fallback
        current = self._now() if now is None else now
        if not _aware(current):
            fallback['status'] = 'invalid_time'
            return fallback
        key = (kind, symbol, session_id, snapshot_id)
        latest, results = self._view
        expected = latest.get((kind, symbol))
        if expected is None or expected[0] != key:
            return fallback
        result = results.get(key)
        if result is None:
            fallback['status'] = 'pending'
            return fallback
        if (not result.observed_at <= result.received_at <= current < result.expires_at
                or result.completed_at > result.deadline or self._clock() >= result.valid_until
                or result.state_sha256 != expected[1]):
            fallback['status'] = 'stale_or_late'
            return fallback
        if result.status != 'ready' or result.judgment is None:
            fallback['status'] = result.status
            return fallback
        return {'source': 'jev_cache', 'status': 'ready',
                'probabilities': dict(result.judgment.probabilities), 'model': result.judgment.model,
                'advisory_only': True, 'network_elapsed_ms': result.judgment.elapsed_ms,
                'input_tokens': result.judgment.input_tokens, 'state_sha256': result.state_sha256,
                'received_at': result.received_at.isoformat(), 'expires_at': result.expires_at.isoformat()}

    def invalidate(self):
        """Caller must use a new session id after session/ledger invalidation."""
        if not self._lock.acquire(blocking=False):
            return False
        try:
            self._epoch += 1
            self._pending.clear()
            self._view = (MappingProxyType({}), MappingProxyType({}))
            return True
        finally:
            self._lock.release()

    def close(self):
        # Does not join an in-flight HTTP worker or wait on socket shutdown.
        self._closed = True
        self._wake.set()

    def _run(self):
        while not self._closed:
            self._wake.wait(0.05)
            with self._lock:
                task = self._pending.popleft() if self._pending else None
                if not self._pending:
                    self._wake.clear()
            if task is None:
                continue
            status, judgment = 'deadline_exceeded', None
            if self._clock() < task.deadline:
                try:
                    method = self._client.evaluate_fast if task.key[0] == 'fast' else self._client.evaluate_news
                    judgment = method(task.state, deadline_monotonic=task.deadline)
                    status = 'ready' if isinstance(judgment, JevJudgment) else 'response_invalid'
                except JevUnavailable as error:
                    status = error.reason
                except Exception:
                    status = 'transport_unavailable'
            completed, received = self._clock(), self._now()
            if completed > task.deadline:
                status, judgment = 'deadline_exceeded', None
            with self._lock:
                if self._closed or task.epoch != self._epoch:
                    continue
                latest, results = self._view
                if latest.get(task.key[:2]) != (task.key, task.state_sha256, task.observed_at):
                    continue
                new_results = dict(results)
                new_results[task.key] = _Result(status, judgment, task.observed_at, task.expires_at,
                                               received, task.deadline, completed, task.valid_until,
                                               task.state_sha256)
                self._view = (latest, MappingProxyType(new_results))
