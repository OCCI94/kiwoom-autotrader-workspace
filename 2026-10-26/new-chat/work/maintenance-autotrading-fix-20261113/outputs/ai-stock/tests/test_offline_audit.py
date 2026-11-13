"""Read-only diagnostics use temporary synthetic files and no network/keys."""
from contextlib import redirect_stderr, redirect_stdout
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from kiwoom_autotrader.diagnostics import audit_local, AuditReport, Check, Verdict
from kiwoom_autotrader.diagnostics import audit
from kiwoom_autotrader.diagnostics.__main__ import main
from kiwoom_autotrader.diagnostics.render import render_html


class OfflineAuditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.ledger = self.root / "ledger.sqlite3"
        self.csv = self.root / "20261026.csv"
        self.state = self.root / "state.json"
        with sqlite3.connect(self.ledger) as db:
            db.executescript("""
                CREATE TABLE intents(scope,id,day,mode,side,symbol,quantity,strategy,order_number,state);
                CREATE TABLE orders(scope,day,mode,ord_no,symbol,side,strategy,quantity);
                CREATE TABLE fills(scope,day,mode,ord_no,fill_no,symbol,side,moment,price,quantity,strategy);
                CREATE TABLE profits(scope,day,mode,symbol,net,basis);
                INSERT INTO intents VALUES('default','i1','20261026','paper','buy','005930',5,'gdcrs','1','accepted');
                INSERT INTO intents VALUES('default','i2','20261026','paper','sell','005930',2,'ddcrs','2','accepted');
                INSERT INTO orders VALUES('default','20261026','paper','1','005930','buy','gdcrs',5);
                INSERT INTO orders VALUES('default','20261026','paper','2','005930','sell','ddcrs',2);
                INSERT INTO fills VALUES('default','20261026','paper','1','1','005930','buy','10:00:00','100',2,'gdcrs');
                INSERT INTO fills VALUES('default','20261026','paper','1','2','005930','buy','10:01:00','110',3,'gdcrs');
                INSERT INTO fills VALUES('default','20261026','paper','2','1','005930','sell','10:02:00','120',2,'ddcrs');
                INSERT INTO profits VALUES('default','20261026','paper','005930','30','210');
            """)
        self.rows = [
            {"체결시간": "20261026 10:00:00", "투자구분": "paper", "종목코드": "005930", "주문번호": "0000001", "체결번호": "0000001", "매수매도": "매수", "가격": "100", "수량": "2", "전략": "gdcrs", "행구분": "체결"},
            {"체결시간": "20261026 10:01:00", "투자구분": "paper", "종목코드": "005930", "주문번호": "0000001", "체결번호": "0000002", "매수매도": "매수", "가격": "110", "수량": "3", "전략": "gdcrs", "행구분": "체결"},
            {"체결시간": "20261026 10:02:00", "투자구분": "paper", "종목코드": "005930", "주문번호": "0000002", "체결번호": "0000001", "매수매도": "매도", "가격": "120", "수량": "2", "전략": "ddcrs", "행구분": "체결"},
            {"행구분": "일일합계", "투자구분": "paper", "매수합계": "530", "매도합계": "240"},
        ]
        self.write_csv()
        self.state.write_text(json.dumps({"version": 1, "status": "running", "pid": 123,
            "poll_count": 10, "received_count": 3, "replied_count": 3,
            "token": "secret-offline-token", "account": "private-account-987", "error": "private-remote-error"}), encoding="utf-8")
        guard = patch("socket.socket.connect", side_effect=AssertionError("network forbidden"))
        guard.start()
        self.addCleanup(guard.stop)

    def write_csv(self):
        with self.csv.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=sorted(audit._CSV_FIELDS))
            writer.writeheader()
            writer.writerows(self.rows)

    def result(self):
        return audit_local(ledger_path=self.ledger, csv_path=self.csv, state_path=self.state).to_dict()

    def check(self, name, result=None):
        return next(row for row in (result or self.result())["checks"] if row["name"] == name)

    def test_complete_record_checks_do_not_claim_live_readiness(self):
        result = self.result()
        self.assertTrue(result["inspection_completed"])
        self.assertEqual(result["readiness_state"], "UNKNOWN")
        self.assertFalse(result["account_reconciled"])
        for name in ("ledger_attribution", "ledger_fill_quantity", "ledger_cost_coverage", "csv_ledger", "csv_summaries"):
            self.assertEqual(self.check(name, result)["verdict"], "PASS")
        counts = self.check("ledger_records", result)["statistics"]
        self.assertEqual((counts["accepted_orders"], counts["fill_rows"], counts["fully_filled_orders"]), (2, 3, 2))
        self.assertEqual(self.check("runtime_records", result)["verdict"], "OBSERVE")

    def test_sources_are_byte_identical_and_sqlite_uses_readonly_snapshot(self):
        paths = (self.ledger, self.csv, self.state)
        before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in paths}
        entries = set(self.root.iterdir())
        original = sqlite3.connect
        connections, statements = [], []
        def connect(address, **kwargs):
            self.assertIn("?mode=ro", address)
            self.assertTrue(kwargs["uri"])
            self.assertNotIn(str(self.ledger), address)
            connection = original(address, **kwargs)
            connection.set_trace_callback(statements.append)
            connections.append(connection)
            return connection
        with patch.object(audit.sqlite3, "connect", side_effect=connect):
            result = self.result()
        self.assertEqual(set(self.root.iterdir()), entries)
        for path in paths:
            self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before[path])
        self.assertIn("PRAGMA query_only=ON", statements)
        with self.assertRaises(sqlite3.ProgrammingError):
            connections[0].execute("SELECT 1")
        self.assertEqual(result["sources"][0]["sha256"], hashlib.sha256(before[self.ledger][0]).hexdigest())

    def test_missing_inputs_are_unknown_and_never_created(self):
        missing = self.root / "missing" / "ledger.sqlite3"
        result = audit_local(ledger_path=missing).to_dict()
        self.assertFalse(missing.parent.exists())
        self.assertEqual(self.check("ledger_source", result)["reason_code"], "SOURCE_MISSING")
        self.assertEqual(result["sources"], [])

    def test_active_wal_or_journal_is_unknown_and_preserved(self):
        for suffix in ("-wal", "-journal"):
            with self.subTest(suffix=suffix):
                sidecar = Path(str(self.ledger) + suffix)
                sidecar.write_bytes(b"synthetic-active-journal")
                result = self.result()
                self.assertEqual(self.check("ledger_source", result)["reason_code"], "LEDGER_ACTIVE_JOURNAL_SNAPSHOT_UNKNOWN")
                self.assertEqual(sidecar.read_bytes(), b"synthetic-active-journal")
                sidecar.unlink()

    def test_corrupt_or_unsupported_database_is_unknown_without_repair(self):
        for raw in (b"private-invalid-database", b""):
            self.ledger.write_bytes(raw)
            result = self.result()
            self.assertEqual(self.check("ledger_source", result)["verdict"], "UNKNOWN")
            self.assertEqual(self.ledger.read_bytes(), raw)
            self.assertNotIn("private-invalid", json.dumps(result))

    def test_overfill_and_wrong_strategy_are_reported(self):
        with sqlite3.connect(self.ledger) as db:
            db.execute("UPDATE fills SET quantity=6,strategy='wrong-secret-strategy' WHERE ord_no='1' AND fill_no='2'")
        result = self.result()
        self.assertEqual(self.check("ledger_fill_quantity", result)["reason_code"], "ORDER_OVERFILL")
        self.assertEqual(self.check("ledger_attribution", result)["verdict"], "NOT_MET")
        self.assertNotIn("wrong-secret-strategy", json.dumps(result))

    def test_partial_accepted_unfilled_and_unresolved_intents_are_distinct(self):
        with sqlite3.connect(self.ledger) as db:
            db.execute("DELETE FROM fills WHERE ord_no='1' AND fill_no='2'")
            db.execute("DELETE FROM fills WHERE ord_no='2'")
            db.execute("INSERT INTO intents VALUES('default','unknown','20261026','paper','buy','005930',1,'gdcrs',NULL,'prepared')")
            db.execute("INSERT INTO intents VALUES('default','cancelled','20261026','paper','buy','005930',1,'gdcrs',NULL,'cancelled')")
        result = self.result()
        counts = self.check("ledger_records", result)["statistics"]
        self.assertEqual(counts["partially_filled_orders"], 1)
        self.assertEqual(counts["accepted_without_recorded_fills"], 1)
        self.assertEqual(counts["cancelled_intents"], 1)
        self.assertEqual(self.check("ledger_ambiguous_intents", result)["statistics"]["unresolved_intents"], 1)

    def test_missing_costs_are_unknown_not_zero(self):
        with sqlite3.connect(self.ledger) as db:
            db.execute("UPDATE profits SET net=NULL,basis=NULL")
        check = self.check("ledger_cost_coverage")
        self.assertEqual(check["verdict"], "UNKNOWN")
        self.assertEqual(check["statistics"]["cost_unknown_groups"], 1)

    def test_old_ledgers_do_not_invent_decision_or_cost_evidence(self):
        result = self.result()
        self.assertEqual(self.check("decision_evidence", result)["reason_code"], "HISTORICAL_DECISION_EVIDENCE_UNAVAILABLE")
        self.assertEqual(self.check("cost_evidence", result)["reason_code"], "HISTORICAL_COST_EVIDENCE_UNAVAILABLE")
        with sqlite3.connect(self.ledger) as db:
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='order_decisions'").fetchone())

    def test_optional_evidence_hashes_and_coverage_do_not_expose_payloads(self):
        first = json.dumps({"schema_version": 1, "source_kind": "synthetic", "strategy": "gdcrs", "private": "private-evidence-content"})
        second = json.dumps({"schema_version": 1, "source_kind": "synthetic", "strategy": "ddcrs"})
        cost = json.dumps({"schema_version": 1, "day": "20261026", "symbol": "005930", "source_kind": "synthetic", "net": "30", "basis": "210"})
        digest = lambda value: hashlib.sha256(value.encode()).hexdigest()
        with sqlite3.connect(self.ledger) as db:
            db.executescript("""
                CREATE TABLE order_decisions(scope,intent_id,recorded_at,order_price,order_type,evidence_json,evidence_sha256);
                CREATE TABLE profit_observations(scope,day,mode,symbol,digest,observed_at,source_kind,payload_json);
            """)
            db.execute("INSERT INTO order_decisions VALUES('default','i1','2026-10-26T10:00:00+09:00','100','limit',?,?)", (first, digest(first)))
            db.execute("INSERT INTO order_decisions VALUES('default','i2','2026-10-26T10:00:00+09:00',NULL,'market',?,?)", (second, digest(second)))
            db.execute("INSERT INTO profit_observations VALUES('default','20261026','paper','005930',?,'2026-10-26T10:00:00+09:00','synthetic',?)", (digest(cost), cost))
        result = self.result()
        self.assertEqual(self.check("decision_evidence", result)["reason_code"], "RECORDED_DECISION_HASHES_MATCH")
        self.assertEqual(self.check("cost_evidence", result)["reason_code"], "RECORDED_COST_HASHES_MATCH")
        self.assertEqual(self.check("decision_evidence", result)["statistics"]["synthetic_rows"], 2)
        self.assertNotIn("private-evidence-content", json.dumps(result))
        self.assertEqual(result["readiness_state"], "UNKNOWN")
        with sqlite3.connect(self.ledger) as db:
            db.execute("UPDATE order_decisions SET evidence_json='{}' WHERE intent_id='i1'")
            db.execute("UPDATE profit_observations SET digest=?", ("0" * 64,))
        result = self.result()
        self.assertEqual(self.check("decision_evidence", result)["verdict"], "NOT_MET")
        self.assertEqual(self.check("cost_evidence", result)["verdict"], "NOT_MET")

    def test_view_and_projection_limit_are_rejected_before_unbounded_reads(self):
        with patch.object(audit, "_MAX_PROJECTION_BYTES", 1):
            self.assertEqual(self.check("ledger_source")["reason_code"], "LEDGER_PROJECTION_LIMIT")
        with sqlite3.connect(self.ledger) as db:
            db.execute("ALTER TABLE orders RENAME TO original_orders")
            db.execute("CREATE VIEW orders AS SELECT * FROM original_orders")
        self.assertEqual(self.check("ledger_source")["reason_code"], "LEDGER_SCHEMA_UNSUPPORTED")

    def test_csv_duplicates_amount_tampering_and_summary_mismatch_are_detected(self):
        self.rows.insert(0, dict(self.rows[0]))
        self.rows[1]["가격"] = "999"
        self.write_csv()
        result = self.result()
        self.assertEqual(self.check("csv_records", result)["reason_code"], "CSV_DUPLICATE_FILL_IDS")
        self.assertEqual(self.check("csv_summaries", result)["reason_code"], "CSV_SUMS_MISMATCH")
        self.assertEqual(self.check("csv_ledger", result)["reason_code"], "CSV_LEDGER_MISMATCH")

    def test_large_csv_principal_sum_detects_a_one_won_difference_exactly(self):
        self.rows = [dict(self.rows[0]), {"행구분": "일일합계", "투자구분": "paper", "매도합계": "0"}]
        self.rows[0].update(가격="99999999999999999999", 수량="999999999999")
        exact = 99999999999999999999 * 999999999999
        self.rows[1]["매수합계"] = str(exact - 1)
        self.write_csv()
        self.assertEqual(self.check("csv_summaries")["reason_code"], "CSV_SUMS_MISMATCH")
        self.rows[1]["매수합계"] = str(exact)
        self.write_csv()
        self.assertEqual(self.check("csv_summaries")["reason_code"], "CSV_SUMS_MATCH_RECORDED_FILLS")

    def test_csv_missing_rows_and_ambiguous_account_scope(self):
        self.rows = []
        self.write_csv()
        self.assertEqual(self.check("csv_ledger")["statistics"]["missing_rows"], 3)
        with sqlite3.connect(self.ledger) as db:
            db.execute("INSERT INTO fills SELECT 'other',day,mode,ord_no,fill_no,symbol,side,moment,price,quantity,strategy FROM fills LIMIT 1")
        self.assertEqual(self.check("csv_ledger")["reason_code"], "CSV_ACCOUNT_SCOPE_AMBIGUOUS")

    def test_state_allowlist_never_exports_tokens_account_paths_or_source_rows(self):
        output = json.dumps(self.result())
        for forbidden in ("secret-offline-token", "private-account-987", "private-remote-error", "005930", str(self.root)):
            self.assertNotIn(forbidden, output)
        self.state.write_text('{"version":1,"status":"running","poll_count":"secret"}', encoding="utf-8")
        self.assertEqual(self.check("state_source")["verdict"], "UNKNOWN")

    def test_source_changes_during_inspection_are_not_reported_stable(self):
        original = audit._read
        counts = {}
        def read(path, kind):
            counts[kind] = counts.get(kind, 0) + 1
            raw = original(path, kind)
            return raw + b" " if kind == "state" and counts[kind] == 2 else raw
        with patch.object(audit, "_read", side_effect=read):
            result = self.result()
        self.assertEqual(self.check("state_snapshot", result)["reason_code"], "SOURCE_CHANGED_DURING_INSPECTION")

    def test_symlink_and_oversized_source_rejected(self):
        link = self.root / "link.json"
        link.symlink_to(self.state)
        result = audit_local(state_path=link).to_dict()
        self.assertEqual(self.check("state_source", result)["reason_code"], "SOURCE_SYMLINK_REJECTED")
        self.state.write_bytes(b"x" * (audit._LIMITS["state"] + 1))
        self.assertEqual(self.check("state_source")["reason_code"], "SOURCE_SIZE_LIMIT")

    @unittest.skipUnless(hasattr(os, "mkfifo") and hasattr(os, "O_NONBLOCK"), "POSIX FIFO regression")
    def test_fifo_input_is_rejected_without_waiting_for_a_writer(self):
        fifo = self.root / "state.fifo"
        os.mkfifo(fifo, 0o600)
        before = set(self.root.iterdir())
        # A subprocess timeout makes a missing O_NONBLOCK fail the test instead
        # of hanging the whole suite while open() waits for a pipe writer.
        completed = subprocess.run(
            [sys.executable, "-m", "kiwoom_autotrader.diagnostics", "--state", str(fifo)],
            cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=3,
        )
        self.assertEqual(completed.returncode, 0)
        result = json.loads(completed.stdout)
        self.assertEqual(self.check("state_source", result)["reason_code"], "SOURCE_NOT_REGULAR_FILE")
        self.assertEqual(result["readiness_state"], "UNKNOWN")
        self.assertTrue(stat.S_ISFIFO(fifo.stat().st_mode))
        self.assertEqual(set(self.root.iterdir()), before)

    def test_cli_creates_json_and_static_korean_html_without_overwriting(self):
        output, html = self.root / "audit.json", self.root / "audit.html"
        args = ["--ledger", str(self.ledger), "--csv", str(self.csv), "--state", str(self.state),
                "--output", str(output), "--html", str(html)]
        self.assertEqual(main(args), 0)
        self.assertEqual(json.loads(output.read_text())["readiness_state"], "UNKNOWN")
        text = html.read_text()
        self.assertIn("현재 매매 준비 상태: 미확인", text)
        for forbidden in ("<script", "https://", "http://", "secret-offline-token", "private-account-987"):
            self.assertNotIn(forbidden, text)
        original = output.read_bytes(), html.read_bytes()
        with redirect_stderr(io.StringIO()):
            self.assertEqual(main(args), 2)
        self.assertEqual((output.read_bytes(), html.read_bytes()), original)

    def test_cli_refuses_source_sidecar_and_duplicate_output_targets(self):
        for args in (["--ledger", str(self.ledger), "--output", str(self.ledger)],
                     ["--ledger", str(self.ledger), "--output", str(self.ledger) + "-wal"],
                     ["--output", str(self.root / "same"), "--html", str(self.root / "same")]):
            with self.subTest(args=args), redirect_stderr(io.StringIO()):
                self.assertEqual(main(args), 2)
        self.assertFalse(Path(str(self.ledger) + "-wal").exists())
        self.assertFalse((self.root / "same").exists())

    def test_empty_cli_and_html_escape_are_explicit(self):
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(main([]), 0)
        self.assertEqual(json.loads(output.getvalue())["readiness_state"], "UNKNOWN")
        report = AuditReport("<unsafe>", (Check("<script>alert(1)</script>", Verdict.UNKNOWN, "<reason>"),))
        html = render_html(report)
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)
        self.assertIn("읽을 수 있는 자료가 없습니다", html)


if __name__ == "__main__":
    unittest.main()
