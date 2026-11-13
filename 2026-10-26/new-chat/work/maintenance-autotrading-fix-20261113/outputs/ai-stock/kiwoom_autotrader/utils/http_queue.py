"""Bounded FIFO HTTP dispatch, pending-request coalescing, and a shared rate cap.

Only pending identical requests share one exchange. Once dispatched, a later
identical request is a new operation (this is not a response cache). Secrets and
request bodies stay in memory; the process-shared limiter stores timestamps only.
All project HTTP clients must call queued_open, never opener.open directly.
"""
from __future__ import annotations

from collections import deque
from concurrent.futures import Future
import copy
from kiwoom_autotrader.utils.platform_io import fcntl
import hashlib
import io
import json
import math
import re
from kiwoom_autotrader.utils.platform_io import os
from pathlib import Path
from kiwoom_autotrader.utils.paths import app_data_root
import stat
import sys
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit

_MAX_BYTES = 4 * 1024 * 1024
# Codex-isolated processes on this host exposed different monotonic origins;
# launchd's receiver was observed in the host RAW time range. Shared limiter
# files need one host-wide timebase, so use CLOCK_MONOTONIC_RAW on Darwin.
# Other platforms retain their default. Coordinate upgrades of queue users.
if sys.platform == "darwin" and hasattr(time, "CLOCK_MONOTONIC_RAW"):
    def _CLOCK():
        return time.clock_gettime(time.CLOCK_MONOTONIC_RAW)
else:
    _CLOCK = time.monotonic


_ALLOWED_NOT_SENT_REASONS = frozenset({
    "insufficient_cash",
    "daily_entry_limit_reached",
    "daily_entry_limit_unavailable",
    "concurrent_position_limit_reached",
    "concurrent_position_limit_unavailable",
    "theme_concentration_limit_reached",
    "theme_concentration_unavailable",
    "reconciliation_proof_expired",
    "reconciliation_state_changed",
    "reconciliation_failed",
    "broker_pending_order_exists",
    "local_pending_order_exists",
    "symbol_principal_limit_reached",
    "insufficient_sellable_qty",
    "paper_policy_rejected",
    "session_unavailable",
    "session_changed",
    "maintenance_paused",
    "external_storage_unavailable",
    "stream_unverified",
    "ledger_prepare_failed",
    "multi_exit_dispatch_rejected",
    "multi_buy_dispatch_rejected",
    "b3_dispatch_rejected",
    "dynamic_s1_admission_changed",
    "timed_exit_window_expired",
    "deadline_exceeded",
    "paper_session_changed",
    "experiment_profile_changed",
    "official_session_unavailable",
    "signal_window_expired",
    "exp_d_exit_session_changed",
    "exp_d_exit_ownership_changed",
    "extended_late_exit_session_changed",
    "extended_late_exit_ownership_changed",
    "intraday_entry_cutoff",
    "s5_risk_evidence_changed",
    "hot_policy_rejected",
    "pre_send_guard_rejected",
})


def extract_guard_reason_code(error):
    """Extract only an allowlisted local pre-send code; never copy raw messages."""
    for attr in ("reason_code", "skipped_reason"):
        code = getattr(error, attr, None)
        if isinstance(code, str) and code in _ALLOWED_NOT_SENT_REASONS:
            return code
    if type(error).__name__ == "HotPolicyRejected":
        return "hot_policy_rejected"
    if len(getattr(error, "args", ())) == 1 and isinstance(error.args[0], str) and error.args[0] in _ALLOWED_NOT_SENT_REASONS:
        return error.args[0]
    return None


class HTTPNotSentError(OSError):
    """A pre-send guard rejected the request before any HTTP transport."""

    def __init__(self, message, *, reason_code=None):
        super().__init__(message)
        if reason_code is None and isinstance(message, str) and message in _ALLOWED_NOT_SENT_REASONS:
            reason_code = message
        self.reason_code = reason_code if reason_code in _ALLOWED_NOT_SENT_REASONS else None


class RequestDeadlineOpener:
    """Queue adapter: clamp timeout after queue/thread scheduling delay.

    It is only passed to queued_open; constructing it performs no transport.
    """
    def __init__(self, opener, deadline, clock):
        self._opener, self._deadline, self._clock = opener, deadline, clock
        self.addheaders = getattr(opener, 'addheaders', ())

    def open(self, request, *, timeout):
        remaining = self._deadline - self._clock()
        if remaining <= 0:
            raise HTTPNotSentError('deadline_exceeded')
        return self._opener.open(request, timeout=min(timeout, remaining))


class SharedRateLimit:
    """At most four dispatches in any 1.01s window, across local processes.

    The small margin also keeps exact one-second boundary bursts below five.
    A slot is consumed even when transport fails; no automatic retry is made.
    """
    def __init__(self, path=None, *, clock=_CLOCK, limit=4, window=1.01):
        self.path = Path(path) if path is not None else (
            app_data_root() / "http-rate.json")
        self.clock, self.limit, self.window = clock, limit, window
        self._mutex = threading.Lock()
        self._wait = threading.Event()

    def acquire(self, *, before_send=None):
        while True:
            with self._mutex:
                self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
                try:
                    info = os.fstat(fd)
                    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                        raise OSError("HTTP 전송 제한 기록을 확인하지 못했습니다.")
                    fcntl.flock(fd, fcntl.LOCK_EX)
                    raw = os.read(fd, 4097)
                    try:
                        timestamps = json.loads(raw) if raw else []
                        if (not isinstance(timestamps, list) or len(timestamps) > self.limit
                                or any(type(t) not in (int, float) or not math.isfinite(t) or t < 0
                                       for t in timestamps)
                                or timestamps != sorted(timestamps)):
                            raise ValueError
                    except (ValueError, TypeError):
                        raise OSError("HTTP 전송 제한 기록을 확인하지 못했습니다.") from None
                    now = self.clock()
                    # Monotonic clock is host-wide; a restart of the OS resets it.
                    if timestamps and timestamps[-1] > now:
                        timestamps = []
                    timestamps = [t for t in timestamps if now - t < self.window]
                    if len(timestamps) < self.limit:
                        if before_send is not None:
                            before_send()
                        # Guards can take time. Reserve against their completion,
                        # immediately before dispatch, never against queue arrival.
                        now = self.clock()
                        timestamps = [t for t in timestamps if now - t < self.window]
                        timestamps.append(now)
                        payload = json.dumps(timestamps, separators=(",", ":")).encode("ascii")
                        os.lseek(fd, 0, os.SEEK_SET)
                        os.ftruncate(fd, 0)
                        os.write(fd, payload)
                        os.fsync(fd)
                        return
                    delay = max(0.001, self.window - (now - timestamps[0]))
                finally:
                    os.close(fd)
            self._wait.wait(delay)

    def acquire_for_request(self, request=None, *, before_send=None):
        """Hold a paper TR lease, reserve the global slot, then check guards.

        Kiwoom's paper limit is one call per TR per second. A separate timestamp
        file preserves the legacy four-call global file and shares this stricter
        limit across local processes. All paper accounts conservatively share a
        TR bucket: no token, token hash, body or account identifier is persisted.

        Lock order is always TR then global. The returned TR lease is held until
        the response finishes and uses its completion time, conservatively
        preventing a slow fsync or late worker start from aging out the TR slot.
        A same-TR FIFO entry can wait for the preceding response plus 1.05s.
        No global lock is held during that wait or while HTTP is in flight.
        Source: https://openapi2.kiwoom.com/intro?dummyVal=0 (2026-10-27).
        """
        tr = self._paper_tr(request)
        bucket = ".paper-" + tr if tr is not None else None
        # Kiwoom notice: kt00018 limit changes to three requests per second
        # after 2026-10-29 18:00 KST. Serializing the real TR with a 1.05s
        # completion-based cooldown is intentionally stricter than that cap.
        # This shared timestamp file coordinates only cooperating local apps;
        # other computers or external clients are outside its scope.
        if bucket is None and request is not None and urlsplit(request.full_url).hostname == "api.kiwoom.com":
            api_id = request.get_header("Api-id")
            if isinstance(api_id, str) and api_id.lower() == "kt00018":
                bucket = ".real-kt00018"
        if bucket is None:
            self.acquire(before_send=before_send)
            return None
        lease = _PaperTRLease(self.path.with_name(self.path.name + bucket), self.clock, self._wait)
        lease.acquire()
        try:
            self.acquire(before_send=before_send)
        except BaseException:
            lease.release(consumed=False)
            raise
        return lease

    @staticmethod
    def _paper_tr(request):
        tr = None
        if request is not None and urlsplit(request.full_url).hostname == "mockapi.kiwoom.com":
            tr = request.get_header("Api-id")
            if tr is None:
                tr = {"/oauth2/token": "au10001", "/oauth2/revoke": "au10002"}.get(urlsplit(request.full_url).path)
            if not isinstance(tr, str) or re.fullmatch(r"[A-Za-z]{2,3}[0-9]{5}", tr) is None:
                raise OSError("모의투자 TR 전송 제한 구분을 확인하지 못했습니다.")
            tr = tr.lower()
        return tr

    def try_reserve_research_tr(self, request):
        """Try a paper TR lease without blocking the shared FIFO dispatcher."""
        tr = self._paper_tr(request)
        if tr is None:
            raise OSError("연구 요청은 모의투자 TR만 허용됩니다.")
        lease = _PaperTRLease(self.path.with_name(self.path.name + ".paper-" + tr), self.clock, self._wait)
        return lease if lease.try_acquire() else None


class _PaperTRLease:
    """Cross-process serialization for one restricted TR; timestamp-only state."""
    window = 1.05

    def __init__(self, path, clock, wait):
        self.path, self.clock, self.wait, self.fd = path, clock, wait, None

    def acquire(self):
        while True:
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                    raise OSError("TR 전송 제한 기록을 확인하지 못했습니다.")
                fcntl.flock(fd, fcntl.LOCK_EX)
                raw = os.read(fd, 4097)
                try:
                    times = json.loads(raw) if raw else []
                    if (not isinstance(times, list) or len(times) > 1
                            or any(type(t) not in (int, float) or not math.isfinite(t) or t < 0 for t in times)):
                        raise ValueError
                except (ValueError, TypeError):
                    raise OSError("TR 전송 제한 기록을 확인하지 못했습니다.") from None
                now = self.clock()
                if not times or times[0] > now or now - times[0] >= self.window:
                    self.fd = fd
                    return
                delay = max(0.001, self.window - (now - times[0]))
            finally:
                if self.fd != fd:
                    os.close(fd)
            self.wait.wait(delay)

    def try_acquire(self):
        """Acquire only when the lock and the per-TR cooldown are ready."""
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                raise OSError("TR 전송 제한 기록을 확인하지 못했습니다.")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return False
            raw = os.read(fd, 4097)
            try:
                times = json.loads(raw) if raw else []
                if (not isinstance(times, list) or len(times) > 1
                        or any(type(t) not in (int, float) or not math.isfinite(t) or t < 0 for t in times)):
                    raise ValueError
            except (ValueError, TypeError):
                raise OSError("TR 전송 제한 기록을 확인하지 못했습니다.") from None
            now = self.clock()
            if times and times[0] <= now and now - times[0] < self.window:
                return False
            self.fd = fd
            return True
        finally:
            if self.fd != fd:
                os.close(fd)

    def release(self, *, consumed=True):
        fd, self.fd = self.fd, None
        if fd is None:
            return
        try:
            if consumed:
                payload = json.dumps([self.clock()], separators=(",", ":")).encode("ascii")
                os.lseek(fd, 0, os.SEEK_SET)
                os.ftruncate(fd, 0)
                os.write(fd, payload)
                os.fsync(fd)
        finally:
            os.close(fd)


class BufferedResponse(io.BytesIO):
    """Each waiting caller gets its own response cursor and header copy."""
    def __init__(self, raw, headers, status):
        super().__init__(raw)
        self.headers = copy.deepcopy(headers)
        self.status = self.code = status

    def getcode(self):
        return self.status


class HTTPQueue:
    def __init__(self, *, limiter=None, max_pending=512, max_active=16):
        self._limiter = limiter if limiter is not None else SharedRateLimit()
        self._max_pending, self._max_active = max_pending, max_active
        self._condition = threading.Condition()
        self._pending = deque()
        self._by_key = {}
        self._active = 0
        self._closed = False
        self._worker = None

    @staticmethod
    def _snapshot(opener, request, timeout, max_bytes):
        if (not isinstance(request, urllib.request.Request)
                or request.type != "https" or request.data is not None and not isinstance(request.data, bytes)
                or type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0
                or type(max_bytes) is not int or not 0 < max_bytes <= _MAX_BYTES):
            raise ValueError("HTTP 요청 형식이 올바르지 않습니다.")
        headers = dict(request.header_items())
        defaults = getattr(opener, "addheaders", ())
        if isinstance(defaults, (list, tuple)):
            for key, value in defaults:
                if key.lower() not in {k.lower() for k in headers}:
                    headers[key] = value
        frozen = urllib.request.Request(request.full_url, data=request.data,
                                        headers=headers, method=request.get_method())
        canonical = (request.get_method(), request.full_url,
                     sorted((k.lower(), v) for k, v in headers.items()),
                     timeout, max_bytes)
        digest = hashlib.sha256(json.dumps(canonical, ensure_ascii=True).encode("utf-8"))
        digest.update(b"\0")
        digest.update(b"NONE" if request.data is None else b"BODY" + request.data)
        return digest.digest(), frozen

    def submit(self, opener, request, *, timeout=15, max_bytes=_MAX_BYTES, before_send=None, priority="normal"):
        if priority not in ("normal", "research"):
            raise ValueError("HTTP 요청 우선순위를 확인하지 못했습니다.")
        key, request = self._snapshot(opener, request, timeout, max_bytes)
        # Keep the first pending operation, including its pre-send guard.
        with self._condition:
            if self._closed:
                raise OSError("HTTP 대기열이 종료되었습니다.")
            if key in self._by_key:
                return self._by_key[key]["future"]
            if len(self._pending) >= self._max_pending:
                raise OSError("HTTP 대기열이 가득 찼습니다. 요청을 전송하지 않았습니다.")
            ticket = {"key": key, "opener": opener, "request": request,
                      "timeout": timeout, "max_bytes": max_bytes,
                      "future": Future(), "before_send": before_send, "priority": priority}
            self._pending.append(ticket)
            self._by_key[key] = ticket
            if self._worker is None:
                self._worker = threading.Thread(target=self._dispatch, name="http-queue", daemon=True)
                self._worker.start()
            self._condition.notify_all()
            return ticket["future"]

    def acquire_slot(self):
        """FIFO entry for the shared engine's HTTP Upgrade handshake."""
        with self._condition:
            if self._closed or len(self._pending) >= self._max_pending:
                raise OSError("HTTP 대기열을 사용할 수 없습니다.")
            ticket = {"key": object(), "future": Future(), "before_send": None, "slot_only": True}
            self._pending.append(ticket)
            self._by_key[ticket["key"]] = ticket
            if self._worker is None:
                self._worker = threading.Thread(target=self._dispatch, name="http-queue", daemon=True)
                self._worker.start()
            self._condition.notify_all()
        ticket["future"].result()

    def open(self, opener, request, *, timeout=15, max_bytes=_MAX_BYTES, before_send=None, priority="normal"):
        result = self.submit(opener, request, timeout=timeout, max_bytes=max_bytes,
                             before_send=before_send, priority=priority).result()
        return BufferedResponse(*result)

    def _dispatch(self):
        while True:
            with self._condition:
                self._condition.wait_for(lambda: self._closed or self._pending and self._active < self._max_active)
                if self._closed:
                    return
                # Keep normal FIFO while allowing it to pass a low-priority
                # research request whose broker TR is currently busy.
                ticket = next((item for item in self._pending if item.get("priority") != "research"), self._pending[0])
            def check_guard():
                if ticket["before_send"] is not None:
                    try:
                        ticket["before_send"]()
                    except Exception as error:
                        raise HTTPNotSentError("대기 중 실행 조건이 변경되어 HTTP 요청을 전송하지 않았습니다.",
                                               reason_code=extract_guard_reason_code(error)) from None
            try:
                paper_lease = None
                if isinstance(self._limiter, SharedRateLimit):
                    if ticket.get("priority") == "research":
                        check_guard()
                        paper_lease = self._limiter.try_reserve_research_tr(ticket.get("request"))
                        if paper_lease is None:
                            # Wake promptly for new normal work and recheck the
                            # research deadline/cancellation guard on each pass.
                            with self._condition:
                                if not self._closed:
                                    self._condition.wait(timeout=0.05)
                            continue
                        try:
                            self._limiter.acquire(before_send=check_guard)
                        except BaseException:
                            paper_lease.release(consumed=False)
                            raise
                    else:
                        paper_lease = self._limiter.acquire_for_request(ticket.get("request"), before_send=check_guard)
                else:
                    self._limiter.acquire(before_send=check_guard)
            except Exception as error:
                with self._condition:
                    if self._closed:
                        return
                    self._pending.remove(ticket)
                    self._by_key.pop(ticket["key"], None)
                ticket["future"].set_exception(error if isinstance(error, HTTPNotSentError) else
                                                OSError("HTTP 전송 제한을 확인하지 못해 요청을 보내지 않았습니다."))
                continue
            with self._condition:
                if self._closed:
                    if paper_lease is not None:
                        paper_lease.release(consumed=False)
                    return
                self._pending.remove(ticket)
                self._by_key.pop(ticket["key"], None)
                self._active += 1
            ticket["paper_lease"] = paper_lease
            # Waiting for response headers/body must not block Telegram polling
            # or requests to the other host. Only dispatches share the limiter.
            started = threading.Event()
            worker = threading.Thread(target=self._exchange, args=(ticket, started), name="http-exchange", daemon=True)
            try:
                worker.start()
            except Exception:
                if paper_lease is not None:
                    paper_lease.release(consumed=False)
                ticket["future"].set_exception(OSError("HTTP 전송 작업을 시작하지 못해 요청을 보내지 않았습니다."))
                with self._condition:
                    self._active -= 1
                    self._condition.notify_all()
                continue
            started.wait()

    def _exchange(self, ticket, started):
        result, failure, sent = None, None, False
        try:
            if ticket.get("paper_lease") is not None and ticket["before_send"] is not None:
                try:
                    ticket["before_send"]()
                except Exception as error:
                    raise HTTPNotSentError("전송 직전 실행 조건이 변경되어 HTTP 요청을 전송하지 않았습니다.",
                                           reason_code=extract_guard_reason_code(error)) from None
            started.set()
            if ticket.get("slot_only"):
                result = None
            else:
                sent = True
                with ticket["opener"].open(ticket["request"], timeout=ticket["timeout"]) as response:
                    raw = response.read(ticket["max_bytes"] + 1)
                    if not isinstance(raw, bytes):
                        raise ValueError
                    result = (raw, copy.deepcopy(response.headers), getattr(response, "status", 200))
        except urllib.error.HTTPError as error:
            code = error.code if type(error.code) is int and 100 <= error.code <= 599 else None
            try:
                error.close()
            except Exception:
                pass
            # Reconstruct a sanitized exception: never retain token URLs/body.
            failure = (urllib.error.HTTPError("", code, "HTTP request failed", {}, io.BytesIO(b""))
                       if code is not None else OSError("HTTP 요청에 실패했습니다."))
        except HTTPNotSentError as error:
            failure = error
        except Exception:
            failure = OSError("HTTP 요청을 완료하지 못했습니다. 자동 재시도하지 않습니다.")
        finally:
            if ticket.get("paper_lease") is not None:
                try:
                    ticket["paper_lease"].release(consumed=sent)
                except Exception:
                    failure = OSError("TR 전송 완료 시각을 기록하지 못했습니다. 자동 재시도하지 않습니다.")
            if failure is None:
                ticket["future"].set_result(result)
            else:
                ticket["future"].set_exception(failure)
            started.set()
            with self._condition:
                self._active -= 1
                self._condition.notify_all()

    def close(self):
        """Reject unsent work; already-dispatched exchanges finish once."""
        with self._condition:
            self._closed = True
            for ticket in self._pending:
                ticket["future"].set_exception(OSError("HTTP 대기열이 종료되어 요청을 보내지 않았습니다."))
            self._pending.clear()
            self._by_key.clear()
            self._condition.notify_all()


_default_lock = threading.Lock()
_default_queue = None
_default_pid = None


def queued_open(opener, request, *, timeout=15, max_bytes=_MAX_BYTES, before_send=None, priority="normal", queue=None):
    # Explicit isolated queues are for non-broker auxiliary services. Existing
    # callers retain the process-shared broker limiter and FIFO unchanged.
    if queue is not None:
        if not isinstance(queue, HTTPQueue):
            raise ValueError("HTTP 대기열 형식을 확인하지 못했습니다.")
        host = urlsplit(request.full_url).hostname if isinstance(request, urllib.request.Request) else None
        if host is None or host == "kiwoom.com" or host.endswith(".kiwoom.com"):
            raise ValueError("키움 요청은 공용 HTTP 대기열을 사용해야 합니다.")
        return queue.open(opener, request, timeout=timeout, max_bytes=max_bytes,
                          before_send=before_send, priority=priority)
    global _default_queue, _default_pid
    with _default_lock:
        if _default_queue is None or _default_pid != os.getpid():
            _default_queue, _default_pid = HTTPQueue(), os.getpid()
        queue = _default_queue
    return queue.open(opener, request, timeout=timeout, max_bytes=max_bytes, before_send=before_send, priority=priority)


def acquire_http_slot():
    """Share the host-wide HTTP start limit for the sole WebSocket handshake.

    The WebSocket engine serializes and coalesces its connection attempts; only
    its HTTP Upgrade handshake consumes this slot, not subsequent WS frames.
    """
    global _default_queue, _default_pid
    with _default_lock:
        if _default_queue is None or _default_pid != os.getpid():
            _default_queue, _default_pid = HTTPQueue(), os.getpid()
        queue = _default_queue
    queue.acquire_slot()
