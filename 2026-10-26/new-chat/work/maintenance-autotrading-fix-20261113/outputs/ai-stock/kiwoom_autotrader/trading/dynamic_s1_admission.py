"""Pure, bounded dynamic S1 observation contract; never an order permission.

The receiver must also match this immutable evidence to its current in-memory
gate and authenticated session immediately before transport. Historical reads
validate at their original evaluation time, never refresh an old observation.
"""
from copy import deepcopy
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re

from kiwoom_autotrader.api.session import KST

SCHEMA = 'paper-dynamic-s1-admission-v1'
TRACK_SCHEMA = 'paper-dynamic-s1-admission-v2'
FIELDS = {'schema_version','mode','symbol','session_sha256','minute','admitted_at',
    'verified_at','provider_binding','total_budget_krw','initial_entry_pct',
    'initial_budget_krw','candidate','precision_book','admission_sha256'}
CANDIDATE_FIELDS = {'name','change_pct','surge_rate_pct','turnover_krw','available_at',
                    'source_hashes','membership_evidence','liquidity_evidence'}
TRACK_CANDIDATE_FIELDS=CANDIDATE_FIELDS|{'track','current_price','toss_evidence','toss_session_day','toss_previous_session_day','precursor_evidence'}


def validate_track_candidate(candidate, *, symbol, now):
    """V2 watch proof preserves its actual independent source and track."""
    from .candidate_tracks import track_for_change, T1
    from .dynamic_candidates import _normal_member
    from .toss_candidate_feed import validate_candidate, toss_membership_evidence
    if not isinstance(candidate,dict) or set(candidate)!=TRACK_CANDIDATE_FIELDS:raise ValueError('track_candidate')
    track=track_for_change(candidate['change_pct'])
    if track is None or candidate['track']!=track:raise ValueError('track_candidate')
    if type(candidate['current_price']) is not int or not 0<candidate['current_price']<=500_000:raise ValueError('track_one_share_budget')
    member=candidate['membership_evidence'];hashes=candidate['source_hashes'];toss=candidate['toss_evidence']
    if not isinstance(member,dict) or not isinstance(hashes,dict) or not hashes or not all(_hash(v) for v in hashes.values()):raise ValueError('track_source')
    if isinstance(toss,dict):
        checked=validate_candidate(toss,now=now,session_day=candidate['toss_session_day'],previous_session_day=candidate['toss_previous_session_day'])
        if (checked['symbol']!=symbol or checked['name']!=candidate['name']
                or checked['current_price']!=candidate['current_price']
                or Decimal(checked['change_pct'])!=Decimal(candidate['change_pct'])
                or not _hash(checked.get('feed_sha256')) or checked['feed_sha256']!=hashes.get('toss_feed')
                or candidate['precursor_evidence']!=checked.get('precursor_evidence')):raise ValueError('track_source_conflict')
        if member.get('source')=='toss_stocks':
            if member!=toss_membership_evidence(checked,now=now):raise ValueError('track_membership_conflict')
        elif (member.get('source')!='ka10099' or _normal_member({**member,'name':candidate['name']},member.get('marketCode')) is not None
                or member.get('marketCode')!=('10' if checked['market']=='KOSDAQ' else '0')):raise ValueError('track_membership_conflict')
        if {'surge','turnover'}<=set(hashes):
            legacy=dict(candidate,source_hashes={k:hashes[k] for k in ('surge','turnover','membership')})
            _validate_legacy_candidate_source(legacy,now=now,available=_time(candidate['available_at']))
        else:
            liquidity=candidate['liquidity_evidence']
            if (set(hashes)!={'toss_feed','membership'} or not isinstance(liquidity,dict)
                    or liquidity.get('basis')!='Toss_KRX_NXT_rank_turnover' or liquidity.get('turnover_unit')!='KRW'
                    or liquidity.get('turnover_krw')!=checked['turnover_krw']
                    or candidate['turnover_krw']!=checked['turnover_krw']
                    or liquidity.get('same_time_rvol20') is not None):raise ValueError('track_liquidity')
    else:
        if track==T1 or any(candidate[k] is not None for k in ('toss_session_day','toss_previous_session_day','precursor_evidence')):raise ValueError('track_precursor_missing')
        if _decimal(candidate['surge_rate_pct'])<=0:raise ValueError('track_liquidity')
        _validate_legacy_candidate_source(candidate,now=now,available=_time(candidate['available_at']))
    member_at=_time(member['available_at']);available=_time(candidate['available_at'])
    if (member.get('source_sha256')!=hashes.get('membership') or member_at.date()!=now.date()
            or not member_at<=available<=now or now-member_at>timedelta(seconds=300)
            or type(candidate['turnover_krw']) is not int or not 0<candidate['turnover_krw']<10**20):raise ValueError('track_source')
    return candidate


def _canonical(value):
    raw=json.dumps(value,ensure_ascii=True,sort_keys=True,separators=(',',':'),allow_nan=False)
    if len(raw.encode()) > 16384:raise ValueError('dynamic_admission_too_large')
    return raw


def admission_digest(value):
    return hashlib.sha256(_canonical({k:v for k,v in value.items() if k!='admission_sha256'}).encode()).hexdigest()


def session_digest(identity):
    return hashlib.sha256(_canonical(list(identity)).encode()).hexdigest()


def _time(value):
    if not isinstance(value,str) or len(value)>40:raise ValueError('dynamic_admission_time')
    at=datetime.fromisoformat(value)
    if at.tzinfo is None or at.utcoffset()!=timedelta(hours=9):raise ValueError('dynamic_admission_time')
    return at


def _hash(value,size=64):
    return isinstance(value,str) and re.fullmatch('[0-9a-f]{%d}'%size,value) is not None


def _decimal(value):
    if not isinstance(value,str) or re.fullmatch(r'[+-]?[0-9]{1,12}(?:\.[0-9]{1,6})?',value) is None:
        raise ValueError('dynamic_admission_ratio')
    result=Decimal(value)
    if not result.is_finite():raise ValueError('dynamic_admission_ratio')
    return result


def validate_dynamic_admission(value, *, now, symbol=None, session_sha256=None):
    """Validate fixed schema, observed eligibility, same minute and strict5 book."""
    from kiwoom_autotrader.api.order_book import observed_book_is_fresh
    try:
        if (not isinstance(value,dict) or set(value)!=FIELDS or value['schema_version'] not in (SCHEMA,TRACK_SCHEMA)
                or value['mode']!='paper' or not isinstance(now,datetime) or now.tzinfo is None
                or now.utcoffset() is None or not isinstance(value['symbol'],str)
                or re.fullmatch('[0-9]{6}',value['symbol']) is None
                or symbol is not None and value['symbol']!=symbol
                or not _hash(value['session_sha256']) or not _hash(value['provider_binding'],32)
                or session_sha256 is not None and value['session_sha256']!=session_sha256
                or not _hash(value['admission_sha256']) or admission_digest(value)!=value['admission_sha256']):
            raise ValueError
        now=now.astimezone(KST); minute=_time(value['minute'])
        admitted,verified=(_time(value[k]) for k in ('admitted_at','verified_at'))
        if (minute!=now.replace(second=0,microsecond=0) or not minute<=admitted<=verified<=now
                or verified.replace(second=0,microsecond=0)!=minute
                or not 0<=(now-verified).total_seconds()<=5 or now.second>=45
                or any(type(value[k]) is not int or value[k]!=n for k,n in
                       (('total_budget_krw',1000000),('initial_entry_pct',50),('initial_budget_krw',500000)))):
            raise ValueError
        candidate=value['candidate']
        tracked=value['schema_version']==TRACK_SCHEMA
        if tracked:
            validate_track_candidate(candidate,symbol=value['symbol'],now=now)
        elif (not isinstance(candidate,dict) or set(candidate)!=CANDIDATE_FIELDS
                or not 4<=_decimal(candidate['change_pct'])<=28
                or _decimal(candidate['surge_rate_pct'])<=0
                or type(candidate['turnover_krw']) is not int or not 0<candidate['turnover_krw']<10**20):
            raise ValueError
        available=_time(candidate['available_at'])
        if not minute<=available<=admitted:raise ValueError
        if not tracked:
            _validate_legacy_candidate_source(candidate,now=now,available=available)
        book=value['precision_book']
        if (not isinstance(book,dict) or book.get('symbol')!=value['symbol']
                or book.get('session_binding')!=value['provider_binding']
                or not observed_book_is_fresh(book,now)
                or _time(book['available_at'])>verified):raise ValueError
        return deepcopy(value)
    except (ValueError,KeyError,TypeError,OverflowError,InvalidOperation,RecursionError):
        raise ValueError('dynamic_s1_admission_unverified') from None


def _validate_legacy_candidate_source(candidate, *, now, available):
    from .dynamic_candidates import _normal_member
    hashes=candidate['source_hashes']
    if (not isinstance(hashes,dict) or set(hashes)!={'surge','turnover','membership'}
            or not all(_hash(h) for h in hashes.values())):raise ValueError
    member=candidate['membership_evidence']
    member_keys={'marketCode','auditInfo','orderWarning','state','upSizeName','source',
                 'available_at','source_sha256','instrument_filter'}
    if (not isinstance(member,dict) or set(member)!=member_keys or member['source']!='ka10099'
            or member['source_sha256']!=hashes['membership']
            or member['instrument_filter']!='ka10023_stk_cnd_20'
            or _normal_member({**member,'name':candidate['name']},member['marketCode']) is not None):
        raise ValueError
    member_at=_time(member['available_at'])
    if (member_at.date()!=now.date() or not member_at<=available
            or not 0<=(now-member_at).total_seconds()<=300):raise ValueError
    liquidity=candidate['liquidity_evidence']
    if (not isinstance(liquidity,dict) or set(liquidity)!={'surge_now_trde_qty','turnover_now_trde_qty',
            'turnover_change_pct','volume_unit','turnover_unit_source','basis','same_time_rvol20'}
            or any(type(liquidity[k]) is not int or not 0<liquidity[k]<10**20
                   for k in ('surge_now_trde_qty','turnover_now_trde_qty'))
            or _decimal(liquidity['turnover_change_pct'])<=0
            or liquidity['volume_unit']!='shares' or liquidity['turnover_unit_source']!='million_krw'
            or liquidity['basis']!='current_day_cumulative' or liquidity['same_time_rvol20'] is not None):
        raise ValueError

def validate_dynamic_s1_policy(settings, config, symbol):
    """Current paper options must still equal the frozen S1/B3 configuration."""
    from kiwoom_autotrader.config.settings import (effective_paper_funnel,
        effective_cross_guards,effective_b3_guards,effective_entry_time_window)
    from kiwoom_autotrader.trading.paper_policy import _multi_enabled,_symbol_cap,_buy_cap
    try:
        initial,profit=effective_b3_guards(settings,'paper')
        spread,cooldown=effective_cross_guards(settings,'paper')
        start,end=effective_entry_time_window(settings,'paper')
        stocks=[{'code':row['code'],'budget':row.get('paper_budget',row['budget'])}
                for row in settings['gdcrs_stocks']]
        expected={'enabled':settings.get('gdcrs_enabled'),
            'short_period':settings['gdcrs_short_period'],'long_period':settings['gdcrs_long_period'],
            'market_start_time':settings.get('market_start_time','09:00'),
            'market_end_time':settings.get('market_end_time','15:30'),
            'entry_start_time':start,'entry_end_time':end,'stocks':stocks,
            'min_cross_bps':spread,'reentry_cooldown_minutes':cooldown,
            'max_daily_entries_per_symbol':settings.get('gdcrs_max_daily_entries_per_symbol',0),
            'b3_initial_entry_pct':initial,'b3_add_on_profit_bps':profit,'b3_total_budget_krw':1000000}
        if isinstance(config,dict) and config.get('b3_stage')=='add':
            # Existing owned additions are not initial entry-window signals.
            expected.pop('entry_start_time');expected.pop('entry_end_time')
        if (not effective_paper_funnel(settings,'paper')['enabled'] or not _multi_enabled(settings)
                or initial!=50 or profit<50 or spread<10 or cooldown<20
                or settings.get('gdcrs_max_daily_entries_per_symbol')!=2 or settings.get('gdcrs_enabled') is not True
                or _symbol_cap(settings)!=1000000 or _buy_cap(settings)>1000000
                or not isinstance(config,dict) or any(config.get(k)!=v for k,v in expected.items())
                or any(row['code']==symbol for row in stocks)):
            raise ValueError
    except (ValueError,TypeError,KeyError,AttributeError):
        raise ValueError('dynamic_s1_policy_changed') from None
