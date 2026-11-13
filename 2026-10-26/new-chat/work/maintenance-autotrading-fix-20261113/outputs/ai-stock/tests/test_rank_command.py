"""Rank menu, validation, numeric presentation, and Telegram chunk contracts."""

import unittest
from unittest.mock import Mock, patch

from kiwoom_autotrader.api.rankings import RankedStock, RankingsError
from kiwoom_autotrader.telegram.commands import rank


class RankCommandTests(unittest.TestCase):
    def test_menu_has_the_requested_number_order_and_needs_no_login(self):
        with patch.object(rank, "fetch_ranked_stocks") as fetch:
            result = rank.handle_rank("  RaNk  ", None)
        fetch.assert_not_called()
        self.assertEqual(result, [rank.MENU])
        for number, name in enumerate(("거래대금", "상승률", "거래량", "인기검색"), 1):
            self.assertIn(f"{number}. {name} 상위 20종목", result[0])
        self.assertIsNone(rank.parse_rank_command("rank"))

    def test_plain_number_queries_are_case_and_whitespace_tolerant(self):
        for number in range(1, 5):
            with self.subTest(number=number):
                self.assertEqual(rank.parse_rank_command(f"  RaNk    {number}  "), number)

    def test_bad_inputs_have_no_query_side_effect(self):
        for text in ("rank 0", "rank 5", "rank -1", "rank 1.0", "rank 01", "rank １", "rank 1 extra",
                     "rank abc", "1", "top", "", None):
            with self.subTest(text=text), patch.object(rank, "fetch_ranked_stocks") as fetch:
                self.assertEqual(rank.handle_rank(text, None), [rank.USAGE])
                fetch.assert_not_called()

    def test_current_session_and_limit_are_passed_through(self):
        session = Mock(mode="real")
        with patch.object(rank, "fetch_ranked_stocks", return_value=[RankedStock("005930", "삼성전자", "+1.23")]) as fetch:
            result = rank.handle_rank("rank 2", session)
        fetch.assert_called_once_with(session, 2, limit=20)
        self.assertIn("실전투자", result[0])
        self.assertIn("+1.23%", result[0])
        self.assertIn("서버에서 1종목", result[0])

    def test_turnover_uses_millions_to_hundred_millions_without_float_rounding(self):
        result = "\n".join(rank.format_ranking([RankedStock("005930", "삼성전자", "5308092", "8")], 1, "paper"))
        self.assertIn("53,080.92억원", result)
        self.assertIn("제공 순위 8위", result)
        self.assertIn("모의투자", result)
        self.assertIn("국내 전체 · 통합거래소 · 관리종목 포함", result)
        self.assertIn("조회 시각(KST)", result)
        large = "\n".join(rank.format_ranking([RankedStock("005930", "삼성전자", "99999999999999999999")], 1, "paper"))
        self.assertIn("999,999,999,999,999,999.99억원", large)

    def test_rate_and_volume_units_and_missing_value(self):
        rate = "\n".join(rank.format_ranking([RankedStock("005930", "삼성전자", "-0.57")], 2, "paper"))
        self.assertIn("-0.57%", rate)
        volume = "\n".join(rank.format_ranking([RankedStock("005930", "삼성전자", "34954641")], 3, "paper"))
        self.assertIn("34,954,641주", volume)
        missing = "\n".join(rank.format_ranking([RankedStock("005930", "", "")], 3, "paper"))
        self.assertIn("종목명 미제공", missing)
        self.assertIn("· 미제공", missing)

    def test_popularity_retains_provider_rank_changes_and_documented_basis(self):
        result = "\n".join(rank.format_ranking([
            RankedStock("005930", "삼성전자", "3", "3", "2", "+", "2026-10-26 09:01:00"),
            RankedStock("000660", "SK하이닉스", "0", "0", "", "N"),
        ], 4, "real"))
        self.assertIn("1분 기준", result)
        self.assertIn("제공 순위 3위", result)
        self.assertIn("순위 변동 +2", result)
        self.assertIn("기준 일시: 2026-10-26 09:01:00", result)
        self.assertIn("제공 순위값 0", result)
        self.assertIn("변동 없음", result)
        self.assertNotIn("통합거래소", result)
        self.assertNotIn("검색 횟수", result)

    def test_empty_query_is_explicit_and_messages_are_bounded_in_utf16(self):
        self.assertIn("조회 결과가 없습니다", "\n".join(rank.format_ranking([], 1, "paper")))
        stocks = [RankedStock(f"{i:06d}", "😀" * 40, "9" * 20, "9" * 20, "9" * 20,
                              "+", "2026-10-26 09:00:00") for i in range(1, 41)]
        chunks = rank.format_ranking(stocks, 4, "paper")
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(text.encode("utf-16-le")) // 2 <= 3900 for text in chunks))
        for i in range(1, 41):
            self.assertEqual(sum(f"({i:06d})" in text for text in chunks), 1)

    def test_safe_errors_are_reported_and_unexpected_details_are_not_echoed(self):
        with patch.object(rank, "fetch_ranked_stocks", side_effect=RankingsError("먼저 login paper 또는 login real로 로그인하세요.")):
            self.assertIn("먼저 login", rank.handle_rank("rank 1", None)[0])
        with patch.object(rank, "fetch_ranked_stocks", side_effect=RuntimeError("private-token")):
            result = rank.handle_rank("rank 1", None)
        self.assertNotIn("private-token", "".join(result))


if __name__ == "__main__":
    unittest.main()
