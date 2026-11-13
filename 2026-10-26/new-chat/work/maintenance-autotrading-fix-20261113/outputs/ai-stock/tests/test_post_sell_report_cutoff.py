"""Point-in-time checks for broker cost queries after a trading day ends."""
from copy import deepcopy
from datetime import datetime
import json
import unittest

from kiwoom_autotrader.trading.execution_log import _post_sell_report_state


DAY='20261029'
SELL='2026-10-29T14:00:00+09:00'
START='2026-10-30T00:01:00+09:00'
OBSERVED='2026-10-30T00:01:01+09:00'
CUTOFF=datetime.fromisoformat('2026-10-30T00:02:00+09:00')


def records():
    sell={'source_api':'00','mode':'paper','day':DAY,'event_kind':'execution_stream',
          'observed_at':SELL,'values':{'9001':'005930','907':'1','913':'체결',
          '9203':'1','909':'2','914':'10000','915':'5'}}
    items=[sell]
    for api in ('ka10072','ka10073'):
        request={'stk_cd':'005930','strt_dt':DAY}
        if api=='ka10073':request['end_dt']=DAY
        row={'stk_cd':'005930','crd_tp':'현금잔고','cntr_qty':'5','buy_uv':'9000',
             'cntr_pric':'10000','tdy_trde_cmsn':'10','tdy_trde_tax':'20','tdy_sel_pl':'4970'}
        if api=='ka10073':row['dt']=DAY
        field='dt_stk_div_rlzt_pl' if api=='ka10072' else 'dt_stk_rlzt_pl'
        items.append({'source_api':api,'mode':'paper','day':DAY,'schema':'kiwoom-broker-page-v1',
          'status':'success','http_status':200,'query_id':('a' if api=='ka10072' else 'b')*32,
          'page':1,'request_started_at':START,'observed_at':OBSERVED,'request_body':request,
          'response_body':{'return_code':0,field:[row]},
          'continuation':{'request_cont_yn':'N','response_cont_yn':'N','next_key_present':False}})
    return items


def evaluate(items, cutoff=CUTOFF):
    return _post_sell_report_state(DAY,'paper','005930',
              [{'payload_json':json.dumps(item,ensure_ascii=False)} for item in items],as_of=cutoff)


class PostSellReportCutoffTests(unittest.TestCase):
    def test_next_day_exact_trade_scope_and_post_sell_observation_is_current(self):
        state, rows=evaluate(records())
        self.assertEqual(state['status'],'reports_current')
        self.assertEqual(state['observed_sell_quantity'],5)
        self.assertEqual(len(rows),1)

    def test_future_or_clock_reversal_is_not_current(self):
        for start,received in [(START,'2026-10-30T00:03:00+09:00'),
                               ('2026-10-30T00:01:02+09:00',OBSERVED)]:
            items=records();items[1]['request_started_at']=start;items[1]['observed_at']=received
            self.assertEqual(evaluate(items)[0]['status'],'post_sell_report_pending')

    def test_wrong_request_day_and_period_row_day_are_not_current(self):
        a=records();a[1]['request_body']['strt_dt']='20261030'
        b=records();b[2]['response_body']['dt_stk_rlzt_pl'][0]['dt']='20261030'
        c=records();c[2]['request_body']['end_dt']='20261030'
        for items in (a,b,c):self.assertEqual(evaluate(items)[0]['status'],'post_sell_report_pending')

    def test_queries_before_latest_sell_or_future_sell_are_blocked(self):
        items=records();items[1]['request_started_at']='2026-10-29T13:59:00+09:00'
        items[1]['observed_at']='2026-10-29T13:59:01+09:00'
        self.assertEqual(evaluate(items)[0]['status'],'post_sell_report_pending')
        items=records();items[0]['observed_at']='2026-10-29T15:00:00+09:00'
        cutoff=datetime.fromisoformat('2026-10-29T14:59:59+09:00')
        self.assertNotEqual(evaluate(items,cutoff)[0]['status'],'reports_current')

    def test_old_query_does_not_replace_latest_failed_query(self):
        items=records(); newer=deepcopy(items[1]);newer['query_id']='c'*32
        newer['status']='failed';items.append(newer)
        self.assertEqual(evaluate(items)[0]['status'],'post_sell_report_pending')


if __name__=='__main__':unittest.main()
