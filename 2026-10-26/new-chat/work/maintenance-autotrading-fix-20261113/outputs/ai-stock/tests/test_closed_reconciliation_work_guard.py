"""Historical work guards use only private ledgers and fake clocks/transports."""
from collections import deque
from datetime import datetime, timedelta
import hashlib
import json
from unittest.mock import patch
import unittest

from kiwoom_autotrader.api.minute_chart import session_identity
from kiwoom_autotrader.api.session import KST
from kiwoom_autotrader.trading import execution_log as module
from tests import test_closed_day_backoff as backoff_fixture

OLD, SYMBOL = backoff_fixture.OLD, backoff_fixture.SYMBOL


class ClosedReconciliationWorkGuardTests(unittest.TestCase):
    def setUp(self):
        self.fixture = backoff_fixture.ClosedDayBackoffTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.tracker = self.fixture.tracker
        self.tracker._closed_calendar_resolver = lambda now, day: {}

    def at(self, hour, minute=0, second=0, *, day=27):
        self.fixture.now = datetime(2026, 10, day, hour, minute, second, tzinfo=KST)
        return self.fixture.now

    def seed_costs(self, original=20001, compact=0, *, day=OLD):
        now = self.fixture.now.isoformat()
        event = {'schema': 'kiwoom-broker-page-v1', 'source_api': 'ka10072',
            'source_kind': 'broker_observation', 'mode': 'paper', 'day': day,
            'query_id': 'a' * 32, 'request_started_at': now, 'observed_at': now,
            'page': 1, 'request_body': {'stk_cd': SYMBOL, 'strt_dt': day},
            'response_body': {'return_code': 0, 'dt_stk_div_rlzt_pl': []},
            'continuation': {'request_cont_yn': 'N', 'response_cont_yn': 'N', 'next_key_present': False},
            'status': 'success', 'http_status': 200}
        raw = module.canonical_json(event)
        with self.tracker._db() as db:
            db.executemany('INSERT INTO broker_observations(scope,day,mode,observed_at,source_api,digest,payload_json) VALUES (?,?,?,?,?,?,?)',
                [('default', day, 'paper', now, 'ka10072', hashlib.sha256(raw.encode()).hexdigest(), raw)] * original)
            # Count-only guard must not hydrate these deliberately invalid
            # receipts; no fixture turns them into valid financial evidence.
            start = original + 1
            db.executemany('INSERT INTO broker_observation_repeats VALUES (?,?,?,?,?,?,?,?,?,?)',
                [(start + i, 'default', day, 'paper', now, 'ka10072', 'x', 1, '{}', 'x') for i in range(compact)])

    def test_ordinary_open_and_auction_boundaries_defer_historical_work(self):
        for h, m, s in ((8, 55, 0), (9, 0, 0), (9, 5, 0), (15, 20, 0), (15, 30, 0), (15, 39, 59)):
            self.assertTrue(self.tracker._historical_work_deferred(self.at(h, m, s)))
        self.assertFalse(self.tracker._historical_work_deferred(self.at(15, 40)))
        self.assertEqual(self.tracker._historical_guard_status['basis'], 'conservative_unverified_session')

    def test_special_delayed_session_extends_guard_without_narrowing_unknown_range(self):
        self.tracker._closed_calendar_resolver = lambda now, day: {
            'current_session_start': now.replace(hour=10, minute=0, second=0),
            'current_session_end': now.replace(hour=16, minute=30, second=0)}
        self.assertTrue(self.tracker._historical_work_deferred(self.at(9)))
        self.assertTrue(self.tracker._historical_work_deferred(self.at(16, 30)))
        self.assertTrue(self.tracker._historical_work_deferred(self.at(16, 39, 59)))
        self.assertFalse(self.tracker._historical_work_deferred(self.at(16, 40)))

    def test_open_prunes_preopen_history_queue_and_preserves_current_account_work(self):
        now = self.at(9)
        today = now.strftime('%Y%m%d')
        self.tracker._identity = session_identity(self.fixture.session)
        self.tracker._tasks = deque([('executions', OLD, None), ('profit', OLD, SYMBOL),
            ('summary', OLD, None), ('holdings', today, None)])
        self.tracker._cycle_days = [OLD, today]
        self.tracker._next_at = now + timedelta(hours=1)
        self.tracker._sell_refresh_pending = {('paper', OLD, SYMBOL), ('paper', today, SYMBOL)}
        self.tracker.tick(self.fixture.session, self.fixture.notify)
        self.assertTrue(self.fixture.executions.called)
        self.assertEqual(self.fixture.executions.call_args.args[1], today)
        self.assertTrue(all(task[1] == today for task in self.tracker._tasks))
        self.assertEqual(self.tracker._cycle_days, [today])
        self.assertIn(('paper', OLD, SYMBOL), self.tracker._sell_refresh_pending)

    def test_day_transition_cannot_keep_yesterdays_tasks_in_opening_loop(self):
        self.at(16)
        self.tracker._identity = session_identity(self.fixture.session)
        self.tracker._tasks = deque([('summary', '20261027', None), ('holdings', '20261027', None)])
        self.tracker._cycle_days = ['20261027']
        self.tracker._next_at = self.fixture.now
        now = self.at(9, day=28)
        self.tracker.tick(self.fixture.session, self.fixture.notify)
        self.assertTrue(all(task[1] == '20261028' for task in self.tracker._tasks))
        self.assertEqual(self.fixture.executions.call_args.args[1], now.strftime('%Y%m%d'))

    def test_old_cycle_days_are_removed_even_if_remaining_task_is_today_holdings(self):
        now = self.at(9)
        self.tracker._identity = session_identity(self.fixture.session)
        self.tracker._cycle_days = [OLD, now.strftime('%Y%m%d')]
        self.tracker._tasks = deque([('holdings', now.strftime('%Y%m%d'), None)])
        self.tracker._next_at = now
        self.tracker.tick(self.fixture.session, self.fixture.notify)
        self.assertNotIn(OLD, self.tracker._cycle_days)
        self.assertFalse(any(task[1] == OLD for task in self.tracker._tasks))

    def test_over_limit_day_skips_json_fingerprint_and_does_not_settle_financials(self):
        self.seed_costs()
        with self.tracker._db() as db:
            before = db.execute('SELECT COUNT(*) FROM intents').fetchone()[0]
            with patch.object(self.tracker, '_broker_observations_snapshot', side_effect=AssertionError('JSON must not load')):
                self.assertFalse(self.tracker._closed_due(db, 'paper', OLD, self.fixture.now))
            self.assertEqual(db.execute('SELECT COUNT(*) FROM intents').fetchone()[0], before)
        state = self.tracker.diagnostics_snapshot()['closed_day_management'][0]
        self.assertEqual(state['status'], 'limit_blocked')
        self.assertEqual(state['cost_observation_rows'], 20001)
        self.assertEqual(state['financial_status'], 'unconfirmed')
        self.assertFalse(state['reservation_released'])

    def test_compact_receipts_count_toward_logical_limit_without_hydration(self):
        self.seed_costs(original=1, compact=20000)
        with self.tracker._db() as db:
            with patch.object(self.tracker, '_closed_economic_fingerprint', side_effect=AssertionError('fingerprint must not run')):
                self.assertFalse(self.tracker._closed_due(db, 'paper', OLD, self.fixture.now))
        self.assertEqual(self.tracker._closed_management[('paper', OLD)]['cost_observation_rows'], 20001)

    def test_disabled_cadence_backoff_cannot_bypass_first_queue_source_limit(self):
        self.seed_costs()
        self.tracker._closed_backoff_enabled = False
        self.tracker.tick(self.fixture.session, self.fixture.notify)
        self.assertTrue(self.fixture.executions.called)
        self.assertNotEqual(self.fixture.executions.call_args.args[1], OLD)
        self.assertFalse(any(task[1] == OLD for task in self.tracker._tasks))
        self.assertEqual(self.tracker._closed_management[('paper', OLD)]['status'], 'limit_blocked')

    def test_queued_blocked_day_tasks_and_sidecars_do_not_fetch_or_rebuild(self):
        self.seed_costs()
        now = self.fixture.now
        today = now.strftime('%Y%m%d')
        self.tracker._identity = session_identity(self.fixture.session)
        self.tracker._tasks = deque((name, OLD, SYMBOL if 'profit' in name else None)
            for name in ('executions', 'profit', 'period_profit', 'daily_profit', 'summary'))
        self.tracker._tasks.append(('holdings', today, None))
        self.tracker._cycle_days = [OLD, today]
        self.tracker._next_at = now
        self.tracker._sell_refresh_pending.add(('paper', OLD, SYMBOL))
        with patch.object(self.tracker, '_materialize_history_locked', side_effect=AssertionError('no export')):
            self.tracker.tick(self.fixture.session, self.fixture.notify)
            self.tracker._materialize_history(OLD)
            self.assertIsNone(self.tracker._materialize_accounting(OLD))
        self.assertFalse(self.fixture.executions.called)
        self.assertFalse(self.fixture.profits.called)
        self.assertTrue(self.fixture.account.called)
        self.assertFalse(any(task[1] == OLD for task in self.tracker._tasks))
        self.assertIn(('paper', OLD, SYMBOL), self.tracker._sell_refresh_pending)

    def test_unchanged_deadline_gate_precedes_full_fingerprint_but_new_intent_wakes(self):
        self.fixture.prepare()
        with self.tracker._db() as db:
            with patch.object(self.tracker, '_closed_economic_fingerprint', side_effect=AssertionError('not due')):
                self.assertFalse(self.tracker._closed_due(db, 'paper', OLD, self.fixture.now + timedelta(seconds=1)))
            db.execute('INSERT INTO intents(scope,id,day,mode,side,symbol,quantity,strategy,created_at) VALUES (?,?,?,?,?,?,?,?,?)',
                ('default', 'late', OLD, 'paper', 'buy', '000660', 1, 'fixture', self.fixture.now.isoformat()))
            with patch.object(self.tracker, '_closed_economic_fingerprint', return_value=('changed', True)) as fingerprint:
                self.assertTrue(self.tracker._closed_due(db, 'paper', OLD, self.fixture.now + timedelta(seconds=2)))
                self.assertEqual(fingerprint.call_count, 1)

    def test_closed_metadata_cache_does_not_pin_raw_snapshot_and_has_four_entry_bound(self):
        self.at(16)
        for day in ('20261020', '20261021', '20261022', '20261023', '20261026'):
            with self.tracker._db() as db:
                self.assertIsNotNone(self.tracker._closed_economic_fingerprint(db, 'paper', day)[0])
        self.assertLessEqual(len(self.tracker._closed_observation_cache), 4)
        for watermark, evidence in self.tracker._closed_observation_cache.values():
            self.assertFalse(any(isinstance(value, module._BrokerObservation) for value in evidence))
            self.assertNotIn('payload_json', json.dumps(evidence))
            self.assertLessEqual(len(module.canonical_json(evidence).encode()), 512 * 1024)

    def test_verified_unchanged_delta_updates_cheap_watermark_before_deadline(self):
        event = {'schema_version': 1, 'source_api': '00', 'source_kind': 'broker_observation',
            'observed_at': self.fixture.now.isoformat(), 'day': OLD, 'mode': 'paper',
            'event_kind': 'execution_stream', 'values': {'909': '1', '915': '1'}}
        self.tracker.record_broker_observation(event)
        self.fixture.prepare()
        self.tracker.record_broker_observation({**event,
            'observed_at': (self.fixture.now + timedelta(seconds=1)).isoformat()})
        with self.tracker._db() as db:
            self.assertFalse(self.tracker._closed_due(db, 'paper', OLD, self.fixture.now + timedelta(seconds=2)))
            with patch.object(self.tracker, '_closed_economic_fingerprint', side_effect=AssertionError('repeat delta reparsed')):
                self.assertFalse(self.tracker._closed_due(db, 'paper', OLD, self.fixture.now + timedelta(seconds=3)))


if __name__ == '__main__':
    unittest.main()
