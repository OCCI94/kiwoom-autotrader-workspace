"""Shared ka10080 pacing uses fake time and transport, never live HTTP."""

from datetime import datetime
import io
import json
import threading
import unittest
from unittest.mock import Mock, patch

from kiwoom_autotrader.api import minute_chart as chart
from kiwoom_autotrader.api.session import BrokerSession, IssuedToken, KST
from kiwoom_autotrader.utils.http_queue import HTTPNotSentError


class FakeClock:
    def __init__(self):
        self.value = 100.0
        self.waits = []
        self.on_wait = None

    def now(self):
        return self.value

    def wait(self, delay):
        self.waits.append(delay)
        self.value += delay
        if self.on_wait is not None:
            self.on_wait()


class MinuteChartPacerTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.pacer = chart._MinuteChartPacer(clock=self.clock.now, wait=self.clock.wait)

    def test_first_grant_is_immediate_and_next_grants_are_spaced(self):
        grants = []
        for _ in range(4):
            self.pacer.acquire(before_send=lambda: None)
            grants.append(self.clock.now())
        self.assertEqual(len(self.clock.waits), 3)
        self.assertEqual(grants[0], 100.0)
        for left, right in zip(grants, grants[1:]):
            self.assertGreaterEqual(right - left, 1.05 - 1e-12)

    def test_time_spent_elsewhere_reduces_wait_and_long_gap_needs_none(self):
        self.pacer.acquire(before_send=lambda: None)
        self.clock.value += .4
        self.pacer.acquire(before_send=lambda: None)
        self.assertAlmostEqual(self.clock.waits[0], .65)
        self.clock.value += 10
        self.pacer.acquire(before_send=lambda: None)
        self.assertEqual(len(self.clock.waits), 1)

    def test_early_wakeup_waits_for_the_remaining_gap(self):
        waits = []
        def early_wait(delay):
            waits.append(delay)
            self.clock.value += delay / 2 if len(waits) == 1 else delay
        self.pacer = chart._MinuteChartPacer(clock=self.clock.now, wait=early_wait)
        self.pacer.acquire(before_send=lambda: None)
        self.pacer.acquire(before_send=lambda: None)
        self.assertEqual(len(waits), 2)
        self.assertAlmostEqual(sum(waits), 1.575)
        self.assertAlmostEqual(self.clock.now(), 101.05)

    def test_guard_rejection_before_wait_does_not_consume_a_grant(self):
        self.pacer.acquire(before_send=lambda: None)
        with self.assertRaises(ValueError):
            self.pacer.acquire(before_send=Mock(side_effect=ValueError("invalid session")))
        self.assertEqual(self.clock.waits, [])
        self.assertEqual(self.pacer._last, 100.0)

    def test_guard_rechecks_after_wait_and_failed_check_does_not_advance_last_grant(self):
        self.pacer.acquire(before_send=lambda: None)
        valid = True
        def guard():
            if not valid:
                raise ValueError("session changed")
        def invalidate():
            nonlocal valid
            valid = False
        self.clock.on_wait = invalidate
        with self.assertRaises(ValueError):
            self.pacer.acquire(before_send=guard)
        self.assertEqual(self.pacer._last, 100.0)
        self.clock.on_wait = None
        self.pacer.acquire(before_send=lambda: None)
        self.assertEqual(len(self.clock.waits), 1)

    def test_concurrent_callers_share_one_serial_pacer(self):
        wait_entered = threading.Event()
        release_wait = threading.Event()
        second_started = threading.Event()
        second_checked = threading.Event()
        errors = []
        def blocked_wait(delay):
            wait_entered.set()
            if not release_wait.wait(1):
                raise AssertionError("Test did not release fake wait")
            self.clock.wait(delay)
        self.pacer = chart._MinuteChartPacer(clock=self.clock.now, wait=blocked_wait)
        self.pacer.acquire(before_send=lambda: None)
        def acquire(check):
            try:
                self.pacer.acquire(before_send=check)
            except Exception as error:
                errors.append(error)
        first = threading.Thread(target=acquire, args=(lambda: None,))
        def second_target():
            second_started.set()
            acquire(second_checked.set)
        second = threading.Thread(target=second_target)
        try:
            first.start()
            self.assertTrue(wait_entered.wait(1))
            second.start()
            self.assertTrue(second_started.wait(1))
            self.assertFalse(second_checked.is_set())
        finally:
            release_wait.set()
            first.join(1)
            if second.ident is not None:
                second.join(1)
        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(len(self.clock.waits), 2)
        self.assertAlmostEqual(self.clock.now(), 102.1)


class MinuteChartPacingIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.wall_clock = datetime(2026, 10, 26, 10, tzinfo=KST)
        self.pacer = chart._MinuteChartPacer(clock=self.clock.now, wait=self.clock.wait)
        self.session = BrokerSession(now=lambda: self.wall_clock,
            credential_loader=Mock(side_effect=AssertionError("No credentials")),
            token_issuer=Mock(side_effect=AssertionError("No login")))
        self.session.mode = "paper"
        self.session._issued = IssuedToken("offline-pacing-private-token", "20300101000000")
        self.sent_at = []
        self.requests = []
        self.responses = []
        self.fail_transport = False
        self.before_grant = None
        self.patch(chart, "_REQUEST_PACER", self.pacer)
        self.opener = self.patch(chart.urllib.request, "build_opener")
        self.queue = self.patch(chart, "queued_open", side_effect=self.transport)
        self.network = self.patch_path("socket.socket.connect", side_effect=AssertionError("Live HTTP forbidden"))
        self.keychain = self.patch_path("kiwoom_autotrader.utils.keychain._get_backend",
                                        side_effect=AssertionError("Live Keychain forbidden"))

    def patch(self, target, name, *args, **options):
        patcher = patch.object(target, name, *args, **options)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def patch_path(self, target, **options):
        patcher = patch(target, **options)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def tearDown(self):
        self.network.assert_not_called()
        self.keychain.assert_not_called()
        self.session._credential_loader.assert_not_called()
        self.session._token_issuer.assert_not_called()
        self.opener.return_value.open.assert_not_called()

    def transport(self, opener, request, **options):
        if self.before_grant is not None:
            self.before_grant()
        try:
            options["before_send"]()
        except Exception:
            raise HTTPNotSentError("Offline guard rejection") from None
        self.sent_at.append(self.clock.now())
        self.requests.append(request)
        if self.fail_transport:
            raise OSError("private transport failure")
        symbol = json.loads(request.data)["stk_cd"]
        if self.responses:
            payload, headers = self.responses.pop(0)
        else:
            payload = {"return_code": 0, "stk_cd": symbol, "stk_min_pole_chart_qry": [
                {"cntr_tm": "20261026100100", "cur_prc": "100"},
                {"cntr_tm": "20261026100200", "cur_prc": "200"}]}
            headers = {"api-id": "ka10080", "cont-yn": "N"}
        result = io.BytesIO(json.dumps(payload).encode())
        result.headers = headers
        return result

    def fetch(self, symbol="005930"):
        return chart.fetch_minute_chart(self.session, symbol, count=2)

    def test_different_symbols_sessions_and_modes_share_the_dispatch_gap(self):
        self.fetch("005930")
        self.fetch("000660")
        self.session.mode = "real"
        self.session._issued = IssuedToken("another-offline-pacing-token", "20300101000000")
        self.fetch("035420")
        self.assertEqual(len(self.sent_at), 3)
        for left, right in zip(self.sent_at, self.sent_at[1:]):
            self.assertAlmostEqual(right - left, 1.05)
        self.assertTrue(self.requests[-1].full_url.startswith("https://api.kiwoom.com/"))

    def test_continuation_page_also_uses_same_pacer(self):
        def payload(minute):
            return {"return_code": 0, "stk_cd": "005930", "stk_min_pole_chart_qry": [
                {"cntr_tm": "20261026100" + str(minute) + "00", "cur_prc": "100"}]}
        self.responses = [(payload(2), {"cont-yn": "Y", "next-key": "offline-next"}),
                          (payload(1), {"cont-yn": "N"})]
        self.assertEqual(len(self.fetch()), 2)
        self.assertAlmostEqual(self.sent_at[1] - self.sent_at[0], 1.05)
        self.assertEqual(self.requests[1].get_header("Next-key"), "offline-next")

    def test_pacing_is_at_dispatch_after_queue_wait_not_request_construction(self):
        self.fetch()
        self.before_grant = lambda: setattr(self.clock, "value", self.clock.value + 3)
        self.fetch("000660")
        self.assertEqual(self.clock.waits, [])
        self.assertEqual(self.sent_at, [100.0, 103.0])

    def test_mode_token_or_expiry_change_while_pacing_prevents_second_transport(self):
        changes = (lambda: setattr(self.session, "mode", "real"),
                   lambda: setattr(self.session, "_issued", IssuedToken("changed-token", "20300101000000")),
                   lambda: setattr(self, "wall_clock", datetime(2030, 1, 1, tzinfo=KST)))
        for change in changes:
            with self.subTest(change=change):
                self.session.mode = "paper"
                self.session._issued = IssuedToken("offline-pacing-private-token", "20300101000000")
                self.wall_clock = datetime(2026, 10, 26, 10, tzinfo=KST)
                self.clock.on_wait = None
                self.clock.value += 2
                self.fetch()
                sent = len(self.sent_at)
                self.clock.on_wait = change
                with self.assertRaises(chart.MinuteChartError):
                    self.fetch("000660")
                self.assertEqual(len(self.sent_at), sent)

    def test_failed_transport_is_not_retried_and_still_reserves_the_gap(self):
        self.fail_transport = True
        with self.assertRaises(chart.MinuteChartError) as caught:
            self.fetch()
        self.assertNotIn("private transport failure", str(caught.exception))
        self.queue.assert_called_once()
        self.fail_transport = False
        self.fetch("000660")
        self.assertAlmostEqual(self.sent_at[1] - self.sent_at[0], 1.05)


if __name__ == "__main__":
    unittest.main()
