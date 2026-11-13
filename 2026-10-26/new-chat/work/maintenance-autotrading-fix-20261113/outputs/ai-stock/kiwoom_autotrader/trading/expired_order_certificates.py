"""Immutable once-collected prior-day terminal order proofs, never new fills."""
from bisect import bisect_right
from collections import OrderedDict
from datetime import datetime
import hashlib
import json
import os
import re
import sqlite3
import threading
from .decision_evidence import canonical_json
from .broker_observation_repeats import TABLE as REPEAT_TABLE, VIEW as EVIDENCE_VIEW, evidence_table

TABLE='paper_expired_order_certificates'
KEYS=('scope','day','mode','ord_no','symbol','side','strategy','quantity','registered_at','filled_quantity')
_V2_REPEAT_PAIR = {'218972': ('002790', 7, 26250), '219563': ('010660', 52, 3690)}
_ORD_KEY_RE = re.compile(r'[0-9]{1,7}')
_INDEX_LOCK = threading.RLock()
_INDEX_LRU: OrderedDict[tuple, "_ExpiryIndexEntry"] = OrderedDict()
_INDEX_MAX_ENTRIES = 4
_SUBCACHE_MAX_ENTRIES = 64


class _ExpiryIndexEntry:
    __slots__ = (
        'obs_state',
        'rep_state',
        'global_max_id',
        'ka10075_ids',
        'ka10075_invalid_ids',
        'ka10075_last_seen_id',
        'day_late_entries',
        'day_late_invalid',
        'order_late_hits',
        'source_hashes_cache',
        'captured_pages_cache',
    )

    def __init__(self, obs_state, rep_state):
        self.obs_state = obs_state
        self.rep_state = rep_state
        self.global_max_id = max(obs_state[4], rep_state[4])
        self.ka10075_ids = []
        self.ka10075_invalid_ids = []
        self.ka10075_last_seen_id = {}
        self.day_late_entries = {}
        self.day_late_invalid = {}
        self.order_late_hits = {}
        self.source_hashes_cache = OrderedDict()
        self.captured_pages_cache = OrderedDict()

    def clone_metadata_for_delta(self, obs_state, rep_state):
        clone = _ExpiryIndexEntry(obs_state, rep_state)
        clone.ka10075_ids = list(self.ka10075_ids)
        clone.ka10075_invalid_ids = list(self.ka10075_invalid_ids)
        clone.ka10075_last_seen_id = dict(self.ka10075_last_seen_id)
        clone.day_late_entries = {day: list(items) for day, items in self.day_late_entries.items()}
        clone.day_late_invalid = {day: list(items) for day, items in self.day_late_invalid.items()}
        clone.order_late_hits = {key: list(items) for key, items in self.order_late_hits.items()}
        clone.source_hashes_cache = OrderedDict(self.source_hashes_cache)
        clone.captured_pages_cache = OrderedDict(self.captured_pages_cache)
        return clone

    def ingest_row(self, row):
        row_id = row['id']
        day = row['day']
        observed_at = row['observed_at']
        api = row['source_api']
        digest = row['digest']
        raw = row['payload_json']
        event = None
        if isinstance(raw, str) and hashlib.sha256(raw.encode()).hexdigest() == digest:
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    event = parsed
            except (ValueError, TypeError):
                event = None
        if api == 'ka10075':
            self.ka10075_ids.append(row_id)
            if event is None or event.get('status') != 'success':
                self.ka10075_invalid_ids.append(row_id)
                return
            rb = event.get('response_body')
            pending = rb.get('oso') if isinstance(rb, dict) else None
            if not isinstance(pending, list) or any(not isinstance(item, dict) for item in pending):
                self.ka10075_invalid_ids.append(row_id)
                return
            for item in pending:
                for field in ('ord_no', 'orig_ord_no'):
                    value = item.get(field)
                    if isinstance(value, str) and _ORD_KEY_RE.fullmatch(value) is not None:
                        normalized = str(int(value))
                        if normalized != '0':
                            self.ka10075_last_seen_id[normalized] = row_id
            return
        if api in ('00', 'kt00009'):
            self.day_late_entries.setdefault(day, []).append((row_id, observed_at))
            if event is None:
                self.day_late_invalid.setdefault(day, []).append((row_id, observed_at))
                return
            if event.get('source_api') == '00':
                values = [event.get('values', {})]
            else:
                rb = event.get('response_body', {})
                values = rb.get('acnt_ord_cntr_prst_array', []) if isinstance(rb, dict) else None
            if not isinstance(values, list) or any(not isinstance(v, dict) for v in values):
                self.day_late_invalid.setdefault(day, []).append((row_id, observed_at))
                return
            stream = event.get('source_api') == '00'
            matched_keys = set()
            for value in values:
                n_val = value.get('9203') if stream else value.get('ord_no')
                o_val = value.get('904') if stream else value.get('orig_ord_no')
                if isinstance(n_val, str) and _ORD_KEY_RE.fullmatch(n_val) is not None:
                    normalized = str(int(n_val))
                    if normalized != '0':
                        matched_keys.add(normalized)
                if isinstance(o_val, str) and _ORD_KEY_RE.fullmatch(o_val) is not None:
                    normalized = str(int(o_val))
                    if normalized != '0':
                        matched_keys.add(normalized)
            for ord_key in matched_keys:
                self.order_late_hits.setdefault(ord_key, []).append((row_id, day, observed_at))


def _table_append_state(db, table, scope, *, check_exists=False):
    if check_exists and db.execute(
        "SELECT 1 FROM main.sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is None:
        return (False, 0, 0, None, 0, None)
    count, min_id, max_id = db.execute(
        f"SELECT COUNT(*), COALESCE(MIN(id),0), COALESCE(MAX(id),0) FROM main.{table} WHERE scope=?",
        (scope,),
    ).fetchone()
    if not count:
        return (True, 0, 0, None, 0, None)
    first_row = db.execute(
        f"SELECT digest FROM main.{table} WHERE id=? AND +scope=?", (min_id, scope)
    ).fetchone()
    tail_row = db.execute(
        f"SELECT digest FROM main.{table} WHERE id=? AND +scope=?", (max_id, scope)
    ).fetchone()
    if first_row is None or tail_row is None:
        raise ValueError('expiry_index_boundary_missing')
    return (True, count, min_id, first_row[0], max_id, tail_row[0])


def _is_valid_table_append(db, table, scope, prev_state, cur_state, prev_global_max_id):
    if cur_state == prev_state:
        return True
    if not cur_state[0] or cur_state[1] < prev_state[1]:
        return False
    if prev_state[1] == 0:
        min_new = cur_state[2]
        return cur_state[1] > 0 and min_new > prev_global_max_id
    if (cur_state[1] <= prev_state[1]
            or cur_state[2] != prev_state[2]
            or cur_state[3] != prev_state[3]
            or cur_state[4] <= prev_state[4]):
        return False
    prev_tail = db.execute(
        f"SELECT digest FROM main.{table} WHERE id=? AND +scope=?", (prev_state[4], scope)
    ).fetchone()
    if prev_tail is None or prev_tail[0] != prev_state[5]:
        return False
    tail_count, min_new_id = db.execute(
        f"SELECT COUNT(*), COALESCE(MIN(id),0) FROM main.{table} WHERE id>? AND +scope=?",
        (prev_state[4], scope),
    ).fetchone()
    return tail_count == (cur_state[1] - prev_state[1]) and min_new_id > prev_global_max_id


def _sync_expiry_index(db, scope):
    try:
        if evidence_table(db) != EVIDENCE_VIEW:
            return None
        db_list = db.execute("PRAGMA database_list").fetchall()
        main_file = next((row[2] for row in db_list if row[1] == 'main'), '')
        if not main_file:
            return None
        info = os.stat(main_file)
        schema_version = db.execute("PRAGMA schema_version").fetchone()[0]
        query_only = bool(db.execute("PRAGMA query_only").fetchone()[0])
        uncommitted_rw = (not query_only) and bool(db.in_transaction)
        cache_key = (info.st_dev, info.st_ino, schema_version, scope)
    except (sqlite3.Error, OSError, ValueError, TypeError):
        return None

    with _INDEX_LOCK:
        savepoint_open = False
        try:
            db.execute("SAVEPOINT expiry_obs_index_sync")
            savepoint_open = True
            obs_state = _table_append_state(db, 'broker_observations', scope)
            rep_state = _table_append_state(db, REPEAT_TABLE, scope, check_exists=True)
            cached = _INDEX_LRU.get(cache_key)
            if cached is not None and cached.obs_state == obs_state and cached.rep_state == rep_state:
                if not uncommitted_rw:
                    _INDEX_LRU.move_to_end(cache_key)
                return cached
            if uncommitted_rw:
                return None
            if (cached is not None
                    and _is_valid_table_append(db, 'broker_observations', scope, cached.obs_state, obs_state, cached.global_max_id)
                    and _is_valid_table_append(db, REPEAT_TABLE, scope, cached.rep_state, rep_state, cached.global_max_id)):
                updated = cached.clone_metadata_for_delta(obs_state, rep_state)
                delta_rows = db.execute(
                    f"""SELECT id,day,observed_at,source_api,digest,payload_json
                        FROM {EVIDENCE_VIEW}
                        WHERE id>? AND +scope=? AND mode='paper'
                          AND source_api IN ('00','kt00009','ka10075')
                        ORDER BY id""",
                    (cached.global_max_id, scope),
                ).fetchall()
                for row in delta_rows:
                    updated.ingest_row(row)
                _INDEX_LRU[cache_key] = updated
                _INDEX_LRU.move_to_end(cache_key)
                return updated

            fresh = _ExpiryIndexEntry(obs_state, rep_state)
            if rep_state[0]:
                days = [row[0] for row in db.execute(
                    f"SELECT DISTINCT day FROM main.broker_observations WHERE scope=? "
                    f"UNION SELECT DISTINCT day FROM main.{REPEAT_TABLE} WHERE scope=?",
                    (scope, scope),
                ).fetchall()]
            else:
                days = [row[0] for row in db.execute(
                    "SELECT DISTINCT day FROM main.broker_observations WHERE scope=?",
                    (scope,),
                ).fetchall()]
            collected = []
            for day in sorted(days):
                for api in ('00', 'kt00009', 'ka10075'):
                    collected.extend(db.execute(
                        f"""SELECT id,day,observed_at,source_api,digest,payload_json
                            FROM {EVIDENCE_VIEW}
                            WHERE scope=? AND day=? AND source_api=? AND mode='paper'
                            ORDER BY id""",
                        (scope, day, api),
                    ).fetchall())
            collected.sort(key=lambda r: r['id'])
            for row in collected:
                fresh.ingest_row(row)
            _INDEX_LRU[cache_key] = fresh
            _INDEX_LRU.move_to_end(cache_key)
            while len(_INDEX_LRU) > _INDEX_MAX_ENTRIES:
                _INDEX_LRU.popitem(last=False)
            return fresh
        except (sqlite3.Error, OSError, ValueError, TypeError):
            if not uncommitted_rw:
                _INDEX_LRU.pop(cache_key, None)
            if savepoint_open:
                try:
                    db.execute("ROLLBACK TO expiry_obs_index_sync")
                except sqlite3.Error:
                    pass
            return None
        finally:
            if savepoint_open:
                try:
                    db.execute("RELEASE expiry_obs_index_sync")
                except sqlite3.Error:
                    pass


def _bounded_cache_put(mapping, key, value):
    mapping[key] = value
    mapping.move_to_end(key)
    while len(mapping) > _SUBCACHE_MAX_ENTRIES:
        mapping.popitem(last=False)


def verify_certificate_sources(db, scope, order_day, capture_day, source_max_id, expected_hashes):
    if type(source_max_id) is int:
        entry = _sync_expiry_index(db, scope)
        if entry is not None and source_max_id <= entry.global_max_id:
            cache_key = (order_day, capture_day, source_max_id)
            with _INDEX_LOCK:
                actual = entry.source_hashes_cache.get(cache_key)
            if actual is None:
                sources = db.execute(
                    f"""SELECT id,digest FROM {evidence_table(db)} WHERE scope=? AND mode='paper'
                        AND ((day=? AND source_api='kt00007') OR (day=? AND source_api='ka10075'))
                        AND id<=? ORDER BY id""",
                    (scope, order_day, capture_day, source_max_id),
                ).fetchall()
                actual = [[r['id'], r['digest']] for r in sources]
                with _INDEX_LOCK:
                    _bounded_cache_put(entry.source_hashes_cache, cache_key, actual)
            return actual == expected_hashes
    sources = db.execute(
        f"""SELECT id,digest FROM {evidence_table(db)} WHERE scope=? AND mode='paper'
            AND ((day=? AND source_api='kt00007') OR (day=? AND source_api='ka10075'))
            AND id<=? ORDER BY id""",
        (scope, order_day, capture_day, source_max_id),
    ).fetchall()
    return [[r['id'], r['digest']] for r in sources] == expected_hashes


def check_later_ka10075_clear(db, scope, source_max_id, original_ord_key):
    if type(source_max_id) is int:
        entry = _sync_expiry_index(db, scope)
        if entry is not None:
            later_count = len(entry.ka10075_ids) - bisect_right(entry.ka10075_ids, source_max_id)
            if later_count > 20000:
                return False
            if entry.ka10075_invalid_ids and entry.ka10075_invalid_ids[-1] > source_max_id:
                return False
            if entry.ka10075_last_seen_id.get(original_ord_key, 0) > source_max_id:
                return False
            return True
    later = db.execute(
        f"SELECT digest,payload_json FROM {evidence_table(db)} "
        f"WHERE scope=? AND mode='paper' AND source_api='ka10075' AND id>? ORDER BY id LIMIT 20001",
        (scope, source_max_id),
    ).fetchall()
    if len(later) > 20000:
        return False
    for source in later:
        content = source['payload_json']
        if not isinstance(content, str) or hashlib.sha256(content.encode()).hexdigest() != source['digest']:
            return False
        event = json.loads(content)
        pending = event.get('response_body', {}).get('oso')
        if event.get('status') != 'success' or not isinstance(pending, list):
            return False
        for item in pending:
            if not isinstance(item, dict):
                return False
            for field in ('ord_no', 'orig_ord_no'):
                value = item.get(field)
                if isinstance(value, str) and _ORD_KEY_RE.fullmatch(value) and str(int(value)) == original_ord_key:
                    return False
    return True


def cached_captured_terminal_pages(db, scope, order_day, captured_at, source_max_id, loader):
    if _captured_cacheable(captured_at, source_max_id):
        entry = _sync_expiry_index(db, scope)
        if entry is not None and source_max_id <= entry.global_max_id:
            cache_key = (order_day, captured_at.isoformat(), source_max_id)
            with _INDEX_LOCK:
                hit = cache_key in entry.captured_pages_cache
                cached_value = entry.captured_pages_cache.get(cache_key)
            if hit:
                return cached_value
            computed = loader()
            with _INDEX_LOCK:
                _bounded_cache_put(entry.captured_pages_cache, cache_key, computed)
            return computed
    return loader()


def _captured_cacheable(captured_at, source_max_id):
    return (
        captured_at is not None
        and isinstance(captured_at, datetime)
        and captured_at.tzinfo is not None
        and type(source_max_id) is int
    )


def select_late_order_observations(db, scope, order_day, detailed_end_iso, natural_expiry, original_ord_key):
    entry = _sync_expiry_index(db, scope)
    if entry is not None:
        if not natural_expiry:
            total_matching = sum(
                1 for _, obs_at in entry.day_late_entries.get(order_day, ()) if obs_at > detailed_end_iso
            )
            has_invalid = any(
                obs_at > detailed_end_iso for _, obs_at in entry.day_late_invalid.get(order_day, ())
            )
            candidate_ids = [
                rid for rid, rday, obs_at in entry.order_late_hits.get(original_ord_key, ())
                if rday == order_day and obs_at > detailed_end_iso
            ]
        else:
            total_matching = sum(
                len(rows) for d, rows in entry.day_late_entries.items() if d >= order_day
            )
            has_invalid = any(
                bool(rows) for d, rows in entry.day_late_invalid.items() if d >= order_day
            )
            candidate_ids = [
                rid for rid, rday, _ in entry.order_late_hits.get(original_ord_key, ())
                if rday >= order_day
            ]
        if total_matching > 20000 or has_invalid:
            return None
        if not candidate_ids:
            return []
        fetched = []
        table = evidence_table(db)
        for start in range(0, len(candidate_ids), 500):
            chunk = candidate_ids[start:start + 500]
            placeholders = ','.join('?' for _ in chunk)
            fetched.extend(db.execute(
                f"""SELECT id,day,source_api,observed_at,digest,payload_json FROM {table}
                    WHERE id IN ({placeholders}) AND +scope=? AND mode='paper'
                      AND source_api IN ('00','kt00009') ORDER BY id""",
                (*chunk, scope),
            ).fetchall())
        if len(fetched) != len(candidate_ids):
            return None
        return fetched
    late_rows = db.execute(
        f"""SELECT id,day,source_api,observed_at,digest,payload_json FROM {evidence_table(db)}
            WHERE scope=? AND mode='paper' AND ((day=? AND observed_at>?) OR (? AND day>=?))
              AND source_api IN ('00','kt00009') ORDER BY id LIMIT 20001""",
        (scope, order_day, detailed_end_iso, natural_expiry, order_day),
    ).fetchall()
    if len(late_rows) > 20000:
        return None
    return late_rows


def _number(value, digits=20):
    if isinstance(value, str) and re.fullmatch(rf'[0-9]{{1,{digits}}}', value):
        return int(value)
    return None


def _paired_order(order):
    return (order['scope'], order['day'], order['mode']) == ('default', '20261104', 'paper') and order['ord_no'] in _V2_REPEAT_PAIR


def paired_v2_captured(db, order):
    """Once issued, neither member may bypass the pair's v2 verification."""
    if not _paired_order(order) or not db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)).fetchone():
        return False
    rows = db.execute(f'SELECT payload_json FROM {TABLE} WHERE scope=? AND day=? AND ord_no IN (?,?) LIMIT 1001',
                      (order['scope'], order['day'], *_V2_REPEAT_PAIR))
    for index, row in enumerate(rows, 1):
        if index > 1000:
            return True
        try:
            proof = json.loads(row[0])
        except (ValueError, TypeError):
            return True
        if not isinstance(proof, dict) or proof.get('schema') != 'paper-expired-terminal-proof-v1':
            return True
    return False


def _same_v2_details(db, order, cutoff, captured_at, now):
    """Recheck every later query for the two orders issued as one pair."""
    expected = _V2_REPEAT_PAIR.get(order['ord_no'])
    if (not _paired_order(order) or (order['strategy'], order['side']) != ('EXP_D', 'buy')
            or (order['symbol'], order['quantity'], order['filled_quantity']) != (*expected[:2], 0)):
        return False

    originals = {int(number): terms for number, terms in _V2_REPEAT_PAIR.items()}
    queries = {}
    last_id, last_observed = cutoff, captured_at
    entries = db.execute(f"""SELECT id,observed_at,digest,payload_json FROM {evidence_table(db)}
        WHERE scope=? AND mode='paper' AND day=? AND source_api='kt00007' AND id>?
        ORDER BY id LIMIT 20001""", (order['scope'], order['day'], cutoff))
    for count, entry in enumerate(entries, 1):
        raw = entry['payload_json']
        if (count > 20000 or type(entry['id']) is not int or entry['id'] <= last_id
                or not isinstance(raw, str) or hashlib.sha256(raw.encode()).hexdigest() != entry['digest']):
            return False
        event = json.loads(raw)
        if not isinstance(event, dict):
            return False
        query_id, page = event.get('query_id'), event.get('page')
        request, response, cont = (event.get(k) for k in ('request_body', 'response_body', 'continuation'))
        if (event.get('schema') != 'kiwoom-broker-page-v1' or event.get('source_api') != 'kt00007'
                or event.get('source_kind') != 'broker_observation' or event.get('mode') != 'paper'
                or event.get('day') != order['day'] or event.get('status') != 'success'
                or event.get('http_status') != 200 or event.get('observed_at') != entry['observed_at']
                or not isinstance(query_id, str) or re.fullmatch(r'[0-9a-f]{32}', query_id) is None
                or type(page) is not int or not 1 <= page <= 20
                or not all(isinstance(value, dict) for value in (request, response, cont))):
            return False
        started = datetime.fromisoformat(event['request_started_at'])
        observed = datetime.fromisoformat(event['observed_at'])
        previous_page, previous_end, body, complete, row_count, found = queries.get(
            query_id, (0, None, request, False, 0, set()))
        if (started.tzinfo is None or observed.tzinfo is None or not started <= observed <= now
                or previous_end is not None and started < previous_end
                or observed < last_observed or page != previous_page + 1 or complete or request != body
                or request.get('ord_dt') != order['day'] or request.get('qry_tp') not in ('1', '2')
                or request.get('stk_bond_tp') not in ('0', '1') or request.get('sell_tp') != '0'
                or request.get('stk_cd') not in (None, '') or request.get('fr_ord_no') not in (None, '')
                or request.get('dmst_stex_tp') not in ('%', 'KRX')):
            return False
        code, rows = response.get('return_code'), response.get('acnt_ord_cntr_prps_dtl')
        following = cont.get('response_cont_yn')
        if (type(code) not in (int, str) or code not in (0, '0') or not isinstance(rows, list)
                or row_count + len(rows) > 20000 or cont.get('request_cont_yn') != ('N' if page == 1 else 'Y')
                or following not in ('Y', 'N', '', None) or cont.get('next_key_present') is not (following == 'Y')):
            return False
        for row in rows:
            if not isinstance(row, dict):
                return False
            number, parent = (_number(row.get(field), 7) for field in ('ord_no', 'ori_ord'))
            if not number or parent is None or parent in originals:
                return False
            if number not in originals:
                continue
            symbol, quantity, price = originals[number]
            if (number in found or parent != 0 or row.get('stk_cd') not in (symbol, 'A' + symbol)
                    or row.get('io_tp_nm') not in ('현금매수', '+매수') or row.get('dmst_stex_tp') != 'KRX'
                    or row.get('mdfy_cncl') != '일반' or row.get('acpt_tp') != '주문완료'
                    or tuple(_number(row.get(field)) for field in
                             ('ord_qty', 'cnfm_qty', 'ord_uv', 'cntr_qty', 'cntr_uv', 'ord_remnq'))
                    != (quantity, quantity, price, 0, 0, quantity)):
                return False
            found.add(number)
        queries[query_id] = page, observed, body, following != 'Y', row_count + len(rows), found
        last_id, last_observed = entry['id'], observed
    return all(complete and found == originals.keys() for _, _, _, complete, _, found in queries.values())


def create_schema(db):
    db.executescript('''
      CREATE TABLE IF NOT EXISTS paper_expired_order_certificates (
        id INTEGER PRIMARY KEY,scope TEXT NOT NULL,day TEXT NOT NULL,ord_no TEXT NOT NULL,
        captured_at TEXT NOT NULL,payload_json TEXT NOT NULL,digest TEXT NOT NULL);
      CREATE INDEX IF NOT EXISTS paper_expired_order_certificates_order
        ON paper_expired_order_certificates(scope,day,ord_no,id);
      CREATE TRIGGER IF NOT EXISTS paper_expired_order_certificates_no_update
        BEFORE UPDATE ON paper_expired_order_certificates
        BEGIN SELECT RAISE(ABORT,'terminal certificates are immutable'); END;
      CREATE TRIGGER IF NOT EXISTS paper_expired_order_certificates_no_delete
        BEFORE DELETE ON paper_expired_order_certificates
        BEGIN SELECT RAISE(ABORT,'terminal certificates are immutable'); END;
    ''')


def capture_certificates(tracker,db,now,*,approved_rejected_orders=(),approval_manifest_sha256=None):
    # Generic recovery callers have no opt-in. The separately authorized
    # reconciliation caller must bind exact orders and an approval artifact.
    if approved_rejected_orders or approval_manifest_sha256 is not None:
        if (not isinstance(approved_rejected_orders, tuple) or not approved_rejected_orders
                or not isinstance(approval_manifest_sha256, str)
                or re.fullmatch('[0-9a-f]{64}',approval_manifest_sha256) is None
                or any(not isinstance(item, tuple) or len(item) != 2
                       or any(not isinstance(value, str) for value in item)
                       or re.fullmatch('[0-9]{8}',item[0]) is None
                       or re.fullmatch('[1-9][0-9]{0,6}',item[1]) is None
                       for item in approved_rejected_orders)
                or len(set(approved_rejected_orders)) != len(approved_rejected_orders)):
            raise ValueError('expiry_reconciliation_approval_unverified')
    today=now.strftime('%Y%m%d');rows=db.execute('''SELECT o.*,
       (SELECT COALESCE(SUM(f.quantity),0) FROM fills f WHERE f.scope=o.scope AND f.day=o.day
        AND f.mode=o.mode AND f.ord_no=o.ord_no) AS filled_quantity
        FROM orders o WHERE o.scope=? AND o.mode='paper' AND o.day<? ORDER BY o.day,o.ord_no LIMIT 10001''',
        (tracker.scope,today)).fetchall()
    if len(rows)>10000:raise ValueError('expiry_capture_order_limit')
    pair_locked = any(paired_v2_captured(db, row) for row in rows)
    captured=[];unconfirmed=[]
    for row in rows:
        order=dict(row)
        if order['filled_quantity']==order['quantity']:continue
        if verified_certificate(tracker,db,order,now):continue
        if pair_locked and _paired_order(order):
            unconfirmed.append(order['ord_no'])
            continue

        rejected_basis = {}
        if not tracker._paper_prior_order_terminal(db,order,now):
            if ((order['day'],order['ord_no']) not in approved_rejected_orders
                    or not tracker._paper_prior_order_terminal(db,order,now,
                        _rejected_auction_context=rejected_basis) or not rejected_basis):
                unconfirmed.append(order['ord_no'])
                continue
        sources=db.execute(f'''SELECT id,digest FROM {evidence_table(db)} WHERE scope=? AND mode='paper'
           AND ((day=? AND source_api='kt00007') OR (day=? AND source_api='ka10075')) ORDER BY id''',
           (tracker.scope,order['day'],today)).fetchall()
        proof={'schema':'paper-expired-terminal-proof-v1','order':{k:order[k] for k in KEYS},
               'captured_at':now.isoformat(),'source_max_id':max(r['id'] for r in sources),
               'source_hashes':[[r['id'],r['digest']] for r in sources],
               'semantics':'terminal_remainder_not_fill_or_fresh_account'}
        if rejected_basis:
            proof.update(schema='paper-expired-terminal-proof-v2', reason='rejected_exp_d_closing_buy',
                         approval_manifest_sha256=approval_manifest_sha256, rejection_evidence=rejected_basis)
        raw=canonical_json(proof,maximum=2*1024*1024)
        db.execute(f'INSERT INTO {TABLE}(scope,day,ord_no,captured_at,payload_json,digest) VALUES (?,?,?,?,?,?)',
            (tracker.scope,order['day'],order['ord_no'],now.isoformat(),raw,hashlib.sha256(raw.encode()).hexdigest()))
        captured.append(order['ord_no'])
    return {'status':'unconfirmed' if unconfirmed else 'complete','captured_orders':captured,
            'unconfirmed_orders':unconfirmed,'captured_count':len(captured)}


def verified_certificate(tracker,db,order,now):
    try:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(TABLE,)).fetchone():return False
        row=db.execute(f'SELECT * FROM {TABLE} WHERE scope=? AND day=? AND ord_no=? ORDER BY id DESC LIMIT 1',
            (tracker.scope,order['day'],order['ord_no'])).fetchone()
        if row is None:return False
        raw=row['payload_json'];proof=json.loads(raw);at=datetime.fromisoformat(row['captured_at'])
        schema=proof.get('schema')
        if (hashlib.sha256(raw.encode()).hexdigest()!=row['digest']
                or schema not in ('paper-expired-terminal-proof-v1','paper-expired-terminal-proof-v2')
                or proof.get('semantics')!='terminal_remainder_not_fill_or_fresh_account'
                or proof.get('captured_at')!=row['captured_at'] or at.tzinfo is None or not at<=now
                or proof.get('order')!={k:order[k] for k in KEYS}):return False
        if schema == 'paper-expired-terminal-proof-v1' and paired_v2_captured(db, order):
            return False
        if not verify_certificate_sources(
            db, tracker.scope, order['day'], at.strftime('%Y%m%d'),
            proof['source_max_id'], proof['source_hashes'],
        ):
            return False
        # Only the explicitly paired v2 proofs can survive newer detail. Every
        # subsequent query must establish the same complete zero-fill state.
        if db.execute(f'''SELECT 1 FROM {evidence_table(db)} WHERE scope=? AND mode='paper'
              AND day=? AND source_api='kt00007' AND id>? LIMIT 1''',
              (tracker.scope,order['day'],proof['source_max_id'])).fetchone():
            if schema != 'paper-expired-terminal-proof-v2' or not _same_v2_details(
                    db, order, proof['source_max_id'], at, now):
                return False
        original = str(int(order['ord_no']))
        if not check_later_ka10075_clear(db, tracker.scope, proof['source_max_id'], original):
            return False
        rejection = None
        if schema=='paper-expired-terminal-proof-v2':
            approval = proof.get('approval_manifest_sha256')
            if (proof.get('reason')!='rejected_exp_d_closing_buy'
                    or not isinstance(approval,str) or re.fullmatch('[0-9a-f]{64}',approval) is None):
                return False
            rejection = {}
        verified=tracker._paper_prior_order_terminal(db,order,now,_source_max_id=proof['source_max_id'],
            _captured_at=at,_rejected_auction_context=rejection)
        return verified and (rejection is None or bool(rejection) and rejection==proof.get('rejection_evidence'))
    except (sqlite3.Error,ValueError,KeyError,TypeError,OverflowError):return False
