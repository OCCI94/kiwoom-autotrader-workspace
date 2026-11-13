"""Golden-cross tests with fake sessions, charts, orders and notifications only."""
from datetime import datetime, timedelta
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

from kiwoom_autotrader.api.minute_chart import MinuteBar
from kiwoom_autotrader.api.orders import OrderReceipt
from kiwoom_autotrader.api.session import BrokerSession, IssuedToken, KST
from kiwoom_autotrader.telegram.commands.orders import OrderReplies, handle_buy
from kiwoom_autotrader.trading.golden_cross import GoldenCrossError, GoldenCrossMonitor, GoldenOrderMarkers


def bars(prices, *, minute=0, day=26, hour=10):
    start = datetime(2026, 10, day, hour, minute, tzinfo=KST)
    return tuple(MinuteBar(start + timedelta(minutes=index), value) for index, value in enumerate(prices))


class GoldenCrossTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "gdcrs.sqlite3"
        self.now = datetime(2026, 10, 26, 10, 3, 5, tzinfo=KST)
        self.settings = {"gdcrs_enabled": True, "ddcrs_enabled": True,
            "gdcrs_short_period": 1, "gdcrs_long_period": 3,
            "gdcrs_stocks": [{"code": "005930", "budget": 100000}],
            "market_start_time": "09:00", "market_end_time": "15:30"}
        self.session = BrokerSession(now=lambda: self.now)
        self.session.mode = "paper"
        self.session._issued = IssuedToken("offline-golden-private-token", "20300101000000")
        self.chart = Mock(return_value=bars([3, 2, 1]))
        self.buy = Mock(return_value=["모의투자 매수 주문 접수"])
        self.saver = Mock(side_effect=lambda updates: self.settings.update(updates))
        self.notify = Mock()
        self.stop = False
        self.monitor = self.make_monitor()

    def make_monitor(self, **kwargs):
        return GoldenCrossMonitor(settings_loader=lambda: dict(self.settings), settings_saver=self.saver,
            chart_fetcher=self.chart, buy_handler=self.buy, now=lambda: self.now, state_path=self.path,
            stop_requested=lambda: self.stop, **kwargs)

    def tick(self):
        self.monitor.tick(self.session, self.notify)

    def seed(self, prices=(3, 2, 1)):
        self.chart.return_value = bars(prices, day=self.now.day)
        self.tick()
        self.buy.assert_not_called()

    def cross(self, prices=(3, 2, 1, 4)):
        self.now = self.now.replace(hour=10, minute=4, second=5)
        self.chart.return_value = bars(prices, day=self.now.day)
        self.tick()

    def test_initial_seed_then_cross_calls_existing_budget_buy_after_durable_marker(self):
        self.seed()
        def buy(text, session, *, before_send):
            self.assertEqual((text, session), ("buy 005930 max 100000", self.session))
            self.assertFalse(GoldenOrderMarkers(self.path).claim("paper", "005930", bars([1], minute=3)[0].start))
            before_send()
            return ["주문번호 0000001 · 주문 접수"]
        self.buy.side_effect = buy
        self.cross()
        self.buy.assert_called_once()
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertIn("gdcrs · 005930 · 종료봉 10:03", self.notify.call_args.args[0])
        self.assertNotIn(self.session.token, str(self.notify.call_args))

    def test_known_no_send_skip_keeps_marker_without_notification_or_replay(self):
        self.buy.return_value = OrderReplies([], skipped_reason="budget_below_one_share")
        self.seed()
        self.cross()
        self.buy.assert_called_once()
        self.notify.assert_not_called()
        self.assertFalse(self.monitor._halted)
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM attempts").fetchone()[0], 1)
        self.tick()
        self.buy.assert_called_once()

    def test_theme_cap_rejection_is_recorded_as_exact_skip(self):
        self.buy.return_value = OrderReplies([], submitted=False, decision_reason='theme_concentration_limit_reached')
        self.seed(); self.cross()
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute('SELECT outcome,skip_reason FROM entry_decisions').fetchall(),
                             [('skipped','theme_concentration_limit_reached')])
        self.assertFalse(self.monitor._halted)

    def test_strict_boundary_zero_to_positive_only(self):
        self.seed([3, 3, 1])
        self.cross([3, 3, 1, 2])  # below -> exactly equal: no order
        self.buy.assert_not_called()
        self.assertEqual(self.monitor._baseline["005930"].difference, 0)
        self.now = self.now.replace(minute=5)
        self.chart.return_value = bars([3, 1, 2, 3], minute=1)
        self.tick()
        self.buy.assert_called_once()
        self.buy.reset_mock()
        self.now = self.now.replace(minute=6)
        self.chart.return_value = bars([1, 2, 3, 4], minute=2)
        self.tick()
        self.buy.assert_not_called()  # positive -> positive never repeats

    def test_multiple_new_crosses_same_day_have_no_daily_count_cap(self):
        self.seed()
        self.cross()  # first crossing at 10:03
        self.assertEqual(self.buy.call_count, 1)
        for minute, prices in ((5, [2, 1, 4, 1]), (6, [1, 4, 1, 5]),
                               (7, [4, 1, 5, 6])):
            self.now = self.now.replace(minute=minute)
            self.chart.return_value = bars(prices, minute=minute - 4)
            self.tick()
        self.assertEqual(self.buy.call_count, 2)  # second crossing; positive continuation ignored
        self.tick()
        self.assertEqual(self.buy.call_count, 2)  # same bar not replayed
        self.assertEqual(self.monitor.status()["observed_closed_bars"]["005930"],
                         "2026-10-26T10:06:00+09:00")

    def test_equality_seed_and_current_unclosed_bar(self):
        self.chart.return_value = bars([3, 3, 3, 900])
        self.tick()
        self.assertEqual(self.monitor._baseline["005930"].difference, 0)
        self.cross([3, 3, 3, 4])
        self.buy.assert_called_once()

    def test_missing_duplicate_stale_future_and_previous_day_data_never_buy(self):
        samples = [bars([3, 2, 1]), bars([3, 2, 1, 4])[2:],
            bars([3, 2, 1, 4]) + bars([4], minute=3), bars([3, 2, 1, 4, 8, 9]),
            bars([3, 2, 1, 4], day=25), (bars([3, 2, 1, 4])[0],) + bars([3, 2, 1, 4])[2:]]
        for sample in samples:
            with self.subTest(sample=sample):
                self.now = self.now.replace(minute=3)
                self.monitor = self.make_monitor()
                self.seed()
                self.now = self.now.replace(minute=4)
                self.chart.return_value = sample
                self.tick()
                self.buy.assert_not_called()

    def test_gap_and_revised_history_only_reseed(self):
        self.seed()
        self.now = self.now.replace(minute=5)
        self.chart.return_value = bars([2, 1, 4, 8], minute=1)
        self.tick()
        self.buy.assert_not_called()
        self.monitor = self.make_monitor()
        self.now = self.now.replace(minute=3)
        self.seed()
        self.cross([3, 99, 1, 4])
        self.buy.assert_not_called()

    def test_disabled_empty_invalid_periods_budgets_or_market_make_no_chart_request(self):
        bad = [{"gdcrs_enabled": False}, {"gdcrs_stocks": []}, {"gdcrs_short_period": 3},
            {"gdcrs_short_period": 4}, {"gdcrs_short_period": True},
            {"gdcrs_stocks": [{"code": "005930", "budget": 0}]},
            {"gdcrs_stocks": [{"code": "005930", "budget": True}]},
            {"gdcrs_stocks": [{"code": "005930", "budget": 1}] * 2},
            {"market_start_time": "25:00"}, {"market_start_time": "16:00", "market_end_time": "17:00"}]
        original = dict(self.settings)
        for update in bad:
            self.settings = {**original, **update}
            self.monitor = self.make_monitor()
            self.tick()
        self.chart.assert_not_called()
        self.buy.assert_not_called()

    def test_stop_restart_period_market_budget_list_and_session_changes_clear_warmup(self):
        for change in ("stop", "reset", "period", "market", "budget", "list", "token", "mode"):
            with self.subTest(change=change):
                self.setUp_case()
                self.seed()
                if change == "stop":
                    self.settings["gdcrs_enabled"] = False
                    self.tick()
                    self.settings["gdcrs_enabled"] = True
                elif change == "reset": self.monitor.reset()
                elif change == "period": self.settings["gdcrs_short_period"] = 2
                elif change == "market": self.settings["market_start_time"] = "09:30"
                elif change == "budget": self.settings["gdcrs_stocks"] = [{"code": "005930", "budget": 120000}]
                elif change == "list": self.settings["gdcrs_stocks"] += [{"code": "000660", "budget": 120000}]
                elif change == "token": self.session._issued = IssuedToken("renewed-offline-token", "20300101000000")
                else: self.session.mode = "real"
                self.cross()
                self.buy.assert_not_called()

    def setUp_case(self):
        self.settings.update(gdcrs_enabled=True, gdcrs_short_period=1, gdcrs_long_period=3,
            gdcrs_stocks=[{"code": "005930", "budget": 100000}], market_start_time="09:00", market_end_time="15:30")
        self.session.mode = "paper"
        self.session._issued = IssuedToken("offline-golden-private-token", "20300101000000")
        self.now = datetime(2026, 10, 26, 10, 3, 5, tzinfo=KST)
        self.chart.side_effect = None
        self.buy.reset_mock()
        self.monitor = self.make_monitor()

    def test_market_close_weekend_and_next_weekday_start_seed_without_disabling(self):
        self.seed()
        for moment in (datetime(2026, 10, 26, 15, 30, tzinfo=KST), datetime(2026, 10, 31, 10, 3, tzinfo=KST)):
            self.now = moment
            calls = self.chart.call_count
            self.tick()
            self.assertEqual(self.chart.call_count, calls)
            self.assertEqual(self.monitor._baseline, {})
            self.assertTrue(self.settings["gdcrs_enabled"])
        self.now = datetime(2026, 10, 27, 10, 4, 5, tzinfo=KST)
        self.chart.return_value = bars([3, 2, 1, 4], day=27)
        self.tick()
        self.buy.assert_not_called()
        self.saver.assert_not_called()

    def test_day_change_without_observing_close_and_expired_token_do_not_buy(self):
        self.seed()
        self.now = self.now.replace(day=27, minute=4)
        self.chart.return_value = bars([3, 2, 1, 4], day=27)
        self.tick()
        self.buy.assert_not_called()
        calls = self.chart.call_count
        self.session._issued = IssuedToken("expired-offline-token", "20261027090000")
        self.tick()
        self.assertEqual(self.chart.call_count, calls)

    def test_list_remove_clear_disable_or_reset_during_chart_prevents_order(self):
        for change in ("remove", "clear", "disable", "reset", "shutdown"):
            with self.subTest(change=change):
                self.setUp_case()
                self.stop = False
                self.seed()
                def changed(*args, **kwargs):
                    if change in ("remove", "clear"): self.settings["gdcrs_stocks"] = []
                    elif change == "disable": self.settings["gdcrs_enabled"] = False
                    elif change == "reset": self.monitor.reset()
                    else: self.stop = True
                    return bars([3, 2, 1, 4])
                self.chart.side_effect = changed
                self.cross()
                self.buy.assert_not_called()

    def test_send_guard_rechecks_budget_list_stop_reset_session_day_and_minute(self):
        for change in ("budget", "remove", "clear", "stop", "reset", "token", "mode", "day", "minute", "shutdown"):
            with self.subTest(change=change):
                self.setUp_case()
                self.stop = False
                self.seed()
                self.cross()
                guard = self.buy.call_args.kwargs["before_send"]
                guard()
                if change == "budget": self.settings["gdcrs_stocks"] = [{"code": "005930", "budget": 120000}]
                elif change in ("remove", "clear"): self.settings["gdcrs_stocks"] = []
                elif change == "stop": self.settings["gdcrs_enabled"] = False
                elif change == "reset": self.monitor.reset()
                elif change == "token": self.session._issued = IssuedToken("another-offline-token", "20300101000000")
                elif change == "mode": self.session.mode = "real"
                elif change == "day": self.now += timedelta(days=1)
                elif change == "minute": self.now += timedelta(minutes=1)
                else: self.stop = True
                with self.assertRaises((GoldenCrossError, RuntimeError)):
                    guard()
                # Independent fresh state for the next guard test.
                self.path.unlink(missing_ok=True)

    def test_send_guard_rechecks_clock_after_slow_settings_read(self):
        self.seed()
        self.cross()
        guard = self.buy.call_args.kwargs["before_send"]
        self.now = self.now.replace(second=59)
        def slow_settings():
            self.now += timedelta(seconds=2)
            return dict(self.settings)
        self.monitor.settings_loader = slow_settings
        with self.assertRaises(GoldenCrossError):
            guard()

    def test_one_symbol_per_tick_registration_order_and_fair_rotation(self):
        self.settings["gdcrs_stocks"] = [{"code": code, "budget": 100000} for code in ("005930", "000660", "035420")]
        for minute in (3, 4, 5):
            self.now = self.now.replace(minute=minute)
            self.chart.return_value = bars([3, 2, 1], minute=minute-3)
            self.tick()
        self.assertEqual([call.args[1] for call in self.chart.call_args_list], ["005930", "000660", "035420"])
        self.buy.assert_not_called()

    def test_marker_failure_blocks_handler_and_error_does_not_leak(self):
        markers = Mock()
        markers.claim.side_effect = OSError("private token-shaped detail")
        self.monitor = self.make_monitor(markers=markers)
        self.seed()
        self.cross()
        self.buy.assert_not_called()
        self.assertNotIn("private token-shaped detail", self.notify.call_args.args[0])

    def test_same_signal_durable_replay_suppression_and_mode_namespace(self):
        self.seed()
        self.cross()
        self.buy.assert_called_once()
        self.buy.reset_mock()
        self.now = self.now.replace(minute=3)
        self.monitor = self.make_monitor()
        self.seed()
        self.cross()
        self.buy.assert_not_called()
        candle = bars([1], minute=3)[0].start
        self.assertTrue(GoldenOrderMarkers(self.path).claim("real", "005930", candle))

    def test_uncertain_reply_or_handler_exception_pauses_only_golden_without_retry(self):
        for mode in ("reply", "exception"):
            with self.subTest(mode=mode):
                self.setUp_case()
                self.path.unlink(missing_ok=True)
                self.buy.side_effect = OSError("private provider text") if mode == "exception" else None
                self.buy.return_value = OrderReplies(["접수 확인 필요"], uncertain=True)
                self.seed()
                self.cross()
                self.assertFalse(self.settings["gdcrs_enabled"])
                self.assertTrue(self.settings["ddcrs_enabled"])
                self.assertTrue(self.monitor._halted)
                self.assertIsNone(self.monitor.seconds_until_next())
                self.settings["gdcrs_enabled"] = True
                self.monitor.reset()  # ordinary reset must not clear halt
                self.now += timedelta(minutes=1)
                self.tick()
                self.buy.assert_called_once()
                self.assertNotIn("private provider text", str(self.notify.call_args))
                self.monitor.reset(clear_halt=True)
                self.assertFalse(self.monitor._halted)

    def test_pause_persistence_failure_still_halts_and_notification_failure_never_retries(self):
        self.saver.side_effect = OSError("private store failure")
        self.buy.return_value = OrderReplies(["unknown"], uncertain=True)
        self.seed()
        self.cross()
        self.assertTrue(self.monitor._halted)
        self.assertIn("저장에 실패", self.notify.call_args.args[0])
        self.assertTrue(self.settings["gdcrs_enabled"])
        self.notify.side_effect = OSError("notification failed")
        self.monitor.reset()
        self.now += timedelta(minutes=1)
        self.tick()
        self.buy.assert_called_once()

    def test_disabled_or_external_shutdown_has_no_wake_or_requests(self):
        self.settings["gdcrs_enabled"] = False
        self.tick()
        self.assertIsNone(self.monitor.seconds_until_next())
        self.settings["gdcrs_enabled"] = True
        self.stop = True
        self.tick()
        self.assertIsNone(self.monitor.seconds_until_next())
        self.chart.assert_not_called()

    def test_budget_handler_integration_uses_quote_limit_and_current_budget(self):
        self.monitor.buy_handler = handle_buy
        self.seed()
        def submit(session, side, symbol, quantity, price, *, before_send):
            self.assertEqual((side, symbol, quantity, price), ("buy", "005930", "3", "30000"))
            before_send()
            self.assertFalse(GoldenOrderMarkers(self.path).claim("paper", "005930", bars([1], minute=3)[0].start))
            return OrderReceipt("paper", "buy", "005930", "3", "30000", "0000001", "https://mockapi.kiwoom.com")
        with patch("kiwoom_autotrader.telegram.commands.orders.fetch_stock_info", return_value={"stk_cd": "005930", "cur_prc": "+30000"}), patch("kiwoom_autotrader.telegram.commands.orders.submit_order", side_effect=submit) as order:
            self.cross()
        order.assert_called_once()
        self.assertIn("최대 매수대금 90,000원", self.notify.call_args.args[0])

    def test_one_share_unaffordable_or_list_cleared_during_quote_never_submits(self):
        for change in ("expensive", "clear", "budget", "minute"):
            with self.subTest(change=change):
                self.setUp_case()
                self.path.unlink(missing_ok=True)
                self.monitor.buy_handler = handle_buy
                self.seed()
                def quote(*args):
                    if change == "clear": self.settings["gdcrs_stocks"] = []
                    elif change == "budget": self.settings["gdcrs_stocks"] = [{"code": "005930", "budget": 1}]
                    elif change == "minute": self.now += timedelta(minutes=1)
                    return {"stk_cd": "005930", "cur_prc": "100001" if change == "expensive" else "30000"}
                with patch("kiwoom_autotrader.telegram.commands.orders.fetch_stock_info", side_effect=quote), patch("kiwoom_autotrader.telegram.commands.orders.submit_order") as order:
                    self.cross()
                order.assert_not_called()
                self.assertFalse(self.monitor._halted)
                with sqlite3.connect(self.path) as db:
                    self.assertEqual(db.execute("SELECT COUNT(*) FROM attempts").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
