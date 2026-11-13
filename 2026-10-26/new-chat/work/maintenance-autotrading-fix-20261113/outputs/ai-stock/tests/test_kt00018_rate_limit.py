"""Offline regression tests for the revised kt00018 dispatch limit."""
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import io
import json
import multiprocessing
import os
import tempfile
import time
import unittest
from unittest.mock import Mock
import urllib.error
from urllib.request import Request

from kiwoom_autotrader.utils.http_queue import SharedRateLimit, HTTPQueue, HTTPNotSentError, _CLOCK


def req(host, api_id):
    return Request('https://' + host + '/api/dostk/acnt', data=b'{}',
                   headers={'Api-Id': api_id, 'Authorization': 'Bearer secret-fixture'})


def reserve_in_process(path, go, output):
    limiter = SharedRateLimit(Path(path))
    try:
        if not go.wait(8):
            raise RuntimeError('startup timed out')
        lease = limiter.acquire_for_request(req('api.kiwoom.com', 'KT00018'))
        acquired = _CLOCK()
        prior = lease.path.read_text()
        lease.release()
        output.put(('at', acquired, _CLOCK(), prior))
    except Exception as exc:
        output.put(('error', type(exc).__name__))


class Response(io.BytesIO):
    def __init__(self):
        super().__init__(b'{}')
        self.headers = {}
        self.status = 200


class Opener:
    addheaders = []
    def __init__(self, action=None):
        self.action = action
        self.calls = []

    def open(self, request, timeout):
        self.calls.append(_CLOCK())
        if self.action:
            return self.action(request, timeout)
        return Response()


class AccountBalanceTrLimitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'rate.json'

    def test_real_kt00018_has_separate_one_second_tr_lease(self):
        clock = [10.0]
        limiter = SharedRateLimit(self.path, clock=lambda: clock[0])
        limiter._wait = Mock()
        limiter._wait.wait.side_effect = lambda delay: clock.__setitem__(0, clock[0] + delay)
        lease = limiter.acquire_for_request(req('api.kiwoom.com', 'KT00018'))
        self.assertIsNotNone(lease)
        lease.release()
        again = limiter.acquire_for_request(req('api.kiwoom.com', 'kt00018'))
        self.assertGreaterEqual(clock[0], 11.05)
        again.release()
        state = self.path.with_name('rate.json.real-kt00018').read_text()
        self.assertNotIn('secret-fixture', state)
        self.assertNotIn('api.kiwoom.com', state)
        self.assertEqual(os.stat(self.path.with_name('rate.json.real-kt00018')).st_mode & 0o777, 0o600)

    def test_paper_kt00018_keeps_existing_stricter_bucket_and_other_tr_unchanged(self):
        limiter = SharedRateLimit(self.path)
        paper = limiter.acquire_for_request(req('mockapi.kiwoom.com', 'kt00018'))
        self.assertIsNotNone(paper)
        paper.release()
        self.assertTrue(self.path.with_name('rate.json.paper-kt00018').exists())
        self.assertFalse(self.path.with_name('rate.json.real-kt00018').exists())
        self.assertIsNone(limiter.acquire_for_request(req('api.kiwoom.com', 'kt00001')))

    def test_separate_processes_share_real_tr_dispatch_limit(self):
        context = multiprocessing.get_context('spawn')
        go, output = context.Event(), context.Queue()
        workers = [context.Process(target=reserve_in_process, args=(str(self.path), go, output)) for _ in range(2)]
        for worker in workers: worker.start()
        self.addCleanup(lambda: [worker.terminate() for worker in workers if worker.is_alive()])
        go.set()
        result = [output.get(timeout=10) for _ in workers]
        for worker in workers: worker.join(5)
        self.assertTrue(all(row[0] == 'at' for row in result), result)
        observed = sorted(row[1] for row in result)
        self.assertGreaterEqual(observed[1]-observed[0], 1.0, result)
        self.assertTrue(all(worker.exitcode == 0 for worker in workers))

    def test_exact_cooldown_boundary(self):
        clock = [10.0]
        limiter = SharedRateLimit(self.path, clock=lambda: clock[0])
        first = limiter.acquire_for_request(req('api.kiwoom.com', 'kt00018'))
        first.release()
        limiter._wait = Mock()
        def advance(delay):
            self.assertGreater(delay, 0)
            clock[0] += delay
        limiter._wait.wait.side_effect = advance
        clock[0] = 11.049
        second = limiter.acquire_for_request(req('api.kiwoom.com', 'kt00018'))
        self.assertGreaterEqual(clock[0], 11.05)
        self.assertEqual(limiter._wait.wait.call_count, 1)
        second.release()
        clock[0] = 0.0
        exact = SharedRateLimit(self.path.with_name('exact-rate.json'), clock=lambda: clock[0])
        exact._wait = Mock()
        third = exact.acquire_for_request(req('api.kiwoom.com', 'kt00018'))
        third.release()
        clock[0] = 1.05
        fourth = exact.acquire_for_request(req('api.kiwoom.com', 'kt00018'))
        exact._wait.wait.assert_not_called()
        fourth.release()

    def test_parallel_threads_share_real_tr_bucket(self):
        limiter = SharedRateLimit(self.path)
        def reserve(_):
            lease = limiter.acquire_for_request(req('api.kiwoom.com', 'kt00018'))
            try:
                return _CLOCK()
            finally:
                lease.release()
        with ThreadPoolExecutor(max_workers=3) as pool:
            times = sorted(pool.map(reserve, range(3)))
        self.assertTrue(all(b - a >= 1.0 for a, b in zip(times, times[1:])), times)

    def test_guard_failure_after_wait_does_not_send_or_consume_tr_slot(self):
        clock, session_valid = [10.0], [True]
        limiter = SharedRateLimit(self.path, clock=lambda: clock[0])
        limiter._wait = Mock()
        def advance(delay):
            clock[0] += delay
            session_valid[0] = False
        limiter._wait.wait.side_effect = advance
        queue, opener = HTTPQueue(limiter=limiter), Opener()
        self.addCleanup(queue.close)
        def guard():
            if not session_valid[0]:
                raise RuntimeError('session changed')
        queue.submit(opener, req('api.kiwoom.com', 'kt00018'), before_send=guard).result(timeout=3)
        with self.assertRaises(HTTPNotSentError):
            queue.submit(opener, req('api.kiwoom.com', 'kt00018'), before_send=guard).result(timeout=3)
        self.assertEqual(len(opener.calls), 1)
        self.assertEqual(json.loads(self.path.with_name('rate.json.real-kt00018').read_text()), [10.0])

    def test_transport_failure_consumes_slot_and_explicit_retry_gets_new_slot(self):
        clock = [10.0]
        limiter = SharedRateLimit(self.path, clock=lambda: clock[0])
        limiter._wait = Mock()
        limiter._wait.wait.side_effect = lambda delay: clock.__setitem__(0, clock[0] + delay)
        queue = HTTPQueue(limiter=limiter)
        self.addCleanup(queue.close)
        def failed(request, timeout):
            raise urllib.error.HTTPError('https://example.invalid/secret', 429, 'private', {}, io.BytesIO())
        first, second = Opener(failed), Opener()
        with self.assertRaises(urllib.error.HTTPError) as error:
            queue.submit(first, req('api.kiwoom.com', 'kt00018')).result(timeout=3)
        self.assertNotIn('secret', str(error.exception))
        self.assertEqual(len(first.calls), 1)
        self.assertEqual(json.loads(self.path.with_name('rate.json.real-kt00018').read_text()), [10.0])
        queue.submit(second, req('api.kiwoom.com', 'kt00018')).result(timeout=3)
        self.assertEqual(len(first.calls), 1)
        self.assertEqual(len(second.calls), 1)
        self.assertGreaterEqual(clock[0], 11.05)

    def test_real_and_paper_still_share_global_four_per_window(self):
        clock = [10.0]
        limiter = SharedRateLimit(self.path, clock=lambda: clock[0])
        limiter._wait = Mock()
        limiter._wait.wait.side_effect = lambda delay: clock.__setitem__(0, clock[0] + delay)
        for host, code in [('api.kiwoom.com', 'kt00018'), ('mockapi.kiwoom.com', 'kt00018'),
                           ('api.kiwoom.com', 'kt00001'), ('mockapi.kiwoom.com', 'kt00001')]:
            lease = limiter.acquire_for_request(req(host, code))
            if lease is not None: lease.release()
        self.assertEqual(clock[0], 10.0)
        self.assertIsNone(limiter.acquire_for_request(req('api.kiwoom.com', 'kt00001')))
        self.assertGreaterEqual(clock[0], 11.01)


if __name__ == '__main__':
    unittest.main()
