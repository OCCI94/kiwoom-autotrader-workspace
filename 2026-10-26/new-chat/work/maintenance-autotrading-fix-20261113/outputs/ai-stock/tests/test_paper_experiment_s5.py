"""Native S5 proof: bounded full history and strict dispatch replay."""
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
import unittest

from kiwoom_autotrader.trading.paper_experiment_s5 import (
    build_s5_native_evidence, validate_s5_native_evidence, evaluate_s5_native, _digest)
from kiwoom_autotrader.trading.decision_evidence import freeze_evidence
from tests.test_s5_source_bound_evidence import fixture

class NativeS5ProofTests(unittest.TestCase):
    def build(self,minutes=60):
        now,bars,market=fixture(minutes)
        return now,bars,market,build_s5_native_evidence(bars,market,'003220',now)

    def test_full_prefix_delayed_boundary_and_sparse_real_rows(self):
        for minutes in (35,62,375):
            now,bars,market,proof=self.build(minutes)
            self.assertEqual(len(proof['minute_prefix']['rows']),minutes)
            self.assertEqual(validate_s5_native_evidence(proof,'003220',now),proof['evaluation'])
            self.assertLess(len(str(proof).encode()),57000)
        now,bars,market=fixture(60)
        bars=[bar for i,bar in enumerate(bars) if i not in (5,8,12,18,26,31)]
        proof=build_s5_native_evidence(bars,market,'003220',now)
        self.assertTrue(validate_s5_native_evidence(proof,'003220',now)['eligible'])
        self.assertFalse(proof['evaluation']['vwap']['exact_full_session_vwap'])
        self.assertFalse(proof['evaluation']['session_coverage']['synthetic_bars_created'])

    def test_hash_and_recomputed_checks_are_both_required(self):
        for kind in ('hash','price','evaluation','missing_short','expired','daily'):
            now,bars,market,proof=self.build()
            if kind=='hash':proof['quote']['current_price']+=1
            if kind=='price':proof['minute_prefix']['rows'][-1][4]=1
            if kind=='evaluation':proof['evaluation']['vwap']['volume']+=1
            if kind=='missing_short':del proof['risk_context']['short_selling_evidence']
            if kind=='expired':proof['risk_context']['expires_at']=now.isoformat()
            if kind=='daily':proof['daily_context']['rows'][-1]['date']='20261102'
            if kind!='hash':proof['input_sha256']=_digest({k:v for k,v in proof.items() if k!='input_sha256'})
            with self.subTest(kind=kind),self.assertRaises(ValueError):
                validate_s5_native_evidence(proof,'003220',now)

    def test_deadline_latest_boundary_and_symbol_bound(self):
        now,bars,market,proof=self.build(64)
        for when,symbol in ((now+timedelta(seconds=6),'003220'),
                            (now+timedelta(minutes=1),'003220'),(now,'000001')):
            with self.subTest(when=when,symbol=symbol),self.assertRaises(ValueError):
                validate_s5_native_evidence(proof,symbol,when)
        with self.assertRaises(ValueError):
            validate_s5_native_evidence(proof,'003220',now,signal_at=now.isoformat())

    def test_failure_groups_remain_distinct_and_missing_bucket_not_invented(self):
        now,bars,market=fixture()
        low=deepcopy(market);low['current_price']=10000
        result=evaluate_s5_native(bars,low,'003220',now)
        self.assertIn('current_price_above_vwap',result['failed_checks'])
        missing=[bar for bar in bars if bar.start.minute//5!=10]
        result=evaluate_s5_native(missing,market,'003220',now)
        self.assertFalse(result['eligible'])
        self.assertTrue(result['session_coverage']['empty_pattern_buckets'])
        self.assertFalse(result['session_coverage']['synthetic_bars_created'])


class NativeS5MonitorTests(unittest.TestCase):
    def setUp(self):
        from tests.test_paper_experiment_monitor import MonitorTests, START
        from kiwoom_autotrader.trading.paper_experiment_rules import assign_candidate
        from kiwoom_autotrader.trading.multi_strategy import S5
        case=MonitorTests('runTest');case.setUp();self.addCleanup(case.doCleanups);self.c=case
        case.settings['paper_experiment_s5_enabled']=True
        now,bars,market=fixture(62);case.clock=now
        for number in range(10000):
            value=assign_candidate('20261102',f'{number:06d}',START,assigned_at=START+timedelta(minutes=6),profile='native-six-s5-v1')
            if value['arm']==S5:break
        self.value=case.seed(value);self.symbol=value['symbol']
        self.bars=[replace(bar,source_symbol=self.symbol) for bar in bars]
        self.market=deepcopy(market);self.market['symbol']=self.symbol
        self.market['s5_daily_context']['symbol']=self.symbol
        self.market['s5_risk_context']['symbol']=self.symbol
        self.market['s5_risk_context']['short_selling_evidence']['symbol']=self.symbol
        case.chart.side_effect=lambda *_:self.bars
        def quote(symbol,now):
            return {**self.market,'request_started_at':now.isoformat(),'available_at':now.isoformat(),'observed_at':now.isoformat()}
        case.provider.fetch.side_effect=quote
        case.provider.fetch_s5_inputs.return_value={k:self.market[k] for k in ('s5_daily_context','s5_risk_context')}
        case.monitor.paused_arms.pop(S5,None)

    def test_pass_has_full_actual_order_proof_and_once_per_boundary_across_restart(self):
        c=self.c;c.one(self.value);c.one(self.value)
        self.assertEqual(len(c.orders),1,c.monitor.status())
        proof=c.contexts[-1][1]['evidence']
        self.assertEqual(proof['config']['experiment_signal_at'],'2026-11-02T10:00:00+09:00')
        self.assertEqual(len(proof['config']['experiment_s5_signal']['minute_prefix']['rows']),62)
        freeze_evidence(proof)
        replacement=c.make_monitor();replacement.paused_arms.pop(self.value['arm'],None)
        replacement._one(c.session,c.tracker,c.settings,self.symbol,c.clock,self.value)
        self.assertEqual(len(c.orders),1)
        self.assertTrue(replacement.diagnostics[self.symbol]['same_boundary_already_evaluated'])
        self.assertEqual(c.events[0]['signal_price_basis'],'current_quoted_price')
        self.assertIsNotNone(c.events[0]['signal_key'])

    def test_max_session_evidence_stays_under_64kb_with_actual_prefix(self):
        now,bars,market=fixture(374)
        self.c.clock=now
        self.bars=[replace(b,source_symbol=self.symbol) for b in bars]
        for key in ('s5_daily_context','s5_risk_context'):
            self.market[key]=market[key];self.market[key]['symbol']=self.symbol
        self.market['s5_risk_context']['short_selling_evidence']['symbol']=self.symbol
        self.market['current_price']=market['current_price']
        self.c.provider.fetch_s5_inputs.return_value={k:self.market[k] for k in ('s5_daily_context','s5_risk_context')}
        self.c.one(self.value)
        self.assertEqual(len(self.c.orders),1,self.c.monitor.status())
        frozen=freeze_evidence(self.c.contexts[-1][1]['evidence'])
        self.assertLess(len(frozen.json.encode()),65536)
        self.assertEqual(len(frozen.as_dict()['config']['experiment_s5_signal']['minute_prefix']['rows']),374)

    def test_pattern_and_vwap_failures_do_not_order(self):
        self.market['current_price']=10000
        self.c.one(self.value)
        diagnosis=self.c.monitor.diagnostics[self.symbol]
        self.assertEqual(self.c.orders,[])
        self.assertIn('current_price_above_vwap',diagnosis['failed_checks'])
        self.assertEqual(diagnosis['check_groups']['vwap']['status'],'fail')
        self.assertFalse(diagnosis['order_permission'])

    def test_queue_boundary_change_and_unknown_order_are_not_replayed(self):
        c=self.c
        original=c.monitor.submit
        def changed(*args,**kwargs):
            c.clock=c.clock.replace(minute=5,second=0)
            return original(*args,**kwargs)
        c.monitor.submit=changed
        with self.assertRaises(ValueError):c.one(self.value)
        self.assertEqual(c.orders,[])
        c.clock=c.clock.replace(minute=2,second=3)
        c.monitor.submit=original;c.one(self.value)
        self.assertEqual(c.orders,[])

    def test_owned_s5_time_stop_uses_verified_first_fill_and_skips_entry_risk(self):
        c=self.c;c.owned(self.value,entry_at=c.clock-timedelta(minutes=30))
        pos=c.tracker.positions[(self.symbol,self.value['arm'])]
        pos['first_fill_observed_at']=pos['first_fill_at'];pos['basis_krw']=30000
        c.provider.fetch_s5_inputs.side_effect=AssertionError('owned exit waited for DART')
        c.one(self.value)
        self.assertEqual(c.orders[0][0],'sell')
        evidence=c.contexts[-1][1]['evidence'];freeze_evidence(evidence)
        self.assertEqual(evidence['config']['experiment_s5_exit']['reason'],'time_stop_30m')
        c.chart.assert_not_called()

    def test_unverified_fill_clock_is_not_synthesized_for_time_stop(self):
        c=self.c;c.owned(self.value,entry_at=c.clock-timedelta(minutes=30))
        pos=c.tracker.positions[(self.symbol,self.value['arm'])];pos['basis_krw']=30000
        c.one(self.value)
        self.assertEqual(c.orders,[])
        self.assertEqual(c.monitor.diagnostics[self.symbol]['reason'],'owned_protection_monitoring')

    def test_cutoff_sell_does_not_require_pattern_or_supply_and_restores_owner(self):
        c=self.c;c.clock=c.clock.replace(hour=15,minute=19)
        c.owned(self.value,entry_at=c.clock-timedelta(minutes=1))
        pos=c.tracker.positions[(self.symbol,self.value['arm'])];pos['first_fill_observed_at']=pos['first_fill_at'];pos['basis_krw']=30000
        c.provider.fetch_s5_inputs.side_effect=AssertionError('exit risk gate')
        replacement=c.make_monitor();replacement.paused_arms.pop(self.value['arm'],None)
        owner,problem=replacement._owned_assignment(c.tracker,self.symbol,c.clock)
        self.assertIsNone(problem);self.assertEqual(owner,self.value)
        replacement._one(c.session,c.tracker,c.settings,self.symbol,c.clock,owner)
        self.assertEqual(c.orders[0][0],'sell')
        self.assertEqual(c.contexts[-1][1]['evidence']['config']['experiment_exit_reason'],'s5_regular_cutoff_exit')

    def test_active_status_is_visible_outside_session_without_orders(self):
        c=self.c;c.clock=c.clock.replace(hour=20)
        c.monitor.tick(c.session,c.notify)
        status=c.monitor.status()
        self.assertEqual(status['s5_integration']['status'],'ACTIVE')
        self.assertIn(self.value['arm'],status['active_arms'])
        self.assertNotIn(self.value['arm'],status['paused_arms'])
        self.assertEqual(status['reason'],'outside_session')
        self.assertEqual(c.orders,[]);c.chart.assert_not_called()

    def test_disabled_new_s5_entries_still_protect_native_owned_lot(self):
        c=self.c;c.settings['paper_experiment_s5_enabled']=False
        c.monitor.paused_arms[self.value['arm']]='native_s5_disabled'
        c.owned(self.value,entry_at=c.clock-timedelta(minutes=30))
        pos=c.tracker.positions[(self.symbol,self.value['arm'])]
        pos['first_fill_observed_at']=pos['first_fill_at'];pos['basis_krw']=30000
        c.one(self.value)
        self.assertEqual(c.orders[0][0],'sell')
        self.assertEqual(c.contexts[-1][1]['evidence']['config']['experiment_exit_reason'],'time_stop_30m')

    def test_stop_exit_uses_minimal_real_exit_provider_and_never_entry_or_chart(self):
        from kiwoom_autotrader.trading.paper_strategy_context import PaperStrategyContextProvider
        c=self.c;c.owned(self.value,entry_at=c.clock-timedelta(minutes=1))
        pos=c.tracker.positions[(self.symbol,self.value['arm'])]
        pos['first_fill_observed_at']=pos['first_fill_at'];pos['basis_krw']=30000
        c.session._issued=object();c.session.authenticated=True;c.session.token_usable=True
        c.monitor.exit_provider_factory=lambda session:PaperStrategyContextProvider(session,
            now=lambda:c.clock,exit_only=True,fetcher=lambda session,symbol:{'stk_cd':symbol,'cur_prc':'9500'})
        c.provider.fetch.side_effect=AssertionError('entry/ranking inputs used for exit')
        c.provider.fetch_s5_inputs.side_effect=AssertionError('entry risk used for exit')
        c.chart.side_effect=AssertionError('price stop waited for chart')
        c.one(self.value)
        self.assertEqual(c.orders[0][0],'sell')
        ev=c.contexts[-1][1]['evidence'];freeze_evidence(ev)
        self.assertEqual(ev['config']['experiment_exit_reason'],'stop_loss_or_sma20')
        c.chart.assert_not_called();c.provider.fetch.assert_not_called()

    def test_optional_sma_failure_preserves_owned_price_monitoring(self):
        c=self.c;c.owned(self.value,entry_at=c.clock-timedelta(minutes=1))
        pos=c.tracker.positions[(self.symbol,self.value['arm'])]
        pos['first_fill_observed_at']=pos['first_fill_at'];pos['basis_krw']=30000
        c.chart.side_effect=ValueError('chart unavailable')
        c.one(self.value)
        self.assertEqual(c.orders,[])
        self.assertEqual(c.monitor.diagnostics[self.symbol]['sma_status'],'unavailable')
        self.assertEqual(c.monitor.diagnostics[self.symbol]['reason'],'owned_protection_monitoring')

    def test_scalar_vwap_and_coverage_features_use_actual_numerator(self):
        from decimal import Decimal
        c=self.c;c.one(self.value)
        features=c.events[0]['features']
        vwap=features['vwap'];expected=(Decimal(c.events[0]['signal_price'])*3*vwap['volume']/vwap['hlc3_volume_numerator']-1)*10000
        self.assertEqual(Decimal(features['vwap_distance_bps']),expected)
        self.assertEqual(features['vwap_basis'],'observed_minutes')
        self.assertTrue(features['vwap_exact_full_session'])
        self.assertEqual(features['observed_rows'],60)
        self.assertEqual(features['missing_minutes'],0)

    def test_other_owner_and_pending_cannot_reach_chart_or_orders(self):
        c=self.c;c.tracker.positions[(self.symbol,'EXP_A')]={'status':'owned','owned_qty':1,'basis_krw':10000}
        c.one(self.value)
        self.assertEqual(c.monitor.diagnostics[self.symbol]['reason'],'ownership_unsupported')
        c.chart.assert_not_called();self.assertEqual(c.orders,[])

if __name__=='__main__':unittest.main()
