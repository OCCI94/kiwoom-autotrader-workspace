"""Offline staging tests; no mounted volume, broker login or live orders."""
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

from kiwoom_autotrader.api import orders
from kiwoom_autotrader.utils.external_order_guard import ExternalOrderGuard
from kiwoom_autotrader.utils.external_storage import ExternalStorageUnavailable
from tests.test_orders import fake_session


class ExternalOrderGuardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.ledger = self.root / 'ledger.sqlite3'
        with sqlite3.connect(self.ledger) as db:
            for table in ('orders', 'fills', 'intents', 'broker_observations'):
                db.execute(f'CREATE TABLE {table}(id INTEGER)')
        self.paths = type('Paths', (), {'data_root': self.root})()
        self.volume = Mock()
        self.volume.require_available.return_value = self.paths
        self.stream = Mock(return_value=True)
        self.gate = ExternalOrderGuard(lambda: self.volume, 'ledger.sqlite3', self.stream)

    def test_disarmed_then_explicit_reconciliation_and_failure_latch(self):
        with self.assertRaises(ExternalStorageUnavailable): self.gate()
        with self.assertRaises(ExternalStorageUnavailable): self.gate.arm(lambda: False)
        self.gate.arm(lambda: True)
        self.gate()
        self.stream.return_value = False
        with self.assertRaises(ExternalStorageUnavailable): self.gate()
        self.stream.return_value = True
        with self.assertRaises(ExternalStorageUnavailable): self.gate()
        self.gate.arm(lambda: True)
        self.gate.fail()
        with self.assertRaises(ExternalStorageUnavailable): self.gate()

    def test_absent_replaced_or_invalid_ledger_never_arms(self):
        self.ledger.unlink()
        with self.assertRaises(ExternalStorageUnavailable): self.gate.arm(lambda: True)
        self.assertFalse(self.ledger.exists())
        with sqlite3.connect(self.ledger) as db: db.execute('CREATE TABLE unrelated(id INTEGER)')
        with self.assertRaises(ExternalStorageUnavailable): self.gate.arm(lambda: True)

    def test_ledger_replacement_after_reconciliation_latches_failure(self):
        self.gate.arm(lambda: True)
        self.ledger.rename(self.root / 'original.sqlite3')
        with sqlite3.connect(self.ledger) as db:
            for table in ('orders', 'fills', 'intents', 'broker_observations'):
                db.execute(f'CREATE TABLE {table}(id INTEGER)')
        with self.assertRaises(ExternalStorageUnavailable): self.gate()

    def test_optional_gate_blocks_preparation_and_queue_dispatch(self):
        session = fake_session()
        session.external_storage_required = True
        session.external_storage_guard = self.gate
        with patch.object(orders, 'prepare_tracking') as prepare, patch.object(orders.urllib.request, 'build_opener') as opener:
            with self.assertRaises(orders.OrderError): orders.submit_order(session, 'buy', '005930', 1)
            prepare.assert_not_called(); opener.assert_not_called()
        self.gate.arm(lambda: True)
        # Fail after preparation, before the shared queue's dispatch callback.
        with patch.object(orders.urllib.request, 'build_opener') as factory:
            opener = factory.return_value
            def fail_while_waiting(*args, before_send, **kwargs):
                self.gate.fail()
                try: before_send()
                except Exception: raise orders.HTTPNotSentError('guard rejected') from None
                raise AssertionError('should never send')
            with patch.object(orders, 'queued_open', side_effect=fail_while_waiting):
                with self.assertRaises(orders.OrderError) as error:
                    orders.submit_order(session, 'buy', '005930', 1)
            self.assertFalse(error.exception.uncertain)
            opener.open.assert_not_called()

    def test_no_flag_preserves_existing_orders_and_bad_flag_blocks(self):
        session = fake_session()
        with patch.object(orders.urllib.request, 'build_opener') as factory:
            response = factory.return_value.open.return_value.__enter__.return_value
            response.read.return_value = json.dumps({'return_code': 0, 'ord_no': '1'}).encode()
            self.assertEqual(orders.submit_order(session, 'buy', '005930', 1).order_number, '1')
        session.external_storage_required = 'yes'
        with patch.object(orders.urllib.request, 'build_opener') as factory:
            with self.assertRaises(orders.OrderError): orders.submit_order(session, 'buy', '005930', 1)
            factory.assert_not_called()


if __name__ == '__main__': unittest.main()
