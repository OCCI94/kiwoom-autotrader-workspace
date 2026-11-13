"""Point-in-time feature and private daily export tests on synthetic messages."""

import csv
from datetime import datetime
import json
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from kiwoom_autotrader.trading.message_factors import (
    MessageFactorError, build_daily_factors, symbol_candidates,
)
from kiwoom_autotrader.utils.message_dataset import MessageDatasetError, export_daily_dataset


def stamp(value):
    return int(datetime.fromisoformat(value).timestamp())


D1 = stamp("2026-10-25T10:00:00+09:00")
D2 = stamp("2026-10-26T10:00:00+09:00")
D3 = stamp("2026-10-27T10:00:00+09:00")


def version(version_id=1, message_id=1, *, published_at=D1, observed_at=D1,
            text="$AAPL", edited_at=None, channel_id=10, **extra):
    result = dict(version_id=version_id, message_id=message_id, channel_id=channel_id,
                published_at=published_at, edited_at=edited_at, observed_at=observed_at,
                first_observed_at=extra.pop("first_observed_at", observed_at), origin="observed",
                text=text, media_type=None, grouped_id=None, title="Synthetic channel",
                username="test_channel", permalink="https://untrusted.example/ignored")
    result.update(extra)
    return result


class FakeStore:
    def __init__(self, records):
        self.account_id = 123
        self.records = records
        self.revision = 1
        self.reads = 0

    def dataset_revision(self):
        return self.revision

    def versions(self, *, active_only=True):
        if active_only:
            raise AssertionError("Research exports must retain historically selected sources")
        self.reads += 1
        return self.records


class DailyFactorTests(unittest.TestCase):
    def test_late_arrival_appears_on_availability_day_not_old_publication_day(self):
        records = [version(published_at=D1, observed_at=D2)]
        self.assertEqual(build_daily_factors(records, D1)["daily"], [])
        row = build_daily_factors(records, D2)["daily"][0]
        self.assertEqual(row["date_kst"], "2026-10-26")
        self.assertEqual(row["new_message_count"], 1)
        self.assertEqual(row["prior_day_backfill_count"], 1)
        self.assertTrue(row["is_partial_day"])

    def test_kst_midnight_boundary_is_not_utc_midnight(self):
        before = stamp("2026-10-25T14:59:59+00:00")
        after = before + 1
        result = build_daily_factors([
            version(1, 1, published_at=before, observed_at=before),
            version(2, 2, published_at=after, observed_at=after),
        ], after)
        self.assertEqual([r["date_kst"] for r in result["daily"]], ["2026-10-25", "2026-10-26"])
        self.assertFalse(result["daily"][0]["is_partial_day"])
        self.assertTrue(result["daily"][1]["is_partial_day"])

    def test_later_edit_does_not_rewrite_previous_day_symbol_counts(self):
        initial = version()
        edit = version(2, text="$MSFT", observed_at=D2, edited_at=D2, first_observed_at=D1)
        old = build_daily_factors([initial], D3)
        result = build_daily_factors([edit, initial], D3)
        self.assertEqual(old["daily"][0], result["daily"][0])
        self.assertEqual(result["symbols"][0]["symbol"], "US:AAPL")
        self.assertEqual(result["symbols"][1]["symbol"], "US:MSFT")
        self.assertEqual(result["daily"][1]["new_message_count"], 0)
        self.assertEqual(result["daily"][1]["updated_message_count"], 1)

    def test_intraday_cutoff_uses_only_latest_known_revision(self):
        records = [version(), version(2, text="$MSFT $MSFT", edited_at=D1 + 60,
                                    observed_at=D1 + 120, first_observed_at=D1)]
        before = build_daily_factors(records, D1 + 100)
        self.assertEqual(before["symbols"][0]["symbol"], "US:AAPL")
        after = build_daily_factors(records, D2)
        self.assertEqual(after["symbols"][0]["symbol"], "US:MSFT")
        self.assertEqual(after["symbols"][0]["mention_count"], 2)
        self.assertEqual(after["symbols"][0]["message_count"], 1)
        self.assertEqual(after["daily"][0]["observed_revision_count"], 2)
        self.assertEqual(after["daily"][0]["observed_message_count"], 1)

    def test_duplicate_observation_does_not_inflate_daily_activity(self):
        first = version()
        repeated = version(2, observed_at=D2, first_observed_at=D1)
        result = build_daily_factors([first, first.copy(), repeated], D3)
        self.assertEqual(len(result["daily"]), 1)
        self.assertEqual(result["daily"][0]["observed_revision_count"], 1)

    def test_explicit_symbol_contexts_ignore_prices_dates_and_plain_names(self):
        text = "202610 005930 AAPL 1234567 KR:005930 KRX:005930 A000660 (035420) [051910] 종목코드:005380 $aapl $BRK.B $123 $TOOLONGABC"
        result = symbol_candidates(text)
        self.assertEqual(result[("KR", "KR:005930")], 2)
        self.assertEqual(result[("US", "US:AAPL")], 1)
        self.assertEqual(result[("US", "US:BRK.B")], 1)
        self.assertNotIn(("KR", "KR:202610"), result)
        self.assertEqual(len(result), 7)
        self.assertEqual(symbol_candidates("A0059300 X$AAPL $AAPLfoo123 999999"), {})

    def test_message_ids_are_scoped_by_channel(self):
        rows = [version(1, text="$AAPL"), version(2, channel_id=20, text="$AAPL")]
        result = build_daily_factors(rows, D1)
        self.assertEqual(result["daily"][0]["observed_message_count"], 2)
        self.assertEqual(result["symbols"][0]["channel_count"], 2)

    def test_future_source_edit_is_not_eligible_until_its_timestamp(self):
        row = version(edited_at=D2, observed_at=D1)
        self.assertEqual(build_daily_factors([row], D1)["daily"], [])
        self.assertEqual(build_daily_factors([row], D2)["daily"][0]["date_kst"], "2026-10-26")

    def test_unknown_days_are_not_zero_filled_and_legacy_is_labelled(self):
        legacy = version(2, 2, observed_at=D3)
        legacy["origin"] = "legacy_snapshot"
        result = build_daily_factors([version(), legacy], D3)
        self.assertEqual([r["date_kst"] for r in result["daily"]], ["2026-10-25", "2026-10-27"])
        self.assertEqual(result["daily"][1]["legacy_snapshot_count"], 1)

    def test_invalid_timestamps_and_mutating_publication_time_are_rejected(self):
        for rows, cutoff in (([version(observed_at=True)], D2), ([version()], True),
                             ([version(), version(2, published_at=D2)], D3)):
            with self.subTest(rows=len(rows)):
                with self.assertRaises(MessageFactorError):
                    build_daily_factors(rows, cutoff)


class DailyExportTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "daily"

    def manifest(self):
        return json.loads((self.root / "manifest.json").read_text())

    def test_raw_partition_preserves_versions_and_private_permissions(self):
        rows = [version(observed_at=D2, text="line1\nline2", access_hash="never-export"),
                version(2, observed_at=D3, edited_at=D3, text="edited", first_observed_at=D2)]
        result = export_daily_dataset(FakeStore(rows), self.root, D3)
        self.assertEqual((result["days"], result["versions"], result["messages"]), (1, 2, 1))
        raw = (self.root / "2026-10-25" / "messages.jsonl").read_text()
        self.assertNotIn("never-export", raw)
        self.assertNotIn("untrusted.example", raw)
        self.assertEqual(len(raw.splitlines()), 2)
        parsed = [json.loads(line) for line in raw.splitlines()]
        self.assertEqual(parsed[0]["availability_date_kst"], "2026-10-26")
        self.assertEqual(parsed[0]["text"], "line1\nline2")
        for path in self.root.rglob("*"):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700 if path.is_dir() else 0o600)
        self.assertEqual(self.manifest()["timezone"], "Asia/Seoul")

    def test_repeated_export_is_idempotent_and_avoids_ledger_read(self):
        store = FakeStore([version()])
        export_daily_dataset(store, self.root, D2)
        times = {p: p.stat().st_mtime_ns for p in self.root.rglob("*") if p.is_file()}
        result = export_daily_dataset(store, self.root, D2)
        self.assertTrue(result["cached"])
        self.assertEqual(result["changed_files"], 0)
        self.assertEqual(store.reads, 1)
        self.assertEqual(times, {p: p.stat().st_mtime_ns for p in times})

    def test_unchanged_raw_days_are_not_rewritten_when_new_data_arrives(self):
        store = FakeStore([version()])
        export_daily_dataset(store, self.root, D2)
        old = self.root / "2026-10-25" / "messages.jsonl"
        before = old.stat().st_mtime_ns
        store.records.append(version(2, 2, published_at=D2, observed_at=D2))
        store.revision += 1
        result = export_daily_dataset(store, self.root, D2)
        self.assertFalse(result["cached"])
        self.assertEqual(old.stat().st_mtime_ns, before)
        self.assertTrue((self.root / "2026-10-26" / "messages.jsonl").exists())

    def test_cache_respects_future_source_timestamp_becoming_eligible(self):
        store = FakeStore([version(edited_at=D1 + 60)])
        with patch("kiwoom_autotrader.utils.message_dataset.time.time", return_value=D1):
            first = export_daily_dataset(store, self.root)
        self.assertEqual(first["factor_days"], 0)
        with patch("kiwoom_autotrader.utils.message_dataset.time.time", return_value=D1 + 60):
            second = export_daily_dataset(store, self.root)
        self.assertFalse(second["cached"])
        self.assertEqual(second["factor_days"], 1)

    def test_same_day_default_cutoff_advances_with_clock(self):
        store = FakeStore([version()])
        with patch("kiwoom_autotrader.utils.message_dataset.time.time", return_value=D1):
            export_daily_dataset(store, self.root)
        with patch("kiwoom_autotrader.utils.message_dataset.time.time", return_value=D1 + 60):
            result = export_daily_dataset(store, self.root)
        self.assertFalse(result["cached"])
        self.assertEqual(result["as_of"], D1 + 60)

    def test_earlier_snapshot_removes_only_previously_managed_future_files(self):
        store = FakeStore([version(), version(2, 2, published_at=D2, observed_at=D2)])
        export_daily_dataset(store, self.root, D3)
        note = self.root / "2026-10-26" / "personal-note.txt"
        note.write_text("preserve")
        export_daily_dataset(store, self.root, D1)
        self.assertFalse((note.parent / "messages.jsonl").exists())
        self.assertEqual(note.read_text(), "preserve")
        self.assertEqual(self.manifest()["version_count"], 1)

    def test_csv_contains_only_safe_factor_values_not_arbitrary_source_text(self):
        row = version(text="=HYPERLINK(\"bad\") $AAPL", title="=malicious-title")
        export_daily_dataset(FakeStore([row]), self.root, D2)
        for name in ("daily_factors.csv", "symbol_factors.csv"):
            data = (self.root / name).read_text(encoding="utf-8-sig")
            self.assertNotIn("HYPERLINK", data)
            self.assertNotIn("malicious-title", data)
            for record in csv.DictReader(data.splitlines()):
                self.assertTrue(all(not value.lstrip().startswith(("=", "+", "-", "@")) for value in record.values()))

    def test_symlink_root_is_rejected_without_touching_target(self):
        target = self.root.parent / "target"
        target.mkdir()
        self.root.symlink_to(target, target_is_directory=True)
        with self.assertRaises(MessageDatasetError):
            export_daily_dataset(FakeStore([version()]), self.root, D2)
        self.assertEqual(list(target.iterdir()), [])

    def test_modified_cached_file_is_repaired_from_ledger(self):
        store = FakeStore([version()])
        export_daily_dataset(store, self.root, D2)
        raw = self.root / "2026-10-25" / "messages.jsonl"
        raw.write_text("damaged")
        result = export_daily_dataset(store, self.root, D2)
        self.assertFalse(result["cached"])
        self.assertEqual(json.loads(raw.read_text())["message_id"], 1)

    def test_external_exception_is_sanitized(self):
        store = FakeStore([version()])
        store.versions = lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("private-message-secret"))
        with self.assertRaises(MessageDatasetError) as caught:
            export_daily_dataset(store, self.root, D2)
        self.assertNotIn("private-message-secret", str(caught.exception))

    def test_account_change_cannot_reuse_another_accounts_cached_dataset(self):
        first = FakeStore([version()])
        export_daily_dataset(first, self.root, D2)
        second = FakeStore([version(2, 99, text="$MSFT")])
        second.account_id = 456
        result = export_daily_dataset(second, self.root, D2)
        self.assertFalse(result["cached"])
        raw = self.root / "2026-10-25" / "messages.jsonl"
        self.assertEqual(json.loads(raw.read_text())["message_id"], 99)

    def test_real_temporary_ledger_contract_and_no_historical_hindsight(self):
        from kiwoom_autotrader.channel_store import ChannelStore
        with patch("kiwoom_autotrader.channel_store.time.time", return_value=D2):
            with ChannelStore(self.root.parent / "state", 123) as store:
                store.register_channel(100, 200, "Synthetic", "synthetic_channel")
                store.commit_page(100, [{"message_id": 1, "published_at": D1, "text": "KR:005930 $AAPL"}])
                result = export_daily_dataset(store, self.root, D2)
        self.assertEqual((result["versions"], result["messages"]), (1, 1))
        with (self.root / "daily_factors.csv").open(encoding="utf-8-sig") as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual([row["date_kst"] for row in rows], ["2026-10-26"])
        self.assertEqual(rows[0]["prior_day_backfill_count"], "1")


if __name__ == "__main__":
    unittest.main()
