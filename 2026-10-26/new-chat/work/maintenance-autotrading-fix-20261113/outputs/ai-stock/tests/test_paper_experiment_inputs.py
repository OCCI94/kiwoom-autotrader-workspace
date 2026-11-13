"""Offline input-source boundary tests; no login, live session, or network."""
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

from kiwoom_autotrader.trading.paper_experiment_inputs import (
    ExperimentInputs, build_admission, common_tick, normalize_daily)

KST = ZoneInfo('Asia/Seoul')
NOW = datetime(2026,11,2,10,0,0,tzinfo=KST)


def daily_response():
    rows=[]
    for i in range(21,0,-1):
        day=(NOW.date()-timedelta(days=i)).strftime('%Y%m%d')
        rows.append({'dt':day,'open_pric':'100','high_pric':'110','low_pric':'90','cur_prc':'100',
            'trde_qty':'1000','trde_prica':'3000','upd_stkpc_tp':'','upd_rt':'','pred_pre_sig':'2'})
    return {'symbol':'005930','rows':rows,'source_sha256':'a'*64,'observed_at':NOW.isoformat()}


class ExperimentInputsTests(TestCase):
    def setUp(self):
        self.session=SimpleNamespace(mode='paper')
        self.provider=SimpleNamespace(_read_s5_risk=Mock(return_value=None))
        self.store=SimpleNamespace(selection=Mock(return_value=None))
        self.requester=Mock()
        self.quote=Mock(return_value={'stk_cd':'005930','cur_prc':'94','open_pric':'90','upl_pric':'100'})
        p=patch('kiwoom_autotrader.trading.paper_experiment_inputs.session_identity',return_value=('paper','fixture'))
        p.start();self.addCleanup(p.stop)
        self.inputs=ExperimentInputs(self.session,self.provider,self.store,clock=lambda:NOW,
                                     requester=self.requester,quote_fetcher=self.quote)
        self.inputs._bind()
        self.inputs.master['005930']={'common_share':True,'normal':True,'source_sha256':'b'*64,
                                     'available_at':NOW.isoformat(),'market':'0'}
        self.inputs.master_complete={'0','10'}
        self.inputs.daily_cache['005930']=normalize_daily(daily_response(),'005930',NOW)

    def test_daily_values_and_tick_boundaries(self):
        daily=self.inputs.daily_cache['005930']
        self.assertEqual(daily['avg_turnover20_krw'],3_000_000_000)
        self.assertEqual([common_tick(x) for x in (1999,2000,4999,5000,19999,20000,49999,50000,199999,200000,499999,500000)],
                         [1,5,5,10,10,50,50,100,100,500,500,1000])
        for mutation in ('missing','duplicate','event'):
            response=daily_response()
            if mutation=='missing':response['rows'].pop()
            if mutation=='duplicate':response['rows'][-1]['dt']=response['rows'][-2]['dt']
            if mutation=='event':response['rows'][-1]['upd_stkpc_tp']='1'
            with self.subTest(mutation=mutation),self.assertRaises(ValueError):normalize_daily(response,'005930',NOW)

    def test_prior_upper_limit_unknown_is_not_false(self):
        for value, expected in [('1',True),('2',False),('3',False),('4',False),('5',False),('',None),(None,None),('9',None)]:
            response=daily_response();response['rows'][-1]['pred_pre_sig']=value
            with self.subTest(value=value):
                self.assertIs(normalize_daily(response,'005930',NOW)['previous_limit_up'],expected)

    def result(self, rows, cont='N'):
        stamp=self.inputs.now().isoformat()
        return {'rows':rows,'source_sha256':'c'*64,'observed_at':stamp,
                'request_started_at':stamp,'cont_yn':cont,'next_key':''}

    def test_complete_no_vi_and_valid_sources_build_admission(self):
        self.requester.return_value=self.result([])
        market=self.inputs.market('005930')
        self.assertIs(market['vi_active'],False)
        self.assertTrue(build_admission('005930','EXP_ORB_A',market,self.inputs.daily('005930'),NOW))
        self.requester.assert_called_once()

    def test_truncated_or_failed_vi_is_unknown_not_clear(self):
        for result in (self.result([],cont='Y'),RuntimeError('offline')):
            self.inputs.vi_attempt_at=None;self.inputs.vi=None;self.inputs.quotes={}
            self.requester.side_effect=result if isinstance(result,Exception) else None
            self.requester.return_value=result
            market=self.inputs.market('005930',force=True)
            self.assertIsNone(market['vi_active']);self.assertIs(market['vi_complete'],False)
            with self.assertRaises(ValueError):build_admission('005930','EXP_ORB_A',market,self.inputs.daily('005930'),NOW)

    def test_unreleased_or_future_release_is_never_treated_as_clear(self):
        for release in ('','100100','garbled'):
            self.inputs.vi_attempt_at=None;self.inputs.vi=None;self.inputs.quotes={}
            self.requester.return_value=self.result([{'stk_cd':'005930','trde_cntr_proc_time':'095700','virelis_time':release}])
            market=self.inputs.market('005930',force=True)
            with self.subTest(release=release):self.assertIsNot(market['vi_active'],False)

    def test_vi_state_needs_no_quote_and_waits_120_seconds_after_release(self):
        for release,active in [('095801',True),('095800',False),('100100',True)]:
            self.inputs.vi_attempt_at=None;self.inputs.vi=None
            self.requester.return_value=self.result([{'stk_cd':'A005930','trde_cntr_proc_time':'095700','virelis_time':release}])
            state=self.inputs.vi_state('005930')
            with self.subTest(release=release):
                self.assertIs(state['vi_active'],active)
                self.assertEqual(state['vi_last_trigger_basis'],'release_time_conservative_120s_not_trigger_assertion')
                self.assertEqual(state['vi_processing_times'],['095700'])
        self.quote.assert_not_called()

    def test_vi_unknown_for_unparseable_rows_and_stale_observations(self):
        for response in (self.result([{'stk_cd':'unparseable'}]),
                         dict(self.result([]),observed_at=(NOW-timedelta(seconds=31)).isoformat())):
            self.inputs.vi_attempt_at=None;self.inputs.vi=None;self.requester.return_value=response
            state=self.inputs.vi_state('005930')
            self.assertIsNone(state['vi_active']);self.assertFalse(state['vi_complete'])

    def test_real_mode_stops_before_fetch(self):
        self.session.mode='real'
        with self.assertRaises(ValueError):self.inputs.market('005930')
        self.requester.assert_not_called();self.quote.assert_not_called()

    def test_master_partial_page_never_asserts_common_stock(self):
        self.inputs.master={};self.inputs.master_complete=set();self.inputs.master_cursor={'0':('',0),'10':('',0)}
        row={'code':'005930','name':'회사','marketCode':'0','auditInfo':'정상','state':'정상',
             'orderWarning':'0','upSizeName':'대형주','companyClassName':'보통주'}
        self.requester.side_effect=[dict(self.result([row],cont='Y'),next_key='page2'),self.result([])]
        self.inputs.prepare_one(['005930'])
        self.inputs.vi=self.result([]);self.inputs.vi_attempt_at=NOW
        market=self.inputs.market('005930',force=True)
        self.assertIsNot(market.get('is_common_stock'),True)
        self.inputs.prepare_one(['005930'])
        market=self.inputs.market('005930',force=True)
        self.assertIs(market.get('is_common_stock'),True)

    def test_duplicate_master_symbol_never_asserts_common_stock(self):
        self.inputs.master={};self.inputs.master_complete=set();self.inputs.master_cursor={'0':('',0),'10':('',0)}
        row={'code':'005930','name':'회사','marketCode':'0','auditInfo':'정상','state':'정상',
             'orderWarning':'0','upSizeName':'대형주','companyClassName':'보통주'}
        self.requester.side_effect=[dict(self.result([row],cont='Y'),next_key='page2'),self.result([dict(row)])]
        self.inputs.prepare_one(['005930']);self.inputs.prepare_one(['005930'])
        self.inputs.vi=self.result([]);self.inputs.vi_attempt_at=NOW
        self.assertIsNot(self.inputs.market('005930',force=True).get('is_common_stock'),True)
