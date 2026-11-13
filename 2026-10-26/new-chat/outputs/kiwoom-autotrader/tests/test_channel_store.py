"""Channel archive tests on temporary files, without network or user data."""

from pathlib import Path
import shutil
import sqlite3
import stat
import tempfile
import unittest
from unittest.mock import patch

from kiwoom_autotrader.channel_store import ChannelStore, ChannelStoreError


def post(message_id=10, *, published_at=1000, edited_at=None, text="선택한 채널의 글", **extra):
    return dict(message_id=message_id, published_at=published_at, edited_at=edited_at, text=text, **extra)


class ChannelStoreTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name) / "archive"
        self.store = ChannelStore(self.directory, 101)
        self.addCleanup(self.store.close)
        self.store.register_channel(12345, -9876, "채널 A", "example_channel")

    def test_private_modes_and_no_session_storage(self):
        self.assertEqual(stat.S_IMODE(self.directory.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.store.path.stat().st_mode), 0o600)
        self.assertEqual({p.name for p in self.directory.iterdir()}, {"channels-101.sqlite3"})
        self.assertNotIn("session", self.store.summary())

    def test_selected_sources_only(self):
        with self.assertRaises(ChannelStoreError):
            self.store.commit_page(9999, [post()], latest_id=10)
        self.assertEqual(self.store.summary()["messages"], 0)

    def test_page_and_cursors_persist_and_replay_is_idempotent(self):
        page = [post(10), post(9, published_at=999, text="caption", media_type="photo", grouped_id=456)]
        self.store.commit_page(12345, page, history_cursor=9, latest_id=10)
        self.store.commit_page(12345, page, history_cursor=9, latest_id=10)
        self.store.close()
        with ChannelStore(self.directory, 101) as reopened:
            self.assertEqual(reopened.summary(), {
                "channels": 1, "messages": 2, "history_complete": 0, "history_pending": 1,
            })
            source = reopened.channels()[0]
            self.assertEqual(source["history_cursor"], 9)
            self.assertEqual(source["latest_id"], 10)
            self.assertEqual(source["access_hash"], -9876)
            records = reopened.recent()
            self.assertEqual(records[0]["permalink"], "https://t.me/example_channel/10")
            self.assertEqual(records[1]["text"], "caption")
            self.assertEqual(records[1]["grouped_id"], 456)
            self.assertNotIn("access_hash", records[0])

    def test_edit_upsert_never_replaces_newer_with_old(self):
        self.store.commit_page(12345, [post(text="original")])
        self.store.commit_page(12345, [post(edited_at=1100, text="edited")])
        self.store.commit_page(12345, [post(text="stale original")])
        self.store.commit_page(12345, [post(edited_at=1050, text="stale edit")])
        self.assertEqual(self.store.recent()[0]["text"], "edited")
        self.assertEqual(self.store.summary()["messages"], 1)

    def test_same_second_original_cannot_replace_an_edited_message(self):
        self.store.commit_page(12345, [post(edited_at=1000, text="edited in same second")])
        self.store.commit_page(12345, [post(text="original")])
        self.assertEqual(self.store.recent()[0]["text"], "edited in same second")

    def test_selection_change_preserves_history_but_stops_collection_and_hides_archive(self):
        self.store.commit_page(12345, [post()], latest_id=10, history_complete=True)
        selected = [{"channel_id": 55, "access_hash": 33, "title": "B", "username": None}]
        self.store.set_selected(selected)
        self.assertEqual([row["channel_id"] for row in self.store.channels()], [55])
        self.assertEqual(len(self.store.channels(include_inactive=True)), 2)
        self.assertEqual(self.store.recent(), [])
        self.assertEqual(self.store.summary()["messages"], 0)
        with self.assertRaises(ChannelStoreError):
            self.store.commit_page(12345, [post(11)])
        self.store.register_channel(12345, 999, "restored")
        self.assertEqual(self.store.summary()["messages"], 1)
        self.assertEqual(self.store.recent()[0]["message_id"], 10)
        restored = [source for source in self.store.channels() if source["channel_id"] == 12345][0]
        self.assertEqual(restored["latest_id"], 10)
        self.assertTrue(restored["history_complete"])
        before = self.store.channels()
        with self.assertRaises(ChannelStoreError):
            self.store.set_selected(selected + [{"channel_id": True}])
        self.assertEqual(self.store.channels(), before)
        self.store.set_selected([])
        self.assertEqual(self.store.channels(), [])
        self.assertEqual(len(self.store.channels(include_inactive=True)), 2)

    def test_whole_page_rejected_before_any_cursor_or_post_is_changed(self):
        with self.assertRaises(ChannelStoreError):
            self.store.commit_page(12345, [post(10), post(9, published_at=True)], history_cursor=9, latest_id=10)
        self.assertEqual(self.store.summary()["messages"], 0)
        source = self.store.channels()[0]
        self.assertIsNone(source["history_cursor"])
        self.assertEqual(source["latest_id"], 0)

    def test_transaction_rolls_back_earlier_messages_on_late_database_failure(self):
        with sqlite3.connect(self.store.path) as db:
            db.execute("""CREATE TRIGGER reject_second BEFORE INSERT ON posts WHEN NEW.message_id=9
                          BEGIN SELECT RAISE(ABORT, 'sensitive SQL must not escape'); END""")
        with self.assertRaises(ChannelStoreError) as caught:
            self.store.commit_page(12345, [post(10), post(9)], history_cursor=9, latest_id=10)
        self.assertNotIn("sensitive", str(caught.exception))
        self.assertEqual(self.store.summary()["messages"], 0)
        self.assertEqual(self.store.channels()[0]["latest_id"], 0)
        self.assertEqual(self.store.versions(), [])

    def test_channel_reregistration_updates_metadata_but_preserves_progress(self):
        self.store.commit_page(12345, [post()], history_cursor=10, latest_id=10, history_complete=True)
        self.store.register_channel(12345, 1111, "새 제목", None)
        source = self.store.channels()[0]
        self.assertEqual(source["title"], "새 제목")
        self.assertEqual(source["access_hash"], 1111)
        self.assertEqual(source["history_cursor"], 10)
        self.assertTrue(source["history_complete"])
        self.assertEqual(self.store.recent()[0]["permalink"], "https://t.me/c/12345/10")

    def test_cursors_only_advance_even_for_stale_pages(self):
        self.store.commit_page(12345, [post(30)], history_cursor=30, latest_id=30)
        self.store.commit_page(12345, [post(20)], history_cursor=20, latest_id=35)
        self.store.commit_page(12345, [], history_complete=True)
        self.store.commit_page(12345, [], history_cursor=25, latest_id=29, history_complete=False)
        source = self.store.channels()[0]
        self.assertEqual(source["history_cursor"], 20)
        self.assertEqual(source["latest_id"], 35)
        self.assertTrue(source["history_complete"])

    def test_two_accounts_do_not_share_registered_channels_or_posts(self):
        self.store.commit_page(12345, [post()])
        with ChannelStore(self.directory, 202) as other:
            self.assertNotEqual(other.path, self.store.path)
            self.assertEqual(other.summary()["channels"], 0)
            self.assertEqual(other.recent(), [])
            with self.assertRaises(ChannelStoreError):
                other.commit_page(12345, [post()])

    def test_renamed_database_from_different_account_is_rejected(self):
        self.store.close()
        destination = self.directory / "channels-202.sqlite3"
        shutil.copyfile(self.store.path, destination)
        with self.assertRaises(ChannelStoreError):
            ChannelStore(self.directory, 202)

    def test_filters_bounds_and_sorting(self):
        self.store.register_channel(55, 42, "B")
        self.store.commit_page(12345, [post(1, published_at=1001), post(2, published_at=1002)])
        self.store.commit_page(55, [post(8, published_at=1003)])
        self.assertEqual([row["message_id"] for row in self.store.recent(2)], [8, 2])
        self.assertEqual([row["message_id"] for row in self.store.recent(2, 12345)], [2, 1])
        for limit in (0, 101, True, "2"):
            with self.subTest(limit=limit), self.assertRaises(ChannelStoreError):
                self.store.recent(limit)

    def test_user_text_is_data_not_sql_or_executable(self):
        body = "'); DROP TABLE posts; --\n/status\n<script>bad()</script>"
        self.store.commit_page(12345, [post(text=body)])
        self.assertEqual(self.store.recent()[0]["text"], body)
        self.assertEqual(self.store.summary()["messages"], 1)

    def test_invalid_identity_and_raw_updates_are_rejected(self):
        invalid = (
            lambda: ChannelStore(self.directory, True),
            lambda: self.store.register_channel(-100123, 33, "invalid"),
            lambda: self.store.register_channel(123, True, "invalid"),
            lambda: self.store.register_channel(123, 33, "invalid", "https://elsewhere.test"),
            lambda: self.store.commit_page(12345, [post(update={"secret": "raw"})]),
            lambda: self.store.commit_page(12345, [post(edited_at=999)]),
            lambda: self.store.commit_page(12345, [post(text="x" * 65537)]),
            lambda: self.store.commit_page(12345, [], history_complete=1),
        )
        for operation in invalid:
            with self.subTest(operation=operation), self.assertRaises(ChannelStoreError):
                operation()

    def test_symlinked_database_and_state_directory_are_rejected(self):
        target = self.directory / "unrelated.txt"
        target.write_text("keep me")
        (self.directory / "channels-303.sqlite3").symlink_to(target)
        with self.assertRaises(ChannelStoreError):
            ChannelStore(self.directory, 303)
        linked_dir = self.directory.parent / "linked"
        linked_dir.symlink_to(self.directory, target_is_directory=True)
        with self.assertRaises(ChannelStoreError):
            ChannelStore(linked_dir, 303)
        self.assertEqual(target.read_text(), "keep me")

    def test_closed_and_corrupt_store_errors_are_sanitized(self):
        self.store.close()
        with self.assertRaises(ChannelStoreError):
            self.store.summary()
        path = self.directory / "channels-404.sqlite3"
        path.write_text("sensitive corrupt data")
        with self.assertRaises(ChannelStoreError) as caught:
            ChannelStore(self.directory, 404)
        self.assertNotIn("sensitive", str(caught.exception))

    def test_point_in_time_keeps_original_until_edit_was_observed(self):
        with patch("kiwoom_autotrader.channel_store.time.time", return_value=2000):
            self.store.commit_page(12345, [post(text="original")])
        with patch("kiwoom_autotrader.channel_store.time.time", return_value=3000):
            self.store.commit_page(12345, [post(edited_at=1500, text="later observed edit")])
        self.assertEqual(self.store.point_in_time(1999), [])
        self.assertEqual(self.store.point_in_time(2999)[0]["text"], "original")
        self.assertEqual(self.store.point_in_time(3000)[0]["text"], "later observed edit")
        versions = self.store.versions()
        self.assertEqual([row["observed_at"] for row in versions], [2000, 3000])
        self.assertEqual([row["first_observed_at"] for row in versions], [2000, 2000])
        self.assertEqual([row["origin"] for row in versions], ["observed", "observed"])
        self.assertNotIn("access_hash", versions[0])
        self.assertNotIn("fingerprint", versions[0])

    def test_edited_backfill_never_invents_original_or_historical_availability(self):
        with patch("kiwoom_autotrader.channel_store.time.time", return_value=5000):
            self.store.commit_page(12345, [post(edited_at=1500, text="only accessible edit")])
        self.assertEqual(self.store.point_in_time(4999), [])
        self.assertEqual(len(self.store.versions()), 1)
        self.assertEqual(self.store.point_in_time(5000)[0]["text"], "only accessible edit")

    def test_duplicate_and_stale_pages_do_not_add_versions_or_revert_snapshots(self):
        with patch("kiwoom_autotrader.channel_store.time.time", return_value=2000):
            self.store.commit_page(12345, [post(edited_at=1500, text="first edit")], latest_id=10)
            self.store.commit_page(12345, [post(edited_at=1500, text="same-second update")])
        revision = self.store.dataset_revision()
        self.store.commit_page(12345, [post(edited_at=1500, text="first edit")])
        self.store.commit_page(12345, [post(edited_at=1200, text="stale")])
        self.store.commit_page(12345, [post(edited_at=1500, text="same-second update")], latest_id=10)
        self.assertEqual(self.store.dataset_revision(), revision)
        self.assertEqual(len(self.store.versions()), 2)
        self.assertEqual(self.store.recent()[0]["text"], "same-second update")
        self.assertEqual(self.store.point_in_time(2000)[0]["text"], "same-second update")
        self.assertLess(self.store.versions()[0]["version_id"], self.store.versions()[1]["version_id"])

    def test_publication_timestamp_mismatch_rolls_back_entire_page(self):
        self.store.commit_page(12345, [post()], latest_id=10)
        revision = self.store.dataset_revision()
        with self.assertRaises(ChannelStoreError):
            self.store.commit_page(12345, [post(11), post(published_at=1001)], latest_id=11)
        self.assertEqual(self.store.summary()["messages"], 1)
        self.assertEqual(len(self.store.versions()), 1)
        self.assertEqual(self.store.dataset_revision(), revision)
        self.assertEqual(self.store.channels()[0]["latest_id"], 10)

    def test_observation_clock_never_moves_backwards(self):
        with patch("kiwoom_autotrader.channel_store.time.time", return_value=3000):
            self.store.commit_page(12345, [post()])
        with patch("kiwoom_autotrader.channel_store.time.time", return_value=2000):
            self.store.commit_page(12345, [post(edited_at=1500, text="clock corrected")])
        self.assertEqual([row["observed_at"] for row in self.store.versions()], [3000, 3000])
        self.assertEqual(self.store.point_in_time(2999), [])

    def test_raw_future_timestamp_is_retained_but_not_used_before_available(self):
        with patch("kiwoom_autotrader.channel_store.time.time", return_value=2000):
            self.store.commit_page(12345, [post(edited_at=2500)])
        self.assertEqual(len(self.store.versions(as_of=2000)), 1)
        self.assertEqual(self.store.point_in_time(2499), [])
        self.assertEqual(len(self.store.point_in_time(2500)), 1)

    def test_snapshot_metadata_and_current_source_filter_are_explicit(self):
        with patch("kiwoom_autotrader.channel_store.time.time", return_value=2000):
            self.store.commit_page(12345, [post()])
        revision = self.store.dataset_revision()
        self.store.register_channel(12345, 123, "new title", "new_username")
        self.assertGreater(self.store.dataset_revision(), revision)
        self.assertEqual(self.store.versions()[0]["title"], "채널 A")
        self.assertEqual(self.store.versions()[0]["permalink"], "https://t.me/example_channel/10")
        revision = self.store.dataset_revision()
        self.store.set_selected([])
        self.assertGreater(self.store.dataset_revision(), revision)
        self.assertEqual(self.store.versions(), [])
        self.assertEqual(self.store.point_in_time(3000), [])
        self.assertEqual(len(self.store.versions(active_only=False)), 1)
        self.assertEqual(len(self.store.point_in_time(3000, active_only=False)), 1)

    def test_ledger_is_insert_only_and_rejects_invalid_cutoffs(self):
        self.store.commit_page(12345, [post()])
        with sqlite3.connect(self.store.path) as db:
            for query in ("UPDATE post_versions SET text='changed'", "DELETE FROM post_versions"):
                with self.assertRaises(sqlite3.IntegrityError):
                    db.execute(query)
        for value in (-1, True, "2000", 253402300800):
            with self.subTest(value=value), self.assertRaises(ChannelStoreError):
                self.store.point_in_time(value)
            with self.subTest(value=value), self.assertRaises(ChannelStoreError):
                self.store.versions(as_of=value)
        with self.assertRaises(ChannelStoreError):
            self.store.versions(active_only=1)


class ChannelStoreMigrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.path = self.directory / "channels-101.sqlite3"
        with sqlite3.connect(self.path) as db:
            db.execute("CREATE TABLE archive_meta(account_id INTEGER NOT NULL, version INTEGER NOT NULL)")
            db.execute("INSERT INTO archive_meta VALUES (101, 1)")
            db.execute("""CREATE TABLE channels(channel_id INTEGER PRIMARY KEY, access_hash INTEGER NOT NULL,
                          title TEXT NOT NULL, username TEXT, active INTEGER NOT NULL DEFAULT 1,
                          history_cursor INTEGER, history_complete INTEGER NOT NULL DEFAULT 0,
                          latest_id INTEGER NOT NULL DEFAULT 0)""")
            db.execute("INSERT INTO channels VALUES (12345, -9876, 'legacy channel', 'legacy_channel', 1, 10, 1, 10)")
            db.execute("""CREATE TABLE posts(channel_id INTEGER NOT NULL REFERENCES channels(channel_id),
                          message_id INTEGER NOT NULL, published_at INTEGER NOT NULL, edited_at INTEGER,
                          text TEXT NOT NULL, media_type TEXT, grouped_id INTEGER,
                          PRIMARY KEY(channel_id,message_id))""")
            db.execute("INSERT INTO posts VALUES(12345, 10, 1000, 1500, 'existing edited snapshot', NULL, NULL)")

    def test_legacy_migration_is_conservative_idempotent_and_preserves_cursors(self):
        with patch("kiwoom_autotrader.channel_store.time.time", return_value=5000):
            with ChannelStore(self.directory, 101) as store:
                row = store.versions()[0]
                self.assertEqual(row["origin"], "legacy_snapshot")
                self.assertEqual(row["observed_at"], 5000)
                self.assertEqual(row["first_observed_at"], 5000)
                self.assertEqual(store.point_in_time(4999), [])
                self.assertEqual(store.point_in_time(5000)[0]["text"], "existing edited snapshot")
                self.assertEqual(store.channels()[0]["history_cursor"], 10)
                self.assertTrue(store.channels()[0]["history_complete"])
                revision = store.dataset_revision()
        with patch("kiwoom_autotrader.channel_store.time.time", return_value=6000):
            with ChannelStore(self.directory, 101) as store:
                self.assertEqual(len(store.versions()), 1)
                self.assertEqual(store.versions()[0]["observed_at"], 5000)
                self.assertEqual(store.dataset_revision(), revision)
                store.commit_page(12345, [post(edited_at=1600, text="new live edit")])
                self.assertEqual(store.versions()[1]["first_observed_at"], 5000)
                self.assertEqual(store.versions()[1]["origin"], "observed")

    def test_migration_failure_rolls_back_schema_and_preserves_legacy_content(self):
        with patch("kiwoom_autotrader.channel_store._insert_version", side_effect=sqlite3.IntegrityError("private SQL")):
            with self.assertRaises(ChannelStoreError) as caught:
                ChannelStore(self.directory, 101)
        self.assertNotIn("private SQL", str(caught.exception))
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute("SELECT version FROM archive_meta").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT text FROM posts").fetchone()[0], "existing edited snapshot")
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='post_versions'").fetchone())
            self.assertNotIn("dataset_revision", [row[1] for row in db.execute("PRAGMA table_info(archive_meta)")])


if __name__ == "__main__":
    unittest.main()
