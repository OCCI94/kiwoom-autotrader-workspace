"""Bounded live selections and accumulated session evidence, entirely offline."""
from copy import deepcopy
from datetime import datetime,timedelta
from pathlib import Path
import tempfile
import unittest
from zoneinfo import ZoneInfo
from kiwoom_autotrader.api.minute_chart import MinuteBar
from tests.paper_experiment_history_fixture import history
from kiwoom_autotrader.trading.paper_experiment_scheduler import ExtendedScheduler,record_c_signal
from kiwoom_autotrader.trading.paper_experiment_store import ExperimentStore
from kiwoom_autotrader.trading import paper_experiment_rules as core
from kiwoom_autotrader.trading import paper_experiment_extended_rules as extended

START=datetime(2026,11,2,9,5,tzinfo=ZoneInfo('Asia/Seoul'))

class Clock:
    def __init__(self,now=START):self.value=now
    def __call__(self):return self.value
    def add(self,seconds):self.value+=timedelta(seconds=seconds)

def daily(symbol,now):
    days=[];at=now-timedelta(days=1)
    while len(days)<21:
        if at.weekday()<5:days.append(at.strftime('%Y%m%d'))
        at-=timedelta(days=1)
    return {'symbol':symbol,'mode':'paper','source_sha256':'a'*64,'available_at':now.replace(hour=8,minute=0,second=0).isoformat(),
        'rows':[{'date':d,'open':1000,'high':1100,'low':900,'close':1000,'volume':1000,'turnover_krw':3_000_000_000}
            for d in reversed(days)]}

class Inputs:
    def __init__(self,clock):self.clock=clock;self.market_calls=[];self.overrides={};self.daily_overrides={};self.delay=0
    def daily(self,symbol):return deepcopy(self.daily_overrides.get(symbol,daily(symbol,self.clock())))
    def market(self,symbol):
        self.market_calls.append(symbol);self.clock.add(self.delay);now=self.clock()
        result={'symbol':symbol,'mode':'paper','source_sha256':'b'*64,'observed_at':now.isoformat(),
            'current_price':1040,'day_open':1000,'upper_limit_price':1500,'tick_size':1,'indicative_price':950,
            'day_volume':3000,'prior_limit_up':False,'is_common_stock':True,'managed':False,'investment_warning':False,
            'investment_danger':False,'is_spac':False,'is_etf':False,'is_etn':False,'is_preferred':False,
            'classification_source_sha256':'c'*64,'classification_observed_at':now.isoformat(),
            'vi_active':False,'vi_source_sha256':'d'*64,'vi_observed_at':now.isoformat(),'vi_last_trigger_at':None,
            'dart_bad_news':False,'dart_source_sha256':'e'*64,'dart_observed_at':now.isoformat()}
        result.update(self.overrides.get(symbol,{}));return result
    def status(self):return {'mock':True}
    def same_window_history(self,symbol,now):return history(self.daily(symbol),symbol)

class Tracker:
    def __init__(self,held=()):self.held=set(held)
    def multi_strategy_position(self,mode,symbol,arm,now):return {'status':'owned' if symbol in self.held else 'flat'}

def official(now):
    return {'start':now.replace(hour=9,minute=0,second=0,microsecond=0),
        'end':now.replace(hour=15,minute=30,second=0,microsecond=0),'calendar_source_sha256':'f'*64}

class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.store=ExperimentStore(Path(self.temp.name).resolve()/'experiment.sqlite3')
        self.clock=Clock();self.inputs=Inputs(self.clock);self.chart_calls=[];self.chart_delay=0
        self.scheduler=ExtendedScheduler(self.inputs,self.store,self.chart,self.clock,calendar_reader=official)
    def chart(self,session,symbol):
        self.chart_calls.append(symbol);self.clock.add(self.chart_delay);now=self.clock()
        start=now.replace(hour=9,minute=0,second=0,microsecond=0)
        return [MinuteBar(start=start+timedelta(minutes=i),open_price=1000+i*10,high_price=1000+i*10,
            low_price=1000+i*10,close=1000+i*10,volume=1000,source_symbol=symbol,source_mode='paper',
            source_response_sha256='1'*64,source_query_started_at=now) for i in range(5)]
    def first_seen(self,symbols):
        now=self.clock()
        for s in symbols:self.store.selection(now.strftime('%Y%m%d'),'first_seen:'+s,
            {'first_seen_at':now.replace(hour=8,minute=30,second=0).isoformat()})

    def save_c_signal(self,symbol):
        at=START.replace(minute=0,second=2);first=at.replace(hour=8,minute=30,second=0)
        assignment=core.assign_candidate('20261102',symbol,first,assigned_at=at,c_preopen=True,profile='native-five-v1')
        opening={'symbol':symbol,'mode':'paper','exchange':'KRX','source_sha256':'2'*64,
            'observed_at':at.isoformat(),'request_started_at':at.isoformat(),'day_open':1000,'previous_close':970,
            'previous_close_available_at':first.isoformat(),'previous_session_day':'20261030'}
        result=core.evaluate_entry('EXP_C',[],symbol=symbol,now=at,assignment=assignment,opening_context=opening)
        self.assertTrue(result['signal']);self.store.assign(assignment)
        return record_c_signal(self.store,assignment,result,opening,at)

    def test_actual_native_high_price_and_bound_assignment_metadata(self):
        self.first_seen(['005930']);self.scheduler.run_selection(None,['005930'],Tracker(),self.clock())
        result=self.store.selection('20261102','extended:ORB')
        self.assertEqual(result['status'],'selected');self.assertEqual(len(result['assignments']),1,result)
        a=result['assignments'][0];core.validate_assignment(a,symbol='005930',day='20261102',now=self.clock())
        self.assertEqual(a['selection_universe_sha256'],core._digest(result['candidates']))
        self.assertEqual(a['selection_available_at'],result['selected_at'])
        self.assertEqual(a['selection_features']['tick_size'],1)

    def test_cutoff_preserves_partial_snapshot_without_polling_all40(self):
        symbols=[f'{n:06d}' for n in range(12)];self.first_seen(symbols)
        self.chart_delay=5;self.inputs.delay=5
        self.scheduler.run_selection(None,symbols,Tracker(),self.clock())
        result=self.store.selection('20261102','extended:ORB')
        self.assertEqual(len(self.chart_calls),4);self.assertEqual(len(self.inputs.market_calls),4)
        self.assertEqual(result['candidate_count'],4);self.assertEqual(result['unqueried_symbols'],symbols[4:])
        self.assertEqual(result['detail_observed_symbols'],symbols[:4]);self.assertEqual(len(result['assignments']),4)
        self.assertEqual(self.clock().second,40);self.assertEqual(result['new_request_cutoff_second'],40)
        old=deepcopy(result);self.clock.add(1)
        self.scheduler.run_selection(None,symbols,Tracker(),self.clock())
        self.assertEqual(old,self.store.selection('20261102','extended:ORB'));self.assertEqual(len(self.chart_calls),4)

    def test_chart_consuming_cutoff_cannot_start_followup_market_request(self):
        self.first_seen(['005930','000001']);self.chart_delay=41
        self.scheduler.run_selection(None,['005930','000001'],Tracker(),self.clock())
        result=self.store.selection('20261102','extended:ORB')
        self.assertEqual(self.inputs.market_calls,[]);self.assertEqual(result['unqueried_symbols'],['005930','000001'])
        self.assertEqual(result['assignments'],[])

    def test_response_after_window_is_preserved_as_unavailable_not_backdated(self):
        self.first_seen(['005930']);self.chart_delay=20;self.inputs.delay=45
        self.scheduler.run_selection(None,['005930'],Tracker(),self.clock())
        result=self.store.selection('20261102','extended:ORB')
        self.assertEqual(result['status'],'selection_window_expired');self.assertEqual(result['assignments'],[])
        self.assertTrue(result['selected_at'].startswith('2026-11-02T09:06'))
        self.assertEqual(self.scheduler.failure['005930'],'selection_response_after_window')

    def test_original_pool_at_most40_no_hidden_wide_candidate_fallback(self):
        with self.assertRaises(ValueError):self.scheduler.run_selection(None,[f'{i:06d}' for i in range(41)],Tracker(),self.clock())
        with self.assertRaises(ValueError):self.scheduler.run_selection(None,['005930','005930'],Tracker(),self.clock())
        self.assertEqual(self.chart_calls,[]);self.assertEqual(self.inputs.market_calls,[])

    def test_held_and_etfs_excluded_from_new_stock_selection(self):
        symbols=['005930','000001','069500','229200'];self.first_seen(symbols)
        self.scheduler.run_selection(None,symbols,Tracker(['005930']),self.clock())
        result=self.store.selection('20261102','extended:ORB')
        self.assertEqual([r['symbol'] for r in result['candidates']],['000001'])
        self.assertEqual(self.chart_calls,['000001'])

    def test_d_keeps_original_flat_pool_even_when_all_new_arm_sources_fail(self):
        self.clock.value=START.replace(hour=15,minute=15);symbols=[f'{n:06d}' for n in range(12)]
        self.first_seen(symbols)
        def fail(*args):raise ValueError('mock_unavailable')
        self.scheduler.chart=fail
        self.scheduler.run_selection(None,symbols,Tracker(),self.clock())
        result=self.store.selection('20261102','extended:closing')
        self.assertEqual(result['candidate_count'],0);self.assertEqual(len(result['candidates']),12)
        self.assertEqual(len(result['assignments']),10)
        self.assertEqual({a['arm'] for a in result['assignments']},{'EXP_D'})
        for a in result['assignments']:core.validate_assignment(a,symbol=a['symbol'],day='20261102',now=self.clock())

    def test_stale_classification_cannot_admit_orb(self):
        self.first_seen(['005930']);self.inputs.overrides['005930']={'classification_observed_at':(START-timedelta(seconds=301)).isoformat()}
        self.scheduler.run_selection(None,['005930'],Tracker(),self.clock())
        self.assertEqual(self.store.selection('20261102','extended:ORB')['assignments'],[])

    def test_missing_official_calendar_is_not_inferred_from_etfs(self):
        self.scheduler.calendar_reader=lambda now:None
        result=self.scheduler.context('005930',self.clock(),market=False)
        self.assertNotIn('calendar',result);self.assertEqual(self.scheduler.failure['calendar'],'calendar_history_unverified')
        self.assertIsNone(self.store.selection('20261102','market_calendar'))

    def test_calendar_accumulation_retains_entry_beyond_21_sessions(self):
        first=self.scheduler.context('005930',self.clock(),market=False)['calendar']
        self.assertIn('20261102',first['trading_days'])
        self.clock.value=START.replace(day=16)
        self.assertIn('calendar',self.scheduler.context('005930',self.clock(),market=False))
        self.clock.value=START.replace(month=12,day=14)
        second=self.scheduler.context('005930',self.clock(),market=False)['calendar']
        self.assertIn('20261102',second['trading_days']);self.assertIn('20261214',second['trading_days'])
        self.assertNotIn('20261102',[r['date'] for r in self.inputs.daily('069500')['rows']])
        self.assertEqual(len(self.store.market_calendar_history('20261214')),3)
        restarted=ExtendedScheduler(self.inputs,self.store,self.chart,self.clock,calendar_reader=official)
        self.assertEqual(second,restarted.context('005930',self.clock(),market=False)['calendar'])

    def test_calendar_does_not_fill_disjoint_unobserved_intervals(self):
        self.assertIn('calendar',self.scheduler.context('005930',self.clock(),market=False))
        self.clock.value=START.replace(month=12,day=14)
        self.assertNotIn('calendar',self.scheduler.context('005930',self.clock(),market=False))
        self.assertEqual(self.scheduler.failure['calendar'],'calendar_history_unverified')

    def test_calendar_disagreement_future_and_tampered_history_rejected(self):
        bad=self.inputs.daily('229200');bad['rows'][-1]['date']='20261030'
        bad['rows']=bad['rows'][:-1]  # fewer than 21 is not quietly accepted.
        self.inputs.daily_overrides['229200']=bad
        self.assertNotIn('calendar',self.scheduler.context('005930',self.clock(),market=False))
        self.inputs.daily_overrides.clear()
        valid=self.scheduler.context('005930',self.clock(),market=False)['calendar']
        self.assertTrue(valid['verified'])
        # Corrupt historical immutable payload; hash mismatch must block reuse.
        with self.store._db() as db:db.execute("UPDATE selections SET payload='{}' WHERE kind='market_calendar'")
        self.scheduler.calendar_cache=None
        self.assertNotIn('calendar',self.scheduler.context('005930',self.clock(),market=False))

    def test_selected_gdr_suppresses_0900_fallback_reselection(self):
        self.clock.value=START.replace(hour=8,minute=59);self.first_seen(['005930'])
        self.scheduler.run_selection(None,['005930'],Tracker(),self.clock())
        first=self.store.selection('20261102','extended:GDR');self.assertEqual(len(first['assignments']),1,first)
        self.clock.value=START.replace(hour=9,minute=0)
        self.scheduler.run_selection(None,['005930'],Tracker(),self.clock())
        self.assertIsNone(self.store.selection('20261102','extended:GDR_fallback'))
        self.assertEqual(self.scheduler.overrides('20261102')['005930']['arm'],'EXP_GDR')

    def test_actual_unsubmitted_c_signal_enters_orb_seed_collision_without_replay(self):
        found={}
        for n in range(100):
            symbol=f'{n:06d}';variant='EXP_ORB_A' if int(core._digest(['orb-variant-v1','20261102',symbol]),16)%2==0 else 'EXP_ORB_B'
            winner=extended.resolve_collisions('20261102',{symbol:['EXP_C',variant]})['selected'][symbol]
            found.setdefault('C' if winner=='EXP_C' else 'ORB',symbol)
            if len(found)==2:break
        for side,symbol in found.items():
            # Independent immutable day selection per scenario.
            self.store=ExperimentStore(Path(self.temp.name).resolve()/(side+'.sqlite3'))
            self.scheduler=ExtendedScheduler(self.inputs,self.store,self.chart,self.clock,calendar_reader=official)
            self.first_seen([symbol]);marker=self.save_c_signal(symbol)
            self.scheduler.run_selection(None,[symbol],Tracker(),self.clock())
            result=self.store.selection('20261102','extended:ORB');chosen=result['assignments'][0]
            self.assertEqual(chosen['arm']=='EXP_C',side=='C')
            self.assertEqual(len(result['collision']['priority'][symbol]),2)
            self.assertEqual(result['prior_c_signal_evidence'][0]['source_sha256'],marker['source_sha256'])
            core.validate_assignment(chosen,symbol=symbol,day='20261102',now=self.clock())
            if side=='C':
                self.assertEqual(chosen,marker['assignment'])
                opening=deepcopy(marker['opening_context']);opening.update(observed_at=self.clock().isoformat(),request_started_at=self.clock().isoformat())
                replay=core.evaluate_entry('EXP_C',[],symbol=symbol,now=self.clock(),assignment=chosen,opening_context=opening)
                self.assertFalse(replay['signal'])

    def test_c_assignment_without_actual_signal_does_not_claim_orb_priority(self):
        symbol='005930';self.first_seen([symbol])
        self.store.assign(core.assign_candidate('20261102',symbol,START.replace(hour=8,minute=30),
            assigned_at=START.replace(minute=0),c_preopen=True,profile='native-five-v1'))
        self.scheduler.run_selection(None,[symbol],Tracker(),self.clock())
        result=self.store.selection('20261102','extended:ORB')
        self.assertTrue(result['assignments'][0]['arm'].startswith('EXP_ORB_'))
        self.assertEqual(result['prior_c_signal_evidence'],[])

    def test_corrupt_c_signal_or_owned_c_cannot_be_stolen_by_orb(self):
        symbol='005930';self.first_seen([symbol]);self.save_c_signal(symbol)
        with self.store._db() as db:db.execute("UPDATE selections SET payload='{}' WHERE kind=?",('eligible:C:'+symbol,))
        self.scheduler.run_selection(None,[symbol],Tracker(),self.clock())
        self.assertEqual(self.store.selection('20261102','extended:ORB')['assignments'],[])
        self.assertEqual(self.scheduler.failure[symbol],'c_signal_evidence_invalid')
        self.store=ExperimentStore(Path(self.temp.name).resolve()/'owned.sqlite3')
        self.scheduler=ExtendedScheduler(self.inputs,self.store,self.chart,self.clock,calendar_reader=official)
        self.first_seen([symbol]);self.save_c_signal(symbol)
        self.scheduler.run_selection(None,[symbol],Tracker([symbol]),self.clock())
        self.assertEqual(self.store.selection('20261102','extended:ORB')['assignments'],[])

    def test_c_marker_is_immutable_and_rejects_fabricated_technical_signal(self):
        symbol='005930';marker=self.save_c_signal(symbol)
        repeated=record_c_signal(self.store,marker['assignment'],marker['result'],marker['opening_context'],START.replace(minute=0,second=3))
        self.assertEqual(marker,repeated)
        changed=deepcopy(marker['result']);changed['features']['gap_bps']='500'
        with self.assertRaises(ValueError):record_c_signal(self.store,marker['assignment'],changed,marker['opening_context'],START)


if __name__=='__main__':unittest.main()
