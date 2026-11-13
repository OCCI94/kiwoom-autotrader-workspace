from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
from unittest import TestCase
from pathlib import Path
from tempfile import TemporaryDirectory
from zoneinfo import ZoneInfo

from kiwoom_autotrader.api.minute_chart import MinuteBar
from kiwoom_autotrader.trading.decision_evidence import DecisionEvidenceError, freeze_evidence
from kiwoom_autotrader.trading.multi_strategy import EntryState, S2, S3
from kiwoom_autotrader.trading.multi_strategy_evidence import multi_strategy_evidence
from kiwoom_autotrader.trading.execution_log import ExecutionTracker, ExecutionLogError


class MultiStrategyEvidenceTests(TestCase):
    def setUp(self):
        self.now = datetime(2026,10,30,11,31,1,tzinfo=ZoneInfo('Asia/Seoul'))
        self.bars = [MinuteBar(self.now.replace(second=0)-timedelta(minutes=21-i),100,150,
            source_symbol='005930',source_mode='paper',source_query_started_at=self.now,
            source_response_sha256='a'*64,open_price=100,high_price=101,low_price=99) for i in range(21)]
        self.market = {'symbol':'005930','request_started_at':self.now.isoformat(),
            'observed_at':self.now.isoformat(),'available_at':self.now.isoformat(),
            'source':'ka10001_normalized_response','source_sha256':'b'*64,
            'source_market_timestamp':None,'source_freshness_verified':False,
            'current_price':100,'day_high':101,'upper_limit_price':130,'change_pct':'20.1',
            'session_binding':'must-not-be-stored'}
        self.baseline=EntryState(self.bars[-1].start-timedelta(minutes=1),101,False,Decimal('19.9'))

    def build(self, **changes):
        args=dict(strategy=S3,symbol='005930',side='buy',reason='observed_20pct_crossing',budget=1_000_000,
            position={'status':'flat'},bars=self.bars,market=self.market,observed_at=self.now,baseline=self.baseline,
            source_kind='synthetic')
        args.update(changes)
        return multi_strategy_evidence(**args)

    def test_independent_strategy_evidence_preserves_input_without_fake_cross(self):
        evidence=self.build()
        out=evidence.as_dict()
        self.assertIsNone(out['difference'])
        self.assertEqual(out['multi_strategy_signal']['baseline']['previous_change_pct'],'19.9')
        self.assertEqual(out['multi_strategy_signal']['ohlcv'][-1]['volume'],150)
        self.assertNotIn('session_binding',evidence.json)
        self.market['current_price']=999
        self.assertEqual(out['multi_strategy_signal']['market']['current_price'],100)
        self.assertEqual(freeze_evidence(out),evidence)

    def test_missing_or_future_or_cross_contamination_rejected(self):
        for changes in ({'bars':self.bars[:-1]}, {'baseline':EntryState()},
                        {'budget':1_000_001}, {'market':dict(self.market,source_sha256=None)},
                        {'market':dict(self.market,available_at=(self.now+timedelta(seconds=1)).isoformat())},
                        {'evaluated_at':self.now+timedelta(seconds=6)}):
            with self.subTest(changes=tuple(changes)),self.assertRaises(DecisionEvidenceError):
                self.build(**changes)
        out=self.build().as_dict();out['difference']=100
        with self.assertRaises(DecisionEvidenceError):freeze_evidence(out)
        with self.assertRaises(DecisionEvidenceError):self.build(strategy=S2)

    def test_unknown_ohlcv_cannot_become_buy_evidence(self):
        with self.assertRaises(DecisionEvidenceError):
            self.build(bars=[replace(b,high_price=None) for b in self.bars])

    def test_v3_s2_requires_opening_price_but_historical_v2_is_unchanged(self):
        value=self.build(strategy=S2,reason='shallow_pullback_high_breakout',
            market={**self.market,'day_open':99,'change_pct':'5'},
            baseline=EntryState(self.bars[-1].start-timedelta(minutes=1),99,True)).as_dict()
        self.assertEqual(value['strategy_version'],'paper-multi-observed-v3')
        value['multi_strategy_signal']['market']['day_open']=100
        with self.assertRaises(DecisionEvidenceError):freeze_evidence(value)
        value['strategy_version']='paper-multi-observed-v2'
        del value['multi_strategy_signal']['market']['day_open']
        self.assertEqual(freeze_evidence(value).as_dict()['strategy_version'],'paper-multi-observed-v2')

    def test_historical_s2_evidence_cannot_authorize_new_initial_order(self):
        value=self.build(strategy=S2,reason='shallow_pullback_high_breakout',
            market={**self.market,'day_open':99,'change_pct':'5'},
            baseline=EntryState(self.bars[-1].start-timedelta(minutes=1),99,True)).as_dict()
        ExecutionTracker._assert_current_multi_entry_policy('paper','buy',S2,value,self.now)
        value['strategy_version']='paper-multi-observed-v2'
        del value['multi_strategy_signal']['market']['day_open']
        legacy=freeze_evidence(value)
        with TemporaryDirectory() as tmp:
            tracker=ExecutionTracker(log_dir=Path(tmp),now=lambda:self.now,settings_loader=lambda:{})
            with self.assertRaisesRegex(ExecutionLogError,'신규 S2'):
                tracker.prepare_order('paper','buy','005930',1,S2,evidence=legacy,price=100)
            self.assertFalse(tracker.path.exists())
        ExecutionTracker._assert_current_multi_entry_policy('paper','sell',S2,value,self.now)
        value['config']['multi_b3_stage']='add'
        ExecutionTracker._assert_current_multi_entry_policy('paper','buy',S2,value,self.now)

    def test_derived_bullish_diagnostic_does_not_change_v3_order_evidence(self):
        args={'strategy':S2,'reason':'shallow_pullback_high_breakout',
            'market':{**self.market,'day_open':99,'change_pct':'5','intraday_bullish_candle':False},
            'baseline':EntryState(self.bars[-1].start-timedelta(minutes=1),99,True)}
        evidence=self.build(**args)
        self.assertNotIn('intraday_bullish_candle',evidence.as_dict()['multi_strategy_signal']['market'])
        args['market']['intraday_bullish_candle']=True
        self.assertEqual(evidence,self.build(**args))
        args['market']['day_open']=101
        with self.assertRaises(DecisionEvidenceError): self.build(**args)

    def test_protective_sell_without_minute_bars_keeps_sma_unknown(self):
        out=self.build(strategy=S2,side='sell',reason='stop_loss_or_sma20',quantity=10,bars=[],baseline=None,
            position={'generation_id':'f'*32,'owned_qty':10,'basis_krw':1050,'half_take_done':False}).as_dict()
        self.assertEqual(out['config']['multi_exit_kind'],'full')
        self.assertIsNone(out['candle_start'])
        self.assertEqual(out['multi_strategy_signal']['sma20_status'],'unavailable')

    def test_sma_sell_can_use_observed_close_only_bars(self):
        out=self.build(strategy=S2,side='sell',reason='stop_loss_or_sma20',quantity=10,baseline=None,
            bars=[replace(b,high_price=None,open_price=None,low_price=None,volume=None) for b in self.bars[-20:]],
            position={'generation_id':'f'*32,'owned_qty':10,'basis_krw':1050,'half_take_done':False}).as_dict()
        self.assertEqual(out['multi_strategy_signal']['sma20'],'100')
        self.assertEqual(out['multi_strategy_signal']['sma20_status'],'observed')

    def test_sell_cannot_exceed_proven_qty_or_omit_generation(self):
        for position in ({'owned_qty':10,'basis_krw':1000,'half_take_done':False},
                         {'generation_id':'f'*32,'owned_qty':9,'basis_krw':1000,'half_take_done':False}):
            with self.assertRaises(DecisionEvidenceError):
                self.build(side='sell',reason='take_profit_10pct',quantity=10,bars=[],baseline=None,position=position)
