"""Durable fill ledger tests; all broker, clock and notification calls are fake."""
import csv
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

from kiwoom_autotrader.api.executions import Execution, RealizedProfit
from kiwoom_autotrader.api.orders import OrderReceipt
from kiwoom_autotrader.api.session import BrokerSession, IssuedToken, KST
from kiwoom_autotrader.trading.execution_log import ExecutionTracker, ExecutionLogError, FIELDS


def fill(number="1", quantity=2, price="100", *, order="24", side="sell", name="삼성전자", moment="10:00:00"):
    return Execution("20261026", order, number, "005930", name, side, moment, Decimal(price), quantity)


def holdings(quantity="7"):
    return {"mode": "paper", "exchange": "KRX", "snapshot_changed": False,
        "holdings": [{"stk_cd": "A005930", "rmnd_qty": quantity, "crd_tp": "00"}]}


class ExecutionLogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.now = datetime(2026, 10, 26, 10, 0, 5, tzinfo=KST)
        self.session = BrokerSession(now=lambda: self.now)
        self.session.mode = "paper"
        self.session._issued = IssuedToken("offline-tracker-private-token", "20300101000000")
        self.account = Mock(return_value=holdings())
        self.executions = Mock(return_value=(fill(),))
        self.profits = Mock(return_value=())
        self.notify = Mock()
        self.tracker = self.make_tracker()
        self.receipt = OrderReceipt("paper", "sell", "005930", "5", None, "00024", "https://mockapi.kiwoom.com")

    def make_tracker(self):
        # This suite exercises legacy ten-second closed-day re-polling; the
        # new bounded default is tested independently with an explicit clock.
        return ExecutionTracker(log_dir=self.directory, now=lambda: self.now,
            settings_loader=lambda: {"market_end_time": "15:30"}, account_fetcher=self.account,
            executions_fetcher=self.executions, profit_fetcher=self.profits, closed_day_backoff=False)

    def rows(self):
        with (self.directory / "20261026.csv").open(encoding="utf-8-sig", newline="") as source:
            return list(csv.DictReader(source))

    def tick(self, count=1):
        for _ in range(count):
            self.tracker.tick(self.session, self.notify)

    def register(self):
        self.tracker.register_order(self.receipt, "ddcrs")

    def observations(self):
        with sqlite3.connect(self.tracker.path) as database:
            return [json.loads(row[0]) for row in database.execute("SELECT payload_json FROM execution_observations ORDER BY id")]

    def broker_event(self):
        return {"schema":"kiwoom-broker-page-v1","source_api":"kt00009","source_kind":"broker_observation",
            "mode":"paper","day":"20261026","query_id":"a"*32,"request_started_at":self.now.isoformat(),
            "observed_at":self.now.isoformat(),"page":1,"request_body":{"ord_dt":"20261026"},
            "response_body":{"return_code":0,"acnt_ord_cntr_prst_array":[{"ord_no":"0000024","cntr_no":"0000001","cntr_qty":"0000000027"}]},
            "continuation":{"request_cont_yn":"N","response_cont_yn":"N","next_key_present":False},
            "status":"success","http_status":None}

    def test_same_key_27_to_49_preserves_partial_observation_and_reconciles_new_holdings(self):
        self.receipt=OrderReceipt("paper","sell","005930","49",None,"24","https://mockapi.kiwoom.com")
        self.register()
        self.tracker._ingest("paper","20261026",(fill(quantity=27),),source_kind="broker_observation")
        self.tracker._holdings("paper",holdings("22"),self.now)
        first=self.rows()[0]
        self.now+=timedelta(seconds=10)
        self.tracker._ingest("paper","20261026",(fill(quantity=49,moment="10:00:10"),),source_kind="broker_observation")
        self.assertEqual((self.rows()[0]["수량"],self.rows()[0]["남은수량"]),("49","미확정"))
        self.tracker._holdings("paper",holdings("0"),self.now)
        self.assertEqual((len(self.rows()),self.rows()[0]["수량"],self.rows()[0]["남은수량"]),(1,"49","0"))
        events=self.observations()
        self.assertEqual([event["kind"] for event in events],["new_execution_key","holdings_reconciliation","same_key_quantity_increase","holdings_reconciliation"])
        self.assertEqual(events[2]["previous"]["remaining"],"22")
        self.assertEqual(events[2]["previous"]["holdings_at"],first["잔고조회시각"])
        self.assertEqual([event["current"]["quantity"] for event in events],[27,27,49,49])
        self.assertTrue(all("delta_quantity" not in event for event in events))
        history=self.directory/"20261026.execution-history.jsonl"
        self.assertEqual(len(history.read_text().splitlines()),4)
        self.assertEqual(history.stat().st_mode & 0o777,0o600)

    def test_reordered_and_repeated_snapshots_survive_restart_without_duplicate_fill(self):
        self.register()
        self.tracker._ingest("paper","20261026",(fill(quantity=4,moment="10:00:02"),))
        self.tracker._ingest("paper","20261026",(fill(quantity=2,moment="10:00:00"),))
        self.assertEqual(self.rows()[0]["수량"],"4")
        self.assertEqual(self.observations()[-1]["kind"],"out_of_order_timestamp")
        self.assertFalse(self.observations()[-1]["projection_applied"])
        self.tracker=self.make_tracker()
        self.tracker._ingest("paper","20261026",(fill(quantity=4,moment="10:00:02"),))
        repeated=self.observations()[-1]
        self.assertEqual(repeated["kind"],"repeated_snapshot");self.assertTrue(repeated["after_recorder_restart"])
        self.assertFalse(repeated["projection_applied"]);self.assertEqual(len(self.rows()),1)

    def test_same_key_correction_is_not_a_new_unit_fill_and_old_report_cannot_reconcile(self):
        self.register()
        self.tracker._ingest("paper","20261026",(fill(quantity=4),))
        self.tracker._holdings("paper",holdings("5"),self.now)
        old=self.now;self.now+=timedelta(seconds=10)
        self.tracker._ingest("paper","20261026",(fill(quantity=3,price="101"),))
        self.assertEqual(self.observations()[-1]["kind"],"same_key_correction")
        self.tracker._holdings("paper",holdings("5"),old)
        self.assertEqual(self.rows()[0]["남은수량"],"미확정")
        self.tracker._holdings("paper",holdings("6"),self.now)
        self.assertEqual((self.rows()[0]["수량"],self.rows()[0]["가격"],self.rows()[0]["남은수량"]),("3","101","6"))

    def test_legacy_projection_migrates_once_with_original_holdings_and_unknown_discovery_time(self):
        self.register()
        with sqlite3.connect(self.tracker.path) as database:
            database.execute("INSERT INTO fills VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("default","20261026","paper","24","1","005930","삼성전자","sell","10:00:00","100",2,"ddcrs","7",self.now.isoformat(),"legacy"))
        self.tracker._ingest("paper","20261026",(fill(),))
        legacy=self.observations()[0]
        self.assertEqual(legacy["kind"],"legacy_projection");self.assertIsNone(legacy["original_observed_at"])
        self.assertEqual(legacy["current"]["remaining"],"7")
        self.tracker=self.make_tracker();self.tracker._ingest("paper","20261026",(fill(),))
        self.assertEqual(sum(item["kind"]=="legacy_projection" for item in self.observations()),1)
        with sqlite3.connect(self.tracker.path) as database:
            for sql in ("DELETE FROM execution_observations","UPDATE execution_observations SET kind='changed'"):
                with self.assertRaises(sqlite3.IntegrityError):database.execute(sql)

    def test_raw_api_and_ws_deliveries_are_immutable_separate_from_projection(self):
        event=self.broker_event()
        self.tracker.record_broker_observation(event);self.tracker.record_broker_observation(event)
        ws={"schema_version":1,"source_api":"00","source_kind":"broker_observation","observed_at":self.now.isoformat(),
            "day":"20261026","mode":"paper","event_kind":"execution_stream",
            "values":{"9203":"24","909":"1","910":"100","911":"49","914":"101","915":"22","919":""}}
        self.tracker.record_broker_observation(ws)
        self.tracker.record_broker_observation({**ws,"event_kind":"stream_gap","values":{}})
        with sqlite3.connect(self.tracker.path) as database:
            self.assertEqual(database.execute("SELECT COUNT(*) FROM fills").fetchone()[0],0)
            self.assertEqual(database.execute("SELECT COUNT(*) FROM broker_observations").fetchone()[0],3)
            self.assertEqual(database.execute("SELECT COUNT(*) FROM broker_observation_repeats").fetchone()[0],1)
            for sql in ("DELETE FROM broker_observations","UPDATE broker_observations SET mode='real'"):
                with self.assertRaises(sqlite3.IntegrityError):database.execute(sql)
        exported=[json.loads(line) for line in (self.directory/"20261026.broker-observations.jsonl").read_text().splitlines()]
        self.assertEqual(exported[1]["payload"]["values"],ws["values"])
        self.assertEqual(exported[0]["payload"]["response_body"]["acnt_ord_cntr_prst_array"][0]["cntr_qty"],"0000000027")

    def test_raw_observation_rejects_secret_fields_and_surfaces_persistence_failure(self):
        event=self.broker_event();event["authorization"]="private-token"
        with self.assertRaises(ExecutionLogError):self.tracker.record_broker_observation(event)
        event=self.broker_event();event["response_body"]["acnt_ord_cntr_prst_array"][0]["account_number"]="private-account"
        with self.assertRaises(ExecutionLogError):self.tracker.record_broker_observation(event)
        event=self.broker_event()
        with patch.object(self.tracker,"_db",side_effect=OSError("private-account")):
            with self.assertRaisesRegex(ExecutionLogError,"영구 기록") as caught:self.tracker.record_broker_observation(event)
        self.assertNotIn("private-account",str(caught.exception))

    def test_gap_reason_is_allowlisted_and_legacy_gap_remains_accepted(self):
        event = {"schema_version":1,"source_api":"00","source_kind":"broker_observation",
                 "observed_at":self.now.isoformat(),"day":"20261026","mode":"paper",
                 "event_kind":"stream_gap","values":{"gap_reason":"connection_generation_changed"}}
        self.tracker.record_broker_observation(event)
        self.tracker.record_broker_observation({**event,"values":{}})
        with sqlite3.connect(self.tracker.path) as database:
            rows = database.execute("SELECT payload_json FROM broker_observations ORDER BY id").fetchall()
        self.assertEqual(json.loads(rows[0][0])["values"]["gap_reason"], "connection_generation_changed")
        with self.assertRaises(ExecutionLogError):
            self.tracker.record_broker_observation({**event,"values":{"gap_reason":"secret-token"}})
        with self.assertRaises(ExecutionLogError):
            self.tracker.record_broker_observation({**event,"values":{"gap_reason":"callback_error","token":"secret-token"}})
        diagnosed = {"gap_reason": "callback_error", "prior_ready": "true", "current_ready": "false",
                     "prior_callback_errors": "0", "current_callback_errors": "1",
                     "prior_mode": "paper", "current_mode": "paper"}
        self.tracker.record_broker_observation({**event, "values": diagnosed})
        with self.assertRaises(ExecutionLogError):
            self.tracker.record_broker_observation({**event, "values": {**diagnosed, "current_mode": "private-token"}})

    def test_default_fetchers_receive_durable_observer_but_injected_contract_stays_compatible(self):
        event=self.broker_event()
        def fetch(session,day,*,observer):observer(event);return (fill(),)
        def profit(session,symbol,day,*,observer):
            self.assertEqual(observer,self.tracker.record_broker_observation);return ()
        with patch("kiwoom_autotrader.trading.execution_log.fetch_executions",side_effect=fetch) as executions_mock,patch("kiwoom_autotrader.trading.execution_log.fetch_realized_profit",side_effect=profit) as profit_mock:
            self.tracker=ExecutionTracker(log_dir=self.directory,now=lambda:self.now,account_fetcher=self.account,settings_loader=lambda:{"market_end_time":"15:30"},daily_profit_fetcher=Mock(),period_profit_fetcher=Mock())
            self.register();self.now=self.now.replace(hour=15,minute=31);self.tick(4)
        self.assertTrue(callable(executions_mock.call_args.kwargs["observer"]))
        profit_mock.assert_called_once()

    def test_rounded_projection_gross_and_broker_reported_net_are_preserved_not_forced_to_match(self):
        self.register();self.tracker._ingest("paper","20261026",(fill(quantity=3,price="100.01"),))
        costs=(RealizedProfit("005930",3,Decimal(100),Decimal(80),Decimal(2),Decimal(1),reported_net=Decimal(57)),)
        self.tracker._profit("paper","20261026","005930",costs)
        with sqlite3.connect(self.tracker.path) as database:
            net,reason=database.execute("SELECT net,reason FROM profits").fetchone()
            observation=json.loads(database.execute("SELECT payload_json FROM profit_observations").fetchone()[0])
        self.assertIsNone(net);self.assertIn("대금 불일치",reason)
        amounts=observation["amount_reconciliation"]
        self.assertEqual((amounts["projection_gross"],amounts["profit_rows_gross"],amounts["broker_reported_net"]),("300.03","300","57"))
        self.assertFalse(amounts["tolerance_applied"])

    def test_conflicting_reported_net_and_invalid_optional_cost_clear_previous_confirmation(self):
        self.register();self.tracker._ingest("paper","20261026",(fill(quantity=3),))
        good=RealizedProfit("005930",3,Decimal(100),Decimal(80),Decimal(2),Decimal(1),reported_net=Decimal(57))
        self.tracker._profit("paper","20261026","005930",(good,))
        with sqlite3.connect(self.tracker.path) as database:self.assertEqual(database.execute("SELECT net FROM profits").fetchone()[0],"57")
        bad=RealizedProfit("005930",3,Decimal(100),Decimal(80),Decimal(2),Decimal(1),reported_net=Decimal(58))
        self.tracker._profit("paper","20261026","005930",(bad,))
        with sqlite3.connect(self.tracker.path) as database:
            net,reason=database.execute("SELECT net,reason FROM profits").fetchone();self.assertIsNone(net);self.assertIn("보고손익",reason)
        bad=RealizedProfit("005930",3,Decimal(100),Decimal(80),Decimal(2),Decimal(1),reported_net=Decimal("NaN"))
        self.tracker._profit("paper","20261026","005930",(bad,))
        with sqlite3.connect(self.tracker.path) as database:self.assertIsNone(database.execute("SELECT net FROM profits").fetchone()[0])

    def test_legacy_stale_holdings_are_archived_then_cleared_even_for_repeated_fill(self):
        self.now=self.now.replace(hour=10,minute=54,second=33)
        self.receipt=OrderReceipt("paper","sell","005930","49",None,"24","https://mockapi.kiwoom.com")
        self.register();old_at=self.now.replace(second=22).isoformat()
        with sqlite3.connect(self.tracker.path) as database:
            database.execute("INSERT INTO fills VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("default","20261026","paper","24","1","005930","삼성전자","sell","10:54:29","20392",49,"ddcrs","22",old_at,"legacy"))
        self.tracker._ingest("paper","20261026",(fill(quantity=49,price="20392",moment="10:54:29"),))
        self.assertEqual(self.rows()[0]["남은수량"],"미확정")
        observations=self.observations()
        self.assertEqual(observations[0]["kind"],"legacy_projection")
        self.assertEqual(observations[0]["current"]["remaining"],"22")
        self.assertEqual(observations[-1]["kind"],"stale_holdings_link_cleared")
        self.tracker._holdings("paper",holdings("0"),self.now)
        self.assertEqual((self.rows()[0]["남은수량"],self.rows()[0]["잔고조회시각"]),("0",self.now.isoformat()))

    def test_report_before_broker_fill_time_never_links_remaining_quantity(self):
        self.register()
        self.tracker._ingest("paper","20261026",(fill(moment="10:00:10"),))
        self.tracker._holdings("paper",holdings("0"),self.now)
        self.assertEqual(self.rows()[0]["남은수량"],"미확정")
        self.now+=timedelta(seconds=10);self.tracker._holdings("paper",holdings("0"),self.now)
        self.assertEqual(self.rows()[0]["남은수량"],"0")

    def test_history_appends_new_ids_and_repairs_external_or_failed_exports(self):
        event=self.broker_event()
        from kiwoom_autotrader.trading import execution_log as module
        with patch.object(module.os,"replace",wraps=module.os.replace) as replace:
            self.tracker.record_broker_observation(event)
            self.assertEqual(replace.call_count,1)
            self.tracker.record_broker_observation(event)
            self.assertEqual(replace.call_count,2)  # first compact receipt file, original not rewritten
        target=self.directory/"20261026.broker-observations.jsonl"
        with target.open("a") as output:output.write("incomplete external tail")
        self.tracker.record_broker_observation(event)
        self.assertEqual(len([json.loads(line) for line in target.read_text().splitlines()]),1)
        repeats=self.directory/"20261026.broker-observation-repeats.jsonl"
        self.assertEqual(len(repeats.read_text().splitlines()),2)
        with patch.object(module.os,"fsync",side_effect=OSError("offline full disk")):
            with self.assertRaises(ExecutionLogError):self.tracker.record_broker_observation(event)
        self.tracker.record_broker_observation(event)
        records=[json.loads(line) for line in target.read_text().splitlines()]+[json.loads(line) for line in repeats.read_text().splitlines()]
        self.assertEqual(len(records),5);self.assertEqual(len({row["id"] for row in records}),5)

    def test_concurrent_rest_and_ws_history_exports_cannot_lose_latest_rows(self):
        event=self.broker_event()
        ws={"schema_version":1,"source_api":"00","source_kind":"broker_observation","observed_at":self.now.isoformat(),
            "day":"20261026","mode":"paper","event_kind":"stream_gap","values":{}}
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures=[executor.submit(self.tracker.record_broker_observation, event if index%2 else ws) for index in range(12)]
            for future in futures:future.result()
        with sqlite3.connect(self.tracker.path) as database:
            count=database.execute("SELECT (SELECT COUNT(*) FROM broker_observations)+(SELECT COUNT(*) FROM broker_observation_repeats)").fetchone()[0]
        records=[json.loads(line) for line in (self.directory/"20261026.broker-observations.jsonl").read_text().splitlines()]
        records += [json.loads(line) for line in (self.directory/"20261026.broker-observation-repeats.jsonl").read_text().splitlines()]
        self.assertEqual(count,12);self.assertEqual(len(records),count)
        self.assertEqual(len({row["id"] for row in records}),count)

    def test_accepted_order_does_not_fabricate_fill_and_preserves_other_strategy(self):
        self.register()
        self.assertFalse((self.directory / "20261026.csv").exists())
        self.executions.return_value = ()
        self.tick(2)
        self.assertEqual(self.rows(), [])
        with self.assertRaises(ExecutionLogError):
            self.tracker.register_order(self.receipt, "different-strategy")

    def test_partial_fills_dedup_and_observed_held_quantity_not_order_remaining(self):
        self.register()
        self.executions.return_value = (fill(), fill("2", 1, "101"), fill(order="999"))
        self.tick()
        self.assertEqual([row["남은수량"] for row in self.rows()], ["미확정", "미확정"])
        self.tick()
        data = self.rows()
        self.assertEqual(len(data), 2)
        self.assertEqual([row["수량"] for row in data], ["2", "1"])
        self.assertTrue(all(row["남은수량"] == "7" for row in data))
        self.assertTrue(all("체결순간 수량 아님" in row["잔고상태"] for row in data))
        self.assertTrue(all(row["잔고조회시각"] == self.now.isoformat() for row in data))
        self.assertEqual(data[0]["체결시간"], "20261026 10:00:00")
        self.assertEqual(list(data[0]), FIELDS)
        self.tracker = self.make_tracker()
        self.register()  # idempotent after process restart
        self.tick(2)
        self.assertEqual(len(self.rows()), 2)

    def test_report_failure_or_unknown_credit_type_does_not_invent_zero_holdings(self):
        self.register()
        self.account.side_effect = OSError("private-detail")
        self.tick(2)
        self.assertEqual(self.rows()[0]["남은수량"], "미확정")
        self.assertNotIn("private-detail", self.notify.call_args.args[0])
        self.account.side_effect = None
        unknown = holdings()
        unknown["holdings"][0]["crd_tp"] = None
        self.account.return_value = unknown
        self.now += timedelta(seconds=10)
        self.tick(2)
        self.assertEqual(self.rows()[0]["남은수량"], "미확정")

    def test_daily_net_cost_fees_tax_and_late_fill_correction(self):
        self.register()
        self.now = self.now.replace(hour=15, minute=31)
        self.executions.return_value = (fill(quantity=3),)
        self.profits.return_value = (RealizedProfit("005930", 3, Decimal(100), Decimal(80), Decimal(2), Decimal(1)),)
        self.tick(4)
        summary = self.rows()[-1]
        self.assertEqual((summary["행구분"], summary["매수합계"], summary["매도합계"]), ("일일합계", "0", "300"))
        self.assertEqual((summary["순이익"], summary["수익률분모"], summary["수익률"]), ("57", "240", "23.7500%"))
        self.notify.assert_called_once()
        self.now += timedelta(seconds=10)
        self.tick(4)
        self.notify.assert_called_once()  # identical daily digest is not resent
        self.executions.return_value += (fill("2", quantity=2, price="110", moment="15:30:10"),)
        self.profits.return_value += (RealizedProfit("005930", 2, Decimal(110), Decimal(80), Decimal(2), Decimal(1)),)
        self.account.return_value = holdings("5")
        self.now += timedelta(seconds=10)
        self.tick(4)
        self.assertEqual(len(self.rows()), 3)
        self.assertEqual(self.rows()[0]["남은수량"], "7")  # earlier observation is preserved
        self.assertEqual(self.rows()[1]["남은수량"], "5")
        self.assertEqual(self.rows()[-1]["순이익"], "114")
        self.assertEqual(self.notify.call_count, 2)
        self.assertIn("정정", self.notify.call_args.args[0])

    def test_mixed_manual_sell_or_unavailable_cost_keeps_net_unknown(self):
        self.register()
        self.now = self.now.replace(hour=15, minute=31)
        self.executions.return_value = (fill(), fill(order="999", quantity=1))
        self.profits.return_value = (RealizedProfit("005930", 3, Decimal(100), Decimal(80), Decimal(0), Decimal(0)),)
        self.tick(4)
        self.assertEqual(self.rows()[-1]["순이익"], "미확정")
        self.assertEqual(self.rows()[-1]["매도합계"], "200")
        self.executions.return_value = (fill(),)
        self.profits.side_effect = OSError("unavailable cost")
        self.now += timedelta(seconds=10)
        self.tick(4)
        self.assertEqual(self.rows()[-1]["순이익"], "미확정")

    def test_notification_failure_is_not_replayed_on_restart(self):
        self.register()
        self.now = self.now.replace(hour=15, minute=31)
        self.notify.side_effect = OSError("delivery uncertain")
        self.tick(4)
        self.tracker = self.make_tracker()
        self.notify = Mock()
        self.tick(4)
        self.notify.assert_not_called()

    def test_holdings_differences_never_create_fill_rows(self):
        self.register()
        self.executions.return_value = ()
        self.tick(2)
        self.now += timedelta(seconds=10)
        self.account.return_value = holdings("99")
        self.tick(2)
        self.assertEqual(self.rows(), [])
        with sqlite3.connect(self.tracker.path) as db:
            self.assertIn("005930", db.execute("SELECT discrepancy FROM holdings").fetchone()[0])

    def test_session_change_mid_read_discards_result(self):
        self.register()
        def changed(*_):
            self.session.mode = "real"
            return (fill(),)
        self.executions.side_effect = changed
        self.tick()
        self.assertFalse((self.directory / "20261026.csv").exists())

    def test_overfill_transaction_rolls_back_and_csv_formula_text_is_escaped(self):
        self.register()
        self.executions.return_value = (fill(quantity=6),)
        self.tick()
        with sqlite3.connect(self.tracker.path) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM fills").fetchone()[0], 0)
        self.executions.return_value = (fill(name='=HYPERLINK("x")'),)
        self.now += timedelta(seconds=10)
        self.tick(2)
        self.assertTrue(self.rows()[0]["종목명"].startswith("'="))

    def test_nominal_ten_second_cycle_and_no_request_without_login(self):
        self.tick(2)  # full source then holdings, even when all orders are manual
        self.account.assert_called_once()
        self.assertEqual(self.tracker.seconds_until_next(), 10)
        self.now += timedelta(seconds=9)
        self.tick()
        self.account.assert_called_once()
        self.now += timedelta(seconds=1)
        self.tick(2)
        self.assertEqual(self.account.call_count, 2)
        self.session.clear()
        self.tick()
        self.assertIsNone(self.tracker.seconds_until_next())

    def test_presend_intent_links_receipt_and_connection_is_closed(self):
        identifier = self.tracker.prepare_order("paper", "sell", "005930", "5", "ddcrs")
        with self.tracker._db() as db:
            self.assertEqual(db.execute("SELECT state FROM intents WHERE id=?", (identifier,)).fetchone()[0], "prepared")
        with self.assertRaises(sqlite3.ProgrammingError):
            db.execute("SELECT 1")
        self.tracker.register_order(self.receipt, "ddcrs", intent_id=identifier)
        with self.tracker._db() as db:
            row = db.execute("SELECT state,order_number FROM intents WHERE id=?", (identifier,)).fetchone()
            self.assertEqual(tuple(row), ("accepted", "24"))
        with self.assertRaises(ExecutionLogError):
            self.tracker.cancel_intent(identifier)

    def test_pending_gate_covers_old_intents_both_sides_and_releases_only_known_unsent(self):
        intent = self.tracker.prepare_order("paper", "buy", "005930", "5", "gdcrs", require_no_pending=True)
        self.now += timedelta(days=1)
        with self.assertRaises(ExecutionLogError):
            self.tracker.assert_no_pending_order("paper", "005930")
        with self.assertRaises(ExecutionLogError):
            self.tracker.prepare_order("paper", "sell", "005930", "5", "ddcrs", require_no_pending=True)
        self.tracker.assert_no_pending_order("real", "005930")
        self.tracker.assert_no_pending_order("paper", "000660")
        other = ExecutionTracker(log_dir=self.directory, account_scope="other")
        other.assert_no_pending_order("paper", "005930")
        self.tracker.cancel_intent(intent)
        self.tracker.assert_no_pending_order("paper", "005930")

    def test_pending_gate_partial_fill_complete_repeat_roundtrip_and_malformed_quantity(self):
        intent = self.tracker.prepare_order("paper", "sell", "005930", "5", "ddcrs", require_no_pending=True)
        self.tracker.register_order(self.receipt, "ddcrs", intent_id=intent)
        self.tracker._ingest("paper", "20261026", (fill(quantity=2),))
        with self.assertRaises(ExecutionLogError): self.tracker.assert_no_pending_order("paper", "005930")
        self.tracker._ingest("paper", "20261026", (fill(quantity=5),))
        self.tracker.assert_no_pending_order("paper", "005930")
        next_intent = self.tracker.prepare_order("paper", "buy", "005930", "3", "gdcrs", require_no_pending=True)
        with self.assertRaises(ExecutionLogError): self.tracker.assert_no_pending_order("paper", "005930")
        self.tracker.cancel_intent(next_intent)
        with self.tracker._db() as db: db.execute("UPDATE orders SET quantity=NULL")
        with self.assertRaises(ExecutionLogError): self.tracker.assert_no_pending_order("paper", "005930")

    def test_pending_gate_atomic_competing_trackers_and_legacy_default_compatibility(self):
        def prepare(_):
            tracker = self.make_tracker()
            try:
                return tracker.prepare_order("paper", "buy", "005930", "1", "gdcrs", require_no_pending=True)
            except ExecutionLogError:
                return None
        with ThreadPoolExecutor(max_workers=2) as executor:
            attempts = list(executor.map(prepare, range(2)))
        self.assertEqual(sum(value is not None for value in attempts), 1)
        # Existing non-policy callers retain their explicit legacy behavior.
        self.tracker.prepare_order("paper", "buy", "005930", "1", "gdcrs")
        with self.tracker._db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM intents").fetchone()[0], 2)

    def test_intraday_repeated_sell_refresh_preserves_old_cost_observations(self):
        self.register()
        first = RealizedProfit("005930", 2, Decimal(100), Decimal(80), Decimal(2), Decimal(1))
        self.profits.return_value = (first,)
        self.tick(3)
        with self.tracker._db() as db: self.assertEqual(db.execute("SELECT net FROM profits").fetchone()[0], "37")
        self.tracker.register_order(replace(self.receipt, quantity="2", order_number="25"), "ddcrs")
        self.executions.return_value += (fill(order="25", quantity=2, price="110", moment="10:00:10"),)
        self.profits.return_value += (RealizedProfit("005930", 2, Decimal(110), Decimal(80), Decimal(2), Decimal(1)),)
        self.now += timedelta(seconds=10)
        self.tick()
        with self.tracker._db() as db: self.assertIsNone(db.execute("SELECT net FROM profits").fetchone()[0])
        self.tick(2)
        with self.tracker._db() as db:
            self.assertEqual(db.execute("SELECT net FROM profits").fetchone()[0], "94")
            observations = [json.loads(row[0]) for row in db.execute("SELECT payload_json FROM profit_observations")]
        self.assertEqual({row["net"] for row in observations}, {"37", "94"})
        self.assertEqual(len(self.rows()), 2)
        self.notify.assert_not_called()  # intraday accounting does not emit an EOD summary

    def test_cost_refresh_failure_clears_prior_net_and_unchanged_snapshots_are_throttled(self):
        self.register()
        self.profits.return_value = (RealizedProfit("005930", 2, Decimal(100), Decimal(80), Decimal(2), Decimal(1)),)
        self.tick(3)
        self.now += timedelta(seconds=10)
        self.tick(2)
        self.profits.assert_called_once()
        self.profits.side_effect = OSError("private detail")
        self.now += timedelta(seconds=50)
        self.tick(3)
        self.assertEqual(self.profits.call_count, 2)
        with self.tracker._db() as db:
            self.assertIsNone(db.execute("SELECT net FROM profits").fetchone()[0])
            self.assertTrue(any(json.loads(row[0])["net"] == "37" for row in db.execute("SELECT payload_json FROM profit_observations")))

    def test_manual_sell_or_missing_broker_row_invalidates_profit_even_after_restart(self):
        self.register()
        self.tracker._ingest("paper", "20261026", (fill(),))
        self.tracker._profit("paper", "20261026", "005930", (RealizedProfit("005930", 2, Decimal(100), Decimal(80), Decimal(2), Decimal(1)),))
        self.tracker = self.make_tracker()
        self.tracker._ingest("paper", "20261026", (fill(),))
        with self.tracker._db() as db: self.assertEqual(db.execute("SELECT net FROM profits").fetchone()[0], "37")
        self.tracker._ingest("paper", "20261026", (fill(), fill(order="999")))
        with self.tracker._db() as db: self.assertIsNone(db.execute("SELECT net FROM profits").fetchone()[0])
        self.tracker._ingest("paper", "20261026", ())
        with self.tracker._db() as db: self.assertIsNone(db.execute("SELECT net FROM profits").fetchone()[0])

    def test_multiple_symbols_cost_reads_are_bounded_and_do_not_starve(self):
        symbols = ("005930", "000660", "035420", "035720", "005380")
        for index, symbol in enumerate(symbols):
            self.tracker.register_order(replace(self.receipt, symbol=symbol, quantity="1", order_number=str(24 + index)), "ddcrs")
        self.executions.return_value = tuple(replace(fill(quantity=1, order=str(24 + index)), symbol=symbol) for index, symbol in enumerate(symbols))
        self.profits.side_effect = lambda _, symbol, day: (RealizedProfit(symbol, 1, Decimal(100), Decimal(80), Decimal(1), Decimal(1)),)
        self.tick(2)
        self.assertEqual(len(self.tracker._tasks), 4)
        self.tick(4)
        self.now += timedelta(seconds=10)
        self.tick(3)
        self.assertEqual({call.args[1] for call in self.profits.call_args_list}, set(symbols))
        with self.tracker._db() as db:
            self.assertEqual([row[0] for row in db.execute("SELECT net FROM profits")], ["18"] * 5)

    def test_daily_account_pages_are_observed_without_attribution_and_throttled(self):
        self.register()
        event = self.broker_event()
        event.update(source_api="ka10074", request_body={"strt_dt":"20261026","end_dt":"20261026"},
            response_body={"return_code":0,"tot_buy_amt":"00000160","tot_sell_amt":"00000200", "rlzt_pl":"999", "trde_cmsn":"2", "trde_tax":"1", "dt_rlzt_pl":[]})
        def daily(session, day, *, observer): observer(event)
        self.tracker.daily_profit_fetcher = Mock(side_effect=daily)
        self.profits.return_value = (RealizedProfit("005930", 2, Decimal(100), Decimal(80), Decimal(2), Decimal(1)),)
        self.tick(4)
        self.assertEqual(self.tracker.daily_profit_fetcher.call_count, 1)
        with self.tracker._db() as db:
            self.assertEqual(db.execute("SELECT net FROM profits").fetchone()[0], "37")  # account report never assigned as strategy net
            saved = json.loads(db.execute("SELECT payload_json FROM broker_observations WHERE source_api='ka10074'").fetchone()[0])
            self.assertEqual(saved["response_body"]["rlzt_pl"], "999")
        self.now += timedelta(seconds=10)
        self.tick(2)
        self.assertEqual(self.tracker.daily_profit_fetcher.call_count, 1)
        self.now += timedelta(seconds=50)
        self.tick(4)
        self.assertEqual(self.tracker.daily_profit_fetcher.call_count, 2)

    def test_new_fill_hides_stale_csv_summary_until_costs_reconcile(self):
        self.register()
        self.now = self.now.replace(hour=15, minute=31)
        self.profits.return_value = (RealizedProfit("005930", 2, Decimal(100), Decimal(80), Decimal(2), Decimal(1)),)
        self.tick(4)
        self.assertEqual(self.rows()[-1]["순이익"], "37")
        self.tracker._ingest("paper", "20261026", (fill(quantity=3),))
        self.assertEqual((self.rows()[-1]["매도합계"], self.rows()[-1]["순이익"]), ("300", "미확정"))

    def test_unresolved_intent_makes_eod_unknown_but_known_not_sent_can_be_cancelled(self):
        identifier = self.tracker.prepare_order("paper", "sell", "005930", "5", "ddcrs")
        self.now = self.now.replace(hour=15, minute=31)
        self.tick(3)
        self.assertEqual(self.rows()[-1]["순이익"], "미확정")
        self.assertIn("접수 미확정", self.rows()[-1]["비고"])
        self.tracker.cancel_intent(identifier, "not_sent")
        self.now += timedelta(seconds=10)
        self.tick(3)
        self.assertEqual(self.rows()[-1]["순이익"], "0")
        self.assertNotIn("접수 미확정", self.rows()[-1]["비고"])

    def test_weekday_zero_trade_summary_and_weekend_no_empty_summary(self):
        self.executions.return_value = ()
        self.now = self.now.replace(hour=15, minute=31)
        self.tick(3)
        self.assertEqual((self.rows()[-1]["매수합계"], self.rows()[-1]["매도합계"], self.rows()[-1]["순이익"]), ("0", "0", "0"))
        self.now = datetime(2026, 10, 31, 15, 31, tzinfo=KST)
        self.tracker = self.make_tracker()
        self.tick()
        self.assertFalse((self.directory / "20261031.csv").exists())

    def test_csv_rollup_rebuilds_export_and_rejects_trade_amount_mismatch(self):
        self.register()
        self.tick(2)
        target = self.directory / "20261026.csv"
        rows = self.rows()
        rows[0]["가격"] = "999999"
        with target.open("w", encoding="utf-8-sig", newline="") as output:
            writer = csv.DictWriter(output, fieldnames=FIELDS)
            writer.writeheader()
            writer.writerows(rows)
        # Simulate an edit after the normal rebuild: it must not become a
        # financial Telegram report merely because it is in the CSV.
        with patch.object(self.tracker, "_materialize", return_value=None):
            with self.assertRaises(ExecutionLogError):
                self.tracker._summary("paper", "20261026", self.notify)
        self.notify.assert_not_called()
        self.tracker._summary("paper", "20261026", self.notify)
        exported = self.rows()
        self.assertEqual(exported[0]["가격"], "100")
        amount = sum(Decimal(row["가격"]) * int(row["수량"]) for row in exported if row["행구분"] == "체결")
        self.assertEqual(Decimal(exported[-1]["매도합계"]), amount)
        self.assertIn("매도 합계 200원", self.notify.call_args.args[0])

    def test_older_than_seven_recorded_days_are_rotated_without_starvation(self):
        # Historical fairness applies after the protected current-day session.
        self.now = self.now.replace(hour=16)
        self.executions.return_value = ()
        for age in range(1, 10):
            self.tracker.register_order(self.receipt, "ddcrs", registered_at=self.now - timedelta(days=age))
        for _ in range(2):
            self.tick()
            while self.tracker._tasks:
                self.tick()
            self.now += timedelta(seconds=10)
        queried = {call.args[1] for call in self.executions.call_args_list}
        self.assertIn("20261017", queried)
        self.assertIn("20261026", queried)
        self.assertEqual(len(queried), 10)


if __name__ == "__main__":
    unittest.main()
