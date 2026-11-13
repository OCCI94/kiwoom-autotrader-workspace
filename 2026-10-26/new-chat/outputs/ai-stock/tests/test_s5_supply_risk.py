import unittest
from copy import deepcopy
from datetime import timedelta
from kiwoom_autotrader.trading.s5_supply_risk import validate_short_selling
from kiwoom_autotrader.trading.steady_uptrend_vwap import validate_s5_risk, evaluate_steady_uptrend_vwap
from tests.test_steady_uptrend_vwap import fixture

class S5SupplyRiskTests(unittest.TestCase):
    def setUp(self):
        self.now,self.bars,self.daily,self.risk=fixture()
        self.short=self.risk['short_selling_evidence'];self.expected=self.short['expected_session_days']
    def check(self,context=None,now=None,expected=None):
        return validate_short_selling(self.short if context is None else context,symbol='005930',
            now=now or self.now,expected_session_days=expected or self.expected)
    def test_explicit_five_day_coverage_and_official_fraction(self):
        value=self.check();self.assertTrue(value['eligible']);self.assertEqual(value['observed_session_count'],5)
        self.assertEqual(value['max_daily_short_amount_share'],'0.02')
        self.assertEqual(value['ratio_denominator'],'all_day_including_extended_sessions')
        self.assertNotIn('total_turnover_krw',value)
    def test_any_previous_day_above_ten_percent_blocks_even_if_latest_low(self):
        self.short['rows'][0]['short_amount_share']='0.1000000001'
        result=self.check();self.assertFalse(result['eligible'])
        self.assertEqual(result['status'],'BLOCK');self.assertEqual(result['over_limit_sessions'],[self.expected[0]])
        self.assertEqual(result['latest_short_amount_share'],'0.02')
        self.short['rows'][0]['short_amount_share']='0.10';self.assertTrue(self.check()['eligible'])
    def test_missing_duplicates_order_and_future_dates_are_not_zero(self):
        for change in (lambda c:c['rows'].pop(),lambda c:c['rows'].reverse(),
                       lambda c:c['rows'].__setitem__(1,deepcopy(c['rows'][0])),
                       lambda c:c.update(previous_session_day='2026-11-02'),
                       lambda c:c.update(expected_session_days=['2026-10-23']+self.expected[1:]),
                       lambda c:c['rows'][1].update(updated_at=(self.now+timedelta(seconds=1)).isoformat())):
            changed=deepcopy(self.short);change(changed)
            result=self.check(changed);self.assertFalse(result['eligible']);self.assertEqual(result['status'],'UNAVAILABLE')
    def test_current_session_and_exact_daily_previous_dates_required(self):
        for changes in ({'as_of_session_day':'2026-11-03'},{'symbol':'000660'},
                        {'source_api':'unverified'},{'calendar_sha256':'bad'},
                        {'source_sha256':None}):
            with self.subTest(changes=changes):self.assertFalse(self.check({**self.short,**changes})['eligible'])
        self.assertFalse(self.check(expected=['2026-10-23']+self.expected[1:])['eligible'])
    def test_time_future_expiry_and_slow_request_rejected(self):
        for key,value in [('observed_at',(self.now+timedelta(seconds=1)).isoformat()),
                          ('request_started_at',(self.now-timedelta(minutes=6)).isoformat()),
                          ('expires_at',self.now.isoformat()),
                          ('expires_at',(self.now+timedelta(hours=25)).isoformat()),
                          ('observed_at',self.now.replace(tzinfo=None).isoformat())]:
            changed=deepcopy(self.short);changed[key]=value
            with self.subTest(key=key,value=value):self.assertFalse(self.check(changed)['eligible'])
    def test_nonfinite_percent_unit_and_unknown_cannot_be_clear(self):
        for value in ('NaN','Infinity','-0.01','10','1.01',None,True,.05):
            changed=deepcopy(self.short);changed['rows'][2]['short_amount_share']=value
            with self.subTest(value=value):self.assertFalse(self.check(changed)['eligible'])
    def test_legacy_evidence_stays_readable_but_new_dispatch_requires_supply(self):
        legacy=deepcopy(self.risk);legacy.pop('short_selling_evidence')
        kwargs=dict(current_price=10220,daily_context=self.daily,risk_context=legacy,symbol='005930',now=self.now)
        self.assertTrue(validate_s5_risk(**kwargs)['eligible'])
        self.assertFalse(validate_s5_risk(**kwargs,require_short=True)['eligible'])
        evalargs=dict(bars=self.bars,symbol='005930',now=self.now,current_price=10220,daily_context=self.daily,risk_context=legacy)
        self.assertTrue(evaluate_steady_uptrend_vwap(**evalargs)['eligible'])
        current=evaluate_steady_uptrend_vwap(**evalargs,require_short=True)
        self.assertFalse(current['eligible']);self.assertEqual(current['status'],'DORMANT')
    def test_new_evaluation_is_same_with_optional_and_required_supply_and_bound_to_rows(self):
        kwargs=dict(bars=self.bars,symbol='005930',now=self.now,current_price=10220,
                    daily_context=self.daily,risk_context=self.risk)
        self.assertEqual(evaluate_steady_uptrend_vwap(**kwargs),evaluate_steady_uptrend_vwap(**kwargs,require_short=True))
        self.short['rows'][0]['short_amount_share']='0.11'
        blocked=evaluate_steady_uptrend_vwap(**kwargs,require_short=True)
        self.assertEqual(blocked['reason'],'short_selling_pressure_high');self.assertEqual(blocked['status'],'BLOCK')

class S5SupplyDispatchTests(unittest.TestCase):
    def test_legacy_v4_remains_readable_but_cannot_dispatch_new_buy(self):
        from tests.test_s5_source_bound_evidence import evidence,fixture as source_fixture
        from kiwoom_autotrader.trading.decision_evidence import freeze_evidence
        from kiwoom_autotrader.trading.execution_log import ExecutionTracker,ExecutionLogError
        now,bars,market=source_fixture()
        saved=evidence().as_dict();proof=saved['multi_strategy_signal']['s5_input']
        proof['risk_context'].pop('short_selling_evidence')
        proof['evaluation']=evaluate_steady_uptrend_vwap(bars,symbol='003220',now=now,
            current_price=market['current_price'],daily_context=proof['daily_context'],risk_context=proof['risk_context'])
        restored=freeze_evidence(saved)
        self.assertTrue(restored.as_dict()['multi_strategy_signal']['s5_input']['evaluation']['eligible'])
        with self.assertRaises(ExecutionLogError):
            ExecutionTracker._assert_current_multi_entry_policy('paper','buy','S5_steady_uptrend_vwap',restored.as_dict(),now)
        ExecutionTracker._assert_current_multi_entry_policy('paper','sell','S5_steady_uptrend_vwap',None,now)
    def test_valid_short_proof_cannot_be_tampered_or_expire_at_dispatch(self):
        from tests.test_s5_source_bound_evidence import evidence,fixture as source_fixture
        from kiwoom_autotrader.trading.decision_evidence import freeze_evidence,DecisionEvidenceError
        from kiwoom_autotrader.trading.execution_log import ExecutionTracker,ExecutionLogError
        now,_,_=source_fixture();saved=evidence().as_dict()
        ExecutionTracker._assert_current_multi_entry_policy('paper','buy','S5_steady_uptrend_vwap',saved,now)
        changed=deepcopy(saved);changed['multi_strategy_signal']['s5_input']['risk_context']['short_selling_evidence']['rows'][0]['short_amount_share']='0.20'
        with self.assertRaises(DecisionEvidenceError):freeze_evidence(changed)
        changed=deepcopy(saved)
        short=changed['multi_strategy_signal']['s5_input']['risk_context']['short_selling_evidence']
        short['expires_at']=(now+timedelta(seconds=1)).isoformat()
        changed['multi_strategy_signal']['s5_input']['evaluation']['short_selling']['expires_at']=short['expires_at']
        valid=freeze_evidence(changed)
        with self.assertRaises(ExecutionLogError):
            ExecutionTracker._assert_current_multi_entry_policy('paper','buy','S5_steady_uptrend_vwap',valid.as_dict(),now+timedelta(seconds=1))
    def test_local_replacement_reloads_supply_without_daily_refetch_or_receiver_reload(self):
        import json
        from tests.test_steady_uptrend_vwap import S5ProviderTests
        fx=S5ProviderTests('test_cached_prior_daily_and_clear_risk');fx.setUp()
        try:
            initial=fx.provider.fetch_s5_inputs('005930')['s5_risk_context']
            self.assertEqual(initial['short_selling_evidence']['rows'][0]['short_amount_share'],'0.02')
            payload=json.loads(fx.riskfile.read_text());payload['rows'][0]['short_selling_evidence']['rows'][0]['short_amount_share']='0.12'
            fx.riskfile.write_text(json.dumps(payload))
            later=fx.provider.fetch_s5_inputs('005930')
            self.assertEqual(later['s5_risk_context']['short_selling_evidence']['rows'][0]['short_amount_share'],'0.12')
            self.assertEqual(len(fx.calls),1)
            blocked=validate_s5_risk(current_price=10220,daily_context=later['s5_daily_context'],
                risk_context=later['s5_risk_context'],symbol='005930',now=fx.now,require_short=True)
            self.assertEqual(blocked['reason'],'short_selling_pressure_high')
        finally:fx.doCleanups()
