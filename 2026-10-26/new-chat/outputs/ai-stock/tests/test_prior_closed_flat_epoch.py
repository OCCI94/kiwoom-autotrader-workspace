"""A closed historical cycle can start anew without rewriting gap/cost history."""
from datetime import datetime,timedelta
import hashlib,json,sqlite3,unittest
from kiwoom_autotrader.trading.execution_log import ExecutionLogError
from kiwoom_autotrader.trading.flat_epoch_reanchor import verified_prior_closed_flat
from tests import test_multi_strategy_epoch_review as epoch
S2=epoch.S2

class PriorClosedFlatEpochTests(unittest.TestCase):
 def setUp(self):
  self.f=epoch.EpochIndependentReview('test_complete_zero_broker_evidence_allows_new_generation_without_touching_old_history')
  self.f.setUp();self.addCleanup(self.f.doCleanups);self.tracker=self.f.tracker
  self.event('stream_started','2026-10-29T09:59:50+09:00',day='20261029')
  at=datetime.fromisoformat('2026-10-29T09:59:59+09:00')
  holding=self.tracker.record_strategy_holding_observation('005930',0,0,0,at,source_kind='broker_observation')
  with self.tracker._db() as db:anchor=db.execute("SELECT id,digest FROM broker_observations WHERE day='20261029' ORDER BY id DESC LIMIT 1").fetchone()
  self.tracker.record_broker_observation({'schema':'kiwoom-broker-page-v1','source_api':'ka10075','source_kind':'broker_observation',
    'mode':'paper','day':'20261029','query_id':'d'*32,'request_started_at':at.isoformat(),
    'observed_at':(at+timedelta(microseconds=100000)).isoformat(),'page':1,
    'request_body':{'all_stk_tp':'0','trde_tp':'0','stex_tp':'0','stk_cd':''},
    'response_body':{'return_code':0,'oso':[]},'continuation':{'request_cont_yn':'N','response_cont_yn':'N','next_key_present':False},
    'status':'success','http_status':200})
  p={'schema_version':5,'symbol':'005930','pre_order_held_qty':0,'pre_order_pur_amt_krw':0,
     'holdings_source':'broker_observation','observed_at':at.isoformat(),'holding_observation_id':holding,
     'reconciliation_completed_at':(at+timedelta(microseconds=200000)).isoformat(),'integrated_pending_clear':True,
     'pending_query_id':'d'*32,'recorder_gap_count':0,'recorder_observations':0,'recorder_ingress_sequence':0,'engine_ingress_sequence':0,
     'stream_anchor_id':anchor['id'],'stream_anchor_sha256':anchor['digest'],'stream_anchor_observed_at':'2026-10-29T09:59:50+09:00'}
  raw=json.dumps(p)
  with self.tracker._db() as db:
   db.execute('INSERT INTO pre_order_reconciliations VALUES (?,?,?,?,?)',('default','a'*32,p['observed_at'],raw,hashlib.sha256(raw.encode()).hexdigest()))
  for kind,at,values in [('stream_gap','10:00:01',{}),
       ('execution_stream','10:00:02',self.values('1','buy','10:00:02')),
       ('stream_resumed','10:00:03',{}),('execution_stream','10:01:02',self.values('2','sell','10:01:02'))]:
   self.event(kind,'2026-10-29T'+at+'+09:00',values,day='20261029')
  self.f.now+=timedelta(seconds=20)
  self.account();self.f.pending_page()
 def values(self,number,side,time):
  return {'9203':number,'9001':'A005930','907':'2' if side=='buy' else '1','913':'체결','908':time.replace(':',''),
          '909':number,'914':'1000','915':'10','911':'10','903':'10000'}
 def event(self,kind,at,values=None,day='20261030'):
  self.tracker.record_broker_observation({'schema_version':1,'source_api':'00','source_kind':'broker_observation','mode':'paper','day':day,
    'observed_at':at,'event_kind':kind,'values':values or {}})
 def account(self,holdings=None,age=4,pages=None):
  at=self.f.now-timedelta(seconds=age)
  value={'schema':'paper-account-snapshot-v1','mode':'paper','exchange':'KRX',
         'pages':{'kt00001':1,'kt00018':1} if pages is None else pages,'holdings':holdings or [],'cash':{'entr':'1000000','ord_alow_amt':'1000000'}}
  with self.tracker._db() as db:db.execute('INSERT OR REPLACE INTO account_balance_daily VALUES (?,?,?,?,?,?)',
    ('default','paper','20261030',at.isoformat(),'broker_observation',json.dumps(value)))
 def pos(self):return self.tracker.multi_strategy_position('paper','005930',S2,self.f.now)
 def test_complete_prior_quantities_current_broker_flat_allows_new_cycle_preserving_history(self):
  with self.tracker._db() as db:before=[tuple(r) for r in db.execute('SELECT * FROM broker_observations WHERE day=?',('20261029',))]
  p=self.pos();self.assertEqual(p['status'],'flat_candidate',p)
  evidence=p['flat_epoch_evidence'];self.assertFalse(evidence['repairs_prior_ownership']);self.assertTrue(evidence['preserved_historical_gaps'])
  proof=self.f.proof();ident=self.f.prepare(proof);self.tracker.assert_multi_buy_dispatch('paper','005930',ident)
  with self.tracker._db() as db:after=[tuple(r) for r in db.execute('SELECT * FROM broker_observations WHERE day=?',('20261029',))]
  self.assertEqual(before,after)
 def test_current_nonflat_stale_and_unproven_page_account_block(self):
  for kwargs in ({'holdings':[{'stk_cd':'A005930','rmnd_qty':'1'}]},{'age':91},{'pages':{}}):
   with self.subTest(kwargs=kwargs):self.account(**kwargs);self.assertEqual(self.pos()['status'],'unsupported')
 def test_missing_or_partial_integrated_pending_cannot_anchor(self):
  self.f.pending_page(complete=False);self.assertEqual(self.pos()['status'],'unsupported')
  self.f.pending_page(integrated=False);self.assertEqual(self.pos()['status'],'unsupported')
  self.f.pending_page(rows=[{'stk_cd':'000660','oso_qty':'1','ord_no':'88'}]);self.assertEqual(self.pos()['status'],'unsupported')
 def test_unresolved_intent_and_mismatched_rest_qty_cannot_be_discarded(self):
  with self.tracker._db() as db:db.execute("UPDATE fills SET quantity=9 WHERE side='sell'")
  self.assertNotEqual(self.pos()['status'],'flat_candidate')
 def test_historical_conflicting_unit_does_not_become_flat(self):
  bad=self.values('2','sell','10:01:02');bad['915']='9'
  self.event('execution_stream','2026-10-29T10:01:03+09:00',bad,day='20261029')
  self.assertEqual(self.pos()['status'],'unsupported')
 def test_late_current00_or_current_stream_stop_blocks_before_dispatch(self):
  ident=self.f.prepare(self.f.proof())
  self.event('execution_stream',self.f.now.isoformat(),{'9203':'99','9001':'A005930','913':'접수'})
  with self.assertRaises(ExecutionLogError):self.tracker.assert_multi_buy_dispatch('paper','005930',ident)
 def test_account_change_after_reserved_intent_blocks_final_send(self):
  ident=self.f.prepare(self.f.proof());self.account(holdings=[{'stk_cd':'A005930','rmnd_qty':'1'}])
  with self.assertRaises(ExecutionLogError):self.tracker.assert_multi_buy_dispatch('paper','005930',ident)
 def test_current_stream_gap_blocks_new_anchor_without_removing_old_events(self):
  self.event('stream_gap',self.f.now.isoformat());self.assertEqual(self.pos()['status'],'unsupported')
 def test_real_mode_never_uses_paper_flat_anchor(self):
  self.assertEqual(self.tracker.multi_strategy_position('real','005930',S2,self.f.now)['status'],'unsupported')

 def test_older_unobserved_history_is_preserved_but_latest_verified_zero_cycle_can_anchor(self):
  with self.tracker._db() as db:
   for ident,side,number,at in (('c'*32,'buy','81','2026-10-28T10:00:00+09:00'),('e'*32,'sell','82','2026-10-28T10:01:00+09:00')):
    db.execute('INSERT INTO intents VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',('default',ident,'20261028','paper',side,'005930',10,'gdcrs',at,number,'accepted',None))
    db.execute('INSERT INTO orders VALUES (?,?,?,?,?,?,?,?,?)',('default','20261028','paper',number,'005930',side,'gdcrs',10,at))
    db.execute('INSERT INTO fills VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',('default','20261028','paper',number,'1','005930','fixture',side,at[11:19],'1000',10,'gdcrs',None,None,None))
  # Allow the fixture's explicit legacy projection import to precede the new
  # current account snapshot, as actual prior-day rows do in production.
  with self.tracker._db():pass
  self.f.now+=timedelta(seconds=20);self.account();self.f.pending_page()
  p=self.pos();self.assertEqual(p['status'],'flat_candidate',p)
  self.assertEqual(p['flat_epoch_evidence']['preserved_earlier_order_count'],2)
  self.assertEqual(len(p['flat_epoch_evidence']['prior_closed_orders']),2)
  ident=self.f.prepare(self.f.proof());self.tracker.assert_multi_buy_dispatch('paper','005930',ident)

 def test_other_confirmed_cash_position_does_not_relock_target_flat_cycle(self):
  from kiwoom_autotrader.trading.execution_log import _paper_balance_snapshot
  raw={'mode':'paper','exchange':'KRX','snapshot_changed':False,'pages':{'kt00001':1,'kt00018':1},
       'cash':{'entr':'1000000','ord_alow_amt':'1000000'},'totals':{},
       'holdings':[{'stk_cd':'A000660','stk_nm':'타종목','crd_tp':'00','rmnd_qty':'3','trde_able_qty':'3','pur_amt':'30000'}]}
  normalized=_paper_balance_snapshot(raw);self.assertIsNotNone(normalized)
  self.account(holdings=normalized['holdings'])
  p=self.pos();self.assertEqual(p['status'],'flat_candidate',p);self.assertEqual(p['flat_epoch_evidence']['flat_scope'],'symbol')
  ident=self.f.prepare(self.f.proof());self.tracker.assert_multi_buy_dispatch('paper','005930',ident)
 def test_explicit_zero_target_and_other_cash_holding_are_valid(self):
  self.account(holdings=[{'stk_cd':'A005930','crd_tp':'00','rmnd_qty':'0','trde_able_qty':'0','pur_amt':'0'},
                         {'stk_cd':'000660','crd_tp':'00','rmnd_qty':'2','trde_able_qty':'1','pur_amt':'20000'}])
  self.assertEqual(self.pos()['status'],'flat_candidate')
 def test_ambiguous_other_holdings_cannot_prove_target_absence(self):
  normal={'stk_cd':'A000660','crd_tp':'00','rmnd_qty':'3','trde_able_qty':'3','pur_amt':'30000'}
  invalid=[{**normal,'stk_cd':'J000660'},{**normal,'stk_cd':'Q000660'},
           {**normal,'crd_tp':None},{**normal,'crd_tp':'03'},
           {**normal,'rmnd_qty':None},{**normal,'rmnd_qty':3},
           {**normal,'trde_able_qty':'4'},{**normal,'pur_amt':'0'},{**normal,'pur_amt':None},
           {**normal,'stk_cd':'A005930'}]
  for row in invalid:
   with self.subTest(row=row):self.account(holdings=[row]);self.assertEqual(self.pos()['status'],'unsupported')
  self.account(holdings=[normal,{**normal,'stk_cd':'000660'}]);self.assertEqual(self.pos()['status'],'unsupported')

 def test_only_experiment_profile_allows_verified_other_symbol_pending(self):
  row={'stk_cd':'A000660','ord_no':'78331','oso_qty':'6','stex_tp':'1'}
  self.f.pending_page(rows=[row]);self.assertEqual(self.pos()['status'],'unsupported')
  self.f.settings['paper_experiment_enabled']=True
  p=self.pos();self.assertEqual(p['status'],'flat_candidate',p)
  self.assertEqual(p['flat_epoch_evidence']['pending_clear_scope'],'symbol')
  for changed in ({**row,'stk_cd':'A005930'},{**row,'oso_qty':''},{**row,'ord_no':'0'},
                  {**row,'stk_cd':'UNKNOWN'},{**row,'stex_tp':'2'}):
   with self.subTest(row=changed):
    self.f.pending_page(rows=[changed]);self.assertEqual(self.pos()['status'],'unsupported')
  self.f.pending_page(rows=[row],complete=False);self.assertEqual(self.pos()['status'],'unsupported')
