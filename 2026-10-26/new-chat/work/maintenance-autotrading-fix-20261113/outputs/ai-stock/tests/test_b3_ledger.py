"""Offline B3 initial-fill proof and atomic reservation; no broker access."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
import sqlite3,tempfile,unittest
from unittest.mock import Mock
from kiwoom_autotrader.api.executions import Execution
from kiwoom_autotrader.api.orders import OrderReceipt
from kiwoom_autotrader.trading.execution_log import ExecutionTracker,ExecutionLogError
from kiwoom_autotrader.trading.decision_evidence import b3_addition_evidence
from tests.test_b3_entry import NOW,FILL,bars,config

class B3LedgerTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.now=FILL-timedelta(seconds=10)
  # This legacy B3 unit fixture has no portfolio/taxonomy observation. The
  # independent theme-gate integration suite exercises the normalized cap2.
  self.tracker=ExecutionTracker(log_dir=Path(self.tmp.name),now=lambda:self.now,account_fetcher=Mock(),executions_fetcher=Mock(),profit_fetcher=Mock(),settings_loader=lambda:{'max_positions_per_theme':0})
  self.cfg=config();self.cfg.update(b3_stage='initial',b3_initial_intent_id=None)
  self.event('stream_started',self.now-timedelta(seconds=1),{})
  self.initial=self.tracker.prepare_order('paper','buy','005930',100,'gdcrs',price=1000,evidence={'strategy':'gdcrs','config':self.cfg},reconciliation_evidence=self.recon(0,0,100,1000))
  self.tracker.register_order(OrderReceipt('paper','buy','005930','100','1000','24','https://mockapi.kiwoom.com'),'gdcrs',intent_id=self.initial)
  self.now=FILL
  self.event('execution_stream',self.now,{'9203':'24','9001':'A005930','907':'2','913':'체결','908':'092030','909':'1','914':'1000','915':'100','911':'100','903':'100000'})
  self.tracker._ingest('paper','20261030',(Execution('20261030','24','1','005930','fixture','buy','09:20:30',Decimal('1000'),100),))
  self.now=NOW
 def event(self,kind,at,values):
  self.tracker.record_broker_observation({'schema_version':1,'source_api':'00','source_kind':'broker_observation','observed_at':at.isoformat(),'day':'20261030','mode':'paper','event_kind':kind,'values':values})
 def recon(self,qty,amount,order_qty,price):
  return {'schema_version':1,'symbol':'005930','pre_order_held_qty':qty,'pre_order_pur_amt_krw':amount,'remaining_principal_krw':max(0,1000000-amount),'order_quantity':order_qty,'limit_price_krw':price,'observed_at':self.now.isoformat(),'holdings_source':'broker_observation'}
 def candidate(self):return self.tracker.b3_add_candidate('paper','005930',self.now)
 def add_evidence(self,proof=None):
  cfg=config();cfg['b3_initial_intent_id']=self.initial
  return b3_addition_evidence(run_id='run',source_hash='a'*64,source_kind='broker_observation',config=cfg,observed_at=self.now,evaluated_at=self.now,window=bars(),trend_window=bars()[1:],budget=500000,initial=proof or self.candidate())
 def prepare_add(self,tracker=None,*,price=1005,held=100,principal=100000,evidence=None):
  return (tracker or self.tracker).prepare_order('paper','buy','005930',10,'gdcrs',price=price,
      evidence=evidence or self.add_evidence(),reconciliation_evidence=self.recon(held,principal,10,price))
 def test_fully_reconciled_00_and_rest_yield_exact_candidate(self):
  c=self.candidate();self.assertEqual(c,{'initial_intent_id':self.initial,'initial_fill_last_observed_at':FILL.isoformat(),
      'initial_filled_qty':100,'initial_filled_principal_krw':100000})
 def test_preliminary_strategy_ownership_requires_verified_single_owner(self):
  position=self.tracker.multi_strategy_position('paper','005930','gdcrs',self.now)
  self.assertEqual((position['status'],position['owned_qty'],position['basis_krw']),('owned',100,100000))
  self.assertTrue(position['requires_fresh_broker_reconciliation'])
  self.assertEqual(self.tracker.multi_strategy_position('paper','005930','s2',self.now)['status'],'unsupported')
  self.event('stream_stopped',self.now,{})
  self.assertEqual(self.tracker.multi_strategy_position('paper','005930','gdcrs',self.now)['status'],'unsupported')
 def test_initial_stage_requires_flat_holdings_and_half_budget(self):
  e={'strategy':'gdcrs','config':self.cfg}
  for qty,held,principal in ((10,1,1000),(501,0,0)):
   with self.subTest(qty=qty,held=held),self.assertRaises(ExecutionLogError):
    self.tracker.prepare_order('paper','buy','005930',qty,'gdcrs',price=1000,evidence=e,reconciliation_evidence=self.recon(held,principal,qty,1000))
 def test_add_evidence_initial_quantity_must_match_ledger(self):
  wrong=dict(self.candidate(),initial_filled_qty=101);e=self.add_evidence(wrong)
  with self.assertRaises(ExecutionLogError):self.prepare_add(evidence=e)
 def test_trend_bars_from_other_symbol_are_rejected(self):
  from dataclasses import replace
  cfg=config();cfg['b3_initial_intent_id']=self.initial;trend=list(bars()[1:]);trend[0]=replace(trend[0],source_symbol='000660')
  with self.assertRaises(ValueError):
   b3_addition_evidence(run_id='run',source_hash='a'*64,source_kind='broker_observation',config=cfg,observed_at=self.now,evaluated_at=self.now,window=bars(),trend_window=trend,budget=500000,initial=self.candidate())
 def test_dispatch_rechecks_invalidation_after_reservation(self):
  ident=self.prepare_add();self.tracker.assert_b3_add_dispatch('paper','005930',ident)
  self.tracker.b3_invalidate_candidate('paper','005930',self.initial,self.now,'negative_sma_observed')
  with self.assertRaises(ExecutionLogError):self.tracker.assert_b3_add_dispatch('paper','005930',ident)
 def test_other_observed_order_blocks_preliminary_and_dispatch(self):
  ident=self.prepare_add()
  self.event('execution_stream',self.now-timedelta(seconds=1),{'9203':'25','9001':'A005930','907':'1','913':'접수'})
  with self.assertRaises(ExecutionLogError):self.tracker.assert_b3_add_dispatch('paper','005930',ident)
 def test_other_observed_order_blocks_candidate(self):
  self.event('execution_stream',self.now-timedelta(seconds=1),{'9203':'25','9001':'A005930','907':'1','913':'접수'})
  self.assertIsNone(self.candidate())
 def test_dispatch_rejects_recorded_stream_stop_after_reservation(self):
  ident=self.prepare_add();self.event('stream_stopped',self.now-timedelta(seconds=1),{})
  with self.assertRaises(ExecutionLogError):self.tracker.assert_b3_add_dispatch('paper','005930',ident)
 def test_order_transport_guard_blocks_late_invalidation(self):
  from types import SimpleNamespace
  from unittest.mock import patch
  from kiwoom_autotrader.api import orders
  from tests.test_orders import fake_session
  e=self.add_evidence();ident=self.prepare_add(evidence=e);context=SimpleNamespace(evidence=e,tracker=self.tracker)
  def queue(*args,**kwargs):
   self.tracker.b3_invalidate_candidate('paper','005930',self.initial,self.now,'dead_cross')
   try:kwargs['before_send']()
   except Exception:raise orders.HTTPNotSentError('guard rejected') from None
   self.fail('must not send')
  with patch.object(orders,'prepare_tracking',return_value=(context,ident)),patch.object(orders,'capture_automatic_paper_guard',return_value=None),patch.object(orders,'queued_open',side_effect=queue):
   with self.assertRaises(orders.OrderError):orders.submit_order(fake_session(),'buy','005930',10,1005)
  with sqlite3.connect(self.tracker.path) as db:
   self.assertEqual(db.execute('SELECT state FROM intents WHERE id=?',(ident,)).fetchone()[0],'cancelled')
   self.assertEqual(db.execute('SELECT COUNT(*) FROM b3_add_claims').fetchone()[0],1)
 def test_negative_sma_invalidation_is_permanent(self):
  self.tracker.b3_invalidate_candidate('paper','005930',self.initial,self.now,'negative_sma_observed')
  self.assertIsNone(self.candidate())
 def test_add_claim_survives_not_sent_and_tracker_restart(self):
  ident=self.prepare_add();self.tracker.cancel_intent(ident,'not_sent');other=ExecutionTracker(log_dir=Path(self.tmp.name),now=lambda:self.now)
  self.assertIsNone(other.b3_add_candidate('paper','005930',self.now))
  with sqlite3.connect(self.tracker.path) as db:
   self.assertEqual(db.execute('SELECT COUNT(*) FROM b3_add_claims').fetchone()[0],1)
   with self.assertRaises(sqlite3.IntegrityError):db.execute('DELETE FROM b3_add_claims')
 def test_diagnostic_status_never_grants_permission(self):
  state=self.tracker.b3_add_candidate_status('paper','005930',self.now);self.assertEqual(state['status'],'candidate');self.assertEqual(state['candidate'],self.candidate())
  intent=self.prepare_add();state=self.tracker.b3_add_candidate_status('paper','005930',self.now);self.assertEqual(state['status'],'pending');self.assertIsNone(state['candidate'])
  self.tracker.cancel_intent(intent,'not_sent');self.assertEqual(self.tracker.b3_add_candidate_status('paper','005930',self.now)['status'],'claimed')
 def test_diagnostic_no_initial_and_unavailable(self):
  path=Path(self.tmp.name)/'separate';tracker=ExecutionTracker(log_dir=path,now=lambda:self.now)
  self.assertEqual(tracker.b3_add_candidate_status('paper','005930',self.now)['status'],'unavailable')
  with tracker._db():pass
  self.assertEqual(tracker.b3_add_candidate_status('paper','005930',self.now)['status'],'no_initial')
  self.event('stream_stopped',self.now-timedelta(seconds=1),{})
  self.assertEqual(self.tracker.b3_add_candidate_status('paper','005930',self.now)['status'],'unavailable')
 def test_add_checks_live_holdings_and_price_against_initial(self):
  e=self.add_evidence()
  for kw in ({'held':99},{'principal':99999},{'price':1004}):
   with self.subTest(kw=kw),self.assertRaises(ExecutionLogError):self.prepare_add(evidence=e,**kw)
  self.assertIsNotNone(self.prepare_add(evidence=e))
 def test_race_only_one_add_intent(self):
  evidence=self.add_evidence();other=ExecutionTracker(log_dir=Path(self.tmp.name),now=lambda:self.now)
  def go(t):
   try:return self.prepare_add(t,evidence=evidence)
   except (ExecutionLogError,sqlite3.Error):return None
  with ThreadPoolExecutor(max_workers=2) as pool:values=list(pool.map(go,(self.tracker,other)))
  self.assertEqual(sum(v is not None for v in values),1)
 def test_invalidation_durable_and_cannot_be_deleted(self):
  evidence=self.add_evidence();self.tracker.b3_invalidate_candidate('paper','005930',self.initial,self.now,'dead_cross')
  self.assertIsNone(self.candidate())
  self.assertEqual(self.tracker.b3_add_candidate_status('paper','005930',self.now)['status'],'invalidated')
  with self.assertRaises(ExecutionLogError):self.prepare_add(evidence=evidence)
  with sqlite3.connect(self.tracker.path) as db:
   with self.assertRaises(sqlite3.IntegrityError):db.execute('DELETE FROM b3_invalidations')
 def test_stream_stop_blocks_candidate(self):
  self.event('stream_stopped',self.now-timedelta(seconds=1),{});self.assertIsNone(self.candidate())
 def test_partial_rest_projection_blocks_candidate(self):
  with sqlite3.connect(self.tracker.path) as db:db.execute('UPDATE fills SET quantity=99')
  self.assertIsNone(self.candidate())
 def test_daily_limit_includes_initial_and_add_reservation(self):
  cfg=config();cfg.update(b3_initial_intent_id=self.initial,max_daily_entries_per_symbol=1)
  evidence=b3_addition_evidence(run_id='run',source_hash='a'*64,source_kind='broker_observation',config=cfg,observed_at=self.now,evaluated_at=self.now,window=bars(),trend_window=bars()[1:],budget=500000,initial=self.candidate())
  from kiwoom_autotrader.trading.execution_log import DailyEntryLimitReached
  with self.assertRaises(DailyEntryLimitReached):self.prepare_add(evidence=evidence)
if __name__=='__main__':unittest.main()
