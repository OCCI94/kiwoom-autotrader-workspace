from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta
from threading import Event, Thread
from unittest.mock import patch
import unittest
from zoneinfo import ZoneInfo

from kiwoom_autotrader.trading.prearmed_decision import (
    DecisionVersion, PrearmedDecisionCache, SafetyIndex,
)


class PrearmedDecisionTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 2, 9, 35, 1, tzinfo=ZoneInfo('Asia/Seoul'))
        self.version = DecisionVersion('paper-session', 'ledger-100', 'a'*64, 'b'*64, 10000)
        self.cache = PrearmedDecisionCache()
        self.evaluation = {'schema': 'fixture', 'status': 'PASS', 'eligible': True,
            'vwap': {'volume': 10000, 'hlc3_volume_numerator': 299000000},
            'checks': {'first': True}, 'source': ['a'*64]}

    def arm(self, **kw):
        args = dict(version=self.version, evaluation=self.evaluation, now=self.now,
                    expires_at=self.now+timedelta(seconds=5))
        args.update(kw)
        return self.cache.arm('003220', 'S5_steady_uptrend_vwap', **args)

    def lookup(self, **kw):
        return self.cache.lookup('003220', 'S5_steady_uptrend_vwap',
            current_version=kw.get('version', self.version), now=kw.get('now', self.now))

    def safety(self, **kw):
        args = dict(version=self.version, observed_at=self.now,
            expires_at=self.now+timedelta(seconds=4), held_principal_krw=100000,
            reserved_principal_krw=200000, available_cash_krw=800000,
            theme_slots=1, position_slots=6, daily_entries_remaining=2,
            unresolved_orders=False, verified=True)
        args.update(kw)
        return SafetyIndex(**args)

    def test_unknown_safety_is_only_initial_budget_upper_bound(self):
        result = self.arm(); ticket = result.ticket
        self.assertEqual(ticket.estimated_qty, 50)
        self.assertEqual(ticket.estimate_basis, 'budget_only_upper_bound')
        self.assertIsNone(ticket.theme_slots)
        self.assertIsNone(ticket.remaining_principal_krw)
        self.assertFalse(ticket.safety_verified)
        self.assertFalse(result.order_authorized)
        self.assertFalse(ticket.order_authorized)
        self.assertTrue(ticket.requires_fresh_reconciliation)
        self.assertEqual(self.lookup().status, 'HIT')

    def test_nested_inputs_and_returned_copy_cannot_mutate_snapshot(self):
        self.arm(); ticket = self.lookup().ticket
        self.evaluation['vwap']['volume'] = 0
        self.evaluation['source'].append('changed')
        self.assertEqual(ticket.evaluation['vwap']['volume'], 10000)
        self.assertEqual(ticket.evaluation['source'], ('a'*64,))
        with self.assertRaises(TypeError): ticket.evaluation['vwap']['volume'] = 0
        with self.assertRaises(FrozenInstanceError): ticket.estimated_qty = 100
        with self.assertRaises(ValueError): replace(ticket, order_authorized=True)
        copy = ticket.thawed_evaluation(); copy['vwap']['volume'] = 0
        self.assertEqual(ticket.evaluation['vwap']['volume'], 10000)

    def test_every_relevant_version_change_invalidates(self):
        self.arm()
        cases = [('session_id', 'different', 'session_changed'),
            ('ledger_watermark', 'ledger-partial-fill', 'ledger_changed'),
            ('theme_generation', 1, 'theme_changed'),
            ('reservation_generation', 1, 'reservation_changed'),
            ('evidence_sha256', 'c'*64, 'market_or_risk_evidence_changed'),
            ('settings_sha256', 'c'*64, 'settings_changed'),
            ('price', 10001, 'price_changed')]
        for field, value, reason in cases:
            with self.subTest(field=field):
                result = self.lookup(version=replace(self.version, **{field: value}))
                self.assertEqual((result.status, result.reason), ('STALE', reason))
                self.assertIsNone(result.ticket)

    def test_expiry_clock_reversal_and_naive_time_never_hit(self):
        self.arm()
        for now in (self.now+timedelta(seconds=5), self.now-timedelta(microseconds=1), self.now.replace(tzinfo=None)):
            self.assertEqual(self.lookup(now=now).status, 'STALE')
        self.assertEqual(self.lookup(version=None).reason, 'current_version_unavailable')

    def test_revocation_and_clear_preserve_no_permission(self):
        self.arm(); self.cache.invalidate('003220', 'S5_steady_uptrend_vwap')
        self.assertEqual(self.lookup().status, 'MISS')
        self.arm(); self.cache.clear()
        self.assertEqual(self.cache.status()['entries'], 0)
        self.assertFalse(self.cache.status()['order_authorized'])

    def test_reconciled_budget_preserves_cross_strategy_principal_and_cash(self):
        ticket = self.arm(safety=self.safety(held_principal_krw=650000,
            reserved_principal_krw=170000, available_cash_krw=90000)).ticket
        self.assertEqual(ticket.remaining_principal_krw, 180000)
        self.assertEqual(ticket.estimated_qty, 9)
        self.assertEqual(ticket.remaining_limit_qty, 9)
        self.assertEqual(ticket.theme_slots, 1)
        self.assertTrue(ticket.safety_verified)
        self.assertFalse(ticket.order_authorized)
        self.assertEqual(ticket.expires_at, self.now+timedelta(seconds=4))

    def test_partial_fill_or_intent_requires_new_version_before_rearm(self):
        self.arm(safety=self.safety())
        filled_version = replace(self.version, ledger_watermark='ledger-101', reservation_generation=1)
        self.assertEqual(self.lookup(version=filled_version).status, 'STALE')
        ticket = self.arm(version=filled_version, safety=self.safety()).ticket
        self.assertFalse(ticket.safety_verified)
        self.assertEqual(ticket.estimate_basis, 'budget_only_upper_bound')

    def test_incomplete_or_expired_safety_is_not_inferred(self):
        for change in ({'available_cash_krw': None}, {'theme_slots': None}, {'verified': False},
                       {'expires_at': self.now}, {'unresolved_orders': None},
                       {'observed_at': self.now+timedelta(seconds=1)}):
            with self.subTest(change=change):
                ticket = self.arm(safety=self.safety(**change)).ticket
                self.assertFalse(ticket.safety_verified)
                self.assertIsNone(ticket.theme_slots)

    def test_zero_capacity_cash_daily_and_pending_each_zero_quantity(self):
        cases = [({'unresolved_orders': True}, 'unresolved_order_or_intent'),
            ({'daily_entries_remaining': 0}, 'daily_entry_limit_reached'),
            ({'theme_slots': 0}, 'theme_concentration_limit_reached'),
            ({'position_slots': 0}, 'max_concurrent_positions_reached'),
            ({'held_principal_krw': 900000}, 'cap_exhausted'),
            ({'available_cash_krw': 9999}, 'budget_below_one_share')]
        for changes, reason in cases:
            with self.subTest(reason=reason):
                ticket = self.arm(safety=self.safety(**changes)).ticket
                self.assertEqual((ticket.estimated_qty, ticket.safety_reason), (0, reason))

    def test_addon_does_not_take_new_symbol_slot_and_never_invents_basis(self):
        unknown = self.arm(entry_kind='add_on', owned=True).ticket
        self.assertEqual(unknown.estimated_qty, 0)
        known = self.arm(entry_kind='add_on', owned=True,
            safety=self.safety(held_principal_krw=500000, reserved_principal_krw=0,
                               theme_slots=0, position_slots=0)).ticket
        self.assertEqual(known.estimated_qty, 50)
        self.assertEqual(known.safety_reason, 'cached_safety_clear')
        with self.assertRaises(ValueError): self.arm(entry_kind='add_on', owned=False)

    def test_blocked_strategy_cannot_become_eligible_from_cash(self):
        self.evaluation.update(status='BLOCK', eligible=False)
        ticket = self.arm(safety=self.safety()).ticket
        self.assertFalse(ticket.strategy_eligible)
        self.assertFalse(ticket.order_authorized)

    def test_reader_does_not_acquire_writer_lock_or_make_io(self):
        self.arm(); completed = Event(); results = []
        def read():
            with patch('builtins.open', side_effect=AssertionError('no file I/O')):
                results.append(self.lookup()); completed.set()
        self.cache._writer_lock.acquire()
        thread = Thread(target=read, daemon=True); thread.start()
        try:
            self.assertTrue(completed.wait(0.5), 'reader blocked on writer lock')
        finally:
            self.cache._writer_lock.release(); thread.join(1)
        self.assertEqual(results[0].status, 'HIT')

    def test_owned_capacity_not_evicted_and_expired_rows_removed(self):
        self.cache = PrearmedDecisionCache(1)
        self.arm(owned=True)
        result = self.cache.arm('005930', 'S5_steady_uptrend_vwap', version=self.version,
            evaluation=self.evaluation, now=self.now, expires_at=self.now+timedelta(seconds=5))
        self.assertEqual(result.reason, 'cache_capacity_protects_owned')
        self.assertEqual(self.lookup().status, 'HIT')
        result = self.cache.arm('005930', 'S5_steady_uptrend_vwap', version=self.version,
            evaluation=self.evaluation, now=self.now+timedelta(seconds=5),
            expires_at=self.now+timedelta(seconds=10))
        self.assertEqual(result.status, 'PREPARED')
        self.assertEqual(self.lookup().status, 'MISS')

    def test_caps_limits_and_unbounded_evaluation_fail_closed(self):
        for values in ({'initial_budget_krw': 500001}, {'principal_cap_krw': 1000001},
                       {'expires_at': self.now+timedelta(seconds=61)},
                       {'evaluation': {'payload': 'x'*20000}},
                       {'evaluation': {'payload': float('nan')}},
                       {'evaluation': {'payload': object()}}):
            with self.subTest(values=list(values)):
                with self.assertRaises(ValueError): self.arm(**values)
        for capacity in (0, 101, True):
            with self.assertRaises(ValueError): PrearmedDecisionCache(capacity)


if __name__ == '__main__': unittest.main()
