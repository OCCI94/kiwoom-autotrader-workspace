"""Bounded, read-only live catalyst preparation for an injected async scorer.

No network, credentials, orders, broker IPC, database writes or process control.
Historical ticker-link research is not a live signal: a new immutable version,
verified availability, current identity mapping and short TTL are required.
The downstream scorer is advisory and cannot approve an order or DART risk.
"""
from __future__ import annotations

from collections import Counter, OrderedDict
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import time

from .ticker_entity_linker import TickerLinker, timestamp

SCHEMA = 'catalyst-pre-scoring-v1'
MAX_TEXT_BYTES = 32768
MAX_DART_TEXT_BYTES = 512 * 1024
MAX_MODEL_TEXT_BYTES = 4096
SHA = re.compile(r'[0-9a-f]{64}')
CODE = re.compile(r'[0-9]{6}')
MOUNT = Path('/Volumes/AIStockData')
VOLUME_UUID = '7440E396-8F62-4205-9EC6-16F02440C7C5'
# Defensive redaction, not a general secret detector. No raw text is persisted
# by this module. Authentication must be supplied only by the scorer itself.
PRIVATE = re.compile(r'(?i)(?:ts[sc]k_live_[A-Za-z0-9]+|sk-[A-Za-z0-9_-]{16,}|'
                     r'(?:api[_ -]?key|client[_ -]?secret|authorization|bearer)\s*[:= ]+[^\s,;]+)')


def _time(value):
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError('CATALYST_TIME_INVALID')
        return value.astimezone(timezone.utc)
    result = timestamp(value)
    if result is None:
        raise ValueError('CATALYST_TIME_INVALID')
    return result.astimezone(timezone.utc)


def _digest(value):
    return hashlib.sha256(value.encode() if isinstance(value, str) else value).hexdigest()


def _canon(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False).encode()


def _safe_text(text, *, limit=MAX_TEXT_BYTES):
    if not isinstance(text, str) or not text or len(text.encode()) > limit:
        raise ValueError('CATALYST_TEXT_INVALID')
    redacted = PRIVATE.sub('[REDACTED]', text)
    raw = redacted.encode()
    # A prefix may be insufficient for a full filing. The partial flag is part
    # of the payload and never means a completed disclosure-risk assessment.
    snippet = raw[:MAX_MODEL_TEXT_BYTES].decode('utf-8', errors='ignore')
    return snippet, len(raw) <= MAX_MODEL_TEXT_BYTES, redacted != text


class _ReadonlySSDGuard:
    """Hold verified SSD identity for a read transaction; no file creation."""
    def __enter__(self):
        self.fd = os.open(MOUNT, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        info = os.fstat(self.fd)
        self.identity = (info.st_dev, info.st_ino)
        try:
            self.verify()
        except Exception:
            os.close(self.fd)
            raise
        return self

    def verify(self):
        info = os.stat(MOUNT, follow_symlinks=False)
        if (not os.path.ismount(MOUNT) or info.st_dev == os.stat(MOUNT.parent).st_dev
                or (info.st_dev, info.st_ino) != self.identity):
            raise ValueError('CATALYST_SSD_IDENTITY_INVALID')
        fd = os.open('.ssd-volume-sentinel', os.O_RDONLY | os.O_NOFOLLOW, dir_fd=self.fd)
        try:
            row = os.fstat(fd)
            if (not stat.S_ISREG(row.st_mode) or row.st_size > 128
                    or os.read(fd, 129).decode('ascii').strip() != VOLUME_UUID):
                raise ValueError('CATALYST_SSD_SENTINEL_INVALID')
        finally:
            os.close(fd)

    def __exit__(self, *args):
        os.close(self.fd)


def load_linker(path, expected_sha256):
    """Hash-pinned, owner-controlled existing exact-name master only."""
    if not SHA.fullmatch(str(expected_sha256)):
        raise ValueError('CATALYST_MASTER_HASH_INVALID')
    fd = os.open(Path(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
                or before.st_mode & 0o022 or before.st_size > 4 * 1024 * 1024):
            raise ValueError('CATALYST_MASTER_UNSAFE')
        raw = os.read(fd, 4 * 1024 * 1024 + 1)
        after = os.fstat(fd)
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
                after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
            raise ValueError('CATALYST_MASTER_CHANGED')
    finally:
        os.close(fd)
    if _digest(raw) != expected_sha256:
        raise ValueError('CATALYST_MASTER_HASH_MISMATCH')
    value = json.loads(raw)
    records = value.get('records')
    if not isinstance(records, list) or not 1 <= len(records) <= 10000:
        raise ValueError('CATALYST_MASTER_INVALID')
    _time(value.get('observed_at'))
    return TickerLinker([r for r in records if r.get('market') == 'KR'],
                        master_available_at=value['observed_at'],
                        master_sha256=expected_sha256,
                        source='hash_pinned_saved_official_master')


def _read_regular(path, *, limit, expected_sha256=None):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
                or before.st_mode & 0o022 or before.st_size > limit):
            raise ValueError('CATALYST_SOURCE_FILE_UNSAFE')
        chunks, remaining = [], limit + 1
        while remaining:
            piece = os.read(fd, min(65536, remaining))
            if not piece:
                break
            chunks.append(piece)
            remaining -= len(piece)
        after = os.fstat(fd)
        linked = Path(path).stat(follow_symlinks=False)
        identity = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns)
        if identity(before) != identity(after) or identity(after) != identity(linked):
            raise ValueError('CATALYST_SOURCE_CHANGED')
    finally:
        os.close(fd)
    raw = b''.join(chunks)
    if len(raw) > limit or expected_sha256 is not None and _digest(raw) != expected_sha256:
        raise ValueError('CATALYST_SOURCE_HASH_OR_SIZE_INVALID')
    return raw


class DartOriginalQueueTail:
    """Read only newly archived original filings from the scheduled result.

    Current worker statuses omit archive_relative. Derive one immutable archive
    from hash-matched published risk evidence's DART proof paths, verify its
    result, then verify original-queue, original bytes and text hashes. No
    directory scan, network query, risk approval or historical replay occurs.
    """
    def __init__(self, status_path, *, max_documents=8, ttl_seconds=90,
                 archive_mount=MOUNT, mount_guard=None):
        if type(max_documents) is not int or not 1 <= max_documents <= 25:
            raise ValueError('CATALYST_DOCUMENT_BOUND_INVALID')
        if type(ttl_seconds) is not int or not 1 <= ttl_seconds <= 300:
            raise ValueError('CATALYST_TTL_INVALID')
        self.status_path = Path(status_path).absolute()
        self.risk_path = self.status_path.parent/'toss-risk-evidence.json'
        self.mount = Path(archive_mount).absolute()
        self.root = self.mount/'ai-stock-research-data'
        self.max_documents, self.ttl = max_documents, timedelta(seconds=ttl_seconds)
        self.mount_guard = mount_guard
        self.started_at = None
        self.seen = OrderedDict()
        self.last_status = {'stage': 'not_started', 'events': 0}

    def _path(self, path, *, kind):
        requested = Path(path)
        if not requested.is_absolute():
            requested = self.mount/requested
        resolved = requested.resolve(strict=True)
        base = self.root/kind
        if not requested.is_relative_to(base) or not resolved.is_relative_to(base.resolve(strict=True)):
            raise ValueError('CATALYST_ARCHIVE_PATH_ESCAPE')
        return resolved

    def read_new(self, *, now):
        now = _time(now)
        if self.started_at is None:
            self.started_at = now
        counts, events = Counter(), []
        try:
            status = json.loads(_read_regular(self.status_path, limit=512*1024))
            result = status.get('full_eligibility')
            if (status.get('schema') != 'toss-dart-intraday-refresh-status-v1'
                    or status.get('claim_result_recorded') is not True
                    or status.get('risk_promotions_blocked') is not False
                    or not isinstance(result, dict) or result.get('status') != 'published'):
                self.last_status = {'stage': 'no_published_scheduled_original_queue', 'events': 0}
                return []
            observed = _time(status['observed_at'])
            if not self.started_at <= observed <= now or now-observed > self.ttl:
                self.last_status = {'stage': 'prior_or_expired_queue_not_replayed', 'events': 0}
                return []
            digest = result.get('risk_sha256')
            if not SHA.fullmatch(str(digest)):
                raise ValueError('CATALYST_RISK_HASH_INVALID')
            risk = json.loads(_read_regular(self.risk_path, limit=256*1024, expected_sha256=digest))
            if not _time(risk['generated_at']) <= now < _time(risk['expires_at']):
                raise ValueError('CATALYST_RISK_SOURCE_EXPIRED')
            decisions = risk['evaluations']
            if not isinstance(decisions, list) or len(decisions) > 35:
                raise ValueError('CATALYST_RISK_SOURCE_INVALID')
            guard = self.mount_guard() if self.mount_guard else _ReadonlySSDGuard()
            with guard:
                directories = set()
                for decision in decisions:
                    for proof in decision.get('dart_list_proofs', []):
                        directories.add(self._path(proof['path'], kind='s5-full-eligibility').parent)
                if len(directories) != 1:
                    raise ValueError('CATALYST_ARCHIVE_IDENTITY_AMBIGUOUS')
                directory = directories.pop()
                original_result = json.loads(_read_regular(directory/'result.json', limit=2*1024*1024))
                if original_result.get('status') != 'published' or original_result.get('risk_sha256') != digest:
                    raise ValueError('CATALYST_ARCHIVE_RESULT_MISMATCH')
                queue = json.loads(_read_regular(directory/'review-queue.json', limit=2*1024*1024))
                rows = queue['rows']
                if not isinstance(rows, list) or len(rows) > 35 or _time(queue['created_at']) > now:
                    raise ValueError('CATALYST_QUEUE_INVALID')
                attempted = 0
                for row in rows:
                    if attempted >= self.max_documents:
                        break
                    meta = row.get('original_queue')
                    if not isinstance(meta, dict) or not SHA.fullmatch(str(meta.get('sha256'))):
                        counts['original_queue_missing'] += 1
                        continue
                    path = self._path(meta['path'], kind='s5-dart-original-queues')
                    original = json.loads(_read_regular(path, limit=2*1024*1024, expected_sha256=meta['sha256']))
                    if (original.get('schema') != 'toss-dart-original-review-queue-v1'
                            or original.get('symbol') != row.get('symbol')
                            or original.get('corp_code') != row.get('corp_code')
                            or not CODE.fullmatch(str(row.get('symbol')))):
                        raise ValueError('CATALYST_ORIGINAL_IDENTITY_INVALID')
                    listing = {r['rcept_no']: r for r in original.get('list_rows', [])}
                    documents = original.get('documents')
                    if not isinstance(documents, list) or len(documents) > 200:
                        raise ValueError('CATALYST_ORIGINAL_DOCUMENT_BOUND_INVALID')
                    for doc in documents:
                        if attempted >= self.max_documents:
                            break
                        receipt = doc.get('receipt')
                        if not re.fullmatch('[0-9]{14}', str(receipt)):
                            counts['receipt_invalid'] += 1
                            continue
                        key = (row['symbol'], receipt)
                        if key in self.seen:
                            continue
                        at = _time(doc['observed_at'])
                        if not self.started_at <= at <= now or now-at > self.ttl:
                            counts['old_original_not_replayed'] += 1
                            continue
                        attempted += 1
                        self.seen[key] = at
                        try:
                            record = listing[receipt]
                            if (record['rcept_dt'] != receipt[:8] or record['stock_code'] != row['symbol']
                                    or doc.get('text_extraction_complete') is not True
                                    or not SHA.fullmatch(str(doc.get('sha256')))
                                    or not SHA.fullmatch(str(doc.get('text_sha256')))):
                                raise ValueError('CATALYST_ORIGINAL_INCOMPLETE')
                            raw_path = self._path(doc['raw_path'], kind='s5-dart-original-queues')
                            text_path = self._path(doc['text_path'], kind='s5-dart-original-queues')
                            if path.parent not in raw_path.parents or path.parent not in text_path.parents:
                                raise ValueError('CATALYST_ORIGINAL_CROSS_QUEUE_PATH')
                            _read_regular(raw_path, limit=8*1024*1024, expected_sha256=doc['sha256'])
                            text = _read_regular(text_path, limit=MAX_DART_TEXT_BYTES,
                                                 expected_sha256=doc['text_sha256']).decode('utf-8')
                            event = {'symbol': row['symbol'], 'receipt_no': receipt,
                                     'symbol_identity_verified': True, 'original_text_complete': True,
                                     'new_receipt_observed': True, 'text': text, 'text_sha256': doc['text_sha256'],
                                     'first_observed_at': at.isoformat(), 'available_at': at.isoformat(),
                                     'published_at': None, 'publication_date': datetime.strptime(record['rcept_dt'], '%Y%m%d').date().isoformat()}
                            events.append(event)
                        except (KeyError, ValueError, OSError, UnicodeError):
                            counts['original_invalid_or_unavailable'] += 1
                guard.verify()
            while len(self.seen) > 2048:
                self.seen.popitem(last=False)
            self.last_status = {'stage': 'new_original_events', 'events': len(events),
                                'attempted': attempted, 'counts': dict(counts),
                                'risk_sha256': digest, 'body_logged': False, 'risk_approval': False}
            return events
        except (KeyError, TypeError, ValueError, OSError, UnicodeError, sqlite3.Error):
            self.last_status = {'stage': 'original_queue_unavailable', 'events': 0,
                                'body_logged': False, 'retry_performed': False}
            return []


class TelegramArchiveTail:
    """Append-only new-version cursor; first read primes at current maximum.

    Every read uses mode=ro/query_only/a single bounded transaction. Replaced
    or truncated databases reset to a new maximum without replaying history.
    Read this only on a background thread; never on the order-decision path.
    """
    def __init__(self, path, *, max_rows=256, query_deadline_seconds=0.10,
                 monotonic=time.monotonic, mount_guard=None):
        if type(max_rows) is not int or not 1 <= max_rows <= 1000:
            raise ValueError('CATALYST_ROW_BOUND_INVALID')
        if not 0 < query_deadline_seconds <= 1:
            raise ValueError('CATALYST_QUERY_BOUND_INVALID')
        self.path = Path(path).absolute()
        self.max_rows = max_rows
        self.query_deadline_seconds = query_deadline_seconds
        self.monotonic = monotonic
        self.mount_guard = mount_guard
        self.identity = None
        self.cursor = None
        self.last_status = {'stage': 'not_started', 'rows': 0}

    def _guard(self):
        if self.mount_guard:
            return self.mount_guard()
        if self.path.is_relative_to(MOUNT):
            # The guard holds the mount FD and verifies its UUID; no write
            # method is called. SQLite itself is strictly read-only.
            return _ReadonlySSDGuard()
        return nullcontext()

    def read_new(self):
        with self._guard() as guard:
            before = self.path.stat(follow_symlinks=False)
            if not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid():
                raise ValueError('CATALYST_ARCHIVE_UNSAFE')
            identity = (before.st_dev, before.st_ino)
            deadline = self.monotonic() + self.query_deadline_seconds
            db = sqlite3.connect(self.path.as_uri() + '?mode=ro', uri=True, timeout=0)
            db.row_factory = sqlite3.Row
            try:
                db.execute('PRAGMA query_only=ON')
                db.set_progress_handler(lambda: int(self.monotonic() > deadline), 1000)
                db.execute('BEGIN')
                definitions = {r['name']: ' '.join(r['sql'].upper().split()) for r in
                    db.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger' "
                               "AND name IN ('post_versions_no_update','post_versions_no_delete',"
                               "'provenance_no_update','provenance_no_delete')")}
                for name, clause in (('post_versions_no_update', 'BEFORE UPDATE ON POST_VERSIONS'),
                                     ('post_versions_no_delete', 'BEFORE DELETE ON POST_VERSIONS'),
                                     ('provenance_no_update', 'BEFORE UPDATE ON EVENT_COLLECTION_PROVENANCE'),
                                     ('provenance_no_delete', 'BEFORE DELETE ON EVENT_COLLECTION_PROVENANCE')):
                    if clause not in definitions.get(name, '') or 'RAISE(ABORT' not in definitions.get(name, ''):
                        raise ValueError('CATALYST_ARCHIVE_NOT_IMMUTABLE')
                maximum = db.execute('SELECT coalesce(max(version_id),0) FROM post_versions').fetchone()[0]
                if self.cursor is None or self.identity != identity or maximum < self.cursor:
                    rows, cursor, stage = [], maximum, 'primed_without_history_replay'
                else:
                    rows = [dict(row) for row in db.execute('''
                        SELECT v.version_id,v.channel_id,v.message_id,v.published_at,v.edited_at,
                               v.text,v.observed_at,v.origin,p.recorded_at,p.first_seen_at,
                               p.clock_valid,p.batch_kind
                        FROM post_versions v LEFT JOIN event_collection_provenance p
                        ON p.version_id=v.version_id WHERE v.version_id>? AND v.version_id<=?
                        ORDER BY v.version_id LIMIT ?''', (self.cursor, maximum, self.max_rows))]
                    cursor = rows[-1]['version_id'] if rows else self.cursor
                    stage = 'new_versions_read'
            finally:
                db.close()
            if guard is not None:
                guard.verify()
            after = self.path.stat(follow_symlinks=False)
            if (after.st_dev, after.st_ino) != identity:
                raise ValueError('CATALYST_ARCHIVE_REPLACED_DURING_READ')
            self.identity, self.cursor = identity, cursor
            self.last_status = {'stage': stage, 'rows': len(rows), 'cursor': cursor,
                                'observed_maximum': maximum, 'pending': maximum > cursor,
                                'read_only': True, 'body_logged': False}
            return rows


class CatalystPreScorer:
    """Prepare new immutable text versions for a bounded async submit callback.

    No interpreted probability is generated here. ``latest_for`` returns only
    a fresh snapshot identity used by AsyncJevDecision.lookup(kind='news', ...).
    A missing key, rejected queue, error or stale event leaves no usable score.
    """
    def __init__(self, linker, submit, *, session_id, ttl_seconds=90, max_entries=2048,
                 allowed_symbols=None):
        if not isinstance(linker, TickerLinker) or not callable(submit):
            raise ValueError('CATALYST_LINKER_OR_SUBMIT_INVALID')
        if not isinstance(session_id, str) or not 1 <= len(session_id) <= 128:
            raise ValueError('CATALYST_SESSION_INVALID')
        if type(ttl_seconds) is not int or not 1 <= ttl_seconds <= 300:
            raise ValueError('CATALYST_TTL_INVALID')
        if type(max_entries) is not int or not 1 <= max_entries <= 10000:
            raise ValueError('CATALYST_CACHE_BOUND_INVALID')
        self.linker, self.submit, self.session_id = linker, submit, session_id
        self.ttl, self.max_entries = timedelta(seconds=ttl_seconds), max_entries
        self.allowed = frozenset(allowed_symbols) if allowed_symbols is not None else None
        if self.allowed is not None and any(not CODE.fullmatch(str(c)) for c in self.allowed):
            raise ValueError('CATALYST_SYMBOL_INVALID')
        self._seen = OrderedDict()
        self._source_symbols = OrderedDict()
        self._source_versions = OrderedDict()
        self._latest = {}
        self.counts = Counter()

    def _purge(self, now):
        self._latest = {k: v for k, v in self._latest.items() if _time(v['expires_at']) > now}
        while len(self._latest) > self.max_entries:
            self._latest.pop(next(iter(self._latest)))
        while len(self._seen) > self.max_entries:
            self._seen.popitem(last=False)
        while len(self._source_symbols) > self.max_entries:
            key, _ = self._source_symbols.popitem(last=False)
            self._source_versions.pop(key, None)

    def _revoke_source(self, source_key):
        for symbol in self._source_symbols.get(source_key, ()):
            prior = self._latest.get(symbol)
            if prior and prior['source_key_sha256'] == source_key:
                self._latest.pop(symbol, None)

    def _submit(self, *, symbol, source_kind, source_key, text, content_at, available_at,
                now, precise_publication=True):
        if self.allowed is not None and symbol not in self.allowed:
            self.counts['outside_allowed_symbols'] += 1
            return False
        master_at = _time(self.linker.master_available_at)
        effective = max(available_at, master_at)
        expires = content_at + self.ttl
        if not content_at <= available_at <= now or effective > now or expires <= now:
            self.counts['stale_or_future_event'] += 1
            return False
        snippet, complete, redacted = _safe_text(text,
            limit=MAX_DART_TEXT_BYTES if source_kind == 'opendart' else MAX_TEXT_BYTES)
        source_hash = _digest(text)
        identity = _digest(_canon({'symbol': symbol, 'source_kind': source_kind,
            'source_key_sha256': source_key, 'text_sha256': source_hash,
            'content_at': content_at.isoformat(), 'available_at': available_at.isoformat(),
            'master_sha256': self.linker.master_sha256, 'session_id': self.session_id}))
        if identity in self._seen:
            self.counts['duplicate_event'] += 1
            return False
        self._seen[identity] = expires
        payload = {'symbol': symbol, 'source_kind': source_kind, 'text': snippet,
                   'text_is_untrusted_data': True, 'text_complete': complete,
                   'redacted_sensitive_pattern': redacted, 'source_text_sha256': source_hash,
                   'publication_time_precise': precise_publication,
                   'risk_approval': False, 'historical_replay': False}
        item = {'symbol': symbol, 'session_id': self.session_id, 'snapshot_id': identity,
                'source_key_sha256': source_key, 'source_kind': source_kind,
                'available_at': effective.isoformat(), 'expires_at': expires.isoformat(),
                'advisory_only': True, 'direction_verified': False}
        # Replacing the latest identity precedes submit, so a new unscored
        # version can never fall through to an earlier cached positive score.
        self._latest[symbol] = item
        try:
            accepted = self.submit(kind='news', symbol=symbol, session_id=self.session_id,
                snapshot_id=identity, state=payload, observed_at=effective, expires_at=expires)
        except Exception:
            accepted = False
            self.counts['submit_error'] += 1
        self.counts['submitted' if accepted is True else 'not_accepted'] += 1
        self._purge(now)
        return accepted is True

    def consume_telegram(self, rows, *, now):
        now = _time(now)
        if not isinstance(rows, (list, tuple)) or len(rows) > 1000:
            raise ValueError('CATALYST_INPUT_BOUND_INVALID')
        accepted = 0
        for row in rows:
            try:
                # A newly observed malformed/oversized edit must revoke this
                # message's earlier interpretation before parsing its body.
                if (isinstance(row, dict) and type(row.get('channel_id')) is int
                        and type(row.get('message_id')) is int and row['message_id'] > 0
                        and type(row.get('version_id')) is int and row['version_id'] > 0):
                    source_key = _digest('telegram\0'+str(row['channel_id'])+'\0'+str(row['message_id']))
                    prior = self._source_versions.get(source_key)
                    if prior is not None and row['version_id'] > prior[0]:
                        self._revoke_source(source_key)
                if (not isinstance(row, dict) or row.get('origin') != 'observed'
                        or row.get('clock_valid') not in (True, 1)
                        or type(row.get('version_id')) is not int or row['version_id'] <= 0
                        or type(row.get('channel_id')) is not int
                        or type(row.get('message_id')) is not int or row['message_id'] <= 0):
                    raise ValueError('CATALYST_PROVENANCE_INVALID')
                published = _time(row['published_at'])
                edited = _time(row['edited_at']) if row.get('edited_at') is not None else None
                content_at = edited or published
                if edited and edited < published:
                    raise ValueError('CATALYST_EDIT_TIME_INVALID')
                observed, recorded = _time(row['observed_at']), _time(row['recorded_at'])
                if observed < content_at or recorded < observed or max(recorded, observed) > now:
                    raise ValueError('CATALYST_OBSERVATION_TIME_INVALID')
                _safe_text(row['text'])
                source_key = _digest('telegram\0'+str(row['channel_id'])+'\0'+str(row['message_id']))
                version = row['version_id']
                previous = self._source_versions.get(source_key)
                content_hash = _digest(row['text'])
                if previous is not None and version <= previous[0]:
                    if version == previous[0] and content_hash != previous[1]:
                        self._revoke_source(source_key)
                        raise ValueError('CATALYST_IMMUTABLE_VERSION_CONFLICT')
                    self.counts['duplicate_or_old_version'] += 1
                    continue
                self._source_versions[source_key] = (version, content_hash)
                self._revoke_source(source_key)
                links = self.linker.link(row['text'])['links']
                self._source_symbols[source_key] = tuple(x['symbol'] for x in links)
                if not links:
                    self.counts['no_exact_symbol_link'] += 1
                for link in links:
                    accepted += self._submit(symbol=link['symbol'], source_kind='telegram',
                        source_key=source_key, text=row['text'], content_at=content_at,
                        available_at=max(observed, recorded), now=now)
            except (KeyError, TypeError, ValueError, OverflowError):
                self.counts['invalid_event'] += 1
        self._purge(now)
        return accepted

    def consume_dart(self, event, *, now):
        """Accept a verified new receipt's original text, never a title-only hit.

        A date-only DART publication is accepted solely for a newly observed
        receipt on that same Korean day, explicitly marked imprecise. A later
        reread or report generation cannot refresh its original first-seen TTL.
        """
        now = _time(now)
        try:
            from zoneinfo import ZoneInfo
            symbol, receipt = event['symbol'], event['receipt_no']
            if (not CODE.fullmatch(str(symbol)) or symbol not in self.linker.names
                    or not re.fullmatch('[0-9]{14}', str(receipt))
                    or event.get('symbol_identity_verified') is not True
                    or event.get('original_text_complete') is not True
                    or event.get('new_receipt_observed') is not True
                    or _digest(event['text']) != event['text_sha256']):
                raise ValueError('CATALYST_DART_IDENTITY_INVALID')
            first = _time(event['first_observed_at'])
            available = _time(event['available_at'])
            if available < first:
                raise ValueError('CATALYST_DART_TIME_INVALID')
            precise = event.get('published_at') is not None
            if precise:
                content_at = _time(event['published_at'])
            else:
                if event.get('publication_date') != first.astimezone(ZoneInfo('Asia/Seoul')).date().isoformat():
                    raise ValueError('CATALYST_DART_OLD_OR_UNKNOWN_PUBLICATION')
                content_at = first
            if receipt[:8] != content_at.astimezone(ZoneInfo('Asia/Seoul')).strftime('%Y%m%d'):
                raise ValueError('CATALYST_DART_RECEIPT_DATE_CONFLICT')
            if first < content_at or first + self.ttl <= now:
                raise ValueError('CATALYST_DART_STALE')
            source_key = _digest('dart\0'+receipt)
            old = self._source_versions.get(source_key)
            content_hash = _digest(event['text'])
            if old is not None:
                if old[1] != content_hash:
                    self._revoke_source(source_key)
                    raise ValueError('CATALYST_DART_ORIGINAL_CONFLICT')
                self.counts['duplicate_event'] += 1
                return False
            self._source_versions[source_key] = (0, content_hash)
            self._revoke_source(source_key)
            self._source_symbols[source_key] = (symbol,)
            result = self._submit(symbol=symbol, source_kind='opendart', source_key=source_key,
                text=event['text'], content_at=content_at, available_at=available, now=now,
                precise_publication=precise)
            self._purge(now)
            return result
        except (KeyError, TypeError, ValueError, OverflowError):
            self.counts['invalid_dart_event'] += 1
            self._purge(now)
            return False

    def latest_for(self, symbol, *, now):
        """Constant-size, no-I/O identity lookup; no computed probability."""
        item = self._latest.get(symbol)
        moment = _time(now)
        if item is None or not _time(item['available_at']) <= moment < _time(item['expires_at']):
            return None
        return dict(item)

    def run_once(self, tail, *, now):
        rows = tail.read_new()
        submitted = self.consume_telegram(rows, now=now)
        return {'schema': SCHEMA, 'archive': dict(tail.last_status),
                'submitted': submitted, 'counts': dict(self.counts),
                'pending_symbol_count': len(self._latest),
                'network_requests_by_bridge': 0, 'broker_requests': 0,
                'body_logged': False, 'advisory_only': True}
