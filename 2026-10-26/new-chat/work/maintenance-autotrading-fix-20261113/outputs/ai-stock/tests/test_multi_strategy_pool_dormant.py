"""No live IPC: entry pool capacity and flat-only dormant data states."""
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import tests.test_dynamic_monitor as dynamic_fixture
import tests.test_multi_strategy_flat_candidate as flat_fixture
from kiwoom_autotrader.trading.multi_strategy import STRATEGIES, S3, EntryState, dormant_bar_reason
from kiwoom_autotrader.trading.paper_strategy_context import ContextUnavailable


class DormantMonitorTests(unittest.TestCase):
    def setUp(self):
        self.fx=flat_fixture.FlatCandidateMonitorTests('test_candidate_is_visible_and_first_observation_is_baseline')
        self.fx.setUp();self.addCleanup(self.fx.doCleanups)
        self.good_chart=self.fx.monitor._chart

    def test_insufficient_valid_bars_are_dormant_not_errors_or_zero_volume_claims(self):
        self.fx.monitor._chart=lambda *a,**kw:self.good_chart(*a,**kw)[:-1]
        self.fx.tick();status=self.fx.monitor.status()
        d=status['symbol_input_diagnostics']['005930']
        self.assertEqual(d['stage'],'dormant_waiting_volume')
        self.assertEqual(d['code'],'dormant_low_volume')
        self.assertEqual(d['data_reason'],'insufficient_completed_history')
        self.assertFalse(d['low_volume_cause_confirmed'])
        self.assertFalse(d['synthetic_bars_created'])
        self.assertNotIn('005930',status['symbol_errors'])
        self.assertEqual(self.fx.calls,[])

    def test_successful_empty_chart_has_explicit_data_shortage(self):
        self.fx.monitor._chart=lambda *a,**kw:[]
        self.fx.tick()
        d=self.fx.monitor.status()['dormant_symbols']['005930']
        self.assertEqual(d['data_reason'],'no_completed_bars')
        self.assertEqual(d['classification'],'unobserved_not_proven_no_trade')

    def test_valid_response_omitted_minutes_need_source_sequence_proof(self):
        rows=self.good_chart(None,'005930',22)
        rows=[replace(b,start=b.start-timedelta(minutes=3) if i<10 else b.start,
                      source_batch='batch',source_page=0,source_row=20-i)for i,b in enumerate(rows)]
        self.assertEqual(dormant_bar_reason(rows,'005930',self.fx.now),'omitted_minutes_or_latest_bar')
        invalid=[replace(b,source_row=b.source_row*2)for b in rows]
        self.assertIsNone(dormant_bar_reason(invalid,'005930',self.fx.now))
        self.fx.monitor._chart=lambda *a,**kw:rows
        self.fx.tick()
        self.assertNotIn('005930',self.fx.monitor.status()['symbol_errors'])

    def test_stale_latest_candle_fresh_response_is_data_waiting_not_price_fill(self):
        rows=[replace(b,start=b.start-timedelta(minutes=2))for b in self.good_chart(None,'005930',22)]
        self.fx.monitor._chart=lambda *a,**kw:rows
        self.fx.tick()
        self.assertEqual(self.fx.monitor.status()['dormant_symbols']['005930']['data_reason'],
                         'omitted_minutes_or_latest_bar')
        self.assertEqual(self.fx.calls,[])

    def test_invalid_price_identity_hash_adjustment_time_are_not_dormant(self):
        changes=({'close':0},{'source_symbol':'000660'},{'source_mode':'real'},
                 {'source_response_sha256':'bad'},{'adjustment':'1'},{'adjustment_rate':'1'},
                 {'source_query_started_at':self.fx.now-timedelta(minutes=2)},
                 {'source_query_started_at':self.fx.now+timedelta(seconds=1)},
                 {'volume':None},{'low_price':1000})
        for values in changes:
            with self.subTest(values=values):
                rows=self.good_chart(None,'005930',22)[:-1]
                rows[-1]=replace(rows[-1],**values)
                self.assertIsNone(dormant_bar_reason(rows,'005930',self.fx.now))
                self.fx.monitor._chart=lambda *a,**kw:rows
                self.fx.tick()
                status=self.fx.monitor.status()
                self.assertIn('005930',status['symbol_errors'])
                self.assertNotIn('005930',status['dormant_symbols'])
                self.fx.clock[0]+=timedelta(minutes=1)

    def test_network_or_provider_error_is_not_hidden_by_later_chart_shortage(self):
        def broken(*args,**kwargs):raise OSError('private network detail')
        self.fx.monitor._chart=broken;self.fx.tick()
        status=self.fx.monitor.status()
        self.assertIn('005930',status['symbol_errors'])
        self.assertNotIn('private',repr(status))
        self.fx.clock[0]+=timedelta(minutes=1)
        self.fx.monitor._chart=lambda *a,**kw:[];self.fx.tick()
        status=self.fx.monitor.status()
        self.assertIn('005930',status['symbol_errors'])
        self.assertNotIn('005930',status['dormant_symbols'])

    def test_integrity_error_persists_through_shortage_until_full_valid_recovery(self):
        def malformed(*args,**kwargs):
            bars=self.good_chart(*args,**kwargs)[:-1]
            bars[-1]=replace(bars[-1],source_response_sha256='invalid')
            return bars
        self.fx.monitor._chart=malformed;self.fx.tick()
        self.assertEqual(self.fx.monitor.status()['symbol_errors']['005930'],'bar_data_or_provenance_unverified')
        self.fx.clock[0]+=timedelta(minutes=1)
        self.fx.monitor._chart=lambda *a,**kw:[];self.fx.tick()
        status=self.fx.monitor.status()
        self.assertEqual(status['symbol_input_diagnostics']['005930']['code'],'bar_data_or_provenance_unverified')
        self.assertNotIn('005930',status['dormant_symbols'])
        self.fx.clock[0]+=timedelta(minutes=1);self.fx.monitor._chart=self.good_chart;self.fx.tick()
        self.assertNotIn('005930',self.fx.monitor.status()['symbol_errors'])
        self.assertEqual(self.fx.calls,[])

    def test_session_change_is_not_dormant(self):
        self.fx.monitor._chart=lambda *a,**kw:[]
        with patch('kiwoom_autotrader.trading.multi_strategy.session_identity',side_effect=[('paper','first'),('paper','changed')]):
            self.fx.tick()
        d=self.fx.monitor.status()['symbol_input_diagnostics']['005930']
        self.assertEqual(d['code'],'session_changed')
        self.assertNotEqual(d['stage'],'dormant_waiting_volume')

    def test_unknown_or_pending_ownership_retains_strict_warning(self):
        for status in ('unsupported','pending','unavailable'):
            self.fx.positions[S3]={'status':status}
            self.fx.monitor._chart=lambda *a,**kw:[]
            self.fx.tick()
            self.assertNotIn('005930',self.fx.monitor.status()['dormant_symbols'])
            self.assertEqual(self.fx.calls,[])
            self.fx.clock[0]+=timedelta(minutes=1)

    def test_dormant_clears_baseline_and_recovery_never_replays_old_cross(self):
        self.fx.tick()
        self.assertIn(('005930',S3),self.fx.monitor._states)
        self.fx.clock[0]+=timedelta(minutes=1)
        self.fx.monitor._chart=lambda *a,**kw:self.good_chart(*a,**kw)[:-1]
        self.fx.tick()
        self.assertNotIn(('005930',S3),self.fx.monitor._states)
        self.fx.clock[0]+=timedelta(minutes=1);self.fx.monitor._chart=self.good_chart;self.fx.tick()
        status=self.fx.monitor.status()
        self.assertNotIn('005930',status['dormant_symbols'])
        self.assertNotIn('005930',status['symbol_errors'])
        self.assertIn(('005930',S3),self.fx.monitor._states)
        self.assertEqual(self.fx.calls,[])
        self.fx.clock[0]+=timedelta(minutes=1);self.fx.tick()
        self.assertEqual(self.fx.calls,[])


class EntryPoolTests(unittest.TestCase):
    def setUp(self):
        self.fx=dynamic_fixture.DynamicMonitorTests('test_new_symbol_starts_baseline_then_signal_and_half_budget')
        self.fx.setUp();self.addCleanup(self.fx.doCleanups)

    def test_fixed25_preserved_plus15_dynamic_under_pool40(self):
        codes=[f'{i:06d}'for i in range(1000,1025)]
        self.fx.options['gdcrs_stocks']=[{'code':c,'budget':1000000}for c in codes[:3]]
        self.fx.options['paper_strategy_stocks']=[{'code':c,'budget':1000000}for c in codes[3:]]
        self.fx.options['paper_strategy_entry_pool_limit']=40
        self.fx.options['paper_dynamic_candidate_limit']=30
        self.fx.dynamic=[f'{i:06d}'for i in range(2000,2030)]
        self.fx.tick();status=self.fx.monitor.status()['dynamic_scan']
        self.assertEqual(status['candidate_count'],40)
        self.assertEqual(status['configured_entry_count'],25)
        self.assertEqual(status['dynamic_entry_count'],15)
        self.assertTrue(set(codes)<=set(self.fx.monitor._entry_budgets))
        self.assertEqual(len(status['pool_capacity_exclusions']),15)
        self.assertEqual(status['entry_membership_unverified'],[])
        self.assertTrue(all(self.fx.monitor._entry_admission[c]['kind']=='configured_base' for c in codes))
        self.assertTrue(all(x==500000 for x in self.fx.monitor._entry_budgets.values()))
        self.assertFalse(status['full_universe_every_minute_guaranteed'])

    def test_default_pool_remains30_and_fixed_overflow_is_explicit(self):
        self.fx.options['gdcrs_stocks']=[{'code':f'{i:06d}','budget':1000000}for i in range(1000,1011)]
        self.fx.options['paper_strategy_stocks']=[{'code':f'{i:06d}','budget':1000000}for i in range(2000,2020)]
        self.fx.tick()
        self.assertEqual(self.fx.monitor.status()['error'],'strategy_input_or_gate_unavailable')
        self.assertEqual(self.fx.orders,[])
        self.assertFalse(any(c[0]=='discover' for c in self.fx.calls))

    def test_legacy_gdcrs_only31_to100_symbols_remain_outside_expanded_pool_cap(self):
        for count in (31,100):
            with self.subTest(count=count):
                self.fx.options['gdcrs_stocks']=[{'code':f'{i:06d}','budget':1000000}
                                               for i in range(1000,1000+count)]
                self.fx.options['paper_dynamic_scan_enabled']=False
                self.fx.options['paper_strategy_stocks']=[]
                self.fx.monitor.reset();self.fx.next_minute();self.fx.tick()
                status=self.fx.monitor.status()
                self.assertIsNone(status['error'])
                self.assertEqual(len(self.fx.monitor._entry_budgets),count)
                self.assertEqual(status['dynamic_scan']['candidate_count'],count)
                self.assertIsNone(status['dynamic_scan']['entry_pool_limit'])
                self.assertFalse(status['dynamic_scan']['entry_pool_limit_applied'])
                self.assertFalse(any(c[0]=='discover'for c in self.fx.calls))

    def test_fixed_and_dynamic_duplicates_do_not_consume_extra_slots(self):
        self.fx.dynamic=['005930','000660']
        self.fx.options['paper_strategy_entry_pool_limit']=2
        self.fx.tick()
        self.assertEqual(set(self.fx.monitor._entry_budgets),{'005930','000660'})
        self.assertEqual(self.fx.monitor.status()['dynamic_scan']['candidate_count'],2)

    def test_owned_exit_has_priority_and_is_never_excluded_by_entry_cap(self):
        self.fx.options['paper_strategy_entry_pool_limit']=1
        self.fx.opens=['035420']
        self.fx.tick()
        self.assertEqual(self.fx.monitor._last_symbol,'035420')
        self.assertFalse(any(c[0]=='discover'for c in self.fx.calls))
        self.fx.tick()
        self.assertEqual(self.fx.monitor.status()['dynamic_scan']['candidate_count'],1)
        self.assertEqual(self.fx.monitor.status()['dynamic_scan']['pool_capacity_exclusions'],['000660'])

    def test_removed_dynamic_state_cleanup_and_protected_ownership_state_preserved(self):
        self.fx.tick();self.fx.tick()
        self.fx.monitor._dormant_symbols['000660']={'stage':'dormant_waiting_volume'}
        self.fx.dynamic=[];self.fx.next_minute();self.fx.tick()
        self.assertNotIn(('000660',S3),self.fx.monitor._states)
        self.assertNotIn('000660',self.fx.monitor.status()['dormant_symbols'])
        self.fx.monitor._states[('000660',S3)]=EntryState()
        self.fx.monitor._universe.add('000660');self.fx.next_minute()
        self.fx.monitor._refresh_entries([('005930',500000)],
            {'enabled':True,'limit':20,'stocks':(),'max_positions':8,'entry_pool_limit':40},
            self.fx.now,self.fx.now.replace(second=0),self.fx.options,['000660'])
        self.assertIn(('000660',S3),self.fx.monitor._states)

    def test_configured_base_can_enter_without_dynamic_rank_membership(self):
        self.fx.options['paper_strategy_stocks']=[{'code':'035420','budget':1000000}]
        self.fx.base=[];self.fx.dynamic=[]
        self.fx.tick();self.fx.tick()
        self.fx.change='20.1';self.fx.next_minute();self.fx.tick();self.fx.tick()
        self.assertEqual(len([o for o in self.fx.orders if o['symbol']=='035420']),1)
        self.assertEqual(self.fx.monitor._entry_admission['035420']['kind'],'configured_base')
        self.assertFalse(any(e.get('symbol')=='035420'and e.get('reason')=='candidate_membership_unverified'for e in self.fx.events))
        self.assertIn('035420',self.fx.monitor._entry_budgets)

    def test_current_admission_is_rechecked_at_send_boundary(self):
        self.fx.tick();self.fx.tick();self.fx.change='20.1';self.fx.next_minute()
        called=[]
        def execute(**kw):
            if kw['symbol']=='000660':
                self.fx.monitor._entry_admission.pop('000660',None)
                with self.assertRaisesRegex(ValueError,'candidate_membership_unverified'):kw['before_send']()
                called.append(True)
            return []
        self.fx.monitor._executor=execute
        self.fx.tick();self.fx.tick()
        self.assertEqual(called,[True])


if __name__=='__main__':unittest.main()
