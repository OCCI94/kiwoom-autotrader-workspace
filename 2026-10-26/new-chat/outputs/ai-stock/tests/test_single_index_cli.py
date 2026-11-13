"""The offline CLI accepts only explicit data and never overwrites evidence."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


def input_spec():
    rows = []
    for day, market in enumerate((0.01, 0.02, -0.01, 0.03, 0.0), 1):
        rows.append({
            "period_start": f"2026-01-{day:02d}T00:00:00+00:00",
            "period_end": f"2026-01-{day:02d}T12:00:00+00:00",
            "asset_return": 0.002 + 1.5 * market,
            "market_return": market, "risk_free_return": 0,
            "asset_available_at": f"2026-01-{day:02d}T13:00:00+00:00",
            "market_available_at": f"2026-01-{day:02d}T13:00:00+00:00",
            "risk_free_available_at": f"2026-01-{day:02d}T13:00:00+00:00",
        })
    return {
        "context": {"asset": "test", "benchmark": "test index", "currency": "KRW",
                    "frequency": "daily", "session": "test", "return_basis": "test",
                    "asset_source": "test", "benchmark_source": "test",
                    "risk_free_source": "assumed_zero_not_observed",
                    "universe_selection": "test", "benchmark_role": "market_index"},
        "as_of": "2026-01-06T00:00:00+00:00",
        "training_end": "2026-01-04T14:00:00+00:00", "rows": rows,
        "min_observations": 3, "analysis_mode": "retrospective_descriptive",
    }


class SingleIndexCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)
        self.input = self.directory / "input.json"
        self.output = self.directory / "output.json"
        self.input.write_text(json.dumps(input_spec()), encoding="utf-8")

    def run_cli(self):
        return subprocess.run([sys.executable, "-m", "kiwoom_autotrader.research",
                               "single-index", "--input", str(self.input),
                               "--output", str(self.output)], cwd=ROOT,
                              capture_output=True, text=True, timeout=15)

    def test_complete_output_contains_fit_and_caveats(self):
        proc = self.run_cli()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout)["fit_status"], "fitted")
        result = json.loads(self.output.read_text(encoding="utf-8"))
        self.assertAlmostEqual(result["fit"]["beta"], 1.5)
        self.assertEqual(result["analysis_mode"], "retrospective_descriptive")
        self.assertEqual(result["counts"]["training_rows"], 4)
        self.assertEqual(result["counts"]["evaluation_rows"], 1)
        self.assertFalse(result["automatic_orders"])
        self.assertFalse(result["out_of_sample_performance_verified"])
        self.assertEqual(self.output.stat().st_mode & 0o777, 0o600)

    def test_existing_output_and_input_are_never_overwritten(self):
        self.output.write_text("original", encoding="utf-8")
        self.assertNotEqual(self.run_cli().returncode, 0)
        self.assertEqual(self.output.read_text(), "original")
        self.output.unlink()
        self.output.symlink_to(self.directory / "missing.json")
        self.assertNotEqual(self.run_cli().returncode, 0)
        self.assertTrue(self.output.is_symlink())
        self.assertFalse((self.directory / "missing.json").exists())
        self.assertFalse(list(self.directory.glob(".single-index-*")))
        before = self.input.read_bytes()
        self.output = self.input
        self.assertNotEqual(self.run_cli().returncode, 0)
        self.assertEqual(self.input.read_bytes(), before)

    def test_duplicate_keys_and_incomplete_schema_rejected(self):
        for content in ('{"context":1,"context":2}', json.dumps({"rows": []})):
            self.input.write_text(content, encoding="utf-8")
            proc = self.run_cli()
            self.assertNotEqual(proc.returncode, 0)
            self.assertFalse(self.output.exists())
            self.assertNotIn("Traceback", proc.stderr)

    def test_temporal_errors_do_not_leave_output(self):
        spec = input_spec()
        spec["training_end"] = "2027-01-01T00:00:00+00:00"
        self.input.write_text(json.dumps(spec), encoding="utf-8")
        self.assertNotEqual(self.run_cli().returncode, 0)
        self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main()
