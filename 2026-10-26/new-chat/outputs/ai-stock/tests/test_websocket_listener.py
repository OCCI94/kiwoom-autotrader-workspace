"""Listener ownership of the shared engine; no live HTTP, WS or Keychain."""

import contextlib
from datetime import datetime, timedelta
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from kiwoom_autotrader.api.session import BrokerSession, IssuedToken, KST
from kiwoom_autotrader.realtime import get_websocket_engine, WebSocketEngineError
from kiwoom_autotrader.telegram import listener
from kiwoom_autotrader.telegram.bot_credentials import TelegramConfig
from kiwoom_autotrader.telegram.client import TelegramAPIError
from kiwoom_autotrader.utils.keychain import Credentials

CONFIG = TelegramConfig("123456789:offline_ws_listener_bot_token_abcdefghijklmnopqrstuvwxyz", 998877, 123456789, "offline_ws_bot")


def update(identifier, text):
    return {"update_id": identifier, "message": {
        "date": 1000, "text": text, "chat": {"id": CONFIG.chat_id, "type": "private"},
        "from": {"id": CONFIG.chat_id, "is_bot": False},
    }}


class FakeEngine:
    def __init__(self, provider):
        self.provider = provider
        self.synchronized, self.errors = [], []
        self.closed = 0
        self.subscribe = Mock()

    def sync_session(self):
        session = self.provider()
        self.synchronized.append((session.mode, session.token, session.token_usable))

    def snapshot(self):
        index = min(len(self.synchronized) - 1, len(self.errors) - 1)
        return {"status": "idle", "subscription_count": 0, "pair_count": 0, "connected": False,
                "authenticated": False, "error": self.errors[index] if index >= 0 else None}

    def close(self):
        self.closed += 1


class WebSocketListenerTests(unittest.TestCase):
    def setUp(self):
        recorder_patch = patch("kiwoom_autotrader.telegram.listener.ExecutionStreamRecorder")
        self.addCleanup(recorder_patch.stop)
        self.execution_recorder = recorder_patch.start().return_value
        self.execution_recorder.status.return_value = {"ready": True, "error": None, "coverage_complete": True}
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        settings_patch = patch("kiwoom_autotrader.config.settings.settings_path",
                               return_value=self.directory / "settings.json")
        self.addCleanup(settings_patch.stop)
        settings_patch.start()
        self.now = datetime(2026, 10, 26, 8, 54, tzinfo=KST)
        self.issued = []
        def issue(mode, _credentials):
            token = f"offline-private-ws-{mode}-{len(self.issued) + 1}"
            self.issued.append(token)
            return IssuedToken(token)
        self.session = BrokerSession(now=lambda: self.now,
                                     credential_loader=lambda _: Credentials("fixture-key", "fixture-secret"), token_issuer=issue)
        self.engine = FakeEngine(lambda: self.session)
        self.factory = self.patch(listener, "SharedWebSocketEngine", side_effect=self.make_engine)
        self.patch(listener, "load_telegram_config", return_value=CONFIG)
        self.telegram = self.patch(listener, "TelegramClient").return_value
        self.telegram.get_me.return_value = {"id": CONFIG.bot_id, "username": CONFIG.bot_username}
        self.telegram.get_webhook_info.return_value = {"url": ""}
        self.telegram.get_updates.return_value = []
        self.patch(listener.time, "time", return_value=1000)
        self.patch(listener.time, "sleep")
        self.scheduler, self.monitor, self.tracker = Mock(), Mock(), Mock()
        self.scheduler.claim_due.return_value = None
        for item in (self.scheduler, self.monitor, self.tracker):
            item.seconds_until_next.return_value = None
        self.output = io.StringIO()
        self.addCleanup(self.output.close)
        self.guards = []
        for target in ("urllib.request.OpenerDirector.open", "socket.socket.connect",
                       "kiwoom_autotrader.utils.keychain._get_backend"):
            patcher = patch(target, side_effect=AssertionError("external effect forbidden"))
            self.addCleanup(patcher.stop)
            guard = patcher.start()
            self.addCleanup(guard.assert_not_called)
            self.guards.append(guard)

    def patch(self, obj, name, **kwargs):
        patcher = patch.object(obj, name, **kwargs)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def make_engine(self, provider):
        self.engine.provider = provider
        return self.engine

    def receive(self, polls=1, **options):
        with contextlib.redirect_stdout(self.output):
            result = listener.run_listener(self.directory, max_polls=polls, broker_session=self.session,
                                           scheduler=self.scheduler, monitor=self.monitor,
                                           execution_tracker=self.tracker, **options)
        for guard in self.guards:
            guard.assert_not_called()
        return result

    def replies(self):
        return [call.args[1] for call in self.telegram.send_message.call_args_list]

    def assert_cleanup(self):
        self.assertEqual(self.engine.closed, 1)
        self.assertIsNone(self.session.token)
        descriptor = listener._open_lock(self.directory / "listener.lock")
        listener.os.close(descriptor)

    def assert_no_secrets(self):
        content = self.output.getvalue() + "\n".join(self.replies())
        state = self.directory / "state.json"
        if state.exists():
            content += state.read_text()
        for secret in [CONFIG.bot_token, "fixture-key", "fixture-secret", *self.issued]:
            self.assertNotIn(secret, content)

    def test_one_engine_is_reused_by_getter_and_idle_without_subscribers(self):
        seen = []
        def search(_text, session):
            seen.append(get_websocket_engine(session))
            return ["조회 완료"]
        self.patch(listener, "handle_search", side_effect=search)
        self.telegram.get_updates.side_effect = [[update(1, "srch 005930")], [update(2, "srch 000660")], []]
        self.assertEqual(self.receive(polls=3), 0)
        self.factory.assert_called_once()
        self.assertIs(self.engine.provider(), self.session)
        self.assertEqual(seen, [self.engine, self.engine])
        self.assertIs(get_websocket_engine(self.session), self.engine)
        self.assertEqual(len(self.engine.synchronized), 3)
        self.engine.subscribe.assert_not_called()
        self.assertFalse(self.engine.snapshot()["connected"])
        self.assert_cleanup()
        self.assert_no_secrets()

    def test_injected_engine_prevents_second_construction(self):
        self.assertEqual(self.receive(websocket_engine=self.engine), 0)
        self.factory.assert_not_called()
        self.assertIs(self.session.websocket_engine, self.engine)
        self.assert_cleanup()

    def test_capture_is_started_stopped_and_readiness_is_not_restored(self):
        self.assertEqual(self.receive(), 0)
        self.execution_recorder.start.assert_called_once()
        self.execution_recorder.stop.assert_called_once()
        saved = json.loads((self.directory / "state.json").read_text())
        self.assertTrue(saved["execution_recording"]["ready"])
        restored = listener._load_state(self.directory / "state.json", CONFIG, 1001)
        self.assertNotIn("execution_recording", restored)
        self.assertIsNone(self.session.execution_recording_guard)

    def test_capture_error_is_visible_and_order_guard_is_installed(self):
        self.execution_recorder.status.return_value = {"ready": False, "error": "체결 저장 실패", "coverage_complete": False}
        checked = []
        def poll(**_):
            with self.assertRaisesRegex(ValueError, "recording unavailable"):
                self.session.execution_recording_guard()
            checked.append(True)
            return []
        self.telegram.get_updates.side_effect = poll
        self.assertEqual(self.receive(), 0)
        self.assertEqual(checked, [True])
        self.assertIn("체결 저장 실패", self.replies())

    def test_provider_tracks_login_switch_and_each_loop_syncs_same_engine(self):
        seen = []
        def search(_text, session):
            engine = get_websocket_engine(session)
            seen.append((engine, engine.provider().mode, engine.provider().token))
            return ["실전 조회 완료"]
        self.patch(listener, "handle_search", side_effect=search)
        self.telegram.get_updates.side_effect = [[update(1, "login real"), update(2, "srch 005930")], []]
        self.assertEqual(self.receive(polls=2), 0)
        self.factory.assert_called_once()
        self.assertEqual(seen, [(self.engine, "real", self.issued[1])])
        self.assertEqual([item[0] for item in self.engine.synchronized], ["paper", "real"])
        self.assert_cleanup()
        self.assert_no_secrets()

    def test_renewed_token_is_seen_without_replacing_engine(self):
        def poll(**_):
            self.now += timedelta(minutes=1)
            return []
        self.telegram.get_updates.side_effect = poll
        self.assertEqual(self.receive(polls=3), 0)
        self.assertEqual(len(self.issued), 2)
        self.assertEqual(self.engine.synchronized[-1][1], self.issued[-1])
        self.factory.assert_called_once()
        self.assert_cleanup()

    def test_unchanged_error_is_not_repeated_and_recurrence_is_reported(self):
        self.engine.errors = ["안전한 WebSocket 상태 안내", "안전한 WebSocket 상태 안내", None, "안전한 WebSocket 상태 안내"]
        self.assertEqual(self.receive(polls=4), 0)
        self.assertEqual(self.replies().count("안전한 WebSocket 상태 안내"), 2)
        self.assert_cleanup()
        self.assert_no_secrets()

    def test_config_failure_closes_engine_and_releases_lock(self):
        self.patch(listener, "load_telegram_config", return_value=None)
        self.session.login("paper")
        with self.assertRaises(listener.TelegramListenerError):
            self.receive()
        self.telegram.get_me.assert_not_called()
        self.assert_cleanup()
        self.assert_no_secrets()

    def test_receive_failure_closes_engine_and_clears_session(self):
        self.telegram.get_updates.side_effect = TelegramAPIError("Telegram HTTP 오류 (code=401).")
        with self.assertRaises(listener.TelegramListenerError):
            self.receive()
        self.assert_cleanup()
        self.assertEqual(json.loads((self.directory / "state.json").read_text())["status"], "failed")

    def test_keyboard_interrupt_closes_engine(self):
        self.telegram.get_updates.side_effect = KeyboardInterrupt()
        self.assertEqual(self.receive(), 0)
        self.assert_cleanup()

    def test_power_off_closes_engine_and_skips_remaining_commands(self):
        self.patch(listener, "stop_channels", return_value=True)
        search = self.patch(listener, "handle_search", return_value=["미실행"])
        self.telegram.get_updates.return_value = [update(1, "power off"), update(2, "srch 005930")]
        self.assertEqual(self.receive(polls=5), 0)
        search.assert_not_called()
        self.assert_cleanup()
        self.assertTrue((self.directory / "stop.request").exists())

    def test_power_off_drains_recording_before_clearing_session(self):
        self.patch(listener, "stop_channels", return_value=True)
        events = []
        original_clear = self.session.clear
        def record_stop():
            events.append(("recording_stop", self.session.authenticated))
        def clear():
            events.append(("session_clear", self.session.authenticated))
            original_clear()
        self.execution_recorder.stop.side_effect = record_stop
        self.session.clear = clear
        self.telegram.get_updates.return_value = [update(1, "power off")]
        self.assertEqual(self.receive(), 0)
        self.assertEqual(events[0], ("recording_stop", True))
        self.assertEqual(events[1], ("session_clear", True))
        self.assertFalse(self.session.authenticated)

    def test_engine_cleanup_failure_does_not_skip_session_or_lock_cleanup(self):
        def failing_close():
            self.engine.closed += 1
            raise RuntimeError("private-cleanup-detail")
        self.engine.close = failing_close
        self.assertEqual(self.receive(), 0)
        self.assert_cleanup()
        self.monitor.reset.assert_called_once()
        self.assertNotIn("private-cleanup-detail", self.output.getvalue())

    def test_monitor_cleanup_failure_does_not_skip_session_or_lock_cleanup(self):
        self.monitor.reset.side_effect = RuntimeError("private-monitor-detail")
        self.assertEqual(self.receive(), 0)
        self.assert_cleanup()
        self.assertNotIn("private-monitor-detail", self.output.getvalue())

    def test_getter_requires_listener_owned_engine(self):
        with self.assertRaises(WebSocketEngineError):
            get_websocket_engine(BrokerSession())


if __name__ == "__main__":
    unittest.main()
