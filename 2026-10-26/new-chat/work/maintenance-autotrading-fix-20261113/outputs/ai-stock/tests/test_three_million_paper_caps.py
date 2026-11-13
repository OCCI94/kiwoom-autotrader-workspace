"""The two paper caps and their pre-send evidence are independent and fail closed."""
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

from kiwoom_autotrader.api.session import BrokerSession, IssuedToken, KST
from kiwoom_autotrader.config.settings import load_settings, save_settings, SettingsError
from kiwoom_autotrader.trading.execution_log import ExecutionTracker, ExecutionLogError
from kiwoom_autotrader.trading.paper_reconciliation import PaperReconciliationGate, PaperReconciliationError, _pending_rows
from kiwoom_autotrader.trading.paper_policy import capture_automatic_paper_guard, limit_automatic_buy_budget, PaperAutomationPolicyError
from kiwoom_autotrader.trading.order_context import automated_orders
from kiwoom_autotrader.trading.golden_cross import GoldenCrossMonitor, GoldenCrossError

class ThreeMillionCaps(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.now=datetime(2026,10,29,10,0,tzinfo=KST)
        self.session=BrokerSession(now=lambda:self.now);self.session.mode='paper'
        self.session._issued=IssuedToken('offline-fixture','20300101000000')
        self.session.execution_recorder=Mock(status=Mock(return_value={'ready':True,'error':None,'gap_count':0,'observations':0,'ingress_sequence':0,'inflight_callbacks':0,'engine_ingress_sequence':0,'engine_pending_callbacks':0}))
        self.tracker=ExecutionTracker(log_dir=Path(self.tmp.name),now=lambda:self.now,settings_loader=lambda:{'market_end_time':'15:30'})
        self.account={'mode':'paper','exchange':'KRX','snapshot_changed':False,'cash':{'ord_alow_amt':'5000000'},'holdings':[{'stk_cd':'A005930','crd_tp':'00','rmnd_qty':'10','trde_able_qty':'10','pur_amt':'1000000'}]}
        self.pending=[]
        self.caps={'paper_automation_symbol_cap':3000000,'paper_automation_buy_cap':3000000}
        self.patch=patch('kiwoom_autotrader.trading.paper_reconciliation.load_settings',side_effect=lambda:dict(self.caps));self.patch.start();self.addCleanup(self.patch.stop)
        self.gate=PaperReconciliationGate(self.session,self.tracker,executions_fetcher=lambda *a,**k:(),account_fetcher=lambda s:self.account,pending_fetcher=lambda s:self.pending,now=lambda:self.now,monotonic=lambda:100.)
    def test_config_accepts_300_and_rejects_bool_or_over_cap(self):
        path=Path(self.tmp.name)/'settings.json'
        self.assertEqual(load_settings(path)['paper_automation_symbol_cap'],1000000)
        saved=save_settings({'paper_automation_buy_cap':3000000,'paper_automation_symbol_cap':3000000},path)
        self.assertEqual(saved['paper_automation_buy_cap'],3000000)
        for key in ('paper_automation_buy_cap','paper_automation_symbol_cap'):
            for value in (True,0,3000001):
                with self.subTest(key=key,value=value),self.assertRaises(SettingsError):save_settings({key:value},path)
    def test_existing_position_plus_new_principal_never_exceeds_cap(self):
        self.assertEqual(self.gate.available_buy_principal('005930'),2000000)
        guard=self.gate('005930',side='buy',quantity=20,price=100000);guard()
        e=guard.reconciliation_evidence
        self.assertEqual((e['schema_version'],e['pre_order_pur_amt_krw'],e['remaining_principal_krw'],e['symbol_cap_krw'],e['per_buy_cap_krw']),(2,1000000,2000000,3000000,3000000))
        self.tracker.prepare_order('paper','buy','005930','20','gdcrs',price='100000',reconciliation_evidence=e)
        with self.assertRaises(PaperReconciliationError):self.gate('005930',side='buy',quantity=21,price=100000)
        bad=dict(e,symbol_cap_krw=3000001)
        with self.assertRaises(ExecutionLogError):self.tracker.prepare_order('paper','buy','005930','20','gdcrs',price='100000',reconciliation_evidence=bad)
    def test_pending_buy_or_sell_blocks_topup(self):
        for code,exchange in (('005930','1'),('A005930','2'),('000660','0')):
            self.pending=[{'stk_cd':code,'ord_no':'12','oso_qty':'1','stex_tp':exchange}]
            with self.assertRaisesRegex(PaperReconciliationError,'미체결'):self.gate('005930',side='buy',quantity=1,price=100000)
    def test_integrated_pending_request_is_explicit_and_rejects_later_page_errors(self):
        with patch('kiwoom_autotrader.trading.paper_reconciliation._request_rows',return_value=[]) as req:
            self.assertEqual(_pending_rows(self.session,integrated=True),[])
            self.assertEqual(req.call_args.args[2]['stex_tp'],'0')
        with patch('kiwoom_autotrader.trading.paper_reconciliation._request_rows',side_effect=ValueError('incomplete')):
            with self.assertRaises(ValueError):_pending_rows(self.session,integrated=True)
    def test_elevated_cash_must_cover_principal_plus_fee_reserve(self):
        self.account['holdings']=[]
        for cash in (None, {}, {'ord_alow_amt':None}, {'ord_alow_amt':'2999999'}, {'ord_alow_amt':'3014999'}, {'ord_alow_amt':'-1'}):
            with self.subTest(cash=cash):
                self.account['cash']=cash
                with self.assertRaises(PaperReconciliationError):self.gate('005930',side='buy',quantity=30,price=100000)
        self.account['cash']={'ord_alow_amt':'3015000'}
        guard=self.gate('005930',side='buy',quantity=30,price=100000)
        self.assertEqual(guard.reconciliation_evidence['cost_reserve_krw'],15000)
        self.assertEqual(guard.reconciliation_evidence['available_orderable_krw'],3015000)
        for changed in ({'available_orderable_krw': 3014999}, {'cost_reserve_krw': 14999}):
            with self.assertRaises(ExecutionLogError):
                self.tracker.prepare_order('paper','buy','005930','30','gdcrs',price='100000',
                    reconciliation_evidence=dict(guard.reconciliation_evidence, **changed))
    def test_actual_insufficient_cash_reason_reaches_handler_without_preparing_an_order(self):
        from kiwoom_autotrader.telegram.commands import orders as commands
        self.account['holdings']=[]
        self.account['cash']={'ord_alow_amt':'1000'}
        self.session.paper_automation_reconcile=self.gate
        with patch('kiwoom_autotrader.trading.paper_policy.load_settings',side_effect=lambda:dict(self.caps)), \
             patch.object(commands,'fetch_stock_info',return_value={'stk_cd':'005930','cur_prc':'100000'}), \
             patch('kiwoom_autotrader.api.orders.queued_open') as queue:
            with automated_orders(self.tracker,'gdcrs'):
                replies=commands.handle_buy('buy 005930 max 3000000',self.session)
            self.assertEqual(replies.decision_reason,'insufficient_cash')
            self.assertIsNone(replies.skipped_reason)  # Ordinary error feedback is not a quiet skip.
            self.assertIs(replies.submitted,False)
            queue.assert_not_called()
        with self.tracker._db() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM intents').fetchone()[0],0)
    def test_missing_cash_is_unknown_rather_than_diagnosed_as_insufficient(self):
        from kiwoom_autotrader.telegram.commands import orders as commands
        self.account['holdings']=[]
        self.account['cash']={}
        self.session.paper_automation_reconcile=self.gate
        with patch('kiwoom_autotrader.trading.paper_policy.load_settings',side_effect=lambda:dict(self.caps)), \
             patch.object(commands,'fetch_stock_info',return_value={'stk_cd':'005930','cur_prc':'100000'}), \
             patch('kiwoom_autotrader.api.orders.queued_open') as queue:
            with automated_orders(self.tracker,'gdcrs'):
                replies=commands.handle_buy('buy 005930 max 3000000',self.session)
            self.assertNotEqual(replies.decision_reason,'insufficient_cash')
            self.assertIs(replies.submitted,False)
            queue.assert_not_called()
    def test_two_symbols_cannot_reserve_same_cash_concurrently(self):
        self.account['holdings']=[]
        evidence={code:self.gate(code,side='buy',quantity=30,price=100000).reconciliation_evidence
                  for code in ('005930','000660')}
        barrier=threading.Barrier(2)
        def reserve(code):
            tracker=ExecutionTracker(log_dir=Path(self.tmp.name),now=lambda:self.now,
                                     settings_loader=lambda:{'market_end_time':'15:30'})
            barrier.wait(timeout=5)
            try:
                tracker.prepare_order('paper','buy',code,'30','gdcrs',price='100000',
                                      require_no_pending=True,reconciliation_evidence=evidence[code])
                return 'reserved'
            except ExecutionLogError:
                return 'blocked'
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(reserve,('005930','000660')))
        self.assertCountEqual(results,['reserved','blocked'])
    def test_per_order_max_and_policy_change_during_queue(self):
        self.account['holdings']=[]
        self.gate('005930',side='buy',quantity=30,price=100000)()
        with self.assertRaises(PaperReconciliationError):self.gate('005930',side='buy',quantity=31,price=100000)
        guard=self.gate('005930',side='buy',quantity=2,price=100000)
        self.caps['paper_automation_symbol_cap']=1000000
        with self.assertRaises(PaperReconciliationError):guard()
    def test_complete_policy_gate_binds_both_caps_and_detects_stale_settings(self):
        self.session.paper_automation_reconcile=self.gate
        with patch('kiwoom_autotrader.trading.paper_policy.load_settings',side_effect=lambda:dict(self.caps)):
            with automated_orders(self.tracker,'gdcrs'):
                self.assertEqual(limit_automatic_buy_budget(self.session,'005930','3000000'),2000000)
                guard=capture_automatic_paper_guard(self.session,'buy','005930','20','100000')
                guard.before_prepare()
                self.assertEqual(guard.reconciliation_evidence['symbol_cap_krw'],3000000)
                self.assertEqual(guard.reconciliation_evidence['per_buy_cap_krw'],3000000)
                guard()
                self.caps['paper_automation_buy_cap']=1000000
                with self.assertRaises(PaperAutomationPolicyError):guard()
    def test_no_position_allows_exact_three_million_and_not_more(self):
        self.account['holdings']=[]
        self.assertEqual(self.gate.available_buy_principal('005930'),3000000)
        guard=self.gate('005930',side='buy',quantity=30,price=100000)
        self.tracker.prepare_order('paper','buy','005930','30','gdcrs',price='100000',reconciliation_evidence=guard.reconciliation_evidence)
        with self.assertRaises(PaperReconciliationError):self.gate('005930',side='buy',quantity=31,price=100000)
    def test_paper_budget_never_changes_real_budget_and_is_guarded(self):
        s={'gdcrs_enabled':True,'gdcrs_short_period':5,'gdcrs_long_period':20,
           'gdcrs_stocks':[{'code':'005930','budget':1000000,'paper_budget':3000000}]}
        monitor=GoldenCrossMonitor(settings_loader=lambda:s,now=lambda:self.now)
        paper_context=monitor._context(self.session,self.now)
        self.assertEqual(paper_context.stocks,(('005930',3000000),))
        s['gdcrs_stocks'][0]['paper_budget']=2000000
        self.assertNotEqual(paper_context,monitor._context(self.session,self.now))
        self.session.mode='real'
        real_context=monitor._context(self.session,self.now)
        self.assertEqual(real_context.stocks,(('005930',1000000),))
        s['gdcrs_stocks'][0]['paper_budget']=3000000
        self.assertEqual(real_context,monitor._context(self.session,self.now))
        s['gdcrs_stocks'][0]['paper_budget']=True
        with self.assertRaises(GoldenCrossError):monitor._context(self.session,self.now)
