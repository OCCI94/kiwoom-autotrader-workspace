"""Offline, long-only example strategy; this module never contacts a broker.

Bars represent completed candles. A signal uses that candle's close and volume;
its order can fill only at the *next* candle's open for the same instrument.
Event timestamps identify their candle, not an exchange-confirmed fill time.
Fees and slippage are simplified basis-point estimates, without tick rounding,
taxes, partial fills, liquidity, FX conversion, or corporate-action adjustments.
Results are for testing program behavior, not an investment recommendation.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Iterable


_CURRENCY = {"KR": "KRW", "US": "USD"}
_EXCHANGES = {"KR": {"KRX"}, "US": {"NASDAQ", "NYSE", "AMEX"}}
_ZERO = Decimal("0")
_BPS = Decimal("10000")
Instrument = tuple[str, str, str]


def _decimal(name: str, value: Decimal, *, positive: bool = False) -> None:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError(f"{name} must be a finite Decimal")
    if value < 0 or (positive and value == 0):
        raise ValueError(f"{name} must be {'positive' if positive else 'nonnegative'}")


def _integer(name: str, value: int, *, minimum: int = 1) -> None:
    if type(value) is not int or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")


@dataclass(frozen=True)
class Bar:
    market: str
    exchange: str
    symbol: str
    timestamp: datetime
    open: Decimal
    close: Decimal
    volume: int

    @property
    def currency(self) -> str:
        return _CURRENCY[self.market]

    @property
    def instrument(self) -> Instrument:
        return self.market, self.exchange, self.symbol

    def validate(self) -> None:
        if not isinstance(self.market, str) or self.market not in _CURRENCY:
            raise ValueError("market must be KR or US")
        if not isinstance(self.exchange, str) or self.exchange not in _EXCHANGES[self.market]:
            raise ValueError("unsupported exchange for market")
        if not isinstance(self.symbol, str) or not self.symbol.strip():
            raise ValueError("symbol must be a nonempty string")
        if self.symbol != self.symbol.strip():
            raise ValueError("symbol must not contain leading or trailing whitespace")
        if not isinstance(self.timestamp, datetime) or self.timestamp.utcoffset() is None:
            raise ValueError("timestamp must be a timezone-aware datetime")
        _decimal("open", self.open, positive=True)
        _decimal("close", self.close, positive=True)
        _integer("volume", self.volume, minimum=0)


@dataclass(frozen=True)
class StrategyConfig:
    fast_window: int = 3
    slow_window: int = 5
    volume_window: int = 3
    volume_ratio: Decimal = Decimal("1.2")
    quantity: int = 1

    def validate(self) -> None:
        _integer("fast_window", self.fast_window)
        _integer("slow_window", self.slow_window)
        _integer("volume_window", self.volume_window)
        _integer("quantity", self.quantity)
        if self.fast_window >= self.slow_window:
            raise ValueError("fast_window must be smaller than slow_window")
        _decimal("volume_ratio", self.volume_ratio, positive=True)


@dataclass(frozen=True)
class SimulationConfig:
    strategy: StrategyConfig
    initial_cash: dict[str, Decimal]
    max_order_notional: dict[str, Decimal]
    max_position_notional: dict[str, Decimal]
    fee_bps: Decimal = Decimal("5")
    slippage_bps: Decimal = Decimal("10")

    def validate(self) -> None:
        if not isinstance(self.strategy, StrategyConfig):
            raise ValueError("strategy must be a StrategyConfig")
        self.strategy.validate()
        maps = (self.initial_cash, self.max_order_notional, self.max_position_notional)
        if any(not isinstance(mapping, dict) for mapping in maps):
            raise ValueError("cash and limit settings must be currency dictionaries")
        currencies = set(self.initial_cash)
        if not currencies or not currencies <= {"KRW", "USD"}:
            raise ValueError("cash currencies must be KRW and/or USD")
        if any(set(mapping) != currencies for mapping in maps):
            raise ValueError("cash and limit settings must have the same currencies")
        for currency in currencies:
            _decimal(f"initial_cash.{currency}", self.initial_cash[currency])
            _decimal(f"max_order_notional.{currency}", self.max_order_notional[currency], positive=True)
            _decimal(f"max_position_notional.{currency}", self.max_position_notional[currency], positive=True)
        for name, value in (("fee_bps", self.fee_bps), ("slippage_bps", self.slippage_bps)):
            _decimal(name, value)
            if value >= _BPS:
                raise ValueError(f"{name} must be smaller than 10000")


def _event(bar: Bar, side: str, quantity: int) -> dict:
    return {
        "market": bar.market,
        "exchange": bar.exchange,
        "symbol": bar.symbol,
        "timestamp": bar.timestamp.isoformat(),
        "side": side,
        "quantity": quantity,
        "currency": bar.currency,
    }


def _sma(bars: list[Bar], window: int, *, previous: bool = False) -> Decimal:
    end = len(bars) - int(previous)
    return sum((bar.close for bar in bars[end - window:end]), _ZERO) / window


def simulate(bars: Iterable[Bar], config: SimulationConfig) -> dict:
    """Run one independent simulation and return JSON-compatible records.

    New candles must be globally chronological; input order resolves competing
    orders sharing a timestamp. Identical duplicate candles are ignored even
    if replayed later, while conflicting duplicates raise ValueError. Duplicate
    suppression is scoped to this invocation; it is not durable order recovery.

    Buy limits apply at the simulated execution price, not the signal's close.
    Sells may exceed those limits so an exposure cap never prevents liquidation.
    An execution gap can exceed a position limit after entry; this example does
    not automatically rebalance. Currency balances are kept separate.
    """
    if not isinstance(config, SimulationConfig):
        raise ValueError("config must be a SimulationConfig")
    config.validate()
    strategy = config.strategy
    cash = dict(config.initial_cash)
    quantities: dict[Instrument, int] = {}
    latest: dict[Instrument, Bar] = {}
    histories: dict[Instrument, deque[Bar]] = {}
    seen: dict[tuple[Instrument, datetime], Bar] = {}
    pending: dict[Instrument, dict] = {}
    signals: list[dict] = []
    fills: list[dict] = []
    rejections: list[dict] = []
    processed = duplicates = 0
    previous_time: datetime | None = None
    history_length = max(strategy.slow_window + 1, strategy.volume_window + 1)
    fee_rate = config.fee_bps / _BPS
    slippage_rate = config.slippage_bps / _BPS

    for bar in bars:
        if not isinstance(bar, Bar):
            raise ValueError("every input candle must be a Bar")
        bar.validate()
        key, currency = bar.instrument, bar.currency
        if currency not in cash:
            raise ValueError(f"missing cash and limits for {currency}")
        observation = (key, bar.timestamp)
        if observation in seen:
            if seen[observation] != bar:
                raise ValueError("conflicting duplicate candle")
            duplicates += 1
            continue
        if previous_time is not None and bar.timestamp < previous_time:
            raise ValueError("new candles must be globally chronological")
        previous_time = bar.timestamp
        seen[observation] = bar
        processed += 1

        # Consume only an order created by a previous candle of this instrument.
        order = pending.pop(key, None)
        if order is not None:
            side, quantity = order["side"], order["quantity"]
            price = bar.open * (1 + slippage_rate if side == "BUY" else 1 - slippage_rate)
            notional = price * quantity
            fee = notional * fee_rate
            event = {
                **_event(bar, side, quantity),
                "signal_timestamp": order["timestamp"],
                "price": str(price),
                "notional": str(notional),
                "fee": str(fee),
                "execution": "next_bar_open",
            }
            rejection = None
            if side == "BUY":
                if notional > config.max_order_notional[currency]:
                    rejection = "max_order_notional"
                elif price * (quantities.get(key, 0) + quantity) > config.max_position_notional[currency]:
                    rejection = "max_position_notional"
                elif notional + fee > cash[currency]:
                    rejection = "insufficient_cash"
                else:
                    cash[currency] -= notional + fee
                    quantities[key] = quantities.get(key, 0) + quantity
            else:
                # Positions can change only through this engine; never short.
                if quantity > quantities.get(key, 0):
                    rejection = "insufficient_position"
                else:
                    cash[currency] += notional - fee
                    quantities[key] -= quantity
            if rejection is None:
                fills.append(event)
            else:
                rejections.append({**event, "reason": rejection})

        latest[key] = bar
        history = histories.setdefault(key, deque(maxlen=history_length))
        history.append(bar)
        if len(history) < strategy.slow_window + 1:
            continue
        candles = list(history)
        previous_fast = _sma(candles, strategy.fast_window, previous=True)
        previous_slow = _sma(candles, strategy.slow_window, previous=True)
        current_fast = _sma(candles, strategy.fast_window)
        current_slow = _sma(candles, strategy.slow_window)
        position = quantities.get(key, 0)
        side = reason = None
        quantity = 0
        if position and previous_fast >= previous_slow and current_fast < current_slow:
            side, quantity, reason = "SELL", position, "bearish_sma_cross"
        elif position == 0 and previous_fast <= previous_slow and current_fast > current_slow:
            if len(candles) >= strategy.volume_window + 1:
                baseline = sum(Decimal(c.volume) for c in candles[-strategy.volume_window - 1:-1]) / strategy.volume_window
                if baseline > 0 and bar.volume > 0 and Decimal(bar.volume) >= baseline * strategy.volume_ratio:
                    side, quantity, reason = "BUY", strategy.quantity, "bullish_sma_cross_with_volume"
        if side is not None:
            signal = {**_event(bar, side, quantity), "reason": reason}
            signals.append(signal)
            pending[key] = signal

    equity = dict(cash)
    positions = []
    for key in sorted(quantities):
        quantity = quantities[key]
        if quantity == 0:
            continue
        bar = latest[key]
        market_value = bar.close * quantity
        equity[bar.currency] += market_value
        positions.append({
            "market": bar.market,
            "exchange": bar.exchange,
            "symbol": bar.symbol,
            "quantity": quantity,
            "last_price": str(bar.close),
            "currency": bar.currency,
            "market_value": str(market_value),
        })
    return {
        "mode": "offline_simulation",
        "bars_processed": processed,
        "duplicates_skipped": duplicates,
        "signals": signals,
        "fills": fills,
        "rejections": rejections,
        "pending_orders": [dict(pending[key]) for key in sorted(pending)],
        "cash": {currency: str(amount) for currency, amount in cash.items()},
        "positions": positions,
        "equity": {currency: str(amount) for currency, amount in equity.items()},
    }
