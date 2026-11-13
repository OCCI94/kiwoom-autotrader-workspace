"""Private daily JSONL archives and auditable availability-day feature CSVs."""

from __future__ import annotations

from collections import defaultdict
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import time

from ..trading.message_factors import (
    DAILY_FIELDS, SYMBOL_FIELDS, MessageFactorError, available_at,
    build_daily_factors, kst_date, kst_datetime, normalize_versions,
)


_MANIFEST_LIMIT = 4 * 1024 * 1024
_SCHEMA_VERSION = 1


class MessageDatasetError(RuntimeError):
    """Safe export failure, never including a channel message or credential."""


def _private_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise MessageDatasetError("일자별 데이터 저장 폴더를 확인하세요.")
    path.chmod(0o700)


def _read_manifest(path: Path) -> dict | None:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    try:
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                raise ValueError
            raw = stream.read(_MANIFEST_LIMIT + 1)
        value = json.loads(raw)
        if len(raw) > _MANIFEST_LIMIT or not isinstance(value, dict):
            raise ValueError
        if value.get("schema_version") != _SCHEMA_VERSION:
            return None
        return value
    except (OSError, ValueError, TypeError, RecursionError):
        raise MessageDatasetError("기존 일자별 데이터 명세를 확인하지 못했습니다.") from None


def _write_changed(path: Path, data: bytes) -> bool:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        fd = None
    if fd is not None:
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                raise MessageDatasetError("일자별 데이터 파일을 확인하세요.")
            same = stream.read(len(data) + 1) == data
        if same:
            path.chmod(0o600)
            return False
    temporary = None
    try:
        fd, temporary = tempfile.mkstemp(prefix=".dataset-", dir=path.parent)
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
        return True
    finally:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)


def _csv_bytes(rows: list[dict], fields: tuple[str, ...]) -> bytes:
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    # These exports deliberately contain no message text or channel titles.
    # Even if a future field is textual, never emit a spreadsheet formula.
    for row in rows:
        safe = {}
        for field in fields:
            value = row[field]
            if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
                value = "'" + value
            safe[field] = value
        writer.writerow(safe)
    return stream.getvalue().encode("utf-8-sig")


def _owned_raw_path(relative: str) -> bool:
    return isinstance(relative, str) and re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}/messages\.jsonl", relative) is not None


def _manifest_files_exist(root: Path, manifest: dict) -> bool:
    files = manifest.get("files")
    if not isinstance(files, list):
        return False
    for item in files:
        if not isinstance(item, dict):
            return False
        name = item.get("path")
        if name not in {"daily_factors.csv", "symbol_factors.csv"} and not _owned_raw_path(name):
            return False
        file = root / name
        try:
            info = file.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                return False
            expected = item.get("sha256")
            if not isinstance(expected, str) or re.fullmatch(r"[0-9a-f]{64}", expected) is None:
                return False
            digest = hashlib.sha256()
            fd = os.open(file, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd, "rb") as stream:
                while block := stream.read(1024 * 1024):
                    digest.update(block)
            if digest.hexdigest() != expected:
                return False
            file.chmod(0o600)
        except FileNotFoundError:
            return False
    return True


def _summary(root: Path, manifest: dict, changed: int, cached: bool) -> dict:
    return {
        "days": len(manifest["publication_days"]),
        "versions": manifest["version_count"], "messages": manifest["message_count"],
        "factor_days": manifest["factor_days"], "changed_files": changed, "cached": cached,
        "as_of": manifest["as_of"], "output_root": str(root),
        "daily_factors": str(root / "daily_factors.csv"),
        "symbol_factors": str(root / "symbol_factors.csv"),
        "manifest": str(root / "manifest.json"),
    }


def export_daily_dataset(store, output_root: Path, as_of: int | None = None) -> dict:
    """Export previously selected sources without rewriting historical knowledge.

    Publication-day JSONL files retain every observed revision. CSV factors use
    availability days instead, so a backfilled old post is never placed into an
    old historical information set. Inactive sources remain in the durable
    research dataset; selection changes stop collection, not rewrite history.

    A manifest is written last and records file hashes. Unchanged raw-day files
    are not rewritten. An unchanged ledger can reuse the same-day export, but
    its returned as_of stays at the actual previous export cutoff (never falsely
    claiming newer data). Pending source timestamps invalidate that cache.
    """
    explicit_cutoff = as_of is not None
    cutoff = int(time.time()) if as_of is None else as_of
    try:
        cutoff_day = kst_date(cutoff)
        root = Path(output_root).absolute()
        _private_directory(root)
        previous = _read_manifest(root / "manifest.json")
        revision = store.dataset_revision()
        if type(revision) is not int or revision < 0:
            raise MessageDatasetError("채널 데이터 변경 정보를 확인하지 못했습니다.")
        account_id = getattr(store, "account_id", None)
        if type(account_id) is not int or account_id <= 0:
            raise MessageDatasetError("채널 데이터의 계정 범위를 확인하지 못했습니다.")
        source_identity = hashlib.sha256(f"telegram-account:{account_id}".encode("ascii")).hexdigest()
        if previous is not None:
            previous_cutoff = previous.get("as_of")
            next_available = previous.get("next_available_at")
            same_scope = previous.get("source_scope") == "archived_observed_universe_including_inactive_channels"
            valid_cutoff = type(previous_cutoff) is int and previous_cutoff <= cutoff
            cache_time = valid_cutoff and (
                previous_cutoff == cutoff if explicit_cutoff else kst_date(previous_cutoff) == cutoff_day
            )
            pending_safe = next_available is None or (type(next_available) is int and cutoff < next_available)
            if (previous.get("dataset_revision") == revision and previous.get("source_identity") == source_identity
                    and same_scope and cache_time
                    and pending_safe and _manifest_files_exist(root, previous)):
                return _summary(root, previous, 0, True)

        # Reading the full immutable ledger also discovers future source dates
        # whose eligibility may change later without a new collection event.
        all_rows = normalize_versions(store.versions(active_only=False))
        rows = [row for row in all_rows if row["observed_at"] <= cutoff]
        factors = build_daily_factors(rows, cutoff)
        by_day = defaultdict(list)
        pending = []
        for row in all_rows:
            when = available_at(row)
            if when > cutoff:
                pending.append(when)
        for row in rows:
            by_day[kst_date(row["published_at"])].append({
                **row, "published_at_kst": kst_datetime(row["published_at"]).isoformat(),
                "observed_at_kst": kst_datetime(row["observed_at"]).isoformat(),
                "available_at": available_at(row),
                "available_at_kst": kst_datetime(available_at(row)).isoformat(),
                "publication_date_kst": kst_date(row["published_at"]),
                "availability_date_kst": kst_date(available_at(row)),
            })
        files, changed = [], 0
        for day in sorted(by_day):
            directory = root / day
            _private_directory(directory)
            ordered = sorted(by_day[day], key=lambda row: (
                row["published_at"], row["channel_id"], row["message_id"], row["observed_at"], row["version_id"],
            ))
            data = b"".join((json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8") for row in ordered)
            relative = day + "/messages.jsonl"
            changed += _write_changed(root / relative, data)
            files.append({"path": relative, "sha256": hashlib.sha256(data).hexdigest(), "rows": len(ordered)})
        for name, key, columns in (
            ("daily_factors.csv", "daily", DAILY_FIELDS),
            ("symbol_factors.csv", "symbols", SYMBOL_FIELDS),
        ):
            data = _csv_bytes(factors[key], columns)
            changed += _write_changed(root / name, data)
            files.append({"path": name, "sha256": hashlib.sha256(data).hexdigest(), "rows": len(factors[key])})

        # Only remove obsolete files explicitly owned by a previous manifest,
        # e.g. when the caller deliberately exports an earlier as-of snapshot.
        current_names = {item["path"] for item in files}
        for old in (previous or {}).get("files", []):
            name = old.get("path") if isinstance(old, dict) else None
            if _owned_raw_path(name) and name not in current_names:
                path = root / name
                if path.exists():
                    info = path.lstat()
                    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                        raise MessageDatasetError("이전 일자별 데이터 파일을 확인하세요.")
                    path.unlink()
                    changed += 1
                    try:
                        path.parent.rmdir()
                    except OSError:
                        pass

        manifest = {
            "schema_version": _SCHEMA_VERSION, "timezone": "Asia/Seoul",
            "as_of": cutoff, "as_of_kst": kst_datetime(cutoff).isoformat(),
            "dataset_revision": revision, "next_available_at": min(pending) if pending else None,
            "source_scope": "archived_observed_universe_including_inactive_channels",
            "source_identity": source_identity,
            "channel_ids": sorted({row["channel_id"] for row in rows}),
            "version_count": len(rows),
            "message_count": len({(row["channel_id"], row["message_id"]) for row in rows}),
            "publication_days": sorted(by_day), "factor_days": len(factors["daily"]),
            "raw_partition_basis": "publication date in Asia/Seoul; all known versions retained",
            "factor_date_basis": "availability date of max(observed_at, published_at, edited_at)",
            "daily_cutoff": "23:59:59 Asia/Seoul or as_of, whichever comes first",
            "symbol_extraction": {
                "verified_listings": False, "candidate_only": True,
                "KR": "KR:005930, KRX:005930, A005930, (005930), [005930], 종목코드:005930",
                "US": "$AAPL and explicit dollar-prefixed ticker candidates",
                "counting": "latest eligible intraday revision per observed message; repeated markers count as mentions",
            },
            "limitations": [
                "Availability-day counts are ingestion/revision activity, not original publication-day volume.",
                "Backfilled and legacy snapshot posts are unavailable before their first recorded observation.",
                "Unobserved dates are omitted, not assumed to have zero messages.",
                "Only observed revisions exist; earlier edited/deleted content is not reconstructed.",
                "Previously selected channels remain in the archive; this is not the complete Telegram or stock universe.",
                "No sentiment model, name-to-security mapping, market-calendar alignment, prices or return labels are included.",
                "The current partial day is marked; use completed days or explicit as_of cutoffs for historical research.",
            ],
            "files": files,
        }
        data = (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        changed += _write_changed(root / "manifest.json", data)
        return _summary(root, manifest, changed, False)
    except MessageDatasetError:
        raise
    except Exception:
        raise MessageDatasetError("일자별 채널 데이터와 팩터를 만들지 못했습니다.") from None
