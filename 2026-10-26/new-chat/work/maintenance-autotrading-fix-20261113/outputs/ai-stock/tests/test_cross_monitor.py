"""Offline death-cross safety tests. All broker/notification actions are injected."""
from datetime import datetime, timedelta
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

from kiwoom_autotrader.api.minute_chart import MinuteBar
from kiwoom_autotrader.api.orders import OrderReceipt
from kiwoom_autotrader.api.session import BrokerSession, IssuedToken, KST
from kiwoom_autotrader.telegram.commands.orders import handle_sell, OrderReplies
from kiwoom_autotrader.trading.cross_monitor import (
    CrossMonitor, CrossMonitorError, OrderMarkers, closed_window, sellable_holdings,
)
from kiwoom_autotrader.utils.http_queue import HTTPNotSentError


def report(quantity="3", *, mode="paper", code="A005930", held="9"):
    return {"mode": mode, "exchange": "KRX", "snapshot_changed": False, "holdings": [
        {"stk_cd": code, "rmnd_qty": held, "trde_able_qty": quantity, "crd_tp": "00", "crd_loan_dt": None}]}


def bars(prices, *, minute=0, day=26):
    return tuple(MinuteBar(datetime(2026, 10, day, 10, minute + index, tzinfo=KST), value)
                 for index, value in enumerate(prices))


class CrossMonitorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.now = datetime(2026, 10, 26, 10, 3, 5, tzinfo=KST)
        self.settings = {"ddcrs_enabled": True, "gdcrs_short_period": 1, "gdcrs_long_period": 3,
                         "market_start_time": "09:00", "market_end_time": "15:30"}
        self.session = BrokerSession(now=lambda: self.now)
        self.session.mode = "paper"
        self.session._issued = IssuedToken("offline-monitor-private-token", "20300101000000")
        self.account = Mock(return_value=report())
        self.chart = Mock(return_value=bars([1, 2, 3]))
        self.sell = Mock(return_value=["모의투자 매도 주문 접수"])
        self.notify = Mock()
        self.saver = Mock(side_effect=lambda updates: self.settings.update(updates))
        self.path = Path(self.temp.name) / "markers.sqlite3"
        self.monitor = self.new_monitor()

    def new_monitor(self, **kwargs):
        return CrossMonitor(ownership_reader=lambda *_: 9, settings_loader=lambda: dict(self.settings), settings_saver=self.saver, now=lambda: self.now,
                            state_path=self.path, account_fetcher=self.account,
                            chart_fetcher=self.chart, sell_handler=self.sell, **kwargs)

    def step(self):
        self.monitor.tick(self.session, self.notify)

    def scan(self):
        self.step()  # full report, no chart/order
        self.now += timedelta(seconds=2)
        self.step()  # one symbol

    def seed(self, prices=(1, 2, 3)):
        self.chart.return_value = bars(prices)
        self.scan()
        self.sell.assert_not_called()

    def next_cross(self):
        self.now = datetime(2026, 10, 26, 10, 4, 5, tzinfo=KST)
        self.chart.return_value = bars([1, 2, 3, 1])
        self.scan()

    def test_initial_seed_then_true_cross_sells_current_sellable_quantity(self):
        self.seed()
        self.account.return_value = report("2", held="9")
        self.next_cross()
        self.sell.assert_called_once()
        self.assertEqual(self.sell.call_args.args, ("sell 005930 2", self.session))
        self.sell.call_args.kwargs["before_send"]()
        self.assertEqual(self.account.call_count, 3)  # initial, new scan, immediate re-read
        self.assertTrue(self.path.exists())
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertFalse(OrderMarkers(self.path).claim("paper", "005930", bars([1, 2, 3, 1])[-1].start))
        self.assertIn("종료봉 10:03", self.notify.call_args.args[0])

    def test_equality_then_below_is_a_cross_but_below_then_below_is_not(self):
        self.seed([3, 3, 3])
        self.now = datetime(2026, 10, 26, 10, 4, 5, tzinfo=KST)
        self.chart.return_value = bars([3, 3, 3, 1])
        self.scan()
        self.sell.assert_called_once()
        self.sell.reset_mock()
        self.now = datetime(2026, 10, 26, 10, 5, 5, tzinfo=KST)
        self.chart.return_value = bars([3, 3, 1, 1], minute=1)
        self.scan()
        self.sell.assert_not_called()

    def test_existing_sell_handler_receives_dispatch_guard_after_marker_commit(self):
        self.seed()
        self.monitor.sell_handler = handle_sell
        def submit(session, side, symbol, quantity, price, *, before_send):
            self.assertEqual((session, side, symbol, quantity, price), (self.session, "sell", "005930", "3", None))
            self.assertFalse(OrderMarkers(self.path).claim("paper", "005930", bars([1, 2, 3, 1])[-1].start))
            before_send()
            return OrderReceipt("paper", "sell", "005930", "3", None, "00001", "https://mockapi.kiwoom.com")
        with patch("kiwoom_autotrader.telegram.commands.orders.submit_order", side_effect=submit) as mocked:
            self.next_cross()
        mocked.assert_called_once()
        self.assertIn("주문번호 00001", self.notify.call_args.args[0])

    def test_current_unclosed_bar_never_enters_sma(self):
        self.chart.return_value = bars([1, 2, 3, 1])
        self.scan()
        self.assertGreater(self.monitor._baseline["005930"].difference, 0)
        self.sell.assert_not_called()

    def test_stale_missing_duplicate_future_and_previous_day_bars_do_not_sell(self):
        bad = [bars([1, 2, 3]), bars([1, 2, 3, 1])[1:3],
               bars([1, 2, 3, 1]) + bars([1], minute=3),
               bars([1, 2, 3, 1, 2, 3]), bars([1, 2, 3, 1], day=25),
               (bars([1, 2, 3, 1])[0],) + bars([1, 2, 3, 1])[2:]]
        for sample in bad:
            with self.subTest(sample=sample):
                self.monitor = self.new_monitor()
                self.now = datetime(2026, 10, 26, 10, 3, 5, tzinfo=KST)
                self.seed()
                self.now = datetime(2026, 10, 26, 10, 4, 5, tzinfo=KST)
                self.chart.return_value = sample
                self.scan()
                self.sell.assert_not_called()

    def test_skipped_observation_and_revised_history_only_seed(self):
        self.seed()
        self.now = datetime(2026, 10, 26, 10, 5, 5, tzinfo=KST)
        self.chart.return_value = bars([2, 3, 1, 1], minute=1)
        self.scan()
        self.sell.assert_not_called()
        self.monitor = self.new_monitor()
        self.now = datetime(2026, 10, 26, 10, 3, 5, tzinfo=KST)
        self.seed()
        self.now = datetime(2026, 10, 26, 10, 4, 5, tzinfo=KST)
        self.chart.return_value = bars([1, 99, 3, 1])
        self.scan()
        self.sell.assert_not_called()

    def test_stop_start_reset_and_period_change_clear_warmup(self):
        for transition in ("stop", "reset", "period"):
            with self.subTest(transition=transition):
                self.settings.update(ddcrs_enabled=True, gdcrs_short_period=1)
                self.monitor = self.new_monitor()
                self.now = datetime(2026, 10, 26, 10, 3, 5, tzinfo=KST)
                self.seed()
                if transition == "stop":
                    self.settings["ddcrs_enabled"] = False
                    self.step()
                    self.settings["ddcrs_enabled"] = True
                elif transition == "reset":
                    self.monitor.reset()
                else:
                    self.settings["gdcrs_short_period"] = 2
                self.next_cross()
                self.sell.assert_not_called()

    def test_mode_or_token_change_cannot_reuse_prior_baseline(self):
        for mode, token in (("real", "new-offline-token"), ("paper", "renewed-offline-token")):
            self.session.mode = "paper"
            self.session._issued = IssuedToken("original-offline-token", "20300101000000")
            self.monitor = self.new_monitor()
            self.account.return_value = report()
            self.now = datetime(2026, 10, 26, 10, 3, 5, tzinfo=KST)
            self.seed()
            self.session.mode = mode
            self.session._issued = IssuedToken(token, "20300101000000")
            self.account.return_value = report(mode=mode)
            self.next_cross()
            self.sell.assert_not_called()

    def test_disabled_invalid_periods_expired_session_and_market_closed_make_no_requests(self):
        cases = [(False, 1, 3, datetime(2026, 10, 26, 10, tzinfo=KST)),
                 (True, 3, 3, datetime(2026, 10, 26, 10, tzinfo=KST)),
                 (True, 4, 3, datetime(2026, 10, 26, 10, tzinfo=KST)),
                 (True, 1, 3, datetime(2026, 10, 25, 10, tzinfo=KST)),
                 (True, 1, 3, datetime(2026, 10, 26, 8, tzinfo=KST)),
                 (True, 1, 3, datetime(2026, 10, 26, 15, 30, tzinfo=KST))]
        for enabled, short, long, now in cases:
            self.settings.update(ddcrs_enabled=enabled, gdcrs_short_period=short, gdcrs_long_period=long)
            self.now = now
            self.monitor = self.new_monitor()
            self.step()
        self.now = datetime(2031, 10, 26, 10, tzinfo=KST)
        self.step()
        self.account.assert_not_called()
        self.chart.assert_not_called()
        self.sell.assert_not_called()

    def test_changed_session_settings_or_time_during_fresh_report_prevents_sell(self):
        for change in ("mode", "stop", "period", "minute"):
            self.session.mode = "paper"
            self.settings.update(ddcrs_enabled=True, gdcrs_short_period=1)
            self.account.side_effect = None
            self.account.return_value = report()
            self.monitor = self.new_monitor()
            self.now = datetime(2026, 10, 26, 10, 3, 5, tzinfo=KST)
            self.seed()
            calls = 0
            def fresh(_):
                nonlocal calls
                calls += 1
                if calls == 2:
                    if change == "mode": self.session.mode = "real"
                    elif change == "stop": self.settings["ddcrs_enabled"] = False
                    elif change == "period": self.settings["gdcrs_short_period"] = 2
                    else: self.now += timedelta(minutes=1)
                return report()
            self.account.side_effect = fresh
            self.next_cross()
            self.sell.assert_not_called()

    def test_zero_fresh_sellable_or_marker_failure_prevents_submission(self):
        self.seed()
        self.account.side_effect = [report(), report("0")]
        self.next_cross()
        self.sell.assert_not_called()
        self.assertTrue(self.path.exists())
        with sqlite3.connect(self.path) as database:
            self.assertEqual(database.execute("SELECT reason FROM missed_signals").fetchone()[0], "no_sellable_quantity")
        self.account.side_effect = None
        self.monitor = self.new_monitor(markers=Mock(claim=Mock(side_effect=OSError("private-token"))))
        self.now = datetime(2026, 10, 26, 10, 3, 5, tzinfo=KST)
        self.seed()
        self.next_cross()
        self.sell.assert_not_called()
        self.assertNotIn("private-token", self.notify.call_args.args[0])

    def test_ambiguous_order_keeps_marker_and_never_replays(self):
        self.seed()
        self.sell.side_effect = OSError("offline-monitor-private-token")
        self.next_cross()
        self.sell.assert_called_once()
        self.assertTrue(self.path.exists())
        self.assertFalse(OrderMarkers(self.path).claim("paper", "005930", bars([1, 2, 3, 1])[-1].start))
        self.assertNotIn("offline-monitor-private-token", self.notify.call_args.args[0])
        self.saver.assert_called_once_with({"ddcrs_enabled": False})
        self.assertFalse(self.settings["ddcrs_enabled"])
        self.monitor = self.new_monitor()
        self.scan()  # startup seeds current negative state
        self.sell.assert_called_once()

    def test_uncertain_handler_result_persists_pause_until_explicit_start(self):
        self.seed()
        self.sell.return_value = OrderReplies(["주문 결과를 확인하지 못했습니다."], uncertain=True)
        self.next_cross()
        self.assertFalse(self.settings["ddcrs_enabled"])
        self.assertIsNone(self.monitor.seconds_until_next())
        previous_calls = self.account.call_count
        self.settings["ddcrs_enabled"] = True
        self.monitor.reset()  # unrelated command cannot release the halt
        self.now += timedelta(minutes=1)
        self.step()
        self.assertEqual(self.account.call_count, previous_calls)
        self.monitor.reset(clear_halt=True)  # successful explicit start ddcrs
        self.step()
        self.assertEqual(self.account.call_count, previous_calls + 1)

    def test_failed_pause_save_keeps_in_memory_halt_and_reports_failure(self):
        self.seed()
        self.saver.side_effect = OSError("private-settings-detail")
        self.sell.return_value = OrderReplies(["확인되지 않은 주문"], uncertain=True)
        self.next_cross()
        self.assertTrue(self.settings["ddcrs_enabled"])
        self.assertIn("저장에 실패", self.notify.call_args.args[0])
        self.assertNotIn("private-settings-detail", self.notify.call_args.args[0])
        previous_calls = self.account.call_count
        self.now += timedelta(minutes=1)
        self.step()
        self.assertEqual(self.account.call_count, previous_calls)

    def test_notification_failure_does_not_retry_or_escape_the_tick(self):
        self.seed()
        self.notify.side_effect = OSError("notification failed")
        self.next_cross()
        self.sell.assert_called_once()
        self.assertFalse(OrderMarkers(self.path).claim("paper", "005930", bars([1, 2, 3, 1])[-1].start))
        self.step()
        self.sell.assert_called_once()

    def test_one_symbol_observation_per_tick(self):
        data = report()
        data["holdings"].append(report(code="A000660")["holdings"][0])
        self.account.return_value = data
        self.step()
        self.chart.assert_not_called()
        self.now += timedelta(seconds=2)
        self.step()
        self.chart.assert_called_once()
        self.sell.assert_not_called()

    def test_unfinished_scans_rotate_fairly_at_next_minute(self):
        data = report()
        data["holdings"] += [report(code="A000660")["holdings"][0], report(code="A035420")["holdings"][0]]
        self.account.return_value = data
        self.scan()
        self.assertEqual(self.chart.call_args.args[1], "000660")
        self.now = datetime(2026, 10, 26, 10, 4, 5, tzinfo=KST)
        self.chart.return_value = bars([1, 2, 3, 1])
        self.scan()
        self.assertEqual(self.chart.call_args.args[1], "005930")
        self.now = datetime(2026, 10, 26, 10, 5, 5, tzinfo=KST)
        self.chart.return_value = bars([1, 2, 3, 1, 1])
        self.scan()
        self.assertEqual(self.chart.call_args.args[1], "035420")
        self.sell.assert_not_called()

    def test_dispatch_guard_revalidates_stop_and_candle_freshness(self):
        self.seed()
        self.next_cross()
        guard = self.sell.call_args.kwargs["before_send"]
        guard()
        self.settings["ddcrs_enabled"] = False
        with self.assertRaises(CrossMonitorError):
            guard()
        self.settings["ddcrs_enabled"] = True
        self.now += timedelta(minutes=1)
        with self.assertRaises(CrossMonitorError):
            guard()

    def test_external_stop_before_tick_resets_observations_without_requests(self):
        self.seed()
        stop_path = Path(self.temp.name) / "stop.request"
        self.monitor.stop_requested = stop_path.exists
        stop_path.touch()
        self.account.reset_mock()
        self.chart.reset_mock()
        self.step()
        self.account.assert_not_called()
        self.chart.assert_not_called()
        self.sell.assert_not_called()
        self.assertEqual(self.monitor._baseline, {})
        self.assertIsNone(self.monitor.seconds_until_next())

    def test_external_stop_or_reset_during_each_read_discards_observation(self):
        stop_path = Path(self.temp.name) / "stop.request"
        for transition in ("stop", "reset"):
            for stage in ("scan", "chart", "fresh"):
                with self.subTest(transition=transition, stage=stage):
                    stop_path.unlink(missing_ok=True)
                    self.path.unlink(missing_ok=True)
                    self.account.side_effect = None
                    self.chart.side_effect = None
                    self.account.reset_mock()
                    self.chart.reset_mock()
                    self.monitor = self.new_monitor(stop_requested=stop_path.exists)
                    self.now = datetime(2026, 10, 26, 10, 3, 5, tzinfo=KST)
                    self.seed()
                    account_calls = 0

                    def change():
                        if transition == "stop":
                            stop_path.touch()
                        else:
                            self.monitor.reset()

                    def account(_):
                        nonlocal account_calls
                        account_calls += 1
                        if (stage == "scan" and account_calls == 1) or (stage == "fresh" and account_calls == 2):
                            change()
                        return report()

                    def chart(*_, **__):
                        if stage == "chart":
                            change()
                        return bars([1, 2, 3, 1])

                    self.account.side_effect = account
                    self.chart.side_effect = chart
                    self.now = datetime(2026, 10, 26, 10, 4, 5, tzinfo=KST)
                    self.step()  # new-minute holdings scan
                    if stage != "scan":
                        self.now += timedelta(seconds=2)
                        self.step()
                    self.sell.assert_not_called()
                    self.assertEqual(self.monitor._baseline, {})
                    if self.path.exists():
                        with sqlite3.connect(self.path) as database:
                            attempts = database.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='attempts'").fetchone()
                            self.assertIsNone(attempts)
                            self.assertEqual(database.execute("SELECT reason FROM missed_signals").fetchone()[0], "context_changed")

    def test_external_stop_or_reset_in_http_queue_prevents_transport(self):
        stop_path = Path(self.temp.name) / "stop.request"
        for transition in ("stop", "reset"):
            with self.subTest(transition=transition):
                stop_path.unlink(missing_ok=True)
                self.path.unlink(missing_ok=True)
                self.monitor = self.new_monitor(stop_requested=stop_path.exists)
                self.now = datetime(2026, 10, 26, 10, 3, 5, tzinfo=KST)
                self.seed()
                self.monitor.sell_handler = handle_sell
                opener = Mock()
                opener.open.side_effect = AssertionError("HTTP transport must not run")

                def queued(_opener, _request, *, before_send, **_):
                    self.assertIs(_opener, opener)
                    self.assertFalse(OrderMarkers(self.path).claim("paper", "005930", bars([1, 2, 3, 1])[-1].start))
                    if transition == "stop":
                        stop_path.touch()
                    else:
                        self.monitor.reset()
                    try:
                        before_send()
                    except CrossMonitorError:
                        raise HTTPNotSentError("cancelled before transport") from None
                    self.fail("stop/reset did not reject the queued order")

                with patch("kiwoom_autotrader.api.orders.urllib.request.build_opener", return_value=opener), \
                     patch("kiwoom_autotrader.api.orders.queued_open", side_effect=queued) as queue:
                    self.next_cross()
                queue.assert_called_once()
                opener.open.assert_not_called()
                self.assertFalse(self.monitor._halted)
                self.assertTrue(self.settings["ddcrs_enabled"])
                self.assertIn("전송하지 않았습니다", self.notify.call_args.args[0])

    def test_inactive_monitor_does_not_shorten_listener_poll(self):
        self.assertIsNone(self.monitor.seconds_until_next())
        self.scan()
        self.assertIsNotNone(self.monitor.seconds_until_next())
        self.settings["ddcrs_enabled"] = False
        self.step()
        self.assertIsNone(self.monitor.seconds_until_next())


class HoldingAndMarkerTests(unittest.TestCase):
    def test_sellable_required_and_credit_duplicate_or_non_stock_rows_excluded(self):
        self.assertEqual(sellable_holdings(report("2", held="9"), "paper"), {"005930": 2})
        for field, value in (("trde_able_qty", None), ("trde_able_qty", "-2"), ("rmnd_qty", None)):
            data = report()
            data["holdings"][0][field] = value
            with self.assertRaises(CrossMonitorError):
                sellable_holdings(data, "paper")
        for field, value in (("crd_tp", "03"), ("crd_tp", None), ("stk_cd", "J005930"), ("stk_cd", "Q005930")):
            data = report()
            data["holdings"][0][field] = value
            self.assertEqual(sellable_holdings(data, "paper"), {})
        data = report()
        data["holdings"] *= 2
        self.assertEqual(sellable_holdings(data, "paper"), {})
        data = report()
        data["snapshot_changed"] = True
        with self.assertRaises(CrossMonitorError):
            sellable_holdings(data, "paper")

    def test_persistent_markers_separate_modes_symbols_and_candles(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "markers.db"
            moment = datetime(2026, 10, 26, 10, tzinfo=KST)
            self.assertTrue(OrderMarkers(path).claim("paper", "005930", moment))
            self.assertFalse(OrderMarkers(path).claim("paper", "005930", moment))
            self.assertTrue(OrderMarkers(path).claim("real", "005930", moment))
            self.assertTrue(OrderMarkers(path).claim("paper", "000660", moment))
            self.assertTrue(OrderMarkers(path).claim("paper", "005930", moment + timedelta(minutes=1)))


if __name__ == "__main__":
    unittest.main()
