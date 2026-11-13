"""One-shot KRX buy/sell submissions using the current in-memory login.

Schema: supplied 키움 REST API 문서.xlsx, kt10000 and kt10001, rows 8–38.
An accepted request is not proof of execution. An uncertain response is never
retried or paginated, including when continuation headers are returned.
"""

from __future__ import annotations

from dataclasses import dataclass
import http.client
import json
import re
import urllib.error
import urllib.request

from . import kt10000, kt10001
from .session import BrokerSession, IssuedToken, LoginSessionError


_ORIGINS = {"paper": "https://mockapi.kiwoom.com", "real": "https://api.kiwoom.com"}
_MAX_RESPONSE_BYTES = 64 * 1024
_UNCERTAIN = "주문 접수 여부를 확인하지 못했습니다. 자동 재전송하지 않았습니다. 증권사 주문내역을 확인한 뒤 다음 주문을 결정하세요."


class OrderError(RuntimeError):
    """Locally composed result with no token, remote body or remote message."""

    def __init__(self, message: str, *, uncertain: bool = False):
        super().__init__(message)
        self.uncertain = uncertain


@dataclass(frozen=True)
class OrderReceipt:
    mode: str
    side: str
    symbol: str
    quantity: str
    price: str | None
    order_number: str
    origin: str
    exchange: str = "KRX"


def normalize_order_code(value: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9]{6}", value) is None:
        raise ValueError("주문 종목코드는 KRX 6자리 숫자로 입력하세요.")
    return value


def normalize_positive_integer(value: str | int) -> str:
    """Accept only whole positive quantities/prices within the TR's 12 digits."""
    if type(value) is int:
        value = str(value)
    if (not isinstance(value, str) or re.fullmatch(r"[0-9]{1,12}", value) is None
            or int(value) <= 0):
        raise ValueError("수량과 가격은 12자리 이내의 양의 정수로 입력하세요.")
    return str(int(value))


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _receipt(payload, *, mode: str, side: str, symbol: str,
             quantity: str, price: str | None, token: str) -> OrderReceipt:
    if not isinstance(payload, dict):
        raise OrderError(_UNCERTAIN, uncertain=True)
    code = payload.get("return_code")
    if not ((type(code) is int and code == 0) or (type(code) is str and code == "0")):
        if ((type(code) is int and -999999 <= code <= 999999)
                or (type(code) is str and re.fullmatch(r"-?[0-9]{1,6}", code))):
            raise OrderError(f"주문 요청이 실패했습니다 (return_code={code}). 자동 재전송하지 않았습니다.")
        raise OrderError(_UNCERTAIN, uncertain=True)
    number = payload.get("ord_no")
    # The schema says seven digits but kt10000's response example is '00024'.
    # Preserve leading zeros and accept 1–7 digits; never echo arbitrary text.
    if (not isinstance(number, str) or re.fullmatch(r"[0-9]{1,7}", number) is None
            or token in number):
        raise OrderError(_UNCERTAIN, uncertain=True)
    if payload.get("dmst_stex_tp") not in (None, "", "KRX"):
        raise OrderError(_UNCERTAIN, uncertain=True)
    return OrderReceipt(mode, side, symbol, quantity, price, number, _ORIGINS[mode])


def submit_order(session: BrokerSession, side: str, symbol: str,
                 quantity: str | int, price: str | int | None = None) -> OrderReceipt:
    """Submit exactly once after local validation; never load or renew credentials."""
    if not isinstance(side, str) or side not in {"buy", "sell"}:
        raise ValueError("주문 구분은 buy 또는 sell이어야 합니다.")
    symbol = normalize_order_code(symbol)
    quantity = normalize_positive_integer(quantity)
    if side == "sell" and price is not None:
        raise ValueError("sell 명령은 시장가 매도만 지원합니다.")
    price = normalize_positive_integer(price) if price is not None else None
    if not isinstance(session, BrokerSession) or not session.authenticated:
        raise OrderError("먼저 login paper 또는 login real로 로그인하세요. 주문을 전송하지 않았습니다.")
    mode, token = session.mode, session.token
    if not isinstance(mode, str) or mode not in _ORIGINS:
        raise OrderError("투자 구분을 확인하지 못했습니다. 다시 로그인하세요. 주문을 전송하지 않았습니다.")
    try:
        IssuedToken(token)
    except LoginSessionError:
        raise OrderError("유효한 로그인 토큰이 없습니다. 주문을 전송하지 않았습니다.") from None
    if not session.token_usable:
        raise OrderError("로그인 토큰이 만료되었습니다. 다시 로그인하세요. 주문을 전송하지 않았습니다.")
    tr = kt10000 if side == "buy" else kt10001
    body = tr.request_body(symbol, quantity, price) if side == "buy" else tr.request_body(symbol, quantity)
    request = urllib.request.Request(
        _ORIGINS[mode] + tr.ENDPOINT, data=json.dumps(body, ensure_ascii=True).encode("utf-8"),
        headers={"Content-Type": "application/json;charset=UTF-8",
                 "authorization": f"Bearer {token}", "api-id": tr.API_ID}, method="POST",
    )
    opener = urllib.request.build_opener(_RejectRedirects())
    try:
        with opener.open(request, timeout=15) as response:
            raw = response.read(_MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as error:
        try:
            error.close()
        except Exception:
            pass
        raise OrderError(_UNCERTAIN, uncertain=True) from None
    except (OSError, urllib.error.URLError, http.client.HTTPException, ValueError):
        raise OrderError(_UNCERTAIN, uncertain=True) from None
    if not isinstance(raw, bytes) or len(raw) > _MAX_RESPONSE_BYTES:
        raise OrderError(_UNCERTAIN, uncertain=True)
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeError, RecursionError):
        raise OrderError(_UNCERTAIN, uncertain=True) from None
    return _receipt(payload, mode=mode, side=side, symbol=symbol, quantity=quantity,
                    price=price, token=token)
