"""Closed-day refresh cadence with fake clock and offline broker stubs."""
from datetime import datetime, timedelta
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock, patch
import csv
import json
import tempfile
import unittest

from kiwoom_autotrader.api.executions import Execution, RealizedProfit
from kiwoom_autotrader.api.orders import OrderReceipt
from kiwoom_autotrader.api.session import BrokerSession, IssuedToken, KST
from kiwoom_autotrader.trading.execution_log import ExecutionTracker

OLD = '20261026'
SYMBOL = '005930'


class ClosedDayBackoffTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        # Historical cadence is an out-of-session contract. Current-day-only
        # protection during the regular session is covered separately.
        self.now = datetime(2026, 10, 27, 16, 0, 0, tzinfo=KST)
        self.session = BrokerSession(now=lambda: self.now)
        self.session.mode = 'paper'; self.session._issued = IssuedToken('offline-closed-test', '20300101000000')
        self.failure = False
        self.fill = Execution(OLD, '24', '1', SYMBOL, '삼성전자', 'sell', '10:00:00', Decimal(100), 2)
        self.executions = Mock(side_effect=self._execution)
        self.profits = Mock(return_value=(RealizedProfit(SYMBOL, 2, Decimal(100), Decimal(80), Decimal(2), Decimal(1)),))
        self.account = Mock(return_value={'mode':'paper','exchange':'KRX','snapshot_changed':False,
                           'holdings':[{'stk_cd':'A005930','rmnd_qty':'7','crd_tp':'00'}]})
        self.notify = Mock()
        self.tracker = ExecutionTracker(log_dir=self.path, now=lambda: self.now,
            settings_loader=lambda: {'market_end_time':'15:30'}, account_fetcher=self.account,
            executions_fetcher=self.executions, profit_fetcher=self.profits)
        receipt = OrderReceipt('paper', 'sell', SYMBOL, '2', None, '24', 'https://mockapi.kiwoom.com')
        self.tracker.register_order(receipt, 'ddcrs', registered_at=datetime(2026, 10, 26, 10, 0, tzinfo=KST))

    def _execution(self, session, day):
        if day == OLD and self.failure:
            raise OSError('synthetic failure')
        return (self.fill,) if day == OLD else ()

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)
        self.drain()

    def drain(self):
        for _ in range(100):
            self.tracker.tick(self.session, self.notify)
            if not self.tracker._tasks:
                break
        else:
            self.fail('offline task queue did not drain')

    def count_old(self):
        return sum(call.args[1] == OLD for call in self.executions.call_args_list)

    def prepare(self):
        self.drain()
        state = self.tracker._closed_reconciliation[('paper',OLD)]
        self.assertTrue(state['successful'])
        self.assertEqual(state['level'], 0)

    def test_stable_day_uses_5_15_60_minute_schedule_and_keeps_today_fresh(self):
        self.prepare(); baseline = self.count_old(); balance_calls = self.account.call_count
        for _ in range(29): self.advance(10)
        self.assertEqual(self.count_old(), baseline)
        self.assertGreater(self.account.call_count, balance_calls)
        self.advance(10)
        self.assertEqual(self.count_old(), baseline + 1)
        self.assertEqual(self.tracker._closed_reconciliation[('paper',OLD)]['level'], 1)
        self.advance(14*60+50); self.assertEqual(self.count_old(), baseline + 1)
        self.advance(10); self.assertEqual(self.count_old(), baseline + 2)
        self.assertEqual(self.tracker._closed_reconciliation[('paper',OLD)]['level'], 2)
        self.advance(59*60+50); self.assertEqual(self.count_old(), baseline + 2)
        self.advance(10); self.assertEqual(self.count_old(), baseline + 3)

    def test_today_after_close_is_bounded_but_holding_checks_continue(self):
        self.now = self.now.replace(hour=15,minute=31)
        self.tracker.register_order(OrderReceipt('paper','sell',SYMBOL,'2',None,'24','https://mockapi.kiwoom.com'), 'ddcrs')
        self.executions.side_effect = lambda session, day: (replace(self.fill,day=day),) if day in (OLD,'20261027') else ()
        self.drain()
        self.assertTrue(self.tracker._closed_reconciliation[('paper','20261027')]['successful'])
        today_calls = sum(call.args[1]=='20261027' for call in self.executions.call_args_list)
        balance_calls = self.account.call_count
        self.advance(10)
        self.assertEqual(sum(call.args[1]=='20261027' for call in self.executions.call_args_list),today_calls)
        self.assertGreater(self.account.call_count,balance_calls)

    def test_request_count_over_30_minutes_against_existing_loop(self):
        self.prepare()
        for _ in range(180): self.advance(10)
        # Prior code re-fetched each closed day in each 10 second cycle.  This
        # baseline assumes no throttling from live network delays.
        historical_calls = self.count_old()
        self.assertLessEqual(historical_calls, 4)
        # Today is also closed here; its executions correctly use backoff.
        # The safety-critical account reconciliation still runs every cycle.
        self.assertGreaterEqual(self.account.call_count, 180)
        self.assertGreaterEqual(self.account.call_count, 180)

    def test_stable_unknown_cost_can_backoff_but_cost_remains_unconfirmed(self):
        self.profits.return_value = ()
        self.drain(); initial = self.count_old()
        state = self.tracker._closed_reconciliation[('paper',OLD)]
        self.assertTrue(state['successful'])
        self.advance(290); self.assertEqual(self.count_old(), initial)
        self.advance(10); self.assertEqual(self.count_old(), initial + 1)
        state = self.tracker._closed_reconciliation[('paper',OLD)]
        self.assertTrue(state['successful']); self.assertEqual(state['level'], 1)
        with self.tracker._db() as db:
            self.assertIsNone(db.execute('SELECT net FROM profits WHERE day=?',(OLD,)).fetchone()[0])

    def test_failed_read_waits_five_minutes_and_first_success_does_not_jump_to_15(self):
        self.prepare(); base = self.count_old(); self.failure = True
        self.advance(300)
        self.assertEqual(self.count_old(), base + 1)
        self.assertFalse(self.tracker._closed_reconciliation[('paper',OLD)]['successful'])
        self.failure = False
        self.advance(10); self.assertEqual(self.count_old(), base + 1)
        self.advance(290); self.assertEqual(self.count_old(), base + 2)
        state = self.tracker._closed_reconciliation[('paper',OLD)]
        self.assertEqual(state['level'],0); self.assertTrue(state['successful'])

    def test_incomplete_provider_pages_never_advance_success_backoff(self):
        partial = True
        call_number = 0
        def observed_profit(session, symbol, day, *, observer):
            nonlocal call_number
            call_number += 1
            event = {'schema':'kiwoom-broker-page-v1','source_api':'ka10072','source_kind':'broker_observation',
                     'mode':'paper','day':day,'query_id':format(call_number,'032x'),'request_started_at':self.now.isoformat(),
                     'observed_at':self.now.isoformat(),'page':1,'request_body':{'stk_cd':symbol,'strt_dt':day},
                     'response_body':{'return_code':0,'dt_stk_div_rlzt_pl':[]},
                     'continuation':{'request_cont_yn':'N','response_cont_yn':'Y' if partial else 'N',
                                     'next_key_present':partial},'status':'success','http_status':200}
            observer(event)
            return (RealizedProfit(SYMBOL, 2, Decimal(100), Decimal(80), Decimal(2), Decimal(1)),)
        self.tracker.profit_fetcher = observed_profit
        self.tracker._default_profit_fetcher = True
        self.drain()
        self.assertFalse(self.tracker._closed_reconciliation[('paper',OLD)]['successful'])
        self.advance(300)
        self.assertFalse(self.tracker._closed_reconciliation[('paper',OLD)]['successful'])
        partial = False
        self.advance(300)
        state = self.tracker._closed_reconciliation[('paper',OLD)]
        self.assertTrue(state['successful']); self.assertEqual(state['level'],0)
        self.advance(300)
        self.assertEqual(self.tracker._closed_reconciliation[('paper',OLD)]['level'],1)

    def test_same_source_values_ignore_timestamps_but_changed_cost_resets_backoff(self):
        call_number, fee = 0, '2'
        def observed_profit(session, symbol, day, *, observer):
            nonlocal call_number
            call_number += 1
            event = {'schema':'kiwoom-broker-page-v1','source_api':'ka10072','source_kind':'broker_observation',
                     'mode':'paper','day':day,'query_id':format(call_number,'032x'),'request_started_at':self.now.isoformat(),
                     'observed_at':self.now.isoformat(),'page':1,'request_body':{'stk_cd':symbol,'strt_dt':day},
                     'response_body':{'return_code':0,'dt_stk_div_rlzt_pl':[{'stk_cd':symbol,'tdy_trde_cmsn':fee}]},
                     'continuation':{'request_cont_yn':'N','response_cont_yn':'N','next_key_present':False},
                     'status':'success','http_status':200}
            observer(event)
            return (RealizedProfit(SYMBOL, 2, Decimal(100), Decimal(80), Decimal(2), Decimal(1)),)
        self.tracker.profit_fetcher = observed_profit
        self.tracker._default_profit_fetcher = True
        self.drain(); self.advance(300)
        self.assertEqual(self.tracker._closed_reconciliation[('paper',OLD)]['level'],1)
        fee = '3'
        self.advance(900)
        self.assertEqual(self.tracker._closed_reconciliation[('paper',OLD)]['level'],0)
        with self.tracker._db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM broker_observation_evidence WHERE source_api='ka10072'").fetchone()[0],3)

    def test_new_local_intent_invalidates_prior_deadline(self):
        self.prepare(); base = self.count_old()
        self.tracker.prepare_order('paper','buy','000660','1','gdcrs')
        # New current-day intent is not a historic-day change.
        self.advance(10); self.assertEqual(self.count_old(),base)
        with self.tracker._db() as db:
            db.execute("INSERT INTO intents(scope,id,day,mode,side,symbol,quantity,strategy,created_at,order_number,state,reason) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                       ('default','old-intent',OLD,'paper','buy','000660',1,'gdcrs',self.now.isoformat(),None,'prepared',None))
        self.advance(10); self.assertEqual(self.count_old(),base+1)
        self.assertFalse(self.tracker._closed_reconciliation[('paper',OLD)]['successful'])

    def test_new_00_sell_triggers_urgent_read_and_keeps_raw_observation(self):
        self.prepare(); base = self.count_old()
        ws = {'schema_version':1,'source_api':'00','source_kind':'broker_observation',
              'observed_at':self.now.isoformat(),'day':OLD,'mode':'paper','event_kind':'execution_stream',
              'values':{'9001':'A005930','9203':'24','909':'2','914':'100','915':'1',
                        '911':'2','903':'200','907':'1','913':'체결'}}
        self.tracker.record_broker_observation(ws)
        self.drain()
        self.assertGreater(self.count_old(), base)
        with self.tracker._db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM broker_observations WHERE source_api='00'").fetchone()[0],1)
        data = [json.loads(x) for x in (self.path / (OLD+'.broker-observations.jsonl')).read_text().splitlines()]
        self.assertEqual(data[-1]['payload']['values']['909'],'2')

    def test_00_duplicate_is_preserved_but_not_a_new_fingerprint(self):
        self.prepare()
        event = {'schema_version':1,'source_api':'00','source_kind':'broker_observation',
              'observed_at':self.now.isoformat(),'day':OLD,'mode':'paper','event_kind':'execution_stream',
              'values':{'9001':'A005930','9203':'24','909':'2','914':'100','915':'1',
                        '911':'2','903':'200','907':'1','913':'체결'}}
        self.tracker.record_broker_observation(event)
        with self.tracker._db() as db: first = self.tracker._closed_economic_fingerprint(db,'paper',OLD)[0]
        self.tracker.record_broker_observation({**event,'observed_at':(self.now+timedelta(seconds=1)).isoformat()})
        with self.tracker._db() as db:
            second = self.tracker._closed_economic_fingerprint(db,'paper',OLD)[0]
            self.assertEqual(db.execute("SELECT COUNT(*) FROM broker_observations WHERE source_api='00'").fetchone()[0],2)
        self.assertEqual(first,second)

    def test_large_unchanged_closed_day_reuses_parsed_evidence(self):
        event = {'schema_version':1, 'source_api':'00', 'source_kind':'broker_observation',
                 'observed_at':self.now.isoformat(), 'day':OLD, 'mode':'paper',
                 'event_kind':'execution_stream', 'values':{'909':'1','915':'1'}}
        encoded = json.dumps(event)
        with self.tracker._db() as db:
            db.executemany('INSERT INTO broker_observations(scope,day,mode,observed_at,source_api,digest,payload_json) VALUES (?,?,?,?,?,?,?)',
                [('default',OLD,'paper',self.now.isoformat(),'00','synthetic-digest',encoded)]*10000)
        with self.tracker._db() as db:
            first = self.tracker._closed_economic_fingerprint(db,'paper',OLD)[0]
            self.assertIsNotNone(first)
            with patch.object(self.tracker, '_closed_observation_facts', wraps=self.tracker._closed_observation_facts) as derived, \
                 patch('kiwoom_autotrader.trading.execution_log.json.loads', wraps=json.loads) as parse:
                for _ in range(100):
                    self.assertEqual(self.tracker._closed_economic_fingerprint(db,'paper',OLD)[0], first)
                self.assertEqual(derived.call_count, 0)
                self.assertLessEqual(parse.call_count, 100)  # only small holdings field
        changed = {**event, 'values':{'909':'2','915':'2'}}
        self.tracker.record_broker_observation(changed)
        with self.tracker._db() as db:
            self.assertNotEqual(self.tracker._closed_economic_fingerprint(db,'paper',OLD)[0], first)

    def test_session_change_or_clock_rollback_flushes_cache(self):
        self.prepare(); base = self.count_old()
        self.session._issued = IssuedToken('different-offline-session', '20300101000000')
        self.drain(); self.assertGreater(self.count_old(),base)
        base = self.count_old(); self.now -= timedelta(seconds=20)
        self.drain(); self.assertGreater(self.count_old(),base)

    def test_stream_gap_invalidates_all_closed_deadlines_immediately(self):
        self.prepare(); base = self.count_old()
        event = {'schema_version':1, 'source_api':'00', 'source_kind':'broker_observation',
                 'observed_at':self.now.isoformat(), 'day':self.now.strftime('%Y%m%d'),
                 'mode':'paper', 'event_kind':'stream_gap', 'values':{}}
        self.tracker.record_broker_observation(event)
        self.drain(); self.assertGreater(self.count_old(),base)

    def test_holdings_discrepancy_invalidates_fingerprint(self):
        self.prepare(); base = self.count_old()
        with self.tracker._db() as db:
            db.execute('UPDATE holdings SET discrepancy=? WHERE scope=? AND mode=?',
                       (json.dumps([SYMBOL]),'default','paper'))
        self.advance(10); self.assertGreater(self.count_old(),base)

    def test_new_discrepancy_detected_by_holdings_refresh_wakes_immediately(self):
        self.prepare(); base = self.count_old()
        self.account.return_value = {'mode':'paper','exchange':'KRX','snapshot_changed':False,
                                     'holdings':[{'stk_cd':'A005930','rmnd_qty':'6','crd_tp':'00'}]}
        self.advance(10)
        # The 10-second current-day loop has detected the new discrepancy.
        # No further clock advance is needed before rechecking closed days.
        self.drain(); self.assertGreater(self.count_old(),base)

    def test_csv_and_accounting_are_still_materialized(self):
        self.prepare()
        with (self.path/(OLD+'.csv')).open(encoding='utf-8-sig', newline='') as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual(rows[-1]['행구분'], '일일합계')
        self.assertEqual(rows[-1]['순이익'], '37')
        doc = json.loads((self.path/(OLD+'.execution-accounting.json')).read_text())
        self.assertTrue(doc)

    def test_three_stable_reads_wait_for_official_next_close(self):
        close=datetime(2026,10,28,15,31,tzinfo=KST)
        self.tracker._closed_calendar_resolver=lambda now,day:{'next_close':close,'elapsed_sessions':0,'source_hashes':['c'*64]}
        self.prepare();self.advance(300);self.advance(900)
        state=self.tracker._closed_reconciliation[('paper',OLD)]
        self.assertEqual(state['next_at'],close);self.assertEqual(state['reason'],'three_stable_official_next_close')
        initial=self.count_old();self.advance(3600);self.assertEqual(self.count_old(),initial)
    def test_more_than_seven_verified_sessions_stop_only_without_pending(self):
        self.prepare();self.tracker._closed_calendar_resolver=lambda now,day:{'next_close':None,'elapsed_sessions':8,'source_hashes':['c'*64]}
        self.tracker._closed_calendar_cache={}
        with self.tracker._db() as db:
            self.assertFalse(self.tracker._closed_due(db,'paper',OLD,self.now))
            db.execute('UPDATE fills SET quantity=1 WHERE day=?',(OLD,))
            self.assertTrue(self.tracker._closed_due(db,'paper',OLD,self.now))
    def test_missing_calendar_never_invents_a_future_session(self):
        self.tracker._closed_calendar_resolver=lambda now,day:{}
        self.prepare();self.advance(300);self.advance(900)
        state=self.tracker._closed_reconciliation[('paper',OLD)]
        self.assertEqual(state['next_at'],self.now+timedelta(minutes=60))

if __name__ == '__main__': unittest.main()
