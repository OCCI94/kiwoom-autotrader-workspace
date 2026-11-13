"""Offline HTTP contract tests; never contact a brokerage server."""

import io
import json
import traceback
import unittest
import urllib.error
from unittest.mock import MagicMock, patch

from kiwoom_autotrader.kiwoom import KiwoomAPIError, KiwoomMockClient


class KiwoomMockClientTests(unittest.TestCase):
    def setUp(self):
        opener_patch = patch("kiwoom_autotrader.kiwoom.urllib.request.build_opener")
        self.addCleanup(opener_patch.stop)
        self.opener = opener_patch.start().return_value
        sleep_patch = patch("kiwoom_autotrader.kiwoom.time.sleep")
        self.addCleanup(sleep_patch.stop)
        self.sleep = sleep_patch.start()
        self.client = KiwoomMockClient("test-app-key", "test-app-secret")

    @staticmethod
    def response(payload):
        response = MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = json.dumps(payload).encode("utf-8")
        return response

    def authenticate(self):
        self.opener.open.return_value = self.response(
            {"return_code": 0, "token": "test-access-token", "token_type": "bearer"}
        )
        self.client.authenticate()

    def last_request(self):
        args, kwargs = self.opener.open.call_args
        return args[0], kwargs

    def test_authentication_uses_mock_domain_and_official_schema(self):
        self.authenticate()
        request, kwargs = self.last_request()
        self.assertEqual(request.full_url, "https://mockapi.kiwoom.com/oauth2/token")
        self.assertEqual(request.method, "POST")
        self.assertEqual(request.get_header("Api-id"), "au10001")
        self.assertEqual(request.get_header("Content-type"), "application/json;charset=UTF-8")
        self.assertIsNone(request.get_header("Authorization"))
        self.assertEqual(
            json.loads(request.data),
            {"grant_type": "client_credentials", "appkey": "test-app-key", "secretkey": "test-app-secret"},
        )
        self.assertEqual(kwargs, {"timeout": 15})

    def test_domestic_quote_keeps_raw_prices_and_uses_auth_header(self):
        self.authenticate()
        expected = {"return_code": 0, "stk_cd": "005930", "cur_prc": "+70000"}
        self.opener.open.return_value = self.response(expected)
        self.assertEqual(self.client.quote("KR", "005930", "KRX"), expected)
        request, _ = self.last_request()
        self.assertEqual(request.full_url, "https://mockapi.kiwoom.com/api/dostk/stkinfo")
        self.assertEqual(request.get_header("Api-id"), "ka10001")
        self.assertEqual(request.get_header("Authorization"), "Bearer test-access-token")
        self.assertEqual(json.loads(request.data), {"stk_cd": "005930"})

    def test_us_quote_uses_exchange_and_symbol_schema(self):
        self.authenticate()
        expected = {"return_code": "0", "stk_cd": "NVDA", "cur_prc": "100.2500"}
        self.opener.open.return_value = self.response(expected)
        self.assertEqual(self.client.quote("us", "nvda", "NASDAQ"), expected)
        request, _ = self.last_request()
        self.assertEqual(request.full_url, "https://mockapi.kiwoom.com/api/us/mrkcond")
        self.assertEqual(request.get_header("Api-id"), "usa20100")
        self.assertEqual(json.loads(request.data), {"stex_tp": "ND", "stk_cd": "NVDA"})

    def test_authentication_is_explicit_and_input_errors_do_not_request(self):
        with self.assertRaises(KiwoomAPIError):
            self.client.quote("KR", "005930", "KRX")
        for args in [("XX", "005930", "KRX"), ("KR", "123", "KRX"), ("US", "NVDA", "bad")]:
            with self.subTest(args=args), self.assertRaises(ValueError):
                self.client.quote(*args)
        self.opener.open.assert_not_called()

    def test_broker_error_never_echoes_message_or_credentials(self):
        self.opener.open.return_value = self.response(
            {"return_code": 8005, "return_msg": "test-app-secret test-app-key test-access-token"}
        )
        with self.assertRaises(KiwoomAPIError) as caught:
            self.client.authenticate()
        self.assertIn("return_code=8005", str(caught.exception))
        self.assert_no_secrets(str(caught.exception))
        self.assert_no_secrets(repr(self.client))

    def assert_no_secrets(self, text):
        for secret in ("test-app-key", "test-app-secret", "test-access-token"):
            self.assertNotIn(secret, text)

    def test_http_error_suppresses_sensitive_body_url_and_cause(self):
        self.opener.open.side_effect = urllib.error.HTTPError(
            "https://mockapi.kiwoom.com/?test-app-key", 401,
            "test-app-secret", {}, io.BytesIO(b"test-access-token")
        )
        try:
            self.client.authenticate()
        except KiwoomAPIError as error:
            self.assertIn("401", str(error))
            self.assert_no_secrets("".join(traceback.format_exception(error)))
        else:
            self.fail("Expected sanitized HTTP error")

    def test_network_error_is_sanitized(self):
        self.opener.open.side_effect = urllib.error.URLError("test-app-secret")
        with self.assertRaises(KiwoomAPIError) as caught:
            self.client.authenticate()
        self.assert_no_secrets(str(caught.exception))

    def test_malformed_or_unsuccessful_responses_are_rejected(self):
        for payload in [[], {}, {"return_code": False}, {"return_code": "test-app-secret"},
                        {"return_code": 0}, {"return_code": 0, "token": "test-access-token\r\nx", "token_type": "bearer"}]:
            with self.subTest(payload=payload):
                self.opener.open.return_value = self.response(payload)
                with self.assertRaises(KiwoomAPIError) as caught:
                    self.client.authenticate()
                self.assert_no_secrets(str(caught.exception))
        self.opener.open.return_value.__enter__.return_value.read.return_value = b"not-json test-app-secret"
        with self.assertRaises(KiwoomAPIError) as caught:
            self.client.authenticate()
        self.assert_no_secrets(str(caught.exception))

    def test_requests_are_throttled_including_authentication(self):
        with patch("kiwoom_autotrader.kiwoom.time.monotonic", side_effect=[10.0, 10.0, 10.2, 11.05]):
            self.authenticate()
            self.opener.open.return_value = self.response({"return_code": 0})
            self.client.quote("KR", "005930", "KRX")
        self.sleep.assert_called_once()
        self.assertAlmostEqual(self.sleep.call_args.args[0], 0.85)

    def test_redirect_handler_refuses_another_host(self):
        from kiwoom_autotrader.kiwoom import _RejectRedirects
        self.assertIsNone(_RejectRedirects().redirect_request(
            None, None, 302, "Found", {}, "https://example.com"
        ))


if __name__ == "__main__":
    unittest.main()
