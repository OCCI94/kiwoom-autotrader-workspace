"""Offline coverage for a missing latest candle and safe later reconciliation."""
from datetime import datetime, timedelta
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from kiwoom_autotrader.api.minute_chart import _bars
from kiwoom_autotrader.api.session import BrokerSession, IssuedToken, KST
from kiwoom_autotrader.trading.cross_monitor import CrossMonitor
from kiwoom_autotrader.trading.golden_cross import GoldenCrossMonitor


class LatestNoTradeGapTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.now = datetime(2026, 10, 27, 10, 3, 5, tzinfo=KST)
        self.session = BrokerSession(now=lambda: self.now)
        self.session.mode = "paper"
        self.session._issued = IssuedToken("offline-test-token", "20300101000000")
        self.settings = {"ddcrs_enabled": True, "gdcrs_enabled": True,
            "gdcrs_short_period": 1, "gdcrs_long_period": 3,
            "gdcrs_stocks": [{"code": "005930", "budget": 100000}],
            "market_start_time": "09:00", "market_end_time": "15:30"}
        self.report = {"mode": "paper", "exchange": "KRX", "snapshot_changed": False,
            "holdings": [{"stk_cd": "A005930", "rmnd_qty": "3", "trde_able_qty": "3",
                          "crd_tp": "00", "crd_loan_dt": None}]}
        self.chart = Mock()
        self.notify = Mock()
        self.buy = Mock(return_value=["test"])
        self.sell = Mock(return_value=["test"])

    def chart_rows(self, closes, *, last=None, bad_proof=False, revised=False):
        # 10:03 has no source row. 10:04 provides the required right anchor.
        entries = [(i, price, 1, i + 1) for i, price in enumerate(closes)]
        if revised:
            entries[1] = (1, entries[1][1] + 1, 1, 2)
        if last is not None:
            entries.append((4, last, 1, 5 if bad_proof else 4))
        payload = {"return_code": 0, "stk_cd": "005930", "stk_min_pole_chart_qry": [
            {"cntr_tm": f"2026102710{minute:02d}00", "cur_prc": str(price),
             "trde_qty": str(volume), "acc_trde_qty": str(acc),
             "upd_stkpc_tp": "", "upd_rt": ""}
            for minute, price, volume, acc in reversed(entries)]}
        return _bars(payload, "005930", mode="paper", batch="same-page", page=0,
            response_sha256="a" * 64, query_started_at=self.now)

    def make(self, side):
        common = dict(settings_loader=lambda: dict(self.settings), now=lambda: self.now,
                      state_path=Path(self.temp.name) / (side + ".sqlite"), chart_fetcher=self.chart)
        if side == "buy":
            return GoldenCrossMonitor(**common, buy_handler=self.buy)
        return CrossMonitor(ownership_reader=lambda *_: 9, **common, account_fetcher=lambda _: self.report, sell_handler=self.sell)

    def scan(self, monitor, side):
        monitor.tick(self.session, self.notify)
        if side == "sell":
            self.now += timedelta(seconds=2)
            monitor.tick(self.session, self.notify)

    def run_gap(self, side, closes, *, last, bad_proof=False, revised=False):
        monitor = self.make(side)
        self.chart.return_value = self.chart_rows(closes)
        self.scan(monitor, side)
        baseline = monitor._baseline["005930"]
        self.now = self.now.replace(minute=4, second=5)
        self.chart.return_value = self.chart_rows(closes)
        self.scan(monitor, side)
        self.assertEqual(monitor._baseline["005930"], baseline)
        self.now = self.now.replace(minute=5, second=5)
        self.chart.return_value = self.chart_rows(closes, last=last, bad_proof=bad_proof, revised=revised)
        self.scan(monitor, side)
        return monitor

    def test_fresh_cross_on_resumption_only_when_gap_is_proved(self):
        for side, latest in (("buy", 110), ("sell", 90)):
            with self.subTest(side=side):
                self.now = self.now.replace(minute=3, second=5)
                self.run_gap(side, [100, 100, 100], last=latest)
                (self.buy if side == "buy" else self.sell).assert_called_once()
                self.buy.reset_mock(); self.sell.reset_mock()

    def test_cross_on_inferred_earlier_candle_is_never_replayed(self):
        for side, closes, latest in (("buy", [3, 1, 2], 3), ("sell", [1, 3, 2], 1)):
            with self.subTest(side=side):
                self.now = self.now.replace(minute=3, second=5)
                self.run_gap(side, closes, last=latest)
                self.buy.assert_not_called(); self.sell.assert_not_called()

    def test_missing_volume_proof_or_revised_overlap_cannot_trade(self):
        for side in ("buy", "sell"):
            for broken in ("proof", "revision"):
                with self.subTest(side=side, broken=broken):
                    self.now = self.now.replace(minute=3, second=5)
                    self.run_gap(side, [100, 100, 100], last=110 if side == "buy" else 90,
                                 bad_proof=broken == "proof", revised=broken == "revision")
                    self.buy.assert_not_called(); self.sell.assert_not_called()

    def test_unproved_latest_gap_expires(self):
        for side in ("buy", "sell"):
            with self.subTest(side=side):
                self.now = self.now.replace(minute=3, second=5)
                monitor = self.make(side)
                for minute in (3, 4, 5, 6, 7):
                    self.now = self.now.replace(minute=minute, second=5)
                    self.chart.return_value = self.chart_rows([100, 100, 100])
                    self.scan(monitor, side)
                self.assertNotIn("005930", monitor._baseline)
                self.buy.assert_not_called(); self.sell.assert_not_called()


if __name__ == "__main__":
    unittest.main()
