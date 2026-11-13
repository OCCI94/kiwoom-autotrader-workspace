"""Offline KST initial-entry windows; no operational files or broker requests."""
from datetime import datetime,timedelta,timezone
from pathlib import Path
import json
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock

from kiwoom_autotrader.api.minute_chart import MinuteBar
from kiwoom_autotrader.config.settings import (SettingsError,default_settings,_validate,
    load_settings,save_paper_strategy_options,paper_strategy_options_path,effective_entry_time_window)
from kiwoom_autotrader.telegram.commands.orders import OrderReplies
from kiwoom_autotrader.trading.order_context import automated_orders,_current
from kiwoom_autotrader.api.session import KST
import tests.test_golden_cross as golden_fixture
import tests.test_cross_monitor as sell_fixture
import tests.test_b3_monitor as b3_fixture


class EntryTimeWindowTests(unittest.TestCase):
    setUp=golden_fixture.GoldenCrossTests.setUp
    make_monitor=golden_fixture.GoldenCrossTests.make_monitor
    tick=golden_fixture.GoldenCrossTests.tick

    def cross_at(self,at):
        self.now=at-timedelta(minutes=1)
        start=at.astimezone(KST).replace(second=0,microsecond=0)-timedelta(minutes=4)
        self.chart.return_value=tuple(MinuteBar(start+timedelta(minutes=i),price) for i,price in enumerate((3,2,1)))
        self.tick()
        self.now=at
        self.chart.return_value=tuple(MinuteBar(start+timedelta(minutes=i),price) for i,price in enumerate((3,2,1,4)))
        self.tick()

    def test_default_and_custom_start_inclusive_end_exclusive_at_decision_time(self):
        for start,end,hour,minute,allowed in [('09:00','15:20',9,4,True),
                ('09:00','15:20',15,19,True),('09:00','15:20',15,20,False),
                ('10:30','14:00',10,29,False),('10:30','14:00',10,30,True),
                ('10:30','14:00',13,59,True),('10:30','14:00',14,0,False)]:
            with self.subTest(start=start,end=end,hour=hour,minute=minute):
                self.settings.update(gdcrs_entry_start_time=start,gdcrs_entry_end_time=end)
                self.path=Path(self.temp.name)/f'{start.replace(":","")}-{hour}-{minute}.db'
                self.buy.reset_mock();self.monitor=self.make_monitor()
                self.cross_at(datetime(2026,10,26,hour,minute,5,tzinfo=KST))
                self.assertEqual(self.buy.call_count,int(allowed))
                if not allowed:
                    last=self.monitor.status()['last_entry_decision']
                    self.assertEqual(last['skip_reason'],'outside_entry_time_window')
                    self.assertEqual(last['observed_at'],self.now.isoformat())
                    with sqlite3.connect(self.path) as db:
                        self.assertEqual(db.execute('SELECT skip_reason FROM entry_decisions').fetchone()[0],'outside_entry_time_window')
                        self.assertIsNone(db.execute("SELECT 1 FROM sqlite_master WHERE name='attempts'").fetchone())

    def test_0900_boundary_does_not_replay_previous_day_or_fabricate_warmup(self):
        self.now=datetime(2026,10,26,9,0,tzinfo=KST)
        context=self.monitor._context(self.session,self.now)
        self.assertTrue(self.monitor._entry_window_open(self.now,context))
        self.assertFalse(self.monitor._entry_window_open(self.now-timedelta(seconds=1),context))
        self.chart.return_value=golden_fixture.bars((3,2,1,4),day=25)
        self.tick();self.buy.assert_not_called()

    def test_kst_conversion_uses_decision_time_not_candle_start(self):
        self.settings.update(gdcrs_entry_start_time='10:30',gdcrs_entry_end_time='14:00')
        # The latest completed candle begins 10:29, but the decision is 10:30 KST.
        self.cross_at(datetime(2026,10,26,1,30,5,tzinfo=timezone.utc))
        self.buy.assert_called_once()

    def test_invalid_format_equal_reversed_or_nonoverlapping_window_fail_closed(self):
        baseline=dict(self.settings)
        for changes in ({'gdcrs_entry_start_time':'9:00'},{'gdcrs_entry_end_time':'24:00'},
                {'gdcrs_entry_start_time':None},{'gdcrs_entry_end_time':900},
                {'gdcrs_entry_start_time':True},{'gdcrs_entry_start_time':'09:00:00'},
                {'gdcrs_entry_start_time':' 09:00'},{'gdcrs_entry_start_time':'09:00\n'},
                {'gdcrs_entry_start_time':'15:20'},{'gdcrs_entry_start_time':'20:00','gdcrs_entry_end_time':'09:00'},
                {'gdcrs_entry_start_time':'01:00','gdcrs_entry_end_time':'02:00'}):
            with self.subTest(changes=changes):
                value={**default_settings(),**changes}
                with self.assertRaises(SettingsError):_validate(value)
                self.settings={**baseline,**changes};self.monitor=self.make_monitor()
                self.tick();self.buy.assert_not_called()
                self.chart.assert_not_called()

    def test_weekend_and_naive_clock_do_not_grant_entry_window(self):
        context=self.monitor._context(self.session,self.now)
        self.assertFalse(self.monitor._entry_window_open(self.now.replace(tzinfo=None),context))
        self.now=datetime(2026,10,31,10,30,tzinfo=KST)
        context=self.monitor._context(self.session,self.now)
        self.assertFalse(self.monitor._entry_window_open(self.now,context))
        self.tick();self.chart.assert_not_called();self.buy.assert_not_called()

    def test_skipped_cross_is_recorded_once_and_not_replayed_at_window_start(self):
        self.settings.update(gdcrs_entry_start_time='10:30',gdcrs_entry_end_time='14:00')
        self.cross_at(datetime(2026,10,26,10,29,5,tzinfo=KST));self.buy.assert_not_called()
        self.tick()
        self.now+=timedelta(minutes=1)
        self.chart.return_value=tuple(MinuteBar(datetime(2026,10,26,10,26,tzinfo=KST)+timedelta(minutes=i),p)
                                      for i,p in enumerate((2,1,4,5)))
        self.tick();self.buy.assert_not_called()
        with sqlite3.connect(self.path) as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM entry_decisions').fetchone()[0],1)

    def test_window_change_and_next_day_reset_never_replay_old_cross(self):
        self.settings.update(gdcrs_entry_start_time='10:30',gdcrs_entry_end_time='14:00')
        self.cross_at(datetime(2026,10,26,10,4,5,tzinfo=KST));self.buy.assert_not_called()
        self.settings['gdcrs_entry_start_time']='09:00';self.now+=timedelta(seconds=1)
        self.tick();self.buy.assert_not_called()
        self.now+=timedelta(days=1);self.tick();self.buy.assert_not_called()
        self.assertEqual(self.monitor._baseline,{})

    def test_queue_crossing_end_boundary_has_explicit_no_send_reason(self):
        self.settings.update(gdcrs_entry_start_time='09:00',gdcrs_entry_end_time='14:00')
        def buy(text,session,*,before_send):
            self.now=self.now.replace(hour=14,minute=0,second=0)
            try:before_send()
            except Exception as error:return OrderReplies([],skipped_reason=error.skipped_reason)
            self.fail('queued order passed after entry end')
        self.buy.side_effect=buy
        self.cross_at(datetime(2026,10,26,13,59,59,tzinfo=KST))
        self.assertFalse(self.monitor._halted)
        last=self.monitor.status()['last_entry_decision']
        self.assertEqual(last['skip_reason'],'outside_entry_time_window')
        self.assertEqual(last['observed_at'],self.now.isoformat())

    def test_research_record_and_order_evidence_keep_window_without_fake_fill(self):
        self.settings.update(gdcrs_entry_start_time='10:30',gdcrs_entry_end_time='14:00')
        tracker=Mock();self.session.paper_automation_reconcile=type('Gate',(),{'_tracker':tracker})()
        self.cross_at(datetime(2026,10,26,10,4,5,tzinfo=KST))
        event=tracker.record_strategy_signal.call_args.args[0]
        self.assertTrue(event['research_only']);self.assertEqual(event['reason'],'outside_entry_time_window')
        self.assertEqual(event['entry_time_window']['start'],'10:30')
        self.assertEqual(event['candle_start'],'2026-10-26T10:03:00+09:00')
        self.monitor=self.make_monitor();seen=[]
        def buy(text,session,*,before_send):
            before_send();seen.append(_current.get().evidence.as_dict());return []
        self.buy.side_effect=buy
        with automated_orders(tracker,'gdcrs'):
            self.cross_at(datetime(2026,10,26,10,30,5,tzinfo=KST))
        self.assertEqual(seen[0]['config']['entry_start_time'],'10:30')
        self.assertEqual(seen[0]['config']['entry_end_time'],'14:00')

    def test_paper_options_never_restrict_real_mode(self):
        self.session.mode='real'
        self.settings.update(gdcrs_entry_start_time='10:30',gdcrs_entry_end_time='14:00')
        self.cross_at(datetime(2026,10,26,15,24,5,tzinfo=KST))
        self.buy.assert_called_once()

    def test_b3_addition_and_protective_dead_cross_ignore_initial_entry_window(self):
        b3=b3_fixture.B3MonitorTests();b3.setUp();self.addCleanup(b3.doCleanups)
        b3.settings.update(gdcrs_entry_start_time='10:30',gdcrs_entry_end_time='14:00')
        b3.test_add_on_uses_post_fill_volume_without_fabricating_cross()
        sell=sell_fixture.CrossMonitorTests();sell.setUp();self.addCleanup(sell.doCleanups)
        sell.settings.update(gdcrs_entry_start_time='10:30',gdcrs_entry_end_time='14:00')
        sell.test_initial_seed_then_true_cross_sells_current_sellable_quantity()


class EntryWindowSettingsTests(unittest.TestCase):
    def test_legacy_read_is_byte_preserving_and_exposes_new_defaults(self):
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)/'settings.json';path.write_text('{"gdcrs_enabled": true}\n')
            before=path.read_bytes();settings=load_settings(path)
            self.assertEqual(effective_entry_time_window(settings,'paper'),('09:00','15:20'))
            self.assertEqual(path.read_bytes(),before)
            self.assertFalse(paper_strategy_options_path(path).exists())

    def test_receiver_only_overlay_saves_separately_and_conflicts_fail_closed(self):
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)/'settings.json';path.write_text('{"gdcrs_enabled": true}\n')
            before=path.read_bytes()
            settings=save_paper_strategy_options({'gdcrs_entry_start_time':'10:30','gdcrs_entry_end_time':'14:00'},path)
            self.assertEqual(effective_entry_time_window(settings,'paper'),('10:30','14:00'))
            self.assertEqual(path.read_bytes(),before)
            path.write_text(json.dumps({'gdcrs_entry_start_time':'09:00'}))
            with self.assertRaises(SettingsError):load_settings(path)


if __name__=='__main__':unittest.main()
