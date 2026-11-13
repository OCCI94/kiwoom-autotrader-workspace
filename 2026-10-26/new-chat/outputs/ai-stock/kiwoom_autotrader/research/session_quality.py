"""Pure, bounded checks for observed one-minute research data and claims.

No files, broker connections, settings, interpolation or orders. Session bounds
are explicit caller inputs, not a verified exchange calendar. Records follow
minute_research's normalized START-time format: timestamp YYYYMMDDHHMMSS and
integer open/high/low/close/volume. Optional traded_notional_krw must be actual
executed notional in KRW, never OHLC * volume or rounded million-KRW turnover.
Passing arithmetic/coverage checks cannot certify market data or a strategy.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal, localcontext
import re


_MINUTE = timedelta(minutes=1)
_MAX_ROWS = 20000


def _aware(value):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timezone-aware datetime required")
    return value


def _timestamp(value, timezone):
    if not isinstance(value, str) or re.fullmatch(r"[0-9]{14}", value) is None:
        raise ValueError("normalized START timestamp required")
    return datetime.strptime(value, "%Y%m%d%H%M%S").replace(tzinfo=timezone)


def _notional(value):
    if value is None:
        return None
    text = str(value) if type(value) is int or isinstance(value, Decimal) else value
    if not isinstance(text, str) or re.fullmatch(r"[0-9]{1,30}(?:\.[0-9]{1,12})?", text) is None:
        raise ValueError("bounded nonnegative executed KRW notional required")
    return Decimal(text)


def assess_session_quality(records, *, session_open, session_close, observed_at,
                           opening_range_minutes=5, pagination_complete=None,
                           calendar_verified=False, source_finality_verified=False):
    """Describe coverage; missing clock minutes stay unknown, never zero volume.

    opening_range.calculation_status is a local arithmetic verdict. signal_usable
    additionally requires the caller's explicit calendar/finality confirmations;
    those assertions are not independently verified by this pure function.
    VWAP uses only explicitly supplied executed notional, through the latest
    elapsed minute, and needs complete coverage from the supplied session open.
    """
    start = _aware(session_open)
    end = _aware(session_close).astimezone(start.tzinfo)
    observed = _aware(observed_at).astimezone(start.tzinfo)
    if (start.second or start.microsecond or end.second or end.microsecond
            or not _MINUTE <= end - start <= timedelta(days=1)):
        raise ValueError("explicit minute-aligned session bounds of at most one day required")
    if (not isinstance(records, (list, tuple)) or len(records) > _MAX_ROWS
            or type(opening_range_minutes) is not int or not 1 <= opening_range_minutes <= 60
            or start + opening_range_minutes * _MINUTE > end):
        raise ValueError("bounded normalized records and opening range required")
    if (pagination_complete is not None and type(pagination_complete) is not bool
            or type(calendar_verified) is not bool or type(source_finality_verified) is not bool):
        raise ValueError("explicit boolean provenance flags required")
    flags, rows, seen = set(), {}, {}
    invalid = duplicates = conflicts = outside = forming = zero_volume = 0
    invalid_notional = False
    for row in records:
        try:
            if not isinstance(row, dict):
                raise ValueError
            at = _timestamp(row.get("timestamp"), start.tzinfo)
            if at.second != 0:
                raise ValueError
            if not start <= at < end:
                outside += 1
                continue
            if row.get("valid_ohlcv") is False:
                raise ValueError
            if at > observed:
                flags.add("FUTURE_TIMESTAMP")
                invalid += 1
                continue
            if at + _MINUTE > observed:
                forming += 1
                continue
            values = tuple(row.get(key) for key in ("open", "high", "low", "close", "volume"))
            if (any(type(value) is not int for value in values)
                    or any(not 0 < value <= 999999999999 for value in values[:4])
                    or not 0 <= values[4] <= 999999999999999
                    or not values[2] <= min(values[0], values[3]) <= max(values[0], values[3]) <= values[1]):
                raise ValueError
            try:
                notional = _notional(row.get("traded_notional_krw"))
                if notional is not None and not values[2] * values[4] <= notional <= values[1] * values[4]:
                    raise ValueError
            except ValueError:
                invalid_notional = True
                notional = None
            normalized = (*values, notional)
            if at in seen:
                duplicates += 1
                if seen[at] != normalized:
                    conflicts += 1
                    rows.pop(at, None)
                continue
            seen[at] = normalized
            rows[at] = normalized
            zero_volume += values[4] == 0
        except (ValueError, TypeError, OverflowError):
            invalid += 1
    stop = min(end, max(start, observed.replace(second=0, microsecond=0)))
    expected = [start + index * _MINUTE for index in range(int((stop - start) / _MINUTE))]
    missing = [at for at in expected if at not in rows]
    first = min(rows) if rows else None
    if invalid:
        flags.add("INVALID_MINUTE_ROWS")
    if duplicates:
        flags.add("DUPLICATE_MINUTE_ROWS")
    if conflicts:
        flags.add("CONFLICTING_MINUTE_REVISIONS")
    if forming:
        flags.add("UNFINISHED_MINUTE_ROWS_EXCLUDED")
    if outside:
        flags.add("OUTSIDE_SESSION_ROWS_EXCLUDED")
    if missing:
        flags.update(("MISSING_CLOCK_MINUTES", "MISSING_MINUTE_CAUSE_UNKNOWN"))
    if first is not None and first > start:
        flags.add("SESSION_OPEN_PREFIX_MISSING")
    if observed < end:
        flags.add("SESSION_NOT_YET_COMPLETE")
    if pagination_complete is not True:
        flags.add("SOURCE_PAGINATION_NOT_COMPLETE_OR_UNKNOWN")
    if not calendar_verified:
        flags.add("SESSION_CALENDAR_UNVERIFIED")
    if not source_finality_verified:
        flags.add("SOURCE_BAR_FINALITY_UNVERIFIED")
    if invalid_notional:
        flags.add("INVALID_EXECUTED_NOTIONAL")
    coherent = not invalid and not duplicates and not conflicts
    prefix_complete = bool(expected) and not missing and coherent
    opening_times = [start + index * _MINUTE for index in range(opening_range_minutes)]
    opening_missing = [at for at in opening_times if at not in rows]
    opening_elapsed = observed >= start + opening_range_minutes * _MINUTE
    opening_ready = opening_elapsed and not opening_missing and coherent
    opening_reasons = []
    if not opening_elapsed:
        opening_reasons.append("OPENING_RANGE_NOT_ELAPSED")
    if opening_missing:
        opening_reasons.append("OPENING_RANGE_MINUTES_MISSING")
    if not coherent:
        opening_reasons.append("AMBIGUOUS_OR_INVALID_INPUT")
    vwap_reasons = []
    if not prefix_complete:
        vwap_reasons.append("SESSION_PREFIX_INCOMPLETE")
    if any(rows[at][5] is None for at in expected if at in rows) or invalid_notional:
        vwap_reasons.append("EXACT_EXECUTED_NOTIONAL_UNAVAILABLE")
    total_volume = sum(rows[at][4] for at in expected if at in rows)
    if not total_volume:
        vwap_reasons.append("NO_OBSERVED_TRADE_VOLUME")
    vwap = None
    if not vwap_reasons:
        with localcontext() as context:
            context.prec = 60
            vwap = str(sum((rows[at][5] for at in expected), Decimal(0)) / total_volume)
    provenance = calendar_verified and source_finality_verified
    return {
        "schema": "research-session-quality-v1", "timestamp_semantics": "START",
        "session_open": start.isoformat(), "session_close": end.isoformat(), "observed_at": observed.isoformat(),
        "input_rows": len(records), "elapsed_expected_minutes": len(expected), "observed_completed_minutes": len(rows),
        "missing_minutes": [at.isoformat() for at in missing], "missing_count": len(missing),
        "first_observed_minute": first.isoformat() if first else None,
        "first_observed_at_session_open": first == start if first else None,
        "session_prefix_complete": prefix_complete,
        "full_session_complete": observed >= end and prefix_complete,
        "invalid_rows": invalid, "duplicate_rows": duplicates, "conflicting_rows": conflicts,
        "outside_session_rows": outside, "unfinished_rows": forming, "explicit_zero_volume_rows": zero_volume,
        "opening_range": {"minutes": opening_range_minutes, "calculation_status": "PASS" if opening_ready else "BLOCKED",
            "missing_minutes": [at.isoformat() for at in opening_missing], "reason_codes": opening_reasons,
            "high": max(rows[at][1] for at in opening_times) if opening_ready else None,
            "low": min(rows[at][2] for at in opening_times) if opening_ready else None,
            "signal_usable": opening_ready and provenance},
        "vwap": {"calculation_status": "PASS" if vwap is not None else "UNKNOWN", "value_krw": vwap,
            "reason_codes": vwap_reasons, "signal_usable": vwap is not None and provenance,
            "basis": "sum_explicit_executed_notional_krw_divided_by_observed_volume"},
        "calendar_verified_by_caller": calendar_verified, "source_finality_verified_by_caller": source_finality_verified,
        "flags": sorted(flags), "interpolated_minutes": 0, "missing_minute_cause": "UNKNOWN" if missing else None,
        "profitability_verified": False, "automatic_orders": False,
    }


def assess_strategy_claims(*, decision_at=None, features_available_at=None, assumed_entry_at=None,
                           universe_frozen_at=None, evaluation_start=None, return_measure=None,
                           presented_as_portfolio=False, source_code_available=False, trade_ledger_available=False):
    """Flag E1/lookahead, retrospective-universe and return-unit mistakes.

    Timestamps are supplied evidence, not authenticated provenance. Even a
    clean result is only ready for further review, never a validated strategy.
    Same-time assumed entry needs execution evidence; it is not declared
    physically impossible merely because a minute OHLC row cannot prove it.
    """
    for value in (presented_as_portfolio, source_code_available, trade_ledger_available):
        if type(value) is not bool:
            raise ValueError("boolean claim fields required")
    for value in (decision_at, features_available_at, assumed_entry_at, universe_frozen_at, evaluation_start):
        if value is not None:
            _aware(value)
    if return_measure not in (None, "trade_return_sum_percentage_points", "portfolio_nav_return", "realized_profit_over_buy_basis"):
        raise ValueError("explicit supported return measure required")
    blocking, unknown = [], ["INDEPENDENT_PERFORMANCE_VALIDATION_REQUIRED"]
    if decision_at is None or features_available_at is None:
        unknown.append("FEATURE_AVAILABILITY_UNVERIFIED")
    elif features_available_at > decision_at:
        blocking.append("FEATURES_NOT_AVAILABLE_AT_DECISION")
    if assumed_entry_at is None or decision_at is None:
        unknown.append("ENTRY_TIMING_UNVERIFIED")
    elif assumed_entry_at <= decision_at:
        blocking.append("SAME_OR_EARLIER_TIME_ASSUMED_FILL_UNVERIFIED")
    if universe_frozen_at is None or evaluation_start is None:
        unknown.append("POINT_IN_TIME_UNIVERSE_UNVERIFIED")
    elif universe_frozen_at > evaluation_start:
        blocking.append("RETROSPECTIVE_UNIVERSE_SELECTION")
    if presented_as_portfolio and return_measure != "portfolio_nav_return":
        blocking.append("RETURN_MEASURE_IS_NOT_PORTFOLIO_NAV")
    if return_measure is None:
        unknown.append("RETURN_MEASURE_UNSPECIFIED")
    if not source_code_available or not trade_ledger_available:
        unknown.append("TRADE_LEVEL_REPRODUCTION_UNAVAILABLE")
    return {"schema": "research-claim-quality-v1", "status": "BLOCKED" if blocking else "UNKNOWN",
            "blocking_reason_codes": blocking, "unverified_reason_codes": unknown,
            "validation_state": "NOT_VALIDATED_FOR_TRADING", "profitability_verified": False,
            "performance_verified": False, "automatic_orders": False}
