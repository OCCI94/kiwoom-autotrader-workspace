"""Actual C signal evidence and special-first, non-backdated native allocation."""
from datetime import timedelta
from types import SimpleNamespace
import unittest
from unittest.mock import Mock
import tests.test_paper_experiment_monitor as fixture
from kiwoom_autotrader.trading import paper_experiment_rules as core
START=fixture.START;DAY=fixture.DAY;assignment=fixture.assignment


class SpecialAllocationTests(unittest.TestCase):
    setUp=fixture.MonitorTests.setUp
    make_monitor=fixture.MonitorTests.make_monitor
    market=fixture.MonitorTests.market
    bars=fixture.MonitorTests.bars
    submit=fixture.MonitorTests.submit
    one=fixture.MonitorTests.one
    seed=fixture.MonitorTests.seed

    def test_c_actual_signal_frozen_before_order_attempt_not_just_preopen_assignment(self):
        symbol='005930';self.clock=START+timedelta(seconds=2)
        first=START-timedelta(minutes=5)
        self.monitor.store.selection(DAY,'preopen',{'observed_at':first.isoformat(),
            'candidates':[{'symbol':symbol,'first_seen_at':first.isoformat()}]})
        self.monitor.store.selection(DAY,'prior:'+symbol,{'symbol':symbol,'previous_close':1000,
            'previous_session_day':'20261030','previous_close_available_at':first.isoformat(),'source_sha256':'c'*64})
        self.monitor._dispatch=Mock()
        self.monitor._one(self.session,self.tracker,self.settings,symbol,self.clock)
        marker=self.monitor.store.selection(DAY,'eligible:C:'+symbol)
        self.assertTrue(marker['result']['signal']);self.assertEqual(marker['result']['arm'],'EXP_C')
        self.assertEqual(marker['recorded_at'],self.clock.isoformat())
        self.assertEqual(marker['source_sha256'],core._digest({k:v for k,v in marker.items() if k!='source_sha256'}))
        self.monitor._dispatch.assert_called_once()
        self.clock=START+timedelta(minutes=5)
        value=self.monitor.store.assignment(DAY,symbol)
        self.monitor._one(self.session,self.tracker,self.settings,symbol,self.clock,value)
        self.assertEqual(self.monitor._dispatch.call_count,1)
        self.assertEqual(self.monitor.store.selection(DAY,'eligible:C:'+symbol),marker)

    def test_native_allocation_waits_until0906_but_first_seen_is_not_changed(self):
        symbol='005930';first=START-timedelta(minutes=2)
        self.monitor.store.selection(DAY,'first_seen:'+symbol,{'first_seen_at':first.isoformat()})
        for minute in (1,2,3,4,5):
            self.clock=START+timedelta(minutes=minute)
            self.assertIsNone(self.monitor._assignment(symbol,self.clock))
        self.clock=START+timedelta(minutes=6)
        value=self.monitor._assignment(symbol,self.clock)
        self.assertEqual(value['first_seen_at'],first.isoformat())
        self.assertEqual(value['assigned_at'],self.clock.isoformat())
        self.assertEqual(value['weights'],{a:20 for a in ('EXP_R','EXP_A','EXP_B','EXP_B_TP','EXP_E')})

    def test_initial_balanced_cohort_excludes_special_selected_before_controls_are_frozen(self):
        symbols=[f'{i:06d}' for i in range(11)];self.settings['gdcrs_stocks']=[{'code':s} for s in symbols]
        first=START-timedelta(minutes=2)
        for s in symbols:self.monitor.store.selection(DAY,'first_seen:'+s,{'first_seen_at':first.isoformat()})
        self.monitor.identity=('paper','fixture');self.monitor._one=Mock()
        self.monitor.extended_scheduler=SimpleNamespace(run_selection=Mock(),overrides=lambda day:{symbols[0]:{'arm':'EXP_ORB_A'}})
        self.clock=START+timedelta(minutes=5);self.monitor.tick(self.session,self.notify)
        self.assertIsNone(self.monitor.store.selection(DAY,'initial_intraday_cohort'))
        self.assertEqual(self.monitor.store.assignments(DAY),[])
        self.clock=START+timedelta(minutes=6);self.monitor.tick(self.session,self.notify)
        values=self.monitor.store.assignments(DAY)
        self.assertEqual(len(values),10);self.assertNotIn(symbols[0],[v['symbol'] for v in values])
        counts={arm:sum(v['arm']==arm for v in values) for arm in ('EXP_R','EXP_A','EXP_B','EXP_B_TP','EXP_E')}
        self.assertEqual(set(counts.values()),{2})
        self.assertTrue(all(v['first_seen_at']==first.isoformat() for v in values))
        self.assertTrue(all(v['assigned_at']==self.clock.isoformat() for v in values))

    def test_later_single_candidate_immediately_receives_seeded_assignment(self):
        self.clock=START+timedelta(minutes=17)
        value=self.monitor._assignment('005930',self.clock)
        self.assertIsNotNone(value);self.assertEqual(value['first_seen_at'],self.clock.isoformat())
        self.assertEqual(value,self.monitor._assignment('005930',self.clock+timedelta(minutes=1)))

    def test_realized_control_share_is_reported_without_probability_guarantee_claim(self):
        value=self.seed(assignment('EXP_B'));self.settings['gdcrs_stocks']=[{'code':value['symbol']}]
        self.monitor._one=Mock();self.monitor.tick(self.session,self.notify)
        status=self.monitor.status()['native_allocation']
        self.assertEqual(status['actual_denominator'],1);self.assertEqual(status['actual_counts']['EXP_B'],1)
        self.assertFalse(status['actual_control_share_at_least_15pct'])
        self.assertFalse(status['finite_realized_minimum_guaranteed'])
        self.assertEqual(status['target_probability_pct']['EXP_R'],20)


if __name__=='__main__':unittest.main()
