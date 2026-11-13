"""Offline same-owner B3 reservation and queue-dispatch safety regressions."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

from kiwoom_autotrader.api.executions import Execution
from kiwoom_autotrader.api.minute_chart import MinuteBar
from kiwoom_autotrader.api.orders import OrderReceipt
from kiwoom_autotrader.trading.execution_log import ExecutionTracker, ExecutionLogError, DailyEntryLimitReached
from kiwoom_autotrader.trading.multi_b3 import initial_config, evaluate_add_on
from kiwoom_autotrader.trading.multi_strategy import S3, EntryState
from kiwoom_autotrader.trading.multi_strategy_evidence import multi_strategy_evidence


class MultiB3LedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.now = datetime(2026, 10, 30, 11, 29, 1, tzinfo=ZoneInfo('Asia/Seoul'))
        self.settings = {'paper_multi_strategy_enabled': True, 'paper_multi_strategy_ids': [S3],
            'gdcrs_initial_entry_pct': 50, 'gdcrs_add_on_profit_bps': 50,
            'gdcrs_stocks': [{'code': '005930', 'budget': 1_000_000}]}
        self.tracker = self.new_tracker()
        self.cfg = initial_config(self.settings, '005930', 500000)
        self.event('stream_started', self.now-timedelta(seconds=1), {})
        initial = self.evidence_initial()
        self.initial = self.tracker.prepare_order('paper', 'buy', '005930', 100, S3, price=100,
            evidence=initial, reconciliation_evidence=self.recon(0, 0, 100, 100))
        self.tracker.register_order(OrderReceipt('paper', 'buy', '005930', '100', '100', '24',
            'https://mockapi.kiwoom.com'), S3, intent_id=self.initial)
        self.now = self.now.replace(second=20)
        self.filled_at = self.now
        self.event('execution_stream', self.now, {'9203': '24', '9001': 'A005930', '907': '2',
            '913': '체결', '908': '112920', '909': '1', '914': '100', '915': '100', '911': '100', '903': '10000'})
        self.tracker._ingest('paper', '20261030', (
            Execution('20261030', '24', '1', '005930', 'fixture', 'buy', '11:29:20', Decimal('100'), 100),))
        self.now = self.now.replace(minute=31, second=1)

    def new_tracker(self):
        return ExecutionTracker(log_dir=Path(self.tmp.name), now=lambda: self.now,
            settings_loader=lambda: dict(self.settings), account_fetcher=Mock(),
            executions_fetcher=Mock(), profit_fetcher=Mock())

    def event(self, kind, at, values):
        self.tracker.record_broker_observation({'schema_version': 1, 'source_api': '00',
            'source_kind': 'broker_observation', 'observed_at': at.isoformat(), 'day': '20261030',
            'mode': 'paper', 'event_kind': kind, 'values': values})

    def bars(self):
        return [MinuteBar(self.now.replace(second=0)-timedelta(minutes=21-i), 100, 150 if i==20 else 100,
            source_symbol='005930', source_mode='paper', adjustment='', adjustment_rate='',
            source_batch='test', source_page=0, source_row=20-i, source_response_sha256='a'*64,
            source_query_started_at=self.now, open_price=100, high_price=101, low_price=99) for i in range(21)]

    def market(self, price=101):
        return {'symbol': '005930', 'current_price': price, 'day_high': 101,
            'upper_limit_price': 130, 'change_pct': '20.1', 'request_started_at': self.now.isoformat(),
            'available_at': self.now.isoformat(), 'observed_at': self.now.isoformat(),
            'source': 'ka10001_normalized_response', 'source_sha256': 'b'*64,
            'source_market_timestamp': None, 'source_freshness_verified': False}

    def evidence_initial(self):
        baseline = EntryState(self.now.replace(second=0)-timedelta(minutes=2), 101, False, Decimal('19.9'))
        return multi_strategy_evidence(strategy=S3, symbol='005930', side='buy',
            reason='observed_20pct_crossing', budget=500000, position={'status': 'flat'},
            bars=self.bars(), market=self.market(100), observed_at=self.now, baseline=baseline,
            b3_config=self.cfg, source_kind='synthetic')

    def recon(self, held, principal, qty, price):
        ident = self.tracker.record_strategy_holding_observation('005930', held, held, principal,
            self.now, source_kind='broker_observation')
        return {'schema_version': 3, 'symbol': '005930', 'pre_order_held_qty': held,
            'pre_order_pur_amt_krw': principal, 'remaining_principal_krw': max(0, 1000000-principal),
            'order_quantity': qty, 'limit_price_krw': price, 'observed_at': self.now.isoformat(),
            'holdings_source': 'broker_observation', 'symbol_cap_krw': 1000000, 'per_buy_cap_krw': 1000000,
            'multi_strategy_enabled': True, 'holding_observation_id': ident,
            'symbol_ledger_sha256': self.tracker.symbol_order_watermark('paper', '005930')}

    def candidate(self, tracker=None):
        return (tracker or self.tracker).multi_b3_add_candidate('paper', '005930', S3, self.now)

    def evidence_add(self):
        position = self.tracker.multi_strategy_position('paper', '005930', S3, self.now)
        result = evaluate_add_on(self.candidate(), bars=self.bars(), market=self.market(),
            position=position, now=self.now, symbol='005930', settings=self.settings)
        self.assertTrue(result['eligible'], result)
        return multi_strategy_evidence(strategy=S3, symbol='005930', side='buy', reason='add_on_strength',
            budget=result['budget'], position=position, bars=self.bars(), market=self.market(),
            observed_at=self.now, b3_config=result['config'], b3_add_on=result['addition'], source_kind='synthetic')

    def prepare_add(self, *, tracker=None, evidence=None, proof=None, qty=100, price=101):
        return (tracker or self.tracker).prepare_order('paper', 'buy', '005930', qty, S3, price=price,
            evidence=evidence or self.evidence_add(), reconciliation_evidence=proof or self.recon(100, 10000, qty, price))

    def test_exact_initial_lot_and_one_same_owner_add_dispatch(self):
        candidate = self.candidate()
        self.assertEqual((candidate['initial_intent_id'], candidate['initial_filled_qty'],
                          candidate['initial_filled_principal_krw']), (self.initial, 100, 10000))
        self.assertEqual(candidate['initial_fill_observed_at'], self.filled_at.isoformat())
        ident = self.prepare_add()
        self.tracker.assert_multi_buy_dispatch('paper', '005930', ident)
        self.assertIsNone(self.candidate())

    def test_fully_filled_add_remains_same_owner_and_cannot_add_again(self):
        ident = self.prepare_add()
        self.tracker.register_order(OrderReceipt('paper', 'buy', '005930', '100', '101', '25',
            'https://mockapi.kiwoom.com'), S3, intent_id=ident)
        self.event('execution_stream', self.now, {'9203': '25', '9001': 'A005930', '907': '2',
            '913': '체결', '908': '113101', '909': '2', '914': '101', '915': '100', '911': '100', '903': '10100'})
        self.tracker._ingest('paper', '20261030', (
            Execution('20261030', '24', '1', '005930', 'fixture', 'buy', '11:29:20', Decimal('100'), 100),
            Execution('20261030', '25', '2', '005930', 'fixture', 'buy', '11:31:01', Decimal('101'), 100)))
        position = self.tracker.multi_strategy_position('paper', '005930', S3, self.now)
        self.assertEqual((position['status'], position['owned_qty'], position['basis_krw']), ('owned', 200, 20100))
        self.assertEqual(position['generation_id'], self.initial)
        self.assertIsNone(self.candidate())

    def test_claim_survives_not_sent_and_new_tracker(self):
        ident = self.prepare_add(); self.tracker.cancel_intent(ident, 'not_sent')
        self.assertIsNone(self.candidate(self.new_tracker()))
        with sqlite3.connect(self.tracker.path) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM b3_add_claims').fetchone()[0], 1)
            with self.assertRaises(sqlite3.IntegrityError): db.execute('DELETE FROM b3_add_claims')

    def test_atomic_race_claims_initial_only_once(self):
        evidence, proof = self.evidence_add(), self.recon(100, 10000, 100, 101)
        def run(tracker):
            try: return self.prepare_add(tracker=tracker, evidence=evidence, proof=proof)
            except (ExecutionLogError, sqlite3.Error): return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            values = list(pool.map(run, (self.tracker, self.new_tracker())))
        self.assertEqual(sum(value is not None for value in values), 1)

    def test_latest_limit_price_loss_holdings_mismatch_or_extra_half_budget_blocked(self):
        evidence = self.evidence_add()
        for qty, price, held, basis in ((100, 100, 100, 10000), (100, 101, 99, 9900),
                                      (100, 101, 100, 10001), (4951, 101, 100, 10000)):
            with self.subTest(qty=qty, price=price, held=held, basis=basis), self.assertRaises(ExecutionLogError):
                self.prepare_add(evidence=evidence, qty=qty, price=price, proof=self.recon(held, basis, qty, price))

    def test_negative_trend_invalidation_stays_and_blocks_reserved_dispatch(self):
        ident = self.prepare_add()
        self.tracker.multi_b3_invalidate_candidate('paper', '005930', S3, self.initial, self.now)
        self.assertIsNone(self.candidate())
        with self.assertRaises(ExecutionLogError): self.tracker.assert_multi_buy_dispatch('paper', '005930', ident)
        with sqlite3.connect(self.tracker.path) as db:
            with self.assertRaises(sqlite3.IntegrityError): db.execute('DELETE FROM b3_invalidations')

    def test_stream_gap_does_not_infer_safe_continuation(self):
        ident = self.prepare_add()
        self.event('stream_stopped', self.now, {})
        with self.assertRaises(ExecutionLogError): self.tracker.assert_multi_buy_dispatch('paper', '005930', ident)
        self.tracker.cancel_intent(ident, 'not_sent')
        self.assertIsNone(self.candidate())

    def test_overnight_lot_is_not_same_day_scale_in(self):
        self.assertIsNotNone(self.candidate())
        self.now += timedelta(days=1)
        self.assertIsNone(self.candidate())

    def test_pending_exit_closes_add_opportunity_and_not_sent_preserves_owner(self):
        holding = self.tracker.record_strategy_holding_observation('005930', 100, 100, 10000,
            self.now, source_kind='broker_observation')
        proof = {'schema_version': 4, 'symbol': '005930', 'pre_order_held_qty': 100,
            'pre_order_pur_amt_krw': 10000, 'order_quantity': 100, 'observed_at': self.now.isoformat(),
            'holdings_source': 'broker_observation', 'generation_id': self.initial, 'exit_kind': 'full',
            'strategy': S3, 'symbol_ledger_sha256': self.tracker.symbol_order_watermark('paper', '005930'),
            'holding_observation_id': holding}
        evidence = {'strategy': S3, 'config': {'multi_exit_kind': 'full',
            'multi_position_generation_id': self.initial}}
        ident = self.tracker.prepare_order('paper', 'sell', '005930', 100, S3, price=None,
            evidence=evidence, reconciliation_evidence=proof)
        self.assertIsNone(self.candidate())
        self.tracker.cancel_intent(ident, 'not_sent')
        self.assertIsNotNone(self.candidate())

    def test_transport_rechecks_new_negative_trend_before_any_network_send(self):
        from kiwoom_autotrader.api import orders
        from kiwoom_autotrader.trading.order_context import automated_orders
        from tests.test_orders import fake_session
        proof, evidence = self.recon(100, 10000, 100, 101), self.evidence_add()
        class Guard:
            reconciliation_evidence = proof
            def before_prepare(self): return True
            def __call__(self): return None
        def queue(*args, **kwargs):
            self.tracker.multi_b3_invalidate_candidate('paper', '005930', S3, self.initial, self.now)
            try: kwargs['before_send']()
            except Exception: raise orders.HTTPNotSentError('guard rejected') from None
            self.fail('transport must never send')
        with automated_orders(self.tracker, S3, evidence=evidence), \
                patch.object(orders, 'capture_automatic_paper_guard', return_value=Guard()), \
                patch.object(orders, 'queued_open', side_effect=queue):
            with self.assertRaises(orders.OrderError): orders.submit_order(fake_session(), 'buy', '005930', 100, 101)
        with sqlite3.connect(self.tracker.path) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM b3_add_claims').fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT state FROM intents WHERE id!=?', (self.initial,)).fetchone()[0], 'cancelled')

    def test_other_strategy_and_incomplete_rest_do_not_own_initial(self):
        self.assertIsNone(self.tracker.multi_b3_add_candidate('paper', '005930', 'S2_surge_high_breakout', self.now))
        with sqlite3.connect(self.tracker.path) as db: db.execute('UPDATE fills SET quantity=99')
        self.assertIsNone(self.candidate())

    def test_daily_count_includes_all_accepted_and_reserved_buys(self):
        evidence, proof = self.evidence_add(), self.recon(100, 10000, 100, 101)
        with patch.object(self.tracker, '_daily_buy_entry_counts', return_value={'accepted': 2, 'reserved': 0}):
            with self.assertRaises(DailyEntryLimitReached): self.prepare_add(evidence=evidence, proof=proof)
        ident = self.prepare_add(evidence=evidence, proof=proof)
        self.assertEqual(self.tracker.daily_buy_entry_counts('paper', '005930', self.now),
                         {'accepted': 1, 'reserved': 1})
        with patch.object(self.tracker, '_daily_buy_entry_counts', return_value={'accepted': 2, 'reserved': 1}):
            with self.assertRaises(ExecutionLogError): self.tracker.assert_multi_buy_dispatch('paper', '005930', ident)

    def test_queue_wait_expiry_blocks_dispatch(self):
        ident = self.prepare_add(); self.now += timedelta(seconds=21)
        with self.assertRaises(ExecutionLogError): self.tracker.assert_multi_buy_dispatch('paper', '005930', ident)


if __name__ == '__main__': unittest.main()
