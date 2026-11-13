import json
from datetime import datetime,timedelta
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from kiwoom_autotrader.trading.prearmed_account_index import BackgroundAccountIndex


NOW=datetime.fromisoformat('2026-11-02T09:35:00+09:00')


class AccountIndexTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'ledger.sqlite3';self.clock=[10.]
        self.body={'schema':'paper-account-snapshot-v1','mode':'paper','exchange':'KRX',
            'holdings':[],'cash':{'entr':'9000000','ord_alow_amt':'8000000'}}
        with sqlite3.connect(self.path) as db:
            db.execute('CREATE TABLE account_balance_daily(scope TEXT,mode TEXT,day TEXT,observed_at TEXT,source_kind TEXT,payload_json TEXT)')
            db.execute('INSERT INTO account_balance_daily VALUES(?,?,?,?,?,?)',('scope','paper','20261102',NOW.isoformat(),'broker_observation',json.dumps(self.body)))
        self.occupied=['053800'];self.pending=False;self.after_fill=False;self.readonly=[]
        self.mapping={'themes_by_symbol':{'042510':['security'],'053800':['security'],'003220':['medicine']},
            'invalid_theme_symbols':[],'taxonomy_evidence':{'source_sha256':'a'*64},'supplement_evidence':None}
        def public(now):
            return {'mode':'paper','as_of':now.isoformat(),'observed_at':self.observed_at(),
                'position_limit':8,'theme_limit':2,'unclassified_policy':'block',
                'taxonomy_evidence':self.mapping['taxonomy_evidence'],'supplement_evidence':None}
        def capacity(db,symbol,now,limit,**kwargs):
            self.readonly.append(db.execute('PRAGMA query_only').fetchone()[0])
            if self.after_fill:raise ValueError('later_fill_requires_new_balance')
            row=db.execute("SELECT observed_at FROM account_balance_daily WHERE scope='scope' AND mode='paper'").fetchone()
            return {'occupied_symbols':self.occupied,'observed_at':row[0]}
        def no_pending(db,mode):
            self.assertEqual(mode,'paper');self.assertEqual(db.execute('PRAGMA query_only').fetchone()[0],1)
            if self.pending:raise ValueError('pending_or_partial_cancel_not_final')
        self.tracker=SimpleNamespace(path=self.path,scope='scope',theme_taxonomy_path=Path(self.tmp.name)/'taxonomy.json',
            paper_theme_capacity_snapshot=public,_assert_position_capacity=capacity,
            _assert_no_pending_account=no_pending)
        self.index=BackgroundAccountIndex(clock=lambda:self.clock[0])

    def observed_at(self):
        with sqlite3.connect(self.path) as db:return db.execute('SELECT observed_at FROM account_balance_daily').fetchone()[0]

    def write(self,**values):
        with sqlite3.connect(self.path) as db:
            for key,value in values.items():
                if key=='payload_json':value=json.dumps(value)
                db.execute('UPDATE account_balance_daily SET '+key+'=?',(value,))

    def refresh(self,now=NOW,symbols=('042510','053800','003220')):
        with patch('kiwoom_autotrader.trading.prearmed_account_index.theme_family_snapshot',return_value=self.mapping):
            return self.index.refresh(self.tracker,symbols,now)

    def test_readonly_account_cash_theme_index_never_authorizes_or_estimates_price(self):
        self.assertTrue(self.refresh());row=self.index.lookup('042510',NOW)
        self.assertEqual(row['theme_remaining_slots'],1)
        self.assertEqual(row['position_remaining_slots'],7)
        self.assertEqual(row['available_cash_krw'],8000000)
        self.assertEqual(row['remaining_principal_krw'],1000000)
        self.assertEqual(row['observed_held_qty'],0)
        self.assertIsNone(row['estimated_initial_qty'])
        self.assertFalse(row['order_authorized']);self.assertTrue(row['requires_fresh_reconciliation'])
        self.assertEqual(self.readonly,[1]);self.assertFalse(row['snapshot_is_atomic_broker_balance'])
        self.assertEqual(self.index.lookup('003220',NOW)['theme_remaining_slots'],2)
        row['available_cash_krw']=0
        self.assertEqual(self.index.lookup('042510',NOW)['available_cash_krw'],8000000)

    def test_pending_or_cancel_ack_keeps_reserved_principal_and_quantity_unknown(self):
        self.pending=True;self.occupied=['053800','042510']
        self.assertTrue(self.refresh());row=self.index.lookup('042510',NOW)
        self.assertEqual(row['theme_remaining_slots'],0)
        self.assertEqual(row['observed_orderable_cash_krw'],8000000)
        self.assertIsNone(row['reserved_principal_krw'])
        self.assertIsNone(row['remaining_principal_krw'])
        self.assertIsNone(row['available_cash_krw'])
        self.assertIsNone(row['estimated_initial_qty'])
        self.assertEqual(row['reservation_status'],'pending_or_unverified')

    def test_later_fill_invalidates_prior_observation_instead_of_synthesizing_holding(self):
        self.assertTrue(self.refresh());self.after_fill=True
        self.assertFalse(self.refresh())
        self.assertEqual(self.index.lookup('042510',NOW)['status'],'unavailable')
        self.assertIsNone(self.index.lookup('042510',NOW)['observed_held_qty'])

    def test_actual_holding_principal_is_observed_and_zero_remaining_is_preserved(self):
        self.body['holdings']=[{'stk_cd':'042510','crd_tp':'00','crd_loan_dt':'',
            'rmnd_qty':'100','trde_able_qty':'100','pur_amt':'1000000'}]
        self.occupied=['053800','042510'];self.write(payload_json=self.body)
        self.assertTrue(self.refresh());row=self.index.lookup('042510',NOW)
        self.assertEqual(row['observed_held_qty'],100)
        self.assertEqual(row['observed_held_principal_krw'],1000000)
        self.assertEqual(row['remaining_principal_krw'],0)
        self.assertTrue(row['already_occupied'])

    def test_unknown_holding_cost_does_not_become_zero(self):
        self.body['holdings']=[{'stk_cd':'042510','crd_tp':'00','crd_loan_dt':'',
            'rmnd_qty':'10','trde_able_qty':'10','pur_amt':None}]
        self.occupied=['042510'];self.write(payload_json=self.body)
        self.assertTrue(self.refresh());row=self.index.lookup('042510',NOW)
        self.assertIsNone(row['observed_held_principal_krw'])
        self.assertIsNone(row['remaining_principal_krw'])

    def test_cache_stalls_clock_reversal_or_account_age_cannot_return_current_values(self):
        self.assertTrue(self.refresh())
        self.clock[0]+=2.501
        self.assertIsNone(self.index.lookup('042510',NOW)['theme_remaining_slots'])
        self.clock[0]=10.
        self.assertEqual(self.index.lookup('042510',NOW-timedelta(microseconds=1))['status'],'unavailable')
        self.write(observed_at=(NOW-timedelta(seconds=91)).isoformat())
        self.assertFalse(self.refresh())

    def test_day_mode_scope_or_source_mismatch_is_unknown(self):
        for key,value in [('day','20261030'),('mode','real'),('scope','other'),('source_kind','projection')]:
            with self.subTest(key=key):
                self.write(**{key:value});self.assertFalse(self.refresh())
                self.write(day='20261102',mode='paper',scope='scope',source_kind='broker_observation')

    def test_lookup_has_no_sqlite_files_or_locks_and_clear_revokes(self):
        self.assertTrue(self.refresh())
        with (patch('sqlite3.connect',side_effect=AssertionError('no read-path SQL')),
              patch('builtins.open',side_effect=AssertionError('no read-path file'))):
            self.assertEqual(self.index.lookup('042510',NOW)['theme_remaining_slots'],1)
        self.index.clear();self.assertEqual(self.index.lookup('042510',NOW)['status'],'unavailable')

    def test_read_transaction_cannot_mutate_ledger(self):
        def bad(db,mode):
            db.execute("UPDATE account_balance_daily SET source_kind='changed'")
        self.tracker._assert_no_pending_account=bad
        self.assertTrue(self.refresh());self.assertIsNone(self.index.lookup('042510',NOW)['reserved_principal_krw'])
        with sqlite3.connect(self.path) as db:self.assertEqual(db.execute('SELECT source_kind FROM account_balance_daily').fetchone()[0],'broker_observation')


if __name__=='__main__':unittest.main()
