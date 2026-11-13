"""Dynamic S1 evidence and final v5 reservation tests, all inputs synthetic."""
from copy import deepcopy
from datetime import datetime,timedelta
from decimal import Decimal
import json,sqlite3,unittest
from zoneinfo import ZoneInfo
from kiwoom_autotrader.api.minute_chart import MinuteBar
from kiwoom_autotrader.api.orders import OrderReceipt
from kiwoom_autotrader.api.executions import Execution
from kiwoom_autotrader.config.settings import default_settings
from kiwoom_autotrader.trading.dynamic_s1_admission import admission_digest,validate_dynamic_admission,validate_dynamic_s1_policy
from kiwoom_autotrader.trading.decision_evidence import cross_evidence,freeze_evidence
from kiwoom_autotrader.trading.execution_log import ExecutionTracker,ExecutionLogError
import tests.test_multi_strategy_epoch_review as fixture
NOW=datetime(2026,10,30,10,0,10,tzinfo=ZoneInfo('Asia/Seoul'))
def admission(now=NOW,symbol='005930'):
 v={'schema_version':'paper-dynamic-s1-admission-v1','mode':'paper','symbol':symbol,'session_sha256':'a'*64,
  'minute':now.replace(second=0,microsecond=0).isoformat(),'admitted_at':now.isoformat(),'verified_at':now.isoformat(),
  'provider_binding':'a'*32,'total_budget_krw':1000000,'initial_entry_pct':50,'initial_budget_krw':500000,
  'candidate':{'name':'삼성전자','change_pct':'5.0','surge_rate_pct':'30','turnover_krw':100000000,'available_at':now.isoformat(),
   'source_hashes':{'surge':'b'*64,'turnover':'c'*64,'membership':'d'*64},
   'membership_evidence':{'marketCode':'0','auditInfo':'정상','orderWarning':'0','state':'정상','upSizeName':'대형주',
    'source':'ka10099','available_at':now.isoformat(),'source_sha256':'d'*64,'instrument_filter':'ka10023_stk_cnd_20'},
   'liquidity_evidence':{'surge_now_trde_qty':1000,'turnover_now_trde_qty':1000,'turnover_change_pct':'5',
    'volume_unit':'shares','turnover_unit_source':'million_krw','basis':'current_day_cumulative','same_time_rvol20':None}},
  'precision_book':{'symbol':symbol,'mode':'paper','source':'ka10004_observed_top1','wire_sha256':'e'*64,'session_binding':'a'*32,
   'request_started_at':now.isoformat(),'available_at':now.isoformat(),'quote_time_inferred_kst':now.replace(microsecond=0).isoformat(),
   'source_date_verified':False,'ask':1000,'bid':999,'ask_size':500,'bid_size':500}}
 v['admission_sha256']=admission_digest(v);return v

def settings():
 v=default_settings();v.update(gdcrs_enabled=True,gdcrs_stocks=[{'code':'003220','budget':1000000}],
  gdcrs_short_period=5,gdcrs_long_period=20,market_start_time='09:00',market_end_time='15:30',
  gdcrs_initial_entry_pct=50,gdcrs_add_on_profit_bps=50,gdcrs_max_daily_entries_per_symbol=2,
  paper_gdcrs_guards={'gdcrs_min_cross_bps':10,'gdcrs_reentry_cooldown_minutes':20},
  paper_multi_strategy_enabled=True,paper_multi_strategy_ids=['S2_surge_high_breakout'],
  paper_funnel_enabled=True,paper_automation_buy_cap=1000000,paper_automation_symbol_cap=1000000,max_positions_per_theme=0)
 return v

def config():
 return {'enabled':True,'short_period':5,'long_period':20,'market_start_time':'09:00','market_end_time':'15:30',
  'entry_start_time':'09:00','entry_end_time':'15:20','stocks':[{'code':'003220','budget':1000000}],
  'min_cross_bps':10,'reentry_cooldown_minutes':20,'max_daily_entries_per_symbol':2,'b3_stage':'initial',
  'b3_initial_intent_id':None,'b3_initial_entry_pct':50,'b3_add_on_profit_bps':50,'b3_total_budget_krw':1000000}

def evidence(now=NOW,ad=None):
 bars=[MinuteBar(now.replace(second=0,microsecond=0)-timedelta(minutes=21-i),1100 if i==20 else 1000,100,source_symbol='005930',
  source_mode='paper',source_query_started_at=now,source_response_sha256='a'*64,adjustment='',adjustment_rate='') for i in range(21)]
 return cross_evidence(strategy='gdcrs',run_id='synthetic-fixture',source_hash='a'*64,source_kind='broker_observation',
  config=config(),observed_at=now,evaluated_at=now,window=bars,previous_difference=0,difference=1500,budget=500000,
  dynamic_s1_admission=ad or admission(now))

class AdmissionTests(unittest.TestCase):
 def test_valid_deep_copy_and_original_time_never_renewed(self):
  v=admission();got=validate_dynamic_admission(v,now=NOW,symbol='005930',session_sha256='a'*64)
  got['candidate']['name']='mutated';self.assertEqual(v['candidate']['name'],'삼성전자')
  validate_dynamic_admission(v,now=NOW+timedelta(seconds=5))
  for at in (NOW+timedelta(seconds=5,microseconds=1),NOW-timedelta(microseconds=1),NOW+timedelta(minutes=1)):
   with self.subTest(at=at),self.assertRaises(ValueError):validate_dynamic_admission(v,now=at)
 def test_rehashed_false_evidence_rejected(self):
  mutations=[lambda v:v.update(mode='real'),lambda v:v.update(initial_entry_pct=100),
   lambda v:v.update(initial_budget_krw=1000000),lambda v:v.update(total_budget_krw=1000001),
   lambda v:v['candidate'].update(change_pct='3.99'),lambda v:v['candidate'].update(change_pct='28.01'),
   lambda v:v['candidate'].update(surge_rate_pct='0'),lambda v:v['candidate'].update(turnover_krw=0),
   lambda v:v['candidate']['membership_evidence'].update(state='거래정지'),
   lambda v:v['candidate']['membership_evidence'].update(available_at=(NOW-timedelta(minutes=6)).isoformat()),
   lambda v:v['candidate']['membership_evidence'].update(source_sha256='f'*64),
   lambda v:v['candidate']['liquidity_evidence'].update(turnover_now_trde_qty=0),
   lambda v:v['precision_book'].update(ask_size=0),lambda v:v['precision_book'].update(symbol='000660'),
   lambda v:v['precision_book'].update(session_binding='f'*32),
   lambda v:v['precision_book'].update(quote_time_inferred_kst=(NOW-timedelta(seconds=6)).isoformat()),
   lambda v:v['candidate'].update(available_at=(NOW+timedelta(seconds=1)).isoformat())]
  for n,mutate in enumerate(mutations):
   v=admission();mutate(v);v['admission_sha256']=admission_digest(v)
   with self.subTest(n=n),self.assertRaises(ValueError):validate_dynamic_admission(v,now=NOW)
 def test_changed_hash_session_real_market_clock_rejected(self):
  v=admission();v['candidate']['name']='changed'
  with self.assertRaises(ValueError):validate_dynamic_admission(v,now=NOW)
  with self.assertRaises(ValueError):validate_dynamic_admission(admission(),now=NOW,session_sha256='f'*64)
  for now in (NOW.replace(second=45),NOW+timedelta(days=1)):
   with self.subTest(now=now),self.assertRaises(ValueError):validate_dynamic_admission(admission(now),now=now)
 def test_frozen_marker_cannot_omit_proof_or_change_symbol(self):
  original=evidence();self.assertEqual(original.as_dict()['strategy_version'],'completed-minute-cross-funnel-v1')
  for mutate in (lambda v:v.pop('dynamic_s1_admission'),lambda v:v.update(strategy_version='completed-minute-cross-v1'),
    lambda v:v.update(budget=1000000),lambda v:v['bars'][-1]['source_observation'].update(symbol='000660')):
   v=original.as_dict();mutate(v)
   with self.assertRaises(ValueError):freeze_evidence(v)
 def test_current_settings_must_match_immutable_config(self):
  validate_dynamic_s1_policy(settings(),config(),'005930')
  for update in ({'paper_funnel_enabled':False},{'gdcrs_initial_entry_pct':100,'gdcrs_add_on_profit_bps':0},
   {'gdcrs_max_daily_entries_per_symbol':0},{'paper_multi_strategy_enabled':False},{'gdcrs_short_period':6},
   {'gdcrs_stocks':[{'code':'005930','budget':1000000}]}):
   with self.subTest(update=update),self.assertRaises(ValueError):validate_dynamic_s1_policy({**settings(),**update},config(),'005930')

class DynamicLedgerTests(unittest.TestCase):
 def setUp(self):
  self.fx=fixture.EpochIndependentReview('test_complete_zero_broker_evidence_allows_new_generation_without_touching_old_history')
  self.fx.setUp();self.addCleanup(self.fx.doCleanups);self.fx.settings=settings();self.tracker=self.fx.tracker
  balance={'schema':'paper-account-snapshot-v1','mode':'paper','exchange':'KRX',
   'cash':{'entr':'10000000','ord_alow_amt':'10000000'},'holdings':[]}
  with self.tracker._db() as db:db.execute('INSERT INTO account_balance_daily VALUES(?,?,?,?,?,?)',
   ('default','paper','20261030',self.fx.now.isoformat(),'broker_observation',json.dumps(balance)))
 def prepare(self,ev=None,proof=None,quantity=10,price=1000,mode='paper'):
  return self.tracker.prepare_order(mode,'buy','005930',quantity,'gdcrs',price=price,
   evidence=ev or evidence(self.fx.now),reconciliation_evidence=proof or self.fx.proof())
 def test_v5_actual_atomic_prepare_and_queue_revalidation(self):
  ident=self.prepare();self.tracker.assert_multi_buy_dispatch('paper','005930',ident)
  self.fx.now+=timedelta(seconds=6)
  with self.assertRaises(ExecutionLogError):self.tracker.assert_multi_buy_dispatch('paper','005930',ident)
 def test_legacy_omission_v3_real_or_over_half_budget_rejected_before_insert(self):
  legacy=evidence().as_dict();legacy.pop('dynamic_s1_admission');legacy['strategy_version']='completed-minute-cross-v1'
  for kw in ({'ev':legacy},{'mode':'real'},{'quantity':501,'proof':self.fx.proof(order_quantity=501)},
   {'proof':self.fx.proof(schema_version=3)}):
   with self.subTest(kw=kw),self.assertRaises(ExecutionLogError):self.prepare(**kw)
  with sqlite3.connect(self.tracker.path) as db:self.assertEqual(db.execute("SELECT COUNT(*) FROM intents WHERE day='20261030'").fetchone()[0],0)
 def test_v5_original_epoch_still_rejects_unknown_pending(self):
  p=self.fx.proof();p['pending_query_id']=self.fx.pending_page(rows=[{'stk_cd':'000660','ord_no':'7','oso_qty':'1','stex_tp':'1'}])
  with self.assertRaises(ExecutionLogError):self.prepare(proof=p)
 def test_restart_discovers_filled_dynamic_owned_lot_without_hot_list(self):
  ident=self.prepare();self.fx.now+=timedelta(seconds=1)
  self.tracker.register_order(OrderReceipt('paper','buy','005930','10','1000','31','https://mockapi.kiwoom.com'),'gdcrs',intent_id=ident)
  self.tracker.record_broker_observation({'schema_version':1,'source_api':'00','source_kind':'broker_observation',
   'observed_at':self.fx.now.isoformat(),'day':'20261030','mode':'paper','event_kind':'execution_stream',
   'values':{'9203':'31','9001':'A005930','907':'2','913':'체결','908':'100011','909':'1','914':'1000','915':'10','911':'10','903':'10000'}})
  self.tracker._ingest('paper','20261030',(Execution('20261030','31','1','005930','fixture','buy','10:00:11',Decimal('1000'),10),))
  other=ExecutionTracker(log_dir=self.tracker.path.parent,now=lambda:self.fx.now,settings_loader=lambda:self.fx.settings)
  result=other.b3_monitor_candidates('paper',self.fx.now);self.assertEqual(result['status'],'available')
  self.assertEqual(result['candidates']['005930']['candidate']['initial_intent_id'],ident)
  other.assert_legacy_ddcrs_allowed('paper','005930');self.fx.now+=timedelta(minutes=2)
  self.assertIn('005930',other.b3_monitor_candidates('paper',self.fx.now)['candidates'])
  other.b3_invalidate_candidate('paper','005930',ident,self.fx.now,'dead_cross')
  self.assertNotIn('005930',other.b3_monitor_candidates('paper',self.fx.now)['candidates'])
 def test_owned_dynamic_add_uses_original_fill_not_fresh_admission_or_entry_window(self):
  ident=self.prepare();self.fx.now+=timedelta(seconds=1)
  self.tracker.register_order(OrderReceipt('paper','buy','005930','10','1000','31','https://mockapi.kiwoom.com'),'gdcrs',intent_id=ident)
  self.tracker.record_broker_observation({'schema_version':1,'source_api':'00','source_kind':'broker_observation',
   'observed_at':self.fx.now.isoformat(),'day':'20261030','mode':'paper','event_kind':'execution_stream',
   'values':{'9203':'31','9001':'A005930','907':'2','913':'체결','908':'100011','909':'1','914':'1000','915':'10','911':'10','903':'10000'}})
  self.tracker._ingest('paper','20261030',(Execution('20261030','31','1','005930','fixture','buy','10:00:11',Decimal('1000'),10),))
  self.fx.now=self.fx.now.replace(minute=1,second=10)
  initial=self.tracker.b3_add_candidate('paper','005930',self.fx.now)
  bars=[MinuteBar(self.fx.now.replace(second=0)-timedelta(minutes=21-i),1005,150 if i==20 else 100,
   source_symbol='005930',source_mode='paper',source_query_started_at=self.fx.now,
   source_response_sha256='a'*64,adjustment='',adjustment_rate='') for i in range(21)]
  cfg=config();cfg.update(b3_stage='add',b3_initial_intent_id=ident)
  cfg.pop('entry_start_time');cfg.pop('entry_end_time')
  from kiwoom_autotrader.trading.decision_evidence import b3_addition_evidence
  ev=b3_addition_evidence(run_id='actual-monitor-shape',source_hash='a'*64,source_kind='broker_observation',
   config=cfg,observed_at=self.fx.now,evaluated_at=self.fx.now,window=bars,trend_window=bars[1:],budget=500000,initial=initial)
  self.assertNotIn('dynamic_s1_admission',ev.as_dict());self.assertNotIn('entry_start_time',ev.as_dict()['config'])
  self.fx.settings['gdcrs_entry_start_time']='10:30'
  self.fx.settings['gdcrs_entry_end_time']='15:20'
  holding=self.tracker.record_strategy_holding_observation('005930',10,10,10000,self.fx.now,source_kind='broker_observation')
  proof={'schema_version':3,'symbol':'005930','pre_order_held_qty':10,'pre_order_pur_amt_krw':10000,
   'remaining_principal_krw':990000,'order_quantity':10,'limit_price_krw':1005,'observed_at':self.fx.now.isoformat(),
   'holdings_source':'broker_observation','symbol_cap_krw':1000000,'per_buy_cap_krw':1000000,
   'multi_strategy_enabled':True,'symbol_ledger_sha256':self.tracker.symbol_order_watermark('paper','005930'),
   'holding_observation_id':holding}
  account={'schema':'paper-account-snapshot-v1','mode':'paper','exchange':'KRX',
   'cash':{'entr':'10000000','ord_alow_amt':'10000000'},'holdings':[{'stk_cd':'005930','rmnd_qty':'10'}]}
  with self.tracker._db() as db:db.execute('UPDATE account_balance_daily SET observed_at=?,payload_json=?',
   (self.fx.now.isoformat(),json.dumps(account)))
  add=self.prepare(ev=ev,proof=proof,price=1005)
  self.tracker.assert_b3_add_dispatch('paper','005930',add)
  self.tracker.assert_multi_buy_dispatch('paper','005930',add)
  with self.assertRaises(ExecutionLogError):self.prepare(ev=ev,proof=proof,price=1005)
if __name__=='__main__':unittest.main()
