"""Slow real scans and local-only S5 ownership rechecks cannot starve."""
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
import json
import sqlite3
import unittest
from unittest.mock import Mock

from tests import test_paper_experiment_monitor as monitor_fixture
from tests import test_paper_experiment_s5 as native_fixture
from tests.test_s5_source_bound_evidence import fixture
from kiwoom_autotrader.trading.multi_strategy import S5


class SlowScanFairnessTests(unittest.TestCase):
    def test_more_than_five_minute_batch_preserves_queue_and_observes_every_target(self):
        c=monitor_fixture.MonitorTests('runTest');c.setUp();self.addCleanup(c.doCleanups)
        values=[];codes=[]
        for _ in range(16):
            value=c.seed(monitor_fixture.assignment('EXP_R',exclude=codes))
            values.append(value);codes.append(value['symbol'])
        now,bars,market=fixture(62);c.clock=now
        c.settings.update(paper_experiment_s5_enabled=True,gdcrs_stocks=[{'code':s} for s in codes])
        c.provider.s5_eligible_symbols=Mock(return_value=tuple(codes[:9]))
        c.chart.side_effect=lambda session,symbol:[replace(b,source_symbol=symbol) for b in bars]
        def inputs(symbol,at):
            value={k:deepcopy(market[k]) for k in ('s5_daily_context','s5_risk_context')}
            for v in value.values():v['symbol']=symbol
            value['s5_risk_context']['short_selling_evidence']['symbol']=symbol
            return value
        c.provider.fetch_s5_inputs.side_effect=inputs
        c.provider.fetch.side_effect=lambda symbol,at:{**market,'symbol':symbol,
            'request_started_at':at.isoformat(),'observed_at':at.isoformat()}
        visited=[]
        def slow_one(session,tracker,settings,symbol,at,assignment):
            visited.append(symbol);c.clock+=timedelta(seconds=80)
        c.monitor._one=Mock(side_effect=slow_one)
        for _ in range(9):c.monitor.tick(c.session,c.notify)
        self.assertEqual(set(visited[:16]),set(codes))
        self.assertGreater((c.clock-now).total_seconds(),5*60)
        with sqlite3.connect(c.path) as db:
            observed=[json.loads(r[0]) for r in db.execute("SELECT payload FROM selections WHERE kind LIKE 's5_observation_only:%'")]
        self.assertEqual({r['symbol'] for r in observed},set(codes[:9]))
        self.assertEqual(len(observed),9)
        self.assertTrue(all(r['evaluation_role']=='observation_only' and not r['order_permission'] for r in observed))
        self.assertEqual(c.orders,[]);self.assertEqual(c.events,[])
        self.assertEqual(sorted(c.monitor.store.assignments('20261102'),key=lambda a:a['symbol']),
                         sorted(values,key=lambda a:a['symbol']))


class S5LocalOwnershipRetryTests(unittest.TestCase):
    def setUp(self):
        self.n=native_fixture.NativeS5MonitorTests('runTest');self.n.setUp();self.addCleanup(self.n.doCleanups)
        self.c=self.n.c;self.symbol=self.n.symbol;self.assignment=self.n.value
        self.c.clock=self.c.clock.replace(second=19)
        self.c.tracker.positions[(self.symbol,S5)]={'status':'unsupported'}

    def run_one(self,retry=False):
        self.c.monitor._run_scan_symbol(self.c.session,self.c.tracker,self.c.settings,
            self.symbol,self.assignment,self.c.notify,local_retry=retry)

    def test_fresh_coherent_flat_at_49_seconds_gets_latest_boundary_evaluation(self):
        c=self.c;self.run_one()
        self.assertEqual(c.monitor.diagnostics[self.symbol]['reason'],'ownership_unsupported')
        self.assertEqual(c.monitor._s5_local_retry[self.symbol]['due_at'],'2026-11-02T10:02:29+09:00')
        self.assertIsNone(c.monitor._due_s5_local_retry([self.symbol],set()))
        c.chart.assert_not_called()
        c.clock+=timedelta(seconds=10)
        self.assertEqual(c.monitor._due_s5_local_retry([self.symbol],set()),self.symbol)
        self.run_one(retry=True);c.chart.assert_not_called()
        c.clock+=timedelta(seconds=20)
        c.tracker.positions[(self.symbol,S5)]={'status':'flat','owned_qty':0,'basis_krw':0}
        c.monitor._dispatch=Mock()
        self.run_one(retry=True)
        self.assertEqual(c.monitor.s5_evaluated[self.symbol],'2026-11-02T10:00:00+09:00')
        self.assertNotIn(self.symbol,c.monitor._s5_local_retry)
        self.assertEqual(c.monitor.diagnostics[self.symbol]['status'],'PASS')
        c.chart.assert_called_once()
        c.clock+=timedelta(seconds=5);self.run_one();c.chart.assert_called_once()
        c.monitor._dispatch.assert_called_once()  # Actual assigned path only.

    def test_retry_budget_bounded_and_old_boundary_never_replayed(self):
        c=self.c;self.run_one()
        for _ in range(12):
            c.clock+=timedelta(seconds=10);self.run_one(retry=True)
        state=c.monitor._s5_local_retry[self.symbol]
        self.assertEqual(state['attempts'],12);self.assertIsNone(state['due_at'])
        self.run_one();self.assertIsNone(c.monitor._s5_local_retry[self.symbol]['due_at'])
        self.assertIsNone(c.monitor._due_s5_local_retry([self.symbol],set()))
        c.clock=c.clock.replace(minute=5,second=0)
        self.assertIsNone(c.monitor._due_s5_local_retry([self.symbol],set()))
        self.assertNotIn(self.symbol,c.monitor._s5_local_retry)
        self.run_one();self.assertEqual(c.monitor._s5_local_retry[self.symbol]['boundary'],'2026-11-02T10:05:00+09:00')
        self.assertEqual(c.monitor._s5_local_retry[self.symbol]['attempts'],0)
        c.chart.assert_not_called();self.assertEqual(c.orders,[])


if __name__=='__main__':unittest.main()
