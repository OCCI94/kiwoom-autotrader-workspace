import unittest
from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo
from types import SimpleNamespace
from unittest.mock import patch
from pathlib import Path
import tempfile
import urllib.request

from kiwoom_autotrader.api.minute_chart import MinuteBar
from kiwoom_autotrader.trading.multi_strategy import (EntryState, MultiStrategyMonitor, S2, S3, S4, entry_signal,
                                                       execute_paper_strategy, exit_signal, fresh_owned_basis, next_entry_state, valid_completed_bars)


class MultiStrategyTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 30, 11, 31, 1, tzinfo=ZoneInfo("Asia/Seoul"))
        self.bars = [MinuteBar(self.now.replace(second=0)-timedelta(minutes=21-i), 100,
                               100 if i < 20 else 150, source_symbol="005930", source_mode="paper",
                               source_query_started_at=self.now, source_response_sha256="a" * 64,
                               open_price=100, high_price=101,
                               low_price=99) for i in range(21)]
        self.market = {"symbol": "005930", "change_pct": "20.1", "current_price": 100,
                       "upper_limit_price": 130, "rising_volume_surge": None,
                       "day_open":99,"source":"ka10001_normalized_response","source_sha256":"a"*64,
                       "request_started_at":self.now.isoformat(),"available_at":self.now.isoformat(),
                       "observed_at":self.now.isoformat()}

    def test_valid_latest_and_missing_ohlc_or_gap(self):
        self.assertTrue(valid_completed_bars(self.bars, "005930", self.now))
        self.assertFalse(valid_completed_bars(self.bars[:-1], "005930", self.now))
        self.assertFalse(valid_completed_bars(self.bars, "000000", self.now))
        self.assertFalse(valid_completed_bars(self.bars, "005930", self.now + timedelta(minutes=1)))

    def test_s3_requires_live_20_percent_cross_and_real_limit(self):
        state = EntryState(last_bar_start=self.bars[-1].start-timedelta(minutes=1), previous_change_pct=Decimal("19.9"))
        self.assertEqual(entry_signal(S3, state, self.bars, self.market, symbol="005930", now=self.now), "observed_20pct_crossing")
        self.market["upper_limit_price"] = None
        self.assertIsNone(entry_signal(S3, state, self.bars, self.market, symbol="005930", now=self.now))
        self.market["upper_limit_price"] = 130
        self.assertIsNone(entry_signal(S3, EntryState(), self.bars, self.market, symbol="005930", now=self.now))
        self.assertIsNone(entry_signal(S3, EntryState(state.last_bar_start, previous_change_pct=Decimal("20.0")),
                                       self.bars, self.market, symbol="005930", now=self.now))

    def test_s2_requires_positive_surge_pullback_and_volume(self):
        state = EntryState(self.bars[-1].start-timedelta(minutes=1), 99, True)
        self.market.update(change_pct="5", rising_volume_surge=True)
        self.assertEqual(entry_signal(S2, state, self.bars, self.market, symbol="005930", now=self.now),
                         "shallow_pullback_high_breakout")
        self.market["rising_volume_surge"] = None
        self.assertIsNone(entry_signal(S2, state, self.bars, self.market, symbol="005930", now=self.now))
        self.market["rising_volume_surge"] = True
        self.market["change_pct"] = "NaN"
        self.assertIsNone(entry_signal(S2, state, self.bars, self.market, symbol="005930", now=self.now))

    def test_missing_current_theme_evidence_does_not_send_signal(self):
        state = EntryState(self.bars[-1].start-timedelta(minutes=1), 99, True)
        self.assertIsNone(entry_signal(S4, state, self.bars, self.market, symbol="005930", now=self.now))
        self.market["dart_evidence"] = {"symbol": "005930", "verified": True, "source_sha256": "a"*64,
                                        "receipt_no": "20261030000001", "available_at": self.now.isoformat()}
        self.assertEqual(entry_signal(S4, state, self.bars, self.market, symbol="005930", now=self.now),
                         "theme_leader_or_verified_disclosure")
        self.market["dart_evidence"]["available_at"] = (self.now + timedelta(seconds=1)).isoformat()
        self.assertIsNone(entry_signal(S4, state, self.bars, self.market, symbol="005930", now=self.now))
        self.market["dart_evidence"]["available_at"] = self.now.isoformat()
        self.market["dart_evidence"]["symbol"] = "000000"
        self.assertIsNone(entry_signal(S4, state, self.bars, self.market, symbol="005930", now=self.now))

    def test_first_observation_is_only_baseline(self):
        state = next_entry_state(EntryState(), self.bars[-1], self.market)
        self.assertIsNone(entry_signal(S3, state, self.bars, self.market, symbol="005930", now=self.now))

    def test_known_earlier_session_high_prevents_false_surge_breakout(self):
        previous = next_entry_state(EntryState(), self.bars[-1], {"day_high": 120, "change_pct": "5"})
        self.assertEqual(previous.prior_peak, 120)
        now = self.now + timedelta(minutes=1)
        bars = [MinuteBar(now.replace(second=0)-timedelta(minutes=21-i), 110, 200 if i == 20 else 100,
                          source_symbol="005930", source_mode="paper", source_query_started_at=now,
                          source_response_sha256="a"*64, open_price=110, high_price=112, low_price=109)
                for i in range(21)]
        market = {"symbol": "005930", "change_pct": "5", "rising_volume_surge": True, "day_high": 120}
        self.assertIsNone(entry_signal(S2, previous, bars, market, symbol="005930", now=now))

    def test_exit_half_and_full_and_stop_are_based_on_proven_basis(self):
        self.assertEqual(exit_signal(S2, qty=10, basis_krw=1000, price=105, sma20=100), ("take_profit_half_5pct", 5))
        self.assertEqual(exit_signal(S2, qty=10, basis_krw=1000, price=110, sma20=100), ("take_profit_10pct", 10))
        self.assertEqual(exit_signal(S3, qty=10, basis_krw=1000, price=97, sma20=96), ("stop_loss_or_sma20", 10))
        self.assertIsNone(exit_signal(S2, qty=10, basis_krw=0, price=110, sma20=100))
        self.assertEqual(exit_signal(S2, qty=10, basis_krw=1000, price=100, sma20=Decimal("100.05")),
                         ("stop_loss_or_sma20", 10))

    def test_gap_resets_previous_peak_and_cross_baseline(self):
        state = EntryState(self.bars[-1].start-timedelta(minutes=2), 150, True, Decimal("19"))
        new = next_entry_state(state, self.bars[-1], self.market)
        self.assertEqual(new.prior_peak, 101)
        self.assertFalse(new.pulled_back)

    def test_owned_position_requires_matching_fresh_broker_cash_holding(self):
        position = {"status": "owned", "owned_qty": 10}
        report = {"mode": "paper", "exchange": "KRX", "snapshot_changed": False,
                  "holdings": [{"stk_cd": "A005930", "crd_tp": "00", "crd_loan_dt": "",
                                "rmnd_qty": "10", "trde_able_qty": "9", "pur_amt": "98700"}]}
        self.assertEqual(fresh_owned_basis(None, "005930", position, lambda _: report), (98700, 9))
        report["holdings"][0]["rmnd_qty"] = "11"
        with self.assertRaises(ValueError):
            fresh_owned_basis(None, "005930", position, lambda _: report)
        report["holdings"][0].update(rmnd_qty="10", crd_tp="03")
        with self.assertRaises(ValueError):
            fresh_owned_basis(None, "005930", position, lambda _: report)

    def test_monitor_never_queries_or_orders_without_ownership_gateway(self):
        calls = []
        settings = {"paper_multi_strategy_enabled": True, "paper_multi_strategy_ids": [S3],
                    "gdcrs_stocks": [{"code": "005930", "budget": 1000000}]}
        monitor = MultiStrategyMonitor(settings_loader=lambda: settings, now=lambda: self.now,
                                       chart_fetcher=lambda *a, **kw: calls.append("chart"))
        with patch("kiwoom_autotrader.trading.multi_strategy.session_identity", return_value=("paper", "test")):
            monitor.tick(object(), lambda x: calls.append("notification"))
        self.assertEqual(calls, [])
        self.assertEqual(monitor.status()["error"], "strategy_ownership_gateway_unavailable")

    def test_monitor_crosses_only_after_baseline_and_halts_on_uncertain_dispatch(self):
        moments = [self.now]
        settings = {"paper_multi_strategy_enabled": True, "paper_multi_strategy_ids": [S2],
                    "gdcrs_stocks": [{"code": "005930", "budget": 1000000}]}
        class Provider:
            def fetch(self, symbol, as_of=None):
                return {"symbol": symbol, "change_pct": "5.0", "rising_volume_surge": True,
                        "current_price": 102, "upper_limit_price": 130,"day_open":99,
                        "source":"ka10001_normalized_response","source_sha256":"a"*64,
                        "request_started_at":moments[0].isoformat(),"available_at":moments[0].isoformat(),
                        "observed_at":moments[0].isoformat()}
            def is_fresh(self, context, now):
                return True
        self_now = self.now
        tracker = SimpleNamespace(multi_strategy_position=lambda *args: {"status": "flat"},
                                  multi_strategy_open_symbols=lambda *args: {"status": "available", "symbols": []})
        session = SimpleNamespace(paper_automation_reconcile=SimpleNamespace(_tracker=tracker))
        calls = []
        def chart(_session, symbol, count):
            return [MinuteBar(moments[0].replace(second=0)-timedelta(minutes=21-i),
                              102 if i == 20 and moments[0] >= self_now+timedelta(minutes=2) else 100,
                              200 if i == 20 else 100,
                              source_symbol=symbol, source_mode="paper", source_query_started_at=moments[0],
                              source_response_sha256="a"*64, open_price=100,
                              high_price=103 if i == 20 and moments[0] >= self_now+timedelta(minutes=2) else 101, low_price=99)
                    for i in range(21)]
        def execute(**kwargs):
            calls.append(kwargs)
            raise RuntimeError("unknown outcome")
        monitor = MultiStrategyMonitor(settings_loader=lambda: settings, now=lambda: moments[0],
                                       chart_fetcher=chart, provider_factory=lambda _: Provider(), executor=execute,
                                       cooldown_check=lambda *args: True)
        with patch("kiwoom_autotrader.trading.multi_strategy.session_identity", return_value=("paper", "test")):
            monitor.tick(session, lambda _: None)
            self.assertEqual(calls, [])
            moments[0] += timedelta(minutes=1)
            monitor.tick(session, lambda _: None)
            self.assertEqual(calls, [])
            moments[0] += timedelta(minutes=1)
            monitor.tick(session, lambda _: None)
        self.assertEqual(len(calls), 1)
        self.assertTrue(monitor.status()["halted"])
        self.assertEqual(calls[0]["budget"], 1000000)

    def test_removed_owned_symbol_stops_without_ranking_or_chart(self):
        settings = {"paper_multi_strategy_enabled": True, "paper_multi_strategy_ids": [S2], "gdcrs_stocks": []}
        calls = []
        tracker = SimpleNamespace(
            multi_strategy_open_symbols=lambda *args: {"status": "available", "symbols": ["005930"]},
            multi_strategy_position=lambda _m, _s, strategy, _n: ({"status": "owned", "owned_qty": 10,
                "half_take_done": False, "generation_id": "a"*32} if strategy == S2 else {"status": "unsupported"}))
        session = SimpleNamespace(paper_automation_reconcile=SimpleNamespace(_tracker=tracker))
        report = {"mode": "paper", "exchange": "KRX", "snapshot_changed": False,
                  "holdings": [{"stk_cd": "005930", "crd_tp": "00", "crd_loan_dt": "",
                                "rmnd_qty": "10", "trde_able_qty": "10", "pur_amt": "1000"}]}
        class ExitProvider:
            def fetch(self, symbol, as_of=None):
                return {"symbol": symbol, "current_price": 96}
            def is_fresh(self, context, now):
                return True
        class EntryProvider:
            def fetch(self, *args, **kwargs):
                raise AssertionError("ranking must not be queried for an exit")
        monitor = MultiStrategyMonitor(settings_loader=lambda: settings, now=lambda: self.now,
            provider_factory=lambda _: EntryProvider(), exit_provider_factory=lambda _: ExitProvider(),
            account_fetcher=lambda _: report, chart_fetcher=lambda *a, **kw: (_ for _ in ()).throw(AssertionError("chart")),
            executor=lambda **kwargs: calls.append(kwargs) or [])
        with patch("kiwoom_autotrader.trading.multi_strategy.session_identity", return_value=("paper", "test")):
            monitor.tick(session, lambda _: None)
        self.assertEqual(len(calls), 1)
        self.assertEqual((calls[0]["symbol"], calls[0]["side"], calls[0]["reason"], calls[0]["quantity"]),
                         ("005930", "sell", "stop_loss_or_sma20", 10))
        self.assertIsNone(calls[0]["budget"])

    def test_s3_entry_requires_observed_20pct_crossing_after_baseline(self):
        settings = {"paper_multi_strategy_enabled": True, "paper_multi_strategy_ids": [S3],
                    "gdcrs_stocks": [{"code": "005930", "budget": 1000000}]}
        now = [self.now]
        tracker = SimpleNamespace(multi_strategy_open_symbols=lambda *args: {"status": "available", "symbols": []},
                                  multi_strategy_position=lambda *args: {"status": "flat"})
        session = SimpleNamespace(paper_automation_reconcile=SimpleNamespace(_tracker=tracker))
        class Provider:
            def fetch(self, symbol, as_of=None):
                return {"symbol": symbol, "change_pct": "19.9" if now[0] == initial else "20.1",
                        "current_price": 100, "upper_limit_price": 130}
            def is_fresh(self, context, moment): return True
        initial = self.now
        def chart(_session, symbol, count):
            return [MinuteBar(now[0].replace(second=0)-timedelta(minutes=21-i), 100, 100,
                              source_symbol=symbol, source_mode="paper", source_query_started_at=now[0],
                              source_response_sha256="a"*64, open_price=100, high_price=101, low_price=99)
                    for i in range(21)]
        calls = []
        monitor = MultiStrategyMonitor(settings_loader=lambda: settings, now=lambda: now[0],
            chart_fetcher=chart, provider_factory=lambda _: Provider(), exit_provider_factory=lambda _: object(),
            executor=lambda **kwargs: calls.append(kwargs), cooldown_check=lambda *args: True)
        with patch("kiwoom_autotrader.trading.multi_strategy.session_identity", return_value=("paper", "test")):
            monitor.tick(session, lambda _: None)
            now[0] += timedelta(minutes=1)
            monitor.tick(session, lambda _: None)
        self.assertEqual(len(calls), 1)
        self.assertEqual((calls[0]["strategy"], calls[0]["reason"], calls[0]["side"]),
                         (S3, "observed_20pct_crossing", "buy"))

    def test_no_new_strategy_entry_from_1519(self):
        settings = {"paper_multi_strategy_enabled": True, "paper_multi_strategy_ids": [S2],
                    "gdcrs_stocks": [{"code": "005930", "budget": 1000000}]}
        now = self.now.replace(hour=15, minute=19)
        tracker = SimpleNamespace(multi_strategy_open_symbols=lambda *args: {"status": "available", "symbols": []},
                                  multi_strategy_position=lambda *args: {"status": "flat"})
        session = SimpleNamespace(paper_automation_reconcile=SimpleNamespace(_tracker=tracker))
        monitor = MultiStrategyMonitor(settings_loader=lambda: settings, now=lambda: now,
            provider_factory=lambda _: object(), exit_provider_factory=lambda _: object(),
            chart_fetcher=lambda *a, **kw: self.fail("cutoff should precede new entry query"),
            executor=lambda **kwargs: self.fail("cutoff should block entry"))
        with patch("kiwoom_autotrader.trading.multi_strategy.session_identity", return_value=("paper", "test")):
            monitor.tick(session, lambda _: None)
        self.assertEqual(monitor.status()["last_decision"]["reason"], "entry_cutoff_reached")

    def test_missing_sma_is_visible_and_no_invented_exit(self):
        settings = {"paper_multi_strategy_enabled": True, "paper_multi_strategy_ids": [S2], "gdcrs_stocks": []}
        tracker = SimpleNamespace(multi_strategy_open_symbols=lambda *args: {"status": "available", "symbols": ["005930"]},
            multi_strategy_position=lambda _m, _s, strategy, _n: ({"status": "owned", "owned_qty": 10,
                "half_take_done": False, "generation_id": "a"*32} if strategy == S2 else {"status": "unsupported"}))
        session = SimpleNamespace(paper_automation_reconcile=SimpleNamespace(_tracker=tracker))
        report = {"mode": "paper", "exchange": "KRX", "snapshot_changed": False,
                  "holdings": [{"stk_cd": "005930", "crd_tp": "00", "crd_loan_dt": "",
                                "rmnd_qty": "10", "trde_able_qty": "10", "pur_amt": "1000"}]}
        class ExitProvider:
            def fetch(self, symbol, as_of=None): return {"symbol": symbol, "current_price": 100}
            def is_fresh(self, context, now): return True
        orders = []
        monitor = MultiStrategyMonitor(settings_loader=lambda: settings, now=lambda: self.now,
            provider_factory=lambda _: object(), exit_provider_factory=lambda _: ExitProvider(),
            account_fetcher=lambda _: report, chart_fetcher=lambda *a, **kw: [],
            executor=lambda **kwargs: orders.append(kwargs))
        with patch("kiwoom_autotrader.trading.multi_strategy.session_identity", return_value=("paper", "test")):
            monitor.tick(session, lambda _: None)
        self.assertEqual(orders, [])
        self.assertEqual(monitor.status()["symbol_errors"]["005930"], "sma20_unavailable")

    def test_all_strategies_cutoff_requires_actual_completed_1519_bar(self):
        settings = {"paper_multi_strategy_enabled": True, "paper_multi_strategy_ids": [S2, S3, S4], "gdcrs_stocks": []}
        now = self.now.replace(hour=15, minute=20, second=10)
        report = {"mode": "paper", "exchange": "KRX", "snapshot_changed": False,
                  "holdings": [{"stk_cd": "005930", "crd_tp": "00", "crd_loan_dt": "",
                                "rmnd_qty": "10", "trde_able_qty": "10", "pur_amt": "1000"}]}
        class ExitProvider:
            def fetch(self, symbol, as_of=None): return {"symbol": symbol, "current_price": 100}
            def is_fresh(self, context, moment): return True
        cutoff = MinuteBar(now.replace(hour=15, minute=19, second=0), 100,
                           source_symbol="005930", source_mode="paper", source_query_started_at=now,
                           source_response_sha256="a"*64)
        for owner in (S2, S3, S4):
            tracker = SimpleNamespace(multi_strategy_open_symbols=lambda *args: {"status": "available", "symbols": ["005930"]},
                multi_strategy_position=lambda _m, _s, strategy, _n: ({"status": "owned", "owned_qty": 10,
                    "half_take_done": False, "generation_id": "a"*32} if strategy == owner else {"status": "unsupported"}))
            session = SimpleNamespace(paper_automation_reconcile=SimpleNamespace(_tracker=tracker))
            for bars, expected in (([cutoff], True), ([], False)):
                orders, notifications = [], []
                monitor = MultiStrategyMonitor(settings_loader=lambda: settings, now=lambda: now,
                    provider_factory=lambda _: object(), exit_provider_factory=lambda _: ExitProvider(),
                    account_fetcher=lambda _: report, chart_fetcher=lambda *a, **kw: bars,
                    executor=lambda **kwargs: orders.append(kwargs) or [])
                with patch("kiwoom_autotrader.trading.multi_strategy.session_identity", return_value=("paper", "test")):
                    monitor.tick(session, notifications.append)
                self.assertEqual(bool(orders), expected)
                if expected:
                    self.assertEqual((orders[0]["reason"], orders[0]["quantity"]), ("regular_cutoff_exit", 10))
                else:
                    self.assertEqual(monitor.status()["symbol_errors"]["005930"], "cutoff_bar_unavailable")

    def test_gateway_uses_existing_automatic_order_context_for_both_sides(self):
        from kiwoom_autotrader.trading.order_context import _current
        tracker = SimpleNamespace(multi_strategy_position=lambda *a: None,
            multi_strategy_open_symbols=lambda *a: None, assert_multi_exit_dispatch=lambda *a: None)
        session = SimpleNamespace(mode="paper")
        calls = []
        def handler(command, _session, before_send=None):
            calls.append((command, _current.get().strategy))
            before_send()
            return []
        common = dict(session=session, tracker=tracker, strategy=S2, symbol="005930", reason="x",
                      budget=1000000, position={}, bars=[], market={}, baseline=EntryState(),
                      observed_at=self.now, before_send=lambda: None, run_id="r", source_hash="a"*64)
        with patch("kiwoom_autotrader.trading.multi_strategy_evidence.multi_strategy_evidence", return_value=None), \
             patch("kiwoom_autotrader.telegram.commands.orders.handle_buy", side_effect=handler), \
             patch("kiwoom_autotrader.telegram.commands.orders.handle_sell", side_effect=handler):
            execute_paper_strategy(**common, side="buy", quantity=None)
            execute_paper_strategy(**common, side="sell", quantity=5)
            self.assertEqual(calls, [("buy 005930 max 1000000", S2), ("sell 005930 5", S2)])
            common["session"] = SimpleNamespace(mode="real")
            execute_paper_strategy(**common, side="buy", quantity=None)
        self.assertEqual(len(calls), 2)
        self.assertIsNone(_current.get())

    def test_gateway_fails_closed_if_proof_or_before_send_is_missing(self):
        tracker = SimpleNamespace(multi_strategy_position=lambda *a: None,
            multi_strategy_open_symbols=lambda *a: None, assert_multi_exit_dispatch=lambda *a: None)
        common = dict(session=SimpleNamespace(mode="paper"), tracker=tracker, strategy=S3,
            symbol="005930", side="buy", reason="observed_20pct_crossing", quantity=None, budget=1000000,
            position={}, bars=[], market={}, baseline=EntryState(), observed_at=self.now,
            before_send=lambda: None, run_id="r", source_hash="a"*64)
        with patch("kiwoom_autotrader.trading.multi_strategy_evidence.multi_strategy_evidence", side_effect=ValueError), \
             patch("kiwoom_autotrader.telegram.commands.orders.handle_buy") as handler:
            result = execute_paper_strategy(**common)
            self.assertEqual(result.decision_reason, "strategy_evidence_or_context_unavailable")
            handler.assert_not_called()
        def stale(): raise ValueError("quote expired")
        common["before_send"] = stale
        with patch("kiwoom_autotrader.telegram.commands.orders.handle_buy") as handler:
            result = execute_paper_strategy(**common)
            self.assertEqual(result.decision_reason, "strategy_evidence_or_context_unavailable")
            handler.assert_not_called()

    def test_unsupported_open_position_warns_once_and_never_queries_or_orders(self):
        settings = {"paper_multi_strategy_enabled": True, "paper_multi_strategy_ids": [S2], "gdcrs_stocks": []}
        now = [self.now]
        tracker = SimpleNamespace(multi_strategy_open_symbols=lambda *args: {"status": "available", "symbols": ["005930"]},
                                  multi_strategy_position=lambda *args: {"status": "unsupported"})
        session = SimpleNamespace(paper_automation_reconcile=SimpleNamespace(_tracker=tracker))
        notifications = []
        monitor = MultiStrategyMonitor(settings_loader=lambda: settings, now=lambda: now[0],
            provider_factory=lambda _: object(), exit_provider_factory=lambda _: object(),
            executor=lambda **kwargs: self.fail("unknown ownership must not order"))
        with patch("kiwoom_autotrader.trading.multi_strategy.session_identity", return_value=("paper", "test")):
            monitor.tick(session, notifications.append)
            now[0] += timedelta(minutes=1)
            monitor.tick(session, notifications.append)
        self.assertEqual(len(notifications), 1)
        self.assertEqual(monitor.status()["symbol_errors"]["005930"], "ownership_unavailable")

    def test_gateway_real_evidence_reaches_existing_order_handler(self):
        from kiwoom_autotrader.trading.order_context import _current
        tracker = SimpleNamespace(multi_strategy_position=lambda *a: None,
            multi_strategy_open_symbols=lambda *a: None, assert_multi_exit_dispatch=lambda *a: None)
        market = {"symbol": "005930", "request_started_at": self.now.isoformat(),
                  "observed_at": self.now.isoformat(), "available_at": self.now.isoformat(),
                  "source": "ka10001_normalized_response", "source_sha256": "a"*64,
                  "source_market_timestamp": None, "source_freshness_verified": False,
                  "current_price": 100, "day_high": 110, "upper_limit_price": 130,
                  "change_pct": "20.1", "rising_volume_surge": None, "surge_evidence": None,
                  "theme_evidence": None, "dart_evidence": None}
        baseline = EntryState(self.bars[-1].start-timedelta(minutes=1), 99, False, Decimal("19.9"))
        observed = []
        def handler(command, session, before_send=None):
            observed.append(_current.get().evidence.as_dict())
            return []
        with patch("kiwoom_autotrader.telegram.commands.orders.handle_buy", side_effect=handler):
            result = execute_paper_strategy(session=SimpleNamespace(mode="paper"), tracker=tracker,
                strategy=S3, symbol="005930", side="buy", reason="observed_20pct_crossing",
                quantity=None, budget=1000000, position={"status": "flat"}, bars=self.bars,
                market=market, baseline=baseline, observed_at=self.now, before_send=lambda: None,
                run_id="test", source_hash="a"*64)
        self.assertEqual(result, [])
        self.assertEqual(len(observed), 1)
        self.assertEqual(observed[0]["multi_strategy_signal"]["reason"], "observed_20pct_crossing")
        self.assertIsNone(observed[0]["previous_difference"])
        self.assertEqual(len(observed[0]["multi_strategy_signal"]["ohlcv"]), 21)

    def test_price_only_risk_exit_has_real_evidence_without_candles(self):
        from kiwoom_autotrader.trading.order_context import _current
        tracker = SimpleNamespace(multi_strategy_position=lambda *a: None,
            multi_strategy_open_symbols=lambda *a: None, assert_multi_exit_dispatch=lambda *a: None)
        market = {"symbol": "005930", "request_started_at": self.now.isoformat(),
                  "observed_at": self.now.isoformat(), "available_at": self.now.isoformat(),
                  "source": "ka10001_normalized_response", "source_sha256": "a"*64,
                  "source_market_timestamp": None, "source_freshness_verified": False,
                  "current_price": 96, "day_high": None, "upper_limit_price": None,
                  "change_pct": None, "rising_volume_surge": None, "surge_evidence": None,
                  "theme_evidence": None, "dart_evidence": None}
        position = {"generation_id": "a"*32, "owned_qty": 10, "basis_krw": 1000, "half_take_done": False}
        observed = []
        def handler(command, session, before_send=None):
            observed.append((command, _current.get().evidence.as_dict()))
            return []
        with patch("kiwoom_autotrader.telegram.commands.orders.handle_sell", side_effect=handler):
            result = execute_paper_strategy(session=SimpleNamespace(mode="paper"), tracker=tracker,
                strategy=S2, symbol="005930", side="sell", reason="stop_loss_or_sma20",
                quantity=10, budget=None, position=position, bars=[], market=market, baseline=None,
                observed_at=self.now, before_send=lambda: None, run_id="test", source_hash="a"*64)
        self.assertEqual(result, [])
        self.assertEqual(observed[0][0], "sell 005930 10")
        evidence = observed[0][1]
        self.assertEqual(evidence["config"]["multi_exit_kind"], "full")
        self.assertEqual(evidence["multi_strategy_signal"]["sma20_status"], "unavailable")

    def test_sma_exit_evidence_accepts_real_closes_without_ohlcv(self):
        from kiwoom_autotrader.trading.order_context import _current
        tracker = SimpleNamespace(multi_strategy_position=lambda *a: None,
            multi_strategy_open_symbols=lambda *a: None, assert_multi_exit_dispatch=lambda *a: None)
        bars = [MinuteBar(self.now.replace(second=0)-timedelta(minutes=20-i), 101,
                          source_symbol="005930", source_mode="paper", source_query_started_at=self.now,
                          source_response_sha256="a"*64) for i in range(20)]
        market = {"symbol": "005930", "request_started_at": self.now.isoformat(),
                  "observed_at": self.now.isoformat(), "available_at": self.now.isoformat(),
                  "source": "ka10001_normalized_response", "source_sha256": "a"*64,
                  "source_market_timestamp": None, "source_freshness_verified": False,
                  "current_price": 100}
        position = {"generation_id": "a"*32, "owned_qty": 10, "basis_krw": 1000, "half_take_done": False}
        seen = []
        def handler(command, session, before_send=None):
            seen.append(_current.get().evidence.as_dict())
            return []
        with patch("kiwoom_autotrader.telegram.commands.orders.handle_sell", side_effect=handler):
            execute_paper_strategy(session=SimpleNamespace(mode="paper"), tracker=tracker,
                strategy=S2, symbol="005930", side="sell", reason="stop_loss_or_sma20", quantity=10,
                budget=None, position=position, bars=bars, market=market, baseline=None,
                observed_at=self.now, before_send=lambda: None, run_id="test", source_hash="a"*64)
        self.assertEqual(seen[0]["multi_strategy_signal"]["sma20_status"], "observed")
        self.assertEqual(seen[0]["multi_strategy_signal"]["sma20"], "101")

    def test_cutoff_gateway_records_real_1519_bar(self):
        from kiwoom_autotrader.trading.order_context import _current
        now = self.now.replace(hour=15, minute=20, second=10)
        tracker = SimpleNamespace(multi_strategy_position=lambda *a: None,
            multi_strategy_open_symbols=lambda *a: None, assert_multi_exit_dispatch=lambda *a: None)
        bar = MinuteBar(now.replace(hour=15, minute=19, second=0), 100,
                        source_symbol="005930", source_mode="paper", source_query_started_at=now,
                        source_response_sha256="a"*64)
        market = {"symbol": "005930", "request_started_at": now.isoformat(),
                  "observed_at": now.isoformat(), "available_at": now.isoformat(),
                  "source": "ka10001_normalized_response", "source_sha256": "a"*64,
                  "source_market_timestamp": None, "source_freshness_verified": False, "current_price": 100}
        position = {"generation_id": "a"*32, "owned_qty": 10, "basis_krw": 1000, "half_take_done": False}
        seen = []
        def handler(command, session, before_send=None):
            seen.append(_current.get().evidence.as_dict())
            return []
        with patch("kiwoom_autotrader.telegram.commands.orders.handle_sell", side_effect=handler):
            execute_paper_strategy(session=SimpleNamespace(mode="paper"), tracker=tracker,
                strategy=S3, symbol="005930", side="sell", reason="regular_cutoff_exit", quantity=10,
                budget=None, position=position, bars=[bar], market=market, baseline=None,
                observed_at=now, before_send=lambda: None, run_id="test", source_hash="a"*64)
        self.assertEqual(seen[0]["multi_strategy_signal"]["reason"], "regular_cutoff_exit")
        self.assertEqual(seen[0]["config"]["multi_exit_kind"], "full")

    def test_reconciliation_delay_keeps_five_second_gate(self):
        from kiwoom_autotrader.telegram.commands.orders import OrderReplies
        tracker = SimpleNamespace(multi_strategy_position=lambda *a: None,
            multi_strategy_open_symbols=lambda *a: None, assert_multi_exit_dispatch=lambda *a: None)
        for delay, expected in ((4.9, True), (5.1, False)):
            now = [self.now]
            sent = []
            def guard():
                if not timedelta(0) <= now[0] - self.now <= timedelta(seconds=5):
                    raise ValueError("quote expired")
            def handler(command, session, before_send=None):
                before_send()  # initial budget check
                now[0] += timedelta(seconds=delay)  # account and broker reconciliation
                try: before_send()  # immediately before simulated transport
                except ValueError: return OrderReplies([], submitted=False, decision_reason="buy_guard_changed")
                sent.append(command)
                return OrderReplies([], submitted=True)
            with patch("kiwoom_autotrader.trading.multi_strategy_evidence.multi_strategy_evidence", return_value=None), \
                 patch("kiwoom_autotrader.telegram.commands.orders.handle_buy", side_effect=handler):
                result = execute_paper_strategy(session=SimpleNamespace(mode="paper"), tracker=tracker,
                    strategy=S2, symbol="005930", side="buy", reason="shallow_pullback_high_breakout",
                    quantity=None, budget=1000000, position={}, bars=[], market={}, baseline=None,
                    observed_at=self.now, before_send=guard, run_id="test", source_hash="a"*64)
            self.assertEqual(bool(sent), expected)
            self.assertEqual(result.submitted, expected)

    def test_shared_real_pacer_has_a_feasible_uncontended_order_window(self):
        """Synthetic zero-network pacing only; not a latency guarantee."""
        from kiwoom_autotrader.utils.http_queue import SharedRateLimit
        with tempfile.TemporaryDirectory() as tmp:
            elapsed = [100.0]
            class Wait:
                def wait(self, seconds): elapsed[0] += seconds
            limiter = SharedRateLimit(Path(tmp)/"rate.json", clock=lambda: elapsed[0])
            limiter._wait = Wait()
            sent = []
            def reserve(tr, before_send=None):
                request = urllib.request.Request("https://mockapi.kiwoom.com/api/test", data=b"{}",
                                                 headers={"Api-id": tr}, method="POST")
                lease = limiter.acquire_for_request(request, before_send=before_send)
                sent.append((tr, elapsed[0]))
                elapsed[0] += .05  # explicitly hypothetical transport time
                lease.release()
            reserve("ka10001")  # context quote and its local observed time
            observed = elapsed[0]
            for tr in ("kt00001", "kt00018", "ka10001", "kt00009", "kt00001", "kt00018", "ka10075"):
                reserve(tr)
            def fresh():
                if elapsed[0] - observed > 5:
                    raise ValueError("quote expired")
            reserve("kt10000", before_send=fresh)
            self.assertLessEqual(sent[-1][1] - observed, 5)
            self.assertGreaterEqual(sent[3][1] - sent[0][1], 1.05)


if __name__ == "__main__":
    unittest.main()
