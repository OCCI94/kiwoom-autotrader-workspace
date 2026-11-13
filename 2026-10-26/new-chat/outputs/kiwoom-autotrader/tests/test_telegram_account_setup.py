"""Local-only setup tests with fake authentication and fake service management."""

import asyncio
import http.client
from http.server import ThreadingHTTPServer
import io
import json
from pathlib import Path
import threading
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from kiwoom_autotrader import telegram_account_setup as setup
from kiwoom_autotrader.telegram_account import AccountConnectionError


SECRET = "private-value-must-not-appear"
CHANNEL = {"channel_id": 1234, "access_hash": 9876543210,
           "title": "<script>untrusted channel</script>", "username": "public_channel"}


class SetupWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.storage = self.patch("kiwoom_autotrader.telegram_account_credentials.load_account_config", return_value=None)
        self.threads = self.patch("kiwoom_autotrader.telegram_account_setup.threading.Thread")
        self.workflow = setup.SetupWorkflow()

    def patch(self, target, **kwargs):
        patcher = patch(target, **kwargs)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def credentials(self):
        return {"action": "credentials", "api_id": "123456", "api_hash": "abcdef0123456789" * 2,
                "phone": "+821012345678"}

    def test_state_excludes_credentials_ids_access_hashes_and_verification_input(self):
        self.workflow.config = SimpleNamespace(api_hash=SECRET, session=SECRET, user_id=12345)
        self.workflow.channels = [CHANNEL]
        self.workflow.selected = {CHANNEL["channel_id"]}
        self.workflow.answers.put(SECRET)
        self.workflow.step = "channels"
        state = self.workflow.state()
        self.assertEqual(state["channels"], [{"title": CHANNEL["title"], "username": "public_channel", "selected": True}])
        text = json.dumps(state)
        for secret in (SECRET, str(CHANNEL["access_hash"]), str(CHANNEL["channel_id"]), "12345"):
            self.assertNotIn(secret, text)

    def test_duplicate_credentials_do_not_start_second_login(self):
        self.workflow.submit(self.credentials())
        self.assertEqual(self.workflow.step, "working")
        with self.assertRaises(ValueError):
            self.workflow.submit(self.credentials())
        self.threads.assert_called_once()
        self.threads.return_value.start.assert_called_once()

    def test_malformed_credentials_are_not_echoed_or_submitted(self):
        for key, value in (("api_id", "not-a-number"), ("api_hash", SECRET),
                           ("phone", SECRET)):
            body = dict(self.credentials(), **{key: value})
            self.workflow.submit(body)
            self.assertEqual(self.workflow.step, "credentials")
            self.assertNotIn(value, self.workflow.message)
        self.threads.assert_not_called()

    def test_new_login_discards_stale_answer_from_previous_attempt(self):
        self.workflow.answers.put(SECRET)
        self.workflow.step = "error"
        self.workflow.submit({"action": "retry"})
        self.workflow.submit(self.credentials())
        self.assertTrue(self.workflow.answers.empty())

    def test_input_is_accepted_only_for_current_auth_step(self):
        with self.assertRaises(ValueError):
            self.workflow.submit({"action": "code", "value": "54321"})
        self.workflow.step = "code"
        self.workflow.submit({"action": "code", "value": "54321"})
        self.assertEqual(self.workflow.answers.get_nowait(), "54321")
        self.assertEqual(self.workflow.step, "working")
        with self.assertRaises(ValueError):
            self.workflow.submit({"action": "password", "value": SECRET})

    def test_selection_rejects_bool_out_of_range_and_duplicate_indices(self):
        self.workflow.step = "channels"
        self.workflow.channels = [CHANNEL]
        for indices in ([], [True], [1], [-1], [0, 0]):
            self.workflow.submit({"action": "select", "indices": indices})
            self.assertEqual(self.workflow.step, "channels")
        self.threads.assert_not_called()
        self.workflow.submit({"action": "select", "indices": [0]})
        self.assertEqual(self.threads.call_args.kwargs["args"], ([CHANNEL],))
        self.assertEqual(self.workflow.step, "working")

    def test_authentication_worker_sanitizes_unexpected_errors(self):
        self.patch("kiwoom_autotrader.telegram_account.connect_account", new_callable=AsyncMock,
                   side_effect=RuntimeError(SECRET))
        self.workflow._authenticate((123456, "abcdef0123456789" * 2, "+821012345678"))
        self.assertEqual(self.workflow.step, "error")
        self.assertNotIn(SECRET, self.workflow.message)

    def test_authentication_worker_preserves_safe_actionable_failure(self):
        self.patch("kiwoom_autotrader.telegram_account.connect_account", new_callable=AsyncMock,
                   side_effect=AccountConnectionError("텔레그램 요청 제한으로 120초 후 다시 시도해야 합니다."))
        self.workflow._authenticate((123456, "abcdef0123456789" * 2, "+821012345678"))
        self.assertIn("120초", self.workflow.message)

    def test_prepare_lists_channels_and_preserves_saved_selection(self):
        config = SimpleNamespace(user_id=123456)
        list_channels = self.patch("kiwoom_autotrader.telegram_collector.list_account_channels",
                                   new_callable=AsyncMock, return_value=[CHANNEL])
        store = self.patch("kiwoom_autotrader.channel_store.ChannelStore")
        store.return_value.__enter__.return_value.channels.return_value = [CHANNEL]
        self.patch("kiwoom_autotrader.telegram_account_service.account_paths", return_value=SimpleNamespace(state=Path("/test-only")))
        asyncio.run(self.workflow._prepare(config))
        list_channels.assert_awaited_once_with(config)
        self.assertEqual(self.workflow.selected, {1234})
        self.assertTrue(self.workflow.has_saved)
        self.assertEqual(self.workflow.step, "channels")

    def test_activation_failure_does_not_expose_private_exception(self):
        self.workflow.config = SimpleNamespace(user_id=123456)
        self.patch("kiwoom_autotrader.telegram_account_service.account_paths", return_value=SimpleNamespace(state=Path("/test-only")))
        stop = self.patch("kiwoom_autotrader.telegram_account_service.stop_collector_service")
        store = self.patch("kiwoom_autotrader.channel_store.ChannelStore")
        install = self.patch("kiwoom_autotrader.telegram_account_service.install_collector_service", side_effect=RuntimeError(SECRET))
        self.workflow._activate([CHANNEL])
        stop.assert_called_once()
        store.return_value.__enter__.return_value.set_selected.assert_called_once_with([CHANNEL])
        install.assert_called_once()
        self.assertEqual(self.workflow.step, "error")
        self.assertNotIn(SECRET, self.workflow.message)

    def test_qr_credentials_start_without_phone_and_select_qr_authentication(self):
        body = self.credentials()
        body["action"] = "qr"
        del body["phone"]
        self.workflow.submit(body)
        self.assertEqual(self.workflow.step, "working")
        self.threads.assert_called_once()
        self.assertEqual(self.threads.call_args.kwargs["args"], (
            (123456, "abcdef0123456789" * 2, None), True,
        ))
        self.assertNotIn(body["api_hash"], json.dumps(self.workflow.state()))

    def test_qr_bytes_are_ephemeral_and_state_never_exposes_login_token(self):
        token_url = "tg://login?token=fixture_private_auth_token"
        qrcode = ModuleType("qrcode")
        qrcode_image = ModuleType("qrcode.image")
        qrcode_svg = ModuleType("qrcode.image.svg")
        qrcode_svg.SvgPathFillImage = object()
        fake_image = SimpleNamespace(save=lambda stream: stream.write(b"<svg>temporary-qr</svg>"))
        qrcode.make = MagicMock(return_value=fake_image)
        with patch.dict("sys.modules", {
            "qrcode": qrcode, "qrcode.image": qrcode_image, "qrcode.image.svg": qrcode_svg,
        }):
            for destination in ("password", "error"):
                self.workflow._show_qr(token_url, 123456)
                self.assertEqual(self.workflow.step, "qr")
                self.assertEqual(self.workflow.qr_image(), b"<svg>temporary-qr</svg>")
                public_state = json.dumps(self.workflow.state())
                self.assertNotIn(token_url, public_state)
                self.assertNotIn("temporary-qr", public_state)
                self.workflow._set(destination, "safe instruction")
                self.assertEqual(self.workflow.qr_image(), b"")
                self.assertEqual(self.workflow.qr_svg, b"")
        self.assertEqual(self.workflow.qr_version, 2)


class SetupQRHTTPTests(unittest.TestCase):
    """Exercise QR responses in memory without creating a socket or server."""

    def setUp(self):
        self.workflow = MagicMock()
        self.workflow.qr_image.return_value = b"<svg>ephemeral-image</svg>"
        self.token = "fixture-unpredictable-capability"
        self.origin = "http://127.0.0.1:52999"
        self.handler_type = setup.make_handler(self.workflow, self.token, self.origin)

    def get(self, path, host="127.0.0.1:52999"):
        handler = object.__new__(self.handler_type)
        handler.path = path
        handler.headers = {"Host": host}
        handler.wfile = io.BytesIO()
        response = {"headers": {}}
        handler.send_response = lambda status: response.update(status=status)
        handler.send_header = lambda key, value: response["headers"].update({key: value})
        handler.end_headers = lambda: None
        handler.do_GET()
        response["body"] = handler.wfile.getvalue()
        return response

    def test_qr_get_is_svg_no_store_and_capability_scoped(self):
        response = self.get("/" + self.token + "/qr?v=7")
        self.assertEqual(response["status"], 200)
        self.assertEqual(response["body"], b"<svg>ephemeral-image</svg>")
        self.assertEqual(response["headers"]["Content-Type"], "image/svg+xml")
        self.assertEqual(response["headers"]["Cache-Control"], "no-store")
        self.assertEqual(response["headers"]["Referrer-Policy"], "no-referrer")
        self.assertEqual(response["headers"]["X-Content-Type-Options"], "nosniff")
        self.workflow.qr_image.reset_mock()
        self.assertEqual(self.get("/other-capability/qr")["status"], 404)
        self.assertEqual(self.get("/" + self.token + "/qr", "attacker.invalid")["status"], 403)
        self.workflow.qr_image.assert_not_called()

    def test_qr_get_returns_no_image_after_authentication_step_changes(self):
        self.workflow.qr_image.return_value = b""
        response = self.get("/" + self.token + "/qr")
        self.assertEqual(response["status"], 404)
        self.assertEqual(response["body"], b"")
        self.assertEqual(response["headers"]["Cache-Control"], "no-store")


class SetupHTTPTests(unittest.TestCase):
    def setUp(self):
        self.workflow = MagicMock()
        self.workflow.state.return_value = {"step": "credentials", "message": "ready", "has_saved": False, "channels": []}
        self.token = "test-random-unpredictable-token"
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), setup.BaseHTTPRequestHandler)
        self.origin = f"http://127.0.0.1:{self.server.server_port}"
        self.server.RequestHandlerClass = setup.make_handler(self.workflow, self.token, self.origin)
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()
        self.addCleanup(self.close_server)

    def close_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.worker.join(timeout=2)

    def request(self, method="POST", *, path=None, data=b'{"action":"saved"}', headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        selected = {"Host": self.origin.removeprefix("http://"), "Origin": self.origin,
                    "Content-Type": "application/json", "X-Setup-Token": self.token}
        if headers:
            selected.update(headers)
        try:
            connection.request(method, path or "/" + self.token, body=data if method == "POST" else None,
                               headers=selected)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def test_valid_request_and_state_use_no_store_and_no_referrer(self):
        status, headers, body = self.request()
        self.assertEqual(status, 200)
        self.workflow.submit.assert_called_once_with({"action": "saved"})
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(headers["Referrer-Policy"], "no-referrer")
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        status, _, body = self.request("GET", path="/" + self.token + "/state")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["step"], "credentials")

    def test_csrf_wrong_host_origin_token_and_content_type_are_rejected(self):
        for headers in ({"Host": "attacker.invalid"}, {"Origin": "https://attacker.invalid"},
                        {"Origin": "null"}, {"X-Setup-Token": "wrong"},
                        {"Content-Type": "text/plain"}):
            status, _, _ = self.request(headers=headers)
            self.assertEqual(status, 403)
        self.assertEqual(self.request(path="/wrong")[0], 403)
        self.workflow.submit.assert_not_called()

    def test_html_and_state_require_capability_path_and_exact_host(self):
        self.assertEqual(self.request("GET", path="/")[0], 404)
        self.assertEqual(self.request("GET", headers={"Host": "attacker.invalid"})[0], 403)
        status, _, body = self.request("GET")
        self.assertEqual(status, 200)
        self.assertIn(b"createTextNode", body)
        self.assertNotIn(b"innerHTML", body)

    def test_malformed_or_oversized_json_is_rejected_without_workflow(self):
        for body in (b"{", b"\xff", b"x" * 16385):
            self.assertEqual(self.request(data=body)[0], 400)
        self.workflow.submit.assert_not_called()

    def test_deeply_nested_json_is_handled_as_bad_input(self):
        # Runtime recursion limits vary, so simulate the decoder's documented
        # failure rather than depending on one interpreter's recursion depth.
        with patch.object(setup.json, "loads", side_effect=RecursionError("nested input")):
            self.assertEqual(self.request(data=b"[[]]")[0], 400)
        self.workflow.submit.assert_not_called()


if __name__ == "__main__":
    unittest.main()
