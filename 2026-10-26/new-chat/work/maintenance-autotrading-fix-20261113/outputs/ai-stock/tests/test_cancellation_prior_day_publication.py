"""Purpose-bound prior-day publication; every account/ledger is a private fixture."""
from datetime import datetime, timedelta
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import patch
import unittest

from kiwoom_autotrader.api.session import KST
from kiwoom_autotrader.trading import cancellation_settlement as settlement
from kiwoom_autotrader.trading import execution_log as execution
from kiwoom_autotrader.trading.unfilled_buy_ttl import TTLLedger
from tests import test_cancellation_settlement as fixtures


class PriorDaySettlementPublicationTests(unittest.TestCase):
    def make_unpublished(self, hour=9, minute=2):
        f = fixtures.SettlementFixture(0)
        self.addCleanup(f.close)
        f.tracker._closed_calendar_resolver = lambda now, day: {}
        with patch.object(f.tracker, '_materialize_accounting', side_effect=OSError('private publication interruption')):
            with self.assertRaises(OSError):
                f.settle()
        self.assertEqual(f.released(), 0)
        f.now = datetime(2026, 11, 2, hour, minute, tzinfo=KST)
        ledger = TTLLedger(f.tracker)
        with ledger.db() as db:
            order = dict(f.order(db))
            record = settlement.committed_settlement_record(db, 'default', order)
            self.assertIsNotNone(record)
            self.assertLess(order['day'], f.now.strftime('%Y%m%d'))
        return f, ledger, order, record[0]['evidence_sha256']

    @staticmethod
    def economic_rows(tracker):
        with tracker._db() as db:
            return {table: [tuple(row) for row in db.execute('SELECT * FROM ' + table + ' ORDER BY rowid')]
                for table in ('orders', 'intents', 'fills', 'holdings', 'account_balance_daily',
                              'profits', 'profit_sources', 'execution_observations')}

    def guard(self, fixture):
        started = fixture.now
        def verify():
            # Session/recorder guards must never execute under history lock.
            self.assertFalse(fixture.tracker._history_lock._is_owned())
            if not 0 <= (fixture.now - started).total_seconds() <= 90:
                raise settlement.CancellationSettlementUnavailable('fixture_fresh_pair_expired_90_seconds')
        return verify

    def publish(self, f, ledger, order, *, guard=None):
        return settlement._publish_settlement(f.tracker, ledger, order,
                                              lambda: f.now, guard or self.guard(f))

    def test_true_prior_day_committed_record_publishes_at_open_and_auction(self):
        for hour, minute in ((9, 2), (15, 25)):
            with self.subTest(hour=hour, minute=minute):
                f, ledger, order, evidence = self.make_unpublished(hour, minute)
                before = self.economic_rows(f.tracker)
                self.assertIsNone(f.tracker._materialize_accounting(order['day']))
                with patch.object(f.tracker, '_materialize_history_locked', side_effect=AssertionError('no generic history export')):
                    self.publish(f, ledger, order)
                self.assertEqual(f.released(), 10)
                self.assertEqual(before, self.economic_rows(f.tracker))
                self.assertIsNone(f.tracker._materialize_accounting(order['day']))
                target = f.root / (order['day'] + '.execution-accounting.json')
                raw = target.read_bytes()
                note = json.loads(raw)['cancellation_settlements'][0]
                self.assertEqual((note['original_order_number'], note['evidence_sha256'], note['status']),
                                 ('1', evidence, 'final_evidence_committed'))
                with ledger.db() as db:
                    publication = db.execute('SELECT * FROM ' + settlement.PUBLICATIONS).fetchone()
                    self.assertEqual(publication['accounting_sha256'], hashlib.sha256(raw).hexdigest())

    def test_freshness_expiring_during_sidecar_does_not_release_reservation(self):
        f, ledger, order, _ = self.make_unpublished()
        actual = f.tracker._materialize_accounting
        def delayed(*args, **kwargs):
            result = actual(*args, **kwargs)
            f.now += timedelta(seconds=91)
            return result
        with patch.object(f.tracker, '_materialize_accounting', side_effect=delayed):
            with self.assertRaisesRegex(settlement.CancellationSettlementUnavailable, 'expired_90_seconds'):
                self.publish(f, ledger, order)
        self.assertEqual(f.released(), 0)
        with ledger.db() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM ' + settlement.PUBLICATIONS).fetchone()[0], 0)

    def test_wrong_exact_evidence_hash_cannot_use_publication_exception(self):
        f, ledger, order, _ = self.make_unpublished()
        with self.assertRaisesRegex(settlement.CancellationSettlementUnavailable, 'evidence_changed'):
            f.tracker._materialize_settlement_accounting(order, 'f' * 64, guard=self.guard(f))
        self.assertEqual(f.released(), 0)

    def test_ack_without_committed_terminal_proof_has_no_publication_authority(self):
        f = fixtures.SettlementFixture(0)
        self.addCleanup(f.close)
        f.now = datetime(2026, 11, 2, 9, 2, tzinfo=KST)
        with TTLLedger(f.tracker).db() as db:
            order = dict(f.order(db))
        with self.assertRaisesRegex(settlement.CancellationSettlementUnavailable, 'evidence_changed'):
            f.tracker._materialize_settlement_accounting(order, 'f' * 64, guard=self.guard(f))
        self.assertEqual(f.released(), 0)

    def test_late_financial_change_invalidates_committed_proof_before_publication(self):
        f, ledger, order, _ = self.make_unpublished()
        with f.tracker._db() as db:
            db.execute("UPDATE intents SET reason='changed_after_commit' WHERE id='initial1'")
        with self.assertRaisesRegex(settlement.CancellationSettlementUnavailable, 'late_order_change'):
            self.publish(f, ledger, order)
        with ledger.db() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM ' + settlement.PUBLICATIONS).fetchone()[0], 0)

    def test_over_limit_day_cannot_hydrate_through_committed_publication_exception(self):
        f, ledger, order, _ = self.make_unpublished()
        raw = execution.canonical_json({'source_api': 'ka10072', 'mode': 'paper', 'day': order['day']})
        with f.tracker._db() as db:
            db.executemany('INSERT INTO broker_observations(scope,day,mode,observed_at,source_api,digest,payload_json) VALUES (?,?,?,?,?,?,?)',
                [('default', order['day'], 'paper', f.now.isoformat(), 'ka10072',
                  hashlib.sha256(raw.encode()).hexdigest(), raw)] * 20001)
        with patch.object(f.tracker, '_broker_observations_snapshot', side_effect=AssertionError('blocked source hydrated')):
            with self.assertRaisesRegex(settlement.CancellationSettlementUnavailable, 'source_limit_blocked'):
                self.publish(f, ledger, order)
        self.assertEqual(f.released(), 0)
        self.assertEqual(f.tracker._closed_management[('paper', order['day'])]['status'], 'limit_blocked')

    def test_legacy_adapter_has_no_implicit_prior_day_override(self):
        f, ledger, order, _ = self.make_unpublished()
        legacy = SimpleNamespace(scope=f.tracker.scope, log_dir=f.tracker.log_dir,
                                 _materialize_accounting=f.tracker._materialize_accounting)
        with self.assertRaisesRegex(settlement.CancellationSettlementUnavailable, 'prior_day_publication_adapter_required'):
            settlement._publish_settlement(legacy, ledger, order, lambda: f.now, self.guard(f))
        self.assertEqual(f.released(), 0)


if __name__ == '__main__':
    unittest.main()
