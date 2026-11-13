"""Dynamic S1 admission/baseline tests: isolated fake market, never real orders."""
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from unittest import TestCase

from kiwoom_autotrader.trading.order_context import automated_orders, _current
from kiwoom_autotrader.trading.paper_funnel_state import (publish_gate,mark_precision_ready,
    dynamic_s1_admission,assert_current_dynamic_s1_admission,shared_chart)
from kiwoom_autotrader.telegram.commands.orders import OrderReplies
from kiwoom_autotrader.api.session import IssuedToken
from tests.test_paper_funnel import book_at
import tests.test_golden_cross as fixture


def candidate(now, symbol):
    return {'symbol':symbol,'name':'검증일반주','change_pct':'10','surge_rate_pct':'100',
        'turnover_krw':1_000_000_000,'available_at':now.isoformat(),
        'source_hashes':{'surge':'a'*64,'turnover':'b'*64,'membership':'c'*64},
        'membership_evidence':{'marketCode':'0','auditInfo':'정상','orderWarning':'0','state':'증거금40%',
            'upSizeName':'대형주','source':'ka10099','available_at':now.isoformat(),
            'source_sha256':'c'*64,'instrument_filter':'ka10023_stk_cnd_20'},
        'liquidity_evidence':{'surge_now_trde_qty':10000,'turnover_now_trde_qty':12000,
            'turnover_change_pct':'10','volume_unit':'shares','turnover_unit_source':'million_krw',
            'basis':'current_day_cumulative','same_time_rvol20':None}}


class DynamicS1MonitorTests(TestCase):
    def setUp(self):
        self.fx=fixture.GoldenCrossTests('test_initial_seed_then_cross_calls_existing_budget_buy_after_durable_marker')
        self.fx.setUp();self.addCleanup(self.fx.doCleanups)
        fx=self.fx;fx.settings.update(paper_funnel_enabled=True,paper_multi_strategy_enabled=True,
            gdcrs_initial_entry_pct=50,gdcrs_add_on_profit_bps=50,gdcrs_max_daily_entries_per_symbol=2,
            gdcrs_min_cross_bps=10,gdcrs_reentry_cooldown_minutes=20)
        self.original=deepcopy(fx.settings['gdcrs_stocks']);self.restored={};self.statuses={};self.evidence=[]
        self.tracker=SimpleNamespace(
            b3_monitor_candidates=lambda *a:{'status':'available','candidates':deepcopy(self.restored)},
            b3_add_candidate_status=lambda mode,code,now:deepcopy(self.statuses.get(code,{'status':'no_initial','candidate':None})),
            daily_buy_entry_counts=lambda *a,**kw:{'accepted':0,'reserved':0})
        fx.session.paper_automation_reconcile=SimpleNamespace(_tracker=self.tracker)
        fx.monitor=fx.make_monitor(chart_source_kind='broker_observation',cooldown_check=lambda *a:True)
        self.prices=[3,2,1]
        def chart(session,code,**kw):
            return tuple(replace(b,volume=100,acc_volume=100*(i+1),adjustment='',adjustment_rate='',
                source_symbol=code,source_mode='paper',source_query_started_at=fx.now,
                source_response_sha256='d'*64)for i,b in enumerate(fixture.bars(self.prices)))
        fx.chart.side_effect=chart
        def buy(text,session,*,before_send):
            before_send();self.evidence.append(_current.get().evidence.as_dict())
            return OrderReplies([],submitted=True)
        fx.buy.side_effect=buy

    def gate(self,codes=('000660',),*,ready=True):
        fx=self.fx
        publish_gate(fx.session,fx.now,list(codes),available=True,
            candidates={c:candidate(fx.now,c)for c in codes},provider_binding='f'*32)
        if ready:
            for code in codes:mark_precision_ready(fx.session,fx.now,code,book_at(fx.now,code)|{'session_binding':'f'*32})

    def tick(self):
        with automated_orders(self.tracker,'gdcrs'):self.fx.tick()

    def next(self,prices,codes=('000660',)):
        self.fx.now+=timedelta(minutes=1);self.prices=prices;self.gate(codes);self.tick()

    def test_dynamic_initial_has_half_budget_immutable_fixed_configuration_and_versioned_proof(self):
        self.gate();self.tick();self.fx.buy.assert_not_called()
        self.next([3,2,1,4]);self.fx.buy.assert_called_once()
        self.assertEqual(self.fx.buy.call_args.args[0],'buy 000660 max 500000')
        evidence=self.evidence[0]
        self.assertEqual(evidence['strategy_version'],'completed-minute-cross-funnel-v1')
        self.assertEqual(evidence['config']['stocks'],self.original)
        self.assertEqual(evidence['dynamic_s1_admission']['symbol'],'000660')
        self.assertEqual(evidence['config']['b3_total_budget_krw'],1_000_000)
        self.assertEqual(evidence['config']['max_daily_entries_per_symbol'],2)
        self.assertEqual(self.fx.settings['gdcrs_stocks'],self.original)
        self.assertNotIn(self.fx.session.token,str(evidence))

    def test_adding_another_hot_symbol_preserves_existing_observed_baseline(self):
        self.gate();self.tick()
        previous=self.fx.monitor._baseline['000660'];identity=self.fx.monitor._identity
        self.next([3,2,1,4],('000660','035420'))
        self.fx.buy.assert_called_once();self.assertIs(self.fx.monitor._identity,identity)
        self.assertEqual(previous.bars[-1].close,1)
        self.assertNotIn('035420',dict(self.fx.monitor._identity.stocks))

    def test_left_and_rejoined_dynamic_symbol_seeds_instead_of_replaying_unobserved_cross(self):
        self.gate();self.tick();self.next([3,2,1,4],())
        self.assertNotIn('000660',self.fx.monitor._baseline)
        self.next([3,2,1,4,5]);self.fx.buy.assert_not_called()
        self.assertIn('000660',self.fx.monitor._baseline)

    def test_delayed_same_minute_dynamic_gate_and_book_are_consumed_once(self):
        self.tick();self.fx.chart.assert_not_called()
        self.fx.now+=timedelta(seconds=1);self.gate(ready=False);self.tick()
        self.fx.chart.assert_not_called()
        self.fx.now+=timedelta(seconds=1)
        mark_precision_ready(self.fx.session,self.fx.now,'000660',book_at(self.fx.now,'000660')|{'session_binding':'f'*32})
        self.tick();self.assertEqual(self.fx.chart.call_count,1)
        self.fx.now+=timedelta(seconds=1);self.tick();self.assertEqual(self.fx.chart.call_count,1)

    def test_malformed_dynamic_source_proof_never_produces_order(self):
        self.gate();self.tick();self.fx.now+=timedelta(minutes=1);self.prices=[3,2,1,4]
        row=candidate(self.fx.now,'000660');row['membership_evidence']['auditInfo']='관리'
        publish_gate(self.fx.session,self.fx.now,['000660'],available=True,candidates={'000660':row},provider_binding='f'*32)
        mark_precision_ready(self.fx.session,self.fx.now,'000660',book_at(self.fx.now,'000660')|{'session_binding':'f'*32})
        self.tick();self.fx.buy.assert_not_called()

    def test_missing_dynamic_b3_policy_never_extends_fixed_scan(self):
        for key,value in [('gdcrs_initial_entry_pct',100),('gdcrs_max_daily_entries_per_symbol',0),
                ('gdcrs_reentry_cooldown_minutes',0),('gdcrs_min_cross_bps',0)]:
            with self.subTest(key=key):
                saved=self.fx.settings[key];self.fx.settings[key]=value
                if key=='gdcrs_initial_entry_pct':self.fx.settings['gdcrs_add_on_profit_bps']=0
                self.fx.monitor.reset();self.gate();self.tick()
                self.fx.chart.assert_not_called();self.fx.buy.assert_not_called()
                self.fx.settings[key]=saved;self.fx.settings['gdcrs_add_on_profit_bps']=50

    def test_session_change_and_restart_never_replay_dynamic_baseline(self):
        self.gate();self.tick();self.fx.now+=timedelta(minutes=1);self.prices=[3,2,1,4]
        self.fx.session._issued=IssuedToken('different-test-session','20300101000000')
        self.gate();self.tick();self.fx.buy.assert_not_called()
        self.assertEqual(self.fx.monitor._baseline['000660'].bars[-1].close,4)

    def test_dynamic_daily_cap_counts_existing_buys_and_reservations(self):
        self.gate();self.tick()
        self.tracker.daily_buy_entry_counts=lambda *a:{'accepted':1,'reserved':1}
        self.next([3,2,1,4]);self.fx.buy.assert_not_called()
        self.assertEqual(self.fx.monitor.status()['last_entry_decision']['skip_reason'],'daily_entry_limit_reached')

    def test_dynamic_reuses_multi_precision_chart_without_additional_query(self):
        self.gate();calls=[]
        def fetch(*args,**kwargs):
            calls.append(kwargs['count'])
            return self.fx.chart.side_effect(*args,**kwargs)
        shared_chart(self.fx.session,'000660',count=62,now=self.fx.now,fetcher=fetch)
        self.tick();self.fx.chart.assert_not_called();self.assertEqual(calls,[62])

    def test_queue_delay_expiring_precision_blocks_initial_without_replaying(self):
        self.gate();self.tick();blocked=[]
        def queued(text,session,*,before_send):
            self.fx.now+=timedelta(seconds=6)
            try:before_send()
            except Exception as error:blocked.append(error.skipped_reason)
            return OrderReplies([],submitted=False,decision_reason='context_changed')
        self.fx.buy.side_effect=queued
        self.next([3,2,1,4])
        self.assertEqual(blocked,['context_changed']);self.assertEqual(self.evidence,[])
        self.fx.now+=timedelta(seconds=1);self.tick();self.fx.buy.assert_called_once()

    def test_original_admission_rechecks_book_time_and_gate_not_refreshed_time(self):
        self.gate();proof=dynamic_s1_admission(self.fx.session,self.fx.now,'000660')
        self.assertEqual(assert_current_dynamic_s1_admission(self.fx.session,self.fx.now+timedelta(seconds=3),proof),proof)
        with self.assertRaises(ValueError):assert_current_dynamic_s1_admission(self.fx.session,self.fx.now+timedelta(seconds=6),proof)
        self.fx.now+=timedelta(seconds=1);self.gate()
        with self.assertRaises(ValueError):assert_current_dynamic_s1_admission(self.fx.session,self.fx.now,proof)

    def test_restart_owned_dynamic_b3_is_monitored_without_hot_after_entry_deadline(self):
        fx=self.fx;fx.now=fx.now.replace(second=50)
        cfg={'b3_total_budget_krw':1_000_000,'b3_initial_entry_pct':50,'b3_add_on_profit_bps':50}
        self.restored={'000660':{'config':cfg,'candidate':{'invalid_fixture':True}}}
        self.statuses['000660']={'status':'candidate','candidate':{'invalid_fixture':True}}
        self.tick()
        self.assertEqual(fx.chart.call_args.args[1],'000660')
        self.assertEqual(fx.monitor.status()['funnel']['owned_b3_candidates'],['000660'])
        fx.buy.assert_not_called() # precise original fill/basis still mandatory

    def test_restarted_dynamic_owner_add_uses_original_lot_without_expired_initial_admission(self):
        from tests.test_b3_entry import bars,NOW,FILL
        fx=self.fx;fx.now=NOW.replace(second=50)
        fx.settings.update(gdcrs_short_period=5,gdcrs_long_period=20)
        cfg={'b3_total_budget_krw':1_000_000,'b3_initial_entry_pct':50,'b3_add_on_profit_bps':50}
        initial={'initial_intent_id':'a'*32,'initial_fill_last_observed_at':FILL.isoformat(),
            'initial_filled_qty':100,'initial_filled_principal_krw':100000}
        self.restored={'000660':{'config':cfg,'candidate':initial}}
        self.statuses['000660']={'status':'candidate','candidate':initial}
        fx.chart.side_effect=None
        fx.chart.return_value=tuple(replace(b,source_symbol='000660')for b in bars())
        self.tick();fx.buy.assert_called_once()
        self.assertEqual(fx.buy.call_args.args[0],'buy 000660 max 500000')
        value=self.evidence[0]
        self.assertEqual(value['strategy_version'],'paper-b3-add-v1')
        self.assertIsNone(value.get('dynamic_s1_admission'))
        self.assertEqual(value['config']['b3_initial_intent_id'],'a'*32)
        self.assertEqual(value['config']['stocks'],self.original)
