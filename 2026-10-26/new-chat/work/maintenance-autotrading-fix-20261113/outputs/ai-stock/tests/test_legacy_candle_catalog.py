import unittest

from kiwoom_autotrader.trading.legacy_candle_catalog import (
    DAILY_TREND, PATTERNS, SOURCE_COMPARISON, SOURCES, evaluate_pattern, list_patterns,
)


class LegacyCandleCatalogTests(unittest.TestCase):
    def test_eight_unique_identifiers_have_specific_source_and_unresolved_calculation(self):
        expected = {"lower_wick_rebound", "consolidation_breakout", "decline_rebound", "steady_uptrend",
                    "first_pullback", "decline_koma_reversal", "ascending_pennant", "morning_selloff_doji"}
        patterns = list_patterns()
        self.assertEqual({row.identifier for row in patterns}, expected)
        self.assertEqual(len(patterns), 8)
        for row in patterns:
            self.assertTrue(row.criteria)
            self.assertTrue(row.ambiguities)
            self.assertTrue(row.source_lines)
            self.assertTrue(all(c.description and c.relation and c.value and c.unit and c.reference for c in row.criteria))

    def test_daily_and_time_restrictions_are_not_extended_to_other_patterns(self):
        rows = {row.identifier: row for row in PATTERNS}
        self.assertEqual({name for name, row in rows.items() if row.daily_requirements},
                         {"steady_uptrend", "first_pullback", "morning_selloff_doji"})
        self.assertEqual(rows["morning_selloff_doji"].daily_requirements, DAILY_TREND)
        self.assertEqual(rows["morning_selloff_doji"].session_window, "10:00~12:30 KST")
        self.assertIsNone(rows["first_pullback"].session_window)

    def test_source_versions_have_verified_text_hashes_and_no_binary_claim(self):
        self.assertEqual(len(SOURCES), 2)
        self.assertTrue(all(len(item["sha256"]) == 64 and item["url"].startswith("https://drive.google.com/") for item in SOURCES))
        self.assertEqual(SOURCE_COMPARISON["primary"], "AUTOTRADING-ONLY-PLAN-A.md")
        self.assertEqual(SOURCE_COMPARISON["pattern_paragraphs_74_170"], "text_identical")
        self.assertFalse(SOURCE_COMPARISON["original_binary_verified"])

    def test_no_input_or_threshold_can_generate_a_trade_signal(self):
        for row in PATTERNS:
            for data in (None, {}, {"ohlcv": [1]*100, "force": True}):
                result = evaluate_pattern(row.identifier, data=data)
                self.assertEqual(result["status"], "UNKNOWN")
                self.assertEqual(result["reason"], "original_calculation_unverified")
                self.assertFalse(result["input_evaluated"])
                self.assertFalse(result["eligible_for_order"])
        with self.assertRaises(ValueError):
            evaluate_pattern("not_a_legacy_pattern")

    def test_explicit_pattern_boundaries_remain_distinct(self):
        rows = {row.identifier: row for row in PATTERNS}
        def values(name): return {(c.description, c.relation, c.value, c.unit) for c in rows[name].criteria}
        self.assertIn(("시작 갭 제외", ">", "0.2", "%"), values("consolidation_breakout"))
        self.assertIn(("추격 제외", ">", "1.5", "%"), values("ascending_pennant"))
        self.assertIn(("현재 저점의 하락", ">=", "1", "%"), values("lower_wick_rebound"))
        self.assertIn(("상승 구간 되돌림", "<=", "38", "%"), values("first_pullback"))
        self.assertTrue(any("접촉" in a for a in rows["ascending_pennant"].ambiguities))


if __name__ == "__main__":
    unittest.main()
