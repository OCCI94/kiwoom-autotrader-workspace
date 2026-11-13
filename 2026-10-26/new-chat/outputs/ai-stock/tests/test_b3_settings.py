"""No service or credential access: B3 defaults and explicit paper-only opt-in."""
import json
from pathlib import Path
import tempfile
from unittest import TestCase

from kiwoom_autotrader.config.settings import (
    SettingsError, _validate, default_settings, effective_b3_guards, load_settings,
    save_settings, paper_strategy_options_path,
)


class B3SettingsTests(TestCase):
    def test_defaults_and_legacy_file_read_do_not_migrate_or_enable(self):
        self.assertEqual(effective_b3_guards(default_settings(), "paper"), (100, 0))
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "settings.json"
            original = b'{"paper_automation_buy_cap":1000000}\n'
            path.write_bytes(original)
            self.assertEqual(effective_b3_guards(load_settings(path), "paper"), (100, 0))
            self.assertEqual(path.read_bytes(), original)

    def test_paper_opt_in_preserves_caps_and_real_behavior(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "settings.json"
            settings = save_settings({"gdcrs_initial_entry_pct": 50,
                "gdcrs_add_on_profit_bps": 50, "gdcrs_max_daily_entries_per_symbol": 2}, path)
            self.assertEqual(effective_b3_guards(settings, "paper"), (50, 50))
            self.assertEqual(effective_b3_guards(settings, "real"), (100, 0))
            self.assertEqual(settings["paper_automation_buy_cap"], 1_000_000)
            self.assertEqual(settings["paper_automation_symbol_cap"], 1_000_000)
            self.assertEqual(json.loads(paper_strategy_options_path(path).read_text())["gdcrs_initial_entry_pct"], 50)
            self.assertFalse(path.exists())

    def test_bad_values_fail_closed(self):
        for key, values in (("gdcrs_initial_entry_pct", (0, -1, 101, True, 50.0, "50", None)),
                            ("gdcrs_add_on_profit_bps", (-1, 10001, True, 50.0, "50", None))):
            for value in values:
                with self.subTest(key=key, value=value), self.assertRaises(SettingsError):
                    _validate({**default_settings(), "gdcrs_initial_entry_pct": 50, key: value})
        with self.assertRaises(SettingsError):
            _validate({**default_settings(), "gdcrs_add_on_profit_bps": 50})
        with self.assertRaises(SettingsError):
            effective_b3_guards({}, "unknown")

    def test_initial_split_without_add_on_is_allowed(self):
        settings = _validate({**default_settings(), "gdcrs_initial_entry_pct": 50})
        self.assertEqual(effective_b3_guards(settings, "paper"), (50, 0))
