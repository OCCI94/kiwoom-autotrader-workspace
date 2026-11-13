"""Immutable local research evidence, independent of broker and live settings.

Hashes prove that selected files have not changed. They do not prove that data
was available at a historical decision, that costs were paid, or profitability.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from typing import Iterable

MAX_FILE_BYTES = 16 * 1024 * 1024
MAX_FILES = 256
MAX_TOTAL_BYTES = 64 * 1024 * 1024
SCHEMA = "ai-stock-research-v1"


def aware(value: datetime | str) -> datetime:
    result = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
    if not isinstance(result, datetime) or result.utcoffset() is None:
        raise ValueError("timezone-aware timestamp required")
    return result


def canonical(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _read_regular(path: Path, limit: int) -> bytes:
    path = Path(path)
    if path.is_symlink():
        raise ValueError("symlink input rejected")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    with os.fdopen(fd, "rb") as handle:
        before = os.fstat(handle.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
            raise ValueError("input is not a bounded regular file")
        raw = handle.read(limit + 1)
        after = os.fstat(handle.fileno())
    if len(raw) > limit or len(raw) != before.st_size or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
        raise ValueError("input changed or exceeded bound while reading")
    return raw


def load_json_file(path: Path, limit: int = 1024 * 1024):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result
    def invalid_constant(_):
        raise ValueError("nonfinite JSON constant")
    return json.loads(_read_regular(path, limit).decode("utf-8"), object_pairs_hook=unique, parse_constant=invalid_constant)


def _safe_file(root: Path, relative: str) -> Path:
    # Explicit paths only: no network, automatic discovery or symlink traversal.
    part = Path(relative)
    if not isinstance(relative, str) or not relative or part.is_absolute() or ".." in part.parts or "\\" in relative:
        raise ValueError("input path must be a relative file path")
    current = root
    for segment in part.parts:
        current = current / segment
        if current.is_symlink():
            raise ValueError("symlink input rejected")
    if not current.is_file() or not current.resolve().is_relative_to(root):
        raise ValueError("missing or unsafe research input")
    return current


def fingerprint_files(root: Path, relatives: Iterable[str]) -> list[dict]:
    root = Path(root).resolve(strict=True)
    paths = list(relatives)
    if not paths or len(paths) > MAX_FILES or len(set(paths)) != len(paths):
        raise ValueError("select 1..256 unique files")
    results, total = [], 0
    normalized = set()
    for relative in sorted(paths):
        path = _safe_file(root, relative)
        name = path.relative_to(root).as_posix()
        if name in normalized:
            raise ValueError("duplicate normalized path")
        normalized.add(name)
        raw = _read_regular(path, MAX_FILE_BYTES)
        total += len(raw)
        if total > MAX_TOTAL_BYTES:
            raise ValueError("research bundle too large")
        results.append({"path": name, "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()})
    return results


def _text(value, name: str, limit: int = 4000) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f"invalid {name}")
    return value


def validate_protocol(spec: dict) -> dict:
    keys = {"protocol_id", "hypothesis", "as_of", "market", "currency", "universe", "selection", "planned_trials", "cost_assumptions", "limitations"}
    if not isinstance(spec, dict) or set(spec) != keys:
        raise ValueError("protocol fields missing or unknown")
    for name in ("protocol_id", "hypothesis", "universe", "selection", "cost_assumptions"):
        _text(spec[name], name)
    aware(spec["as_of"])
    if (spec["market"], spec["currency"]) not in {("KR", "KRW"), ("US", "USD")}:
        raise ValueError("one market and its currency required per protocol")
    if type(spec["planned_trials"]) is not int or not 1 <= spec["planned_trials"] <= 100000:
        raise ValueError("planned_trials must be a positive bounded integer")
    if not isinstance(spec["limitations"], list) or not 1 <= len(spec["limitations"]) <= 50:
        raise ValueError("limitations must be explicit")
    for value in spec["limitations"]:
        _text(value, "limitation")
    return json.loads(canonical(spec))


def freeze_protocol(spec: dict, root: Path, files: Iterable[str], output: Path, *, now: datetime | None = None) -> dict:
    protocol = validate_protocol(spec)
    frozen_at = aware(now or datetime.now(timezone.utc))
    fingerprints = fingerprint_files(root, files)
    output = Path(output)
    if output.exists() or output.is_symlink():
        raise FileExistsError("research manifest already exists")
    manifest = {
        "schema": SCHEMA, "frozen_at": frozen_at.isoformat(), "protocol": protocol,
        "protocol_sha256": hashlib.sha256(canonical(protocol)).hexdigest(), "files": fingerprints,
        "input_set_sha256": hashlib.sha256(canonical(fingerprints)).hexdigest(),
        "point_in_time_verified": False, "profitability_verified": False,
        "automatic_orders": False,
    }
    manifest["metadata_sha256"] = hashlib.sha256(canonical(manifest)).hexdigest()
    # Exclusive creation; no implicit directory creation, no previous run overwrite.
    payload = canonical(manifest) + b"\n"
    fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    return manifest


def audit_manifest(manifest_path: Path, root: Path) -> dict:
    """Recheck exact hashes and protocol; never execute anything from a bundle."""
    manifest = load_json_file(manifest_path)
    expected = {"schema", "frozen_at", "protocol", "protocol_sha256", "files", "input_set_sha256", "point_in_time_verified", "profitability_verified", "automatic_orders", "metadata_sha256"}
    if not isinstance(manifest, dict) or set(manifest) != expected or manifest.get("schema") != SCHEMA:
        raise ValueError("unknown research schema")
    if any(manifest[key] is not False for key in ("point_in_time_verified", "profitability_verified", "automatic_orders")):
        raise ValueError("research integrity cannot grant verification or order permissions")
    protocol = validate_protocol(manifest["protocol"])
    frozen_at = aware(manifest["frozen_at"])
    files = manifest["files"]
    if not isinstance(files, list) or not all(isinstance(row, dict) and set(row) == {"path", "bytes", "sha256"} for row in files):
        raise ValueError("invalid input manifest")
    if any(type(row["bytes"]) is not int or row["bytes"] < 0 or not isinstance(row["sha256"], str) or re.fullmatch(r"[0-9a-f]{64}", row["sha256"]) is None for row in files):
        raise ValueError("invalid file fingerprint")
    actual = fingerprint_files(root, (item["path"] for item in files))
    checks = {
        "metadata_hash_matches": hashlib.sha256(canonical({key: value for key, value in manifest.items() if key != "metadata_sha256"})).hexdigest() == manifest["metadata_sha256"],
        "protocol_hash_matches": hashlib.sha256(canonical(protocol)).hexdigest() == manifest.get("protocol_sha256"),
        "input_set_hash_matches": hashlib.sha256(canonical(files)).hexdigest() == manifest.get("input_set_sha256"),
        "file_hashes_match": actual == files,
    }
    return {
        "schema": SCHEMA, "inspection_completed": True, "source_integrity": all(checks.values()),
        "checks": checks, "file_count": len(files),
        "retrospective_protocol": frozen_at > aware(protocol["as_of"]) if all(checks.values()) else None,
        "point_in_time_verified": False, "profitability_verified": False,
        "performance_verified": False, "deployed": False, "readiness_state": "UNKNOWN",
        "limitations": ["Hashes are not authenticated provenance or historical availability.",
                        "Costs, full evaluation denominators and actual fills require independent evidence.",
                        "A passing integrity check never enables a strategy."],
    }


@dataclass(frozen=True)
class CandleEvidence:
    """Provider-specific timestamp meaning; never assume START equals END."""
    provider: str
    market: str
    exchange: str
    symbol: str
    source_at: datetime
    observed_at: datetime
    interval_minutes: int
    timestamp_semantics: str
    revised_at: datetime | None = None

    def __post_init__(self):
        for name in ("provider", "exchange", "symbol"):
            _text(getattr(self, name), name)
        if self.market not in {"KR", "US"} or self.timestamp_semantics not in {"START", "END"}:
            raise ValueError("market and explicit timestamp semantics required")
        if type(self.interval_minutes) is not int or not 1 <= self.interval_minutes <= 1440:
            raise ValueError("invalid candle interval")
        object.__setattr__(self, "source_at", aware(self.source_at))
        object.__setattr__(self, "observed_at", aware(self.observed_at))
        if self.revised_at is not None:
            object.__setattr__(self, "revised_at", aware(self.revised_at))

    @property
    def completed_at(self) -> datetime:
        return self.source_at + timedelta(minutes=self.interval_minutes) if self.timestamp_semantics == "START" else self.source_at

    @property
    def available_at(self) -> datetime:
        return max(self.completed_at, self.observed_at, self.revised_at or self.observed_at)

    def usable_at(self, as_of: datetime) -> bool:
        return self.available_at <= aware(as_of)


@dataclass(frozen=True)
class LabelledSample:
    sample_id: str
    decision_at: datetime
    label_end: datetime
    available_at: datetime
    label_available_at: datetime | None = None

    def __post_init__(self):
        _text(self.sample_id, "sample_id")
        for name in ("decision_at", "label_end", "available_at"):
            object.__setattr__(self, name, aware(getattr(self, name)))
        if self.label_available_at is not None:
            object.__setattr__(self, "label_available_at", aware(self.label_available_at))
            if self.label_available_at < self.label_end:
                raise ValueError("label availability cannot precede label end")
        if self.label_end < self.decision_at:
            raise ValueError("label cannot end before decision")


def walk_forward_splits(samples: Iterable[LabelledSample], *, train_timestamps: int,
                        test_timestamps: int, embargo: timedelta = timedelta(0)) -> list[dict]:
    """Expanding past-only splits, grouping same-time symbols; no optimization.

    Training labels crossing the holdout boundary are purged. A gap before each
    holdout supplies the embargo; future folds never feed back into earlier ones.
    The returned counts include unavailable/PIT-invalid data and dropped labels.
    """
    if any(type(x) is not int or x < 1 for x in (train_timestamps, test_timestamps)):
        raise ValueError("positive train/test sizes required")
    if not isinstance(embargo, timedelta) or embargo < timedelta(0):
        raise ValueError("embargo must be nonnegative")
    rows = list(samples)
    if not all(isinstance(row, LabelledSample) for row in rows) or len({r.sample_id for r in rows}) != len(rows):
        raise ValueError("unique typed samples required")
    if any(b.decision_at < a.decision_at for a, b in zip(rows, rows[1:])):
        raise ValueError("samples must be chronological")
    times = sorted({row.decision_at for row in rows})
    folds = []
    for start in range(train_timestamps, len(times), test_timestamps):
        holdout = times[start:start + test_timestamps]
        if len(holdout) != test_timestamps:
            break  # Explicit complete fixed windows; no opportunistic extension.
        boundary = holdout[0] - embargo
        before = [row for row in rows if row.decision_at < holdout[0]]
        train = [row for row in before if row.available_at <= row.decision_at
                 and row.label_end < boundary and row.label_available_at is not None
                 and row.label_available_at < boundary]
        test = [row for row in rows if row.decision_at in holdout and row.available_at <= row.decision_at]
        invalid = sum(row.available_at > row.decision_at for row in before)
        unknown_label = sum(row.available_at <= row.decision_at and row.label_available_at is None for row in before)
        folds.append({
            "train_ids": [row.sample_id for row in train], "test_ids": [row.sample_id for row in test],
            "test_start": holdout[0].isoformat(), "test_end": holdout[-1].isoformat(),
            "purged_or_embargoed": len(before) - len(train) - invalid - unknown_label,
            "training_pit_invalid": invalid,
            "training_label_availability_unknown": unknown_label,
            "test_pit_invalid": sum(row.decision_at in holdout and row.available_at > row.decision_at for row in rows),
            "status": "READY_FOR_OFFLINE_RESEARCH" if train and test else "INSUFFICIENT_DATA",
            "profitability_verified": False,
        })
    return folds
