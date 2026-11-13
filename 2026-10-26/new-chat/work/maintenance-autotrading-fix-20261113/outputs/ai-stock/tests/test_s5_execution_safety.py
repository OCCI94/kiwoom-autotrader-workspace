"""S5 actual-unit clocks, durable owner/TTL restoration and shared limits.

All mutations below target private fixture databases. No market/login/HTTP call
is made. Complete S5 decision validation is tested by its own evidence suite.
"""
from copy import deepcopy
from datetime import timedelta
from pathlib import Path
import sqlite3
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from kiwoom_autotrader.trading.execution_log import (
    ExecutionTracker, ExecutionLogError, ThemeConcentrationLimitReached,
    ConcurrentPositionLimitReached, _verified_first_fill_timing)
from kiwoom_autotrader.trading.paper_policy import _AutomaticPaperGuard, PaperAutomationPolicyError
from kiwoom_autotrader.trading.unfilled_buy_ttl import TTL_STRATEGIES
from tests import test_b3_ledger as b3_fixture
from tests import test_cancellation_ledger_integration as cancel_fixture
from tests import test_theme_concentration as theme_fixture
from tests import test_unfilled_buy_ttl as ttl_fixture
from tests.test_b3_entry import FILL, NOW

S5='S5_steady_uptrend_vwap'
MODULE='kiwoom_autotrader.trading.execution_log'


def relabel(tracker):
    with sqlite3.connect(tracker.path) as db:
        for table in ('orders','intents','fills'):
            db.execute('UPDATE '+table+' SET strategy=?',(S5,))


class S5SafetyTests(unittest.TestCase):
    def fixture(self, cls):
        f=cls();f.setUp();self.addCleanup(f.doCleanups);return f

    def timing_detail(self):
        return {'verified_amount_source':'00_unit_sum_reconciled_to_cumulative_quantity',
            'unit_coverage':'quantity_reconciled','issues':[], 'stream_gap_observed':False,
            'unit_executions':[
                {'execution_id':'1','broker_time':'092025','reported_cost_observations':[
                    {'observed_at':(FILL-timedelta(seconds=4)).isoformat()},
                    {'observed_at':NOW.isoformat()}]},
                {'execution_id':'2','broker_time':'092030','reported_cost_observations':[
                    {'observed_at':FILL.isoformat()}]}]}

    def test_first_unit_broker_clock_not_ack_last_fill_or_duplicate_receipt(self):
        data=self.timing_detail()
        result=_verified_first_fill_timing('20261030',data,(FILL-timedelta(seconds=10)).isoformat(),NOW)
        self.assertEqual(result['first_fill_at'],(FILL-timedelta(seconds=5)).isoformat())
        self.assertEqual(result['first_fill_observed_at'],(FILL-timedelta(seconds=4)).isoformat())
        self.assertEqual(result['first_fill_execution_id'],'1')
        self.assertTrue(result['first_fill_timing_verified'])

    def test_first_unit_unknown_ambiguous_or_future_time_never_inferred(self):
        base=self.timing_detail()
        cases=[]
        for key,value in [('issues',['gap']),('unit_coverage','unconfirmed'),('stream_gap_observed',True)]:
            d=deepcopy(base);d[key]=value;cases.append(d)
        for key,value in [('broker_time',''),('broker_time','09:20:25'),('broker_time','092040'),
                          ('reported_cost_observations',[{'observed_at':'2026-10-30T09:20:26'}])]:
            d=deepcopy(base);d['unit_executions'][0][key]=value;cases.append(d)
        for d in cases:
            with self.subTest(detail=d):
                self.assertIsNone(_verified_first_fill_timing('20261030',d,(FILL-timedelta(seconds=10)).isoformat(),NOW))

    def test_owner_restores_same_actual_clock_after_restart_and_repeated_receipt(self):
        f=self.fixture(b3_fixture.B3LedgerTests);relabel(f.tracker)
        before=f.tracker.multi_strategy_position('paper','005930',S5,f.now)
        self.assertEqual((before['status'],before['owned_qty'],before['first_fill_at']),('owned',100,FILL.isoformat()))
        f.event('execution_stream',f.now,{'9203':'24','9001':'A005930','907':'2','913':'체결',
            '908':'092030','909':'1','914':'1000','915':'100','911':'100','903':'100000'})
        restarted=ExecutionTracker(log_dir=Path(f.tmp.name),now=lambda:f.now,settings_loader=lambda:{'max_positions_per_theme':0})
        after=restarted.multi_strategy_position('paper','005930',S5,f.now)
        self.assertEqual(after['first_fill_at'],before['first_fill_at'])
        self.assertEqual(after['first_fill_observed_at'],FILL.isoformat())
        self.assertTrue(after['first_fill_timing_verified'])
        self.assertEqual(restarted.multi_strategy_position('paper','005930','gdcrs',f.now)['status'],'unsupported')
        with self.assertRaises(ExecutionLogError):restarted.assert_legacy_ddcrs_allowed('paper','005930')

    def test_missing_or_interrupted_source_never_creates_s5_owned_position(self):
        f=self.fixture(b3_fixture.B3LedgerTests);relabel(f.tracker)
        other=ExecutionTracker(log_dir=Path(f.tmp.name)/'missing',now=lambda:f.now)
        self.assertEqual(other.multi_strategy_position('paper','005930',S5,f.now)['status'],'unsupported')
        f.event('stream_stopped',f.now,{})
        self.assertEqual(f.tracker.multi_strategy_position('paper','005930',S5,f.now)['status'],'unsupported')

    def test_partial_fill_pending_then_confirmed_cancel_restores_actual_lot_clock(self):
        f=self.fixture(cancel_fixture.CancellationLedgerIntegrationTests);relabel(f.tracker);f.fill()
        self.assertEqual(f.tracker.multi_strategy_position('paper','005930',S5,f.now)['status'],'pending')
        before=f.financial();f.confirm()
        position=f.tracker.multi_strategy_position('paper','005930',S5,f.now)
        self.assertEqual((position['status'],position['owned_qty'],position['basis_krw']),('owned',30,30000))
        self.assertEqual(position['first_fill_at'],FILL.isoformat())
        self.assertTrue(position['first_fill_timing_verified'])
        self.assertEqual(before,f.financial())
        # A partially filled first allocation does not grant an unearned B3 add.
        self.assertIsNone(f.tracker.multi_b3_add_candidate('paper','005930',S5,f.now))

    def test_zero_fill_confirmed_cancel_has_no_invented_clock_or_lot(self):
        f=self.fixture(cancel_fixture.CancellationLedgerIntegrationTests);relabel(f.tracker)
        f.confirm(100)
        position=f.tracker.multi_strategy_position('paper','005930',S5,f.now)
        self.assertEqual(position['status'],'flat_candidate')
        self.assertIsNone(position['first_fill_at'])
        with f.tracker._db() as db:
            self.assertEqual(f.tracker._daily_buy_entry_counts(db,'paper','005930','20261030')['accepted'],1)

    def test_s5_ttl_ack_preserves_reservation_and_original_financial_rows(self):
        f=self.fixture(ttl_fixture.TTLTests);relabel(f.tracker)
        self.assertIn(S5,TTL_STRATEGIES)
        f.test_ack_does_not_change_original_financial_rows_or_release_principal()
        with f.tracker._db() as db:
            self.assertEqual(f.tracker._daily_buy_entry_counts(db,'paper','005930','20261030')['accepted'],1)

    def test_s5_intent_is_counted_with_other_strategies_theme_and_position_slots(self):
        f=self.fixture(theme_fixture.ThemeCapTests)
        ident=f.prepare('042510',strategy='legacy_other_strategy')
        with f.tracker._db() as db:db.execute('UPDATE intents SET strategy=? WHERE id=?',(S5,ident))
        with self.assertRaises(ThemeConcentrationLimitReached):f.prepare('434480')
        with f.tracker._db() as db,self.assertRaises(ConcurrentPositionLimitReached):
            f.tracker._assert_position_capacity(db,'005930',f.now,2,principal=1000)

    def test_s5_is_paper_only_and_cannot_bypass_one_million_per_order_cap(self):
        context=SimpleNamespace(tracker=Mock(),strategy=S5,evidence=None)
        options={'paper_multi_strategy_enabled':True,'paper_automation_only':False,
                 'paper_automation_buy_cap':3000000,'paper_automation_symbol_cap':3000000}
        for mode,multi,qty in [('real',True,1),('paper',False,1),('paper',True,1001)]:
            session=SimpleNamespace(mode=mode)
            with self.subTest(mode=mode,multi=multi,qty=qty),patch('kiwoom_autotrader.trading.paper_policy.load_settings',return_value=dict(options,paper_multi_strategy_enabled=multi)):
                guard=_AutomaticPaperGuard(context,session,'buy','005930',qty,1000)
                with self.assertRaises(PaperAutomationPolicyError):guard._policy_enabled()

    def test_s5_missing_b3_or_old_version_evidence_is_rejected_before_any_order(self):
        for evidence in ({},{'strategy':S5,'strategy_version':'paper-multi-observed-v3'}):
            with self.subTest(evidence=evidence),self.assertRaises(ExecutionLogError):
                ExecutionTracker._assert_current_multi_entry_policy('paper','buy',S5,evidence,NOW)
        # Entry-only checks must never suppress separately validated exits.
        ExecutionTracker._assert_current_multi_entry_policy('paper','sell',S5,None,NOW)

    def test_time_stop_clock_is_independent_of_source_contract_and_cannot_be_reset(self):
        position={'first_fill_at':FILL.isoformat(),'first_fill_timing_verified':True}
        def evidence(now,first=FILL,reason='time_stop_30m'):
            return {'strategy':S5,'strategy_version':'paper-multi-observed-v4','source_kind':'broker_observation',
                'observed_at':now.isoformat(),'evaluated_at':now.isoformat(),
                'multi_strategy_signal':{'side':'sell','reason':reason,'position':{'first_fill_at':first.isoformat()}}}
        # This unit isolates the clock binding; complete v4 source validation
        # is independently exercised by the S5 evidence tests.
        with patch(MODULE+'.freeze_evidence',side_effect=lambda d:SimpleNamespace(as_dict=lambda:d)):
            at=FILL+timedelta(minutes=30)
            ExecutionTracker._assert_s5_exit_clock(S5,position,evidence(at),at)
            for now,data in [(at-timedelta(microseconds=1),evidence(at-timedelta(microseconds=1))),
                             (at,evidence(at,FILL+timedelta(seconds=1))),
                             (at,evidence(at-timedelta(seconds=6)))]:
                with self.subTest(now=now),self.assertRaises(ExecutionLogError):
                    ExecutionTracker._assert_s5_exit_clock(S5,position,data,now)
            cutoff=at.replace(hour=15,minute=19,second=0,microsecond=0)
            ExecutionTracker._assert_s5_exit_clock(S5,position,evidence(cutoff,reason='s5_regular_cutoff_exit'),cutoff)
            early=cutoff-timedelta(microseconds=1)
            with self.assertRaises(ExecutionLogError):
                ExecutionTracker._assert_s5_exit_clock(S5,position,evidence(early,reason='s5_regular_cutoff_exit'),early)

