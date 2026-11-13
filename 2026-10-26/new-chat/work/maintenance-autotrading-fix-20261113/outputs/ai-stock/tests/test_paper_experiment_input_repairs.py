"""Real failure shapes: unfinished ETF bars, unknown VI, cached selection."""
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
import json
from pathlib import Path
import unittest
from unittest.mock import Mock

from tests import test_paper_experiment_monitor as monitor_fixture
from tests import test_paper_experiment_extended_monitor as extended_monitor_fixture
from tests import test_paper_experiment_extended_rules as extended_fixture
from tests import test_paper_experiment_scheduler as scheduler_fixture
from tests import test_paper_experiment_inputs as inputs_fixture
from tests import test_paper_experiment_s5 as s5_fixture
from kiwoom_autotrader.trading import paper_experiment_extended_rules as rules
from kiwoom_autotrader.trading.paper_experiment_disclosure import read_s5_risk_diagnostics
from kiwoom_autotrader.trading.paper_experiment_selection_audit import activation_contract


class CompletedExtendedBarsTests(unittest.TestCase):
    def test_max_session_s5_order_proof_includes_prospective_prefix_and_theme_under64kb(self):
        from kiwoom_autotrader.trading import paper_experiment_rules as native
        from kiwoom_autotrader.trading.paper_experiment_theme import theme_context,bind_assignment_theme
        from kiwoom_autotrader.trading.paper_experiment_s5 import build_s5_native_evidence
        from kiwoom_autotrader.trading.decision_evidence import freeze_evidence
        n=s5_fixture.NativeS5MonitorTests('runTest');n.setUp();self.addCleanup(n.doCleanups)
        at=s5_fixture.fixture(374)[0]
        _,bars,market=s5_fixture.fixture(374)
        bars=[replace(bar,source_symbol=n.symbol) for bar in bars]
        market=deepcopy(market);market['symbol']=n.symbol
        for key in ('s5_daily_context','s5_risk_context'):market[key]['symbol']=n.symbol
        market['s5_risk_context']['short_selling_evidence']['symbol']=n.symbol
        prefix=[]
        for i in range(36):
            old=native.assign_candidate(n.value['day'],f'{990000+i:06d}',n.value['first_seen_at'],
                assigned_at=n.value['assigned_at'],profile='native-six-s5-v1')
            prefix.append({k:old[k] for k in ('symbol','arm','assigned_at','assignment_sha256')})
            prefix[-1]['arm']='EXP_R' if i<6 else 'EXP_A' if i<12 else 'EXP_B'
        assignment=native.assign_prospective_candidate(n.value['day'],n.symbol,n.value['first_seen_at'],
            assigned_at=n.value['assigned_at'],prior_allocations=prefix,profile='native-six-s5-v1')
        self.assertEqual(assignment['arm'],n.value['arm'])
        taxonomy=Path(n.c.temp.name)/'theme.json'
        taxonomy.write_text(json.dumps({'schema':'pro6e-sector-taxonomy-v1','metadata_only':True,
            'created_at':'2026-11-01T12:00:00+09:00','kr_stocks':{n.symbol:{'sub_themes':['보안·사이버']}}}))
        context=theme_context(n.symbol,native._time(assignment['assigned_at']),path=taxonomy)
        assignment=bind_assignment_theme(assignment,context)
        proof=build_s5_native_evidence(bars,market,n.symbol,at)
        evidence=n.c.monitor._evidence(assignment,{'action':'buy','reason':'steady_uptrend_vwap',
            'signal_at':proof['signal_at'],'s5_native_proof':proof},bars,at,budget=200000)
        frozen=freeze_evidence(evidence)
        self.assertLess(len(frozen.json.encode()),65536)
        self.assertEqual(frozen.as_dict()['config']['experiment_assignment']['theme_context']['theme'],'보안')
        self.assertEqual(len(frozen.as_dict()['config']['experiment_assignment']['prior_allocations']),36)

    def test_etf_current_unfinished_row_removed_but_actual_signal_rows_preserved(self):
        e=extended_monitor_fixture.ExtendedMonitorTests('runTest');e.setUp();self.addCleanup(e.doCleanups)
        f=e.f;f.clock=extended_fixture.START+timedelta(minutes=36);symbol='069500'
        value=rules.etf_assignment(extended_fixture.DAY,symbol,extended_fixture.START,f.clock);e.wire(symbol=symbol)
        completed=extended_fixture.bars([1000]*30+[990]*5+[1100],f.clock,symbol=symbol,volumes=[100]*35+[1000])
        raw=completed+[replace(completed[-1],start=f.clock,close=1,open_price=1,low_price=1,high_price=1)]
        f.chart.side_effect=None;f.chart.return_value=raw
        f.one(value)
        self.assertEqual(len(raw),37)
        self.assertEqual(f.orders,[('buy',symbol,200,1000)])
        frozen=f.contexts[-1][1]['evidence']
        self.assertTrue(all(b['start']!=f.clock.isoformat() for b in frozen['bars']))

    def test_future_row_is_rejected_not_silently_dropped_as_unfinished(self):
        e=extended_monitor_fixture.ExtendedMonitorTests('runTest');e.setUp();self.addCleanup(e.doCleanups)
        f=e.f;f.clock=extended_fixture.START+timedelta(minutes=36);symbol='069500'
        value=rules.etf_assignment(extended_fixture.DAY,symbol,extended_fixture.START,f.clock);e.wire(symbol=symbol)
        data=extended_fixture.bars([1000]*36,f.clock,symbol=symbol)
        f.chart.side_effect=None;f.chart.return_value=data+[replace(data[-1],start=f.clock+timedelta(minutes=1))]
        with self.assertRaisesRegex(ValueError,'extended_future_session_bar'):f.one(value)
        self.assertEqual(f.orders,[])

    def test_gdr_missing_vi_is_explicit_data_absence_not_kst_conversion_error(self):
        row,at=extended_fixture.candidate('EXP_GDR',vi_active=None,vi_complete=False,
            vi_observed_at=None,vi_source_sha256=None)
        result=rules.select_candidates('EXP_GDR',[row],day=extended_fixture.DAY,selected_at=at)
        self.assertEqual(result['assignments'],[])
        self.assertEqual(result['rejected'][0]['reason'],'vi_evidence_unavailable')
        self.assertNotIn('kst_time_required',str(result))


class SelectionCacheTests(unittest.TestCase):
    def test_quote_prefetch_bounded_original_timestamp_and_order_freshness_not_relaxed(self):
        case=inputs_fixture.ExperimentInputsTests('runTest');case.setUp();self.addCleanup(case.doCleanups)
        inp=case.inputs;clock=[inputs_fixture.NOW.replace(hour=9,minute=4,second=20)];inp.now=lambda:clock[0]
        case.requester.return_value={**case.result([]),'observed_at':clock[0].isoformat()}
        self.assertTrue(inp.prepare_selection_one(['005930','000660']))
        old=deepcopy(inp.quotes['005930']);self.assertEqual(case.quote.call_count,1)
        clock[0]+=timedelta(seconds=50)
        selected=inp.selection_market('005930',clock[0]);self.assertEqual(selected['observed_at'],old['observed_at'])
        self.assertEqual(case.quote.call_count,1)
        case.requester.return_value={**case.result([]),'observed_at':clock[0].isoformat()}
        current=inp.market('005930',force=True)
        self.assertEqual(current['observed_at'],clock[0].isoformat());self.assertEqual(case.quote.call_count,2)
        self.assertFalse(inp.prepare_selection_one(['000660']))

    def test_cached_selection_quotes_allow_entire20symbol_pool_without_hidden_extra_fetch(self):
        case=scheduler_fixture.SchedulerTests('runTest');case.setUp();self.addCleanup(case.doCleanups)
        symbols=[f'{i:06d}' for i in range(20)];case.first_seen(symbols);case.chart_delay=1
        original_at=case.clock()-timedelta(seconds=30)
        quotes={s:extended_fixture.context(original_at,s)['market'] for s in symbols}
        case.inputs.selection_market=lambda s,now:deepcopy(quotes[s])
        case.inputs.market=Mock(side_effect=AssertionError('unexpected live quote'))
        case.scheduler.run_selection(None,symbols,scheduler_fixture.Tracker(),case.clock())
        result=case.store.selection('20261102','extended:ORB')
        self.assertEqual(result['candidate_count'],20);self.assertEqual(result['unqueried_symbols'],[])
        case.inputs.market.assert_not_called()
        self.assertEqual(len(case.chart_calls),20)
        self.assertTrue(all(v['context']['market']['observed_at']==original_at.isoformat()
            for v in [{'context':{'market':quotes[s]}} for s in symbols]))

    def test_vi_failure_records_sanitized_reason_not_exception_secret(self):
        case=inputs_fixture.ExperimentInputsTests('runTest');case.setUp();self.addCleanup(case.doCleanups)
        case.requester.side_effect=RuntimeError('private-token-not-public')
        self.assertIsNone(case.inputs.refresh_vi())
        status=case.inputs.status()['vi_diagnostic']
        self.assertEqual(status['reason'],'RuntimeError');self.assertFalse(status['complete'])
        self.assertNotIn('private-token',str(status))

    def test_orb_activation_date_unknown_until_actual14sessions_not_weekday_guess(self):
        result=activation_contract('EXP_ORB_A')
        self.assertEqual(result['required_saved_same_window_sessions'],14)
        self.assertIsNone(result['expected_activation_date'])
        self.assertEqual(result['before_14_session_policy'],'hold_without_daily_volume_proxy')
        self.assertTrue(result['scheduled_collection_is_not_saved_data'])


class S5RiskDiagnosticTests(unittest.TestCase):
    def test_actual_short_major_and_coverage_holds_are_not_released_or_all_called_dart_transport(self):
        n=s5_fixture.NativeS5MonitorTests('runTest');n.setUp();self.addCleanup(n.doCleanups);f=n.c
        path=Path(f.temp.name)/'risk.json';f.provider._s5_risk_path=path
        n.market['s5_risk_context']=None
        f.provider.fetch_s5_inputs.return_value={k:n.market[k] for k in ('s5_daily_context','s5_risk_context')}
        cases=[(['SHORT_SELLING_FIVE_DAY_MAX_OVER_10_PERCENT'],'PASS','s5_short_selling_pressure_high'),
            (['PRIOR_UNRESOLVED_MAJOR_RISK','SHORT_SELLING_FIVE_DAY_MAX_OVER_10_PERCENT'],'HOLD','s5_unresolved_major_disclosure_risk'),
            (['DART_LIST_NOT_VERIFIED','SHORT_SELLING_FIVE_SESSIONS_UNVERIFIED'],'PENDING','s5_disclosure_list_unverified')]
        for reasons,disclosure,expected in cases:
            doc={'schema':'toss-hybrid-risk-evidence-v1','generated_at':f.clock.isoformat(),
                'expires_at':(f.clock+timedelta(hours=1)).isoformat(),'rows':[],
                'evaluations':[{'symbol':n.symbol,'status':'HOLD','reasons':reasons,
                    'short_max_share':'0.12','disclosure':{'status':disclosure,'reasons':reasons}}]}
            path.write_text(json.dumps(doc),encoding='utf-8')
            detail=read_s5_risk_diagnostics(path,n.symbol,f.clock)
            self.assertEqual(detail['reason'],expected);self.assertFalse(detail['order_permission'])
            diagnostic={'reason':'dart_risk_review_unavailable','eligible':False,'order_permission':False}
            f.monitor._explain_s5_risk(diagnostic,n.symbol,f.clock)
            self.assertEqual(diagnostic['reason'],expected);self.assertFalse(diagnostic['eligible'])
            self.assertEqual(diagnostic['legacy_reason'],'dart_risk_review_unavailable')
        f.one(n.value)
        self.assertEqual(f.orders,[])
        self.assertEqual(f.monitor.diagnostics[n.symbol]['reason'],'s5_disclosure_list_unverified')

    def test_expired_missing_scope_or_duplicate_evaluations_never_clear(self):
        n=s5_fixture.NativeS5MonitorTests('runTest');n.setUp();self.addCleanup(n.doCleanups);f=n.c
        path=Path(f.temp.name)/'risk.json'
        row={'symbol':n.symbol,'status':'ELIGIBLE','reasons':[],'disclosure':{'status':'PASS'}}
        doc={'schema':'toss-hybrid-risk-evidence-v1','generated_at':f.clock.isoformat(),
            'expires_at':(f.clock+timedelta(hours=1)).isoformat(),'evaluations':[row,row]}
        path.write_text(json.dumps(doc));detail=read_s5_risk_diagnostics(path,n.symbol,f.clock)
        self.assertEqual(detail['status'],'UNAVAILABLE');self.assertFalse(detail['order_permission'])
        doc['evaluations']=[row];doc['expires_at']=f.clock.isoformat();path.write_text(json.dumps(doc))
        self.assertEqual(read_s5_risk_diagnostics(path,n.symbol,f.clock)['reason'],'s5_risk_evidence_expired_or_future')
