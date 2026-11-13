"""Lossless compact REST re-observations; original SQLite evidence stays immutable.

Only an identical successful response for the same recorder/session scope,
request, API and page is shared. Every observation retains its own query id,
request/response time, continuation and complete-event hash. WS00 is never
deduplicated. ``install_evidence_view`` installs a connection-local read view,
before query_only is enabled; it does not migrate or write the main database.
"""
from __future__ import annotations
from functools import lru_cache
import hashlib
import json
import sqlite3

TABLE = 'broker_observation_repeats'
VIEW = 'broker_observation_evidence'


def evidence_table(db):
    if db.execute("SELECT 1 FROM sqlite_temp_master WHERE type='view' AND name=?", (VIEW,)).fetchone():
        return VIEW
    if (db.execute("SELECT 1 FROM main.sqlite_master WHERE type='table' AND name=?", (TABLE,)).fetchone()
            and db.execute('SELECT 1 FROM broker_observation_repeats LIMIT 1').fetchone()):
        raise ValueError('broker_repeat_reader_adapter_required')
    return 'broker_observations'


def _json(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _sha(raw):
    return hashlib.sha256(raw.encode()).hexdigest()


def create_repeat_schema(db):
    db.executescript('''
      CREATE TABLE IF NOT EXISTS broker_observation_repeats (
        id INTEGER PRIMARY KEY, scope TEXT NOT NULL, day TEXT NOT NULL,
        mode TEXT NOT NULL, observed_at TEXT NOT NULL, source_api TEXT NOT NULL,
        digest TEXT NOT NULL, original_row_id INTEGER NOT NULL,
        receipt_json TEXT NOT NULL, receipt_digest TEXT NOT NULL);
      CREATE INDEX IF NOT EXISTS broker_observation_repeats_scope
        ON broker_observation_repeats(scope,day,source_api,id);
      CREATE TRIGGER IF NOT EXISTS broker_observation_repeats_no_update
        BEFORE UPDATE ON broker_observation_repeats
        BEGIN SELECT RAISE(ABORT, 'repeat observations are immutable'); END;
      CREATE TRIGGER IF NOT EXISTS broker_observation_repeats_no_delete
        BEFORE DELETE ON broker_observation_repeats
        BEGIN SELECT RAISE(ABORT, 'repeat observations are immutable'); END;
      CREATE TABLE IF NOT EXISTS broker_observation_dedup_heads (
        scope TEXT NOT NULL, epoch TEXT NOT NULL, request_key TEXT NOT NULL,
        original_row_id INTEGER NOT NULL, response_sha256 TEXT NOT NULL,
        PRIMARY KEY(scope,epoch,request_key));
    ''')


@lru_cache(maxsize=64)
def _sha_cached(raw):
    return _sha(raw)


@lru_cache(maxsize=64)
def _parsed_original_payload(raw):
    original = json.loads(raw)
    if not isinstance(original, dict) or original.get('source_api') == '00':
        return original, None
    return original, _sha(_json(original.get('response_body')))


def _hydrate(raw, original_digest, receipt_raw, receipt_digest, event_digest,
             day, mode, observed_at, source_api):
    if (_sha_cached(raw) != original_digest or _sha(receipt_raw) != receipt_digest):
        raise ValueError('broker_repeat_source_hash_mismatch')
    original, original_response_sha256 = _parsed_original_payload(raw)
    receipt = json.loads(receipt_raw)
    if (receipt.get('schema') != 'broker-observation-repeat-v1'
            or original.get('source_api') == '00'
            or original_response_sha256 != receipt['response_sha256']):
        raise ValueError('broker_repeat_response_unverified')
    event = dict(receipt['envelope'])
    event['response_body'] = original['response_body']
    if (event.get('day'),event.get('mode'),event.get('observed_at'),event.get('source_api')) != (day,mode,observed_at,source_api):
        raise ValueError('broker_repeat_row_envelope_mismatch')
    # Sharing must not change the request scope or pagination/status contract.
    for key in ('schema', 'source_api', 'source_kind', 'mode', 'day', 'page',
                'request_body', 'continuation', 'status', 'http_status'):
        if original.get(key) != event.get(key):
            raise ValueError('broker_repeat_request_scope_mismatch')
    result = _json(event)
    if _sha(result) != event_digest:
        raise ValueError('broker_repeat_event_hash_mismatch')
    return result


def install_evidence_view(db):
    """Use on each reader before PRAGMA query_only; existing DBs need no DDL."""
    if db.execute("SELECT 1 FROM sqlite_temp_master WHERE type='view' AND name=?", (VIEW,)).fetchone():
        return
    db.create_function('broker_repeat_payload', 9, _hydrate, deterministic=True)
    if not db.execute("SELECT 1 FROM main.sqlite_master WHERE type='table' AND name=?", (TABLE,)).fetchone():
        db.execute('CREATE TEMP VIEW broker_observation_evidence AS SELECT * FROM main.broker_observations')
        return
    db.execute('''CREATE TEMP VIEW broker_observation_evidence AS
      SELECT id,scope,day,mode,observed_at,source_api,digest,payload_json FROM main.broker_observations
      UNION ALL
      SELECT r.id,r.scope,r.day,r.mode,r.observed_at,r.source_api,r.digest,
        broker_repeat_payload(o.payload_json,o.digest,r.receipt_json,r.receipt_digest,r.digest,
          r.day,r.mode,r.observed_at,r.source_api)
      FROM main.broker_observation_repeats r
      LEFT JOIN main.broker_observations o ON o.id=r.original_row_id
        AND o.scope=r.scope AND o.day=r.day AND o.mode=r.mode AND o.source_api=r.source_api''')
    db.execute('''CREATE TEMP VIEW broker_observation_repeat_receipts AS
      SELECT id,scope,day,mode,observed_at,source_api,receipt_digest AS digest,
        receipt_json AS payload_json FROM main.broker_observation_repeats''')


def persist_observation(db, scope, event, payload, *, epoch):
    """Called inside the tracker's write transaction; returns original/repeat."""
    if not db.in_transaction:
        db.execute('BEGIN IMMEDIATE')
    event_digest = _sha(payload)
    last = max(db.execute('SELECT COALESCE(MAX(id),0) FROM main.broker_observations').fetchone()[0],
               db.execute('SELECT COALESCE(MAX(id),0) FROM broker_observation_repeats').fetchone()[0])
    ident = last + 1
    api = event['source_api']
    request_key = response_digest = head = None
    if api != '00' and event.get('status') == 'success':
        request_key = _sha(_json({key:event.get(key) for key in
            ('source_api','mode','day','page','request_body','continuation','status','http_status')}))
        response_digest = _sha(_json(event['response_body']))
        head = db.execute('SELECT original_row_id,response_sha256 FROM broker_observation_dedup_heads '
                          'WHERE scope=? AND epoch=? AND request_key=?', (scope,epoch,request_key)).fetchone()
    if head is not None and head[1] == response_digest:
        original = db.execute('SELECT digest,payload_json FROM main.broker_observations WHERE id=? AND scope=?',
                              (head[0],scope)).fetchone()
        if original is None:
            raise ValueError('broker_repeat_original_missing')
        receipt = _json({'schema':'broker-observation-repeat-v1','epoch':epoch,
            'response_sha256':response_digest, 'envelope':{k:v for k,v in event.items() if k!='response_body'}})
        receipt_digest = _sha(receipt)
        if _hydrate(original[1],original[0],receipt,receipt_digest,event_digest,
                    event['day'],event['mode'],event['observed_at'],api) != payload:
            raise ValueError('broker_repeat_roundtrip_mismatch')
        db.execute('INSERT INTO broker_observation_repeats VALUES (?,?,?,?,?,?,?,?,?,?)',
            (ident,scope,event['day'],event['mode'],event['observed_at'],api,event_digest,head[0],receipt,receipt_digest))
        return 'repeat'
    db.execute('INSERT INTO main.broker_observations(id,scope,day,mode,observed_at,source_api,digest,payload_json) '
               'VALUES (?,?,?,?,?,?,?,?)', (ident,scope,event['day'],event['mode'],event['observed_at'],api,event_digest,payload))
    if request_key is not None:
        db.execute('INSERT OR REPLACE INTO broker_observation_dedup_heads VALUES (?,?,?,?,?)',
                   (scope,epoch,request_key,ident,response_digest))
    return 'original'
