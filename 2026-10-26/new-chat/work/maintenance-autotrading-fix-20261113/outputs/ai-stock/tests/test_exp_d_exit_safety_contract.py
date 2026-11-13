"""Owned EXP_D exit-cycle identity and purpose proof: temporary data only."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from kiwoom_autotrader.api.session import KST
from kiwoom_autotrader.trading.decision_evidence import freeze_evidence, DecisionEvidenceError
from kiwoom_autotrader.trading.paper_experiment_rules import select_overnight
from kiwoom_autotrader.trading.paper_experiment_store import ExperimentStore, canonical, exp_d_exit_key
from kiwoom_autotrader.trading.paper_reconciliation import (
    _assert_no_broker_pending, _assert_d_pending_pages, PaperReconciliationError)


ENTRY = datetime(2026, 11, 4, 15, 30, 12, tzinfo=KST)
NOW = datetime(2026, 11, 5, 9, 20, 3, tzinfo=KST)
GEN = 'a' * 32


def assignment():
    return select_overnight([{'symbol': '293490', 'first_seen_at': ENTRY.replace(hour=15, minute=0).isoformat()}],
        day='20261104', snapshot_at=ENTRY.replace(hour=15, minute=18))['assignments'][0]


def result(now=NOW, reason='late_recovery_exit'):
    return {'signal': True, 'action': 'sell', 'reason': reason, 'signal_at': now.isoformat()}


def evidence(now=NOW, reason='late_recovery_exit'):
    a = assignment(); start = now.replace(hour=9, minute=0, second=0, microsecond=0)
    end = now.replace(hour=15, minute=30, second=0, microsecond=0)
    proof = {'schema': 'exp-d-owned-exit-v1', 'kind': reason, 'generation_id': GEN,
        'first_fill_at': ENTRY.isoformat(), 'signal_at': now.isoformat(), 'session_day': '20261105',
        'regular_start': start.isoformat(), 'regular_end': end.isoformat(),
        'calendar_source_sha256': 'b' * 64, 'observed_session_open_at': start.isoformat()}
    return {'strategy': 'EXP_D', 'strategy_version': 'paper-experiment-v1', 'run_id': 'offline-exp-d-exit',
        'source_kind': 'broker_observation', 'source_provider': 'kiwoom', 'source_exchange': 'KRX',
        'source_api': 'ka10001', 'source_sha256': 'f' * 64,
        'observed_at': now.isoformat(), 'evaluated_at': now.isoformat(),
        'config': {'enabled': True, 'experiment_arm': 'D', 'experiment_profile_version': 'paper-experiment-v1',
            'experiment_assignment_sha256': a['assignment_sha256'], 'experiment_first_seen_at': a['first_seen_at'],
            'experiment_signal_at': now.isoformat(), 'experiment_signal_sha256': 'e' * 64,
            'experiment_order_window_end_at': (start + timedelta(minutes=1) if reason == 'next_session_open' else end).isoformat(),
            'experiment_exit_reason': reason, 'multi_exit_kind': 'full', 'multi_position_generation_id': GEN,
            'experiment_assignment': a, 'experiment_d_exit': proof}}


class ExitCycleStoreTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / 'assignments.sqlite3'; self.store = ExperimentStore(self.path)
        self.a = assignment()

    def status(self):
        return self.store.exp_d_exit_status(self.a, GEN, now=NOW, entry_at=ENTRY)

    def test_normal_and_late_share_one_generation_even_after_restart(self):
        opening = NOW.replace(hour=9, minute=0)
        self.assertTrue(self.store.claim_exp_d_exit(self.a, GEN, now=opening, entry_at=ENTRY,
            payload=result(opening, 'next_session_open')))
        self.store = ExperimentStore(self.path)
        self.assertEqual(self.status()['status'], 'consumed')
        self.assertFalse(self.store.claim_exp_d_exit(self.a, GEN, now=NOW, entry_at=ENTRY, payload=result()))
        with self.store._db() as db:
            row = db.execute('SELECT * FROM attempts').fetchone()
        self.assertEqual(row['signal_key'], exp_d_exit_key(self.a['assignment_sha256'], GEN))
        self.assertEqual(json.loads(row['payload'])['exp_d_exit_cycle']['generation_id'], GEN)

    def test_accepted_uncertain_missing_and_not_submitted_outcomes_never_release(self):
        for outcome in (None, 'accepted', 'uncertain', 'not_submitted'):
            with self.subTest(outcome=outcome), tempfile.TemporaryDirectory() as tmp:
                store = ExperimentStore(Path(tmp) / 'attempts.sqlite3')
                self.assertTrue(store.claim_exp_d_exit(self.a, GEN, now=NOW, entry_at=ENTRY, payload=result()))
                if outcome is not None:
                    store.outcome(exp_d_exit_key(self.a['assignment_sha256'], GEN), NOW, {'status': outcome})
                self.assertFalse(store.claim_exp_d_exit(self.a, GEN, now=NOW+timedelta(minutes=1),
                    entry_at=ENTRY, payload=result(NOW+timedelta(minutes=1))))

    def legacy(self, *, generation=GEN, payload=None):
        opening = NOW.replace(hour=9, minute=0, second=0)
        payload = payload or result(opening, 'next_session_open')
        key = hashlib.sha256(canonical([self.a['assignment_sha256'], 'sell', payload['signal_at'],
            'next_session_open', generation]).encode()).hexdigest()
        self.store.claim(key, day='20261105', symbol='293490', arm='EXP_D', side='sell',
            now=opening+timedelta(seconds=5), payload=payload)
        return key

    def test_legacy_key_is_recomputed_with_exact_original_generation(self):
        key = self.legacy()
        self.store.outcome(key, NOW, {'status': 'not_submitted'})
        status = self.status()
        self.assertEqual(status['status'], 'consumed'); self.assertTrue(status['legacy_attempt'])
        self.assertEqual(status['matched_key'], key)
        self.assertFalse(self.store.claim_exp_d_exit(self.a, GEN, now=NOW, entry_at=ENTRY, payload=result()))

    def test_unbound_legacy_same_symbol_is_held_not_assumed_safe(self):
        self.legacy(generation='c' * 32)
        self.assertEqual(self.status()['status'], 'unavailable')
        self.assertFalse(self.store.claim_exp_d_exit(self.a, GEN, now=NOW, entry_at=ENTRY, payload=result()))

    def test_prior_exit_before_this_fill_does_not_consume_new_lot(self):
        self.store.claim('c' * 64, day='20261104', symbol='293490', arm='EXP_D', side='sell',
            now=ENTRY.replace(hour=9, minute=0), payload={'unrelated_previous_generation': True})
        self.assertEqual(self.status()['status'], 'available')

    def test_modified_binding_or_future_claim_fails_closed(self):
        self.assertTrue(self.store.claim_exp_d_exit(self.a, GEN, now=NOW, entry_at=ENTRY, payload=result()))
        with self.store._db() as db:
            row = db.execute('SELECT payload FROM attempts').fetchone(); payload = json.loads(row[0])
            payload['exp_d_exit_cycle']['generation_id'] = 'd' * 32
            db.execute('UPDATE attempts SET payload=?', (canonical(payload),))
        self.assertEqual(self.status()['status'], 'unavailable')

    def test_attempt_history_overflow_is_not_treated_as_no_attempt(self):
        with self.store._db() as db:
            db.executemany('INSERT INTO attempts VALUES(?,?,?,?,?,?,?)', [
                (hashlib.sha256(str(i).encode()).hexdigest(), '20261105', '293490', 'EXP_D', 'sell', NOW.isoformat(), '{}')
                for i in range(1001)])
        self.assertEqual(self.status()['reason'], 'exp_d_exit_attempt_history_overflow')

    def test_changed_assignment_and_invalid_generation_do_not_claim(self):
        changed = dict(self.a, symbol='068270')
        self.assertEqual(self.store.exp_d_exit_status(changed, GEN, now=NOW, entry_at=ENTRY)['status'], 'unavailable')
        with self.assertRaises(ValueError):
            self.store.claim_exp_d_exit(self.a, 'not-a-generation', now=NOW, entry_at=ENTRY, payload=result())
        with self.store._db() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM attempts').fetchone()[0], 0)

    def test_atomic_claim_allows_only_one_of_two_contending_stores(self):
        other = ExperimentStore(self.path)
        def claim(store):
            return store.claim_exp_d_exit(self.a, GEN, now=NOW, entry_at=ENTRY, payload=result())
        with ThreadPoolExecutor(max_workers=2) as pool:
            values = list(pool.map(claim, (self.store, other)))
        self.assertEqual(sorted(values), [False, True])


class ExitPurposeEvidenceTests(unittest.TestCase):
    def test_late_proof_preserves_original_assignment_and_actual_signal_time(self):
        original = evidence(); frozen = freeze_evidence(original)
        self.assertEqual(frozen.as_dict()['strategy'], 'EXP_D')
        self.assertEqual(frozen.as_dict()['config']['experiment_assignment'], assignment())
        self.assertEqual(frozen.as_dict()['config']['experiment_signal_at'], NOW.isoformat())
        self.assertEqual(original['config']['experiment_d_exit']['observed_session_open_at'],
            NOW.replace(hour=9, minute=0, second=0).isoformat())

    def test_normal_proof_is_bound_to_actual_first_minute(self):
        now = NOW.replace(hour=9, minute=0, second=33)
        self.assertIsNotNone(freeze_evidence(evidence(now, 'next_session_open')))
        with self.assertRaises(DecisionEvidenceError):
            freeze_evidence(evidence(NOW, 'next_session_open'))

    def test_normal_purpose_cannot_extend_opening_deadline_to_late_window(self):
        value = evidence(NOW.replace(hour=9, minute=0, second=33), 'next_session_open')
        value['config']['experiment_order_window_end_at'] = value['config']['experiment_d_exit']['regular_end']
        with self.assertRaises(DecisionEvidenceError):
            freeze_evidence(value)

    def test_legacy_normal_evidence_without_new_purpose_remains_readable(self):
        value = evidence(NOW.replace(hour=9, minute=0, second=33), 'next_session_open')
        value['config'].pop('experiment_d_exit'); value['config'].pop('experiment_assignment')
        self.assertIsNotNone(freeze_evidence(value))

    def test_missing_proof_generation_assignment_calendar_or_backdated_late_is_rejected(self):
        mutations = [
            lambda v: v['config'].pop('experiment_d_exit'),
            lambda v: v['config']['experiment_d_exit'].update(generation_id='c' * 32),
            lambda v: v['config']['experiment_assignment'].update(symbol='068270'),
            lambda v: v['config']['experiment_d_exit'].update(calendar_source_sha256='missing'),
            lambda v: v['config']['experiment_d_exit'].update(observed_session_open_at=ENTRY.isoformat()),
            lambda v: v['config']['experiment_d_exit'].update(first_fill_at=NOW.isoformat()),
            lambda v: v.update(strategy='manual_protective_exit'),
            lambda v: v['config']['experiment_d_exit'].update(session_day='20261104'),
            lambda v: v['config']['experiment_d_exit'].update(extra_field='untrusted'),
        ]
        for mutate in mutations:
            value = deepcopy(evidence()); mutate(value)
            with self.subTest(mutate=mutate), self.assertRaises(DecisionEvidenceError):
                freeze_evidence(value)
        value = evidence(NOW.replace(hour=9, minute=0, second=33))
        with self.assertRaises(DecisionEvidenceError):freeze_evidence(value)

    def test_all_venues_pending_same_symbol_blocks_without_blocking_other_experiment_symbol(self):
        row = {'stk_cd': '293490', 'ord_no': '123', 'oso_qty': '1', 'stex_tp': '2'}
        with self.assertRaises(PaperReconciliationError):
            _assert_no_broker_pending([row], '293490', all_venues=True)
        _assert_no_broker_pending([dict(row, stk_cd='068270')], '293490', all_venues=True)
        with self.assertRaises(PaperReconciliationError):
            _assert_no_broker_pending([dict(row, oso_qty='unknown')], '293490', all_venues=True)


class IntegratedPendingProofTests(unittest.TestCase):
    def page(self):
        return {'schema': 'kiwoom-broker-page-v1', 'source_api': 'ka10075', 'source_kind': 'broker_observation',
            'mode': 'paper', 'day': '20261105', 'query_id': 'a'*32, 'page': 1,
            'request_started_at': NOW.isoformat(), 'observed_at': NOW.isoformat(),
            'request_body': {'all_stk_tp': '0', 'trde_tp': '0', 'stex_tp': '0'},
            'response_body': {'return_code': 0, 'oso': []},
            'continuation': {'request_cont_yn': 'N', 'response_cont_yn': 'N', 'next_key_present': False},
            'status': 'success', 'http_status': 200}

    def check(self, pages, rows=()):
        _assert_d_pending_pages(pages, rows, '20261105', NOW, NOW+timedelta(seconds=1))

    def test_empty_rows_require_complete_page_and_integrated_request_scope(self):
        self.check([self.page()])
        for mutate in (
            lambda p: p['continuation'].update(response_cont_yn='Y', next_key_present=True),
            lambda p: p['request_body'].update(stex_tp='1'),
            lambda p: p.update(status='transport_error'),
        ):
            value=self.page(); mutate(value)
            with self.subTest(mutate=mutate), self.assertRaises(PaperReconciliationError):
                self.check([value])

    def test_pages_must_follow_account_snapshot_in_same_day_mode_source(self):
        for mutate in (
            lambda p: p.update(request_started_at=(NOW-timedelta(seconds=1)).isoformat()),
            lambda p: p.update(observed_at=(NOW+timedelta(seconds=2)).isoformat()),
            lambda p: p.update(day='20261104'),
            lambda p: p.update(mode='real'),
            lambda p: p.update(source_kind='synthetic'),
        ):
            value=self.page(); mutate(value)
            with self.subTest(mutate=mutate), self.assertRaises(PaperReconciliationError):
                self.check([value])

    def test_complete_multiple_pages_have_one_query_and_exact_continuation(self):
        first=self.page(); first['continuation'].update(response_cont_yn='Y',next_key_present=True)
        second=self.page(); second['page']=2; second['continuation']['request_cont_yn']='Y'
        self.check([first,second])
        second['query_id']='b'*32
        with self.assertRaises(PaperReconciliationError):self.check([first,second])
        second['query_id']='a'*32;second['page']=3
        with self.assertRaises(PaperReconciliationError):self.check([first,second])

    def test_returned_rows_must_match_observed_economic_rows_without_private_fields(self):
        value=self.page(); row={'stk_cd':'068270','ord_no':'123','oso_qty':'1','stex_tp':'2'}
        value['response_body']['oso']=[row]
        self.check([value],[dict(row,private_field='offline-placeholder')])
        with self.assertRaises(PaperReconciliationError):self.check([value],[])
        with self.assertRaises(PaperReconciliationError):self.check([value],[dict(row,oso_qty='0')])


if __name__ == '__main__':
    unittest.main()
