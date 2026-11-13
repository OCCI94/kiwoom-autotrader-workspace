"""Persist account-wide 00 observations through the existing shared socket.

Specification: /tr_docs/00.md. 910/911 are average price/cumulative quantity;
909/914/915 are the execution number/unit price/unit quantity. This recorder
never manufactures fills from cumulative differences or assigns a strategy.
The tracker owns immutable observation storage. Repeated deliveries retain
their broker execution identifiers for downstream deduplication, without
discarding a potentially corrected or additional observation here.
"""
from __future__ import annotations

from datetime import datetime
import re
import sqlite3
import threading
import logging
from zoneinfo import ZoneInfo

from . import Subscription, get_websocket_engine
from .gap_diagnostics import PHASES, TRIGGERS, session_phase


KST = ZoneInfo("Asia/Seoul")
ALLOWED_FIELDS = frozenset({"9203", "9001", "912", "913", "302", "900", "901", "902", "903",
                          "904", "905", "906", "907", "908", "909", "910", "911", "914", "915",
                          "938", "939", "919", "922", "923", "2134", "2135", "2136"})
_NUMERIC_FIELDS = frozenset({"900", "901", "902", "903", "910", "911", "914", "915", "938", "939"})
_STORAGE_ERROR = "주문체결 관측을 저장하지 못했습니다. 주문을 보류하고 원장을 확인하세요."
_INPUT_ERROR = "주문체결 관측 형식을 확인하지 못했습니다. 주문을 보류하세요."
_GAP_ERROR = "주문체결 수신의 연속성을 확인하지 못했습니다. 주문을 보류하고 대사하세요."
GAP_REASONS = frozenset({"session_identity_changed", "callback_error", "callback_dropped",
                         "callback_error_and_dropped", "subscription_not_ready",
                         "connection_generation_changed", "subscription_and_generation_changed",
                         "drain_or_shutdown_uncertain"})
GAP_STATE_FIELDS = frozenset({"prior_ready", "current_ready", "prior_connection_generation",
    "current_connection_generation", "prior_callback_errors", "current_callback_errors",
    "prior_callback_dropped", "current_callback_dropped", "prior_mode", "current_mode"})
GAP_DIAGNOSTIC_FIELDS = frozenset({'ws_close_code', 'last_rx_age_ms', 'session_phase', 'trigger_detail'})


def _storage_failure_category(error):
    """Bounded diagnostics: never expose exception messages, paths or values."""
    current = error
    for _ in range(4):
        if isinstance(current, sqlite3.Error):
            code = getattr(current, 'sqlite_errorcode', None)
            if type(code) is int and code & 255 == sqlite3.SQLITE_BUSY:
                return 'sqlite_busy'
            if type(code) is int and code & 255 == sqlite3.SQLITE_LOCKED:
                return 'sqlite_locked'
            return 'sqlite_error'
        current = getattr(current, '__cause__', None) or getattr(current, '__context__', None)
        if current is None:
            break
    if isinstance(error, OSError):
        return 'os_error'
    if isinstance(error, (ValueError, TypeError)):
        return 'validation_error'
    return 'storage_error'


def _storage_failure_details(error, moment):
    category = _storage_failure_category(error)
    # Raw driver/remote errors can contain tokens, SQL values or account IDs.
    # Preserve the concrete safe class and a bounded message instead.
    allowed = {'OperationalError', 'DatabaseError', 'IntegrityError', 'OSError',
               'PermissionError', 'TimeoutError', 'ValueError', 'TypeError', 'RuntimeError'}
    return {'category': category,
            'exception_type': type(error).__name__ if type(error).__name__ in allowed else 'Exception',
            'safe_message': {'sqlite_busy': 'database is busy', 'sqlite_locked': 'database is locked',
                'sqlite_error': 'database operation failed', 'os_error': 'local I/O failed',
                'validation_error': 'observation validation failed'}.get(category, 'observation storage failed'),
            'failed_at': moment.isoformat() if isinstance(moment, datetime) and moment.tzinfo else None}


def valid_gap_values(values):
    """Allow only bounded, credential-free diagnostics; old empty gaps remain readable."""
    if not isinstance(values, dict):
        return False
    if not values:
        return True
    if values.get("gap_reason") not in GAP_REASONS or not set(values) <= GAP_STATE_FIELDS | GAP_DIAGNOSTIC_FIELDS | {"gap_reason"}:
        return False
    for key, value in values.items():
        if key == "gap_reason":
            continue
        if key == 'ws_close_code':
            allowed = value == 'unknown' or isinstance(value, str) and re.fullmatch(r'[1-4][0-9]{3}', value) is not None
        elif key == 'session_phase':
            allowed = isinstance(value, str) and value in PHASES
        elif key == 'trigger_detail':
            allowed = isinstance(value, str) and value in TRIGGERS
        elif key.endswith("_ready"):
            allowed = value in ("true", "false", "unknown")
        elif key.endswith("_mode"):
            allowed = value in ("paper", "real", "unknown")
        else:
            allowed = value == "unknown" or isinstance(value, str) and re.fullmatch(r"[0-9]{1,15}", value) is not None
        if not allowed:
            return False
    return True


class ExecutionStreamRecorder:
    """One 00 subscription; start/stop/status never create another engine.

    ready describes current capture readiness, not historical completeness.
    After reconnection, coverage_complete stays false even when ready recovers.
    Owners must also reconcile REST balances/pending orders before sending.
    Storage failures and callback loss remain latched for this instance.
    """

    def __init__(self, session, tracker, *, now=None, poll_interval=0.1):
        if not callable(getattr(tracker, "record_broker_observation", None)):
            raise ValueError("영속 주문체결 관측 기록기가 필요합니다.")
        if type(poll_interval) not in (int, float) or not 0.01 <= poll_interval <= 5:
            raise ValueError("주문체결 상태 점검 간격이 올바르지 않습니다.")
        self.session, self.tracker = session, tracker
        self._now = now or (lambda: datetime.now(KST))
        self._poll = poll_interval
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._engine = self._handle = self._thread = self._identity = None
        self._active = self._was_ready = self._ever_ready = False
        self._stopped = False
        self._stopping = False
        self._last_generation = None
        self._counters = None
        self._error = None
        self._gap_count = self._observations = self._ambiguous = 0
        self._ingress_sequence = self._inflight_callbacks = 0
        self._last_observed_at = self._last_classification = None
        self._last_storage_failure = None
        self._recovery_checkpoint = self._recovery_started_at = None

    def __repr__(self):
        return f"ExecutionStreamRecorder(active={self._active}, observations={self._observations})"

    def _session_identity(self):
        try:
            mode, token = self.session.mode, self.session.token
            if self.session.token_usable and mode in {"paper", "real"} and isinstance(token, str) and token:
                return mode, token
        except Exception:
            pass
        return None

    def _moment(self):
        moment = self._now()
        if not isinstance(moment, datetime) or moment.tzinfo is None:
            raise ValueError("관측 시각의 시간대가 필요합니다.")
        return moment.astimezone(KST)

    def _record(self, kind, values, identity):
        if identity is None:
            return False
        moment = None
        try:
            moment = self._moment()
            event = {"schema_version": 1, "source_api": "00", "source_kind": "broker_observation",
                     "observed_at": moment.isoformat(), "day": moment.strftime("%Y%m%d"),
                     "mode": identity[0], "event_kind": kind, "values": values}
            self.tracker.record_broker_observation(event)
            with self._lock:
                self._last_observed_at = max(self._last_observed_at or "", event["observed_at"])
            return True
        except Exception as error:
            with self._lock:
                self._error = self._error or _STORAGE_ERROR
                self._last_storage_failure = {
                    'event_kind': kind if kind in ('stream_started', 'stream_resumed', 'stream_gap',
                                                   'stream_stopped', 'execution_stream') else 'unknown',
                    **_storage_failure_details(error, moment),
                }
                self._recovery_checkpoint = self._recovery_started_at = None
                logging.getLogger(__name__).error('00 storage failure: %s', self._last_storage_failure)
            return False

    def _subscribe(self, identity):
        self._identity = identity
        self._handle = self._engine.subscribe(Subscription(("",), ("00",)),
                                              lambda row: self._on_row(row, identity))

    def start(self):
        with self._lock:
            if self._stopped:
                raise ValueError("종료된 주문체결 기록기는 새 인스턴스로 시작하세요.")
            if self._active:
                return self.status()
            self._engine = get_websocket_engine(self.session)
            self._active = True
            self._stop_event.clear()
            snapshot = self._engine.snapshot()
            self._counters = (snapshot.get("callback_errors", 0), snapshot.get("callback_dropped", 0))
            try:
                self._subscribe(self._session_identity())
            except Exception:
                self._active = False
                self._error = "주문체결 구독을 시작하지 못했습니다."
                return self.status()
            self._thread = threading.Thread(target=self._watch, name="kiwoom-execution-stream", daemon=True)
            self._thread.start()
        return self.status()

    def _gap(self, identity, reason, *, snapshot=None, ready=None, current_identity=None):
        if reason not in GAP_REASONS:
            raise ValueError("invalid stream gap reason")
        self._gap_count += 1
        snapshot = snapshot if isinstance(snapshot, dict) else {}
        def counter(value):
            return str(value) if type(value) is int and 0 <= value < 10**15 else "unknown"
        prior = self._counters or (None, None)
        values = {"gap_reason": reason, "prior_ready": "true" if self._was_ready else "false",
                  "current_ready": "unknown" if ready is None else "true" if ready else "false",
                  "prior_connection_generation": counter(self._last_generation),
                  "current_connection_generation": counter(snapshot.get("connection_generation")),
                  "prior_callback_errors": counter(prior[0]),
                  "current_callback_errors": counter(snapshot.get("callback_errors")),
                  "prior_callback_dropped": counter(prior[1]),
                  "current_callback_dropped": counter(snapshot.get("callback_dropped")),
                  "prior_mode": identity[0] if identity else "unknown",
                  "current_mode": current_identity[0] if current_identity else "unknown"}
        local_triggers = {'session_identity_changed': 'session_changed', 'callback_error': 'callback_error',
            'callback_dropped': 'callback_dropped', 'callback_error_and_dropped': 'callback_error_and_dropped',
            'subscription_not_ready': 'subscription_changed', 'drain_or_shutdown_uncertain': 'shutdown_uncertain'}
        trigger = local_triggers.get(reason, snapshot.get('trigger_detail', 'unknown'))
        code = snapshot.get('ws_close_code') if reason not in local_triggers else None
        generation = snapshot.get('disconnect_generation')
        age = snapshot.get('last_rx_age_ms')
        if reason not in local_triggers:
            if type(generation) is int and generation == self._last_generation:
                age = snapshot.get('disconnect_last_rx_age_ms')
            else:
                trigger, code, age = 'unknown', None, None
        values.update(ws_close_code=str(code) if type(code) is int and 1000 <= code <= 4999 else 'unknown',
                      last_rx_age_ms=counter(age),
                      session_phase=session_phase(self._moment()),
                      trigger_detail=trigger if isinstance(trigger, str) and trigger in TRIGGERS else 'unknown')
        recorded = self._record("stream_gap", values, identity)
        self._was_ready = False
        return recorded

    def _refresh(self):
        if not self._active or self._stopping:
            return False
        identity = self._session_identity()
        if identity != self._identity:
            if self._was_ready:
                self._gap(self._identity, "session_identity_changed", snapshot=self._engine.snapshot(),
                          current_identity=identity)
            self._engine.unsubscribe(self._handle)
            self._subscribe(identity)
        snapshot = self._engine.snapshot()
        counters = (snapshot.get("callback_errors", 0), snapshot.get("callback_dropped", 0))
        ready = identity is not None and self._engine.subscription_ready(self._handle)
        if self._counters is not None and any(new > old for new, old in zip(counters, self._counters)):
            errors = counters[0] > self._counters[0]
            dropped = counters[1] > self._counters[1]
            reason = "callback_error_and_dropped" if errors and dropped else "callback_error" if errors else "callback_dropped"
            self._gap(self._identity, reason, snapshot=snapshot, ready=ready, current_identity=identity)
            self._error = self._error or _GAP_ERROR
        generation = snapshot.get("connection_generation")
        if self._was_ready and (not ready or generation != self._last_generation):
            changed = generation != self._last_generation
            reason = ("subscription_and_generation_changed" if not ready and changed else
                      "subscription_not_ready" if not ready else "connection_generation_changed")
            self._gap(self._identity, reason, snapshot=snapshot, ready=ready, current_identity=identity)
        self._counters = counters
        # A failed lifecycle write must not be acknowledged in memory. Once
        # latched, ordinary status polls cannot mint a new durable anchor;
        # only explicit paper recovery may do so before full reconciliation.
        if ready and not self._was_ready and self._error is None:
            kind = "stream_resumed" if self._ever_ready else "stream_started"
            if self._record(kind, {}, self._identity):
                self._ever_ready = self._was_ready = True
                self._last_generation = generation
        return bool(ready and self._was_ready and self._error is None)

    def _watch(self):
        while not self._stop_event.wait(self._poll):
            with self._lock:
                try:
                    self._refresh()
                except Exception:
                    self._error = self._error or "주문체결 수신 상태를 확인하지 못했습니다. 주문을 보류하세요."

    def status(self):
        with self._lock:
            delivery = None
            try:
                ready = self._refresh()
                if self._active and not self._stopping:
                    delivery = self._engine.subscription_delivery_status(self._handle)
                    if (not isinstance(delivery, dict) or type(delivery.get("ingress_sequence")) is not int
                            or delivery["ingress_sequence"] < 0 or type(delivery.get("pending_callbacks")) is not int
                            or delivery["pending_callbacks"] < 0):
                        raise ValueError
            except Exception:
                ready = False
                self._error = self._error or "주문체결 수신 상태를 확인하지 못했습니다. 주문을 보류하세요."
            return {"active": self._active, "stopping": self._stopping, "ready": ready, "error": self._error,
                    "coverage_complete": bool(self._ever_ready and self._gap_count == 0 and self._error is None),
                    "coverage_scope": "since_first_registration_only", "gap_count": self._gap_count,
                    "observations": self._observations, "ambiguous_observations": self._ambiguous,
                    "ingress_sequence": self._ingress_sequence, "inflight_callbacks": self._inflight_callbacks,
                    "engine_ingress_sequence": delivery["ingress_sequence"] if delivery is not None else None,
                    "engine_pending_callbacks": delivery["pending_callbacks"] if delivery is not None else None,
                    "unit_details_complete": bool(self._ever_ready and self._ambiguous == 0
                                                   and self._gap_count == 0 and self._error is None),
                    "last_observed_at": self._last_observed_at, "last_classification": self._last_classification,
                    "last_storage_failure": dict(self._last_storage_failure) if self._last_storage_failure else None}

    def begin_paper_recovery(self):
        """Mark an uncertain interval before broker reconciliation.

        The returned in-memory checkpoint contains session material and must
        never be logged or persisted. Historical coverage is not repaired.
        """
        with self._lock:
            identity=self._session_identity()
            if (identity is None or identity[0]!='paper' or identity!=self._identity
                    or not self._active or self._stopping or self._engine is None):
                raise ValueError('paper_recorder_recovery_unavailable')
            if self._error is None:
                return None
            self._recovery_checkpoint = self._recovery_started_at = None
            self._refresh()
            snapshot=self._engine.snapshot()
            delivery=self._engine.subscription_delivery_status(self._handle)
            if (identity!=self._session_identity() or not self._engine.subscription_ready(self._handle)
                    or not isinstance(delivery,dict) or delivery.get('pending_callbacks')!=0
                    or self._inflight_callbacks!=0):
                raise ValueError('paper_recorder_recovery_unavailable')
            # The last durable lifecycle row may itself be the failed
            # reconnect's gap. Requiring an existing live anchor deadlocks
            # recovery. Persist BOTH a new uncertain boundary and a resume,
            # then ask the ledger to verify the resulting anchor. Keep the
            # error latch until post-anchor broker reconciliation completes.
            started=self._moment()
            if not self._gap(identity,'drain_or_shutdown_uncertain',snapshot=snapshot,
                             ready=True,current_identity=identity):
                raise ValueError('paper_recorder_recovery_unavailable')
            if not self._record('stream_resumed',{},identity):
                raise ValueError('paper_recorder_recovery_unavailable')
            self._ever_ready = self._was_ready = True
            self._last_generation = snapshot.get('connection_generation')
            delivery=self._engine.subscription_delivery_status(self._handle)
            if (not self._was_ready or not isinstance(delivery,dict)
                    or type(delivery.get('ingress_sequence')) is not int
                    or delivery.get('pending_callbacks')!=0 or self._inflight_callbacks!=0):
                raise ValueError('paper_recorder_recovery_unavailable')
            anchor,_,anchor_at=self.tracker.stream_anchor(self._moment())
            anchor_time=datetime.fromisoformat(anchor_at)
            if (type(anchor) is not int or anchor<=0 or anchor_time.tzinfo is None
                    or not started<=anchor_time<=self._moment() or anchor_at!=self._last_observed_at):
                raise ValueError('paper_recorder_recovery_unavailable')
            checkpoint=(identity,id(self._engine),self._last_generation,self._gap_count,self._counters,
                        self._ingress_sequence,delivery['ingress_sequence'],anchor)
            self._recovery_checkpoint=checkpoint
            self._recovery_started_at=anchor_time
            return checkpoint

    def require_paper_recovery(self):
        """Latch a missing current-day lifecycle; only full recovery clears it."""
        with self._lock:
            identity = self._session_identity()
            if identity is None or identity[0] != 'paper' or not self._active or self._stopping:
                return False
            self._error = self._error or '당일 주문체결 시작·재개 원장 기준을 확인하지 못했습니다. 대사 전 주문을 보류합니다.'
            return True

    def complete_paper_recovery(self, checkpoint, proof):
        """Clear a paper latch only after a verified full broker reconciliation."""
        validator=getattr(self.tracker,'validate_paper_stream_recovery',None)
        with self._lock:
            if checkpoint is None or checkpoint!=self._recovery_checkpoint or self._recovery_started_at is None:
                return False
            minimum_started_at=self._recovery_started_at
        try:
            if not callable(validator) or not validator(proof,self._moment(),minimum_started_at=minimum_started_at):
                return False
        except Exception:
            return False
        with self._lock:
            identity=self._session_identity()
            if (identity is None or identity[0]!='paper' or identity!=self._identity
                    or not self._active or self._stopping or not self._was_ready
                    or not self._engine.subscription_ready(self._handle)):
                return False
            snapshot=self._engine.snapshot();delivery=self._engine.subscription_delivery_status(self._handle)
            counters=(snapshot.get('callback_errors',0),snapshot.get('callback_dropped',0))
            if not isinstance(delivery,dict) or delivery.get('pending_callbacks')!=0 or self._inflight_callbacks!=0:
                return False
            current=(identity,id(self._engine),snapshot.get('connection_generation'),self._gap_count,counters,
                     self._ingress_sequence,delivery.get('ingress_sequence'),
                     self.tracker.stream_anchor(self._moment())[0])
            if current!=checkpoint or checkpoint!=self._recovery_checkpoint:return False
            self._error=None
            self._recovery_checkpoint = self._recovery_started_at = None
            return True

    def _on_row(self, row, identity):
        with self._lock:
            if not self._active or identity is None or identity != self._identity or identity != self._session_identity():
                return  # A queued callback from an old token must not enter the new scope.
            try:
                if not isinstance(row, dict) or row.get("type") != "00" or not isinstance(row.get("values"), dict):
                    raise ValueError
                raw = row["values"]
                sensitive = [identity[1]] + [raw[key] for key in ("9201", "9205", "920", "921")
                                              if isinstance(raw.get(key), str) and len(raw[key]) >= 6]
                values = {}
                for key in ALLOWED_FIELDS & raw.keys():
                    value = raw[key]
                    if (not isinstance(value, str) or len(value) > 256 or value and not value.isprintable()
                            or any(secret in value for secret in sensitive)):
                        raise ValueError
                    if key in _NUMERIC_FIELDS and value and re.fullmatch(r"[+-]?[0-9]{1,24}(?:\.[0-9]{1,12})?", value) is None:
                        raise ValueError
                    values[key] = value
                unit = (bool(re.fullmatch(r"[0-9]{1,20}", values.get("909", "")))
                        and int(values["909"]) > 0
                        and bool(re.fullmatch(r"[0-9]{1,7}", values.get("9203", "")))
                        and int(values["9203"]) > 0
                        and bool(re.fullmatch(r"[AJQ]?[0-9]{6}", values.get("9001", "")))
                        and bool(re.fullmatch(r"(?:[01][0-9]|2[0-3])[0-5][0-9][0-5][0-9]", values.get("908", "")))
                        and values.get("907") in {"1", "2"} and values.get("913") == "체결"
                        and bool(re.fullmatch(r"\+?[0-9]{1,24}(?:\.[0-9]{1,12})?", values.get("914", "")))
                        and bool(re.fullmatch(r"\+?[0-9]{1,15}", values.get("915", "")))
                        and int(values["915"]) > 0 and any(ch in "123456789" for ch in values["914"]))
                possible_fill = values.get("913") == "체결" or any(values.get(key) for key in ("909", "914", "915"))
                classification = "unit_execution" if unit else ("unconfirmed_execution" if possible_fill else "order_update")
            except Exception:
                self._error = self._error or _INPUT_ERROR
                return
            # Publish receipt before releasing the lock for durable I/O, so a
            # pre-order reconciliation never overlooks an in-progress callback.
            self._ingress_sequence += 1
            self._inflight_callbacks += 1
        # Never hold the recorder state lock during durable I/O. In particular,
        # stop/status must be able to mark stopping while this callback drains.
        recorded = False
        try:
            recorded = self._record("execution_stream", values, identity)
        finally:
            with self._lock:
                if recorded:
                    self._observations += 1
                    self._ambiguous += int(classification == "unconfirmed_execution")
                    self._last_classification = classification
                self._inflight_callbacks -= 1

    def stop(self, *, timeout=2.0):
        with self._lock:
            if not self._active or self._stopping:
                return
            # This also captures a disconnect/callback failure that happened
            # after the last periodic poll, before shutdown freezes ingress.
            self.status()
            self._stopping = True
            self._stop_event.set()
            engine, handle, identity = self._engine, self._handle, self._identity
        # Keep queued callbacks enabled until the engine freezes new ingress
        # and drains them. Holding our lock here would deadlock their writes.
        try:
            complete = engine.drain_and_unsubscribe(handle, timeout=timeout)
        except Exception:
            complete = False
            try:
                engine.unsubscribe(handle)
            except Exception:
                pass
        with self._lock:
            self._active = False
            self._stopped = True
            snapshot = None
            try:
                snapshot = engine.snapshot()
                counters = (snapshot.get("callback_errors", 0), snapshot.get("callback_dropped", 0))
            except Exception:
                complete, counters = False, None
            if (not complete or self._inflight_callbacks or self._error is not None or counters != self._counters
                    or self._session_identity() != identity):
                self._gap(identity, "drain_or_shutdown_uncertain", snapshot=snapshot, ready=False,
                          current_identity=self._session_identity())
                self._error = self._error or _GAP_ERROR
            self._record("stream_stopped", {}, self._identity)
            self._was_ready = False
            self._stopping = False
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2)
