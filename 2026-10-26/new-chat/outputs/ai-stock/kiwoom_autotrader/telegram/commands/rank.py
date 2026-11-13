"""Numbered ranking menu and presentation; broker order remains unchanged."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from kiwoom_autotrader.api.rankings import (
    RANK_LABELS, RankedStock, RankingsError, fetch_ranked_stocks,
)
from .report import _chunks


USAGE = "사용법: rank / rank 1 / rank 2 / rank 3 / rank 4"
MENU = "\n".join([
    "조회할 순위를 선택하세요.",
    *[f"{number}. {label} 상위 20종목" for number, label in RANK_LABELS.items()],
    USAGE,
])


def parse_rank_command(text: str) -> int | None:
    """Return None for the menu, 1..4 for a query; reject other grammar."""
    if not isinstance(text, str):
        raise ValueError(USAGE)
    words = text.strip().casefold().split()
    if words == ["rank"]:
        return None
    if len(words) == 2 and words[0] == "rank" and words[1] in {"1", "2", "3", "4"}:
        return int(words[1])
    raise ValueError(USAGE)


def _metric(stock: RankedStock, number: int) -> str:
    if not stock.value:
        return "미제공"
    if number == 1:
        return f"{Decimal(stock.value) / Decimal(100):,f}억원"
    if number == 2:
        return stock.value + "%"
    if number == 3:
        return f"{int(stock.value):,}주"
    value = int(stock.value)
    return f"제공 순위 {value}위" if value > 0 else "제공 순위값 0"


def _change(stock: RankedStock) -> str:
    if stock.rank_change:
        value = int(stock.rank_change)
        if value == 0:
            return "변동 없음"
        if stock.rank_change.startswith(("+", "-")):
            return stock.rank_change
        return (stock.rank_change_sign if stock.rank_change_sign in {"+", "-"} else "") + stock.rank_change
    if stock.rank_change_sign in {"N", "'"}:
        return "변동 없음"
    return "미제공"


def format_ranking(stocks: list[RankedStock], number: int, mode: str) -> list[str]:
    label = RANK_LABELS[number]
    environment = {"paper": "모의투자", "real": "실전투자"}[mode]
    scope = "실시간 종목조회 순위 · 1분 기준" if number == 4 else "국내 전체 · 통합거래소 · 관리종목 포함"
    queried = datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y-%m-%d %H:%M:%S")
    blocks = [f"📊 {label} 상위 {len(stocks)}종목 · {environment}\n{scope}\n조회 시각(KST): {queried}"]
    if not stocks:
        blocks.append("조회 결과가 없습니다.")
    elif len(stocks) < 20:
        blocks.append(f"서버에서 {len(stocks)}종목을 제공했습니다.")
    for index, stock in enumerate(stocks, 1):
        line = f"{index}. {stock.name or '종목명 미제공'} ({stock.code}) · {_metric(stock, number)}"
        if number == 1 and stock.provider_rank and int(stock.provider_rank) > 0:
            line += f" · 제공 순위 {int(stock.provider_rank)}위"
        if number == 4:
            line += f" · 순위 변동 {_change(stock)}"
            if stock.basis_at:
                line += f"\n기준 일시: {stock.basis_at}"
        blocks.append(line)
    return _chunks(blocks)


def handle_rank(text: str, session) -> list[str]:
    try:
        number = parse_rank_command(text)
    except ValueError:
        return [USAGE]
    if number is None:
        return [MENU]
    try:
        stocks = fetch_ranked_stocks(session, number, limit=20)
        return format_ranking(stocks, number, session.mode)
    except RankingsError as error:
        return [str(error)]
    except Exception:
        return ["순위를 표시하지 못했습니다. 로그인 상태를 확인한 뒤 다시 시도하세요."]
