"""Candidate discovery alone grants neither ownership nor order permission."""
import unittest
from datetime import datetime,timedelta
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo
from kiwoom_autotrader.api.minute_chart import MinuteBar
from kiwoom_autotrader.trading.multi_strategy import MultiStrategyMonitor, S3, STRATEGIES

class FlatCandidateMonitorTests(unittest.TestCase):
    def setUp(self):
        self.now=datetime(2026,10,30,11,31,1,tzinfo=ZoneInfo('Asia/Seoul'))
        self.clock=[self.now]
        self.candidate={'status':'flat_candidate','candidate_only':True,
                        'requires_fresh_broker_reconciliation':True}
        self.positions={s:dict(self.candidate) for s in STRATEGIES}
        self.settings={'paper_multi_strategy_enabled':True,'paper_multi_strategy_ids':[S3],
                       'gdcrs_stocks':[{'code':'005930','budget':1000000}]}
        self.tracker=SimpleNamespace(multi_strategy_position=lambda mode,code,strategy,now:self.positions[strategy],
            multi_strategy_open_symbols=lambda *args:{'status':'available','symbols':['005930']})
        self.session=SimpleNamespace(paper_automation_reconcile=SimpleNamespace(_tracker=self.tracker))
        self.calls=[]
        outer=self
        class Provider:
            def fetch(self,symbol,as_of=None):
                return {'symbol':symbol,'change_pct':'19.9' if outer.clock[0]==outer.now else '20.1',
                        'current_price':100,'upper_limit_price':130}
            def is_fresh(self,context,now):return True
        def chart(session,symbol,count):
            now=self.clock[0]
            return [MinuteBar(now.replace(second=0)-timedelta(minutes=21-i),100,100,
                  source_symbol=symbol,source_mode='paper',source_query_started_at=now,
                  source_response_sha256='a'*64,open_price=100,high_price=101,low_price=99)
                  for i in range(21)]
        def gateway(**kw):
            self.calls.append(kw)
            # Simulated broker gate rejects: candidate itself cannot submit.
            return []
        self.monitor=MultiStrategyMonitor(settings_loader=lambda:self.settings,now=lambda:self.clock[0],
              chart_fetcher=chart,provider_factory=lambda _:Provider(),executor=gateway,
              cooldown_check=lambda *args:True)
        self.patcher=patch('kiwoom_autotrader.trading.multi_strategy.session_identity',return_value=('paper','fake'))
        self.patcher.start();self.addCleanup(self.patcher.stop)
    def tick(self):self.monitor.tick(self.session,lambda _:None)
    def test_candidate_is_visible_and_first_observation_is_baseline(self):
        self.tick()
        self.assertEqual(self.calls,[])
        status=self.monitor.status()['flat_candidates']['005930']
        self.assertTrue(status['candidate_only'])
        self.assertTrue(status['requires_fresh_broker_reconciliation'])
        self.clock[0]+=timedelta(minutes=1);self.tick()
        self.assertEqual(len(self.calls),1)
        self.assertEqual(self.calls[0]['side'],'buy')
        self.assertEqual(self.calls[0]['position']['status'],'flat_candidate')
        self.assertEqual(self.calls[0]['budget'],1000000)
    def test_bad_candidate_provenance_is_rejected(self):
        self.positions[S3].pop('requires_fresh_broker_reconciliation')
        self.tick()
        self.assertEqual(self.calls,[])
        self.assertEqual(self.monitor.status()['symbol_errors']['005930'],'ValueError')
    def test_pending_or_unsupported_owner_blocks_candidate(self):
        for kind in ('pending','unsupported'):
            with self.subTest(kind=kind):
                self.positions[S3]={'status':kind}
                self.clock[0]+=timedelta(minutes=1);self.tick()
                self.assertEqual(self.calls,[])
    def test_candidate_is_not_a_sellable_owned_position(self):
        self.settings['gdcrs_stocks']=[]
        self.tick()
        self.assertEqual(self.calls,[])
        self.assertTrue(self.monitor.status()['flat_candidates']['005930']['candidate_only'])

if __name__=='__main__':unittest.main()
