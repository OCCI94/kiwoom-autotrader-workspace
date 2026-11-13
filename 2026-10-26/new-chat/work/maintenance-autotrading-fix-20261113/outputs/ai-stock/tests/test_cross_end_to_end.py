"""Offline paper-trading lifecycle through real strategies, orders and ledger.

Run: python -m unittest discover -s tests -p 'test_cross_end_to_end.py' -v
Only HTTP/Telegram/credential/clock boundaries are replaced. The listener,
session token parser, command handlers, monitors, broker API parsers, HTTP queue,
rate limiter, order context, SQLite ledger and CSV writer are production code.
Every generated state file lives in a temporary directory and is removed.
Set AI_STOCK_TEST_ARTIFACT_DIR to export the verified synthetic CSV and a
sanitized JSON report after every scenario assertion has passed.
"""
from contextlib import ExitStack, redirect_stdout
import csv
from datetime import datetime, timedelta
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from kiwoom_autotrader.api import minute_chart
from kiwoom_autotrader.api import orders
from kiwoom_autotrader.api.session import BrokerSession, KST
from kiwoom_autotrader.config import settings
from kiwoom_autotrader.telegram import listener
from kiwoom_autotrader.telegram.bot_credentials import TelegramConfig
from kiwoom_autotrader.trading.cross_monitor import CrossMonitor
from kiwoom_autotrader.trading.execution_log import ExecutionTracker
from kiwoom_autotrader.trading.golden_cross import GoldenCrossMonitor
from kiwoom_autotrader.trading.order_context import automated_orders
from kiwoom_autotrader.trading.paper_reconciliation import PaperReconciliationGate
from kiwoom_autotrader.realtime.execution_stream import ExecutionStreamRecorder
from kiwoom_autotrader.realtime.engine import SubscriptionHandle
from kiwoom_autotrader.utils import http_queue
from kiwoom_autotrader.utils.command_scheduler import CommandScheduler
from kiwoom_autotrader.utils.keychain import Credentials


TOKEN = "offline-cross-e2e-broker-private-token"
CONFIG = TelegramConfig("123456789:cross_e2e_fixture_abcdefghijklmnopqrstuvwxyz", 778899001,
                        123456789, "cross_e2e_bot")


class ScenarioClock:
    def __init__(self):
        self._wall = datetime(2026, 10, 26, 10, 3, tzinfo=KST)
        self._elapsed = 1000.0
        self._lock = threading.RLock()

    def now(self):
        with self._lock:
            return self._wall

    def monotonic(self):
        with self._lock:
            return self._elapsed

    def timestamp(self):
        return self.now().timestamp()

    def sleep(self, seconds):
        if seconds < 0:
            raise AssertionError("Clock cannot move backwards")
        with self._lock:
            self._elapsed += seconds
            self._wall += timedelta(seconds=seconds)

    def set(self, text):
        hour, minute, second = map(int, text.split(":"))
        moment = self.now().replace(hour=hour, minute=minute, second=second, microsecond=0)
        self.sleep((moment - self.now()).total_seconds())


class ClockWait:
    def __init__(self, clock):
        self.clock = clock

    def wait(self, seconds):
        self.clock.sleep(seconds)


class FixtureEngine:
    """One shared, in-memory engine emits synthetic provider events only."""
    def __init__(self):
        self.callbacks = {}
        self.delivery_sequences = {}
        self.sequence = 0
    def subscribe(self, subscription, callback):
        self.sequence += 1
        handle = SubscriptionHandle(str(self.sequence))
        self.callbacks[handle.id] = callback
        self.delivery_sequences[handle.id] = 0
        return handle
    def unsubscribe(self, handle):
        return self.callbacks.pop(handle.id, None) is not None
    def drain_and_unsubscribe(self, handle, *, timeout=2):
        return self.unsubscribe(handle)
    def subscription_ready(self, handle):
        return handle.id in self.callbacks
    def subscription_delivery_status(self, handle):
        return {"ingress_sequence": self.delivery_sequences[handle.id], "pending_callbacks": 0}
    def snapshot(self):
        return {"connection_generation":1, "callback_errors":0, "callback_dropped":0, "error":None}
    def sync_session(self):
        pass
    def close(self):
        self.callbacks.clear()
    def emit(self, row):
        for handle_id, callback in tuple(self.callbacks.items()):
            self.delivery_sequences[handle_id] += 1
            callback(row)


class PaperBrokerHTTP:
    """An in-memory broker at the opener boundary, never an actual socket."""
    addheaders = []

    def __init__(self, clock, root):
        self.clock, self.root = clock, root
        self.orders, self.executions, self.requests, self.errors = [], [], [], []
        self.held = 0
        self.auth_count = 0
        self.prices = [300, 200, 100, 400, 500, 100]
        self.engine = None

    def fill(self, order_number, execution_number, quantity, price, time):
        order = next(row for row in self.orders if row["order_number"] == order_number)
        filled = sum(int(row["cntr_qty"]) for row in self.executions if int(row["ord_no"]) == order_number)
        if quantity <= 0 or filled + quantity > int(order["body"]["ord_qty"]):
            raise AssertionError("Fixture overfill")
        side = "현금매수" if order["api_id"] == "kt10000" else "현금매도"
        self.executions.append({"stk_bond_tp": "1", "ord_no": str(order_number).zfill(7),
            "cntr_no": str(execution_number).zfill(7), "stk_cd": "A005930", "stk_nm": "삼성전자",
            "io_tp_nm": side, "cntr_qty": str(quantity).zfill(10), "cntr_uv": str(price).zfill(10),
            "cntr_tm": time, "dmst_stex_tp": "KRX"})
        self.held += quantity if side == "현금매수" else -quantity
        if self.held < 0:
            raise AssertionError("Fixture sold more than held")
        same = [row for row in self.executions if int(row["ord_no"]) == order_number]
        cumulative = sum(int(row["cntr_qty"]) for row in same)
        amount = sum(int(row["cntr_qty"]) * int(row["cntr_uv"]) for row in same)
        self.engine.emit({"type":"00", "values":{"9203":str(order_number).zfill(7),
            "9001":"A005930", "907":"2" if side == "현금매수" else "1", "913":"체결",
            "908":time.replace(":", ""), "909":str(execution_number), "914":str(price),
            "915":str(quantity), "911":str(cumulative), "903":str(amount), "938":"0", "939":"0"}})

    def open(self, request, timeout):
        try:
            return self._open(request, timeout)
        except Exception as error:
            self.errors.append(type(error).__name__ + ": " + str(error))
            raise

    def _open(self, request, timeout):
        endpoint = urlsplit(request.full_url)
        if (endpoint.scheme, endpoint.netloc) != ("https", "mockapi.kiwoom.com"):
            raise AssertionError("Only fixture paper origin is allowed")
        if request.get_method() != "POST" or timeout != 15:
            raise AssertionError("Unexpected transport contract")
        body = json.loads(request.data)
        api_id = request.get_header("Api-id")
        if endpoint.path == "/oauth2/token":
            self.auth_count += 1
            if body != {"grant_type": "client_credentials", "appkey": "offline-e2e-key", "secretkey": "offline-e2e-secret"}:
                raise AssertionError("Token request did not use injected credentials")
            self.requests.append(("au10001", self.clock.now().isoformat()))
            payload = {"return_code": 0, "token_type": "bearer", "token": TOKEN, "expires_dt": "20300101090000"}
        else:
            if request.get_header("Authorization") != "Bearer " + TOKEN:
                raise AssertionError("Request did not use current in-memory session")
            self.requests.append((api_id, self.clock.now().isoformat()))
            if api_id == "ka10080":
                if endpoint.path != "/api/dostk/chart" or body != {"stk_cd": "005930", "tic_scope": "1", "upd_stkpc_tp": "1"}:
                    raise AssertionError("Unexpected minute-chart request")
                current = self.clock.now().replace(second=0, microsecond=0)
                start = current.replace(hour=10, minute=0)
                rows = [{"cntr_tm": (start + timedelta(minutes=index)).strftime("%Y%m%d%H%M%S"),
                         "cur_prc": str(price)} for index, price in enumerate(self.prices)
                        if start + timedelta(minutes=index) < current]
                # A large unclosed price must never alter the completed SMA.
                rows.append({"cntr_tm": current.strftime("%Y%m%d%H%M%S"), "cur_prc": "+99999"})
                payload = {"return_code": 0, "stk_cd": "005930", "stk_min_pole_chart_qry": list(reversed(rows))}
            elif api_id == "ka10001":
                if body != {"stk_cd": "005930"}:
                    raise AssertionError("Unexpected quote request")
                payload = {"return_code": 0, "stk_cd": "005930", "stk_nm": "삼성전자", "cur_prc": "+400"}
            elif api_id in ("kt10000", "kt10001"):
                if endpoint.path != "/api/dostk/ordr" or body["stk_cd"] != "005930" or body["dmst_stex_tp"] != "KRX":
                    raise AssertionError("Unexpected stock order")
                strategy = "gdcrs" if api_id == "kt10000" else "ddcrs"
                side = "buy" if api_id == "kt10000" else "sell"
                # Real order-context prepare must be committed before HTTP.
                with sqlite3.connect(self.root / "logs/.execution-ledger.sqlite3") as database:
                    count = database.execute("SELECT COUNT(*) FROM intents WHERE strategy=? AND side=? AND state='prepared'", (strategy, side)).fetchone()[0]
                if count != 1:
                    raise AssertionError("Automatic intent was not durable before send")
                with sqlite3.connect(self.root / f"receiver/{strategy}-order-markers.sqlite3") as database:
                    count = database.execute("SELECT COUNT(*) FROM attempts WHERE mode='paper' AND symbol='005930'").fetchone()[0]
                if count != 1:
                    raise AssertionError("Signal attempt marker was not durable before send")
                if api_id == "kt10001" and int(body["ord_qty"]) != self.held:
                    raise AssertionError("Death cross did not sell all current sellable holdings")
                number = 24 + len(self.orders)
                self.orders.append({"api_id": api_id, "order_number": number, "body": body,
                                    "time": self.clock.now().isoformat()})
                payload = {"return_code": 0, "ord_no": str(number).zfill(7), "dmst_stex_tp": "KRX"}
            elif api_id == "kt00001":
                if body != {"qry_tp": "3"}:
                    raise AssertionError("Unexpected cash request")
                payload = {"return_code": 0, "entr": "1000000", "ord_alow_amt": "1000000"}
            elif api_id == "kt00018":
                if body != {"qry_tp": "1", "dmst_stex_tp": "KRX"}:
                    raise AssertionError("Unexpected holdings request")
                rows = [] if self.held == 0 else [{"stk_cd": "A005930", "stk_nm": "삼성전자",
                    "rmnd_qty": str(self.held), "trde_able_qty": str(self.held), "crd_tp": "00", "crd_loan_dt": "",
                    "pur_pric": "400", "cur_prc": "400", "pur_amt": str(400 * self.held), "evlt_amt": str(400 * self.held)}]
                payload = {"return_code": 0, "acnt_evlt_remn_indv_tot": rows}
            elif api_id == "kt00009":
                if body != {"ord_dt": "20261026", "stk_bond_tp": "1", "mrkt_tp": "0", "sell_tp": "0",
                            "qry_tp": "1", "stk_cd": "", "fr_ord_no": "", "dmst_stex_tp": "KRX"}:
                    raise AssertionError("Unexpected execution request")
                payload = {"return_code": 0, "acnt_ord_cntr_prst_array": list(reversed(self.executions))}
            elif api_id == "ka10075":
                if endpoint.path != "/api/dostk/acnt" or body != {"all_stk_tp":"0", "trde_tp":"0", "stex_tp":"1"}:
                    raise AssertionError("Unexpected open-order reconciliation request")
                payload = {"return_code": 0, "oso": []}
            elif api_id == "ka10072":
                if body != {"stk_cd": "005930", "strt_dt": "20261026"}:
                    raise AssertionError("Unexpected dated cost request")
                payload = {"return_code": 0, "dt_stk_div_rlzt_pl": [
                    {"stk_cd": "A005930", "crd_tp": "현금잔고", "cntr_qty": "1", "cntr_pric": "110",
                     "buy_uv": "400", "tdy_trde_cmsn": "1", "tdy_trde_tax": "0", "tdy_sel_pl": "-291"},
                    {"stk_cd": "A005930", "crd_tp": "현금잔고", "cntr_qty": "2", "cntr_pric": "105",
                     "buy_uv": "400", "tdy_trde_cmsn": "2", "tdy_trde_tax": "1", "tdy_sel_pl": "-593"}]}
            elif api_id == "ka10074":
                if body != {"strt_dt": "20261026", "end_dt": "20261026"}:
                    raise AssertionError("Unexpected daily account cost request")
                payload = {"return_code": 0, "tot_buy_amt": "1200", "tot_sell_amt": "320",
                    "rlzt_pl": "-884", "trde_cmsn": "3", "trde_tax": "1", "dt_rlzt_pl": []}
            elif api_id == "ka10073":
                if body != {"stk_cd":"005930", "strt_dt":"20261026", "end_dt":"20261026"}:
                    raise AssertionError("Unexpected period cost request")
                payload = {"return_code":0, "dt_stk_rlzt_pl":[
                    {"dt":"20261026", "stk_cd":"A005930", "crd_tp":"현금잔고", "cntr_qty":"1", "cntr_pric":"110", "buy_uv":"400", "tdy_trde_cmsn":"1", "tdy_trde_tax":"0", "tdy_sel_pl":"-291"},
                    {"dt":"20261026", "stk_cd":"A005930", "crd_tp":"현금잔고", "cntr_qty":"2", "cntr_pric":"105", "buy_uv":"400", "tdy_trde_cmsn":"2", "tdy_trde_tax":"1", "tdy_sel_pl":"-593"}]}
            else:
                raise AssertionError("Unexpected TR " + str(api_id))
        return http_queue.BufferedResponse(json.dumps(payload, ensure_ascii=False).encode(),
            {"api-id": api_id, "cont-yn": "N", "next-key": ""}, 200)


class ScenarioTelegram:
    def __init__(self, clock, broker, snapshot):
        self.clock, self.broker, self.snapshot = clock, broker, snapshot
        self.messages, self.polls = [], []
        self.update_id = 0
        self.assert_repeated_buy_blocked = None
        self.phases = [
            ("10:03:05", ["login paper", "gdcrs intv 1 3", "gdcrs add 005930 1200", "start gdcrs", "start ddcrs"], None, None),
            ("10:03:10", [], None, None),
            ("10:04:05", [], None, None),
            ("10:04:16", [], (24, 1, 2, 400, "10:04:10"), "buy_accepted"),
            ("10:04:18", [], None, None),
            ("10:04:30", [], (24, 2, 1, 400, "10:04:25"), "buy_partial"),
            ("10:04:32", [], None, None),
            ("10:05:05", [], None, "buy_full"),
            ("10:05:08", [], None, None),
            ("10:06:05", [], None, None),
            ("10:06:08", [], None, None),
            ("10:06:20", [], (25, 1, 1, 110, "10:06:15"), "sell_accepted"),
            ("10:06:22", [], None, None),
            ("10:06:35", [], (25, 2, 2, 105, "10:06:30"), "sell_partial"),
            ("10:06:37", [], None, None),
            ("10:06:39", [], None, None),  # bounded intraday cost/account stages
            ("10:06:41", [], None, None),
            ("10:06:43", [], None, None),  # independent 73 comparison read
            ("10:06:45", ["stop gdcrs", "stop ddcrs"], None, "sell_full"),
            ("10:06:47", [], None, "stopped"),
            ("15:31:05", [], None, None),
            ("15:31:08", [], None, None),
            ("15:31:10", [], None, None),
            ("15:31:12", [], None, None),
            ("15:31:14", [], None, None),
            ("15:31:16", [], None, None),
            ("15:31:18", [], None, None),  # allow bounded balance refresh and queued summary
        ]

    def get_me(self):
        return {"id": CONFIG.bot_id, "username": CONFIG.bot_username}

    def get_webhook_info(self):
        return {"url": ""}

    def send_message(self, chat_id, text):
        if chat_id != CONFIG.chat_id:
            raise AssertionError("Unexpected Telegram recipient")
        self.messages.append(text)

    def get_updates(self, *, timeout, offset):
        phase = self.phases[len(self.polls)]
        when, commands, execution, capture = phase
        if capture:
            self.snapshot(capture)
        if capture == "buy_full" and self.assert_repeated_buy_blocked:
            self.assert_repeated_buy_blocked()
        self.clock.set(when)
        if execution:
            self.broker.fill(*execution)
        self.polls.append({"time": when, "timeout": timeout, "offset": offset})
        result = []
        for command in commands:
            self.update_id += 1
            result.append({"update_id": self.update_id, "message": {"date": int(self.clock.timestamp()),
                "text": command, "chat": {"id": CONFIG.chat_id, "type": "private"},
                "from": {"id": CONFIG.chat_id, "is_bot": False}}})
        return result


class CrossEndToEndTests(unittest.TestCase):
    def test_telegram_both_strategies_actual_orders_partial_fills_csv_and_stop(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as patches:
            root = Path(directory)
            clock = ScenarioClock()
            patches.enter_context(patch.object(listener, "PaperReconciliationGate",
                side_effect=lambda session, tracker: PaperReconciliationGate(session, tracker,
                    now=clock.now, monotonic=clock.monotonic)))
            settings_file = root / "config/settings.json"
            patches.enter_context(patch.object(settings, "settings_path", return_value=settings_file))
            # Legacy cross/fill fixture deliberately has no portfolio taxonomy.
            settings.save_settings({"paper_automation_only": True, "paper_automation_buy_cap": 1000000})
            settings.save_paper_strategy_options({"max_positions_per_theme":0})
            patches.enter_context(patch("time.monotonic", clock.monotonic))
            patches.enter_context(patch("time.sleep", clock.sleep))
            patches.enter_context(patch("time.time", clock.timestamp))
            network = patches.enter_context(patch("socket.socket.connect", side_effect=AssertionError("Live sockets forbidden")))
            keychain = patches.enter_context(patch("kiwoom_autotrader.utils.keychain._get_backend", side_effect=AssertionError("Live Keychain forbidden")))
            broker = PaperBrokerHTTP(clock, root)
            patches.enter_context(patch("urllib.request.build_opener", return_value=broker))
            limiter = http_queue.SharedRateLimit(root / "rate.json", clock=clock.monotonic)
            limiter._wait = ClockWait(clock)
            queue = http_queue.HTTPQueue(limiter=limiter)
            patches.callback(queue.close)
            patches.enter_context(patch.object(http_queue, "_default_queue", queue))
            patches.enter_context(patch.object(http_queue, "_default_pid", os.getpid()))
            patches.enter_context(patch.object(minute_chart, "_REQUEST_PACER",
                minute_chart._MinuteChartPacer(clock=clock.monotonic, wait=clock.sleep)))
            patches.enter_context(patch.object(minute_chart, "_query_now", clock.now))
            # Observer request/response dates must use the same scenario clock
            # as the synthetic 00 fills, rather than this test runner's date.
            observer_clock = patches.enter_context(patch("kiwoom_autotrader.api.executions.datetime", wraps=datetime))
            observer_clock.now.side_effect = lambda tz=None: clock.now().astimezone(tz) if tz else clock.now().replace(tzinfo=None)

            credentials_read = []
            def credentials(mode):
                credentials_read.append(mode)
                if mode != "paper":
                    raise AssertionError("Real credentials forbidden")
                return Credentials("offline-e2e-key", "offline-e2e-secret")
            session = BrokerSession(now=clock.now, credential_loader=credentials)
            scheduler = CommandScheduler(settings_file=settings_file, state_file=root / "reservation-state.json", now=clock.now)
            tracker = ExecutionTracker(log_dir=root / "logs", now=clock.now)
            engine = FixtureEngine()
            broker.engine = engine
            session.websocket_engine = engine
            recorder = ExecutionStreamRecorder(session, tracker, now=clock.now, poll_interval=5)
            dd = CrossMonitor(now=clock.now, state_path=root / "receiver/ddcrs-order-markers.sqlite3")
            gd = GoldenCrossMonitor(now=clock.now, state_path=root / "receiver/gdcrs-order-markers.sqlite3",
                stop_requested=(root / "receiver/stop.request").exists)
            snapshots = {}
            def rows():
                target = root / "logs/20261026.csv"
                if not target.exists():
                    return []
                with target.open(encoding="utf-8-sig", newline="") as source:
                    return list(csv.DictReader(source))
            def capture(label):
                snapshots[label] = {"rows": rows(), "settings": settings.load_settings(),
                    "orders": len(broker.orders), "chart_requests": sum(api == "ka10080" for api, _ in broker.requests)}
            telegram = ScenarioTelegram(clock, broker, capture)
            def check_buy_policy():
                count = len(broker.orders)
                with automated_orders(tracker, "gdcrs"):
                    from kiwoom_autotrader.trading.paper_policy import limit_automatic_buy_budget
                    self.assertEqual(limit_automatic_buy_budget(session, "005930", "1000000"), 998800)
                    with self.assertRaises(orders.OrderError):
                        orders.submit_order(session, "buy", "005930", "2498", "400")
                    with self.assertRaisesRegex(orders.OrderError, "한도"):
                        orders.submit_order(session, "buy", "000660", "1", "1000001")
                self.assertEqual(len(broker.orders), count)
                snapshots['buy_full']['position_cap_and_order_cap_blocked'] = True
            telegram.assert_repeated_buy_blocked = check_buy_policy
            patches.enter_context(patch.object(listener, "TelegramClient", return_value=telegram))
            patches.enter_context(patch.object(listener, "load_telegram_config", return_value=CONFIG))
            # This fixture specifies a fixed broker request sequence. The independent
            # periodic recovery/TTL contract is covered by external safety tests.
            legacy=patches.enter_context(patch.object(listener, "_LegacyRecoverySupervisor")).return_value
            legacy.status.return_value={}
            output = io.StringIO()
            with redirect_stdout(output):
                result = listener.run_listener(root / "receiver", max_polls=len(telegram.phases),
                    broker_session=session, scheduler=scheduler, monitor=dd, golden_monitor=gd,
                    execution_tracker=tracker, websocket_engine=engine, execution_recorder=recorder)

            self.assertEqual(result, 0)
            self.assertEqual(broker.errors, [])
            network.assert_not_called()
            keychain.assert_not_called()
            self.assertEqual(credentials_read, ["paper", "paper"])
            self.assertEqual(broker.auth_count, 2)
            self.assertEqual([row["api_id"] for row in broker.orders], ["kt10000", "kt10001"])
            self.assertEqual(broker.orders[0]["body"], {"dmst_stex_tp": "KRX", "stk_cd": "005930",
                "ord_qty": "3", "ord_uv": "400", "trde_tp": "0", "cond_uv": ""})
            self.assertEqual(broker.orders[1]["body"], {"dmst_stex_tp": "KRX", "stk_cd": "005930",
                "ord_qty": "3", "ord_uv": "", "trde_tp": "3", "cond_uv": ""})
            self.assertEqual(snapshots["buy_accepted"]["rows"], [])  # acceptance is not a fill
            self.assertEqual([(row["수량"], row["남은수량"]) for row in snapshots["buy_partial"]["rows"]], [("2", "2")])
            self.assertEqual([(row["수량"], row["남은수량"]) for row in snapshots["buy_full"]["rows"]], [("2", "2"), ("1", "3")])
            self.assertEqual(len(snapshots["sell_accepted"]["rows"]), 2)
            self.assertEqual(snapshots["sell_partial"]["rows"][-1]["남은수량"], "2")
            self.assertEqual(snapshots["sell_full"]["rows"][-1]["남은수량"], "0")
            self.assertTrue(snapshots["buy_accepted"]["settings"]["gdcrs_enabled"])
            self.assertTrue(snapshots["buy_accepted"]["settings"]["ddcrs_enabled"])
            self.assertTrue(snapshots['buy_full']['position_cap_and_order_cap_blocked'])
            self.assertFalse(snapshots["stopped"]["settings"]["gdcrs_enabled"])
            self.assertFalse(snapshots["stopped"]["settings"]["ddcrs_enabled"])
            self.assertEqual(snapshots["stopped"]["chart_requests"], sum(api == "ka10080" for api, _ in broker.requests))
            self.assertEqual(broker.held, 0)
            exported = rows()
            trades = [row for row in exported if row["행구분"] == "체결"]
            self.assertEqual([(row["매수매도"], row["전략"], row["가격"], row["수량"], row["남은수량"]) for row in trades],
                [("매수", "gdcrs", "400", "2", "2"), ("매수", "gdcrs", "400", "1", "3"),
                 ("매도", "ddcrs", "110", "1", "2"), ("매도", "ddcrs", "105", "2", "0")])
            self.assertTrue(all(row["투자구분"] == "paper" and row["잔고조회시각"] and "체결순간 수량 아님" in row["잔고상태"] for row in trades))
            self.assertEqual([row["체결시간"] for row in trades], ["20261026 10:04:10", "20261026 10:04:25", "20261026 10:06:15", "20261026 10:06:30"])
            self.assertEqual(len({(row["주문번호"], row["체결번호"]) for row in trades}), 4)
            summary = exported[-1]
            self.assertEqual((summary["행구분"], summary["매수합계"], summary["매도합계"], summary["순이익"], summary["수익률분모"], summary["수익률"]),
                             ("일일합계", "1200", "320", "-884", "1200", "-73.6667%"))
            with sqlite3.connect(tracker.path) as database:
                self.assertEqual(database.execute("SELECT strategy,side,quantity FROM orders ORDER BY CAST(ord_no AS INTEGER)").fetchall(), [("gdcrs", "buy", 3), ("ddcrs", "sell", 3)])
                self.assertEqual(database.execute("SELECT state,COUNT(*) FROM intents GROUP BY state").fetchall(), [("accepted", 2)])
                saved = database.execute("""SELECT i.side,i.state,i.order_number,r.evidence_json
                    FROM pre_order_reconciliations r JOIN intents i ON i.scope=r.scope AND i.id=r.intent_id""").fetchall()
                self.assertEqual(len(saved), 1)
                self.assertEqual(saved[0][:3], ("buy", "accepted", "24"))
                pre = json.loads(saved[0][3])
                self.assertEqual((pre["pre_order_held_qty"], pre["pre_order_pur_amt_krw"],
                                  pre["remaining_principal_krw"], pre["order_quantity"], pre["limit_price_krw"]),
                                 (0, 0, 1000000, 3, 400))
                self.assertEqual(database.execute("SELECT COUNT(*) FROM fills").fetchone()[0], 4)
                from kiwoom_autotrader.trading.broker_observation_repeats import install_evidence_view
                install_evidence_view(database)
                pending_observations=database.execute("SELECT COUNT(*) FROM broker_observation_evidence WHERE source_api='ka10075'").fetchone()[0]
                self.assertGreaterEqual(pending_observations, 2)
                self.assertEqual(pending_observations, database.execute("SELECT (SELECT COUNT(*) FROM broker_observations WHERE source_api='ka10075')+(SELECT COUNT(*) FROM broker_observation_repeats WHERE source_api='ka10075')").fetchone()[0])
                self.assertEqual(database.execute("SELECT COUNT(*) FROM broker_observations WHERE source_api='00' AND json_extract(payload_json, '$.event_kind')='execution_stream'").fetchone()[0], 4)
            accounting = json.loads((root / 'logs/20261026.execution-accounting.json').read_text())
            self.assertEqual([(order['side'], order['verified_amount'], order['unit_coverage']) for order in accounting['orders']],
                             [('buy', '1200', 'quantity_reconciled'), ('sell', '320', 'quantity_reconciled')])
            for strategy in ("gdcrs", "ddcrs"):
                with sqlite3.connect(root / f"receiver/{strategy}-order-markers.sqlite3") as database:
                    self.assertEqual(database.execute("SELECT COUNT(*) FROM attempts").fetchone()[0], 1)
            messages = "\n".join(telegram.messages)
            self.assertIn("gdcrs · 005930 · 종료봉 10:03", messages)
            self.assertIn("ddcrs · 005930 · 종료봉 10:05", messages)
            self.assertIn("매수 합계 1200원 · 매도 합계 320원", messages)
            self.assertIn("ka10072 기준 계산 순손익 -884", messages)
            self.assertEqual(sum("자동매매 일일결산" in message for message in telegram.messages), 1)
            self.assertIsNone(session.token)
            self.assertIsNone(session.mode)
            self.assertEqual(dd._baseline, {})
            self.assertEqual(gd._baseline, {})
            visible = messages + output.getvalue() + (root / "logs/20261026.csv").read_text(encoding="utf-8-sig")
            visible += (root / "receiver/state.json").read_text()
            for secret in (TOKEN, CONFIG.bot_token, "offline-e2e-key", "offline-e2e-secret"):
                self.assertNotIn(secret, visible)
            artifact_dir = os.environ.get("AI_STOCK_TEST_ARTIFACT_DIR")
            if artifact_dir:
                destination = Path(artifact_dir).expanduser()
                destination.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(root / "logs/20261026.csv", destination / "replayed-cross-fills.csv")
                report = {
                    "environment": "deterministic_replay_not_broker_execution",
                    "passed": True,
                    "description": "외부 HTTP·Telegram·키체인과 시간을 대체한 결정적 재현입니다. 실제 증권사 체결·Telegram 전송 결과가 아닙니다.",
                    "scenario": ["텔레그램 모의 로그인·기간/예산 등록·gdcrs/ddcrs 시작",
                        "완료봉 골든크로스 → 3주 지정가 매수 접수", "매수 2주+1주 부분/전량 체결 재현",
                        "완료봉 데드크로스 → 보유 3주 시장가 매도 접수", "매도 1주+2주 부분/전량 체결 재현",
                        "두 전략 중지", "CSV 기반 장 마감 결산"],
                    "simulated_order_count": len(broker.orders),
                    "simulated_fill_count": len(trades),
                    "simulated_final_held_quantity": broker.held,
                    "simulated_orders": broker.orders,
                    "csv_file": "replayed-cross-fills.csv",
                    "verified_totals": {"buy_principal_krw": summary["매수합계"],
                        "sell_principal_krw": summary["매도합계"], "realized_net_krw": summary["순이익"],
                        "sold_cost_basis_krw": summary["수익률분모"], "return_on_sold_cost_basis": summary["수익률"],
                        "fee_and_tax_krw": "4", "source": "fixture kt00009 executions and ka10072 costs; CSV rollup cross-checked against SQLite"},
                    "verified": {"actual_strategy_order_and_ledger_code": True,
                        "intents_and_signal_markers_durable_before_send": True,
                        "both_orders_paper_krx_only": True, "fills_deduplicated_by_order_and_execution_id": True,
                        "paper_automation_cap_enabled_1000000_krw": True,
                        "position_and_per_order_principal_caps_blocked": True,
                        "pending_query_recorded_in_immutable_ledger": True,
                        "synthetic_websocket_partial_fills_recorded_by_real_recorder": True,
                        "csv_strategies": [row["전략"] for row in trades],
                        "csv_observed_held_quantities": [row["남은수량"] for row in trades],
                        "remaining_quantity_is_observed_holdings_not_order_remaining": True,
                        "stop_both_persisted_and_no_further_chart_requests": True,
                        "one_daily_summary": True, "session_cleared_on_exit": True,
                        "real_network_calls": 0, "real_keychain_calls": 0},
                    "simulated_telegram": {
                        "environment": "simulated_messages_not_sent_to_telegram",
                        "commands": [{"simulated_kst_time": phase[0], "text": command}
                            for phase in telegram.phases for command in phase[1]],
                        "responses": telegram.messages,
                    },
                }
                (destination / "replayed-cross-result.json").write_text(
                    json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
