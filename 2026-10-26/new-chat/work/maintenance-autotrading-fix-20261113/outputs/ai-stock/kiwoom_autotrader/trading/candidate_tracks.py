"""Observed watch-list tracks; selection never grants an order permission.

T1 groups are independent evidence families, not four copies of a rank flag.
Unknown observations stay unknown. Existing execution, account and risk gates
remain mandatory after this pure, bounded in-memory allocation.
"""
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
import re

T1 = 'T1_PRECURSOR'
T2 = 'T2_MOMENTUM'
POLICY = 't1_t2_kosdaq_v1'
RANK_TYPES = frozenset(('MARKET_TRADING_VOLUME','TOSS_SECURITIES_TRADING_AMOUNT','MARKET_TRADING_AMOUNT'))


def _at(value):
    at = value if isinstance(value,datetime) else datetime.fromisoformat(value)
    if at.tzinfo is None or at.utcoffset() is None: raise ValueError('precursor_time')
    return at


def _num(value):
    if isinstance(value,bool) or not isinstance(value,(str,int,Decimal)): raise ValueError('precursor_number')
    try:result=Decimal(value)
    except InvalidOperation:raise ValueError('precursor_number') from None
    if not result.is_finite(): raise ValueError('precursor_number')
    return result


def track_for_change(value):
    change=_num(value)
    return T1 if Decimal('0.5')<=change<3 else T2 if 3<=change<=15 else None


def validate_precursor_evidence(value, *, symbol, now, session_day, current_price, day_open, change_pct):
    """Recompute four family checks from explicit observed facts, never flags.

The source hashes identify saved observations; this is not a transaction
signal. Spread and BUY ratio are usable only when actually supplied, and
program-flow absence is not inferred from foreign/institution daily flow.
"""
    try:
        now=_at(now)
        if (not isinstance(value,dict) or value.get('schema')!='toss-precursor-evidence-v1'
                or value.get('symbol')!=symbol or value.get('session_day')!=session_day
                or track_for_change(change_pct)!=T1): raise ValueError('precursor_identity')
        available=_at(value['available_at'])
        if not available<=now or now-available>timedelta(seconds=75) or available.date().isoformat()!=session_day:
            raise ValueError('precursor_stale')
        guard=value.get('common_guard')
        if not isinstance(guard,dict) or guard.get('basis')!='observed_open_support': raise ValueError('precursor_support')
        previous,current=(_at(guard[k]) for k in ('previous_price_at','current_price_at'))
        if (guard.get('day_open')!=day_open or guard.get('current_price')!=current_price
                or not _num(guard.get('previous_price'))>day_open or not current_price>day_open
                or not previous<current<=available or now-previous>timedelta(seconds=75)
                or previous.date().isoformat()!=session_day): raise ValueError('precursor_support')
        conditions=value.get('conditions')
        if not isinstance(conditions,dict) or set(conditions)!={'a','b','c','d'}: raise ValueError('precursor_groups')
        passed=[]
        for group,condition in conditions.items():
            if not isinstance(condition,dict) or condition.get('status') not in ('pass','fail','unknown'):
                raise ValueError('precursor_group')
            if condition['status']=='unknown': continue
            observed=_at(condition['observed_at']); digest=condition.get('source_sha256'); details=condition.get('details')
            max_age=300 if group=='a' else 75
            local_observed=observed.astimezone(now.tzinfo)
            if (not observed<=available or now-observed>timedelta(seconds=max_age)
                    or observed.date().isoformat()!=session_day or not isinstance(details,dict)
                    or group=='a' and not (9,0)<=(local_observed.hour,local_observed.minute)<(15,30)
                    or not isinstance(digest,str) or re.fullmatch('[0-9a-f]{64}',digest) is None):
                raise ValueError('precursor_source')
            valid=False
            if group=='a':
                valid=(details.get('rank_type') in RANK_TYPES and type(details.get('rank')) is int
                       and 1<=details['rank']<=100)
            elif group=='b':
                if 'ask_total' in details or 'bid_total' in details:
                    ask,bid=(_num(details[k]) for k in ('ask_total','bid_total'))
                    valid=ask>0 and bid>0 and ask>=bid*Decimal('1.15')
                elif 'spread_bps' in details or 'buy_trade_ratio' in details:
                    spread,buy=(_num(details[k]) for k in ('spread_bps','buy_trade_ratio'))
                    valid=0<=spread<=30 and Decimal('0.5')<buy<=1
            elif group=='c':
                # The alternative prior-investor/VDU branch needs its own raw
                # validated formula contract; a convergence boolean is not it.
                if 'program_net_buy_krw' in details:
                    valid=_num(details['program_net_buy_krw'])>0
            else:
                flow=details.get('volume_inflow',{})
                before,after=(_at(flow[k]) for k in ('previous_at','current_at'))
                valid=(isinstance(details.get('theme'),str) and 0<len(details['theme'])<=100
                    and isinstance(details.get('leader_symbol'),str)
                    and re.fullmatch('[0-9]{6}',details['leader_symbol']) is not None
                    and details['leader_symbol']!=symbol and details.get('laggard_symbol')==symbol
                    and _num(details.get('leader_change_pct'))>=5
                    and _num(details.get('laggard_change_pct'))==_num(change_pct)
                    and _num(flow.get('current_volume'))>_num(flow.get('previous_volume'))>=0
                    and before<after<=observed and now-before<=timedelta(seconds=75)
                    and before.date().isoformat()==session_day
                    and isinstance(details.get('taxonomy_sha256'),str)
                    and re.fullmatch('[0-9a-f]{64}',details['taxonomy_sha256']) is not None)
            if (condition['status']=='pass') is not bool(valid): raise ValueError('precursor_flag_conflict')
            if valid: passed.append(group)
        if type(value.get('passed_count')) is not int or value['passed_count']!=len(passed) or len(passed)<2:
            raise ValueError('precursor_insufficient')
        return tuple(passed)
    except (ValueError,TypeError,KeyError,InvalidOperation,OverflowError,AttributeError):
        raise ValueError('precursor_evidence_unverified') from None


def select_candidate_tracks(rows, *, configured_symbols=(), limit=15):
    """Allocate already validated observations: T1<=7,T2<=8,total<=15.

First reserve up to eight eligible KOSDAQ names within both track ceilings,
then fill by observed turnover. Never relax eligibility or track ceilings to
meet a market quota. Configured base names never consume a dynamic slot.
"""
    if type(limit) is not int or limit<0: raise ValueError('candidate_limit')
    limit=min(limit,15); base=set(configured_symbols); unique={}; excluded=[]
    for row in rows:
        code=row.get('symbol') if isinstance(row,dict) else None
        if code in unique: raise ValueError('duplicate_candidate')
        if not isinstance(code,str) or re.fullmatch('[0-9]{6}',code) is None: raise ValueError('candidate_symbol')
        unique[code]=row
    base_rows=[row for code,row in unique.items() if code in base]
    eligible=[]
    for code,row in unique.items():
        if code in base: continue
        try: track=track_for_change(row.get('change_pct'))
        except ValueError: track=None
        if track is None or row.get('track',track)!=track:
            excluded.append({'symbol':code,'reason':'outside_t1_t2_gain_window'});continue
        if type(row.get('current_price')) is not int or not 0<row['current_price']<=500_000:
            excluded.append({'symbol':code,'reason':'initial_half_budget_below_one_share_or_price_unverified'});continue
        if track==T1 and (not isinstance(row.get('precursor_evidence'),dict)
                or type(row['precursor_evidence'].get('passed_count')) is not int
                or row['precursor_evidence']['passed_count']<2):
            excluded.append({'symbol':code,'reason':'t1_precursor_evidence_unavailable'});continue
        market=row.get('market') or {'0':'KOSPI','10':'KOSDAQ'}.get(row.get('membership_evidence',{}).get('marketCode'))
        if market not in ('KOSPI','KOSDAQ') or type(row.get('turnover_krw')) is not int or row['turnover_krw']<=0:
            excluded.append({'symbol':code,'reason':'track_market_or_turnover_unverified'});continue
        eligible.append(dict(row,track=track,market=market))
    eligible.sort(key=lambda row:(-row['turnover_krw'],row['symbol']))
    selected=[]; selected_codes=set(); counts={T1:0,T2:0}; caps={T1:7,T2:8}
    def admit(row):
        track=row['track']
        if len(selected)>=limit or row['symbol'] in selected_codes or counts[track]>=caps[track]: return
        selected.append(row);selected_codes.add(row['symbol']);counts[track]+=1
    for row in eligible:
        if sum(r['market']=='KOSDAQ' for r in selected)>=min(8,limit): break
        if row['market']=='KOSDAQ': admit(row)
    for row in eligible: admit(row)
    for row in eligible:
        if row['symbol'] not in selected_codes: excluded.append({'symbol':row['symbol'],'reason':'dynamic_track_capacity_deferred'})
    return {'candidates':selected,'base_candidates':base_rows,'selection_diagnostics':{
        'policy':POLICY,'dynamic_limit':limit,'track_caps':caps,'selected_track_counts':counts,
        'eligible_count':len(eligible),'eligible_kosdaq_count':sum(r['market']=='KOSDAQ' for r in eligible),
        'selected_kosdaq_count':sum(r['market']=='KOSDAQ' for r in selected),
        'kosdaq_capacity_eligible_count':sum(min(caps[t],sum(r['market']=='KOSDAQ' and r['track']==t for r in eligible)) for t in caps),
        'kosdaq_shortfall_reason':'insufficient_eligible_within_track_caps' if sum(r['market']=='KOSDAQ' for r in selected)<min(8,limit) else None,
        'kosdaq_target':min(8,limit),'forced_ineligible_count':0,'exclusions':excluded}}
