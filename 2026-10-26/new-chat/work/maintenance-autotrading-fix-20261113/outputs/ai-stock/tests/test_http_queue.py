"""Queue contracts with fake transports and temporary limiter files only."""

from concurrent.futures import ThreadPoolExecutor
import io
import json
import multiprocessing
from pathlib import Path
import stat
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch
import urllib.error
import urllib.request

from kiwoom_autotrader.utils import http_queue


SECRET = "offline-private-queue-secret"


class NoWaitLimiter:
    def acquire(self, *, before_send=None):
        if before_send is not None:
            before_send()


class GateLimiter:
    def __init__(self):
        self.waiting = threading.Event()
        self.release = threading.Event()
        self.calls = 0

    def acquire(self, *, before_send=None):
        self.calls += 1
        self.waiting.set()
        if not self.release.wait(5):
            raise RuntimeError("test gate timed out")
        if before_send is not None:
            before_send()


class Response(io.BytesIO):
    def __init__(self, raw=b"payload", headers=None, status=200):
        super().__init__(raw)
        self.headers = headers if headers is not None else {"Content-Type": "application/json"}
        self.status = status
        self.read_sizes = []

    def read(self, size=-1):
        self.read_sizes.append(size)
        return super().read(size)


class FakeOpener:
    addheaders = []

    def __init__(self, action=None):
        self.action = action or (lambda request, timeout: Response())
        self.calls = []
        self.lock = threading.Lock()

    def open(self, request, *, timeout):
        with self.lock:
            self.calls.append((request, timeout, http_queue._CLOCK()))
        return self.action(request, timeout)


def request(path="/query", *, token=SECRET, body=b"body", method="POST", origin="https://api.kiwoom.com"):
    return urllib.request.Request(origin + path, data=body,
                                  headers={"Authorization": "Bearer " + token, "Api-id": "ka10001"},
                                  method=method)


def acquire_in_process(path, start, output):
    """Spawn-safe target; no HTTP transport or external state is touched."""
    limiter = http_queue.SharedRateLimit(path)
    if not start.wait(5):
        output.put(("error", "startup timeout"))
        return
    try:
        for _ in range(2):
            limiter.acquire()
            output.put(("time", http_queue._CLOCK()))
    except Exception:
        output.put(("error", "limiter failure"))


def paper_request_in_process(path, start, output):
    """Cross-process paper TR pacing with a fake HTTP transport only."""
    queue = http_queue.HTTPQueue(limiter=http_queue.SharedRateLimit(path))
    try:
        if not start.wait(5):
            raise RuntimeError("test startup timed out")
        opener = FakeOpener()
        queue.submit(opener, request(origin="https://mockapi.kiwoom.com")).result(timeout=5)
        output.put(("time", opener.calls[0][2]))
    except Exception:
        output.put(("error", "paper limiter failure"))
    finally:
        queue.close()


class HTTPQueueTests(unittest.TestCase):
    def queue(self, **options):
        queue = http_queue.HTTPQueue(limiter=options.pop("limiter", NoWaitLimiter()), **options)
        self.addCleanup(queue.close)
        return queue

    def gated(self):
        gate = GateLimiter()
        self.addCleanup(gate.release.set)
        return self.queue(limiter=gate), gate

    def test_concurrent_identical_pending_requests_keep_first_exchange_only(self):
        queue, gate = self.gated()
        first_opener = FakeOpener(lambda req, timeout: Response(b"first"))
        duplicate_opener = FakeOpener(lambda req, timeout: Response(b"duplicate"))
        first = queue.submit(first_opener, request())
        self.assertTrue(gate.waiting.wait(1))
        with ThreadPoolExecutor(max_workers=8) as pool:
            duplicates = list(pool.map(lambda _: queue.submit(duplicate_opener, request()), range(16)))
        for duplicate in duplicates:
            self.assertIs(duplicate, first)
        gate.release.set()
        self.assertEqual(first.result(timeout=2)[0], b"first")
        self.assertEqual(len(first_opener.calls), 1)
        self.assertEqual(duplicate_opener.calls, [])
        self.assertEqual(gate.calls, 1)

    def test_each_open_caller_has_its_own_cursor_and_header_copy(self):
        queue, gate = self.gated()
        headers = {"next-key": "page-2", "nested": ["original"]}
        opener = FakeOpener(lambda req, timeout: Response(b"abcdef", headers=headers))
        shared = queue.submit(opener, request())
        self.assertTrue(gate.waiting.wait(1))
        with patch.object(queue, "submit", return_value=shared), ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(queue.open, opener, request())
            second = pool.submit(queue.open, opener, request())
            gate.release.set()
            one, two = first.result(timeout=2), second.result(timeout=2)
        self.addCleanup(one.close)
        self.addCleanup(two.close)
        self.assertEqual(one.read(2), b"ab")
        self.assertEqual(two.read(), b"abcdef")
        self.assertEqual(one.read(), b"cdef")
        one.headers["nested"].append("changed")
        self.assertEqual(two.headers["nested"], ["original"])
        self.assertEqual(headers["nested"], ["original"])
        one.close()
        self.assertFalse(two.closed)
        self.assertEqual(two.getcode(), 200)

    def test_different_url_headers_body_method_timeout_or_bound_remain_distinct(self):
        queue, gate = self.gated()
        opener = FakeOpener()
        first = queue.submit(opener, request())
        self.assertTrue(gate.waiting.wait(1))
        variations = [(request("/other"), {}),
                      (request(origin="https://mockapi.kiwoom.com"), {}),
                      (request(token="other-session"), {}),
                      (request(body=b"different"), {}),
                      (request(body=None), {}),
                      (request(body=b""), {}),
                      (request(method="PUT"), {}),
                      (request(), {"timeout": 30}),
                      (request(), {"max_bytes": 1024})]
        futures = [first, *(queue.submit(opener, req, **kwargs) for req, kwargs in variations)]
        self.assertEqual(len({id(future) for future in futures}), len(futures))
        gate.release.set()
        for future in futures:
            future.result(timeout=2)
        self.assertEqual(len(opener.calls), len(futures))

    def test_snapshot_preserves_first_request_despite_later_input_mutation(self):
        queue, gate = self.gated()
        opener = FakeOpener()
        original = request()
        future = queue.submit(opener, original)
        self.assertTrue(gate.waiting.wait(1))
        original.add_header("Authorization", "changed")
        original.data = b"changed"
        gate.release.set()
        future.result(timeout=2)
        sent = opener.calls[0][0]
        self.assertEqual(sent.get_header("Authorization"), "Bearer " + SECRET)
        self.assertEqual(sent.data, b"body")

    def test_header_names_are_case_insensitive_but_values_and_defaults_are_not(self):
        queue, gate = self.gated()
        opener = FakeOpener()
        first = queue.submit(opener, request())
        self.assertTrue(gate.waiting.wait(1))
        equivalent = urllib.request.Request("https://api.kiwoom.com/query", data=b"body", method="POST",
                                            headers={"api-id": "ka10001", "authorization": "Bearer " + SECRET})
        self.assertIs(queue.submit(opener, equivalent), first)
        another_tr = request()
        another_tr.add_header("api-id", "ka10080")
        another_page = request()
        another_page.add_header("next-key", "page-2")
        with_defaults = FakeOpener()
        with_defaults.addheaders = [("X-Transport-Context", "different")]
        distinct = [queue.submit(opener, another_tr), queue.submit(opener, another_page),
                    queue.submit(with_defaults, request())]
        self.assertEqual(len({id(first), *(id(future) for future in distinct)}), 4)
        gate.release.set()
        for future in [first, *distinct]:
            future.result(timeout=2)

    def test_identical_inflight_and_completed_requests_are_not_cached(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def blocked(req, timeout):
            entered.set()
            if not release.wait(5):
                raise RuntimeError("test timeout")
            return Response()
        opener = FakeOpener(blocked)
        queue = self.queue(max_active=2)
        first = queue.submit(opener, request())
        self.assertTrue(entered.wait(1))
        second = queue.submit(opener, request())
        self.assertIsNot(first, second)
        release.set()
        first.result(timeout=2)
        second.result(timeout=2)
        third = queue.submit(opener, request())
        self.assertIsNot(third, first)
        third.result(timeout=2)
        self.assertEqual(len(opener.calls), 3)

    def test_long_poll_body_does_not_block_an_unrelated_exchange(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        class LongPoll(Response):
            def read(self, size=-1):
                entered.set()
                if not release.wait(5):
                    raise RuntimeError("test timeout")
                return super().read(size)
        long_opener = FakeOpener(lambda req, timeout: LongPoll())
        normal_opener = FakeOpener(lambda req, timeout: Response(b"normal"))
        queue = self.queue()
        long_poll = queue.submit(long_opener, request("/getUpdates", origin="https://api.telegram.org"))
        self.assertTrue(entered.wait(1))
        normal = queue.submit(normal_opener, request())
        self.assertEqual(normal.result(timeout=1)[0], b"normal")
        self.assertFalse(long_poll.done())
        release.set()
        long_poll.result(timeout=2)

    def test_failures_fan_out_without_retry_or_private_error_details(self):
        for error in (RuntimeError(SECRET), TimeoutError(SECRET),
                      urllib.error.HTTPError("https://host/" + SECRET, 429, SECRET, {}, io.BytesIO(SECRET.encode()))):
            with self.subTest(error_type=type(error).__name__):
                queue, gate = self.gated()
                def fail(req, timeout):
                    raise error
                opener = FakeOpener(fail)
                first = queue.submit(opener, request())
                self.assertTrue(gate.waiting.wait(1))
                second = queue.submit(opener, request())
                gate.release.set()
                for future in (first, second):
                    with self.assertRaises(OSError) as caught:
                        future.result(timeout=2)
                    self.assertNotIn(SECRET, str(caught.exception))
                    if isinstance(caught.exception, urllib.error.HTTPError):
                        self.assertEqual(caught.exception.code, 429)
                        self.assertEqual(caught.exception.url, "")
                        self.assertEqual(caught.exception.read(), b"")
                self.assertEqual(len(opener.calls), 1)
                self.assertEqual(gate.calls, 1)

    def test_response_read_is_bounded_and_keeps_one_oversize_sentinel_byte(self):
        response = Response(b"x" * 1000)
        opener = FakeOpener(lambda req, timeout: response)
        queue = self.queue()
        with queue.open(opener, request(), max_bytes=64) as result:
            self.assertEqual(result.read(), b"x" * 65)
        self.assertEqual(response.read_sizes, [65])
        self.assertTrue(response.closed)

    def test_invalid_http_status_does_not_survive_error_sanitization(self):
        for status in (SECRET, None, True, 99, 600):
            with self.subTest(status=status):
                queue = self.queue()
                def fail(req, timeout):
                    raise urllib.error.HTTPError("https://host/" + SECRET, status, SECRET, {}, None)
                opener = FakeOpener(fail)
                with self.assertRaises(OSError) as caught:
                    queue.submit(opener, request()).result(timeout=2)
                self.assertNotIn(SECRET, str(caught.exception))
                self.assertNotIsInstance(caught.exception, urllib.error.HTTPError)
                self.assertEqual(len(opener.calls), 1)

    def test_bad_request_parameters_never_dispatch(self):
        opener = FakeOpener()
        queue = self.queue()
        options = [{"timeout": value} for value in (0, -1, True, None, float("nan"), float("inf"))]
        options += [{"max_bytes": value} for value in (0, -1, True, None, 1.5, http_queue._MAX_BYTES + 1)]
        for kwargs in options:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                queue.submit(opener, request(), **kwargs)
        with self.assertRaises(ValueError):
            queue.submit(opener, request(origin="http://api.kiwoom.com"))
        self.assertEqual(opener.calls, [])

    def test_guard_failure_happens_before_transport_without_retry(self):
        queue = self.queue()
        opener = FakeOpener()
        guard = Mock(side_effect=RuntimeError(SECRET))
        with self.assertRaises(OSError) as caught:
            queue.submit(opener, request(), before_send=guard).result(timeout=2)
        self.assertNotIn(SECRET, str(caught.exception))
        guard.assert_called_once_with()
        self.assertEqual(opener.calls, [])

    def test_pending_duplicate_keeps_only_the_first_guard(self):
        queue, gate = self.gated()
        opener = FakeOpener()
        first_guard, duplicate_guard = Mock(), Mock(side_effect=RuntimeError(SECRET))
        first = queue.submit(opener, request(), before_send=first_guard)
        self.assertTrue(gate.waiting.wait(1))
        duplicate = queue.submit(opener, request(), before_send=duplicate_guard)
        self.assertIs(first, duplicate)
        gate.release.set()
        first.result(timeout=2)
        first_guard.assert_called_once_with()
        duplicate_guard.assert_not_called()
        self.assertEqual(len(opener.calls), 1)

    def test_pending_bound_and_close_reject_unsent_work(self):
        queue, gate = self.gated()
        queue._max_pending = 1
        opener = FakeOpener()
        first = queue.submit(opener, request())
        self.assertTrue(gate.waiting.wait(1))
        self.assertIs(queue.submit(opener, request()), first)
        with self.assertRaises(OSError):
            queue.submit(opener, request("/different"))
        queue.close()
        with self.assertRaises(OSError):
            first.result(timeout=1)
        with self.assertRaises(OSError):
            queue.submit(opener, request())
        gate.release.set()
        self.assertEqual(opener.calls, [])

    def test_failed_limiter_does_not_send_or_expose_private_state(self):
        limiter = Mock()
        limiter.acquire.side_effect = OSError(SECRET)
        queue = self.queue(limiter=limiter)
        opener = FakeOpener()
        with self.assertRaises(OSError) as caught:
            queue.submit(opener, request()).result(timeout=2)
        self.assertNotIn(SECRET, str(caught.exception))
        self.assertEqual(opener.calls, [])
        limiter.acquire.assert_called_once()
        self.assertTrue(callable(limiter.acquire.call_args.kwargs["before_send"]))


class SharedRateLimitTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "private-state" / "rate.json"

    def assert_sliding_limit(self, times):
        times = sorted(times)
        # 1.01s production window leaves margin for fsync/worker scheduling.
        for index in range(len(times) - 4):
            self.assertGreaterEqual(times[index + 4] - times[index], 0.99)

    def test_real_clock_dispatch_cap_and_private_timestamp_only_state(self):
        opener = FakeOpener()
        queue = http_queue.HTTPQueue(limiter=http_queue.SharedRateLimit(self.path))
        self.addCleanup(queue.close)
        futures = [queue.submit(opener, request(f"/{index}")) for index in range(8)]
        for future in futures:
            future.result(timeout=4)
        self.assertEqual(len(opener.calls), 8)
        self.assert_sliding_limit([row[2] for row in opener.calls])
        raw = self.path.read_text()
        self.assertNotIn(SECRET, raw)
        self.assertNotIn("api.kiwoom.com", raw)
        self.assertNotIn("body", raw)
        state = json.loads(raw)
        self.assertLessEqual(len(state), 4)
        self.assertTrue(all(type(value) in (int, float) for value in state))
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.path.parent.stat().st_mode), 0o700)

    def test_separate_processes_share_one_four_per_second_cap(self):
        context = multiprocessing.get_context("spawn")
        start, output = context.Event(), context.Queue()
        processes = [context.Process(target=acquire_in_process, args=(str(self.path), start, output)) for _ in range(4)]
        try:
            for process in processes:
                process.start()
            start.set()
            rows = [output.get(timeout=6) for _ in range(8)]
            self.assertTrue(all(kind == "time" for kind, _ in rows), rows)
            self.assert_sliding_limit([value for _, value in rows])
            for process in processes:
                process.join(timeout=3)
                self.assertEqual(process.exitcode, 0)
        finally:
            for process in processes:
                if process.is_alive():
                    process.terminate()
                    process.join(timeout=2)
            output.close()

    def test_slow_guard_does_not_consume_an_old_slot_and_allow_a_five_request_burst(self):
        opener = FakeOpener()
        queue = http_queue.HTTPQueue(limiter=http_queue.SharedRateLimit(self.path))
        self.addCleanup(queue.close)
        guard_entered = threading.Event()
        def slow_guard():
            guard_entered.set()
            time.sleep(1.05)
        first = queue.submit(opener, request("/first"), before_send=slow_guard)
        self.assertTrue(guard_entered.wait(1))
        remaining = [queue.submit(opener, request(f"/next-{index}")) for index in range(4)]
        for future in [first, *remaining]:
            future.result(timeout=4)
        self.assertEqual(len(opener.calls), 5)
        times = sorted(row[2] for row in opener.calls)
        self.assertGreaterEqual(times[4] - times[0], 1.0)

    def test_corrupt_nonfinite_oversized_state_and_symlink_are_rejected(self):
        self.path.parent.mkdir()
        for raw in ("{}", "[NaN]", "[Infinity]", "[-1]", "[true]", "[2,1]", "[1,2,3,4,5]", "x" * 5000):
            self.path.write_text(raw)
            with self.subTest(raw=raw[:20]), self.assertRaises(OSError):
                http_queue.SharedRateLimit(self.path).acquire()
            self.assertEqual(self.path.read_text(), raw)
        self.path.unlink()
        target = self.path.parent / "elsewhere.json"
        target.write_text("[]")
        self.path.symlink_to(target)
        with self.assertRaises(OSError):
            http_queue.SharedRateLimit(self.path).acquire()
        self.assertEqual(target.read_text(), "[]")


class PaperTRRateLimitTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "private" / "rate.json"

    def queue(self, limiter=None):
        result = http_queue.HTTPQueue(limiter=limiter or http_queue.SharedRateLimit(self.path))
        self.addCleanup(result.close)
        return result

    def test_same_paper_tr_shares_one_second_cap_and_only_stores_timestamps(self):
        queue, opener = self.queue(), FakeOpener()
        for index in range(3):
            req = request(f"/{index}", origin="https://mockapi.kiwoom.com", token=SECRET + str(index))
            if index == 1:
                req.add_header("api-id", "KA10001")
            queue.submit(opener, req).result(timeout=4)
        times = [item[2] for item in opener.calls]
        self.assertTrue(all(right - left >= 1.0 for left, right in zip(times, times[1:])), times)
        self.assertEqual({file.name for file in self.path.parent.iterdir()}, {"rate.json", "rate.json.paper-ka10001"})
        for file in self.path.parent.iterdir():
            raw = file.read_text()
            self.assertNotIn(SECRET, raw)
            self.assertNotIn("body", raw)
            self.assertNotIn("mockapi", raw)
            values = json.loads(raw)
            self.assertTrue(isinstance(values, list) and all(type(item) in (int, float) for item in values))
            self.assertLessEqual(len(values), 4 if file == self.path else 1)
            self.assertEqual(stat.S_IMODE(file.stat().st_mode), 0o600)

    def test_separate_processes_share_the_same_paper_tr_bucket(self):
        context = multiprocessing.get_context("spawn")
        start, output = context.Event(), context.Queue()
        processes = [context.Process(target=paper_request_in_process, args=(str(self.path), start, output)) for _ in range(2)]
        try:
            for process in processes:
                process.start()
            start.set()
            rows = [output.get(timeout=7) for _ in processes]
            self.assertTrue(all(kind == "time" for kind, _ in rows), rows)
            times = sorted(value for _, value in rows)
            self.assertGreaterEqual(times[1] - times[0], 1.0)
            for process in processes:
                process.join(timeout=3)
                self.assertEqual(process.exitcode, 0)
        finally:
            for process in processes:
                if process.is_alive():
                    process.terminate(); process.join(timeout=2)
            output.close()

    def test_guard_rechecks_after_paper_wait_before_transport(self):
        clock = [10.0]
        allowed = [True]
        limiter = http_queue.SharedRateLimit(self.path, clock=lambda: clock[0])
        def advance(delay):
            clock[0] += delay
            allowed[0] = False
        limiter._wait = Mock()
        limiter._wait.wait.side_effect = advance
        queue, opener = self.queue(limiter), FakeOpener()
        def guard():
            if not allowed[0]:
                raise RuntimeError(SECRET)
        queue.submit(opener, request("/first", origin="https://mockapi.kiwoom.com"), before_send=guard).result(timeout=2)
        with self.assertRaises(http_queue.HTTPNotSentError) as caught:
            queue.submit(opener, request("/second", origin="https://mockapi.kiwoom.com"), before_send=guard).result(timeout=2)
        self.assertNotIn(SECRET, str(caught.exception))
        self.assertEqual(len(opener.calls), 1)
        limiter._wait.wait.assert_called_once()
        self.assertGreaterEqual(limiter._wait.wait.call_args.args[0], 1.0)
        self.assertEqual(json.loads(self.path.read_text()), [10.0])
        self.assertEqual(json.loads(self.path.with_name("rate.json.paper-ka10001").read_text()), [10.0])

    def test_distinct_trs_keep_the_global_four_request_cap(self):
        queue, opener = self.queue(), FakeOpener()
        futures = []
        for index in range(5):
            req = request(f"/{index}", origin="https://mockapi.kiwoom.com")
            req.add_header("api-id", "ka1000" + str(index))
            futures.append(queue.submit(opener, req))
        for future in futures:
            future.result(timeout=4)
        times = sorted(row[2] for row in opener.calls)
        self.assertGreaterEqual(times[4] - times[0], 1.0)

    def test_real_telegram_and_websocket_handshake_do_not_create_paper_buckets(self):
        queue, opener = self.queue(), FakeOpener()
        queue.submit(opener, request()).result(timeout=2)
        queue.submit(opener, request(origin="https://api.telegram.org")).result(timeout=2)
        queue.acquire_slot()
        self.assertEqual([file.name for file in self.path.parent.iterdir()], ["rate.json"])
        self.assertEqual(len(json.loads(self.path.read_text())), 3)

    def test_missing_or_malformed_paper_tr_refuses_without_exposing_headers(self):
        queue, opener = self.queue(), FakeOpener()
        for header in (None, "../" + SECRET, " " + SECRET):
            req = urllib.request.Request("https://mockapi.kiwoom.com/api/dostk/acnt", data=b"body",
                                         headers={} if header is None else {"Api-id": header})
            with self.subTest(header=header), self.assertRaises(OSError) as caught:
                queue.submit(opener, req).result(timeout=2)
            self.assertNotIn(SECRET, str(caught.exception))
        self.assertEqual(opener.calls, [])
        self.assertFalse(self.path.exists())

    def test_auth_request_without_api_id_uses_token_tr_bucket(self):
        queue, opener = self.queue(), FakeOpener()
        req = urllib.request.Request("https://mockapi.kiwoom.com/oauth2/token", data=b"private-body")
        queue.submit(opener, req).result(timeout=2)
        self.assertEqual(len(json.loads(self.path.with_name("rate.json.paper-au10001").read_text())), 1)
        self.assertNotIn("private-body", self.path.read_text())

    def test_slow_global_fsync_cannot_expire_paper_limit_before_first_dispatch(self):
        clock = [10.0]
        limiter = http_queue.SharedRateLimit(self.path, clock=lambda: clock[0])
        limiter._wait = Mock()
        limiter._wait.wait.side_effect = lambda delay: clock.__setitem__(0, clock[0] + delay)
        queue = self.queue(limiter)
        dispatched = []
        def respond(req, timeout):
            dispatched.append(clock[0])
            return Response()
        opener = FakeOpener(respond)
        original_fsync, first = http_queue.os.fsync, [True]
        def delayed_fsync(fd):
            if first[0]:
                first[0] = False
                clock[0] += 2.0
            original_fsync(fd)
        with patch.object(http_queue.os, "fsync", side_effect=delayed_fsync):
            for index in range(2):
                queue.submit(opener, request(f"/{index}", origin="https://mockapi.kiwoom.com")).result(timeout=2)
        self.assertGreaterEqual(dispatched[1] - dispatched[0], 1.0)

    def test_close_waiting_same_tr_does_not_leak_lease_or_send_closed_ticket(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def respond(req, timeout):
            entered.set()
            if not release.wait(5):
                raise RuntimeError("test response timeout")
            return Response()
        queue, opener = self.queue(), FakeOpener(respond)
        first = queue.submit(opener, request("/first", origin="https://mockapi.kiwoom.com"))
        self.assertTrue(entered.wait(1))
        second = queue.submit(opener, request("/second", origin="https://mockapi.kiwoom.com"))
        queue.close()
        with self.assertRaises(OSError):
            second.result(timeout=2)
        release.set()
        first.result(timeout=2)
        queue._worker.join(timeout=3)
        self.assertFalse(queue._worker.is_alive())
        self.assertEqual(len(opener.calls), 1)
        lease = http_queue.SharedRateLimit(self.path).acquire_for_request(request(origin="https://mockapi.kiwoom.com"))
        lease.release(consumed=False)

    def test_http_error_consumes_paper_slot_without_retry_and_releases_lease(self):
        clock = [10.0]
        limiter = http_queue.SharedRateLimit(self.path, clock=lambda: clock[0])
        limiter._wait = Mock()
        limiter._wait.wait.side_effect = lambda delay: clock.__setitem__(0, clock[0] + delay)
        queue = self.queue(limiter)
        def fail(req, timeout):
            raise urllib.error.HTTPError("https://host/" + SECRET, 429, SECRET, {}, io.BytesIO())
        failed, succeeded = FakeOpener(fail), FakeOpener()
        with self.assertRaises(urllib.error.HTTPError):
            queue.submit(failed, request(origin="https://mockapi.kiwoom.com")).result(timeout=2)
        queue.submit(succeeded, request(origin="https://mockapi.kiwoom.com")).result(timeout=2)
        self.assertEqual(len(failed.calls), 1)
        self.assertEqual(len(succeeded.calls), 1)
        self.assertGreaterEqual(clock[0], 11.0)

    def test_worker_start_failure_releases_paper_lease_without_dispatch(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        class GatedShared(http_queue.SharedRateLimit):
            def acquire(self, *, before_send=None):
                entered.set()
                if not release.wait(4):
                    raise RuntimeError("test gate timeout")
                return super().acquire(before_send=before_send)
        queue, opener = self.queue(GatedShared(self.path)), FakeOpener()
        first = queue.submit(opener, request(origin="https://mockapi.kiwoom.com"))
        self.assertTrue(entered.wait(1))
        original_start = threading.Thread.start
        def refuse_exchange(thread):
            if thread.name == "http-exchange":
                raise RuntimeError(SECRET)
            return original_start(thread)
        with patch.object(threading.Thread, "start", refuse_exchange):
            release.set()
            with self.assertRaises(OSError) as caught:
                first.result(timeout=2)
        self.assertNotIn(SECRET, str(caught.exception))
        self.assertEqual(opener.calls, [])
        queue.submit(opener, request(origin="https://mockapi.kiwoom.com")).result(timeout=2)
        self.assertEqual(len(opener.calls), 1)


if __name__ == "__main__":
    unittest.main()
