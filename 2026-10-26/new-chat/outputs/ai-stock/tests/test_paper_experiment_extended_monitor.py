"""Offline monitor integration using selected evidence and a fake broker transport."""
from copy import deepcopy
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock

from kiwoom_autotrader.trading.decision_evidence import freeze_evidence
from kiwoom_autotrader.trading.paper_experiment_store import ExperimentStore
from kiwoom_autotrader.trading import paper_experiment_extended_rules as rules
from tests import test_paper_experiment_monitor as monitor_fixture
from tests.test_paper_experiment_extended_rules import START, DAY, SYMBOL, bars, context, selected, calendar


class ExtendedMonitorTests(TestCase):
    def setUp(self):
        self.f=monitor_fixture.MonitorTests('test_real_mode_does_not_load_settings_provider_store_or_submit')
        self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.f.clock=START+timedelta(minutes=6)

    def wire(self,*,symbol=SYMBOL,vi=False):
        f=self.f;ctx=context(f.clock,symbol)
        market=ctx['market']
        market.update(vi_complete=True,vi_last_trigger_basis='release_time_conservative_120s_not_trigger_assertion',
            request_started_at=f.clock.isoformat(),available_at=f.clock.isoformat(),
            indicative_price=950,classification_available_at=f.clock.isoformat(),normal=True,common_share=True)
        ctx['calendar']=calendar()
        f.monitor.inputs=SimpleNamespace(market=Mock(return_value=market),daily=Mock(return_value=ctx['daily']),
            vi_state=Mock(return_value={'vi_active':vi,'vi_complete':vi is not None}))
        f.monitor.extended_scheduler=SimpleNamespace(context=Mock(return_value=ctx),status=Mock(return_value={}))
        return ctx

    def test_orb_signal_freezes_real_admission_and_submits_once_to_fake_transport(self):
        f=self.f;assignment,_,_=selected();ctx=self.wire()
        f.chart.side_effect=None;f.chart.return_value=bars([1000]*5+[1042],f.clock)
        f.one(assignment)
        self.assertEqual(f.orders,[('buy',SYMBOL,200,1000)])
        frozen=freeze_evidence(f.contexts[-1][1]['evidence']).as_dict()
        self.assertEqual(frozen['config']['experiment_extended_admission']['symbol'],SYMBOL)
        self.assertEqual(frozen['config']['experiment_extended_admission']['arm'],assignment['arm'])
        self.assertEqual(frozen['config']['experiment_extended_admission']['vi']['last_trigger_basis'],
                         'release_time_conservative_120s_not_trigger_assertion')
        f.one(assignment);self.assertEqual(len(f.orders),1)

    def test_orb_owned_stop_during_vi_records_unfillable_without_order(self):
        f=self.f;assignment,_,_=selected();self.wire(vi=True)
        f.owned(assignment,entry_at=START+timedelta(minutes=5))
        f.chart.side_effect=None;f.chart.return_value=bars([900],f.clock,start=f.clock-timedelta(minutes=1))
        f.one(assignment)
        self.assertFalse(f.orders)
        self.assertTrue(any(e.get('skip_reason')=='vi_active_unfillable'
            and e.get('execution_status')=='unfillable_observed'
            and e.get('vi_observation',{}).get('vi_active') is True for e in f.events),f.events)

    def test_gdr_preopen_buy_is_paper_only_and_expires_at_open(self):
        f=self.f;f.clock=START-timedelta(minutes=1)
        assignment,_,_=selected('EXP_GDR');self.wire()
        f.one(assignment)
        self.assertEqual(f.orders,[('buy',SYMBOL,210,950)])
        evidence=freeze_evidence(f.contexts[-1][1]['evidence']).as_dict()
        self.assertEqual(evidence['config']['experiment_order_window_end_at'],START.isoformat())
        f.session.mode='real'
        f.monitor.tick(f.session,f.notify)
        self.assertEqual(len(f.orders),1)

    def test_vss_updates_survive_same_day_append_and_restart(self):
        f=self.f;assignment,_,_=selected('EXP_VSS');f.owned(assignment,entry_at=START.replace(hour=15,minute=25))
        f.clock=START+timedelta(days=1,hours=1)
        ctx=self.wire();ctx['calendar']=calendar()
        ctx['day_close']={'date':'20261102','close':900,'available_at':f.clock.isoformat(),'source_sha256':'1'*64}
        # A previous update on the same day must not suppress a new stop trigger.
        f.monitor.store.selection('20261103','position_update:'+'a'*32+':090001000000',{'held_trading_sessions':1})
        f.one(assignment)
        self.assertEqual(f.monitor.store.position_updates('a'*32)['vss_stop_trigger_day'],'20261102')
        f.clock+=timedelta(minutes=1)
        f.one(assignment)
        import sqlite3,json
        with sqlite3.connect(f.path) as db:
            payload=db.execute("SELECT payload FROM selections WHERE kind LIKE ? ORDER BY kind DESC LIMIT 1",
                               ('position_update:'+'a'*32+':%',)).fetchone()[0]
        self.assertEqual(json.loads(payload)['vss_stop_trigger_day'],'20261102')
        reopened=ExperimentStore(Path(f.path))
        self.assertEqual(reopened.position_updates('a'*32)['vss_stop_trigger_day'],'20261102')
        self.assertFalse(f.orders)

    def test_etf_pair_is_separate_from_stock_assignment(self):
        f=self.f;f.clock=START+timedelta(minutes=36)
        for symbol in ('069500','229200'):
            assignment=rules.etf_assignment(DAY,symbol,START,f.clock)
            ctx=self.wire(symbol=symbol)
            f.chart.side_effect=None;f.chart.return_value=bars([1000]*30+[990]*5+[1100],f.clock,symbol=symbol,volumes=[100]*35+[1000])
            f.one(assignment)
        self.assertEqual([(x[1],x[0]) for x in f.orders],[('069500','buy'),('229200','buy')])
        with self.assertRaises(ValueError):rules.etf_assignment(DAY,'122630',START,f.clock)
