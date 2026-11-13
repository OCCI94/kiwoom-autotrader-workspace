"""One-shot KRX buy/sell submissions using the current in-memory login.

Schema: supplied 키움 REST API 문서.xlsx, kt10000 and kt10001, rows 8–38.
An accepted request is not proof of execution. An uncertain response is never
retried or paginated, including when continuation headers are returned.
"""

from __future__ import annotations
from .errors import BrokerAPIError

from dataclasses import dataclass
from datetime import datetime
import http.client
import json
import re
import urllib.error
import urllib.request
from kiwoom_autotrader.utils.http_queue import queued_open, HTTPNotSentError

from . import kt10000, kt10001
from kiwoom_autotrader.trading.order_context import prepare_tracking, complete_tracking, cancel_tracking
from kiwoom_autotrader.trading.paper_policy import capture_automatic_paper_guard, PaperAutomationPolicyError
from .session import BrokerSession, IssuedToken, LoginSessionError, KST


_ORIGINS = {"paper": "https://mockapi.kiwoom.com", "real": "https://api.kiwoom.com"}
_MAX_RESPONSE_BYTES = 64 * 1024
_UNCERTAIN = "주문 접수 여부를 확인하지 못했습니다. 자동 재전송하지 않았습니다. 증권사 주문내역을 확인한 뒤 다음 주문을 결정하세요."


def _dynamic_now():
    return datetime.now(KST)


def _check_dynamic_context(session, context, side, symbol):
    decision=getattr(context,'evidence',None)
    data=decision.as_dict() if decision is not None else {}
    admission=data.get('dynamic_s1_admission')
    if admission is None:return
    if (session.mode!='paper' or side!='buy' or context.strategy!='gdcrs'
            or admission.get('symbol')!=symbol):raise ValueError('dynamic S1 scope changed')
    from kiwoom_autotrader.trading.paper_funnel_state import assert_current_dynamic_s1_admission
    assert_current_dynamic_s1_admission(session,_dynamic_now(),admission)


class OrderError(BrokerAPIError):
    """Locally composed result with no token, remote body or remote message."""

    def __init__(self, message: str, *, uncertain: bool = False, reason_code=None):
        super().__init__(message)
        self.uncertain = uncertain
        self.reason_code = reason_code if reason_code in {"insufficient_cash", "daily_entry_limit_reached", "daily_entry_limit_unavailable",
            "concurrent_position_limit_reached", "concurrent_position_limit_unavailable",
            "theme_concentration_limit_reached", "theme_concentration_unavailable"} else None


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


def _check_external_storage(session, required, guard):
    """Optional hook only; configuring and rearming it is not implemented here.

    A future external-data launcher must set ``external_storage_required`` and
    install a reconciliation-aware callable before making any order possible.
    An unset flag preserves the current internal-storage behavior. A guard is
    checked again immediately before dispatch, after all queue waits.
    """
    current = getattr(session, "external_storage_required", False)
    if type(current) is not bool or current != required:
        raise ValueError("external storage configuration changed")
    if required:
        if not callable(guard) or getattr(session, "external_storage_guard", None) is not guard:
            raise ValueError("external storage guard unavailable")
        if guard() is False:
            raise ValueError("external storage guard rejected")


def _check_maintenance(session, guard):
    """Keep an installed receiver pause guard bound through the queue wait."""
    if getattr(session, "maintenance_order_guard", None) is not guard:
        raise ValueError("maintenance guard changed")
    if guard is not None:
        if not callable(guard) or guard() is False:
            raise ValueError("maintenance guard rejected")


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
            or int(number) <= 0 or token in number):
        raise OrderError(_UNCERTAIN, uncertain=True)
    if payload.get("dmst_stex_tp") not in (None, "", "KRX"):
        raise OrderError(_UNCERTAIN, uncertain=True)
    return OrderReceipt(mode, side, symbol, quantity, price, number, _ORIGINS[mode])


def submit_order(session: BrokerSession, side: str, symbol: str,
                 quantity: str | int, price: str | int | None = None, *, before_send=None) -> OrderReceipt:
    """Submit exactly once after local validation; never load or renew credentials.

    ``before_send`` is an idempotent validation callback, not an action. Queue
    waits and the final account/recording checks may invoke it multiple times.
    """
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
    maintenance_guard = getattr(session, "maintenance_order_guard", None)
    try:
        _check_maintenance(session, maintenance_guard)
    except Exception:
        raise OrderError("유지보수 상태를 확인하지 못해 주문을 전송하지 않았습니다.") from None
    storage_required = getattr(session, "external_storage_required", False)
    storage_guard = getattr(session, "external_storage_guard", None)
    try:
        if type(storage_required) is not bool:
            raise ValueError
        _check_external_storage(session, storage_required, storage_guard)
    except Exception:
        raise OrderError("외장 저장소와 재대사 상태를 확인하지 못해 주문을 전송하지 않았습니다.") from None
    automatic_guard = capture_automatic_paper_guard(session, side, symbol, quantity, price)
    require_no_pending = False
    if automatic_guard is not None:
        try:
            require_no_pending = automatic_guard.before_prepare()
        except PaperAutomationPolicyError as error:
            raise OrderError(str(error), reason_code=error.reason_code) from None
    recording_guard = getattr(session, "execution_recording_guard", None)
    if recording_guard is not None:
        try:
            recording_guard()
        except Exception:
            raise OrderError("실시간 체결 기록 연결을 확인하지 못해 주문을 전송하지 않았습니다.") from None
    tr = kt10000 if side == "buy" else kt10001
    body = tr.request_body(symbol, quantity, price) if side == "buy" else tr.request_body(symbol, quantity)
    request = urllib.request.Request(
        _ORIGINS[mode] + tr.ENDPOINT, data=json.dumps(body, ensure_ascii=True).encode("utf-8"),
        headers={"Content-Type": "application/json;charset=UTF-8",
                 "authorization": f"Bearer {token}", "api-id": tr.API_ID}, method="POST",
    )
    try:
        from kiwoom_autotrader.trading.order_context import _current
        _check_dynamic_context(session,_current.get(),side,symbol)
        options = {"price": price}
        if require_no_pending:
            options["require_no_pending"] = True
        captured_reconciliation = getattr(automatic_guard, "reconciliation_evidence", None)
        if captured_reconciliation is not None:
            options["reconciliation_evidence"] = captured_reconciliation
        tracking = prepare_tracking(mode, side, symbol, quantity, **options)
    except Exception as error:
        from kiwoom_autotrader.trading.execution_log import (DailyEntryLimitReached, DailyEntryLimitUnavailable,
            ConcurrentPositionLimitReached, ConcurrentPositionLimitUnavailable, PaperCashLimitReached,
            ThemeConcentrationLimitReached, ThemeConcentrationUnavailable)
        reason = ("daily_entry_limit_reached" if isinstance(error, DailyEntryLimitReached) else
                  "daily_entry_limit_unavailable" if isinstance(error, DailyEntryLimitUnavailable) else
                  "concurrent_position_limit_reached" if isinstance(error, ConcurrentPositionLimitReached) else
                  "concurrent_position_limit_unavailable" if isinstance(error, ConcurrentPositionLimitUnavailable) else
                  "theme_concentration_limit_reached" if isinstance(error, ThemeConcentrationLimitReached) else
                  "theme_concentration_unavailable" if isinstance(error, ThemeConcentrationUnavailable) else
                  "insufficient_cash" if isinstance(error, PaperCashLimitReached) else None)
        raise OrderError("자동주문 원장에 사전 기록하지 못해 주문을 전송하지 않았습니다.", reason_code=reason) from None
    opener = urllib.request.build_opener(_RejectRedirects())
    dispatch_skip_reason = None
    def guard():
        nonlocal dispatch_skip_reason
        if (session.mode, session.token) != (mode, token) or not session.token_usable:
            raise ValueError("session changed")
        _check_maintenance(session, maintenance_guard)
        if automatic_guard is not None:
            automatic_guard()
        if before_send is not None:
            before_send()
        if recording_guard is not None:
            recording_guard()
        if (session.mode, session.token) != (mode, token) or not session.token_usable:
            raise ValueError("session changed")
        if automatic_guard is not None:
            automatic_guard()
        if (session.mode, session.token) != (mode, token) or not session.token_usable:
            raise ValueError("session changed")
        # Recording and account checks above can themselves cross a minute
        # boundary. Re-run the caller's idempotent strategy guard at the end,
        # closest to the transport, and reject any session change it observes.
        if before_send is not None:
            before_send()
        _check_external_storage(session, storage_required, storage_guard)
        if (session.mode, session.token) != (mode, token) or not session.token_usable:
            raise ValueError("session changed")
        # A pause may have appeared during the checks above. Check it closest
        # to dispatch, even if this request already prepared a local intent.
        _check_maintenance(session, maintenance_guard)
        if tracking is not None:
            context, intent_id = tracking
            if mode == 'paper' and side == 'buy':
                theme_check = getattr(context.tracker, 'assert_paper_theme_dispatch', None)
                if callable(theme_check):
                    try:
                        theme_check(mode, symbol, intent_id)
                    except Exception as error:
                        reason = getattr(error, 'skipped_reason', None)
                        if reason in ('theme_concentration_limit_reached','theme_concentration_unavailable'):
                            dispatch_skip_reason = reason
                        raise
            decision = getattr(context, 'evidence', None)
            config = decision.as_dict().get('config') if decision is not None else None
            if isinstance(config, dict) and config.get('b3_stage') == 'add':
                check = getattr(context.tracker, 'assert_b3_add_dispatch', None)
                if not callable(check):raise ValueError('B3 dispatch evidence unavailable')
                check(mode, symbol, intent_id)
            if (isinstance(config,dict) and config.get('multi_exit_kind') is not None
                    or isinstance(captured_reconciliation,dict) and captured_reconciliation.get('schema_version')==4):
                check=getattr(context.tracker,'assert_multi_exit_dispatch',None)
                if not callable(check):raise ValueError('strategy exit dispatch evidence unavailable')
                check(mode,symbol,intent_id)
            if isinstance(captured_reconciliation,dict) and captured_reconciliation.get('schema_version') in (3, 5):
                check=getattr(context.tracker,'assert_multi_buy_dispatch',None)
                if not callable(check):raise ValueError('strategy buy dispatch evidence unavailable')
                check(mode,symbol,intent_id)
            # Last check, after all ledger/storage work: an original strict5
            # observation must still belong to this live paper minute/gate.
            # A new factory call cannot renew the immutable admission clock.
            if decision is not None and decision.as_dict().get('dynamic_s1_admission') is not None:
                _check_dynamic_context(session,context,side,symbol)
                if (session.mode,session.token)!=(mode,token) or not session.token_usable:
                    raise ValueError('dynamic S1 session changed')
    try:
        with queued_open(opener, request, timeout=15, max_bytes=_MAX_RESPONSE_BYTES, before_send=guard) as response:
            raw = response.read(_MAX_RESPONSE_BYTES + 1)
    except HTTPNotSentError:
        try:
            cancel_tracking(tracking, "not_sent")
        except Exception:
            pass
        raise OrderError("대기 중 로그인 또는 실행 조건이 변경되어 주문을 전송하지 않았습니다.",
                         reason_code=dispatch_skip_reason) from None
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
    try:
        receipt = _receipt(payload, mode=mode, side=side, symbol=symbol, quantity=quantity,
                           price=price, token=token)
    except OrderError as error:
        if not error.uncertain:
            try:
                cancel_tracking(tracking, "rejected")
            except Exception:
                pass
        raise
    try:
        complete_tracking(tracking, receipt)
    except Exception:
        raise OrderError("주문은 접수됐지만 자동주문 원장 연결을 확인하지 못했습니다. 자동 재전송하지 않으며 주문내역을 확인하세요.", uncertain=True) from None
    return receipt
