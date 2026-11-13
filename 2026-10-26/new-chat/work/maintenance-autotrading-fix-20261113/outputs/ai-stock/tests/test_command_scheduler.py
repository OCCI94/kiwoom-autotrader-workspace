"""Offline daily reservations: durable claims, temporal boundaries and commands."""

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from kiwoom_autotrader.config.settings import load_settings, reset_settings, save_settings
from kiwoom_autotrader.telegram.commands import reservations
from kiwoom_autotrader.utils import command_scheduler as scheduling


class CommandSchedulerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.settings = self.root / "config" / "data" / "settings.json"
        self.state = self.settings.parent / "reservation-state.json"
        self.now = datetime(2026, 10, 26, 8, 50, tzinfo=scheduling.KST)  # Monday.
        self.scheduler = self.new_scheduler()

    def new_scheduler(self):
        return scheduling.CommandScheduler(self.settings, self.state, now=lambda: self.now)

    def test_add_list_remove_persist_only_expected_schema_and_preserve_other_settings(self):
        save_settings({"market_start_time": "08:30"}, self.settings)
        first = self.scheduler.add("9:05", '  BUY "JYP Ent." 1  ')
        second = self.scheduler.add("08:55", "LOGIN paper")
        self.assertEqual(first, {"id": 1, "time": "09:05", "command": 'BUY "JYP Ent." 1',
                                 "created_at": "2026-10-26T08:50:00.000000+09:00"})
        self.assertEqual(second["id"], 2)
        self.assertEqual(self.scheduler.list(), [first, second])
        settings = load_settings(self.settings)
        self.assertEqual(settings["next_reservation_id"], 3)
        self.assertEqual(settings["market_start_time"], "08:30")
        self.assertTrue(self.scheduler.remove(1))
        self.assertFalse(self.scheduler.remove(99))
        self.assertEqual(self.scheduler.list(), [second])
        self.assertEqual(self.new_scheduler().list(), [second])
        self.assertEqual(self.settings.stat().st_mode & 0o777, 0o600)

    def test_claim_is_durable_before_return_and_only_once_per_day(self):
        row = self.scheduler.add("9:00", "srch 삼성전자")
        self.assertIsNone(self.scheduler.claim_due())
        self.assertEqual(self.scheduler.seconds_until_next(), 600)
        self.now += timedelta(minutes=10)
        self.assertEqual(self.scheduler.seconds_until_next(), 0)
        self.assertEqual(self.scheduler.claim_due(), row)
        saved = json.loads(self.state.read_text())
        self.assertEqual(saved, {"version": 1, "claimed": {f"1:{row['created_at']}": "2026-10-26"}})
        self.assertEqual(self.state.stat().st_mode & 0o777, 0o600)
        self.assertIsNone(self.scheduler.claim_due())
        self.assertEqual(self.scheduler.seconds_until_next(), 86400)
        self.now += timedelta(days=1)
        self.assertEqual(self.scheduler.claim_due(), row)
        self.assertIsNone(self.scheduler.claim_due())

    def test_multiple_due_jobs_are_claimed_one_at_a_time_by_time_then_id(self):
        late = self.scheduler.add("9:05", "report")
        early = self.scheduler.add("9:00", "login paper")
        same = self.scheduler.add("9:00", "srch 삼성전자")
        self.now += timedelta(minutes=20)
        self.assertEqual(self.scheduler.claim_due(), early)
        self.assertEqual(self.scheduler.seconds_until_next(), 0)
        self.assertEqual(self.scheduler.claim_due(), same)
        self.assertEqual(self.scheduler.claim_due(), late)
        self.assertIsNone(self.scheduler.claim_due())

    def test_two_instances_use_persisted_claim_without_duplicate_side_effect(self):
        row = self.scheduler.add("9:00", "report")
        other = self.new_scheduler()
        self.now += timedelta(minutes=10)
        self.assertEqual(self.scheduler.claim_due(), row)
        self.assertIsNone(other.claim_due())

    def test_restart_does_not_catch_up_passed_or_equal_startup_time(self):
        past = self.scheduler.add("9:00", "report")
        future = self.scheduler.add("9:05", "srch 삼성전자")
        self.now = self.now.replace(hour=9, minute=0)
        restarted = self.new_scheduler()
        self.assertIsNone(restarted.claim_due())
        self.assertEqual(restarted.seconds_until_next(), 300)
        self.now += timedelta(minutes=5)
        self.assertEqual(restarted.claim_due(), future)
        self.assertIsNone(restarted.claim_due())
        self.now += timedelta(days=1)
        self.assertEqual(restarted.claim_due(), past)

    def test_registration_at_or_after_target_waits_for_next_weekday(self):
        self.now = self.now.replace(hour=9, minute=0)
        at = self.scheduler.add("9:00", "report")
        self.now += timedelta(minutes=1)
        after = self.scheduler.add("9:00", "srch 삼성전자")
        self.assertIsNone(self.scheduler.claim_due())
        self.assertEqual(self.scheduler.seconds_until_next(), 23 * 3600 + 59 * 60)
        self.now = self.now.replace(day=27, minute=0)
        self.assertEqual(self.scheduler.claim_due(), at)
        self.assertEqual(self.scheduler.claim_due(), after)

    def test_registration_just_before_target_and_weekend_skip(self):
        self.now = datetime(2026, 10, 30, 8, 59, 59, 999999, tzinfo=scheduling.KST)
        row = self.scheduler.add("9:00", "report")
        self.now += timedelta(microseconds=1)
        self.assertEqual(self.scheduler.claim_due(), row)
        self.assertEqual(self.scheduler.seconds_until_next(), 3 * 86400)
        self.now += timedelta(days=1)
        self.assertIsNone(self.scheduler.claim_due())
        self.assertEqual(self.scheduler.seconds_until_next(), 2 * 86400)
        self.now += timedelta(days=1)
        self.assertIsNone(self.scheduler.claim_due())
        self.now += timedelta(days=1)
        self.assertEqual(self.scheduler.claim_due(), row)

    def test_utc_clock_is_interpreted_in_kst_and_naive_clock_rejected(self):
        self.now = datetime(2026, 10, 25, 23, 50, tzinfo=timezone.utc)
        scheduler = self.new_scheduler()
        row = scheduler.add("9:00", "report")
        self.assertTrue(row["created_at"].endswith("+09:00"))
        self.now += timedelta(minutes=10)
        self.assertEqual(scheduler.claim_due(), row)
        with self.assertRaises(scheduling.SchedulerError):
            scheduling.CommandScheduler(self.settings, self.state, now=lambda: datetime(2026, 10, 26))

    def test_checkpoint_failure_does_not_return_or_retry_same_day_and_preserves_prior_state(self):
        first = self.scheduler.add("9:00", "report")
        second = self.scheduler.add("9:00", "srch 삼성전자")
        self.now += timedelta(minutes=10)
        self.assertEqual(self.scheduler.claim_due(), first)
        previous = self.state.read_bytes()
        with patch.object(scheduling.os, "replace", side_effect=OSError("private-fixture-secret")):
            with self.assertRaises(scheduling.SchedulerError) as caught:
                self.scheduler.claim_due()
        self.assertNotIn("private-fixture-secret", str(caught.exception))
        self.assertEqual(self.state.read_bytes(), previous)
        self.assertEqual(list(self.state.parent.glob(".reservation-state-*")), [])
        self.assertIsNone(self.scheduler.claim_due())
        self.assertIsNone(self.new_scheduler().claim_due())
        self.now += timedelta(days=1)
        self.assertEqual(self.scheduler.claim_due(), first)
        self.assertEqual(self.scheduler.claim_due(), second)

    def test_directory_sync_uncertainty_also_blocks_the_claim(self):
        self.scheduler.add("9:00", "report")
        self.now += timedelta(minutes=10)
        original = scheduling.os.fsync
        count = 0
        def fail_directory(fd):
            nonlocal count
            count += 1
            if count == 2:
                raise OSError("uncertain directory sync")
            return original(fd)
        with patch.object(scheduling.os, "fsync", side_effect=fail_directory):
            with self.assertRaises(scheduling.SchedulerError):
                self.scheduler.claim_due()
        self.assertIsNone(self.scheduler.claim_due())
        self.assertIsNone(self.new_scheduler().claim_due())

    def test_deletion_and_settings_reset_take_effect_without_restarting(self):
        old = self.scheduler.add("9:00", "report")
        self.now += timedelta(minutes=10)
        self.assertEqual(self.scheduler.claim_due(), old)
        reset_settings(self.settings)
        self.assertEqual(self.scheduler.list(), [])
        self.assertIsNone(self.scheduler.claim_due())
        self.assertIsNone(self.scheduler.seconds_until_next())
        new = self.scheduler.add("9:01", "srch 삼성전자")
        self.assertEqual(new["id"], old["id"])
        self.assertNotEqual(new["created_at"], old["created_at"])
        self.now += timedelta(minutes=1)
        self.assertEqual(self.scheduler.claim_due(), new)
        future = self.scheduler.add("9:02", "report")
        save_settings({"reservations": [new]}, self.settings)
        self.now += timedelta(minutes=1)
        self.assertIsNone(self.scheduler.claim_due())
        self.assertNotIn(future, self.scheduler.list())

    def test_reset_and_same_frozen_clock_cannot_reuse_a_previous_identity(self):
        initial_time = self.now
        old = self.scheduler.add("9:00", "report")
        self.now += timedelta(minutes=10)
        self.assertEqual(self.scheduler.claim_due(), old)
        reset_settings(self.settings)
        self.now = initial_time
        replacement = self.scheduler.add("9:00", "srch 삼성전자")
        self.assertEqual(old["id"], replacement["id"])
        self.assertEqual(datetime.fromisoformat(replacement["created_at"]) - datetime.fromisoformat(old["created_at"]),
                         timedelta(microseconds=1))
        self.now += timedelta(minutes=10)
        self.assertEqual(self.scheduler.claim_due(), replacement)

    def test_clock_rollback_never_replays_a_claimed_day_or_startup_backlog(self):
        row = self.scheduler.add("9:00", "report")
        self.now += timedelta(days=1, minutes=10)
        self.assertEqual(self.scheduler.claim_due(), row)
        self.now -= timedelta(days=1)
        self.assertIsNone(self.scheduler.claim_due())
        self.assertEqual(self.scheduler.seconds_until_next(), 2 * 86400)
        self.now -= timedelta(days=1)
        self.assertIsNone(self.scheduler.claim_due())

    def test_malformed_checkpoint_fails_closed_instead_of_resetting_execution_history(self):
        self.scheduler.add("9:00", "report")
        self.now += timedelta(minutes=10)
        for value in ([], {"version": True, "claimed": {}}, {"version": 1, "claimed": {"bad": "2026-10-26"}},
                      {"version": 1, "claimed": {"1:2026-10-26T08:50:00+09:00": "2026-02-31"}}):
            self.state.write_text(json.dumps(value))
            with self.assertRaises(scheduling.SchedulerError):
                self.scheduler.claim_due()
            self.assertEqual(json.loads(self.state.read_text()), value)

    def test_invalid_times_nested_or_unknown_commands_and_ids_are_rejected(self):
        for time in (None, "24:00", "-1:00", "8:5", "09:60", "09:00:00"):
            with self.assertRaises(scheduling.SchedulerError):
                self.scheduler.add(time, "report")
        for command in (None, "", "rsv 9:00 report", "RSV list", "unknown argument",
                        "report\nsecret", "report " + "x" * 500):
            with self.assertRaises(scheduling.SchedulerError):
                self.scheduler.add("9:00", command)
        for id in (True, "1", 0, -1):
            with self.assertRaises(scheduling.SchedulerError):
                self.scheduler.remove(id)
        self.assertEqual(self.scheduler.list(), [])

    def test_reservation_handler_normalizes_actions_preserves_quotes_and_lists_safely(self):
        reply = reservations.handle_reservation('  RsV 9:05 BuY "JYP Ent." 1  ', self.scheduler)
        self.assertIn("예약 1번 등록", reply[0])
        self.assertEqual(self.scheduler.list()[0]["command"], 'BuY "JYP Ent." 1')
        self.assertIn('09:05 — BuY "JYP Ent." 1', reservations.handle_reservation("RSV LIST", self.scheduler)[0])
        self.assertIn("삭제", reservations.handle_reservation("rsv REMOVE 1", self.scheduler)[0])
        self.assertIn("찾지 못했습니다", reservations.handle_reservation("rsv remove 1", self.scheduler)[0])
        for text in (None, "rsv", "rsv list extra", "rsv remove", "rsv remove 0", "rsv remove 1 extra"):
            self.assertEqual(reservations.handle_reservation(text, self.scheduler), [reservations.USAGE])
        self.assertIn("예약할 수 없습니다", reservations.handle_reservation("rsv 9:00 rsv list", self.scheduler)[0])

    def test_malformed_syntax_is_never_saved_and_private_arguments_are_not_echoed(self):
        self.scheduler.add("9:00", "report")
        previous = self.settings.read_bytes()
        private = "private-fixture-argument"
        invalid = (
            f"login paper {private}", f"login real {private}", "login", "login mock",
            f"login renewal 8:55 {private}", "login renewal 24:00", "login renewal 8:5",
            f"buy 005930 1 {private}", "buy 005930", "buy 005930 0", "buy 005930 1 0",
            "buy 005930 1 1.5", "buy 005930 1 1000 extra", 'buy "삼성전자 1',
            f"sell 005930 1 {private}", "sell 삼성전자 1", "sell 005930 -1",
            "srch", "srch 005930 extra", "srch 123", "srch 005930_US", "srch " + "가" * 41,
            "mkhr", "mkhr 9:00", "mkhr 15:30 9:00", "mkhr 9:00 9:00", "mkhr 9:00 24:00",
            f"mkhr 9:00 15:30 {private}", "stts invalid", f"stts default {private}",
            "power", "power on", f"power off {private}",
        )
        invalid += tuple(f"{alias} {private}" for alias in (
            "help", "/help", "/start", "status", "/status", "top", "/top", "channels", "/channels",
            "posts", "/posts", "도움말", "상태", "상위20", "거래대금", "채널", "채널글", "report", "r",
        ))
        for command in invalid:
            with self.subTest(command=command), self.assertRaises(scheduling.SchedulerError) as caught:
                self.scheduler.add("9:05", command)
            self.assertEqual(str(caught.exception), "예약할 명령의 형식이 올바르지 않습니다. help에서 사용법을 확인하세요.")
            self.assertNotIn(private, str(caught.exception))
            self.assertEqual(self.settings.read_bytes(), previous)
        self.assertEqual(len(self.scheduler.list()), 1)

    def test_valid_existing_command_syntax_is_preserved_without_external_calls(self):
        commands = (
            "LOGIN paper", "login REAL", "login renewal 8:55", "login renewal 00:00",
            "BuY 삼성전자 1", 'buy "JYP Ent." 1 70000', "buy 005930 2", "SELL 005930 3",
            "srch 삼성전자", "SRCH JYP Ent.", "srch 005930_NX", "mkhr 9:00 15:30",
            "stts", "STTS DEFAULT", "Power OFF", "report", "R", "help", "/start", "status", "top",
            "channels", "posts", "도움말", "상태", "상위20", "거래대금", "채널", "채널글",
        )
        with patch("urllib.request.build_opener", side_effect=AssertionError("network forbidden")) as transport, \
                patch("kiwoom_autotrader.api.session.load_credentials", side_effect=AssertionError("keychain forbidden")) as credentials:
            for command in commands:
                with self.subTest(command=command):
                    self.assertEqual(scheduling.validate_command("  " + command + "  "), command)
            transport.assert_not_called()
            credentials.assert_not_called()

    def test_preexisting_invalid_reservation_is_rejected_again_before_claim(self):
        row = self.scheduler.add("9:00", "login paper")
        row["command"] = "login paper private-fixture-argument"
        save_settings({"reservations": [row]}, self.settings)
        self.now += timedelta(minutes=10)
        with patch.object(self.scheduler, "_save_state") as save:
            with self.assertRaises(scheduling.SchedulerError) as caught:
                self.scheduler.claim_due()
            save.assert_not_called()
        self.assertNotIn("private-fixture-argument", str(caught.exception))
        self.assertIsNone(self.scheduler.claim_due())

    def test_handler_does_not_echo_or_store_malformed_private_command(self):
        reply = reservations.handle_reservation("rsv 9:05 login paper private-fixture-argument", self.scheduler)
        self.assertEqual(len(reply), 1)
        self.assertIn("형식", reply[0])
        self.assertNotIn("private-fixture-argument", reply[0])
        self.assertFalse(self.settings.exists())
        self.assertEqual(self.scheduler.list(), [])

    def test_supported_aliases_and_long_list_messages_stay_within_telegram_limit(self):
        for command in ("도움말", "/start", "상위20", "채널글", "power off", "stts default", "r"):
            self.scheduler.add("9:00", command)
        long = "srch " + "가" * 40
        for _ in range(50):
            self.scheduler.add("9:00", long)
        replies = reservations.handle_reservation("rsv list", self.scheduler)
        self.assertGreater(len(replies), 1)
        for reply in replies:
            self.assertTrue(reply)
            self.assertLessEqual(len(reply.encode("utf-8")), 3900)
            self.assertLessEqual(len(reply.encode("utf-16-le")) // 2, 3900)


if __name__ == "__main__":
    unittest.main()
