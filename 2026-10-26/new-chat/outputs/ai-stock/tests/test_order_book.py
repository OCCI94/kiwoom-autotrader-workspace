"""Offline ka10004 budget-limit selection, freshness and dispatch guards."""
from datetime import datetime, timedelta
import json
import unittest
from unittest.mock import Mock, patch

from kiwoom_autotrader.api import order_book as book
from kiwoom_autotrader.api.errors import BrokerAPIError
from kiwoom_autotrader.api.session import BrokerSession, IssuedToken, KST


class OrderBookTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 26, 10, 30, 0, tzinfo=KST)
        self.session = BrokerSession(now=lambda: self.now)
        self.session.mode = "paper"
        self.session._issued = IssuedToken("offline-private-book-token", "20300101000000")
        self.payload = {"return_code": 0, "bid_req_base_tm": "102959",
                        "buy_fpr_bid": "+213500", "buy_fpr_req": "20",
                        "sel_fpr_bid": "-214000", "sel_fpr_req": "10"}
        self.response = Mock()
        self.response.headers = {"api-id": "ka10004", "cont-yn": "N", "next-key": ""}
        self.response.read.side_effect = lambda _: json.dumps(self.payload).encode()
        self.context = Mock()
        self.context.__enter__ = Mock(return_value=self.response)
        self.context.__exit__ = Mock(return_value=False)
        self.opener = Mock()
        self.opener.open.side_effect = AssertionError("no real HTTP")
        self.queue = Mock(side_effect=self.queued)
        pacer = Mock()
        pacer.acquire.side_effect = lambda *, before_send: before_send()
        for patcher in (
            patch.object(book, "queued_open", self.queue),
            patch.object(book, "_now", side_effect=lambda: self.now),
            patch.object(book, "_REQUEST_PACER", pacer),
            patch.object(book.urllib.request, "build_opener", return_value=self.opener),
            patch("socket.socket.connect", side_effect=AssertionError("network forbidden")),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def queued(self, _opener, _request, *, before_send, **_):
        before_send()
        return self.context

    def fetch(self, **kwargs):
        return book.fetch_budget_limit(self.session, "005930", 213750, **kwargs)

    def test_exact_contract_uses_actual_displayed_quote_without_symbol_echo(self):
        self.assertEqual(self.fetch(), 213500)
        opener, request = self.queue.call_args.args
        self.assertIs(opener, self.opener)
        self.assertEqual(request.full_url, "https://mockapi.kiwoom.com/api/dostk/mrkcond")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(json.loads(request.data), {"stk_cd": "005930"})
        self.assertEqual(request.get_header("Api-id"), "ka10004")
        self.assertEqual(request.get_header("Authorization"), "Bearer offline-private-book-token")
        self.assertEqual(request.get_header("Cont-yn"), "N")
        self.assertEqual(request.get_header("Next-key"), "")
        self.assertEqual(self.queue.call_args.kwargs["max_bytes"], book._MAX_RESPONSE_BYTES)
        self.opener.open.assert_not_called()
        self.session.mode = "real"
        self.assertEqual(self.fetch(), 213500)
        self.assertEqual(self.queue.call_args.args[1].full_url, "https://api.kiwoom.com/api/dostk/mrkcond")

    def test_all_twenty_levels_and_signed_prices_are_considered_without_grid_guess(self):
        for side in ("buy", "sel"):
            for level in range(1, 11):
                with self.subTest(side=side, level=level):
                    self.payload = {"return_code": "0", "bid_req_base_tm": "103000"}
                    stem = f"{side}_fpr" if level == 1 else f"{side}_{level}th_pre"
                    self.payload[stem + "_bid"] = "-213700"
                    self.payload[stem + "_req"] = "+2"
                    self.assertEqual(self.fetch(), 213700)

    def test_highest_at_or_below_current_and_positive_quantity_only(self):
        self.payload.update(buy_2th_pre_bid="213750", buy_2th_pre_req="0",
                            sel_3th_pre_bid="213600", sel_3th_pre_req="2")
        self.assertEqual(self.fetch(), 213600)
        self.payload["buy_2th_pre_req"] = "1"
        self.assertEqual(self.fetch(), 213750)

    def test_no_eligible_quote_is_safe_failure(self):
        for changed in ({"buy_fpr_req": "0"}, {"buy_fpr_bid": "0"}, {"buy_fpr_bid": "214500"}):
            with self.subTest(changed=changed):
                old = self.payload.copy()
                self.payload.update(changed)
                with self.assertRaises(book.OrderBookError):
                    self.fetch()
                self.payload = old

    def test_malformed_prices_quantities_codes_and_payloads_are_rejected(self):
        original = self.payload.copy()
        cases = [{"return_code": True}, {"return_code": None}, {"return_code": 12},
                 {"stk_cd": "000660"}, {"buy_fpr_bid": "213500.0"},
                 {"buy_fpr_bid": True}, {"buy_fpr_bid": "9" * 21},
                 {"buy_fpr_bid": "1000000000000"}, {"buy_fpr_req": "-1"},
                 {"buy_fpr_req": "1.5"}, {"buy_fpr_req": 3}]
        for changed in cases:
            with self.subTest(changed=changed), self.assertRaises(book.OrderBookError):
                self.payload = {**original, **changed}
                self.fetch()
        for payload in ([], None, True):
            with self.subTest(payload=payload), self.assertRaises(book.OrderBookError):
                self.payload = payload
                self.fetch()

    def test_freshness_boundaries_future_and_invalid_clock(self):
        for delta in (-90, 0, 2):
            self.payload["bid_req_base_tm"] = (self.now + timedelta(seconds=delta)).strftime("%H%M%S")
            self.assertEqual(self.fetch(), 213500)
        for value in ("102829", "103003", "240000", "10:30:00", "", None, 103000):
            with self.subTest(value=value), self.assertRaises(book.OrderBookError):
                self.payload["bid_req_base_tm"] = value
                self.fetch()
        self.payload["bid_req_base_tm"] = "103000"
        with patch.object(book, "_now", return_value=datetime(2026, 10, 26, 10, 30)), self.assertRaises(book.OrderBookError):
            self.fetch()

    def test_wrong_headers_oversized_and_invalid_json_are_rejected(self):
        for headers in ({}, {"api-id": "ka10001"}, {"api-id": "ka10004", "cont-yn": "Y"},
                        {"api-id": "ka10004", "cont-yn": "wrong"},
                        {"api-id": "ka10004", "next-key": "unexpected"}):
            with self.subTest(headers=headers), self.assertRaises(book.OrderBookError):
                self.response.headers = headers
                self.fetch()
        self.response.headers = {"api-id": "ka10004"}
        for raw in (b"x" * (book._MAX_RESPONSE_BYTES + 1), b"bad", b"\xff", "not bytes"):
            with self.subTest(raw_length=len(raw)), self.assertRaises(book.OrderBookError):
                self.response.read.side_effect = None
                self.response.read.return_value = raw
                self.fetch()

    def test_invalid_input_or_expired_session_never_queues(self):
        for symbol, price in (("005930_NX", 100), ("A005930", 100), (" 005930", 100),
                              ("005930", True), ("005930", "100"), ("005930", 0), ("005930", 10**12)):
            with self.subTest(symbol=symbol, price=price), self.assertRaises(book.OrderBookError):
                book.fetch_budget_limit(self.session, symbol, price)
        self.session._issued = IssuedToken("offline-private-book-token", "20200101000000")
        with self.assertRaises(book.OrderBookError):
            self.fetch()
        self.queue.assert_not_called()

    def test_external_guard_before_queue_and_after_response(self):
        guard = Mock(side_effect=ValueError("private guard details"))
        with self.assertRaises(book.OrderBookError) as error:
            self.fetch(before_send=guard)
        self.assertNotIn("private guard details", str(error.exception))
        self.queue.assert_not_called()
        stopped = False
        def read(_):
            nonlocal stopped
            stopped = True
            return json.dumps(self.payload).encode()
        def check():
            if stopped:
                raise RuntimeError("private stop detail")
        self.response.read.side_effect = read
        with self.assertRaises(book.OrderBookError):
            self.fetch(before_send=check)

    def test_mode_token_and_expiry_changes_in_queue_or_response_reject_data(self):
        for stage in ("queue", "response"):
            for change in ("mode", "token", "expiry"):
                with self.subTest(stage=stage, change=change):
                    self.session.mode = "paper"
                    self.session._issued = IssuedToken("offline-private-book-token", "20300101000000")
                    def mutate():
                        if change == "mode":
                            self.session.mode = "real"
                        elif change == "token":
                            self.session._issued = IssuedToken("different-offline-token", "20300101000000")
                        else:
                            self.session._issued = IssuedToken("offline-private-book-token", "20200101000000")
                    def queued(*args, **kwargs):
                        if stage == "queue":
                            mutate()
                        return self.queued(*args, **kwargs)
                    def read(_):
                        if stage == "response":
                            mutate()
                        return json.dumps(self.payload).encode()
                    self.queue.side_effect = queued
                    self.response.read.side_effect = read
                    with self.assertRaises(book.OrderBookError):
                        self.fetch()

    def test_transport_failure_no_retry_and_remote_message_not_exposed(self):
        self.queue.side_effect = OSError("offline-private-book-token")
        with self.assertRaises(book.OrderBookError) as error:
            self.fetch()
        self.assertNotIn("offline-private-book-token", str(error.exception))
        self.queue.assert_called_once()
        self.queue.side_effect = self.queued
        self.payload.update(return_code=8010, return_msg="private remote message")
        with patch("kiwoom_autotrader.api.errors.annotate_ip_error", side_effect=lambda _, message: message), \
             self.assertRaises(BrokerAPIError) as error:
            self.fetch()
        self.assertTrue(error.exception.ip_related)
        self.assertNotIn("private remote message", str(error.exception))

    def test_redirect_handler_refuses_other_hosts(self):
        self.assertIsNone(book._RejectRedirects().redirect_request(None, None, 302, "", {}, "https://other.invalid"))


class OrderBookPacerTests(unittest.TestCase):
    def test_dispatch_grants_spaced_and_guard_rechecked_after_wait(self):
        clock = [0.0]
        waits = []
        def wait(delay):
            waits.append(delay)
            clock[0] += delay
        pacer = book._OrderBookPacer(clock=lambda: clock[0], wait=wait)
        guard = Mock()
        pacer.acquire(before_send=guard)
        pacer.acquire(before_send=guard)
        pacer.acquire(before_send=guard)
        self.assertEqual(waits, [1.05, 1.05])
        self.assertEqual(clock[0], 2.1)
        self.assertEqual(guard.call_count, 8)

    def test_cancel_during_pacing_does_not_grant_another_dispatch(self):
        clock = [0.0]
        stopped = [False]
        def wait(delay):
            clock[0] += delay
            stopped[0] = True
        def guard():
            if stopped[0]:
                raise book.OrderBookError("stopped")
        pacer = book._OrderBookPacer(clock=lambda: clock[0], wait=wait)
        pacer.acquire(before_send=guard)
        with self.assertRaises(book.OrderBookError):
            pacer.acquire(before_send=guard)
        self.assertEqual(pacer._last, 0)


if __name__ == "__main__":
    unittest.main()
