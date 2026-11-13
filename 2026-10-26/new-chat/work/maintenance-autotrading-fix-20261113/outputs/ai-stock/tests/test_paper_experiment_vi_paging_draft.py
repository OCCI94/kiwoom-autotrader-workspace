"""Offline regression for complete, date-bound VI snapshots."""
from datetime import timedelta
from email.message import Message
import hashlib
import json
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch
from urllib.error import HTTPError

from kiwoom_autotrader.trading import paper_experiment_inputs as inputs
from kiwoom_autotrader.utils.http_queue import BufferedResponse
from tests.test_paper_experiment_inputs import NOW


def vi_row(symbol='005930', release=''):
    return {'stk_cd':symbol,'stex_tp':'KRX','virelis_time':release,
            'trde_cntr_proc_time':'095900','vimotn_cnt':'1','viaplc_tp':'동적'}


class ViPaginationDraftTests(TestCase):
    def setUp(self):
        self.clock=NOW
        self.session=SimpleNamespace(mode='paper',token='fixture-token')
        self.identity=('paper','fixture-a')
        session=patch.object(inputs,'session_identity',side_effect=lambda _:self.identity)
        session.start();self.addCleanup(session.stop)
        self.quote=Mock(side_effect=AssertionError('VI input must not request a quote'))
        self.client=inputs.ExperimentInputs(self.session,SimpleNamespace(),SimpleNamespace(),
            clock=lambda:self.clock,quote_fetcher=self.quote)
        self.fake=patch.object(inputs,'queued_open')
        self.send=self.fake.start();self.addCleanup(self.fake.stop)

    def response(self, body, *, cont='N', key='', api='ka10054', status=200):
        headers=Message()
        if cont is not None:headers['cont-yn']=cont
        if key:headers['next-key']=key
        if api:headers['api-id']=api
        raw=json.dumps(body,ensure_ascii=False).encode() if not isinstance(body,bytes) else body
        return BufferedResponse(raw,headers,status)

    def setup_pages(self, *responses):
        chain=iter(responses)
        def next_page(opener,request,*,before_send,priority,timeout,max_bytes):
            self.assertEqual(request.full_url,'https://mockapi.kiwoom.com/api/dostk/stkinfo')
            self.assertEqual(priority,'research')
            self.assertEqual(timeout,8)
            self.assertEqual(max_bytes,2*1024*1024)
            self.assertEqual(request.get_header('Api-id'),'ka10054')
            self.assertEqual(json.loads(request.data)['stex_tp'],'1')
            before_send()
            self.clock+=timedelta(seconds=1)
            value=next(chain)
            if isinstance(value,Exception):raise value
            return value
        self.send.side_effect=next_page

    def test_explicit_empty_is_verified_with_documented_optional_terminal_header(self):
        self.setup_pages(self.response({'return_code':'0','motn_stk':[]},cont=None))
        vi=self.client.vi_state('005930')
        self.assertIs(vi['vi_active'],False)
        self.assertTrue(vi['vi_complete'])
        self.assertEqual(self.client.vi_diagnostic['row_count'],0)
        self.quote.assert_not_called()

    def test_missing_null_malformed_nonzero_and_wrong_api_do_not_mean_empty(self):
        cases=[
            ({'return_code':0},'ka10054','vi_rows_unverified','missing'),
            ({'return_code':0,'motn_stk':None},'ka10054','vi_rows_unverified','null'),
            ({'return_code':0,'motn_stk':{}},'ka10054','vi_rows_unverified','other'),
            ({'return_code':901,'motn_stk':[],'return_msg':'fixture-secret'},'ka10054','vi_broker_status_unverified','list'),
            ({'return_code':False,'motn_stk':[]},'ka10054','vi_broker_status_unverified','list'),
            ({'motn_stk':[]},'ka10054','vi_broker_status_unverified','list'),
            ({'return_code':0,'motn_stk':[]},'ka10099','vi_response_api_unverified','list'),
        ]
        for body,api,reason,shape in cases:
            with self.subTest(reason=reason,shape=shape):
                self.setup_pages(self.response(body,api=api))
                self.assertIsNone(self.client.refresh_vi(force=True))
                diagnostic=self.client.vi_diagnostic
                self.assertEqual(diagnostic['reason'],reason)
                self.assertEqual(diagnostic['last_wire']['motn_stk_shape'],shape)
                self.assertNotIn('fixture-secret',str(diagnostic))
                self.assertIsNone(self.client.vi_state('005930')['vi_active'])

    def test_duplicate_json_and_transport_error_hold_without_raw_payload(self):
        cases=[(self.response(b'{"return_code":0,"motn_stk":[],"motn_stk":[]}'),'vi_response_json_invalid'),
               (self.response(b'{"return_code":0,"motn_stk":[],"x":NaN}'),'vi_response_json_invalid'),
               (HTTPError('fixture-secret',429,'fixture-secret',{},None),'vi_http_status_unverified')]
        for response,reason in cases:
            with self.subTest(reason=reason):
                self.setup_pages(response)
                self.assertIsNone(self.client.refresh_vi(force=True))
                self.assertEqual(self.client.vi_diagnostic['reason'],reason)
                self.assertNotIn('fixture-secret',str(self.client.vi_diagnostic))

    def test_paginated_source_keeps_first_observation_and_second_page_active(self):
        payloads=[{'return_code':0,'motn_stk':[vi_row('000660','095700')]},
                  {'return_code':'0','motn_stk':[vi_row()]}]
        self.setup_pages(self.response(payloads[0],cont='Y',key='opaque-second'),
                         self.response(payloads[1]))
        vi=self.client.vi_state('005930')
        self.assertTrue(vi['vi_complete'])
        self.assertTrue(vi['vi_active'])
        self.assertEqual(self.send.call_count,2)
        first,second=[c.args[1] for c in self.send.call_args_list]
        self.assertIsNone(first.get_header('Next-key'))
        self.assertEqual(second.get_header('Cont-yn'),'Y')
        self.assertEqual(second.get_header('Next-key'),'opaque-second')
        self.assertEqual(first.data,second.data)
        self.assertEqual(vi['vi_observed_at'],(NOW+timedelta(seconds=1)).isoformat())
        diagnostic=self.client.vi_diagnostic
        self.assertEqual(diagnostic['last_page_observed_at'],(NOW+timedelta(seconds=2)).isoformat())
        self.assertEqual(diagnostic['page_count'],2)
        self.assertNotIn('opaque-second',str(diagnostic))
        self.assertEqual([p['source_sha256'] for p in diagnostic['pages']],
            [hashlib.sha256(json.dumps(p,ensure_ascii=False).encode()).hexdigest() for p in payloads])

    def test_second_page_failure_discards_partial_clear_and_throttles_retry(self):
        self.setup_pages(self.response({'return_code':0,'motn_stk':[vi_row('000660')]},cont='Y',key='next'),
                         self.response({'return_code':0}))
        self.assertIsNone(self.client.refresh_vi())
        self.assertEqual(self.client.vi_diagnostic['pages_observed'],1)
        self.assertIsNone(self.client.vi_state('005930')['vi_active'])
        self.assertEqual(self.send.call_count,2)

    def test_ambiguous_headers_and_empty_continuation_hold(self):
        for cont,key,rows,reason in [
            ('Y','', [vi_row()], 'vi_continuation_unverified'),
            ('Y','x'*51,[vi_row()], 'vi_continuation_unverified'),
            ('N','contradiction', [], 'vi_continuation_unverified'),
            ('Y','next', [], 'vi_page_rows_unverified'),
            ('unknown','', [], 'vi_continuation_unverified'),
        ]:
            with self.subTest(cont=cont,key=key):
                self.setup_pages(self.response({'return_code':0,'motn_stk':rows},cont=cont,key=key))
                self.assertIsNone(self.client.refresh_vi(force=True))
                self.assertEqual(self.client.vi_diagnostic['reason'],reason)

    def test_cursor_cycle_and_event_overlap_hold(self):
        for second_row,second_key,reason in [
            (vi_row('000660'),'loop','vi_continuation_repeated'),
            (vi_row(release='095945'),'new','vi_page_overlap_unverified'),
            (vi_row(),'new','vi_page_overlap_unverified'),
        ]:
            with self.subTest(reason=reason):
                self.setup_pages(self.response({'return_code':0,'motn_stk':[vi_row()]},cont='Y',key='loop'),
                                 self.response({'return_code':0,'motn_stk':[second_row]},cont='Y',key=second_key))
                self.assertIsNone(self.client.refresh_vi(force=True))
                self.assertEqual(self.client.vi_diagnostic['reason'],reason)

    def test_caps_and_wrong_exchange_hold(self):
        self.setup_pages(*(self.response({'return_code':0,'motn_stk':[vi_row(f'{n+1:06d}')]},
                             cont='Y',key=f'page-{n}') for n in range(inputs._VI_MAX_PAGES)))
        self.assertIsNone(self.client.refresh_vi(force=True))
        self.assertEqual(self.client.vi_diagnostic['reason'],'vi_page_bound')
        self.assertEqual(self.send.call_count,inputs._VI_MAX_PAGES)
        for rows,reason in [([vi_row()]*2001,'vi_row_bound'),
                            ([dict(vi_row(),stex_tp='NXT')],'vi_row_scope_unverified')]:
            self.setup_pages(self.response({'return_code':0,'motn_stk':rows}))
            self.assertIsNone(self.client.refresh_vi(force=True))
            self.assertEqual(self.client.vi_diagnostic['reason'],reason)

    def test_stale_earliest_page_is_not_retimestamped_by_terminal_page(self):
        self.setup_pages(self.response({'return_code':0,'motn_stk':[vi_row()]},cont='Y',key='next'),
                         self.response({'return_code':0,'motn_stk':[vi_row('000660')]}))
        self.assertIsNotNone(self.client.refresh_vi())
        self.clock=NOW+timedelta(seconds=32)
        status=self.client.status()
        self.assertFalse(status['vi_complete'])
        self.assertEqual(status['vi_diagnostic']['reason'],'vi_evidence_expired')
        # Pin the read path to the existing cache and prove it cannot become clear.
        with patch.object(self.client,'refresh_vi',return_value=self.client.vi):
            self.assertFalse(self.client.vi_state('000660')['vi_complete'])

    def test_midday_session_switch_and_day_rollover_reject_sequence(self):
        for changes in ('session','day'):
            self.clock=NOW
            self.identity=('paper','fixture-a')
            def switch(*args,**kwargs):
                if changes=='session':self.identity=('paper','fixture-b')
                else:self.clock+=timedelta(days=1)
                return self.response({'return_code':0,'motn_stk':[]})
            self.send.side_effect=switch
            self.assertIsNone(self.client.refresh_vi(force=True))
            self.assertEqual(self.client.vi_diagnostic['reason'],'experiment_request_expired')

    def test_monotonic_deadline_binds_entire_sequence(self):
        self.setup_pages(self.response({'return_code':0,'motn_stk':[vi_row()]},cont='Y',key='next'))
        with patch.object(inputs.time,'monotonic',side_effect=[0,0,0,0,1,9]):
            self.assertIsNone(self.client.refresh_vi(force=True))
        self.assertEqual(self.send.call_count,1)
        self.assertEqual(self.client.vi_diagnostic['reason'],'vi_page_budget_exceeded')
