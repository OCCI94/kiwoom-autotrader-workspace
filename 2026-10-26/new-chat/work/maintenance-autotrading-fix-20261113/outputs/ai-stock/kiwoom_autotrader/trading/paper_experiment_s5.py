"""Native experiment S5 adapter: actual observed inputs, never a PASS-only flag.

The S5 research_v1 evaluator owns the pattern, observed VWAP and risk rules.
This adapter binds its full observed prefix, current quote and current five
minute boundary to immutable native order evidence. It performs no I/O.
"""
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal
import hashlib
import json
import re

from .s5_decision_evidence import pack_prefix, unpack_prefix
from .steady_uptrend_vwap import evaluate_steady_uptrend_vwap, moment

SCHEMA = 's5-native-adapter-v1'


def boundary(now):
    return now.replace(second=0, microsecond=0)-timedelta(minutes=now.minute % 5)


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def _quote(value, symbol, now):
    if not isinstance(value, dict) or value.get('symbol') != symbol:
        raise ValueError('s5_quote_symbol_unverified')
    start, observed = moment(value.get('request_started_at')), moment(value.get('observed_at'))
    if (start is None or observed is None or not start <= observed <= now
            or not timedelta(0) <= now-start <= timedelta(seconds=5)
            or start.date() != now.date() or type(value.get('current_price')) is not int
            or value['current_price'] <= 0 or value.get('source') not in ('ka10001', 'ka10001_normalized_response')
            or re.fullmatch('[a-f0-9]{64}', str(value.get('source_sha256'))) is None):
        raise ValueError('s5_quote_unverified_or_stale')
    return value


def evaluate_s5_native(bars, market, symbol, now):
    """Return an honest failed-check diagnosis even when no buy is possible."""
    _quote(market, symbol, now)
    return evaluate_steady_uptrend_vwap(bars, symbol=symbol, now=now,
        current_price=market['current_price'], daily_context=market.get('s5_daily_context'),
        risk_context=market.get('s5_risk_context'), require_short=True)


def s5_observation_diagnostic(bars, market, symbol, now, *, assignment=None,
                              ownership_status=None):
    """Evaluate the same inputs without producing an order/assignment proof.

    A PASS here is only a counterfactual observation. It never changes the
    immutable randomized arm, its budget or the native signal denominator.
    """
    evaluation = evaluate_s5_native(bars, market, symbol, now)
    return {'schema':'s5-assignment-independent-observation-v1','symbol':symbol,
        'observed_at':now.isoformat(),'signal_at':evaluation.get('signal_at',boundary(now).isoformat()),
        'evaluation_role':'observation_only','order_permission':False,
        'assignment_changed':False,'actual_assignment_arm':(assignment or {}).get('arm'),
        'actual_assignment_sha256':(assignment or {}).get('assignment_sha256'),
        'quote_source_sha256':market.get('source_sha256'),
        'daily_source_sha256':(market.get('s5_daily_context') or {}).get('source_sha256'),
        'risk_source_sha256':(market.get('s5_risk_context') or {}).get('source_sha256'),
        'ownership_status':ownership_status,'status':evaluation['status'],
        'reason':evaluation['reason'],'eligible':evaluation.get('eligible') is True,
        'checks':evaluation.get('checks',{}),'failed_checks':evaluation.get('failed_checks',[]),
        'check_groups':evaluation.get('check_groups',{}),'failure_details':evaluation.get('failure_details',{}),
        'session_coverage':evaluation.get('session_coverage'),'vwap':evaluation.get('vwap'),
        'order_block_reason':'observation_only_no_order_authority'}


def build_s5_native_evidence(bars, market, symbol, now):
    evaluation = evaluate_s5_native(bars, market, symbol, now)
    if evaluation.get('eligible') is not True:
        raise ValueError('s5_native_signal_not_eligible')
    quote = {key: market[key] for key in ('symbol', 'current_price', 'source',
        'source_sha256', 'request_started_at', 'observed_at')}
    proof = {'schema': SCHEMA, 'symbol': symbol, 'evaluated_at': now.isoformat(),
        'signal_at': evaluation['signal_at'], 'quote': quote,
        'minute_prefix': pack_prefix(bars, symbol),
        'daily_context': deepcopy(market['s5_daily_context']),
        'risk_context': deepcopy(market['s5_risk_context']), 'evaluation': evaluation}
    proof['input_sha256'] = _digest(proof)
    return proof


def validate_s5_native_evidence(payload, symbol, now, signal_at=None):
    """Recompute all inputs at reservation/send; no old-boundary replay."""
    keys = {'schema', 'symbol', 'evaluated_at', 'signal_at', 'quote', 'minute_prefix',
            'daily_context', 'risk_context', 'evaluation', 'input_sha256'}
    if (not isinstance(payload, dict) or set(payload) != keys
            or payload['schema'] != SCHEMA or payload['symbol'] != symbol
            or moment(now) is None):
        raise ValueError('s5_native_proof_invalid')
    digest = payload['input_sha256']
    if digest != _digest({key: value for key,value in payload.items() if key != 'input_sha256'}):
        raise ValueError('s5_native_proof_hash_mismatch')
    evaluated = moment(payload['evaluated_at'])
    actual_signal = moment(payload['signal_at'])
    if (evaluated is None or actual_signal != boundary(now) or boundary(evaluated) != actual_signal
            or not timedelta(0) <= now-evaluated <= timedelta(seconds=5)
            or signal_at is not None and moment(signal_at) != actual_signal):
        raise ValueError('s5_native_boundary_or_freshness_changed')
    quote = _quote(payload['quote'], symbol, now)
    bars = unpack_prefix(payload['minute_prefix'], symbol)
    market = {**quote, 's5_daily_context': payload['daily_context'], 's5_risk_context': payload['risk_context']}
    replay = evaluate_s5_native(bars, market, symbol, evaluated)
    if replay.get('eligible') is not True or replay != payload['evaluation']:
        raise ValueError('s5_native_recomputed_signal_mismatch')
    # Expiry can change between the first evaluation and queued dispatch.
    current = evaluate_s5_native(bars, market, symbol, now)
    if current.get('eligible') is not True:
        raise ValueError('s5_native_risk_or_input_expired')
    return current


def s5_signal_features(evaluation, current_price):
    """Scalar observed research fields; numerator stores three times HLC3."""
    vwap=evaluation.get('vwap') or {};coverage=evaluation.get('session_coverage') or {}
    numerator=vwap.get('hlc3_volume_numerator');volume=vwap.get('volume')
    distance=None
    if type(current_price) is int and type(numerator) is int and numerator>0 and type(volume) is int and volume>0:
        distance=str((Decimal(current_price)*3*volume/Decimal(numerator)-1)*10000)
    return {'vwap_distance_bps':distance,'vwap_basis':vwap.get('basis'),
        'vwap_exact_full_session':vwap.get('exact_full_session_vwap'),
        'missing_minutes':coverage.get('missing_count'),'observed_rows':coverage.get('observed_minutes'),
        'vwap':vwap,'failed_checks':evaluation.get('failed_checks',[])}
