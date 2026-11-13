"""Offline-only OF-001..006 evaluation of caller-supplied paper 0B/0D windows.

No I/O, websocket subscription, dispatch, clock or order interface is provided.
`evaluate_order_flow` consumes a caller-supplied point-in-time observation. It
never treats a PASS as permission to trade. Thresholds labelled
`mock_initial_unvalidated` are the original document's unvalidated mock choices.

Input contract (all times aware ISO8601; KST, same trading date):
  symbol (six digits); mode='paper'; session_id, login_id, generation (nonempty
  opaque provenance strings); as_of; coverage={from, available_at, gap_free,
  source_sha256}; events: records with kind '0B' or '0D', same identity fields,
  symbol, exchange='KRX', source_at, available_at, source_sha256, sequence
  (unique increasing receive order), and raw `values` keyed by documented FIDs.
  calendar={market:'KRX',session_date,is_trading_day:true,available_at,source_sha256}
  is supplied by the caller; a hash alone is not independent authentication.
  0B needs 20(HHmmss),10(price),15(signed individual size),13(cumulative size).
  0D needs 21(HHmmss), bid FIDs 51..60/71..80 and ask 41..50/61..70.
  An actual preceding 0B anchor at/before as_of-90 seconds is required. The
  subsequent cumulative increments must exactly equal each individual trade.
  Coverage is a source-backed continuity assertion, not inferred from event count.
  The optional Toss ranking packet must be independently supplied; no network.

Ambiguous original qualitative conditions (notably 'price support' and exact
stable-range/retention formulas) remain UNKNOWN rather than invented predicates.
"""
from __future__ import annotations
from datetime import datetime, timedelta, time
import re
from zoneinfo import ZoneInfo

KST=ZoneInfo('Asia/Seoul')
PATTERNS=('OF-001','OF-002','OF-003','OF-004','OF-005','OF-006')
_DIGEST=re.compile(r'[0-9a-f]{64}\Z')

class InputUnknown(ValueError):
    pass

def _time(value):
    if not isinstance(value,str): raise InputUnknown('time_missing')
    try: result=datetime.fromisoformat(value)
    except ValueError: raise InputUnknown('time_invalid') from None
    if result.tzinfo is None or result.utcoffset() is None: raise InputUnknown('time_naive')
    return result.astimezone(KST)

def _int(value, *, positive=False, signed_price=False):
    if not isinstance(value,str) or not re.fullmatch(r'[+-]?[0-9]{1,12}',value):
        raise InputUnknown('numeric_missing_or_invalid')
    if value.startswith('-') and not signed_price: raise InputUnknown('negative_quantity')
    n=abs(int(value))
    if positive and not n: raise InputUnknown('zero_quantity_or_price')
    return n

def _raw_time(value, expected):
    if (not isinstance(value,str) or not re.fullmatch(r'[0-9]{6}',value)
            or expected.strftime('%H%M%S') != value or expected.microsecond):
        raise InputUnknown('source_time_mismatch')

def _proof(value):
    return isinstance(value,str) and _DIGEST.fullmatch(value) is not None

def _normalize(packet):
    if not isinstance(packet,dict): raise InputUnknown('packet_missing')
    now=_time(packet.get('as_of'))
    if now.weekday() >= 5 or not time(9)<=now.time().replace(tzinfo=None)<time(15,20):
        raise InputUnknown('krx_continuous_session_unavailable')
    symbol=packet.get('symbol'); identity=tuple(packet.get(k) for k in ('session_id','login_id','generation'))
    if (packet.get('mode')!='paper' or not isinstance(symbol,str) or re.fullmatch(r'[0-9]{6}',symbol) is None
            or any(not isinstance(x,str) or not x or len(x)>200 for x in identity)):
        raise InputUnknown('paper_identity_unavailable')
    calendar=packet.get('calendar')
    if (not isinstance(calendar,dict) or calendar.get('market')!='KRX' or calendar.get('is_trading_day') is not True
            or calendar.get('session_date')!=now.strftime('%Y-%m-%d') or not _proof(calendar.get('source_sha256'))
            or _time(calendar.get('available_at'))>now):
        raise InputUnknown('krx_trading_day_not_proven')
    coverage=packet.get('coverage')
    if (not isinstance(coverage,dict) or coverage.get('gap_free') is not True
            or not _proof(coverage.get('source_sha256')) or _time(coverage.get('available_at'))>now):
        raise InputUnknown('continuity_not_proven')
    coverage_from=_time(coverage.get('from'))
    raw=packet.get('events')
    if not isinstance(raw,list) or len(raw)>100000: raise InputUnknown('events_unavailable')
    records=[];seen=set(); last_seq=-1; last_received=None
    for item in raw:
        if not isinstance(item,dict) or item.get('kind') not in ('0B','0D'):
            raise InputUnknown('event_invalid')
        if (item.get('symbol')!=symbol or item.get('mode')!='paper' or item.get('exchange')!='KRX'
                or tuple(item.get(k) for k in ('session_id','login_id','generation'))!=identity
                or not _proof(item.get('source_sha256'))):
            raise InputUnknown('event_identity_or_provenance_mismatch')
        src=_time(item.get('source_at')); received=_time(item.get('available_at'))
        seq=item.get('sequence')
        if (type(seq) is not int or seq<0 or seq in seen or seq<=last_seq
                or src.date()!=now.date() or received.date()!=now.date()
                or src>received or received>now or last_received is not None and received<last_received):
            raise InputUnknown('event_time_order_or_duplicate')
        seen.add(seq);last_seq=seq;last_received=received
        v=item.get('values')
        if not isinstance(v,dict): raise InputUnknown('fields_missing')
        _raw_time(v.get('20' if item['kind']=='0B' else '21'),src)
        if item['kind']=='0B':
            signed=v.get('15')
            if not isinstance(signed,str) or re.fullmatch(r'[+-][0-9]{1,12}',signed) is None:
                raise InputUnknown('trade_direction_unknown')
            records.append({'kind':'0B','src':src,'received':received,'sequence':seq,
                'price':_int(v.get('10'),positive=True,signed_price=True),'size':_int(signed[1:],positive=True),
                'side':'buy' if signed[0]=='+' else 'sell','cum':_int(v.get('13'),positive=True),
                'source_sha256':item['source_sha256']})
        else:
            bids=[(_int(v.get(str(51+i)),positive=True,signed_price=True),_int(v.get(str(71+i)))) for i in range(10)]
            asks=[(_int(v.get(str(41+i)),positive=True,signed_price=True),_int(v.get(str(61+i)))) for i in range(10)]
            if (any(bids[i][0]<=bids[i+1][0] or asks[i][0]>=asks[i+1][0] for i in range(9))
                    or bids[0][0]>=asks[0][0] or not bids[0][1] or not asks[0][1]):
                raise InputUnknown('book_invalid_or_crossed')
            records.append({'kind':'0D','src':src,'received':received,'sequence':seq,
                            'bids':bids,'asks':asks,'source_sha256':item['source_sha256']})
    trades=[x for x in records if x['kind']=='0B']; books=[x for x in records if x['kind']=='0D']
    if not trades or not books:raise InputUnknown('trade_or_orderbook_missing')
    if any(a['src']>b['src'] for a,b in zip(trades,trades[1:])):raise InputUnknown('trade_source_order_ambiguous')
    if any(a['src']>b['src'] for a,b in zip(books,books[1:])):raise InputUnknown('book_source_order_ambiguous')
    if (now-trades[-1]['src']>timedelta(seconds=5) or now-books[-1]['src']>timedelta(seconds=5)
            or now-trades[-1]['received']>timedelta(seconds=5)
            or now-books[-1]['received']>timedelta(seconds=5)):
        raise InputUnknown('latest_trade_or_book_stale')
    cut=now-timedelta(seconds=90)
    if cut.time().replace(tzinfo=None) < time(9):raise InputUnknown('session_window_incomplete')
    anchors=[x for x in trades if x['src']<=cut]
    if not anchors:raise InputUnknown('pre_window_anchor_missing')
    anchor=anchors[-1]
    if coverage_from>anchor['src'] or anchor['src'].time().replace(tzinfo=None)<time(9):
        raise InputUnknown('anchor_outside_proven_coverage')
    relevant=[x for x in trades if x['sequence']>anchor['sequence']]
    previous=anchor
    for trade in relevant:
        if trade['cum']-previous['cum']!=trade['size']:
            raise InputUnknown('cumulative_trade_discontinuity')
        previous=trade
    # Cutoffs use half-open intervals; event at evaluation time is included.
    windows=[[x for x in relevant if lo<x['src']<=hi] for lo,hi in (
       (now-timedelta(seconds=90),now-timedelta(seconds=60)),
       (now-timedelta(seconds=60),now-timedelta(seconds=30)),
       (now-timedelta(seconds=30),now))]
    return now,symbol,windows,books,identity

def _status(status, reason, metrics=None):
    return {'status':status,'reason':reason,'metrics':metrics or {},'permission_to_trade':False}

def _enough(trades):return len(trades)>=12 and sum(t['size'] for t in trades)>=12

def _ratio(trades):
    buy=sum(x['size'] for x in trades if x['side']=='buy')
    sell=sum(x['size'] for x in trades if x['side']=='sell')
    return buy,sell

def _toss_ranking(packet, now, symbol):
    rank = packet.get('toss_ranking')
    if not isinstance(rank,dict) or rank.get('source')!='TOSS_MARKET_TRADING_AMOUNT_KR_REALTIME':
        return _status('UNKNOWN','toss_ranking_unavailable')
    try:
        sourced=_time(rank.get('ranked_at')); received=_time(rank.get('available_at'))
        if (not _proof(rank.get('source_sha256')) or sourced.date()!=now.date() or received.date()!=now.date()
                or sourced>received or received>now or now-sourced>timedelta(seconds=60)
                or now-received>timedelta(seconds=60)):
            raise InputUnknown('toss_ranking_stale_or_future')
        entries=rank.get('items')
        if not isinstance(entries,list) or len(entries)>100:raise InputUnknown('toss_ranking_incomplete')
        mapping={}
        for row in entries:
            if not isinstance(row,dict):raise InputUnknown('toss_ranking_invalid')
            ordinal=row.get('rank');code=row.get('symbol'); currency=row.get('currency'); amount=row.get('trading_amount')
            if (type(ordinal) is not int or not 1<=ordinal<=100 or ordinal in mapping
                    or not isinstance(code,str) or re.fullmatch(r'[0-9]{6}',code) is None
                    or currency!='KRW' or type(amount) is not int or amount<0):
                raise InputUnknown('toss_ranking_invalid')
            mapping[ordinal]=code
        if not all(k in mapping for k in range(1,11)) or len(set(mapping.values()))!=len(mapping):
            raise InputUnknown('toss_ranking_incomplete')
        if symbol not in [mapping[k] for k in range(1,11)]:
            return _status('FAIL','not_in_toss_original_top10',{'ranked_at':sourced.isoformat()})
        return _status('UNKNOWN','price_support_formula_unspecified',{'ranking_source':'Toss',
                  'exchange_coverage':'unknown','ranked_at':sourced.isoformat()})
    except (InputUnknown,KeyError,TypeError,OverflowError):
        return _status('UNKNOWN','toss_ranking_invalid_or_stale')

def _clause(value):
    return 'UNKNOWN' if value is None else 'PASS' if value else 'FAIL'

def _pullback_clauses(windows):
    past, stable, recent = windows
    if any(not _enough(window) for window in windows):
        return {'samples':'UNKNOWN','decline_0_3pct':'UNKNOWN', 'stable_range_0_4pct':'UNKNOWN',
                'low_break_tolerance_0_1pct':'UNKNOWN','rebound_strict_0_1pct':'UNKNOWN','buy_share_55pct':'UNKNOWN'}
    low=min(t['price'] for t in stable); high=max(t['price'] for t in stable)
    buy,sell=_ratio(recent)
    return {'samples':'PASS',
            # Past decline endpoints, range denominator and the low-break
            # observation rule are unspecified; raw extrema remain visible.
            'decline_0_3pct':'UNKNOWN', 'stable_range_0_4pct':'UNKNOWN',
            'low_break_tolerance_0_1pct':'UNKNOWN',
            'rebound_strict_0_1pct':_clause(recent[-1]['price']*1000 > high*1001),
            'buy_share_55pct':_clause(buy*100 >= (buy+sell)*55),
            'stable_low':low,'stable_high':high,'recent_low':min(t['price'] for t in recent),
            'recent_last':recent[-1]['price'],'buy_qty':buy,'sell_qty':sell}

def _rank_book_clauses(books,recent,now):
    samples=[b for b in books if now-timedelta(seconds=30)<b['src']<=now]
    buy,sell=_ratio(recent)
    share=_clause(buy*100 >= (buy+sell)*55) if _enough(recent) else 'UNKNOWN'
    ratios=[]
    if len(samples)>=3:
        for b in samples:
            bid=sum(q for _,q in b['bids'][:3]);ask=sum(q for _,q in b['asks'][:3])
            ratios.append({'bid_qty':bid,'ask_qty':ask,'status':_clause(bid>=2*ask) if ask else 'UNKNOWN'})
    ratio='UNKNOWN' if len(samples)<3 or any(r['status']=='UNKNOWN' for r in ratios) else (
          'FAIL' if any(r['status']=='FAIL' for r in ratios) else 'PASS')
    return {'three_level_bid_over_ask_2x':ratio, 'buy_share_55pct':share,
            'price_support':'UNKNOWN','spread_0_3pct':'UNKNOWN',
            'spread_reason':'denominator_unspecified','book_samples':ratios,'buy_qty':buy,'sell_qty':sell}

def _wall_clauses(books,windows,now):
    # Report observable candidate components, not continuous market behavior.
    # A later snapshot must retain the same absolute level; rank changes alone
    # are never interpreted as depletion. Same-second ordering is ambiguous.
    trades=[t for window in windows for t in window]; buy,sell=_ratio(windows[-1])
    evidence=[]
    for i,base in enumerate(books):
        if not now-timedelta(seconds=90)<base['src']<=now: continue
        for pos,(price,qty) in enumerate(base['asks'][:3]):
            others=[q for j,(_,q) in enumerate(base['asks'][:3]) if j!=pos]
            if qty<1000 or 2*qty<3*sum(others): continue
            candidate={'wall_price':price,'wall_initial_qty':qty,
                'wall_1000_and_3x_other_mean':'PASS','baseline_at':base['src'].isoformat(),
                'same_price_drop_50pct':'UNKNOWN','same_price_buy_50pct':'UNKNOWN',
                'breakout_0_1pct_observed_2sec':'UNKNOWN','post_break_book_2sec':'UNKNOWN',
                'buy_share_60pct':_clause(buy*100 >= 60*(buy+sell)) if _enough(windows[-1]) else 'UNKNOWN'}
            for later in books[i+1:]:
                if later['src']<=base['src']: continue
                matches=[q for p,q in later['asks'] if p==price]
                if not matches: continue  # outside observed range is unknown
                reduced=matches[0]; candidate['same_price_drop_50pct']=_clause(reduced*2<=qty)
                hits=[t for t in trades if base['src']<t['src']<later['src']
                      and t['price']==price and t['side']=='buy']
                # An empty interval cannot establish complete event ordering.
                ambiguous=any(t['src'] in (base['src'],later['src']) and t['price']==price for t in trades)
                candidate['same_price_buy_50pct']=(_clause(sum(t['size'] for t in hits)*2>=qty)
                                                     if hits and not ambiguous else 'UNKNOWN')
                candidate['same_price_buy_qty']=sum(t['size'] for t in hits)
                after=[t for t in trades if t['src']>later['src']]
                if after:
                    high=[t for t in after if t['price']*1000>=price*1001]
                    if len(high)>=2:
                        first,last=high[0],high[-1]
                        between=[t for t in after if first['src']<=t['src']<=last['src']]
                        candidate['breakout_0_1pct_observed_2sec']=_clause(
                            (last['src']-first['src']).total_seconds()>=2 and
                            all(t['price']*1000>=price*1001 for t in between))
                    # Count only books temporally inside the observed trade
                    # breakout sample.  The original does not define a book
                    # price-support predicate, so overlapping timestamps alone
                    # never establish that quotes stayed above the wall.
                    if len(high)>=2:
                        following=[b for b in books if high[0]['src']<=b['src']<=high[-1]['src']]
                        candidate['post_break_book_overlap_count']=len(following)
                        candidate['post_break_book_overlap_span_seconds']=(
                            (following[-1]['src']-following[0]['src']).total_seconds()
                            if len(following)>=2 else None)
                        candidate['post_break_book_reason']='quote_price_support_definition_unspecified'
                candidate['reduction_at']=later['src'].isoformat()
                break
            evidence.append(candidate)
            if len(evidence)>=50:return evidence
    return evidence

def evaluate_order_flow(packet):
    """Return independent three-state pattern results without performing orders."""
    base={'schema':'legacy-order-flow-v1','calibration':'mock_initial_unvalidated',
          'execution_enabled':False,'source_integrity_independently_verified':False,'patterns':{}}
    try:now,symbol,windows,books,_identity=_normalize(packet)
    except (InputUnknown,TypeError,KeyError,OverflowError) as error:
        reason=str(error) if isinstance(error,InputUnknown) else 'input_invalid'
        base['patterns']={key:_status('UNKNOWN',reason) for key in PATTERNS}
        return base
    past,stable,recent=windows
    result=base['patterns'];buy,sell=_ratio(recent)
    if not _enough(recent):result['OF-001']=_status('UNKNOWN','recent_trades_insufficient')
    elif sell==0:result['OF-001']=_status('UNKNOWN','sell_volume_zero')
    else:result['OF-001']=_status('PASS' if buy>=2*sell else 'FAIL','volume_ratio_observed',{'buy_qty':buy,'sell_qty':sell})
    if any(not _enough(w) for w in windows):result['OF-004']=_status('UNKNOWN','each_30s_window_needs_12_trades_12_shares')
    else:
        previous=sum(x['size'] for x in past+stable);current=buy+sell
        if not previous:result['OF-004']=_status('UNKNOWN','baseline_volume_zero')
        else:
            # current/30 divided by previous/60 >= 2 => current >= previous.
            price_up=recent[-1]['price']*1000>=recent[0]['price']*1001
            passed=current>=previous and buy*100>=60*current and price_up
            result['OF-004']=_status('PASS' if passed else 'FAIL','volume_rate_direction_price_observed',
                {'recent_qty':current,'prior_60s_qty':previous,'buy_qty':buy,'sell_qty':sell,
                 'first_price':recent[0]['price'],'last_price':recent[-1]['price']})
    latest_books=[b for b in books if now-timedelta(seconds=30)<b['src']<=now]
    baseline=[b for b in books if now-timedelta(seconds=60)<b['src']<=now-timedelta(seconds=30)]
    if len(latest_books)<3 or not baseline or not _enough(recent):
        result['OF-002']=_status('UNKNOWN','book_or_trade_samples_insufficient')
    else:
        ref=baseline[-1];levels=(tuple(p for p,_ in ref['bids'][:3]),tuple(p for p,_ in ref['asks'][:3])); checks=latest_books
        if any((tuple(p for p,_ in b['bids'][:3]),tuple(p for p,_ in b['asks'][:3]))!=levels for b in checks):
            result['OF-002']=_status('UNKNOWN','bid_price_levels_changed')
        else:
            prior=sum(q for _,q in ref['bids'][:3]);last=sum(q for _,q in checks[-1]['bids'][:3])
            if not prior:result['OF-002']=_status('UNKNOWN','baseline_bid_zero')
            elif (last*10<prior*11 or buy<=sell or any(
                  sum(q for _,q in b['bids'][:3])*100<60*(sum(q for _,q in b['bids'][:3])+sum(q for _,q in b['asks'][:3])) for b in checks)):
                result['OF-002']=_status('FAIL','book_trade_threshold_not_met')
            else:result['OF-002']=_status('UNKNOWN','price_support_and_spread_definition_unspecified')
    pullback=_pullback_clauses(windows)
    if pullback.get('samples')=='PASS' and ('FAIL' in (pullback['rebound_strict_0_1pct'],pullback['buy_share_55pct'])):
        result['OF-003']=_status('FAIL','rebound_or_buy_share_not_met',pullback)
    else:
        result['OF-003']=_status('UNKNOWN','decline_stability_rebound_formula_unspecified',pullback)
    ranked=_toss_ranking(packet,now,symbol);book_clauses=_rank_book_clauses(books,recent,now)
    ranking_metrics=ranked['metrics'];ranking_metrics.update(book_clauses)
    if ranked['status']=='FAIL' or 'FAIL' in (book_clauses['three_level_bid_over_ask_2x'],book_clauses['buy_share_55pct']):
        result['OF-005']=_status('FAIL',ranked['reason'] if ranked['status']=='FAIL' else 'book_or_buy_share_not_met',ranking_metrics)
    else:
        result['OF-005']=_status('UNKNOWN',ranked['reason'],ranking_metrics)
    result['OF-006']=_status('UNKNOWN','event_order_and_continuous_retention_not_proven',
                             {'candidate_walls':_wall_clauses(books,windows,now), 'candidate_limit':50,
                              'candidate_list_may_be_truncated':True})
    return base
