"""Offline tests for the optional, paper-only journal account projection."""
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from kiwoom_autotrader.api.session import KST
from kiwoom_autotrader.trading.execution_log import ExecutionTracker, _paper_balance_snapshot


def account():
    return {"mode": "paper", "exchange": "KRX", "snapshot_changed": False,
        "pages": {"kt00001": 1, "kt00018": 2},
        "cash": {"entr": "1000000", "ord_alow_amt": "950000", "password": "secret"},
        "totals": {"tot_pur_amt": "500000", "tot_evlt_amt": "510000", "tot_prft_rt": "+2.00"},
        "holdings": [{"stk_cd": "A005930", "stk_nm": "삼성전자", "crd_tp": "00", "rmnd_qty": "5",
                      "cur_prc": "+70000", "pur_amt": "345000", "evlt_amt": "350000", "token": "secret"}],
        "account_number": "1234567890", "token": "secret"}


class PaperBalanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.tracker = ExecutionTracker(log_dir=Path(self.temp.name))
        self.at = datetime(2026, 10, 28, 13, 42, tzinfo=KST)

    def rows(self):
        with sqlite3.connect(self.tracker.path) as db:
            return db.execute("SELECT scope,mode,day,observed_at,source_kind,payload_json FROM account_balance_daily").fetchall()

    def test_latest_without_disclosing_raw_or_secret_fields(self):
        self.tracker._save_paper_balance_snapshot("paper", account(), self.at)
        self.tracker._save_paper_balance_snapshot("paper", account(), self.at + timedelta(seconds=10))
        stale = account()
        stale["cash"]["entr"] = "10"
        self.tracker._save_paper_balance_snapshot("paper", stale, self.at - timedelta(seconds=1))
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        scope, mode, day, observed_at, source, raw = rows[0]
        self.assertEqual((scope, mode, day, source), ("default", "paper", "20261028", "broker_observation"))
        self.assertEqual(observed_at, (self.at + timedelta(seconds=10)).isoformat())
        self.assertNotIn("secret", raw)
        self.assertNotIn("1234567890", raw)
        payload = json.loads(raw)
        self.assertEqual(payload["cash"]["entr"], "1000000")
        self.assertEqual(payload["holdings"][0]["cur_prc"], "+70000")
        self.assertIsNone(payload["totals"]["tot_evlt_pl"])
        self.assertEqual(payload["pages"]["kt00018"], 2)

    def test_distinct_day_and_scope(self):
        self.tracker._save_paper_balance_snapshot("paper", account(), self.at)
        self.tracker._save_paper_balance_snapshot("paper", account(), self.at + timedelta(days=1))
        other = ExecutionTracker(log_dir=Path(self.temp.name), account_scope="other")
        other._save_paper_balance_snapshot("paper", account(), self.at)
        self.assertEqual(len(self.rows()), 3)

    def test_refuses_real_injected_incomplete_and_malformed(self):
        self.tracker._save_paper_balance_snapshot("real", account(), self.at)
        test_tracker = ExecutionTracker(log_dir=Path(self.temp.name), account_fetcher=lambda _: account())
        test_tracker._save_paper_balance_snapshot("paper", account(), self.at)
        for mutate in (lambda x: x.update(snapshot_changed=True), lambda x: x.update(holdings=None),
                       lambda x: x.update(pages={"kt00001": 1}), lambda x: x["cash"].update(entr="bad 123"),
                       lambda x: x["holdings"][0].update(stk_cd="1234567890"),
                       lambda x: x["holdings"][0].update(stk_nm="1234567890")):
            data = account()
            mutate(data)
            self.assertIsNone(_paper_balance_snapshot(data))
            self.tracker._save_paper_balance_snapshot("paper", data, self.at)
        self.assertFalse(self.tracker.path.exists())

    def test_missing_numeric_and_empty_holdings(self):
        data = account()
        data["holdings"] = []
        result = _paper_balance_snapshot(data)
        self.assertEqual(result["holdings"], [])
        self.assertIsNone(result["cash"]["d2_entra"])

    def test_periodic_account_fetch_records_only_unchanged_session(self):
        tracker = self.tracker
        tracker.now = lambda: self.at
        tracker.account_fetcher = lambda _session: account()
        tracker._identity = ("paper", "session")
        tracker._tasks.append(("holdings", "20261028", None))
        with patch("kiwoom_autotrader.trading.execution_log.session_identity", return_value=tracker._identity):
            tracker.tick(object(), lambda _: None)
        self.assertEqual(len(self.rows()), 1)

    def test_changed_session_between_report_and_record_does_not_record(self):
        tracker = self.tracker
        tracker.now = lambda: self.at
        tracker.account_fetcher = lambda _session: account()
        tracker._identity = ("paper", "session")
        tracker._tasks.append(("holdings", "20261028", None))
        with patch("kiwoom_autotrader.trading.execution_log.session_identity",
                   side_effect=[tracker._identity, tracker._identity, ("real", "other")]):
            tracker.tick(object(), lambda _: None)
        self.assertEqual(self.rows(), [])


if __name__ == "__main__":
    unittest.main()
