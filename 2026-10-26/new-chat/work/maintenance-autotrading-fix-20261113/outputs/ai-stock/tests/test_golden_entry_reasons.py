"""Observed crossings keep structured no-send reasons, never inferred from text."""
import sqlite3
from unittest.mock import Mock

from kiwoom_autotrader.telegram.commands.orders import OrderReplies
from tests.test_golden_cross import GoldenCrossTests


class GoldenEntryReasonTests(GoldenCrossTests):
    def decision(self):
        with sqlite3.connect(self.path) as db:
            return db.execute('SELECT outcome,skip_reason FROM entry_decisions ORDER BY id DESC LIMIT 1').fetchone()

    def test_cap_skip_is_persisted_and_attempt_is_never_replayed(self):
        self.buy.return_value = OrderReplies([], skipped_reason='symbol_buy_limit_exhausted')
        self.seed(); self.cross()
        self.assertEqual(self.decision(), ('skipped', 'cap_exhausted'))
        self.tick()
        self.assertEqual(self.buy.call_count, 1)

    def test_only_structured_cash_reason_can_be_recorded_as_insufficient_cash(self):
        self.buy.return_value = OrderReplies(['known cash check failed'], skipped_reason='insufficient_cash', submitted=False)
        self.seed(); self.cross()
        self.assertEqual(self.decision(), ('skipped', 'insufficient_cash'))

    def test_cash_words_in_untyped_reply_are_not_a_cash_diagnosis(self):
        self.buy.return_value = ['예수금이 부족할 수 있음 private-token']
        self.seed(); self.cross()
        self.assertEqual(self.decision(), ('outcome_unconfirmed', 'order_outcome_unconfirmed'))
        self.assertNotIn('private-token', str(self.monitor.status()))

    def test_bps_filter_logs_reason_without_claiming_an_order(self):
        self.settings['gdcrs_min_cross_bps'] = 7143
        self.seed(); self.cross()
        self.buy.assert_not_called()
        self.assertEqual(self.decision(), ('skipped', 'below_min_cross_bps'))
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM sqlite_master WHERE name='attempts'").fetchone()[0], 0)

    def test_cooldown_filter_keeps_reason_and_no_order_claim(self):
        self.settings['gdcrs_reentry_cooldown_minutes'] = 10
        self.monitor = self.make_monitor(cooldown_check=Mock(return_value=False))
        self.seed(); self.cross()
        self.assertEqual(self.decision(), ('skipped', 'reentry_cooldown'))
        self.buy.assert_not_called()

    def test_explicit_receipt_metadata_is_distinguished_from_unknown_handler_result(self):
        self.buy.return_value = OrderReplies(['receipt'], submitted=True)
        self.seed(); self.cross()
        self.assertEqual(self.decision(), ('receipt_observed', None))

    def test_existing_attempt_claim_keeps_its_actual_skip_reason(self):
        from tests.test_golden_cross import bars
        self.seed()
        self.monitor.markers.claim('paper', '005930', bars([1], minute=3)[0].start)
        self.cross()
        self.buy.assert_not_called()
        self.assertEqual(self.decision(), ('skipped', 'attempt_already_claimed'))

    def test_unallowlisted_handler_reason_does_not_enter_audit(self):
        self.buy.return_value = OrderReplies([], skipped_reason='private-token')
        self.seed(); self.cross()
        self.assertEqual(self.decision(), ('outcome_unconfirmed', 'order_outcome_unconfirmed'))
        self.assertNotIn('private-token', str(self.monitor.status()))

    def test_audit_failure_cannot_bypass_uncertain_order_halt(self):
        self.buy.return_value = OrderReplies(['unconfirmed'], uncertain=True)
        self.monitor.markers.record_decision = Mock(side_effect=OSError('offline audit unavailable'))
        self.seed(); self.cross()
        self.assertTrue(self.monitor._halted)
        self.assertFalse(self.settings['gdcrs_enabled'])
