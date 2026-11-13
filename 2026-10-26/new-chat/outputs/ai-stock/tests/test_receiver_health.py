from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

from kiwoom_autotrader.telegram.receiver_health import maintenance_status, ErrorReminder, PreopenStreamCheck

KST=ZoneInfo('Asia/Seoul')


class ReceiverHealthTests(TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.now=datetime(2026,11,4,8,50,tzinfo=KST)

    def marker(self,body=''):
        path=self.root/'maintenance-quiet.lock';path.write_text(body)
        os.utime(path,(self.now.timestamp(),self.now.timestamp()))
        return path

    def test_empty_marker_expires_without_deletion_and_io_error_stays_closed(self):
        path=self.marker()
        self.assertTrue(maintenance_status(self.root,now=self.now)['paused'])
        self.assertFalse(maintenance_status(self.root,now=self.now+timedelta(minutes=10))['paused'])
        self.assertTrue(path.exists())
        with patch('kiwoom_autotrader.telegram.receiver_health.os.lstat',side_effect=PermissionError):
            self.assertTrue(maintenance_status(self.root,now=self.now)['paused'])

    def test_explicit_expiry_can_only_shorten_and_symlink_is_not_read(self):
        path=self.marker(json.dumps({'expires_at':(self.now+timedelta(days=1)).isoformat()}))
        self.assertFalse(maintenance_status(self.root,now=self.now+timedelta(minutes=10))['paused'])
        self.marker(json.dumps({'expires_at':(self.now+timedelta(seconds=30)).isoformat()}))
        self.assertFalse(maintenance_status(self.root,now=self.now+timedelta(seconds=30))['paused'])
        path.unlink();path.symlink_to(self.root/'missing')
        self.assertTrue(maintenance_status(self.root,now=self.now)['paused'])

    def test_immediate_error_transition_and_ten_minute_repeat(self):
        reminder=ErrorReminder()
        self.assertFalse(reminder.due(None,0))
        self.assertTrue(reminder.due('storage',1))
        self.assertFalse(reminder.due('storage',600))
        self.assertTrue(reminder.due('storage',601))
        self.assertTrue(reminder.due('socket',602))
        self.assertFalse(reminder.due(None,603))
        self.assertTrue(reminder.due('socket',604))

    def checker(self):
        self.calendar=Mock(return_value={'session_day':'20261104','start':self.now.replace(hour=9,minute=0)})
        self.recorder=Mock();self.recorder.status.return_value={'ready':True,'error':None}
        self.session=SimpleNamespace(mode='paper',execution_recorder=self.recorder)
        self.tracker=Mock();self.tracker.stream_anchor.return_value=(1,'a'*64,self.now.isoformat())
        self.notify=Mock();return PreopenStreamCheck(self.calendar)

    def test_preopen_current_ledger_lifecycle_and_utc_clock(self):
        check=self.checker()
        result=check.tick(self.session,self.tracker,self.now.astimezone(timezone.utc),self.notify)
        self.assertTrue(result['ready']);self.notify.assert_called_once()
        check.tick(self.session,self.tracker,self.now+timedelta(seconds=20),self.notify)
        self.tracker.stream_anchor.assert_called_once()
        self.recorder.require_paper_recovery.assert_not_called()

    def test_prior_day_or_missing_lifecycle_latches_existing_recovery_no_fake_ready(self):
        check=self.checker()
        self.tracker.stream_anchor.return_value=(1,'a'*64,(self.now-timedelta(days=1)).isoformat())
        result=check.tick(self.session,self.tracker,self.now,self.notify)
        self.assertFalse(result['ready']);self.assertEqual(result['reason'],'current_day_lifecycle_unverified')
        self.recorder.require_paper_recovery.assert_called_once()
        self.tracker.stream_anchor.return_value=(2,'b'*64,(self.now+timedelta(seconds=30)).isoformat())
        result=check.tick(self.session,self.tracker,self.now+timedelta(minutes=1),self.notify)
        self.assertTrue(result['ready']);self.assertEqual(self.notify.call_count,2)

    def test_missing_calendar_real_mode_and_outside_window_never_recover(self):
        check=self.checker();self.calendar.return_value=None
        result=check.tick(self.session,self.tracker,self.now,self.notify)
        self.assertEqual(result['reason'],'official_session_unverified')
        self.recorder.require_paper_recovery.assert_not_called();self.tracker.stream_anchor.assert_not_called()
        check=self.checker();self.session.mode='real'
        self.assertEqual(check.tick(self.session,self.tracker,self.now,self.notify)['reason'],'not_paper')
        self.recorder.require_paper_recovery.assert_not_called()
        check=self.checker()
        self.assertIsNone(check.tick(self.session,self.tracker,self.now.replace(hour=9),self.notify))
        self.calendar.assert_not_called()
