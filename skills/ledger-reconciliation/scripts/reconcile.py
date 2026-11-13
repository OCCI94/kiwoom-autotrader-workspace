#!/usr/bin/env python3
"""Reconcile one trading day of the Kiwoom paper-trading SQLite ledger against the
broker observations stored inside the same ledger (kt00009, ka10075, kt00018 snapshot,
ka10072/73/74, kt00007) and the ledger's own TTL-cancellation evidence.

Read-only. Standard library only. Python 3.9+.

The database is ALWAYS opened with a `mode=ro` URI and `PRAGMA query_only`. Nothing is
written, no order is placed, no network call is made. Pass a COPY of the ledger; the live
ledger path (`.../KiwoomAutotrader/runtime/logs/.execution-ledger.sqlite3`) is refused
unless `--allow-live-readonly` is given.

Fee/tax defaults are what the Kiwoom mock account showed on 2026-11-03:
  commission 6,360 KRW on 1,819,730 KRW turnover  -> ~35 bps  (mock account; real accounts differ)
  tax        1,653 KRW on   828,460 KRW of sells  -> ~20 bps  (확인 필요: 2026 securities transaction tax)
Listed ETF/ETN sales carry no securities transaction tax (observed 2026-11-04: KODEX 코스닥150
sold 197,540 KRW, broker tax 0). They are detected by name prefix (ETF_NAME_PREFIXES) or given
with --etf-symbols; --no-etf-heuristic disables the name rule.
Tolerances: --pnl-tolerance-krw (realized/tax, default 100), --commission-tolerance-krw (default 50;
the mock server's own APIs disagree on commission by ~10 KRW per trade), and turnover is tolerated
up to 1 KRW per share because `fills.price` stores a truncated average price.

Exit codes: 0 no discrepancies, 1 discrepancies found, 2 cannot reconcile,
3 usage/runtime error.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import math
import os
import re
import sqlite3
import sys
from collections import OrderedDict, defaultdict, deque
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

KST = _dt.timezone(_dt.timedelta(hours=9))
LIVE_BASENAME = ".execution-ledger.sqlite3"
LIVE_DIR_MARK = os.path.join("KiwoomAutotrader", "runtime", "logs")
ACCOUNT_KEY_RE = re.compile(r"(acnt|acct|account|계좌)", re.IGNORECASE)
ACCOUNT_KEY_EXCLUDE_RE = re.compile(r"(_ord_|_cntr_|_array|_dtl|_prst|_prps)", re.IGNORECASE)
MISSING = "MISSING"
UNRECONCILED = "UNRECONCILED"

CONFIRMED_TTL_STATUSES = ("cancellation_confirmed", "fill_resolved")
REJECTED_TTL_STATUSES = ("cancel_broker_rejected",)


# ----------------------------------------------------------------------------- helpers
def log(msg: str) -> None:
    sys.stderr.write(msg.rstrip("\n") + "\n")


def to_int(value: Any, default: int = 0) -> int:
    """'0000000082' -> 82, '+0.17' -> 0, '' / None -> default."""
    if value is None:
        return default
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip().replace(",", "")
    if text in ("", "-", "+"):
        return default
    try:
        return int(text)
    except ValueError:
        try:
            return int(float(text))
        except ValueError:
            return default


def norm_ord(value: Any) -> str:
    """Broker '0078331' and ledger '78331' are the same order number."""
    text = str(value or "").strip()
    text = text.lstrip("0")
    return text or "0"


def norm_sym(value: Any) -> str:
    """Broker 'A030530' and ledger '030530' are the same symbol."""
    text = str(value or "").strip()
    if len(text) == 7 and text[0] in "AJ" and text[1:].isdigit():
        return text[1:]
    return text


def side_from_io_tp(value: Any) -> Optional[str]:
    text = str(value or "")
    if "매수" in text:
        return "buy"
    if "매도" in text:
        return "sell"
    return None


def is_original_row(row: Dict[str, Any], mdfy_key: str, orig_key: str) -> bool:
    """kt00009 returns mdfy_cncl_tp='일반' for an unchanged original (docs say ''),
    '취소' for a cancel child, '정정' for a modify child. Rely on orig_ord_no too."""
    mdfy = str(row.get(mdfy_key) or "").strip()
    orig = norm_ord(row.get(orig_key))
    if mdfy in ("취소", "정정"):
        return False
    return orig == "0"


def bps_amount(amount: int, bps: float) -> int:
    """Floor to whole KRW (the broker's per-fill tax floors; commission rounding is
    unknown on the mock account, see references/fee-tax-table.md)."""
    return int(math.floor(amount * bps / 10000.0))


# Korean listed ETF/ETN brand prefixes (as of 2026, 확인 필요: completeness). Sales of listed
# ETF/ETN carry no securities transaction tax, so tax_bps is not applied to them.
ETF_NAME_PREFIXES = ("KODEX", "TIGER", "RISE", "KBSTAR", "ACE", "KINDEX", "SOL", "PLUS", "ARIRANG", "HANARO",
                     "KOSEF", "KIWOOM", "1Q", "WOORI", "BNK", "TIMEFOLIO", "KCGI", "DAISHIN343", "ITF", "TREX",
                     "UNICORN", "HK", "KOACT", "ISELECT", "VITA", "FOCUS", "마이티", "파워", "히어로즈", "에셋플러스")


def tax_exempt_reason(name: Any, symbol: str, explicit: Any, heuristic: bool) -> Optional[str]:
    """Why a sale carries no transaction tax: explicit --etf-symbols, an ETF brand prefix,
    or 'ETF'/'ETN' in the instrument name. None means ordinary stock (tax applies)."""
    if symbol in explicit:
        return "etf_symbols_option"
    if not heuristic or not isinstance(name, str):
        return None
    n = name.strip().upper()
    for prefix in ETF_NAME_PREFIXES:
        if n.startswith(prefix):
            return "name_prefix:%s" % prefix
    if re.search(r"\bETN\b|\bETF\b", n):
        return "name_contains_etf_etn"
    return None


def mask_accounts(obj: Any, key: str = "") -> Any:
    """Mask any string value whose key looks like an account number: keep last 4."""
    if isinstance(obj, dict):
        return {k: mask_accounts(v, str(k)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [mask_accounts(v, key) for v in obj]
    if isinstance(obj, str) and key and ACCOUNT_KEY_RE.search(key) and not ACCOUNT_KEY_EXCLUDE_RE.search(key):
        if len(obj) > 4:
            return "***" + obj[-4:]
    return obj


def day_iso(day: str) -> str:
    return "%s-%s-%s" % (day[0:4], day[4:6], day[6:8])


def refuse_live_path(db: str, allow_live: bool) -> None:
    candidates = {os.path.abspath(db)}
    try:
        candidates.add(str(Path(db).resolve()))
    except OSError:
        pass
    for cand in candidates:
        if os.path.basename(cand) == LIVE_BASENAME and LIVE_DIR_MARK in cand:
            if not allow_live:
                raise SystemExit(
                    "refusing to open the LIVE ledger %s; copy it first "
                    "(see SKILL.md) or pass --allow-live-readonly" % cand
                )
            log("warning: opening the live ledger read-only (--allow-live-readonly)")


def open_readonly(db: str) -> sqlite3.Connection:
    path = Path(db)
    if not path.is_file():
        raise SystemExit("database not found: %s" % db)
    uri = path.resolve().as_uri() + "?mode=ro"
    con = sqlite3.connect(uri, uri=True, timeout=5.0)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA query_only = 1")
    return con


def table_columns(con: sqlite3.Connection, table: str) -> List[str]:
    try:
        return [r[1] for r in con.execute("PRAGMA table_info(%s)" % table)]
    except sqlite3.Error:
        return []


def rows_to_dicts(cursor: sqlite3.Cursor) -> List[Dict[str, Any]]:
    return [dict(r) for r in cursor.fetchall()]


# ----------------------------------------------------------------------------- reconciler
class Reconciler:
    def __init__(self, con: sqlite3.Connection, day: str, scope: str, mode: str,
                 commission_bps: float, tax_bps: float, limit: int, tolerance: int,
                 commission_tolerance: Optional[int] = None, etf_symbols: Optional[Any] = None,
                 etf_heuristic: bool = True) -> None:
        self.con = con
        self.day = day
        self.scope = scope
        self.mode = mode
        self.commission_bps = commission_bps
        self.tax_bps = tax_bps
        self.limit = limit
        self.tolerance = tolerance
        self.commission_tolerance = tolerance if commission_tolerance is None else commission_tolerance
        self.etf_symbols = set(etf_symbols or ())
        self.etf_heuristic = etf_heuristic
        self.tax_exempt: Dict[str, str] = {}   # symbol -> reason, filled while scanning fills
        self.discrepancies: List[Dict[str, Any]] = []
        self.section_errors: List[Dict[str, str]] = []
        self.tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        # ledger caches
        self.orders: List[Dict[str, Any]] = []
        self.fills: List[Dict[str, Any]] = []
        self.fills_by_ord: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        self.open_positions: Dict[str, int] = {}
        self.open_position_detail: List[Dict[str, Any]] = []
        self.round_trips: List[Dict[str, Any]] = []
        self.kt00009_rows: List[Dict[str, Any]] = []
        self.kt00009_cancels_by_orig: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        self.ka10075_pending: Dict[str, Dict[str, Any]] = {}

    # ---- plumbing
    def add(self, kind: str, label: str, detail: str, symbol: Optional[str] = None,
            order_number: Optional[str] = None) -> None:
        self.discrepancies.append(OrderedDict([
            ("kind", kind), ("symbol", symbol), ("order_number", order_number),
            ("detail", detail), ("label", label),
        ]))

    def section(self, name: str, fn):
        try:
            return fn()
        except sqlite3.Error as exc:
            self.section_errors.append({"section": name, "error": "sqlite: %s" % exc})
        except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            self.section_errors.append({"section": name, "error": "%s: %s" % (type(exc).__name__, exc)})
        return None

    def has(self, table: str) -> bool:
        return table in self.tables

    def latest_observation(self, source_api: str, require_success: bool = True) -> Optional[Dict[str, Any]]:
        """Latest broker_observations row (by id) for day/scope/mode and source_api."""
        if not self.has("broker_observations"):
            return None
        cur = self.con.execute(
            "SELECT id, observed_at, source_api, digest, payload_json FROM broker_observations "
            "WHERE scope=? AND day=? AND mode=? AND source_api=? ORDER BY id DESC LIMIT ?",
            (self.scope, self.day, self.mode, source_api, max(1, min(self.limit, 50))),
        )
        for row in cur.fetchall():
            try:
                payload = json.loads(row["payload_json"])
            except (TypeError, ValueError):
                continue
            if require_success and payload.get("status") not in (None, "success"):
                continue
            if require_success and to_int((payload.get("response_body") or {}).get("return_code"), 0) != 0:
                continue
            return {"id": row["id"], "observed_at": row["observed_at"], "digest": row["digest"], "payload": payload}
        return None

    def latest_observation_per_symbol(self, source_api: str) -> Dict[str, Dict[str, Any]]:
        out: Dict[str, Dict[str, Any]] = {}
        if not self.has("broker_observations"):
            return out
        cur = self.con.execute(
            "SELECT id, observed_at, payload_json FROM broker_observations "
            "WHERE scope=? AND day=? AND mode=? AND source_api=? ORDER BY id DESC LIMIT ?",
            (self.scope, self.day, self.mode, source_api, self.limit),
        )
        for row in cur.fetchall():
            try:
                payload = json.loads(row["payload_json"])
            except (TypeError, ValueError):
                continue
            if payload.get("status") not in (None, "success"):
                continue
            sym = norm_sym((payload.get("request_body") or {}).get("stk_cd"))
            if sym and sym not in out:
                out[sym] = {"id": row["id"], "observed_at": row["observed_at"], "payload": payload}
        return out

    # ---- 1. counts
    def counts(self) -> Dict[str, Any]:
        q = self.con.execute
        out: Dict[str, Any] = OrderedDict()
        intents = OrderedDict()
        for r in q("SELECT side, state, COUNT(*) c FROM intents WHERE scope=? AND day=? AND mode=? "
                   "GROUP BY side, state ORDER BY side, state", (self.scope, self.day, self.mode)):
            intents["%s/%s" % (r["side"], r["state"])] = r["c"]
        out["intents"] = intents
        orders = OrderedDict()
        for r in q("SELECT side, COUNT(*) c, SUM(quantity) qty FROM orders WHERE scope=? AND day=? AND mode=? "
                   "GROUP BY side ORDER BY side", (self.scope, self.day, self.mode)):
            orders[r["side"]] = {"count": r["c"], "quantity": to_int(r["qty"])}
        out["orders"] = orders
        fills = OrderedDict()
        for side in ("buy", "sell"):
            rows = [f for f in self.fills if f["side"] == side]
            fills[side] = {
                "count": len(rows),
                "quantity": sum(f["quantity"] for f in rows),
                "amount_krw": sum(f["quantity"] * f["price"] for f in rows),
            }
        out["fills"] = fills
        ttl = OrderedDict()
        if self.has("buy_ttl_cancellation_attempts"):
            ttl["attempts"] = q("SELECT COUNT(*) FROM buy_ttl_cancellation_attempts WHERE scope=? AND day=?",
                                (self.scope, self.day)).fetchone()[0]
        if self.has("buy_ttl_cancellation_outcomes"):
            st = OrderedDict()
            for r in q("SELECT status, COUNT(*) c FROM buy_ttl_cancellation_outcomes WHERE scope=? AND day=? "
                       "GROUP BY status ORDER BY status", (self.scope, self.day)):
                st[r["status"]] = r["c"]
            ttl["outcomes_by_status"] = st
        if self.has("buy_ttl_cancellation_retry_attempts"):
            ttl["retry_attempts"] = q("SELECT COUNT(*) FROM buy_ttl_cancellation_retry_attempts WHERE scope=? AND day=?",
                                      (self.scope, self.day)).fetchone()[0]
        if self.has("buy_ttl_cancellation_retry_outcomes"):
            st = OrderedDict()
            for r in q("SELECT status, COUNT(*) c FROM buy_ttl_cancellation_retry_outcomes WHERE scope=? AND day=? "
                       "GROUP BY status ORDER BY status", (self.scope, self.day)):
                st[r["status"]] = r["c"]
            ttl["retry_outcomes_by_status"] = st
        if self.has("paper_expired_order_certificates"):
            ttl["expired_order_certificates"] = q(
                "SELECT COUNT(*) FROM paper_expired_order_certificates WHERE scope=? AND day=?",
                (self.scope, self.day)).fetchone()[0]
        out["ttl"] = ttl
        obs = OrderedDict()
        if self.has("broker_observations"):
            for r in q("SELECT source_api, COUNT(*) c, MIN(observed_at) first_at, MAX(observed_at) last_at "
                       "FROM broker_observations WHERE scope=? AND day=? AND mode=? GROUP BY source_api "
                       "ORDER BY source_api", (self.scope, self.day, self.mode)):
                obs[r["source_api"]] = {"count": r["c"], "first_at": r["first_at"], "last_at": r["last_at"]}
        out["broker_observations"] = obs
        return out

    # ---- ledger load
    def load_ledger(self) -> None:
        q = self.con.execute
        self.orders = rows_to_dicts(q(
            "SELECT ord_no, symbol, side, strategy, quantity, registered_at FROM orders "
            "WHERE scope=? AND day=? AND mode=? ORDER BY registered_at, ord_no LIMIT ?",
            (self.scope, self.day, self.mode, self.limit)))
        for o in self.orders:
            o["ord_no"] = norm_ord(o["ord_no"])
            o["symbol"] = norm_sym(o["symbol"])
            o["quantity"] = to_int(o["quantity"])
        raw_fills = rows_to_dicts(q(
            "SELECT ord_no, fill_no, symbol, name, side, moment, price, quantity, strategy, remaining, "
            "holdings_at, holdings_status FROM fills WHERE scope=? AND day=? AND mode=? "
            "ORDER BY moment, ord_no, fill_no LIMIT ?", (self.scope, self.day, self.mode, self.limit)))
        self.fills = []
        for f in raw_fills:
            f["ord_no"] = norm_ord(f["ord_no"])
            f["symbol"] = norm_sym(f["symbol"])
            f["price"] = to_int(f["price"])
            f["quantity"] = to_int(f["quantity"])
            self.fills.append(f)
            self.fills_by_ord[f["ord_no"]].append(f)

    def sell_tax(self, fill: Dict[str, Any], amount: int) -> int:
        """Transaction tax for one sale: 0 for listed ETF/ETN, else floor(amount * tax_bps)."""
        reason = tax_exempt_reason(fill.get("name"), fill["symbol"], self.etf_symbols, self.etf_heuristic)
        if reason:
            self.tax_exempt[fill["symbol"]] = reason
            return 0
        return bps_amount(amount, self.tax_bps)

    def filled_qty(self, ord_no: str) -> int:
        # fills.quantity is a provider key snapshot (cumulative per order), one row per order;
        # if several rows exist for one ord_no take the largest, never the sum.
        rows = self.fills_by_ord.get(ord_no) or []
        return max((f["quantity"] for f in rows), default=0)

    def filled_price(self, ord_no: str) -> int:
        rows = self.fills_by_ord.get(ord_no) or []
        if not rows:
            return 0
        best = max(rows, key=lambda f: f["quantity"])
        return best["price"]

    # ---- 2. round trips
    def round_trips_section(self) -> Dict[str, Any]:
        queues: Dict[Tuple[str, str], deque] = defaultdict(deque)
        trips: List[Dict[str, Any]] = []
        unmatched_sells: List[Dict[str, Any]] = []
        for f in self.fills:
            key = (f["symbol"], f["strategy"] or "")
            if f["side"] == "buy":
                queues[key].append({"ord_no": f["ord_no"], "qty": f["quantity"], "price": f["price"], "moment": f["moment"]})
                continue
            if f["side"] != "sell":
                continue
            remaining = f["quantity"]
            legs: List[Dict[str, Any]] = []
            while remaining > 0 and queues[key]:
                buy = queues[key][0]
                take = min(remaining, buy["qty"])
                legs.append({"buy_ord_no": buy["ord_no"], "qty": take, "buy_price": buy["price"],
                             "buy_moment": buy["moment"], "buy_qty_before": buy["qty"]})
                buy["qty"] -= take
                remaining -= take
                if buy["qty"] == 0:
                    queues[key].popleft()
            if remaining > 0:
                unmatched_sells.append({"symbol": f["symbol"], "strategy": f["strategy"], "sell_ord_no": f["ord_no"],
                                        "quantity": remaining, "price": f["price"]})
                self.add("sell_without_ledger_buy", UNRECONCILED,
                         "매도 %d주가 당일 원장 매수 체결과 짝지어지지 않음(전일 이월 또는 누락 매수)" % remaining,
                         f["symbol"], f["ord_no"])
            if not legs:
                continue
            qty = sum(l["qty"] for l in legs)
            buy_amount = sum(l["qty"] * l["buy_price"] for l in legs)
            sell_amount = qty * f["price"]
            commission = bps_amount(buy_amount, self.commission_bps) + bps_amount(sell_amount, self.commission_bps)
            tax = self.sell_tax(f, sell_amount)
            gross = sell_amount - buy_amount
            trips.append(OrderedDict([
                ("symbol", f["symbol"]), ("name", f.get("name")), ("strategy", f["strategy"]),
                ("buy_ord_no", ",".join(sorted({l["buy_ord_no"] for l in legs}))), ("sell_ord_no", f["ord_no"]),
                ("quantity", qty), ("buy_avg_price", round(buy_amount / qty, 2) if qty else None),
                ("sell_price", f["price"]), ("buy_amount_krw", buy_amount), ("sell_amount_krw", sell_amount),
                ("gross_pnl_krw", gross), ("commission_est_krw", commission), ("tax_est_krw", tax),
                ("tax_exempt", self.tax_exempt.get(f["symbol"])),
                ("net_pnl_krw", gross - commission - tax),
                ("partial", len(legs) > 1 or legs[0]["qty"] != legs[0]["buy_qty_before"]),
                ("buy_moment", legs[0]["buy_moment"]), ("sell_moment", f["moment"]),
            ]))
        self.round_trips = trips
        self.open_positions = {}
        self.open_position_detail = []
        for (symbol, strategy), dq in queues.items():
            for buy in dq:
                if buy["qty"] <= 0:
                    continue
                self.open_positions[symbol] = self.open_positions.get(symbol, 0) + buy["qty"]
                self.open_position_detail.append(OrderedDict([
                    ("symbol", symbol), ("strategy", strategy), ("buy_ord_no", buy["ord_no"]),
                    ("quantity", buy["qty"]), ("price", buy["price"]), ("amount_krw", buy["qty"] * buy["price"]),
                    ("buy_moment", buy["moment"]),
                ]))
        totals = OrderedDict([
            ("completed", len(trips)),
            ("gross_pnl_krw", sum(t["gross_pnl_krw"] for t in trips)),
            ("commission_est_krw", sum(t["commission_est_krw"] for t in trips)),
            ("tax_est_krw", sum(t["tax_est_krw"] for t in trips)),
            ("net_pnl_krw", sum(t["net_pnl_krw"] for t in trips)),
        ])
        return OrderedDict([("totals", totals), ("trips", trips), ("open_positions", self.open_position_detail),
                            ("unmatched_sells", unmatched_sells)])

    # ---- 3a. kt00009 vs ledger orders
    def kt00009_section(self) -> Dict[str, Any]:
        obs = self.latest_observation("kt00009")
        out: Dict[str, Any] = OrderedDict()
        if obs is None:
            out["status"] = "no_successful_kt00009_observation"
            self.add("broker_orders_unavailable", UNRECONCILED,
                     "당일 kt00009(당일 주문체결) 성공 관측이 없어 주문 대조 불가", None, None)
            return out
        rows = (obs["payload"].get("response_body") or {}).get("acnt_ord_cntr_prst_array") or []
        self.kt00009_rows = rows
        out["observed_at"] = obs["observed_at"]
        out["observation_id"] = obs["id"]
        out["row_count"] = len(rows)
        originals: Dict[str, Dict[str, Any]] = {}
        cancels: List[Dict[str, Any]] = []
        for r in rows:
            if is_original_row(r, "mdfy_cncl_tp", "orig_ord_no"):
                originals[norm_ord(r.get("ord_no"))] = r
            else:
                cancels.append(r)
                self.kt00009_cancels_by_orig[norm_ord(r.get("orig_ord_no"))].append(r)
        ledger = {o["ord_no"]: o for o in self.orders}
        in_ledger_not_at_broker = []
        for ord_no, o in ledger.items():
            if ord_no not in originals:
                in_ledger_not_at_broker.append({"order_number": ord_no, "symbol": o["symbol"], "side": o["side"],
                                                "quantity": o["quantity"], "strategy": o["strategy"]})
                self.add("order_missing_at_broker", MISSING,
                         "원장 주문이 kt00009 최신 관측에 없음 (%s %d주, %s)" % (o["side"], o["quantity"], o["strategy"]),
                         o["symbol"], ord_no)
        at_broker_not_in_ledger = []
        for ord_no, r in originals.items():
            if ord_no not in ledger:
                at_broker_not_in_ledger.append({"order_number": ord_no, "symbol": norm_sym(r.get("stk_cd")),
                                                "side": side_from_io_tp(r.get("io_tp_nm")),
                                                "ord_qty": to_int(r.get("ord_qty")), "cntr_qty": to_int(r.get("cntr_qty")),
                                                "mdfy_cncl_tp": r.get("mdfy_cncl_tp")})
                self.add("order_not_in_ledger", UNRECONCILED,
                         "증권사 주문이 원장에 없음 (%s 주문 %d주 체결 %d주; 수동 주문이거나 원장 누락)" % (
                             r.get("io_tp_nm"), to_int(r.get("ord_qty")), to_int(r.get("cntr_qty"))),
                         norm_sym(r.get("stk_cd")), ord_no)
        mismatches = []
        for ord_no, o in ledger.items():
            r = originals.get(ord_no)
            if r is None:
                continue
            problems = []
            bsym = norm_sym(r.get("stk_cd"))
            if bsym != o["symbol"]:
                problems.append("symbol ledger=%s broker=%s" % (o["symbol"], bsym))
            bside = side_from_io_tp(r.get("io_tp_nm"))
            if bside != o["side"]:
                problems.append("side ledger=%s broker=%s" % (o["side"], r.get("io_tp_nm")))
            bqty = to_int(r.get("ord_qty"))
            if bqty != o["quantity"]:
                problems.append("ord_qty ledger=%d broker=%d" % (o["quantity"], bqty))
            bfilled = to_int(r.get("cntr_qty"))
            lfilled = self.filled_qty(ord_no)
            if bfilled != lfilled:
                problems.append("filled_qty ledger=%d broker=%d" % (lfilled, bfilled))
            bprice = to_int(r.get("cntr_uv"))
            lprice = self.filled_price(ord_no)
            if bfilled > 0 and lfilled > 0 and bprice != lprice:
                problems.append("fill_price ledger=%d broker=%d" % (lprice, bprice))
            if problems:
                mismatches.append({"order_number": ord_no, "symbol": o["symbol"], "problems": problems})
                label = MISSING if (bfilled > lfilled) else UNRECONCILED
                kind = "fill_missing_in_ledger" if (bfilled > lfilled) else "order_field_mismatch"
                self.add(kind, label, "; ".join(problems), o["symbol"], ord_no)
        out["originals_at_broker"] = len(originals)
        out["cancel_or_modify_rows_at_broker"] = len(cancels)
        out["in_ledger_not_at_broker"] = in_ledger_not_at_broker
        out["at_broker_not_in_ledger"] = at_broker_not_in_ledger
        out["mismatches"] = mismatches
        return out

    # ---- 3b. ka10075 pending vs ledger pending
    def ka10075_section(self) -> Dict[str, Any]:
        out: Dict[str, Any] = OrderedDict()
        obs = self.latest_observation("ka10075")
        broker_pending: Dict[str, Dict[str, Any]] = {}
        if obs is None:
            out["status"] = "no_successful_ka10075_observation"
        else:
            out["observed_at"] = obs["observed_at"]
            out["observation_id"] = obs["id"]
            for r in (obs["payload"].get("response_body") or {}).get("oso") or []:
                broker_pending[norm_ord(r.get("ord_no"))] = r
        self.ka10075_pending = broker_pending
        ledger_pending = []
        for o in self.orders:
            filled = self.filled_qty(o["ord_no"])
            if filled >= o["quantity"]:
                continue
            cancels = self.kt00009_cancels_by_orig.get(o["ord_no"]) or []
            cancel_cnfm = sum(to_int(c.get("cnfm_qty")) for c in cancels)
            cancel_nos = [norm_ord(c.get("ord_no")) for c in cancels]
            at_broker_pending = o["ord_no"] in broker_pending
            broker_row = next((r for r in self.kt00009_rows
                               if is_original_row(r, "mdfy_cncl_tp", "orig_ord_no") and norm_ord(r.get("ord_no")) == o["ord_no"]), None)
            broker_filled = to_int(broker_row.get("cntr_qty")) if broker_row else None
            if at_broker_pending:
                state = "pending_at_broker"
            elif broker_filled is not None and broker_filled >= o["quantity"]:
                state = "filled_at_broker_but_not_in_ledger"
            elif cancel_cnfm >= (o["quantity"] - filled) and cancels:
                state = "cancelled_per_kt00009"
            elif broker_row is None:
                state = "unknown_not_at_broker"
            else:
                state = "unresolved"
            ledger_pending.append(OrderedDict([
                ("order_number", o["ord_no"]), ("symbol", o["symbol"]), ("side", o["side"]), ("strategy", o["strategy"]),
                ("quantity", o["quantity"]), ("filled_in_ledger", filled), ("broker_filled", broker_filled),
                ("cancel_order_numbers", cancel_nos), ("cancel_confirmed_qty_kt00009", cancel_cnfm),
                ("in_ka10075_pending", at_broker_pending), ("state", state),
            ]))
            if state == "filled_at_broker_but_not_in_ledger":
                self.add("fill_missing_in_ledger", MISSING, "증권사 체결 %d주, 원장 체결 %d주" % (broker_filled or 0, filled),
                         o["symbol"], o["ord_no"])
            elif state in ("unknown_not_at_broker", "unresolved"):
                self.add("pending_unresolved", UNRECONCILED,
                         "미체결 주문의 증권사 상태를 확정할 수 없음 (state=%s)" % state, o["symbol"], o["ord_no"])
        broker_not_in_ledger = []
        ledger_nos = {o["ord_no"] for o in self.orders}
        for ord_no, r in broker_pending.items():
            if ord_no not in ledger_nos:
                broker_not_in_ledger.append({"order_number": ord_no, "symbol": norm_sym(r.get("stk_cd")),
                                             "oso_qty": to_int(r.get("oso_qty")), "io_tp_nm": r.get("io_tp_nm")})
                self.add("pending_not_in_ledger", UNRECONCILED,
                         "증권사 미체결(ka10075)에 있으나 원장에 없는 주문 (%s %d주)" % (r.get("io_tp_nm"), to_int(r.get("oso_qty"))),
                         norm_sym(r.get("stk_cd")), ord_no)
        out["broker_pending_count"] = len(broker_pending)
        out["ledger_pending"] = ledger_pending
        out["broker_pending_not_in_ledger"] = broker_not_in_ledger
        return out

    # ---- 3c. holdings vs ledger open positions
    def holdings_section(self) -> Dict[str, Any]:
        out: Dict[str, Any] = OrderedDict()
        broker_holdings: Dict[str, Dict[str, Any]] = {}
        snapshot = None
        if self.has("account_balance_daily"):
            row = self.con.execute(
                "SELECT observed_at, source_kind, payload_json FROM account_balance_daily "
                "WHERE scope=? AND mode=? AND day=? LIMIT 1", (self.scope, self.mode, self.day)).fetchone()
            if row is not None:
                payload = json.loads(row["payload_json"])
                snapshot = {"observed_at": row["observed_at"], "source_kind": row["source_kind"],
                            "schema": payload.get("schema"), "pages": payload.get("pages")}
                for h in payload.get("holdings") or []:
                    sym = norm_sym(h.get("stk_cd"))
                    broker_holdings[sym] = {"symbol": sym, "name": h.get("stk_nm"), "quantity": to_int(h.get("rmnd_qty")),
                                            "purchase_price": to_int(h.get("pur_pric")), "purchase_amount": to_int(h.get("pur_amt")),
                                            "sellable_qty": to_int(h.get("trde_able_qty"))}
        out["kt00018_snapshot"] = snapshot
        out["broker_holdings"] = list(broker_holdings.values())
        ledger_holdings_table = None
        if self.has("holdings"):
            row = self.con.execute("SELECT observed_at, positions, discrepancy FROM holdings WHERE scope=? AND mode=? LIMIT 1",
                                   (self.scope, self.mode)).fetchone()
            if row is not None:
                positions = {}
                try:
                    positions = {norm_sym(k): to_int(v) for k, v in (json.loads(row["positions"] or "{}") or {}).items()}
                except (TypeError, ValueError):
                    positions = {}
                stale = not str(row["observed_at"] or "").startswith(day_iso(self.day))
                ledger_holdings_table = {"observed_at": row["observed_at"], "positions": positions,
                                         "discrepancy": row["discrepancy"], "stale_for_day": stale}
                if stale:
                    self.add("holdings_snapshot_stale", UNRECONCILED,
                             "holdings 표의 observed_at(%s)이 대조일이 아님; day 열이 없으므로 observed_at으로 판단" % row["observed_at"],
                             None, None)
        out["ledger_holdings_table"] = ledger_holdings_table
        out["ledger_open_positions"] = dict(sorted(self.open_positions.items()))
        comparison = []
        symbols = set(broker_holdings) | set(self.open_positions)
        if ledger_holdings_table and not ledger_holdings_table.get("stale_for_day"):
            symbols |= set(ledger_holdings_table["positions"])
        for sym in sorted(symbols):
            bq = broker_holdings.get(sym, {}).get("quantity")
            lq = self.open_positions.get(sym)
            hq = (ledger_holdings_table or {}).get("positions", {}).get(sym) if ledger_holdings_table else None
            entry = {"symbol": sym, "broker_qty": bq, "ledger_open_qty": lq, "holdings_table_qty": hq}
            comparison.append(entry)
            if snapshot is None:
                continue
            if bq is None and lq:
                self.add("position_missing_at_broker", MISSING, "원장 미청산 %d주가 증권사 잔고에 없음" % lq, sym, None)
            elif lq is None and bq:
                self.add("position_not_in_ledger", UNRECONCILED,
                         "증권사 잔고 %d주가 당일 원장 왕복에 없음(전일 이월이면 정상, 아니면 누락)" % bq, sym, None)
            elif bq is not None and lq is not None and bq != lq:
                self.add("position_qty_mismatch", UNRECONCILED, "원장 %d주 vs 증권사 %d주" % (lq, bq), sym, None)
            if hq is not None and bq is not None and hq != bq:
                self.add("holdings_table_mismatch", UNRECONCILED, "holdings 표 %d주 vs 증권사 %d주" % (hq, bq), sym, None)
        if snapshot is None:
            out["status"] = "no_kt00018_snapshot_for_day"
            self.add("broker_holdings_unavailable", UNRECONCILED, "account_balance_daily에 당일 잔고 스냅샷이 없어 잔고 대조 불가", None, None)
        out["comparison"] = comparison
        return out

    # ---- 4. TTL cancellations
    def ttl_section(self) -> Dict[str, Any]:
        out: Dict[str, Any] = OrderedDict()
        if not self.has("buy_ttl_cancellation_attempts"):
            out["status"] = "table_missing"
            return out
        attempts = rows_to_dicts(self.con.execute(
            "SELECT original_ord_no, intent_id, symbol, claimed_at, evidence_json FROM buy_ttl_cancellation_attempts "
            "WHERE scope=? AND day=? ORDER BY claimed_at LIMIT ?", (self.scope, self.day, self.limit)))
        outcomes: Dict[str, Dict[str, Any]] = {}
        if self.has("buy_ttl_cancellation_outcomes"):
            for r in rows_to_dicts(self.con.execute(
                    "SELECT original_ord_no, observed_at, status, cancellation_ord_no, reported_cancel_quantity "
                    "FROM buy_ttl_cancellation_outcomes WHERE scope=? AND day=? LIMIT ?", (self.scope, self.day, self.limit))):
                outcomes[norm_ord(r["original_ord_no"])] = r
        retries: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        if self.has("buy_ttl_cancellation_retry_outcomes"):
            for r in rows_to_dicts(self.con.execute(
                    "SELECT original_ord_no, attempt_sequence, observed_at, status, cancellation_ord_no, reported_cancel_quantity "
                    "FROM buy_ttl_cancellation_retry_outcomes WHERE scope=? AND day=? ORDER BY attempt_sequence LIMIT ?",
                    (self.scope, self.day, self.limit))):
                retries[norm_ord(r["original_ord_no"])].append(r)
        certificates: Dict[str, int] = defaultdict(int)
        if self.has("paper_expired_order_certificates"):
            for r in self.con.execute("SELECT ord_no, COUNT(*) c FROM paper_expired_order_certificates WHERE scope=? AND day=? "
                                      "GROUP BY ord_no LIMIT ?", (self.scope, self.day, self.limit)):
                certificates[norm_ord(r["ord_no"])] = r["c"]
        items = []
        by_settlement = defaultdict(int)
        for a in attempts:
            ord_no = norm_ord(a["original_ord_no"])
            try:
                ev = json.loads(a["evidence_json"] or "{}")
            except (TypeError, ValueError):
                ev = {}
            oc = outcomes.get(ord_no)
            rl = retries.get(ord_no) or []
            last = rl[-1] if rl else None
            status = (last or oc or {}).get("status")
            cancels = self.kt00009_cancels_by_orig.get(ord_no) or []
            cancel_cnfm = sum(to_int(c.get("cnfm_qty")) for c in cancels)
            orig_row = next((r for r in self.kt00009_rows
                             if is_original_row(r, "mdfy_cncl_tp", "orig_ord_no") and norm_ord(r.get("ord_no")) == ord_no), None)
            orig_filled = to_int(orig_row.get("cntr_qty")) if orig_row else None
            ledger_filled = self.filled_qty(ord_no)
            cert = certificates.get(ord_no, 0)
            if cert > 0 or status in CONFIRMED_TTL_STATUSES:
                settlement = "confirmed"
            elif status in REJECTED_TTL_STATUSES:
                settlement = "rejected"
            elif status is None:
                settlement = "no_outcome_record"
            else:
                settlement = "unconfirmed"
            by_settlement[settlement] += 1
            principal = ev.get("ack_releases_principal")
            item = OrderedDict([
                ("original_order_number", ord_no), ("symbol", norm_sym(a["symbol"])), ("strategy", ev.get("strategy")),
                ("intent_id", a["intent_id"]), ("claimed_at", a["claimed_at"]), ("due_reason", ev.get("due_reason")),
                ("ttl_seconds", ev.get("ttl_seconds")), ("broker_remaining_qty_at_claim", ev.get("broker_remaining_qty")),
                ("cancel_request_qty", ev.get("cancel_request_qty")),
                ("principal_reservation_released_by_ack", principal),
                ("outcome_status", status), ("cancellation_order_number", norm_ord((last or oc or {}).get("cancellation_ord_no")) if (last or oc or {}).get("cancellation_ord_no") else None),
                ("reported_cancel_quantity", (last or oc or {}).get("reported_cancel_quantity")),
                ("retry_outcomes", len(rl)), ("expired_order_certificates", cert),
                ("broker_rest_kt00009", {"cancel_rows": len(cancels), "cancel_confirmed_qty": cancel_cnfm,
                                         "original_filled_qty": orig_filled,
                                         "original_in_ka10075_pending": ord_no in self.ka10075_pending}),
                ("ledger_filled_qty", ledger_filled), ("settlement", settlement),
            ])
            items.append(item)
            if settlement == "unconfirmed":
                rest = ("REST kt00009: 취소행 %d건 확인수량 %d, 원주문 체결 %s주" % (len(cancels), cancel_cnfm,
                        "?" if orig_filled is None else str(orig_filled)))
                extra = "" if principal else "; 원금 예약 미해제(ack_releases_principal=%s)" % principal
                self.add("ttl_settlement_unconfirmed", UNRECONCILED,
                         "원장 TTL 취소 상태 %s — 취소 ACK는 정산이 아님. %s%s" % (status, rest, extra), norm_sym(a["symbol"]), ord_no)
            elif settlement == "rejected":
                if ledger_filled > 0 or (orig_filled or 0) > 0:
                    self.add("ttl_cancel_rejected_order_filled", UNRECONCILED,
                             "취소 거부(%s); 원주문 체결 원장 %d주 / 증권사 %s주 → 포지션으로 남음" % (
                                 status, ledger_filled, "?" if orig_filled is None else str(orig_filled)),
                             norm_sym(a["symbol"]), ord_no)
                else:
                    self.add("ttl_cancel_rejected", UNRECONCILED, "취소 거부(%s)이나 체결도 확인되지 않음" % status,
                             norm_sym(a["symbol"]), ord_no)
            elif settlement == "no_outcome_record":
                self.add("ttl_outcome_missing", MISSING, "TTL 취소 시도는 있으나 outcome 레코드가 없음", norm_sym(a["symbol"]), ord_no)
        for ord_no, oc in outcomes.items():
            if not any(i["original_order_number"] == ord_no for i in items):
                self.add("ttl_outcome_without_attempt", UNRECONCILED, "outcome(%s)만 있고 attempt가 없음" % oc.get("status"),
                         None, ord_no)
        out["attempts"] = len(attempts)
        out["by_settlement"] = dict(by_settlement)
        out["items"] = items
        return out

    # ---- 5. realized P&L vs ka10074 / ka10072 / ka10073
    def pnl_section(self) -> Dict[str, Any]:
        out: Dict[str, Any] = OrderedDict()
        buy_turnover = sum(f["quantity"] * f["price"] for f in self.fills if f["side"] == "buy")
        sell_turnover = sum(f["quantity"] * f["price"] for f in self.fills if f["side"] == "sell")
        commission_all = sum(bps_amount(f["quantity"] * f["price"], self.commission_bps) for f in self.fills)
        tax_all = sum(self.sell_tax(f, f["quantity"] * f["price"]) for f in self.fills if f["side"] == "sell")
        buy_shares = sum(f["quantity"] for f in self.fills if f["side"] == "buy")
        sell_shares = sum(f["quantity"] for f in self.fills if f["side"] == "sell")
        trips_gross = sum(t["gross_pnl_krw"] for t in self.round_trips)
        trips_comm = sum(t["commission_est_krw"] for t in self.round_trips)
        trips_tax = sum(t["tax_est_krw"] for t in self.round_trips)
        ledger = OrderedDict([
            ("buy_turnover_krw", buy_turnover), ("sell_turnover_krw", sell_turnover),
            ("commission_est_all_fills_krw", commission_all), ("tax_est_krw", tax_all),
            ("round_trip_gross_krw", trips_gross), ("round_trip_commission_est_krw", trips_comm),
            ("round_trip_tax_est_krw", trips_tax), ("round_trip_net_krw", trips_gross - trips_comm - trips_tax),
            ("open_position_cost_krw", sum(p["amount_krw"] for p in self.open_position_detail)),
            ("params", {"commission_bps": self.commission_bps, "tax_bps": self.tax_bps, "tolerance_krw": self.tolerance,
                        "commission_tolerance_krw": self.commission_tolerance,
                        "turnover_tolerance_krw_per_share": 1, "etf_heuristic": self.etf_heuristic,
                        "etf_symbols_option": sorted(self.etf_symbols)}),
            ("tax_exempt_symbols", OrderedDict(sorted(self.tax_exempt.items()))),
        ])
        out["ledger"] = ledger
        out["notes"] = []
        obs = self.latest_observation("ka10074")
        if obs is None:
            out["ka10074"] = {"status": "no_successful_ka10074_observation"}
            self.add("realized_pnl_unavailable", UNRECONCILED, "당일 ka10074(일자별 실현손익) 관측이 없어 손익 대조 불가", None, None)
        else:
            rb = obs["payload"].get("response_body") or {}
            broker = OrderedDict([
                ("observed_at", obs["observed_at"]), ("observation_id", obs["id"]),
                ("buy_amt", to_int(rb.get("tot_buy_amt"))), ("sell_amt", to_int(rb.get("tot_sell_amt"))),
                ("commission", to_int(rb.get("trde_cmsn"))), ("tax", to_int(rb.get("trde_tax"))),
                ("realized", to_int(rb.get("rlzt_pl"))),
            ])
            out["ka10074"] = broker
            deltas = OrderedDict([
                ("buy_amt", buy_turnover - broker["buy_amt"]),
                ("sell_amt", sell_turnover - broker["sell_amt"]),
                ("commission", commission_all - broker["commission"]),
                ("tax", tax_all - broker["tax"]),
                ("realized", (trips_gross - trips_comm - trips_tax) - broker["realized"]),
            ])
            out["delta_ledger_minus_broker"] = deltas
            for field, delta in deltas.items():
                if field in ("buy_amt", "sell_amt"):
                    # fills.price is a truncated average; allow up to 1 KRW per share of rounding.
                    shares = buy_shares if field == "buy_amt" else sell_shares
                    if delta == 0:
                        continue
                    if abs(delta) <= shares:
                        out["notes"].append("turnover_rounding_%s: 차이 %+d원은 %d주의 체결가 소수점 절사 범위(±%d원) 이내"
                                            % (field, delta, shares, shares))
                    else:
                        self.add("turnover_mismatch_%s" % field, UNRECONCILED,
                                 "원장 체결금액과 ka10074 %s 차이 %+d원 (주당 1원 절사 허용 ±%d원 초과)" % (field, delta, shares), None, None)
                    continue
                tol = self.commission_tolerance if field == "commission" else self.tolerance
                if abs(delta) > tol:
                    self.add("pnl_mismatch_%s" % field, UNRECONCILED,
                             "원장 추정 %s과 ka10074 차이 %+d원 (허용 %d원 초과; 수수료율·절사 규칙·ETF 면세 확인)" % (field, delta, tol),
                             None, None)
        per_symbol = []
        k72 = self.latest_observation_per_symbol("ka10072")
        k73 = self.latest_observation_per_symbol("ka10073")
        ledger_by_sym: Dict[str, Dict[str, int]] = defaultdict(lambda: {"gross": 0, "commission": 0, "tax": 0, "net": 0, "trips": 0})
        for t in self.round_trips:
            d = ledger_by_sym[t["symbol"]]
            d["gross"] += t["gross_pnl_krw"]; d["commission"] += t["commission_est_krw"]
            d["tax"] += t["tax_est_krw"]; d["net"] += t["net_pnl_krw"]; d["trips"] += 1
        for sym in sorted(set(ledger_by_sym) | set(k72) | set(k73)):
            entry: Dict[str, Any] = OrderedDict([("symbol", sym), ("ledger", ledger_by_sym.get(sym))])
            for api, key, store in (("ka10072", "dt_stk_div_rlzt_pl", k72), ("ka10073", "dt_stk_rlzt_pl", k73)):
                o = store.get(sym)
                if o is None:
                    entry[api] = None
                    continue
                rows = (o["payload"].get("response_body") or {}).get(key) or []
                entry[api] = {"observed_at": o["observed_at"], "rows": len(rows),
                              "sold_qty": sum(to_int(r.get("cntr_qty")) for r in rows),
                              "realized": sum(to_int(r.get("tdy_sel_pl")) for r in rows),
                              "commission": sum(to_int(r.get("tdy_trde_cmsn")) for r in rows),
                              "tax": sum(to_int(r.get("tdy_trde_tax")) for r in rows)}
            lnet = (ledger_by_sym.get(sym) or {}).get("net")
            lcomm = (ledger_by_sym.get(sym) or {}).get("commission")
            b72, b73 = entry.get("ka10072"), entry.get("ka10073")
            if b72 and b73:
                spread = OrderedDict()
                for key in ("realized", "commission", "tax"):
                    if b72[key] != b73[key]:
                        spread[key] = {"ka10072": b72[key], "ka10073": b73[key], "diff": b72[key] - b73[key]}
                if spread:
                    entry["broker_report_spread"] = spread
                    out["notes"].append("%s: 증권사 보고 간 불일치 %s (모의서버 절사 차이로 알려짐)"
                                        % (sym, json.dumps(spread, ensure_ascii=False)))
            # When ka10072 and ka10073 disagree, the broker's own range (widened by the tolerance)
            # is the acceptance band; a single report is compared directly.
            present = [(api, entry[api]) for api in ("ka10072", "ka10073") if entry.get(api)]
            for key, lval, tol, kind, label in (("realized", lnet, self.tolerance, "symbol_pnl_mismatch", "원장 순손익"),
                                               ("commission", lcomm, self.commission_tolerance, "symbol_commission_mismatch", "원장 추정 수수료")):
                if lval is None or not present:
                    continue
                values = OrderedDict((api, b[key]) for api, b in present)
                lo, hi = min(values.values()), max(values.values())
                if lval < lo - tol or lval > hi + tol:
                    self.add(kind, UNRECONCILED, "%s %d vs %s (허용 %d원 초과)"
                             % (label, lval, " / ".join("%s %d" % kv for kv in values.items()), tol), sym, None)
            if lnet is None and (entry.get("ka10072") or entry.get("ka10073")):
                self.add("symbol_realized_without_ledger_trip", UNRECONCILED, "증권사 종목별 실현손익이 있으나 원장 왕복이 없음", sym, None)
            per_symbol.append(entry)
        out["per_symbol"] = per_symbol
        ledger_rows = []
        if self.has("profits"):
            for r in self.con.execute("SELECT symbol, net, basis, reason FROM profits WHERE scope=? AND day=? AND mode=? LIMIT ?",
                                      (self.scope, self.day, self.mode, self.limit)):
                ledger_rows.append(dict(r))
        out["ledger_profits_table"] = ledger_rows
        if self.has("summaries"):
            r = self.con.execute("SELECT payload FROM summaries WHERE scope=? AND day=? AND mode=? LIMIT 1",
                                 (self.scope, self.day, self.mode)).fetchone()
            if r is not None:
                try:
                    p = json.loads(r["payload"])
                    out["ledger_summary"] = {k: p.get(k) for k in ("status", "basis", "buy", "sell", "net", "rate")}
                except (TypeError, ValueError):
                    out["ledger_summary"] = None
        return out

    # ---- run
    def run(self) -> Dict[str, Any]:
        report: Dict[str, Any] = OrderedDict()
        self.section("ledger", self.load_ledger)
        report["counts"] = self.section("counts", self.counts)
        report["ledger_round_trips"] = self.section("round_trips", self.round_trips_section)
        broker: Dict[str, Any] = OrderedDict()
        broker["kt00009_orders"] = self.section("kt00009", self.kt00009_section)
        broker["ka10075_pending"] = self.section("ka10075", self.ka10075_section)
        broker["kt00018_holdings"] = self.section("holdings", self.holdings_section)
        report["broker_comparison"] = broker
        report["ttl_cancellations"] = self.section("ttl", self.ttl_section)
        report["realized_pnl"] = self.section("pnl", self.pnl_section)
        report["discrepancies"] = self.discrepancies
        report["section_errors"] = self.section_errors
        return report


# ----------------------------------------------------------------------------- output
def render_table(report: Dict[str, Any]) -> str:
    lines: List[str] = []
    meta = report.get("meta", {})
    lines.append("원장 대조 %s scope=%s mode=%s  (db=%s)" % (meta.get("day"), meta.get("scope"), meta.get("mode"), meta.get("db")))
    c = report.get("counts") or {}
    lines.append("intents=%s orders=%s" % (json.dumps(c.get("intents"), ensure_ascii=False), json.dumps(c.get("orders"), ensure_ascii=False)))
    lines.append("fills=%s" % json.dumps(c.get("fills"), ensure_ascii=False))
    lines.append("ttl=%s" % json.dumps(c.get("ttl"), ensure_ascii=False))
    rt = report.get("ledger_round_trips") or {}
    lines.append("")
    lines.append("== 왕복 (%s)" % json.dumps(rt.get("totals"), ensure_ascii=False))
    lines.append("symbol  strategy   qty  buy->sell        gross   comm   tax    net")
    for t in rt.get("trips") or []:
        lines.append("%-7s %-10s %4d %8s->%-8s %7d %6d %5d %7d" % (
            t["symbol"], t["strategy"], t["quantity"], t["buy_avg_price"], t["sell_price"],
            t["gross_pnl_krw"], t["commission_est_krw"], t["tax_est_krw"], t["net_pnl_krw"]))
    for p in rt.get("open_positions") or []:
        lines.append("open   %-7s %-10s %4d @%d" % (p["symbol"], p["strategy"], p["quantity"], p["price"]))
    pnl = report.get("realized_pnl") or {}
    exempt = (pnl.get("ledger") or {}).get("tax_exempt_symbols") or {}
    if exempt:
        lines.append("거래세 면제(ETF/ETN): %s" % ", ".join("%s(%s)" % kv for kv in exempt.items()))
    lines.append("")
    lines.append("== 손익: ledger=%s" % json.dumps({k: v for k, v in (pnl.get("ledger") or {}).items() if k != "params"}, ensure_ascii=False))
    lines.append("   ka10074=%s" % json.dumps(pnl.get("ka10074"), ensure_ascii=False))
    lines.append("   delta=%s" % json.dumps(pnl.get("delta_ledger_minus_broker"), ensure_ascii=False))
    lines.append("   params=%s" % json.dumps((pnl.get("ledger") or {}).get("params"), ensure_ascii=False))
    for n in pnl.get("notes") or []:
        lines.append("   note: %s" % n)
    ttl = report.get("ttl_cancellations") or {}
    lines.append("")
    lines.append("== TTL 취소: attempts=%s by_settlement=%s" % (ttl.get("attempts"), json.dumps(ttl.get("by_settlement"), ensure_ascii=False)))
    for i in ttl.get("items") or []:
        lines.append("  %s %s %s -> %s (cancel %s, rest cnfm %s, principal_released=%s)" % (
            i["original_order_number"], i["symbol"], i["outcome_status"], i["settlement"], i["cancellation_order_number"],
            i["broker_rest_kt00009"]["cancel_confirmed_qty"], i["principal_reservation_released_by_ack"]))
    lines.append("")
    lines.append("== 불일치 %d건" % len(report.get("discrepancies") or []))
    for d in report.get("discrepancies") or []:
        lines.append("  [%s] %s %s %s: %s" % (d["label"], d["kind"], d.get("symbol") or "-", d.get("order_number") or "-", d["detail"]))
    if report.get("section_errors"):
        lines.append("section_errors=%s" % json.dumps(report["section_errors"], ensure_ascii=False))
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Reconcile one day of the Kiwoom paper ledger (a COPY, opened read-only) against the broker "
                    "observations stored in it. Never writes, never orders.")
    p.add_argument("--db", required=True, help="path to a COPY of .execution-ledger.sqlite3 (opened with mode=ro)")
    p.add_argument("--day", default=None, help="trading day YYYYMMDD (default: today in KST)")
    p.add_argument("--scope", default="default")
    p.add_argument("--mode", default="paper")
    p.add_argument("--commission-bps", type=float, default=35.0,
                   help="commission in bps of turnover; default 35 = Kiwoom mock account observed 2026-11-03 (6,360 KRW on 1,819,730 KRW)")
    p.add_argument("--tax-bps", type=float, default=20.0,
                   help="securities transaction tax in bps of sell amount; default 20 = observed 1,653 KRW on 828,460 KRW sells (확인 필요)")
    p.add_argument("--pnl-tolerance-krw", type=int, default=100,
                   help="abs KRW difference tolerated for tax/realized comparisons before labelling UNRECONCILED (default 100)")
    p.add_argument("--commission-tolerance-krw", type=int, default=50,
                   help="abs KRW difference tolerated for commission comparisons (default 50; the mock server's APIs "
                        "disagree by ~10 KRW per trade)")
    p.add_argument("--etf-symbols", default="",
                   help="comma-separated symbols treated as listed ETF/ETN (no transaction tax) in addition to the name heuristic")
    p.add_argument("--no-etf-heuristic", action="store_true",
                   help="do not infer ETF/ETN from the instrument name (ETF_NAME_PREFIXES, 'ETF'/'ETN' in name)")
    p.add_argument("--json", action="store_true", help="JSON output (default)")
    p.add_argument("--format", choices=("json", "table"), default="json")
    p.add_argument("--limit", type=int, default=500, help="row cap per query (default 500)")
    p.add_argument("--allow-live-readonly", action="store_true",
                   help="allow opening the live ledger path read-only (normally refused; copy it first)")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    day = args.day or _dt.datetime.now(tz=KST).strftime("%Y%m%d")
    if not re.fullmatch(r"\d{8}", day):
        log("error: --day must be YYYYMMDD")
        return 3
    if args.limit <= 0:
        log("error: --limit must be positive")
        return 3
    fmt = "table" if args.format == "table" and not args.json else "json"
    try:
        refuse_live_path(args.db, args.allow_live_readonly)
        con = open_readonly(args.db)
    except SystemExit as exc:
        log("error: %s" % exc)
        return 3
    except sqlite3.Error as exc:
        log("error: cannot open database: %s" % exc)
        return 3
    try:
        st = os.stat(args.db)
        meta = OrderedDict([
            ("day", day), ("scope", args.scope), ("mode", args.mode), ("db", os.path.abspath(args.db)),
            ("db_size_bytes", st.st_size),
            ("db_mtime", _dt.datetime.fromtimestamp(st.st_mtime, tz=KST).isoformat()),
            ("generated_at", _dt.datetime.now(tz=KST).isoformat()),
            ("opened", "mode=ro,query_only"),
        ])
        etf_symbols = {norm_sym(x) for x in args.etf_symbols.split(",") if x.strip()}
        rec = Reconciler(con, day, args.scope, args.mode, args.commission_bps, args.tax_bps, args.limit, args.pnl_tolerance_krw,
                         commission_tolerance=args.commission_tolerance_krw, etf_symbols=etf_symbols,
                         etf_heuristic=not args.no_etf_heuristic)
        report = rec.run()
    except sqlite3.DatabaseError as exc:
        log("error: database unreadable: %s" % exc)
        return 2
    finally:
        try:
            con.close()
        except sqlite3.Error:
            pass
    full: Dict[str, Any] = OrderedDict()
    full["meta"] = meta
    full.update(report)
    counts = report.get("counts") or {}
    obs_counts = counts.get("broker_observations") or {}
    cannot = False
    reasons: List[str] = []
    if not obs_counts:
        cannot = True
        reasons.append("no broker observations for day %s" % day)
    if "kt00009" not in obs_counts:
        cannot = True
        reasons.append("no kt00009 observations for day %s" % day)
    core_failed = [e for e in (report.get("section_errors") or []) if e["section"] in ("ledger", "counts", "kt00009")]
    if core_failed:
        cannot = True
        reasons.extend("%s: %s" % (e["section"], e["error"]) for e in core_failed)
    labels = defaultdict(int)
    for d in report.get("discrepancies") or []:
        labels[d["label"]] += 1
    full["summary"] = OrderedDict([
        ("discrepancy_count", len(report.get("discrepancies") or [])),
        ("by_label", dict(labels)),
        ("by_kind", dict(sorted(((k, sum(1 for d in report["discrepancies"] if d["kind"] == k))
                                 for k in {d["kind"] for d in (report.get("discrepancies") or [])})))),
        ("cannot_reconcile", cannot), ("cannot_reconcile_reasons", reasons),
    ])
    full = mask_accounts(full)
    if fmt == "json":
        print(json.dumps(full, ensure_ascii=False, indent=2, default=str))
    else:
        print(render_table(full))
    if cannot:
        return 2
    return 1 if full["summary"]["discrepancy_count"] else 0


if __name__ == "__main__":
    sys.exit(main())
