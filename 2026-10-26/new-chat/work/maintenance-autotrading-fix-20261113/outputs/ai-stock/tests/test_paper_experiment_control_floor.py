"""Prospective actual R/A floor, immutable history, concurrent arrivals."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime,timedelta
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from zoneinfo import ZoneInfo

from kiwoom_autotrader.trading import paper_experiment_rules as r
from kiwoom_autotrader.trading.paper_experiment_store import ExperimentStore

NOW=datetime(2026,11,4,10,tzinfo=ZoneInfo('Asia/Seoul'));DAY='20261104'
PROFILE='native-six-s5-v1'


class ControlFloorTests(unittest.TestCase):
    def test_every_prefix_after_first_has_both_actual_controls_at_least15pct(self):
        prefix=[];counts={arm:0 for arm,_ in r.NATIVE_SIX_S5_WEIGHTS}
        for i in range(180):
            value=r.assign_prospective_candidate(DAY,f'{i:06d}',NOW,
                assigned_at=NOW,prior_allocations=prefix,profile=PROFILE)
            self.assertEqual(r.validate_assignment(value,symbol=value['symbol'],day=DAY,now=NOW),value)
            counts[value['arm']]+=1
            prefix.append({k:value[k] for k in ('symbol','arm','assigned_at','assignment_sha256')})
            if len(prefix)>=2:
                self.assertTrue(all(counts[a]*100>=15*len(prefix) for a in ('EXP_R','EXP_A')),counts)
                self.assertTrue(value['control_floor']['satisfied'])
            self.assertEqual(value['allocation_policy'],r.CONTROL_FLOOR_POLICY)
            self.assertFalse(value['control_floor']['past_assignments_changed'])

    def test_legacy_deficient_prefix_is_not_rewritten_or_claimed_fixed_immediately(self):
        with tempfile.TemporaryDirectory() as folder:
            store=ExperimentStore(Path(folder)/'exp.sqlite3')
            # A deliberately deficient old day remains a deficient old day.
            legacy=[]
            for i in range(1000):
                old=r.assign_candidate(DAY,f'{i:06d}',NOW,profile=PROFILE)
                if old['arm'] in ('EXP_R','EXP_A'):continue
                store.assign(old);legacy.append(deepcopy(old))
                if len(legacy)==8:break
            with sqlite3.connect(store.path) as db:
                before=db.execute('SELECT symbol,payload,digest FROM assignments ORDER BY symbol').fetchall()
            first=store.assign_prospective(DAY,'999900',NOW,assigned_at=NOW,profile=PROFILE)
            self.assertTrue(first['control_floor']['legacy_prefix_below_floor'])
            self.assertFalse(first['control_floor']['satisfied'])
            for i in range(1,12):store.assign_prospective(DAY,f'{999900+i:06d}',NOW,assigned_at=NOW,profile=PROFILE)
            with sqlite3.connect(store.path) as db:
                after=db.execute("SELECT symbol,payload,digest FROM assignments WHERE symbol<'999900' ORDER BY symbol").fetchall()
            self.assertEqual(before,after)
            self.assertEqual(store.assign_prospective(DAY,legacy[0]['symbol'],NOW,
                assigned_at=NOW+timedelta(minutes=1),profile=PROFILE),legacy[0])

    def test_atomic_prefix_for_concurrent_arrivals_has_no_duplicate_sequence(self):
        with tempfile.TemporaryDirectory() as folder:
            store=ExperimentStore(Path(folder)/'exp.sqlite3')
            with ThreadPoolExecutor(max_workers=4) as pool:
                rows=list(pool.map(lambda i:store.assign_prospective(DAY,f'{i:06d}',NOW,
                    assigned_at=NOW,profile=PROFILE),range(40)))
            ordered=sorted(rows,key=lambda v:v['control_floor']['denominator'])
            self.assertEqual([r['control_floor']['denominator'] for r in ordered],list(range(1,41)))
            self.assertTrue(all(r['control_floor']['satisfied'] for r in ordered[1:]))
            self.assertEqual(len(store.assignments(DAY)),40)

    def test_prefix_arithmetic_and_hash_bound_mutation_rejected(self):
        value=r.assign_prospective_candidate(DAY,'005930',NOW,assigned_at=NOW,
            prior_allocations=[],profile=PROFILE)
        value['control_floor']['actual_counts']['EXP_R']+=1
        value['assignment_sha256']=r._digest({k:v for k,v in value.items() if k!='assignment_sha256'})
        with self.assertRaises(ValueError):r.validate_assignment(value,symbol='005930',day=DAY,now=NOW)
        with self.assertRaises(ValueError):r.assign_prospective_candidate(DAY,'005930',NOW,
            assigned_at=NOW,prior_allocations=[{'symbol':'000001','arm':'EXP_ETF_B',
                'assigned_at':NOW.isoformat(),'assignment_sha256':'a'*64}],profile=PROFILE)

    def test_old_hash_assignment_and_fixed_cohort_still_validate_without_migration(self):
        old=r.assign_candidate(DAY,'005930',NOW,profile=PROFILE)
        before=json.dumps(old,sort_keys=True)
        cohort=r.assign_cohort([{'symbol':f'{i:06d}','first_seen_at':NOW.isoformat()} for i in range(25)],
            day=DAY,snapshot_at=NOW,profile=PROFILE)
        for value in [old,*cohort['assignments']]:
            self.assertEqual(r.validate_assignment(value,symbol=value['symbol'],day=DAY,now=NOW),value)
        self.assertEqual(json.dumps(old,sort_keys=True),before)
