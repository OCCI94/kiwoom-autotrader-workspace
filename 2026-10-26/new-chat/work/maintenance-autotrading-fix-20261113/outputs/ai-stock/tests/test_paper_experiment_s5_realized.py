"""Actual native S5 producer -> accepted orders -> 00 units -> readonly FIFO."""
from datetime import timedelta
from decimal import Decimal
import json,hashlib,sqlite3
from unittest import TestCase
from kiwoom_autotrader.api.executions import Execution
from kiwoom_autotrader.trading.realized_generation import collect_realized_profit_evidence,prepare_realized_reader
from tests import test_paper_experiment_s5_dispatch_e2e as native_producer


def native_s5_roundtrip(test):
    """Private temporary actual-schema fixture, reusable by reporting tests."""
    producer=native_producer.S5MonitorDispatchE2E('test_s5_to_queued_order_and_persistent_native_generation')
    test.addCleanup(producer.doCleanups)
    producer.test_s5_to_queued_order_and_persistent_native_generation()
    case=producer.case;context=producer.native_context
    case.now+=timedelta(seconds=1)
    price=context['price']+10;qty=context['quantity']
    case.tracker.record_broker_observation({'schema_version':1,'source_api':'00','source_kind':'broker_observation',
        'observed_at':case.now.isoformat(),'day':'20261030','mode':'paper','event_kind':'execution_stream',
        'values':{'9203':'32','9001':'A005930','907':'1','913':'체결','908':case.now.strftime('%H%M%S'),
            '909':'2','914':str(price),'915':str(qty),'911':str(qty),'903':str(price*qty)}})
    case.tracker._ingest('paper','20261030',(Execution('20261030','32','2','005930','fixture','sell',
        case.now.strftime('%H:%M:%S'),Decimal(price),qty),))
    return case,context

class NativeS5Realized(TestCase):
    def setUp(self):self.case,self.context=native_s5_roundtrip(self)
    def result(self):
        with sqlite3.connect(self.case.tracker.path.resolve().as_uri()+'?mode=ro',uri=True) as db:
            db.row_factory=sqlite3.Row;prepare_realized_reader(db);db.execute('PRAGMA query_only=ON');db.execute('PRAGMA trusted_schema=OFF');db.execute('BEGIN')
            return collect_realized_profit_evidence(db,'20261030',self.case.now)
    def test_native_zero_epoch_and_00_units_yield_exact_gross_no_invented_net(self):
        before=self.case.tracker.path.read_bytes();result=self.result()
        self.assertEqual(before,self.case.tracker.path.read_bytes())
        self.assertIn('005930',result['by_symbol'],result)
        row=result['by_symbol']['005930']
        self.assertEqual(Decimal(row['verified_gross_pnl_krw']),self.context['quantity']*10)
        self.assertEqual(row['sold_quantity'],self.context['quantity'])
        self.assertFalse(row['actual_costs_reconciled'])
        self.assertIn('native_s5_v6',row['basis_method'])
        self.assertEqual(self.case.tracker.multi_strategy_position('paper','005930',
            'S5_steady_uptrend_vwap',self.case.now)['status'],'flat')
    def test_rehashed_missing_assignment_and_zero_basis_cannot_masquerade_as_native(self):
        with sqlite3.connect(self.case.tracker.path) as db:
            db.row_factory=sqlite3.Row
            row=db.execute("SELECT d.* FROM order_decisions d JOIN intents i ON d.scope=i.scope AND d.intent_id=i.id WHERE i.strategy='S5_steady_uptrend_vwap' AND i.side='buy'").fetchone()
            value=json.loads(row['evidence_json']);value['config']['experiment_assignment_sha256']='f'*64
            value['config_sha256']=None
            raw=json.dumps(value,sort_keys=True,separators=(',',':'))
            db.execute('DROP TRIGGER order_decisions_no_update')
            db.execute('UPDATE order_decisions SET evidence_json=?,evidence_sha256=? WHERE intent_id=?',
                (raw,hashlib.sha256(raw.encode()).hexdigest(),row['intent_id']))
        self.assertNotIn('005930',self.result()['by_symbol'])
    def test_unreconciled_sale_quantity_and_new_stream_gap_are_unavailable(self):
        with sqlite3.connect(self.case.tracker.path) as db:
            db.execute("UPDATE orders SET quantity=quantity+1 WHERE ord_no='32'")
        self.assertNotIn('005930',self.result()['by_symbol'])
