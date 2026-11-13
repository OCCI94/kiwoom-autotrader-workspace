"""No-network ka10080 transport and response contracts."""
from datetime import datetime
import json
import unittest
from unittest.mock import Mock, patch

from kiwoom_autotrader.api import minute_chart as chart
from kiwoom_autotrader.api.session import BrokerSession, IssuedToken, KST


class MinuteChartTests(unittest.TestCase):
    def setUp(self):
        self.session = BrokerSession(now=lambda: datetime(2026, 10, 26, 10, tzinfo=KST))
        self.session.mode = "paper"
        self.session._issued = IssuedToken("offline-private-chart-token", "20300101000000")
        patcher = patch.object(chart, "queued_open")
        self.queue = patcher.start()
        self.addCleanup(patcher.stop)
        self.response = self.queue.return_value.__enter__.return_value
        self.response.headers = {"api-id": "ka10080", "cont-yn": "N"}
        self.payload = {"return_code": 0, "stk_cd": "005930", "stk_min_pole_chart_qry": [
            {"cntr_tm": "20261026100200", "cur_prc": "-00200"},
            {"cntr_tm": "20261026100100", "cur_prc": "+00100"},
        ]}
        self.response.read.side_effect = lambda size: json.dumps(self.payload).encode()

    def test_exact_chart_contract_and_price_sign(self):
        bars = chart.fetch_minute_chart(self.session, "005930", count=2)
        self.assertEqual([bar.close for bar in bars], [100, 200])
        self.assertEqual(bars[0].start, datetime(2026, 10, 26, 10, 1, tzinfo=KST))
        request = self.queue.call_args.args[1]
        self.assertEqual(request.full_url, "https://mockapi.kiwoom.com/api/dostk/chart")
        self.assertEqual(request.get_header("Api-id"), "ka10080")
        self.assertEqual(request.get_header("Cont-yn"), "N")
        self.assertEqual(json.loads(request.data), {"stk_cd": "005930", "tic_scope": "1", "upd_stkpc_tp": "1"})
        self.assertEqual(self.queue.call_args.kwargs["max_bytes"], chart._MAX_RESPONSE_BYTES)
        self.queue.call_args.kwargs["before_send"]()

    def test_invalid_chart_response_is_rejected(self):
        base = self.payload.copy()
        for update in ({"return_code": True}, {"return_code": 9}, {"stk_cd": "000660"},
                       {"stk_min_pole_chart_qry": None},
                       {"stk_min_pole_chart_qry": [{"cntr_tm": "20261026100130", "cur_prc": "10"}]},
                       {"stk_min_pole_chart_qry": [{"cntr_tm": "20261026100100", "cur_prc": "0"}]},
                       {"stk_min_pole_chart_qry": [{"cntr_tm": "20261026100100", "cur_prc": "1.5"}]}):
            with self.subTest(update=update), self.assertRaises(chart.MinuteChartError):
                self.payload = {**base, **update}
                chart.fetch_minute_chart(self.session, "005930", count=2)

    def test_duplicate_candles_and_bad_continuation_are_rejected(self):
        self.payload["stk_min_pole_chart_qry"] *= 2
        with self.assertRaises(chart.MinuteChartError):
            chart.fetch_minute_chart(self.session, "005930", count=2)
        self.payload["stk_min_pole_chart_qry"] = []
        self.response.headers = {"cont-yn": "Y", "next-key": ""}
        with self.assertRaises(chart.MinuteChartError):
            chart.fetch_minute_chart(self.session, "005930", count=2)

    def test_failed_transport_is_not_retried_or_exposed(self):
        self.queue.side_effect = OSError("offline-private-chart-token")
        with self.assertRaises(chart.MinuteChartError) as error:
            chart.fetch_minute_chart(self.session, "005930")
        self.assertNotIn("offline-private-chart-token", str(error.exception))
        self.queue.assert_called_once()

    def test_session_and_expiry_are_checked_before_request_and_at_dispatch(self):
        chart.fetch_minute_chart(self.session, "005930", count=2)
        guard = self.queue.call_args.kwargs["before_send"]
        self.session.mode = "real"
        with self.assertRaises(chart.MinuteChartError):
            guard()
        self.queue.reset_mock()
        self.session._issued = IssuedToken("offline-private-chart-token", "20200101000000")
        with self.assertRaises(chart.MinuteChartError):
            chart.fetch_minute_chart(self.session, "005930")
        self.queue.assert_not_called()

    def test_session_change_during_response_rejects_data(self):
        def read(_):
            self.session.mode = "real"
            return json.dumps(self.payload).encode()
        self.response.read.side_effect = read
        with self.assertRaises(chart.MinuteChartError):
            chart.fetch_minute_chart(self.session, "005930", count=2)


if __name__ == "__main__":
    unittest.main()
