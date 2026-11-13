"""Private, per-account archive of explicitly selected Telegram channels.

The transport verifies that registered entities are broadcast channels before
calling this module. Only registered numeric channel IDs accept posts. Message
content is untrusted data: it is stored as text, never evaluated or logged.
Each page and its collection cursors commit in one durable transaction.
"""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
from kiwoom_autotrader.utils.platform_io import os, fcntl
import sys
from pathlib import Path
import re
import sqlite3
import stat
import time
from typing import Any


_ERROR = "텔레그램 채널 저장소를 안전하게 읽거나 기록하지 못했습니다."
_INVALID = "텔레그램 채널 또는 게시글 데이터 형식이 올바르지 않습니다."
_MAX_ID = (1 << 52) - 1
_MAX_SIGNED = (1 << 63) - 1
_MAX_DATE = 253402300799
_MESSAGE_FIELDS = frozenset({
    "message_id", "published_at", "edited_at", "text", "media_type", "grouped_id",
})


class ChannelStoreError(RuntimeError):
    """Sanitized failure without SQL, message content or access hashes."""


class ExternalStorageUnavailable(ChannelStoreError):
    """External archive failed a mount, identity, or connection check."""


def _positive(value: Any, maximum: int = _MAX_ID) -> bool:
    return type(value) is int and 0 < value <= maximum


def _signed(value: Any) -> bool:
    return type(value) is int and -_MAX_SIGNED - 1 <= value <= _MAX_SIGNED


def _validate_message(message: Any) -> tuple:
    if not isinstance(message, dict) or not set(message).issubset(_MESSAGE_FIELDS):
        raise ChannelStoreError(_INVALID)
    message_id, published_at = message.get("message_id"), message.get("published_at")
    edited_at, text = message.get("edited_at"), message.get("text")
    media_type, grouped_id = message.get("media_type"), message.get("grouped_id")
    if (
        not _positive(message_id)
        or not _positive(published_at, _MAX_DATE)
        or (edited_at is not None and (
            not _positive(edited_at, _MAX_DATE) or edited_at < published_at
        ))
        or not isinstance(text, str) or len(text) > 65536
        or (media_type is not None and (
            not isinstance(media_type, str)
            or re.fullmatch(r"[A-Za-z0-9_]{1,64}", media_type) is None
        ))
        or (grouped_id is not None and not _signed(grouped_id))
    ):
        raise ChannelStoreError(_INVALID)
    return message_id, published_at, edited_at, text, media_type, grouped_id


def _validate_channel(channel_id: Any, access_hash: Any, title: Any, username: Any) -> tuple:
    if (
        not _positive(channel_id) or not _signed(access_hash)
        or not isinstance(title, str) or not 1 <= len(title) <= 256
        or "\x00" in title
        or (username is not None and (
            not isinstance(username, str)
            or re.fullmatch(r"[A-Za-z0-9_]{1,32}", username) is None
        ))
    ):
        raise ChannelStoreError(_INVALID)
    return channel_id, access_hash, title, username


def _register(db: sqlite3.Connection, values: tuple) -> bool:
    previous = db.execute("SELECT title, username, active FROM channels WHERE channel_id=?", (values[0],)).fetchone()
    db.execute("""
        INSERT INTO channels(channel_id, access_hash, title, username) VALUES (?, ?, ?, ?)
        ON CONFLICT(channel_id) DO UPDATE SET
            access_hash=excluded.access_hash, title=excluded.title, username=excluded.username, active=1
    """, values)
    return previous is None or (previous["title"], previous["username"], previous["active"]) != (values[2], values[3], 1)


def _fingerprint(message: tuple) -> str:
    return hashlib.sha256(json.dumps(message, ensure_ascii=True, separators=(",", ":")).encode()).hexdigest()


def _bump_revision(db: sqlite3.Connection) -> None:
    db.execute("UPDATE archive_meta SET dataset_revision=dataset_revision+1")


def _observed_now(db: sqlite3.Connection) -> int:
    now = int(time.time())
    if not _positive(now, _MAX_DATE):
        raise ChannelStoreError(_ERROR)
    # A local clock correction must not backdate a revision already seen later.
    latest = db.execute("SELECT max(observed_at) FROM post_versions").fetchone()[0]
    return max(now, latest or 0)


def _insert_version(db: sqlite3.Connection, channel: dict, message: tuple, observed_at: int, origin: str) -> int:
    first = db.execute("SELECT min(first_observed_at) FROM post_versions WHERE channel_id=? AND message_id=?",
                       (channel["channel_id"], message[0])).fetchone()[0]
    cursor = db.execute("""
        INSERT INTO post_versions (
            channel_id, message_id, published_at, edited_at, text, media_type, grouped_id,
            observed_at, first_observed_at, origin, title, username, fingerprint
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (channel["channel_id"], *message, observed_at, first or observed_at, origin,
          channel["title"], channel["username"], _fingerprint(message)))
    return cursor.lastrowid


def _with_permalink(row: sqlite3.Row) -> dict:
    item = dict(row)
    path = item["username"] or f"c/{item['channel_id']}"
    item["permalink"] = f"https://t.me/{path}/{item['message_id']}"
    return item


class ChannelStore:
    """SQLite archive scoped to one verified Telegram user account.

    ``channels()`` includes channel access hashes for the authenticated reader;
    callers must not log or send these dictionaries verbatim. ``recent()`` and
    ``summary()`` exclude access hashes. This store never contains API secrets,
    login codes, phone numbers, sessions or entire Telegram update payloads.
    """

    def __init__(self, state_dir: Path, account_id: int):
        if not _positive(account_id):
            raise ChannelStoreError(_INVALID)
        self.account_id = account_id
        self._db: sqlite3.Connection | None = None
        self._external_archive = None
        self._archive_lock_fd = None
        try:
            directory = Path(state_dir)
            # External archives must be selected by a locally stored marker;
            # never create an unverified directory under /Volumes.
            if directory.is_absolute() and directory.parts[:2] == ('/', 'Volumes'):
                raise ExternalStorageUnavailable(_ERROR)
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            info = directory.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
                raise ChannelStoreError(_ERROR)
            directory.chmod(0o700)
            # Shared for the whole connection lifetime: a cutover takes an
            # exclusive lock, so no archive user can straddle the switch.
            # Windows compatibility wrapper does not implement shared locks.
            if sys.platform != "win32":
                lock_fd = os.open(directory / "channel-storage.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
                self._archive_lock_fd = lock_fd
                lock_info = os.fstat(lock_fd)
                if not stat.S_ISREG(lock_info.st_mode) or lock_info.st_uid != os.getuid():
                    raise ChannelStoreError(_ERROR)
                fcntl.flock(lock_fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            from kiwoom_autotrader.telegram.channel_storage import ExternalArchive, ExternalArchiveError, selected
            try:
                if selected(directory):
                    self._external_archive = ExternalArchive(directory, f"channels-{account_id}.sqlite3")
                    self.path = self._external_archive.path
                    self._db = self._external_archive.connect()
                else:
                    self.path = directory / f"channels-{account_id}.sqlite3"
                    fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
                    try:
                        info = os.fstat(fd)
                        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                            raise ChannelStoreError(_ERROR)
                        os.fchmod(fd, 0o600)
                    finally:
                        os.close(fd)
                    self._db = sqlite3.connect(self.path, timeout=5)
            except ExternalArchiveError:
                raise ExternalStorageUnavailable(_ERROR) from None
            self._db.row_factory = sqlite3.Row
            self._db.execute("PRAGMA foreign_keys=ON")
            self._db.execute("PRAGMA journal_mode=DELETE")
            self._db.execute("PRAGMA synchronous=FULL")
            self._initialize()
        except (OSError, ValueError, TypeError, sqlite3.Error):
            self.close()
            raise ChannelStoreError(_ERROR) from None
        except ChannelStoreError:
            self.close()
            raise

    def __enter__(self) -> "ChannelStore":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def close(self) -> None:
        db, self._db = self._db, None
        error = None
        if db is not None:
            try:
                db.close()
            except sqlite3.Error:
                error = ChannelStoreError(_ERROR)
        lock_fd, self._archive_lock_fd = self._archive_lock_fd, None
        if lock_fd is not None:
            try:
                os.close(lock_fd)
            except OSError:
                error = ChannelStoreError(_ERROR)
        if error is not None:
            raise error from None

    def _connection(self) -> sqlite3.Connection:
        if self._db is None:
            raise ChannelStoreError("텔레그램 채널 저장소가 닫혀 있습니다.")
        if self._external_archive is not None:
            from kiwoom_autotrader.telegram.channel_storage import ExternalArchiveError
            try:
                self._external_archive.verify()
            except ExternalArchiveError:
                raise ExternalStorageUnavailable(_ERROR) from None
        return self._db

    def check_storage_health(self) -> None:
        if self._external_archive is not None:
            from kiwoom_autotrader.telegram.channel_storage import ExternalArchiveError
            try:
                self._external_archive.quick_verify()
            except ExternalArchiveError:
                raise ExternalStorageUnavailable(_ERROR) from None

    @contextmanager
    def _transaction(self):
        try:
            with self._connection() as db:
                db.execute("BEGIN IMMEDIATE")
                yield db
        except sqlite3.Error:
            raise ChannelStoreError(_ERROR) from None

    def _initialize(self) -> None:
        with self._transaction() as db:
            db.execute("CREATE TABLE IF NOT EXISTS archive_meta (account_id INTEGER NOT NULL, version INTEGER NOT NULL)")
            rows = db.execute("SELECT account_id, version FROM archive_meta").fetchall()
            if not rows:
                db.execute("INSERT INTO archive_meta(account_id, version) VALUES (?, 1)", (self.account_id,))
            elif len(rows) != 1 or rows[0]["account_id"] != self.account_id or rows[0]["version"] not in {1, 2}:
                raise ChannelStoreError(_ERROR)
            if "dataset_revision" not in {row["name"] for row in db.execute("PRAGMA table_info(archive_meta)")}:
                db.execute("ALTER TABLE archive_meta ADD COLUMN dataset_revision INTEGER NOT NULL DEFAULT 0")
            db.execute("""
                CREATE TABLE IF NOT EXISTS channels (
                    channel_id INTEGER PRIMARY KEY,
                    access_hash INTEGER NOT NULL,
                    title TEXT NOT NULL,
                    username TEXT,
                    active INTEGER NOT NULL DEFAULT 1,
                    history_cursor INTEGER,
                    history_complete INTEGER NOT NULL DEFAULT 0,
                    latest_id INTEGER NOT NULL DEFAULT 0
                )
            """)
            if "active" not in {row["name"] for row in db.execute("PRAGMA table_info(channels)")}:
                db.execute("ALTER TABLE channels ADD COLUMN active INTEGER NOT NULL DEFAULT 1")
            db.execute("""
                CREATE TABLE IF NOT EXISTS posts (
                    channel_id INTEGER NOT NULL REFERENCES channels(channel_id),
                    message_id INTEGER NOT NULL,
                    published_at INTEGER NOT NULL,
                    edited_at INTEGER,
                    text TEXT NOT NULL,
                    media_type TEXT,
                    grouped_id INTEGER,
                    PRIMARY KEY(channel_id, message_id)
                )
            """)
            db.execute("CREATE INDEX IF NOT EXISTS posts_recent ON posts(published_at DESC, message_id DESC)")
            db.execute("""
                CREATE TABLE IF NOT EXISTS post_versions (
                    version_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    channel_id INTEGER NOT NULL,
                    message_id INTEGER NOT NULL,
                    published_at INTEGER NOT NULL,
                    edited_at INTEGER,
                    text TEXT NOT NULL,
                    media_type TEXT,
                    grouped_id INTEGER,
                    observed_at INTEGER NOT NULL,
                    first_observed_at INTEGER NOT NULL,
                    origin TEXT NOT NULL CHECK(origin IN ('observed', 'legacy_snapshot')),
                    title TEXT NOT NULL,
                    username TEXT,
                    fingerprint TEXT NOT NULL,
                    UNIQUE(channel_id, message_id, fingerprint),
                    FOREIGN KEY(channel_id, message_id) REFERENCES posts(channel_id, message_id)
                )
            """)
            db.execute("CREATE INDEX IF NOT EXISTS post_versions_asof ON post_versions(observed_at, version_id)")
            db.execute("CREATE INDEX IF NOT EXISTS post_versions_identity ON post_versions(channel_id, message_id, observed_at, version_id)")
            db.execute("""CREATE TRIGGER IF NOT EXISTS post_versions_no_update BEFORE UPDATE ON post_versions
                          BEGIN SELECT RAISE(ABORT, 'post versions are immutable'); END""")
            db.execute("""CREATE TRIGGER IF NOT EXISTS post_versions_no_delete BEFORE DELETE ON post_versions
                          BEGIN SELECT RAISE(ABORT, 'post versions are immutable'); END""")
            # Additive metadata table: old versions remain untouched and unknown.
            db.execute("""CREATE TABLE IF NOT EXISTS event_collection_provenance (
                version_id INTEGER PRIMARY KEY REFERENCES post_versions(version_id),
                batch_id TEXT NOT NULL, page_index INTEGER NOT NULL,
                batch_kind TEXT NOT NULL, first_seen_at INTEGER NOT NULL,
                recorded_at INTEGER NOT NULL, observed_delay_seconds INTEGER NOT NULL,
                clock_valid INTEGER NOT NULL CHECK(clock_valid IN (0,1)),
                first_version INTEGER NOT NULL CHECK(first_version IN (0,1))
            )""")
            db.execute("""CREATE TRIGGER IF NOT EXISTS provenance_no_update BEFORE UPDATE ON event_collection_provenance
                          BEGIN SELECT RAISE(ABORT, 'provenance is immutable'); END""")
            db.execute("""CREATE TRIGGER IF NOT EXISTS provenance_no_delete BEFORE DELETE ON event_collection_provenance
                          BEGIN SELECT RAISE(ABORT, 'provenance is immutable'); END""")
            legacy = db.execute("""
                SELECT posts.*, channels.title, channels.username
                FROM posts JOIN channels USING(channel_id)
                WHERE NOT EXISTS (SELECT 1 FROM post_versions v
                                  WHERE v.channel_id=posts.channel_id AND v.message_id=posts.message_id)
            """).fetchall()
            if legacy:
                observed_at = _observed_now(db)
                for row in legacy:
                    message = tuple(row[key] for key in (
                        "message_id", "published_at", "edited_at", "text", "media_type", "grouped_id",
                    ))
                    _insert_version(db, row, message, observed_at, "legacy_snapshot")
                _bump_revision(db)
            db.execute("UPDATE archive_meta SET version=2")

    def register_channel(
        self, channel_id: int, access_hash: int, title: str, username: str | None = None,
    ) -> None:
        """Register a caller-verified broadcast channel, preserving cursors/posts."""
        values = _validate_channel(channel_id, access_hash, title, username)
        with self._transaction() as db:
            if _register(db, values):
                _bump_revision(db)

    def set_selected(self, channels: list[dict]) -> None:
        """Replace the selected channel set atomically, retaining all archives."""
        if not isinstance(channels, list) or len(channels) > 1000:
            raise ChannelStoreError(_INVALID)
        values, seen = [], set()
        for source in channels:
            if not isinstance(source, dict):
                raise ChannelStoreError(_INVALID)
            item = _validate_channel(
                source.get("channel_id"), source.get("access_hash"),
                source.get("title"), source.get("username"),
            )
            if item[0] in seen:
                raise ChannelStoreError(_INVALID)
            seen.add(item[0])
            values.append(item)
        with self._transaction() as db:
            previous = {(row["channel_id"], row["title"], row["username"]) for row in
                        db.execute("SELECT channel_id, title, username FROM channels WHERE active=1")}
            db.execute("UPDATE channels SET active=0")
            for source in values:
                _register(db, source)
            if previous != {(item[0], item[2], item[3]) for item in values}:
                _bump_revision(db)

    def channels(self, include_inactive: bool = False) -> list[dict]:
        """Return registered sources, including reader-only access hashes."""
        if type(include_inactive) is not bool:
            raise ChannelStoreError(_INVALID)
        try:
            rows = self._connection().execute(
                "SELECT * FROM channels WHERE active=1 OR ? ORDER BY channel_id", (include_inactive,),
            ).fetchall()
            return [{**dict(row), "history_complete": bool(row["history_complete"]),
                     "active": bool(row["active"])} for row in rows]
        except sqlite3.Error:
            raise ChannelStoreError(_ERROR) from None

    def commit_page(
        self, channel_id: int, messages: list[dict], *,
        history_cursor: int | None = None, history_complete: bool | None = None,
        latest_id: int | None = None, collection_kind: str | None = None,
        page_index: int | None = None, batch_id: str | None = None,
    ) -> None:
        """Atomically upsert a page and advance its caller-supplied cursors.

        Historical cursors only move toward older IDs; latest_id only increases.
        A completed history cannot be reopened by an older concurrent collector.
        None leaves a cursor unchanged. Empty pages can mark history complete.
        Each newly observed revision is retained before its current snapshot
        changes. Existing content is never assigned a guessed past observation.
        """
        if (
            not _positive(channel_id)
            or not isinstance(messages, list) or len(messages) > 1000
            or (history_cursor is not None and not _positive(history_cursor))
            or (history_complete is not None and type(history_complete) is not bool)
            or (latest_id is not None and (
                type(latest_id) is not int or not 0 <= latest_id <= _MAX_ID
            ))
            or (collection_kind is None and (page_index is not None or batch_id is not None))
            or (collection_kind is not None and (
                collection_kind not in {'latest_page', 'forward_page', 'backfill_page'}
                or type(page_index) is not int or not 1 <= page_index <= 1000000
                or not isinstance(batch_id, str) or re.fullmatch(r'[0-9a-f]{32}', batch_id) is None
            ))
        ):
            raise ChannelStoreError(_INVALID)
        validated = [_validate_message(message) for message in messages]
        with self._transaction() as db:
            source = db.execute("SELECT * FROM channels WHERE channel_id=?", (channel_id,)).fetchone()
            if source is None or not source["active"]:
                raise ChannelStoreError("수집 대상으로 등록되지 않은 텔레그램 채널입니다.")
            observed_at = _observed_now(db) if validated else None
            changed = False
            for message in validated:
                previous = db.execute("SELECT * FROM posts WHERE channel_id=? AND message_id=?",
                                      (channel_id, message[0])).fetchone()
                if previous is not None:
                    if message[1] != previous["published_at"]:
                        raise ChannelStoreError("기존 게시글의 최초 게시 시각이 달라 저장을 중단했습니다.")
                    if (message[2] or 0) < (previous["edited_at"] or 0):
                        continue
                fingerprint = _fingerprint(message)
                if db.execute("SELECT 1 FROM post_versions WHERE channel_id=? AND message_id=? AND fingerprint=?",
                              (channel_id, message[0], fingerprint)).fetchone() is not None:
                    # Replayed already-known content cannot revert the current
                    # snapshot, including conflicting edits within one second.
                    continue
                db.execute("""
                    INSERT INTO posts(channel_id, message_id, published_at, edited_at, text, media_type, grouped_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(channel_id, message_id) DO UPDATE SET
                        edited_at=excluded.edited_at, text=excluded.text,
                        media_type=excluded.media_type, grouped_id=excluded.grouped_id
                """, (channel_id, *message))
                version_id = _insert_version(db, source, message, observed_at, "observed")
                if collection_kind is not None:
                    first = db.execute("SELECT first_observed_at FROM post_versions WHERE version_id=?", (version_id,)).fetchone()[0]
                    # _observed_now clamps backwards clock steps for monotonicity;
                    # clamping must not masquerade as a low-latency observation.
                    clock_valid = int(int(time.time()) >= observed_at)
                    db.execute("""INSERT INTO event_collection_provenance
                      (version_id,batch_id,page_index,batch_kind,first_seen_at,recorded_at,observed_delay_seconds,clock_valid,first_version)
                      VALUES (?,?,?,?,?,?,?,?,?)""", (version_id,batch_id,page_index,collection_kind,
                                                  first,observed_at,observed_at-message[1],clock_valid,int(previous is None)))
                changed = True
            cursor = source["history_cursor"]
            if history_cursor is not None:
                cursor = history_cursor if cursor is None else min(cursor, history_cursor)
            complete = bool(source["history_complete"]) or history_complete is True
            latest = max(source["latest_id"], latest_id if latest_id is not None else 0)
            db.execute("""
                UPDATE channels SET history_cursor=?, history_complete=?, latest_id=? WHERE channel_id=?
            """, (cursor, int(complete), latest, channel_id))
            if changed or (cursor, int(complete), latest) != (source["history_cursor"], source["history_complete"], source["latest_id"]):
                _bump_revision(db)

    def dataset_revision(self) -> int:
        """A cache invalidation counter covering content, selection and coverage."""
        try:
            return self._connection().execute("SELECT dataset_revision FROM archive_meta").fetchone()[0]
        except sqlite3.Error:
            raise ChannelStoreError(_ERROR) from None

    def versions(self, as_of: int | None = None, active_only: bool = True) -> list[dict]:
        """All retained observations in deterministic observation/version order.

        Channel names/usernames are snapshots from observation time. The active
        source set is the CURRENT selection, not a historical source universe;
        pass active_only=False for all archived sources. Legacy snapshots become
        available at migration time, not at their old publication timestamp.
        as_of filters observation time only; raw future source timestamps are
        retained for diagnostics. Use point_in_time for an availability filter.
        """
        if type(active_only) is not bool or (as_of is not None and (
            type(as_of) is not int or not 0 <= as_of <= _MAX_DATE
        )):
            raise ChannelStoreError(_INVALID)
        try:
            rows = self._connection().execute("""
                SELECT v.version_id, v.channel_id, v.message_id, v.published_at, v.edited_at,
                       v.text, v.media_type, v.grouped_id, v.observed_at, v.first_observed_at,
                       v.origin, v.title, v.username
                FROM post_versions v JOIN channels c ON c.channel_id=v.channel_id
                WHERE (c.active=1 OR ?=0)
                  AND (? IS NULL OR v.observed_at<=?)
                ORDER BY v.observed_at ASC, v.version_id ASC
            """, (int(active_only), as_of, as_of)).fetchall()
            return [_with_permalink(row) for row in rows]
        except sqlite3.Error:
            raise ChannelStoreError(_ERROR) from None

    def point_in_time(self, as_of: int, active_only: bool = True) -> list[dict]:
        """Latest known revision per message at a UTC epoch-second cutoff.

        A historical post collected today is absent from yesterday's result.
        Same-second revisions are ordered by monotonically increasing version_id.
        Full historical deletion and channel-membership reconstruction are not
        implied by this append-only local observation ledger.
        """
        if type(as_of) is not int or not 0 <= as_of <= _MAX_DATE:
            raise ChannelStoreError(_INVALID)
        latest = {}
        for row in self.versions(as_of=as_of, active_only=active_only):
            if max(row["published_at"], row["edited_at"] or row["published_at"]) > as_of:
                continue
            latest[(row["channel_id"], row["message_id"])] = row
        return sorted(latest.values(), key=lambda row: (row["published_at"], row["channel_id"], row["message_id"]))

    def recent(self, limit: int = 5, channel_id: int | None = None) -> list[dict]:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ChannelStoreError(_INVALID)
        if channel_id is not None and not _positive(channel_id):
            raise ChannelStoreError(_INVALID)
        try:
            rows = self._connection().execute("""
                SELECT posts.*, channels.title, channels.username
                FROM posts JOIN channels USING(channel_id)
                WHERE channels.active=1 AND (? IS NULL OR posts.channel_id=?)
                ORDER BY published_at DESC, message_id DESC, channel_id ASC LIMIT ?
            """, (channel_id, channel_id, limit)).fetchall()
            output = []
            for row in rows:
                item = dict(row)
                path = item["username"] or f"c/{item['channel_id']}"
                item["permalink"] = f"https://t.me/{path}/{item['message_id']}"
                output.append(item)
            return output
        except sqlite3.Error:
            raise ChannelStoreError(_ERROR) from None

    def summary(self) -> dict:
        try:
            db = self._connection()
            channels = db.execute("SELECT count(*) FROM channels WHERE active=1").fetchone()[0]
            complete = db.execute("SELECT count(*) FROM channels WHERE active=1 AND history_complete=1").fetchone()[0]
            return {
                "channels": channels,
                "messages": db.execute("SELECT count(*) FROM posts JOIN channels USING(channel_id) WHERE active=1").fetchone()[0],
                "history_complete": complete,
                "history_pending": channels - complete,
            }
        except sqlite3.Error:
            raise ChannelStoreError(_ERROR) from None


Store = ChannelStore
