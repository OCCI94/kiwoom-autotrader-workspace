"""Pure bootstrap candidates never settle, write or release financial gates."""
from contextlib import contextmanager
from datetime import timedelta
import hashlib
import json
import sqlite3
import unittest
from unittest.mock import patch
from tests.test_cancellation_settlement import SettlementFixture
from tests.test_cancellation_settlement_production_labels import ObservedFixture,CASES
from kiwoom_autotrader.trading.broker_observation_repeats import install_evidence_view
from kiwoom_autotrader.trading.cancellation_settlement import (
    cancellation_reconciliation_candidate,CancellationSettlementUnavailable,TABLE,PUBLICATIONS)


@contextmanager
def snapshot(f):
    db=sqlite3.connect(f.tracker.path.as_uri()+'?mode=ro',uri=True)
    db.row_factory=sqlite3.Row
    install_evidence_view(db)
    db.execute('PRAGMA trusted_schema=OFF');db.execute('PRAGMA query_only=ON');db.execute('BEGIN')
    try:yield db
    finally:db.rollback();db.close()


class CancellationBootstrapTests(unittest.TestCase):
    def fixture(self,case=None,**kw):
        f=ObservedFixture(case or CASES[0],**kw);self.addCleanup(f.close);return f
    def pages(self,f):
        for api,rows in [('ka10075',f.pending),('kt00009',f.rest)]:f.tracker.record_broker_observation(f.page(api,rows))
    def candidate(self,f,now=None):
        with snapshot(f)as db:
            before=db.total_changes
            value=cancellation_reconciliation_candidate(db,'default',f.now.strftime('%Y%m%d'),f.claim['original_ord_no'],now=now or f.now)
            self.assertEqual(db.total_changes,before)
            return value
    def test_nine_real_producer_cases_are_candidates_not_settled(self):
        for case in CASES:
            with self.subTest(number=case['order']['ord_no']):
                f=self.fixture(case);self.pages(f);before=f.tracker.path.read_bytes()
                value=self.candidate(f)
                self.assertEqual(value['status'],'bootstrap_reconciliation_candidate')
                self.assertFalse(value['settled']);self.assertFalse(value['reservations_released'])
                self.assertEqual(value['filled_quantity'],0);self.assertEqual(value['cancelled_quantity'],case['order']['quantity'])
                raw={k:v for k,v in value.items()if k!='evidence_sha256'}
                self.assertEqual(hashlib.sha256(json.dumps(raw,ensure_ascii=True,sort_keys=True,separators=(',',':')).encode()).hexdigest(),value['evidence_sha256'])
                self.assertEqual(before,f.tracker.path.read_bytes());self.assertEqual(f.released(),0)
                with snapshot(f)as db:
                    self.assertFalse(db.execute("SELECT name FROM sqlite_master WHERE name IN (?,?)",(TABLE,PUBLICATIONS)).fetchall())
    def test_requires_readonly_active_transaction(self):
        f=self.fixture();self.pages(f)
        with f.tracker._db()as db:
            with self.assertRaisesRegex(CancellationSettlementUnavailable,'readonly_snapshot'):
                cancellation_reconciliation_candidate(db,'default','20261103',f.number,now=f.now)
    def test_no_session_or_network_entrypoints(self):
        f=self.fixture();self.pages(f)
        with patch('kiwoom_autotrader.trading.cancellation_settlement._fetch_pending',side_effect=AssertionError),patch('kiwoom_autotrader.trading.cancellation_settlement._fetch_orders',side_effect=AssertionError),patch('kiwoom_autotrader.trading.cancellation_settlement._stream',side_effect=AssertionError):
            self.candidate(f)
    def test_partial_one_fill_is_not_bootstrap_candidate_even_if_settle_can_reconcile(self):
        f=SettlementFixture(1,strategy='EXP_D');self.addCleanup(f.close);self.pages(f)
        with self.assertRaisesRegex(CancellationSettlementUnavailable,'requires_zero_fill'):self.candidate(f)
        self.assertEqual(f.released(),0)
    def test_ack_without_terminal_cannot_bootstrap(self):
        f=self.fixture(terminal=False);self.pages(f)
        with self.assertRaisesRegex(CancellationSettlementUnavailable,'00_terminal_missing'):self.candidate(f)
    def test_unknown_enum_or_forged_ack_link_rejected(self):
        for row,key,value in [(0,'mdfy_cncl_tp','unknown'),(1,'orig_ord_no','0000001'),(1,'ord_no','1234567')]:
            f=self.fixture();f.rest[row][key]=value;self.pages(f)
            with self.assertRaises(CancellationSettlementUnavailable):self.candidate(f)
    def test_latest_partial_page_does_not_fall_back(self):
        f=self.fixture();self.pages(f)
        e=f.page('ka10075',[]);e['continuation']['response_cont_yn']='Y';e['continuation']['next_key_present']=True
        f.tracker.record_broker_observation(e)
        with self.assertRaisesRegex(CancellationSettlementUnavailable,'query_completion'):self.candidate(f)
    def test_latest_failed_page_does_not_fall_back(self):
        f=self.fixture();self.pages(f)
        e=f.page('kt00009',f.rest);e['status']='transport_error';e['http_status']=500;e['response_body']={};f.tracker.record_broker_observation(e)
        with self.assertRaisesRegex(CancellationSettlementUnavailable,'query_completion'):self.candidate(f)
    def test_stale_future_crossday_and_widened_age_rejected(self):
        f=self.fixture();self.pages(f)
        for at in (f.now+timedelta(seconds=91),f.now-timedelta(seconds=1),f.now+timedelta(days=1)):
            with self.assertRaises(CancellationSettlementUnavailable):self.candidate(f,at)
        with snapshot(f)as db:
            with self.assertRaises(CancellationSettlementUnavailable):cancellation_reconciliation_candidate(db,'default','20261103',f.number,now=f.now,max_age_seconds=91)
    def test_nonzero_pending_rejected(self):
        f=self.fixture();f.pending=[{'ord_no':f.number,'orig_ord_no':'0','stk_cd':f.case['order']['symbol'],'oso_qty':'1'}];self.pages(f)
        with self.assertRaisesRegex(CancellationSettlementUnavailable,'remainder_not_zero'):self.candidate(f)
    def test_late_unit_and_changed_00_block_new_snapshot(self):
        f=self.fixture();self.pages(f);self.candidate(f)
        o=f.case['order'];v={**f.case['ws'][0]['values'],'913':'체결','909':'123','914':o['order_price'],'915':'1','911':'1','903':o['order_price'],'902':str(o['quantity']-1),'908':'172800'}
        f.record00(v)
        with self.assertRaises(CancellationSettlementUnavailable):self.candidate(f)
        f=self.fixture();self.pages(f);v={**f.case['ws'][0]['values'],'901':'1'};f.record00(v)
        with self.assertRaisesRegex(CancellationSettlementUnavailable,'00_identity_conflict'):self.candidate(f)
    def test_claim_or_page_hash_tamper_rejected(self):
        for target in ('claim','page'):
            f=self.fixture();self.pages(f)
            with sqlite3.connect(f.tracker.path)as db:
                if target=='claim':
                    db.execute('DROP TRIGGER buy_ttl_cancellation_attempts_no_update');db.execute("UPDATE buy_ttl_cancellation_attempts SET evidence_sha256=?",('0'*64,))
                else:
                    for name, in db.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='broker_observations'").fetchall():db.execute('DROP TRIGGER '+name)
                    db.execute("UPDATE broker_observations SET digest=? WHERE source_api='ka10075'",('0'*64,))
            with self.assertRaises(CancellationSettlementUnavailable):self.candidate(f)

if __name__=='__main__':unittest.main()
