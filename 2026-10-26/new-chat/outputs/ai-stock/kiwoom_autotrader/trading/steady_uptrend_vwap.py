"""S5 paper-only research_v1 formula; no I/O, orders, or inferred candles.

Seven exact KST five-minute buckets and twenty strictly prior daily bars.
Each pattern bucket needs at least one observed positive-volume completed
minute. The first observed session bar must start between09:00 and09:10.
VWAP uses only actual observed minutes; gaps remain explicit and never become
synthetic candles, zero-volume fillers, or an exact whole-session claim.
ADR/DART remain entry-only gates.
"""
from datetime import datetime, timedelta
from decimal import Decimal
from statistics import median
import re

S5 = 'S5_steady_uptrend_vwap'


def moment(value):
    try:
        value = datetime.fromisoformat(value) if isinstance(value, str) else value
        return value if isinstance(value, datetime) and value.utcoffset() == timedelta(hours=9) else None
    except (ValueError, TypeError):
        return None


def _hash(value):
    return isinstance(value, str) and re.fullmatch('[0-9a-f]{64}', value) is not None


_CHECK_GROUPS = {
    'pattern': ('bullish_70pct','rising_closes_66pct','rising_lows_66pct','ma3_above_ma7',
        'return_1_to_4pct','close_drawdown_at_most_0_8pct','volume_median_growth_1_1',
        'last_bullish_body','last_upper_wick','last_close_location','no_bar_above_1_5pct'),
    'vwap': ('vwap_valid','closed_price_above_vwap','current_price_above_vwap','vwap_five_minute_slope_nonnegative'),
    'prior_daily': ('prior_daily_uptrend',),
    'risk': ('adr_below_6','dart_risk_review_clear','short_selling_five_sessions_clear'),
}


def _empty_coverage():
    return {'basis':'observed_minutes','session_complete':None,'expected_minutes':None,
        'observed_minutes':None,'missing_count':None,'missing_minutes':[],
        'missing_before_pattern_count':None,'missing_pattern_minutes':[],
        'tolerated_old_missing_count':0,'synthetic_bars_created':False,'coverage_validated':False,
        'first_observed_minute':None,'opening_anchor_valid':False,'opening_anchor_latest_allowed':None,
        'pattern_bucket_coverage':[],'pattern_nonempty_positive_volume_buckets':0,'empty_pattern_buckets':[]}


def _result(status, reason, **values):
    checks = values.get('checks', {})
    reasons = values.pop('failure_reasons', {})
    if status != 'PASS' and not reasons and reason != 'pattern_or_vwap_not_met':
        group = ('prior_daily' if reason == 'prior_daily_unavailable' else
                 'risk' if reason in ('dart_risk_review_unavailable','adr_overheated_or_unavailable')
                 or reason.startswith('short_') else 'pattern')
        reasons = {group:[reason]}
    groups, details = {}, {}
    for group, names in _CHECK_GROUPS.items():
        known = {name:checks[name] for name in names if type(checks.get(name)) is bool}
        failed = [name for name, passed in known.items() if not passed]
        required = set(names) - ({'short_selling_five_sessions_clear'} if group == 'risk' and 'short_selling_five_sessions_clear' not in checks else set())
        groups[group] = {'status':'fail' if failed else 'pass' if required <= set(known) else 'not_evaluated',
                         'checks':known,'failed_checks':failed}
        details[group] = {'failed_checks':failed,'reason_codes':list(reasons.get(group, []))}
    values.setdefault('session_coverage', _empty_coverage())
    return {'schema':'s5-steady-uptrend-vwap-research-v1', 'status':status,
            'eligible':status == 'PASS', 'reason':reason, **values,
            'check_groups':groups,'failed_checks':[name for name,passed in checks.items() if passed is False],
            'failure_details':details}


def validate_daily_context(context, symbol, now):
    if not isinstance(context, dict):
        return None
    start, available = moment(context.get('request_started_at')), moment(context.get('available_at'))
    rows = context.get('rows')
    if (context.get('schema') != 's5-prior-daily-v1' or context.get('symbol') != symbol
            or context.get('mode') != 'paper' or context.get('session_day') != now.strftime('%Y%m%d')
            or context.get('source_api') != 'ka10081' or not _hash(context.get('source_sha256'))
            or start is None or available is None or not start <= available <= now
            or start.date() != now.date() or not isinstance(rows, list) or len(rows) != 20):
        return None
    prior = None
    for row in rows:
        if not isinstance(row, dict): return None
        day = row.get('date')
        try:
            parsed = datetime.strptime(day, '%Y%m%d').date()
        except (TypeError, ValueError): return None
        if (parsed >= now.date() or prior is not None and parsed <= prior
                or parsed.weekday() >= 5 or any(type(row.get(k)) is not int or row[k] <= 0
                                              for k in ('open','high','low','close'))
                or not row['low'] <= min(row['open'],row['close']) <= max(row['open'],row['close']) <= row['high']
                or type(row.get('volume')) is not int or row['volume'] < 0):
            return None
        prior = parsed
    # Holiday spacing is not invented; a full returned series can still be
    # unavailable when the latest source bar is older than a calendar week.
    if (now.date()-prior).days > 7: return None
    return rows


def risk_eligible(context, symbol, now):
    if not isinstance(context, dict): return False
    reviewed, expires = moment(context.get('reviewed_at')), moment(context.get('expires_at'))
    return bool(context.get('schema') == 's5-dart-risk-v1' and context.get('symbol') == symbol
        and reviewed is not None and expires is not None and reviewed <= now < expires
        and expires-reviewed <= timedelta(hours=24)
        and context.get('coverage_complete') is True and context.get('unresolved_major_risk') is False
        and _hash(context.get('source_sha256')) and _hash(context.get('file_sha256')))


def validate_s5_risk(*, current_price, daily_context, risk_context, symbol, now, require_short=False):
    now = moment(now)
    prior = validate_daily_context(daily_context,symbol,now) if now is not None else None
    if prior is None or type(current_price) is not int or current_price <= 0:
        return {'eligible':False,'reason':'prior_daily_unavailable','adr_extension_multiple':None}
    ma20 = Decimal(sum(b['close'] for b in prior))/20
    adr = sum(Decimal(b['high']-b['low'])/b['low'] for b in prior)/20
    extension = (Decimal(current_price)/ma20-1)/adr if adr>0 else None
    if not risk_eligible(risk_context,symbol,now):
        reason = 'dart_risk_review_unavailable'
    elif extension is None or extension >= 6:
        reason = 'adr_overheated_or_unavailable'
    else:
        reason = 's5_risk_clear'
    result={'eligible':reason=='s5_risk_clear','reason':reason,'ma20':str(ma20),
            'adr_pct':str(adr*100),'adr_extension_multiple':str(extension) if extension is not None else None}
    short=risk_context.get('short_selling_evidence') if isinstance(risk_context,dict) else None
    if require_short or short is not None:
        from .s5_supply_risk import validate_short_selling
        expected=[datetime.strptime(b['date'],'%Y%m%d').date().isoformat() for b in prior[-5:]]
        supply=validate_short_selling(short,symbol=symbol,now=now,expected_session_days=expected)
        result['short_selling']=supply
        if reason=='s5_risk_clear' and not supply['eligible']:
            result.update(eligible=False,reason=supply['reason'])
    return result


def evaluate_steady_uptrend_vwap(bars, *, symbol, now, current_price, daily_context, risk_context=None, require_short=False):
    """Recompute signal and risk facts from immutable PIT inputs, never a flag."""
    now = moment(now)
    if now is None or not isinstance(symbol, str) or re.fullmatch('[0-9]{6}',symbol) is None:
        return _result('BLOCK','invalid_identity_or_time')
    if type(current_price) is not int or current_price <= 0:
        return _result('BLOCK','current_price_unavailable')
    minute = now.replace(second=0,microsecond=0)
    boundary = minute.replace(minute=(minute.minute//5)*5)
    if now.weekday() >= 5 or not (9,0) <= (now.hour,now.minute) < (15,19):
        return _result('DORMANT','outside_entry_session')
    opening = boundary.replace(hour=9,minute=0)
    required = int((boundary-opening).total_seconds()//60)
    if required < 35:
        return _result('DORMANT','five_minute_warmup')
    coverage = _empty_coverage()
    coverage['expected_minutes'] = required
    if not isinstance(bars,(list,tuple)) or not 1 <= len(bars) <= 390:
        return _result('DORMANT','session_minutes_unavailable',session_coverage=coverage)
    # Validate every supplied row, including prior-day transport margin and
    # completed minutes in the current incomplete five-minute bucket. Never
    # silently discard malformed, duplicated, reversed, future or inferred rows.
    previous_start = None
    for bar in bars:
        start = moment(getattr(bar,'start',None))
        query = moment(getattr(bar,'source_query_started_at',None))
        if start is not None and start > minute:
            return _result('DORMANT','future_session_minute',session_coverage=coverage)
        if start is None or start.second or start.microsecond or (previous_start is not None and start <= previous_start):
            return _result('DORMANT','invalid_or_missing_session_minute',session_coverage=coverage,
                failure_reasons={'pattern':['minute_timestamps_not_strictly_increasing']})
        previous_start = start
        prices = [getattr(bar,key,None) for key in ('open_price','high_price','low_price','close')]
        if (query is None or not start+timedelta(minutes=1) <= query <= now
                or now-query > timedelta(seconds=60)
                or getattr(bar,'source_symbol',None) != symbol or getattr(bar,'source_mode',None) != 'paper'
                or getattr(bar,'gap_proof',None) is not None
                or getattr(bar,'adjustment',None) not in ('',None)
                or getattr(bar,'adjustment_rate',None) not in ('',None)
                or not _hash(getattr(bar,'source_response_sha256',None))
                or any(type(p) is not int or p <= 0 for p in prices)
                or not bar.low_price <= min(bar.open_price,bar.close) <= max(bar.open_price,bar.close) <= bar.high_price
                or type(getattr(bar,'volume',None)) is not int or not 0 <= bar.volume < 10**19):
            return _result('DORMANT','invalid_or_missing_session_minute',session_coverage=coverage,
                failure_reasons={'pattern':['invalid_minute_provenance_or_ohlcv']})
    selected = [b for b in bars if opening <= b.start < boundary]
    indexed = {bar.start:bar for bar in selected}
    expected = [opening+timedelta(minutes=i) for i in range(required)]
    missing = [at for at in expected if at not in indexed]
    pattern_start = boundary-timedelta(minutes=35)
    recent_missing = [at for at in missing if at >= pattern_start]
    old_missing = [at for at in missing if at < pattern_start]
    first_observed = selected[0].start if selected else None
    opening_latest = opening+timedelta(minutes=10)
    opening_valid = first_observed is not None and opening <= first_observed <= opening_latest
    buckets = []
    for offset in range(7):
        bucket_start = pattern_start+timedelta(minutes=5*offset)
        times = [bucket_start+timedelta(minutes=i) for i in range(5)]
        observed_rows = [indexed[at] for at in times if at in indexed]
        buckets.append({'start':bucket_start.isoformat(),'end':(bucket_start+timedelta(minutes=5)).isoformat(),
            'observed_minutes':len(observed_rows),'positive_volume_minutes':sum(b.volume>0 for b in observed_rows),
            'missing_minutes':[at.isoformat() for at in times if at not in indexed],
            'complete':len(observed_rows)==5})
    empty_buckets = [bucket['start'] for bucket in buckets if bucket['positive_volume_minutes']==0]
    coverage.update(session_complete=not missing,observed_minutes=len(selected),
        missing_count=len(missing),missing_minutes=[at.isoformat() for at in missing],
        missing_before_pattern_count=len(old_missing),missing_pattern_minutes=[at.isoformat() for at in recent_missing],
        tolerated_old_missing_count=len(old_missing) if opening_valid and not empty_buckets else 0,
        coverage_validated=True,first_observed_minute=first_observed.isoformat() if first_observed else None,
        opening_anchor_valid=opening_valid,opening_anchor_latest_allowed=opening_latest.isoformat(),
        pattern_bucket_coverage=buckets,pattern_nonempty_positive_volume_buckets=7-len(empty_buckets),
        empty_pattern_buckets=empty_buckets)
    reasons = {}
    if not opening_valid: reasons['vwap']=['session_opening_anchor_unavailable']
    if empty_buckets: reasons['pattern']=['pattern_bucket_without_positive_volume']
    if reasons:
        return _result('DORMANT','incomplete_session_minutes',session_coverage=coverage,
            session_minute_count=len(selected),failure_reasons=reasons)
    # Align by KST timestamps, not by ordinal positions in a sparse row list.
    # All actual OHLCV rows remain observations, including supplied zero-volume
    # rows. A bucket still requires at least one positive-volume minute.
    five = []
    for offset in range(0,required,5):
        times=[opening+timedelta(minutes=offset+i) for i in range(5)]
        part=[indexed[at] for at in times if at in indexed]
        if not part: continue
        five.append({'open':part[0].open_price,'high':max(b.high_price for b in part),
                     'low':min(b.low_price for b in part),'close':part[-1].close,
                     'volume':sum(b.volume for b in part)})
    window = five[-7:]
    last = window[-1]
    peak = window[0]['close']
    shallow = True
    for b in window:
        peak = max(peak,b['close'])
        shallow = shallow and (peak-b['close'])*1000 <= peak*8
    span = last['high']-last['low']
    body = last['close']-last['open']
    initial_volume = median(b['volume'] for b in window[:3])
    checks = {
        'bullish_70pct':sum(b['close']>b['open'] for b in window)*10 >= 7*7,
        'rising_closes_66pct':sum(b['close']>a['close'] for a,b in zip(window,window[1:]))*100 >= 66*6,
        'rising_lows_66pct':sum(b['low']>a['low'] for a,b in zip(window,window[1:]))*100 >= 66*6,
        'ma3_above_ma7':sum(b['close'] for b in window[-3:])*7 > sum(b['close'] for b in window)*3,
        'return_1_to_4pct':window[0]['open']*101 <= last['close']*100 <= window[0]['open']*104,
        'close_drawdown_at_most_0_8pct':shallow,
        'volume_median_growth_1_1':initial_volume>0 and median(b['volume'] for b in window[-3:])*10 >= initial_volume*11,
        'last_bullish_body':body>0 and span>0 and body*4 >= span,
        'last_upper_wick':span>0 and (last['high']-max(last['open'],last['close']))*10 <= span*3,
        'last_close_location':span>0 and (last['close']-last['low'])*100 >= span*65,
        'no_bar_above_1_5pct':all((b['close']-b['open'])*1000 <= b['open']*15 for b in window),
    }
    num = sum((b.high_price+b.low_price+b.close)*b.volume for b in selected)
    vol = sum(b.volume for b in selected)
    prior_minutes = [b for b in selected if b.start < boundary-timedelta(minutes=5)]
    prior_num = sum((b.high_price+b.low_price+b.close)*b.volume for b in prior_minutes)
    prior_vol = sum(b.volume for b in prior_minutes)
    checks['vwap_valid'] = vol>0 and prior_vol>0
    checks['closed_price_above_vwap'] = vol>0 and selected[-1].close*3*vol>num
    checks['current_price_above_vwap'] = vol>0 and current_price*3*vol>num
    checks['vwap_five_minute_slope_nonnegative'] = vol>0 and prior_vol>0 and num*prior_vol>=prior_num*vol
    vwap = {'hlc3_volume_numerator':num,'volume':vol,'prior_5m_numerator':prior_num,
        'prior_5m_volume':prior_vol,'basis':'observed_minutes','session_complete':coverage['session_complete'],
        'exact_full_session_vwap':coverage['session_complete']}
    common = dict(session_coverage=coverage,signal_at=boundary.isoformat(),session_minute_count=len(selected),
        five_minute_count=len(five),pattern_five_minute_count=7,vwap=vwap)
    prior = validate_daily_context(daily_context,symbol,now)
    if prior is None:
        return _result('DORMANT','prior_daily_unavailable',checks=checks,**common)
    closes = [b['close'] for b in prior]
    checks['prior_daily_uptrend'] = sum(closes[-5:])*4 > sum(closes) and closes[-1] > closes[-6]
    ma20 = Decimal(sum(closes))/20
    adr = sum(Decimal(b['high']-b['low'])/b['low'] for b in prior)/20
    extension = (Decimal(current_price)/ma20-1)/adr if adr>0 else None
    checks['adr_below_6'] = extension is not None and extension < 6
    checks['dart_risk_review_clear'] = risk_eligible(risk_context,symbol,now)
    details = dict(checks=checks,**common,
        prior_daily={'ma5':str(Decimal(sum(closes[-5:]))/5),'ma20':str(ma20)},
        adr_pct=str(adr*100),adr_extension_multiple=str(extension) if extension is not None else None)
    if not checks['dart_risk_review_clear']:
        return _result('DORMANT','dart_risk_review_unavailable',**details)
    if not checks['adr_below_6']:
        return _result('BLOCK','adr_overheated_or_unavailable',**details)
    if require_short or isinstance(risk_context,dict) and risk_context.get('short_selling_evidence') is not None:
        supply=validate_s5_risk(current_price=current_price,daily_context=daily_context,
            risk_context=risk_context,symbol=symbol,now=now,require_short=True)['short_selling']
        checks['short_selling_five_sessions_clear']=supply['eligible']
        details['short_selling']=supply
        if not supply['eligible']:
            return _result('BLOCK' if supply['status']=='BLOCK' else 'DORMANT',supply['reason'],**details)
    return _result('PASS' if all(checks.values()) else 'BLOCK',
        'steady_uptrend_vwap' if all(checks.values()) else 'pattern_or_vwap_not_met',**details)


def timed_exit(position, now):
    """S5 only: first real verified00fill, never receipt or last partial fill."""
    now = moment(now)
    if not isinstance(position,dict) or now is None or position.get('status') != 'owned': return None
    qty = position.get('owned_qty')
    first = moment(position.get('first_fill_at'))
    received = moment(position.get('first_fill_observed_at'))
    if type(qty) is not int or qty <= 0: return None
    if now.strftime('%H:%M') >= '15:19': return ('s5_regular_cutoff_exit',qty)
    if (position.get('first_fill_timing_verified') is True and first is not None and received is not None
            and first <= received <= now and now >= first+timedelta(minutes=30)):
        return ('time_stop_30m',qty)
    return None
