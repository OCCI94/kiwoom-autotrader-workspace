"""One unavailable chart must not erase other symbols' actionable history."""
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import Mock

from kiwoom_autotrader.api.minute_chart import MinuteBar
from kiwoom_autotrader.api.session import BrokerSession, IssuedToken, KST
from kiwoom_autotrader.trading.cross_monitor import CrossMonitor, MinuteWindowError, closed_window
from kiwoom_autotrader.trading.golden_cross import GoldenCrossMonitor


class SymbolDataHealthTests(TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.now = datetime(2026, 10, 27, 10, 3, 5, tzinfo=KST)
        self.session = BrokerSession(now=lambda: self.now)
        self.session.mode = 'paper'
        self.session._issued = IssuedToken('offline-test-token', '20300101000000')
        self.settings = {'gdcrs_enabled': True, 'ddcrs_enabled': True,
            'gdcrs_short_period': 1, 'gdcrs_long_period': 3,
            'market_start_time': '09:00', 'market_end_time': '15:30',
            'gdcrs_stocks': [{'code': c, 'budget': 1000000} for c in ('000660','005930')]}
        self.report = {'mode': 'paper', 'exchange': 'KRX', 'snapshot_changed': False,
            'holdings': [{'stk_cd': c, 'rmnd_qty': '3', 'trde_able_qty': '3',
                          'crd_tp': '00', 'crd_loan_dt': None} for c in ('000660','005930')]}
        self.action, self.notify = Mock(return_value=['accepted']), Mock()
        self.chart = Mock()

    def bars(self, values):
        return tuple(MinuteBar(self.now.replace(minute=i, second=0), p) for i,p in enumerate(values))

    def tick(self, monitor, n=1):
        for _ in range(n):
            monitor.tick(self.session, self.notify)
            self.now += timedelta(seconds=2)

    def test_death_cross_other_symbol_survives_missing_chart_and_can_sell(self):
        monitor = CrossMonitor(ownership_reader=lambda *_: 9, settings_loader=lambda: self.settings, now=lambda: self.now,
            state_path=Path(self.tmp.name)/'dd.sqlite3', account_fetcher=lambda _: self.report,
            chart_fetcher=self.chart, sell_handler=self.action)
        self.chart.return_value = self.bars([1,2,3])
        self.tick(monitor, 3)
        self.assertEqual(set(monitor._baseline), {'000660','005930'})
        self.now = self.now.replace(minute=4, second=5)
        good = self.bars([1,2,3,1])
        self.chart.side_effect = lambda _, code, **kw: (good[0],good[2],good[3]) if code=='000660' else good
        self.tick(monitor, 3)
        self.action.assert_called_once()
        self.assertEqual(self.action.call_args.args[0], 'sell 005930 3')
        state = monitor.status()
        self.assertTrue(state['enabled'])
        self.assertIn('000660', state['symbol_errors'])
        self.assertIn('10:01:00', state['symbol_errors']['000660']['details']['missing_minutes'][0])
        self.assertIn('000660', state['error'])
        # Status callers cannot mutate the monitor's diagnostic state.
        state['symbol_errors'].clear()
        self.assertIn('000660', monitor.status()['symbol_errors'])

    def test_golden_success_does_not_hide_another_symbols_gap(self):
        monitor = GoldenCrossMonitor( settings_loader=lambda: self.settings, now=lambda: self.now,
            state_path=Path(self.tmp.name)/'gd.sqlite3', chart_fetcher=self.chart, buy_handler=self.action)
        good = self.bars([3,2,1])
        self.chart.side_effect = lambda _, code, **kw: good[1:] if code=='000660' else good
        self.tick(monitor, 2)
        self.action.assert_not_called()
        self.assertIn('000660', monitor.status()['symbol_errors'])
        self.assertIn('000660', monitor.status()['error'])
        self.assertIn('005930', monitor.status()['observed_closed_bars'])
        monitor.reset()
        self.assertEqual(monitor.status()['symbol_errors'], {})

    def test_gap_diagnostic_never_interpolates(self):
        self.now = self.now.replace(minute=4)
        good = self.bars([3,2,1,4])
        with self.assertRaises(MinuteWindowError) as raised:
            closed_window((good[0],good[2],good[3]), self.now, 3)
        self.assertFalse(raised.exception.details['interpolated'])
        self.assertEqual(raised.exception.details['missing_minutes'], ['2026-10-27T10:01:00+09:00'])
