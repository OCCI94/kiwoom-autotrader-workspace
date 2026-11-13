"""Actual previous-day upper-price cache; no percentage inference or API."""
from copy import deepcopy
from datetime import datetime,timedelta
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch
from zoneinfo import ZoneInfo
from kiwoom_autotrader.trading import paper_experiment_inputs as m

FRIDAY=datetime(2026,10,30,10,tzinfo=ZoneInfo('Asia/Seoul'))
MONDAY=datetime(2026,11,2,10,tzinfo=ZoneInfo('Asia/Seoul'))
SYMBOL='005930'

class MemoryStore:
    def __init__(self):self.rows={}
    def selection(self,day,kind,value=None):
        if value is not None:self.rows.setdefault((day,kind),deepcopy(value))
        return deepcopy(self.rows.get((day,kind)))

def response(now,*,close=100,sign=None):
    dates=[];at=now-timedelta(days=1)
    while len(dates)<21:
        if at.weekday()<5:dates.append(at.strftime('%Y%m%d'))
        at-=timedelta(days=1)
    rows=[{'dt':d,'open_pric':'95','high_pric':str(max(110,close)),'low_pric':'90','cur_prc':str(close),
        'trde_qty':'1000','trde_prica':'3000','upd_stkpc_tp':'','upd_rt':'','pred_pre_sig':sign}
        for d in reversed(dates)]
    return {'symbol':SYMBOL,'rows':rows,'source_sha256':'a'*64,'observed_at':now.isoformat()}

class PriorLimitTests(unittest.TestCase):
    def setUp(self):
        self.now=FRIDAY;self.store=MemoryStore();self.session=SimpleNamespace(mode='paper')
        self.quote=Mock(return_value={'stk_cd':SYMBOL,'cur_prc':'94','open_pric':'90','upl_pric':'100'})
        self.requester=Mock(side_effect=lambda *a,**kw:{'rows':[],'source_sha256':'b'*64,
            'request_started_at':self.now.isoformat(),'observed_at':self.now.isoformat(),'cont_yn':'N','next_key':''})
        p=patch.object(m,'session_identity',return_value=('paper','fixture'));p.start();self.addCleanup(p.stop)
        self.inputs=m.ExperimentInputs(self.session,SimpleNamespace(),self.store,clock=lambda:self.now,
            requester=self.requester,quote_fetcher=self.quote)
    def monday(self,close=100,sign=None):
        self.now=MONDAY;self.inputs._bind()
        self.inputs.daily_cache[SYMBOL]=m.normalize_daily(response(self.now,close=close,sign=sign),SYMBOL,self.now)
        self.quote.return_value={'stk_cd':SYMBOL,'cur_prc':'110','open_pric':'100','upl_pric':'130'}

    def test_actual_friday_upper_limit_and_final_close_work_across_weekend(self):
        first=self.inputs.market(SYMBOL)
        original=self.store.selection('20261030','observed_upper_limit:'+SYMBOL)
        self.assertEqual(original['upper_limit_price'],100);self.assertEqual(first['upper_limit_observation_status'],'actual_upper_limit_preserved')
        self.assertEqual(original['source_sha256'],m.digest(self.quote.return_value))
        self.monday(close=100)
        value=self.inputs.market(SYMBOL)
        self.assertIs(value['prior_limit_up'],True)
        self.assertEqual(value['prior_limit_up_basis'],'final_prior_close_vs_actual_observed_upper_limit')
        self.assertEqual(value['prior_upper_limit_evidence_sha256'],original['evidence_sha256'])
        self.assertEqual(original,self.store.selection('20261030','observed_upper_limit:'+SYMBOL))
        self.assertEqual(self.quote.call_count,2);self.assertEqual(self.requester.call_count,2)

    def test_lower_final_close_proves_false_but_missing_prior_observation_stays_none(self):
        self.inputs.market(SYMBOL);self.monday(close=99)
        self.assertIs(self.inputs.market(SYMBOL)['prior_limit_up'],False)
        self.store.rows.pop(('20261030','observed_upper_limit:'+SYMBOL))
        value=self.inputs.market(SYMBOL,force=True)
        self.assertIsNone(value['prior_limit_up']);self.assertEqual(value['prior_limit_up_basis'],'first_day_prior_upper_limit_unobserved')

    def test_actual_daily_sign_kept_when_explicit_and_no_prior_quote_exists(self):
        self.monday(sign='2');self.assertIs(self.inputs.market(SYMBOL)['prior_limit_up'],False)
        self.assertEqual(self.inputs.market(SYMBOL)['prior_limit_up_basis'],'explicit_ka10081_daily_sign')

    def test_different_observed_uppers_are_both_preserved_and_never_used(self):
        self.inputs.market(SYMBOL);original=self.store.selection('20261030','observed_upper_limit:'+SYMBOL)
        self.quote.return_value={**self.quote.return_value,'upl_pric':'101'}
        value=self.inputs.market(SYMBOL,force=True)
        self.assertEqual(value['upper_limit_observation_status'],'conflicting_daily_upper_limits')
        conflict=self.store.selection('20261030','observed_upper_limit_conflict:'+SYMBOL)
        self.assertEqual(conflict['original'],original);self.assertEqual(conflict['conflicting']['upper_limit_price'],101)
        self.monday();value=self.inputs.market(SYMBOL)
        self.assertIsNone(value['prior_limit_up']);self.assertEqual(value['prior_limit_up_basis'],'prior_upper_limit_conflicting')

    def test_daily_sign_disagreement_and_above_limit_close_are_unverified(self):
        self.inputs.market(SYMBOL);self.monday(close=100,sign='2')
        value=self.inputs.market(SYMBOL);self.assertIsNone(value['prior_limit_up'])
        self.assertEqual(value['prior_limit_up_basis'],'daily_sign_and_upper_limit_conflict')
        self.monday(close=101);value=self.inputs.market(SYMBOL,force=True)
        self.assertIsNone(value['prior_limit_up']);self.assertEqual(value['prior_limit_up_basis'],'prior_close_above_observed_upper_limit')

    def test_hash_currency_basis_day_and_future_data_cannot_supply_fallback(self):
        self.inputs.market(SYMBOL);self.monday()
        original=deepcopy(self.inputs.daily_cache[SYMBOL]);saved=deepcopy(self.store.rows)
        for change in ('hash','currency','basis','day','future','rows'):
            self.inputs.daily_cache[SYMBOL]=deepcopy(original);self.store.rows=deepcopy(saved)
            daily=self.inputs.daily_cache[SYMBOL]
            if change=='hash':self.store.rows[('20261030','observed_upper_limit:'+SYMBOL)]['source_sha256']='x'*64
            if change=='currency':daily['currency']='USD'
            if change=='basis':daily['price_basis']='adjusted'
            if change=='day':daily['session_day']='20261030'
            if change=='future':daily['available_at']=(MONDAY+timedelta(seconds=1)).isoformat()
            if change=='rows':daily['rows'][-1]['close']=99
            with self.subTest(change=change):self.assertIsNone(self.inputs.market(SYMBOL,force=True)['prior_limit_up'])

    def test_preopen_upper_value_is_not_asserted_as_today_and_corporate_action_rejected(self):
        self.now=FRIDAY.replace(hour=8,minute=59)
        value=self.inputs.market(SYMBOL)
        self.assertEqual(value['upper_limit_observation_status'],'current_session_upper_limit_unobserved')
        self.assertIsNone(self.store.selection('20261030','observed_upper_limit:'+SYMBOL))
        self.monday();self.assertIsNone(self.inputs.market(SYMBOL)['prior_limit_up'])
        for key,val in [('upd_stkpc_tp','8'),('upd_rt','0.00')]:
            raw=response(MONDAY);raw['rows'][-1][key]=val
            with self.assertRaisesRegex(ValueError,'corporate_action'):m.normalize_daily(raw,SYMBOL,MONDAY)


if __name__=='__main__':unittest.main()
