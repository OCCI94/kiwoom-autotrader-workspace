"""Read KRX one-minute prices using ka10080; see /tr_docs/ka10080.md.

cntr_tm is the candle START time. cur_prc is the signed close; its sign denotes
movement, so the absolute integer price is used. No credentials are loaded.
"""
from __future__ import annotations
from .errors import BrokerAPIError

from dataclasses import dataclass, field
from datetime import datetime
import http.client
import hashlib
import json
import re
import threading
import time
import urllib.error
import urllib.request
import uuid

from .orders import normalize_order_code
from .session import BrokerSession, IssuedToken, KST, LoginSessionError
from kiwoom_autotrader.utils.http_queue import queued_open


_ORIGINS = {"paper": "https://mockapi.kiwoom.com", "real": "https://api.kiwoom.com"}
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_MAX_PAGES = 3


class _MinuteChartPacer:
    """Space process-local ka10080 dispatch grants, including continuation pages.

    Both monitors share this instance. Run at the HTTP queue's send boundary,
    after queue congestion, so separate callers cannot reserve overlapping gaps.
    A small margin over one second covers the paper server's same-TR limit.
    """

    def __init__(self, *, clock=time.monotonic, wait=time.sleep):
        self._clock = clock
        self._wait = wait
        self._lock = threading.Lock()
        self._last = None

    def acquire(self, *, before_send):
        with self._lock:
            before_send()
            while self._last is not None:
                delay = self._last + 1.05 - self._clock()
                if delay <= 0:
                    break
                self._wait(delay)
                before_send()
            # Session expiry or a login switch during the wait must block send.
            before_send()
            self._last = self._clock()


_REQUEST_PACER = _MinuteChartPacer()


class MinuteChartError(BrokerAPIError):
    """A local message, without provider text or authentication data."""


@dataclass(frozen=True)
class GapProof:
    """Evidence for a carry-forward assumption, never a returned broker bar."""
    symbol: str
    mode: str
    previous_start: datetime
    previous_close: int
    previous_volume: int
    previous_acc_volume: int
    next_start: datetime
    next_close: int
    next_volume: int
    next_acc_volume: int
    # New requests have a different whole-page hash even when the anchor rows
    # are identical; keep it as provenance without breaking overlap equality.
    response_sha256: str = field(compare=False)
    query_started_at: datetime = field(compare=False)


@dataclass(frozen=True)
class MinuteBar:
    start: datetime
    close: int
    # Optional response details are preserved for a bounded no-trade inference.
    # A missing or malformed optional value blocks inference, never becomes zero.
    volume: int | None = None
    acc_volume: int | None = None
    adjustment: str | None = None
    adjustment_rate: str | None = None
    source_symbol: str | None = None
    source_mode: str | None = None
    # Transport identity is used only to ensure both anchors came from one
    # complete response page. It must not invalidate overlap on the next query.
    source_batch: str | None = field(default=None, compare=False, repr=False)
    source_page: int | None = field(default=None, compare=False, repr=False)
    source_row: int | None = field(default=None, compare=False, repr=False)
    source_response_sha256: str | None = field(default=None, compare=False, repr=False)
    source_query_started_at: datetime | None = field(default=None, compare=False, repr=False)
    gap_proof: GapProof | None = None
    # Optional provider OHLC is independent of the close and never inferred.
    # Excluding these additions from equality preserves existing baseline
    # comparisons; strategies that need highs validate them separately.
    open_price: int | None = field(default=None, compare=False)
    high_price: int | None = field(default=None, compare=False)
    low_price: int | None = field(default=None, compare=False)


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def session_identity(session: BrokerSession) -> tuple[str, str]:
    if not isinstance(session, BrokerSession) or not session.authenticated:
        raise MinuteChartError("ddcrs: 현재 로그인 세션이 없습니다.")
    mode, token = session.mode, session.token
    if mode not in _ORIGINS:
        raise MinuteChartError("ddcrs: 투자 구분을 확인하지 못했습니다.")
    try:
        IssuedToken(token)
        usable = session.token_usable
    except (LoginSessionError, ValueError, TypeError):
        raise MinuteChartError("ddcrs: 로그인 토큰을 확인하지 못했습니다.") from None
    if not usable:
        raise MinuteChartError("ddcrs: 로그인 토큰이 만료되었습니다.")
    return mode, token


def _bars(payload, symbol: str, *, mode=None, batch=None, page=None, response_sha256=None, query_started_at=None) -> list[MinuteBar]:
    invalid = "ddcrs: 1분봉 응답 형식을 확인하지 못했습니다."
    if not isinstance(payload, dict):
        raise MinuteChartError(invalid)
    code = payload.get("return_code")
    if not ((type(code) is int and code == 0) or (type(code) is str and code == "0")):
        raise MinuteChartError("ddcrs: 1분봉 조회가 성공하지 않았습니다.", code=code)
    if payload.get("stk_cd") != symbol:
        raise MinuteChartError("ddcrs: 1분봉 종목코드가 요청과 다릅니다.")
    rows = payload.get("stk_min_pole_chart_qry")
    if not isinstance(rows, list) or len(rows) > 2000:
        raise MinuteChartError(invalid)
    result = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise MinuteChartError(invalid)
        moment, price = row.get("cntr_tm"), row.get("cur_prc")
        if (not isinstance(moment, str) or re.fullmatch(r"[0-9]{14}", moment) is None
                or not isinstance(price, str) or re.fullmatch(r"[+-]?[0-9]{1,19}", price) is None):
            raise MinuteChartError(invalid)
        try:
            start = datetime.strptime(moment, "%Y%m%d%H%M%S").replace(tzinfo=KST)
        except ValueError:
            raise MinuteChartError(invalid) from None
        if start.second != 0 or abs(int(price)) <= 0:
            raise MinuteChartError(invalid)
        def quantity(name):
            value = row.get(name)
            return int(value) if isinstance(value, str) and re.fullmatch(r"\+?[0-9]{1,19}", value) else None
        def optional_price(name):
            value = row.get(name)
            if not isinstance(value, str) or re.fullmatch(r"[+-]?[0-9]{1,19}", value) is None:
                return None
            amount = abs(int(value))
            return amount if amount > 0 else None
        def marker(name):
            value = row.get(name)
            if value == "":
                return ""
            # Do not persist arbitrary provider text as adjustment metadata.
            pattern = r"[0-9]{1,3}(?:,[0-9]{1,3})*" if name == "upd_stkpc_tp" else r"[+-]?[0-9]{1,15}(?:\.[0-9]{1,4})?"
            return value if isinstance(value, str) and len(value) <= 20 and re.fullmatch(pattern, value) else None
        result.append(MinuteBar(start, abs(int(price)), quantity("trde_qty"), quantity("acc_trde_qty"),
            marker("upd_stkpc_tp"), marker("upd_rt"), symbol, mode, batch, page, index, response_sha256, query_started_at,
            open_price=optional_price("open_pric"), high_price=optional_price("high_pric"), low_price=optional_price("low_pric")))
    return result


def _query_now() -> datetime:
    """Independent KST wall clock for a request's conservative start bound."""
    return datetime.now(KST)


def _fetch_minute_chart(session: BrokerSession, symbol: str, *, count: int, maximum: int) -> tuple[MinuteBar, ...]:
    """Return chronological bars, including any current unclosed bar.

The monitor determines closure/freshness using its clock. Continuations are
bounded; incomplete history yields no signal. HTTP responses are never retried.
"""
    symbol = normalize_order_code(symbol)
    if type(count) is not int or not 2 <= count <= maximum:
        raise ValueError("1분봉 조회 개수는 2~62여야 합니다.")
    identity = session_identity(session)
    mode, token = identity
    opener = urllib.request.build_opener(_RejectRedirects())
    bars: dict[datetime, MinuteBar] = {}
    batch = uuid.uuid4().hex
    next_key = ""
    keys = set()
    def check_session():
        if session_identity(session) != identity:
            raise MinuteChartError("ddcrs: 전송 대기 중 로그인 상태가 변경되었습니다.")
    def before_send():
        _REQUEST_PACER.acquire(before_send=check_session)
    for page in range(_MAX_PAGES):
        if session_identity(session) != identity:
            raise MinuteChartError("ddcrs: 조회 중 로그인 상태가 변경되었습니다.")
        request = urllib.request.Request(
            _ORIGINS[mode] + "/api/dostk/chart",
            data=json.dumps({"stk_cd": symbol, "tic_scope": "1", "upd_stkpc_tp": "1"}).encode("utf-8"),
            headers={"Content-Type": "application/json;charset=UTF-8", "authorization": f"Bearer {token}",
                     "api-id": "ka10080", "cont-yn": "Y" if next_key else "N", "next-key": next_key},
            method="POST",
        )
        query_started_at = _query_now()
        if (not isinstance(query_started_at, datetime) or query_started_at.tzinfo is None
                or query_started_at.utcoffset() is None):
            raise MinuteChartError("ddcrs: 1분봉 조회 시각을 확인하지 못했습니다.")
        query_started_at = query_started_at.astimezone(KST)
        try:
            with queued_open(opener, request, timeout=15, max_bytes=_MAX_RESPONSE_BYTES, before_send=before_send) as response:
                raw = response.read(_MAX_RESPONSE_BYTES + 1)
                api_id = response.headers.get("api-id")
                continuation = response.headers.get("cont-yn")
                following = response.headers.get("next-key")
        except (OSError, urllib.error.URLError, http.client.HTTPException, ValueError):
            raise MinuteChartError("ddcrs: 1분봉 서버 요청을 완료하지 못했습니다.") from None
        if session_identity(session) != identity:
            raise MinuteChartError("ddcrs: 조회 중 로그인 상태가 변경되었습니다.")
        response_at = _query_now()
        if (not isinstance(response_at, datetime) or response_at.tzinfo is None
                or response_at.utcoffset() is None or response_at.astimezone(KST) < query_started_at):
            raise MinuteChartError("ddcrs: 1분봉 조회 중 시계가 역행하여 판단하지 않았습니다.")
        if (not isinstance(raw, bytes) or len(raw) > _MAX_RESPONSE_BYTES
                or api_id not in (None, "", "ka10080") or continuation not in (None, "", "N", "Y")):
            raise MinuteChartError("ddcrs: 1분봉 응답을 확인하지 못했습니다.")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeError, RecursionError):
            raise MinuteChartError("ddcrs: 1분봉 응답을 해석하지 못했습니다.") from None
        for bar in _bars(payload, symbol, mode=mode, batch=batch, page=page,
                         response_sha256=hashlib.sha256(raw).hexdigest(), query_started_at=query_started_at):
            if bar.start in bars:
                raise MinuteChartError("ddcrs: 중복 1분봉이 있어 관측을 건너뜁니다.")
            bars[bar.start] = bar
        if len(bars) >= count or continuation != "Y":
            return tuple(sorted(bars.values(), key=lambda bar: bar.start)[-count:])
        if (not isinstance(following, str) or not 1 <= len(following) <= 50
                or not following.isascii() or not following.isprintable() or not following.strip() or following in keys):
            raise MinuteChartError("ddcrs: 1분봉 연속조회 키를 확인하지 못했습니다.")
        keys.add(following)
        next_key = following
    raise MinuteChartError("ddcrs: 1분봉 조회 한도 내에서 필요한 이력을 확보하지 못했습니다.")


def fetch_minute_chart(session: BrokerSession, symbol: str, *, count: int = 62) -> tuple[MinuteBar, ...]:
    """Legacy bounded window; unchanged for existing GDCRS/S2/S3/S4 readers."""
    return _fetch_minute_chart(session,symbol,count=count,maximum=62)


def fetch_session_minute_chart(session: BrokerSession, symbol: str, *, count: int = 390) -> tuple[MinuteBar, ...]:
    """Paper S5 session prefix only; same queue/pacer, at most three pages.

    A partial prefix is preserved and rejected by S5, never filled synthetically.
    """
    if session_identity(session)[0] != 'paper':
        raise MinuteChartError('S5: 모의 세션만 조회할 수 있습니다.')
    return _fetch_minute_chart(session,symbol,count=count,maximum=390)
