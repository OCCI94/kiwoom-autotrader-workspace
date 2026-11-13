from copy import deepcopy
from datetime import datetime
from unittest.mock import Mock
from zoneinfo import ZoneInfo
import unittest

from kiwoom_autotrader.config.settings import default_settings, _validate, effective_cross_guards, SettingsError
from kiwoom_autotrader.trading.golden_cross import GoldenCrossMonitor
from kiwoom_autotrader.api.session import BrokerSession, IssuedToken


class PaperCrossGuardTests(unittest.TestCase):
    def test_paper_override_preserves_real_and_legacy_behavior(self):
        settings = default_settings()
        settings.update(gdcrs_min_cross_bps=3, gdcrs_reentry_cooldown_minutes=10)
        before = deepcopy(settings)
        self.assertEqual(effective_cross_guards(settings, 'paper'), (3,10))
        settings['paper_gdcrs_guards'] = {'gdcrs_min_cross_bps':10, 'gdcrs_reentry_cooldown_minutes':20}
        self.assertEqual(effective_cross_guards(_validate(settings), 'paper'), (10,20))
        self.assertEqual(effective_cross_guards(settings, 'real'), (3,10))
        self.assertEqual({k:v for k,v in settings.items() if k!='paper_gdcrs_guards'},
                         {k:v for k,v in before.items() if k!='paper_gdcrs_guards'})

    def test_malformed_or_partial_override_fails_closed(self):
        for value in (None, [], {'gdcrs_min_cross_bps':10},
                      {'gdcrs_min_cross_bps':True, 'gdcrs_reentry_cooldown_minutes':20},
                      {'gdcrs_min_cross_bps':10, 'gdcrs_reentry_cooldown_minutes':-1},
                      {'gdcrs_min_cross_bps':10, 'gdcrs_reentry_cooldown_minutes':20, 'token':'private'}):
            with self.subTest(value=value), self.assertRaises(SettingsError):
                _validate({**default_settings(), 'paper_gdcrs_guards':value})

    def test_monitor_uses_session_scoped_override(self):
        settings = default_settings()
        settings.update(gdcrs_enabled=True, gdcrs_stocks=[{'code':'005930','budget':1000000}],
                        paper_gdcrs_guards={'gdcrs_min_cross_bps':10,'gdcrs_reentry_cooldown_minutes':20})
        monitor = GoldenCrossMonitor(settings_loader=lambda:settings, markers=Mock(), chart_fetcher=Mock(), buy_handler=Mock())
        now = datetime(2026,10,30,9,15,tzinfo=ZoneInfo('Asia/Seoul'))
        for mode, expected in [('paper',(10,20)), ('real',(0,0))]:
            session=BrokerSession(now=lambda:now)
            session.mode=mode
            session._issued=IssuedToken('offline-test-token','20300101000000')
            context = monitor._context(session, now)
            self.assertEqual((context.min_cross_bps,context.reentry_cooldown_minutes), expected)
        monitor.buy_handler.assert_not_called()
        self.assertEqual(settings['paper_automation_buy_cap'],1000000)
        self.assertEqual(settings['paper_automation_symbol_cap'],1000000)

    def test_old_resource_is_accepted_without_mutating_it(self):
        old = default_settings();old.pop('paper_gdcrs_guards')
        self.assertEqual(_validate(old)['paper_gdcrs_guards'],{})
        self.assertNotIn('paper_gdcrs_guards',old)


if __name__=='__main__':unittest.main()
