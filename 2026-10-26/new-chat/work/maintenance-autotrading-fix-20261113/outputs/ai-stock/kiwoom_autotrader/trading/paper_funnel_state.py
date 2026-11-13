"""Receiver-local funnel observation sharing; never an order authorization.

A same-session, same-minute gate narrows legacy GDCRS new-entry work. Historical
signals are not replayed when a symbol leaves/re-enters the active set. Chart
sharing preserves original receipt/provenance; it cannot refresh a quote or bar.
No disk, broker, authentication, strategy ownership or reservation is changed.
"""
from copy import deepcopy
from datetime import datetime
import hashlib
import json
import re

from kiwoom_autotrader.api.minute_chart import session_identity


def _state(session, now):
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError('funnel_clock_unavailable')
    identity = session_identity(session)
    if identity[0] != 'paper':
        raise ValueError('funnel_paper_only')
    minute = now.replace(second=0, microsecond=0)
    state = getattr(session, '_paper_funnel_observation_state', None)
    if not isinstance(state, dict) or state.get('identity') != identity or state.get('minute') != minute:
        state = {'identity':identity, 'minute':minute, 'gate':None, 'charts':{}}
        setattr(session, '_paper_funnel_observation_state', state)
    return state


def publish_gate(session, now, symbols, *, available, candidates=None, provider_binding=None,
                 watch_candidate_validator=None):
    if (type(available) is not bool or not isinstance(symbols, (list,tuple)) or len(symbols)>15
            or any(not isinstance(code,str) or re.fullmatch(r'[0-9]{6}',code) is None for code in symbols)):
        raise ValueError('funnel_gate_unavailable')
    state = _state(session, now)
    if watch_candidate_validator is not None and not callable(watch_candidate_validator):raise ValueError('candidate_validator')
    state['watch_candidate_validator']=watch_candidate_validator
    if candidates is not None and (not isinstance(candidates,dict) or set(candidates)-set(symbols)):
        raise ValueError('funnel_candidate_gate_unavailable')
    state['gate'] = {'available':available, 'symbols':tuple(dict.fromkeys(symbols)),
                     'available_at':now, 'observation_only':True, 'precision_books':{},
                     'candidates':deepcopy(candidates or {}), 'provider_binding':provider_binding}


def _canonical(value):
    return json.dumps(value,ensure_ascii=True,sort_keys=True,separators=(',',':'),allow_nan=False)


def _session_sha(session):
    return hashlib.sha256(_canonical(list(session_identity(session))).encode()).hexdigest()


_CANDIDATE_FIELDS = ('name','change_pct','surge_rate_pct','turnover_krw','available_at',
    'source_hashes','membership_evidence','liquidity_evidence')


def _candidate_projection(row, *, tracked=None):
    if tracked is None:tracked=row.get('track') in ('T1_PRECURSOR','T2_MOMENTUM')
    keys=_CANDIDATE_FIELDS+('track','current_price','toss_evidence','toss_session_day','toss_previous_session_day','precursor_evidence') if tracked else _CANDIDATE_FIELDS
    return {key:deepcopy(row.get(key)) for key in keys}


def dynamic_s1_admission(session, now, symbol):
    """Bind one observed dynamic first-entry permission to immutable evidence.

    This is separate from the legacy configuration universe; never append hot
    rows to settings or use an old rank/book to authorize a later initial buy.
    Final ownership, cash, position, theme and principal guards still apply.
    """
    from .dynamic_s1_admission import validate_dynamic_admission
    gate=read_gate(session,now)
    if (not gate or not gate['available'] or symbol not in precision_ready_symbols(gate,now)
            or symbol not in gate['candidates']):
        raise ValueError('dynamic_s1_admission_unavailable')
    row=gate['candidates'][symbol]
    _assert_watch_generation(session,now,symbol,row)
    value={'schema_version':'paper-dynamic-s1-admission-v2' if row.get('track') in ('T1_PRECURSOR','T2_MOMENTUM') else 'paper-dynamic-s1-admission-v1','mode':'paper','symbol':symbol,
        'session_sha256':_session_sha(session),'minute':now.replace(second=0,microsecond=0).isoformat(),
        'admitted_at':gate['available_at'].isoformat(),'verified_at':now.isoformat(),
        'provider_binding':gate['provider_binding'],'total_budget_krw':1_000_000,
        'initial_entry_pct':50,'initial_budget_krw':500_000,
        'candidate':_candidate_projection(row),
        'precision_book':deepcopy(gate['precision_books'][symbol])}
    value['admission_sha256']=hashlib.sha256(_canonical(value).encode()).hexdigest()
    return validate_dynamic_admission(value,now=now,symbol=symbol,session_sha256=_session_sha(session))


def assert_current_dynamic_s1_admission(session, now, value):
    """Revalidate the original proof immediately before queued transport."""
    from .dynamic_s1_admission import validate_dynamic_admission
    checked=validate_dynamic_admission(value,now=now,session_sha256=_session_sha(session))
    symbol=checked['symbol'];gate=read_gate(session,now)
    _assert_watch_generation(session,now,symbol,checked['candidate'])
    if (not gate or not gate['available'] or symbol not in gate['symbols']
            or symbol not in precision_ready_symbols(gate,now)
            or checked['admitted_at']!=gate['available_at'].isoformat()
            or checked['provider_binding']!=gate['provider_binding']
            or checked['candidate']!=_candidate_projection(gate['candidates'].get(symbol,{}),tracked=checked['schema_version']=='paper-dynamic-s1-admission-v2')
            or checked['precision_book']!=gate['precision_books'].get(symbol)):
        raise ValueError('dynamic_s1_admission_changed')
    return checked


def _assert_watch_generation(session, now, symbol, candidate):
    if candidate.get('track') not in ('T1_PRECURSOR','T2_MOMENTUM'):return
    check=_state(session,now).get('watch_candidate_validator')
    if not callable(check) or check(symbol,candidate,now) is not True:
        raise ValueError('watch_candidate_generation_or_price_changed')


def read_gate(session, now):
    state = _state(session, now)
    gate = state['gate']
    if gate is None or gate['available_at'] > now:
        return None
    return deepcopy(gate)


def mark_precision_ready(session, now, symbol, book):
    from kiwoom_autotrader.api.order_book import observed_book_is_fresh
    state=_state(session,now);gate=state['gate']
    if (not gate or not gate['available'] or symbol not in gate['symbols']
            or book.get('symbol')!=symbol or not observed_book_is_fresh(book,now)):
        raise ValueError('funnel_precision_unavailable')
    gate['precision_books'][symbol]=deepcopy(book)


def precision_ready_symbols(gate, now):
    from kiwoom_autotrader.api.order_book import observed_book_is_fresh
    if not isinstance(gate,dict) or gate.get('available') is not True:
        return ()
    return tuple(code for code,book in gate.get('precision_books',{}).items()
        if code in gate.get('symbols',()) and observed_book_is_fresh(book,now))


def shared_chart(session, symbol, *, count, now, fetcher):
    """Successful or failed request is attempted once per required count/minute.

    A second loop may reuse an earlier larger request. A larger count requested
    later requires its own bounded fetch, so callers align entry counts (62 for
    B3, 22 otherwise). An error cannot trigger a duplicate retry through a peer.
    """
    state = _state(session, now)
    cached = state['charts'].get(symbol)
    if cached is not None and cached['failed']:
        raise ValueError('funnel_chart_prior_request_failed')
    if cached is not None and cached['requested_count'] >= count:
        return deepcopy(cached['rows'])
    entry = {'requested_count':count, 'rows':None, 'failed':True}
    state['charts'][symbol] = entry
    rows = fetcher(session, symbol, count=count)
    if session_identity(session) != state['identity']:
        raise ValueError('funnel_chart_session_changed')
    entry.update(rows=deepcopy(rows), failed=False)
    return rows
