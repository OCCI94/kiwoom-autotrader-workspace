"""New paper hypotheses use actual observations and never authorize an order."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime,timedelta
from decimal import Decimal
import unittest
from zoneinfo import ZoneInfo
from kiwoom_autotrader.api.minute_chart import MinuteBar
from tests.paper_experiment_history_fixture import history
from kiwoom_autotrader.trading import paper_experiment_rules as core
from kiwoom_autotrader.trading import paper_experiment_extended_rules as rules

START=datetime(2026,11,2,9,tzinfo=ZoneInfo('Asia/Seoul'));DAY='20261102';SYMBOL='005930'

def daily(symbol=SYMBOL):
    days=[];at=START-timedelta(days=1)
    while len(days)<21:
        if at.weekday()<5:days.append(at.strftime('%Y%m%d'))
        at-=timedelta(days=1)
    return {'symbol':symbol,'mode':'paper','source_sha256':'a'*64,
        'available_at':START.replace(hour=8).isoformat(),'rows':[
            {'date':day,'open':1000,'high':1100,'low':900,'close':1000,'volume':1000,
             'turnover_krw':3_000_000_000} for day in reversed(days)]}

def context(at,symbol=SYMBOL,**market):
    result={'daily':daily(symbol),'market':{'symbol':symbol,'mode':'paper','source_sha256':'b'*64,
        'observed_at':at.isoformat(),'current_price':1000,'day_open':1000,'upper_limit_price':1500,'tick_size':1,
        'day_volume':3000,'prior_limit_up':False,'is_common_stock':True,'managed':False,
        'investment_warning':False,'investment_danger':False,'is_spac':False,'is_etf':False,
        'is_etn':False,'is_preferred':False,'classification_source_sha256':'c'*64,
        'classification_observed_at':at.isoformat(),'vi_active':False,'vi_observed_at':at.isoformat(),
        'vi_source_sha256':'d'*64,'vi_last_trigger_at':None,
        'dart_bad_news':False,'dart_source_sha256':'e'*64,'dart_observed_at':at.isoformat()}}
    result['market'].update(market);result['same_window_history']=history(result['daily'],symbol);return result

def bars(prices,at,*,start=START,symbol=SYMBOL,volumes=None):
    return [MinuteBar(start=start+timedelta(minutes=i),close=p,volume=volumes[i] if volumes else 1000,
        source_symbol=symbol,source_mode='paper',source_response_sha256='f'*64,
        source_query_started_at=at,open_price=p,high_price=p,low_price=p) for i,p in enumerate(prices)]

def candidate(kind='ORB',symbol=SYMBOL,**changes):
    at=START.replace(hour=9,minute=5) if kind=='ORB' else START.replace(hour=8,minute=59) if kind=='EXP_GDR' else START.replace(hour=15,minute=15)
    ctx=context(at,symbol,**changes)
    data=bars([1000,1010,1020,1030,1040],at,symbol=symbol)
    if kind=='EXP_GDR':ctx['market'].setdefault('indicative_price',950)
    elif kind=='EXP_VSS':data=bars([1000],at,start=at-timedelta(minutes=1),symbol=symbol)
    elif kind=='EXP_LDR':data=bars([1000]*60+[975],at,start=at-timedelta(minutes=61),symbol=symbol)
    return {'symbol':symbol,'first_seen_at':START.replace(hour=8,minute=30).isoformat(),'bars':data,'context':ctx},at

def selected(kind='ORB',symbol=SYMBOL,**changes):
    row,at=candidate(kind,symbol,**changes)
    result=rules.select_candidates(kind,[row],day=DAY,selected_at=at)
    if not result['assignments']:raise AssertionError(result)
    return result['assignments'][0],row,at

def position(value,at=None,price=1000):
    return {'arm':value['arm'],'symbol':value['symbol'],'quantity':3,'entry_price':price,
        'entry_basis_krw':price*3,'entry_at':(at or START.replace(hour=15,minute=25)).isoformat()}

def calendar():
    days=[];at=START
    while len(days)<30:
        if at.weekday()<5:days.append(at.strftime('%Y%m%d'))
        at+=timedelta(days=1)
    # Explicit exchange fixture omits Nov 4, testing non-weekday arithmetic.
    days.remove('20261104')
    return {'verified':True,'trading_days':days,'source_sha256':'a'*64,'available_at':START.replace(hour=8).isoformat()}

class ExtendedRulesTests(unittest.TestCase):
    def test_real_mode_never_reads_inputs(self):
        class Bomb:
            def __getattribute__(self,name):raise AssertionError('input touched')
        self.assertIsNone(rules.select_candidates(Bomb(),Bomb(),day=Bomb(),selected_at=Bomb(),mode='real'))
        self.assertIsNone(rules.etf_assignment(Bomb(),Bomb(),Bomb(),Bomb(),mode='real'))
        self.assertIsNone(rules.resolve_collisions(Bomb(),Bomb(),mode='real'))
        self.assertEqual(rules.evaluate_entry(Bomb(),Bomb(),symbol=Bomb(),now=Bomb(),mode='real')['reason'],'paper_only')
        self.assertEqual(rules.evaluate_exit(Bomb(),Bomb(),symbol=Bomb(),now=Bomb(),position=Bomb(),mode='real')['reason'],'paper_only')

    def test_atr14_actual_21_sessions_and_future_or_duplicate_rejected(self):
        value=daily();at=START.replace(hour=9,minute=5)
        result=rules.daily_features(value,symbol=SYMBOL,now=at)
        self.assertEqual(result['atr14'],Decimal(200));self.assertEqual(result['momentum20'],0)
        for mutate in ('short','future','duplicate','source','envelope'):
            bad=deepcopy(value)
            if mutate=='short':bad['rows']=bad['rows'][-20:]
            if mutate=='future':bad['available_at']=(at+timedelta(seconds=1)).isoformat()
            if mutate=='duplicate':bad['rows'][-1]['date']=bad['rows'][-2]['date']
            if mutate=='source':bad['source_sha256']='bad'
            if mutate=='envelope':bad['rows'][-1]['low']=1200
            with self.assertRaises(ValueError):rules.daily_features(bad,symbol=SYMBOL,now=at)

    def test_selection_then_order_freshness_are_distinct(self):
        row,at=candidate();row['context']['market']['observed_at']=(at-timedelta(seconds=60)).isoformat()
        row['context']['market']['vi_observed_at']=(at-timedelta(seconds=60)).isoformat()
        value=rules.select_candidates('ORB',[row],day=DAY,selected_at=at)['assignments'][0]
        later=at+timedelta(minutes=1)
        result=rules.evaluate_entry(value['arm'],bars([1000]*5+[1042],later),symbol=SYMBOL,
            now=later,selection=value,context=row['context'])
        self.assertFalse(result['signal'])
        row['context']['market']['observed_at']=(at-timedelta(seconds=61)).isoformat()
        self.assertFalse(rules.select_candidates('ORB',[row],day=DAY,selected_at=at)['assignments'])

    def test_common_thresholds_and_unknown_evidence_fail_closed(self):
        at=START.replace(hour=9,minute=5)
        for key,val in [('managed',True),('is_etf',True),('investment_warning',True),('vi_active',True),('current_price',1425)]:
            row,_=candidate(**{key:val});self.assertFalse(rules.select_candidates('ORB',[row],day=DAY,selected_at=at)['assignments'],key)
        for seconds,okay in [(119,False),(120,True)]:
            row,_=candidate(vi_last_trigger_at=(at-timedelta(seconds=seconds)).isoformat())
            self.assertEqual(bool(rules.select_candidates('ORB',[row],day=DAY,selected_at=at)['assignments']),okay)
        for key in ('is_common_stock','vi_last_trigger_at','classification_source_sha256'):
            row,_=candidate();row['context']['market'].pop(key)
            self.assertFalse(rules.select_candidates('ORB',[row],day=DAY,selected_at=at)['assignments'])
        row,_=candidate();row['context']['daily']['rows'][-1]['turnover_krw']-=1
        self.assertFalse(rules.select_candidates('ORB',[row],day=DAY,selected_at=at)['assignments'])

    def test_orb_same_window14_actual_no_daily_fallback_or_auction_inference(self):
        value,row,at=selected();feat=value['selection_features']
        self.assertEqual(feat['definition_version'],'orb_same_window14_observed_v2')
        self.assertEqual(Decimal(feat['relative_volume_daily20']),5)
        self.assertEqual(Decimal(feat['relative_volume_same_window14']),2)
        self.assertFalse(feat['opening_range']['auction_trade_isolation_available'])
        self.assertFalse(feat['daily_volume_fallback_used'])
        row['context'].pop('same_window_history')
        missing=rules.select_candidates('ORB',[row],day=DAY,selected_at=at)
        self.assertFalse(missing['assignments'])
        self.assertEqual(missing['rejected'][0]['reason'],'orb_actual_same_window_14_sessions_required')
        row['context']['same_window_history']=history(row['context']['daily'],SYMBOL)
        row['context']['same_window_history']['rows'][0]['volume']+=1
        self.assertFalse(rules.select_candidates('ORB',[row],day=DAY,selected_at=at)['assignments'])

    def test_orb_selection_deterministic_top5_and_no_mutation(self):
        rows=[candidate(symbol=f'{n:06d}')[0] for n in range(8)];at=START.replace(minute=5)
        before=deepcopy(rows);first=rules.select_candidates('ORB',rows,day=DAY,selected_at=at)
        second=rules.select_candidates('ORB',list(reversed(rows)),day=DAY,selected_at=at)
        self.assertEqual(first,second);self.assertEqual(len(first['assignments']),5);self.assertEqual(rows,before)
        self.assertEqual(first,rules.select_candidates('ORB',[],day=DAY,selected_at=at+timedelta(minutes=1),existing=first))
        for value in first['assignments']:core.validate_assignment(value,symbol=value['symbol'],day=DAY,now=at)
        self.assertFalse(rules.select_candidates('ORB',[rows[0]],day=DAY,selected_at=at,held_symbols=[rows[0]['symbol']])['assignments'])

    def test_orb_missing_first_minute_cannot_create_opening_range(self):
        row,at=candidate();row['bars']=row['bars'][1:]
        self.assertFalse(rules.select_candidates('ORB',[row],day=DAY,selected_at=at)['assignments'])

    def test_orb_entry_strict_tick_latest_bar_and_window(self):
        value,row,_=selected();at=START.replace(minute=6)
        for price,okay in [(1041,False),(1042,True)]:
            result=rules.evaluate_entry(value['arm'],bars([1000]*5+[price],at),symbol=SYMBOL,now=at,
                selection=value,context=context(at))
            self.assertEqual(result['signal'],okay,result)
            self.assertFalse(result['permission_to_trade'])
        for hm in [(9,5),(10,31)]:
            at=START.replace(hour=hm[0],minute=hm[1]);data=bars([1042],at,start=at-timedelta(minutes=1))
            self.assertFalse(rules.evaluate_entry(value['arm'],data,symbol=SYMBOL,now=at,selection=value,context=context(at))['signal'])

    def test_orb_tick_at_range_high_across_krx_price_boundaries(self):
        for high,tick in [(2000,5),(5000,10),(20000,50),(50000,100),(200000,500),(500000,1000)]:
            row,at=candidate(current_price=high-1,upper_limit_price=high*2,tick_size=1)
            row['bars']=bars([high-5,high-4,high-3,high-2,high],at)
            value=rules.select_candidates('ORB',[row],day=DAY,selected_at=at)['assignments'][0]
            self.assertEqual(value['selection_features']['tick_size'],tick)
            at+=timedelta(minutes=1)
            for close,okay in [(high+tick,False),(high+2*tick,True)]:
                result=rules.evaluate_entry(value['arm'],bars([close],at,start=at-timedelta(minutes=1)),
                    symbol=SYMBOL,now=at,selection=value,context=context(at,current_price=close,upper_limit_price=high*2))
                self.assertEqual(result['signal'],okay,result)

    def test_vi_block_preserves_only_actual_technical_signal_as_unfillable(self):
        value,_,_=selected();at=START.replace(minute=6)
        for close,technical in [(1041,False),(1042,True)]:
            result=rules.evaluate_entry(value['arm'],bars([close],at,start=at-timedelta(minutes=1)),
                symbol=SYMBOL,now=at,selection=value,context=context(at,vi_active=True))
            self.assertEqual(result['signal'],technical,result)
            self.assertEqual(result['execution_status'],'unfillable_observed' if technical else 'no_signal')
            self.assertFalse(result['checks']['vi_entry_executable'])
            self.assertFalse(result['permission_to_trade'])
        result=rules.evaluate_entry(value['arm'],[],symbol=SYMBOL,now=at,selection=value,context=context(at,vi_active=True))
        self.assertFalse(result['signal']);self.assertEqual(result['reason'],'extended_entry_unverified')

    def test_orb_atr_stops_use_close_not_intrabar_and_closing_auction(self):
        # Pick each seeded variant without forging its arm.
        for arm in ('EXP_ORB_A','EXP_ORB_B'):
            value=next(selected(symbol=f'{n:06d}')[0] for n in range(100) if selected(symbol=f'{n:06d}')[0]['arm']==arm)
            symbol=value['symbol'];at=START.replace(minute=7);pos=position(value,at=START.replace(minute=6),price=1100)
            threshold=1080 if arm=='EXP_ORB_A' else 1050
            data=bars([threshold+1],at,start=at-timedelta(minutes=1),symbol=symbol)
            data=[replace(data[0],low_price=900)]
            self.assertFalse(rules.evaluate_exit(arm,data,symbol=symbol,now=at,position=pos,selection=value)['signal'])
            data=bars([threshold],at,start=at-timedelta(minutes=1),symbol=symbol)
            self.assertEqual(rules.evaluate_exit(arm,data,symbol=symbol,now=at,position=pos,selection=value)['reason'],'orb_atr_close_stop')
            result=rules.evaluate_exit(arm,[],symbol=symbol,now=START.replace(hour=15,minute=20),position=pos,selection=value)
            self.assertEqual(result['reason'],'orb_closing_auction')

    def test_gdr_inclusive_gap_edges_and_score_selection(self):
        for p,okay in [(850,True),(970,True),(849,False),(971,False)]:
            row,at=candidate('EXP_GDR',indicative_price=p)
            self.assertEqual(bool(rules.select_candidates('EXP_GDR',[row],day=DAY,selected_at=at)['assignments']),okay)
        rows=[candidate('EXP_GDR',f'{i:06d}',indicative_price=850+i*10)[0] for i in range(8)]
        result=rules.select_candidates('EXP_GDR',rows,day=DAY,selected_at=at)
        self.assertEqual(result['selected_symbols'],[f'{i:06d}' for i in range(5)])

    def test_gdr_indicative_and_open_fallback_are_explicit(self):
        value,_,at=selected('EXP_GDR')
        result=rules.evaluate_entry('EXP_GDR',[],symbol=SYMBOL,now=at,selection=value,context=context(at))
        self.assertTrue(result['signal']);self.assertTrue(result['auction_intent'])
        row,_=candidate('EXP_GDR');at=START;row['context']=context(at,day_open=950)
        fallback=rules.select_candidates('EXP_GDR',[row],day=DAY,selected_at=at)['assignments'][0]
        result=rules.evaluate_entry('EXP_GDR',[],symbol=SYMBOL,now=at,selection=fallback,context=row['context'])
        self.assertTrue(result['signal']);self.assertFalse(result['auction_intent'])
        self.assertEqual(result['signal_price_basis'],'official_open_fallback_not_auction_fill')

    def test_gdr_1519_proxy_not_future_final_close_and_next_open(self):
        value,_,_=selected('EXP_GDR');at=START.replace(hour=15,minute=20);pos=position(value,at=START,price=950)
        ctx=context(at);ctx['calendar']=calendar()
        for close,okay in [(980,False),(979,True)]:
            data=bars([close],at,start=at-timedelta(minutes=1))
            result=rules.evaluate_exit('EXP_GDR',data,symbol=SYMBOL,now=at,position=pos,selection=value,context=ctx)
            self.assertEqual(result['signal'],okay,result)
        tomorrow=START.replace(day=3,hour=8,minute=59)
        result=rules.evaluate_exit('EXP_GDR',[],symbol=SYMBOL,now=tomorrow,position=pos,selection=value,context={'calendar':calendar()})
        self.assertEqual(result['reason'],'next_session_opening_auction')

    def test_vss_inclusive_3x_and_0_to8pct_prior_limit_flag(self):
        for price,volume,limit,okay in [(1000,3000,False,True),(1080,3000,False,True),(1081,3000,False,False),(999,3000,False,False),(1000,2999,False,False),(1000,3000,True,False)]:
            row,at=candidate('EXP_VSS',day_volume=volume,prior_limit_up=limit)
            row['bars']=bars([price],at,start=at-timedelta(minutes=1))
            self.assertEqual(bool(rules.select_candidates('EXP_VSS',[row],day=DAY,selected_at=at)['assignments']),okay)

    def test_vss_maturity_uses_provided_trading_sessions_not_weekdays(self):
        value,_,_=selected('EXP_VSS');cal=calendar();target=value['selection_features']['hold_sessions'];pos=position(value)
        for offset,okay in [(target-1,False),(target,True)]:
            at=core._day(cal['trading_days'][offset]).replace(hour=15,minute=20)
            result=rules.evaluate_exit('EXP_VSS',[],symbol=SYMBOL,now=at,position=pos,selection=value,context={'calendar':cal})
            self.assertEqual(result['signal'],okay,result);self.assertEqual(result['position_update']['held_trading_sessions'],offset)

    def test_vss_minus8_close_only_sets_next_session_closing_exit(self):
        value,_,_=selected('EXP_VSS');pos=position(value);at=START.replace(hour=15,minute=35);ctx={'calendar':calendar(),
            'day_close':{'date':DAY,'close':920,'available_at':START.replace(hour=15,minute=30).isoformat(),'source_sha256':'1'*64}}
        result=rules.evaluate_exit('EXP_VSS',[],symbol=SYMBOL,now=at,position=pos,selection=value,context=ctx)
        self.assertFalse(result['signal']);pos.update(result['position_update'])
        for hm,okay in [((9,0),False),((15,20),True)]:
            now=START.replace(day=3,hour=hm[0],minute=hm[1])
            result=rules.evaluate_exit('EXP_VSS',[],symbol=SYMBOL,now=now,position=pos,selection=value,context={'calendar':calendar()})
            self.assertEqual(result['signal'],okay,result)
        bad=deepcopy(ctx);bad['day_close']['available_at']=START.replace(hour=15,minute=29).isoformat()
        self.assertEqual(rules.evaluate_exit('EXP_VSS',[],symbol=SYMBOL,now=at,position=position(value),selection=value,context=bad)['reason'],'extended_exit_unverified')

    def test_ldr_exact60min_minus2point5_and_adverse_news(self):
        value,row,at=selected('EXP_LDR');self.assertEqual(Decimal(value['selection_features']['return60']),Decimal('-.025'))
        for mutate in ('missing','bad_news','unknown_news','minus10'):
            bad=deepcopy(row)
            if mutate=='missing':bad['bars']=bad['bars'][1:]
            if mutate=='bad_news':bad['context']['market']['dart_bad_news']=True
            if mutate=='unknown_news':bad['context']['market'].pop('dart_source_sha256')
            if mutate=='minus10':bad['bars'][-1]=replace(bad['bars'][-1],close=900,open_price=900,high_price=900,low_price=900)
            self.assertFalse(rules.select_candidates('EXP_LDR',[bad],day=DAY,selected_at=at)['assignments'])

    def test_ldr_disclosure_bound_matches_15minute_worker_without_changing_quote_age(self):
        for seconds,okay in [(900,True),(1200,True),(1201,False)]:
            row,at=candidate('EXP_LDR')
            row['context']['market']['dart_observed_at']=(at-timedelta(seconds=seconds)).isoformat()
            self.assertEqual(bool(rules.select_candidates('EXP_LDR',[row],day=DAY,selected_at=at)['assignments']),okay)
        row,at=candidate('EXP_LDR')
        row['context']['market']['dart_observed_at']=(at-timedelta(days=1)).isoformat()
        self.assertFalse(rules.select_candidates('EXP_LDR',[row],day=DAY,selected_at=at)['assignments'])

    def test_closing_entry_stable_signal_key_and_no_selected_price_inference(self):
        for kind in ('EXP_VSS','EXP_LDR'):
            value,_,_=selected(kind);at=START.replace(hour=15,minute=20)
            first=rules.evaluate_entry(kind,[],symbol=SYMBOL,now=at,selection=value,context=context(at))
            at+=timedelta(minutes=5)
            second=rules.evaluate_entry(kind,[],symbol=SYMBOL,now=at,selection=value,context=context(at))
            self.assertTrue(first['signal'],first);self.assertEqual(first['signal_key'],second['signal_key'])
            self.assertIsNone(first['signal_price']);self.assertTrue(first['auction_intent'])

    def test_etf_pair_independent_hash_assignment_and_forced_exit(self):
        for symbol in ('069500','229200'):
            value=rules.etf_assignment(DAY,symbol,START,START)
            core.validate_assignment(value,symbol=symbol,day=DAY,now=START)
            self.assertTrue(value['selection_features']['independent_of_stock_allocation'])
            pos=position(value,at=START.replace(hour=10))
            result=rules.evaluate_exit('EXP_ETF_B',[],symbol=symbol,now=START.replace(hour=15,minute=15),position=pos,selection=value)
            self.assertEqual(result['reason'],'intraday_forced_liquidation',result)
        with self.assertRaises(ValueError):rules.etf_assignment(DAY,'122630',START,START)

    def test_etf_entry_is_exact_b_formula_for_latest_actual_bars(self):
        symbol='069500';at=START+timedelta(minutes=36);value=rules.etf_assignment(DAY,symbol,START,START)
        prices=[1000]*30+[990]*5+[1100];data=bars(prices,at,symbol=symbol,volumes=[100]*35+[1000])
        result=rules.evaluate_entry('EXP_ETF_B',data,symbol=symbol,now=at,selection=value)
        parsed=core._bars(data,symbol,at);features=core._features(parsed,10,30)
        expected=core._bar_entry_checks('EXP_B',parsed,at,features,{})
        self.assertTrue(result['signal'],result)
        for key,val in expected.items():self.assertEqual(result['checks'][key],val)
        self.assertFalse(rules.evaluate_entry('EXP_ETF_B',data[:-1],symbol=symbol,now=at,selection=value)['signal'])

    def test_collision_seed_is_reproducible_and_held_never_reassigned(self):
        pool={SYMBOL:['EXP_D','EXP_LDR','EXP_VSS'],'000001':['EXP_C','EXP_ORB_A']}
        result=rules.resolve_collisions(DAY,pool,held_symbols=['000001'])
        self.assertEqual(result,rules.resolve_collisions(DAY,{k:list(reversed(v)) for k,v in pool.items()},held_symbols=['000001']))
        self.assertNotIn('000001',result['selected'])
        self.assertEqual(result,rules.resolve_collisions(DAY,{},existing=result))

    def test_assignment_and_selection_mutation_not_accepted(self):
        value,_,at=selected();bad=deepcopy(value);bad['selection_features']['tick_size']=500
        with self.assertRaises(ValueError):core.validate_assignment(bad,symbol=SYMBOL,day=DAY,now=at)
        bad=deepcopy(value);bad['arm']='EXP_GDR';bad['assignment_sha256']=core._digest({k:v for k,v in bad.items() if k!='assignment_sha256'})
        with self.assertRaises(ValueError):core.validate_assignment(bad,symbol=SYMBOL,day=DAY,now=at)


if __name__=='__main__':unittest.main()
