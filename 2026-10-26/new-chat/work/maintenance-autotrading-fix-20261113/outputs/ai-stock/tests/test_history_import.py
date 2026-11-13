"""Synthetic canonical fixtures only; no preserved archive or real DB is read."""
from contextlib import redirect_stdout
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from decimal import Decimal
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from uuid import UUID

from kiwoom_autotrader.history import core
from kiwoom_autotrader.history.__main__ import main


def timestamp(raw, meaning, zone="Asia/Seoul"):
    return {"raw": raw, "timezone": zone, "meaning": meaning}


def fixtures():
    sources, records = [], {entity: [] for entity in core.ENTITIES}
    for index, (cohort, currency) in enumerate(core.COHORTS.items()):
        lineage = "SYNTHETIC-FIXTURE-" + cohort
        common = {"lineage_id": lineage, "cohort": cohort}
        sources.append({**common, "currency": currency, "snapshot_sha256": str(index + 1) * 64,
            "pseudonym_scheme": "synthetic-v1", "snapshot_recorded_at": timestamp("2026-10-25T11:00:00", "snapshot_saved_at"),
            "source_completeness": "partial_or_unknown", "expected_counts": {"orders": 2, "roundtrips": 1, "links": 2}})
        cost = {"cost_state": "unknown", "commission": None, "tax": None} if cohort == "kr_mock" else {"cost_state": "assumed", "commission": "0.10", "tax": "0.00"}
        for side, ref in (("BUY", "buy-1"), ("SELL", "sell-1")):
            records["orders"].append({**common, "record_ref": ref, "symbol": "005930" if currency == "KRW" else "AAPL",
                "side": side, "status": "CANCELLED" if side == "BUY" else "FILLED",
                "requested_quantity": "3.00" if side == "BUY" else "2", "filled_quantity": "2.00",
                "requested_price": "100.12345678901234567890123456789", "filled_amount": "200.0000000000000000001" if side == "BUY" else "210.0000000000000000002",
                **cost, "created_at": timestamp("2026-10-14 09:00:00", "order_created_at", None),
                "updated_at": timestamp("2026-10-14T09:01:00+09:00", "order_updated_at"),
                "actual_fill_at": None, "decision_reason": None})
        records["roundtrips"].append({**common, "record_ref": "trip-1", "symbol": "005930" if currency == "KRW" else "AAPL",
            "quantity": "2.00", "buy_amount": "200.0000000000000000001", "sell_amount": "210.0000000000000000002",
            "gross_pnl": "10.0000000000000000001", **cost, "net_pnl": None if cohort == "kr_mock" else "9.9000000000000000001",
            "opened_at": None, "closed_at": timestamp("2026-10-14T09:01:01+09:00", "ledger_closed_at"),
            "entry_reason": None, "exit_reason": None})
        for ref in ("buy-1", "sell-1"):
            records["links"].append({**common, "record_ref": "link-" + ref, "order_ref": ref,
                "roundtrip_ref": "trip-1", "allocated_quantity": "2.00"})
    return sources, records


def write_bundle(root, sources=None, records=None, *, spaced=False):
    if sources is None:
        sources, records = fixtures()
    root.mkdir(parents=True, exist_ok=True)
    files = []
    for entity, rows in records.items():
        raw = b"".join((json.dumps(row, ensure_ascii=False, separators=None if spaced else (",", ":")) + "\n").encode() for row in rows)
        (root / (entity + ".jsonl")).write_bytes(raw)
        files.append({"entity": entity, "path": entity + ".jsonl", "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw), "rows": len(rows)})
    manifest = {"schema": core.FORMAT, "bundle_kind": "synthetic_fixture", "sources": sources, "files": files}
    (root / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2 if spaced else None))
    return root


class HistoryImportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.bundle = write_bundle(self.root / "bundle")
        self.database = self.root / "historical.sqlite3"

    def stored(self, entity):
        with sqlite3.connect(self.database) as db:
            return [json.loads(row[0]) for row in db.execute(f"SELECT payload FROM historical_{entity} ORDER BY source_key,record_ref")]

    def test_validate_is_read_only_and_import_preserves_every_canonical_field(self):
        original = {path.name: path.read_bytes() for path in self.bundle.iterdir()}
        result = core.validate_bundle(self.bundle)
        self.assertFalse(result.summary["historical_import_verified"])
        self.assertFalse(self.database.exists())
        imported = core.import_bundle(self.bundle, self.database)
        self.assertEqual(imported["inserted"], {"sources": 3, "orders": 6, "roundtrips": 3, "links": 6})
        self.assertTrue(imported["roundtrip_payload_verified"])
        for entity in core.ENTITIES:
            expected = sorted(result.records[entity], key=lambda row: (row["lineage_id"], row["cohort"], row["record_ref"]))
            self.assertEqual(self.stored(entity), expected)
        self.assertEqual(original, {path.name: path.read_bytes() for path in self.bundle.iterdir()})

    def test_same_bundle_and_repack_are_idempotent_without_moving_known_at(self):
        core.import_bundle(self.bundle, self.database)
        known = [row["known_at"] for row in core.report_history(self.database)["sources"]]
        again = core.import_bundle(self.bundle, self.database)
        repacked = write_bundle(self.root / "repacked", spaced=True)
        result = core.import_bundle(repacked, self.database)
        for report in (again, result):
            self.assertEqual(report["inserted"], {"sources": 0, "orders": 0, "roundtrips": 0, "links": 0})
        self.assertEqual(known, [row["known_at"] for row in core.report_history(self.database)["sources"]])
        self.assertEqual(core.report_history(self.database)["imports"], 2)

    def test_unknown_costs_partial_cancelled_order_and_time_meaning_survive(self):
        core.import_bundle(self.bundle, self.database)
        report = core.report_history(self.database, cohort="kr_mock")
        source = report["sources"][0]
        self.assertEqual(source["partial_cancelled_orders"], 1)
        self.assertEqual(source["cost_unknown_roundtrips"], 1)
        self.assertEqual(source["cost_confirmed_roundtrips"], 0)
        self.assertEqual(source["totals"]["roundtrips"]["commission"], {"sum_of_known": "0", "known_count": 0, "null_count": 1})
        for name in ("confirmed_net_pnl", "confirmed_win_rate", "account_return", "mdd"):
            self.assertIsNone(source[name])
        self.assertFalse(source["pit_eligible"])
        order = next(row for row in self.stored("orders") if row["cohort"] == "kr_mock")
        self.assertIsNone(order["actual_fill_at"])
        self.assertIsNone(order["created_at"]["timezone"])
        self.assertEqual(order["created_at"]["raw"], "2026-10-14 09:00:00")

    def test_reports_never_mix_cohorts_currencies_or_different_lineages(self):
        core.import_bundle(self.bundle, self.database)
        source, records = fixtures()
        for row in source:
            row["lineage_id"] += "-OTHER-SNAPSHOT"
            row["snapshot_sha256"] = "a" * 64
        for rows in records.values():
            for row in rows:
                row["lineage_id"] += "-OTHER-SNAPSHOT"
        core.import_bundle(write_bundle(self.root / "other", source, records), self.database)
        report = core.report_history(self.database)
        self.assertEqual(len(report["sources"]), 6)
        self.assertEqual(report["cross_lineage_or_currency_aggregation"], "not_performed")
        self.assertEqual({row["currency"] for row in report["sources"]}, {"KRW", "USD"})
        self.assertTrue(all(row["bundle_kind"] == "synthetic_fixture" for row in report["sources"]))

    def test_same_identity_changed_value_is_quarantined_without_overwriting_facts(self):
        core.import_bundle(self.bundle, self.database)
        before = self.stored("orders")
        sources, records = fixtures()
        records["orders"][0]["requested_price"] = "999"
        changed = write_bundle(self.root / "conflict", sources, records)
        with self.assertRaises(core.HistoryConflict):
            core.import_bundle(changed, self.database)
        self.assertEqual(self.stored("orders"), before)
        self.assertEqual(core.report_history(self.database)["quarantined_conflicts"], 1)
        with self.assertRaises(core.HistoryConflict):
            core.import_bundle(changed, self.database)
        self.assertEqual(core.report_history(self.database)["quarantined_conflicts"], 1)

    def test_source_snapshot_change_cannot_redefine_an_existing_lineage(self):
        core.import_bundle(self.bundle, self.database)
        sources, records = fixtures()
        sources[0]["snapshot_sha256"] = "f" * 64
        with self.assertRaises(core.HistoryConflict):
            core.import_bundle(write_bundle(self.root / "changed-source", sources, records), self.database)

    def test_transaction_failure_rolls_back_every_new_fact_and_import_receipt(self):
        original = core._insert_record
        calls = []
        def failing(db, entity, values):
            calls.append(entity)
            if len(calls) == 2:
                raise OSError("synthetic disk failure")
            original(db, entity, values)
        with patch.object(core, "_insert_record", side_effect=failing), self.assertRaises(OSError):
            core.import_bundle(self.bundle, self.database)
        with sqlite3.connect(self.database) as db:
            for table in ("historical_sources", "historical_orders", "historical_roundtrips", "historical_links", "historical_imports"):
                self.assertEqual(db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0)
        core.import_bundle(self.bundle, self.database)

    def test_operational_unidentified_db_symlinks_and_bundle_destination_are_rejected(self):
        operating = self.root / "operating.history.sqlite3"
        with sqlite3.connect(operating) as db:
            db.execute("CREATE TABLE orders (quantity INTEGER)")
            db.execute("INSERT INTO orders VALUES (7)")
        before = operating.read_bytes()
        for action in (lambda: core.import_bundle(self.bundle, operating), lambda: core.report_history(operating)):
            with self.assertRaises(core.HistoryError): action()
            self.assertEqual(operating.read_bytes(), before)
        for path in (self.root / ".execution-ledger.sqlite3", self.bundle / "historical.sqlite3"):
            with self.assertRaises(core.HistoryError): core.import_bundle(self.bundle, path)
            self.assertFalse(path.exists())
        linked = self.root / "linked.history.sqlite3"
        linked.symlink_to(operating)
        with self.assertRaises(core.HistoryError): core.import_bundle(self.bundle, linked)
        source_link = self.root / "bundle-link"
        source_link.symlink_to(self.bundle, target_is_directory=True)
        with self.assertRaises(core.HistoryError): core.validate_bundle(source_link)
        orders = self.bundle / "orders.jsonl"
        saved = orders.read_bytes()
        orders.unlink()
        actual = self.root / "actual.jsonl"
        actual.write_bytes(saved)
        orders.symlink_to(actual)
        with self.assertRaises(core.HistoryError): core.validate_bundle(self.bundle)

    def test_readonly_report_leaves_database_bytes_unchanged_and_facts_are_immutable(self):
        core.import_bundle(self.bundle, self.database)
        before = self.database.read_bytes()
        core.report_history(self.database)
        self.assertEqual(before, self.database.read_bytes())
        with sqlite3.connect(self.database) as db:
            with self.assertRaises(sqlite3.IntegrityError): db.execute("DELETE FROM historical_orders")
            with self.assertRaises(sqlite3.IntegrityError): db.execute("UPDATE historical_orders SET payload='{}'")

    def test_manifest_hash_length_count_path_and_missing_file_fail_closed(self):
        original = json.loads((self.bundle / "manifest.json").read_text())
        for field, value in (("sha256", "0" * 64), ("bytes", 1), ("rows", 0), ("path", "../orders.jsonl")):
            manifest = deepcopy(original)
            manifest["files"][0][field] = value
            (self.bundle / "manifest.json").write_text(json.dumps(manifest))
            with self.subTest(field=field), self.assertRaises(core.HistoryError): core.import_bundle(self.bundle, self.database)
            self.assertFalse(self.database.exists())
        (self.bundle / "manifest.json").write_text(json.dumps(original))
        (self.bundle / "links.jsonl").unlink()
        with self.assertRaises(core.HistoryError): core.validate_bundle(self.bundle)

    def test_wrong_currency_ref_null_conversion_float_and_fabricated_fill_time_fail(self):
        cases = [lambda s, r: s[0].update(currency="USD"),
            lambda s, r: r["orders"][0].update(record_ref="bad/ref"),
            lambda s, r: r["links"][0].update(order_ref="missing"),
            lambda s, r: r["orders"][0].update(commission="0"),
            lambda s, r: r["orders"][0].update(filled_amount=200.0),
            lambda s, r: r["orders"][0].update(actual_fill_at="2026-10-14T09:01:00+09:00"),
            lambda s, r: r["roundtrips"][0].update(net_pnl="10"),
            lambda s, r: r["links"][0].update(allocated_quantity="3"),
            lambda s, r: r["orders"].append(deepcopy(r["orders"][0]))]
        for index, mutate in enumerate(cases):
            sources, records = fixtures()
            mutate(sources, records)
            path = write_bundle(self.root / f"bad-{index}", sources, records)
            with self.subTest(index=index), self.assertRaises(core.HistoryError): core.validate_bundle(path)

    def test_duplicate_json_keys_nonfinite_and_wrong_format_are_rejected(self):
        manifest = self.bundle / "manifest.json"
        for raw in ('{"schema":"ai-stock-history-v1","schema":"other"}', '{"value":NaN}', '{}'):
            manifest.write_text(raw)
            with self.assertRaises(core.HistoryError): core.validate_bundle(self.bundle)

    def test_original_decimal_residuals_are_reported_without_fixing_or_rounding(self):
        sources, records = fixtures()
        records["roundtrips"][0]["gross_pnl"] = "10.00000000000000000010000000001"
        core.import_bundle(write_bundle(self.root / "residual", sources, records), self.database)
        source = core.report_history(self.database, cohort="kr_mock")["sources"][0]
        residual = source["arithmetic_residuals"][0]["gross_equation_residual"]
        self.assertEqual(Decimal(residual), Decimal("-0.00000000000000000000000000001"))
        trip = next(row for row in self.stored("roundtrips") if row["cohort"] == "kr_mock")
        self.assertEqual(trip["gross_pnl"], records["roundtrips"][0]["gross_pnl"])
        self.assertEqual(source["evidence_scope"], core.EVIDENCE_SCOPE)

    def test_same_snapshot_with_renamed_reference_set_is_quarantined(self):
        core.import_bundle(self.bundle, self.database)
        sources, records = fixtures()
        for entity, rows in records.items():
            for row in rows:
                row["record_ref"] += "-renamed"
                if entity == "links":
                    row["order_ref"] += "-renamed"
                    row["roundtrip_ref"] += "-renamed"
        renamed = write_bundle(self.root / "renamed", sources, records)
        with self.assertRaises(core.HistoryConflict) as conflict:
            core.import_bundle(renamed, self.database)
        self.assertTrue(any(row["identity"].startswith("identity_set:") for row in conflict.exception.conflicts))
        report = core.report_history(self.database)
        self.assertEqual(report["imports"], 1)
        self.assertTrue(all(source["counts"] == {"orders": 2, "roundtrips": 1, "links": 2} for source in report["sources"]))

    def test_concurrent_conflicting_imports_cannot_both_commit(self):
        with core._db(self.database, write=True):
            pass
        sources, records = fixtures()
        records["orders"][0]["requested_price"] = "101"
        alternate = write_bundle(self.root / "concurrent", sources, records)
        original = core._db
        barrier = threading.Barrier(2)
        @contextmanager
        def simultaneous_open(path, **kwargs):
            with original(path, **kwargs) as db:
                barrier.wait(timeout=5)
                yield db
        def run(bundle):
            try:
                core.import_bundle(bundle, self.database)
                return "accepted"
            except core.HistoryConflict:
                return "conflict"
        with patch.object(core, "_db", simultaneous_open), ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(run, path) for path in (self.bundle, alternate)]
            self.assertEqual(sorted(future.result(timeout=10) for future in futures), ["accepted", "conflict"])
        report = core.report_history(self.database)
        self.assertEqual(report["imports"], 1)
        self.assertTrue(all(source["counts"]["orders"] == 2 for source in report["sources"]))

    def test_report_revalidates_hash_identity_links_and_declared_counts(self):
        core.import_bundle(self.bundle, self.database)
        cases = ("checksum", "identity", "link", "count")
        for case in cases:
            path = self.root / (case + ".history.sqlite3")
            shutil.copyfile(self.database, path)
            table = "historical_sources" if case == "count" else "historical_links" if case == "link" else "historical_orders"
            with sqlite3.connect(path) as db:
                trigger = table + "_no_update"
                db.execute("DROP TRIGGER " + trigger)
                if case == "checksum":
                    rowid, payload = db.execute("SELECT rowid,payload FROM historical_orders LIMIT 1").fetchone()
                    row = json.loads(payload)
                    row["filled_amount"] = "999"
                    db.execute("UPDATE historical_orders SET payload=? WHERE rowid=?", (core._canonical(row), rowid))
                elif case == "identity":
                    db.execute("UPDATE historical_orders SET record_ref=record_ref || '-changed'")
                elif case == "link":
                    rowid, payload = db.execute("SELECT rowid,payload FROM historical_links LIMIT 1").fetchone()
                    row = json.loads(payload)
                    row["order_ref"] = "does-not-exist"
                    payload = core._canonical(row)
                    db.execute("UPDATE historical_links SET payload=?,content_sha256=? WHERE rowid=?", (payload, core._hash(payload.encode()), rowid))
                else:
                    rowid, payload = db.execute("SELECT rowid,metadata FROM historical_sources LIMIT 1").fetchone()
                    row = json.loads(payload)
                    row["expected_counts"]["orders"] += 1
                    db.execute("UPDATE historical_sources SET metadata=? WHERE rowid=?", (core._canonical(row), rowid))
                db.execute(core.TRIGGER_SQL[trigger])
            before = path.read_bytes()
            with self.subTest(case=case), self.assertRaises(core.HistoryError):
                core.report_history(path)
            self.assertEqual(before, path.read_bytes())

    def test_store_identity_is_independent_stable_and_report_rejects_schema_or_journals(self):
        core.import_bundle(self.bundle, self.database)
        report = core.report_history(self.database)
        self.assertEqual(str(UUID(report["store_uuid"])), report["store_uuid"])
        self.assertTrue(report["stored_payloads_revalidated"])
        core.import_bundle(self.bundle, self.database)
        self.assertEqual(report["store_uuid"], core.report_history(self.database)["store_uuid"])
        other = self.root / "other.history.sqlite3"
        core.import_bundle(self.bundle, other)
        self.assertNotEqual(report["store_uuid"], core.report_history(other)["store_uuid"])
        for suffix in ("-wal", "-shm", "-journal"):
            journal = Path(str(self.database) + suffix)
            journal.write_bytes(b"SYNTHETIC unresolved sidecar")
            with self.assertRaises(core.HistoryError): core.report_history(self.database)
            journal.unlink()
        with sqlite3.connect(other) as db:
            db.execute("CREATE VIEW unexpected AS SELECT * FROM historical_orders")
        with self.assertRaises(core.HistoryError): core.report_history(other)
        with sqlite3.connect(self.database) as db:
            db.execute("DROP TRIGGER historical_orders_no_update")
        with self.assertRaises(core.HistoryError): core.report_history(self.database)

    def test_input_encoding_size_and_nonregular_files_fail_without_waiting(self):
        manifest = self.bundle / "manifest.json"
        original = manifest.read_text()
        manifest.write_bytes(original.encode("utf-16"))
        with self.assertRaises(core.HistoryError): core.validate_bundle(self.bundle)
        manifest.write_text(original)
        with patch.object(core, "MAX_FILE", 10), self.assertRaises(core.HistoryError):
            core.validate_bundle(self.bundle)
        if hasattr(os, "mkfifo"):
            order_file = self.bundle / "orders.jsonl"
            order_file.unlink()
            os.mkfifo(order_file)
            result = subprocess.run([sys.executable, "-m", "kiwoom_autotrader.history", "validate", "--bundle", str(self.bundle)],
                cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 2)
            self.assertIn("regular file", json.loads(result.stdout)["error"])

    def test_empty_snapshot_is_valid_without_inventing_zero_confirmed_profit(self):
        sources, _ = fixtures()
        sources = sources[:1]
        sources[0]["expected_counts"] = {entity: 0 for entity in core.ENTITIES}
        bundle = write_bundle(self.root / "empty", sources, {entity: [] for entity in core.ENTITIES})
        core.import_bundle(bundle, self.database)
        source = core.report_history(self.database)["sources"][0]
        self.assertEqual(source["counts"], {"orders": 0, "roundtrips": 0, "links": 0})
        self.assertIsNone(source["confirmed_net_pnl"])

    def test_cli_runs_offline_and_never_exposes_confirmed_completeness(self):
        with patch("socket.socket.connect", side_effect=AssertionError("No network")) as network:
            for command in (["validate", "--bundle", str(self.bundle)],
                            ["import", "--bundle", str(self.bundle), "--database", str(self.database)],
                            ["report", "--database", str(self.database), "--cohort", "us_shadow"]):
                output = io.StringIO()
                with redirect_stdout(output): self.assertEqual(main(command), 0)
                result = json.loads(output.getvalue())
                self.assertFalse(result["authoritative_history_complete"])
                self.assertFalse(result["current_account_verified"])
                self.assertFalse(result["deployed"])
            network.assert_not_called()


if __name__ == "__main__":
    unittest.main()
