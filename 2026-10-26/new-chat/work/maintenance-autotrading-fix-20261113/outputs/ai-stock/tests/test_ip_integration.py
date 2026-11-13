"""IP error propagation and command dispatch; no external discovery or trades."""

from datetime import datetime
import io
import json
import unittest
from unittest.mock import Mock, patch

from kiwoom_autotrader.api import errors, minute_chart, session as session_api
from kiwoom_autotrader.api.account_report import AccountReportError
from kiwoom_autotrader.api.client import KiwoomAPIError
from kiwoom_autotrader.api.orders import OrderError
from kiwoom_autotrader.api.rankings import RankingsError
from kiwoom_autotrader.api.session import BrokerSession, IssuedToken, KST, LoginSessionError
from kiwoom_autotrader.api.stock_info import StockInfoError
from kiwoom_autotrader.api.stock_names import StockNameError
from kiwoom_autotrader.telegram import listener
from kiwoom_autotrader.telegram.bot_credentials import TelegramConfig
from kiwoom_autotrader.telegram.commands import orders, rank_batch
from kiwoom_autotrader.telegram.commands.help import HELP
from kiwoom_autotrader.utils import public_ip
from kiwoom_autotrader.utils.command_scheduler import CommandSchedulerError, validate_command
from kiwoom_autotrader.utils.keychain import Credentials


SECRET = "offline-ip-integration-private-secret"
IP_REPLY = "현재 외부 공인 IP (IPv4): 8.8.8.8"
CONFIG = TelegramConfig("123456789:ip_integration_abcdefghijklmnopqrstuvwxyz", 778899001, 123456789, "ip_test_bot")


class IPIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.annotation = self.patch(errors, "annotate_ip_error", side_effect=lambda code, message: f"키움 IP 오류 {code}\n{IP_REPLY}")
        self.discovery = self.patch(public_ip, "get_public_ip", side_effect=AssertionError("External IP lookup forbidden"))
        self.network = self.patch_path("socket.socket.connect", side_effect=AssertionError("Network forbidden"))
        self.keychain = self.patch_path("kiwoom_autotrader.utils.keychain._get_backend", side_effect=AssertionError("Keychain forbidden"))
        self.session = BrokerSession(now=lambda: datetime(2026, 10, 26, 10, tzinfo=KST))
        self.session.mode = "paper"
        self.session._issued = IssuedToken("offline-private-broker-token", "20300101090000")

    def patch(self, owner, name, **kwargs):
        patcher = patch.object(owner, name, **kwargs)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def patch_path(self, target, **kwargs):
        patcher = patch(target, **kwargs)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def tearDown(self):
        self.discovery.assert_not_called()
        self.network.assert_not_called()
        self.keychain.assert_not_called()

    def test_confirmed_explicit_and_serialized_codes_are_annotated_once(self):
        cases = [("safe failure", 8010), ("safe failure", "8050"),
                 ("키움 조회 실패 (return_code=8010).", None),
                 ("키움 조회 실패 (returnCode=8050).", None)]
        for message, code in cases:
            with self.subTest(message=message, code=code):
                self.annotation.reset_mock()
                error = errors.BrokerAPIError(message, code=code)
                self.assertTrue(error.ip_related)
                self.assertIn(str(error.code), ("8010", "8050"))
                self.assertIn(IP_REPLY, str(error))
                self.annotation.assert_called_once_with(error.code, message)

    def test_unknown_http_auth_and_malformed_explicit_codes_do_not_annotate(self):
        for code in (None, False, True, 0, 401, 403, 500, 8005, 8040, 8103, 8052, -8010,
                     "08010", "8010 ", 8010.0, [], {}):
            with self.subTest(code=code):
                error = errors.BrokerAPIError("locally composed safe failure", code=code)
                self.assertFalse(error.ip_related)
                self.assertIsNone(error.code)
                self.assertEqual(str(error), "locally composed safe failure")
        self.annotation.assert_not_called()

    def test_malformed_serialized_codes_and_generic_ip_text_are_not_evidence(self):
        for message in ("return_code=80100", "return_code=-8010", "return_code=08010",
                        "return_code=8010.5", "returnCode=8050.0", "return_code=8010suffix",
                        "IP connection failed", "HTTP code=403", "8005 token expired", "IP not registered"):
            with self.subTest(message=message):
                error = errors.BrokerAPIError(message)
                self.assertFalse(error.ip_related)
                self.assertEqual(str(error), message)
        self.annotation.assert_not_called()

    def test_explicit_non_ip_code_overrides_a_different_code_in_safe_message(self):
        error = errors.BrokerAPIError("previous return_code=8010", code=8005)
        self.assertFalse(error.ip_related)
        self.annotation.assert_not_called()

    def test_all_broker_error_types_share_the_ip_diagnostic_boundary(self):
        classes = (LoginSessionError, StockInfoError, StockNameError, OrderError,
                   AccountReportError, RankingsError, KiwoomAPIError, minute_chart.MinuteChartError)
        for error_type in classes:
            with self.subTest(error_type=error_type.__name__):
                error = error_type("키움 요청 실패 (return_code=8050).")
                self.assertTrue(error.ip_related)
                self.assertIn(IP_REPLY, str(error))
        self.assertEqual(self.annotation.call_count, len(classes))

    def test_token_response_ip_error_survives_login_result_without_provider_message(self):
        raw = json.dumps({"return_code": 8050, "return_msg": SECRET}).encode()
        with patch.object(session_api, "queued_open", return_value=io.BytesIO(raw)), \
                patch.object(session_api.urllib.request, "build_opener"):
            with self.assertRaises(LoginSessionError) as caught:
                session_api.request_token("paper", Credentials("offline-key", SECRET))
        self.assertIn(IP_REPLY, str(caught.exception))
        self.assertNotIn(SECRET, str(caught.exception))
        session = BrokerSession(now=lambda: datetime(2026, 10, 26, 10, tzinfo=KST),
                                credential_loader=Mock(return_value=Credentials("offline-key", SECRET)),
                                token_issuer=Mock(side_effect=caught.exception))
        result = session.login("paper")
        self.assertFalse(result.success)
        self.assertIn(IP_REPLY, result.message)
        self.assertNotIn(SECRET, result.message)

    def test_minute_chart_uses_structured_ip_code_and_ignores_remote_text(self):
        with self.assertRaises(minute_chart.MinuteChartError) as caught:
            minute_chart._bars({"return_code": "8010", "return_msg": SECRET}, "005930")
        self.assertTrue(caught.exception.ip_related)
        self.assertIn(IP_REPLY, str(caught.exception))
        self.assertNotIn(SECRET, str(caught.exception))
        self.annotation.reset_mock()
        with self.assertRaises(minute_chart.MinuteChartError) as caught:
            minute_chart._bars({"return_code": "8010.5", "return_msg": SECRET}, "005930")
        self.assertFalse(caught.exception.ip_related)
        self.annotation.assert_not_called()

    def test_budget_quote_ip_failure_is_preserved_without_submitting_an_order(self):
        error = StockInfoError("종목정보 실패 (return_code=8050).")
        with patch.object(orders, "fetch_stock_info", side_effect=error), patch.object(orders, "submit_order") as submit:
            reply = "\n".join(orders.handle_buy("buy 005930 max 100000", self.session))
        self.assertIn(IP_REPLY, reply)
        submit.assert_not_called()

    def test_rank_batch_ip_failure_is_preserved_before_any_expansion_executes(self):
        error = RankingsError("순위 실패 (return_code=8010).")
        dispatch = Mock()
        with patch.object(rank_batch, "fetch_ranked_stocks", side_effect=error):
            reply = "\n".join(rank_batch.handle_rank_batch("rank 4 cmd 5 buy () max 100000", self.session, dispatch))
        self.assertIn(IP_REPLY, reply)
        dispatch.assert_not_called()

    def test_legacy_top_and_status_preserve_confirmed_ip_errors_only(self):
        for command in ("top", "status"):
            for error in (KiwoomAPIError("조회 실패 (return_code=8050)."), KiwoomAPIError(SECRET), RuntimeError(SECRET)):
                with self.subTest(command=command, error_type=type(error).__name__), \
                        patch.object(listener, "load_mock_credentials", return_value=Credentials("offline-key", SECRET)), \
                        patch.object(listener, "KiwoomMockClient") as client:
                    client.return_value.authenticate.side_effect = error
                    reply = listener._reply(command, CONFIG)
                    if getattr(error, "ip_related", False):
                        self.assertIn(IP_REPLY, reply)
                    else:
                        self.assertEqual(reply, listener._QUERY_ERROR)
                    self.assertNotIn(SECRET, reply)

    def test_ip_dispatch_trim_case_and_schedule_validation_are_pure_until_execution(self):
        for text in ("IP", " ip ", " \nIp\t"):
            self.assertEqual(listener._text_command(text, CONFIG), "ip")
        self.assertEqual(validate_command("  IP  "), "IP")
        with self.assertRaises(CommandSchedulerError):
            validate_command("IP extra")
        with patch.object(listener, "handle_ip", return_value=[IP_REPLY]) as handler:
            self.assertEqual(listener._dispatch("ip", " IP ", CONFIG, self.session, Mock()), [IP_REPLY])
        handler.assert_called_once_with()
        self.assertIn("IP — 현재 외부 공인 IP 확인", HELP)


if __name__ == "__main__":
    unittest.main()
