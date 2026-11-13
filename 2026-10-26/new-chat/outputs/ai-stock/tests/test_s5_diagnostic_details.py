"""Read-only S5 diagnostics retain actual checks without granting orders."""
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
import json
import unittest

from kiwoom_autotrader.trading.multi_strategy import MultiStrategyMonitor
from kiwoom_autotrader.trading.steady_uptrend_vwap import evaluate_steady_uptrend_vwap
from tests.test_steady_uptrend_vwap import fixture


class S5DiagnosticDetailsTests(unittest.TestCase):
    def setUp(self):
        self.now, self.bars, self.daily, self.risk = fixture()
        self.monitor = object.__new__(MultiStrategyMonitor)
        self.monitor._s5_input_diagnostics = {}
        self.monitor._symbol_errors = {'005930':'ownership_unavailable'}

    def evaluate(self, **changes):
        values = {'bars':self.bars, 'symbol':'005930', 'now':self.now,
            'current_price':10220, 'daily_context':self.daily, 'risk_context':self.risk,
            'require_short':True}
        values.update(changes)
        return evaluate_steady_uptrend_vwap(**values)

    def record(self, evaluation):
        before = deepcopy(evaluation)
        self.monitor._record_s5_input('005930', evaluation, self.now)
        self.assertEqual(evaluation, before)
        result = self.monitor._s5_input_diagnostics['005930']
        self.assertFalse(result['order_permission'])
        self.assertEqual(self.monitor._symbol_errors, {'005930':'ownership_unavailable'})
        json.dumps(result, allow_nan=False)
        return result

    def test_actual_pattern_only_failure_separate_from_passed_vwap(self):
        bars = [replace(bar, volume=100) for bar in self.bars]
        result = self.record(self.evaluate(bars=bars))
        self.assertEqual(result['reason'], 'pattern_or_vwap_not_met')
        self.assertEqual(result['check_groups']['pattern']['status'], 'fail')
        self.assertIn('volume_median_growth_1_1', result['failed_checks'])
        self.assertEqual(result['check_groups']['vwap']['status'], 'pass')
        self.assertEqual(result['check_groups']['risk']['status'], 'pass')

    def test_actual_vwap_only_failure_separate_from_passed_pattern(self):
        result = self.record(self.evaluate(current_price=9000))
        self.assertEqual(result['check_groups']['pattern']['status'], 'pass')
        self.assertEqual(result['check_groups']['vwap']['status'], 'fail')
        self.assertEqual(result['check_groups']['vwap']['failed_checks'], ['current_price_above_vwap'])

    def test_actual_pattern_and_vwap_failures_both_retained(self):
        result = self.record(self.evaluate(
            bars=[replace(bar, volume=100) for bar in self.bars], current_price=9000))
        self.assertEqual(result['check_groups']['pattern']['status'], 'fail')
        self.assertEqual(result['check_groups']['vwap']['status'], 'fail')
        self.assertEqual(set(result['failed_checks']),
            {'volume_median_growth_1_1', 'current_price_above_vwap'})

    def test_early_missing_minutes_does_not_claim_unvisited_gates_passed(self):
        result = self.record(self.evaluate(bars=self.bars[:-5]))
        self.assertEqual(result['status'], 'DORMANT')
        self.assertEqual(result['checks'], {})
        self.assertIn('pattern_bucket_without_positive_volume',
            result['failure_details']['pattern']['reason_codes'])
        for group in result['check_groups'].values():
            self.assertEqual(group['status'], 'not_evaluated')
            self.assertFalse(group['evaluation_complete'])
            self.assertEqual(group['failed_checks'], [])
            self.assertTrue(group['not_evaluated_checks'])

    def test_missing_daily_distinguishes_evaluated_price_gates_from_unvisited_risk(self):
        result = self.record(self.evaluate(daily_context=None))
        self.assertEqual(result['reason'], 'prior_daily_unavailable')
        self.assertEqual(result['check_groups']['pattern']['status'], 'pass')
        self.assertEqual(result['check_groups']['vwap']['status'], 'pass')
        for name in ('prior_daily', 'risk'):
            self.assertEqual(result['check_groups'][name]['status'], 'not_evaluated')

    def test_observed_vwap_basis_and_exact_missing_minutes_survive(self):
        self.now += timedelta(minutes=5)
        last = self.bars[-1]
        bars = self.bars + [replace(last, start=last.start+timedelta(minutes=i),
            open_price=last.open_price+6*i, high_price=last.high_price+6*i,
            low_price=last.low_price+6*i, close=last.close+6*i, volume=170) for i in range(1,6)]
        bars = [replace(bar, source_query_started_at=self.now) for i,bar in enumerate(bars) if i!=1]
        evaluation = self.evaluate(bars=bars, current_price=10250)
        gap = self.now.replace(hour=9, minute=1, second=0).isoformat()
        result = self.record(evaluation)
        self.assertEqual(result['session_coverage']['missing_minutes'], [gap])
        self.assertEqual(result['session_coverage']['expected_minutes'], 40)
        self.assertEqual(result['session_coverage']['observed_minutes'], 39)
        self.assertEqual(result['session_coverage']['tolerated_old_missing_count'], 1)
        self.assertEqual(result['session_coverage'], evaluation['session_coverage'])
        self.assertEqual(result['vwap'], evaluation['vwap'])
        self.assertFalse(result['vwap']['exact_full_session_vwap'])

    def test_allowlist_and_bounds_drop_raw_payload_and_nonboolean_checks(self):
        evaluation = self.evaluate()
        evaluation['checks']['provider_raw_body'] = 'private' * 10000
        evaluation['checks']['vwap_valid'] = 1
        evaluation['check_groups'] = {'vwap':{'status':'pass'}}
        gap = self.now.replace(hour=9, minute=1, second=0).isoformat()
        evaluation['session_coverage'] = {'basis':'invented', 'expected_minutes':10**30,
            'missing_minutes':[gap]*1000, 'missing_pattern_minutes':["secret"],
            'synthetic_bars_created':False, 'coverage_validated':False,
            'raw_body':'private' * 10000}
        evaluation['vwap'].update(volume=10**100, raw_body='private' * 10000)
        evaluation['failure_details'] = {'pattern':{'failed_checks':['provider_raw_body'],
            'reason_codes':['recent_pattern_minutes_missing', 'private' * 10000]}}
        result = self.record(evaluation)
        self.assertNotIn('provider_raw_body', result['checks'])
        self.assertNotIn('vwap_valid', result['checks'])
        self.assertEqual(result['check_groups']['vwap']['status'], 'not_evaluated')
        self.assertEqual(result['session_coverage']['missing_minutes'], [gap])
        self.assertIsNone(result['session_coverage']['expected_minutes'])
        self.assertIsNone(result['session_coverage']['basis'])
        self.assertNotIn('volume', result['vwap'])
        self.assertNotIn('private', json.dumps(result))
        self.assertEqual(result['failure_details']['pattern']['reason_codes'],
            ['recent_pattern_minutes_missing'])
        self.assertLess(len(json.dumps(result)), 12000)

    def test_check_failures_are_derived_not_trusted_from_claimed_group_status(self):
        evaluation = self.evaluate()
        evaluation['checks']['current_price_above_vwap'] = False
        evaluation['failed_checks'] = []
        evaluation['check_groups'] = {'vwap':{'status':'pass'}}
        result = self.record(evaluation)
        self.assertEqual(result['check_groups']['vwap']['status'], 'fail')
        self.assertIn('current_price_above_vwap', result['failed_checks'])

    def test_partial_bucket_coverage_and_seventeen_failed_checks_are_not_lost(self):
        evaluation = self.evaluate(bars=[bar for i,bar in enumerate(self.bars) if i%5 == 0])
        self.assertTrue(evaluation['eligible'], evaluation)
        keys = list(evaluation['checks'])
        for key in keys[:17]:
            evaluation['checks'][key] = False
        result = self.record(evaluation)
        self.assertEqual(len(result['failed_checks']), 17)
        coverage = result['session_coverage']
        self.assertEqual(coverage, evaluation['session_coverage'])
        self.assertEqual(coverage['pattern_nonempty_positive_volume_buckets'], 7)
        self.assertEqual(len(coverage['pattern_bucket_coverage']), 7)
        for bucket in coverage['pattern_bucket_coverage']:
            self.assertEqual(bucket['observed_minutes'], 1)
            self.assertEqual(bucket['positive_volume_minutes'], 1)
            self.assertEqual(len(bucket['missing_minutes']), 4)
            self.assertFalse(bucket['complete'])
        self.assertFalse(result['vwap']['exact_full_session_vwap'])


if __name__ == '__main__':
    unittest.main()
