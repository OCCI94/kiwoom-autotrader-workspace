"""Safety tests for opt-in golden-cross entry filters; never touch live state."""
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase, mock
import json, sqlite3, tempfile

from kiwoom_autotrader.api.session import KST
from kiwoom_autotrader.config.settings import default_settings, _validate, SettingsError
from kiwoom_autotrader.trading.reentry_cooldown import reentry_allowed, CooldownUnavailable
from tests.test_golden_cross import GoldenCrossTests


class SettingsTests(TestCase):
    def test_defaults_and_strict_ranges(self):
        original = default_settings()
        self.assertEqual(original['gdcrs_min_cross_bps'], 0)
        self.assertEqual(original['gdcrs_reentry_cooldown_minutes'], 0)
        for key, invalid in [('gdcrs_min_cross_bps', True),('gdcrs_min_cross_bps',-1),
                             ('gdcrs_min_cross_bps',10001),('gdcrs_reentry_cooldown_minutes',1441)]:
            with self.subTest(key=key,value=invalid), self.assertRaises(SettingsError):
                _validate({**original,key:invalid})

class ObservationTests(TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'ledger.sqlite3'
        self.now=datetime(2026,10,28,10,20,tzinfo=KST)
        with sqlite3.connect(self.path) as db:
            db.execute('CREATE TABLE broker_observations(id INTEGER PRIMARY KEY, scope TEXT, day TEXT, mode TEXT, source_api TEXT, observed_at TEXT, payload_json TEXT)')
        self.tracker=SimpleNamespace(path=self.path,scope='default')
        self.recorder=SimpleNamespace(status=lambda:{'ready':True,'error':None,'gap_count':0,'observations':0,
            'ingress_sequence':0,'engine_ingress_sequence':0,'inflight_callbacks':0,'engine_pending_callbacks':0})
        self.session=SimpleNamespace(mode='paper',paper_automation_reconcile=SimpleNamespace(_tracker=self.tracker),execution_recorder=self.recorder)
    def row(self, kind, minute, values=None, mode='paper', scope='default'):
        t=(self.now+timedelta(minutes=minute)).isoformat()
        obj={'source_api':'00','source_kind':'broker_observation','mode':mode,'observed_at':t,'event_kind':kind,'values':values or {}}
        with sqlite3.connect(self.path) as db:
            db.execute('INSERT INTO broker_observations(scope,day,mode,source_api,observed_at,payload_json) VALUES (?,?,?,?,?,?)',(scope,'20261028',mode,'00',t,json.dumps(obj)))
    def test_sell_or_partial_blocks_then_expires_at_boundary(self):
        self.row('stream_started',-30)
        self.row('execution_stream',-5,{'9001':'A005930','907':'1','915':'2'})
        self.assertFalse(reentry_allowed(self.session,'005930',self.now,10))
        self.assertTrue(reentry_allowed(self.session,'005930',self.now+timedelta(minutes=5),10))
    def test_other_symbol_and_mode_do_not_block(self):
        self.row('stream_started',-30)
        self.row('execution_stream',-5,{'9001':'A000660','907':'1','915':'2'})
        self.row('execution_stream',-4,{'9001':'A005930','907':'2','915':'1'})
        self.assertTrue(reentry_allowed(self.session,'005930',self.now,10))
    def test_gap_or_recent_start_fail_closed(self):
        self.row('stream_started',-30); self.row('stream_gap',-6); self.row('stream_resumed',-4)
        with self.assertRaises(CooldownUnavailable): reentry_allowed(self.session,'005930',self.now,10)
        self.assertTrue(reentry_allowed(self.session,'005930',self.now+timedelta(minutes=6),10))
    def test_unknown_fill_side_and_unready_fail_closed(self):
        self.row('stream_started',-30); self.row('execution_stream',-3,{'9001':'A005930','915':'1'})
        with self.assertRaises(CooldownUnavailable): reentry_allowed(self.session,'005930',self.now,10)
        self.recorder.status=lambda:{'ready':False}
        with self.assertRaises(CooldownUnavailable): reentry_allowed(self.session,'005930',self.now,10)
    def test_default_never_accesses_ledger(self):
        self.assertTrue(reentry_allowed(None,'anything',self.now,0))
    def test_callback_sequence_change_during_read_fails_closed(self):
        self.row('stream_started',-30)
        base=self.recorder.status()
        self.recorder.status=mock.Mock(side_effect=[base,{**base,'engine_ingress_sequence':1}])
        with self.assertRaises(CooldownUnavailable): reentry_allowed(self.session,'005930',self.now,10)

class FilterIntegrationTests(GoldenCrossTests):
    def test_bps_threshold_at_cross_and_default_compatibility(self):
        # SMA1=4, SMA3=7/3, difference/long average = 7142.857...bp.
        self.settings['gdcrs_min_cross_bps']=7143
        self.seed(); self.cross(); self.buy.assert_not_called()
        self.settings['gdcrs_min_cross_bps']=7142
        self.monitor=self.make_monitor(); self.now=self.now.replace(minute=3)
        self.seed(); self.cross(); self.buy.assert_called_once()
    def test_cooldown_skip_is_before_marker_and_rechecked_at_send(self):
        self.settings['gdcrs_reentry_cooldown_minutes']=10
        checker=mock.Mock(return_value=False)
        self.monitor=self.make_monitor(cooldown_check=checker)
        self.seed(); self.cross(); self.buy.assert_not_called()
        # A filtered signal has an audit row, but never claims an order.
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute("SELECT skip_reason FROM entry_decisions").fetchone()[0], 'reentry_cooldown')
            self.assertEqual(db.execute("SELECT count(*) FROM sqlite_master WHERE name='attempts'").fetchone()[0], 0)
        checker.assert_called_once()
    def test_unknown_cooldown_data_blocks(self):
        self.settings['gdcrs_reentry_cooldown_minutes']=10
        self.seed(); self.cross(); self.buy.assert_not_called()
        self.assertIn('005930',self.monitor.status()['symbol_errors'])
