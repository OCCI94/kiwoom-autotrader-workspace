"""External stop requests block command work and queued orders, fully offline."""

import contextlib
from datetime import datetime
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from kiwoom_autotrader.api import orders as api
from kiwoom_autotrader.api.rankings import RankedStock
from kiwoom_autotrader.api.session import BrokerSession, IssuedToken, KST
from kiwoom_autotrader.config import settings
from kiwoom_autotrader.telegram import listener
from kiwoom_autotrader.telegram.bot_credentials import TelegramConfig
from kiwoom_autotrader.telegram.commands import orders, rank_batch
from kiwoom_autotrader.utils.http_queue import HTTPNotSentError
from kiwoom_autotrader.utils.keychain import Credentials
from tests.paper_reconciliation_fixture import fake_reconciliation


CONFIG = TelegramConfig("123456789:stop_listener_fixture_abcdefghijklmnopqrstuvwxyz", 778899001,
                        123456789, "stop_listener_bot")
TOKEN = "offline-stop-listener-token"


def update(identifier, text):
    return {"update_id": identifier, "message": {
        "date": 1000, "text": text, "chat": {"id": CONFIG.chat_id, "type": "private"},
        "from": {"id": CONFIG.chat_id, "is_bot": False},
    }}


class ListenerStopOrderTests(unittest.TestCase):
    def setUp(self):
        recorder_patch = patch("kiwoom_autotrader.telegram.listener.ExecutionStreamRecorder")
        self.addCleanup(recorder_patch.stop)
        self.execution_recorder = recorder_patch.start().return_value
        self.execution_recorder.status.return_value = {"ready": True, "error": None, "coverage_complete": True}
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.receiver = self.root / "receiver"
        self.receiver.mkdir()
        self.stop_path = self.receiver / "stop.request"
        self.patch(settings, "settings_path", return_value=self.root / "settings.json")
        self.patch(listener, "load_telegram_config", return_value=CONFIG)
        self.patch(listener.time, "time", return_value=1000)
        self.patch(listener, "terminal_report")
        self.session = BrokerSession(now=lambda: datetime(2026, 10, 26, 10, tzinfo=KST),
            credential_loader=Mock(return_value=Credentials("offline-key", "offline-secret")),
            token_issuer=Mock(return_value=IssuedToken(TOKEN, "20300101000000")))
        self.session.mode = "paper"
        self.session._issued = IssuedToken(TOKEN, "20300101000000")
        self.telegram = self.patch(listener, "TelegramClient").return_value
        self.telegram.get_me.return_value = {"id": CONFIG.bot_id, "username": CONFIG.bot_username}
        self.telegram.get_webhook_info.return_value = {"url": ""}
        self.telegram.get_updates.return_value = []
        self.scheduler = Mock()
        self.scheduler.claim_due.return_value = None
        self.scheduler.seconds_until_next.return_value = None
        self.dd = Mock()
        self.gd = Mock()
        self.tracker = Mock()
        for component in (self.dd, self.gd, self.tracker):
            component.seconds_until_next.return_value = None
        self.tracker.prepare_order.return_value = "offline-intent"
        self.websocket = Mock()
        self.websocket.snapshot.return_value = {"error": None}
        self.quote = self.patch(orders, "fetch_stock_info", return_value={"stk_cd": "005930", "cur_prc": "70000", "upl_pric": "91000"})
        self.opener = self.patch(api.urllib.request, "build_opener")
        self.queued = self.patch(api, "queued_open", side_effect=self.transport)
        self.before_transport = None
        self.sent = []
        self.network = self.patch_path("socket.socket.connect", side_effect=AssertionError("Live network forbidden"))
        self.keychain = self.patch_path("kiwoom_autotrader.utils.keychain._get_backend",
                                        side_effect=AssertionError("Live Keychain forbidden"))

    def patch(self, owner, name, **options):
        patcher = patch.object(owner, name, **options)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def patch_path(self, target, **options):
        patcher = patch(target, **options)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def tearDown(self):
        self.network.assert_not_called()
        self.keychain.assert_not_called()
        self.opener.return_value.open.assert_not_called()

    def transport(self, opener, request, **options):
        if self.before_transport is not None:
            self.before_transport()
        try:
            options["before_send"]()
        except Exception:
            raise HTTPNotSentError("offline stop before dispatch") from None
        self.sent.append(request)
        return io.BytesIO(json.dumps({"return_code": 0, "ord_no": "00024", "dmst_stex_tp": "KRX"}).encode())

    def dispatch(self, text):
        return listener._dispatch(listener._text_command(text, CONFIG), text, CONFIG,
            self.session, self.scheduler, monitor=self.dd, golden_monitor=self.gd,
            should_stop=self.stop_path.exists)

    def receive(self, *, default_monitors=False):
        options = {} if default_monitors else {"monitor": self.dd, "golden_monitor": self.gd}
        with contextlib.redirect_stdout(io.StringIO()):
            result = listener.run_listener(self.receiver, max_polls=1, broker_session=self.session,
                scheduler=self.scheduler, execution_tracker=self.tracker,
                websocket_engine=self.websocket, **options)
        self.assertEqual(result, 0)
        self.assertIsNone(self.session.mode)
        self.assertIsNone(self.session.token)
        return json.loads((self.receiver / "state.json").read_text())

    def test_stop_at_dispatch_entry_blocks_all_handlers_and_monitor_resets(self):
        names = ("handle_buy", "handle_sell", "handle_login", "handle_search", "handle_report",
                 "handle_market_hours", "handle_settings", "handle_reservation",
                 "handle_cross_command", "handle_ip", "handle_rank_batch", "_reply")
        handlers = [self.patch(listener, name) for name in names]
        self.stop_path.touch()
        for text in ("buy 005930 1", "sell 005930 1", "login paper", "srch 005930", "report",
                     "mkhr 9:00 15:30", "stts default", "rsv list", "start gdcrs", "stop ddcrs",
                     "IP", "rank 1 cmd 2 buy () 1", "help"):
            with self.subTest(text=text):
                self.assertIn("종료 요청", "\n".join(self.dispatch(text)))
        for handler in handlers:
            handler.assert_not_called()
        self.dd.reset.assert_not_called()
        self.gd.reset.assert_not_called()
        self.queued.assert_not_called()

    def test_buy_budget_buy_and_sell_stop_in_queue_never_reach_transport(self):
        for text in ("buy 005930 1", "buy 005930 max 150000", "sell 005930 1"):
            with self.subTest(text=text):
                self.stop_path.unlink(missing_ok=True)
                self.queued.reset_mock()
                self.before_transport = self.stop_path.touch
                replies = self.dispatch(text)
                self.assertFalse(replies.uncertain)
                self.assertIn("전송하지 않았습니다", "\n".join(replies))
                self.queued.assert_called_once()
        self.assertEqual(self.sent, [])

    def test_unstopped_orders_preserve_the_normal_order_path(self):
        for text in ("buy 005930 1 70000", "buy 005930 max 150000", "sell 005930 1"):
            with self.subTest(text=text):
                self.assertIn("주문 접수", "\n".join(self.dispatch(text)))
        self.assertEqual(len(self.sent), 3)

    def test_stop_during_budget_quote_prevents_even_order_queueing(self):
        def quote(*_):
            self.stop_path.touch()
            return {"stk_cd": "005930", "cur_prc": "70000"}
        self.quote.side_effect = quote
        replies = self.dispatch("buy 005930 max 150000")
        self.assertFalse(replies.uncertain)
        self.assertIn("전송하지 않았습니다", "\n".join(replies))
        self.queued.assert_not_called()

    def test_stop_during_name_lookup_is_rechecked_at_order_dispatch(self):
        def lookup(*_):
            self.stop_path.touch()
            return "005930"
        resolve = self.patch(orders, "resolve_stock_name", side_effect=lookup)
        replies = self.dispatch("buy 삼성전자 1")
        resolve.assert_called_once()
        self.assertFalse(replies.uncertain)
        self.assertEqual(self.sent, [])

    def test_real_rank_recursion_keeps_send_guard_and_stops_remaining_symbols(self):
        self.patch(rank_batch, "fetch_ranked_stocks", return_value=[
            RankedStock("005930", "offline first", "1"), RankedStock("000660", "offline second", "2")])
        self.before_transport = self.stop_path.touch
        replies = self.dispatch("rank 1 cmd 2 buy () 1 70000")
        self.assertEqual(self.sent, [])
        self.queued.assert_called_once()
        self.assertIn("일괄 실행을 중단", replies[-1])

    def test_recursive_entry_rejects_stop_even_if_batch_dispatches_anyway(self):
        def batch(text, session, dispatch, *, should_stop):
            self.stop_path.touch()
            self.assertTrue(should_stop())
            return dispatch("buy 005930 1")
        self.patch(listener, "handle_rank_batch", side_effect=batch)
        buy = self.patch(listener, "handle_buy")
        self.assertIn("종료 요청", "\n".join(self.dispatch("rank 1 cmd 1 buy () 1")))
        buy.assert_not_called()

    def test_stop_during_reservation_notice_prevents_dispatch_and_monitor_ticks(self):
        self.scheduler.claim_due.side_effect = [{"id": 7, "time": "10:00", "command": "buy 005930 1"}, None]
        def notify(_, text):
            if "예약 #7 실행" in text:
                self.stop_path.touch()
        self.telegram.send_message.side_effect = notify
        dispatch = self.patch(listener, "_dispatch")
        state = self.receive()
        dispatch.assert_not_called()
        self.scheduler.claim_due.assert_called_once()
        self.telegram.get_updates.assert_not_called()
        self.tracker.prepare_order.assert_not_called()
        self.dd.tick.assert_not_called()
        self.gd.tick.assert_not_called()
        self.assertEqual(state["status"], "stopped")

    def test_stop_in_reserved_order_queue_cancels_intent_without_order_or_monitor(self):
        self.scheduler.claim_due.side_effect = [{"id": 7, "time": "10:00", "command": "buy 005930 1 70000"}, None]
        self.tracker.assert_no_pending_order = Mock()
        self.patch(listener, "PaperReconciliationGate", return_value=Mock(side_effect=fake_reconciliation))
        self.before_transport = self.stop_path.touch
        self.receive()
        self.assertEqual(self.sent, [])
        self.tracker.prepare_order.assert_called_once()
        args = self.tracker.prepare_order.call_args
        self.assertEqual(args.args, ("paper", "buy", "005930", "1", "rsv#7"))
        self.assertEqual((args.kwargs["price"], args.kwargs["require_no_pending"]), ("70000", True))
        self.assertEqual(args.kwargs["reconciliation_evidence"]["remaining_principal_krw"], 1000000)
        self.tracker.cancel_intent.assert_called_once_with("offline-intent", "not_sent")
        self.tracker.register_order.assert_not_called()
        self.telegram.get_updates.assert_not_called()
        self.dd.tick.assert_not_called()
        self.gd.tick.assert_not_called()

    def test_stop_in_incoming_order_queue_blocks_later_updates_and_monitor_ticks(self):
        self.telegram.get_updates.return_value = [update(1, "sell 005930 1"), update(2, "buy 005930 1")]
        self.before_transport = self.stop_path.touch
        state = self.receive()
        self.assertEqual(self.sent, [])
        self.queued.assert_called_once()
        self.dd.tick.assert_not_called()
        self.gd.tick.assert_not_called()
        self.assertEqual((state["status"], state["received_count"], state["next_offset"]), ("stopped", 1, 2))

    def test_both_default_monitors_receive_live_stop_file_callbacks(self):
        dd_factory = self.patch(listener, "CrossMonitor", return_value=self.dd)
        gd_factory = self.patch(listener, "GoldenCrossMonitor", return_value=self.gd)
        self.receive(default_monitors=True)
        self.assertEqual(dd_factory.call_args.kwargs["state_path"], self.receiver / "ddcrs-order-markers.sqlite3")
        for factory in (dd_factory, gd_factory):
            callback = factory.call_args.kwargs["stop_requested"]
            self.assertFalse(callback())
            self.stop_path.touch()
            self.assertTrue(callback())
            self.stop_path.unlink()


if __name__ == "__main__":
    unittest.main()
