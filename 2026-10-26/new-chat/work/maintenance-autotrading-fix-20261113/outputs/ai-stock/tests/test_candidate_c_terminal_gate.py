"""Candidate-C reader regressions against local tracker fixtures."""
from datetime import timedelta
import unittest

from deploy_handoff_runtime import db_summary
from kiwoom_autotrader.trading.expired_order_certificates import capture_certificates
from tests import test_cancellation_settlement as cancellation
from tests import test_paper_experiment_ledger_recovery as recovery


class CandidateCTerminalGateTests(unittest.TestCase):
    def test_published_zero_fill_settlement_is_required(self):
        fixture = cancellation.SettlementFixture(quantity=0)
        self.addCleanup(fixture.close)
        def pending():
            return db_summary(fixture.tracker.path.resolve(), pending=True, now=fixture.now)['unfilled_orders']
        self.assertEqual(pending(), 1)
        self.assertEqual(fixture.settle()['status'], 'cancellation_confirmed')
        self.assertEqual(pending(), 0)

    def test_verified_expiry_is_excluded_until_conflicting_new_observation(self):
        fixture = recovery.PriorDayPendingRecovery()
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        fixture.save()
        def pending():
            return db_summary(fixture.tracker.path.resolve(), pending=True, now=fixture.now)['unfilled_orders']
        self.assertEqual(pending(), 1)
        with fixture.tracker._db() as db:
            result = capture_certificates(fixture.tracker, db, fixture.now)
        self.assertEqual(result['captured_orders'], ['24'])
        self.assertEqual(pending(), 1, 'certificate without accepted intent does not release')
        with fixture.tracker._db() as db:
            db.execute('INSERT INTO intents VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                       ('default', 'prior-buy-24', '20261030', 'paper', 'buy', '005930',
                        2, 'gdcrs', '2026-10-30T14:59:59+09:00', '24', 'accepted', None))
        self.assertEqual(pending(), 0)
        fixture.now += timedelta(seconds=2)
        fixture.tracker.record_broker_observation(
            fixture.event('ka10075', 'c', fixture.now - timedelta(seconds=1),
                          [{'ord_no': '0000024', 'orig_ord_no': '0000000'}]))
        self.assertEqual(pending(), 1)
