"""Isolated future S5 ledger evidence; never reads a real account database."""
from datetime import timedelta
from decimal import Decimal
import hashlib,json,sqlite3,unittest
from kiwoom_autotrader.trading.realized_generation import S5,collect_realized_profit_evidence,fifo_realized_evidence,prepare_realized_reader
from kiwoom_autotrader.trading.fee_equivalent import realized_profit_tracks
from tests import test_s5_ledger_roundtrip as fixtures

class RealizedGenerationTests(unittest.TestCase):
 def setUp(self):
  self.f=fixtures.S5LedgerRoundtripTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
  self.f.test_full_v4_and_v5_initial_dispatch_unit_fill_restart_and_time_stop_sell()
  self.t=self.f.tracker
 def result(self):
  with sqlite3.connect(self.t.path.resolve().as_uri()+'?mode=ro',uri=True) as db:
   db.row_factory=sqlite3.Row;prepare_realized_reader(db);db.execute('PRAGMA query_only=ON');db.execute('PRAGMA trusted_schema=OFF');db.execute('BEGIN')
   return collect_realized_profit_evidence(db,'20261102',self.f.now+timedelta(hours=1))
 def test_real_s5_roundtrip_exact_three_tracks_and_no_write(self):
  before=self.t.path.read_bytes();r=self.result();self.assertEqual(before,self.t.path.read_bytes())
  self.assertIn('003220',r['by_symbol'],r)
  e=r['by_symbol']['003220'];p=realized_profit_tracks(e)
  buy=Decimal(e['matched_buy_amount_krw']);sell=Decimal(e['matched_sell_amount_krw'])
  self.assertEqual(Decimal(p['gross_verified_krw']),sell-buy);self.assertIsNone(p['mock_net_krw'])
  self.assertEqual(Decimal(p['real_fee_equivalent_krw']),sell-buy-(buy+sell)*Decimal('.00015')-sell*Decimal('.002'))
  self.assertEqual(e['sold_quantity'],10);self.assertFalse(p['instrument_fee_classification_verified'])
 def test_incomplete_buy_and_forged_order_identity_stay_unknown(self):
  with sqlite3.connect(self.t.path) as db:db.execute("UPDATE orders SET quantity=11 WHERE ord_no='24'")
  self.assertFalse(self.result()['by_symbol'])
 def test_unbound_exit_claim_blocks(self):
  with sqlite3.connect(self.t.path) as db:
   db.execute('DROP TRIGGER multi_strategy_exit_claims_no_update');db.execute("UPDATE multi_strategy_exit_claims SET generation_id=?",('f'*32,))
  self.assertFalse(self.result()['by_symbol'])
 def test_gap_within_generation_blocks(self):
  self.f.event('stream_gap',self.f.now-timedelta(minutes=1),{})
  self.assertFalse(self.result()['by_symbol'])
 def test_later_stop_does_not_erase_completed_sold_scope(self):
  self.f.event('stream_stopped',self.f.now+timedelta(seconds=1),{})
  self.assertIn('003220',self.result()['by_symbol'])
 def test_unknown_external_order_within_generation_blocks(self):
  at=self.f.now-timedelta(minutes=1)
  self.f.event('execution_stream',at,{'9203':'99','9001':'A003220','907':'2','913':'체결','908':at.strftime('%H%M%S'),'909':'9','914':'1000','915':'1','911':'1','903':'1000'})
  self.assertFalse(self.result()['by_symbol'])
 def test_tampered_00_digest_blocks(self):
  with sqlite3.connect(self.t.path) as db:
   db.execute('DROP TRIGGER broker_observations_no_update');db.execute("UPDATE broker_observations SET digest=? WHERE source_api='00'",('f'*64,))
  self.assertFalse(self.result()['by_symbol'])
 def test_missing_initial_v5_does_not_infer_basis(self):
  with sqlite3.connect(self.t.path) as db:
   db.execute('DROP TRIGGER pre_order_reconciliations_no_delete');db.execute("DELETE FROM pre_order_reconciliations WHERE intent_id IN (SELECT id FROM intents WHERE side='buy')")
  self.assertFalse(self.result()['by_symbol'])
 def test_queryonly_transaction_required(self):
  with sqlite3.connect(self.t.path) as db:
   db.row_factory=sqlite3.Row
   self.assertEqual(collect_realized_profit_evidence(db,'20261102',self.f.now)['status'],'unavailable')
 def test_missing_reader_adapter_fails_closed_without_relaxing_query_only(self):
  before=self.t.path.read_bytes()
  with sqlite3.connect(self.t.path.resolve().as_uri()+'?mode=ro',uri=True) as db:
   db.row_factory=sqlite3.Row;db.execute('PRAGMA query_only=ON');db.execute('BEGIN')
   r=collect_realized_profit_evidence(db,'20261102',self.f.now)
   self.assertEqual(r['status'],'unavailable');self.assertEqual(r['diagnostics']['reader'],'broker_repeat_reader_adapter_required')
   with self.assertRaises(ValueError):prepare_realized_reader(db)
   self.assertEqual(db.execute('PRAGMA query_only').fetchone()[0],1);self.assertTrue(db.in_transaction)
  self.assertEqual(before,self.t.path.read_bytes())
 def test_pure_fifo_partial_sales_add_on_and_unknown_basis(self):
  def unit(n,side,qty,price):return {'order_number':str(n),'execution_id':str(n),'side':side,'price':str(price),'quantity':qty,'executed_at':f'2026-11-02T10:0{n}:00+09:00','observed_at':f'2026-11-02T10:0{n}:01+09:00'}
  g={'reader_validated':True,'mode':'paper','strategy':S5,'scope':'default','symbol':'003220','generation_id':'g','evidence_refs':['00:fixture'],'units':[unit(1,'buy',4,1000),unit(2,'buy',6,1100),unit(3,'sell',5,1200)]}
  e=fifo_realized_evidence(g,'20261102');self.assertEqual(e['matched_buy_amount_krw'],'5100');self.assertEqual(e['verified_gross_pnl_krw'],'900');self.assertEqual(e['remaining_observed_quantity'],5)
  g['units'].append(unit(4,'sell',5,1300));e=fifo_realized_evidence(g,'20261102');self.assertEqual(e['matched_buy_amount_krw'],'10600');self.assertEqual(e['verified_gross_pnl_krw'],'1900')
  g['units']=g['units'][2:]
  with self.assertRaises(ValueError):fifo_realized_evidence(g,'20261102')
 def _add_b3_fixture(self):
  with sqlite3.connect(self.t.path) as db:
   db.row_factory=sqlite3.Row
   first=dict(db.execute("SELECT * FROM intents WHERE side='buy'").fetchone())
   sell=dict(db.execute("SELECT * FROM intents WHERE side='sell'").fetchone())
   original=dict(db.execute("SELECT * FROM pre_order_reconciliations WHERE intent_id=?",(sell['id'],)).fetchone())
   sp=json.loads(original['evidence_json']);initial_basis=sp['pre_order_pur_amt_krw']
  from dataclasses import replace
  from tests.test_s5_source_bound_evidence import fixture
  from kiwoom_autotrader.trading.multi_b3 import evaluate_add_on
  from kiwoom_autotrader.trading.multi_strategy_evidence import multi_strategy_evidence
  from kiwoom_autotrader.trading.decision_evidence import freeze_evidence
  at,bars,market=fixture(80);ident='e'*32;price=initial_basis//10+100
  bars=[replace(b,adjustment='',adjustment_rate='') for b in bars]
  bars[-1]=replace(bars[-1],volume=2000);market['current_price']=price
  with sqlite3.connect(self.t.path) as db:
   first_dec=json.loads(db.execute('SELECT evidence_json FROM order_decisions WHERE intent_id=?',(first['id'],)).fetchone()[0])
   sell_dec=json.loads(db.execute('SELECT evidence_json FROM order_decisions WHERE intent_id=?',(sell['id'],)).fetchone()[0])
  pos=dict(sell_dec['multi_strategy_signal']['position']);pos.update(owned_qty=10,basis_krw=initial_basis,overnight_unobserved_interval=False)
  candidate={'config':first_dec['config'],'initial_intent_id':first['id'],'initial_filled_qty':10,'initial_filled_principal_krw':initial_basis,'initial_fill_observed_at':pos['first_fill_observed_at']}
  addition=evaluate_add_on(candidate,bars=bars,market=market,position=pos,now=at,symbol='003220',settings={'gdcrs_initial_entry_pct':50,'gdcrs_add_on_profit_bps':50,'gdcrs_stocks':[{'code':'003220','budget':1000000}]})
  self.assertTrue(addition['eligible'],addition)
  de=multi_strategy_evidence(strategy=S5,symbol='003220',side='buy',reason='add_on_strength',budget=addition['budget'],position=pos,bars=bars,market=market,observed_at=at,b3_config=addition['config'],b3_add_on=addition['addition'],source_hash='f'*64).as_dict()
  hid=self.t.record_strategy_holding_observation('003220',10,10,initial_basis,at,source_kind='broker_observation')
  bp={'schema_version':3,'symbol':'003220','pre_order_held_qty':10,'pre_order_pur_amt_krw':initial_basis,'order_quantity':10,'observed_at':at.isoformat(),'holdings_source':'broker_observation','holding_observation_id':hid}
  raw=lambda x:json.dumps(x,sort_keys=True,separators=(',',':'))
  with sqlite3.connect(self.t.path) as db:
   db.execute('INSERT INTO intents VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',('default',ident,'20261102','paper','buy','003220',10,S5,at.isoformat(),'26','accepted',None))
   db.execute('INSERT INTO orders VALUES (?,?,?,?,?,?,?,?,?)',('default','20261102','paper','26','003220','buy',S5,10,at.isoformat()))
   db.execute('INSERT INTO b3_add_claims VALUES (?,?,?,?)',('default',first['id'],ident,at.isoformat()))
   db.execute('INSERT INTO pre_order_reconciliations VALUES (?,?,?,?,?)',('default',ident,at.isoformat(),raw(bp),hashlib.sha256(raw(bp).encode()).hexdigest()))
   db.execute('INSERT INTO order_decisions VALUES (?,?,?,?,?,?,?)',('default',ident,at.isoformat(),str(price),'limit',raw(de),hashlib.sha256(raw(de).encode()).hexdigest()))
  self.f.event('execution_stream',at+timedelta(seconds=1),{'9203':'26','9001':'A003220','907':'2','913':'체결','908':(at+timedelta(seconds=1)).strftime('%H%M%S'),'909':'3','914':str(price),'915':'10','911':'10','903':str(price*10)})
  from kiwoom_autotrader.api.executions import Execution
  self.t._ingest('paper','20261102',(Execution('20261102','26','3','003220','fixture','buy',(at+timedelta(seconds=1)).strftime('%H:%M:%S'),Decimal(price),10),))
  sell_at=self.f.now-timedelta(seconds=1)
  shid=self.t.record_strategy_holding_observation('003220',20,20,initial_basis+price*10,sell_at,source_kind='broker_observation')
  sp.update(pre_order_held_qty=20,pre_order_pur_amt_krw=initial_basis+price*10,holding_observation_id=shid,observed_at=sell_at.isoformat(),order_quantity=20)
  with sqlite3.connect(self.t.path) as db:
   db.row_factory=sqlite3.Row
   db.execute('DROP TRIGGER pre_order_reconciliations_no_update')
   sell_dec['multi_strategy_signal']['quantity']=20
   sell_dec['multi_strategy_signal']['position'].update(owned_qty=20,basis_krw=initial_basis+price*10)
   sf=freeze_evidence(sell_dec)
   db.execute('DROP TRIGGER order_decisions_no_update')
   db.execute('UPDATE order_decisions SET evidence_json=?,evidence_sha256=? WHERE intent_id=?',(sf.json,sf.sha256,sell['id']))
   db.execute('UPDATE pre_order_reconciliations SET evidence_json=?,evidence_sha256=? WHERE intent_id=?',(raw(sp),hashlib.sha256(raw(sp).encode()).hexdigest(),sell['id']))
   db.execute("UPDATE intents SET quantity=20 WHERE side='sell'");db.execute("UPDATE orders SET quantity=20 WHERE side='sell'");db.execute("UPDATE fills SET quantity=20 WHERE side='sell'")
   db.execute('DROP TRIGGER broker_observations_no_update')
   for row in db.execute("SELECT * FROM broker_observations WHERE source_api='00'").fetchall():
    ev=json.loads(row['payload_json']);v=ev.get('values') or {}
    if v.get('9203')=='25':
     v.update({'915':'20','911':'20','903':str(int(v['914'])*20)});text=raw(ev)
     db.execute('UPDATE broker_observations SET payload_json=?,digest=? WHERE id=?',(text,hashlib.sha256(text.encode()).hexdigest(),row['id']))
  return initial_basis+price*10
 def test_bound_b3_addition_fifo_uses_both_actual_acquisitions(self):
  basis=self._add_b3_fixture();r=self.result();self.assertIn('003220',r['by_symbol'],r)
  e=r['by_symbol']['003220'];self.assertEqual(e['sold_quantity'],20);self.assertEqual(Decimal(e['matched_buy_amount_krw']),basis)
 def test_b3_claim_wrong_generation_rejects(self):
  self._add_b3_fixture()
  with sqlite3.connect(self.t.path) as db:
   db.execute('DROP TRIGGER b3_add_claims_no_update');db.execute('UPDATE b3_add_claims SET initial_intent_id=?',('f'*32,))
  self.assertFalse(self.result()['by_symbol'])
 def test_missing_initial_add_and_sell_decisions_never_attribute_s5(self):
  for side in ('buy','sell'):
   with self.subTest(side=side):
    with sqlite3.connect(self.t.path) as db:
     row=db.execute("SELECT * FROM order_decisions WHERE intent_id IN (SELECT id FROM intents WHERE side=?) LIMIT 1",(side,)).fetchone()
     db.execute('DROP TRIGGER IF EXISTS order_decisions_no_delete')
     db.execute('DELETE FROM order_decisions WHERE scope=? AND intent_id=?',row[:2])
    self.assertFalse(self.result()['by_symbol'])
    with sqlite3.connect(self.t.path) as db:db.execute('INSERT INTO order_decisions VALUES (?,?,?,?,?,?,?)',row)
  with sqlite3.connect(self.t.path) as db:db.execute("CREATE TRIGGER order_decisions_no_delete BEFORE DELETE ON order_decisions BEGIN SELECT RAISE(ABORT, 'immutable'); END")
  self._add_b3_fixture()
  with sqlite3.connect(self.t.path) as db:
   db.execute('DROP TRIGGER order_decisions_no_delete');db.execute('DELETE FROM order_decisions WHERE intent_id=?',('e'*32,))
  self.assertFalse(self.result()['by_symbol'])
 def test_s5_decision_corruption_and_rehashed_wrong_identity_rejected(self):
  with sqlite3.connect(self.t.path) as db:
   db.row_factory=sqlite3.Row;row=dict(db.execute("SELECT * FROM order_decisions WHERE intent_id IN (SELECT id FROM intents WHERE side='sell')").fetchone())
   db.execute('DROP TRIGGER order_decisions_no_update')
   db.execute('UPDATE order_decisions SET evidence_sha256=? WHERE intent_id=?',('f'*64,row['intent_id']))
  self.assertFalse(self.result()['by_symbol'])
  for field,value in (('strategy_version','paper-multi-observed-v3'),('strategy','S3_upper_limit_continuation')):
   doc=json.loads(row['evidence_json']);doc[field]=value;raw=json.dumps(doc,sort_keys=True,separators=(',',':'))
   with sqlite3.connect(self.t.path) as db:db.execute('UPDATE order_decisions SET evidence_json=?,evidence_sha256=? WHERE intent_id=?',(raw,hashlib.sha256(raw.encode()).hexdigest(),row['intent_id']))
   self.assertFalse(self.result()['by_symbol'])
 def _unknown_prior_sale(self,strategy=S5,order=True):
  with sqlite3.connect(self.t.path) as db:
   if order:db.execute('INSERT INTO orders VALUES (?,?,?,?,?,?,?,?,?)',('default','20261102','paper','12','003220','sell',strategy,1,'2026-11-02T09:30:01+09:00'))
   db.execute('INSERT INTO intents VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',('default','a'*32,'20261102','paper','sell','003220',1,strategy,'2026-11-02T09:30:00+09:00','12','accepted',None))
 def test_prior_s5_sale_before_new_zero_epoch_prevents_partial_daily_total(self):
  self._unknown_prior_sale();r=self.result();self.assertFalse(r['by_symbol'])
  self.assertEqual(r['diagnostics']['003220'],'day_sale_order_coverage_incomplete')
  self.assertEqual(r['coverage']['003220'],{'requested_sale_orders':2,'assessed_sale_orders':1,'all_symbol_sales_reconciled':False})
 def test_mixed_strategy_sales_are_not_a_whole_symbol_s5_profit(self):
  self._unknown_prior_sale('gdcrs');r=self.result();self.assertFalse(r['by_symbol'])
  self.assertEqual(r['diagnostics']['003220'],'mixed_strategy_sales_symbol_total_unverified')
 def test_accepted_sale_without_registered_order_is_not_dropped(self):
  self._unknown_prior_sale(order=False);r=self.result();self.assertFalse(r['by_symbol'])
  self.assertEqual(r['diagnostics']['003220'],'day_sale_order_coverage_incomplete')
 def test_confirmed_cancellation_scope_is_explicitly_unevaluated(self):
  from unittest.mock import patch
  with patch('kiwoom_autotrader.trading.cancellation_settlement.confirmed_cancelled_quantity',return_value=1):r=self.result()
  self.assertFalse(r['by_symbol']);self.assertEqual(r['diagnostics']['003220'],'confirmed_cancellation_realized_scope_unsupported')
 def test_pure_rejects_duplicate_or_naive_units(self):
  u={'order_number':'1','execution_id':'1','side':'buy','price':'1000','quantity':1,'executed_at':'2026-11-02T10:00:00+09:00','observed_at':'2026-11-02T10:00:01+09:00'}
  g={'reader_validated':True,'mode':'paper','strategy':S5,'scope':'default','symbol':'003220','generation_id':'g','evidence_refs':['00:fixture'],'units':[u,dict(u)]}
  with self.assertRaises(ValueError):fifo_realized_evidence(g,'20261102')
  g['units']=[dict(u,observed_at='2026-11-02T10:00:01')]
  with self.assertRaises(ValueError):fifo_realized_evidence(g,'20261102')

if __name__=='__main__':unittest.main()
