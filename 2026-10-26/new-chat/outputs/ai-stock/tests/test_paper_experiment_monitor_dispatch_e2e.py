"""One native E-rule signal through monitor, policy, ledger and fake transport."""
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from unittest import TestCase
from unittest.mock import Mock,patch
import json

from tests.test_orders import fake_session
from kiwoom_autotrader.api.minute_chart import MinuteBar
from kiwoom_autotrader.trading.paper_experiment_monitor import PaperExperimentMonitor
from kiwoom_autotrader.trading.paper_experiment_rules import assign_candidate
from kiwoom_autotrader.trading.paper_experiment_store import ExperimentStore
from kiwoom_autotrader.trading.paper_policy import experiment_order_sizing

class MonitorDispatchE2E(TestCase):
    def test_native_e_signal_reaches_actual_order_policy_and_fake_transport(self):
        from tests.test_multi_strategy_epoch_review import EpochIndependentReview
        case=EpochIndependentReview('test_complete_zero_broker_evidence_allows_new_generation_without_touching_old_history');case.setUp()
        self.addCleanup(case.doCleanups);proof=case.proof();case.settings.update(paper_experiment_enabled=True,
          gdcrs_stocks=[{'code':'005930','budget':10000}])
        sizing=experiment_order_sizing(case.settings,'paper',available_cash_krw=450000,price=1000,
          existing_principal_krw=0,pending_principal_krw=0)
        proof.update(schema_version=6,integrated_pending_clear=False,symbol_pending_clear=True,
          paper_experiment_sizing=sizing,symbol_cap_krw=20000,per_buy_cap_krw=10000,remaining_principal_krw=20000)
        account={'schema':'paper-account-snapshot-v1','mode':'paper','exchange':'KRX',
                 'holdings':[],'cash':{'entr':'450000','ord_alow_amt':'450000'}}
        with case.tracker._db() as db:
            db.execute('INSERT OR REPLACE INTO account_balance_daily VALUES(?,?,?,?,?,?)',
              ('default','paper','20261030',proof['observed_at'],'broker_observation',json.dumps(account)))
        case.tracker._assert_position_capacity=Mock(return_value={})
        case.tracker._holdings=Mock();case.tracker._save_paper_balance_snapshot=Mock()
        session=fake_session(clock=lambda:case.now)
        class Reconcile:
            _tracker=case.tracker
            def __call__(self,symbol,**kwargs):
                def guard():pass
                guard.reconciliation_evidence=proof
                return guard
        session.paper_automation_reconcile=Reconcile();session.execution_recording_guard=lambda:None
        class Provider:
            def discover_dynamic_candidates(self,*a,**k):return {'candidates':[]}
            def fetch(self,symbol,now):return {'symbol':symbol,'current_price':1000,'source_sha256':'a'*64}
        bars=[]
        for i in range(21):
            bars.append(MinuteBar(case.now.replace(second=0)-timedelta(minutes=21-i),980 if i==20 else 1000,300 if i==20 else 100,
              source_symbol='005930',source_mode='paper',source_query_started_at=case.now,source_response_sha256='a'*64,
              open_price=1000,high_price=1000 if i==20 else 1001,low_price=980 if i==20 else 999))
        report={'mode':'paper','exchange':'KRX','snapshot_changed':False,'holdings':[],
                'cash':{'entr':'450000','ord_alow_amt':'450000'}}
        submitted=[]
        @contextmanager
        def queue(opener,request,**kwargs):
            kwargs['before_send']();submitted.append(json.loads(request.data))
            class Response:
                def read(self,n):return b'{"return_code":0,"ord_no":"31"}'
            yield Response()
        monitor=PaperExperimentMonitor(settings_loader=lambda:case.settings,now=lambda:case.now,
          store_path=Path(case.tmp.name)/'experiment.sqlite3',chart_fetcher=lambda *_:bars,
          provider_factory=lambda _:Provider(),account_fetcher=lambda _:report,
          calendar_reader=lambda at:{'start':at.replace(hour=9,minute=0,second=0,microsecond=0),
                                     'end':at.replace(hour=15,minute=30,second=0,microsecond=0)},batch_size=1)
        monitor.store=ExperimentStore(monitor.path)
        assignment=assign_candidate('20261030','005930',case.now-timedelta(minutes=3),
                                    assigned_at=case.now-timedelta(minutes=2),profile='native-five-v1')
        self.assertEqual(assignment['arm'],'EXP_E');monitor.store.assign(assignment)
        with patch('kiwoom_autotrader.api.orders.queued_open',side_effect=queue), \
             patch('kiwoom_autotrader.trading.paper_policy.load_settings',side_effect=lambda **kw:case.settings), \
             patch('kiwoom_autotrader.trading.paper_experiment_monitor.fetch_budget_limit',return_value=1000):
            monitor.tick(session,lambda _:None)
        self.assertEqual(len(submitted),1,monitor.status())
        self.assertEqual((submitted[0]['ord_qty'],submitted[0]['ord_uv']),('10','1000'))
        with case.tracker._db() as db:
            generation=db.execute("SELECT id FROM intents WHERE strategy='EXP_E' AND state='accepted'").fetchone()[0]
        self.assertTrue(monitor._generation_matches(case.tracker,'005930',assignment,generation))
        self.assertFalse(monitor._generation_matches(case.tracker,'005930',{**assignment,'assignment_sha256':'f'*64},generation))
