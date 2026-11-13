"""Read broker execution snapshots (kt00009) and realized costs (ka10072).

Sources: /tr_docs/kt00009.md and /tr_docs/ka10072.md. kt00009 supplies
cntr_no/cntr_tm; ord_tm from other TRs is NOT a fill timestamp. A broker row can
change between polls or contain a rounded average/cumulative quantity. Its ID
alone does not prove an immutable individual partial fill. Observe raw allowed
fields before parsing and let reconciliation retain revisions. No orders.
"""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
import http.client
import json
import re
import urllib.error
import urllib.request
import uuid

from .minute_chart import session_identity
from .errors import BrokerAPIError
from .session import KST
from kiwoom_autotrader.utils.http_queue import queued_open

_ORIGINS = {"paper": "https://mockapi.kiwoom.com", "real": "https://api.kiwoom.com"}
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_MAX_PAGES = 20
_MAX_ROWS = 20000

# Documented body fields only. No account number, authentication field, provider
# message or opaque continuation key is an observation field. kt00007 is an
# order-status detail and kt00008 a settlement forecast: neither supplies an
# individual execution ID/time contract equivalent to kt00009.
BROKER_OBSERVATION_SCHEMAS = {
    "ka10075": {
        "request_fields": frozenset("all_stk_tp trde_tp stk_cd stex_tp".split()),
        "response_fields": frozenset(("return_code", "oso")),
        "row_field": "oso",
        # acnt_no and mang_empno are intentionally never persisted.
        "row_fields": frozenset("ord_no stk_cd tsk_tp ord_stt stk_nm ord_qty ord_pric oso_qty cntr_tot_amt orig_ord_no io_tp_nm trde_tp tm cntr_no cntr_pric cntr_qty cur_prc sel_bid buy_bid unit_cntr_pric unit_cntr_qty tdy_trde_cmsn tdy_trde_tax ind_invsr stex_tp stex_tp_txt sor_yn stop_pric".split()),
    },
    "kt00009": {
        "request_fields": frozenset("ord_dt stk_bond_tp mrkt_tp sell_tp qry_tp stk_cd fr_ord_no dmst_stex_tp".split()),
        "response_fields": frozenset("return_code sell_grntl_engg_amt buy_engg_amt engg_amt acnt_ord_cntr_prst_array".split()),
        "row_field": "acnt_ord_cntr_prst_array",
        "row_fields": frozenset("stk_bond_tp ord_no stk_cd trde_tp io_tp_nm ord_qty ord_uv cnfm_qty rsrv_oppo cntr_no acpt_tp orig_ord_no stk_nm setl_tp crd_deal_tp cntr_qty cntr_uv comm_ord_tp mdfy_cncl_tp cntr_tm dmst_stex_tp cond_uv".split()),
    },
    "ka10072": {
        "request_fields": frozenset("stk_cd strt_dt".split()),
        "response_fields": frozenset(("return_code", "dt_stk_div_rlzt_pl")),
        "row_field": "dt_stk_div_rlzt_pl",
        "row_fields": frozenset("stk_nm cntr_qty buy_uv cntr_pric tdy_sel_pl pl_rt stk_cd tdy_trde_cmsn tdy_trde_tax wthd_alowa loan_dt crd_tp stk_cd_1 tdy_sel_pl_1".split()),
    },
    "ka10073": {
        "request_fields": frozenset("stk_cd strt_dt end_dt".split()),
        "response_fields": frozenset(("return_code", "dt_stk_rlzt_pl")),
        "row_field": "dt_stk_rlzt_pl",
        "row_fields": frozenset("dt tdy_htssel_cmsn stk_nm cntr_qty buy_uv cntr_pric tdy_sel_pl pl_rt stk_cd tdy_trde_cmsn tdy_trde_tax wthd_alowa loan_dt crd_tp".split()),
    },
    "kt00007": {
        "request_fields": frozenset("ord_dt qry_tp stk_bond_tp sell_tp stk_cd fr_ord_no dmst_stex_tp".split()),
        "response_fields": frozenset(("return_code", "acnt_ord_cntr_prps_dtl")),
        "row_field": "acnt_ord_cntr_prps_dtl",
        "row_fields": frozenset("ord_no stk_cd trde_tp crd_tp ord_qty ord_uv cnfm_qty acpt_tp rsrv_tp ord_tm ori_ord stk_nm io_tp_nm loan_dt cntr_qty cntr_uv ord_remnq comm_ord_tp mdfy_cncl cnfm_tm dmst_stex_tp cond_uv".split()),
    },
    "kt00008": {
        "request_fields": frozenset(("strt_dcd_seq",)),
        "response_fields": frozenset("return_code trde_dt setl_dt sell_amt_sum buy_amt_sum acnt_nxdy_setl_frcs_prps_array".split()),
        "row_field": "acnt_nxdy_setl_frcs_prps_array",
        "row_fields": frozenset("seq stk_cd loan_dt qty engg_amt cmsn incm_tax rstx stk_nm sell_tp unp exct_amt trde_tax resi_tax crd_tp".split()),
    },
    "ka10074": {
        "request_fields": frozenset(("strt_dt", "end_dt")),
        "response_fields": frozenset("return_code tot_buy_amt tot_sell_amt rlzt_pl trde_cmsn trde_tax dt_rlzt_pl".split()),
        "row_field": "dt_rlzt_pl",
        "row_fields": frozenset("dt buy_amt sell_amt tdy_sel_pl tdy_trde_cmsn tdy_trde_tax".split()),
    },
}

_TEXT_FIELDS = {"stk_nm": 40, "trde_tp": 20, "io_tp_nm": 20, "rsrv_oppo": 4,
                "acpt_tp": 20, "setl_tp": 8, "crd_deal_tp": 20, "comm_ord_tp": 20,
                "mdfy_cncl_tp": 12, "crd_tp": 20, "rsrv_tp": 20, "mdfy_cncl": 20,
                "tdy_htssel_cmsn": 20, "tsk_tp": 20, "ord_stt": 20,
                "stex_tp_txt": 20}  # Official textual broker fields; never treat them as money.
_DATE_FIELDS = frozenset("ord_dt strt_dt end_dt loan_dt trde_dt setl_dt dt".split())
_ORDER_FIELDS = frozenset("ord_no cntr_no orig_ord_no ori_ord fr_ord_no strt_dcd_seq seq".split())


class ExecutionsError(BrokerAPIError):
    pass


class BrokerObservationError(ExecutionsError):
    """Durable observation failed; callers must not turn this into no data."""
    observation_failed = True


@dataclass(frozen=True)
class Execution:
    day: str
    order_number: str
    execution_number: str
    symbol: str
    name: str
    side: str
    time: str
    price: Decimal
    quantity: int


@dataclass(frozen=True)
class RealizedProfit:
    symbol: str
    quantity: int
    price: Decimal
    buy_price: Decimal
    commission: Decimal
    tax: Decimal
    reported_net: Decimal | None = None
    reported_rate: Decimal | None = None
    reported_net_secondary: Decimal | None = None


@dataclass(frozen=True)
class DailyRealizedProfit:
    day: str
    buy_amount: Decimal | None
    sell_amount: Decimal | None
    reported_net: Decimal | None
    commission: Decimal | None
    tax: Decimal | None


@dataclass(frozen=True)
class DailyRealizedProfitReport:
    """Reported account aggregates, never attributed strategy profit or fills.

    totals_consistent only compares repeated top totals across response pages.
    It is not proof that net arithmetic, daily rows or ka10072 agree.
    """
    day: str
    page_totals: tuple[DailyRealizedProfit, ...]
    rows: tuple[DailyRealizedProfit, ...]
    totals_consistent: bool


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _day(value: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9]{8}", value) is None:
        raise ValueError("조회일은 YYYYMMDD 형식이어야 합니다.")
    datetime.strptime(value, "%Y%m%d")
    return value


def order_key(value: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9]{1,7}", value) is None or int(value) <= 0:
        raise ExecutionsError("체결 조회의 주문·체결 번호를 확인하지 못했습니다.")
    return str(int(value))


def _number(value, *, positive=False) -> Decimal:
    if not isinstance(value, str) or re.fullmatch(r"[+]?[0-9]{1,20}(?:\.[0-9]{1,12})?", value.strip()) is None:
        raise ExecutionsError("체결·손익 조회의 숫자 형식을 확인하지 못했습니다.")
    try:
        result = Decimal(value.strip())
    except InvalidOperation:
        raise ExecutionsError("체결·손익 조회의 숫자 형식을 확인하지 못했습니다.") from None
    if result < 0 or (positive and result <= 0):
        raise ExecutionsError("체결·손익 조회의 숫자 범위를 확인하지 못했습니다.")
    return result


def _quantity(value) -> int:
    number = _number(value, positive=True)
    if number != number.to_integral_value() or number > 999999999999:
        raise ExecutionsError("체결수량을 확인하지 못했습니다.")
    return int(number)


def _symbol(value) -> str:
    if not isinstance(value, str) or re.fullmatch(r"A?[0-9]{6}", value) is None:
        raise ExecutionsError("체결 조회의 주식 종목코드를 확인하지 못했습니다.")
    return value[-6:]


def _observation_value(key, value, token):
    """Return a safe documented scalar, preserving its original string form."""
    if key == "return_code":
        if type(value) is int and -999999 <= value <= 999999:
            return value
        if isinstance(value, str) and re.fullmatch(r"-?[0-9]{1,6}", value):
            return value
        return None
    if not isinstance(value, str) or len(value) > 64 or (token and token in value):
        return None
    if value == "":
        return value
    if key in _TEXT_FIELDS:
        return value if (len(value) <= _TEXT_FIELDS[key] and value.isprintable()
                         and not re.search(r"bearer\s|authorization|secretkey|appkey", value, re.I)) else None
    if key in _DATE_FIELDS:
        return value if re.fullmatch(r"[0-9]{8}", value) else None
    if key in _ORDER_FIELDS:
        return value if re.fullmatch(r"[0-9]{1,7}", value) else None
    if key in ("stk_cd", "stk_cd_1"):
        return value if re.fullmatch(r"[AJQ]?[0-9A-Z]{6}", value) else None
    if key in ("cntr_tm", "ord_tm", "cnfm_tm"):
        return value if re.fullmatch(r"[0-9]{2}:[0-9]{2}:[0-9]{2}", value) else None
    if key == "tm":
        return value if re.fullmatch(r"(?:[01][0-9]|2[0-3])[0-5][0-9][0-5][0-9]", value) else None
    if key == "sor_yn":
        return value if value in ("Y", "N") else None
    if key == "dmst_stex_tp":
        return value if value in ("%", "KRX", "NXT", "SOR") else None
    return value if re.fullmatch(r"[+-]?[0-9]{1,20}(?:\.[0-9]{1,12})?", value.strip()) else None


def _observed_body(payload, schema, token):
    """Allowlist body fields; never persist a provider message or credentials.

    Valid scalar values retain their original formatting. Unknown fields and
    malformed/private scalars are omitted, not coerced into zero. Row order and
    duplicates are preserved before business parsing or deduplication.
    """
    if not isinstance(payload, dict):
        return {}
    # The server may repeat identifying values in an otherwise allowed text
    # field. Exclude both the documented private fields and their contents;
    # opaque short values are not used for substring matching.
    private = set()
    potential_rows = payload.get(schema["row_field"])
    if isinstance(potential_rows, list):
        for row in potential_rows:
            if isinstance(row, dict):
                for field in ("acnt_no", "mang_empno"):
                    value = row.get(field)
                    if isinstance(value, str) and 4 <= len(value) <= 64 and value.isprintable():
                        private.add(value)
    def clean(key, value):
        safe = _observation_value(key, value, token)
        if isinstance(safe, str) and any(secret in safe for secret in private):
            return None
        return safe
    output = {}
    for key in schema["response_fields"]:
        if key not in payload:
            continue
        value = payload[key]
        if key == schema["row_field"]:
            if isinstance(value, list):
                output[key] = [{name: safe for name in schema["row_fields"] if name in row
                                and (safe := clean(name, row[name])) is not None}
                               if isinstance(row, dict) else {} for row in value]
        elif (safe := clean(key, value)) is not None:
            output[key] = safe
    return output


def _notify_observer(observer, event):
    if observer is None:
        return
    try:
        observer(event)
    except Exception:
        raise BrokerObservationError("증권사 응답 관측을 저장하지 못해 조회 결과를 반환하지 않았습니다.") from None


def _request_rows(session, api_id, body, field, *, observer=None, day=None, before_send=None, priority="normal"):
    identity = session_identity(session)
    mode, token = identity
    schema = BROKER_OBSERVATION_SCHEMAS.get(api_id)
    if observer is not None:
        if not callable(observer) or schema is None or schema["row_field"] != field:
            raise ValueError("관측할 TR과 저장 함수를 확인하지 못했습니다.")
        day = _day(day if day is not None else body.get("ord_dt", body.get("strt_dt")))
        request_body = {key: safe for key in schema["request_fields"] if key in body
                        and (safe := _observation_value(key, body[key], token)) is not None}
        query_id = uuid.uuid4().hex
    opener = urllib.request.build_opener(_RejectRedirects())
    rows = []
    next_key = ""
    seen = set()
    queue_options = {} if priority == "normal" else {"priority": priority}
    def guard():
        if session_identity(session) != identity:
            raise ExecutionsError("체결 조회 중 로그인 상태가 바뀌었습니다.")
        if before_send is not None:before_send()
    for page_number in range(1, _MAX_PAGES + 1):
        guard()
        request_started_at = datetime.now(KST).isoformat()
        def observe(status, *, payload=None, continuation=None, following=None, http_status=None):
            if observer is None:
                return
            event = {"schema": "kiwoom-broker-page-v1", "source_api": api_id,
                "source_kind": "broker_observation", "mode": mode, "day": day,
                "query_id": query_id, "request_started_at": request_started_at,
                "observed_at": datetime.now(KST).isoformat(), "page": page_number,
                "request_body": dict(request_body), "response_body": _observed_body(payload, schema, token),
                "continuation": {"request_cont_yn": "Y" if next_key else "N",
                    "response_cont_yn": continuation if continuation in (None, "", "N", "Y") else None,
                    "next_key_present": isinstance(following, str) and bool(following)},
                "status": status, "http_status": http_status if type(http_status) is int and 100 <= http_status <= 599 else None}
            _notify_observer(observer, event)
        request = urllib.request.Request(_ORIGINS[mode] + "/api/dostk/acnt",
            data=json.dumps(body, ensure_ascii=True).encode(), method="POST",
            headers={"Content-Type": "application/json;charset=UTF-8", "authorization": f"Bearer {token}",
                     "api-id": api_id, "cont-yn": "Y" if next_key else "N", "next-key": next_key})
        try:
            with queued_open(opener, request, timeout=15, max_bytes=_MAX_RESPONSE_BYTES,
                             before_send=guard, **queue_options) as response:
                raw = response.read(_MAX_RESPONSE_BYTES + 1)
                http_status = getattr(response, "status", None)
                response_id = response.headers.get("api-id")
                continuation, following = response.headers.get("cont-yn"), response.headers.get("next-key")
        except (OSError, urllib.error.URLError, http.client.HTTPException, ValueError) as error:
            observe("transport_error", http_status=error.code if isinstance(error, urllib.error.HTTPError) else None)
            raise ExecutionsError("체결·손익 조회 요청을 완료하지 못했습니다.") from None
        if not isinstance(raw, bytes) or len(raw) > _MAX_RESPONSE_BYTES or response_id not in (None, "", api_id):
            observe("invalid_response", continuation=continuation, following=following, http_status=http_status)
            raise ExecutionsError("체결·손익 조회 응답을 확인하지 못했습니다.")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeError, RecursionError):
            observe("invalid_response", continuation=continuation, following=following, http_status=http_status)
            raise ExecutionsError("체결·손익 조회 응답을 해석하지 못했습니다.") from None
        if not isinstance(payload, dict):
            observe("invalid_response", continuation=continuation, following=following, http_status=http_status)
            raise ExecutionsError("체결·손익 조회 응답 형식이 잘못되었습니다.")
        code = payload.get("return_code")
        if not ((type(code) is int and code == 0) or (type(code) is str and code == "0")):
            observe("broker_error", payload={"return_code": code}, continuation=continuation,
                    following=following, http_status=http_status)
            if ((type(code) is int and -999999 <= code <= 999999)
                    or (type(code) is str and re.fullmatch(r"-?[0-9]{1,6}", code))):
                raise ExecutionsError(f"체결·손익 조회 실패 (return_code={code}).", code=code)
            raise ExecutionsError("체결·손익 조회가 성공하지 않았습니다.")
        page = payload.get(field)
        valid_page = isinstance(page, list) and all(isinstance(row, dict) for row in page)
        observe("success" if valid_page else "invalid_response", payload=payload,
                continuation=continuation, following=following, http_status=http_status)
        guard()
        if not valid_page:
            raise ExecutionsError("전체 체결·손익 목록을 확인하지 못했습니다.")
        rows.extend(page)
        if len(rows) > _MAX_ROWS:
            raise ExecutionsError("체결·손익 조회 행 수 한도를 초과했습니다.")
        if continuation in (None, "", "N"):
            return rows
        if (continuation != "Y" or not isinstance(following, str) or not 1 <= len(following) <= 50
                or not following.isascii() or not following.isprintable() or not following.strip() or following in seen):
            raise ExecutionsError("체결·손익 연속조회 키를 확인하지 못했습니다.")
        seen.add(following)
        next_key = following
    raise ExecutionsError("체결·손익 연속조회 한도를 초과했습니다. 부분 결과는 기록하지 않습니다.")


def fetch_executions(session, day: str, *, observer=None, before_send=None, priority="normal") -> tuple[Execution, ...]:
    """Return this query's reported rows, not inferred unseen partial fills."""
    day = _day(day)
    rows = _request_rows(session, "kt00009", {"ord_dt": day, "stk_bond_tp": "1", "mrkt_tp": "0",
        "sell_tp": "0", "qry_tp": "1", "stk_cd": "", "fr_ord_no": "", "dmst_stex_tp": "KRX"},
        "acnt_ord_cntr_prst_array", observer=observer, day=day, before_send=before_send, priority=priority)
    result = {}
    for row in rows:
        # Only cash-stock fills can correspond to the existing cash order API.
        if row.get("io_tp_nm") not in ("현금매수", "현금매도"):
            continue
        if row.get("stk_bond_tp") != "1" or row.get("dmst_stex_tp") != "KRX":
            raise ExecutionsError("체결 조회 상품·거래소 범위를 확인하지 못했습니다.")
        quantity = _number(row.get("cntr_qty"))
        if quantity == 0:
            continue
        name, moment = row.get("stk_nm"), row.get("cntr_tm")
        if (not isinstance(name, str) or not name.strip() or len(name) > 40 or not name.isprintable()
                or session.token in name):
            raise ExecutionsError("체결 조회 종목명을 확인하지 못했습니다.")
        if not isinstance(moment, str) or re.fullmatch(r"[0-2][0-9]:[0-5][0-9]:[0-5][0-9]", moment) is None:
            raise ExecutionsError("실제 체결시간을 확인하지 못했습니다.")
        try:
            datetime.strptime(moment, "%H:%M:%S")
        except ValueError:
            raise ExecutionsError("실제 체결시간을 확인하지 못했습니다.") from None
        item = Execution(day, order_key(row.get("ord_no")), order_key(row.get("cntr_no")),
            _symbol(row.get("stk_cd")), name.strip(), "buy" if row["io_tp_nm"] == "현금매수" else "sell",
            moment, _number(row.get("cntr_uv"), positive=True), _quantity(row.get("cntr_qty")))
        key = item.order_number, item.execution_number
        if key in result and result[key] != item:
            raise ExecutionsError("같은 체결번호에 서로 다른 체결 정보가 있습니다.")
        result[key] = item
    return tuple(sorted(result.values(), key=lambda item: (item.time, int(item.order_number), int(item.execution_number))))


def _optional_signed(value) -> Decimal | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str) or re.fullmatch(r"[+-]?[0-9]{1,20}(?:\.[0-9]{1,12})?", value.strip()) is None:
        raise ExecutionsError("증권사가 보고한 손익·수익률 형식을 확인하지 못했습니다.")
    return Decimal(value.strip())


def fetch_realized_profit(session, symbol: str, day: str, *, observer=None,
                          before_send=None, priority="normal") -> tuple[RealizedProfit, ...]:
    symbol, day = _symbol(symbol), _day(day)
    rows = _request_rows(session, "ka10072", {"stk_cd": symbol, "strt_dt": day}, "dt_stk_div_rlzt_pl",
                         observer=observer, day=day, before_send=before_send, priority=priority)
    return _profit_rows(rows, symbol)


def fetch_period_realized_profit(session, symbol: str, day: str, *, observer=None,
                                 before_send=None, priority="normal") -> tuple[RealizedProfit, ...]:
    """Read same-day ka10073 independently; never overwrite ka10072 values."""
    symbol, day = _symbol(symbol), _day(day)
    rows = _request_rows(session, "ka10073", {"stk_cd": symbol, "strt_dt": day, "end_dt": day}, "dt_stk_rlzt_pl",
                         observer=observer, day=day, before_send=before_send, priority=priority)
    if any(_day(row.get("dt")) != day for row in rows):
        raise ExecutionsError("기간 실현손익 응답 일자가 요청과 다릅니다.")
    return _profit_rows(rows, symbol)


def _profit_rows(rows, symbol):
    result = []
    for row in rows:
        if row.get("crd_tp") != "현금잔고":
            continue
        if _symbol(row.get("stk_cd")) != symbol:
            raise ExecutionsError("실현손익 응답 종목이 요청과 다릅니다.")
        # These are provider profit/cost rows, not individual execution IDs.
        # Preserve reported values separately: rounded average prices and
        # cumulative/scope differences must never be silently fitted to a net.
        result.append(RealizedProfit(symbol, _quantity(row.get("cntr_qty")),
            _number(row.get("cntr_pric"), positive=True), _number(row.get("buy_uv"), positive=True),
            _number(row.get("tdy_trde_cmsn")), _number(row.get("tdy_trde_tax")),
            _optional_signed(row.get("tdy_sel_pl")), _optional_signed(row.get("pl_rt")),
            _optional_signed(row.get("tdy_sel_pl_1"))))
    return tuple(result)


def fetch_daily_realized_profit(session, day: str, *, observer=None,
                                before_send=None, priority="normal") -> DailyRealizedProfitReport:
    """Read ka10074 for one date; retain each page's totals without summing.

    ka10074 fills rows only for days with realized profit/loss. Missing figures
    remain None. Reported net and arithmetic net can disagree; this function
    neither adjusts the provider's values nor assigns them to strategy orders.
    """
    day = _day(day)
    observed_totals = []
    def capture(event):
        if event["status"] == "success":
            observed_totals.append(dict(event["response_body"]))
        _notify_observer(observer, event)
    rows = _request_rows(session, "ka10074", {"strt_dt": day, "end_dt": day}, "dt_rlzt_pl",
                         observer=capture, day=day, before_send=before_send, priority=priority)
    def amount(value):
        return None if value in (None, "") else _number(value)
    page_totals = tuple(DailyRealizedProfit(day, amount(row.get("tot_buy_amt")), amount(row.get("tot_sell_amt")),
        _optional_signed(row.get("rlzt_pl")), amount(row.get("trde_cmsn")), amount(row.get("trde_tax")))
        for row in observed_totals)
    daily = []
    for row in rows:
        if _day(row.get("dt")) != day:
            raise ExecutionsError("계좌 실현손익 응답 일자가 요청과 다릅니다.")
        daily.append(DailyRealizedProfit(day, amount(row.get("buy_amt")), amount(row.get("sell_amt")),
            _optional_signed(row.get("tdy_sel_pl")), amount(row.get("tdy_trde_cmsn")), amount(row.get("tdy_trde_tax"))))
    return DailyRealizedProfitReport(day, page_totals, tuple(daily),
        bool(page_totals) and all(item == page_totals[0] for item in page_totals))
