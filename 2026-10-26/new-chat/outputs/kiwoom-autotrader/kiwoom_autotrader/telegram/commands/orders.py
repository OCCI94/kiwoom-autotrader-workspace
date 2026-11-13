"""Explicit buy/sell commands, with no inference from collected messages."""

from __future__ import annotations

from dataclasses import dataclass
import re
import shlex

from kiwoom_autotrader.api.orders import (
    OrderError, OrderReceipt, normalize_order_code, normalize_positive_integer, submit_order,
)
from kiwoom_autotrader.api.stock_names import StockNameError, resolve_stock_name


BUY_USAGE = ("사용법: buy {종목코드 또는 종목명} {수량} [지정가]\n"
             "예: buy 005930 1 / buy 삼성전자 1 70000\n"
             "공백이 포함된 종목명은 따옴표로 묶으세요: buy \"삼성 전자\" 1\n"
             "수량·지정가는 12자리 이내 양의 정수이며 지정가를 생략하면 시장가입니다.")
SELL_USAGE = ("사용법: sell {6자리 종목코드} {수량}\n예: sell 005930 1\n"
              "수량은 12자리 이내 양의 정수이며 KRX 시장가 매도입니다.")
_UNKNOWN_RESULT = ("주문 처리 결과를 확인하지 못했습니다. 자동 재전송하지 않았습니다. "
                   "증권사 주문내역을 확인한 뒤 다음 주문을 결정하세요.")


@dataclass(frozen=True)
class ParsedOrder:
    side: str
    identifier: str
    quantity: str
    price: str | None = None


def _words(text: str, side: str, usage: str) -> list[str] | None:
    if not isinstance(text, str):
        return None
    prefix = text.strip().split(maxsplit=1)
    if not prefix or prefix[0].casefold() != side:
        return None
    try:
        words = shlex.split(text.strip(), comments=False, posix=True)
    except ValueError:
        raise ValueError(usage) from None
    return words


def parse_buy_command(text: str) -> ParsedOrder | None:
    """Names with spaces must be quoted; arguments are never guessed or split."""
    words = _words(text, "buy", BUY_USAGE)
    if words is None:
        return None
    if len(words) not in (3, 4):
        raise ValueError(BUY_USAGE)
    identifier = words[1].strip()
    if (not identifier or len(identifier) > 80 or not identifier.isprintable()
            or (identifier.isdecimal() and re.fullmatch(r"[0-9]{6}", identifier) is None)):
        raise ValueError(BUY_USAGE)
    try:
        quantity = normalize_positive_integer(words[2])
        price = normalize_positive_integer(words[3]) if len(words) == 4 else None
    except ValueError:
        raise ValueError(BUY_USAGE) from None
    return ParsedOrder("buy", identifier, quantity, price)


def parse_sell_command(text: str) -> ParsedOrder | None:
    words = _words(text, "sell", SELL_USAGE)
    if words is None:
        return None
    if len(words) != 3:
        raise ValueError(SELL_USAGE)
    try:
        symbol = normalize_order_code(words[1])
        quantity = normalize_positive_integer(words[2])
    except ValueError:
        raise ValueError(SELL_USAGE) from None
    return ParsedOrder("sell", symbol, quantity)


def _format_receipt(receipt: OrderReceipt) -> str:
    mode = {"paper": "모의투자", "real": "실전투자"}[receipt.mode]
    side = {"buy": "매수", "sell": "매도"}[receipt.side]
    price = f"지정가 {int(receipt.price):,}원" if receipt.price is not None else "시장가"
    return (f"{mode} {side} 주문 접수 · {receipt.exchange}\n"
            f"종목 {receipt.symbol} · {int(receipt.quantity):,}주 · {price}\n"
            f"주문번호 {receipt.order_number}\n"
            "접수 결과이며 실제 체결 여부는 확인하지 않았습니다.")


def _handle(text: str, session, side: str) -> list[str]:
    usage = BUY_USAGE if side == "buy" else SELL_USAGE
    try:
        parsed = parse_buy_command(text) if side == "buy" else parse_sell_command(text)
    except ValueError:
        return [usage]
    if parsed is None:
        return [usage]
    try:
        symbol = parsed.identifier
        if re.fullmatch(r"[0-9]{6}", symbol) is None:
            symbol = resolve_stock_name(session, symbol)
        symbol = normalize_order_code(symbol)
        receipt = submit_order(session, parsed.side, symbol, parsed.quantity, parsed.price)
        return [_format_receipt(receipt)]
    except (StockNameError, OrderError) as error:
        return [str(error)]
    except ValueError:
        return [usage]
    except Exception:
        return [_UNKNOWN_RESULT]


def handle_buy(text: str, session) -> list[str]:
    return _handle(text, session, "buy")


def handle_sell(text: str, session) -> list[str]:
    return _handle(text, session, "sell")
