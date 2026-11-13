"""Listener routing and retry contracts; no operational services or network."""
from datetime import datetime
from types import SimpleNamespace
from unittest import TestCase
from pathlib import Path
import tempfile
from kiwoom_autotrader.trading.legacy_resume_gate import LegacyResumeGate
from unittest.mock import Mock
from zoneinfo import ZoneInfo
from kiwoom_autotrader.telegram.listener import _ExperimentSupervisor

class ExperimentListenerSupervisorTests(TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.transition=LegacyResumeGate(Path(self.temp.name)/"transition.json")
        self.elapsed=0.0;self.options={'paper_experiment_enabled':True}
        self.monitor=Mock();self.monitor.status.return_value={'quarantined':{},'symbol_diagnostics':{}}
        self.monitor.seconds_until_next.return_value=1
        self.ttl=Mock();self.ttl.last_status={'status':'no_due_owned_limit_buy'}
        self.recover=Mock(return_value={'reconciled':True,'remaining_registered_pending':0,'recorder_recovered':False})
        self.settings=Mock(side_effect=lambda:self.options)
        self.prior_scheduler=Mock();self.prior_scheduler.status.return_value={"status":"waiting"}
        self.supervisor=_ExperimentSupervisor(self.monitor,ttl=self.ttl,recovery=self.recover,prior_day_scheduler=self.prior_scheduler,
            transition_gate=self.transition,clock=lambda:self.elapsed,settings=self.settings,now=lambda:datetime(2026,11,2,10,0,tzinfo=ZoneInfo('Asia/Seoul')),launcher=lambda fn:fn())
        self.session=SimpleNamespace(mode='paper',execution_recorder=None);self.tracker=Mock();self.notify=Mock()
    def tick(self):return self.supervisor.tick(self.session,self.tracker,self.notify,lambda:False)
    def test_real_session_does_not_read_experiment_settings_or_use_tools(self):
        self.session.mode='real';self.assertFalse(self.tick());self.settings.assert_not_called()
        self.ttl.tick.assert_not_called();self.monitor.tick.assert_not_called();self.recover.assert_not_called()
    def test_disabled_mode_runs_no_experiment_or_separate_ttl(self):
        self.options['paper_experiment_enabled']=False;self.assertFalse(self.tick())
        self.ttl.tick.assert_not_called();self.monitor.tick.assert_not_called()
    def test_enabled_runs_independent_ttl_and_monitor_even_when_old_gdcrs_is_off(self):
        self.options['gdcrs_enabled']=False;self.assertTrue(self.tick())
        self.ttl.tick.assert_called_once();self.assertEqual(self.ttl.tick.call_args.args[2],180)
        self.monitor.tick.assert_called_once();self.recover.assert_called_once()
    def test_failed_recovery_retries_only_after_60_seconds(self):
        self.recover.return_value={'reconciled':False}
        self.tick();self.elapsed=59;self.tick();self.assertEqual(self.recover.call_count,1)
        self.elapsed=60;self.tick();self.assertEqual(self.recover.call_count,2)
    def test_unresolved_order_rechecks_at_60_seconds_without_any_order_resend(self):
        self.recover.return_value={'reconciled':True,'remaining_registered_pending':1}
        self.tick();self.elapsed=60;self.tick();self.assertEqual(self.recover.call_count,2)
        self.assertEqual(self.monitor.tick.call_count,2)
    def test_invalid_setting_suppresses_legacy_without_order(self):
        self.options['paper_experiment_enabled']='yes';self.assertTrue(self.tick())
        self.ttl.tick.assert_not_called();self.monitor.tick.assert_not_called()
    def test_stop_suppresses_every_experiment_action(self):
        self.assertTrue(self.supervisor.tick(self.session,self.tracker,self.notify,lambda:True))
        self.ttl.tick.assert_not_called();self.recover.assert_not_called();self.monitor.tick.assert_not_called()
    def test_ttl_error_notification_is_deduplicated(self):
        self.ttl.last_status={'status':'retry_pending','reason':'source_unavailable'}
        self.tick();self.tick();self.notify.assert_called_once()

    def test_auction_sell_cancel_issue_is_reported_once_without_resend(self):
        self.ttl.last_sell_status={'status':'retry_pending','reason':'sell_cancel_pending_unverified'}
        self.tick();self.tick()
        self.notify.assert_called_once()
        self.assertEqual(self.supervisor.status()['sell_auction_cancellation']['reason'],'sell_cancel_pending_unverified')

    def test_monitor_exception_does_not_stop_listener_or_resume_legacy(self):
        self.monitor.tick.side_effect=RuntimeError('private details not exposed')
        self.assertTrue(self.tick());self.assertTrue(self.tick())
        self.assertEqual(self.supervisor.failure,'experiment_monitor_retry_pending')
        self.notify.assert_called_once()

    def test_slow_recovery_does_not_block_monitor_or_start_duplicate_worker(self):
        workers=[];self.supervisor.launcher=lambda fn:workers.append(fn)
        self.assertTrue(self.tick());self.assertEqual(len(workers),1);self.assertEqual(self.monitor.tick.call_count,1)
        self.elapsed=120;self.assertTrue(self.tick());self.assertEqual(len(workers),1);self.assertEqual(self.monitor.tick.call_count,2)
        self.assertTrue(self.supervisor.status()['recovery_inflight'])
        workers[0]();self.assertFalse(self.supervisor.status()['recovery_inflight'])

    def test_broken_deadline_reader_retries_without_terminating_listener(self):
        self.tick();self.monitor.seconds_until_next.side_effect=RuntimeError('bad clock')
        self.assertEqual(self.supervisor.seconds_until_next(),1)

    def test_mode_off_never_silently_resumes_legacy_before_explicit_confirmation(self):
        self.tick();self.monitor.tick.reset_mock();self.options['paper_experiment_enabled']=False
        self.assertTrue(self.tick());self.monitor.tick.assert_not_called()
        self.assertEqual(self.supervisor.failure,'legacy_resume_confirmation_required')
        token=self.transition.status(self.options)['confirmation_token']
        self.transition.confirm(self.options,token)
        self.assertFalse(self.tick())

    def test_prior_day_scheduler_runs_without_startup_error_and_is_separate_status(self):
        self.tick();self.prior_scheduler.tick.assert_called_once()
        self.elapsed=1;self.tick();self.assertEqual(self.prior_scheduler.tick.call_count,2)
        self.assertEqual(self.supervisor.status()['prior_day_reconciliation'],{'status':'waiting'})

    def test_prior_day_scheduler_waits_for_other_recovery(self):
        workers=[];self.supervisor.launcher=workers.append
        self.tick();self.prior_scheduler.tick.assert_not_called()
        workers[0]();self.tick();self.prior_scheduler.tick.assert_called_once()
