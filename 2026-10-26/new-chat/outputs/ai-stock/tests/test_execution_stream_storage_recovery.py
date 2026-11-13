"""Reconnect persistence failures never turn metadata into order permission."""
from copy import deepcopy
from datetime import datetime, timedelta
import json
import sqlite3
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

from kiwoom_autotrader.realtime.execution_stream import ExecutionStreamRecorder
from kiwoom_autotrader.trading import paper_experiment_recovery as recovery
from tests.test_execution_stream import FakeEngine, row


class StorageRecoveryTests(TestCase):
    def setUp(self):
        guard=patch('socket.socket.connect',side_effect=AssertionError('offline'))
        guard.start();self.addCleanup(guard.stop)
        self.now=datetime(2026,11,3,8,0,tzinfo=ZoneInfo('Asia/Seoul'))
        self.engine=FakeEngine()
        self.session=SimpleNamespace(mode='paper',token='fixture-private',token_usable=True,
                                     websocket_engine=self.engine)
        self.events=[];self.attempts=[];self.fail_kind=None;self.commit_before_failure=False
        self.tracker=Mock()
        self.tracker.record_broker_observation.side_effect=self.store
        self.tracker.stream_anchor.side_effect=self.anchor
        self.tracker.validate_paper_stream_recovery.return_value=True
        self.recorder=ExecutionStreamRecorder(self.session,self.tracker,now=lambda:self.now,poll_interval=5)
        self.session.execution_recorder=self.recorder
        self.addCleanup(self.recorder.stop)
        self.recorder.start();self.engine.ready=True
        self.assertTrue(self.recorder.status()['ready'])

    def store(self,event):
        self.attempts.append(event['event_kind'])
        if self.fail_kind==event['event_kind']:
            self.fail_kind=None
            if self.commit_before_failure:self.events.append(deepcopy(event))
            raise OSError('fixture-private account-number raw-response')
        self.events.append(deepcopy(event))

    def anchor(self,now):
        for i in range(len(self.events)-1,-1,-1):
            event=self.events[i]
            if event['event_kind'] in ('stream_started','stream_resumed'):
                return i+1,'a'*64,event['observed_at']
            if event['event_kind'] in ('stream_gap','stream_stopped'):
                raise ValueError('gap')
        raise ValueError('no_anchor')

    def fail_resume(self):
        self.engine.ready=False;self.recorder.status()
        self.engine.generation+=1;self.engine.ready=True;self.fail_kind='stream_resumed'
        self.now+=timedelta(seconds=1)
        self.assertFalse(self.recorder.status()['ready'])

    def test_failed_resume_is_not_acknowledged_or_retried_by_status(self):
        self.fail_resume()
        self.assertFalse(self.recorder._was_ready)
        before=len(self.attempts)
        for _ in range(5):self.assertFalse(self.recorder.status()['ready'])
        self.assertEqual(len(self.attempts),before)
        self.assertEqual(self.events[-1]['event_kind'],'stream_gap')
        diagnostic=self.recorder.status()['last_storage_failure']
        self.assertEqual(diagnostic['event_kind'],'stream_resumed')
        self.assertEqual(diagnostic['category'],'os_error')
        self.assertEqual(diagnostic['exception_type'],'OSError')
        self.assertEqual(diagnostic['failed_at'],self.now.isoformat())
        self.assertEqual(diagnostic['safe_message'],'local I/O failed')
        for secret in ('fixture-private','account-number','raw-response'):
            self.assertNotIn(secret,json.dumps(self.recorder.status()))

    def test_explicit_recovery_persists_gap_and_resume_but_stays_blocked_until_full_proof(self):
        self.fail_resume();self.now+=timedelta(seconds=1)
        checkpoint=self.recorder.begin_paper_recovery()
        self.assertEqual([e['event_kind'] for e in self.events],
                         ['stream_started','stream_gap','stream_gap','stream_resumed'])
        self.assertFalse(self.recorder.status()['ready'])
        self.tracker.validate_paper_stream_recovery.return_value=False
        self.assertFalse(self.recorder.complete_paper_recovery(checkpoint,{}))
        self.assertFalse(self.recorder.status()['ready'])
        self.tracker.validate_paper_stream_recovery.return_value=True
        self.assertTrue(self.recorder.complete_paper_recovery(checkpoint,{'fixture':'verified_pages'}))
        self.tracker.validate_paper_stream_recovery.assert_called_with(
            {'fixture':'verified_pages'},self.now,minimum_started_at=self.now)
        self.assertTrue(self.recorder.status()['ready'])
        self.assertFalse(self.recorder.status()['coverage_complete'])
        self.assertFalse(self.recorder.complete_paper_recovery(checkpoint,{'fixture':'verified_pages'}))

    def test_gap_or_resume_write_failure_aborts_recovery_without_false_ready(self):
        for fail in ('stream_gap','stream_resumed'):
            with self.subTest(fail=fail):
                self.fail_resume();self.fail_kind=fail
                with self.assertRaises(ValueError):self.recorder.begin_paper_recovery()
                self.assertFalse(self.recorder.status()['ready'])
                self.assertIsNone(self.recorder._recovery_checkpoint)
                self.assertFalse(self.recorder.complete_paper_recovery(('forged',),{}))
                # A later explicit attempt may recover, while ordinary polls
                # cannot silently substitute for the broker reconciliation.
                checkpoint=self.recorder.begin_paper_recovery()
                self.assertFalse(self.recorder.status()['ready'])
                self.assertTrue(self.recorder.complete_paper_recovery(checkpoint,{}))

    def test_commit_then_materialization_failure_still_requires_a_new_full_recovery(self):
        self.commit_before_failure=True;self.fail_resume()
        self.assertEqual(self.events[-1]['event_kind'],'stream_resumed')
        self.assertFalse(self.recorder._was_ready)
        old_anchor=self.anchor(self.now)[0]
        checkpoint=self.recorder.begin_paper_recovery()
        self.assertGreater(checkpoint[-1],old_anchor)
        self.assertFalse(self.recorder.status()['ready'])

    def test_stale_ledger_anchor_is_rejected(self):
        old=self.anchor(self.now)
        self.fail_resume();self.now+=timedelta(seconds=1)
        self.tracker.stream_anchor.side_effect=lambda now:old
        with self.assertRaises(ValueError):self.recorder.begin_paper_recovery()
        self.assertIsNone(self.recorder._recovery_checkpoint)
        self.assertFalse(self.recorder.status()['ready'])

    def test_new_callback_or_generation_or_storage_error_invalidates_checkpoint(self):
        for change in ('generation','callback','storage'):
            with self.subTest(change=change):
                self.recorder._error='prior failure'
                checkpoint=self.recorder.begin_paper_recovery()
                if change=='generation':self.engine.generation+=1
                else:
                    if change=='storage':self.fail_kind='execution_stream'
                    self.engine.emit(row())
                self.assertFalse(self.recorder.complete_paper_recovery(checkpoint,{}))
                self.assertFalse(self.recorder.status()['ready'])

    def test_real_and_pending_callback_recovery_do_not_create_new_anchors(self):
        self.fail_resume();before=len(self.events)
        self.session.mode='real'
        with self.assertRaises(ValueError):self.recorder.begin_paper_recovery()
        self.session.mode='paper'
        self.engine.delivery[self.recorder._handle.id]['pending_callbacks']=1
        with self.assertRaises(ValueError):self.recorder.begin_paper_recovery()
        self.assertEqual(len(self.events),before)
        self.engine.delivery[self.recorder._handle.id]['pending_callbacks']=0

    def test_wrapped_sqlite_failure_category_does_not_expose_exception_text(self):
        def fail(event):
            try:
                error=sqlite3.OperationalError('fixture-private')
                error.sqlite_errorcode=sqlite3.SQLITE_BUSY
                raise error
            except sqlite3.Error:
                raise RuntimeError('account-number') from None
        self.tracker.record_broker_observation.side_effect=fail
        self.engine.emit(row())
        diagnostic=self.recorder.status()['last_storage_failure']
        self.assertEqual(diagnostic['event_kind'],'execution_stream')
        self.assertEqual(diagnostic['category'],'sqlite_busy')
        self.assertEqual(diagnostic['safe_message'],'database is busy')
        self.assertNotIn('private',json.dumps(self.recorder.status()))

    def test_preopen_missing_anchor_requires_full_recovery_not_status_clear(self):
        self.assertTrue(self.recorder.require_paper_recovery())
        self.assertFalse(self.recorder.status()['ready'])
        checkpoint=self.recorder.begin_paper_recovery()
        self.assertFalse(self.recorder.status()['ready'])
        self.assertTrue(self.recorder.complete_paper_recovery(checkpoint,{}))
        self.assertTrue(self.recorder.status()['ready'])
        self.assertFalse(self.recorder.status()['coverage_complete'])

    def test_real_sqlite_lock_on_resume_is_observable_and_recovers_after_unlock(self):
        with tempfile.TemporaryDirectory() as tmp:
            db=sqlite3.connect(Path(tmp)/'fixture.db',timeout=0)
            blocker=sqlite3.connect(Path(tmp)/'fixture.db',timeout=0)
            try:
                db.execute('CREATE TABLE lifecycle(kind TEXT)');db.commit()
                blocker.execute('BEGIN EXCLUSIVE')
                def record(event):
                    if event['event_kind']=='stream_resumed':
                        db.execute('INSERT INTO lifecycle VALUES (?)',(event['event_kind'],));db.commit()
                    self.store(event)
                self.tracker.record_broker_observation.side_effect=record
                self.engine.ready=False;self.recorder.status()
                self.engine.ready=True;self.engine.generation+=1;self.now+=timedelta(seconds=1)
                self.assertFalse(self.recorder.status()['ready'])
                self.assertFalse(self.recorder._was_ready)
                self.assertEqual(self.recorder.status()['last_storage_failure']['category'],'sqlite_busy')
                blocker.rollback()
                checkpoint=self.recorder.begin_paper_recovery()
                self.assertFalse(self.recorder.status()['ready'])
                self.assertTrue(self.recorder.complete_paper_recovery(checkpoint,{}))
                self.assertTrue(self.recorder.status()['ready'])
                self.assertEqual(db.execute('SELECT COUNT(*) FROM lifecycle').fetchone()[0],1)
            finally:
                self.tracker.record_broker_observation.side_effect=self.store
                blocker.close();db.close()

    def test_existing_recovery_orchestrator_fetches_all_broker_evidence_before_release(self):
        self.fail_resume();calls=[]
        self.tracker.settings_loader.return_value={'paper_experiment_enabled':True}
        self.tracker._moment.side_effect=lambda:self.now
        db=Mock();dbctx=Mock();dbctx.__enter__=Mock(return_value=db);dbctx.__exit__=Mock(return_value=False)
        self.tracker._db.return_value=dbctx;self.tracker._pending_order_rows.return_value=[]
        def detail(*args,**kwargs):calls.append('detail');return []
        def executions(*args,observer,**kwargs):
            calls.append('executions');observer({'source_api':'kt00009','query_id':'a'*32});return ()
        def account(*args):calls.append('account');return {'mode':'paper'}
        def pending(*args,observer,**kwargs):
            calls.append('pending');observer({'source_api':'ka10075','query_id':'b'*32});return []
        def observe(event):
            if event.get('source_api')=='00':self.store(event)
        self.tracker.record_broker_observation.side_effect=observe
        def validate(proof,now,*,minimum_started_at):
            calls.append('validate')
            return calls==['detail','executions','account','pending','validate'] and minimum_started_at==self.now
        self.tracker.validate_paper_stream_recovery.side_effect=validate
        with patch.object(recovery,'session_identity',return_value=('paper','fixture-private')), \
                patch.object(recovery,'_prior_days',return_value=[]), \
                patch.object(recovery,'_request_rows',side_effect=detail), \
                patch.object(recovery,'fetch_executions',side_effect=executions), \
                patch.object(recovery,'fetch_account_report',side_effect=account), \
                patch.object(recovery,'_pending_rows',side_effect=pending):
            result=recovery.recover_paper_experiment(self.session,self.tracker,self.now)
        self.assertTrue(result['recorder_recovered']);self.assertEqual(result['orders_sent'],0)
        self.assertEqual(calls,['detail','executions','account','pending','validate'])
        self.assertTrue(self.recorder.status()['ready'])
        self.assertFalse(self.recorder.status()['coverage_complete'])
