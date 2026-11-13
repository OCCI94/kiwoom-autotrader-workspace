"""Mandatory paper automation limits; the optional real-mode ban is separate.

The automatic context is captured on the calling thread. Queue workers do not
inherit ContextVars, so their final send check uses this captured guard instead
of attempting to rediscover the context. The cap is principal, excluding fees.
"""
from __future__ import annotations

import re

from kiwoom_autotrader.config.settings import (
    load_settings, effective_paper_experiment, PAPER_EXPERIMENT_STRATEGIES,
)
from kiwoom_autotrader.trading.order_context import _current


MAX_BUY_PRINCIPAL = 3_000_000
DEFAULT_BUY_PRINCIPAL = 1_000_000
MAX_SYMBOL_BUY_PRINCIPAL = 3_000_000
DEFAULT_SYMBOL_BUY_PRINCIPAL = 1_000_000


def _multi_enabled(settings):
    value = settings.get("paper_multi_strategy_enabled", False)
    if type(value) is not bool:
        raise PaperAutomationPolicyError("다중 전략 안전 설정을 확인하지 못했습니다.")
    return value


def _symbol_cap(settings):
    value = settings.get("paper_automation_symbol_cap", DEFAULT_SYMBOL_BUY_PRINCIPAL)
    if type(value) is not int or not 1 <= value <= MAX_SYMBOL_BUY_PRINCIPAL:
        raise PaperAutomationPolicyError("모의 자동매매 종목당 한도를 확인하지 못했습니다.")
    return min(value, DEFAULT_SYMBOL_BUY_PRINCIPAL) if _multi_enabled(settings) else value


def _buy_cap(settings):
    value = settings.get("paper_automation_buy_cap", DEFAULT_BUY_PRINCIPAL)
    if type(value) is not int or not 1 <= value <= MAX_BUY_PRINCIPAL:
        raise PaperAutomationPolicyError("모의 자동매매 매수당 한도를 확인하지 못했습니다.")
    return min(value, DEFAULT_BUY_PRINCIPAL) if _multi_enabled(settings) else value


ALLOWED_ORDER_REASON_CODES = frozenset({
    "insufficient_cash",
    "daily_entry_limit_reached",
    "daily_entry_limit_unavailable",
    "concurrent_position_limit_reached",
    "concurrent_position_limit_unavailable",
    "theme_concentration_limit_reached",
    "theme_concentration_unavailable",
    "reconciliation_proof_expired",
    "reconciliation_state_changed",
    "reconciliation_failed",
    "broker_pending_order_exists",
    "local_pending_order_exists",
    "symbol_principal_limit_reached",
    "insufficient_sellable_qty",
    "paper_policy_rejected",
    "session_unavailable",
    "session_changed",
    "maintenance_paused",
    "external_storage_unavailable",
    "stream_unverified",
    "ledger_prepare_failed",
    "multi_exit_dispatch_rejected",
    "multi_buy_dispatch_rejected",
    "b3_dispatch_rejected",
    "dynamic_s1_admission_changed",
    "timed_exit_window_expired",
    "deadline_exceeded",
    "paper_session_changed",
    "experiment_profile_changed",
    "official_session_unavailable",
    "signal_window_expired",
    "exp_d_exit_session_changed",
    "exp_d_exit_ownership_changed",
    "extended_late_exit_session_changed",
    "extended_late_exit_ownership_changed",
    "intraday_entry_cutoff",
    "s5_risk_evidence_changed",
    "hot_policy_rejected",
    "pre_send_guard_rejected",
    "broker_order_rejected",
})


class PaperAutomationPolicyError(ValueError):
    """A local, known pre-send rejection without provider details or secrets."""

    def __init__(self, message, *, reason_code=None):
        super().__init__(message)
        self.reason_code = reason_code if reason_code in ALLOWED_ORDER_REASON_CODES else None


def experiment_order_sizing(settings, mode, *, available_cash_krw, price,
                            existing_principal_krw=0, pending_principal_krw=0):
    """Pure paper ticket sizing; caller supplies freshly reconciled cash.

    A single share above the normal budget is allowed only for a flat symbol
    with no reserved buy principal and enough confirmed cash. This exception
    cannot accumulate a second share above the nominal symbol cap. No balance
    or price is inferred, and real mode returns before reading settings/inputs.
    """
    profile = effective_paper_experiment(settings, mode)
    if not profile["enabled"]:
        return {"enabled": False}
    values = (available_cash_krw, existing_principal_krw, pending_principal_krw)
    if any(type(v) is not int or v < 0 for v in values) or type(price) is not int or price <= 0:
        raise PaperAutomationPolicyError("실측 주문가능현금·종목 원금·지정가를 확인하지 못했습니다.")
    budget = min(profile["per_buy_budget_ceiling_krw"], available_cash_krw // profile["cash_budget_divisor"])
    nominal_cap = budget * profile["symbol_budget_multiplier"]
    remaining = max(0, nominal_cap - existing_principal_krw - pending_principal_krw)
    quantity = min(budget, remaining, available_cash_krw) // price
    exception = (quantity == 0 and price > budget and available_cash_krw >= price
                 and existing_principal_krw == 0 and pending_principal_krw == 0)
    if exception:
        quantity = 1
    cap = max(nominal_cap, price) if exception else nominal_cap
    reason = None if quantity else ("insufficient_cash" if available_cash_krw < price else "symbol_budget_exhausted")
    return {"enabled": True, "profile": profile["profile"],
            "available_cash_krw": available_cash_krw, "budget_krw": budget,
            "nominal_symbol_cap_krw": nominal_cap, "effective_symbol_cap_krw": cap,
            "existing_principal_krw": existing_principal_krw,
            "pending_principal_krw": pending_principal_krw, "price_krw": price,
            "quantity": quantity, "principal_krw": quantity * price,
            "one_share_exception": exception, "reason": reason}


def validate_experiment_order_sizing(evidence, *, quantity, price):
    """Verify exact sizing proof supplied by the live account reconciler.

    Freshness, session identity, actual account cash and ledger watermark are
    owned by the surrounding reconciliation schema; this validates arithmetic.
    """
    if not isinstance(evidence, dict):
        raise PaperAutomationPolicyError("모의 실험 매수 예산 증빙이 없습니다.")
    expected = experiment_order_sizing({"paper_experiment_enabled": True}, "paper",
        available_cash_krw=evidence.get("available_cash_krw"), price=_positive_integer(price),
        existing_principal_krw=evidence.get("existing_principal_krw"),
        pending_principal_krw=evidence.get("pending_principal_krw"))
    if (evidence != expected or any(type(evidence[k]) is not type(v) for k,v in expected.items())
            or _positive_integer(quantity) > expected["quantity"]):
        raise PaperAutomationPolicyError("모의 실험 매수 수량·현금·예약 원금 증빙이 일치하지 않습니다.")
    return expected


def _positive_integer(value):
    if type(value) is int:
        value = str(value)
    if not isinstance(value, str) or re.fullmatch(r"[0-9]{1,12}", value) is None or int(value) <= 0:
        raise PaperAutomationPolicyError("자동주문의 수량·지정가를 확인하지 못해 주문을 전송하지 않았습니다.")
    return int(value)


class _AutomaticPaperGuard:
    def __init__(self, context, session, side, symbol, quantity, price):
        self._tracker = context.tracker
        self._strategy = context.strategy
        self._decision = context.evidence.as_dict() if context.evidence is not None else None
        self._session = session
        self._side, self._symbol, self._quantity, self._price = side, symbol, quantity, price
        self._pending_checked = False
        self._reconciliation_guard = None
        self.reconciliation_evidence = None
        self._initial_mode = session.mode
        self._initial_policy = None
        self._initial_cap = None
        self._initial_symbol_cap = None
        self._initial_multi = None
        self._initial_experiment = None
        self._initial_s5_experiment = None

    def _policy_enabled(self):
        if self._session.mode=='real' and self._side=='sell':
            raise PaperAutomationPolicyError('실전 자동매도 어댑터가 미검증되어 보호 청산이 꺼져 있습니다. 수동 매도가 필요합니다.')
        experiment_arm = self._strategy in PAPER_EXPERIMENT_STRATEGIES
        if experiment_arm and self._session.mode != "paper":
            raise PaperAutomationPolicyError("실험 전략은 모의투자에서만 허용합니다.")
        try:
            settings = load_settings(mode="real") if self._session.mode == "real" else load_settings()
        except Exception:
            raise PaperAutomationPolicyError("모의 자동매매 안전 설정을 읽지 못해 주문을 전송하지 않았습니다.") from None
        if not isinstance(settings, dict) or type(settings.get("paper_automation_only", False)) is not bool:
            raise PaperAutomationPolicyError("모의 자동매매 안전 설정의 형식을 확인하세요. 주문을 전송하지 않았습니다.")
        paper_only = settings.get("paper_automation_only", False)
        experiment = effective_paper_experiment(settings, self._session.mode)["enabled"]
        if self._session.mode=='paper' and not experiment and self._strategy in (
                'gdcrs','ddcrs','S2_surge_high_breakout','S3_upper_limit_continuation',
                'S4_theme_and_catalyst_leader','S5_steady_uptrend_vwap'):
            from .legacy_resume_gate import session_gate
            try:
                gate=session_gate(self._session)
                if gate.status(settings)['confirmation_required']:
                    raise ValueError('explicit_legacy_resume_required')
            except Exception:
                raise PaperAutomationPolicyError('모의 실험 해제 후 레거시 자동주문 재개 확인이 필요합니다. stts를 확인하세요.') from None
        native_s5 = (self._strategy == 'S5_steady_uptrend_vwap'
                     and isinstance(self._decision, dict)
                     and self._decision.get('strategy_version') == 'paper-experiment-v1')
        if native_s5:
            s5_enabled = effective_paper_experiment(settings,self._session.mode).get('s5_enabled',False)
            if self._side=='buy' and not s5_enabled:
                raise PaperAutomationPolicyError('S5 모의 실험 신규 진입이 비활성화되어 있습니다.')
            if self._initial_s5_experiment is None:self._initial_s5_experiment=s5_enabled
            elif self._initial_s5_experiment != s5_enabled:
                raise PaperAutomationPolicyError('대기 중 S5 모의 실험 정책이 변경되었습니다.')
        if experiment_arm and not experiment:
            raise PaperAutomationPolicyError("모의 실험 모드가 비활성화되어 실험 주문을 보류합니다.")
        if self._initial_experiment is None:
            self._initial_experiment = experiment
        elif self._initial_experiment != experiment:
            raise PaperAutomationPolicyError("대기 중 모의 실험 모드가 변경되어 주문을 전송하지 않았습니다.")
        multi = _multi_enabled(settings) or experiment
        staged=self._strategy in ('S2_surge_high_breakout','S3_upper_limit_continuation','S4_theme_and_catalyst_leader','S5_steady_uptrend_vwap')
        if staged and (not multi or self._session.mode!='paper'):
            raise PaperAutomationPolicyError('추가 전략은 활성화된 모의투자에서만 허용합니다.')
        if self._initial_multi is None:
            self._initial_multi = multi
        elif multi != self._initial_multi:
            raise PaperAutomationPolicyError("대기 중 다중 전략 정책이 변경되어 주문을 전송하지 않았습니다.")
        if self._initial_policy is None:
            self._initial_policy = paper_only
        elif paper_only != self._initial_policy:
            raise PaperAutomationPolicyError("대기 중 모의 자동매매 정책이 변경되어 주문을 전송하지 않았습니다.")
        if self._session.mode != self._initial_mode:
            raise PaperAutomationPolicyError("대기 중 투자 구분이 변경되어 주문을 전송하지 않았습니다.")
        if self._session.mode == "real" and paper_only:
            raise PaperAutomationPolicyError("모의투자 전용 자동매매 정책으로 실전 자동주문을 차단했습니다.")
        if self._session.mode == "real":
            if settings.get("paper_automation_only") is not False:
                raise PaperAutomationPolicyError("실전 자동주문은 명시적인 모의 전용 해제 설정이 필요합니다.")
            if self._side != "buy":
                raise PaperAutomationPolicyError("실전 자동매도 소유권 대사 경로가 미검증되어 전송을 보류합니다. 수동 매도는 별도입니다.")
            from .real_automation_safety import PER_BUY_KRW
            if self._price is None or _positive_integer(self._quantity) * _positive_integer(self._price) > PER_BUY_KRW:
                raise PaperAutomationPolicyError("실전 자동매수는 건당 100만원 이하의 지정가만 허용합니다.")
            return True
        if self._session.mode != "paper":
            raise PaperAutomationPolicyError("자동주문의 투자 구분을 확인하지 못했습니다. 주문을 전송하지 않았습니다.")
        # Paper orders always require reconciliation, including after reset.
        if self._side == "buy":
            if experiment:
                if self._price is None:
                    raise PaperAutomationPolicyError("모의 실험 매수는 실측 지정가만 허용합니다.")
                _positive_integer(self._quantity)
                _positive_integer(self._price)
                return True
            cap = _buy_cap(settings)
            if self._initial_cap is None:
                self._initial_cap = cap
            elif cap != self._initial_cap:
                raise PaperAutomationPolicyError("대기 중 자동매수 원금 한도가 변경되어 주문을 전송하지 않았습니다.")
            symbol_cap = _symbol_cap(settings)
            if self._initial_symbol_cap is None:
                self._initial_symbol_cap = symbol_cap
            elif symbol_cap != self._initial_symbol_cap:
                raise PaperAutomationPolicyError("대기 중 종목당 매입원금 한도가 변경되어 주문을 전송하지 않았습니다.")
            if self._price is None:
                raise PaperAutomationPolicyError("자동매수는 원금 한도를 확인할 수 있는 지정가만 허용합니다. 시장가 주문을 전송하지 않았습니다.")
            principal = _positive_integer(self._quantity) * _positive_integer(self._price)
            if principal > cap:
                raise PaperAutomationPolicyError("자동매수 지정가 원금이 설정된 건당 한도를 초과해 주문을 전송하지 않았습니다. 수수료는 한도와 별도입니다.")
        return True

    def before_prepare(self):
        """Check the prior ledger before creating this order's own intent."""
        if self._policy_enabled():
            if self._session.mode == "real":
                from .real_automation_safety import RealAutomaticBuyGuard
                try:
                    self._reconciliation_guard = RealAutomaticBuyGuard(self._session, self._tracker,
                        self._symbol, self._quantity, self._price, self._strategy).prepare()
                except Exception:
                    raise PaperAutomationPolicyError("실전 자동매수의 잔고·미체결·일일 한도 대사를 확인하지 못했습니다.") from None
                self._pending_checked = True
                return True
            reconcile = getattr(self._session, "paper_automation_reconcile", None)
            if reconcile is None:
                raise PaperAutomationPolicyError("보유·매도가능 수량을 확인할 모의 계좌 대사가 없어 자동주문을 보류합니다.")
            if reconcile is not None:
                try:
                    if not callable(reconcile):
                        raise ValueError
                    options={'side':self._side,'quantity':self._quantity,'price':self._price}
                    if self._initial_experiment:
                        options['strategy'] = self._strategy
                    staged=(self._initial_multi and self._side=='sell' and self._strategy in
                            ('S2_surge_high_breakout','S3_upper_limit_continuation','S4_theme_and_catalyst_leader','S5_steady_uptrend_vwap', *PAPER_EXPERIMENT_STRATEGIES))
                    if staged:
                        cfg=(self._decision or {}).get('config') or {}
                        options.update(strategy=self._strategy,exit_kind=cfg.get('multi_exit_kind'),
                                       generation_id=cfg.get('multi_position_generation_id'))
                        if cfg.get('ownership_status') is not None:
                            options['ownership_status']=cfg['ownership_status']
                    elif self._initial_multi and self._side=='sell' and self._strategy=='ddcrs':
                        options['strategy']='ddcrs'
                    guard = reconcile(self._symbol, **options)
                    if not callable(guard):
                        raise ValueError
                    self._reconciliation_guard = guard
                    captured = getattr(guard, "reconciliation_evidence", None)
                    if self._side == "buy" and not isinstance(captured, dict):
                        raise ValueError("모의 자동매수 대사 증빙이 없습니다")
                    if self._side == "buy" and self._initial_experiment:
                        validate_experiment_order_sizing(captured.get('paper_experiment_sizing'),
                            quantity=self._quantity, price=self._price)
                    if (self._side == "buy" and not self._initial_experiment and (self._initial_cap > DEFAULT_BUY_PRINCIPAL
                            or self._initial_symbol_cap > DEFAULT_SYMBOL_BUY_PRINCIPAL)
                            and captured.get("schema_version") != 2):
                        raise ValueError("상향 한도의 대사 증빙 형식이 잘못되었습니다")
                    if self._side == "buy" and not self._initial_experiment and (captured.get("symbol_cap_krw", DEFAULT_SYMBOL_BUY_PRINCIPAL)
                                                  != self._initial_symbol_cap):
                        raise ValueError("매수 대사와 설정의 종목당 한도가 다릅니다")
                    if self._side == "buy" and not self._initial_experiment and captured.get("schema_version") in (2, 3, 5) and (
                            captured.get("per_buy_cap_krw") != self._initial_cap):
                        raise ValueError("매수 대사와 설정의 건당 한도가 다릅니다")
                    if self._side == "buy" and self._initial_multi and (captured.get("schema_version") not in ((3, 6) if self._initial_experiment else (3, 5)) or captured.get("multi_strategy_enabled") is not True):
                        raise ValueError("다중 전략 원장 대사 증빙이 없습니다")
                    expected_sell_schema=7 if staged and cfg.get('ownership_status')=='exit_only_broker_confirmed' else 4
                    if staged and (not isinstance(captured,dict) or captured.get('schema_version')!=expected_sell_schema):
                        raise ValueError('전략별 매도 대사 증빙이 없습니다')
                    self.reconciliation_evidence = captured if isinstance(captured, dict) else None
                except Exception as error:
                    raise PaperAutomationPolicyError("모의 계좌 대사 또는 종목별 매입 한도를 확인하지 못해 자동주문을 보류합니다.",
                        reason_code=getattr(error, "reason_code", None) or "reconciliation_failed") from None
            checker = getattr(self._tracker, "assert_no_pending_order", None)
            if not callable(checker):
                raise PaperAutomationPolicyError("기존 자동주문의 미체결·접수 상태를 확인할 원장이 없어 새 주문을 보류합니다.",
                                                 reason_code="paper_policy_rejected")
            try:
                checker("paper", self._symbol)
            except Exception:
                raise PaperAutomationPolicyError("기존 자동주문의 미체결·접수 상태를 확인하지 못해 새 주문을 보류합니다.",
                                                 reason_code="local_pending_order_exists") from None
            self._pending_checked = True
            return True
        return False

    def __call__(self):
        # Do not requery the ledger here: it now contains our own prepared intent.
        enabled = self._policy_enabled()
        if self._pending_checked and not enabled:
            raise PaperAutomationPolicyError("대기 중 모의 자동매매 정책이 해제되어 주문을 전송하지 않았습니다.",
                                             reason_code="paper_policy_rejected")
        if enabled:
            if not self._pending_checked:
                raise PaperAutomationPolicyError("대기 중 모의 자동매매 정책이 활성화되어 주문을 전송하지 않았습니다.",
                                                 reason_code="paper_policy_rejected")
            if self._reconciliation_guard is not None:
                try:
                    self._reconciliation_guard()
                except Exception as error:
                    raise PaperAutomationPolicyError("대기 중 모의 계좌 대사 상태가 변경되어 주문을 전송하지 않았습니다.",
                        reason_code=getattr(error, "reason_code", None) or "reconciliation_state_changed") from None


def capture_automatic_paper_guard(session, side, symbol, quantity, price):
    """Return None for manual calls, with no settings or ledger access."""
    context = _current.get()
    if context is None:
        return None
    return _AutomaticPaperGuard(context, session, side, symbol, quantity, price)


def limit_automatic_buy_budget(session, symbol, budget):
    """Bound budget to fresh per-symbol remaining principal for paper automation.

    This is sizing only. The complete reconciliation and send-boundary guards
    run again after the quote. Real automated buys use independent conservative
    limits; manual commands have their own command-level cap. Fees are outside
    principal limits.
    """
    if _current.get() is None:
        return budget
    try:
        settings = load_settings(mode="real") if session.mode == "real" else load_settings()
        if not isinstance(settings, dict) or type(settings.get("paper_automation_only", False)) is not bool:
            raise ValueError
        paper_only = settings.get("paper_automation_only", False)
        if session.mode == "real" and not paper_only:
            from .real_automation_safety import real_buy_budget
            return real_buy_budget(budget)
        if session.mode != "paper":
            raise ValueError
        per_buy = _buy_cap(settings)
        symbol_cap = _symbol_cap(settings)
        multi = _multi_enabled(settings)
        original = _positive_integer(budget)
        reconcile = getattr(session, "paper_automation_reconcile", None)
        capacity = getattr(reconcile, "available_buy_principal", None)
        if not callable(capacity):
            raise ValueError
        remaining = capacity(symbol)
        if type(remaining) is not int or not 0 <= remaining <= symbol_cap:
            raise ValueError
        # Account I/O can outlive a settings reset. Never size under a policy
        # or per-buy cap different from the one observed before that I/O.
        after = load_settings()
        if (not isinstance(after, dict) or type(after.get("paper_automation_only", False)) is not bool
                or after.get("paper_automation_only", False) != paper_only
                or _buy_cap(after) != per_buy or _multi_enabled(after) != multi
                or _symbol_cap(after) != symbol_cap
                or session.mode != "paper"):
            raise ValueError
        return min(original, per_buy, remaining)
    except Exception:
        raise PaperAutomationPolicyError("종목별 잔여 매입 한도를 확인하지 못해 자동매수를 보류합니다.") from None
