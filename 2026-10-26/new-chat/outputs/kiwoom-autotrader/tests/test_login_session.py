"""Offline broker login, environment isolation and daily scheduling tests."""

from datetime import datetime, timedelta, timezone
import io
import json
import traceback
import unittest
import urllib.error
from unittest.mock import Mock, patch

from kiwoom_autotrader.credentials import Credentials, KeychainError
from kiwoom_autotrader import login_session as login
from kiwoom_autotrader import real_credentials as real


TOKEN = "fixture-access-token-private"
PAIR = Credentials("fixture-app-key", "fixture-app-secret")


class TokenTransportTests(unittest.TestCase):
    def setUp(self):
        self.patcher = patch("kiwoom_autotrader.login_session.urllib.request.build_opener")
        self.addCleanup(self.patcher.stop)
        self.factory = self.patcher.start()
        self.opener = self.factory.return_value
        self.response = self.opener.open.return_value.__enter__.return_value
        self.payload = {"return_code": 0, "token_type": "Bearer", "token": TOKEN,
                        "expires_dt": "20300101085500"}
        self.response.read.side_effect = lambda size: json.dumps(self.payload).encode()

    def test_each_mode_uses_only_its_fixed_token_endpoint(self):
        for mode, host in (("paper", "mockapi.kiwoom.com"), ("real", "api.kiwoom.com")):
            with self.subTest(mode=mode):
                issued = login.request_token(mode, PAIR)
                self.assertEqual(issued.token, TOKEN)
                self.assertNotIn(TOKEN, repr(issued))
                request = self.opener.open.call_args.args[0]
                self.assertEqual(request.full_url, f"https://{host}/oauth2/token")
                self.assertEqual(request.method, "POST")
                self.assertEqual(json.loads(request.data), {
                    "grant_type": "client_credentials", "appkey": PAIR.app_key,
                    "secretkey": PAIR.app_secret,
                })
                self.assertEqual(self.opener.open.call_args.kwargs["timeout"], 15)
                self.assertIsInstance(self.factory.call_args.args[0], login._RejectRedirects)

    def test_unknown_mode_never_issues_request(self):
        for value in ("live", "https://example.com", None, "paper extra"):
            with self.assertRaises(ValueError):
                login.request_token(value, PAIR)
        self.factory.assert_not_called()

    def test_invalid_success_responses_do_not_leak_body_or_credentials(self):
        cases = [
            {"return_code": True}, {"return_code": 2, "return_msg": PAIR.app_secret},
            {"token": TOKEN, "token_type": "Bearer"},
            {**self.payload, "token_type": "basic"}, {**self.payload, "token": "bad\n"},
            {**self.payload, "expires_dt": PAIR.app_secret},
            {**self.payload, "expires_dt": "20300231085500"},
        ]
        for payload in cases:
            with self.subTest(payload_keys=list(payload)):
                self.payload = payload
                with self.assertRaises(login.LoginSessionError) as caught:
                    login.request_token("paper", PAIR)
                text = "".join(traceback.format_exception(caught.exception))
                for secret in (TOKEN, PAIR.app_key, PAIR.app_secret):
                    self.assertNotIn(secret, text)

    def test_transport_exception_omits_private_remote_description(self):
        self.opener.open.side_effect = urllib.error.HTTPError(
            "https://invalid/" + PAIR.app_secret, 403, TOKEN, {}, io.BytesIO(TOKEN.encode()),
        )
        with self.assertRaises(login.LoginSessionError) as caught:
            login.request_token("real", PAIR)
        self.assertIn("403", str(caught.exception))
        for secret in (TOKEN, PAIR.app_key, PAIR.app_secret):
            self.assertNotIn(secret, "".join(traceback.format_exception(caught.exception)))

    def test_token_response_requires_a_nonempty_expiry(self):
        original = dict(self.payload)
        for expiry in (None, "", False, 20300101085500):
            with self.subTest(expiry=expiry):
                self.payload = {**original, "expires_dt": expiry}
                with self.assertRaisesRegex(login.LoginSessionError, "만료 시각"):
                    login.request_token("paper", PAIR)
        self.payload = {key: value for key, value in original.items() if key != "expires_dt"}
        with self.assertRaisesRegex(login.LoginSessionError, "만료 시각"):
            login.request_token("paper", PAIR)

    def test_json_and_size_limits_are_sanitized(self):
        for body in (b"[", b"[]", b"x" * (login._MAX_RESPONSE_BYTES + 1)):
            self.response.read.side_effect = None
            self.response.read.return_value = body
            with self.assertRaises(login.LoginSessionError):
                login.request_token("paper", PAIR)

    def test_credentials_are_loaded_from_separate_fixed_stores(self):
        paper, live = Credentials("paper-key", "paper-secret"), Credentials("real-key", "real-secret")
        with patch.object(login, "load_mock_credentials", return_value=paper) as mock_load, \
                patch.object(login, "load_real_credentials", return_value=live) as real_load:
            self.assertIs(login.load_credentials("PAPER"), paper)
            self.assertIs(login.load_credentials(" real "), live)
            mock_load.assert_called_once_with()
            real_load.assert_called_once_with()


class BrokerSessionTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 26, 8, 54, tzinfo=login.KST)
        self.loader = Mock(return_value=PAIR)
        self.issuer = Mock(return_value=login.IssuedToken(TOKEN, "20300101085500"))
        self.save = Mock()
        self.session = login.BrokerSession(now=lambda: self.now, credential_loader=self.loader,
                                           token_issuer=self.issuer, save_renewal=self.save)

    def test_command_normalization_and_exact_grammar(self):
        for text in (" login PAPER ", "LOGIN paper", "\nLoGiN\tPaPeR\t"):
            self.assertEqual(login.parse_login_command(text), login.LoginCommand("login", "paper"))
        self.assertEqual(login.parse_login_command(" LOGIN  REAL "), login.LoginCommand("login", "real"))
        self.assertEqual(login.parse_login_command(" login ReNeWaL 8:50 "), login.LoginCommand("renewal", "08:50"))
        self.assertIsNone(login.parse_login_command("power off"))
        self.assertIsNone(login.parse_login_command("/top"))
        for text in ("login", "login live", "login paper extra", "login renewal 24:00",
                     "login renewal 8:5", "login renewal 08:55:00", "login renewal -1:00"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                login.parse_login_command(text)

    def test_no_renewal_before_login_even_if_clock_passes_due_time(self):
        self.now += timedelta(days=2)
        self.assertIsNone(self.session.seconds_until_renewal())
        self.assertIsNone(self.session.renew_if_due())
        self.loader.assert_not_called()
        self.issuer.assert_not_called()

    def test_login_retains_token_and_mode_but_result_and_repr_are_redacted(self):
        result = self.session.login("paper")
        self.assertTrue(result.success)
        self.assertEqual(self.session.mode, "paper")
        self.assertEqual(self.session.token, TOKEN)
        self.assertEqual(self.session.expires_dt, "20300101085500")
        self.assertEqual(self.session.seconds_until_renewal(), 60)
        self.assertEqual(self.session.next_renewal_at.hour, 8)
        self.loader.assert_called_once_with("paper")
        self.issuer.assert_called_once_with("paper", PAIR)
        self.assertNotIn(TOKEN, repr(self.session) + repr(result) + result.message)
        self.assertTrue(self.session.token_usable)

    def test_expired_or_exactly_expiring_new_tokens_are_rejected(self):
        for expiry in ("20261026085359", "20261026085400"):
            with self.subTest(expiry=expiry):
                self.issuer.return_value = login.IssuedToken(TOKEN, expiry)
                result = self.session.login("paper")
                self.assertFalse(result.success)
                self.assertIn("이미 만료", result.message)
                self.assertFalse(self.session.authenticated)
                self.assertFalse(self.session.token_usable)
                self.assertIsNone(self.session.next_renewal_at)

    def test_token_expiring_during_authentication_is_rejected_at_completion(self):
        def issue_after_delay(*_):
            self.now += timedelta(minutes=2)
            return login.IssuedToken(TOKEN, "20261026085500")
        self.issuer.side_effect = issue_after_delay
        self.assertFalse(self.session.login("paper").success)
        self.assertFalse(self.session.token_usable)

    def test_expired_session_can_still_renew_and_become_usable(self):
        self.issuer.return_value = login.IssuedToken(TOKEN, "20261026085500")
        self.assertTrue(self.session.login("paper").success)
        self.now += timedelta(minutes=1)
        self.assertTrue(self.session.authenticated)
        self.assertFalse(self.session.token_usable)
        self.issuer.return_value = login.IssuedToken("fresh-token", "20300101085500")
        self.assertTrue(self.session.renew_if_due().success)
        self.assertTrue(self.session.token_usable)

    def test_failed_renewal_does_not_claim_expired_token_is_still_valid(self):
        self.issuer.return_value = login.IssuedToken(TOKEN, "20261026085500")
        self.session.login("paper")
        self.now += timedelta(minutes=1)
        self.issuer.side_effect = RuntimeError(PAIR.app_secret)
        result = self.session.renew_if_due()
        self.assertFalse(result.success)
        self.assertIn("만료되어 다시 로그인", result.message)
        self.assertNotIn("상태를 유지", result.message)
        self.assertTrue(self.session.authenticated)
        self.assertFalse(self.session.token_usable)
        self.assertIsNone(self.session.renew_if_due())

    def test_failed_environment_switch_keeps_existing_token_mode_and_schedule(self):
        self.session.login("paper")
        next_due = self.session.next_renewal_at
        self.loader.return_value = None
        result = self.session.login("real")
        self.assertFalse(result.success)
        self.assertIn("setup-real.command", result.message)
        self.assertIn("기존 모의투자", result.message)
        self.assertEqual(self.session.mode, "paper")
        self.assertEqual(self.session.token, TOKEN)
        self.assertEqual(self.session.next_renewal_at, next_due)
        self.assertEqual(self.issuer.call_count, 1)

    def test_successful_environment_switch_updates_all_session_fields(self):
        self.session.login("paper")
        self.issuer.return_value = login.IssuedToken("different-real-token")
        result = self.session.login("real")
        self.assertTrue(result.success)
        self.assertEqual(self.session.mode, "real")
        self.assertEqual(self.session.token, "different-real-token")
        self.assertIsNone(self.session.expires_dt)

    def test_due_renewal_fires_once_and_reloads_current_environment_credentials(self):
        self.session.login("real")
        self.now += timedelta(minutes=1)
        result = self.session.renew_if_due()
        self.assertTrue(result.success)
        self.assertTrue(result.renewal)
        self.assertEqual(result.mode, "real")
        self.assertEqual(self.loader.call_count, 2)
        self.assertEqual(self.loader.call_args.args, ("real",))
        self.assertIsNone(self.session.renew_if_due())
        self.assertEqual(self.session.next_renewal_at, datetime(2026, 10, 27, 8, 55, tzinfo=login.KST))

    def test_failed_scheduled_attempt_does_not_spin_or_erase_token(self):
        self.session.login("paper")
        self.issuer.side_effect = RuntimeError(PAIR.app_secret)
        self.now += timedelta(minutes=1)
        result = self.session.renew_if_due()
        self.assertFalse(result.success)
        self.assertNotIn(PAIR.app_secret, result.message)
        self.assertEqual(self.session.token, TOKEN)
        self.assertIsNone(self.session.renew_if_due())
        self.assertEqual(self.issuer.call_count, 2)

    def test_clock_in_utc_and_missed_days_renew_once_then_next_kst_day(self):
        self.session.login("paper")
        self.now = datetime(2026, 10, 29, 2, 0, tzinfo=timezone.utc)
        self.assertTrue(self.session.renew_if_due().success)
        self.assertEqual(self.session.next_renewal_at, datetime(2026, 10, 30, 8, 55, tzinfo=login.KST))
        self.assertIsNone(self.session.renew_if_due())

    def test_login_at_or_after_appointed_time_does_not_immediately_renew_again(self):
        for minute in (55, 56):
            self.now = datetime(2026, 10, 26, 8, minute, tzinfo=login.KST)
            self.session.login("paper")
            self.assertEqual(self.session.next_renewal_at.date().isoformat(), "2026-10-27")
            self.assertIsNone(self.session.renew_if_due())

    def test_login_crossing_renewal_boundary_schedules_from_completion(self):
        self.now = datetime(2026, 10, 26, 8, 54, 59, tzinfo=login.KST)
        def issue_after_delay(*_):
            self.now += timedelta(seconds=3)
            return login.IssuedToken(TOKEN)
        self.issuer.side_effect = issue_after_delay
        self.assertTrue(self.session.login("paper").success)
        self.assertEqual(self.session.next_renewal_at, datetime(2026, 10, 27, 8, 55, tzinfo=login.KST))
        self.assertIsNone(self.session.renew_if_due())
        self.assertEqual(self.issuer.call_count, 1)

    def test_midnight_login_completion_does_not_schedule_a_past_deadline(self):
        self.session.set_renewal("0:00")
        self.now = datetime(2026, 10, 26, 23, 59, 59, tzinfo=login.KST)
        def issue_after_delay(*_):
            self.now += timedelta(seconds=2)
            return login.IssuedToken(TOKEN)
        self.issuer.side_effect = issue_after_delay
        self.assertTrue(self.session.login("real").success)
        self.assertEqual(self.session.next_renewal_at, datetime(2026, 10, 28, 0, 0, tzinfo=login.KST))
        self.assertIsNone(self.session.renew_if_due())

    def test_overdue_renewal_crossing_next_boundary_does_not_renew_twice(self):
        self.session.login("paper")
        # The process resumes the next morning just before another appointment.
        self.now = datetime(2026, 10, 27, 8, 54, 59, tzinfo=login.KST)
        def issue_after_delay(*_):
            self.now += timedelta(seconds=2)
            return login.IssuedToken(TOKEN)
        self.issuer.side_effect = issue_after_delay
        self.assertTrue(self.session.renew_if_due().success)
        self.assertEqual(self.session.next_renewal_at, datetime(2026, 10, 28, 8, 55, tzinfo=login.KST))
        self.assertIsNone(self.session.renew_if_due())
        self.assertEqual(self.issuer.call_count, 2)

    def test_request_reference_is_retained_if_clock_moves_backwards(self):
        self.now = datetime(2026, 10, 26, 8, 55, 1, tzinfo=login.KST)
        def issue_during_clock_adjustment(*_):
            self.now -= timedelta(seconds=3)
            return login.IssuedToken(TOKEN)
        self.issuer.side_effect = issue_during_clock_adjustment
        self.assertTrue(self.session.login("paper").success)
        self.assertEqual(self.session.next_renewal_at, datetime(2026, 10, 27, 8, 55, tzinfo=login.KST))

    def test_time_change_normalizes_persists_and_recalculates_next_due(self):
        self.session.login("paper")
        self.assertEqual(self.session.set_renewal(" 8:50 "), "08:50")
        self.save.assert_called_once_with("08:50")
        self.assertEqual(self.session.next_renewal_at, datetime(2026, 10, 27, 8, 50, tzinfo=login.KST))
        self.assertEqual(self.session.set_renewal("0:00"), "00:00")
        self.assertEqual(self.session.next_renewal_at, datetime(2026, 10, 27, 0, 0, tzinfo=login.KST))

    def test_save_failure_leaves_active_schedule_unchanged(self):
        self.session.login("paper")
        due = self.session.next_renewal_at
        self.save.side_effect = OSError("private-path")
        with self.assertRaises(login.LoginSessionError) as caught:
            self.session.set_renewal("9:00")
        self.assertNotIn("private-path", str(caught.exception))
        self.assertEqual(self.session.renewal_time, "08:55")
        self.assertEqual(self.session.next_renewal_at, due)

    def test_clear_removes_runtime_authentication_and_stops_renewal(self):
        self.session.login("paper")
        self.session.clear()
        self.assertFalse(self.session.authenticated)
        self.assertIsNone(self.session.token)
        self.assertIsNone(self.session.mode)
        self.assertIsNone(self.session.next_renewal_at)
        self.assertIsNone(self.session.renew_if_due())

    def test_terminal_prints_full_token_only_for_interactive_stdout(self):
        result = self.session.login("paper")
        terminal, log = io.StringIO(), io.StringIO()
        terminal.isatty = lambda: True
        log.isatty = lambda: False
        login.terminal_report(result, self.session, terminal)
        login.terminal_report(result, self.session, log)
        self.assertIn(TOKEN, terminal.getvalue())
        self.assertNotIn(TOKEN, log.getvalue())
        self.assertIn("로그인 성공", log.getvalue())
        failure = login.LoginResult(False, "paper", True, "갱신 실패")
        terminal.seek(0)
        terminal.truncate()
        login.terminal_report(failure, self.session, terminal)
        self.assertNotIn(TOKEN, terminal.getvalue())


class RealCredentialStorageTests(unittest.TestCase):
    def setUp(self):
        self.patcher = patch.object(real, "_get_backend")
        self.addCleanup(self.patcher.stop)
        self.factory = self.patcher.start()
        self.backend = self.factory.return_value

    def test_real_key_roundtrip_is_atomic_and_never_touches_mock_store(self):
        self.backend.update.return_value = 0
        real.save_real_credentials(PAIR)
        raw = self.backend.update.call_args.args[0]
        self.backend.load.return_value = (0, raw)
        self.assertEqual(real.load_real_credentials(), PAIR)
        self.backend.delete.assert_not_called()
        self.assertEqual(set(json.loads(raw)), {"version", "app_key", "app_secret"})

    def test_missing_real_key_is_not_replaced_with_mock_credentials(self):
        self.backend.load.return_value = (-25300, None)
        with patch("kiwoom_autotrader.credentials.load_mock_credentials") as mock:
            self.assertIsNone(real.load_real_credentials())
            mock.assert_not_called()

    def test_invalid_real_payload_is_sanitized_and_preserved(self):
        self.backend.load.return_value = (0, json.dumps({"version": True, "app_key": PAIR.app_key,
                                                       "app_secret": PAIR.app_secret}).encode())
        with self.assertRaises(KeychainError) as caught:
            real.load_real_credentials()
        self.assertNotIn(PAIR.app_secret, str(caught.exception))
        self.backend.update.assert_not_called()
        self.backend.delete.assert_not_called()

    def test_real_backend_name_is_distinct_and_allowed(self):
        from kiwoom_autotrader import credentials
        self.patcher.stop()
        with patch.object(credentials, "_get_backend") as native:
            real._get_backend()
            native.assert_called_once_with(service="com.local.kiwoom-autotrader.real", account="default")
        self.assertIn("com.local.kiwoom-autotrader.real", credentials._ALLOWED_SERVICES)


if __name__ == "__main__":
    unittest.main()
