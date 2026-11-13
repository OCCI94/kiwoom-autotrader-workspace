"""Atomic paper overnight and VSS capacity, with private offline ledgers."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from zoneinfo import ZoneInfo
import json
from kiwoom_autotrader.trading.execution_log import ExecutionTracker,ConcurrentPositionLimitReached

class OvernightCapacityTests(TestCase):
    def setUp(self):
        self.tmp=TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.now=datetime(2026,11,2,15,20,tzinfo=ZoneInfo('Asia/Seoul'))
        self.settings={'paper_experiment_enabled':True}
        self.tracker=self.make_tracker();self.balance([])
    def make_tracker(self):
        return ExecutionTracker(log_dir=Path(self.tmp.name),now=lambda:self.now,settings_loader=lambda:self.settings)
    def balance(self,holdings):
        data={'schema':'paper-account-snapshot-v1','mode':'paper','exchange':'KRX','holdings':holdings,
              'cash':{'entr':'10000000','ord_alow_amt':'10000000'}}
        with self.tracker._db() as db:
            db.execute('INSERT OR REPLACE INTO account_balance_daily VALUES(?,?,?,?,?,?)',
              ('default','paper','20261102',self.now.isoformat(),'broker_observation',json.dumps(data)))
    def reserve(self,tracker,symbol,arm):
        with tracker._db() as db:
            if not db.in_transaction:db.execute('BEGIN IMMEDIATE')
            tracker._assert_overnight_capacity(db,symbol,arm,self.now)
            db.execute('INSERT INTO intents VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
              ('default',symbol+'-'+arm,'20261102','paper','buy',symbol,1,arm,self.now.isoformat(),None,'prepared',None))
    def test_twenty_overnight_reservations_block_a_new_symbol(self):
        for n in range(20):self.reserve(self.tracker,f'{n:06}',('EXP_D','EXP_GDR','EXP_LDR','EXP_VSS')[n%4])
        with self.assertRaises(ConcurrentPositionLimitReached):self.reserve(self.tracker,'999999','EXP_GDR')
    def test_eight_vss_reservations_block_vss_but_not_gdr(self):
        for n in range(8):self.reserve(self.tracker,f'{n:06}','EXP_VSS')
        with self.assertRaises(ConcurrentPositionLimitReached):self.reserve(self.tracker,'999999','EXP_VSS')
        self.reserve(self.tracker,'999998','EXP_GDR')
    def test_unattributed_held_symbol_consumes_conservative_overnight_and_vss_slot(self):
        self.balance([{'stk_cd':'A000999','rmnd_qty':'1'}])
        for n in range(7):self.reserve(self.tracker,f'{n:06}','EXP_VSS')
        with self.assertRaises(ConcurrentPositionLimitReached):self.reserve(self.tracker,'999999','EXP_VSS')
    def test_two_trackers_cannot_claim_twentieth_slot_concurrently(self):
        for n in range(19):self.reserve(self.tracker,f'{n:06}','EXP_D')
        other=self.make_tracker()
        def run(pair):
            try:self.reserve(*pair);return True
            except ConcurrentPositionLimitReached:return False
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes=list(pool.map(run,[(self.tracker,'999998','EXP_D'),(other,'999999','EXP_LDR')]))
        self.assertEqual(sum(outcomes),1)
    def test_accepted_unfilled_reservation_is_counted(self):
        with self.tracker._db() as db:
            for n in range(20):
                code=f'{n:06}';ord_no=str(n+1)
                db.execute('INSERT INTO intents VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                  ('default','i'+code,'20261102','paper','buy',code,1,'EXP_D',self.now.isoformat(),ord_no,'accepted',None))
                db.execute('INSERT INTO orders VALUES(?,?,?,?,?,?,?,?,?)',
                  ('default','20261102','paper',ord_no,code,'buy','EXP_D',1,self.now.isoformat()))
        with self.assertRaises(ConcurrentPositionLimitReached):self.reserve(self.tracker,'999999','EXP_VSS')

    def test_broker_holdings_and_accepted_pending_count_once_per_symbol(self):
        positions=[{'stk_cd':'A'+f'{n:06}','rmnd_qty':'1'} for n in range(8)]
        self.balance(positions)
        with self.tracker._db() as db:
            for n in range(8):
                code=f'{n:06}';order=str(n+1)
                db.execute('INSERT INTO orders VALUES(?,?,?,?,?,?,?,?,?)',
                    ('default','20261102','paper',order,code,'buy','EXP_VSS',1,self.now.isoformat()))
                db.execute('INSERT INTO fills VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    ('default','20261102','paper',order,'1',code,'fixture','buy','15:20:00','1000',1,'EXP_VSS',None,None,None))
        with self.assertRaises(ConcurrentPositionLimitReached):self.reserve(self.tracker,'999999','EXP_VSS')
        self.reserve(self.tracker,'999998','EXP_D')

    def test_real_or_disabled_profile_never_activates_experiment(self):
        self.assertFalse(self.tracker.settings_loader().get('real_mode_enabled',False))
        from kiwoom_autotrader.config.settings import effective_paper_experiment
        class NoRead(dict):
            def get(self,*args):raise AssertionError('real mode read paper options')
        self.assertEqual(effective_paper_experiment(NoRead(),'real'),{'enabled':False})
        self.settings['paper_experiment_enabled']=False
        with self.tracker._db() as db:
            from kiwoom_autotrader.trading.execution_log import ExecutionLogError
            with self.assertRaises(ExecutionLogError):self.tracker._assert_overnight_capacity(db,'999999','EXP_D',self.now)

    def test_missing_theme_data_is_explicit_unknown_in_experiment_only(self):
        from kiwoom_autotrader.trading.theme_concentration import theme_occupancy,UNKNOWN_POOL
        missing=Path(self.tmp.name)/'missing-taxonomy.json'
        proof=theme_occupancy('005930',[],self.now,path=missing,unclassified_policy='allow_unknown')
        self.assertEqual(proof['theme_families'],[UNKNOWN_POOL])
        self.assertEqual(proof['metadata_status'],'unavailable_treated_as_unknown')
        with self.assertRaises(ValueError):theme_occupancy('005930',[],self.now,path=missing,unclassified_policy='block')

    def test_experiment_etf_whitelist_and_unproven_new_arm_fail_closed(self):
        from kiwoom_autotrader.trading.execution_log import ExecutionLogError
        for strategy, symbol in [('EXP_ETF_B', '005930'), ('EXP_A', '069500'), ('EXP_B', '229200')]:
            with self.subTest(strategy=strategy, symbol=symbol), self.assertRaisesRegex(ExecutionLogError, 'ETF 실험'):
                self.tracker.prepare_order('paper', 'buy', symbol, 1, strategy)
        for strategy in ('EXP_ORB_A', 'EXP_ORB_B', 'EXP_GDR', 'EXP_VSS', 'EXP_LDR'):
            with self.subTest(strategy=strategy), self.assertRaisesRegex(ExecutionLogError, '증빙 연결'):
                self.tracker.prepare_order('paper', 'buy', '005930', 1, strategy)
