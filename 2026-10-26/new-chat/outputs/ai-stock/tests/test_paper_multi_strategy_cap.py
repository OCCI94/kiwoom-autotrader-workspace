"""Offline tests for cross-strategy cap and serialized paper intent evidence."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
import tempfile
import unittest
from zoneinfo import ZoneInfo

from kiwoom_autotrader.trading.execution_log import ExecutionLogError, ExecutionTracker
from kiwoom_autotrader.trading.paper_policy import _buy_cap, _symbol_cap
from kiwoom_autotrader.trading.paper_policy import capture_automatic_paper_guard, PaperAutomationPolicyError
from kiwoom_autotrader.trading.order_context import automated_orders
from unittest.mock import Mock, patch


class MultiStrategyCapTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.now = datetime(2026, 10, 30, 10, 0, tzinfo=ZoneInfo('Asia/Seoul'))
        self.settings = {'paper_multi_strategy_enabled': True, 'paper_automation_buy_cap': 3000000,
                         'paper_automation_symbol_cap': 3000000}
        self.tracker = ExecutionTracker(log_dir=Path(self.tmp.name), now=lambda: self.now,
                                        settings_loader=lambda: self.settings)
        with self.tracker._db():
            pass

    def evidence(self, tracker, *, quantity=100, price=1000, held=0, basis=0):
        observation_id=tracker.record_strategy_holding_observation('005930',held,0,basis,self.now,source_kind='broker_observation')
        return {'schema_version': 3, 'symbol': '005930', 'pre_order_held_qty': held,
                'pre_order_pur_amt_krw': basis, 'remaining_principal_krw': 1000000-basis,
                'order_quantity': quantity, 'limit_price_krw': price, 'observed_at': self.now.isoformat(),
                'holdings_source': 'broker_observation', 'symbol_cap_krw': 1000000,
                'per_buy_cap_krw': 1000000, 'multi_strategy_enabled': True,
                'symbol_ledger_sha256': tracker.symbol_order_watermark('paper', '005930'),
                'holding_observation_id':observation_id}

    def prepare(self, tracker, proof, strategy):
        return tracker.prepare_order('paper', 'buy', '005930', proof['order_quantity'], strategy,
                                     price=proof['limit_price_krw'], reconciliation_evidence=proof)

    def test_cap_is_hard_when_enabled_and_unchanged_when_off(self):
        self.assertEqual((_buy_cap(self.settings), _symbol_cap(self.settings)), (1000000, 1000000))
        self.settings['paper_multi_strategy_enabled'] = False
        self.assertEqual((_buy_cap(self.settings), _symbol_cap(self.settings)), (3000000, 3000000))

    def test_unknown_or_oversized_evidence_is_rejected(self):
        for proof in (None, self.evidence(self.tracker, quantity=1001),
                      self.evidence(self.tracker, held=500, basis=950000)):
            with self.subTest(proof=proof), self.assertRaises(ExecutionLogError):
                self.tracker.prepare_order('paper', 'buy', '005930', 100 if proof is None else proof['order_quantity'],
                    's2', price=1000, reconciliation_evidence=proof)

    def test_two_instances_cannot_use_same_preorder_snapshot(self):
        other = ExecutionTracker(log_dir=Path(self.tmp.name), now=lambda: self.now,
                                 settings_loader=lambda: self.settings)
        proof = self.evidence(self.tracker)
        def attempt(item):
            tracker, name = item
            try:
                return self.prepare(tracker, proof, name)
            except ExecutionLogError:
                return None
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(attempt, ((self.tracker, 's2'), (other, 's3'))))
        self.assertEqual(sum(result is not None for result in results), 1)

    def test_canceled_intent_also_invalidates_stale_snapshot(self):
        proof = self.evidence(self.tracker)
        intent = self.prepare(self.tracker, proof, 's2')
        self.tracker.cancel_intent(intent, 'not_sent')
        with self.assertRaises(ExecutionLogError):
            self.prepare(self.tracker, proof, 's3')

    def test_changed_policy_invalidates_evidence(self):
        proof = self.evidence(self.tracker)
        self.settings['paper_multi_strategy_enabled'] = False
        with self.assertRaises(ExecutionLogError):
            self.prepare(self.tracker, proof, 's2')

    def test_buy_dispatch_rechecks_policy_and_ownership(self):
        proof=self.evidence(self.tracker)
        ident=self.prepare(self.tracker,proof,'s2')
        self.tracker.assert_multi_buy_dispatch('paper','005930',ident)
        self.settings['paper_multi_strategy_enabled']=False
        with self.assertRaises(ExecutionLogError):
            self.tracker.assert_multi_buy_dispatch('paper','005930',ident)

    def test_unknown_existing_ownership_cannot_buy_even_when_under_cap(self):
        proof=self.evidence(self.tracker,held=1,basis=1000)
        with self.assertRaises(ExecutionLogError):
            self.prepare(self.tracker,proof,'s2')

    def test_non_broker_reconciliation_cannot_authorize_entry(self):
        proof=self.evidence(self.tracker)
        proof['holdings_source']='unknown'
        with self.assertRaises(ExecutionLogError):
            self.prepare(self.tracker,proof,'s2')

    def test_fake_or_mismatched_holding_observation_cannot_authorize_entry(self):
        proof=self.evidence(self.tracker)
        proof['holding_observation_id']='f'*32
        with self.assertRaises(ExecutionLogError):self.prepare(self.tracker,proof,'s2')
        proof=self.evidence(self.tracker)
        proof['pre_order_pur_amt_krw']=1;proof['remaining_principal_krw']=999999
        with self.assertRaises(ExecutionLogError):self.prepare(self.tracker,proof,'s2')

    def test_staged_strategy_cannot_bypass_missing_broker_reconciliation(self):
        session=Mock(mode='paper')
        with patch('kiwoom_autotrader.trading.paper_policy.load_settings',return_value=self.settings):
            for strategy in ('S2_surge_high_breakout','S3_upper_limit_continuation','S4_theme_and_catalyst_leader'):
                for side in ('buy','sell'):
                    with self.subTest(strategy=strategy,side=side), automated_orders(self.tracker,strategy):
                        guard=capture_automatic_paper_guard(session,side,'005930','1','1000' if side=='buy' else None)
                        with self.assertRaises(PaperAutomationPolicyError):guard.before_prepare()

    def test_staged_strategy_is_default_off_and_paper_only(self):
        self.settings['paper_multi_strategy_enabled']=False
        for mode in ('paper','real'):
            with self.subTest(mode=mode),self.assertRaises(ExecutionLogError):
                self.tracker.prepare_order(mode,'buy','005930',1,'S2_surge_high_breakout',price=1000)

    def test_s3_entry_requires_the_same_immutable_broker_and_ownership_proof(self):
        proof=self.evidence(self.tracker)
        intent=self.prepare(self.tracker,proof,'S3_upper_limit_continuation')
        self.tracker.assert_multi_buy_dispatch('paper','005930',intent)
