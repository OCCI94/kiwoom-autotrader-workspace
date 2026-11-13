"""An in-progress server snapshot cannot become a completed candle by delay."""
from datetime import datetime
import json
from unittest import TestCase
from unittest.mock import patch

from kiwoom_autotrader.api import minute_chart
from kiwoom_autotrader.api.minute_chart import fetch_minute_chart
from kiwoom_autotrader.api.session import BrokerSession, IssuedToken, KST
from kiwoom_autotrader.trading.cross_monitor import MinuteWindowError, closed_window
from kiwoom_autotrader.utils.http_queue import BufferedResponse


class CaptureTimeTests(TestCase):
    def setUp(self):
        self.session=BrokerSession(now=lambda:datetime(2026,10,27,10,5,5,tzinfo=KST))
        self.session.mode='paper'
        self.session._issued=IssuedToken('offline-chart-capture-token','20300101000000')

    def capture(self, rows, query_start):
        payload={'return_code':0,'stk_cd':'003220','stk_min_pole_chart_qry':list(reversed(rows))}
        response=BufferedResponse(json.dumps(payload).encode(),{'api-id':'ka10080','cont-yn':'N'},200)
        with patch.object(minute_chart,'_query_now',return_value=query_start,create=True), \
             patch.object(minute_chart,'queued_open',return_value=response):
            return fetch_minute_chart(self.session,'003220',count=3)

    def row(self, minute, close, volume='1', cumulative='1'):
        return {'cntr_tm':f'2026102710{minute:02d}00','cur_prc':str(close),'trde_qty':volume,
                'acc_trde_qty':cumulative,'upd_stkpc_tp':'','upd_rt':''}

    def test_request_before_close_cannot_become_completed_after_response_delay(self):
        bars=self.capture([self.row(2,100),self.row(3,99),self.row(4,80)],
                          datetime(2026,10,27,10,4,59,tzinfo=KST))
        # The 10:04 row was still forming when the broker request began.
        with self.assertRaises(MinuteWindowError):
            closed_window(bars,datetime(2026,10,27,10,5,1,tzinfo=KST),3)

    def test_unclosed_right_anchor_cannot_prove_zero_volume_gap(self):
        bars=self.capture([self.row(0,100,'5','5'),self.row(1,99,'5','10'),
                           self.row(3,98,'3','13')],
                          datetime(2026,10,27,10,3,59,tzinfo=KST))
        with self.assertRaises(MinuteWindowError):
            closed_window(bars,datetime(2026,10,27,10,4,1,tzinfo=KST),3)

    def test_request_after_close_can_use_recent_completed_rows(self):
        bars=self.capture([self.row(2,100),self.row(3,99),self.row(4,98)],
                          datetime(2026,10,27,10,5,1,tzinfo=KST))
        window=closed_window(bars,datetime(2026,10,27,10,5,5,tzinfo=KST),3)
        self.assertEqual([bar.close for bar in window],[100,99,98])
