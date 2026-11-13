"""Actual metadata families and immutable future assignment annotation."""
from copy import deepcopy
from datetime import datetime,timedelta
import json
from pathlib import Path
import tempfile
import unittest
from zoneinfo import ZoneInfo

from kiwoom_autotrader.trading.paper_experiment_theme import (
    theme_context,bind_assignment_theme,signal_theme_features,validate_theme_context)
from kiwoom_autotrader.trading.theme_concentration import theme_occupancy
from kiwoom_autotrader.trading import paper_experiment_rules as rules

NOW=datetime(2026,11,4,10,tzinfo=ZoneInfo('Asia/Seoul'))


class ThemeMetadataTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'taxonomy.json'
        self.value={'schema':'pro6e-sector-taxonomy-v1','metadata_only':True,
            'created_at':(NOW-timedelta(days=1)).isoformat(),
            'kr_stocks':{'053800':{'sub_themes':['보안·사이버']},
                '042510':{'sub_themes':['보안·인증']},'158430':{'sub_themes':['보안·인증','스테이블·결제']}}}
        self.path.write_text(json.dumps(self.value),encoding='utf-8')

    def test_same_snapshot_family_rule_as_actual_concentration_gate(self):
        context=theme_context('053800',NOW,path=self.path)
        gate=theme_occupancy('053800',['042510'],NOW,path=self.path,unclassified_policy='allow_unknown')
        self.assertEqual(context['theme_families'],gate['theme_families'])
        self.assertEqual(context['taxonomy_evidence'],gate['taxonomy_evidence'])
        self.assertEqual(context['theme'],'보안')
        self.assertEqual(signal_theme_features(context)['theme'],'보안')
        self.assertFalse(context['order_permission'])

    def test_all_families_are_recorded_without_inventing_new_independent_slots(self):
        context=theme_context('158430',NOW,path=self.path)
        self.assertEqual(context['theme_families'],['보안','스테이블'])
        self.assertEqual(context['theme'],'보안|스테이블')

    def test_unknown_reason_and_mapping_version_change_are_explicit(self):
        first=theme_context('005930',NOW,path=self.path)
        self.assertEqual(first['theme'],'unknown');self.assertIsNotNone(first['unknown_reason'])
        self.value['kr_stocks']['005930']={'sub_themes':['반도체']}
        self.path.write_text(json.dumps(self.value),encoding='utf-8')
        second=theme_context('005930',NOW,path=self.path)
        self.assertEqual(second['theme'],'반도체');self.assertNotEqual(first['mapping_version'],second['mapping_version'])
        missing=theme_context('005930',NOW,path=self.path.with_name('missing.json'))
        self.assertEqual(missing['unknown_reason'],'taxonomy_unavailable')

    def test_assignment_annotation_hash_and_asof_are_validated_old_assignment_unmodified(self):
        old=rules.assign_candidate('20261104','053800',NOW,profile='native-six-s5-v1');before=deepcopy(old)
        context=theme_context('053800',NOW,path=self.path);value=bind_assignment_theme(old,context)
        self.assertEqual(old,before);self.assertNotEqual(value['assignment_sha256'],old['assignment_sha256'])
        rules.validate_assignment(value,symbol='053800',day='20261104',now=NOW)
        bad=deepcopy(context);bad['observed_at']=(NOW+timedelta(seconds=1)).isoformat()
        with self.assertRaises(ValueError):validate_theme_context(bad,symbol='053800',as_of=NOW)
        bad=deepcopy(value);bad['theme_context']['theme']='임의';bad['assignment_sha256']=rules._digest({k:v for k,v in bad.items() if k!='assignment_sha256'})
        with self.assertRaises(ValueError):rules.validate_assignment(bad,symbol='053800',day='20261104',now=NOW)
