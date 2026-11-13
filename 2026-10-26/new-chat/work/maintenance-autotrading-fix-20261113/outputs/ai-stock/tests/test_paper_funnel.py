"""Opt-in broad radar and shared entry observations; fake broker/ledger only."""
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace
import tempfile
from pathlib import Path
import json
from unittest import TestCase
from unittest.mock import patch

from kiwoom_autotrader.config.settings import (default_settings,_validate,SettingsError,
    effective_paper_funnel,effective_theme_unclassified_policy,effective_paper_scan,
    save_settings,load_settings,paper_strategy_options_path)
from kiwoom_autotrader.trading.paper_funnel_state import publish_gate,read_gate,shared_chart,mark_precision_ready,precision_ready_symbols
from kiwoom_autotrader.trading.multi_strategy import STRATEGIES
import tests.test_dynamic_candidates as candidate_fixture
import tests.test_dynamic_monitor as monitor_fixture


def book_at(now,symbol='005930'):
    return {'symbol':symbol,'mode':'paper','source':'ka10004_observed_top1','wire_sha256':'b'*64,
        'request_started_at':now.isoformat(),'available_at':now.isoformat(),
        'quote_time_inferred_kst':now.replace(microsecond=0).isoformat(),
        'source_date_verified':False,'ask':101,'bid':100,'ask_size':500,'bid_size':600}


class FunnelSettingsTests(TestCase):
    def test_default_disabled_real_off_and_separate_sidecar_bytes(self):
        value=default_settings()
        self.assertFalse(value['paper_funnel_enabled']);self.assertEqual(value['paper_funnel_hot_limit'],10)
        self.assertEqual(value['paper_theme_unclassified_policy'],'block')
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)/'settings.json';original=b'{"gdcrs_enabled":true}\n';path.write_bytes(original)
            value=save_settings({'paper_funnel_enabled':True,'paper_funnel_hot_limit':15,
                'paper_funnel_priority_symbols':['491000'],'paper_theme_unclassified_policy':'shared_bucket'},path)
            self.assertEqual(path.read_bytes(),original)
            self.assertEqual(effective_paper_scan(value,'paper')['max_positions'],8)
            self.assertTrue(effective_paper_funnel(value,'paper')['enabled'])
            self.assertFalse(effective_paper_funnel(value,'real')['enabled'])
            self.assertEqual(effective_theme_unclassified_policy(value,'real'),'block')
            self.assertEqual(effective_theme_unclassified_policy(value,'paper'),'shared_bucket')
            self.assertEqual(load_settings(path),value)
            self.assertTrue(json.loads(paper_strategy_options_path(path).read_text())['paper_funnel_enabled'])

    def test_strict_options_and_default_lists_are_independent(self):
        for update in ({'paper_funnel_enabled':1},{'paper_funnel_hot_limit':True},
                {'paper_funnel_hot_limit':0},{'paper_funnel_hot_limit':16},
                {'paper_funnel_priority_symbols':['491000','491000']},
                {'paper_funnel_priority_symbols':['unverified']},
                {'paper_theme_unclassified_policy':'infer'}, {'paper_theme_unclassified_policy':None}):
            with self.subTest(update=update),self.assertRaises(SettingsError):
                _validate({**default_settings(),**update})
        value=default_settings();value['paper_funnel_priority_symbols'].append('491000')
        self.assertEqual(default_settings()['paper_funnel_priority_symbols'],[])


class FunnelProviderTests(TestCase):
    def setUp(self):
        self.fx=candidate_fixture.DynamicCandidateTests('test_bounded_current_evidence_catalog_staggers_and_deepcopy')
        self.fx.setUp();self.addCleanup(self.fx.doCleanups)

    def test_broad_verified_non_taxonomy_symbol_and_rank_cache_counts(self):
        fx=self.fx
        fx.members['0'].append(fx.member('491000'))
        fx.surge_rows.append({'stk_cd':'491000','flu_rt':'28','sdnin_rt':'100','trde_pre':'-90'})
        fx.turnover_rows.append({'stk_cd':'491000','trde_prica':'999','trde_pre':'-90'})
        first=fx.provider.discover_funnel_candidates(fx.now,configured_symbols=['005930'])
        self.assertEqual(first['schema'],'paper-funnel-candidates-v1')
        self.assertEqual(first['candidates'][0]['symbol'],'491000')
        self.assertNotIn('491000',json.loads(fx.taxonomy.read_text())['kr_stocks'])
        self.assertEqual(first['candidates'][0]['turnover_krw'],999_000_000)
        self.assertFalse(first['taxonomy_is_membership_proof'])
        again=fx.provider.discover_funnel_candidates(fx.now)
        self.assertEqual((fx.calls['surge'],fx.calls['turnover'],fx.calls['membership']),(1,1,['0']))
        self.assertEqual(again['request_accounting']['ka10099'],1)
        self.assertEqual(again['request_accounting']['ka10023'],1)
        self.assertEqual(again['request_accounting']['per_symbol_market_calls_in_discovery'],0)
        self.assertEqual(again['candidates'][0]['liquidity_evidence']['surge_now_trde_qty'],10000)

    def test_turnover_request_explicitly_excludes_managed_symbols(self):
        with patch.object(self.fx.provider,'_ranking_page',return_value={}) as page:
            self.fx.provider._fetch_turnover_page()
        self.assertEqual(page.call_args.args[1]['mang_stk_incls'],'0')

    def test_core_and_configured_priority_cannot_override_four_percent_or_membership(self):
        fx=self.fx
        for change in ('3.99','0','-29.93','28.01'):
            fx.now+=timedelta(minutes=1);fx.surge_rows[0]['flu_rt']=change
            result=fx.provider.discover_funnel_candidates(fx.now,configured_symbols=['005930'])
            self.assertNotIn('005930',[r['symbol'] for r in result['candidates']])
        fx.now+=timedelta(minutes=1);fx.surge_rows[0]['flu_rt']='4'
        result=fx.provider.discover_funnel_candidates(fx.now)
        self.assertIn('005930',[r['symbol'] for r in result['candidates']])
        fx.now+=timedelta(minutes=6);fx.members['0']=[]
        self.assertNotIn('005930',[r['symbol'] for r in fx.provider.discover_funnel_candidates(fx.now)['candidates']])

    def test_all_candidates_reach_memory_filter_before_hot_limit(self):
        fx=self.fx
        codes=[f'{i:06d}'for i in range(1000,1050)]
        fx.members['0']=[fx.member(c) for c in codes]
        fx.surge_rows=[{'stk_cd':c,'flu_rt':'10','sdnin_rt':'1'}for c in codes]
        fx.turnover_rows=[{'stk_cd':c,'trde_prica':str(i+1)}for i,c in enumerate(codes)]
        result=fx.provider.discover_funnel_candidates(fx.now)
        self.assertEqual(len(result['candidates']),50)
        self.assertFalse(result['candidate_limit_truncated'])
        self.assertEqual(result['candidates'][0]['symbol'],codes[-1])

    def test_missing_actual_volume_never_uses_signed_trade_comparison(self):
        fx=self.fx;fx.surge_rows[0].update(now_trde_qty='',trde_pre='300')
        result=fx.provider.discover_funnel_candidates(fx.now)
        self.assertNotIn('005930',[r['symbol']for r in result['candidates']])
        self.assertIn('current_volume_unverified',[r['reason']for r in result['exclusions']])


class FunnelMonitorTests(TestCase):
    def setUp(self):
        self.fx=monitor_fixture.DynamicMonitorTests('test_new_symbol_starts_baseline_then_signal_and_half_budget')
        self.fx.setUp();self.addCleanup(self.fx.doCleanups)
        fx=self.fx;fx.options.update(paper_funnel_enabled=True,paper_funnel_hot_limit=10)
        self.occupied=[];self.themes={};self.limit=8;self.snapshot_calls=0
        self.rows=[self.row('000660')];test=self
        class Provider:
            def discover_funnel_candidates(self,as_of=None,configured_symbols=()):
                fx.calls.append(('funnel',configured_symbols))
                return {'schema':'paper-funnel-candidates-v1','status':'ready','available_at':fx.now.isoformat(),
                    'session_binding':fx.binding,'candidates':deepcopy(test.rows),'request_accounting':{'ka10023':1,'ka10032':1}}
            def fetch(self,symbol,as_of=None):
                fx.calls.append(('quote',symbol))
                return {'symbol':symbol,'change_pct':fx.change,'current_price':100,'upper_limit_price':130}
            def fetch_orderbook(self,symbol,as_of=None,before_send=None):
                if before_send:before_send()
                fx.calls.append(('book',symbol))
                return book_at(fx.now,symbol)
            def is_orderbook_fresh(self,book,now):
                from kiwoom_autotrader.api.order_book import observed_book_is_fresh
                return observed_book_is_fresh(book,now)
            def is_fresh(self,*a):return True
        fx.monitor._provider_factory=lambda _:Provider()
        fx.tracker.paper_theme_capacity_snapshot=self.snapshot
        self.shared_patch=patch('kiwoom_autotrader.trading.paper_funnel_state.session_identity',return_value=('paper','fixture'))
        self.shared_patch.start();self.addCleanup(self.shared_patch.stop)

    def row(self,code,amount=10000000):
        return {'symbol':code,'available_at':self.fx.now.isoformat(),'change_pct':'10',
            'surge_rate_pct':'100','turnover_krw':amount,'membership_evidence':{'source':'ka10099'},
            'source_hashes':{'membership':'a'*64},'liquidity_evidence':{'actual_volume':100}}

    def snapshot(self,now):
        self.snapshot_calls+=1
        return {'mode':'paper','as_of':now.isoformat(),'observed_at':now.isoformat(),
            'occupied_symbols':self.occupied,'position_limit':self.limit,'theme_limit':2,
            'unclassified_policy':'block','themes_by_symbol':{r['symbol']:['반도체']for r in self.rows}|self.themes,
            'invalid_theme_symbols':[],'taxonomy_evidence':{'source_sha256':'a'*64}}

    def test_non_hot_fixed_symbols_no_chart_and_hot_half_budget(self):
        self.fx.tick();fx=self.fx
        self.assertEqual([x for x in fx.calls if x[0]=='chart'],[('chart','000660')])
        self.assertEqual(fx.monitor._entry_budgets,{'000660':500000})
        self.assertEqual(fx.monitor.status()['dormant_symbols']['005930']['code'],'funnel_not_hot')
        self.assertEqual(self.snapshot_calls,1)
        self.assertEqual(read_gate(fx.session,fx.now)['symbols'],('000660',))

    def test_large_hot_pool_keeps_eight_deep_request_budget_and_every_minute_not_guaranteed(self):
        self.rows=[self.row(f'{i:06d}',100000+i)for i in range(100,125)]
        self.fx.options['paper_funnel_hot_limit']=15
        for _ in range(8):self.fx.tick()
        scan=self.fx.monitor.status()['dynamic_scan']
        self.assertEqual(scan['candidate_count'],15);self.assertEqual(len(scan['active_entry_symbols']),8)
        self.assertEqual(len([x for x in self.fx.calls if x[0]=='chart']),8)
        self.assertEqual(len([x for x in self.fx.calls if x[0]=='book']),8)
        self.assertEqual(len([x for x in self.fx.calls if x[0]=='funnel']),1)
        self.assertFalse(scan['full_universe_every_minute_guaranteed'])
        self.assertEqual(scan['entry_detail_request_upper_bound'],24)
        self.assertEqual(self.snapshot_calls,1)

    def test_owned_outside_hot_is_processed_before_any_global_rank(self):
        self.fx.opens=['035420'];self.fx.tick()
        self.assertEqual(self.fx.monitor._last_symbol,'035420')
        self.assertFalse([x for x in self.fx.calls if x[0]=='funnel'])
        self.assertEqual(self.snapshot_calls,0)
        self.fx.tick();self.assertEqual(self.snapshot_calls,1)
        self.assertIn('035420',self.fx.monitor.status()['dynamic_scan']['owned_exit_symbols_outside_entry_cap'])

    def test_all_theme_membership_and_position_limit_filtered_in_memory(self):
        self.occupied=['000001','000002'];self.themes={'000001':['반도체'],'000002':['반도체']}
        self.fx.tick()
        self.assertFalse([x for x in self.fx.calls if x[0]=='chart'])
        self.assertEqual(self.fx.monitor.status()['dynamic_scan']['portfolio_exclusions'][0]['reason'],
                         'theme_concentration_limit_reached')
        self.fx.next_minute();self.rows=[self.row('000660')];self.limit=2
        self.fx.tick()
        self.assertEqual(self.fx.monitor.status()['dynamic_scan']['portfolio_exclusions'][0]['reason'],
                         'concurrent_position_limit_reached')

    def test_stale_portfolio_fails_closed_without_new_query_or_retry(self):
        def stale(now):
            result=self.snapshot(now);result['observed_at']=(now-timedelta(seconds=91)).isoformat();return result
        self.fx.tracker.paper_theme_capacity_snapshot=stale
        self.fx.tick();self.fx.tick()
        self.assertFalse(self.fx.calls)
        self.assertEqual(self.snapshot_calls,1)
        self.assertFalse(read_gate(self.fx.session,self.fx.now)['available'])

    def test_sparse_precision_quote_blocks_signal_and_is_preserved_without_retry(self):
        fx=self.fx;fx.tick();fx.next_minute();self.rows=[self.row('000660')];fx.change='20.1'
        fx.monitor._provider.fetch_orderbook=lambda symbol,**kw:book_at(fx.now,symbol)|{'ask_size':0}
        fx.tick();fx.tick()
        self.assertFalse(fx.orders)
        self.assertEqual(fx.monitor.status()['funnel_orderbooks']['000660']['status'],'unavailable')
        self.assertEqual(fx.monitor.status()['symbol_input_diagnostics']['000660']['code'],'orderbook_unavailable')
        self.assertTrue(any(e['reason']=='orderbook_unavailable' for e in fx.events))
        self.assertEqual(precision_ready_symbols(read_gate(fx.session,fx.now),fx.now),())

    def test_disappearing_candidate_baseline_removed_no_retro_signal(self):
        self.fx.tick();self.assertIn(('000660',STRATEGIES[1]),self.fx.monitor._states)
        self.rows=[];self.fx.next_minute();self.fx.tick()
        self.assertNotIn(('000660',STRATEGIES[1]),self.fx.monitor._states)
        self.fx.next_minute();self.rows=[self.row('000660')];self.fx.change='20.1';self.fx.tick()
        self.assertFalse(self.fx.orders)

    def test_failed_discovery_does_not_reuse_previous_success_gate(self):
        self.fx.tick();self.fx.next_minute();self.fx.binding='changed'
        self.rows=[self.row('000660')];self.fx.tick()
        self.assertFalse(read_gate(self.fx.session,self.fx.now)['available'])
        self.assertEqual(len([x for x in self.fx.calls if x[0]=='chart']),1)

    def test_hot_tail_eventually_receives_active_slots_and_delay_is_reported(self):
        codes=[f'{i:06d}'for i in range(100,115)]
        self.fx.options['paper_funnel_hot_limit']=15
        for minute in range(4):
            self.rows=[self.row(code,100000+index)for index,code in enumerate(codes)]
            for _ in range(8):self.fx.tick()
            if minute<3:self.fx.next_minute()
        observed={x[1]for x in self.fx.calls if x[0]=='chart'}
        self.assertEqual(observed,set(codes))
        self.assertEqual(len(self.fx.monitor.status()['dynamic_scan']['queue_delay_seconds']),15)

    def test_request_deadline_keeps_hot_unobserved_not_price_filled(self):
        self.fx.now=self.fx.now.replace(second=45)
        self.rows=[self.row('000660')];self.fx.tick()
        self.assertFalse([x for x in self.fx.calls if x[0]=='chart'])
        self.assertEqual(self.fx.monitor.status()['dynamic_scan']['deadline_deferred'],['000660'])
        self.assertFalse(self.fx.orders)

    def test_owned_exit_ignores_entry_funnel_and_deadline(self):
        fx=self.fx;fx.now=fx.now.replace(second=50);fx.opens=['035420']
        fx.tracker.multi_strategy_position=lambda mode,symbol,strategy,now: (
            {'status':'owned','owned_qty':10,'half_take_done':False} if strategy==STRATEGIES[1] else {'status':'flat'})
        fx.monitor._exit_provider_factory=lambda _:SimpleNamespace(fetch=lambda *a,**k:{'symbol':'035420','current_price':90},
                                                                   is_fresh=lambda *a:True)
        fx.monitor._account=lambda _: {'mode':'paper','exchange':'KRX','snapshot_changed':False,
            'holdings':[{'stk_cd':'A035420','crd_tp':'00','crd_loan_dt':'','rmnd_qty':'10','trde_able_qty':'10','pur_amt':'1000'}]}
        fx.tick()
        self.assertEqual(len(fx.orders),1);self.assertEqual(fx.orders[0]['side'],'sell')
        self.assertEqual(self.snapshot_calls,0)
        self.assertFalse([x for x in fx.calls if x[0]=='funnel'])


class SharedFunnelObservationTests(TestCase):
    def setUp(self):
        self.fx=candidate_fixture.DynamicCandidateTests('test_bounded_current_evidence_catalog_staggers_and_deepcopy')
        self.fx.setUp();self.addCleanup(self.fx.doCleanups)

    def test_same_session_minute_gate_and_chart_cache_keep_original_provenance(self):
        fx=self.fx;calls=[]
        def chart(session,symbol,count):calls.append(count);return [{'source':'original','count':count}]
        publish_gate(fx.session,fx.now,['005930'],available=True)
        self.assertEqual(read_gate(fx.session,fx.now)['symbols'],('005930',))
        one=shared_chart(fx.session,'005930',count=62,now=fx.now,fetcher=chart)
        two=shared_chart(fx.session,'005930',count=22,now=fx.now,fetcher=chart)
        one[0]['source']='mutated';self.assertEqual(two[0]['source'],'original');self.assertEqual(calls,[62])
        fx.now+=timedelta(minutes=1);self.assertIsNone(read_gate(fx.session,fx.now))
        shared_chart(fx.session,'005930',count=62,now=fx.now,fetcher=chart)
        self.assertEqual(calls,[62,62])

    def test_failed_request_not_retried_by_other_loop_and_session_change_clears_gate(self):
        fx=self.fx;calls=[]
        def failed(*a,**kw):calls.append(1);raise OSError('fixture')
        for count in (22,62):
            with self.assertRaises((OSError,ValueError)):
                shared_chart(fx.session,'005930',count=count,now=fx.now,fetcher=failed)
        self.assertEqual(calls,[1])
        publish_gate(fx.session,fx.now,['005930'],available=True)
        from kiwoom_autotrader.api.session import IssuedToken
        fx.session._issued=IssuedToken('new-fixture','20261030235959')
        self.assertIsNone(read_gate(fx.session,fx.now))


class GoldenFunnelIntegrationTests(TestCase):
    def setUp(self):
        import tests.test_golden_cross as fixture
        self.fixture=fixture
        self.fx=fixture.GoldenCrossTests('test_initial_seed_then_cross_calls_existing_budget_buy_after_durable_marker')
        self.fx.setUp();self.addCleanup(self.fx.doCleanups)
        self.fx.settings['paper_funnel_enabled']=True

    def ready(self):
        fx=self.fx;publish_gate(fx.session,fx.now,['005930'],available=True)
        mark_precision_ready(fx.session,fx.now,'005930',book_at(fx.now))

    def test_waiting_gate_has_no_chart_then_same_minute_hot_allows_existing_gdcrs(self):
        fx=self.fx;fx.tick();fx.chart.assert_not_called()
        self.assertEqual(fx.monitor.status()['funnel']['status'],'awaiting_shared_hot_gate')
        fx.now+=timedelta(seconds=1);self.ready()
        fx.tick();self.assertEqual(fx.chart.call_count,1)
        fx.now=fx.now.replace(minute=4,second=5)
        fx.chart.return_value=self.fixture.bars([3,2,1,4])
        self.ready()
        fx.tick();fx.buy.assert_called_once()

    def test_same_minute_delayed_precision_retains_active_tail_and_picks_ready_first(self):
        fx=self.fx;fx.settings['gdcrs_stocks'].append({'code':'000660','budget':100000})
        publish_gate(fx.session,fx.now,['005930','000660'],available=True)
        fx.tick();fx.chart.assert_not_called()
        self.assertEqual(set(fx.monitor._scan),{'005930','000660'})
        fx.now+=timedelta(seconds=1)
        mark_precision_ready(fx.session,fx.now,'000660',book_at(fx.now,'000660'))
        fx.tick();self.assertEqual(fx.chart.call_args.args[1],'000660')
        self.assertIn('005930',fx.monitor._scan)
        fx.now+=timedelta(seconds=1)
        mark_precision_ready(fx.session,fx.now,'005930',book_at(fx.now,'005930'))
        fx.tick();self.assertEqual(fx.chart.call_args.args[1],'005930')
        self.assertEqual(fx.chart.call_count,2);fx.buy.assert_not_called()

    def test_non_hot_never_requests_chart_and_new_gate_does_not_replay_old_cross(self):
        fx=self.fx;publish_gate(fx.session,fx.now,[],available=True)
        fx.tick();fx.chart.assert_not_called()
        fx.now=fx.now.replace(minute=4,second=5)
        fx.chart.return_value=self.fixture.bars([3,2,1,4])
        self.ready()
        fx.tick();fx.buy.assert_not_called()

    def test_gdcrs_reuses_shared_larger_chart_without_duplicate_broker_request(self):
        fx=self.fx;self.ready()
        calls=[]
        def chart(*a,**kw):calls.append(kw['count']);return fx.chart.return_value
        shared_chart(fx.session,'005930',count=22,now=fx.now,fetcher=chart)
        fx.tick();fx.chart.assert_not_called();self.assertEqual(calls,[22])

    def test_dispatch_gate_change_blocks_queued_initial_order(self):
        fx=self.fx;self.ready();fx.seed()
        fx.now=fx.now.replace(minute=4,second=5);fx.chart.return_value=self.fixture.bars([3,2,1,4])
        self.ready()
        errors=[]
        def buy(*a,before_send,**kw):
            publish_gate(fx.session,fx.now,[],available=False)
            try:before_send()
            except Exception as error:errors.append(error.skipped_reason)
            from kiwoom_autotrader.telegram.commands.orders import OrderReplies
            return OrderReplies([],submitted=False,decision_reason='context_changed')
        fx.buy.side_effect=buy;fx.tick();self.assertEqual(errors,['context_changed'])

    def test_deadline_leaves_proven_owned_b3_candidate_monitored(self):
        fx=self.fx;fx.now=fx.now.replace(second=50)
        fx.settings.update(gdcrs_initial_entry_pct=50,gdcrs_add_on_profit_bps=50)
        fx.session.paper_automation_reconcile=SimpleNamespace(_tracker=SimpleNamespace(
            b3_add_candidate_status=lambda *a:{'status':'candidate','candidate':{'invalid_fixture':True}}))
        # A verified-looking candidate permits protective basis checks, not an
        # order: malformed final B3 proof still fails the original strict gate.
        fx.tick();self.assertEqual(fx.chart.call_count,1);fx.buy.assert_not_called()
        self.assertEqual(fx.monitor.status()['funnel']['owned_b3_candidates'],['005930'])


class StrictFunnelBookTests(TestCase):
    def setUp(self):
        self.fx=candidate_fixture.DynamicCandidateTests('test_bounded_current_evidence_catalog_staggers_and_deepcopy')
        self.fx.setUp();self.addCleanup(self.fx.doCleanups)

    def test_top1_transport_uses_existing_queue_and_five_second_provenance(self):
        import io
        from kiwoom_autotrader.api.order_book import fetch_paper_observed_book,observed_book_is_fresh
        fx=self.fx;calls=[]
        payload={'return_code':0,'bid_req_base_tm':fx.now.strftime('%H%M%S'),
            'sel_fpr_bid':'+101','buy_fpr_bid':'-100','sel_fpr_req':'500','buy_fpr_req':'600'}
        class Response(io.BytesIO):
            headers={'api-id':'ka10004','cont-yn':'N','next-key':''}
        def queued(opener,request,**kw):
            kw['before_send']();calls.append((request.get_header('Api-id'),json.loads(request.data),kw['timeout']))
            return Response(json.dumps(payload).encode())
        with patch('kiwoom_autotrader.api.order_book.queued_open',side_effect=queued), \
             patch('kiwoom_autotrader.api.order_book._REQUEST_PACER',SimpleNamespace(acquire=lambda before_send:before_send())):
            result=fetch_paper_observed_book(fx.session,'005930',now=lambda:fx.now)
        self.assertEqual(calls,[('ka10004',{'stk_cd':'005930'},5)])
        self.assertEqual((result['ask'],result['bid'],result['ask_size'],result['bid_size']),(101,100,500,600))
        self.assertTrue(observed_book_is_fresh(result,fx.now));self.assertFalse(result['source_date_verified'])
        self.assertFalse(observed_book_is_fresh(result,fx.now+timedelta(seconds=6)))

    def test_stale_future_sparse_crossed_or_missing_top1_are_not_permission(self):
        from kiwoom_autotrader.api.order_book import observed_book_is_fresh
        fx=self.fx
        for change in ({'quote_time_inferred_kst':(fx.now-timedelta(seconds=6)).isoformat()},
                {'quote_time_inferred_kst':(fx.now+timedelta(seconds=1)).isoformat()},
                {'ask_size':0},{'bid_size':None},{'ask':99},{'wire_sha256':'bad'},
                {'request_started_at':(fx.now-timedelta(seconds=6)).isoformat()}):
            with self.subTest(change=change):
                self.assertFalse(observed_book_is_fresh(book_at(fx.now)|change,fx.now))

    def test_provider_failed_book_never_retries_same_minute_and_session_is_bound(self):
        fx=self.fx;calls=[]
        def fetch(session,symbol,now,before_send):
            before_send();calls.append(symbol);return book_at(fx.now,symbol)|{'bid_size':0}
        fx.provider._book_fetcher=fetch
        from kiwoom_autotrader.trading.paper_strategy_context import ContextUnavailable
        for _ in range(2):
            with self.assertRaises(ContextUnavailable):fx.provider.fetch_orderbook('005930',fx.now)
        self.assertEqual(calls,['005930'])
        fx.now+=timedelta(minutes=1)
        fx.provider._book_fetcher=lambda session,symbol,now,before_send:book_at(fx.now,symbol)
        result=fx.provider.fetch_orderbook('005930',fx.now)
        self.assertTrue(fx.provider.is_orderbook_fresh(result,fx.now))
        from kiwoom_autotrader.api.session import IssuedToken
        fx.session._issued=IssuedToken('new-fixture','20261030235959')
        self.assertFalse(fx.provider.is_orderbook_fresh(result,fx.now))

    def test_queued_expiry_runs_before_transport_and_ready_gate_expires(self):
        fx=self.fx;calls=[]
        def fetch(session,symbol,now,before_send):
            fx.now+=timedelta(seconds=6);before_send();calls.append(symbol);return book_at(fx.now,symbol)
        fx.provider._book_fetcher=fetch
        from kiwoom_autotrader.trading.paper_strategy_context import ContextUnavailable
        with self.assertRaises(ContextUnavailable):fx.provider.fetch_orderbook('005930',fx.now)
        self.assertEqual(calls,[])
        publish_gate(fx.session,fx.now,['005930'],available=True)
        self.assertEqual(precision_ready_symbols(read_gate(fx.session,fx.now),fx.now),())
        mark_precision_ready(fx.session,fx.now,'005930',book_at(fx.now))
        self.assertEqual(precision_ready_symbols(read_gate(fx.session,fx.now),fx.now),('005930',))
        fx.now+=timedelta(seconds=6)
        self.assertEqual(precision_ready_symbols(read_gate(fx.session,fx.now),fx.now),())
