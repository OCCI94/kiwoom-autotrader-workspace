"""Pure arm math, chronology and frozen allocation; no account or transport."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime,timedelta
from decimal import Decimal
from types import SimpleNamespace
import unittest
from zoneinfo import ZoneInfo

from kiwoom_autotrader.api.minute_chart import MinuteBar
from kiwoom_autotrader.trading import paper_experiment_rules as r

DAY='20261102';START=datetime(2026,11,2,9,tzinfo=ZoneInfo('Asia/Seoul'))


def assigned(arm,first=None):
    for n in range(10000):
        value=r.assign_candidate(DAY,f'{n:06d}',first or START)
        if value['arm']==arm:return value
    raise AssertionError('assignment fixture unavailable')


def bars(prices,symbol,*,start=START,volumes=None,opens=None):
    now=start+timedelta(minutes=len(prices))
    return [MinuteBar(start=start+timedelta(minutes=i),close=price,
        volume=volumes[i] if volumes else 100,source_symbol=symbol,source_mode='paper',
        source_response_sha256='a'*64,source_query_started_at=now,
        open_price=opens[i] if opens else price,high_price=max(price,opens[i] if opens else price),
        low_price=min(price,opens[i] if opens else price)) for i,price in enumerate(prices)]


class RuleTests(unittest.TestCase):
    def test_d_signal_is_observed_in_auction_not_at_prior_selection(self):
        symbol='005930';snapshot=START.replace(hour=15,minute=18)
        value=r.select_overnight([{'symbol':symbol,'first_seen_at':START.isoformat()}],day=DAY,snapshot_at=snapshot)['assignments'][0]
        at=START.replace(hour=15,minute=21,second=3)
        first=r.evaluate_entry('EXP_D',[],symbol=symbol,now=at,assignment=value)
        second=r.evaluate_entry('EXP_D',[],symbol=symbol,now=at+timedelta(minutes=2),assignment=value)
        self.assertTrue(first['signal']);self.assertEqual(first['signal_at'],at.isoformat())
        self.assertEqual(first['selection_at'],snapshot.isoformat())
        self.assertEqual(first['dedup_at'],START.replace(hour=15,minute=20).isoformat())
        self.assertEqual(first['signal_key'],second['signal_key'])
        self.assertIsNone(first.get('signal_price'))

    def test_btp_uses_exact_total_basis_not_floored_average(self):
        value=assigned('EXP_B_TP');symbol=value['symbol'];now=START+timedelta(minutes=31)
        position={'arm':'EXP_B_TP','symbol':symbol,'quantity':3,'entry_price':1000,
            'entry_basis_krw':3001,'entry_at':(START+timedelta(minutes=30)).isoformat()}
        data=bars([1000]*30+[1010],symbol)
        result=r.evaluate_exit('EXP_B_TP',data,symbol=symbol,now=now,position=position)
        self.assertFalse(result['signal'],result)
        data=bars([1000]*30+[1011],symbol)
        result=r.evaluate_exit('EXP_B_TP',data,symbol=symbol,now=now,position=position)
        self.assertEqual(result['reason'],'take_profit_100bp')
        for invalid in (0,-1,'3001',True):
            result=r.evaluate_exit('EXP_B_TP',data,symbol=symbol,now=now,position={**position,'entry_basis_krw':invalid})
            self.assertFalse(result['signal'])

    def test_native_five_profile_is_fixed_equal_weight_and_source_validated(self):
        expected={'EXP_R':20,'EXP_A':20,'EXP_B':20,'EXP_B_TP':20,'EXP_E':20}
        for i in range(100):
            value=r.assign_candidate(DAY,f'{i:06d}',START,profile='native-five-v1')
            self.assertIn(value['arm'],expected)
            self.assertEqual(value['weights'],expected)
            r.validate_assignment(value,symbol=value['symbol'],day=DAY,now=START)
        altered=deepcopy(value);altered['profile']='unknown-profile'
        altered['assignment_sha256']=r._digest({k:v for k,v in altered.items() if k!='assignment_sha256'})
        with self.assertRaises(ValueError):r.validate_assignment(altered,symbol=value['symbol'],day=DAY,now=START)
        altered=deepcopy(value);altered['arm']='S5_steady_uptrend_vwap'
        altered['assignment_sha256']=r._digest({k:v for k,v in altered.items() if k!='assignment_sha256'})
        with self.assertRaises(ValueError):r.validate_assignment(altered,symbol=value['symbol'],day=DAY,now=START)
        with self.assertRaises(ValueError):r.assign_candidate(DAY,'005930',START,profile={'EXP_A':100})

    def test_native_five_cohort_balances_all_five_without_reassigning_old_records(self):
        rows=[{'symbol':f'{i:06d}','first_seen_at':START.isoformat()} for i in range(25)]
        result=r.assign_cohort(rows,day=DAY,snapshot_at=START,profile='native-five-v1')
        self.assertEqual(result['actual_counts'],{name:5 for name in dict(r.NATIVE_FIVE_WEIGHTS)})
        for value in result['assignments']:
            r.validate_assignment(value,symbol=value['symbol'],day=DAY,now=START)
        self.assertEqual(result,r.assign_cohort([],day=DAY,snapshot_at=START+timedelta(minutes=1),existing=result))
        prior=assigned('S5_steady_uptrend_vwap')
        self.assertEqual(prior,r.assign_candidate(DAY,prior['symbol'],START,profile='native-five-v1',existing=prior))
        for size in range(2,41):
            cohort=r.assign_cohort([{'symbol':f'{i:06d}','first_seen_at':START.isoformat()} for i in range(size)],day=DAY,snapshot_at=START,profile='native-five-v1')
            counts=list(cohort['actual_counts'].values())
            self.assertLessEqual(max(counts)-min(counts),1)
            self.assertGreaterEqual(cohort['actual_counts']['EXP_R']*100,15*size)
            self.assertGreaterEqual(cohort['actual_counts']['EXP_A']*100,15*size)

    def test_native_five_preserves_separate_preopen_c_selection(self):
        value=r.assign_candidate(DAY,'005930',START-timedelta(minutes=5),assigned_at=START,
            c_preopen=True,profile='native-five-v1')
        self.assertEqual(value['arm'],'EXP_C')
        self.assertEqual(value['profile'],'native-five-v1')
        r.validate_assignment(value,symbol='005930',day=DAY,now=START)

    def entry(self,arm,prices,**kwargs):
        assignment=assigned(arm);data=bars(prices,assignment['symbol'],**kwargs)
        result=r.evaluate_entry(arm,data,symbol=assignment['symbol'],now=data[-1].start+timedelta(minutes=1),assignment=assignment)
        return result,data,assignment

    def test_assignment_repeatable_immutable_and_weights_explicit(self):
        value=r.assign_candidate(DAY,'005930',START)
        self.assertEqual(value,r.assign_candidate(DAY,'005930',START))
        self.assertEqual(value,r.assign_candidate(DAY,'005930',START+timedelta(minutes=5),existing=value))
        self.assertEqual(sum(value['weights'].values()),100)
        self.assertEqual(value['weights']['EXP_R'],15);self.assertEqual(value['weights']['EXP_A'],15)
        self.assertEqual(len(value['weights']),9)
        changed=deepcopy(value);changed['first_seen_at']=(START-timedelta(minutes=1)).isoformat()
        with self.assertRaises(ValueError):r.assign_candidate(DAY,'005930',START,existing=changed)
        self.assertIsNone(r.assign_candidate(DAY,'005930',START,d_locked=True))

    def test_c_assignment_retains_first_seen_and_actual_later_assignment_time(self):
        first=START-timedelta(minutes=10);assigned_at=START+timedelta(seconds=5)
        value=r.assign_candidate(DAY,'005930',first,assigned_at=assigned_at,c_preopen=True)
        self.assertEqual(value['first_seen_at'],first.isoformat())
        self.assertEqual(value['assigned_at'],assigned_at.isoformat())
        r.validate_assignment(value,symbol='005930',day=DAY,now=assigned_at)

    def test_frozen_cohort_has_actual_control_minimum_and_no_reassignment(self):
        rows=[{'symbol':f'{i:06d}','first_seen_at':START.isoformat()} for i in range(25)]
        result=r.assign_cohort(rows,day=DAY,snapshot_at=START)
        self.assertEqual(result['actual_counts']['EXP_A'],4)
        self.assertEqual(result['actual_counts']['EXP_R'],4)
        self.assertEqual(sum(result['actual_counts'].values()),25)
        self.assertEqual(result,r.assign_cohort(rows[::-1],day=DAY,snapshot_at=START))
        self.assertEqual(result,r.assign_cohort([],day=DAY,snapshot_at=START+timedelta(minutes=1),existing=result))
        for assignment in result['assignments']:
            r.validate_assignment(assignment,symbol=assignment['symbol'],day=DAY,now=START)
        forged=deepcopy(result['assignments'][0]);forged['arm']='EXP_E'
        forged['assignment_sha256']=r._digest({k:v for k,v in forged.items() if k!='assignment_sha256'})
        with self.assertRaises(ValueError):r.validate_assignment(forged,symbol=forged['symbol'],day=DAY,now=START)
        with self.assertRaises(ValueError):r.assign_cohort(rows[:1],day=DAY,snapshot_at=START)

    def test_control_minimum_all_supported_cohort_sizes(self):
        for size in range(2,41):
            result=r.assign_cohort([{'symbol':f'{i:06d}','first_seen_at':START.isoformat()} for i in range(size)],day=DAY,snapshot_at=START)
            self.assertGreaterEqual(result['actual_counts']['EXP_R']*100,15*size)
            self.assertGreaterEqual(result['actual_counts']['EXP_A']*100,15*size)

    def test_real_mode_does_not_inspect_experiment_data(self):
        class Poison:
            def __getattribute__(self,key):raise AssertionError('real mode touched experimental input')
        self.assertIsNone(r.assign_candidate(Poison(),Poison(),Poison(),mode='real'))
        self.assertIsNone(r.select_overnight(Poison(),day=Poison(),snapshot_at=Poison(),mode='real'))
        self.assertFalse(r.evaluate_entry(Poison(),Poison(),symbol=Poison(),now=Poison(),assignment=Poison(),mode='real')['signal'])
        self.assertFalse(r.evaluate_exit(Poison(),Poison(),symbol=Poison(),now=Poison(),position=Poison(),mode='real')['signal'])

    def test_a_cross_math_and_exact_strength(self):
        result,_,_=self.entry('EXP_A',[1000]*20+[1100])
        self.assertTrue(result['signal']);self.assertEqual(Decimal(result['features']['previous_difference']),0)
        expected=(Decimal(1020)-Decimal(20100)/20)/(Decimal(20100)/20)*10000
        self.assertEqual(Decimal(result['features']['cross_bps']),expected)
        weak,_,_=self.entry('EXP_A',[1000]*20+[1001]);self.assertFalse(weak['signal'])

    def test_b_and_btp_same_entry_rvol_excludes_signal_and_slope_pit(self):
        prices=[800]*5+[1000]*20+[990]*10+[1300]
        for arm in ('EXP_B','EXP_B_TP'):
            result,_,_=self.entry(arm,prices,volumes=[100]*35+[200])
            self.assertTrue(result['signal'],result);self.assertEqual(result['features']['volume_multiple'],'2')
            self.assertGreater(Decimal(result['features']['sma_slope']),0)
        low,_,_=self.entry('EXP_B',prices,volumes=[100]*35+[199]);self.assertFalse(low['signal'])

    def test_b_no_entry_at_or_after_1500(self):
        assignment=assigned('EXP_B')
        data=bars([800]*5+[1000]*20+[990]*10+[1300],assignment['symbol'],
            start=START.replace(hour=14,minute=24),volumes=[100]*35+[200])
        result=r.evaluate_entry('EXP_B',data,symbol=assignment['symbol'],now=START.replace(hour=15),assignment=assignment)
        self.assertFalse(result['signal']);self.assertFalse(result['checks']['entry_before_1500'])

    def test_e_exact_negative_bar_and_volume_threshold(self):
        result,_,_=self.entry('EXP_E',[1000]*20+[985],volumes=[100]*20+[300],opens=[1000]*21)
        self.assertTrue(result['signal'],result)
        for close,volume in ((986,300),(985,299)):
            result,_,_=self.entry('EXP_E',[1000]*20+[close],volumes=[100]*20+[volume],opens=[1000]*21)
            self.assertFalse(result['signal'])

    def test_no_future_stale_duplicate_synthetic_or_bad_hash_and_no_mutation(self):
        result,data,assignment=self.entry('EXP_A',[1000]*20+[1100]);before=deepcopy(data);now=START+timedelta(minutes=21)
        variants=[data+[replace(data[-1],start=now)],data[:-1],data+[data[-1]],
            data[:-1]+[replace(data[-1],source_response_sha256='bad')],
            data[:-1]+[replace(data[-1],gap_proof=object())],
            data[:-1]+[replace(data[-1],source_mode='real')],
            data[:-1]+[replace(data[-1],source_query_started_at=now+timedelta(seconds=1))]]
        for value in variants:
            self.assertFalse(r.evaluate_entry('EXP_A',value,symbol=assignment['symbol'],now=now,assignment=assignment)['signal'])
        self.assertEqual(data,before)

    def test_observed_rows_basis_preserves_missing_not_synthetic(self):
        assignment=assigned('EXP_A');data=bars([1000]*21+[1100],assignment['symbol'])
        del data[1]
        result=r.evaluate_entry('EXP_A',data,symbol=assignment['symbol'],now=START+timedelta(minutes=22),assignment=assignment)
        self.assertTrue(result['signal']);self.assertEqual(result['features']['missing_minutes'],1)
        self.assertFalse(result['features']['vwap_exact_full_session'])

    def test_random_schedule_has_one_or_two_future_minutes_without_replay(self):
        assignment=assigned('EXP_R');times=assignment['random_entry_times']
        self.assertIn(len(times),(1,2));self.assertEqual(len(set(times)),len(times))
        for text in times:
            at=datetime.fromisoformat(text);self.assertTrue('09:30'<=at.strftime('%H:%M')<='14:30')
            data=bars([1000]*int((at-START).total_seconds()//60),assignment['symbol'])
            result=r.evaluate_entry('EXP_R',data,symbol=assignment['symbol'],now=at,assignment=assignment)
            self.assertTrue(result['signal'])
            self.assertFalse(r.evaluate_entry('EXP_R',data,symbol=assignment['symbol'],now=at+timedelta(minutes=1),assignment=assignment)['signal'])
        self.assertEqual(r.random_entry_times(DAY,'005930',START.replace(hour=15)),())

    def test_c_gap_source_time_and_preopen_only(self):
        symbol='005930';now=START+timedelta(seconds=5)
        assignment=r.assign_candidate(DAY,symbol,START-timedelta(minutes=5),assigned_at=now,c_preopen=True)
        context={'symbol':symbol,'mode':'paper','exchange':'KRX','source_sha256':'b'*64,
            'request_started_at':START.isoformat(),'observed_at':now.isoformat(),'day_open':1030,'previous_close':1000,
            'previous_session_day':'20261030','previous_close_available_at':START.replace(day=1,hour=20).isoformat()}
        self.assertTrue(r.evaluate_entry('EXP_C',[],symbol=symbol,now=now,assignment=assignment,opening_context=context)['signal'])
        for replacement in ({'day_open':1029},{'observed_at':(START-timedelta(seconds=1)).isoformat()},
                {'previous_close_available_at':now.isoformat()},{'source_sha256':'bad'}):
            self.assertFalse(r.evaluate_entry('EXP_C',[],symbol=symbol,now=now,assignment=assignment,opening_context={**context,**replacement})['signal'])
        with self.assertRaises(ValueError):r.assign_candidate(DAY,symbol,START,c_preopen=True)

    def test_d_selection_frozen_max10_order_independent_and_no_late_addition(self):
        rows=[{'symbol':f'{i:06d}','first_seen_at':START.isoformat()} for i in range(30)]
        at=START.replace(hour=15,minute=19,second=30)
        selected=r.select_overnight(rows,day=DAY,snapshot_at=at)
        self.assertEqual(len(selected['assignments']),10)
        self.assertEqual(selected,r.select_overnight(rows[::-1],day=DAY,snapshot_at=at))
        self.assertEqual(selected,r.select_overnight([],day=DAY,snapshot_at=at,existing=selected))
        with self.assertRaises(ValueError):r.select_overnight(rows,day=DAY,snapshot_at=at.replace(minute=20))
        assignment=selected['assignments'][0];symbol=assignment['symbol']
        self.assertTrue(r.evaluate_entry('EXP_D',[],symbol=symbol,now=at.replace(minute=20),assignment=assignment)['signal'])
        self.assertFalse(r.evaluate_entry('EXP_D',[],symbol=symbol,now=at.replace(minute=30),assignment=assignment)['signal'])
        tampered=deepcopy(selected);tampered['assignments'].pop()
        with self.assertRaises(ValueError):r.select_overnight(rows,day=DAY,snapshot_at=at,existing=tampered)

    def position(self,arm,at,price=1000):return {'arm':arm,'symbol':'005930','entry_at':at.isoformat(),'entry_price':price,'quantity':1}

    def test_e_fifteen_real_completed_bars_then_next_minute(self):
        pos=self.position('EXP_E',START+timedelta(minutes=21,seconds=2))
        data=bars([1000]*36,'005930');now=START+timedelta(minutes=36)
        self.assertTrue(r.evaluate_exit('EXP_E',data,symbol='005930',now=now,position=pos)['signal'])
        self.assertFalse(r.evaluate_exit('EXP_E',data[:-1],symbol='005930',now=now-timedelta(minutes=1),position=pos)['signal'])

    def test_btp_uses_completed_close_not_high(self):
        pos=self.position('EXP_B_TP',START+timedelta(minutes=30))
        data=bars([1000]*30+[1005],'005930');data[-1]=replace(data[-1],high_price=2000)
        self.assertFalse(r.evaluate_exit('EXP_B_TP',data,symbol='005930',now=START+timedelta(minutes=31),position=pos)['signal'])
        data[-1]=replace(data[-1],close=1010)
        self.assertEqual(r.evaluate_exit('EXP_B_TP',data,symbol='005930',now=START+timedelta(minutes=31),position=pos)['reason'],'take_profit_100bp')

    def test_dead_cross_a_b_and_negative_random_fallback(self):
        for arm,long in (('EXP_A',20),('EXP_B',30)):
            data=bars([1000]*long+[900],'005930');pos=self.position(arm,START+timedelta(minutes=long))
            self.assertEqual(r.evaluate_exit(arm,data,symbol='005930',now=START+timedelta(minutes=long+1),position=pos)['reason'],'dead_cross')
        data=bars([1100]*20+[1000]*25,'005930');pos=self.position('EXP_R',START+timedelta(minutes=30))
        first_rows=[replace(bar,source_query_started_at=START+timedelta(minutes=31)) for bar in data[:31]]
        first=r.evaluate_exit('EXP_R',first_rows,symbol='005930',now=START+timedelta(minutes=31),position=pos)
        self.assertFalse(first['signal']);self.assertEqual(first['position_update']['r_first_bar_negative'],True)
        pos.update(first['position_update'])
        self.assertEqual(r.evaluate_exit('EXP_R',data,symbol='005930',now=START+timedelta(minutes=45),position=pos)['reason'],'negative_entry_hold_15_completed_bars')

    def test_forced_liquidation_all_intraday_owner_only_and_d_next_session(self):
        for arm in r.ARMS-{'EXP_D'}:
            pos=self.position(arm,START)
            self.assertTrue(r.evaluate_exit(arm,[],symbol='005930',now=START.replace(hour=15,minute=15),position=pos)['signal'])
            self.assertFalse(r.evaluate_exit(arm,[],symbol='000660',now=START.replace(hour=15,minute=15),position=pos)['signal'])
        pos=self.position('EXP_D',START.replace(hour=15,minute=25));nextday=START+timedelta(days=1,seconds=5)
        self.assertFalse(r.evaluate_exit('EXP_D',[],symbol='005930',now=nextday,position=pos)['signal'])
        self.assertTrue(r.evaluate_exit('EXP_D',[],symbol='005930',now=nextday,position=pos,next_session_day='20261103')['signal'])
        self.assertFalse(r.evaluate_exit('EXP_D',[],symbol='005930',now=nextday+timedelta(minutes=1),position=pos,next_session_day='20261103')['signal'])

    def test_existing_s_arm_requires_assignment_and_external_bound_result(self):
        assignment=assigned('S2_surge_high_breakout');symbol=assignment['symbol'];now=START+timedelta(minutes=31)
        data=bars([1000]*31,symbol)
        signal={'arm':assignment['arm'],'symbol':symbol,'eligible':True,'signal_at':now.isoformat(),'evidence_sha256':'b'*64}
        self.assertTrue(r.evaluate_entry(assignment['arm'],data,symbol=symbol,now=now,assignment=assignment,external_signal=signal)['signal'])
        self.assertFalse(r.evaluate_entry('EXP_A',data,symbol=symbol,now=now,assignment=assignment)['signal'])

    def test_s5_keeps_five_minute_signal_dedupe_with_current_evaluation(self):
        assignment=assigned('S5_steady_uptrend_vwap');symbol=assignment['symbol'];now=START+timedelta(minutes=33)
        data=bars([1000]*30,symbol)
        signal={'arm':assignment['arm'],'symbol':symbol,'eligible':True,'signal_at':(START+timedelta(minutes=30)).isoformat(),
            'evaluated_at':now.isoformat(),'evidence_sha256':'b'*64}
        first=r.evaluate_entry(assignment['arm'],data,symbol=symbol,now=now,assignment=assignment,external_signal=signal)
        self.assertTrue(first['signal'])
        later=now+timedelta(minutes=1)
        second=r.evaluate_entry(assignment['arm'],data,symbol=symbol,now=later,assignment=assignment,external_signal={**signal,'evaluated_at':later.isoformat()})
        self.assertTrue(second['signal']);self.assertEqual(first['signal_key'],second['signal_key'])
        self.assertFalse(r.evaluate_entry(assignment['arm'],data,symbol=symbol,now=later+timedelta(minutes=1),assignment=assignment,external_signal={**signal,'evaluated_at':(later+timedelta(minutes=1)).isoformat()})['signal'])


if __name__=='__main__':unittest.main()
