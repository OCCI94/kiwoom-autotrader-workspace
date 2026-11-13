from datetime import datetime, timedelta
import hashlib
import sqlite3
import unittest

from kiwoom_autotrader.trading.broker_observation_repeats import (
    create_repeat_schema, install_evidence_view, persist_observation)
from kiwoom_autotrader.trading.decision_evidence import canonical_json
from kiwoom_autotrader.trading.execution_log import ExecutionTracker
from kiwoom_autotrader.trading.expired_order_certificates import (
    capture_certificates, create_schema, verified_certificate)


class CapturedTracker:
    scope = 'default'

    def _paper_prior_order_terminal(self, db, order, now, **kwargs):
        rejection = kwargs.get('_rejected_auction_context')
        if rejection is not None:
            rejection['proof'] = 'captured_unchanged'
        # Model the direct original remainder-zero route independently from v2.
        return True

    def _confirmed_cancelled_quantity(self, db, order):
        return 0


class V2RepeatTests(unittest.TestCase):
    captured_at = datetime.fromisoformat('2026-11-09T15:41:00+09:00')
    now = datetime.fromisoformat('2026-11-10T15:41:00+09:00')

    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.addCleanup(self.db.close)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            CREATE TABLE broker_observations (id INTEGER PRIMARY KEY, scope, day,
                mode, observed_at, source_api, digest, payload_json);
            CREATE TABLE orders (scope, day, mode, ord_no, symbol, side, strategy,
                quantity, registered_at);
            CREATE TABLE fills (scope, day, mode, ord_no, quantity);
        """)
        create_schema(self.db)
        create_repeat_schema(self.db)
        install_evidence_view(self.db)
        self.tracker = CapturedTracker()
        self.orders = [dict(scope='default', day='20261104', mode='paper', ord_no=number,
                            symbol=symbol, side='buy', strategy='EXP_D', quantity=quantity,
                            registered_at='2026-11-04T15:22:50+09:00', filled_quantity=0)
                       for number, symbol, quantity in [('218972', '002790', 7), ('219563', '010660', 52)]]
        for order in self.orders:
            self.db.execute('INSERT INTO orders VALUES (?,?,?,?,?,?,?,?,?)', tuple(order.values())[:-1])
        self.originals = [dict(ord_no=number, ori_ord='0000000', stk_cd='A'+symbol, io_tp_nm='현금매수',
                               dmst_stex_tp='KRX', mdfy_cncl='일반', acpt_tp='주문완료', ord_qty=str(quantity),
                               cnfm_qty=str(quantity), ord_uv=str(price), cntr_qty='0', cntr_uv='0', ord_remnq=str(quantity))
                          for number, symbol, quantity, price in [('0218972', '002790', 7, 26250),
                                                                  ('0219563', '010660', 52, 3690)]]
        captured = self.page(1, at=self.captured_at-timedelta(seconds=1))
        self.captured_digest = self.insert(1, captured)
        for order in self.orders:
            self.certificate(order)

    def page(self, query, page=1, *, more=False, at=None, rows=None):
        at = at or self.now-timedelta(seconds=30-query-page)
        return dict(schema='kiwoom-broker-page-v1', source_api='kt00007', source_kind='broker_observation',
                    day='20261104', mode='paper', http_status=200, status='success',
                    query_id=f'{query:032x}', page=page,
                    request_started_at=(at-timedelta(milliseconds=10)).isoformat(), observed_at=at.isoformat(),
                    request_body=dict(ord_dt='20261104', qry_tp='1', stk_bond_tp='1', sell_tp='0',
                                      stk_cd='', fr_ord_no='', dmst_stex_tp='KRX'),
                    response_body=dict(return_code=0, acnt_ord_cntr_prps_dtl=self.originals if rows is None else rows),
                    continuation=dict(request_cont_yn='N' if page==1 else 'Y',
                                      response_cont_yn='Y' if more else 'N', next_key_present=more))

    def insert(self, ident, event, *, bad_hash=False):
        raw = canonical_json(event)
        digest = hashlib.sha256(raw.encode()).hexdigest()
        self.db.execute('INSERT INTO broker_observations VALUES (?,?,?,?,?,?,?,?)',
                        (ident, 'default', '20261104', 'paper', event['observed_at'], event['source_api'],
                         '0'*64 if bad_hash else digest, raw))
        return digest

    def certificate(self, order, version='v2'):
        proof = dict(schema='paper-expired-terminal-proof-'+version,
                     semantics='terminal_remainder_not_fill_or_fresh_account',
                     order=order, captured_at=self.captured_at.isoformat(), source_max_id=1,
                     source_hashes=[[1, self.captured_digest]])
        if version == 'v2':
            proof.update(reason='rejected_exp_d_closing_buy', approval_manifest_sha256='a'*64,
                         rejection_evidence={'proof': 'captured_unchanged'})
        raw = canonical_json(proof)
        self.db.execute("""INSERT INTO paper_expired_order_certificates
            (scope,day,ord_no,captured_at,payload_json,digest) VALUES (?,?,?,?,?,?)""",
            (order['scope'], order['day'], order['ord_no'], proof['captured_at'], raw,
             hashlib.sha256(raw.encode()).hexdigest()))

    def verify_both(self, expected):
        self.assertEqual([verified_certificate(self.tracker, self.db, order, self.now)
                          for order in self.orders], [expected, expected])

    def test_identical_after_capture_and_complete_paginated_queries_preserve_pair(self):
        # The first page can be in flight when the certificate is captured.
        self.insert(2, self.page(2, at=self.captured_at+timedelta(milliseconds=2)))
        self.insert(3, self.page(3, more=True, rows=self.originals[:1]))
        self.insert(4, self.page(3, page=2, rows=self.originals[1:]))
        self.verify_both(True)

    def test_identical_compact_reobservation_preserves_pair(self):
        for query, expected in [(2, 'original'), (3, 'repeat')]:
            event = self.page(query)
            self.assertEqual(persist_observation(self.db, 'default', event, canonical_json(event),
                                                epoch='fixture'), expected)
        self.verify_both(True)

    def test_single_order_fill_changes_both_certificates_and_blocks_direct_fallback(self):
        self.insert(2, self.page(2, rows=[dict(self.originals[0], cntr_qty='1'), self.originals[1]]))
        self.verify_both(False)
        self.assertEqual([ExecutionTracker._confirmed_expired_quantity(self.tracker, self.db, order, self.now)
                          for order in self.orders], [0, 0])

    def test_one_linked_child_invalidates_both(self):
        child = dict(self.originals[0], ord_no='0222222', ori_ord='0218972', mdfy_cncl='취소')
        self.insert(2, self.page(2, rows=[*self.originals, child]))
        self.verify_both(False)

    def test_missing_duplicate_or_changed_original_invalidates_both(self):
        for rows in (self.originals[:1], [*self.originals, self.originals[0]],
                     [self.originals[0], dict(self.originals[1], ord_uv='3700')]):
            with self.subTest(rows=rows):
                self.insert(2, self.page(2, rows=rows))
                self.verify_both(False)
                self.db.execute('DELETE FROM broker_observations WHERE id=2')

    def test_partial_or_failed_older_query_cannot_be_hidden_by_later_success(self):
        for incomplete in (self.page(2, more=True), dict(self.page(2), status='transport_error')):
            with self.subTest(incomplete=incomplete['status']):
                self.insert(2, incomplete)
                self.insert(3, self.page(3))
                self.verify_both(False)
                self.db.execute('DELETE FROM broker_observations WHERE id>1')

    def test_corrupt_future_backdated_or_non_monotone_receipt_fails_closed(self):
        cases = [
            (self.page(2), True),
            (self.page(2, at=self.now+timedelta(seconds=1)), False),
            (self.page(2, at=self.captured_at-timedelta(seconds=1)), False),
        ]
        for event, bad_hash in cases:
            with self.subTest(observed_at=event['observed_at'], bad_hash=bad_hash):
                self.insert(2, event, bad_hash=bad_hash)
                self.verify_both(False)
                self.db.execute('DELETE FROM broker_observations WHERE id=2')
        self.insert(2, self.page(2, at=self.now-timedelta(seconds=1)))
        self.insert(3, self.page(3, at=self.now-timedelta(seconds=2)))
        self.verify_both(False)

    def test_v1_on_other_order_still_rejects_later_detail(self):
        ordinary = dict(self.orders[0], ord_no='201001')
        self.certificate(ordinary, 'v1')
        self.assertTrue(verified_certificate(self.tracker, self.db, ordinary, self.now))
        self.insert(2, self.page(2))
        self.assertFalse(verified_certificate(self.tracker, self.db, ordinary, self.now))

    def test_automatic_v1_or_already_appended_v1_cannot_replace_conflicting_pair(self):
        self.insert(2, self.page(2, rows=[dict(self.originals[0], ord_remnq='0'), self.originals[1]]))
        outcome = capture_certificates(self.tracker, self.db, self.now)
        self.assertEqual(outcome['captured_orders'], [])
        self.assertEqual(set(outcome['unconfirmed_orders']), {'218972', '219563'})
        self.certificate(self.orders[0], 'v1')
        self.verify_both(False)
