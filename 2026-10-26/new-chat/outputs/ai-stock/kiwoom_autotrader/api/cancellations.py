"""One-shot paper KRX cancel-remainder transport; never a buy/sell receipt.

Contract: tr_docs/kt10003.md. cncl_qty=0 cancels the original order's remaining
quantity at the broker, including a partial-fill race. An input ACK does NOT
prove cancellation or release any position/budget reservation.
"""
from dataclasses import dataclass
import http.client
import json
import re
import urllib.error
import urllib.request

from .minute_chart import session_identity
from .orders import (_RejectRedirects, _check_external_storage, _check_maintenance,
                     normalize_order_code)
from kiwoom_autotrader.utils.http_queue import queued_open, HTTPNotSentError


class CancellationError(RuntimeError):
    def __init__(self, reason, *, uncertain=False):
        super().__init__(reason)
        self.uncertain = uncertain


@dataclass(frozen=True)
class CancellationReceipt:
    original_order_number: str
    cancellation_order_number: str
    reported_cancel_quantity: int


def _number(value):
    if not isinstance(value, str) or re.fullmatch(r"[0-9]{1,7}", value) is None or int(value) <= 0:
        raise ValueError("invalid order number")
    return str(int(value))


def cancel_paper_buy_remainder(session, symbol, original_order_number, *, before_send):
    """The caller must durably claim ownership before invoking this transport.

    ``before_send`` verifies the exact owned original order, fresh ka10075 and
    unchanged ledger/00 evidence. It is required and may be called repeatedly
    by the shared queue. No authentication, retry or automatic renewal occurs.
    """
    symbol = normalize_order_code(symbol)
    original = _number(original_order_number)
    identity = session_identity(session)
    if identity[0] != "paper" or not callable(before_send):
        raise CancellationError("paper_cancel_guard_required")
    maintenance = getattr(session, "maintenance_order_guard", None)
    recording = getattr(session, "execution_recording_guard", None)
    storage_required = getattr(session, "external_storage_required", False)
    storage_guard = getattr(session, "external_storage_guard", None)
    if not callable(recording) or type(storage_required) is not bool:
        raise CancellationError("paper_cancel_guard_required")

    def guard():
        if session_identity(session) != identity or identity[0] != "paper":
            raise ValueError("cancel session changed")
        _check_maintenance(session, maintenance)
        _check_external_storage(session, storage_required, storage_guard)
        if getattr(session, "execution_recording_guard", None) is not recording:
            raise ValueError("cancel recording guard changed")
        recording()
        before_send()
        if session_identity(session) != identity:
            raise ValueError("cancel session changed")
        _check_maintenance(session, maintenance)

    try:
        guard()
    except Exception:
        raise CancellationError("cancel_not_sent") from None
    request = urllib.request.Request("https://mockapi.kiwoom.com/api/dostk/ordr",
        data=json.dumps({"dmst_stex_tp": "KRX", "orig_ord_no": original_order_number,
                         "stk_cd": symbol, "cncl_qty": "0"}).encode(), method="POST",
        headers={"Content-Type": "application/json;charset=UTF-8",
                 "authorization": "Bearer " + identity[1], "api-id": "kt10003"})
    opener = urllib.request.build_opener(_RejectRedirects())
    try:
        with queued_open(opener, request, timeout=15, max_bytes=65536, before_send=guard) as response:
            raw = response.read(65537)
            api_id = response.headers.get("api-id")
    except HTTPNotSentError:
        raise CancellationError("cancel_not_sent") from None
    except (OSError, urllib.error.URLError, http.client.HTTPException, ValueError):
        raise CancellationError("cancel_response_unconfirmed", uncertain=True) from None
    try:
        if not isinstance(raw, bytes) or len(raw) > 65536 or api_id not in (None, "", "kt10003"):
            raise ValueError
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError
        code = payload.get("return_code")
        if not ((type(code) is int and code == 0) or type(code) is str and code == "0"):
            if type(code) is int or type(code) is str and re.fullmatch(r"-?[0-9]{1,6}", code):
                raise CancellationError("cancel_broker_rejected")
            raise ValueError
        number = _number(payload.get("ord_no"))
        parent = _number(payload.get("base_orig_ord_no"))
        quantity = payload.get("cncl_qty")
        if (parent != original or number == original or not isinstance(quantity, str)
                or re.fullmatch(r"\+?[0-9]{1,12}", quantity) is None):
            raise ValueError
        return CancellationReceipt(parent, number, int(quantity))
    except CancellationError:
        raise
    except (ValueError, UnicodeError, TypeError, RecursionError):
        raise CancellationError("cancel_response_unconfirmed", uncertain=True) from None
