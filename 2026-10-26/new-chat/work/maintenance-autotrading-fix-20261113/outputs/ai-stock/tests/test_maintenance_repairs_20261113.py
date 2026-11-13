"""Offline regression tests for 2026-11-13 maintenance defect repairs + EXP_QLS SHADOW observer."""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
import json
import os
import shutil
import sqlite3
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from kiwoom_autotrader.api import orders
from kiwoom_autotrader.api.session import BrokerSession, IssuedToken, KST
from kiwoom_autotrader.trading import paper_experiment_monitor as monitor_mod
from kiwoom_autotrader.trading.broker_observation_repeats import persist_observation
from kiwoom_autotrader.trading.decision_evidence import canonical_json
from kiwoom_autotrader.trading.execution_log import ExecutionTracker
from kiwoom_autotrader.trading.expired_order_certificates import verified_certificate
from kiwoom_autotrader.trading.paper_experiment_qls_shadow import (
    CHECKPOINT_SCHEMA,
    EXPIRES_ON,
    GATE_NAME,
    GATE_STATE,
    ORDER_PERMISSION,
    observe_qls_shadow,
)
from kiwoom_autotrader.trading.paper_experiment_store import ExperimentStore
from kiwoom_autotrader.trading.paper_policy import PaperAutomationPolicyError
from kiwoom_autotrader.trading.paper_reconciliation import PaperReconciliationError
from kiwoom_autotrader.utils import http_queue
from kiwoom_autotrader.utils.keychain import Credentials


TOKEN = "offline-defect-repair-token"
KEY = "offline-defect-repair-key"
SECRET = "offline-defect-repair-secret"


def _fake_session(clock=None):
    session = BrokerSession(
        now=clock or (lambda: datetime(2026, 11, 13, 10, 0, tzinfo=KST)),
        credential_loader=Mock(return_value=Credentials(KEY, SECRET)),
        token_issuer=Mock(return_value=IssuedToken(TOKEN, "20300101090000")),
    )
    session.login("paper")
    return session


def _broker_page(api, day, query_num, observed_at, rows, *, status="success"):
    started = observed_at - timedelta(milliseconds=20)
    if api == "kt00007":
        req = {
            "ord_dt": day,
            "qry_tp": "1",
            "stk_bond_tp": "0",
            "sell_tp": "0",
            "stk_cd": "",
            "fr_ord_no": "",
            "dmst_stex_tp": "KRX",
        }
        resp = {"return_code": 0, "acnt_ord_cntr_prps_dtl": rows} if status == "success" else {}
    elif api == "ka10075":
        req = {"all_stk_tp": "0", "trde_tp": "0", "stex_tp": "0", "stk_cd": ""}
        resp = {"return_code": 0, "oso": rows} if status == "success" else {}
    elif api == "kt00009":
        req = {
            "ord_dt": day,
            "qry_tp": "1",
            "stk_bond_tp": "0",
            "sell_tp": "0",
            "stk_cd": "",
            "fr_ord_no": "",
            "dmst_stex_tp": "KRX",
        }
        resp = {"return_code": 0, "acnt_ord_cntr_prst_array": rows} if status == "success" else {}
    else:
        raise ValueError(api)
    return {
        "schema": "kiwoom-broker-page-v1",
        "source_api": api,
        "source_kind": "broker_observation",
        "mode": "paper",
        "day": day,
        "query_id": f"{query_num:032x}",
        "request_started_at": started.isoformat(),
        "observed_at": observed_at.isoformat(),
        "page": 1,
        "request_body": req,
        "response_body": resp,
        "continuation": {
            "request_cont_yn": "N",
            "response_cont_yn": "N",
            "next_key_present": False,
        },
        "status": status,
        "http_status": 200 if status == "success" else None,
    }


class ExpiryObservationIndexAndContentionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="maintenance-repair-20261113-"))
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.now = datetime(2026, 11, 6, 9, 5, 0, tzinfo=KST)
        self.tracker = ExecutionTracker(log_dir=self.tmp / "logs", now=lambda: self.now)

    def test_expiry_index_steady_state_speed_new_instance_and_delta_invalidation(self):
        order_day = "20261105"
        with self.tracker._db() as db:
            db.execute(
                "INSERT INTO orders VALUES (?,?,?,?,?,?,?,?,?)",
                ("default", order_day, "paper", "10042", "005930", "buy", "EXP_R", 5, "2026-11-05T10:00:00+09:00"),
            )
            db.execute(
                "INSERT INTO intents VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                ("default", "intent-10042", order_day, "paper", "buy", "005930", 5, "EXP_R", "2026-11-05T09:59:59+09:00", "10042", "accepted", None),
            )
        detail_at = self.now - timedelta(seconds=20)
        pending_at = self.now - timedelta(seconds=10)
        self.tracker.record_broker_observation(
            _broker_page(
                "kt00007",
                order_day,
                1,
                detail_at,
                [
                    {
                        "ord_no": "0010042",
                        "ori_ord": "0000000",
                        "stk_cd": "A005930",
                        "io_tp_nm": "현금매수",
                        "dmst_stex_tp": "KRX",
                        "ord_qty": "5",
                        "cntr_qty": "0",
                        "ord_remnq": "0",
                    }
                ],
            )
        )
        self.tracker.record_broker_observation(
            _broker_page("ka10075", "20261106", 2, pending_at, [])
        )
        capture = self.tracker.record_paper_expired_settlements(self.now)
        self.assertEqual(capture["status"], "complete")
        self.assertEqual(capture["captured_orders"], ["10042"])

        # Append repeated ka10075 and kt00009 observations across multiple later days
        self.now = datetime(2026, 11, 13, 10, 30, 0, tzinfo=KST)
        with self.tracker._db() as db:
            qnum = 100
            for day in ("20261109", "20261110", "20261111", "20261112"):
                base_at = datetime.strptime(day + "100000", "%Y%m%d%H%M%S").replace(tzinfo=KST)
                for step in range(250):
                    qnum += 1
                    ev75 = _broker_page("ka10075", day, qnum, base_at + timedelta(seconds=step), [])
                    persist_observation(db, "default", ev75, canonical_json(ev75), epoch="bench")
                    qnum += 1
                    ev09 = _broker_page(
                        "kt00009",
                        day,
                        qnum,
                        base_at + timedelta(seconds=step),
                        [{"ord_no": "0099999", "orig_ord_no": "0000000", "stk_cd": "A000660", "cntr_qty": "1", "ord_qty": "1", "io_tp_nm": "현금매수", "mdfy_cncl_tp": "일반"}],
                    )
                    persist_observation(db, "default", ev09, canonical_json(ev09), epoch="bench")

        order_row = {
            "scope": "default",
            "day": order_day,
            "mode": "paper",
            "ord_no": "10042",
            "symbol": "005930",
            "side": "buy",
            "strategy": "EXP_R",
            "quantity": 5,
            "registered_at": "2026-11-05T10:00:00+09:00",
            "filled_quantity": 0,
        }

        bare_tracker = ExecutionTracker.__new__(ExecutionTracker)
        bare_tracker.scope = "default"
        with self.tracker._ro_db() as ro_db:
            self.assertTrue(verified_certificate(bare_tracker, ro_db, order_row, self.now))
            self.assertEqual(bare_tracker._confirmed_expired_quantity(ro_db, order_row, self.now), 5)
            started = time.perf_counter()
            for _ in range(30):
                self.assertEqual(bare_tracker._confirmed_expired_quantity(ro_db, order_row, self.now), 5)
            elapsed = time.perf_counter() - started
        self.assertLess(elapsed, 0.5, f"30 steady-state checks took {elapsed:.3f}s")

        # Incremental delta invalidation: appending a ka10075 row mentioning ord_no 10042 must fail closed immediately
        self.tracker.record_broker_observation(
            _broker_page(
                "ka10075",
                "20261113",
                999999,
                self.now - timedelta(seconds=1),
                [{"ord_no": "0010042", "orig_ord_no": "0000000"}],
            )
        )
        with self.tracker._ro_db() as ro_db:
            self.assertFalse(verified_certificate(bare_tracker, ro_db, order_row, self.now))
            self.assertEqual(bare_tracker._confirmed_expired_quantity(ro_db, order_row, self.now), 0)

    def test_record_broker_observation_retries_transient_sqlite_busy(self):
        calls = {"count": 0}
        real_persist = persist_observation

        def flaky_persist(*args, **kwargs):
            calls["count"] += 1
            if calls["count"] <= 2:
                raise sqlite3.OperationalError("database is locked")
            return real_persist(*args, **kwargs)

        event = {
            "schema_version": 1,
            "source_api": "00",
            "source_kind": "broker_observation",
            "observed_at": self.now.isoformat(),
            "day": "20261106",
            "mode": "paper",
            "event_kind": "stream_started",
            "values": {},
        }
        with patch("kiwoom_autotrader.trading.execution_log.persist_observation", side_effect=flaky_persist):
            self.tracker.record_broker_observation(event)
        self.assertEqual(calls["count"], 3)
        with self.tracker._ro_db() as ro_db:
            total = ro_db.execute("SELECT COUNT(*) FROM broker_observations").fetchone()[0]
        self.assertEqual(total, 1)

    def test_sidecar_materialization_and_balance_observations_table_present_under_ro_db(self):
        event = {
            "schema_version": 1,
            "source_api": "00",
            "source_kind": "broker_observation",
            "observed_at": self.now.isoformat(),
            "day": "20261106",
            "mode": "paper",
            "event_kind": "stream_started",
            "values": {},
        }
        self.tracker.record_broker_observation(event)
        writer = sqlite3.connect(self.tracker.path, timeout=0.1)
        self.addCleanup(writer.close)
        writer.execute("BEGIN IMMEDIATE")
        try:
            self.tracker._materialize("20261106")
        finally:
            writer.rollback()
        self.assertTrue((self.tracker.log_dir / "20261106.broker-observations.jsonl").is_file())
        self.assertTrue((self.tracker.log_dir / "20261106.execution-accounting.json").is_file())
        self.assertTrue((self.tracker.log_dir / "20261106.csv").is_file())
        with self.tracker._ro_db() as ro_db:
            exists = ro_db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='paper_account_balance_observations'"
            ).fetchone()
            self.assertIsNotNone(exists)


class OrderReasonCodePreservationTests(unittest.TestCase):
    def setUp(self):
        self.home_tmp = Path(tempfile.mkdtemp(prefix="order-reason-home-"))
        self.addCleanup(lambda: shutil.rmtree(self.home_tmp, ignore_errors=True))
        env_patcher = patch.dict(os.environ, {"HOME": str(self.home_tmp)})
        self.addCleanup(env_patcher.stop)
        env_patcher.start()
        self.now = datetime(2026, 11, 13, 10, 0, 0, tzinfo=KST)
        self.session = _fake_session(clock=lambda: self.now)
        patcher = patch.object(orders.urllib.request, "build_opener")
        self.addCleanup(patcher.stop)
        self.factory = patcher.start()
        self.opener = self.factory.return_value

    def test_reconciliation_and_policy_guard_reason_codes_survive_pre_prepare_and_queue(self):
        cases = [
            (
                PaperReconciliationError("대사 증빙 만료", reason_code="reconciliation_proof_expired"),
                "reconciliation_proof_expired",
            ),
            (
                PaperReconciliationError("대사 잔고 변경", reason_code="reconciliation_state_changed"),
                "reconciliation_state_changed",
            ),
            (
                PaperAutomationPolicyError("신호 창 만료", reason_code="signal_window_expired"),
                "signal_window_expired",
            ),
            (
                PaperAutomationPolicyError("시간 청산 창 만료", reason_code="timed_exit_window_expired"),
                "timed_exit_window_expired",
            ),
            (
                PaperAutomationPolicyError("실시간 스트림 미검증", reason_code="stream_unverified"),
                "stream_unverified",
            ),
            (
                PaperAutomationPolicyError("유지보수 일시정지", reason_code="maintenance_paused"),
                "maintenance_paused",
            ),
        ]
        for guard_exc, expected_code in cases:
            with self.subTest(phase="before_prepare", expected=expected_code):
                guard = Mock()
                guard.before_prepare = Mock(side_effect=guard_exc)
                guard.reconciliation_evidence = None
                with patch.object(orders, "capture_automatic_paper_guard", return_value=guard):
                    with self.assertRaises(orders.OrderError) as ctx:
                        orders.submit_order(self.session, "buy", "005930", "1", "70000")
                self.assertFalse(ctx.exception.uncertain)
                self.assertEqual(ctx.exception.reason_code, expected_code)
                self.opener.open.assert_not_called()

            with self.subTest(phase="queued_before_send", expected=expected_code):
                guard = Mock(side_effect=guard_exc)
                guard.before_prepare = Mock(return_value=False)
                guard.reconciliation_evidence = None
                with patch.object(orders, "capture_automatic_paper_guard", return_value=guard):
                    with self.assertRaises(orders.OrderError) as ctx:
                        orders.submit_order(self.session, "buy", "005930", "1", "70000")
                self.assertFalse(ctx.exception.uncertain)
                self.assertEqual(ctx.exception.reason_code, expected_code)
                self.opener.open.assert_not_called()

    def test_unknown_guard_exception_collapses_to_bounded_code_without_secret_leakage(self):
        leaked_secret = "Bearer SUPER_SECRET_VALUE_999"
        with self.assertRaises(orders.OrderError) as ctx:
            orders.submit_order(
                self.session,
                "buy",
                "005930",
                "1",
                "70000",
                before_send=Mock(side_effect=RuntimeError(leaked_secret)),
            )
        self.assertFalse(ctx.exception.uncertain)
        self.assertEqual(ctx.exception.reason_code, "pre_send_guard_rejected")
        self.assertNotIn("SUPER_SECRET_VALUE_999", str(ctx.exception))
        self.opener.open.assert_not_called()

        not_sent = http_queue.HTTPNotSentError("미전송", reason_code=leaked_secret)
        self.assertIsNone(not_sent.reason_code)
        self.assertNotIn("SUPER_SECRET_VALUE_999", str(not_sent))


class ClosingFallbackAndQlsShadowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="qls-shadow-d-fallback-"))
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.store_path = self.tmp / "experiment.sqlite3"
        self.shadow_dir = self.tmp / "qls-shadow"
        self.shadow_dir.mkdir(mode=0o700)
        self.clock = datetime(2026, 11, 13, 15, 18, 5, tzinfo=KST)
        self.settings = {
            "paper_experiment_enabled": True,
            "paper_experiment_s5_enabled": True,
            "gdcrs_stocks": [{"code": "005930"}, {"code": "000660"}],
            "paper_strategy_stocks": [],
        }

        class _StubTracker:
            def multi_strategy_position(self, mode, symbol, arm, now):
                return {"status": "flat", "owned_qty": 0, "basis_krw": 0}

            def multi_strategy_open_symbols(self, mode, now):
                return {"status": "available", "symbols": []}

        self.tracker = _StubTracker()
        self.session = SimpleNamespace(
            mode="paper",
            token="fixture",
            paper_automation_reconcile=SimpleNamespace(_tracker=self.tracker),
        )
        self.provider = SimpleNamespace(
            fetch=Mock(
                side_effect=lambda symbol, now: {
                    "symbol": symbol,
                    "mode": "paper",
                    "current_price": 70000,
                    "day_open": 70000,
                    "request_started_at": now.isoformat(),
                    "observed_at": now.isoformat(),
                    "source_sha256": "b" * 64,
                }
            ),
            discover_dynamic_candidates=Mock(return_value={"candidates": []}),
            fetch_s5_inputs=Mock(return_value={}),
        )
        self.calendar = lambda now: {
            "start": now.replace(hour=9, minute=0, second=0, microsecond=0),
            "end": now.replace(hour=15, minute=30, second=0, microsecond=0),
            "session_day": now.strftime("%Y%m%d"),
            "calendar_source_sha256": "c" * 64,
        }
        self.addCleanup(patch.stopall)
        patch.object(monitor_mod, "session_identity", side_effect=lambda s: (s.mode, s.token)).start()
        patch.object(monitor_mod, "record_signal_safely", return_value=None).start()
        self.monitor = monitor_mod.PaperExperimentMonitor(
            settings_loader=lambda: self.settings,
            now=lambda: self.clock,
            store_path=self.store_path,
            chart_fetcher=Mock(return_value=[]),
            provider_factory=lambda s: self.provider,
            account_fetcher=Mock(return_value={"cash": {"ord_alow_amt": 9_000_000}, "holdings": []}),
            submitter=Mock(),
            calendar_reader=self.calendar,
            qls_shadow_dir=self.shadow_dir,
        )
        self.monitor.store = ExperimentStore(self.store_path)

    def test_exp_d_1518_fallback_runs_when_extended_closing_expired_but_not_when_selected(self):
        day = "20261113"
        first_seen = "2026-11-13T08:50:10+09:00"
        for sym in ("005930", "000660"):
            self.monitor.store.selection(day, "first_seen:" + sym, {"symbol": sym, "first_seen_at": first_seen})
        self.monitor.store.selection(
            day,
            "extended:closing",
            {"day": day, "status": "selection_window_expired", "assignments": []},
        )
        self.monitor.tick(self.session, Mock())
        fallback = self.monitor.store.selection(day, "D")
        self.assertIsNotNone(fallback)
        self.assertEqual(len(fallback["assignments"]), 2)
        self.assertEqual({a["arm"] for a in fallback["assignments"]}, {"EXP_D"})

        # Verify that when extended:closing succeeded ('selected'), D fallback is NOT overwritten/created
        store2 = ExperimentStore(self.tmp / "experiment2.sqlite3")
        for sym in ("005930", "000660"):
            store2.selection(day, "first_seen:" + sym, {"symbol": sym, "first_seen_at": first_seen})
        store2.selection(day, "extended:closing", {"day": day, "status": "selected", "assignments": []})
        self.monitor.store = store2
        self.monitor.tick(self.session, Mock())
        self.assertIsNone(store2.selection(day, "D"))

    def test_qls_shadow_zero_order_authority_and_auto_expiry(self):
        self.assertEqual(GATE_NAME, "EXP_QLS")
        self.assertEqual(GATE_STATE, "SHADOW")
        self.assertEqual(EXPIRES_ON, "2026-11-27")
        self.assertFalse(ORDER_PERMISSION)

        # Write a valid prospective SHADOW checkpoint for 2026-11-13
        ckpt = self.shadow_dir / "20261113-qls-shadow-checkpoint.json"
        ckpt.write_text(
            json.dumps(
                {
                    "schema": CHECKPOINT_SCHEMA,
                    "day": "2026-11-13",
                    "would_have_ordered": [
                        {"symbol": "005930", "target_weight": 0.125, "rank": 1},
                        {"symbol": "000660", "target_weight": 0.10, "rank": 2},
                    ],
                }
            ),
            encoding="utf-8",
        )
        self.monitor.tick(self.session, Mock())
        status = self.monitor.status()
        # Strictly 14 active arms, EXP_QLS never in active_arms
        self.assertEqual(len(status["active_arms"]), 14)
        self.assertNotIn("EXP_QLS", status["active_arms"])
        qls = status["qls_shadow"]
        self.assertEqual(qls["gate"], "EXP_QLS")
        self.assertEqual(qls["state"], "SHADOW")
        self.assertFalse(qls["order_permission"])
        self.assertEqual(qls["submitted_orders"], 0)
        self.assertEqual(qls["status"], "observed")
        self.assertEqual(qls["would_have_ordered_count"], 2)

        # Auto-expiry on 2026-11-27 transitions gate to OFF automatically
        expired_status = observe_qls_shadow(
            self.monitor.store,
            datetime(2026, 11, 27, 9, 5, 0, tzinfo=KST),
            checkpoint_dir=self.shadow_dir,
        )
        self.assertEqual(expired_status["state"], "OFF")
        self.assertTrue(expired_status["expired"])
        self.assertFalse(expired_status["order_permission"])
        self.assertEqual(expired_status["submitted_orders"], 0)


if __name__ == "__main__":
    unittest.main()
