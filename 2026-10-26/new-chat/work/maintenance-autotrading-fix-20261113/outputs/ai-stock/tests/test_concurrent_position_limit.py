"""Atomic all-strategy paper capacity; no broker or operational DB access."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timedelta
import json
from pathlib import Path
import tempfile
from unittest import TestCase
from zoneinfo import ZoneInfo

from kiwoom_autotrader.trading.execution_log import (ExecutionTracker,
    ConcurrentPositionLimitReached,ConcurrentPositionLimitUnavailable, PaperCashLimitReached, ExecutionLogError)


class ConcurrentPositionTests(TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.now=datetime(2026,10,30,11,0,tzinfo=ZoneInfo('Asia/Seoul'))
        self.options={'paper_dynamic_scan_enabled':True,'paper_max_concurrent_positions':2}
        self.tracker=self.make_tracker()
        with self.tracker._db():pass
        self.balance(['005930'])

    def make_tracker(self,scope='default'):
        return ExecutionTracker(log_dir=Path(self.tmp.name),now=lambda:self.now,
                                settings_loader=lambda:self.options,account_scope=scope)

    def balance(self, symbols, *, age=0, source='broker_observation', deposit='10000000', orderable='10000000'):
        data={'schema':'paper-account-snapshot-v1','mode':'paper','exchange':'KRX',
              'cash':{'entr':deposit,'ord_alow_amt':orderable},
              'holdings':[{'stk_cd':'A'+s,'rmnd_qty':'1'} for s in symbols]}
        with self.tracker._db() as db:
            db.execute('INSERT OR REPLACE INTO account_balance_daily VALUES(?,?,?,?,?,?)',
                (self.tracker.scope,'paper','20261030',(self.now-timedelta(seconds=age)).isoformat(),source,json.dumps(data)))

    def prepare(self,tracker,symbol):
        return tracker.prepare_order('paper','buy',symbol,1,'gdcrs',price=1000)

    def test_account_cap_counts_broker_holdings_and_reserved_symbols_once(self):
        self.prepare(self.tracker,'000660')
        with self.assertRaises(ConcurrentPositionLimitReached):self.prepare(self.tracker,'035420')
        # Existing-symbol add does not consume a third position slot.
        self.assertEqual(len(self.tracker.paper_position_capacity('005930')['occupied_symbols']),2)
        # It nevertheless cannot reuse cash while another intent is unresolved.
        with self.assertRaises(ExecutionLogError):self.prepare(self.tracker,'005930')

    def test_two_trackers_cannot_reserve_the_final_slot(self):
        second=self.make_tracker()
        def reserve(pair):
            tracker,symbol=pair
            try:return self.prepare(tracker,symbol)
            except ConcurrentPositionLimitReached:return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            result=list(pool.map(reserve,[(self.tracker,'000660'),(second,'035420')]))
        self.assertEqual(sum(x is not None for x in result),1)

    def test_cancelled_unsent_intent_releases_slot(self):
        intent=self.prepare(self.tracker,'000660');self.tracker.cancel_intent(intent)
        self.assertTrue(self.prepare(self.tracker,'035420'))

    def test_explicit_empty_snapshot_is_distinct_from_stale_or_unknown(self):
        self.balance([])
        self.assertEqual(self.tracker.paper_position_capacity('005930')['occupied_symbols'],[])
        for age,source in [(91,'broker_observation'),(-1,'broker_observation'),(0,'unknown')]:
            self.balance([],age=age,source=source)
            with self.subTest(age=age,source=source),self.assertRaises(ConcurrentPositionLimitUnavailable):
                self.prepare(self.tracker,'005930')

    def test_no_balance_and_other_scope_cannot_authorize(self):
        other=self.make_tracker('other')
        with self.assertRaises(ConcurrentPositionLimitUnavailable):self.prepare(other,'000660')

    def test_new_fill_after_account_snapshot_requires_a_new_balance(self):
        with self.tracker._db() as db:
            db.execute('INSERT INTO execution_observations(scope,day,mode,ord_no,fill_no,observed_at,kind,source_kind,digest,payload_json) VALUES(?,?,?,?,?,?,?,?,?,?)',
                ('default','20261030','paper','1','1',(self.now+timedelta(seconds=1)).isoformat(),'unit','broker_observation','a'*64,'{}'))
        self.now+=timedelta(seconds=2)
        with self.assertRaises(ConcurrentPositionLimitUnavailable):self.prepare(self.tracker,'000660')

    def test_disabled_and_real_modes_do_not_activate_new_guard(self):
        self.options['paper_dynamic_scan_enabled']=False
        self.assertTrue(self.prepare(self.tracker,'000660'))
        self.options['paper_dynamic_scan_enabled']=True
        self.assertTrue(self.tracker.prepare_order('real','buy','035420',1,'gdcrs',price=1000))

    def test_deposit_lower_than_orderable_and_fee_reserve_are_enforced(self):
        for deposit,orderable in [('1000','999999'),('999999','1000'),('0','999999')]:
            self.balance([],deposit=deposit,orderable=orderable)
            with self.subTest(deposit=deposit,orderable=orderable),self.assertRaises(PaperCashLimitReached):
                self.prepare(self.tracker,'005930')
        self.balance([],deposit='1005',orderable='999999')
        self.assertTrue(self.prepare(self.tracker,'005930'))

    def test_missing_deposit_does_not_fall_back_to_orderable(self):
        self.balance([],deposit=None)
        with self.assertRaises(ConcurrentPositionLimitUnavailable):self.prepare(self.tracker,'005930')
