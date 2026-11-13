from contextlib import nullcontext
from datetime import datetime,timedelta
import hashlib,json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from zoneinfo import ZoneInfo
from kiwoom_autotrader.trading.paper_experiment_disclosure import read_disclosure_clearance

class DisclosureTests(TestCase):
    def setUp(self):
        self.tmp=TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name).resolve();self.archive=self.root/'archive';self.archive.mkdir()
        self.page=self.archive/'005930-dart-1.json';self.risk=self.root/'risk.json'
        self.now=datetime(2026,11,2,10,tzinfo=ZoneInfo('Asia/Seoul'))
        self.body={'status':'000','page_no':1,'page_count':100,'total_count':1,'total_page':1,
                   'list':[{'stock_code':'005930','rcept_no':'20261102000001','rcept_dt':'20261102'}]}
        self.doc={'schema':'toss-hybrid-risk-evidence-v1','generated_at':self.now.isoformat(),
            'expires_at':(self.now+timedelta(minutes=30)).isoformat(),'evaluations':[{
            'symbol':'005930','status':'HOLD','reasons':['SHORT_SELLING_FIVE_DAY_MAX_OVER_10_PERCENT'],
            'disclosure':{'status':'PASS','reasons':[]},'dart_list_proofs':[]}]}
        self.save()
    def save(self):
        raw=json.dumps(self.body).encode();self.page.write_bytes(raw)
        self.doc['evaluations'][0]['dart_list_proofs']=[{'path':str(self.page),
            'sha256':hashlib.sha256(raw).hexdigest(),'observed_at':self.now.isoformat()}]
        self.risk.write_text(json.dumps(self.doc))
    def read(self,now=None):
        return read_disclosure_clearance(self.risk,'005930',now or self.now,archive_root=self.archive,
                                          guard_factory=nullcontext)
    def test_disclosure_pass_is_not_blocked_by_unrelated_short_filter(self):
        result=self.read();self.assertIsNotNone(result)
        self.assertFalse(result['unresolved_major_risk']);self.assertIsNone(result['original_documents_reviewed'])
    def test_incomplete_stale_wrong_code_or_nonpass_never_clear(self):
        for mutation in ('stale','code','page','pending','hash'):
            with self.subTest(mutation=mutation):
                self.save()
                if mutation=='code':self.body['list'][0]['stock_code']='000660';self.save()
                if mutation=='page':self.body['total_count']=101;self.body['total_page']=2;self.save()
                if mutation=='pending':self.doc['evaluations'][0]['disclosure']['status']='PENDING';self.save()
                if mutation=='hash':self.page.write_text('{}')
                self.assertIsNone(self.read(self.now+timedelta(seconds=1201) if mutation=='stale' else None))
                self.body={'status':'000','page_no':1,'page_count':100,'total_count':1,'total_page':1,
                    'list':[{'stock_code':'005930','rcept_no':'20261102000001','rcept_dt':'20261102'}]}
                self.doc['evaluations'][0]['disclosure']['status']='PASS'
    def test_zero_results_status013_is_not_fabricated_original_review(self):
        self.body={'status':'013'};self.save()
        self.assertIsNotNone(self.read());self.assertIsNone(self.read()['original_documents_reviewed'])

    def test_twenty_minute_boundary_is_explicit(self):
        result=self.read(self.now+timedelta(seconds=1200))
        self.assertEqual(result['oldest_list_age_seconds'],1200)
        self.assertIsNone(self.read(self.now+timedelta(seconds=1200,microseconds=1)))
