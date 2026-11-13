"""Pure research validation; synthetic data only, no external services."""
from copy import deepcopy
from datetime import datetime, timedelta
from decimal import Decimal
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from kiwoom_autotrader.research.session_quality import assess_session_quality, assess_strategy_claims


KST = ZoneInfo("Asia/Seoul")


class SessionQualityTests(unittest.TestCase):
    def setUp(self):
        self.start = datetime(2026, 10, 27, 9, tzinfo=KST)
        self.end = self.start + timedelta(minutes=10)
        self.observed = self.end
        network = patch("socket.socket.connect", side_effect=AssertionError("network forbidden"))
        network.start()
        self.addCleanup(network.stop)

    def rows(self, count=10, offset=0, *, notional=False):
        result = []
        for index in range(offset, offset + count):
            row = {"timestamp": (self.start + timedelta(minutes=index)).strftime("%Y%m%d%H%M%S"),
                   "open": 100, "high": 110 + index, "low": 90 - index, "close": 105,
                   "volume": 10, "finality": "unverified", "valid_ohlcv": True}
            if notional:
                row["traded_notional_krw"] = "1030"
            result.append(row)
        return result

    def assess(self, rows, **kwargs):
        return assess_session_quality(rows, session_open=self.start, session_close=self.end,
            observed_at=self.observed, **kwargs)

    def test_complete_clock_coverage_is_not_verified_market_or_strategy(self):
        result = self.assess(self.rows())
        self.assertTrue(result["full_session_complete"])
        self.assertEqual(result["missing_count"], 0)
        self.assertEqual(result["opening_range"]["calculation_status"], "PASS")
        self.assertEqual((result["opening_range"]["high"], result["opening_range"]["low"]), (114, 86))
        self.assertFalse(result["opening_range"]["signal_usable"])
        self.assertIn("SOURCE_BAR_FINALITY_UNVERIFIED", result["flags"])
        self.assertFalse(result["profitability_verified"])
        self.assertFalse(result["automatic_orders"])

    def test_oldest_partial_session_is_not_rebased_to_first_observed_bar(self):
        result = self.assess(self.rows(count=7, offset=3))
        self.assertFalse(result["first_observed_at_session_open"])
        self.assertIn("SESSION_OPEN_PREFIX_MISSING", result["flags"])
        self.assertEqual(result["missing_count"], 3)
        self.assertIsNone(result["opening_range"]["high"])
        self.assertFalse(result["full_session_complete"])
        self.assertEqual(result["missing_minute_cause"], "UNKNOWN")

    def test_internal_gap_never_gets_zero_volume_or_forward_filled(self):
        rows = self.rows(notional=True)
        del rows[2]
        before = deepcopy(rows)
        result = self.assess(rows)
        self.assertEqual(rows, before)
        self.assertEqual(result["missing_minutes"], [(self.start + timedelta(minutes=2)).isoformat()])
        self.assertEqual(result["interpolated_minutes"], 0)
        self.assertEqual(result["explicit_zero_volume_rows"], 0)
        self.assertEqual(result["opening_range"]["calculation_status"], "BLOCKED")
        self.assertIsNone(result["vwap"]["value_krw"])

    def test_later_gap_does_not_destroy_observed_opening_range(self):
        rows = self.rows(notional=True)
        del rows[8]
        result = self.assess(rows, calendar_verified=True, source_finality_verified=True)
        self.assertEqual(result["opening_range"]["calculation_status"], "PASS")
        self.assertTrue(result["opening_range"]["signal_usable"])
        self.assertIsNone(result["vwap"]["value_krw"])

    def test_clock_elapsed_does_not_make_an_incomplete_opening_range_valid(self):
        self.observed = self.start + timedelta(minutes=4, seconds=59)
        result = self.assess(self.rows(count=5))
        self.assertEqual(result["unfinished_rows"], 1)
        self.assertEqual(result["elapsed_expected_minutes"], 4)
        self.assertTrue(result["session_prefix_complete"])
        self.assertFalse(result["full_session_complete"])
        self.assertIn("OPENING_RANGE_NOT_ELAPSED", result["opening_range"]["reason_codes"])

    def test_true_notional_vwap_is_distinct_from_close_volume_proxy(self):
        result = self.assess(self.rows(notional=True), calendar_verified=True, source_finality_verified=True)
        self.assertEqual(result["vwap"]["value_krw"], "103")
        self.assertTrue(result["vwap"]["signal_usable"])
        self.assertNotEqual(result["vwap"]["value_krw"], "105")
        without = self.assess(self.rows())
        self.assertEqual(without["vwap"]["calculation_status"], "UNKNOWN")
        self.assertIn("EXACT_EXECUTED_NOTIONAL_UNAVAILABLE", without["vwap"]["reason_codes"])

    def test_bad_or_rounded_unit_notional_is_not_silently_used(self):
        for notional in (True, 1.03, "1e3", "NaN", "-1", "1.03", "999999999999999999999999999999999"):
            with self.subTest(notional=notional):
                rows = self.rows(notional=True)
                rows[1]["traded_notional_krw"] = notional
                result = self.assess(rows)
                self.assertIsNone(result["vwap"]["value_krw"])
                self.assertIn("INVALID_EXECUTED_NOTIONAL", result["flags"])

    def test_reported_zero_volume_is_kept_but_missing_volume_is_invalid(self):
        rows = self.rows(notional=True)
        rows[2].update(volume=0, traded_notional_krw="0")
        result = self.assess(rows)
        self.assertEqual(result["explicit_zero_volume_rows"], 1)
        self.assertEqual(result["missing_count"], 0)
        self.assertEqual(result["vwap"]["value_krw"], "103")
        rows[2]["volume"] = None
        result = self.assess(rows)
        self.assertEqual(result["invalid_rows"], 1)
        self.assertEqual(result["missing_count"], 1)

    def test_duplicate_and_conflicting_revisions_do_not_gain_validity(self):
        rows = self.rows()
        rows.append(deepcopy(rows[2]))
        self.assertEqual(self.assess(rows)["duplicate_rows"], 1)
        self.assertFalse(self.assess(rows)["session_prefix_complete"])
        rows[-1]["close"] = 104
        result = self.assess(rows)
        self.assertEqual(result["conflicting_rows"], 1)
        self.assertEqual(result["missing_count"], 1)

    def test_descending_source_order_and_other_dates_preserve_boundaries(self):
        rows = self.rows()
        previous = deepcopy(rows[0])
        previous["timestamp"] = "20261026151000"
        rows.insert(0, previous)
        result = self.assess(list(reversed(rows)), pagination_complete=False)
        self.assertTrue(result["session_prefix_complete"])
        self.assertTrue(result["first_observed_at_session_open"])
        self.assertEqual(result["outside_session_rows"], 1)
        self.assertIn("SOURCE_PAGINATION_NOT_COMPLETE_OR_UNKNOWN", result["flags"])

    def test_source_invalid_flag_and_bad_ohlc_are_not_ignored(self):
        for update in ({"valid_ohlcv": False}, {"open": 0}, {"high": 80}, {"volume": True}, {"timestamp": "not-a-date"}):
            with self.subTest(update=update):
                rows = self.rows()
                rows[1].update(update)
                result = self.assess(rows)
                self.assertEqual(result["invalid_rows"], 1)
                self.assertFalse(result["full_session_complete"])

    def test_empty_and_before_open_remain_unknown(self):
        self.observed = self.start - timedelta(minutes=1)
        result = self.assess([])
        self.assertIsNone(result["first_observed_at_session_open"])
        self.assertFalse(result["full_session_complete"])
        self.assertFalse(result["session_prefix_complete"])
        self.assertIsNone(result["vwap"]["value_krw"])

    def test_explicit_boundaries_and_bounded_input_are_required(self):
        for kwargs in ({"session_open": self.start.replace(tzinfo=None)},
                       {"session_close": self.start}, {"opening_range_minutes": True},
                       {"opening_range_minutes": 15}, {"calendar_verified": 1}):
            with self.subTest(kwargs=kwargs):
                arguments = {"session_open": self.start, "session_close": self.end, "observed_at": self.end}
                arguments.update(kwargs)
                with self.assertRaises(ValueError):
                    assess_session_quality([], **arguments)
        with self.assertRaises(ValueError):
            self.assess([{}] * 20001)


class StrategyClaimTests(unittest.TestCase):
    def setUp(self):
        self.decision = datetime(2026, 10, 26, 15, 20, tzinfo=KST)

    def test_final_daily_inputs_cannot_support_earlier_entry_decision(self):
        result = assess_strategy_claims(decision_at=self.decision,
            features_available_at=self.decision + timedelta(minutes=10), assumed_entry_at=self.decision)
        self.assertIn("FEATURES_NOT_AVAILABLE_AT_DECISION", result["blocking_reason_codes"])
        self.assertIn("SAME_OR_EARLIER_TIME_ASSUMED_FILL_UNVERIFIED", result["blocking_reason_codes"])
        self.assertEqual(result["status"], "BLOCKED")

    def test_future_selected_universe_and_percent_sum_do_not_make_oos_portfolio(self):
        result = assess_strategy_claims(universe_frozen_at=self.decision,
            evaluation_start=datetime(2026, 1, 1, tzinfo=KST),
            return_measure="trade_return_sum_percentage_points", presented_as_portfolio=True)
        self.assertIn("RETROSPECTIVE_UNIVERSE_SELECTION", result["blocking_reason_codes"])
        self.assertIn("RETURN_MEASURE_IS_NOT_PORTFOLIO_NAV", result["blocking_reason_codes"])

    def test_missing_code_trade_ledger_and_availability_stay_unverified(self):
        result = assess_strategy_claims()
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertIn("TRADE_LEVEL_REPRODUCTION_UNAVAILABLE", result["unverified_reason_codes"])
        self.assertIn("FEATURE_AVAILABILITY_UNVERIFIED", result["unverified_reason_codes"])

    def test_clean_timing_is_never_an_automatic_strategy_approval(self):
        result = assess_strategy_claims(decision_at=self.decision, features_available_at=self.decision,
            assumed_entry_at=self.decision + timedelta(minutes=1), universe_frozen_at=self.decision - timedelta(days=2),
            evaluation_start=self.decision - timedelta(days=1), return_measure="portfolio_nav_return",
            presented_as_portfolio=True, source_code_available=True, trade_ledger_available=True)
        self.assertEqual(result["blocking_reason_codes"], [])
        self.assertEqual(result["validation_state"], "NOT_VALIDATED_FOR_TRADING")
        self.assertFalse(result["performance_verified"])
        self.assertFalse(result["automatic_orders"])

    def test_ambiguous_units_and_naive_times_are_rejected(self):
        with self.assertRaises(ValueError):
            assess_strategy_claims(return_measure="percent")
        with self.assertRaises(ValueError):
            assess_strategy_claims(decision_at=self.decision.replace(tzinfo=None))


if __name__ == "__main__":
    unittest.main()
