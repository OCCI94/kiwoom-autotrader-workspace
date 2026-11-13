"""Strict canonical JSONL -> isolated historical SQLite, using stdlib only."""
from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
from uuid import UUID, uuid4

FORMAT = "ai-stock-history-v1"
APPLICATION_ID = 0x41534831
MAX_FILE = 16 * 1024 * 1024
MAX_ROWS = 100000
MAX_DATABASE = 128 * 1024 * 1024
EVIDENCE_SCOPE = "canonical_payload_integrity_and_declared_references_only_not_accounting_or_broker_verification"
COHORTS = {"kr_mock": "KRW", "kr_shadow": "KRW", "us_shadow": "USD"}
ENTITIES = ("orders", "roundtrips", "links")
MONEY = {"orders": ("requested_quantity", "filled_quantity", "requested_price", "filled_amount", "commission", "tax"),
         "roundtrips": ("quantity", "buy_amount", "sell_amount", "gross_pnl", "commission", "tax", "net_pnl"),
         "links": ("allocated_quantity",)}
BASE = {"lineage_id", "cohort", "record_ref"}
FIELDS = {
    "orders": BASE | {"symbol", "side", "status", *MONEY["orders"], "cost_state", "created_at", "updated_at", "actual_fill_at", "decision_reason"},
    "roundtrips": BASE | {"symbol", *MONEY["roundtrips"], "cost_state", "opened_at", "closed_at", "entry_reason", "exit_reason"},
    "links": BASE | {"order_ref", "roundtrip_ref", "allocated_quantity"},
}
TABLES = {"historical_meta", "historical_sources", "historical_orders", "historical_roundtrips", "historical_links", "historical_imports", "historical_conflicts"}
SCHEMA_SQL = {
    "historical_meta": "CREATE TABLE historical_meta (schema TEXT NOT NULL, store_uuid TEXT NOT NULL, created_at TEXT NOT NULL)",
    "historical_sources": "CREATE TABLE historical_sources (source_key TEXT PRIMARY KEY, metadata TEXT NOT NULL, first_imported_at TEXT NOT NULL)",
    "historical_imports": "CREATE TABLE historical_imports (manifest_sha256 TEXT PRIMARY KEY, imported_at TEXT NOT NULL, metadata TEXT NOT NULL)",
    "historical_conflicts": "CREATE TABLE historical_conflicts (identity TEXT NOT NULL, existing_sha256 TEXT NOT NULL, incoming_sha256 TEXT NOT NULL, manifest_sha256 TEXT NOT NULL, observed_at TEXT NOT NULL, UNIQUE(identity,existing_sha256,incoming_sha256,manifest_sha256))",
}
for _entity in ENTITIES:
    SCHEMA_SQL["historical_" + _entity] = f"CREATE TABLE historical_{_entity} (source_key TEXT NOT NULL REFERENCES historical_sources(source_key), record_ref TEXT NOT NULL, payload TEXT NOT NULL, content_sha256 TEXT NOT NULL, known_at TEXT NOT NULL, PRIMARY KEY(source_key,record_ref))"
TRIGGER_SQL = {}
for _table in ("historical_sources", "historical_orders", "historical_roundtrips", "historical_links"):
    for _action in ("UPDATE", "DELETE"):
        _name = f"{_table}_no_{_action.lower()}"
        TRIGGER_SQL[_name] = f"CREATE TRIGGER {_name} BEFORE {_action} ON {_table} BEGIN SELECT RAISE(ABORT, 'Historical facts are immutable'); END"


class HistoryError(RuntimeError):
    """Locally composed validation error without source text or secrets."""


class HistoryConflict(HistoryError):
    def __init__(self, conflicts):
        self.conflicts = conflicts
        super().__init__(f"Historical import quarantined: {len(conflicts)} conflicting source/record identities; existing facts unchanged.")


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value):
    return hashlib.sha256(value).hexdigest()


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise HistoryError("Duplicate JSON object key.")
        result[key] = value
    return result


def _json(raw):
    try:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        return json.loads(raw, object_pairs_hook=_object,
            parse_constant=lambda _: (_ for _ in ()).throw(HistoryError("Non-finite JSON value.")))
    except (ValueError, UnicodeError, RecursionError):
        raise HistoryError("Invalid bounded UTF-8 JSON input.") from None


def _keys(value, expected, label):
    if not isinstance(value, dict) or set(value) != set(expected):
        raise HistoryError(f"Unsupported {label} fields; use the explicit canonical contract.")


def _id(value):
    if not isinstance(value, str) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", value) is None:
        raise HistoryError("Invalid pseudonymous reference or lineage identifier.")
    return value


def _decimal(value, *, nonnegative=True, positive=False):
    if value is None:
        return None
    if (not isinstance(value, str) or len(value) > 80
            or re.fullmatch(r"[+-]?[0-9]+(?:\.[0-9]+)?(?:[eE][+-]?[0-9]{1,3})?", value) is None):
        raise HistoryError("Quantities and amounts must be bounded decimal strings or null, never JSON floats.")
    try:
        number = Decimal(value)
    except InvalidOperation:
        raise HistoryError("Invalid decimal string.") from None
    if not number.is_finite() or (nonnegative and number < 0) or (positive and number <= 0):
        raise HistoryError("Invalid quantity or amount range.")
    return number


def _time(value, meaning):
    if value is None:
        return
    _keys(value, {"raw", "timezone", "meaning"}, "stored timestamp")
    if (value["meaning"] != meaning or not isinstance(value["raw"], str)
            or not 1 <= len(value["raw"]) <= 80 or not value["raw"].isprintable()
            or value["timezone"] is not None and (not isinstance(value["timezone"], str)
                or not 1 <= len(value["timezone"]) <= 80 or not value["timezone"].isprintable())):
        raise HistoryError("Invalid stored timestamp meaning; timezone and raw value must be preserved.")


def _path(value):
    path = Path(value).expanduser().absolute()
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise HistoryError("Symlinks are not accepted in historical source or database paths.")
    return path


def _read(path, limit):
    descriptor = None
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = None
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise HistoryError("Historical input must be a regular file.")
            raw = stream.read(limit + 1)
            after = os.fstat(stream.fileno())
        if _fingerprint(before) != _fingerprint(after) or len(raw) != before.st_size and len(raw) <= limit:
            raise HistoryError("Historical input changed during its bounded read.")
        if len(raw) > limit:
            raise HistoryError("Historical input exceeds its size limit.")
        return raw
    except OSError:
        raise HistoryError("Historical input could not be read safely.") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


@dataclass(frozen=True)
class ValidatedBundle:
    root: Path
    manifest: dict
    manifest_sha256: str
    records: dict
    summary: dict


def _source_key(value):
    return value["lineage_id"], value["cohort"]


def _fingerprint(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _validate_record(entity, row, sources):
    _keys(row, FIELDS[entity], entity)
    if not isinstance(row["cohort"], str):
        raise HistoryError("Invalid historical cohort.")
    key = (_id(row["lineage_id"]), row["cohort"])
    _id(row["record_ref"])
    if key not in sources:
        raise HistoryError("Record refers to an undeclared lineage/cohort.")
    for field in MONEY[entity]:
        _decimal(row[field], nonnegative=field not in {"gross_pnl", "net_pnl"}, positive=field in {"requested_price", "quantity", "allocated_quantity"})
    if entity == "links":
        _id(row["order_ref"])
        _id(row["roundtrip_ref"])
        return
    if not isinstance(row["symbol"], str) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,31}", row["symbol"]) is None:
        raise HistoryError("Invalid historical symbol.")
    if not isinstance(row["cost_state"], str) or row["cost_state"] not in {"unknown", "assumed"}:
        raise HistoryError("v1 supports unknown historical costs or explicitly assumed shadow costs only.")
    if row["cohort"] == "kr_mock" and row["cost_state"] != "unknown":
        raise HistoryError("Preserved MOCK costs must remain unknown in this contract.")
    if row["cost_state"] == "unknown" and any(row[field] is not None for field in ("commission", "tax")):
        raise HistoryError("Unknown costs must retain null commission and tax.")
    if row["cost_state"] == "assumed" and any(row[field] is None for field in ("commission", "tax")):
        raise HistoryError("Assumed shadow costs require explicit commission and tax strings.")
    if entity == "orders":
        if (not isinstance(row["side"], str) or row["side"] not in {"BUY", "SELL"}
                or not isinstance(row["status"], str) or row["status"] not in {"NEW", "ACCEPTED", "PARTIAL", "FILLED", "CANCELLED", "REJECTED", "UNKNOWN"}):
            raise HistoryError("Invalid historical order side/status.")
        requested, filled = _decimal(row["requested_quantity"]), _decimal(row["filled_quantity"])
        if requested is not None and filled is not None and filled > requested:
            raise HistoryError("Filled quantity exceeds requested quantity.")
        if row["actual_fill_at"] is not None or row["decision_reason"] is not None:
            raise HistoryError("v1 aggregate history has no independent fill timestamp or decision reason evidence.")
        _time(row["created_at"], "order_created_at")
        _time(row["updated_at"], "order_updated_at")
    else:
        if row["entry_reason"] is not None or row["exit_reason"] is not None:
            raise HistoryError("Do not reconstruct missing historical decision reasons.")
        if row["cost_state"] == "unknown" and row["net_pnl"] is not None:
            raise HistoryError("Unknown costs cannot have confirmed or inferred net profit.")
        _time(row["opened_at"], "ledger_opened_at")
        _time(row["closed_at"], "ledger_closed_at")


def _summaries(sources, records):
    output = []
    groups = {}
    for entity in ENTITIES:
        for row in records[entity]:
            groups.setdefault((_source_key(row), entity), []).append(row)
    with localcontext() as context:
        context.prec = 4096
        for source in sources:
            key = _source_key(source)
            selected = {entity: groups.get((key, entity), []) for entity in ENTITIES}
            totals = {}
            for entity in ENTITIES:
                totals[entity] = {}
                for field in MONEY[entity]:
                    values = [_decimal(row[field], nonnegative=False) for row in selected[entity]]
                    known = [value for value in values if value is not None]
                    totals[entity][field] = {"sum_of_known": str(sum(known, Decimal(0))), "known_count": len(known), "null_count": len(values) - len(known)}
            trips = selected["roundtrips"]
            residuals = []
            for row in trips:
                if all(row[field] is not None for field in ("buy_amount", "sell_amount", "gross_pnl")):
                    delta = Decimal(row["sell_amount"]) - Decimal(row["buy_amount"]) - Decimal(row["gross_pnl"])
                    if delta:
                        residuals.append({"record_ref": row["record_ref"], "gross_equation_residual": str(delta)})
                if all(row[field] is not None for field in ("gross_pnl", "commission", "tax", "net_pnl")):
                    delta = Decimal(row["gross_pnl"]) - Decimal(row["commission"]) - Decimal(row["tax"]) - Decimal(row["net_pnl"])
                    if delta:
                        residuals.append({"record_ref": row["record_ref"], "net_equation_residual": str(delta)})
            output.append({"lineage_id": key[0], "cohort": key[1], "currency": source["currency"],
                "snapshot_sha256": source["snapshot_sha256"], "snapshot_recorded_at": source["snapshot_recorded_at"],
                "counts": {entity: len(selected[entity]) for entity in ENTITIES}, "totals": totals,
                "cost_unknown_roundtrips": sum(row["cost_state"] == "unknown" for row in trips),
                "cost_assumed_roundtrips": sum(row["cost_state"] == "assumed" for row in trips),
                "cost_confirmed_roundtrips": 0,
                "partial_cancelled_orders": sum(row["status"] == "CANCELLED" and row["filled_quantity"] is not None and Decimal(row["filled_quantity"]) > 0 for row in selected["orders"]),
                "order_status_counts": dict(Counter(row["status"] for row in selected["orders"])),
                "arithmetic_residuals": residuals,
                "evidence_scope": EVIDENCE_SCOPE,
                "confirmed_net_pnl": None, "confirmed_win_rate": None, "account_return": None, "mdd": None,
                "historical_only": True, "current_account_verified": False,
                "source_completeness": "partial_or_unknown", "pit_eligible": False})
    return output


def validate_bundle(directory):
    root = _path(directory)
    if not root.is_dir():
        raise HistoryError("Canonical bundle directory does not exist.")
    raw_manifest = _read(_path(root / "manifest.json"), 1024 * 1024)
    manifest = _json(raw_manifest)
    _keys(manifest, {"schema", "bundle_kind", "sources", "files"}, "manifest")
    if manifest["schema"] != FORMAT or not isinstance(manifest["bundle_kind"], str) or manifest["bundle_kind"] not in {"synthetic_fixture", "preserved_history"}:
        raise HistoryError("Only explicit ai-stock-history-v1 bundles are supported; no legacy adapter is installed.")
    if not isinstance(manifest["sources"], list) or not 1 <= len(manifest["sources"]) <= 1000:
        raise HistoryError("Invalid canonical source inventory.")
    sources = _sources(manifest["sources"])
    if not isinstance(manifest["files"], list) or len(manifest["files"]) != 3:
        raise HistoryError("Manifest must declare the three canonical JSONL files.")
    records, seen_files = {entity: [] for entity in ENTITIES}, set()
    identities = set()
    for item in manifest["files"]:
        _keys(item, {"entity", "path", "sha256", "bytes", "rows"}, "source file")
        entity = item["entity"]
        if not isinstance(entity, str) or entity not in ENTITIES or entity in seen_files or item["path"] != entity + ".jsonl":
            raise HistoryError("Invalid, duplicate or traversing canonical file path.")
        seen_files.add(entity)
        raw = _read(_path(root / item["path"]), MAX_FILE)
        if type(item["bytes"]) is not int or item["bytes"] != len(raw) or item["sha256"] != _hash(raw):
            raise HistoryError("Source file SHA256 or byte length does not match its manifest.")
        lines = raw.splitlines()
        if type(item["rows"]) is not int or len(lines) != item["rows"] or len(lines) > MAX_ROWS:
            raise HistoryError("Source file row count does not match its manifest.")
        for line in lines:
            if not line or len(line) > 1024 * 1024:
                raise HistoryError("Empty or oversized JSONL row.")
            row = _json(line)
            _validate_record(entity, row, sources)
            identity = (*_source_key(row), entity, row["record_ref"])
            if identity in identities:
                raise HistoryError("Duplicate record reference inside a canonical bundle.")
            identities.add(identity)
            records[entity].append(row)
    summaries = _reconcile(sources, records)
    result = {"schema": FORMAT, "bundle_kind": manifest["bundle_kind"], "manifest_sha256": _hash(raw_manifest),
        "bundle_validated": True, "historical_import_verified": False,
        "evidence_scope": EVIDENCE_SCOPE,
        "authoritative_history_complete": False, "current_account_verified": False, "deployed": False,
        "sources": summaries}
    return ValidatedBundle(root, manifest, _hash(raw_manifest), records, result)


def _sources(items):
    sources = {}
    for source in items:
        _keys(source, {"lineage_id", "cohort", "currency", "snapshot_sha256", "pseudonym_scheme", "snapshot_recorded_at", "source_completeness", "expected_counts"}, "source")
        if not isinstance(source["cohort"], str):
            raise HistoryError("Invalid source cohort.")
        key = (_id(source["lineage_id"]), source["cohort"])
        if key in sources or source["cohort"] not in COHORTS or source["currency"] != COHORTS[source["cohort"]]:
            raise HistoryError("Duplicate source or incompatible cohort/currency.")
        if not isinstance(source["snapshot_sha256"], str) or re.fullmatch(r"[a-f0-9]{64}", source["snapshot_sha256"]) is None:
            raise HistoryError("Missing declared source snapshot SHA256.")
        _id(source["pseudonym_scheme"])
        _time(source["snapshot_recorded_at"], "snapshot_saved_at")
        if source["source_completeness"] != "partial_or_unknown":
            raise HistoryError("Preserved source completeness cannot be promoted by an import.")
        _keys(source["expected_counts"], ENTITIES, "source counts")
        if any(type(number) is not int or not 0 <= number <= MAX_ROWS for number in source["expected_counts"].values()):
            raise HistoryError("Invalid expected source row counts.")
        sources[key] = source
    return sources


def _reconcile(sources, records):
    order_map = {(*_source_key(row), row["record_ref"]): row for row in records["orders"]}
    trip_map = {(*_source_key(row), row["record_ref"]): row for row in records["roundtrips"]}
    pairs = set()
    allocated_orders, allocated_sides = {}, {}
    for row in records["links"]:
        key = _source_key(row)
        order = order_map.get((*key, row["order_ref"]))
        trip = trip_map.get((*key, row["roundtrip_ref"]))
        pair = (*key, row["order_ref"], row["roundtrip_ref"])
        if order is None or trip is None or pair in pairs or order["symbol"] != trip["symbol"]:
            raise HistoryError("Missing, duplicate, cross-source or inconsistent order/roundtrip link.")
        pairs.add(pair)
        amount = _decimal(row["allocated_quantity"])
        if amount is not None:
            with localcontext() as context:
                context.prec = 4096
                order_key, side_key = (*key, row["order_ref"]), (*key, row["roundtrip_ref"], order["side"])
                allocated_orders[order_key] = allocated_orders.get(order_key, Decimal(0)) + amount
                allocated_sides[side_key] = allocated_sides.get(side_key, Decimal(0)) + amount
                if order["filled_quantity"] is not None and allocated_orders[order_key] > Decimal(order["filled_quantity"]):
                    raise HistoryError("Linked allocated quantity exceeds the order's preserved fill quantity.")
                if trip["quantity"] is not None and allocated_sides[side_key] > Decimal(trip["quantity"]):
                    raise HistoryError("Linked allocated quantity exceeds a roundtrip side's preserved quantity.")
    summaries = _summaries(list(sources.values()), records)
    for summary in summaries:
        if summary["counts"] != sources[(summary["lineage_id"], summary["cohort"])]["expected_counts"]:
            raise HistoryError("Cohort/source row counts do not match the manifest.")
    return summaries


def _database_path(value, root=None):
    path = _path(value)
    if path.name != "historical.sqlite3" and not path.name.endswith(".history.sqlite3"):
        raise HistoryError("Use historical.sqlite3 or *.history.sqlite3; operational database names are forbidden.")
    if root is not None and (path == root or root in path.parents):
        raise HistoryError("The historical database must be outside the source bundle.")
    return path


def _schema(db):
    db.execute(f"PRAGMA application_id={APPLICATION_ID}")
    for sql in SCHEMA_SQL.values():
        db.execute(sql)
    db.execute("INSERT INTO historical_meta VALUES (?,?,?)", (FORMAT, str(uuid4()), datetime.now(timezone.utc).isoformat()))
    for sql in TRIGGER_SQL.values():
        db.execute(sql)


def _observation(value):
    try:
        if not isinstance(value, str) or len(value) > 80 or datetime.fromisoformat(value).utcoffset() is None:
            raise ValueError
    except ValueError:
        raise HistoryError("Invalid local historical-store observation timestamp.") from None


def _limits(db):
    if db.execute("PRAGMA page_count").fetchone()[0] * db.execute("PRAGMA page_size").fetchone()[0] > MAX_DATABASE:
        raise HistoryError("Historical database exceeds its size limit.")
    for table in TABLES:
        limit = 1 if table == "historical_meta" else 1000 if table == "historical_sources" else MAX_ROWS
        if db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] > limit:
            raise HistoryError("Historical database exceeds its row limit.")


def _configure(db):
    db.execute("PRAGMA trusted_schema=OFF")
    db.execute("PRAGMA foreign_keys=ON")
    # A malformed local database must not create an unbounded SQL job.
    steps = [0]
    def bounded_query():
        steps[0] += 1
        return steps[0] > 50000
    db.set_progress_handler(bounded_query, 1000)
    if hasattr(db, "setlimit"):
        db.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 2 * 1024 * 1024)
        db.setlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH, 16384)


def _identify(db):
    if db.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID:
        raise HistoryError("This is not an identified historical database; it will not be modified.")
    objects = db.execute("SELECT type,name,sql FROM sqlite_master LIMIT 100").fetchall()
    tables = {name: sql for kind, name, sql in objects if kind == "table"}
    triggers = {name: sql for kind, name, sql in objects if kind == "trigger"}
    if (len(objects) >= 100 or tables != SCHEMA_SQL or triggers != TRIGGER_SQL
            or any(kind not in {"table", "trigger", "index"} or kind == "index" and (sql is not None or not name.startswith("sqlite_autoindex_")) for kind, name, sql in objects)):
        raise HistoryError("Historical database schema identity does not match.")
    meta = db.execute("SELECT schema,store_uuid,created_at FROM historical_meta LIMIT 2").fetchall()
    try:
        if len(meta) != 1 or meta[0][0] != FORMAT or str(UUID(meta[0][1])) != meta[0][1]:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise HistoryError("Invalid independent historical-store identity.") from None
    _observation(meta[0][2])
    _limits(db)


@contextmanager
def _db(path, *, write=False):
    existed = path.exists()
    if any(Path(str(path) + suffix).exists() or Path(str(path) + suffix).is_symlink() for suffix in ("-wal", "-shm", "-journal")):
        raise HistoryError("Active or unresolved SQLite journals are not accepted; operational database recovery is outside this importer.")
    if existed and not path.is_file():
        raise HistoryError("Historical database must be a regular file.")
    if existed and path.stat().st_size > MAX_DATABASE:
        raise HistoryError("Historical database exceeds its size limit.")
    before = _fingerprint(path.stat()) if existed else None
    if not existed and not write:
        raise HistoryError("Historical database does not exist.")
    if not existed:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), 0o600)
        os.close(descriptor)
    # Existing databases are identified through a read-only connection first.
    if existed:
        check = sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)
        try:
            _configure(check)
            _identify(check)
        finally:
            check.close()
    db = sqlite3.connect(path.as_uri() + ("?mode=rw" if write else "?mode=ro&immutable=1"), uri=True, timeout=5)
    try:
        _configure(db)
        if write:
            db.execute("PRAGMA synchronous=FULL")
            if not existed:
                with db:
                    _schema(db)
            _identify(db)
        else:
            db.execute("PRAGMA query_only=ON")
            _identify(db)
        yield db
        if not write and _fingerprint(path.stat()) != before:
            raise HistoryError("Historical database changed during the read-only report.")
    finally:
        db.close()


def _stored(db):
    sources = []
    for key, payload, known_at in db.execute("SELECT source_key,metadata,first_imported_at FROM historical_sources ORDER BY source_key LIMIT 1001"):
        source = _json(payload)
        if not isinstance(source, dict) or source.get("bundle_kind") not in ("synthetic_fixture", "preserved_history"):
            raise HistoryError("Invalid stored source metadata.")
        _sources([{name: value for name, value in source.items() if name != "bundle_kind"}])
        if key != _canonical(_source_key(source)):
            raise HistoryError("Stored source key does not match its metadata.")
        _observation(known_at)
        sources.append(source)
    if len(sources) > 1000:
        raise HistoryError("Historical source inventory exceeds its limit.")
    source_map = _sources([{key: value for key, value in source.items() if key != "bundle_kind"} for source in sources])
    records = {entity: [] for entity in ENTITIES}
    for entity in ENTITIES:
        for key, ref, payload, digest, known_at in db.execute(f"SELECT source_key,record_ref,payload,content_sha256,known_at FROM historical_{entity} ORDER BY source_key,record_ref LIMIT {MAX_ROWS + 1}"):
            if not isinstance(payload, str) or _hash(payload.encode()) != digest:
                raise HistoryError("Stored historical payload checksum mismatch.")
            row = _json(payload)
            _validate_record(entity, row, source_map)
            if key != _canonical(_source_key(row)) or ref != row["record_ref"] or payload != _canonical(row):
                raise HistoryError("Stored historical row identity/canonical payload mismatch.")
            _observation(known_at)
            records[entity].append(row)
        if len(records[entity]) > MAX_ROWS:
            raise HistoryError("Historical record inventory exceeds its limit.")
    _reconcile(source_map, records)
    return sources, records


def _insert_record(db, entity, values):
    db.execute(f"INSERT INTO historical_{entity} VALUES (?,?,?,?,?)", values)


def import_bundle(directory, database):
    bundle = validate_bundle(directory)
    path = _database_path(database, bundle.root)
    moment = datetime.now(timezone.utc).isoformat()
    added = Counter()
    with _db(path, write=True) as db:
        # The lock covers identity checks and insertion, including competing
        # importers. It is never held while reading an external bundle.
        db.execute("BEGIN IMMEDIATE")
        existing_sources, existing_records = _stored(db)
        existing_source_keys = {_source_key(source) for source in existing_sources}
        incoming_sources = {}
        for source in bundle.manifest["sources"]:
            key = _canonical(_source_key(source))
            metadata = {**source, "bundle_kind": bundle.manifest["bundle_kind"]}
            incoming_sources[key] = _canonical(metadata)
        conflicts = []
        for key, payload in incoming_sources.items():
            existing = db.execute("SELECT metadata FROM historical_sources WHERE source_key=?", (key,)).fetchone()
            if existing and existing[0] != payload:
                conflicts.append(("source:" + key, _hash(existing[0].encode()), _hash(payload.encode())))
        # Every source is a complete declared snapshot, not an append stream.
        # Renaming all refs must not silently double that snapshot's rows.
        for source in bundle.manifest["sources"]:
            key = _source_key(source)
            if key not in existing_source_keys:
                continue
            for entity in ENTITIES:
                saved_refs = sorted(row["record_ref"] for row in existing_records[entity] if _source_key(row) == key)
                supplied_refs = sorted(row["record_ref"] for row in bundle.records[entity] if _source_key(row) == key)
                if saved_refs != supplied_refs:
                    conflicts.append(("identity_set:" + entity + ":" + _canonical(key), _hash(_canonical(saved_refs).encode()), _hash(_canonical(supplied_refs).encode())))
        for entity in ENTITIES:
            for row in bundle.records[entity]:
                key, payload = _canonical(_source_key(row)), _canonical(row)
                existing = db.execute(f"SELECT payload FROM historical_{entity} WHERE source_key=? AND record_ref=?", (key, row["record_ref"])).fetchone()
                if existing and existing[0] != payload:
                    conflicts.append((entity + ":" + key + ":" + row["record_ref"], _hash(existing[0].encode()), _hash(payload.encode())))
        if conflicts:
            with db:
                db.executemany("INSERT OR IGNORE INTO historical_conflicts VALUES (?,?,?,?,?)", [(*item, bundle.manifest_sha256, moment) for item in conflicts])
                _limits(db)
            raise HistoryConflict([{"identity": item[0], "existing_sha256": item[1], "incoming_sha256": item[2]} for item in conflicts])
        with db:
            for key, payload in incoming_sources.items():
                added["sources"] += db.execute("INSERT OR IGNORE INTO historical_sources VALUES (?,?,?)", (key, payload, moment)).rowcount
            for entity in ENTITIES:
                for row in bundle.records[entity]:
                    key, payload = _canonical(_source_key(row)), _canonical(row)
                    existing = db.execute(f"SELECT payload FROM historical_{entity} WHERE source_key=? AND record_ref=?", (key, row["record_ref"])).fetchone()
                    if existing is None:
                        _insert_record(db, entity, (key, row["record_ref"], payload, _hash(payload.encode()), moment))
                        added[entity] += 1
                    # Full canonical payload round trip includes nulls, amount
                    # lexemes and every timestamp meaning, not just row counts.
                    saved = db.execute(f"SELECT payload FROM historical_{entity} WHERE source_key=? AND record_ref=?", (key, row["record_ref"])).fetchone()[0]
                    if saved != payload:
                        raise HistoryError("Historical payload round-trip mismatch; batch rolled back.")
            db.execute("INSERT OR IGNORE INTO historical_imports VALUES (?,?,?)", (bundle.manifest_sha256, moment, _canonical(bundle.manifest)))
            _limits(db)
            _stored(db)
    return {**bundle.summary, "historical_import_verified": True,
        "inserted": {name: added[name] for name in ("sources", *ENTITIES)}, "conflicts": 0,
        "database": str(path), "roundtrip_payload_verified": True}


def report_history(database, *, cohort=None):
    if cohort is not None and cohort not in COHORTS:
        raise HistoryError("Unknown preserved-history cohort.")
    path = _database_path(database)
    with _db(path) as db:
        sources, records = _stored(db)
        batches = db.execute("SELECT COUNT(*) FROM historical_imports").fetchone()[0]
        conflicts = db.execute("SELECT COUNT(*) FROM historical_conflicts").fetchone()[0]
        knowledge = {row[0]: row[1] for row in db.execute("SELECT source_key,first_imported_at FROM historical_sources")}
        store_uuid, created_at = db.execute("SELECT store_uuid,created_at FROM historical_meta").fetchone()
    if cohort is not None:
        sources = [source for source in sources if source["cohort"] == cohort]
    summaries = _summaries(sources, records)
    for source in summaries:
        key = _canonical((source["lineage_id"], source["cohort"]))
        source["known_at"] = knowledge[key]
        source["known_at_meaning"] = "local_import_observation_not_historical_fill_or_decision_time"
        source["bundle_kind"] = next(item["bundle_kind"] for item in sources if _source_key(item) == (source["lineage_id"], source["cohort"]))
    return {"schema": FORMAT, "store_uuid": store_uuid, "store_created_at": created_at,
        "historical_import_verified": batches > 0, "evidence_scope": EVIDENCE_SCOPE,
        "stored_payloads_revalidated": True,
        "authoritative_history_complete": False, "current_account_verified": False, "deployed": False,
        "imports": batches, "quarantined_conflicts": conflicts, "sources": summaries,
        "cross_lineage_or_currency_aggregation": "not_performed",
        "limitations": ["Checksums verify this supplied bundle, not authoritative source completeness.",
            "Order and ledger timestamps are not independently proven broker fill timestamps.",
            "Shadow assumed costs and MOCK unknown costs are never combined into confirmed performance.",
            "No operational orders, positions, cash, holds, latches, reservations or account state are imported."]}
