"""Owned-lot and overnight routing; no broker or operational resources."""
from datetime import timedelta
from unittest import TestCase
from unittest.mock import Mock

class ExperimentOwnedEdgeTests(TestCase):
    def setUp(self):
        from tests.test_paper_experiment_monitor import MonitorTests, assignment, START
        self.assignment=assignment;self.start=START
        self.f=MonitorTests('test_real_mode_does_not_load_settings_provider_store_or_submit');self.f.setUp()
        self.addCleanup(self.f.doCleanups)
    def test_original_overnight_owner_outside_pool_gets_protective_sell_without_reassignment(self):
        f=self.f;value=f.seed(self.assignment('EXP_A'));f.owned(value)
        f.settings['gdcrs_stocks']=[];f.clock=self.start+timedelta(days=1,seconds=2)
        f.monitor.tick(f.session,f.notify)
        self.assertEqual(f.orders,[('sell',value['symbol'],3,None)])
        self.assertIsNone(f.monitor.store.assignment('20261103',value['symbol']))
        self.assertEqual(f.monitor.diagnostics[value['symbol']]['reason'],'unexpected_overnight_protective_exit')
    def test_current_owner_has_priority_over_d_override_and_unclosed_alert(self):
        from kiwoom_autotrader.trading.paper_experiment_rules import select_overnight
        f=self.f;value=f.seed(self.assignment('EXP_A'));f.owned(value)
        selected=select_overnight([{'symbol':value['symbol'],'first_seen_at':value['first_seen_at']}],day='20261102',
                                 snapshot_at=self.start.replace(hour=15,minute=18))
        f.monitor.store.selection('20261102','D',selected);f.clock=self.start.replace(hour=15,minute=20)
        f.monitor._one=Mock();f.monitor.tick(f.session,f.notify)
        self.assertEqual(f.monitor._one.call_args.args[-1]['arm'],'EXP_A')
        self.assertTrue(any('intraday_position_not_closed_by_cutoff' in str(x.args) for x in f.notify.call_args_list))
    def test_d_selection_accepts_only_proven_flat_and_excludes_owned_or_uncertain(self):
        f=self.f;owner=f.seed(self.assignment('EXP_A'));f.owned(owner)
        codes=[owner['symbol'],'000123','000124','000125']
        f.settings['gdcrs_stocks']=[{'code':s,'budget':10000} for s in codes]
        f.tracker.positions[('000123','EXP_D')]={'status':'pending'}
        f.tracker.positions[('000124','EXP_D')]={'status':'flat_candidate'}
        f.clock=self.start.replace(hour=15,minute=18);f.monitor._one=Mock();f.monitor.tick(f.session,f.notify)
        self.assertEqual(f.monitor.store.selection('20261102','D')['selected_symbols'],['000125'])
    def test_owned_lot_without_verified_original_fill_never_guesses_owner(self):
        f=self.f;value=f.seed(self.assignment('EXP_A'));f.owned(value)
        f.tracker.positions[(value['symbol'],'EXP_A')]['first_fill_timing_verified']=False
        f.clock=self.start.replace(hour=15,minute=15);f.monitor._one=Mock();f.monitor.tick(f.session,f.notify)
        f.monitor._one.assert_not_called();self.assertEqual(f.monitor.diagnostics[value['symbol']]['status'],'unsupported')

    def test_old_owned_generation_is_recovered_even_after_100_later_assignments(self):
        from kiwoom_autotrader.trading.paper_experiment_rules import assign_candidate
        f=self.f;value=f.seed(self.assignment('EXP_A'));f.owned(value)
        for offset in range(1,102):
            day=self.start+timedelta(days=offset)
            f.monitor.store.assign(assign_candidate(day.strftime('%Y%m%d'),value['symbol'],day,profile='native-five-v1'))
        f.clock=self.start+timedelta(days=102)
        recovered,reason=f.monitor._owned_assignment(f.tracker,value['symbol'],f.clock)
        self.assertIsNone(reason)
        self.assertEqual(recovered['assignment_sha256'],value['assignment_sha256'])

    def test_first_seen_is_pool_admission_not_discovery_outside_cap(self):
        f=self.f;base=[f'{i:06d}' for i in range(25)];dynamic=[f'{i:06d}' for i in range(100,130)]
        f.provider.discover_dynamic_candidates.return_value={'candidates':[{'symbol':s} for s in dynamic]}
        f.monitor.provider=f.provider
        pool=f.monitor._candidate_pool(base,f.clock)
        self.assertEqual(len(pool),40)
        self.assertIsNotNone(f.monitor.store.selection('20261102','first_seen:'+dynamic[14]))
        self.assertIsNone(f.monitor.store.selection('20261102','first_seen:'+dynamic[15]))
        self.assertIsNotNone(f.monitor.store.selection('20261102','discovered:'+dynamic[15]))

    def test_overnight_d_after_first_minute_recovers_once_without_reassignment(self):
        from kiwoom_autotrader.trading.paper_experiment_rules import select_overnight
        f=self.f;symbol='000321';selected=select_overnight([{'symbol':symbol,'first_seen_at':self.start.isoformat()}],
          day='20261102',snapshot_at=self.start.replace(hour=15,minute=18))
        f.monitor.store.selection('20261102','D',selected)
        value=selected['assignments'][0];f.owned(value,entry_at=self.start.replace(hour=15,minute=25))
        f.clock=self.start+timedelta(days=1,minutes=2);f.monitor.tick(f.session,f.notify)
        self.assertEqual(f.orders,[('sell',symbol,3,None)])
        self.assertEqual(f.monitor.diagnostics[symbol]['reason'],'late_recovery_exit')
        self.assertIsNone(f.monitor.store.assignment('20261103',symbol))
        f.clock+=timedelta(minutes=1);f.monitor.tick(f.session,f.notify)
        self.assertEqual(f.orders,[('sell',symbol,3,None)])

    def test_overnight_d_first_opening_minute_submits_only_proven_owner(self):
        from kiwoom_autotrader.trading.paper_experiment_rules import select_overnight
        f=self.f;symbol='000322';selected=select_overnight([{'symbol':symbol,'first_seen_at':self.start.isoformat()}],
          day='20261102',snapshot_at=self.start.replace(hour=15,minute=18))
        f.monitor.store.selection('20261102','D',selected);f.owned(selected['assignments'][0],entry_at=self.start.replace(hour=15,minute=25))
        f.clock=self.start+timedelta(days=1,seconds=15);f.monitor.tick(f.session,f.notify)
        self.assertEqual(f.orders,[('sell',symbol,3,None)])
