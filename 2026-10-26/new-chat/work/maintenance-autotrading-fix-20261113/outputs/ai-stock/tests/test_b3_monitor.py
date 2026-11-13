from datetime import timedelta
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

from kiwoom_autotrader.trading.order_context import automated_orders, _current
from tests.test_b3_entry import bars, NOW, FILL
from tests.test_golden_cross import GoldenCrossTests


class B3MonitorTests(GoldenCrossTests):
    def test_closed_lot_is_a_new_initial_cycle_and_still_requires_a_new_cross(self):
        self.settings.update(gdcrs_initial_entry_pct=50,gdcrs_add_on_profit_bps=50)
        tracker=Mock();tracker.b3_add_candidate_status.return_value={'status':'lot_closed','candidate':None}
        self.session.paper_automation_reconcile=SimpleNamespace(_tracker=tracker)
        with automated_orders(tracker,'gdcrs'):
            self.seed();self.buy.assert_not_called();self.cross()
        self.buy.assert_called_once()

    def test_split_initial_scales_budget_and_persists_evidence_context(self):
        self.settings.update(gdcrs_initial_entry_pct=50,gdcrs_add_on_profit_bps=0)
        tracker=Mock()
        observed=[]
        def buy(text, session, *, before_send):
            before_send()
            observed.append((text,_current.get().evidence.as_dict()))
            return []
        self.buy.side_effect=buy
        with automated_orders(tracker,'gdcrs'):
            self.seed();self.cross()
        self.assertEqual(observed[0][0], 'buy 005930 max 50000')
        self.assertEqual(observed[0][1]['config']['b3_stage'], 'initial')
        self.assertEqual(observed[0][1]['config']['b3_total_budget_krw'],100000)

    def test_add_on_uses_post_fill_volume_without_fabricating_cross(self):
        self.now=NOW
        self.settings.update(gdcrs_initial_entry_pct=50,gdcrs_add_on_profit_bps=50)
        initial={'initial_intent_id':'a'*32,'initial_fill_last_observed_at':FILL.isoformat(),
                 'initial_filled_qty':100,'initial_filled_principal_krw':100000}
        tracker=Mock();tracker.b3_add_candidate_status.return_value={'status':'candidate','candidate':initial}
        self.session.paper_automation_reconcile=SimpleNamespace(_tracker=tracker)
        self.chart.return_value=bars()
        observed=[]
        def buy(text, session, *, before_send):
            before_send();observed.append((text,_current.get().evidence.as_dict()));return []
        self.buy.side_effect=buy
        with automated_orders(tracker,'gdcrs'):
            self.tick()
        self.assertEqual(observed[0][0],'buy 005930 max 50000')
        self.assertEqual(observed[0][1]['strategy_version'],'paper-b3-add-v1')
        self.assertIsNone(observed[0][1]['previous_difference'])
        self.assertEqual(observed[0][1]['config']['b3_initial_intent_id'],'a'*32)
        self.chart.assert_called_once_with(self.session,'005930',count=62)

    def test_unverified_or_below_volume_add_does_not_buy(self):
        self.now=NOW;self.settings.update(gdcrs_initial_entry_pct=50,gdcrs_add_on_profit_bps=50)
        tracker=Mock();tracker.b3_add_candidate_status.return_value={'status':'candidate','candidate':{'initial_intent_id':'a'*32,
            'initial_fill_last_observed_at':FILL.isoformat(),'initial_filled_qty':100,'initial_filled_principal_krw':100000}}
        self.session.paper_automation_reconcile=SimpleNamespace(_tracker=tracker)
        self.chart.return_value=bars(149)
        with automated_orders(tracker,'gdcrs'):self.tick()
        self.buy.assert_not_called()
        self.assertEqual(self.monitor.status()['last_entry_decision']['skip_reason'],'add_on_volume_insufficient')

    def test_below_basis_or_pending_are_distinct_no_send_reasons(self):
        self.now=NOW;self.settings.update(gdcrs_initial_entry_pct=50,gdcrs_add_on_profit_bps=50)
        tracker=Mock();tracker.b3_add_candidate_status.return_value={'status':'candidate','candidate':{'initial_intent_id':'a'*32,
            'initial_fill_last_observed_at':FILL.isoformat(),'initial_filled_qty':100,'initial_filled_principal_krw':100010}}
        self.session.paper_automation_reconcile=SimpleNamespace(_tracker=tracker);self.chart.return_value=bars()
        with automated_orders(tracker,'gdcrs'):self.tick()
        self.buy.assert_not_called()
        self.assertEqual(self.monitor.status()['last_entry_decision']['skip_reason'],'add_on_below_profit_threshold')
        self.now+=timedelta(minutes=1);self.monitor=self.make_monitor();self.chart.return_value=tuple(
            replace(bar,start=bar.start+timedelta(minutes=1),
                source_query_started_at=bar.source_query_started_at+timedelta(minutes=1)) for bar in bars())
        tracker.b3_add_candidate_status.return_value={'status':'pending','candidate':None}
        with automated_orders(tracker,'gdcrs'):self.tick()
        self.buy.assert_not_called()
        self.assertEqual(self.monitor.status()['last_entry_decision']['skip_reason'],'add_on_pending_order')

    def test_observed_negative_after_initial_fill_durably_invalidates(self):
        self.now=NOW;self.settings.update(gdcrs_initial_entry_pct=50,gdcrs_add_on_profit_bps=50)
        tracker=Mock();tracker.b3_add_candidate_status.return_value={'status':'candidate','candidate':{'initial_intent_id':'a'*32,
            'initial_fill_last_observed_at':FILL.isoformat(),'initial_filled_qty':100,'initial_filled_principal_krw':100000}}
        self.session.paper_automation_reconcile=SimpleNamespace(_tracker=tracker)
        negative=list(bars());negative[-1]=replace(negative[-1],close=800);self.chart.return_value=tuple(negative)
        with automated_orders(tracker,'gdcrs'):self.tick()
        self.buy.assert_not_called()
        tracker.b3_invalidate_candidate.assert_called_once_with('paper','005930','a'*32,NOW,'negative_sma_observed')
        self.assertEqual(self.monitor.status()['last_entry_decision']['skip_reason'],'add_on_dead_cross_invalidated')
