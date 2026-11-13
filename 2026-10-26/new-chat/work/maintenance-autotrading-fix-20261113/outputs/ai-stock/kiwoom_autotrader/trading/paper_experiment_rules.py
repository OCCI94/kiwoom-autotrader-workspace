"""Pure paper experiment rules, version 1; no I/O, orders or synthetic prices.

Signals use actual completed MinuteBar-compatible rows only. A signal on bar i
permits an order in minute i+1, never a retrospective fill at that minute's open.
SMA/volume windows are observed-row windows, as in the supplied backtest;
missing minutes remain explicit. C/D are explicitly timed auction/open events.
Assignment weights are probabilities, not guaranteed finite-cohort quotas.
"""
from copy import deepcopy
from datetime import datetime, timedelta
from decimal import Decimal
import hashlib
import json
import re
from zoneinfo import ZoneInfo

KST = ZoneInfo('Asia/Seoul')
WEIGHTS = (('EXP_R',15),('EXP_A',15),('EXP_B',10),('EXP_B_TP',10),('EXP_E',10),
    ('S2_surge_high_breakout',10),('S3_upper_limit_continuation',10),
    ('S4_theme_and_catalyst_leader',10),('S5_steady_uptrend_vwap',10))
ARMS = frozenset(arm for arm,_ in WEIGHTS) | {'EXP_C','EXP_D'}
ALIASES = {'R':'EXP_R','A':'EXP_A','B':'EXP_B','B-TP':'EXP_B_TP','E':'EXP_E','C':'EXP_C','D':'EXP_D'}
SCHEMA = 'paper-experiment-rules-v1'
ASSIGNMENT_SCHEMA = 'paper-experiment-assignment-v1'
NATIVE_FIVE_WEIGHTS = tuple((arm,20) for arm in ('EXP_R','EXP_A','EXP_B','EXP_B_TP','EXP_E'))
NATIVE_SIX_S5_WEIGHTS = tuple((arm,1) for arm in ('EXP_R','EXP_A','EXP_B','EXP_B_TP','EXP_E','S5_steady_uptrend_vwap'))
CONTROL_FLOOR_POLICY = 'prospective_daily_control_floor_v2'


def _weights(profile):
    if profile is None:return WEIGHTS
    if profile=='native-five-v1':return NATIVE_FIVE_WEIGHTS
    if profile=='native-six-s5-v1':return NATIVE_SIX_S5_WEIGHTS
    raise ValueError('assignment_profile_invalid')


def _digest(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=True,sort_keys=True,separators=(',',':'),
        allow_nan=False).encode()).hexdigest()


def _time(value):
    value = datetime.fromisoformat(value) if isinstance(value,str) else value
    if not isinstance(value,datetime) or value.utcoffset()!=timedelta(hours=9):raise ValueError('kst_time_required')
    return value.astimezone(KST)


def _symbol(value):
    if not isinstance(value,str) or re.fullmatch(r'[0-9]{6}',value) is None:raise ValueError('symbol_invalid')
    return value


def _day(value):
    parsed=datetime.strptime(value,'%Y%m%d')
    if parsed.strftime('%Y%m%d')!=value:raise ValueError('day_invalid')
    return parsed.replace(tzinfo=KST)


def _sha(value):return isinstance(value,str) and re.fullmatch('[0-9a-f]{64}',value) is not None
def _arm(value):return ALIASES.get(value,value)
def _number(value):return None if value is None else format(value,'f')


def random_entry_times(day, symbol, first_seen_at):
    """One/two planned wall-clock minutes; missed slots are never replayed."""
    start=_day(day);_symbol(symbol);first=_time(first_seen_at)
    if first.date()!=start.date():raise ValueError('first_seen_day_mismatch')
    seed=_digest(['paper-r-schedule-v1',day,symbol])
    count=1+int(seed[:8],16)%2
    minutes=[start.replace(hour=9,minute=30)+timedelta(minutes=i) for i in range(301)]
    minutes=[at for at in minutes if at>=first]
    chosen=sorted(minutes,key=lambda at:_digest([seed,at.isoformat()]))[:count]
    return tuple(at.isoformat() for at in sorted(chosen))


def assign_candidate(day, symbol, first_seen_at, *, assigned_at=None, existing=None, c_preopen=False,
                     d_locked=False, mode='paper', profile=None):
    """Produce/preserve one source-hashed assignment; persistence is caller-owned.

    C eligibility is decided from the later observed opening quote. c_preopen
    reserves a pre-open C candidate only; it is not proof that the gap passed.
    A D-owned symbol cannot receive any new intraday assignment until sold.
    """
    if mode!='paper':return None  # Do not inspect experiment inputs in real mode.
    start=_day(day);_symbol(symbol);first=_time(first_seen_at)
    assigned=_time(assigned_at) if assigned_at is not None else first
    if first.date()!=start.date():raise ValueError('first_seen_day_mismatch')
    if assigned.date()!=first.date() or assigned<first:raise ValueError('assignment_time_invalid')
    if d_locked:return None
    if existing is not None:
        validate_assignment(existing,symbol=symbol,day=day,now=assigned)
        return deepcopy(existing)
    weights=_weights(profile)
    if type(c_preopen) is not bool:raise ValueError('c_preopen_invalid')
    seed=_digest(['paper-arm-assignment-v1',day,symbol]);slot=int(seed,16)%sum(weight for _,weight in weights)
    arm=None;edge=0
    for name,weight in weights:
        edge+=weight
        if slot<edge:arm=name;break
    if c_preopen:
        if first>=start.replace(hour=9):raise ValueError('c_candidate_not_preopen')
        arm='EXP_C'
    result={'schema':ASSIGNMENT_SCHEMA,'day':day,'symbol':symbol,'arm':arm,
        'first_seen_at':first.isoformat(),'assigned_at':assigned.isoformat(),
        'seed_sha256':seed,'slot':slot,'weights':dict(weights),'weight_basis':'probability_not_finite_quota',
        'random_entry_times':list(random_entry_times(day,symbol,assigned)) if arm=='EXP_R' else [],
        'c_preopen':c_preopen,'selection_sha256':None}
    if profile is not None:result['profile']=profile
    result['assignment_sha256']=_digest(result)
    return result


def validate_assignment(value, *, symbol, day, now):
    now=_time(now)
    if isinstance(value,dict) and value.get('schema')=='paper-experiment-extended-assignment-v1':
        from .paper_experiment_extended_rules import validate_assignment as extended_assignment
        return extended_assignment(value,symbol=symbol,day=day,now=now)
    if (not isinstance(value,dict) or value.get('schema')!=ASSIGNMENT_SCHEMA
            or value.get('symbol')!=symbol or value.get('day')!=day or value.get('arm') not in ARMS
            or not _sha(value.get('assignment_sha256'))
            or value['assignment_sha256']!=_digest({k:v for k,v in value.items() if k!='assignment_sha256'})):
        raise ValueError('assignment_invalid')
    first=_time(value['first_seen_at']);assigned=_time(value['assigned_at'])
    if first.date()!=_day(day).date() or not first<=assigned<=now:raise ValueError('assignment_future_or_changed')
    if value['arm']=='EXP_D':
        if not _sha(value.get('selection_sha256')) or assigned>=_day(day).replace(hour=15,minute=20):
            raise ValueError('d_selection_unverified')
    elif value.get('allocation_policy')==CONTROL_FLOOR_POLICY:
        expected=assign_prospective_candidate(day,symbol,first,assigned_at=assigned,
            prior_allocations=value['prior_allocations'],profile=value.get('profile'))
        if expected!=_without_theme(value):raise ValueError('prospective_assignment_changed')
    elif value.get('allocation_policy')=='frozen_cohort_minimum_controls_v1':
        cohort=assign_cohort(value['cohort_candidates'],day=day,snapshot_at=value['cohort_snapshot_at'],profile=value.get('profile'))
        expected=next((row for row in cohort['assignments'] if row['symbol']==symbol),None)
        if expected!=_without_theme(value):raise ValueError('cohort_assignment_changed')
    else:
        expected=assign_candidate(day,symbol,first,assigned_at=assigned,c_preopen=value.get('c_preopen'),profile=value.get('profile'))
        if expected!=_without_theme(value):raise ValueError('assignment_policy_changed')
    if 'theme_context' in value:
        from .paper_experiment_theme import validate_theme_context
        validate_theme_context(value['theme_context'],symbol=symbol,as_of=assigned)
    return value


def _without_theme(value):
    result=deepcopy(value)
    if 'theme_context' in result:
        result.pop('theme_context')
        result['assignment_sha256']=_digest({k:v for k,v in result.items() if k!='assignment_sha256'})
    return result


def assign_prospective_candidate(day,symbol,first_seen_at,*,assigned_at,prior_allocations,profile):
    """New arrivals only: preserve every saved assignment and enforce R/A floors.

    The hash proposal is not a finite-sample guarantee. Before a simultaneous
    ceiling increase, reserve one of the two control deficits one arrival early.
    A legacy deficient prefix is repaired only by future arrivals; its history
    remains deficient. This policy is not independent uniform randomization.
    """
    at=_time(assigned_at);names=[arm for arm,_ in _weights(profile)]
    if profile not in ('native-five-v1','native-six-s5-v1'):
        raise ValueError('prospective_profile_invalid')
    if not isinstance(prior_allocations,list) or len(prior_allocations)>2000:
        raise ValueError('prospective_prefix_invalid')
    prefix=[];seen=set();counts={arm:0 for arm in names}
    for row in prior_allocations:
        if (not isinstance(row,dict) or set(row)!={'symbol','arm','assigned_at','assignment_sha256'}
                or row['arm'] not in names or not _sha(row['assignment_sha256'])):
            raise ValueError('prospective_prefix_invalid')
        code=_symbol(row['symbol']);when=_time(row['assigned_at'])
        if code in seen or code==symbol or when.date()!=at.date() or when>at:
            raise ValueError('prospective_prefix_invalid')
        seen.add(code);counts[row['arm']]+=1;prefix.append(deepcopy(row))
    prefix.sort(key=lambda row:row['symbol'])
    result=assign_candidate(day,symbol,first_seen_at,assigned_at=at,profile=profile)
    n=len(prefix)+1;required=(15*n+99)//100;next_required=(15*(n+1)+99)//100
    deficit=[arm for arm in ('EXP_R','EXP_A') if counts[arm]<required]
    choices=deficit or ([arm for arm in ('EXP_R','EXP_A') if counts[arm]<next_required]
                        if all(counts[arm]<next_required for arm in ('EXP_R','EXP_A')) else [])
    proposal=result['arm'];reason='seeded_proposal_with_floor_guard'
    if choices:
        result['arm']=min(choices,key=lambda arm:(counts[arm],_digest([CONTROL_FLOOR_POLICY,day,symbol,arm])))
        reason='current_control_deficit' if deficit else 'reserve_before_simultaneous_floor_increase'
    after=dict(counts);after[result['arm']]+=1
    result.update(allocation_policy=CONTROL_FLOOR_POLICY,prior_allocations=prefix,
        proposed_arm=proposal,allocation_reason=reason,
        control_floor={'minimum_pct':15,'denominator':n,'required_each':required,
            'actual_counts':after,'satisfied':n>=2 and all(after[a]>=required for a in ('EXP_R','EXP_A')),
            'legacy_prefix_below_floor':bool(prefix) and any(counts[a]*100<15*len(prefix) for a in ('EXP_R','EXP_A')),
            'past_assignments_changed':False},weight_basis='target_weights_with_prospective_actual_control_floor',
        random_entry_times=list(random_entry_times(day,symbol,at)) if result['arm']=='EXP_R' else [])
    result['assignment_sha256']=_digest({k:v for k,v in result.items() if k!='assignment_sha256'})
    return result


def assign_cohort(candidates, *, day, snapshot_at, existing=None, mode='paper', profile=None):
    """Optional fixed-cohort allocation with actual R/A minimums for N>=2.

    Each symbol must have been observed before this immutable cohort cutoff.
    Never add later winners to this object, reshuffle an existing assignment,
    or claim that separately allocated subsequent arrivals retain these quotas.
    """
    if mode!='paper':return None
    at=_time(snapshot_at);start=_day(day);weights=_weights(profile)
    if at.date()!=start.date():raise ValueError('cohort_day_mismatch')
    if existing is not None:
        expected=assign_cohort(existing['candidates'],day=day,snapshot_at=existing['snapshot_at'],profile=existing.get('profile'))
        if existing!=expected or _time(existing['snapshot_at'])>at:raise ValueError('cohort_changed')
        return deepcopy(existing)
    records=[];seen=set()
    for item in candidates:
        symbol=_symbol(item['symbol']);first=_time(item['first_seen_at'])
        if symbol in seen or first.date()!=at.date() or first>at:raise ValueError('cohort_candidate_invalid')
        seen.add(symbol);records.append({'symbol':symbol,'first_seen_at':first.isoformat()})
    if not 2<=len(records)<=40:raise ValueError('cohort_size_requires_2_to_40')
    records.sort(key=lambda row:row['symbol'])
    ranking=sorted(records,key=lambda row:_digest(['paper-cohort-rank-v1',day,row['symbol']]))
    control=(15*len(records)+99)//100
    if profile=='native-five-v1':
        names=[weights[i%len(weights)][0] for i in range(len(records))]
    elif profile=='native-six-s5-v1':
        # Equal target probabilities; a finite cohort still reserves the
        # explicitly promised R/A minimum instead of rounding it below 15%.
        names=['EXP_R']*control+['EXP_A']*control
        counts={arm:names.count(arm) for arm,_ in weights}
        while len(names)<len(records):
            arm=min(weights,key=lambda item:(counts[item[0]],list(dict(weights)).index(item[0])))[0]
            names.append(arm);counts[arm]+=1
    else:
        names=['EXP_R']*control+['EXP_A']*control
        others=[arm for arm,_ in weights if arm not in ('EXP_R','EXP_A')]
        names += [others[i%len(others)] for i in range(len(records)-len(names))]
    result={'schema':'paper-experiment-cohort-v1','day':day,'snapshot_at':at.isoformat(),
        'candidates':records,'control_minimum_count':control,'target_weights':dict(weights),
        'actual_counts':{arm:names.count(arm) for arm,_ in weights},'assignments':[]}
    if profile is not None:result['profile']=profile
    for item,arm in zip(ranking,names):
        assigned=assign_candidate(day,item['symbol'],item['first_seen_at'],assigned_at=at,profile=profile)
        assigned.update(arm=arm,allocation_policy='frozen_cohort_minimum_controls_v1',
            cohort_candidates=deepcopy(records),cohort_snapshot_at=at.isoformat(),
            weight_basis='fixed_cohort_actual_R_A_at_least_15pct',
            random_entry_times=list(random_entry_times(day,item['symbol'],at)) if arm=='EXP_R' else [])
        assigned['assignment_sha256']=_digest({k:v for k,v in assigned.items() if k!='assignment_sha256'})
        result['assignments'].append(assigned)
    result['cohort_sha256']=_digest(result)
    return result


def select_overnight(candidates, *, day, snapshot_at, existing=None, mode='paper'):
    """Freeze at most ten D symbols from a genuinely pre-15:20 candidate set."""
    if mode!='paper':return None
    at=_time(snapshot_at);start=_day(day)
    if at.date()!=start.date() or at>=start.replace(hour=15,minute=20):raise ValueError('d_snapshot_after_cutoff')
    if existing is not None:
        if (existing.get('day')!=day or existing.get('selection_sha256')!=_digest({k:v for k,v in existing.items()
                if k not in ('selection_sha256','assignments')}) or _time(existing['snapshot_at'])>at):
            raise ValueError('d_selection_changed')
        expected=select_overnight(existing['candidates'],day=day,snapshot_at=existing['snapshot_at'])
        if existing!=expected:raise ValueError('d_selection_changed')
        return deepcopy(existing)
    records=[];seen=set()
    for value in candidates:
        symbol=_symbol(value['symbol']);first=_time(value['first_seen_at'])
        if symbol in seen or first.date()!=at.date() or first>at:raise ValueError('d_candidate_invalid')
        seen.add(symbol);records.append({'symbol':symbol,'first_seen_at':first.isoformat()})
    if len(records)>2000:raise ValueError('d_candidate_limit')
    records.sort(key=lambda r:r['symbol'])
    chosen=sorted(records,key=lambda r:_digest(['paper-d-selection-v1',day,r['symbol']]))[:10]
    result={'schema':'paper-d-selection-v1','day':day,'snapshot_at':at.isoformat(),
        'candidates':records,'selected_symbols':[r['symbol'] for r in chosen],'max_symbols':10}
    digest=_digest(result);result['selection_sha256']=digest;result['assignments']=[]
    for value in chosen:
        assignment={'schema':ASSIGNMENT_SCHEMA,'day':day,'symbol':value['symbol'],'arm':'EXP_D',
            'first_seen_at':value['first_seen_at'],'assigned_at':at.isoformat(),'selection_sha256':digest}
        assignment['assignment_sha256']=_digest(assignment);result['assignments'].append(assignment)
    return result


def _bars(bars,symbol,now):
    """Validate real native MinuteBar fields; no transport or inferred row use."""
    if not isinstance(bars,(tuple,list)) or not bars or len(bars)>390:raise ValueError('bars_missing')
    result=[];previous=None
    for row in bars:
        at=_time(row.start);request=_time(row.source_query_started_at)
        if (at.date()!=now.date() or at.second or at.microsecond or at.hour<9
                or at.strftime('%H:%M')>='15:30' or at+timedelta(minutes=1)>now
                or request>now or row.source_symbol!=symbol or row.source_mode!='paper'
                or not _sha(row.source_response_sha256) or row.gap_proof is not None
                or previous is not None and at<=previous):raise ValueError('bar_time_or_provenance_invalid')
        o,h,l,c,v=row.open_price,row.high_price,row.low_price,row.close,row.volume
        if (any(type(x) is not int or x<=0 for x in (o,h,l,c)) or type(v) is not int or v<0
                or not l<=min(o,c)<=max(o,c)<=h):raise ValueError('bar_ohlcv_invalid')
        result.append((at,Decimal(o),Decimal(h),Decimal(l),Decimal(c),Decimal(v),row.source_response_sha256))
        previous=at
    return result


def _sma(rows,period,end=None):
    end=len(rows) if end is None else end
    if end<period:return None
    return sum(r[4] for r in rows[end-period:end])/Decimal(period)


def _cross(rows,short,long):
    if len(rows)<long+1:return None,None,None,None
    long_now=_sma(rows,long);difference=_sma(rows,short)-long_now
    previous=_sma(rows,short,len(rows)-1)-_sma(rows,long,len(rows)-1)
    return previous,difference,difference/long_now*10000,long_now


def _features(rows,short,long,opening_context=None):
    prev,diff,bps,slow=_cross(rows,short,long)
    rvol=None
    if len(rows)>=21:
        baseline=sum(row[5] for row in rows[-21:-1])/20
        if baseline>0:rvol=rows[-1][5]/baseline
    slope=None
    # Match engine.entry_ok: require i >= long+5, not merely enough slices.
    if len(rows)>=long+6:slope=_sma(rows,long)-_sma(rows,long,len(rows)-5)
    volume=sum(row[5] for row in rows)
    vwap=sum((r[2]+r[3]+r[4])/3*r[5] for r in rows)/volume if volume>0 else None
    trs=[rows[0][2]-rows[0][3]]+[max(rows[i][2],rows[i-1][4])-min(rows[i][3],rows[i-1][4]) for i in range(1,len(rows))]
    atr=sum(trs[-14:])/14/rows[-1][4] if len(rows)>=14 else None
    day_open=rows[0][1] if rows[0][0].strftime('%H:%M')=='09:00' else None
    previous_close=None
    if isinstance(opening_context,dict):
        prior=opening_context.get('previous_close');available=opening_context.get('previous_close_available_at')
        if type(prior) is int and prior>0 and available and _time(available)<rows[0][0].replace(hour=9,minute=0):previous_close=Decimal(prior)
    missing=int((rows[-1][0]-rows[0][0]).total_seconds()//60)+1-len(rows)
    return {'volume_multiple':_number(rvol),'cross_bps':_number(bps),'sma_slope':_number(slope),
        'vwap_distance_bps':_number((rows[-1][4]/vwap-1)*10000) if vwap else None,
        'atr_price_ratio':_number(atr),'day_open_return_bps':_number((rows[-1][4]/day_open-1)*10000) if day_open else None,
        'gap_bps':_number((day_open/previous_close-1)*10000) if day_open and previous_close else None,
        'previous_difference':_number(prev),'difference':_number(diff),'slow_sma':_number(slow),
        'sma_basis':'actual_observed_rows','vwap_basis':'actual_observed_rows',
        'vwap_exact_full_session':bool(day_open and missing==0),'missing_minutes':missing,
        'observed_rows':len(rows),'theme':None,'market_index_state':None,
        'time_band':('morning' if rows[-1][0].strftime('%H:%M')<'10:30' else 'midday' if rows[-1][0].strftime('%H:%M')<'13:30' else 'afternoon')}


def _result(arm,symbol,now,reason,*,signal=False,action=None,**extra):
    key_at=extra.get('dedup_at',extra.get('signal_at')) or (now.replace(second=0,microsecond=0).isoformat() if isinstance(now,datetime) else None)
    return {'schema':SCHEMA,'arm':arm,'symbol':symbol,'evaluated_at':now.isoformat() if isinstance(now,datetime) else None,
        'available_at':now.isoformat() if isinstance(now,datetime) else None,
        'signal_key':_digest([SCHEMA,arm,symbol,action,key_at]) if signal else None,
        'signal':signal,'action':action if signal else None,'reason':reason,'permission_to_trade':False,**extra}


def _bar_entry_checks(arm,rows,minute,features,assignment):
    """Shared formula core; callers must separately enforce PIT/ownership."""
    short,long=(5,20) if arm=='EXP_A' else (10,30)
    prev,diff,bps,_=_cross(rows,short,long)
    cross=prev is not None and prev<=0<diff
    rvol=Decimal(features['volume_multiple']) if features['volume_multiple'] is not None else None
    slope=Decimal(features['sma_slope']) if features['sma_slope'] is not None else None
    if arm=='EXP_A':return {'golden_cross':cross,'cross_at_least_10bp':bps is not None and bps>=10}
    if arm in ('EXP_B','EXP_B_TP'):
        return {'golden_cross':cross,'volume_multiple_at_least_2':rvol is not None and rvol>=2,
            'slow_sma_rising_vs_five_rows_ago':slope is not None and slope>0,
            'entry_before_1500':minute.strftime('%H:%M')<'15:00'}
    if arm=='EXP_E':
        return {'event_window': '09:20'<=rows[-1][0].strftime('%H:%M')<'15:00',
            'bar_return_at_most_minus_150bp':rows[-1][4]/rows[-1][1]-1<=Decimal('-.015'),
            'volume_multiple_at_least_3':rvol is not None and rvol>=3}
    if arm=='EXP_R':return {'random_scheduled_minute':minute.isoformat() in assignment['random_entry_times']}
    raise ValueError('arm_invalid')


def _opening(context,symbol,now):
    if not isinstance(context,dict):raise ValueError('opening_context_missing')
    observed=_time(context['observed_at']);request=_time(context['request_started_at'])
    if (context.get('symbol')!=symbol or context.get('mode')!='paper' or context.get('exchange')!='KRX'
            or not _sha(context.get('source_sha256')) or not request<=observed<=now
            or observed.date()!=now.date() or observed<now.replace(hour=9,minute=0,second=0,microsecond=0)
            or not 0<=(now-observed).total_seconds()<=5
            or type(context.get('day_open')) is not int or context['day_open']<=0
            or type(context.get('previous_close')) is not int or context['previous_close']<=0
            or _time(context['previous_close_available_at'])>=now.replace(hour=9,minute=0,second=0,microsecond=0)
            or _day(context['previous_session_day']).date()>=now.date()):raise ValueError('opening_context_unverified')
    return Decimal(context['day_open'])/context['previous_close']-1


def evaluate_entry(arm,bars,*,symbol,now,assignment,opening_context=None,external_signal=None,mode='paper'):
    if mode!='paper':return _result(None,None,None,'paper_only')
    arm=_arm(arm)
    try:
        now=_time(now);_symbol(symbol)
        validate_assignment(assignment,symbol=symbol,day=now.strftime('%Y%m%d'),now=now)
        if arm!=assignment['arm']:raise ValueError('arm_not_assigned')
        minute=now.replace(second=0,microsecond=0);hm=now.strftime('%H:%M')
        if arm=='EXP_C':
            gap=_opening(opening_context,symbol,now)
            good=hm=='09:00' and gap>=Decimal('.03')
            return _result(arm,symbol,now,'gap_open' if good else 'gap_or_open_window_not_met',signal=good,action='buy',
                signal_at=opening_context['observed_at'],dedup_at=minute.isoformat(),order_window_start=minute.isoformat(),
                order_window_end=(minute+timedelta(minutes=1)).isoformat(),signal_price=opening_context['day_open'],
                signal_price_basis='official_day_open',features={'gap_bps':_number(gap*10000)})
        if arm=='EXP_D':
            good='15:20'<=hm<'15:30'
            return _result(arm,symbol,now,'closing_auction' if good else 'outside_closing_auction',signal=good,action='buy',
                signal_at=now.isoformat(),selection_at=assignment['assigned_at'],
                dedup_at=now.replace(hour=15,minute=20,second=0,microsecond=0).isoformat(),
                selection_sha256=assignment['selection_sha256'],
                order_window_start=now.replace(hour=15,minute=20,second=0,microsecond=0).isoformat(),
                order_window_end=now.replace(hour=15,minute=30,second=0,microsecond=0).isoformat())
        rows=_bars(bars,symbol,now)
        if arm!='S5_steady_uptrend_vwap' and rows[-1][0]+timedelta(minutes=1)!=minute:raise ValueError('latest_completed_minute_missing')
        short,long=(5,20) if arm=='EXP_A' else (10,30)
        features=_features(rows,short,long,opening_context)
        extra={'signal_at':minute.isoformat(),'signal_bar_start':rows[-1][0].isoformat(),
            'order_window_start':minute.isoformat(),'order_window_end':(minute+timedelta(minutes=1)).isoformat(),
            'signal_price':int(rows[-1][4]),'signal_price_basis':'completed_signal_bar_close',
            'features':features,'source_sha256':sorted({r[6] for r in rows})}
        if not '09:00'<=hm<'15:15':return _result(arm,symbol,now,'outside_intraday_entry_window',**extra)
        if arm.startswith('S'):
            # Existing S2--S5 modules retain their own validated entry rules.
            expected_signal=(minute-timedelta(minutes=minute.minute%5)) if arm=='S5_steady_uptrend_vwap' else minute
            good=(isinstance(external_signal,dict) and external_signal.get('arm')==arm
                and external_signal.get('symbol')==symbol and external_signal.get('eligible') is True
                and external_signal.get('signal_at')==expected_signal.isoformat()
                and (arm!='S5_steady_uptrend_vwap' or external_signal.get('evaluated_at')==now.isoformat())
                and _sha(external_signal.get('evidence_sha256')))
            extra['signal_at']=expected_signal.isoformat()
            return _result(arm,symbol,now,'existing_arm_signal' if good else 'existing_arm_evaluator_required',signal=good,action='buy',**extra)
        checks=_bar_entry_checks(arm,rows,minute,features,assignment)
        good=all(checks.values())
        return _result(arm,symbol,now,'entry_signal' if good else 'entry_conditions_not_met',signal=good,action='buy',checks=checks,**extra)
    except (ValueError,TypeError,KeyError,AttributeError,ArithmeticError):
        return _result(arm,symbol,now,'entry_evidence_unverified')


def normalize_exit_session(value, now):
    """Validate a caller's saved official range; never infer a trading date."""
    now = _time(now)
    if not isinstance(value, dict):
        raise ValueError('official_exit_session_unavailable')
    start, end = _time(value['start']), _time(value['end'])
    day = _day(value['session_day']).strftime('%Y%m%d')
    source = value.get('calendar_source_sha256')
    if (not start < end or start.date() != end.date() or start.date() != now.date()
            or day != now.strftime('%Y%m%d') or not isinstance(source, str)
            or re.fullmatch('[a-f0-9]{64}', source) is None):
        raise ValueError('official_exit_session_unverified')
    return {'start': start, 'end': end, 'session_day': day,
            'calendar_source_sha256': source}


def evaluate_exit(arm,bars,*,symbol,now,position,next_session_day=None,official_session=None,mode='paper'):
    if mode!='paper':return _result(None,None,None,'paper_only')
    arm=_arm(arm)
    try:
        now=_time(now);_symbol(symbol)
        if (arm not in ARMS or not isinstance(position,dict) or position.get('arm')!=arm
                or position.get('symbol')!=symbol or type(position.get('quantity')) is not int or position['quantity']<=0
                or type(position.get('entry_price')) is not int or position['entry_price']<=0):raise ValueError('ownership_unverified')
        basis=position.get('entry_basis_krw')
        if basis is not None and (type(basis) is not int or basis<=0):raise ValueError('entry_basis_invalid')
        entry=_time(position['entry_at'])
        if entry>now:raise ValueError('entry_future')
        hm=now.strftime('%H:%M')
        if arm=='EXP_D':
            if official_session is not None:
                regular = normalize_exit_session(official_session, now)
                if next_session_day is not None and next_session_day != regular['session_day']:
                    raise ValueError('next_session_day_conflict')
                eligible = entry.date() < now.date() and regular['start'] <= now < regular['end']
                opening_end = min(regular['start'] + timedelta(minutes=1), regular['end'])
                reason = ('next_session_open' if now < opening_end else 'late_recovery_exit')
                return _result(arm,symbol,now,reason if eligible else 'await_verified_next_session',
                    signal=eligible,action='sell',signal_at=now.isoformat() if eligible else None,
                    order_window_end=(opening_end if reason == 'next_session_open' else regular['end']).isoformat(),
                    observed_session_open_at=regular['start'].isoformat(),
                    late_seconds=max(0,(now-regular['start']).total_seconds()) if eligible else None)
            # Backward-compatible pure evaluator contract. Runtime callers
            # always provide a source-bound official range for the new path.
            good=(next_session_day is not None and _day(next_session_day).date()>entry.date()
                and now.strftime('%Y%m%d')==next_session_day and hm=='09:00')
            return _result(arm,symbol,now,'next_session_open' if good else 'await_verified_next_session',signal=good,action='sell')
        if '15:15'<=hm<'15:20':
            return _result(arm,symbol,now,'intraday_forced_liquidation',signal=True,action='sell')
        if entry.date()!=now.date():raise ValueError('unexpected_overnight_position')
        if arm=='EXP_C' or arm.startswith('S'):return _result(arm,symbol,now,'await_owner_exit_rule')
        rows=_bars(bars,symbol,now);minute=now.replace(second=0,microsecond=0)
        if rows[-1][0]+timedelta(minutes=1)!=minute:raise ValueError('latest_completed_minute_missing')
        owned=[r for r in rows if r[0]>=entry.replace(second=0,microsecond=0)]
        if not owned:return _result(arm,symbol,now,'await_first_owned_completed_bar')
        short,long=(5,20) if arm=='EXP_A' else (10,30)
        prev,diff,_,_=_cross(rows,short,long);dead=prev is not None and prev>=0>diff
        reason=None;update={}
        if arm=='EXP_E':reason='hold_15_completed_bars' if len(owned)>=15 else None
        elif arm=='EXP_R':
            flag=position.get('r_first_bar_negative')
            if flag is not None and type(flag) is not bool:raise ValueError('r_initial_difference_invalid')
            if flag is None:
                first=entry.replace(second=0,microsecond=0)
                if owned[0][0]!=first:raise ValueError('r_first_owned_bar_missing')
                prefix=[r for r in rows if r[0]<=first]
                if len(prefix)<30:raise ValueError('r_initial_sma_missing')
                flag=_sma(prefix,10)-_sma(prefix,30)<0
                update={'r_first_bar_negative':flag,'r_first_bar_start':first.isoformat()}
            reason=('negative_entry_hold_15_completed_bars' if len(owned)>=15 else None) if flag else ('dead_cross' if dead else None)
        elif arm=='EXP_B_TP' and (rows[-1][4]*position['quantity']>=Decimal(basis)*Decimal('1.01')
                if basis is not None else rows[-1][4]>=Decimal(position['entry_price'])*Decimal('1.01')):reason='take_profit_100bp'
        elif dead:reason='dead_cross'
        return _result(arm,symbol,now,reason or 'hold',signal=reason is not None,action='sell',
            signal_at=minute.isoformat(),signal_bar_start=rows[-1][0].isoformat(),
            signal_price=int(rows[-1][4]),signal_price_basis='completed_signal_bar_close',
            order_window_start=minute.isoformat(),order_window_end=(minute+timedelta(minutes=1)).isoformat(),
            held_completed_bars=len(owned),position_update=update)
    except (ValueError,TypeError,KeyError,AttributeError,ArithmeticError):
        return _result(arm,symbol,now,'exit_evidence_unverified')
