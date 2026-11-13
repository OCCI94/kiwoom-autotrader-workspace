"""00 observation capture tests: no credentials, sockets, or broker requests."""
import asyncio
from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

from kiwoom_autotrader.realtime.engine import SharedWebSocketEngine, Subscription, SubscriptionHandle
from kiwoom_autotrader.realtime.execution_stream import ExecutionStreamRecorder


def wait_until(predicate, timeout=2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError("offline stream condition did not become true")


class FakeEngine:
    def __init__(self):
        self.registrations = {}
        self.ready = False
        self.generation = 1
        self.next_handle = 0
        self.callback_errors = self.callback_dropped = 0
        self.closed = False
        self.delivery = {}
        self.delivery_lock = threading.RLock()

    def subscribe(self, subscription, callback):
        self.next_handle += 1
        handle = SubscriptionHandle(str(self.next_handle))
        self.registrations[handle.id] = (subscription, callback)
        with self.delivery_lock:
            self.delivery[handle.id] = {"ingress_sequence": 0, "pending_callbacks": 0}
        return handle

    def unsubscribe(self, handle):
        existed = self.registrations.pop(handle.id, None) is not None
        with self.delivery_lock:
            self.delivery.pop(handle.id, None)
        return existed

    def drain_and_unsubscribe(self, handle, *, timeout=2):
        return self.unsubscribe(handle) and self.ready

    def subscription_ready(self, handle):
        return self.ready and handle.id in self.registrations

    def subscription_delivery_status(self, handle):
        with self.delivery_lock:
            return dict(self.delivery[handle.id])

    def snapshot(self):
        return {"connection_generation": self.generation, "callback_errors": self.callback_errors,
                "callback_dropped": self.callback_dropped}

    def emit(self, row):
        for key, (_, callback) in tuple(self.registrations.items()):
            with self.delivery_lock:
                if key not in self.delivery:
                    continue
                self.delivery[key]["ingress_sequence"] += 1
                self.delivery[key]["pending_callbacks"] += 1
            try:
                callback(deepcopy(row))
            finally:
                with self.delivery_lock:
                    if key in self.delivery:
                        self.delivery[key]["pending_callbacks"] -= 1


def row(**updates):
    values = {"9201": "1111222233", "9205": "secret-admin", "920": "screen-secret", "921": "terminal-secret",
              "9203": "0090284", "9001": "097520", "913": "체결", "302": "엠씨넥스",
              "900": "49", "902": "22", "903": "550800", "907": "1", "908": "105419",
              "909": "0000027", "910": "20400", "911": "27", "914": "20400", "915": "27",
              "922": "00", "2134": "1", "2135": "KRX", "10": "+20400", "unexpected": "never persist"}
    values.update(updates)
    return {"type": "00", "item": "097520", "name": "주문체결", "values": values,
            "unexpected": "never persist"}


class ExecutionStreamTests(unittest.TestCase):
    def setUp(self):
        self.network = patch("socket.socket.connect", side_effect=AssertionError("network forbidden"))
        self.network.start()
        self.addCleanup(self.network.stop)
        self.engine = FakeEngine()
        self.session = SimpleNamespace(mode="paper", token="offline-secret-token", token_usable=True,
                                       websocket_engine=self.engine)
        self.tracker = Mock()
        self.events = []
        self.tracker.record_broker_observation.side_effect = lambda event: self.events.append(deepcopy(event))
        self.now = datetime(2026, 10, 27, 10, 54, 22, tzinfo=ZoneInfo("Asia/Seoul"))
        self.recorder = ExecutionStreamRecorder(self.session, self.tracker, now=lambda: self.now, poll_interval=5)
        self.addCleanup(self.recorder.stop)

    def start(self):
        self.recorder.start()
        self.engine.ready = True
        self.assertTrue(self.recorder.status()["ready"])

    def test_shared_engine_single_account_wildcard_subscription(self):
        self.start()
        self.recorder.start()
        self.assertEqual(len(self.engine.registrations), 1)
        subscription = next(iter(self.engine.registrations.values()))[0]
        self.assertEqual(subscription, Subscription(("",), ("00",)))
        self.assertEqual([event["event_kind"] for event in self.events], ["stream_started"])

    def test_gap_telemetry_uses_bounded_current_transport_and_clock_bucket(self):
        from kiwoom_autotrader.realtime.execution_stream import valid_gap_values
        self.start()
        self.now = self.now.replace(hour=20, minute=9)
        original = self.engine.snapshot
        self.engine.snapshot = lambda:{**original(), 'ws_close_code':1001,'last_rx_age_ms':2450,
                                        'trigger_detail':'ws_close_frame','disconnect_generation':1,'disconnect_last_rx_age_ms':2450}
        self.engine.generation=2;self.engine.ready=False
        self.recorder.status()
        values = self.events[-1]['values']
        self.assertEqual(values['ws_close_code'],'1001')
        self.assertEqual(values['last_rx_age_ms'],'2450')
        self.assertEqual(values['session_phase'],'post_close_idle')
        self.assertEqual(values['trigger_detail'],'ws_close_frame')
        self.assertTrue(valid_gap_values(values))
        for key,value in [('ws_close_code','secret-token'),('last_rx_age_ms','-1'),
                          ('session_phase','server_confirmed_closed'),('trigger_detail','secret-token')]:
            self.assertFalse(valid_gap_values({**values,key:value}))
        self.assertTrue(valid_gap_values({}))

    def test_old_disconnect_metadata_does_not_explain_later_gap(self):
        self.start()
        self.engine.generation=3
        self.recorder._last_generation=3
        original=self.engine.snapshot
        self.engine.snapshot=lambda:{**original(),'ws_close_code':1001,'last_rx_age_ms':10,
                                     'trigger_detail':'ws_close_frame','disconnect_generation':1}
        self.engine.generation=4;self.engine.ready=False;self.recorder.status()
        self.assertEqual(self.events[-1]['values']['ws_close_code'],'unknown')
        self.assertEqual(self.events[-1]['values']['trigger_detail'],'unknown')

    def test_intermediate_unobserved_connection_cannot_explain_earlier_gap(self):
        self.start()
        original=self.engine.snapshot
        self.engine.snapshot=lambda:{**original(),'ws_close_code':1001,'last_rx_age_ms':10,
                                     'trigger_detail':'ws_close_frame','disconnect_generation':3}
        self.engine.generation=4;self.engine.ready=False;self.recorder.status()
        self.assertEqual(self.events[-1]['values']['ws_close_code'],'unknown')
        self.assertEqual(self.events[-1]['values']['last_rx_age_ms'],'unknown')

    def test_login_or_requested_subscription_is_not_registration_ack(self):
        status = self.recorder.start()
        self.assertFalse(status["ready"])
        self.assertFalse(status["coverage_complete"])
        self.assertEqual(self.events, [])
        self.engine.ready = True
        self.assertTrue(self.recorder.status()["ready"])

    def test_allowlist_keeps_unit_and_cumulative_fields_separate(self):
        self.start()
        original = row()
        self.engine.emit(original)
        event = self.events[-1]
        self.assertEqual(event["source_api"], "00")
        self.assertEqual(event["source_kind"], "broker_observation")
        self.assertEqual(event["observed_at"], "2026-10-27T10:54:22+09:00")
        self.assertEqual(event["day"], "20261027")
        self.assertEqual(event["mode"], "paper")
        self.assertEqual(event["values"]["909"], "0000027")
        self.assertEqual(event["values"]["914"], "20400")
        self.assertEqual(event["values"]["915"], "27")
        for field in ("910", "911", "903"):
            self.assertEqual(event["values"][field], original["values"][field])
        for field in ("9201", "9205", "920", "921", "10", "unexpected"):
            self.assertNotIn(field, event["values"])
        self.assertNotIn("1111222233", json.dumps(event))
        self.assertNotIn("offline-secret-token", repr(self.recorder))
        self.assertEqual(self.recorder.status()["last_classification"], "unit_execution")

    def test_missing_unit_values_stay_unconfirmed_without_inferred_fills(self):
        self.start()
        self.engine.emit(row(**{"909": "", "914": "", "915": "", "910": "20392", "911": "49", "902": "0"}))
        values = self.events[-1]["values"]
        self.assertEqual(values["910"], "20392")
        self.assertEqual(values["915"], "")
        self.assertNotIn("price", self.events[-1])
        self.assertNotIn("quantity", self.events[-1])
        self.assertEqual(self.recorder.status()["ambiguous_observations"], 1)
        self.assertEqual(self.recorder.status()["last_classification"], "unconfirmed_execution")
        self.assertFalse(self.recorder.status()["unit_details_complete"])
        self.tracker._ingest.assert_not_called()

    def test_order_acceptance_without_fill_is_valid_observation(self):
        self.start()
        self.engine.emit(row(**{"913": "접수", "909": "", "914": "", "915": "", "910": "", "911": ""}))
        self.assertEqual(self.recorder.status()["last_classification"], "order_update")
        self.assertEqual(self.recorder.status()["ambiguous_observations"], 0)

    def test_missing_order_symbol_or_time_keeps_unit_details_unconfirmed(self):
        self.start()
        for field in ("9203", "9001", "908"):
            with self.subTest(field=field):
                self.engine.emit(row(**{field: ""}))
                self.assertEqual(self.events[-1]["values"][field], "")
                self.assertEqual(self.recorder.status()["last_classification"], "unconfirmed_execution")
                self.assertFalse(self.recorder.status()["unit_details_complete"])

    def test_duplicate_deliveries_retain_execution_identity_without_summing(self):
        self.start()
        self.engine.emit(row())
        self.engine.emit(row())
        self.assertEqual(self.events[-1], self.events[-2])
        self.assertEqual(self.events[-1]["values"]["915"], "27")

    def test_ingress_is_visible_before_slow_persistence_completes(self):
        self.start()
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def slow_store(event):
            if event["event_kind"] == "execution_stream":
                entered.set()
                if not release.wait(2):
                    raise RuntimeError("fixture timeout")
            self.events.append(deepcopy(event))
        self.tracker.record_broker_observation.side_effect = slow_store
        worker = threading.Thread(target=lambda: self.engine.emit(row()))
        worker.start()
        self.assertTrue(entered.wait(1))
        status = self.recorder.status()
        self.assertTrue(status["ready"])
        self.assertEqual((status["ingress_sequence"], status["inflight_callbacks"], status["observations"]), (1,1,0))
        release.set(); worker.join(2)
        self.assertFalse(worker.is_alive())
        status = self.recorder.status()
        self.assertEqual((status["ingress_sequence"], status["inflight_callbacks"], status["observations"]), (1,0,1))

    def test_failed_persistence_releases_inflight_and_keeps_ingress_and_error(self):
        self.start()
        self.tracker.record_broker_observation.side_effect = OSError("private")
        self.engine.emit(row())
        status = self.recorder.status()
        self.assertFalse(status["ready"])
        self.assertEqual((status["ingress_sequence"], status["inflight_callbacks"], status["observations"]), (1,0,0))
        self.assertIsNotNone(status["error"])

    def test_storage_failure_latches_safe_error_and_blocks_readiness(self):
        self.start()
        self.tracker.record_broker_observation.side_effect = OSError("offline-secret-token secret-account")
        self.engine.emit(row())
        status = self.recorder.status()
        self.assertFalse(status["ready"])
        self.assertFalse(status["coverage_complete"])
        self.assertIsNotNone(status["error"])
        self.assertNotIn("secret", json.dumps(status))
        self.tracker.record_broker_observation.side_effect = lambda event: self.events.append(deepcopy(event))
        self.engine.emit(row())
        self.assertFalse(self.recorder.status()["ready"])

    def test_disconnect_and_reconnect_append_gap_without_claiming_recovery(self):
        self.start()
        self.engine.ready = False
        self.assertFalse(self.recorder.status()["ready"])
        self.assertEqual(self.recorder.status()["gap_count"], 1)
        self.engine.generation += 2
        self.engine.ready = True
        status = self.recorder.status()
        self.assertTrue(status["ready"])
        self.assertFalse(status["coverage_complete"])
        self.assertEqual([e["event_kind"] for e in self.events], ["stream_started", "stream_gap", "stream_resumed"])
        values = self.events[1]["values"]
        self.assertEqual(values["gap_reason"], "subscription_not_ready")
        self.assertEqual((values["prior_ready"], values["current_ready"]), ("true", "false"))

    def test_reconnect_between_polls_still_records_gap(self):
        self.start()
        self.engine.generation += 2
        status = self.recorder.status()
        self.assertTrue(status["ready"])
        self.assertEqual(status["gap_count"], 1)
        self.assertFalse(status["coverage_complete"])
        values = self.events[-2]["values"]
        self.assertEqual(values["gap_reason"], "connection_generation_changed")
        self.assertEqual(int(values["current_connection_generation"]) - int(values["prior_connection_generation"]), 2)

    def test_callback_loss_latches_error_even_when_socket_remains_ready(self):
        self.start()
        self.engine.callback_dropped = 1
        self.assertFalse(self.recorder.status()["ready"])
        self.assertEqual(self.recorder.status()["gap_count"], 1)
        self.assertIsNotNone(self.recorder.status()["error"])
        gap = next(event for event in self.events if event["event_kind"] == "stream_gap")
        self.assertEqual(gap["values"]["gap_reason"], "callback_dropped")
        self.assertEqual((gap["values"]["prior_callback_dropped"], gap["values"]["current_callback_dropped"]), ("0", "1"))

    def test_callback_error_or_drop_during_drain_marks_gap(self):
        self.start()
        def drain(handle, *, timeout):
            self.engine.callback_errors += 1
            self.engine.callback_dropped += 1
            self.engine.unsubscribe(handle)
            return True
        self.engine.drain_and_unsubscribe = drain
        self.recorder.stop()
        self.assertFalse(self.recorder.status()["coverage_complete"])
        self.assertIsNotNone(self.recorder.status()["error"])
        self.assertIn("stream_gap", [event["event_kind"] for event in self.events])

    def test_old_session_callback_never_enters_new_mode(self):
        self.start()
        old_callback = next(iter(self.engine.registrations.values()))[1]
        self.session.mode, self.session.token = "real", "different-secret-token"
        old_callback(row())
        self.assertEqual(self.recorder.status()["observations"], 0)
        self.engine.emit(row())
        self.assertEqual(self.events[-1]["mode"], "real")
        self.assertEqual(self.recorder.status()["gap_count"], 1)
        gaps = [event for event in self.events if event["event_kind"] == "stream_gap"]
        self.assertEqual(gaps[0]["values"]["gap_reason"], "session_identity_changed")
        self.assertEqual((gaps[0]["values"]["prior_mode"], gaps[0]["values"]["current_mode"]), ("paper", "real"))
        self.assertNotIn("offline-secret-token", json.dumps(gaps[0]))
        self.assertNotIn("different-secret-token", json.dumps(gaps[0]))
        old_callback(row())
        self.assertEqual(self.recorder.status()["observations"], 1)

    def test_start_before_login_keeps_mode_unknown_until_valid_session(self):
        self.session.mode, self.session.token, self.session.token_usable = None, None, False
        self.recorder.start()
        self.engine.ready = True
        self.engine.emit(row())
        self.assertEqual(self.events, [])
        self.assertFalse(self.recorder.status()["ready"])
        self.session.mode, self.session.token, self.session.token_usable = "paper", "new-secret-token", True
        self.assertTrue(self.recorder.status()["ready"])
        self.engine.emit(row())
        self.assertEqual(self.events[-1]["mode"], "paper")

    def test_secret_embedded_in_allowed_field_is_never_stored(self):
        self.start()
        self.engine.emit(row(**{"302": "offline-secret-token"}))
        self.assertEqual(len(self.events), 1)
        self.assertFalse(self.recorder.status()["ready"])
        self.assertNotIn("offline-secret-token", json.dumps(self.recorder.status()))

    def test_invalid_field_is_not_serialized_or_reported_in_error(self):
        self.start()
        self.engine.emit(row(**{"915": {"secret": "unexpected object"}}))
        self.assertEqual(len(self.events), 1)
        self.assertFalse(self.recorder.status()["ready"])

    def test_stop_releases_only_its_subscription_and_is_idempotent(self):
        self.engine.subscribe(Subscription(("005930",), ("0B",)), lambda _: None)
        self.start()
        self.recorder.stop()
        self.recorder.stop()
        self.assertEqual(len(self.engine.registrations), 1)
        self.assertFalse(self.engine.closed)
        self.assertFalse(self.recorder.status()["ready"])
        self.assertEqual(self.events[-1]["event_kind"], "stream_stopped")
        with self.assertRaises(ValueError):
            self.recorder.start()

    def test_real_tracker_fixture_preserves_units_and_empty_fields_without_fills(self):
        from kiwoom_autotrader.trading.execution_log import ExecutionTracker
        with tempfile.TemporaryDirectory() as directory:
            tracker = ExecutionTracker(log_dir=Path(directory), now=lambda: self.now,
                                       settings_loader=lambda: {}, account_fetcher=Mock(),
                                       executions_fetcher=Mock(), profit_fetcher=Mock(), account_scope="stream_test")
            self.recorder.tracker = tracker
            self.start()
            event = row()
            self.engine.emit(event)
            self.engine.emit(event)
            self.engine.emit(row(**{"913": "접수", "909": "", "914": "", "915": "", "910": "", "911": ""}))
            self.assertTrue(self.recorder.status()["ready"])
            self.recorder.stop()
            with sqlite3.connect(tracker.path) as database:
                payloads = [json.loads(r[0]) for r in database.execute("SELECT payload_json FROM broker_observations ORDER BY rowid")]
                self.assertEqual(database.execute("SELECT COUNT(*) FROM fills").fetchone()[0], 0)
                self.assertEqual(database.execute("SELECT COUNT(*) FROM orders").fetchone()[0], 0)
                with self.assertRaises(sqlite3.DatabaseError):
                    database.execute("DELETE FROM broker_observations")
            deliveries = [e for e in payloads if e["event_kind"] == "execution_stream"]
            self.assertEqual(len(deliveries), 3)
            self.assertEqual(deliveries[0]["values"]["909"], deliveries[1]["values"]["909"])
            self.assertEqual(deliveries[0]["values"]["915"], "27")
            self.assertEqual(deliveries[2]["values"]["915"], "")
            self.assertNotIn("1111222233", json.dumps(payloads))


class ManualAckSocket:
    def __init__(self):
        self.loop = asyncio.get_running_loop()
        self.incoming = asyncio.Queue()
        self.sent = []
        self.closed = False

    async def send(self, raw):
        self.sent.append(json.loads(raw))

    async def recv(self):
        value = await self.incoming.get()
        if isinstance(value, Exception):
            raise value
        return json.dumps(value)

    async def close(self):
        self.closed = True

    def push(self, value):
        self.loop.call_soon_threadsafe(self.incoming.put_nowait, value)


class EngineAcknowledgementTests(unittest.TestCase):
    def connected_recorder(self, store):
        sockets = []
        async def connector(url):
            socket = ManualAckSocket()
            sockets.append(socket)
            return socket
        session = SimpleNamespace(mode="paper", token="offline-token", token_usable=True)
        engine = SharedWebSocketEngine(lambda: session, connector=connector, acquire_slot=lambda: None,
                                       ack_timeout=2, poll_interval=0.01, reconnect_delays=())
        self.addCleanup(engine.close)
        session.websocket_engine = engine
        recorder = ExecutionStreamRecorder(session, SimpleNamespace(record_broker_observation=store), poll_interval=5)
        self.addCleanup(recorder.stop)
        recorder.start()
        wait_until(lambda: sockets and any(p["trnm"] == "LOGIN" for p in sockets[0].sent))
        socket = sockets[0]
        socket.push({"trnm": "LOGIN", "return_code": 0})
        wait_until(lambda: any(p["trnm"] == "REG" for p in socket.sent))
        socket.push({"trnm": "REG", "return_code": 0})
        wait_until(lambda: recorder.status()["ready"])
        return recorder, engine

    def test_queued_execution_blocks_gate_before_recorder_callback_begins(self):
        from kiwoom_autotrader.api.session import BrokerSession, IssuedToken
        from kiwoom_autotrader.trading.paper_reconciliation import PaperReconciliationError, PaperReconciliationGate
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        events = []
        with patch("socket.socket.connect", side_effect=AssertionError("network forbidden")):
            recorder, engine = self.connected_recorder(lambda event: events.append(deepcopy(event)))
            session = BrokerSession()
            session.mode = "paper"
            session._issued = IssuedToken("offline-token", "20300101000000")
            session.execution_recorder = recorder
            gate = PaperReconciliationGate(session, Mock(), executions_fetcher=Mock(return_value=()),
                account_fetcher=Mock(return_value={"mode": "paper", "exchange": "KRX", "snapshot_changed": False, "holdings": []}),
                pending_fetcher=Mock(return_value=[]))
            guard = gate("097520", quantity=1, price=100)
            def slow_callback(_):
                entered.set()
                release.wait(2)
            handle = engine.subscribe(Subscription(("005930",), ("0B",)), slow_callback)
            engine._fanout({"type": "0B", "item": "005930", "values": {}})
            self.assertTrue(entered.wait(1))
            engine._fanout(row())
            status = recorder.status()
            self.assertTrue(status["ready"])
            self.assertEqual(status["engine_pending_callbacks"], 1)
            self.assertEqual(status["engine_ingress_sequence"], 1)
            self.assertEqual(status["ingress_sequence"], 0)
            with self.assertRaises(PaperReconciliationError):
                guard()
            with self.assertRaises(PaperReconciliationError):
                gate("097520", quantity=1, price=100)
            release.set()
            wait_until(lambda: recorder.status()["engine_pending_callbacks"] == 0)
            self.assertEqual(recorder.status()["observations"], 1)
            with self.assertRaises(PaperReconciliationError):
                guard()
            gate("097520", quantity=1, price=100)()
            engine.unsubscribe(handle)

    def test_shutdown_drains_queued_executions_and_freezes_new_ingress(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        events = []
        def store(event):
            if event["event_kind"] == "execution_stream" and not entered.is_set():
                entered.set()
                release.wait(2)
            events.append(deepcopy(event))
        with patch("socket.socket.connect", side_effect=AssertionError("network forbidden")):
            recorder, engine = self.connected_recorder(store)
            engine._fanout(row())
            self.assertTrue(entered.wait(1))
            engine._fanout(row(**{"909": "0000028", "915": "22"}))
            stopper = threading.Thread(target=lambda: recorder.stop(timeout=1))
            stopper.start()
            wait_until(lambda: recorder.status()["stopping"])
            self.assertFalse(recorder.status()["ready"])
            wait_until(lambda: bool(engine._draining))
            engine._fanout(row(**{"909": "0000029", "915": "1"}))
            release.set()
            stopper.join(2)
            self.assertFalse(stopper.is_alive())
            deliveries = [event for event in events if event["event_kind"] == "execution_stream"]
            self.assertEqual([e["values"]["909"] for e in deliveries], ["0000027", "0000028"])
            self.assertEqual(events[-1]["event_kind"], "stream_stopped")
            self.assertEqual(recorder.status()["gap_count"], 0)
            self.assertTrue(recorder.status()["coverage_complete"])

    def test_shutdown_timeout_records_gap_and_does_not_claim_complete(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        events = []
        def store(event):
            if event["event_kind"] == "execution_stream":
                entered.set()
                release.wait(2)
            events.append(deepcopy(event))
        with patch("socket.socket.connect", side_effect=AssertionError("network forbidden")):
            recorder, engine = self.connected_recorder(store)
            engine._fanout(row())
            self.assertTrue(entered.wait(1))
            engine._fanout(row(**{"909": "0000028"}))
            started = time.monotonic()
            recorder.stop(timeout=0.03)
            self.assertLess(time.monotonic() - started, 0.5)
            self.assertFalse(recorder.status()["ready"])
            self.assertFalse(recorder.status()["coverage_complete"])
            self.assertGreaterEqual(recorder.status()["gap_count"], 1)
            self.assertIn("stream_gap", [event["event_kind"] for event in events])
            release.set()

    def test_storage_failure_during_drain_is_recorded_as_gap(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        events = []
        def store(event):
            if event["event_kind"] == "execution_stream":
                entered.set()
                release.wait(2)
                raise OSError("private storage failure")
            events.append(deepcopy(event))
        with patch("socket.socket.connect", side_effect=AssertionError("network forbidden")):
            recorder, engine = self.connected_recorder(store)
            engine._fanout(row())
            self.assertTrue(entered.wait(1))
            stopper = threading.Thread(target=lambda: recorder.stop(timeout=1))
            stopper.start()
            wait_until(lambda: recorder.status()["stopping"])
            release.set()
            stopper.join(2)
            self.assertFalse(stopper.is_alive())
            self.assertIsNotNone(recorder.status()["error"])
            self.assertFalse(recorder.status()["coverage_complete"])
            self.assertIn("stream_gap", [e["event_kind"] for e in events])
            self.assertNotIn("private", json.dumps(recorder.status()))

    def test_shared_engine_only_marks_subscription_ready_after_own_reg_ack(self):
        sockets = []
        async def connector(url):
            socket = ManualAckSocket()
            sockets.append(socket)
            return socket
        session = SimpleNamespace(mode="paper", token="offline-token", token_usable=True)
        with patch("socket.socket.connect", side_effect=AssertionError("network forbidden")):
            engine = SharedWebSocketEngine(lambda: session, connector=connector, acquire_slot=lambda: None,
                                           ack_timeout=2, poll_interval=0.01, reconnect_delays=())
            self.addCleanup(engine.close)
            handle = engine.subscribe(Subscription(("",), ("00",)), lambda _: None)
            wait_until(lambda: sockets and any(p["trnm"] == "LOGIN" for p in sockets[0].sent))
            socket = sockets[0]
            self.assertFalse(engine.subscription_ready(handle))
            socket.push({"trnm": "LOGIN", "return_code": 0})
            wait_until(lambda: any(p["trnm"] == "REG" for p in socket.sent))
            self.assertTrue(engine.snapshot()["authenticated"])
            self.assertFalse(engine.subscription_ready(handle))
            socket.push({"trnm": "REG", "return_code": 0})
            wait_until(lambda: engine.subscription_ready(handle))
            old_generation = engine.snapshot()["connection_generation"]
            socket.push(OSError("simulated disconnect"))
            wait_until(lambda: socket.closed)
            self.assertFalse(engine.subscription_ready(handle))
            self.assertGreater(engine.snapshot()["connection_generation"], old_generation)


if __name__ == "__main__":
    unittest.main()
