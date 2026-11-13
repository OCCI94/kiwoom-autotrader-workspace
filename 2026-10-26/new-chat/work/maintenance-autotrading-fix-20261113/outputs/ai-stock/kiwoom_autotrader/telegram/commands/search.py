"""Resolve a stock code or name and show the six requested quote fields."""

import re

from kiwoom_autotrader.api.stock_info import StockInfoError, fetch_stock_info, normalize_stock_code
from kiwoom_autotrader.api.stock_names import StockNameError, resolve_stock_name


USAGE = ("사용법: srch {종목코드 또는 종목명}\n예: srch 005930 / srch 삼성전자\n"
         "종목명은 코스피·코스닥의 정확한 이름을 사용하세요.")


def parse_search_command(text: str) -> str | None:
    if not isinstance(text, str):
        return None
    parts = text.strip().split(maxsplit=1)
    if not parts or parts[0].casefold() != "srch":
        return None
    if len(parts) != 2:
        raise ValueError(USAGE)
    query = parts[1].strip()
    try:
        return normalize_stock_code(query)
    except ValueError:
        pass
    if (not query or len(query) > 40 or not query.isprintable()
            or query[0].isdigit() and query.replace(" ", "").isdigit()
            or re.match(r"[0-9]{6}(?:\s|_)", query)
            or re.fullmatch(r"[\w가-힣][\w가-힣 &().+%·/-]*", query) is None):
        raise ValueError(USAGE)
    return query


def _number(value: str, unit: str, *, price: bool = False) -> str:
    if not value:
        return "미제공"
    sign = value[0] if value[0] in "+-" else ""
    digits = value[1:] if sign else value
    integer, dot, fraction = digits.partition(".")
    # Quote-price signs indicate movement, not a negative monetary price.
    return f"{'' if price else sign}{int(integer):,}{dot}{fraction}{unit}"


def format_stock_info(data: dict[str, str], symbol: str, mode: str) -> list[str]:
    change = data.get("pred_pre", "")
    arrow = "🔺" if change.startswith("+") else "🔻" if change.startswith("-") else "↔️"
    n = lambda key, unit="원", price=False: _number(data.get(key, ""), unit, price=price)
    return ["\n".join([
        f"🔎 {data['stk_nm']} ({symbol})",
        f"💰 현재가: {n('cur_prc', price=True)}",
        f"📊 거래량: {n('trde_qty', '주')}",
        f"🌅 시가: {n('open_pric', price=True)}",
        f"🔼 고가: {n('high_pric', price=True)}",
        f"🔽 저가: {n('low_pric', price=True)}",
        f"{arrow} 전일대비: {n('pred_pre')}",
    ])]


def handle_search(text: str, session) -> list[str]:
    try:
        query = parse_search_command(text)
    except ValueError:
        return [USAGE]
    if query is None:
        return [USAGE]
    try:
        try:
            symbol = normalize_stock_code(query)
        except ValueError:
            symbol = resolve_stock_name(session, query)
        data = fetch_stock_info(session, symbol)
        return format_stock_info(data, symbol, session.mode)
    except (StockInfoError, StockNameError) as error:
        return [str(error)]
    except Exception:
        return ["종목정보를 표시하지 못했습니다. 로그인 상태와 종목코드·종목명을 확인하세요."]
