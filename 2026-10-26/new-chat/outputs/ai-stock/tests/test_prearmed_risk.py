import json
from datetime import datetime, timedelta
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from kiwoom_autotrader.trading.prearmed_risk import BackgroundS5RiskCache, read_index
from kiwoom_autotrader.config.settings import effective_fast_decision, SettingsError

NOW=datetime.fromisoformat('2026-11-02T09:35:00+09:00')

class RiskTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'risk.json';self.clock=[10.]
        self.data={'schema':'toss-hybrid-risk-evidence-v1','generated_at':NOW.isoformat(),
            'expires_at':(NOW+timedelta(minutes=15)).isoformat(),
            'rows':[{'symbol':'042510','reviewed_at':NOW.isoformat(),
                'coverage_complete':True,'unresolved_major_risk':False,'source_sha256':'a'*64}]}
        self.write()
        self.cache=BackgroundS5RiskCache(self.path,now=lambda:NOW,clock=lambda:self.clock[0],autostart=False)
        self.addCleanup(self.cache.close)
    def write(self):self.path.write_text(json.dumps(self.data));self.path.chmod(0o600)
    def test_background_prepare_and_signal_read_no_io(self):
        self.cache.refresh()
        with patch('os.open',side_effect=AssertionError('signal IO')):
            value=self.cache.get('042510',NOW);self.assertTrue(value['coverage_complete'])
            value['coverage_complete']=False
            self.assertTrue(self.cache.get('042510',NOW)['coverage_complete'])
            self.assertEqual(len(self.cache.generation(NOW)),64)
    def test_slow_or_expired_cache_does_not_authorize(self):
        self.cache.refresh();self.clock[0]+=1.501;self.assertIsNone(self.cache.get('042510',NOW))
        self.cache.refresh();self.assertIsNone(self.cache.get('042510',NOW+timedelta(minutes=15)))
    def test_new_risk_and_corruption_revoke_old_data(self):
        self.cache.refresh();old=self.cache.generation(NOW)
        self.data['rows'][0]['unresolved_major_risk']=True;self.write();self.cache.refresh()
        self.assertIsNone(self.cache.get('042510',NOW));self.assertNotEqual(old,self.cache.generation(NOW))
        self.path.write_text('{}');self.cache.refresh();self.assertIsNone(self.cache.generation(NOW))
    def test_duplicates_symlink_and_unsafe_mode_rejected(self):
        self.data['rows']*=2;self.write()
        with self.assertRaises(ValueError):read_index(self.path,NOW)
        self.data['rows']=self.data['rows'][:1];self.write();self.path.chmod(0o666)
        with self.assertRaises(ValueError):read_index(self.path,NOW)
        self.path.chmod(0o600);link=self.path.with_name('link');link.symlink_to(self.path)
        with self.assertRaises(OSError):read_index(link,NOW)
    def test_close_revokes_without_waiting(self):
        self.cache.refresh();self.cache.close();self.assertIsNone(self.cache.get('042510',NOW))
    def test_wall_clock_rollback_cannot_expose_future_review(self):
        self.cache.refresh();self.clock[0] += .1
        self.assertIsNone(self.cache.get('042510',NOW-timedelta(microseconds=1)))
        self.assertIsNone(self.cache.generation(NOW-timedelta(seconds=1)))
        self.assertIsNotNone(self.cache.get('042510',NOW+timedelta(milliseconds=100)))
    def test_option_default_and_real_mode_unchanged(self):
        self.assertFalse(effective_fast_decision({},'paper'))
        self.assertFalse(effective_fast_decision({'paper_fast_decision_enabled':True},'real'))
        with self.assertRaises(SettingsError):effective_fast_decision({'paper_fast_decision_enabled':1},'paper')

    def test_scheduler_scope_uses_fresh_index_without_network_or_io(self):
        self.cache.refresh()
        with patch('os.open',side_effect=AssertionError('scheduler IO')):
            self.assertEqual(self.cache.symbols(NOW),('042510',))
            self.clock[0]+=1.501
            self.assertEqual(self.cache.symbols(NOW),())
        self.data['rows'][0]['unresolved_major_risk']=True
        self.write();self.cache.refresh()
        self.assertEqual(self.cache.symbols(NOW),())

    def test_provider_scope_is_local_and_expiry_or_future_time_fails_closed(self):
        from kiwoom_autotrader.trading.paper_strategy_context import PaperStrategyContextProvider
        provider=PaperStrategyContextProvider.__new__(PaperStrategyContextProvider)
        provider._exit_only=False;provider._now=lambda:NOW
        provider._s5_risk_path=self.path;provider._prearmed_risk=None
        self.assertEqual(provider.s5_eligible_symbols(NOW),('042510',))
        self.assertEqual(provider.s5_eligible_symbols(NOW+timedelta(seconds=1)),())
        self.assertEqual(provider.s5_eligible_symbols(NOW-timedelta(seconds=61)),())
        provider._prearmed_risk=self.cache;self.cache.refresh()
        with patch('os.open',side_effect=AssertionError('provider IO')):
            self.assertEqual(provider.s5_eligible_symbols(NOW),('042510',))
        provider._exit_only=True
        self.assertEqual(provider.s5_eligible_symbols(NOW),())

if __name__=='__main__':unittest.main()
