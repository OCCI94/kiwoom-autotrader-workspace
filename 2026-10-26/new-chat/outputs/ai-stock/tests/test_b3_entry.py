import unittest
from datetime import datetime, timedelta
from dataclasses import replace
from zoneinfo import ZoneInfo

from kiwoom_autotrader.api.minute_chart import MinuteBar
from kiwoom_autotrader.trading.b3_entry import volume_window, price_above_basis, post_fill_trend_is_nonnegative, B3EvidenceUnavailable
from kiwoom_autotrader.trading.decision_evidence import b3_addition_evidence, freeze_evidence, DecisionEvidenceError

KST=ZoneInfo('Asia/Seoul')
START=datetime(2026,10,30,9,0,tzinfo=KST)
NOW=START+timedelta(minutes=21,seconds=10)
QUERY=START+timedelta(minutes=21,seconds=2)
FILL=START+timedelta(minutes=20,seconds=30)


def bars(volume=150):
    return tuple(MinuteBar(START+timedelta(minutes=i), 1005,
        volume=volume if i==20 else 100,acc_volume=100*(i+1),
        adjustment='',adjustment_rate='',source_symbol='005930',source_mode='paper',
        source_batch='test',source_page=0,source_row=20-i,
        source_response_sha256='a'*64,source_query_started_at=QUERY) for i in range(21))


def config():
    return {'enabled':True,'short_period':5,'long_period':20,'market_start_time':'09:00',
        'market_end_time':'15:30','stocks':[{'code':'005930','budget':1000000}],
        'min_cross_bps':0,'reentry_cooldown_minutes':0,'b3_stage':'add',
        'b3_initial_intent_id':'a'*32,'b3_initial_entry_pct':50,
        'b3_add_on_profit_bps':50,'b3_total_budget_krw':1000000}


class B3EntryTests(unittest.TestCase):
    def test_volume_ratio_exact_boundary_and_below(self):
        self.assertTrue(volume_window(bars(),now=NOW,symbol='005930',initial_fill_observed_at=FILL)[1])
        self.assertFalse(volume_window(bars(149),now=NOW,symbol='005930',initial_fill_observed_at=FILL)[1])

    def test_missing_nonpaper_and_fill_after_close_fail_closed(self):
        for bad in (bars()[:-1], tuple(b for i,b in enumerate(bars()) if i!=10),
                    tuple(MinuteBar(b.start,b.close,volume=None) for b in bars())):
            with self.subTest(count=len(bad)),self.assertRaises(B3EvidenceUnavailable):
                volume_window(bad,now=NOW,symbol='005930',initial_fill_observed_at=FILL)
        with self.assertRaises(B3EvidenceUnavailable):
            volume_window(bars(),now=NOW,symbol='005930',initial_fill_observed_at=NOW)

    def test_basis_threshold_exact_and_invalid(self):
        self.assertTrue(price_above_basis(1005,100,100000,50))
        self.assertFalse(price_above_basis(1004,100,100000,50))
        with self.assertRaises(B3EvidenceUnavailable):price_above_basis(1005,0,100000,50)

    def test_post_fill_negative_is_evidence_even_if_prior_history_missing(self):
        negative=list(bars());negative[-1]=replace(negative[-1],close=800)
        safe, _ = post_fill_trend_is_nonnegative(tuple(negative),now=NOW,symbol='005930',
            initial_fill_observed_at=FILL,short=5,long=20)
        self.assertFalse(safe)
        with self.assertRaises(B3EvidenceUnavailable):
            post_fill_trend_is_nonnegative(bars(),now=NOW,symbol='005930',
                initial_fill_observed_at=START+timedelta(minutes=5),short=5,long=20)

    def test_addition_evidence_is_separate_and_revalidates_volume(self):
        initial={'initial_intent_id':'a'*32,'initial_filled_qty':100,
                 'initial_filled_principal_krw':100000,'initial_fill_last_observed_at':FILL.isoformat()}
        evidence=b3_addition_evidence(run_id='run',source_hash='a'*64,source_kind='broker_observation',
            config=config(),observed_at=NOW,evaluated_at=NOW,window=bars(),trend_window=bars()[1:],budget=500000,initial=initial).as_dict()
        self.assertEqual(evidence['strategy_version'],'paper-b3-add-v1')
        self.assertIsNone(evidence['previous_difference'])
        self.assertEqual(evidence['b3_addition']['volume_prior_sum'],2000)
        evidence['b3_addition']['volume_current']=149
        with self.assertRaises(DecisionEvidenceError):freeze_evidence(evidence)
