import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import stat
import tempfile
import threading
import unittest
from unittest.mock import patch

from kiwoom_autotrader.trading.tracker_diagnostics import TrackerDiagnostics, process_rss


class Clock:
    def __init__(self):
        self.at = datetime(2026, 11, 4, 7, 0, tzinfo=timezone.utc)
        self.elapsed = 100.0

    def now(self):
        return self.at

    def monotonic(self):
        return self.elapsed

    def advance(self, seconds):
        self.at += timedelta(seconds=seconds)
        self.elapsed += seconds


class TrackerDiagnosticsTests(unittest.TestCase):
    def make(self, **options):
        clock = options.pop("clock", Clock())
        events = []
        writer = options.pop("append_writer", lambda path, raw: events.append(json.loads(raw)))
        diagnostics = TrackerDiagnostics(
            "/unused-diagnostics-target", now=clock.now, monotonic=clock.monotonic,
            rss_reader=options.pop("rss_reader", lambda: {
                "current_rss_bytes": 1200, "peak_rss_bytes": 1800}),
            append_writer=writer, **options)
        return diagnostics, clock, events

    def test_construction_does_not_touch_files_or_query_memory(self):
        with patch.object(Path, "mkdir", side_effect=AssertionError("unexpected mkdir")), \
                patch("kiwoom_autotrader.trading.tracker_diagnostics.os.open",
                      side_effect=AssertionError("unexpected open")), \
                patch("kiwoom_autotrader.trading.tracker_diagnostics.process_rss",
                      side_effect=AssertionError("unexpected memory query")):
            diagnostics = TrackerDiagnostics("/must-not-be-created")
            self.assertFalse(diagnostics.snapshot()["active"])

    def test_advancing_clock_counts_and_before_work_publication(self):
        published = []
        diagnostics, clock, events = self.make(progress=lambda status: published.append(status))
        with diagnostics.stage("closed_fingerprint") as sample:
            self.assertEqual(published[-1]["phase"], "closed_fingerprint")
            self.assertTrue(published[-1]["active"])
            self.assertIsNone(published[-1]["last_completed_at"])
            clock.advance(12.75)
            sample.set_counts(rows=21721, bytes_in=133000000, bytes_out=64,
                              bytes_in_basis="payload_json_utf8",
                              bytes_out_basis="canonical_output_utf8")
        self.assertEqual(events[0]["kind"], "begin")
        end = events[-1]
        self.assertEqual(end["duration_seconds"], 12.75)
        self.assertEqual(end["rows"], 21721)
        self.assertEqual(end["bytes_in"], 133000000)
        self.assertEqual(end["bytes_out"], 64)
        self.assertEqual(end["bytes_in_basis"], "payload_json_utf8")
        self.assertEqual(end["bytes_out_basis"], "canonical_output_utf8")
        self.assertEqual(end["current_rss_basis"], "injected_numeric_footprint")
        self.assertEqual(end["observed_at"], "2026-11-04T16:00:12.750000+09:00")
        self.assertEqual(end["observed_at_utc"], "2026-11-04T07:00:12.750000+00:00")
        self.assertFalse(published[-1]["active"])

    def test_original_exception_is_not_swallowed_or_replaced(self):
        diagnostics, clock, events = self.make()
        original = RuntimeError("private provider response must not be logged")
        with self.assertRaises(RuntimeError) as caught:
            with diagnostics.stage("profit_fetch"):
                clock.advance(3)
                raise original
        self.assertIs(caught.exception, original)
        self.assertTrue(events[-1]["failed"])
        self.assertEqual(events[-1]["duration_seconds"], 3)
        self.assertNotIn("private", json.dumps(events))

    def test_nullable_rss_is_unobserved_not_zero(self):
        diagnostics, _, events = self.make(rss_reader=lambda: {
            "current_rss_bytes": None, "peak_rss_bytes": None})
        with diagnostics.stage("tracker_task"):
            pass
        self.assertIsNone(events[-1]["current_rss_bytes"])
        self.assertIsNone(events[-1]["peak_rss_bytes"])
        self.assertIsNone(events[-1]["bytes_in"])
        self.assertIsNone(events[-1]["bytes_out"])
        self.assertIsNone(events[-1]["current_rss_basis"])
        self.assertEqual(diagnostics.snapshot()["failures"]["rss"], 0)

    def test_rss_failure_does_not_replace_work_result(self):
        def fail():
            raise RuntimeError("private memory-read context")
        diagnostics, _, events = self.make(rss_reader=fail)
        with diagnostics.stage("tracker_task"):
            result = 42
        self.assertEqual(result, 42)
        self.assertEqual(diagnostics.snapshot()["failures"]["rss"], 2)
        self.assertIsNone(events[-1]["current_rss_bytes"])
        self.assertNotIn("private", json.dumps(diagnostics.snapshot()))

    def test_write_failure_still_publishes_progress_without_error_text(self):
        def fail(path, raw):
            raise PermissionError("sensitive path or account value")
        published = []
        diagnostics, _, _ = self.make(append_writer=fail, progress=published.append)
        with diagnostics.stage("tracker_cycle"):
            self.assertTrue(published[-1]["write_failed"])
            self.assertEqual(published[-1]["phase"], "tracker_cycle")
        status = diagnostics.snapshot()
        self.assertTrue(status["write_failed"])
        self.assertEqual(status["failures"]["write"], 2)
        self.assertNotIn("sensitive", json.dumps(status))

    def test_callback_failure_does_not_change_caller_exception(self):
        def fail(status):
            raise RuntimeError("private callback payload")
        diagnostics, _, events = self.make(progress=fail)
        with self.assertRaisesRegex(ValueError, "original"):
            with diagnostics.stage("tracker_task"):
                raise ValueError("original")
        status = diagnostics.snapshot()
        self.assertTrue(status["callback_failed"])
        self.assertEqual(status["failures"]["callback"], 2)
        self.assertTrue(events[-1]["failed"])
        self.assertNotIn("private", json.dumps(status))

    def test_invalid_labels_and_counts_cannot_leak_payload(self):
        diagnostics, _, events = self.make()
        with diagnostics.stage("account-123456789-secret", rows="raw provider payload") as sample:
            sample.set_counts(bytes_in=True)
        self.assertTrue(all(event["stage"] == "unclassified" for event in events))
        self.assertIsNone(events[-1]["rows"])
        self.assertEqual(diagnostics.snapshot()["failures"]["stage"], 1)
        self.assertEqual(diagnostics.snapshot()["failures"]["counts"], 2)
        serialized = json.dumps(events) + json.dumps(diagnostics.snapshot())
        self.assertNotIn("123456789", serialized)
        self.assertNotIn("provider payload", serialized)

    def test_nested_stage_parent_links_and_bounded_recent_history(self):
        diagnostics, clock, events = self.make(max_recent=3)
        with diagnostics.stage("tracker_cycle"):
            with diagnostics.stage("accounting"):
                self.assertEqual(diagnostics.snapshot()["phase"], "accounting")
                clock.advance(2)
            self.assertEqual(diagnostics.snapshot()["phase"], "tracker_cycle")
        self.assertEqual(events[1]["parent_sequence"], events[0]["sequence"])
        self.assertEqual(events[2]["parent_sequence"], events[0]["sequence"])
        self.assertEqual(events[1]["run_id"], events[0]["run_id"])
        for _ in range(10):
            with diagnostics.stage("summary"):
                clock.advance(1)
        self.assertEqual(len(diagnostics.snapshot()["recent"]), 3)

    def test_other_thread_does_not_publish_or_overwrite_main_completion(self):
        published = []
        diagnostics, _, events = self.make(progress=published.append)
        background_status = []
        with diagnostics.stage("tracker_cycle"):
            count = len(published)
            def worker():
                with diagnostics.stage("history_export"):
                    background_status.append(diagnostics.snapshot())
            thread = threading.Thread(target=worker)
            thread.start()
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive())
            self.assertEqual(len(published), count)
            self.assertEqual(background_status[0]["phase"], "tracker_cycle")
            self.assertEqual(background_status[0]["background_active_stage_count"], 1)
            self.assertIsNone(diagnostics.snapshot()["last_completed_at"])
        self.assertEqual(diagnostics.snapshot()["last_stage"], "tracker_cycle")
        self.assertEqual(diagnostics.snapshot()["callback_skipped_other_thread"], 2)
        self.assertEqual(sum(event["owner_thread"] is False for event in events), 2)

    def test_clock_failure_or_backward_monotonic_is_null_and_visible(self):
        diagnostics, clock, events = self.make()
        with diagnostics.stage("summary"):
            clock.elapsed -= 1
        self.assertIsNone(events[-1]["duration_seconds"])
        self.assertGreater(diagnostics.snapshot()["failures"]["clock"], 0)
        diagnostics = TrackerDiagnostics("/unused", now=lambda: datetime(2026, 11, 4),
                                         append_writer=lambda path, raw: None,
                                         rss_reader=lambda: {})
        with diagnostics.stage("summary"):
            pass
        self.assertIsNone(diagnostics.snapshot()["last_completed_at"])
        self.assertEqual(diagnostics.snapshot()["failures"]["clock"], 2)

    def test_callback_may_read_snapshot_without_recursion(self):
        seen = []
        diagnostics, _, _ = self.make()
        diagnostics.set_progress_callback(lambda status: seen.append(diagnostics.snapshot()))
        with diagnostics.stage("broker_snapshot"):
            pass
        self.assertEqual(len(seen), 2)
        self.assertEqual(seen[0]["phase"], "broker_snapshot")

    def test_work_day_and_mode_inherit_only_within_same_thread(self):
        diagnostics, _, events = self.make()
        with diagnostics.stage("tracker_task", work_day="20261027", mode="paper"):
            with diagnostics.stage("accounting"):
                status = diagnostics.snapshot()
                self.assertEqual(status["work_day"], "20261027")
                self.assertEqual(status["mode"], "paper")
            thread = threading.Thread(target=lambda: self.background_stage(diagnostics))
            thread.start()
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive())
        main_events = [e for e in events if e["owner_thread"]]
        self.assertTrue(all(e["work_day"] == "20261027" and e["mode"] == "paper"
                            for e in main_events))
        background_events = [e for e in events if not e["owner_thread"]]
        self.assertTrue(all(e["work_day"] is None and e["mode"] is None
                            and e["parent_sequence"] is None for e in background_events))
        self.assertEqual(diagnostics.snapshot()["last_work_day"], "20261027")

    @staticmethod
    def background_stage(diagnostics):
        with diagnostics.stage("history_export"):
            pass

    def test_absent_work_context_remains_null_not_today_or_paper(self):
        diagnostics, _, events = self.make()
        with diagnostics.stage("tracker_cycle"):
            with diagnostics.stage("broker_snapshot"):
                self.assertIsNone(diagnostics.snapshot()["work_day"])
                self.assertIsNone(diagnostics.snapshot()["mode"])
        self.assertTrue(all(e["work_day"] is None and e["mode"] is None for e in events))
        self.assertIsNone(diagnostics.snapshot()["last_work_day"])

    def test_numeric_work_day_normalizes_but_invalid_context_is_never_logged(self):
        diagnostics, _, events = self.make()
        with diagnostics.stage("tracker_task", work_day=20261027, mode="paper"):
            with diagnostics.stage("accounting", work_day="20260230", mode="account-private-123456789"):
                self.assertIsNone(diagnostics.snapshot()["work_day"])
                self.assertIsNone(diagnostics.snapshot()["mode"])
            with diagnostics.stage("history_export", work_day=True):
                self.assertIsNone(diagnostics.snapshot()["work_day"])
                self.assertEqual(diagnostics.snapshot()["mode"], "paper")
        self.assertEqual(events[0]["work_day"], "20261027")
        self.assertEqual(diagnostics.snapshot()["failures"]["context"], 3)
        serialized = json.dumps(events) + json.dumps(diagnostics.snapshot())
        self.assertNotIn("20260230", serialized)
        self.assertNotIn("123456789", serialized)

    def test_real_jsonl_append_is_private_bounded_and_never_truncated(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory).resolve()
            clock = Clock()
            diagnostics = TrackerDiagnostics(directory, now=clock.now,
                                             monotonic=clock.monotonic,
                                             rss_reader=lambda: {}, max_file_bytes=2048)
            with diagnostics.stage("tracker_task"):
                clock.advance(1)
            path = directory / "tracker-diagnostics-20261104.jsonl"
            prefix = path.read_bytes()
            for _ in range(10):
                with diagnostics.stage("tracker_task"):
                    clock.advance(1)
            raw = path.read_bytes()
            self.assertTrue(raw.startswith(prefix))
            self.assertLessEqual(len(raw), 2048)
            self.assertTrue(diagnostics.snapshot()["write_failed"])
            self.assertEqual(diagnostics.snapshot()["write_status"],
                             "daily_byte_cap_reached_no_truncation")
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertTrue(all(json.loads(line)["schema"] == "tracker-stage-timing-v1"
                                for line in raw.splitlines()))

    def test_symlink_output_is_rejected_without_touching_target(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory).resolve()
            target = directory / "protected.txt"
            target.write_text("unchanged")
            log = directory / "logs"
            log.mkdir()
            (log / "tracker-diagnostics-20261104.jsonl").symlink_to(target)
            clock = Clock()
            diagnostics = TrackerDiagnostics(log, now=clock.now, monotonic=clock.monotonic,
                                             rss_reader=lambda: {})
            with diagnostics.stage("summary"):
                pass
            self.assertEqual(target.read_text(), "unchanged")
            self.assertTrue(diagnostics.snapshot()["write_failed"])

    def test_windows_rss_is_explicitly_unknown(self):
        with patch("kiwoom_autotrader.trading.tracker_diagnostics.sys.platform", "win32"):
            self.assertEqual(process_rss(), {"current_rss_bytes": None, "peak_rss_bytes": None,
                                             "current_rss_basis": None, "peak_rss_basis": None})


if __name__ == "__main__":
    unittest.main()
