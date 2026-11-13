"""Offline IP discovery/diagnosis tests. External HTTP is always replaced."""

from concurrent.futures import ThreadPoolExecutor
import io
import threading
import time
import unittest
from unittest.mock import Mock, patch
import urllib.error

from kiwoom_autotrader.telegram.commands import ip as command
from kiwoom_autotrader.utils import public_ip


SECRET = "private-ip-test-token"


class Response(io.BytesIO):
    def __init__(self, data=b"8.8.8.8\n", status=200):
        super().__init__(data)
        self.status = status
        self.read_sizes = []

    def read(self, size=-1):
        self.read_sizes.append(size)
        return super().read(size)


class PublicIPLookupTests(unittest.TestCase):
    def setUp(self):
        self.now = 100.0
        self.lookup = public_ip.PublicIPLookup(clock=lambda: self.now)
        self.queued = self.patch(public_ip, "queued_open")
        self.opener = self.patch(public_ip.urllib.request, "build_opener")
        self.network = self.patch_path("socket.socket.connect", side_effect=AssertionError("Live network forbidden"))
        self.queued.side_effect = self.good_response

    def patch(self, owner, name, **kwargs):
        patcher = patch.object(owner, name, **kwargs)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def patch_path(self, path, **kwargs):
        patcher = patch(path, **kwargs)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def tearDown(self):
        self.network.assert_not_called()

    def good_response(self, opener, request, **options):
        options["before_send"]()
        return Response()

    def test_lazy_construction_and_successful_discovery_use_bounded_queued_https_without_credentials(self):
        self.queued.assert_not_called()
        self.assertEqual(self.lookup.get(), "8.8.8.8")
        self.queued.assert_called_once()
        opener, request = self.queued.call_args.args
        self.assertIs(opener, self.opener.return_value)
        self.assertEqual(request.full_url, "https://api64.ipify.org")
        self.assertEqual(request.get_method(), "GET")
        self.assertIsNone(request.data)
        self.assertEqual(request.header_items(), [("Accept", "text/plain")])
        options = self.queued.call_args.kwargs
        self.assertEqual(options["timeout"], 2.0)
        self.assertEqual(options["max_bytes"], 64)
        self.assertTrue(callable(options["before_send"]))
        self.assertIsInstance(self.opener.call_args.args[0], public_ip._RejectRedirects)
        self.assertIsNone(public_ip._RejectRedirects().redirect_request(None, None, 302, "", {}, "https://invalid/"))

    def test_success_cache_expires_at_45_seconds_and_forced_refresh_bypasses_it(self):
        self.assertEqual(self.lookup.get(), "8.8.8.8")
        self.now += 44.9
        self.assertEqual(self.lookup.get(), "8.8.8.8")
        self.assertEqual(self.queued.call_count, 1)
        self.lookup.get(force_refresh=True)
        self.assertEqual(self.queued.call_count, 2)
        self.now += 45
        self.lookup.get()
        self.assertEqual(self.queued.call_count, 3)

    def test_ipv6_is_validated_and_canonicalized(self):
        self.queued.side_effect = lambda *args, **kwargs: Response(b"2606:4700:4700:0:0:0:0:1111\n")
        self.assertEqual(self.lookup.get(), "2606:4700:4700::1111")

    def test_invalid_private_non_ip_and_oversized_responses_are_rejected(self):
        values = (b"127.0.0.1", b"10.0.0.1", b"192.168.1.1", b"169.254.1.2", b"100.64.1.2",
                  b"0.0.0.0", b"224.0.0.1", b"::1", b"fc00::1", b"fe80::1", b"ff02::1",
                  b"fe80::1%en0", b"2001:db8::1", b"https://8.8.8.8", b"8.8.8.8:443",
                  b"8.8.8.8\n1.1.1.1", b'{"ip":"8.8.8.8"}', b"", b"x" * 1000, b"\xff")
        for raw in values:
            with self.subTest(raw=raw[:40]):
                self.queued.reset_mock()
                self.queued.side_effect = lambda *args, **kwargs: Response(raw)
                with self.assertRaises(public_ip.PublicIPError) as caught:
                    self.lookup.get(force_refresh=True)
                self.assertNotIn(SECRET, str(caught.exception))
                self.assertEqual(self.queued.call_count, 2)

    def test_first_provider_failure_uses_only_the_fixed_fallback(self):
        self.queued.side_effect = [TimeoutError(SECRET), Response(b"1.1.1.1")]
        self.assertEqual(self.lookup.get(force_refresh=True), "1.1.1.1")
        self.assertEqual([call.args[1].full_url for call in self.queued.call_args_list],
                         ["https://api64.ipify.org", "https://checkip.amazonaws.com"])

    def test_transport_failures_are_sanitized_never_retried_and_do_not_reuse_old_success(self):
        self.lookup.get()
        self.queued.reset_mock()
        self.queued.side_effect = urllib.error.HTTPError("https://invalid/" + SECRET, 503, SECRET, {}, None)
        for _ in range(2):
            with self.assertRaises(public_ip.PublicIPError) as caught:
                self.lookup.get(force_refresh=True)
            self.assertNotIn(SECRET, str(caught.exception))
        self.assertEqual(self.queued.call_count, 4)  # once per provider for each explicit lookup

    def test_provider_response_read_is_bounded_even_when_queue_is_mocked(self):
        responses = []
        def response(*args, **kwargs):
            result = Response(b"x" * 1000)
            responses.append(result)
            return result
        self.queued.side_effect = response
        with self.assertRaises(public_ip.PublicIPError):
            self.lookup.get(force_refresh=True)
        self.assertEqual([item.read_sizes for item in responses], [[65], [65]])

    def test_simultaneous_refreshes_share_one_active_lookup(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def response(*args, **kwargs):
            entered.set()
            if not release.wait(2):
                raise TimeoutError
            return Response()
        self.queued.side_effect = response
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(self.lookup.get, force_refresh=True)
            self.assertTrue(entered.wait(1))
            second = pool.submit(self.lookup.get, force_refresh=True)
            # Ensure both callers have an active task before it can complete.
            time.sleep(0.01)
            release.set()
            self.assertEqual(first.result(timeout=2), "8.8.8.8")
            self.assertEqual(second.result(timeout=2), "8.8.8.8")
        self.queued.assert_called_once()

    def test_queue_congestion_has_a_bounded_reply_and_expired_lookup_cannot_send_late(self):
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        self.addCleanup(release.set)
        guard_failed = []
        actual_lookup = public_ip.PublicIPLookup()
        def blocked(opener, request, **options):
            entered.set()
            release.wait(2)
            try:
                options["before_send"]()
            except public_ip.PublicIPError:
                guard_failed.append(True)
                raise
            finally:
                finished.set()
            raise AssertionError("expired request reached transport")
        self.queued.side_effect = blocked
        with patch.object(public_ip, "_LOOKUP_TIMEOUT", 0.03):
            started = time.monotonic()
            with self.assertRaises(public_ip.PublicIPError):
                actual_lookup.get(force_refresh=True)
            self.assertLess(time.monotonic() - started, 0.3)
            self.assertTrue(entered.is_set())
            with self.assertRaises(public_ip.PublicIPError):
                actual_lookup.get(force_refresh=True)
            self.queued.assert_called_once()  # no growing backlog of lookup workers
            release.set()
            self.assertTrue(finished.wait(1))
        self.assertEqual(guard_failed, [True])


class PublicIPCommandAndErrorTests(unittest.TestCase):
    def test_ip_command_forces_fresh_lookup_and_labels_ipv4_or_ipv6(self):
        for address, version in (("8.8.8.8", 4), ("2606:4700:4700::1111", 6)):
            with self.subTest(address=address), patch.object(command, "get_public_ip", return_value=address) as lookup:
                reply = command.handle_ip()
                lookup.assert_called_once_with(force_refresh=True)
                self.assertEqual(len(reply), 1)
                self.assertIn(f"IPv{version}", reply[0])
                self.assertIn(address, reply[0])

    def test_command_failures_never_expose_raw_diagnostics(self):
        for error in (RuntimeError(SECRET), public_ip.PublicIPError(SECRET)):
            with patch.object(command, "get_public_ip", side_effect=error):
                reply = command.handle_ip()[0]
            self.assertNotIn(SECRET, reply)
            self.assertIn("확인하지 못했습니다", reply)

    def test_only_workbook_confirmed_ip_codes_trigger_fresh_lookup(self):
        for code in (8010, "8010", 8050, "8050"):
            with self.subTest(code=code), patch.object(public_ip, "get_public_ip", return_value="1.1.1.1") as lookup:
                reply = public_ip.annotate_ip_error(code, SECRET)
                lookup.assert_called_once_with(force_refresh=True)
                self.assertIn(str(code), reply)
                self.assertIn("현재 외부 공인 IP (IPv4): 1.1.1.1", reply)
                self.assertNotIn(SECRET, reply)
                self.assertIn("자동 재시도하지 않았습니다", reply)
                self.assertIn("다시 로그인" if str(code) == "8010" else "API 사용신청", reply)

    def test_network_http_auth_device_and_unknown_errors_do_not_trigger_ip_lookup(self):
        with patch.object(public_ip, "get_public_ip") as lookup:
            for code in (None, True, 401, 403, 429, 500, 8005, 8040, 8103, 8052, 0, -8010,
                         "08010", " 8010 ", 8010.0, "network error", {"code": 8010}):
                with self.subTest(code=code):
                    self.assertEqual(public_ip.annotate_ip_error(code, "locally composed safe failure"),
                                     "locally composed safe failure")
            lookup.assert_not_called()

    def test_failed_ip_lookup_keeps_precise_broker_diagnosis_without_raw_errors(self):
        with patch.object(public_ip, "get_public_ip", side_effect=RuntimeError(SECRET)):
            reply = public_ip.annotate_ip_error(8010, SECRET)
        self.assertIn("토큰 발급 IP와 요청 IP가 다릅니다", reply)
        self.assertIn("공인 IP를 확인하지 못했습니다", reply)
        self.assertNotIn(SECRET, reply)


if __name__ == "__main__":
    unittest.main()
