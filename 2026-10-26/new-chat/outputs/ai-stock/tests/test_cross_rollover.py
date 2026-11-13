"""A live detected death cross is revalidated or durably reported at rollover."""
from datetime import datetime, timedelta
from pathlib import Path
import sqlite3
import json
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import Mock, patch

from kiwoom_autotrader.api.minute_chart import MinuteBar
from kiwoom_autotrader.api.session import BrokerSession, IssuedToken, KST
from kiwoom_autotrader.trading.cross_monitor import CrossMonitor, CrossMonitorError
from kiwoom_autotrader.trading.execution_log import ExecutionTracker
from kiwoom_autotrader.trading.order_context import automated_orders
from kiwoom_autotrader.telegram.commands.orders import handle_sell
from kiwoom_autotrader.utils.http_queue import HTTPNotSentError, BufferedResponse
from tests.test_cross_monitor import report


class CrossRolloverTests(TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'markers.sqlite3'
        self.now = datetime(2026, 10, 26, 10, 3, 5, tzinfo=KST)
        self.session = BrokerSession(now=lambda: self.now)
        self.session.mode = 'paper'
        self.session._issued = IssuedToken('offline-rollover-token', '20300101000000')
        self.settings = {'ddcrs_enabled':True, 'gdcrs_short_period':1, 'gdcrs_long_period':3,
                         'market_start_time':'09:00', 'market_end_time':'15:30'}
        self.account = Mock(return_value=report('3', held='3'))
        self.chart, self.sell, self.notify = Mock(), Mock(return_value=['접수']), Mock()
        self.monitor = CrossMonitor(ownership_reader=lambda *_: 9, settings_loader=lambda:dict(self.settings), now=lambda:self.now,
            state_path=self.path, account_fetcher=self.account, chart_fetcher=self.chart,
            sell_handler=self.sell)

    def bars(self, prices, start=0):
        return tuple(MinuteBar(datetime(2026,10,26,10,start+i,tzinfo=KST), p) for i,p in enumerate(prices))

    def seed(self):
        self.chart.return_value = self.bars([1,2,3])
        self.monitor.tick(self.session,self.notify)
        self.now += timedelta(seconds=2)
        self.monitor.tick(self.session,self.notify)
        self.account.reset_mock(); self.chart.reset_mock()

    def exercise(self, *, new_bars=None, after_first_account=None, advance=4):
        self.seed()
        self.now=datetime(2026,10,26,10,4,55,tzinfo=KST)
        old=self.bars([1,2,3,1]); newer=new_bars or self.bars([2,3,1,1],start=1)
        self.chart.side_effect=[old,newer]
        def holdings(_):
            if self.account.call_count==2:
                self.now+=timedelta(seconds=advance)
                if after_first_account: after_first_account()
            return report('3', held='3')
        self.account.side_effect=holdings
        self.monitor.tick(self.session,self.notify)   # scan holdings
        self.now+=timedelta(seconds=2)               # 10:04:57
        self.monitor.tick(self.session,self.notify)   # detect, rollover and revalidate
        return old,newer

    def incidents(self):
        with sqlite3.connect(self.path) as db:
            return db.execute('SELECT mode,symbol,candle,reason FROM missed_signals').fetchall()

    def test_revalidates_exact_overlap_and_negative_latest_before_one_sell(self):
        from kiwoom_autotrader.trading import cross_monitor
        with patch.object(cross_monitor,'cross_evidence',wraps=cross_monitor.cross_evidence) as evidence:
            original,newer=self.exercise()
        self.sell.assert_called_once()
        self.assertEqual(self.sell.call_args.args[0], 'sell 005930 3')
        self.assertEqual(self.chart.call_count,2)
        self.assertEqual(self.account.call_count,3)
        self.assertEqual(evidence.call_args.kwargs['window'],original)
        self.assertEqual(evidence.call_args.kwargs['rollover_window'],newer)
        self.assertEqual(evidence.call_args.kwargs['rollover_observed_at'],self.now)
        self.assertEqual(self.monitor._baseline['005930'].bars,newer[-3:])
        self.sell.call_args.kwargs['before_send']()
        self.now += timedelta(minutes=1)
        with self.assertRaises(CrossMonitorError): self.sell.call_args.kwargs['before_send']()

    def test_reversal_or_revised_overlap_blocks_sell_and_persists_incident(self):
        for newer in (self.bars([2,3,1,10],start=1), self.bars([9,3,1,1],start=1)):
            with self.subTest(newer=newer):
                self.setUp()
                self.exercise(new_bars=newer)
                self.sell.assert_not_called()
                self.assertEqual(self.incidents()[0][3], 'rollover_reversed_or_revised')
                self.assertIn('자동매도를 실행하지 못했습니다',self.notify.call_args.args[0])
                self.assertTrue(self.monitor.status()['missed_signals'][0]['durable'])

    def test_more_than_one_minute_is_never_replayed(self):
        self.exercise(advance=65)
        self.sell.assert_not_called(); self.assertEqual(self.chart.call_count,1)
        self.assertEqual(self.incidents()[0][3], 'rollover_multiple_minutes')
        # A new instance cannot treat an incident as an executable signal.
        restarted=CrossMonitor(ownership_reader=lambda *_: 9, settings_loader=lambda:dict(self.settings),now=lambda:self.now,
            state_path=self.path,account_fetcher=self.account,chart_fetcher=self.chart,sell_handler=self.sell)
        self.assertEqual(restarted._baseline,{})
        self.assertEqual(restarted.status()['missed_signals'][0]['reason'],'rollover_multiple_minutes')

    def test_missing_or_stale_revalidation_never_sells(self):
        self.exercise(new_bars=self.bars([3,1,1],start=2))
        self.sell.assert_not_called()
        self.assertEqual(self.incidents()[0][3], 'rollover_revalidation_failed')

    def test_session_switch_before_revalidation_blocks_sell_and_records_cause(self):
        self.exercise(after_first_account=lambda:setattr(self.session,'mode','real'))
        self.sell.assert_not_called(); self.assertEqual(self.chart.call_count,1)
        self.assertEqual(self.incidents()[0][3], 'context_changed')

    def test_second_rollover_during_fresh_holdings_read_blocks_sell(self):
        self.seed()
        self.now=datetime(2026,10,26,10,4,55,tzinfo=KST)
        self.chart.side_effect=[self.bars([1,2,3,1]),self.bars([2,3,1,1],start=1)]
        def holdings(_):
            if self.account.call_count==2:self.now+=timedelta(seconds=4)
            if self.account.call_count==3:self.now+=timedelta(minutes=1)
            return report('3',held='3')
        self.account.side_effect=holdings
        self.monitor.tick(self.session,self.notify)
        self.now+=timedelta(seconds=2)
        self.monitor.tick(self.session,self.notify)
        self.sell.assert_not_called()
        self.assertEqual(self.incidents()[0][3],'rollover_revalidation_failed')

    def test_revalidation_chart_crossing_a_second_boundary_never_sends(self):
        self.seed()
        self.now=datetime(2026,10,26,10,4,55,tzinfo=KST)
        old,newer=self.bars([1,2,3,1]),self.bars([2,3,1,1],start=1)
        def holdings(_):
            if self.account.call_count==2:self.now+=timedelta(seconds=4)
            return report('3',held='3')
        def chart(*_,**__):
            if self.chart.call_count==2:
                self.now+=timedelta(minutes=1)
                return newer
            return old
        self.account.side_effect=holdings
        self.chart.side_effect=chart
        self.monitor.tick(self.session,self.notify)
        self.now+=timedelta(seconds=2)
        self.monitor.tick(self.session,self.notify)
        self.sell.assert_not_called()
        self.assertEqual(self.incidents()[0][3],'rollover_revalidation_failed')

    def test_rollover_in_send_queue_never_transports_and_is_audited(self):
        def blocked(_command,_session,*,before_send):
            self.now += timedelta(minutes=1)
            with self.assertRaises(CrossMonitorError): before_send()
            return ['전송하지 않았습니다']
        self.sell.side_effect=blocked
        self.exercise()
        self.sell.assert_called_once()
        self.assertEqual(self.incidents()[0][3], 'rollover_send_boundary')
        self.assertTrue(self.monitor.status()['missed_signals'][0]['durable'])

    def test_real_order_path_rejects_stale_queue_without_transport_and_cancels_intent(self):
        self.monitor.sell_handler=handle_sell
        self.session.paper_automation_reconcile=Mock(return_value=Mock())
        tracker=ExecutionTracker(log_dir=Path(self.temp.name)/'logs',now=lambda:self.now,
                                 account_fetcher=Mock(),executions_fetcher=Mock(),profit_fetcher=Mock())
        opener=Mock()
        def blocked(actual,request,*,before_send,**kwargs):
            self.assertIs(actual,opener)
            self.now += timedelta(minutes=1)
            with self.assertRaises(CrossMonitorError): before_send()
            raise HTTPNotSentError('offline: before send')
        with patch('kiwoom_autotrader.trading.paper_policy.load_settings',return_value={'paper_automation_only':False}), \
             patch('kiwoom_autotrader.api.orders.urllib.request.build_opener',return_value=opener), \
             patch('kiwoom_autotrader.api.orders.queued_open',side_effect=blocked) as queue, \
             automated_orders(tracker,'ddcrs'):
            self.exercise()
        queue.assert_called_once();opener.open.assert_not_called()
        with sqlite3.connect(tracker.path) as db:
            self.assertEqual(db.execute('SELECT state FROM intents').fetchall(), [('cancelled',)])
            self.assertEqual(db.execute('SELECT COUNT(*) FROM orders').fetchone()[0],0)
        self.assertEqual(self.incidents()[0][3], 'rollover_send_boundary')

    def test_recorder_check_crossing_boundary_is_caught_by_final_strategy_guard(self):
        self.monitor.sell_handler=handle_sell
        tracker=ExecutionTracker(log_dir=Path(self.temp.name)/'logs',now=lambda:self.now,
                                 account_fetcher=Mock(),executions_fetcher=Mock(),profit_fetcher=Mock())
        self.session.paper_automation_reconcile=Mock(return_value=Mock())
        def check_recorder():
            if self.session.execution_recording_guard.call_count == 2:
                self.now += timedelta(minutes=1)
        self.session.execution_recording_guard=Mock(side_effect=check_recorder)
        opener=Mock()
        def blocked(actual,request,*,before_send,**kwargs):
            self.assertIs(actual,opener)
            with self.assertRaises(CrossMonitorError):before_send()
            raise HTTPNotSentError('offline: final guard rejected')
        with patch('kiwoom_autotrader.trading.paper_policy.load_settings',return_value={'paper_automation_only':True,'paper_automation_buy_cap':1000000}), \
             patch('kiwoom_autotrader.api.orders.urllib.request.build_opener',return_value=opener), \
             patch('kiwoom_autotrader.api.orders.queued_open',side_effect=blocked) as queue, \
             automated_orders(tracker,'ddcrs'):
            self.exercise()
        queue.assert_called_once();opener.open.assert_not_called()
        with sqlite3.connect(tracker.path) as db:
            self.assertEqual(db.execute('SELECT state FROM intents').fetchall(), [('cancelled',)])
            self.assertEqual(db.execute('SELECT COUNT(*) FROM orders').fetchone()[0],0)
        self.assertEqual(self.incidents()[0][3], 'rollover_send_boundary')

    def test_valid_revalidation_records_both_windows_before_fake_paper_order(self):
        self.monitor.sell_handler=handle_sell
        tracker=ExecutionTracker(log_dir=Path(self.temp.name)/'logs',now=lambda:self.now,
                                 account_fetcher=Mock(),executions_fetcher=Mock(),profit_fetcher=Mock())
        self.session.execution_recording_guard=Mock()
        reconciliation_guard=Mock()
        self.session.paper_automation_reconcile=Mock(return_value=reconciliation_guard)
        opener=Mock()
        def accepted(actual,request,*,before_send,**kwargs):
            self.assertIs(actual,opener)
            before_send()
            with sqlite3.connect(tracker.path) as db:
                state, raw=db.execute('SELECT i.state,d.evidence_json FROM intents i JOIN order_decisions d ON i.scope=d.scope AND i.id=d.intent_id').fetchone()
                self.assertEqual(state,'prepared')
                evidence=json.loads(raw)
                self.assertEqual(evidence['candle_start'],'2026-10-26T10:03:00+09:00')
                self.assertEqual(evidence['rollover_revalidation']['bars'][-1]['start'],'2026-10-26T10:04:00+09:00')
                self.assertLess(evidence['rollover_revalidation']['difference'],0)
            return BufferedResponse(json.dumps({'return_code':0,'ord_no':'0000042','dmst_stex_tp':'KRX'}).encode(),{},200)
        with patch('kiwoom_autotrader.trading.paper_policy.load_settings',return_value={'paper_automation_only':True,'paper_automation_buy_cap':1000000}), \
             patch('kiwoom_autotrader.api.orders.urllib.request.build_opener',return_value=opener), \
             patch('kiwoom_autotrader.api.orders.queued_open',side_effect=accepted) as queue, \
             automated_orders(tracker,'ddcrs'):
            self.exercise()
        queue.assert_called_once();opener.open.assert_not_called()
        self.assertGreaterEqual(self.session.execution_recording_guard.call_count,2)
        self.session.paper_automation_reconcile.assert_called_once_with('005930',side='sell',quantity='3',price=None)
        self.assertGreaterEqual(reconciliation_guard.call_count,1)
        with sqlite3.connect(tracker.path) as db:
            self.assertEqual(db.execute('SELECT state FROM intents').fetchall(), [('accepted',)])
            self.assertEqual(db.execute('SELECT side,quantity FROM orders').fetchall(), [('sell',3)])
