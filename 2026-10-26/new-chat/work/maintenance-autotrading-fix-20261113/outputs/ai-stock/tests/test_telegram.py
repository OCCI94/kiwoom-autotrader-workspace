"""Offline Telegram transport and privacy tests; no real token or API calls."""

import io
import json
import traceback
import unittest
import urllib.error
from unittest.mock import MagicMock, patch

from kiwoom_autotrader.telegram import TelegramAPIError, TelegramClient, _RejectRedirects


_TOKEN = "123456789:unit_test_token_abcdefghijklmnopqrstuvwxyz"
_BOT = {"id": 123456789, "is_bot": True, "username": "unit_test_bot"}
_CHAT = {"id": 4321, "type": "private"}


class TelegramClientTests(unittest.TestCase):
    def setUp(self):
        opener_patch = patch("kiwoom_autotrader.telegram.urllib.request.build_opener")
        self.addCleanup(opener_patch.stop)
        self.build_opener = opener_patch.start()
        self.opener = self.build_opener.return_value
        self.client = TelegramClient(_TOKEN)

    def response(self, payload):
        response = MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = json.dumps(payload).encode("utf-8")
        self.opener.open.return_value = response
        return response

    def last_request(self):
        args, kwargs = self.opener.open.call_args
        return args[0], kwargs

    def assert_safe_error(self, call):
        try:
            call()
        except TelegramAPIError as error:
            rendered = "".join(traceback.format_exception(error))
            for secret in (_TOKEN, "api.telegram.org", "remote-private-text"):
                self.assertNotIn(secret, str(error))
                self.assertNotIn(secret, repr(error))
                self.assertNotIn(secret, rendered)
            return error
        self.fail("Expected sanitized TelegramAPIError")

    def test_construction_and_repr_never_send_or_expose_token(self):
        self.opener.open.assert_not_called()
        self.assertNotIn(_TOKEN, repr(self.client))
        self.assertNotIn(_TOKEN, str(self.client))
        self.assertIsInstance(self.build_opener.call_args.args[0], _RejectRedirects)

    def test_invalid_token_cannot_change_origin_path_or_headers(self):
        for token in (None, "", "abc:abc", "0:" + "a" * 35, "1:short",
                      _TOKEN + "/sendMessage", _TOKEN + "?x=1", _TOKEN + "\n",
                      " " + _TOKEN, _TOKEN + "@evil.example", _TOKEN + "%2f"):
            with self.subTest(token=token), self.assertRaises(ValueError) as caught:
                TelegramClient(token)
            self.assertNotIn(_TOKEN, str(caught.exception))
        self.opener.open.assert_not_called()

    def test_get_me_uses_fixed_https_origin_post_json_and_bounded_read(self):
        response = self.response({"ok": True, "result": _BOT})
        self.assertEqual(self.client.get_me(), _BOT)
        request, kwargs = self.last_request()
        self.assertEqual(request.full_url, f"https://api.telegram.org/bot{_TOKEN}/getMe")
        self.assertEqual(request.method, "POST")
        self.assertEqual(json.loads(request.data), {})
        self.assertEqual(request.get_header("Content-type"), "application/json;charset=UTF-8")
        self.assertEqual(kwargs, {"timeout": 15})
        response.read.assert_called_once_with(1024 * 1024 + 1)

    def test_get_webhook_info_is_read_only_and_checks_required_fields(self):
        webhook = {"url": "", "has_custom_certificate": False, "pending_update_count": 0}
        self.response({"ok": True, "result": webhook})
        self.assertEqual(self.client.get_webhook_info(), webhook)
        request, _ = self.last_request()
        self.assertTrue(request.full_url.endswith("/getWebhookInfo"))
        self.assertEqual(json.loads(request.data), {})
        for result in ({}, {**webhook, "url": None},
                       {**webhook, "pending_update_count": True},
                       {**webhook, "pending_update_count": -1},
                       {**webhook, "has_custom_certificate": "false"}):
            self.response({"ok": True, "result": result})
            self.assert_safe_error(self.client.get_webhook_info)

    def test_get_updates_preserves_queue_and_subscription_without_printing(self):
        updates = [{"update_id": 12, "message": {"text": "remote-private-text"}}]
        self.response({"ok": True, "result": updates})
        with patch("builtins.print") as printer:
            self.assertEqual(self.client.get_updates(), updates)
            self.client.get_updates(timeout=25)
        printer.assert_not_called()
        for call, expected in zip(self.opener.open.call_args_list, (0, 25)):
            request = call.args[0]
            self.assertTrue(request.full_url.endswith("/getUpdates"))
            self.assertEqual(json.loads(request.data), {"limit": 100, "timeout": expected})
            self.assertLessEqual(call.kwargs["timeout"], 30)

    def test_get_updates_rejects_invalid_timeout_and_result_schema(self):
        for timeout in (True, -1, 26, 1.5, "0"):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                self.client.get_updates(timeout)
        self.opener.open.assert_not_called()
        for result in ({}, [None], [{}], [{"update_id": True}],
                       [{"update_id": -1}], [{"update_id": 1}] * 101):
            self.response({"ok": True, "result": result})
            self.assert_safe_error(self.client.get_updates)

    def test_get_updates_only_acknowledges_explicit_nonnegative_offset(self):
        self.response({"ok": True, "result": []})
        for offset in (None, 0, 12345):
            self.client.get_updates(timeout=10, offset=offset)
            request, _ = self.last_request()
            expected = {"limit": 100, "timeout": 10}
            if offset is not None:
                expected["offset"] = offset
            self.assertEqual(json.loads(request.data), expected)

    def test_get_updates_rejects_invalid_offset_without_network(self):
        for offset in (True, False, -1, -12345, "0", 0.0, 1.5, [], {}):
            with self.subTest(offset=offset), self.assertRaises(ValueError):
                self.client.get_updates(offset=offset)
        self.opener.open.assert_not_called()

    def test_send_plain_text_to_private_positive_id_with_no_preview(self):
        result = {"message_id": 72, "chat": _CHAT, "text": "모의투자 <확인>"}
        self.response({"ok": True, "result": result})
        self.assertEqual(self.client.send_message(4321, result["text"]), result)
        request, _ = self.last_request()
        self.assertTrue(request.full_url.endswith("/sendMessage"))
        self.assertEqual(json.loads(request.data), {
            "chat_id": 4321, "text": "모의투자 <확인>",
            "link_preview_options": {"is_disabled": True},
        })
        self.opener.open.assert_called_once()

    def test_send_rejects_groups_channel_names_bad_text_without_network(self):
        for chat_id in (True, 0, -123, "4321", "@channel", 1.5, 1 << 52):
            with self.subTest(chat_id=chat_id), self.assertRaises(ValueError):
                self.client.send_message(chat_id, "x")
        for text in (None, "", "x" * 4097, 123):
            with self.subTest(text=text), self.assertRaises(ValueError):
                self.client.send_message(4321, text)
        self.opener.open.assert_not_called()

    def test_send_response_must_identify_same_private_chat_and_message(self):
        for result in ({}, {"message_id": True, "chat": _CHAT},
                       {"message_id": -1, "chat": _CHAT},
                       {"message_id": 72, "chat": {"id": 4321, "type": "group"}},
                       {"message_id": 72, "chat": {"id": 9999, "type": "private"}}):
            self.response({"ok": True, "result": result})
            self.assert_safe_error(lambda: self.client.send_message(4321, "x"))

    def test_bot_identity_validation_prevents_unsafe_links(self):
        for result in ({}, {**_BOT, "id": True}, {**_BOT, "id": 0}, {**_BOT, "id": 9876},
                       {**_BOT, "is_bot": 1}, {**_BOT, "is_bot": False},
                       {**_BOT, "username": None}, {**_BOT, "username": "name/evil"},
                       {**_BOT, "username": "bot\n"}, {**_BOT, "username": "four"},
                       {**_BOT, "username": "x" * 33}):
            self.response({"ok": True, "result": result})
            self.assert_safe_error(self.client.get_me)

    def test_http_error_exposes_only_numeric_code_never_url_body_or_reason(self):
        for code in (301, 302, 307, 308, 401, 404, 409, 429, 500):
            self.opener.open.side_effect = urllib.error.HTTPError(
                f"https://api.telegram.org/bot{_TOKEN}/getMe", code,
                _TOKEN, {}, io.BytesIO(b"remote-private-text"),
            )
            error = self.assert_safe_error(self.client.get_me)
            self.assertIn(f"code={code}", str(error))

    def test_transport_failure_is_sanitized_and_send_never_retried(self):
        for error in (urllib.error.URLError(_TOKEN), TimeoutError(_TOKEN), OSError(_TOKEN)):
            self.opener.open.reset_mock()
            self.opener.open.side_effect = error
            self.assert_safe_error(lambda: self.client.send_message(4321, "x"))
            self.opener.open.assert_called_once()

    def test_api_error_exposes_only_valid_numeric_code(self):
        for code in (401, 404, 409, 429, True, _TOKEN, 999999999999):
            self.response({"ok": False, "error_code": code,
                           "description": f"remote-private-text {_TOKEN}"})
            error = self.assert_safe_error(self.client.get_me)
            if type(code) is int and 100 <= code <= 599:
                self.assertIn(f"code={code}", str(error))
            else:
                self.assertNotIn("code=", str(error))

    def test_malformed_missing_or_ambiguous_success_responses_are_rejected(self):
        for payload in ([], {}, {"ok": 1, "result": _BOT}, {"ok": True},
                        {"ok": True, "result": None}, {"ok": False},
                        {"ok": True, "result": _BOT, "error_code": 401}):
            self.response(payload)
            self.assert_safe_error(self.client.get_me)
        for raw in (b"not-json remote-private-text", b"\xff", b"[" * 2000,
                    b"x" * (1024 * 1024 + 1)):
            response = self.response({})
            response.read.return_value = raw
            self.assert_safe_error(self.client.get_me)

    def test_redirects_and_unlisted_methods_are_rejected(self):
        self.assertIsNone(_RejectRedirects().redirect_request(
            None, None, 302, "Found", {}, "https://example.com"
        ))
        for method in ("deleteWebhook", "setWebhook", "getMe/../../x"):
            with self.assertRaises(TelegramAPIError):
                self.client._post(method, {})
        self.opener.open.assert_not_called()


if __name__ == "__main__":
    unittest.main()
