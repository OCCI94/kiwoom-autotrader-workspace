from dataclasses import replace
from datetime import datetime,timedelta
from types import SimpleNamespace
from unittest.mock import patch
import unittest
from zoneinfo import ZoneInfo

from kiwoom_autotrader.api.minute_chart import MinuteBar
from kiwoom_autotrader.trading.multi_b3 import initial_config,evaluate_add_on
from kiwoom_autotrader.trading.multi_strategy import S3,MultiStrategyMonitor
from kiwoom_autotrader.trading.multi_strategy_evidence import multi_strategy_evidence
from kiwoom_autotrader.trading.decision_evidence import DecisionEvidenceError,freeze_evidence


class MultiB3Tests(unittest.TestCase):
    def setUp(self):
        self.now=datetime(2026,10,30,11,31,1,tzinfo=ZoneInfo('Asia/Seoul'))
        self.settings={'paper_multi_strategy_enabled':True,'paper_multi_strategy_ids':[S3],
            'gdcrs_initial_entry_pct':50,'gdcrs_add_on_profit_bps':50,
            'gdcrs_stocks':[{'code':'005930','budget':1_000_000}]}
        self.bars=[MinuteBar(self.now.replace(second=0)-timedelta(minutes=21-i),100,150 if i==20 else 100,
            source_symbol='005930',source_mode='paper',adjustment='',adjustment_rate='',
            source_batch='test',source_page=0,source_row=20-i,source_response_sha256='a'*64,
            source_query_started_at=self.now,open_price=100,high_price=101,low_price=99) for i in range(21)]
        self.config=initial_config(self.settings,'005930',500000)
        self.candidate={'initial_intent_id':'f'*32,'initial_fill_observed_at':self.now.replace(minute=29,second=20).isoformat(),
            'initial_filled_qty':100,'initial_filled_principal_krw':10000,'config':self.config}
        self.position={'status':'owned','generation_id':'f'*32,'owned_qty':100,'basis_krw':10000,
            'half_take_done':False,'overnight_unobserved_interval':False}
        self.market={'symbol':'005930','current_price':101,'request_started_at':self.now.isoformat(),
            'available_at':self.now.isoformat(),'observed_at':self.now.isoformat(),
            'source':'ka10001_normalized_response','source_sha256':'b'*64,
            'source_market_timestamp':None,'source_freshness_verified':False}

    def evaluate(self,**changes):
        args=dict(bars=self.bars,market=self.market,position=self.position,now=self.now,symbol='005930',settings=self.settings)
        args.update(changes)
        return evaluate_add_on(self.candidate,**args)

    def evidence(self,result):
        return multi_strategy_evidence(strategy=S3,symbol='005930',side='buy',reason='add_on_strength',
            budget=result['budget'],position=self.position,bars=self.bars,market=self.market,observed_at=self.now,
            b3_config=result['config'],b3_add_on=result['addition'],source_kind='synthetic')

    def test_default_disabled_and_dynamic_initial_half_preserves_original_total(self):
        self.assertIsNone(initial_config({},'005930',1_000_000))
        self.assertEqual(self.config['multi_b3_total_budget_krw'],1_000_000)
        self.assertEqual(initial_config(self.settings,'097520',500000),self.config)
        self.assertIsNone(initial_config(self.settings,'005930',250000))

    def test_same_owner_add_proves_gain_volume_path_and_half_budget(self):
        result=self.evaluate()
        self.assertTrue(result['eligible'],result)
        self.assertEqual(result['budget'],500000)
        out=self.evidence(result).as_dict()
        self.assertEqual(out['strategy'],S3)
        self.assertEqual(out['config']['multi_b3_initial_intent_id'],self.position['generation_id'])
        self.assertEqual(out['config']['max_daily_entries_per_symbol'],2)
        self.assertEqual(freeze_evidence(out).as_dict(),out)

    def test_first_post_fill_completed_bar_and_full_saved_config_agree_with_gateway(self):
        self.candidate['initial_fill_observed_at']=self.now.replace(minute=30,second=20).isoformat()
        self.candidate['config']=dict(self.config,enabled=True,reentry_cooldown_minutes=20)
        result=self.evaluate()
        self.assertTrue(result['eligible'],result)
        self.assertEqual(len(result['addition']['trend_bars']),20)
        self.assertEqual(self.evidence(result).as_dict()['config']['multi_b3_stage'],'add')

    def test_loss_low_volume_unknown_basis_policy_partial_and_overnight_fail_closed(self):
        self.assertEqual(self.evaluate(market=dict(self.market,current_price=100))['reason'],'add_on_below_profit_threshold')
        self.assertEqual(self.evaluate(bars=self.bars[:-1]+[replace(self.bars[-1],volume=149)])['reason'],'add_on_volume_insufficient')
        for patch in ({'basis_krw':10001},{'generation_id':'e'*32},{'half_take_done':True},{'overnight_unobserved_interval':True}):
            self.assertFalse(self.evaluate(position=dict(self.position,**patch))['eligible'])
        self.assertFalse(self.evaluate(settings=dict(self.settings,gdcrs_add_on_profit_bps=60))['eligible'])

    def test_post_fill_unknown_gap_or_prior_negative_sma_never_authorizes_recovery_add(self):
        self.assertFalse(self.evaluate(bars=self.bars[:-2]+[self.bars[-1]])['eligible'])
        negative=[replace(b,close=101,high_price=102,open_price=101,low_price=100) if i<16 else b
                  for i,b in enumerate(self.bars)]
        self.assertEqual(self.evaluate(bars=negative)['reason'],'add_on_negative_sma_observed')

    def test_frozen_proof_rejects_forged_ratio_profit_owner_or_omitted_trend(self):
        out=self.evidence(self.evaluate()).as_dict()
        for key,value in (('budget',500001),):
            bad=dict(out,**{key:value})
            with self.assertRaises(DecisionEvidenceError):freeze_evidence(bad)
        for field,value in (('trend_bars',[]),('trend_timeline',[])):
            import copy
            bad=copy.deepcopy(out);bad['multi_strategy_signal']['b3_add_on'][field]=value
            with self.assertRaises(DecisionEvidenceError):freeze_evidence(bad)

    def test_monitor_uses_same_owner_add_after_exit_checks_and_keeps_no_extra_chart_query(self):
        calls=[];outer=self
        class Provider:
            def fetch(self,*args,**kwargs):return dict(outer.market)
            def is_fresh(self,*args):return True
        class Tracker:
            def multi_strategy_open_symbols(self,*args):return {'status':'available','symbols':['005930']}
            def multi_strategy_position(self,mode,symbol,strategy,now):
                return dict(outer.position) if strategy==S3 else {'status':'flat'}
            def multi_b3_add_candidate(self,*args):return outer.candidate
            def record_strategy_signal(self,event):return None
        tracker=Tracker();session=SimpleNamespace(mode='paper',paper_automation_reconcile=SimpleNamespace(_tracker=tracker))
        def chart(*args,**kwargs):calls.append(('chart',kwargs['count']));return outer.bars
        report={'mode':'paper','exchange':'KRX','snapshot_changed':False,'holdings':[
            {'stk_cd':'A005930','crd_tp':'00','crd_loan_dt':'','rmnd_qty':'100','trde_able_qty':'100','pur_amt':'10000'}]}
        monitor=MultiStrategyMonitor(settings_loader=lambda:self.settings,chart_fetcher=chart,
            provider_factory=lambda _:Provider(),exit_provider_factory=lambda _:Provider(),
            account_fetcher=lambda _:report,now=lambda:self.now,cooldown_check=lambda *a:True,
            executor=lambda **kw:calls.append(('order',kw)) or SimpleNamespace(submitted=True,uncertain=False))
        with patch('kiwoom_autotrader.trading.multi_strategy.session_identity',return_value=('paper','fixture')):
            monitor.tick(session,lambda _:None)
        self.assertEqual(calls[0],('chart',62))
        order=calls[1][1]
        self.assertEqual((order['strategy'],order['side'],order['reason']),(S3,'buy','add_on_strength'))
        self.assertEqual(order['budget'],500000)
        self.assertEqual(len([c for c in calls if c[0]=='chart']),1)
