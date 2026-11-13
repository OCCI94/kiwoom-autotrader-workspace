from dataclasses import replace
from datetime import datetime,timedelta
import unittest
from zoneinfo import ZoneInfo

from kiwoom_autotrader.api.minute_chart import MinuteBar
from kiwoom_autotrader.trading.multi_strategy import valid_completed_bars,valid_sma_bars,EntryState,entry_signal,S3
from kiwoom_autotrader.trading.multi_strategy_evidence import multi_strategy_evidence
from kiwoom_autotrader.trading.decision_evidence import DecisionEvidenceError,freeze_evidence
from decimal import Decimal


class ActualObservedWindowTests(unittest.TestCase):
    def setUp(self):
        self.now=datetime(2026,10,30,11,31,1,tzinfo=ZoneInfo('Asia/Seoul'))

    def bars(self,missing=()):
        ends=[self.now.replace(second=0)-timedelta(minutes=i) for i in range(24,0,-1)]
        ends=[x for x in ends if x.minute not in missing][-21:]
        return [MinuteBar(x,100,100,source_symbol='005930',source_mode='paper',source_batch='fixture',
            source_page=0,source_row=len(ends)-i-1,source_query_started_at=self.now,source_response_sha256='a'*64,
            open_price=100,high_price=101,low_price=99) for i,x in enumerate(ends)]

    def evidence(self,bars):
        market=dict(symbol='005930',request_started_at=self.now.isoformat(),observed_at=self.now.isoformat(),
            available_at=self.now.isoformat(),source='ka10001_normalized_response',source_sha256='b'*64,
            source_market_timestamp=None,source_freshness_verified=False,current_price=100)
        return multi_strategy_evidence(strategy=S3,symbol='005930',side='buy',reason='observed_20pct_crossing',
            budget=500000,position={'status':'flat'},bars=bars,market=market,observed_at=self.now,source_kind='synthetic',
            baseline=EntryState(self.now.replace(second=0)-timedelta(minutes=2),101,False,Decimal('19.9')))

    def test_one_and_two_omissions_preserve_real_rows_and_explicit_count_basis(self):
        for missing in ((17,),(17,24)):
            bars=self.bars(missing)
            self.assertTrue(valid_completed_bars(bars,'005930',self.now))
            self.assertTrue(valid_sma_bars(bars,'005930',self.now))
            out=self.evidence(bars).as_dict()
            policy=out['multi_strategy_signal']['bar_window']
            self.assertEqual(policy['missing_minutes'],len(missing))
            self.assertEqual(policy['missing_cause'],'unknown')
            self.assertEqual(policy['sma_basis'],'last_20_actual_observed_closes')
            self.assertEqual(len(out['bars']),21)
            self.assertFalse(policy['synthetic_bars_created'])
            self.assertEqual(freeze_evidence(out).as_dict(),out)

    def test_mismatched_page_or_removed_source_rows_are_not_gap_proof(self):
        bars=self.bars((17,))
        for bad in (replace(bars[0],source_batch=None),replace(bars[0],source_page=1),
                    replace(bars[0],source_row=99),replace(bars[0],source_response_sha256='c'*64)):
            values=[bad]+bars[1:]
            self.assertFalse(valid_completed_bars(values,'005930',self.now))
            with self.assertRaises(DecisionEvidenceError):self.evidence(values)

    def test_larger_gap_duplicate_future_stale_overnight_and_synthetic_are_rejected(self):
        bars=self.bars((17,18,19))
        self.assertFalse(valid_completed_bars(bars,'005930',self.now))
        bars=self.bars()
        for bad in (replace(bars[-1],start=bars[-2].start),replace(bars[-1],start=self.now.replace(second=0)),
                    replace(bars[-1],source_query_started_at=self.now+timedelta(seconds=1)),
                    replace(bars[-1],source_mode='real'),replace(bars[-1],gap_proof=object()),
                    replace(bars[0],start=bars[0].start-timedelta(days=1))):
            values=bars[:-1]+[bad] if bad.start>=bars[0].start else [bad]+bars[1:]
            self.assertFalse(valid_completed_bars(values,'005930',self.now))
        self.assertFalse(valid_completed_bars(bars,'005930',self.now+timedelta(minutes=1)))

    def test_gap_tolerance_never_replays_a_missed_signal_observation(self):
        bars=self.bars((17,))
        state=EntryState(bars[-1].start-timedelta(minutes=2),100,False,Decimal('19.9'))
        self.assertIsNone(entry_signal(S3,state,bars,dict(symbol='005930',change_pct='20.1',
            current_price=100,upper_limit_price=130),symbol='005930',now=self.now))

    def test_v1_historical_evidence_is_still_readable_but_does_not_gain_gap_policy(self):
        out=self.evidence(self.bars()).as_dict()
        out['strategy_version']='paper-multi-observed-v1'
        del out['multi_strategy_signal']['market']['day_open']
        del out['multi_strategy_signal']['bar_window'];del out['multi_strategy_signal']['b3_add_on']
        self.assertEqual(freeze_evidence(out).as_dict()['strategy_version'],'paper-multi-observed-v1')
