"""Offline settings commands; every runtime settings path points to a temp dir."""

from datetime import datetime
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from kiwoom_autotrader.api.session import BrokerSession, IssuedToken, KST
from kiwoom_autotrader.config import settings
from kiwoom_autotrader.telegram.commands import settings as commands
from kiwoom_autotrader.utils.keychain import Credentials


class SettingsCommandTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "config" / "data" / "settings.json"
        patcher = patch.object(settings, "settings_path", return_value=self.path)
        self.addCleanup(patcher.stop)
        patcher.start()
        self.persist = Mock(side_effect=AssertionError("unexpected settings callback"))
        self.issuer = Mock(return_value=IssuedToken("offline-private-token", "20300101090000"))
        self.session = BrokerSession(
            renewal_time="10:30", now=lambda: datetime(2026, 10, 26, 8, tzinfo=KST),
            save_renewal=self.persist,
            credential_loader=Mock(return_value=Credentials("offline-key", "offline-secret")),
            token_issuer=self.issuer,
        )
        self.session.login("paper")

    def write_nondefaults(self):
        return settings.save_settings({
            "login_renewal_time": "10:30", "channel_poll_interval_seconds": 180,
            "daily_message_export": False, "market_start_time": "09:30", "market_end_time": "15:00",
            "next_reservation_id": 3, "reservations": [
                {"id": 2, "time": "09:35", "command": "srch 005930", "created_at": "2026-10-26T08:00:00+09:00"},
            ],
        })

    def test_market_hours_trim_and_case_normalize_then_preserve_other_settings(self):
        self.write_nondefaults()
        original = settings.save_settings({"market_start_time": "09:00", "market_end_time": "15:30"})
        reply = commands.handle_market_hours(" \nMkHr  9:30   15:00 \t")
        self.assertIn("09:30 ~ 15:00", reply[0])
        updated = settings.load_settings()
        self.assertEqual(updated, {**original, "market_start_time": "09:30", "market_end_time": "15:00"})
        self.persist.assert_not_called()

    def test_market_hours_invalid_ranges_and_grammar_never_change_settings(self):
        self.write_nondefaults()
        original = self.path.read_bytes()
        for text in ("mkhr", "mkhr 9:30", "mkhr 9:30 15:00 extra", "mkhr 24:00 15:00",
                     "mkhr 9:60 15:00", "mkhr 9:3 15:00", "mkhr 15:00 15:00",
                     "mkhr 16:00 15:00", "mkhr -1:00 15:00", "mkhr null 15:00"):
            with self.subTest(text=text):
                reply = commands.handle_market_hours(text)
                self.assertNotIn("저장했습니다", reply[0])
                self.assertEqual(self.path.read_bytes(), original)

    def test_market_hours_store_failure_is_reported_without_success(self):
        self.write_nondefaults()
        original = self.path.read_bytes()
        with patch.object(commands, "save_settings", side_effect=settings.SettingsError("설정 저장 완료를 확인하지 못했습니다.")) as save:
            reply = commands.handle_market_hours("mkhr 9:00 15:30")
        save.assert_called_once_with({"market_start_time": "09:00", "market_end_time": "15:30"})
        self.assertIn("확인하지 못했습니다", reply[0])
        self.assertNotIn("저장했습니다", reply[0])
        self.assertEqual(self.path.read_bytes(), original)

    def test_stts_displays_every_setting_without_mutating_storage_or_session(self):
        original = self.write_nondefaults()
        raw = self.path.read_bytes()
        messages = commands.handle_settings(" \nSTTS\t", self.session)
        displayed = "\n".join(messages).partition("\n")[2]
        self.assertEqual(json.loads(displayed), original)
        self.assertEqual(self.path.read_bytes(), raw)
        self.assertEqual(self.session.renewal_time, "10:30")
        self.assertNotIn("offline-private-token", displayed)
        self.persist.assert_not_called()

    def test_large_settings_split_preserves_all_json_and_utf16_message_limit(self):
        rows = [{"id": index, "time": "09:35", "command": "buy \"" + "가😀" * 200 + "\" 1",
                 "created_at": "2026-10-26T08:00:00+09:00"} for index in range(1, 31)]
        expected = settings.save_settings({"reservations": rows, "next_reservation_id": 31})
        messages = commands.handle_settings("stts", self.session)
        self.assertGreater(len(messages), 1)
        for message in messages:
            self.assertTrue(message)
            self.assertLessEqual(len(message.encode("utf-16-le")) // 2, 3500)
        self.assertEqual(json.loads("\n".join(messages).partition("\n")[2]), expected)

    def test_default_resets_all_settings_and_reservations_and_applies_renewal_immediately(self):
        self.write_nondefaults()
        before_deadline = self.session.next_renewal_at
        with patch.object(self.session, "set_renewal", wraps=self.session.set_renewal) as apply:
            messages = commands.handle_settings(" \tStTs  DeFaUlT \n", self.session)
        apply.assert_called_once_with("08:55", persist=False)
        self.persist.assert_not_called()
        self.assertEqual(settings.load_settings(), settings.default_settings())
        self.assertEqual(settings.load_settings()["reservations"], [])
        self.assertEqual(settings.load_settings()["next_reservation_id"], 1)
        self.assertEqual(self.session.renewal_time, "08:55")
        self.assertNotEqual(before_deadline, self.session.next_renewal_at)
        self.assertEqual(self.session.next_renewal_at, datetime(2026, 10, 26, 8, 55, tzinfo=KST))
        self.assertEqual(self.session.token, "offline-private-token")
        self.assertEqual(self.session.mode, "paper")
        self.issuer.assert_called_once()
        self.assertIn("예약도 삭제", messages[0])
        self.assertEqual(json.loads("\n".join(messages[1:]).partition("\n")[2]), settings.default_settings())

    def test_default_save_failure_does_not_change_in_memory_renewal_or_claim_success(self):
        self.write_nondefaults()
        original = self.path.read_bytes()
        deadline = self.session.next_renewal_at
        with patch.object(commands, "reset_settings", side_effect=settings.SettingsError("설정 저장 완료를 확인하지 못했습니다.")), \
                patch.object(self.session, "set_renewal", wraps=self.session.set_renewal) as apply:
            messages = commands.handle_settings("stts default", self.session)
        apply.assert_not_called()
        self.assertEqual(self.session.renewal_time, "10:30")
        self.assertEqual(self.session.next_renewal_at, deadline)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertNotIn("되돌렸습니다", messages[0])

    def test_invalid_stts_arguments_never_read_or_reset_settings(self):
        with patch.object(commands, "load_settings") as load, patch.object(commands, "reset_settings") as reset:
            for text in ("stts extra", "stts default extra", "stts defaults", "status"):
                self.assertIn("사용법", commands.handle_settings(text, self.session)[0])
        load.assert_not_called()
        reset.assert_not_called()

    def test_stts_read_failure_returns_safe_settings_error(self):
        with patch.object(commands, "load_settings", side_effect=settings.SettingsError("프로그램 일반 설정을 읽지 못했습니다.")):
            self.assertEqual(commands.handle_settings("stts", self.session), ["프로그램 일반 설정을 읽지 못했습니다."])


if __name__ == "__main__":
    unittest.main()
