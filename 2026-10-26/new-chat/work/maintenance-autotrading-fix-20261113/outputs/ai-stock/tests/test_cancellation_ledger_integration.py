"""Ledger adapters for independently verified cancellations, without broker I/O.

The settlement validator has its own raw-source tests. These tests inject its
validated result and verify every reservation/ownership consumer, preserving
the original financial rows and accepted-entry counts.
"""
from datetime import timedelta
from decimal import Decimal
import json
import hashlib
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

from kiwoom_autotrader.api.executions import Execution
from kiwoom_autotrader.api.orders import OrderReceipt
from kiwoom_autotrader.trading.execution_log import (
    ExecutionTracker, ExecutionLogError, ConcurrentPositionLimitReached, PaperCashLimitReached)
from tests.test_b3_entry import NOW, FILL, config


MODULE='kiwoom_autotrader.trading.cancellation_settlement'


class CancellationLedgerIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.now=FILL-timedelta(seconds=10)
        self.settings={}
        self.tracker=ExecutionTracker(log_dir=Path(self.tmp.name),now=lambda:self.now,
            settings_loader=lambda:self.settings,account_fetcher=Mock(),executions_fetcher=Mock(),profit_fetcher=Mock())
        self.cancelled=0
        self.cancel_numbers=set()
        self.settlement_rows=[]
        self.addCleanup(patch.stopall)
        self.cancelled_mock=patch(MODULE+'.confirmed_cancelled_quantity',side_effect=self.cancel_quantity).start()
        patch(MODULE+'.confirmed_cancel_order_numbers',side_effect=lambda *args:set(self.cancel_numbers)).start()
        patch(MODULE+'.settlement_watermark_rows',side_effect=lambda *args:list(self.settlement_rows)).start()
        self.event('stream_started',self.now-timedelta(seconds=1),{})
        cfg=config();cfg.update(b3_stage='initial',b3_initial_intent_id=None)
        proof={'schema_version':1,'symbol':'005930','pre_order_held_qty':0,'pre_order_pur_amt_krw':0,
            'remaining_principal_krw':1000000,'order_quantity':100,'limit_price_krw':1000,
            'observed_at':self.now.isoformat(),'holdings_source':'broker_observation'}
        self.initial=self.tracker.prepare_order('paper','buy','005930',100,'gdcrs',price=1000,
            evidence={'strategy':'gdcrs','config':cfg},reconciliation_evidence=proof)
        self.tracker.register_order(OrderReceipt('paper','buy','005930','100','1000','24','https://mockapi.kiwoom.com'),
            'gdcrs',intent_id=self.initial)
        self.now=NOW

    def cancel_quantity(self,db,scope,order):
        return self.cancelled if order['ord_no']=='24' else 0

    def event(self,kind,at,values):
        self.tracker.record_broker_observation({'schema_version':1,'source_api':'00','source_kind':'broker_observation',
            'observed_at':at.isoformat(),'day':'20261030','mode':'paper','event_kind':kind,'values':values})

    def fill(self,qty=30):
        self.event('execution_stream',FILL,{'9203':'24','9001':'A005930','907':'2','913':'체결','908':'092030',
            '909':'1','914':'1000','915':str(qty),'911':str(qty),'903':str(qty*1000)})
        self.tracker._ingest('paper','20261030',(Execution('20261030','24','1','005930','fixture','buy',
            '09:20:30',Decimal('1000'),qty),))

    def confirm(self,qty=70):
        self.cancelled=qty;self.cancel_numbers={'25'}
        self.settlement_rows=[['24',qty,'fixture-verified-evidence']]
        self.event('execution_stream',self.now-timedelta(seconds=1),{'9203':'25','904':'24','9001':'A005930',
            '907':'2','905':'+매수취소','913':'확인','900':str(qty),'902':'0','909':'','911':'0','915':'0'})

    def financial(self):
        with sqlite3.connect(self.tracker.path) as db:
            return {table:db.execute('SELECT * FROM '+table).fetchall() for table in
                    ('orders','intents','fills','order_decisions','pre_order_reconciliations')}

    def balance(self,qty=0,deposit='10000000'):
        payload={'schema':'paper-account-snapshot-v1','mode':'paper','exchange':'KRX',
            'cash':{'entr':deposit,'ord_alow_amt':'10000000'},
            'holdings':[{'stk_cd':'A005930','rmnd_qty':str(qty)}] if qty else []}
        with self.tracker._db() as db:
            db.execute('INSERT OR REPLACE INTO account_balance_daily VALUES(?,?,?,?,?,?)',
                ('default','paper','20261030',self.now.isoformat(),'broker_observation',json.dumps(payload)))

    def test_ack_without_confirmed_quantity_never_releases_any_pending_gate(self):
        self.fill()
        for symbol in ('005930',None):
            with self.subTest(symbol=symbol),self.tracker._db() as db,self.assertRaises(ExecutionLogError):
                if symbol:self.tracker._assert_no_pending_order(db,'paper',symbol)
                else:self.tracker._assert_no_pending_account(db,'paper')

    def test_completed_history_does_not_consume_pending_query_budget(self):
        with self.tracker._db() as db:
            orders=[('default','20261029','paper',str(100000+i),'000660','buy','gdcrs',1,self.now.isoformat())
                    for i in range(10001)]
            db.executemany('INSERT INTO orders VALUES(?,?,?,?,?,?,?,?,?)',orders)
            db.executemany('INSERT INTO fills VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                [(o[0],o[1],o[2],o[3],'1',o[4],'fixture','buy','10:00:00','1000',1,'gdcrs',None,None,None)
                 for o in orders])
            self.cancelled_mock.reset_mock()
            pending=self.tracker._pending_order_rows(db,'paper')
            self.assertEqual([r['ord_no'] for r in pending],['24'])
            self.assertEqual(self.cancelled_mock.call_count,1)

    def test_partial_cancel_preserves_originals_and_enables_only_actual_owned_quantity(self):
        self.fill();before=self.financial();self.confirm()
        self.tracker.assert_no_pending_order('paper','005930')
        with self.tracker._db() as db:self.tracker._assert_no_pending_account(db,'paper')
        position=self.tracker.multi_strategy_position('paper','005930','gdcrs',self.now)
        self.assertEqual((position['status'],position['owned_qty'],position['basis_krw']),('owned',30,30000))
        self.assertEqual(self.tracker.multi_strategy_position('paper','005930','S2_surge_high_breakout',self.now)['status'],'unsupported')
        candidate=self.tracker.b3_add_candidate('paper','005930',self.now)
        self.assertEqual((candidate['initial_filled_qty'],candidate['initial_filled_principal_krw']),(30,30000))
        self.assertEqual(self.financial(),before)

    def test_partial_cancel_keeps_held_position_slot_and_fresh_cash_gate(self):
        self.fill();self.confirm();self.balance(30)
        with self.tracker._db() as db:
            with self.assertRaises(ConcurrentPositionLimitReached):
                self.tracker._assert_position_capacity(db,'000660',self.now,1,principal=1000)
            self.assertEqual(self.tracker._assert_position_capacity(db,'005930',self.now,1)['occupied_symbols'],['005930'])
        self.balance(30,deposit='1000')
        with self.tracker._db() as db,self.assertRaises(PaperCashLimitReached):
            self.tracker._assert_position_capacity(db,'005930',self.now,1,principal=1000)

    def test_full_cancel_removes_reservation_but_preserves_accepted_entry_and_needs_new_zero_proof(self):
        before=self.financial();self.confirm(100);self.balance()
        with self.tracker._db() as db:
            self.tracker._assert_no_pending_account(db,'paper')
            self.assertEqual(self.tracker._assert_position_capacity(db,'000660',self.now,1)['occupied_symbols'],[])
            self.assertEqual(self.tracker._daily_buy_entry_counts(db,'paper','005930','20261030')['accepted'],1)
        self.assertEqual(self.tracker.multi_strategy_position('paper','005930','gdcrs',self.now)['status'],'flat_candidate')
        self.assertIsNone(self.tracker.b3_add_candidate('paper','005930',self.now))
        self.assertEqual(self.financial(),before)

    def test_invalidated_late_fill_proof_blocks_all_consumers(self):
        self.fill();self.confirm();self.balance(30)
        self.cancelled_mock.side_effect=ValueError('late fill')
        with self.assertRaises(ExecutionLogError):self.tracker.assert_no_pending_order('paper','005930')
        with self.tracker._db() as db,self.assertRaises(ExecutionLogError):self.tracker._assert_no_pending_account(db,'paper')
        with self.tracker._db() as db,self.assertRaises(ExecutionLogError):self.tracker._assert_position_capacity(db,'000660',self.now,2)
        self.assertNotEqual(self.tracker.multi_strategy_position('paper','005930','gdcrs',self.now)['status'],'owned')
        self.assertIsNone(self.tracker.b3_add_candidate('paper','005930',self.now))

    def test_settlement_changes_dispatch_watermark_without_rewriting_order(self):
        self.fill();old=self.tracker.symbol_order_watermark('paper','005930');before=self.financial()
        self.confirm();new=self.tracker.symbol_order_watermark('paper','005930')
        self.assertNotEqual(old,new);self.assertEqual(self.financial(),before)

    def test_confirmed_partial_buy_can_exit_actual_thirty_shares_and_become_flat(self):
        self.fill();self.confirm();self.settings['paper_multi_strategy_enabled']=True
        holding=self.tracker.record_strategy_holding_observation('005930',30,30,30000,self.now,source_kind='broker_observation')
        proof={'schema_version':4,'symbol':'005930','pre_order_held_qty':30,'pre_order_pur_amt_krw':30000,
            'order_quantity':30,'observed_at':self.now.isoformat(),'holdings_source':'broker_observation',
            'generation_id':self.initial,'exit_kind':'full','strategy':'ddcrs',
            'symbol_ledger_sha256':self.tracker.symbol_order_watermark('paper','005930'),'holding_observation_id':holding}
        with self.assertRaises(ExecutionLogError):
            self.tracker.prepare_order('paper','sell','005930',100,'ddcrs',price=None,reconciliation_evidence=proof)
        intent=self.tracker.prepare_order('paper','sell','005930',30,'ddcrs',price=None,reconciliation_evidence=proof)
        self.tracker.assert_legacy_ddcrs_allowed('paper','005930')
        self.tracker.register_order(OrderReceipt('paper','sell','005930','30',None,'26','https://mockapi.kiwoom.com'),
            'ddcrs',intent_id=intent)
        self.now+=timedelta(seconds=1)
        self.event('execution_stream',self.now,{'9203':'26','9001':'A005930','907':'1','913':'체결','908':'093101',
            '909':'2','914':'1010','915':'30','911':'30','903':'30300'})
        self.tracker._ingest('paper','20261030',(
            Execution('20261030','24','1','005930','fixture','buy','09:20:30',Decimal('1000'),30),
            Execution('20261030','26','2','005930','fixture','sell','09:31:01',Decimal('1010'),30)))
        position=self.tracker.multi_strategy_position('paper','005930','gdcrs',self.now)
        self.assertEqual(position['status'],'flat')

    def test_unknown_cancel_child_cannot_hide_unowned_order_flow(self):
        self.fill();self.cancelled=70
        self.event('execution_stream',self.now,{'9203':'99','904':'24','9001':'A005930','905':'+매수취소','913':'확인'})
        self.assertEqual(self.tracker.multi_strategy_position('paper','005930','gdcrs',self.now)['status'],'unsupported')
        self.assertIsNone(self.tracker.b3_add_candidate('paper','005930',self.now))

    def test_cancel_child_zero_fields_match_finalizer_contract(self):
        values={'9203':'25','904':'24','905':'+매수취소','913':'확인','909':''}
        fields=('903','910','911','914','915')
        for width in range(1,13):
            row={**values,**{key:'0'*width for key in fields}}
            self.assertTrue(self.tracker._confirmed_cancel_event(row,{'25'},{'24'}))
            for key in fields:
                self.assertFalse(self.tracker._confirmed_cancel_event({**row,key:'1'},{'25'},{'24'}))

    def test_sell_cancel_child_requires_explicit_matching_side(self):
        values={'9203':'25','904':'24','905':'-매도취소','913':'확인','909':'','907':'1'}
        self.assertTrue(self.tracker._confirmed_cancel_event(values,{'25'},{'24'}))
        self.assertFalse(self.tracker._confirmed_cancel_event({**values,'907':'2'},{'25'},{'24'}))
        self.assertFalse(self.tracker._confirmed_cancel_event({**values,'907':''},{'25'},{'24'}))


class ConfirmedCancellationSourceIntegrationTests(unittest.TestCase):
    """Run the real finalizer and ledger gates together using mocked TR pages."""
    def fixture(self,qty=3):
        from tests.test_cancellation_settlement import SettlementFixture
        f=SettlementFixture(qty);self.addCleanup(f.close)
        proof={'schema_version':1,'symbol':'005930','pre_order_held_qty':0,'pre_order_pur_amt_krw':0,
            'remaining_principal_krw':1000000,'order_quantity':10,'limit_price_krw':10000,
            'observed_at':'2026-10-30T09:59:58+09:00','holdings_source':'broker_observation'}
        raw=json.dumps(proof,ensure_ascii=True,sort_keys=True,separators=(',',':'))
        with f.tracker._db() as db:
            db.execute('INSERT INTO pre_order_reconciliations VALUES(?,?,?,?,?)',
                ('default','initial1','2026-10-30T09:59:59+09:00',raw,hashlib.sha256(raw.encode()).hexdigest()))
        return f

    def test_verified_pages_and_terminal_release_remainder_preserving_partial_owner_and_accounting(self):
        f=self.fixture()
        with self.assertRaises(ExecutionLogError):f.tracker.assert_no_pending_order('paper','005930')
        f.tracker._materialize('20261030')
        csv_before=(f.root/'20261030.csv').read_bytes()
        original=f.tracker._materialize_accounting('20261030')
        f.settle()
        published=(f.root/'20261030.execution-accounting.json').read_bytes()
        f.tracker.assert_no_pending_order('paper','005930')
        with f.tracker._db() as db:f.tracker._assert_no_pending_account(db,'paper')
        position=f.tracker.multi_strategy_position('paper','005930','gdcrs',f.now)
        self.assertEqual((position['status'],position['owned_qty'],position['basis_krw']),('owned',3,30000))
        accounting=f.tracker._materialize_accounting('20261030')
        self.assertEqual((f.root/'20261030.execution-accounting.json').read_bytes(),published)
        self.assertEqual((f.root/'20261030.csv').read_bytes(),csv_before)
        self.assertEqual(accounting['orders'],original['orders'])
        note=accounting['cancellation_settlements'][0]
        self.assertEqual((note['original_order_quantity'],note['filled_quantity_at_confirmation'],
                          note['cancelled_quantity_at_confirmation']),(10,3,7))
        self.assertEqual(note['cash_adjustment_krw'],0)
        self.assertEqual(note['status'],'final_evidence_committed')

    def test_real_proof_late_fill_blocks_reservations_and_marks_sidecar_unconfirmed(self):
        f=self.fixture();f.settle()
        f.record00({**f.unit(4),'909':'11','915':'1'})
        with self.assertRaises(ExecutionLogError):f.tracker.assert_no_pending_order('paper','005930')
        with f.tracker._db() as db,self.assertRaises(ExecutionLogError):f.tracker._assert_no_pending_account(db,'paper')
        self.assertNotEqual(f.tracker.multi_strategy_position('paper','005930','gdcrs',f.now)['status'],'owned')
        note=f.tracker._materialize_accounting('20261030')['cancellation_settlements'][0]
        self.assertEqual(note['status'],'confirmation_invalidated_requires_review')

    def test_late_projection_matching_full_original_still_revalidates_cancellation(self):
        f=self.fixture();f.settle()
        with f.tracker._db() as db:
            db.execute('''INSERT INTO fills VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                ('default','20261030','paper','1','late','005930','fixture','buy','09:30:00',
                 '10000',7,'gdcrs',None,None,None))
            # Raw 3 + 7 now equals original 10; it must not evade proof validation.
            with self.assertRaises(ExecutionLogError):f.tracker._assert_no_pending_account(db,'paper')

    def test_real_zero_fill_confirmation_preserves_entry_count_and_requests_new_zero_position_proof(self):
        f=self.fixture(0);f.settle()
        f.tracker.assert_no_pending_order('paper','005930')
        with f.tracker._db() as db:
            self.assertEqual(f.tracker._daily_buy_entry_counts(db,'paper','005930','20261030')['accepted'],1)
        self.assertEqual(f.tracker.multi_strategy_position('paper','005930','gdcrs',f.now)['status'],'flat_candidate')


if __name__=='__main__':unittest.main()
