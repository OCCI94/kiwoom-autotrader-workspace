"""Read ka10001 with the current in-memory login; no authentication or orders.

Schema source: supplied 키움 REST API 문서.xlsx, ka10001 rows 8–72.
Only a fixed origin selected by BrokerSession.mode receives that session's
token. Credentials are never loaded here, and redirects are never followed.
"""

from __future__ import annotations

import http.client
import json
import re
import urllib.error
import urllib.request

from . import ka10001
from .session import BrokerSession, IssuedToken, LoginSessionError


_ORIGINS = {"paper": "https://mockapi.kiwoom.com", "real": "https://api.kiwoom.com"}
_MAX_RESPONSE_BYTES = 64 * 1024
NUMERIC_FIELDS = (
    "fav", "cap", "flo_stk", "crd_rt", "oyr_hgst", "oyr_lwst", "mac",
    "mac_wght", "for_exh_rt", "repl_pric", "per", "eps", "roe", "pbr", "ev",
    "bps", "sale_amt", "bus_pro", "cup_nga", "250hgst", "250lwst",
    "high_pric", "open_pric", "low_pric", "upl_pric", "lst_pric", "base_pric",
    "exp_cntr_pric", "exp_cntr_qty", "250hgst_pric_pre_rt", "250lwst_pric_pre_rt",
    "cur_prc", "pred_pre", "flu_rt", "trde_qty", "trde_pre", "dstr_stk", "dstr_rt",
)
_OTHER_FIELDS = ("setl_mm", "250hgst_pric_dt", "250lwst_pric_dt", "pre_sig", "fav_unit")


class StockInfoError(RuntimeError):
    """A locally composed, credential-free lookup failure."""


def normalize_stock_code(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("종목코드는 6자리 숫자 또는 _NX·_AL 접미사 형식이어야 합니다.")
    result = value.strip().upper()
    if re.fullmatch(r"[0-9]{6}(?:_NX|_AL)?", result) is None:
        raise ValueError("종목코드는 6자리 숫자 또는 _NX·_AL 접미사 형식이어야 합니다.")
    return result


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _validate_payload(payload, symbol: str, token: str) -> dict[str, str]:
    invalid = "키움 종목정보 응답 형식이 올바르지 않습니다."
    if not isinstance(payload, dict):
        raise StockInfoError(invalid)
    code = payload.get("return_code")
    if not ((type(code) is int and code == 0) or (type(code) is str and code == "0")):
        if ((type(code) is int and -999999 <= code <= 999999)
                or (type(code) is str and re.fullmatch(r"-?[0-9]{1,6}", code))):
            raise StockInfoError(f"키움 종목정보 조회 실패 (return_code={code}).")
        raise StockInfoError("키움 종목정보 조회 성공 여부를 확인하지 못했습니다.")
    try:
        returned_code = normalize_stock_code(payload.get("stk_cd"))
    except ValueError:
        raise StockInfoError(invalid) from None
    # Allow a base-code echo for an exchange-specific request, but never another
    # stock or an explicitly different exchange suffix.
    if returned_code not in {symbol, symbol[:6]}:
        raise StockInfoError("키움 응답의 종목코드가 요청한 종목과 다릅니다.")
    name = payload.get("stk_nm")
    if (not isinstance(name, str) or not name.strip() or len(name) > 40
            or not name.isprintable() or token in name):
        raise StockInfoError(invalid)
    result = {"stk_cd": returned_code, "stk_nm": name.strip()}
    for key in NUMERIC_FIELDS + _OTHER_FIELDS:
        value = payload.get(key, "")
        if value is None:
            value = ""
        if not isinstance(value, str) or len(value) > 20 or (value and not value.isprintable()):
            raise StockInfoError(invalid)
        value = value.strip()
        if key in NUMERIC_FIELDS and value and re.fullmatch(r"[+-]?[0-9]+(?:\.[0-9]+)?", value) is None:
            raise StockInfoError(invalid)
        result[key] = value
    # Unknown fields and return_msg are intentionally not exposed to callers.
    return result


def fetch_stock_info(session: BrokerSession, symbol: str) -> dict[str, str]:
    """Fetch a single stock using the usable token already held by the session."""
    symbol = normalize_stock_code(symbol)
    if not isinstance(session, BrokerSession) or not session.authenticated:
        raise StockInfoError("먼저 login paper 또는 login real로 로그인하세요.")
    mode, token = session.mode, session.token
    if not isinstance(mode, str) or mode not in _ORIGINS:
        raise StockInfoError("로그인 투자 구분을 확인하지 못했습니다. 다시 로그인하세요.")
    try:
        IssuedToken(token)
    except LoginSessionError:
        raise StockInfoError("유효한 로그인 토큰이 없습니다. 다시 로그인하세요.") from None
    if not session.token_usable:
        raise StockInfoError("로그인 토큰이 만료되었습니다. login paper 또는 login real로 다시 로그인하세요.")
    request = urllib.request.Request(
        _ORIGINS[mode] + ka10001.ENDPOINT,
        data=json.dumps(ka10001.request_body(symbol), ensure_ascii=True).encode("utf-8"),
        headers={"Content-Type": "application/json;charset=UTF-8",
                 "authorization": f"Bearer {token}", "api-id": ka10001.API_ID},
        method="POST",
    )
    opener = urllib.request.build_opener(_RejectRedirects())
    try:
        with opener.open(request, timeout=15) as response:
            raw = response.read(_MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as error:
        code = error.code
        try:
            error.close()
        except Exception:
            pass
        if type(code) is int and 100 <= code <= 599:
            raise StockInfoError(f"키움 종목정보 HTTP 오류 (code={code}).") from None
        raise StockInfoError("키움 종목정보 요청에 실패했습니다.") from None
    except (OSError, urllib.error.URLError, http.client.HTTPException, ValueError):
        raise StockInfoError("키움 종목정보 서버에 연결하지 못했습니다.") from None
    if not isinstance(raw, bytes):
        raise StockInfoError("키움 종목정보 응답 형식이 올바르지 않습니다.")
    if len(raw) > _MAX_RESPONSE_BYTES:
        raise StockInfoError("키움 종목정보 응답 크기가 제한을 초과했습니다.")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeError, RecursionError):
        raise StockInfoError("키움 종목정보 응답 형식이 올바르지 않습니다.") from None
    return _validate_payload(payload, symbol, token)
