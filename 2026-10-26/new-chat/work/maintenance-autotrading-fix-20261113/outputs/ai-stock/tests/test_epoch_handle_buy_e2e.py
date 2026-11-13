"""Exercise real command, policy, ledger and dispatch guards with fake HTTP."""
from contextlib import contextmanager
from datetime import timedelta
from decimal import Decimal
import json,sqlite3,unittest
from unittest.mock import patch
import tests.test_multi_strategy_epoch_review as epoch_fixture
from tests.test_orders import fake_session
from kiwoom_autotrader.api.minute_chart import MinuteBar
from kiwoom_autotrader.api.orders import HTTPNotSentError
from kiwoom_autotrader.trading.multi_strategy import execute_paper_strategy, EntryState, S3

class EpochHandleBuyTests(unittest.TestCase):
    def setUp(self):
        self.fx=epoch_fixture.EpochIndependentReview('test_complete_zero_broker_evidence_allows_new_generation_without_touching_old_history')
        self.fx.setUp();self.addCleanup(self.fx.doCleanups)
        self.tracker=self.fx.tracker
        self.session=fake_session(clock=lambda:self.fx.now)
        proof=self.fx.proof()
        class Reconcile:
            _tracker=self.tracker
            def available_buy_principal(self,symbol):return 1000000
            def __call__(self,symbol,**kwargs):
                assert symbol=='005930' and kwargs['quantity']=='10' and kwargs['price']=='1000'
                def guard():pass
                guard.reconciliation_evidence=proof
                return guard
        self.session.paper_automation_reconcile=Reconcile()
        self.session.execution_recording_guard=lambda:None
        self.calls=[]; self.corrupt=False
        @contextmanager
        def queue(opener,request,**kwargs):
            if self.corrupt:
                self.tracker.record_broker_observation({'schema_version':1,'source_api':'00',
                   'source_kind':'broker_observation','observed_at':self.fx.now.isoformat(),
                   'day':'20261030','mode':'paper','event_kind':'execution_stream',
                   'values':{'9203':'99','9001':'A005930','907':'2','913':'체결','908':'100010',
                             '909':'9','914':'1000','915':'1','911':'1','903':'1000'}})
            try:kwargs['before_send']()
            except Exception:raise HTTPNotSentError('rejected') from None
            self.calls.append(json.loads(request.data))
            class Response:
                def read(self,n):return b'{"return_code":0,"ord_no":"31"}'
            yield Response()
        for target, obj in (('kiwoom_autotrader.api.orders.queued_open',queue),
            ('kiwoom_autotrader.trading.paper_policy.load_settings',lambda:self.fx.settings),
            ('kiwoom_autotrader.telegram.commands.orders.fetch_stock_info',lambda *a:{'stk_cd':'005930','cur_prc':'1000'})):
            p=patch(target,side_effect=obj);p.start();self.addCleanup(p.stop)
    def run_signal(self):
        now=self.fx.now
        bars=[MinuteBar(now.replace(second=0)-timedelta(minutes=21-i),1000,100,
              source_symbol='005930',source_mode='paper',source_query_started_at=now,
              source_response_sha256='a'*64,open_price=1000,high_price=1001,low_price=999) for i in range(21)]
        market={'symbol':'005930','request_started_at':now.isoformat(),'observed_at':now.isoformat(),
          'available_at':now.isoformat(),'source':'ka10001_normalized_response','source_sha256':'a'*64,
          'source_market_timestamp':None,'source_freshness_verified':False,'current_price':1000,
          'day_high':1100,'upper_limit_price':1300,'change_pct':'20.1','rising_volume_surge':None,
          'surge_evidence':None,'theme_evidence':None,'dart_evidence':None}
        position=self.tracker.multi_strategy_position('paper','005930',S3,now)
        self.assertEqual(position['status'],'flat_candidate')
        return execute_paper_strategy(session=self.session,tracker=self.tracker,strategy=S3,symbol='005930',
          side='buy',reason='observed_20pct_crossing',quantity=None,budget=10000,position=position,
          bars=bars,market=market,baseline=EntryState(bars[-1].start-timedelta(minutes=1),1001,False,Decimal('19.9')),
          observed_at=now,before_send=lambda:None,run_id='test',source_hash='a'*64)
    def test_v5_actual_command_policy_atomic_intent_and_fake_transport(self):
        result=self.run_signal()
        self.assertTrue(result.submitted,repr(result))
        self.assertEqual(len(self.calls),1)
        self.assertEqual((self.calls[0]['ord_qty'],self.calls[0]['ord_uv']),('10','1000'))
        with sqlite3.connect(self.tracker.path) as db:
            row=db.execute("SELECT state,order_number FROM intents WHERE day='20261030' AND strategy=?",(S3,)).fetchone()
            self.assertEqual(row,('accepted','31'))
    def test_late_unit_event_is_caught_before_fake_transport(self):
        self.corrupt=True
        result=self.run_signal()
        self.assertFalse(result.submitted)
        self.assertEqual(self.calls,[])
        with sqlite3.connect(self.tracker.path) as db:
            row=db.execute("SELECT state,reason FROM intents WHERE day='20261030' AND strategy=?",(S3,)).fetchone()
            self.assertEqual(row,('cancelled','not_sent'))

if __name__=='__main__':unittest.main()
