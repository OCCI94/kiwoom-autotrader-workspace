"""Synthetic research inputs; no broker, message or historical account access."""
import contextlib
from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import tempfile
import unittest

from kiwoom_autotrader.research.contracts import (
    CandleEvidence, LabelledSample, audit_manifest, fingerprint_files,
    freeze_protocol, walk_forward_splits, load_json_file,
)
from kiwoom_autotrader.research.__main__ import main
from kiwoom_autotrader.main import demo_bars, load_config, save_results
from kiwoom_autotrader.trading.simulation import simulate

T = datetime(2026, 1, 5, 0, 0, tzinfo=timezone.utc)


class ResearchContractsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "synthetic.csv").write_text("synthetic_only\n1\n", encoding="utf-8")
        self.spec = {
            "protocol_id": "synthetic-v1", "hypothesis": "synthetic fixture correctness",
            "as_of": T.isoformat(), "market": "KR", "currency": "KRW",
            "universe": "synthetic instruments only", "selection": "fixed fixture",
            "planned_trials": 1, "cost_assumptions": "5bp per side, assumed",
            "limitations": ["Not account data; no historical availability proof"],
        }
        self.output = self.root / "manifest.json"

    def freeze(self):
        return freeze_protocol(self.spec, self.root, ["synthetic.csv"], self.output, now=T + timedelta(days=1))

    def test_hash_integrity_is_separate_from_profitability_and_pit(self):
        self.freeze()
        report = audit_manifest(self.output, self.root)
        self.assertTrue(report["source_integrity"])
        self.assertTrue(report["retrospective_protocol"])
        self.assertFalse(report["profitability_verified"])
        self.assertFalse(report["point_in_time_verified"])
        self.assertEqual(report["readiness_state"], "UNKNOWN")

    def test_modified_file_is_not_rebaselined(self):
        self.freeze()
        before = self.output.read_bytes()
        (self.root / "synthetic.csv").write_text("tampered", encoding="utf-8")
        report = audit_manifest(self.output, self.root)
        self.assertFalse(report["source_integrity"])
        self.assertEqual(self.output.read_bytes(), before)

    def test_manifest_and_protocol_tampering_detected(self):
        value = self.freeze()
        value["protocol"]["planned_trials"] = 10
        self.output.write_text(json.dumps(value))
        self.assertFalse(audit_manifest(self.output, self.root)["checks"]["protocol_hash_matches"])

    def test_backdated_metadata_is_not_a_valid_preregistration(self):
        value = self.freeze()
        value["frozen_at"] = (T - timedelta(days=1)).isoformat()
        self.output.write_text(json.dumps(value))
        report = audit_manifest(self.output, self.root)
        self.assertFalse(report["source_integrity"])
        self.assertIsNone(report["retrospective_protocol"])

    def test_unsafe_flags_and_top_level_fields_are_rejected(self):
        original = self.freeze()
        for changes in ({"automatic_orders": True}, {"profitability_verified": True}, {"additional": 1}):
            self.output.write_text(json.dumps({**original, **changes}))
            with self.assertRaises(ValueError):
                audit_manifest(self.output, self.root)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO is not available on this platform")
    def test_fifo_inputs_rejected_without_waiting_for_a_writer(self):
        fifo = self.root / "fifo.json"
        os.mkfifo(fifo)
        with self.assertRaises(ValueError):
            load_json_file(fifo)
        with self.assertRaises(ValueError):
            audit_manifest(fifo, self.root)

    def test_oversized_json_duplicate_keys_and_nonfinite_rejected(self):
        for content in ('{"a":1,"a":2}', '{"a":NaN}', ' ' * 33):
            self.output.write_text(content)
            with self.assertRaises(ValueError):
                load_json_file(self.output, 32)

    def test_exclusive_freeze_preserves_previous_run(self):
        self.freeze()
        before = self.output.read_bytes()
        with self.assertRaises(FileExistsError):
            self.freeze()
        self.assertEqual(self.output.read_bytes(), before)

    def test_missing_and_unsafe_paths_rejected(self):
        for value in ("../else.csv", "/etc/passwd", "missing.csv", r"..\else.csv"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                fingerprint_files(self.root, [value])

    def test_symlink_and_duplicate_paths_rejected(self):
        (self.root / "link.csv").symlink_to(self.root / "synthetic.csv")
        for paths in (["link.csv"], ["synthetic.csv", "./synthetic.csv"], ["synthetic.csv", "synthetic.csv"]):
            with self.subTest(paths=paths), self.assertRaises(ValueError):
                fingerprint_files(self.root, paths)

    def test_missing_timezone_currency_unknown_fields_and_bool_rejected(self):
        for changes in ({"as_of": "2026-01-05"}, {"currency": "USD"}, {"planned_trials": True}, {"secret": "do-not-copy"}, {"limitations": []}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                freeze_protocol({**self.spec, **changes}, self.root, ["synthetic.csv"], self.output)
        self.assertFalse(self.output.exists())

    def test_cli_audit_fails_on_missing_or_modified_inputs(self):
        self.freeze()
        (self.root / "synthetic.csv").unlink()
        out = io.StringIO()
        with contextlib.redirect_stderr(out):
            status = main(["audit", "--root", str(self.root), "--manifest", str(self.output)])
        self.assertEqual(status, 1)
        self.assertNotIn(str(self.root), out.getvalue())

    def test_candle_start_end_and_backfill_availability_are_distinct(self):
        args = dict(provider="synthetic", market="KR", exchange="KRX", symbol="DEMO", source_at=T, observed_at=T, interval_minutes=1)
        start = CandleEvidence(**args, timestamp_semantics="START")
        end = CandleEvidence(**args, timestamp_semantics="END")
        self.assertFalse(start.usable_at(T))
        self.assertTrue(end.usable_at(T))
        backfill = CandleEvidence(**{**args, "observed_at": T + timedelta(days=1)}, timestamp_semantics="END")
        self.assertFalse(backfill.usable_at(T + timedelta(minutes=1)))
        revised = CandleEvidence(**args, timestamp_semantics="END", revised_at=T + timedelta(minutes=2))
        self.assertFalse(revised.usable_at(T + timedelta(minutes=1)))

    def test_unknown_candle_contract_rejected(self):
        with self.assertRaises(ValueError):
            CandleEvidence("fixture", "KR", "KRX", "DEMO", T, T, 1, "UNKNOWN")

    def test_simulation_has_separate_assumptions_denominators_and_file_receipt(self):
        bars, config = demo_bars(), load_config(None)
        result = simulate(bars, config)
        self.assertFalse(result["execution_assumptions"]["actual_account_performance"])
        self.assertEqual(result["denominators"]["signals"], len(result["fills"]) + len(result["rejections"]) + len(result["pending_orders"]))
        path = self.root / "run"
        save_results(path, bars, config, result)
        evidence = json.loads((path / "evidence-manifest.json").read_text())
        self.assertEqual(evidence["files"], fingerprint_files(path, [row["path"] for row in evidence["files"]]))
        self.assertFalse(evidence["preregistered"])
        self.assertFalse(evidence["broker_fills_verified"])


class WalkForwardTest(unittest.TestCase):
    def sample(self, index, *, suffix="", horizon=0, delayed=0):
        moment = T + timedelta(minutes=index)
        return LabelledSample(f"s{index}{suffix}", moment, moment + timedelta(minutes=horizon), moment + timedelta(minutes=delayed), moment + timedelta(minutes=horizon))

    def test_same_timestamp_symbols_remain_in_same_fold(self):
        rows = [self.sample(i, suffix=s) for i in range(8) for s in ("A", "B")]
        folds = walk_forward_splits(rows, train_timestamps=3, test_timestamps=2)
        self.assertEqual(len(folds), 2)  # Last incomplete window excluded.
        self.assertEqual(folds[0]["test_ids"], ["s3A", "s3B", "s4A", "s4B"])
        self.assertFalse(set(folds[0]["test_ids"]) & set(folds[1]["test_ids"]))
        self.assertFalse(folds[0]["profitability_verified"])

    def test_crossing_labels_purged_and_embargo_kept(self):
        rows = [self.sample(i, horizon=1) for i in range(8)]
        folds = walk_forward_splits(rows, train_timestamps=4, test_timestamps=2, embargo=timedelta(minutes=1))
        self.assertEqual(folds[0]["train_ids"], ["s0", "s1"])
        self.assertEqual(folds[0]["purged_or_embargoed"], 2)

    def test_backfilled_input_counted_not_silently_included(self):
        rows = [self.sample(i, delayed=1 if i in (0, 3) else 0) for i in range(6)]
        fold = walk_forward_splits(rows, train_timestamps=3, test_timestamps=2)[0]
        self.assertEqual(fold["training_pit_invalid"], 1)
        self.assertEqual(fold["test_pit_invalid"], 1)
        self.assertNotIn("s0", fold["train_ids"])
        self.assertNotIn("s3", fold["test_ids"])

    def test_no_training_is_not_pass(self):
        rows = [self.sample(i, horizon=50) for i in range(5)]
        self.assertEqual(walk_forward_splits(rows, train_timestamps=3, test_timestamps=2)[0]["status"], "INSUFFICIENT_DATA")

    def test_unknown_label_availability_is_not_assumed_known(self):
        rows = [LabelledSample(str(i), T + timedelta(minutes=i), T + timedelta(minutes=i), T + timedelta(minutes=i)) for i in range(5)]
        fold = walk_forward_splits(rows, train_timestamps=3, test_timestamps=2)[0]
        self.assertEqual(fold["training_label_availability_unknown"], 3)
        self.assertEqual(fold["status"], "INSUFFICIENT_DATA")

    def test_invalid_and_duplicate_sample_rejected(self):
        for rows in ([self.sample(1), self.sample(0)], [self.sample(1), self.sample(1)]):
            with self.assertRaises(ValueError):
                walk_forward_splits(rows, train_timestamps=1, test_timestamps=1)
        with self.assertRaises(ValueError):
            walk_forward_splits([], train_timestamps=True, test_timestamps=1)
        with self.assertRaises(ValueError):
            LabelledSample("bad", T, T - timedelta(seconds=1), T)


if __name__ == "__main__":
    unittest.main()
