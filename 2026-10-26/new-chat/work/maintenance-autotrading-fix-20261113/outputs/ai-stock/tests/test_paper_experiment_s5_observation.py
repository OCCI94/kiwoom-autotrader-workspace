"""Observation-only S5 cannot alter randomized assignment or create intent."""
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
import sqlite3
import unittest
from unittest.mock import Mock

from tests import test_paper_experiment_monitor as monitor_fixture
from tests.test_s5_source_bound_evidence import fixture


class AssignmentIndependentS5Tests(unittest.TestCase):
    def setUp(self):
        self.c=monitor_fixture.MonitorTests('runTest');self.c.setUp();self.addCleanup(self.c.doCleanups)
        c=self.c;self.assignment=c.seed(monitor_fixture.assignment('EXP_R'))
        self.symbol=self.assignment['symbol'];now,bars,market=fixture(62);c.clock=now
        self.bars=[replace(b,source_symbol=self.symbol) for b in bars]
        self.market=deepcopy(market);self.market['symbol']=self.symbol
        for key in ('s5_daily_context','s5_risk_context'):self.market[key]['symbol']=self.symbol
        self.market['s5_risk_context']['short_selling_evidence']['symbol']=self.symbol
        c.chart.side_effect=lambda *_:self.bars
        c.provider.fetch.side_effect=lambda symbol,now:{**self.market,
            'request_started_at':now.isoformat(),'observed_at':now.isoformat()}
        c.provider.fetch_s5_inputs.return_value={k:self.market[k] for k in ('s5_daily_context','s5_risk_context')}
        c.provider.s5_eligible_symbols=Mock(return_value=(self.symbol,))
        c.monitor._dispatch=Mock(side_effect=AssertionError('Observation must never dispatch'))
        self.profile={'s5_enabled':True}

    def observe(self):
        self.c.monitor._observe_s5_one(self.c.session,self.c.tracker,[self.symbol],self.profile)
        return self.c.monitor.s5_observations.get(self.symbol)

    def test_pass_under_r_assignment_has_no_order_intent_or_randomized_reassignment(self):
        value=self.observe();c=self.c
        self.assertEqual(value['status'],'PASS');self.assertTrue(value['eligible'])
        self.assertEqual(value['actual_assignment_arm'],'EXP_R');self.assertFalse(value['order_permission'])
        self.assertEqual(c.monitor.store.assignment('20261102',self.symbol),self.assignment)
        with sqlite3.connect(c.path) as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM attempts').fetchone()[0],0)
        self.assertEqual(c.orders,[]);self.assertEqual(c.events,[]);c.monitor._dispatch.assert_not_called()

    def test_once_per_boundary_persists_across_restart(self):
        original=self.observe();self.observe();self.c.chart.assert_called_once()
        replacement=self.c.make_monitor()
        replacement._observe_s5_one(self.c.session,self.c.tracker,[self.symbol],self.profile)
        self.assertEqual(replacement.s5_observations[self.symbol],original)
        self.c.chart.assert_called_once();self.assertEqual(self.c.orders,[])

    def test_ownership_block_does_not_hide_pattern_but_remains_without_order_authority(self):
        self.c.tracker.positions[(self.symbol,'EXP_R')]={'status':'unsupported','reason':'fixture_stream_gap'}
        value=self.observe();self.assertEqual(value['status'],'PASS')
        self.assertEqual(value['ownership_status'],'unsupported');self.assertFalse(value['order_permission'])
        self.assertEqual(self.c.orders,[])

    def test_hold_scope_and_input_error_not_fabricated(self):
        self.c.provider.s5_eligible_symbols.return_value=()
        self.assertIsNone(self.observe());self.c.chart.assert_not_called()
        self.c.provider.s5_eligible_symbols.return_value=(self.symbol,)
        self.c.chart.side_effect=ValueError('missing_observed_session_minutes')
        value=self.observe();self.assertEqual(value['status'],'UNOBSERVED');self.assertIsNone(value['eligible'])
        self.assertEqual(value['reason'],'missing_observed_session_minutes')
        self.assertEqual(self.c.monitor.s5_observation_scope['observation_evaluated_symbols'],[])
        self.assertEqual(self.c.monitor.s5_observation_scope['observation_attempted_symbols'],[self.symbol])

    def test_failed_vwap_conditions_and_exact_chart_reuse(self):
        self.market['current_price']=10000
        self.c.monitor._remember_s5_chart(self.c.session,self.symbol)
        value=self.observe();self.c.chart.assert_called_once()
        self.assertIn('current_price_above_vwap',value['failed_checks'])
        self.assertEqual(value['check_groups']['vwap']['status'],'fail');self.assertFalse(value['eligible'])

    def test_boundary_change_does_not_consume_new_boundary(self):
        def moved(*args):self.c.clock+=timedelta(minutes=3);return self.bars
        self.c.chart.side_effect=moved
        self.assertIsNone(self.observe());self.assertEqual(self.c.orders,[])
        self.assertEqual(self.c.monitor.store.assignments('20261102'),[self.assignment])

    def test_one_symbol_per_cycle_and_native_evaluation_not_duplicated(self):
        self.c.monitor.s5_evaluated[self.symbol]='2026-11-02T10:00:00+09:00'
        self.assertIsNone(self.observe());self.c.chart.assert_not_called()
        self.assertEqual(self.c.monitor.s5_observation_scope['native_evaluated_symbols'],[self.symbol])

    def test_tick_runs_real_assignment_path_before_supplemental_observation(self):
        c=self.c;c.settings.update(paper_experiment_s5_enabled=True,
            gdcrs_stocks=[{'code':self.symbol}])
        order=[]
        c.monitor._one=Mock(side_effect=lambda *args,**kw:order.append('real_assignment'))
        actual=c.monitor._observe_s5_one
        def observed(*args,**kwargs):order.append('observation');return actual(*args,**kwargs)
        c.monitor._observe_s5_one=observed
        c.monitor.tick(c.session,c.notify)
        self.assertEqual(order,['real_assignment','observation'])
        self.assertEqual(c.monitor.s5_observations[self.symbol]['status'],'PASS')
        self.assertEqual(c.monitor.store.assignment('20261102',self.symbol),self.assignment)
        self.assertFalse(c.monitor.status()['s5_integration']['observation_coverage']['order_permission'])


if __name__=='__main__':unittest.main()
