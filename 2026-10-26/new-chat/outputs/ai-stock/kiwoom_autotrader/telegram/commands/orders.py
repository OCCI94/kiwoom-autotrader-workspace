"""Explicit buy/sell commands, with no inference from collected messages."""

from __future__ import annotations

from dataclasses import dataclass
import re
import shlex
import hashlib
import json
from datetime import datetime
import uuid

from kiwoom_autotrader.api.orders import (
    OrderError, OrderReceipt, normalize_order_code, normalize_positive_integer, submit_order,
)
from kiwoom_autotrader.api.session import BrokerSession, IssuedToken, LoginSessionError
from kiwoom_autotrader.api.stock_info import fetch_stock_info
from kiwoom_autotrader.api.order_book import fetch_budget_limit
from kiwoom_autotrader.api.errors import BrokerAPIError
from kiwoom_autotrader.api.stock_names import StockNameError, resolve_stock_name
from kiwoom_autotrader.trading.paper_policy import limit_automatic_buy_budget, PaperAutomationPolicyError
from kiwoom_autotrader.trading.order_context import _current, automated_orders


MANUAL_BUY_CAP_KRW = 1_000_000


BUY_USAGE = ("사용법: buy {종목코드 또는 종목명} {수량} [지정가]\n"
             "예: buy 005930 1 / buy 삼성전자 1 70000\n"
             "공백이 포함된 종목명은 따옴표로 묶으세요: buy \"삼성 전자\" 1\n"
             "수량·지정가는 12자리 이내 양의 정수이며 지정가를 생략하면 시장가입니다.\n"
             "예산 매수: buy {6자리 종목코드} max {금액}\n"
             "예: buy 005930 max 100000\n"
             "금액은 12자리 이내 양의 정수(원)입니다. 조회 현재가 이하의 확인된 가격을 지정가로 제출하며, "
             "수수료를 제외한 매수대금만 예산 이내로 제한합니다. 미체결될 수 있습니다.")
SELL_USAGE = ("사용법: sell {6자리 종목코드} {수량}\n예: sell 005930 1\n"
              "수량은 12자리 이내 양의 정수이며 KRX 시장가 매도입니다.")
_UNKNOWN_RESULT = ("주문 처리 결과를 확인하지 못했습니다. 자동 재전송하지 않았습니다. "
                   "증권사 주문내역을 확인한 뒤 다음 주문을 결정하세요.")


@dataclass(frozen=True)
class ParsedOrder:
    side: str
    identifier: str
    quantity: str | None
    price: str | None = None
    budget: str | None = None


class OrderReplies(list):
    """List-compatible replies carrying an explicit ambiguous-order flag."""
    def __init__(self, replies, *, uncertain=False, skipped_reason=None, submitted=None, decision_reason=None):
        super().__init__(replies)
        self.uncertain = uncertain
        self.skipped_reason = skipped_reason
        # skipped_reason keeps its existing quiet-no-send contract. Detailed
        # audit reasons must not turn ordinary error feedback into a quiet skip.
        self.decision_reason = decision_reason if decision_reason is not None else skipped_reason
        # Receipt metadata, never inferred from a translated reply string.
        self.submitted = submitted


class BudgetBuyError(ValueError):
    """A local, safe failure before a budget order has been submitted."""


class BudgetTooSmallError(BudgetBuyError):
    """A verified quote costs more than the entire order budget."""


@dataclass(frozen=True)
class BudgetBuyPlan:
    budget: int
    price: int
    quantity: int
    gross_total: int
    unused_budget: int
    quote_price: int | None = None


def plan_budget_buy(budget: str | int, current_price: str) -> BudgetBuyPlan:
    """Cap principal with exact integer arithmetic and a quote-priced limit.

    ka10001's leading +/- indicates price movement. A fractional quote cannot
    safely become an integer KRX limit price, so it is rejected, never rounded.
    Fees are outside this principal-only cap. No market order is planned here.
    """
    amount = int(normalize_positive_integer(budget))
    if (not isinstance(current_price, str)
            or re.fullmatch(r"[+-]?[0-9]{1,12}", current_price.strip()) is None):
        raise BudgetBuyError("현재가가 유효한 12자리 이내 정수 원화 가격이 아닙니다. 주문을 전송하지 않았습니다.")
    price = abs(int(current_price.strip()))
    if price <= 0:
        raise BudgetBuyError("현재가가 0원 이하이므로 주문을 전송하지 않았습니다.")
    quantity = amount // price
    if quantity == 0:
        raise BudgetTooSmallError("예산으로 1주도 매수할 수 없어 주문을 전송하지 않았습니다.")
    # Retain the TR bounds explicitly even if today's budget bound implies them.
    normalize_positive_integer(price)
    normalize_positive_integer(quantity)
    total = quantity * price
    return BudgetBuyPlan(amount, price, quantity, total, amount - total)


def _on_common_krx_grid(price: int) -> bool:
    """A conservative common grid, not a guess at the security's actual tick.

    KRX stock ticks by price plus a 5-KRW minimum also fit ETF/ETN/ELW grids.
    Other prices need an actual displayed KRX book price, not numeric rounding.
    Source: https://regulation.krx.co.kr/contents/RGL/03/03010100/RGL03010100T3.jsp
    """
    for ceiling, stock_tick in ((2000, 1), (5000, 5), (20000, 10), (50000, 50),
                                (200000, 100), (500000, 500)):
        if price < ceiling:
            return price % max(5, stock_tick) == 0
    return price % 1000 == 0


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
    if words[2].casefold() == "max":
        try:
            if len(words) != 4:
                raise ValueError
            symbol = normalize_order_code(words[1])
            budget = normalize_positive_integer(words[3])
        except ValueError:
            raise ValueError(BUY_USAGE) from None
        return ParsedOrder("buy", symbol, None, budget=budget)
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


def _budget_session_identity(session) -> tuple[str, str]:
    """Read existing authentication only; never log in, renew or load secrets."""
    if not isinstance(session, BrokerSession) or not session.authenticated:
        raise BudgetBuyError("먼저 login paper 또는 login real로 로그인하세요. 주문을 전송하지 않았습니다.")
    mode, token = session.mode, session.token
    if mode not in ("paper", "real"):
        raise BudgetBuyError("투자 구분을 확인하지 못했습니다. 다시 로그인하세요. 주문을 전송하지 않았습니다.")
    try:
        IssuedToken(token)
    except LoginSessionError:
        raise BudgetBuyError("유효한 로그인 토큰이 없습니다. 주문을 전송하지 않았습니다.") from None
    if not session.token_usable:
        raise BudgetBuyError("로그인 토큰이 만료되었습니다. 다시 로그인하세요. 주문을 전송하지 않았습니다.")
    return mode, token


def _format_budget_receipt(receipt: OrderReceipt, plan: BudgetBuyPlan) -> str:
    quote = plan.quote_price if plan.quote_price is not None else plan.price
    prices = (f"조회가 {quote:,}원 · 실제 지정가 {plan.price:,}원" if quote != plan.price else
              f"조회가/지정가 {plan.price:,}원")
    explanation = ("원래 조회 현재가로 수량을 계산하고, 현재가 이하의 표시된 KRX 호가를 지정가로 제출했습니다."
                   if quote != plan.price else "조회한 현재가를 지정가로 제출했습니다.")
    return (_format_receipt(receipt) + "\n"
            f"예산 {plan.budget:,}원 · {prices}\n"
            f"수량 {plan.quantity:,}주 · 최대 매수대금 {plan.gross_total:,}원\n"
            f"미사용 예산 {plan.unused_budget:,}원\n"
            f"{explanation} 미체결될 수 있습니다.\n"
            "수수료는 예산·매수대금 계산에서 제외되며, 총 출금액은 수수료만큼 늘어날 수 있습니다.")


def _check_buy_guard(before_send) -> None:
    """Reject changed strategy conditions before work without exposing details."""
    if before_send is not None:
        try:
            before_send()
        except Exception as error:
            failure = BudgetBuyError("자동매수 실행 조건이 변경되어 주문을 전송하지 않았습니다.")
            code = getattr(error, "skipped_reason", None)
            failure.skipped_reason = code if code in ("reentry_cooldown", "context_changed") else "buy_guard_changed"
            raise failure from None


def _handle_budget_buy(parsed: ParsedOrder, session, *, before_send=None) -> list[str]:
    budget_note = ""
    if _current.get() is None and int(parsed.budget) > MANUAL_BUY_CAP_KRW:
        return OrderReplies(["수동 매수 예산이 100만원 한도를 초과해 전송하지 않았습니다."], submitted=False)
    try:
        identity = _budget_session_identity(session)
        _check_buy_guard(before_send)
        # Automatic paper buys are sized against a freshly checked per-symbol
        # remaining principal. Manual commands do no account lookup here.
        limited = limit_automatic_buy_budget(session, parsed.identifier, parsed.budget)
        if _budget_session_identity(session) != identity:
            raise BudgetBuyError("잔여 한도 확인 중 로그인 투자 구분 또는 토큰이 변경되었습니다. 주문을 전송하지 않았습니다.")
        _check_buy_guard(before_send)
        if _budget_session_identity(session) != identity:
            raise BudgetBuyError("잔여 한도 확인 후 로그인 상태가 변경되었습니다. 주문을 전송하지 않았습니다.")
        if type(limited) is int:
            adjusted = limited
        elif isinstance(limited, str) and re.fullmatch(r"[0-9]{1,12}", limited):
            adjusted = int(limited)
        else:
            raise BudgetBuyError("잔여 매입 한도 결과를 확인하지 못했습니다. 주문을 전송하지 않았습니다.")
        original = int(parsed.budget)
        if not 0 <= adjusted <= original:
            raise BudgetBuyError("잔여 매입 한도 결과가 요청 범위를 벗어났습니다. 주문을 전송하지 않았습니다.")
        if adjusted == 0:
            if _current.get() is not None and session.mode == "paper":
                return OrderReplies([], skipped_reason="symbol_buy_limit_exhausted")
            raise BudgetBuyError("종목별 잔여 매입 한도가 없어 자동매수를 보류했습니다. 주문을 전송하지 않았습니다.")
        if adjusted < original:
            budget_note = f"안전 한도에 맞춰 요청 예산 {original:,}원 → 적용 예산 {adjusted:,}원으로 조정했습니다.\n"
        data = fetch_stock_info(session, parsed.identifier)
    except PaperAutomationPolicyError as error:
        reason = "insufficient_cash" if getattr(error, "reason_code", None) == "insufficient_cash" else "reconciliation_unavailable"
        return OrderReplies(["종목별 잔여 매입 한도를 확인하지 못해 자동매수를 보류했습니다. 주문을 전송하지 않았습니다."], decision_reason=reason, submitted=False)
    except BudgetBuyError as error:
        return OrderReplies([str(error)], decision_reason=getattr(error, "skipped_reason", "buy_guard_changed"), submitted=False)
    except BrokerAPIError as error:
        return OrderReplies([str(error)] if error.ip_related else ["현재가 조회에 실패했습니다. 주문을 전송하지 않았습니다."], decision_reason="quote_unavailable", submitted=False)
    except Exception:
        # Even provider failures are replaced here: quote errors occur before
        # any order, so an ambiguous-order warning would be misleading.
        return OrderReplies(["현재가 조회에 실패했습니다. 주문을 전송하지 않았습니다."], decision_reason="quote_unavailable", submitted=False)
    try:
        if not isinstance(data, dict) or data.get("stk_cd") != parsed.identifier:
            raise BudgetBuyError("현재가 응답의 종목코드를 확인하지 못했습니다. 주문을 전송하지 않았습니다.")
        if _budget_session_identity(session) != identity:
            raise BudgetBuyError("현재가 조회 중 로그인 투자 구분 또는 토큰이 변경되었습니다. 주문을 전송하지 않았습니다.")
        _check_buy_guard(before_send)
        if _budget_session_identity(session) != identity:
            raise BudgetBuyError("현재가 조회 후 로그인 상태가 변경되었습니다. 주문을 전송하지 않았습니다.")
        plan = plan_budget_buy(adjusted, data.get("cur_prc"))
    except BudgetTooSmallError as error:
        if _current.get() is not None and session.mode == "paper":
            return OrderReplies([], skipped_reason="budget_below_one_share")
        return OrderReplies([budget_note + str(error)], decision_reason="budget_below_one_share", submitted=False)
    except BudgetBuyError as error:
        return OrderReplies([budget_note + str(error)], decision_reason=getattr(error, "skipped_reason", "quote_unavailable"), submitted=False)
    except BrokerAPIError as error:
        return OrderReplies([str(error)] if error.ip_related else ["현재가 조회에 실패했습니다. 주문을 전송하지 않았습니다."], decision_reason="quote_unavailable", submitted=False)
    except Exception:
        return OrderReplies(["현재가 또는 로그인 상태를 확인하지 못했습니다. 주문을 전송하지 않았습니다."], decision_reason="quote_unavailable", submitted=False)
    if not _on_common_krx_grid(plan.price):
        try:
            # The check after the quote also guards entry to this extra lookup.
            options = {"before_send": before_send} if before_send is not None else {}
            limit = fetch_budget_limit(session, parsed.identifier, plan.price, **options)
            if (type(limit) is not int or not 0 < limit <= plan.price
                    or limit > 999999999999):
                raise BudgetBuyError("현재가 이하의 유효한 KRX 지정가를 확인하지 못했습니다. 주문을 전송하지 않았습니다.")
            if _budget_session_identity(session) != identity:
                raise BudgetBuyError("호가 조회 중 로그인 투자 구분 또는 토큰이 변경되었습니다. 주문을 전송하지 않았습니다.")
            _check_buy_guard(before_send)
            if _budget_session_identity(session) != identity:
                raise BudgetBuyError("호가 조회 후 로그인 상태가 변경되었습니다. 주문을 전송하지 않았습니다.")
            # Keep the original quote-based quantity: a cheaper displayed limit
            # must not increase exposure or force a previously unaffordable share.
            total = plan.quantity * limit
            plan = BudgetBuyPlan(plan.budget, limit, plan.quantity, total,
                                 plan.budget - total, quote_price=plan.price)
        except BudgetBuyError as error:
            return OrderReplies([str(error)], decision_reason=getattr(error, "skipped_reason", "order_book_unavailable"), submitted=False)
        except BrokerAPIError as error:
            return OrderReplies([str(error)] if error.ip_related else
                    ["KRX 호가 확인에 실패했습니다. 주문을 전송하지 않았습니다."], decision_reason="order_book_unavailable", submitted=False)
        except Exception:
            return OrderReplies(["KRX 호가 확인에 실패했습니다. 주문을 전송하지 않았습니다."], decision_reason="order_book_unavailable", submitted=False)
    try:
        options = {"before_send": before_send} if before_send is not None else {}
        receipt = submit_order(session, "buy", parsed.identifier, str(plan.quantity), str(plan.price), **options)
        return OrderReplies([budget_note + _format_budget_receipt(receipt, plan)], submitted=True)
    except OrderError as error:
        code = getattr(error, "reason_code", None)
        reason = code if code in {"insufficient_cash", "daily_entry_limit_reached", "daily_entry_limit_unavailable",
                                 "concurrent_position_limit_reached", "concurrent_position_limit_unavailable"} else "order_not_submitted"
        return OrderReplies([str(error)], uncertain=error.uncertain,
                            decision_reason=None if error.uncertain else reason, submitted=None if error.uncertain else False)
    except Exception:
        return OrderReplies([_UNKNOWN_RESULT], uncertain=True)


def _handle(text: str, session, side: str, *, before_send=None) -> list[str]:
    usage = BUY_USAGE if side == "buy" else SELL_USAGE
    try:
        parsed = parse_buy_command(text) if side == "buy" else parse_sell_command(text)
    except ValueError:
        return [usage]
    if parsed is None:
        return [usage]
    if parsed.budget is not None:
        return _handle_budget_buy(parsed, session, before_send=before_send)
    if side == "buy":
        try:
            _check_buy_guard(before_send)
        except BudgetBuyError as error:
            return OrderReplies([str(error)])
    try:
        symbol = parsed.identifier
        if re.fullmatch(r"[0-9]{6}", symbol) is None:
            symbol = resolve_stock_name(session, symbol)
        symbol = normalize_order_code(symbol)
        if side == "buy" and _current.get() is None:
            if parsed.price is not None:
                bound = int(parsed.price)
            else:
                # A current quote cannot cap a market fill. Use the actual
                # exchange upper limit, or reject an unbounded market order.
                identity = _budget_session_identity(session)
                try:
                    data = fetch_stock_info(session, symbol)
                except Exception:
                    return OrderReplies(["시장가 매수의 상한가격 조회에 실패해 전송하지 않았습니다. 지정가를 입력하세요."], submitted=False)
                if (not isinstance(data, dict) or data.get("stk_cd") != symbol
                        or _budget_session_identity(session) != identity
                        or re.fullmatch(r'\+?[0-9]{1,12}', str(data.get("upl_pric", ""))) is None):
                    return OrderReplies(["시장가 매수의 상한가격을 확인하지 못했습니다. 지정가를 입력하세요."], submitted=False)
                bound = int(data["upl_pric"])
            if bound <= 0 or int(parsed.quantity) * bound > MANUAL_BUY_CAP_KRW:
                return OrderReplies(["수동 매수의 최대 원금이 100만원을 초과해 전송하지 않았습니다."], submitted=False)
        options = {"before_send": before_send} if before_send is not None else {}
        receipt = submit_order(session, parsed.side, symbol, parsed.quantity, parsed.price, **options)
        return [_format_receipt(receipt)]
    except (StockNameError, OrderError) as error:
        return OrderReplies([str(error)], uncertain=isinstance(error, OrderError) and error.uncertain)
    except ValueError:
        return [usage]
    except Exception:
        return OrderReplies([_UNKNOWN_RESULT], uncertain=True)


def handle_buy(text: str, session, *, before_send=None) -> list[str]:
    return _handle(text, session, "buy", before_send=before_send)


def _manual_protective_sell(text, session, *, before_send=None):
    """Explicit paired-user command. Never called by reservations/rank batches."""
    words=text.strip().casefold().split()
    if len(words)!=3 or words[:2]!=['sell','protect'] or re.fullmatch(r'[0-9]{6}',words[2]) is None:
        return ['사용법: sell protect {6자리 종목코드} — 최신 브로커 확인 매도가능 수량만 시장가 청산']
    if getattr(session,'mode',None)!='paper' or _current.get() is not None:
        return ['보호 청산 명령은 모의 계좌의 명시적 사용자 명령에서만 허용합니다. 자동·예약 호출은 거부합니다.']
    tracker=getattr(getattr(session,'paper_automation_reconcile',None),'_tracker',None)
    if tracker is None:return ['모의 원장 연결이 없어 보호 청산을 전송하지 않았습니다.']
    attempted=False
    try:
        from kiwoom_autotrader.api.account_report import fetch_account_report
        from kiwoom_autotrader.api.session import KST
        from kiwoom_autotrader.trading.paper_reconciliation import _validate_holdings
        from kiwoom_autotrader.api.minute_chart import session_identity
        symbol=words[2];identity=session_identity(session)
        if before_send is not None:before_send()
        report=fetch_account_report(session)
        if session_identity(session)!=identity:raise ValueError('session_changed')
        _,sellable,_,_=_validate_holdings(report);quantity=sellable.get(symbol)
        if type(quantity) is not int or quantity<=0:
            return ['최신 브로커 확인 매도가능 수량이 0 또는 미확정이므로 전송하지 않았습니다.']
        now=datetime.now(KST)
        tracker._holdings('paper',report,now,source_kind='broker_observation')
        tracker._save_paper_balance_snapshot('paper',report,now)
        tracker.assert_no_pending_order('paper',symbol)
        evidence={'schema_version':1,'strategy':'manual_protective_exit','strategy_version':'manual-protective-v1',
            'run_id':uuid.uuid4().hex,'observed_at':now.isoformat(),'evaluated_at':now.isoformat(),
            'source_kind':'broker_observation','source_provider':'kiwoom','source_exchange':'KRX',
            'source_api':'kt00018','source_sha256':hashlib.sha256(json.dumps(report,sort_keys=True,ensure_ascii=True).encode()).hexdigest(),
            'config':{'ownership_status':'exit_only_broker_confirmed','profit_attribution':'unconfirmed',
                'manual_protective_exit':True,'manual_confirmed_quantity':quantity}}
        def guard():
            if before_send is not None:before_send()
            if session_identity(session)!=identity:raise ValueError('session_changed')
        # The ordinary final paper reconciliation checks account sellable qty,
        # complete pending orders, session/00 epochs and local reservation again.
        with automated_orders(tracker,'manual_protective_exit',evidence=evidence):
            attempted=True
            receipt=submit_order(session,'sell',symbol,quantity,None,before_send=guard)
        return OrderReplies([_format_receipt(receipt)+'\n수동 보호 청산으로 원장에 기록했습니다. 전략 수익 귀속은 미확정입니다.'],submitted=True)
    except OrderError as error:
        return OrderReplies([str(error)],uncertain=error.uncertain,submitted=None if error.uncertain else False)
    except Exception:
        if attempted:return OrderReplies([_UNKNOWN_RESULT],uncertain=True,submitted=None)
        return OrderReplies(['브로커 수량·미체결·원장 대사를 확인하지 못해 보호 청산을 보류했습니다. 불확실 주문은 재전송하지 않습니다.'],submitted=False)


def handle_sell(text: str, session, *, before_send=None) -> list[str]:
    if text.strip().casefold().split()[:2]==['sell','protect']:
        return _manual_protective_sell(text,session,before_send=before_send)
    return _handle(text, session, "sell", before_send=before_send)
