"""Protective broker quantity proof never upgrades strict unit attribution."""
from datetime import datetime,timedelta
from decimal import Decimal
import json,sqlite3,tempfile,unittest
from pathlib import Path
from unittest.mock import Mock
from kiwoom_autotrader.api.session import KST
from kiwoom_autotrader.api.orders import OrderReceipt
from kiwoom_autotrader.api.executions import Execution
from kiwoom_autotrader.trading.execution_log import ExecutionTracker,ExecutionLogError
S='S2_surge_high_breakout';DAY='20261103'

class BrokerExitOnlyTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
  self.now=datetime(2026,11,3,9,20,tzinfo=KST)
  self.tracker=ExecutionTracker(log_dir=Path(self.tmp.name),now=lambda:self.now,settings_loader=lambda:{'max_positions_per_theme':0},
   account_fetcher=Mock(),executions_fetcher=Mock(),profit_fetcher=Mock())
  self.event('stream_started',self.now-timedelta(seconds=1))
  self.initial=self.tracker.prepare_order('paper','buy','005930',100,'gdcrs',price=1000,
   reconciliation_evidence={'schema_version':1,'symbol':'005930','pre_order_held_qty':0,'pre_order_pur_amt_krw':0,'remaining_principal_krw':1000000,
    'order_quantity':100,'limit_price_krw':1000,'observed_at':self.now.isoformat(),'holdings_source':'broker_observation'})
  self.tracker.register_order(OrderReceipt('paper','buy','005930','100','1000','24','https://example.invalid'),'gdcrs',intent_id=self.initial)
  with sqlite3.connect(self.tracker.path) as db:
   db.execute('UPDATE intents SET strategy=?',(S,));db.execute('UPDATE orders SET strategy=?',(S,))
  self.tracker.settings_loader=lambda:{'paper_multi_strategy_enabled':True}
 def event(self,kind,at,values=None):
  self.tracker.record_broker_observation({'schema_version':1,'source_api':'00','source_kind':'broker_observation','observed_at':at.isoformat(),
   'day':DAY,'mode':'paper','event_kind':kind,'values':values or {}})
 def fill(self,at,num,qty,cum):
  self.event('execution_stream',at,{'9203':'24','9001':'A005930','907':'2','913':'체결','908':at.strftime('%H%M%S'),'909':str(num),
   '914':'1000','915':str(qty),'911':str(cum),'903':str(cum*1000)})
 def page(self,api,at,rows,*,incomplete=False):
  body={'ord_dt':DAY,'qry_tp':'1','stk_bond_tp':'1','sell_tp':'0','dmst_stex_tp':'%'} if api=='kt00007' else {'all_stk_tp':'0','trde_tp':'0','stex_tp':'0'}
  event={'schema':'kiwoom-broker-page-v1','source_api':api,'source_kind':'broker_observation','mode':'paper','day':DAY,
   'query_id':format(int(at.timestamp()*1000000),'032x'),'request_started_at':at.isoformat(),'observed_at':at.isoformat(),'page':1,'request_body':body,
   'response_body':{'return_code':0,('acnt_ord_cntr_prps_dtl' if api=='kt00007' else 'oso'):rows},
   'continuation':{'request_cont_yn':'N','response_cont_yn':'Y' if incomplete else 'N','next_key_present':incomplete},'status':'success','http_status':200}
  self.tracker.record_broker_observation(event)
 def broker(self,*,incomplete=False,external=False,pending=False,minute=22):
  self.now=self.now.replace(minute=minute,second=0)
  rows=[{'ord_no':'24','stk_cd':'A005930','io_tp_nm':'현금매수','dmst_stex_tp':'KRX','ord_qty':'100','cntr_qty':'100','ord_remnq':'0'}]
  if external:rows.append(dict(rows[0],ord_no='25'))
  self.page('kt00007',self.now,rows,incomplete=incomplete)
  self.now+=timedelta(seconds=1)
  payload={'schema':'paper-account-snapshot-v1','mode':'paper','exchange':'KRX','pages':{'kt00001':1,'kt00018':1},
    'holdings':[{'stk_cd':'A005930','crd_tp':'00','rmnd_qty':'100','trde_able_qty':'100','pur_amt':'100000'}]}
  with self.tracker._db() as db:db.execute('INSERT OR REPLACE INTO account_balance_daily VALUES (?,?,?,?,?,?)',
   ('default','paper',DAY,self.now.isoformat(),'broker_observation',json.dumps(payload)))
  self.now+=timedelta(seconds=1);self.page('ka10075',self.now,[{'stk_cd':'A005930','ord_no':'24','oso_qty':'1'}] if pending else [])
  self.now+=timedelta(seconds=1)
 def setup_gap(self,where='between'):
  first=self.now+timedelta(seconds=10);last=self.now+timedelta(seconds=20)
  self.fill(first,1,40,40)
  gap=first+timedelta(seconds=5) if where=='between' else last if where=='equal' else last+timedelta(seconds=1)
  self.event('stream_stopped',gap)
  self.fill(last,2,60,100);self.event('stream_resumed',last+timedelta(seconds=2))
  self.tracker._ingest('paper',DAY,(Execution(DAY,'24','1','005930','fixture','buy','09:20:10',Decimal('1000'),40),
   Execution(DAY,'24','2','005930','fixture','buy','09:20:20',Decimal('1000'),60)))
  self.broker()
 def pos(self):return self.tracker.paper_exit_only_position('paper','005930',S,self.now)
 def proof(self):
  pos=self.pos();self.assertEqual(pos['status'],'exit_only_broker_confirmed',pos)
  ident=self.tracker.record_strategy_holding_observation('005930',100,100,100000,self.now,source_kind='broker_observation')
  return {'schema_version':7,'symbol':'005930','pre_order_held_qty':100,'pre_order_pur_amt_krw':100000,'order_quantity':100,
    'observed_at':self.now.isoformat(),'holdings_source':'broker_observation','generation_id':self.initial,'exit_kind':'full','strategy':S,
    'symbol_ledger_sha256':self.tracker.symbol_order_watermark('paper','005930'),'holding_observation_id':ident,
    'ownership_status':'exit_only_broker_confirmed','profit_attribution':'unconfirmed','exit_only_broker_proof':pos['evidence']}
 def prepare(self,proof):
  return self.tracker.prepare_order('paper','sell','005930',100,S,price=None,reconciliation_evidence=proof,
    evidence={'strategy':S,'source_kind':'broker_observation','config':{'multi_exit_kind':'full','multi_position_generation_id':self.initial,
      'ownership_status':'exit_only_broker_confirmed','profit_attribution':'unconfirmed'}})
 def test_gap_between_partials_exit_only_and_strict_ownership_stays_unsupported(self):
  self.setup_gap();self.assertEqual(self.tracker.multi_strategy_position('paper','005930',S,self.now)['status'],'unsupported')
  p=self.pos();self.assertEqual(p['status'],'exit_only_broker_confirmed',p);self.assertEqual(p['profit_attribution'],'unconfirmed')
  self.assertTrue(p['first_fill_timing_verified']);self.assertEqual(p['first_fill_at'],'2026-11-03T09:20:10+09:00')
  ident=self.prepare(self.proof());self.tracker.assert_multi_exit_dispatch('paper','005930',ident)
 def test_gap_at_last_fill_is_exit_only_and_gap_after_preserves_strict_path(self):
  self.setup_gap('equal');self.assertEqual(self.tracker.multi_strategy_position('paper','005930',S,self.now)['status'],'unsupported')
  self.assertEqual(self.pos()['status'],'exit_only_broker_confirmed')
 def test_gap_after_last_fill_uses_existing_strict_restore(self):
  self.setup_gap('after');self.assertEqual(self.tracker.multi_strategy_position('paper','005930',S,self.now)['status'],'owned')
 def test_expired_account_external_order_incomplete_and_pending_fail_closed(self):
  self.setup_gap();self.now+=timedelta(seconds=91);self.assertEqual(self.pos()['status'],'unsupported')
  for kwargs in ({'external':True},{'incomplete':True},{'pending':True}):
   with self.subTest(kwargs=kwargs):self.broker(**kwargs);self.assertEqual(self.pos()['status'],'unsupported')
 def test_late00_after_reservation_and_uncertain_second_intent_cannot_dispatch(self):
  self.setup_gap();proof=self.proof();ident=self.prepare(proof)
  with self.assertRaises(ExecutionLogError):self.prepare(proof)
  self.event('execution_stream',self.now,{'9203':'24','9001':'A005930','907':'2','913':'접수'})
  with self.assertRaises(ExecutionLogError):self.tracker.assert_multi_exit_dispatch('paper','005930',ident)
 def test_changed_broker_quantity_before_prepare_rejects_stale_proof(self):
  self.setup_gap();proof=self.proof()
  with self.tracker._db() as db:
   row=db.execute('SELECT payload_json FROM account_balance_daily').fetchone();payload=json.loads(row[0]);payload['holdings'][0]['rmnd_qty']='99'
   db.execute('UPDATE account_balance_daily SET payload_json=?',(json.dumps(payload),))
  with self.assertRaises(ExecutionLogError):self.prepare(proof)
 def test_exit_only_cannot_authorize_buy_or_real_order(self):
  self.setup_gap();proof=self.proof()
  with self.assertRaises(ExecutionLogError):self.tracker.prepare_order('paper','buy','005930',100,S,price=1000,reconciliation_evidence=proof)
  self.assertEqual(self.tracker.paper_exit_only_position('real','005930',S,self.now)['status'],'unsupported')

 def test_repeated_complete_broker_pages_preserve_both_strict_and_exit_only_results(self):
  self.setup_gap('after');self.broker(minute=23)
  with self.tracker._db() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM broker_observation_repeats').fetchone()[0],2)
  self.assertEqual(self.pos()['status'],'exit_only_broker_confirmed')
  self.assertEqual(self.tracker.multi_strategy_position('paper','005930',S,self.now)['status'],'owned')

 def test_final_pending_page_with_next_key_is_not_complete_exit_proof(self):
  self.setup_gap();self.assertEqual(self.pos()['status'],'exit_only_broker_confirmed')
  # The recorder preserves provider headers, including contradictory ones.
  # An N response with a remaining next key cannot prove all pending pages.
  with self.tracker._db() as db:
   saved=db.execute("SELECT payload_json FROM broker_observation_evidence WHERE source_api='ka10075' ORDER BY id DESC LIMIT 1").fetchone()
  event=json.loads(saved[0]);event['query_id']='f'*32
  event['request_started_at']=self.now.isoformat();event['observed_at']=self.now.isoformat()
  event['continuation']['next_key_present']=True
  self.tracker.record_broker_observation(event)
  position=self.pos()
  self.assertEqual(position['status'],'unsupported',position)
  self.assertEqual(position['reason'],'exit_only_ka10075_incomplete')
  with self.tracker._db() as db:
   raw=db.execute("SELECT payload_json FROM broker_observation_evidence WHERE source_api='ka10075' ORDER BY id DESC LIMIT 1").fetchone()[0]
  self.assertTrue(json.loads(raw)['continuation']['next_key_present'])

 def test_final_order_detail_page_with_next_key_is_not_complete_exit_proof(self):
  self.setup_gap()
  with self.tracker._db() as db:
   saved=db.execute("SELECT payload_json FROM broker_observation_evidence WHERE source_api='kt00007' ORDER BY id DESC LIMIT 1").fetchone()
  event=json.loads(saved[0]);event['query_id']='e'*32
  event['continuation']['next_key_present']=True
  self.tracker.record_broker_observation(event)
  position=self.pos()
  self.assertEqual(position['status'],'unsupported',position)
  self.assertEqual(position['reason'],'exit_only_kt00007_incomplete')
