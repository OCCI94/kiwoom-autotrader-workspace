from datetime import datetime,timedelta
import threading
import time
import unittest
from unittest.mock import patch
from kiwoom_autotrader.trading.fast_decision_pipeline import FastDecisionCoordinator,numeric_state,estimated_order_ticket

NOW=datetime.fromisoformat('2026-11-02T09:35:00+09:00')

class MissingClient:
    available=False
    def close(self):pass

class PipelineTests(unittest.TestCase):
    def test_ticket_never_spends_uncertain_reservations_or_more_than_cash(self):
        row={'status':'observed_advisory','reserved_principal_krw':0,
             'available_cash_krw':210000,'remaining_principal_krw':900000,'observed_held_qty':0}
        result=estimated_order_ticket(row,10000,500000)
        self.assertEqual(result['estimated_initial_qty'],21)
        self.assertFalse(result['order_authorized'])
        row['reserved_principal_krw']=None
        self.assertIsNone(estimated_order_ticket(row,10000,500000)['estimated_initial_qty'])
        row['reserved_principal_krw']=0;row['observed_held_qty']=10
        self.assertIsNone(estimated_order_ticket(row,10000,500000)['estimated_initial_qty'])
    def test_numeric_missing_stays_null_and_observed_fields_bound(self):
        row=numeric_state({'current_price':10100},{'vwap':{'hlc3_volume_numerator':30000,'volume':1},
            'short_selling':{'max_daily_short_amount_share':'0.05'}})
        self.assertEqual(row['price_vwap_bps'],100)
        self.assertEqual(row['short_amount_share'],.05)
        self.assertIsNone(row['spread_bps']);self.assertIsNone(row['catalyst_direction'])
        self.assertIsNone(row['five_minute_slope_bps'])
    def test_background_isolation_cache_miss_no_model_wait(self):
        waiting=threading.Event();release=threading.Event()
        def client_factory():waiting.set();release.wait(2);return MissingClient()
        p=FastDecisionCoordinator(client_factory=client_factory,now=lambda:NOW)
        self.addCleanup(p.reset);self.addCleanup(release.set)
        p.start('session-a');self.assertTrue(waiting.wait(1))
        # The model constructor is deliberately blocked; the local path returns.
        result=p.observe(market={'current_price':10000},evaluation={},symbol='042510',
            observed=NOW,session_id='session-a',budget=500000)
        self.assertEqual(result['source'],'deterministic_fallback')
        self.assertIsNone(result['probabilities']);self.assertFalse(result['order_authority'])
        self.assertTrue(waiting.is_set());self.assertFalse(release.is_set())
    def test_session_reset_never_uses_old_background_or_account(self):
        p=FastDecisionCoordinator(client_factory=MissingClient,now=lambda:NOW)
        self.addCleanup(p.reset)
        p._session='new';p._account_snapshot=('new',{'042510':{'prepared_at':NOW.isoformat(),'expires_at':(NOW+timedelta(seconds=2)).isoformat()}})
        self.assertIsNotNone(p.account_for('042510',NOW))
        self.assertIsNone(p.account_for('042510',NOW+timedelta(seconds=3)))
        p._session='changed';self.assertIsNone(p.account_for('042510',NOW))
        p.reset();self.assertIsNone(p.account_for('042510',NOW))
    def test_untrusted_numeric_values_do_not_create_ai_score(self):
        row=numeric_state({'current_price':'not-price','spread_bps':float('nan')},{})
        self.assertTrue(all(v is None for v in row.values()))

if __name__=='__main__':unittest.main()
