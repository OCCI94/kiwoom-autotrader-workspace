"""Read-only collector checks using fake sessions, channels and page responses."""

import asyncio
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from kiwoom_autotrader import telegram_collector as collector


_CONFIG = SimpleNamespace(user_id=777, session="fixture-memory-session")


def entity(identifier=123, **kwargs):
    return SimpleNamespace(id=identifier, broadcast=True, megagroup=False, left=False,
                           access_hash=987654, title="Selected channel", username="fixture_news", **kwargs)


def message(identifier, channel_id=123, text="fixture-channel-text"):
    return SimpleNamespace(
        id=identifier, peer_id=SimpleNamespace(channel_id=channel_id),
        date=datetime(2026, 1, 1, tzinfo=timezone.utc), edit_date=None,
        message=text, media=None, grouped_id=None,
    )


class FakeClient:
    def __init__(self, ids=()):
        self.messages = {identifier: message(identifier) for identifier in ids}
        self.calls = []
        self.get_entity = AsyncMock(return_value=entity())
        self.connect = AsyncMock()
        self.disconnect = AsyncMock()
        self.is_user_authorized = AsyncMock(return_value=True)
        self.get_me = AsyncMock(return_value=SimpleNamespace(id=777, bot=False))
        self.session = SimpleNamespace(save=lambda: _CONFIG.session)
        self.dialogs = [SimpleNamespace(entity=entity())]
        self.error = None

    async def iter_dialogs(self):
        for dialog in self.dialogs:
            yield dialog

    async def iter_messages(self, peer, **kwargs):
        self.calls.append((peer, kwargs))
        if self.error is not None:
            raise self.error
        ids = sorted(self.messages, reverse=not kwargs.get("reverse", False))
        ids = [i for i in ids if i > kwargs.get("min_id", 0)]
        if kwargs.get("offset_id"):
            ids = [i for i in ids if i < kwargs["offset_id"]]
        for identifier in ids[:kwargs["limit"]]:
            yield self.messages[identifier]


class FakeStore:
    def __init__(self):
        self.channel = {
            "channel_id": 123, "access_hash": 987654, "title": "Selected channel",
            "username": "fixture_news", "latest_id": 0,
            "history_cursor": None, "history_complete": False,
        }
        self.rows = {}
        self.commits = []
        self.empty = False

    def channels(self):
        return [] if self.empty else [dict(self.channel)]

    def commit_page(self, channel_id, rows, **kwargs):
        if channel_id != 123:
            raise ValueError("Wrong source")
        self.commits.append((channel_id, rows, kwargs))
        for row in rows:
            self.rows[row["message_id"]] = row
        for key, value in kwargs.items():
            if value is not None:
                self.channel[key] = value

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class CollectorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        export_patch = patch("kiwoom_autotrader.utils.message_dataset.export_daily_dataset", return_value={})
        self.addCleanup(export_patch.stop)
        self.dataset_export = export_patch.start()
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.client = FakeClient(range(1, 251))
        self.store = FakeStore()
        self.patch("make_client", return_value=self.client)
        self.patch("load_account_config", return_value=_CONFIG)
        self.patch("_store", return_value=self.store)
        self.patch("_input_peer", side_effect=lambda channel: channel["channel_id"])
        self.patch("asyncio.sleep", new=AsyncMock())
        self.patch("time.time", return_value=1000)
        bot = patch("kiwoom_autotrader.telegram_account.load_telegram_config", return_value=SimpleNamespace(chat_id=777))
        self.addCleanup(bot.stop)
        self.bot_config = bot.start()
        library = patch("kiwoom_autotrader.telegram_account._telethon", return_value=(None, None, SimpleNamespace(
            FloodWaitError=type("FixtureFloodWait", (Exception,), {}),
        )))
        self.addCleanup(library.stop)
        library.start()
        printer = patch("builtins.print")
        self.addCleanup(printer.stop)
        self.printer = printer.start()

    def patch(self, name, **kwargs):
        patcher = patch("kiwoom_autotrader.telegram_collector." + name, **kwargs)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def status(self):
        return json.loads((self.directory / "collector-status.json").read_text())

    async def test_list_only_joined_broadcast_channels_without_messages(self):
        group = entity()
        group.broadcast = False
        group.megagroup = True
        left = entity()
        left.left = True
        private = SimpleNamespace(id=20, title="person")
        self.client.dialogs += [SimpleNamespace(entity=item) for item in (group, left, private)]
        channels = await collector.list_account_channels(_CONFIG)
        self.assertEqual(len(channels), 1)
        self.assertEqual(channels[0]["channel_id"], 123)
        self.assertEqual(self.client.calls, [])
        self.client.disconnect.assert_awaited_once()

    async def test_list_identity_mismatch_never_enumerates_channels(self):
        self.client.get_me.return_value = SimpleNamespace(id=999, bot=False)
        with self.assertRaises(collector.CollectorError):
            await collector.list_account_channels(_CONFIG)
        self.client.disconnect.assert_awaited_once()

    async def test_list_must_match_previously_paired_private_account(self):
        self.bot_config.return_value = SimpleNamespace(chat_id=999)
        with self.assertRaises(collector.CollectorError):
            await collector.list_account_channels(_CONFIG)

    async def test_initial_history_pages_are_committed_with_cursors(self):
        self.assertEqual(await collector.collect_once(self.client, self.store), 100)
        self.assertEqual(self.store.channel["latest_id"], 250)
        self.assertEqual(self.store.channel["history_cursor"], 151)
        self.assertFalse(self.store.channel["history_complete"])
        await collector.collect_once(self.client, self.store)
        self.assertEqual(self.store.channel["history_cursor"], 51)
        await collector.collect_once(self.client, self.store)
        self.assertTrue(self.store.channel["history_complete"])
        self.assertEqual(set(self.store.rows), set(range(1, 251)))
        self.assertTrue(all(call[1]["limit"] == 100 for call in self.client.calls))

    async def test_forward_backlog_over_page_size_never_skips_oldest_unread(self):
        await collector.collect_once(self.client, self.store)
        self.client.messages.update({i: message(i) for i in range(251, 601)})
        for _ in range(4):
            await collector.collect_once(self.client, self.store)
        self.assertEqual(set(self.store.rows), set(range(1, 601)))
        self.assertEqual(self.store.channel["latest_id"], 600)
        forward = [kwargs for _, kwargs in self.client.calls if "min_id" in kwargs]
        self.assertTrue(all(kwargs["reverse"] for kwargs in forward))
        self.assertEqual([kwargs["min_id"] for kwargs in forward], [250, 350, 450, 550])

    async def test_empty_channel_can_later_receive_first_post(self):
        self.client.messages = {}
        await collector.collect_once(self.client, self.store)
        self.assertTrue(self.store.channel["history_complete"])
        self.client.messages[5] = message(5)
        await collector.collect_once(self.client, self.store)
        self.assertEqual(self.store.channel["latest_id"], 5)
        self.assertIn(5, self.store.rows)

    async def test_other_chat_or_group_is_never_archived(self):
        wrong = self.client.messages[250]
        wrong.peer_id.channel_id = 999
        with self.assertRaises(collector.CollectorError):
            await collector.collect_once(self.client, self.store)
        self.assertEqual(self.store.commits, [])
        self.client.get_entity.return_value.broadcast = False
        with self.assertRaises(collector.CollectorError):
            await collector.collect_once(self.client, self.store)

    async def test_stop_between_pages_keeps_history_checkpoint_unchanged(self):
        await collector.collect_once(self.client, self.store)
        checks = iter((False, True))
        await collector.collect_once(self.client, self.store, stop_requested=lambda: next(checks))
        self.assertEqual(self.store.channel["history_cursor"], 151)

    async def test_runtime_status_has_only_counts_and_no_body_or_secrets(self):
        self.assertEqual(await collector.run_collector(self.directory, max_cycles=1), 0)
        status = self.status()
        self.assertEqual(status["status"], "stopped")
        self.assertEqual(status["cycle_count"], 1)
        self.assertEqual(status["fetched_count"], 100)
        serialized = (self.directory / "collector-status.json").read_text()
        for text in ("fixture-channel-text", "fixture_news", "987654", "777"):
            self.assertNotIn(text, serialized)
        self.assertEqual(stat.S_IMODE((self.directory / "collector-status.json").stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((self.directory / "collector.lock").stat().st_mode), 0o600)
        self.client.disconnect.assert_awaited_once()

    async def test_missing_account_does_not_create_client_or_login(self):
        self.patch("load_account_config", return_value=None)
        factory = self.patch("make_client")
        with self.assertRaises(collector.CollectorError):
            await collector.run_collector(self.directory, max_cycles=1)
        factory.assert_not_called()

    async def test_unselected_channels_fail_without_reading_posts(self):
        self.store.empty = True
        with self.assertRaises(collector.CollectorError):
            await collector.run_collector(self.directory, max_cycles=1)
        self.assertEqual(self.client.calls, [])

    async def test_expired_session_never_auto_signs_in(self):
        self.client.is_user_authorized.return_value = False
        with self.assertRaises(collector.CollectorError):
            await collector.run_collector(self.directory, max_cycles=1)
        self.assertEqual(self.client.calls, [])
        self.assertEqual(self.status()["status"], "failed")

    async def test_flood_wait_is_persisted_before_exit_and_honored_after_restart(self):
        FloodWaitError = type("FloodWaitError", (Exception,), {})
        error = FloodWaitError("secret-remote-data")
        error.seconds = 900
        self.client.error = error
        self.assertEqual(await collector.run_collector(self.directory, max_cycles=1), 0)
        self.assertEqual(self.status()["retry_after"], 1901)
        self.client.connect.reset_mock()
        wait = self.patch("_wait_until", new=AsyncMock(return_value=False))
        self.assertEqual(await collector.run_collector(self.directory, max_cycles=1), 0)
        wait.assert_awaited_once_with(1901, self.directory / "collector-stop.request")
        self.client.connect.assert_not_awaited()

    async def test_single_receiver_lock_rejects_duplicate_instance(self):
        lock = os.open(self.directory / "collector.lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(collector.CollectorError):
                await collector.run_collector(self.directory, max_cycles=1)
            self.client.connect.assert_not_awaited()
        finally:
            os.close(lock)

    async def test_network_error_is_sanitized_and_checkpoint_not_advanced(self):
        self.client.error = RuntimeError("secret-remote-data")
        with self.assertRaises(collector.CollectorError) as caught:
            await collector.run_collector(self.directory, max_cycles=1)
        self.assertNotIn("secret-remote-data", str(caught.exception))
        self.assertNotIn("secret-remote-data", (self.directory / "collector-status.json").read_text())
        self.assertEqual(self.store.commits, [])

    async def test_media_is_metadata_only_and_caption_is_preserved(self):
        post = message(9, text="caption")
        post.media = type("MessageMediaPhoto", (), {})()
        post.grouped_id = 3456
        post.edit_date = datetime(2026, 1, 2, tzinfo=timezone.utc)
        row = collector._message_row(post, 123)
        self.assertEqual(row["text"], "caption")
        self.assertEqual(row["media_type"], "photo")
        self.assertEqual(row["grouped_id"], 3456)
        self.assertEqual(row["edited_at"], int(post.edit_date.timestamp()))

    async def test_deselection_during_page_read_discards_page_without_failure(self):
        from kiwoom_autotrader.channel_store import ChannelStoreError
        def no_longer_selected(*args, **kwargs):
            self.store.empty = True
            raise ChannelStoreError("not selected")
        self.store.commit_page = no_longer_selected
        self.assertEqual(await collector.collect_once(self.client, self.store), 0)
        self.assertEqual(self.store.rows, {})

    async def test_storage_error_for_still_selected_source_is_not_hidden(self):
        from kiwoom_autotrader.channel_store import ChannelStoreError
        def unavailable(*args, **kwargs):
            raise ChannelStoreError("unavailable")
        self.store.commit_page = unavailable
        with self.assertRaises(ChannelStoreError):
            await collector.collect_once(self.client, self.store)

    async def test_real_store_receives_complete_history_without_duplicate_posts(self):
        from kiwoom_autotrader.channel_store import ChannelStore
        with ChannelStore(self.directory, 777) as store:
            store.register_channel(123, 987654, "Selected channel", "fixture_news")
            for _ in range(4):
                await collector.collect_once(self.client, store)
            self.assertEqual(store.channels()[0]["latest_id"], 250)
            self.assertTrue(store.channels()[0]["history_complete"])
            self.assertEqual(len(store.recent(limit=5)), 5)

    async def test_sanitized_auth_rate_limit_keeps_machine_readable_delay(self):
        from kiwoom_autotrader.telegram_account import AccountConnectionError
        error = AccountConnectionError("safe message")
        error.retry_after_seconds = 360
        self.assertEqual(collector._flood_delay(error), 360)


if __name__ == "__main__":
    unittest.main()
