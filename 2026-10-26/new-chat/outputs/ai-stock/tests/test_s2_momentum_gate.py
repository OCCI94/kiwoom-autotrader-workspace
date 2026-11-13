"""Same-day ka10001 open is distinct from positive previous-close change."""
from copy import deepcopy
from datetime import datetime, timedelta
from unittest import TestCase
from zoneinfo import ZoneInfo

from kiwoom_autotrader.trading.multi_strategy import s2_momentum_eligible
from kiwoom_autotrader.trading.paper_strategy_context import (PaperStrategyContextProvider, ContextUnavailable,
    intraday_bullish_candle_state)
from kiwoom_autotrader.api.session import BrokerSession, IssuedToken


class S2MomentumTests(TestCase):
    def setUp(self):
        self.now=datetime(2026,10,30,11,30,2,tzinfo=ZoneInfo('Asia/Seoul'))
        self.market={'current_price':105,'day_open':100,'change_pct':'5',
            'source':'ka10001_normalized_response','source_sha256':'a'*64,
            'request_started_at':self.now.isoformat(),'available_at':self.now.isoformat(),
            'observed_at':self.now.isoformat()}

    def test_authorized_inclusive_three_to_fifteen_and_strict_bullish_day(self):
        for change,expected in [('2.999',False),('3',True),('3.999',True),('4',True),('15',True),('15.001',False),('18',False)]:
            self.market['change_pct']=change
            self.assertEqual(s2_momentum_eligible(self.market,self.now),expected)
        self.market['change_pct']='5'
        for opening in (None,0,105,106,True,'100'):
            self.market['day_open']=opening
            self.assertFalse(s2_momentum_eligible(self.market,self.now))

    def test_stale_future_previous_day_and_wrong_source_not_accepted(self):
        for key,value in [('available_at',(self.now+timedelta(seconds=1)).isoformat()),
                          ('request_started_at',(self.now-timedelta(days=1)).isoformat()),
                          ('request_started_at',(self.now-timedelta(seconds=6)).isoformat()),
                          ('source','unverified'),('source_sha256','bad')]:
            market={**self.market,key:value}
            self.assertFalse(s2_momentum_eligible(market,self.now))

    def test_provider_hash_binds_optional_open_and_signed_price_normalized(self):
        session=BrokerSession(now=lambda:self.now);session.mode='paper'
        session._issued=IssuedToken('test','20261030235959')
        data={'stk_cd':'005930','cur_prc':'+105','open_pric':'-100',
              'high_pric':'110','upl_pric':'130','flu_rt':'5','trde_pre':'40'}
        def context(value):
            return PaperStrategyContextProvider(session,now=lambda:self.now,fetcher=lambda *a:dict(value)).fetch('005930')
        first=context(data);self.assertEqual(first['day_open'],100)
        self.assertIs(first['intraday_bullish_candle'],True)
        self.assertTrue(s2_momentum_eligible(first,self.now))
        missing=context({**data,'open_pric':''})
        self.assertIsNone(missing['day_open']);self.assertFalse(s2_momentum_eligible(missing,self.now))
        self.assertIsNone(missing['intraday_bullish_candle'])
        self.assertNotEqual(first['source_sha256'],missing['source_sha256'])
        with self.assertRaises(ContextUnavailable):context({**data,'open_pric':'111'})

    def test_exit_context_does_not_require_open(self):
        session=BrokerSession(now=lambda:self.now);session.mode='paper'
        session._issued=IssuedToken('test','20261030235959')
        result=PaperStrategyContextProvider(session,now=lambda:self.now,exit_only=True,
            fetcher=lambda *a:{'stk_cd':'005930','cur_prc':'100','open_pric':'bad'}).fetch('005930')
        self.assertEqual(result['current_price'],100);self.assertIsNone(result['day_open'])
        self.assertIsNone(result['intraday_bullish_candle'])

    def test_derived_bullish_candle_distinguishes_bearish_equal_and_unknown(self):
        for current,opening,expected in ((105,100,True),(100,105,False),(100,100,False),
                (None,100,None),(100,None,None),(None,None,None),(0,100,None),
                (True,100,None),(100,'99',None),(100,-1,None),(10**12+1,100,None)):
            with self.subTest(current=current,opening=opening):
                self.assertIs(intraday_bullish_candle_state(current,opening),expected)

    def test_provider_derives_gap_fade_and_keeps_source_hash_and_cache_isolated(self):
        session=BrokerSession(now=lambda:self.now);session.mode='paper'
        session._issued=IssuedToken('test','20261030235959')
        data={'stk_cd':'005930','cur_prc':'100','open_pric':'105','high_pric':'110',
              'upl_pric':'130','flu_rt':'5','trde_pre':'40','intraday_bullish_candle':True}
        provider=PaperStrategyContextProvider(session,now=lambda:self.now,fetcher=lambda *a:dict(data))
        first=provider.fetch('005930')
        self.assertIs(first['intraday_bullish_candle'],False)
        self.assertFalse(s2_momentum_eligible(first,self.now))
        first['intraday_bullish_candle']=True
        self.assertIs(provider.fetch('005930')['intraday_bullish_candle'],False)
        data['intraday_bullish_candle']=False
        second=PaperStrategyContextProvider(session,now=lambda:self.now,fetcher=lambda *a:dict(data)).fetch('005930')
        self.assertEqual(first['source_sha256'],second['source_sha256'])
        data['cur_prc']=''
        with self.assertRaises(ContextUnavailable):
            PaperStrategyContextProvider(session,now=lambda:self.now,fetcher=lambda *a:dict(data)).fetch('005930')

    def test_forged_derived_flag_cannot_override_price_or_source(self):
        for market in ({**self.market,'day_open':106}, {**self.market,'day_open':None},
                       {**self.market,'current_price':None}, {**self.market,'source':'unverified'}):
            self.assertFalse(s2_momentum_eligible({**market,'intraday_bullish_candle':True},self.now))
        self.assertTrue(s2_momentum_eligible({**self.market,'intraday_bullish_candle':False},self.now))
