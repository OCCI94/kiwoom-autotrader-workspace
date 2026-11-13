"""Bounded, immutable *advisory* pre-arm snapshots; never an order gateway.

Preparation may calculate features and read validated sources elsewhere. Lookup
does neither: it reads one published in-memory view and compares fixed-size
identity/version fields. A hit is NOT an atomic reservation or permission to buy.
The normal fresh broker reconciliation, shared ledger reservation and final
dispatch risk checks remain mandatory, including after a cache hit.

Writers publish copy-on-write views under a writer-only lock. Readers never
acquire that lock. This is an optimistic snapshot, not a real-time scheduling
guarantee: a revocation racing a returned result must still be caught by the
existing final order gateway. No wall-clock latency guarantee is made here.
"""
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from math import isfinite
import re
from threading import Lock
from types import MappingProxyType
from typing import Mapping, Optional


_HEX = re.compile(r'[0-9a-f]{64}\Z')
_SYMBOL = re.compile(r'[0-9]{6}\Z')
_STRATEGY = re.compile(r'[A-Za-z0-9_]{1,64}\Z')
_KST = timedelta(hours=9)
_MAX_TTL = timedelta(seconds=60)


def _time(value):
    return isinstance(value, datetime) and value.utcoffset() == _KST


def _integer(value, lower=0, upper=10**18):
    return type(value) is int and lower <= value <= upper


@dataclass(frozen=True, slots=True)
class DecisionVersion:
    """Caller-owned input identity, prepared before entering the fast path.

    ``evidence_sha256`` binds the complete market/strategy/risk input snapshot,
    not merely the DART file. ``ledger_watermark`` must change on any order,
    intent, fill, holdings/cash reconciliation or reservation change. Theme and
    reservation counters are additional revocation versions, never substitutes
    for the authoritative ledger. Supplying the stored version as 'current'
    without independently maintaining these versions is not a safety check.
    """
    session_id: str
    ledger_watermark: str
    evidence_sha256: str
    settings_sha256: str
    price: int
    theme_generation: int = 0
    reservation_generation: int = 0

    def __post_init__(self):
        if (not isinstance(self.session_id, str) or not 1 <= len(self.session_id) <= 128
                or not isinstance(self.ledger_watermark, str)
                or not 1 <= len(self.ledger_watermark) <= 128
                or not isinstance(self.evidence_sha256, str) or not _HEX.fullmatch(self.evidence_sha256)
                or not isinstance(self.settings_sha256, str) or not _HEX.fullmatch(self.settings_sha256)
                or not _integer(self.price, 1)
                or not _integer(self.theme_generation)
                or not _integer(self.reservation_generation)):
            raise ValueError('invalid_prearm_version')


@dataclass(frozen=True, slots=True)
class SafetyIndex:
    """Optional already-reconciled estimates at one exact version.

    Cash means available cash after all outstanding reservations; principal is
    cross-strategy held purchase principal plus outstanding buy reservations.
    A partial or expired index stays UNKNOWN. This class never queries or
    invents broker facts and never releases a reservation on a cancel ACK.
    """
    version: Optional[DecisionVersion] = None
    observed_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None
    held_principal_krw: Optional[int] = None
    reserved_principal_krw: Optional[int] = None
    available_cash_krw: Optional[int] = None
    theme_slots: Optional[int] = None
    position_slots: Optional[int] = None
    daily_entries_remaining: Optional[int] = None
    unresolved_orders: Optional[bool] = None
    verified: bool = False

    def usable(self, version, now):
        return (self.verified is True and self.version == version
                and _time(self.observed_at) and _time(self.expires_at)
                and self.observed_at <= now < self.expires_at
                and self.expires_at - self.observed_at <= _MAX_TTL
                and all(_integer(x) for x in (self.held_principal_krw,
                    self.reserved_principal_krw, self.available_cash_krw))
                and _integer(self.theme_slots, 0, 2)
                and _integer(self.position_slots, 0, 8)
                and _integer(self.daily_entries_remaining, 0, 2)
                and type(self.unresolved_orders) is bool)


def _freeze(value, *, depth=0, budget=None):
    """Bounded preparation-only deep copy; no caller-owned mutable objects."""
    if budget is None:
        budget = [512, 16384]
    budget[0] -= 1
    if budget[0] < 0 or depth > 8:
        raise ValueError('prearm_evaluation_too_large')
    if value is None or type(value) in (bool, int):
        if type(value) is int and abs(value) >= 10**60:
            raise ValueError('prearm_evaluation_number_too_large')
        return value
    if type(value) is float:
        if not isfinite(value):
            raise ValueError('invalid_prearm_evaluation_number')
        return value
    if type(value) is str:
        budget[1] -= len(value)
        if budget[1] < 0:
            raise ValueError('prearm_evaluation_too_large')
        return value
    if type(value) is dict:
        if any(type(key) is not str or len(key) > 128 for key in value):
            raise ValueError('invalid_prearm_evaluation_key')
        return MappingProxyType({key: _freeze(item, depth=depth+1, budget=budget)
                                 for key, item in value.items()})
    if type(value) in (list, tuple):
        return tuple(_freeze(item, depth=depth+1, budget=budget) for item in value)
    raise ValueError('invalid_prearm_evaluation_type')


def _thaw(value):
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if type(value) is tuple:
        return [_thaw(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class PrearmedTicket:
    symbol: str
    strategy: str
    version: DecisionVersion
    prepared_at: datetime
    expires_at: datetime
    evaluation: Mapping
    strategy_eligible: bool
    estimated_qty: int
    initial_budget_qty: int
    remaining_limit_qty: Optional[int]
    remaining_principal_krw: Optional[int]
    theme_slots: Optional[int]
    position_slots: Optional[int]
    daily_entries_remaining: Optional[int]
    safety_verified: bool
    estimate_basis: str
    safety_reason: str
    entry_kind: str
    owned: bool
    order_authorized: bool = field(default=False, init=False)
    requires_fresh_reconciliation: bool = field(default=True, init=False)

    def thawed_evaluation(self):
        """Optional preparation/decision boundary copy, not needed for lookup."""
        return _thaw(self.evaluation)


@dataclass(frozen=True, slots=True)
class PrearmLookup:
    status: str
    reason: str
    ticket: Optional[PrearmedTicket] = None
    order_authorized: bool = field(default=False, init=False)


@dataclass(frozen=True, slots=True)
class _View:
    tickets: Mapping
    serial: int


class PrearmedDecisionCache:
    """Fixed-capacity cache; owned tickets are never evicted for a flat entry.

    ``lookup`` does one hash lookup and a bounded number of scalar comparisons.
    Its returned result is advisory even when all cached safety fields were
    verified. This module has no file, broker, networking or order APIs.
    """
    def __init__(self, max_entries=40):
        if not _integer(max_entries, 1, 100):
            raise ValueError('invalid_prearm_capacity')
        self.max_entries = max_entries
        self._view = _View(MappingProxyType({}), 0)
        self._writer_lock = Lock()

    def arm(self, symbol, strategy, *, version, evaluation, now, expires_at,
            safety=None, initial_budget_krw=500000, principal_cap_krw=1000000,
            entry_kind='initial', owned=False):
        if (not isinstance(symbol, str) or not _SYMBOL.fullmatch(symbol)
                or not isinstance(strategy, str) or not _STRATEGY.fullmatch(strategy)
                or not isinstance(version, DecisionVersion)
                or not _time(now) or not _time(expires_at)
                or not now < expires_at <= now + _MAX_TTL
                or now.date() != expires_at.date()
                or type(evaluation) is not dict
                or not _integer(initial_budget_krw, 1, 500000)
                or not _integer(principal_cap_krw, 1, 1000000)
                or entry_kind not in ('initial', 'add_on') or type(owned) is not bool
                or entry_kind == 'add_on' and not owned
                or safety is not None and not isinstance(safety, SafetyIndex)):
            raise ValueError('invalid_prearm_input')
        frozen = _freeze(evaluation)
        eligible = evaluation.get('eligible') is True and evaluation.get('status') == 'PASS'
        initial_qty = min(initial_budget_krw, principal_cap_krw) // version.price
        verified = safety is not None and safety.usable(version, now)
        remaining = remaining_qty = theme_slots = position_slots = daily_entries = None
        quantity, basis, reason = initial_qty, 'budget_only_upper_bound', 'fresh_reconciliation_required'
        if entry_kind == 'add_on' and not verified:
            quantity = 0  # Never invent the remaining cost basis for an add-on.
        if verified:
            expires_at = min(expires_at, safety.expires_at)
            remaining = max(0, principal_cap_krw - safety.held_principal_krw - safety.reserved_principal_krw)
            remaining_qty = min(remaining, safety.available_cash_krw) // version.price
            theme_slots, position_slots = safety.theme_slots, safety.position_slots
            daily_entries = safety.daily_entries_remaining
            budget = min(initial_budget_krw, remaining) if entry_kind == 'initial' else remaining
            quantity = min(budget, safety.available_cash_krw) // version.price
            basis, reason = 'reconciled_advisory_estimate', 'cached_safety_clear'
            if safety.unresolved_orders:
                reason = 'unresolved_order_or_intent'
            elif daily_entries == 0:
                reason = 'daily_entry_limit_reached'
            elif entry_kind == 'initial' and theme_slots == 0:
                reason = 'theme_concentration_limit_reached'
            elif entry_kind == 'initial' and position_slots == 0:
                reason = 'max_concurrent_positions_reached'
            elif remaining == 0:
                reason = 'cap_exhausted'
            elif quantity == 0:
                reason = 'budget_below_one_share'
            if reason != 'cached_safety_clear':
                quantity = 0
        ticket = PrearmedTicket(symbol, strategy, version, now, expires_at,
            frozen, eligible, quantity, initial_qty, remaining_qty, remaining,
            theme_slots, position_slots, daily_entries, verified, basis, reason,
            entry_kind, owned)
        key = (symbol, strategy)
        with self._writer_lock:
            prior = self._view
            rows = {k: v for k, v in prior.tickets.items() if v.expires_at > now}
            if key not in rows and len(rows) >= self.max_entries:
                evictable = [(v.prepared_at, k) for k, v in rows.items() if not v.owned]
                if not evictable:
                    return PrearmLookup('UNAVAILABLE', 'cache_capacity_protects_owned')
                del rows[min(evictable)[1]]
            rows[key] = ticket
            self._view = _View(MappingProxyType(rows), prior.serial+1)
        return PrearmLookup('PREPARED', 'advisory_snapshot_prepared', ticket)

    def lookup(self, symbol, strategy, *, current_version, now):
        """No I/O, copying of feature arrays, network wait, or writer lock."""
        view = self._view
        try:
            ticket = view.tickets.get((symbol, strategy))
        except TypeError:
            return PrearmLookup('UNAVAILABLE', 'invalid_lookup_identity')
        if ticket is None:
            return PrearmLookup('MISS', 'not_prearmed')
        if not _time(now) or not ticket.prepared_at <= now < ticket.expires_at:
            return PrearmLookup('STALE', 'expired_or_clock_reversed')
        if not isinstance(current_version, DecisionVersion):
            return PrearmLookup('STALE', 'current_version_unavailable')
        prior = ticket.version
        if prior.session_id != current_version.session_id:
            return PrearmLookup('STALE', 'session_changed')
        if prior.ledger_watermark != current_version.ledger_watermark:
            return PrearmLookup('STALE', 'ledger_changed')
        if prior.theme_generation != current_version.theme_generation:
            return PrearmLookup('STALE', 'theme_changed')
        if prior.reservation_generation != current_version.reservation_generation:
            return PrearmLookup('STALE', 'reservation_changed')
        if prior.evidence_sha256 != current_version.evidence_sha256:
            return PrearmLookup('STALE', 'market_or_risk_evidence_changed')
        if prior.settings_sha256 != current_version.settings_sha256:
            return PrearmLookup('STALE', 'settings_changed')
        if prior.price != current_version.price:
            return PrearmLookup('STALE', 'price_changed')
        return PrearmLookup('HIT', 'advisory_snapshot_current', ticket)

    def invalidate(self, symbol=None, strategy=None):
        """Explicit revoke; only writers take a lock, never readers."""
        with self._writer_lock:
            prior = self._view
            rows = {k: v for k, v in prior.tickets.items()
                    if not (symbol is None or k[0] == symbol)
                    or strategy is not None and k[1] != strategy}
            self._view = _View(MappingProxyType(rows), prior.serial+1)

    clear = invalidate

    def status(self):
        view = self._view
        return {'entries': len(view.tickets), 'capacity': self.max_entries,
                'publication_serial': view.serial, 'order_authorized': False,
                'lookup_uses_network': False, 'lookup_acquires_lock': False,
                'requires_fresh_reconciliation': True}
