"""Offline receiver rotation, candidate admission and prospective signal proof."""
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch
from zoneinfo import ZoneInfo

from kiwoom_autotrader.api.minute_chart import MinuteBar
from kiwoom_autotrader.trading.multi_strategy import MultiStrategyMonitor, S3, EntryState
from kiwoom_autotrader.telegram.commands.orders import OrderReplies
from kiwoom_autotrader.trading.execution_log import ConcurrentPositionLimitReached


class DynamicMonitorTests(TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 30, 11, 31, 1, tzinfo=ZoneInfo('Asia/Seoul'))
        self.options = {'paper_multi_strategy_enabled': True, 'paper_multi_strategy_ids':[S3],
            'gdcrs_stocks':[{'code':'005930', 'budget':1_000_000}],
            'paper_dynamic_scan_enabled':True, 'paper_dynamic_candidate_limit':20,
            'paper_max_concurrent_positions':8, 'gdcrs_initial_entry_pct':50}
        self.dynamic = ['000660']; self.base=[]; self.calls=[]; self.events=[]; self.orders=[]
        self.opens=[]; self.binding='session-proof'; self.change='19.9'; self.block=False
        test=self
        class Provider:
            def discover_dynamic_candidates(self, as_of=None, limit=30, configured_symbols=()):
                test.calls.append(('discover',test.now,limit,configured_symbols))
                return {'schema':'paper-dynamic-candidates-v1', 'status':'ready',
                    'available_at':test.now.isoformat(), 'session_binding':test.binding,
                    'candidates':[test.candidate(s) for s in test.dynamic],
                    'base_candidates':[test.candidate(s) for s in test.base]}
            def fetch(self, symbol, as_of=None):
                test.calls.append(('quote',symbol))
                return {'symbol':symbol,'change_pct':test.change,'current_price':100,'upper_limit_price':130}
            def is_fresh(self, market, now):return True
        self.tracker=SimpleNamespace(multi_strategy_position=lambda *a:{'status':'flat'},
            multi_strategy_open_symbols=lambda *a:{'status':'available','symbols':list(self.opens)},
            paper_position_capacity=self.capacity, record_strategy_signal=self.events.append)
        self.session=SimpleNamespace(paper_automation_reconcile=SimpleNamespace(_tracker=self.tracker))
        self.monitor=MultiStrategyMonitor(settings_loader=lambda:self.options, now=lambda:self.now,
            provider_factory=lambda _:Provider(), chart_fetcher=self.chart, executor=self.execute,
            cooldown_check=lambda *a:True)
        self.patch=patch('kiwoom_autotrader.trading.multi_strategy.session_identity',return_value=('paper','fixture'))
        self.patch.start(); self.addCleanup(self.patch.stop)

    def candidate(self, symbol):
        return {'symbol':symbol,'available_at':self.now.isoformat(),
                'membership_evidence':{'verified':True},'source_hashes':['a'*64]}

    def capacity(self, *args):
        if self.block:raise ConcurrentPositionLimitReached('full')
        return {'limit':8,'occupied_symbols':[]}

    def chart(self, session, symbol, count):
        self.calls.append(('chart',symbol))
        return [MinuteBar(self.now.replace(second=0)-timedelta(minutes=21-i),100,100,
            source_symbol=symbol,source_mode='paper',source_query_started_at=self.now,
            source_response_sha256='a'*64,open_price=100,high_price=101,low_price=99) for i in range(21)]

    def execute(self, **kw):
        kw['before_send'](); self.orders.append(kw)
        return OrderReplies([], submitted=False, decision_reason='insufficient_cash')

    def tick(self):
        self.monitor.tick(self.session,lambda _:None);self.now+=timedelta(seconds=1)

    def next_minute(self):
        self.now=self.now.replace(second=1)+timedelta(minutes=1)

    def test_new_symbol_starts_baseline_then_signal_and_half_budget(self):
        self.tick();self.tick()
        self.assertFalse(self.orders)
        self.assertEqual(len([x for x in self.calls if x[0]=='discover']),1)
        self.change='20.1';self.next_minute();self.tick();self.tick()
        self.assertEqual({o['symbol'] for o in self.orders},{'005930','000660'})
        self.assertTrue(all(o['budget']==500_000 for o in self.orders))
        self.assertEqual(len([e for e in self.events if e['event_kind']=='eligible_signal']),2)
        outcomes=[e for e in self.events if e['event_kind']=='decision_outcome']
        self.assertTrue(all(e['reason']=='insufficient_cash' for e in outcomes))

    def test_open_symbols_precede_discovery_and_removed_position_is_retained(self):
        self.opens=['035420']
        self.tick()
        self.assertEqual(self.monitor._last_symbol,'035420')
        self.assertFalse(any(x[0]=='discover' for x in self.calls))
        self.tick()
        self.assertTrue(any(x[0]=='discover' for x in self.calls))

    def test_other_baselines_survive_dynamic_membership_change(self):
        self.tick();self.tick()
        fixed=self.monitor._states[('005930',S3)]
        self.dynamic=['035420'];self.next_minute()
        self.monitor._refresh_entries([('005930',500000)],
            {'enabled':True,'limit':20,'stocks':(),'max_positions':8},self.now,
            self.now.replace(second=0),self.options)
        self.assertEqual(self.monitor._states[('005930',S3)],fixed)
        self.assertNotIn(('000660',S3),self.monitor._states)
        self.assertNotIn(('035420',S3),self.monitor._states)

    def test_disappeared_then_reenrolled_symbol_cannot_replay_crossing(self):
        self.tick();self.tick();self.dynamic=[];self.next_minute();self.tick()
        self.dynamic=['000660'];self.change='20.1';self.next_minute();self.tick();self.tick()
        self.assertFalse([o for o in self.orders if o['symbol']=='000660'])

    def test_all_eligible_signals_preserved_when_position_cap_blocks(self):
        self.tick();self.tick();self.block=True;self.change='20.1';self.next_minute();self.tick();self.tick()
        self.assertFalse(self.orders)
        self.assertEqual(len([e for e in self.events if e['event_kind']=='eligible_signal']),2)
        self.assertEqual(len([e for e in self.events if e.get('reason')=='concurrent_position_limit_reached']),2)

    def test_theme_cap_rejection_keeps_signal_and_exact_skip_reason(self):
        from kiwoom_autotrader.trading.execution_log import ThemeConcentrationLimitReached
        self.tick(); self.tick(); self.change='20.1'
        self.tracker.paper_position_capacity=lambda *a: (_ for _ in ()).throw(ThemeConcentrationLimitReached('full'))
        self.next_minute(); self.tick(); self.tick()
        self.assertFalse(self.orders)
        self.assertEqual(len([e for e in self.events if e.get('reason')=='theme_concentration_limit_reached']),2)

    def test_candidate_binding_change_fails_closed_without_retry(self):
        self.tick();self.tick();self.binding='new-session';self.next_minute();self.tick();self.tick()
        self.assertEqual(len([x for x in self.calls if x[0]=='discover']),2)
        self.assertEqual(self.monitor.status()['error'],'strategy_input_or_gate_unavailable')

    def test_configured_base_rows_do_not_require_dynamic_ranking_membership(self):
        self.options['paper_strategy_stocks']=[{'code':'035420','budget':800000},{'code':'035720','budget':1000000}]
        self.base=['035420'];self.tick()
        self.assertIn('035420',self.monitor._entry_budgets)
        self.assertIn('035720',self.monitor._entry_budgets)
        self.assertEqual(self.monitor._entry_budgets['035420'],400000)
        self.assertTrue(self.monitor._entry_admitted('035420',self.now))
        self.assertTrue(self.monitor._entry_admitted('035720',self.now))

    def test_no_new_entry_at_cutoff_and_one_symbol_per_tick(self):
        self.tick()
        self.assertEqual(len([x for x in self.calls if x[0]=='chart']),1)
        self.now=self.now.replace(hour=15,minute=19,second=1);self.change='20.1';self.tick();self.tick()
        self.assertFalse(self.orders)
        self.assertEqual(self.monitor.status()['last_decision']['reason'],'entry_cutoff_reached')
        self.assertEqual(len([x for x in self.calls if x[0]=='discover']),1)

    def test_research_sink_failure_never_blocks_protective_exit(self):
        self.opens=['005930']
        self.tracker.multi_strategy_position=lambda mode,symbol,strategy,now: ({'status':'owned','owned_qty':10,
            'half_take_done':False} if strategy==S3 else {'status':'flat'})
        self.tracker.record_strategy_signal=lambda event: (_ for _ in ()).throw(OSError('fixture write failure'))
        self.monitor._exit_provider_factory=lambda _:SimpleNamespace(fetch=lambda *a,**k:{'symbol':'005930','current_price':90},
                                                                    is_fresh=lambda *a:True)
        self.monitor._account=lambda _: {'mode':'paper','exchange':'KRX','snapshot_changed':False,
            'holdings':[{'stk_cd':'A005930','crd_tp':'00','crd_loan_dt':'','rmnd_qty':'10','trde_able_qty':'10','pur_amt':'1000'}]}
        self.tick()
        self.assertEqual(len(self.orders),1)
        self.assertEqual(self.orders[0]['side'],'sell')
        self.assertFalse(self.monitor.status()['halted'])
        self.assertFalse(self.monitor.status()['research_signal_log']['ok'])

    def test_large_universe_retains_four_for_consecutive_forward_slots(self):
        self.dynamic=[f'{i:06d}' for i in range(100,120)]
        for _ in range(4):self.tick()
        first=self.monitor.status()['dynamic_scan']['active_entry_symbols']
        self.assertEqual(len(first),4)
        self.next_minute()
        for _ in range(4):self.tick()
        self.assertEqual(set(first),set(self.monitor.status()['dynamic_scan']['active_entry_symbols']))
        self.next_minute()
        for _ in range(4):self.tick()
        self.next_minute()
        for _ in range(4):self.tick()
        self.assertFalse(set(first)&set(self.monitor.status()['dynamic_scan']['active_entry_symbols']))

    def test_actual_sparse_bars_are_logged_but_never_synthesized(self):
        bars=self.chart(self.session,'005930',22)
        self.monitor._states[('005930',S3)]=EntryState(last_bar_start=bars[-1].start)
        self.monitor._record_observed_bars(self.tracker,'005930',bars[:-2]+bars[-1:],self.now)
        diag=self.monitor.status()['bar_window_diagnostics']['005930']
        self.assertEqual(diag['missing_count'],1)
        self.assertEqual(diag['classification'],'unobserved_not_proven_no_trade')
        self.assertTrue(diag['baseline_preserved'])
        self.assertFalse(diag['synthetic_bars_created'])
        self.assertEqual(len(self.events[-1]['bars']),20)
