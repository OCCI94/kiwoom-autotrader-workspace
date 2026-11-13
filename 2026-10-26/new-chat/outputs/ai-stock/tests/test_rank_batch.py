"""Offline rank-template validation, one-snapshot expansion and dispatch."""

from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import ANY, Mock, call, patch

from kiwoom_autotrader.api.rankings import RankedStock
from kiwoom_autotrader.api.session import KST
from kiwoom_autotrader.config import settings
from kiwoom_autotrader.telegram import listener
from kiwoom_autotrader.telegram.bot_credentials import TelegramConfig
from kiwoom_autotrader.telegram.commands import rank, rank_batch
from kiwoom_autotrader.telegram.commands.orders import OrderReplies
from kiwoom_autotrader.utils.command_scheduler import CommandScheduler, SchedulerError, validate_command


def stock(code="005930"):
    return RankedStock(code, f"종목 {code}", "1")


class RankBatchTests(unittest.TestCase):
    def setUp(self):
        self.session = SimpleNamespace(mode="paper", token="offline-batch-session-token")
        self.dispatch = Mock(return_value=["명령 처리 완료"])
        patcher = patch.object(rank_batch, "fetch_ranked_stocks", return_value=[stock(), stock("000660")])
        self.addCleanup(patcher.stop)
        self.fetch = patcher.start()

    def handle(self, text="rank 4 cmd 2 gdcrs add ( ) 100000", **options):
        return rank_batch.handle_rank_batch(text, self.session, self.dispatch, **options)

    def test_allowed_templates_put_the_code_in_the_identifier_position(self):
        cases = {
            "srch ()": "srch 000660",
            "buy ( ) 3": "buy 000660 3",
            'buy "( )" 3 70000': 'buy "000660" 3 70000',
            "buy () max 100000": "buy 000660 max 100000",
            "sell () 5": "sell 000660 5",
            "gdcrs add ( ) 100000": "gdcrs add 000660 100000",
        }
        for template, expected in cases.items():
            with self.subTest(template=template):
                parsed = rank_batch.parse_rank_batch("  RaNk  4  CmD  15 " + template + "  ")
                self.assertEqual((parsed.number, parsed.count), (4, 15))
                self.assertEqual(parsed.command("000660"), expected)

    def test_all_menu_numbers_and_count_boundaries_are_accepted(self):
        for number in range(1, 5):
            for count in (1, 20):
                with self.subTest(number=number, count=count):
                    parsed = rank_batch.parse_rank_batch(f"rank {number} cmd {count} srch ()")
                    self.assertEqual((parsed.number, parsed.count), (number, count))

    def test_invalid_grammar_and_templates_fail_before_any_effect(self):
        invalid = [
            "rank 0 cmd 1 srch ()", "rank 5 cmd 1 srch ()", "rank 01 cmd 1 srch ()",
            "rank 1 cmd 0 srch ()", "rank 1 cmd 21 srch ()", "rank 1 cmd -1 srch ()",
            "rank 1 cmd 1.0 srch ()", "rank 1 cmd １ srch ()", "rank 1 cmd 100 srch ()",
            "rank 1 cmd 1", "rank 1 cmd 1 srch", "rank 1 cmd 1 srch 005930",
            "rank 1 cmd 1 buy () ()", "rank 1 cmd 1 buy (()) 1", "rank 1 cmd 1 ()",
            "rank 1 cmd 1 srch () extra", "rank 1 cmd 1 srch ()\nlogin real",
            "rank 1 cmd 1 buy () 1; power off", "rank 1 cmd 1 buy () 1 && login real",
            "rank 1 cmd 1 buy () $(date)", "rank 1 cmd 1 buy () `date`",
            "rank 1 cmd 1 rank 4 cmd 1 srch ()", "rank 1 cmd 1 rsv 9:00 srch ()",
            "rank 1 cmd 1 login ()", "rank 1 cmd 1 gdcrs remove ()", "rank 1 cmd 1 gdcrs clear ()",
            "rank 1 cmd 1 gdcrs intv () 20", "rank 1 cmd 1 start ()", "rank 1 cmd 1 stts ()",
            "rank 1 cmd 1 buy () 1 " + "x" * 500,
        ]
        for text in invalid:
            with self.subTest(text=text):
                self.assertEqual(self.handle(text), [rank_batch.BATCH_USAGE])
        self.fetch.assert_not_called()
        self.dispatch.assert_not_called()

    def test_placeholder_cannot_be_quantity_or_budget_even_when_fixed_code_matches_fixture(self):
        # A single dummy substitution of 005930 cannot distinguish a constant
        # 005930 identifier from the actual placeholder's argument position.
        for template in ("buy 005930 ()", "buy 005930 1 ()", "buy 005930 max ()",
                         "sell 005930 ()", "gdcrs add 005930 ()"):
            with self.subTest(template=template):
                self.assertEqual(self.handle("rank 1 cmd 2 " + template), [rank_batch.BATCH_USAGE])
        self.fetch.assert_not_called()
        self.dispatch.assert_not_called()

    def test_simple_rank_commands_fall_through_unchanged(self):
        for text in ("rank", "rank 2", "rank invalid"):
            with self.subTest(text=text), patch.object(rank_batch, "handle_rank", return_value=["plain result"]) as plain:
                self.assertEqual(self.handle(text), ["plain result"])
                plain.assert_called_once_with(text, self.session)
        self.fetch.assert_not_called()
        self.dispatch.assert_not_called()

    def test_one_snapshot_is_expanded_in_original_order_with_same_session(self):
        self.fetch.return_value = [stock("000660"), stock("005930")]
        messages = self.handle()
        self.fetch.assert_called_once_with(self.session, 4, 2)
        self.assertEqual(self.dispatch.call_args_list,
                         [call("gdcrs add 000660 100000"), call("gdcrs add 005930 100000")])
        self.assertIn("2종목", messages[0])
        self.assertIn("1. 000660", messages[1])
        self.assertIn("2. 005930", messages[2])

    def test_suffix_normalization_and_duplicates_preserve_first_appearance(self):
        self.fetch.return_value = [stock("005930_AL"), stock("005930"), stock("000660_NX"),
                                  stock("005930_NX"), stock("000660")]
        messages = self.handle("rank 3 cmd 5 srch ()")
        self.assertEqual(self.dispatch.call_args_list, [call("srch 005930"), call("srch 000660")])
        self.assertIn("2종목", messages[0])
        self.assertIn("KRX", rank_batch.BATCH_USAGE)

    def test_entire_snapshot_is_validated_before_first_command(self):
        for invalid in ("bad", "005930_US", "../005930", "005930;login real", "0083Q0", "00593", None):
            with self.subTest(invalid=invalid):
                self.fetch.return_value = [stock("005930"), stock(invalid)]
                result = self.handle()
                self.assertIn("일괄 명령을 실행하지 않았습니다", result[0])
        self.dispatch.assert_not_called()

    def test_short_snapshot_runs_available_codes_once_and_empty_runs_none(self):
        self.fetch.return_value = [stock("005930")]
        result = self.handle("rank 4 cmd 15 gdcrs add () 100000")
        self.dispatch.assert_called_once_with("gdcrs add 005930 100000")
        self.assertIn("1종목", result[0])
        self.dispatch.reset_mock()
        self.fetch.return_value = []
        self.assertIn("조회된 종목이 없어", self.handle()[0])
        self.dispatch.assert_not_called()

    def test_query_failure_prevents_all_commands_and_hides_external_details(self):
        self.fetch.side_effect = RuntimeError("sensitive-remote-body")
        result = self.handle()
        self.dispatch.assert_not_called()
        self.assertNotIn("sensitive-remote-body", "".join(result))
        self.assertIn("실행하지 않았습니다", result[0])

    def test_mode_change_while_fetching_prevents_dispatch(self):
        def changed(*_):
            self.session.mode = "real"
            return [stock()]
        self.fetch.side_effect = changed
        messages = self.handle()
        self.dispatch.assert_not_called()
        self.assertIn("중단", messages[-1])

    def test_token_change_between_commands_stops_without_replay(self):
        def execute(_):
            self.session.token = "new-offline-token"
            return ["첫 명령 완료"]
        self.dispatch.side_effect = execute
        messages = self.handle()
        self.dispatch.assert_called_once()
        self.assertIn("첫 명령 완료", "".join(messages))
        self.assertIn("중단", messages[-1])

    def test_cancellation_before_or_between_commands_stops_without_replay(self):
        self.handle(should_stop=lambda: True)
        self.dispatch.assert_not_called()
        stop = Mock(side_effect=[False, True])
        messages = self.handle(should_stop=stop)
        self.dispatch.assert_called_once()
        self.assertIn("중단", messages[-1])

    def test_uncertain_order_reply_stops_remaining_commands(self):
        self.dispatch.return_value = OrderReplies(["첫 주문 결과 불확실"], uncertain=True)
        messages = self.handle("rank 1 cmd 2 buy () 1 10000")
        self.dispatch.assert_called_once_with("buy 005930 1 10000")
        self.assertIn("첫 주문 결과 불확실", "".join(messages))
        self.assertIn("남은 일괄 실행을 중단", messages[-1])

    def test_known_unsent_failure_does_not_become_ambiguous(self):
        self.dispatch.return_value = OrderReplies(["주문을 전송하지 않았습니다."], uncertain=False)
        result = self.handle("rank 1 cmd 2 buy () max 1")
        self.assertEqual(self.dispatch.call_count, 2)
        self.assertNotIn("불확실", "".join(result))

    def test_unexpected_dispatch_failure_stops_and_does_not_leak_or_retry(self):
        self.dispatch.side_effect = RuntimeError("secret-token")
        messages = self.handle()
        self.dispatch.assert_called_once()
        self.assertIn("자동 재시도하지 않습니다", messages[-1])
        self.assertNotIn("secret-token", "".join(messages))


class RankListenerIntegrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.settings_file = self.root / "config" / "settings.json"
        self.session = SimpleNamespace(mode="paper", token="offline-integration-token")
        self.config = TelegramConfig("123456789:offline_integration_bot_token_abcdefghijklmnopqrstuvwxyz",
                                     998877, 123456789, "offline_bot")
        self.monitor = Mock()
        self.scheduler = CommandScheduler(self.settings_file, self.root / "reservation-state.json",
                                          now=lambda: datetime(2026, 10, 26, 9, tzinfo=KST))
        self.patch(settings, "settings_path", return_value=self.settings_file)
        self.fetch = self.patch(rank_batch, "fetch_ranked_stocks", return_value=[stock(), stock("000660")])
        for target in ("socket.socket.connect", "kiwoom_autotrader.utils.keychain._get_backend"):
            patcher = patch(target, side_effect=AssertionError("external effect forbidden"))
            self.addCleanup(patcher.stop)
            patcher.start()

    def patch(self, obj, name, **kwargs):
        patcher = patch.object(obj, name, **kwargs)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def dispatch(self, text, **options):
        command = listener._text_command(text, self.config)
        return listener._dispatch(command, text, self.config, self.session, self.scheduler,
                                  monitor=self.monitor, **options)

    def test_rank_and_cross_commands_are_classified_and_state_allowlisted(self):
        for text, expected in (("RaNk", "rank"), ("rank 4 cmd 2 srch ()", "rank"),
                               ("gdcrs list", "gdcrs"), ("start ddcrs", "cross_control"),
                               ("stop gdcrs", "cross_control")):
            with self.subTest(text=text):
                self.assertEqual(listener._text_command(text, self.config), expected)
                self.assertIn(expected, listener._COMMANDS)
        self.assertEqual(self.dispatch("rank"), [rank.MENU])
        self.fetch.assert_not_called()

    def test_rank_batch_reuses_gdcrs_handler_with_temporary_settings(self):
        result = self.dispatch("rank 4 cmd 2 gdcrs add ( ) 100000")
        self.assertEqual(settings.load_settings()["gdcrs_stocks"], [
            {"code": "005930", "budget": 100000}, {"code": "000660", "budget": 100000},
        ])
        self.fetch.assert_called_once_with(self.session, 4, 2)
        self.assertIn("등록했습니다", "".join(result))

    def test_cross_controls_update_only_temporary_settings_and_reset_monitor(self):
        self.dispatch("gdcrs intv 5 20")
        self.dispatch("start ddcrs")
        saved = settings.load_settings()
        self.assertEqual((saved["gdcrs_short_period"], saved["gdcrs_long_period"]), (5, 20))
        self.assertTrue(saved["ddcrs_enabled"])
        self.dispatch("stop ddcrs")
        self.assertFalse(settings.load_settings()["ddcrs_enabled"])
        self.assertEqual(self.monitor.reset.call_count, 3)
        self.fetch.assert_not_called()

    def test_recursive_order_dispatch_keeps_session_and_uncertain_stop_signal(self):
        buy = self.patch(listener, "handle_buy", return_value=OrderReplies(["주문 결과 불확실"], uncertain=True))
        result = self.dispatch("rank 1 cmd 2 buy () 1 10000")
        buy.assert_called_once_with("buy 005930 1 10000", self.session, before_send=ANY)
        self.assertIn("남은 일괄 실행을 중단", result[-1])

    def test_listener_stop_callback_reaches_rank_batch(self):
        stop = Mock(return_value=False)
        def search_once(*_):
            stop.return_value = True
            return ["조회 완료"]
        search = self.patch(listener, "handle_search", side_effect=search_once)
        result = self.dispatch("rank 2 cmd 2 srch ()", should_stop=stop)
        search.assert_called_once_with("srch 005930", self.session)
        self.assertIn("중단", result[-1])

    def test_scheduler_accepts_rank_and_cross_syntax_without_querying(self):
        valid = ("rank", "rank 3", "rank 4 cmd 15 gdcrs add () 100000", "gdcrs intv 5 20",
                 "gdcrs add 005930 100000", "start ddcrs", "stop gdcrs")
        for text in valid:
            with self.subTest(text=text):
                self.assertEqual(validate_command(text), text)
                self.scheduler.add("9:30", text)
        self.assertEqual(len(self.scheduler.list()), len(valid))
        for text in ("rank 5", "rank 1 cmd 0 srch ()", "rank 1 cmd 2 buy 005930 ()",
                     "rank 1 cmd 2 rsv 9:00 srch ()", "gdcrs add 005930 0"):
            with self.subTest(text=text), self.assertRaises(SchedulerError):
                self.scheduler.add("9:30", text)
        self.fetch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
