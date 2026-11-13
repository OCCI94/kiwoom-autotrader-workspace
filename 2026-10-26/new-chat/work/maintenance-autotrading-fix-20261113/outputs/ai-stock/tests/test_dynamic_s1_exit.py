"""Independent owned-exit regressions for the optional dynamic S1 adapter.

Only local synthetic account/00/REST fixtures are used. Entry admission is not
ownership: its expiry or absence cannot suppress an already owned GDCRS exit,
and a fresh hot admission cannot transfer another strategy's position.
"""
from datetime import datetime, timedelta
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import Mock, patch

from kiwoom_autotrader.api.session import IssuedToken, KST
from kiwoom_autotrader.telegram.commands.orders import OrderReplies
from kiwoom_autotrader.trading.execution_log import ExecutionLogError, ExecutionTracker
from kiwoom_autotrader.trading.paper_funnel_state import (
    assert_current_dynamic_s1_admission, dynamic_s1_admission, mark_precision_ready,
    publish_gate, read_gate,
)
import tests.test_b3_ledger as ledger_fixture
import tests.test_cross_monitor as cross_fixture
import tests.test_dynamic_candidates as candidate_fixture
import tests.test_paper_funnel as funnel_fixture


class DynamicS1OwnedExitTests(unittest.TestCase):
    def setUp(self):
        settings_patch = patch('kiwoom_autotrader.trading.execution_log.load_settings', return_value={})
        settings_patch.start()
        self.addCleanup(settings_patch.stop)
        self.fx = cross_fixture.CrossMonitorTests('test_initial_seed_then_true_cross_sells_current_sellable_quantity')
        self.fx.setUp()
        self.addCleanup(self.fx.doCleanups)
        self.fx.now = datetime(2026, 10, 30, 10, 3, 5, tzinfo=KST)
        self.fx.settings.update(
            gdcrs_stocks=[{'code': code, 'budget': 1000000} for code in ('003220', '097520', '072950')],
            paper_funnel_enabled=True, paper_multi_strategy_enabled=True,
            max_positions_per_theme=0,
        )
        self.fx.account.return_value = cross_fixture.report('100', held='100')
        # The reusable cross fixture owns nine shares by default. These entry-
        # admission scenarios explicitly model a synthetic 100-share owner.
        self.fx.monitor.ownership_reader = lambda *_: 100
        self.assertNotIn('005930', [r['code'] for r in self.fx.settings['gdcrs_stocks']])

    def scan(self, minute, prices, *, first_minute=None):
        fx = self.fx
        fx.now = datetime(2026, 10, 30, 10, minute, 5, tzinfo=KST)
        start = minute - len(prices) if first_minute is None else first_minute
        fx.chart.return_value = cross_fixture.bars(prices, minute=start, day=30)
        fx.scan()

    def publish_admission(self):
        """Native contract produced from injected ranking/membership fixtures."""
        fx = self.fx
        source = candidate_fixture.DynamicCandidateTests('test_bounded_current_evidence_catalog_staggers_and_deepcopy')
        source.setUp()
        self.addCleanup(source.doCleanups)
        source.now = fx.now
        catalog = source.provider.discover_funnel_candidates(fx.now)
        row = next(r for r in catalog['candidates'] if r['symbol'] == '005930')
        publish_gate(fx.session, fx.now, ['005930'], available=True,
                     candidates={'005930': row}, provider_binding=catalog['session_binding'])
        book = funnel_fixture.book_at(fx.now) | {'session_binding': catalog['session_binding']}
        mark_precision_ready(fx.session, fx.now, '005930', book)
        return dynamic_s1_admission(fx.session, fx.now, '005930')

    def ledger(self, *, owner='gdcrs'):
        fx = ledger_fixture.B3LedgerTests('test_fully_reconciled_00_and_rest_yield_exact_candidate')
        fx.setUp()
        self.addCleanup(fx.doCleanups)
        fx.now = self.fx.now
        fx.tracker.settings_loader = lambda: dict(self.fx.settings)
        if owner != 'gdcrs':
            # Ownership fixtures are intentionally created locally. No current
            # operating records or strategy attribution are changed.
            with sqlite3.connect(fx.tracker.path) as db:
                db.execute('UPDATE intents SET strategy=? WHERE id=?', (owner, fx.initial))
                db.execute('UPDATE orders SET strategy=? WHERE ord_no=?', (owner, '24'))
        return fx

    def guarded_local_sell(self, tracker, transport):
        """Exercise actual owner/reservation gates; the final transport is fake."""
        def sell(command, session, *, before_send):
            _, symbol, quantity = command.split()
            try:
                tracker.assert_legacy_ddcrs_allowed('paper', symbol)
                before_send()
                intent = tracker.prepare_order('paper', 'sell', symbol, int(quantity), 'ddcrs',
                                               price=None, require_no_pending=True)
            except ExecutionLogError:
                return OrderReplies(['소유권 검증 보류'], skipped_reason='ownership_unverified', submitted=False)
            transport(symbol, int(quantity), intent)
            return ['격리 모의 접수']
        return sell

    def test_owned_symbol_outside_fixed_universe_exits_without_any_entry_gate(self):
        self.assertIsNone(read_gate(self.fx.session, self.fx.now))
        self.scan(3, [1000, 1100, 1200])
        self.fx.sell.assert_not_called()
        self.fx.account.return_value = cross_fixture.report('37', held='100')
        self.scan(4, [1000, 1100, 1200, 1000])
        self.fx.sell.assert_called_once()
        self.assertEqual(self.fx.sell.call_args.args[0], 'sell 005930 37')
        self.fx.sell.call_args.kwargs['before_send']()

    def test_expired_previous_minute_admission_does_not_suppress_owned_exit(self):
        proof = self.publish_admission()
        self.scan(3, [1000, 1100, 1200])
        self.fx.now = datetime(2026, 10, 30, 10, 4, 5, tzinfo=KST)
        with self.assertRaises(ValueError):
            assert_current_dynamic_s1_admission(self.fx.session, self.fx.now, proof)
        self.assertIsNone(read_gate(self.fx.session, self.fx.now))
        self.scan(4, [1000, 1100, 1200, 1000])
        self.fx.sell.assert_called_once()

    def test_removed_hot_symbol_retains_owned_exit_monitoring(self):
        self.publish_admission()
        self.scan(3, [1000, 1100, 1200])
        self.fx.now = datetime(2026, 10, 30, 10, 4, 5, tzinfo=KST)
        publish_gate(self.fx.session, self.fx.now, ['000660'], available=True)
        self.scan(4, [1000, 1100, 1200, 1000])
        self.fx.sell.assert_called_once()
        self.assertNotIn('005930', read_gate(self.fx.session, self.fx.now)['symbols'])

    def test_token_renewal_resets_cross_baseline_but_preserves_durable_gdcrs_owner(self):
        fx = self.fx
        ledger = self.ledger()
        proof = self.publish_admission()
        self.scan(3, [1000, 1100, 1200])
        # A token identity change is not evidence of a missing 00 interval.
        # The separate gap test below explicitly preserves that distinction.
        fx.session._issued = IssuedToken('renewed-isolated-token', '20300101000000')
        restored = ExecutionTracker(log_dir=Path(ledger.tmp.name), now=lambda: fx.now,
                                    settings_loader=lambda: dict(fx.settings))
        state = restored.multi_strategy_position('paper', '005930', 'gdcrs', fx.now)
        self.assertEqual((state['status'], state['owned_qty']), ('owned', 100))
        with self.assertRaises(ValueError):
            assert_current_dynamic_s1_admission(fx.session, fx.now, proof)
        sent = Mock()
        fx.monitor.sell_handler = self.guarded_local_sell(restored, sent)
        fx.monitor.ownership_reader = lambda _session, symbol, now: restored.multi_strategy_position(
            'paper', symbol, 'ddcrs', now)['owned_qty']
        # An old crossover first seen after session renewal is only a baseline.
        self.scan(4, [1000, 1100, 1200, 1000])
        sent.assert_not_called()
        self.scan(5, [1100, 1200, 1000, 1400])
        sent.assert_not_called()
        self.scan(6, [1200, 1000, 1400, 900])
        sent.assert_called_once()
        self.assertEqual(sent.call_args.args[:2], ('005930', 100))
        with sqlite3.connect(restored.path) as db:
            self.assertEqual(db.execute("SELECT symbol,quantity,strategy FROM intents WHERE side='sell'").fetchall(),
                             [('005930', 100, 'ddcrs')])

    def test_first_negative_sample_and_skipped_interval_never_backfill_exit(self):
        self.scan(4, [1000, 1100, 1200, 1000])
        self.fx.sell.assert_not_called()
        self.scan(5, [1100, 1200, 1000, 900])
        self.fx.sell.assert_not_called()
        # A newer positive sample starts the observed basis, but a gap across
        # the next crossover must not be replayed as an immediately executable sell.
        self.scan(6, [1200, 1000, 900, 1400])
        self.scan(8, [900, 1400, 1000, 900])
        self.fx.sell.assert_not_called()

    def test_current_hot_admission_cannot_transfer_s2_s3_s4_lot_to_ddcrs(self):
        for owner in ('S2_surge_high_breakout', 'S3_upper_limit_continuation', 'S4_theme_and_catalyst_leader'):
            with self.subTest(owner=owner):
                self.fx.monitor = self.fx.new_monitor()
                self.fx.now = datetime(2026, 10, 30, 10, 3, 5, tzinfo=KST)
                ledger = self.ledger(owner=owner)
                self.publish_admission()
                sent = Mock()
                checked_sell = Mock(wraps=self.guarded_local_sell(ledger.tracker, sent))
                self.fx.monitor.sell_handler = checked_sell
                self.scan(3, [1000, 1100, 1200])
                self.scan(4, [1000, 1100, 1200, 1000])
                checked_sell.assert_called_once()
                sent.assert_not_called()
                with sqlite3.connect(ledger.tracker.path) as db:
                    self.assertEqual(db.execute("SELECT COUNT(*) FROM intents WHERE side='sell'").fetchone()[0], 0)
                # Avoid crossing marker reuse, which could make later subcases
                # vacuously pass before reaching their actual ownership guard.
                self.fx.path = Path(self.fx.temp.name) / (owner + '-markers.sqlite3')

    def test_recorded_stream_gap_does_not_become_verified_ownership_after_restart(self):
        ledger = self.ledger()
        ledger.event('stream_gap', ledger.now, {})
        ledger.now += timedelta(seconds=1)
        ledger.event('stream_resumed', ledger.now, {})
        restored = ExecutionTracker(log_dir=Path(ledger.tmp.name), now=lambda: ledger.now,
                                    settings_loader=lambda: dict(self.fx.settings))
        self.assertEqual(restored.multi_strategy_position('paper', '005930', 'gdcrs', ledger.now)['status'],
                         'unsupported')

    def test_fresh_account_zero_sellable_blocks_exit_even_with_preserved_baseline(self):
        self.scan(3, [1000, 1100, 1200])
        self.fx.account.side_effect = [cross_fixture.report('100', held='100'),
                                       cross_fixture.report('0', held='100')]
        self.scan(4, [1000, 1100, 1200, 1000])
        self.fx.sell.assert_not_called()


if __name__ == '__main__':
    unittest.main()
