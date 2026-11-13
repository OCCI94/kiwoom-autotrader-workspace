"""Resolve an exact KOSPI/KOSDAQ name using authenticated ka10099 lists.

Reference: supplied 키움 REST API 문서.xlsx, ka10099 rows 8–42. Only
six-digit codes are supported here. Catalogs are kept for six hours in memory,
separately for each BrokerSession and mode; no credentials or tokens are cached.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from email.message import Message
import http.client
import json
import re
import threading
import time
import unicodedata
import urllib.error
import urllib.request
import weakref

from . import ka10099
from .session import BrokerSession, IssuedToken, LoginSessionError


_ORIGINS = {"paper": "https://mockapi.kiwoom.com", "real": "https://api.kiwoom.com"}
_MARKETS = ("0", "10")
_CACHE_TTL_SECONDS = 6 * 60 * 60
_REQUEST_INTERVAL = 1.05
_MAX_PAGES = 20
_MAX_ROWS = 50000
_MAX_RESPONSE_BYTES = 4 * 1024 * 1024
_BAD_RESPONSE = "키움 종목명 목록 응답을 확인하지 못했습니다. 잠시 후 다시 시도하세요."


class StockNameError(RuntimeError):
    """Safe user-facing error; candidates contain validated (name, code) pairs."""

    def __init__(self, message: str, *, candidates: tuple[tuple[str, str], ...] = ()):
        super().__init__(message)
        self.candidates = candidates


@dataclass(frozen=True)
class _Stock:
    code: str
    name: str
    normalized: str


@dataclass(frozen=True)
class _MarketCache:
    stocks: tuple[_Stock, ...]
    expires_at: float


@dataclass
class _SessionCache:
    markets: dict[tuple[str, str], _MarketCache] = field(default_factory=dict)
    last_request_at: float | None = None
    lock: threading.RLock = field(default_factory=threading.RLock, repr=False)


_CACHES: weakref.WeakKeyDictionary[BrokerSession, _SessionCache] = weakref.WeakKeyDictionary()
_CACHES_LOCK = threading.Lock()


def normalize_stock_name(value: str) -> str:
    """Normalize compatibility letters and case, ignoring all name whitespace."""
    if (not isinstance(value, str) or not 1 <= len(value) <= 200
            or any(not ch.isprintable() and not ch.isspace() for ch in value)):
        raise StockNameError("정확한 종목명이나 6자리 종목코드를 입력하세요.")
    normalized = "".join(unicodedata.normalize("NFKC", value).casefold().split())
    if not normalized or len(normalized) > 120:
        raise StockNameError("정확한 종목명이나 6자리 종목코드를 입력하세요.")
    return normalized


def _session_values(session: BrokerSession) -> tuple[str, str]:
    if not isinstance(session, BrokerSession) or not session.authenticated:
        raise StockNameError("먼저 login paper 또는 login real로 로그인하세요.")
    mode, token = session.mode, session.token
    if not isinstance(mode, str) or mode not in _ORIGINS:
        raise StockNameError("로그인 투자 구분을 확인하지 못했습니다. 다시 로그인하세요.")
    try:
        IssuedToken(token)
        usable = session.token_usable
    except (LoginSessionError, ValueError, TypeError):
        raise StockNameError("유효한 로그인 토큰이 없습니다. 다시 로그인하세요.") from None
    if not usable:
        raise StockNameError("로그인 토큰이 만료되었습니다. login paper 또는 login real로 다시 로그인하세요.")
    return mode, token


def _same_session(session: BrokerSession, mode: str, token: str):
    if _session_values(session) != (mode, token):
        raise StockNameError("조회 중 로그인 상태가 변경되었습니다. 명령을 다시 입력하세요.")


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _continuation(headers) -> str | None:
    if not isinstance(headers, (Mapping, Message)):
        raise StockNameError(_BAD_RESPONSE)
    selected = {}
    for name, value in headers.items():
        key = name.casefold() if isinstance(name, str) else ""
        if key not in {"cont-yn", "next-key", "api-id"}:
            continue
        if key in selected or not isinstance(value, str):
            raise StockNameError(_BAD_RESPONSE)
        selected[key] = value
    if selected.get("api-id", ka10099.API_ID) != ka10099.API_ID:
        raise StockNameError(_BAD_RESPONSE)
    continuation = selected.get("cont-yn", "N")
    if continuation not in {"", "N", "Y"}:
        raise StockNameError(_BAD_RESPONSE)
    next_key = selected.get("next-key", "")
    if (len(next_key) > 50 or not next_key.isascii()
            or any(not 33 <= ord(ch) <= 126 for ch in next_key)):
        raise StockNameError(_BAD_RESPONSE)
    if continuation == "Y":
        if not next_key:
            raise StockNameError(_BAD_RESPONSE)
        return next_key
    return None


def _wait_for_interval(state: _SessionCache):
    if state.last_request_at is not None:
        delay = _REQUEST_INTERVAL - (time.monotonic() - state.last_request_at)
        if delay > 0:
            time.sleep(delay)


def _page(session: BrokerSession, mode: str, token: str, market: str,
          next_key: str | None, state: _SessionCache) -> tuple[list, str | None]:
    headers = {"Content-Type": "application/json;charset=UTF-8",
               "authorization": f"Bearer {token}", "api-id": ka10099.API_ID}
    if next_key is not None:
        headers.update({"cont-yn": "Y", "next-key": next_key})
    request = urllib.request.Request(
        _ORIGINS[mode] + ka10099.ENDPOINT,
        data=json.dumps(ka10099.request_body(market)).encode("utf-8"),
        headers=headers, method="POST",
    )
    _wait_for_interval(state)
    _same_session(session, mode, token)
    opener = urllib.request.build_opener(_RejectRedirects())
    state.last_request_at = time.monotonic()
    try:
        with opener.open(request, timeout=15) as response:
            raw = response.read(_MAX_RESPONSE_BYTES + 1)
            following = _continuation(response.headers)
    except urllib.error.HTTPError as error:
        code = error.code
        try:
            error.close()
        except Exception:
            pass
        if type(code) is int and 100 <= code <= 599:
            raise StockNameError(f"키움 종목명 목록 HTTP 오류 (code={code}).") from None
        raise StockNameError("키움 종목명 목록 요청에 실패했습니다.") from None
    except (OSError, urllib.error.URLError, http.client.HTTPException, ValueError, TypeError):
        raise StockNameError("키움 종목명 목록 서버에 연결하지 못했습니다.") from None
    if not isinstance(raw, bytes) or len(raw) > _MAX_RESPONSE_BYTES:
        raise StockNameError(_BAD_RESPONSE)
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeError, RecursionError):
        raise StockNameError(_BAD_RESPONSE) from None
    if not isinstance(payload, dict):
        raise StockNameError(_BAD_RESPONSE)
    code = payload.get("return_code")
    if not ((type(code) is int and code == 0) or (type(code) is str and code == "0")):
        if ((type(code) is int and -999999 <= code <= 999999)
                or (type(code) is str and re.fullmatch(r"-?[0-9]{1,6}", code))):
            raise StockNameError(f"키움 종목명 목록 조회 실패 (return_code={code}).")
        raise StockNameError(_BAD_RESPONSE)
    rows = payload.get("list")
    if not isinstance(rows, list) or len(rows) > _MAX_ROWS:
        raise StockNameError(_BAD_RESPONSE)
    return rows, following


def _stock(row, market: str, token: str) -> _Stock | None:
    if not isinstance(row, dict):
        raise StockNameError(_BAD_RESPONSE)
    actual_market = row.get("marketCode", market)
    if type(actual_market) is int and 0 <= actual_market <= 999:
        actual_market = str(actual_market)
    if not isinstance(actual_market, str) or re.fullmatch(r"0|[1-9][0-9]{0,2}", actual_market) is None:
        raise StockNameError(_BAD_RESPONSE)
    if actual_market != market:
        return None
    code, name = row.get("code"), row.get("name")
    if not isinstance(code, str) or re.fullmatch(r"[0-9A-Z]{6}", code) is None:
        raise StockNameError(_BAD_RESPONSE)
    if not code.isdigit():
        return None  # The existing domestic search contract accepts digits only.
    if (not isinstance(name, str) or not name.strip() or len(name) > 40
            or not name.isprintable() or token in name):
        raise StockNameError(_BAD_RESPONSE)
    return _Stock(code, name.strip(), normalize_stock_name(name))


def _market_stocks(session: BrokerSession, mode: str, token: str, market: str,
                   state: _SessionCache) -> tuple[_Stock, ...]:
    cached = state.markets.get((mode, market))
    if cached is not None and time.monotonic() < cached.expires_at:
        return cached.stocks
    stocks: dict[str, _Stock] = {}
    next_key = None
    seen_keys: set[str] = set()
    rows_seen = 0
    for _ in range(_MAX_PAGES):
        rows, following = _page(session, mode, token, market, next_key, state)
        rows_seen += len(rows)
        if rows_seen > _MAX_ROWS:
            raise StockNameError(_BAD_RESPONSE)
        for row in rows:
            stock = _stock(row, market, token)
            if stock is None:
                continue
            existing = stocks.get(stock.code)
            if existing is not None and existing.normalized != stock.normalized:
                raise StockNameError("키움 종목명 목록에 상충하는 정보가 있습니다. 종목코드로 조회하세요.")
            stocks[stock.code] = stock
        if following is None:
            _same_session(session, mode, token)
            result = tuple(stocks.values())
            state.markets[(mode, market)] = _MarketCache(result, time.monotonic() + _CACHE_TTL_SECONDS)
            return result
        if not rows or following in seen_keys:
            raise StockNameError("키움 종목명 목록 연속조회를 완료하지 못했습니다.")
        seen_keys.add(following)
        next_key = following
    raise StockNameError("키움 종목명 목록 연속조회가 최대 페이지 수를 초과했습니다.")


def _selection_error(stocks: list[_Stock], *, exact: bool) -> StockNameError:
    choices = sorted(stocks, key=lambda item: (item.normalized, item.code))[:8]
    candidates = tuple((item.name, item.code) for item in choices)
    heading = ("같은 이름의 종목이 여러 개입니다. 종목코드로 지정하세요." if exact else
               "정확히 일치하는 종목명이 없습니다. 아래 후보의 전체 이름이나 코드를 입력하세요.")
    lines = [heading] + [f"{name} ({code})" for name, code in candidates]
    if len(stocks) > len(choices):
        lines.append(f"후보 {len(stocks)}개 중 {len(choices)}개 표시")
    return StockNameError("\n".join(lines), candidates=candidates)


def resolve_stock_name(session: BrokerSession, name: str) -> str:
    """Return only one exact normalized name match; never choose a fuzzy match."""
    normalized = normalize_stock_name(name)
    mode, token = _session_values(session)
    with _CACHES_LOCK:
        state = _CACHES.setdefault(session, _SessionCache())
    with state.lock:
        _same_session(session, mode, token)
        by_code: dict[str, _Stock] = {}
        for market in _MARKETS:
            for stock in _market_stocks(session, mode, token, market, state):
                existing = by_code.get(stock.code)
                if existing is not None and existing.normalized != stock.normalized:
                    raise StockNameError("키움 종목명 목록에 상충하는 정보가 있습니다. 종목코드로 조회하세요.")
                by_code[stock.code] = stock
        exact = [stock for stock in by_code.values() if stock.normalized == normalized]
        if len(exact) == 1:
            # The caller commonly sends ka10001 immediately after resolution.
            # Leave the mock server's request interval before that next call.
            _wait_for_interval(state)
            _same_session(session, mode, token)
            return exact[0].code
        if exact:
            raise _selection_error(exact, exact=True)
        partial = [stock for stock in by_code.values() if normalized in stock.normalized]
        if partial:
            raise _selection_error(partial, exact=False)
        raise StockNameError("코스피·코스닥에서 일치하는 종목명을 찾지 못했습니다. 정확한 종목명이나 6자리 코드를 입력하세요.")
