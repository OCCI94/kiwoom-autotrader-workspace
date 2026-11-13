"""Daily paper entry limits: offline ledgers, fake clocks and no broker access."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase, mock
import sqlite3
import tempfile

from kiwoom_autotrader.api.session import KST
from kiwoom_autotrader.config.settings import _validate, default_settings, SettingsError
from kiwoom_autotrader.trading.execution_log import (ExecutionTracker, DailyEntryLimitReached,
    DailyEntryLimitUnavailable)
from tests.test_golden_cross import GoldenCrossTests


class DailyEntryLedgerTests(TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.now = datetime(2026, 10, 28, 10, tzinfo=KST)
        self.tracker = ExecutionTracker(settings_loader=lambda:{'max_positions_per_theme':0}, log_dir=Path(self.tmp.name), now=lambda: self.now)
        with self.tracker._db(): pass
        self.evidence = {"strategy": "gdcrs", "config": {"max_daily_entries_per_symbol": 1}}

    def test_default_validation_and_legacy_settings(self):
        values = default_settings()
        self.assertEqual(values['gdcrs_max_daily_entries_per_symbol'], 0)
        del values['gdcrs_max_daily_entries_per_symbol']
        self.assertEqual(_validate(values)['gdcrs_max_daily_entries_per_symbol'], 0)
        for value in (True, -1, 1001, '1'):
            with self.subTest(value=value), self.assertRaises(SettingsError):
                _validate({**values, 'gdcrs_max_daily_entries_per_symbol': value})

    def test_distinct_orders_partial_fills_scope_day_and_mode(self):
        with self.tracker._db() as db:
            for scope, day, mode, number, symbol, side in [('default','20261028','paper','2','005930','buy'),
                    ('default','20261028','paper','3','005930','buy'),
                    ('other','20261028','paper','4','005930','buy'),
                    ('default','20261027','paper','5','005930','buy'),
                    ('default','20261028','real','6','005930','buy'),
                    ('default','20261028','paper','7','000660','buy'),
                    ('default','20261028','paper','8','005930','sell')]:
                db.execute('INSERT INTO orders(scope,day,mode,ord_no,symbol,side) VALUES(?,?,?,?,?,?)',
                           (scope,day,mode,number,symbol,side))
            for fill in ('1','2','3'):
                db.execute('INSERT INTO fills(scope,day,mode,ord_no,fill_no,symbol,side) VALUES(?,?,?,?,?,?,?)',
                           ('default','20261028','paper','0000002',fill,'005930','buy'))
        count = self.tracker.daily_buy_entry_counts('paper','005930',self.now)
        self.assertEqual(count, {'accepted': 2, 'reserved': 0})
        # Even a UTC clock uses KST trade date, and another KST day resets it.
        self.assertEqual(self.tracker.daily_buy_entry_counts('paper','005930',self.now.astimezone(timezone.utc))['accepted'],2)
        self.assertEqual(self.tracker.daily_buy_entry_counts('paper','005930',self.now+timedelta(days=1))['accepted'],0)
        with self.assertRaises(DailyEntryLimitUnavailable):
            self.tracker.daily_buy_entry_counts('real','005930',self.now)

    def test_reservation_is_atomic_restart_and_cancel(self):
        other = ExecutionTracker(settings_loader=lambda:{'max_positions_per_theme':0}, log_dir=Path(self.tmp.name), now=lambda: self.now)
        def run(tracker):
            try:
                return tracker.prepare_order('paper','buy','005930',1,'gdcrs', evidence=self.evidence)
            except DailyEntryLimitReached:
                return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            attempts = list(pool.map(run, [self.tracker, other]))
        self.assertEqual(sum(x is not None for x in attempts),1)
        self.assertEqual(other.daily_buy_entry_counts('paper','005930',self.now), {'accepted':0,'reserved':1})
        self.tracker.cancel_intent(next(x for x in attempts if x is not None))
        self.assertIsNotNone(run(other))

    def test_unlimited_does_not_invoke_limit_lookup(self):
        self.tracker._daily_buy_entry_counts = lambda *args: self.fail('unlimited attempted daily lookup')
        self.tracker.prepare_order('paper','buy','005930',1,'gdcrs')
        self.tracker.prepare_order('paper','buy','005930',1,'gdcrs')

    def test_accepted_order_counts_once_and_blocks_until_next_kst_day(self):
        from kiwoom_autotrader.api.orders import OrderReceipt
        intent = self.tracker.prepare_order('paper','buy','005930',1,'gdcrs',evidence=self.evidence)
        receipt = OrderReceipt('paper','buy','005930','1',None,'0000123','https://mockapi.kiwoom.com')
        self.tracker.register_order(receipt,'gdcrs',intent_id=intent)
        self.assertEqual(self.tracker.daily_buy_entry_counts('paper','005930',self.now), {'accepted':1,'reserved':0})
        with self.assertRaises(DailyEntryLimitReached):
            self.tracker.prepare_order('paper','buy','005930',1,'gdcrs',evidence=self.evidence)
        self.now += timedelta(days=1)
        self.assertIsNotNone(self.tracker.prepare_order('paper','buy','005930',1,'gdcrs',evidence=self.evidence))

    def test_empty_existing_database_is_not_repaired_or_accepted(self):
        blankdir=Path(self.tmp.name)/'empty'; blankdir.mkdir()
        blank=ExecutionTracker(settings_loader=lambda:{'max_positions_per_theme':0}, log_dir=blankdir, now=lambda:self.now)
        with sqlite3.connect(blank.path) as db: db.execute('CREATE TABLE unrelated(id INTEGER)')
        original=blank.path.read_bytes()
        with self.assertRaises(DailyEntryLimitUnavailable):
            blank.daily_buy_entry_counts('paper','005930',self.now)
        with self.assertRaises(DailyEntryLimitUnavailable):
            blank.prepare_order('paper','buy','005930',1,'gdcrs',evidence=self.evidence)
        self.assertEqual(blank.path.read_bytes(), original)
        with sqlite3.connect(f'file:{blank.path}?mode=ro',uri=True) as db:
            self.assertEqual(db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall(), [('unrelated',)])

    def test_missing_table_or_immutable_trigger_is_not_repaired(self):
        for kind, name in [('table','fills'),('trigger','order_decisions_no_delete')]:
            with self.subTest(kind=kind):
                folder=Path(self.tmp.name)/kind; folder.mkdir()
                tracker=ExecutionTracker(settings_loader=lambda:{'max_positions_per_theme':0}, log_dir=folder,now=lambda:self.now)
                with tracker._db(): pass
                with sqlite3.connect(tracker.path) as db: db.execute(f'DROP {kind.upper()} {name}')
                original=tracker.path.read_bytes()
                with self.assertRaises(DailyEntryLimitUnavailable):
                    tracker.daily_buy_entry_counts('paper','005930',self.now)
                with self.assertRaises(DailyEntryLimitUnavailable):
                    tracker.prepare_order('paper','buy','005930',1,'gdcrs',evidence=self.evidence)
                self.assertEqual(tracker.path.read_bytes(),original)
                with sqlite3.connect(f'file:{tracker.path}?mode=ro',uri=True) as db:
                    self.assertIsNone(db.execute('SELECT name FROM sqlite_master WHERE name=?',(name,)).fetchone())

    def test_malformed_order_key_fails_closed(self):
        with self.tracker._db() as db:
            db.execute("INSERT INTO orders(scope,day,mode,ord_no,symbol,side) VALUES (?,?,?,?,?,?)",
                       ('default','20261028','paper','bad','005930','buy'))
        with self.assertRaises(DailyEntryLimitUnavailable):
            self.tracker.daily_buy_entry_counts('paper','005930',self.now)

    def test_unknown_and_missing_ledger_fail_closed(self):
        with self.tracker._db() as db:
            db.execute("INSERT INTO intents(scope,id,day,mode,side,symbol,state) VALUES (?,?,?,?,?,?,?)",
                       ('default','bad','20261028','paper','buy','005930','unknown'))
        with self.assertRaises(DailyEntryLimitUnavailable):
            self.tracker.daily_buy_entry_counts('paper','005930',self.now)
        missing = ExecutionTracker(settings_loader=lambda:{'max_positions_per_theme':0}, log_dir=Path(self.tmp.name)/'missing', now=lambda:self.now)
        with self.assertRaises(DailyEntryLimitUnavailable):
            missing.daily_buy_entry_counts('paper','005930',self.now)


class DailyEntryMonitorTests(GoldenCrossTests):
    def test_cap_skip_is_audited_before_claim_or_buy(self):
        self.settings['gdcrs_max_daily_entries_per_symbol'] = 1
        tracker = SimpleNamespace(daily_buy_entry_counts=lambda *a: {'accepted':1, 'reserved':0})
        self.session.paper_automation_reconcile = SimpleNamespace(_tracker=tracker)
        self.seed(); self.cross(); self.buy.assert_not_called()
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute('SELECT skip_reason FROM entry_decisions').fetchone()[0], 'daily_entry_limit_reached')
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='attempts'").fetchone())

    def test_missing_ledger_blocks_and_audits_unknown(self):
        self.settings['gdcrs_max_daily_entries_per_symbol'] = 2
        self.seed(); self.cross(); self.buy.assert_not_called()
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute('SELECT skip_reason FROM entry_decisions').fetchone()[0], 'daily_entry_limit_unavailable')

    def test_real_mode_is_not_limited(self):
        self.settings['gdcrs_max_daily_entries_per_symbol'] = 1
        self.session.mode = 'real'
        self.seed(); self.cross()
        self.buy.assert_called_once()

    def test_atomic_race_reason_written_to_entry_decisions(self):
        from kiwoom_autotrader.telegram.commands.orders import OrderReplies
        self.settings['gdcrs_max_daily_entries_per_symbol'] = 1
        self.session.paper_automation_reconcile = SimpleNamespace(_tracker=SimpleNamespace(
            daily_buy_entry_counts=lambda *a: {'accepted':0, 'reserved':0}))
        self.buy.return_value = OrderReplies([], submitted=False, decision_reason='daily_entry_limit_reached')
        self.seed(); self.cross()
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute('SELECT skip_reason FROM entry_decisions').fetchone()[0], 'daily_entry_limit_reached')

    def test_under_cap_forwards_setting_in_frozen_decision(self):
        self.settings['gdcrs_max_daily_entries_per_symbol'] = 2
        tracker = SimpleNamespace(daily_buy_entry_counts=lambda *a: {'accepted':1, 'reserved':0})
        self.session.paper_automation_reconcile = SimpleNamespace(_tracker=tracker)
        from kiwoom_autotrader.trading.order_context import automated_orders, _current
        def buy(*a, **kw):
            self.assertEqual(_current.get().evidence.as_dict()['config']['max_daily_entries_per_symbol'],2)
            return ['test']
        self.buy.side_effect = buy
        with automated_orders(tracker, 'gdcrs'):
            self.seed(); self.cross()
        self.buy.assert_called_once()


class PropagationTests(TestCase):
    def test_atomic_race_rejection_has_explicit_reason_and_no_transport(self):
        from kiwoom_autotrader.api import orders
        from tests.test_orders import fake_session
        session = fake_session()
        for error, reason in ((DailyEntryLimitReached('limit'), 'daily_entry_limit_reached'),
                              (DailyEntryLimitUnavailable('unknown'), 'daily_entry_limit_unavailable')):
            with self.subTest(reason=reason), mock.patch.object(orders, 'prepare_tracking', side_effect=error), \
                    mock.patch.object(orders.urllib.request, 'build_opener') as opener:
                with self.assertRaises(orders.OrderError) as caught:
                    orders.submit_order(session, 'buy', '005930', 1, 70000)
                self.assertEqual(caught.exception.reason_code, reason)
                self.assertFalse(caught.exception.uncertain)
                opener.assert_not_called()

    def test_order_command_propagates_reason_into_monitor_decision(self):
        from kiwoom_autotrader.telegram.commands import orders as commands
        from tests.test_orders import fake_session
        session = fake_session()
        with mock.patch.object(commands, 'limit_automatic_buy_budget', return_value=100000), \
             mock.patch.object(commands, 'fetch_stock_info', return_value={'stk_cd':'005930','cur_prc':'70000'}), \
             mock.patch.object(commands, 'submit_order', side_effect=commands.OrderError('limit', reason_code='daily_entry_limit_reached')):
            result = commands.handle_buy('buy 005930 max 100000', session)
        self.assertEqual(result.decision_reason, 'daily_entry_limit_reached')
        self.assertFalse(result.submitted)
