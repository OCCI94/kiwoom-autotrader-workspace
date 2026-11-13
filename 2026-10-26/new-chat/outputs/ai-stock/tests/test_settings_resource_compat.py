"""Legacy packaged defaults are read compatibly without changing any bytes."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from kiwoom_autotrader.config import settings


def fixture_defaults():
    return dict(version=1, login_renewal_time="08:55", timezone="Asia/Seoul",
                channel_poll_interval_seconds=60, daily_message_export=True,
                market_start_time="09:00", market_end_time="15:30", reservations=[],
                next_reservation_id=1, gdcrs_short_period=5, gdcrs_long_period=20,
                gdcrs_stocks=[], gdcrs_enabled=False, ddcrs_enabled=False,
                gdcrs_min_cross_bps=0, gdcrs_reentry_cooldown_minutes=0,
                paper_automation_only=False, paper_automation_buy_cap=1000000)


class SettingsResourceCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "data").mkdir()
        self.resource = self.root / "data/settings-default.json"
        self.overrides = self.root / "settings.json"
        self.addCleanup(patch.stopall)
        patch.object(settings, "__file__", str(self.root / "settings.py")).start()
        patch.object(settings, "settings_path", return_value=self.overrides).start()
        patch.object(settings, "_write_settings", side_effect=AssertionError("read attempted write")).start()

    def test_legacy_and_new_resource_have_identical_effective_settings(self):
        legacy = fixture_defaults()
        self.resource.write_text(json.dumps(legacy))
        self.overrides.write_text(json.dumps({"gdcrs_enabled": True,
            "gdcrs_stocks": [{"code": "003220", "budget": 1000000}],
            "paper_automation_only": True}))
        original, overrides = self.resource.read_bytes(), self.overrides.read_bytes()
        old_result = settings.load_settings()
        self.assertEqual(old_result["paper_automation_symbol_cap"], 1000000)
        self.assertEqual(old_result["paper_automation_buy_cap"], 1000000)
        self.assertEqual(old_result["gdcrs_min_cross_bps"], 0)
        self.assertEqual(old_result["gdcrs_reentry_cooldown_minutes"], 0)
        self.assertEqual(self.resource.read_bytes(), original)
        self.assertEqual(self.overrides.read_bytes(), overrides)
        self.resource.write_text(json.dumps({**legacy, "paper_automation_symbol_cap": 1000000}))
        self.assertEqual(settings.load_settings(), old_result)
        self.assertEqual(self.overrides.read_bytes(), overrides)

    def test_explicit_valid_cap_is_not_replaced(self):
        self.resource.write_text(json.dumps({**fixture_defaults(), "paper_automation_symbol_cap": 2000000}))
        original = self.resource.read_bytes()
        self.assertEqual(settings.load_settings()["paper_automation_symbol_cap"], 2000000)
        self.assertEqual(self.resource.read_bytes(), original)
        self.assertFalse(self.overrides.exists())

    def test_invalid_explicit_cap_is_rejected_without_changes(self):
        for bad in (None, True, "1000000", 0, -1, 3000001):
            with self.subTest(value=bad):
                self.resource.write_text(json.dumps({**fixture_defaults(), "paper_automation_symbol_cap": bad}))
                original = self.resource.read_bytes()
                with self.assertRaises(settings.SettingsError):
                    settings.load_settings()
                self.assertEqual(self.resource.read_bytes(), original)
                self.assertFalse(self.overrides.exists())

    def test_other_missing_fields_are_not_silently_defaulted(self):
        value = fixture_defaults()
        del value["paper_automation_buy_cap"]
        self.resource.write_text(json.dumps(value))
        with self.assertRaises(settings.SettingsError):
            settings.load_settings()


if __name__ == "__main__":
    unittest.main()
