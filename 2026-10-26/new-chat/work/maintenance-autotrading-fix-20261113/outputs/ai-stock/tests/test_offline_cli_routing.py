"""Offline commands must never start authentication or the Telegram listener."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from kiwoom_autotrader.main import main


class OfflineRoutingTest(unittest.TestCase):
    def test_missing_audit_inputs_are_unknown_without_connecting(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "audit.json"
            with patch('kiwoom_autotrader.main._telegram_command') as telegram, patch('kiwoom_autotrader.main._connection_command') as broker:
                self.assertEqual(main(['offline-audit', '--output', str(output)]), 0)
                telegram.assert_not_called()
                broker.assert_not_called()
            report = json.loads(output.read_text())
            self.assertEqual(report['readiness_state'], 'UNKNOWN')
            self.assertFalse(report['account_reconciled'])

    def test_invalid_history_does_not_fall_back_to_listener(self):
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(io.StringIO()):
            with patch('kiwoom_autotrader.main._telegram_command') as telegram, patch('kiwoom_autotrader.main._connection_command') as broker:
                self.assertEqual(main(['history', 'validate', '--bundle', directory]), 2)
                telegram.assert_not_called()
                broker.assert_not_called()

    def test_invalid_research_does_not_fall_back_to_listener(self):
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stderr(io.StringIO()):
            with patch('kiwoom_autotrader.main._telegram_command') as telegram, patch('kiwoom_autotrader.main._connection_command') as broker:
                self.assertEqual(main(['research', 'audit', '--root', directory, '--manifest', str(Path(directory) / 'missing.json')]), 1)
                telegram.assert_not_called()
                broker.assert_not_called()


if __name__ == '__main__':
    unittest.main()
