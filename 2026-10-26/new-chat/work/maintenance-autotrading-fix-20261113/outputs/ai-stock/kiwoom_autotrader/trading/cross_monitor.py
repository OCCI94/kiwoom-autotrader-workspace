"""Serial, opt-in death-cross monitoring of current KRX cash holdings.

Call tick on the command dispatch thread, never concurrently with login or order
commands. Each tick observes at most one symbol and attempts at most one sell.
Only newly closed 1-minute candles can trigger; startup, unverified gaps,
settings/session transitions and revised history seed a new baseline. A bounded
paper-only source gap can be reconciled after a completed right anchor; no
crossing in the inferred past is replayed. There is
no holiday calendar: closed/current-day candle freshness is additionally required.
Large portfolios rotate fairly between scans. A symbol not observed in successive
minutes cannot trade until consecutive fresh observations are available again.
"""
from __future__ import annotations

from dataclasses import dataclass
from copy import deepcopy
from kiwoom_autotrader.api.errors import BrokerAPIError
from datetime import datetime, timedelta
from pathlib import Path
from kiwoom_autotrader.utils.paths import app_data_root
import os
import re
import sqlite3
import uuid

from kiwoom_autotrader.api.account_report import fetch_account_report
from kiwoom_autotrader.api.minute_chart import GapProof, MinuteBar, MinuteChartError, fetch_minute_chart, session_identity
from kiwoom_autotrader.api.session import KST
from kiwoom_autotrader.config.settings import load_settings, save_settings
from kiwoom_autotrader.telegram.commands.orders import handle_sell, OrderReplies
from kiwoom_autotrader.trading.decision_evidence import cross_evidence, strategy_source_hash, SOURCE_KINDS
from kiwoom_autotrader.trading.order_context import order_decision, _current


class CrossMonitorError(RuntimeError):
    pass


class MinuteWindowError(CrossMonitorError):
    """A data gap with source omissions and bounded inference kept distinct."""
    def __init__(self, message, *, now, long, bars, reconciled_bars=None):
        super().__init__(message)
        minute = now.replace(second=0, microsecond=0)
        expected = [minute - timedelta(minutes=i) for i in range(long + 1, 0, -1)]
        observed = {bar.start for bar in bars if bar.start < minute}
        reconciled = bars if reconciled_bars is None else reconciled_bars
        available = {bar.start for bar in reconciled if bar.start < minute}
        inferred = {bar.start for bar in reconciled if bar.start < minute and bar.gap_proof is not None}
        self.details = {"observed_at": now.isoformat(), "required_minutes": long,
            "latest_observed_closed_bar": max(observed).isoformat() if observed else None,
            "source_missing_minutes": [at.isoformat() for at in expected if at not in observed],
            "inferred_minutes": [at.isoformat() for at in expected if at in inferred],
            "missing_minutes": [at.isoformat() for at in expected if at not in available],
            "cause": "unverified_source_gap", "interpolated": bool(inferred.intersection(expected))}


def _now() -> datetime:
    return datetime.now(KST)


def marker_path() -> Path:
    return app_data_root() / "data/ddcrs-order-markers.sqlite3"


class OrderMarkers:
    """A committed claim precedes submission, including ambiguous failures."""

    def __init__(self, path: Path | None = None):
        self.path = path or marker_path()

    def claim(self, mode: str, symbol: str, candle: datetime) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.path.is_symlink() or (self.path.exists() and not self.path.is_file()):
            raise CrossMonitorError("ddcrs: 주문 시도 기록 파일을 확인하지 못했습니다.")
        with sqlite3.connect(self.path, timeout=2) as database:
            os.chmod(self.path, 0o600)
            database.execute("PRAGMA synchronous=FULL")
            database.execute("CREATE TABLE IF NOT EXISTS attempts (mode TEXT NOT NULL, symbol TEXT NOT NULL, candle TEXT NOT NULL, PRIMARY KEY (mode,symbol,candle))")
            inserted = database.execute("INSERT OR IGNORE INTO attempts VALUES (?,?,?)", (mode, symbol, candle.isoformat())).rowcount == 1
            database.commit()
            return inserted


def _quantity(value) -> int:
    if not isinstance(value, str) or re.fullmatch(r"\+?[0-9]{1,15}", value) is None:
        raise CrossMonitorError("ddcrs: 보유·매매가능 수량을 확인하지 못했습니다.")
    result = int(value)
    if result > 999999999999:
        raise CrossMonitorError("ddcrs: 주문 수량 한도를 초과합니다.")
    return result


def sellable_holdings(report, mode: str) -> dict[str, int]:
    """Only unambiguous cash shares; never substitute rmnd_qty for sellable."""
    if (not isinstance(report, dict) or report.get("mode") != mode or report.get("exchange") != "KRX"
            or report.get("snapshot_changed") is not False or not isinstance(report.get("holdings"), list)):
        raise CrossMonitorError("ddcrs: 완전하고 일관된 KRX 잔고를 확인하지 못했습니다.")
    quantities = {}
    blocked = set()
    for row in report["holdings"]:
        if not isinstance(row, dict):
            raise CrossMonitorError("ddcrs: 보유종목 응답 형식을 확인하지 못했습니다.")
        raw_code = row.get("stk_cd")
        if not isinstance(raw_code, str) or re.fullmatch(r"A?[0-9]{6}", raw_code) is None:
            continue  # J/Q securities are not the ordinary cash-stock sell TR.
        symbol = raw_code[-6:]
        if symbol in quantities or symbol in blocked:
            quantities.pop(symbol, None)
            blocked.add(symbol)
            continue
        if row.get("crd_tp") != "00" or row.get("crd_loan_dt") not in (None, ""):
            blocked.add(symbol)
            continue
        quantity = _quantity(row.get("trde_able_qty"))
        held = _quantity(row.get("rmnd_qty"))
        if quantity > held:
            raise CrossMonitorError("ddcrs: 매매가능 수량이 보유수량보다 큽니다.")
        quantities[symbol] = quantity
    return {symbol: quantity for symbol, quantity in quantities.items() if quantity > 0}


@dataclass(frozen=True)
class Baseline:
    bars: tuple[MinuteBar, ...]
    difference: int


def _infer_zero_volume_gaps(closed, now, long):
    """Use a bounded, explicit assumption only between two source anchors.

    Kiwoom documents per-bar and cumulative volume but does not guarantee
    omission semantics, finality, or VI/halt history. This is therefore an
    *inferred* carry-forward close, not a returned bar or a claim of complete
    exchange data. Never infer the newest closed minute: its right anchor must
    itself be completed. Real mode and unanchored/cross-page gaps stay blocked.
    """
    if len(closed) < 2:
        return closed
    first_needed = now.replace(second=0, microsecond=0) - timedelta(minutes=long + 1)
    result = []
    inferred = 0
    for left, right in zip(closed, closed[1:]):
        result.append(left)
        delta = right.start - left.start
        if delta <= timedelta(minutes=1):
            continue
        missing = int(delta.total_seconds() // 60) - 1
        needed = [left.start + timedelta(minutes=offset) for offset in range(1, missing + 1)
                  if left.start + timedelta(minutes=offset) >= first_needed] if 1 <= missing <= 3 else []
        eligible = (
            delta == timedelta(minutes=missing + 1) and 1 <= missing <= 3
            and bool(needed) and inferred + len(needed) <= 10
            and left.start.date() == right.start.date() == now.date()
            and left.start.utcoffset() == right.start.utcoffset() == now.utcoffset() == timedelta(hours=9)
            and (left.start.hour, left.start.minute) >= (9, 0)
            and (right.start.hour, right.start.minute) < (15, 20)
            and right.start + timedelta(minutes=1) <= now
            and left.gap_proof is None and right.gap_proof is None
            and left.source_mode == right.source_mode == "paper"
            and isinstance(left.source_symbol, str) and re.fullmatch(r"[0-9]{6}", left.source_symbol)
            and left.source_symbol == right.source_symbol
            and isinstance(left.source_batch, str) and bool(left.source_batch)
            and left.source_batch == right.source_batch
            and type(left.source_page) is int and left.source_page == right.source_page
            and type(left.source_row) is int and type(right.source_row) is int
            and left.source_row == right.source_row + 1  # adjacent descending response rows
            and isinstance(left.source_response_sha256, str)
            and re.fullmatch(r"[0-9a-f]{64}", left.source_response_sha256) is not None
            and left.source_response_sha256 == right.source_response_sha256
            and isinstance(left.source_query_started_at, datetime)
            and left.source_query_started_at == right.source_query_started_at
            and right.start + timedelta(minutes=1) <= right.source_query_started_at <= now
            and left.adjustment == right.adjustment == ""
            and left.adjustment_rate == right.adjustment_rate == ""
            and type(left.volume) is type(right.volume) is int
            and type(left.acc_volume) is type(right.acc_volume) is int
            and 0 < left.volume <= left.acc_volume and right.volume > 0
            and right.acc_volume - left.acc_volume == right.volume
        )
        if not eligible:
            continue
        proof = GapProof(left.source_symbol, left.source_mode, left.start, left.close,
                         left.volume, left.acc_volume, right.start, right.close,
                         right.volume, right.acc_volume, left.source_response_sha256, left.source_query_started_at)
        for at in needed:
            result.append(MinuteBar(at, left.close,
                volume=0, acc_volume=left.acc_volume, adjustment="", adjustment_rate="",
                source_symbol=left.source_symbol, source_mode=left.source_mode,
                source_query_started_at=left.source_query_started_at, gap_proof=proof))
        inferred += len(needed)
    result.append(closed[-1])
    return tuple(result)


def closed_window(bars, now: datetime, long: int) -> tuple[MinuteBar, ...]:
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise CrossMonitorError("ddcrs: 1분봉 관측 시각을 확인하지 못했습니다.")
    minute = now.replace(second=0, microsecond=0)
    if not isinstance(bars, (list, tuple)) or any(not isinstance(bar, MinuteBar) for bar in bars):
        raise CrossMonitorError("ddcrs: 1분봉 자료를 확인하지 못했습니다.")
    if any(bar.start.tzinfo is None or type(bar.close) is not int or bar.close <= 0 for bar in bars):
        raise CrossMonitorError("ddcrs: 1분봉 시간·가격을 확인하지 못했습니다.")
    if len({bar.start for bar in bars}) != len(bars):
        raise CrossMonitorError("ddcrs: 중복 1분봉이 있습니다.")
    if any(bar.start > minute for bar in bars):
        raise CrossMonitorError("ddcrs: 미래 시각의 1분봉이 있습니다.")
    if any(bar.source_symbol is not None and (not isinstance(bar.source_query_started_at, datetime)
           or bar.source_query_started_at.tzinfo is None or bar.source_query_started_at.utcoffset() is None
           or bar.source_query_started_at > now) for bar in bars):
        raise CrossMonitorError("ddcrs: 1분봉 조회 시작 시각을 확인하지 못했습니다.")
    # Wall clock time at *response* is insufficient: a request started before
    # this candle closed may contain its forming snapshot after the boundary.
    closed = tuple(sorted((bar for bar in bars if bar.start < minute and
        (bar.source_symbol is None or bar.start + timedelta(minutes=1) <= bar.source_query_started_at)),
        key=lambda bar: bar.start))
    raw_closed = closed
    closed = _infer_zero_volume_gaps(closed, now, long)
    if len(closed) < long or closed[-1].start != minute - timedelta(minutes=1):
        raise MinuteWindowError("ddcrs: 최신 종료 1분봉 또는 이동평균 이력이 부족합니다.",
                                now=now, long=long, bars=raw_closed, reconciled_bars=closed)
    window = closed[-(long + 1):]
    if any(bar.start.date() != now.date() or bar.start.hour < 9 or bar.start.second != 0 for bar in window):
        raise CrossMonitorError("ddcrs: 당일 정규장 1분봉이 아닙니다.")
    if any(right.start - left.start != timedelta(minutes=1) for left, right in zip(window, window[1:])):
        raise MinuteWindowError("ddcrs: 1분봉 사이에 누락된 분이 있습니다.",
                                now=now, long=long, bars=raw_closed, reconciled_bars=closed)
    return window


def sma_difference(window: tuple[MinuteBar, ...], short: int, long: int) -> int:
    # Positive denominator short*long is omitted: exact comparison, no floats.
    return sum(bar.close for bar in window[-short:]) * long - sum(bar.close for bar in window[-long:]) * short


def continuous_cross_previous(previous: Baseline | None, window: tuple[MinuteBar, ...],
                              short: int, long: int, *, side: str) -> int | None:
    """Return the immediately preceding difference only for a fresh crossing.

    A temporary missing newest bar may be bridged only after every omitted bar
    has a same-page, cumulative-volume gap proof. Never replay a crossing that
    occurred in a previously unobserved inferred bar.
    """
    if (previous is None or len(previous.bars) != long or len(window) != long + 1
            or side not in ("buy", "sell")):
        return None
    delta = window[-1].start - previous.bars[-1].start
    steps = int(delta.total_seconds() // 60)
    if delta != timedelta(minutes=steps) or not 1 <= steps <= 4:
        return None
    overlap = long + 1 - steps
    if overlap <= 0 or window[:overlap] != previous.bars[-overlap:]:
        return None
    skipped = window[overlap:-1]
    if any(bar.gap_proof is None or bar.close != previous.bars[-1].close for bar in skipped):
        return None
    prior = previous.difference
    if sma_difference(previous.bars, short, long) != prior:
        return None
    rolling = list(previous.bars)
    for inferred in skipped:
        rolling.append(inferred)
        current = sma_difference(tuple(rolling), short, long)
        # If the crossing happened while the feed had no completed right anchor,
        # it is historical by the time the gap can be proved. Do not replay it.
        if (side == "buy" and prior <= 0 < current) or (side == "sell" and prior >= 0 > current):
            return None
        prior = current
    current = sma_difference(window, short, long)
    if (side == "buy" and prior <= 0 < current) or (side == "sell" and prior >= 0 > current):
        return prior
    return None


def keep_recent_missing_baseline(error, previous, now) -> bool:
    """Retain a warm baseline only for an unverified newest closed minute.

    This does not authorize a trade; later reconciliation must prove every
    missing candle and ensure the newly closed candle owns the crossing.
    """
    if not isinstance(error, MinuteWindowError) or previous is None or not previous.bars:
        return False
    minute = now.replace(second=0, microsecond=0)
    age = minute - previous.bars[-1].start
    return (previous.bars[-1].start.date() == minute.date()
            and timedelta(minutes=2) <= age <= timedelta(minutes=4)
            and (minute - timedelta(minutes=1)).isoformat() in error.details.get("missing_minutes", []))


class CrossMonitor:
    def __init__(self, *, settings_loader=None, settings_saver=None, now=None, state_path: Path | None = None,
                 account_fetcher=None, chart_fetcher=None, sell_handler=None, markers=None,
                 stop_requested=None, chart_source_kind=None, ownership_reader=None):
        self.settings_loader = settings_loader or load_settings
        self.settings_saver = settings_saver or save_settings
        self.now = now or _now
        self.account_fetcher = account_fetcher or fetch_account_report
        self.chart_fetcher = chart_fetcher or fetch_minute_chart
        self._evidence_run_id = uuid.uuid4().hex
        self._strategy_source_hash = strategy_source_hash(__file__)
        self._chart_source_kind = chart_source_kind or ("broker_observation" if chart_fetcher is None else "unknown")
        if self._chart_source_kind not in SOURCE_KINDS:
            raise ValueError("분봉 근거의 출처 구분이 올바르지 않습니다.")
        self.sell_handler = sell_handler or handle_sell
        self.markers = markers or OrderMarkers(state_path)
        self.stop_requested = stop_requested or (lambda: False)
        self.ownership_reader = ownership_reader or self._owned_quantity
        self._ownership_diagnostics = {}
        self._identity = None
        self._baseline: dict[str, Baseline] = {}
        self._scan: list[str] = []
        self._scan_at: datetime | None = None
        self._next_at: datetime | None = None
        self._error = None
        self._symbol_errors = {}
        # Informational incidents are never replayed as orders on restart.
        self._signal_incidents = {}
        self._baseline_warnings = {}
        self._negative_warning_active = set()
        self._warning_session_identity = None
        self._enabled = False
        self._last_symbol: str | None = None
        self._halted = False
        self._generation = 0
        self._load_recent_signal_incidents()
        self._load_baseline_warnings()

    @staticmethod
    def _owned_quantity(session, symbol, now):
        context = _current.get()
        if context is None or session.mode != "paper":
            raise CrossMonitorError("ddcrs: 자동전략 소유 수량이 미확정입니다.")
        position = context.tracker.multi_strategy_position("paper", symbol, "ddcrs", now)
        if (not isinstance(position, dict) or position.get("status") != "owned"
                or type(position.get("owned_qty")) is not int or position["owned_qty"] <= 0):
            raise CrossMonitorError("ddcrs: 원장과 단위체결로 자동전략 소유 수량을 확인하지 못했습니다.")
        return position["owned_qty"]

    def _owned_holdings(self, session, report, mode, now, notify):
        raw = sellable_holdings(report, mode)
        result = {}
        for symbol, quantity in raw.items():
            try:
                owned = self.ownership_reader(session, symbol, now)
                if type(owned) is not int or owned <= 0:
                    raise ValueError
                result[symbol] = min(quantity, owned)
                self._ownership_diagnostics.pop(symbol, None)
            except Exception:
                changed = symbol not in self._ownership_diagnostics
                self._ownership_diagnostics[symbol] = {"reason":"automatic_ownership_unverified",
                    "observed_at":now.isoformat(),"broker_sellable_qty":quantity}
                if changed:
                    self._message(notify, f"ddcrs · {symbol}: 자동전략 소유 수량이 미확정이어서 자동매도를 보류합니다.")
        return result

    def reset(self, *, clear_halt: bool = False):
        """Clear warmup. Only an explicit start ddcrs may clear an order halt."""
        self._generation += 1
        self._identity = None
        self._baseline.clear()
        self._scan.clear()
        self._scan_at = None
        self._next_at = None
        self._enabled = False
        self._last_symbol = None
        self._symbol_errors.clear()
        if clear_halt:
            self._halted = False

    def seconds_until_next(self) -> float | None:
        if not self._enabled:
            return None
        return max(0.0, (self._next_at - self.now()).total_seconds()) if self._next_at else 1.0

    def _context(self, session):
        settings = self.settings_loader()
        if settings.get("ddcrs_enabled") is not True:
            return None
        short, long = settings.get("gdcrs_short_period"), settings.get("gdcrs_long_period")
        if type(short) is not int or type(long) is not int or not 1 <= short < long <= 60:
            raise CrossMonitorError("ddcrs: 1 ≤ 단기 < 장기 ≤ 60으로 gdcrs intv를 설정하세요. 주문하지 않습니다.")
        return (session_identity(session), short, long, settings.get("market_start_time", "09:00"), settings.get("market_end_time", "15:30"))

    def status(self):
        return {"enabled": self._enabled, "halted": self._halted, "error": self._error,
                "symbol_errors": deepcopy(self._symbol_errors),
                "ownership_diagnostics": deepcopy(self._ownership_diagnostics),
                "missed_signals": deepcopy(list(self._signal_incidents.values())),
                "baseline_warnings": deepcopy(list(self._baseline_warnings.values())),
                "observed_closed_bars": {code: baseline.bars[-1].start.isoformat()
                    for code, baseline in self._baseline.items() if baseline.bars},
                "next_check_at": self._next_at.isoformat() if self._next_at else None}

    def _load_recent_signal_incidents(self):
        """Restore only recent diagnostics, never order/strategy state."""
        try:
            path = Path(self.markers.path)
            if path.is_symlink() or not path.is_file():
                return
            with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.2) as database:
                database.execute("PRAGMA query_only=ON")
                database.execute("PRAGMA trusted_schema=OFF")
                exists = database.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='missed_signals'").fetchone()
                if not exists:
                    return
                rows = database.execute("SELECT mode,symbol,candle,observed_at,reason FROM missed_signals ORDER BY rowid DESC LIMIT 100").fetchall()
            for mode, symbol, candle, observed_at, reason in reversed(rows):
                if (mode not in ("paper", "real") or not isinstance(symbol, str)
                        or re.fullmatch(r"[0-9]{6}", symbol) is None
                        or not isinstance(reason, str) or re.fullmatch(r"[a-z_]{1,80}", reason) is None
                        or not isinstance(candle, str) or len(candle) > 40
                        or not isinstance(observed_at, str) or len(observed_at) > 40):
                    continue
                if (datetime.fromisoformat(candle).utcoffset() is None
                        or datetime.fromisoformat(observed_at).utcoffset() is None):
                    continue
                key = (mode, symbol, candle, reason)
                self._signal_incidents[key] = {"mode": mode, "symbol": symbol, "candle": candle,
                    "observed_at": observed_at, "reason": reason, "durable": True}
        except (OSError, sqlite3.Error, TypeError, ValueError, AttributeError):
            # A diagnostic DB is never an authorization to trade or replay.
            return

    def _load_baseline_warnings(self):
        """Load informational history only; it can never authorize an order."""
        import math
        try:
            path = Path(self.markers.path)
            if path.is_symlink() or not path.is_file():
                return
            with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.2) as database:
                database.execute("PRAGMA query_only=ON")
                database.execute("PRAGMA trusted_schema=OFF")
                exists = database.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='baseline_warnings'").fetchone()
                if not exists:
                    return
                rows = database.execute("SELECT mode,symbol,candle,observed_at,difference,reason,short_period,long_period FROM baseline_warnings ORDER BY rowid DESC LIMIT 100").fetchall()
            for mode, symbol, candle, observed_at, difference, reason, short, long in reversed(rows):
                if (mode not in ("paper", "real") or not isinstance(symbol, str)
                        or re.fullmatch(r"[0-9]{6}", symbol) is None
                        or reason not in ("initial_or_resumed", "observation_discontinuity")
                        or not isinstance(candle, str) or len(candle) > 40
                        or not isinstance(observed_at, str) or len(observed_at) > 40
                        or not isinstance(difference, (int, float)) or not math.isfinite(difference) or difference >= 0
                        or type(short) is not int or type(long) is not int or not 1 <= short < long <= 60):
                    continue
                if (datetime.fromisoformat(candle).utcoffset() is None
                        or datetime.fromisoformat(observed_at).utcoffset() is None):
                    continue
                key = (mode, symbol, observed_at)
                self._baseline_warnings[key] = {"mode": mode, "symbol": symbol, "candle": candle,
                    "observed_at": observed_at, "difference_numerator": difference, "short_period": short,
                    "long_period": long, "reason": reason, "durable": True}
        except (OSError, sqlite3.Error, TypeError, ValueError, AttributeError):
            return

    def _warn_negative_baseline(self, *, mode, symbol, candle, observed_at, difference, short, long, reason, notify):
        """Record unknown history without interpreting it as a trade signal."""
        identity = (mode, symbol)
        if identity in self._negative_warning_active:
            return
        self._negative_warning_active.add(identity)
        event = {"mode": mode, "symbol": symbol, "candle": candle.isoformat(),
                 "observed_at": observed_at.isoformat(), "difference_numerator": difference,
                 "short_period": short, "long_period": long, "reason": reason, "durable": False}
        try:
            path = Path(self.markers.path)
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if path.is_symlink() or (path.exists() and not path.is_file()):
                raise OSError("invalid warning path")
            with sqlite3.connect(path, timeout=2) as database:
                os.chmod(path, 0o600)
                database.execute("PRAGMA synchronous=FULL")
                database.execute("CREATE TABLE IF NOT EXISTS baseline_warnings (mode TEXT NOT NULL, symbol TEXT NOT NULL, candle TEXT NOT NULL, observed_at TEXT NOT NULL, difference INTEGER NOT NULL, reason TEXT NOT NULL, short_period INTEGER NOT NULL, long_period INTEGER NOT NULL)")
                database.execute("INSERT INTO baseline_warnings VALUES (?,?,?,?,?,?,?,?)",
                                 (mode, symbol, event["candle"], event["observed_at"], event["difference_numerator"], reason, short, long))
                database.commit()
            event["durable"] = True
        except Exception:
            pass
        self._baseline_warnings[(mode, symbol, event["observed_at"])] = event
        while len(self._baseline_warnings) > 100:
            self._baseline_warnings.pop(next(iter(self._baseline_warnings)))
        message = (f"ddcrs · {symbol}: 감시 시작 또는 관측 재개 시 단기평균이 장기평균보다 낮습니다. "
                   "과거 교차 발생 여부와 시각은 확인되지 않아 자동 매도를 실행하지 않았습니다. "
                   "최근 잔고 조회 기준 종목의 보유·주문내역을 확인하세요.")
        if not event["durable"]:
            message += " 경고 기록 저장도 실패했습니다."
        try:
            print(message, flush=True)
        except OSError:
            pass
        try:
            notify(message)
        except Exception:
            pass

    def _moment(self) -> datetime:
        value = self.now()
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise CrossMonitorError("ddcrs: 한국 시간 시계를 확인하지 못했습니다.")
        return value.astimezone(KST)

    @staticmethod
    def _market_open(now: datetime, context) -> bool:
        start, end = context[3:]
        if (not isinstance(start, str) or not isinstance(end, str)
                or re.fullmatch(r"[0-2][0-9]:[0-5][0-9]", start) is None
                or re.fullmatch(r"[0-2][0-9]:[0-5][0-9]", end) is None):
            raise CrossMonitorError("ddcrs: 장 시간 설정을 확인하지 못했습니다.")
        return now.weekday() < 5 and max("09:00", start) <= now.strftime("%H:%M") < min("15:30", end)

    def _message(self, notify, message):
        if self._error != message:
            self._error = message
            try:
                notify(message)
            except Exception:
                pass  # Notification failure cannot cause a broker retry.

    def _record_missed_signal(self, *, mode, symbol, candle, observed_at, reason, notify):
        """Keep an audit incident, never an executable pending order.

        A loss of signal can be consequential even when failing closed is the
        right decision. The incident is committed separately from an order
        marker, and can never cause a replay or a fresh order on restart.
        """
        allowed = {"rollover_revalidation_failed", "rollover_reversed_or_revised",
                   "rollover_multiple_minutes", "rollover_send_boundary", "no_sellable_quantity",
                   "account_or_chart_unavailable", "context_changed", "attempt_marker_unavailable",
                   "order_not_submitted"}
        if reason not in allowed:
            reason = "account_or_chart_unavailable"
        event = {"mode": mode, "symbol": symbol, "candle": candle.isoformat(),
                 "observed_at": observed_at.isoformat(), "reason": reason, "durable": False}
        try:
            # Injected custom markers may expose a path; an unavailable path
            # must not hide the incident or weaken the order guard.
            path = Path(self.markers.path)
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if path.is_symlink() or (path.exists() and not path.is_file()):
                raise OSError("invalid incident path")
            with sqlite3.connect(path, timeout=2) as database:
                os.chmod(path, 0o600)
                database.execute("PRAGMA synchronous=FULL")
                database.execute("CREATE TABLE IF NOT EXISTS missed_signals (mode TEXT NOT NULL, symbol TEXT NOT NULL, candle TEXT NOT NULL, observed_at TEXT NOT NULL, reason TEXT NOT NULL, PRIMARY KEY(mode,symbol,candle,reason))")
                database.execute("INSERT OR IGNORE INTO missed_signals VALUES (?,?,?,?,?)",
                                 (mode, symbol, event["candle"], event["observed_at"], reason))
                database.commit()
            event["durable"] = True
        except Exception:
            pass
        key = (mode, symbol, event["candle"], reason)
        self._signal_incidents[key] = event
        # Keep status bounded; the complete record remains in SQLite.
        while len(self._signal_incidents) > 100:
            self._signal_incidents.pop(next(iter(self._signal_incidents)))
        message = (f"ddcrs · {symbol} · {candle:%H:%M} 종료봉 교차 후 자동매도를 실행하지 못했습니다 "
                   f"({reason}). 보유·주문내역과 최신 시세를 확인하세요.")
        if not event["durable"]:
            message += " 신호 미실행 기록 저장도 실패했습니다."
        self._symbol_errors[symbol] = {"message": message, "observed_at": self._moment().isoformat(),
                                       "details": {"missed_signal": deepcopy(event)}}
        self._message(notify, message)

    def _pause_uncertain(self, notify):
        self._halted = True
        self.reset()
        message = "ddcrs: 주문 접수 여부가 불명확하여 자동매도를 중지했습니다. 증권사 주문내역을 확인한 뒤 start ddcrs로 다시 시작하세요."
        try:
            self.settings_saver({"ddcrs_enabled": False})
        except Exception:
            message += " 중지 상태 저장에 실패해 현재 프로세스에서만 중지했습니다. 재시작 전에 설정과 주문내역을 확인하세요."
        self._message(notify, message)

    def tick(self, session, notify) -> None:
        """Run serially with commands. No login, retry, background order or catch-up."""
        try:
            if self._halted:
                return
            if self.stop_requested():
                self.reset()
                return
            context = self._context(session)
            now = self._moment()
            if context is None:
                self.reset()
                self._error = None
                return
            if context != self._identity:
                self.reset()
                self._identity = context
            if context[0] != self._warning_session_identity:
                self._negative_warning_active.clear()
                self._warning_session_identity = context[0]
            self._enabled = True
            if not self._market_open(now, context):
                self._baseline.clear()
                self._scan.clear()
                self._scan_at = None
                self._next_at = now + timedelta(seconds=5)
                return
            if self._next_at and now < self._next_at:
                return
            self._next_at = now + timedelta(seconds=1)
            minute = now.replace(second=0, microsecond=0)
            generation = self._generation
            # An unfinished old-minute scan is discarded, never caught up.
            if self._scan_at != minute:
                holdings = self._owned_holdings(session, self.account_fetcher(session), context[0][0], self._moment(), notify)
                if (self.stop_requested() or self._generation != generation
                        or self._context(session) != context):
                    self.reset()
                    return
                symbols = sorted(holdings)
                self._negative_warning_active = {
                    key for key in self._negative_warning_active
                    if key[0] == context[0][0] and key[1] in holdings
                }
                if self._last_symbol is not None:
                    symbols = ([code for code in symbols if code > self._last_symbol]
                               + [code for code in symbols if code <= self._last_symbol])
                self._scan = symbols
                self._scan_at = minute
                self._baseline = {code: value for code, value in self._baseline.items() if code in holdings}
                self._symbol_errors = {code: value for code, value in self._symbol_errors.items() if code in holdings}
                # The holdings request is this step's work; the next step reads one chart.
                return
            if not self._scan:
                self._next_at = minute + timedelta(minutes=1)
                return
            symbol = self._scan.pop(0)
            self._last_symbol = symbol
            short, long = context[1:3]
            try:
                bars = self.chart_fetcher(session, symbol, count=long + 2)
                observed_at = self._moment()
                if (self.stop_requested() or self._generation != generation
                        or self._context(session) != context or not self._market_open(observed_at, context)):
                    self.reset()
                    return
                window = closed_window(bars, observed_at, long)
                self._symbol_errors.pop(symbol, None)
                difference = sma_difference(window, short, long)
                previous = self._baseline.get(symbol)
                self._baseline[symbol] = Baseline(window[-long:], difference)
                cross_previous = continuous_cross_previous(previous, window, short, long, side="sell")
                continuous = (previous is not None and len(window) == long + 1 and window[:-1] == previous.bars) or cross_previous is not None
                warning_key = (context[0][0], symbol)
                if difference >= 0:
                    self._negative_warning_active.discard(warning_key)
                elif not continuous:
                    self._warn_negative_baseline(mode=context[0][0], symbol=symbol,
                        candle=window[-1].start, observed_at=observed_at, difference=difference, short=short, long=long,
                        reason="initial_or_resumed" if previous is None else "observation_discontinuity", notify=notify)
                # Both the timestamp and every overlapping observed price must
                # match. A revision or skipped observation only seeds a baseline.
                crossed = cross_previous is not None
                if not crossed:
                    self._error = next((item["message"] for item in self._symbol_errors.values()), None)
                    return
                # Re-read sellable quantity just before submission. A preexisting
                # manual sell/order changes trde_able_qty and is respected here.
                try:
                    fresh = self._owned_holdings(session, self.account_fetcher(session), context[0][0], self._moment(), notify)
                except Exception:
                    self._baseline.pop(symbol, None)
                    self._record_missed_signal(mode=context[0][0], symbol=symbol,
                        candle=window[-1].start, observed_at=observed_at,
                        reason="account_or_chart_unavailable", notify=notify)
                    return
                final_now = self._moment()
                try:
                    changed = (self.stop_requested() or self._generation != generation
                               or self._context(session) != context or not self._market_open(final_now, context))
                except Exception:
                    changed = True
                if changed:
                    self._record_missed_signal(mode=context[0][0], symbol=symbol,
                        candle=window[-1].start, observed_at=observed_at,
                        reason="context_changed", notify=notify)
                    self.reset()
                    return
                rollover_window = rollover_observed_at = None
                signal_minute = observed_at.replace(second=0, microsecond=0)
                if final_now.replace(second=0, microsecond=0) != signal_minute:
                    # The signal was detected live before the boundary, not
                    # reconstructed after restart. Give it one bounded chance:
                    # re-read exactly the next completed bar and verify the
                    # unchanged overlap and still-negative difference.
                    if final_now.replace(second=0, microsecond=0) != signal_minute + timedelta(minutes=1):
                        self._baseline.pop(symbol, None)
                        self._record_missed_signal(mode=context[0][0], symbol=symbol,
                            candle=window[-1].start, observed_at=observed_at,
                            reason="rollover_multiple_minutes", notify=notify)
                        return
                    try:
                        newer = self.chart_fetcher(session, symbol, count=long + 2)
                        rollover_observed_at = self._moment()
                        if (self.stop_requested() or self._generation != generation
                                or self._context(session) != context or not self._market_open(rollover_observed_at, context)
                                or rollover_observed_at.replace(second=0, microsecond=0) != signal_minute + timedelta(minutes=1)):
                            raise CrossMonitorError("ddcrs: 재검증 중 세션 또는 시간이 변경되었습니다.")
                        rollover_window = closed_window(newer, rollover_observed_at, long)
                        if len(rollover_window) != long + 1:
                            raise CrossMonitorError("ddcrs: 재검증 이력이 부족합니다.")
                        if (rollover_window[:-1] != window[1:]
                                or sma_difference(rollover_window, short, long) >= 0):
                            self._baseline.pop(symbol, None)
                            self._record_missed_signal(mode=context[0][0], symbol=symbol,
                                candle=window[-1].start, observed_at=observed_at,
                                reason="rollover_reversed_or_revised", notify=notify)
                            return
                        # Chart reads can be slow: obtain sellable holdings again
                        # after it and preserve the current-minute guard.
                        fresh = self._owned_holdings(session, self.account_fetcher(session), context[0][0], self._moment(), notify)
                        final_now = self._moment()
                        if (self.stop_requested() or self._generation != generation
                                or self._context(session) != context or not self._market_open(final_now, context)
                                or final_now.replace(second=0, microsecond=0) != signal_minute + timedelta(minutes=1)):
                            raise CrossMonitorError("ddcrs: 재검증 후 상태 또는 시간이 변경되었습니다.")
                    except Exception:
                        self._baseline.pop(symbol, None)
                        self._record_missed_signal(mode=context[0][0], symbol=symbol,
                            candle=window[-1].start, observed_at=observed_at,
                            reason="rollover_revalidation_failed", notify=notify)
                        return
                    self._baseline[symbol] = Baseline(rollover_window[-long:],
                                                       sma_difference(rollover_window, short, long))
                quantity = fresh.get(symbol, 0)
                if quantity <= 0:
                    self._record_missed_signal(mode=context[0][0], symbol=symbol,
                        candle=window[-1].start, observed_at=observed_at,
                        reason="no_sellable_quantity", notify=notify)
                    return
                latest_candle = (rollover_window or window)[-1].start
                dispatch_failure = {"minute_changed": False}
                def before_send():
                    if (self.stop_requested() or self._halted or self._generation != generation
                            or self._context(session) != context):
                        raise CrossMonitorError("ddcrs: 전송 대기 중 상태 또는 종료봉이 바뀌어 주문하지 않았습니다.")
                    # Settings/session lookup can cross a minute boundary too.
                    dispatch_now = self._moment()
                    if (not self._market_open(dispatch_now, context)
                            or dispatch_now.replace(second=0, microsecond=0) - timedelta(minutes=1) != latest_candle):
                        if dispatch_now.replace(second=0, microsecond=0) - timedelta(minutes=1) != latest_candle:
                            dispatch_failure["minute_changed"] = True
                        raise CrossMonitorError("ddcrs: 전송 대기 중 상태 또는 종료봉이 바뀌어 주문하지 않았습니다.")
                evaluated_at = self._moment()
                try:
                    evidence = cross_evidence(
                        strategy="ddcrs", run_id=self._evidence_run_id, source_hash=self._strategy_source_hash,
                        source_kind=self._chart_source_kind, observed_at=observed_at, evaluated_at=evaluated_at,
                        config={"enabled": True, "short_period": short, "long_period": long,
                                "market_start_time": context[3], "market_end_time": context[4]},
                        window=window, previous_difference=cross_previous, difference=difference,
                        rollover_window=rollover_window, rollover_observed_at=rollover_observed_at)
                except Exception:
                    self._baseline.pop(symbol, None)
                    self._record_missed_signal(mode=context[0][0], symbol=symbol,
                        candle=window[-1].start, observed_at=observed_at,
                        reason="rollover_revalidation_failed" if rollover_window is not None else "account_or_chart_unavailable",
                        notify=notify)
                    return
                try:
                    claimed = self.markers.claim(context[0][0], symbol, window[-1].start)
                except Exception:
                    self._record_missed_signal(mode=context[0][0], symbol=symbol,
                        candle=window[-1].start, observed_at=observed_at,
                        reason="attempt_marker_unavailable", notify=notify)
                    return
                if not claimed:
                    return
                # A committed marker is retained even on timeout, exception, a
                # provider rejection, or failed Telegram notification.
                try:
                    with order_decision(evidence):
                        replies = self.sell_handler(f"sell {symbol} {quantity}", session, before_send=before_send)
                except Exception:
                    self._pause_uncertain(notify)
                    return
                if dispatch_failure["minute_changed"]:
                    self._record_missed_signal(mode=context[0][0], symbol=symbol,
                        candle=window[-1].start, observed_at=observed_at,
                        reason="rollover_send_boundary", notify=notify)
                elif isinstance(replies, OrderReplies) and not getattr(replies, "uncertain", False):
                    self._record_missed_signal(mode=context[0][0], symbol=symbol,
                        candle=window[-1].start, observed_at=observed_at,
                        reason="order_not_submitted", notify=notify)
                self._error = next((item["message"] for item in self._symbol_errors.values()), None)
                for reply in replies:
                    try:
                        notify(f"ddcrs · {symbol} · 종료봉 {window[-1].start:%H:%M}\n{reply}")
                    except Exception:
                        pass  # The order marker already committed; never resend.
                if getattr(replies, "uncertain", False):
                    self._pause_uncertain(notify)
            except (CrossMonitorError, MinuteChartError) as error:
                # A bad chart invalidates only this symbol. Keep the rest of the
                # same-minute scan/baselines, and retry on the next normal scan.
                if not keep_recent_missing_baseline(error, self._baseline.get(symbol), self._moment()):
                    self._baseline.pop(symbol, None)
                message = f"ddcrs · {symbol} · {error}"
                previous_error = self._symbol_errors.get(symbol, {}).get("message")
                self._symbol_errors[symbol] = {"message": message,
                    "observed_at": self._moment().isoformat(),
                    "details": deepcopy(getattr(error, "details", {}))}
                if previous_error != message:
                    self._message(notify, message)
                else:
                    self._error = message
                return
            except Exception:
                self._baseline.pop(symbol, None)
                raise
        except Exception as error:
            # Never expose remote exception text or tokens through monitoring.
            self._baseline.clear()
            self._scan.clear()
            self._scan_at = None
            self._enabled = False
            message = str(error) if isinstance(error, (CrossMonitorError, MinuteChartError)) else "ddcrs: 조회·상태·주문 결과를 확인하지 못했습니다. 자동 재전송하지 않으며 주문내역을 확인하세요."
            self._message(notify, message)
