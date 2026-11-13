import copy
from datetime import datetime, timedelta
from decimal import Decimal
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo
from kiwoom_autotrader.trading.toss_candidate_feed import read_toss_candidates, merge_toss_candidates

NOW=datetime(2026,11,2,10,0,20,tzinfo=ZoneInfo('Asia/Seoul'))


def row(code='005930'):
    return {'symbol':code,'name':'삼성전자','currency':'KRW','market':'KOSPI','security_type':'STOCK',
        'common_share':True,'listing_active':True,'krx_suspended':False,'liquidation_trading':False,
        'permission_to_trade':False,'current_price':105000,'day_open':101000,'base_price':100000,
        'change_pct':'5','price_timestamp':NOW.isoformat(),'ranked_at':NOW.isoformat(),
        'available_at':NOW.isoformat(),'turnover_rank':1,'original_top10_complete':True,'of005_top10_rank':1,'turnover_krw':100000000,
        'short_session_day':'2026-10-30','daily_baseline_session_day':'2026-10-30',
        'short_amount_share':'0.10','ma20':'100000','adr20_pct':'2.5','adr_extension_multiple':'2',
        'completed_daily_count':20,'risk_review':{'coverage_complete':True,'unresolved_major_risk':False,
            'reviewed_at':NOW.isoformat(),'expires_at':(NOW+timedelta(hours=1)).isoformat()},
        'source_hashes':{k:'a'*64 for k in ('price','ranking','stocks','candles','short_selling','investor_trading','calendar','risk_review')}}


def feed():
    return {'schema':'toss-hybrid-watch-feed-v1','status':'ready','read_only':True,'permission_to_trade':False,
        'generated_at':NOW.isoformat(),'expires_at':(NOW+timedelta(seconds=75)).isoformat(),
        'session_day':'2026-11-02','previous_session_day':'2026-10-30',
        'regular_start':NOW.replace(hour=9,minute=0,second=0).isoformat(),
        'regular_end':NOW.replace(hour=15,minute=30,second=0).isoformat(),'candidates':[row()]}


class TossFeedTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'feed.json'
    def write(self,doc):
        self.path.write_text(json.dumps(doc));self.path.chmod(0o600)
    def test_valid_and_semantic_boundary(self):
        self.write(feed());self.assertEqual(read_toss_candidates(self.path,NOW)['status'],'ready')
        for key,value in [('short_amount_share','0.10001'),('current_price',100000),('adr_extension_multiple','6'),
                          ('common_share',False),('day_open',105000),('base_price',120000)]:
            doc=feed();doc['candidates'][0][key]=value;self.write(doc)
            self.assertEqual(read_toss_candidates(self.path,NOW)['candidates'],[],key)
    def test_missing_stale_future_waiting_are_immediate_empty(self):
        self.assertEqual(read_toss_candidates(self.path,NOW)['fallback'],'KIWOOM_ONLY')
        for key,value in [('status','error'),('status','waiting_market'),('generated_at',(NOW+timedelta(seconds=1)).isoformat()),
                          ('session_day','2026-10-30')]:
            doc=feed();doc[key]=value;self.write(doc);self.assertEqual(read_toss_candidates(self.path,NOW)['candidates'],[])
        self.write(feed());self.assertEqual(read_toss_candidates(self.path,NOW+timedelta(seconds=76))['candidates'],[])
    def test_underlying_quote_age_not_extended_by_feed_refresh(self):
        doc=feed();doc['candidates'][0]['price_timestamp']=(NOW-timedelta(seconds=76)).isoformat();self.write(doc)
        self.assertEqual(read_toss_candidates(self.path,NOW)['candidates'],[])
    def test_risk_and_previous_session_fail_closed(self):
        for change in ('risk','date'):
            doc=feed()
            if change=='risk':doc['candidates'][0]['risk_review']['coverage_complete']=False
            else:doc['candidates'][0]['short_session_day']='2026-10-29'
            self.write(doc);self.assertEqual(read_toss_candidates(self.path,NOW)['candidates'],[])
    def test_risk_and_feed_expire_between_admission_and_before_send(self):
        from kiwoom_autotrader.trading.toss_candidate_feed import validate_candidate
        candidate=row();candidate['risk_review']['expires_at']=(NOW+timedelta(seconds=1)).isoformat()
        with self.assertRaises(ValueError):validate_candidate(candidate,now=NOW+timedelta(seconds=2),session_day='2026-11-02',previous_session_day='2026-10-30')
        candidate=row();candidate['feed_expires_at']=(NOW+timedelta(seconds=1)).isoformat()
        with self.assertRaises(ValueError):validate_candidate(candidate,now=NOW+timedelta(seconds=2),session_day='2026-11-02',previous_session_day='2026-10-30')
    def test_symlink_fifo_oversize_and_duplicate_rejected(self):
        target=Path(self.temp.name)/'other';target.write_text(json.dumps(feed()));self.path.symlink_to(target)
        self.assertEqual(read_toss_candidates(self.path,NOW)['candidates'],[]);self.path.unlink()
        os.mkfifo(self.path);self.assertEqual(read_toss_candidates(self.path,NOW)['candidates'],[]);self.path.unlink()
        self.path.write_bytes(b'x'*65537);self.assertEqual(read_toss_candidates(self.path,NOW)['candidates'],[])
        doc=feed();doc['candidates']*=2;self.write(doc);self.assertEqual(read_toss_candidates(self.path,NOW)['candidates'],[])
    def test_merge_requires_independent_membership_and_does_not_invent_surge(self):
        self.write(feed());source=read_toss_candidates(self.path,NOW)
        result={'schema':'paper-funnel-candidates-v1','status':'unavailable','candidates':[],'base_candidates':[]}
        self.assertEqual(merge_toss_candidates(result,source,[],{},now=NOW,funnel=True)['candidates'],[])
        member={'code':'005930','name':'삼성전자','marketCode':'0','auditInfo':'정상','orderWarning':'0',
                'state':'증거금20%','upSizeName':'대형주','companyClassName':'일반기업'}
        pages=[{'market':'0','rows':{'005930':member},'request_started_at':NOW.isoformat(),
                'available_at':NOW.isoformat(),'source_sha256':'b'*64}]
        out=merge_toss_candidates(result,source,pages,{},now=NOW,funnel=True)
        self.assertEqual(out['status'],'ready');self.assertIsNone(out['candidates'][0]['surge_rate_pct'])
        self.assertNotIn('surge',out['candidates'][0]['source_hashes'])
        self.assertEqual(out['candidates'][0]['candidate_basis'],'toss_hybrid_verified_watch')
        self.assertFalse(out['candidates'][0]['signal_eligible'])
    def test_fallback_preserves_existing_kiwoom_candidate(self):
        original={'status':'ready','candidates':[{'symbol':'003220','turnover_krw':1}],'base_candidates':[]}
        out=merge_toss_candidates(original,{'status':'unavailable'},[],{},now=NOW,funnel=True)
        self.assertEqual(out['candidates'],original['candidates']);self.assertNotIn('toss_discovery',original)
    def test_provider_dynamic_and_funnel_integrate_local_only_and_disabled_default(self):
        from kiwoom_autotrader.api.session import BrokerSession,IssuedToken
        from kiwoom_autotrader.trading.paper_strategy_context import PaperStrategyContextProvider
        session=BrokerSession(now=lambda:NOW);session.mode='paper';session._issued=IssuedToken('fake','20261102235959')
        self.write(feed())
        provider=PaperStrategyContextProvider(session,now=lambda:NOW,enable_surge=True,
            enable_toss_hybrid=True,toss_candidates_path=self.path,taxonomy_path=Path(self.temp.name)/'absent')
        member={'code':'005930','name':'삼성전자','marketCode':'0','auditInfo':'정상','orderWarning':'0',
                'state':'증거금20%','upSizeName':'대형주','companyClassName':'일반기업'}
        pages=[{'market':'0','rows':{'005930':member},'request_started_at':NOW.isoformat(),
                'available_at':NOW.isoformat(),'source_sha256':'b'*64}]
        with patch.object(provider,'_membership',return_value=pages),patch.object(provider,'_surge',return_value=None),\
                patch('socket.create_connection',side_effect=AssertionError('receiver must not call Toss')):
            dynamic=provider.discover_dynamic_candidates(configured_symbols=())
            funnel=provider.discover_funnel_candidates(configured_symbols=())
        self.assertEqual(dynamic['schema'],'paper-dynamic-candidates-v1');self.assertEqual(dynamic['candidates'][0]['symbol'],'005930')
        self.assertEqual(funnel['schema'],'paper-funnel-candidates-v1');self.assertEqual(funnel['candidates'][0]['symbol'],'005930')
        self.assertEqual(dynamic['request_accounting']['toss_http_calls_in_receiver'],0)


if __name__=='__main__':unittest.main()
