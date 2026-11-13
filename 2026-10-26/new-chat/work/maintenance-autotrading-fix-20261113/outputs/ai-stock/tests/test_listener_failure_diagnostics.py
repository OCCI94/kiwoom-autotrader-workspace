"""Receiver failure output never includes arbitrary provider details."""
import contextlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from kiwoom_autotrader import main
from kiwoom_autotrader.telegram.listener import TelegramListenerError, listener_error_code


class ListenerFailureDiagnosticsTests(unittest.TestCase):
    def test_known_categories_and_untrusted_exception(self):
        self.assertEqual(listener_error_code(TelegramListenerError("텔레그램 수신 연결이 반복해서 실패해 중지했습니다.")),
                         "poll_retry_exhausted")
        self.assertEqual(listener_error_code(TelegramListenerError("telegram token=secret-value")),
                         "unknown_listener_failure")
        self.assertEqual(listener_error_code(ValueError("private-account")), "unknown_listener_failure")

    def test_cli_prints_category_only_not_exception_text(self):
        for message, category in (("텔레그램 수신 프로그램이 이미 실행 중입니다.", "listener_already_running"),
                                  ("telegram token=secret-value", "unknown_listener_failure")):
            with self.subTest(message=message), tempfile.TemporaryDirectory() as directory:
                stderr = io.StringIO()
                with patch("kiwoom_autotrader.telegram.listener.run_listener", side_effect=TelegramListenerError(message)), contextlib.redirect_stderr(stderr):
                    result = main.main(["telegram-listen", "--state-dir", str(Path(directory))])
                self.assertEqual(result, 1)
                self.assertIn(category, stderr.getvalue())
                self.assertNotIn("secret-value", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
