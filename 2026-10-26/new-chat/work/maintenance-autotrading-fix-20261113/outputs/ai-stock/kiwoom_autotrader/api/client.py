"""Read-only Kiwoom mock REST client; standard library only.

Official sources verified 2026-10-26:
https://openapi.kiwoom.com/guide/apiguide (OAuth request and mock domain)
https://openapi.kiwoom.com/intro (mock request limits)
https://github.com/Kiwoom-Securities/Kiwoom-REST-API/blob/main/examples/국내주식/종목정보/get_domestic_stock_info.py
https://github.com/Kiwoom-Securities/Kiwoom-REST-API/blob/main/examples/미국주식/시세/get_overseas_stock_quote.py
https://github.com/Kiwoom-Securities/Kiwoom-REST-API/blob/main/examples/국내주식/순위정보/get_domestic_trading_value_top.py
https://openapi.kiwoom.com/intro/mockInvestInfo (KRX mock trading restrictions)

Only authentication, quotes, and turnover ranking are implemented. Requests stay on
the mock domain, redirects are rejected, and response messages are never
included in exceptions because a remote message could contain credentials.
The request interval applies to one client instance; reuse the instance.
"""

from __future__ import annotations
from .errors import BrokerAPIError

import json
import re
import threading
import time
import urllib.error
import urllib.request
from kiwoom_autotrader.utils.http_queue import queued_open
from collections.abc import Mapping
from email.message import Message
from typing import Any

from . import au10001, ka10001, ka10032, usa20100


_MOCK_ORIGIN = "https://mockapi.kiwoom.com"
_ENDPOINTS = {
    tr.API_ID: tr.ENDPOINT for tr in (au10001, ka10001, usa20100, ka10032)
}
_REQUEST_INTERVAL = 1.05
_TIMEOUT_SECONDS = 15
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_MAX_RANKING_PAGES = 10


class KiwoomAPIError(BrokerAPIError):
    """A sanitized network, protocol, or broker error."""


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class KiwoomMockClient:
    """Fetch KR/US stock information from the mock environment.

    Credentials and tokens remain in memory. No requests occur at construction.
    An expired/rejected token requires another explicit authenticate() call.
    This initial client supports KRX, NASDAQ, NYSE, and AMEX quotes.
    """

    def __init__(self, app_key: str, app_secret: str):
        if not isinstance(app_key, str) or not app_key.strip():
            raise ValueError("모의투자 App Key가 필요합니다.")
        if not isinstance(app_secret, str) or not app_secret.strip():
            raise ValueError("모의투자 App Secret이 필요합니다.")
        self._app_key = app_key
        self._app_secret = app_secret
        self._token: str | None = None
        self._last_request_at: float | None = None
        self._lock = threading.RLock()
        self._opener = urllib.request.build_opener(_RejectRedirects())

    def __repr__(self) -> str:
        return f"KiwoomMockClient(authenticated={self._token is not None})"

    def authenticate(self) -> None:
        """Obtain a mock token without returning or persisting credentials."""
        with self._lock:
            self._token = None
            response = self._post(
                au10001.API_ID, au10001.request_body(self._app_key, self._app_secret),
            )
            token = response.get("token")
            token_type = response.get("token_type")
            if (
                not isinstance(token, str)
                or not token
                or not token.isascii()
                or any(ch.isspace() or ord(ch) < 33 or ord(ch) > 126 for ch in token)
                or not isinstance(token_type, str)
                or token_type.lower() != "bearer"
            ):
                raise KiwoomAPIError("모의투자 인증 응답에 유효한 토큰이 없습니다.")
            self._token = token

    def quote(self, market: str, symbol: str, exchange: str) -> dict[str, Any]:
        """Return the original quote object after successful authentication.

        market: KR or US. KR exchange: KRX. US exchange: ND/NY/NA, or
        NASDAQ/NYSE/AMEX. Prices remain strings exactly as returned by Kiwoom.
        """
        if not all(isinstance(value, str) for value in (market, symbol, exchange)):
            raise ValueError("시장·종목코드·거래소는 문자열이어야 합니다.")
        market, symbol, exchange = (
            value.strip().upper() for value in (market, symbol, exchange)
        )
        if market == "KR":
            if exchange != "KRX" or re.fullmatch(r"[0-9]{6}", symbol) is None:
                raise ValueError("국내 조회는 KRX와 6자리 종목코드를 사용합니다.")
            api_id, body = ka10001.API_ID, ka10001.request_body(symbol)
        elif market == "US":
            exchange = {"NASDAQ": "ND", "NYSE": "NY", "AMEX": "NA"}.get(
                exchange, exchange
            )
            if exchange not in {"ND", "NY", "NA"}:
                raise ValueError("미국 거래소는 ND, NY, NA 중 하나여야 합니다.")
            if re.fullmatch(r"[A-Z0-9][A-Z0-9.\-/]{0,19}", symbol) is None:
                raise ValueError("유효한 미국 종목코드가 필요합니다.")
            api_id, body = usa20100.API_ID, usa20100.request_body(symbol, exchange)
        else:
            raise ValueError("시장은 KR 또는 US여야 합니다.")

        with self._lock:
            if self._token is None:
                raise KiwoomAPIError("먼저 모의투자 authenticate()를 실행하세요.")
            return self._post(api_id, body)

    def top_turnover(
        self, market: str = "001", include_managed: bool = True, limit: int = 20,
        exchange: str = "3",
    ) -> list[dict[str, Any]]:
        """Return up to limit turnover rows in the broker's ranking order.

        market is 000 (all), 001 (KOSPI), or 101 (KOSDAQ). Raw string values
        are preserved. Continuation is fetched only when more rows are needed.
        exchange is 1 (KRX), 2 (NXT), or 3 (combined). Mock quotes may include
        NXT even though mock trading and account queries are limited to KRX.
        """
        if not isinstance(market, str) or market not in {"000", "001", "101"}:
            raise ValueError("시장은 000(전체), 001(코스피), 101(코스닥)이어야 합니다.")
        if type(include_managed) is not bool:
            raise ValueError("관리종목 포함 여부는 True 또는 False여야 합니다.")
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("조회 종목 수는 1부터 100 사이의 정수여야 합니다.")
        if not isinstance(exchange, str) or exchange not in {"1", "2", "3"}:
            raise ValueError("거래소는 1(KRX), 2(NXT), 3(통합)이어야 합니다.")
        body = ka10032.request_body(market, include_managed, exchange)
        rows: list[dict[str, Any]] = []
        seen_keys: set[str] = set()
        next_key: str | None = None
        with self._lock:
            if self._token is None:
                raise KiwoomAPIError("먼저 모의투자 authenticate()를 실행하세요.")
            for _ in range(_MAX_RANKING_PAGES):
                payload, continuation = self._request(
                    ka10032.API_ID, body,
                    cont_yn="Y" if next_key is not None else None,
                    next_key=next_key,
                )
                records = payload.get("trde_prica_upper")
                if not isinstance(records, list) or not all(
                    isinstance(record, dict) for record in records
                ):
                    raise KiwoomAPIError("키움 거래대금 순위 응답 형식이 올바르지 않습니다.")
                has_more = continuation.get("cont-yn") == "Y"
                if has_more:
                    returned_key = continuation.get("next-key")
                    if not returned_key or not returned_key.strip():
                        raise KiwoomAPIError("키움 연속조회 응답에 다음 조회 키가 없습니다.")
                    if returned_key in seen_keys:
                        raise KiwoomAPIError("키움 연속조회 응답의 조회 키가 반복되었습니다.")
                    seen_keys.add(returned_key)
                    next_key = returned_key
                rows.extend(records[: limit - len(rows)])
                if len(rows) == limit or not has_more:
                    return rows
        raise KiwoomAPIError("키움 거래대금 순위 연속조회가 최대 10페이지를 초과했습니다.")

    def _post(self, api_id: str, body: dict[str, str]) -> dict[str, Any]:
        payload, _ = self._request(api_id, body)
        return payload

    def _request(
        self, api_id: str, body: dict[str, str], *,
        cont_yn: str | None = None, next_key: str | None = None,
    ) -> tuple[dict[str, Any], dict[str, str]]:
        # A closed endpoint allowlist prevents accidental order endpoint use.
        if api_id not in _ENDPOINTS:
            raise KiwoomAPIError("지원하지 않는 조회 요청입니다.")
        headers = {"Content-Type": "application/json;charset=UTF-8", "api-id": api_id}
        if api_id != au10001.API_ID:
            headers["authorization"] = f"Bearer {self._token}"
        if cont_yn is not None:
            headers["cont-yn"] = cont_yn
        if next_key is not None:
            headers["next-key"] = next_key
        request = urllib.request.Request(
            _MOCK_ORIGIN + _ENDPOINTS[api_id],
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        with self._lock:
            now = time.monotonic()
            if self._last_request_at is not None:
                delay = _REQUEST_INTERVAL - (now - self._last_request_at)
                if delay > 0:
                    time.sleep(delay)
            self._last_request_at = time.monotonic()
            try:
                with queued_open(self._opener, request, timeout=_TIMEOUT_SECONDS, max_bytes=_MAX_RESPONSE_BYTES) as response:
                    raw = response.read(_MAX_RESPONSE_BYTES + 1)
                    continuation = self._continuation_headers(
                        getattr(response, "headers", None)
                    )
            except urllib.error.HTTPError as error:
                status = error.code
                error.close()
                # Deliberately omit URL, response body, and the exception cause.
                raise KiwoomAPIError(f"키움 모의투자 HTTP 오류 ({status}).") from None
            except (urllib.error.URLError, OSError, ValueError):
                raise KiwoomAPIError("키움 모의투자 연결 또는 응답 시간 초과 오류.") from None

        if len(raw) > _MAX_RESPONSE_BYTES:
            raise KiwoomAPIError("키움 모의투자 응답 크기가 제한을 초과했습니다.")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise KiwoomAPIError("키움 모의투자 응답이 올바른 JSON이 아닙니다.") from None
        if not isinstance(payload, dict):
            raise KiwoomAPIError("키움 모의투자 응답 형식이 올바르지 않습니다.")
        code = payload.get("return_code")
        if (type(code) is int and code == 0) or (type(code) is str and code == "0"):
            return payload, continuation
        if type(code) is int or (
            type(code) is str and re.fullmatch(r"-?[0-9]{1,6}", code)
        ):
            raise KiwoomAPIError(f"키움 모의투자 요청 실패 (return_code={code}).")
        raise KiwoomAPIError("키움 모의투자 응답에 유효한 성공 코드가 없습니다.")

    @staticmethod
    def _continuation_headers(response_headers: Any) -> dict[str, str]:
        """Extract only validated continuation values, never remote diagnostics."""
        if not isinstance(response_headers, (Mapping, Message)):
            # Older response fixtures and header-less responses are supported.
            return {}
        result: dict[str, str] = {}
        for name, value in response_headers.items():
            if not isinstance(name, str) or name.lower() not in {"cont-yn", "next-key"}:
                continue
            name = name.lower()
            if (
                name in result
                or not isinstance(value, str)
                or len(value) > 4096
                or not value.isascii()
                or any(ord(ch) < 32 or ord(ch) > 126 for ch in value)
            ):
                raise KiwoomAPIError("키움 연속조회 응답 헤더가 올바르지 않습니다.")
            if name == "cont-yn":
                value = value.strip()
                if value not in {"", "N", "Y"}:
                    raise KiwoomAPIError("키움 연속조회 응답 헤더가 올바르지 않습니다.")
            result[name] = value
        return result
