"""Offline expiry/cancellation contracts; no Keychain, network or real orders."""
from datetime import datetime, timedelta
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from kiwoom_autotrader.api import cancellations
from kiwoom_autotrader.api.cancellations import CancellationError, CancellationReceipt
from kiwoom_autotrader.api.session import BrokerSession, IssuedToken, KST
from kiwoom_autotrader.config.settings import (_validate, default_settings, effective_unfilled_buy_ttl,
    load_settings, paper_strategy_options_path)
from kiwoom_autotrader.trading.execution_log import ExecutionTracker
from kiwoom_autotrader.trading.golden_cross import GoldenCrossMonitor
from kiwoom_autotrader.trading.unfilled_buy_ttl import (UnfilledBuyTTLMonitor, TTLLedger, TTL_STRATEGIES,
    is_ttl_strategy, ttl_strategy_sql)
from kiwoom_autotrader.trading.cancellation_settlement import CancellationSettlementUnavailable
from kiwoom_autotrader.utils.http_queue import HTTPNotSentError


class TTLTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.now=datetime(2026,10,30,10,5,tzinfo=KST)
        self.tracker=ExecutionTracker(log_dir=Path(self.temp.name),now=lambda:self.now)
        with self.tracker._db() as db:
            pass
        self.session=BrokerSession(now=lambda:self.now)
        self.session.mode='paper'
        self.session._issued=IssuedToken('offline-cancel-private-token','20300101000000')
        self.stream={'ready':True,'error':None,'gap_count':0,'observations':1,
            'ingress_sequence':0,'engine_ingress_sequence':0,'inflight_callbacks':0,'engine_pending_callbacks':0}
        self.session.execution_recorder=SimpleNamespace(status=lambda:dict(self.stream))
        self.session.execution_recording_guard=Mock()
        self.session.paper_automation_reconcile=SimpleNamespace(_session=self.session,_tracker=self.tracker)
        self.pending=[{'ord_no':'0000001','stk_cd':'A005930','ord_qty':'10','ord_pric':'10000',
            'oso_qty':'10','orig_ord_no':'0000000','io_tp_nm':'+매수','stex_tp':'1','sor_yn':'N',
            'ord_stt':'접수','tm':'100000'}]
        self.fetch=Mock(side_effect=lambda *a,**k:list(self.pending))
        self.cancel=Mock(side_effect=self.ack)
        self.settle=Mock(side_effect=CancellationSettlementUnavailable('cancellation_ack_unconfirmed'))
        self.monitor=self.make_monitor()
        self.own_order()

    def make_monitor(self):
        return UnfilledBuyTTLMonitor(now=lambda:self.now,pending_fetcher=self.fetch,cancel_handler=self.cancel,
                                     settlement_handler=self.settle)

    def own_order(self, *, number='1', strategy='gdcrs', side='buy', accepted='2026-10-30T10:00:01+09:00',
                  prepared='2026-10-30T09:59:59+09:00', order_type='limit', state='accepted',day='20261030',mode='paper'):
        with sqlite3.connect(self.tracker.path) as db:
            db.execute('INSERT INTO orders VALUES (?,?,?,?,?,?,?,?,?)',
                ('default',day,mode,number,'005930',side,strategy,10,accepted))
            db.execute('INSERT INTO intents VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                ('default','intent'+number,day,mode,side,'005930',10,strategy,prepared,number,state,None))
            db.execute('INSERT INTO order_decisions VALUES (?,?,?,?,?,?,?)',
                ('default','intent'+number,prepared,'10000',order_type,None,None))

    def ack(self,session,symbol,number,*,before_send):
        before_send()
        return CancellationReceipt(number,'2',0)

    def counts(self):
        with sqlite3.connect(self.tracker.path) as db:
            names={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            return (db.execute('SELECT COUNT(*) FROM buy_ttl_cancellation_attempts').fetchone()[0]
                    if 'buy_ttl_cancellation_attempts' in names else 0,
                    db.execute('SELECT COUNT(*) FROM buy_ttl_cancellation_outcomes').fetchone()[0]
                    if 'buy_ttl_cancellation_outcomes' in names else 0)

    def fill(self,qty=3):
        with sqlite3.connect(self.tracker.path) as db:
            db.execute('INSERT INTO fills VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                ('default','20261030','paper','1','99','005930','삼성전자','buy','10:01:00','10000',qty,'gdcrs','7',None,None))

    def test_disabled_performs_zero_clock_database_queries_or_cancellations(self):
        self.monitor.now=Mock(side_effect=AssertionError('no clock'))
        self.monitor.tick(None,None,0)
        self.fetch.assert_not_called()
        self.cancel.assert_not_called()
        self.assertEqual(self.counts(),(0,0))

    def test_ttl_starts_from_ack_not_old_intent_and_boundary_inclusive(self):
        self.now=self.now.replace(hour=10,minute=3,second=0)
        self.monitor.tick(self.session,self.tracker,180)
        self.fetch.assert_not_called()
        self.now=self.now.replace(second=1)
        self.monitor=self.make_monitor()
        self.monitor.tick(self.session,self.tracker,180)
        self.cancel.assert_called_once()
        self.assertEqual(self.counts(),(1,1))

    def test_ack_does_not_change_original_financial_rows_or_release_principal(self):
        sidecars={name:Path(self.temp.name)/name for name in
            ('orders.csv','execution-accounting.json','broker-observations.jsonl')}
        for path in sidecars.values():path.write_bytes(b'unchanged-sidecar-fixture\n')
        with sqlite3.connect(self.tracker.path) as db:
            before={t:db.execute('SELECT * FROM '+t).fetchall() for t in
                    ('orders','intents','fills','holdings','account_balance_daily','pre_order_reconciliations')}
        self.monitor.tick(self.session,self.tracker,180)
        with sqlite3.connect(self.tracker.path) as db:
            after={t:db.execute('SELECT * FROM '+t).fetchall() for t in before}
            evidence=json.loads(db.execute('SELECT evidence_json FROM buy_ttl_cancellation_attempts').fetchone()[0])
            result=db.execute('SELECT original_ord_no,cancellation_ord_no,status FROM buy_ttl_cancellation_outcomes').fetchone()
        self.assertEqual(before,after)
        self.assertEqual(result,('1','2','cancel_input_ack_unconfirmed_settlement'))
        self.assertEqual(evidence['cancel_request_qty'],0)
        self.assertFalse(evidence['ack_releases_principal'])
        self.assertNotIn(self.session.token,json.dumps(evidence))
        self.assertFalse(self.monitor.last_status['principal_reservation_released'])
        for path in sidecars.values():self.assertEqual(path.read_bytes(),b'unchanged-sidecar-fixture\n')

    def test_partial_fills_cancel_only_broker_remainder(self):
        self.fill()
        self.pending[0]['oso_qty']='7'
        self.monitor.tick(self.session,self.tracker,180)
        self.cancel.assert_called_once()
        self.assertEqual(self.monitor.last_status['observed_remaining_qty'],7)

    def test_complete_fills_no_pending_query(self):
        self.fill(10)
        self.monitor.tick(self.session,self.tracker,180)
        self.fetch.assert_not_called()

    def test_manual_foreign_unknown_strategy_sell_market_and_unknown_ownership_excluded(self):
        for index,(kind,changes) in enumerate([('manual',{'strategy':'manual'}),('unknown_strategy',{'strategy':'unknown'}),
            ('sell',{'side':'sell'}),('market',{'order_type':'market'}),('unknown',{'state':'prepared'}),
            ('real',{'mode':'real'}),('past',{'day':'20261029'})]):
            with self.subTest(kind=kind):
                with sqlite3.connect(self.tracker.path) as db:
                    db.execute('DELETE FROM orders')
                    db.execute('DELETE FROM intents')
                    # Decisions are deliberately immutable; use a new ID.
                self.own_order(number=str(index+10),**changes)
                self.monitor=self.make_monitor()
                self.monitor.tick(self.session,self.tracker,180)
                self.fetch.assert_not_called()
                self.cancel.assert_not_called()

    def test_all_enabled_automatic_strategy_orders_due_without_early_release(self):
        for strategy in (*TTL_STRATEGIES, 'rsv#1', 'rsv#107'):
            with self.subTest(strategy=strategy):
                self.setUp()
                with sqlite3.connect(self.tracker.path) as db:
                    db.execute('UPDATE orders SET strategy=?',(strategy,))
                    db.execute('UPDATE intents SET strategy=?',(strategy,))
                self.monitor.tick(self.session,self.tracker,180)
                self.cancel.assert_called_once()
                self.assertFalse(self.monitor.last_status['principal_reservation_released'])
                with sqlite3.connect(self.tracker.path) as db:
                    evidence=json.loads(db.execute('SELECT evidence_json FROM buy_ttl_cancellation_attempts').fetchone()[0])
                self.assertEqual(evidence['strategy'],strategy)
                self.now+=timedelta(seconds=31)
                self.make_monitor().tick(self.session,self.tracker,180)
                self.cancel.assert_called_once()

    def test_reserved_order_requires_matching_accepted_automatic_intent(self):
        for changes in ("DELETE FROM intents", "UPDATE intents SET strategy='manual'",
                "UPDATE intents SET state='prepared'", "UPDATE intents SET scope='other-account'"):
            with self.subTest(changes=changes):
                self.setUp()
                with sqlite3.connect(self.tracker.path) as db:
                    db.execute("UPDATE orders SET strategy='rsv#7'")
                    db.execute("UPDATE intents SET strategy='rsv#7'")
                    db.execute(changes)
                self.monitor.tick(self.session,self.tracker,180)
                self.fetch.assert_not_called()
                self.cancel.assert_not_called()

    def test_reserved_strategy_lookalikes_and_manual_remain_excluded(self):
        invalid=('manual','unknown','rsv#','rsv#0','rsv#01','rsv#-1','rsv#+1',
            'rsv#1x','rsv#1.0','rsv#1\n','rsv#1\x00junk',' rsv#1','RSV#1','rsv#１')
        predicate,params=ttl_strategy_sql('strategy')
        with sqlite3.connect(':memory:') as db:
            db.execute('CREATE TABLE candidates(strategy TEXT)')
            db.executemany('INSERT INTO candidates VALUES(?)',[(s,) for s in (*TTL_STRATEGIES,'rsv#1','rsv#12345',*invalid)])
            selected={r[0] for r in db.execute('SELECT strategy FROM candidates WHERE '+predicate,params)}
        self.assertEqual(selected,set((*TTL_STRATEGIES,'rsv#1','rsv#12345')))
        for strategy in invalid:
            with self.subTest(strategy=strategy):
                self.assertFalse(is_ttl_strategy(strategy))
                self.setUp()
                with sqlite3.connect(self.tracker.path) as db:
                    db.execute('UPDATE orders SET strategy=?',(strategy,))
                    db.execute('UPDATE intents SET strategy=?',(strategy,))
                self.monitor.tick(self.session,self.tracker,180)
                self.fetch.assert_not_called()
                self.cancel.assert_not_called()

    def test_fill_with_conflicting_strategy_blocks_cancellation(self):
        self.fill(3);self.pending[0]['oso_qty']='7'
        with sqlite3.connect(self.tracker.path) as db:db.execute("UPDATE fills SET strategy='S2_surge_high_breakout'")
        self.monitor.tick(self.session,self.tracker,180)
        self.cancel.assert_not_called()
        self.assertEqual(self.monitor.last_status['reason'],'ttl_fill_ownership_conflict')

    def test_pending_row_mismatch_duplicate_modified_and_no_remainder_fail_closed(self):
        cases=[{'stk_cd':'000660'},{'io_tp_nm':'매도'},{'stex_tp':'2'},{'sor_yn':'Y'},
               {'orig_ord_no':'3'},{'ord_qty':'11'},{'ord_pric':'9999'},{'oso_qty':'11'},
               {'tm':'090000'},{'ord_stt':'unknown'},{'ord_no':'junk'}]
        original=dict(self.pending[0])
        for changes in cases:
            with self.subTest(changes=changes):
                self.pending=[{**original,**changes}]
                self.monitor=self.make_monitor()
                self.monitor.tick(self.session,self.tracker,180)
                self.cancel.assert_not_called()
        self.pending=[original,dict(original)]
        self.monitor=self.make_monitor();self.monitor.tick(self.session,self.tracker,180)
        self.cancel.assert_not_called()
        self.pending=[{**original,'oso_qty':'0'}]
        self.monitor=self.make_monitor();self.monitor.tick(self.session,self.tracker,180)
        self.cancel.assert_not_called()
        self.assertEqual(self.counts(),(0,0))

    def test_fill_during_query_blocks_before_claim(self):
        def fetch(*a,**k):
            self.fill(3)
            return self.pending
        self.fetch.side_effect=fetch
        self.monitor.tick(self.session,self.tracker,180)
        self.cancel.assert_not_called()
        self.assertEqual(self.counts(),(0,0))

    def test_fill_while_queued_keeps_claim_and_prevents_transport_and_restart_resend(self):
        def queue(session,symbol,number,*,before_send):
            self.fill(3)
            try:before_send()
            except Exception:raise CancellationError('cancel_not_sent')
            self.fail('stale evidence passed')
        self.cancel.side_effect=queue
        self.monitor.tick(self.session,self.tracker,180)
        self.assertEqual(self.counts(),(1,1))
        self.now+=timedelta(minutes=1)
        self.make_monitor().tick(self.session,self.tracker,180)
        self.cancel.assert_called_once()

    def test_timeout_and_crash_claims_are_never_resent_after_restart(self):
        self.cancel.side_effect=CancellationError('cancel_response_unconfirmed',uncertain=True)
        self.monitor.tick(self.session,self.tracker,180)
        self.now+=timedelta(minutes=1)
        self.make_monitor().tick(self.session,self.tracker,180)
        self.cancel.assert_called_once()
        self.assertEqual(self.counts(),(1,1))

    def test_proven_not_sent_retries_fresh_after_restart_preserving_original_claim(self):
        self.cancel.side_effect=CancellationError('cancel_not_sent')
        self.monitor.tick(self.session,self.tracker,180)
        with sqlite3.connect(self.tracker.path) as db:
            originals={t:db.execute('SELECT * FROM '+t).fetchall() for t in
                ('buy_ttl_cancellation_attempts','buy_ttl_cancellation_outcomes','orders','intents','fills')}
        self.cancel.side_effect=self.ack
        self.now+=timedelta(seconds=31)
        self.make_monitor().tick(self.session,self.tracker,180)
        self.assertEqual(self.cancel.call_count,2);self.assertEqual(self.fetch.call_count,2)
        self.settle.assert_not_called()
        with sqlite3.connect(self.tracker.path) as db:
            self.assertEqual(originals,{t:db.execute('SELECT * FROM '+t).fetchall() for t in originals})
            self.assertEqual(db.execute('SELECT attempt_sequence FROM buy_ttl_cancellation_retry_attempts').fetchall(),[(1,)])
            self.assertEqual(db.execute('SELECT status FROM buy_ttl_cancellation_retry_outcomes').fetchall(),[('cancel_input_ack_unconfirmed_settlement',)])
            for table in ('buy_ttl_cancellation_retry_attempts','buy_ttl_cancellation_retry_outcomes'):
                for sql in ('DELETE FROM '+table,'UPDATE '+table+' SET scope=scope'):
                    with self.assertRaises(sqlite3.IntegrityError):db.execute(sql)
        self.now+=timedelta(seconds=31);self.make_monitor().tick(self.session,self.tracker,180)
        self.assertEqual(self.cancel.call_count,2);self.settle.assert_called_once()

    def test_consecutive_not_sent_attempts_form_immutable_linear_history(self):
        self.cancel.side_effect=CancellationError('cancel_not_sent')
        for _ in range(3):
            self.make_monitor().tick(self.session,self.tracker,180);self.now+=timedelta(seconds=31)
        self.assertEqual(self.cancel.call_count,3)
        ledger=TTLLedger(self.tracker)
        with ledger.db() as db:
            saved,outcome=ledger.latest_attempt(db,'20261030','1')
            self.assertEqual(saved['attempt_sequence'],2);self.assertEqual(outcome['status'],'cancel_not_sent')
        self.cancel.side_effect=self.ack
        self.make_monitor().tick(self.session,self.tracker,180)
        self.assertEqual(self.cancel.call_count,4)

    def test_not_sent_retry_requires_new_matching_remainder_and_stream(self):
        self.cancel.side_effect=CancellationError('cancel_not_sent')
        self.monitor.tick(self.session,self.tracker,180)
        self.now+=timedelta(seconds=31);self.fill(3);self.pending[0]['oso_qty']='9'
        self.make_monitor().tick(self.session,self.tracker,180)
        self.assertEqual(self.cancel.call_count,1)
        self.pending[0]['oso_qty']='7';self.stream['ready']=False
        self.make_monitor().tick(self.session,self.tracker,180)
        self.assertEqual(self.cancel.call_count,1)
        self.stream['ready']=True;self.cancel.side_effect=self.ack
        self.make_monitor().tick(self.session,self.tracker,180)
        self.assertEqual(self.cancel.call_count,2)

    def test_retry_claim_crash_without_outcome_is_not_resent(self):
        self.cancel.side_effect=CancellationError('cancel_not_sent')
        self.monitor.tick(self.session,self.tracker,180)
        self.now+=timedelta(seconds=31)
        self.cancel.side_effect=SystemExit('fixture process died before outcome')
        with self.assertRaises(SystemExit):self.make_monitor().tick(self.session,self.tracker,180)
        self.now+=timedelta(seconds=31);self.cancel.side_effect=self.ack
        self.make_monitor().tick(self.session,self.tracker,180)
        self.assertEqual(self.cancel.call_count,2);self.settle.assert_called_once()

    def test_uncertain_not_sent_spelling_and_broker_rejection_never_retry(self):
        for error in (CancellationError('cancel_not_sent',uncertain=True),CancellationError('cancel_broker_rejected')):
            with self.subTest(error=str(error)):
                self.setUp();self.cancel.side_effect=error
                self.monitor.tick(self.session,self.tracker,180)
                self.now+=timedelta(seconds=31);self.make_monitor().tick(self.session,self.tracker,180)
                self.cancel.assert_called_once()

    def test_retry_claim_is_atomic_between_competing_monitors(self):
        self.cancel.side_effect=CancellationError('cancel_not_sent')
        self.monitor.tick(self.session,self.tracker,180);self.now+=timedelta(seconds=31)
        def interleave(session,symbol,number,*,before_send):
            self.make_monitor().tick(session,self.tracker,180)
            return self.ack(session,symbol,number,before_send=before_send)
        self.cancel.side_effect=interleave
        self.make_monitor().tick(self.session,self.tracker,180)
        self.assertEqual(self.cancel.call_count,2)

    def test_corrupt_retry_chain_cannot_authorize_another_dispatch(self):
        self.cancel.side_effect=CancellationError('cancel_not_sent')
        self.monitor.tick(self.session,self.tracker,180);self.now+=timedelta(seconds=31)
        self.make_monitor().tick(self.session,self.tracker,180);self.now+=timedelta(seconds=31)
        with sqlite3.connect(self.tracker.path) as db:
            db.execute('DROP TRIGGER buy_ttl_cancellation_retry_attempts_no_update')
            db.execute("UPDATE buy_ttl_cancellation_retry_attempts SET previous_outcome_sha256='bad'")
        monitor=self.make_monitor();monitor.tick(self.session,self.tracker,180)
        self.assertEqual(self.cancel.call_count,2)
        self.assertEqual(monitor.last_status['reason'],'ttl_retry_chain_unverified')

    def test_retry_after_uncertain_cancel_only_reconciles_never_resends(self):
        self.cancel.side_effect=CancellationError('cancel_response_unconfirmed',uncertain=True)
        self.monitor.tick(self.session,self.tracker,180)
        self.assertEqual(self.monitor.last_status['status'],'retry_pending')
        self.now+=timedelta(seconds=31)
        self.monitor.tick(self.session,self.tracker,180)
        self.cancel.assert_called_once()
        self.settle.assert_called_once()
        self.assertEqual(self.monitor.last_status['status'],'settlement_unconfirmed')
        self.assertFalse(self.monitor.last_status['principal_reservation_released'])
        self.now+=timedelta(seconds=31)
        self.monitor.tick(self.session,self.tracker,180)
        self.assertEqual(self.settle.call_count,2)
        self.cancel.assert_called_once()

    def test_unconfirmed_cancel_does_not_starve_another_symbol_due_buy(self):
        self.monitor.tick(self.session,self.tracker,180)
        self.own_order(number='3',strategy='EXP_B')
        with sqlite3.connect(self.tracker.path) as db:
            db.execute("UPDATE orders SET symbol='000660' WHERE ord_no='3'")
            db.execute("UPDATE intents SET symbol='000660' WHERE id='intent3'")
        self.pending.append(dict(self.pending[0],ord_no='0000003',stk_cd='000660'))
        self.now+=timedelta(seconds=31)
        def ack(session,symbol,number,*,before_send):
            before_send()
            return CancellationReceipt(number,'4',0)
        self.cancel.side_effect=ack
        self.monitor.tick(self.session,self.tracker,180)
        self.settle.assert_called_once()
        self.assertEqual(self.cancel.call_count,2)
        self.assertEqual(self.cancel.call_args.args[1:3],('000660','3'))
        self.assertFalse(self.monitor.last_status['principal_reservation_released'])

    def test_claim_is_atomic_between_competing_monitors(self):
        def interleave(session,symbol,number,*,before_send):
            other=self.make_monitor()
            other.tick(session,self.tracker,180)
            return self.ack(session,symbol,number,before_send=before_send)
        self.cancel.side_effect=interleave
        self.monitor.tick(self.session,self.tracker,180)
        self.cancel.assert_called_once()

    def test_mutable_cancellation_records_are_forbidden(self):
        self.monitor.tick(self.session,self.tracker,180)
        with sqlite3.connect(self.tracker.path) as db:
            for table in ('buy_ttl_cancellation_attempts','buy_ttl_cancellation_outcomes'):
                for sql in ('DELETE FROM '+table,'UPDATE '+table+' SET scope=scope'):
                    with self.assertRaises(sqlite3.IntegrityError):db.execute(sql)

    def test_session_change_stale_query_gap_callback_and_disable_prevent_send(self):
        for failure in ('session','age','gap','callback','disabled'):
            with self.subTest(failure=failure):
                self.monitor=self.make_monitor()
                self.stream.update(gap_count=0,inflight_callbacks=0)
                self.session._issued=IssuedToken('offline-cancel-private-token','20300101000000')
                def fetch(*a,**k):
                    if failure=='session':self.session._issued=IssuedToken('offline-changed-token','20300101000000')
                    if failure=='age':self.now+=timedelta(seconds=11)
                    if failure=='gap':self.stream['gap_count']+=1
                    if failure=='callback':self.stream['inflight_callbacks']=1
                    return self.pending
                self.fetch.side_effect=fetch
                self.monitor.tick(self.session,self.tracker,180,enabled_check=lambda:failure!='disabled')
                self.cancel.assert_not_called()
        self.assertEqual(self.counts(),(0,0))

    def test_read_failure_retries_verification_next_cycle_without_leaking_error(self):
        self.fetch.side_effect=OSError('PRIVATE remote detail')
        self.monitor.tick(self.session,self.tracker,180)
        self.now+=timedelta(minutes=1)
        self.monitor.tick(self.session,self.tracker,180)
        self.assertEqual(self.fetch.call_count,2)
        self.assertNotIn('PRIVATE',str(self.monitor.last_status))
        self.assertEqual(self.monitor.next_at,self.now+timedelta(seconds=30))
        self.assertFalse(self.monitor.last_status['principal_reservation_released'])
        self.fetch.side_effect=lambda *a,**k:list(self.pending)
        self.now+=timedelta(seconds=30)
        self.monitor.tick(self.session,self.tracker,180)
        self.cancel.assert_called_once()

    def test_foreign_tracker_or_session_binding_rejected_without_query(self):
        for key in ('_tracker','_session'):
            gate=SimpleNamespace(_tracker=self.tracker,_session=self.session)
            setattr(gate,key,object());self.session.paper_automation_reconcile=gate
            self.make_monitor().tick(self.session,self.tracker,180)
            self.fetch.assert_not_called();self.cancel.assert_not_called()
        self.assertEqual(self.counts(),(0,0))

    def test_gate_replacement_while_queued_blocks_transport_and_never_resends(self):
        transported=[]
        def queued(session,symbol,number,*,before_send):
            session.paper_automation_reconcile=SimpleNamespace(_tracker=self.tracker,_session=session)
            before_send();transported.append(number)
            return CancellationReceipt(number,'2',0)
        self.cancel.side_effect=queued
        self.monitor.tick(self.session,self.tracker,180)
        self.assertEqual(transported,[])
        self.assertEqual(self.counts(),(1,1))
        self.assertEqual(self.monitor.last_status['status'],'retry_pending')
        self.now+=timedelta(seconds=31)
        self.make_monitor().tick(self.session,self.tracker,180)
        self.cancel.assert_called_once()

    def test_monotonic_staleness_blocks_despite_unchanged_wall_clock(self):
        clock=[0.0]
        self.monitor.monotonic=lambda:clock[0]
        def fetch(*a,**k):
            clock[0]=11.0
            return self.pending
        self.fetch.side_effect=fetch
        self.monitor.tick(self.session,self.tracker,180)
        self.cancel.assert_not_called()
        self.assertEqual(self.counts(),(0,0))

    def test_claim_without_outcome_crash_survives_restart_and_reports_review(self):
        ledger=TTLLedger(self.tracker)
        due,watermark=ledger.due(self.now,180)
        self.assertTrue(ledger.claim(due[0],{'claimed_at':self.now.isoformat()},watermark))
        self.monitor.tick(self.session,self.tracker,180)
        self.fetch.assert_not_called();self.cancel.assert_not_called()
        self.assertEqual(self.monitor.last_status['status'],'settlement_unconfirmed')

    def test_golden_disabled_ttl_has_no_hook_and_enabled_runs_before_chart_cadence(self):
        settings={'gdcrs_enabled':True,'gdcrs_short_period':5,'gdcrs_long_period':20,
                  'gdcrs_stocks':[{'code':'005930','budget':1000000}]}
        ttl=Mock(next_at=None,last_status=None)
        chart=Mock(return_value=[])
        golden=GoldenCrossMonitor(settings_loader=lambda:dict(settings),chart_fetcher=chart,
            buy_handler=Mock(),now=lambda:self.now,ttl_monitor=ttl,state_path=Path(self.temp.name)/'markers.db')
        golden.tick(self.session,Mock())
        ttl.tick.assert_not_called()
        self.assertNotIn('unfilled_buy_ttl',golden.status())
        settings['gdcrs_unfilled_buy_ttl_seconds']=180
        self.session.paper_automation_reconcile=SimpleNamespace(_tracker=self.tracker)
        golden.tick(self.session,Mock())
        self.assertEqual(ttl.tick.call_args.args,(self.session,self.tracker,180))
        ttl.tick.reset_mock()
        golden._next_at=self.now+timedelta(minutes=1)
        golden.tick(self.session,Mock())
        ttl.tick.assert_called_once()
        ttl.next_at=self.now-timedelta(minutes=1)
        self.now=self.now.replace(hour=16)
        golden._next_at=self.now+timedelta(seconds=5)
        self.assertEqual(golden.seconds_until_next(),5)

    def test_legacy_defaults_and_paper_only_settings_validation(self):
        settings=default_settings()
        self.assertEqual(settings['gdcrs_unfilled_buy_ttl_seconds'],0)
        settings.pop('gdcrs_unfilled_buy_ttl_seconds')
        self.assertEqual(_validate(settings)['gdcrs_unfilled_buy_ttl_seconds'],0)
        for invalid in (-1,86401,True,180.0,'180'):
            with self.assertRaises(Exception):effective_unfilled_buy_ttl({'gdcrs_unfilled_buy_ttl_seconds':invalid},'paper')
        self.assertEqual(effective_unfilled_buy_ttl({'gdcrs_unfilled_buy_ttl_seconds':180},'real'),0)
        self.assertEqual(effective_unfilled_buy_ttl({'gdcrs_unfilled_buy_ttl_seconds':180},'paper'),180)


class CancellationTransportTests(unittest.TestCase):
    def setUp(self):
        self.session=BrokerSession(now=lambda:datetime(2026,10,30,10,tzinfo=KST))
        self.session.mode='paper'
        self.session._issued=IssuedToken('offline-cancel-token','20300101000000')
        self.session.execution_recording_guard=Mock()
        self.guard=Mock()
        self.payload={'return_code':0,'ord_no':'0000002','base_orig_ord_no':'0000001','cncl_qty':'000000000007'}

    def invoke(self,queued):
        with patch.object(cancellations,'queued_open',queued):
            return cancellations.cancel_paper_buy_remainder(self.session,'005930','0000001',before_send=self.guard)

    def transport(self,*args,**kwargs):
        kwargs['before_send']()
        response=Mock()
        response.headers={}
        response.read.return_value=json.dumps(self.payload).encode()
        context=Mock();context.__enter__=Mock(return_value=response);context.__exit__=Mock(return_value=False)
        return context

    def test_exact_paper_kt10003_contract_cancel_all_remaining_and_single_shared_queue(self):
        queue=Mock(side_effect=self.transport)
        receipt=self.invoke(queue)
        queue.assert_called_once()
        request=queue.call_args.args[1]
        self.assertEqual(request.full_url,'https://mockapi.kiwoom.com/api/dostk/ordr')
        self.assertEqual(request.get_header('Api-id'),'kt10003')
        self.assertEqual(json.loads(request.data),{'dmst_stex_tp':'KRX','orig_ord_no':'0000001','stk_cd':'005930','cncl_qty':'0'})
        self.assertEqual(receipt,CancellationReceipt('1','2',7))
        self.assertNotIn('offline-cancel-token',repr(receipt))

    def test_real_session_is_rejected_before_queue(self):
        self.session.mode='real';queue=Mock()
        with self.assertRaises(CancellationError):self.invoke(queue)
        queue.assert_not_called()

    def test_session_change_and_guard_loss_while_queued_never_reach_transport(self):
        def queued(*args,**kwargs):
            self.session.mode='real'
            try:kwargs['before_send']()
            except Exception:raise HTTPNotSentError('no send')
            self.fail('send allowed')
        with self.assertRaises(CancellationError) as ctx:self.invoke(Mock(side_effect=queued))
        self.assertFalse(ctx.exception.uncertain)

    def test_parent_mismatch_bad_ack_and_transport_error_are_unknown_no_retry(self):
        for changes in ({'base_orig_ord_no':'3'},{'ord_no':'1'},{'cncl_qty':'-1'},{'return_code':None}):
            with self.subTest(changes=changes):
                old=dict(self.payload);self.payload.update(changes)
                queue=Mock(side_effect=self.transport)
                with self.assertRaises(CancellationError) as ctx:self.invoke(queue)
                self.assertTrue(ctx.exception.uncertain)
                queue.assert_called_once();self.payload=old
        queue=Mock(side_effect=OSError('secret'))
        with self.assertRaises(CancellationError) as ctx:self.invoke(queue)
        self.assertTrue(ctx.exception.uncertain);self.assertNotIn('secret',str(ctx.exception));queue.assert_called_once()


if __name__=='__main__':unittest.main()

class AuctionWindowTests(unittest.TestCase):
    setUp = TTLTests.setUp
    make_monitor = TTLTests.make_monitor
    own_order = TTLTests.own_order
    ack = TTLTests.ack
    counts = TTLTests.counts
    def auction_order(self, strategy, registered, end, *, bad_hash=False):
        import hashlib
        raw = json.dumps({'config': {'experiment_order_window_end_at': end}}, separators=(',', ':'))
        digest = hashlib.sha256(raw.encode()).hexdigest() if not bad_hash else '0' * 64
        with self.tracker._db():
            pass
        with sqlite3.connect(self.tracker.path) as db:
            db.execute('INSERT INTO orders VALUES (?,?,?,?,?,?,?,?,?)',
                ('default', '20261030', 'paper', '44', '000660', 'buy', strategy, 1, registered))
            db.execute('INSERT INTO intents VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                ('default', 'intent44', '20261030', 'paper', 'buy', '000660', 1, strategy,
                 registered, '44', 'accepted', None))
            db.execute('INSERT INTO order_decisions VALUES (?,?,?,?,?,?,?)',
                ('default', 'intent44', registered, '10000', 'limit', raw, digest))

    def test_auction_window_end_cancels_early_only_at_exact_boundary(self):
        for arm, registered, end in [
            ('EXP_C', '2026-10-30T09:00:30+09:00', '2026-10-30T09:01:00+09:00'),
            ('EXP_GDR', '2026-10-30T08:59:30+09:00', '2026-10-30T09:00:00+09:00'),
            ('EXP_D', '2026-10-30T15:29:30+09:00', '2026-10-30T15:30:00+09:00'),
            ('EXP_VSS', '2026-10-30T15:29:30+09:00', '2026-10-30T15:30:00+09:00'),
            ('EXP_LDR', '2026-10-30T15:29:30+09:00', '2026-10-30T15:30:00+09:00')]:
            with self.subTest(arm=arm):
                # Independent DB per arm, avoiding shared-order fixtures.
                temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
                original = self.tracker
                self.tracker = ExecutionTracker(log_dir=Path(temp.name),now=lambda:self.now)
                self.auction_order(arm, registered, end)
                moment=datetime.fromisoformat(end)
                rows,_=TTLLedger(self.tracker).due(moment-timedelta(microseconds=1),180)
                self.assertFalse(rows)
                rows,_=TTLLedger(self.tracker).due(moment,180)
                self.assertEqual([(r['strategy'],r['due_reason']) for r in rows],[(arm,'auction_window_end')])
                self.tracker=original

    def test_invalid_deadline_or_hash_never_shortens_ttl(self):
        for end, bad_hash in [('2026-10-30T09:00:00+09:00',False),
                              ('2026-10-31T09:01:00+09:00',False),
                              ('2026-10-30T09:01:00+09:00',True)]:
            with self.subTest(end=end,bad_hash=bad_hash):
                temp=tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
                original=self.tracker
                self.tracker=ExecutionTracker(log_dir=Path(temp.name),now=lambda:self.now)
                self.auction_order('EXP_C','2026-10-30T09:00:30+09:00',end,bad_hash=bad_hash)
                rows,_=TTLLedger(self.tracker).due(datetime(2026,10,30,9,1,tzinfo=KST),180)
                self.assertFalse(rows)
                rows,_=TTLLedger(self.tracker).due(datetime(2026,10,30,9,3,30,tzinfo=KST),180)
                self.assertEqual(rows[0]['due_reason'],'ttl')
                self.tracker=original

    def test_gdr_observed_open_fallback_expires_at_0901(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.tracker=ExecutionTracker(log_dir=Path(temp.name),now=lambda:self.now)
        self.auction_order('EXP_GDR','2026-10-30T09:00:30+09:00','2026-10-30T09:01:00+09:00')
        rows,_=TTLLedger(self.tracker).due(datetime(2026,10,30,9,1,tzinfo=KST),180)
        self.assertEqual([(r['strategy'],r['due_reason']) for r in rows],[('EXP_GDR','auction_window_end')])

    def test_gdr_preauction_order_cannot_claim_fallback_deadline(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.tracker=ExecutionTracker(log_dir=Path(temp.name),now=lambda:self.now)
        self.auction_order('EXP_GDR','2026-10-30T08:59:30+09:00','2026-10-30T09:01:00+09:00')
        rows,_=TTLLedger(self.tracker).due(datetime(2026,10,30,9,1,tzinfo=KST),180)
        self.assertEqual(rows,[])

    def test_window_expiry_uses_existing_broker_reconciliation_and_one_cancel(self):
        temp=tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.now=datetime(2026,10,30,9,1,tzinfo=KST)
        self.tracker=ExecutionTracker(log_dir=Path(temp.name),now=lambda:self.now)
        self.auction_order('EXP_C','2026-10-30T09:00:30+09:00','2026-10-30T09:01:00+09:00')
        self.session.paper_automation_reconcile=SimpleNamespace(_session=self.session,_tracker=self.tracker)
        self.pending=[{'ord_no':'0000044','stk_cd':'A000660','ord_qty':'1','ord_pric':'10000',
            'oso_qty':'1','orig_ord_no':'0000000','io_tp_nm':'+매수','stex_tp':'1',
            'sor_yn':'N','ord_stt':'접수','tm':'090030'}]
        self.monitor=self.make_monitor()
        self.monitor.tick(self.session,self.tracker,180)
        self.cancel.assert_called_once()
        self.assertEqual(self.counts(),(1,1))
        with sqlite3.connect(self.tracker.path) as db:
            payload=json.loads(db.execute('SELECT evidence_json FROM buy_ttl_cancellation_attempts').fetchone()[0])
        self.assertEqual(payload['due_reason'],'auction_window_end')
        self.now+=timedelta(seconds=31)
        self.monitor.tick(self.session,self.tracker,180)
        self.cancel.assert_called_once()
