import copy
from datetime import datetime,timedelta
from zoneinfo import ZoneInfo
from decimal import Decimal
import unittest
from kiwoom_autotrader.api.minute_chart import MinuteBar
from kiwoom_autotrader.trading.multi_strategy import EntryState
from kiwoom_autotrader.trading.multi_strategy_evidence import multi_strategy_evidence
from kiwoom_autotrader.trading.steady_uptrend_vwap import evaluate_steady_uptrend_vwap
from kiwoom_autotrader.trading.decision_evidence import freeze_evidence,DecisionEvidenceError


def fixture(minutes=60):
    now=datetime(2026,11,2,9,tzinfo=ZoneInfo('Asia/Seoul'))+timedelta(minutes=minutes,seconds=3)
    opening=now.replace(hour=9,minute=0,second=0)
    bars=[]
    for i in range(minutes):
        base=10000 if i<minutes-35 else 10000+(i-(minutes-35))*5
        bars.append(MinuteBar(opening+timedelta(minutes=i),base+5,volume=100+max(0,i-(minutes-35))*5,acc_volume=10000+i*1000,
            source_symbol='003220',source_mode='paper',source_batch='test-source-page',source_page=0,
            source_row=minutes-i-1,source_response_sha256='a'*64,source_query_started_at=now-timedelta(seconds=2),
            open_price=base,high_price=base+6,low_price=base-1))
    rows=[];day=now.date()-timedelta(days=1)
    while len(rows)<20:
        if day.weekday()<5: rows.append(day)
        day-=timedelta(days=1)
    daily={'schema':'s5-prior-daily-v1','symbol':'003220','mode':'paper','session_day':'20261102',
        'source_api':'ka10081','source_sha256':'b'*64,'request_started_at':(now-timedelta(seconds=3)).isoformat(),
        'available_at':(now-timedelta(seconds=2)).isoformat(),
        'rows':[{'date':day.strftime('%Y%m%d'),'open':9500+i*20,'high':9900+i*20,'low':9300+i*20,
                 'close':9600+i*20,'volume':10000} for i,day in enumerate(reversed(rows))]}
    risk={'schema':'s5-dart-risk-v1','symbol':'003220','reviewed_at':(now-timedelta(hours=1)).isoformat(),
        'expires_at':(now+timedelta(hours=1)).isoformat(),'coverage_complete':True,'unresolved_major_risk':False,
        'source_sha256':'c'*64,'file_sha256':'d'*64}
    from tests.s5_short_fixture import short_evidence
    risk['short_selling_evidence']=short_evidence('003220',daily,now)
    market={'symbol':'003220','request_started_at':(now-timedelta(seconds=1)).isoformat(),
        'available_at':now.isoformat(),'observed_at':now.isoformat(),'source':'ka10001','source_sha256':'e'*64,
        'source_market_timestamp':None,'source_freshness_verified':False,'current_price':bars[-1].close,
        'day_open':10000,'day_high':bars[-1].high_price,'change_pct':'1.75','upper_limit_price':13000,
        's5_daily_context':daily,'s5_risk_context':risk}
    market['steady_uptrend_vwap_evidence']=evaluate_steady_uptrend_vwap(bars,symbol='003220',now=now,
        current_price=market['current_price'],daily_context=daily,risk_context=risk)
    return now,bars,market


def evidence(minutes=60, *, baseline='fixture'):
    now,bars,market=fixture(minutes)
    assert market['steady_uptrend_vwap_evidence']['eligible'],market['steady_uptrend_vwap_evidence']
    if baseline == 'fixture':
        baseline=EntryState(bars[-2].start,10000,False,Decimal('1'))
    return multi_strategy_evidence(strategy='S5_steady_uptrend_vwap',symbol='003220',side='buy',
        reason='steady_uptrend_vwap',budget=500000,position={'generation_id':None,'owned_qty':None,'basis_krw':None},
        bars=bars,market=market,observed_at=now,baseline=baseline,
        source_hash='f'*64,b3_config={'multi_b3_stage':'initial','multi_b3_initial_intent_id':None,
        'multi_b3_initial_entry_pct':50,'multi_b3_add_on_profit_bps':50,'multi_b3_total_budget_krw':1000000,
        'max_daily_entries_per_symbol':2})


class S5SourceBoundEvidenceTests(unittest.TestCase):
    def test_sparse_actual_7_20_34_minutes_bind_full_prefix_and_roundtrip(self):
        now,complete,market=fixture(45)
        required=set(range(10,45,5))
        for count in (7,20,34):
            with self.subTest(actual_minutes=count):
                chosen=set(required)
                for i in reversed(range(45)):
                    if len(chosen)==count: break
                    chosen.add(i)
                bars=[bar for i,bar in enumerate(complete) if i in chosen]
                observed=copy.deepcopy(market)
                observed['steady_uptrend_vwap_evidence']=evaluate_steady_uptrend_vwap(
                    bars,symbol='003220',now=now,current_price=observed['current_price'],
                    daily_context=observed['s5_daily_context'],risk_context=observed['s5_risk_context'])
                self.assertTrue(observed['steady_uptrend_vwap_evidence']['eligible'])
                ev=multi_strategy_evidence(strategy='S5_steady_uptrend_vwap',symbol='003220',side='buy',
                    reason='steady_uptrend_vwap',budget=500000,
                    position={'generation_id':None,'owned_qty':None,'basis_krw':None},
                    bars=bars,market=observed,observed_at=now,baseline=None,
                    source_hash='f'*64,b3_config={'multi_b3_stage':'initial','multi_b3_initial_intent_id':None,
                    'multi_b3_initial_entry_pct':50,'multi_b3_add_on_profit_bps':50,
                    'multi_b3_total_budget_krw':1000000,'max_daily_entries_per_symbol':2})
                frozen=freeze_evidence(ev.as_dict()).as_dict()
                self.assertEqual(frozen,ev.as_dict())
                proof=frozen['multi_strategy_signal']['s5_input']
                self.assertEqual(len(proof['minute_prefix']['rows']),count)
                self.assertEqual(len(frozen['bars']),min(count,21))
                self.assertEqual(proof['evaluation']['session_coverage']['missing_count'],45-count)
                self.assertFalse(proof['evaluation']['vwap']['exact_full_session_vwap'])
                self.assertEqual(proof['evaluation']['vwap']['volume'],sum(b.volume for b in bars))

    def test_first_visit_and_stale_baselines_do_not_block_s5_prefix_proof(self):
        old=EntryState(datetime(2026,11,2,9,35,tzinfo=ZoneInfo('Asia/Seoul')),10000,False,Decimal('1'))
        for baseline in (None,EntryState(),old):
            with self.subTest(baseline=baseline):
                ev=evidence(60,baseline=baseline)
                self.assertEqual(freeze_evidence(ev.as_dict()).sha256,ev.sha256)

    def test_delayed_latest_boundary_is_bound_to_complete_source_prefix(self):
        for minutes in (61,62,63,64):
            with self.subTest(minutes=minutes):
                ev=evidence(minutes,baseline=None).as_dict()
                self.assertEqual(ev['multi_strategy_signal']['s5_input']['evaluation']['signal_at'],
                                 '2026-11-02T10:00:00+09:00')
                self.assertEqual(freeze_evidence(ev).as_dict(),ev)

    def test_replayable_all_day_prefix_is_bounded(self):
        ev=evidence(375)
        self.assertLess(len(ev.json.encode()),65536)
        self.assertEqual(len(ev.as_dict()['multi_strategy_signal']['s5_input']['minute_prefix']['rows']),375)
        self.assertEqual(freeze_evidence(ev.as_dict()).sha256,ev.sha256)

    def test_caller_pass_flag_cannot_authorize_changed_prices(self):
        ev=evidence().as_dict();ev['multi_strategy_signal']['s5_input']['minute_prefix']['rows'][0][4]=20000
        with self.assertRaises(DecisionEvidenceError):freeze_evidence(ev)

    def test_recomputed_evaluation_required(self):
        ev=evidence().as_dict();ev['multi_strategy_signal']['s5_input']['evaluation']['vwap']['volume']+=1
        with self.assertRaises(DecisionEvidenceError):freeze_evidence(ev)

    def test_missing_session_minute_not_interpolated(self):
        ev=evidence().as_dict();del ev['multi_strategy_signal']['s5_input']['minute_prefix']['rows'][0]
        with self.assertRaises(DecisionEvidenceError):freeze_evidence(ev)

    def test_risk_expiry_and_overheated_daily_reject(self):
        for variant in ('risk','daily'):
            ev=evidence().as_dict();p=ev['multi_strategy_signal']['s5_input']
            if variant=='risk':p['risk_context']['expires_at']=p['risk_context']['reviewed_at']
            else:p['daily_context']['rows'][-1]['date']='20261102'
            with self.assertRaises(DecisionEvidenceError):freeze_evidence(ev)

    def test_risk_expiry_between_observation_and_evaluation_blocks(self):
        ev=evidence().as_dict()
        observed=datetime.fromisoformat(ev['observed_at'])
        ev['evaluated_at']=(observed+timedelta(seconds=1)).isoformat()
        ev['multi_strategy_signal']['s5_input']['risk_context']['expires_at']=ev['evaluated_at']
        with self.assertRaises(DecisionEvidenceError):freeze_evidence(ev)

    def test_legacy_version_cannot_claim_s5(self):
        ev=evidence().as_dict();ev['strategy_version']='paper-multi-observed-v3'
        with self.assertRaises(DecisionEvidenceError):freeze_evidence(ev)

    def test_buy_requires_b3_split_contract(self):
        ev=evidence().as_dict();ev['config']={'enabled':True};ev['config_sha256']=None
        with self.assertRaises(DecisionEvidenceError):freeze_evidence(ev)

    def test_actual_fill_clock_is_required_for_time_stop(self):
        now,bars,market=fixture()
        pos={'generation_id':'a'*32,'owned_qty':5,'basis_krw':50000,'half_take_done':False,
             'first_fill_at':(now-timedelta(minutes=30)).isoformat(),
             'first_fill_observed_at':(now-timedelta(minutes=29)).isoformat(),'first_fill_timing_verified':True}
        ev=multi_strategy_evidence(strategy='S5_steady_uptrend_vwap',symbol='003220',side='sell',
            reason='time_stop_30m',budget=None,position=pos,bars=bars[-21:],market=market,observed_at=now,
            quantity=5,exit_kind='full',source_hash='f'*64)
        quote_only=multi_strategy_evidence(strategy='S5_steady_uptrend_vwap',symbol='003220',side='sell',
            reason='time_stop_30m',budget=None,position=pos,bars=[],market=market,observed_at=now,
            quantity=5,exit_kind='full',source_hash='f'*64)
        self.assertEqual(quote_only.as_dict()['bars'],[])
        bad=ev.as_dict();bad['multi_strategy_signal']['position']['first_fill_at']=now.isoformat()
        with self.assertRaises(DecisionEvidenceError):freeze_evidence(bad)
