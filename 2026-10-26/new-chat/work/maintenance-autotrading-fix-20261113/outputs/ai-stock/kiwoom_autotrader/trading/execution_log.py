"""Actual automatic-order fills, a 10-second holdings reconciliation and CSV log.

Only registered accepted order numbers can acquire a strategy label. kt00009
execution numbers identify partial fills; holdings changes never create fills.
Repeated REST keys may be cumulative snapshots: their changes are immutable
observations, never invented individual fills. `fills` is only a projection.
CSV remaining quantity is the first holdings observation AFTER discovery, not an
invented position at the exact fill instant. One configured account per mode is
assumed; changing accounts requires a separate ledger directory. The scope is
an additional local database label, not proof of broker account identity.
"""
from __future__ import annotations

from collections import deque
from bisect import bisect_right
from contextlib import contextmanager, nullcontext
import csv
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_FLOOR, localcontext
from functools import wraps
from inspect import signature
import hashlib
import io
import json
from pathlib import Path
import re
import sqlite3
import tempfile
import threading
from time import monotonic, sleep
import uuid

from kiwoom_autotrader.api.account_report import fetch_account_report, CASH_FIELDS, TOTAL_FIELDS, HOLDING_INTEGER_FIELDS
from kiwoom_autotrader.api.executions import Execution, RealizedProfit, ExecutionsError, fetch_executions, fetch_realized_profit, fetch_period_realized_profit, fetch_daily_realized_profit, order_key, BROKER_OBSERVATION_SCHEMAS, _profit_rows
from kiwoom_autotrader.api.minute_chart import MinuteChartError, session_identity
from kiwoom_autotrader.api.orders import OrderReceipt
from kiwoom_autotrader.api.session import KST
from kiwoom_autotrader.config.settings import (load_settings, effective_paper_scan, effective_theme_cap,
                                              effective_theme_unclassified_policy, effective_paper_experiment)
from kiwoom_autotrader.trading.decision_evidence import freeze_evidence, canonical_json, UNSPECIFIED_PRICE
from kiwoom_autotrader.trading.cutoff_reconciliation import classify_cost_cutoff
from kiwoom_autotrader.trading.profit_decomposition import decompose_profit
from .broker_observation_repeats import create_repeat_schema, install_evidence_view, persist_observation
from .tracker_diagnostics import TrackerDiagnostics
from kiwoom_autotrader.realtime.execution_stream import valid_gap_values
from kiwoom_autotrader.utils.platform_io import os, sync_directory


EXPERIMENT_STRATEGIES = frozenset({'EXP_R','EXP_A','EXP_B','EXP_B_TP','EXP_E','EXP_C','EXP_D',
    'EXP_ORB_A','EXP_ORB_B','EXP_GDR','EXP_VSS','EXP_LDR','EXP_ETF_B'})
OVERNIGHT_EXPERIMENT_STRATEGIES = frozenset({'EXP_D','EXP_GDR','EXP_LDR','EXP_VSS'})
_ERROR_NOTICE_COOLDOWN_SECONDS = 600
_ERROR_NOTICE_CACHE_LIMIT = 64
_SQLITE_TIMEOUT_SECONDS = 30.0
_OBSERVATION_BUSY_BACKOFFS = (0.05, 0.15, 0.35)


def _is_sqlite_busy_error(exc: BaseException) -> bool:
    if not isinstance(exc, sqlite3.OperationalError):
        return False
    code = getattr(exc, "sqlite_errorcode", None)
    if code in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
        return True
    text = str(exc).lower()
    return "database is locked" in text or "database table is locked" in text or "busy" in text


def _run_with_busy_retry(fn):
    for attempt in range(len(_OBSERVATION_BUSY_BACKOFFS) + 1):
        try:
            return fn()
        except sqlite3.OperationalError as exc:
            if not _is_sqlite_busy_error(exc) or attempt >= len(_OBSERVATION_BUSY_BACKOFFS):
                raise
            sleep(_OBSERVATION_BUSY_BACKOFFS[attempt])

EXPERIMENT_ETF_CODES = frozenset({'069500','229200'})
STAGED_STRATEGIES = EXPERIMENT_STRATEGIES | frozenset({'S2_surge_high_breakout','S3_upper_limit_continuation','S4_theme_and_catalyst_leader','S5_steady_uptrend_vwap'})

FIELDS = ["체결시간", "종목명", "매수매도", "가격", "수량", "전략", "남은수량",
          "투자구분", "종목코드", "주문번호", "체결번호", "잔고조회시각", "잔고상태",
          "행구분", "매수합계", "매도합계", "순이익", "수익률", "수익률분모", "비고"]


def _diagnosed(stage):
    """Observe duration only; the diagnostics adapter cannot authorize work."""
    def decorate(function):
        positions = {name: index - 1 for index, name in enumerate(signature(function).parameters)
                     if name in ('day', 'mode') and index > 0}
        @wraps(function)
        def wrapped(self, *args, **kwargs):
            context = {name: kwargs.get(name, args[index] if index < len(args) else None)
                       for name, index in positions.items()}
            with self._diagnostics.stage(stage, work_day=context.get('day'), mode=context.get('mode')):
                return function(self, *args, **kwargs)
        return wrapped
    return decorate


class ExecutionLogError(RuntimeError):
    pass


class _HistoryLanePaused(ExecutionLogError):
    """The history worker lost its session, clock window or run permission."""


class DailyEntryLimitReached(ExecutionLogError):
    """Known pre-send daily automatic-entry rejection."""


class DailyEntryLimitUnavailable(ExecutionLogError):
    """Daily limit cannot be established from the local order ledger."""


class ConcurrentPositionLimitReached(ExecutionLogError):
    """No remaining account-wide paper symbol slot; no order was sent."""
    skipped_reason = "concurrent_position_limit_reached"


class ConcurrentPositionLimitUnavailable(ExecutionLogError):
    """A fresh complete account observation is required to count positions."""
    skipped_reason = "concurrent_position_limit_unavailable"


class PaperCashLimitReached(ExecutionLogError):
    """Deposit/orderable funds cannot cover principal plus assumed cost reserve."""
    skipped_reason = "insufficient_cash"


class ThemeConcentrationLimitReached(ExecutionLogError):
    skipped_reason = "theme_concentration_limit_reached"


class ThemeConcentrationUnavailable(ExecutionLogError):
    skipped_reason = "theme_concentration_unavailable"


class _ImmutableJSONDict(dict):
    """A JSON-serializable, read-only tree shared by observation snapshots."""
    def _readonly(self, *args, **kwargs):
        raise TypeError("broker observation snapshots are immutable")

    __setitem__ = __delitem__ = clear = pop = popitem = setdefault = update = __ior__ = _readonly


class _ImmutableJSONList(list):
    def _readonly(self, *args, **kwargs):
        raise TypeError("broker observation snapshots are immutable")

    __setitem__ = __delitem__ = append = clear = extend = insert = pop = remove = reverse = sort = __iadd__ = __imul__ = _readonly


def _immutable_json(value):
    if isinstance(value, dict):
        return _ImmutableJSONDict((key, _immutable_json(item)) for key, item in value.items())
    if isinstance(value, list):
        return _ImmutableJSONList(_immutable_json(item) for item in value)
    return value


@dataclass(frozen=True, slots=True)
class _BrokerObservation:
    id: int
    digest: str
    payload_json: str
    mode: str
    source_api: str
    event: dict

    def __getitem__(self, key):
        return getattr(self, key)


def _observation_event(row):
    return row.event if isinstance(row, _BrokerObservation) else json.loads(row["payload_json"])


def _number(value: Decimal | int | str) -> str:
    result = format(Decimal(value), "f")
    if "." in result:
        result = result.rstrip("0").rstrip(".")
    return result or "0"


def _safe_csv(value) -> str:
    text = str(value)
    return "'" + text if text.startswith(("=", "+", "-", "@", "\t", "\r")) else text


def _now():
    return datetime.now(KST)


def _paper_balance_snapshot(report):
    """Copy only validated display fields from a complete paper account report.

    The two TRs are sequential observations, not an atomic balance. Failure
    to validate the optional journal projection must not change trade logic.
    """
    if (not isinstance(report, dict) or report.get("mode") != "paper"
            or report.get("exchange") != "KRX" or report.get("snapshot_changed") is not False
            or not isinstance(report.get("holdings"), list) or len(report["holdings"]) > 2000):
        return None
    pages = report.get("pages")
    if (not isinstance(pages, dict) or any(type(pages.get(key)) is not int or not 1 <= pages[key] <= 20
                                           for key in ("kt00001", "kt00018"))):
        return None

    def numeric(value, percent=False):
        if value is None:
            return None
        pattern = r"[+-]?[0-9]{1,15}(?:\.[0-9]{1,2})?" if percent else r"[+-]?[0-9]{1,15}"
        if not isinstance(value, str) or re.fullmatch(pattern, value) is None:
            raise ValueError
        return value

    try:
        cash, totals = report.get("cash"), report.get("totals")
        if not isinstance(cash, dict) or not isinstance(totals, dict):
            return None
        cash = {key: numeric(cash.get(key)) for key in CASH_FIELDS}
        totals = {key: numeric(totals.get(key), key == "tot_prft_rt") for key in TOTAL_FIELDS}
        holdings = []
        for row in report["holdings"]:
            if not isinstance(row, dict):
                return None
            code, name, credit, loan_date = (row.get(key) for key in ("stk_cd", "stk_nm", "crd_tp", "crd_loan_dt"))
            if not isinstance(code, str) or not re.fullmatch(r"[AJQ]?[0-9]{6}", code):
                return None
            if name is not None and (not isinstance(name, str) or len(name) > 40 or not name.isprintable()
                                     or re.search(r"[0-9]{8,}|[A-Za-z0-9_-]{24,}", name)):
                return None
            if credit is not None and (not isinstance(credit, str) or not re.fullmatch(r"[0-9]{2}", credit)):
                return None
            if loan_date is not None and (not isinstance(loan_date, str) or not re.fullmatch(r"[0-9]{8}", loan_date)):
                return None
            clean = {key: numeric(row.get(key)) for key in HOLDING_INTEGER_FIELDS}
            clean.update(stk_cd=code, stk_nm=name, crd_tp=credit, crd_loan_dt=loan_date,
                         prft_rt=numeric(row.get("prft_rt"), True))
            holdings.append(clean)
        return {"schema": "paper-account-snapshot-v1", "mode": "paper", "exchange": "KRX",
                "cash": cash, "totals": totals, "holdings": holdings,
                "pages": {key: pages[key] for key in ("kt00001", "kt00018")}}
    except (ValueError, TypeError):
        return None


def _observed_number(value, *, signed=False, integer=False):
    """Missing/invalid broker numbers stay missing, never zero or floats."""
    pattern = r"[+-]?[0-9]{1,24}(?:\.[0-9]{1,12})?" if signed else r"\+?[0-9]{1,24}(?:\.[0-9]{1,12})?"
    if not isinstance(value, str) or re.fullmatch(pattern, value) is None:
        return None
    number = Decimal(value)
    return None if integer and number != number.to_integral_value() else number


def _successful_return_code(value):
    # Bool must not be accepted merely because False compares equal to 0.
    return (type(value) is int and value == 0) or (type(value) is str and value == "0")


def _complete_observation_pages(events):
    if not events or [event.get("page") for event in events] != list(range(1, len(events) + 1)):
        return False
    if len({event.get("query_id") for event in events}) != 1:
        return False
    for index, event in enumerate(events):
        cont = event.get("continuation", {})
        if event.get("status") != "success" or not _successful_return_code(event.get("response_body", {}).get("return_code")):
            return False
        if cont.get("request_cont_yn") not in ((None, "", "N") if index == 0 else ("Y",)):
            return False
        if index == len(events) - 1:
            if cont.get("response_cont_yn") not in (None, "", "N") or cont.get("next_key_present") is not False:
                return False
        elif cont.get("response_cont_yn") != "Y" or cont.get("next_key_present") is not True:
            return False
    return True


def _post_sell_report_state(day, mode, symbol, observations, *, as_of=None):
    """Bind the latest two cost queries to actual same-day 00 sell units.

    Failed, partial or old latest attempts never fall back to earlier queries.
    Return the selected 72 rows separately so callers can bind their typed
    calculation cache to this exact response without persisting duplicate rows.
    """
    as_of = _now() if as_of is None else as_of
    if not isinstance(as_of, datetime) or as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ExecutionLogError("자동매매 원장의 한국 시간 시계를 확인하지 못했습니다.")
    state = {"status": "post_sell_report_pending", "latest_sell_observed_at": None,
             "observed_sell_quantity": None, "observed_sell_amount": None,
             "reports": {}, "reason_codes": []}
    parsed, units, reasons, sell_times = [], {}, set(), []
    for record in observations:
        try:
            event = _observation_event(record)
            if event.get("mode") != mode or event.get("day") != day:
                continue
            parsed.append(event)
            values = event.get("values", {})
            if (event.get("source_api") != "00" or event.get("event_kind") != "execution_stream"
                    or values.get("9001", "").removeprefix("A") != symbol
                    or values.get("907") != "1" or values.get("913") != "체결"):
                continue
            moment = datetime.fromisoformat(event["observed_at"])
            if moment.tzinfo is None or moment > as_of or moment.astimezone(KST).strftime("%Y%m%d") != day:
                raise ValueError
            sell_times.append(moment)
            order, unit = values.get("9203", ""), values.get("909", "")
            price, quantity = _observed_number(values.get("914")), _observed_number(values.get("915"), integer=True)
            if (re.fullmatch(r"[0-9]{1,20}", order) is None or int(order) <= 0
                    or re.fullmatch(r"[0-9]{1,20}", unit) is None or int(unit) <= 0
                    or price is None or price <= 0 or quantity is None or quantity <= 0):
                reasons.add("sell_unit_fields_incomplete")
                continue
            key, current = (str(int(order)), str(int(unit))), (price, quantity)
            if key in units and units[key] != current:
                reasons.add("sell_unit_conflict")
            else:
                units[key] = current
        except (ValueError, TypeError, KeyError, AttributeError, json.JSONDecodeError):
            reasons.add("source_observation_invalid")
    latest = max(sell_times) if sell_times else None
    if latest is None:
        state["status"] = "no_observed_sell"
        reasons.add("no_valid_sell_observation")
    state["latest_sell_observed_at"] = latest.isoformat() if latest else None
    if units and not reasons:
        state["observed_sell_quantity"] = int(sum((unit[1] for unit in units.values()), Decimal(0)))
        state["observed_sell_amount"] = _number(sum((price * qty for price, qty in units.values()), Decimal(0)))
    selected72 = None
    for api in ("ka10072", "ka10073"):
        candidates = [event for event in parsed if event.get("source_api") == api
                      and event.get("request_body", {}).get("stk_cd", "").removeprefix("A") == symbol]
        report = {"query_id": None, "request_started_at": None, "observed_at": None,
                  "complete_query": False, "current_for_latest_sell": False,
                  "quantity": None, "sell_amount": None, "reason_codes": []}
        problems = report["reason_codes"]
        state["reports"][api] = report
        if not candidates:
            problems.append("query_missing")
            continue
        query = candidates[-1].get("query_id")
        report["query_id"] = query
        pages = [event for event in candidates if event.get("query_id") == query]
        try:
            report["complete_query"] = (isinstance(query, str) and re.fullmatch(r"[0-9a-f]{32}", query) is not None
                                         and _complete_observation_pages(pages))
            if not report["complete_query"]:
                problems.append("query_incomplete_or_failed")
            expected_request = {"stk_cd": symbol, "strt_dt": day}
            if api == "ka10073":
                expected_request["end_dt"] = day
            rows, requests, responses = [], [], []
            for page in pages:
                request = page.get("request_body", {})
                if (page.get("schema") != "kiwoom-broker-page-v1" or type(page.get("http_status")) is not int
                        or page["http_status"] != 200 or request != expected_request):
                    problems.append("request_scope_or_transport_unconfirmed")
                started, observed = datetime.fromisoformat(page["request_started_at"]), datetime.fromisoformat(page["observed_at"])
                if (started.tzinfo is None or observed.tzinfo is None or started > observed or observed > as_of):
                    raise ValueError
                requests.append(started); responses.append(observed)
                if latest is None or started < latest:
                    problems.append("query_started_before_latest_sell")
                body_rows = page.get("response_body", {}).get(BROKER_OBSERVATION_SCHEMAS[api]["row_field"])
                if not isinstance(body_rows, list):
                    raise ValueError
                rows.extend(body_rows)
            report["request_started_at"] = min(requests).isoformat()
            report["observed_at"] = max(responses).isoformat()
            total_qty, total_sell = Decimal(0), Decimal(0)
            if not rows:
                problems.append("cost_rows_missing")
            for row in rows:
                if (not isinstance(row, dict) or row.get("stk_cd", "").removeprefix("A") != symbol
                        or row.get("crd_tp") != "현금잔고" or api == "ka10073" and row.get("dt") != day):
                    raise ValueError
                fields = {name: _observed_number(row.get(field), signed=name == "net", integer=name == "qty")
                          for name, field in {"qty": "cntr_qty", "buy": "buy_uv", "sell": "cntr_pric",
                                              "fee": "tdy_trde_cmsn", "tax": "tdy_trde_tax", "net": "tdy_sel_pl"}.items()}
                if (any(value is None for value in fields.values()) or fields["qty"] <= 0
                        or fields["buy"] <= 0 or fields["sell"] <= 0):
                    raise ValueError
                total_qty += fields["qty"]; total_sell += fields["sell"] * fields["qty"]
            report["quantity"], report["sell_amount"] = int(total_qty), _number(total_sell)
            if total_qty != state["observed_sell_quantity"]:
                problems.append("sell_quantity_not_reconciled")
            # Freshness and full quantity establish this gate only. Existing
            # money reconciliation below handles reported average rounding,
            # sub-won basis and conflicting costs without calling stale data
            # current or manufacturing a confirmed profit.
            report["current_for_latest_sell"] = not problems
            if api == "ka10072" and report["current_for_latest_sell"]:
                selected72 = rows
        except (ValueError, TypeError, KeyError, AttributeError):
            problems.append("cost_fields_or_clock_invalid")
        report["reason_codes"] = sorted(set(problems))
    if (latest is not None and not reasons
            and all(report["current_for_latest_sell"] for report in state["reports"].values())):
        state["status"] = "reports_current"
    state["reason_codes"] = sorted(reasons | {api + ":" + code for api, report in state["reports"].items()
                                               for code in report["reason_codes"]})
    return state, selected72


def _verified_first_fill_timing(day, detail, created_at, now):
    """First actual 00 unit's broker clock, separately from receipt/ACK clocks.

    Call only for completely quantity-reconciled unit evidence. Partial fills
    acquire ownership only after the remaining order is filled or its cancel
    settlement is confirmed by the existing ledger checks.
    """
    try:
        if (detail.get('verified_amount_source')!='00_unit_sum_reconciled_to_cumulative_quantity'
                or detail.get('unit_coverage')!='quantity_reconciled' or detail.get('issues')
                or detail.get('stream_gap_observed') or not detail.get('unit_executions')):
            return None
        created=datetime.fromisoformat(created_at)
        if created.tzinfo is None or created.utcoffset() is None:
            return None
        rows=[]
        for unit in detail['unit_executions']:
            reported=unit['broker_time']
            if re.fullmatch(r'(?:[01][0-9]|2[0-3])[0-5][0-9][0-5][0-9]',reported or '') is None:
                return None
            executed=datetime.strptime(day+reported,'%Y%m%d%H%M%S').replace(tzinfo=KST)
            observations=[datetime.fromisoformat(x['observed_at']) for x in unit['reported_cost_observations']]
            if (not observations or any(x.tzinfo is None or x.utcoffset() is None for x in observations)):
                return None
            observed=min(observations)
            # The provider's unit clock has second resolution. Do not reject
            # a legitimate fill inside the order-created fractional second.
            if not created.replace(microsecond=0)<=executed<=observed<=now:
                return None
            rows.append((executed,observed,unit['execution_id']))
        if any(right[0]<left[0] for left,right in zip(rows,rows[1:])):
            return None
        first=rows[0]
        return {'first_fill_at':first[0].isoformat(),
                'first_fill_observed_at':first[1].isoformat(),
                'first_fill_execution_id':first[2],
                'first_fill_timing_verified':True,
                'first_fill_time_source':'00_verified_first_unit_broker_time'}
    except (TypeError,ValueError,KeyError,OverflowError):
        return None


def _execution_accounting(day, orders, fills, observations):
    """Pure derived view: WS units and amounts never replace REST projections.

    The 00 specification does not guarantee the aggregation scope of 938/939.
    Retain each report without treating the latest value or its sum as an order
    total. Profit API rows never acquire invented unit IDs or times.
    Source/query IDs point back to the append-only broker observation journal.
    """
    document = {"schema": "execution-accounting-v3", "day": day, "orders": [], "cost_reports": [],
        "comparisons": [], "verified_totals": {}, "final_realized_net": None, "final_net_status": "unconfirmed",
        "notes": ["REST price × cumulative quantity is a reference estimate, not verified consideration.",
                  "00 fields 938/939 have unverified aggregation scope: preserve each report; neither sum them nor treat the latest as an order total.",
                  "ka10074 daily buys can include still-held positions: sell minus buy minus reported costs is cash flow, not verified realized profit.",
                  "Profit breakdown rows are not unit executions; account and selected-symbol scopes can differ."]}
    parsed = [(row["id"], _observation_event(row)) for row in observations]
    gaps = [(identifier, event) for identifier, event in parsed if event.get("source_api") == "00"
            and event.get("event_kind") in ("stream_gap", "stream_stopped")]
    events_by_order, fills_by_order = {}, {}
    for identifier, event in parsed:
        values = event.get("values", {})
        if event.get("source_api") == "00" and values.get("913") == "체결":
            events_by_order.setdefault((event.get("mode"), values.get("9203", "").lstrip("0")), []).append((identifier, event))
    for row in fills:
        fills_by_order.setdefault((row["mode"], row["ord_no"]), []).append(row)
    for order in orders:
        key = order["mode"], order["ord_no"]
        projected = fills_by_order.get(key, ())
        projected_qty = sum(row["quantity"] for row in projected)
        reference = sum((Decimal(row["price"]) * row["quantity"] for row in projected), Decimal(0))
        units, cumulative, costs, issues = {}, [], [], set()
        for identifier, event in events_by_order.get(key, ()):
            values = event.get("values", {})
            if (values.get("9001") not in (order["symbol"], "A" + order["symbol"])
                    or values.get("907") != ("1" if order["side"] == "sell" else "2")):
                issues.add("order_identity_mismatch")
                continue
            quantity = _observed_number(values.get("911"), integer=True)
            commission = _observed_number(values.get("938"))
            tax = _observed_number(values.get("939"))
            costs.append({"observation_id": identifier, "observed_at": event["observed_at"],
                "broker_time": values.get("908"), "execution_id_reported": values.get("909"),
                "cumulative_quantity_reported": values.get("911"), "unit_quantity_reported": values.get("915"),
                "values": {field: values.get(field) for field in ("938", "939")},
                "commission": None if commission is None else _number(commission),
                "tax": None if tax is None else _number(tax), "aggregation_scope": "unverified"})
            cumulative_amount = _observed_number(values.get("903"))
            if values.get("903") and (cumulative_amount is None or cumulative_amount <= 0):
                issues.add("invalid_cumulative_amount")
                cumulative_amount = None
            if quantity is not None and 0 < quantity <= order["quantity"]:
                cumulative.append({"observation_id": identifier, "observed_at": event["observed_at"],
                    "quantity": int(quantity), "amount": cumulative_amount})
            else:
                issues.add("invalid_cumulative_quantity")
            unit_id, time_value = values.get("909", ""), values.get("908", "")
            price = _observed_number(values.get("914"))
            size = _observed_number(values.get("915"), integer=True)
            if size is not None and size > order["quantity"]:
                issues.add("unit_quantity_exceeds_order_quantity")
            if size is not None and quantity is not None and size > quantity:
                issues.add("unit_quantity_exceeds_own_cumulative_quantity")
            if price is not None and size is not None and cumulative_amount is not None and price * size > cumulative_amount:
                issues.add("unit_amount_exceeds_own_cumulative_amount")
            if (re.fullmatch(r"[0-9]{1,20}", unit_id) is None or int(unit_id) <= 0 or price is None or price <= 0
                    or size is None or size <= 0 or size > order["quantity"]
                    or re.fullmatch(r"(?:[01][0-9]|2[0-3])[0-5][0-9][0-5][0-9]", time_value) is None):
                issues.add("missing_or_invalid_unit_fields")
                continue
            unit_id = str(int(unit_id))
            unit = {"execution_id": unit_id, "broker_time": time_value, "price": _number(price),
                    "quantity": int(size), "amount": _number(price * size), "cumulative_quantity": None if quantity is None else int(quantity)}
            if unit_id in units:
                old = units[unit_id]
                if any(old[field] != unit[field] for field in unit):
                    issues.add("conflicting_unit_id")
                    old["conflicting_observation_ids"].append(identifier)
                old["observation_ids"].append(identifier)
            else:
                units[unit_id] = {**unit, "observation_ids": [identifier], "conflicting_observation_ids": []}
        unit_rows = sorted(units.values(), key=lambda row: (row["cumulative_quantity"] or 0, int(row["execution_id"])))
        for row in unit_rows:
            row["reported_cost_observations"] = [cost for cost in costs if cost["observation_id"] in row["observation_ids"]]
        latest = max(cumulative, key=lambda row: (row["quantity"], row["observation_id"])) if cumulative else None
        if cumulative and cumulative[-1]["quantity"] < latest["quantity"]:
            issues.add("out_of_order_cumulative_quantity")
        total_qty, total_amount, sequence_complete = 0, Decimal(0), bool(unit_rows)
        for row in unit_rows:
            total_qty += row["quantity"]; total_amount += Decimal(row["amount"])
            sequence_complete = sequence_complete and row["cumulative_quantity"] == total_qty
        if total_qty > order["quantity"]:
            issues.add("observed_unit_quantity_exceeds_order_quantity")
        if latest and total_qty > latest["quantity"]:
            issues.add("observed_unit_quantity_exceeds_cumulative_quantity")
        if latest and latest["amount"] is not None and total_amount > latest["amount"]:
            issues.add("observed_unit_amount_exceeds_cumulative_amount")
        if not latest or total_qty != latest["quantity"]:
            sequence_complete = False
        if latest:
            same_quantity = [row for row in cumulative if row["quantity"] == latest["quantity"] and row["amount"] is not None]
            if len({row["amount"] for row in same_quantity}) > 1:
                issues.add("conflicting_cumulative_amount")
            if sequence_complete and latest["amount"] is not None and total_amount != latest["amount"]:
                issues.add("unit_sum_vs_cumulative_amount_mismatch")
        gap_observed = bool(latest and any(event["mode"] == key[0]
            and datetime.fromisoformat(order["registered_at"]) <= datetime.fromisoformat(event["observed_at"]) <= datetime.fromisoformat(latest["observed_at"]) for _, event in gaps))
        verified = None
        source = None
        if not issues and sequence_complete:
            verified, source = total_amount, "00_unit_sum_reconciled_to_cumulative_quantity"
        elif issues <= {"missing_or_invalid_unit_fields"} and latest and latest["amount"] is not None:
            # Aggregate consideration can be reported exactly even if unit
            # coverage is incomplete. It never creates missing partial rows.
            verified, source = latest["amount"], "00_reported_cumulative_amount_only"
        quantity = latest["quantity"] if latest else None
        # An integer REST average multiplied by quantity is not an actual
        # consideration.  Classify the difference only when complete 00 unit
        # executions, order identity and cumulative quantity are corroborated.
        # A difference within Q won is necessary, not proof that two unrelated
        # fills or broker scopes refer to the same transactions.
        rounding_diff = reference - verified if verified is not None and quantity == projected_qty else None
        integer_projection = bool(projected) and all(Decimal(row["price"]) == Decimal(row["price"]).to_integral_value() for row in projected)
        rounding_status = "unconfirmed"
        if (rounding_diff is not None and source == "00_unit_sum_reconciled_to_cumulative_quantity"
                and not gap_observed and integer_projection and abs(rounding_diff) <= quantity):
            rounding_status = "exact" if rounding_diff == 0 else "reconciled_with_unit_rounding"
        document["orders"].append({"mode": key[0], "order_number": key[1], "symbol": order["symbol"],
            "side": order["side"], "strategy": order["strategy"], "ordered_quantity": order["quantity"],
            "rest_projection_quantity": projected_qty, "rest_reference_amount": _number(reference),
            "verified_quantity": quantity if verified is not None else None,
            "verified_amount": None if verified is None else _number(verified), "verified_amount_source": source,
            "rest_reference_minus_verified": _number(reference - verified) if verified is not None and quantity == projected_qty else None,
            "avg_price_rounding_diff_krw": None if rounding_status == "unconfirmed" else _number(rounding_diff),
            "avg_price_reconciliation_status": rounding_status,
            "observed_unit_quantity": total_qty, "observed_unit_amount": _number(total_amount),
            "unit_coverage": "quantity_reconciled" if sequence_complete and not issues else "unconfirmed",
            "stream_gap_observed": gap_observed, "issues": sorted(issues), "unit_executions": unit_rows,
            "latest_cumulative": None if latest is None else {field: _number(value) if isinstance(value, Decimal) else value for field, value in latest.items()},
            "reported_cost_observations": costs, "latest_reported_cost": costs[-1] if costs else None,
            "order_total_commission": None, "order_total_tax": None, "cost_status": "aggregation_scope_unverified"})

    for mode in {row["mode"] for row in document["orders"]}:
        sides = {}
        for side in ("buy", "sell"):
            registered = [row for row in document["orders"] if row["mode"] == mode and row["side"] == side]
            verified = [row for row in registered if row["verified_amount"] is not None
                and row["verified_quantity"] == row["rest_projection_quantity"] and row["rest_projection_quantity"] > 0]
            unconfirmed = [row for row in registered if row not in verified]
            partial = [row for row in registered if 0 < row["rest_projection_quantity"] < row["ordered_quantity"]]
            pending = [row for row in registered if row["rest_projection_quantity"] < row["ordered_quantity"]]
            amount = sum((Decimal(row["verified_amount"]) for row in verified), Decimal(0))
            sides[side] = {"scope": "registered_automatic_orders_only", "verified_observed_amount": _number(amount) if verified else None,
                "verified_order_count": len(verified), "unconfirmed_order_count": len(unconfirmed),
                "partially_filled_order_count": len(partial), "pending_order_count": len(pending),
                "fully_verified_amount": _number(amount) if registered and not unconfirmed and not pending else None,
                "coverage": "all_registered_orders_fully_reconciled" if registered and not unconfirmed and not pending
                    else "no_registered_orders" if not registered else "partial_or_unconfirmed"}
        document["verified_totals"][mode] = sides

    queries, latest_queries = {}, {}
    for identifier, event in parsed:
        api = event.get("source_api")
        if api not in ("ka10072", "ka10073", "ka10074"):
            continue
        symbol = event["request_body"].get("stk_cd", "")
        key = event["mode"], api, symbol
        query = key + (event["query_id"],)
        queries.setdefault(query, []).append((identifier, event))
        latest_queries[key] = query
    for key, query in sorted(latest_queries.items()):
        pages = queries[query]
        complete = _complete_observation_pages([event for _, event in pages])
        api, symbol = key[1], key[2]
        rows = [row for _, event in pages for row in event["response_body"].get(BROKER_OBSERVATION_SCHEMAS[api]["row_field"], [])]
        report = {"mode": key[0], "source_api": api, "symbol": symbol or None, "query_id": query[3],
            "observed_at": pages[-1][1]["observed_at"], "observation_ids": [identifier for identifier, _ in pages],
            "complete_query": complete, "scope": "account_day" if api == "ka10074" else "requested_symbol_day",
            "row_kind": "profit_breakdown_not_unit_execution", "profit_rows": rows,
            "totals": {}, "arithmetic_matches": None, "final_net_confirmed": False}
        totals = report["totals"]
        if api == "ka10074":
            report["comparison_scope"] = "account_cashflow_not_realized_cost_basis"
            report["realized_comparability"] = "unverified"
            mapping = {"buy_amount": "tot_buy_amt", "sell_amount": "tot_sell_amt", "reported_net": "rlzt_pl", "commission": "trde_cmsn", "tax": "trde_tax"}
            tops = [{name: _observed_number(event["response_body"].get(field), signed=name == "reported_net")
                     for name, field in mapping.items()} for _, event in pages]
            report["page_totals_match"] = bool(tops) and all(top == tops[0] for top in tops)
            totals.update(tops[0] if tops else {})
            totals["calculated_net"] = None
        else:
            valid_scope = bool(rows) and all(row.get("crd_tp") == "현금잔고" and row.get("stk_cd", "").removeprefix("A") == symbol
                and (api != "ka10073" or row.get("dt") == day) for row in rows)
            report["cash_symbol_scope_verified"] = valid_scope
            numbers = [{name: _observed_number(row.get(field), signed=name == "reported_net", integer=name == "quantity")
                for name, field in {"quantity":"cntr_qty", "buy_price":"buy_uv", "sell_price":"cntr_pric",
                                    "reported_net":"tdy_sel_pl", "commission":"tdy_trde_cmsn", "tax":"tdy_trde_tax"}.items()} for row in rows]
            for name in ("quantity", "reported_net", "commission", "tax"):
                totals[name] = sum((row[name] for row in numbers), Decimal(0)) if valid_scope and all(row[name] is not None for row in numbers) else None
            for name, price in (("buy_amount", "buy_price"), ("sell_amount", "sell_price")):
                totals[name] = sum((row[price] * row["quantity"] for row in numbers), Decimal(0)) if valid_scope and all(row[price] is not None and row["quantity"] is not None for row in numbers) else None
        if all(totals.get(field) is not None for field in ("buy_amount", "sell_amount", "commission", "tax")):
            difference = totals["sell_amount"] - totals["buy_amount"] - totals["commission"] - totals["tax"]
            if api == "ka10074":
                # Account day purchases are not the acquisition cost of sold
                # lots. Even a flat snapshot does not establish that scope.
                totals["cashflow_after_reported_costs"] = difference
                if totals.get("reported_net") is not None:
                    totals["reported_realized_net_minus_cashflow"] = totals["reported_net"] - difference
            else:
                totals["calculated_net"] = difference
                if totals.get("reported_net") is not None:
                    report["arithmetic_matches"] = totals["calculated_net"] == totals["reported_net"]
                    totals["reported_minus_calculated_net"] = totals["reported_net"] - totals["calculated_net"]
        report["totals"] = {field: None if value is None else _number(value) for field, value in totals.items()}
        document["cost_reports"].append(report)
    for mode in {report["mode"] for report in document["cost_reports"]}:
        reports = [report for report in document["cost_reports"] if report["mode"] == mode]
        by_key = {(report["source_api"], report["symbol"]): report for report in reports}
        for report in reports:
            if report["source_api"] != "ka10072": continue
            other = by_key.get(("ka10073", report["symbol"]))
            if other:
                fields = ("quantity", "buy_amount", "sell_amount", "commission", "tax", "reported_net")
                differences = {field: {"ka10072": report["totals"].get(field), "ka10073": other["totals"].get(field)}
                               for field in fields if report["totals"].get(field) != other["totals"].get(field)}
                document["comparisons"].append({"mode": mode, "symbol": report["symbol"], "sources": ["ka10072", "ka10073"],
                    "scope": "same_requested_symbol_day_at_separate_observation_times", "differences": differences,
                    "status": "scope_or_query_unconfirmed" if not (report["complete_query"] and other["complete_query"]
                        and report.get("cash_symbol_scope_verified") and other.get("cash_symbol_scope_verified"))
                        else "different_reported_values" if differences else "matching_reported_values_not_final_confirmation"})
        account = by_key.get(("ka10074", None))
        selected = [report for report in reports if report["source_api"] == "ka10072"]
        if account and selected:
            differences = {}
            for field in ("buy_amount", "sell_amount", "commission", "tax", "reported_net"):
                values = [report["totals"].get(field) for report in selected]
                selected_total = _number(sum((Decimal(value) for value in values), Decimal(0))) if all(value is not None for value in values) else None
                if selected_total != account["totals"].get(field):
                    differences[field] = {"selected_ka10072_sum": selected_total, "ka10074": account["totals"].get(field)}
            document["comparisons"].append({"mode": mode, "sources": ["ka10072", "ka10074"], "differences": differences,
                "scope": "account_vs_selected_symbols_not_proven_equal", "status": "scope_or_values_unconfirmed"})
    document['source_profit_decomposition'] = [
        {'mode': mode, 'symbol': symbol, **decompose_profit(document, mode=mode, symbol=symbol)}
        for mode, symbol in sorted({(r['mode'], r['symbol']) for r in document['cost_reports'] if r['symbol']})
    ]
    return document


class ExecutionTracker:
    def __init__(self, *, log_dir: Path | None = None, now=None, settings_loader=None,
                 account_fetcher=None, executions_fetcher=None, profit_fetcher=None, account_scope="default",
                 daily_profit_fetcher=None, period_profit_fetcher=None, closed_day_backoff=None,
                 theme_taxonomy_path=None, closed_calendar_resolver=None):
        self.log_dir = Path(log_dir) if log_dir is not None else Path(__file__).resolve().parents[2] / "logs"
        self.path = self.log_dir / ".execution-ledger.sqlite3"
        self.now = now or _now
        self.settings_loader = settings_loader or load_settings
        self.theme_taxonomy_path = theme_taxonomy_path
        self.account_fetcher = account_fetcher or fetch_account_report
        self.executions_fetcher = executions_fetcher or fetch_executions
        self.profit_fetcher = profit_fetcher or fetch_realized_profit
        self._default_executions_fetcher = executions_fetcher is None
        self._default_profit_fetcher = profit_fetcher is None
        if closed_day_backoff is not None and type(closed_day_backoff) is not bool:
            raise ValueError("종료일 대사 대기 설정을 확인하지 못했습니다.")
        self._closed_backoff_enabled = True if closed_day_backoff is None else closed_day_backoff
        if closed_calendar_resolver is None:
            from .closed_ledger_calendar import closed_poll_plan
            closed_calendar_resolver=closed_poll_plan
        self._closed_calendar_resolver=closed_calendar_resolver
        self._closed_calendar_cache={}
        self._historical_guard_cache = None
        self._historical_guard_status = {}
        self._closed_management = {}
        self._diagnostics = TrackerDiagnostics(self.log_dir, now=self._moment)
        # Injected transports remain fully offline unless they explicitly opt
        # into this additional read. Production defaults always collect ka10074.
        self.daily_profit_fetcher = daily_profit_fetcher or (fetch_daily_realized_profit
            if executions_fetcher is None and profit_fetcher is None else None)
        self.period_profit_fetcher = period_profit_fetcher or (fetch_period_realized_profit
            if executions_fetcher is None and profit_fetcher is None else None)
        self._profit_refresh_at = {}
        self._daily_profit_refresh_at = {}
        self._sell_refresh_lock = threading.Lock()
        self._sell_refresh_pending = set()
        self._sell_refresh_seen = set()
        self._sell_refresh_seen_order = deque()
        self._execution_source_kind = "broker_observation" if executions_fetcher is None else "unknown"
        self._holdings_source_kind = "broker_observation" if account_fetcher is None else "unknown"
        self._observer_run_id = uuid.uuid4().hex
        self._broker_dedup_epoch = uuid.uuid4().hex
        self._history_lock = threading.RLock()
        self._materialize_lock = threading.RLock()
        self._observation_write_lock = threading.RLock()
        self._schema_initialized_identity = None
        self._reconciliation_lane_lock = threading.Lock()
        self._history_tracker = None
        self._defer_prior_day_sidecars = False
        self._broker_cache_lock = threading.RLock()
        self._broker_cache = {}
        # Only detached, bounded economic metadata may survive a day change.
        # Keeping the snapshot here would pin every historic day's raw JSON.
        self._closed_observation_cache = {}
        self._history_exports = {}
        self._profit_source_kind = "broker_observation" if profit_fetcher is None else "unknown"
        if not isinstance(account_scope, str) or re.fullmatch(r"[A-Za-z0-9_-]{1,80}", account_scope) is None:
            raise ValueError("계좌 원장 구분자는 영문·숫자·밑줄·하이픈으로 지정하세요.")
        self.scope = account_scope
        self._next_at = None
        self._identity = None
        self._tasks = deque()
        self._cycle_days = []
        self._snapshots = {}
        # Exact typed ka10072 rows retained only for the current session so a
        # subsequent ka10073 response can immediately revise the projection.
        self._profit_rows_cache = {}
        self._error_notice_times = {}
        self._cycle_started_at = None
        self._history_cursor = {}
        # Closed-day broker reads are bounded but not disabled. State is local
        # to this session and deliberately never persisted across restarts.
        self._closed_cache_lock = threading.RLock()
        self._closed_reconciliation = {}
        self._closed_attempts = {}
        self._closed_last_clock = None
        self._closed_last_discrepancy = {}
        self._closed_force_refresh = False
        # Balance is a time-sensitive display and reconciliation input. A
        # historical profit backlog must not hold it until the whole cycle
        # finishes. The deadline is advisory, not a freshness guarantee.
        self._balance_refresh_at = None

    def _moment(self):
        value = self.now()
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise ExecutionLogError("자동매매 원장의 한국 시간 시계를 확인하지 못했습니다.")
        return value.astimezone(KST)

    def diagnostics_snapshot(self):
        result = self._diagnostics.snapshot()
        result['historical_work_guard'] = dict(self._historical_guard_status)
        with self._closed_cache_lock:
            result['closed_day_management'] = [dict(value) for value in self._closed_management.values()]
        return result

    def set_diagnostics_callback(self, callback):
        if callback is not None and not callable(callback):
            raise TypeError('tracker diagnostics callback must be callable')
        self._diagnostics.set_progress_callback(
            None if callback is None else lambda _sample: callback(self.diagnostics_snapshot()))

    def _remember_closed_management(self, mode, day, **details):
        # Diagnostics only: never settle an order, release cash or change Net.
        key = mode, day
        with self._closed_cache_lock:
            self._closed_management.pop(key, None)
            self._closed_management[key] = {'mode': mode, 'day': day, **details,
                'financial_status': 'unconfirmed', 'reservation_released': False}
            while len(self._closed_management) > 64:
                self._closed_management.pop(next(iter(self._closed_management)))

    def _historical_work_deferred(self, now):
        """Protect current-day reconciliation; this is not market permission.

        Use a conservative union of the ordinary range and any hash-bound
        saved special session. A missing calendar never permits historic work
        during the ordinary session. Cleanup starts five minutes before open;
        closed-day work resumes ten minutes after the latest possible close.
        """
        minute = now.strftime('%Y%m%d%H%M')
        if self._historical_guard_cache is None or self._historical_guard_cache[0] != minute:
            day = now.strftime('%Y%m%d')
            start = now.replace(hour=9, minute=0, second=0, microsecond=0)
            end = now.replace(hour=15, minute=30, second=0, microsecond=0)
            plan = self._closed_poll_plan(day, now)
            supplied_start, supplied_end = plan.get('current_session_start'), plan.get('current_session_end')
            verified = (isinstance(supplied_start, datetime) and isinstance(supplied_end, datetime)
                and supplied_start.tzinfo is not None and supplied_end.tzinfo is not None
                and supplied_start.date() == supplied_end.date() == now.date()
                and supplied_start < supplied_end)
            if verified:
                start, end = min(start, supplied_start), max(end, supplied_end)
            self._historical_guard_cache = (minute, start, end,
                'verified_saved_session_conservative_union' if verified else 'conservative_unverified_session')
        _, start, end, basis = self._historical_guard_cache
        resume = end + timedelta(minutes=10)
        deferred = start - timedelta(minutes=5) <= now < resume
        window = ('opening_preparation' if now < start else 'opening_exit_09_00_to_09_05'
            if now <= start + timedelta(minutes=5) else 'closing_auction_15_20_to_15_30'
            if end - timedelta(minutes=10) <= now <= end else 'postclose_current_day_only'
            if end < now < resume else 'regular_session_current_day_only') if deferred else 'outside_protected_session'
        self._historical_guard_status = {'deferred': deferred, 'basis': basis,
            'session_start': start.isoformat(), 'session_end': end.isoformat(),
            'history_resume_at': resume.isoformat(), 'protected_window': window}
        return deferred

    @_diagnosed('closed_source_count')
    def _closed_source_state(self, db, mode, day):
        """Count physical originals and compact receipts without hydrating JSON."""
        groups, counts = [], {'00': 0, 'cost': 0}
        for table in ('broker_observations', 'broker_observation_repeats'):
            if table == 'broker_observation_repeats' and db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is None:
                continue
            for api, count, last in db.execute(f'''SELECT source_api,COUNT(*),COALESCE(MAX(id),0)
                    FROM {table} WHERE scope=? AND day=? AND mode=?
                    AND source_api IN ('00','ka10072','ka10073','ka10074') GROUP BY source_api''',
                    (self.scope, day, mode)):
                tail = db.execute(f'SELECT digest FROM {table} WHERE scope=? AND day=? AND mode=? AND id=?',
                                  (self.scope, day, mode, last)).fetchone()
                groups.append((table, api, count, last, tail[0] if tail else None))
                counts['00' if api == '00' else 'cost'] += count
        return {'identity': self._database_identity(db), 'groups': tuple(sorted(groups)), **counts}

    def _closed_limit_blocked(self, db, mode, day, *, source_state=None):
        source_state = source_state or self._closed_source_state(db, mode, day)
        reason = ('closed_unit_observation_limit' if source_state['00'] > 20000 else
                  'closed_cost_observation_limit' if source_state['cost'] > 20000 else None)
        if reason is not None:
            self._closed_observation_cache.pop((mode, day), None)
            self._remember_closed_management(mode, day, status='limit_blocked', reason=reason,
                unit_observation_rows=source_state['00'], cost_observation_rows=source_state['cost'],
                source_identity=source_state['identity'], retry='requires_offline_source_review')
            return True
        with self._closed_cache_lock:
            previous = self._closed_management.get((mode, day))
            if previous is not None and previous.get('status') == 'limit_blocked':
                self._closed_management.pop((mode, day), None)
        return False

    def _historical_sidecar_deferred(self, day):
        now = self._moment()
        if day >= now.strftime('%Y%m%d'):
            return False
        if self._defer_prior_day_sidecars or self._historical_work_deferred(now):
            return True
        with self._db() as db:
            return any(self._closed_limit_blocked(db, mode, day) for mode in ('paper', 'real'))

    def new_history_tracker(self):
        """Use one independent task/cache owner for this paper ledger's history."""
        if self._history_tracker is None:
            history = ExecutionTracker(
                log_dir=self.log_dir, now=self.now, settings_loader=self.settings_loader,
                account_fetcher=self.account_fetcher,
                executions_fetcher=None if self._default_executions_fetcher else self.executions_fetcher,
                profit_fetcher=None if self._default_profit_fetcher else self.profit_fetcher,
                account_scope=self.scope, daily_profit_fetcher=self.daily_profit_fetcher,
                period_profit_fetcher=self.period_profit_fetcher,
                closed_day_backoff=self._closed_backoff_enabled,
                theme_taxonomy_path=self.theme_taxonomy_path,
                closed_calendar_resolver=self._closed_calendar_resolver)
            # Serialize publication and ordinary tracker steps for the same
            # database. The shared sell queue hands any late, old-date fill
            # refresh from the recorder to the worker that owns its date.
            history._history_lock = self._history_lock
            history._materialize_lock = self._materialize_lock
            history._observation_write_lock = self._observation_write_lock
            history._reconciliation_lane_lock = self._reconciliation_lane_lock
            history._sell_refresh_lock = self._sell_refresh_lock
            history._sell_refresh_pending = self._sell_refresh_pending
            history._diagnostics = TrackerDiagnostics(
                self.log_dir / 'history-reconciliation', now=history._moment)
            self._defer_prior_day_sidecars = True
            self._history_tracker = history
        return self._history_tracker

    def _history_allowed(self, now):
        if now.strftime('%H:%M') < '15:40':
            return False
        return (not self._historical_work_deferred(now)
                and self._historical_guard_status['basis'] == 'verified_saved_session_conservative_union')

    @staticmethod
    def _closed_financial_queries():
        return (
            ('intents', 'SELECT id,side,symbol,quantity,strategy,order_number,state,reason FROM intents WHERE scope=? AND day=? AND mode=? ORDER BY id'),
            ('orders', 'SELECT ord_no,symbol,side,strategy,quantity FROM orders WHERE scope=? AND day=? AND mode=? ORDER BY ord_no'),
            ('fills', 'SELECT ord_no,fill_no,symbol,side,moment,price,quantity,strategy FROM fills WHERE scope=? AND day=? AND mode=? ORDER BY ord_no,fill_no'),
            ('profit_sources', 'SELECT symbol,digest FROM profit_sources WHERE scope=? AND day=? AND mode=? ORDER BY symbol'),
            ('profits', 'SELECT symbol,net,basis,reason FROM profits WHERE scope=? AND day=? AND mode=? ORDER BY symbol'),
        )

    @_diagnosed('closed_watermark')
    def _closed_change_watermark(self, db, mode, day, source_state):
        """Small economic rows and immutable source tails, never full JSON."""
        values = {'sources': source_state}
        for name, query in self._closed_financial_queries():
            rows = [tuple(row) for row in db.execute(query + ' LIMIT 20001', (self.scope, day, mode))]
            if len(rows) > 20000:
                return None
            values[name] = rows
        holding = db.execute('SELECT discrepancy FROM holdings WHERE scope=? AND mode=?', (self.scope, mode)).fetchone()
        values['discrepancy'] = holding[0] if holding else None
        return hashlib.sha256(canonical_json(values, maximum=8 * 1024 * 1024).encode()).hexdigest()

    def _prune_historical_tasks(self, mode, today, now, deferred):
        if deferred:
            self._cycle_days = [day for day in self._cycle_days if day == today]
        historical = {task[1] for task in self._tasks if task[1] != today}
        blocked = set(historical) if deferred else set()
        if historical and not deferred:
            with self._db() as db:
                blocked = {day for day in historical if self._closed_limit_blocked(db, mode, day)}
        if blocked:
            before = len(self._tasks)
            self._tasks = deque(task for task in self._tasks if task[1] not in blocked)
            self._cycle_days = [day for day in self._cycle_days if day not in blocked]
            with self._closed_cache_lock:
                for day in blocked:
                    self._closed_attempts.pop((mode, day), None)
            self._historical_guard_status['deferred_queued_tasks'] = before - len(self._tasks)
            self._next_at = now  # Old queue deadlines cannot postpone today's account read.

    def record_strategy_signal(self, event):
        """Independent research JSONL, leaving financial DB/schema unchanged."""
        from .strategy_signal_log import append_signal_event
        return append_signal_event(self.log_dir, event)

    def record_broker_observation(self, event):
        """Persist one allowlisted API page or WS 00 event, never create fills.

        Repeated deliveries remain separate observations. Unit execution fields
        from WS and cumulative/average fields retain their original field names.
        Failure is raised to the caller; raw provider messages are never logged.
        """
        try:
            if (not isinstance(event, dict) or event.get("mode") not in ("paper", "real")
                    or event.get("source_kind") != "broker_observation"):
                raise ValueError
            day = event.get("day")
            if not isinstance(day, str) or re.fullmatch(r"[0-9]{8}", day) is None:
                raise ValueError
            datetime.strptime(day, "%Y%m%d")
            moment = datetime.fromisoformat(event["observed_at"])
            if moment.tzinfo is None or moment.utcoffset() is None:
                raise ValueError
            api = event.get("source_api")
            if api == "00":
                allowed = {"schema_version", "source_api", "source_kind", "observed_at", "day", "mode", "event_kind", "values"}
                kinds = {"execution_stream", "stream_started", "stream_gap", "stream_resumed", "stream_stopped"}
                fields = set("9203 9001 912 913 302 900 901 902 903 904 905 906 907 908 909 910 911 914 915 938 939 919 922 923 2134 2135 2136".split())
                values = event.get("values")
                if (set(event) != allowed or type(event.get("schema_version")) is not int or event["schema_version"] != 1
                        or event.get("event_kind") not in kinds or not isinstance(values, dict)
                        or event["event_kind"] == "execution_stream" and not set(values) <= fields
                        or event["event_kind"] == "stream_gap" and not valid_gap_values(values)
                        or event["event_kind"] not in ("execution_stream", "stream_gap") and values
                        or any(type(value) is not str or len(value) > 256 or value and not value.isprintable() for value in values.values())):
                    raise ValueError
            else:
                allowed = {"schema", "source_api", "source_kind", "mode", "day", "query_id", "request_started_at",
                           "observed_at", "page", "request_body", "response_body", "continuation", "status", "http_status"}
                if (set(event) != allowed or event.get("schema") != "kiwoom-broker-page-v1" or api not in BROKER_OBSERVATION_SCHEMAS
                        or not isinstance(event.get("query_id"), str) or re.fullmatch(r"[0-9a-f]{32}", event["query_id"]) is None
                        or type(event.get("page")) is not int or not 1 <= event["page"] <= 20
                        or event.get("status") not in {"success", "broker_error", "invalid_response", "transport_error"}
                        or event.get("http_status") is not None and (type(event["http_status"]) is not int or not 100 <= event["http_status"] <= 599)):
                    raise ValueError
                started = datetime.fromisoformat(event["request_started_at"])
                if started.tzinfo is None or started.utcoffset() is None or started > moment:
                    raise ValueError
                schema = BROKER_OBSERVATION_SCHEMAS[api]
                request, response, continuation = event["request_body"], event["response_body"], event["continuation"]
                if (not isinstance(request, dict) or not set(request) <= schema["request_fields"]
                        or any(type(value) is not str or len(value) > 80 or value and not value.isprintable() for value in request.values())
                        or not isinstance(response, dict) or not set(response) <= schema["response_fields"]
                        or not isinstance(continuation, dict)
                        or set(continuation) != {"request_cont_yn", "response_cont_yn", "next_key_present"}
                        or continuation["request_cont_yn"] not in (None, "", "N", "Y")
                        or continuation["response_cont_yn"] not in (None, "", "N", "Y")
                        or type(continuation["next_key_present"]) is not bool):
                    raise ValueError
                for key, value in response.items():
                    if key == schema["row_field"]:
                        if not isinstance(value, list) or len(value) > 20000:
                            raise ValueError
                        for row in value:
                            if (not isinstance(row, dict) or not set(row) <= schema["row_fields"]
                                    or any(type(item) is not str or len(item) > 256 or item and not item.isprintable() for item in row.values())):
                                raise ValueError
                    elif key == "return_code":
                        if not (type(value) is int and -999999 <= value <= 999999 or type(value) is str and re.fullmatch(r"-?[0-9]{1,6}", value)):
                            raise ValueError
                    elif type(value) is not str or len(value) > 256 or value and not value.isprintable():
                        raise ValueError
            payload = canonical_json(event, maximum=4 * 1024 * 1024)
        except (KeyError, TypeError, ValueError, OverflowError):
            raise ExecutionLogError("증권사 관측 원자료의 허용 필드·형식·범위를 확인하지 못했습니다.") from None
        try:
            # Keep the immutable event and projection invalidation in one
            # transaction: an immediately subsequent profit projection must
            # not survive a newly persisted sell unit.
            sell_key = unit = None
            if api == "00" and event["event_kind"] == "execution_stream":
                values = event["values"]
                match = re.fullmatch(r"(?:A)?([0-9A-Z]{6})", values.get("9001", ""))
                qty = values.get("915", "")
                if not re.fullmatch(r"[0-9]{1,20}", qty) or int(qty) <= 0:
                    qty = values.get("911", "")
                fill_id = values.get("909", "")
                if values.get("907") == "1" and values.get("913") == "체결" and match:
                    mode, symbol = event["mode"], match.group(1)
                    sell_key = (mode, day, symbol)
                    order_id = values.get("9203", "")
                    unit = ((mode, day, symbol, order_id.lstrip("0"), fill_id.lstrip("0"),
                             values.get("914", ""), qty.lstrip("0"),
                             values.get("911", ""), values.get("903", ""))
                            if (re.fullmatch(r"[0-9]{1,20}", order_id) and int(order_id) > 0
                                and re.fullmatch(r"[0-9]{1,20}", fill_id) and int(fill_id) > 0
                                and re.fullmatch(r"[0-9]{1,20}", values.get("915", ""))
                                and int(values["915"]) > 0
                                and re.fullmatch(r"[0-9]{1,20}", values.get("914", ""))
                                and int(values["914"]) > 0) else None)
            with self._sell_refresh_lock if sell_key else nullcontext():
                novel_sell = sell_key is not None and (unit is None or unit not in self._sell_refresh_seen)
                obs_lock = getattr(self, "_observation_write_lock", None) or nullcontext()
                def _persist_once():
                    with obs_lock:
                        with self._db() as db:
                            persist_observation(db, self.scope, event, payload, epoch=self._broker_dedup_epoch)
                            if novel_sell:
                                db.execute("INSERT OR REPLACE INTO profits VALUES (?,?,?,?,NULL,NULL,?)",
                                    (self.scope, day, mode, symbol, "미확정: 매도 후 최신 72·73 보고 대기(post_sell_report_pending)"))
                _run_with_busy_retry(_persist_once)
                if novel_sell:
                    if unit is not None:
                        self._sell_refresh_seen.add(unit)
                        self._sell_refresh_seen_order.append(unit)
                        if len(self._sell_refresh_seen_order) > 10000:
                            self._sell_refresh_seen.discard(self._sell_refresh_seen_order.popleft())
                    self._profit_refresh_at.pop(sell_key, None)
                    self._daily_profit_refresh_at.pop((mode, day), None)
                    self._sell_refresh_pending.add(sell_key)
                    with self._closed_cache_lock:
                        self._closed_reconciliation.pop((mode, day), None)
                if api == '00' and event['event_kind'] in ('stream_started','stream_gap','stream_resumed','stream_stopped'):
                    with self._closed_cache_lock:
                        self._closed_reconciliation.clear()
                        for attempt in self._closed_attempts.values():
                            attempt['ok'] = False
                        self._closed_force_refresh = True
            def _publish_sidecars():
                if novel_sell:
                    self._materialize(day)
                else:
                    self._materialize_history(day)
                    if api in ("00", "ka10072", "ka10073", "ka10074"):
                        self._materialize_accounting(day)
            _run_with_busy_retry(_publish_sidecars)
        except ExecutionLogError:
            raise
        except Exception:
            raise ExecutionLogError("증권사 관측 원자료를 영구 기록하지 못했습니다.") from None

    @contextmanager
    def _db(self, *, require_existing_daily_ledger=False):
        if not require_existing_daily_ledger:
            self.log_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        if (self.path.is_symlink() or (self.path.exists() and not self.path.is_file())
                or require_existing_daily_ledger and not self.path.is_file()):
            raise ExecutionLogError("자동매매 원장 파일을 확인하지 못했습니다.")
        database = (sqlite3.connect(self.path.resolve().as_uri() + "?mode=rw", uri=True, timeout=_SQLITE_TIMEOUT_SECONDS)
                    if require_existing_daily_ledger else sqlite3.connect(self.path, timeout=_SQLITE_TIMEOUT_SECONDS))
        try:
            database.row_factory = sqlite3.Row
            if require_existing_daily_ledger:
                # Check the same opened DB before the normal create/migration
                # script can create missing tables. mode=rw forbids creation.
                self._validate_daily_ledger(database)
            os.chmod(self.path, 0o600)
            database.execute("PRAGMA synchronous=FULL")
            identity = self._database_identity(database)
            if getattr(self, "_schema_initialized_identity", None) != identity:
                database.executescript("""
          CREATE TABLE IF NOT EXISTS intents (
            scope TEXT, id TEXT, day TEXT, mode TEXT, side TEXT, symbol TEXT,
            quantity INTEGER, strategy TEXT, created_at TEXT, order_number TEXT,
            state TEXT NOT NULL DEFAULT 'prepared', reason TEXT,
            PRIMARY KEY(scope,id));
          CREATE TABLE IF NOT EXISTS order_decisions (
            scope TEXT NOT NULL, intent_id TEXT NOT NULL, recorded_at TEXT NOT NULL,
            order_price TEXT, order_type TEXT NOT NULL, evidence_json TEXT, evidence_sha256 TEXT,
            PRIMARY KEY(scope,intent_id));
          CREATE TRIGGER IF NOT EXISTS order_decisions_no_update BEFORE UPDATE ON order_decisions
            BEGIN SELECT RAISE(ABORT, 'order decisions are immutable'); END;
          CREATE TRIGGER IF NOT EXISTS order_decisions_no_delete BEFORE DELETE ON order_decisions
            BEGIN SELECT RAISE(ABORT, 'order decisions are immutable'); END;
          CREATE TABLE IF NOT EXISTS pre_order_reconciliations (
            scope TEXT NOT NULL, intent_id TEXT NOT NULL, recorded_at TEXT NOT NULL,
            evidence_json TEXT NOT NULL, evidence_sha256 TEXT NOT NULL,
            PRIMARY KEY(scope,intent_id));
          CREATE TRIGGER IF NOT EXISTS pre_order_reconciliations_no_update BEFORE UPDATE ON pre_order_reconciliations
            BEGIN SELECT RAISE(ABORT, 'pre-order evidence is immutable'); END;
          CREATE TRIGGER IF NOT EXISTS pre_order_reconciliations_no_delete BEFORE DELETE ON pre_order_reconciliations
            BEGIN SELECT RAISE(ABORT, 'pre-order evidence is immutable'); END;
          CREATE TABLE IF NOT EXISTS b3_add_claims (
            scope TEXT NOT NULL, initial_intent_id TEXT NOT NULL, add_intent_id TEXT NOT NULL,
            claimed_at TEXT NOT NULL, PRIMARY KEY(scope,initial_intent_id), UNIQUE(scope,add_intent_id));
          CREATE TRIGGER IF NOT EXISTS b3_add_claims_no_update BEFORE UPDATE ON b3_add_claims
            BEGIN SELECT RAISE(ABORT, 'B3 claims are immutable'); END;
          CREATE TRIGGER IF NOT EXISTS b3_add_claims_no_delete BEFORE DELETE ON b3_add_claims
            BEGIN SELECT RAISE(ABORT, 'B3 claims are immutable'); END;
          CREATE TABLE IF NOT EXISTS b3_invalidations (
            scope TEXT NOT NULL, initial_intent_id TEXT NOT NULL, symbol TEXT NOT NULL,
            observed_at TEXT NOT NULL, reason TEXT NOT NULL, PRIMARY KEY(scope,initial_intent_id));
          CREATE TRIGGER IF NOT EXISTS b3_invalidations_no_update BEFORE UPDATE ON b3_invalidations
            BEGIN SELECT RAISE(ABORT, 'B3 invalidations are immutable'); END;
          CREATE TRIGGER IF NOT EXISTS b3_invalidations_no_delete BEFORE DELETE ON b3_invalidations
            BEGIN SELECT RAISE(ABORT, 'B3 invalidations are immutable'); END;
          CREATE TABLE IF NOT EXISTS multi_strategy_exit_claims (
            scope TEXT NOT NULL, generation_id TEXT NOT NULL, exit_kind TEXT NOT NULL,
            intent_id TEXT NOT NULL, claimed_at TEXT NOT NULL,
            PRIMARY KEY(scope,intent_id));
          CREATE TRIGGER IF NOT EXISTS multi_strategy_exit_claims_no_update BEFORE UPDATE ON multi_strategy_exit_claims
            BEGIN SELECT RAISE(ABORT, 'exit claims are immutable'); END;
          CREATE TRIGGER IF NOT EXISTS multi_strategy_exit_claims_no_delete BEFORE DELETE ON multi_strategy_exit_claims
            BEGIN SELECT RAISE(ABORT, 'exit claims are immutable'); END;
          CREATE TABLE IF NOT EXISTS multi_strategy_exit_releases (
            scope TEXT NOT NULL, intent_id TEXT NOT NULL, released_at TEXT NOT NULL,
            reason TEXT NOT NULL, PRIMARY KEY(scope,intent_id));
          CREATE TRIGGER IF NOT EXISTS multi_strategy_exit_releases_no_update BEFORE UPDATE ON multi_strategy_exit_releases
            BEGIN SELECT RAISE(ABORT, 'exit releases are immutable'); END;
          CREATE TRIGGER IF NOT EXISTS multi_strategy_exit_releases_no_delete BEFORE DELETE ON multi_strategy_exit_releases
            BEGIN SELECT RAISE(ABORT, 'exit releases are immutable'); END;
          CREATE TABLE IF NOT EXISTS strategy_holding_observations (
            scope TEXT NOT NULL, id TEXT NOT NULL, day TEXT NOT NULL, mode TEXT NOT NULL,
            symbol TEXT NOT NULL, observed_at TEXT NOT NULL, source_kind TEXT NOT NULL,
            held_qty INTEGER NOT NULL, sellable_qty INTEGER NOT NULL, purchase_amount INTEGER NOT NULL,
            digest TEXT NOT NULL, PRIMARY KEY(scope,id));
          CREATE TRIGGER IF NOT EXISTS strategy_holding_observations_no_update BEFORE UPDATE ON strategy_holding_observations
            BEGIN SELECT RAISE(ABORT, 'holding observations are immutable'); END;
          CREATE TRIGGER IF NOT EXISTS strategy_holding_observations_no_delete BEFORE DELETE ON strategy_holding_observations
            BEGIN SELECT RAISE(ABORT, 'holding observations are immutable'); END;
          CREATE TABLE IF NOT EXISTS paper_experiment_signal_claims (
            scope TEXT NOT NULL,day TEXT NOT NULL,mode TEXT NOT NULL,symbol TEXT NOT NULL,
            strategy TEXT NOT NULL,signal_sha256 TEXT NOT NULL,intent_id TEXT NOT NULL,
            PRIMARY KEY(scope,day,mode,symbol,strategy,signal_sha256),UNIQUE(scope,intent_id));
          CREATE TRIGGER IF NOT EXISTS paper_experiment_signal_claims_no_update BEFORE UPDATE ON paper_experiment_signal_claims
            BEGIN SELECT RAISE(ABORT, 'experiment signal claims are immutable'); END;
          CREATE TRIGGER IF NOT EXISTS paper_experiment_signal_claims_no_delete BEFORE DELETE ON paper_experiment_signal_claims
            BEGIN SELECT RAISE(ABORT, 'experiment signal claims are immutable'); END;
          CREATE TABLE IF NOT EXISTS orders (
            scope TEXT, day TEXT, mode TEXT, ord_no TEXT, symbol TEXT, side TEXT,
            strategy TEXT, quantity INTEGER, registered_at TEXT,
            PRIMARY KEY(scope,day,mode,ord_no));
          CREATE TABLE IF NOT EXISTS fills (
            scope TEXT, day TEXT, mode TEXT, ord_no TEXT, fill_no TEXT, symbol TEXT,
            name TEXT, side TEXT, moment TEXT, price TEXT, quantity INTEGER, strategy TEXT,
            remaining TEXT, holdings_at TEXT, holdings_status TEXT,
            PRIMARY KEY(scope,day,mode,ord_no,fill_no));
          CREATE TABLE IF NOT EXISTS execution_observations (
            id INTEGER PRIMARY KEY AUTOINCREMENT, scope TEXT NOT NULL, day TEXT NOT NULL,
            mode TEXT NOT NULL, ord_no TEXT NOT NULL, fill_no TEXT NOT NULL,
            observed_at TEXT NOT NULL, kind TEXT NOT NULL, source_kind TEXT NOT NULL,
            digest TEXT NOT NULL, payload_json TEXT NOT NULL);
          CREATE INDEX IF NOT EXISTS execution_observations_key ON execution_observations(scope,day,mode,ord_no,fill_no);
          CREATE TRIGGER IF NOT EXISTS execution_observations_no_update BEFORE UPDATE ON execution_observations
            BEGIN SELECT RAISE(ABORT, 'execution observations are immutable'); END;
          CREATE TRIGGER IF NOT EXISTS execution_observations_no_delete BEFORE DELETE ON execution_observations
            BEGIN SELECT RAISE(ABORT, 'execution observations are immutable'); END;
          CREATE TABLE IF NOT EXISTS broker_observations (
            id INTEGER PRIMARY KEY AUTOINCREMENT, scope TEXT NOT NULL, day TEXT NOT NULL,
            mode TEXT NOT NULL, observed_at TEXT NOT NULL, source_api TEXT NOT NULL,
            digest TEXT NOT NULL, payload_json TEXT NOT NULL);
          CREATE INDEX IF NOT EXISTS broker_observations_accounting ON broker_observations(scope,day,source_api,id);
          CREATE TRIGGER IF NOT EXISTS broker_observations_no_update BEFORE UPDATE ON broker_observations
            BEGIN SELECT RAISE(ABORT, 'broker observations are immutable'); END;
          CREATE TRIGGER IF NOT EXISTS broker_observations_no_delete BEFORE DELETE ON broker_observations
            BEGIN SELECT RAISE(ABORT, 'broker observations are immutable'); END;
          CREATE TABLE IF NOT EXISTS profits (
            scope TEXT, day TEXT, mode TEXT, symbol TEXT, net TEXT, basis TEXT, reason TEXT,
            PRIMARY KEY(scope,day,mode,symbol));
          CREATE TABLE IF NOT EXISTS profit_sources (
            scope TEXT, day TEXT, mode TEXT, symbol TEXT, digest TEXT NOT NULL,
            PRIMARY KEY(scope,day,mode,symbol));
          CREATE TABLE IF NOT EXISTS profit_observations (
            scope TEXT NOT NULL, day TEXT NOT NULL, mode TEXT NOT NULL, symbol TEXT NOT NULL,
            digest TEXT NOT NULL, observed_at TEXT NOT NULL, source_kind TEXT NOT NULL,
            payload_json TEXT NOT NULL, PRIMARY KEY(scope,day,mode,symbol,digest));
          CREATE TRIGGER IF NOT EXISTS profit_observations_no_update BEFORE UPDATE ON profit_observations
            BEGIN SELECT RAISE(ABORT, 'profit observations are immutable'); END;
          CREATE TRIGGER IF NOT EXISTS profit_observations_no_delete BEFORE DELETE ON profit_observations
            BEGIN SELECT RAISE(ABORT, 'profit observations are immutable'); END;
          CREATE TABLE IF NOT EXISTS summaries (
            scope TEXT, day TEXT, mode TEXT, digest TEXT, payload TEXT,
            PRIMARY KEY(scope,day,mode));
          CREATE TABLE IF NOT EXISTS holdings (
            scope TEXT, mode TEXT, observed_at TEXT, positions TEXT, discrepancy TEXT,
            PRIMARY KEY(scope,mode));
          CREATE TABLE IF NOT EXISTS account_balance_daily (
            scope TEXT NOT NULL, mode TEXT NOT NULL, day TEXT NOT NULL, observed_at TEXT NOT NULL,
            source_kind TEXT NOT NULL, payload_json TEXT NOT NULL,
            PRIMARY KEY(scope,mode,day));
          CREATE TABLE IF NOT EXISTS paper_account_balance_observations (
            id INTEGER PRIMARY KEY, scope TEXT NOT NULL, mode TEXT NOT NULL, day TEXT NOT NULL,
            observed_at TEXT NOT NULL, source_kind TEXT NOT NULL, digest TEXT NOT NULL,
            payload_json TEXT NOT NULL, UNIQUE(scope,mode,day,observed_at));
          CREATE TRIGGER IF NOT EXISTS paper_account_balance_observations_no_update
            BEFORE UPDATE ON paper_account_balance_observations
            BEGIN SELECT RAISE(ABORT, 'paper balance observations are immutable'); END;
          CREATE TRIGGER IF NOT EXISTS paper_account_balance_observations_no_delete
            BEFORE DELETE ON paper_account_balance_observations
            BEGIN SELECT RAISE(ABORT, 'paper balance observations are immutable'); END;
                """)
                create_repeat_schema(database)
                from .expired_order_certificates import create_schema as create_expiry_schema
                create_expiry_schema(database)
                columns = {row[1] for row in database.execute("PRAGMA table_info(intents)")}
                if "state" not in columns:
                    database.execute("ALTER TABLE intents ADD COLUMN state TEXT NOT NULL DEFAULT 'prepared'")
                    database.execute("UPDATE intents SET state='accepted' WHERE order_number IS NOT NULL")
                if "reason" not in columns:
                    database.execute("ALTER TABLE intents ADD COLUMN reason TEXT")
                self._schema_initialized_identity = self._database_identity(database)
            install_evidence_view(database)
            with database:
                # Existing projections are retained exactly once, with migration
                # time explicitly distinct from their unknown discovery time.
                legacy = list(database.execute("""SELECT f.* FROM fills f WHERE NOT EXISTS (
                    SELECT 1 FROM execution_observations o WHERE o.scope=f.scope AND o.day=f.day
                    AND o.mode=f.mode AND o.ord_no=f.ord_no AND o.fill_no=f.fill_no)"""))
                for row in legacy:
                    self._observe_execution(database, dict(row), "legacy_projection", "legacy_existing_projection",
                                            self._moment().isoformat())
                yield database
        finally:
            database.close()

    @contextmanager
    def _ro_db(self):
        if self.path.is_symlink() or (self.path.exists() and not self.path.is_file()):
            raise ExecutionLogError("자동매매 원장 파일을 확인하지 못했습니다.")
        if not self.path.is_file():
            with self._db():
                pass
        database = sqlite3.connect(
            self.path.resolve().as_uri() + "?mode=ro",
            uri=True,
            timeout=_SQLITE_TIMEOUT_SECONDS,
        )
        needs_init = False
        try:
            database.row_factory = sqlite3.Row
            identity = self._database_identity(database)
            if getattr(self, "_schema_initialized_identity", None) != identity:
                present = {
                    row[0]
                    for row in database.execute(
                        "SELECT name FROM main.sqlite_master WHERE type='table' "
                        "AND name IN ('orders','fills','summaries','profits',"
                        "'execution_observations','broker_observations','broker_observation_repeats',"
                        "'account_balance_daily','paper_account_balance_observations','expired_order_certificates')"
                    )
                }
                if len(present) < 10:
                    needs_init = True
                else:
                    self._schema_initialized_identity = identity
            if not needs_init:
                install_evidence_view(database)
                database.execute("PRAGMA query_only=ON")
                yield database
        finally:
            database.close()
        if needs_init:
            with self._db():
                pass
            database2 = sqlite3.connect(
                self.path.resolve().as_uri() + "?mode=ro",
                uri=True,
                timeout=_SQLITE_TIMEOUT_SECONDS,
            )
            try:
                database2.row_factory = sqlite3.Row
                install_evidence_view(database2)
                database2.execute("PRAGMA query_only=ON")
                yield database2
            finally:
                database2.close()

    def _database_identity(self, db):
        info = self.path.stat()
        return info.st_dev, info.st_ino, db.execute("PRAGMA schema_version").fetchone()[0]

    @_diagnosed('broker_snapshot')
    def _broker_observations_snapshot(self, db, day, *, mode=None, apis=None):
        """Reuse immutable parsed rows, reading only validated append deltas.

        The ledger's UPDATE/DELETE triggers are the append-only contract, not
        a defense against arbitrary external edits with those triggers removed.
        Device/inode/schema changes, count/MAX regressions, a changed tail or
        an inconsistent delta all rebuild the cache. The small read savepoint
        binds COUNT/MAX and its rows to one SQLite snapshot. Uncommitted caller
        changes never enter the shared cache. Returned tuples and JSON trees
        cannot be changed by another exporter or reconciliation thread.
        """
        key = self.scope, day
        with self._broker_cache_lock:
            # Historical report reads may revisit any date, but must not keep
            # every day's raw broker pages resident for the process lifetime.
            for expired_key in tuple(self._broker_cache):
                if expired_key != key:
                    self._broker_cache.pop(expired_key, None)
            transactional = db.in_transaction
            identity = self._database_identity(db)
            db.execute("SAVEPOINT broker_observations_snapshot")
            try:
                with self._diagnostics.stage('sql_snapshot'):
                    count, last_id = db.execute(
                        "SELECT COUNT(*),COALESCE(MAX(id),0) FROM broker_observation_evidence WHERE scope=? AND day=?", key).fetchone()
                cached = None if transactional else self._broker_cache.get(key)
                append = (cached is not None and cached[0] == identity and count >= cached[1]
                    and ((count == cached[1] and last_id == cached[2])
                         or (count > cached[1] and last_id > cached[2])))
                if append and cached[1]:
                    with self._diagnostics.stage('sql_snapshot'):
                        tail = db.execute("SELECT digest FROM broker_observation_evidence WHERE scope=? AND day=? AND id=?",
                            (*key, cached[2])).fetchone()
                    append = tail is not None and tail[0] == cached[3][-1].digest
                after_id = cached[2] if append else 0
                # SQLite expands compact receipts in this SELECT. Its hydration
                # CPU is included here; never emit a diagnostics event per row.
                with self._diagnostics.stage('sql_snapshot') as measurement:
                    rows = list(db.execute(
                        "SELECT id,digest,payload_json,mode,source_api FROM broker_observation_evidence WHERE scope=? AND day=? AND id>? ORDER BY id",
                        (*key, after_id)))
                    measurement.set_counts(rows=len(rows))
                expected = count - cached[1] if append else count
                if (len(rows) != expected or rows and (rows[-1]["id"] != last_id
                        or any(left["id"] >= right["id"] for left, right in zip(rows, rows[1:])))):
                    # A malformed/non-append source must not inherit a cached
                    # prefix. Re-read the complete current snapshot instead.
                    append = False
                    with self._diagnostics.stage('sql_snapshot') as measurement:
                        rows = list(db.execute(
                            "SELECT id,digest,payload_json,mode,source_api FROM broker_observation_evidence WHERE scope=? AND day=? ORDER BY id", key))
                        measurement.set_counts(rows=len(rows))
                    if len(rows) != count or (rows[-1]["id"] if rows else 0) != last_id:
                        raise ExecutionLogError("증권사 관측 원장의 행 수·최신 ID를 확인하지 못했습니다.")
                with self._diagnostics.stage('json_parse') as measurement:
                    measurement.set_counts(rows=len(rows),
                        bytes_in=sum(len(row['payload_json'].encode('utf-8')) for row in rows),
                        bytes_in_basis='payload_json_utf8')
                    added = tuple(_BrokerObservation(row["id"], row["digest"], row["payload_json"], row["mode"],
                        row["source_api"], _immutable_json(json.loads(row["payload_json"]))) for row in rows)
                snapshot = cached[3] + added if append else added
            except Exception:
                self._broker_cache.pop(key, None)
                db.execute("ROLLBACK TO broker_observations_snapshot")
                raise
            finally:
                db.execute("RELEASE broker_observations_snapshot")
            if not transactional and self._database_identity(db) == identity:
                self._broker_cache[key] = identity, count, last_id, snapshot
            else:
                self._broker_cache.pop(key, None)
            if mode is not None or apis is not None:
                return tuple(row for row in snapshot if (mode is None or row.mode == mode)
                    and (apis is None or row.source_api in apis))
            return snapshot

    @staticmethod
    def _validate_daily_ledger(db):
        """Fail closed on an incomplete order ledger before any schema repair.

        This validates the schema, not broker-account identity or manual-order
        coverage. The surrounding execution tracker establishes account scope.
        """
        required_columns = {
            "intents": {"scope", "id", "day", "mode", "side", "symbol", "quantity", "strategy", "created_at", "order_number", "state", "reason"},
            "orders": {"scope", "day", "mode", "ord_no", "symbol", "side", "strategy", "quantity", "registered_at"},
            "fills": {"scope", "day", "mode", "ord_no", "fill_no", "symbol", "name", "side", "moment", "price", "quantity", "strategy", "remaining", "holdings_at", "holdings_status"},
            "order_decisions": {"scope", "intent_id", "recorded_at", "order_price", "order_type", "evidence_json", "evidence_sha256"},
            "execution_observations": {"scope", "day", "mode", "ord_no", "fill_no", "observed_at", "kind", "source_kind", "digest", "payload_json"},
            "broker_observations": {"scope", "day", "mode", "observed_at", "source_api", "digest", "payload_json"},
            "pre_order_reconciliations": {"scope", "intent_id", "recorded_at", "evidence_json", "evidence_sha256"},
            "profit_observations": {"scope", "day", "mode", "symbol", "digest", "observed_at", "source_kind", "payload_json"},
            "profits": {"scope", "day", "mode", "symbol", "net", "basis", "reason"},
            "profit_sources": {"scope", "day", "mode", "symbol", "digest"},
            "summaries": {"scope", "day", "mode", "digest", "payload"},
            "holdings": {"scope", "mode", "observed_at", "positions", "discrepancy"},
            "account_balance_daily": {"scope", "mode", "day", "observed_at", "source_kind", "payload_json"},
        }
        keys = {"intents": ("scope", "id"), "orders": ("scope", "day", "mode", "ord_no"),
                "fills": ("scope", "day", "mode", "ord_no", "fill_no"),
                "order_decisions": ("scope", "intent_id")}
        try:
            items = {r[0]: (r[1], r[2], r[3]) for r in db.execute("SELECT name,type,tbl_name,sql FROM sqlite_master")}
            for table, required in required_columns.items():
                if items.get(table, (None,))[0] != "table":
                    raise ValueError
                cols = list(db.execute(f'PRAGMA table_info("{table}")'))
                if not required.issubset({r[1] for r in cols}):
                    raise ValueError
                if table in keys:
                    primary = tuple(r[1] for r in sorted((r for r in cols if r[5]), key=lambda r:r[5]))
                    if primary != keys[table]:
                        raise ValueError
            for table in ("order_decisions", "pre_order_reconciliations", "execution_observations", "broker_observations", "profit_observations"):
                for action in ("update", "delete"):
                    kind, owner, sql = items.get(f"{table}_no_{action}", (None, None, None))
                    normalized = " ".join((sql or "").upper().split())
                    if (kind != "trigger" or owner != table or f"BEFORE {action.upper()} ON" not in normalized
                            or "RAISE(ABORT" not in normalized):
                        raise ValueError
        except (sqlite3.Error, ValueError, TypeError, AttributeError):
            raise DailyEntryLimitUnavailable("일일 진입 원장의 기존 구조를 확인하지 못했습니다.") from None

    def _daily_buy_entry_counts(self, db, mode, symbol, day):
        """Distinct registered buy orders and unresolved local reservations.

        This ledger only establishes program-recorded orders; it is not a
        complete broker-account history of externally submitted manual orders.
        """
        if (mode != "paper" or not isinstance(symbol, str)
                or re.fullmatch(r"[0-9]{6}", symbol) is None
                or not isinstance(day, str) or re.fullmatch(r"[0-9]{8}", day) is None):
            raise DailyEntryLimitUnavailable("일일 진입 범위를 확인하지 못했습니다.")
        try:
            self._validate_daily_ledger(db)
            params = (self.scope, day, mode, symbol)
            numbers = set()
            for table in ("orders", "fills"):
                for row in db.execute(f"SELECT ord_no FROM {table} WHERE scope=? AND day=? AND mode=? AND symbol=? AND side='buy'", params):
                    numbers.add(order_key(row[0]))
            reserved = 0
            rows = db.execute("SELECT state,order_number FROM intents WHERE scope=? AND day=? AND mode=? AND symbol=? AND side='buy'", params)
            for row in rows:
                state, number = row[0], row[1]
                if state == "cancelled":
                    if number is not None:
                        raise ValueError
                elif state == "accepted":
                    if number is None:
                        raise ValueError
                    numbers.add(order_key(number))
                elif state == "prepared" and number is None:
                    reserved += 1
                else:
                    raise ValueError
            return {"accepted": len(numbers), "reserved": reserved}
        except (sqlite3.Error, ValueError, TypeError, AttributeError, ExecutionsError):
            raise DailyEntryLimitUnavailable("일일 진입 원장을 확인하지 못했습니다.") from None

    def daily_buy_entry_counts(self, mode, symbol, now):
        if (not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None
                or self.path.is_symlink() or not self.path.is_file()):
            raise DailyEntryLimitUnavailable("일일 진입 원장을 확인하지 못했습니다.")
        try:
            # Read-only precheck: no table/trigger creation or migration.
            with sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True, timeout=_SQLITE_TIMEOUT_SECONDS) as db:
                db.execute("BEGIN")
                return self._daily_buy_entry_counts(db, mode, symbol, now.astimezone(KST).strftime("%Y%m%d"))
        except (OSError, sqlite3.Error, ExecutionLogError):
            raise DailyEntryLimitUnavailable("일일 진입 원장을 확인하지 못했습니다.") from None

    def _symbol_order_watermark(self, db, mode, symbol, *, ignored_intent_id=None):
        """Digest the complete local order/fill state for one paper symbol."""
        if mode!='paper' or not isinstance(symbol,str) or re.fullmatch(r'[0-9]{6}',symbol) is None:
            raise ExecutionLogError('종목별 주문 원장 범위를 확인하지 못했습니다.')
        self._validate_daily_ledger(db)
        specs={
            'intents':('id,day,side,quantity,strategy,created_at,order_number,state,reason','day,id'),
            'orders':('day,ord_no,side,quantity,strategy,registered_at','day,ord_no'),
            'fills':('day,ord_no,fill_no,side,price,quantity,moment','day,ord_no,fill_no'),
        }
        data={'mode':mode,'symbol':symbol,'scope':self.scope,
              'database_identity':list(self._database_identity(db)),'rows':{}}
        for table,(fields,ordering) in specs.items():
            rows=db.execute(f'SELECT {fields} FROM {table} WHERE scope=? AND mode=? AND symbol=? ORDER BY {ordering} LIMIT 10001',
                            (self.scope,mode,symbol)).fetchall()
            if len(rows)>10000:raise ExecutionLogError('종목별 주문 원장 조회 한도를 넘었습니다.')
            data['rows'][table]=[list(row) for row in rows if table!='intents' or row['id']!=ignored_intent_id]
        # Cancellation confirmations are independent immutable evidence; they
        # never change the original order quantity or historical financial rows.
        from .cancellation_settlement import settlement_watermark_rows
        cancellations = settlement_watermark_rows(db, self.scope, mode, symbol)
        if cancellations:
            data['rows']['confirmed_cancellations'] = cancellations
        return hashlib.sha256(canonical_json(data,maximum=8*1024*1024).encode('utf-8')).hexdigest()

    def _confirmed_cancelled_quantity(self, db, order):
        from .cancellation_settlement import confirmed_cancelled_quantity
        try:
            if order['side']=='sell':
                from .unfilled_sell_auction import confirmed_sell_cancelled_quantity
                result=confirmed_sell_cancelled_quantity(db,self.scope,order)
            else:
                result = confirmed_cancelled_quantity(db, self.scope, order)
            if (type(result) is not int or result < 0 or type(order['quantity']) is not int
                    or order['quantity'] <= 0 or result > order['quantity']):
                raise ValueError
            return result
        except (ValueError, TypeError, KeyError, sqlite3.Error):
            raise ExecutionLogError('취소 확정 및 잔여 체결 증빙이 불완전하여 예약 해제를 보류합니다.') from None

    def _paper_rejected_closing_buy(self, db, order, row, detailed, pending, number, key):
        """Prove the narrower EXP_D case where dated REST retains the original remainder."""
        from .unfilled_buy_ttl import TTLLedger, TTLUnavailable, _auction_window_end

        if (order['strategy'] != 'EXP_D' or order['side'] != 'buy'
                or type(order['quantity']) is not int or order['quantity'] <= 0
                or type(order['filled_quantity']) is not int or order['filled_quantity'] != 0
                or number(row.get('ord_remnq')) != order['quantity']
                or key(row.get('ori_ord')) != '0' or row.get('mdfy_cncl') != '일반'
                or row.get('acpt_tp') != '주문완료' or number(row.get('cnfm_qty')) != order['quantity']
                or number(row.get('cntr_uv')) != 0
                or not detailed[3] or not pending[3]):
            return False
        observation_day = pending[1].astimezone(KST).date()
        if any(moment.astimezone(KST).date() != observation_day
               for moment in (detailed[0], detailed[1], pending[0])):
            return False
        original = order_key(order['ord_no'])
        if any(key(item.get('ori_ord')) in (None, original) for item in detailed[2]):
            return False
        if any(key(item.get('orig_ord_no')) is None for item in pending[2]):
            return False
        if db.execute("""SELECT 1 FROM orders WHERE scope=? AND mode='paper' AND ord_no=? AND day<>?
                LIMIT 1""", (self.scope, order['ord_no'], order['day'])).fetchone():
            return False

        decisions = db.execute("""SELECT i.id, i.created_at, d.order_price, d.order_type,
                d.evidence_json, d.evidence_sha256 FROM intents i JOIN order_decisions d
                ON d.scope=i.scope AND d.intent_id=i.id
                WHERE i.scope=? AND i.day=? AND i.mode='paper' AND i.order_number=?
                AND i.symbol=? AND i.side='buy' AND i.strategy='EXP_D'
                AND i.quantity=? AND i.state='accepted' LIMIT 2""",
                (self.scope, order['day'], order['ord_no'], order['symbol'], order['quantity'])).fetchall()
        if len(decisions) != 1:
            return False
        decision = dict(decisions[0], strategy='EXP_D')
        registered = datetime.fromisoformat(order['registered_at'])
        price = number(decision['order_price'])
        if registered.tzinfo is None:
            return False
        registered = registered.astimezone(KST)
        if (registered.strftime('%Y%m%d') != order['day']
                or decision['order_type'] != 'limit' or not price or price != number(row.get('ord_uv'))
                or json.loads(decision['evidence_json']).get('strategy') != 'EXP_D'):
            return False
        window_end = _auction_window_end(decision, registered)
        if window_end is None or not window_end - timedelta(minutes=10) <= registered < window_end:
            return False

        ledger = TTLLedger.__new__(TTLLedger)
        ledger.scope = self.scope
        try:
            claim, outcome = ledger.latest_attempt(db, order['day'], order['ord_no'])
        except TTLUnavailable:
            return False
        if (claim is None or outcome is None or outcome['status'] != 'cancel_broker_rejected'
                or outcome['cancellation_ord_no'] is not None or outcome['reported_cancel_quantity'] is not None
                or claim['intent_id'] != decision['id'] or claim['symbol'] != order['symbol']):
            return False
        claimed = datetime.fromisoformat(claim['claimed_at'])
        rejected = datetime.fromisoformat(outcome['observed_at'])
        if (claimed.tzinfo is None or rejected.tzinfo is None or not window_end <= claimed <= rejected
                or rejected.astimezone(KST).strftime('%Y%m%d') != order['day']):
            return False
        ttl = json.loads(claim['evidence_json'])
        expected = {
            'schema': 'gdcrs-unfilled-buy-ttl-v1', 'mode': 'paper', 'strategy': 'EXP_D',
            'symbol': order['symbol'], 'original_order_number': order['ord_no'],
            'due_reason': 'auction_window_end', 'expiry_policy': 'verified_auction_window',
            'expires_at': window_end.isoformat(), 'order_window_end_at': window_end.isoformat(),
            'accepted_at': order['registered_at'], 'intent_created_at': decision['created_at'],
            'claimed_at': claim['claimed_at'],
        }
        if (not all(ttl.get(field) == value for field, value in expected.items())
                or ttl.get('ack_releases_principal') is not False
                or type(ttl.get('cancel_request_qty')) is not int or ttl['cancel_request_qty'] != 0
                or type(ttl.get('broker_remaining_qty')) is not int
                or ttl['broker_remaining_qty'] != order['quantity']):
            return False
        return {'decision_sha256': decision['evidence_sha256'], 'ttl_claim_sha256': claim['evidence_sha256'],
                'ttl_outcome_sha256': ledger._outcome_digest(outcome)}

    def _paper_prior_order_terminal(self, db, order, now, *, _source_max_id=None, _captured_at=None,
                                    _rejected_auction_context=None):
        """Check persisted full REST evidence; never infer fills or change orders.

        Zero remaining is the direct read rule. The certificate path can also
        prove a rejected, unfilled EXP_D limit order from the completed auction.
        """
        try:
            if order['mode']!='paper' or not isinstance(now,datetime) or now.tzinfo is None:
                return False
            source_clock=_captured_at or now
            today=source_clock.astimezone(KST).strftime('%Y%m%d')
            if not order['day']<today or order['side'] not in ('buy','sell'):
                return False
            from .expired_order_certificates import (
                cached_captured_terminal_pages,
                select_late_order_observations,
            )
            def _load_terminal_pages():
                events=db.execute("""SELECT id,day,source_api,digest,payload_json FROM broker_observation_evidence
                    WHERE scope=? AND mode='paper' AND ((day=? AND source_api='kt00007')
                      OR (day=? AND source_api='ka10075')) AND (? IS NULL OR id<=?) ORDER BY id DESC LIMIT 20001""",
                    (self.scope,order['day'],today,_source_max_id,_source_max_id)).fetchall()
                if len(events)>20000:return None
                grouped={}
                for entry in events:
                    raw=entry['payload_json']
                    if not isinstance(raw,str) or hashlib.sha256(raw.encode()).hexdigest()!=entry['digest']:return None
                    event=json.loads(raw)
                    if (event.get('mode')!='paper' or event.get('day')!=entry['day']
                            or event.get('source_api')!=entry['source_api'] or event.get('source_kind')!='broker_observation'):
                        return None
                    key=(entry['source_api'],event.get('query_id'))
                    grouped.setdefault(key,[]).append((entry['id'],event))
                def latest(api):
                    queries=[items for (name,_),items in grouped.items() if name==api]
                    if not queries:return None
                    selected=max(queries,key=lambda pages:max(x[0] for x in pages))
                    pages=sorted((event for _,event in selected),key=lambda event:event.get('page',0))
                    values=[];last=None;first=None
                    for idx,event in enumerate(pages):
                        c=event['continuation'];body=event['request_body'];response=event['response_body']
                        started=datetime.fromisoformat(event['request_started_at']);at=datetime.fromisoformat(event['observed_at'])
                        if (event['page']!=idx+1 or event['status']!='success' or started.tzinfo is None or at.tzinfo is None
                                or not started<=at<=source_clock or last is not None and started<last
                                or response.get('return_code') not in (0,'0') or isinstance(response.get('return_code'),bool)
                                or c.get('request_cont_yn')!=('N' if idx==0 else 'Y')
                                or idx<len(pages)-1 and (c.get('response_cont_yn')!='Y' or c.get('next_key_present') is not True)
                                or idx==len(pages)-1 and c.get('response_cont_yn') not in (None,'','N')):return None
                        if api=='kt00007':
                            if (body.get('ord_dt')!=order['day'] or body.get('qry_tp') not in ('1','2')
                                    or body.get('stk_bond_tp') not in ('0','1') or body.get('sell_tp')!='0'
                                    or body.get('stk_cd') not in (None,'') or body.get('fr_ord_no') not in (None,'')
                                    or body.get('dmst_stex_tp') not in ('%','KRX')):return None
                            rows=response.get('acnt_ord_cntr_prps_dtl')
                        else:
                            if (body.get('all_stk_tp')!='0' or body.get('trde_tp')!='0'
                                    or body.get('stex_tp')!='0' or body.get('stk_cd') not in (None,'')):return None
                            rows=response.get('oso')
                        if not isinstance(rows,list):return None
                        values.extend(rows);first=first or started;last=at
                    return first,last,values,c.get('next_key_present') is False,max(ident for ident,_ in selected)
                return latest('kt00007'), latest('ka10075')
            loaded = cached_captured_terminal_pages(
                db, self.scope, order['day'], _captured_at, _source_max_id, _load_terminal_pages
            )
            if loaded is None:
                return False
            detailed, pending = loaded
            if detailed is None or pending is None or pending[0]<detailed[1]:return False
            if not 0<=(source_clock-pending[1]).total_seconds()<=90:return False
            def number(value):
                if isinstance(value,str) and re.fullmatch(r'\+?[0-9]{1,20}',value):return int(value)
                return None
            original=order_key(order['ord_no'])
            def key(value):
                return str(int(value)) if isinstance(value,str) and re.fullmatch(r'[0-9]{1,7}',value) else None
            for item in pending[2]:
                if not isinstance(item,dict) or key(item.get('ord_no')) is None:return False
                if key(item.get('ord_no'))==original or key(item.get('orig_ord_no'))==original:
                    return False
            if any(not isinstance(r,dict) or key(r.get('ord_no')) is None for r in detailed[2]):return False
            rows=[r for r in detailed[2] if key(r['ord_no'])==original]
            if len(rows)!=1:return False
            row=rows[0]
            if (row.get('stk_cd') not in (order['symbol'],'A'+order['symbol'])
                    or row.get('io_tp_nm') not in (('현금매수','+매수') if order['side']=='buy' else ('현금매도','-매도'))
                    or row.get('dmst_stex_tp')!='KRX' or number(row.get('ord_qty'))!=order['quantity']
                    or number(row.get('cntr_qty'))!=order['filled_quantity']):return False
            natural_expiry = number(row.get('ord_remnq')) != 0
            if natural_expiry:
                if _rejected_auction_context is None:
                    return False
                basis = self._paper_rejected_closing_buy(db, order, row, detailed, pending, number, key)
                if not basis:
                    return False
                if db.execute("""SELECT 1 FROM execution_observations
                        WHERE scope=? AND mode='paper' AND day>=? AND ord_no=? LIMIT 1""",
                        (self.scope,order['day'],order['ord_no'])).fetchone():
                    return False
            # A later newly observed correction makes the terminal proof stale.
            changes=db.execute("""SELECT * FROM execution_observations WHERE scope=? AND mode='paper' AND day=?
                    AND ord_no=? AND observed_at>? ORDER BY id LIMIT 20001""",
                    (self.scope,order['day'],order['ord_no'],detailed[1].isoformat())).fetchall()
            if len(changes)>20000 or any(not self._verified_nonprojecting_repeat(db,r,now) for r in changes):return False
            late_rows = select_late_order_observations(
                db, self.scope, order['day'], detailed[1].isoformat(), natural_expiry, original
            )
            if late_rows is None:
                return False
            for late in late_rows:
                raw=late['payload_json']
                if not isinstance(raw,str) or hashlib.sha256(raw.encode()).hexdigest()!=late['digest']:return False
                event=json.loads(raw)
                values=([event.get('values',{})] if event.get('source_api')=='00' else
                        event.get('response_body',{}).get('acnt_ord_cntr_prst_array',[]))
                if not isinstance(values,list):return False
                for value in values:
                    if not isinstance(value,dict):return False
                    stream = event.get('source_api') == '00'
                    if not natural_expiry:
                        if key(value.get('9203') if stream else value.get('ord_no')) == original:
                            return False
                        continue
                    number_key = key(value.get('9203') if stream else value.get('ord_no'))
                    original_key = key(value.get('904') if stream else value.get('orig_ord_no'))
                    if original_key == original:
                        return False
                    if number_key != original:
                        continue
                    observed = datetime.fromisoformat(late['observed_at'])
                    if (original_key != '0' or observed.tzinfo is None or observed >= detailed[1]
                            or late['id'] > max(detailed[4],pending[4]) or late['day'] != order['day']
                            or event.get('source_api') != late['source_api']
                            or event.get('source_kind') != 'broker_observation' or event.get('mode') != 'paper'
                            or event.get('day') != late['day'] or event.get('observed_at') != late['observed_at']):
                        return False
                    if stream:
                        if (event.get('schema_version') != 1 or event.get('event_kind') != 'execution_stream'
                                or value.get('9001') not in (order['symbol'], 'A'+order['symbol'])
                                or value.get('905') != '+매수' or value.get('913') != '접수' or value.get('907') != '2'
                                or number(value.get('900')) != order['quantity']
                                or number(value.get('901')) != number(row.get('ord_uv'))
                                or number(value.get('902')) != order['quantity'] or number(value.get('903')) != 0
                                or any(value.get(field) not in (None, '') and number(value[field]) != 0
                                       for field in ('909', '910', '911', '914', '915'))):
                            return False
                    elif (event.get('status') != 'success'
                          or value.get('stk_cd') not in (order['symbol'], 'A'+order['symbol'])
                          or value.get('mdfy_cncl_tp') != '일반' or value.get('io_tp_nm') != '현금매수'
                          or number(value.get('cntr_qty')) != 0 or number(value.get('ord_qty')) != order['quantity']):
                        return False
            if natural_expiry:
                _rejected_auction_context.update(basis)
            return True
        except (KeyError,TypeError,ValueError,sqlite3.Error,OverflowError,ExecutionsError):
            return False

    def record_paper_expired_settlements(self, now):
        """Persist a newly collected terminal proof once, without new API reads.

        Historical quantity settlement is distinct from current account freshness.
        Every later read revalidates the immutable capture plus late corrections.
        """
        from . import expired_order_certificates
        with self._db() as db:
            if not db.in_transaction:
                # Only builds with the append index can warm it from a
                # committed snapshot before taking the writer reservation.
                sync_expiry_index = getattr(expired_order_certificates, '_sync_expiry_index', None)
                if sync_expiry_index is not None:
                    sync_expiry_index(db, self.scope)
                db.execute('BEGIN IMMEDIATE')
            return expired_order_certificates.capture_certificates(self, db, now)

    def _confirmed_expired_quantity(self, db, order, now):
        """Quantity no longer open, distinct from an explicit cancellation."""
        if order['mode']!='paper':
            return 0
        projected=dict(order)
        if 'filled_quantity' not in projected:
            projected['filled_quantity']=db.execute("SELECT COALESCE(SUM(quantity),0) FROM fills WHERE scope=? AND day=? AND mode=? AND ord_no=?",
                (self.scope,order['day'],order['mode'],order['ord_no'])).fetchone()[0]
        quantity=projected.get('quantity');filled=projected.get('filled_quantity')
        if type(quantity) is not int or type(filled) is not int or not 0<=filled<=quantity:return 0
        from .expired_order_certificates import paired_v2_captured, verified_certificate
        if not (verified_certificate(self,db,projected,now)
                or (not paired_v2_captured(db, projected) and self._paper_prior_order_terminal(db,projected,now))):return 0
        cancelled=self._confirmed_cancelled_quantity(db,order)
        if type(cancelled) is not int or not 0<=cancelled<=quantity-filled:return 0
        return quantity-filled-cancelled

    def _pending_order_rows(self, db, mode, symbol=None):
        """Keep every original quantity; only verified cancellation closes its remainder."""
        from .cancellation_settlement import TABLE
        # Completed historical orders are neither pending nor a cancellation
        # verification candidate. Bound the unresolved set, not lifetime volume.
        # Always revisit confirmed cancellations, even after a later fill makes
        # their original quantity match: that late change invalidates release.
        cancellation_candidate = ''
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)).fetchone():
            cancellation_candidate = f''' OR EXISTS (SELECT 1 FROM {TABLE} c
                WHERE c.scope=q.scope AND c.day=q.day AND c.mode=q.mode AND c.original_ord_no=q.ord_no)'''
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='sell_auction_cancellation_settlements'").fetchone():
            cancellation_candidate += ''' OR EXISTS (SELECT 1 FROM sell_auction_cancellation_settlements c
                WHERE c.scope=q.scope AND c.day=q.day AND c.mode=q.mode AND c.original_ord_no=q.ord_no)'''
        orders = db.execute('''SELECT q.* FROM (SELECT o.*, (SELECT COALESCE(SUM(f.quantity),0) FROM fills f
            WHERE f.scope=o.scope AND f.day=o.day AND f.mode=o.mode AND f.ord_no=o.ord_no) AS filled_quantity
            FROM orders o WHERE o.scope=? AND o.mode=? AND (? IS NULL OR o.symbol=?)) q
            WHERE typeof(q.quantity)!='integer' OR q.quantity<=0
            OR typeof(q.filled_quantity)!='integer' OR q.quantity != q.filled_quantity''' + cancellation_candidate +
            ' ORDER BY q.day,q.ord_no LIMIT 10001', (self.scope, mode, symbol, symbol)).fetchall()
        if len(orders) > 10000:
            raise ExecutionLogError('미결 주문 조회 범위를 확인하지 못했습니다.')
        pending = []
        for row in orders:
            if type(row['quantity']) is not int or row['quantity'] <= 0 or type(row['filled_quantity']) is not int:
                raise ExecutionLogError('미결 주문 수량을 확인하지 못했습니다.')
            cancelled = self._confirmed_cancelled_quantity(db, row)
            if row['quantity'] - cancelled != row['filled_quantity']:
                if not self._confirmed_expired_quantity(db,row,self._moment()):
                    pending.append(row)
        return pending

    @staticmethod
    def _confirmed_cancel_event(values, cancel_numbers, original_numbers):
        """Only a verified non-fill cash cancel child may be ignored for ownership."""
        return (isinstance(values, dict) and order_key(values.get('9203', '')) in cancel_numbers
            and order_key(values.get('904', '')) in original_numbers
            and ((values.get('905', '').lstrip('+-') == '매수취소')
                 or (values.get('905', '').lstrip('+-') == '매도취소' and values.get('907')=='1'))
            and values.get('913') in ('접수', '확인', '취소')
            and values.get('909') in (None, '', '0', '0000000')
            and all(value in (None, '') or isinstance(value,str) and re.fullmatch(r'0{1,12}',value) is not None
                    for value in (values.get(key) for key in ('903','910','911','914','915'))))

    def symbol_order_watermark(self, mode, symbol):
        """Read only; never creates or repairs an operational ledger."""
        if self.path.is_symlink() or not self.path.is_file():
            raise ExecutionLogError('종목별 주문 원장을 확인하지 못했습니다.')
        try:
            before=self.path.stat()
            with sqlite3.connect(self.path.resolve().as_uri()+'?mode=ro',uri=True,timeout=_SQLITE_TIMEOUT_SECONDS) as db:
                db.row_factory=sqlite3.Row;install_evidence_view(db);db.execute('PRAGMA query_only=ON');db.execute('BEGIN')
                result=self._symbol_order_watermark(db,mode,symbol)
                after=self.path.stat()
                if (before.st_dev,before.st_ino)!=(after.st_dev,after.st_ino):
                    raise ExecutionLogError('종목별 주문 원장이 변경되었습니다.')
                return result
        except (sqlite3.Error,OSError,ValueError,TypeError,ExecutionLogError):
            raise ExecutionLogError('종목별 주문 원장을 확인하지 못했습니다.') from None

    def _paper_owned_after_gap(self, db, symbol, now, gap_at, quantity, basis):
        """Fresh complete broker reads may validate a previously proven lot.

        Only gaps after the last proven unit fill are considered. Any external
        order, conflicting fill, missing page or later 00 row keeps it blocked.
        This does not manufacture trade units or repair historical coverage.
        """
        try:
            day=now.astimezone(KST).strftime('%Y%m%d')
            balance=db.execute("SELECT * FROM account_balance_daily WHERE scope=? AND mode='paper' AND day=?",(self.scope,day)).fetchone()
            if balance is None or balance['source_kind']!='broker_observation':return False
            account_at=datetime.fromisoformat(balance['observed_at']);account=json.loads(balance['payload_json'])
            if (account_at.tzinfo is None or not gap_at<account_at<=now or (now-account_at).total_seconds()>90
                    or account.get('schema')!='paper-account-snapshot-v1' or account.get('mode')!='paper'
                    or account.get('exchange')!='KRX' or not isinstance(account.get('holdings'),list)):return False
            positions=[r for r in account['holdings'] if isinstance(r,dict) and r.get('stk_cd') in (symbol,'A'+symbol)]
            if len(positions)!=1:return False
            row=positions[0]
            if row.get('crd_tp')!='00':return False
            held=_observed_number(row.get('rmnd_qty'),integer=True)
            principal=_observed_number(row.get('pur_amt'),integer=True)
            if held!=quantity or basis is not None and principal!=basis:return False
            entries=db.execute("SELECT id,source_api,digest,payload_json FROM broker_observation_evidence WHERE scope=? AND mode='paper' AND day=? AND source_api IN ('00','kt00007','ka10075') ORDER BY id LIMIT 20001",(self.scope,day)).fetchall()
            if len(entries)>20000:return False
            groups={};stream=[]
            for entry in entries:
                raw=entry['payload_json']
                if not isinstance(raw,str) or hashlib.sha256(raw.encode()).hexdigest()!=entry['digest']:return False
                event=json.loads(raw)
                if event.get('mode')!='paper' or event.get('day')!=day or event.get('source_kind')!='broker_observation':return False
                if entry['source_api']=='00':stream.append(event)
                else:groups.setdefault((entry['source_api'],event.get('query_id')),[]).append((entry['id'],event))
            def latest(api):
                choices=[items for (kind,_),items in groups.items() if kind==api]
                if not choices:return None
                pages=sorted((e for _,e in max(choices,key=lambda xs:max(x[0] for x in xs))),key=lambda e:e.get('page',0))
                rows=[];first=last=None
                for i,e in enumerate(pages):
                    c=e['continuation'];b=e['request_body'];r=e['response_body']
                    start=datetime.fromisoformat(e['request_started_at']);end=datetime.fromisoformat(e['observed_at'])
                    if (e['page']!=i+1 or e['status']!='success' or start.tzinfo is None or end.tzinfo is None
                            or not gap_at<start<=end<=now or last is not None and start<last
                            or r.get('return_code') not in (0,'0') or isinstance(r.get('return_code'),bool)
                            or c.get('request_cont_yn')!=('N' if i==0 else 'Y')
                            or i<len(pages)-1 and (c.get('response_cont_yn')!='Y' or c.get('next_key_present') is not True)
                            or i==len(pages)-1 and c.get('response_cont_yn') not in (None,'','N')):return None
                    if api=='kt00007':
                        if (b.get('ord_dt')!=day or b.get('qry_tp') not in ('1','2') or b.get('stk_bond_tp') not in ('0','1')
                                or b.get('sell_tp')!='0' or b.get('stk_cd') not in (None,'')
                                or b.get('fr_ord_no') not in (None,'') or b.get('dmst_stex_tp')!='%'):return None
                        items=r.get('acnt_ord_cntr_prps_dtl')
                    else:
                        if (b.get('all_stk_tp')!='0' or b.get('trde_tp')!='0' or b.get('stex_tp')!='0'
                                or b.get('stk_cd') not in (None,'')):return None
                        items=r.get('oso')
                    if not isinstance(items,list) or any(not isinstance(x,dict) for x in items):return None
                    rows.extend(items);first=first or start;last=end
                return first,last,rows
            detail=latest('kt00007');pending=latest('ka10075')
            if detail is None or pending is None or not detail[1]<=account_at<=pending[0] or (now-pending[1]).total_seconds()>90:return False
            for item in pending[2]:
                if item.get('stk_cd') in (symbol,'A'+symbol):
                    unresolved=_observed_number(item.get('oso_qty'),integer=True)
                    if unresolved is None or unresolved!=0:return False
            saved={r['ord_no']:r for r in db.execute("SELECT * FROM orders WHERE scope=? AND mode='paper' AND day=? AND symbol=?",(self.scope,day,symbol))}
            seen=set()
            for item in detail[2]:
                if item.get('stk_cd') not in (symbol,'A'+symbol):continue
                number=order_key(item.get('ord_no',''))
                if number in seen or number not in saved:return False
                seen.add(number);order=saved[number]
                projected=db.execute("SELECT COALESCE(SUM(quantity),0) FROM fills WHERE scope=? AND mode='paper' AND day=? AND ord_no=?",(self.scope,day,number)).fetchone()[0]
                if (_observed_number(item.get('ord_qty'),integer=True)!=order['quantity']
                        or _observed_number(item.get('cntr_qty'),integer=True)!=projected
                        or _observed_number(item.get('ord_remnq'),integer=True)!=0
                        or item.get('io_tp_nm') not in (('현금매수','+매수') if order['side']=='buy' else ('현금매도','-매도'))):return False
            if seen!=set(saved):return False
            lifecycle=[]
            for e in stream:
                at=datetime.fromisoformat(e['observed_at']);kind=e.get('event_kind')
                if at.tzinfo is None or at>now:return False
                if kind in ('stream_started','stream_resumed','stream_gap','stream_stopped'):lifecycle.append((at,kind))
                values=e.get('values',{})
                if kind=='execution_stream' and isinstance(values,dict) and values.get('9001') in (symbol,'A'+symbol) and at>=detail[0]:return False
            before=[item for item in lifecycle if item[0]<=detail[0]]
            if not before or max(before)[1] not in ('stream_started','stream_resumed'):return False
            if any(kind in ('stream_gap','stream_stopped') and at>=detail[0] for at,kind in lifecycle):return False
            return True
        except (sqlite3.Error,ValueError,TypeError,KeyError,OverflowError,ExecutionsError):
            return False

    def paper_exit_only_position(self, mode, symbol, strategy, now, *, _ignored_intent_id=None):
        """Prove only protective liquidation; strict ownership/entry is unchanged."""
        from .broker_exit_only import broker_confirmed_position
        failure={'status':'unsupported','ownership_status':'unsupported','owned_qty':None,
                 'broker_quantity':None,'profit_attribution':'unconfirmed',
                 'reason':'exit_only_evidence_unavailable'}
        if mode!='paper' or self.path.is_symlink() or not self.path.is_file():return failure
        try:
            with sqlite3.connect(self.path.resolve().as_uri()+'?mode=ro',uri=True,timeout=_SQLITE_TIMEOUT_SECONDS) as db:
                db.row_factory=sqlite3.Row;install_evidence_view(db)
                db.execute('PRAGMA query_only=ON');db.execute('BEGIN')
                return broker_confirmed_position(self,db,symbol,strategy,now,ignored_intent_id=_ignored_intent_id)
        except (sqlite3.Error,ExecutionLogError,ValueError,TypeError,KeyError,OverflowError,OSError):
            return failure

    def _assert_exit_only_proof(self, db, proof, now, *, ignored_intent_id=None):
        from .broker_exit_only import broker_confirmed_position
        if (proof.get('schema_version')!=7 or proof.get('ownership_status')!='exit_only_broker_confirmed'
                or proof.get('profit_attribution')!='unconfirmed' or proof.get('exit_kind')!='full'):
            raise ExecutionLogError('청산 전용 브로커 증빙 범위가 다릅니다.')
        position=broker_confirmed_position(self,db,proof['symbol'],proof['strategy'],now,
                                          ignored_intent_id=ignored_intent_id)
        if (position.get('status')!='exit_only_broker_confirmed'
                or position.get('evidence')!=proof.get('exit_only_broker_proof')
                or position.get('generation_id')!=proof.get('generation_id')
                or position.get('owned_qty')!=proof.get('pre_order_held_qty')
                or position.get('basis_krw')!=proof.get('pre_order_pur_amt_krw')
                or position.get('owned_qty')!=proof.get('order_quantity')):
            raise ExecutionLogError('청산 전용 브로커 수량·세대·미결 증빙이 변경되었습니다.')
        return position

    def multi_strategy_position(self, mode, symbol, strategy, now, *, _ignored_intent_id=None):
        """Preliminary multi-day ownership; never proves current broker holdings.

        A caller must independently reconcile the live broker account and
        reserve and revalidate any exit before sending. Unknown, mixed,
        incomplete or mixed positions are deliberately unsupported.
        """
        def outcome(status, **fields):
            return dict(status=status, generation_id=None, owned_qty=None, basis_krw=None,
                        initial_fill_at=None, first_fill_at=None, first_fill_observed_at=None,
                        first_fill_timing_verified=False, half_take_done=None, **fields)
        if (mode != 'paper' or not isinstance(symbol,str) or re.fullmatch(r'[0-9]{6}',symbol) is None
                or not isinstance(strategy,str) or not strategy.strip() or not isinstance(now,datetime)
                or now.tzinfo is None or now.utcoffset() is None or self.path.is_symlink() or not self.path.is_file()):
            return outcome('unsupported')
        try:
            day=now.astimezone(KST).strftime('%Y%m%d')
            with sqlite3.connect(self.path.resolve().as_uri()+'?mode=ro',uri=True,timeout=_SQLITE_TIMEOUT_SECONDS) as db:
                db.row_factory=sqlite3.Row;install_evidence_view(db);db.execute('PRAGMA query_only=ON');db.execute('BEGIN')
                self._validate_daily_ledger(db)
                if _ignored_intent_id is None:
                    try:self._assert_no_pending_order(db,mode,symbol)
                    except ExecutionLogError:return outcome('pending')
                else:
                    own=db.execute('SELECT * FROM intents WHERE scope=? AND id=?',(self.scope,_ignored_intent_id)).fetchone()
                    if (own is None or own['mode']!=mode or own['symbol']!=symbol or own['strategy']!=strategy
                            or own['side'] not in ('buy','sell') or own['state']!='prepared' or own['order_number'] is not None):return outcome('unsupported')
                    unresolved=db.execute("""SELECT 1 FROM intents i WHERE i.scope=? AND i.mode=? AND i.symbol=? AND i.id!=?
                        AND i.state IS NOT 'cancelled' AND (i.state IS NOT 'accepted' OR i.order_number IS NULL OR NOT EXISTS
                        (SELECT 1 FROM orders o WHERE o.scope=i.scope AND o.day=i.day AND o.mode=i.mode AND o.ord_no=i.order_number)) LIMIT 1""",
                        (self.scope,mode,symbol,_ignored_intent_id)).fetchone()
                    pending=self._pending_order_rows(db,mode,symbol)
                    if unresolved or pending:return outcome('pending')
                rows=db.execute("""SELECT * FROM intents WHERE scope=? AND mode=? AND symbol=? AND day<=?
                    AND state!='cancelled' AND (? IS NULL OR id!=?) ORDER BY day,created_at,id LIMIT 10001""",(self.scope,mode,symbol,day,_ignored_intent_id,_ignored_intent_id)).fetchall()
                if not rows:
                    # An earlier open lot cannot become flat merely because
                    # the calendar changed. REST quantities are used only to
                    # reject here; they do not establish ownership.
                    old=db.execute("""SELECT side,quantity FROM fills WHERE scope=? AND mode=? AND symbol=? AND day<? LIMIT 10001""",
                                   (self.scope,mode,symbol,day)).fetchall()
                    if len(old)>10000 or any(r['side'] not in ('buy','sell') or type(r['quantity']) is not int or r['quantity']<=0 for r in old):return outcome('unsupported')
                    if sum(r['quantity']*(1 if r['side']=='buy' else -1) for r in old)!=0:return outcome('unsupported')
                    holding=db.execute('SELECT positions FROM holdings WHERE scope=? AND mode=?',(self.scope,mode)).fetchone()
                    if holding is not None:
                        positions=json.loads(holding['positions'])
                        if not isinstance(positions,dict) or type(positions.get(symbol,0)) is not int or positions.get(symbol,0)!=0:return outcome('unsupported')
                    return outcome('flat')
                if len(rows)>10000 or any(r['state']!='accepted' for r in rows):return outcome('unsupported')
                # A zero-fill confirmed cancellation closes a reservation,
                # not a position. Keep its accepted order in the financial
                # ledger/daily entry count, but do not invent an ownership lot.
                effective_rows=[]
                for intent in rows:
                    original=db.execute('SELECT * FROM orders WHERE scope=? AND day=? AND mode=? AND ord_no=?',
                        (self.scope,intent['day'],mode,intent['order_number'])).fetchone()
                    if (original is None or original['symbol']!=symbol or original['side']!=intent['side']
                            or original['quantity']!=intent['quantity'] or original['strategy']!=intent['strategy']):
                        return outcome('unsupported')
                    cancelled=self._confirmed_cancelled_quantity(db,original)
                    expired=self._confirmed_expired_quantity(db,original,now)
                    if cancelled + expired != original['quantity']:
                        effective_rows.append(intent)
                rows=effective_rows
                if not rows:
                    return outcome('flat_candidate',candidate_only=True,requires_fresh_broker_reconciliation=True)
                if all(r['day']<day for r in rows):
                    from .flat_epoch_reanchor import verified_prior_closed_flat
                    anchor=verified_prior_closed_flat(self,db,symbol,now,ignored_intent_id=_ignored_intent_id)
                    if anchor is not None:
                        return outcome('flat_candidate',candidate_only=True,requires_fresh_broker_reconciliation=True,
                            flat_epoch_reason='prior_closed_cycle_current_broker_flat',flat_epoch_evidence=anchor)
                # Only a first intent atomically bound to a new broker-zero
                # proof can delimit an unknown historical ownership interval.
                # Old records and their unreconciled profit remain untouched.
                epoch_index=None
                for i,intent in enumerate(rows):
                    if intent['side']!='buy':continue
                    rec=db.execute('SELECT * FROM pre_order_reconciliations WHERE scope=? AND intent_id=?',
                                   (self.scope,intent['id'])).fetchone()
                    if rec is None:continue
                    raw=rec['evidence_json']
                    if not isinstance(raw,str) or hashlib.sha256(raw.encode()).hexdigest()!=rec['evidence_sha256']:return outcome('unsupported')
                    candidate=json.loads(raw)
                    if candidate.get('schema_version') not in (5,6):continue
                    created=datetime.fromisoformat(intent['created_at'])
                    completed=datetime.fromisoformat(candidate['reconciliation_completed_at'])
                    if (candidate.get('symbol')!=symbol or created.tzinfo is None or completed.tzinfo is None
                            or not completed<created<=now):return outcome('unsupported')
                    self._assert_epoch_proof(db,candidate,created)
                    epoch_index=i
                if epoch_index is not None:
                    rows=rows[epoch_index:]
                first=rows[0]
                if first['side']!='buy':return outcome('unsupported')
                reconciliation=db.execute('SELECT * FROM pre_order_reconciliations WHERE scope=? AND intent_id=?',
                                          (self.scope,first['id'])).fetchone()
                if reconciliation is None:
                    return outcome('flat_candidate',candidate_only=True,requires_fresh_broker_reconciliation=True)
                raw=reconciliation['evidence_json']
                if not isinstance(raw,str) or hashlib.sha256(raw.encode('utf-8')).hexdigest()!=reconciliation['evidence_sha256']:return outcome('unsupported')
                proof=json.loads(raw)
                if (proof.get('symbol')!=symbol or proof.get('pre_order_held_qty')!=0 or proof.get('pre_order_pur_amt_krw')!=0
                        or proof.get('holdings_source')!='broker_observation'):return outcome('unsupported')
                holdings_at=datetime.fromisoformat(proof['observed_at'])
                if holdings_at.tzinfo is None or holdings_at.astimezone(KST).strftime('%Y%m%d')!=first['day']:return outcome('unsupported')
                orders=[]
                for intent in rows:
                    order=db.execute('SELECT * FROM orders WHERE scope=? AND day=? AND mode=? AND ord_no=?',
                        (self.scope,intent['day'],mode,intent['order_number'])).fetchone()
                    if (order is None or order['symbol']!=symbol or order['side']!=intent['side']
                            or order['quantity']!=intent['quantity'] or order['strategy']!=intent['strategy']):return outcome('unsupported')
                    orders.append(dict(order))
                start=datetime.fromisoformat(first['created_at']);first_registered=datetime.fromisoformat(orders[0]['registered_at'])
                if start.tzinfo is None or first_registered.tzinfo is None or not holdings_at<=start<=first_registered<=now:return outcome('unsupported')
                details=[]; overnight_unobserved=False; recovery_gap=None
                for order_day in sorted({o['day'] for o in orders}):
                    if re.fullmatch(r'[0-9]{8}',order_day or '') is None:return outcome('unsupported')
                    day_orders=[o for o in orders if o['day']==order_day]
                    day_intents=[i for i in rows if i['day']==order_day]
                    order_numbers={o['ord_no'] for o in day_orders}
                    from .cancellation_settlement import confirmed_cancel_order_numbers
                    cancel_numbers=confirmed_cancel_order_numbers(db,self.scope,order_day,mode,symbol)
                    from .unfilled_sell_auction import confirmed_sell_cancel_order_numbers
                    cancel_numbers |= confirmed_sell_cancel_order_numbers(db,self.scope,order_day,mode,symbol)
                    zero_cancelled={r['ord_no'] for r in db.execute('SELECT * FROM orders WHERE scope=? AND day=? AND mode=? AND symbol=?',
                        (self.scope,order_day,mode,symbol)) if (self._confirmed_cancelled_quantity(db,r)+self._confirmed_expired_quantity(db,r,now))==r['quantity']}
                    known_numbers=order_numbers|zero_cancelled
                    day_fills=[dict(r) for r in db.execute('SELECT * FROM fills WHERE scope=? AND day=? AND mode=? AND symbol=?',
                                                           (self.scope,order_day,mode,symbol))]
                    if epoch_index is not None:
                        # Earlier projected fills are not evidence for the new
                        # lot. Any newly observed change to them still blocks.
                        for old in db.execute("SELECT e.* FROM execution_observations e JOIN orders o ON o.scope=e.scope AND o.day=e.day AND o.mode=e.mode AND o.ord_no=e.ord_no WHERE e.scope=? AND e.day=? AND e.mode=? AND o.symbol=? AND e.ord_no NOT IN (%s)" % ','.join('?' for _ in order_numbers),
                                              (self.scope,order_day,mode,symbol,*order_numbers)):
                            at=datetime.fromisoformat(old['observed_at'])
                            if at.tzinfo is None or at>=holdings_at and not self._verified_nonprojecting_repeat(db,old,now):return outcome('unsupported')
                        day_fills=[r for r in day_fills if r['ord_no'] in order_numbers]
                    elif any(r['ord_no'] not in order_numbers for r in day_fills):return outcome('unsupported')
                    obs=db.execute("SELECT id,digest,payload_json FROM broker_observation_evidence WHERE scope=? AND day=? AND mode=? AND source_api='00' ORDER BY id LIMIT 20001",(self.scope,order_day,mode)).fetchall()
                    if len(obs)>20000:return outcome('unsupported')
                    parsed=[];stream_events=[];last_order_event=None
                    first_at=min(datetime.fromisoformat(o['registered_at']) for o in day_orders)
                    first_created=min(datetime.fromisoformat(i['created_at']) for i in day_intents)
                    start_at=holdings_at if order_day==first['day'] else first_created
                    if first_at.tzinfo is None or first_created.tzinfo is None or not start_at<=first_created<=first_at<=now:return outcome('unsupported')
                    for row in obs:
                        raw=row['payload_json']
                        if not isinstance(raw,str) or hashlib.sha256(raw.encode('utf-8')).hexdigest()!=row['digest']:return outcome('unsupported')
                        event=json.loads(raw); at=datetime.fromisoformat(event['observed_at'])
                        if at.tzinfo is None or at>now or at.astimezone(KST).strftime('%Y%m%d')!=order_day or event.get('mode')!=mode or event.get('day')!=order_day:return outcome('unsupported')
                        kind=event.get('event_kind');values=event.get('values',{})
                        if kind in ('stream_started','stream_resumed','stream_gap','stream_stopped'):stream_events.append((at,row['id'],kind))
                        if kind=='execution_stream' and isinstance(values,dict) and values.get('9001') in (symbol,'A'+symbol) and at>=start_at:
                            if (order_key(values.get('9203','')) not in known_numbers
                                    and not self._confirmed_cancel_event(values,cancel_numbers,known_numbers)):
                                return outcome('unsupported')
                            if values.get('913')=='체결':last_order_event=at if last_order_event is None else max(at,last_order_event)
                        parsed.append(dict(row))
                    prior=[entry for entry in stream_events if entry[0]<=start_at]
                    if not prior or max(prior)[2] not in ('stream_started','stream_resumed') or last_order_event is None:return outcome('unsupported')
                    deadline=now if order_day==day else last_order_event
                    gaps=[at for at,_,kind in stream_events if kind in ('stream_gap','stream_stopped') and start_at<=at<=deadline]
                    if gaps:
                        if last_order_event is None or any(at<=last_order_event for at in gaps):
                            return outcome('unsupported')
                        recovery_gap=max(gaps) if recovery_gap is None else max(recovery_gap,max(gaps))
                    if order_day!=day:overnight_unobserved=True
                    details.extend(_execution_accounting(order_day,day_orders,day_fills,parsed)['orders'])
                for d,original in zip(details,orders):
                    expected=original['quantity']-self._confirmed_cancelled_quantity(db,original)-self._confirmed_expired_quantity(db,original,now)
                    if (d['verified_amount_source']!='00_unit_sum_reconciled_to_cumulative_quantity'
                            or d['unit_coverage']!='quantity_reconciled' or d['stream_gap_observed'] or d['issues']
                            or d['verified_quantity']!=expected or d['rest_projection_quantity']!=expected):return outcome('unsupported')
                if day not in {o['day'] for o in orders}:
                    current=db.execute("SELECT id,digest,payload_json FROM broker_observation_evidence WHERE scope=? AND day=? AND mode=? AND source_api='00' ORDER BY id LIMIT 20001",(self.scope,day,mode)).fetchall()
                    if len(current)>20000:return outcome('unsupported')
                    for row in current:
                        raw=row['payload_json']
                        if not isinstance(raw,str) or hashlib.sha256(raw.encode('utf-8')).hexdigest()!=row['digest']:return outcome('unsupported')
                        event=json.loads(raw);at=datetime.fromisoformat(event['observed_at'])
                        if at.tzinfo is None or at>now or event.get('day')!=day or event.get('mode')!=mode:return outcome('unsupported')
                        values=event.get('values',{})
                        if event.get('event_kind')=='execution_stream' and isinstance(values,dict) and values.get('9001') in (symbol,'A'+symbol):return outcome('unsupported')
                remaining=0;basis=0;owner=None;generation=None;initial_at=None;first_fill_timing=None;half=False;last_fill_at=None
                for intent,detail in zip(rows,details):
                    amount=_observed_number(detail['verified_amount'],integer=True)
                    latest=detail.get('latest_cumulative')
                    if amount is None or amount<=0 or not isinstance(latest,dict):return outcome('unsupported')
                    qty=detail['verified_quantity']
                    if intent['side']=='buy':
                        rec=db.execute('SELECT * FROM pre_order_reconciliations WHERE scope=? AND intent_id=?',(self.scope,intent['id'])).fetchone()
                        if rec is None:return outcome('unsupported')
                        raw=rec['evidence_json']
                        if not isinstance(raw,str) or hashlib.sha256(raw.encode('utf-8')).hexdigest()!=rec['evidence_sha256']:return outcome('unsupported')
                        buyproof=json.loads(raw)
                        buy_observed=datetime.fromisoformat(buyproof['observed_at']);created=datetime.fromisoformat(intent['created_at'])
                        if (buyproof.get('symbol')!=symbol or buyproof.get('holdings_source')!='broker_observation'
                                or buy_observed.tzinfo is None or created.tzinfo is None or buy_observed>created
                                or last_fill_at is not None and buy_observed<last_fill_at
                                or buyproof.get('pre_order_held_qty')!=remaining
                                or basis is not None and buyproof.get('pre_order_pur_amt_krw')!=basis):return outcome('unsupported')
                        if remaining==0:
                            if buyproof.get('pre_order_pur_amt_krw')!=0:return outcome('unsupported')
                            owner=intent['strategy'];generation=intent['id'];initial_at=latest['observed_at'];half=False;basis=0
                            first_fill_timing=_verified_first_fill_timing(intent['day'],detail,intent['created_at'],now)
                            if owner=='S5_steady_uptrend_vwap' and first_fill_timing is None:return outcome('unsupported')
                        elif intent['strategy']!=owner:return outcome('unsupported')
                        remaining+=qty
                        basis=None if basis is None else basis+int(amount)
                    elif intent['side']=='sell':
                        if (remaining<qty or intent['strategy']!=owner and not (owner=='gdcrs' and intent['strategy']=='ddcrs')):return outcome('unsupported')
                        remaining-=qty;basis=None
                        if remaining:half=True
                        else:owner=None;generation=None;initial_at=None;first_fill_timing=None;half=False;basis=0
                    else:return outcome('unsupported')
                    last_fill_at=datetime.fromisoformat(latest['observed_at'])
                    if last_fill_at.tzinfo is None or last_fill_at>now:return outcome('unsupported')
                if remaining==0:return outcome('flat')
                if recovery_gap is not None and not self._paper_owned_after_gap(db,symbol,now,recovery_gap,remaining,basis):
                    return outcome('unsupported')
                if strategy!=owner and not (owner=='gdcrs' and strategy=='ddcrs'):return outcome('unsupported')
                return {'status':'owned','generation_id':generation,'owned_qty':remaining,'basis_krw':basis,
                        'initial_fill_at':initial_at,'half_take_done':half,
                        **(first_fill_timing or {'first_fill_at':None,'first_fill_observed_at':None,'first_fill_timing_verified':False}),
                        'overnight_unobserved_interval':overnight_unobserved,
                        'requires_fresh_broker_reconciliation':True,
                        'symbol_ledger_sha256':self._symbol_order_watermark(db,mode,symbol,ignored_intent_id=_ignored_intent_id)}
        except (sqlite3.Error,ExecutionLogError,ExecutionsError,ValueError,KeyError,TypeError,OverflowError,OSError):
            return outcome('unsupported')

    def multi_strategy_open_symbols(self, mode, now):
        """Conservative monitoring denominator, never an ownership authorization."""
        failed={'status':'unsupported','symbols':[],'holdings_observed_at':None}
        if (mode!='paper' or not isinstance(now,datetime) or now.tzinfo is None or now.utcoffset() is None
                or self.path.is_symlink() or not self.path.is_file()):return failed
        try:
            day=now.astimezone(KST).strftime('%Y%m%d')
            with sqlite3.connect(self.path.resolve().as_uri()+'?mode=ro',uri=True,timeout=_SQLITE_TIMEOUT_SECONDS) as db:
                db.row_factory=sqlite3.Row;install_evidence_view(db);db.execute('PRAGMA query_only=ON');db.execute('BEGIN')
                self._validate_daily_ledger(db)
                holding=db.execute('SELECT observed_at,positions,discrepancy FROM holdings WHERE scope=? AND mode=?',(self.scope,mode)).fetchone()
                if holding is None:return failed
                observed=datetime.fromisoformat(holding['observed_at']); positions=json.loads(holding['positions']); discrepancies=json.loads(holding['discrepancy'])
                if (observed.tzinfo is None or observed>now or not isinstance(positions,dict) or not isinstance(discrepancies,list)):
                    return failed
                symbols=set()
                for symbol,qty in positions.items():
                    if not isinstance(symbol,str) or re.fullmatch(r'[0-9]{6}',symbol) is None or type(qty) is not int or qty<0:return failed
                    if qty:symbols.add(symbol)
                for symbol in discrepancies:
                    if not isinstance(symbol,str) or re.fullmatch(r'[0-9]{6}',symbol) is None:return failed
                    symbols.add(symbol)
                candidates=db.execute("""SELECT DISTINCT i.symbol FROM intents i WHERE i.scope=? AND i.mode=? AND
                    (i.day=? OR (i.state IS NOT 'cancelled' AND (i.state IS NOT 'accepted' OR i.order_number IS NULL OR NOT EXISTS
                        (SELECT 1 FROM orders o WHERE o.scope=i.scope AND o.day=i.day AND o.mode=i.mode AND o.ord_no=i.order_number)))) LIMIT 10001""",(self.scope,mode,day)).fetchall()
                pending=self._pending_order_rows(db,mode)
                if len(candidates)>10000 or len(pending)>10000:return failed
                for row in (*candidates,*pending):
                    code=row['symbol']
                    if not isinstance(code,str) or re.fullmatch(r'[0-9]{6}',code) is None:return failed
                    symbols.add(code)
                return {'status':'available','symbols':sorted(symbols),'holdings_observed_at':holding['observed_at']}
        except (sqlite3.Error,ExecutionLogError,ValueError,KeyError,TypeError,OverflowError,OSError):return failed

    def record_strategy_holding_observation(self, symbol, held_qty, sellable_qty, purchase_amount, observed_at, *, source_kind):
        """Persist a bounded broker holding projection; never infer a fill."""
        if (source_kind!='broker_observation' or not isinstance(symbol,str) or re.fullmatch(r'[0-9]{6}',symbol) is None
                or any(type(x) is not int or x<0 for x in (held_qty,sellable_qty,purchase_amount))
                or sellable_qty>held_qty or (held_qty==0 and purchase_amount!=0) or (held_qty>0 and purchase_amount<=0)
                or not isinstance(observed_at,datetime) or observed_at.tzinfo is None or observed_at.utcoffset() is None):
            raise ExecutionLogError('증권사 잔고 관측 근거를 확인하지 못했습니다.')
        observed_at=observed_at.astimezone(KST);ident=uuid.uuid4().hex
        payload={'id':ident,'day':observed_at.strftime('%Y%m%d'),'mode':'paper','symbol':symbol,
                 'observed_at':observed_at.isoformat(),'source_kind':source_kind,'held_qty':held_qty,
                 'sellable_qty':sellable_qty,'purchase_amount':purchase_amount}
        digest=hashlib.sha256(canonical_json(payload).encode('utf-8')).hexdigest()
        with self._db(require_existing_daily_ledger=True) as db:
            self._validate_strategy_holding_schema(db)
            db.execute('INSERT INTO strategy_holding_observations VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                       (self.scope,ident,payload['day'],'paper',symbol,payload['observed_at'],source_kind,
                        held_qty,sellable_qty,purchase_amount,digest))
        return ident

    @staticmethod
    def _validate_strategy_holding_schema(db):
        cols=tuple(r[1] for r in db.execute('PRAGMA table_info(strategy_holding_observations)'))
        indexes=set()
        for row in db.execute('PRAGMA index_list(strategy_holding_observations)'):
            if row[2]:indexes.add(tuple(x[2] for x in db.execute('SELECT seqno,cid,name FROM pragma_index_info(?) ORDER BY seqno',(row[1],))))
        triggers={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='strategy_holding_observations'")}
        if (cols!=('scope','id','day','mode','symbol','observed_at','source_kind','held_qty','sellable_qty','purchase_amount','digest')
                or ('scope','id') not in indexes
                or not {'strategy_holding_observations_no_update','strategy_holding_observations_no_delete'}<=triggers):
            raise ExecutionLogError('전략 잔고 관측 원장 구조를 확인하지 못했습니다.')

    def _assert_strategy_holding_evidence(self,db,proof):
        self._validate_strategy_holding_schema(db)
        row=db.execute('SELECT * FROM strategy_holding_observations WHERE scope=? AND id=?',
                       (self.scope,proof['holding_observation_id'])).fetchone()
        if row is None:raise ExecutionLogError('저장된 브로커 잔고 관측이 없습니다.')
        fields=('id','day','mode','symbol','observed_at','source_kind','held_qty','sellable_qty','purchase_amount')
        payload={k:row[k] for k in fields}
        digest=hashlib.sha256(canonical_json(payload).encode('utf-8')).hexdigest()
        if (digest!=row['digest'] or row['mode']!='paper' or row['symbol']!=proof['symbol']
                or row['source_kind']!='broker_observation' or row['observed_at']!=proof['observed_at']
                or row['held_qty']!=proof['pre_order_held_qty'] or row['purchase_amount']!=proof['pre_order_pur_amt_krw']
                or proof['schema_version'] in (4,7) and row['sellable_qty']<proof['order_quantity']):
            raise ExecutionLogError('저장된 브로커 잔고 관측과 매도 증빙이 다릅니다.')

    def stream_anchor(self, observed_at):
        if not isinstance(observed_at,datetime) or observed_at.tzinfo is None or self.path.is_symlink() or not self.path.is_file():
            raise ExecutionLogError('실시간 체결 기준을 확인하지 못했습니다.')
        with sqlite3.connect(self.path.resolve().as_uri()+'?mode=ro',uri=True,timeout=_SQLITE_TIMEOUT_SECONDS) as db:
            db.row_factory=sqlite3.Row;install_evidence_view(db);db.execute('PRAGMA query_only=ON')
            day=observed_at.astimezone(KST).strftime('%Y%m%d')
            rows=db.execute("SELECT id,digest,payload_json FROM broker_observation_evidence WHERE scope=? AND day=? AND mode='paper' AND source_api='00' ORDER BY id LIMIT 20001",(self.scope,day)).fetchall()
            if len(rows)>20000:raise ExecutionLogError('실시간 체결 기준을 확인하지 못했습니다.')
            events=[]
            for row in rows:
                raw=row['payload_json']
                if not isinstance(raw,str) or hashlib.sha256(raw.encode()).hexdigest()!=row['digest']:raise ExecutionLogError('실시간 체결 근거가 손상되었습니다.')
                event=json.loads(raw);at=datetime.fromisoformat(event['observed_at'])
                if at.tzinfo is None:raise ExecutionLogError('실시간 체결 시각이 불명확합니다.')
                if at<=observed_at and event.get('event_kind') in ('stream_started','stream_resumed','stream_gap','stream_stopped'):
                    events.append((at,row['id'],event['event_kind'],row['digest']))
            if not events or max(events)[2] not in ('stream_started','stream_resumed'):
                raise ExecutionLogError('실시간 체결 수신이 시작되지 않았습니다.')
            at,ident,_,digest=max(events)
            return ident,digest,at.isoformat()

    def _assert_epoch_proof(self, db, proof, now):
        """Check a new zero-position anchor against saved broker and 00 evidence."""
        try:
            from kiwoom_autotrader.trading.paper_reconciliation import _assert_no_broker_pending
            observed=datetime.fromisoformat(proof['observed_at'])
            completed=datetime.fromisoformat(proof['reconciliation_completed_at'])
            version=proof['schema_version']
            if (version not in (5,6)
                    or version==5 and proof['integrated_pending_clear'] is not True
                    or version==6 and (not effective_paper_experiment(self.settings_loader(),'paper')['enabled']
                        or proof['integrated_pending_clear'] is not False or proof.get('symbol_pending_clear') is not True)
                    or proof['pre_order_held_qty']!=0 or proof['pre_order_pur_amt_krw']!=0
                    or not isinstance(proof['pending_query_id'],str)
                    or re.fullmatch(r'[0-9a-f]{32}',proof['pending_query_id']) is None
                    or any(type(proof[k]) is not int or proof[k]<0 for k in
                           ('recorder_gap_count','recorder_observations','recorder_ingress_sequence','engine_ingress_sequence'))
                    or observed.tzinfo is None or completed.tzinfo is None
                    or not observed<=completed<=now or (now-completed).total_seconds()>20
                    or observed.astimezone(KST).date()!=now.astimezone(KST).date()):raise ValueError
            self._assert_strategy_holding_evidence(db,proof)
            day=observed.astimezone(KST).strftime('%Y%m%d')
            pages=[]; stream=[]
            observations=db.execute("SELECT id,digest,payload_json FROM broker_observation_evidence WHERE scope=? AND day=? AND mode='paper' AND source_api IN ('00','ka10075') ORDER BY id LIMIT 20001",(self.scope,day)).fetchall()
            if len(observations)>20000:raise ValueError
            for row in observations:
                raw=row['payload_json']
                if not isinstance(raw,str) or hashlib.sha256(raw.encode()).hexdigest()!=row['digest']:raise ValueError
                event=json.loads(raw)
                if event.get('source_api')=='ka10075' and event.get('query_id')==proof['pending_query_id']:pages.append(event)
                elif event.get('source_api')=='00':
                    at=datetime.fromisoformat(event['observed_at'])
                    if at.tzinfo is None:raise ValueError
                    values=event.get('values',{})
                    if (event.get('event_kind')=='execution_stream' and observed<=at<=now
                            and isinstance(values,dict) and values.get('9001') in (proof['symbol'],'A'+proof['symbol'])):
                        raise ValueError
                    if event.get('event_kind') in ('stream_started','stream_resumed','stream_gap','stream_stopped'):
                        stream.append((at,row['id'],event['event_kind']))
            if len(pages)==0 or len(stream)>20000:raise ValueError
            pages.sort(key=lambda e:e['page'])
            prior_page_at=observed
            for i,event in enumerate(pages):
                c=event['continuation']; body=event['request_body']; response=event['response_body']
                at=datetime.fromisoformat(event['observed_at'])
                requested=datetime.fromisoformat(event['request_started_at'])
                if (event['page']!=i+1 or event['status']!='success' or event['mode']!='paper'
                        or event['day']!=day or body.get('all_stk_tp')!='0' or body.get('trde_tp')!='0'
                        or body.get('stex_tp')!='0' or body.get('stk_cd') not in (None,'')
                        or at.tzinfo is None or requested.tzinfo is None
                        or not prior_page_at<=requested<=at<=completed
                        or not ((type(response.get('return_code')) is int and response.get('return_code')==0)
                                or (type(response.get('return_code')) is str and response.get('return_code')=='0'))
                        or c.get('request_cont_yn')!=('N' if i==0 else 'Y')
                        or (i<len(pages)-1 and (c.get('response_cont_yn')!='Y' or c.get('next_key_present') is not True))
                        or (i==len(pages)-1 and c.get('response_cont_yn') not in (None,'','N'))):raise ValueError
                _assert_no_broker_pending(response['oso'],proof['symbol'],integrated=(version==5))
                prior_page_at=at
            prior=[s for s in stream if s[0]<=observed]
            if (not prior or max(prior)[2] not in ('stream_started','stream_resumed')
                    or type(proof['stream_anchor_id']) is not int or max(prior)[1]!=proof['stream_anchor_id']):
                raise ValueError
            anchor=next(r for r in observations if r['id']==proof['stream_anchor_id'])
            if (anchor['digest']!=proof['stream_anchor_sha256']
                    or json.loads(anchor['payload_json'])['observed_at']!=proof['stream_anchor_observed_at']):raise ValueError
            if any(k in ('stream_gap','stream_stopped') and observed<=at<=now for at,_,k in stream):raise ValueError
        except Exception:
            raise ExecutionLogError('통합 미결·실시간 체결 및 신규 0주 기준 증빙이 불완전합니다.') from None

    @staticmethod
    def _assert_s5_exit_clock(strategy, position, evidence, now):
        """Bind the time-stop to independently reconstructed actual unit fills."""
        if strategy!='S5_steady_uptrend_vwap':
            return
        try:
            data=freeze_evidence(evidence).as_dict()
            native=data.get('strategy_version')=='paper-experiment-v1'
            if native:
                saved=data['config']['experiment_s5_exit']
                signal={'side':'sell','reason':saved['reason']}
            else:
                signal=data['multi_strategy_signal']; saved=signal['position']
            observed=datetime.fromisoformat(data['observed_at'])
            evaluated=datetime.fromisoformat(data['evaluated_at'])
            exit_only=position.get('ownership_status')=='exit_only_broker_confirmed'
            reason=signal.get('reason')
            if (data.get('strategy')!=strategy or data.get('strategy_version') not in ('paper-multi-observed-v4','paper-experiment-v1')
                    or data.get('source_kind')!='broker_observation' or signal.get('side')!='sell'
                    or observed.tzinfo is None or evaluated.tzinfo is None
                    or not observed<=evaluated<=now or (now-observed).total_seconds()>5):
                raise ValueError
            first=None
            if position.get('first_fill_timing_verified') is True:
                first=datetime.fromisoformat(position['first_fill_at'])
                if (saved.get('first_fill_at')!=position['first_fill_at'] or first.tzinfo is None
                        or not first<=observed):raise ValueError
            elif (not exit_only or reason not in ('s5_regular_cutoff_exit','s5_1519_cutoff',
                                                   'stop_loss_or_sma20','take_profit_10pct')):
                raise ValueError
            if reason=='time_stop_30m' and (first is None or now-first<timedelta(minutes=30)):
                raise ValueError
            if signal['reason'] in ('s5_regular_cutoff_exit','s5_1519_cutoff'):
                local=now.astimezone(KST)
                if local<local.replace(hour=15,minute=19,second=0,microsecond=0):
                    raise ValueError
            if native:
                reason=signal['reason']
                if reason not in ('time_stop_30m','s5_regular_cutoff_exit','stop_loss_or_sma20','take_profit_10pct'):
                    raise ValueError
                if reason in ('stop_loss_or_sma20','take_profit_10pct'):
                    price=data['config']['experiment_signal_price'];qty=position['owned_qty'];basis=position['basis_krw']
                    if any(type(n) is not int or n<=0 for n in (price,qty,basis)):raise ValueError
                    gain=price*qty*10000-basis*10000
                    if reason=='take_profit_10pct':
                        if gain<1000*basis:raise ValueError
                    elif gain>-300*basis:
                        rows=(data.get('bars') or [])[-20:]
                        starts=[datetime.fromisoformat(row['start']) for row in rows]
                        if (len(rows)!=20 or starts[-1]+timedelta(minutes=1)!=observed.replace(second=0,microsecond=0)
                                or starts[-1]-starts[0]>timedelta(minutes=21)
                                or any(row.get('source_observation',{}).get('mode')!='paper'
                                       or row.get('source_observation',{}).get('symbol')!=data['config']['experiment_assignment']['symbol']
                                       or row.get('source_observation',{}).get('adjustment') not in (None,'')
                                       or row.get('source_observation',{}).get('adjustment_rate') not in (None,'') for row in rows)
                                or price*20>=sum(row['close'] for row in rows)):raise ValueError
        except (ValueError,KeyError,TypeError,OverflowError,AttributeError):
            raise ExecutionLogError('S5 청산의 최초 실체결 시각 또는 현재 판단을 확인하지 못했습니다.') from None

    def assert_multi_exit_dispatch(self, mode, symbol, intent_id):
        """Fail closed if the reserved exit or its ownership changed in queue."""
        from kiwoom_autotrader.trading.paper_policy import _multi_enabled
        reason_code = "reconciliation_state_changed"
        try:
            if mode!='paper' or self.path.is_symlink() or not self.path.is_file():raise ValueError
            now=self._moment()
            with sqlite3.connect(self.path.resolve().as_uri()+'?mode=ro',uri=True,timeout=_SQLITE_TIMEOUT_SECONDS) as db:
                db.row_factory=sqlite3.Row;install_evidence_view(db);db.execute('PRAGMA query_only=ON');db.execute('BEGIN')
                self._validate_multi_exit_schema(db)
                intent=db.execute('SELECT * FROM intents WHERE scope=? AND id=?',(self.scope,intent_id)).fetchone()
                rec=db.execute('SELECT * FROM pre_order_reconciliations WHERE scope=? AND intent_id=?',(self.scope,intent_id)).fetchone()
                claim=db.execute('SELECT * FROM multi_strategy_exit_claims WHERE scope=? AND intent_id=?',(self.scope,intent_id)).fetchone()
                settings=self.settings_loader()
                if intent is None or not (_multi_enabled(settings) or effective_paper_experiment(settings,mode)['enabled'] and intent['strategy'] in STAGED_STRATEGIES):raise ValueError
                if intent is None or rec is None or claim is None or intent['symbol']!=symbol or intent['mode']!=mode or intent['side']!='sell' or intent['state']!='prepared':raise ValueError
                raw=rec['evidence_json']
                if not isinstance(raw,str) or hashlib.sha256(raw.encode('utf-8')).hexdigest()!=rec['evidence_sha256']:raise ValueError
                proof=json.loads(raw); observed=datetime.fromisoformat(proof['observed_at'])
                self._assert_strategy_holding_evidence(db,proof)
                if observed.tzinfo is not None and not 0 <= (now-observed).total_seconds() <= 20:
                    reason_code = "reconciliation_proof_expired"
                    raise ValueError
                if (proof.get('schema_version') not in (4,7) or proof.get('symbol')!=symbol or proof.get('strategy')!=intent['strategy']
                        or proof.get('holdings_source')!='broker_observation' or observed.tzinfo is None
                        or claim['generation_id']!=proof.get('generation_id') or claim['exit_kind']!=proof.get('exit_kind')
                        or proof.get('order_quantity')!=intent['quantity']
                        or self._symbol_order_watermark(db,mode,symbol,ignored_intent_id=intent_id)!=proof.get('symbol_ledger_sha256')):raise ValueError
                position=(self._assert_exit_only_proof(db,proof,now,ignored_intent_id=intent_id) if proof['schema_version']==7
                          else self.multi_strategy_position(mode,symbol,intent['strategy'],now,_ignored_intent_id=intent_id))
                if (position.get('status') not in ('owned','exit_only_broker_confirmed') or position.get('generation_id')!=proof['generation_id']
                        or position.get('owned_qty')!=proof['pre_order_held_qty']
                        or proof['exit_kind']=='half' and position.get('half_take_done')):raise ValueError
                if intent['strategy']=='S5_steady_uptrend_vwap':
                    decision=db.execute('SELECT evidence_json,evidence_sha256 FROM order_decisions WHERE scope=? AND intent_id=?',
                                        (self.scope,intent_id)).fetchone()
                    if (decision is None or not isinstance(decision['evidence_json'],str)
                            or hashlib.sha256(decision['evidence_json'].encode()).hexdigest()!=decision['evidence_sha256']):raise ValueError
                    self._assert_s5_exit_clock(intent['strategy'],position,json.loads(decision['evidence_json']),now)
        except (sqlite3.Error,ExecutionLogError,ValueError,TypeError,KeyError,OverflowError,OSError) as exc:
            err = ExecutionLogError('매도 대기 중 전략 소유권이나 잔고 근거가 바뀌어 전송하지 않았습니다.')
            err.reason_code = getattr(exc, 'reason_code', None) or reason_code
            raise err from None

    def assert_multi_buy_dispatch(self,mode,symbol,intent_id):
        """Revalidate the same pre-buy evidence after the queue wait."""
        from kiwoom_autotrader.trading.paper_policy import _multi_enabled,_symbol_cap,_buy_cap
        reason_code = "reconciliation_state_changed"
        try:
            settings=self.settings_loader()
            if mode!='paper' or self.path.is_symlink() or not self.path.is_file():raise ValueError
            now=self._moment()
            with sqlite3.connect(self.path.resolve().as_uri()+'?mode=ro',uri=True,timeout=_SQLITE_TIMEOUT_SECONDS) as db:
                db.row_factory=sqlite3.Row;install_evidence_view(db);db.execute('PRAGMA query_only=ON');db.execute('BEGIN')
                intent=db.execute('SELECT * FROM intents WHERE scope=? AND id=?',(self.scope,intent_id)).fetchone()
                rec=db.execute('SELECT * FROM pre_order_reconciliations WHERE scope=? AND intent_id=?',(self.scope,intent_id)).fetchone()
                if (intent is None or rec is None or intent['symbol']!=symbol or intent['mode']!=mode
                        or intent['side']!='buy' or intent['state']!='prepared'):raise ValueError
                if not (_multi_enabled(settings) or effective_paper_experiment(settings,mode)['enabled'] and intent['strategy'] in STAGED_STRATEGIES):raise ValueError
                raw=rec['evidence_json']
                if not isinstance(raw,str) or hashlib.sha256(raw.encode('utf-8')).hexdigest()!=rec['evidence_sha256']:raise ValueError
                proof=json.loads(raw); observed=datetime.fromisoformat(proof['observed_at'])
                self._assert_strategy_holding_evidence(db,proof)
                if effective_paper_experiment(settings,mode)['enabled']:
                    self._assert_paper_experiment_budget(db,proof,intent['quantity'],proof['limit_price_krw'],now)
                self._assert_no_pending_account(db,mode,ignored_intent_id=intent_id,symbol=symbol)
                position_limit = effective_paper_scan(settings, mode)['max_positions']
                if position_limit:
                    self._assert_position_capacity(db, symbol, now, position_limit, ignored_intent_id=intent_id,
                        principal=proof['order_quantity']*proof['limit_price_krw'], strategy=intent['strategy'],
                        theme_limit=effective_theme_cap(settings,mode),
                        unclassified_policy=effective_theme_unclassified_policy(settings,mode))
                if effective_paper_experiment(settings,mode)['enabled'] and intent['strategy'] in OVERNIGHT_EXPERIMENT_STRATEGIES:
                    self._assert_overnight_capacity(db,symbol,intent['strategy'],now,ignored_intent_id=intent_id)
                if proof.get('schema_version') in (5,6):self._assert_epoch_proof(db,proof,now)
                sizing=proof.get('paper_experiment_sizing')
                if effective_paper_experiment(settings,mode)['enabled'] and isinstance(sizing,dict):
                    expected_symbol=sizing.get('effective_symbol_cap_krw')
                    expected_buy=max(300000,sizing.get('price_krw',0)) if sizing.get('one_share_exception') is True else sizing.get('budget_krw')
                else:
                    expected_symbol,expected_buy=_symbol_cap(settings),_buy_cap(settings)
                if observed.tzinfo is not None and not 0 <= (now-observed).total_seconds() <= 20:
                    reason_code = "reconciliation_proof_expired"
                    raise ValueError
                if (proof.get('schema_version') not in (3,5,6) or proof.get('symbol')!=symbol or observed.tzinfo is None
                        or proof.get('symbol_cap_krw')!=expected_symbol or proof.get('per_buy_cap_krw')!=expected_buy
                        or proof.get('order_quantity')!=intent['quantity']
                        or self._symbol_order_watermark(db,mode,symbol,ignored_intent_id=intent_id)!=proof.get('symbol_ledger_sha256')):raise ValueError
                position=self.multi_strategy_position(mode,symbol,intent['strategy'],now,_ignored_intent_id=intent_id)
                held=proof['pre_order_held_qty'];basis=proof['pre_order_pur_amt_krw']
                if position.get('status')=='flat_candidate':
                    if held!=0 or basis!=0 or proof.get('schema_version') not in (5,6):raise ValueError
                    if position.get('flat_epoch_reason')=='prior_closed_cycle_current_broker_flat':
                        from .flat_epoch_reanchor import verified_prior_closed_flat
                        if verified_prior_closed_flat(self,db,symbol,now,ignored_intent_id=intent_id) is None:raise ValueError
                    self._assert_epoch_proof(db,proof,now)
                elif position.get('status')=='flat':
                    if held!=0 or basis!=0:raise ValueError
                elif position.get('status')=='owned':
                    if (position.get('overnight_unobserved_interval') is not False
                            or position.get('owned_qty')!=held or position.get('basis_krw')!=basis):raise ValueError
                else:raise ValueError
                decision = db.execute('SELECT order_price,evidence_json,evidence_sha256 FROM order_decisions WHERE scope=? AND intent_id=?',
                                      (self.scope,intent_id)).fetchone()
                if decision is None:raise ValueError
                data = json.loads(decision['evidence_json']) if decision['evidence_json'] is not None else {}
                cfg = data.get('config') or {}
                if intent['strategy']=='S5_steady_uptrend_vwap' and data.get('strategy_version')=='paper-experiment-v1':
                    if (not effective_paper_experiment(settings,mode).get('s5_enabled')
                            or cfg['experiment_s5_signal']['symbol']!=symbol
                            or cfg['experiment_assignment']['symbol']!=symbol
                            or proof.get('schema_version')!=6 or held!=0 or basis!=0):raise ValueError
                if intent['strategy'] in {'EXP_ORB_A','EXP_ORB_B','EXP_GDR','EXP_VSS','EXP_LDR'}:
                    if (not isinstance(decision['evidence_json'],str)
                            or hashlib.sha256(decision['evidence_json'].encode()).hexdigest()!=decision['evidence_sha256']):
                        raise ValueError
                    self._assert_experiment_admission(mode,'buy',symbol,intent['strategy'],
                                                      freeze_evidence(data).as_dict(),now)
                if intent['strategy']=='gdcrs':
                    if decision['evidence_json'] is not None:
                        if hashlib.sha256(decision['evidence_json'].encode()).hexdigest()!=decision['evidence_sha256']:
                            raise ValueError
                        data=freeze_evidence(data).as_dict()
                    self._assert_dynamic_s1_entry(mode,'buy',symbol,intent['strategy'],data,now,
                        price=decision['order_price'],quantity=intent['quantity'],reconciliation_evidence=proof)
                if intent['strategy'] in ('S2_surge_high_breakout','S5_steady_uptrend_vwap'):
                    if hashlib.sha256(decision['evidence_json'].encode()).hexdigest()!=decision['evidence_sha256']:
                        raise ValueError
                    self._assert_current_multi_entry_policy(mode, 'buy', intent['strategy'],
                                                            freeze_evidence(data).as_dict(), now)
                if (intent['strategy']=='S5_steady_uptrend_vwap' and cfg.get('multi_b3_stage')=='initial'
                        and proof.get('schema_version')!=5):
                    raise ValueError
                if cfg.get('multi_b3_stage') is not None:
                    if (hashlib.sha256(decision['evidence_json'].encode()).hexdigest()!=decision['evidence_sha256']
                            or decision['order_price'] is None):raise ValueError
                    frozen = freeze_evidence(data)
                    initial = self._assert_multi_b3_buy(db,mode,symbol,intent['strategy'],now,cfg,frozen.as_dict(),proof,
                                                       intent['quantity'],int(decision['order_price']),ignored_intent_id=intent_id)
                    if cfg['multi_b3_stage']=='add':
                        claim = db.execute('SELECT initial_intent_id FROM b3_add_claims WHERE scope=? AND add_intent_id=?',
                                           (self.scope,intent_id)).fetchone()
                        if claim is None or claim['initial_intent_id']!=initial:raise ValueError
                    counts = self._daily_buy_entry_counts(db,mode,symbol,now.astimezone(KST).strftime('%Y%m%d'))
                    if counts['accepted']+counts['reserved']>cfg['max_daily_entries_per_symbol']:raise ValueError
        except (sqlite3.Error,ExecutionLogError,ValueError,TypeError,KeyError,OverflowError,OSError) as exc:
            err = ExecutionLogError('매수 대기 중 전략 소유권이나 잔고 근거가 바뀌어 전송하지 않았습니다.')
            err.reason_code = getattr(exc, 'reason_code', None) or reason_code
            raise err from None

    @staticmethod
    def _validate_multi_exit_schema(db):
        cols=tuple(r[1] for r in db.execute('PRAGMA table_info(multi_strategy_exit_claims)'))
        indexes=set()
        for row in db.execute('PRAGMA index_list(multi_strategy_exit_claims)'):
            if row[2]:indexes.add(tuple(x[2] for x in db.execute('SELECT seqno,cid,name FROM pragma_index_info(?) ORDER BY seqno',(row[1],))))
        triggers={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='multi_strategy_exit_claims'")}
        if (cols!=('scope','generation_id','exit_kind','intent_id','claimed_at')
                or ('scope','intent_id') not in indexes
                or not {'multi_strategy_exit_claims_no_update','multi_strategy_exit_claims_no_delete'}<=triggers):
            raise ExecutionLogError('전략 매도 예약 원장 구조를 확인하지 못했습니다.')
        release_cols=tuple(r[1] for r in db.execute('PRAGMA table_info(multi_strategy_exit_releases)'))
        release_indexes=set()
        for row in db.execute('PRAGMA index_list(multi_strategy_exit_releases)'):
            if row[2]:release_indexes.add(tuple(x[2] for x in db.execute('SELECT seqno,cid,name FROM pragma_index_info(?) ORDER BY seqno',(row[1],))))
        release_triggers={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='multi_strategy_exit_releases'")}
        if (release_cols!=('scope','intent_id','released_at','reason') or ('scope','intent_id') not in release_indexes or
                not {'multi_strategy_exit_releases_no_update','multi_strategy_exit_releases_no_delete'}<=release_triggers):
            raise ExecutionLogError('전략 매도 미전송 해제 원장 구조를 확인하지 못했습니다.')

    def _assert_no_pending_order(self, db, mode, symbol):
        if mode not in ("paper", "real") or not isinstance(symbol, str) or re.fullmatch(r"[0-9]{6}", symbol) is None:
            raise ExecutionLogError("미결 주문을 확인할 투자구분·종목코드가 잘못되었습니다.")
        unresolved = db.execute("""SELECT 1 FROM intents i WHERE i.scope=? AND i.mode=? AND i.symbol=?
            AND i.state IS NOT 'cancelled' AND (i.state IS NOT 'accepted' OR i.order_number IS NULL OR NOT EXISTS (
                SELECT 1 FROM orders o WHERE o.scope=i.scope AND o.day=i.day AND o.mode=i.mode
                AND o.ord_no=i.order_number)) LIMIT 1""", (self.scope, mode, symbol)).fetchone()
        pending = self._pending_order_rows(db,mode,symbol)
        if unresolved or pending:
            raise ExecutionLogError(f"{symbol} 종목의 접수 미확정 또는 미체결 주문이 남아 있어 추가 자동주문을 보류합니다.")

    def _assert_legacy_ddcrs_allowed(self,db,mode,symbol):
        if mode!='paper' or re.fullmatch(r'[0-9]{6}',symbol or '') is None:raise ExecutionLogError('기존 매도 범위가 불명확합니다.')
        row=db.execute("""SELECT 1 FROM intents WHERE scope=? AND mode=? AND symbol=? AND state IS NOT 'cancelled'
            AND strategy IN ('S2_surge_high_breakout','S3_upper_limit_continuation','S4_theme_and_catalyst_leader','S5_steady_uptrend_vwap') LIMIT 1""",
            (self.scope,mode,symbol)).fetchone()
        if row:
            # History alone does not prove current ownership. Only the
            # independently reconciled current gdcrs generation may be sold.
            pending=db.execute("SELECT id FROM intents WHERE scope=? AND mode=? AND symbol=? AND side='sell' AND strategy='ddcrs' AND state='prepared' AND order_number IS NULL LIMIT 2",(self.scope,mode,symbol)).fetchall()
            if len(pending)>1:raise ExecutionLogError('기존 매도 예약이 중복되었습니다.')
            position=self.multi_strategy_position(mode,symbol,'ddcrs',self._moment(),
                       _ignored_intent_id=pending[0]['id'] if pending else None)
            if position.get('status')!='owned':
                raise ExecutionLogError('현재 보유 세대의 기존 전략 소유권이 확인되지 않아 자동매도를 보류합니다.')

    def assert_legacy_ddcrs_allowed(self,mode,symbol):
        if self.path.is_symlink() or not self.path.is_file():raise ExecutionLogError('매도 소유권 원장이 없습니다.')
        try:
            with sqlite3.connect(self.path.resolve().as_uri()+'?mode=ro',uri=True,timeout=_SQLITE_TIMEOUT_SECONDS) as db:
                db.row_factory=sqlite3.Row;install_evidence_view(db);db.execute('PRAGMA query_only=ON');db.execute('BEGIN')
                self._validate_daily_ledger(db);self._assert_legacy_ddcrs_allowed(db,mode,symbol)
        except (sqlite3.Error,OSError,ValueError,TypeError,ExecutionLogError):
            raise ExecutionLogError('추가 전략 소유 여부가 불명확하여 기존 자동매도를 보류합니다.') from None

    def assert_no_pending_order(self, mode, symbol):
        """Read all dates and both sides; unknown cancellation never releases a gate."""
        with self._db() as db:
            self._assert_no_pending_order(db, mode, symbol)

    def _assert_no_pending_account(self, db, mode, *, ignored_intent_id=None, symbol=None):
        # Account-wide serialization prevents two internal symbols from using
        # the same earlier cash observation before either order is accepted.
        experiment=mode=='paper' and effective_paper_experiment(self.settings_loader(),mode)['enabled']
        if experiment and (not isinstance(symbol,str) or re.fullmatch(r'[0-9]{6}',symbol) is None):
            raise ExecutionLogError('모의 실험의 종목별 미결 확인에 종목이 필요합니다.')
        selected=symbol if experiment else None
        unresolved = db.execute("""SELECT 1 FROM intents i WHERE i.scope=? AND i.mode=? AND (? IS NULL OR i.id!=?)
            AND (? IS NULL OR i.symbol=?)
            AND i.state IS NOT 'cancelled' AND (i.state IS NOT 'accepted' OR i.order_number IS NULL OR NOT EXISTS (
                SELECT 1 FROM orders o WHERE o.scope=i.scope AND o.day=i.day AND o.mode=i.mode
                AND o.ord_no=i.order_number)) LIMIT 1""", (self.scope,mode,ignored_intent_id,ignored_intent_id,selected,selected)).fetchone()
        pending = self._pending_order_rows(db,mode,selected)
        if unresolved or pending:
            raise ExecutionLogError("계좌 내 미해결 또는 미체결 자동주문이 있어 자동매수를 보류합니다.")

    def validate_paper_monitor_resume(self, proof, now):
        """Fresh saved REST proof plus no unresolved sends; never clear history."""
        if not self.validate_paper_stream_recovery(proof, now):
            return False
        try:
            with sqlite3.connect(self.path.resolve().as_uri()+'?mode=ro', uri=True, timeout=_SQLITE_TIMEOUT_SECONDS) as db:
                db.row_factory=sqlite3.Row; install_evidence_view(db);db.execute('PRAGMA query_only=ON'); db.execute('BEGIN')
                self._assert_no_pending_account(db, 'paper')
            return True
        except (ExecutionLogError, sqlite3.Error, OSError, ValueError, TypeError):
            return False

    def validate_paper_stream_recovery(self, proof, now, *, minimum_started_at=None):
        """Validate saved, complete paper account observations after a 00 gap."""
        try:
            if (not isinstance(proof,dict) or set(proof)!={'day','execution_query_id','pending_query_id','account_observed_at'}
                    or not isinstance(now,datetime) or now.tzinfo is None):return False
            day=now.astimezone(KST).strftime('%Y%m%d')
            if minimum_started_at is not None and (not isinstance(minimum_started_at,datetime)
                    or minimum_started_at.tzinfo is None or minimum_started_at.utcoffset() is None
                    or minimum_started_at>now or minimum_started_at.astimezone(KST).strftime('%Y%m%d')!=day):return False
            if proof['day']!=day:return False
            for key in ('execution_query_id','pending_query_id'):
                if not isinstance(proof[key],str) or re.fullmatch(r'[0-9a-f]{32}',proof[key]) is None:return False
            account_at=datetime.fromisoformat(proof['account_observed_at'])
            if account_at.tzinfo is None or not 0<=(now-account_at).total_seconds()<=90:return False
            if self.path.is_symlink() or not self.path.is_file():return False
            with sqlite3.connect(self.path.resolve().as_uri()+'?mode=ro',uri=True,timeout=_SQLITE_TIMEOUT_SECONDS) as db:
                db.row_factory=sqlite3.Row;install_evidence_view(db);db.execute('PRAGMA query_only=ON');db.execute('BEGIN')
                balance=db.execute("SELECT * FROM account_balance_daily WHERE scope=? AND mode='paper' AND day=?",(self.scope,day)).fetchone()
                if balance is None:
                    return False
                if balance['observed_at'] != proof['account_observed_at']:
                    if not db.execute("""SELECT 1 FROM sqlite_master WHERE type='table'
                            AND name='paper_account_balance_observations'""").fetchone():
                        return False
                    balance = db.execute("""SELECT source_kind,digest,payload_json
                        FROM paper_account_balance_observations
                        WHERE scope=? AND mode='paper' AND day=? AND observed_at=?""",
                        (self.scope, day, proof['account_observed_at'])).fetchone()
                    if (balance is None or not isinstance(balance['payload_json'], str)
                            or hashlib.sha256(balance['payload_json'].encode()).hexdigest() != balance['digest']):
                        return False
                if balance['source_kind'] != 'broker_observation':
                    return False
                account = json.loads(balance['payload_json'])
                if (not isinstance(account, dict) or account.get('schema') != 'paper-account-snapshot-v1'
                        or account.get('mode') != 'paper' or not isinstance(account.get('holdings'), list)):
                    return False
                saved=db.execute("SELECT source_api,digest,payload_json FROM broker_observation_evidence WHERE scope=? AND mode='paper' AND day=? AND source_api IN ('kt00009','ka10075') ORDER BY id DESC LIMIT 20001",(self.scope,day)).fetchall()
                if len(saved)>20000:return False
                groups={'kt00009':[],'ka10075':[]}
                for row in saved:
                    raw=row['payload_json']
                    if not isinstance(raw,str) or hashlib.sha256(raw.encode()).hexdigest()!=row['digest']:return False
                    event=json.loads(raw);api=row['source_api'];key='execution_query_id' if api=='kt00009' else 'pending_query_id'
                    if event.get('query_id')==proof[key]:groups[api].append(event)
                ranges={}
                for api,events in groups.items():
                    if not events:return False
                    events.sort(key=lambda e:e['page']);previous=None
                    for i,event in enumerate(events):
                        body=event['request_body'];response=event['response_body'];c=event['continuation']
                        start=datetime.fromisoformat(event['request_started_at']);end=datetime.fromisoformat(event['observed_at'])
                        if (event.get('source_kind')!='broker_observation' or event.get('mode')!='paper' or event.get('day')!=day
                                or event['page']!=i+1 or event['status']!='success' or start.tzinfo is None or end.tzinfo is None
                                or not start<=end<=now or previous is not None and start<previous
                                or response.get('return_code') not in (0,'0') or isinstance(response.get('return_code'),bool)
                                or c.get('request_cont_yn')!=('N' if i==0 else 'Y')
                                or i<len(events)-1 and (c.get('response_cont_yn')!='Y' or c.get('next_key_present') is not True)
                                or i==len(events)-1 and c.get('response_cont_yn') not in (None,'','N')):return False
                        if api=='kt00009':
                            if (body.get('ord_dt')!=day or body.get('qry_tp')!='1' or body.get('stk_bond_tp')!='1'
                                    or body.get('sell_tp')!='0' or body.get('stk_cd') not in (None,'')
                                    or body.get('fr_ord_no') not in (None,'') or body.get('dmst_stex_tp')!='KRX'
                                    or not isinstance(response.get('acnt_ord_cntr_prst_array'),list)):return False
                        else:
                            if (body.get('all_stk_tp')!='0' or body.get('trde_tp')!='0' or body.get('stex_tp')!='0'
                                    or body.get('stk_cd') not in (None,'') or not isinstance(response.get('oso'),list)):return False
                        previous=end
                        if i==0:ranges[api]=[start,end]
                        else:ranges[api][1]=end
                return (ranges['kt00009'][1]<=account_at<=ranges['ka10075'][0]
                    and 0<=(now-ranges['ka10075'][1]).total_seconds()<=90
                    and (minimum_started_at is None or ranges['kt00009'][0]>=minimum_started_at))
        except (ValueError,TypeError,KeyError,sqlite3.Error,OverflowError,OSError):
            return False

    def _assert_paper_experiment_budget(self, db, proof, quantity, price, now):
        """Bind sizing arithmetic to the same fresh paper broker balance."""
        from .paper_policy import validate_experiment_order_sizing
        try:
            sizing=proof['paper_experiment_sizing']
            checked=validate_experiment_order_sizing(sizing,quantity=quantity,price=price)
            row=db.execute("SELECT observed_at,source_kind,payload_json FROM account_balance_daily WHERE scope=? AND mode='paper' AND day=?",
                           (self.scope,now.astimezone(KST).strftime('%Y%m%d'))).fetchone()
            if row is None or row['source_kind']!='broker_observation':raise ValueError
            at=datetime.fromisoformat(row['observed_at']);observed=datetime.fromisoformat(proof['observed_at'])
            data=json.loads(row['payload_json']);cash=data['cash']
            if (at.tzinfo is None or observed.tzinfo is None or not at<=observed<=now
                    or not 0<=(now-at).total_seconds()<=90 or data.get('schema')!='paper-account-snapshot-v1'
                    or data.get('mode')!='paper' or data.get('exchange')!='KRX'
                    or any(not isinstance(cash.get(k),str) or re.fullmatch(r'\+?[0-9]{1,15}',cash[k]) is None
                           for k in ('entr','ord_alow_amt'))
                    or checked['available_cash_krw']!=int(cash['ord_alow_amt'])
                    or checked['existing_principal_krw']!=proof['pre_order_pur_amt_krw']
                    or checked['pending_principal_krw']!=0):raise ValueError
        except Exception:
            raise ExecutionLogError('모의 실험 예산·현재 증권사 현금·종목 원금 증빙이 일치하지 않습니다.') from None

    def _assert_overnight_capacity(self, db, symbol, strategy, now, *, ignored_intent_id=None):
        """Count distinct overnight-owned or reserved symbols in this account.

        Run under the same order reservation transaction. A broker holding
        whose owner cannot be established consumes a conservative slot in both
        limits; it is never silently treated as a flat instrument.
        """
        if strategy not in OVERNIGHT_EXPERIMENT_STRATEGIES:return
        profile=effective_paper_experiment(self.settings_loader(),'paper')
        if not profile['enabled']:raise ExecutionLogError('모의 실험 정책이 비활성화되었습니다.')
        row=db.execute("SELECT observed_at,source_kind,payload_json FROM account_balance_daily WHERE scope=? AND mode='paper' AND day=?",
                       (self.scope,now.astimezone(KST).strftime('%Y%m%d'))).fetchone()
        try:
            if row is None or row['source_kind']!='broker_observation':raise ValueError
            at=datetime.fromisoformat(row['observed_at']);account=json.loads(row['payload_json'])
            if (at.tzinfo is None or not 0<=(now-at).total_seconds()<=90
                    or account.get('schema')!='paper-account-snapshot-v1' or account.get('mode')!='paper'
                    or not isinstance(account.get('holdings'),list)):raise ValueError
            held={}
            for item in account['holdings']:
                code=item.get('stk_cd');qty=item.get('rmnd_qty')
                if (not isinstance(code,str) or re.fullmatch(r'[AJQ]?[0-9]{6}',code) is None
                        or not isinstance(qty,str) or re.fullmatch(r'\+?[0-9]{1,15}',qty) is None):raise ValueError
                if int(qty):held[code[-6:]]=held.get(code[-6:],0)+int(qty)
            rows=db.execute("""SELECT f.symbol,f.side,f.quantity,f.strategy,o.symbol AS osymbol,
                    o.side AS oside,o.strategy AS ostrategy FROM fills f LEFT JOIN orders o
                    ON o.scope=f.scope AND o.day=f.day AND o.mode=f.mode AND o.ord_no=f.ord_no
                    WHERE f.scope=? AND f.mode='paper' ORDER BY f.day,f.ord_no,f.fill_no LIMIT 100001""",(self.scope,)).fetchall()
            if len(rows)>100000:raise ValueError
            positions={};invalid=set()
            for fill in rows:
                code=fill['symbol']
                if code not in held:continue
                if (fill['osymbol']!=code or fill['side']!=fill['oside'] or fill['strategy']!=fill['ostrategy']
                        or fill['side'] not in ('buy','sell') or type(fill['quantity']) is not int or fill['quantity']<=0):
                    invalid.add(code);continue
                byarm=positions.setdefault(code,{})
                byarm[fill['strategy']]=byarm.get(fill['strategy'],0)+fill['quantity']*(1 if fill['side']=='buy' else -1)
            overnight=set();vss=set()
            for code,qty in held.items():
                groups=positions.get(code,{})
                owners=[arm for arm,n in groups.items() if n>0]
                unknown=(code in invalid or len(owners)!=1 or any(n<0 for n in groups.values())
                         or sum(groups.values())!=qty)
                if unknown:
                    overnight.add(code);vss.add(code)
                elif owners[0] in OVERNIGHT_EXPERIMENT_STRATEGIES:
                    overnight.add(code)
                    if owners[0]=='EXP_VSS':vss.add(code)
            unresolved=db.execute("""SELECT i.symbol,i.strategy FROM intents i WHERE i.scope=? AND i.mode='paper'
                AND i.side='buy' AND (? IS NULL OR i.id!=?) AND i.state IS NOT 'cancelled'
                AND (i.state IS NOT 'accepted' OR i.order_number IS NULL OR NOT EXISTS
                    (SELECT 1 FROM orders o WHERE o.scope=i.scope AND o.day=i.day AND o.mode=i.mode AND o.ord_no=i.order_number))
                LIMIT 10001""",(self.scope,ignored_intent_id,ignored_intent_id)).fetchall()
            pending=list(unresolved)+[r for r in self._pending_order_rows(db,'paper') if r['side']=='buy']
            if len(pending)>10000:raise ValueError
            for candidate in pending:
                code=candidate['symbol'];arm=candidate['strategy']
                if not isinstance(code,str) or re.fullmatch(r'[0-9]{6}',code) is None:raise ValueError
                if arm in OVERNIGHT_EXPERIMENT_STRATEGIES:
                    overnight.add(code)
                    if arm=='EXP_VSS':vss.add(code)
            if symbol not in overnight and len(overnight)>=profile['max_overnight_positions']:
                raise ConcurrentPositionLimitReached('모의 오버나잇·스윙 보유 및 예약 20종목 한도에 도달했습니다.')
            if strategy=='EXP_VSS' and symbol not in vss and len(vss)>=profile['max_vss_positions']:
                raise ConcurrentPositionLimitReached('모의 VSS 보유 및 예약 8종목 한도에 도달했습니다.')
            return {'overnight_symbols':sorted(overnight),'vss_symbols':sorted(vss)}
        except ConcurrentPositionLimitReached:raise
        except (ValueError,TypeError,KeyError,AttributeError,sqlite3.Error,OverflowError):
            raise ConcurrentPositionLimitUnavailable('오버나잇·스윙 보유 및 예약을 확인하지 못했습니다.') from None

    def _verified_nonprojecting_repeat(self, db, row, now):
        """A kind label alone is never evidence that economics did not change."""
        try:
            raw=row['payload_json']
            if not isinstance(raw,str) or hashlib.sha256(raw.encode()).hexdigest()!=row['digest']:
                return False
            data=json.loads(raw); at=datetime.fromisoformat(row['observed_at'])
            if (row['kind']!='repeated_snapshot' or row['source_kind']!='broker_observation'
                    or data.get('kind')!=row['kind'] or data.get('source_kind')!=row['source_kind']
                    or data.get('observed_at')!=row['observed_at'] or data.get('projection_applied') is not False
                    or data.get('schema_version')!=1 or at.tzinfo is None or at>now
                    or data.get('quantity_semantics')!='provider_key_snapshot_not_inferred_unit_fill'):
                return False
            current=data['current']; previous=data['previous']
            identity=('scope','day','mode','ord_no','fill_no')
            fields=identity+('symbol','name','side','moment','price','quantity','strategy')
            key=tuple(row[k] for k in identity)
            persisted=db.execute('SELECT * FROM fills WHERE scope=? AND day=? AND mode=? AND ord_no=? AND fill_no=?',key).fetchone()
            earlier=db.execute('SELECT digest,payload_json FROM execution_observations WHERE scope=? AND day=? AND mode=? AND ord_no=? AND fill_no=? AND id<? ORDER BY id DESC LIMIT 1',(*key,row['id'])).fetchone()
            if persisted is None or earlier is None or hashlib.sha256(earlier['payload_json'].encode()).hexdigest()!=earlier['digest']:
                return False
            prior_event=json.loads(earlier['payload_json']); prior_current=prior_event['current']
            if (prior_event.get('source_kind')!='broker_observation'
                    or datetime.fromisoformat(prior_event['observed_at'])>at
                    or prior_event.get('kind') not in ('new_execution_key','same_key_quantity_increase','same_key_correction','repeated_snapshot')):
                return False
            if any(type(current[k]) is not type(previous[k]) or type(current[k]) is not type(persisted[k])
                    or type(current[k]) is not type(prior_current[k])
                    or not current[k]==previous[k]==persisted[k]==prior_current[k] for k in fields):
                return False
            if any(current[k]!=row[k] for k in identity):return False
            return type(current['quantity']) is int and current['quantity']>0
        except (KeyError,TypeError,ValueError,sqlite3.Error,AttributeError):
            return False

    def _s5_reservation_state(self, db, holdings, pending, now, ignored_intent_id):
        """Release reserved S5 slots only for attributable holdings/intents.

        Called under the same capacity transaction; an unknown owner never
        frees an S5 reservation. Accepted broker reservations are already in
        ord_alow_amt; local unresolved intents are additionally cash reserved.
        """
        s5='S5_steady_uptrend_vwap'; occupied=set()
        held={row['stk_cd'][-6:]:int(row['rmnd_qty']) for row in holdings if int(row['rmnd_qty'])}
        fills=db.execute('''SELECT f.symbol,f.side,f.quantity,f.strategy,o.symbol AS osymbol,
            o.side AS oside,o.strategy AS ostrategy FROM fills f LEFT JOIN orders o
            ON o.scope=f.scope AND o.day=f.day AND o.mode=f.mode AND o.ord_no=f.ord_no
            WHERE f.scope=? AND f.mode='paper' ORDER BY f.day,f.ord_no,f.fill_no LIMIT 100001''',(self.scope,)).fetchall()
        if len(fills)>100000:raise ValueError
        bysymbol={}; invalid=set()
        for row in fills:
            code=row['symbol']
            if code not in held:continue
            if (row['osymbol']!=code or row['side']!=row['oside'] or row['strategy']!=row['ostrategy']
                    or row['side'] not in ('buy','sell') or type(row['quantity']) is not int or row['quantity']<=0):
                invalid.add(code);continue
            arms=bysymbol.setdefault(code,{})
            arms[row['strategy']]=arms.get(row['strategy'],0)+row['quantity']*(1 if row['side']=='buy' else -1)
        for code,qty in held.items():
            arms=bysymbol.get(code,{})
            if code not in invalid and arms.get(s5)==qty and all(n==0 for arm,n in arms.items() if arm!=s5):
                occupied.add(code)
        for row in pending:
            if row['strategy']==s5:occupied.add(row['symbol'])
        unresolved=db.execute('''SELECT i.quantity,d.order_price,d.order_type FROM intents i LEFT JOIN order_decisions d
            ON d.scope=i.scope AND d.intent_id=i.id WHERE i.scope=? AND i.mode='paper' AND i.side='buy'
            AND (? IS NULL OR i.id!=?) AND i.state IS NOT 'cancelled'
            AND (i.state IS NOT 'accepted' OR i.order_number IS NULL OR NOT EXISTS
                (SELECT 1 FROM orders o WHERE o.scope=i.scope AND o.day=i.day AND o.mode=i.mode AND o.ord_no=i.order_number))
            LIMIT 10001''',(self.scope,ignored_intent_id,ignored_intent_id)).fetchall()
        if len(unresolved)>10000:raise ValueError
        local_cash=0
        for row in unresolved:
            if (row['order_type']!='limit' or type(row['quantity']) is not int or row['quantity']<=0
                    or not isinstance(row['order_price'],str) or re.fullmatch(r'[0-9]{1,12}',row['order_price']) is None
                    or int(row['order_price'])<=0):raise ValueError
            amount=row['quantity']*int(row['order_price']);local_cash+=amount+(amount*5+999)//1000
        return occupied,local_cash

    def _assert_position_capacity(self, db, symbol, now, limit, *, ignored_intent_id=None, principal=None,
                                  theme_limit=0, unclassified_policy='block', snapshot_only=False, strategy=None):
        """Count broker holdings and all-strategy reservations under one lock.

        Holdings are an explicit complete broker account snapshot. A recent
        fill after that snapshot invalidates it; neither an empty projection
        nor a stale balance can authorize a new symbol. Existing-symbol adds
        consume no additional slot but still pass every principal/cash gate.
        """
        diagnostic={'validation_at':now.isoformat(),'reason':'capacity_input_invalid'}
        try:
            experimental=effective_paper_experiment(self.settings_loader(), 'paper')['enabled']
            if (type(limit) is not int or not 1 <= limit <= (40 if experimental else 10)
                    or unclassified_policy=='allow_unknown' and not experimental):
                raise ValueError
            day = now.strftime('%Y%m%d')
            row = db.execute("SELECT observed_at,source_kind,payload_json FROM account_balance_daily WHERE scope=? AND mode='paper' AND day=?",
                             (self.scope, day)).fetchone()
            if row is None or row['source_kind'] != 'broker_observation':
                diagnostic['reason']='balance_missing_or_unverified'
                raise ValueError
            observed = datetime.fromisoformat(row['observed_at'])
            diagnostic.update(balance_observed_at=row['observed_at'], reason='balance_stale_future_or_invalid')
            if observed.tzinfo is not None:diagnostic['balance_age_seconds']=(now-observed).total_seconds()
            payload = json.loads(row['payload_json'])
            if (observed.tzinfo is None or not 0 <= (now-observed).total_seconds() <= 90
                    or payload.get('schema') != 'paper-account-snapshot-v1'
                    or payload.get('mode') != 'paper' or payload.get('exchange') != 'KRX'
                    or not isinstance(payload.get('holdings'), list) or len(payload['holdings']) > 2000):
                raise ValueError
            symbols = set()
            if principal is not None:
                cash = payload.get('cash')
                if (type(principal) is not int or principal <= 0 or not isinstance(cash, dict)
                        or any(not isinstance(cash.get(k), str) or re.fullmatch(r'\+?[0-9]{1,15}', cash[k]) is None
                               for k in ('entr', 'ord_alow_amt'))):
                    raise ValueError
                available = int(cash['ord_alow_amt']) if experimental else min(int(cash['entr']), int(cash['ord_alow_amt']))
                if available < principal + (principal*5+999)//1000:
                    raise PaperCashLimitReached('예수금과 주문가능금액 중 작은 금액이 주문원금·수수료 여유보다 부족합니다.')
            for holding in payload['holdings']:
                code, qty = holding.get('stk_cd'), holding.get('rmnd_qty')
                if (not isinstance(code, str) or re.fullmatch(r'[AJQ]?[0-9]{6}', code) is None
                        or not isinstance(qty, str) or re.fullmatch(r'\+?[0-9]{1,15}', qty) is None):
                    raise ValueError
                if int(qty):
                    symbols.add(code[-6:])
            # A later fill can establish an unseen holding even if its order
            # has already become fully filled and left the pending set.
            # An identical re-poll is not a later economic event.  A new key,
            # correction, changed cumulative amount, or uncertain projection
            # still invalidates the older account snapshot.
            observations=db.execute("SELECT * FROM execution_observations WHERE scope=? AND mode='paper' AND day=? AND observed_at>? ORDER BY id LIMIT 20001",
                          (self.scope, day, observed.isoformat())).fetchall()
            diagnostic['reason']='post_balance_execution_change_or_unverified_repeat'
            if len(observations)>20000:raise ValueError
            for observation in observations:
                if not self._verified_nonprojecting_repeat(db,observation,now):
                    diagnostic.update(blocking_observation_id=observation['id'],blocking_kind=observation['kind'])
                    raise ValueError
            diagnostic['reason']='post_balance_stream_change_or_invalid'
            later = db.execute("SELECT digest,payload_json FROM broker_observation_evidence WHERE scope=? AND mode='paper' AND day=? AND source_api='00' AND observed_at>? LIMIT 20001",
                               (self.scope, day, observed.isoformat())).fetchall()
            if len(later) > 20000:
                raise ValueError
            for event_row in later:
                if hashlib.sha256(event_row['payload_json'].encode()).hexdigest()!=event_row['digest']:raise ValueError
                event = json.loads(event_row['payload_json'])
                if (event.get('source_kind')!='broker_observation' or event.get('source_api')!='00'
                        or event.get('mode')!='paper' or event.get('day')!=day):raise ValueError
                if event.get('event_kind') in ('stream_gap', 'stream_stopped'):
                    raise ValueError
                values = event.get('values')
                if event.get('event_kind') == 'execution_stream' and (not isinstance(values, dict)
                        or values.get('909') or values.get('915')):
                    raise ValueError
            diagnostic['reason']='pending_reservations_unverified'
            pending = db.execute("""SELECT i.symbol,i.strategy FROM intents i WHERE i.scope=? AND i.mode='paper' AND i.side='buy'
                AND (? IS NULL OR i.id!=?) AND i.state IS NOT 'cancelled'
                AND (i.state IS NOT 'accepted' OR i.order_number IS NULL OR NOT EXISTS
                    (SELECT 1 FROM orders o WHERE o.scope=i.scope AND o.day=i.day AND o.mode=i.mode AND o.ord_no=i.order_number))
                LIMIT 10001""", (self.scope, ignored_intent_id, ignored_intent_id)).fetchall()
            pending = list(pending) + [row for row in self._pending_order_rows(db,'paper') if row['side']=='buy']
            if len(pending) > 10000:
                raise ValueError
            for candidate in pending:
                code = candidate['symbol']
                if not isinstance(code, str) or re.fullmatch(r'[0-9]{6}', code) is None:
                    raise ValueError
                symbols.add(code)
            reservation=None
            profile=effective_paper_experiment(self.settings_loader(),'paper')
            if profile.get('s5_enabled') and not snapshot_only and strategy is not None:
                s5_symbols,local_cash=self._s5_reservation_state(db,payload['holdings'],pending,now,ignored_intent_id)
                unused=max(0,profile['s5_reserved_positions']-len(s5_symbols))
                cash=payload.get('cash')
                if (not isinstance(cash,dict) or not isinstance(cash.get('ord_alow_amt'),str)
                        or re.fullmatch(r'\+?[0-9]{1,15}',cash['ord_alow_amt']) is None):raise ValueError
                available=int(cash['ord_alow_amt'])
                budget=min(profile['per_buy_budget_ceiling_krw'],available//profile['cash_budget_divisor'])
                reserve=0 if strategy=='S5_steady_uptrend_vwap' else unused*budget
                if principal is not None and available < local_cash+reserve+principal+(principal*5+999)//1000:
                    raise PaperCashLimitReached('s5_reserved_cash: S5 미사용 예약 예산 또는 기존 매수 의도의 현금을 침범합니다.')
                if (strategy!='S5_steady_uptrend_vwap' and symbol not in symbols
                        and len(symbols)>=limit-unused):
                    raise ConcurrentPositionLimitReached('s5_reserved_positions: S5용 공유 한도 내 예약 슬롯을 보존합니다.')
                reservation={'s5_occupied_symbols':sorted(s5_symbols),'unused_s5_slots':unused,
                             'reserved_cash_krw':reserve,'local_intent_cash_krw':local_cash}
        except (ValueError, TypeError, KeyError, AttributeError, sqlite3.Error):
            error=ConcurrentPositionLimitUnavailable('계좌 전체의 최신 보유·예약 종목 수를 확인하지 못해 매수를 보류합니다.')
            error.diagnostics=diagnostic
            raise error from None
        theme = None
        if theme_limit and symbol not in symbols and not snapshot_only:
            from .theme_concentration import theme_occupancy
            try:
                if type(theme_limit) is not int or not 1 <= theme_limit <= 10:
                    raise ValueError
                theme = theme_occupancy(symbol, symbols, now, path=self.theme_taxonomy_path,
                                        unclassified_policy=unclassified_policy)
            except (ValueError, TypeError, KeyError, OSError):
                raise ThemeConcentrationUnavailable('보유·예약 종목의 대표 테마를 확인하지 못해 신규 매수를 보류합니다.') from None
            # An existing-symbol add never consumes another distinct slot.
            if any(len(codes) >= (min(theme_limit, 2) if family == 'unclassified_pool' else theme_limit)
                   for family, codes in theme['occupied_by_theme'].items()
                   if not (family=='unclassified_pool' and unclassified_policy=='allow_unknown')):
                raise ThemeConcentrationLimitReached('동일 대표 테마 보유·미체결·매수 의도 종목 상한에 도달했습니다.')
        if not snapshot_only and symbol not in symbols and len(symbols) >= limit:
            raise ConcurrentPositionLimitReached('모의계좌 동시 보유 종목 상한에 도달해 신규 종목 매수를 보류합니다.')
        return {'limit': limit, 'occupied_symbols': sorted(symbols), 'observed_at': observed.isoformat(),
                'theme_limit': theme_limit, 'theme_evidence': theme, 's5_reservation':reservation}

    def paper_theme_capacity_snapshot(self, now=None):
        """One read transaction for radar prefiltering; final order gates repeat.

        The returned observation is scoped to paper and current KST date. No
        reservations are created and full capacity is represented, not raised,
        so owned-symbol management can proceed independently of entry capacity.
        """
        moment = self._moment() if now is None else now.astimezone(KST)
        settings = self.settings_loader()
        limit = effective_paper_scan(settings, 'paper')['max_positions']
        theme_limit = effective_theme_cap(settings, 'paper')
        policy = effective_theme_unclassified_policy(settings, 'paper')
        if self.path.is_symlink() or not self.path.is_file():
            raise ConcurrentPositionLimitUnavailable('동시 보유 종목 원장이 없습니다.')
        with sqlite3.connect(self.path.resolve().as_uri()+'?mode=ro', uri=True, timeout=_SQLITE_TIMEOUT_SECONDS) as db:
            db.row_factory=sqlite3.Row; install_evidence_view(db);db.execute('PRAGMA query_only=ON'); db.execute('BEGIN')
            result = self._assert_position_capacity(db, None, moment, limit or 10,
                theme_limit=theme_limit, unclassified_policy=policy, snapshot_only=True)
        result.update({'mode': 'paper', 'as_of': moment.isoformat(), 'position_limit': limit,
                       'unclassified_policy': policy, 'themes_by_symbol': {},
                       'invalid_theme_symbols': [], 'taxonomy_evidence': None})
        if theme_limit:
            from .theme_concentration import theme_family_snapshot
            try:
                result.update(theme_family_snapshot(result['occupied_symbols'], moment,
                    path=self.theme_taxonomy_path, unclassified_policy=policy))
            except (ValueError, TypeError, KeyError, OSError):
                raise ThemeConcentrationUnavailable('테마 레이더의 분류 근거를 확인하지 못했습니다.') from None
        return result

    def paper_position_capacity(self, symbol, now=None):
        """Read-only early diagnostic; prepare_order repeats atomically before send."""
        moment = self._moment() if now is None else now.astimezone(KST)
        settings = self.settings_loader()
        limit = effective_paper_scan(settings, 'paper')['max_positions']
        theme_limit = effective_theme_cap(settings, 'paper')
        if not limit and not theme_limit:
            return {'limit': 0, 'occupied_symbols': [], 'observed_at': None}
        if self.path.is_symlink() or not self.path.is_file():
            raise ConcurrentPositionLimitUnavailable('동시 보유 종목 원장이 없습니다.')
        with sqlite3.connect(self.path.resolve().as_uri()+'?mode=ro', uri=True, timeout=_SQLITE_TIMEOUT_SECONDS) as db:
            db.row_factory=sqlite3.Row; install_evidence_view(db);db.execute('PRAGMA query_only=ON'); db.execute('BEGIN')
            return self._assert_position_capacity(db, symbol, moment, limit or 10, theme_limit=theme_limit,
                unclassified_policy=effective_theme_unclassified_policy(settings, 'paper'))

    def assert_paper_theme_dispatch(self, mode, symbol, intent_id):
        """Queue-time check of the same committed reservation; no new intent."""
        settings = self.settings_loader()
        limit = effective_theme_cap(settings, mode)
        position_limit = effective_paper_scan(settings, mode)['max_positions']
        if not limit and not position_limit:
            return
        if self.path.is_symlink() or not self.path.is_file():
            raise ThemeConcentrationUnavailable('테마 집중도 대사 원장을 확인하지 못했습니다.')
        with sqlite3.connect(self.path.resolve().as_uri()+'?mode=ro', uri=True, timeout=_SQLITE_TIMEOUT_SECONDS) as db:
            db.row_factory = sqlite3.Row; install_evidence_view(db);db.execute('PRAGMA query_only=ON'); db.execute('BEGIN')
            row = db.execute('SELECT mode,side,symbol,state FROM intents WHERE scope=? AND id=?',
                             (self.scope,intent_id)).fetchone()
            if row is None or tuple(row) != ('paper','buy',symbol,'prepared'):
                raise ThemeConcentrationUnavailable('동일 매수 의도의 테마 대사를 확인하지 못했습니다.')
            return self._assert_position_capacity(db,symbol,self._moment(),position_limit or 10,
                ignored_intent_id=intent_id,theme_limit=limit,
                unclassified_policy=effective_theme_unclassified_policy(settings, mode))

    @staticmethod
    def _validate_b3_claim_schema(db):
        cols = tuple(row[1] for row in db.execute("PRAGMA table_info(b3_add_claims)"))
        indexes = set()
        for row in db.execute("PRAGMA index_list(b3_add_claims)"):
            if row[2]:
                name = row[1]
                indexes.add(tuple(x[2] for x in db.execute("SELECT seqno,cid,name FROM pragma_index_info(?) ORDER BY seqno", (name,))))
        triggers = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='b3_add_claims'")}
        if (cols != ('scope','initial_intent_id','add_intent_id','claimed_at') or
                ('scope','initial_intent_id') not in indexes or ('scope','add_intent_id') not in indexes or
                not {'b3_add_claims_no_update','b3_add_claims_no_delete'} <= triggers):
            raise ExecutionLogError('분할매수 예약 원장 구조를 확인하지 못했습니다.')
        invalidation_cols=tuple(row[1] for row in db.execute('PRAGMA table_info(b3_invalidations)'))
        invalidation_indexes=set()
        for row in db.execute('PRAGMA index_list(b3_invalidations)'):
            if row[2]:
                invalidation_indexes.add(tuple(x[2] for x in db.execute('SELECT seqno,cid,name FROM pragma_index_info(?) ORDER BY seqno',(row[1],))))
        invalidation_triggers={row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='b3_invalidations'")}
        if (invalidation_cols != ('scope','initial_intent_id','symbol','observed_at','reason') or
            ('scope','initial_intent_id') not in invalidation_indexes or
            not {'b3_invalidations_no_update','b3_invalidations_no_delete'}<=invalidation_triggers):
            raise ExecutionLogError('분할매수 무효 원장 구조를 확인하지 못했습니다.')

    def _b3_candidate(self, db, mode, symbol, now):
        """Return a fully observed, same-day initial lot; no inferred holdings."""
        if (mode != 'paper' or not isinstance(symbol,str) or re.fullmatch(r'[0-9]{6}',symbol) is None
                or not isinstance(now,datetime) or now.tzinfo is None or now.utcoffset() is None):
            return None
        self._validate_daily_ledger(db)
        self._validate_b3_claim_schema(db)
        day=now.astimezone(KST).strftime('%Y%m%d')
        self._assert_no_pending_order(db,mode,symbol)
        rows=db.execute("""SELECT i.*,d.evidence_json,d.evidence_sha256 FROM intents i
            JOIN order_decisions d ON d.scope=i.scope AND d.intent_id=i.id
            WHERE i.scope=? AND i.day=? AND i.mode=? AND i.symbol=? AND i.side='buy'
              AND i.strategy='gdcrs' AND i.state='accepted' ORDER BY i.created_at DESC,i.id DESC LIMIT 10001""",
            (self.scope,day,mode,symbol)).fetchall()
        if len(rows)>10000:return None
        initial=None; config=None
        for row in rows:
            raw=row['evidence_json']
            if not isinstance(raw,str) or hashlib.sha256(raw.encode('utf-8')).hexdigest()!=row['evidence_sha256']:return None
            evidence=json.loads(raw); cfg=evidence.get('config')
            if evidence.get('dynamic_s1_admission') is not None or evidence.get('strategy_version')=='completed-minute-cross-funnel-v1':
                evidence=freeze_evidence(evidence).as_dict()
                if evidence['dynamic_s1_admission']['symbol']!=symbol:return None
            if isinstance(cfg,dict) and cfg.get('b3_stage')=='initial':
                initial=row;config=cfg;break
        if initial is None or not re.fullmatch(r'[0-9a-f]{32}',initial['id'] or ''):return None
        if (db.execute('SELECT 1 FROM b3_add_claims WHERE scope=? AND initial_intent_id=?',(self.scope,initial['id'])).fetchone()
            or db.execute('SELECT 1 FROM b3_invalidations WHERE scope=? AND initial_intent_id=?',(self.scope,initial['id'])).fetchone()):return None
        if (type(config.get('b3_initial_entry_pct')) is not int or not 1<=config['b3_initial_entry_pct']<=99
                or type(config.get('b3_add_on_profit_bps')) is not int or not 0<=config['b3_add_on_profit_bps']<=10000
                or type(config.get('b3_total_budget_krw')) is not int or not 1<=config['b3_total_budget_krw']<=1_000_000
                or config.get('b3_initial_intent_id') is not None):return None
        order=db.execute('SELECT * FROM orders WHERE scope=? AND day=? AND mode=? AND ord_no=?',
                         (self.scope,day,mode,initial['order_number'])).fetchone()
        if order is None or (order['symbol'],order['side'],order['quantity'],order['strategy'])!=(symbol,'buy',initial['quantity'],'gdcrs'):return None
        expected_quantity=order['quantity']-self._confirmed_cancelled_quantity(db,order)
        if expected_quantity<=0:return None
        started=datetime.fromisoformat(order['registered_at'])
        created=datetime.fromisoformat(initial['created_at'])
        if started.tzinfo is None or created.tzinfo is None or not created<=started<=now:return None
        for row in db.execute("SELECT id,state,created_at FROM intents WHERE scope=? AND mode=? AND symbol=?",(self.scope,mode,symbol)):
            at=datetime.fromisoformat(row['created_at'])
            if at.tzinfo is None or at>now:return None
            if row['id']!=initial['id'] and row['state']!='cancelled' and at>=created:return None
        for row in db.execute("SELECT day,ord_no,registered_at FROM orders WHERE scope=? AND mode=? AND symbol=?",(self.scope,mode,symbol)):
            at=datetime.fromisoformat(row['registered_at'])
            if at.tzinfo is None or at>now:return None
            if (row['day'],row['ord_no'])!=(day,initial['order_number']) and at>=created:return None
        fills=[dict(r) for r in db.execute('SELECT * FROM fills WHERE scope=? AND day=? AND mode=? AND ord_no=?',
                                             (self.scope,day,mode,initial['order_number']))]
        obs=db.execute("SELECT id,digest,payload_json FROM broker_observation_evidence WHERE scope=? AND day=? AND mode=? AND source_api='00' ORDER BY id LIMIT 20001",(self.scope,day,mode)).fetchall()
        if len(obs)>20000:return None
        from .cancellation_settlement import confirmed_cancel_order_numbers
        cancel_numbers=confirmed_cancel_order_numbers(db,self.scope,day,mode,symbol)
        parsed=[];stream_starts=[];stream_breaks=[]
        for row in obs:
            raw=row['payload_json']
            if not isinstance(raw,str) or hashlib.sha256(raw.encode('utf-8')).hexdigest()!=row['digest']:return None
            event=json.loads(raw);at=datetime.fromisoformat(event['observed_at'])
            if (at.tzinfo is None or at>now or event.get('day')!=day or event.get('mode')!=mode
                    or event.get('source_api')!='00'):return None
            if event.get('event_kind') in ('stream_started','stream_resumed') and at<=started:stream_starts.append(at)
            if event.get('event_kind') in ('stream_gap','stream_stopped') and started<=at<=now:stream_breaks.append(at)
            values=event.get('values',{})
            if (event.get('event_kind')=='execution_stream' and at>=created and isinstance(values,dict)
                    and values.get('9001') in (symbol,'A'+symbol)
                    and order_key(values.get('9203',''))!=initial['order_number']
                    and not self._confirmed_cancel_event(values,cancel_numbers,{initial['order_number']})):return None
            parsed.append(dict(row))
        if not stream_starts or stream_breaks:return None
        with localcontext() as ctx:
            ctx.prec=80
            result=_execution_accounting(day,[dict(order)],fills,parsed)['orders'][0]
        amount=_observed_number(result['verified_amount'],integer=True)
        if (result['verified_amount_source']!='00_unit_sum_reconciled_to_cumulative_quantity'
                or result['unit_coverage']!='quantity_reconciled' or result['stream_gap_observed']
                or result['issues'] or expected_quantity!=result['verified_quantity']
                or result['rest_projection_quantity']!=expected_quantity
                or amount is None or amount<=0
                or amount>config['b3_total_budget_krw']*config['b3_initial_entry_pct']//100):return None
        amount=int(amount)
        last=result.get('latest_cumulative')
        if not isinstance(last,dict) or not isinstance(last.get('observed_at'),str):return None
        observed=datetime.fromisoformat(last['observed_at'])
        if observed.tzinfo is None or not started<=observed<=now:return None
        return ({'initial_intent_id':initial['id'],'initial_fill_last_observed_at':last['observed_at'],
                 'initial_filled_qty':result['verified_quantity'],'initial_filled_principal_krw':amount},config)

    @staticmethod
    def _multi_b3_config(config):
        """Validate an opt-in scale-in contract without granting ownership."""
        if (not isinstance(config, dict) or config.get('multi_b3_stage') not in ('initial', 'add')
                or type(config.get('multi_b3_initial_entry_pct')) is not int
                or not 1 <= config['multi_b3_initial_entry_pct'] <= 99
                or type(config.get('multi_b3_add_on_profit_bps')) is not int
                or not 1 <= config['multi_b3_add_on_profit_bps'] <= 10000
                or type(config.get('multi_b3_total_budget_krw')) is not int
                or not 1 <= config['multi_b3_total_budget_krw'] <= 1_000_000
                or type(config.get('max_daily_entries_per_symbol')) is not int
                or config['max_daily_entries_per_symbol'] != 2):
            raise ExecutionLogError('다중 전략 분할매수 설정 근거를 확인하지 못했습니다.')
        ident = config.get('multi_b3_initial_intent_id')
        if (config['multi_b3_stage'] == 'initial' and ident is not None
                or config['multi_b3_stage'] == 'add' and
                (not isinstance(ident, str) or re.fullmatch(r'[0-9a-f]{32}', ident) is None)):
            raise ExecutionLogError('다중 전략 최초 매수 식별자가 잘못되었습니다.')
        return config

    def _multi_b3_candidate(self, db, mode, symbol, strategy, now, *, ignored_intent_id=None):
        """Same-day, fully observed single initial lot; never inferred inventory."""
        if (mode != 'paper' or strategy not in ('S2_surge_high_breakout', 'S3_upper_limit_continuation',
                'S4_theme_and_catalyst_leader','S5_steady_uptrend_vwap') or not isinstance(symbol, str)
                or re.fullmatch(r'[0-9]{6}', symbol) is None or not isinstance(now, datetime)
                or now.tzinfo is None or now.utcoffset() is None):
            return None
        self._validate_daily_ledger(db); self._validate_b3_claim_schema(db)
        position = self.multi_strategy_position(mode, symbol, strategy, now, _ignored_intent_id=ignored_intent_id)
        if (position.get('status') != 'owned' or position.get('overnight_unobserved_interval') is not False
                or position.get('half_take_done') is not False):
            return None
        ident = position.get('generation_id')
        day = now.astimezone(KST).strftime('%Y%m%d')
        row = db.execute('''SELECT i.*,d.evidence_json,d.evidence_sha256,d.order_price AS limit_price,d.order_type
            FROM intents i JOIN order_decisions d ON d.scope=i.scope AND d.intent_id=i.id
            WHERE i.scope=? AND i.id=?''', (self.scope, ident)).fetchone()
        if (row is None or (row['mode'], row['side'], row['symbol'], row['strategy'], row['day'], row['state'])
                != (mode, 'buy', symbol, strategy, day, 'accepted') or row['order_number'] is None):
            return None
        raw = row['evidence_json']
        if not isinstance(raw, str) or hashlib.sha256(raw.encode()).hexdigest() != row['evidence_sha256']:
            return None
        evidence = json.loads(raw)
        config = self._multi_b3_config(evidence.get('config'))
        if (config['multi_b3_stage'] != 'initial' or evidence.get('strategy') != strategy
                or evidence.get('strategy_version') not in ('paper-multi-observed-v2','paper-multi-observed-v3','paper-multi-observed-v4')):
            return None
        claim = db.execute('SELECT add_intent_id FROM b3_add_claims WHERE scope=? AND initial_intent_id=?',
                           (self.scope, ident)).fetchone()
        if ((claim is not None and claim['add_intent_id'] != ignored_intent_id)
                or db.execute('SELECT 1 FROM b3_invalidations WHERE scope=? AND initial_intent_id=?',
                              (self.scope, ident)).fetchone()):
            return None
        created = datetime.fromisoformat(row['created_at'])
        filled_at = datetime.fromisoformat(position['initial_fill_at'])
        if created.tzinfo is None or filled_at.tzinfo is None or not created <= filled_at <= now:
            return None
        # Any later accepted buy, sale or unresolved reservation closes the
        # original scale-in opportunity, even if a current balance looks alike.
        later = db.execute('''SELECT 1 FROM intents WHERE scope=? AND mode=? AND symbol=?
            AND id!=? AND (? IS NULL OR id!=?) AND state IS NOT 'cancelled'
            AND created_at>=? LIMIT 1''',
            (self.scope, mode, symbol, ident, ignored_intent_id, ignored_intent_id, row['created_at'])).fetchone()
        if later:
            return None
        qty, basis = position.get('owned_qty'), position.get('basis_krw')
        allocation = config['multi_b3_total_budget_krw'] * config['multi_b3_initial_entry_pct'] // 100
        if (type(qty) is not int or qty <= 0 or row['quantity'] != qty or type(basis) is not int or not 0 < basis <= allocation
                or row['order_type'] != 'limit' or not isinstance(row['limit_price'], str)
                or re.fullmatch(r'[0-9]{1,12}', row['limit_price']) is None
                or not 0 < int(row['limit_price']) * qty <= allocation):
            return None
        return {'initial_intent_id': ident, 'generation_id': ident,
                'initial_fill_observed_at': filled_at.isoformat(), 'initial_filled_qty': qty,
                'initial_filled_principal_krw': basis, 'config': config}

    def multi_b3_add_candidate(self, mode, symbol, strategy, now):
        """Read-only eligibility; order preparation must claim this lot again."""
        if self.path.is_symlink() or not self.path.is_file():
            return None
        try:
            with sqlite3.connect(self.path.resolve().as_uri()+'?mode=ro', uri=True, timeout=_SQLITE_TIMEOUT_SECONDS) as db:
                db.row_factory=sqlite3.Row; install_evidence_view(db);db.execute('PRAGMA query_only=ON'); db.execute('BEGIN')
                return self._multi_b3_candidate(db, mode, symbol, strategy, now)
        except (sqlite3.Error, ExecutionLogError, ExecutionsError, ValueError, KeyError, TypeError, OverflowError, OSError):
            return None

    def _assert_multi_b3_buy(self, db, mode, symbol, strategy, now, config, evidence, proof,
                             quantity, price, *, ignored_intent_id=None):
        """Independent amount/lot checks after immutable signal validation."""
        config = self._multi_b3_config(config)
        if (mode != 'paper' or strategy not in ('S2_surge_high_breakout', 'S3_upper_limit_continuation',
                'S4_theme_and_catalyst_leader','S5_steady_uptrend_vwap') or evidence.get('strategy_version') not in ('paper-multi-observed-v2','paper-multi-observed-v3','paper-multi-observed-v4')
                or not isinstance(proof, dict) or type(quantity) is not int or quantity <= 0
                or type(price) is not int or price <= 0):
            raise ExecutionLogError('다중 전략 분할매수 주문 근거가 잘못되었습니다.')
        pct, budget = config['multi_b3_initial_entry_pct'], config['multi_b3_total_budget_krw']
        signal = evidence.get('multi_strategy_signal')
        if not isinstance(signal, dict) or signal.get('side') != 'buy' or signal.get('symbol') != symbol:
            raise ExecutionLogError('다중 전략 분할매수 종목 근거가 없습니다.')
        if config['multi_b3_stage'] == 'initial':
            if (proof.get('pre_order_held_qty') != 0 or proof.get('pre_order_pur_amt_krw') != 0
                    or quantity * price > budget * pct // 100):
                raise ExecutionLogError('첫 분할매수의 빈 잔고 또는 비중 한도를 확인하지 못했습니다.')
            return None
        candidate = self._multi_b3_candidate(db, mode, symbol, strategy, now, ignored_intent_id=ignored_intent_id)
        if candidate is None:
            raise ExecutionLogError('추가매수할 동일 전략 최초 체결을 확인하지 못했습니다.')
        prior = candidate['config']; initial = candidate['initial_intent_id']
        keys = ('multi_b3_initial_entry_pct', 'multi_b3_add_on_profit_bps', 'multi_b3_total_budget_krw',
                'max_daily_entries_per_symbol')
        addon, position = signal.get('b3_add_on'), signal.get('position')
        qty, basis = candidate['initial_filled_qty'], candidate['initial_filled_principal_krw']
        if (config['multi_b3_initial_intent_id'] != initial or any(prior.get(k) != config.get(k) for k in keys)
                or not isinstance(addon, dict) or addon.get('initial_fill_observed_at') != candidate['initial_fill_observed_at']
                or not isinstance(position, dict) or position.get('generation_id') != initial
                or position.get('owned_qty') != qty or position.get('basis_krw') != basis
                or position.get('half_take_done') is not False or proof.get('pre_order_held_qty') != qty
                or proof.get('pre_order_pur_amt_krw') != basis
                or datetime.fromisoformat(proof['observed_at']) < datetime.fromisoformat(candidate['initial_fill_observed_at'])
                or quantity * price > budget * (100-pct) // 100 or basis + quantity * price > budget
                or price * qty * 10000 < basis * (10000 + config['multi_b3_add_on_profit_bps'])):
            raise ExecutionLogError('다중 전략 추가매수의 최초 체결·잔고·수익 구간 대사가 일치하지 않습니다.')
        return initial

    def multi_b3_invalidate_candidate(self, mode, symbol, strategy, initial_intent_id, observed_at,
                                      reason='negative_sma_observed'):
        """Persist an observed negative-trend veto for the same initial lot."""
        now = self._moment()
        if (mode != 'paper' or strategy not in ('S2_surge_high_breakout', 'S3_upper_limit_continuation',
                'S4_theme_and_catalyst_leader','S5_steady_uptrend_vwap') or not isinstance(initial_intent_id, str)
                or re.fullmatch(r'[0-9a-f]{32}', initial_intent_id) is None
                or reason not in ('dead_cross', 'negative_sma_observed') or not isinstance(observed_at, datetime)
                or observed_at.tzinfo is None or observed_at.utcoffset() is None or observed_at > now):
            raise ExecutionLogError('다중 전략 분할매수 무효화 근거가 잘못되었습니다.')
        with self._db(require_existing_daily_ledger=True) as db:
            if not db.in_transaction: db.execute('BEGIN IMMEDIATE')
            self._validate_b3_claim_schema(db)
            row = db.execute('SELECT * FROM intents WHERE scope=? AND id=?', (self.scope, initial_intent_id)).fetchone()
            if (row is None or (row['mode'], row['side'], row['symbol'], row['strategy'], row['state'])
                    != (mode, 'buy', symbol, strategy, 'accepted')
                    or row['day'] != observed_at.astimezone(KST).strftime('%Y%m%d')
                    or datetime.fromisoformat(row['created_at']) > observed_at):
                raise ExecutionLogError('무효화할 동일 전략 최초 매수 의도가 없습니다.')
            db.execute('INSERT OR IGNORE INTO b3_invalidations VALUES (?,?,?,?,?)',
                       (self.scope, initial_intent_id, symbol, observed_at.isoformat(), reason))

    def b3_add_candidate(self, mode, symbol, now):
        """Read-only preliminary check; final check and durable claim occur together."""
        if self.path.is_symlink() or not self.path.is_file():return None
        try:
            with sqlite3.connect(self.path.resolve().as_uri()+'?mode=ro',uri=True,timeout=_SQLITE_TIMEOUT_SECONDS) as db:
                db.row_factory=sqlite3.Row;install_evidence_view(db);db.execute('PRAGMA query_only=ON');db.execute('BEGIN')
                value=self._b3_candidate(db,mode,symbol,now)
                return None if value is None else value[0]
        except (sqlite3.Error,ExecutionLogError,ExecutionsError,ValueError,KeyError,TypeError,OverflowError,OSError):
            return None

    def b3_add_candidate_status(self, mode, symbol, now):
        """Read-only diagnostic; never substitutes for atomic order preparation."""
        def output(status, candidate=None):return {'status':status,'candidate':candidate}
        if (mode!='paper' or not isinstance(symbol,str) or re.fullmatch(r'[0-9]{6}',symbol) is None
                or not isinstance(now,datetime) or now.tzinfo is None or now.utcoffset() is None
                or self.path.is_symlink() or not self.path.is_file()):return output('unavailable')
        try:
            with sqlite3.connect(self.path.resolve().as_uri()+'?mode=ro',uri=True,timeout=_SQLITE_TIMEOUT_SECONDS) as db:
                db.row_factory=sqlite3.Row;install_evidence_view(db);db.execute('PRAGMA query_only=ON');db.execute('BEGIN')
                self._validate_daily_ledger(db);self._validate_b3_claim_schema(db)
                try:self._assert_no_pending_order(db,mode,symbol)
                except ExecutionLogError:return output('pending')
                result=self._b3_candidate(db,mode,symbol,now)
                if result is not None:return output('candidate',result[0])
                day=now.astimezone(KST).strftime('%Y%m%d')
                rows=db.execute("""SELECT i.id,d.evidence_json,d.evidence_sha256 FROM intents i
                   JOIN order_decisions d ON d.scope=i.scope AND d.intent_id=i.id
                   WHERE i.scope=? AND i.day=? AND i.mode=? AND i.symbol=? AND i.side='buy'
                   AND i.strategy='gdcrs' AND i.state='accepted' ORDER BY i.created_at DESC,i.id DESC LIMIT 10001""",
                   (self.scope,day,mode,symbol)).fetchall()
                if len(rows)>10000:return output('unavailable')
                for row in rows:
                    raw=row['evidence_json']
                    if not isinstance(raw,str) or hashlib.sha256(raw.encode('utf-8')).hexdigest()!=row['evidence_sha256']:
                        return output('unavailable')
                    data=json.loads(raw);cfg=data.get('config')
                    if isinstance(cfg,dict) and cfg.get('b3_stage')=='initial':
                        # A fully unit-reconciled closed lot is not a broken
                        # add-on basis. This is a hint only: a new initial buy
                        # must still obtain fresh broker-zero reconciliation.
                        if self.multi_strategy_position(mode, symbol, 'gdcrs', now).get('status') == 'flat':
                            return output('lot_closed')
                        if db.execute('SELECT 1 FROM b3_invalidations WHERE scope=? AND initial_intent_id=?',(self.scope,row['id'])).fetchone():return output('invalidated')
                        if db.execute('SELECT 1 FROM b3_add_claims WHERE scope=? AND initial_intent_id=?',(self.scope,row['id'])).fetchone():return output('claimed')
                        return output('unavailable')
                return output('no_initial')
        except (sqlite3.Error,ExecutionLogError,ExecutionsError,ValueError,KeyError,TypeError,OverflowError,OSError):
            return output('unavailable')

    def b3_monitor_candidates(self, mode, now):
        """Read-only same-day owned B3 lots, independent of the changing hot list.

        A returned candidate is only a monitoring hint. The existing atomic
        preparation repeats original-fill, remaining-principal and lot checks.
        """
        empty={'status':'unavailable','candidates':{}}
        if (mode!='paper' or not isinstance(now,datetime) or now.tzinfo is None
                or now.utcoffset() is None or self.path.is_symlink() or not self.path.is_file()):return empty
        try:
            with sqlite3.connect(self.path.resolve().as_uri()+'?mode=ro',uri=True,timeout=_SQLITE_TIMEOUT_SECONDS) as db:
                db.row_factory=sqlite3.Row;install_evidence_view(db);db.execute('PRAGMA query_only=ON');db.execute('BEGIN')
                self._validate_daily_ledger(db);self._validate_b3_claim_schema(db)
                rows=db.execute("""SELECT DISTINCT symbol FROM intents WHERE scope=? AND day=? AND mode='paper'
                    AND strategy='gdcrs' AND side='buy' AND state='accepted' ORDER BY symbol LIMIT 101""",
                    (self.scope,now.astimezone(KST).strftime('%Y%m%d'))).fetchall()
                if len(rows)>100:return empty
                candidates={}
                for row in rows:
                    symbol=row['symbol']
                    try:self._assert_no_pending_order(db,mode,symbol)
                    except ExecutionLogError:continue
                    result=self._b3_candidate(db,mode,symbol,now)
                    if result is not None:candidates[symbol]={'candidate':result[0],'config':result[1]}
                return {'status':'available','candidates':candidates}
        except (sqlite3.Error,ExecutionLogError,ExecutionsError,ValueError,KeyError,TypeError,OverflowError,OSError):
            return empty

    def assert_b3_add_dispatch(self, mode, symbol, intent_id):
        """Recheck committed reservation and invalidation immediately before send."""
        if (mode!='paper' or not isinstance(symbol,str) or re.fullmatch(r'[0-9]{6}',symbol) is None
                or not isinstance(intent_id,str) or re.fullmatch(r'[0-9a-f]{32}',intent_id) is None
                or self.path.is_symlink() or not self.path.is_file()):
            raise ExecutionLogError('추가매수 전송 직전 예약을 확인하지 못했습니다.')
        try:
            with sqlite3.connect(self.path.resolve().as_uri()+'?mode=ro',uri=True,timeout=_SQLITE_TIMEOUT_SECONDS) as db:
                db.row_factory=sqlite3.Row;install_evidence_view(db);db.execute('PRAGMA query_only=ON');db.execute('BEGIN')
                self._validate_daily_ledger(db);self._validate_b3_claim_schema(db)
                row=db.execute("""SELECT i.mode,i.side,i.symbol,i.strategy,i.state,i.day,d.evidence_json,d.evidence_sha256,
                     c.initial_intent_id FROM intents i JOIN order_decisions d ON d.scope=i.scope AND d.intent_id=i.id
                     JOIN b3_add_claims c ON c.scope=i.scope AND c.add_intent_id=i.id
                     WHERE i.scope=? AND i.id=?""",(self.scope,intent_id)).fetchone()
                if row is None or (row['mode'],row['side'],row['symbol'],row['strategy'],row['state'])!=(mode,'buy',symbol,'gdcrs','prepared'):
                    raise ValueError
                raw=row['evidence_json'];cfg=json.loads(raw)['config']
                if (hashlib.sha256(raw.encode('utf-8')).hexdigest()!=row['evidence_sha256'] or
                    cfg.get('b3_stage')!='add' or cfg.get('b3_initial_intent_id')!=row['initial_intent_id'] or
                    db.execute('SELECT 1 FROM b3_invalidations WHERE scope=? AND initial_intent_id=?',(self.scope,row['initial_intent_id'])).fetchone()):
                    raise ValueError
                initial=db.execute('SELECT created_at,day,state,order_number FROM intents WHERE scope=? AND id=?',
                    (self.scope,row['initial_intent_id'])).fetchone()
                if initial is None or initial['state']!='accepted' or initial['day']!=row['day'] or initial['order_number'] is None:raise ValueError
                start=datetime.fromisoformat(initial['created_at']);now=self._moment()
                if start.tzinfo is None or start>now:raise ValueError
                events=db.execute("SELECT digest,payload_json FROM broker_observation_evidence WHERE scope=? AND mode=? AND day=? AND source_api='00' ORDER BY id LIMIT 20001",
                                  (self.scope,mode,row['day'])).fetchall()
                if len(events)>20000:raise ValueError
                for event in events:
                    raw_event=event['payload_json']
                    if hashlib.sha256(raw_event.encode('utf-8')).hexdigest()!=event['digest']:raise ValueError
                    data=json.loads(raw_event);at=datetime.fromisoformat(data['observed_at'])
                    if at.tzinfo is None or at>now:raise ValueError
                    if start<=at<=now and data.get('event_kind') in ('stream_gap','stream_stopped'):raise ValueError
                    values=data.get('values',{})
                    if (at>=start and data.get('event_kind')=='execution_stream' and isinstance(values,dict)
                            and values.get('9001') in (symbol,'A'+symbol)
                            and order_key(values.get('9203',''))!=initial['order_number']):raise ValueError
        except (sqlite3.Error,ExecutionLogError,ExecutionsError,ValueError,KeyError,TypeError,OverflowError,OSError):
            raise ExecutionLogError('추가매수 예약 또는 실시간 체결 연속성을 확인하지 못했습니다.') from None

    def b3_invalidate_candidate(self, mode, symbol, initial_intent_id, observed_at, reason):
        """Permanently close an initial lot to scale-in after an observed cross."""
        now=self._moment()
        if (mode!='paper' or not isinstance(symbol,str) or re.fullmatch(r'[0-9]{6}',symbol) is None
                or not isinstance(initial_intent_id,str) or re.fullmatch(r'[0-9a-f]{32}',initial_intent_id) is None
                or reason not in ('dead_cross','negative_sma_observed') or not isinstance(observed_at,datetime) or observed_at.tzinfo is None
                or observed_at.utcoffset() is None or observed_at>now):
            raise ExecutionLogError('분할매수 무효화 근거를 확인하지 못했습니다.')
        with self._db() as db:
            if not db.in_transaction:db.execute('BEGIN IMMEDIATE')
            self._validate_b3_claim_schema(db)
            row=db.execute("SELECT mode,side,symbol,strategy,state,created_at FROM intents WHERE scope=? AND id=?",(self.scope,initial_intent_id)).fetchone()
            if (row is None or (row['mode'],row['side'],row['symbol'],row['strategy'],row['state'])!=(mode,'buy',symbol,'gdcrs','accepted')
                    or datetime.fromisoformat(row['created_at'])>observed_at):
                raise ExecutionLogError('무효화할 최초 매수 의도를 확인하지 못했습니다.')
            db.execute('INSERT OR IGNORE INTO b3_invalidations VALUES (?,?,?,?,?)',
                       (self.scope,initial_intent_id,symbol,observed_at.isoformat(),reason))

    @staticmethod
    def _assert_experiment_admission(mode, side, symbol, strategy, evidence, now):
        from .paper_experiment_admission import ARMS, validate_admission
        if strategy not in ARMS or side != 'buy':
            return
        try:
            if (mode != 'paper' or not isinstance(evidence,dict)
                    or evidence.get('source_kind') != 'broker_observation'):
                raise ValueError
            validate_admission((evidence.get('config') or {}).get('experiment_extended_admission'),
                               symbol,strategy,now)
        except (ValueError,TypeError,KeyError,OverflowError):
            raise ExecutionLogError('신규 arm의 보통주·유동성·VI·상한가 증빙 연결을 확인하지 못했습니다.') from None

    @staticmethod
    def _assert_current_multi_entry_policy(mode, side, strategy, evidence, now):
        if strategy == 'S5_steady_uptrend_vwap':
            if side != 'buy':
                return  # Sells use the independent ownership/first-fill gate.
            data=evidence if isinstance(evidence,dict) else {}
            signal=data.get('multi_strategy_signal') or {}
            try:
                if data.get('strategy_version')=='paper-experiment-v1':
                    from .paper_experiment_s5 import validate_s5_native_evidence
                    cfg=data['config']; native=cfg['experiment_s5_signal']
                    observed=datetime.fromisoformat(data['observed_at'])
                    evaluated=datetime.fromisoformat(data['evaluated_at'])
                    if (mode!='paper' or data.get('strategy')!=strategy or data.get('source_kind')!='broker_observation'
                            or not observed<=evaluated<=now or not 0<=(now-observed).total_seconds()<=5):raise ValueError
                    freeze_evidence(data)
                    validate_s5_native_evidence(native,symbol=native['symbol'],now=now,
                                               signal_at=cfg['experiment_signal_at'])
                    return
                config=ExecutionTracker._multi_b3_config(data.get('config'))
                observed=datetime.fromisoformat(data['observed_at'])
                evaluated=datetime.fromisoformat(data['evaluated_at'])
                if (mode!='paper' or side!='buy' or data.get('strategy')!=strategy
                        or config['multi_b3_initial_entry_pct']!=50
                        or config['multi_b3_add_on_profit_bps']!=50
                        or data.get('strategy_version')!='paper-multi-observed-v4'
                        or data.get('source_kind')!='broker_observation'
                        or signal.get('side')!='buy' or signal.get('reason') not in ('steady_uptrend_vwap','add_on_strength')
                        or observed.tzinfo is None or evaluated.tzinfo is None
                        or not observed<=evaluated<=now or not 0<=(now-observed).total_seconds()<=5):
                    raise ValueError
                freeze_evidence(data)  # Independently revalidate the compact S5 contract.
                risk=signal['s5_input']['risk_context']
                from .steady_uptrend_vwap import validate_s5_risk
                current_risk=validate_s5_risk(current_price=signal['market']['current_price'],
                    daily_context=signal['s5_input']['daily_context'],risk_context=risk,
                    symbol=signal['symbol'],now=now,require_short=True)
                if current_risk.get('eligible') is not True:raise ValueError
                expiry=datetime.fromisoformat(risk['expires_at'])
                if expiry.tzinfo is None or not now<expiry:
                    raise ValueError
            except (ValueError,TypeError,KeyError,OverflowError):
                raise ExecutionLogError('S5 매수의 현재 관측·패턴·VWAP 증빙을 확인하지 못했습니다.') from None
            return
        # Historical v1/v2 evidence remains readable for ownership and exits;
        # it cannot authorize a new S2 initial entry without observed day open.
        if mode != 'paper' or side != 'buy' or strategy != 'S2_surge_high_breakout':
            return
        data = evidence if isinstance(evidence, dict) else {}
        config = data.get('config') or {}
        if config.get('multi_b3_stage') == 'add':
            return  # Independently proven existing-lot B3 rules apply below.
        from kiwoom_autotrader.trading.multi_strategy import s2_momentum_eligible
        signal = data.get('multi_strategy_signal') or {}
        if (data.get('strategy_version') != 'paper-multi-observed-v3'
                or not s2_momentum_eligible(signal.get('market'), now)):
            raise ExecutionLogError('신규 S2 매수에 필요한 현재 양봉·등락률 증빙이 없습니다.')

    def _assert_dynamic_s1_entry(self, mode, side, symbol, strategy, evidence, now, *, price, quantity,
                                 reconciliation_evidence):
        """A hot-list name cannot enter through legacy evidence or a wider budget."""
        data=evidence if isinstance(evidence,dict) else {}
        admission=data.get('dynamic_s1_admission')
        marker=data.get('strategy_version')=='completed-minute-cross-funnel-v1'
        if mode!='paper' or side!='buy' or strategy!='gdcrs':
            if admission is not None or marker:raise ExecutionLogError('동적 S1은 모의 신규 매수에만 허용합니다.')
            return
        settings=self.settings_loader()
        cfg=data.get('config') or {}
        from .dynamic_s1_admission import validate_dynamic_admission,validate_dynamic_s1_policy
        from kiwoom_autotrader.config.settings import effective_paper_funnel
        fixed=settings.get('gdcrs_stocks',[])
        dynamic=(effective_paper_funnel(settings,'paper')['enabled'] and
                 not any(isinstance(row,dict) and row.get('code')==symbol for row in fixed))
        # Adds are not new entries. Their original accepted intent and fully
        # observed fills are independently validated below inside the write lock.
        if dynamic and cfg.get('b3_stage')=='add' and admission is None and not marker:
            try:validate_dynamic_s1_policy(settings,cfg,symbol)
            except ValueError:raise ExecutionLogError('동적 S1 추가매수 정책이 변경되었습니다.') from None
            return
        if not (dynamic or admission is not None or marker):return
        try:
            if not marker or admission is None:raise ValueError
            validate_dynamic_s1_policy(settings,cfg,symbol)
            validate_dynamic_admission(admission,now=now,symbol=symbol)
            proof=reconciliation_evidence
            if (data.get('budget')!=500000 or cfg.get('b3_stage')!='initial'
                    or not isinstance(proof,dict) or proof.get('schema_version')!=5
                    or proof.get('pre_order_held_qty')!=0 or proof.get('pre_order_pur_amt_krw')!=0
                    or price is None or type(quantity) is not int or quantity<=0
                    or not 0<quantity*int(price)<=500000):raise ValueError
        except (ValueError,KeyError,TypeError,OverflowError):
            raise ExecutionLogError('동적 S1 편입·50만원 최초 한도·0주 대사 근거를 확인하지 못했습니다.') from None

    def _assert_s5_initial_boundary_unclaimed(self, db, mode, symbol, moment, data):
        """One initial S5 intent per observed boundary, including after restart.

        Called under prepare_order's existing IMMEDIATE transaction. Existing
        immutable decision rows are the claim; no schema change or replay is
        needed. A rejected/unsent attempt also consumes its signal boundary.
        """
        try:
            boundary = moment.replace(minute=moment.minute // 5 * 5, second=0, microsecond=0)
            signal_at = (data['config']['experiment_s5_signal']['signal_at']
                         if data.get('strategy_version')=='paper-experiment-v1'
                         else data['multi_strategy_signal']['s5_input']['evaluation']['signal_at'])
            if mode != 'paper' or datetime.fromisoformat(signal_at) != boundary:
                raise ValueError
            rows = db.execute('''SELECT d.evidence_json,d.evidence_sha256 FROM intents i
                LEFT JOIN order_decisions d ON d.scope=i.scope AND d.intent_id=i.id
                WHERE i.scope=? AND i.day=? AND i.mode=? AND i.symbol=?
                AND i.side='buy' AND i.strategy='S5_steady_uptrend_vwap' LIMIT 1001''',
                (self.scope, moment.strftime('%Y%m%d'), mode, symbol)).fetchall()
            if len(rows) > 1000:
                raise ValueError
            for row in rows:
                raw = row['evidence_json']
                if (not isinstance(raw, str) or len(raw.encode()) > 65536
                        or hashlib.sha256(raw.encode()).hexdigest() != row['evidence_sha256']):
                    raise ValueError
                prior = json.loads(raw)
                if prior.get('strategy_version')=='paper-experiment-v1':
                    previous=prior['config']['experiment_s5_signal']['signal_at']
                else:
                    stage = prior['config']['multi_b3_stage']
                    if stage == 'add':continue
                    if stage != 'initial':raise ValueError
                    previous = prior['multi_strategy_signal']['s5_input']['evaluation']['signal_at']
                if datetime.fromisoformat(previous) == boundary:
                    raise ExecutionLogError('s5_boundary_already_claimed: 동일 5분 경계의 S5 최초 매수는 이미 기록되었습니다.')
        except (KeyError, TypeError, ValueError, OverflowError):
            raise ExecutionLogError('S5 현재 경계 또는 기존 신호 원장의 무결성을 확인하지 못했습니다.') from None

    def prepare_order(self, mode, side, symbol, quantity, strategy, *, evidence=None, price=UNSPECIFIED_PRICE,
                      require_no_pending=False, reconciliation_evidence=None):
        """Commit an auto-order intent before dispatch; failure must prevent send."""
        quantity = str(quantity) if type(quantity) is int else quantity
        if (mode not in ("paper", "real") or side not in ("buy", "sell")
                or not isinstance(symbol, str) or re.fullmatch(r"[0-9]{6}", symbol) is None
                or not isinstance(quantity, str) or re.fullmatch(r"[0-9]{1,12}", quantity) is None or int(quantity) <= 0
                or not isinstance(strategy, str) or not strategy.strip() or len(strategy) > 80 or not strategy.isprintable()):
            raise ExecutionLogError("자동주문 사전 기록 정보를 확인하지 못했습니다.")
        if strategy in EXPERIMENT_STRATEGIES and ((strategy=='EXP_ETF_B') != (symbol in EXPERIMENT_ETF_CODES)):
            raise ExecutionLogError('ETF 실험은 지정된 두 종목에만 적용합니다.')
        frozen = freeze_evidence(evidence)
        native_s5=(strategy=='S5_steady_uptrend_vwap' and frozen is not None
                   and frozen.as_dict().get('strategy_version')=='paper-experiment-v1')
        if native_s5 and side=='buy':
            profile=effective_paper_experiment(self.settings_loader(),mode)
            data=frozen.as_dict();cfg=data['config']
            if (not profile.get('s5_enabled') or cfg['experiment_s5_signal']['symbol']!=symbol
                    or cfg['experiment_assignment']['symbol']!=symbol
                    or symbol in EXPERIMENT_ETF_CODES):
                raise ExecutionLogError('S5 모의 실험 진입 설정 또는 종목 근거가 일치하지 않습니다.')
        if frozen is not None and frozen.as_dict()["strategy"] not in (None, strategy):
            raise ExecutionLogError("자동주문 전략과 판단 근거가 일치하지 않습니다.")
        order_type = "unknown" if price is UNSPECIFIED_PRICE else "market" if price is None else "limit"
        if order_type == "limit":
            price = str(price) if type(price) is int else price
            if (not isinstance(price, str) or re.fullmatch(r"[0-9]{1,12}", price) is None or int(price) <= 0
                    or side != "buy"):
                raise ExecutionLogError("자동주문 지정가를 확인하지 못했습니다.")
            price = str(int(price))
        else:
            price = None
        moment = self._moment()
        self._assert_experiment_admission(mode,side,symbol,strategy,
                                          frozen.as_dict() if frozen is not None else None,moment)
        self._assert_current_multi_entry_policy(mode, side, strategy,
                                               frozen.as_dict() if frozen is not None else None, moment)
        self._assert_dynamic_s1_entry(mode,side,symbol,strategy,
            frozen.as_dict() if frozen is not None else None,moment,price=price,quantity=int(quantity),
            reconciliation_evidence=reconciliation_evidence)
        reconciliation_json = None
        require_account_clear = False
        if reconciliation_evidence is not None:
            value = reconciliation_evidence
            if isinstance(value,dict) and value.get('schema_version') in (4,7):
                fields4={'schema_version','symbol','pre_order_held_qty','pre_order_pur_amt_krw','order_quantity','observed_at',
                         'holdings_source','generation_id','exit_kind','strategy','symbol_ledger_sha256','holding_observation_id'}
                try:
                    version=value['schema_version']
                    fields7=fields4|{'ownership_status','profit_attribution','exit_only_broker_proof'}
                    observed=datetime.fromisoformat(value['observed_at'])
                    if (set(value)!=(fields7 if version==7 else fields4) or mode!='paper' or side!='sell' or value['symbol']!=symbol
                            or value['strategy']!=strategy or value['holdings_source']!='broker_observation'
                            or value['exit_kind'] not in ('half','full') or not isinstance(value['generation_id'],str)
                            or re.fullmatch(r'[0-9a-f]{32}',value['generation_id']) is None
                            or not isinstance(value['symbol_ledger_sha256'],str)
                            or re.fullmatch(r'[0-9a-f]{64}',value['symbol_ledger_sha256']) is None
                            or not isinstance(value['holding_observation_id'],str)
                            or re.fullmatch(r'[0-9a-f]{32}',value['holding_observation_id']) is None
                            or any(type(value[k]) is not int or value[k]<=0 for k in ('pre_order_held_qty','pre_order_pur_amt_krw','order_quantity'))
                            or value['order_quantity']!=int(quantity) or int(quantity)>value['pre_order_held_qty']
                            or (value['exit_kind']=='half' and int(quantity)>=value['pre_order_held_qty'])
                            or (value['exit_kind']=='full' and int(quantity)!=value['pre_order_held_qty'])
                            or observed.tzinfo is None or observed.utcoffset() is None or observed>moment):raise ValueError
                    if version==7 and (value['ownership_status']!='exit_only_broker_confirmed'
                            or value['profit_attribution']!='unconfirmed' or value['exit_kind']!='full'
                            or not isinstance(value['exit_only_broker_proof'],dict)):raise ValueError
                    reconciliation_json=canonical_json(value,maximum=8192 if version==7 else 4096)
                except (ValueError,TypeError,KeyError,OverflowError):
                    raise ExecutionLogError('매도 전 소유 잔고 대사 증빙을 확인하지 못했습니다.') from None
            else:
                value = None
        if reconciliation_evidence is not None and reconciliation_json is None:
            value = reconciliation_evidence
            fields = {"schema_version", "symbol", "pre_order_held_qty", "pre_order_pur_amt_krw",
                      "remaining_principal_krw", "order_quantity", "limit_price_krw", "observed_at", "holdings_source"}
            try:
                observed = datetime.fromisoformat(value["observed_at"])
                version = value["schema_version"]
                if type(version) is not int:
                    raise ValueError
                if version == 1 and set(value) == fields:
                    symbol_cap = per_buy_cap = 1_000_000
                elif version in (3,5,6) and set(value) == fields | {"symbol_cap_krw", "per_buy_cap_krw", "multi_strategy_enabled", "symbol_ledger_sha256", "holding_observation_id"} | ({'integrated_pending_clear','pending_query_id','recorder_gap_count','recorder_observations','recorder_ingress_sequence','engine_ingress_sequence','reconciliation_completed_at','stream_anchor_id','stream_anchor_sha256','stream_anchor_observed_at'} if version in (5,6) else set()) | ({'symbol_pending_clear'} if version==6 else set()) | ({'paper_experiment_sizing'} if mode=='paper' and effective_paper_experiment(self.settings_loader(),mode)['enabled'] else set()):
                    symbol_cap, per_buy_cap = value["symbol_cap_krw"], value["per_buy_cap_krw"]
                    experimental=mode=='paper' and effective_paper_experiment(self.settings_loader(),mode)['enabled']
                    max_cap=10**12 if experimental else 1_000_000
                    if (value["multi_strategy_enabled"] is not True
                            or type(symbol_cap) is not int or not 1 <= symbol_cap <= max_cap
                            or type(per_buy_cap) is not int or not 1 <= per_buy_cap <= max_cap
                            or not isinstance(value["symbol_ledger_sha256"], str)
                            or re.fullmatch(r"[0-9a-f]{64}", value["symbol_ledger_sha256"]) is None
                            or not isinstance(value['holding_observation_id'],str)
                            or re.fullmatch(r'[0-9a-f]{32}',value['holding_observation_id']) is None):
                        raise ValueError
                elif version == 2 and set(value) in (fields | {"symbol_cap_krw", "per_buy_cap_krw"},
                        fields | {"symbol_cap_krw", "per_buy_cap_krw", "available_orderable_krw", "cost_reserve_krw"}):
                    symbol_cap, per_buy_cap = value["symbol_cap_krw"], value["per_buy_cap_krw"]
                    if (type(symbol_cap) is not int or not 1 <= symbol_cap <= 3_000_000
                            or type(per_buy_cap) is not int or not 1 <= per_buy_cap <= 3_000_000):
                        raise ValueError
                    elevated = symbol_cap > 1_000_000 or per_buy_cap > 1_000_000
                    if elevated:
                        require_account_clear = True
                        cash, reserve = value["available_orderable_krw"], value["cost_reserve_krw"]
                        principal = int(quantity) * int(price)
                        if (type(cash) is not int or type(reserve) is not int
                                or reserve != (principal * 5 + 999) // 1000
                                or cash < principal + reserve):
                            raise ValueError
                else:
                    raise ValueError
                if (not isinstance(value, dict)
                        or mode != "paper" or side != "buy" or order_type != "limit" or value["symbol"] != symbol
                        or any(type(value[k]) is not int or value[k] < 0 for k in
                               ("pre_order_held_qty", "pre_order_pur_amt_krw", "remaining_principal_krw", "order_quantity", "limit_price_krw"))
                        or value["order_quantity"] != int(quantity) or value["limit_price_krw"] != int(price)
                        or value["remaining_principal_krw"] != max(0, symbol_cap - value["pre_order_pur_amt_krw"])
                        or int(quantity) * int(price) > value["remaining_principal_krw"]
                        or int(quantity) * int(price) > per_buy_cap
                        or value["holdings_source"] not in ("broker_observation", "unknown")
                        or observed.tzinfo is None or observed.utcoffset() is None or observed > moment):
                    raise ValueError
                reconciliation_json = canonical_json(value, maximum=4096)
            except (ValueError, TypeError, KeyError, OverflowError):
                raise ExecutionLogError("주문 전 잔고와 매입원금 대사 증빙을 확인하지 못했습니다.") from None
        multi = False
        staged_strategy = strategy in ('S2_surge_high_breakout','S3_upper_limit_continuation','S4_theme_and_catalyst_leader','S5_steady_uptrend_vwap') or strategy in EXPERIMENT_STRATEGIES
        if strategy in EXPERIMENT_STRATEGIES or native_s5:
            try:
                data=frozen.as_dict() if frozen is not None else {}
                cfg=data.get('config') or {}
                if (mode!='paper' or not effective_paper_experiment(self.settings_loader(),mode)['enabled']
                        or data.get('strategy_version')!='paper-experiment-v1'
                        or not isinstance(cfg.get('experiment_signal_sha256'),str)
                        or re.fullmatch(r'[0-9a-f]{64}',cfg['experiment_signal_sha256']) is None):raise ValueError
            except Exception:
                raise ExecutionLogError('모의 실험 전략과 신호 근거가 확인되지 않습니다.') from None
        if staged_strategy and mode!='paper':
            raise ExecutionLogError('추가 전략은 모의투자에서만 허용합니다.')
        if mode == 'paper' and side == 'buy':
            try:
                from kiwoom_autotrader.trading.paper_policy import _multi_enabled, _symbol_cap, _buy_cap
                settings=self.settings_loader()
                if not isinstance(settings,dict):raise ValueError
                multi=_multi_enabled(settings) or (staged_strategy and effective_paper_experiment(settings,mode)['enabled'])
                if staged_strategy and not multi:raise ValueError
                experimental=effective_paper_experiment(settings,mode)['enabled']
                sizing=value.get('paper_experiment_sizing') if reconciliation_json is not None else None
                if experimental and isinstance(sizing,dict):
                    expected_symbol=sizing.get('effective_symbol_cap_krw')
                    expected_buy=max(300000,sizing.get('price_krw',0)) if sizing.get('one_share_exception') is True else sizing.get('budget_krw')
                else:
                    expected_symbol,expected_buy=_symbol_cap(settings),_buy_cap(settings)
                if multi and (reconciliation_json is None or value['schema_version'] not in (3,5,6)
                        or value['symbol_cap_krw']!=expected_symbol or value['per_buy_cap_krw']!=expected_buy):
                    raise ValueError
                if not multi and reconciliation_json is not None and value['schema_version'] in (3,5,6):raise ValueError
            except Exception:
                raise ExecutionLogError('다중 전략 매수 한도 또는 원장 대사를 확인하지 못했습니다.') from None
            if multi:require_no_pending=True
        b3_config = (frozen.as_dict().get('config') or {}) if frozen is not None else {}
        if mode=='paper' and side=='sell' and strategy=='ddcrs':
            try:
                from kiwoom_autotrader.trading.paper_policy import _multi_enabled
                multi=_multi_enabled(self.settings_loader())
            except Exception:raise ExecutionLogError('다중 전략 정책을 확인하지 못했습니다.') from None
        legacy_exit=(mode=='paper' and side=='sell' and strategy=='ddcrs' and multi)
        if strategy=='S5_steady_uptrend_vwap' and side=='sell':
            data=frozen.as_dict() if frozen is not None else {}
            signal=data.get('multi_strategy_signal') or {}
            if (mode!='paper' or data.get('strategy')!=strategy
                    or data.get('strategy_version') not in ('paper-multi-observed-v4','paper-experiment-v1')
                    or data.get('source_kind')!='broker_observation'
                    or (not native_s5 and signal.get('side')!='sell')
                    or (native_s5 and ('experiment_s5_exit' not in data['config']
                        or data['config']['experiment_assignment']['symbol']!=symbol))):
                raise ExecutionLogError('S5 매도에 필요한 실제 관측 판단 근거가 없습니다.')
        staged_exit=(mode=='paper' and side=='sell' and (strategy in
                     ('S2_surge_high_breakout','S3_upper_limit_continuation','S4_theme_and_catalyst_leader','S5_steady_uptrend_vwap') or strategy in EXPERIMENT_STRATEGIES))
        if legacy_exit:require_no_pending=True
        if staged_exit:
            try:
                from kiwoom_autotrader.trading.paper_policy import _multi_enabled
                if (not (_multi_enabled(self.settings_loader()) or strategy in STAGED_STRATEGIES and effective_paper_experiment(self.settings_loader(),mode)['enabled']) or reconciliation_json is None
                        or reconciliation_evidence.get('schema_version') not in (4,7)
                        or reconciliation_evidence.get('schema_version')==7 and (
                            b3_config.get('ownership_status')!='exit_only_broker_confirmed'
                            or b3_config.get('profit_attribution')!='unconfirmed')
                        or strategy!='ddcrs' and (b3_config.get('multi_exit_kind')!=reconciliation_evidence['exit_kind']
                        or b3_config.get('multi_position_generation_id')!=reconciliation_evidence['generation_id'])):raise ValueError
            except Exception:
                raise ExecutionLogError('전략별 매도 대사 또는 판단 근거를 확인하지 못했습니다.') from None
            require_no_pending=True
        b3_stage = b3_config.get('b3_stage') if isinstance(b3_config,dict) else None
        b3_initial = None
        if b3_stage is not None:
            pct=b3_config.get('b3_initial_entry_pct');profit=b3_config.get('b3_add_on_profit_bps')
            budget=b3_config.get('b3_total_budget_krw');b3_initial=b3_config.get('b3_initial_intent_id')
            if (b3_stage not in ('initial','add') or mode!='paper' or side!='buy' or strategy!='gdcrs'
                    or order_type!='limit' or reconciliation_json is None
                    or value.get('holdings_source')!='broker_observation'
                    or type(pct) is not int or not 1<=pct<=99 or type(profit) is not int or not 0<=profit<=10000
                    or type(budget) is not int or not 1<=budget<=1_000_000
                    or symbol_cap>1_000_000 or per_buy_cap>1_000_000
                    or (b3_stage=='initial' and (b3_initial is not None or value['pre_order_held_qty']!=0
                         or value['pre_order_pur_amt_krw']!=0 or int(quantity)*int(price)>budget*pct//100))
                    or (b3_stage=='add' and (profit==0 or not isinstance(b3_initial,str)
                         or re.fullmatch(r'[0-9a-f]{32}',b3_initial) is None
                         or int(quantity)*int(price)>budget*(100-pct)//100))):
                raise ExecutionLogError('분할매수의 투자구분·예산·보유·주문 근거를 확인하지 못했습니다.')
            require_no_pending = True
        multi_b3_stage = b3_config.get('multi_b3_stage') if isinstance(b3_config,dict) else None
        multi_b3_initial = None
        if multi_b3_stage is not None:
            self._multi_b3_config(b3_config)
            if (mode!='paper' or side!='buy' or not multi or order_type!='limit'
                    or strategy not in ('S2_surge_high_breakout','S3_upper_limit_continuation','S4_theme_and_catalyst_leader','S5_steady_uptrend_vwap')
                    or reconciliation_evidence is None or b3_stage is not None):
                raise ExecutionLogError('다중 전략 분할매수의 투자구분·주문·대사 근거가 없습니다.')
            if (strategy=='S5_steady_uptrend_vwap' and multi_b3_stage=='initial'
                    and reconciliation_evidence.get('schema_version')!=5):
                raise ExecutionLogError('S5 최초 매수에는 동일 세션의 신규 0주 기준 증빙이 필요합니다.')
            require_no_pending = True
        daily_limit = 2 if multi_b3_stage is not None else 0
        position_limit = 0
        theme_limit = 0
        unclassified_policy = 'block'
        if mode == 'paper' and side == 'buy':
            try:
                gate_settings = self.settings_loader()
                position_limit = effective_paper_scan(gate_settings, mode)['max_positions']
                theme_limit = effective_theme_cap(gate_settings, mode)
                unclassified_policy = effective_theme_unclassified_policy(gate_settings, mode)
            except Exception:
                raise ConcurrentPositionLimitUnavailable('모의 동시 보유 한도 설정을 확인하지 못했습니다.') from None
            if position_limit or theme_limit:
                # Reject every outstanding account reservation, so no previous
                # cash snapshot can be reused for concurrent internal orders.
                require_account_clear = True
                if order_type != 'limit':
                    raise ConcurrentPositionLimitUnavailable('확장 모의 감시 매수는 원금이 정해진 지정가만 허용합니다.')
        if mode == "paper" and side == "buy" and strategy == "gdcrs" and frozen is not None:
            daily_limit = (frozen.as_dict().get("config") or {}).get("max_daily_entries_per_symbol", 0)
        if daily_limit and (self.path.is_symlink() or not self.path.is_file()):
            raise DailyEntryLimitUnavailable("일일 진입 원장을 확인하지 못했습니다.")
        identifier = uuid.uuid4().hex
        connection = self._db(require_existing_daily_ledger=True) if position_limit or theme_limit or daily_limit or staged_exit or legacy_exit or (mode == 'paper' and side == 'buy' and multi) else self._db()
        with connection as db:
            if mode == 'real' and side == 'buy':
                from .real_automation_safety import assert_real_buy_ledger
                if not db.in_transaction:
                    db.execute('BEGIN IMMEDIATE')
                assert_real_buy_ledger(db, self.scope, symbol, quantity, price, moment)
                self._assert_no_pending_account(db, 'real')
            if type(require_no_pending) is not bool:
                raise ExecutionLogError("미결 주문 검증 옵션은 참 또는 거짓이어야 합니다.")
            if position_limit or theme_limit or require_no_pending or require_account_clear or daily_limit or b3_stage is not None:
                # Serialize the check and insert across trackers/processes. A
                # migration write may already hold the SQLite transaction.
                if not db.in_transaction:
                    db.execute("BEGIN IMMEDIATE")
                if strategy == 'S5_steady_uptrend_vwap' and side == 'buy' and (multi_b3_stage == 'initial' or native_s5):
                    self._assert_s5_initial_boundary_unclaimed(db, mode, symbol, moment, frozen.as_dict())
                if position_limit or theme_limit:
                    self._assert_position_capacity(db, symbol, moment, position_limit or 10,
                        principal=int(quantity)*int(price), theme_limit=theme_limit,
                        unclassified_policy=unclassified_policy, strategy=strategy)
                if mode=='paper' and side=='buy' and strategy in OVERNIGHT_EXPERIMENT_STRATEGIES and effective_paper_experiment(self.settings_loader(),mode)['enabled']:
                    self._assert_overnight_capacity(db,symbol,strategy,moment)
                if legacy_exit:self._assert_legacy_ddcrs_allowed(db,mode,symbol)
                if multi and side=='buy' and self._symbol_order_watermark(db,mode,symbol)!=value['symbol_ledger_sha256']:
                    raise ExecutionLogError('대사 후 동일 종목 주문·체결 원장이 바뀌어 매수를 보류합니다.')
                if staged_exit:
                    self._validate_multi_exit_schema(db)
                    value=reconciliation_evidence
                    self._assert_strategy_holding_evidence(db,value)
                    position=(self._assert_exit_only_proof(db,value,moment) if value['schema_version']==7
                              else self.multi_strategy_position(mode,symbol,strategy,moment))
                    if (self._symbol_order_watermark(db,mode,symbol)!=value['symbol_ledger_sha256']
                            or position.get('status') not in ('owned','exit_only_broker_confirmed') or position.get('generation_id')!=value['generation_id']
                            or position.get('owned_qty')!=value['pre_order_held_qty']
                            or value['exit_kind']=='half' and position.get('half_take_done')):
                        raise ExecutionLogError('전략 보유 세대 또는 보유 수량이 바뀌어 매도를 보류합니다.')
                    self._assert_s5_exit_clock(strategy,position,frozen.as_dict() if frozen is not None else None,moment)
                    existing=db.execute('''SELECT i.state,i.reason,i.order_number,r.reason AS release_reason FROM multi_strategy_exit_claims c
                        LEFT JOIN intents i ON i.scope=c.scope AND i.id=c.intent_id
                        LEFT JOIN multi_strategy_exit_releases r ON r.scope=c.scope AND r.intent_id=c.intent_id
                        WHERE c.scope=? AND c.generation_id=? AND c.exit_kind=?''',
                        (self.scope,value['generation_id'],value['exit_kind'])).fetchall()
                    if any(row['state']!='cancelled' or row['reason']!='not_sent' or row['release_reason']!='not_sent' or row['order_number'] is not None for row in existing):
                        raise ExecutionLogError('해당 세대의 매도 접수 또는 전송 여부가 확인되지 않았습니다.')
                if multi and side=='buy':
                    self._assert_strategy_holding_evidence(db,value)
                    if effective_paper_experiment(gate_settings,mode)['enabled']:
                        self._assert_paper_experiment_budget(db,value,int(quantity),int(price),moment)
                    self._assert_no_pending_account(db,mode,symbol=symbol)
                    if value.get('schema_version') in (5,6):self._assert_epoch_proof(db,value,moment)
                    # A broker snapshot must agree with the independently
                    # verified single-owner history. A stale zero-balance
                    # response cannot authorize another strategy's entry.
                    position=self.multi_strategy_position(mode,symbol,strategy,moment)
                    held=value['pre_order_held_qty'];basis=value['pre_order_pur_amt_krw']
                    if native_s5 and (held!=0 or basis!=0 or value.get('schema_version')!=6):
                        raise ExecutionLogError('S5 실험 최초 매수에는 새 0주·미결 없음 기준 증빙이 필요합니다.')
                    if value.get('holdings_source')!='broker_observation':
                        raise ExecutionLogError('증권사 잔고 출처가 확인되지 않아 매수를 보류합니다.')
                    if position['status']=='flat_candidate':
                        if position.get('flat_epoch_reason')=='prior_closed_cycle_current_broker_flat':
                            from .flat_epoch_reanchor import verified_prior_closed_flat
                            if verified_prior_closed_flat(self,db,symbol,moment) is None:
                                raise ExecutionLogError('새 평탄 진입 사이클의 원장·브로커 증빙이 변경되었습니다.')
                        if held!=0 or basis!=0 or value.get('schema_version') not in (5,6):
                            raise ExecutionLogError('신규 0주 기준의 증빙이 없어 매수를 보류합니다.')
                        self._assert_epoch_proof(db,value,moment)
                    elif position['status']=='flat':
                        if held!=0 or basis!=0:
                            raise ExecutionLogError('기존 종목의 전략 소유권이 불명확하여 매수를 보류합니다.')
                    elif position['status']=='owned':
                        if (position.get('overnight_unobserved_interval') is not False
                                or position['owned_qty']!=held or position['basis_krw'] is None or position['basis_krw']!=basis):
                            raise ExecutionLogError('전략 소유권과 현재 보유 원금이 달라 매수를 보류합니다.')
                    else:
                        raise ExecutionLogError('종목의 전략 소유권을 확인하지 못해 매수를 보류합니다.')
                if b3_stage is not None:
                    self._validate_b3_claim_schema(db)
                    if b3_stage=='add':
                        candidate=self._b3_candidate(db,mode,symbol,moment)
                        if candidate is None:
                            raise ExecutionLogError('완전 체결된 최초 분할매수 또는 미결 여부를 확인하지 못했습니다.')
                        proof,initial_config=candidate
                        addition=frozen.as_dict().get('b3_addition')
                        bars=frozen.as_dict().get('bars')
                        trend=addition.get('trend_bars') if isinstance(addition,dict) else None
                        if (not isinstance(addition,dict) or not isinstance(bars,list) or len(bars)!=21
                                or any(not isinstance(row,dict) or not isinstance(row.get('source_observation'),dict)
                                       or row['source_observation'].get('symbol')!=symbol
                                       or row['source_observation'].get('mode')!='paper' for row in bars)
                                or not isinstance(trend,list) or not trend
                                or any(not isinstance(row,dict) or not isinstance(row.get('source_observation'),dict)
                                       or row['source_observation'].get('symbol')!=symbol
                                       or row['source_observation'].get('mode')!='paper' for row in trend)
                                or any(addition.get(k)!=proof[k] for k in ('initial_filled_qty','initial_filled_principal_krw','initial_fill_last_observed_at'))
                                or proof['initial_intent_id']!=b3_initial
                                or any(initial_config.get(k)!=b3_config.get(k) for k in
                                       ('b3_initial_entry_pct','b3_add_on_profit_bps','b3_total_budget_krw'))
                                or value['pre_order_held_qty']!=proof['initial_filled_qty']
                                or value['pre_order_pur_amt_krw']!=proof['initial_filled_principal_krw']
                                or datetime.fromisoformat(value['observed_at'])<datetime.fromisoformat(proof['initial_fill_last_observed_at'])
                                or proof['initial_filled_principal_krw']+int(quantity)*int(price)>budget
                                or int(price)*proof['initial_filled_qty']*10000 < proof['initial_filled_principal_krw']*(10000+profit)):
                            raise ExecutionLogError('최초 체결·현재 잔고·추가매수 가격 대사에 실패했습니다.')
                if multi_b3_stage is not None:
                    multi_b3_initial = self._assert_multi_b3_buy(db,mode,symbol,strategy,moment,b3_config,
                        frozen.as_dict(),value,int(quantity),int(price))
                if daily_limit:
                    counts = self._daily_buy_entry_counts(db, mode, symbol, moment.strftime("%Y%m%d"))
                    if counts["accepted"] + counts["reserved"] >= daily_limit:
                        raise DailyEntryLimitReached("해당 종목의 일일 자동매수 진입 한도에 도달했습니다.")
                if require_no_pending:
                    if staged_exit and value.get('schema_version')==7:
                        # Full broker details plus fresh integrated pending were
                        # validated in this same write transaction. A missing 00
                        # fill projection must not recreate an already terminal order.
                        self._assert_exit_only_proof(db,value,moment)
                    else:
                        self._assert_no_pending_order(db, mode, symbol)
                if require_account_clear:
                    self._assert_no_pending_account(db, mode,symbol=symbol)
            if strategy in EXPERIMENT_STRATEGIES or native_s5:
                signal_hash=b3_config['experiment_signal_sha256']
                try:
                    db.execute('INSERT INTO paper_experiment_signal_claims VALUES (?,?,?,?,?,?,?)',
                        (self.scope,moment.strftime('%Y%m%d'),mode,symbol,strategy,signal_hash,identifier))
                except sqlite3.IntegrityError:
                    raise ExecutionLogError('동일 모의 실험 신호의 주문 시도는 이미 기록되었습니다.') from None
            db.execute("INSERT INTO intents (scope,id,day,mode,side,symbol,quantity,strategy,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (self.scope, identifier, moment.strftime("%Y%m%d"), mode, side, symbol, int(quantity), strategy, moment.isoformat()))
            # The intent and frozen evidence commit together, before any HTTP.
            # Old intents remain without an evidence row; never backfill them.
            db.execute("INSERT INTO order_decisions VALUES (?,?,?,?,?,?,?)",
                (self.scope, identifier, moment.isoformat(), price, order_type,
                 frozen.json if frozen is not None else None, frozen.sha256 if frozen is not None else None))
            if b3_stage=='add':
                db.execute('INSERT INTO b3_add_claims VALUES (?,?,?,?)',
                           (self.scope,b3_initial,identifier,moment.isoformat()))
            if multi_b3_stage=='add':
                db.execute('INSERT INTO b3_add_claims VALUES (?,?,?,?)',
                           (self.scope,multi_b3_initial,identifier,moment.isoformat()))
            if staged_exit:
                db.execute('INSERT INTO multi_strategy_exit_claims VALUES (?,?,?,?,?)',
                           (self.scope,reconciliation_evidence['generation_id'],reconciliation_evidence['exit_kind'],identifier,moment.isoformat()))
            if reconciliation_json is not None:
                db.execute("INSERT INTO pre_order_reconciliations VALUES (?,?,?,?,?)",
                    (self.scope, identifier, moment.isoformat(), reconciliation_json,
                     hashlib.sha256(reconciliation_json.encode("utf-8")).hexdigest()))
        self._next_at = moment
        return identifier

    def cancel_intent(self, intent_id, reason="not_sent"):
        """Resolve only a known unsent/rejected intent, never an ambiguous order."""
        if not isinstance(reason, str) or re.fullmatch(r"[a-z_]{1,40}", reason) is None:
            raise ExecutionLogError("주문 미전송 사유는 확인된 내부 상태값이어야 합니다.")
        with self._db() as db:
            intent = db.execute("SELECT * FROM intents WHERE scope=? AND id=?", (self.scope, intent_id)).fetchone()
            if intent is None:
                raise ExecutionLogError("취소할 자동주문 사전 기록이 없습니다.")
            if intent["order_number"] is not None:
                raise ExecutionLogError("접수된 주문의 사전 기록을 미전송으로 바꿀 수 없습니다.")
            db.execute("UPDATE intents SET state='cancelled',reason=? WHERE scope=? AND id=?", (reason, self.scope, intent_id))
            if reason=='not_sent' and db.execute('SELECT 1 FROM multi_strategy_exit_claims WHERE scope=? AND intent_id=?',(self.scope,intent_id)).fetchone():
                self._validate_multi_exit_schema(db)
                db.execute('INSERT INTO multi_strategy_exit_releases VALUES (?,?,?,?)',
                           (self.scope,intent_id,self._moment().isoformat(),'not_sent'))

    def register_order(self, receipt: OrderReceipt, strategy: str, *, intent_id=None, baseline=None, registered_at=None):
        """Persist one accepted automatic order. No broker lookup or fill is implied.

        baseline is accepted for callers with a pre-order report; actual remaining
        stock is always observed again after fills and never inferred from it.
        """
        if not isinstance(receipt, OrderReceipt) or receipt.mode not in ("paper", "real") or receipt.side not in ("buy", "sell"):
            raise ExecutionLogError("자동주문 접수 정보를 확인하지 못했습니다.")
        if (re.fullmatch(r"[0-9]{6}", receipt.symbol or "") is None
                or re.fullmatch(r"[0-9]{1,12}", receipt.quantity or "") is None or int(receipt.quantity) <= 0
                or not isinstance(strategy, str) or not strategy.strip() or len(strategy) > 80 or not strategy.isprintable()):
            raise ExecutionLogError("자동주문 전략·종목·수량을 확인하지 못했습니다.")
        moment = self._moment() if registered_at is None else registered_at
        if not isinstance(moment, datetime) or moment.tzinfo is None:
            raise ExecutionLogError("자동주문 접수일을 확인하지 못했습니다.")
        moment = moment.astimezone(KST)
        key = (self.scope, moment.strftime("%Y%m%d"), receipt.mode, order_key(receipt.order_number))
        with self._db() as db:
            if intent_id is not None:
                intent = db.execute("SELECT * FROM intents WHERE scope=? AND id=?", (self.scope, intent_id)).fetchone()
                if (intent is None or (intent["day"], intent["mode"], intent["side"], intent["symbol"], intent["quantity"], intent["strategy"])
                        != (key[1], receipt.mode, receipt.side, receipt.symbol, int(receipt.quantity), strategy)
                        or intent["order_number"] not in (None, key[3]) or intent["state"] == "cancelled"):
                    raise ExecutionLogError("자동주문 사전 기록과 접수 결과가 일치하지 않습니다.")
                decision = db.execute("SELECT order_price,order_type FROM order_decisions WHERE scope=? AND intent_id=?", (self.scope, intent_id)).fetchone()
                if decision is not None and (
                        decision["order_type"] == "limit" and receipt.price != decision["order_price"]
                        or decision["order_type"] == "market" and receipt.price is not None):
                    raise ExecutionLogError("자동주문 사전 가격과 접수 결과가 일치하지 않습니다.")
                db.execute("UPDATE intents SET order_number=?,state='accepted' WHERE scope=? AND id=?", (key[3], self.scope, intent_id))
            existing = db.execute("SELECT * FROM orders WHERE scope=? AND day=? AND mode=? AND ord_no=?", key).fetchone()
            if existing:
                if (existing["symbol"], existing["side"], existing["quantity"], existing["strategy"]) != (receipt.symbol, receipt.side, int(receipt.quantity), strategy):
                    raise ExecutionLogError("같은 날짜·투자구분·주문번호의 원장 정보가 충돌합니다. 계좌 구분을 확인하세요.")
                return
            db.execute("INSERT INTO orders VALUES (?,?,?,?,?,?,?,?,?)",
                (*key, receipt.symbol, receipt.side, strategy, int(receipt.quantity), moment.isoformat()))
        self._next_at = moment

    def seconds_until_next(self):
        if self._identity is None:
            return None
        return max(0.0, (self._next_at - self._moment()).total_seconds()) if self._next_at else 0.0

    def _closed(self, day, now):
        if day < now.strftime("%Y%m%d"):
            return True
        end = self.settings_loader().get("market_end_time", "15:30")
        if not isinstance(end, str) or re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", end) is None:
            raise ExecutionLogError("장 종료 시각을 확인하지 못해 일일 결산을 보류합니다.")
        return day == now.strftime("%Y%m%d") and now.strftime("%H:%M") >= end

    @staticmethod
    def _closed_observation_facts(snapshot, mode):
        """Derive evidence once per immutable snapshot; never alter raw rows."""
        ws, groups = set(), {}
        stream_gap_seen = False
        selected = [row for row in snapshot if row.mode == mode and row.source_api in
                    ('00', 'ka10072', 'ka10073', 'ka10074')]
        if sum(row.source_api == '00' for row in selected) > 20000 or sum(row.source_api != '00' for row in selected) > 20000:
            return None
        for row in selected:
            if row.source_api != '00':
                continue
            event = _observation_event(row)
            if not isinstance(event, dict) or not isinstance(event.get('values'), dict):
                return None
            if event.get('event_kind') == 'stream_gap':
                stream_gap_seen = True
            semantic = {'event_kind': event.get('event_kind'), 'values': event['values']}
            ws.add(hashlib.sha256(canonical_json(semantic, maximum=64 * 1024).encode()).hexdigest())
        # Iterate backwards to capture only the latest complete or partial
        # query for each cost request, including all returned page evidence.
        for row in reversed(selected):
            if row.source_api == '00':
                continue
            event = _observation_event(row)
            if (not isinstance(event, dict) or not isinstance(event.get('request_body'), dict)
                    or not isinstance(event.get('response_body'), dict)
                    or not isinstance(event.get('continuation'), dict)
                    or not isinstance(event.get('query_id'), str)):
                return None
            key = (event.get('source_api'), tuple(sorted(event['request_body'].items())))
            if key not in groups:
                groups[key] = {'query_id': event['query_id'], 'pages': []}
            if groups[key]['query_id'] == event['query_id']:
                groups[key]['pages'].append({'page': event.get('page'), 'status': event.get('status'),
                    'response_body': event['response_body'], 'continuation': event['continuation']})
        cost = [{'api': key[0], 'request': key[1],
                 'pages': sorted(value['pages'], key=lambda item: item['page'])}
                for key, value in sorted(groups.items())]
        return sorted(ws), cost, stream_gap_seen

    @_diagnosed('closed_fingerprint')
    def _closed_economic_fingerprint(self, db, mode, day):
        """Hash financial facts, not repeat observation timestamps.

        Any malformed/oversized source or unresolved order prevents long
        backoff. Original observations are neither changed nor deduplicated.
        """
        try:
            source_state = self._closed_source_state(db, mode, day)
            if self._closed_limit_blocked(db, mode, day, source_state=source_state):
                return None, True
            facts = {}
            for name, query in self._closed_financial_queries():
                rows = [tuple(row) for row in db.execute(query + ' LIMIT 20001', (self.scope, day, mode))]
                if len(rows) > 20000:
                    return None, True
                facts[name] = rows
            # Cache only detached, small economic facts. Do not retain the
            # raw immutable snapshot in a per-day cache (it pins whole days).
            key = (mode, day)
            watermark = (source_state['identity'], source_state['groups'])
            with self._broker_cache_lock:
                cached = self._closed_observation_cache.get(key)
                if cached is None or cached[0] != watermark or db.in_transaction:
                    snapshot = self._broker_observations_snapshot(db, day)
                    with self._diagnostics.stage('closed_facts') as measurement:
                        measurement.set_counts(rows=len(snapshot))
                        evidence = self._closed_observation_facts(snapshot, mode)
                    self._closed_observation_cache.pop(key, None)
                    if evidence is not None and not db.in_transaction:
                        encoded = canonical_json(evidence, maximum=8 * 1024 * 1024)
                        if len(encoded.encode('utf-8')) <= 512 * 1024:
                            evidence = _immutable_json(json.loads(encoded))
                            self._closed_observation_cache[key] = (watermark, evidence)
                            while len(self._closed_observation_cache) > 4:
                                self._closed_observation_cache.pop(next(iter(self._closed_observation_cache)))
                else:
                    evidence = cached[1]
            if evidence is None:
                return None, True
            facts['execution_events'], facts['cost_reports'], stream_gap_seen = evidence
            holding = db.execute('SELECT discrepancy FROM holdings WHERE scope=? AND mode=?', (self.scope, mode)).fetchone()
            discrepancies = json.loads(holding[0]) if holding else []
            if not isinstance(discrepancies, list) or any(not isinstance(item, str) for item in discrepancies):
                return None, True
            facts['holding_discrepancies'] = sorted(set(discrepancies))
            unresolved = any(row[6] not in ('accepted', 'cancelled') or row[6] == 'accepted' and not row[5]
                             for row in facts['intents'])
            orders = {row[0]: row[4] for row in facts['orders']}
            totals = {}
            for row in facts['fills']:
                totals[row[0]] = totals.get(row[0], 0) + row[6]
            unresolved |= any(r['day']==day for r in self._pending_order_rows(db,mode))
            unresolved |= any(row[6] == 'accepted' and row[5] not in orders for row in facts['intents'])
            # Unknown cost attribution and past stream gaps stay in the
            # economic fingerprint but are not outstanding broker orders.
            # Stable unknown P&L remains unknown; repeated reads cannot create it.
            digest = hashlib.sha256(canonical_json(facts, maximum=8 * 1024 * 1024).encode()).hexdigest()
            return digest, bool(unresolved)
        except (TypeError, ValueError, OverflowError, json.JSONDecodeError):
            return None, True

    @_diagnosed('calendar_plan')
    def _closed_poll_plan(self, day, now):
        key=(day,now.strftime('%Y%m%d%H%M'))
        if key not in self._closed_calendar_cache:
            try:plan=self._closed_calendar_resolver(now,day)
            except Exception:plan={}
            if not isinstance(plan,dict):plan={}
            close=plan.get('next_close');elapsed=plan.get('elapsed_sessions')
            if (close is not None and (not isinstance(close,datetime) or close.tzinfo is None or close<=now)
                    or elapsed is not None and (type(elapsed) is not int or elapsed<0)):
                plan={}
            self._closed_calendar_cache={key:plan}
        return self._closed_calendar_cache[key]

    def _closed_due(self, db, mode, day, now):
        source_state = self._closed_source_state(db, mode, day)
        if self._closed_limit_blocked(db, mode, day, source_state=source_state):
            return False
        watermark = self._closed_change_watermark(db, mode, day, source_state)
        key = (mode, day)
        # The deadline is checked before any observation JSON is materialized.
        # Source identities/tails and small economic rows still wake on change.
        with self._closed_cache_lock:
            previous = self._closed_reconciliation.get(key)
            if (previous is not None and watermark is not None
                    and previous.get('watermark') == watermark and now >= previous['observed_at']
                    and (previous.get('polling_stopped') is True
                         or previous.get('next_at') is not None and now < previous['next_at'])):
                return False
        fingerprint, unresolved = self._closed_economic_fingerprint(db, mode, day)
        plan=self._closed_poll_plan(day,now)
        with self._closed_cache_lock:
            previous = self._closed_reconciliation.get(key)
            # More than seven *observed official sessions*, never weekdays.
            # An unresolved intent/order or invalid proof always keeps polling.
            elapsed=plan.get('elapsed_sessions')
            if fingerprint is not None and not unresolved and type(elapsed) is int and elapsed>7:
                self._closed_reconciliation[key]={'fingerprint':fingerprint,'observed_at':now,
                    'next_at':None,'level':3,'successful':True,'polling_stopped':True,
                    'calendar':plan,'reason':'older_than_seven_verified_sessions_no_pending',
                    'watermark':watermark}
                return False
            due = (previous is None or previous['fingerprint'] != fingerprint
                   or previous.get('polling_stopped') is True
                   or now >= previous['next_at'] or now < previous['observed_at'])
            if due:self._closed_attempts[key] = {'ok': True}
            elif previous is not None:
                # A newly verified repeat can change source IDs without
                # changing economics. Do not reparse that same delta at every
                # subsequent current-day cycle before the historical deadline.
                previous['watermark'] = watermark
            return due

    def _closed_incomplete(self, mode, day):
        with self._closed_cache_lock:
            attempt = self._closed_attempts.get((mode, day))
            if attempt is not None:
                attempt['ok'] = False

    def _closed_finish(self, mode, day, now):
        with self._db() as db:
            source_state = self._closed_source_state(db, mode, day)
            if self._closed_limit_blocked(db, mode, day, source_state=source_state):
                with self._closed_cache_lock:
                    self._closed_attempts.pop((mode, day), None)
                return True
            fingerprint, unresolved = self._closed_economic_fingerprint(db, mode, day)
            watermark = self._closed_change_watermark(db, mode, day, source_state)
        key = (mode, day)
        with self._closed_cache_lock:
            attempt = self._closed_attempts.pop(key, None)
            previous = self._closed_reconciliation.get(key)
            if attempt is None:
                return True
            if not attempt.get('summary_stable', False) or attempt.get('summary_fingerprint') != fingerprint:
                # A late edit/fill must trigger another cycle, not become the
                # cached baseline after an older summary was written.
                self._closed_reconciliation.pop(key, None)
                return False
            stable = (bool(attempt['ok']) and not unresolved and fingerprint is not None
                      and previous is not None and previous.get('successful') is True
                      and previous['fingerprint'] == fingerprint
                      and now >= previous['observed_at'])
            level = min(2, previous['level'] + 1) if stable else 0
            delay = (5, 15, 60)[level]
            plan=self._closed_poll_plan(day,now) if level>=2 else {}
            next_at=plan.get('next_close') or now+timedelta(minutes=delay)
            self._closed_reconciliation[key] = {'fingerprint': fingerprint, 'observed_at': now,
                'next_at':next_at,'level':level,'calendar':plan,
                'successful':bool(attempt['ok']) and not unresolved and fingerprint is not None,
                'watermark':watermark,
                'reason':'three_stable_official_next_close' if plan.get('next_close') else 'bounded_retry_or_calendar_unavailable'}
            return True

    def _closed_failed(self, mode, day, now):
        """Bound retries after failed/partial reads without claiming success."""
        try:
            with self._db() as db:
                if self._closed_limit_blocked(db, mode, day):
                    with self._closed_cache_lock:
                        self._closed_attempts.pop((mode, day), None)
                    return
                fingerprint, _ = self._closed_economic_fingerprint(db, mode, day)
        except Exception:
            fingerprint = None
        with self._closed_cache_lock:
            self._closed_attempts.pop((mode, day), None)
            self._closed_reconciliation[(mode, day)] = {'fingerprint': fingerprint,
                'observed_at': now, 'next_at': now + timedelta(minutes=5), 'level': 0, 'successful': False}

    def _observe_execution(self, db, current, kind, source_kind, observed_at, *, previous=None, applied=True, after_restart=False):
        payload = canonical_json({"schema_version": 1, "kind": kind, "source_kind": source_kind,
            "observed_at": observed_at, "projection_applied": applied,
            "recorder_run_id": self._observer_run_id, "after_recorder_restart": after_restart,
            "quantity_semantics": "provider_key_snapshot_not_inferred_unit_fill",
            "original_observed_at": None if kind == "legacy_projection" else observed_at,
            "current": current, "previous": previous}, maximum=64 * 1024)
        db.execute("""INSERT INTO execution_observations
            (scope,day,mode,ord_no,fill_no,observed_at,kind,source_kind,digest,payload_json)
            VALUES (?,?,?,?,?,?,?,?,?,?)""", (current["scope"], current["day"], current["mode"], current["ord_no"],
            current["fill_no"], observed_at, kind, source_kind, hashlib.sha256(payload.encode()).hexdigest(), payload))

    def _ingest(self, mode, day, executions, *, observed_at=None, source_kind=None):
        if not isinstance(executions, (tuple, list)) or any(not isinstance(item, Execution) or item.day != day for item in executions):
            raise ExecutionLogError("날짜가 확인된 전체 체결 목록이 필요합니다.")
        keyed = {}
        for item in executions:
            key = (item.order_number, item.execution_number)
            if key in keyed and keyed[key] != item:
                raise ExecutionLogError("중복 체결번호의 정보가 서로 다릅니다.")
            keyed[key] = item
        moment = self._moment() if observed_at is None else observed_at
        if not isinstance(moment, datetime) or moment.tzinfo is None or moment.utcoffset() is None:
            raise ExecutionLogError("체결 관측 시각을 확인하지 못했습니다.")
        source_kind = self._execution_source_kind if source_kind is None else source_kind
        if source_kind not in ("broker_observation", "unknown", "synthetic"):
            raise ExecutionLogError("체결 관측 출처를 확인하지 못했습니다.")
        observed = moment.astimezone(KST).isoformat()
        with self._db() as db:
            orders = {row["ord_no"]: row for row in db.execute("SELECT * FROM orders WHERE scope=? AND day=? AND mode=?", (self.scope, day, mode))}
            for item in keyed.values():
                order = orders.get(item.order_number)
                if order is None:
                    continue  # Manual/unregistered trades cannot acquire an auto strategy.
                if item.symbol != order["symbol"] or item.side != order["side"]:
                    raise ExecutionLogError("자동주문과 실제 체결의 종목·방향이 일치하지 않습니다.")
                key = (self.scope, day, mode, item.order_number, item.execution_number)
                old = db.execute("SELECT * FROM fills WHERE scope=? AND day=? AND mode=? AND ord_no=? AND fill_no=?", key).fetchone()
                previous = dict(old) if old is not None else None
                current = dict(zip(("scope", "day", "mode", "ord_no", "fill_no", "symbol", "name", "side", "moment", "price", "quantity", "strategy"),
                    (*key, item.symbol, item.name, item.side, item.time, _number(item.price), item.quantity, order["strategy"])))
                current.update(remaining=None, holdings_at=None, holdings_status="조회 대기")
                compared = ("symbol", "name", "side", "moment", "price", "quantity", "strategy")
                repeated = previous is not None and all(previous[field] == current[field] for field in compared)
                older = previous is not None and current["moment"] < previous["moment"]
                kind = ("new_execution_key" if previous is None else "repeated_snapshot" if repeated
                        else "out_of_order_timestamp" if older else "same_key_quantity_increase" if current["quantity"] > previous["quantity"]
                        else "same_key_correction")
                last = db.execute("SELECT payload_json FROM execution_observations WHERE scope=? AND day=? AND mode=? AND ord_no=? AND fill_no=? ORDER BY id DESC LIMIT 1", key).fetchone()
                after_restart = last is not None and json.loads(last[0]).get("recorder_run_id") != self._observer_run_id
                self._observe_execution(db, current, kind, source_kind, observed, previous=previous,
                                        applied=not repeated and not older, after_restart=after_restart)
                if repeated or older:
                    if previous["holdings_at"] is not None:
                        try:
                            linked = datetime.fromisoformat(previous["holdings_at"])
                            fill_at = datetime.strptime(day + previous["moment"], "%Y%m%d%H:%M:%S").replace(tzinfo=KST)
                            stale_link = linked.tzinfo is None or linked.utcoffset() is None or linked < fill_at
                        except (TypeError, ValueError):
                            stale_link = True
                        if stale_link:
                            cleared = {**previous, "remaining": None, "holdings_at": None, "holdings_status": "조회 대기: 체결보다 이른 과거 잔고 연결 해제"}
                            self._observe_execution(db, cleared, "stale_holdings_link_cleared", "validation_of_existing_projection",
                                                    observed, previous=previous)
                            db.execute("UPDATE fills SET remaining=NULL,holdings_at=NULL,holdings_status=? WHERE scope=? AND day=? AND mode=? AND ord_no=? AND fill_no=?",
                                       (cleared["holdings_status"], *key))
                    continue
                db.execute("""INSERT INTO fills VALUES (?,?,?,?,?,?,?,?,?,?,?,?,NULL,NULL,'조회 대기')
                    ON CONFLICT(scope,day,mode,ord_no,fill_no) DO UPDATE SET
                    name=excluded.name,moment=excluded.moment,price=excluded.price,quantity=excluded.quantity,
                    remaining=NULL,holdings_at=NULL,holdings_status='조회 대기'""", tuple(current[field] for field in
                    ("scope", "day", "mode", "ord_no", "fill_no", "symbol", "name", "side", "moment", "price", "quantity", "strategy")))
            for order in orders.values():
                total = db.execute("SELECT COALESCE(SUM(quantity),0) FROM fills WHERE scope=? AND day=? AND mode=? AND ord_no=?",
                                   (self.scope, day, mode, order["ord_no"])).fetchone()[0]
                if total > order["quantity"]:
                    raise ExecutionLogError("누적 체결수량이 등록 주문수량보다 큽니다. 원장 반영을 보류합니다.")
            symbols = [row[0] for row in db.execute("SELECT DISTINCT symbol FROM fills WHERE scope=? AND day=? AND mode=? AND side='sell'",
                                                   (self.scope, day, mode))]
            for symbol in symbols:
                # Include unregistered sells in the source fingerprint. A later
                # manual trade must invalidate an earlier automatic attribution.
                broker = sorted((item.order_number, item.execution_number, item.time, _number(item.price), item.quantity)
                    for item in keyed.values() if item.symbol == symbol and item.side == "sell")
                projected = [tuple(row) for row in db.execute("SELECT ord_no,fill_no,moment,price,quantity FROM fills WHERE scope=? AND day=? AND mode=? AND symbol=? AND side='sell' ORDER BY ord_no,fill_no",
                    (self.scope, day, mode, symbol))]
                digest = hashlib.sha256(canonical_json({"broker": broker, "projection": projected}, maximum=4 * 1024 * 1024).encode()).hexdigest()
                previous = db.execute("SELECT digest FROM profit_sources WHERE scope=? AND day=? AND mode=? AND symbol=?",
                                      (self.scope, day, mode, symbol)).fetchone()
                if previous is None or previous[0] != digest:
                    db.execute("INSERT OR REPLACE INTO profit_sources VALUES (?,?,?,?,?)", (self.scope, day, mode, symbol, digest))
                    db.execute("INSERT OR REPLACE INTO profits VALUES (?,?,?,?,NULL,NULL,?)", (self.scope, day, mode, symbol,
                               "미확정: 체결 또는 귀속 범위 변경, 수수료·세금 재조회 대기"))
                    self._profit_refresh_at.pop((mode, day, symbol), None)
        self._snapshots[(mode, day)] = tuple(keyed.values())
        self._materialize(day)

    def _invalidate_profit(self, mode, day, symbol):
        # A failed fresh read must not leave an old result labelled confirmed.
        # Prior observations remain immutable in profit_observations/JSONL.
        with self._db() as db:
            db.execute("INSERT OR REPLACE INTO profits VALUES (?,?,?,?,NULL,NULL,?)", (self.scope, day, mode, symbol,
                       "미확정: 수수료·세금 재조회 진행 또는 조회 실패"))
        self._materialize(day)

    def _save_paper_balance_snapshot(self, mode, report, moment, *, strict=False):
        """Store a fully fetched paper balance witness and advance its daily view.

        A fresh process and injected/test fetchers cannot assert broker provenance.
        Do not store token, account identity, raw payload, or real-account data.
        """
        try:
            if mode != "paper" or self._holdings_source_kind != "broker_observation":
                raise ExecutionLogError("모의 잔고 관측 출처를 확인하지 못했습니다.")
            snapshot = _paper_balance_snapshot(report)
            if snapshot is None or not isinstance(moment, datetime) or moment.tzinfo is None or moment.utcoffset() is None:
                raise ExecutionLogError("모의 잔고 관측 또는 시각을 확인하지 못했습니다.")
            moment = moment.astimezone(KST)
            payload = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            digest = hashlib.sha256(payload.encode()).hexdigest()
            key = (self.scope, mode, moment.strftime("%Y%m%d"), moment.isoformat())
            source = "broker_observation"
            with self._db() as db:
                inserted = db.execute("""INSERT OR IGNORE INTO paper_account_balance_observations
                    (scope,mode,day,observed_at,source_kind,digest,payload_json) VALUES (?,?,?,?,?,?,?)""",
                    (*key, source, digest, payload))
                if not inserted.rowcount:
                    existing = db.execute("""SELECT source_kind,digest,payload_json
                        FROM paper_account_balance_observations WHERE scope=? AND mode=? AND day=? AND observed_at=?""",
                        key).fetchone()
                    if existing is None or tuple(existing) != (source, digest, payload):
                        raise ExecutionLogError("같은 시각의 모의 잔고 관측이 일치하지 않습니다.")
                db.execute("""INSERT INTO account_balance_daily
                    (scope,mode,day,observed_at,source_kind,payload_json) VALUES (?,?,?,?,?,?)
                    ON CONFLICT(scope,mode,day) DO UPDATE SET
                      observed_at=excluded.observed_at, source_kind=excluded.source_kind,
                      payload_json=excluded.payload_json
                    WHERE excluded.observed_at > account_balance_daily.observed_at""",
                    (*key, source, payload))
            return True
        except (sqlite3.Error, OSError, ExecutionLogError):
            # Optional journal projection must not interrupt order supervision.
            # A stale/missing snapshot remains visibly stale/missing to readers.
            if strict:
                raise
            return False

    def _holdings(self, mode, report, moment, *, source_kind=None):
        source_kind = self._holdings_source_kind if source_kind is None else source_kind
        if (source_kind not in ("broker_observation", "unknown", "synthetic") or not isinstance(moment, datetime)
                or moment.tzinfo is None or moment.utcoffset() is None):
            raise ExecutionLogError("잔고 관측 출처·시각을 확인하지 못했습니다.")
        if (not isinstance(report, dict) or report.get("mode") != mode or report.get("exchange") != "KRX"
                or report.get("snapshot_changed") is not False or not isinstance(report.get("holdings"), list)):
            raise ExecutionLogError("남은 보유수량을 확인할 완전한 KRX 잔고가 없습니다.")
        positions = {}
        unknown = set()
        for row in report["holdings"]:
            if not isinstance(row, dict):
                raise ExecutionLogError("잔고 자료 형식이 잘못되었습니다.")
            code = row.get("stk_cd")
            if not isinstance(code, str) or re.fullmatch(r"[AJQ]?[0-9]{6}", code) is None:
                raise ExecutionLogError("잔고 종목코드가 빠져 남은 보유수량을 확인하지 못했습니다.")
            if code.startswith(("J", "Q")):
                continue
            symbol = code[-6:]
            if row.get("crd_tp") != "00":
                unknown.add(symbol)
                continue
            quantity = row.get("rmnd_qty")
            if not isinstance(quantity, str) or re.fullmatch(r"\+?[0-9]{1,15}", quantity) is None:
                raise ExecutionLogError("남은 보유수량을 확인하지 못했습니다.")
            if symbol in positions:
                unknown.add(symbol)
                continue
            positions[symbol] = int(quantity)
        with self._db() as db:
            previous = db.execute("SELECT * FROM holdings WHERE scope=? AND mode=?", (self.scope, mode)).fetchone()
            discrepancies = []
            if previous:
                old = json.loads(previous["positions"])
                previous_at = datetime.fromisoformat(previous["observed_at"])
                expected = {}
                for (saved_mode, day), items in self._snapshots.items():
                    if saved_mode != mode:
                        continue
                    for item in items:
                        at = datetime.strptime(day + item.time, "%Y%m%d%H:%M:%S").replace(tzinfo=KST)
                        if previous_at < at <= moment:
                            expected[item.symbol] = expected.get(item.symbol, 0) + item.quantity * (1 if item.side == "buy" else -1)
                # A REST row may be a cumulative rounded snapshot and its
                # trade timestamp need not describe every unit execution.
                # Use immutable WS unit fills only when the observation window
                # is unambiguous; never fabricate a fill or clear an old gap.
                if source_kind == "broker_observation" and previous_at.date() == moment.date() and previous_at < moment:
                    rows = db.execute("""SELECT observed_at,payload_json FROM broker_observation_evidence
                        WHERE scope=? AND mode=? AND day=? AND source_api='00' AND observed_at>? AND observed_at<=?
                        ORDER BY id LIMIT 20001""", (self.scope, mode, moment.strftime("%Y%m%d"),
                        previous_at.isoformat(), moment.isoformat())).fetchall()
                    unit, malformed = {}, len(rows) > 20000
                    for observation in rows:
                        try:
                            event = json.loads(observation["payload_json"])
                            if event.get("event_kind") in ("stream_gap", "stream_stopped", "stream_started", "stream_resumed"):
                                malformed = True
                                break
                            if event.get("event_kind") != "execution_stream":
                                continue
                            values = event["values"]
                            if not values.get("909") and not values.get("915"):
                                continue
                            order, fill, code, side = (values.get(k, "") for k in ("9203", "909", "9001", "907"))
                            qty, tm = values.get("915", ""), values.get("908", "")
                            price, cumulative = values.get("914", ""), values.get("911", "")
                            if (not re.fullmatch(r"[0-9]{1,7}", order) or int(order) <= 0
                                    or not re.fullmatch(r"[0-9]{1,20}", fill) or int(fill) <= 0
                                    or not re.fullmatch(r"A?[0-9]{6}", code) or side not in ("1", "2")
                                    or not re.fullmatch(r"[0-9]{1,15}", qty) or int(qty) <= 0
                                    or values.get("913") != "체결"
                                    or not re.fullmatch(r"[+-]?[0-9]{1,15}", price) or abs(int(price)) <= 0
                                    or not re.fullmatch(r"[0-9]{1,15}", cumulative) or int(cumulative) < int(qty)
                                    or not re.fullmatch(r"[0-9]{6}", tm)):
                                raise ValueError
                            at = datetime.strptime(moment.strftime("%Y%m%d") + tm, "%Y%m%d%H%M%S").replace(tzinfo=KST)
                            # The execution timestamp has only one-second precision.
                            if not previous_at < at and at + timedelta(seconds=1) > previous_at:
                                raise ValueError
                            if not previous_at < at <= moment:
                                raise ValueError
                            key = (int(order), int(fill))
                            value = (code[-6:], side, int(qty), tm)
                            if key in unit and unit[key] != value:
                                raise ValueError
                            unit[key] = value
                        except (ValueError, KeyError, TypeError, json.JSONDecodeError):
                            malformed = True
                            break
                    if unit and not malformed:
                        ws_expected = {}
                        for code, side, qty, _ in unit.values():
                            ws_expected[code] = ws_expected.get(code, 0) + qty * (1 if side == "2" else -1)
                        for code, quantity in ws_expected.items():
                            if code not in unknown and positions.get(code, 0) - old.get(code, 0) == quantity:
                                expected[code] = quantity
                for symbol in set(old) | set(positions) | set(expected):
                    if symbol in unknown:
                        continue
                    delta = positions.get(symbol, 0) - old.get(symbol, 0)
                    if delta != expected.get(symbol, 0):
                        discrepancies.append(symbol)
            db.execute("INSERT OR REPLACE INTO holdings VALUES (?,?,?,?,?)",
                       (self.scope, mode, moment.isoformat(), json.dumps(positions, sort_keys=True), json.dumps(discrepancies)))
            pending = list(db.execute("SELECT DISTINCT day,symbol FROM fills WHERE scope=? AND mode=? AND holdings_at IS NULL", (self.scope, mode)))
            for row in pending:
                if row["symbol"] in unknown:
                    db.execute("UPDATE fills SET holdings_status='미확정: 신용구분 또는 중복잔고 확인 필요' WHERE scope=? AND mode=? AND day=? AND symbol=? AND holdings_at IS NULL",
                               (self.scope, mode, row["day"], row["symbol"]))
                    continue
                status = "조회시점 보유수량(체결순간 수량 아님)"
                if row["symbol"] in discrepancies:
                    status += "; 잔고 변동 대사 필요"
                for fill_row in db.execute("SELECT * FROM fills WHERE scope=? AND mode=? AND day=? AND symbol=? AND holdings_at IS NULL",
                                           (self.scope, mode, row["day"], row["symbol"])):
                    previous = dict(fill_row)
                    key = (self.scope, row["day"], mode, fill_row["ord_no"], fill_row["fill_no"])
                    fill_at = datetime.strptime(row["day"] + fill_row["moment"], "%Y%m%d%H:%M:%S").replace(tzinfo=KST)
                    if moment < fill_at:
                        continue
                    latest = db.execute("SELECT observed_at FROM execution_observations WHERE scope=? AND day=? AND mode=? AND ord_no=? AND fill_no=? ORDER BY id DESC LIMIT 1", key).fetchone()
                    if latest is not None and datetime.fromisoformat(latest[0]) > moment:
                        continue  # A report observed before this snapshot cannot reconcile it.
                    current = {**previous, "remaining": str(positions.get(row["symbol"], 0)),
                               "holdings_at": moment.isoformat(), "holdings_status": status}
                    self._observe_execution(db, current, "holdings_reconciliation", source_kind,
                                            moment.isoformat(), previous=previous)
                    db.execute("UPDATE fills SET remaining=?,holdings_at=?,holdings_status=? WHERE scope=? AND day=? AND mode=? AND ord_no=? AND fill_no=?",
                               (current["remaining"], moment.isoformat(), status, *key))
        for day in {row["day"] for row in pending}:
            self._materialize(day)

    def _latest_sell_observation_id(self, db, mode, day, symbol):
        # A broad day/mode watermark is intentionally conservative: any new
        # 00 observation may invalidate the in-progress profit projection.
        return db.execute("SELECT COALESCE(MAX(id),0) FROM broker_observation_evidence WHERE scope=? AND day=? AND mode=? AND source_api='00'", (self.scope, day, mode)).fetchone()[0]

    def _profit_input_fingerprint(self, db, mode, day, symbol):
        # Immutable observation IDs detect all new source data. The additional
        # order/fill tuples catch projection changes which do not insert a 00.
        latest = db.execute("SELECT COALESCE(MAX(id),0) FROM broker_observation_evidence WHERE scope=? AND day=? AND mode=?", (self.scope, day, mode)).fetchone()[0]
        orders = tuple(tuple(row) for row in db.execute("SELECT ord_no,side,quantity FROM orders WHERE scope=? AND day=? AND mode=? AND symbol=? ORDER BY ord_no", (self.scope, day, mode, symbol)))
        fills = tuple(tuple(row) for row in db.execute("SELECT ord_no,fill_no,side,price,quantity FROM fills WHERE scope=? AND day=? AND mode=? AND symbol=? ORDER BY ord_no,fill_no", (self.scope, day, mode, symbol)))
        return latest, orders, fills

    def _profit(self, mode, day, symbol, rows, *, await_period=False, sell_watermark=None):
        if self._historical_sidecar_deferred(day):
            return
        reason = "미확정: 자동매도와 증권사 당일 손익 범위를 대사하지 못함"
        net = basis = None
        source = self._snapshots.get((mode, day))
        comparison = None
        post_sell_state = None
        try:
            cost_rows = self._cost_evidence_rows(rows, symbol)
            canonical_json(cost_rows, maximum=3 * 1024 * 1024)
        except (ExecutionLogError, ValueError):
            cost_rows = None
            reason = "미확정: 손익 원자료의 형식·범위 또는 보존 한도를 확인하지 못함"
        with self._db() as db:
            # Compute without taking the ledger writer lock. A short writer
            # transaction at commit verifies that every input is still current.
            initial_fingerprint = self._profit_input_fingerprint(db, mode, day, symbol)
            observations_snapshot = accounting_snapshot = None

            def observations_for_profit():
                nonlocal observations_snapshot
                if observations_snapshot is None:
                    observations_snapshot = self._broker_observations_snapshot(db, day, mode=mode)
                return observations_snapshot

            def accounting_for_profit():
                nonlocal accounting_snapshot
                if accounting_snapshot is None:
                    day_orders = [dict(row) for row in db.execute(
                        "SELECT * FROM orders WHERE scope=? AND day=? AND mode=? ORDER BY ord_no", (self.scope, day, mode))]
                    day_fills = [dict(row) for row in db.execute(
                        "SELECT * FROM fills WHERE scope=? AND day=? AND mode=?", (self.scope, day, mode))]
                    accounting_snapshot = _execution_accounting(day, day_orders, day_fills, observations_for_profit())
                return accounting_snapshot

            tracked = list(db.execute("SELECT * FROM fills WHERE scope=? AND day=? AND mode=? AND symbol=? AND side='sell'", (self.scope, day, mode, symbol)))
            broker_sells = [item for item in source or () if item.symbol == symbol and item.side == "sell"]
            tracked_ids = {(row["ord_no"], row["fill_no"]) for row in tracked}
            broker_ids = {(item.order_number, item.execution_number) for item in broker_sells}
            if (cost_rows is not None and source is not None and tracked_ids and tracked_ids == broker_ids
                    and isinstance(rows, (tuple, list)) and rows and all(isinstance(row, RealizedProfit) and row.symbol == symbol for row in rows)):
                with localcontext() as context:
                    context.prec = 60
                    quantity = sum(item.quantity for item in broker_sells)
                    gross = sum((item.price * item.quantity for item in broker_sells), Decimal(0))
                    projection = {(row["ord_no"], row["fill_no"]): (row["quantity"], Decimal(row["price"])) for row in tracked}
                    snapshot_matches = all(projection[(item.order_number, item.execution_number)] == (item.quantity, item.price) for item in broker_sells)
                    cost_gross = sum((row.price * row.quantity for row in rows), Decimal(0))
                    cost = sum((row.buy_price * row.quantity for row in rows), Decimal(0))
                    commission = sum((row.commission for row in rows), Decimal(0))
                    tax = sum((row.tax for row in rows), Decimal(0))
                    calculated = cost_gross - cost - commission - tax
                    reported = [getattr(row, "reported_net", None) for row in rows]
                    secondary = [getattr(row, "reported_net_secondary", None) for row in rows]
                    reported_total = sum(reported, Decimal(0)) if all(value is not None for value in reported) else None
                    secondary_total = sum(secondary, Decimal(0)) if all(value is not None for value in secondary) else None
                    reported_matches = all(value is None or value == (row.price - row.buy_price) * row.quantity - row.commission - row.tax
                        for row in rows for value in (getattr(row, "reported_net", None), getattr(row, "reported_net_secondary", None)))
                    # The displayed purchase unit price can be rounded.  A
                    # difference below one won is compatible with that, but
                    # the TR specification does not state the pricing or
                    # profit rounding rule.  Record it as a hypothesis only;
                    # never manufacture a confirmed net from this tolerance.
                    raw_row_values = [getattr(row, "reported_net", None) for row in rows]
                    row_residuals = [value - ((row.price-row.buy_price)*row.quantity-row.commission-row.tax)
                                     for row in rows for value in
                                     (getattr(row, "reported_net", None), getattr(row, "reported_net_secondary", None))
                                     if value is not None]
                    total_residual = None if reported_total is None else reported_total-calculated
                    sub_won_candidate = (all(value is not None for value in raw_row_values) and total_residual is not None
                        and Decimal(0) < abs(total_residual) < Decimal(1)
                        and (secondary_total is None or abs(secondary_total-calculated) < Decimal(1))
                        and all(abs(value) < Decimal(1) for value in row_residuals))
                    # REST fills can contain a whole-won average rather than
                    # the exact consideration of the individual executions.
                    # Only bridge that rounding difference when every sell
                    # order has a complete, gap-free 00 unit sequence, the
                    # quantities agree and the independently observed unit
                    # sum exactly agrees with the broker profit-row amount.
                    # Merely being within Q won cannot prove correct scope or
                    # resolve a conflicting broker profit/cost calculation.
                    unit_gross = None
                    if snapshot_matches and quantity == sum(row.quantity for row in rows) and gross != cost_gross:
                        account = accounting_for_profit()
                        sells = [item for item in account["orders"] if item["side"] == "sell" and item["symbol"] == symbol]
                        tracked_orders = {row["ord_no"] for row in tracked}
                        if (sells and {item["order_number"] for item in sells} == tracked_orders
                                and all(item["verified_amount_source"] == "00_unit_sum_reconciled_to_cumulative_quantity"
                                    and item["avg_price_reconciliation_status"] in ("exact", "reconciled_with_unit_rounding")
                                    and item["verified_quantity"] == item["rest_projection_quantity"] == item["ordered_quantity"]
                                    for item in sells)):
                            unit_gross = sum((Decimal(item["verified_amount"]) for item in sells), Decimal(0))
                    rounding_reconciled = unit_gross is not None and unit_gross == cost_gross
                    comparison = {"execution_snapshot_gross": _number(gross),
                        "projection_gross": _number(sum((Decimal(row["price"]) * row["quantity"] for row in tracked), Decimal(0))),
                        "profit_rows_gross": _number(cost_gross), "profit_rows_quantity": sum(row.quantity for row in rows),
                        "buy_basis": _number(cost), "commission": _number(commission), "tax": _number(tax),
                        "calculated_net_from_profit_rows": _number(calculated),
                        "broker_reported_net": None if reported_total is None else _number(reported_total),
                        "broker_reported_net_secondary": None if secondary_total is None else _number(secondary_total),
                        "projection_matches_snapshot": snapshot_matches, "gross_matches": gross == cost_gross,
                        "unit_execution_gross": None if unit_gross is None else _number(unit_gross),
                        "avg_price_rounding_diff_krw": _number(gross - unit_gross) if rounding_reconciled else None,
                        "reconciliation_status": "reconciled_with_unit_rounding" if rounding_reconciled else ("exact" if gross == cost_gross else "unconfirmed"),
                        "reported_matches_calculation": reported_matches, "tolerance_applied": rounding_reconciled,
                        "sub_won_basis_rounding": {"status": "possible_sub_won_basis_rounding" if sub_won_candidate else "not_established",
                            "reported_minus_calculated": None if total_residual is None else _number(total_residual),
                            "max_abs_row_difference": _number(max(map(abs, row_residuals))) if row_residuals else None,
                            "cause_confirmed": False, "net_confirmed_by_this_test": False}}
                    cutoff = None
                    if snapshot_matches and quantity != sum(row.quantity for row in rows):
                        # Link to the *same* recorded ka10072 response, not
                        # merely another query with coincidentally equal totals.
                        # The helper treats request/response time and 00 event
                        # precision conservatively; no subset result is final.
                        try:
                            pages = [_observation_event(row) for row in observations_for_profit() if row.source_api == 'ka10072']
                            pages = [item for item in pages if item.get('request_body',{}).get('stk_cd','').removeprefix('A') == symbol]
                            if pages:
                                selected_id = pages[-1].get('query_id')
                                pages = [item for item in pages if selected_id and item.get('query_id') == selected_id]
                                raw = [row for page in pages for row in page.get('response_body',{}).get('dt_stk_div_rlzt_pl',[])]
                                if self._cost_evidence_rows(_profit_rows(raw, symbol), symbol) == cost_rows:
                                    account = accounting_for_profit()
                                    cutoff = classify_cost_cutoff(day, symbol, account['orders'], pages,
                                        broker_quantity=sum(row.quantity for row in rows), broker_amount=cost_gross)
                        except (ValueError, TypeError, KeyError, AttributeError, ExecutionLogError, ExecutionsError):
                            cutoff = None
                        if cutoff is not None: comparison['cost_query_cutoff'] = cutoff
                    if not snapshot_matches:
                        reason = "미확정: 체결 원장 최신값과 현재 조회 관측이 불일치"
                    elif quantity != sum(row.quantity for row in rows):
                        reason = ("미확정: 손익 조회 이후 신규 체결 발생(post_cost_query_fills_present)"
                                  if cutoff and cutoff.get('status') == 'post_cost_query_fills_present'
                                  else "미확정: 체결 원장과 ka10072 손익 원자료 수량 불일치")
                    elif gross != cost_gross and not rounding_reconciled:
                        reason = "미확정: 체결 원장 대금과 ka10072 손익 원자료 대금 불일치; 분할체결·원금 대사 필요"
                    elif not reported_matches:
                        reason = "미확정: 증권사 보고손익과 단가·수량·수수료·세금 산식 불일치"
                    elif cost > 0:
                        net, basis = _number(calculated), _number(cost)
                        reason = ("ka10072 기준 계산값; 분할체결 실제 대금과 일치, REST 평균단가 반올림 차이; 다른 손익 TR 비용 대사는 별도"
                                  if rounding_reconciled else "ka10072 매입단가×매도체결량 및 매매수수료·세금; 해당 종목 당일 현금매도 전체가 등록 자동주문과 일치")
                    # Another complete ka10073 report for the same account,
                    # symbol and day is independent evidence. Do not silently
                    # label a ka10072 value final when the two disagree, or
                    # when their transaction scopes cannot be established.
                    if (comparison is not None and snapshot_matches and quantity == sum(row.quantity for row in rows)
                            and (gross == cost_gross or rounding_reconciled) and cost > 0
                            and (net is not None or sub_won_candidate)):
                        period_observations = [row for row in observations_for_profit() if row.source_api == 'ka10073']
                        if period_observations:
                            period_view = accounting_for_profit()
                            period = next((item for item in period_view["cost_reports"] if item["source_api"] == "ka10073" and item["symbol"] == symbol), None)
                            if period is not None:
                                relevant = [_observation_event(row) for row in period_observations
                                            if row["id"] in period["observation_ids"]]
                                valid_period = (period["complete_query"] and period.get("cash_symbol_scope_verified")
                                    and all(item.get("request_body", {}).get("strt_dt") == day
                                        and item.get("request_body", {}).get("end_dt") == day
                                        and _successful_return_code(item.get("response_body", {}).get("return_code")) for item in relevant))
                                totals = period["totals"]
                                expected = {"quantity": _number(quantity), "buy_amount": _number(cost),
                                            "sell_amount": _number(cost_gross)}
                                same_scope = valid_period and all(totals.get(key) == value for key, value in expected.items())
                                costs_match = same_scope and all(totals.get(key) == value for key, value in {
                                    "commission": _number(commission), "tax": _number(tax),
                                    "reported_net": None if reported_total is None else _number(reported_total)}.items())
                                comparison["period_cost_comparison"] = {
                                    "source_api": "ka10073", "observation_ids": period["observation_ids"],
                                    "observed_at": period["observed_at"], "scope_matches": bool(same_scope),
                                    "costs_and_reported_net_match": bool(costs_match), "totals": totals}
                                # The bound 10 won × rows is only a screening
                                # heuristic: neither TR documents row/daily
                                # commission truncation and scope equality
                                # alone does not establish the cause.
                                period_fee = _observed_number(str(totals.get("commission"))) if totals.get("commission") is not None else None
                                period_tax = _observed_number(str(totals.get("tax"))) if totals.get("tax") is not None else None
                                period_net = _observed_number(str(totals.get("reported_net")), signed=True) if totals.get("reported_net") is not None else None
                                fee_diff = None if period_fee is None else period_fee - commission
                                row_fee_candidate = (same_scope and period_tax == tax and fee_diff is not None
                                    and Decimal(0) < abs(fee_diff) <= Decimal(10*len(rows))
                                    and commission % 10 == 0 and period_fee % 10 == 0
                                    and reported_total is not None and period_net is not None
                                    and period_net - reported_total == -fee_diff
                                    and reported_matches and period.get("arithmetic_matches") is True)
                                combined_candidate = (same_scope and sub_won_candidate and period_tax == tax
                                    and fee_diff is not None and Decimal(0) < abs(fee_diff) <= Decimal(10*len(rows))
                                    and commission % 10 == 0 and period_fee % 10 == 0
                                    and reported_total is not None and period_net is not None
                                    and period_net - reported_total == -fee_diff
                                    and abs(period_net - (calculated-fee_diff)) < Decimal(1))
                                comparison["period_cost_comparison"]["row_fee_rounding"] = {
                                    "status": "possible_row_fee_rounding" if row_fee_candidate or combined_candidate else "not_established",
                                    "ka10072_commission": _number(commission),
                                    "ka10073_commission": None if period_fee is None else _number(period_fee),
                                    "difference_73_minus_72": None if fee_diff is None else _number(fee_diff),
                                    "screen_bound_krw": 10*len(rows), "cause_confirmed": False,
                                    "net_confirmed_by_this_test": False}
                                # Bounds are a *range of observed/calculated
                                # figures*, not a proven broker rounding rule
                                # or an assertion of the final account net.
                                # Require a complete 00 unit stream and both
                                # complete broker queries taken after it.
                                try:
                                    all_observations = observations_for_profit()
                                    observed_account = accounting_for_profit()
                                    observed_orders = [item for item in observed_account['orders'] if item['side'] == 'sell' and item['symbol'] == symbol]
                                    observed72 = next((item for item in observed_account['cost_reports'] if item['source_api'] == 'ka10072' and item['symbol'] == symbol), None)
                                    source_events = {item['id']: _observation_event(item) for item in all_observations}
                                    source72 = [source_events[i] for i in observed72['observation_ids']] if observed72 else []
                                    unit_times = [datetime.fromisoformat(obs['observed_at']) for item in observed_orders
                                                  for obs in item['reported_cost_observations']]
                                    # Include even an unregistered sell event
                                    # when checking time and identity: a stale
                                    # REST snapshot must not hide a newly
                                    # observed manual/unknown same-symbol sell.
                                    same_symbol_sells = [event for event in source_events.values()
                                        if event.get('source_api') == '00' and event.get('event_kind') == 'execution_stream'
                                        and event.get('values', {}).get('9001') in (symbol, 'A' + symbol)
                                        and event.get('values', {}).get('907') == '1' and event.get('values', {}).get('913') == '체결']
                                    unit_times.extend(datetime.fromisoformat(event['observed_at']) for event in same_symbol_sells)
                                    last_unit = max(unit_times) if unit_times else None
                                    full_units = (observed_orders and {item['order_number'] for item in observed_orders} == {row['ord_no'] for row in tracked}
                                        and all(event.get('values', {}).get('9203', '').lstrip('0') in {row['ord_no'] for row in tracked} for event in same_symbol_sells)
                                        and all(item['verified_amount_source'] == '00_unit_sum_reconciled_to_cumulative_quantity'
                                            and not item['stream_gap_observed'] and item['verified_quantity'] == item['rest_projection_quantity'] == item['ordered_quantity']
                                            for item in observed_orders)
                                        and sum((Decimal(item['verified_amount']) for item in observed_orders), Decimal(0)) == cost_gross)
                                    complete72 = (observed72 and observed72['complete_query'] and observed72.get('cash_symbol_scope_verified')
                                        and self._cost_evidence_rows(_profit_rows(observed72['profit_rows'], symbol), symbol) == cost_rows
                                        and all(item.get('request_body', {}).get('strt_dt') == day and _successful_return_code(item.get('response_body', {}).get('return_code')) for item in source72))
                                    timely = (last_unit is not None and source72 and relevant and
                                        all(datetime.fromisoformat(item['request_started_at']) > last_unit for item in source72 + relevant))
                                    rounding_case = (sub_won_candidate and same_scope and period_fee == commission and period_tax == tax
                                        and period_net is not None and abs(period_net-calculated) < Decimal(1))
                                    if (self._profit_source_kind == 'broker_observation' and full_units and complete72 and timely
                                            and same_scope and (row_fee_candidate or rounding_case or combined_candidate)):
                                        values = [reported_total, period_net, calculated.to_integral_value(rounding=ROUND_FLOOR)]
                                        low, high = min(values), max(values)
                                        comparison['conservative_estimate'] = {
                                            'status': 'observed_value_range_unconfirmed', 'conservative_net': _number(low),
                                            'min_net': _number(low), 'max_net': _number(high), 'width_krw': _number(high-low),
                                            'basis': _number(cost), 'reason': ('possible_row_fee_and_sub_won_rounding' if combined_candidate else 'possible_row_fee_rounding' if row_fee_candidate else 'possible_sub_won_basis_rounding'),
                                            'ka10072_net': _number(reported_total), 'ka10073_net': _number(period_net),
                                            'calculated_net_floor': _number(calculated.to_integral_value(rounding=ROUND_FLOOR)),
                                            'ka10072_query_id': observed72['query_id'], 'ka10073_query_id': period['query_id'],
                                            'last_unit_observed_at': last_unit.isoformat(), 'rounding_cause_confirmed': False,
                                            'final_net_confirmed': False}
                                except (ValueError, TypeError, KeyError, AttributeError, ExecutionLogError, ExecutionsError):
                                    pass
                                if not same_scope:
                                    net = basis = None
                                    reason = "미확정: ka10072·ka10073 조회 범위 또는 수량·대금 비교 불가"
                                elif not costs_match or reported_total is None:
                                    net = basis = None
                                    reason = "미확정: ka10072·ka10073 수수료·세금 또는 보고손익 불일치"
            # A matching typed 72 cache alone is not evidence that it includes
            # the latest actual sell. Require both latest raw queries, their
            # complete pages and quantities reconciled to 00 units. Monetary
            # differences remain subject to the existing amount reconciler.
            if self._profit_source_kind == "broker_observation":
                observations = observations_for_profit()
                post_sell_state, selected72 = _post_sell_report_state(day, mode, symbol, observations, as_of=self._moment())
                if post_sell_state["status"] == "reports_current":
                    try:
                        cache_matches = self._cost_evidence_rows(_profit_rows(selected72, symbol), symbol) == cost_rows
                    except (ExecutionLogError, ExecutionsError, ValueError, TypeError):
                        cache_matches = False
                    if not cache_matches:
                        post_sell_state["status"] = "post_sell_report_pending"
                        post_sell_state["reason_codes"].append("typed_72_cache_not_latest_response")
                if post_sell_state["status"] != "reports_current":
                    await_period = True
            # Serialize only the final fingerprint check and projection write
            # with WS sell insertion. A later sell invalidates atomically.
            if not db.in_transaction:
                db.execute("BEGIN IMMEDIATE")
            if (self._profit_input_fingerprint(db, mode, day, symbol) != initial_fingerprint
                    or sell_watermark is not None and self._latest_sell_observation_id(db, mode, day, symbol) != sell_watermark):
                await_period = True
                if post_sell_state is not None:
                    post_sell_state["status"] = "post_sell_report_pending"
                    post_sell_state["reason_codes"].append("inputs_changed_before_commit")
            if await_period:
                # A previously fetched ka10073 is not the independent response
                # for the current cycle; expose no confirmed value until it
                # has arrived and the two observations are compared again.
                net = basis = None
                reason = "미확정: 최신 매도 후 72·73 보고 및 비용 대사 대기(post_sell_report_pending)"
                if post_sell_state is not None and post_sell_state["status"] == "reports_current":
                    post_sell_state["status"] = "post_sell_report_pending"
                    post_sell_state["reason_codes"].append("current_period_query_pending")
                if comparison is not None:
                    # A stale observed range is not a current conservative
                    # estimate. Preserve the raw comparison evidence, but do
                    # not let downstream journal views publish the old bound.
                    comparison.pop("conservative_estimate", None)
                    comparison["cross_source_projection_status"] = "stale_or_period_pending"
            # Retain only typed, bounded cost fields, never raw account payloads.
            # The projection and its new source observation commit atomically.
            payload = canonical_json({"schema_version": 1, "day": day, "symbol": symbol,
                "source_api": "ka10072" if self._profit_source_kind == "broker_observation" else None,
                "source_kind": self._profit_source_kind, "rows": cost_rows,
                "amount_reconciliation": comparison,
                "post_sell_report_state": post_sell_state,
                "attribution_status": ("reconciled_with_unit_rounding" if net is not None and comparison and comparison.get("reconciliation_status") == "reconciled_with_unit_rounding"
                                       else "reconciled" if net is not None else "unconfirmed"),
                "net": net, "basis": basis, "reason": reason}, maximum=4 * 1024 * 1024)
            digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
            db.execute("INSERT OR IGNORE INTO profit_observations VALUES (?,?,?,?,?,?,?,?)",
                (self.scope, day, mode, symbol, digest, self._moment().isoformat(), self._profit_source_kind, payload))
            db.execute("INSERT OR REPLACE INTO profits VALUES (?,?,?,?,?,?,?)", (self.scope, day, mode, symbol, net, basis, reason))
        self._materialize(day)

    @staticmethod
    def _cost_evidence_rows(rows, symbol):
        if not isinstance(rows, (tuple, list)) or len(rows) > 20000:
            raise ExecutionLogError("손익 원자료의 범위를 확인하지 못했습니다.")
        result = []
        for row in rows:
            if (not isinstance(row, RealizedProfit) or row.symbol != symbol or type(row.quantity) is not int
                    or not 0 < row.quantity <= 999999999999):
                raise ExecutionLogError("손익 원자료의 종목·수량을 확인하지 못했습니다.")
            fields = {"price": row.price, "buy_price": row.buy_price, "commission": row.commission, "tax": row.tax}
            for key, value in fields.items():
                if (not isinstance(value, Decimal) or not value.is_finite() or value < 0
                        or key in ("price", "buy_price") and value <= 0 or value > Decimal("99999999999999999999")
                        or not -12 <= value.as_tuple().exponent <= 20):
                    raise ExecutionLogError("손익 원자료의 가격·비용을 확인하지 못했습니다.")
            reported = {key: getattr(row, key, None) for key in ("reported_net", "reported_rate", "reported_net_secondary")}
            for value in reported.values():
                if value is not None and (not isinstance(value, Decimal) or not value.is_finite()
                        or abs(value) > Decimal("99999999999999999999") or not -12 <= value.as_tuple().exponent <= 20):
                    raise ExecutionLogError("손익 원자료의 증권사 보고값을 확인하지 못했습니다.")
            result.append({"quantity": row.quantity, **{key: _number(value) for key, value in fields.items()},
                           **{key: None if value is None else _number(value) for key, value in reported.items()}})
        return result

    def _materialize(self, day):
        if self._historical_sidecar_deferred(day):
            return
        # Prevent an older concurrent CSV projection from replacing the
        # invalidated view produced by a newly recorded sell.
        with self._materialize_lock:
            return self._materialize_locked(day)

    def _materialize_locked(self, day):
        if re.fullmatch(r"[0-9]{8}", day) is None:
            raise ExecutionLogError("원장 파일 날짜가 잘못되었습니다.")
        self._materialize_history(day)
        self._materialize_accounting(day)
        with self._ro_db() as db:
            rows = list(db.execute("SELECT * FROM fills WHERE scope=? AND day=? ORDER BY moment,mode,CAST(ord_no AS INTEGER),CAST(fill_no AS INTEGER)", (self.scope, day)))
            summaries = list(db.execute("SELECT payload FROM summaries WHERE scope=? AND day=? ORDER BY mode", (self.scope, day)))
            profits = {(row["mode"], row["symbol"]): row for row in db.execute("SELECT * FROM profits WHERE scope=? AND day=?", (self.scope, day))}
        stream = io.StringIO(newline="")
        writer = csv.DictWriter(stream, fieldnames=FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow({"체결시간": day + " " + row["moment"], "종목명": _safe_csv(row["name"]),
                "매수매도": "매수" if row["side"] == "buy" else "매도", "가격": row["price"], "수량": row["quantity"],
                "전략": _safe_csv(row["strategy"]), "남은수량": row["remaining"] if row["remaining"] is not None else "미확정",
                "투자구분": row["mode"], "종목코드": row["symbol"], "주문번호": row["ord_no"].zfill(7),
                "체결번호": row["fill_no"].zfill(7), "잔고조회시각": row["holdings_at"] or "", "잔고상태": row["holdings_status"], "행구분": "체결"})
        for item in summaries:
            data = json.loads(item["payload"])
            current = [row for row in rows if row["mode"] == data["mode"]]
            buys = _number(sum((Decimal(row["price"]) * row["quantity"] for row in current if row["side"] == "buy"), Decimal(0)))
            sells = _number(sum((Decimal(row["price"]) * row["quantity"] for row in current if row["side"] == "sell"), Decimal(0)))
            costs = [profits.get((data["mode"], code)) for code in {row["symbol"] for row in current if row["side"] == "sell"}]
            complete = all(row is not None and row["net"] is not None and row["basis"] is not None for row in costs)
            current_net = _number(sum((Decimal(row["net"]) for row in costs), Decimal(0))) if complete else None
            current_basis = _number(sum((Decimal(row["basis"]) for row in costs), Decimal(0))) if complete else None
            if (buys, sells) != (data["buy"], data["sell"]) or data["net"] != "미확정" and (
                    not complete or (data["net"], data["basis"]) != (current_net, current_basis)):
                data.update(buy=buys, sell=sells, net="미확정", basis="미확정", rate="미확정",
                            status="체결 또는 비용 관측이 변경되어 결산 재대사 대기")
            writer.writerow({"행구분": "일일합계", "투자구분": data["mode"], "매수합계": data["buy"], "매도합계": data["sell"],
                "순이익": data["net"], "수익률": data["rate"], "수익률분모": data["basis"],
                "비고": "REST 가격×누적수량 참고합계(정확 약정금액 아님); 정확금액·부분체결·비용은 execution-accounting.json; 수익률=72계산손익/매도분 매입원가×100; " + data["status"]})
        target = self.log_dir / f"{day}.csv"
        if target.is_symlink():
            raise ExecutionLogError("CSV 출력 대상이 심볼릭 링크입니다.")
        payload = stream.getvalue().encode("utf-8-sig")
        if target.exists() and target.read_bytes() == payload:
            return
        temporary = None
        try:
            fd, temporary = tempfile.mkstemp(prefix=".executions-", dir=self.log_dir)
            with os.fdopen(fd, "wb") as handle:
                os.fchmod(handle.fileno(), 0o600)
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
            sync_directory(self.log_dir)
        finally:
            if temporary is not None:
                Path(temporary).unlink(missing_ok=True)

    def _materialize_history(self, day):
        with self._diagnostics.stage('history_export', work_day=day) as measurement:
            if self._historical_sidecar_deferred(day):
                return
            # Include lock waiting; SQLite/parse child stages separate active IO.
            with self._history_lock:
                self._materialize_history_locked(day, measurement=measurement)

    def _materialize_settlement_accounting(self, order, evidence_sha256, *, guard):
        """Publish one committed cancellation, never a general history override.

        A prior-day reservation must not wait for the historical cost queue.
        This purpose-bound path bypasses only regular-session deferral; source
        size limits, immutable proof, fresh caller guard and readback remain.
        """
        from .cancellation_settlement import CancellationSettlementUnavailable
        fields = ('scope', 'day', 'mode', 'ord_no', 'symbol', 'side', 'strategy', 'quantity', 'registered_at')
        if (not isinstance(order, dict) or any(key not in order for key in fields)
                or order.get('scope') != self.scope or order.get('mode') != 'paper'
                or order.get('side') != 'buy' or not callable(guard)
                or not isinstance(evidence_sha256, str) or re.fullmatch('[a-f0-9]{64}', evidence_sha256) is None):
            raise CancellationSettlementUnavailable('cancellation_accounting_authority_unverified')
        authorization = ({key: order[key] for key in fields}, evidence_sha256)
        # The caller's session/stream guard can acquire the recorder lock.
        # Never call it while holding the history lock used by WS writers.
        guard()
        document = self._materialize_accounting(order['day'], _settlement_authorization=authorization)
        guard()
        with self._db() as db:
            self._assert_settlement_accounting_authority(db, order['day'], authorization)
        guard()
        return document

    def _assert_settlement_accounting_authority(self, db, day, authorization):
        from .cancellation_settlement import CancellationSettlementUnavailable, committed_settlement_record
        expected, expected_hash = authorization
        # The exception never permits a 20k blocked day to hydrate again.
        if any(self._closed_limit_blocked(db, mode, day) for mode in ('paper', 'real')):
            raise CancellationSettlementUnavailable('cancellation_accounting_source_limit_blocked')
        current = db.execute('SELECT * FROM orders WHERE scope=? AND day=? AND mode=? AND ord_no=?',
            (self.scope, day, 'paper', expected['ord_no'])).fetchone()
        if current is None or any(current[key] != value for key, value in expected.items()):
            raise CancellationSettlementUnavailable('cancellation_accounting_original_changed')
        record = committed_settlement_record(db, self.scope, current)
        if record is None or record[0]['evidence_sha256'] != expected_hash:
            raise CancellationSettlementUnavailable('cancellation_accounting_evidence_changed')
        return record

    def _materialize_accounting(self, day, *, _settlement_authorization=None):
        """Atomically publish an explicitly derived view, preserving raw ledgers."""
        with self._diagnostics.stage('accounting', work_day=day) as measurement:
            if _settlement_authorization is None and self._historical_sidecar_deferred(day):
                return None
            with self._history_lock:
                if not isinstance(day, str) or re.fullmatch(r"[0-9]{8}", day) is None:
                    raise ExecutionLogError("약정금액 대사 파일의 일자를 확인하지 못했습니다.")
                with self._ro_db() as db:
                    if _settlement_authorization is not None:
                        self._assert_settlement_accounting_authority(db, day, _settlement_authorization)
                    orders = [dict(row) for row in db.execute("SELECT * FROM orders WHERE scope=? AND day=? ORDER BY mode,ord_no", (self.scope, day))]
                    fills = [dict(row) for row in db.execute("SELECT * FROM fills WHERE scope=? AND day=?", (self.scope, day))]
                    observations = self._broker_observations_snapshot(db, day, apis=("00", "ka10072", "ka10073", "ka10074"))
                    cancellation_notes = self._cancellation_accounting_notes(db, day)
                with localcontext() as context:
                    context.prec = 80
                    document = _execution_accounting(day, orders, fills, observations)
                    if cancellation_notes:
                        document['cancellation_settlements'] = cancellation_notes
                        document['notes'].append('Original ordered quantities and historical quantity-based pending comparisons remain unchanged; cancellation_settlements separately describes verified reservation closure. Cash and filled principal are never added back locally.')
                    payload = canonical_json(document, maximum=32 * 1024 * 1024).encode("utf-8")
                    measurement.set_counts(rows=len(observations), bytes_out=len(payload),
                                           bytes_out_basis='canonical_output_utf8')
                target = self.log_dir / f"{day}.execution-accounting.json"
                if target.is_symlink():
                    raise ExecutionLogError("약정금액 대사 출력 대상이 심볼릭 링크입니다.")
                if _settlement_authorization is not None:
                    with self._ro_db() as db:
                        self._assert_settlement_accounting_authority(db, day, _settlement_authorization)
                if target.exists() and target.read_bytes() == payload:
                    return document
                temporary = None
                try:
                    fd, temporary = tempfile.mkstemp(prefix=".execution-accounting-", dir=self.log_dir)
                    with os.fdopen(fd, "wb") as handle:
                        os.fchmod(handle.fileno(), 0o600)
                        handle.write(payload); handle.flush(); os.fsync(handle.fileno())
                    os.replace(temporary, target); sync_directory(self.log_dir)
                finally:
                    if temporary is not None:
                        Path(temporary).unlink(missing_ok=True)
                return document

    def _cancellation_accounting_notes(self, db, day):
        """Derived cancellation status only; never relabel a receipt as a fill."""
        from .cancellation_settlement import TABLE, committed_settlement_record
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(TABLE,)).fetchone() is None:
            return []
        rows=db.execute(f'SELECT * FROM {TABLE} WHERE scope=? AND day=? ORDER BY mode,original_ord_no LIMIT 10001',
                        (self.scope,day)).fetchall()
        if len(rows)>10000:
            raise ExecutionLogError('취소 확정 내역 조회 범위를 초과했습니다.')
        notes=[]
        for row in rows:
            order=db.execute('SELECT * FROM orders WHERE scope=? AND day=? AND mode=? AND ord_no=?',
                (self.scope,day,row['mode'],row['original_ord_no'])).fetchone()
            item={'original_order_number':row['original_ord_no'],'mode':row['mode'],'symbol':row['symbol'],
                  'confirmed_at':row['confirmed_at'],'evidence_sha256':row['evidence_sha256'],
                  'original_order_quantity':order['quantity'] if order else None,
                  'filled_quantity_at_confirmation':row['filled_qty'],
                  'cancelled_quantity_at_confirmation':row['cancelled_qty'],
                  'cash_adjustment_krw':0,'filled_principal_released_krw':0}
            try:
                if order is None:raise ExecutionLogError('원주문 부재')
                verified=committed_settlement_record(db,self.scope,order)
                if verified is None:raise ExecutionLogError('취소 확정 증빙 부재')
                item.update(status='final_evidence_committed',broker_resolution=verified[1]['status'])
            except (ExecutionLogError,ValueError,TypeError,KeyError,sqlite3.Error):
                item.update(status='confirmation_invalidated_requires_review',broker_resolution=None)
            notes.append(item)
        return notes

    def _materialize_history_locked(self, day, *, measurement=None):
        if not isinstance(day, str) or re.fullmatch(r"[0-9]{8}", day) is None:
            raise ExecutionLogError("관측 이력 날짜를 확인하지 못했습니다.")
        written_rows = written_bytes = 0
        for table, suffix in (("execution_observations", "execution-history"), ("broker_observations", "broker-observations"), ("broker_observation_repeat_receipts", "broker-observation-repeats")):
            target = self.log_dir / f"{day}.{suffix}.jsonl"
            if target.is_symlink():
                raise ExecutionLogError("관측 이력 출력 대상이 심볼릭 링크입니다.")
            cached = self._history_exports.get(target)
            signature = lambda info: (info.st_ino, info.st_size, info.st_mtime_ns)
            append = cached is not None and target.exists() and signature(target.stat()) == cached[1]
            temporary = None
            try:
                with self._ro_db() as db:
                    identity = self._database_identity(db)
                    snapshot = None  # JSONL exports physical originals/compact receipts, never re-expanded repeats.
                    db.execute("SAVEPOINT history_export_snapshot")
                    try:
                        count, last_id = ((len(snapshot), snapshot[-1].id if snapshot else 0) if snapshot is not None
                            else db.execute(f"SELECT COUNT(*),COALESCE(MAX(id),0) FROM {table} WHERE scope=? AND day=?", (self.scope, day)).fetchone())
                        append = (append and cached[2] == identity and count >= cached[3]
                            and ((count == cached[3] and last_id == cached[0])
                                or (count > cached[3] and last_id > cached[0])))
                        if append and cached[3]:
                            tail_index = bisect_right(snapshot, cached[0], key=lambda row: row.id) - 1 if snapshot is not None else -1
                            tail = ((snapshot[tail_index].digest,) if tail_index >= 0 and snapshot[tail_index].id == cached[0]
                                else None) if snapshot is not None else db.execute(
                                    f"SELECT digest FROM {table} WHERE scope=? AND day=? AND id=?", (self.scope, day, cached[0])).fetchone()
                            append = tail is not None and tail[0] == cached[4]
                        after_id = cached[0] if append else 0
                        if snapshot is not None:
                            # No second JSON parse or full-prefix SQL read for
                            # the raw JSONL export of the same immutable rows.
                            rows = snapshot[bisect_right(snapshot, after_id, key=lambda row: row.id):]
                        else:
                            rows = list(db.execute(f"SELECT id,digest,payload_json FROM {table} WHERE scope=? AND day=? AND id>? ORDER BY id",
                                (self.scope, day, after_id)))
                    finally:
                        db.execute("RELEASE history_export_snapshot")
                if not rows and (append or not target.exists()):
                    continue
                if append:
                    descriptor = os.open(target, os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW)
                    if signature(os.fstat(descriptor)) != cached[1]:
                        os.close(descriptor)
                        raise ExecutionLogError("관측 이력 파일이 동시 변경되어 재생성이 필요합니다.")
                else:
                    descriptor, temporary = tempfile.mkstemp(prefix=".execution-history-", dir=self.log_dir)
                with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                    os.fchmod(output.fileno(), 0o600)
                    initial_size = os.fstat(output.fileno()).st_size if append else 0
                    last_digest = cached[4] if append else None
                    for row in rows:
                        output.write(json.dumps({"record_type": table, "id": row["id"], "sha256": row["digest"],
                            "payload": _observation_event(row)}, ensure_ascii=False, sort_keys=True) + "\n")
                        after_id = row["id"]
                        last_digest = row["digest"]
                    output.flush(); os.fsync(output.fileno())
                    exported_signature = signature(os.fstat(output.fileno()))
                    written_rows += len(rows)
                    written_bytes += exported_signature[1] - initial_size
                if not append:
                    os.replace(temporary, target); sync_directory(self.log_dir)
                self._history_exports[target] = (after_id, exported_signature, identity, count, last_digest)
            except Exception:
                self._history_exports.pop(target, None)
                raise
            finally:
                if temporary is not None:
                    Path(temporary).unlink(missing_ok=True)
        if measurement is not None:
            measurement.set_counts(rows=written_rows, bytes_out=written_bytes,
                                   bytes_out_basis='export_written_bytes')

    @_diagnosed('summary')
    def _summary(self, mode, day, notify):
        if self._historical_sidecar_deferred(day):
            return
        # Report amounts come from this CSV. Rebuild it, then cross-check its
        # trade identities and amounts against the durable ledger.
        self._materialize(day)
        accounting = self._materialize_accounting(day)
        with self._db() as db:
            fills = list(db.execute("SELECT * FROM fills WHERE scope=? AND day=? AND mode=?", (self.scope, day, mode)))
            profits = {row["symbol"]: row for row in db.execute("SELECT * FROM profits WHERE scope=? AND day=? AND mode=?", (self.scope, day, mode))}
            unresolved = db.execute("SELECT COUNT(*) FROM intents WHERE scope=? AND day=? AND mode=? AND order_number IS NULL AND state='prepared'", (self.scope, day, mode)).fetchone()[0]
            with localcontext() as context:
                context.prec = 60
                buy, sell = self._csv_totals(mode, day, fills)
                symbols = {row["symbol"] for row in fills if row["side"] == "sell"}
                known = not unresolved and all(code in profits and profits[code]["net"] is not None for code in symbols)
                net = sum((Decimal(profits[code]["net"]) for code in symbols), Decimal(0)) if known else None
                basis = sum((Decimal(profits[code]["basis"]) for code in symbols), Decimal(0)) if known else None
                data = {"day": day, "mode": mode, "buy": _number(buy), "sell": _number(sell),
                        "verified_totals": accounting["verified_totals"].get(mode, {}),
                        "net": _number(net) if net is not None else "미확정",
                        "basis": _number(basis) if basis is not None else "미확정",
                        "rate": format(net / basis * 100, ".4f") + "%" if basis and net is not None else ("해당 없음" if known else "미확정"),
                        "status": "ka10072 기준 계산값; 계좌 최종 손익·출처 간 비용 대사는 별도 확인" if known and symbols else ("실현 매도 없음" if known else "비용 또는 자동매도 귀속 범위 미확정")}
                if unresolved:
                    data["status"] += f"; 접수 미확정 자동주문 의도 {unresolved}건(주문내역 확인 필요, 체결 전략 추정 안 함)"
            serialized = json.dumps(data, ensure_ascii=False, sort_keys=True)
            digest = hashlib.sha256(serialized.encode()).hexdigest()
            prior = db.execute("SELECT digest FROM summaries WHERE scope=? AND day=? AND mode=?", (self.scope, day, mode)).fetchone()
            if prior and prior[0] == digest:
                self._materialize(day)
                return
            # Commit before notification: uncertain Telegram delivery is not resent.
            db.execute("INSERT OR REPLACE INTO summaries VALUES (?,?,?,?,?)", (self.scope, day, mode, digest, serialized))
        self._materialize(day)
        label = "모의" if mode == "paper" else "실전"
        correction = " 정정" if prior else ""
        verified_lines = []
        for side, side_label in (("buy", "매수"), ("sell", "매도")):
            verified = data["verified_totals"].get(side)
            if not verified or verified["coverage"] == "no_registered_orders":
                verified_lines.append(f"{side_label} 검증금액: 등록 주문 없음")
            else:
                amount = verified["verified_observed_amount"]
                verified_lines.append(f"{side_label} 검증된 관측분: {amount + '원' if amount is not None else '미확정'} "
                    f"· 수량대사 {verified['verified_order_count']}건 · 미확인 {verified['unconfirmed_order_count']}건 "
                    f"· 부분체결 {verified['partially_filled_order_count']}건 · 잔여미체결 {verified['pending_order_count']}건 "
                    f"· 등록주문 전체금액 {verified['fully_verified_amount'] + '원' if verified['fully_verified_amount'] is not None else '미확정'}")
        notify(f"{day} {label} 자동매매 일일결산{correction}\n매수 합계 {data['buy']}원 · 매도 합계 {data['sell']}원 (REST 참고금액)\n"
               + "\n".join(verified_lines) + "\n"
               f"ka10072 기준 계산 순손익 {data['net']} · 수익률 {data['rate']}\n수익률 분모: 매도분 매입원가 {data['basis']}원\n{data['status']}\nlogs/{day}.csv\n정확약정금액·비용: logs/{day}.execution-accounting.json")

    def _csv_totals(self, mode, day, fills):
        target = self.log_dir / f"{day}.csv"
        if target.is_symlink():
            raise ExecutionLogError("일일 결산 CSV 파일이 심볼릭 링크입니다.")
        with target.open(encoding="utf-8-sig", newline="") as source:
            reader = csv.DictReader(source)
            if reader.fieldnames != FIELDS:
                raise ExecutionLogError("일일 결산 CSV의 열 구성이 원장과 다릅니다.")
            rows = [row for row in reader if row.get("행구분") == "체결" and row.get("투자구분") == mode]
        columns = ("체결시간", "투자구분", "종목코드", "주문번호", "체결번호", "매수매도", "가격", "수량", "전략", "종목명")
        actual = sorted(tuple(row.get(column) for column in columns) for row in rows)
        expected = sorted((day + " " + row["moment"], mode, row["symbol"], row["ord_no"].zfill(7),
            row["fill_no"].zfill(7), "매수" if row["side"] == "buy" else "매도", row["price"], str(row["quantity"]),
            _safe_csv(row["strategy"]), _safe_csv(row["name"])) for row in fills)
        if actual != expected:
            raise ExecutionLogError("일일 결산 CSV의 체결 내역이 원장과 달라 합계를 보내지 않았습니다.")
        buy = sum((Decimal(row["가격"]) * int(row["수량"]) for row in rows if row["매수매도"] == "매수"), Decimal(0))
        sell = sum((Decimal(row["가격"]) * int(row["수량"]) for row in rows if row["매수매도"] == "매도"), Decimal(0))
        return buy, sell

    def tick(self, session, notify):
        """Compatibility entry point for a caller that owns the whole cycle."""
        self._defer_prior_day_sidecars = False
        return self._run_reconciliation_lane(session, notify, lane='combined')

    def tick_current(self, session, notify):
        """Run one current-day accounting step on the receiver thread."""
        self._defer_prior_day_sidecars = True
        return self._run_reconciliation_lane(session, notify, lane='current')

    def tick_history(self, session, notify, *, stop_requested=None):
        """Run one past-date step on a separate worker after verified close."""
        return self._run_reconciliation_lane(session, notify, lane='history',
                                             stop_requested=stop_requested)

    def _run_reconciliation_lane(self, session, notify, *, lane, stop_requested=None):
        if not self._reconciliation_lane_lock.acquire(blocking=False):
            return 'busy' if lane == 'history' else None
        try:
            return self._tick_reconciliation(session, notify, lane=lane, stop_requested=stop_requested)
        finally:
            self._reconciliation_lane_lock.release()

    def _queue_profit_tasks(self, db, mode, days):
        intraday_slots = 4
        for day in days:
            now = self._moment()
            symbols = [row[0] for row in db.execute(
                "SELECT DISTINCT symbol FROM fills WHERE scope=? AND day=? AND mode=? AND side='sell' ORDER BY symbol",
                (self.scope, day, mode))]
            closed = self._closed(day, now)
            if closed:
                selected = symbols
            else:
                due = sorted(symbols, key=lambda code: self._profit_refresh_at.get(
                    (mode, day, code), datetime.min.replace(tzinfo=KST)))
                selected = [code for code in due
                            if self._profit_refresh_at.get((mode, day, code), now) <= now][:intraday_slots]
                intraday_slots -= len(selected)
            self._tasks.extend(("profit", day, code) for code in selected)
            if self.period_profit_fetcher is not None:
                self._tasks.extend(("period_profit", day, code) for code in selected)
            if symbols and self.daily_profit_fetcher is not None and self._daily_profit_refresh_at.get((mode, day), now) <= now:
                self._tasks.append(("daily_profit", day, None))
            if closed:
                self._tasks.append(("summary", day, None))
                if self._closed_backoff_enabled:
                    self._tasks.append(("closed_complete", day, None))

    @_diagnosed('tracker_cycle')
    def _tick_reconciliation(self, session, notify, *, lane, stop_requested):
        """One bounded read/publish stage per call; no orders or auto login."""
        mode = day = None
        history_only = lane == 'history'
        current_only = lane == 'current'
        guard_ready = False
        try:
            now = self._moment()
            if history_only:
                if getattr(session, 'mode', None) != 'paper' or (stop_requested is not None and stop_requested()):
                    return 'paused'
                if not self._history_allowed(now):
                    return 'awaiting_verified_close'
            identity = session_identity(session)
            today = now.strftime('%Y%m%d')

            def history_guard():
                if not history_only:
                    return
                at = self._moment()
                if ((stop_requested is not None and stop_requested())
                        or at.strftime('%Y%m%d') != today or at < now or not self._history_allowed(at)):
                    raise _HistoryLanePaused('historical_lane_no_longer_available')
                try:
                    same_session = session_identity(session) == identity
                except MinuteChartError:
                    same_session = False
                if not same_session:
                    raise _HistoryLanePaused('historical_lane_no_longer_available')

            guard_ready = True
            queue_options = {'before_send': history_guard, 'priority': 'research'} if history_only else {}
            if self._closed_last_clock is not None and now < self._closed_last_clock:
                # A backwards wall-clock jump must not postpone work until an
                # old future deadline or certify stale cached observations.
                with self._closed_cache_lock:
                    self._closed_reconciliation.clear()
                    self._closed_attempts.clear()
                    self._closed_last_discrepancy.clear()
                self._tasks.clear()
                self._snapshots.clear()
                self._next_at = now
                self._balance_refresh_at = now + timedelta(seconds=60)
            self._closed_last_clock = now
            if identity != self._identity:
                self._broker_dedup_epoch = uuid.uuid4().hex
                self._tasks.clear()
                self._snapshots.clear()
                self._profit_rows_cache.clear()
                with self._closed_cache_lock:
                    self._closed_reconciliation.clear()
                    self._closed_attempts.clear()
                    self._closed_last_discrepancy.clear()
                self._identity = identity
                self._next_at = now
                self._balance_refresh_at = now + timedelta(seconds=60)
            mode = identity[0]
            historical_deferred = current_only or (not history_only and self._historical_work_deferred(now))
            if current_only:
                self._historical_guard_status = {'deferred': True, 'basis': 'separate_history_lane'}
            self._prune_historical_tasks(mode, today, now, historical_deferred)
            if history_only:
                self._tasks = deque(task for task in self._tasks if task[1] < today)
                self._cycle_days = [day for day in self._cycle_days if day < today]
            with self._closed_cache_lock:
                if self._closed_force_refresh:
                    self._closed_force_refresh = False
                    self._next_at = now
            with self._sell_refresh_lock:
                pending = sorted(item for item in self._sell_refresh_pending
                                 if item[0] == mode and ((history_only and item[1] < today)
                                     or (not history_only and (not historical_deferred or item[1] == today))))
                if pending and not historical_deferred:
                    with self._db() as db:
                        pending = [item for item in pending if item[1] == today
                            or not self._closed_limit_blocked(db, mode, item[1])]
                self._sell_refresh_pending.difference_update(pending)
            if pending:
                # Coalesce repeated callbacks and preserve existing queued work.
                # The existing shared HTTP queue still enforces TR rate limits.
                # Refresh executions and holdings before reporting profit; a
                # cost query that overtakes these reads would show stale fills
                # and can delay the account's safety-critical reconciliation.
                urgent = []
                for _, sell_day, _ in pending:
                    task = ("executions", sell_day, None)
                    if task not in urgent:
                        urgent.append(task)
                if not history_only:
                    urgent.append(("holdings", today, None))
                    for _, sell_day, sell_symbol in pending:
                        symbol_tasks = [("profit", sell_day, sell_symbol)]
                        if self.period_profit_fetcher is not None:
                            symbol_tasks.append(("period_profit", sell_day, sell_symbol))
                        for task in symbol_tasks:
                            if task not in urgent:
                                urgent.append(task)
                        daily = ("daily_profit", sell_day, None)
                        if self.daily_profit_fetcher is not None and daily not in urgent:
                            urgent.append(daily)
                if urgent:
                    self._tasks = deque(task for task in self._tasks if task not in urgent)
                    if not self._tasks:
                        self._cycle_started_at = now
                    self._tasks.extendleft(reversed(urgent))
                    self._next_at = now
            if self._next_at and now < self._next_at:
                return 'waiting' if history_only else None
            # Revisit paper balances during long profit/history queues. Do not
            # interrupt an in-flight request, overtake queued executions, or
            # regenerate the profit task list: the ordinary cycle owns it.
            # Set the next attempt before IO to avoid a tight retry loop.
            if (not history_only and mode == "paper" and self._tasks and self._balance_refresh_at
                    and now >= self._balance_refresh_at
                    and self._tasks[0][0] not in ("executions", "holdings", "balance_refresh")):
                self._tasks.appendleft(("balance_refresh", now.strftime("%Y%m%d"), None))
                self._balance_refresh_at = now + timedelta(seconds=60)
            if not self._tasks:
                self._cycle_started_at = now
                with self._db() as db:
                    if historical_deferred:
                        recorded_days = [row[0] for row in db.execute(
                            "SELECT day FROM orders WHERE scope=? AND mode=? AND day=? "
                            "UNION SELECT day FROM intents WHERE scope=? AND mode=? AND day=?",
                            (self.scope, mode, today, self.scope, mode, today))]
                    else:
                        recorded_days = [row[0] for row in db.execute(
                            "SELECT day FROM orders WHERE scope=? AND mode=? AND day<=? "
                            "UNION SELECT day FROM intents WHERE scope=? AND mode=? AND day<=? ORDER BY day DESC",
                            (self.scope, mode, today, self.scope, mode, today))]
                    history = [day for day in recorded_days if day < today]
                    cursor = self._history_cursor.get(mode, 0) % max(1, len(history))
                    rotated = history[cursor:] + history[:cursor]
                    days = rotated[:7]
                    self._history_cursor[mode] = (cursor + len(days)) % max(1, len(history))
                    if not history_only and (now.weekday() < 5 or today in recorded_days):
                        days.insert(0, today)
                    # Completed dates are retried on a bounded schedule when
                    # their financial fingerprint remains unchanged.
                    if self._closed_backoff_enabled:
                        days = [saved_day for saved_day in days
                                if not self._closed(saved_day, now) or self._closed_due(db, mode, saved_day, now)]
                    else:
                        # Disabling cadence backoff is not permission to
                        # bypass the independent immutable-source size guard.
                        days = [saved_day for saved_day in days if not self._closed(saved_day, now)
                                or not self._closed_limit_blocked(db, mode, saved_day)]
                self._cycle_days = days
                self._tasks.extend(("executions", day, None) for day in days)
                if not history_only:
                    self._tasks.append(("holdings", today, None))
                elif not self._tasks:
                    self._next_at = now + timedelta(seconds=10)
                    return 'idle'
            history_guard()
            action, day, symbol = self._tasks.popleft()
            with self._diagnostics.stage('tracker_task', work_day=day, mode=mode):
                recheck_now = False
                if action == "executions":
                    observed_execution_pages = []
                    def observe_execution(event):
                        history_guard()
                        self.record_broker_observation(event)
                        if event.get('source_api') == 'kt00009' and event.get('mode') == mode and event.get('day') == day:
                            observed_execution_pages.append(event)
                    with self._diagnostics.stage('executions_fetch') as measurement:
                        data = (self.executions_fetcher(session, day, observer=observe_execution, **queue_options)
                                if self._default_executions_fetcher else self.executions_fetcher(session, day))
                        measurement.set_counts(rows=len(data))
                    history_guard()
                    if session_identity(session) != identity:
                        raise ExecutionLogError("체결 조회 중 로그인 상태가 바뀌었습니다.")
                    self._ingest(mode, day, data)
                    if self._default_executions_fetcher and not _complete_observation_pages(observed_execution_pages):
                        self._closed_incomplete(mode, day)
                    if history_only:
                        history_guard()
                        with self._db() as db:
                            self._queue_profit_tasks(db, mode, (day,))
                elif action in ("holdings", "balance_refresh"):
                    with self._diagnostics.stage('holdings_fetch') as measurement:
                        data = self.account_fetcher(session)
                        if isinstance(data, dict) and isinstance(data.get('holdings'), list):
                            measurement.set_counts(rows=len(data['holdings']))
                    if session_identity(session) != identity:
                        raise ExecutionLogError("잔고 조회 중 로그인 상태가 바뀌었습니다.")
                    observed_at = self._moment()
                    self._holdings(mode, data, observed_at)
                    if session_identity(session) == identity:
                        self._save_paper_balance_snapshot(mode, data, observed_at)
                    if self._closed_backoff_enabled:
                        with self._db() as db:
                            row = db.execute('SELECT discrepancy FROM holdings WHERE scope=? AND mode=?',
                                             (self.scope, mode)).fetchone()
                        current = tuple(sorted(json.loads(row[0]))) if row is not None else None
                        with self._closed_cache_lock:
                            prior = self._closed_last_discrepancy.get(mode)
                            if prior is not None and current != prior:
                                self._closed_reconciliation.clear()
                                for attempt in self._closed_attempts.values():
                                    attempt['ok'] = False
                                recheck_now = True
                            self._closed_last_discrepancy[mode] = current
                    if mode == "paper":
                        self._balance_refresh_at = observed_at + timedelta(seconds=60)
                    if action == "balance_refresh":
                        # One bounded task only: leave pending executions, profit
                        # and history work exactly as it stood before the refresh.
                        self._next_at = observed_at if self._tasks or recheck_now else max(observed_at, self._cycle_started_at + timedelta(seconds=10))
                        return
                    with self._db() as db:
                        self._queue_profit_tasks(db, mode, self._cycle_days)
                elif action == "profit":
                    history_guard()
                    self._profit_refresh_at[(mode, day, symbol)] = self._moment() + timedelta(seconds=60)
                    self._invalidate_profit(mode, day, symbol)
                    history_guard()
                    observed_profit_pages = []
                    def observe_profit(event):
                        history_guard()
                        self.record_broker_observation(event)
                        if (event.get('source_api') == 'ka10072' and event.get('mode') == mode and event.get('day') == day
                                and event.get('request_body', {}).get('stk_cd', '').removeprefix('A') == symbol):
                            observed_profit_pages.append(event)
                    try:
                        with self._diagnostics.stage('profit_fetch') as measurement:
                            data = (self.profit_fetcher(session, symbol, day, observer=observe_profit, **queue_options)
                                    if self._default_profit_fetcher else self.profit_fetcher(session, symbol, day))
                            measurement.set_counts(rows=len(data))
                    except Exception as error:
                        self._closed_incomplete(mode, day)
                        if isinstance(error, ExecutionLogError) or getattr(error, "observation_failed", False) or getattr(error, "ip_related", False):
                            raise
                        data = ()  # unavailable costs are explicitly unknown, never zero
                    history_guard()
                    if session_identity(session) != identity:
                        raise ExecutionLogError("손익 조회 중 로그인 상태가 바뀌었습니다.")
                    self._profit(mode, day, symbol, data, await_period=self.period_profit_fetcher is not None)
                    self._profit_rows_cache[(mode, day, symbol)] = tuple(data)
                    if self._default_profit_fetcher and not _complete_observation_pages(observed_profit_pages):
                        self._closed_incomplete(mode, day)
                elif action == "period_profit":
                    history_guard()
                    self._invalidate_profit(mode, day, symbol)
                    history_guard()
                    observed_period_pages = []
                    def observe_period(event):
                        history_guard()
                        self.record_broker_observation(event)
                        if (event.get("source_api") == "ka10073" and event.get("mode") == mode
                                and event.get("day") == day
                                and event.get("request_body", {}).get("stk_cd", "").removeprefix("A") == symbol):
                            observed_period_pages.append(event)
                    try:
                        with self._diagnostics.stage('period_profit_fetch'):
                            options = queue_options if self.period_profit_fetcher is fetch_period_realized_profit else {}
                            self.period_profit_fetcher(session, symbol, day, observer=observe_period, **options)
                    except Exception as error:
                        if isinstance(error, ExecutionLogError) or getattr(error, "observation_failed", False) or getattr(error, "ip_related", False):
                            raise
                        raise ExecutionLogError("기간 종목 손익(ka10073)을 확인하지 못했습니다. 출처 간 비용 대사는 미확정입니다.") from None
                    history_guard()
                    if session_identity(session) != identity:
                        raise ExecutionLogError("기간 종목 손익 조회 중 로그인 상태가 바뀌었습니다.")
                    cache_key = (mode, day, symbol)
                    with self._db() as db:
                        sell_watermark = self._latest_sell_observation_id(db, mode, day, symbol)
                    with self._sell_refresh_lock:
                        has_new_sell = cache_key in self._sell_refresh_pending
                    # Only re-evaluate from the exact locally received, complete
                    # period query. A failed/missing/partial observation stays
                    # explicitly unconfirmed; it cannot reuse an older response.
                    if (not has_new_sell and cache_key in self._profit_rows_cache
                            and _complete_observation_pages(observed_period_pages)):
                        history_guard()
                        self._profit(mode, day, symbol, self._profit_rows_cache[cache_key], sell_watermark=sell_watermark)
                    else:
                        self._closed_incomplete(mode, day)
                elif action == "daily_profit":
                    history_guard()
                    self._daily_profit_refresh_at[(mode, day)] = self._moment() + timedelta(seconds=60)
                    # ka10074 reports the whole account, including possible manual
                    # trades. Preserve every page; never allocate it to a strategy
                    # or overwrite ka10072 when its values conflict.
                    observed_daily_pages = []
                    def observe_daily(event):
                        history_guard()
                        self.record_broker_observation(event)
                        if event.get('source_api') == 'ka10074' and event.get('mode') == mode and event.get('day') == day:
                            observed_daily_pages.append(event)
                    try:
                        with self._diagnostics.stage('daily_profit_fetch'):
                            options = queue_options if self.daily_profit_fetcher is fetch_daily_realized_profit else {}
                            self.daily_profit_fetcher(session, day, observer=observe_daily, **options)
                    except Exception as error:
                        if isinstance(error, ExecutionLogError) or getattr(error, "observation_failed", False) or getattr(error, "ip_related", False):
                            raise
                        raise ExecutionLogError("계좌 일일 손익(ka10074)을 확인하지 못했습니다. 계좌 비용 대사는 미확정입니다.") from None
                    history_guard()
                    if session_identity(session) != identity:
                        raise ExecutionLogError("계좌 일일 손익 조회 중 로그인 상태가 바뀌었습니다.")
                    if not _complete_observation_pages(observed_daily_pages):
                        self._closed_incomplete(mode, day)
                elif action == "summary":
                    history_guard()
                    before_summary = None
                    if self._closed_backoff_enabled:
                        with self._db() as db:
                            before_summary, _ = self._closed_economic_fingerprint(db, mode, day)
                    history_guard()
                    self._summary(mode, day, notify)
                    history_guard()
                    if self._closed_backoff_enabled:
                        with self._db() as db:
                            after_summary, _ = self._closed_economic_fingerprint(db, mode, day)
                        with self._closed_cache_lock:
                            attempt = self._closed_attempts.get((mode, day))
                            if attempt is not None:
                                attempt['summary_stable'] = before_summary == after_summary
                                attempt['summary_fingerprint'] = after_summary
                elif action == "closed_complete":
                    history_guard()
                    recheck_now = not self._closed_finish(mode, day, self._moment())
                else:
                    raise ExecutionLogError("자동매매 원장 작업 종류를 확인하지 못했습니다.")
                history_guard()
                completed = self._moment()
                self._next_at = completed if self._tasks or recheck_now else max(completed, self._cycle_started_at + timedelta(seconds=10))
                return 'stage_finished' if history_only else None
        except Exception as error:
            # A revoked history lease is ordinary shutdown or rollover. Do
            # not fingerprint old days or generate an error after revocation.
            if history_only and guard_ready:
                try:
                    history_guard()
                except _HistoryLanePaused:
                    self._tasks.clear()
                    self._snapshots.clear()
                    with self._closed_cache_lock:
                        self._closed_attempts.clear()
                    return 'paused'
                except ExecutionLogError:
                    pass
            try:
                failed_at = self._moment()
                with self._closed_cache_lock:
                    attempted = list(self._closed_attempts)
                for failed_mode, failed_day in attempted:
                    self._closed_failed(failed_mode, failed_day, failed_at)
            except Exception:
                pass
            self._tasks.clear()
            self._snapshots.clear()
            try:
                self._next_at = self._moment() + timedelta(seconds=10)
            except Exception:
                self._next_at = None
            if not getattr(session, "authenticated", False):
                self._identity = None
                return
            message = str(error) if isinstance(error, ExecutionLogError) or getattr(error, "ip_related", False) else "자동매매 원장: 체결·잔고 확인 또는 파일 기록을 완료하지 못했습니다. 실제 체결을 추정해 기록하지 않았습니다."
            self._notify_reconciliation_error(message, notify)
            return 'reconciliation_unavailable' if history_only else None

    def _notify_reconciliation_error(self, message, notify):
        """Keep repeated errors quiet even when other tracker tasks succeed."""
        now = monotonic()
        last = self._error_notice_times.get(message)
        if last is not None and now - last < _ERROR_NOTICE_COOLDOWN_SECONDS:
            return
        self._error_notice_times = {
            text: at for text, at in self._error_notice_times.items()
            if now - at < _ERROR_NOTICE_COOLDOWN_SECONDS
        }
        if len(self._error_notice_times) >= _ERROR_NOTICE_CACHE_LIMIT:
            self._error_notice_times.pop(next(iter(self._error_notice_times)))
        self._error_notice_times[message] = now
        try:
            notify(message)
        except Exception:
            pass
