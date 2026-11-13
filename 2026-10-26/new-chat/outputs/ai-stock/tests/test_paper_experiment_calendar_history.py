from pathlib import Path
from datetime import date, timedelta
from tempfile import TemporaryDirectory
from unittest import TestCase
import sqlite3
from kiwoom_autotrader.trading.paper_experiment_store import ExperimentStore

class CalendarHistoryTests(TestCase):
    def setUp(self):
        self.tmp=TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=ExperimentStore(Path(self.tmp.name)/'store.sqlite3')
    def test_readonly_sorted_cutoff(self):
        for day in ('20261030','20261028','20261102'):
            self.store.selection(day,'market_calendar',{'date':day})
        self.store.selection('20261029','other',{'date':'ignored'})
        before=self.store.path.stat().st_mtime_ns
        result=self.store.market_calendar_history('20261030')
        self.assertEqual(result,[{'day':'20261028','payload':{'date':'20261028'}},
                                 {'day':'20261030','payload':{'date':'20261030'}}])
        self.assertEqual(self.store.path.stat().st_mtime_ns,before)
    def test_malformed_json_and_invalid_dates_fail_closed(self):
        with self.assertRaises(ValueError):self.store.market_calendar_history('20261342')
        with sqlite3.connect(self.store.path) as db:
            db.execute("INSERT INTO selections VALUES ('20261028','market_calendar','not json')")
        with self.assertRaises(ValueError):self.store.market_calendar_history('20261030')
    def test_symlink_rejected(self):
        link=Path(self.tmp.name)/'link.sqlite3';link.symlink_to(self.store.path)
        self.store.path=link
        with self.assertRaises(ValueError):self.store.market_calendar_history('20261030')

    def test_history_bound_keeps_latest_400_in_chronological_order(self):
        start=date(2025,1,1)
        with sqlite3.connect(self.store.path) as db:
            db.executemany('INSERT INTO selections VALUES (?,?,?)',
                [((start+timedelta(days=i)).strftime('%Y%m%d'),'market_calendar','{}') for i in range(401)])
        rows=self.store.market_calendar_history('20261231')
        self.assertEqual(len(rows),400)
        self.assertEqual(rows[0]['day'],(start+timedelta(days=1)).strftime('%Y%m%d'))
        self.assertEqual(rows[-1]['day'],(start+timedelta(days=400)).strftime('%Y%m%d'))
