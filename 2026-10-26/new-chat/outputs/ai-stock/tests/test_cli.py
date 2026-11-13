import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from kiwoom_autotrader.cli import main


class CLITests(unittest.TestCase):
    def test_default_program_enters_persistent_command_listener(self):
        with patch("kiwoom_autotrader.cli._telegram_command", return_value=0) as receiver:
            self.assertEqual(main([]), 0)
            self.assertEqual(receiver.call_args.args[0].command, "telegram-listen")

    def test_demo_replays_from_saved_bars_and_config(self):
        with tempfile.TemporaryDirectory() as temporary, contextlib.redirect_stdout(io.StringIO()):
            first = Path(temporary) / "first"
            replay = Path(temporary) / "replay"
            self.assertEqual(main(["demo", "--output", str(first)]), 0)
            self.assertEqual(main([
                "simulate", "--bars", str(first / "bars.csv"),
                "--config", str(first / "config.json"), "--output", str(replay),
            ]), 0)
            original = json.loads((first / "result.json").read_text())
            repeated = json.loads((replay / "result.json").read_text())
            self.assertTrue(original["fills"])
            self.assertEqual(original["mode"], "offline_simulation")
            self.assertEqual(original["fills"], repeated["fills"])
            self.assertEqual(original["cash"], repeated["cash"])
            self.assertTrue((first / "rejections.csv").is_file())

    def test_existing_run_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as temporary, contextlib.redirect_stderr(io.StringIO()):
            output = Path(temporary)
            marker = output / "result.json"
            marker.write_text("preserve this run")
            self.assertEqual(main(["demo", "--output", str(output)]), 1)
            self.assertEqual(marker.read_text(), "preserve this run")

    def test_misspelled_limit_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary, contextlib.redirect_stderr(io.StringIO()):
            config = Path(temporary) / "config.json"
            output = Path(temporary) / "result"
            config.write_text(json.dumps({"max_order_notinal": {"USD": "10"}}))
            self.assertEqual(main(["demo", "--config", str(config), "--output", str(output)]), 1)
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
