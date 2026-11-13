"""Realistic local ledger fixtures: no provider calls, credentials or live DB."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
import sqlite3, unittest
from unittest.mock import patch
import tests.test_b3_ledger as b3_fixture
from kiwoom_autotrader.api.executions import Execution
from kiwoom_autotrader.api.orders import OrderReceipt
from kiwoom_autotrader.trading.execution_log import ExecutionLogError, ExecutionTracker

STRATEGY='S2_surge_high_breakout'

class MultiExitTests(unittest.TestCase):
 def setUp(self):
  self.fixture=b3_fixture.B3LedgerTests('test_fully_reconciled_00_and_rest_yield_exact_candidate')
  self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)
  self.tmp=self.fixture.tmp;self.tracker=self.fixture.tracker;self.initial=self.fixture.initial
  with sqlite3.connect(self.tracker.path) as db:
   db.execute('UPDATE intents SET strategy=? WHERE id=?',(STRATEGY,self.initial))
   db.execute('UPDATE orders SET strategy=? WHERE ord_no=?',(STRATEGY,'24'))
  self.settings={'paper_multi_strategy_enabled':True}
  self.tracker.settings_loader=lambda:self.settings
 @property
 def now(self):return self.fixture.now
 @now.setter
 def now(self,value):self.fixture.now=value
 def event(self,*args):return self.fixture.event(*args)
 def proof(self,kind='half',quantity=50,held=100,basis=100000):
  observation_id=self.tracker.record_strategy_holding_observation('005930',held,held,basis,self.now,source_kind='broker_observation')
  return {'schema_version':4,'symbol':'005930','pre_order_held_qty':held,'pre_order_pur_amt_krw':basis,
          'order_quantity':quantity,'observed_at':self.now.isoformat(),'holdings_source':'broker_observation',
          'generation_id':self.initial,'exit_kind':kind,'strategy':STRATEGY,
          'symbol_ledger_sha256':self.tracker.symbol_order_watermark('paper','005930'),'holding_observation_id':observation_id}
 def exit(self,proof,tracker=None):
  e={'strategy':STRATEGY,'config':{'multi_exit_kind':proof['exit_kind'],'multi_position_generation_id':self.initial}}
  return (tracker or self.tracker).prepare_order('paper','sell','005930',proof['order_quantity'],STRATEGY,
                                                 price=None,evidence=e,reconciliation_evidence=proof)
 def test_half_claim_is_durable_and_dispatch_valid(self):
  proof=self.proof();ident=self.exit(proof)
  self.tracker.assert_multi_exit_dispatch('paper','005930',ident)
  self.tracker.cancel_intent(ident,'not_sent')
  next_ident=self.exit(self.proof())
  self.tracker.assert_multi_exit_dispatch('paper','005930',next_ident)
  with sqlite3.connect(self.tracker.path) as db:
   self.assertEqual(db.execute('SELECT count(*) FROM multi_strategy_exit_releases').fetchone()[0],1)
   self.assertEqual(db.execute('SELECT count(*) FROM multi_strategy_exit_claims').fetchone()[0],2)
   with self.assertRaises(sqlite3.IntegrityError):db.execute('DELETE FROM multi_strategy_exit_claims')
 def test_changed_stream_or_another_order_blocks_queued_exit(self):
  ident=self.exit(self.proof())
  self.event('stream_stopped',self.now,{})
  with self.assertRaises(ExecutionLogError):self.tracker.assert_multi_exit_dispatch('paper','005930',ident)
 def test_age_and_wrong_position_fail_closed(self):
  p=self.proof(held=99)
  with self.assertRaises(ExecutionLogError):self.exit(p)
  ident=self.exit(self.proof());self.now+=timedelta(seconds=21)
  with self.assertRaises(ExecutionLogError):self.tracker.assert_multi_exit_dispatch('paper','005930',ident)
 def test_two_instances_cannot_reserve_same_exit(self):
  proof=self.proof();other=ExecutionTracker(log_dir=Path(self.tmp.name),now=lambda:self.now,settings_loader=lambda:self.settings)
  def run(tracker):
   try:return self.exit(proof,tracker)
   except (ExecutionLogError,sqlite3.Error):return None
  with ThreadPoolExecutor(max_workers=2) as executor:result=list(executor.map(run,(self.tracker,other)))
  self.assertEqual(sum(x is not None for x in result),1)
 def test_half_fill_then_full_exit_retains_single_owner(self):
  ident=self.exit(self.proof())
  self.tracker.register_order(OrderReceipt('paper','sell','005930','50',None,'25','https://mockapi.kiwoom.com'),STRATEGY,intent_id=ident)
  self.event('execution_stream',self.now,{'9203':'25','9001':'A005930','907':'1','913':'체결','908':'092100','909':'2','914':'1100','915':'50','911':'50','903':'55000'})
  self.tracker._ingest('paper','20261030',(Execution('20261030','24','1','005930','fixture','buy','09:20:30',Decimal('1000'),100),
                                             Execution('20261030','25','2','005930','fixture','sell','09:21:00',Decimal('1100'),50)))
  pos=self.tracker.multi_strategy_position('paper','005930',STRATEGY,self.now)
  self.assertEqual((pos['status'],pos['owned_qty'],pos['half_take_done']),('owned',50,True))
  proof=self.proof(kind='full',quantity=50,held=50,basis=50000)
  ident=self.exit(proof);self.tracker.assert_multi_exit_dispatch('paper','005930',ident)
 def test_order_transport_rechecks_exit_after_queue_wait(self):
  from kiwoom_autotrader.api import orders
  from kiwoom_autotrader.trading.order_context import automated_orders
  from tests.test_orders import fake_session
  proof=self.proof()
  class Guard:
   reconciliation_evidence=proof
   def before_prepare(self):return True
   def __call__(self):return None
  def queue(*args,**kwargs):
   self.event('stream_stopped',self.now,{})
   try:kwargs['before_send']()
   except Exception:raise orders.HTTPNotSentError('guard rejected') from None
   self.fail('transport must not send')
  evidence={'strategy':STRATEGY,'config':{'multi_exit_kind':'half','multi_position_generation_id':self.initial}}
  with automated_orders(self.tracker,STRATEGY,evidence=evidence), patch.object(orders,'capture_automatic_paper_guard',return_value=Guard()), patch.object(orders,'queued_open',side_effect=queue):
   with self.assertRaises(orders.OrderError):orders.submit_order(fake_session(),'sell','005930',50)
  with sqlite3.connect(self.tracker.path) as db:
   self.assertEqual(db.execute('SELECT state FROM intents WHERE side=?',('sell',)).fetchone()[0],'cancelled')
   self.assertEqual(db.execute('SELECT count(*) FROM multi_strategy_exit_claims').fetchone()[0],1)
 def test_monitor_denominator_keeps_holding_and_unresolved_even_if_unconfigured(self):
  with sqlite3.connect(self.tracker.path) as db:
   db.execute('INSERT INTO holdings VALUES (?,?,?,?,?)',('default','paper',self.now.isoformat(),'{"005930":100,"000660":2}','[]'))
   db.execute('INSERT INTO intents VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',('default','d'*32,'20261030','paper','buy','003220',1,STRATEGY,self.now.isoformat(),None,'prepared',None))
  result=self.tracker.multi_strategy_open_symbols('paper',self.now)
  self.assertEqual(result['status'],'available')
  self.assertEqual(result['symbols'],['000660','003220','005930'])
 def test_legacy_ddcrs_is_allowed_without_new_strategy_history_and_blocked_with_it(self):
  with self.assertRaises(ExecutionLogError):
   self.tracker.assert_legacy_ddcrs_allowed('paper','005930')
  self.tracker.assert_legacy_ddcrs_allowed('paper','003220')
  with self.assertRaises(ExecutionLogError):
   self.tracker.prepare_order('paper','sell','005930',100,'ddcrs',price=None,require_no_pending=True)
 def test_s3_protective_exit_can_reconcile_verified_overnight_generation(self):
  strategy='S3_upper_limit_continuation'
  with sqlite3.connect(self.tracker.path) as db:
   db.execute('UPDATE intents SET strategy=? WHERE id=?',(strategy,self.initial))
   db.execute('UPDATE orders SET strategy=? WHERE ord_no=?',(strategy,'24'))
  self.event('stream_stopped',self.now+timedelta(seconds=1),{})
  self.now+=timedelta(days=1)
  pos=self.tracker.multi_strategy_position('paper','005930',strategy,self.now)
  self.assertEqual((pos['status'],pos['owned_qty'],pos['basis_krw']),('owned',100,100000))
  self.assertTrue(pos['overnight_unobserved_interval'])
  proof=self.proof(kind='full',quantity=100);proof['strategy']=strategy
  evidence={'strategy':strategy,'config':{'multi_exit_kind':'full','multi_position_generation_id':self.initial}}
  ident=self.tracker.prepare_order('paper','sell','005930',100,strategy,price=None,evidence=evidence,reconciliation_evidence=proof)
  self.tracker.assert_multi_exit_dispatch('paper','005930',ident)
 def test_overnight_generation_does_not_hide_missing_unit_fill(self):
  self.now+=timedelta(days=1)
  with sqlite3.connect(self.tracker.path) as db:db.execute('UPDATE fills SET quantity=99')
  self.assertIn(self.tracker.multi_strategy_position('paper','005930',STRATEGY,self.now)['status'],('pending','unsupported'))
 def test_order_number_reuse_on_different_days_does_not_merge_executions(self):
  self.now+=timedelta(days=1)
  def observe(at,kind,values):
   self.tracker.record_broker_observation({'schema_version':1,'source_api':'00','source_kind':'broker_observation',
      'observed_at':at.isoformat(),'day':at.strftime('%Y%m%d'),'mode':'paper','event_kind':kind,'values':values})
  observe(self.now-timedelta(seconds=1),'stream_started',{})
  proof=self.proof(kind='full',quantity=100)
  ident=self.exit(proof)
  self.tracker.register_order(OrderReceipt('paper','sell','005930','100',None,'24','https://mockapi.kiwoom.com'),STRATEGY,intent_id=ident)
  observe(self.now,'execution_stream',{'9203':'24','9001':'A005930','907':'1','913':'체결','908':'092100','909':'1','914':'1100','915':'100','911':'100','903':'110000'})
  self.tracker._ingest('paper',self.now.strftime('%Y%m%d'),(Execution(self.now.strftime('%Y%m%d'),'24','1','005930','fixture','sell','09:21:00',Decimal('1100'),100),))
  self.assertEqual(self.tracker.multi_strategy_position('paper','005930',STRATEGY,self.now)['status'],'flat')
