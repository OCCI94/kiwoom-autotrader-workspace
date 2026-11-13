"""CLI credential flows using fake keys and mocked storage/network only."""

import contextlib
import getpass
import io
import unittest
import warnings
from unittest.mock import call, patch

from kiwoom_autotrader import cli
from kiwoom_autotrader.credentials import Credentials, KeychainError
from kiwoom_autotrader.kiwoom import KiwoomAPIError


FAKE_KEY = "unit-test-app-key-not-a-real-credential"
FAKE_SECRET = "unit-test-app-secret-not-a-real-credential"
FAKE_TOKEN = "unit-test-token-not-a-real-token"
QUOTE_ARGS = [
    "mock-quote", "--market", "KR", "--symbol", "005930", "--exchange", "KRX",
]


class AutoConnectTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stdout = io.StringIO()
        self.stderr = io.StringIO()
        self.stack.enter_context(contextlib.redirect_stdout(self.stdout))
        self.stack.enter_context(contextlib.redirect_stderr(self.stderr))
        self.credentials = Credentials(FAKE_KEY, FAKE_SECRET)
        # Patch every persistence and API entry point, even for tests that should
        # never call them. No test can access the user's Keychain or Kiwoom API.
        self.load = self.stack.enter_context(patch.object(
            cli, "load_mock_credentials", return_value=self.credentials,
        ))
        self.save = self.stack.enter_context(patch.object(cli, "save_mock_credentials"))
        self.delete = self.stack.enter_context(patch.object(
            cli, "delete_mock_credentials", return_value=True,
        ))
        self.client_class = self.stack.enter_context(patch.object(cli, "KiwoomMockClient"))
        self.client = self.client_class.return_value
        self.client.authenticate.return_value = FAKE_TOKEN
        self.client.quote.side_effect = lambda market, symbol, exchange: {
            "market": market, "symbol": symbol, "exchange": exchange, "cur_prc": "100",
        }
        self.hidden_input = self.stack.enter_context(patch.object(
            getpass, "getpass", side_effect=[FAKE_KEY, FAKE_SECRET],
        ))
        self.isatty = self.stack.enter_context(patch.object(
            cli.sys.stdin, "isatty", return_value=False,
        ))
        self.stack.enter_context(patch(
            "socket.create_connection", side_effect=AssertionError("Network is forbidden in CLI tests"),
        ))

    def output(self):
        return self.stdout.getvalue() + self.stderr.getvalue()

    def assert_no_secret_output(self):
        for value in (FAKE_KEY, FAKE_SECRET, FAKE_TOKEN):
            self.assertNotIn(value, self.output())

    def assert_storage_unchanged(self):
        self.save.assert_not_called()
        self.delete.assert_not_called()

    def test_connect_uses_saved_credentials_without_any_prompt(self):
        self.assertEqual(cli.main(["connect"]), 0)
        self.client_class.assert_called_once_with(FAKE_KEY, FAKE_SECRET)
        self.client.authenticate.assert_called_once_with()
        self.assertEqual(self.client.quote.call_args_list, [
            call("KR", "005930", "KRX"), call("US", "AAPL", "NASDAQ"),
        ])
        self.hidden_input.assert_not_called()
        self.assert_storage_unchanged()
        self.assertTrue(self.stdout.getvalue().strip())
        self.assert_no_secret_output()

    def test_explicit_connect_uses_saved_keys(self):
        self.assertEqual(cli.main(["connect"]), 0)
        self.client_class.assert_called_once_with(FAKE_KEY, FAKE_SECRET)
        self.client.authenticate.assert_called_once_with()
        self.assertEqual(self.client.quote.call_args_list, [
            call("KR", "005930", "KRX"), call("US", "AAPL", "NASDAQ"),
        ])
        self.hidden_input.assert_not_called()
        self.assert_storage_unchanged()
        self.assert_no_secret_output()

    def test_connect_missing_credentials_explains_setup_without_prompt(self):
        self.load.return_value = None
        self.assertEqual(cli.main(["connect"]), 1)
        self.assertIn("credentials-setup", self.output())
        self.hidden_input.assert_not_called()
        self.client_class.assert_not_called()
        self.assert_storage_unchanged()

    def test_connect_locked_keychain_does_not_fall_back_to_plaintext_input(self):
        self.load.side_effect = KeychainError("Keychain unavailable")
        self.assertEqual(cli.main(["connect"]), 1)
        self.hidden_input.assert_not_called()
        self.client_class.assert_not_called()
        self.assert_storage_unchanged()
        self.assert_no_secret_output()

    def test_connect_auth_failure_keeps_keys_and_does_not_log_sensitive_error(self):
        self.client.authenticate.side_effect = KiwoomAPIError(
            f"Authentication failed: {FAKE_KEY} {FAKE_SECRET} {FAKE_TOKEN}"
        )
        self.assertEqual(cli.main(["connect"]), 1)
        self.client.quote.assert_not_called()
        self.hidden_input.assert_not_called()
        self.assert_storage_unchanged()
        self.assert_no_secret_output()

    def test_connect_quote_failure_returns_failure_without_deleting_keys(self):
        self.client.quote.side_effect = KiwoomAPIError("Quote unavailable")
        self.assertEqual(cli.main(["connect"]), 1)
        self.hidden_input.assert_not_called()
        self.assert_storage_unchanged()
        self.assert_no_secret_output()

    def test_setup_authenticates_before_saving(self):
        self.isatty.return_value = True
        events = []
        self.client.authenticate.side_effect = lambda: events.append("authenticate")
        self.save.side_effect = lambda value: events.append("save")
        self.assertEqual(cli.main(["credentials-setup"]), 0)
        self.assertEqual(events, ["authenticate", "save"])
        self.assertEqual(self.hidden_input.call_count, 2)
        self.client_class.assert_called_once_with(FAKE_KEY, FAKE_SECRET)
        self.save.assert_called_once_with(self.credentials)
        self.client.quote.assert_not_called()
        self.delete.assert_not_called()
        self.assert_no_secret_output()

    def test_setup_auth_failure_preserves_existing_saved_keys(self):
        self.isatty.return_value = True
        self.load.return_value = Credentials("previous-fake-key", "previous-fake-secret")
        self.client.authenticate.side_effect = KiwoomAPIError(
            f"Authentication failed: {FAKE_KEY} {FAKE_SECRET} {FAKE_TOKEN}"
        )
        self.assertEqual(cli.main(["credentials-setup"]), 1)
        self.assert_storage_unchanged()
        self.client.quote.assert_not_called()
        self.assert_no_secret_output()

    def test_setup_nonterminal_refuses_input_before_authentication(self):
        self.assertEqual(cli.main(["credentials-setup"]), 1)
        self.hidden_input.assert_not_called()
        self.client_class.assert_not_called()
        self.assert_storage_unchanged()

    def test_setup_hidden_input_failure_does_not_fall_back_to_echoed_input(self):
        self.isatty.return_value = True
        fallback_reached = []

        def unsafe_input_fallback(prompt):
            warnings.warn("Cannot control echo", getpass.GetPassWarning)
            fallback_reached.append(True)
            return FAKE_KEY

        self.hidden_input.side_effect = unsafe_input_fallback
        self.assertEqual(cli.main(["credentials-setup"]), 1)
        self.assertFalse(fallback_reached)
        self.client_class.assert_not_called()
        self.assert_storage_unchanged()

    def test_setup_keychain_failure_does_not_report_success(self):
        self.isatty.return_value = True
        self.save.side_effect = KeychainError("Keychain unavailable")
        self.assertEqual(cli.main(["credentials-setup"]), 1)
        self.client.authenticate.assert_called_once_with()
        self.save.assert_called_once_with(self.credentials)
        self.delete.assert_not_called()
        self.assert_no_secret_output()

    def test_setup_interrupted_input_does_not_modify_credentials(self):
        self.isatty.return_value = True
        self.hidden_input.side_effect = KeyboardInterrupt
        self.assertEqual(cli.main(["credentials-setup"]), 130)
        self.client_class.assert_not_called()
        self.assert_storage_unchanged()

    def test_status_reports_presence_without_authentication_or_secret_values(self):
        self.assertEqual(cli.main(["credentials-status"]), 0)
        present_output = self.output()
        self.assertTrue(present_output.strip())
        self.client_class.assert_not_called()
        self.hidden_input.assert_not_called()
        self.assert_storage_unchanged()
        self.assert_no_secret_output()

        self.stdout.seek(0)
        self.stdout.truncate()
        self.stderr.seek(0)
        self.stderr.truncate()
        self.load.return_value = None
        self.assertEqual(cli.main(["credentials-status"]), 0)
        self.assertTrue(self.output().strip())
        self.assertNotEqual(self.output(), present_output)
        self.client_class.assert_not_called()
        self.hidden_input.assert_not_called()
        self.assert_storage_unchanged()

    def test_status_keychain_failure_is_reported_without_prompting(self):
        self.load.side_effect = KeychainError("Keychain unavailable")
        self.assertEqual(cli.main(["credentials-status"]), 1)
        self.hidden_input.assert_not_called()
        self.client_class.assert_not_called()
        self.assert_storage_unchanged()

    def test_delete_removes_keys_only_when_explicit_command_runs(self):
        self.assertEqual(cli.main(["credentials-delete"]), 0)
        self.delete.assert_called_once_with()
        self.save.assert_not_called()
        self.hidden_input.assert_not_called()
        self.client_class.assert_not_called()
        self.assert_no_secret_output()

    def test_delete_absent_credentials_is_safe(self):
        self.delete.return_value = False
        self.assertEqual(cli.main(["credentials-delete"]), 0)
        self.delete.assert_called_once_with()
        self.save.assert_not_called()
        self.client_class.assert_not_called()
        self.assert_no_secret_output()

    def test_mock_quote_prefers_stored_keys_without_prompting(self):
        self.assertEqual(cli.main(QUOTE_ARGS), 0)
        self.client_class.assert_called_once_with(FAKE_KEY, FAKE_SECRET)
        self.client.authenticate.assert_called_once_with()
        self.client.quote.assert_called_once_with("KR", "005930", "KRX")
        self.hidden_input.assert_not_called()
        self.assert_storage_unchanged()
        self.assert_no_secret_output()

    def test_mock_quote_temporary_keys_are_never_saved(self):
        self.load.return_value = None
        self.isatty.return_value = True
        self.assertEqual(cli.main(QUOTE_ARGS), 0)
        self.assertEqual(self.hidden_input.call_count, 2)
        self.client_class.assert_called_once_with(FAKE_KEY, FAKE_SECRET)
        self.client.quote.assert_called_once_with("KR", "005930", "KRX")
        self.assert_storage_unchanged()
        self.assert_no_secret_output()

    def test_mock_quote_never_logs_secret_fields_from_server_response(self):
        self.client.quote.side_effect = None
        self.client.quote.return_value = {
            "appkey": FAKE_KEY,
            "secretkey": FAKE_SECRET,
            "token": FAKE_TOKEN,
            "return_msg": f"Echoed {FAKE_KEY} {FAKE_SECRET}",
            "cur_prc": FAKE_TOKEN,
        }
        self.assertEqual(cli.main(QUOTE_ARGS), 0)
        self.assert_no_secret_output()
        self.assert_storage_unchanged()

    def test_mock_quote_missing_keys_in_nonterminal_refuses_plain_input(self):
        self.load.return_value = None
        self.assertEqual(cli.main(QUOTE_ARGS), 1)
        self.hidden_input.assert_not_called()
        self.client_class.assert_not_called()
        self.assert_storage_unchanged()

    def test_mock_quote_locked_keychain_does_not_prompt_for_replacement_keys(self):
        self.load.side_effect = KeychainError("Keychain unavailable")
        self.isatty.return_value = True
        self.assertEqual(cli.main(QUOTE_ARGS), 1)
        self.hidden_input.assert_not_called()
        self.client_class.assert_not_called()
        self.assert_storage_unchanged()


if __name__ == "__main__":
    unittest.main()
