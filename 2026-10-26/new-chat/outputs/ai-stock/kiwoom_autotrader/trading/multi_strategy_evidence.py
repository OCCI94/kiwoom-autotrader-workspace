"""Source-bound S2/S3/S4 decisions, without inventing a golden/dead cross.

No network, file writes or runtime setting changes. This records the observed
signal; broker quantity, ownership and atomic claims are independently checked
by the order gateway. Historical records never acquire this evidence later.
"""
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
import re

STRATEGIES = {'S2_surge_high_breakout', 'S3_upper_limit_continuation', 'S4_theme_and_catalyst_leader', 'S5_steady_uptrend_vwap'}
REASONS = {'shallow_pullback_high_breakout', 'observed_20pct_crossing',
           'theme_leader_or_verified_disclosure', 'stop_loss_or_sma20',
           'take_profit_10pct', 'take_profit_half_5pct', 'regular_cutoff_exit', 'add_on_strength', 'steady_uptrend_vwap', 'time_stop_30m', 's5_regular_cutoff_exit'}
LEGACY_MARKET_FIELDS = ('symbol', 'request_started_at', 'observed_at', 'available_at',
    'source', 'source_sha256', 'source_market_timestamp', 'source_freshness_verified',
    'current_price', 'day_high', 'upper_limit_price', 'change_pct',
    'rising_volume_surge', 'surge_evidence', 'theme_evidence', 'dart_evidence')
MARKET_FIELDS = LEGACY_MARKET_FIELDS + ('day_open',)
_S5_INITIAL_POLICY = 's5_actual_observed_five_minute_buckets_v1'


def _s5_initial_window_policy(bars):
    """Descriptive suffix only; the full S5 prefix is independently replayed."""
    from .observed_bar_window import timeline_rows
    rows = timeline_rows(bars)
    starts = [datetime.fromisoformat(row['start']) for row in rows]
    missing = int(sum((b-a).total_seconds()/60-1 for a,b in zip(starts, starts[1:])))
    return {'policy':_S5_INITIAL_POLICY, 'bar_count':len(rows), 'missing_minutes':missing,
        'missing_cause':'unknown' if missing else None, 'synthetic_bars_created':False,
        'sma_basis':'not_used_for_s5_initial', 'volume_basis':'actual_observed_bucket_volumes',
        'timeline':rows}


def validate_multi_signal(out):
    from kiwoom_autotrader.trading.decision_evidence import _fail, _integer, _time
    signal = out.get('multi_strategy_signal')
    version4 = out.get('strategy_version') == 'paper-multi-observed-v4'
    is_s5 = out.get('strategy') == 'S5_steady_uptrend_vwap'
    if version4 != is_s5:
        _fail()
    version3 = out.get('strategy_version') in ('paper-multi-observed-v3','paper-multi-observed-v4')
    version2 = out.get('strategy_version') in ('paper-multi-observed-v2', 'paper-multi-observed-v3', 'paper-multi-observed-v4')
    keys = {'symbol', 'side', 'reason', 'quantity', 'baseline', 'ohlcv', 'market', 'position', 'sma20', 'sma20_status'}
    if version4:
        keys |= {'s5_input'}
    if version2:
        keys |= {'bar_window', 'b3_add_on'}
    if (out.get('strategy') not in STRATEGIES or out.get('strategy_version') not in ('paper-multi-observed-v1','paper-multi-observed-v2','paper-multi-observed-v3','paper-multi-observed-v4')
            or out.get('previous_difference') is not None or out.get('difference') is not None
            or out.get('b3_addition') is not None or out.get('rollover_revalidation') is not None
            or not isinstance(signal, dict) or set(signal) != keys
            or signal['side'] not in {'buy', 'sell'} or signal['reason'] not in REASONS
            or not isinstance(signal['symbol'], str) or not re.fullmatch(r'[0-9]{6}', signal['symbol'])):
        _fail()
    symbol, side = signal['symbol'], signal['side']
    s5_initial = is_s5 and side == 'buy' and signal['reason'] == 'steady_uptrend_vwap'
    market, rows, position = signal['market'], signal['ohlcv'], signal['position']
    if (not isinstance(market, dict) or set(market) != set(MARKET_FIELDS if version3 else LEGACY_MARKET_FIELDS) or market['symbol'] != symbol
            or not isinstance(market['source_sha256'], str)
            or not re.fullmatch(r'[0-9a-f]{64}', market['source_sha256'])
            or market['source_freshness_verified'] is not False or market['source_market_timestamp'] is not None
            or not isinstance(rows, list) or len(rows) > 21 or not isinstance(position, dict)
            or set(position) != ({'generation_id', 'owned_qty', 'basis_krw', 'half_take_done'} | ({'first_fill_at','first_fill_observed_at','first_fill_timing_verified'} if version4 else set()))):
        _fail()
    _integer(market['current_price'], 1, 10**12-1)
    observed, evaluated = _time(out['observed_at']), _time(out['evaluated_at'])
    request, received = _time(market['request_started_at']), _time(market['available_at'])
    if (not request <= received <= observed <= evaluated or _time(market['observed_at']) != received
            or evaluated - request > timedelta(seconds=5) or request.date() != evaluated.date()):
        _fail()
    if side == 'buy':
        addition = version2 and signal['reason'] == 'add_on_strength'
        if version3 and out['strategy'] == 'S2_surge_high_breakout' and not addition:
            from .multi_strategy import s2_momentum_eligible
            if not s2_momentum_eligible(market, evaluated):
                _fail()
        expected_reason = {'S2_surge_high_breakout':'shallow_pullback_high_breakout',
                           'S3_upper_limit_continuation':'observed_20pct_crossing',
                           'S4_theme_and_catalyst_leader':'theme_leader_or_verified_disclosure',
                           'S5_steady_uptrend_vwap':'steady_uptrend_vwap'}[out['strategy']]
        if ((s5_initial and not 7 <= len(rows) <= 21)
                or (not s5_initial and len(rows) != 21) or signal['quantity'] is not None):
            _fail()
        if signal['reason'] != expected_reason and not addition:
            _fail()
        _integer(out.get('budget'), 1, 1_000_000)
        baseline = signal['baseline']
        if not addition and is_s5:
            # S5 uses the complete observed session prefix as its entry proof;
            # the cross-oriented EntryState baseline is only optional context.
            # Its absence or age cannot invalidate a first visit. The full
            # prefix is independently re-evaluated in validate_s5_signal.
            if baseline is not None:
                if (not isinstance(baseline, dict) or set(baseline) != {'last_bar_start', 'prior_peak',
                        'pulled_back', 'previous_change_pct'} or type(baseline['pulled_back']) is not bool):
                    _fail()
                if baseline['last_bar_start'] is not None:
                    _time(baseline['last_bar_start'])
                if baseline['prior_peak'] is not None:
                    _integer(baseline['prior_peak'], 1, 10**12-1)
        elif not addition:
            if (not isinstance(baseline, dict) or set(baseline) != {'last_bar_start', 'prior_peak',
                    'pulled_back', 'previous_change_pct'} or type(baseline['pulled_back']) is not bool
                    or _time(rows[-1]['start']) - _time(baseline['last_bar_start']) != timedelta(minutes=1)):
                _fail()
            _integer(baseline['prior_peak'], 1, 10**12-1)
        elif baseline is not None:
            _fail()
    else:
        _integer(signal['quantity'], 1, 10**12-1)
        _integer(position['owned_qty'], signal['quantity'], 10**12-1)
        _integer(position['basis_krw'], 1, 10**15-1)
        generation = position['generation_id']
        if (not isinstance(generation, str) or re.fullmatch(r'[0-9a-f]{32}', generation) is None
                or out['config'].get('multi_position_generation_id') != generation
                or signal['reason'] not in ({'stop_loss_or_sma20', 'take_profit_10pct', 'take_profit_half_5pct', 'regular_cutoff_exit'} | ({'time_stop_30m','s5_regular_cutoff_exit'} if is_s5 else set()))):
            _fail()
        if signal['reason'] == 'take_profit_half_5pct':
            if (out['strategy'] != 'S2_surge_high_breakout' or out['config'].get('multi_exit_kind') != 'half'
                    or signal['quantity'] != position['owned_qty']//2 or position['half_take_done']):
                _fail()
        elif out['config'].get('multi_exit_kind') != 'full' or signal['quantity'] != position['owned_qty']:
            _fail()
        if signal['reason'] == 'regular_cutoff_exit':
            cutoff = evaluated.replace(hour=15, minute=19, second=0, microsecond=0)
            if (evaluated.utcoffset() != timedelta(hours=9) or evaluated < cutoff+timedelta(minutes=1)
                    or not any(_time(row['start']) == cutoff for row in rows)
                    or signal['quantity'] != position['owned_qty'] or out['config'].get('multi_exit_kind') != 'full'):
                _fail()
    if type(position['half_take_done']) is not bool or signal['sma20_status'] not in {'observed', 'unavailable'}:
        _fail()
    if signal['sma20_status'] == 'observed':
        try:
            sma = Decimal(signal['sma20'])
            if not sma.is_finite() or sma <= 0:
                _fail()
        except (TypeError, ValueError, InvalidOperation):
            _fail()
    elif signal['sma20'] is not None:
        _fail()
    if len(rows) != len(out.get('bars') or []):
        _fail()
    for row, bar in zip(rows, out.get('bars') or []):
        if (not isinstance(row, dict) or set(row) != {'start', 'open', 'high', 'low', 'close', 'volume',
                'query_started_at', 'response_sha256'} or row['start'] != bar['start'] or row['close'] != bar['close']
                or not isinstance(row['response_sha256'], str) or re.fullmatch(r'[0-9a-f]{64}', row['response_sha256']) is None):
            _fail()
        _integer(row['close'], 1, 10**12-1)
        for key in ('open', 'high', 'low'):
            if side == 'buy' or row[key] is not None:
                _integer(row[key], 1, 10**12-1)
        if side == 'buy' or row['volume'] is not None:
            _integer(row['volume'], 0, 10**19-1)
        source = bar.get('source_observation', {})
        if all(row[k] is not None for k in ('open', 'high', 'low')):
            if not row['low'] <= min(row['open'], row['close']) <= max(row['open'], row['close']) <= row['high']:
                _fail()
        if (source.get('symbol') != symbol or source.get('mode') != 'paper'
                or source.get('volume') != row['volume'] or source.get('query_started_at') != row['query_started_at']
                or source.get('adjustment') not in ('', None) or source.get('adjustment_rate') not in ('', None)):
            _fail()
    if version2:
        from .observed_bar_window import POLICY, validate_timeline
        policy = signal['bar_window']
        s5_bucket_policy = s5_initial and isinstance(policy, dict) and policy.get('policy') == _S5_INITIAL_POLICY
        if (not isinstance(policy,dict) or set(policy) != {'policy','bar_count','missing_minutes','missing_cause',
                'synthetic_bars_created','sma_basis','volume_basis','timeline'}
                or policy['policy'] != (_S5_INITIAL_POLICY if s5_bucket_policy else POLICY)
                or type(policy['bar_count']) is not int or policy['bar_count'] != len(rows)
                or type(policy['missing_minutes']) is not int
                or policy['synthetic_bars_created'] is not False
                or policy['sma_basis'] != ('not_used_for_s5_initial' if s5_bucket_policy else 'last_20_actual_observed_closes')
                or policy['volume_basis'] != ('actual_observed_bucket_volumes' if s5_bucket_policy else 'previous_20_actual_observed_volumes')):
            _fail()
        try:
            if s5_bucket_policy:
                from .s5_decision_evidence import unpack_prefix
                prefix = unpack_prefix(signal['s5_input']['minute_prefix'],symbol)
                # Neither a caller's policy flag nor an abbreviated suffix can
                # substitute for the actual full-prefix evaluator below.
                expected = _s5_initial_window_policy(prefix[-21:])
                if policy != expected or signal['sma20_status'] != 'unavailable' or signal['sma20'] is not None:
                    _fail()
                missing = expected['missing_minutes']
            else:
                missing = validate_timeline(policy['timeline'],observed,require_latest=signal['reason']!='regular_cutoff_exit')
        except (ValueError,TypeError,KeyError,OverflowError):
            _fail()
        if (policy['missing_minutes'] != missing or policy['missing_cause'] != ('unknown' if missing else None)
                or len(policy['timeline']) != len(rows)):
            _fail()
        for source,row in zip(policy['timeline'],rows):
            if any(source[key] != row[key] for key in ('start','query_started_at','response_sha256')):
                _fail()
        if signal['sma20_status'] == 'observed' and (len(rows)<20 or Decimal(signal['sma20']) != Decimal(sum(r['close'] for r in rows[-20:]))/20):
            _fail()
        cfg = out['config']
        if signal['reason'] == 'add_on_strength':
            _validate_add_on(out)
        elif signal['b3_add_on'] is not None or cfg.get('multi_b3_stage') == 'add':
            _fail()
        if cfg.get('multi_b3_stage') == 'initial':
            if (side!='buy' or position['generation_id'] is not None or position['owned_qty'] not in (0,None)
                    or position['basis_krw'] not in (0,None)
                    or out['budget'] > cfg['multi_b3_total_budget_krw']*cfg['multi_b3_initial_entry_pct']//100):
                _fail()

    if is_s5:
        from .s5_decision_evidence import validate_s5_signal
        validate_s5_signal(out)

def _validate_add_on(out):
    """An add-on is stricter than an indicator: prove the entire post-fill path."""
    from .decision_evidence import _fail, _integer, _time, _validate_bar_rows
    from .observed_bar_window import validate_timeline
    sig, cfg = out['multi_strategy_signal'], out['config']
    pos, add, rows = sig['position'], sig['b3_add_on'], sig['ohlcv']
    if (sig['side']!='buy' or cfg.get('multi_b3_stage')!='add'
            or not isinstance(add,dict) or set(add) != {'initial_fill_observed_at','trend_bars','trend_timeline'}
            or pos['generation_id'] != cfg.get('multi_b3_initial_intent_id') or pos['half_take_done'] is not False):
        _fail()
    _integer(pos['owned_qty'],1,10**12-1); _integer(pos['basis_krw'],1,1_000_000)
    fill, now = _time(add['initial_fill_observed_at']), _time(out['observed_at'])
    first = fill.replace(second=0,microsecond=0)
    if fill.date()!=now.date() or fill>=now or _time(rows[-1]['start'])+timedelta(minutes=1)<=fill:
        _fail()
    trend = add['trend_bars']
    starts = _validate_bar_rows(trend,60,now)
    expected = int((_time(rows[-1]['start'])-(first-timedelta(minutes=19))).total_seconds()//60)+1
    shared = min(21,len(trend))
    if (not 20<=expected<=61 or len(trend)!=expected or starts[0]!=first-timedelta(minutes=19)
            or starts[-1]!=_time(rows[-1]['start']) or trend[-shared:]!=out['bars'][-shared:]):
        _fail()
    try:
        if validate_timeline(add['trend_timeline'],now)!=0:
            _fail()
    except (ValueError,TypeError,KeyError,OverflowError):
        _fail()
    if (len(add['trend_timeline']) != len(trend)
            or add['trend_timeline'][-shared:] != sig['bar_window']['timeline'][-shared:]):
        _fail()
    for source,bar in zip(add['trend_timeline'],trend):
        provenance = bar.get('source_observation',{})
        if (source['start']!=bar['start'] or source['query_started_at']!=provenance.get('query_started_at')
                or provenance.get('symbol')!=sig['symbol'] or provenance.get('mode')!='paper'
                or provenance.get('adjustment') not in ('',None) or provenance.get('adjustment_rate') not in ('',None)):
            _fail()
    # S2/S3/S4 retain their own exits. An observed negative 5/20 state after
    # the initial fill forbids averaging down even if a later bar recovers.
    if any(sum(b['close'] for b in trend[i-4:i+1])*20 < sum(b['close'] for b in trend[i-19:i+1])*5
           for i in range(19,len(trend))):
        _fail()
    prior = sum(row['volume'] for row in rows[:-1])
    if (prior<=0 or 40*rows[-1]['volume']<3*prior
            or sig['market']['current_price']*pos['owned_qty']*10000 < pos['basis_krw']*(10000+cfg['multi_b3_add_on_profit_bps'])
            or out['budget'] > cfg['multi_b3_total_budget_krw']-pos['basis_krw']
            or out['budget'] > cfg['multi_b3_total_budget_krw']*(100-cfg['multi_b3_initial_entry_pct'])//100):
        _fail()


def multi_strategy_evidence(*, strategy, symbol, side, reason, budget, position, bars,
                            market, observed_at, baseline=None, quantity=None, exit_kind=None,
                            evaluated_at=None, run_id='paper-multi', source_hash=None,
                            source_kind='broker_observation', b3_config=None, b3_add_on=None):
    from kiwoom_autotrader.trading.decision_evidence import freeze_evidence, _serialize_bar, _fail
    from .observed_bar_window import window_policy
    evaluated_at = evaluated_at or observed_at
    is_s5 = strategy == 'S5_steady_uptrend_vwap'
    s5_initial = is_s5 and side == 'buy' and reason == 'steady_uptrend_vwap'
    window = list(bars)[-21:]
    config = {'enabled': True, 'reentry_cooldown_minutes': 20}
    if b3_config is not None:
        config.update(b3_config)
    if side == 'sell':
        config.update(multi_exit_kind=exit_kind or ('half' if reason == 'take_profit_half_5pct' else 'full'),
                      multi_position_generation_id=position.get('generation_id'))
    state = None if baseline is None else {
        'last_bar_start': baseline.last_bar_start.isoformat() if baseline.last_bar_start else None,
        'prior_peak': baseline.prior_peak, 'pulled_back': baseline.pulled_back,
        'previous_change_pct': str(baseline.previous_change_pct) if baseline.previous_change_pct is not None else None}
    sma = str(Decimal(sum(x.close for x in window[-20:])) / 20) if len(window) >= 20 and not s5_initial else None
    try:
        policy = (_s5_initial_window_policy(window) if s5_initial else
            window_policy(window,observed_at,require_latest=reason!='regular_cutoff_exit'))
    except (ValueError,TypeError,AttributeError,OverflowError):
        _fail()
    return freeze_evidence({
        'strategy': strategy, 'strategy_version': 'paper-multi-observed-v4' if is_s5 else 'paper-multi-observed-v3', 'run_id': run_id,
        'strategy_source_sha256': source_hash, 'config': config,
        'run_scope': 'monitor_instance', 'strategy_source_scope': 'monitor_module',
        'source_hash_scope': 'normalized_completed_window',
        'observed_at': observed_at.isoformat(), 'evaluated_at': evaluated_at.isoformat(),
        'candle_start': window[-1].start.isoformat() if window else None,
        'candle_end': (window[-1].start+timedelta(minutes=1)).isoformat() if window else None,
        'interval_seconds': 60, 'short_period': None, 'long_period': None,
        'budget': budget if side == 'buy' else None, 'source_kind': source_kind,
        'source_provider': 'kiwoom-rest' if source_kind == 'broker_observation' else None,
        'source_api': 'ka10080' if window else 'ka10001', 'source_exchange': 'KRX',
        'source_time_semantics': 'start', 'source_revision': None,
        'previous_difference': None, 'difference': None, 'bars': [_serialize_bar(x) for x in window],
        'rollover_revalidation': None,
        'multi_strategy_signal': {'symbol': symbol, 'side': side, 'reason': reason, 'quantity': quantity,
            'baseline': state, 'ohlcv': [{'start':x.start.isoformat(), 'open':x.open_price,
                'high':x.high_price, 'low':x.low_price, 'close':x.close, 'volume':x.volume,
                'query_started_at':x.source_query_started_at.isoformat(),
                'response_sha256':x.source_response_sha256} for x in window],
            'market':{k:market.get(k) for k in MARKET_FIELDS},
            # An unowned entry has no prior partial exit for this future
            # generation. This is not a claim about historical holdings.
            'position':{k:(False if side == 'buy' and k == 'half_take_done'
                                  and position.get(k) is None else position.get(k))
                        for k in ('generation_id','owned_qty','basis_krw','half_take_done')} | (
                        {'first_fill_at':position.get('first_fill_at'), 'first_fill_observed_at':position.get('first_fill_observed_at'),
                         'first_fill_timing_verified':position.get('first_fill_timing_verified') is True} if is_s5 else {}),
            'sma20':sma, 'sma20_status':'observed' if sma is not None else 'unavailable',
            'bar_window': policy,
            'b3_add_on': b3_add_on,
            **({'s5_input': __import__('kiwoom_autotrader.trading.s5_decision_evidence',fromlist=['input_proof']).input_proof(bars,market,side,reason,symbol)} if is_s5 else {})}})
