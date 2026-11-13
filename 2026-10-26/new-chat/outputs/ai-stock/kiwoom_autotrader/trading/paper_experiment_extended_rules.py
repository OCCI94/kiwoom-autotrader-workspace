"""Pure paper ORB/GDR/VSS/LDR/ETF-B hypotheses; no I/O or order permission.

Frozen time-of-day selections use observed source-bound inputs, never closing
winners. Missing classification, VI, liquidity or calendar evidence stays
unevaluated. No synthetic bar, inferred fill or future official close is used.
"""
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal
from kiwoom_autotrader.trading import paper_experiment_rules as core

ARMS=frozenset({'EXP_ORB_A','EXP_ORB_B','EXP_GDR','EXP_VSS','EXP_LDR','EXP_ETF_B'})
OVERNIGHT=frozenset({'EXP_GDR','EXP_VSS','EXP_LDR'})
SCHEMA='paper-experiment-extended-assignment-v1'
ETF_SYMBOLS=frozenset({'069500','229200'})
LIMIT=Decimal(3_000_000_000)


def require(ok,why):
    if not ok:raise ValueError(why)
def _positive(v):
    require(type(v) is int and v>0,'positive_integer_required');return Decimal(v)
def _fresh(at,now,seconds):
    at=core._time(at)
    require(at.date()==now.date() and 0<=(now-at).total_seconds()<=seconds,'source_stale_or_future')
    return at
def _out(arm,symbol,now,reason,**values):
    return core._result(arm,symbol,now,reason,**values)


def daily_features(daily,*,symbol,now):
    require(isinstance(daily,dict) and daily.get('symbol')==symbol and daily.get('mode')=='paper'
        and core._sha(daily.get('source_sha256')) and core._time(daily['available_at'])<=now,'daily_source_unverified')
    rows=daily.get('rows');require(isinstance(rows,list) and 21<=len(rows)<=260,'daily_21_sessions_required')
    dates=[];values=[]
    for row in rows:
        day=core._day(row['date']);require(day.date()<now.date() and (not dates or row['date']>dates[-1]),'daily_time_invalid')
        o,h,l,c=(_positive(row[k]) for k in ('open','high','low','close'))
        require(l<=min(o,c)<=max(o,c)<=h and type(row['volume']) is int and row['volume']>=0
            and type(row['turnover_krw']) is int and row['turnover_krw']>=0,'daily_ohlcv_invalid')
        dates.append(row['date']);values.append((o,h,l,c,Decimal(row['volume']),Decimal(row['turnover_krw'])))
    tr=[max(values[i][1],values[i-1][3])-min(values[i][2],values[i-1][3]) for i in range(1,len(values))]
    return {'previous_close':values[-1][3],'atr14':sum(tr[-14:])/14,
        'mean_volume20':sum(r[4] for r in values[-20:])/20,
        'mean_turnover20_krw':sum(r[5] for r in values[-20:])/20,
        'momentum20':values[-1][3]/values[-21][3]-1,'prior_dates':dates,
        'source_sha256':daily['source_sha256'],'available_at':daily['available_at'],
        'basis':'actual_completed_observed_daily_sessions'}


def common_checks(context,*,symbol,now,features=None,selection_snapshot=False):
    """These entry checks never grant broker permission or alter owned exits."""
    market=context['market'];require(isinstance(market,dict) and market.get('symbol')==symbol
        and market.get('mode')=='paper' and core._sha(market.get('source_sha256')),'market_source_unverified')
    _fresh(market['observed_at'],now,60 if selection_snapshot else 5)
    require(market.get('classification_observed_at') is not None,'classification_unverified')
    _fresh(market['classification_observed_at'],now,300)
    require(core._sha(market.get('classification_source_sha256')),'classification_source_unverified')
    expected={'is_common_stock':True,'managed':False,'investment_warning':False,'investment_danger':False,
        'is_spac':False,'is_etf':False,'is_etn':False,'is_preferred':False}
    require(all(type(market.get(k)) is bool for k in expected),'classification_unverified')
    require(market.get('vi_complete',True) is True and market.get('vi_observed_at') is not None,
        'vi_evidence_unavailable')
    _fresh(market['vi_observed_at'],now,60 if selection_snapshot else 30)
    require(core._sha(market.get('vi_source_sha256')) and type(market.get('vi_active')) is bool
        and 'vi_last_trigger_at' in market,'vi_unverified')
    trigger=market['vi_last_trigger_at']
    if trigger is not None:
        trigger=core._time(trigger);require(trigger<=now,'vi_future')
    price,upper=_positive(market['current_price']),_positive(market['upper_limit_price'])
    require(price<=upper,'price_above_upper_limit')
    features=features or daily_features(context['daily'],symbol=symbol,now=now)
    return {'common_stock_only':all(market[k] is v for k,v in expected.items()),
        'liquidity_20d_at_least_3bn':features['mean_turnover20_krw']>=LIMIT,
        'vi_entry_executable':not market['vi_active'] and (trigger is None or (now-trigger).total_seconds()>=120),
        'upper_limit_distance_gt_5pct':(upper-price)/upper>Decimal('.05')}


def _serialize_features(value):
    return {k:core._number(v) if isinstance(v,Decimal) else deepcopy(v) for k,v in value.items()}


def krx_equity_tick_size(price):
    """KRX ordinary-share grid; ORB uses the range high, not later quote price.

    This is the ordinary-equity 2023-01-25 grid, not the ETF/ETN grid.
    """
    price=_positive(price)
    for upper,tick in ((2000,1),(5000,5),(20000,10),(50000,50),(200000,100),(500000,500)):
        if price<upper:return tick
    return 1000


def _candidate(kind,row,at):
    symbol=core._symbol(row['symbol']);first=core._time(row['first_seen_at'])
    require(first.date()==at.date() and first<=at,'candidate_first_seen_invalid')
    context=row['context'];features=daily_features(context['daily'],symbol=symbol,now=at)
    checks=common_checks(context,symbol=symbol,now=at,features=features,selection_snapshot=True)
    require(all(checks.values()),next((k for k,v in checks.items() if not v),'common_condition_failed'))
    market=context['market'];selection={'symbol':symbol,'first_seen_at':first.isoformat(),
        'features':_serialize_features(features),'common_checks':checks,'market_sha256':market['source_sha256']}
    if kind=='ORB':
        require(at.strftime('%H:%M')=='09:05','orb_selection_window')
        bars=core._bars(row['bars'],symbol,at)
        first5=[r for r in bars if r[0].strftime('%H:%M')<'09:05']
        require([r[0].strftime('%H:%M') for r in first5]==['09:00','09:01','09:02','09:03','09:04'],
            'orb_first_five_actual_minutes_required')
        volume=sum(r[5] for r in first5);history=context.get('same_window_history')
        from .paper_experiment_volume_history import validate_history
        validate_history(history,symbol=symbol,prior_dates=features['prior_dates'][-14:],now=at)
        same=sum(Decimal(r['volume']) for r in history['rows'])/14;denom=same;version='orb_same_window14_observed_v2'
        require(denom>0 and volume/denom>=2,'orb_relative_volume_below_2')
        require(first5[-1][4]>first5[0][1],'orb_first_five_not_bullish')
        opening={'including_opening_auction':{'high':str(max(r[2] for r in first5)),
            'low':str(min(r[3] for r in first5)),'volume':str(volume)},
            'excluding_0900_minute_proxy':{'high':str(max(r[2] for r in first5[1:])),
            'low':str(min(r[3] for r in first5[1:])),'volume':str(sum(r[5] for r in first5[1:]))},
            'auction_trade_isolation_available':False}
        selection.update(score=str(volume/denom),relative_volume=str(volume/denom),definition_version=version,
            relative_volume_same_window14=None if same is None or same==0 else str(volume/same),
            relative_volume_daily20=None if features['mean_volume20']==0 else str(volume/features['mean_volume20']),
            historical_session_count=14,historical_source_sha256=history['source_sha256'],
            historical_available_at=history['available_at'],daily_volume_fallback_used=False,
            opening_range=opening,tick_size=krx_equity_tick_size(int(max(r[2] for r in first5))),
            observed_current_price_tick_size=int(_positive(market['tick_size'])),
            tick_size_basis='KRX_ordinary_equity_20230125_at_opening_range_high',
            bar_source_sha256=sorted({r[6] for r in first5}),
            order_difference='completed_bar_confirmation_up_to_one_minute_later_than_stop_order')
    elif kind=='EXP_GDR':
        hm=at.strftime('%H:%M');require('08:30'<=hm<='09:00','gdr_selection_window')
        if hm<'09:00':
            price=_positive(market.get('indicative_price'));version='observed_indicative_open_v1'
        else:price=_positive(market.get('day_open'));version='observed_0900_open_fallback_v0'
        gap=price/features['previous_close']-1
        require(Decimal('-.15')<=gap<=Decimal('-.03'),'gdr_gap_range')
        selection.update(gap=str(gap),momentum20=str(features['momentum20']),definition_version=version,
            selection_price=str(price))
    elif kind in ('EXP_VSS','EXP_LDR'):
        require(at.strftime('%H:%M')=='15:15','closing_selection_window')
        bars=core._bars(row['bars'],symbol,at)
        require(bars[-1][0]==at.replace(second=0,microsecond=0)-timedelta(minutes=1),'selection_latest_minute_missing')
        price=bars[-1][4];day_return=price/features['previous_close']-1
        if kind=='EXP_VSS':
            volume=market.get('day_volume')
            require(type(volume) is int and volume>=0 and features['mean_volume20']>0,'vss_actual_day_volume_unverified')
            multiple=Decimal(volume)/features['mean_volume20']
            require(multiple>=3 and Decimal(0)<=day_return<=Decimal('.08'),'vss_volume_or_return')
            require(type(market.get('prior_limit_up')) is bool and not market['prior_limit_up'],'vss_prior_limit_up_unverified_or_true')
            selection.update(score=str(multiple),volume_multiple=str(multiple),day_return=str(day_return),
                hold_sessions=5 if int(core._digest(['vss-hold-v1',at.strftime('%Y%m%d'),symbol]),16)%2==0 else 20)
        else:
            target=bars[-1][0]-timedelta(minutes=60)
            prior=next((r for r in bars if r[0]==target),None)
            require(prior is not None,'ldr_exact_60minute_price_missing')
            ret=price/prior[4]-1
            require(ret<=Decimal('-.025') and day_return>Decimal('-.10'),'ldr_return_range')
            require(type(market.get('dart_bad_news')) is bool and not market['dart_bad_news']
                and core._sha(market.get('dart_source_sha256')),'ldr_disclosures_unverified_or_adverse')
            # The observed DART list worker runs every fifteen minutes. This
            # bound is independent of the 5s price / 30s VI entry freshness.
            _fresh(market['dart_observed_at'],at,1200)
            selection.update(score=str(-ret),return60=str(ret),day_return=str(day_return),
                disclosure_source_sha256=market['dart_source_sha256'])
    else:raise ValueError('selection_kind_invalid')
    return selection


def select_candidates(kind,candidates,*,day,selected_at,held_symbols=(),existing=None,mode='paper'):
    if mode!='paper':return None
    at=core._time(selected_at);require(at.strftime('%Y%m%d')==day,'selection_day_invalid')
    require(kind in ('ORB','EXP_GDR','EXP_VSS','EXP_LDR'),'selection_kind_invalid')
    if existing is not None:
        require(existing.get('selection_sha256')==core._digest({k:v for k,v in existing.items() if k not in ('selection_sha256','assignments')})
            and existing.get('kind')==kind and existing.get('day')==day
            and core._time(existing['selected_at'])<=at,'selection_changed')
        return deepcopy(existing)
    require(isinstance(candidates,(list,tuple)) and len(candidates)<=2000,'candidate_limit')
    selected=[];rejected=[];seen=set();held=set(held_symbols)
    for row in candidates:
        symbol=row.get('symbol') if isinstance(row,dict) else None
        try:
            require(symbol not in seen,'duplicate_candidate');seen.add(symbol)
            require(symbol not in held,'already_owned')
            selected.append(_candidate(kind,row,at))
        except (ValueError,KeyError,TypeError,AttributeError,ArithmeticError) as error:
            failure={'symbol':symbol,'reason':str(error) if isinstance(error,ValueError) else 'candidate_evidence_unverified'}
            if kind=='ORB' and isinstance(row,dict):
                history=(row.get('context') or {}).get('same_window_history')
                failure['historical_session_count']=history.get('observed_sessions') if isinstance(history,dict) else 0
                failure['historical_expected_sessions']=14
                failure['history_missing_sessions']=history.get('missing_sessions') if isinstance(history,dict) else None
            rejected.append(failure)
    if kind=='EXP_GDR' and selected:
        gaps=[Decimal(r['gap']) for r in selected];mom=[Decimal(r['momentum20']) for r in selected]
        def zs(values):
            mean=sum(values)/len(values);sd=(sum((x-mean)**2 for x in values)/len(values)).sqrt()
            return [(x-mean)/sd if sd else Decimal(0) for x in values]
        for row,g,m in zip(selected,zs(gaps),zs(mom)):row['score']=str(-g-m)
    selected.sort(key=lambda row:(-Decimal(row['score']),core._digest(['extended-selection-tie-v1',day,row['symbol']])))
    # Selection cap is independent of the account-wide atomic 20 overnight/8 VSS guards.
    chosen=selected[:5]
    base={'schema':'paper-experiment-extended-selection-v1','kind':kind,'day':day,'selected_at':at.isoformat(),
        'pool':selected,'selected_symbols':[r['symbol'] for r in chosen],'rejected':rejected,'max_symbols':5}
    sha=core._digest(base);base['selection_sha256']=sha;base['assignments']=[]
    for row in chosen:
        arm=('EXP_ORB_A' if int(core._digest(['orb-variant-v1',day,row['symbol']]),16)%2==0 else 'EXP_ORB_B') if kind=='ORB' else kind
        assignment={'schema':SCHEMA,'day':day,'symbol':row['symbol'],'arm':arm,'first_seen_at':row['first_seen_at'],
            'assigned_at':at.isoformat(),'selection_sha256':sha,'selection_kind':kind,
            'selection_features':deepcopy(row),'selection_snapshot':deepcopy({k:v for k,v in base.items() if k not in ('selection_sha256','assignments')})}
        assignment['assignment_sha256']=core._digest(assignment);base['assignments'].append(assignment)
    return base


def validate_assignment(value,*,symbol,day,now):
    now=core._time(now)
    require(isinstance(value,dict) and value.get('schema')==SCHEMA and value.get('symbol')==symbol
        and value.get('day')==day and value.get('arm') in ARMS
        and value.get('assignment_sha256')==core._digest({k:v for k,v in value.items() if k!='assignment_sha256'}),
        'extended_assignment_invalid')
    require(core._time(value['first_seen_at']).strftime('%Y%m%d')==day
        and core._time(value['assigned_at']).strftime('%Y%m%d')==day,'extended_assignment_day_invalid')
    if 'theme_context' in value:
        from .paper_experiment_theme import validate_theme_context
        validate_theme_context(value['theme_context'],symbol=symbol,as_of=core._time(value['assigned_at']))
    if value['arm']=='EXP_ETF_B':
        expected=etf_assignment(day,symbol,value['first_seen_at'],core._time(value['assigned_at']))
        require(core._without_theme(value)==expected and core._time(value['assigned_at'])<=now,'etf_assignment_changed')
        return value
    snapshot=value['selection_snapshot'];features=value['selection_features']
    require(core._digest(snapshot)==value['selection_sha256'] and snapshot['kind']==value['selection_kind']
        and snapshot['day']==day and snapshot['selected_at']==value['assigned_at']
        and core._time(value['first_seen_at'])<=core._time(value['assigned_at'])<=now
        and symbol in snapshot['selected_symbols'] and features in snapshot['pool'] and features['symbol']==symbol,
        'extended_selection_binding_invalid')
    kind=snapshot['kind'];arm=kind
    if kind=='ORB':arm='EXP_ORB_A' if int(core._digest(['orb-variant-v1',day,symbol]),16)%2==0 else 'EXP_ORB_B'
    require(value['arm']==arm and snapshot['selected_symbols']==[r['symbol'] for r in snapshot['pool'][:5]],
        'extended_selection_changed')
    return value


def etf_assignment(day,symbol,first_seen_at,now,*,mode='paper'):
    if mode!='paper':return None
    first=core._time(first_seen_at);at=core._time(now)
    require(symbol in ETF_SYMBOLS and first.strftime('%Y%m%d')==day
        and at.strftime('%Y%m%d')==day and first<=at,'etf_assignment_invalid')
    result={'schema':SCHEMA,'day':day,'symbol':symbol,'arm':'EXP_ETF_B',
        'first_seen_at':first.isoformat(),'assigned_at':at.isoformat(),
        'selection_kind':'EXP_ETF_B','selection_sha256':core._digest(['etf-independent-pair-v1',day,symbol]),
        'selection_features':{'definition_version':'etf_b_same_formula_v1','independent_of_stock_allocation':True}}
    result['assignment_sha256']=core._digest(result);return result


def resolve_collisions(day,eligible_by_symbol,*,held_symbols=(),existing=None,mode='paper'):
    if mode!='paper':return None
    core._day(day)
    if existing is not None:
        require(existing.get('sha256')==core._digest({k:v for k,v in existing.items() if k!='sha256'})
            and existing.get('day')==day,'collision_choice_changed');return deepcopy(existing)
    result={'schema':'paper-experiment-collision-v1','day':day,'selected':{},'priority':{},'held_excluded':[]}
    for symbol,arms in sorted(eligible_by_symbol.items()):
        core._symbol(symbol);require(isinstance(arms,(list,tuple)) and arms and len(set(arms))==len(arms)
            and set(arms)<=(ARMS|core.ARMS),'collision_arms_invalid')
        if symbol in held_symbols:result['held_excluded'].append(symbol);continue
        ordered=sorted(arms,key=lambda arm:core._digest(['collision-v1',day,symbol,arm]))
        result['selected'][symbol]=ordered[0];result['priority'][symbol]=ordered
    result['sha256']=core._digest(result);return result


def evaluate_entry(arm,bars,*,symbol,now,selection=None,context=None,mode='paper'):
    if mode!='paper':return _out(None,None,None,'paper_only')
    try:
        now=core._time(now);core._symbol(symbol);context=context or {};minute=now.replace(second=0,microsecond=0)
        require(arm in ARMS,'unknown_extended_arm')
        if arm=='EXP_ETF_B':
            require(symbol in ETF_SYMBOLS,'etf_pair_only')
            validate_assignment(selection,symbol=symbol,day=now.strftime('%Y%m%d'),now=now)
            rows=core._bars(bars,symbol,now);require(rows[-1][0]+timedelta(minutes=1)==minute,'latest_completed_minute_missing')
            features=core._features(rows,10,30)
            checks=core._bar_entry_checks('EXP_B',rows,minute,features,{})
            checks['intraday_entry_window']='09:00'<=now.strftime('%H:%M')<'15:00'
            return _out(arm,symbol,now,'etf_b_signal' if all(checks.values()) else 'etf_b_conditions',
                signal=all(checks.values()),action='buy',signal_at=minute.isoformat(),
                order_window_start=minute.isoformat(),order_window_end=(minute+timedelta(minutes=1)).isoformat(),
                signal_price=int(rows[-1][4]),signal_price_basis='completed_signal_bar_close',features=features,checks=checks)
        validate_assignment(selection,symbol=symbol,day=now.strftime('%Y%m%d'),now=now)
        require(selection['arm']==arm,'extended_arm_not_assigned')
        features=selection['selection_features'];common=common_checks(context,symbol=symbol,now=now)
        checks={}
        hm=now.strftime('%H:%M');start=end=None;signal_at=now.isoformat();price=None;basis=None
        if arm.startswith('EXP_ORB_'):
            rows=core._bars(bars,symbol,now);require(rows[-1][0]+timedelta(minutes=1)==minute,'latest_completed_minute_missing')
            high=Decimal(features['opening_range']['including_opening_auction']['high'])
            checks.update(orb_entry_window='09:06'<=hm<='10:30',
                completed_close_above_high_plus_tick=rows[-1][4]>high+features['tick_size'])
            start=minute;end=minute+timedelta(minutes=1);price=int(rows[-1][4]);basis='completed_signal_bar_close'
            signal_at=minute.isoformat()
        elif arm=='EXP_GDR':
            fallback=features['definition_version']=='observed_0900_open_fallback_v0'
            checks['opening_window']=hm=='09:00' if fallback else hm=='08:59'
            start=minute.replace(hour=9,minute=0) if fallback else minute.replace(hour=8,minute=59)
            end=start+timedelta(minutes=1);price=int(Decimal(features['selection_price']))
            basis='official_open_fallback_not_auction_fill' if fallback else 'observed_indicative_price_not_fill'
        else:
            checks['closing_auction_window']='15:20'<=hm<'15:30'
            start=minute.replace(hour=15,minute=20);end=minute.replace(hour=15,minute=30)
        good=all(checks.values());executable=all(common.values())
        # A real technical signal remains a research observation when a fresh
        # executable-entry check blocks it. The monitor must record, not submit,
        # this explicit unfillable result; missing technical evidence never
        # becomes a signal merely because VI/price/classification was blocked.
        reason=('extended_entry_signal' if executable else 'entry_not_executable') if good else 'extended_entry_conditions'
        return _out(arm,symbol,now,reason,signal=good,action='buy',
            signal_at=signal_at,dedup_at=selection['assigned_at'],selection_at=selection['assigned_at'],
            order_window_start=start.isoformat(),order_window_end=end.isoformat(),signal_price=price,signal_price_basis=basis,
            features=deepcopy(features),checks={**common,**checks},technical_checks=checks,
            execution_status='unfillable_observed' if good and not executable else 'eligible' if good else 'no_signal',
            selection_sha256=selection['selection_sha256'],
            auction_intent=arm in OVERNIGHT and not (arm=='EXP_GDR' and hm=='09:00'))
    except (ValueError,KeyError,TypeError,AttributeError,ArithmeticError) as error:
        return _out(arm,symbol,now,'extended_entry_unverified',detail=str(error) if isinstance(error,ValueError) else 'input_unverified')


def _calendar(context,now,entry):
    value=context['calendar'];require(value.get('verified') is True and core._sha(value.get('source_sha256'))
        and core._time(value['available_at'])<=now,'calendar_unverified')
    days=value['trading_days'];require(isinstance(days,list) and len(days)<=400 and days==sorted(set(days))
        and all(core._day(d) for d in days),'calendar_days_invalid')
    require(entry.strftime('%Y%m%d') in days and now.strftime('%Y%m%d') in days,'calendar_session_missing')
    return days


def evaluate_exit(arm,bars,*,symbol,now,position,selection=None,context=None,mode='paper'):
    if mode!='paper':return _out(None,None,None,'paper_only')
    try:
        now=core._time(now);context=context or {};entry=core._time(position['entry_at'])
        require(arm in ARMS and position.get('arm')==arm and position.get('symbol')==symbol
            and type(position.get('quantity')) is int and position['quantity']>0 and entry<=now,'ownership_unverified')
        basis=position.get('entry_basis_krw')
        require(basis is None or type(basis) is int and basis>0,'entry_basis_invalid')
        cost=Decimal(basis)/position['quantity'] if type(basis) is int and basis>0 else _positive(position['entry_price'])
        hm=now.strftime('%H:%M');minute=now.replace(second=0,microsecond=0);reason=None;update={};end=None
        if arm=='EXP_ETF_B':
            require(symbol in ETF_SYMBOLS,'etf_pair_only')
            validate_assignment(selection,symbol=symbol,day=entry.strftime('%Y%m%d'),now=now)
            adapted={**position,'arm':'EXP_B'}
            result=core.evaluate_exit('EXP_B',bars,symbol=symbol,now=now,position=adapted)
            return _out(arm,symbol,now,result['reason'],**{k:v for k,v in result.items()
                if k not in ('schema','arm','symbol','evaluated_at','available_at','reason','signal_key','permission_to_trade')})
        require(selection is not None,'owned_selection_missing')
        validate_assignment(selection,symbol=symbol,day=entry.strftime('%Y%m%d'),now=now)
        require(selection['arm']==arm,'owned_selection_arm_mismatch')
        features=selection['selection_features']
        if arm.startswith('EXP_ORB_'):
            if '15:20'<=hm<'15:30':reason='orb_closing_auction';end=minute.replace(hour=15,minute=30)
            elif hm<'15:20':
                rows=core._bars(bars,symbol,now);require(rows[-1][0]+timedelta(minutes=1)==minute,'latest_completed_minute_missing')
                atr=Decimal(features['features']['atr14']);low=Decimal(features['opening_range']['including_opening_auction']['low'])
                stop=cost-atr*Decimal('.10') if arm=='EXP_ORB_A' else max(cost-atr*Decimal('.25'),low)
                if rows[-1][4]<=stop:reason='orb_atr_close_stop';end=minute+timedelta(minutes=1)
                update={'stop_price':str(stop),'stop_basis':'completed_minute_close'}
        else:
            days=_calendar(context,now,entry);entry_index=days.index(entry.strftime('%Y%m%d'));today=now.strftime('%Y%m%d')
            next_day=days[entry_index+1] if len(days)>entry_index+1 else None
            if arm in ('EXP_GDR','EXP_LDR'):
                if today==next_day and hm=='08:59':reason='next_session_opening_auction';end=minute.replace(hour=9,minute=0)
                elif arm=='EXP_GDR' and entry.date()==now.date() and '15:20'<=hm<'15:30':
                    rows=core._bars(bars,symbol,now)
                    last=next((r for r in rows if r[0].strftime('%H:%M')=='15:19'),None)
                    require(last is not None,'preauction_1519_completed_price_missing')
                    market=context['market'];require(market.get('symbol')==symbol and market.get('mode')=='paper'
                        and core._sha(market.get('source_sha256')),'opening_source_unverified')
                    _fresh(market['observed_at'],now,5)
                    open_price=_positive(context['market']['day_open'])
                    if last[4]<open_price*Decimal('.98'):reason='gdr_preauction_close_below_open_2pct_v1';end=minute.replace(hour=15,minute=30)
                    update={'day_close_basis':'1519_completed_preauction_close_v1_not_final_1530'}
            else:
                held=days.index(today)-entry_index;target=features['hold_sessions'];require(target in (5,20),'vss_hold_sessions_invalid')
                trigger=position.get('vss_stop_trigger_day')
                close=context.get('day_close')
                if close is not None:
                    closed=core._day(close['date']);available=core._time(close['available_at'])
                    require(core._sha(close.get('source_sha256')) and entry.date()<=closed.date()<=now.date()
                        and available>=closed.replace(hour=15,minute=30) and available<=now,'vss_final_close_unverified')
                    if _positive(close['close'])<=cost*Decimal('.92') and (trigger is None or close['date']<trigger):
                        trigger=close['date'];update={'vss_stop_trigger_day':trigger,'vss_stop_source_sha256':close['source_sha256']}
                stop_due=False
                if trigger is not None:
                    require(trigger in days and trigger>=entry.strftime('%Y%m%d'),'vss_stop_day_invalid')
                    stop_due=days.index(today)>days.index(trigger)
                if '15:20'<=hm<'15:30' and (held>=target or stop_due):
                    reason='vss_maturity_closing_auction' if held>=target else 'vss_prior_close_stop_next_closing_auction'
                    end=minute.replace(hour=15,minute=30)
                update={**update,'held_trading_sessions':held,'assigned_hold_sessions':target}
        return _out(arm,symbol,now,reason or 'hold',signal=reason is not None,action='sell',
            signal_at=minute.isoformat(),order_window_start=minute.isoformat(),
            order_window_end=end.isoformat() if end else None,position_update=update)
    except (ValueError,KeyError,TypeError,AttributeError,ArithmeticError) as error:
        return _out(arm,symbol,now,'extended_exit_unverified',detail=str(error) if isinstance(error,ValueError) else 'input_unverified')
