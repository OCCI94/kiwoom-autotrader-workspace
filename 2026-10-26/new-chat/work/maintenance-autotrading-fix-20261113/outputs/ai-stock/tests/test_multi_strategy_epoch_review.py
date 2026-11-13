"""Independent broker-zero epoch tests, isolated from any live environment."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timedelta
from pathlib import Path
import sqlite3,tempfile,unittest,uuid
from zoneinfo import ZoneInfo
from kiwoom_autotrader.trading.execution_log import ExecutionTracker,ExecutionLogError,DailyEntryLimitReached
from kiwoom_autotrader.api.orders import OrderReceipt
from kiwoom_autotrader.api.executions import Execution
from kiwoom_autotrader.api.minute_chart import MinuteBar
from kiwoom_autotrader.trading.multi_strategy import EntryState
from kiwoom_autotrader.trading.multi_strategy_evidence import multi_strategy_evidence
from decimal import Decimal

S2='S2_surge_high_breakout'
S3='S3_upper_limit_continuation'

class EpochIndependentReview(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
  self.now=datetime(2026,10,30,10,0,10,tzinfo=ZoneInfo('Asia/Seoul'))
  self.settings={'paper_multi_strategy_enabled':True,'paper_automation_buy_cap':1000000,'paper_automation_symbol_cap':1000000}
  self.tracker=ExecutionTracker(log_dir=Path(self.tmp.name),now=lambda:self.now,settings_loader=lambda:self.settings)
  with self.tracker._db() as db:
   for ident,side,number,at in (('a'*32,'buy','1','2026-10-29T10:00:00+09:00'),('b'*32,'sell','2','2026-10-29T10:01:00+09:00')):
    db.execute('INSERT INTO intents VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',('default',ident,'20261029','paper',side,'005930',10,'gdcrs',at,number,'accepted',None))
    db.execute('INSERT INTO orders VALUES (?,?,?,?,?,?,?,?,?)',('default','20261029','paper',number,'005930',side,'gdcrs',10,at))
    db.execute('INSERT INTO fills VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',('default','20261029','paper',number,'1','005930','fixture',side,at[11:19],'1000',10,'gdcrs',None,None,None))
  # A real program session provides this through the immutable 00 recorder.
  at=self.now-timedelta(seconds=10)
  self.anchor_at=at
  self.tracker.record_broker_observation({'schema_version':1,'source_api':'00','source_kind':'broker_observation',
    'observed_at':at.isoformat(),'day':'20261030','mode':'paper','event_kind':'stream_started','values':{}})
  with sqlite3.connect(self.tracker.path) as db:
   self.anchor_id,self.anchor_hash=db.execute("SELECT id,digest FROM broker_observations WHERE source_api='00' ORDER BY id DESC LIMIT 1").fetchone()
 def pending_page(self,*,integrated=True,rows=None,complete=True,started_offset=-2,extra_body=None):
  at=self.now-timedelta(seconds=1);query=uuid.uuid4().hex
  e={'schema':'kiwoom-broker-page-v1','source_api':'ka10075','source_kind':'broker_observation','mode':'paper',
     'day':'20261030','query_id':query,'request_started_at':(self.now+timedelta(seconds=started_offset)).isoformat(),
     'observed_at':at.isoformat(),'page':1,'request_body':{'all_stk_tp':'0','trde_tp':'0','stex_tp':'0' if integrated else '1'},
     'response_body':{'return_code':0,'oso':rows or []},
     'continuation':{'request_cont_yn':'N','response_cont_yn':'N' if complete else 'Y','next_key_present':not complete},
     'status':'success','http_status':200}
  if extra_body:e['request_body'].update(extra_body)
  self.tracker.record_broker_observation(e);return query
 def proof(self,**changes):
  observed=self.now-timedelta(seconds=3)
  holding=self.tracker.record_strategy_holding_observation('005930',0,0,0,observed,source_kind='broker_observation')
  query=self.pending_page()
  result={'schema_version':5,'symbol':'005930','pre_order_held_qty':0,'pre_order_pur_amt_krw':0,
   'remaining_principal_krw':1000000,'order_quantity':10,'limit_price_krw':1000,'observed_at':observed.isoformat(),
   'holdings_source':'broker_observation','symbol_cap_krw':1000000,'per_buy_cap_krw':1000000,
   'multi_strategy_enabled':True,'symbol_ledger_sha256':self.tracker.symbol_order_watermark('paper','005930'),
   'holding_observation_id':holding,'integrated_pending_clear':True,'pending_query_id':query,
   'recorder_observations':0,'recorder_ingress_sequence':0,'engine_ingress_sequence':0,'recorder_gap_count':0,
   'reconciliation_completed_at':(self.now-timedelta(microseconds=1)).isoformat(),'stream_anchor_id':self.anchor_id,'stream_anchor_sha256':self.anchor_hash,
   'stream_anchor_observed_at':self.anchor_at.isoformat()}
  result.update(changes);return result
 def prepare(self,proof,strategy=S2,tracker=None):
  evidence=None
  if strategy==S2:
   bars=[MinuteBar(self.now.replace(second=0)-timedelta(minutes=21-i),1000,150,
     source_symbol='005930',source_mode='paper',source_query_started_at=self.now,
     source_response_sha256='a'*64,open_price=1000,high_price=1010,low_price=990) for i in range(21)]
   market={'symbol':'005930','current_price':1000,'day_open':990,'day_high':1010,
     'upper_limit_price':1300,'change_pct':'5','request_started_at':self.now.isoformat(),
     'available_at':self.now.isoformat(),'observed_at':self.now.isoformat(),
     'source':'ka10001_normalized_response','source_sha256':'b'*64,
     'source_market_timestamp':None,'source_freshness_verified':False}
   evidence=multi_strategy_evidence(strategy=S2,symbol='005930',side='buy',
     reason='shallow_pullback_high_breakout',budget=1000000,position={'status':'flat'},
     bars=bars,market=market,observed_at=self.now,
     baseline=EntryState(bars[-1].start-timedelta(minutes=1),999,True),source_kind='synthetic')
  return (tracker or self.tracker).prepare_order('paper','buy','005930',10,strategy,price=1000,
    evidence=evidence,reconciliation_evidence=proof)
 def test_complete_zero_broker_evidence_allows_new_generation_without_touching_old_history(self):
  with sqlite3.connect(self.tracker.path) as db:before=db.execute("SELECT id,state,order_number FROM intents WHERE day='20261029' ORDER BY id").fetchall()
  ident=self.prepare(self.proof());self.assertTrue(ident)
  with sqlite3.connect(self.tracker.path) as db:after=db.execute("SELECT id,state,order_number FROM intents WHERE day='20261029' ORDER BY id").fetchall()
  self.assertEqual(after,before)
 def test_forged_or_non_integrated_pending_page_cannot_anchor(self):
  proof=self.proof(pending_query_id='f'*32)
  with self.assertRaises(ExecutionLogError):self.prepare(proof)
  proof=self.proof();proof['pending_query_id']=self.pending_page(integrated=False)
  with self.assertRaises(ExecutionLogError):self.prepare(proof)
  proof=self.proof();proof['pending_query_id']=self.pending_page(complete=False)
  with self.assertRaises(ExecutionLogError):self.prepare(proof)
 def test_other_symbol_broker_pending_is_not_ignored(self):
  proof=self.proof()
  proof['pending_query_id']=self.pending_page(rows=[{'stk_cd':'000660','ord_no':'7','oso_qty':'1','stex_tp':'1'}])
  with self.assertRaises(ExecutionLogError):self.prepare(proof)
 def test_pending_request_started_before_zero_holdings_cannot_anchor(self):
  proof=self.proof();proof['pending_query_id']=self.pending_page(started_offset=-4)
  with self.assertRaises(ExecutionLogError):self.prepare(proof)
 def test_symbol_restricted_query_cannot_prove_account_wide_empty(self):
  proof=self.proof();proof['pending_query_id']=self.pending_page(extra_body={'stk_cd':'005930'})
  with self.assertRaises(ExecutionLogError):self.prepare(proof)
 def test_prior_unresolved_intent_is_not_erased_by_new_epoch(self):
  with sqlite3.connect(self.tracker.path) as db:
   db.execute("UPDATE intents SET state='prepared',order_number=NULL WHERE id=?",('a'*32,))
  with self.assertRaises(ExecutionLogError):self.prepare(self.proof())
 def test_late_unknown_execution_invalidates_before_send(self):
  ident=self.prepare(self.proof())
  self.tracker.record_broker_observation({'schema_version':1,'source_api':'00','source_kind':'broker_observation',
    'observed_at':self.now.isoformat(),'day':'20261030','mode':'paper','event_kind':'execution_stream',
    'values':{'9203':'99','9001':'A005930','907':'2','913':'체결','908':'095900','909':'9','914':'1000','915':'1','911':'1','903':'1000'}})
  with self.assertRaises(ExecutionLogError):self.tracker.assert_multi_buy_dispatch('paper','005930',ident)
 def test_stream_stop_after_snapshot_blocks_epoch_commit(self):
  proof=self.proof()
  self.tracker.record_broker_observation({'schema_version':1,'source_api':'00','source_kind':'broker_observation',
    'observed_at':self.now.isoformat(),'day':'20261030','mode':'paper','event_kind':'stream_stopped','values':{}})
  with self.assertRaises(ExecutionLogError):self.prepare(proof)
 def test_v5_new_buy_complete_fill_creates_owned_generation_and_sell(self):
  ident=self.prepare(self.proof())
  self.now+=timedelta(seconds=1)
  self.tracker.register_order(OrderReceipt('paper','buy','005930','10','1000','31','https://mockapi.kiwoom.com'),S2,intent_id=ident)
  self.tracker.record_broker_observation({'schema_version':1,'source_api':'00','source_kind':'broker_observation',
    'observed_at':self.now.isoformat(),'day':'20261030','mode':'paper','event_kind':'execution_stream',
    'values':{'9203':'31','9001':'A005930','907':'2','913':'체결','908':'100011','909':'1','914':'1000','915':'10','911':'10','903':'10000'}})
  self.tracker._ingest('paper','20261030',(Execution('20261030','31','1','005930','fixture','buy','10:00:11',Decimal('1000'),10),))
  state=self.tracker.multi_strategy_position('paper','005930',S2,self.now)
  self.assertEqual((state['status'],state['owned_qty'],state['generation_id']),('owned',10,ident))
  holding=self.tracker.record_strategy_holding_observation('005930',10,10,10000,self.now,source_kind='broker_observation')
  proof={'schema_version':4,'symbol':'005930','pre_order_held_qty':10,'pre_order_pur_amt_krw':10000,
    'order_quantity':10,'observed_at':self.now.isoformat(),'holdings_source':'broker_observation',
    'generation_id':ident,'exit_kind':'full','strategy':S2,'holding_observation_id':holding,
    'symbol_ledger_sha256':self.tracker.symbol_order_watermark('paper','005930')}
  evidence={'strategy':S2,'config':{'multi_exit_kind':'full','multi_position_generation_id':ident}}
  exit_id=self.tracker.prepare_order('paper','sell','005930',10,S2,price=None,evidence=evidence,reconciliation_evidence=proof)
  self.tracker.assert_multi_exit_dispatch('paper','005930',exit_id)
 def test_new_gdcrs_generation_after_s2_closed_is_owned_and_ddcrs_allowed(self):
  self.test_v5_new_buy_complete_fill_creates_owned_generation_and_sell()
  with sqlite3.connect(self.tracker.path) as db:
   sell_id=db.execute("SELECT id FROM intents WHERE day='20261030' AND strategy=? AND side='sell'",(S2,)).fetchone()[0]
  self.now+=timedelta(seconds=1)
  self.tracker.register_order(OrderReceipt('paper','sell','005930','10',None,'32','https://mockapi.kiwoom.com'),S2,intent_id=sell_id)
  self.tracker.record_broker_observation({'schema_version':1,'source_api':'00','source_kind':'broker_observation',
    'observed_at':self.now.isoformat(),'day':'20261030','mode':'paper','event_kind':'execution_stream',
    'values':{'9203':'32','9001':'A005930','907':'1','913':'체결','908':'100012','909':'2','914':'1010','915':'10','911':'10','903':'10100'}})
  buy=Execution('20261030','31','1','005930','fixture','buy','10:00:11',Decimal('1000'),10)
  sell=Execution('20261030','32','2','005930','fixture','sell','10:00:12',Decimal('1010'),10)
  self.tracker._ingest('paper','20261030',(buy,sell))
  self.now+=timedelta(seconds=10)
  new_id=self.prepare(self.proof(),'gdcrs')
  self.now+=timedelta(seconds=1)
  self.tracker.register_order(OrderReceipt('paper','buy','005930','10','1000','33','https://mockapi.kiwoom.com'),'gdcrs',intent_id=new_id)
  self.tracker.record_broker_observation({'schema_version':1,'source_api':'00','source_kind':'broker_observation',
    'observed_at':self.now.isoformat(),'day':'20261030','mode':'paper','event_kind':'execution_stream',
    'values':{'9203':'33','9001':'A005930','907':'2','913':'체결','908':'100023','909':'3','914':'1000','915':'10','911':'10','903':'10000'}})
  second=Execution('20261030','33','3','005930','fixture','buy','10:00:23',Decimal('1000'),10)
  self.tracker._ingest('paper','20261030',(buy,sell,second))
  position=self.tracker.multi_strategy_position('paper','005930','ddcrs',self.now)
  self.assertEqual((position['status'],position['generation_id'],position['owned_qty']),('owned',new_id,10))
  self.tracker.assert_legacy_ddcrs_allowed('paper','005930')
 def test_broker_zero_recorded_after_intent_time_cannot_be_used(self):
  proof=self.proof();self.now-=timedelta(seconds=4)
  with self.assertRaises(ExecutionLogError):self.prepare(proof)
 def test_same_day_old_buy_counts_toward_gdcrs_daily_cap_after_anchor(self):
  with sqlite3.connect(self.tracker.path) as db:
   for table in ('intents','orders','fills'):
    db.execute("UPDATE "+table+" SET day='20261030'")
   db.execute("UPDATE intents SET created_at=CASE side WHEN 'buy' THEN '2026-10-30T09:00:00+09:00' ELSE '2026-10-30T09:01:00+09:00' END")
   db.execute("UPDATE orders SET registered_at=CASE side WHEN 'buy' THEN '2026-10-30T09:00:00+09:00' ELSE '2026-10-30T09:01:00+09:00' END")
  proof=self.proof()
  evidence={'strategy':'gdcrs','config':{'max_daily_entries_per_symbol':1}}
  with self.assertRaises(DailyEntryLimitReached):
   self.tracker.prepare_order('paper','buy','005930',10,'gdcrs',price=1000,evidence=evidence,reconciliation_evidence=proof)
 def test_conflicting_pending_or_old_completion_cannot_anchor(self):
  for changes in ({'integrated_pending_clear':False},
                  {'reconciliation_completed_at':'2026-10-30T09:59:00+09:00'}):
   with self.subTest(changes=changes),self.assertRaises(ExecutionLogError):self.prepare(self.proof(**changes))
 def test_two_strategy_attempts_cannot_share_one_zero_anchor(self):
  proof=self.proof();other=ExecutionTracker(log_dir=Path(self.tmp.name),now=lambda:self.now,settings_loader=lambda:self.settings)
  def run(pair):
   try:return self.prepare(proof,pair[1],pair[0])
   except (ExecutionLogError,sqlite3.Error):return None
  with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(run,((self.tracker,S2),(other,S3))))
  self.assertEqual(sum(x is not None for x in results),1)
