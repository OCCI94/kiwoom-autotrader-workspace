"""Immutable old S5 evidence stays readable; new dispatch requires supply."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta
import unittest
from kiwoom_autotrader.trading.decision_evidence import freeze_evidence, DecisionEvidenceError, _serialize_bar
from kiwoom_autotrader.trading.multi_strategy_evidence import multi_strategy_evidence
from kiwoom_autotrader.trading.execution_log import ExecutionTracker, ExecutionLogError
from kiwoom_autotrader.trading.observed_bar_window import timeline_rows
from kiwoom_autotrader.trading.s5_decision_evidence import unpack_prefix
from kiwoom_autotrader.trading.steady_uptrend_vwap import evaluate_steady_uptrend_vwap, validate_s5_risk
from tests.test_s5_source_bound_evidence import fixture, evidence


# Exact output from the preserved pre-coverage evaluator, not reconstructed keys.
ORIGINAL_EVALUATOR_SHA256 = 'd00245bca49792d836be92daaf49ebd54b9554578f1af023cc8323b36ee96764'
LEGACY_COMPLETE_EVALUATION = {'adr_extension_multiple': '0.6219118775539963258613770332',
 'adr_pct': '6.323378619379209614528936995',
 'checks': {'adr_below_6': True,
            'bullish_70pct': True,
            'close_drawdown_at_most_0_8pct': True,
            'closed_price_above_vwap': True,
            'current_price_above_vwap': True,
            'dart_risk_review_clear': True,
            'last_bullish_body': True,
            'last_close_location': True,
            'last_upper_wick': True,
            'ma3_above_ma7': True,
            'no_bar_above_1_5pct': True,
            'prior_daily_uptrend': True,
            'return_1_to_4pct': True,
            'rising_closes_66pct': True,
            'rising_lows_66pct': True,
            'short_selling_five_sessions_clear': True,
            'volume_median_growth_1_1': True,
            'vwap_five_minute_slope_nonnegative': True,
            'vwap_valid': True},
 'eligible': True,
 'five_minute_count': 12,
 'prior_daily': {'ma20': '9790', 'ma5': '9940'},
 'reason': 'steady_uptrend_vwap',
 'schema': 's5-steady-uptrend-vwap-research-v1',
 'session_minute_count': 60,
 'short_selling': {'calendar_sha256': 'eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee',
                   'eligible': True,
                   'expires_at': '2026-11-02T18:00:03+09:00',
                   'latest_short_amount_share': '0.02',
                   'max_daily_short_amount_share': '0.02',
                   'observed_at': '2026-11-02T10:00:03+09:00',
                   'observed_session_count': 5,
                   'over_limit_sessions': [],
                   'previous_session_day': '2026-10-30',
                   'ratio_denominator': 'all_day_including_extended_sessions',
                   'reason': 'short_selling_clear',
                   'source_sha256': 'ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff',
                   'status': 'PASS'},
 'signal_at': '2026-11-02T10:00:00+09:00',
 'status': 'PASS',
 'vwap': {'hlc3_volume_numerator': 271258625,
          'prior_5m_numerator': 231620875,
          'prior_5m_volume': 7675,
          'volume': 8975}}

class RequiredShortEvidenceTests(unittest.TestCase):
    def test_valid_initial_proof_still_roundtrips(self):
        ev=evidence().as_dict()
        self.assertEqual(freeze_evidence(ev).as_dict(),ev)

    def test_missing_short_with_recomputed_optional_pass_cannot_authorize_initial(self):
        ev=evidence().as_dict();proof=ev['multi_strategy_signal']['s5_input']
        del proof['risk_context']['short_selling_evidence']
        bars=unpack_prefix(proof['minute_prefix'],'003220')
        # Attack recomputes all user-controlled conclusions rather than merely
        # deleting a leaf. Optional research evaluation still passes, but the
        # final operational S5 gate explicitly requires the missing evidence.
        proof['evaluation']=evaluate_steady_uptrend_vwap(bars,symbol='003220',
            now=datetime.fromisoformat(ev['observed_at']),
            current_price=ev['multi_strategy_signal']['market']['current_price'],
            daily_context=proof['daily_context'],risk_context=proof['risk_context'])
        self.assertTrue(proof['evaluation']['eligible'])
        restored=freeze_evidence(ev).as_dict()
        with self.assertRaises(ExecutionLogError):
            ExecutionTracker._assert_current_multi_entry_policy('paper','buy','S5_steady_uptrend_vwap',
                restored,datetime.fromisoformat(ev['evaluated_at']))

    def add_evidence(self):
        now,bars,market=fixture(60)
        bars=bars[-21:];bars[-1]=replace(bars[-1],volume=1000)
        filled=now-timedelta(minutes=2)
        pos={'generation_id':'f'*32,'owned_qty':10,'basis_krw':100000,'half_take_done':False,
             'first_fill_at':filled.isoformat(),'first_fill_observed_at':filled.isoformat(),'first_fill_timing_verified':True}
        addition={'initial_fill_observed_at':filled.isoformat(),
                  'trend_bars':[_serialize_bar(b) for b in bars],'trend_timeline':timeline_rows(bars)}
        config={'multi_b3_stage':'add','multi_b3_initial_intent_id':'f'*32,'multi_b3_initial_entry_pct':50,
                'multi_b3_add_on_profit_bps':50,'multi_b3_total_budget_krw':1000000,'max_daily_entries_per_symbol':2}
        return multi_strategy_evidence(strategy='S5_steady_uptrend_vwap',symbol='003220',side='buy',reason='add_on_strength',
            budget=500000,position=pos,bars=bars,market=market,observed_at=now,source_hash='f'*64,
            b3_config=config,b3_add_on=addition).as_dict()

    def test_add_on_cannot_omit_short_evidence_after_valid_owned_proof(self):
        ev=self.add_evidence();self.assertEqual(freeze_evidence(ev).as_dict(),ev)
        proof=ev['multi_strategy_signal']['s5_input'];del proof['risk_context']['short_selling_evidence']
        optional=validate_s5_risk(current_price=ev['multi_strategy_signal']['market']['current_price'],
            daily_context=proof['daily_context'],risk_context=proof['risk_context'],symbol='003220',
            now=datetime.fromisoformat(ev['evaluated_at']))
        self.assertTrue(optional['eligible'])
        restored=freeze_evidence(ev).as_dict()
        with self.assertRaises(ExecutionLogError):
            ExecutionTracker._assert_current_multi_entry_policy('paper','buy','S5_steady_uptrend_vwap',
                restored,datetime.fromisoformat(ev['evaluated_at']))

    def test_null_or_empty_short_is_never_equivalent_to_verified(self):
        for value in (None,{}):
            ev=evidence().as_dict();ev['multi_strategy_signal']['s5_input']['risk_context']['short_selling_evidence']=value
            with self.subTest(value=value),self.assertRaises(DecisionEvidenceError):freeze_evidence(ev)

    def test_actual_precoverage_evaluator_result_remains_immutable_readable(self):
        ev=evidence().as_dict();ev['multi_strategy_signal']['s5_input']['evaluation']=deepcopy(LEGACY_COMPLETE_EVALUATION)
        restored=freeze_evidence(ev).as_dict()
        self.assertEqual(restored,ev)
        self.assertNotIn('session_coverage',restored['multi_strategy_signal']['s5_input']['evaluation'])
        ExecutionTracker._assert_current_multi_entry_policy('paper','buy','S5_steady_uptrend_vwap',restored,
            datetime.fromisoformat(ev['evaluated_at']))

    def test_sparse_current_input_cannot_hide_as_legacy_projection(self):
        ev=evidence().as_dict();proof=ev['multi_strategy_signal']['s5_input']
        del proof['minute_prefix']['rows'][0]
        bars=unpack_prefix(proof['minute_prefix'],'003220')
        fresh=evaluate_steady_uptrend_vwap(bars,symbol='003220',now=datetime.fromisoformat(ev['observed_at']),
            current_price=ev['multi_strategy_signal']['market']['current_price'],daily_context=proof['daily_context'],risk_context=proof['risk_context'])
        self.assertTrue(fresh['eligible']);self.assertFalse(fresh['session_coverage']['session_complete'])
        for key in ('session_coverage','check_groups','failed_checks','failure_details','pattern_five_minute_count'):fresh.pop(key)
        for key in ('basis','session_complete','exact_full_session_vwap'):fresh['vwap'].pop(key)
        proof['evaluation']=fresh
        with self.assertRaises(DecisionEvidenceError):freeze_evidence(ev)

    def test_mixed_legacy_schema_or_changed_economic_value_is_rejected(self):
        for change in ('partial_metadata','altered_vwap'):
            ev=evidence().as_dict();proof=ev['multi_strategy_signal']['s5_input']
            proof['evaluation']=deepcopy(LEGACY_COMPLETE_EVALUATION)
            if change=='partial_metadata':proof['evaluation']['session_coverage']={}
            else:proof['evaluation']['vwap']['volume']+=1
            with self.subTest(change=change),self.assertRaises(DecisionEvidenceError):freeze_evidence(ev)
