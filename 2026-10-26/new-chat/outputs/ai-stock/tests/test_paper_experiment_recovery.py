"""No-network paper recovery orchestration tests."""
from datetime import datetime
from unittest import TestCase
from unittest.mock import Mock,patch
from zoneinfo import ZoneInfo
from kiwoom_autotrader.trading import paper_experiment_recovery as recovery

class RecoveryTests(TestCase):
    def setUp(self):
        self.now=datetime(2026,11,2,10,0,tzinfo=ZoneInfo('Asia/Seoul'))
        self.session=Mock();self.session.execution_recorder=None;self.tracker=Mock();self.tracker.settings_loader.return_value={'paper_experiment_enabled':True}
        self.tracker._moment.return_value=self.now
        self.db=Mock();self.context=Mock();self.context.__enter__=Mock(return_value=self.db);self.context.__exit__=Mock(return_value=False)
        self.tracker._db.return_value=self.context;self.tracker._pending_order_rows.return_value=[]
    @patch.object(recovery,'session_identity',return_value=('real','private'))
    def test_real_does_not_fetch_or_read_experiment_settings(self,identity):
        self.assertEqual(recovery.recover_paper_experiment(self.session,self.tracker,self.now)['status'],'not_paper')
        self.tracker.settings_loader.assert_not_called()
    @patch.object(recovery,'session_identity',return_value=('paper','private'))
    @patch.object(recovery,'_prior_days',return_value=['20261030'])
    @patch.object(recovery,'_request_rows',return_value=[])
    @patch.object(recovery,'fetch_executions',return_value=())
    @patch.object(recovery,'fetch_account_report',return_value={'mode':'paper'})
    @patch.object(recovery,'_pending_rows',return_value=[])
    def test_complete_pages_are_recorded_without_claiming_ownership_or_recorder_recovery(self,pending,account,fills,detail,days,identity):
        result=recovery.recover_paper_experiment(self.session,self.tracker,self.now)
        self.assertTrue(result['reconciled']);self.assertFalse(result['ownership_recovered']);self.assertFalse(result['recorder_recovered'])
        self.assertEqual(result['prior_days'],['20261030']);self.assertEqual(detail.call_count,2);pending.assert_called_once()
        self.assertEqual(result['orders_sent'],0)
    @patch.object(recovery,'session_identity',return_value=('paper','private'))
    @patch.object(recovery,'_prior_days',return_value=['20261030'])
    @patch.object(recovery,'_request_rows',side_effect=ValueError('partial'))
    def test_incomplete_detail_stops_without_releasing_pending(self,detail,days,identity):
        notify=Mock();result=recovery.recover_paper_experiment(self.session,self.tracker,self.now,notify)
        self.assertFalse(result['reconciled']);self.tracker._ingest.assert_not_called();notify.assert_called_once()

class RecorderRecoveryTests(TestCase):
    def setUp(self):
        from types import SimpleNamespace
        from tests.test_execution_stream import FakeEngine
        from kiwoom_autotrader.realtime.execution_stream import ExecutionStreamRecorder
        self.now=datetime(2026,11,2,10,0,tzinfo=ZoneInfo('Asia/Seoul'));self.engine=FakeEngine()
        self.session=SimpleNamespace(mode='paper',token='fake-private',token_usable=True,websocket_engine=self.engine)
        self.events=[];self.tracker=Mock()
        self.tracker.record_broker_observation.side_effect=lambda event:self.events.append(event)
        def anchor(moment):
            for i in range(len(self.events)-1,-1,-1):
                if self.events[i]['event_kind'] in ('stream_started','stream_resumed'):
                    return i+1,'a'*64,self.events[i]['observed_at']
                if self.events[i]['event_kind'] in ('stream_gap','stream_stopped'):raise ValueError('gap')
            raise ValueError('no anchor')
        self.tracker.stream_anchor.side_effect=anchor;self.tracker.validate_paper_stream_recovery.return_value=True
        self.recorder=ExecutionStreamRecorder(self.session,self.tracker,now=lambda:self.now,poll_interval=5)
        self.addCleanup(self.recorder.stop);self.recorder.start();self.engine.ready=True;self.recorder.status()
        self.recorder._error='prior storage failure'
    def test_complete_reconciliation_releases_latch_but_retains_gap(self):
        checkpoint=self.recorder.begin_paper_recovery()
        self.assertTrue(self.recorder.complete_paper_recovery(checkpoint,{'fixture':True}))
        state=self.recorder.status();self.assertTrue(state['ready']);self.assertFalse(state['coverage_complete'])
        self.assertEqual([e['event_kind'] for e in self.events],['stream_started','stream_gap','stream_resumed'])
    def test_generation_change_or_failed_validation_remains_blocked(self):
        checkpoint=self.recorder.begin_paper_recovery();self.engine.generation+=1
        self.assertFalse(self.recorder.complete_paper_recovery(checkpoint,{'fixture':True}))
        self.assertIsNotNone(self.recorder._error)
    def test_unverified_reconciliation_does_not_clear_latch(self):
        checkpoint=self.recorder.begin_paper_recovery();self.tracker.validate_paper_stream_recovery.return_value=False
        self.assertFalse(self.recorder.complete_paper_recovery(checkpoint,{}));self.assertIsNotNone(self.recorder._error)

class OwnedGapIntegrationTests(TestCase):
    def test_proven_owned_lot_recovers_after_gap_only_with_complete_broker_reconciliation(self):
        from tests.test_multi_strategy_epoch_review import EpochIndependentReview, S2
        from kiwoom_autotrader.api.orders import OrderReceipt
        from kiwoom_autotrader.api.executions import Execution
        from decimal import Decimal
        import json
        case=EpochIndependentReview('test_v5_new_buy_complete_fill_creates_owned_generation_and_sell');case.setUp()
        self.addCleanup(case.doCleanups)
        ident=case.prepare(case.proof());case.now+=__import__('datetime').timedelta(seconds=1)
        case.tracker.register_order(OrderReceipt('paper','buy','005930','10','1000','31','https://mockapi.kiwoom.com'),S2,intent_id=ident)
        case.tracker.record_broker_observation({'schema_version':1,'source_api':'00','source_kind':'broker_observation',
          'observed_at':case.now.isoformat(),'day':'20261030','mode':'paper','event_kind':'execution_stream',
          'values':{'9203':'31','9001':'A005930','907':'2','913':'체결','908':'100011','909':'1','914':'1000','915':'10','911':'10','903':'10000'}})
        case.tracker._ingest('paper','20261030',(Execution('20261030','31','1','005930','fixture','buy','10:00:11',Decimal('1000'),10),))
        case.settings['paper_experiment_enabled']=True
        case.now+=__import__('datetime').timedelta(seconds=1);gap=case.now
        def stream(kind):
            case.tracker.record_broker_observation({'schema_version':1,'source_api':'00','source_kind':'broker_observation',
              'observed_at':case.now.isoformat(),'day':'20261030','mode':'paper','event_kind':kind,'values':{}})
        stream('stream_gap');case.now+=__import__('datetime').timedelta(seconds=1);stream('stream_resumed')
        self.assertEqual(case.tracker.multi_strategy_position('paper','005930',S2,case.now)['status'],'unsupported')
        case.now+=__import__('datetime').timedelta(seconds=1);start=case.now
        event={'schema':'kiwoom-broker-page-v1','source_api':'kt00007','source_kind':'broker_observation','mode':'paper',
          'day':'20261030','query_id':'d'*32,'request_started_at':start.isoformat(),'observed_at':start.isoformat(),
          'page':1,'request_body':{'ord_dt':'20261030','qry_tp':'1','stk_bond_tp':'1','sell_tp':'0','stk_cd':'','fr_ord_no':'','dmst_stex_tp':'%'},
          'response_body':{'return_code':0,'acnt_ord_cntr_prps_dtl':[{'ord_no':'31','stk_cd':'A005930','ord_qty':'10','cntr_qty':'10','ord_remnq':'0','io_tp_nm':'현금매수'}]},
          'continuation':{'request_cont_yn':'N','response_cont_yn':'N','next_key_present':False},'status':'success','http_status':200}
        case.tracker.record_broker_observation(event);case.now+=__import__('datetime').timedelta(seconds=1)
        snapshot={'schema':'paper-account-snapshot-v1','mode':'paper','exchange':'KRX',
          'holdings':[{'stk_cd':'A005930','rmnd_qty':'10','pur_amt':'10000','crd_tp':'00'}],
          'cash':{'entr':'1000000','ord_alow_amt':'1000000'}}
        with case.tracker._db() as db:
            db.execute('INSERT OR REPLACE INTO account_balance_daily VALUES(?,?,?,?,?,?)',
              ('default','paper','20261030',case.now.isoformat(),'broker_observation',json.dumps(snapshot)))
        case.now+=__import__('datetime').timedelta(seconds=1)
        pending={'schema':'kiwoom-broker-page-v1','source_api':'ka10075','source_kind':'broker_observation','mode':'paper',
          'day':'20261030','query_id':'e'*32,'request_started_at':case.now.isoformat(),'observed_at':case.now.isoformat(),
          'page':1,'request_body':{'all_stk_tp':'0','trde_tp':'0','stex_tp':'0'},'response_body':{'return_code':0,'oso':[]},
          'continuation':{'request_cont_yn':'N','response_cont_yn':'N','next_key_present':False},'status':'success','http_status':200}
        case.tracker.record_broker_observation(pending)
        position=case.tracker.multi_strategy_position('paper','005930',S2,case.now)
        self.assertEqual((position['status'],position['owned_qty'],position['generation_id']),('owned',10,ident))
        case.settings['paper_experiment_enabled']=False
        legacy=case.tracker.multi_strategy_position('paper','005930',S2,case.now)
        self.assertEqual((legacy['status'],legacy['owned_qty']),('owned',10))
        case.now+=__import__('datetime').timedelta(seconds=91)
        self.assertEqual(case.tracker.multi_strategy_position('paper','005930',S2,case.now)['status'],'unsupported')

class SymbolEpochIntegrationTests(TestCase):
    def test_symbol_epoch_accepts_other_symbol_broker_pending(self):
        from tests.test_multi_strategy_epoch_review import EpochIndependentReview
        from kiwoom_autotrader.trading.paper_policy import experiment_order_sizing
        import json
        case=EpochIndependentReview('test_complete_zero_broker_evidence_allows_new_generation_without_touching_old_history');case.setUp()
        self.addCleanup(case.doCleanups)
        proof=case.proof();proof['pending_query_id']=case.pending_page(rows=[{'stk_cd':'000660','ord_no':'7','oso_qty':'1','stex_tp':'1'}])
        case.settings['paper_experiment_enabled']=True
        sizing=experiment_order_sizing(case.settings,'paper',available_cash_krw=3000000,price=1000,
                                        existing_principal_krw=0,pending_principal_krw=0)
        proof.update(schema_version=6,integrated_pending_clear=False,symbol_pending_clear=True,
          paper_experiment_sizing=sizing,symbol_cap_krw=sizing['effective_symbol_cap_krw'],per_buy_cap_krw=sizing['budget_krw'],remaining_principal_krw=sizing['effective_symbol_cap_krw'])
        account={'schema':'paper-account-snapshot-v1','mode':'paper','exchange':'KRX',
                 'holdings':[],'cash':{'entr':'3000000','ord_alow_amt':'3000000'}}
        with case.tracker._db() as db:
            db.execute('INSERT OR REPLACE INTO account_balance_daily VALUES(?,?,?,?,?,?)',
              ('default','paper','20261030',proof['observed_at'],'broker_observation',json.dumps(account)))
        # This isolates the new epoch semantics; taxonomy/capacity have their own tests.
        case.tracker._assert_position_capacity=Mock(return_value={})
        self.assertTrue(case.prepare(proof))

    def test_symbol_epoch_rejects_same_symbol_pending_and_inactive_experiment(self):
        from tests.test_multi_strategy_epoch_review import EpochIndependentReview
        from kiwoom_autotrader.trading.execution_log import ExecutionLogError
        case=EpochIndependentReview('test_complete_zero_broker_evidence_allows_new_generation_without_touching_old_history');case.setUp()
        self.addCleanup(case.doCleanups);proof=case.proof()
        proof.update(schema_version=6,integrated_pending_clear=False,symbol_pending_clear=True)
        case.settings['paper_experiment_enabled']=True
        proof['pending_query_id']=case.pending_page(rows=[{'stk_cd':'005930','ord_no':'7','oso_qty':'1','stex_tp':'1'}])
        with case.tracker._db() as db:
            with self.assertRaises(ExecutionLogError):case.tracker._assert_epoch_proof(db,proof,case.now)
        proof['pending_query_id']=case.pending_page();case.settings['paper_experiment_enabled']=False
        with case.tracker._db() as db:
            with self.assertRaises(ExecutionLogError):case.tracker._assert_epoch_proof(db,proof,case.now)

class ExperimentDispatchIntegrationTests(TestCase):
    def test_owned_zero_broker_experiment_buy_goes_through_actual_policy_and_fake_queue(self):
        from contextlib import contextmanager
        from datetime import timedelta
        import json
        from tests.test_multi_strategy_epoch_review import EpochIndependentReview
        from tests.test_orders import fake_session
        from kiwoom_autotrader.api.minute_chart import MinuteBar
        from kiwoom_autotrader.api.orders import submit_order
        from kiwoom_autotrader.trading.order_context import automated_orders
        from kiwoom_autotrader.trading.paper_experiment_monitor import PaperExperimentMonitor
        from kiwoom_autotrader.trading.paper_policy import experiment_order_sizing
        from unittest.mock import patch
        case=EpochIndependentReview('test_complete_zero_broker_evidence_allows_new_generation_without_touching_old_history');case.setUp()
        self.addCleanup(case.doCleanups);proof=case.proof();case.settings['paper_experiment_enabled']=True
        sizing=experiment_order_sizing(case.settings,'paper',available_cash_krw=3000000,price=1000,
                                        existing_principal_krw=0,pending_principal_krw=0)
        proof.update(schema_version=6,integrated_pending_clear=False,symbol_pending_clear=True,
          paper_experiment_sizing=sizing,symbol_cap_krw=sizing['effective_symbol_cap_krw'],per_buy_cap_krw=sizing['budget_krw'],remaining_principal_krw=sizing['effective_symbol_cap_krw'])
        account={'schema':'paper-account-snapshot-v1','mode':'paper','exchange':'KRX',
                 'holdings':[],'cash':{'entr':'3000000','ord_alow_amt':'3000000'}}
        with case.tracker._db() as db:
            db.execute('INSERT OR REPLACE INTO account_balance_daily VALUES(?,?,?,?,?,?)',
              ('default','paper','20261030',proof['observed_at'],'broker_observation',json.dumps(account)))
        case.tracker._assert_position_capacity=Mock(return_value={})
        session=fake_session(clock=lambda:case.now)
        class Reconcile:
            def __call__(self,symbol,**kwargs):
                def guard():pass
                guard.reconciliation_evidence=proof
                return guard
        session.paper_automation_reconcile=Reconcile();session.execution_recording_guard=lambda:None
        submitted=[]
        @contextmanager
        def queue(opener,request,**kwargs):
            kwargs['before_send']();submitted.append(json.loads(request.data))
            class Response:
                def read(self,n):return json.dumps({'return_code':0,'ord_no':str(30+len(submitted))}).encode()
            yield Response()
        monitor=PaperExperimentMonitor(settings_loader=lambda:case.settings,now=lambda:case.now)
        assignment={'arm':'EXP_A','symbol':'005930','first_seen_at':(case.now-timedelta(minutes=3)).isoformat(),
                    'assignment_sha256':'b'*64}
        result={'action':'buy','reason':'entry_signal','signal_at':case.now.replace(second=0,microsecond=0).isoformat()}
        bars=[MinuteBar(case.now.replace(second=0)-timedelta(minutes=21-i),1000,100,
              source_symbol='005930',source_mode='paper',source_query_started_at=case.now,
              source_response_sha256='a'*64,open_price=1000,high_price=1001,low_price=999) for i in range(21)]
        evidence=monitor._evidence(assignment,result,bars,case.now,budget=10000)
        with patch('kiwoom_autotrader.api.orders.queued_open',side_effect=queue), \
             patch('kiwoom_autotrader.trading.paper_policy.load_settings',side_effect=lambda **kw:case.settings):
            with automated_orders(case.tracker,'EXP_A',evidence=evidence):
                receipt=submit_order(session,'buy','005930',10,1000,before_send=lambda:None)
        self.assertEqual(receipt.order_number,'31');self.assertEqual(len(submitted),1)

        from decimal import Decimal
        from kiwoom_autotrader.api.executions import Execution
        case.now+=timedelta(seconds=1)
        case.tracker.record_broker_observation({'schema_version':1,'source_api':'00','source_kind':'broker_observation',
          'observed_at':case.now.isoformat(),'day':'20261030','mode':'paper','event_kind':'execution_stream',
          'values':{'9203':'31','9001':'A005930','907':'2','913':'체결','908':'100011','909':'1','914':'1000','915':'10','911':'10','903':'10000'}})
        case.tracker._ingest('paper','20261030',(Execution('20261030','31','1','005930','fixture','buy','10:00:11',Decimal('1000'),10),))
        position=case.tracker.multi_strategy_position('paper','005930','EXP_A',case.now)
        self.assertEqual(position['status'],'owned')
        holding=case.tracker.record_strategy_holding_observation('005930',10,10,10000,case.now,source_kind='broker_observation')
        proof={'schema_version':4,'symbol':'005930','pre_order_held_qty':10,'pre_order_pur_amt_krw':10000,
          'order_quantity':10,'observed_at':case.now.isoformat(),'holdings_source':'broker_observation',
          'generation_id':position['generation_id'],'exit_kind':'full','strategy':'EXP_A','holding_observation_id':holding,
          'symbol_ledger_sha256':case.tracker.symbol_order_watermark('paper','005930')}
        sell_result={'action':'sell','reason':'dead_cross','signal_at':case.now.replace(second=0,microsecond=0).isoformat()}
        sell_evidence=monitor._evidence(assignment,sell_result,bars,case.now,budget=1,position=position)
        with patch('kiwoom_autotrader.api.orders.queued_open',side_effect=queue), \
             patch('kiwoom_autotrader.trading.paper_policy.load_settings',side_effect=lambda **kw:case.settings):
            with automated_orders(case.tracker,'EXP_A',evidence=sell_evidence):
                sold=submit_order(session,'sell','005930',10,None,before_send=lambda:None)
        self.assertEqual(sold.order_number,'32');self.assertEqual(len(submitted),2)
