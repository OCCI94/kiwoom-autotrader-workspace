"""Audit explicit local snapshots without broker, credentials or service access.

The source database is never opened by SQLite. A bounded byte snapshot is
opened read-only in a temporary directory, avoiding source WAL/SHM mutations.
An active journal/WAL makes snapshot coherence unknown and blocks inspection.
"""
from collections import Counter, defaultdict
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import tempfile
import time

from .models import AuditReport, Check, Provenance, Verdict

_LIMITS = {"ledger": 64 * 1024 * 1024, "csv": 32 * 1024 * 1024, "state": 64 * 1024}
_MAX_ROWS = 100000
_MAX_PROJECTION_BYTES = 16 * 1024 * 1024
_MAX_SQLITE_VALUE_BYTES = 4 * 1024 * 1024 + 4096
_TABLES = {
    "intents": ("scope", "id", "day", "mode", "side", "symbol", "quantity", "strategy", "order_number", "state"),
    "orders": ("scope", "day", "mode", "ord_no", "symbol", "side", "strategy", "quantity"),
    "fills": ("scope", "day", "mode", "ord_no", "fill_no", "symbol", "side", "moment", "price", "quantity", "strategy"),
    "profits": ("scope", "day", "mode", "symbol", "net", "basis"),
}
_OPTIONAL_TABLES = {
    "order_decisions": ("scope", "intent_id", "recorded_at", "order_price", "order_type", "evidence_json", "evidence_sha256"),
    "profit_observations": ("scope", "day", "mode", "symbol", "digest", "observed_at", "source_kind", "payload_json"),
}
_CSV_FIELDS = {"체결시간", "투자구분", "종목코드", "주문번호", "체결번호", "매수매도", "가격", "수량", "전략", "행구분", "매수합계", "매도합계"}


class _Unavailable(Exception):
    def __init__(self, reason):
        self.reason = reason


def _check(name, verdict, reason, **statistics):
    return Check(name, verdict, reason, tuple(sorted(statistics.items())))


def _read(path, kind):
    try:
        path = Path(path)
        if path.is_symlink():
            raise _Unavailable("SOURCE_SYMLINK_REJECTED")
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
        with os.fdopen(fd, "rb") as handle:
            before = os.fstat(handle.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise _Unavailable("SOURCE_NOT_REGULAR_FILE")
            if before.st_size > _LIMITS[kind]:
                raise _Unavailable("SOURCE_SIZE_LIMIT")
            raw = handle.read(_LIMITS[kind] + 1)
            after = os.fstat(handle.fileno())
        if len(raw) > _LIMITS[kind]:
            raise _Unavailable("SOURCE_SIZE_LIMIT")
        if (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
            raise _Unavailable("SOURCE_CHANGED_DURING_READ")
        return raw
    except FileNotFoundError:
        raise _Unavailable("SOURCE_MISSING") from None
    except (OSError, ValueError, TypeError):
        raise _Unavailable("SOURCE_UNREADABLE") from None


def _journal_present(path):
    try:
        for suffix in ("-wal", "-journal"):
            sidecar = Path(str(path) + suffix)
            if sidecar.is_symlink() or (sidecar.exists() and sidecar.stat().st_size > 0):
                return True
        return False
    except OSError:
        return True


def _day(value):
    if not isinstance(value, str) or re.fullmatch(r"[0-9]{8}", value) is None:
        raise ValueError
    datetime.strptime(value, "%Y%m%d")
    return value


def _decimal(value, *, positive=False):
    if not isinstance(value, (str, int)) or isinstance(value, bool) or len(str(value)) > 128:
        raise ValueError
    if re.fullmatch(r"-?[0-9]+(?:\.[0-9]+)?", str(value)) is None:
        raise ValueError
    number = Decimal(value)
    if not number.is_finite() or (positive and number <= 0):
        raise ValueError
    return number


def _quantity(value):
    number = _decimal(value, positive=True)
    if number != number.to_integral_value() or number > 999999999999:
        raise ValueError
    return int(number)


def _number_id(value):
    if not isinstance(value, str) or re.fullmatch(r"[0-9]{1,7}", value) is None or int(value) <= 0:
        raise ValueError
    return str(int(value))


def _identity(row):
    if (not isinstance(row["scope"], str) or not row["scope"] or row["mode"] not in ("paper", "real")
            or not isinstance(row["symbol"], str) or re.fullmatch(r"[0-9]{6}", row["symbol"]) is None):
        raise ValueError
    return row["scope"], _day(row["day"]), row["mode"]


def _read_ledger(raw):
    with tempfile.TemporaryDirectory(prefix="ai-stock-audit-") as directory:
        snapshot = Path(directory) / "snapshot.sqlite3"
        snapshot.write_bytes(raw)
        database = sqlite3.connect(snapshot.as_uri() + "?mode=ro", uri=True, timeout=1)
        try:
            deadline = time.monotonic() + 5
            database.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
            database.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, _MAX_SQLITE_VALUE_BYTES)
            database.execute("PRAGMA query_only=ON")
            database.execute("PRAGMA trusted_schema=OFF")
            if database.execute("PRAGMA integrity_check(1)").fetchone() != ("ok",):
                raise _Unavailable("LEDGER_INTEGRITY_FAILED")
            database.row_factory = sqlite3.Row
            result = {}
            projected = 0
            for table, columns in {**_TABLES, **_OPTIONAL_TABLES}.items():
                schema = database.execute("SELECT type,sql FROM sqlite_master WHERE name=?", (table,)).fetchone()
                if schema is None and table in _OPTIONAL_TABLES:
                    result[table] = None
                    continue
                if schema is None or schema["type"] != "table" or "VIRTUAL TABLE" in (schema["sql"] or "").upper():
                    raise _Unavailable("LEDGER_SCHEMA_UNSUPPORTED")
                actual = {row["name"] for row in database.execute(f"PRAGMA table_info({table})")}
                if not set(columns) <= actual:
                    raise _Unavailable("LEDGER_SCHEMA_UNSUPPORTED")
                rows = []
                cursor = database.execute(f"SELECT {','.join(columns)} FROM {table} LIMIT {_MAX_ROWS + 1}")
                for row in cursor:
                    if len(rows) >= _MAX_ROWS:
                        raise _Unavailable("LEDGER_ROW_LIMIT")
                    projected += sum(len(value.encode("utf-8")) if isinstance(value, str) else len(value) if isinstance(value, bytes)
                                     else len(str(value)) for value in row)
                    if projected > _MAX_PROJECTION_BYTES:
                        raise _Unavailable("LEDGER_PROJECTION_LIMIT")
                    rows.append(dict(row))
                result[table] = rows
            return result
        finally:
            database.close()


def _ledger_checks(data):
    orders, totals, seen_fills = {}, defaultdict(int), set()
    attribution = 0
    for row in data["orders"]:
        base = _identity(row)
        if row["side"] not in ("buy", "sell") or not isinstance(row["strategy"], str) or not row["strategy"]:
            raise ValueError
        key = (*base, _number_id(row["ord_no"]))
        _quantity(row["quantity"])
        if key in orders:
            attribution += 1
        orders[key] = row
    for row in data["fills"]:
        key = (*_identity(row), _number_id(row["ord_no"]))
        fill_key = (*key, _number_id(row["fill_no"]))
        if fill_key in seen_fills:
            attribution += 1
        seen_fills.add(fill_key)
        datetime.strptime(row["moment"], "%H:%M:%S")
        _decimal(row["price"], positive=True)
        totals[key] += _quantity(row["quantity"])
        order = orders.get(key)
        if order is None or any(row[field] != order[field] for field in ("symbol", "side", "strategy")):
            attribution += 1
    unresolved = cancelled = 0
    for row in data["intents"]:
        base = _identity(row)
        if row["state"] not in ("prepared", "accepted", "cancelled"):
            raise ValueError
        _quantity(row["quantity"])
        if row["order_number"] is None:
            unresolved += row["state"] == "prepared"
            cancelled += row["state"] == "cancelled"
            attribution += row["state"] == "accepted"
        else:
            order = orders.get((*base, _number_id(row["order_number"])))
            if row["state"] != "accepted" or order is None or any(row[field] != order[field] for field in ("symbol", "side", "strategy", "quantity")):
                attribution += 1
    overfills = sum(totals[key] > row["quantity"] for key, row in orders.items())
    partial = sum(0 < totals[key] < row["quantity"] for key, row in orders.items())
    fully = sum(totals[key] == row["quantity"] for key, row in orders.items())
    without_fills = sum(totals[key] == 0 for key in orders)
    sold = {(*_identity(row), row["symbol"]) for row in data["fills"] if row["side"] == "sell"}
    confirmed = set()
    for row in data["profits"]:
        key = (*_identity(row), row["symbol"])
        if row["net"] is not None and row["basis"] is not None:
            _decimal(row["net"])
            _decimal(row["basis"], positive=True)
            confirmed.add(key)
    covered = len(sold & confirmed)
    checks = [
        _check("ledger_records", Verdict.PASS, "LEDGER_RECORDS_PARSED", intents=len(data["intents"]), accepted_orders=len(orders),
               fill_rows=len(data["fills"]), orders_with_fills=sum(totals[key] > 0 for key in orders), partially_filled_orders=partial,
               fully_filled_orders=fully, accepted_without_recorded_fills=without_fills, cancelled_intents=cancelled),
        _check("ledger_attribution", Verdict.NOT_MET if attribution else Verdict.PASS,
               "ORDER_FILL_ATTRIBUTION_MISMATCH" if attribution else "ORDER_FILL_ATTRIBUTION_MATCH", mismatches=attribution),
        _check("ledger_fill_quantity", Verdict.NOT_MET if overfills else Verdict.PASS,
               "ORDER_OVERFILL" if overfills else "RECORDED_FILLS_WITHIN_ORDER_QUANTITY", overfilled_orders=overfills),
        _check("ledger_ambiguous_intents", Verdict.UNKNOWN if unresolved else Verdict.PASS,
               "UNRESOLVED_ORDER_INTENTS" if unresolved else "NO_UNRESOLVED_RECORDED_INTENTS", unresolved_intents=unresolved),
        _check("ledger_cost_coverage", Verdict.UNKNOWN if covered != len(sold) else (Verdict.PASS if sold else Verdict.OBSERVE),
               "COST_COVERAGE_INCOMPLETE" if covered != len(sold) else ("RECORDED_COST_COVERAGE_COMPLETE" if sold else "NO_RECORDED_SELL_FILLS"),
               sell_groups=len(sold), cost_confirmed_groups=covered, cost_unknown_groups=len(sold)-covered),
    ]
    return checks


def _evidence_payload(raw, digest):
    if (not isinstance(raw, str) or not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None
            or hashlib.sha256(raw.encode("utf-8")).hexdigest() != digest):
        raise ValueError
    value = json.loads(raw)
    if not isinstance(value, dict) or type(value.get("schema_version")) is not int or value["schema_version"] != 1:
        raise ValueError
    if value.get("source_kind") not in ("broker_observation", "synthetic", "unknown"):
        raise ValueError
    return value


def _evidence_checks(data):
    checks = []
    decisions = data.get("order_decisions")
    intents = {(row["scope"], row["id"]): row for row in data["intents"]}
    if decisions is None:
        checks.append(_check("decision_evidence", Verdict.UNKNOWN, "HISTORICAL_DECISION_EVIDENCE_UNAVAILABLE", intents=len(intents)))
    else:
        valid, seen, broken, synthetic, broker = set(), set(), 0, 0, 0
        for row in decisions:
            key = row["scope"], row["intent_id"]
            intent = intents.get(key)
            if key in seen or intent is None:
                broken += 1
            seen.add(key)
            if row["evidence_json"] is None and row["evidence_sha256"] is None:
                continue
            try:
                payload = _evidence_payload(row["evidence_json"], row["evidence_sha256"])
                if intent is None or payload.get("strategy") not in (None, intent["strategy"]):
                    raise ValueError
                if row["order_type"] not in ("market", "limit", "unknown"):
                    raise ValueError
                if row["order_type"] == "limit":
                    _decimal(row["order_price"], positive=True)
                elif row["order_price"] is not None:
                    raise ValueError
                valid.add(key)
                synthetic += payload["source_kind"] == "synthetic"
                broker += payload["source_kind"] == "broker_observation"
            except (ValueError, TypeError, KeyError, InvalidOperation, RecursionError):
                broken += 1
        missing = len(set(intents) - valid)
        checks.append(_check("decision_evidence", Verdict.NOT_MET if broken else (Verdict.UNKNOWN if missing else Verdict.PASS),
                             "DECISION_EVIDENCE_HASH_OR_LINK_MISMATCH" if broken else ("DECISION_EVIDENCE_INCOMPLETE" if missing else "RECORDED_DECISION_HASHES_MATCH"),
                             intents=len(intents), evidence_rows=len(decisions), verified_intents=len(valid & set(intents)),
                             missing_or_unverified_intents=missing, mismatches=broken, synthetic_rows=synthetic, broker_labelled_rows=broker))
    observations = data.get("profit_observations")
    profits = {(*_identity(row), row["symbol"]): row for row in data["profits"]}
    if observations is None:
        checks.append(_check("cost_evidence", Verdict.UNKNOWN, "HISTORICAL_COST_EVIDENCE_UNAVAILABLE", profit_groups=len(profits)))
    else:
        covered, broken, valid_rows, synthetic, broker = set(), 0, 0, 0, 0
        for row in observations:
            try:
                key = (*_identity(row), row["symbol"])
                payload = _evidence_payload(row["payload_json"], row["digest"])
                if (payload.get("day") != row["day"] or payload.get("symbol") != row["symbol"]
                        or payload["source_kind"] != row["source_kind"]):
                    raise ValueError
                valid_rows += 1
                synthetic += payload["source_kind"] == "synthetic"
                broker += payload["source_kind"] == "broker_observation"
                projection = profits.get(key)
                if projection is not None and payload.get("net") == projection["net"] and payload.get("basis") == projection["basis"]:
                    covered.add(key)
            except (ValueError, TypeError, KeyError, InvalidOperation, RecursionError):
                broken += 1
        missing = len(set(profits) - covered)
        checks.append(_check("cost_evidence", Verdict.NOT_MET if broken else (Verdict.UNKNOWN if missing else Verdict.PASS),
                             "COST_EVIDENCE_HASH_OR_IDENTITY_MISMATCH" if broken else ("COST_EVIDENCE_INCOMPLETE" if missing else "RECORDED_COST_HASHES_MATCH"),
                             observation_rows=len(observations), verified_rows=valid_rows, profit_groups=len(profits),
                             matched_projection_groups=len(covered), missing_projection_groups=missing,
                             mismatches=broken, synthetic_rows=synthetic, broker_labelled_rows=broker))
    return checks


def _escaped(value):
    return "'" + value if value.startswith(("=", "+", "-", "@", "\t", "\r")) else value


def _csv_checks(raw, path, ledger):
    # At most 128 price digits * 12 quantity digits, summed over 100000 rows.
    # Keep exact principal sums even when the ambient Decimal precision is 28.
    with localcontext() as context:
        context.prec = 160
        return _csv_checks_exact(raw, path, ledger)


def _csv_checks_exact(raw, path, ledger):
    source = csv.DictReader(io.StringIO(raw.decode("utf-8-sig"), newline=""))
    if source.fieldnames is None or len(set(source.fieldnames)) != len(source.fieldnames) or not _CSV_FIELDS <= set(source.fieldnames):
        raise _Unavailable("CSV_SCHEMA_UNSUPPORTED")
    actual, summaries, totals, days = [], [], defaultdict(Decimal), set()
    for index, row in enumerate(source):
        if index >= _MAX_ROWS:
            raise _Unavailable("CSV_ROW_LIMIT")
        if None in row or any(value is None for value in row.values()):
            raise ValueError
        mode = row["투자구분"]
        if mode not in ("paper", "real"):
            raise ValueError
        if row["행구분"] == "일일합계":
            summaries.append(row)
            continue
        if row["행구분"] != "체결" or row["매수매도"] not in ("매수", "매도"):
            raise ValueError
        moment = datetime.strptime(row["체결시간"], "%Y%m%d %H:%M:%S")
        day = moment.strftime("%Y%m%d")
        days.add(day)
        if re.fullmatch(r"[0-9]{6}", row["종목코드"]) is None:
            raise ValueError
        price, quantity = _decimal(row["가격"], positive=True), _quantity(row["수량"])
        actual.append((day, moment.strftime("%H:%M:%S"), mode, row["종목코드"], _number_id(row["주문번호"]),
                       _number_id(row["체결번호"]), row["매수매도"], price, quantity, row["전략"]))
        totals[(mode, row["매수매도"])] += price * quantity
    name = Path(path).stem
    if re.fullmatch(r"[0-9]{8}", name):
        days.add(_day(name))
    if len(days) > 1:
        raise _Unavailable("CSV_MULTIPLE_OR_CONFLICTING_DAYS")
    duplicates = len(actual) - len(set((row[0], row[2], row[4], row[5]) for row in actual))
    checks = [_check("csv_records", Verdict.NOT_MET if duplicates else Verdict.PASS,
                     "CSV_DUPLICATE_FILL_IDS" if duplicates else "CSV_RECORDS_PARSED",
                     fill_rows=len(actual), summary_rows=len(summaries), duplicate_fill_ids=duplicates)]
    mismatches, summary_modes = 0, set()
    for row in summaries:
        mode = row["투자구분"]
        mismatches += mode in summary_modes
        summary_modes.add(mode)
        if (_decimal(row["매수합계"]) != totals[(mode, "매수")]
                or _decimal(row["매도합계"]) != totals[(mode, "매도")]):
            mismatches += 1
    checks.append(_check("csv_summaries", Verdict.NOT_MET if mismatches else (Verdict.PASS if summaries else Verdict.OBSERVE),
                         "CSV_SUMS_MISMATCH" if mismatches else ("CSV_SUMS_MATCH_RECORDED_FILLS" if summaries else "NO_CSV_SUMMARY_ROWS"),
                         mismatches=mismatches))
    if ledger is None or not days:
        checks.append(_check("csv_ledger", Verdict.UNKNOWN, "LEDGER_OR_CSV_DAY_UNAVAILABLE"))
    else:
        day = next(iter(days))
        rows = [row for row in ledger["fills"] if row["day"] == day]
        if len({row["scope"] for row in rows}) > 1:
            checks.append(_check("csv_ledger", Verdict.UNKNOWN, "CSV_ACCOUNT_SCOPE_AMBIGUOUS"))
        else:
            expected = [(day, row["moment"], row["mode"], row["symbol"], _number_id(row["ord_no"]), _number_id(row["fill_no"]),
                         "매수" if row["side"] == "buy" else "매도", _decimal(row["price"], positive=True),
                         _quantity(row["quantity"]), _escaped(row["strategy"])) for row in rows]
            missing = sum((Counter(expected) - Counter(actual)).values())
            extra = sum((Counter(actual) - Counter(expected)).values())
            checks.append(_check("csv_ledger", Verdict.NOT_MET if missing or extra else Verdict.PASS,
                                 "CSV_LEDGER_MISMATCH" if missing or extra else "CSV_MATCHES_RECORDED_LEDGER",
                                 missing_rows=missing, extra_or_changed_rows=extra))
    return checks


def _state_checks(raw):
    state = json.loads(raw.decode("utf-8"))
    if not isinstance(state, dict) or state.get("version") != 1 or type(state.get("version")) is not int:
        raise _Unavailable("RUNTIME_STATE_SCHEMA_UNSUPPORTED")
    status = state.get("status")
    if status not in ("running", "stopped", "failed"):
        raise ValueError
    statistics = {}
    for key in ("poll_count", "received_count", "replied_count"):
        value = state.get(key)
        if type(value) is not int or value < 0:
            raise ValueError
        statistics[key] = value
    return [_check("runtime_records", Verdict.OBSERVE, "PERSISTED_" + status.upper() + "_IS_NOT_LIVE_PROOF", **statistics),
            _check("runtime_readiness", Verdict.UNKNOWN, "CURRENT_ACCOUNT_AND_RUNTIME_NOT_VERIFIED")]


def audit_local(*, ledger_path=None, csv_path=None, state_path=None):
    """Return allowlisted statistics only; no paths, rows or source error text."""
    checks, sources, captured = [], [], {}
    ledger = None
    for kind, path in (("ledger", ledger_path), ("csv", csv_path), ("state", state_path)):
        if path is None:
            checks.append(_check(kind + "_source", Verdict.UNKNOWN, "SOURCE_NOT_SUPPLIED"))
            continue
        try:
            if kind == "ledger" and _journal_present(path):
                raise _Unavailable("LEDGER_ACTIVE_JOURNAL_SNAPSHOT_UNKNOWN")
            raw = _read(path, kind)
            digest = hashlib.sha256(raw).hexdigest()
            captured[kind] = path, digest
            sources.append(Provenance(kind, digest, len(raw)))
            if kind == "ledger":
                data = _read_ledger(raw)
                checks.extend(_ledger_checks(data))
                checks.extend(_evidence_checks(data))
                ledger = data
            elif kind == "csv":
                checks.extend(_csv_checks(raw, path, ledger))
            else:
                checks.extend(_state_checks(raw))
        except _Unavailable as error:
            checks.append(_check(kind + "_source", Verdict.UNKNOWN, error.reason))
        except (OSError, ValueError, TypeError, KeyError, OverflowError, InvalidOperation, UnicodeError, RecursionError, csv.Error, sqlite3.Error):
            checks.append(_check(kind + "_source", Verdict.UNKNOWN, "SOURCE_CONTENT_INVALID_OR_UNREADABLE"))
    for kind, (path, digest) in captured.items():
        try:
            unchanged = hashlib.sha256(_read(path, kind)).hexdigest() == digest
            if kind == "ledger" and _journal_present(path):
                unchanged = False
        except _Unavailable:
            unchanged = False
        checks.append(_check(kind + "_snapshot", Verdict.PASS if unchanged else Verdict.UNKNOWN,
                             "SOURCE_BYTES_UNCHANGED" if unchanged else "SOURCE_CHANGED_DURING_INSPECTION"))
    return AuditReport(datetime.now(timezone.utc).isoformat(), tuple(checks), tuple(sources))
