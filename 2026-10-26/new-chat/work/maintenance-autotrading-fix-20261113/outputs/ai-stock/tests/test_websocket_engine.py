"""Shared WebSocket engine contracts using an injected in-memory transport."""

import asyncio
import json
import threading
import time
import types
import unittest
from unittest.mock import Mock, patch

from kiwoom_autotrader.realtime import engine as engine_module
from kiwoom_autotrader.realtime.engine import (
    SharedWebSocketEngine, Subscription, WebSocketEngineError, _default_connector,
)


def wait_until(predicate, timeout=2):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError("offline WebSocket condition did not become true")


class FakeSocket:
    def __init__(self, *, login_code=0, auto_login=True, auto_control=True, close_fails=False, close_delay=0.002):
        self.loop = asyncio.get_running_loop()
        self.incoming = asyncio.Queue()
        self.sent = []
        self.closed = False
        self.login_code = login_code
        self.auto_login, self.auto_control = auto_login, auto_control
        self.close_fails = close_fails
        self.close_delay = close_delay
        self.sending = self.receiving = 0
        self.max_sends = self.max_receives = 0

    async def send(self, raw):
        self.sending += 1
        self.max_sends = max(self.max_sends, self.sending)
        try:
            await asyncio.sleep(0.001)
            packet = json.loads(raw)
            self.sent.append(packet)
            if packet["trnm"] == "LOGIN" and self.auto_login:
                await self.incoming.put({"trnm": "LOGIN", "return_code": self.login_code})
            if packet["trnm"] in {"REG", "REMOVE"} and self.auto_control:
                await self.incoming.put({"trnm": packet["trnm"], "return_code": "0"})
        finally:
            self.sending -= 1

    async def recv(self):
        self.receiving += 1
        self.max_receives = max(self.max_receives, self.receiving)
        try:
            value = await self.incoming.get()
            if isinstance(value, Exception):
                raise value
            return value if isinstance(value, (str, bytes)) else json.dumps(value)
        finally:
            self.receiving -= 1

    async def close(self):
        await asyncio.sleep(self.close_delay)
        if self.close_fails:
            raise OSError("sensitive-close-error")
        self.closed = True

    def push(self, value):
        self.loop.call_soon_threadsafe(self.incoming.put_nowait, value)

    def packets(self, trnm):
        return [packet for packet in self.sent if packet["trnm"] == trnm]


class FakeConnector:
    def __init__(self, **options):
        self.options = options
        self.sockets, self.urls = [], []
        self.max_open = 0

    async def __call__(self, url):
        self.urls.append(url)
        socket = FakeSocket(**self.options)
        self.sockets.append(socket)
        self.max_open = max(self.max_open, sum(not value.closed for value in self.sockets))
        return socket


def real(kind="0B", item="005930", value="70000"):
    return {"trnm": "REAL", "data": [{"type": kind, "item": item, "name": "주식체결", "values": {"10": value}}]}


class WebSocketEngineTests(unittest.TestCase):
    def setUp(self):
        self.session = ["paper", "offline-websocket-secret-token", True]
        self.connector = FakeConnector()
        self.slot = Mock()
        self.engines = []
        self.network = patch("socket.socket.connect", side_effect=AssertionError("live network forbidden"))
        self.network.start()
        self.addCleanup(self.network.stop)
        self.addCleanup(self.cleanup)

    def cleanup(self):
        for engine in self.engines:
            engine.close()

    def engine(self, **options):
        result = SharedWebSocketEngine(lambda: tuple(self.session), connector=options.pop("connector", self.connector),
                                       acquire_slot=options.pop("acquire_slot", self.slot),
                                       reconnect_delays=options.pop("reconnect_delays", (0.01, 0.02)),
                                       ack_timeout=options.pop("ack_timeout", 0.2), poll_interval=0.01, **options)
        self.engines.append(result)
        return result

    def connected(self, engine, count=1):
        wait_until(lambda: len(self.connector.sockets) >= count and bool(self.connector.sockets[count - 1].packets("REG")))
        return self.connector.sockets[count - 1]

    def test_initialization_and_session_sync_are_lazy(self):
        engine = self.engine()
        engine.sync_session()
        self.assertEqual(engine.snapshot()["status"], "idle")
        self.assertEqual(self.connector.urls, [])
        self.slot.assert_not_called()
        engine.close()
        with self.assertRaises(WebSocketEngineError):
            engine.subscribe(Subscription(("005930",), ("0B",)), lambda _: None)

    def test_disconnect_telemetry_does_not_store_exception_text(self):
        class PeerClosed(Exception):
            rcvd=types.SimpleNamespace(code=1001)
        engine=self.engine(reconnect_delays=(1,))
        engine.subscribe(Subscription(("",),("00",)),lambda _:None)
        ws=self.connected(engine)
        wait_until(lambda:engine.snapshot()['authenticated'])
        self.assertIsNotNone(engine.snapshot()['last_rx_age_ms'])
        ws.push(PeerClosed('offline-websocket-secret-token private-account'))
        wait_until(lambda:engine.snapshot()['trigger_detail']=='ws_close_frame')
        state=engine.snapshot()
        self.assertEqual(state['ws_close_code'],1001)
        self.assertGreaterEqual(state['last_rx_age_ms'],0)
        self.assertNotIn('private-account',json.dumps(state))
        self.assertNotIn('offline-websocket-secret-token',json.dumps(state))

    def test_simultaneous_subscribers_share_one_handshake_and_login_precedes_reg(self):
        gate = threading.Event()
        engine = self.engine(acquire_slot=lambda: (self.slot(), gate.wait(1)))
        handles = []
        workers = [threading.Thread(target=lambda: handles.append(engine.subscribe(Subscription(("005930",), ("0B",)), lambda _: None)))
                   for _ in range(20)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()
        gate.set()
        socket = self.connected(engine)
        self.assertEqual(len(handles), 20)
        self.assertEqual(len(self.connector.urls), 1)
        self.slot.assert_called_once()
        self.assertEqual(socket.sent[0], {"trnm": "LOGIN", "token": self.session[1]})
        self.assertEqual(socket.packets("REG"), [{"trnm": "REG", "grp_no": "1", "refresh": "0",
                                                   "data": [{"item": ["005930"], "type": ["0B"]}]}])
        self.assertEqual(engine.snapshot()["pair_count"], 1)
        self.assertEqual(self.connector.urls[0], "wss://mockapi.kiwoom.com:10000/api/websocket")

    def test_reference_counts_only_remove_last_consumer(self):
        self.connector.options["close_delay"] = 0.03
        engine, first, second = self.engine(), [], []
        sub = Subscription(("005930",), ("0B",))
        one = engine.subscribe(sub, first.append)
        socket = self.connected(engine)
        two = engine.subscribe(sub, second.append)
        time.sleep(0.03)
        self.assertEqual(len(socket.packets("REG")), 1)
        engine.unsubscribe(one)
        socket.push(real())
        wait_until(lambda: len(second) == 1)
        self.assertEqual(first, [])
        self.assertEqual(socket.packets("REMOVE"), [])
        self.assertTrue(engine.unsubscribe(two))
        self.assertFalse(engine.unsubscribe(two))
        wait_until(lambda: socket.closed)
        self.assertEqual(socket.packets("REMOVE")[0]["data"], [{"item": ["005930"], "type": ["0B"]}])
        self.assertEqual(socket.packets("REMOVE")[0]["refresh"], "1")

    def test_overlapping_sets_share_pairs_and_only_remove_unused_pair(self):
        engine = self.engine()
        one = engine.subscribe(Subscription(("005930", "000660"), ("0B",)), lambda _: None)
        socket = self.connected(engine)
        engine.subscribe(Subscription(("005930",), ("0B",)), lambda _: None)
        engine.unsubscribe(one)
        wait_until(lambda: len(socket.packets("REMOVE")) == 1)
        self.assertEqual(socket.packets("REMOVE")[0]["data"], [{"item": ["000660"], "type": ["0B"]}])
        self.assertEqual(len(socket.packets("REG")), 1)
        self.assertFalse(socket.closed)

    def test_type_case_and_empty_item_wildcard_are_preserved(self):
        engine, upper, lower, account = self.engine(), [], [], []
        engine.subscribe(Subscription(("005930",), ("0G",)), upper.append)
        engine.subscribe(Subscription(("005930",), ("0g",)), lower.append)
        engine.subscribe(Subscription(("",), ("00",)), account.append)
        socket = self.connected(engine)
        socket.push(real("0g"))
        socket.push(real("00", "000660"))
        wait_until(lambda: len(lower) == 1 and len(account) == 1)
        self.assertEqual(upper, [])
        self.assertEqual(account[0]["item"], "000660")

    def test_callback_failure_and_mutation_do_not_affect_other_consumers(self):
        engine, received = self.engine(), []
        def broken(row):
            row["values"]["10"] = "mutated"
            raise RuntimeError("sensitive-callback-error")
        engine.subscribe(Subscription(("005930",), ("0B",)), broken)
        engine.subscribe(Subscription(("005930",), ("0B",)), received.append)
        socket = self.connected(engine)
        socket.push(real())
        socket.push(real(value="71000"))
        wait_until(lambda: len(received) == 2)
        self.assertEqual([row["values"]["10"] for row in received], ["70000", "71000"])
        self.assertEqual(engine.snapshot()["callback_errors"], 2)
        self.assertNotIn("sensitive-callback-error", str(engine.snapshot()))
        self.assertEqual(socket.max_receives, 1)

    def test_ping_echo_is_exact_and_sends_are_serialized_while_ack_pending(self):
        self.connector.options["auto_control"] = False
        engine = self.engine()
        engine.subscribe(Subscription(("005930",), ("0B",)), lambda _: None)
        socket = self.connected(engine)
        engine.subscribe(Subscription(("000660",), ("0B",)), lambda _: None)
        ping = {"trnm": "PING", "nonce": "fixture", "nested": {"keep": 1}}
        socket.push(ping)
        wait_until(lambda: bool(socket.packets("PING")))
        self.assertEqual(socket.packets("PING"), [ping])
        self.assertEqual(len(socket.packets("REG")), 1)
        socket.push({"trnm": "REG", "return_code": 0})
        wait_until(lambda: len(socket.packets("REG")) == 2)
        socket.push({"trnm": "REG", "return_code": 0})
        self.assertEqual(socket.max_sends, 1)

    def test_mode_and_token_changes_close_old_socket_before_new_login(self):
        engine = self.engine()
        engine.subscribe(Subscription(("005930",), ("0B",)), lambda _: None)
        first = self.connected(engine)
        self.session[:] = ["real", "new-offline-token", True]
        engine.sync_session()
        second = self.connected(engine, 2)
        self.assertTrue(first.closed)
        self.assertEqual(self.connector.max_open, 1)
        self.assertEqual(self.connector.urls[1], "wss://api.kiwoom.com:10000/api/websocket")
        self.assertEqual(second.sent[0]["token"], "new-offline-token")
        self.assertEqual(second.packets("REG")[0]["refresh"], "0")
        self.assertEqual(self.slot.call_count, 2)
        self.session[2] = False
        engine.sync_session()
        wait_until(lambda: second.closed and engine.snapshot()["status"] == "waiting_login")
        self.assertEqual(len(self.connector.urls), 2)

    def test_missing_login_keeps_subscription_without_connecting(self):
        self.session[:] = [None, None, False]
        engine = self.engine()
        engine.subscribe(Subscription(("005930",), ("0B",)), lambda _: None)
        wait_until(lambda: engine.snapshot()["status"] == "waiting_login")
        self.assertEqual(self.connector.urls, [])
        self.session[:] = ["paper", "fresh-offline-token", True]
        engine.sync_session()
        self.connected(engine)

    def test_removing_last_subscriber_cancels_pending_handshake(self):
        started, cancelled = threading.Event(), threading.Event()
        async def connector(_url):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        engine = self.engine(connector=connector)
        handle = engine.subscribe(Subscription(("005930",), ("0B",)), lambda _: None)
        self.assertTrue(started.wait(1))
        engine.unsubscribe(handle)
        self.assertTrue(cancelled.wait(1))
        wait_until(lambda: engine.snapshot()["status"] == "idle")
        self.assertFalse(engine.snapshot()["connected"])

    def test_removing_last_subscriber_cancels_pending_login_without_registration(self):
        self.connector.options["auto_login"] = False
        engine = self.engine()
        handle = engine.subscribe(Subscription(("005930",), ("0B",)), lambda _: None)
        wait_until(lambda: bool(self.connector.sockets) and bool(self.connector.sockets[0].packets("LOGIN")))
        socket = self.connector.sockets[0]
        engine.unsubscribe(handle)
        wait_until(lambda: socket.closed and engine.snapshot()["status"] == "idle")
        self.assertEqual(socket.packets("REG"), [])

    def test_session_change_while_handshake_slot_waits_never_opens_old_server(self):
        gate, started = threading.Event(), threading.Event()
        def acquire():
            self.slot()
            started.set()
            gate.wait(1)
        engine = self.engine(acquire_slot=acquire)
        engine.subscribe(Subscription(("005930",), ("0B",)), lambda _: None)
        self.assertTrue(started.wait(1))
        self.session[:] = ["real", "replacement-offline-token", True]
        engine.sync_session()
        time.sleep(0.03)
        gate.set()
        socket = self.connected(engine)
        self.assertEqual(self.connector.urls, ["wss://api.kiwoom.com:10000/api/websocket"])
        self.assertEqual(socket.sent[0]["token"], "replacement-offline-token")

    def test_disconnect_reconnects_with_login_then_restores_subscription(self):
        engine = self.engine()
        engine.subscribe(Subscription(("005930",), ("0B",)), lambda _: None)
        first = self.connected(engine)
        first.push(ConnectionError("private-error"))
        second = self.connected(engine, 2)
        self.assertTrue(first.closed)
        self.assertEqual([packet["trnm"] for packet in second.sent[:2]], ["LOGIN", "REG"])
        self.assertEqual(first.packets("REG")[0], second.packets("REG")[0])
        self.assertEqual(self.connector.max_open, 1)
        self.assertNotIn("private-error", str(engine.snapshot()))

    def test_reconnect_budget_is_bounded_and_plain_sync_does_not_reset_it(self):
        attempts = []
        async def broken(url):
            attempts.append(url)
            raise OSError("secret-network-failure")
        engine = self.engine(connector=broken)
        engine.subscribe(Subscription(("005930",), ("0B",)), lambda _: None)
        wait_until(lambda: engine.snapshot()["status"] == "failed")
        self.assertEqual(len(attempts), 3)
        self.assertEqual(self.slot.call_count, 3)
        engine.sync_session()
        time.sleep(0.04)
        self.assertEqual(len(attempts), 3)
        self.assertNotIn("secret-network-failure", repr(engine) + str(engine.snapshot()))

    def test_login_ack_must_explicitly_succeed_before_registration(self):
        for code in (False, None, 1, "1"):
            with self.subTest(code=code):
                connector = FakeConnector(login_code=code)
                engine = self.engine(connector=connector)
                engine.subscribe(Subscription(("005930",), ("0B",)), lambda _: None)
                wait_until(lambda: engine.snapshot()["status"] == "authentication_failed")
                self.assertEqual(connector.sockets[0].packets("REG"), [])
                time.sleep(0.02)
                self.assertEqual(len(connector.sockets), 1)
                engine.close()

    def test_only_documented_ip_auth_codes_receive_safe_annotation_after_close(self):
        for code in (8010, "8050", 8001):
            with self.subTest(code=code):
                connector = FakeConnector(login_code=code)
                def annotate(value, local_message):
                    self.assertTrue(connector.sockets[0].closed)
                    self.assertIn("인증이 거절", local_message)
                    return f"offline safe IP hint {value}"
                with patch.object(engine_module, "annotate_ip_error", side_effect=annotate) as helper:
                    engine = self.engine(connector=connector)
                    engine.subscribe(Subscription(("005930",), ("0B",)), lambda _: None)
                    wait_until(lambda: engine.snapshot()["status"] == "authentication_failed")
                    if str(code) in {"8010", "8050"}:
                        helper.assert_called_once()
                        self.assertIn("safe IP hint", engine.snapshot()["error"])
                    else:
                        helper.assert_not_called()
                    engine.close()

    def test_ack_timeout_is_bounded_and_never_marks_registration_success(self):
        self.connector.options["auto_login"] = False
        engine = self.engine(ack_timeout=0.03, reconnect_delays=())
        engine.subscribe(Subscription(("005930",), ("0B",)), lambda _: None)
        wait_until(lambda: engine.snapshot()["status"] == "failed")
        self.assertEqual(self.connector.sockets[0].packets("REG"), [])
        self.assertFalse(engine.snapshot()["authenticated"])

    def test_socket_cleanup_failure_prevents_any_replacement(self):
        self.connector.options["close_fails"] = True
        engine = self.engine()
        engine.subscribe(Subscription(("005930",), ("0B",)), lambda _: None)
        self.connected(engine)
        self.session[:] = ["real", "replacement-token", True]
        engine.sync_session()
        wait_until(lambda: engine.snapshot()["status"] == "failed")
        time.sleep(0.03)
        self.assertEqual(len(self.connector.sockets), 1)
        self.assertIn("종료를 확인하지 못해", engine.snapshot()["error"])

    def test_validation_and_shared_symbol_limit_have_no_network_effect(self):
        for items, kinds in (((), ("0B",)), (("005930",), ("0BB",)), (("005930",), ("00",)),
                             (("bad\nitem",), ("0B",)), (("005930",), ())):
            with self.subTest(items=items, kinds=kinds), self.assertRaises(ValueError):
                Subscription(items, kinds)
        self.session[2] = False
        engine = self.engine()
        engine.subscribe(Subscription(tuple(f"{i:06d}" for i in range(200)), ("0B",)), lambda _: None)
        with self.assertRaises(ValueError):
            engine.subscribe(Subscription(("999999",), ("0B",)), lambda _: None)
        self.assertEqual(self.connector.urls, [])
        self.assertEqual(engine.snapshot()["pair_count"], 200)

    def test_close_is_idempotent_and_stops_socket_and_workers(self):
        engine = self.engine()
        engine.subscribe(Subscription(("005930",), ("0B",)), lambda _: None)
        socket = self.connected(engine)
        engine.close()
        engine.close()
        self.assertTrue(socket.closed)
        self.assertEqual(engine.snapshot()["status"], "closed")
        self.assertFalse(engine._thread.is_alive())
        self.assertFalse(engine._callback_thread.is_alive())
        self.assertNotIn(self.session[1], repr(engine) + str(engine.snapshot()))

    def test_blocked_callback_does_not_block_ping_and_overflow_is_observable(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def callback(_row):
            entered.set()
            release.wait(1)
        engine = self.engine(callback_capacity=1)
        engine.subscribe(Subscription(("005930",), ("0B",)), callback)
        socket = self.connected(engine)
        socket.push(real())
        self.assertTrue(entered.wait(1))
        for _ in range(10):
            socket.push(real())
        socket.push({"trnm": "PING"})
        wait_until(lambda: engine.snapshot()["callback_dropped"] > 0 and bool(socket.packets("PING")))
        self.assertEqual(socket.max_receives, 1)
        release.set()

    def test_missing_optional_dependency_is_reported_without_repeated_handshakes(self):
        with patch.dict("sys.modules", {"websockets": None, "websockets.asyncio": None,
                                        "websockets.asyncio.client": None}), \
                patch.object(engine_module, "enable_account_dependencies") as loader:
            engine = self.engine(connector=_default_connector)
            engine.subscribe(Subscription(("005930",), ("0B",)), lambda _: None)
            wait_until(lambda: engine.snapshot()["status"] == "dependency_missing")
            loader.assert_called_once()
            self.slot.assert_called_once()
            self.assertFalse(engine.snapshot()["connected"])
            engine.close()

    def test_default_connector_disables_redirects_proxy_and_library_ping(self):
        instances = []
        class Connect:
            def __init__(self, url, **kwargs):
                self.url, self.options = url, kwargs
                instances.append(self)
            def __await__(self):
                async def ready():
                    return "fake-socket"
                return ready().__await__()
        module = types.ModuleType("websockets.asyncio.client")
        module.connect = Connect
        with patch.object(engine_module, "enable_account_dependencies") as loader, patch.dict("sys.modules", {"websockets": types.ModuleType("websockets"),
                                        "websockets.asyncio": types.ModuleType("websockets.asyncio"),
                                        "websockets.asyncio.client": module}):
            self.assertEqual(asyncio.run(_default_connector("wss://fixture.invalid")), "fake-socket")
            loader.assert_called_once()
        instance = instances[0]
        error = RuntimeError("redirect")
        self.assertIs(instance.process_redirect(error), error)
        self.assertIsNone(instance.options["proxy"])
        self.assertIsNone(instance.options["ping_interval"])
        self.assertEqual(instance.options["open_timeout"], 10)


if __name__ == "__main__":
    unittest.main()
