"""Offline official-business overlay and account-wide distinct theme slots."""
from copy import deepcopy
from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path
import tempfile
from unittest import TestCase
from zoneinfo import ZoneInfo

from kiwoom_autotrader.api.orders import OrderReceipt
from kiwoom_autotrader.trading.execution_log import (
    ExecutionTracker, ThemeConcentrationLimitReached, ThemeConcentrationUnavailable)
from kiwoom_autotrader.trading.theme_concentration import (
    SUPPLEMENT_FILENAME, funnel_theme_reason, theme_family_snapshot, theme_occupancy)


class ThemeSupplementTests(TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.now = datetime(2026, 11, 1, 12, 0, tzinfo=ZoneInfo('Asia/Seoul'))
        self.taxonomy = self.root/'taxonomy.json'
        self.tax = {'schema': 'pro6e-sector-taxonomy-v1', 'metadata_only': True,
                    'created_at': '2026-10-30T09:00:00+09:00', 'kr_stocks': {
                        '042510': {'sub_themes': ['보안·인증']},
                        '053800': {'sub_themes': ['보안·사이버']},
                        '434480': {'sub_themes': ['보안·클라우드']},
                        '128940': {'sub_themes': ['제약']}}}
        self.taxonomy.write_text(json.dumps(self.tax), encoding='utf-8')
        self.bundled = (Path(__file__).resolve().parents[1]/
                        'kiwoom_autotrader/config/data'/SUPPLEMENT_FILENAME)
        self.supplement = json.loads(self.bundled.read_text(encoding='utf-8'))
        self.target = self.root/SUPPLEMENT_FILENAME
        self.save_supplement()
        self.options = {'max_positions_per_theme': 2,
                        'paper_theme_unclassified_policy': 'block'}
        self.tracker = ExecutionTracker(log_dir=self.root, now=lambda: self.now,
            settings_loader=lambda: self.options, theme_taxonomy_path=self.taxonomy)
        self.balance([])

    def save_supplement(self):
        self.target.write_text(json.dumps(self.supplement, ensure_ascii=False), encoding='utf-8')

    def balance(self, symbols):
        payload = {'schema': 'paper-account-snapshot-v1', 'mode': 'paper', 'exchange': 'KRX',
                   'cash': {'entr': '10000000', 'ord_alow_amt': '10000000'},
                   'holdings': [{'stk_cd': 'A'+code, 'rmnd_qty': '1'} for code in symbols]}
        with self.tracker._db() as db:
            db.execute('INSERT OR REPLACE INTO account_balance_daily VALUES(?,?,?,?,?,?)',
                (self.tracker.scope, 'paper', self.now.strftime('%Y%m%d'), self.now.isoformat(),
                 'broker_observation', json.dumps(payload)))

    def prepare(self, code, strategy='gdcrs'):
        return self.tracker.prepare_order('paper', 'buy', code, 1, strategy, price=1000)

    def test_verified_five_mappings_have_separate_provenance_and_original_bytes_unchanged(self):
        before = self.taxonomy.read_bytes()
        snapshot = theme_family_snapshot([], self.now, path=self.taxonomy)
        expected = {'002790': ['화장품'], '003220': ['제약'], '003610': ['섬유'],
                    '010660': ['기계', '자동차'], '053350': ['보안']}
        self.assertEqual({code: snapshot['themes_by_symbol'][code] for code in expected}, expected)
        evidence = snapshot['supplement_evidence']
        self.assertEqual(evidence['applied_symbols'], sorted(expected))
        self.assertEqual(evidence['source_sha256'], hashlib.sha256(self.target.read_bytes()).hexdigest())
        self.assertEqual(set(evidence['provenance_by_symbol']), set(expected))
        self.assertEqual(snapshot['taxonomy_evidence']['source_sha256'], hashlib.sha256(before).hexdigest())
        self.assertEqual(self.taxonomy.read_bytes(), before)
        self.assertFalse(self.supplement['order_universe_authorization'])

    def test_both_security_candidates_fit_when_empty_and_third_is_blocked(self):
        # Theme admission does not bypass the separate account-wide pending gate.
        self.assertIsNotNone(self.tracker.paper_position_capacity('042510', self.now))
        self.assertIsNotNone(self.tracker.paper_position_capacity('053800', self.now))
        first = self.prepare('042510')
        self.tracker.assert_paper_theme_dispatch('paper', '042510', first)
        self.assertIsNotNone(self.tracker.paper_position_capacity('053800', self.now))
        self.tracker.cancel_intent(first, 'not_sent')
        self.balance(['042510'])
        second = self.prepare('053800', 's2_surge_high_breakout')
        self.assertTrue(first and second)
        self.tracker.assert_paper_theme_dispatch('paper', '053800', second)
        with self.assertRaises(ThemeConcentrationLimitReached):
            self.prepare('053350', 's4_theme_catalyst_leader')
        snapshot = self.tracker.paper_theme_capacity_snapshot(self.now)
        self.assertEqual(funnel_theme_reason('053350', snapshot), 'theme_concentration_limit_reached')

    def test_same_held_pending_and_intent_symbol_counts_once_across_strategies(self):
        first = self.prepare('053800', 's2_surge_high_breakout')
        self.tracker.register_order(OrderReceipt('paper', 'buy', '053800', '1', '1000', '100',
            'https://mockapi.kiwoom.com'), 's2_surge_high_breakout', intent_id=first)
        # Partial/existing holding and pending broker order share one symbol slot.
        self.balance(['053800'])
        # Model a pre-existing different-strategy reservation in this temporary
        # ledger. Production prepare_order correctly refuses another buy while
        # the first broker order is pending; that guard remains unchanged.
        second = 'isolated-existing-intent'
        with self.tracker._db() as db:
            db.execute('INSERT INTO intents(scope,id,day,mode,side,symbol,quantity,strategy,created_at) '
                       'VALUES(?,?,?,?,?,?,?,?,?)', (self.tracker.scope, second,
                       self.now.strftime('%Y%m%d'), 'paper', 'buy', '042510', 1,
                       'legacy_other_strategy', self.now.isoformat()))
        self.tracker.assert_paper_theme_dispatch('paper', '042510', second)
        proof = self.tracker.paper_position_capacity('053800', self.now)
        self.assertEqual(proof['occupied_symbols'], ['042510', '053800'])
        snapshot = self.tracker.paper_theme_capacity_snapshot(self.now)
        self.assertIsNone(funnel_theme_reason('053800', snapshot))
        self.assertEqual(funnel_theme_reason('053350', snapshot), 'theme_concentration_limit_reached')
        with self.assertRaises(ThemeConcentrationLimitReached):
            self.prepare('053350')

    def test_classified_supplement_does_not_admit_unverified_others(self):
        self.assertTrue(self.prepare('003220'))
        with self.assertRaises(ThemeConcentrationUnavailable):
            self.prepare('999999')
        self.assertEqual(funnel_theme_reason('999999', self.tracker.paper_theme_capacity_snapshot(self.now)),
                         'theme_membership_unavailable')

    def test_original_valid_membership_is_never_overridden(self):
        self.tax['kr_stocks']['053350'] = {'sub_themes': ['original·family']}
        self.taxonomy.write_text(json.dumps(self.tax))
        result = theme_family_snapshot([], self.now, path=self.taxonomy)
        self.assertEqual(result['themes_by_symbol']['053350'], ['original'])
        self.assertIn('053350', result['supplement_evidence']['original_taxonomy_retained_symbols'])
        self.assertNotIn('053350', result['supplement_evidence']['applied_symbols'])

    def test_original_malformed_membership_is_not_silently_healed(self):
        for bad in (None, {'sub_themes': None}, {'sub_themes': ['']}, {'sub_themes': ['·bad']}):
            with self.subTest(bad=bad):
                self.tax['kr_stocks']['053350'] = bad
                self.taxonomy.write_text(json.dumps(self.tax))
                result = theme_family_snapshot([], self.now, path=self.taxonomy)
                self.assertIn('053350', result['invalid_theme_symbols'])
                with self.assertRaises(ValueError):
                    theme_occupancy('053350', [], self.now, path=self.taxonomy)

    def test_explicit_empty_membership_can_be_supplemented(self):
        self.tax['kr_stocks']['053350'] = {'sub_themes': []}
        self.taxonomy.write_text(json.dumps(self.tax))
        self.assertEqual(theme_occupancy('053350', [], self.now, path=self.taxonomy)['theme_families'], ['보안'])

    def test_absent_file_keeps_legacy_and_unknown_block(self):
        self.target.unlink()
        result = theme_family_snapshot([], self.now, path=self.taxonomy)
        self.assertIsNone(result['supplement_evidence'])
        self.assertEqual(result['themes_by_symbol']['053800'], ['보안'])
        with self.assertRaises(ValueError):
            theme_occupancy('053350', [], self.now, path=self.taxonomy)

    def test_future_and_unaware_provenance_are_blocked(self):
        original = deepcopy(self.supplement)
        for field, bad in [('created_at', (self.now+timedelta(seconds=1)).isoformat()),
                           ('created_at', '2026-11-01T10:00:00'),
                           ('verified_at', (self.now+timedelta(seconds=1)).isoformat()),
                           ('verified_at', '2026-11-01T10:00:00')]:
            with self.subTest(field=field, bad=bad):
                self.supplement = deepcopy(original)
                if field == 'created_at': self.supplement[field] = bad
                else: self.supplement['stocks']['053350']['sources'][0][field] = bad
                self.save_supplement()
                with self.assertRaisesRegex(ValueError, 'theme_supplement_unavailable'):
                    theme_family_snapshot([], self.now, path=self.taxonomy)

    def test_invalid_provenance_is_fail_closed(self):
        original = deepcopy(self.supplement)
        mutations = [lambda d: d.update(metadata_only=False),
                     lambda d: d.update(order_universe_authorization=True),
                     lambda d: d['stocks']['053350'].update(sources=[]),
                     lambda d: d['stocks']['053350'].update(review_method='guessed'),
                     lambda d: d['stocks']['053350']['sources'][0].update(url='https://example.org/fake'),
                     lambda d: d['stocks']['053350']['sources'][0].update(url='http://kind.krx.co.kr/fake'),
                     lambda d: d['stocks']['053350']['sources'][0].update(url='https://user:pass@kind.krx.co.kr/fake')]
        for index, mutate in enumerate(mutations):
            with self.subTest(index=index):
                self.supplement = deepcopy(original)
                mutate(self.supplement); self.save_supplement()
                with self.assertRaisesRegex(ValueError, 'theme_supplement_unavailable'):
                    theme_family_snapshot([], self.now, path=self.taxonomy)

    def test_duplicate_keys_or_oversized_file_are_rejected(self):
        for payload in ('{"stocks":{},"stocks":{}}', ' '*65537, '{}', '[]'):
            with self.subTest(length=len(payload)):
                self.target.write_text(payload)
                with self.assertRaisesRegex(ValueError, 'theme_supplement_unavailable'):
                    theme_family_snapshot([], self.now, path=self.taxonomy)

    def test_both_real_business_families_count(self):
        result = theme_occupancy('010660', ['010660', '053800'], self.now, path=self.taxonomy)
        self.assertEqual(result['occupied_by_theme'], {'기계': ['010660'], '자동차': ['010660']})
