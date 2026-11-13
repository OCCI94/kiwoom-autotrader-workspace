"""Ownership regression using local 00 and REST execution fixtures only."""
import sqlite3
import unittest
from unittest.mock import patch
from datetime import timedelta
from decimal import Decimal
from kiwoom_autotrader.api.executions import Execution
from kiwoom_autotrader.api.orders import OrderReceipt
from kiwoom_autotrader.trading.execution_log import ExecutionLogError
import tests.test_multi_strategy_exit_ledger as fixture_exit
import tests.test_b3_ledger as fixture_b3

STRATEGY = fixture_exit.STRATEGY

class MultiOwnershipReview(unittest.TestCase):
    def setUp(self):
        settings_patch=patch('kiwoom_autotrader.trading.execution_log.load_settings', return_value={})
        settings_patch.start(); self.addCleanup(settings_patch.stop)
        self.fixture = fixture_exit.MultiExitTests('test_half_claim_is_durable_and_dispatch_valid')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.tracker = self.fixture.tracker
    def _full_sell(self):
        x=self.fixture
        ident=x.exit(x.proof(kind='full',quantity=100,held=100,basis=100000))
        self.tracker.register_order(OrderReceipt('paper','sell','005930','100',None,'25',
            'https://mockapi.kiwoom.com'),STRATEGY,intent_id=ident)
        x.event('execution_stream',x.now,{'9203':'25','9001':'A005930','907':'1','913':'체결',
            '908':'092100','909':'2','914':'1100','915':'100','911':'100','903':'110000'})
        self.tracker._ingest('paper','20261030',(
            Execution('20261030','24','1','005930','fixture','buy','09:20:30',Decimal('1000'),100),
            Execution('20261030','25','2','005930','fixture','sell','09:21:00',Decimal('1100'),100)))
    def test_complete_cycle_allows_other_strategy_only_with_proven_flat_broker_state(self):
        self._full_sell()
        x=self.fixture
        other='S3_upper_limit_continuation'
        state=self.tracker.multi_strategy_position('paper','005930',other,x.now)
        self.assertEqual(state['status'],'flat')
        holding_id=self.tracker.record_strategy_holding_observation('005930',0,0,0,x.now,source_kind='broker_observation')
        base={'holding_observation_id':holding_id,'schema_version':3,'symbol':'005930','pre_order_held_qty':0,'pre_order_pur_amt_krw':0,
          'remaining_principal_krw':1000000,'order_quantity':10,'limit_price_krw':1000,
          'observed_at':x.now.isoformat(),'holdings_source':'broker_observation',
          'symbol_cap_krw':1000000,'per_buy_cap_krw':1000000,'multi_strategy_enabled':True,
          'symbol_ledger_sha256':self.tracker.symbol_order_watermark('paper','005930')}
        wrong=dict(base, pre_order_held_qty=1,pre_order_pur_amt_krw=1000,remaining_principal_krw=999000)
        with self.assertRaises(ExecutionLogError):
            self.tracker.prepare_order('paper','buy','005930',10,other,price=1000,reconciliation_evidence=wrong)
        forged=dict(base, holding_observation_id='f'*32)
        with self.assertRaises(ExecutionLogError):
            self.tracker.prepare_order('paper','buy','005930',10,other,price=1000,reconciliation_evidence=forged)
        ident=self.tracker.prepare_order('paper','buy','005930',10,other,price=1000,reconciliation_evidence=base)
        self.assertTrue(ident)
    def test_owned_strategy_blocks_legacy_ddcrs_sell(self):
        with self.assertRaises(ExecutionLogError):
            self.tracker.prepare_order('paper','sell','005930',100,'ddcrs',price=None)
    def test_legacy_ddcrs_still_blocked_for_overnight_new_strategy(self):
        self.fixture.now += timedelta(days=1)
        with self.assertRaises(ExecutionLogError):
            self.tracker.assert_legacy_ddcrs_allowed('paper','005930')
    def test_verified_not_sent_exit_can_be_reserved_again(self):
        x=self.fixture
        first=x.exit(x.proof(kind='full',quantity=100,held=100,basis=100000))
        self.tracker.cancel_intent(first,'not_sent')
        second=x.exit(x.proof(kind='full',quantity=100,held=100,basis=100000))
        self.assertNotEqual(first,second)
        self.tracker.assert_multi_exit_dispatch('paper','005930',second)
        with sqlite3.connect(self.tracker.path) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM multi_strategy_exit_claims').fetchone()[0],2)
    def test_uncertain_exit_blocks_further_reservation(self):
        x=self.fixture
        x.exit(x.proof(kind='full',quantity=100,held=100,basis=100000))
        with self.assertRaises(ExecutionLogError):
            x.exit(x.proof(kind='full',quantity=100,held=100,basis=100000))
    def test_overnight_owner_requires_fresh_broker_proof_to_sell(self):
        x=self.fixture
        # The original 00 and REST fill remain recorded on their actual day.
        x.now += timedelta(days=1)
        result=self.tracker.multi_strategy_position('paper','005930',STRATEGY,x.now)
        self.assertEqual((result['status'],result['owned_qty']),('owned',100))
        self.assertTrue(result['overnight_unobserved_interval'])
        invalid=x.proof(kind='full',quantity=100,held=99,basis=99000)
        with self.assertRaises(ExecutionLogError): x.exit(invalid)
        valid=x.proof(kind='full',quantity=100,held=100,basis=100000)
        ident=x.exit(valid)
        self.tracker.assert_multi_exit_dispatch('paper','005930',ident)
    def test_overnight_unobserved_interval_blocks_additional_buy(self):
        x=self.fixture
        x.now += timedelta(days=1)
        holding=self.tracker.record_strategy_holding_observation('005930',100,100,100000,x.now,source_kind='broker_observation')
        proof={'holding_observation_id':holding,'schema_version':3,'symbol':'005930',
          'pre_order_held_qty':100,'pre_order_pur_amt_krw':100000,'remaining_principal_krw':900000,
          'order_quantity':10,'limit_price_krw':1000,'observed_at':x.now.isoformat(),
          'holdings_source':'broker_observation','symbol_cap_krw':1000000,'per_buy_cap_krw':1000000,
          'multi_strategy_enabled':True,'symbol_ledger_sha256':self.tracker.symbol_order_watermark('paper','005930')}
        with self.assertRaises(ExecutionLogError):
            self.tracker.prepare_order('paper','buy','005930',10,STRATEGY,price=1000,reconciliation_evidence=proof)
    def test_overnight_partial_or_conflicting_fill_does_not_infer_owner(self):
        x=self.fixture
        with sqlite3.connect(self.tracker.path) as db:
            db.execute('UPDATE fills SET quantity=99 WHERE ord_no=?',('24',))
        x.now += timedelta(days=1)
        result=self.tracker.multi_strategy_position('paper','005930',STRATEGY,x.now)
        self.assertNotIn(result['status'],('owned','flat'))

class B3OwnershipReview(unittest.TestCase):
    def setUp(self):
        settings_patch=patch('kiwoom_autotrader.trading.execution_log.load_settings', return_value={})
        settings_patch.start(); self.addCleanup(settings_patch.stop)
        self.fixture=fixture_b3.B3LedgerTests('test_fully_reconciled_00_and_rest_yield_exact_candidate')
        self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)
    def test_legacy_gdcrs_overnight_ddcrs_remains_permitted_by_owner_gate(self):
        x=self.fixture
        x.tracker.settings_loader=lambda:{'paper_multi_strategy_enabled':True}
        x.now += timedelta(days=1)
        x.tracker.assert_legacy_ddcrs_allowed('paper','005930')
        # This is only the owner gate: the actual broker reconciliation and
        # final order dispatch remain independently required in production.
        ident=x.tracker.prepare_order('paper','sell','005930',100,'ddcrs',price=None,require_no_pending=True)
        self.assertTrue(ident)
    def test_initial_and_add_are_same_owner(self):
        x=self.fixture
        ident=x.prepare_add()
        x.tracker.register_order(OrderReceipt('paper','buy','005930','10','1005','25',
            'https://mockapi.kiwoom.com'),'gdcrs',intent_id=ident)
        x.event('execution_stream',x.now,{'9203':'25','9001':'A005930','907':'2','913':'체결',
             '908':'092100','909':'2','914':'1005','915':'10','911':'10','903':'10050'})
        x.tracker._ingest('paper','20261030',(
            Execution('20261030','24','1','005930','fixture','buy','09:20:30',Decimal('1000'),100),
            Execution('20261030','25','2','005930','fixture','buy','09:21:00',Decimal('1005'),10)))
        state=x.tracker.multi_strategy_position('paper','005930','gdcrs',x.now)
        self.assertEqual((state['status'],state['owned_qty'],state['basis_krw']),('owned',110,110050))

if __name__ == '__main__': unittest.main()
