"""Opt-in, serial golden-cross buying from newly closed KRX minute candles.

The command loop calls one tick at a time inside automated_orders(..., "gdcrs").
Each tick reads at most one registered symbol and attempts at most one budget
buy. Settings/session/day changes, unverified gaps and revised bars only seed a
baseline. A bounded paper-only source gap may be reconciled after a completed
right anchor without replaying an older crossing. There is no holiday calendar;
fresh same-day bars are required.
Large watchlists rotate fairly, but cannot trade a symbol without consecutive
minute observations. No background orders, automatic login or catch-up occurs.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
import re
import sqlite3
import uuid

from kiwoom_autotrader.api.errors import BrokerAPIError
from kiwoom_autotrader.api.minute_chart import MinuteChartError, fetch_minute_chart, session_identity
from kiwoom_autotrader.api.session import KST
from kiwoom_autotrader.config.settings import (load_settings, save_settings, effective_cross_guards,
    effective_b3_guards, effective_unfilled_buy_ttl, effective_entry_time_window, effective_paper_funnel)
from kiwoom_autotrader.telegram.commands.orders import OrderReplies, handle_buy
from kiwoom_autotrader.trading.decision_evidence import cross_evidence, b3_addition_evidence, strategy_source_hash, SOURCE_KINDS
from kiwoom_autotrader.trading.b3_entry import (volume_window, price_above_basis,
    post_fill_trend_is_nonnegative, B3EvidenceUnavailable)
from kiwoom_autotrader.trading.order_context import order_decision
from kiwoom_autotrader.trading.reentry_cooldown import reentry_allowed, CooldownUnavailable
from kiwoom_autotrader.trading.cross_monitor import (Baseline, CrossMonitorError, closed_window,
    continuous_cross_previous, keep_recent_missing_baseline, sma_difference)
from kiwoom_autotrader.utils.paths import app_data_root
from kiwoom_autotrader.utils.platform_io import os
from kiwoom_autotrader.trading.strategy_signal_log import record_signal_safely
from kiwoom_autotrader.trading.unfilled_buy_ttl import UnfilledBuyTTLMonitor


class GoldenCrossError(RuntimeError):
    """Locally composed failure; no provider descriptions or credentials."""


ENTRY_SKIP_REASONS = frozenset({"cap_exhausted", "insufficient_cash", "reentry_cooldown",
    "below_min_cross_bps", "budget_below_one_share", "reconciliation_unavailable",
    "buy_guard_changed", "quote_unavailable", "order_book_unavailable", "order_not_submitted",
    "order_outcome_unconfirmed", "attempt_already_claimed", "attempt_marker_unavailable",
    "context_changed", "monitor_error", "daily_entry_limit_reached", "daily_entry_limit_unavailable",
    "outside_entry_time_window",
    "concurrent_position_limit_reached", "concurrent_position_limit_unavailable",
    "theme_concentration_limit_reached", "theme_concentration_unavailable",
    "add_on_below_profit_threshold", "add_on_volume_insufficient", "add_on_basis_unverified",
    "add_on_pending_order", "add_on_trend_unverified", "add_on_dead_cross_invalidated"})


def marker_path() -> Path:
    return app_data_root() / "data/gdcrs-order-markers.sqlite3"


class GoldenOrderMarkers:
    """Commit before the quote/order handler and retain every attempted claim."""

    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path is not None else marker_path()

    def claim(self, mode, symbol, candle):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.path.is_symlink() or (self.path.exists() and not self.path.is_file()):
            raise GoldenCrossError("gdcrs: 주문 시도 기록 파일을 확인하지 못했습니다.")
        database = sqlite3.connect(self.path, timeout=2)
        try:
            os.chmod(self.path, 0o600)
            database.execute("PRAGMA synchronous=FULL")
            database.execute("CREATE TABLE IF NOT EXISTS attempts (mode TEXT NOT NULL, symbol TEXT NOT NULL, candle TEXT NOT NULL, PRIMARY KEY(mode,symbol,candle))")
            with database:
                inserted = database.execute("INSERT OR IGNORE INTO attempts VALUES (?,?,?)", (mode, symbol, candle.isoformat())).rowcount == 1
            return inserted
        finally:
            database.close()

    def record_decision(self, mode, symbol, candle, observed_at, outcome, skip_reason=None):
        """Append a no-send reason without turning a filtered signal into a claim."""
        if (mode not in ("paper", "real") or re.fullmatch(r"[0-9]{6}", symbol) is None
                or outcome not in ("skipped", "receipt_observed", "outcome_unconfirmed")
                or skip_reason is not None and skip_reason not in ENTRY_SKIP_REASONS
                or outcome == "skipped" and skip_reason is None):
            raise GoldenCrossError("gdcrs: 진입 판단 기록의 형식을 확인하지 못했습니다.")
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.path.is_symlink() or self.path.exists() and not self.path.is_file():
            raise GoldenCrossError("gdcrs: 진입 판단 기록 파일을 확인하지 못했습니다.")
        with sqlite3.connect(self.path, timeout=2) as database:
            os.chmod(self.path, 0o600)
            database.execute("PRAGMA synchronous=FULL")
            database.execute("CREATE TABLE IF NOT EXISTS entry_decisions (id INTEGER PRIMARY KEY,mode TEXT NOT NULL,symbol TEXT NOT NULL,candle TEXT NOT NULL,observed_at TEXT NOT NULL,outcome TEXT NOT NULL,skip_reason TEXT)")
            database.execute("INSERT INTO entry_decisions(mode,symbol,candle,observed_at,outcome,skip_reason) VALUES (?,?,?,?,?,?)",
                             (mode, symbol, candle.isoformat(), observed_at.isoformat(), outcome, skip_reason))


@dataclass(frozen=True)
class _Context:
    session: tuple[str, str] = field(repr=False)
    short: int
    long: int
    start: str
    end: str
    min_cross_bps: int
    reentry_cooldown_minutes: int
    max_daily_entries_per_symbol: int
    stocks: tuple[tuple[str, int], ...]
    day: str
    initial_entry_pct: int
    add_on_profit_bps: int
    unfilled_buy_ttl_seconds: int
    entry_start: str
    entry_end: str
    paper_funnel_enabled: bool = False
    paper_dynamic_s1_enabled: bool = False


class GoldenCrossMonitor:
    def __init__(self, *, settings_loader=None, settings_saver=None, chart_fetcher=None,
                 buy_handler=None, markers=None, now=None, state_path: Path | None = None,
                 stop_requested=None, chart_source_kind=None, cooldown_check=None, ttl_monitor=None):
        self.settings_loader = settings_loader or load_settings
        self.settings_saver = settings_saver or save_settings
        self.chart_fetcher = chart_fetcher or fetch_minute_chart
        self._evidence_run_id = uuid.uuid4().hex
        self._strategy_source_hash = strategy_source_hash(__file__)
        self._chart_source_kind = chart_source_kind or ("broker_observation" if chart_fetcher is None else "unknown")
        if self._chart_source_kind not in SOURCE_KINDS:
            raise ValueError("분봉 근거의 출처 구분이 올바르지 않습니다.")
        self.buy_handler = buy_handler or handle_buy
        self.cooldown_check = cooldown_check or reentry_allowed
        self.markers = markers if markers is not None else GoldenOrderMarkers(state_path)
        self.now = now or (lambda: datetime.now(KST))
        self._ttl_monitor = ttl_monitor if ttl_monitor is not None else UnfilledBuyTTLMonitor(now=self.now)
        self.stop_requested = stop_requested or (lambda: False)
        self._identity = None
        self._baseline: dict[str, Baseline] = {}
        self._scan = []
        self._scan_at = None
        self._next_at = None
        self._last_symbol = None
        self._enabled = False
        self._halted = False
        self._error = None
        self._symbol_errors = {}
        self._generation = 0
        self._last_entry_decision = None
        self._research_log_status = None
        self._funnel_diagnostics = {}
        self._funnel_active = set()
        self._funnel_seen = set()
        self._funnel_owned = {}
        self._funnel_owned_at = None

    def reset(self, *, clear_halt=False):
        """Clear observations; only an explicit successful start clears a halt."""
        self._generation += 1
        self._identity = None
        self._baseline.clear()
        self._scan.clear()
        self._scan_at = None
        self._next_at = None
        self._last_symbol = None
        self._symbol_errors.clear()
        self._funnel_diagnostics.clear()
        self._funnel_active.clear()
        self._funnel_seen.clear()
        self._funnel_owned.clear()
        self._funnel_owned_at = None
        self._enabled = False
        if clear_halt:
            self._halted = False

    def _moment(self):
        value = self.now()
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise GoldenCrossError("gdcrs: 한국 시간 시계를 확인하지 못했습니다.")
        return value.astimezone(KST)

    def seconds_until_next(self):
        if not self._enabled:
            return None
        delay = max(0.0, (self._next_at - self._moment()).total_seconds()) if self._next_at else 1.0
        if (self._identity and self._identity.unfilled_buy_ttl_seconds and self._ttl_monitor.next_at
                and self._market_open(self._moment(), self._identity)):
            delay = min(delay, max(0.0, (self._ttl_monitor.next_at - self._moment()).total_seconds()))
        return delay

    def status(self):
        """Credential-free observations for the running listener's status file."""
        result = {"enabled": self._enabled, "halted": self._halted, "error": self._error,
                "last_entry_decision": deepcopy(self._last_entry_decision),
                "research_signal_log": deepcopy(self._research_log_status),
                "symbol_errors": deepcopy(self._symbol_errors),
                "funnel": deepcopy(self._funnel_diagnostics),
                "observed_closed_bars": {code: baseline.bars[-1].start.isoformat()
                    for code, baseline in self._baseline.items() if baseline.bars},
                "next_check_at": self._next_at.isoformat() if self._next_at else None}
        if self._identity and self._identity.unfilled_buy_ttl_seconds:
            result["unfilled_buy_ttl"] = deepcopy(self._ttl_monitor.last_status)
        return result

    def _context(self, session, now):
        settings = self.settings_loader()
        if settings.get("gdcrs_enabled") is not True:
            return None
        short, long = settings.get("gdcrs_short_period"), settings.get("gdcrs_long_period")
        if type(short) is not int or type(long) is not int or not 1 <= short < long <= 60:
            raise GoldenCrossError("gdcrs: 1 ≤ 단기 < 장기 ≤ 60으로 gdcrs intv를 설정하세요. 주문하지 않습니다.")
        min_bps = settings.get("gdcrs_min_cross_bps", 0)
        cooldown = settings.get("gdcrs_reentry_cooldown_minutes", 0)
        daily_limit = settings.get("gdcrs_max_daily_entries_per_symbol", 0)
        if type(daily_limit) is not int or not 0 <= daily_limit <= 1000:
            raise GoldenCrossError("gdcrs: 종목별 일일 진입 한도 설정을 확인하세요.")
        if (type(min_bps) is not int or not 0 <= min_bps <= 10000
                or type(cooldown) is not int or not 0 <= cooldown <= 1440):
            raise GoldenCrossError("gdcrs: 교차 이격도 또는 재진입 대기 설정을 확인하세요.")
        start, end = settings.get("market_start_time", "09:00"), settings.get("market_end_time", "15:30")
        if (any(not isinstance(value, str) or re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", value) is None for value in (start, end))
                or start >= end or max("09:00", start) >= min("15:30", end)):
            raise GoldenCrossError("gdcrs: KRX 정규장과 겹치는 유효한 장 시간을 설정하세요. 주문하지 않습니다.")
        stocks = settings.get("gdcrs_stocks")
        if not isinstance(stocks, list) or len(stocks) > 100:
            raise GoldenCrossError("gdcrs: 등록 종목 설정을 확인하지 못했습니다.")
        seen = set()
        result = []
        identity = session_identity(session)
        mode = identity[0]
        min_bps, cooldown = effective_cross_guards(settings, mode)
        initial_pct, addon_bps = effective_b3_guards(settings, mode)
        for row in stocks:
            if (not isinstance(row, dict) or set(row) not in ({"code", "budget"}, {"code", "budget", "paper_budget"})
                    or not isinstance(row["code"], str) or re.fullmatch(r"[0-9]{6}", row["code"]) is None
                    or row["code"] in seen or type(row["budget"]) is not int or not 1 <= row["budget"] <= 999999999999
                    or ("paper_budget" in row and (type(row["paper_budget"]) is not int or not 1 <= row["paper_budget"] <= 3000000))):
                raise GoldenCrossError("gdcrs: 등록 종목코드·예산을 확인하지 못했습니다.")
            seen.add(row["code"])
            result.append((row["code"], row.get("paper_budget", row["budget"]) if mode == "paper" else row["budget"]))
        ttl_seconds = effective_unfilled_buy_ttl(settings, mode)
        entry_start,entry_end = effective_entry_time_window(settings, mode)
        funnel=effective_paper_funnel(settings,mode)['enabled']
        dynamic=(funnel and settings.get('paper_multi_strategy_enabled') is True
            and initial_pct==50 and addon_bps>=50 and min_bps>=10 and cooldown>=20 and daily_limit==2)
        return _Context(identity, short, long, start, end, min_bps, cooldown, daily_limit, tuple(result), now.strftime("%Y%m%d"), initial_pct, addon_bps, ttl_seconds, entry_start, entry_end, funnel, dynamic)

    @staticmethod
    def _market_open(now, context):
        return now.weekday() < 5 and max("09:00", context.start) <= now.strftime("%H:%M") < min("15:30", context.end)

    @staticmethod
    def _entry_window_open(now, context):
        if not isinstance(now,datetime) or now.tzinfo is None or now.utcoffset() is None:
            return False
        now=now.astimezone(KST)
        return (now.strftime("%Y%m%d")==context.day and GoldenCrossMonitor._market_open(now,context)
                and context.entry_start<=now.strftime("%H:%M")<context.entry_end)

    def _message(self, notify, message):
        if message == self._error:
            return
        self._error = message
        try:
            notify(message)
        except Exception:
            pass

    def _pause_uncertain(self, notify):
        self._halted = True
        self.reset()
        message = "gdcrs: 주문 접수 여부가 불명확하여 자동매수를 중지했습니다. 증권사 주문내역을 확인한 뒤 start gdcrs로 다시 시작하세요."
        try:
            self.settings_saver({"gdcrs_enabled": False})
        except Exception:
            message += " 중지 상태 저장에 실패해 현재 프로세스에서만 중지했습니다. 재시작 전에 설정과 주문내역을 확인하세요."
        self._message(notify, message)

    def _record_entry_decision(self, signal, outcome, skip_reason=None):
        mode, symbol, candle, observed_at = signal
        self._last_entry_decision = {"mode": mode, "symbol": symbol, "candle": candle.isoformat(),
            "observed_at": observed_at.isoformat(), "outcome": outcome, "skip_reason": skip_reason,
            "persisted": False}
        writer = getattr(self.markers, "record_decision", None)
        if callable(writer):
            writer(mode, symbol, candle, observed_at, outcome, skip_reason)
            self._last_entry_decision["persisted"] = True

    def tick(self, session, notify):
        """Call serially with command dispatch, inside the gdcrs order context."""
        symbol = None
        signal = None
        decision_attempted = False
        def note(outcome, reason=None):
            nonlocal decision_attempted
            decision_attempted = True
            try:
                self._record_entry_decision(signal, outcome, reason)
                tracker = getattr(getattr(session, 'paper_automation_reconcile', None), '_tracker', None)
                writer = getattr(tracker, 'record_strategy_signal', None)
                if signal[0] == 'paper' and callable(writer):
                    self._research_log_status = record_signal_safely(tracker, {'mode':'paper', 'strategy':'gdcrs', 'symbol':signal[1], 'side':'buy',
                        'event_kind':'decision_outcome', 'observed_at':signal[3].isoformat(),
                        'candle_start':signal[2].isoformat(), 'outcome':outcome, 'reason':reason,
                        'raw_cross_signal':cross_previous is not None, 'b3_stage':b3_stage, 'research_only':True,
                        'bars':[{'start':b.start.isoformat(), 'close':b.close, 'volume':b.volume,
                            'source_response_sha256':b.source_response_sha256,
                            'source_query_started_at':b.source_query_started_at.isoformat()
                                if b.source_query_started_at else None} for b in window],
                        'cross_difference_integer':difference,
                        'entry_time_window':{'start':context.entry_start,'end':context.entry_end,
                            'timezone':'Asia/Seoul','applies_to':'initial_golden_cross_only'},
                        'short_period':context.short, 'long_period':context.long})
            except Exception:
                # Audit storage must never bypass the existing uncertain-order
                # halt when a handler result or its receipt is unknown.
                if outcome == "outcome_unconfirmed":
                    self._pause_uncertain(notify)
                raise
        try:
            if self._halted:
                return
            if self.stop_requested():
                self.reset()
                return
            now = self._moment()
            context = self._context(session, now)
            if context is None:
                self.reset()
                self._error = None
                return
            if context != self._identity:
                self.reset()
                self._identity = context
            self._enabled = True
            if not self._market_open(now, context):
                self._baseline.clear()
                self._scan.clear()
                self._scan_at = None
                self._next_at = now + timedelta(seconds=5)
                return
            if context.unfilled_buy_ttl_seconds:
                tracker = getattr(getattr(session, 'paper_automation_reconcile', None), '_tracker', None)
                self._ttl_monitor.tick(session, tracker, context.unfilled_buy_ttl_seconds,
                    enabled_check=lambda: not self.stop_requested()
                    and self._market_open(self._moment(), context)
                    and self._context(session, self._moment()) == context)
                # Cancellation and chart/order work stay serial. Revalidate
                # after the bounded query/dispatch, including a session change.
                if (self.stop_requested() or self._context(session, self._moment()) != context
                        or not self._market_open(self._moment(), context)):
                    self.reset()
                    return
            if self._next_at and now < self._next_at:
                return
            minute = now.replace(second=0, microsecond=0)
            self._next_at = now + timedelta(seconds=1)
            if self._scan_at != minute:
                # Immutable configured stocks never absorb changing hot rows.
                symbols = [code for code, _ in context.stocks]
                if self._last_symbol in symbols:
                    after = symbols.index(self._last_symbol) + 1
                    symbols = symbols[after:] + symbols[:after]
                self._scan = symbols
                self._scan_at = minute
                self._funnel_seen.clear()
            if context.paper_funnel_enabled:
                from .paper_funnel_state import read_gate, precision_ready_symbols
                gate = read_gate(session, now)
                tracker = getattr(getattr(session,'paper_automation_reconcile',None),'_tracker',None)
                fixed = dict(context.stocks)
                # Restore proven GDCRS first lots after restart/hot removal.
                # This read-only denominator does not authorize an add: the
                # existing per-symbol and atomic B3 checks run again below.
                if self._funnel_owned_at != minute:
                    self._funnel_owned_at=minute
                    self._funnel_owned={}
                    reader=getattr(tracker,'b3_monitor_candidates',None)
                    if context.add_on_profit_bps and callable(reader):
                        try:
                            restored=reader('paper',now)
                            if (not isinstance(restored,dict) or restored.get('status')!='available'
                                    or not isinstance(restored.get('candidates'),dict)
                                    or len(restored['candidates'])>100):
                                raise ValueError('dynamic_b3_ownership_unavailable')
                            for code,row in restored['candidates'].items():
                                cfg=row.get('config') if isinstance(row,dict) else None
                                if (not isinstance(code,str) or re.fullmatch(r'[0-9]{6}',code) is None
                                        or not isinstance(cfg,dict) or not isinstance(row.get('candidate'),dict)
                                        or cfg.get('b3_total_budget_krw')!=1_000_000
                                        or cfg.get('b3_initial_entry_pct')!=context.initial_entry_pct
                                        or cfg.get('b3_add_on_profit_bps')!=context.add_on_profit_bps):
                                    continue
                                self._funnel_owned[code]=deepcopy(row)
                        except Exception:
                            self._funnel_owned={}
                lookup = getattr(tracker,'b3_add_candidate_status',None)
                owned_add = set(self._funnel_owned)
                if context.add_on_profit_bps and callable(lookup):
                    for code in fixed:
                        try:
                            status=lookup('paper',code,now)
                            if (isinstance(status,dict) and status.get('status')=='candidate'
                                    and isinstance(status.get('candidate'),dict)):
                                owned_add.add(code)
                        except Exception:
                            pass
                active=set(gate['symbols']) if gate and gate.get('available') else set()
                dynamic_active=active-set(fixed) if context.paper_dynamic_s1_enabled else set()
                # A normal-equity proof is mandatory for dynamic admission.
                dynamic_active.intersection_update((gate or {}).get('candidates',{}))
                eligible_active=(active & set(fixed)) | dynamic_active
                if gate and gate.get('available'):
                    for code in (self._funnel_active-eligible_active) | (eligible_active-self._funnel_active):
                        if code not in owned_add:
                            self._baseline.pop(code,None)
                    self._funnel_active=eligible_active
                allowed=eligible_active | owned_add
                self._scan=[code for code in self._scan if code in allowed and code not in self._funnel_seen]
                for code in list(sorted(owned_add))+list((gate or {}).get('symbols',())):
                    if code in allowed and code not in self._funnel_seen and code not in self._scan:
                        self._scan.append(code)
                ready=set(precision_ready_symbols(gate,now)) | owned_add
                self._scan.sort(key=lambda code:(code not in owned_add,code not in ready))
                self._funnel_diagnostics={'minute':minute.isoformat(),
                    'new_entries':'shared_active_hot_with_versioned_dynamic_admission',
                    'dynamic_s1_adapter':True, 'dynamic_s1_policy_ready':context.paper_dynamic_s1_enabled,
                    'dynamic_entry_symbols':sorted(dynamic_active),
                    'owned_b3_candidates':sorted(owned_add), 'ddcrs_exit_monitor_unchanged':True,
                    'gate_available':bool(gate and gate.get('available')),
                    'precision_ready_symbols':sorted(ready-owned_add),
                    'dormant_without_entry_chart':sorted(set(fixed)-allowed),
                    'status':'ready'}
                if not self._scan:
                    self._next_at=now+timedelta(seconds=1)
                    self._funnel_diagnostics['status']='awaiting_shared_hot_gate' if not gate or not gate.get('available') else 'minute_active_work_complete'
                    return
                if now.second>=45 and self._scan[0] not in owned_add:
                    self._next_at=minute+timedelta(minutes=1)
                    self._funnel_diagnostics['status']='entry_request_deadline_reached'
                    return
                if self._scan[0] not in ready:
                    self._next_at=now+timedelta(seconds=1)
                    self._funnel_diagnostics['status']='awaiting_hot_orderbook_precision'
                    return
            elif not self._scan:
                self._next_at=minute+timedelta(minutes=1)
                return
            symbol = self._scan.pop(0)
            self._last_symbol = symbol
            if context.paper_funnel_enabled:
                self._funnel_seen.add(symbol)
            generation = self._generation
            chart_count=62 if context.add_on_profit_bps else context.long+2
            if context.paper_funnel_enabled:
                from .paper_funnel_state import shared_chart
                bars=shared_chart(session,symbol,count=chart_count,now=now,fetcher=self.chart_fetcher)
            else:
                bars = self.chart_fetcher(session, symbol, count=chart_count)
            observed_at = self._moment()
            if (self.stop_requested() or self._generation != generation or self._context(session, observed_at) != context
                    or not self._market_open(observed_at, context)):
                self.reset()
                return
            window = closed_window(bars, observed_at, context.long)
            tracker = getattr(getattr(session, 'paper_automation_reconcile', None), '_tracker', None)
            writer = getattr(tracker, 'record_strategy_signal', None)
            if context.session[0] == 'paper' and callable(writer):
                self._research_log_status = record_signal_safely(tracker, {'mode':'paper', 'strategy':'market_observation', 'symbol':symbol, 'side':'observe',
                    'event_kind':'market_observation', 'observed_at':observed_at.isoformat(),
                    'origin':'gdcrs', 'reason':'observed_completed_bars',
                    'bars':[{'start':b.start.isoformat(), 'close':b.close, 'volume':b.volume,
                        'source_query_started_at':b.source_query_started_at.isoformat() if b.source_query_started_at else None,
                        'source_response_sha256':b.source_response_sha256} for b in window if b.gap_proof is None]})
            self._symbol_errors.pop(symbol, None)
            difference = sma_difference(window, context.short, context.long)
            previous = self._baseline.get(symbol)
            self._baseline[symbol] = Baseline(window[-context.long:], difference)
            cross_previous = continuous_cross_previous(previous, window, context.short, context.long, side="buy")
            b3_initial = None
            b3_volume_window = None
            b3_trend_window = None
            b3_stage = "initial" if context.session[0] == "paper" and context.initial_entry_pct < 100 else None
            if b3_stage and context.add_on_profit_bps:
                tracker = getattr(getattr(session, "paper_automation_reconcile", None), "_tracker", None)
                lookup = getattr(tracker, "b3_add_candidate_status", None)
                if not callable(lookup):
                    signal = (context.session[0], symbol, window[-1].start, observed_at)
                    note("skipped", "add_on_basis_unverified")
                    raise GoldenCrossError("gdcrs: B3 최초 체결 원장을 확인하지 못해 주문하지 않았습니다.")
                try:
                    status = lookup("paper", symbol, observed_at)
                    if (not isinstance(status, dict) or set(status) != {"status", "candidate"}
                            or status["status"] not in ("candidate", "no_initial", "lot_closed", "pending", "unavailable", "claimed", "invalidated")
                            or (status["status"] == "candidate") != isinstance(status["candidate"], dict)):
                        raise ValueError
                    b3_initial = status["candidate"]
                except Exception:
                    signal = (context.session[0], symbol, window[-1].start, observed_at)
                    note("skipped", "add_on_basis_unverified")
                    raise GoldenCrossError("gdcrs: B3 최초 체결 원장을 확인하지 못해 주문하지 않았습니다.") from None
                if status["status"] in ("pending", "unavailable"):
                    signal = (context.session[0], symbol, window[-1].start, observed_at)
                    note("skipped", "add_on_pending_order" if status["status"] == "pending" else "add_on_basis_unverified")
                    return
                if b3_initial is not None:
                    signal = (context.session[0], symbol, window[-1].start, observed_at)
                    try:
                        if (set(b3_initial) != {"initial_intent_id", "initial_fill_last_observed_at", "initial_filled_qty", "initial_filled_principal_krw"}
                                or not isinstance(b3_initial["initial_intent_id"], str)
                                or re.fullmatch(r"[0-9a-f]{32}", b3_initial["initial_intent_id"]) is None
                                or type(b3_initial["initial_filled_qty"]) is not int
                                or not 1 <= b3_initial["initial_filled_qty"] <= 10**12
                                or type(b3_initial["initial_filled_principal_krw"]) is not int
                                or not 1 <= b3_initial["initial_filled_principal_krw"] <= 10**15):
                            raise ValueError
                        last_fill = datetime.fromisoformat(b3_initial["initial_fill_last_observed_at"])
                    except (KeyError, TypeError, ValueError, B3EvidenceUnavailable):
                        note("skipped", "add_on_basis_unverified")
                        raise GoldenCrossError("gdcrs: B3 최초 체결 근거를 확인하지 못해 주문하지 않았습니다.") from None
                    try:
                        safe_trend, b3_trend_window = post_fill_trend_is_nonnegative(bars, now=observed_at, symbol=symbol,
                            initial_fill_observed_at=last_fill, short=context.short, long=context.long)
                    except (KeyError, TypeError, ValueError, B3EvidenceUnavailable):
                        note("skipped", "add_on_trend_unverified")
                        raise GoldenCrossError("gdcrs: B3 체결·거래량·이동평균 경로를 확인하지 못해 주문하지 않았습니다.") from None
                    if not safe_trend:
                        invalidate = getattr(tracker, "b3_invalidate_candidate", None)
                        if not callable(invalidate):
                            note("skipped", "add_on_basis_unverified")
                            raise GoldenCrossError("gdcrs: B3 하락 전환 원장 확인에 실패해 주문하지 않았습니다.")
                        try:
                            invalidate("paper", symbol, b3_initial["initial_intent_id"], observed_at, "negative_sma_observed")
                        except Exception:
                            note("skipped", "add_on_basis_unverified")
                            raise GoldenCrossError("gdcrs: B3 하락 전환 원장 기록에 실패해 주문하지 않았습니다.") from None
                        note("skipped", "add_on_dead_cross_invalidated")
                        return
                    try:
                        b3_volume_window, passed = volume_window(bars, now=observed_at, symbol=symbol,
                            initial_fill_observed_at=last_fill)
                    except (ValueError, B3EvidenceUnavailable):
                        note("skipped", "add_on_basis_unverified")
                        raise GoldenCrossError("gdcrs: B3 거래량 기준 봉을 확인하지 못해 주문하지 않았습니다.") from None
                    if not passed:
                        note("skipped", "add_on_volume_insufficient")
                        return
                    if not price_above_basis(b3_volume_window[-1].close, b3_initial["initial_filled_qty"],
                            b3_initial["initial_filled_principal_krw"], context.add_on_profit_bps):
                        note("skipped", "add_on_below_profit_threshold")
                        return
                    b3_stage = "add"
            if b3_stage != "add" and cross_previous is None:
                self._error = next((item["message"] for item in self._symbol_errors.values()), None)
                return
            signal = (context.session[0], symbol, window[-1].start, observed_at)
            # Keep observing/updating the baseline outside the entry window.
            # A skipped crossing is audited once and never queued for later.
            decision_at=self._moment()
            if b3_stage != "add" and not self._entry_window_open(decision_at,context):
                signal=(context.session[0],symbol,window[-1].start,decision_at)
                note("skipped","outside_entry_time_window")
                return
            # Exact integer arithmetic: (SMA_short - SMA_long) / SMA_long
            # in basis points. A too-small crossing is skipped, not replayed.
            if b3_stage != "add" and difference * 10000 < context.min_cross_bps * context.short * sum(b.close for b in window[-context.long:]):
                note("skipped", "below_min_cross_bps")
                return
            if context.reentry_cooldown_minutes and not self.cooldown_check(session, symbol, self._moment(), context.reentry_cooldown_minutes):
                note("skipped", "reentry_cooldown")
                return
            if context.session[0] == "paper" and context.max_daily_entries_per_symbol:
                try:
                    tracker = getattr(getattr(session, "paper_automation_reconcile", None), "_tracker", None)
                    if tracker is None:
                        raise ValueError
                    state = tracker.daily_buy_entry_counts("paper", symbol, self._moment())
                    if (not isinstance(state, dict) or set(state) != {"accepted", "reserved"}
                            or any(type(state[key]) is not int or state[key] < 0 for key in ("accepted", "reserved"))):
                        raise ValueError
                except Exception:
                    note("skipped", "daily_entry_limit_unavailable")
                    raise GoldenCrossError("gdcrs: 일일 진입 원장을 확인하지 못해 주문하지 않았습니다.") from None
                if state["accepted"] + state["reserved"] >= context.max_daily_entries_per_symbol:
                    note("skipped", "daily_entry_limit_reached")
                    return
            evaluated_at = self._moment()
            fixed_budgets=dict(context.stocks)
            dynamic_admission=None
            if symbol in fixed_budgets:
                budget=fixed_budgets[symbol]
            elif b3_stage=='add' and symbol in self._funnel_owned:
                # The original reconciled lot, not today's hot list, supplies
                # this budget. Existing B3 preparation checks it atomically.
                budget=self._funnel_owned[symbol]['config']['b3_total_budget_krw']
            elif context.paper_dynamic_s1_enabled and b3_stage=='initial':
                from .paper_funnel_state import dynamic_s1_admission
                dynamic_admission=dynamic_s1_admission(session,evaluated_at,symbol)
                budget=dynamic_admission['total_budget_krw']
            else:
                raise GoldenCrossError('gdcrs: 동적 종목의 신규 진입 또는 기존 소유 예산을 확인하지 못했습니다.')
            total_budget = min(budget, 1_000_000) if b3_stage else None
            if b3_stage:
                initial_budget = total_budget * context.initial_entry_pct // 100
                budget = initial_budget if b3_stage == "initial" else total_budget - initial_budget
                if budget <= 0:
                    note("skipped", "budget_below_one_share")
                    return

            def before_send():
                nonlocal signal
                dispatch_now = self._moment()
                valid = (not self.stop_requested() and not self._halted and self._generation == generation
                         and self._context(session, dispatch_now) == context)
                if context.paper_funnel_enabled and b3_stage != 'add':
                    from .paper_funnel_state import read_gate, precision_ready_symbols
                    current_gate=read_gate(session,dispatch_now)
                    valid = valid and bool(current_gate and current_gate.get('available')
                                           and symbol in precision_ready_symbols(current_gate,dispatch_now))
                    if dynamic_admission is not None:
                        from .paper_funnel_state import assert_current_dynamic_s1_admission
                        try:
                            assert_current_dynamic_s1_admission(session,dispatch_now,dynamic_admission)
                        except Exception:
                            valid=False
                # Settings/session reads above may cross a minute boundary.
                dispatch_now = self._moment()
                if valid and b3_stage != "add" and not self._entry_window_open(dispatch_now,context):
                    signal=(context.session[0],symbol,window[-1].start,dispatch_now)
                    error=GoldenCrossError("gdcrs: 최초 진입 허용 시간 밖이므로 주문하지 않았습니다.")
                    error.skipped_reason="outside_entry_time_window"
                    raise error
                if (not valid or not self._market_open(dispatch_now, context)
                        or dispatch_now.replace(second=0, microsecond=0) - timedelta(minutes=1) != window[-1].start):
                    error = GoldenCrossError("gdcrs: 전송 대기 중 상태·예산 또는 종료봉이 바뀌어 주문하지 않았습니다.")
                    error.skipped_reason = "context_changed"
                    raise error
                if context.reentry_cooldown_minutes and not self.cooldown_check(session, symbol, dispatch_now, context.reentry_cooldown_minutes):
                    error = GoldenCrossError("gdcrs: 매도 후 재진입 대기 기간이므로 주문하지 않았습니다.")
                    error.skipped_reason = "reentry_cooldown"
                    raise error

            before_send()
            try:
                claimed = self.markers.claim(context.session[0], symbol, window[-1].start)
            except Exception:
                note("skipped", "attempt_marker_unavailable")
                raise
            if not claimed:
                note("skipped", "attempt_already_claimed")
                return
            evidence_config={"enabled": True, "short_period": context.short, "long_period": context.long,
                        "market_start_time": context.start, "market_end_time": context.end,
                        "min_cross_bps": context.min_cross_bps,
                        "reentry_cooldown_minutes": context.reentry_cooldown_minutes,
                        **({"max_daily_entries_per_symbol": context.max_daily_entries_per_symbol}
                           if context.session[0] == "paper" and context.max_daily_entries_per_symbol else {}),
                        "stocks": [{"code": code, "budget": amount} for code, amount in context.stocks]}
            if context.session[0]=="paper" and b3_stage!="add":
                evidence_config.update(entry_start_time=context.entry_start,entry_end_time=context.entry_end)
            if b3_stage:
                evidence_config.update(b3_stage=b3_stage,
                    b3_initial_intent_id=b3_initial["initial_intent_id"] if b3_stage == "add" else None,
                    b3_initial_entry_pct=context.initial_entry_pct,
                    b3_add_on_profit_bps=context.add_on_profit_bps, b3_total_budget_krw=total_budget)
            if b3_stage == "add":
                evidence = b3_addition_evidence(run_id=self._evidence_run_id, source_hash=self._strategy_source_hash,
                    source_kind=self._chart_source_kind, observed_at=observed_at, evaluated_at=evaluated_at,
                    config=evidence_config, window=b3_volume_window, trend_window=b3_trend_window, budget=budget, initial=b3_initial)
            else:
                evidence = cross_evidence(
                    strategy="gdcrs", run_id=self._evidence_run_id, source_hash=self._strategy_source_hash,
                    source_kind=self._chart_source_kind, observed_at=observed_at, evaluated_at=evaluated_at,
                    config=evidence_config, window=window, previous_difference=cross_previous,
                    difference=difference, budget=budget, dynamic_s1_admission=dynamic_admission)
            # This handler verifies a current quote and forwards the guard into
            # the shared HTTP queue. A claim remains even for known no-send.
            try:
                with order_decision(evidence):
                    replies = self.buy_handler(f"buy {symbol} max {budget}", session, before_send=before_send)
            except Exception:
                note("outcome_unconfirmed", "order_outcome_unconfirmed")
                self._pause_uncertain(notify)
                return
            if not isinstance(replies, (list, tuple)) or any(not isinstance(reply, str) for reply in replies):
                note("outcome_unconfirmed", "order_outcome_unconfirmed")
                self._pause_uncertain(notify)
                return
            raw_reason = getattr(replies, "decision_reason", None) or getattr(replies, "skipped_reason", None)
            raw_reason = raw_reason if isinstance(raw_reason, str) else None
            reason = {"symbol_buy_limit_exhausted": "cap_exhausted"}.get(raw_reason, raw_reason)
            if getattr(replies, "uncertain", False):
                note("outcome_unconfirmed", "order_outcome_unconfirmed")
            elif reason in ENTRY_SKIP_REASONS:
                note("skipped", reason)
            elif isinstance(replies, OrderReplies) and replies.submitted is True:
                note("receipt_observed")
            elif isinstance(replies, OrderReplies) and replies.submitted is False:
                note("skipped", "order_not_submitted")
            else:
                note("outcome_unconfirmed", "order_outcome_unconfirmed")
            self._error = next((item["message"] for item in self._symbol_errors.values()), None)
            for reply in replies:
                try:
                    notify(f"gdcrs · {symbol} · 종료봉 {window[-1].start:%H:%M}\n{reply}")
                except Exception:
                    pass
            if getattr(replies, "uncertain", False):
                self._pause_uncertain(notify)
        except Exception as error:
            if signal is not None and not decision_attempted:
                reason = getattr(error, "skipped_reason", "monitor_error")
                try:
                    note("skipped", reason if reason in ENTRY_SKIP_REASONS else "monitor_error")
                except Exception:
                    pass  # Existing marker remains; storage failures never retry an order.
            if symbol is None:
                self.reset()
            elif not keep_recent_missing_baseline(error, self._baseline.get(symbol), self._moment()):
                self._baseline.pop(symbol, None)
            if isinstance(error, (GoldenCrossError, CrossMonitorError, MinuteChartError, CooldownUnavailable)) or (isinstance(error, BrokerAPIError) and error.ip_related):
                message = str(error)
                if message.startswith("ddcrs:"):
                    message = "gdcrs:" + message[len("ddcrs:"):]
            else:
                message = "gdcrs: 조회·설정 또는 주문 시도 기록을 확인하지 못해 주문하지 않았습니다."
            if symbol is not None:
                message = f"gdcrs · {symbol} · {message}"
                previous_error = self._symbol_errors.get(symbol, {}).get("message")
                self._symbol_errors[symbol] = {"message": message,
                    "observed_at": self._moment().isoformat(),
                    "details": deepcopy(getattr(error, "details", {}))}
                if previous_error == message:
                    self._error = message
                    return
            self._message(notify, message)
