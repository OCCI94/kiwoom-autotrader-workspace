"""Actual monitor producer, durable boundaries and non-interfering telemetry."""
from copy import deepcopy
import sqlite3,unittest
from tests import test_paper_experiment_monitor as fixture


class EvaluationReceiptTests(unittest.TestCase):
    def case(self):
        case=fixture.MonitorTests('runTest');case.setUp();self.addCleanup(case.doCleanups);return case

    def rows(self,case,prefix='evaluated:'):
        with sqlite3.connect(case.path) as db:
            return db.execute('SELECT kind,payload FROM selections WHERE kind LIKE ?',(prefix+'%',)).fetchall()

    def test_actual_native_evaluators_record_once_per_boundary_and_keep_arm_identity(self):
        for arm in ('EXP_R','EXP_A','EXP_B','EXP_B_TP','EXP_E'):
            with self.subTest(arm=arm):
                c=self.case();value=c.seed(fixture.assignment(arm));c.one(value);c.one(value)
                rows=self.rows(c);self.assertEqual(len(rows),1)
                import json
                payload=json.loads(rows[0][1]);self.assertEqual(payload['arm'],arm)
                self.assertEqual(payload['evaluation_role'],'entry');self.assertFalse(payload['order_permission'])
                self.assertEqual(payload['assignment_sha256'],value['assignment_sha256'])

    def test_input_to_technical_same_boundary_preserves_both_receipts_and_exit_separate(self):
        c=self.case();value=c.seed(fixture.assignment('EXP_A'))
        c.monitor._record_evaluation(value,{'reason':'entry_evidence_unverified','signal':False},c.clock)
        c.one(value)
        self.assertEqual(len(self.rows(c)),2)
        c.owned(value);c.clock=c.clock.replace(hour=15,minute=15);c.one(value)
        self.assertEqual(len(self.rows(c,'evaluated_exit:')),1)

    def test_receipt_write_failure_does_not_change_order_decision_or_claim(self):
        c=self.case();value=c.seed(fixture.assignment('EXP_A'));original=c.monitor.store.selection
        def fail_metadata(day,kind,value=None):
            if kind.startswith(('evaluated:','evaluated_exit:')):raise OSError('private-fixture')
            return original(day,kind,value)
        c.monitor.store.selection=fail_metadata;c.one(value)
        self.assertEqual(len(c.orders),1)
        self.assertEqual(c.monitor.status()['evaluation_receipt_error']['reason'],'OSError')
        self.assertEqual(self.rows(c),[])
