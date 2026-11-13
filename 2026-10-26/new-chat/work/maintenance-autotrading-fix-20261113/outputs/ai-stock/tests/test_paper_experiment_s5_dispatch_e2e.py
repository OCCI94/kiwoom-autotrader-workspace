"""S5 actual-input proof through native order policy, ledger and queued transport."""
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
from datetime import datetime,timedelta
from decimal import Decimal
from pathlib import Path
import json
from unittest import TestCase
from unittest.mock import Mock,patch

from tests.test_orders import fake_session
from tests.test_s5_source_bound_evidence import fixture
from tests.s5_short_fixture import short_evidence
from kiwoom_autotrader.trading.paper_experiment_monitor import PaperExperimentMonitor
from kiwoom_autotrader.trading.paper_experiment_rules import assign_cohort
from kiwoom_autotrader.trading.paper_experiment_store import ExperimentStore
from kiwoom_autotrader.trading.paper_policy import experiment_order_sizing
from kiwoom_autotrader.trading.steady_uptrend_vwap import S5

class S5MonitorDispatchE2E(TestCase):
    def test_s5_to_queued_order_and_persistent_native_generation(self):
        from tests.test_multi_strategy_epoch_review import EpochIndependentReview
        case=EpochIndependentReview('test_complete_zero_broker_evidence_allows_new_generation_without_touching_old_history')
        case.setUp();self.addCleanup(case.doCleanups)
        case.settings.update(paper_experiment_enabled=True,paper_experiment_s5_enabled=True,
                             gdcrs_stocks=[{'code':'005930','budget':10000}])
        original,bars,market=fixture(60);now=case.now;delta=now-original
        bars=[replace(b,start=b.start+delta-timedelta(seconds=delta.seconds%60),
            source_symbol='005930',source_query_started_at=now-timedelta(seconds=2)) for b in bars]
        market=deepcopy(market);market.update(symbol='005930',source='ka10001_normalized_response',
            request_started_at=now.isoformat(),available_at=now.isoformat(),observed_at=now.isoformat())
        daily=market['s5_daily_context'];daily.update(symbol='005930',session_day='20261030',
            request_started_at=(now-timedelta(seconds=3)).isoformat(),available_at=(now-timedelta(seconds=2)).isoformat())
        dates=[];day=now.date()-timedelta(days=1)
        while len(dates)<20:
            if day.weekday()<5:dates.append(day)
            day-=timedelta(days=1)
        for row,day in zip(daily['rows'],reversed(dates)):row['date']=day.strftime('%Y%m%d')
        risk=market['s5_risk_context'];risk.update(symbol='005930',reviewed_at=(now-timedelta(hours=1)).isoformat(),expires_at=(now+timedelta(hours=1)).isoformat())
        risk['short_selling_evidence']=short_evidence('005930',daily,now)
        price=market['current_price']
        sizing=experiment_order_sizing(case.settings,'paper',available_cash_krw=9_000_000,price=price,
            existing_principal_krw=0,pending_principal_krw=0)
        proof=case.proof();proof.update(schema_version=6,integrated_pending_clear=False,symbol_pending_clear=True,
            paper_experiment_sizing=sizing,order_quantity=sizing['quantity'],limit_price_krw=price,
            symbol_cap_krw=sizing['effective_symbol_cap_krw'],per_buy_cap_krw=sizing['budget_krw'],
            remaining_principal_krw=sizing['effective_symbol_cap_krw'])
        account={'schema':'paper-account-snapshot-v1','mode':'paper','exchange':'KRX',
            'holdings':[],'cash':{'entr':'9000000','ord_alow_amt':'9000000'}}
        with case.tracker._db() as db:
            db.execute('INSERT OR REPLACE INTO account_balance_daily VALUES(?,?,?,?,?,?)',
              ('default','paper','20261030',proof['observed_at'],'broker_observation',json.dumps(account)))
        # Use the real account-wide capacity check at both reservation/send.
        # Holdings writes stay stubbed because the immutable snapshot is above.
        capacity_calls=[];actual_capacity=case.tracker._assert_position_capacity
        def checked_capacity(*args,**kwargs):
            capacity_calls.append(dict(kwargs));return actual_capacity(*args,**kwargs)
        case.tracker._assert_position_capacity=checked_capacity
        case.tracker._holdings=Mock();case.tracker._save_paper_balance_snapshot=Mock()
        session=fake_session(clock=lambda:case.now)
        class Reconcile:
            _tracker=case.tracker
            def __call__(self,symbol,**kwargs):
                def guard():pass
                current_proof=proof
                if kwargs.get('side')=='sell':
                    pos=case.tracker.multi_strategy_position('paper',symbol,S5,case.now)
                    held=case.tracker.record_strategy_holding_observation(symbol,pos['owned_qty'],pos['owned_qty'],pos['basis_krw'],case.now,source_kind='broker_observation')
                    current_proof={'schema_version':4,'symbol':symbol,'pre_order_held_qty':pos['owned_qty'],
                        'pre_order_pur_amt_krw':pos['basis_krw'],'order_quantity':pos['owned_qty'],
                        'observed_at':case.now.isoformat(),'holdings_source':'broker_observation',
                        'generation_id':pos['generation_id'],'exit_kind':'full','strategy':S5,
                        'holding_observation_id':held,'symbol_ledger_sha256':case.tracker.symbol_order_watermark('paper',symbol)}
                guard.reconciliation_evidence=current_proof
                return guard
        session.paper_automation_reconcile=Reconcile();session.execution_recording_guard=lambda:None
        class Provider:
            def discover_dynamic_candidates(self,*a,**k):return {'candidates':[]}
            def fetch(self,symbol,now):return {**deepcopy(market),'request_started_at':now.isoformat(),'observed_at':now.isoformat(),'available_at':now.isoformat()}
            def fetch_s5_inputs(self,symbol,now):return {k:deepcopy(market[k]) for k in ('s5_daily_context','s5_risk_context')}
        submitted=[]
        @contextmanager
        def queue(opener,request,**kwargs):
            kwargs['before_send']();submitted.append(json.loads(request.data))
            class Response:
                def read(self,n):return json.dumps({'return_code':0,'ord_no':'31' if len(submitted)==1 else '32'}).encode()
            yield Response()
        monitor=PaperExperimentMonitor(settings_loader=lambda:case.settings,now=lambda:case.now,
            store_path=Path(case.tmp.name)/'experiment.sqlite3',chart_fetcher=lambda *_:bars,
            provider_factory=lambda _:Provider(),account_fetcher=lambda _:account,
            calendar_reader=lambda at:{'start':at.replace(hour=9,minute=0,second=0,microsecond=0),
                'end':at.replace(hour=15,minute=30,second=0,microsecond=0)},batch_size=1)
        monitor.store=ExperimentStore(monitor.path)
        # A legitimate finite cohort assigns this fixture symbol to S5. No
        # assignment hashes, probabilities or outcomes are manually forged.
        for start in range(0,1000,5):
            cohort=assign_cohort([{'symbol':s,'first_seen_at':now.replace(hour=9,minute=0,second=0).isoformat()}
                for s in ['005930']+[f'{n:06d}' for n in range(start,start+5)]],day='20261030',
                snapshot_at=now-timedelta(minutes=1),profile='native-six-s5-v1')
            assignment=next(a for a in cohort['assignments'] if a['symbol']=='005930')
            if assignment['arm']==S5:break
        self.assertEqual(assignment['arm'],S5);monitor.store.assign(assignment)
        with patch('kiwoom_autotrader.api.orders.queued_open',side_effect=queue), \
             patch('kiwoom_autotrader.trading.paper_policy.load_settings',side_effect=lambda **kw:case.settings), \
             patch('kiwoom_autotrader.trading.paper_experiment_monitor.fetch_budget_limit',return_value=price):
            monitor.tick(session,lambda _:None)
        self.assertEqual(len(submitted),1,monitor.status())
        self.assertTrue(any(row.get('ignored_intent_id') is None for row in capacity_calls))
        self.assertTrue(any(row.get('ignored_intent_id') is not None for row in capacity_calls))
        self.assertTrue(all(row.get('strategy')==S5 for row in capacity_calls if row.get('principal') is not None))
        self.assertEqual(submitted[0]['ord_qty'],str(sizing['quantity']))
        with case.tracker._db() as db:
            row=db.execute("SELECT i.id,d.evidence_json FROM intents i JOIN order_decisions d ON d.intent_id=i.id AND d.scope=i.scope WHERE i.strategy=? AND i.state='accepted'",(S5,)).fetchone()
        self.assertIsNotNone(row);data=json.loads(row[1])
        self.assertEqual(data['strategy_version'],'paper-experiment-v1')
        self.assertEqual(len(data['config']['experiment_s5_signal']['minute_prefix']['rows']),60)
        self.assertTrue(monitor._generation_matches(case.tracker,'005930',assignment,row[0]))
        self.assertFalse(monitor._generation_matches(case.tracker,'005930',{**assignment,'assignment_sha256':'f'*64},row[0]))

        # Receive actual unique00 unit execution, then reconstruct ownership
        # with a fresh monitor. Receipt alone above was deliberately not a fill.
        from kiwoom_autotrader.api.executions import Execution
        case.now+=timedelta(seconds=1);quantity=sizing['quantity']
        case.tracker.record_broker_observation({'schema_version':1,'source_api':'00','source_kind':'broker_observation',
            'observed_at':case.now.isoformat(),'day':'20261030','mode':'paper','event_kind':'execution_stream',
            'values':{'9203':'31','9001':'A005930','907':'2','913':'체결','908':case.now.strftime('%H%M%S'),
                '909':'1','914':str(price),'915':str(quantity),'911':str(quantity),'903':str(price*quantity)}})
        case.tracker._ingest('paper','20261030',(Execution('20261030','31','1','005930','fixture','buy',
            case.now.strftime('%H:%M:%S'),Decimal(price),quantity),))
        position=case.tracker.multi_strategy_position('paper','005930',S5,case.now)
        self.assertEqual(position['status'],'owned',position)
        self.assertEqual(position['owned_qty'],quantity)
        case.now+=timedelta(minutes=30)
        replacement=PaperExperimentMonitor(settings_loader=lambda:case.settings,now=lambda:case.now,
            store_path=monitor.path,provider_factory=lambda _:Provider(),chart_fetcher=Mock(side_effect=AssertionError('timed exit requested chart')),
            calendar_reader=monitor.calendar)
        replacement.store=ExperimentStore(replacement.path);replacement.provider=Provider()
        replacement.paused_arms.pop(S5,None)
        owner,problem=replacement._owned_assignment(case.tracker,'005930',case.now)
        self.assertIsNone(problem);self.assertEqual(owner,assignment)
        with patch('kiwoom_autotrader.api.orders.queued_open',side_effect=queue), \
             patch('kiwoom_autotrader.trading.paper_policy.load_settings',side_effect=lambda **kw:case.settings):
            replacement._one(session,case.tracker,case.settings,'005930',case.now,owner)
        self.assertEqual(len(submitted),2,replacement.status())
        with case.tracker._db() as db:
            sell=db.execute("SELECT d.evidence_json FROM intents i JOIN order_decisions d ON d.intent_id=i.id AND d.scope=i.scope WHERE i.strategy=? AND i.side='sell' AND i.state='accepted'",(S5,)).fetchone()
        self.assertIsNotNone(sell)
        self.assertEqual(json.loads(sell[0])['config']['experiment_exit_reason'],'time_stop_30m')
        self.case=case
        self.native_context={'price':price,'quantity':quantity,'assignment':assignment,'monitor':monitor}
