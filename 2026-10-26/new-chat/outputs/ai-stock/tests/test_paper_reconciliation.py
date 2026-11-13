"""Offline reconciliation gates: no credentials, broker I/O or service access."""
from copy import deepcopy
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
import tempfile
import threading
import unittest
import json
from unittest.mock import Mock, patch

from kiwoom_autotrader.api.executions import Execution
from kiwoom_autotrader.api.orders import OrderReceipt
from kiwoom_autotrader.api.session import BrokerSession, IssuedToken, KST
from kiwoom_autotrader.trading.execution_log import ExecutionTracker
from kiwoom_autotrader.trading.paper_reconciliation import PaperReconciliationError, PaperReconciliationGate
from kiwoom_autotrader.trading.order_context import automated_orders


def account(holdings=None):
    return {"mode": "paper", "exchange": "KRX", "snapshot_changed": False,
            "holdings": [] if holdings is None else holdings}


def pending(code="097520", quantity="1", **updates):
    return {"stk_cd": code, "ord_no": "0000007", "oso_qty": quantity, "stex_tp": "1", **updates}


class PaperReconciliationTests(unittest.TestCase):
    def setUp(self):
        network = patch("socket.socket.connect", side_effect=AssertionError("network forbidden"))
        network.start()
        self.addCleanup(network.stop)
        self.now = datetime(2026, 10, 27, 13, 0, 5, tzinfo=KST)
        self.clock = 100.0
        self.session = BrokerSession(now=lambda: self.now)
        self.session.mode = "paper"
        self.session._issued = IssuedToken("offline-reconcile-private-token", "20300101000000")
        self.status = {"ready": True, "error": None, "gap_count": 3, "observations": 7,
                       "ingress_sequence": 7, "inflight_callbacks": 0,
                       "engine_ingress_sequence": 7, "engine_pending_callbacks": 0,
                       "coverage_complete": False}
        self.recorder = Mock()
        self.recorder.status.side_effect = lambda: dict(self.status)
        self.session.execution_recorder = self.recorder
        self.tracker = Mock()
        self.executions = Mock(return_value=())
        self.account = Mock(return_value=account())
        self.pending = Mock(return_value=[])
        self.gate = self.make_gate()

    def make_gate(self, tracker=None):
        gate = PaperReconciliationGate(self.session, tracker or self.tracker,
            executions_fetcher=self.executions, account_fetcher=self.account, pending_fetcher=self.pending,
            now=lambda: self.now, monotonic=lambda: self.clock)
        def call(symbol, **kwargs):
            kwargs.setdefault("quantity", "1")
            if kwargs.get("side", "buy") == "buy":
                kwargs.setdefault("price", "100")
            return gate(symbol, **kwargs)
        call.available_buy_principal = gate.available_buy_principal
        return call

    def test_complete_pass_records_both_sources_without_claiming_old_gap_recovery(self):
        guard = self.gate("097520")
        self.executions.assert_called_once_with(self.session, "20261027",
                                               observer=self.tracker.record_broker_observation)
        self.tracker._ingest.assert_called_once_with("paper", "20261027", (), observed_at=self.now,
                                                    source_kind="unknown")
        self.tracker._holdings.assert_called_once_with("paper", account(), self.now, source_kind="unknown")
        guard()
        guard()
        self.assertFalse(self.status["coverage_complete"])
        self.assertEqual(self.status["gap_count"], 3)
        self.assertEqual(self.executions.call_count, 1)
        self.assertEqual(self.account.call_count, 1)
        self.assertEqual(self.pending.call_count, 1)
        self.assertEqual(self.tracker._ingest.call_count, 1)

    def test_multi_strategy_v3_proof_and_changed_watermark_fail_closed(self):
        settings={'paper_multi_strategy_enabled':True,'paper_automation_symbol_cap':3000000,
                  'paper_automation_buy_cap':3000000}
        self.tracker.symbol_order_watermark.return_value='a'*64
        with patch('kiwoom_autotrader.trading.paper_reconciliation.load_settings',return_value=settings):
            guard=self.gate('097520',quantity='1',price='100')
            self.assertEqual(guard.reconciliation_evidence['schema_version'],3)
            self.assertEqual(guard.reconciliation_evidence['symbol_ledger_sha256'],'a'*64)
            self.assertEqual(guard.reconciliation_evidence['symbol_cap_krw'],1000000)
            guard()
            settings['paper_multi_strategy_enabled']=False
            with self.assertRaises(PaperReconciliationError):guard()
            settings['paper_multi_strategy_enabled']=True
            self.tracker.symbol_order_watermark.side_effect=['a'*64,'b'*64]
            with self.assertRaises(PaperReconciliationError):self.gate('097520',quantity='1',price='100')

    def test_expanded_universe_uses_lower_deposit_and_orderable_without_extra_fetch(self):
        settings={'paper_multi_strategy_enabled':True, 'paper_dynamic_scan_enabled':True}
        self.tracker.symbol_order_watermark.return_value='a'*64
        self.account.return_value={**account(), 'cash':{'entr':'100','ord_alow_amt':'999999'}}
        with patch('kiwoom_autotrader.trading.paper_reconciliation.load_settings',return_value=settings):
            with self.assertRaises(PaperReconciliationError) as error:
                self.gate('097520',quantity='1',price='100')
            self.assertEqual(error.exception.reason_code,'insufficient_cash')
            self.account.return_value['cash']['entr']='101'
            self.account.reset_mock();self.pending.reset_mock()
            guard=self.gate('097520',quantity='1',price='100')
            self.assertEqual(self.account.call_count,1)
            self.assertEqual(self.pending.call_count,1)
            self.tracker._save_paper_balance_snapshot.assert_called_once_with('paper',self.account.return_value,self.now)
            guard()

    def test_expanded_universe_missing_deposit_fails_closed(self):
        self.account.return_value={**account(),'cash':{'ord_alow_amt':'999999'}}
        with patch('kiwoom_autotrader.trading.paper_reconciliation.load_settings',return_value={'paper_dynamic_scan_enabled':True}):
            with self.assertRaises(PaperReconciliationError):self.gate('097520')

    def test_multi_epoch_requires_complete_integrated_broker_query_and_stable_recorder(self):
        settings={'paper_multi_strategy_enabled':True}
        self.tracker.symbol_order_watermark.return_value='a'*64
        self.tracker.record_strategy_holding_observation.return_value='b'*32
        self.tracker.stream_anchor.return_value=(7,'c'*64,(self.now-timedelta(seconds=2)).isoformat())
        gate=PaperReconciliationGate(self.session,self.tracker,executions_fetcher=self.executions,
            account_fetcher=self.account,now=lambda:self.now,monotonic=lambda:self.clock)
        gate._execution_source=gate._holdings_source='broker_observation'
        def pending_query(session,*,observer,day,integrated):
            self.assertTrue(integrated)
            observer({'query_id':'d'*32})
            return []
        gate._pending=pending_query
        with patch('kiwoom_autotrader.trading.paper_reconciliation.load_settings',return_value=settings):
            guard=gate('097520',quantity='1',price='100')
            proof=guard.reconciliation_evidence
            self.assertEqual(proof['schema_version'],5)
            self.assertEqual((proof['pending_query_id'],proof['stream_anchor_id']),('d'*32,7))
            guard()
            self.status['engine_ingress_sequence']+=1
            with self.assertRaises(PaperReconciliationError):guard()

    def test_staged_sell_requires_broker_snapshot_and_single_owner(self):
        settings={'paper_multi_strategy_enabled':True}
        self.tracker.symbol_order_watermark.return_value='a'*64
        self.tracker.multi_strategy_position.return_value={'status':'owned','generation_id':'a'*32,
            'owned_qty':10,'half_take_done':False}
        self.account.return_value=account([{'stk_cd':'A097520','crd_tp':'00','rmnd_qty':'10',
                                           'trde_able_qty':'10','pur_amt':'100000'}])
        gate=PaperReconciliationGate(self.session,self.tracker,executions_fetcher=self.executions,
            account_fetcher=self.account,pending_fetcher=self.pending,now=lambda:self.now,monotonic=lambda:self.clock)
        gate._holdings_source='broker_observation'  # explicit offline fixture for the built-in broker reader
        strategy='S2_surge_high_breakout'
        with patch('kiwoom_autotrader.trading.paper_reconciliation.load_settings',return_value=settings):
            guard=gate('097520',side='sell',quantity='5',strategy=strategy,exit_kind='half',generation_id='a'*32)
            self.assertEqual(guard.reconciliation_evidence['pre_order_pur_amt_krw'],100000)
            self.assertEqual(guard.reconciliation_evidence['schema_version'],4)
            self.tracker.multi_strategy_position.return_value['basis_krw']=100001
            with self.assertRaises(PaperReconciliationError):
                gate('097520',side='sell',quantity='5',strategy=strategy,exit_kind='half',generation_id='a'*32)
            self.tracker.multi_strategy_position.return_value['basis_krw']=100000
            self.tracker.multi_strategy_position.return_value['owned_qty']=9
            with self.assertRaises(PaperReconciliationError):
                gate('097520',side='sell',quantity='5',strategy=strategy,exit_kind='half',generation_id='a'*32)
            gate._holdings_source='unknown'
            with self.assertRaises(PaperReconciliationError):
                gate('097520',side='sell',quantity='5',strategy=strategy,exit_kind='half',generation_id='a'*32)

    def test_existing_holding_enforces_remaining_budget_and_pending_blocks_sell(self):
        self.account.return_value = account([{"stk_cd": "A097520", "crd_tp": "00",
                                               "rmnd_qty": "49", "trde_able_qty": "49", "pur_amt": "999950"}])
        self.pending.return_value = [pending("005930"), pending(quantity="0")]
        with self.assertRaisesRegex(PaperReconciliationError, "초과"):
            self.gate("097520", side="buy")
        self.gate("097520", side="sell", quantity="49")()
        self.pending.return_value.append(pending("A097520"))
        with self.assertRaisesRegex(PaperReconciliationError, "미체결"):
            self.gate("097520", side="sell", quantity="49")

    def test_missing_purchase_cost_for_cash_credit_and_jq_all_blocks_automatic_buy(self):
        for code, credit, loan in (("097520", "00", None), ("A097520", "03", "20261001"),
                                   ("J097520", "00", None), ("Q097520", "03", "20261001")):
            with self.subTest(code=code, credit=credit):
                self.account.return_value = account([{"stk_cd": code, "crd_tp": credit, "crd_loan_dt": loan,
                                                       "rmnd_qty": "1", "trde_able_qty": "0"}])
                with self.assertRaisesRegex(PaperReconciliationError, "매입금액"):
                    self.gate("097520", side="buy")
                with self.assertRaisesRegex(PaperReconciliationError, "매도가능"):
                    self.gate("097520", side="sell", quantity="1")

    def test_zero_position_or_other_stock_allows_buy_but_duplicate_or_incomplete_fails_closed(self):
        row = {"stk_cd": "A097520", "crd_tp": "00", "rmnd_qty": "0", "trde_able_qty": "0"}
        for holdings in ([row], [{**row, "stk_cd": "A005930", "rmnd_qty": "2", "trde_able_qty": "1"}]):
            self.account.return_value = account(holdings)
            self.gate("097520", side="buy")()
        for holdings in ([row, dict(row)], [row, {**row, "stk_cd": "097520"}],
                         [{**row, "rmnd_qty": None}], [{**row, "crd_loan_dt": "20260230"}]):
            with self.subTest(holdings=holdings):
                self.account.return_value = account(holdings)
                with self.assertRaises(PaperReconciliationError):
                    self.gate("097520", side="buy")

    def test_multiple_distinct_credit_lots_require_purchase_amount(self):
        self.account.return_value = account([
            {"stk_cd": "097520", "crd_tp": "03", "crd_loan_dt": "20261001", "rmnd_qty": "0", "trde_able_qty": "0"},
            {"stk_cd": "097520", "crd_tp": "03", "crd_loan_dt": "20261002", "rmnd_qty": "2", "trde_able_qty": "0"},
        ])
        with self.assertRaisesRegex(PaperReconciliationError, "매입금액"):
            self.gate("097520", side="buy")
        with self.assertRaisesRegex(PaperReconciliationError, "매도가능"):
            self.gate("097520", side="sell", quantity="1")

    def test_sell_rechecks_exact_current_cash_sellable_quantity_and_rejects_ambiguity(self):
        cash = {"stk_cd": "A097520", "crd_tp": "00", "crd_loan_dt": None,
                "rmnd_qty": "49", "trde_able_qty": "10"}
        self.account.return_value = account([cash])
        self.gate("097520", side="sell", quantity="10")()  # Exactly available is valid.
        for requested in ("11", 49):
            with self.subTest(requested=requested), self.assertRaisesRegex(PaperReconciliationError, "매도가능"):
                self.gate("097520", side="sell", quantity=requested)
        for holdings in ([], [{**cash, "trde_able_qty": "0"}],
                         [cash, {**cash, "crd_tp": "03", "crd_loan_dt": "20261001"}],
                         [{**cash, "stk_cd": "J097520"}], [{**cash, "crd_loan_dt": "20261001"}]):
            with self.subTest(holdings=holdings):
                self.account.return_value = account(holdings)
                with self.assertRaisesRegex(PaperReconciliationError, "매도가능"):
                    self.gate("097520", side="sell", quantity="1")

    def test_sell_quantity_validation_happens_before_account_or_broker_reads(self):
        for quantity in (None, "", "0", 0, -1, True, 1.0, "-1", "+1", "1.0", "1" * 13):
            with self.subTest(quantity=quantity), self.assertRaises(PaperReconciliationError):
                self.gate("097520", side="sell", quantity=quantity)
        self.executions.assert_not_called()
        self.account.assert_not_called()
        self.pending.assert_not_called()

    def test_sell_guard_rejects_new_observation_after_reconciled_quantity(self):
        self.account.return_value = account([{"stk_cd": "097520", "crd_tp": "00",
                                               "rmnd_qty": "49", "trde_able_qty": "49"}])
        guard = self.gate("097520", side="sell", quantity=49)
        self.status["observations"] += 1  # Order/fill received after fresh account read.
        with self.assertRaises(PaperReconciliationError):
            guard()

    def test_invalid_side_fails_before_any_reads(self):
        for side in (None, "", "BUY", "other"):
            with self.subTest(side=side), self.assertRaises(PaperReconciliationError):
                self.gate("097520", side=side)
        self.executions.assert_not_called()

    def test_top_up_uses_fresh_total_purchase_cost_for_all_lots(self):
        cash = {"stk_cd":"A097520", "crd_tp":"00", "rmnd_qty":"10", "trde_able_qty":"10", "pur_amt":"400000"}
        credit = {"stk_cd":"Q097520", "crd_tp":"03", "crd_loan_dt":"20261001",
                  "rmnd_qty":"5", "trde_able_qty":"0", "pur_amt":"200000"}
        self.account.return_value = account([cash, credit])
        self.assertEqual(self.gate.available_buy_principal("097520"), 400000)
        self.gate("097520", side="buy", quantity="4", price="100000")()
        with self.assertRaisesRegex(PaperReconciliationError, "초과"):
            self.gate("097520", side="buy", quantity="5", price="100000")
        self.assertEqual(self.gate.available_buy_principal("005930"), 1000000)

    def test_top_up_revalidates_amount_after_sizing(self):
        row = {"stk_cd":"097520", "crd_tp":"00", "rmnd_qty":"10", "trde_able_qty":"10", "pur_amt":"400000"}
        self.account.return_value = account([row])
        self.assertEqual(self.gate.available_buy_principal("097520"), 600000)
        self.account.return_value = account([{**row, "pur_amt":"500000"}])
        with self.assertRaisesRegex(PaperReconciliationError, "초과"):
            self.gate("097520", side="buy", quantity="6", price="100000")
        self.gate("097520", side="buy", quantity="5", price="100000")()

    def test_same_automatic_order_rechecks_balance_even_when_recorder_is_unchanged(self):
        row = {"stk_cd":"097520", "crd_tp":"00", "rmnd_qty":"10", "trde_able_qty":"10", "pur_amt":"400000"}
        self.account.return_value = account([row])
        with automated_orders(self.tracker, "gdcrs"):
            self.assertEqual(self.gate.available_buy_principal("097520"), 600000)
            # Broker fills or account changes can be delayed in the websocket
            # and executions endpoint while the local checkpoint stays steady.
            self.account.return_value = account([{**row, "pur_amt":"500000"}])
            with self.assertRaisesRegex(PaperReconciliationError, "초과"):
                self.gate("097520", side="buy", quantity="6", price="100000")
            self.assertEqual(self.account.call_count, 2)

    def test_positive_holding_requires_valid_positive_purchase_amount(self):
        row = {"stk_cd":"097520", "crd_tp":"00", "rmnd_qty":"1", "trde_able_qty":"1"}
        for amount in (None, "", "0", "-1", "1.5", True, "1,000", "1" * 16):
            with self.subTest(amount=amount):
                self.account.return_value = account([{**row, "pur_amt": amount}])
                with self.assertRaisesRegex(PaperReconciliationError, "매입금액"):
                    self.gate.available_buy_principal("097520")
                with self.assertRaisesRegex(PaperReconciliationError, "매입금액"):
                    self.gate("097520", side="buy")
        self.account.return_value = account([{**row,"pur_amt":"1000001"}])
        self.assertEqual(self.gate.available_buy_principal("097520"), 0)
        with self.assertRaisesRegex(PaperReconciliationError, "초과"):
            self.gate("097520", side="buy")

    def test_unknown_or_inconsistent_lot_type_cannot_establish_buy_cost(self):
        row = {"stk_cd":"097520", "crd_tp":"00", "rmnd_qty":"1", "trde_able_qty":"1", "pur_amt":"1000"}
        for changes in ({"crd_tp":"99"}, {"crd_tp":"03", "crd_loan_dt":None},
                        {"crd_tp":"00", "crd_loan_dt":"20261001"}):
            self.account.return_value=account([{**row,**changes}])
            with self.subTest(changes=changes), self.assertRaises(PaperReconciliationError):
                self.gate.available_buy_principal("097520")

    def test_buy_quantity_and_limit_price_are_required_and_validated_before_reads(self):
        raw_gate = PaperReconciliationGate(self.session, self.tracker, executions_fetcher=self.executions,
            account_fetcher=self.account, pending_fetcher=self.pending, now=lambda: self.now, monotonic=lambda: self.clock)
        for quantity, price in ((None,"1"),("1",None),(0,"1"),("1",0),(True,"1"),("1",True),
                                ("1.5","1"),("1","1.5"),("1" * 13,"1"),("1","1" * 13)):
            with self.subTest(quantity=quantity, price=price), self.assertRaises(PaperReconciliationError):
                raw_gate("097520", side="buy", quantity=quantity, price=price)
        self.executions.assert_not_called()
        self.account.assert_not_called()

    def test_zero_holding_does_not_invent_basis_and_inconsistent_zero_is_rejected(self):
        row = {"stk_cd":"097520", "crd_tp":"00", "rmnd_qty":"0", "trde_able_qty":"0"}
        for value in (None, "", "0", "+000000"):
            self.account.return_value=account([{**row,"pur_amt":value}])
            self.assertEqual(self.gate.available_buy_principal("097520"), 1000000)
        for value in ("1", "-1", "junk"):
            self.account.return_value=account([{**row,"pur_amt":value}])
            with self.assertRaises(PaperReconciliationError): self.gate.available_buy_principal("097520")

    def test_unready_real_expired_or_missing_session_fails_before_reads(self):
        for change in (lambda: self.status.update(ready=False),
                       lambda: setattr(self.session, "mode", "real"),
                       lambda: setattr(self.session, "_issued", IssuedToken("expired", "20200101000000")),
                       lambda: setattr(self.session, "execution_recorder", None)):
            with self.subTest(change=change):
                self.status["ready"] = True
                self.session.mode = "paper"
                self.session._issued = IssuedToken("offline-token", "20300101000000")
                self.session.execution_recorder = self.recorder
                change()
                with self.assertRaises(PaperReconciliationError):
                    self.gate("097520")
        self.executions.assert_not_called()

    def test_missing_or_invalid_recorder_fields_fail_closed(self):
        for field, value in (("ready", 1), ("gap_count", None), ("gap_count", True),
                             ("gap_count", -1), ("observations", None), ("error", "failed"),
                             ("ingress_sequence", None), ("ingress_sequence", True), ("ingress_sequence", -1),
                             ("inflight_callbacks", None), ("inflight_callbacks", True), ("inflight_callbacks", 1),
                             ("engine_ingress_sequence", None), ("engine_ingress_sequence", True),
                             ("engine_pending_callbacks", None), ("engine_pending_callbacks", True),
                             ("engine_pending_callbacks", 1)):
            with self.subTest(field=field, value=value):
                original = deepcopy(self.status)
                self.status[field] = value
                with self.assertRaises(PaperReconciliationError):
                    self.gate("097520")
                self.status = original
        self.executions.assert_not_called()

    def test_gap_or_execution_during_any_read_prevents_a_valid_guard(self):
        for reader, change in ((self.executions, "gap_count"), (self.account, "observations"),
                               (self.pending, "gap_count")):
            with self.subTest(reader=reader):
                def mutate(*args, **kwargs):
                    self.status[change] += 1
                    return reader.return_value
                reader.side_effect = mutate
                with self.assertRaises(PaperReconciliationError):
                    self.gate("097520")
                reader.side_effect = None

    def test_token_change_during_read_prevents_ledger_ingest(self):
        def change(*args, **kwargs):
            self.session._issued = IssuedToken("replacement-token", "20300101000000")
            return ()
        self.executions.side_effect = change
        with self.assertRaises(PaperReconciliationError):
            self.gate("097520")
        self.tracker._ingest.assert_not_called()
        self.account.assert_not_called()

    def test_queued_guard_rejects_change_without_any_new_reads(self):
        mutations = (
            lambda: self.status.update(ready=False),
            lambda: self.status.update(gap_count=self.status["gap_count"] + 1),
            lambda: self.status.update(observations=self.status["observations"] + 1),
            lambda: setattr(self.session, "mode", "real"),
            lambda: setattr(self.session, "_issued", IssuedToken("changed-token", "20300101000000")),
            lambda: setattr(self.session, "execution_recorder", Mock(status=lambda: dict(self.status))),
            lambda: setattr(self, "now", self.now + timedelta(days=1)),
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                self.session.mode = "paper"
                self.session._issued = IssuedToken("offline-token", "20300101000000")
                self.session.execution_recorder = self.recorder
                self.status["ready"] = True
                guard = self.gate("097520")
                counts = (self.executions.call_count, self.account.call_count, self.pending.call_count)
                mutation()
                with self.assertRaises(PaperReconciliationError):
                    guard()
                self.assertEqual(counts, (self.executions.call_count, self.account.call_count, self.pending.call_count))

    def test_twenty_second_lifetime_and_backwards_or_invalid_clock(self):
        guard = self.gate("097520")
        self.clock = 120
        guard()
        for clock in (120.0001, 99.9, float("nan"), float("inf"), True):
            with self.subTest(clock=clock):
                self.clock = clock
                with self.assertRaises(PaperReconciliationError):
                    guard()

    def test_slow_account_or_pending_read_cannot_make_old_balance_look_fresh(self):
        for reader in (self.account, self.pending):
            with self.subTest(reader=reader):
                self.clock = 100
                def slow(*args, **kwargs):
                    self.clock = 120.001
                    return reader.return_value
                reader.side_effect = slow
                with self.assertRaises(PaperReconciliationError):
                    self.gate("097520")
                reader.side_effect = None

    def test_total_age_not_only_last_pending_completion_is_bounded(self):
        def slow_pending(*args, **kwargs):
            self.clock = 119
            return []
        self.pending.side_effect = slow_pending
        guard = self.gate("097520")
        self.clock = 120
        guard()  # Exactly 20 seconds since first request is allowed.
        self.clock = 120.001
        with self.assertRaises(PaperReconciliationError):
            guard()

    def test_clock_reversal_during_read_is_rejected(self):
        def reverse(*args, **kwargs):
            self.clock = 99
            return self.account.return_value
        self.account.side_effect = reverse
        with self.assertRaises(PaperReconciliationError):
            self.gate("097520")

    def test_real_recorder_inflight_event_blocks_gate_until_new_reconciliation(self):
        from tests.test_execution_stream import FakeEngine, row
        from kiwoom_autotrader.realtime.execution_stream import ExecutionStreamRecorder
        engine = FakeEngine(); self.session.websocket_engine = engine
        recorder = ExecutionStreamRecorder(self.session, self.tracker, now=lambda: self.now, poll_interval=5)
        self.addCleanup(recorder.stop)
        self.session.execution_recorder = recorder
        recorder.start(); engine.ready = True; self.assertTrue(recorder.status()["ready"])
        gate = self.make_gate()
        old_guard = gate("097520")
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def slow_store(event):
            if event["event_kind"] == "execution_stream":
                entered.set()
                if not release.wait(2): raise RuntimeError("fixture timeout")
        self.tracker.record_broker_observation.side_effect = slow_store
        worker = threading.Thread(target=lambda: engine.emit(row()))
        worker.start(); self.assertTrue(entered.wait(1))
        self.assertEqual(recorder.status()["inflight_callbacks"], 1)
        with self.assertRaises(PaperReconciliationError): gate("097520")
        with self.assertRaises(PaperReconciliationError): old_guard()
        release.set(); worker.join(2); self.assertFalse(worker.is_alive())
        self.assertEqual(recorder.status()["inflight_callbacks"], 0)
        with self.assertRaises(PaperReconciliationError): old_guard()
        gate("097520")()  # A new reconciliation after persistence is valid.

    def test_missing_or_inconsistent_account_fields_fail_closed(self):
        for report in (None, {}, account(None) | {"holdings": None},
                       account() | {"snapshot_changed": True}, account() | {"mode": "real"},
                       account() | {"exchange": "NXT"}, account([{}]),
                       account([{"stk_cd": "097520", "crd_tp": "00", "rmnd_qty": "1", "trde_able_qty": "2"}])):
            with self.subTest(report=report):
                self.account.return_value = report
                with self.assertRaises(PaperReconciliationError):
                    self.gate("097520")
        self.pending.assert_not_called()

    def test_missing_malformed_or_wrong_exchange_pending_rows_fail_closed(self):
        bad_rows = (None, {}, [None], [{}], [pending(oso_qty=None)], [pending(oso_qty="-1")],
                    [pending(ord_no="0")], [pending(stex_tp="2")], [pending(stk_cd="bad")])
        for rows in bad_rows:
            with self.subTest(rows=rows):
                self.pending.return_value = rows
                with self.assertRaises(PaperReconciliationError):
                    self.gate("097520")

    def test_bad_execution_result_or_wrong_day_is_not_accepted_as_empty(self):
        old = Execution("20261026", "1", "2", "097520", "name", "buy", "12:00:00", Decimal("100"), 1)
        for data in (None, {}, [None], [old]):
            with self.subTest(data=data):
                self.executions.return_value = data
                with self.assertRaises(PaperReconciliationError):
                    self.gate("097520")
        self.tracker._ingest.assert_not_called()

    def test_reader_or_storage_error_never_returns_guard_or_leaks_details(self):
        for dependency in (self.executions, self.account, self.pending,
                           self.tracker._ingest, self.tracker._holdings):
            with self.subTest(dependency=dependency):
                dependency.side_effect = OSError("private-token account-secret")
                with self.assertRaises(PaperReconciliationError) as raised:
                    self.gate("097520")
                self.assertNotIn("private-token", str(raised.exception))
                self.assertNotIn("account-secret", str(raised.exception))
                dependency.side_effect = None

    def test_default_reader_contract_uses_all_symbols_all_sides_and_krx(self):
        with patch("kiwoom_autotrader.trading.paper_reconciliation.fetch_executions", return_value=()) as executions, \
             patch("kiwoom_autotrader.trading.paper_reconciliation.fetch_account_report", return_value=account()), \
             patch("kiwoom_autotrader.trading.paper_reconciliation._request_rows", return_value=[]) as rows:
            gate = PaperReconciliationGate(self.session, self.tracker, now=lambda: self.now, monotonic=lambda: self.clock)
            gate("097520", quantity="1", price="100")()
        rows.assert_called_once_with(self.session, "ka10075", {"all_stk_tp": "0", "trde_tp": "0", "stex_tp": "1"}, "oso",
                                     observer=self.tracker.record_broker_observation, day="20261027")
        executions.assert_called_once_with(self.session, "20261027", observer=self.tracker.record_broker_observation)
        self.assertEqual(self.tracker._ingest.call_args.kwargs["source_kind"], "broker_observation")

    def test_default_pending_query_persists_each_safe_page_and_keeps_duplicates(self):
        from kiwoom_autotrader.api import executions as api
        from unittest.mock import MagicMock
        def response(payload, headers):
            raw = MagicMock()
            raw.headers, raw.status = headers, 200
            raw.read.return_value = json.dumps(payload, ensure_ascii=False).encode()
            wrapper = MagicMock()
            wrapper.__enter__.return_value = raw
            return wrapper
        raw_row = {"acnt_no": "1234567890", "mang_empno": "secret-employee",
            "stk_cd": "A005930", "stk_nm": "삼성전자", "ord_no": "0000007",
            "ord_stt": "접수", "tsk_tp": "현금", "ord_qty": "7", "ord_pric": "70000",
            "oso_qty": "2", "tm": "125930", "stex_tp": "1", "stex_tp_txt": "KRX",
            "sor_yn": "N", "io_tp_nm": "+매수", "unknown": "private-value"}
        pages = [response({"return_code": 0, "oso": [raw_row]}, {"cont-yn": "Y", "next-key": "opaque-private"}),
                 response({"return_code": 0, "oso": [raw_row]}, {"cont-yn": "N"})]
        with tempfile.TemporaryDirectory() as directory, patch.object(api, "queued_open", side_effect=pages):
            tracker = ExecutionTracker(log_dir=Path(directory), now=lambda: self.now,
                executions_fetcher=self.executions, account_fetcher=self.account)
            gate = PaperReconciliationGate(self.session, tracker, executions_fetcher=self.executions,
                account_fetcher=self.account, now=lambda: self.now, monotonic=lambda: self.clock)
            gate("097520", quantity="1", price="100")()
            with tracker._db() as db:
                saved = [json.loads(row[0]) for row in db.execute(
                    "SELECT payload_json FROM broker_observations WHERE source_api='ka10075' ORDER BY id")]
                self.assertEqual(len(saved), 2)
                self.assertEqual([event["page"] for event in saved], [1, 2])
                self.assertEqual(saved[0]["query_id"], saved[1]["query_id"])
                self.assertEqual([event["day"] for event in saved], ["20261027", "20261027"])
                self.assertEqual(saved[0]["response_body"]["oso"], saved[1]["response_body"]["oso"])
                self.assertEqual(saved[0]["response_body"]["oso"][0]["ord_stt"], "접수")
                self.assertEqual(saved[0]["response_body"]["oso"][0]["tm"], "125930")
                with self.assertRaises(Exception):
                    db.execute("UPDATE broker_observations SET payload_json='{}' WHERE source_api='ka10075'")
            serialized = json.dumps(saved, ensure_ascii=False)
            for secret in (self.session.token, "1234567890", "acnt_no", "mang_empno", "secret-employee", "private-value", "opaque-private"):
                self.assertNotIn(secret, serialized)
            exported = (Path(directory) / "20261027.broker-observations.jsonl").read_text()
            self.assertEqual(len([item for item in map(json.loads, exported.splitlines()) if item["payload"]["source_api"] == "ka10075"]), 2)

    def test_default_pending_failed_second_page_is_durable_but_never_allows_order(self):
        from kiwoom_autotrader.api import executions as api
        from unittest.mock import MagicMock
        def response(payload, headers):
            raw = MagicMock(); raw.headers, raw.status = headers, 200
            raw.read.return_value = json.dumps(payload).encode()
            wrapper = MagicMock(); wrapper.__enter__.return_value = raw
            return wrapper
        pages = [response({"return_code": 0, "oso": []}, {"cont-yn": "Y", "next-key": "cursor"}),
                 response({"return_code": 9, "return_msg": "private-server-message"}, {})]
        with tempfile.TemporaryDirectory() as directory, patch.object(api, "queued_open", side_effect=pages) as queue:
            tracker = ExecutionTracker(log_dir=Path(directory), now=lambda: self.now,
                executions_fetcher=self.executions, account_fetcher=self.account)
            gate = PaperReconciliationGate(self.session, tracker, executions_fetcher=self.executions,
                account_fetcher=self.account, now=lambda: self.now, monotonic=lambda: self.clock)
            with self.assertRaises(PaperReconciliationError) as caught:
                gate("097520", quantity="1", price="100")
            self.assertNotIn("private-server-message", str(caught.exception))
            self.assertEqual(queue.call_count, 2)
            with tracker._db() as db:
                saved = [json.loads(row[0]) for row in db.execute(
                    "SELECT payload_json FROM broker_observations WHERE source_api='ka10075' ORDER BY id")]
            self.assertEqual([event["status"] for event in saved], ["success", "broker_error"])
            self.assertEqual(saved[1]["response_body"], {"return_code": 9})

    def test_default_pending_observation_failure_stops_before_following_page(self):
        from kiwoom_autotrader.api import executions as api
        from unittest.mock import MagicMock
        raw = MagicMock(); raw.headers = {"cont-yn": "Y", "next-key": "cursor"}; raw.status = 200
        raw.read.return_value = b'{"return_code": 0, "oso": []}'
        wrapper = MagicMock(); wrapper.__enter__.return_value = raw
        self.tracker.record_broker_observation.side_effect = OSError("private-ledger-failure")
        with patch.object(api, "queued_open", return_value=wrapper) as queue:
            gate = PaperReconciliationGate(self.session, self.tracker, executions_fetcher=self.executions,
                account_fetcher=self.account, now=lambda: self.now, monotonic=lambda: self.clock)
            with self.assertRaises(PaperReconciliationError) as caught:
                gate("097520", quantity="1", price="100")
        self.assertEqual(queue.call_count, 1)
        self.assertNotIn("private-ledger-failure", str(caught.exception))

    def test_sensitive_account_and_staff_values_repeated_in_allowed_fields_are_redacted(self):
        from kiwoom_autotrader.api import executions as api
        from unittest.mock import MagicMock
        raw = MagicMock(); raw.headers = {}; raw.status = 200
        raw.read.return_value = json.dumps({"return_code": 0, "oso": [
            {"acnt_no": "1234567890", "mang_empno": "private-staff-456", "stk_nm": "계좌1234567890",
             "tsk_tp": "private-staff-456", "stk_cd": "005930", "ord_no": "7", "oso_qty": "0", "stex_tp": "1"}]}, ensure_ascii=False).encode()
        wrapper = MagicMock(); wrapper.__enter__.return_value = raw
        events = []
        with patch.object(api, "queued_open", return_value=wrapper):
            rows = api._request_rows(self.session, "ka10075", {"all_stk_tp": "0", "trde_tp": "0", "stex_tp": "1"},
                                     "oso", observer=events.append, day="20261027")
        self.assertEqual(len(rows), 1)
        saved = events[0]["response_body"]["oso"][0]
        self.assertNotIn("stk_nm", saved)
        self.assertNotIn("tsk_tp", saved)
        self.assertNotIn("1234567890", json.dumps(events, ensure_ascii=False))
        self.assertNotIn("private-staff-456", json.dumps(events, ensure_ascii=False))

    def test_default_pending_rejects_session_or_day_change_after_observation(self):
        from kiwoom_autotrader.api import executions as api
        from unittest.mock import MagicMock
        for mutation in (lambda: setattr(self.session, "mode", "real"),
                         lambda: setattr(self, "now", self.now + timedelta(days=1))):
            with self.subTest(mutation=mutation):
                self.session.mode = "paper"
                self.now = datetime(2026, 10, 27, 13, 0, 5, tzinfo=KST)
                raw = MagicMock(); raw.headers = {}; raw.status = 200
                def read(*args):
                    mutation()
                    return b'{"return_code": 0, "oso": []}'
                raw.read.side_effect = read
                wrapper = MagicMock(); wrapper.__enter__.return_value = raw
                with patch.object(api, "queued_open", return_value=wrapper):
                    gate = PaperReconciliationGate(self.session, self.tracker, executions_fetcher=self.executions,
                        account_fetcher=self.account, now=lambda: self.now, monotonic=lambda: self.clock)
                    with self.assertRaises(PaperReconciliationError):
                        gate("097520", quantity="1", price="100")

    def test_actual_temporary_tracker_projects_reported_snapshot_without_unit_inference(self):
        with tempfile.TemporaryDirectory() as directory:
            tracker = ExecutionTracker(log_dir=Path(directory), now=lambda: self.now,
                executions_fetcher=self.executions, account_fetcher=self.account)
            receipt = OrderReceipt("paper", "buy", "097520", "49", "20150", "0000001", "mockapi.kiwoom.com")
            tracker.register_order(receipt, "gdcrs")
            execution = Execution("20261027", "1", "8", "097520", "엠씨넥스", "buy", "12:59:00", Decimal("20150"), 27)
            self.executions.return_value = (execution,)
            self.account.return_value = account([{"stk_cd": "A097520", "crd_tp": "00",
                                                   "rmnd_qty": "27", "trde_able_qty": "27"}])
            self.make_gate(tracker)("097520", side="sell", quantity="27")()
            with tracker._db() as db:
                fills = list(db.execute("SELECT quantity,price FROM fills"))
                self.assertEqual([(row["quantity"], row["price"]) for row in fills], [(27, "20150")])
                self.assertEqual(db.execute("SELECT COUNT(*) FROM execution_observations").fetchone()[0], 2)


if __name__ == "__main__":
    unittest.main()
