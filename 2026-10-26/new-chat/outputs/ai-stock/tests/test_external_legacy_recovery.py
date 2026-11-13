"""External audit regression fixtures: no broker access, only temporary ledgers."""
from datetime import timedelta
from decimal import Decimal
import hashlib,json,sqlite3
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock,patch
from kiwoom_autotrader.api.executions import Execution
from kiwoom_autotrader.api.orders import OrderReceipt
from kiwoom_autotrader.trading.execution_log import ConcurrentPositionLimitUnavailable
from kiwoom_autotrader.trading.multi_strategy import MultiStrategyMonitor
from tests import test_b3_ledger as b3_fixture

class VerifiedRepeatTests(TestCase):
    def setUp(self):
        self.f=b3_fixture.B3LedgerTests('test_fully_reconciled_00_and_rest_yield_exact_candidate');self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.t=self.f.tracker
        # Real producer source is explicit, injected fixture's default is unknown.
        with self.t._db() as db:
            current=dict(db.execute('SELECT * FROM fills').fetchone())
            self.t._observe_execution(db,current,'new_execution_key','broker_observation',(self.f.now-timedelta(seconds=2)).isoformat())
            balance={'schema':'paper-account-snapshot-v1','mode':'paper','exchange':'KRX','holdings':[{'stk_cd':'005930','rmnd_qty':'100'}]}
            db.execute('INSERT INTO account_balance_daily VALUES(?,?,?,?,?,?)',('default','paper','20261030',self.f.now.isoformat(),'broker_observation',json.dumps(balance)))
        self.f.now+=timedelta(seconds=1)
        self.t._ingest('paper','20261030',(Execution('20261030','24','1','005930','fixture','buy','09:20:30',Decimal('1000'),100),),source_kind='broker_observation')
    def capacity(self):
        with self.t._db() as db:return self.t._assert_position_capacity(db,'000660',self.f.now,8)
    def tamper(self,fn,*,rehash=True):
        with self.t._db() as db:
            row=db.execute('SELECT * FROM execution_observations ORDER BY id DESC LIMIT 1').fetchone();data=json.loads(row['payload_json']);fn(data)
            raw=json.dumps(data,sort_keys=True);digest=hashlib.sha256(raw.encode()).hexdigest() if rehash else row['digest']
            db.execute('DROP TRIGGER execution_observations_no_update')  # Corruption simulation, temp DB only.
            db.execute('UPDATE execution_observations SET payload_json=?,digest=? WHERE id=?',(raw,digest,row['id']))
    def test_identical_nonprojecting_repeat_is_not_an_unseen_fill(self):
        self.assertEqual(self.capacity()['occupied_symbols'],['005930'])
    def test_kind_alone_does_not_bypass_hash_or_projection(self):
        for fn,rehash in [(lambda d:d.update(projection_applied=True),True),(lambda d:d['current'].update(quantity=101),True),(lambda d:d.update(recorder_run_id='different'),False)]:
            with self.subTest(fn=fn):
                with self.t._db() as db:old=dict(db.execute('SELECT * FROM execution_observations ORDER BY id DESC LIMIT 1').fetchone())
                self.tamper(fn,rehash=rehash)
                with self.assertRaises(ConcurrentPositionLimitUnavailable) as exc:self.capacity()
                self.assertEqual(exc.exception.diagnostics['reason'],'post_balance_execution_change_or_unverified_repeat')
                with self.t._db() as db:
                    db.execute('DROP TRIGGER execution_observations_no_update')
                    db.execute('UPDATE execution_observations SET payload_json=?,digest=? WHERE id=?',(old['payload_json'],old['digest'],old['id']))
    def test_mismatched_key_previous_source_and_persisted_economics_rejected(self):
        for field,value in [('ord_no','99'),('price','999'),('quantity',True),('side','sell')]:
            with self.subTest(field=field):
                with self.t._db() as db:old=dict(db.execute('SELECT * FROM execution_observations ORDER BY id DESC LIMIT 1').fetchone())
                self.tamper(lambda d:d['previous'].update({field:value}))
                with self.assertRaises(ConcurrentPositionLimitUnavailable):self.capacity()
                with self.t._db() as db:
                    db.execute('DROP TRIGGER execution_observations_no_update')
                    db.execute('UPDATE execution_observations SET payload_json=?,digest=? WHERE id=?',(old['payload_json'],old['digest'],old['id']))
        with self.t._db() as db:db.execute('UPDATE fills SET quantity=101')
        with self.assertRaises(ConcurrentPositionLimitUnavailable):self.capacity()
    def test_new_correction_and_stream_gap_still_require_new_balance(self):
        self.f.event('stream_stopped',self.f.now,{})
        with self.assertRaises(ConcurrentPositionLimitUnavailable):self.capacity()
    def test_unknown_repeat_source_remains_unavailable(self):
        self.tamper(lambda d:d.update(source_kind='unknown'))
        with self.assertRaises(ConcurrentPositionLimitUnavailable):self.capacity()
    def test_true_future_balance_remains_blocked(self):
        self.f.now-=timedelta(seconds=2)
        with self.assertRaises(ConcurrentPositionLimitUnavailable) as exc:self.capacity()
        self.assertEqual(exc.exception.diagnostics['reason'],'balance_stale_future_or_invalid')

class LegacyResumeTests(TestCase):
    def setUp(self):
        from tests.test_b3_entry import NOW
        self.now=NOW;self.monitor=MultiStrategyMonitor(now=lambda:self.now)
        self.tracker=Mock();self.tracker.validate_paper_monitor_resume.return_value=True
        self.session=SimpleNamespace(execution_recorder=SimpleNamespace(status=lambda:{'ready':True,'error':None}))
    def test_halt_alert_is_once_reset_does_not_clear_and_request_required(self):
        notice=Mock();self.monitor._halt_uncertain('005930',notice);self.monitor._halt_uncertain('005930',notice)
        notice.assert_called_once();self.monitor.reset();self.assertTrue(self.monitor.status()['halted'])
        with patch('kiwoom_autotrader.trading.multi_strategy.session_identity',return_value=('paper','fixture')):
            self.assertFalse(self.monitor.resume_after_reconciliation(self.session,self.tracker,proof={}))
            self.monitor.request_resume();self.tracker.validate_paper_monitor_resume.return_value=False
            self.assertFalse(self.monitor.resume_after_reconciliation(self.session,self.tracker,proof={}))
            self.tracker.validate_paper_monitor_resume.return_value=True
            self.monitor._s5_attempted[('005930','2026-10-30T09:35')]=True
            self.assertTrue(self.monitor.resume_after_reconciliation(self.session,self.tracker,proof={'saved':True}))
            self.assertIn(('005930','2026-10-30T09:35'),self.monitor._s5_attempted)
    def test_recorder_unready_or_real_can_never_resume(self):
        self.monitor._halt_uncertain('005930',lambda _:None);self.monitor.request_resume()
        with patch('kiwoom_autotrader.trading.multi_strategy.session_identity',return_value=('real','fixture')):
            self.assertFalse(self.monitor.resume_after_reconciliation(self.session,self.tracker,proof={}))
        self.session.execution_recorder.status=lambda:{'ready':False,'error':None}
        with patch('kiwoom_autotrader.trading.multi_strategy.session_identity',return_value=('paper','fixture')):
            self.assertFalse(self.monitor.resume_after_reconciliation(self.session,self.tracker,proof={}))
    def test_ownership_warning_is_deduplicated(self):
        notify=Mock();self.monitor._warning('005930','ownership_unavailable',notify);self.monitor._warning('005930','ownership_unavailable',notify)
        notify.assert_called_once()

class ClosedB3LotTests(TestCase):
    def test_unit_reconciled_full_sale_has_closed_status_not_failed_addon(self):
        f=b3_fixture.B3LedgerTests('test_fully_reconciled_00_and_rest_yield_exact_candidate');f.setUp();self.addCleanup(f.doCleanups)
        ident=f.tracker.prepare_order('paper','sell','005930',100,'ddcrs',price=None)
        f.tracker.register_order(OrderReceipt('paper','sell','005930','100',None,'25','https://mockapi.kiwoom.com'),'ddcrs',intent_id=ident)
        f.event('execution_stream',f.now,{'9203':'25','9001':'A005930','907':'1','913':'체결','908':'092100','909':'2','914':'1100','915':'100','911':'100','903':'110000'})
        f.tracker._ingest('paper','20261030',(Execution('20261030','25','2','005930','fixture','sell','09:21:00',Decimal('1100'),100),))
        self.assertEqual(f.tracker.b3_add_candidate_status('paper','005930',f.now),{'status':'lot_closed','candidate':None})
        with f.tracker._db() as db:db.execute('UPDATE fills SET quantity=99 WHERE side=\'sell\'')
        self.assertEqual(f.tracker.b3_add_candidate_status('paper','005930',f.now)['status'],'pending')
