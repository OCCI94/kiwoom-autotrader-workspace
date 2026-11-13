"""Bounded, replayable S5 input evidence. No broker or disk access.

The complete observed session prefix is compacted without dropping OHLCV or
source page identity. It is independently decoded and evaluated at order time;
a producer's precomputed PASS flag cannot authorize a buy.
"""
from datetime import timedelta
import re
from .decision_evidence import _fail, _integer, _time, _serialize_bar
from ..api.minute_chart import MinuteBar

S5 = 'S5_steady_uptrend_vwap'


def pack_prefix(bars, symbol):
    sources, rows = [], []
    for b in bars:
        if (b.source_symbol != symbol or b.source_mode != 'paper' or b.gap_proof is not None
                or b.adjustment not in ('',None) or b.adjustment_rate not in ('',None)):
            _fail()
        source = [b.source_batch,b.source_page,b.source_response_sha256,b.source_query_started_at.isoformat()]
        if source not in sources:
            sources.append(source)
        rows.append([b.start.isoformat(),b.open_price,b.high_price,b.low_price,b.close,b.volume,
                     b.acc_volume,sources.index(source),b.source_row])
    return {'schema':'s5-observed-session-prefix-v1','symbol':symbol,'sources':sources,'rows':rows}


def unpack_prefix(value, symbol):
    if (not isinstance(value,dict) or set(value)!={'schema','symbol','sources','rows'}
            or value['schema']!='s5-observed-session-prefix-v1' or value['symbol']!=symbol
            or not isinstance(value['sources'],list) or not 1<=len(value['sources'])<=8
            or not isinstance(value['rows'],list) or not 7<=len(value['rows'])<=390):
        _fail()
    sources=value['sources']
    for s in sources:
        if (not isinstance(s,list) or len(s)!=4 or not isinstance(s[0],str) or not 1<=len(s[0])<=128
                or type(s[1]) is not int or not 0<=s[1]<=8 or not isinstance(s[2],str)
                or re.fullmatch('[a-f0-9]{64}',s[2]) is None):
            _fail()
        _time(s[3])
    result=[]
    for row in value['rows']:
        if not isinstance(row,list) or len(row)!=9:
            _fail()
        start=_time(row[0])
        for val in row[1:5]:_integer(val,1,10**12-1)
        _integer(row[5],0,10**19-1)
        if row[6] is not None:_integer(row[6],0,10**19-1)
        _integer(row[7],0,len(sources)-1);_integer(row[8],0,10**5)
        source=sources[row[7]]
        result.append(MinuteBar(start,row[4],volume=row[5],acc_volume=row[6],
            source_symbol=symbol,source_mode='paper',source_batch=source[0],source_page=source[1],
            source_row=row[8],source_response_sha256=source[2],source_query_started_at=_time(source[3]),
            open_price=row[1],high_price=row[2],low_price=row[3]))
    return result


def input_proof(bars, market, side, reason, symbol):
    if side!='buy':return None
    initial=reason=='steady_uptrend_vwap'
    return {'schema':'s5-order-input-v1','minute_prefix':pack_prefix(bars,symbol) if initial else None,
            'daily_context':market.get('s5_daily_context'), 'risk_context':market.get('s5_risk_context'),
            'evaluation':market.get('steady_uptrend_vwap_evidence') if initial else None}


def _evaluation_matches(fresh, saved):
    """Read immutable pre-coverage evidence without widening sparse new input.

    The pre-2026-11-02 evaluator required every minute since09:00. Its exact
    economic values and keys must match; only the later diagnostic additions
    can be projected away. Partial/mixed schemas and missing old minutes are
    never accepted as legacy. Current dispatch has its independent required
    five-session supply gate in ExecutionTracker.
    """
    if fresh == saved: return True
    added = {'session_coverage','check_groups','failed_checks','failure_details','pattern_five_minute_count'}
    coverage = fresh.get('session_coverage', {})
    if (not isinstance(saved,dict) or any(key in saved for key in added)
            or coverage.get('coverage_validated') is not True
            or coverage.get('session_complete') is not True or coverage.get('missing_count') != 0
            or coverage.get('observed_minutes') != coverage.get('expected_minutes')):
        return False
    legacy_vwap = {'hlc3_volume_numerator','volume','prior_5m_numerator','prior_5m_volume'}
    if not isinstance(saved.get('vwap'),dict) or set(saved['vwap']) != legacy_vwap:
        return False
    projection = {key:value for key,value in fresh.items() if key not in added}
    projection['vwap'] = {key:fresh['vwap'][key] for key in legacy_vwap}
    return projection == saved


def validate_s5_signal(out):
    from .steady_uptrend_vwap import evaluate_steady_uptrend_vwap, validate_s5_risk
    sig=out['multi_strategy_signal'];side=sig['side'];reason=sig['reason']
    observed,evaluated=_time(out['observed_at']),_time(out['evaluated_at'])
    pos=sig['position']
    if out['strategy_version']!='paper-multi-observed-v4' or out['source_kind']!='broker_observation':_fail()
    for name in ('first_fill_at','first_fill_observed_at'):
        if pos.get(name) is not None and _time(pos[name])>evaluated:_fail()
    if type(pos['first_fill_timing_verified']) is not bool:_fail()
    if pos['first_fill_timing_verified'] and (pos['first_fill_at'] is None or pos['first_fill_observed_at'] is None
            or not _time(pos['first_fill_at'])<=_time(pos['first_fill_observed_at'])<=evaluated):_fail()
    if side=='buy':
        config=out['config']
        if (config.get('multi_b3_stage') not in ('initial','add') or config.get('multi_b3_initial_entry_pct')!=50
                or config.get('multi_b3_add_on_profit_bps')!=50 or config.get('multi_b3_total_budget_krw')!=1000000
                or config.get('max_daily_entries_per_symbol')!=2):_fail()
        proof=sig['s5_input']
        if (not isinstance(proof,dict) or set(proof)!={'schema','minute_prefix','daily_context','risk_context','evaluation'}
                or proof['schema']!='s5-order-input-v1'):_fail()
        risk=validate_s5_risk(current_price=sig['market']['current_price'],daily_context=proof['daily_context'],
            risk_context=proof['risk_context'],symbol=sig['symbol'],now=evaluated)
        if risk.get('eligible') is not True:_fail()
        if reason=='steady_uptrend_vwap':
            bars=unpack_prefix(proof['minute_prefix'],sig['symbol'])
            if [_serialize_bar(b) for b in bars[-21:]]!=out['bars']:_fail()
            # Full raw OHLCV agrees with the shared final-window evidence too.
            for b,r in zip(bars[-21:],sig['ohlcv']):
                if [b.open_price,b.high_price,b.low_price,b.volume,b.source_response_sha256]!=[r['open'],r['high'],r['low'],r['volume'],r['response_sha256']]:_fail()
            fresh=evaluate_steady_uptrend_vwap(bars,symbol=sig['symbol'],now=observed,
                current_price=sig['market']['current_price'],daily_context=proof['daily_context'],risk_context=proof['risk_context'])
            if fresh.get('eligible') is not True or not _evaluation_matches(fresh,proof['evaluation']):_fail()
            # The evaluation cannot silently cross into another five-minute slot.
            if evaluated.replace(second=0,microsecond=0)!=observed.replace(second=0,microsecond=0):_fail()
        elif reason=='add_on_strength':
            if proof['minute_prefix'] is not None or proof['evaluation'] is not None:_fail()
            risk=validate_s5_risk(current_price=sig['market']['current_price'],daily_context=proof['daily_context'],
                                 risk_context=proof['risk_context'],symbol=sig['symbol'],now=evaluated)
            if risk.get('eligible') is not True:_fail()
        else:_fail()
    else:
        if sig['s5_input'] is not None:_fail()
        if reason=='time_stop_30m':
            if (pos['first_fill_timing_verified'] is not True or pos['first_fill_at'] is None
                    or evaluated-_time(pos['first_fill_at'])<timedelta(minutes=30)):_fail()
        elif reason=='s5_regular_cutoff_exit':
            if evaluated.utcoffset()!=timedelta(hours=9) or (evaluated.hour,evaluated.minute)<(15,19):_fail()
