"""Offline latest-boundary workset; no broker, runtime or production DB."""
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from kiwoom_autotrader.api.minute_chart import MinuteBar
from kiwoom_autotrader.trading.multi_strategy import MultiStrategyMonitor, EntryState, S3, S5, entry_signal
from kiwoom_autotrader.telegram.commands.orders import OrderReplies
from tests.test_steady_uptrend_vwap import fixture
from tests.s5_short_fixture import short_evidence


class BoundarySchedulerTests(unittest.TestCase):
    def setUp(self):
        origin, self.template_bars, self.template_daily, self.template_risk = fixture()
        self.now = origin + timedelta(minutes=1)
        self.codes = [f'{1000+i:06d}' for i in range(25)]
        self.eligible = tuple(self.codes[3:15])
        self.options = {'paper_multi_strategy_enabled': True, 'paper_multi_strategy_ids': [S5],
            'gdcrs_stocks': [{'code': c, 'budget': 1000000} for c in self.codes[:3]],
            'paper_strategy_stocks': [{'code': c, 'budget': 1000000} for c in self.codes[3:]],
            'paper_dynamic_scan_enabled': True, 'paper_dynamic_candidate_limit': 15,
            'paper_strategy_entry_pool_limit': 40, 'paper_max_concurrent_positions': 8,
            'gdcrs_initial_entry_pct': 50, 'gdcrs_add_on_profit_bps': 50,
            'gdcrs_max_daily_entries_per_symbol': 2}
        self.calls, self.orders, self.events = [], [], []
        self.opens = []
        self.capacity_blocked = False
        test = self
        class Provider:
            def s5_eligible_symbols(self, as_of=None):
                test.calls.append(('risk_index',as_of))
                return test.eligible
            def discover_dynamic_candidates(self, **kwargs):
                test.calls.append(('discover',test.now))
                return {'schema':'paper-dynamic-candidates-v1','status':'ready',
                    'available_at':test.now.isoformat(),'session_binding':'same-session',
                    'candidates':[],'base_candidates':[]}
            def fetch_s5_inputs(self,symbol,as_of=None):
                test.calls.append(('s5_inputs',symbol))
                daily=deepcopy(test.template_daily)
                daily.update(symbol=symbol,request_started_at=test.now.isoformat(),available_at=test.now.isoformat())
                risk=deepcopy(test.template_risk)
                risk.update(symbol=symbol,reviewed_at=test.now.isoformat(),expires_at=(test.now+timedelta(hours=1)).isoformat())
                risk['short_selling_evidence']=short_evidence(symbol,daily,test.now)
                return {'s5_daily_context':daily,'s5_risk_context':risk}
            def fetch(self,symbol,as_of=None):
                test.calls.append(('quote',symbol))
                return {'symbol':symbol,'current_price':10220,'day_high':10220,'change_pct':'5'}
            def is_fresh(self,market,now): return True
        self.provider=Provider()
        self.tracker=SimpleNamespace(multi_strategy_open_symbols=lambda *a:{'status':'available','symbols':list(self.opens)},
            multi_strategy_position=self.position,paper_position_capacity=self.capacity,
            record_strategy_signal=self.events.append)
        self.session=SimpleNamespace(paper_automation_reconcile=SimpleNamespace(_tracker=self.tracker))
        self.monitor=MultiStrategyMonitor(settings_loader=lambda:self.options,now=lambda:self.now,
            chart_fetcher=self.chart,provider_factory=lambda _:self.provider,exit_provider_factory=lambda _:self.provider,
            executor=self.execute,cooldown_check=lambda *a:True)
        self.identity=patch('kiwoom_autotrader.trading.multi_strategy.session_identity',return_value=('paper','synthetic'))
        self.identity.start();self.addCleanup(self.identity.stop)

    def position(self,mode,symbol,strategy,now):
        self.calls.append(('ownership',symbol))
        return {'status':'pending'} if symbol in self.opens else {'status':'flat'}

    def capacity(self,*args):
        self.calls.append(('capacity',args[0]))
        if self.capacity_blocked:
            from kiwoom_autotrader.trading.execution_log import ConcurrentPositionLimitReached
            raise ConcurrentPositionLimitReached('full')
        return {'limit':8,'occupied_symbols':[]}

    def chart(self,session,symbol,count):
        self.calls.append(('chart',symbol,count))
        start=self.now.replace(hour=9,minute=0,second=0,microsecond=0)
        total=int((self.now.replace(second=0,microsecond=0)-start).total_seconds()//60)
        return [MinuteBar(start+timedelta(minutes=i),10006+i*6,100+(i//5)*10,
            source_symbol=symbol,source_mode='paper',source_query_started_at=self.now,
            source_response_sha256='a'*64,open_price=10000+i*6,high_price=10007+i*6,low_price=9999+i*6)
            for i in range(total)]

    def execute(self,**kw):
        kw['before_send']();self.orders.append(kw)
        return OrderReplies([],submitted=False,decision_reason='fixture_not_submitted')

    def tick(self, seconds=1):
        self.monitor.tick(self.session,lambda _:None)
        self.now+=timedelta(seconds=seconds)

    def test_all_twelve_risk_eligible_configured_symbols_evaluated_in_current_boundary(self):
        for _ in range(12):self.tick()
        state=self.monitor.status()
        self.assertEqual(set(state['s5_schedule']['evaluated_symbols']),set(self.eligible),state)
        self.assertEqual(len(self.orders),12,state)
        self.assertEqual({o['symbol'] for o in self.orders},set(self.eligible))
        self.assertTrue(all(o['budget']==500000 for o in self.orders))
        self.assertTrue(all(o['b3_config']['multi_b3_initial_entry_pct']==50 for o in self.orders))
        self.assertTrue(all(o['market']['steady_uptrend_vwap_evidence']['signal_at'].endswith('09:35:00+09:00') for o in self.orders))
        self.assertEqual({x[1] for x in self.calls if x[0]=='chart'},set(self.eligible))
        self.assertEqual(len([x for x in self.calls if x[0]=='discover']),1)
        self.assertTrue(all(self.monitor._entry_admitted(c,self.now) for c in self.codes))

    def test_same_boundary_not_requeried_or_resubmitted_in_later_minutes(self):
        for _ in range(12):self.tick()
        before=len([x for x in self.calls if x[0]=='chart'])
        self.now=self.now.replace(minute=39,second=3)
        for _ in range(20):self.tick()
        self.assertEqual(len([x for x in self.calls if x[0]=='chart']),before)
        self.assertEqual(len(self.orders),12)

    def test_minute_rollover_retains_unattempted_work_and_next_boundary_discards_history(self):
        for _ in range(3):self.tick()
        first=set(o['symbol'] for o in self.orders)
        self.now=self.now.replace(minute=38,second=3)
        for _ in range(9):self.tick()
        self.assertEqual(set(o['symbol'] for o in self.orders),set(self.eligible))
        self.assertEqual(len(self.orders),12)
        self.now=self.now.replace(minute=40,second=3)
        self.tick()
        newest=self.orders[-1]
        self.assertEqual(newest['market']['steady_uptrend_vwap_evidence']['signal_at'],'2026-11-02T09:40:00+09:00')
        self.assertEqual(len(self.orders),13)
        self.assertEqual(len(first),3)

    def test_owned_or_pending_supervision_precedes_risk_scan_and_discovery(self):
        self.opens=['072950']
        self.tick()
        self.assertEqual(self.monitor._last_symbol,'072950')
        self.assertFalse(any(x[0] in ('discover','risk_index','chart') for x in self.calls))
        self.tick()
        self.assertEqual(self.monitor._last_symbol,self.eligible[0])

    def test_stale_or_empty_risk_snapshot_suppresses_flat_s5_inputs(self):
        self.eligible=()
        for _ in range(8):self.tick()
        self.assertFalse(any(x[0] in ('chart','s5_inputs','quote') for x in self.calls))
        self.assertEqual(self.orders,[])
        self.assertEqual(self.monitor.status()['s5_schedule']['status'],'risk_eligible_empty')

    def test_capacity_gate_still_runs_and_blocks_after_signal(self):
        self.capacity_blocked=True
        self.tick()
        self.assertEqual(self.orders,[])
        self.assertEqual(self.monitor.status()['last_decision']['reason'],'concurrent_position_limit_reached')
        self.assertTrue(any(x[0]=='capacity' for x in self.calls))

    def test_configured_admission_is_not_an_arbitrary_symbol_or_old_setting_bypass(self):
        self.tick()
        target=self.eligible[0]
        self.assertTrue(self.monitor._entry_admitted(target,self.now))
        self.monitor._entry_admission['999999']={'kind':'configured_base','initial_budget_krw':500000}
        self.monitor._entry_budgets['999999']=500000
        self.assertFalse(self.monitor._entry_admitted('999999',self.now))
        self.options['paper_strategy_stocks']=[r for r in self.options['paper_strategy_stocks'] if r['code']!=target]
        self.assertFalse(self.monitor._entry_admitted(target,self.now))

    def test_initial_and_old_s5_state_allowed_but_s3_baseline_stays_required(self):
        now,bars,daily,risk=fixture()
        market={'symbol':'005930','current_price':10220,'s5_daily_context':daily,'s5_risk_context':risk}
        for state in (EntryState(),EntryState(bars[-1].start),EntryState(bars[-1].start-timedelta(minutes=7))):
            self.assertEqual(entry_signal(S5,state,bars,market,symbol='005930',now=now),'steady_uptrend_vwap')
        self.assertIsNone(entry_signal(S3,EntryState(),bars,market,symbol='005930',now=now))

    def test_full_session_is_fetched_at_plus_four_minutes_not_just_boundary_minute(self):
        self.now=self.now.replace(minute=39,second=3)
        self.tick()
        chart=next(x for x in self.calls if x[0]=='chart')
        self.assertGreaterEqual(chart[2],40)
        self.assertEqual(len(self.orders),1,self.monitor.status())
        self.assertEqual(self.orders[0]['market']['steady_uptrend_vwap_evidence']['session_minute_count'],35)

    def test_missing_unfinished_bucket_minutes_does_not_invent_or_block_completed_s5(self):
        self.now=self.now.replace(minute=39,second=3)
        chart=self.monitor._session_chart
        self.monitor._session_chart=lambda *a,**kw:[b for b in chart(*a,**kw) if b.start.minute<35]
        self.tick()
        self.assertEqual(len(self.orders),1,self.monitor.status())
        self.assertEqual(len(self.orders[0]['bars']),35)
        self.assertEqual(self.orders[0]['market']['steady_uptrend_vwap_evidence']['signal_at'],'2026-11-02T09:35:00+09:00')
        self.assertEqual(self.monitor.status()['symbol_last_validated_at'][self.eligible[0]]['stage'],'entry_s5_session_and_quote')

    def test_expired_old_prefix_cannot_be_replayed_at_new_boundary(self):
        self.now=self.now.replace(minute=40,second=3)
        chart=self.monitor._session_chart
        self.monitor._session_chart=lambda *a,**kw:[b for b in chart(*a,**kw) if b.start.minute<35]
        self.tick()
        self.assertEqual(self.orders,[])
        self.assertEqual(self.monitor.status()['s5_input_diagnostics'][self.eligible[0]]['reason'],'incomplete_session_minutes')

    def test_actual_owned_s5_exit_precedes_discovery_and_does_not_need_eligible_list(self):
        symbol='072950'; self.opens=[symbol]; self.eligible=()
        owned={'status':'owned','owned_qty':10,'basis_krw':100000,'half_take_done':False,
            'first_fill_at':self.now.replace(hour=9,minute=0).isoformat(),
            'first_fill_observed_at':self.now.replace(hour=9,minute=0).isoformat(),
            'first_fill_timing_verified':True}
        self.tracker.multi_strategy_position=lambda mode,code,strategy,now:owned if code==symbol and strategy==S5 else {'status':'flat'}
        self.monitor._account=lambda session:{'mode':'paper','exchange':'KRX','snapshot_changed':False,
            'holdings':[{'stk_cd':'A'+symbol,'crd_tp':'00','crd_loan_dt':'','rmnd_qty':'10','trde_able_qty':'10','pur_amt':'100000'}]}
        self.tick()
        self.assertEqual(len(self.orders),1,self.monitor.status())
        self.assertEqual(self.orders[0]['side'],'sell')
        self.assertEqual(self.orders[0]['quantity'],10)
        self.assertFalse(any(x[0] in ('risk_index','discover','chart','s5_inputs') for x in self.calls))

    def test_configured_s5_can_finish_fresh_request_across_minute_same_boundary(self):
        self.now=self.now.replace(minute=36,second=59)
        original=self.monitor._session_chart
        def crossing(*args,**kwargs):
            self.now+=timedelta(seconds=2)
            return original(*args,**kwargs)
        self.monitor._session_chart=crossing
        self.tick()
        self.assertEqual(len(self.orders),1,self.monitor.status())
        self.assertEqual(self.orders[0]['observed_at'].minute,37)
        self.assertEqual(self.orders[0]['market']['steady_uptrend_vwap_evidence']['signal_at'],'2026-11-02T09:35:00+09:00')

    def test_inflight_next_five_minute_boundary_cannot_submit_old_window(self):
        self.now=self.now.replace(minute=39,second=59)
        original=self.monitor._session_chart
        def crossing(*args,**kwargs):
            self.now+=timedelta(seconds=2)
            return original(*args,**kwargs)
        self.monitor._session_chart=crossing
        self.tick()
        self.assertEqual(self.orders,[])
        self.assertEqual(self.monitor.status()['symbol_input_diagnostics'][self.eligible[0]]['code'],'minute_rollover')

    def test_stale_boundary_cannot_dispatch_after_clock_advance(self):
        self.tick()
        callback=self.orders[0]['before_send']
        self.now=self.now.replace(minute=40,second=3)
        with self.assertRaises(ValueError):callback()


if __name__=='__main__':unittest.main()
