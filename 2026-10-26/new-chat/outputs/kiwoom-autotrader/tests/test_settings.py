"""Ordinary settings tests in temporary directories; never touch real settings."""

import json
from copy import deepcopy
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from kiwoom_autotrader.config import settings


class SettingsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / "config" / "data" / "settings.json"
        self.patcher = patch.object(settings, "settings_path", return_value=self.path)
        self.addCleanup(self.patcher.stop)
        self.patcher.start()

    def write(self, payload):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(payload))

    def test_defaults_are_kst_daily_0855_and_read_does_not_create_file(self):
        result = settings.load_settings()
        self.assertEqual(result["login_renewal_time"], "08:55")
        self.assertEqual(result["timezone"], "Asia/Seoul")
        self.assertEqual(result["channel_poll_interval_seconds"], 60)
        self.assertIs(result["daily_message_export"], True)
        self.assertEqual(result["market_start_time"], "09:00")
        self.assertEqual(result["market_end_time"], "15:30")
        self.assertEqual(result["reservations"], [])
        self.assertEqual(result["next_reservation_id"], 1)
        self.assertFalse(self.path.exists())

    def test_save_roundtrip_preserves_other_supported_settings_with_private_file(self):
        self.write({"channel_poll_interval_seconds": 90, "daily_message_export": False})
        settings.save_renewal_time("08:50")
        value = settings.load_settings()
        self.assertEqual(value["login_renewal_time"], "08:50")
        self.assertEqual(value["channel_poll_interval_seconds"], 90)
        self.assertFalse(value["daily_message_export"])
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(list(self.path.parent.glob(".settings-*")), [])

    def test_unknown_keys_cannot_store_tokens_or_credentials(self):
        self.write({"access_token": "fixture-secret-value"})
        original = self.path.read_bytes()
        with self.assertRaises(settings.SettingsError) as caught:
            settings.load_settings()
        self.assertNotIn("fixture-secret-value", str(caught.exception))
        with self.assertRaises(settings.SettingsError):
            settings.save_renewal_time("09:00")
        self.assertEqual(self.path.read_bytes(), original)

    def test_invalid_times_and_types_are_rejected_without_changing_settings(self):
        settings.save_renewal_time("08:55")
        original = self.path.read_bytes()
        for value in ("8:50", "24:00", "08:60", "08:55:00", None, True, 855):
            with self.subTest(value=value), self.assertRaises(settings.SettingsError):
                settings.save_renewal_time(value)
            self.assertEqual(self.path.read_bytes(), original)
        # CLI/session normalization happens before this persistence boundary.
        settings.save_renewal_time("00:00")
        self.assertEqual(settings.load_settings()["login_renewal_time"], "00:00")

    def test_invalid_saved_types_and_schema_versions_are_rejected(self):
        for value in ({"version": True}, {"version": 2}, {"timezone": "UTC"},
                      {"channel_poll_interval_seconds": True}, {"channel_poll_interval_seconds": 29},
                      {"daily_message_export": "true"}, []):
            self.write(value)
            with self.subTest(value=value), self.assertRaises(settings.SettingsError):
                settings.load_settings()

    def test_atomic_replace_failure_preserves_original_and_removes_temporary_file(self):
        settings.save_renewal_time("08:55")
        original = self.path.read_bytes()
        with patch.object(settings.os, "replace", side_effect=OSError("fixture-private-path")):
            with self.assertRaises(settings.SettingsError) as caught:
                settings.save_renewal_time("09:00")
        self.assertNotIn("fixture-private-path", str(caught.exception))
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(list(self.path.parent.glob(".settings-*")), [])

    def test_old_settings_merge_new_defaults_without_erasing_existing_choices(self):
        old = {"version": 1, "login_renewal_time": "08:40", "timezone": "Asia/Seoul",
               "channel_poll_interval_seconds": 120, "daily_message_export": False}
        self.write(old)
        original = self.path.read_bytes()
        migrated = settings.load_settings()
        self.assertEqual({key: migrated[key] for key in old}, old)
        self.assertEqual(migrated["market_start_time"], "09:00")
        self.assertEqual(migrated["market_end_time"], "15:30")
        self.assertEqual(migrated["reservations"], [])
        self.assertEqual(migrated["next_reservation_id"], 1)
        self.assertEqual(self.path.read_bytes(), original)
        settings.save_settings({"market_end_time": "15:00"})
        self.assertEqual(settings.load_settings()["login_renewal_time"], "08:40")
        self.assertEqual(set(json.loads(self.path.read_text())), set(settings.default_settings()))

    def test_market_hour_updates_preserve_reservations_and_other_settings(self):
        reservation = {"id": 1, "time": "09:05", "command": "srch 005930",
                       "created_at": "2026-10-26T08:00:00+09:00"}
        initial = settings.save_settings({"login_renewal_time": "08:40", "reservations": [reservation],
                                          "next_reservation_id": 2, "daily_message_export": False})
        updated = settings.save_settings({"market_start_time": "09:30", "market_end_time": "15:00"})
        for key in initial:
            if key not in {"market_start_time", "market_end_time"}:
                self.assertEqual(updated[key], initial[key])
        self.assertEqual(updated, settings.load_settings())

    def test_market_hour_schema_rejects_wrong_format_equal_and_reversed_bounds(self):
        settings.save_settings({})
        original = self.path.read_bytes()
        for start, end in (("9:00", "15:30"), ("24:00", "15:30"), ("09:00", "15:60"),
                           ("09:00", "09:00"), ("15:30", "09:00"), (None, "15:30"),
                           (900, "15:30"), ("09:00", False)):
            with self.subTest(start=start, end=end), self.assertRaises(settings.SettingsError):
                settings.save_settings({"market_start_time": start, "market_end_time": end})
            self.assertEqual(self.path.read_bytes(), original)

    def test_reset_restores_every_default_and_removes_reservations_even_from_corrupt_file(self):
        self.write({"unknown": "fixture-private-secret"})
        reset = settings.reset_settings()
        self.assertEqual(reset, settings.default_settings())
        self.assertEqual(reset, settings.load_settings())
        self.assertEqual(reset["reservations"], [])
        self.assertEqual(reset["next_reservation_id"], 1)
        self.assertNotIn("fixture-private-secret", self.path.read_text())
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)

    def test_reservation_schema_rejects_tampering_without_overwriting_valid_settings(self):
        row = {"id": 1, "time": "09:05", "command": "srch 005930",
               "created_at": "2026-10-26T08:00:00+09:00"}
        base = {"reservations": [row], "next_reservation_id": 2}
        settings.save_settings(base)
        original = self.path.read_bytes()
        bad_rows = []
        for key, values in {
            "id": (True, 0, -1, 2, "1", 1.5),
            "time": ("9:05", "24:00", "09:60", None, 905),
            "command": ("", "x" * 501, "srch\n005930", None, []),
            "created_at": (None, 1, "not-a-date", "2026-10-26T08:00:00",
                           "2026-02-30T08:00:00+09:00", "2026-10-26T08:00:00." + "0" * 4000 + "+09:00"),
        }.items():
            bad_rows.extend({**row, key: value} for value in values)
        bad_rows += [{**row, "token": "fixture-private-secret"}, {key: value for key, value in row.items() if key != "command"}]
        changes = [{"reservations": [bad], "next_reservation_id": 2} for bad in bad_rows]
        changes += [{"reservations": [row, deepcopy(row)]}, {"reservations": {}},
                    {"reservations": [None]}, {"reservations": [row] * 101}]
        changes += [{"next_reservation_id": value} for value in (True, 0, -1, "2", 2.1, 10**12 + 1)]
        for change in changes:
            with self.subTest(changed_keys=list(change)), self.assertRaises(settings.SettingsError):
                settings.save_settings(change)
            self.assertEqual(self.path.read_bytes(), original)

    def test_valid_reservations_roundtrip_preserve_ids_times_and_commands(self):
        rows = [{"id": index, "time": "09:05", "command": f"srch {index:06d}",
                 "created_at": "2026-10-26T08:00:00+09:00"} for index in range(1, 101)]
        settings.save_settings({"reservations": rows, "next_reservation_id": 101})
        self.assertEqual(settings.load_settings()["reservations"], rows)

    def test_failed_temp_cleanup_does_not_expose_private_path_or_replace_original(self):
        settings.save_settings({})
        original = self.path.read_bytes()
        with patch.object(settings.os, "replace", side_effect=OSError("fixture-private-path")), \
                patch.object(Path, "unlink", side_effect=OSError("fixture-private-path")):
            with self.assertRaises(settings.SettingsError) as caught:
                settings.save_settings({"market_start_time": "09:30"})
        self.assertNotIn("fixture-private-path", str(caught.exception))
        self.assertEqual(self.path.read_bytes(), original)

    def test_directory_fsync_failure_reports_uncertainty_after_replacement(self):
        settings.save_settings({})
        with patch.object(settings.os, "fsync", side_effect=[None, OSError("fixture-private-path")]):
            with self.assertRaises(settings.SettingsError) as caught:
                settings.save_settings({"market_start_time": "09:30"})
        self.assertIn("stts", str(caught.exception))
        self.assertNotIn("기존 설정을 유지", str(caught.exception))
        self.assertNotIn("fixture-private-path", str(caught.exception))
        self.assertEqual(settings.load_settings()["market_start_time"], "09:30")


if __name__ == "__main__":
    unittest.main()
