"""Cross strategy settings use temp files; no service, credentials or orders."""

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from kiwoom_autotrader.config import settings
from kiwoom_autotrader.telegram.commands import crosses
from kiwoom_autotrader.telegram.commands import help as command_help


class CrossParserTests(unittest.TestCase):
    def test_case_whitespace_and_all_supported_shapes(self):
        cases = [
            (" \nGdCrS  InTv  5  20 \t", crosses.CrossCommand("intv", short_period=5, long_period=20)),
            ("gdcrs intv 1 60", crosses.CrossCommand("intv", short_period=1, long_period=60)),
            ("gdcrs intv 60 1", crosses.CrossCommand("intv", short_period=60, long_period=1)),
            ("gdcrs intv 5 5", crosses.CrossCommand("intv", short_period=5, long_period=5)),
            ("gdcrs ADD 005930 000100000", crosses.CrossCommand("add", code="005930", budget=100000)),
            ("gdcrs list", crosses.CrossCommand("list")),
            ("gdcrs REMOVE 001", crosses.CrossCommand("remove", index=1)),
            ("gdcrs clear", crosses.CrossCommand("clear")),
            (" START DDCRS ", crosses.CrossCommand("start", "ddcrs")),
            ("stop ddcrs", crosses.CrossCommand("stop", "ddcrs")),
            ("start gdcrs", crosses.CrossCommand("start", "gdcrs")),
            ("stop gdcrs", crosses.CrossCommand("stop", "gdcrs")),
        ]
        with patch.object(crosses, "load_settings") as load, patch.object(crosses, "save_settings") as save:
            for text, expected in cases:
                with self.subTest(text=text):
                    self.assertEqual(crosses.parse_cross_command(text), expected)
            load.assert_not_called()
            save.assert_not_called()

    def test_unrelated_input_is_not_interpreted_as_cross_command(self):
        for value in (None, True, "", "help", "buy 005930 1", "start", "start other", "stopping ddcrs"):
            with self.subTest(value=value):
                self.assertIsNone(crosses.parse_cross_command(value))

    def test_bad_grammar_codes_periods_budgets_and_indices_are_rejected(self):
        bad = ["gdcrs", "gdcrs unknown", "gdcrs intv 5", "gdcrs intv 5 20 extra",
               "gdcrs list extra", "gdcrs clear extra", "gdcrs remove", "gdcrs add 005930",
               "gdcrs add 삼성전자 1000", "gdcrs add 5930 1000", "gdcrs add 005930_NX 1000",
               "gdcrs add 005930_AL 1000", "gdcrs add ００５９３０ 1000", "ddcrs intv 5 20",
               "ddcrs add 005930 1000", "start ddcrs extra", "stop gdcrs extra"]
        for value in ("0", "-1", "+5", "61", "1.5", "1.0", "1e1", "１", "NaN"):
            bad += [f"gdcrs intv {value} 20", f"gdcrs intv 5 {value}"]
        for value in ("0", "-1", "+1", "1.0", "1.5", "1e3", "1,000", "１", "1000000000000"):
            bad += [f"gdcrs add 005930 {value}", f"gdcrs remove {value}"]
        for text in bad:
            with self.subTest(text=text), self.assertRaises(ValueError):
                crosses.parse_cross_command(text)


class CrossSettingsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "config" / "data" / "settings.json"
        path_patch = patch.object(settings, "settings_path", return_value=self.path)
        network_patch = patch("urllib.request.build_opener", side_effect=AssertionError("No network in settings tests"))
        self.addCleanup(path_patch.stop)
        self.addCleanup(network_patch.stop)
        path_patch.start()
        self.network = network_patch.start()

    def tearDown(self):
        self.network.assert_not_called()

    def reply(self, command):
        return "\n".join(crosses.handle_cross_command(command))

    def seed_other_settings(self):
        return settings.save_settings({
            "login_renewal_time": "08:40", "daily_message_export": False,
            "market_start_time": "09:30", "market_end_time": "15:00",
            "reservations": [{"id": 1, "time": "09:35", "command": "srch 005930",
                              "created_at": "2026-10-26T08:00:00+09:00"}],
            "next_reservation_id": 2,
        })

    def test_new_defaults_and_old_settings_migration_preserve_existing_values(self):
        defaults = settings.load_settings()
        self.assertEqual((defaults["gdcrs_short_period"], defaults["gdcrs_long_period"]), (5, 20))
        self.assertEqual(defaults["gdcrs_stocks"], [])
        self.assertIs(defaults["gdcrs_enabled"], False)
        self.assertIs(defaults["ddcrs_enabled"], False)
        self.assertFalse(self.path.exists())
        self.path.parent.mkdir(parents=True)
        self.path.write_text(json.dumps({"login_renewal_time": "08:40", "daily_message_export": False}))
        raw = self.path.read_bytes()
        migrated = settings.load_settings()
        self.assertEqual(migrated, {**defaults, "login_renewal_time": "08:40", "daily_message_export": False})
        self.assertEqual(self.path.read_bytes(), raw)

    def test_period_updates_preserve_unrelated_settings_and_allow_config_boundaries(self):
        before = self.seed_other_settings()
        for short, long in ((1, 60), (60, 1), (5, 5), (5, 20)):
            with self.subTest(short=short, long=long):
                reply = self.reply(f"gdcrs intv {short} {long}")
                self.assertIn(f"단기 {short} / 장기 {long}", reply)
                self.assertIn("gdcrs·ddcrs", reply)
                self.assertEqual(settings.load_settings(), {**before, "gdcrs_short_period": short, "gdcrs_long_period": long})
                if short >= long:
                    self.assertIn("단기가 장기보다", reply)

    def test_add_list_and_remove_use_current_registration_order(self):
        self.seed_other_settings()
        for code, budget in (("005930", 100000), ("000660", 200000), ("035420", 300000)):
            self.assertIn("등록했습니다", self.reply(f"gdcrs add {code} {budget}"))
        displayed = self.reply("gdcrs list")
        for text in ("1. 005930", "2. 000660", "3. 035420", "100,000원", "300,000원", "감시: 꺼짐"):
            self.assertIn(text, displayed)
        self.assertIn("000660", self.reply("gdcrs remove 2"))
        self.assertEqual(settings.load_settings()["gdcrs_stocks"],
                         [{"code": "005930", "budget": 100000}, {"code": "035420", "budget": 300000}])
        self.assertIn("2. 035420", self.reply("gdcrs list"))
        self.assertIn("035420", self.reply("gdcrs remove 2"))
        self.assertEqual(settings.load_settings()["gdcrs_stocks"], [{"code": "005930", "budget": 100000}])

    def test_duplicate_code_does_not_change_budget_order_or_file(self):
        self.reply("gdcrs add 005930 100000")
        raw = self.path.read_bytes()
        self.assertIn("이미 등록", self.reply("gdcrs add 005930 200000"))
        self.assertEqual(self.path.read_bytes(), raw)

    def test_out_of_range_remove_and_invalid_commands_do_not_write(self):
        self.seed_other_settings()
        raw = self.path.read_bytes()
        self.assertIn("해당 번호가 없습니다", self.reply("gdcrs remove 1"))
        for command in ("gdcrs remove 0", "gdcrs intv 61 20", "gdcrs add 삼성전자 100000",
                        "start ddcrs extra", "unrelated"):
            self.assertIn("사용법", self.reply(command))
        self.assertEqual(self.path.read_bytes(), raw)

    def test_clear_only_erases_registered_stocks(self):
        self.seed_other_settings()
        self.reply("gdcrs add 005930 100000")
        self.reply("gdcrs intv 10 30")
        self.reply("start ddcrs")
        self.reply("start gdcrs")
        before = settings.load_settings()
        self.assertIn("모두 삭제", self.reply("gdcrs clear"))
        self.assertEqual(settings.load_settings(), {**before, "gdcrs_stocks": []})
        self.assertIn("등록된 종목이 없습니다", self.reply("gdcrs list"))
        self.assertIn("등록을 기다립니다", self.reply("gdcrs list"))

    def test_start_stop_ddcrs_persist_flag_and_keep_shared_periods(self):
        before = self.seed_other_settings()
        self.reply("gdcrs intv 10 30")
        self.assertIn("감시를 켰습니다", self.reply("START DDCRS"))
        enabled = settings.load_settings()
        self.assertEqual(enabled, {**before, "gdcrs_short_period": 10, "gdcrs_long_period": 30, "ddcrs_enabled": True})
        self.assertIn("감시를 껐습니다", self.reply("STOP DDCRS"))
        self.assertEqual(settings.load_settings(), {**enabled, "ddcrs_enabled": False})

    def test_ddcrs_start_refuses_equal_or_reversed_periods_without_rewriting_settings(self):
        for short, long in ((5, 5), (20, 5)):
            self.reply(f"gdcrs intv {short} {long}")
            raw = self.path.read_bytes()
            self.assertIn("단기가 장기보다", self.reply("start ddcrs"))
            self.assertFalse(settings.load_settings()["ddcrs_enabled"])
            self.assertEqual(self.path.read_bytes(), raw)

    def test_gdcrs_start_with_empty_list_arms_waiting_without_touching_other_settings(self):
        before = self.seed_other_settings()
        result = crosses.handle_cross_command("START GDCRS")
        self.assertIsInstance(result, crosses.CrossReplies)
        self.assertTrue(result.started_gdcrs)
        self.assertFalse(result.started_ddcrs)
        self.assertIn("종목 등록을 기다립니다", "\n".join(result))
        self.assertIn("즉시 매수하지 않습니다", "\n".join(result))
        self.assertEqual(settings.load_settings(), {**before, "gdcrs_enabled": True})
        self.assertTrue(json.loads(self.path.read_text())["gdcrs_enabled"])
        displayed = self.reply("gdcrs list")
        self.assertIn("감시: 켜짐", displayed)
        self.assertIn("등록을 기다립니다", displayed)

    def test_gdcrs_start_with_registered_stocks_persists_only_its_flag_and_never_buys(self):
        self.seed_other_settings()
        self.reply("gdcrs add 005930 100000")
        self.reply("start ddcrs")
        before = settings.load_settings()
        with patch("kiwoom_autotrader.api.orders.submit_order") as submit, \
                patch("kiwoom_autotrader.telegram.commands.orders.handle_buy") as buy:
            result = crosses.handle_cross_command("start gdcrs")
        self.assertTrue(result.started_gdcrs)
        self.assertFalse(result.started_ddcrs)
        self.assertIn("새로운 완료봉 상향 교차", "\n".join(result))
        self.assertIn("첫 관측은 기준만", "\n".join(result))
        self.assertEqual(settings.load_settings(), {**before, "gdcrs_enabled": True})
        submit.assert_not_called()
        buy.assert_not_called()

    def test_stop_gdcrs_only_disables_golden_flag_and_preserves_ddcrs_and_list(self):
        self.seed_other_settings()
        self.reply("gdcrs add 005930 100000")
        self.reply("start ddcrs")
        self.reply("start gdcrs")
        before = settings.load_settings()
        result = crosses.handle_cross_command("stop gdcrs")
        self.assertIn("감시를 껐습니다", "\n".join(result))
        self.assertFalse(getattr(result, "started_gdcrs", False))
        self.assertFalse(getattr(result, "started_ddcrs", False))
        self.assertEqual(settings.load_settings(), {**before, "gdcrs_enabled": False})
        self.assertIn("감시: 꺼짐", self.reply("gdcrs list"))

    def test_gdcrs_start_refuses_equal_or_reversed_periods_without_started_signal(self):
        for short, long in ((5, 5), (20, 5)):
            self.reply(f"gdcrs intv {short} {long}")
            raw = self.path.read_bytes()
            result = crosses.handle_cross_command("start gdcrs")
            self.assertIn("단기가 장기보다", "\n".join(result))
            self.assertFalse(getattr(result, "started_gdcrs", False))
            self.assertFalse(settings.load_settings()["gdcrs_enabled"])
            self.assertEqual(self.path.read_bytes(), raw)

    def test_only_successfully_persisted_start_signals_the_corresponding_monitor(self):
        self.seed_other_settings()
        raw = self.path.read_bytes()
        for strategy in ("gdcrs", "ddcrs"):
            with self.subTest(strategy=strategy), patch.object(crosses, "save_settings", side_effect=settings.SettingsError("저장 실패")):
                result = crosses.handle_cross_command("start " + strategy)
                self.assertFalse(getattr(result, "started_gdcrs", False))
                self.assertFalse(getattr(result, "started_ddcrs", False))
                self.assertNotIn("감시를 켰습니다", "\n".join(result))
                self.assertEqual(self.path.read_bytes(), raw)
        result = crosses.handle_cross_command("start ddcrs")
        self.assertTrue(result.started_ddcrs)
        self.assertFalse(result.started_gdcrs)

    def test_adding_stock_after_arming_preserves_active_flag_without_immediate_order(self):
        self.reply("start gdcrs")
        result = self.reply("gdcrs add 005930 100000")
        self.assertIn("감시가 켜져", result)
        self.assertIn("즉시 매수하지 않습니다", result)
        saved = settings.load_settings()
        self.assertTrue(saved["gdcrs_enabled"])
        self.assertEqual(saved["gdcrs_stocks"], [{"code": "005930", "budget": 100000}])

    def test_help_documents_implemented_golden_start_and_fits_telegram_limit(self):
        self.assertIn("start gdcrs / stop gdcrs", command_help.HELP)
        self.assertIn("현재가 이하 KRX 표시호가 확인", command_help.HELP)
        self.assertIn("수수료 별도", command_help.HELP)
        self.assertNotIn("별도 구현 지시서", command_help.HELP)
        self.assertNotIn("아직 구현", crosses.CROSS_USAGE)
        self.assertLessEqual(len(command_help.HELP.encode("utf-16-le")) // 2, 4096)

    def test_maximum_budget_and_100_rows_stay_exact_and_fit_message_limit(self):
        rows = [{"code": f"{index:06d}", "budget": 999999999999} for index in range(100)]
        settings.save_settings({"gdcrs_stocks": rows})
        raw = self.path.read_bytes()
        messages = crosses.handle_cross_command("gdcrs list")
        self.assertIn("100. 000099 · 예산 999,999,999,999원", "\n".join(messages))
        for message in messages:
            self.assertLessEqual(len(message.encode("utf-16-le")) // 2, 3500)
        self.assertIn("최대 100개", self.reply("gdcrs add 005930 1"))
        self.assertEqual(self.path.read_bytes(), raw)

    def test_reset_restores_periods_clears_stocks_and_disables_both_strategies(self):
        settings.save_settings({"gdcrs_short_period": 10, "gdcrs_long_period": 30,
                                "gdcrs_stocks": [{"code": "005930", "budget": 100000}],
                                "gdcrs_enabled": True, "ddcrs_enabled": True})
        reset = settings.reset_settings()
        self.assertEqual(reset, settings.default_settings())
        self.assertEqual(reset["gdcrs_stocks"], [])
        self.assertFalse(reset["gdcrs_enabled"])
        self.assertFalse(reset["ddcrs_enabled"])
        self.assertEqual((reset["gdcrs_short_period"], reset["gdcrs_long_period"]), (5, 20))

    def test_schema_rejects_invalid_periods_flags_and_registration_rows(self):
        self.seed_other_settings()
        original = self.path.read_bytes()
        invalid = []
        for key in ("gdcrs_short_period", "gdcrs_long_period"):
            invalid.extend({key: value} for value in (True, False, 0, -1, 61, "5", 5.0, None))
        for key in ("gdcrs_enabled", "ddcrs_enabled"):
            invalid.extend({key: value} for value in (0, 1, "true", None, []))
        valid = {"code": "005930", "budget": 100000}
        bad_rows = [None, {}, {**valid, "token": "private-value"}]
        for key, values in {"code": (None, 5930, "5930", "００５９３０", "005930_NX"),
                            "budget": (None, True, 0, -1, 1.5, "100000", 1000000000000)}.items():
            bad_rows.extend({**valid, key: value} for value in values)
        invalid += [{"gdcrs_stocks": [row]} for row in bad_rows]
        invalid += [{"gdcrs_stocks": value} for value in
                    (None, {}, [valid, deepcopy(valid)], [valid] * 101)]
        for updates in invalid:
            with self.subTest(updates=updates), self.assertRaises(settings.SettingsError):
                settings.save_settings(updates)
            self.assertEqual(self.path.read_bytes(), original)

    def test_save_failure_and_unknown_errors_never_claim_success_or_leak_details(self):
        self.seed_other_settings()
        raw = self.path.read_bytes()
        with patch.object(settings.os, "replace", side_effect=OSError("private-path-secret")):
            reply = self.reply("gdcrs add 005930 100000")
        self.assertIn("확인하지 못했습니다", reply)
        self.assertNotIn("private-path-secret", reply)
        self.assertNotIn("등록했습니다", reply)
        self.assertEqual(self.path.read_bytes(), raw)
        with patch.object(crosses, "load_settings", side_effect=RuntimeError("private-token-secret")):
            reply = self.reply("gdcrs list")
        self.assertNotIn("private-token-secret", reply)
        self.assertIn("stts", reply)


if __name__ == "__main__":
    unittest.main()
