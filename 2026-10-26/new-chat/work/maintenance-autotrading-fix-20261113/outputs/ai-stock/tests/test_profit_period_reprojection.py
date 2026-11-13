"""A newly observed period report must immediately revise the profit view."""
from collections import deque
from datetime import timedelta
from decimal import Decimal
import sqlite3
import threading
import unittest
from unittest.mock import Mock

from kiwoom_autotrader.api.executions import RealizedProfit
from kiwoom_autotrader.api.minute_chart import session_identity
from kiwoom_autotrader.api.session import BrokerSession, IssuedToken
from tests import test_execution_accounting as accounting_fixture


class ProfitPeriodReprojectionTests(unittest.TestCase):
    def setUp(self):
        self.fx = accounting_fixture.ExecutionAccountingTests(methodName="runTest")
        self.fx.setUp()
        self.addCleanup(self.fx.doCleanups)
        self.tracker = self.fx.tracker
        self.session = BrokerSession(now=lambda: self.fx.now)
        self.session.mode = "paper"
        self.session._issued = IssuedToken("offline-test-token", "20300101000000")
        self.tracker._identity = session_identity(self.session)
        self.fx.unit(1, 49, 49, 20392, 999208, 100)
        # The fixture's stream notification is not work for this test cycle.
        self.tracker._sell_refresh_pending.clear()
        self.fx.now += timedelta(seconds=1)
        self.rows = (RealizedProfit("097520", 49, Decimal(20392), Decimal(20150),
                    Decimal(100), Decimal(50), reported_net=Decimal(11708)),)
        self.tracker.profit_fetcher = Mock(return_value=self.rows)
        self.tracker._tasks = deque([("profit", "20261026", "097520"),
                                     ("period_profit", "20261026", "097520")])
        self.tracker._next_at = None
        self.tracker._cycle_started_at = self.fx.now

    def net(self):
        with sqlite3.connect(self.tracker.path) as db:
            return db.execute("SELECT net FROM profits WHERE symbol='097520'").fetchone()[0]

    def period(self, fee, net, *, response_cont="N", next_key=None, return_code=0, request_cont="N"):
        def fetch(session, symbol, day, *, observer):
            row = {"stk_cd": symbol, "dt": day, "crd_tp": "현금잔고", "cntr_qty": "49",
                   "buy_uv": "20150", "cntr_pric": "20392", "tdy_trde_cmsn": str(fee),
                   "tdy_trde_tax": "50", "tdy_sel_pl": str(net)}
            observer({"schema": "kiwoom-broker-page-v1", "source_api": "ka10073",
                "source_kind": "broker_observation", "mode": "paper", "day": day, "query_id": "d"*32,
                "request_started_at": self.fx.now.isoformat(), "observed_at": self.fx.now.isoformat(), "page": 1,
                "request_body": {"strt_dt": day, "end_dt": day, "stk_cd": symbol},
                "response_body": {"return_code": return_code, "dt_stk_rlzt_pl": [row]},
                "continuation": {"request_cont_yn": request_cont, "response_cont_yn": response_cont,
                                 "next_key_present": response_cont == "Y" if next_key is None else next_key},
                "status": "success", "http_status": 200})
        return fetch

    def test_conflicting_later_period_does_not_leave_previous_net_confirmed(self):
        self.tracker.period_profit_fetcher = self.period(101, 11707)
        self.tracker.tick(self.session, Mock())
        self.assertIsNone(self.net())  # A previous matching period cannot establish this cycle.
        self.tracker.tick(self.session, Mock())
        self.assertIsNone(self.net())
        with sqlite3.connect(self.tracker.path) as db:
            reason = db.execute("SELECT reason FROM profits").fetchone()[0]
        self.assertIn("불일치", reason)

    def test_matching_fresh_period_recomputes_without_second_cost_fetch(self):
        self.tracker.period_profit_fetcher = self.period(100, 11708)
        self.tracker.tick(self.session, Mock())
        self.assertIsNone(self.net())
        self.tracker.tick(self.session, Mock())
        self.assertEqual(self.net(), "11708")
        self.tracker.profit_fetcher.assert_called_once()

    def test_incomplete_or_missing_period_never_reuses_old_projection(self):
        self.tracker.period_profit_fetcher = self.period(100, 11708, response_cont="Y")
        self.tracker.tick(self.session, Mock())
        self.tracker.tick(self.session, Mock())
        self.assertIsNone(self.net())

    def test_failed_period_query_keeps_profit_unconfirmed(self):
        self.tracker.period_profit_fetcher = Mock(side_effect=OSError("offline failure"))
        self.tracker.tick(self.session, Mock())
        self.tracker.tick(self.session, Mock())
        self.assertIsNone(self.net())

    def test_missing_period_observation_never_confirms_cached_data(self):
        self.tracker.period_profit_fetcher = Mock()
        self.tracker.tick(self.session, Mock())
        self.tracker.tick(self.session, Mock())
        self.assertIsNone(self.net())

    def test_new_sell_during_period_query_requires_execution_refresh(self):
        normal_period = self.period(100, 11708)
        def fetch(session, symbol, day, *, observer):
            normal_period(session, symbol, day, observer=observer)
            self.fx.unit(2, 1, 49, 20392, 999208, 100)
        self.tracker.period_profit_fetcher = fetch
        self.tracker.tick(self.session, Mock())
        self.tracker.tick(self.session, Mock())
        self.assertIsNone(self.net())
        self.assertIn(("paper", "20261026", "097520"), self.tracker._sell_refresh_pending)

    def test_text_zero_is_success_but_bool_is_not(self):
        from kiwoom_autotrader.trading.execution_log import _successful_return_code
        self.assertFalse(_successful_return_code(False))
        self.tracker.period_profit_fetcher = self.period(100, 11708, return_code="0")
        self.tracker.tick(self.session, Mock())
        self.tracker.tick(self.session, Mock())
        self.assertEqual(self.net(), "11708")

    def test_terminal_next_key_or_wrong_continuation_fails_closed(self):
        from kiwoom_autotrader.trading.execution_log import _complete_observation_pages
        single = {"query_id": "a"*32, "page": 1, "status": "success", "response_body": {"return_code": "0"},
                  "continuation": {"request_cont_yn": "N", "response_cont_yn": "N", "next_key_present": True}}
        self.assertFalse(_complete_observation_pages([single]))
        self.tracker.period_profit_fetcher = self.period(100, 11708, next_key=True)
        self.tracker.tick(self.session, Mock())
        self.tracker.tick(self.session, Mock())
        self.assertIsNone(self.net())
        page1 = {**single, "continuation": {"request_cont_yn": "N", "response_cont_yn": "Y", "next_key_present": True}}
        page2 = {**single, "page": 2, "continuation": {"request_cont_yn": "N", "response_cont_yn": "N", "next_key_present": False}}
        self.assertFalse(_complete_observation_pages([page1, page2]))
        page2["continuation"]["request_cont_yn"] = "Y"
        self.assertTrue(_complete_observation_pages([page1, page2]))

    def test_sell_after_pending_check_before_profit_commit_is_not_confirmed(self):
        # Interpose exactly after tick checks pending and before _profit begins.
        normal = self.tracker._profit
        def raced(mode, day, symbol, rows, **kwargs):
            if kwargs.get("sell_watermark") is not None:
                self.fx.unit(2, 1, 49, 20392, 999208, 100)
            return normal(mode, day, symbol, rows, **kwargs)
        self.tracker._profit = raced
        self.tracker.period_profit_fetcher = self.period(100, 11708)
        self.tracker.tick(self.session, Mock())
        self.tracker.tick(self.session, Mock())
        self.assertIsNone(self.net())

    def test_sell_after_projection_commit_invalidates_without_waiting_for_tick(self):
        self.tracker.period_profit_fetcher = self.period(100, 11708)
        self.tracker.tick(self.session, Mock())
        self.tracker.tick(self.session, Mock())
        self.assertEqual(self.net(), "11708")
        self.fx.unit(2, 1, 49, 20392, 999208, 100)
        self.assertIsNone(self.net())

    def test_concurrent_sell_during_projection_cannot_leave_confirmed_net(self):
        normal = self.tracker._latest_sell_observation_id
        started, done = threading.Event(), threading.Event()
        thread = None
        calls = 0
        def latest(db, mode, day, symbol):
            nonlocal calls, thread
            calls += 1
            if calls == 2:
                def record():
                    started.set()
                    self.fx.unit(2, 1, 49, 20392, 999208, 100)
                    done.set()
                thread = threading.Thread(target=record)
                thread.start()
                self.assertTrue(started.wait(2))
                self.assertFalse(done.wait(.05))  # projection holds writer transaction
            return normal(db, mode, day, symbol)
        self.tracker._latest_sell_observation_id = latest
        self.tracker.period_profit_fetcher = self.period(100, 11708)
        self.tracker.tick(self.session, Mock())
        self.tracker.tick(self.session, Mock())
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertTrue(done.is_set())
        self.assertIsNone(self.net())

    def test_sell_during_calculation_does_not_wait_for_writer_lock(self):
        normal = self.tracker._profit_input_fingerprint
        done = threading.Event()
        thread = None
        calls = 0
        def fingerprint(db, mode, day, symbol):
            nonlocal calls, thread
            result = normal(db, mode, day, symbol)
            calls += 1
            # First profit call is the provisional ka10072 stage. The third
            # fingerprint is the beginning of post-period recalculation.
            if calls == 3:
                def record():
                    self.fx.unit(2, 1, 49, 20392, 999208, 100)
                    done.set()
                thread = threading.Thread(target=record)
                thread.start()
                self.assertTrue(done.wait(3), "profit calculation blocked the 00 writer")
            return result
        self.tracker._profit_input_fingerprint = fingerprint
        self.tracker.period_profit_fetcher = self.period(100, 11708)
        self.tracker.tick(self.session, Mock())
        self.tracker.tick(self.session, Mock())
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertIsNone(self.net())


if __name__ == "__main__":
    unittest.main()
