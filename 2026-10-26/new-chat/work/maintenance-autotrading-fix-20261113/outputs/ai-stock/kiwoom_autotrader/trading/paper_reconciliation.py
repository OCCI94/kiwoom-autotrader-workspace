"""Fresh PAPER observations before an automatic order, never inferred fills.

Call the gate on the caller thread, before creating this order's intent. The
returned closure is safe for the shared HTTP queue: it performs no broker I/O.
Existing readers own pagination, fixed origins and the common HTTP limiter.
The pending-order request follows /tr_docs/ka10075.md (all KRX symbols/sides).
This does not erase a recorder gap or certify historical unit-fill coverage.
"""
from __future__ import annotations

from datetime import datetime
import hashlib
import json
import math
import re
import time

from kiwoom_autotrader.api.account_report import fetch_account_report
from kiwoom_autotrader.api.executions import Execution, _request_rows, fetch_executions
from kiwoom_autotrader.api.minute_chart import session_identity
from kiwoom_autotrader.api.session import KST
from kiwoom_autotrader.config.settings import load_settings, effective_paper_scan
from kiwoom_autotrader.trading.paper_policy import _symbol_cap, _buy_cap, _multi_enabled


_MAX_AGE_SECONDS = 20.0
_FAILED = "모의 자동주문의 체결·잔고·미체결 대사를 확인하지 못해 주문을 보류합니다."
_CHANGED = "대사 후 로그인·체결 수신 상태 또는 유효시간이 바뀌어 주문을 보류합니다."


class PaperReconciliationError(ValueError):
    """Known pre-send failure, containing no remote text or private fields."""

    def __init__(self, message, *, reason_code=None):
        super().__init__(message)
        self.reason_code = reason_code


def _pending_rows(session, *, observer=None, day=None, integrated=False):
    return _request_rows(session, "ka10075", {
        "all_stk_tp": "0", "trde_tp": "0", "stex_tp": "0" if integrated else "1",
    }, "oso", observer=observer, day=day)


def _quantity(value):
    if not isinstance(value, str) or re.fullmatch(r"\+?[0-9]{1,15}", value) is None:
        raise PaperReconciliationError(_FAILED)
    return int(value)


def _validate_holdings(report):
    if (not isinstance(report, dict) or report.get("mode") != "paper"
            or report.get("exchange") != "KRX" or report.get("snapshot_changed") is not False
            or not isinstance(report.get("holdings"), list)):
        raise PaperReconciliationError(_FAILED)
    held_symbols, seen, lots, purchase_amounts, unknown_cost = set(), set(), {}, {}, set()
    for row in report["holdings"]:
        if (not isinstance(row, dict) or not isinstance(row.get("stk_cd"), str)
                or re.fullmatch(r"[AJQ]?[0-9]{6}", row["stk_cd"]) is None
                or not isinstance(row.get("crd_tp"), str)
                or re.fullmatch(r"[0-9]{2}", row["crd_tp"]) is None):
            raise PaperReconciliationError(_FAILED)
        held, sellable = _quantity(row.get("rmnd_qty")), _quantity(row.get("trde_able_qty"))
        if sellable > held:
            raise PaperReconciliationError(_FAILED)
        loan_date = row.get("crd_loan_dt")
        if loan_date not in (None, ""):
            if not isinstance(loan_date, str) or re.fullmatch(r"[0-9]{8}", loan_date) is None:
                raise PaperReconciliationError(_FAILED)
            try:
                datetime.strptime(loan_date, "%Y%m%d")
            except ValueError:
                raise PaperReconciliationError(_FAILED) from None
        # A/no prefix and J/Q forms can refer to the same six-digit code. A
        # duplicate identity is ambiguous; different credit lots remain valid
        # and any one positive lot is sufficient to block another buy.
        symbol = row["stk_cd"][-6:]
        key = (symbol, row["crd_tp"], loan_date or None)
        if key in seen:
            raise PaperReconciliationError(_FAILED)
        seen.add(key)
        lots.setdefault(symbol, []).append((row["stk_cd"], row["crd_tp"], loan_date, sellable))
        # The source contract documents 00 cash and 03 credit; unknown types,
        # contradictory cash loan dates and unidentified credit lots are not
        # sufficient to establish a reliable purchase-cost total.
        if (row["crd_tp"] not in ("00", "03")
                or row["crd_tp"] == "00" and loan_date not in (None, "")
                or held > 0 and row["crd_tp"] == "03" and loan_date in (None, "")):
            unknown_cost.add(symbol)
        if held > 0:
            held_symbols.add(symbol)
            try:
                amount = _quantity(row.get("pur_amt"))
                if amount <= 0:
                    raise PaperReconciliationError(_FAILED)
                purchase_amounts[symbol] = purchase_amounts.get(symbol, 0) + amount
            except PaperReconciliationError:
                unknown_cost.add(symbol)
        elif row.get("pur_amt") not in (None, ""):
            try:
                if _quantity(row["pur_amt"]) != 0:
                    unknown_cost.add(symbol)
            except PaperReconciliationError:
                unknown_cost.add(symbol)
    # Only one unambiguous, non-credit cash row supports a cash-stock sell.
    # A credit/short/split position in the same six-digit code is not folded
    # into sellable cash or guessed from the aggregate holding quantity.
    cash_sellable = {}
    for symbol, rows in lots.items():
        if len(rows) == 1:
            code, credit, loan_date, sellable = rows[0]
            if not code.startswith(("J", "Q")) and credit == "00" and loan_date in (None, ""):
                cash_sellable[symbol] = sellable
    return held_symbols, cash_sellable, purchase_amounts, unknown_cost


def _positive_order_value(value):
    if type(value) is int:
        value = str(value)
    if not isinstance(value, str) or re.fullmatch(r"[0-9]{1,12}", value) is None or int(value) <= 0:
        raise PaperReconciliationError(_FAILED)
    return int(value)


def _remaining_principal(symbol, held_symbols, purchase_amounts, unknown_cost, cap=1_000_000):
    if symbol in unknown_cost or symbol in held_symbols and symbol not in purchase_amounts:
        raise PaperReconciliationError("보유 종목의 매입금액을 확인하지 못해 자동매수를 보류합니다.")
    return max(0, cap - purchase_amounts.get(symbol, 0))


def _assert_no_broker_pending(rows, symbol, *, integrated=False, all_venues=False):
    if not isinstance(rows, (list, tuple)):
        raise PaperReconciliationError(_FAILED)
    pending = False
    for row in rows:
        if (not isinstance(row, dict) or not isinstance(row.get("stk_cd"), str)
                or re.fullmatch(r"A?[0-9]{6}", row["stk_cd"]) is None
                or not isinstance(row.get("ord_no"), str)
                or re.fullmatch(r"[0-9]{1,7}", row["ord_no"]) is None
                or int(row["ord_no"]) == 0
                or ("stex_tp" in row and row["stex_tp"] not in (("0", "1", "2") if integrated or all_venues else ("1",)))):
            raise PaperReconciliationError(_FAILED)
        quantity = _quantity(row.get("oso_qty"))
        pending |= (integrated or row["stk_cd"][-6:] == symbol) and quantity > 0
    if pending:
        raise PaperReconciliationError("해당 종목의 증권사 미체결 주문이 있어 새 자동주문을 보류합니다.",
                                       reason_code="broker_pending_order_exists")


def _assert_d_pending_pages(events, rows, day, minimum_at, now):
    """Require the just-saved complete integrated query, not only empty rows.

    Local indices only support the shared page validator's ordering. They are
    not broker observation IDs and are never persisted as financial proof.
    Private response fields are excluded from the economic row comparison.
    """
    from .broker_exit_only import latest_complete_pages
    from .decision_evidence import canonical_json
    try:
        if (not isinstance(events, list) or not 1 <= len(events) <= 20
                or not isinstance(rows, (list, tuple)) or len(rows) > 20000):
            raise ValueError
        entries = []; queries = set()
        for index, event in enumerate(events):
            if (not isinstance(event, dict) or event.get('source_api') != 'ka10075'
                    or event.get('source_kind') != 'broker_observation' or event.get('mode') != 'paper'
                    or event.get('day') != day or not isinstance(event.get('query_id'), str)
                    or re.fullmatch('[0-9a-f]{32}', event['query_id']) is None):
                raise ValueError
            requested = datetime.fromisoformat(event['request_started_at'])
            observed = datetime.fromisoformat(event['observed_at'])
            if (requested.tzinfo is None or observed.tzinfo is None
                    or requested.astimezone(KST).strftime('%Y%m%d') != day
                    or not minimum_at <= requested <= observed <= now):
                raise ValueError
            queries.add(event['query_id'])
            entries.append(({'id': index, 'source_api': 'ka10075',
                'digest': hashlib.sha256(canonical_json(event, maximum=4*1024*1024).encode()).hexdigest()}, event))
        if len(queries) != 1:
            raise ValueError
        complete = latest_complete_pages(entries, 'ka10075', day, now)
        if len(complete['rows']) > 20000:
            raise ValueError
        def economic(value):
            if not isinstance(value, dict):raise ValueError
            return {key: value[key] for key in ('stk_cd', 'ord_no', 'oso_qty', 'stex_tp') if key in value}
        if [economic(value) for value in rows] != [economic(value) for value in complete['rows']]:
            raise ValueError
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
        raise PaperReconciliationError(_FAILED) from None


def _exit_order_details(session, day, *, observer):
    """Full exchange scope; broker_exit_only validates the immutable pages."""
    from kiwoom_autotrader.api.executions import _request_rows
    return _request_rows(session,'kt00007',{'ord_dt':day,'qry_tp':'1','stk_bond_tp':'1',
        'sell_tp':'0','stk_cd':'','fr_ord_no':'','dmst_stex_tp':'%'},
        'acnt_ord_cntr_prps_dtl',observer=observer,day=day)


class PaperReconciliationGate:
    def __init__(self, session, tracker, *, executions_fetcher=None, account_fetcher=None,
                 pending_fetcher=None, now=None, monotonic=None, detail_fetcher=None):
        for name in ("record_broker_observation", "_ingest", "_holdings"):
            if not callable(getattr(tracker, name, None)):
                raise ValueError("대사 결과를 기록할 체결 원장이 필요합니다.")
        self._session = session
        self._tracker = tracker
        self._executions = executions_fetcher or fetch_executions
        self._account = account_fetcher or fetch_account_report
        self._pending = pending_fetcher or _pending_rows
        self._default_pending = pending_fetcher is None
        self._detail = detail_fetcher or _exit_order_details
        self._now = now or (lambda: datetime.now(KST))
        self._monotonic = monotonic or time.monotonic
        self._execution_source = "broker_observation" if executions_fetcher is None else "unknown"
        self._holdings_source = "broker_observation" if account_fetcher is None else "unknown"

    def _moment(self):
        moment = self._now()
        if not isinstance(moment, datetime) or moment.tzinfo is None or moment.utcoffset() is None:
            raise PaperReconciliationError(_FAILED)
        return moment.astimezone(KST)

    def _clock(self):
        value = self._monotonic()
        if type(value) not in (int, float) or not math.isfinite(value):
            raise PaperReconciliationError(_FAILED)
        return value

    def _checkpoint(self):
        identity = session_identity(self._session)
        if identity[0] != "paper":
            raise PaperReconciliationError("모의투자 세션에서만 자동주문 대사를 수행합니다.")
        recorder = getattr(self._session, "execution_recorder", None)
        if recorder is None or not callable(getattr(recorder, "status", None)):
            raise PaperReconciliationError(_FAILED)
        status = recorder.status()
        if (not isinstance(status, dict) or status.get("ready") is not True
                or status.get("error") is not None
                or type(status.get("gap_count")) is not int or status["gap_count"] < 0
                or type(status.get("observations")) is not int or status["observations"] < 0
                or type(status.get("ingress_sequence")) is not int or status["ingress_sequence"] < 0
                or type(status.get("inflight_callbacks")) is not int or status["inflight_callbacks"] != 0
                or type(status.get("engine_ingress_sequence")) is not int or status["engine_ingress_sequence"] < 0
                or type(status.get("engine_pending_callbacks")) is not int or status["engine_pending_callbacks"] != 0):
            raise PaperReconciliationError(_FAILED)
        # Capture the actual recorder, not only a counter which resets on restart.
        return (identity, recorder, status["gap_count"], self._moment().date(), status["observations"],
                status["ingress_sequence"], status["engine_ingress_sequence"])

    def _unchanged(self, initial, started_at):
        if self._checkpoint() != initial:
            raise PaperReconciliationError(_CHANGED, reason_code="reconciliation_state_changed")
        elapsed = self._clock() - started_at
        if not 0 <= elapsed <= _MAX_AGE_SECONDS:
            raise PaperReconciliationError(_CHANGED, reason_code="reconciliation_proof_expired")

    def available_buy_principal(self, symbol):
        """Fresh pre-sizing bound, rechecked again before any actual order.

        All positive cash and credit lots of this six-digit code count toward
        the holding's purchase cost. No estimate from market price is used.
        """
        try:
            if not isinstance(symbol, str) or re.fullmatch(r"[0-9]{6}", symbol) is None:
                raise PaperReconciliationError(_FAILED)
            settings = load_settings()
            cap, multi = _symbol_cap(settings), _multi_enabled(settings)
            initial, started_at = self._checkpoint(), self._clock()
            report = self._account(self._session)
            self._unchanged(initial, started_at)
            held, _, amounts, unknown = _validate_holdings(report)
            self._tracker._holdings("paper", report, self._moment(), source_kind=self._holdings_source)
            self._unchanged(initial, started_at)
            current = load_settings()
            if (_symbol_cap(current), _multi_enabled(current)) != (cap, multi):
                raise PaperReconciliationError(_CHANGED)
            return _remaining_principal(symbol, held, amounts, unknown, cap)
        except PaperReconciliationError:
            raise
        except Exception:
            raise PaperReconciliationError(_FAILED) from None

    def __call__(self, symbol, *, side="buy", quantity=None, price=None, strategy=None, exit_kind=None, generation_id=None, ownership_status=None):
        try:
            from kiwoom_autotrader.config.settings import effective_paper_experiment
            from kiwoom_autotrader.trading.paper_policy import experiment_order_sizing
            initial_settings = load_settings()
            experiment = effective_paper_experiment(initial_settings, getattr(self._session, 'mode', None))
            experimental = experiment.get('enabled') is True
            if (not isinstance(symbol, str) or re.fullmatch(r"[0-9]{6}", symbol) is None
                    or side not in ("buy", "sell")):
                raise PaperReconciliationError(_FAILED)
            quantity = _positive_order_value(quantity)
            multi = _multi_enabled(initial_settings) or experimental
            if side == "buy":
                price = _positive_order_value(price)
                caps_settings = load_settings()
                cap, per_buy_cap = _symbol_cap(caps_settings), _buy_cap(caps_settings)
                multi = _multi_enabled(caps_settings) or experimental
                elevated = cap > 1_000_000 or per_buy_cap > 1_000_000
                expanded = effective_paper_scan(caps_settings, 'paper')['max_positions'] > 0
                if not experimental and quantity * price > per_buy_cap:
                    raise PaperReconciliationError("매수당 원금 한도를 초과해 자동매수를 보류합니다.",
                                                   reason_code="symbol_principal_limit_reached")
            elif price is not None:
                raise PaperReconciliationError(_FAILED)
            initial = self._checkpoint()
            day = initial[3].strftime("%Y%m%d")
            started_at = self._clock()
            legacy_sell = side=='sell' and multi and strategy=='ddcrs'
            if legacy_sell:self._tracker.assert_legacy_ddcrs_allowed('paper',symbol)
            from .execution_log import STAGED_STRATEGIES
            staged_sell = side == 'sell' and multi and strategy in STAGED_STRATEGIES
            exit_only = ownership_status == 'exit_only_broker_confirmed'
            if ownership_status is not None and (not exit_only or not staged_sell or exit_kind!='full'):
                raise PaperReconciliationError(_FAILED)
            if staged_sell and (self._holdings_source!='broker_observation' or
                    (exit_kind not in ('half','full') or not isinstance(generation_id,str) or re.fullmatch(r'[0-9a-f]{32}',generation_id) is None)):
                raise PaperReconciliationError(_FAILED)
            watermark = self._tracker.symbol_order_watermark('paper', symbol) if multi and (side == 'buy' or staged_sell) else None

            def unchanged():
                # A fill/order arriving during the multi-request observation can
                # invalidate its ordering. Require a fresh pass in that case.
                self._unchanged(initial, started_at)

            executions = self._executions(self._session, day,
                                          observer=self._tracker.record_broker_observation)
            unchanged()
            if (not isinstance(executions, (tuple, list))
                    or any(not isinstance(item, Execution) or item.day != day for item in executions)):
                raise PaperReconciliationError(_FAILED)
            self._tracker._ingest("paper", day, executions, observed_at=self._moment(),
                                  source_kind=self._execution_source)
            unchanged()
            if exit_only:
                self._detail(self._session,day,observer=self._tracker.record_broker_observation)
                unchanged()
            report = self._account(self._session)
            unchanged()
            if side == "buy" and (elevated or expanded):
                # Cash and holdings are sequential snapshots, not atomic. The
                # 0.5% reserve is a conservative assumption, not a quoted fee.
                # Missing or insufficient orderable amount fails closed; external manual
                # account activity can still race the final HTTP dispatch.
                cash = report.get("cash") if isinstance(report, dict) else None
                if not isinstance(cash, dict):
                    raise PaperReconciliationError("주문가능금액을 확인하지 못해 자동매수를 보류합니다.")
                available = _quantity(cash.get("ord_alow_amt"))
                if expanded and not experimental:
                    available = min(available, _quantity(cash.get('entr')))
                cash_reserve = (quantity * price * 5 + 999) // 1000
                if available < quantity * price + cash_reserve:
                    failure = PaperReconciliationError("수수료 여유를 포함한 주문가능금액이 부족해 자동매수를 보류합니다.")
                    failure.reason_code = "insufficient_cash"
                    raise failure
            held_symbols, cash_sellable, purchase_amounts, unknown_cost = _validate_holdings(report)
            sizing = None
            if side == 'buy' and experimental:
                if symbol in unknown_cost:
                    raise PaperReconciliationError(_FAILED)
                sizing = experiment_order_sizing(initial_settings, 'paper',
                    available_cash_krw=_quantity(report['cash']['ord_alow_amt']), price=price,
                    existing_principal_krw=purchase_amounts.get(symbol, 0), pending_principal_krw=0)
                # Same-symbol broker/local pending is independently rejected below.
                if quantity > sizing['quantity'] or sizing['quantity'] <= 0:
                    sizing_reason = ("insufficient_cash"
                                     if sizing.get("reason") == "insufficient_cash"
                                     else "symbol_principal_limit_reached")
                    raise PaperReconciliationError('모의 실험의 현재 현금 기준 주문 예산을 초과했습니다.',
                                                   reason_code=sizing_reason)
                cap = sizing['effective_symbol_cap_krw']
                per_buy_cap = max(300000, price) if sizing['one_share_exception'] else sizing['budget_krw']
            holdings_observed_at = self._moment()
            self._tracker._holdings("paper", report, holdings_observed_at, source_kind=self._holdings_source)
            if (side == 'buy' and expanded) or exit_only:
                # Reuse the same complete fetched report for the atomic local
                # account guard. This does not issue another broker request.
                self._tracker._save_paper_balance_snapshot('paper', report, holdings_observed_at)
            unchanged()
            if side == "buy" and quantity * price > _remaining_principal(symbol, held_symbols, purchase_amounts, unknown_cost, cap):
                raise PaperReconciliationError("보유 종목 매입금액과 새 주문 원금이 종목당 한도를 초과해 자동매수를 보류합니다.",
                                               reason_code="symbol_principal_limit_reached")
            if side == "sell" and (symbol not in cash_sellable or quantity > cash_sellable[symbol]):
                raise PaperReconciliationError("최신 잔고의 현금주식 매도가능 수량이 부족하거나 불명확하여 자동매도를 보류합니다.",
                                               reason_code="insufficient_sellable_qty")
            pending_events = []
            def pending_observer(event):
                self._tracker.record_broker_observation(event)
                if d_owned_exit:
                    from .decision_evidence import canonical_json
                    pending_events.append(json.loads(canonical_json(event, maximum=4*1024*1024)))
                else:
                    pending_events.append(event)
            # Both normal and late EXP_D exits must see every venue, while the
            # paper experiment still serializes only this symbol's pending lot.
            d_owned_exit = staged_sell and strategy == 'EXP_D'
            integrated_pending = exit_only or d_owned_exit or side == "buy" and (elevated or multi or expanded)
            if self._default_pending:
                if integrated_pending:
                    rows = self._pending(self._session, observer=pending_observer, day=day, integrated=True)
                else:
                    rows = self._pending(self._session, observer=self._tracker.record_broker_observation, day=day)
            else:
                # An injected reader is a test/alternative source with the
                # established single-argument contract. It is not labeled as
                # a provider observation or fabricated into a broker page.
                rows = self._pending(self._session)
            unchanged()
            if d_owned_exit:
                _assert_d_pending_pages(pending_events, rows, day, holdings_observed_at, self._moment())
            _assert_no_broker_pending(rows, symbol, integrated=integrated_pending and not experimental,
                                      all_venues=d_owned_exit)
            if multi and (side == 'buy' or staged_sell) and self._tracker.symbol_order_watermark('paper', symbol) != watermark:
                raise PaperReconciliationError(_CHANGED, reason_code="reconciliation_state_changed")
            if side == "buy":
                held_qty = sum(_quantity(row["rmnd_qty"]) for row in report["holdings"]
                               if row["stk_cd"][-6:] == symbol)
                principal = purchase_amounts.get(symbol, 0)
                remaining = _remaining_principal(symbol, held_symbols, purchase_amounts, unknown_cost, cap)
                # This is the pre-order snapshot, not proof of eventual fills.
                evidence = {"schema_version": 1, "symbol": symbol,
                    "pre_order_held_qty": held_qty, "pre_order_pur_amt_krw": principal,
                    "remaining_principal_krw": remaining, "order_quantity": quantity,
                    "limit_price_krw": price, "observed_at": holdings_observed_at.isoformat(),
                    "holdings_source": self._holdings_source}
                if multi:
                    observation_id=self._tracker.record_strategy_holding_observation(symbol,held_qty,
                        cash_sellable.get(symbol,0),principal,holdings_observed_at,source_kind=self._holdings_source)
                    evidence.update(schema_version=3, symbol_cap_krw=cap, per_buy_cap_krw=per_buy_cap,
                                    multi_strategy_enabled=True, symbol_ledger_sha256=watermark,
                                    holding_observation_id=observation_id)
                    if (held_qty == 0 and principal == 0 and self._default_pending
                            and self._execution_source == 'broker_observation'
                            and self._holdings_source == 'broker_observation'):
                        if not pending_events or len({e.get('query_id') for e in pending_events}) != 1:
                            raise PaperReconciliationError(_FAILED)
                        stream_anchor_id,stream_anchor_sha256,stream_anchor_observed_at=self._tracker.stream_anchor(holdings_observed_at)
                        evidence.update(schema_version=6 if experimental else 5, integrated_pending_clear=not experimental,
                            pending_query_id=pending_events[0]['query_id'],
                            stream_anchor_id=stream_anchor_id,stream_anchor_sha256=stream_anchor_sha256,
                            stream_anchor_observed_at=stream_anchor_observed_at,
                            recorder_gap_count=initial[2], recorder_observations=initial[4],
                            recorder_ingress_sequence=initial[5], engine_ingress_sequence=initial[6],
                            reconciliation_completed_at=self._moment().isoformat())
                        if experimental:
                            evidence['symbol_pending_clear'] = True
                elif cap != 1_000_000 or per_buy_cap != 1_000_000:
                    evidence["schema_version"] = 2
                    evidence["symbol_cap_krw"] = cap
                    evidence["per_buy_cap_krw"] = per_buy_cap
                    if elevated:
                        evidence["available_orderable_krw"] = available
                        evidence["cost_reserve_krw"] = cash_reserve
                if experimental:
                    evidence['paper_experiment_sizing'] = sizing
            elif staged_sell:
                position=(self._tracker.paper_exit_only_position('paper',symbol,strategy,self._moment()) if exit_only else
                          self._tracker.multi_strategy_position('paper',symbol,strategy,self._moment()))
                held_qty=sum(_quantity(row['rmnd_qty']) for row in report['holdings'] if row['stk_cd'][-6:]==symbol)
                principal=purchase_amounts.get(symbol)
                if (position.get('status')!=('exit_only_broker_confirmed' if exit_only else 'owned') or position.get('generation_id')!=generation_id
                        or position.get('owned_qty')!=held_qty or type(principal) is not int or principal<=0
                        or position.get('basis_krw') is not None and position['basis_krw']!=principal
                        or symbol in unknown_cost or quantity>held_qty
                        or exit_kind=='half' and (position.get('half_take_done') or quantity>=held_qty)
                        or exit_kind=='full' and quantity!=held_qty):
                    raise PaperReconciliationError(_FAILED)
                observation_id=self._tracker.record_strategy_holding_observation(symbol,held_qty,cash_sellable[symbol],principal,
                    holdings_observed_at,source_kind=self._holdings_source)
                evidence={'schema_version':4,'symbol':symbol,'pre_order_held_qty':held_qty,
                    'pre_order_pur_amt_krw':principal,'order_quantity':quantity,'observed_at':holdings_observed_at.isoformat(),
                    'holdings_source':'broker_observation','generation_id':generation_id,'exit_kind':exit_kind,
                    'strategy':strategy,'symbol_ledger_sha256':watermark,'holding_observation_id':observation_id}
                if exit_only:
                    evidence.update(schema_version=7,ownership_status='exit_only_broker_confirmed',
                        profit_attribution='unconfirmed',exit_only_broker_proof=position['evidence'])
            else:
                evidence = None
            completed_at = self._clock()
            unchanged()
            if side == "buy":
                current = load_settings()
                if (effective_paper_experiment(current, 'paper') != experiment if experimental else
                        (_symbol_cap(current), _buy_cap(current), _multi_enabled(current)) != (cap, per_buy_cap, multi)):
                    raise PaperReconciliationError(_CHANGED, reason_code="reconciliation_state_changed")
        except PaperReconciliationError:
            raise
        except Exception:
            raise PaperReconciliationError(_FAILED, reason_code="reconciliation_failed") from None

        def before_send():
            """No broker queries or retrospective coverage claims."""
            try:
                # Reject changed credentials before asking the recorder to
                # refresh its subscription for that new session.
                if session_identity(self._session) != initial[0]:
                    raise PaperReconciliationError(_CHANGED, reason_code="reconciliation_state_changed")
                unchanged()
                if side == "buy":
                    current = load_settings()
                    if (effective_paper_experiment(current, 'paper') != experiment if experimental else
                            (_symbol_cap(current), _buy_cap(current), _multi_enabled(current)) != (cap, per_buy_cap, multi)):
                        raise PaperReconciliationError(_CHANGED, reason_code="reconciliation_state_changed")
                elif (staged_sell or legacy_sell) and not (_multi_enabled(load_settings()) or experimental and effective_paper_experiment(load_settings(), 'paper').get('enabled')):
                    raise PaperReconciliationError(_CHANGED, reason_code="reconciliation_state_changed")
                if legacy_sell:self._tracker.assert_legacy_ddcrs_allowed('paper',symbol)
                age = self._clock() - completed_at
                if not 0 <= age <= _MAX_AGE_SECONDS:
                    raise PaperReconciliationError(_CHANGED, reason_code="reconciliation_proof_expired")
            except PaperReconciliationError:
                raise
            except Exception:
                raise PaperReconciliationError(_CHANGED, reason_code="reconciliation_state_changed") from None

        before_send.reconciliation_evidence = evidence
        return before_send
