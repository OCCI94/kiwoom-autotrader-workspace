"""External-audit real/manual/ownership/session boundaries, entirely offline."""
from datetime import datetime, timedelta
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from kiwoom_autotrader.api.session import BrokerSession, IssuedToken, KST
from kiwoom_autotrader.telegram.commands import login, orders, rank_batch
from kiwoom_autotrader.trading import real_automation_safety as real
from kiwoom_autotrader.trading.execution_log import ExecutionTracker, ExecutionLogError
from kiwoom_autotrader.trading.cross_monitor import CrossMonitor, CrossMonitorError
from kiwoom_autotrader.trading.order_context import automated_orders
from kiwoom_autotrader.utils.command_scheduler import CommandScheduler
from kiwoom_autotrader.utils.kst_logging import KSTLineWriter, kst_listener_logs
from kiwoom_autotrader.telegram.listener import _LegacyRecoverySupervisor

class ExternalSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.now=datetime(2026,11,2,10,0,tzinfo=KST)
        self.session=BrokerSession(now=lambda:self.now);self.session.mode='real'
        self.session._issued=IssuedToken('offline-safety-token','20300101000000')
        self.stream={'ready':True,'error':None,'gap_count':0,'observations':0,'ingress_sequence':0,
                     'engine_ingress_sequence':0,'inflight_callbacks':0,'engine_pending_callbacks':0}
        self.session.execution_recorder=SimpleNamespace(status=lambda:dict(self.stream))
        self.tracker=ExecutionTracker(log_dir=self.root/'ledger',now=lambda:self.now)
        with self.tracker._db():pass

    def gate(self,**kwargs):
        report={'mode':'real','exchange':'KRX','snapshot_changed':False,'holdings':[],
                'cash':{'ord_alow_amt':'5000000'}}
        return real.RealAutomaticBuyGuard(self.session,self.tracker,'005930','10','10000','gdcrs',
            account=kwargs.get('account',lambda _:report),pending=kwargs.get('pending',lambda _:[]),
            now=lambda:self.now,clock=lambda:1.0)

    def insert(self,ident='first',symbol='005930',quantity=10,price=10000,state='accepted',side='buy',at=None):
        with self.tracker._db() as db:
            db.execute('INSERT INTO intents(scope,id,day,mode,symbol,side,quantity,strategy,created_at,order_number,state,reason) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                (self.tracker.scope,ident,(at or self.now).strftime('%Y%m%d'),'real',symbol,side,quantity,'gdcrs',(at or self.now).isoformat(),'123' if state=='accepted' else None,state,None))
            db.execute('INSERT INTO order_decisions(scope,intent_id,recorded_at,order_type,order_price,evidence_json,evidence_sha256) VALUES(?,?,?,?,?,?,?)',
                (self.tracker.scope,ident,self.now.isoformat(),'limit',str(price),None,None))

    def test_real_order_cap_before_any_broker_request(self):
        gate=self.gate();gate.quantity='101';gate.account=Mock()
        with self.assertRaises(real.RealAutomationSafetyError):gate.prepare()
        gate.account.assert_not_called()

    def test_real_account_daily_cap_and_per_symbol_count(self):
        self.insert(quantity=10,price=100000)
        self.insert('second','000660',quantity=10,price=100000)
        with real.read_ledger(self.tracker) as db:
            with self.assertRaises(real.RealAutomationSafetyError):
                real.assert_real_buy_ledger(db,self.tracker.scope,'035420',1,100,self.now)
        self.insert('third',quantity=1,price=100)
        with real.read_ledger(self.tracker) as db:
            with self.assertRaises(real.RealAutomationSafetyError):
                real.assert_real_buy_ledger(db,self.tracker.scope,'005930',1,100,self.now)

    def test_real_unknown_price_never_counted_as_zero(self):
        self.insert(price=0)
        with self.assertRaises(real.RealAutomationSafetyError):self.gate().prepare()

    def test_real_holdings_cash_pending_and_recorder_fail_closed(self):
        good={'mode':'real','exchange':'KRX','snapshot_changed':False,'holdings':[], 'cash':{'ord_alow_amt':'0'}}
        with self.assertRaises(real.RealAutomationSafetyError):self.gate(account=lambda _:good).prepare()
        good['cash']['ord_alow_amt']='5000000'
        good['holdings']=[{'stk_cd':'A005930','rmnd_qty':'1','pur_amt':'950000','crd_tp':'00'}]
        with self.assertRaises(real.RealAutomationSafetyError):self.gate(account=lambda _:good).prepare()
        with self.assertRaises(real.RealAutomationSafetyError):self.gate(pending=lambda _:[{'stk_cd':'000660','oso_qty':'1'}]).prepare()
        self.stream['ready']=False
        with self.assertRaises(real.RealAutomationSafetyError):self.gate().prepare()

    def test_real_send_accepts_only_exact_own_new_intent_and_rejects_change(self):
        gate=self.gate().prepare();self.insert(state='prepared')
        gate()
        self.stream['observations']=1
        with self.assertRaises(real.RealAutomationSafetyError):gate()

    def test_real_send_rejects_second_concurrent_intent(self):
        gate=self.gate().prepare();self.insert(state='prepared');self.insert('other','000660',state='prepared')
        with self.assertRaises(real.RealAutomationSafetyError):gate()

    def test_real_cooldown_sell_acceptance_and_hhmmss_fill(self):
        self.insert(side='sell',at=self.now-timedelta(minutes=10))
        with self.assertRaises(real.RealAutomationSafetyError):self.gate().prepare()

    def test_real_cooldown_parses_actual_hhmmss_fill_schema(self):
        with self.tracker._db() as db:
            db.execute("INSERT INTO fills(scope,day,mode,ord_no,fill_no,symbol,side,moment,price,quantity,strategy) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (self.tracker.scope,'20261102','real','order','unit','005930','sell','09:50:00','10000',1,'gdcrs'))
            with self.assertRaises(real.RealAutomationSafetyError):
                real.assert_real_buy_ledger(db,self.tracker.scope,'005930',1,10000,self.now)
            passed=real.assert_real_buy_ledger(db,self.tracker.scope,'005930',1,10000,self.now+timedelta(minutes=10))
            self.assertEqual(passed['prior_symbol_entries'],0)

    def test_real_prepare_blocks_other_symbol_prepared_and_prior_day_unknown(self):
        for day_offset,state in ((0,'prepared'),(-1,'unknown'),(-1,'prepared')):
            with self.subTest(day_offset=day_offset,state=state):
                other=ExecutionTracker(log_dir=self.root/f'ledger-{day_offset}-{state}',now=lambda:self.now)
                prior=self.tracker;self.tracker=other
                self.insert('other','000660',state=state,at=self.now+timedelta(days=day_offset))
                gate=self.gate();gate.account=Mock()
                with self.assertRaises(ExecutionLogError):gate.prepare()
                gate.account.assert_not_called();self.tracker=prior

    def test_real_final_send_ignores_only_exact_own_intent_not_prior_day_reservation(self):
        gate=self.gate().prepare()
        self.insert('own',state='prepared')
        gate()  # Own prepared reservation is the sole permitted ignore.
        self.insert('old-other','000660',state='prepared',at=self.now-timedelta(days=1))
        with self.assertRaises(ExecutionLogError):gate()

    def test_real_atomic_reservation_blocks_prior_day_other_symbol(self):
        self.insert('old-other','000660',state='prepared',at=self.now-timedelta(days=1))
        with self.assertRaises(ExecutionLogError):
            self.tracker.prepare_order('real','buy','005930',1,'gdcrs',price=10000)
        with self.tracker._db() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM intents').fetchone()[0],1)

    def test_real_atomic_concurrent_different_symbols_reserve_only_one(self):
        from concurrent.futures import ThreadPoolExecutor
        import threading
        barrier=threading.Barrier(2)
        def reserve(symbol):
            tracker=ExecutionTracker(log_dir=self.root/'ledger',now=lambda:self.now)
            barrier.wait(timeout=5)
            try:
                return tracker.prepare_order('real','buy',symbol,1,'gdcrs',price=10000)
            except ExecutionLogError:
                return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            result=list(pool.map(reserve,['005930','000660']))
        self.assertEqual(sum(x is not None for x in result),1)
        with self.tracker._db() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM intents').fetchone()[0],1)

    def test_real_budget_cannot_inherit_aggressive_values(self):
        self.assertEqual(real.real_buy_budget(999999999999),1000000)

    def test_login_real_denies_enabled_monitors_and_order_reservation(self):
        session=Mock();reporter=Mock()
        for cfg in ({'gdcrs_enabled':True},{'ddcrs_enabled':True},{'reservations':[{'command':'rank 4 cmd 2 buy () max 10000'}]}):
            with self.subTest(cfg=cfg),patch.object(login,'load_settings',return_value=cfg):
                self.assertIn('거부',login.handle_login('login_real','login real',session,reporter=reporter))
        session.login.assert_not_called()
        with patch.object(login,'load_settings',return_value={'reservations':[{'command':'report'}]}):
            login.handle_login('login_real','login real',session,reporter=reporter)
        session.login.assert_called_once_with('real')

    def test_ddcrs_no_context_and_real_ownership_are_unverified(self):
        with self.assertRaises(CrossMonitorError):CrossMonitor._owned_quantity(self.session,'005930',self.now)
        with automated_orders(self.tracker,'ddcrs'):
            with self.assertRaises(CrossMonitorError):CrossMonitor._owned_quantity(self.session,'005930',self.now)

    def test_ddcrs_caps_broker_quantity_to_proven_automatic_owner(self):
        monitor=CrossMonitor(state_path=self.root/'markers',ownership_reader=lambda *_:3)
        report={'mode':'paper','exchange':'KRX','snapshot_changed':False,'holdings':[
            {'stk_cd':'A005930','rmnd_qty':'20','trde_able_qty':'20','crd_tp':'00','crd_loan_dt':None}]}
        self.assertEqual(monitor._owned_holdings(self.session,report,'paper',self.now,Mock()),{'005930':3})
        monitor.ownership_reader=Mock(side_effect=ValueError('unknown'))
        notify=Mock()
        self.assertEqual(monitor._owned_holdings(self.session,report,'paper',self.now,notify),{})
        self.assertEqual(monitor.status()['ownership_diagnostics']['005930']['reason'],'automatic_ownership_unverified')
        monitor._owned_holdings(self.session,report,'paper',self.now,notify);notify.assert_called_once()

    def test_reserved_buy_requires_official_current_session_target(self):
        clock=[self.now.replace(hour=8,minute=50)]
        session=[None]
        scheduler=CommandScheduler(self.root/'settings.json',now=lambda:clock[0],session_reader=lambda _:session[0])
        scheduler.add('09:00','buy 005930 1 1000');clock[0]=self.now.replace(hour=9,minute=0)
        self.assertIsNone(scheduler.claim_due())
        session[0]={'session_day':'20261102','start':self.now.replace(hour=10,minute=0),'end':self.now.replace(hour=16,minute=30)}
        clock[0]=self.now.replace(hour=10,minute=0);self.assertIsNone(scheduler.claim_due())
        session[0]['start']=self.now.replace(hour=9,minute=0)
        self.assertIsNotNone(scheduler.claim_due())
        self.assertIsNone(scheduler.claim_due())

    def test_manual_limit_and_budget_over_cap_are_not_sent(self):
        with patch.object(orders,'submit_order') as send,patch.object(orders,'fetch_stock_info') as quote:
            self.assertFalse(orders.handle_buy('buy 005930 101 10000',self.session).submitted)
            self.assertFalse(orders.handle_buy('buy 005930 max 1000001',self.session).submitted)
            send.assert_not_called();quote.assert_not_called()

    def test_manual_market_upper_limit_bounds_order_or_rejects(self):
        with patch.object(orders,'submit_order') as send,patch.object(orders,'fetch_stock_info',return_value={'stk_cd':'005930','upl_pric':'100000'}) as quote:
            result=orders.handle_buy('buy 005930 11',self.session)
            self.assertFalse(result.submitted);send.assert_not_called()
        with patch.object(orders,'submit_order') as send,patch.object(orders,'fetch_stock_info',return_value={'stk_cd':'005930','cur_prc':'10000'}):
            self.assertFalse(orders.handle_buy('buy 005930 1',self.session).submitted);send.assert_not_called()

    def test_rank_buy_aggregate_cap_and_unbounded_market_reject_before_query(self):
        with patch.object(rank_batch,'fetch_ranked_stocks') as query:
            for command in ('rank 4 cmd 20 buy () max 100000','rank 4 cmd 2 buy () 1'):
                reply=rank_batch.handle_rank_batch(command,self.session,Mock())
                self.assertTrue(reply)
            query.assert_not_called()

    def test_kst_prefix_handles_partial_and_multiline_without_other_output(self):
        raw=io.StringIO();writer=KSTLineWriter(raw,now=lambda:self.now)
        writer.write('first');writer.write(' line\nsecond\n');writer.flush()
        self.assertEqual(raw.getvalue(),'[2026-11-02 10:00:00 KST] first line\n[2026-11-02 10:00:00 KST] second\n')

    def test_legacy_status_omits_unavailable_ttl_status_before_maintenance_tick(self):
        supervisor=_LegacyRecoverySupervisor(Mock(),ttl=Mock())
        self.assertEqual(json.loads(json.dumps(supervisor.status())),
            {'recovery_inflight':False,'recovery':None,'ttl':None})

    def test_multi_resume_is_explicit_paper_only_and_requests_no_order(self):
        from kiwoom_autotrader.telegram.listener import _dispatch
        monitor=Mock();monitor.request_resume.return_value=True
        self.session.mode='real'
        _dispatch('multi_resume','multi resume',None,self.session,None,multi_monitor=monitor)
        monitor.request_resume.assert_not_called()
        self.session.mode='paper'
        reply=_dispatch('multi_resume','multi resume',None,self.session,None,multi_monitor=monitor)
        monitor.request_resume.assert_called_once()
        self.assertIn('대사',reply[0])
        monitor.resume_after_reconciliation.assert_not_called()

    def test_legacy_ttl_runs_without_gdcrs_and_resume_requires_explicit_request(self):
        self.session.mode='paper';monitor=Mock();monitor.status.return_value={'halted':True,'resume_requested':False}
        ttl=Mock();ttl.last_status={};recover=Mock(return_value={'reconciled':True,'proof':{'x':1}})
        cfg={'gdcrs_enabled':False,'gdcrs_unfilled_buy_ttl_seconds':180}
        supervisor=_LegacyRecoverySupervisor(monitor,ttl=ttl,recovery=recover,settings=lambda:cfg,
            now=lambda:self.now,clock=lambda:1,launcher=lambda fn:fn())
        supervisor.tick(self.session,self.tracker,Mock(),lambda:False)
        ttl.tick.assert_called_once();recover.assert_called_once()
        monitor.resume_after_reconciliation.assert_not_called()
        monitor.status.return_value={'halted':True,'resume_requested':True}
        supervisor.tick(self.session,self.tracker,Mock(),lambda:False)
        monitor.resume_after_reconciliation.assert_called_once_with(self.session,self.tracker,proof={'x':1})

    def test_legacy_supervisor_does_not_run_for_experiment_or_real(self):
        monitor=Mock();ttl=Mock();recover=Mock()
        supervisor=_LegacyRecoverySupervisor(monitor,ttl=ttl,recovery=recover,settings=lambda:{'paper_experiment_enabled':True})
        supervisor.tick(self.session,self.tracker,Mock(),lambda:False)
        self.session.mode='paper';supervisor.tick(self.session,self.tracker,Mock(),lambda:False)
        ttl.tick.assert_not_called();recover.assert_not_called()
