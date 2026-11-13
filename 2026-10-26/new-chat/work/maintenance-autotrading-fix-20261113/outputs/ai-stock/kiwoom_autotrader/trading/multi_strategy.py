"""Paper-only multi-strategy signal primitives.

These primitives do not submit orders. An order may only be submitted by an
integration that verifies position ownership and reserves it atomically in the
shared execution ledger. Missing market or ownership evidence means no order.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from copy import deepcopy
import re
import uuid
import hashlib
import json
from time import perf_counter_ns

from kiwoom_autotrader.api.minute_chart import fetch_minute_chart, fetch_session_minute_chart, session_identity
from kiwoom_autotrader.api.session import KST
from kiwoom_autotrader.config.settings import effective_multi_strategies, effective_paper_scan, effective_b3_guards, effective_theme_cap, effective_paper_funnel, effective_toss_hybrid, load_settings
from kiwoom_autotrader.trading.paper_strategy_context import PaperStrategyContextProvider, ContextUnavailable
from kiwoom_autotrader.api.account_report import fetch_account_report
from kiwoom_autotrader.trading.paper_reconciliation import _validate_holdings
from kiwoom_autotrader.trading.reentry_cooldown import reentry_allowed
from kiwoom_autotrader.trading.decision_evidence import strategy_source_hash
from kiwoom_autotrader.trading.strategy_signal_log import record_signal_safely

S2 = "S2_surge_high_breakout"
S3 = "S3_upper_limit_continuation"
S4 = "S4_theme_and_catalyst_leader"
from .steady_uptrend_vwap import S5, evaluate_steady_uptrend_vwap, validate_s5_risk, timed_exit
from .prearmed_decision import DecisionVersion, PrearmedDecisionCache
STRATEGIES = (S2, S3, S4, S5)

_DIAGNOSTIC_CODES = frozenset({'price_missing','price_invalid','price_conflict','ratio_invalid',
    'session_unavailable','session_changed','symbol_invalid','symbol_mismatch','market_closed',
    'request_stale','minute_rollover','clock_reversed','time_invalid','ranking_unavailable',
    'closed_bars_unavailable','bar_data_or_provenance_unverified','market_context_stale','exit_quote_stale',
    'ownership_unavailable','orderbook_unavailable','sma20_unavailable','cutoff_bar_unavailable','other'})

_DIAGNOSTIC_STAGES = frozenset({'unknown','ownership','broker_holdings','exit_quote','exit_sma',
    'entry_bars','entry_quote','entry_orderbook','strategy_signal','dormant_waiting_volume'})
_SAFE_ERROR_NAMES = frozenset({'ValueError','ContextUnavailable','MinuteChartError',
    'StockInfoError','ExecutionLogError','PaperReconciliationError','TypeError'})

# Observation-only diagnostics. Never retain arbitrary provider payloads or use
# these summaries as permission to trade; the evaluator and order gates own it.
_S5_DIAGNOSTIC_GROUPS = {
    'pattern': ('bullish_70pct', 'rising_closes_66pct', 'rising_lows_66pct',
        'ma3_above_ma7', 'return_1_to_4pct', 'close_drawdown_at_most_0_8pct',
        'volume_median_growth_1_1', 'last_bullish_body', 'last_upper_wick',
        'last_close_location', 'no_bar_above_1_5pct'),
    'vwap': ('vwap_valid', 'closed_price_above_vwap', 'current_price_above_vwap',
        'vwap_five_minute_slope_nonnegative'),
    'prior_daily': ('prior_daily_uptrend',),
    'risk': ('adr_below_6', 'dart_risk_review_clear', 'short_selling_five_sessions_clear'),
}
_S5_DIAGNOSTIC_REASONS = frozenset({
    'invalid_identity_or_time', 'current_price_unavailable', 'outside_entry_session',
    'five_minute_warmup', 'session_minutes_unavailable', 'future_session_minute',
    'incomplete_session_minutes', 'invalid_or_missing_session_minute',
    'prior_daily_unavailable', 'dart_risk_review_unavailable',
    'adr_overheated_or_unavailable', 'pattern_or_vwap_not_met',
    'old_missing_minutes_above_limit', 'recent_pattern_minutes_missing',
    'minute_timestamps_not_strictly_increasing', 'invalid_minute_provenance_or_ohlcv',
    'short_selling_evidence_unavailable', 'short_selling_pressure_high',
    'session_opening_anchor_unavailable', 'pattern_bucket_without_positive_volume',
}) | frozenset(key for group in _S5_DIAGNOSTIC_GROUPS.values() for key in group)

class _InputDiagnostic(ValueError):
    def __init__(self, code):
        super().__init__(code if code in _DIAGNOSTIC_CODES else 'other')
        self.code = code if code in _DIAGNOSTIC_CODES else 'other'


def _number(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return result if result.is_finite() else None


def _moment(value):
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return None
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        return None
    return value


def _visible_proof(proof, now):
    if not isinstance(proof, dict):
        return False
    available = _moment(proof.get("available_at"))
    digest = proof.get("source_sha256")
    return bool(available is not None and available <= now and isinstance(digest, str)
                and re.fullmatch(r"[0-9a-f]{64}", digest))


def _theme_or_disclosure(market, symbol, now):
    """Validate point-in-time evidence from an independently trusted provider."""
    dart = market.get("dart_evidence")
    if (_visible_proof(dart, now) and dart.get("verified") is True
            and dart.get("symbol") == symbol and isinstance(dart.get("receipt_no"), str)
            and re.fullmatch(r"[0-9]{14}", dart["receipt_no"])):
        return True
    theme = market.get("theme_evidence")
    if not _visible_proof(theme, now):
        return False
    taxonomy = _moment(theme.get("taxonomy_available_at"))
    members = theme.get("members")
    if taxonomy is None or taxonomy > now or not isinstance(members, list) or not 2 <= len(members) <= 2000:
        return False
    valid = {}
    for row in members:
        if not isinstance(row, dict):
            return False
        code, change, turnover = row.get("symbol"), _number(row.get("change_pct")), _number(row.get("turnover_krw"))
        observed = _moment(row.get("observed_at"))
        if (not isinstance(code, str) or re.fullmatch(r"[0-9]{6}", code) is None or code in valid
                or change is None or change <= 0 or turnover is None or turnover <= 0
                or row.get("rising_volume_surge") is not True or observed is None
                or not timedelta(0) <= now - observed <= timedelta(seconds=60)):
            return False
        valid[code] = (change, turnover)
    if symbol not in valid:
        return False
    top_change = max(row[0] for row in valid.values())
    top_turnover = max(row[1] for row in valid.values())
    return (valid[symbol] == (top_change, top_turnover)
            and sum(row[0] == top_change for row in valid.values()) == 1
            and sum(row[1] == top_turnover for row in valid.values()) == 1)


def valid_completed_bars(bars, symbol, now):
    """21 actual observations, at most two unknown omitted minutes, no synthesis."""
    from .observed_bar_window import actual_window
    try:
        actual_window(bars, symbol, now, 21, require_ohlcv=True)
        return True
    except (ValueError, TypeError, AttributeError, OverflowError):
        return False


def dormant_bar_reason(rows, symbol, now):
    """Classify only a successful, well-formed flat-entry chart shortage.

    Missing returned minutes do not prove zero trading. Source, time, price,
    adjustment and provenance failures remain errors, even with too few bars.
    Empty successful responses are data shortages, never volume assertions.
    """
    if not isinstance(rows, (list, tuple)) or _moment(now) is None or now.utcoffset() != timedelta(hours=9):
        return None
    if not rows:
        return 'no_completed_bars'
    starts = []
    for bar in rows:
        start = _moment(getattr(bar, 'start', None))
        query = _moment(getattr(bar, 'source_query_started_at', None))
        digest = getattr(bar, 'source_response_sha256', None)
        if (start is None or query is None or start.utcoffset() != timedelta(hours=9)
                or query.utcoffset() != timedelta(hours=9) or start.second or start.microsecond
                or start > now.replace(second=0, microsecond=0)
                or not timedelta(0) <= now-query <= timedelta(seconds=60)
                or query < start or getattr(bar, 'source_symbol', None) != symbol
                or getattr(bar, 'source_mode', None) != 'paper'
                or getattr(bar, 'gap_proof', None) is not None
                or getattr(bar, 'adjustment', None) not in (None, '')
                or getattr(bar, 'adjustment_rate', None) not in (None, '')
                or not isinstance(digest, str) or re.fullmatch(r'[0-9a-f]{64}', digest) is None):
            return None
        prices = [getattr(bar, key, None) for key in ('open_price','high_price','low_price','close')]
        volume = getattr(bar, 'volume', None)
        if (any(type(v) is not int or v <= 0 for v in prices)
                or not bar.low_price <= min(bar.open_price, bar.close) <= max(bar.open_price, bar.close) <= bar.high_price
                or type(volume) is not int or not 0 <= volume < 10**19):
            return None
        if start+timedelta(minutes=1) <= now and query < start+timedelta(minutes=1):
            return None
        starts.append(start)
    if any(b <= a for a, b in zip(starts, starts[1:])):
        return None
    completed = [b for b in rows if b.start.date() == now.date()
                 and (9,0) <= (b.start.hour,b.start.minute) < (15,30)
                 and b.start+timedelta(minutes=1) <= now]
    if len(completed) < 21:
        return 'insufficient_completed_history'
    recent = completed[-21:]
    gaps = [int((b.start-a.start).total_seconds()/60)-1 for a,b in zip(recent,recent[1:])]
    if any(gaps):
        first = recent[0]
        if (not isinstance(first.source_batch, str) or not first.source_batch or len(first.source_batch) > 128
                or type(first.source_page) is not int or first.source_page < 0
                or any(type(b.source_row) is not int or b.source_row < 0 for b in recent)
                or any(any(getattr(b,k) != getattr(first,k) for k in
                           ('source_batch','source_page','source_response_sha256','source_query_started_at')) for b in recent)
                or any(a.source_row-b.source_row != 1 for a,b in zip(recent,recent[1:]))):
            return None
    latest = now.replace(second=0,microsecond=0)-timedelta(minutes=1)
    if sum(gaps) > 2 or recent[-1].start < latest:
        return 'omitted_minutes_or_latest_bar'
    return None


@dataclass(frozen=True)
class EntryState:
    last_bar_start: datetime | None = None
    prior_peak: int | None = None
    pulled_back: bool = False
    previous_change_pct: Decimal | None = None


def next_entry_state(state, bar, market):
    """Advance only on a newly observed bar; never reconstruct old signals."""
    if not isinstance(state, EntryState):
        raise ValueError("invalid strategy state")
    now_bar = bar.start
    if state.last_bar_start is not None and now_bar <= state.last_bar_start:
        return state
    if state.last_bar_start is not None and now_bar - state.last_bar_start != timedelta(minutes=1):
        state = EntryState()
    high, close = bar.high_price, bar.close
    if type(high) is not int or high <= 0 or type(close) is not int or close <= 0 or close > high:
        raise ValueError("invalid completed bar")
    day_high = market.get("day_high")
    # Remember the session high known at this observation. It is a baseline,
    # never proof of a historical crossing or signal before observation.
    known_day_high = day_high if type(day_high) is int and day_high > 0 else high
    peak = max(state.prior_peak or high, high, known_day_high)
    pulled = bool(state.prior_peak and close < state.prior_peak
                  and (state.prior_peak - close) * 100 <= 3 * state.prior_peak)
    return EntryState(now_bar, peak, pulled, _number(market.get("change_pct")))


def s2_momentum_eligible(market, now):
    """Use this session's received ka10001 opening price, never previous close.

    Request/receipt age proves local retrieval only: ka10001 has no exchange
    quote timestamp, so source_freshness_verified remains False.
    """
    if not isinstance(market, dict) or _moment(now) is None:
        return False
    start, received = _moment(market.get('request_started_at')), _moment(market.get('available_at'))
    current, opening = market.get('current_price'), market.get('day_open')
    change = _number(market.get('change_pct'))
    return bool(start is not None and received is not None
        and start.date() == now.date() and start <= received <= now
        and now-start <= timedelta(seconds=5)
        and start.replace(second=0,microsecond=0) == now.replace(second=0,microsecond=0)
        and market.get('observed_at') == market.get('available_at')
        and market.get('source') == 'ka10001_normalized_response'
        and isinstance(market.get('source_sha256'),str)
        and re.fullmatch(r'[0-9a-f]{64}',market['source_sha256'])
        and type(current) is int and type(opening) is int and 0 < opening < current
        and change is not None and Decimal(3) <= change <= Decimal(15))


def entry_signal(strategy, state, bars, market, *, symbol, now):
    """Return a research-derived entry reason, or None; never sends orders.

    Context must have already been verified by the same-session provider; the
    caller additionally rechecks its age and ownership at the send boundary.
    """
    if strategy not in STRATEGIES or not isinstance(state, EntryState) or not isinstance(market, dict):
        return None
    if strategy == S5:
        if not isinstance(bars,(list,tuple)) or not bars or market.get('symbol') != symbol:
            return None
        # S5 is an observed-session/latest-boundary pattern, not an observed
        # minute-to-minute cross. Its own source validation proves the latest
        # completed 5-minute window even on the first visit or after rotation.
        # The monitor separately consumes (symbol, boundary) once.
        result = evaluate_steady_uptrend_vwap(bars,symbol=symbol,now=now,
            current_price=market.get('current_price'),daily_context=market.get('s5_daily_context'),
            risk_context=market.get('s5_risk_context'),require_short=True)
        return 'steady_uptrend_vwap' if result['eligible'] else None
    if not valid_completed_bars(bars, symbol, now) or market.get("symbol") != symbol:
        return None
    bar = bars[-1]
    # The first observation after a restart is baseline only. A skipped minute
    # also resets signal continuity; observations are not invented.
    if state.last_bar_start is None or bar.start - state.last_bar_start != timedelta(minutes=1):
        return None
    change = _number(market.get("change_pct"))
    price = bar.close
    if strategy == S2:
        volumes = [b.volume for b in bars[-21:-1]]
        if (market.get("rising_volume_surge") is True and s2_momentum_eligible(market, now)
                and state.prior_peak and price > state.prior_peak and state.pulled_back
                and sum(volumes) > 0 and 40 * bar.volume >= 3 * sum(volumes)):
            return "shallow_pullback_high_breakout"
    elif strategy == S3:
        limit = market.get("upper_limit_price")
        current = market.get("current_price")
        if (state.previous_change_pct is not None and state.previous_change_pct < 20
                and change is not None and Decimal(20) <= change <= Decimal(28)
                and type(limit) is int and limit > 0 and type(current) is int
                and 0 < current <= limit):
            return "observed_20pct_crossing"
    elif strategy == S4:
        # The present provider supplies neither independently authenticated
        # theme breadth nor DART evidence, so normal operation remains blocked.
        if state.prior_peak and price > state.prior_peak and _theme_or_disclosure(market, symbol, now):
            return "theme_leader_or_verified_disclosure"
    return None


def exit_signal(strategy, *, qty, basis_krw, price, sma20, half_taken=False):
    average = _number(sma20)
    if (strategy not in STRATEGIES or any(type(x) is not int or x <= 0
            for x in (qty, basis_krw, price)) or (sma20 is not None and (average is None or average <= 0))
            or type(half_taken) is not bool):
        return None
    gain = price * qty * 10000 - basis_krw * 10000
    if gain <= -300 * basis_krw or average is not None and Decimal(price) < average:
        return ("stop_loss_or_sma20", qty)
    if gain >= 1000 * basis_krw:
        return ("take_profit_10pct", qty)
    if strategy == S2 and not half_taken and gain >= 500 * basis_krw and qty >= 2:
        return ("take_profit_half_5pct", qty // 2)
    return None


def valid_sma_bars(bars, symbol, now):
    """SMA20 is the last 20 real closes, bounded to at most 22 calendar minutes."""
    from .observed_bar_window import actual_window
    try:
        actual_window(bars, symbol, now, 20)
        return True
    except (ValueError, TypeError, AttributeError, OverflowError):
        return False


def cutoff_bar(bars, symbol, now):
    """Return the actual completed 15:19 bar, never an inferred closing price."""
    if not isinstance(bars, (list, tuple)) or _moment(now) is None or now.strftime("%H:%M") < "15:20":
        return None
    target = now.replace(hour=15, minute=19, second=0, microsecond=0)
    matches = [bar for bar in bars if getattr(bar, "start", None) == target]
    if len(matches) != 1:
        return None
    bar = matches[0]
    query = _moment(getattr(bar, "source_query_started_at", None))
    digest = getattr(bar, "source_response_sha256", None)
    if (query is None or not target + timedelta(minutes=1) <= query <= now
            or getattr(bar, "source_symbol", None) != symbol or getattr(bar, "source_mode", None) != "paper"
            or getattr(bar, "gap_proof", None) is not None
            or getattr(bar, "adjustment", None) not in (None, "")
            or getattr(bar, "adjustment_rate", None) not in (None, "")
            or not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None
            or type(getattr(bar, "close", None)) is not int or bar.close <= 0):
        return None
    return bar


def fresh_owned_basis(session, symbol, position, account_fetcher):
    """Derive owned cash basis from fresh broker report, never the ledger alone."""
    if (not isinstance(position, dict) or position.get("status") != "owned"
            or type(position.get("owned_qty")) is not int or position["owned_qty"] <= 0):
        raise ValueError("strategy_ownership_unavailable")
    report = account_fetcher(session)
    held, sellable, purchase, unknown = _validate_holdings(report)
    rows = [row for row in report["holdings"] if row["stk_cd"][-6:] == symbol]
    if (symbol not in held or symbol in unknown or symbol not in sellable or len(rows) != 1
            or rows[0]["stk_cd"].startswith(("J", "Q")) or rows[0]["crd_tp"] != "00"
            or int(rows[0]["rmnd_qty"]) != position["owned_qty"]
            or type(purchase.get(symbol)) is not int or purchase[symbol] <= 0):
        raise ValueError("broker_position_mismatch")
    return purchase[symbol], sellable[symbol]


def execute_paper_strategy(*, session, tracker, strategy, symbol, side, reason, quantity, budget,
                           position, bars, market, baseline, observed_at, before_send,
                           run_id, source_hash, b3_config=None, b3_add_on=None):
    """Submit only through the existing paper automation and reconciliation path."""
    from kiwoom_autotrader.telegram.commands.orders import OrderReplies, handle_buy, handle_sell
    from kiwoom_autotrader.trading.multi_strategy_evidence import multi_strategy_evidence
    from kiwoom_autotrader.trading.order_context import automated_orders
    if (getattr(session, "mode", None) != "paper" or strategy not in STRATEGIES
            or side not in ("buy", "sell") or not callable(before_send)
            or not callable(getattr(tracker, "multi_strategy_position", None))
            or not callable(getattr(tracker, "multi_strategy_open_symbols", None))
            or not callable(getattr(tracker, "assert_multi_exit_dispatch", None))):
        return OrderReplies([], submitted=False, decision_reason="strategy_ownership_gateway_unavailable")
    try:
        before_send()
        evidence = multi_strategy_evidence(strategy=strategy, symbol=symbol, side=side, reason=reason,
            budget=budget, position=position, bars=bars, market=market, baseline=baseline,
            observed_at=observed_at, quantity=quantity, run_id=run_id, source_hash=source_hash,
            b3_config=b3_config, b3_add_on=b3_add_on)
    except Exception:
        return OrderReplies([], submitted=False, decision_reason="strategy_evidence_or_context_unavailable")
    with automated_orders(tracker, strategy, evidence=evidence):
        if side == "buy":
            return handle_buy(f"buy {symbol} max {budget}", session, before_send=before_send)
        return handle_sell(f"sell {symbol} {quantity}", session, before_send=before_send)


class MultiStrategyMonitor:
    """Receiver-thread monitor; execution requires a separately proven gateway.

    `executor` is an injected execution gateway. A production gateway must
    reconcile broker holdings, reserve strategy ownership atomically, and run
    the existing automated_orders and handle_buy/handle_sell path. No gateway
    is installed by default, so unavailable ownership cannot create a position.
    """
    def __init__(self, *, settings_loader=None, chart_fetcher=None, provider_factory=None, exit_provider_factory=None,
                 executor=None, now=None, stop_requested=None, cooldown_check=None, account_fetcher=None,
                 fast_pipeline=None):
        self._settings = settings_loader or load_settings
        self._chart = chart_fetcher or fetch_minute_chart
        self._session_chart = chart_fetcher or fetch_session_minute_chart
        self._provider_factory = provider_factory or (lambda session: PaperStrategyContextProvider(
            session, enable_surge=True,
            enable_toss_hybrid=effective_toss_hybrid(self._settings(), 'paper'),
            enable_prearmed_risk=self._settings().get('paper_fast_decision_enabled') is True))
        self._exit_provider_factory = exit_provider_factory or (lambda session: PaperStrategyContextProvider(session, exit_only=True))
        self._executor = executor
        self._run_id = uuid.uuid4().hex
        self._source_hash = strategy_source_hash(__file__)
        self._cooldown = cooldown_check or reentry_allowed
        self._account = account_fetcher or fetch_account_report
        self._now = now or (lambda: datetime.now(KST))
        self._stop = stop_requested or (lambda: False)
        self._identity = None
        self._provider = None
        self._exit_provider = None
        self._states = {}
        self._scan = []
        self._scan_at = None
        self._last_symbol = None
        self._next_at = None
        self._enabled = False
        self._halted = False
        self._resume_requested = False
        self._error = None
        self._symbol_errors = {}
        self._symbol_input_diagnostics = {}
        self._symbol_last_validated_at = {}
        self._s5_input_diagnostics = {}
        self._s5_attempted = {}
        self._s5_evaluated = {}
        self._s5_risk_symbols = None
        self._s5_schedule = {}
        self._overnight_positions = {}
        self._flat_candidates = {}
        self._last_decision = None
        self._entry_budgets = {}
        self._discovery_at = None
        self._discovery_binding = None
        self._discovery = None
        self._seen_this_minute = set()
        self._last_entry_symbol = None
        self._universe = set()
        self._scan_diagnostics = {}
        self._bar_window_diagnostics = {}
        self._research_log_status = None
        self._active_entries = {}
        self._b3_add_on_diagnostics = {}
        self._dormant_symbols = {}
        self._entry_admission = {}
        self._funnel_first_seen = {}
        self._funnel_last_attempt = {}
        self._orderbook_diagnostics = {}
        self._prearmed = PrearmedDecisionCache(max_entries=40)
        self._prearmed_diagnostics = {}
        self._fast_pipeline = fast_pipeline

    def reset(self):
        closer = getattr(self._provider, 'close_prearmed', None)
        if callable(closer):
            try:
                closer()
            except Exception:
                pass  # A background advisory source cannot prevent reset/exit.
        if self._fast_pipeline is not None:
            self._fast_pipeline.reset()
        self._prearmed.clear()
        self._prearmed_diagnostics.clear()
        self._identity = None
        self._provider = None
        self._exit_provider = None
        self._states.clear()
        self._s5_input_diagnostics.clear()
        self._s5_attempted.clear()
        self._s5_evaluated.clear()
        self._s5_risk_symbols = None
        self._s5_schedule.clear()
        self._overnight_positions.clear()
        self._flat_candidates.clear()
        self._scan.clear()
        self._scan_at = None
        self._next_at = None
        self._enabled = False
        self._entry_budgets.clear()
        self._discovery_at = None
        self._discovery_binding = None
        self._discovery = None
        self._seen_this_minute.clear()
        self._last_entry_symbol = None
        self._universe.clear()
        self._scan_diagnostics.clear()
        self._bar_window_diagnostics.clear()
        self._active_entries.clear()
        self._b3_add_on_diagnostics.clear()
        for symbol in self._dormant_symbols:
            if self._symbol_input_diagnostics.get(symbol, {}).get('stage') == 'dormant_waiting_volume':
                self._symbol_input_diagnostics.pop(symbol, None)
        self._dormant_symbols.clear()
        self._entry_admission.clear()
        self._funnel_first_seen.clear()
        self._funnel_last_attempt.clear()
        self._orderbook_diagnostics.clear()

    def seconds_until_next(self):
        if not self._enabled:
            return None
        now = self._now()
        return max(0.0, (self._next_at - now).total_seconds()) if self._next_at else 1.0

    def status(self):
        return {"enabled": self._enabled, "halted": self._halted, "error": self._error,
                "resume_requested": self._resume_requested,
                "configured_strategy_ids": list(self._identity[1]) if self._identity is not None else [],
                "symbol_errors": deepcopy(self._symbol_errors),
                "symbol_input_diagnostics": deepcopy(self._symbol_input_diagnostics),
                "symbol_last_validated_at": deepcopy(self._symbol_last_validated_at),
                "s5_input_diagnostics": deepcopy(self._s5_input_diagnostics),
                "s5_schedule": deepcopy(self._s5_schedule),
                "overnight_unobserved_positions": deepcopy(self._overnight_positions),
                "flat_candidates": deepcopy(self._flat_candidates),
                "last_decision": deepcopy(self._last_decision),
                "dynamic_scan": deepcopy(self._scan_diagnostics),
                "candidate_discovery": deepcopy(self._discovery),
                "bar_window_diagnostics": deepcopy(self._bar_window_diagnostics),
                "b3_add_on_policy": "same_strategy_owner_once_daily_two_buys_verified_post_fill_path",
                "b3_add_on_diagnostics": deepcopy(self._b3_add_on_diagnostics),
                "dormant_symbols": deepcopy(self._dormant_symbols),
                "funnel_orderbooks": deepcopy(self._orderbook_diagnostics),
                "prearmed_decisions": deepcopy(self._prearmed_diagnostics),
                "prearmed_cache": self._prearmed.status(),
                "fast_decision_pipeline": self._fast_pipeline.status() if self._fast_pipeline is not None else None,
                "research_signal_log": deepcopy(self._research_log_status),
                "next_check_at": self._next_at.isoformat() if self._next_at else None}

    def _prepare_s5(self, *, session, symbol, bars, market, evaluation, observed, settings, positions, budget):
        """Preparation stage only. No additional broker/DB reads or authority.

        The ownership digest is explicitly an observed snapshot, not a current
        ledger index. Therefore cash, theme capacity and remaining principal are
        UNKNOWN here; actual dispatch still runs every canonical safety gate.
        """
        started = perf_counter_ns()
        session_key = hashlib.sha256((self._run_id+':'+str(id(session))+':'+observed.date().isoformat()).encode()).hexdigest()
        try:
            def digest(value):
                return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                    ensure_ascii=True, allow_nan=False).encode()).hexdigest()
            bar_inputs = [(bar.start.isoformat(),bar.open_price,bar.high_price,bar.low_price,bar.close,
                bar.volume,bar.source_symbol,bar.source_mode,bar.source_query_started_at.isoformat(),
                bar.source_response_sha256) for bar in bars]
            version = DecisionVersion(session_key, digest(positions),
                digest({'bars':bar_inputs,'market':market,'evaluation':evaluation}),
                digest(settings), market.get('current_price'))
            expires = min(observed+timedelta(seconds=5), observed.replace(second=0,microsecond=0)+timedelta(minutes=1))
            risk = market.get('s5_risk_context')
            risk_expiry = _moment(risk.get('expires_at')) if isinstance(risk,dict) else None
            if risk_expiry is not None:
                expires = min(expires,risk_expiry)
            prepared = self._prearmed.arm(symbol,S5,version=version,evaluation=evaluation,
                now=observed,expires_at=expires,initial_budget_krw=min(500000,budget))
            ticket = prepared.ticket
            self._prearmed_diagnostics[symbol] = {'stage':'prepared' if ticket else 'fallback',
                'observed_at':observed.isoformat(),'preparation_ms':(perf_counter_ns()-started)/1e6,
                'lookup_ms':None,'strategy_eligible':ticket.strategy_eligible if ticket else False,
                'estimated_initial_qty_upper_bound':ticket.estimated_qty if ticket else None,
                'remaining_principal_krw':None,'theme_remaining_slots':None,
                'ledger_index_authoritative':False,'cash_and_reservations_verified':False,
                'order_permission':False,'requires_fresh_reconciliation':True,
                'cache_reason':prepared.reason}
            if self._fast_pipeline is None:
                from .fast_decision_pipeline import FastDecisionCoordinator
                self._fast_pipeline = FastDecisionCoordinator()
            self._fast_pipeline.observe(market=market,evaluation=evaluation,symbol=symbol,
                observed=observed,session_id=session_key,budget=budget)
            read_account=getattr(self._fast_pipeline,'account_for',None)
            account=read_account(symbol,observed) if callable(read_account) else None
            if account:
                from .fast_decision_pipeline import estimated_order_ticket
                self._prearmed_diagnostics[symbol]['account_preparation']=estimated_order_ticket(
                    account,market.get('current_price'),budget)
            return version if ticket else None
        except Exception:
            self._prearmed.invalidate(symbol,S5)
            self._prearmed_diagnostics[symbol] = {'stage':'deterministic_fallback',
                'observed_at':observed.isoformat(),'preparation_ms':(perf_counter_ns()-started)/1e6,
                'lookup_ms':None,'order_permission':False,'requires_fresh_reconciliation':True,
                'cache_reason':'preparation_unavailable'}
            return None

    def _s5_prearmed_signal(self, previous, bars, market, *, symbol, now, version):
        """Reuse one already validated same-observation evaluation only."""
        started = perf_counter_ns()
        lookup = self._prearmed.lookup(symbol,S5,current_version=version,now=now)
        diagnostic = self._prearmed_diagnostics.get(symbol)
        if diagnostic is not None:
            diagnostic.update(lookup_ms=(perf_counter_ns()-started)/1e6,cache_reason=lookup.reason)
        ticket = lookup.ticket
        if (lookup.status != 'HIT' or ticket.prepared_at != now or ticket.version != version
                or market.get('symbol') != symbol or market.get('current_price') != version.price):
            return False, None
        # The ticket binds a freshly validated complete-session observation.
        # Prior polling cadence is irrelevant to this S5-only pattern; the
        # caller's boundary work ledger prevents repeated evaluation/entry.
        return True, 'steady_uptrend_vwap' if ticket.strategy_eligible else None

    def _refresh_entries(self, normalized, options, now, minute, settings, protected_symbols=(), *, tracker=None, session=None):
        """At most one discovery after this minute's exits, never per symbol."""
        self._discovery_at = minute
        if effective_paper_funnel(settings, 'paper')['enabled']:
            self._refresh_funnel_entries(normalized, options, now, minute, settings,
                protected_symbols, tracker=tracker, session=session)
            self._prioritize_s5_work(tuple(dict.fromkeys([c for c,_ in normalized] + [c for c,_ in options['stocks']])),
                                     now, minute, settings, protected_symbols)
            return
        budgets = dict(normalized)
        extra = dict(options['stocks'])
        expanded = options['enabled'] or bool(extra)
        pool_limit = options.get('entry_pool_limit', 30)
        if type(pool_limit) is not int or not 1 <= pool_limit <= 40:
            raise ValueError('invalid_entry_pool_limit')
        pct, _ = effective_b3_guards(settings, 'paper')
        for code, budget in extra.items():
            budgets.setdefault(code, min(1_000_000, budget) * pct // 100)
        # Preserve the existing legacy gdcrs-only configured scan. The bounded
        # candidate pool applies when dynamic or additional entries are enabled.
        if expanded and len(budgets) > pool_limit:
            raise ValueError('configured_entry_pool_exceeds_limit')
        fixed_symbols = tuple(budgets)
        admission = {code:{'kind':'configured_base', 'initial_budget_krw':budget}
                     for code,budget in budgets.items()}
        excluded_capacity = []
        self._discovery = None
        if (options['enabled'] or extra) and now.strftime('%H:%M') < '15:19':
            discover = getattr(self._provider, 'discover_dynamic_candidates', None)
            if not callable(discover):
                raise _InputDiagnostic('ranking_unavailable')
            result = discover(as_of=now, limit=options['limit'], configured_symbols=tuple(extra))
            received = self._now().astimezone(KST)
            available = _moment(result.get('available_at')) if isinstance(result, dict) else None
            if (not isinstance(result, dict) or result.get('schema') != 'paper-dynamic-candidates-v1'
                    or result.get('status') not in ('ready', 'unavailable')
                    or available is None or not 0 <= (received-available).total_seconds() <= 60
                    or available.replace(second=0, microsecond=0) != minute
                    or received.replace(second=0, microsecond=0) != minute
                    or not isinstance(result.get('session_binding'), str) or not result['session_binding']
                    or self._discovery_binding is not None and result['session_binding'] != self._discovery_binding):
                raise _InputDiagnostic('ranking_unavailable')
            self._discovery_binding = result['session_binding']
            self._discovery = deepcopy(result)
            if result['status'] == 'ready':
                rows = result.get('candidates', []) if options['enabled'] else []
                base = result.get('base_candidates', [])
                if (not isinstance(rows, list) or len(rows) > options['limit']
                        or not isinstance(base, list) or len(base) > len(extra)):
                    raise _InputDiagnostic('ranking_unavailable')
                for row in base + rows:
                    code = row.get('symbol') if isinstance(row, dict) else None
                    stamp = _moment(row.get('available_at')) if isinstance(row, dict) else None
                    if (not isinstance(code, str) or re.fullmatch(r'[0-9]{6}', code) is None
                            or stamp is None or stamp > received or received-stamp > timedelta(seconds=60)
                            or not isinstance(row.get('membership_evidence'), dict)
                            or not row.get('source_hashes')):
                        raise _InputDiagnostic('ranking_unavailable')
                    if row in base and code not in extra:
                        raise _InputDiagnostic('ranking_unavailable')
                    # Discovered symbols use the configured initial allocation;
                    # a future add requires separate proven B3 ownership.
                    if code not in budgets and len(budgets) >= pool_limit:
                        excluded_capacity.append(code)
                        continue
                    budgets.setdefault(code, min(1_000_000, extra.get(code, 1_000_000)) * pct // 100)
                    toss_only = row.get('candidate_basis') == 'toss_hybrid_verified_watch'
                    admission.setdefault(code, {'kind':'verified_toss_watch' if toss_only else 'verified_discovery',
                        'track':row.get('track'),
                        'available_at':stamp.isoformat(), 'session_binding':result['session_binding'],
                        'source_hashes':deepcopy(row['source_hashes']),
                        'toss_evidence':deepcopy(row.get('toss_evidence')) if toss_only else None,
                        'toss_session_day':result.get('toss_discovery',{}).get('session_day'),
                        'toss_previous_session_day':result.get('toss_discovery',{}).get('previous_session_day')})
        self._entry_budgets = budgets
        self._entry_admission = admission
        added = set(budgets) - self._universe
        removed = self._universe - set(budgets)
        # Removal never erases an owned position. Re-enrollment starts a new
        # baseline, so a past crossing is not replayed after a ranking gap.
        for code in added - self._seen_this_minute:
            for strategy in STRATEGIES:
                self._states.pop((code, strategy), None)
        for code in removed - set(protected_symbols):
            for strategy in STRATEGIES:
                self._states.pop((code, strategy), None)
            for mapping in (self._flat_candidates, self._dormant_symbols, self._symbol_errors,
                            self._symbol_input_diagnostics, self._symbol_last_validated_at, self._s5_input_diagnostics,
                            self._bar_window_diagnostics, self._active_entries):
                mapping.pop(code, None)
        self._universe = set(budgets)
        rotation = list(budgets)
        if self._last_entry_symbol in rotation:
            offset = rotation.index(self._last_entry_symbol) + 1
            rotation = rotation[offset:] + rotation[:offset]
        if expanded:
            # Keep a small forward-observed subset for three consecutive minute
            # slots: baseline, possible pullback, possible fresh breakout. A
            # 30-symbol round-robin alone would miss every adjacent-bar signal.
            # Missing input is still missing, never replayed as an old crossing.
            active = {code:started for code,started in self._active_entries.items()
                      if code in budgets and timedelta(0) <= minute-started < timedelta(minutes=3)}
            for code in rotation:
                if len(active) >= 4:
                    break
                active.setdefault(code, minute)
            self._active_entries = active
            rotation = [code for code in rotation if code in active]
        self._scan = [code for code in rotation if code not in self._seen_this_minute]
        self._scan_diagnostics = {'minute': minute.isoformat(), 'candidate_count': len(budgets),
            'new_symbols': sorted(added), 'removed_entry_symbols': sorted(removed),
            'remaining': len(self._scan), 'observed_symbols': sorted(self._seen_this_minute),
            'entry_batch_size': 1, 'full_universe_every_minute_guaranteed': False,
            'max_concurrent_positions': options['max_positions'],
            'entry_pool_limit':pool_limit if expanded else None, 'entry_pool_limit_applied':expanded,
            'configured_entry_symbols':list(fixed_symbols),
            'configured_entry_count':len(fixed_symbols),
            'dynamic_entry_count':len(budgets)-len(fixed_symbols),
            'entry_membership_unverified':[code for code in budgets if code not in admission],
            'owned_exit_symbols_outside_entry_cap':[code for code in protected_symbols if code not in budgets],
            'active_entry_symbols':list(self._active_entries) if expanded else list(budgets),
            'active_subset_max':4 if expanded else len(budgets),
            'active_subset_retention_minutes':3 if expanded else None,
            'queued_not_observed': [code for code in budgets if code not in rotation and code not in self._seen_this_minute],
            'pool_capacity_exclusions':excluded_capacity}
        self._prioritize_s5_work(fixed_symbols, now, minute, settings, protected_symbols)

    @staticmethod
    def _s5_boundary(now):
        return now.replace(minute=(now.minute // 5) * 5, second=0, microsecond=0)

    def _s5_due(self, symbol, now, ids):
        return (S5 in ids and self._s5_attempted.get(symbol) != self._s5_boundary(now)
                and (self._s5_risk_symbols is None or symbol in self._s5_risk_symbols))

    def _prioritize_s5_work(self, configured, now, minute, settings, protected_symbols):
        """Forward-only five-minute workset; owns no market or order permission.

        Fresh eligible configured symbols are independent of the S2~S4 four-
        symbol, three-minute continuity subset. Work surviving a minute rollover
        is selected again only if it was never attempted in THIS boundary. At
        the next boundary the old workset is discarded, never replayed.
        """
        ids = effective_multi_strategies(settings, 'paper')
        if S5 not in ids:
            self._s5_risk_symbols = None
            self._s5_schedule = {}
            return
        boundary = self._s5_boundary(now)
        previous = self._s5_schedule
        old_pending = previous.get('pending_symbols', []) if previous.get('boundary') != boundary.isoformat() else []
        self._s5_attempted = {code:stamp for code,stamp in self._s5_attempted.items() if stamp >= boundary}
        self._s5_evaluated = {code:stamp for code,stamp in self._s5_evaluated.items() if stamp >= boundary}
        reader = getattr(self._provider, 's5_eligible_symbols', None)
        status = 'risk_scheduler_unavailable_legacy_rotation'
        self._s5_risk_symbols = None
        if callable(reader):
            try:
                values = reader(as_of=now)
                if (not isinstance(values, tuple) or len(values) > 1000
                        or any(not isinstance(code,str) or not re.fullmatch(r'[0-9]{6}',code) for code in values)
                        or len(set(values)) != len(values)):
                    raise ValueError('invalid_s5_risk_schedule')
                self._s5_risk_symbols = set(values)
                status = 'ready' if values else 'risk_eligible_empty'
            except Exception:
                self._s5_risk_symbols = set()
                status = 'risk_snapshot_unavailable'
        funnel_enabled = effective_paper_funnel(settings,'paper')['enabled']
        eligible = [code for code in configured if self._s5_risk_symbols is not None
                    and code in self._s5_risk_symbols and code in self._entry_budgets
                    and (not funnel_enabled or code in self._active_entries)
                    and code not in protected_symbols]
        pending = [code for code in eligible if self._s5_attempted.get(code) != boundary]
        ordinary = self._scan
        if ids == (S5,):
            ordinary = [code for code in ordinary if self._s5_due(code,now,ids)]
        self._scan = list(dict.fromkeys(code for code in pending + ordinary
                                       if code not in self._seen_this_minute))
        self._s5_schedule = {'boundary':boundary.isoformat(), 'expires_at':(boundary+timedelta(minutes=5)).isoformat(),
            'status':status, 'configured_symbols':list(configured), 'eligible_symbols':eligible,
            'attempted_symbols':[code for code in eligible if self._s5_attempted.get(code)==boundary],
            'evaluated_symbols':[code for code in eligible if self._s5_evaluated.get(code)==boundary],
            'pending_symbols':pending, 'previous_boundary_unattempted_discarded':old_pending,
            'owned_exit_priority':True, 'historical_boundary_replay_allowed':False,
            'coverage_guaranteed':False, 'deadline_policy':'latest_boundary_only_no_same_boundary_retry'}
        self._scan_diagnostics.update(remaining=len(self._scan), s5_boundary=boundary.isoformat(),
            s5_priority_symbols=pending, s5_risk_eligible_count=len(eligible))

    def _s5_mark_attempt(self, symbol, now):
        boundary = self._s5_boundary(now)
        self._s5_attempted[symbol] = boundary
        if self._s5_schedule.get('boundary') == boundary.isoformat():
            targets = self._s5_schedule.get('eligible_symbols', [])
            self._s5_schedule.update(
                attempted_symbols=[code for code in targets if self._s5_attempted.get(code)==boundary],
                pending_symbols=[code for code in targets if self._s5_attempted.get(code)!=boundary])

    def _refresh_funnel_entries(self, normalized, options, now, minute, settings,
                                protected_symbols, *, tracker, session):
        """Owned management already ran; all remaining filters are in memory.

        Hot pool is at most 15; opt-in deep work is bounded to eight symbols
        with three-minute retention and a 45-second new-request deadline. Neither list promises complete per-minute
        coverage. Fresh atomic money/position checks remain in the executor.
        """
        from .paper_funnel_state import publish_gate
        from .theme_concentration import funnel_theme_reason
        funnel = effective_paper_funnel(settings, 'paper')
        self._entry_budgets, self._entry_admission, self._scan = {}, {}, []
        self._discovery = None
        publish_gate(session, now, [], available=False)
        fixed = dict(normalized)
        pct, _ = effective_b3_guards(settings, 'paper')
        for code, budget in options['stocks']:
            fixed.setdefault(code, min(1_000_000, budget)*pct//100)
        priority = tuple(dict.fromkeys((*fixed, *funnel['priority_symbols'])))
        self._scan_diagnostics = {'minute':minute.isoformat(), 'funnel_enabled':True,
            'status':'awaiting_verified_rank_and_portfolio', 'hot_limit':funnel['hot_limit'],
            'full_universe_every_minute_guaranteed':False, 'entry_batch_size':1,
            'active_subset_max':8, 'active_subset_retention_minutes':3,
            'entry_start_deadline_second':45,
            'entry_detail_request_upper_bound':24, 'rank_request_upper_bound':2,
            'entry_detail_calls':{'ka10080':8,'ka10001':8,'ka10004':8},
            'all_hot_orderbook_observed':False,
            'orderbook_scope':'active_entry_top1_strict5s_positive_bid_ask_sizes',
            'membership_refresh_request_upper_bound':1,
            'owned_requests_outside_entry_budget':True,
            'legacy_gdcrs_dynamic_adapter_added':True,
            'b3_scope':'verified_hot_gdcrs_initial_and_same_owner_multi_strategy_lots',
            'configured_entry_symbols':list(fixed), 'configured_entry_count':len(fixed),
            'max_concurrent_positions':options['max_positions'],
            'order_authorization':False}
        snapshotter = getattr(tracker, 'paper_theme_capacity_snapshot', None)
        discover = getattr(self._provider, 'discover_funnel_candidates', None)
        if not callable(snapshotter) or not callable(discover):
            self._scan_diagnostics['status'] = 'portfolio_or_ranking_provider_unavailable'
            raise _InputDiagnostic('ranking_unavailable')
        snapshot = snapshotter(now)
        observed = _moment(snapshot.get('observed_at')) if isinstance(snapshot,dict) else None
        as_of = _moment(snapshot.get('as_of')) if isinstance(snapshot,dict) else None
        occupied = snapshot.get('occupied_symbols') if isinstance(snapshot,dict) else None
        if (not isinstance(snapshot,dict) or snapshot.get('mode') != 'paper'
                or as_of is None or not timedelta(0) <= now-as_of <= timedelta(seconds=5)
                or as_of.replace(second=0,microsecond=0) != minute
                or observed is None or observed.date()!=now.date()
                or not timedelta(0) <= now-observed <= timedelta(seconds=90)
                or not isinstance(occupied,list)
                or any(not isinstance(code,str) or re.fullmatch(r'[0-9]{6}',code) is None for code in occupied)):
            self._scan_diagnostics['status'] = 'portfolio_snapshot_unavailable'
            raise _InputDiagnostic('ownership_unavailable')
        if now.strftime('%H:%M') >= '15:19':
            self._scan_diagnostics['status'] = 'entry_cutoff_reached'
            return
        result = discover(as_of=now, configured_symbols=priority)
        received = self._now().astimezone(KST)
        available = _moment(result.get('available_at')) if isinstance(result,dict) else None
        if (not isinstance(result,dict) or result.get('schema') != 'paper-funnel-candidates-v1'
                or result.get('status') not in ('ready','unavailable')
                or available is None or not timedelta(0) <= received-available <= timedelta(seconds=60)
                or available.replace(second=0,microsecond=0) != minute
                or received.replace(second=0,microsecond=0) != minute
                or received-observed > timedelta(seconds=90)
                or not isinstance(result.get('session_binding'),str) or not result['session_binding']
                or self._discovery_binding is not None and result['session_binding']!=self._discovery_binding):
            raise _InputDiagnostic('ranking_unavailable')
        self._discovery_binding = result['session_binding']
        self._discovery = deepcopy(result)
        self._scan_diagnostics['request_accounting'] = deepcopy(result.get('request_accounting'))
        if result['status'] != 'ready':
            self._scan_diagnostics['status'] = result.get('reason','ranking_unavailable')
            return
        rows = result.get('candidates')
        if not isinstance(rows,list) or len(rows)>10000:
            raise _InputDiagnostic('ranking_unavailable')
        eligible, exclusions, seen = [], [], set()
        limit = snapshot.get('position_limit')
        if type(limit) is not int or not 0 <= limit <= 10:
            raise _InputDiagnostic('ownership_unavailable')
        for row in rows:
            code = row.get('symbol') if isinstance(row,dict) else None
            stamp = _moment(row.get('available_at')) if isinstance(row,dict) else None
            change = _number(row.get('change_pct')) if isinstance(row,dict) else None
            surge = _number(row.get('surge_rate_pct')) if isinstance(row,dict) else None
            amount = row.get('turnover_krw') if isinstance(row,dict) else None
            tracked=isinstance(row,dict) and row.get('track') in ('T1_PRECURSOR','T2_MOMENTUM')
            if tracked:
                from .paper_funnel_state import _candidate_projection
                from .dynamic_s1_admission import validate_track_candidate
                try:validate_track_candidate(_candidate_projection(row),symbol=code,now=received)
                except (ValueError,TypeError,KeyError,InvalidOperation,OverflowError):
                    raise _InputDiagnostic('ranking_unavailable') from None
            if (not isinstance(code,str) or re.fullmatch(r'[0-9]{6}',code) is None or code in seen
                    or stamp is None or not timedelta(0)<=received-stamp<=timedelta(seconds=60)
                    or not tracked and (change is None or not 4<=change<=28 or surge is None or surge<=0)
                    or type(amount) is not int or amount<=0
                    or not isinstance(row.get('membership_evidence'),dict) or not row.get('source_hashes')
                    or not isinstance(row.get('liquidity_evidence'),dict)):
                raise _InputDiagnostic('ranking_unavailable')
            seen.add(code)
            reason = ('position_already_occupied_priority_managed' if code in occupied else
                      'concurrent_position_limit_reached' if limit and len(set(occupied))>=limit else
                      funnel_theme_reason(code,snapshot))
            if reason:
                exclusions.append({'symbol':code,'reason':reason})
            else:
                eligible.append(row)
        eligible.sort(key=lambda row:(-row['turnover_krw'], -int(row['symbol'] in priority),
                                      -int(row.get('is_core_leader') is True), row['symbol']))
        hot = eligible[:funnel['hot_limit']]
        budgets = {row['symbol']:fixed.get(row['symbol'],1_000_000*pct//100) for row in hot}
        admission = {row['symbol']:{'kind':'verified_toss_watch' if row.get('toss_evidence') else 'verified_discovery',
            'track':row.get('track'),
            'available_at':row['available_at'],'session_binding':result['session_binding'],
            'source_hashes':deepcopy(row['source_hashes']),
            'toss_evidence':deepcopy(row.get('toss_evidence')),
            'toss_session_day':row.get('toss_session_day'),
            'toss_previous_session_day':row.get('toss_previous_session_day')} for row in hot}
        old = self._universe
        for code in (old-set(budgets)) | (set(budgets)-old):
            if code not in protected_symbols:
                for strategy in STRATEGIES:
                    self._states.pop((code,strategy),None)
        self._universe = set(budgets)
        self._entry_budgets,self._entry_admission = budgets,admission
        for code in budgets:
            self._funnel_first_seen.setdefault(code,minute)
        rotation=sorted(budgets,key=lambda code:(code in self._funnel_last_attempt,
            self._funnel_last_attempt.get(code,self._funnel_first_seen[code]), list(budgets).index(code)))
        active={code:started for code,started in self._active_entries.items()
            if code in budgets and timedelta(0)<=minute-started<timedelta(minutes=3)}
        for code in rotation:
            if len(active)>=8:break
            active.setdefault(code,minute)
        self._active_entries=active
        self._scan=[code for code in rotation if code in active and code not in self._seen_this_minute]
        for code in fixed:
            if code in budgets or code in protected_symbols:
                if self._dormant_symbols.get(code,{}).get('code')=='funnel_not_hot':
                    self._dormant_symbols.pop(code,None)
                    self._symbol_input_diagnostics.pop(code,None)
            elif code not in self._symbol_errors:
                diagnostic={'stage':'dormant_waiting_volume','code':'funnel_not_hot',
                    'observed_at':received.isoformat(),'classification':'ranking_gate_not_met_or_capacity_deferred',
                    'low_volume_cause_confirmed':False,'minute_chart_requested':False,
                    'synthetic_bars_created':False}
                self._dormant_symbols[code]=diagnostic
                self._symbol_input_diagnostics[code]=deepcopy(diagnostic)
        publish_gate(session,received,list(active),available=True,
            watch_candidate_validator=getattr(self._provider,'watch_candidate_is_current',None),
            candidates={row['symbol']:row for row in hot if row['symbol'] in active},
            provider_binding=result['session_binding'])
        self._scan_diagnostics.update(status='ready',candidate_count=len(budgets),
            broad_market_candidate_count=len(rows),portfolio_eligible_count=len(eligible),
            hot_symbols=list(budgets),active_entry_symbols=list(active),remaining=len(self._scan),
            observed_symbols=sorted(self._seen_this_minute),portfolio_exclusions=exclusions,
            queue_capacity_deferred=[row['symbol'] for row in eligible[funnel['hot_limit']:]],
            dormant_without_entry_chart=[code for code in fixed if code not in budgets and code not in protected_symbols],
            owned_exit_symbols_outside_entry_cap=[code for code in protected_symbols if code not in budgets],
            portfolio_snapshot_observed_at=observed.isoformat(),
            queued_not_observed=[code for code in budgets if code not in active],
            fixed_watchlist_preserved=True,
            queue_delay_seconds={code:max(0,(received-self._funnel_last_attempt.get(code,
                self._funnel_first_seen[code])).total_seconds()) for code in budgets},
            queue_delay_basis='last_entry_attempt_or_first_hot_selection_not_exchange_latency')

    def _signal_event(self, tracker, *, symbol, strategy, side, reason, observed, bars=(),
                      market=None, budget=None, kind='eligible_signal', outcome=None):
        writer = getattr(tracker, 'record_strategy_signal', None)
        if not callable(writer):
            return  # Legacy injected gateways have no persistent research sink.
        from .multi_strategy_evidence import MARKET_FIELDS
        self._research_log_status = record_signal_safely(tracker, {'mode':'paper', 'strategy':strategy, 'symbol':symbol, 'side':side,
            'event_kind':kind, 'reason':reason, 'outcome':outcome, 'observed_at':observed.isoformat(),
            'run_id':self._run_id, 'strategy_source_sha256':self._source_hash, 'budget_krw':budget,
            'watch_candidate_admission':deepcopy(self._entry_admission.get(symbol)) if side == 'buy' else None,
            'market':{key:market.get(key) for key in MARKET_FIELDS} if market else None,
            'bars':[{'start':b.start.isoformat(), 'open':b.open_price, 'high':b.high_price,
                     'low':b.low_price, 'close':b.close, 'volume':b.volume,
                     'source_query_started_at':b.source_query_started_at.isoformat(),
                     'source_response_sha256':b.source_response_sha256,
                     'source_batch':b.source_batch, 'source_page':b.source_page,
                     'source_row':b.source_row} for b in list(bars)[-21:]],
            'entry_discovery_available_at':self._discovery.get('available_at') if self._discovery else None})

    def _record_observed_bars(self, tracker, symbol, bars, observed):
        # A missing neighbour never manufactures a candle. Genuine completed
        # rows can still settle later research horizons even with an SMA gap.
        actual = []
        for bar in bars:
            start, query = _moment(getattr(bar, 'start', None)), _moment(getattr(bar, 'source_query_started_at', None))
            digest = getattr(bar, 'source_response_sha256', None)
            if (start is not None and query is not None and start.date() == observed.date()
                    and not start.second and not start.microsecond and start+timedelta(minutes=1) <= query <= observed
                    and getattr(bar, 'source_symbol', None) == symbol and getattr(bar, 'source_mode', None) == 'paper'
                    and getattr(bar, 'gap_proof', None) is None and getattr(bar, 'adjustment', None) in (None, '')
                    and getattr(bar, 'adjustment_rate', None) in (None, '')
                    and isinstance(digest, str) and re.fullmatch(r'[0-9a-f]{64}', digest)
                    and type(getattr(bar, 'close', None)) is int and bar.close > 0
                    and type(getattr(bar, 'volume', None)) is int and bar.volume >= 0):
                actual.append(bar)
        if actual:
            self._signal_event(tracker, symbol=symbol, strategy='market_observation', side='observe',
                reason='observed_completed_bars', observed=observed, bars=actual, kind='market_observation')
        expected = [observed.replace(second=0,microsecond=0)-timedelta(minutes=i) for i in range(21,0,-1)]
        seen = {bar.start for bar in actual}
        missing = [at.isoformat() for at in expected if at not in seen]
        self._bar_window_diagnostics[symbol] = {'observed_at':observed.isoformat(),
            'actual_completed_rows':len(actual), 'missing_minutes':missing,
            'missing_count':len(missing), 'actual_zero_volume_rows':sum(b.volume == 0 for b in actual),
            'classification':'unobserved_not_proven_no_trade' if missing else 'actual_window_complete',
            'baseline_preserved':any(key[0] == symbol for key in self._states),
            'synthetic_bars_created':False, 'historical_cross_replay_allowed':False,
            'indicator_basis':'actual_observed_bars', 'indicator_calendar_minutes_equivalent':False,
            'bounded_actual_21_bar_window_valid':valid_completed_bars(bars,symbol,observed),
            'bounded_actual_20_close_window_valid':valid_sma_bars(bars,symbol,observed)}

    def _record_input_failure(self, symbol, code, stage='unknown'):
        self._dormant_symbols.pop(symbol, None)
        safe = code if code in _DIAGNOSTIC_CODES else 'other'
        stage = stage if stage in _DIAGNOSTIC_STAGES else 'unknown'
        try:
            stamp = self._now()
            stamp = stamp.astimezone(KST).isoformat() if _moment(stamp) is not None else None
        except Exception:
            stamp = None
        self._symbol_input_diagnostics[symbol] = {'code':safe, 'observed_at':stamp, 'stage':stage}

    def _record_s5_input(self, symbol, evaluation, observed, *, stage='entry'):
        """Strategy-specific, observation-only metadata without clearing alarms."""
        diagnostic = {
            'stage':stage, 'status':evaluation.get('status', 'PASS' if evaluation.get('eligible') else 'BLOCK'),
            'reason':evaluation['reason'], 'observed_at':observed.isoformat(),
            'signal_at':evaluation.get('signal_at'),
            'session_minute_count':evaluation.get('session_minute_count'),
            'order_permission':False}
        raw_checks = evaluation.get('checks')
        raw_checks = raw_checks if isinstance(raw_checks, dict) else {}
        checks, groups, failures = {}, {}, {}
        supplied_details = evaluation.get('failure_details')
        supplied_details = supplied_details if isinstance(supplied_details, dict) else {}
        for name, allowed in _S5_DIAGNOSTIC_GROUPS.items():
            values = {key:raw_checks[key] for key in allowed if type(raw_checks.get(key)) is bool}
            failed = [key for key, value in values.items() if not value]
            missing = [key for key in allowed if key not in values]
            # An early data return never implies that unvisited checks passed.
            status = 'fail' if failed else ('not_evaluated' if missing else 'pass')
            groups[name] = {'status':status, 'checks':values, 'failed_checks':failed,
                'evaluation_complete':not missing, 'not_evaluated_checks':missing}
            checks.update(values)
            detail = supplied_details.get(name)
            codes = detail.get('reason_codes', []) if isinstance(detail, dict) else []
            codes = codes if isinstance(codes, (list, tuple)) else []
            reasons = [code for code in codes[:32] if isinstance(code, str) and code in _S5_DIAGNOSTIC_REASONS]
            failures[name] = {'failed_checks':failed, 'reason_codes':list(dict.fromkeys(reasons))}
        diagnostic.update(checks=checks, check_groups=groups,
            failed_checks=[key for key, value in checks.items() if not value], failure_details=failures)

        coverage = evaluation.get('session_coverage')
        if isinstance(coverage, dict):
            clean = {'basis':'observed_minutes' if coverage.get('basis') == 'observed_minutes' else None}
            for key in ('expected_minutes', 'observed_minutes', 'missing_count',
                        'missing_before_pattern_count', 'tolerated_old_missing_count'):
                value = coverage.get(key)
                clean[key] = value if type(value) is int and 0 <= value <= 390 else None
            for key in ('session_complete', 'synthetic_bars_created', 'coverage_validated'):
                value = coverage.get(key)
                clean[key] = value if type(value) is bool else None
            boundary = self._s5_boundary(observed)
            opening = boundary.replace(hour=9, minute=0)
            for key in ('missing_minutes', 'missing_pattern_minutes'):
                values = coverage.get(key)
                values = values if isinstance(values, (list, tuple)) else ()
                minutes = []
                for raw in values[:390]:
                    at = _moment(raw) if isinstance(raw, str) and len(raw) <= 40 else None
                    if (at is not None and at.utcoffset() == timedelta(hours=9)
                            and not at.second and not at.microsecond and opening <= at < boundary):
                        minutes.append(at.isoformat())
                clean[key] = list(dict.fromkeys(minutes))
            if 'opening_anchor_valid' in coverage:
                value = coverage.get('opening_anchor_valid')
                clean['opening_anchor_valid'] = value if type(value) is bool else None
            for key in ('first_observed_minute', 'opening_anchor_latest_allowed'):
                if key in coverage:
                    raw = coverage.get(key)
                    at = _moment(raw) if isinstance(raw, str) and len(raw) <= 40 else None
                    clean[key] = (at.isoformat() if at is not None and at.utcoffset() == timedelta(hours=9)
                        and not at.second and not at.microsecond and opening <= at < boundary else None)
            if 'pattern_nonempty_positive_volume_buckets' in coverage:
                value = coverage.get('pattern_nonempty_positive_volume_buckets')
                clean['pattern_nonempty_positive_volume_buckets'] = value if type(value) is int and 0 <= value <= 7 else None
            if 'empty_pattern_buckets' in coverage:
                raw = coverage.get('empty_pattern_buckets')
                raw = raw if isinstance(raw, (list, tuple)) else ()
                clean['empty_pattern_buckets'] = []
                for value in raw[:7]:
                    at = _moment(value) if isinstance(value, str) and len(value) <= 40 else None
                    if (at is not None and at.utcoffset() == timedelta(hours=9) and not at.second
                            and not at.microsecond and at.minute % 5 == 0
                            and boundary-timedelta(minutes=35) <= at < boundary):
                        clean['empty_pattern_buckets'].append(at.isoformat())
            if 'pattern_bucket_coverage' in coverage:
                raw = coverage.get('pattern_bucket_coverage')
                raw = raw if isinstance(raw, (list, tuple)) else ()
                clean['pattern_bucket_coverage'] = []
                for index, bucket in enumerate(raw[:7]):
                    if not isinstance(bucket, dict):
                        continue
                    raw_start, raw_end = bucket.get('start'), bucket.get('end')
                    start = _moment(raw_start) if isinstance(raw_start, str) and len(raw_start) <= 40 else None
                    end = _moment(raw_end) if isinstance(raw_end, str) and len(raw_end) <= 40 else None
                    expected = boundary-timedelta(minutes=35-5*index)
                    if (start != expected or end != expected+timedelta(minutes=5)
                            or start.utcoffset() != timedelta(hours=9) or end.utcoffset() != timedelta(hours=9)):
                        continue
                    item = {'start':expected.isoformat(), 'end':end.isoformat()}
                    for key in ('observed_minutes', 'positive_volume_minutes'):
                        value = bucket.get(key)
                        item[key] = value if type(value) is int and 0 <= value <= 5 else None
                    value = bucket.get('complete')
                    item['complete'] = value if type(value) is bool else None
                    missing = bucket.get('missing_minutes')
                    missing = missing if isinstance(missing, (list, tuple)) else ()
                    item['missing_minutes'] = []
                    for value in missing[:5]:
                        at = _moment(value) if isinstance(value, str) and len(value) <= 40 else None
                        if (at is not None and at.utcoffset() == timedelta(hours=9)
                                and not at.second and not at.microsecond and start <= at < end):
                            item['missing_minutes'].append(at.isoformat())
                    clean['pattern_bucket_coverage'].append(item)
            diagnostic['session_coverage'] = clean

        vwap = evaluation.get('vwap')
        if isinstance(vwap, dict):
            clean = {}
            for key in ('hlc3_volume_numerator', 'volume', 'prior_5m_numerator', 'prior_5m_volume'):
                value = vwap.get(key)
                if type(value) is int and 0 <= value < 10**40:
                    clean[key] = value
            if vwap.get('basis') == 'observed_minutes':
                clean['basis'] = 'observed_minutes'
            for key in ('session_complete', 'exact_full_session_vwap'):
                if type(vwap.get(key)) is bool:
                    clean[key] = vwap[key]
            diagnostic['vwap'] = clean
        self._s5_input_diagnostics[symbol] = diagnostic

    def _mark_dormant(self, symbol, reason, observed):
        for strategy in STRATEGIES:
            self._states.pop((symbol, strategy), None)
        # A shortage cannot clear an unresolved price/session/provenance error.
        if self._symbol_errors.get(symbol) not in (None, 'closed_bars_unavailable'):
            self._bar_window_diagnostics.setdefault(symbol, {})['waiting_on_data_with_unresolved_error'] = True
            return
        item = {'stage':'dormant_waiting_volume', 'code':'dormant_low_volume',
                'data_reason':reason, 'observed_at':observed.isoformat(),
                'classification':'unobserved_not_proven_no_trade', 'low_volume_cause_confirmed':False,
                'baseline_reset':True, 'recovery_requires_new_observed_baseline':True,
                'synthetic_bars_created':False, 'historical_cross_replay_allowed':False}
        self._dormant_symbols[symbol] = item
        self._symbol_input_diagnostics[symbol] = deepcopy(item)
        self._symbol_errors.pop(symbol, None)
        self._bar_window_diagnostics.setdefault(symbol, {})['baseline_preserved'] = False
        if not self._symbol_errors and self._error == 'strategy_input_or_gate_unavailable':
            self._error = None

    def _entry_admitted(self, symbol, observed):
        proof = self._entry_admission.get(symbol)
        if not isinstance(proof, dict):
            return False
        if proof.get('kind') == 'explicit_legacy_fixed':
            return True
        if proof.get('kind') == 'configured_base':
            settings = self._settings()
            pct, _ = effective_b3_guards(settings, 'paper')
            configured = {}
            for row in settings.get('gdcrs_stocks', []):
                if isinstance(row, dict):
                    budget = row.get('paper_budget', row.get('budget'))
                    if type(budget) is int:
                        configured[row.get('code')] = min(1_000_000,budget)*pct//100
            for code,budget in effective_paper_scan(settings,'paper')['stocks']:
                configured.setdefault(code,min(1_000_000,budget)*pct//100)
            return (symbol in configured and configured[symbol] == proof.get('initial_budget_krw')
                    and configured[symbol] == self._entry_budgets.get(symbol))
        available = _moment(proof.get('available_at'))
        fresh = (proof.get('kind') in ('verified_discovery','verified_toss_watch') and available is not None
                and timedelta(0) <= observed-available <= timedelta(seconds=60)
                and available.replace(second=0,microsecond=0) == observed.replace(second=0,microsecond=0)
                and proof.get('session_binding') == self._discovery_binding)
        if fresh and proof.get('track') in ('T1_PRECURSOR','T2_MOMENTUM'):
            validate_live=getattr(self._provider,'watch_candidate_is_current',None)
            if not callable(validate_live) or validate_live(symbol,proof,observed) is not True:return False
        if not fresh or proof.get('kind') != 'verified_toss_watch':
            return fresh
        # A promotion is a watch nomination, never a substituted Kiwoom signal.
        # Recheck its independent screening evidence immediately before send.
        from .toss_candidate_feed import validate_candidate
        try:
            row = validate_candidate(proof.get('toss_evidence'), now=observed,
                session_day=proof.get('toss_session_day'),
                previous_session_day=proof.get('toss_previous_session_day'))
            validate_live=getattr(self._provider,'toss_candidate_is_current',None)
            return (row['symbol'] == symbol and (proof.get('track') in ('T1_PRECURSOR','T2_MOMENTUM')
                    or callable(validate_live) and validate_live(row,observed) is True))
        except (ValueError, TypeError, KeyError, AttributeError, InvalidOperation, OverflowError):
            return False

    def _mark_input_validated(self, symbol, observed, stage):
        # Only local completion time, not an exchange quote timestamp or trade.
        self._symbol_last_validated_at[symbol] = {'observed_at':observed.isoformat(), 'stage':stage}
        prior = self._symbol_errors.get(symbol)
        # A valid price does not prove the SMA or the 15:19 cutoff bar.
        # Likewise a valid SMA alone does not prove the cutoff bar.
        preserved = ((prior == 'sma20_unavailable' and stage == 'exit_quote') or
                     (prior == 'cutoff_bar_unavailable' and stage in ('exit_quote', 'exit_quote_and_sma')))
        if not preserved:
            self._symbol_errors.pop(symbol, None)
            self._symbol_input_diagnostics.pop(symbol, None)
            self._dormant_symbols.pop(symbol, None)
        if not self._symbol_errors and self._error == 'strategy_input_or_gate_unavailable':
            self._error = None

    def request_resume(self):
        """Explicit request only; this never grants permission or resends orders."""
        self._resume_requested = self._halted
        return self._resume_requested

    def resume_after_reconciliation(self, session, tracker, *, proof):
        if not self._halted or not self._resume_requested:
            return False
        try:
            now = self._now().astimezone(KST)
            if session_identity(session)[0] != 'paper':
                return False
            recorder = session.execution_recorder.status()
            if (recorder.get('ready') is not True or recorder.get('error') is not None
                    or tracker.validate_paper_monitor_resume(proof, now) is not True):
                return False
        except Exception:
            return False
        # Preserve attempted signal keys, order markers and audit history. A
        # configuration reset is not an uncertain-order recovery mechanism.
        self._halted = False
        self._resume_requested = False
        self._error = None
        return True

    def _halt_uncertain(self, symbol, notify):
        first = not self._halted
        self._halted = True
        self._resume_requested = False
        self._error = 'order_outcome_unconfirmed'
        if first:
            try:
                notify(f'모의 다중 전략 · {symbol}: 주문 결과가 미확정이라 정지했습니다. 재전송하지 않습니다. 대사 후 multi resume으로 재개하세요.')
            except Exception:
                pass

    def _warning(self, symbol, reason, notify):
        previous = self._symbol_errors.get(symbol)
        if previous == 'cutoff_bar_unavailable' and reason == 'sma20_unavailable':
            return  # Do not hide an unverified cutoff until its bar is observed.
        self._symbol_errors[symbol] = reason
        self._record_input_failure(symbol, reason)
        if previous != reason:
            if reason == "ownership_unavailable":
                notify(f"모의 다중 전략 · {symbol}: 소유 또는 미결 상태를 확인하지 못해 자동 청산을 보류합니다. 원장을 확인하세요.")
            elif reason == "sma20_unavailable":
                notify(f"모의 다중 전략 · {symbol}: 완료봉이 부족해 SMA20 청산 기준을 확인하지 못했습니다.")
            elif reason == "cutoff_bar_unavailable":
                notify(f"모의 다중 전략 · {symbol}: 15:19 완료봉을 확인하지 못해 장 종료 청산을 확인할 수 없습니다.")

    def tick(self, session, notify):
        symbol = None
        input_stage = 'unknown'
        try:
            if self._halted or self._stop():
                return
            now = self._now()
            if _moment(now) is None:
                raise ValueError("clock_unavailable")
            now = now.astimezone(KST)
            settings = self._settings()
            if not isinstance(settings, dict) or settings.get("paper_multi_strategy_enabled") is not True:
                self.reset()
                self._error = None
                return
            identity = session_identity(session)
            if identity[0] != "paper":
                self.reset()
                self._error = "paper_only"
                return
            ids = effective_multi_strategies(settings, identity[0])
            scan_options = effective_paper_scan(settings, identity[0])
            stocks = settings.get("gdcrs_stocks")
            if (not ids or any(x not in STRATEGIES for x in ids)
                    or not isinstance(stocks, list) or len(stocks) > 100):
                raise ValueError("invalid_strategy_settings")
            normalized = []
            initial_pct, _ = effective_b3_guards(settings, 'paper')
            for row in stocks:
                if not isinstance(row, dict):
                    raise ValueError("invalid_symbol_settings")
                code, budget = row.get("code"), row.get("paper_budget", row.get("budget"))
                if (not isinstance(code, str) or re.fullmatch(r"[0-9]{6}", code) is None
                        or type(budget) is not int or not 1 <= budget <= 3000000
                        or any(existing[0] == code for existing in normalized)):
                    raise ValueError("invalid_symbol_settings")
                normalized.append((code, min(1000000, budget) * initial_pct // 100))
            # A changing candidate universe must not reset already observed
            # strategy baselines or owned-position supervision.
            key = (identity, tuple(ids), now.date(), effective_toss_hybrid(settings, identity[0]),
                   settings.get('paper_fast_decision_enabled') is True)
            if key != self._identity:
                self.reset()
                self._identity = key
                self._provider = self._provider_factory(session)
                self._exit_provider = self._exit_provider_factory(session)
            self._enabled = True
            if self._executor is None:
                # Do not even perform broker queries without an ownership and
                # atomic-dispatch gateway. Never silently degrade to direct buys.
                self._error = "strategy_ownership_gateway_unavailable"
                self._next_at = now + timedelta(seconds=25)
                return
            if settings.get('paper_fast_decision_enabled') is True:
                try:
                    if self._fast_pipeline is None:
                        from .fast_decision_pipeline import FastDecisionCoordinator
                        self._fast_pipeline=FastDecisionCoordinator()
                    starter=getattr(self._fast_pipeline,'start',None)
                    session_key=hashlib.sha256((self._run_id+':'+str(id(session))+':'+now.date().isoformat()).encode()).hexdigest()
                    if callable(starter):starter(session_key)
                    source=getattr(self._fast_pipeline,'set_account_source',None)
                    if callable(source):
                        active_tracker=getattr(getattr(session,'paper_automation_reconcile',None),'_tracker',None)
                        watch=list(dict.fromkeys([c for c,_ in normalized]+[c for c,_ in scan_options['stocks']]+list(self._universe)))[:40]
                        if active_tracker is not None:source(active_tracker,watch)
                except Exception:
                    pass  # Optional preparation cannot suppress protective exits.
            if now.weekday() >= 5 or not "09:00" <= now.strftime("%H:%M") < "15:30":
                self._states.clear()
                self._next_at = now + timedelta(seconds=25)
                return
            if self._next_at and now < self._next_at:
                return
            minute = now.replace(second=0, microsecond=0)
            tracker = getattr(getattr(session, "paper_automation_reconcile", None), "_tracker", None)
            get_position = getattr(tracker, "multi_strategy_position", None)
            get_open_symbols = getattr(tracker, "multi_strategy_open_symbols", None)
            if not callable(get_position) or not callable(get_open_symbols):
                raise ValueError("strategy_ownership_unavailable")
            open_result = get_open_symbols("paper", now)
            if (not isinstance(open_result, dict) or open_result.get("status") != "available"
                    or not isinstance(open_result.get("symbols"), list)
                    or any(not isinstance(code, str) or re.fullmatch(r"[0-9]{6}", code) is None
                           for code in open_result["symbols"])):
                raise ValueError("open_positions_unavailable")
            if self._scan_at != minute:
                # Open/pending symbols remain under observation even if removed
                # from the configured entry universe. Prioritize their exits.
                open_symbols = list(dict.fromkeys(open_result["symbols"]))
                if self._last_symbol in open_symbols:
                    offset = open_symbols.index(self._last_symbol) + 1
                    open_symbols = open_symbols[offset:] + open_symbols[:offset]
                self._scan, self._scan_at = open_symbols, minute
                self._entry_budgets = dict(normalized)
                self._entry_admission = {code:{'kind':'explicit_legacy_fixed'} for code,_ in normalized}
                self._seen_this_minute.clear()
            else:
                urgent = [code for code in open_result['symbols'] if code not in self._seen_this_minute]
                self._scan = list(dict.fromkeys(urgent + self._scan))
            if not self._scan and self._discovery_at != minute:
                self._refresh_entries(normalized, scan_options, now, minute, settings, open_result['symbols'], tracker=tracker, session=session)
                if session_identity(session) != identity:
                    raise _InputDiagnostic('session_changed')
            if not self._scan:
                self._next_at = minute + timedelta(minutes=1)
                return
            symbol = self._scan.pop(0)
            self._seen_this_minute.add(symbol)
            self._scan_diagnostics.update(remaining=len(self._scan), observed_symbols=sorted(self._seen_this_minute))
            self._last_symbol = symbol
            self._next_at = now + timedelta(seconds=1)
            input_stage = 'ownership'
            positions = [(strategy, get_position("paper", symbol, strategy, now)) for strategy in STRATEGIES]
            owned = [(strategy, position) for strategy, position in positions
                     if isinstance(position, dict) and position.get("status") == "owned"]
            if len(owned) > 1:
                raise ValueError("multiple_strategy_owners")
            if any(not isinstance(position, dict) or position.get("status") not in ("owned", "flat", "flat_candidate", "pending", "unsupported")
                   for _, position in positions):
                raise ValueError("strategy_ownership_unavailable")
            candidates = [p for _, p in positions if p.get("status") == "flat_candidate"]
            if any(p.get("candidate_only") is not True or
                   p.get("requires_fresh_broker_reconciliation") is not True for p in candidates):
                raise ValueError("flat_candidate_contract_invalid")
            if candidates:
                self._flat_candidates[symbol] = {"candidate_only": True,
                    "requires_fresh_broker_reconciliation": True,
                    "observed_at": now.isoformat()}
            else:
                self._flat_candidates.pop(symbol, None)
            budget = self._entry_budgets.get(symbol)
            completed = ()
            state = EntryState()
            quantity = None
            action = None
            b3_config = b3_add_on = None
            add_candidate = None
            precision_book = None
            prearmed_version = None
            if owned:
                strategy, position = owned[0]
                if position.get("overnight_unobserved_interval") is True:
                    self._overnight_positions[symbol] = {"strategy": strategy,
                                                        "observed_at": now.isoformat()}
                else:
                    self._overnight_positions.pop(symbol, None)
                input_stage = 'broker_holdings'
                try:
                    basis, sellable = fresh_owned_basis(session, symbol, position, self._account)
                except Exception:
                    self._warning(symbol, 'ownership_unavailable', notify)
                    return
                position = dict(position, basis_krw=basis, broker_sellable_qty=sellable)
                if strategy in ids and now.strftime('%H:%M') < '15:19':
                    candidate_reader = getattr(tracker,'multi_b3_add_candidate',None)
                    if callable(candidate_reader):
                        # Read-only eligibility. Failure cannot delay or block
                        # the already prioritized protective exit path.
                        try:
                            add_candidate = candidate_reader('paper',symbol,strategy,now)
                        except Exception:
                            add_candidate = None
                        self._b3_add_on_diagnostics[symbol] = {'strategy':strategy,'observed_at':now.isoformat(),
                            'stage':'initial_lot_verified' if add_candidate else 'initial_lot_unavailable_or_already_claimed',
                            'eligible_signal':False}
                requested = self._now().astimezone(KST)
                input_stage = 'exit_quote'
                market = self._exit_provider.fetch(symbol, as_of=requested)
                observed = self._now().astimezone(KST)
                if observed.replace(second=0, microsecond=0) != minute:
                    raise _InputDiagnostic("minute_rollover")
                if not self._exit_provider.is_fresh(market, observed):
                    raise _InputDiagnostic("exit_quote_stale")
                self._mark_input_validated(symbol, observed, 'exit_quote')
                action = exit_signal(strategy, qty=position.get("owned_qty"), basis_krw=basis,
                                     price=market.get("current_price"), sma20=None,
                                     half_taken=position.get("half_take_done"))
                if action is None and strategy == S5:
                    action = timed_exit(position,observed)
                if action is None:
                    # SMA is optional for price-based risk exits. Never substitute
                    # a stale or incomplete history for a known moving average.
                    try:
                        input_stage = 'exit_sma'
                        rows = self._chart(session, symbol, count=62 if add_candidate else 21)
                        after_chart = self._now().astimezone(KST)
                        completed = tuple(bar for bar in rows if bar.start + timedelta(minutes=1) <= after_chart)
                        self._record_observed_bars(tracker, symbol, completed, after_chart)
                        cutoff = cutoff_bar(completed, symbol, after_chart)
                        sma_valid = valid_sma_bars(completed, symbol, after_chart)
                        if sma_valid:
                            sma20 = Decimal(sum(bar.close for bar in completed[-20:])) / 20
                            input_stage = 'exit_quote'
                            market = self._exit_provider.fetch(symbol, as_of=after_chart)
                            observed = self._now().astimezone(KST)
                            if observed.replace(second=0, microsecond=0) != minute:
                                raise _InputDiagnostic("minute_rollover")
                            if not self._exit_provider.is_fresh(market, observed):
                                raise _InputDiagnostic("exit_quote_stale")
                            self._mark_input_validated(symbol, observed, 'exit_quote_and_sma')
                            action = exit_signal(strategy, qty=position.get("owned_qty"), basis_krw=basis,
                                                 price=market.get("current_price"), sma20=sma20,
                                                 half_taken=position.get("half_take_done"))
                        else:
                            self._warning(symbol, "sma20_unavailable", notify)
                        if cutoff is not None and self._symbol_errors.get(symbol) == 'cutoff_bar_unavailable':
                            self._mark_input_validated(symbol, after_chart, 'exit_cutoff_bar')
                            if not sma_valid:
                                self._warning(symbol, 'sma20_unavailable', notify)
                        if action is None and cutoff is not None:
                            # It is an observed bar, not a confirmed closing
                            # auction price; resulting order can remain unfilled.
                            completed = (cutoff,)
                            action = ("regular_cutoff_exit", position["owned_qty"])
                        elif action is None and after_chart.strftime("%H:%M") >= "15:20":
                            self._warning(symbol, "cutoff_bar_unavailable", notify)
                    except Exception as error:
                        self._warning(symbol, "cutoff_bar_unavailable" if
                                      self._now().astimezone(KST).strftime("%H:%M") >= "15:20"
                                      else "sma20_unavailable", notify)
                        if isinstance(error, (ContextUnavailable, _InputDiagnostic)):
                            self._record_input_failure(symbol, error.code, input_stage)
                if action is None:
                    if not add_candidate or observed.strftime('%H:%M') >= '15:19':
                        return
                    if strategy == S5:
                        reader = getattr(self._provider,'fetch_s5_inputs',None)
                        inputs = reader(symbol,as_of=self._now()) if callable(reader) else {}
                        market = self._exit_provider.fetch(symbol,as_of=self._now())
                        market.update(inputs)
                        observed = self._now().astimezone(KST)
                        if (observed.replace(second=0,microsecond=0)!=minute
                                or not self._exit_provider.is_fresh(market,observed)):
                            raise _InputDiagnostic('exit_quote_stale')
                        risk = validate_s5_risk(current_price=market.get('current_price'),
                            daily_context=market.get('s5_daily_context'),risk_context=market.get('s5_risk_context'),
                            symbol=symbol,now=observed,require_short=True)
                        self._record_s5_input(symbol,risk,observed,stage='add_on_risk')
                        if not risk['eligible']:
                            self._b3_add_on_diagnostics[symbol]={'strategy':strategy,'observed_at':observed.isoformat(),
                                'stage':risk['reason'],'eligible_signal':False}
                            return
                    from .multi_b3 import evaluate_add_on
                    add = evaluate_add_on(add_candidate,bars=completed,market=market,position=position,
                                          now=observed,symbol=symbol,settings=settings)
                    self._b3_add_on_diagnostics[symbol] = {'strategy':strategy,'observed_at':observed.isoformat(),
                        'stage':add['reason'],'eligible_signal':add['eligible'],
                        'initial_intent_id':add_candidate['initial_intent_id']}
                    if not add['eligible']:
                        self._last_decision = {'symbol':symbol,'strategy':strategy,'side':'buy',
                            'reason':add['reason'],'observed_at':observed.isoformat()}
                        self._signal_event(tracker,symbol=symbol,strategy=strategy,side='buy',reason=add['reason'],
                            observed=observed,kind='decision_outcome',outcome='not_submitted')
                        if add['reason']=='add_on_negative_sma_observed':
                            invalidator = getattr(tracker,'multi_b3_invalidate_candidate',None)
                            if callable(invalidator):
                                invalidator('paper',symbol,strategy,add_candidate['initial_intent_id'],observed,
                                            reason='negative_sma_observed')
                        return
                    side, reason, budget = 'buy','add_on_strength',add['budget']
                    b3_config,b3_add_on,state = add['config'],add['addition'],None
                    self._signal_event(tracker,symbol=symbol,strategy=strategy,side=side,reason=reason,
                        observed=observed,bars=completed,market=market,budget=budget)
                else:
                    reason, quantity = action
                    if quantity > sellable:
                        raise ValueError("insufficient_verified_sellable")
                    side = "sell"
                provider = self._exit_provider
            else:
                if symbol not in open_result['symbols']:
                    self._last_entry_symbol = symbol
                self._overnight_positions.pop(symbol, None)
                # No inferred ownership and no candidate when the symbol was
                # removed from the entry universe or any order is unresolved.
                if (symbol in open_result["symbols"] and any(p["status"] in ("pending", "unsupported")
                                                               for _, p in positions)):
                    self._warning(symbol, "ownership_unavailable", notify)
                    return
                if (budget is None or any(p["status"] == "pending" for _, p in positions)
                        or candidates and any(p["status"] not in ("flat", "flat_candidate") for _, p in positions)):
                    return
                if now.strftime("%H:%M") >= "15:19":
                    self._last_decision = {"symbol": symbol, "side": "buy",
                                           "reason": "entry_cutoff_reached", "observed_at": now.isoformat()}
                    return
                input_stage = 'entry_bars'
                s5_boundary = self._s5_due(symbol, now, ids)
                s5_attempt_boundary = self._s5_boundary(now) if s5_boundary else None
                s5_rollover_allowed = (s5_boundary and self._entry_admission.get(symbol,{}).get('kind')=='configured_base'
                    and not effective_paper_funnel(settings,'paper')['enabled'])
                s5_minute_advanced = False
                if s5_boundary:
                    self._s5_mark_attempt(symbol, now)
                # One extra raw row covers a small in-flight minute rollover;
                # it is never used to fabricate a missing opening prefix.
                session_count = min(390,max(2,int((minute-minute.replace(hour=9,minute=0)).total_seconds()//60)+2))
                if effective_paper_funnel(settings, 'paper')['enabled']:
                    if self._now().astimezone(KST).second>=45:
                        self._scan_diagnostics.setdefault('deadline_deferred',[]).append(symbol)
                        return
                    self._funnel_last_attempt[symbol]=self._now().astimezone(KST)
                    from .paper_funnel_state import shared_chart
                    # Align fixed and dynamic GDCRS/B3 readers; both reuse
                    # these rows without another entry-chart request.
                    count = session_count if s5_boundary else (62 if effective_b3_guards(settings,'paper')[1] else 22)
                    fetcher = self._session_chart if s5_boundary else self._chart
                    rows = shared_chart(session,symbol,count=count,now=self._now(),fetcher=fetcher)
                else:
                    rows = self._session_chart(session,symbol,count=session_count) if s5_boundary else self._chart(session,symbol,count=22)
                observed = self._now().astimezone(KST)
                if self._stop() or session_identity(session) != identity:
                    raise _InputDiagnostic("session_changed")
                if observed.replace(second=0, microsecond=0) != minute:
                    if s5_rollover_allowed and self._s5_boundary(observed)==s5_attempt_boundary:
                        minute=observed.replace(second=0,microsecond=0)
                        s5_minute_advanced=True
                    else:
                        raise _InputDiagnostic("minute_rollover")
                completed = tuple(bar for bar in rows if bar.start + timedelta(minutes=1) <= observed)
                self._record_observed_bars(tracker, symbol, completed, observed)
                ordinary_bars_valid = valid_completed_bars(completed, symbol, observed)
                # S5 validates the full actual session prefix through floor5m
                # itself. Missing rows AFTER that completed boundary cannot
                # veto it as if they belonged to the pattern. S2~S4 retain the
                # strict latest-minute/21-row requirement below.
                if not ordinary_bars_valid and not s5_boundary:
                    reason = dormant_bar_reason(rows, symbol, observed)
                    if S5 in ids:
                        self._record_s5_input(symbol,{'status':'DORMANT' if reason is not None else 'BLOCK',
                            'reason':reason or 'bar_data_or_provenance_unverified'},observed)
                    # Ownership-unknown/unsupported positions never become a
                    # harmless data-wait state, even when not in open_symbols.
                    flat_verified = all(p['status'] in ('flat','flat_candidate') for _,p in positions)
                    if reason is not None and flat_verified:
                        self._mark_dormant(symbol, reason, observed)
                        return
                    code = 'bar_data_or_provenance_unverified' if reason is None else 'closed_bars_unavailable'
                    self._symbol_errors[symbol] = code
                    self._record_input_failure(symbol, code, input_stage)
                    for strategy in STRATEGIES:
                        self._states.pop((symbol, strategy), None)
                    return
                s5_inputs = {}
                if s5_boundary:
                    reader = getattr(self._provider,'fetch_s5_inputs',None)
                    if callable(reader):
                        s5_inputs = reader(symbol,as_of=observed)
                input_stage = 'entry_quote'
                market = self._provider.fetch(symbol, as_of=self._now())
                market.update(s5_inputs)
                observed = self._now().astimezone(KST)
                if observed.replace(second=0, microsecond=0) != minute:
                    if s5_rollover_allowed and self._s5_boundary(observed)==s5_attempt_boundary:
                        minute=observed.replace(second=0,microsecond=0)
                        s5_minute_advanced=True
                    else:
                        raise _InputDiagnostic("minute_rollover")
                ordinary_bars_valid = valid_completed_bars(completed, symbol, observed)
                if not ordinary_bars_valid and not s5_boundary:
                    raise _InputDiagnostic("closed_bars_unavailable")
                if not self._provider.is_fresh(market, observed):
                    raise _InputDiagnostic("market_context_stale")
                if effective_paper_funnel(settings,'paper')['enabled']:
                    input_stage='entry_orderbook'
                    book_reader=getattr(self._provider,'fetch_orderbook',None)
                    book_fresh=getattr(self._provider,'is_orderbook_fresh',None)
                    def before_book():
                        current=self._now().astimezone(KST)
                        if (self._stop() or session_identity(session)!=identity or self._settings()!=settings
                                or current.replace(second=0,microsecond=0)!=minute or current.second>=45):
                            raise ValueError('funnel_orderbook_request_expired')
                    try:
                        if not callable(book_reader) or not callable(book_fresh):raise ValueError('book_provider_missing')
                        before_book()
                        precision_book=book_reader(symbol,as_of=observed,before_send=before_book)
                        observed=self._now().astimezone(KST)
                        if (not book_fresh(precision_book,observed) or not self._provider.is_fresh(market,observed)
                                or observed.replace(second=0,microsecond=0)!=minute):
                            raise ValueError('book_or_price_stale')
                        from .paper_funnel_state import mark_precision_ready
                        mark_precision_ready(session,observed,symbol,precision_book)
                    except Exception:
                        self._orderbook_diagnostics[symbol]={'status':'unavailable','observed_at':self._now().isoformat(),
                            'reason':'stale_sparse_missing_or_expired_orderbook','same_slot_retry':False}
                        self._research_log_status=record_signal_safely(tracker,{'mode':'paper','strategy':'market_observation',
                            'symbol':symbol,'side':'observe','event_kind':'market_observation',
                            'observed_at':self._now().isoformat(),'origin':'paper_funnel',
                            'reason':'orderbook_unavailable','broker_fill':False})
                        raise _InputDiagnostic('orderbook_unavailable') from None
                    self._orderbook_diagnostics[symbol]={'status':'observed','observed_at':observed.isoformat(),
                        'orderbook':deepcopy(precision_book),'order_permission':False}
                    self._research_log_status=record_signal_safely(tracker,{'mode':'paper','strategy':'market_observation',
                        'symbol':symbol,'side':'observe','event_kind':'market_observation','observed_at':observed.isoformat(),
                        'origin':'paper_funnel','reason':'strict_top1_orderbook_observed',
                        'orderbook':deepcopy(precision_book),'broker_fill':False})
                if ordinary_bars_valid:
                    self._mark_input_validated(symbol, observed, 'entry_bars_and_quote')
                if s5_boundary:
                    s5_evaluation = evaluate_steady_uptrend_vwap(completed,symbol=symbol,now=observed,
                        current_price=market.get('current_price'),daily_context=market.get('s5_daily_context'),
                        risk_context=market.get('s5_risk_context'),require_short=True)
                    market['steady_uptrend_vwap_evidence'] = s5_evaluation
                    self._record_s5_input(symbol,s5_evaluation,observed)
                    if s5_evaluation['eligible'] and not ordinary_bars_valid:
                        self._mark_input_validated(symbol, observed, 'entry_s5_session_and_quote')
                    evaluated_boundary = self._s5_boundary(observed)
                    self._s5_evaluated[symbol] = evaluated_boundary
                    if self._s5_schedule.get('boundary') == evaluated_boundary.isoformat():
                        self._s5_schedule['evaluated_symbols'] = [code for code in self._s5_schedule.get('eligible_symbols', [])
                            if self._s5_evaluated.get(code) == evaluated_boundary]
                    if settings.get('paper_fast_decision_enabled') is True:
                        prearmed_version = self._prepare_s5(session=session,symbol=symbol,bars=completed,
                            market=market,evaluation=s5_evaluation,observed=observed,settings=settings,
                            positions=positions,budget=budget)
                    if s5_evaluation['status']=='DORMANT' and ids==(S5,):
                        self._mark_dormant(symbol,s5_evaluation['reason'],observed)
                provider = self._provider
                strategy = None
                reason = None
                position = None
                input_stage = 'strategy_signal'
                for candidate, candidate_position in positions:
                    if (candidate not in ids or candidate == S5 and not s5_boundary
                            or candidate != S5 and (not ordinary_bars_valid or s5_minute_advanced)):
                        continue
                    previous = self._states.get((symbol, candidate), EntryState())
                    cached, candidate_reason = (False,None)
                    if candidate == S5 and prearmed_version is not None:
                        cached,candidate_reason = self._s5_prearmed_signal(previous,completed,market,
                            symbol=symbol,now=observed,version=prearmed_version)
                    if not cached:
                        # The local S5 result above is bound to this exact
                        # observation. Reuse it once even when advisory caching
                        # is disabled/unavailable; do not re-evaluate the same
                        # complete-session pattern at the signal stage.
                        candidate_reason = ('steady_uptrend_vwap' if s5_evaluation['eligible'] else None) if candidate == S5 else entry_signal(candidate, previous, completed, market, symbol=symbol, now=observed)
                    if candidate == S5:
                        if s5_evaluation.get('signal_at') == self._s5_boundary(observed).isoformat():
                            self._states[(symbol,candidate)] = EntryState(self._s5_boundary(observed)-timedelta(minutes=1))
                    else:
                        self._states[(symbol, candidate)] = next_entry_state(previous, completed[-1], market)
                    if candidate_reason:
                        self._signal_event(tracker, symbol=symbol, strategy=candidate, side='buy',
                            reason=candidate_reason, observed=observed, bars=completed, market=market, budget=budget)
                    if strategy is None and candidate_position.get("status") in ("flat", "flat_candidate") and candidate_reason:
                        strategy, position, state, reason = candidate, candidate_position, previous, candidate_reason
                    elif candidate_reason:
                        self._signal_event(tracker, symbol=symbol, strategy=candidate, side='buy',
                            reason='strategy_arbitration_or_ownership', observed=observed,
                            kind='decision_outcome', outcome='not_submitted')
                if strategy is None:
                    return
                side = "buy"
                if not self._entry_admitted(symbol, observed):
                    self._last_decision = {'symbol':symbol,'strategy':strategy,'side':side,
                        'reason':'candidate_membership_unverified','observed_at':observed.isoformat()}
                    self._signal_event(tracker,symbol=symbol,strategy=strategy,side=side,
                        reason='candidate_membership_unverified',observed=observed,
                        kind='decision_outcome',outcome='not_submitted')
                    return
                from .multi_b3 import initial_config
                b3_config = initial_config(settings,symbol,budget)
                if not self._cooldown(session, symbol, observed, 20):
                    self._last_decision = {"symbol": symbol, "strategy": strategy, "side": side,
                                           "reason": "reentry_cooldown", "observed_at": observed.isoformat()}
                    self._signal_event(tracker, symbol=symbol, strategy=strategy, side=side,
                        reason='reentry_cooldown', observed=observed, kind='decision_outcome', outcome='not_submitted')
                    return
                if scan_options['max_positions'] or effective_theme_cap(settings, 'paper'):
                    capacity = getattr(tracker, 'paper_position_capacity', None)
                    try:
                        if not callable(capacity):
                            raise ValueError('capacity_unavailable')
                        # Quote time remains immutable evidence; an account
                        # poll completed since then is not a future balance.
                        capacity(symbol, self._now().astimezone(KST))
                    except Exception as error:
                        code = getattr(error, 'skipped_reason', 'concurrent_position_limit_unavailable')
                        if code not in ('concurrent_position_limit_reached', 'concurrent_position_limit_unavailable',
                                        'theme_concentration_limit_reached','theme_concentration_unavailable'):
                            code = 'concurrent_position_limit_unavailable'
                        self._last_decision = {'symbol':symbol, 'strategy':strategy, 'side':side,
                                               'reason':code, 'observed_at':observed.isoformat()}
                        diagnostics=getattr(error,'diagnostics',None)
                        if isinstance(diagnostics,dict):
                            self._last_decision['capacity_diagnostics']={key:diagnostics[key] for key in
                                ('reason','validation_at','balance_observed_at','balance_age_seconds',
                                 'blocking_observation_id','blocking_kind') if key in diagnostics}
                        self._signal_event(tracker, symbol=symbol, strategy=strategy, side=side,
                            reason=code, observed=observed, kind='decision_outcome', outcome='not_submitted')
                        return
            if side == 'sell':
                self._signal_event(tracker, symbol=symbol, strategy=strategy, side=side, reason=reason,
                    observed=observed, bars=completed, market=market)
            def before_send():
                current = self._now().astimezone(KST)
                if (self._stop() or session_identity(session) != identity or self._settings() != settings
                        or current.replace(second=0, microsecond=0) != minute
                        or not provider.is_fresh(market, current)):
                    raise ValueError("strategy_context_changed")
                if side == "buy" and not self._cooldown(session, symbol, current, 20):
                    raise ValueError("reentry_cooldown")
                if side == 'buy' and current.strftime('%H:%M') >= '15:19':
                    raise ValueError('entry_cutoff_reached')
                if side == 'buy' and b3_add_on is None and effective_paper_funnel(settings,'paper')['enabled']:
                    if not provider.is_orderbook_fresh(precision_book,current):
                        raise ValueError('funnel_orderbook_expired_before_dispatch')
                if side == 'buy' and strategy == S5:
                    if (b3_add_on is None and market.get('steady_uptrend_vwap_evidence',{}).get('signal_at')
                            != self._s5_boundary(current).isoformat()):
                        raise ValueError('s5_boundary_changed')
                    advisory=getattr(self._fast_pipeline,'decision_for',None)
                    if callable(advisory):
                        try:
                            self._prearmed_diagnostics.setdefault(symbol,{})['pre_send_advisory']=advisory(symbol,current)
                        except Exception:
                            pass  # No network wait or new authorization at send.
                    if settings.get('paper_fast_decision_enabled') is True:
                        generation = getattr(self._provider,'s5_risk_generation',None)
                        expected = market.get('s5_risk_context',{}).get('file_sha256')
                        if not callable(generation) or not expected or generation() != expected:
                            self._prearmed.invalidate(symbol,S5)
                            raise ValueError('s5_risk_evidence_changed')
                    risk = validate_s5_risk(current_price=market.get('current_price'),
                        daily_context=market.get('s5_daily_context'),risk_context=market.get('s5_risk_context'),
                        symbol=symbol,now=current,require_short=True)
                    if not risk['eligible']:
                        raise ValueError(risk['reason'])
                if side == 'buy' and b3_add_on is None and not self._entry_admitted(symbol,current):
                    raise ValueError('candidate_membership_unverified')
            self._last_decision = {"symbol": symbol, "strategy": strategy, "side": side,
                                   "reason": reason, "observed_at": observed.isoformat()}
            try:
                result = self._executor(session=session, tracker=tracker, strategy=strategy, symbol=symbol,
                                        side=side, reason=reason, quantity=quantity, budget=budget,
                                        position=position, bars=(completed if strategy == S5 and side == "buy" and b3_add_on is None else completed[-21:]), market=market,
                                        baseline=state, observed_at=observed, before_send=before_send,
                                        run_id=self._run_id, source_hash=self._source_hash,
                                        b3_config=b3_config,b3_add_on=b3_add_on)
            except Exception:
                self._halt_uncertain(symbol, notify)
                return
            if getattr(result, "uncertain", False):
                self._halt_uncertain(symbol, notify)
            self._signal_event(tracker, symbol=symbol, strategy=strategy, side=side,
                reason=(getattr(result, 'decision_reason', None) or getattr(result, 'skipped_reason', None)
                        or ('order_outcome_unconfirmed' if getattr(result, 'uncertain', False) else
                            'order_submitted' if getattr(result, 'submitted', None) is True else 'order_not_submitted')),
                observed=self._now().astimezone(KST), kind='decision_outcome',
                outcome='unconfirmed' if getattr(result, 'uncertain', False) or getattr(result, 'submitted', None) is None else
                    'receipt_observed' if getattr(result, 'submitted', None) is True else 'not_submitted')
            if isinstance(result, (list, tuple)):
                for message in result:
                    if isinstance(message, str) and message:
                        notify(f"{strategy} · {symbol}\n{message}")
            if self._symbol_errors.get(symbol) not in ("sma20_unavailable", "cutoff_bar_unavailable"):
                self._symbol_errors.pop(symbol, None)
        except Exception as error:
            # No retries or blind assumptions on malformed broker data.
            self._error = "strategy_input_or_gate_unavailable"
            if symbol:
                name = type(error).__name__
                self._symbol_errors[symbol] = name if name in _SAFE_ERROR_NAMES else 'Error'
                code = error.code if isinstance(error, (ContextUnavailable, _InputDiagnostic)) else 'other'
                self._record_input_failure(symbol, code, input_stage)
