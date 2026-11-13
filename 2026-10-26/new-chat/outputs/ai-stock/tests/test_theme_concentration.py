"""Offline all-strategy theme admission, reservations, queue and real isolation."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
import json
from pathlib import Path
import tempfile
from unittest import TestCase
from unittest.mock import patch
from zoneinfo import ZoneInfo

from kiwoom_autotrader.api.orders import OrderReceipt
from kiwoom_autotrader.config.settings import default_settings, effective_theme_cap, SettingsError
from kiwoom_autotrader.trading.execution_log import (ExecutionTracker, ExecutionLogError,
    ThemeConcentrationLimitReached, ThemeConcentrationUnavailable, ConcurrentPositionLimitUnavailable,
    ConcurrentPositionLimitReached)
from kiwoom_autotrader.trading.golden_cross import GoldenOrderMarkers
from kiwoom_autotrader.trading.theme_concentration import funnel_theme_reason


class ThemeCapTests(TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.now = datetime(2026,10,30,11,0,tzinfo=ZoneInfo('Asia/Seoul'))
        self.options = {'max_positions_per_theme':2}
        self.taxonomy = self.root/'taxonomy.json'
        self.tax = {'schema':'pro6e-sector-taxonomy-v1','metadata_only':True,
            'created_at':(self.now-timedelta(days=1)).isoformat(),'kr_stocks':{
                '053800':{'sub_themes':['보안·사이버']},
                '042510':{'sub_themes':['보안·인증']},
                '434480':{'sub_themes':['보안·클라우드']},
                '158430':{'sub_themes':['보안·인증','스테이블·인프라']},
                '005930':{'sub_themes':['반도체']},
                '000660':{'sub_themes':['반도체·메모리']}}}
        self.taxonomy.write_text(json.dumps(self.tax))
        self.tracker = self.tracker_for()
        with self.tracker._db(): pass
        self.balance(['053800'])

    def tracker_for(self):
        return ExecutionTracker(log_dir=self.root,now=lambda:self.now,
            settings_loader=lambda:self.options,theme_taxonomy_path=self.taxonomy)

    def balance(self, symbols, age=0):
        data={'schema':'paper-account-snapshot-v1','mode':'paper','exchange':'KRX',
              'cash':{'entr':'10000000','ord_alow_amt':'10000000'},
              'holdings':[{'stk_cd':'A'+s,'rmnd_qty':'1'} for s in symbols]}
        with self.tracker._db() as db:
            db.execute('INSERT OR REPLACE INTO account_balance_daily VALUES(?,?,?,?,?,?)',
                (self.tracker.scope,'paper','20261030',(self.now-timedelta(seconds=age)).isoformat(),
                 'broker_observation',json.dumps(data)))

    def prepare(self, symbol, tracker=None, strategy='gdcrs'):
        return (tracker or self.tracker).prepare_order('paper','buy',symbol,1,strategy,price=1000)

    def test_normalized_default_and_real_mode(self):
        self.assertEqual(default_settings()['max_positions_per_theme'],2)
        self.assertEqual(effective_theme_cap(default_settings(),'paper'),2)
        self.assertEqual(effective_theme_cap(default_settings(),'real'),0)
        self.assertEqual(effective_theme_cap({},'paper'),0)
        for value in (True,-1,11,'2',None):
            with self.subTest(value=value),self.assertRaises(SettingsError):
                effective_theme_cap({'max_positions_per_theme':value},'paper')

    def test_different_security_subthemes_share_two_slots(self):
        self.balance(['053800','042510'])
        with self.assertRaises(ThemeConcentrationLimitReached): self.prepare('434480')
        self.assertTrue(self.prepare('005930'))

    def test_intent_counts_and_existing_symbol_add_is_not_new_slot(self):
        self.prepare('042510')
        with self.assertRaises(ThemeConcentrationLimitReached):self.prepare('158430')
        proof=self.tracker.paper_position_capacity('053800',self.now)
        self.assertEqual(set(proof['occupied_symbols']),{'053800','042510'})
        # Existing add remains subject to account pending/cash/principal gates.
        with self.assertRaises(ExecutionLogError): self.prepare('053800')

    def test_two_trackers_cannot_reserve_last_theme_slot(self):
        other=self.tracker_for()
        def reserve(args):
            tracker,symbol=args
            try:return self.prepare(symbol,tracker,strategy='gdcrs' if symbol=='042510' else 'legacy_other_strategy')
            except ExecutionLogError:return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(reserve,[(self.tracker,'042510'),(other,'434480')]))
        self.assertEqual(sum(x is not None for x in results),1)

    def test_accepted_unfilled_buy_from_other_strategy_still_occupies(self):
        # An older strategy label cannot escape the account-wide theme count.
        ident=self.prepare('042510',strategy='legacy_other_strategy')
        self.tracker.register_order(OrderReceipt('paper','buy','042510','1','1000','100',
            'https://mockapi.kiwoom.com'),'legacy_other_strategy',intent_id=ident)
        with self.assertRaises(ThemeConcentrationLimitReached):self.prepare('434480')
        # Merely changing/cancelling a local intent cannot erase an accepted
        # broker order's still-unconfirmed remaining quantity.
        with self.assertRaises(ExecutionLogError):self.tracker.cancel_intent(ident,'not_sent')
        with self.assertRaises(ThemeConcentrationLimitReached):self.prepare('434480')

    def test_cancel_ack_is_not_release_but_unsent_intent_is(self):
        ident=self.prepare('042510')
        with self.tracker._db() as db:
            db.execute("UPDATE intents SET state='uncertain' WHERE id=?",(ident,))
        with self.assertRaises(ThemeConcentrationLimitReached):self.prepare('434480')
        self.tracker.cancel_intent(ident,'not_sent')
        self.assertTrue(self.prepare('434480'))

    def test_multiple_family_memberships_all_count(self):
        self.tax['kr_stocks']['005930']={'sub_themes':['스테이블·인프라']}
        self.tax['kr_stocks']['000660']={'sub_themes':['스테이블·거래소']}
        self.taxonomy.write_text(json.dumps(self.tax)); self.balance(['005930','000660'])
        with self.assertRaises(ThemeConcentrationLimitReached):self.prepare('158430')

    def test_unknown_candidate_or_occupied_theme_fails_closed_but_exit_allowed(self):
        with self.assertRaises(ThemeConcentrationUnavailable):self.prepare('053350')
        self.balance(['053350'])
        with self.assertRaises(ThemeConcentrationUnavailable):self.prepare('005930')
        # The existing symbol consumes no new theme slot, even unknown metadata.
        self.assertEqual(self.tracker.paper_position_capacity('053350',self.now)['occupied_symbols'],['053350'])
        self.assertTrue(self.tracker.prepare_order('paper','sell','053350',1,'ddcrs',price=None))

    def test_future_taxonomy_stale_snapshot_and_later_fill_block(self):
        self.tax['created_at']=(self.now+timedelta(seconds=1)).isoformat()
        self.taxonomy.write_text(json.dumps(self.tax))
        with self.assertRaises(ThemeConcentrationUnavailable):self.prepare('005930')
        self.tax['created_at']=(self.now-timedelta(days=1)).isoformat();self.taxonomy.write_text(json.dumps(self.tax))
        self.balance([],age=91)
        with self.assertRaises(ConcurrentPositionLimitUnavailable):self.prepare('005930')
        self.balance([])
        with self.tracker._db() as db:
            db.execute('INSERT INTO execution_observations(scope,day,mode,ord_no,fill_no,observed_at,kind,source_kind,digest,payload_json) VALUES(?,?,?,?,?,?,?,?,?,?)',
                ('default','20261030','paper','1','1',(self.now+timedelta(seconds=1)).isoformat(),'unit','broker_observation','a'*64,'{}'))
        self.now+=timedelta(seconds=2)
        with self.assertRaises(ConcurrentPositionLimitUnavailable):self.prepare('005930')

    def test_dispatch_rechecks_theme_and_reserved_intent_without_double_count(self):
        ident=self.prepare('042510')
        self.tracker.assert_paper_theme_dispatch('paper','042510',ident)
        self.balance(['053800','434480'])
        with self.assertRaises(ThemeConcentrationLimitReached):
            self.tracker.assert_paper_theme_dispatch('paper','042510',ident)

    def test_real_and_explicit_disabled_never_read_taxonomy(self):
        self.taxonomy.unlink()
        self.assertTrue(self.tracker.prepare_order('real','buy','042510',1,'gdcrs',price=1000))
        self.options['max_positions_per_theme']=0
        self.assertTrue(self.prepare('042510'))

    def test_golden_marker_accepts_exact_theme_reason(self):
        marker=GoldenOrderMarkers(self.root/'markers.sqlite3')
        marker.record_decision('paper','042510',self.now,self.now,'skipped','theme_concentration_limit_reached')
        import sqlite3
        with sqlite3.connect(marker.path) as db:
            self.assertEqual(db.execute('SELECT skip_reason FROM entry_decisions').fetchone()[0],
                             'theme_concentration_limit_reached')

    def test_unknown_shared_bucket_is_combined_across_symbols_not_individual(self):
        self.options['paper_theme_unclassified_policy'] = 'shared_bucket'
        self.balance(['003220'])
        proof = self.tracker.paper_position_capacity('003610', self.now)
        self.assertEqual(proof['theme_evidence']['occupied_by_theme'],
                         {'unclassified_pool': ['003220']})
        self.assertTrue(self.prepare('003610'))
        with self.assertRaises(ThemeConcentrationLimitReached):
            self.prepare('053350', strategy='legacy_other_strategy')
        self.options['max_positions_per_theme'] = 8
        with self.assertRaises(ThemeConcentrationLimitReached):
            self.tracker.paper_position_capacity('053350', self.now)
        self.assertIsNotNone(self.tracker.paper_position_capacity('005930', self.now))

    def test_unknown_accepted_order_keeps_slot_after_ack_and_own_dispatch_works(self):
        self.options['paper_theme_unclassified_policy'] = 'shared_bucket'
        self.balance(['003220'])
        ident = self.prepare('003610')
        self.tracker.assert_paper_theme_dispatch('paper', '003610', ident)
        self.tracker.register_order(OrderReceipt('paper','buy','003610','1','1000','100',
            'https://mockapi.kiwoom.com'), 'gdcrs', intent_id=ident)
        with self.assertRaises(ThemeConcentrationLimitReached):
            self.tracker.paper_position_capacity('053350', self.now)

    def test_unknown_shared_fallback_does_not_hide_malformed_or_future_taxonomy(self):
        self.options['paper_theme_unclassified_policy'] = 'shared_bucket'
        for row in (None, {'sub_themes': None}, {'sub_themes': ['']},
                    {'sub_themes': ['unclassified_pool']}, {'sub_themes': ['·bad']}):
            self.tax['kr_stocks']['003220'] = row
            self.taxonomy.write_text(json.dumps(self.tax))
            with self.subTest(row=row), self.assertRaises(ThemeConcentrationUnavailable):
                self.prepare('003220')
        self.tax['kr_stocks']['003220'] = {'sub_themes': []}
        self.taxonomy.write_text(json.dumps(self.tax))
        self.assertIsNotNone(self.tracker.paper_position_capacity('003220', self.now))
        self.tax['created_at'] = (self.now+timedelta(seconds=1)).isoformat()
        self.taxonomy.write_text(json.dumps(self.tax))
        with self.assertRaises(ThemeConcentrationUnavailable):
            self.tracker.paper_theme_capacity_snapshot(self.now)

    def test_radar_snapshot_reads_once_and_keeps_exact_final_gate_semantics(self):
        self.options['paper_theme_unclassified_policy'] = 'shared_bucket'
        self.balance(['003220', '003610', '053800', '042510'])
        import kiwoom_autotrader.trading.theme_concentration as module
        with patch.object(module, 'read_taxonomy', wraps=module.read_taxonomy) as read:
            snapshot = self.tracker.paper_theme_capacity_snapshot(self.now)
            for _ in range(1000):
                self.assertEqual(funnel_theme_reason('053350', snapshot),
                                 'theme_concentration_limit_reached')
                self.assertEqual(funnel_theme_reason('434480', snapshot),
                                 'theme_concentration_limit_reached')
                self.assertIsNone(funnel_theme_reason('005930', snapshot))
                self.assertIsNone(funnel_theme_reason('003220', snapshot))
            self.assertEqual(read.call_count, 1)
        self.assertEqual(snapshot['mode'], 'paper')
        self.assertEqual(snapshot['observed_at'], self.now.isoformat())
        self.balance([], age=91)
        with self.assertRaises(ConcurrentPositionLimitUnavailable):
            self.tracker.paper_theme_capacity_snapshot(self.now)

    def test_snapshot_unknown_block_and_invalid_candidate_stay_unavailable(self):
        snapshot = self.tracker.paper_theme_capacity_snapshot(self.now)
        self.assertEqual(funnel_theme_reason('053350', snapshot), 'theme_membership_unavailable')
        self.options['paper_theme_unclassified_policy'] = 'shared_bucket'
        self.tax['kr_stocks']['053350'] = {'sub_themes': None}
        self.taxonomy.write_text(json.dumps(self.tax))
        snapshot = self.tracker.paper_theme_capacity_snapshot(self.now)
        self.assertEqual(funnel_theme_reason('053350', snapshot), 'theme_membership_unavailable')
        self.assertIsNone(funnel_theme_reason('003220', snapshot))

    def test_gdcrs_queued_dispatch_rechecks_actual_position_limit_even_theme_disabled(self):
        for theme_limit in (0, 2):
            with self.subTest(theme_limit=theme_limit):
                self.options.update(max_positions_per_theme=theme_limit,
                    paper_dynamic_scan_enabled=True, paper_max_concurrent_positions=1)
                self.balance([])
                ident = self.prepare('005930')
                self.balance(['053800'])
                with self.assertRaises(ConcurrentPositionLimitReached):
                    self.tracker.assert_paper_theme_dispatch('paper', '005930', ident)
                self.tracker.cancel_intent(ident, 'not_sent')
