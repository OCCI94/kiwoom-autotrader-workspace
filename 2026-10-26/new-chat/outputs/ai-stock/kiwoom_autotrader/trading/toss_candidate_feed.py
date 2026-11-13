"""Bounded local Toss watch feed. No transport, credentials or order authority.

Failure is an immediate empty feed; the receiver's Kiwoom path is independent.
The producer supplies a screening observation, never bars/quotes for execution.
"""
from __future__ import annotations
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
import re
import stat
from zoneinfo import ZoneInfo

KST = ZoneInfo('Asia/Seoul')
SCHEMA = 'toss-hybrid-watch-feed-v1'
MAX_BYTES = 64 * 1024
TTL_SECONDS = 75
RANKING_MAX_AGE_SECONDS = 300
MAX_ROWS = 15
SOURCE_KEYS = ('price','ranking','stocks','candles','short_selling','investor_trading','calendar','risk_review')


def _time(value):
    at = value if isinstance(value,datetime) else datetime.fromisoformat(value)
    if at.tzinfo is None or at.utcoffset() is None: raise ValueError('aware_time_required')
    return at.astimezone(KST)


def _decimal(value):
    if isinstance(value,bool) or not isinstance(value,(str,int,Decimal)): raise ValueError('number')
    out=Decimal(value)
    if not out.is_finite(): raise ValueError('number')
    return out


def _unique(pairs):
    out={}
    for key,value in pairs:
        if key in out: raise ValueError('duplicate_field')
        out[key]=value
    return out


def validate_candidate(row, *, now, session_day, previous_session_day):
    """Recompute all numeric screening gates; presence of flags is insufficient."""
    now=_time(now)
    if not isinstance(row,dict): raise ValueError('row')
    code=row.get('symbol'); name=row.get('name')
    if (not isinstance(code,str) or re.fullmatch('[0-9]{6}',code) is None
            or not isinstance(name,str) or not name.strip() or len(name)>100
            or any(ord(c)<32 or ord(c)==127 for c in name)
            or row.get('currency')!='KRW' or row.get('security_type')!='STOCK'
            or row.get('market') not in ('KOSPI','KOSDAQ') or row.get('common_share') is not True
            or row.get('listing_active') is not True or row.get('krx_suspended') is not False
            or row.get('liquidation_trading') is not False
            or row.get('permission_to_trade') is not False): raise ValueError('identity')
    prices=[row.get(k) for k in ('current_price','day_open','base_price')]
    if any(type(v) is not int or v<=0 for v in prices): raise ValueError('price')
    price,opening,base=prices
    change=(Decimal(price)/base-1)*100
    from .candidate_tracks import track_for_change, validate_precursor_evidence, T1
    track=track_for_change(change)
    if not (price>opening and price<=500_000 and track is not None): raise ValueError('price_gate')
    if 'track' in row and row['track']!=track: raise ValueError('track_conflict')
    if abs(change-_decimal(row.get('change_pct'))) > Decimal('0.000001'): raise ValueError('change_conflict')
    stamp=_time(row['available_at']); quote=_time(row['price_timestamp']); ranked=_time(row['ranked_at'])
    if (not quote<=stamp<=now or not ranked<=stamp or now-stamp>timedelta(seconds=TTL_SECONDS)
            or now-quote>timedelta(seconds=TTL_SECONDS) or now-ranked>timedelta(seconds=RANKING_MAX_AGE_SECONDS)
            or any(t.date().isoformat()!=session_day for t in (now,stamp,quote,ranked))): raise ValueError('stale')
    rank=row.get('turnover_rank'); turnover=row.get('turnover_krw')
    if type(rank) is not int or not 1<=rank<=100 or type(turnover) is not int or turnover<=0: raise ValueError('ranking')
    complete=row.get('original_top10_complete')
    if 'of005_ranking_evidence' in row or 'track' in row:
        of=row.get('of005_ranking_evidence')
        if of is None:
            if complete is not False or row.get('of005_top10_rank') is not None or row.get('market_rank') is not None:raise ValueError('of005_unverified')
        else:
            at,received=(_time(of[k]) for k in ('ranked_at','observed_at'))
            actual_rank=of.get('rank')
            if (of.get('schema')!='toss-of005-ranking-evidence-v1' or of.get('symbol')!=code
                    or of.get('rank_type')!='MARKET_TRADING_AMOUNT'
                    or not at<=received<=stamp or now-at>timedelta(seconds=RANKING_MAX_AGE_SECONDS)
                    or now-received>timedelta(seconds=TTL_SECONDS) or at.date().isoformat()!=session_day
                    or at.hour<9 or type(actual_rank) is not int or not 1<=actual_rank<=100
                    or not isinstance(of.get('source_sha256'),str) or re.fullmatch('[0-9a-f]{64}',of['source_sha256']) is None
                    or type(of.get('top10_complete')) is not bool or complete is not of['top10_complete']
                    or row.get('market_rank')!=actual_rank
                    or row.get('of005_top10_rank')!=(actual_rank if complete and actual_rank<=10 else None)):
                raise ValueError('of005_source_conflict')
    elif type(complete) is not bool or row.get('of005_top10_rank') != (rank if rank<=10 and complete else None):raise ValueError('of005_rank')
    if row.get('short_session_day')!=previous_session_day or row.get('daily_baseline_session_day')!=previous_session_day:
        raise ValueError('previous_session')
    short=_decimal(row.get('short_amount_share'))
    ma=_decimal(row.get('ma20')); adr=_decimal(row.get('adr20_pct'))
    if not 0<=short<=Decimal('0.10') or ma<=0 or adr<=0: raise ValueError('short_or_adr')
    multiple=(Decimal(price)/ma-1)*100/adr
    if (multiple>=6 or abs(multiple-_decimal(row.get('adr_extension_multiple')))>Decimal('0.000001')
            or row.get('completed_daily_count')!=20): raise ValueError('adr_gate')
    risk=row.get('risk_review')
    if (not isinstance(risk,dict) or risk.get('coverage_complete') is not True
            or risk.get('unresolved_major_risk') is not False
            or not _time(risk['reviewed_at'])<=stamp<=now<_time(risk['expires_at'])
            or _time(risk['expires_at'])-_time(risk['reviewed_at'])>timedelta(hours=24)):
        raise ValueError('risk_unverified')
    if 'feed_expires_at' in row and not now<_time(row['feed_expires_at']): raise ValueError('feed_expired')
    hashes=row.get('source_hashes')
    if (not isinstance(hashes,dict) or set(hashes)!=set(SOURCE_KEYS)
            or any(not isinstance(v,str) or re.fullmatch('[0-9a-f]{64}',v) is None for v in hashes.values())):
        raise ValueError('provenance')
    if track==T1:
        validate_precursor_evidence(row.get('precursor_evidence'),symbol=code,now=now,
            session_day=session_day,current_price=price,day_open=opening,change_pct=change)
        if _time(row['precursor_evidence']['common_guard']['current_price_at'])!=quote:
            raise ValueError('precursor_price_time_conflict')
    if 'stocks_observed_at' in row or 'stocks_request_started_at' in row:
        begin,received=(_time(row[k]) for k in ('stocks_request_started_at','stocks_observed_at'))
        if (not begin<=received<=stamp or now-received>timedelta(seconds=TTL_SECONDS)
                or received-begin>timedelta(seconds=8) or begin.date().isoformat()!=session_day):
            raise ValueError('stocks_provenance')
    if 'stock_warning_evidence' in row:validate_stock_warning_evidence(row['stock_warning_evidence'],symbol=code,now=now)
    return dict(row)


def validate_stock_warning_evidence(proof, *, symbol, now):
    if not isinstance(proof,dict):raise ValueError('stock_warning_unverified')
    start,observed,expiry=(_time(proof[k]) for k in ('request_started_at','observed_at','expires_at'))
    if (proof.get('schema')!='toss-stock-warnings-v1' or proof.get('symbol')!=symbol
            or proof.get('requested_symbol')!=symbol or proof.get('status')!='clear'
            or proof.get('record_count')!=0 or type(proof.get('record_count')) is not int
            or proof.get('warning_types')!=[] or proof.get('reason_codes')!=[]
            or proof.get('source')!='toss_openapi' or proof.get('permission_to_trade') is not False
            or proof.get('risk_zero_guaranteed') is not False
            or not start<=observed<=now<expiry or expiry-observed>timedelta(minutes=15)
            or observed-start>timedelta(seconds=8) or start.date()!=now.date()
            or not isinstance(proof.get('source_sha256'),str) or re.fullmatch('[0-9a-f]{64}',proof['source_sha256']) is None):
        raise ValueError('stock_warning_unverified')
    return proof


def toss_membership_evidence(row, *, now):
    """Fresh explicit STOCK metadata can fill absence, never a broker conflict."""
    now=_time(now)
    start,received=(_time(row[k]) for k in ('stocks_request_started_at','stocks_observed_at'))
    digest=row['source_hashes']['stocks']
    warning=validate_stock_warning_evidence(row.get('stock_warning_evidence'),symbol=row.get('symbol'),now=now)
    if (not start<=received<=now or now-received>timedelta(seconds=TTL_SECONDS)
            or received-start>timedelta(seconds=8) or start.date()!=now.date()
            or row.get('market') not in ('KOSPI','KOSDAQ') or row.get('security_type')!='STOCK'
            or row.get('common_share') is not True or row.get('listing_active') is not True
            or row.get('krx_suspended') is not False or row.get('liquidation_trading') is not False
            or not isinstance(digest,str) or re.fullmatch('[0-9a-f]{64}',digest) is None):
        raise ValueError('toss_membership_unverified')
    return {'source':'toss_stocks','available_at':received.isoformat(),
        'request_started_at':start.isoformat(),'source_sha256':digest,
        'marketCode':'10' if row['market']=='KOSDAQ' else '0','market':row['market'],
        'security_type':'STOCK','common_share':True,'listing_active':True,
        'krx_suspended':False,'liquidation_trading':False,
        'instrument_filter':'toss_verified_active_common_stock',
        'kiwoom_membership_status':'absent_not_negative_evidence',
        'warning_source_sha256':warning['source_sha256'],'warning_observed_at':warning['observed_at'],
        'warning_expires_at':warning['expires_at']}


def read_toss_candidates(path, now):
    """A nonblocking bounded local-file read; every failure enables fallback."""
    out={'status':'unavailable','reason':'feed_missing_or_invalid','candidates':[],
         'source_sha256':None,'fallback':'KIWOOM_ONLY','permission_to_trade':False}
    try:
        now=_time(now)
        # O_NONBLOCK prevents a malicious/replaced FIFO from blocking the loop.
        fd=os.open(path,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0)|getattr(os,'O_NONBLOCK',0))
        try:
            info=os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_size>MAX_BYTES or info.st_mode&0o022
                    or hasattr(os,'getuid') and info.st_uid!=os.getuid()): raise ValueError('file')
            raw=os.read(fd,MAX_BYTES+1)
        finally: os.close(fd)
        if len(raw)>MAX_BYTES: raise ValueError('bound')
        doc=json.loads(raw,object_pairs_hook=_unique,parse_constant=lambda _:(_ for _ in ()).throw(ValueError('constant')))
        out['source_sha256']=hashlib.sha256(raw).hexdigest()
        if (doc.get('schema')!=SCHEMA or doc.get('read_only') is not True
                or doc.get('permission_to_trade') is not False): raise ValueError('schema')
        if doc.get('status')!='ready':
            out['reason']='feed_'+str(doc.get('status')) if doc.get('status') in ('waiting_market','credentials_missing','circuit_open','error') else 'feed_not_ready'
            return out
        generated,expires=(_time(doc[k]) for k in ('generated_at','expires_at'))
        start,end=(_time(doc[k]) for k in ('regular_start','regular_end'))
        today=doc['session_day']; previous=doc['previous_session_day']
        if (not generated<=now<expires or expires-generated>timedelta(seconds=TTL_SECONDS)
                or not start<=now<end or now.date().isoformat()!=today
                or start.date().isoformat()!=today or end.date().isoformat()!=today
                or not isinstance(previous,str) or datetime.strptime(previous,'%Y-%m-%d').date()>=now.date()):
            raise ValueError('stale_session')
        rows=doc.get('candidates')
        if not isinstance(rows,list) or len(rows)>MAX_ROWS: raise ValueError('rows')
        accepted=[];seen=set()
        for row in rows:
            checked=validate_candidate(row,now=now,session_day=today,previous_session_day=previous)
            if _time(checked['price_timestamp'])<start or _time(checked['ranked_at'])<start:
                raise ValueError('premarket_observation')
            if checked['symbol'] in seen: raise ValueError('duplicate_symbol')
            seen.add(checked['symbol']);accepted.append(checked)
        out.update(status='ready',reason=None,candidates=accepted,generated_at=generated.isoformat(),
                   expires_at=expires.isoformat(),session_day=today,previous_session_day=previous)
    except (OSError,ValueError,TypeError,KeyError,AttributeError,InvalidOperation,OverflowError):
        out.update(status='unavailable',reason='feed_missing_or_invalid',candidates=[])
    return out


def merge_toss_candidates(result, feed, memberships, taxonomy, *, now,
                          configured_symbols=(), funnel=False, limit=30, tiered=False):
    """Reconcile observed classification; absence is not a negative listing.

    Toss-only rows deliberately have no synthetic ka10023 surge ratio. Their
    distinct basis must be explicitly admitted by the monitor; old consumers
    cannot accidentally accept a row as a Kiwoom ranking intersection.
    """
    from copy import deepcopy
    from .dynamic_candidates import _fresh, _normal_member, _same_name
    out=deepcopy(result);now=_time(now)
    out['toss_discovery']={k:deepcopy(v) for k,v in feed.items() if k!='candidates'}
    out['toss_discovery'].update(selected_count=0,exclusions=[])
    if feed.get('status')!='ready' and not tiered: return out
    members={}
    for page in memberships:
        if not _fresh(page,now,age=300): continue
        for code,row in page.get('rows',{}).items():
            members[code]=None if code in members else (row,page)
    found={r['symbol']:r for r in out.get('candidates',[])+out.get('base_candidates',[])}
    for incoming in (feed.get('candidates',[]) if feed.get('status')=='ready' else []):
        code=incoming['symbol']; pair=members.get(code); membership=None; reason=None
        try:
            validate_candidate(incoming,now=now,session_day=feed.get('session_day'),previous_session_day=feed.get('previous_session_day'))
            if code in members:
                reason='membership_conflicting' if pair is None else _normal_member(pair[0],pair[1]['market'])
                if reason is None and not _same_name(incoming['name'],pair[0]['name']): reason='toss_kiwoom_name_conflict'
                if reason is None and incoming['market']!={'0':'KOSPI','10':'KOSDAQ'}.get(pair[1]['market']): reason='toss_kiwoom_market_conflict'
                if reason is None:
                    member,page=pair
                    membership={k:member.get(k) for k in ('marketCode','auditInfo','orderWarning','state','upSizeName')}|{
                        'source':'ka10099','available_at':page['available_at'],'source_sha256':page['source_sha256'],
                        'instrument_filter':'toss_active_common_stock_and_ka10099_normal'}
            else:
                membership=toss_membership_evidence(incoming,now=now)
        except (ValueError,TypeError,KeyError,InvalidOperation,OverflowError):
            reason='toss_membership_or_screen_unverified'
        if reason:
            out['toss_discovery']['exclusions'].append({'symbol':code,'reason':reason});continue
        screen=deepcopy(incoming)|{'feed_sha256':feed['source_sha256'],'feed_expires_at':feed['expires_at']}
        if code in found:
            found[code]['toss_evidence']=screen
            found[code]['source_hashes']['toss_feed']=feed['source_sha256']
            found[code]['candidate_origins']=sorted(set(found[code].get('candidate_origins',['kiwoom_ranking']))|{'toss_readonly_watch'})
        else:
            meta=taxonomy.get(code,{}) if isinstance(taxonomy,dict) else {}
            found[code]={'symbol':code,'name':incoming['name'],'change_pct':incoming['change_pct'],
                'surge_rate_pct':None,'turnover_krw':incoming['turnover_krw'],
                'is_core_leader':meta.get('is_core_leader') is True,
                'sub_themes':[x for x in meta.get('sub_themes',[]) if isinstance(x,str)][:50],
                'available_at':now.isoformat(),'candidate_basis':'toss_hybrid_verified_watch',
                'candidate_origins':['toss_readonly_watch'],'signal_eligible':False,
                'source_hashes':{'toss_feed':feed['source_sha256'],'membership':membership['source_sha256']},
                'membership_evidence':membership,
                'liquidity_evidence':{'basis':'Toss_KRX_NXT_rank_turnover','turnover_unit':'KRW',
                                      'turnover_krw':incoming['turnover_krw'],'same_time_rvol20':None},
                'toss_evidence':screen}
        from .candidate_tracks import track_for_change
        found[code].update(track=track_for_change(incoming['change_pct']),market=incoming['market'],
            change_pct=incoming['change_pct'],candidate_basis='toss_hybrid_verified_watch',
            current_price=incoming['current_price'],screening_pending=[],
            toss_session_day=feed.get('session_day'),toss_previous_session_day=feed.get('previous_session_day'))
        if 'precursor_evidence' in incoming:found[code]['precursor_evidence']=deepcopy(incoming['precursor_evidence'])
        out['toss_discovery']['selected_count']+=1
    rows=sorted(found.values(),key=lambda r:(-int('toss_evidence' in r),-r['turnover_krw'],r['symbol']))
    if tiered:
        from .candidate_tracks import select_candidate_tracks
        selected=select_candidate_tracks(rows,configured_symbols=configured_symbols,limit=min(limit,15))
        out.update(selected)
        if funnel:
            out['schema']='paper-funnel-candidates-v1'
            out['candidates']=out['base_candidates']+out['candidates'];out['base_candidates']=[]
            out['memory_candidate_count']=len(out['candidates'])
    elif funnel:
        out['schema']='paper-funnel-candidates-v1'
        out['candidates']=rows;out['base_candidates']=[];out['memory_candidate_count']=len(rows)
    else:
        base=set(configured_symbols)
        out['base_candidates']=[r for r in rows if r['symbol'] in base]
        out['candidates']=[r for r in rows if r['symbol'] not in base][:limit]
    if out['toss_discovery']['selected_count']:
        out.update(status='ready',reason=None,available_at=now.isoformat(),scope='independent_Kiwoom_and_Toss_watch_sources')
    if tiered:
        out['toss_discovery']['eligible_before_track_allocation_count']=out['toss_discovery']['selected_count']
        out['toss_discovery']['selected_count']=sum('toss_evidence' in r for r in out['candidates']+out['base_candidates'])
    return out
