"""Observed bucket gaps survive evidence validation without bypassing gates."""
from copy import deepcopy
from datetime import timedelta
import unittest

from kiwoom_autotrader.trading.decision_evidence import freeze_evidence, DecisionEvidenceError
from kiwoom_autotrader.trading.multi_strategy import S5
from kiwoom_autotrader.trading.multi_strategy_evidence import multi_strategy_evidence
from kiwoom_autotrader.trading.steady_uptrend_vwap import evaluate_steady_uptrend_vwap
from tests.test_s5_source_bound_evidence import fixture
from tests import test_s5_boundary_scheduler as scheduler_fixture


def bounded_evidence(missing=(2, 7)):
    now, complete, market = fixture(60)
    bars = [bar for index, bar in enumerate(complete) if index not in missing]
    evaluation = evaluate_steady_uptrend_vwap(
        bars, symbol='003220', now=now, current_price=market['current_price'],
        daily_context=market['s5_daily_context'], risk_context=market['s5_risk_context'])
    market['steady_uptrend_vwap_evidence'] = evaluation
    evidence = multi_strategy_evidence(
        strategy=S5, symbol='003220', side='buy', reason='steady_uptrend_vwap',
        budget=500000, position={'generation_id': None, 'owned_qty': None, 'basis_krw': None},
        bars=bars, market=market, observed_at=now, baseline=None, source_hash='f' * 64,
        b3_config={'multi_b3_stage': 'initial', 'multi_b3_initial_intent_id': None,
                   'multi_b3_initial_entry_pct': 50, 'multi_b3_add_on_profit_bps': 50,
                   'multi_b3_total_budget_krw': 1000000, 'max_daily_entries_per_symbol': 2})
    return evidence, bars, evaluation


class BoundedGapOrderEvidenceTests(unittest.TestCase):
    def test_old_and_partial_recent_gaps_roundtrip_with_actual_rows_only(self):
        for missing in ((2,), (2, 7), (2, 7, 10), (2, 7, 10, 40)):
            with self.subTest(missing=missing):
                evidence, bars, evaluation = bounded_evidence(missing)
                self.assertTrue(evaluation['eligible'], evaluation)
                decoded = freeze_evidence(evidence.as_dict()).as_dict()
                prefix = decoded['multi_strategy_signal']['s5_input']['minute_prefix']
                self.assertEqual(len(prefix['rows']), 60 - len(missing))
                self.assertEqual([row[0] for row in prefix['rows']], [bar.start.isoformat() for bar in bars])
                self.assertEqual(decoded['budget'], 500000)
                self.assertEqual(decoded['config']['multi_b3_total_budget_krw'], 1000000)

    def test_dropping_another_row_without_recomputed_evidence_is_rejected(self):
        evidence, _, _ = bounded_evidence((2,))
        changed = deepcopy(evidence.as_dict())
        del changed['multi_strategy_signal']['s5_input']['minute_prefix']['rows'][0]
        with self.assertRaises(DecisionEvidenceError):
            freeze_evidence(changed)

    def test_empty_recent_bucket_and_truncated_open_cannot_create_order_evidence(self):
        for missing in (tuple(range(55,60)), tuple(range(11))):
            with self.subTest(missing=missing), self.assertRaises(DecisionEvidenceError):
                bounded_evidence(missing)

    def test_old_gap_does_not_relax_risk_or_b3_policy_at_order_validation(self):
        evidence, _, _ = bounded_evidence()
        for kind in ('risk', 'budget'):
            changed = deepcopy(evidence.as_dict())
            if kind == 'risk':
                changed['multi_strategy_signal']['s5_input']['risk_context']['unresolved_major_risk'] = True
            else:
                changed['config']['multi_b3_total_budget_krw'] = 2000000
            with self.subTest(kind=kind), self.assertRaises(DecisionEvidenceError):
                freeze_evidence(changed)

    def scheduler(self):
        case = scheduler_fixture.BoundarySchedulerTests(methodName='runTest')
        case.setUp()
        self.addCleanup(case.doCleanups)
        case.now = case.now.replace(hour=10, minute=0, second=3)
        case.eligible = (case.codes[3],)
        def current_quote(symbol, as_of=None):
            opening = case.now.replace(hour=9, minute=0, second=0, microsecond=0)
            price = 10000 + int((case.now - opening).total_seconds() // 60) * 6
            return {'symbol': symbol, 'current_price': price, 'day_high': price, 'change_pct': '5'}
        case.provider.fetch = current_quote
        return case

    def test_monitor_recovers_old_gaps_and_emits_once_per_boundary(self):
        case = self.scheduler()
        original = case.monitor._session_chart
        case.monitor._session_chart = lambda *args, **kwargs: [
            bar for bar in original(*args, **kwargs)
            if bar.start.strftime('%H:%M') not in ('09:02', '09:07')]
        case.tick()
        self.assertEqual(len(case.orders), 1, case.monitor.status())
        order = case.orders[0]
        self.assertEqual((order['strategy'], order['budget']), (S5, 500000))
        self.assertEqual(len(order['bars']), 58)
        case.now += timedelta(minutes=1)
        for _ in range(5):
            case.tick()
        self.assertEqual(len(case.orders), 1)

    def test_monitor_empty_recent_bucket_and_capacity_still_block(self):
        case = self.scheduler()
        original = case.monitor._session_chart
        case.monitor._session_chart = lambda *args, **kwargs: [
            bar for bar in original(*args, **kwargs)
            if bar.start.strftime('%H:%M') not in ('09:55','09:56','09:57','09:58','09:59')]
        case.tick()
        self.assertEqual(case.orders, [])
        case.now += timedelta(minutes=5)
        case.monitor._session_chart = original
        case.capacity_blocked = True
        case.tick()
        self.assertEqual(case.orders, [])
        self.assertEqual(case.monitor.status()['last_decision']['reason'], 'concurrent_position_limit_reached')
