"""Synthetic EXP_D late-exit regressions; never load an operating account.

Orchestration cases use the existing monitor-only fake tracker. Integration
cases build the real tracker/store schema in a private temporary directory,
seed explicitly synthetic historical accepted buys plus unique00 events, and
run actual reconciliation/policy/ledger dispatch through a fake queued opener.
No accepted receipt is represented as a broker fill.
"""
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta
from decimal import Decimal
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

from tests import test_paper_experiment_monitor as base
from tests.test_orders import fake_session
from kiwoom_autotrader.api.executions import Execution
from kiwoom_autotrader.api.orders import OrderReceipt
from kiwoom_autotrader.trading import paper_experiment_monitor as monitor_module
from kiwoom_autotrader.trading import paper_experiment_rules as rules
from kiwoom_autotrader.trading.decision_evidence import canonical_json
from kiwoom_autotrader.trading.execution_log import ExecutionTracker
from kiwoom_autotrader.trading.paper_experiment_store import ExperimentStore
from kiwoom_autotrader.trading.paper_reconciliation import PaperReconciliationGate, PaperReconciliationError

KST=ZoneInfo('Asia/Seoul')
OPEN=datetime(2026,11,5,9,tzinfo=KST)
CODES=('293490','042940','456010','388050','068270','326030')


def calendar(at, *, start_hour=9):
    return {'start':at.replace(hour=start_hour,minute=0,second=0,microsecond=0),
        'end':at.replace(hour=15 if start_hour==9 else 16,minute=30,second=0,microsecond=0),
        'session_day':at.strftime('%Y%m%d'),'calendar_source_sha256':'c'*64}


def selection(code, day='20261104'):
    at=datetime.strptime(day,'%Y%m%d').replace(hour=15,minute=19,tzinfo=KST)
    return rules.select_overnight([{'symbol':code,'first_seen_at':at.replace(hour=9,minute=0).isoformat()}],
        day=day,snapshot_at=at)


class LateExitOrchestrationTests(unittest.TestCase):
    def setUp(self):
        self.f=base.MonitorTests('test_real_mode_does_not_load_settings_provider_store_or_submit')
        self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.f.clock=OPEN+timedelta(minutes=2)
        self.f.calendar.side_effect=calendar

    def own(self,code=CODES[0],day='20261104'):
        selected=selection(code,day);a=selected['assignments'][0]
        self.f.monitor.store.selection(day,'D',selected)
        self.f.monitor.store.assign(a)
        self.f.owned(a,entry_at=datetime.strptime(day,'%Y%m%d').replace(hour=15,minute=30,tzinfo=KST))
        return a

    def evidence(self):
        from kiwoom_autotrader.trading.decision_evidence import freeze_evidence
        return freeze_evidence(self.f.contexts[-1][1]['evidence']).as_dict()

    def test_085915_blocking_preparation_advances109_seconds_then_exits_before_candidates(self):
        f=self.f;self.own();f.clock=OPEN-timedelta(seconds=45);events=[]
        def warm(_):
            events.append('preparation_started');f.clock+=timedelta(seconds=109)
            return True
        def candidate(*a,**kw):events.append('candidate');return []
        original=f.submit
        def submit(*args,**kwargs):
            result=original(*args,**kwargs);events.append('sell');return result
        f.monitor.submit=submit;f.monitor._candidate_pool=candidate
        f.monitor.inputs=SimpleNamespace(prepare_selection_one=warm,
            prepare_one=lambda _:events.append('candidate_input'),
            market=lambda symbol,**kwargs:f.market(symbol,f.clock))
        f.monitor.identity=monitor_module.session_identity(f.session)
        f.monitor.tick(f.session,f.notify)
        self.assertEqual(f.clock,OPEN+timedelta(seconds=64))
        self.assertEqual(f.orders,[('sell',CODES[0],3,None)])
        self.assertLess(events.index('sell'),events.index('candidate') if 'candidate' in events else len(events))
        self.assertLess(events.index('sell'),events.index('candidate_input') if 'candidate_input' in events else len(events))
        config=self.evidence()['config'];self.assertEqual(config['experiment_exit_reason'],'late_recovery_exit')
        self.assertEqual(config['experiment_signal_at'],f.clock.isoformat())
        self.assertEqual(config['experiment_d_exit']['signal_at'],f.clock.isoformat())

    def test_six_owned_lots_exceed_batch4_without_starvation_or_intracycle_duplicate(self):
        f=self.f
        for code in CODES:self.own(code,'20261103' if code=='326030' else '20261104')
        events=[];original=f.submit
        def submit(*args,**kwargs):
            result=original(*args,**kwargs);events.append(('sell',args[2]));return result
        def candidates(*a,**kw):events.append(('candidate',None));return []
        f.monitor.submit=submit;f.monitor._candidate_pool=candidates
        f.monitor.tick(f.session,f.notify)
        self.assertEqual(len(f.orders),6,f.monitor.status())
        self.assertEqual({r[1] for r in f.orders},set(CODES))
        self.assertTrue(all(kind=='sell' for kind,_ in events[:6]))
        f.clock+=timedelta(seconds=2);f.monitor.tick(f.session,f.notify)
        self.assertEqual(len(f.orders),6)

    def test_original_assignment_and_generation_are_reused_after_monitor_restart(self):
        f=self.f;a=self.own();before=deepcopy(f.monitor.store.assignment(a['day'],a['symbol']))
        f.monitor.tick(f.session,f.notify)
        replacement=f.make_monitor();replacement.tick(f.session,f.notify)
        self.assertEqual(f.orders,[('sell',a['symbol'],3,None)])
        self.assertEqual(replacement.store.assignment(a['day'],a['symbol']),before)
        self.assertIsNone(replacement.store.assignment('20261105',a['symbol']))
        self.assertEqual(self.evidence()['config']['multi_position_generation_id'],'a'*32)

    def test_legacy_normal_open_accepted_uncertain_and_claim_only_are_not_replayed_late(self):
        f=self.f
        for index,status in enumerate(('accepted','uncertain',None,'not_submitted')):
            code=f'11111{index}';a=self.own(code)
            signal_at=(OPEN+timedelta(seconds=2)).isoformat()
            result={'signal':True,'action':'sell','reason':'next_session_open','signal_at':signal_at}
            key=monitor_module._digest([a['assignment_sha256'],'sell',signal_at,'next_session_open','a'*32])
            self.assertTrue(f.monitor.store.claim(key,day='20261105',symbol=code,arm='EXP_D',side='sell',now=OPEN+timedelta(seconds=2),payload=result))
            if status:f.monitor.store.outcome(key,OPEN+timedelta(seconds=3),{'status':status})
        f.monitor.tick(f.session,f.notify)
        self.assertEqual(f.orders,[])

    def test_no_calendar_preopen_afterclose_and_disabled_profile_never_send(self):
        f=self.f;self.own()
        for label,at,enabled,cal in (
            ('missing',OPEN+timedelta(minutes=2),True,None),
            ('holiday',OPEN+timedelta(days=2,minutes=2),True,None),
            ('unverified_source',OPEN+timedelta(minutes=2),True,{**calendar(OPEN),'calendar_source_sha256':None}),
            ('preopen',OPEN-timedelta(seconds=1),True,calendar(OPEN)),
            ('afterclose',OPEN.replace(hour=15,minute=30),True,calendar(OPEN)),
            ('disabled',OPEN+timedelta(minutes=2),False,calendar(OPEN))):
            with self.subTest(label=label):
                f.clock=at;f.settings['paper_experiment_enabled']=enabled
                f.calendar.side_effect=None;f.calendar.return_value=cal
                f.monitor.tick(f.session,f.notify);self.assertEqual(f.orders,[])
        f.session.mode='real';f.settings['paper_experiment_enabled']=True
        f.monitor.tick(f.session,f.notify);self.assertEqual(f.orders,[])

    def test_delayed_official_open_is_not_treated_as_0900_and_late_time_is_current(self):
        f=self.f;a=self.own();f.calendar.side_effect=lambda at:calendar(at,start_hour=10)
        f.clock=OPEN+timedelta(minutes=5);f.monitor.tick(f.session,f.notify)
        self.assertEqual(f.orders,[])
        f.clock=OPEN.replace(hour=10,minute=2,second=7,microsecond=1234)
        f.monitor.tick(f.session,f.notify)
        self.assertEqual(f.orders,[('sell',a['symbol'],3,None)])
        proof=self.evidence()['config']['experiment_d_exit']
        self.assertEqual(proof['regular_start'],OPEN.replace(hour=10).isoformat())
        self.assertEqual(proof['observed_session_open_at'],proof['regular_start'])
        self.assertEqual(proof['signal_at'],f.clock.isoformat())
        self.assertEqual(proof['kind'],'late_recovery_exit')

    def test_future_calendar_day_or_session_boundaries_never_create_exit_attempt(self):
        f=self.f;self.own()
        for mismatch in ('day','boundaries','both'):
            with self.subTest(mismatch=mismatch):
                value=calendar(f.clock)
                if mismatch in ('day','both'):value['session_day']='20261106'
                if mismatch in ('boundaries','both'):
                    value.update(start=value['start']+timedelta(days=1),end=value['end']+timedelta(days=1))
                f.calendar.side_effect=None;f.calendar.return_value=value
                f.monitor.tick(f.session,f.notify)
                self.assertEqual(f.orders,[])
                with f.monitor.store._db() as db:
                    self.assertEqual(db.execute("SELECT COUNT(*) FROM attempts WHERE arm='EXP_D' AND side='sell'").fetchone()[0],0)

    def test_current_session_buy_generation_is_not_an_overdue_exit(self):
        f=self.f;a=self.own(day='20261105');f.clock=OPEN.replace(hour=15,minute=29)
        f.tracker.positions[(a['symbol'],'EXP_D')]['first_fill_at']=OPEN.replace(hour=15,minute=25).isoformat()
        f.monitor.tick(f.session,f.notify)
        self.assertEqual(f.orders,[])
        with f.monitor.store._db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM attempts WHERE arm='EXP_D' AND side='sell'").fetchone()[0],0)

    def test_queue_crossing_close_blocks_the_send(self):
        f=self.f;self.own();f.clock=OPEN.replace(hour=15,minute=29,second=59)
        def blocked(*args,before_send,**kw):
            f.clock+=timedelta(seconds=2);before_send()
            raise AssertionError('queued late exit crossed close')
        f.monitor.submit=blocked;f.monitor.tick(f.session,f.notify)
        self.assertEqual(f.orders,[])
    def test_calendar_generation_change_while_queued_blocks_the_send(self):
        f=self.f;self.own()
        def changed(*args,before_send,**kw):
            f.calendar.side_effect=lambda at:{**calendar(at),'calendar_source_sha256':'d'*64}
            before_send();raise AssertionError('changed calendar submitted')
        f.monitor.submit=changed;f.monitor.tick(f.session,f.notify)
        self.assertEqual(f.orders,[])

    def test_preclaim_quote_delay_refreshes_normal_signal_to_actual_late_time(self):
        f=self.f;self.own();f.clock=OPEN+timedelta(seconds=59)
        def quote(symbol,at):
            f.clock+=timedelta(seconds=3)
            return f.market(symbol,f.clock)
        f.provider.fetch.side_effect=quote
        f.monitor.tick(f.session,f.notify)
        self.assertEqual(len(f.orders),1,f.monitor.status())
        config=self.evidence()['config']
        self.assertEqual(config['experiment_exit_reason'],'late_recovery_exit')
        self.assertEqual(config['experiment_signal_at'],f.clock.isoformat())
        self.assertEqual(config['experiment_d_exit']['signal_at'],f.clock.isoformat())

    def test_claimed_normal_exit_expiring_in_queue_stays_consumed_after_restart(self):
        f=self.f;a=self.own();f.clock=OPEN+timedelta(seconds=59)
        def expired(*args,before_send,**kwargs):
            f.clock+=timedelta(seconds=2);before_send()
            raise AssertionError('claimed normal-open signal crossed deadline')
        f.monitor.submit=expired;f.monitor.tick(f.session,f.notify)
        self.assertEqual(f.orders,[])
        replacement=f.make_monitor();f.clock=OPEN+timedelta(minutes=2)
        replacement.tick(f.session,f.notify)
        self.assertEqual(f.orders,[])
        with f.monitor.store._db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM attempts WHERE symbol=? AND arm='EXP_D' AND side='sell'",(a['symbol'],)).fetchone()[0],1)

    def test_s5_and_generic_owned_protection_also_precedes_candidate_work(self):
        f=self.f;f.settings['paper_experiment_s5_enabled']=True
        f.clock=base.START+timedelta(days=1,minutes=2)
        self.own(CODES[0],'20261102')
        a=f.seed(base.assignment('EXP_A'));f.owned(a)
        s5=f.seed(base.assignment('S5_steady_uptrend_vwap',(a['symbol'],CODES[0])));f.owned(s5)
        f.tracker.positions[(s5['symbol'],s5['arm'])]['first_fill_observed_at']=(base.START+timedelta(seconds=1)).isoformat()
        def candidates(*args,**kwargs):
            self.assertEqual({row[1] for row in f.orders},{CODES[0],a['symbol'],s5['symbol']})
            return []
        f.monitor._candidate_pool=candidates
        f.monitor.tick(f.session,f.notify)
        self.assertEqual(len(f.orders),3)
        self.assertEqual(len({row[1] for row in f.orders}),3)

    def test_unsupported_or_pending_owner_never_enters_late_exit(self):
        f=self.f
        for index,status in enumerate(('unsupported','pending','flat_candidate')):
            a=self.own(f'22222{index}');f.tracker.positions[(a['symbol'],'EXP_D')]['status']=status
        f.monitor.tick(f.session,f.notify);self.assertEqual(f.orders,[])


class RealLateExitPipelineTests(unittest.TestCase):
    """Actual ledger/store/reconciler/policy with explicitly synthetic history."""
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name).resolve();self.now=OPEN+timedelta(minutes=2)
        self.settings={'paper_experiment_enabled':True,'gdcrs_stocks':[],'paper_strategy_stocks':[]}
        self.tracker=ExecutionTracker(log_dir=self.root/'ledger',now=lambda:self.now,settings_loader=lambda:self.settings)
        self.store=ExperimentStore(self.root/'assignments.sqlite3');self.a=selection(CODES[0])['assignments'][0]
        self.store.assign(self.a);self.quantity=3;self.price=1000;self.generation='1'*32
        self.buy_number='101';self.outputs=[];self.pending_rows=[];self.pending_complete=True
        self.pending_scope='0';self.sellable=self.quantity;self.report_quantity=self.quantity;self.pending_calls=0
        self.recorder_state={'ready':True,'error':None,'gap_count':0,'observations':0,
            'ingress_sequence':0,'inflight_callbacks':0,'engine_ingress_sequence':0,'engine_pending_callbacks':0,'coverage_complete':True}
        self.session=fake_session(clock=lambda:self.now)
        self.session.execution_recorder=SimpleNamespace(status=lambda:dict(self.recorder_state))
        self.session.execution_recording_guard=lambda:None
        self._seed_history()
        self.gate=PaperReconciliationGate(self.session,self.tracker,executions_fetcher=lambda *a,**kw:(),
            account_fetcher=self.account,now=lambda:self.now,monotonic=lambda:1000.0)
        self.gate._execution_source=self.gate._holdings_source='broker_observation'
        self.gate._pending=self.pending
        self.session.paper_automation_reconcile=self.gate
        self.provider=SimpleNamespace(fetch=lambda symbol,at:{'symbol':symbol,'current_price':1000,'source_sha256':'a'*64},
            discover_dynamic_candidates=Mock(return_value={'candidates':[]}))
        self.monitor=self.make_monitor()
        self.addCleanup(patch.stopall)
        patch('socket.socket.connect',side_effect=AssertionError('network forbidden')).start()
        patch('kiwoom_autotrader.trading.paper_policy.load_settings',side_effect=lambda **kw:self.settings).start()
        patch('kiwoom_autotrader.trading.paper_reconciliation.load_settings',side_effect=lambda **kw:self.settings).start()
        patch('kiwoom_autotrader.api.orders.queued_open',side_effect=self.queue).start()

    def make_monitor(self):
        m=monitor_module.PaperExperimentMonitor(settings_loader=lambda:self.settings,now=lambda:self.now,
            store_path=self.store.path,provider_factory=lambda _:self.provider,
            chart_fetcher=Mock(side_effect=AssertionError('owned late exit requested entry candles')),
            calendar_reader=calendar,batch_size=4)
        m.store=self.store;return m

    def _seed_history(self):
        entry=OPEN-timedelta(days=1);entry=entry.replace(hour=15,minute=25)
        self.now=entry-timedelta(minutes=1)
        self.tracker.record_broker_observation({'schema_version':1,'source_api':'00','source_kind':'broker_observation',
            'observed_at':self.now.isoformat(),'day':'20261104','mode':'paper','event_kind':'stream_started','values':{}})
        proof={'schema_version':3,'symbol':CODES[0],'pre_order_held_qty':0,'pre_order_pur_amt_krw':0,
            'holdings_source':'broker_observation','observed_at':(entry-timedelta(seconds=1)).isoformat()}
        evidence={'schema_version':1,'strategy':'EXP_D','strategy_version':'paper-experiment-v1',
            'config':{'experiment_assignment_sha256':self.a['assignment_sha256']}}
        raw=canonical_json(proof);evraw=canonical_json(evidence)
        with self.tracker._db() as db:
            db.execute('INSERT INTO intents VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',('default',self.generation,'20261104','paper','buy',CODES[0],self.quantity,'EXP_D',entry.isoformat(),None,'prepared',None))
            db.execute('INSERT INTO pre_order_reconciliations VALUES(?,?,?,?,?)',('default',self.generation,entry.isoformat(),raw,hashlib.sha256(raw.encode()).hexdigest()))
            db.execute('INSERT INTO order_decisions VALUES(?,?,?,?,?,?,?)',('default',self.generation,entry.isoformat(),'1000','limit',evraw,hashlib.sha256(evraw.encode()).hexdigest()))
        self.now=entry+timedelta(seconds=1)
        self.tracker.register_order(OrderReceipt('paper','buy',CODES[0],str(self.quantity),'1000',self.buy_number,'https://example.invalid'),'EXP_D',intent_id=self.generation)
        self.now=entry.replace(minute=30,second=1)
        filled=getattr(self,'seed_fill_quantity',self.quantity)
        self.tracker.record_broker_observation({'schema_version':1,'source_api':'00','source_kind':'broker_observation',
            'observed_at':self.now.isoformat(),'day':'20261104','mode':'paper','event_kind':'execution_stream',
            'values':{'9203':self.buy_number,'9001':'A'+CODES[0],'907':'2','913':'체결','908':'153001','909':'201','914':'1000','915':str(filled),'911':str(filled),'903':str(filled*1000)}})
        self.tracker._ingest('paper','20261104',(Execution('20261104',self.buy_number,'201',CODES[0],'synthetic','buy','15:30:01',Decimal(1000),filled),),source_kind='broker_observation')
        self.now=OPEN+timedelta(minutes=2)
        self.tracker.record_broker_observation({'schema_version':1,'source_api':'00','source_kind':'broker_observation',
            'observed_at':(OPEN-timedelta(minutes=1)).isoformat(),'day':'20261105','mode':'paper','event_kind':'stream_started','values':{}})
        self.tracker._holdings('paper',self.account(),self.now,source_kind='broker_observation')

    def account(self,*args):
        return {'mode':'paper','exchange':'KRX','snapshot_changed':False,'holdings':[{
            'stk_cd':'A'+CODES[0],'crd_tp':'00','rmnd_qty':str(self.report_quantity),
            'trde_able_qty':str(self.sellable),'pur_amt':str(self.price*self.report_quantity)}],
            'cash':{'entr':'9000000','ord_alow_amt':'9000000'}}

    def pending(self,session,*,observer,day,integrated=False):
        self.assertTrue(integrated,'EXP_D late exit must use full integrated broker pending query')
        self.pending_calls+=1
        observer({'schema':'kiwoom-broker-page-v1','source_api':'ka10075','source_kind':'broker_observation',
            'mode':'paper','day':day,'query_id':f'{self.pending_calls:032x}','request_started_at':self.now.isoformat(),
            'observed_at':self.now.isoformat(),'page':1,'request_body':{'all_stk_tp':'0','trde_tp':'0','stex_tp':self.pending_scope},
            'response_body':{'return_code':0,'oso':self.pending_rows},
            'continuation':{'request_cont_yn':'N','response_cont_yn':'N' if self.pending_complete else 'Y','next_key_present':not self.pending_complete},
            'status':'success','http_status':200})
        return self.pending_rows

    @contextmanager
    def queue(self,opener,request,**kwargs):
        if hasattr(self,'before_queue'):self.before_queue()
        kwargs['before_send']();self.outputs.append(json.loads(request.data))
        class Response:
            def read(self,n):return b'{"return_code":0,"ord_no":"301"}'
        yield Response()

    def immutable_history(self):
        with self.tracker._db() as db:
            return {table:[tuple(row) for row in db.execute('SELECT * FROM '+table+' WHERE day=? ORDER BY 1,2',('20261104',))]
                for table in ('intents','orders','fills','broker_observations')}

    def test_actual_late_exit_preserves_original_generation_assignment_and_00_across_restart(self):
        original=self.immutable_history();assignment=deepcopy(self.store.assignment('20261104',CODES[0]))
        pos=self.tracker.multi_strategy_position('paper',CODES[0],'EXP_D',self.now)
        self.assertEqual((pos['status'],pos['generation_id'],pos['owned_qty']),('owned',self.generation,3),pos)
        self.monitor.tick(self.session,lambda _:None)
        self.assertEqual(len(self.outputs),1,self.monitor.status())
        self.assertEqual(self.outputs[0]['ord_qty'],'3')
        with self.tracker._db() as db:
            row=db.execute("SELECT i.*,d.evidence_json,r.evidence_json AS reconciliation FROM intents i JOIN order_decisions d ON d.intent_id=i.id JOIN pre_order_reconciliations r ON r.intent_id=i.id WHERE i.side='sell'").fetchone()
        self.assertEqual((row['state'],row['strategy'],row['order_number']),('accepted','EXP_D','301'))
        config=json.loads(row['evidence_json'])['config']
        self.assertEqual(config['multi_position_generation_id'],self.generation)
        self.assertEqual(config['experiment_assignment_sha256'],assignment['assignment_sha256'])
        self.assertEqual(config['experiment_assignment'],assignment)
        self.assertEqual(config['experiment_exit_reason'],'late_recovery_exit')
        self.assertEqual(config['experiment_signal_at'],self.now.isoformat())
        self.assertEqual(json.loads(row['reconciliation'])['pre_order_held_qty'],3)
        self.make_monitor().tick(self.session,lambda _:None)
        self.assertEqual(len(self.outputs),1)
        self.assertEqual(self.immutable_history(),original)
        self.assertEqual(self.store.assignment('20261104',CODES[0]),assignment)
        self.assertIsNone(self.store.assignment('20261105',CODES[0]))
        self.assertNotEqual(self.tracker.multi_strategy_position('paper',CODES[0],'EXP_D',self.now)['status'],'flat')
        # Only a distinct synthetic broker unique00 execution, not the receipt
        # or store outcome above, closes the temporary fixture ownership lot.
        self.now+=timedelta(seconds=1)
        self.tracker.record_broker_observation({'schema_version':1,'source_api':'00','source_kind':'broker_observation',
            'observed_at':self.now.isoformat(),'day':'20261105','mode':'paper','event_kind':'execution_stream',
            'values':{'9203':'301','9001':'A'+CODES[0],'907':'1','913':'체결','908':self.now.strftime('%H%M%S'),
                '909':'401','914':'1010','915':'3','911':'3','903':'3030'}})
        self.tracker._ingest('paper','20261105',(Execution('20261105','301','401',CODES[0],'synthetic','sell',
            self.now.strftime('%H:%M:%S'),Decimal(1010),3),),source_kind='broker_observation')
        self.assertEqual(self.tracker.multi_strategy_position('paper',CODES[0],'EXP_D',self.now)['status'],'flat')
        self.assertEqual(self.immutable_history(),original)

    def test_real_pipeline_rejects_partial_integrated_pending_and_current_qty_or_sellable_mismatch(self):
        # Each failed transmission consumes its generation cycle. Use the real
        # reconciliation gate directly to exercise every independent rejection.
        for mutation in ('partial','scope','pending','quantity','sellable'):
            with self.subTest(mutation=mutation):
                self.pending_complete=True;self.pending_scope='0';self.pending_rows=[]
                self.report_quantity=self.sellable=self.quantity
                if mutation=='partial':self.pending_complete=False
                elif mutation=='scope':self.pending_scope='1'
                elif mutation=='pending':self.pending_rows=[{'stk_cd':CODES[0],'ord_no':'88','oso_qty':'1','stex_tp':'1'}]
                elif mutation=='quantity':self.report_quantity=2;self.sellable=2
                elif mutation=='sellable':self.sellable=2
                with self.assertRaises(PaperReconciliationError):
                    self.gate(CODES[0],side='sell',quantity='3',strategy='EXP_D',exit_kind='full',generation_id=self.generation)
        self.assertEqual(self.outputs,[])

    def test_00_change_while_queued_prevents_real_order(self):
        def invalidate():self.recorder_state['ingress_sequence']+=1
        self.before_queue=invalidate
        self.monitor.tick(self.session,lambda _:None)
        self.assertEqual(self.outputs,[])
        with self.tracker._db() as db:
            self.assertFalse(db.execute("SELECT 1 FROM intents WHERE side='sell' AND state='accepted'").fetchone())

    def test_owner_change_while_queued_prevents_real_order(self):
        def invalidate():
            with self.tracker._db() as db:
                db.execute("UPDATE intents SET strategy='EXP_A' WHERE id=?",(self.generation,))
        self.before_queue=invalidate
        self.monitor.tick(self.session,lambda _:None)
        self.assertEqual(self.outputs,[])
        with self.tracker._db() as db:
            self.assertFalse(db.execute("SELECT 1 FROM intents WHERE side='sell' AND state='accepted'").fetchone())

    def test_partial_buy_fill_is_not_recovered_as_full_owned_exit(self):
        partial=RealLateExitPipelineTests('test_actual_late_exit_preserves_original_generation_assignment_and_00_across_restart')
        partial.seed_fill_quantity=2;partial.setUp();self.addCleanup(partial.doCleanups)
        state=partial.tracker.multi_strategy_position('paper',CODES[0],'EXP_D',partial.now)
        self.assertNotEqual(state['status'],'owned',state)
        partial.monitor.tick(partial.session,lambda _:None)
        self.assertEqual(partial.outputs,[])


if __name__=='__main__':unittest.main()
