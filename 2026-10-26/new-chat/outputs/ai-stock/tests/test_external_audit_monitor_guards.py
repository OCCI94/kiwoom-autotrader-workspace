"""Offline transition/manual protection contracts; no live transport or state."""
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch
from kiwoom_autotrader.api.session import KST
from kiwoom_autotrader.api.orders import OrderError
from kiwoom_autotrader.trading.legacy_resume_gate import LegacyResumeGate
from kiwoom_autotrader.trading.order_context import automated_orders
from kiwoom_autotrader.telegram.commands import orders
from kiwoom_autotrader.trading.decision_evidence import freeze_evidence


class TransitionTests(TestCase):
    def setUp(self):
        self.tmp=TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'gate.json';self.gate=LegacyResumeGate(self.path)
        self.cfg={'paper_experiment_enabled':True,'gdcrs_enabled':True,'ddcrs_enabled':True,
                  'paper_multi_strategy_enabled':True,'paper_multi_strategy_ids':['S5_steady_uptrend_vwap']}
    def test_off_restart_requires_explicit_exact_settings_confirmation(self):
        self.assertFalse(self.gate.observe(self.cfg))
        self.cfg['paper_experiment_enabled']=False
        self.assertFalse(self.gate.observe(self.cfg))
        restarted=LegacyResumeGate(self.path)
        self.assertFalse(restarted.observe(self.cfg))
        st=restarted.status(self.cfg)
        self.assertTrue(st['confirmation_required']);self.assertIn('S5_steady_uptrend_vwap',st['legacy_resume_targets'])
        with self.assertRaises(ValueError):restarted.confirm(self.cfg,'wrong')
        restarted.confirm(self.cfg,st['confirmation_token'])
        self.assertTrue(restarted.observe(self.cfg))
        self.cfg['gdcrs_enabled']=False
        self.assertFalse(restarted.observe(self.cfg))
    def test_status_distinguishes_native_active_from_stored_legacy_paused(self):
        from kiwoom_autotrader.telegram.commands.settings import _transition_display
        self.cfg.update(paper_experiment_s5_enabled=True,gdcrs_stocks=[{'code':'072950','budget':1000000}])
        lines=' '.join(_transition_display(self.cfg,SimpleNamespace(mode='paper',legacy_resume_gate=self.gate)))
        self.assertIn('S5_steady_uptrend_vwap',lines);self.assertIn('S2~S4',lines);self.assertIn('PAUSED',lines)
        self.assertIn('현재 활성 전략 목록이 아니',lines);self.assertIn('등록 1종목',lines);self.assertIn('1,000,000원',lines)

    def test_optional_session_gate_does_not_materialize_dynamic_attributes(self):
        from kiwoom_autotrader.trading.legacy_resume_gate import session_gate
        session=Mock(mode='paper')
        self.assertIsInstance(session_gate(session),LegacyResumeGate)
        self.assertNotIn('legacy_resume_gate',session._mock_children)
        session.legacy_resume_gate=Mock()
        with self.assertRaises(ValueError):session_gate(session)
        session.legacy_resume_gate=self.gate
        self.assertIs(session_gate(session),self.gate)

    def test_active_mode_revokes_previous_confirmation(self):
        self.gate.observe(self.cfg);self.cfg['paper_experiment_enabled']=False;self.gate.observe(self.cfg)
        self.gate.confirm(self.cfg,self.gate.status(self.cfg)['confirmation_token'])
        self.cfg['paper_experiment_enabled']=True;self.gate.observe(self.cfg)
        self.cfg['paper_experiment_enabled']=False;self.assertFalse(self.gate.observe(self.cfg))
    def test_never_experimental_legacy_unaffected_and_corrupt_marker_fails_closed(self):
        self.cfg['paper_experiment_enabled']=False;self.assertTrue(self.gate.observe(self.cfg))
        self.path.write_text('{')
        with self.assertRaises(ValueError):self.gate.observe(self.cfg)


class ManualProtectionTests(TestCase):
    def setUp(self):
        self.tracker=Mock();self.tracker.assert_no_pending_order=Mock();self.session=SimpleNamespace(mode='paper',token='fixture',
            paper_automation_reconcile=SimpleNamespace(_tracker=self.tracker))
        self.report={'mode':'paper','exchange':'KRX','snapshot_changed':False,
            'holdings':[{'stk_cd':'072950','crd_tp':'00','crd_loan_dt':'','rmnd_qty':'7','trde_able_qty':'7','pur_amt':'99400'}]}
        self.contexts=[];self.calls=[]
        self.addCleanup(patch.stopall)
        patch('kiwoom_autotrader.api.account_report.fetch_account_report',return_value=self.report).start()
        patch('kiwoom_autotrader.api.minute_chart.session_identity',side_effect=lambda s:(s.mode,s.token)).start()
        @contextmanager
        def context(tracker,strategy,**kwargs):
            self.contexts.append((strategy,kwargs['evidence']));yield
        patch.object(orders,'automated_orders',side_effect=context).start()
        def submit(session,side,symbol,quantity,price,**kwargs):
            kwargs['before_send']();self.calls.append((side,symbol,quantity,price))
            return SimpleNamespace(mode='paper',side=side,symbol=symbol,quantity=quantity,price=None,order_number='123',exchange='KRX')
        self.submit=patch.object(orders,'submit_order',side_effect=submit).start()
    def test_explicit_command_records_manual_unconfirmed_and_exact_broker_qty(self):
        reply=orders.handle_sell('sell protect 072950',self.session)
        self.assertTrue(reply.submitted);self.assertEqual(self.calls,[('sell','072950',7,None)])
        strategy,evidence=self.contexts[0];self.assertEqual(strategy,'manual_protective_exit')
        self.assertEqual(evidence['config']['profit_attribution'],'unconfirmed')
        freeze_evidence(evidence)
        self.tracker.assert_no_pending_order.assert_called_once_with('paper','072950')
    def test_pending_ambiguous_zero_or_real_cannot_send(self):
        self.tracker.assert_no_pending_order.side_effect=ValueError('pending')
        self.assertFalse(orders.handle_sell('sell protect 072950',self.session).submitted)
        self.submit.assert_not_called()
        self.tracker.assert_no_pending_order.side_effect=None
        self.report['holdings'][0]['trde_able_qty']='0'
        orders.handle_sell('sell protect 072950',self.session);self.submit.assert_not_called()
        self.session.mode='real';orders.handle_sell('sell protect 072950',self.session);self.submit.assert_not_called()
    def test_uncertain_submit_is_not_retryable(self):
        self.submit.side_effect=RuntimeError('lost transport response')
        response=orders.handle_sell('sell protect 072950',self.session)
        self.assertTrue(response.uncertain);self.assertIsNone(response.submitted);self.assertEqual(self.submit.call_count,1)
    def test_automatic_context_cannot_invoke_manual_command(self):
        with automated_orders(self.tracker,'gdcrs'):
            result=orders.handle_sell('sell protect 072950',self.session)
        self.assertIn('명시적',result[0]);self.submit.assert_not_called()


class ExitConsumerTests(TestCase):
    def setUp(self):
        from tests.test_paper_experiment_monitor import MonitorTests
        self.c=MonitorTests('runTest');self.c.setUp();self.addCleanup(self.c.doCleanups)
    def fallback(self,value,*,timing=True):
        from tests.test_paper_experiment_monitor import START
        self.c.tracker.positions[(value['symbol'],value['arm'])]={'status':'unsupported'}
        self.position={'status':'exit_only_broker_confirmed','ownership_status':'exit_only_broker_confirmed',
            'owned_qty':3,'broker_quantity':3,'basis_krw':3000,'generation_id':'a'*32,
            'first_fill_timing_verified':timing,'first_fill_at':START.isoformat() if timing else None,
            'first_fill_observed_at':START.isoformat() if timing else None,'profit_attribution':'unconfirmed','evidence':{}}
        self.c.tracker.paper_exit_only_position=Mock(side_effect=lambda mode,symbol,arm,now:
            dict(self.position) if symbol==value['symbol'] and arm==value['arm'] else {'status':'unsupported','broker_quantity':3})
        self.c.tracker.open_symbols=[value['symbol']]
    def test_all_fourteen_arms_consume_separate_exit_status_and_recover_assignment(self):
        from kiwoom_autotrader.config.settings import PAPER_EXPERIMENT_STRATEGIES
        from kiwoom_autotrader.trading.multi_strategy import S5
        from kiwoom_autotrader.trading.paper_experiment_rules import assign_candidate
        from tests.test_paper_experiment_monitor import START
        from unittest.mock import patch
        # Assignment normalization stays covered by production fixtures; this
        # test checks routing of every existing arm, not new arm construction.
        for arm in (*PAPER_EXPERIMENT_STRATEGIES,S5):
            with self.subTest(arm=arm):
                value=assign_candidate('20261102','000001',START)
                value={**value,'arm':arm}
                self.fallback(value)
                self.assertEqual(self.c.monitor._position(self.c.tracker,'000001',arm,self.c.clock)['status'],'exit_only_broker_confirmed')
                with patch.object(self.c.monitor.store,'recent_assignments',return_value=[value]),patch('kiwoom_autotrader.trading.paper_experiment_monitor.validate_assignment'):
                    found,reason=self.c.monitor._owned_assignment(self.c.tracker,'000001',self.c.clock)
                self.assertEqual(found['arm'],arm);self.assertIsNone(reason)
    def test_changed_exit_quantity_before_dispatch_is_not_sent_or_retried(self):
        from tests.test_paper_experiment_monitor import assignment
        value=self.c.seed(assignment('EXP_B_TP'));self.fallback(value)
        self.c.tracker.paper_exit_only_position.side_effect=[dict(self.position),{**self.position,'owned_qty':2}]
        with self.assertRaisesRegex(ValueError,'exit_only_broker_evidence_changed'):self.c.one(value)
        self.assertFalse(self.c.orders)
        self.assertTrue(any(e.get('skip_reason')=='ValueError' for e in self.c.events))
        self.c.tracker.paper_exit_only_position.side_effect=lambda *args:dict(self.position)
        self.c.one(value);self.assertFalse(self.c.orders)
    def test_s5_without_first_fill_clock_can_exit_price_but_not_time(self):
        from kiwoom_autotrader.trading.multi_strategy import S5
        from kiwoom_autotrader.trading.paper_experiment_rules import assign_candidate
        from tests.test_paper_experiment_monitor import START
        from datetime import timedelta
        for n in range(10000):
            value=assign_candidate('20261102',f'{n:06d}',START,profile='native-six-s5-v1')
            if value['arm']==S5:break
        value=self.c.seed(value);self.fallback(value,timing=False);self.c.monitor.paused_arms.pop(S5,None)
        self.c.clock=START+timedelta(minutes=40)
        self.c.provider.fetch.side_effect=lambda symbol,now:{'current_price':1200,'source_sha256':'a'*64}
        self.c.one(value)
        self.assertEqual(self.c.orders[0][0],'sell');self.assertEqual(self.c.orders[0][2],3)
        proof=self.c.contexts[-1][1]['evidence'];freeze_evidence(proof)
        self.assertIsNone(proof['config']['experiment_s5_exit']['first_fill_at'])
        self.assertEqual(proof['config']['experiment_exit_reason'],'take_profit_10pct')
    def test_unverified_ownership_alert_has_quantity_and_direct_command(self):
        self.c.tracker.paper_exit_only_position=Mock(return_value={'status':'unsupported','broker_quantity':7})
        self.c.monitor._protection_unavailable('072950','ownership_unsupported',self.c.tracker,self.c.notify)
        self.c.monitor._protection_unavailable('072950','ownership_unsupported',self.c.tracker,self.c.notify)
        self.c.notify.assert_called_once();message=self.c.notify.call_args.args[0]
        self.assertIn('수동 매도',message);self.assertIn('7주',message);self.assertIn('sell protect 072950',message)


class RealAutomaticSellTests(TestCase):
    def test_real_auto_sell_denied_before_settings_ledger_or_network(self):
        from kiwoom_autotrader.trading.paper_policy import _AutomaticPaperGuard,PaperAutomationPolicyError
        session=SimpleNamespace(mode='real');context=SimpleNamespace(strategy='ddcrs',tracker=Mock(),evidence=None)
        guard=_AutomaticPaperGuard(context,session,'sell','072950','7',None)
        with patch('kiwoom_autotrader.trading.paper_policy.load_settings',side_effect=AssertionError('settings not needed')):
            with self.assertRaisesRegex(PaperAutomationPolicyError,'수동 매도'):
                guard.before_prepare()
        context.tracker.assert_not_called()


class ExitReconciliationPolicyTests(TestCase):
    def test_schema7_required_only_for_exit_only_and_forwarded_to_reconciler(self):
        from kiwoom_autotrader.trading.paper_policy import _AutomaticPaperGuard,PaperAutomationPolicyError
        for exit_only,schema,allowed in ((True,7,True),(True,4,False),(False,7,False),(False,4,True)):
            with self.subTest(exit_only=exit_only,schema=schema):
                tracker=Mock();tracker.assert_no_pending_order=Mock()
                evidence={'config':{'multi_exit_kind':'full','multi_position_generation_id':'a'*32}}
                if exit_only:evidence['config'].update(ownership_status='exit_only_broker_confirmed',profit_attribution='unconfirmed')
                proofguard=Mock();proofguard.reconciliation_evidence={'schema_version':schema}
                reconciler=Mock(return_value=proofguard)
                session=SimpleNamespace(mode='paper',paper_automation_reconcile=reconciler)
                context=SimpleNamespace(tracker=tracker,strategy='EXP_B_TP',evidence=SimpleNamespace(as_dict=lambda:evidence))
                guard=_AutomaticPaperGuard(context,session,'sell','072950','7',None)
                with patch('kiwoom_autotrader.trading.paper_policy.load_settings',return_value={'paper_experiment_enabled':True}):
                    if allowed:self.assertTrue(guard.before_prepare());guard()
                    else:
                        with self.assertRaises(PaperAutomationPolicyError):guard.before_prepare()
                self.assertEqual(reconciler.call_args.kwargs.get('ownership_status'),'exit_only_broker_confirmed' if exit_only else None)

class ExitReconciliationBridgeTests(TestCase):
    def test_full_real_schema_consumes_broker_only_proof_and_integrated_pending(self):
        from tests.test_paper_reconciliation import PaperReconciliationTests,account
        from kiwoom_autotrader.trading.paper_reconciliation import PaperReconciliationGate,PaperReconciliationError
        f=PaperReconciliationTests('runTest');f.setUp();self.addCleanup(f.doCleanups)
        row={'stk_cd':'072950','rmnd_qty':'7','trde_able_qty':'7','pur_amt':'99400','crd_tp':'00','crd_loan_dt':''}
        f.account.return_value=account([row]);f.tracker.symbol_order_watermark.return_value='a'*64
        f.tracker.record_strategy_holding_observation.return_value=20
        f.tracker.paper_exit_only_position.return_value={'status':'exit_only_broker_confirmed','generation_id':'b'*32,
            'owned_qty':7,'basis_krw':99400,'evidence':{'fixture':'strict_final_ledger_contract_separately_tested'}}
        with patch('kiwoom_autotrader.trading.paper_reconciliation._pending_rows',return_value=[]) as pending, \
             patch('kiwoom_autotrader.trading.paper_reconciliation.load_settings',return_value={'paper_experiment_enabled':True}):
            details=Mock(return_value=[])
            gate=PaperReconciliationGate(f.session,f.tracker,executions_fetcher=f.executions,account_fetcher=f.account,
                now=lambda:f.now,monotonic=lambda:f.clock,detail_fetcher=details)
            gate._holdings_source='broker_observation'
            guard=gate('072950',side='sell',quantity=7,strategy='EXP_ORB_A',exit_kind='full',generation_id='b'*32,
                ownership_status='exit_only_broker_confirmed')
            proof=guard.reconciliation_evidence
            self.assertEqual(proof['schema_version'],7);self.assertEqual(proof['profit_attribution'],'unconfirmed')
            self.assertTrue(pending.call_args.kwargs['integrated']);details.assert_called_once_with(f.session,'20261027',observer=f.tracker.record_broker_observation);guard()
            f.status['gap_count']+=1
            with self.assertRaises(PaperReconciliationError):guard()

    def test_exit_detail_reader_preserves_full_exchange_scope_and_observer(self):
        from kiwoom_autotrader.trading.paper_reconciliation import _exit_order_details
        session=object();observer=Mock()
        with patch('kiwoom_autotrader.api.executions._request_rows',return_value=[]) as request:
            _exit_order_details(session,'20261103',observer=observer)
        args=request.call_args
        self.assertIs(args.args[0],session);self.assertEqual(args.args[1],'kt00007')
        self.assertEqual(args.args[2]['dmst_stex_tp'],'%');self.assertEqual(args.args[2]['stk_cd'],'')
        self.assertEqual(args.args[2]['qry_tp'],'1');self.assertIs(args.kwargs['observer'],observer)
