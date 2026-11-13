"""Read-only mock turnover snapshots restricted to KOSPI/KOSDAQ equities.

The caller owns authentication. No credentials are loaded or persisted here.
Instrument classification uses the verified ka10099 stock-list endpoint while
sharing the client's redirect-blocking opener, lock and request-rate clock.
Only actual market codes 0/10 qualify; separate ETF/ETN lists are cross-checked.
Preferred shares remain included. Ranking order is exactly the broker's order.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal, InvalidOperation, localcontext
from email.message import Message
from typing import Any
from zoneinfo import ZoneInfo

from kiwoom_autotrader.api.client import KiwoomAPIError, KiwoomMockClient
from . import ka10099


_CLASSIFICATION_URL = "https://mockapi.kiwoom.com" + ka10099.ENDPOINT
_MARKETS = ("0", "10", "8", "60", "70", "90")
_EQUITY_MARKETS = {"0": "코스피", "10": "코스닥"}
_MAX_CLASSIFICATION_PAGES = 20
_MAX_RESPONSE_BYTES = 4 * 1024 * 1024
_REQUEST_INTERVAL = 1.05
_TIMEOUT_SECONDS = 15
_CLASSIFICATION_ERROR = "키움 종목 분류를 안전하게 확인하지 못했습니다."
_RANKING_ERROR = "키움 일반기업 주식 순위 응답 형식이 올바르지 않습니다."


def _symbol(value: object) -> str:
    if not isinstance(value, str):
        raise KiwoomAPIError(_CLASSIFICATION_ERROR)
    match = re.fullmatch(r"([0-9A-Z]{6})(?:_(?:AL|NX))?", value)
    if match is None:
        raise KiwoomAPIError(_CLASSIFICATION_ERROR)
    return match.group(1)


def _market_code(value: object) -> str:
    if type(value) is int and 0 <= value <= 999:
        return str(value)
    if isinstance(value, str) and re.fullmatch(r"0|[1-9][0-9]{0,2}", value):
        return value
    raise KiwoomAPIError(_CLASSIFICATION_ERROR)


def _classification_page(
    client: KiwoomMockClient, market: str, next_key: str | None,
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """Make only the fixed stock-list request without extending the client allowlist."""
    if market not in _MARKETS:
        raise KiwoomAPIError(_CLASSIFICATION_ERROR)
    headers = {
        "Content-Type": "application/json;charset=UTF-8",
        "authorization": f"Bearer {client._token}",
        "api-id": ka10099.API_ID,
    }
    if next_key is not None:
        headers.update({"cont-yn": "Y", "next-key": next_key})
    with client._lock:
        if client._token is None:
            raise KiwoomAPIError("먼저 모의투자 authenticate()를 실행하세요.")
        try:
            request = urllib.request.Request(
                _CLASSIFICATION_URL, data=json.dumps(ka10099.request_body(market)).encode("utf-8"),
                headers=headers, method="POST",
            )
            now = time.monotonic()
            if client._last_request_at is not None:
                delay = _REQUEST_INTERVAL - (now - client._last_request_at)
                if delay > 0:
                    time.sleep(delay)
            client._last_request_at = time.monotonic()
            with client._opener.open(request, timeout=_TIMEOUT_SECONDS) as response:
                raw = response.read(_MAX_RESPONSE_BYTES + 1)
                response_headers = getattr(response, "headers", None)
                if not isinstance(response_headers, (Mapping, Message)):
                    raise KiwoomAPIError(_CLASSIFICATION_ERROR)
                continuation = client._continuation_headers(response_headers)
        except urllib.error.HTTPError as error:
            error.close()
            raise KiwoomAPIError(_CLASSIFICATION_ERROR) from None
        except (urllib.error.URLError, OSError, ValueError, TypeError):
            raise KiwoomAPIError(_CLASSIFICATION_ERROR) from None
    if not isinstance(raw, bytes) or len(raw) > _MAX_RESPONSE_BYTES:
        raise KiwoomAPIError(_CLASSIFICATION_ERROR)
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeError, RecursionError):
        raise KiwoomAPIError(_CLASSIFICATION_ERROR) from None
    if not isinstance(payload, dict):
        raise KiwoomAPIError(_CLASSIFICATION_ERROR)
    status = payload.get("return_code")
    if not ((type(status) is int and status == 0) or (type(status) is str and status == "0")):
        raise KiwoomAPIError(_CLASSIFICATION_ERROR)
    records = payload.get("list")
    if not isinstance(records, list) or not all(isinstance(row, dict) for row in records):
        raise KiwoomAPIError(_CLASSIFICATION_ERROR)
    return records, continuation


def _instrument_list(client: KiwoomMockClient, market: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    next_key = None
    seen_keys: set[str] = set()
    for _ in range(_MAX_CLASSIFICATION_PAGES):
        page, continuation = _classification_page(client, market, next_key)
        records.extend(page)
        if continuation.get("cont-yn") != "Y":
            return records
        returned_key = continuation.get("next-key")
        if not page or not returned_key or not returned_key.strip() or returned_key in seen_keys:
            raise KiwoomAPIError(_CLASSIFICATION_ERROR)
        seen_keys.add(returned_key)
        next_key = returned_key
    raise KiwoomAPIError("키움 종목 분류 연속조회를 완료하지 못했습니다.")


def _turnover(value: object) -> str:
    # ka10032 trde_prica is in KRW millions; divide by 100 for KRW 100 millions.
    if (
        not isinstance(value, str)
        or len(value) > 64
        or re.fullmatch(r"\+?[0-9]+(?:\.[0-9]+)?", value) is None
    ):
        raise KiwoomAPIError(_RANKING_ERROR)
    try:
        amount = Decimal(value)
        if not amount.is_finite() or amount < 0:
            raise InvalidOperation
        with localcontext() as context:
            context.prec = max(28, len(value) + 2)
            return format(amount / Decimal(100), "f")
    except InvalidOperation:
        raise KiwoomAPIError(_RANKING_ERROR) from None


def get_top_equities(client: KiwoomMockClient, limit: int = 20) -> dict[str, Any]:
    """Return a complete, freshly queried mock snapshot or a sanitized error.

    Fetches the first 100 all-market, managed-stock-inclusive combined-exchange
    ranking rows. A snapshot is returned only when at least ``limit`` distinct
    eligible equities are present. No ranking rows are reordered by this code.
    """
    if not isinstance(client, KiwoomMockClient):
        raise ValueError("모의투자 조회 클라이언트가 필요합니다.")
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("조회 종목 수는 1부터 100 사이의 정수여야 합니다.")
    with client._lock:
        if client._token is None:
            raise KiwoomAPIError("먼저 모의투자 authenticate()를 실행하세요.")
        eligible: dict[str, str] = {}
        excluded: set[str] = set()
        counts: dict[str, int] = {}
        classifications: dict[str, str] = {}
        for requested_market in _MARKETS:
            records = _instrument_list(client, requested_market)
            counts[requested_market] = len(records)
            matching_equities = 0
            for record in records:
                symbol = _symbol(record.get("code"))
                actual_market = _market_code(record.get("marketCode"))
                previous = classifications.get(symbol)
                if previous is not None and previous != actual_market:
                    raise KiwoomAPIError(_CLASSIFICATION_ERROR)
                classifications[symbol] = actual_market
                if requested_market in _EQUITY_MARKETS:
                    if actual_market != requested_market:
                        continue
                    matching_equities += 1
                    eligible[symbol] = actual_market
                else:
                    excluded.add(symbol)
            if requested_market in _EQUITY_MARKETS and matching_equities == 0:
                raise KiwoomAPIError(_CLASSIFICATION_ERROR)

        rows = client.top_turnover(
            market="000", include_managed=True, limit=100, exchange="3",
        )
        if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
            raise KiwoomAPIError(_RANKING_ERROR)
        selected: list[dict[str, Any]] = []
        used: set[str] = set()
        for row in rows:
            symbol = _symbol(row.get("stk_cd"))
            if symbol not in eligible or symbol in excluded or symbol in used:
                continue
            name = row.get("stk_nm")
            if (
                not isinstance(name, str) or not name.strip() or len(name) > 100
                or any(ord(character) < 32 or ord(character) == 127 for character in name)
            ):
                raise KiwoomAPIError(_RANKING_ERROR)
            selected.append({
                "rank": len(selected) + 1,
                "symbol": symbol,
                "name": name,
                "market": _EQUITY_MARKETS[eligible[symbol]],
                "turnover_100million_krw": _turnover(row.get("trde_prica")),
            })
            used.add(symbol)
            if len(selected) == limit:
                break
        if len(selected) < limit:
            raise KiwoomAPIError("일반기업 주식 순위를 요청한 종목 수만큼 확보하지 못했습니다.")
    return {
        "environment": "mock",
        "queried_at_kst": datetime.now(ZoneInfo("Asia/Seoul")).isoformat(),
        "markets": ["코스피", "코스닥"],
        "exchange": "통합",
        "include_managed": True,
        "eligible_market_codes": ["0", "10"],
        "preferred_shares": "included",
        "classification_counts": counts,
        "ranking_rows_scanned": len(rows),
        "rows": selected,
    }
