"""Offline regression tests for paper recovery and expiry lock ordering."""
from contextlib import ExitStack, closing, contextmanager
from datetime import datetime, timedelta
import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

from kiwoom_autotrader.realtime.execution_stream import ExecutionStreamRecorder
from kiwoom_autotrader.trading import execution_log, expired_order_certificates
from kiwoom_autotrader.trading import paper_experiment_recovery as recovery
from tests.test_execution_stream import FakeEngine


class RecoveryBalanceWitnessTests(TestCase):
    def setUp(self):
        temp = self.enterContext(TemporaryDirectory())
        self.socket_guard = self.enterContext(
            patch('socket.socket', side_effect=AssertionError('unexpected socket creation'))
        )
        self.now = datetime(2026, 11, 12, 10, tzinfo=ZoneInfo('Asia/Seoul'))
        self.day = self.now.strftime('%Y%m%d')
        self.execution_start = self.now + timedelta(seconds=1)
        self.t1 = self.now + timedelta(seconds=3)
        self.pending_start = self.now + timedelta(seconds=4)
        self.t2 = self.now + timedelta(seconds=6)
        self.engine = FakeEngine()
        self.session = SimpleNamespace(
            mode='paper', token='offline-fixture', token_usable=True, websocket_engine=self.engine
        )
        identity = (self.session.mode, self.session.token)
        self.enterContext(patch.object(execution_log, 'session_identity', return_value=identity))
        self.enterContext(patch.object(recovery, 'session_identity', return_value=identity))
        self.tracker = execution_log.ExecutionTracker(
            log_dir=Path(temp), now=lambda: self.now, settings_loader=lambda: {},
            closed_day_backoff=False, closed_calendar_resolver=lambda *_: {},
        )

    def tearDown(self):
        self.socket_guard.assert_not_called()

    @staticmethod
    def report(cash):
        return {
            'mode': 'paper', 'exchange': 'KRX', 'snapshot_changed': False,
            'holdings': [], 'cash': {'entr': cash, 'ord_alow_amt': cash},
            'totals': {}, 'pages': {'kt00001': 1, 'kt00018': 1},
        }

    def page(self, api, query_id, started_at):
        if api == 'kt00009':
            body = {'ord_dt': self.day, 'qry_tp': '1', 'stk_bond_tp': '1', 'sell_tp': '0',
                    'stk_cd': '', 'fr_ord_no': '', 'dmst_stex_tp': 'KRX'}
            row_key = 'acnt_ord_cntr_prst_array'
        else:
            body = {'all_stk_tp': '0', 'trde_tp': '0', 'stex_tp': '0', 'stk_cd': ''}
            row_key = 'oso'
        return {
            'schema': 'kiwoom-broker-page-v1', 'source_api': api, 'source_kind': 'broker_observation',
            'mode': 'paper', 'day': self.day, 'query_id': query_id, 'page': 1,
            'request_started_at': started_at.isoformat(),
            'observed_at': (started_at + timedelta(seconds=1)).isoformat(),
            'request_body': body, 'response_body': {'return_code': 0, row_key: []},
            'continuation': {'request_cont_yn': 'N', 'response_cont_yn': 'N', 'next_key_present': False},
            'status': 'success', 'http_status': 200,
        }

    def proof(self, account_at):
        return {
            'day': self.day, 'execution_query_id': 'a' * 32, 'pending_query_id': 'b' * 32,
            'account_observed_at': account_at.isoformat(),
        }

    def execution_page(self):
        return self.page('kt00009', 'a' * 32, self.execution_start)

    def pending_page(self):
        return self.page('ka10075', 'b' * 32, self.pending_start)

    def run_holdings_tick(self):
        self.now = self.t2
        self.tracker._identity = (self.session.mode, self.session.token)
        self.tracker._cycle_started_at = self.now
        self.tracker._tasks.append(('holdings', self.day, None))
        notify = Mock()
        with patch.object(self.tracker, 'account_fetcher', return_value=self.report('2000000')) as fetch:
            self.tracker.tick_current(self.session, notify)
        fetch.assert_called_once_with(self.session)
        notify.assert_not_called()
        with self.tracker._db() as db:
            row = db.execute('SELECT observed_at FROM account_balance_daily').fetchone()
            self.assertEqual(row['observed_at'], self.t2.isoformat())

    def start_latched_recorder(self):
        recorder = ExecutionStreamRecorder(self.session, self.tracker, now=lambda: self.now, poll_interval=5)
        self.session.execution_recorder = recorder
        self.addCleanup(recorder.stop)
        recorder.start()
        self.engine.ready = True
        self.assertTrue(recorder.status()['ready'])
        self.assertTrue(recorder.require_paper_recovery())
        self.assertFalse(recorder.status()['ready'])
        return recorder

    @contextmanager
    def broker_responses(self, calls):
        def detail(*args, **kwargs):
            calls.append('detail')
            return []

        def executions(*args, observer):
            calls.append('executions')
            observer(self.execution_page())
            return ()

        def account(session):
            calls.append('account')
            self.now = self.t1
            return self.report('1000000')

        def pending(session, *, observer, day, integrated):
            calls.append('pending')
            self.assertTrue(integrated)
            self.assertEqual(day, self.day)
            self.now = self.pending_start + timedelta(seconds=1)
            observer(self.pending_page())
            return []

        with ExitStack() as stack:
            for name, substitute in (('_request_rows', detail), ('fetch_executions', executions),
                                     ('fetch_account_report', account), ('_pending_rows', pending)):
                stack.enter_context(patch.object(recovery, name, side_effect=substitute))
            yield

    def test_newer_holdings_tick_preserves_recovery_proof_and_rejects_bad_digest(self):
        self.tracker.record_broker_observation(self.execution_page())
        self.assertTrue(self.tracker._save_paper_balance_snapshot('paper', self.report('1000000'), self.t1,
                                                                 strict=True))
        self.tracker.record_broker_observation(self.pending_page())
        self.run_holdings_tick()
        proof = self.proof(self.t1)
        self.assertTrue(self.tracker.validate_paper_stream_recovery(proof, self.now))
        self.assertTrue(self.tracker.validate_paper_monitor_resume(proof, self.now))

        with self.tracker._db() as db:
            for statement in ('UPDATE paper_account_balance_observations SET digest=? WHERE observed_at=?',
                              'DELETE FROM paper_account_balance_observations WHERE digest=? OR observed_at=?'):
                with self.assertRaises(sqlite3.IntegrityError):
                    db.execute(statement, ('0' * 64, self.t1.isoformat()))

        # This disposable fixture simulates out-of-band file corruption.
        with self.tracker._db() as db:
            trigger = db.execute("""SELECT name FROM sqlite_master
                WHERE type='trigger' AND tbl_name='paper_account_balance_observations'
                AND UPPER(sql) LIKE '%BEFORE UPDATE%'""").fetchone()['name']
            db.execute(f'DROP TRIGGER "{trigger}"')
            db.execute('UPDATE paper_account_balance_observations SET digest=? WHERE observed_at=?',
                       ('0' * 64, self.t1.isoformat()))
        self.assertFalse(self.tracker.validate_paper_stream_recovery(proof, self.now))
        self.assertFalse(self.tracker.validate_paper_monitor_resume(proof, self.now))

    def test_busy_strict_save_cannot_be_replaced_by_later_holdings_tick(self):
        recorder = self.start_latched_recorder()
        save = self.tracker._save_paper_balance_snapshot
        connect = sqlite3.connect
        errors = []

        def connect_without_wait(*args, **kwargs):
            return connect(*args, **{**kwargs, 'timeout': 0})

        def save_while_locked(mode, report, moment, *, strict):
            self.assertTrue(strict)
            with closing(connect(self.tracker.path, timeout=0)) as writer:
                writer.execute('BEGIN IMMEDIATE')
                with patch.object(sqlite3, 'connect', side_effect=connect_without_wait):
                    try:
                        return save(mode, report, moment, strict=strict)
                    except sqlite3.OperationalError as error:
                        errors.append(error.sqlite_errorcode)
                        raise

        calls = []
        with self.broker_responses(calls), patch.object(
                self.tracker, '_save_paper_balance_snapshot', side_effect=save_while_locked):
            result = recovery.recover_paper_experiment(self.session, self.tracker, self.now, allow_legacy=True)
        self.assertEqual(calls, ['detail', 'executions', 'account'])
        self.assertEqual(errors, [sqlite3.SQLITE_BUSY])
        self.assertEqual(result['status'], 'reconciliation_unavailable')
        self.assertFalse(result['recorder_recovered'])
        self.assertNotIn('proof', result)
        self.assertFalse(recorder.status()['ready'])

        # Even complete REST pages cannot authenticate a balance that failed to commit.
        self.tracker.record_broker_observation(self.pending_page())
        self.run_holdings_tick()
        with self.tracker._db() as db:
            saved = db.execute('SELECT observed_at FROM paper_account_balance_observations').fetchall()
            self.assertEqual([row['observed_at'] for row in saved], [self.t2.isoformat()])
        self.assertFalse(self.tracker.validate_paper_stream_recovery(self.proof(self.t1), self.now))

    def test_settlement_failure_after_validation_relatches_real_recorder(self):
        recorder = self.start_latched_recorder()
        calls = []
        ready_before_settlement = []
        validate = self.tracker.validate_paper_stream_recovery

        def checked(*args, **kwargs):
            calls.append('validate')
            return validate(*args, **kwargs)

        def failed_settlement(_now):
            calls.append('settlement')
            ready_before_settlement.append(recorder.status()['ready'])
            raise sqlite3.OperationalError('isolated settlement failure')

        with self.broker_responses(calls), patch.object(
                self.tracker, 'validate_paper_stream_recovery', side_effect=checked), patch.object(
                self.tracker, 'record_paper_expired_settlements', side_effect=failed_settlement):
            result = recovery.recover_paper_experiment(self.session, self.tracker, self.now, allow_legacy=True)
        self.assertEqual(calls, ['detail', 'executions', 'account', 'pending', 'validate', 'settlement'])
        self.assertEqual(ready_before_settlement, [True])
        self.assertEqual(result['status'], 'reconciliation_unavailable')
        self.assertFalse(result['recorder_recovered'])
        self.assertFalse(result['reconciled'])
        self.assertNotIn('proof', result)
        self.assertFalse(recorder.status()['ready'])

    def test_supported_expiry_index_is_prepared_before_write_lock_and_reused(self):
        self.tracker.record_broker_observation(self.execution_page())
        self.tracker.record_broker_observation(self.pending_page())
        cache = {}
        transaction_states = []

        def sync(db, scope):
            transaction_states.append(db.in_transaction)
            if not db.in_transaction:
                cache[scope] = tuple(row['id'] for row in db.execute(
                    'SELECT id FROM broker_observation_evidence WHERE scope=? ORDER BY id', (scope,)
                ))
            return cache.get(scope)

        def capture(tracker, db, _now):
            self.assertTrue(db.in_transaction)
            entry = expired_order_certificates._sync_expiry_index(db, tracker.scope)
            self.assertIsNotNone(entry)
            self.assertIs(entry, cache[tracker.scope])
            return entry

        with patch.object(expired_order_certificates, '_sync_expiry_index', sync, create=True), patch.object(
                expired_order_certificates, 'capture_certificates', side_effect=capture):
            self.assertEqual(self.tracker.record_paper_expired_settlements(self.now), (1, 2))
            self.tracker.record_broker_observation(self.page('kt00009', 'c' * 32, self.t2))
            self.tracker.record_broker_observation(self.page('ka10075', 'd' * 32, self.t2))
            with self.tracker._db() as db:
                repeats = db.execute('SELECT id FROM broker_observation_repeats ORDER BY id').fetchall()
                self.assertEqual([row['id'] for row in repeats], [3, 4])
            self.assertEqual(self.tracker.record_paper_expired_settlements(self.now), (1, 2, 3, 4))
        self.assertEqual(transaction_states, [False, True, False, True])
