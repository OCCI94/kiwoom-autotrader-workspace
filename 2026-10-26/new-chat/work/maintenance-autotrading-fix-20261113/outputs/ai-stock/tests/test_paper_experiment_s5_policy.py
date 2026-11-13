"""Offline S5 profile, frozen assignment and atomic shared reservation tests."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from zoneinfo import ZoneInfo
import json

from kiwoom_autotrader.config.settings import (effective_paper_experiment, default_settings,
    save_paper_strategy_options,load_settings,paper_strategy_options_path,SettingsError)
from kiwoom_autotrader.trading import paper_experiment_rules as rules
from kiwoom_autotrader.trading.execution_log import (ExecutionTracker,ConcurrentPositionLimitReached,
    ConcurrentPositionLimitUnavailable,PaperCashLimitReached)

S5='S5_steady_uptrend_vwap'
NOW=datetime(2026,11,3,10,0,tzinfo=ZoneInfo('Asia/Seoul'))

class NativeSixAssignments(TestCase):
    def test_equal_target_probability_and_prior_profiles_still_validate(self):
        seen=set()
        for i in range(200):
            row=rules.assign_candidate('20261103',f'{i:06}',NOW,profile='native-six-s5-v1')
            self.assertEqual(set(row['weights'].values()),{1})
            self.assertEqual(sum(row['weights'].values()),6)
            rules.validate_assignment(row,symbol=row['symbol'],day='20261103',now=NOW)
            seen.add(row['arm'])
        self.assertEqual(seen,set(dict(rules.NATIVE_SIX_S5_WEIGHTS)))
        for profile in (None,'native-five-v1'):
            old=rules.assign_candidate('20261103','005930',NOW,profile=profile)
            preserved=rules.assign_candidate('20261103','005930',NOW+timedelta(minutes=1),
                profile='native-six-s5-v1',existing=old)
            self.assertEqual(old,preserved)

    def test_finite_cohorts_retain_control_minimum_and_balanced_six(self):
        for size in range(2,41):
            rows=[{'symbol':f'{i:06}','first_seen_at':NOW.isoformat()} for i in range(size)]
            result=rules.assign_cohort(rows,day='20261103',snapshot_at=NOW,profile='native-six-s5-v1')
            for name in ('EXP_R','EXP_A'):self.assertGreaterEqual(result['actual_counts'][name]/size,.15)
            # Small cohorts cannot satisfy six equal counts and ceil(15%)
            # controls simultaneously (e.g. N=7 needs two R and two A).
            self.assertLessEqual(max(result['actual_counts'].values())-min(result['actual_counts'].values()),2)
            for row in result['assignments']:rules.validate_assignment(row,symbol=row['symbol'],day='20261103',now=NOW)

    def test_source_opt_in_is_paper_only_and_old_sidecar_loads(self):
        self.assertFalse(default_settings()['paper_experiment_s5_enabled'])
        self.assertFalse(effective_paper_experiment({'paper_experiment_enabled':True},'paper')['s5_enabled'])
        class NoRead(dict):
            def get(self,*args):raise AssertionError('real read paper options')
        self.assertEqual(effective_paper_experiment(NoRead(),'real'),{'enabled':False})
        with TemporaryDirectory() as folder:
            path=Path(folder)/'settings.json'
            save_paper_strategy_options({'paper_experiment_enabled':True,'paper_experiment_s5_enabled':True},path)
            self.assertTrue(load_settings(path)['paper_experiment_s5_enabled'])
            self.assertFalse(load_settings(path,mode='real')['paper_experiment_s5_enabled'])
            self.assertFalse(path.exists())
            before=paper_strategy_options_path(path).read_bytes()
            for value in (1,'true',None):
                with self.assertRaises(SettingsError):save_paper_strategy_options({'paper_experiment_s5_enabled':value},path)
            self.assertEqual(paper_strategy_options_path(path).read_bytes(),before)

class S5SharedReservation(TestCase):
    def setUp(self):
        self.tmp=TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.settings={'paper_experiment_enabled':True,'paper_experiment_s5_enabled':True}
        self.tracker=self.make_tracker();self.cash=9000000;self.balance([])
    def make_tracker(self):
        return ExecutionTracker(log_dir=Path(self.tmp.name),now=lambda:NOW,settings_loader=lambda:self.settings)
    def balance(self,holdings):
        payload={'schema':'paper-account-snapshot-v1','mode':'paper','exchange':'KRX',
            'holdings':holdings,'cash':{'entr':str(self.cash),'ord_alow_amt':str(self.cash)}}
        with self.tracker._db() as db:
            db.execute('INSERT OR REPLACE INTO account_balance_daily VALUES(?,?,?,?,?,?)',
                ('default','paper','20261103',NOW.isoformat(),'broker_observation',json.dumps(payload)))
    def check(self,tracker,symbol,arm,principal=1000,ignore=None,reserve=False):
        with tracker._db() as db:
            if not db.in_transaction:db.execute('BEGIN IMMEDIATE')
            result=tracker._assert_position_capacity(db,symbol,NOW,40,principal=principal,
                strategy=arm,ignored_intent_id=ignore)
            if reserve:
                ident=symbol+arm
                db.execute('INSERT INTO intents VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                    ('default',ident,'20261103','paper','buy',symbol,1,arm,NOW.isoformat(),None,'prepared',None))
                db.execute('INSERT INTO order_decisions VALUES(?,?,?,?,?,?,?)',
                    ('default',ident,NOW.isoformat(),str(principal),'limit',None,None))
            return result
    def test_non_s5_cannot_consume_last_two_shared_slots(self):
        for i in range(38):self.check(self.tracker,f'{i:06}','EXP_R',reserve=True)
        with self.assertRaisesRegex(ConcurrentPositionLimitReached,'s5_reserved_positions'):
            self.check(self.tracker,'800000','EXP_A')
        self.check(self.tracker,'800001',S5,reserve=True)
        with self.assertRaises(ConcurrentPositionLimitReached):self.check(self.tracker,'800002','EXP_A')
        self.check(self.tracker,'800002',S5,reserve=True)
        with self.assertRaises(ConcurrentPositionLimitReached):self.check(self.tracker,'800003',S5)
    def test_unused_s5_cash_reserved_and_own_intent_not_double_counted(self):
        self.cash=450000;self.balance([])
        with self.assertRaisesRegex(PaperCashLimitReached,'s5_reserved_cash'):
            self.check(self.tracker,'800000','EXP_A',principal=430000)
        self.check(self.tracker,'800000',S5,principal=430000,reserve=True)
        result=self.check(self.tracker,'800000',S5,principal=430000,ignore='800000'+S5)
        self.assertEqual(result['s5_reservation']['local_intent_cash_krw'],0)
        with self.assertRaises(PaperCashLimitReached):self.check(self.tracker,'800001','EXP_A',principal=20000)
    def test_pending_s5_counts_once_and_two_trackers_cannot_take_reserved_slot(self):
        self.check(self.tracker,'800001',S5,reserve=True)
        for i in range(37):self.check(self.tracker,f'{i:06}','EXP_A',reserve=True)
        other=self.make_tracker()
        def run(pair):
            try:self.check(pair[0],pair[1],'EXP_A',reserve=True);return True
            except ConcurrentPositionLimitReached:return False
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes=list(pool.map(run,[(self.tracker,'900001'),(other,'900002')]))
        self.assertEqual(sum(outcomes),1)
    def test_disabled_keeps_old_capacity_and_unknown_holdings_do_not_free_reserve(self):
        self.balance([{'stk_cd':f'A{i:06}','rmnd_qty':'1'} for i in range(38)])
        with self.assertRaises(ConcurrentPositionLimitReached):self.check(self.tracker,'900000','EXP_R')
        self.settings['paper_experiment_s5_enabled']=False
        self.check(self.tracker,'900000','EXP_R')
    def test_missing_local_intent_price_fails_closed(self):
        with self.tracker._db() as db:
            db.execute('INSERT INTO intents VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                ('default','legacy','20261103','paper','buy','800000',1,'EXP_R',NOW.isoformat(),None,'prepared',None))
        with self.assertRaises(ConcurrentPositionLimitUnavailable):self.check(self.tracker,'800001',S5)
