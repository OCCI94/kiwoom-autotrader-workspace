import copy
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import unittest

from kiwoom_autotrader.trading.paper_experiment_admission import validate_admission


class AdmissionTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 2, 10, 0, tzinfo=ZoneInfo('Asia/Seoul'))
        self.stamp = self.now.isoformat()
        source = {'source_sha256': 'a' * 64, 'available_at': self.stamp}
        self.value = {
            'schema': 'paper-experiment-admission-v1', 'symbol': '005930', 'arm': 'EXP_ORB_A',
            'observed_at': self.stamp,
            'instrument': {**source, 'common_share': True, 'status': 'normal'},
            'turnover': {**source, 'complete': True, 'days': [
                {'date': (self.now.date() - timedelta(days=21-i)).strftime('%Y%m%d'),
                 'turnover_krw': 3_000_000_000} for i in range(20)]},
            'vi': {**source, 'complete': True, 'active': False, 'last_trigger_at': None,
                   'last_trigger_basis':'release_time_conservative_120s_not_trigger_assertion'},
            'price': {**source, 'current_price': 94, 'upper_limit_price': 100},
        }

    def check(self, value=None, now=None):
        return validate_admission(self.value if value is None else value, '005930', 'EXP_ORB_A', now or self.now)

    def test_positive_boundaries_and_freshness(self):
        self.assertTrue(self.check())
        v = copy.deepcopy(self.value)
        v['vi']['last_trigger_at'] = (self.now - timedelta(seconds=120)).isoformat()
        self.assertTrue(self.check(v))
        self.assertTrue(self.check(now=self.now + timedelta(seconds=5)))
        with self.assertRaises(ValueError): self.check(now=self.now + timedelta(seconds=5, microseconds=1))

    def test_each_source_has_independent_age_limit(self):
        for part, limit in [('instrument', 300), ('turnover', 36 * 3600), ('vi', 30), ('price', 5)]:
            v = copy.deepcopy(self.value)
            v[part]['available_at'] = (self.now - timedelta(seconds=limit)).isoformat()
            self.assertTrue(self.check(v))
            v[part]['available_at'] = (self.now - timedelta(seconds=limit, microseconds=1)).isoformat()
            with self.subTest(part=part), self.assertRaises(ValueError): self.check(v)

    def test_reject_scope_and_hash(self):
        for field, bad in [('symbol', '000660'), ('arm', 'EXP_D'), ('schema', 'other')]:
            v = copy.deepcopy(self.value); v[field] = bad
            with self.subTest(field=field), self.assertRaises(ValueError): self.check(v)
        for part in ('instrument', 'turnover', 'vi', 'price'):
            v = copy.deepcopy(self.value); v[part]['source_sha256'] = 'A' * 64
            with self.subTest(part=part), self.assertRaises(ValueError): self.check(v)

    def test_turnover_needs_20_strict_historical_distinct_rows(self):
        for mutation in ('short', 'duplicate', 'today', 'low', 'bool', 'incomplete'):
            v = copy.deepcopy(self.value); rows = v['turnover']['days']
            if mutation == 'short': rows.pop()
            if mutation == 'duplicate': rows[-1]['date'] = rows[-2]['date']
            if mutation == 'today': rows[-1]['date'] = '20261102'
            if mutation == 'low': rows[-1]['turnover_krw'] -= 1
            if mutation == 'bool': rows[-1]['turnover_krw'] = True
            if mutation == 'incomplete': v['turnover']['complete'] = False
            with self.subTest(mutation=mutation), self.assertRaises(ValueError): self.check(v)

    def test_vi_and_price_unsafe_states_rejected(self):
        for part, field, bad in [
            ('vi', 'active', True), ('vi', 'complete', False),
            ('vi', 'last_trigger_at', (self.now - timedelta(seconds=119)).isoformat()),
            ('price', 'current_price', 95), ('price', 'current_price', True),
            ('price', 'upper_limit_price', 0), ('instrument', 'common_share', False),
            ('instrument', 'status', 'unknown'),
        ]:
            v = copy.deepcopy(self.value); v[part][field] = bad
            with self.subTest(part=part, field=field), self.assertRaises(ValueError): self.check(v)

    def test_no_future_or_naive_observations(self):
        for bad in [(self.now + timedelta(microseconds=1)).isoformat(), '2026-11-02T10:00:00']:
            v = copy.deepcopy(self.value); v['price']['available_at'] = bad
            with self.assertRaises(ValueError): self.check(v)

    def test_ledger_gate_rechecks_scope_and_time(self):
        from kiwoom_autotrader.trading.execution_log import ExecutionTracker, ExecutionLogError
        evidence = {'source_kind':'broker_observation','config': {'experiment_extended_admission': self.value}}
        ExecutionTracker._assert_experiment_admission('paper','buy','005930','EXP_ORB_A',evidence,self.now)
        for mode, symbol, when in [('real','005930',self.now), ('paper','000660',self.now),
                                    ('paper','005930',self.now + timedelta(seconds=6))]:
            with self.subTest(mode=mode,symbol=symbol), self.assertRaises(ExecutionLogError):
                ExecutionTracker._assert_experiment_admission(mode,'buy',symbol,'EXP_ORB_A',evidence,when)
        synthetic={**evidence,'source_kind':'synthetic'}
        with self.assertRaises(ExecutionLogError):
            ExecutionTracker._assert_experiment_admission('paper','buy','005930','EXP_ORB_A',synthetic,self.now)

    def test_frozen_evidence_retains_admission_and_rejects_tampering(self):
        from kiwoom_autotrader.trading.decision_evidence import freeze_evidence, DecisionEvidenceError
        config = {'experiment_arm':'ORB-a', 'experiment_assignment_sha256':'b'*64,
                  'experiment_first_seen_at':self.stamp, 'experiment_signal_at':self.stamp,
                  'experiment_signal_sha256':'c'*64, 'experiment_profile_version':'paper-experiment-v1',
                  'experiment_extended_admission':self.value}
        evidence = {'strategy':'EXP_ORB_A', 'observed_at':self.stamp, 'evaluated_at':self.stamp,
                    'source_kind':'synthetic', 'config':config}
        frozen=freeze_evidence(evidence).as_dict()
        self.assertEqual(frozen['config']['experiment_extended_admission'],self.value)
        for field in ('symbol','arm'):
            invalid=copy.deepcopy(evidence)
            invalid['config']['experiment_extended_admission'][field]='invalid'
            with self.subTest(field=field),self.assertRaises(DecisionEvidenceError):freeze_evidence(invalid)

    def test_real_ledger_reserves_then_rechecks_expiring_admission(self):
        from unittest.mock import Mock
        import json
        from tests.test_multi_strategy_epoch_review import EpochIndependentReview
        from kiwoom_autotrader.trading.paper_policy import experiment_order_sizing
        from kiwoom_autotrader.trading.execution_log import ExecutionLogError
        case=EpochIndependentReview('test_complete_zero_broker_evidence_allows_new_generation_without_touching_old_history')
        case.setUp();self.addCleanup(case.doCleanups)
        case.settings.update(paper_experiment_enabled=True)
        proof=case.proof()
        sizing=experiment_order_sizing(case.settings,'paper',available_cash_krw=450000,price=1000,
            existing_principal_krw=0,pending_principal_krw=0)
        proof.update(schema_version=6,integrated_pending_clear=False,symbol_pending_clear=True,
            paper_experiment_sizing=sizing,symbol_cap_krw=20000,per_buy_cap_krw=10000,remaining_principal_krw=20000)
        account={'schema':'paper-account-snapshot-v1','mode':'paper','exchange':'KRX','holdings':[],
                 'cash':{'entr':'450000','ord_alow_amt':'450000'}}
        with case.tracker._db() as db:
            db.execute('INSERT OR REPLACE INTO account_balance_daily VALUES(?,?,?,?,?,?)',
                ('default','paper','20261030',proof['observed_at'],'broker_observation',json.dumps(account)))
        case.tracker._assert_position_capacity=Mock(return_value={})
        case.tracker._holdings=Mock();case.tracker._save_paper_balance_snapshot=Mock()
        admission=copy.deepcopy(self.value);stamp=case.now.isoformat();admission['observed_at']=stamp
        for part in ('instrument','turnover','vi','price'):admission[part]['available_at']=stamp
        for i,row in enumerate(admission['turnover']['days']):
            row['date']=(case.now.date()-timedelta(days=20-i)).strftime('%Y%m%d')
        config={'experiment_arm':'ORB-a','experiment_assignment_sha256':'b'*64,
            'experiment_first_seen_at':stamp,'experiment_signal_at':stamp,'experiment_signal_sha256':'c'*64,
            'experiment_profile_version':'paper-experiment-v1','experiment_extended_admission':admission}
        evidence={'strategy':'EXP_ORB_A','strategy_version':'paper-experiment-v1',
                  'observed_at':stamp,'evaluated_at':stamp,'source_kind':'broker_observation','config':config}
        ident=case.tracker.prepare_order('paper','buy','005930',10,'EXP_ORB_A',price=1000,
                                        evidence=evidence,reconciliation_evidence=proof)
        case.tracker.assert_multi_buy_dispatch('paper','005930',ident)
        case.now+=timedelta(seconds=6)
        with self.assertRaises(ExecutionLogError):case.tracker.assert_multi_buy_dispatch('paper','005930',ident)

if __name__ == '__main__': unittest.main()
