"""Point-in-time text counts from an observed Telegram message ledger.

These are descriptive availability-day features, not sentiment or trading
signals. Publication time alone never makes a backfilled message available to
a historical feature. Unknown/unobserved days are not filled with zeroes.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime
import re
from typing import Any
from zoneinfo import ZoneInfo


KST = ZoneInfo("Asia/Seoul")
DAILY_FIELDS = (
    "date_kst", "cutoff_at", "cutoff_at_kst", "is_partial_day",
    "observed_message_count", "new_message_count", "updated_message_count",
    "observed_revision_count", "channel_count", "prior_day_backfill_count",
    "legacy_snapshot_count", "text_character_count", "url_count",
    "symbol_candidate_message_count", "symbol_candidate_mentions",
    "distinct_symbol_candidates",
)
SYMBOL_FIELDS = (
    "date_kst", "cutoff_at", "is_partial_day", "market", "symbol",
    "message_count", "mention_count", "channel_count", "candidate_only",
)
_FIELDS = (
    "version_id", "channel_id", "message_id", "published_at", "edited_at",
    "observed_at", "first_observed_at", "origin", "text", "media_type",
    "grouped_id", "title", "username", "permalink",
)
_MAX_ID = (1 << 63) - 1
_KR_PATTERNS = (
    re.compile(r"(?<![A-Za-z0-9_])KRX?\s*:\s*([0-9]{6})(?![0-9])", re.I),
    re.compile(r"(?<![A-Za-z0-9_])A([0-9]{6})(?![A-Za-z0-9_])", re.I),
    re.compile(r"[\[(]\s*([0-9]{6})\s*[\])]"),
    re.compile(r"종목\s*코드\s*[:：]?\s*([0-9]{6})(?![0-9])"),
)
_US_PATTERN = re.compile(r"(?<![A-Za-z0-9_$])\$([A-Z][A-Z0-9]{0,5}(?:[.-][A-Z0-9]{1,2})?)(?![A-Za-z0-9_.-])", re.I)
_URL_PATTERN = re.compile(r"https?://[^\s<>]+", re.I)


class MessageFactorError(RuntimeError):
    """Safe validation failure without message bodies or source credentials."""


def _timestamp(value: Any) -> int:
    if type(value) is not int or value < 0:
        raise MessageFactorError("시계열 데이터의 시각 형식을 확인하세요.")
    try:
        datetime.fromtimestamp(value, KST)
    except (OverflowError, ValueError, OSError):
        raise MessageFactorError("시계열 데이터의 시각 범위를 확인하세요.") from None
    return value


def kst_datetime(value: int) -> datetime:
    return datetime.fromtimestamp(_timestamp(value), KST)


def kst_date(value: int) -> str:
    return kst_datetime(value).date().isoformat()


def available_at(row: dict) -> int:
    return max(row["observed_at"], row["published_at"], row["edited_at"] or 0)


def normalize_versions(records) -> list[dict]:
    """Validate, allowlist fields and deduplicate repeated observations.

    Version IDs break ties for two revisions first observed in the same second.
    Identical revision content observed more than once remains one event.
    """
    if not isinstance(records, (list, tuple)):
        raise MessageFactorError("시계열 데이터는 게시글 버전 목록이어야 합니다.")
    normalized = []
    for raw in records:
        if not isinstance(raw, dict):
            raise MessageFactorError("시계열 게시글 데이터 형식이 올바르지 않습니다.")
        row = {key: raw.get(key) for key in _FIELDS}
        if any(type(row[key]) is not int or not 0 < row[key] <= _MAX_ID
               for key in ("version_id", "channel_id", "message_id")):
            raise MessageFactorError("시계열 게시글 식별 정보가 올바르지 않습니다.")
        for key in ("published_at", "observed_at"):
            row[key] = _timestamp(row[key])
        if row["edited_at"] is not None:
            row["edited_at"] = _timestamp(row["edited_at"])
            if row["edited_at"] < row["published_at"]:
                raise MessageFactorError("게시글 수정 시각이 게시 시각보다 빠릅니다.")
        row["first_observed_at"] = _timestamp(
            row["observed_at"] if row["first_observed_at"] is None else row["first_observed_at"]
        )
        if row["first_observed_at"] > row["observed_at"]:
            raise MessageFactorError("게시글 최초 관측 시각이 올바르지 않습니다.")
        row["origin"] = row["origin"] or "observed"
        if row["origin"] not in {"observed", "legacy_snapshot"}:
            raise MessageFactorError("게시글 관측 출처를 확인하지 못했습니다.")
        if not isinstance(row["text"], str) or len(row["text"]) > 65536:
            raise MessageFactorError("게시글 본문 형식이 올바르지 않습니다.")
        if row["title"] is None:
            row["title"] = ""
        if not isinstance(row["title"], str) or len(row["title"]) > 256:
            raise MessageFactorError("채널 제목 형식이 올바르지 않습니다.")
        if row["username"] is not None and (
            not isinstance(row["username"], str)
            or re.fullmatch(r"[A-Za-z0-9_]{1,32}", row["username"]) is None
        ):
            raise MessageFactorError("채널 사용자 이름 형식이 올바르지 않습니다.")
        if row["media_type"] is not None and (
            not isinstance(row["media_type"], str)
            or re.fullmatch(r"[A-Za-z0-9_]{1,64}", row["media_type"]) is None
        ):
            raise MessageFactorError("게시글 미디어 종류가 올바르지 않습니다.")
        if row["grouped_id"] is not None and (
            type(row["grouped_id"]) is not int or not -_MAX_ID - 1 <= row["grouped_id"] <= _MAX_ID
        ):
            raise MessageFactorError("게시글 묶음 식별 정보가 올바르지 않습니다.")
        # Generate only Telegram links; do not trust an arbitrary source URL.
        path = row["username"] or f"c/{row['channel_id']}"
        row["permalink"] = f"https://t.me/{path}/{row['message_id']}"
        normalized.append(row)
    normalized.sort(key=lambda row: (row["observed_at"], row["version_id"]))
    output, seen, version_keys, publication_times = [], set(), {}, {}
    for row in normalized:
        key = (row["channel_id"], row["message_id"])
        if key in publication_times and publication_times[key] != row["published_at"]:
            raise MessageFactorError("동일 게시글의 게시 시각이 일치하지 않습니다.")
        publication_times[key] = row["published_at"]
        content = tuple(row[name] for name in (
            "channel_id", "message_id", "published_at", "edited_at", "text", "media_type", "grouped_id", "origin",
        ))
        if row["version_id"] in version_keys and version_keys[row["version_id"]] != content:
            raise MessageFactorError("게시글 버전 식별 정보가 중복됩니다.")
        version_keys[row["version_id"]] = content
        if content not in seen:
            seen.add(content)
            output.append(row)
    return output


def symbol_candidates(text: str) -> Counter:
    """Count explicit code/ticker candidates; not validated security listings.

    Naked six-digit numbers and bare English words are intentionally ignored.
    Repeated markers count as repeated mentions, while daily message counts are
    deduplicated per source message.
    """
    found = Counter()
    kr_spans = set()
    for pattern in _KR_PATTERNS:
        for match in pattern.finditer(text):
            span = match.span(1)
            if span not in kr_spans:
                kr_spans.add(span)
                found[("KR", "KR:" + match.group(1))] += 1
    for match in _US_PATTERN.finditer(text):
        found[("US", "US:" + match.group(1).upper())] += 1
    return found


def build_daily_factors(records, as_of: int) -> dict[str, list[dict]]:
    """Build availability-day features using each day's actual information set.

    A message contributes on a day only if a revision became available then.
    Multiple intraday edits use the latest version available at that day's
    cutoff for text counts. Later edits never alter an earlier day's counts.
    New/backfilled/updated message counts are separate to avoid mistaking
    an initial archive import for original publishing activity.
    """
    as_of = _timestamp(as_of)
    rows = [row for row in normalize_versions(records) if available_at(row) <= as_of]
    by_day = defaultdict(list)
    first = {}
    for row in sorted(rows, key=lambda r: (available_at(r), r["observed_at"], r["version_id"])):
        key = row["channel_id"], row["message_id"]
        day = kst_date(available_at(row))
        first.setdefault(key, day)
        by_day[day].append(row)
    daily, symbols = [], []
    for day in sorted(by_day):
        end = datetime.fromisoformat(day).replace(hour=23, minute=59, second=59, tzinfo=KST)
        day_end = int(end.timestamp())
        cutoff = min(day_end, as_of)
        partial = as_of < day_end
        current = {}
        for row in by_day[day]:
            current[row["channel_id"], row["message_id"]] = row
        selected = list(current.values())
        candidate_mentions = Counter()
        candidate_messages = Counter()
        candidate_channels = defaultdict(set)
        symbol_messages = 0
        for row in selected:
            candidates = symbol_candidates(row["text"])
            symbol_messages += bool(candidates)
            candidate_mentions.update(candidates)
            candidate_messages.update(candidates.keys())
            for candidate in candidates:
                candidate_channels[candidate].add(row["channel_id"])
        new = sum(first[key] == day for key in current)
        daily.append({
            "date_kst": day, "cutoff_at": cutoff,
            "cutoff_at_kst": kst_datetime(cutoff).isoformat(), "is_partial_day": partial,
            "observed_message_count": len(selected), "new_message_count": new,
            "updated_message_count": len(selected) - new,
            "observed_revision_count": len(by_day[day]),
            "channel_count": len({row["channel_id"] for row in selected}),
            "prior_day_backfill_count": sum(
                first[key] == day and kst_date(row["published_at"]) < day
                for key, row in current.items()
            ),
            "legacy_snapshot_count": sum(row["origin"] == "legacy_snapshot" for row in selected),
            "text_character_count": sum(len(row["text"]) for row in selected),
            "url_count": sum(len(_URL_PATTERN.findall(row["text"])) for row in selected),
            "symbol_candidate_message_count": symbol_messages,
            "symbol_candidate_mentions": sum(candidate_mentions.values()),
            "distinct_symbol_candidates": len(candidate_mentions),
        })
        for market, symbol in sorted(candidate_mentions):
            key = market, symbol
            symbols.append({
                "date_kst": day, "cutoff_at": cutoff, "is_partial_day": partial,
                "market": market, "symbol": symbol, "message_count": candidate_messages[key],
                "mention_count": candidate_mentions[key],
                "channel_count": len(candidate_channels[key]), "candidate_only": True,
            })
    return {"daily": daily, "symbols": symbols}
