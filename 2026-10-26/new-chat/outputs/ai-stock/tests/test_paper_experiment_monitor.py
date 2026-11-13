"""Receiver-loop orchestration only: isolated ledger, fake provider and transport.

These tests do not substitute for final order-policy/00 reconciliation tests.
The submit stub executes the actual queued before_send callback and records no
broker fill, so timing, durable claims and per-symbol quarantine are observable.
"""
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

from kiwoom_autotrader.api.minute_chart import MinuteBar
from kiwoom_autotrader.trading import paper_experiment_monitor as m
from kiwoom_autotrader.trading import paper_experiment_rules as r
from kiwoom_autotrader.trading.paper_experiment_store import ExperimentStore

KST=ZoneInfo('Asia/Seoul');START=datetime(2026,11,2,9,tzinfo=KST);DAY='20261102'


def assignment(arm,exclude=()):
    for n in range(10000):
        code=f'{n:06d}'
        if code in exclude:continue
        value=r.assign_candidate(DAY,code,START,profile='native-five-v1' if arm.startswith('EXP_') else None)
        if value['arm']==arm:return value
    raise AssertionError('fixture assignment missing')


class Tracker:
    def __init__(self):
        self.positions={};self.open_symbols=[];self.calls=[]
        self._holdings=Mock();self._save_paper_balance_snapshot=Mock()

    def multi_strategy_position(self,mode,symbol,arm,now):
        self.calls.append((mode,symbol,arm,now))
        value=self.positions.get((symbol,arm))
        if value is not None:return deepcopy(value)
        if any(s==symbol for s,_ in self.positions):return {'status':'unsupported'}
        return {'status':'flat','owned_qty':0,'basis_krw':0}

    def multi_strategy_open_symbols(self,mode,now):
        return {'status':'available','symbols':list(self.open_symbols)}


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name).resolve()/'experiment.sqlite3'
        self.clock=START+timedelta(minutes=21)
        self.settings={'paper_experiment_enabled':True,'gdcrs_stocks':[],'paper_strategy_stocks':[]}
        self.tracker=Tracker();self.events=[];self.orders=[];self.contexts=[]
        self.session=SimpleNamespace(mode='paper',token='fixture',paper_automation_reconcile=SimpleNamespace(_tracker=self.tracker))
        self.provider=SimpleNamespace(fetch=Mock(side_effect=self.market),
            discover_dynamic_candidates=Mock(return_value={'candidates':[]}),
            fetch_s5_inputs=Mock(return_value={}))
        self.chart=Mock(side_effect=self.bars)
        self.account=Mock(return_value={'cash':{'ord_alow_amt':9_000_000},'holdings':[]})
        self.calendar=Mock(side_effect=lambda now:{'start':now.replace(hour=9,minute=0,second=0,microsecond=0),
            'end':now.replace(hour=15,minute=30,second=0,microsecond=0),'session_day':now.strftime('%Y%m%d'),
            'calendar_source_sha256':'c'*64})
        self.notify=Mock()
        self.addCleanup(patch.stopall)
        patch.object(m,'session_identity',side_effect=lambda session:(session.mode,session.token)).start()
        patch.object(m,'fetch_budget_limit',return_value=1000).start()
        patch.object(m,'record_signal_safely',side_effect=lambda tracker,event:self.events.append(deepcopy(event))).start()
        @contextmanager
        def order_context(tracker,arm,**kwargs):
            self.contexts.append((arm,deepcopy(kwargs)))
            yield
        patch.object(m,'automated_orders',side_effect=order_context).start()
        self.monitor=self.make_monitor()

    def make_monitor(self):
        monitor=m.PaperExperimentMonitor(settings_loader=lambda:self.settings,now=lambda:self.clock,
            store_path=self.path,chart_fetcher=self.chart,provider_factory=lambda session:self.provider,
            account_fetcher=self.account,submitter=self.submit,calendar_reader=self.calendar,batch_size=4)
        monitor.store=ExperimentStore(self.path);monitor.provider=self.provider
        monitor._generation_matches=Mock(return_value=True)  # monitor-only fixture; real ledger covered separately
        return monitor

    def market(self,symbol,now):
        return {'symbol':symbol,'mode':'paper','current_price':1030,'day_open':1030,
            'request_started_at':now.isoformat(),'observed_at':now.isoformat(),'source_sha256':'b'*64}

    def bars(self,session,symbol):
        end=self.clock.replace(second=0,microsecond=0);start=end-timedelta(minutes=21)
        return [MinuteBar(start=start+timedelta(minutes=i),open_price=p,high_price=p,low_price=p,
            close=p,volume=100,source_symbol=symbol,source_mode='paper',source_response_sha256='a'*64,
            source_query_started_at=self.clock) for i,p in enumerate([1000]*20+[1100])]

    def submit(self,session,side,symbol,quantity,price,*,before_send):
        before_send();self.orders.append((side,symbol,quantity,price))
        return SimpleNamespace(order_number=f'P{len(self.orders):04d}')

    def seed(self,value):
        return self.monitor.store.assign(value)

    def owned(self,value,*,entry_at=None):
        self.tracker.positions[(value['symbol'],value['arm'])]={'status':'owned','owned_qty':3,
            'basis_krw':3000,'generation_id':'a'*32,'first_fill_timing_verified':True,
            'first_fill_at':(entry_at or START).isoformat()}
        self.tracker.open_symbols.append(value['symbol'])

    def one(self,value):
        self.monitor._one(self.session,self.tracker,self.settings,value['symbol'],self.clock,value)

    def test_real_mode_does_not_load_settings_provider_store_or_submit(self):
        forbidden=Mock(side_effect=AssertionError('real mode touched experiment'))
        path=self.path.parent/'must-not-exist.sqlite3'
        monitor=m.PaperExperimentMonitor(settings_loader=forbidden,store_path=path,
            provider_factory=forbidden,chart_fetcher=forbidden,account_fetcher=forbidden,
            submitter=forbidden,calendar_reader=forbidden)
        monitor.tick(SimpleNamespace(mode='real'),forbidden)
        forbidden.assert_not_called();self.assertFalse(path.exists())
        self.assertEqual(monitor.status()['reason'],'paper_only')

    def test_disabled_or_unverified_calendar_never_submits(self):
        self.settings['paper_experiment_enabled']=False
        self.monitor.tick(self.session,self.notify)
        self.calendar.assert_not_called();self.chart.assert_not_called()
        self.settings['paper_experiment_enabled']=True;self.calendar.side_effect=None;self.calendar.return_value=None
        self.monitor.tick(self.session,self.notify)
        self.assertEqual(self.monitor.status()['reason'],'official_calendar_unverified')
        self.assertEqual(self.orders,[]);self.chart.assert_not_called()

    def test_identical_signal_claim_survives_monitor_recreation(self):
        value=self.seed(assignment('EXP_A'));self.one(value);self.one(value)
        replacement=self.make_monitor();replacement._one(self.session,self.tracker,self.settings,value['symbol'],self.clock,value)
        self.assertEqual(len(self.orders),1)
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM attempts').fetchone()[0],1)
            self.assertEqual(db.execute('SELECT count(*) FROM outcomes').fetchone()[0],1)
        self.assertEqual(self.contexts[0][0],'EXP_A')

    def test_uncertain_symbol_does_not_stop_other_symbol_or_replay_same_claim(self):
        first=self.seed(assignment('EXP_A'));second=self.seed(assignment('EXP_A',(first['symbol'],)))
        self.settings['gdcrs_stocks']=[{'code':first['symbol']},{'code':second['symbol']}]
        class Uncertain(Exception):
            uncertain=True;reason_code='broker_outcome_unknown'
        def submit(session,side,symbol,qty,price,*,before_send):
            before_send()
            if symbol==first['symbol']:raise Uncertain()
            return self.submit(session,side,symbol,qty,price,before_send=before_send)
        self.monitor.submit=submit;self.monitor.tick(self.session,self.notify)
        self.assertEqual([row[1] for row in self.orders],[second['symbol']])
        self.assertIn(first['symbol'],self.monitor.quarantined)
        self.notify.assert_called_once()
        self.one(first)
        self.assertEqual(len(self.orders),1)
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM attempts').fetchone()[0],2)

    def test_other_arm_ownership_never_sold_by_assignment(self):
        value=self.seed(assignment('EXP_A'));other={**value,'arm':'EXP_B'};self.owned(other)
        self.clock=START.replace(hour=15,minute=15)
        self.one(value)
        self.assertEqual(self.orders,[])
        self.assertNotIn((value['symbol'],'EXP_A'),self.tracker.positions)

    def test_forced_exit_owned_native_arm_at_1515_needs_no_new_candles(self):
        value=self.seed(assignment('EXP_A'));self.owned(value);self.clock=START.replace(hour=15,minute=15)
        self.one(value)
        self.assertEqual(self.orders,[('sell',value['symbol'],3,None)])
        self.chart.assert_not_called();self.account.assert_not_called()
        from kiwoom_autotrader.trading.decision_evidence import freeze_evidence
        config=freeze_evidence(self.contexts[-1][1]['evidence']).as_dict()['config']
        self.assertTrue(config['experiment_exit_reason'])
        self.assertEqual(config['multi_exit_kind'],'full')

    def test_owned_dynamic_symbol_removed_from_current_pool_still_exits(self):
        value=self.seed(assignment('EXP_A'));self.owned(value);self.clock=START.replace(hour=15,minute=15)
        self.settings['gdcrs_stocks']=[];self.settings['paper_strategy_stocks']=[]
        self.monitor.tick(self.session,self.notify)
        self.assertEqual(self.orders,[('sell',value['symbol'],3,None)])

    def test_c_preopen_prior_close_and_fresh_open_can_buy_only_0900(self):
        symbol='005930';self.clock=START+timedelta(seconds=2)
        self.monitor.store.selection(DAY,'preopen',{'observed_at':(START-timedelta(minutes=5)).isoformat(),
            'candidates':[{'symbol':symbol,'first_seen_at':(START-timedelta(minutes=5)).isoformat()}]})
        self.monitor.store.selection(DAY,'prior:'+symbol,{'symbol':symbol,'previous_close':1000,
            'previous_session_day':'20261030','previous_close_available_at':(START-timedelta(minutes=5)).isoformat(),
            'source_sha256':'c'*64})
        self.monitor._one(self.session,self.tracker,self.settings,symbol,self.clock)
        self.assertEqual(len(self.orders),1)
        saved=self.monitor.store.assignment(DAY,symbol);self.assertEqual(saved['arm'],'EXP_C')
        self.clock=START+timedelta(minutes=1);self.one(saved)
        self.assertEqual(len(self.orders),1)

    def test_d_frozen_selection_is_auction_only_and_exit_requires_next_session(self):
        symbol='005930';at=START.replace(hour=15,minute=19)
        selection=r.select_overnight([{'symbol':symbol,'first_seen_at':START.isoformat()}],day=DAY,snapshot_at=at)
        value=selection['assignments'][0]
        self.clock=at;self.one(value);self.assertEqual(self.orders,[])
        self.clock=at+timedelta(minutes=1);self.one(value)
        self.assertEqual(self.orders,[('buy',symbol,194,1030)])
        self.owned(value,entry_at=self.clock)
        self.clock=START.replace(day=3,hour=9,minute=0,second=2);self.one(value)
        self.assertEqual(self.orders[-1],('sell',symbol,3,None))
        self.assertEqual(len(self.orders),2)

    def test_d_next_open_missing_official_calendar_does_not_exit(self):
        symbol='005930';at=START.replace(hour=15,minute=19)
        value=r.select_overnight([{'symbol':symbol,'first_seen_at':START.isoformat()}],day=DAY,snapshot_at=at)['assignments'][0]
        self.owned(value,entry_at=at+timedelta(minutes=1));self.clock=START.replace(day=3,second=2)
        self.calendar.side_effect=None;self.calendar.return_value=None
        self.one(value);self.assertEqual(self.orders,[])

    def test_queued_entry_guard_rechecks_official_session_and_profile(self):
        value=self.seed(assignment('EXP_A'))
        def submit(session,side,symbol,qty,price,*,before_send):
            self.settings={**self.settings,'paper_experiment_enabled':False};before_send()
            raise AssertionError('disabled profile submitted')
        self.monitor.submit=submit
        with self.assertRaisesRegex(ValueError,'experiment_profile_changed'):self.one(value)
        self.assertEqual(self.orders,[])

    def test_hot_policy_entry_pause_keeps_assignment_and_owned_exit(self):
        from kiwoom_autotrader.config.paper_hot_policy import HotPolicyRejected
        value=self.seed(assignment('EXP_A'))
        reader=SimpleNamespace(assert_entry_current=Mock(side_effect=HotPolicyRejected('HOT_POLICY_ARM_ENTRY_PAUSED')))
        self.monitor.policy_reloader=reader
        self.monitor.hot_policy_snapshot={'version':1,'changes':{'entry_paused_arms':['EXP_A']}}
        before=deepcopy(self.monitor.store.assignment(DAY,value['symbol']))
        self.one(value)
        self.assertEqual(self.orders,[])
        self.assertEqual(self.monitor.store.assignment(DAY,value['symbol']),before)
        self.assertEqual(self.monitor.diagnostics[value['symbol']]['reason'],'HOT_POLICY_ARM_ENTRY_PAUSED')
        reader.assert_entry_current.reset_mock();self.owned(value);self.clock=START.replace(hour=15,minute=15)
        self.one(value)
        self.assertEqual(self.orders,[('sell',value['symbol'],3,None)])
        reader.assert_entry_current.assert_not_called()

    def test_hot_policy_pending_epoch_change_is_rechecked_in_queued_send(self):
        from kiwoom_autotrader.config.paper_hot_policy import HotPolicyRejected
        value=self.seed(assignment('EXP_A'));snapshot={'version':1,'policy_sha256':'a'*64}
        reader=SimpleNamespace(assert_entry_current=Mock())
        self.monitor.policy_reloader=reader;self.monitor.hot_policy_snapshot=snapshot
        def submit(session,side,symbol,qty,price,*,before_send):
            self.monitor.hot_policy_snapshot={'version':2,'policy_sha256':'b'*64}
            reader.assert_entry_current.side_effect=HotPolicyRejected('HOT_POLICY_CHANGED_BEFORE_SEND')
            before_send()
            raise AssertionError('changed policy submitted')
        self.monitor.submit=submit
        with self.assertRaisesRegex(HotPolicyRejected,'CHANGED_BEFORE_SEND'):self.one(value)
        self.assertIs(reader.assert_entry_current.call_args.args[0],snapshot)
        self.assertEqual(self.orders,[])

    def test_hot_policy_loads_at_next_cycle_and_pool_reduction_preserves_base(self):
        snapshot={'version':1,'policy_sha256':'a'*64,'entry_authorized':True,
            'changes':{'dynamic_candidate_limit':15,'entry_pool_limit':2,'entry_paused_arms':[]}}
        reader=SimpleNamespace(next_cycle=Mock(return_value=deepcopy(snapshot)))
        self.monitor.policy_reloader=reader
        with patch.object(self.monitor,'_tick') as tick:
            self.monitor.tick(self.session,self.notify)
        reader.next_cycle.assert_called_once_with(self.clock);tick.assert_called_once()
        self.provider.discover_dynamic_candidates.return_value={'candidates':[{'symbol':'999999'}]}
        pool=self.monitor._candidate_pool(['111111','222222','333333'],self.clock)
        self.assertEqual(pool,['111111','222222','333333'])
        self.assertEqual(self.provider.discover_dynamic_candidates.call_args.kwargs['limit'],15)
        self.assertEqual(self.monitor.status()['hot_policy']['version'],1)

    def test_real_tick_never_reads_pending_paper_hot_policy(self):
        reader=SimpleNamespace(next_cycle=Mock(side_effect=AssertionError('paper policy read in real')))
        self.monitor.policy_reloader=reader;self.session.mode='real'
        self.monitor.tick(self.session,self.notify)
        reader.next_cycle.assert_not_called()

    def test_queued_signal_cannot_replay_after_next_minute(self):
        value=self.seed(assignment('EXP_A'))
        def submit(session,side,symbol,qty,price,*,before_send):
            self.clock+=timedelta(minutes=1);before_send()
            raise AssertionError('expired signal submitted')
        self.monitor.submit=submit
        with self.assertRaisesRegex(ValueError,'signal_window_expired'):self.one(value)
        self.assertEqual(self.orders,[])

    def test_forced_exit_cannot_cross_1520_queue_boundary(self):
        value=self.seed(assignment('EXP_A'));self.owned(value);self.clock=START.replace(hour=15,minute=19,second=59)
        def submit(session,side,symbol,qty,price,*,before_send):
            self.clock+=timedelta(seconds=2);before_send()
            raise AssertionError('forced exit outside window submitted')
        self.monitor.submit=submit
        with self.assertRaisesRegex(ValueError,'window|cutoff|expired'):self.one(value)
        self.assertEqual(self.orders,[])

    def test_d_next_open_signal_expires_at_0901_before_send(self):
        symbol='005930';at=START.replace(hour=15,minute=19)
        value=r.select_overnight([{'symbol':symbol,'first_seen_at':START.isoformat()}],day=DAY,snapshot_at=at)['assignments'][0]
        self.owned(value,entry_at=at+timedelta(minutes=1));self.clock=START.replace(day=3,second=59)
        def submit(session,side,symbol,qty,price,*,before_send):
            self.clock+=timedelta(seconds=2);before_send()
            raise AssertionError('D open exit outside window submitted')
        self.monitor.submit=submit
        with self.assertRaisesRegex(ValueError,'signal_window_expired'):self.one(value)
        self.assertEqual(self.orders,[])

    def test_d_auction_signal_expires_at_official_1530_close(self):
        symbol='005930';at=START.replace(hour=15,minute=19)
        value=r.select_overnight([{'symbol':symbol,'first_seen_at':START.isoformat()}],day=DAY,snapshot_at=at)['assignments'][0]
        self.clock=at.replace(minute=29,second=59)
        def submit(session,side,symbol,qty,price,*,before_send):
            self.clock+=timedelta(seconds=2);before_send()
            raise AssertionError('D auction outside official session submitted')
        self.monitor.submit=submit
        with self.assertRaisesRegex(ValueError,'official_session_unavailable|signal_window_expired'):self.one(value)
        self.assertEqual(self.orders,[])

    def test_signal_journal_preserves_observed_price_and_not_a_broker_fill(self):
        value=self.seed(assignment('EXP_A'));self.one(value)
        signal=next(event for event in self.events if event['event_kind']=='eligible_signal')
        self.assertEqual(signal['signal_price'],1100)
        self.assertEqual(signal['signal_price_basis'],'completed_signal_bar_close')
        self.assertEqual(signal['available_at'],self.clock.isoformat())
        self.assertEqual(len(signal['signal_key']),64)
        self.assertIs(signal['broker_fill'],False)
        accepted=next(event for event in self.events if event.get('outcome')=='accepted')
        self.assertIs(accepted['broker_fill'],False)
        self.assertTrue(accepted['order_number'])

    def test_existing_s2_s5_assignments_are_explicitly_paused_not_reassigned(self):
        for arm in ('S2_surge_high_breakout','S5_steady_uptrend_vwap'):
            value=self.seed(assignment(arm));self.one(value)
            self.assertEqual(self.monitor.store.assignment(DAY,value['symbol']),value)
            self.assertEqual('paused',self.monitor.diagnostics[value['symbol']]['status'])
            self.assertTrue(self.monitor.diagnostics[value['symbol']]['reason'])
        self.assertEqual(self.orders,[])


if __name__=='__main__':unittest.main()
