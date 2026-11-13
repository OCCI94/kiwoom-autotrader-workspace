"""Four domestic stock rankings using the current in-memory broker session.

Specification: tr_docs/ka10032.md, ka10027.md, ka10030.md, ka00198.md.
The caller owns login. Raw numbers and provider order are preserved; amount
conversion belongs to presentation. Every request uses the shared HTTP queue.
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

from .session import BrokerSession, IssuedToken, LoginSessionError
from kiwoom_autotrader.utils.http_queue import queued_open


RANK_LABELS = {1: "거래대금", 2: "상승률", 3: "거래량", 4: "인기검색"}
_ORIGINS = {"paper": "https://mockapi.kiwoom.com", "real": "https://api.kiwoom.com"}
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_MAX_PAGES = 20
_INVALID = "키움 순위 응답 형식이 올바르지 않습니다."


class RankingsError(BrokerAPIError):
    """Locally composed error, excluding server messages and credentials."""


@dataclass(frozen=True)
class RankedStock:
    """A stable provider-order entry; value uses the documented source unit.

    Empty value/name/metadata means unprovided, never numeric zero. Codes keep
    their exchange suffix. Only the code is mandatory for identification.
    """

    code: str
    name: str
    value: str
    provider_rank: str = ""
    rank_change: str = ""
    rank_change_sign: str = ""
    basis_at: str = ""


@dataclass(frozen=True)
class _Ranking:
    api_id: str
    endpoint: str
    output: str
    metric: str
    body: tuple[tuple[str, str], ...]


_RANKINGS = {
    1: _Ranking("ka10032", "/api/dostk/rkinfo", "trde_prica_upper", "trde_prica", (
        ("mrkt_tp", "000"), ("mang_stk_incls", "1"), ("stex_tp", "3"),
    )),
    2: _Ranking("ka10027", "/api/dostk/rkinfo", "pred_pre_flu_rt_upper", "flu_rt", (
        ("mrkt_tp", "000"), ("sort_tp", "1"), ("trde_qty_cnd", "0000"),
        ("stk_cnd", "0"), ("crd_cnd", "0"), ("updown_incls", "1"),
        ("pric_cnd", "0"), ("trde_prica_cnd", "0"), ("stex_tp", "3"),
    )),
    # The managed-stock flag is inverted relative to ka10032.
    3: _Ranking("ka10030", "/api/dostk/rkinfo", "tdy_trde_qty_upper", "trde_qty", (
        ("mrkt_tp", "000"), ("sort_tp", "1"), ("mang_stk_incls", "0"),
        ("crd_tp", "0"), ("trde_qty_tp", "0"), ("pric_tp", "0"),
        ("trde_prica_tp", "0"), ("mrkt_open_tp", "0"), ("stex_tp", "3"),
    )),
    # One minute is this product's initial choice, not an omitted API default.
    4: _Ranking("ka00198", "/api/dostk/stkinfo", "item_inq_rank", "bigd_rank", (
        ("qry_tp", "1"),
    )),
}


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _text(value, *, length: int, token: str) -> str:
    if value is None or value == "":
        return ""
    if (not isinstance(value, str) or len(value) > length
            or not value.isprintable() or token in value):
        raise RankingsError(_INVALID)
    return value.strip()


def _number(value, token: str, *, signed: bool = False, integer: bool = False) -> str:
    value = _text(value, length=20, token=token)
    pattern = (r"[+-]?" if signed else r"\+?") + r"[0-9]+" + ("" if integer else r"(?:\.[0-9]+)?")
    if value and re.fullmatch(pattern, value) is None:
        raise RankingsError(_INVALID)
    return value


def _rank(value, token: str) -> str:
    value = _text(value, length=20, token=token)
    if value and re.fullmatch(r"[0-9]+", value) is None:
        raise RankingsError(_INVALID)
    return value


def _basis(row: dict, token: str) -> str:
    day = _text(row.get("dt"), length=20, token=token)
    moment = _text(row.get("tm"), length=20, token=token)
    try:
        if day and re.fullmatch(r"[0-9]{8}", day) is None:
            raise ValueError
        if moment and re.fullmatch(r"[0-9]{6}", moment) is None:
            raise ValueError
        parts = []
        if day:
            parts.append(datetime.strptime(day, "%Y%m%d").strftime("%Y-%m-%d"))
        if moment:
            parts.append(datetime.strptime(moment, "%H%M%S").strftime("%H:%M:%S"))
        return " ".join(parts)
    except ValueError:
        raise RankingsError(_INVALID) from None


def _stock(row: dict, number: int, token: str) -> RankedStock:
    code = _text(row.get("stk_cd"), length=20, token=token)
    pattern = r"[0-9A-Z]{6}" + (r"(?:_(?:AL|NX))?" if number != 4 else "")
    if re.fullmatch(pattern, code) is None:
        raise RankingsError("키움 순위 응답의 종목코드를 확인하지 못했습니다.")
    name = _text(row.get("stk_nm"), length=40, token=token)
    metric = row.get(_RANKINGS[number].metric)
    value = _rank(metric, token) if number == 4 else _number(
        metric, token, signed=number == 2, integer=number == 3,
    )
    rank = value if number == 4 else _rank(row.get("now_rank"), token) if number == 1 else ""
    change = sign = basis = ""
    if number == 4:
        change = _number(row.get("rank_chg"), token, signed=True, integer=True)
        sign = _text(row.get("rank_chg_sign"), length=20, token=token)
        # The description and example use different no-change markers.
        if sign not in {"", "+", "-", "N", "'"}:
            raise RankingsError(_INVALID)
        basis = _basis(row, token)
    return RankedStock(code, name, value, rank, change, sign, basis)


def _rows(payload, output: str) -> list[dict]:
    if not isinstance(payload, dict):
        raise RankingsError(_INVALID)
    # ka10030's supplied example uses camelCase; other examples use snake_case.
    statuses = [payload[key] for key in ("return_code", "returnCode") if key in payload]
    if not statuses:
        raise RankingsError("키움 순위 조회 성공 여부를 확인하지 못했습니다.")
    for code in statuses:
        if (type(code) is int and code == 0) or (type(code) is str and code == "0"):
            continue
        if ((type(code) is int and -999999 <= code <= 999999)
                or (type(code) is str and re.fullmatch(r"-?[0-9]{1,6}", code))):
            raise RankingsError(f"키움 순위 조회 실패 (return_code={code}).")
        raise RankingsError("키움 순위 조회 성공 여부를 확인하지 못했습니다.")
    result = payload.get(output)
    if not isinstance(result, list) or not all(isinstance(row, dict) for row in result):
        raise RankingsError(_INVALID)
    return result


def _next_key(headers) -> str | None:
    if not hasattr(headers, "items"):
        raise RankingsError("키움 순위 연속조회 헤더를 확인하지 못했습니다.")
    values = {}
    for name, value in headers.items():
        key = name.lower() if isinstance(name, str) else ""
        if key not in {"cont-yn", "next-key"}:
            continue
        if key in values and values[key] != value:
            raise RankingsError("키움 순위 연속조회 헤더가 서로 다릅니다.")
        values[key] = value
    continuation = values.get("cont-yn", "")
    if not isinstance(continuation, str) or continuation not in {"", "N", "Y"}:
        raise RankingsError("키움 순위 연속조회 여부가 올바르지 않습니다.")
    if continuation != "Y":
        return None
    key = values.get("next-key")
    if (not isinstance(key, str) or not key.strip() or len(key) > 50
            or not key.isascii() or any(ord(ch) < 32 or ord(ch) == 127 for ch in key)):
        raise RankingsError("키움 순위 연속조회 키가 올바르지 않습니다.")
    return key


def _session_values(session: BrokerSession) -> tuple[str, str]:
    if not isinstance(session, BrokerSession) or not session.authenticated:
        raise RankingsError("먼저 login paper 또는 login real로 로그인하세요.")
    mode, token = session.mode, session.token
    if not isinstance(mode, str) or mode not in _ORIGINS:
        raise RankingsError("로그인 투자 구분을 확인하지 못했습니다. 다시 로그인하세요.")
    try:
        IssuedToken(token)
    except LoginSessionError:
        raise RankingsError("유효한 로그인 토큰이 없습니다. 다시 로그인하세요.") from None
    if not session.token_usable:
        raise RankingsError("로그인 토큰이 만료되었습니다. login paper 또는 login real로 다시 로그인하세요.")
    return mode, token


def fetch_ranked_stocks(session: BrokerSession, rank_number: int, limit: int = 20) -> list[RankedStock]:
    """Fetch up to ``limit`` distinct codes, preserving provider order and units.

    A shorter list is returned only after a successful final page. A failure
    on any needed page raises instead of returning an incomplete snapshot.
    No credentials, login, persistent cache, or order endpoints are used.
    """
    if type(rank_number) is not int or rank_number not in _RANKINGS:
        raise ValueError("순위 번호는 1~4이어야 합니다.")
    if type(limit) is not int or not 1 <= limit <= 20:
        raise ValueError("조회 종목 수는 1~20 정수이어야 합니다.")
    mode, token = _session_values(session)

    def check_session() -> None:
        if _session_values(session) != (mode, token):
            raise RankingsError("조회 중 로그인 상태가 변경되었습니다. 다시 조회하세요.")

    spec = _RANKINGS[rank_number]
    data = json.dumps(dict(spec.body), ensure_ascii=True).encode("utf-8")
    opener = urllib.request.build_opener(_RejectRedirects())
    rows: list[RankedStock] = []
    seen_codes: set[str] = set()
    seen_keys: set[str] = set()
    next_key = None
    for _ in range(_MAX_PAGES):
        check_session()
        headers = {"Content-Type": "application/json;charset=UTF-8",
                   "authorization": f"Bearer {token}", "api-id": spec.api_id}
        if next_key is not None:
            headers.update({"cont-yn": "Y", "next-key": next_key})
        request = urllib.request.Request(_ORIGINS[mode] + spec.endpoint, data=data,
                                         headers=headers, method="POST")
        try:
            with queued_open(opener, request, timeout=15, max_bytes=_MAX_RESPONSE_BYTES,
                             before_send=check_session) as response:
                raw = response.read(_MAX_RESPONSE_BYTES + 1)
                response_headers = response.headers
        except RankingsError:
            raise
        except urllib.error.HTTPError as error:
            code = error.code
            try:
                error.close()
            except Exception:
                pass
            if type(code) is int and 100 <= code <= 599:
                raise RankingsError(f"키움 순위 조회 HTTP 오류 (code={code}).") from None
            raise RankingsError("키움 순위 조회 요청에 실패했습니다.") from None
        except (OSError, urllib.error.URLError, http.client.HTTPException, ValueError):
            raise RankingsError("키움 순위 조회 서버에 연결하지 못했습니다.") from None
        except Exception:
            raise RankingsError("키움 순위 조회 요청을 완료하지 못했습니다.") from None
        check_session()
        if not isinstance(raw, bytes) or len(raw) > _MAX_RESPONSE_BYTES:
            raise RankingsError("키움 순위 응답 크기나 형식이 올바르지 않습니다.")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeError, RecursionError):
            raise RankingsError(_INVALID) from None
        before = len(rows)
        for row in _rows(payload, spec.output):
            stock = _stock(row, rank_number, token)
            if stock.code in seen_codes:
                continue
            rows.append(stock)
            seen_codes.add(stock.code)
            if len(rows) == limit:
                return rows
        next_key = _next_key(response_headers)
        if next_key is None:
            return rows
        if next_key in seen_keys:
            raise RankingsError("키움 순위 연속조회 키가 반복되었습니다.")
        if len(rows) == before:
            raise RankingsError("키움 순위 연속조회에서 새로운 종목을 확보하지 못했습니다.")
        seen_keys.add(next_key)
    raise RankingsError("키움 순위 연속조회가 최대 페이지 수를 초과했습니다.")
