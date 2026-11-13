"""Background-only read-only account/theme projections, never trade authority.

The existing ledger capacity validator is reused inside one SQLite read
transaction. Price, cash and holdings can still change after that observation;
the immutable projection expires after 2.5 seconds and does NOT replace fresh
broker reconciliation or transactional reservation/dispatch checks.
"""
from copy import deepcopy
from datetime import datetime, timedelta
from hashlib import sha256
import json
from pathlib import Path
import re
import sqlite3
import time
from types import MappingProxyType

from .theme_concentration import theme_family_snapshot
from .paper_reconciliation import _validate_holdings


_CODE = re.compile(r'[0-9]{6}\Z')
_NUM = re.compile(r'\+?[0-9]{1,15}\Z')
_KST = timedelta(hours=9)
_TTL = timedelta(seconds=2.5)


def _moment(value):
    try:
        value = datetime.fromisoformat(value) if isinstance(value, str) else value
        return value if isinstance(value, datetime) and value.utcoffset() == _KST else None
    except (TypeError, ValueError):
        return None


def _amount(value):
    return int(value) if isinstance(value,str) and _NUM.fullmatch(value) else None


def _unknown(reason='not_prepared'):
    return {'schema':'prearmed-account-advisory-v1','status':'unavailable','reason':reason,
        'observed_at':None,'prepared_at':None,'expires_at':None,'theme_remaining_slots':None,
        'position_remaining_slots':None,'observed_held_qty':None,
        'observed_held_principal_krw':None,'reserved_principal_krw':None,
        'remaining_principal_krw':None,'observed_deposit_krw':None,
        'observed_orderable_cash_krw':None,'available_cash_krw':None,
        'estimated_initial_qty':None,'reservation_status':'unknown',
        'order_authorized':False,'requires_fresh_reconciliation':True,
        'snapshot_is_atomic_broker_balance':False}


def _snapshot(tracker, symbols, now):
    if (_moment(now) is None or not isinstance(symbols,(tuple,list,set)) or not 1<=len(symbols)<=100
            or any(not isinstance(s,str) or not _CODE.fullmatch(s) for s in symbols)):
        raise ValueError('account_index_input_invalid')
    symbols=tuple(sorted(set(symbols)))
    public = tracker.paper_theme_capacity_snapshot(now)
    if (not isinstance(public,dict) or public.get('mode')!='paper'
            or _moment(public.get('as_of')) != now
            or type(public.get('position_limit')) is not int or not 1<=public['position_limit']<=8
            or type(public.get('theme_limit')) is not int or not 1<=public['theme_limit']<=2):
        raise ValueError('account_index_capacity_unavailable')
    scope=tracker.scope;path=Path(tracker.path)
    if not isinstance(scope,str) or not scope or path.is_symlink() or not path.is_file():
        raise ValueError('account_index_source_unavailable')
    with sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=.1) as db:
        db.row_factory=sqlite3.Row;db.execute('PRAGMA query_only=ON');db.execute('BEGIN')
        # Reuse the actual capacity/fill/gap validator under this transaction;
        # never treat a stale public display snapshot as the present balance.
        capacity=tracker._assert_position_capacity(db,None,now,public['position_limit'],
            theme_limit=public['theme_limit'],unclassified_policy=public['unclassified_policy'],snapshot_only=True)
        occupied=capacity.get('occupied_symbols')
        if (not isinstance(occupied,list) or len(occupied)>10000
                or any(not isinstance(s,str) or not _CODE.fullmatch(s) for s in occupied)):
            raise ValueError('account_index_occupied_unknown')
        occupied=set(occupied)
        rows=db.execute("SELECT observed_at,source_kind,payload_json FROM account_balance_daily WHERE scope=? AND mode='paper' AND day=?",
                        (scope,now.strftime('%Y%m%d'))).fetchall()
        if len(rows)!=1 or rows[0]['source_kind']!='broker_observation':
            raise ValueError('account_index_balance_unobserved')
        observed=_moment(rows[0]['observed_at']);body=json.loads(rows[0]['payload_json'])
        if (observed is None or not timedelta(0)<=now-observed<=timedelta(seconds=90)
                or capacity.get('observed_at')!=rows[0]['observed_at']
                or body.get('schema')!='paper-account-snapshot-v1' or body.get('mode')!='paper'
                or body.get('exchange')!='KRX' or not isinstance(body.get('holdings'),list)):
            raise ValueError('account_index_balance_stale')
        # _paper_balance_snapshot only stores this schema after the original
        # complete report's snapshot_changed=False check. Retain its source
        # timestamp; do not call this projection an atomic broker balance.
        held,_,principal,unknown_cost=_validate_holdings(dict(body,snapshot_changed=False))
        quantities={}
        for row in body['holdings']:
            code=row['stk_cd'][-6:];qty=_amount(row.get('rmnd_qty'))
            if qty is None:raise ValueError('account_index_quantity_unknown')
            quantities[code]=quantities.get(code,0)+qty
        if not held<=occupied:raise ValueError('account_index_holdings_conflict')
        reservation_clear=False
        try:
            tracker._assert_no_pending_account(db,'paper')
            reservation_clear=True
        except Exception:
            # Unknown cancellation/partial-fill principal stays unknown; ACK
            # receipt is never a reason to release reservation capital here.
            pass
        cash=body.get('cash',{})
        deposit=_amount(cash.get('entr')) if isinstance(cash,dict) else None
        orderable=_amount(cash.get('ord_alow_amt')) if isinstance(cash,dict) else None
        available=min(deposit,orderable) if reservation_clear and deposit is not None and orderable is not None else None
        account_sha=sha256(rows[0]['payload_json'].encode()).hexdigest()
    # Local taxonomy is metadata, not a quote/API request. All memberships count.
    taxonomy=theme_family_snapshot(set(symbols)|occupied,now,path=tracker.theme_taxonomy_path,
        unclassified_policy=public['unclassified_policy'])
    families=taxonomy['themes_by_symbol']
    taxonomy_source=taxonomy.get('taxonomy_evidence')
    if public.get('taxonomy_evidence') != taxonomy_source or public.get('supplement_evidence') != taxonomy.get('supplement_evidence'):
        raise ValueError('account_index_theme_changed_during_prepare')
    for code in occupied:
        if not isinstance(families.get(code),list) or not families[code]:
            raise ValueError('account_index_occupied_theme_unknown')
    counts={}
    for code in occupied:
        for family in families[code]:counts[family]=counts.get(family,0)+1
    result={};expires=min(now+_TTL,observed+timedelta(seconds=90))
    scope_digest=sha256(scope.encode()).hexdigest()
    for code in symbols:
        row=_unknown();groups=families.get(code)
        slots=min(max(0,public['theme_limit']-counts.get(family,0)) for family in groups) if isinstance(groups,list) and groups else None
        held_principal=None if code in unknown_cost else principal.get(code,0)
        reserved=0 if reservation_clear else None
        remaining=max(0,1000000-held_principal) if reserved==0 and held_principal is not None else None
        row.update(status='observed_advisory',reason='requires_fresh_reconciliation',symbol=code,
            observed_at=observed.isoformat(),prepared_at=now.isoformat(),expires_at=expires.isoformat(),
            theme_remaining_slots=slots,theme_families=tuple(groups) if groups else (),
            position_remaining_slots=max(0,public['position_limit']-len(occupied)),
            already_occupied=code in occupied,observed_held_qty=quantities.get(code,0),
            observed_held_principal_krw=held_principal,reserved_principal_krw=reserved,
            remaining_principal_krw=remaining,observed_deposit_krw=deposit,
            observed_orderable_cash_krw=orderable,available_cash_krw=available,
            reservation_status='no_pending_at_observation' if reservation_clear else 'pending_or_unverified',
            scope_sha256=scope_digest,account_source_sha256=account_sha,
            source_kind='broker_observation',estimated_initial_qty=None)
        result[code]=MappingProxyType(row)
    return MappingProxyType(result)


class BackgroundAccountIndex:
    """Refresh from a background owner; lookups never open SQLite or acquire locks."""
    def __init__(self, *, clock=time.monotonic):
        self._clock=clock;self._snapshot=None

    def refresh(self,tracker,symbols,now):
        started=self._clock()
        try:
            rows=_snapshot(tracker,symbols,now)
            value=(started,now,rows)
        except Exception:
            value=None
        self._snapshot=value
        return value is not None

    def lookup(self,symbol,now):
        snapshot=self._snapshot
        if (snapshot is None or _moment(now) is None or not snapshot[1]<=now
                or not 0<=self._clock()-snapshot[0]<=2.5):
            return _unknown('unavailable_or_stale')
        row=snapshot[2].get(symbol)
        if row is None or now>=_moment(row['expires_at']):return _unknown('symbol_unavailable_or_stale')
        return dict(row)

    def clear(self):self._snapshot=None
