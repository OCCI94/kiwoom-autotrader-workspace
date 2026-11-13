"""Attribute only program-originated orders to an explicit strategy context."""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace

from kiwoom_autotrader.trading.decision_evidence import freeze_evidence, UNSPECIFIED_PRICE


@dataclass(frozen=True)
class AutomaticOrderContext:
    tracker: object
    strategy: str
    evidence: object = None


_current = ContextVar('automatic_order_context', default=None)


@contextmanager
def automated_orders(tracker, strategy, *, evidence=None):
    if tracker is None or not isinstance(strategy, str) or not strategy:
        raise ValueError('자동주문 원장과 전략 이름이 필요합니다.')
    token = _current.set(AutomaticOrderContext(tracker, strategy, freeze_evidence(evidence)))
    try:
        yield
    finally:
        _current.reset(token)


@contextmanager
def order_decision(evidence):
    """Attach a frozen decision only to the surrounding automatic order scope."""
    frozen = freeze_evidence(evidence)
    context = _current.get()
    if context is None:
        yield
        return
    if frozen is not None and frozen.as_dict()["strategy"] not in (None, context.strategy):
        raise ValueError("자동주문 전략과 판단 근거가 일치하지 않습니다.")
    token = _current.set(replace(context, evidence=frozen))
    try:
        yield
    finally:
        _current.reset(token)


def prepare_tracking(mode, side, symbol, quantity, *, price=UNSPECIFIED_PRICE, require_no_pending=False,
                     reconciliation_evidence=None):
    context = _current.get()
    if context is None:
        return None
    options = {}
    if context.evidence is not None:
        options["evidence"] = context.evidence
    if price is not UNSPECIFIED_PRICE:
        options["price"] = price
    if reconciliation_evidence is not None:
        options["reconciliation_evidence"] = reconciliation_evidence
    if require_no_pending is True:
        options["require_no_pending"] = True
    elif require_no_pending is not False:
        raise ValueError("자동주문 미결 확인 옵션은 참 또는 거짓이어야 합니다.")
    intent = context.tracker.prepare_order(mode, side, symbol, quantity, context.strategy, **options)
    return context, intent


def complete_tracking(tracking, receipt):
    if tracking is not None:
        context, intent = tracking
        context.tracker.register_order(receipt, context.strategy, intent_id=intent)


def cancel_tracking(tracking, reason):
    if tracking is not None:
        context, intent = tracking
        context.tracker.cancel_intent(intent, reason)
