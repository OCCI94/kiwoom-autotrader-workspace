"""Temporary checkpoints and fake tracker work; no credentials or network."""
from copy import deepcopy
from datetime import datetime, timedelta
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from kiwoom_autotrader.api.session import KST
from kiwoom_autotrader.telegram import listener


NOW = datetime(2026, 11, 4, 16, 0, tzinfo=KST)


class FakeTracker:
    def __init__(self):
        self.callback = None
        self.value = {'schema':'tracker-diagnostics-status-v1', 'active':False,
                      'phase':'idle', 'started_at':None, 'last_completed_at':None}
        self.work = lambda: None

    def set_diagnostics_callback(self, callback):self.callback = callback
    def diagnostics_snapshot(self):return deepcopy(self.value)
    def tick(self, session, notify):return self.work()


class TrackerCheckpointTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'state.json'
        self.state = {'automation_status':{'mode':'paper'},
                      'stability_observation':{'observed_at':(NOW-timedelta(minutes=5)).isoformat()}}
        self.health = {'schema':'paper-monitor-tick-health-v1',
            'last_tick_completed_at':(NOW-timedelta(seconds=4)).isoformat(),
            'maximum_tick_duration_seconds':98.3}
        self.supervisor = Mock()
        self.supervisor.status.return_value = {'enabled':True, 'monitor':{'tick_health':self.health}}
        self.session = Mock(); self.session.execution_recorder.status.return_value = {'ready':True}
        self.tracker = FakeTracker()
        self.publisher = listener._TrackerCheckpointPublisher(self.path, self.state, self.tracker,
            self.supervisor, self.session, now=lambda:NOW)
        for target in ('socket.socket.connect', 'urllib.request.OpenerDirector.open'):
            guard = patch(target, side_effect=AssertionError('network forbidden'))
            self.addCleanup(guard.stop); mocked = guard.start(); self.addCleanup(mocked.assert_not_called)

    def stored(self):return json.loads(self.path.read_bytes())

    def test_before_blocking_tracker_publishes_latest_monitor_then_stage(self):
        self.publisher.bind()
        def blocking_work():
            before = self.stored()
            self.assertEqual(before['state_publication']['reason'], 'before_tracker_tick')
            self.assertEqual(before['automation_status']['paper_experiment']['monitor']['tick_health'], self.health)
            self.tracker.value.update(active=True, phase='closed_reconcile', started_at=NOW.isoformat())
            self.tracker.callback(self.tracker.diagnostics_snapshot())
            active = self.stored()
            self.assertEqual(active['state_publication']['reason'], 'tracker_stage_progress')
            self.assertTrue(active['tracker_diagnostics']['active'])
            self.assertEqual(active['tracker_diagnostics']['phase'], 'closed_reconcile')
            self.assertEqual(active['automation_status']['paper_experiment']['monitor']['tick_health'], self.health)
            self.assertEqual(active['stability_observation'], self.state['stability_observation'])
            self.assertFalse(active['state_publication']['monitor_tick_time_synthesized'])
            self.tracker.value.update(active=False, phase='idle', last_completed_at=NOW.isoformat())
        self.tracker.work = blocking_work
        self.publisher.tick(Mock())
        self.assertEqual(self.stored()['state_publication']['reason'], 'after_tracker_tick')
        self.assertEqual(self.supervisor.status.call_count, 2)  # no expensive refresh per stage

    def test_tracker_exception_still_publishes_final_snapshot_and_propagates(self):
        self.publisher.bind()
        def failure():
            self.tracker.value.update(active=False, phase='idle', last_completed_at=NOW.isoformat(),
                                      last_stage='accounting', last_success=False)
            raise RuntimeError('fixture tracker failure')
        self.tracker.work = failure
        with self.assertRaisesRegex(RuntimeError, 'fixture tracker failure'):
            self.publisher.tick(Mock())
        after = self.stored()
        self.assertEqual(after['state_publication']['reason'], 'after_tracker_tick')
        self.assertFalse(after['tracker_diagnostics']['last_success'])
        self.assertEqual(after['automation_status']['paper_experiment']['monitor']['tick_health'], self.health)

    def test_publication_failure_is_explicit_and_does_not_change_tracker_policy(self):
        self.publisher.bind(); ran = []
        self.tracker.work = lambda:ran.append(True)
        with patch.object(listener, '_save_state', side_effect=OSError('secret raw message')), patch('builtins.print'):
            self.publisher.tick(Mock())
        self.assertEqual(ran, [True])
        error = self.state['telemetry_publish_error']
        self.assertEqual(error['code'], 'tracker_checkpoint_publication_failed')
        self.assertFalse(error['financial_policy_changed'])
        self.assertNotIn('secret raw message', json.dumps(error))
        self.publisher.publish('after_tracker_tick', refresh_monitor=True)
        self.assertEqual(self.stored()['telemetry_publish_error']['failure_count'], 2)

    def test_callback_failure_is_exposed_to_diagnostics_owner(self):
        self.publisher.bind()
        with patch.object(listener, '_save_state', side_effect=OSError), patch('builtins.print'):
            with self.assertRaises(OSError):self.tracker.callback(self.tracker.diagnostics_snapshot())
        self.assertIn('telemetry_publish_error', self.state)

    def test_foreign_thread_does_not_publish_listener_checkpoint(self):
        self.publisher.bind(); self.publisher.publish('before_tracker_tick', refresh_monitor=True)
        before = self.path.read_bytes()
        thread = threading.Thread(target=lambda:self.tracker.callback(self.tracker.diagnostics_snapshot()))
        thread.start(); thread.join(timeout=2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(self.path.read_bytes(), before)

    def test_substage_callback_does_not_enter_monitor_recorder_or_diagnostics_locks(self):
        self.publisher.bind(); self.publisher.publish('before_tracker_tick', refresh_monitor=True)
        recording_clock=self.stored()['recorder_snapshot_observed_at']
        self.publisher.now=lambda:NOW+timedelta(seconds=200)
        self.tracker.value.update(active=True, phase='history_export', started_at=NOW.isoformat())
        snapshot=self.tracker.diagnostics_snapshot()
        with patch.object(self.session.execution_recorder,'status',side_effect=AssertionError('recorder lock')) as recorder, \
             patch.object(self.supervisor,'status',side_effect=AssertionError('monitor lock')) as monitor, \
             patch.object(self.tracker,'diagnostics_snapshot',side_effect=AssertionError('diagnostics lock')) as diagnostics:
            self.tracker.callback(snapshot)
            recorder.assert_not_called(); monitor.assert_not_called(); diagnostics.assert_not_called()
        self.assertEqual(self.stored()['tracker_diagnostics']['phase'],'history_export')
        self.assertEqual(self.stored()['recorder_snapshot_observed_at'],recording_clock)
        self.assertEqual(self.stored()['state_publication']['published_at'],(NOW+timedelta(seconds=200)).isoformat())

    def test_legacy_tracker_without_diagnostic_methods_still_runs(self):
        class Legacy:
            def tick(self, session, notify):return 7
        publisher = listener._TrackerCheckpointPublisher(self.path, self.state, Legacy(),
            self.supervisor, self.session, now=lambda:NOW)
        publisher.bind()
        self.assertEqual(publisher.tick(Mock()), 7)
        self.assertNotIn('tracker_diagnostics', self.stored())

    def test_real_diagnostics_stage_callback_publishes_during_and_after_exception(self):
        from kiwoom_autotrader.trading.tracker_diagnostics import TrackerDiagnostics
        diagnostic = TrackerDiagnostics(Path(self.tmp.name)/'diagnostics', now=lambda:NOW,
            monotonic=iter([10.0, 22.0]).__next__,
            rss_reader=lambda:{'current_rss_bytes':1000,'peak_rss_bytes':2000})
        self.tracker.set_diagnostics_callback = diagnostic.set_progress_callback
        self.tracker.diagnostics_snapshot = diagnostic.snapshot
        self.publisher.bind()
        def failure():
            with diagnostic.stage('accounting', rows=9):
                active = self.stored()
                self.assertEqual(active['tracker_diagnostics']['phase'], 'accounting')
                self.assertEqual(active['tracker_diagnostics']['started_at'], NOW.isoformat())
                self.assertEqual(active['automation_status']['paper_experiment']['monitor']['tick_health'], self.health)
                raise RuntimeError('fixture accounting failure')
        self.tracker.work = failure
        with self.assertRaisesRegex(RuntimeError,'fixture accounting failure'):
            self.publisher.tick(Mock())
        final = self.stored()
        self.assertEqual(final['state_publication']['reason'],'after_tracker_tick')
        self.assertFalse(final['tracker_diagnostics']['active'])
        self.assertEqual(final['tracker_diagnostics']['last_duration_seconds'],12.0)
        self.assertEqual(final['automation_status']['paper_experiment']['monitor']['tick_health']['maximum_tick_duration_seconds'],98.3)

    def test_restart_discards_previous_process_publication_and_tracker_state(self):
        config = listener.TelegramConfig('123456789:fixture_token_abcdefghijklmnopqrstuvwxyz',
                                        778899001, 123456789, 'fixture_bot')
        state = listener._new_state(config, 1000)
        state.update(state_publication={'published_at':NOW.isoformat()},
                     tracker_diagnostics={'active':True}, telemetry_publish_error={'code':'old'},
                     recorder_snapshot_observed_at=NOW.isoformat())
        listener._save_state(self.path, state)
        loaded = listener._load_state(self.path, config, 2000)
        for key in ('state_publication', 'tracker_diagnostics', 'telemetry_publish_error',
                    'recorder_snapshot_observed_at'):
            self.assertNotIn(key, loaded)


if __name__ == '__main__':unittest.main()
