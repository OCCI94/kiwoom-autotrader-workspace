"""S5 bucket evidence reaches the guarded gateway; other strategies stay strict."""
from copy import deepcopy
from datetime import timedelta
import unittest

from kiwoom_autotrader.trading.decision_evidence import freeze_evidence, DecisionEvidenceError
from kiwoom_autotrader.trading.multi_strategy import S2, S3, S4, S5, EntryState
from kiwoom_autotrader.trading.multi_strategy_evidence import multi_strategy_evidence
from kiwoom_autotrader.trading.steady_uptrend_vwap import evaluate_steady_uptrend_vwap
from tests.test_s5_source_bound_evidence import fixture


def build_sparse(per_bucket=1, **overrides):
    now, complete, market = fixture(35)
    bars = [bar for i,bar in enumerate(complete) if i%5 < per_bucket]
    market['steady_uptrend_vwap_evidence'] = evaluate_steady_uptrend_vwap(
        bars, symbol='003220', now=now, current_price=market['current_price'],
        daily_context=market['s5_daily_context'], risk_context=market['s5_risk_context'])
    values = dict(strategy=S5, symbol='003220', side='buy', reason='steady_uptrend_vwap',
        budget=500000, position={'generation_id':None, 'owned_qty':None, 'basis_krw':None},
        bars=bars, market=market, observed_at=now, baseline=None, source_hash='f'*64,
        b3_config={'multi_b3_stage':'initial', 'multi_b3_initial_intent_id':None,
            'multi_b3_initial_entry_pct':50, 'multi_b3_add_on_profit_bps':50,
            'multi_b3_total_budget_krw':1000000, 'max_daily_entries_per_symbol':2})
    values.update(overrides)
    return multi_strategy_evidence(**values)


class SparseOrderEvidenceTests(unittest.TestCase):
    def test_seven_fourteen_and_twentyone_actual_rows_roundtrip(self):
        for count in (1,2,3):
            with self.subTest(per_bucket=count):
                evidence = build_sparse(count)
                out = freeze_evidence(evidence.as_dict()).as_dict()
                signal = out['multi_strategy_signal']
                self.assertEqual(len(out['bars']), 7*count)
                self.assertEqual(len(signal['s5_input']['minute_prefix']['rows']), 7*count)
                self.assertEqual(signal['bar_window']['policy'], 's5_actual_observed_five_minute_buckets_v1')
                self.assertEqual(signal['sma20_status'], 'unavailable')
                self.assertIsNone(signal['sma20'])
                self.assertEqual(out['budget'], 500000)
                self.assertEqual(out['config']['multi_b3_total_budget_krw'], 1000000)

    def test_suffix_timeline_is_bound_to_complete_prefix_provenance(self):
        out = build_sparse().as_dict()
        out['multi_strategy_signal']['bar_window']['timeline'][0]['batch'] = 'invented'
        with self.assertRaises(DecisionEvidenceError):
            freeze_evidence(out)

    def test_policy_cannot_bypass_current_s5_risk_or_recomputed_checks(self):
        for variant in ('risk','checks','budget'):
            out = build_sparse().as_dict()
            if variant == 'risk':
                out['multi_strategy_signal']['s5_input']['risk_context']['unresolved_major_risk'] = True
            elif variant == 'checks':
                out['multi_strategy_signal']['s5_input']['evaluation']['checks']['current_price_above_vwap'] = False
            else:
                out['budget'] = 500001
            with self.subTest(variant=variant), self.assertRaises(DecisionEvidenceError):
                freeze_evidence(out)

    def test_unchanged_s2_s3_s4_and_add_on_paths_reject_sparse_suffix(self):
        reasons = {S2:'shallow_pullback_high_breakout', S3:'observed_20pct_crossing',
            S4:'theme_leader_or_verified_disclosure'}
        for strategy, reason in reasons.items():
            with self.subTest(strategy=strategy), self.assertRaises(DecisionEvidenceError):
                build_sparse(strategy=strategy, reason=reason, baseline=EntryState())
        with self.assertRaises(DecisionEvidenceError):
            build_sparse(reason='add_on_strength')

    def test_quote_age_and_five_minute_boundary_are_still_rechecked(self):
        out = build_sparse().as_dict()
        from datetime import datetime
        out['evaluated_at'] = (datetime.fromisoformat(out['observed_at'])+timedelta(seconds=6)).isoformat()
        with self.assertRaises(DecisionEvidenceError):
            freeze_evidence(out)

    def scheduler(self):
        from tests.test_s5_boundary_scheduler import BoundarySchedulerTests
        case = BoundarySchedulerTests(methodName='runTest')
        case.setUp()
        self.addCleanup(case.doCleanups)
        case.eligible = (case.codes[3],)
        case.options['paper_multi_strategy_ids'] = [S2,S3,S4,S5]
        original = case.monitor._session_chart
        def sparse(*args, **kwargs):
            return [bar for bar in original(*args, **kwargs)
                if bar.start.minute%5 == 0 and bar.start < case.monitor._s5_boundary(case.now)]
        case.monitor._session_chart = sparse
        return case

    def test_monitor_s5_reaches_gateway_despite_ordinary_twentyone_bar_gate(self):
        case = self.scheduler()
        case.tick()
        self.assertEqual(len(case.orders), 1, case.monitor.status())
        order = case.orders[0]
        self.assertEqual(order['strategy'], S5)
        self.assertEqual(len(order['bars']), 7)
        self.assertEqual(order['budget'], 500000)
        diag = case.monitor.status()['s5_input_diagnostics'][case.eligible[0]]
        self.assertEqual(diag['status'], 'PASS')
        self.assertEqual(diag['session_coverage']['pattern_nonempty_positive_volume_buckets'], 7)
        self.assertFalse(diag['order_permission'])
        self.assertFalse(case.monitor.status()['bar_window_diagnostics'][case.eligible[0]]['bounded_actual_21_bar_window_valid'])

    def test_ownership_unknown_still_blocks_before_sparse_chart(self):
        case = self.scheduler()
        case.opens = [case.eligible[0]]
        case.tracker.multi_strategy_position = lambda *a:{'status':'unsupported'}
        case.tick()
        self.assertEqual(case.orders, [])
        self.assertFalse(any(call[0]=='chart' for call in case.calls))
        self.assertEqual(case.monitor.status()['symbol_errors'][case.eligible[0]], 'ownership_unavailable')


if __name__ == '__main__':
    unittest.main()
