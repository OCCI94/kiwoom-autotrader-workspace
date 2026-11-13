"""Paper-only GDR/LDR next-session exit tests with no actual broker."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import timedelta
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from kiwoom_autotrader.trading import paper_experiment_extended_rules as rules
from kiwoom_autotrader.trading import paper_experiment_monitor as monitor
from kiwoom_autotrader.trading.decision_evidence import DecisionEvidenceError, freeze_evidence
from kiwoom_autotrader.trading.paper_experiment_store import ExperimentStore
from tests import test_paper_experiment_monitor as fixture
from tests.test_paper_experiment_extended_rules import DAY, START, SYMBOL, calendar, context, position, selected


ARMS = ('EXP_GDR', 'EXP_LDR')
ENTRY = START.replace(hour=15, minute=25)
LATE = START.replace(day=3, minute=0, second=2)
GENERATION = 'a' * 32


def session(at, *, open_hour=9, close_hour=15):
    return {'start': at.replace(hour=open_hour, minute=0, second=0, microsecond=0),
            'end': at.replace(hour=close_hour, minute=30, second=0, microsecond=0),
            'session_day': at.strftime('%Y%m%d'), 'calendar_source_sha256': 'c' * 64}


class RuleTests(unittest.TestCase):
    def evaluate(self, arm, at, regular=None, *, first=ENTRY, history=None):
        a = selected(arm)[0]
        return rules.evaluate_exit(arm, [], symbol=SYMBOL, now=at, position=position(a, first),
            selection=a, official_session=regular, context=history)

    def test_exact_regular_open_and_overdue_day_do_not_need_entry_data(self):
        for arm in ARMS:
            for at in (LATE.replace(second=0), LATE.replace(day=5, hour=10, minute=37)):
                with self.subTest(arm=arm, at=at):
                    result = self.evaluate(arm, at, session(at))
                    self.assertTrue(result['signal'])
                    self.assertEqual((result['reason'], result['action']), ('late_recovery_exit','sell'))
                    self.assertEqual(result['signal_at'], at.isoformat())
                    self.assertEqual(result['order_window_end'], session(at)['end'].isoformat())
                    self.assertFalse(result['permission_to_trade'])

    def test_delayed_open_and_actual_close_are_half_open_boundaries(self):
        for arm in ARMS:
            for hour, minute, okay in ((9,59,False),(10,0,True),(15,35,True),(16,30,False)):
                at = LATE.replace(hour=hour,minute=minute,second=0)
                with self.subTest(arm=arm, at=at):
                    self.assertEqual(self.evaluate(arm,at,session(at,open_hour=10,close_hour=16))['signal'],okay)

    def test_missing_mismatched_calendar_and_same_day_position_do_not_recover(self):
        for arm in ARMS:
            for supplied in (None, {**session(LATE),'session_day':DAY},
                             {**session(LATE),'calendar_source_sha256':None}):
                self.assertFalse(self.evaluate(arm,LATE,supplied)['signal'])
            same = ENTRY.replace(hour=15,minute=26)
            self.assertFalse(self.evaluate(arm,same,session(same),history={'calendar':calendar()})['signal'])

    def test_exact_next_session_0859_auction_still_available(self):
        for arm in ARMS:
            at = LATE.replace(hour=8,minute=59,second=0)
            result = self.evaluate(arm,at,history={'calendar':calendar()})
            self.assertEqual(result['reason'],'next_session_opening_auction')
            self.assertEqual(result['order_window_end'],session(at)['start'].isoformat())
            result = self.evaluate(arm,at.replace(day=5),history={'calendar':calendar()})
            self.assertFalse(result['signal'])


class DispatchTests(unittest.TestCase):
    def setUp(self):
        self.f = fixture.MonitorTests('test_real_mode_does_not_load_settings_provider_store_or_submit')
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.f.clock = LATE
        self.quote = Mock(return_value={'stk_cd':SYMBOL,'cur_prc':'+1030'})
        patch.object(monitor,'fetch_stock_info',self.quote).start()

    def own(self, arm='EXP_GDR'):
        a = self.f.seed(selected(arm)[0])
        self.f.owned(a,entry_at=ENTRY)
        return a

    def test_monitor_tick_sells_original_gdr_before_discovery_outside_entry_pool(self):
        f=self.f;a=self.own()
        f.settings['gdcrs_stocks']=[{'code':'000660','budget':10000}]
        def discover(*args,**kwargs):
            self.assertEqual(f.orders,[('sell',SYMBOL,3,None)])
            return {'candidates':[]}
        f.clock=LATE.replace(minute=2)
        # The priority-lane monitor uses a separate discovery provider;
        # run that worker deterministically so the order of operations is tested.
        discovery=SimpleNamespace(discover_dynamic_candidates=Mock(side_effect=discover))
        f.monitor.discovery_provider_factory=lambda _:discovery
        f.monitor.discovery_launcher=lambda work:work()
        f.monitor.discovery_timer_factory=None
        f.monitor.tick(f.session,f.notify)
        discovery.discover_dynamic_candidates.assert_called_once()
        self.assertEqual(f.orders,[('sell',SYMBOL,3,None)])
        self.assertIsNone(f.monitor.store.assignment('20261103',SYMBOL))
        self.assertEqual(f.monitor.store.assignment(DAY,SYMBOL),a)
        self.assertEqual(f.monitor.diagnostics[SYMBOL]['reason'],'late_recovery_exit')
        frozen=freeze_evidence(f.contexts[0][1]['evidence']).as_dict()
        self.assertEqual(frozen['config']['experiment_assignment'],a)
        self.assertEqual(frozen['config']['experiment_extended_late_exit']['generation_id'],GENERATION)

    def test_monitor_tick_recovers_ldr_after_days_without_chart_or_vi(self):
        f=self.f;self.own('EXP_LDR');f.clock=LATE.replace(day=5,hour=15,minute=26)
        f.chart.side_effect=AssertionError('held LDR fetched chart')
        f.provider.fetch.side_effect=AssertionError('held LDR fetched entry context')
        f.monitor.tick(f.session,f.notify)
        self.assertEqual(f.orders,[('sell',SYMBOL,3,None)])
        f.chart.assert_not_called()
        f.provider.fetch.assert_not_called()

    def test_auction_owner_dispatches_before_candidate_selection(self):
        f=self.f;self.own('EXP_LDR');f.clock=LATE.replace(hour=8,minute=59)
        ctx=context(f.clock);ctx['calendar']=calendar()
        f.monitor.identity=('paper','fixture')
        f.monitor.inputs=SimpleNamespace(market=Mock(return_value=ctx['market']),prepare_one=Mock())
        def select(*args,**kwargs):
            self.assertEqual(f.orders,[('sell',SYMBOL,3,None)])
        f.monitor.extended_scheduler=SimpleNamespace(context=Mock(return_value=ctx),
            run_selection=Mock(side_effect=select),overrides=Mock(return_value={}))
        f.monitor._preopen=Mock()
        f.monitor.tick(f.session,f.notify)
        f.monitor.extended_scheduler.run_selection.assert_called_once()
        self.assertEqual(f.orders,[('sell',SYMBOL,3,None)])
        self.quote.assert_not_called()

    def test_crossing_open_in_auction_lookup_does_not_consume_regular_pass(self):
        f=self.f;self.own('EXP_LDR');f.clock=LATE.replace(hour=8,minute=59)
        reader=f.monitor._owned_assignment
        def advance(*args):
            owned=reader(*args)
            f.clock=LATE
            return owned
        f.monitor._owned_assignment=advance
        f.monitor.tick(f.session,f.notify)
        self.assertEqual(f.orders,[('sell',SYMBOL,3,None)])
        self.assertEqual(f.monitor.diagnostics[SYMBOL]['reason'],'late_recovery_exit')

    def test_restart_and_old_opening_attempts_do_not_resend(self):
        f=self.f;a=self.own('EXP_LDR');opening=LATE.replace(hour=8,minute=59,second=0)
        key=monitor._digest([a['assignment_sha256'],'sell',opening.isoformat(),
                            'next_session_opening_auction',GENERATION])
        f.monitor.store.claim(key,day='20261103',symbol=SYMBOL,arm=a['arm'],side='sell',
            now=opening+timedelta(seconds=5),payload={'action':'sell','reason':'next_session_opening_auction',
                                                     'signal_at':opening.isoformat()})
        f.monitor.store.outcome(key,opening+timedelta(seconds=6),{'status':'not_submitted'})
        f.monitor=f.make_monitor()
        f.one(a)
        self.assertEqual(f.orders,[])
        self.assertEqual(f.monitor.diagnostics[SYMBOL]['dispatch_status'],'extended_overnight_exit_legacy_attempt_consumed')
        self.quote.assert_not_called()

    def test_actual_attempt_after_missed_auction_is_sticky_after_restart(self):
        f=self.f;a=self.own()
        f.one(a);f.clock+=timedelta(days=1)
        f.monitor=f.make_monitor()
        f.one(a)
        self.assertEqual(f.orders,[('sell',SYMBOL,3,None)])
        self.assertEqual(f.monitor.diagnostics[SYMBOL]['dispatch_status'],'extended_overnight_exit_attempt_consumed')
        self.quote.assert_called_once()

    def test_missing_fill_or_foreign_generation_blocks_before_quote(self):
        f=self.f;a=self.own('EXP_LDR')
        f.monitor._generation_matches.return_value=False
        f.one(a)
        self.assertEqual(f.monitor.diagnostics[SYMBOL]['dispatch_status'],'extended_late_exit_ownership_changed')
        f.monitor._generation_matches.return_value=True
        f.tracker.positions[(SYMBOL,a['arm'])]['first_fill_timing_verified']=False
        f.one(a)
        self.assertEqual(f.monitor.diagnostics[SYMBOL]['reason'],'first_fill_timing_unverified')
        self.quote.assert_not_called()
        self.assertEqual(f.orders,[])

    def test_queue_rechecks_close_calendar_and_generation(self):
        cases=(
            ('close', lambda f: setattr(f,'clock',session(f.clock)['end']), 'official_session_unavailable'),
            ('calendar',lambda f: setattr(f.calendar,'side_effect',lambda at:{**session(at),'calendar_source_sha256':'d'*64}),
             'extended_late_exit_session_changed'),
            ('generation',lambda f: setattr(f.monitor._generation_matches,'return_value',False),
             'extended_late_exit_ownership_changed'),
        )
        for name,change,expected in cases:
            with self.subTest(name=name):
                f=self.f;f.clock=LATE;f.calendar.side_effect=session
                f.monitor._generation_matches.return_value=True
                # Keep each scenario's claim in its own real temporary store.
                f.monitor.store=ExperimentStore(f.path.parent/(name+'.sqlite3'))
                a=self.own()
                def submit(*args, before_send, **kwargs):
                    change(f)
                    before_send()
                    self.fail('queued request accepted after evidence changed')
                f.monitor.submit=submit
                with self.assertRaisesRegex(ValueError,expected):
                    f.one(a)
        self.assertEqual(self.f.orders,[])

    def test_bad_quote_does_not_claim_and_is_not_turned_into_market_fill(self):
        f=self.f;a=self.own()
        self.quote.return_value={'stk_cd':'000660','cur_prc':'1030'}
        with self.assertRaisesRegex(ValueError,'extended_late_exit_quote_unverified'):
            f.one(a)
        with f.monitor.store._db() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM attempts').fetchone()[0],0)
        self.assertEqual(f.orders,[])

    def test_extended_official_session_after_usual_close_keeps_owned_path(self):
        f=self.f;a=self.own('EXP_LDR');f.clock=LATE.replace(hour=15,minute=35)
        f.calendar.side_effect=lambda at:session(at,open_hour=10,close_hour=16)
        f.one(a)
        self.assertEqual(f.orders,[('sell',SYMBOL,3,None)])
        evidence=freeze_evidence(f.contexts[0][1]['evidence']).as_dict()
        self.assertEqual(evidence['config']['experiment_order_window_end_at'],session(f.clock,close_hour=16)['end'].isoformat())

    def test_gdr_entry_day_preauction_attempt_blocks_late_resend(self):
        f=self.f;a=self.own()
        first=START.replace(hour=15,minute=27,second=0)
        reason='gdr_preauction_close_below_open_2pct_v1'
        key=monitor._digest([a['assignment_sha256'],'sell',first.isoformat(),reason,GENERATION])
        f.monitor.store.claim(key,day=DAY,symbol=SYMBOL,arm=a['arm'],side='sell',now=first,
            payload={'action':'sell','reason':reason,'signal_at':first.isoformat()})
        f.one(a)
        self.assertEqual(f.orders,[])
        self.quote.assert_not_called()

    def test_evidence_rejects_absent_or_tampered_original_fill_and_window(self):
        f=self.f;a=self.own();f.one(a)
        good=f.contexts[0][1]['evidence']
        self.assertIsNotNone(freeze_evidence(good))
        for mutate in (
            lambda e:e['config'].pop('experiment_extended_late_exit'),
            lambda e:e['config']['experiment_extended_late_exit'].update(generation_id='b'*32),
            lambda e:e['config']['experiment_extended_late_exit'].update(first_fill_at=LATE.isoformat()),
            lambda e:e['config']['experiment_extended_late_exit'].update(signal_at=ENTRY.isoformat()),
            lambda e:e['config']['experiment_extended_late_exit'].update(calendar_source_sha256='bad'),
            lambda e:e['config'].update(experiment_order_window_end_at=(LATE+timedelta(minutes=2)).isoformat()),
            lambda e:e.update(source_kind='synthetic'),
        ):
            bad=deepcopy(good);mutate(bad)
            with self.assertRaises(DecisionEvidenceError):
                freeze_evidence(bad)

    def test_atomic_generation_claim_and_missing_outcome(self):
        f=self.f;a=self.own()
        result=f.monitor._extended_late_exit_result(a,f.tracker.positions[(SYMBOL,a['arm'])],f.clock)
        other=ExperimentStore(f.path)
        def claim(store):
            return store.claim_extended_exit(a,GENERATION,now=f.clock,entry_at=ENTRY,payload=result)
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sorted(pool.map(claim,(f.monitor.store,other))),[False,True])
        self.assertEqual(other.extended_exit_status(a,GENERATION,now=f.clock,entry_at=ENTRY)['status'],'consumed')
        with other._db() as db:
            payload=json.loads(db.execute('SELECT payload FROM attempts').fetchone()[0])
        self.assertEqual(payload['extended_overnight_exit_cycle']['generation_id'],GENERATION)
