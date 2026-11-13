"""Read domestic cash and holdings using only the active in-memory login.

Source: supplied 키움 REST API 문서.xlsx, kt00001 rows 18–115 and
kt00018 rows 18–64. Numeric strings retain provider precision and missingness.
No credentials, owner name, account number, or response body is logged/stored.
"""

from __future__ import annotations

from datetime import datetime
import hashlib
import http.client
import json
import re
import time
import urllib.error
import urllib.request

from . import kt00001, kt00018
from .session import BrokerSession, IssuedToken, LoginSessionError


_ORIGINS = {"paper": "https://mockapi.kiwoom.com", "real": "https://api.kiwoom.com"}
_MAX_RESPONSE_BYTES = 1024 * 1024
_MAX_PAGES = 20
_MAX_HOLDINGS = 2000
_MIN_REQUEST_INTERVAL = 1.05

# Each retained field is present in the supplied workbook. Everything else,
# including account-owner identifiers and server return_msg, is discarded.
CASH_FIELDS = ("entr", "pymn_alow_amt", "ord_alow_amt", "d1_entra", "d2_entra")
TOTAL_FIELDS = ("tot_pur_amt", "tot_evlt_amt", "tot_evlt_pl", "tot_prft_rt",
                "prsm_dpst_aset_amt", "tot_loan_amt")
HOLDING_INTEGER_FIELDS = ("evltv_prft", "pur_pric", "rmnd_qty", "trde_able_qty",
                          "cur_prc", "pur_amt", "evlt_amt")
_INVALID = "계좌 조회 응답 형식이 올바르지 않습니다. 잠시 후 다시 조회하세요."


class AccountReportError(RuntimeError):
    """Locally composed messages suitable for a private Telegram reply."""


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _session_credentials(session: BrokerSession) -> tuple[str, str]:
    if not isinstance(session, BrokerSession) or not session.authenticated:
        raise AccountReportError("먼저 login paper 또는 login real로 로그인하세요.")
    mode, token = session.mode, session.token
    if not isinstance(mode, str) or mode not in _ORIGINS:
        raise AccountReportError("로그인 투자 구분을 확인하지 못했습니다. 다시 로그인하세요.")
    try:
        IssuedToken(token)
        usable = session.token_usable
    except (LoginSessionError, ValueError, TypeError):
        raise AccountReportError("유효한 로그인 토큰이 없습니다. 다시 로그인하세요.") from None
    if not usable:
        raise AccountReportError("로그인 토큰이 만료되었습니다. login paper 또는 login real로 다시 로그인하세요.")
    return mode, token


def _numeric(value, *, length: int = 15, percent: bool = False, token: str) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str) or len(value) > length:
        raise AccountReportError(_INVALID)
    value = value.strip()
    if not value:
        return None
    pattern = r"[+-]?[0-9]+(?:\.[0-9]{1,2})?" if percent else r"[+-]?[0-9]+"
    if re.fullmatch(pattern, value) is None or token in value:
        raise AccountReportError(_INVALID)
    return value


def _text(value, *, length: int, token: str, pattern: str | None = None) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str) or len(value) > length or not value.isprintable() or token in value:
        raise AccountReportError(_INVALID)
    value = value.strip()
    if not value:
        return None
    if pattern is not None and re.fullmatch(pattern, value) is None:
        raise AccountReportError(_INVALID)
    return value


def _holding(row: dict, token: str) -> dict[str, str | None]:
    if not isinstance(row, dict):
        raise AccountReportError(_INVALID)
    result = {key: _numeric(row.get(key), length=12 if key == "cur_prc" else 15, token=token)
              for key in HOLDING_INTEGER_FIELDS}
    result["prft_rt"] = _numeric(row.get("prft_rt"), length=12, percent=True, token=token)
    result["stk_cd"] = _text(row.get("stk_cd"), length=12, token=token, pattern=r"(?:[AJQ])?[0-9]{6}")
    result["stk_nm"] = _text(row.get("stk_nm"), length=40, token=token)
    result["crd_tp"] = _text(row.get("crd_tp"), length=2, token=token, pattern=r"[0-9]{2}")
    result["crd_loan_dt"] = _text(row.get("crd_loan_dt"), length=8, token=token, pattern=r"[0-9]{8}")
    if result["crd_loan_dt"]:
        try:
            datetime.strptime(result["crd_loan_dt"], "%Y%m%d")
        except ValueError:
            raise AccountReportError(_INVALID) from None
    return result


def _success(payload) -> None:
    if not isinstance(payload, dict):
        raise AccountReportError(_INVALID)
    code = payload.get("return_code")
    if (type(code) is int and code == 0) or (type(code) is str and code == "0"):
        return
    if ((type(code) is int and -999999 <= code <= 999999)
            or (type(code) is str and re.fullmatch(r"-?[0-9]{1,6}", code))):
        raise AccountReportError(f"키움 계좌 조회 실패 (return_code={code}). 로그인과 계좌 이용 상태를 확인하세요.")
    raise AccountReportError("키움 계좌 조회 성공 여부를 확인하지 못했습니다.")


def _request(opener, origin: str, token: str, module, body: dict, cont_yn: str, next_key: str):
    request = urllib.request.Request(
        origin + module.ENDPOINT,
        data=json.dumps(body, ensure_ascii=True).encode("utf-8"),
        headers={"Content-Type": "application/json;charset=UTF-8", "authorization": f"Bearer {token}",
                 "api-id": module.API_ID, "cont-yn": cont_yn, "next-key": next_key},
        method="POST",
    )
    try:
        with opener.open(request, timeout=15) as response:
            raw = response.read(_MAX_RESPONSE_BYTES + 1)
            response_api_id = response.headers.get("api-id")
            continuation = response.headers.get("cont-yn")
            key = response.headers.get("next-key")
    except urllib.error.HTTPError as error:
        code = error.code
        try:
            error.close()
        except Exception:
            pass
        if type(code) is int and 100 <= code <= 599:
            raise AccountReportError(f"키움 계좌 조회 HTTP 오류 (code={code}).") from None
        raise AccountReportError("키움 계좌 조회 요청에 실패했습니다.") from None
    except (OSError, urllib.error.URLError, http.client.HTTPException, ValueError):
        raise AccountReportError("키움 계좌 조회 서버에 연결하지 못했습니다.") from None
    if not isinstance(raw, bytes):
        raise AccountReportError(_INVALID)
    if len(raw) > _MAX_RESPONSE_BYTES:
        raise AccountReportError("계좌 조회 응답 크기가 제한을 초과했습니다.")
    if response_api_id not in {None, "", module.API_ID}:
        raise AccountReportError(_INVALID)
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeError, RecursionError):
        raise AccountReportError(_INVALID) from None
    _success(payload)
    if continuation not in {None, "", "N", "Y"}:
        raise AccountReportError("계좌 연속조회 여부를 확인하지 못했습니다.")
    if continuation == "Y":
        if (not isinstance(key, str) or not 1 <= len(key) <= 50 or not key.strip()
                or not key.isascii() or not key.isprintable()):
            raise AccountReportError("계좌 연속조회 키가 올바르지 않습니다.")
        return payload, key
    return payload, None


def _merge_totals(target: dict, page: dict) -> bool:
    """Use the first supplied value for each total; never add page summaries."""
    changed = False
    for key, value in page.items():
        if target[key] is None:
            target[key] = value
        elif value is not None and value != target[key]:
            changed = True
    return changed


def fetch_account_report(session: BrokerSession) -> dict:
    """Return only validated cash/totals/holdings, or a safe complete failure.

    Both TRs are read to their final continuation page, subject to bounded page
    and row limits. A repeated continuation key or any later-page failure rejects
    the report instead of presenting a partial holdings list as complete.
    """
    mode, token = _session_credentials(session)
    opener = urllib.request.build_opener(_RejectRedirects())
    cash = dict.fromkeys(CASH_FIELDS)
    totals = dict.fromkeys(TOTAL_FIELDS)
    holdings: list[dict] = []
    holdings_supplied = True
    previous_rows: set[bytes] = set()
    snapshot_changed = False
    last_request_at = None
    page_counts = {}
    for module, body, summary, names in (
        (kt00001, kt00001.request_body(), cash, CASH_FIELDS),
        (kt00018, kt00018.request_body(), totals, TOTAL_FIELDS),
    ):
        next_key = ""
        seen_keys: set[str] = set()
        for page_number in range(1, _MAX_PAGES + 1):
            if _session_credentials(session) != (mode, token):
                raise AccountReportError("조회 중 로그인 상태가 변경되었습니다. 다시 조회하세요.")
            if last_request_at is not None:
                time.sleep(max(0.0, _MIN_REQUEST_INTERVAL - (time.monotonic() - last_request_at)))
            last_request_at = time.monotonic()
            payload, following_key = _request(opener, _ORIGINS[mode], token, module, body,
                                              "Y" if next_key else "N", next_key)
            page_totals = {key: _numeric(payload.get(key), length=12 if key == "tot_prft_rt" else 15,
                                         percent=key == "tot_prft_rt", token=token) for key in names}
            snapshot_changed |= _merge_totals(summary, page_totals)
            if module is kt00018:
                raw_rows = payload.get(kt00018.HOLDINGS_FIELD)
                if raw_rows is None:
                    holdings_supplied = False
                elif not isinstance(raw_rows, list):
                    raise AccountReportError(_INVALID)
                else:
                    if len(raw_rows) > _MAX_HOLDINGS:
                        raise AccountReportError("보유 내역이 조회 한도를 초과했습니다. 전체 내역을 표시하지 못했습니다.")
                    current_rows = []
                    current_keys = set()
                    for raw_row in raw_rows:
                        row = _holding(raw_row, token)
                        identity = hashlib.sha256(json.dumps(raw_row, sort_keys=True,
                                                              separators=(",", ":")).encode("utf-8")).digest()
                        # A code can legitimately occur on multiple rows. Only
                        # identical previously returned rows are page overlap.
                        if identity not in previous_rows:
                            current_rows.append(row)
                        current_keys.add(identity)
                    if len(holdings) + len(current_rows) > _MAX_HOLDINGS:
                        raise AccountReportError("보유 내역이 조회 한도를 초과했습니다. 전체 내역을 표시하지 못했습니다.")
                    holdings.extend(current_rows)
                    previous_rows.update(current_keys)
            page_counts[module.API_ID] = page_number
            if following_key is None:
                break
            if following_key in seen_keys:
                raise AccountReportError("계좌 연속조회 키가 반복되어 전체 내역을 확인하지 못했습니다.")
            seen_keys.add(following_key)
            next_key = following_key
        else:
            raise AccountReportError("계좌 연속조회 한도를 초과했습니다. 전체 내역을 확인하지 못했습니다.")
    return {"mode": mode, "exchange": "KRX", "cash_query_type": "3", "cash": cash, "totals": totals,
            "holdings": holdings if holdings_supplied else None, "snapshot_changed": snapshot_changed,
            "pages": page_counts}
