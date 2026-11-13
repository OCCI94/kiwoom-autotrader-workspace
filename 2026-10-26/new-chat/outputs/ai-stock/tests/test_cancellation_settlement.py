"""Final cancellation proofs, all transports mocked and ledgers temporary."""
from datetime import datetime,timedelta
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

from kiwoom_autotrader.api.session import BrokerSession,IssuedToken,KST
from kiwoom_autotrader.api.cancellations import CancellationReceipt
from kiwoom_autotrader.trading.execution_log import ExecutionTracker
from kiwoom_autotrader.trading.unfilled_buy_ttl import TTLLedger,UnfilledBuyTTLMonitor,TTL_STRATEGIES
from kiwoom_autotrader.trading.cancellation_settlement import (
    settle_cancellation,confirmed_cancelled_quantity,confirmed_cancel_order_numbers,
    settlement_watermark_rows,CancellationSettlementUnavailable,TABLE)


class SettlementFixture:
    """Reusable temporary fixture for pending/cap/ownership integration tests."""
    def __init__(self,quantity=3,prices=None,strategy='gdcrs',retry_not_sent=False):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.now=datetime(2026,10,30,10,5,tzinfo=KST);self.monotonic=100.0
        self.quantity=quantity
        self.tracker=ExecutionTracker(log_dir=self.root,now=lambda:self.now,
            settings_loader=lambda:{'market_end_time':'15:30'})
        with self.tracker._db() as db:
            db.execute('INSERT INTO orders VALUES (?,?,?,?,?,?,?,?,?)',('default','20261030','paper','1','005930','buy',strategy,10,'2026-10-30T10:00:01+09:00'))
            db.execute('INSERT INTO intents VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',('default','initial1','20261030','paper','buy','005930',10,strategy,'2026-10-30T09:59:59+09:00','1','accepted',None))
            db.execute('INSERT INTO order_decisions VALUES (?,?,?,?,?,?,?)',('default','initial1','2026-10-30T09:59:59+09:00','10000','limit',None,None))
            self.prices=prices if prices is not None else [(quantity,10000)] if quantity else []
            assert sum(q for q,p in self.prices)==quantity
            amount=sum(q*p for q,p in self.prices)
            average=amount//quantity if quantity else 0
            if quantity:
                db.execute('INSERT INTO fills VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',('default','20261030','paper','1','10','005930','삼성전자','buy','10:01:00','10000',quantity,strategy,str(10-quantity),None,None))
                db.execute('UPDATE fills SET price=?',(str(average),))
        self.session=BrokerSession(now=lambda:self.now);self.session.mode='paper'
        self.session._issued=IssuedToken('offline-settlement-private-token','20300101000000')
        self.stream={'ready':True,'error':None,'gap_count':0,'observations':2,'ingress_sequence':2,
            'engine_ingress_sequence':2,'inflight_callbacks':0,'engine_pending_callbacks':0}
        self.session.execution_recorder=SimpleNamespace(status=lambda:dict(self.stream))
        self.session.paper_automation_reconcile=SimpleNamespace(_tracker=self.tracker,_session=self.session)
        self.session.execution_recording_guard=lambda:None
        self.record00({},'2026-10-30T09:59:58+09:00',kind='stream_started')
        cumulative=amount=0
        for index,(qty,price) in enumerate(self.prices,10):
            cumulative+=qty;amount+=qty*price
            self.record00({**self.unit(cumulative),'903':str(amount),'909':str(index),
                '910':str(price),'914':str(price),'915':str(qty)},'2026-10-30T10:01:00+09:00')
        ledger=TTLLedger(self.tracker)
        # A full-fill race may have occurred after the persisted attempt.
        with ledger.db() as db:
            row=dict(db.execute('SELECT o.*,i.id AS intent_id FROM orders o JOIN intents i ON i.order_number=o.ord_no').fetchone())
            watermark=ledger._snapshot(db,'20261030')[2]
        evidence={'claimed_at':'2026-10-30T10:03:30+09:00' if retry_not_sent else '2026-10-30T10:04:00+09:00',
            'schema':'gdcrs-unfilled-buy-ttl-v1','strategy':strategy,'mode':'paper',
            'original_order_number':'1','symbol':'005930','cancel_request_qty':0,'ack_releases_principal':False}
        ledger.claim(row,evidence,watermark)
        if retry_not_sent:
            ledger.outcome(row,'2026-10-30T10:03:30+09:00','cancel_not_sent')
            row['_ttl_attempt_sequence']=1
            assert ledger.claim(row,{**evidence,'claimed_at':'2026-10-30T10:04:00+09:00'},watermark)
        ledger.outcome(row,'2026-10-30T10:04:00+09:00','cancel_input_ack_unconfirmed_settlement',CancellationReceipt('1','2',0))
        self.claim={'original_ord_no':'1'}
        self.pending=[]
        self.rest=[self.original_row(quantity)]
        self.rest[0]['cntr_uv']=str(average)
        if quantity<10:
            self.record00(self.cancel_values(10-quantity),'2026-10-30T10:04:01+09:00')
            self.rest.append(self.cancel_row(10-quantity))
        self.pending_events_transform=lambda e:e
        self.order_events_transform=lambda e:e
        self.pending_calls=0;self.order_calls=0;self.page_calls=0

    def close(self):self.tmp.cleanup()

    @staticmethod
    def unit(qty):
        return {'9203':'0000001','9001':'005930','900':'10','901':'10000','902':str(10-qty),
            '903':str(qty*10000),'904':'0000000','905':'+매수','907':'2','908':'100100','909':'10',
            '910':'10000','911':str(qty),'914':'10000','915':str(qty),'913':'체결',
            '2134':'1','2136':'N','922':'00'}

    @staticmethod
    def cancel_values(qty):
        return {'9203':'0000002','9001':'005930','900':str(qty),'901':'0','902':'0','903':'0',
            '904':'0000001','905':'+매수취소','907':'2','908':'100401','909':'','913':'확인',
            '2134':'1','2136':'N','922':'00'}

    @staticmethod
    def original_row(qty):
        return {'ord_no':'0000001','orig_ord_no':'0000000','stk_cd':'A005930','stk_bond_tp':'1',
            'io_tp_nm':'현금매수','ord_qty':'10','ord_uv':'10000','cnfm_qty':'0','cntr_no':'10' if qty else '0',
            'cntr_qty':str(qty),'cntr_uv':'10000' if qty else '0','cntr_tm':'10:01:00' if qty else '',
            'mdfy_cncl_tp':'','dmst_stex_tp':'KRX'}

    @staticmethod
    def cancel_row(qty):
        return {'ord_no':'0000002','orig_ord_no':'0000001','stk_cd':'A005930','stk_bond_tp':'1',
            'io_tp_nm':'현금매수','ord_qty':str(qty),'ord_uv':'0','cnfm_qty':str(qty),'cntr_no':'0',
            'cntr_qty':'0','cntr_uv':'0','mdfy_cncl_tp':'취소주문','dmst_stex_tp':'KRX'}

    def record00(self,values,at=None,kind='execution_stream'):
        event={'schema_version':1,'source_api':'00','source_kind':'broker_observation','observed_at':at or self.now.isoformat(),
            'day':'20261030','mode':'paper','event_kind':kind,'values':values}
        self.tracker.record_broker_observation(event)
        return event

    def page(self,api,rows):
        pending=api=='ka10075'
        self.page_calls+=1
        return {'schema':'kiwoom-broker-page-v1','source_api':api,'source_kind':'broker_observation',
            'mode':'paper','day':'20261030','query_id':f'{self.page_calls:032x}',
            'request_started_at':self.now.isoformat(),'observed_at':self.now.isoformat(),'page':1,
            'request_body':({'all_stk_tp':'0','trde_tp':'0','stex_tp':'0'} if pending else
                {'ord_dt':'20261030','stk_bond_tp':'1','mrkt_tp':'0','sell_tp':'0','qry_tp':'0','stk_cd':'','fr_ord_no':'','dmst_stex_tp':'KRX'}),
            'response_body':{'return_code':0,('oso' if pending else 'acnt_ord_cntr_prst_array'):rows},
            'continuation':{'request_cont_yn':'N','response_cont_yn':'N','next_key_present':False},
            'status':'success','http_status':200}

    def pending_fetcher(self,session,*,observer,day):
        self.pending_calls+=1
        observer(self.pending_events_transform(self.page('ka10075',self.pending)))
        return self.pending

    def order_fetcher(self,session,*,observer,day):
        self.order_calls+=1
        observer(self.order_events_transform(self.page('kt00009',self.rest)))
        return self.rest

    def settle(self,**kwargs):
        return settle_cancellation(self.session,self.tracker,self.claim,now=lambda:self.now,
            monotonic=lambda:self.monotonic,pending_fetcher=self.pending_fetcher,order_fetcher=self.order_fetcher,**kwargs)

    def order(self,db):return db.execute('SELECT * FROM orders WHERE ord_no=?',('1',)).fetchone()

    def released(self):
        with TTLLedger(self.tracker).db() as db:return confirmed_cancelled_quantity(db,'default',self.order(db))


class CancellationSettlementTests(unittest.TestCase):
    def setUp(self):
        self.f=SettlementFixture();self.addCleanup(self.f.close)

    def test_retry_ack_settles_latest_attempt_and_preserves_prior_not_sent(self):
        f=SettlementFixture(retry_not_sent=True);self.addCleanup(f.close)
        result=f.settle()
        self.assertEqual(result['status'],'cancellation_confirmed');self.assertEqual(f.released(),7)
        with TTLLedger(f.tracker).db() as db:
            proof=json.loads(db.execute('SELECT evidence_json FROM '+TABLE).fetchone()[0])
            self.assertEqual(proof['cancellation_attempt_sequence'],1)
            self.assertEqual(db.execute('SELECT status FROM buy_ttl_cancellation_outcomes').fetchone()[0],'cancel_not_sent')
            self.assertEqual(db.execute('SELECT status FROM buy_ttl_cancellation_retry_outcomes').fetchone()[0],'cancel_input_ack_unconfirmed_settlement')

    def test_stale_retry_generation_cannot_settle_a_new_attempt(self):
        f=SettlementFixture(retry_not_sent=True);self.addCleanup(f.close)
        f.claim={'original_ord_no':'1','attempt_sequence':0}
        with self.assertRaisesRegex(CancellationSettlementUnavailable,'generation_changed'):f.settle()
        self.assertEqual(f.released(),0)

    def test_retry_outcome_corruption_after_final_proof_revokes_release(self):
        f=SettlementFixture(retry_not_sent=True);self.addCleanup(f.close)
        f.settle();self.assertEqual(f.released(),7)
        with sqlite3.connect(f.tracker.path) as db:
            db.execute('DROP TRIGGER buy_ttl_cancellation_retry_outcomes_no_update')
            db.execute("UPDATE buy_ttl_cancellation_retry_outcomes SET status='cancel_not_sent'")
        with self.assertRaisesRegex(CancellationSettlementUnavailable,'attempt_changed'):f.released()

    def test_partial_filled_only_remaining_seven_released_original_rows_preserved(self):
        with sqlite3.connect(self.f.tracker.path) as db:
            before={t:db.execute('SELECT * FROM '+t).fetchall() for t in ('orders','intents','fills','holdings','account_balance_daily')}
        result=self.f.settle()
        self.assertEqual(result['status'],'cancellation_confirmed')
        self.assertEqual((result['filled_quantity'],result['filled_amount'],result['cancelled_quantity']),(3,'30000',7))
        self.assertEqual(self.f.released(),7)
        with TTLLedger(self.f.tracker).db() as db:
            after={t:[tuple(r) for r in db.execute('SELECT * FROM '+t)] for t in before}
            self.assertEqual(confirmed_cancel_order_numbers(db,'default','20261030','paper','005930'),{'2'})
            self.assertEqual(len(settlement_watermark_rows(db,'default','paper','005930')),2)
        self.assertEqual(before,after)
        self.assertEqual(result['cash_adjustment_krw'],0)

    def test_all_owned_strategies_reconcile_partial_full_and_zero_fills(self):
        for strategy in (*TTL_STRATEGIES,'rsv#1','rsv#107'):
            for quantity in (0,3,10):
                with self.subTest(strategy=strategy,quantity=quantity):
                    f=SettlementFixture(quantity,strategy=strategy);self.addCleanup(f.close)
                    self.assertEqual(f.released(),0)  # ACK is never release.
                    result=f.settle()
                    self.assertEqual(result['filled_quantity'],quantity)
                    self.assertEqual(f.released(),10-quantity)
                    with TTLLedger(f.tracker).db() as db:
                        proof=json.loads(db.execute('SELECT evidence_json FROM '+TABLE).fetchone()[0])
                    self.assertEqual(proof['strategy'],strategy)
                    self.assertEqual(result['cash_adjustment_krw'],0)
                    f.tracker.assert_no_pending_order('paper','005930')
                    with f.tracker._db() as db:
                        f.tracker._assert_no_pending_account(db,'paper')
                        self.assertEqual(f.tracker._daily_buy_entry_counts(db,'paper','005930','20261030')['accepted'],1)

    def test_manual_or_unknown_strategy_has_no_settlement_authority(self):
        for strategy in ('manual','unknown','rsv#0','rsv#01','rsv#1x','rsv#1\x00junk','RSV#1'):
            with self.subTest(strategy=strategy):
                f=SettlementFixture(strategy=strategy);self.addCleanup(f.close)
                with self.assertRaisesRegex(CancellationSettlementUnavailable,'cancellation_owned_original_unverified'):
                    f.settle()
                self.assertEqual(f.released(),0)

    def test_fill_strategy_conflict_prevents_release(self):
        with sqlite3.connect(self.f.tracker.path) as db:
            db.execute("UPDATE fills SET strategy='S2_surge_high_breakout'")
        with self.assertRaisesRegex(CancellationSettlementUnavailable,'cancellation_projection_identity_conflict'):
            self.f.settle()
        self.assertEqual(self.f.released(),0)

    def test_zero_fill_cancel_releases_all_without_fabricating_fill(self):
        f=SettlementFixture(0);self.addCleanup(f.close)
        result=f.settle();self.assertEqual(f.released(),10)
        self.assertEqual((result['filled_quantity'],result['filled_amount']),(0,'0'))

    def test_full_fill_race_resolves_fills_and_releases_zero(self):
        f=SettlementFixture(10);self.addCleanup(f.close)
        result=f.settle()
        self.assertEqual(result['status'],'fill_resolved');self.assertEqual(f.released(),0)
        self.assertEqual(result['filled_quantity'],10)

    def test_ack_plus_absence_without_terminal_is_not_cancelled(self):
        self.f.rest=self.f.rest[:1]
        with self.assertRaises(CancellationSettlementUnavailable):self.f.settle()
        self.assertEqual(self.f.released(),0)

    def test_raw_empty_pending_return_without_complete_page_is_rejected(self):
        self.f.pending_fetcher=lambda *a,**k:[]
        with self.assertRaises(CancellationSettlementUnavailable):self.f.settle()
        self.assertEqual(self.f.released(),0)

    def test_query_scope_page_completion_and_missing_original_rejected(self):
        variants=[lambda e:{**e,'request_body':{**e['request_body'],'qry_tp':'1'}},
            lambda e:{**e,'continuation':{**e['continuation'],'response_cont_yn':'Y','next_key_present':True}},
            lambda e:{**e,'status':'broker_error'},lambda e:{**e,'page':2}]
        for transform in variants:
            with self.subTest(transform=transform):
                f=SettlementFixture();self.addCleanup(f.close);f.order_events_transform=transform
                with self.assertRaises(Exception):f.settle()
                self.assertEqual(f.released(),0)

    def test_nonzero_pending_partial_and_foreign_chain_rejected(self):
        self.f.pending=[{'ord_no':'1','orig_ord_no':'0','stk_cd':'A005930','oso_qty':'1'}]
        with self.assertRaises(CancellationSettlementUnavailable):self.f.settle()
        self.f.pending=[]
        self.f.rest.append({**self.f.cancel_row(7),'ord_no':'3','mdfy_cncl_tp':'정정주문'})
        with self.assertRaises(CancellationSettlementUnavailable):self.f.settle()

    def test_unit_rest_quantity_amount_and_terminal_quantity_conflicts_rejected(self):
        for changes,index in [({'cntr_qty':'4'},0),({'cntr_uv':'9999'},0),({'cnfm_qty':'6'},1),({'stk_cd':'A000660'},1)]:
            f=SettlementFixture();self.addCleanup(f.close);f.rest[index].update(changes)
            with self.assertRaises(CancellationSettlementUnavailable):f.settle()
            self.assertEqual(f.released(),0)

    def test_fresh_current_session_after_restart_rechecks_no_cancel_resend(self):
        self.f.session._issued=IssuedToken('offline-new-session-token','20300101000000')
        self.assertEqual(self.f.settle()['status'],'cancellation_confirmed')
        self.assertEqual(self.f.settle()['status'],'already_settled')
        self.assertEqual((self.f.pending_calls,self.f.order_calls),(1,1))

    def test_late_fill_before_commit_and_after_commit_invalidate_release(self):
        def late():
            self.f.record00({**self.f.unit(4),'909':'11','915':'1'},self.f.now.isoformat())
        with self.assertRaises(CancellationSettlementUnavailable):self.f.settle(before_commit=late)
        self.assertEqual(self.f.released(),0)
        f=SettlementFixture();self.addCleanup(f.close);f.settle()
        f.record00({**f.unit(4),'909':'11','915':'1'})
        with self.assertRaises(CancellationSettlementUnavailable):f.released()

    def test_later_rest_revision_invalidates_before_projection_updates(self):
        self.f.settle()
        revised=self.f.page('kt00009',[self.f.original_row(4)])
        self.f.tracker.record_broker_observation(revised)
        with self.assertRaises(CancellationSettlementUnavailable):self.f.released()

    def test_later_rest_time_only_revision_invalidates_finalized_order(self):
        self.f.settle()
        row={**self.f.original_row(3),'cntr_tm':'10:02:00'}
        self.f.tracker.record_broker_observation(self.f.page('kt00009',[row]))
        with self.assertRaises(CancellationSettlementUnavailable):self.f.released()

    def test_unrelated_and_duplicate_events_do_not_invalidate_finalized_order(self):
        self.f.settle()
        self.f.record00(self.f.cancel_values(7))
        self.assertEqual(self.f.released(),7)
        unrelated={**self.f.unit(1),'9203':'0000099','9001':'000660'}
        self.f.record00(unrelated)
        with self.f.tracker._db() as db:
            db.execute('INSERT INTO orders VALUES (?,?,?,?,?,?,?,?,?)',
                ('default','20261030','paper','99','000660','buy','gdcrs',1,self.f.now.isoformat()))
            db.execute('INSERT INTO intents VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                ('default','unrelated99','20261030','paper','buy','000660',1,'gdcrs',self.f.now.isoformat(),'99','accepted',None))
        self.assertEqual(self.f.released(),7)

    def test_session_stream_callback_or_monotonic_change_during_query_rejected(self):
        for kind in ('session','gap','callback','monotonic','wall','gate_session','gate_tracker','gate_replaced'):
            f=SettlementFixture();self.addCleanup(f.close)
            original=f.order_fetcher
            def fetch(*a,**k):
                result=original(*a,**k)
                if kind=='session':f.session._issued=IssuedToken('offline-changed-token','20300101000000')
                if kind=='gap':f.stream['gap_count']+=1
                if kind=='callback':f.stream['inflight_callbacks']=1
                if kind=='monotonic':f.monotonic+=21
                if kind=='wall':f.now+=timedelta(seconds=21)
                if kind=='gate_session':f.session.paper_automation_reconcile._session=object()
                if kind=='gate_tracker':f.session.paper_automation_reconcile._tracker=object()
                if kind=='gate_replaced':f.session.paper_automation_reconcile=SimpleNamespace(_tracker=f.tracker,_session=f.session)
                return result
            f.order_fetcher=fetch
            with self.assertRaises(CancellationSettlementUnavailable):f.settle()
            self.assertEqual(f.released(),0)

    def test_no_bound_account_tracker_and_real_session_are_rejected_before_queries(self):
        self.f.session.paper_automation_reconcile._tracker=object()
        with self.assertRaises(CancellationSettlementUnavailable):self.f.settle()
        self.assertEqual(self.f.pending_calls,0)

    def test_final_evidence_immutable_and_hash_protected(self):
        self.f.settle()
        with sqlite3.connect(self.f.tracker.path) as db:
            with self.assertRaises(sqlite3.IntegrityError):db.execute(f'DELETE FROM {TABLE}')
            with self.assertRaises(sqlite3.IntegrityError):db.execute(f'UPDATE {TABLE} SET cancelled_qty=10')

    def test_conflicting_00_credit_rejection_exchange_side_or_child_fill_blocks(self):
        for terminal,changes in [(False,{'919':'100'}),(False,{'922':'03'}),
                (False,{'905':'매도'}),(False,{'2134':'2'}),(False,{'2136':'Y'}),
                (True,{'919':'100'}),(True,{'922':'03'}),(True,{'905':'매수'}),
                (True,{'2134':'2'}),(True,{'2136':'Y'}),(True,{'911':'1'}),(True,{'915':'1'}),
                (True,{'903':'10000'}),(True,{'910':'10000'}),(True,{'914':'10000'})]:
            with self.subTest(terminal=terminal,changes=changes):
                f=SettlementFixture();self.addCleanup(f.close)
                f.record00({**(f.cancel_values(7) if terminal else f.unit(3)),**changes})
                with self.assertRaises(CancellationSettlementUnavailable):f.settle()
                self.assertEqual(f.released(),0)

    def test_broker_occurrence_time_must_match_owned_order_and_rest_unit_time(self):
        for terminal,stamp in [(False,'090000'),(False,'250000'),(False,'110000'),
                                (True,'100300'),(True,'250000'),(True,'110000')]:
            with self.subTest(terminal=terminal,stamp=stamp):
                f=SettlementFixture();self.addCleanup(f.close)
                f.record00({**(f.cancel_values(7) if terminal else f.unit(3)),'908':stamp})
                with self.assertRaises(CancellationSettlementUnavailable):f.settle()
                self.assertEqual(f.released(),0)
        self.f.rest[0]['cntr_tm']='10:02:00'
        with self.assertRaises(CancellationSettlementUnavailable):self.f.settle()

    def test_partial_integer_average_floor_matches_exact_00_amount(self):
        f=SettlementFixture(3,prices=[(1,9999),(2,10000)]);self.addCleanup(f.close)
        result=f.settle()
        self.assertEqual((result['filled_amount'],result['cancelled_quantity']),('29999',7))
        self.assertEqual(f.released(),7)

    def test_average_rounded_up_or_whole_won_difference_is_not_floor_proof(self):
        for rest_only in (False,True):
            f=SettlementFixture(3,prices=[(1,9999),(2,10000)]);self.addCleanup(f.close)
            f.rest[0]['cntr_uv']='10000'
            if not rest_only:
                with sqlite3.connect(f.tracker.path) as db:db.execute('UPDATE fills SET price=?',('10000',))
            with self.assertRaises(CancellationSettlementUnavailable):f.settle()
            self.assertEqual(f.released(),0)

    def test_sidecar_failure_keeps_reservation_then_restart_fresh_reads_publish(self):
        with patch.object(self.f.tracker,'_materialize_accounting',side_effect=OSError('offline disk failure')):
            with self.assertRaises(OSError):self.f.settle()
        self.assertEqual(self.f.released(),0)
        with TTLLedger(self.f.tracker).db() as db:
            self.assertEqual(db.execute(f'SELECT COUNT(*) FROM {TABLE}').fetchone()[0],1)
            original=json.loads(db.execute(f'SELECT evidence_json FROM {TABLE}').fetchone()[0])
        self.f.now+=timedelta(seconds=31)
        self.f.session._issued=IssuedToken('offline-restarted-session','20300101000000')
        self.assertEqual(self.f.settle()['status'],'cancellation_confirmed')
        self.assertEqual(self.f.released(),7)
        self.assertEqual((self.f.pending_calls,self.f.order_calls),(2,2))
        with TTLLedger(self.f.tracker).db() as db:
            proof=json.loads(db.execute(f'SELECT evidence_json FROM {TABLE}').fetchone()[0])
        self.assertEqual(original,proof)

    def test_same_complete_rest_query_rows_across_new_pages_preserve_fingerprint(self):
        self.f.settle()
        self.f.now+=timedelta(seconds=1)
        page=self.f.page('kt00009',[self.f.rest[0]])
        page['continuation']={'request_cont_yn':'N','response_cont_yn':'Y','next_key_present':True}
        self.f.tracker.record_broker_observation(page)
        next_page={**page,'page':2,'response_body':{'return_code':0,'acnt_ord_cntr_prst_array':[self.f.rest[1]]},
            'continuation':{'request_cont_yn':'Y','response_cont_yn':'N','next_key_present':False}}
        self.f.tracker.record_broker_observation(next_page)
        self.assertEqual(self.f.released(),7)

    def test_monitor_finalizes_once_and_later_ticks_restart_do_not_regress_or_resend(self):
        cancel=Mock(side_effect=AssertionError('never send cancel again'))
        pending=Mock(side_effect=AssertionError('no new cancellation query'))
        handler=Mock(side_effect=lambda *a,**k:self.f.settle())
        def monitor():return UnfilledBuyTTLMonitor(now=lambda:self.f.now,monotonic=lambda:self.f.monotonic,
            cancel_handler=cancel,pending_fetcher=pending,settlement_handler=handler)
        m=monitor();m.tick(self.f.session,self.f.tracker,180)
        self.assertEqual(m.last_status['status'],'cancellation_confirmed')
        self.f.now+=timedelta(seconds=31);m.tick(self.f.session,self.f.tracker,180)
        self.assertEqual(m.last_status['status'],'no_due_owned_limit_buy')
        restarted=monitor();restarted.tick(self.f.session,self.f.tracker,180)
        self.assertEqual(restarted.last_status['status'],'no_due_owned_limit_buy')
        handler.assert_called_once();cancel.assert_not_called();pending.assert_not_called()


if __name__=='__main__':unittest.main()
