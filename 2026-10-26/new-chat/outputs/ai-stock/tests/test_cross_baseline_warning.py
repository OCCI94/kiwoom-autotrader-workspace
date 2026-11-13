"""Unknown pre-observation history is warned about, never sold or replayed."""
from datetime import datetime, timedelta
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from kiwoom_autotrader.api.session import BrokerSession, IssuedToken, KST
from kiwoom_autotrader.trading.cross_monitor import CrossMonitor
from tests.test_cross_monitor import bars, report


class BaselineWarnings(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "markers.sqlite"
        self.now = datetime(2026, 10, 26, 10, 3, 5, tzinfo=KST)
        self.session = BrokerSession(now=lambda: self.now)
        self.session.mode = "paper"
        self.session._issued = IssuedToken("test-only", "20300101000000")
        self.settings = {"ddcrs_enabled": True, "gdcrs_short_period": 1, "gdcrs_long_period": 3,
                         "market_start_time": "09:00", "market_end_time": "15:30"}
        self.chart = Mock(return_value=bars([3, 2, 1]))
        self.sell, self.notify = Mock(), Mock()
        self.account = Mock(return_value=report())
        self.monitor = self.create_monitor()

    def create_monitor(self):
        return CrossMonitor(ownership_reader=lambda *_: 9, settings_loader=lambda: dict(self.settings), now=lambda: self.now,
                            state_path=self.path, account_fetcher=self.account,
                            chart_fetcher=self.chart, sell_handler=self.sell)

    def scan(self):
        self.monitor.tick(self.session, self.notify)
        self.now += timedelta(seconds=2)
        self.monitor.tick(self.session, self.notify)

    def test_initial_negative_warning_durable_distinct_and_no_sell(self):
        with patch('builtins.print') as terminal:
            self.scan()
        self.sell.assert_not_called()
        self.notify.assert_called_once()
        self.assertIn('과거 교차 발생 여부와 시각은 확인되지 않아', self.notify.call_args.args[0])
        terminal.assert_called_once()
        status = self.monitor.status()
        self.assertEqual(status['missed_signals'], [])
        warning = status['baseline_warnings'][0]
        self.assertEqual(warning['reason'], 'initial_or_resumed')
        self.assertEqual(warning['difference_numerator'], -3)
        self.assertTrue(warning['durable'])
        restarted = self.create_monitor()
        self.assertEqual(restarted.status()['baseline_warnings'], [warning])
        self.sell.assert_not_called()

    def test_reset_while_negative_deduplicates_rearms_after_positive(self):
        self.scan()
        self.assertEqual(self.notify.call_count, 1)
        self.monitor.reset()
        self.now += timedelta(minutes=1)
        self.chart.return_value = bars([3, 2, 1], minute=1)
        self.scan()
        self.assertEqual(self.notify.call_count, 1)
        self.now += timedelta(minutes=1)
        self.chart.return_value = bars([1, 2, 3], minute=2)
        self.scan()
        self.monitor.reset()
        self.now += timedelta(minutes=1)
        self.chart.return_value = bars([3, 2, 1], minute=3)
        self.scan()
        self.assertEqual(self.notify.call_count, 2)
        self.sell.assert_not_called()

    def test_discontinuous_negative_warns_without_triggering_order(self):
        self.chart.return_value = bars([1, 2, 3])
        self.scan()
        self.now += timedelta(minutes=2)
        self.chart.return_value = bars([3, 2, 1], minute=2)
        self.scan()
        self.sell.assert_not_called()
        self.assertEqual(self.monitor.status()['baseline_warnings'][0]['reason'], 'observation_discontinuity')

    def test_untrusted_holdings_never_create_warning_or_order(self):
        self.account.return_value = report(mode='real')
        self.scan()
        self.assertEqual(self.monitor.status()['baseline_warnings'], [])
        self.sell.assert_not_called()

    def test_notification_and_persistence_failures_do_not_trade(self):
        self.monitor.markers.path = Path(self.temp.name)
        self.notify.side_effect = OSError('offline')
        self.scan()
        self.sell.assert_not_called()
        self.assertFalse(self.monitor.status()['baseline_warnings'][0]['durable'])


if __name__ == '__main__':
    unittest.main()
