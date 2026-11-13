"""Credential-free failure metadata and success-clearing behavior."""
import unittest
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch
import tests.test_multi_strategy_flat_candidate as fixture_monitor
import tests.test_paper_strategy_context as fixture_context
from kiwoom_autotrader.trading.paper_strategy_context import ContextUnavailable
from kiwoom_autotrader.trading.multi_strategy import MultiStrategyMonitor, S2

class InputDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.fx=fixture_monitor.FlatCandidateMonitorTests('test_candidate_is_visible_and_first_observation_is_baseline')
        self.fx.setUp();self.addCleanup(self.fx.doCleanups)
    def test_failure_code_is_safe_and_success_without_signal_clears_it(self):
        original=self.fx.monitor._provider_factory
        class Broken:
            def fetch(self,*a,**kw):raise ContextUnavailable('PRIVATE_RAW_TOKEN',code='price_conflict')
        self.fx.monitor._provider_factory=lambda _:Broken()
        self.fx.tick()
        status=self.fx.monitor.status()
        self.assertEqual(status['symbol_input_diagnostics']['005930']['code'],'price_conflict')
        self.assertEqual(status['symbol_input_diagnostics']['005930']['stage'],'entry_quote')
        self.assertNotIn('PRIVATE_RAW_TOKEN',repr(status))
        self.assertNotIn('005930',status['symbol_last_validated_at'])
        self.fx.monitor._provider=original(None)
        self.fx.clock[0]+=timedelta(minutes=1);self.fx.tick()
        status=self.fx.monitor.status()
        self.assertNotIn('005930',status['symbol_errors'])
        self.assertNotIn('005930',status['symbol_input_diagnostics'])
        self.assertEqual(status['symbol_last_validated_at']['005930'],
             {'observed_at':self.fx.clock[0].isoformat(),'stage':'entry_bars_and_quote'})
        self.assertEqual(self.fx.calls,[]) # first successful signal is baseline
    def test_unknown_provider_exception_never_exposes_text(self):
        class Broken:
            def fetch(self,*a,**kw):raise RuntimeError('CREDENTIAL_SECRET')
        self.fx.monitor._provider_factory=lambda _:Broken()
        self.fx.tick();status=self.fx.monitor.status()
        self.assertEqual(status['symbol_input_diagnostics']['005930']['code'],'other')
        self.assertNotIn('CREDENTIAL_SECRET',repr(status))
    def test_exit_quote_does_not_clear_sma_warning_until_sma_confirmed(self):
        monitor=self.fx.monitor
        monitor._warning('005930','sma20_unavailable',lambda _:None)
        monitor._mark_input_validated('005930',self.fx.clock[0],'exit_quote')
        self.assertEqual(monitor.status()['symbol_errors']['005930'],'sma20_unavailable')
        monitor._mark_input_validated('005930',self.fx.clock[0],'exit_quote_and_sma')
        self.assertNotIn('005930',monitor.status()['symbol_errors'])
    def test_exit_quote_and_sma_do_not_clear_missing_cutoff_bar(self):
        monitor=self.fx.monitor
        monitor._warning('005930','cutoff_bar_unavailable',lambda _:None)
        monitor._mark_input_validated('005930',self.fx.clock[0],'exit_quote')
        monitor._mark_input_validated('005930',self.fx.clock[0],'exit_quote_and_sma')
        monitor._warning('005930','sma20_unavailable',lambda _:None)
        self.assertEqual(monitor.status()['symbol_errors']['005930'],'cutoff_bar_unavailable')
        monitor._mark_input_validated('005930',self.fx.clock[0],'exit_cutoff_bar')
        self.assertNotIn('005930',monitor.status()['symbol_errors'])
    def test_repeated_fresh_exit_quote_does_not_repeat_sma_warning(self):
        clock=[self.fx.now]
        settings={'paper_multi_strategy_enabled':True,'paper_multi_strategy_ids':[S2],'gdcrs_stocks':[]}
        tracker=SimpleNamespace(multi_strategy_open_symbols=lambda *a:{'status':'available','symbols':['005930']},
            multi_strategy_position=lambda _m,_s,strategy,_n: ({'status':'owned','owned_qty':10,
                'half_take_done':False,'generation_id':'a'*32} if strategy==S2 else {'status':'unsupported'}))
        session=SimpleNamespace(paper_automation_reconcile=SimpleNamespace(_tracker=tracker))
        report={'mode':'paper','exchange':'KRX','snapshot_changed':False,'holdings':[{'stk_cd':'005930',
            'crd_tp':'00','crd_loan_dt':'','rmnd_qty':'10','trde_able_qty':'10','pur_amt':'1000'}]}
        class Exit:
            def fetch(self,symbol,as_of=None):return {'symbol':symbol,'current_price':100}
            def is_fresh(self,context,now):return True
        notices=[]
        monitor=MultiStrategyMonitor(settings_loader=lambda:settings,now=lambda:clock[0],
            provider_factory=lambda _:object(),exit_provider_factory=lambda _:Exit(),account_fetcher=lambda _:report,
            chart_fetcher=lambda *a,**k:[],executor=lambda **k:self.fail('no exit signal'))
        with patch('kiwoom_autotrader.trading.multi_strategy.session_identity',return_value=('paper','fake')):
            monitor.tick(session,notices.append)
            clock[0]+=timedelta(minutes=1)
            monitor.tick(session,notices.append)
        self.assertEqual(len(notices),1)
        self.assertEqual(monitor.status()['symbol_errors']['005930'],'sma20_unavailable')
    def test_rejected_price_exit_preserves_missing_cutoff_warning(self):
        from kiwoom_autotrader.telegram.commands.orders import OrderReplies
        settings={'paper_multi_strategy_enabled':True,'paper_multi_strategy_ids':[S2],'gdcrs_stocks':[]}
        tracker=SimpleNamespace(multi_strategy_open_symbols=lambda *a:{'status':'available','symbols':['005930']},
            multi_strategy_position=lambda _m,_s,strategy,_n: ({'status':'owned','owned_qty':10,
                'half_take_done':False,'generation_id':'a'*32} if strategy==S2 else {'status':'unsupported'}))
        session=SimpleNamespace(paper_automation_reconcile=SimpleNamespace(_tracker=tracker))
        report={'mode':'paper','exchange':'KRX','snapshot_changed':False,'holdings':[{'stk_cd':'005930',
            'crd_tp':'00','crd_loan_dt':'','rmnd_qty':'10','trde_able_qty':'10','pur_amt':'1000'}]}
        class Exit:
            def fetch(self,symbol,as_of=None):return {'symbol':symbol,'current_price':96}
            def is_fresh(self,context,now):return True
        actions=[]
        def rejected(**kwargs):
            actions.append(kwargs)
            return OrderReplies([],submitted=False,decision_reason='order_not_submitted')
        monitor=MultiStrategyMonitor(settings_loader=lambda:settings,now=lambda:self.fx.now,
            provider_factory=lambda _:object(),exit_provider_factory=lambda _:Exit(),
            account_fetcher=lambda _:report,executor=rejected)
        monitor._warning('005930','cutoff_bar_unavailable',lambda _:None)
        with patch('kiwoom_autotrader.trading.multi_strategy.session_identity',return_value=('paper','fake')):
            monitor.tick(session,lambda _:None)
        status=monitor.status()
        self.assertEqual(len(actions),1)
        self.assertEqual(actions[0]['reason'],'stop_loss_or_sma20')
        self.assertEqual(status['symbol_errors']['005930'],'cutoff_bar_unavailable')
        self.assertEqual(status['symbol_input_diagnostics']['005930']['code'],'cutoff_bar_unavailable')
    def test_arbitrary_exception_class_name_is_not_exposed(self):
        SecretError=type('CREDENTIAL_PRIVATE_CLASS',(Exception,),{})
        class Broken:
            def fetch(self,*a,**kw):raise SecretError('private')
        self.fx.monitor._provider_factory=lambda _:Broken()
        self.fx.tick();status=self.fx.monitor.status()
        self.assertEqual(status['symbol_errors']['005930'],'Error')
        self.assertNotIn('CREDENTIAL_PRIVATE_CLASS',repr(status))
    def test_minute_rollover_is_not_mislabeled_stale(self):
        chart=self.fx.monitor._chart
        def rollover(*args,**kw):
            rows=chart(*args,**kw)
            self.fx.clock[0]+=timedelta(minutes=1)
            return rows
        self.fx.monitor._chart=rollover
        self.fx.tick()
        self.assertEqual(self.fx.monitor.status()['symbol_input_diagnostics']['005930']['code'],'minute_rollover')
        self.assertEqual(self.fx.monitor.status()['symbol_input_diagnostics']['005930']['stage'],'entry_bars')
        self.assertEqual(self.fx.calls,[])

class ContextDiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.fx=fixture_context.ContextTests('test_basic_cache_precision_and_provenance')
        self.fx.setUp()
    def test_missing_invalid_conflict_codes(self):
        for field,value,expected in [('high_pric','','price_missing'),('upl_pric','abc','price_invalid'),
                                     ('upl_pric','999','price_conflict')]:
            original=self.fx.data[field];self.fx.data[field]=value
            with self.subTest(field=field,value=value),self.assertRaises(ContextUnavailable) as caught:
                self.fx.provider.fetch('005930')
            self.assertEqual(caught.exception.code,expected)
            self.fx.data[field]=original
    def test_initial_market_time_and_requested_age_reasons(self):
        moment=self.fx.clock.value
        for now,requested,reason in (
            (moment.replace(hour=8),moment.replace(hour=8),'market_closed'),
            (moment,moment+timedelta(seconds=1),'clock_reversed'),
            (moment,moment-timedelta(seconds=6),'request_stale')):
            self.fx.clock.value=now
            with self.subTest(reason=reason),self.assertRaises(ContextUnavailable) as caught:
                self.fx.provider.fetch('005930',as_of=requested)
            self.assertEqual(caught.exception.code,reason)
    def test_fresh_age_and_minute_rollover_codes(self):
        def slow(session,symbol):
            self.fx.clock.value+=timedelta(seconds=6);return dict(self.fx.data)
        self.fx.provider._fetcher=slow
        with self.assertRaises(ContextUnavailable) as caught:self.fx.provider.fetch('005930')
        self.assertEqual(caught.exception.code,'request_stale')
        self.fx.clock.value=self.fx.clock.value.replace(second=59)
        def rollover(session,symbol):
            self.fx.clock.value+=timedelta(seconds=1);return dict(self.fx.data)
        self.fx.provider._fetcher=rollover
        with self.assertRaises(ContextUnavailable) as caught:self.fx.provider.fetch('005930')
        self.assertEqual(caught.exception.code,'minute_rollover')

if __name__=='__main__':unittest.main()
