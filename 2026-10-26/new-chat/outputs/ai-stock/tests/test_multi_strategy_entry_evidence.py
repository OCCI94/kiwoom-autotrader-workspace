"""Exercise the real evidence freezer and mock only the final order handler."""
import unittest
from decimal import Decimal
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch
import tests.test_multi_strategy as fixtures
from kiwoom_autotrader.trading.multi_strategy import EntryState, S3, execute_paper_strategy
from kiwoom_autotrader.trading.order_context import _current

class EntryEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixtures.MultiStrategyTests('test_s3_requires_live_20_percent_cross_and_real_limit')
        self.fixture.setUp()
        self.now=self.fixture.now
        self.tracker=SimpleNamespace(multi_strategy_position=lambda *a:None,
           multi_strategy_open_symbols=lambda *a:None,assert_multi_exit_dispatch=lambda *a:None)
        self.market={'symbol':'005930','request_started_at':self.now.isoformat(),
           'observed_at':self.now.isoformat(),'available_at':self.now.isoformat(),
           'source':'ka10001_normalized_response','source_sha256':'a'*64,'source_market_timestamp':None,
           'source_freshness_verified':False,'current_price':100,'day_high':110,'upper_limit_price':130,
           'change_pct':'20.1','rising_volume_surge':None,'surge_evidence':None,
           'theme_evidence':None,'dart_evidence':None}
        self.base=dict(session=SimpleNamespace(mode='paper'),tracker=self.tracker,strategy=S3,
           symbol='005930',side='buy',reason='observed_20pct_crossing',quantity=None,budget=1000000,
           bars=self.fixture.bars,market=self.market,baseline=EntryState(self.fixture.bars[-1].start-timedelta(minutes=1),99,False,Decimal('19.9')),
           observed_at=self.now,before_send=lambda:None,run_id='test',source_hash='a'*64)
    def test_real_freezer_accepts_actual_flat_and_candidate_shape(self):
        outcomes=[]
        def handler(command,session,before_send=None):
            before_send()
            outcomes.append((command,_current.get().evidence.as_dict()))
            return []
        with patch('kiwoom_autotrader.telegram.commands.orders.handle_buy',side_effect=handler):
            for status in ('flat','flat_candidate'):
                position={'status':status,'generation_id':None,'owned_qty':None,'basis_krw':None,
                          'initial_fill_at':None,'half_take_done':None,
                          'candidate_only':status=='flat_candidate','requires_fresh_broker_reconciliation':True}
                result=execute_paper_strategy(**self.base,position=position)
                self.assertEqual(result,[])
        self.assertEqual(len(outcomes),2)
        for command,evidence in outcomes:
            self.assertEqual(command,'buy 005930 max 1000000')
            pos=evidence['multi_strategy_signal']['position']
            self.assertFalse(pos['half_take_done'])
            self.assertIsNone(pos['generation_id'])
            self.assertIsNone(pos['owned_qty'])
    def test_sell_unknown_half_status_is_not_invented(self):
        args=dict(self.base,side='sell',reason='stop_loss_or_sma20',quantity=10,budget=None,
                  position={'generation_id':'a'*32,'owned_qty':10,'basis_krw':1000,'half_take_done':None})
        with patch('kiwoom_autotrader.telegram.commands.orders.handle_sell') as handler:
            result=execute_paper_strategy(**args)
            self.assertEqual(result.decision_reason,'strategy_evidence_or_context_unavailable')
            handler.assert_not_called()

if __name__=='__main__':unittest.main()
