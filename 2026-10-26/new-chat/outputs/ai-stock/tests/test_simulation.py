"""Behavioral checks for the offline example; no broker credentials or network."""

import json
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from kiwoom_autotrader.simulation import (
    Bar,
    SimulationConfig,
    StrategyConfig,
    simulate,
)


D = Decimal
START = datetime(2026, 1, 5, 0, 0, tzinfo=timezone.utc)


def example_bars(market="US", symbol="DEMO_US", exchange="NASDAQ"):
    """An up crossing at bar 3 and a down crossing at bar 5, counted from zero."""
    closes = [3, 3, 3, 6, 9, 2, 1]
    opens = [3, 3, 3, 6, 10, 2, 4]
    return [
        Bar(
            market=market,
            exchange=exchange,
            symbol=symbol,
            timestamp=START + timedelta(minutes=index),
            open=D(str(open_price)),
            close=D(str(close)),
            volume=200 if index == 3 else 100,
        )
        for index, (open_price, close) in enumerate(zip(opens, closes))
    ]


def config(**changes):
    value = SimulationConfig(
        strategy=StrategyConfig(
            fast_window=2,
            slow_window=3,
            volume_window=3,
            volume_ratio=D("1.2"),
            quantity=1,
        ),
        initial_cash={"KRW": D("1000"), "USD": D("100")},
        max_order_notional={"KRW": D("1000"), "USD": D("100")},
        max_position_notional={"KRW": D("1000"), "USD": D("100")},
        fee_bps=D("0"),
        slippage_bps=D("0"),
    )
    return replace(value, **changes)


class SimulationBehaviorTests(unittest.TestCase):
    def test_signals_fill_at_next_open_and_exit_entire_position(self):
        bars = example_bars()
        result = simulate(bars, config())

        self.assertEqual([event["side"] for event in result["fills"]], ["BUY", "SELL"])
        self.assertEqual([D(event["price"]) for event in result["fills"]], [D("10"), D("4")])
        self.assertEqual(
            datetime.fromisoformat(result["fills"][0]["timestamp"].replace("Z", "+00:00")),
            bars[4].timestamp,
        )
        self.assertEqual(
            datetime.fromisoformat(result["signals"][0]["timestamp"].replace("Z", "+00:00")),
            bars[3].timestamp,
        )
        self.assertEqual(D(result["cash"]["USD"]), D("94"))
        self.assertEqual(result["positions"], [])
        self.assertEqual(result["pending_orders"], [])

    def test_final_bar_signal_stays_pending_without_fabricated_fill(self):
        result = simulate(example_bars()[:4], config())
        self.assertEqual(result["fills"], [])
        self.assertEqual(len(result["signals"]), 1)
        self.assertEqual(len(result["pending_orders"]), 1)
        self.assertEqual(result["pending_orders"][0]["side"], "BUY")
        self.assertEqual(D(result["cash"]["USD"]), D("100"))

    def test_volume_filter_uses_previous_bars_and_can_block_entry(self):
        bars = [replace(bar, volume=100) for bar in example_bars()]
        result = simulate(bars, config())
        self.assertEqual(result["fills"], [])
        self.assertEqual(result["positions"], [])

    def test_zero_volume_cross_does_not_create_buy_signal(self):
        bars = [replace(bar, volume=0) for bar in example_bars()]
        result = simulate(bars, config())
        self.assertEqual(result["signals"], [])
        self.assertEqual(result["fills"], [])
        self.assertEqual(result["pending_orders"], [])

    def test_positive_volume_after_zero_baseline_is_not_a_confirmed_entry(self):
        bars = [replace(bar, volume=0) for bar in example_bars()]
        bars[3] = replace(bars[3], volume=200)
        result = simulate(bars, config())
        self.assertEqual(result["signals"], [])
        self.assertEqual(result["fills"], [])
        self.assertEqual(result["positions"], [])

    def test_currency_balances_are_independent(self):
        usd = example_bars()[:5]
        krw = example_bars("KR", "DEMO_KR", "KRX")[:5]
        bars = sorted(usd + krw, key=lambda bar: bar.timestamp)
        settings = config(initial_cash={"KRW": D("1000"), "USD": D("9")})

        result = simulate(bars, settings)
        self.assertEqual(len(result["fills"]), 1)
        self.assertEqual(result["fills"][0]["market"], "KR")
        self.assertEqual(len(result["rejections"]), 1)
        self.assertEqual(result["rejections"][0]["market"], "US")
        self.assertEqual(D(result["cash"]["USD"]), D("9"))
        self.assertEqual(D(result["cash"]["KRW"]), D("990"))

    def test_slippage_and_fees_reduce_cash_and_equity(self):
        settings = config(
            strategy=replace(config().strategy, quantity=2),
            fee_bps=D("50"),
            slippage_bps=D("100"),
        )
        result = simulate(example_bars()[:5], settings)
        fill = result["fills"][0]
        self.assertEqual(D(fill["price"]), D("10.10"))
        self.assertEqual(D(fill["notional"]), D("20.20"))
        self.assertEqual(D(fill["fee"]), D("0.101"))
        self.assertEqual(D(result["cash"]["USD"]), D("79.699"))
        self.assertEqual(D(result["equity"]["USD"]), D("97.699"))

    def test_price_gap_checks_order_limit_at_fill(self):
        result = simulate(
            example_bars()[:5],
            config(max_order_notional={"KRW": D("1000"), "USD": D("8")}),
        )
        self.assertEqual(result["fills"], [])
        self.assertEqual(len(result["rejections"]), 1)
        self.assertEqual(result["positions"], [])
        self.assertEqual(D(result["cash"]["USD"]), D("100"))

    def test_position_limit_checks_quantity_at_fill(self):
        result = simulate(
            example_bars()[:5],
            config(
                strategy=replace(config().strategy, quantity=2),
                max_position_notional={"KRW": D("1000"), "USD": D("19")},
            ),
        )
        self.assertEqual(result["fills"], [])
        self.assertEqual(len(result["rejections"]), 1)

    def test_cash_check_includes_fee(self):
        result = simulate(
            example_bars()[:5],
            config(initial_cash={"KRW": D("1000"), "USD": D("10")}, fee_bps=D("100")),
        )
        self.assertEqual(result["fills"], [])
        self.assertEqual(len(result["rejections"]), 1)
        self.assertEqual(D(result["cash"]["USD"]), D("10"))

    def test_order_cap_does_not_prevent_liquidation(self):
        bars = example_bars()
        bars[6] = replace(bars[6], open=D("100"))
        result = simulate(bars, config(max_order_notional={"KRW": D("1000"), "USD": D("11")}))
        self.assertEqual([event["side"] for event in result["fills"]], ["BUY", "SELL"])
        self.assertEqual(D(result["fills"][1]["notional"]), D("100"))
        self.assertEqual(result["positions"], [])

    def test_bearish_cross_while_flat_does_not_open_short(self):
        bars = example_bars()[:5]
        bars = [replace(bar, close=D(str(close))) for bar, close in zip(bars, [6, 6, 6, 3, 2])]
        result = simulate(bars, config())
        self.assertEqual(result["fills"], [])
        self.assertEqual(result["positions"], [])
        self.assertEqual(D(result["cash"]["USD"]), D("100"))

    def test_identical_duplicate_does_not_repeat_signal_or_fill(self):
        bars = example_bars()
        result = simulate(bars[:4] + [bars[3]] + bars[4:], config())
        self.assertEqual(result["duplicates_skipped"], 1)
        self.assertEqual(result["bars_processed"], 7)
        self.assertEqual(len(result["fills"]), 2)
        self.assertEqual(D(result["cash"]["USD"]), D("94"))

    def test_conflicting_duplicate_is_rejected(self):
        bars = example_bars()
        with self.assertRaises((ValueError, TypeError)):
            simulate(bars[:4] + [replace(bars[3], close=D("7"))] + bars[4:], config())

    def test_out_of_order_input_is_rejected(self):
        bars = example_bars()
        with self.assertRaises((ValueError, TypeError)):
            simulate([bars[1], bars[0]], config())

    def test_nonfinite_or_nonpositive_prices_are_rejected(self):
        for invalid in [D("NaN"), D("Infinity"), D("-Infinity"), D("0"), D("-1")]:
            for field in ["open", "close"]:
                with self.subTest(value=invalid, field=field):
                    with self.assertRaises((ValueError, TypeError)):
                        simulate([replace(example_bars()[0], **{field: invalid})], config())

    def test_naive_timestamp_is_rejected(self):
        with self.assertRaises((ValueError, TypeError)):
            simulate([replace(example_bars()[0], timestamp=START.replace(tzinfo=None))], config())

    def test_negative_volume_is_rejected(self):
        with self.assertRaises((ValueError, TypeError)):
            simulate([replace(example_bars()[0], volume=-1)], config())

    def test_invalid_strategy_is_rejected(self):
        for changes in [{"fast_window": 0}, {"fast_window": 3}, {"quantity": 0}, {"volume_ratio": D("NaN")}]:
            with self.subTest(changes=changes):
                with self.assertRaises((ValueError, TypeError)):
                    simulate(example_bars(), config(strategy=replace(config().strategy, **changes)))

    def test_market_exchange_mismatch_is_rejected(self):
        with self.assertRaises((ValueError, TypeError)):
            simulate([replace(example_bars()[0], market="KR", exchange="NASDAQ")], config())

    def test_result_is_serializable_and_contains_no_live_mode(self):
        result = simulate(example_bars(), config())
        self.assertEqual(result["mode"], "offline_simulation")
        json.dumps(result, allow_nan=False)

    def test_empty_input_preserves_initial_balances(self):
        result = simulate([], config())
        self.assertEqual(result["bars_processed"], 0)
        self.assertEqual(result["fills"], [])
        self.assertEqual(D(result["equity"]["USD"]), D("100"))
        self.assertEqual(D(result["equity"]["KRW"]), D("1000"))


if __name__ == "__main__":
    unittest.main()
