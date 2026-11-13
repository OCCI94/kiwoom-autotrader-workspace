"""Read-only SHADOW observer for Track 2 Quant Leader (EXP_QLS).

Zero order authority: this module never imports order submission routines,
never contacts network endpoints, never modifies active_arms, and automatically
transitions to OFF on EXPIRES_ON per feature-flag-policy Rule 2.
"""
from __future__ import annotations

from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import stat

from kiwoom_autotrader.api.session import KST
from kiwoom_autotrader.utils.paths import app_data_root

GATE_NAME = "EXP_QLS"
GATE_STATE = "SHADOW"
EXPIRES_ON = "2026-11-27"
ORDER_PERMISSION = False
CHECKPOINT_SCHEMA = "qls-prospective-shadow-checkpoint-v1"
SELECTION_KEY = "qls_shadow:checkpoint"
_MAX_CHECKPOINT_BYTES = 256 * 1024
_SYMBOL_RE = re.compile(r"[0-9]{6}")


def _extract_would_have_ordered(payload: dict) -> list[dict]:
    candidates = payload.get("would_have_ordered")
    if not isinstance(candidates, list):
        candidates = payload.get("target_portfolio")
    if not isinstance(candidates, list):
        return []
    sanitized: list[dict] = []
    for item in candidates[:40]:
        if not isinstance(item, dict):
            continue
        symbol = item.get("symbol")
        if not isinstance(symbol, str) or _SYMBOL_RE.fullmatch(symbol) is None:
            continue
        weight = item.get("target_weight")
        weight_val = round(float(weight), 6) if type(weight) in (int, float) and 0.0 <= float(weight) <= 1.0 else None
        rank = item.get("rank")
        rank_val = rank if type(rank) is int and 1 <= rank <= 500 else None
        sanitized.append({
            "symbol": symbol,
            "target_weight": weight_val,
            "rank": rank_val,
        })
    return sanitized


def observe_qls_shadow(store, now: datetime, *, checkpoint_dir: Path | str | None = None) -> dict:
    """Return bounded SHADOW telemetry and persist once per day when a checkpoint exists."""
    moment = now.astimezone(KST)
    day_iso = moment.date().isoformat()
    day = moment.strftime("%Y%m%d")
    if day_iso >= EXPIRES_ON:
        return {
            "gate": GATE_NAME,
            "state": "OFF",
            "expired": True,
            "expires_on": EXPIRES_ON,
            "order_permission": False,
            "submitted_orders": 0,
            "status": "shadow_expired",
            "day": day,
            "would_have_ordered": [],
        }

    if store is not None:
        try:
            existing = store.selection(day, SELECTION_KEY)
            if isinstance(existing, dict) and existing.get("gate") == GATE_NAME and existing.get("order_permission") is False:
                return dict(existing)
        except Exception:
            pass

    directory = Path(checkpoint_dir) if checkpoint_dir is not None else app_data_root() / "telegram/qls-shadow"
    candidate = directory / f"{day}-qls-shadow-checkpoint.json"
    try:
        if any(p.is_symlink() for p in (candidate, directory)) or not candidate.exists():
            return {
                "gate": GATE_NAME,
                "state": GATE_STATE,
                "expired": False,
                "expires_on": EXPIRES_ON,
                "order_permission": False,
                "submitted_orders": 0,
                "status": "awaiting_daily_checkpoint",
                "day": day,
                "would_have_ordered": [],
            }
        info = candidate.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_CHECKPOINT_BYTES:
            raise ValueError("checkpoint_file_invalid")
        raw = candidate.read_bytes()
        if len(raw) > _MAX_CHECKPOINT_BYTES:
            raise ValueError("checkpoint_size_exceeded")
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict) or payload.get("schema") != CHECKPOINT_SCHEMA:
            raise ValueError("checkpoint_schema_mismatch")
        checkpoint_day = str(payload.get("day") or payload.get("trade_date") or "").replace("-", "")
        if checkpoint_day != day:
            raise ValueError("checkpoint_day_mismatch")
        would_have = _extract_would_have_ordered(payload)
        record = {
            "gate": GATE_NAME,
            "state": GATE_STATE,
            "expired": False,
            "expires_on": EXPIRES_ON,
            "order_permission": False,
            "submitted_orders": 0,
            "status": "observed",
            "day": day,
            "observed_at": moment.isoformat(),
            "checkpoint_sha256": hashlib.sha256(raw).hexdigest(),
            "would_have_ordered_count": len(would_have),
            "would_have_ordered": would_have,
        }
        if store is not None:
            store.selection(day, SELECTION_KEY, record)
        return record
    except Exception:
        return {
            "gate": GATE_NAME,
            "state": GATE_STATE,
            "expired": False,
            "expires_on": EXPIRES_ON,
            "order_permission": False,
            "submitted_orders": 0,
            "status": "checkpoint_invalid",
            "day": day,
            "would_have_ordered": [],
        }
