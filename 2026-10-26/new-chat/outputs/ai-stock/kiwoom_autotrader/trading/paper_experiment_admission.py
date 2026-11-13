"""Fail-closed admission checks for prospective paper experiment strategies.

Evidence hashes identify the producer's source observations; this validator does
not itself establish that a producer fetched or authenticated those sources.
"""
from datetime import date, datetime, timedelta
import re
from zoneinfo import ZoneInfo

ARMS = frozenset({'EXP_ORB_A', 'EXP_ORB_B', 'EXP_GDR', 'EXP_VSS', 'EXP_LDR'})
_KST = ZoneInfo('Asia/Seoul')
_HASH = re.compile(r'[0-9a-f]{64}\Z')


def _time(value):
    if not isinstance(value, str) or len(value) > 40:
        raise ValueError('invalid timestamp')
    result = datetime.fromisoformat(value)
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError('timezone required')
    return result


def _keys(value, names):
    if not isinstance(value, dict) or set(value) != set(names):
        raise ValueError('invalid evidence fields')


def _source(value, observed, now, max_age_seconds):
    digest = value.get('source_sha256')
    if not isinstance(digest, str) or _HASH.fullmatch(digest) is None:
        raise ValueError('invalid source digest')
    available = _time(value.get('available_at'))
    if not (now - timedelta(seconds=max_age_seconds) <= available <= observed <= now):
        raise ValueError('stale or future source')


def validate_admission(value, symbol, arm, now):
    """Validate frozen inputs at prepare and again at dispatch; return True.

    Raised ValueError means no order permission. A complete VI query with no
    trigger may state None; an incomplete query can never certify that state.
    """
    _keys(value, ('schema', 'symbol', 'arm', 'observed_at', 'instrument', 'turnover', 'vi', 'price'))
    if (value['schema'] != 'paper-experiment-admission-v1' or arm not in ARMS
            or value['arm'] != arm or value['symbol'] != symbol
            or not isinstance(symbol, str) or re.fullmatch(r'[0-9]{6}', symbol) is None
            or not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None):
        raise ValueError('invalid admission scope')
    observed = _time(value['observed_at'])
    instrument, turnover, vi, price = (value[x] for x in ('instrument', 'turnover', 'vi', 'price'))
    _keys(instrument, ('source_sha256', 'available_at', 'common_share', 'status'))
    _keys(turnover, ('source_sha256', 'available_at', 'complete', 'days'))
    _keys(vi, ('source_sha256', 'available_at', 'complete', 'active', 'last_trigger_at','last_trigger_basis'))
    _keys(price, ('source_sha256', 'available_at', 'current_price', 'upper_limit_price'))
    # Completed daily data may be cached; do not relabel the old retrieval time
    # as a fresh observation. VI and executable-price data have tighter leases.
    for source, max_age in ((instrument, 300), (turnover, 36 * 3600), (vi, 30), (price, 5)):
        _source(source, observed, now, max_age)
    if instrument['common_share'] is not True or instrument['status'] != 'normal':
        raise ValueError('not an eligible common stock')
    days = turnover['days']
    if turnover['complete'] is not True or not isinstance(days, list) or len(days) != 20:
        raise ValueError('20 completed daily observations required')
    previous = None
    total = 0
    for row in days:
        _keys(row, ('date', 'turnover_krw'))
        raw = row['date']
        if not isinstance(raw, str) or re.fullmatch(r'[0-9]{8}', raw) is None:
            raise ValueError('invalid trading day')
        day = date(int(raw[:4]), int(raw[4:6]), int(raw[6:]))
        if previous is not None and day <= previous or day >= observed.astimezone(_KST).date():
            raise ValueError('daily series must precede observation and be ordered')
        amount = row['turnover_krw']
        if type(amount) is not int or not 0 <= amount <= 10**18:
            raise ValueError('invalid turnover')
        previous, total = day, total + amount
    if total < 60_000_000_000:
        raise ValueError('insufficient 20-day average turnover')
    if (vi['complete'] is not True or vi['active'] is not False
            or vi['last_trigger_basis'] != 'release_time_conservative_120s_not_trigger_assertion'):
        raise ValueError('VI state unknown or active')
    if vi['last_trigger_at'] is not None:
        trigger = _time(vi['last_trigger_at'])
        if trigger > observed or trigger > now - timedelta(seconds=120):
            raise ValueError('VI trigger is too recent')
    current, upper = price['current_price'], price['upper_limit_price']
    if (type(current) is not int or type(upper) is not int or not 0 < current <= upper <= 10**12
            or current * 100 >= upper * 95):
        raise ValueError('invalid price or insufficient upper-limit distance')
    return True
