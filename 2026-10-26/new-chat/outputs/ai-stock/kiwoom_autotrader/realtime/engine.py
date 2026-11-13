"""One lazy WebSocket connection shared by all runtime REG subscribers.

Wire contract: project tr_docs/0B.md and 00.md; LOGIN/PING follow Kiwoom's
official tutorial. The workbook's /api/websocket URL and required REMOVE
refresh field take precedence over conflicting examples. No generic RPC or
condition-search client is implemented: control acknowledgements have no ID.

The application creates one engine and passes it to features. A session provider
returns (mode, token, token_usable), or a BrokerSession-like object. subscribe()
is nonblocking; synchronous callbacks receive copied REAL.data rows on a bounded
worker queue. Callback errors/overflow are observable through snapshot().
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from copy import deepcopy
from dataclasses import dataclass
import inspect
import json
import math
import queue
import re
import threading
import time
from typing import Callable
import uuid

from kiwoom_autotrader.utils.dependencies import enable_account_dependencies
from kiwoom_autotrader.utils.public_ip import annotate_ip_error
from .gap_diagnostics import classify_error, close_code


_URLS = {"paper": "wss://mockapi.kiwoom.com:10000/api/websocket",
         "real": "wss://api.kiwoom.com:10000/api/websocket"}
_MAX_MESSAGE = 1024 * 1024
_GROUP = "1"


class WebSocketEngineError(RuntimeError):
    """Local, secret-free engine or protocol failure."""


class _SessionChanged(Exception):
    pass


class _AuthenticationRejected(WebSocketEngineError):
    def __init__(self, message, *, code=None):
        super().__init__(message)
        self.code = code if type(code) in (int, str) and str(code) in {"8010", "8050"} else None


class _DependencyMissing(WebSocketEngineError):
    pass


class _CleanupFailed(WebSocketEngineError):
    pass


@dataclass(frozen=True)
class Subscription:
    items: tuple[str, ...]
    types: tuple[str, ...]

    def __post_init__(self):
        if (not isinstance(self.items, (tuple, list)) or not self.items
                or not isinstance(self.types, (tuple, list)) or not self.types):
            raise ValueError("WebSocket 구독에는 item/type 목록이 필요합니다.")
        if any(not isinstance(item, str) or len(item) > 200 or not item.isascii()
               or (item and not item.isprintable()) for item in self.items):
            raise ValueError("WebSocket item 형식이 올바르지 않습니다.")
        if any(not isinstance(kind, str) or re.fullmatch(r"[0-9A-Za-z]{2}", kind) is None for kind in self.types):
            raise ValueError("WebSocket type은 대소문자를 구분하는 두 자리 코드입니다.")
        items, types = tuple(sorted(set(self.items))), tuple(sorted(set(self.types)))
        if len(items) > 200 or len(types) > 64:
            raise ValueError("WebSocket 구독 항목 수가 제한을 초과했습니다.")
        if "00" in types and items != ("",):
            raise ValueError("주문체결 00 구독은 item을 빈 문자열로 지정하세요.")
        object.__setattr__(self, "items", items)
        object.__setattr__(self, "types", types)

    @property
    def pairs(self) -> frozenset[tuple[str, str]]:
        return frozenset((item, kind) for item in self.items for kind in self.types)


@dataclass(frozen=True)
class SubscriptionHandle:
    id: str


async def _default_connector(url: str):
    """Pinned websockets 15.0.1; redirects and proxies must not bypass slots."""
    try:
        enable_account_dependencies()
        from websockets.asyncio.client import connect
    except ImportError:
        raise _DependencyMissing("WebSocket 의존성이 없습니다. 프로젝트 의존성(websockets)을 설치하세요.") from None

    class _NoRedirectConnect(connect):
        def process_redirect(self, exc):
            return exc

    return await _NoRedirectConnect(url, proxy=None, open_timeout=10, close_timeout=2,
                                    ping_interval=None, max_size=_MAX_MESSAGE)


def _default_slot() -> None:
    from kiwoom_autotrader.utils.http_queue import acquire_http_slot
    acquire_http_slot()


class SharedWebSocketEngine:
    """Thread-safe synchronous facade over one asyncio connection/receive loop.

    An injected connector is async callable(url) -> socket with async
    send(str), recv(), close(). acquire_slot is a synchronous handshake limiter.
    All consumers must share this instance; it never obtains/renews credentials.
    """

    def __init__(self, session_provider: Callable, *, connector=None, acquire_slot=None,
                 reconnect_delays=(0.5, 1.0, 2.0, 4.0), ack_timeout=10.0,
                 poll_interval=0.1, callback_capacity=1024):
        if not callable(session_provider):
            raise ValueError("로그인 상태 제공 함수가 필요합니다.")
        delays = tuple(reconnect_delays)
        if (any(type(value) not in (int, float) or not math.isfinite(value) or value < 0 for value in delays)
                or type(ack_timeout) not in (int, float) or not math.isfinite(ack_timeout) or ack_timeout <= 0
                or type(poll_interval) not in (int, float) or not math.isfinite(poll_interval) or poll_interval <= 0
                or type(callback_capacity) is not int or callback_capacity < 1):
            raise ValueError("WebSocket 재시도·대기 설정이 올바르지 않습니다.")
        self._provider = session_provider
        self._connector = connector or _default_connector
        self._acquire_slot = acquire_slot or _default_slot
        self._delays, self._ack_timeout, self._poll = delays, ack_timeout, poll_interval
        self._lock = threading.RLock()
        self._callback_condition = threading.Condition(self._lock)
        self._draining = set()
        self._callback_pending = {}
        self._callback_ingress = {}
        self._registrations: dict[str, tuple[Subscription, Callable]] = {}
        self._revision = 0
        self._closed = False
        self._cleanup_failed = False
        self._thread = self._callback_thread = None
        self._loop = self._changed = None
        self._callbacks = queue.Queue(maxsize=callback_capacity)
        self._generation = 0
        self._active_identity = None
        self._registered_pairs = set()
        self._pending = None
        self._send_lock = None
        self._last_rx_monotonic = None
        self._state = {"status": "idle", "connected": False, "authenticated": False,
                       "reconnect_attempts": 0, "connection_count": 0,
                       "callback_errors": 0, "callback_dropped": 0, "error": None,
                       "ws_close_code": None, "trigger_detail": "unknown", "disconnect_generation": None,
                       "disconnect_last_rx_age_ms": None}

    def __repr__(self):
        state = self.snapshot()
        return f"SharedWebSocketEngine(status={state['status']!r}, subscriptions={state['subscription_count']})"

    def subscribe(self, subscription: Subscription, callback: Callable) -> SubscriptionHandle:
        if not isinstance(subscription, Subscription) or not callable(callback) or inspect.iscoroutinefunction(callback):
            raise ValueError("Subscription과 동기 콜백 함수를 지정하세요.")
        handle = SubscriptionHandle(uuid.uuid4().hex)
        with self._lock:
            if self._closed:
                raise WebSocketEngineError("WebSocket 엔진이 종료되었습니다.")
            items = {item for sub, _ in self._registrations.values() for item in sub.items if item}
            items.update(item for item in subscription.items if item)
            if len(items) > 200:
                raise ValueError("공유 WebSocket의 종목 수는 200개 이하여야 합니다.")
            self._registrations[handle.id] = (subscription, callback)
            self._callback_ingress[handle.id] = 0
            self._revision += 1
            if self._thread is None:
                self._callback_thread = threading.Thread(target=self._callback_worker, name="kiwoom-ws-callback", daemon=True)
                self._thread = threading.Thread(target=self._thread_main, name="kiwoom-ws", daemon=True)
                self._callback_thread.start()
                self._thread.start()
        self._notify()
        return handle

    def unsubscribe(self, handle: SubscriptionHandle) -> bool:
        if not isinstance(handle, SubscriptionHandle):
            raise ValueError("WebSocket 구독 핸들이 필요합니다.")
        with self._lock:
            existed = self._registrations.pop(handle.id, None) is not None
            if existed:
                self._callback_ingress.pop(handle.id, None)
                self._revision += 1
        if existed:
            self._notify()
        return existed

    def drain_and_unsubscribe(self, handle: SubscriptionHandle, *, timeout=2.0) -> bool:
        """Freeze this consumer's ingress, then drain already accepted callbacks.

        Other subscriptions keep receiving. A timeout unregisters the consumer
        but returns False: the owner must record a gap, never claim completeness.
        This must not run from the callback worker itself.
        """
        if (not isinstance(handle, SubscriptionHandle) or type(timeout) not in (int, float)
                or not math.isfinite(timeout) or timeout < 0):
            raise ValueError("구독 핸들과 유한한 종료 대기시간이 필요합니다.")
        deadline = time.monotonic() + timeout
        with self._callback_condition:
            if handle.id not in self._registrations:
                return False
            # _fanout enqueues/increments under this same lock: no late enqueue
            # can slip past the frozen ingress and a zero pending count.
            self._draining.add(handle.id)
            generation = self._generation
            failures = (self._state["callback_errors"], self._state["callback_dropped"])
            while (self._callback_pending.get(handle.id, 0) and not self._closed
                   and threading.current_thread() is not self._callback_thread):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._callback_condition.wait(remaining)
            complete = bool(not self._closed and handle.id in self._registrations
                            and self._callback_pending.get(handle.id, 0) == 0
                            and self._generation == generation and self._state["authenticated"]
                            and self._active_identity is not None and self._identity() == self._active_identity
                            and failures == (self._state["callback_errors"], self._state["callback_dropped"]))
            self._registrations.pop(handle.id, None)
            self._callback_ingress.pop(handle.id, None)
            self._draining.discard(handle.id)
            self._revision += 1
        self._notify()
        return complete

    def sync_session(self) -> None:
        """Wake reconciliation after login/renewal; it never logs in itself."""
        self._notify()

    def snapshot(self) -> dict:
        with self._lock:
            pairs = {pair for sub, _ in self._registrations.values() for pair in sub.pairs}
            age = None if self._last_rx_monotonic is None else max(0, int((time.monotonic() - self._last_rx_monotonic) * 1000))
            return {**self._state, "subscription_count": len(self._registrations), "pair_count": len(pairs),
                    "connection_generation": self._generation, "registered_pair_count": len(self._registered_pairs),
                    "last_rx_age_ms": age}

    def _record_disconnect(self, trigger, code=None):
        with self._lock:
            age = None if self._last_rx_monotonic is None else max(0, int((time.monotonic() - self._last_rx_monotonic) * 1000))
            self._state.update(ws_close_code=code, trigger_detail=trigger,
                               disconnect_generation=self._generation, disconnect_last_rx_age_ms=age)

    def subscription_ready(self, handle: SubscriptionHandle) -> bool:
        """True only after this subscription's REG acknowledgement, for this session."""
        with self._lock:
            registration = self._registrations.get(handle.id) if isinstance(handle, SubscriptionHandle) else None
            return bool(not self._closed and registration is not None and handle.id not in self._draining
                        and self._state["authenticated"]
                        and self._active_identity is not None and self._identity() == self._active_identity
                        and registration[0].pairs <= self._registered_pairs)

    def subscription_delivery_status(self, handle: SubscriptionHandle) -> dict:
        """Snapshot this subscription's accepted ingress and unfinished work.

        Enqueue, sequence increment and pending increment use the same lock.
        The pending count includes both queued and currently running callbacks.
        Sequence remains monotonic for the lifetime of this unique handle.
        """
        if not isinstance(handle, SubscriptionHandle):
            raise ValueError("WebSocket 구독 핸들이 필요합니다.")
        with self._lock:
            if self._closed or handle.id not in self._registrations or handle.id not in self._callback_ingress:
                raise WebSocketEngineError("WebSocket 구독 수신 대기 상태를 확인하지 못했습니다.")
            return {"ingress_sequence": self._callback_ingress[handle.id],
                    "pending_callbacks": self._callback_pending.get(handle.id, 0)}

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._registrations.clear()
            self._callback_ingress.clear()
            self._registered_pairs.clear()
            self._generation += 1
            self._revision += 1
            self._callback_condition.notify_all()
        self._notify()
        while True:
            try:
                key, _, _ = self._callbacks.get_nowait()
                self._callback_finished(key)
                self._callbacks.task_done()
            except queue.Empty:
                break
        for thread in (self._thread, self._callback_thread):
            if thread is not None and thread is not threading.current_thread():
                thread.join(timeout=5)
        self._set_state(status="closed", connected=False, authenticated=False)

    def _set_state(self, **values):
        with self._lock:
            self._state.update(values)

    def _notify(self):
        with self._lock:
            loop, changed = self._loop, self._changed
        if loop is not None and changed is not None:
            try:
                loop.call_soon_threadsafe(changed.set)
            except RuntimeError:
                pass

    def _identity(self):
        try:
            state = self._provider()
            if isinstance(state, tuple) and len(state) == 3:
                mode, token, usable = state
            else:
                mode, token, usable = state.mode, state.token, state.token_usable
            if (not usable or mode not in _URLS or not isinstance(token, str) or not 1 <= len(token) <= 8192
                    or not token.isascii() or any(not 33 <= ord(ch) <= 126 for ch in token)):
                return None
            return mode, token
        except Exception:
            return None

    def _desired(self):
        with self._lock:
            return {pair for sub, _ in self._registrations.values() for pair in sub.pairs}, self._revision

    def _thread_main(self):
        try:
            asyncio.run(self._main())
        except Exception:
            self._set_state(status="failed", connected=False, authenticated=False,
                            error="WebSocket 엔진 실행을 완료하지 못했습니다.")
        finally:
            with self._lock:
                self._loop = self._changed = None

    async def _wait_change(self, delay=None):
        self._changed.clear()
        try:
            await asyncio.wait_for(self._changed.wait(), timeout=self._poll if delay is None else delay)
        except asyncio.TimeoutError:
            pass

    async def _main(self):
        with self._lock:
            self._loop, self._changed = asyncio.get_running_loop(), asyncio.Event()
        failures, blocked, auth_blocked, last_identity = 0, None, None, None
        while not self._closed:
            if self._cleanup_failed:
                await self._wait_change()
                continue
            identity = self._identity()
            pairs, revision = self._desired()
            if identity != last_identity:
                failures, blocked, auth_blocked = 0, None, None
                last_identity = identity
            if not pairs or identity is None:
                self._set_state(status="idle" if not pairs else "waiting_login", connected=False, authenticated=False)
                await self._wait_change()
                continue
            if auth_blocked == identity or blocked == (identity, revision):
                await self._wait_change()
                continue
            if blocked is not None:
                failures, blocked = 0, None
            started = time.monotonic()
            try:
                await self._connection(identity)
                failures = 0
            except _SessionChanged:
                continue
            except _CleanupFailed:
                self._cleanup_failed = True
                self._set_state(status="failed", error="이전 WebSocket 연결 종료를 확인하지 못해 새 연결을 열지 않습니다.")
                continue
            except _AuthenticationRejected as error:
                auth_blocked = identity
                message = "WebSocket 인증이 거절되었습니다. 다시 로그인하세요."
                if error.code is not None:
                    # The rejected socket is already closed, so IP lookup
                    # cannot stall its single receive loop or PING handling.
                    try:
                        message = await asyncio.to_thread(annotate_ip_error, error.code, message)
                    except Exception:
                        pass
                self._set_state(status="authentication_failed", error=message)
                continue
            except _DependencyMissing:
                blocked = (identity, revision)
                self._set_state(status="dependency_missing", error="WebSocket 의존성이 없습니다. 프로젝트 의존성(websockets)을 설치하세요.")
                continue
            except Exception:
                if self._closed:
                    break
                if time.monotonic() - started >= 30:
                    failures = 0
                failures += 1
                self._set_state(reconnect_attempts=failures, error="WebSocket 연결 또는 구독 응답을 확인하지 못했습니다.")
                if failures > len(self._delays):
                    blocked = (identity, revision)
                    self._set_state(status="failed")
                else:
                    self._set_state(status="reconnecting")
                    # Subscription changes may wake this wait; ordinary polls
                    # never reset the bounded consecutive-attempt budget.
                    await self._wait_change(self._delays[failures - 1])
        self._set_state(status="closed", connected=False, authenticated=False)

    async def _watch_session(self, identity):
        while True:
            await asyncio.sleep(self._poll)
            if (self._closed or self._identity() != identity
                    or not self._desired()[0] and not self.snapshot()["authenticated"]):
                self._record_disconnect("session_changed")
                raise _SessionChanged

    async def _connection(self, identity):
        operation = asyncio.create_task(self._operate(identity))
        watcher = asyncio.create_task(self._watch_session(identity))
        try:
            done, _ = await asyncio.wait((operation, watcher), return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        finally:
            for task in (operation, watcher):
                task.cancel()
            results = await asyncio.gather(operation, watcher, return_exceptions=True)
            if any(isinstance(result, _CleanupFailed) for result in results):
                raise _CleanupFailed("이전 WebSocket 연결 종료를 확인하지 못했습니다.")

    async def _operate(self, identity):
        ws, reader, controller = None, None, None
        try:
            self._set_state(status="connecting")
            await asyncio.to_thread(self._acquire_slot)
            if self._closed or self._identity() != identity or not self._desired()[0]:
                raise _SessionChanged
            ws = await self._connector(_URLS[identity[0]])
            if self._closed or self._identity() != identity or not self._desired()[0]:
                raise _SessionChanged
            with self._lock:
                self._generation += 1
                self._last_rx_monotonic = None
                self._active_identity = identity
                self._registered_pairs.clear()
                self._state.update(connected=True, authenticated=False, status="authenticating",
                                   connection_count=self._state["connection_count"] + 1)
            self._pending, self._send_lock = None, asyncio.Lock()
            reader = asyncio.create_task(self._reader(ws))
            controller = asyncio.create_task(self._control(ws, identity))
            done, _ = await asyncio.wait((reader, controller), return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            trigger = ("authentication_rejected" if isinstance(error, _AuthenticationRejected) else
                       "session_changed" if isinstance(error, _SessionChanged) else
                       "protocol_error" if isinstance(error, WebSocketEngineError) else classify_error(error))
            self._record_disconnect(trigger, close_code(error))
            raise
        finally:
            cleanup = asyncio.create_task(self._cleanup_connection(ws, reader, controller))
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                # A session change may arrive after normal controller exit
                # while close is already running. Never abandon that close.
                await cleanup
                raise

    async def _cleanup_connection(self, ws, reader, controller):
        for task in (reader, controller):
            if task is not None:
                task.cancel()
        await asyncio.gather(*(task for task in (reader, controller) if task is not None), return_exceptions=True)
        self._pending = None
        with self._lock:
            self._generation += 1
            self._active_identity = None
            self._registered_pairs.clear()
            self._state.update(connected=False, authenticated=False)
        if ws is not None:
            try:
                await asyncio.wait_for(ws.close(), timeout=3)
            except Exception:
                # Never open a replacement while an old transport may
                # remain alive. The pinned implementation exposes these
                # transport primitives; injected transports can omit them.
                transport = getattr(ws, "transport", None)
                try:
                    if transport is None or not callable(getattr(transport, "abort", None)):
                        raise _CleanupFailed
                    transport.abort()
                    await asyncio.wait_for(ws.wait_closed(), timeout=1)
                except Exception:
                    raise _CleanupFailed("이전 WebSocket 연결 종료를 확인하지 못했습니다.") from None

    async def _send(self, ws, packet):
        async with self._send_lock:
            await asyncio.wait_for(ws.send(json.dumps(packet, ensure_ascii=True, separators=(",", ":"))), self._ack_timeout)

    async def _request(self, ws, packet):
        if self._pending is not None:
            raise WebSocketEngineError("WebSocket 제어 요청이 이미 대기 중입니다.")
        future = asyncio.get_running_loop().create_future()
        self._pending = (packet["trnm"], future)
        try:
            await self._send(ws, packet)
            await asyncio.wait_for(future, self._ack_timeout)
        finally:
            self._pending = None
            if not future.done():
                future.cancel()

    @staticmethod
    def _packet(trnm, pairs, *, refresh="1"):
        grouped = defaultdict(list)
        for item, kind in sorted(pairs):
            grouped[kind].append(item)
        return {"trnm": trnm, "grp_no": _GROUP, "refresh": refresh,
                "data": [{"item": items, "type": [kind]} for kind, items in sorted(grouped.items())]}

    async def _control(self, ws, identity):
        await self._request(ws, {"trnm": "LOGIN", "token": identity[1]})
        if self._identity() != identity:
            raise _SessionChanged
        self._set_state(authenticated=True, status="connected", error=None)
        registered, initial = set(), True
        while not self._closed:
            desired, _ = self._desired()
            removed = registered - desired
            if removed:
                await self._request(ws, self._packet("REMOVE", removed))
                registered -= removed
                with self._lock:
                    self._registered_pairs = set(registered)
            added = desired - registered
            if added:
                await self._request(ws, self._packet("REG", added, refresh="0" if initial else "1"))
                registered |= added
                with self._lock:
                    self._registered_pairs = set(registered)
                initial = False
            if not self._desired()[0]:
                return
            await self._wait_change()

    async def _reader(self, ws):
        while True:
            raw = await ws.recv()
            with self._lock:
                self._last_rx_monotonic = time.monotonic()
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            if not isinstance(raw, str) or len(raw.encode("utf-8")) > _MAX_MESSAGE:
                raise WebSocketEngineError("WebSocket 응답 크기가 제한을 초과했습니다.")
            packet = json.loads(raw)
            if not isinstance(packet, dict):
                raise WebSocketEngineError("WebSocket 응답 형식이 올바르지 않습니다.")
            trnm = packet.get("trnm")
            if trnm == "PING":
                await self._send(ws, packet)
            elif trnm in {"LOGIN", "REG", "REMOVE"}:
                pending = self._pending
                if pending is None or pending[0] != trnm or pending[1].done():
                    continue
                code = packet.get("return_code")
                if (type(code) is int and code == 0) or (type(code) is str and code == "0"):
                    pending[1].set_result(None)
                else:
                    error = (_AuthenticationRejected("WebSocket 인증이 거절되었습니다.", code=code) if trnm == "LOGIN"
                             else WebSocketEngineError("WebSocket 제어 요청이 거절되었습니다."))
                    pending[1].set_exception(error)
            elif trnm == "REAL" and self.snapshot()["authenticated"]:
                data = packet.get("data")
                if not isinstance(data, list):
                    raise WebSocketEngineError("WebSocket 실시간 목록 형식이 올바르지 않습니다.")
                for row in data:
                    if (not isinstance(row, dict) or not isinstance(row.get("type"), str)
                            or not isinstance(row.get("item"), str) or not isinstance(row.get("values"), dict)):
                        continue
                    self._fanout(row)

    def _fanout(self, row):
        with self._lock:
            generation = self._generation
            targets = [(key, sub) for key, (sub, _) in self._registrations.items()
                       if key not in self._draining and row["type"] in sub.types
                       and (row["item"] in sub.items or "" in sub.items)]
            for key, _ in targets:
                try:
                    self._callbacks.put_nowait((key, generation, deepcopy(row)))
                    self._callback_pending[key] = self._callback_pending.get(key, 0) + 1
                    self._callback_ingress[key] += 1
                except queue.Full:
                    self._state["callback_dropped"] += 1

    def _callback_finished(self, key):
        with self._callback_condition:
            pending = self._callback_pending.get(key, 0)
            if pending <= 1:
                self._callback_pending.pop(key, None)
            else:
                self._callback_pending[key] = pending - 1
            self._callback_condition.notify_all()

    def _callback_worker(self):
        while not self._closed:
            try:
                key, generation, row = self._callbacks.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                with self._lock:
                    registration = self._registrations.get(key)
                    valid = (registration is not None and generation == self._generation
                             and self._state["authenticated"] and not self._closed)
                    identity = self._active_identity
                if valid and self._identity() == identity:
                    result = registration[1](row)
                    if inspect.isawaitable(result):
                        if inspect.iscoroutine(result):
                            result.close()
                        raise TypeError("동기 콜백이 필요합니다.")
            except Exception:
                with self._lock:
                    self._state["callback_errors"] += 1
            finally:
                self._callback_finished(key)
                self._callbacks.task_done()
