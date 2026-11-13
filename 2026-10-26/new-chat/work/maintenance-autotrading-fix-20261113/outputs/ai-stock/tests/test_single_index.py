"""Risk factor research is offline and cannot authorize trades."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import math
import unittest
from unittest.mock import patch

from kiwoom_autotrader.research.single_index import fit_single_index, RegressionInputError

CONTEXT = {'asset':'005930','benchmark':'KOSPI','currency':'KRW','frequency':'daily',
           'session':'KRX regular only','return_basis':'comparable adjusted total returns',
           'asset_source':'test observation', 'benchmark_source':'test benchmark',
           'risk_free_source':'test daily rate', 'universe_selection':'fixed beforehand', 'benchmark_role':'market_index'}
START=datetime(2026,1,1,9,tzinfo=timezone.utc)


def fixture(n=30):
    rows=[]
    for i in range(n):
        begin=START+timedelta(days=i)
        end=begin+timedelta(hours=6)
        market=(i%5-2)*0.01
        rf=0.001
        excess=0.002+1.5*market
        rows.append({'period_start':begin.isoformat(),'period_end':end.isoformat(),
            'asset_return':excess+rf, 'market_return':market+rf, 'risk_free_return':rf,
            'asset_available_at':(end+timedelta(minutes=1)).isoformat(),
            'market_available_at':(end+timedelta(minutes=1)).isoformat(),
            'risk_free_available_at':begin.isoformat()})
    return rows


def calculate(rows, cutoff_index=24, **kw):
    train_end=START+timedelta(days=cutoff_index,hours=7)
    as_of=START+timedelta(days=max(len(rows)-1, cutoff_index),hours=8)
    return fit_single_index(rows, as_of=as_of, training_end=train_end, context=CONTEXT, **kw)


class SingleIndexTests(unittest.TestCase):
    def setUp(self):
        network=patch('socket.socket.connect',side_effect=AssertionError('network prohibited'))
        self.network=network.start();self.addCleanup(network.stop)

    def test_known_alpha_beta_split_and_conditional_evaluation(self):
        rows=fixture();data=calculate(rows)
        self.assertEqual(data['counts'],{'input_rows':30,'training_rows':25,'evaluation_rows':5,
                                         'excluded_rows':0,'minimum_training_rows':24})
        self.assertAlmostEqual(data['fit']['alpha_per_period'],0.002,places=12)
        self.assertAlmostEqual(data['fit']['beta'],1.5,places=12)
        self.assertAlmostEqual(data['fit']['r_squared'],1,places=12)
        self.assertLess(data['evaluation']['root_mean_squared_error'],1e-12)
        self.assertIn('not an ex-ante forecast',data['evaluation']['method'])
        self.assertFalse(data['automatic_orders']);self.assertFalse(data['forecast_or_trading_signal'])
        self.assertFalse(data['availability_timestamps_independently_verified'])
        self.network.assert_not_called()

    def test_evaluation_never_contaminates_fit(self):
        rows=fixture();before=calculate(rows)
        for r in rows[25:]: r['asset_return'] += 0.03
        after=calculate(rows)
        self.assertEqual(before['fit'],after['fit'])
        self.assertAlmostEqual(after['evaluation']['root_mean_squared_error'],0.03,places=12)

    def test_missing_values_and_late_knowledge_preserve_denominator(self):
        rows=fixture();rows[0]['risk_free_return']=None
        rows[1]['asset_available_at']=(START+timedelta(days=26)).isoformat()
        rows[26]['market_available_at']=None
        d=calculate(rows,min_observations=3)
        self.assertEqual(d['counts']['input_rows'],30)
        self.assertEqual(d['counts']['training_rows'],23)
        self.assertEqual(d['counts']['evaluation_rows'],4)
        self.assertEqual([x['reason'] for x in d['excluded']],
                         ['missing_or_invalid_return','not_available_at_training_end','availability_missing_or_invalid'])

    def test_market_constant_and_short_samples_are_not_fitted(self):
        rows=fixture(10)
        result=calculate(rows,cutoff_index=9)
        self.assertEqual(result['fit']['status'],'insufficient')
        self.assertEqual(result['evaluation']['status'],'not_evaluated')
        rows=fixture(30)
        for r in rows:r['market_return']=r['risk_free_return']
        result=calculate(rows)
        self.assertEqual(result['fit']['reason'],'market_excess_return_zero_variance')
        self.assertEqual(result['evaluation']['status'],'not_evaluated')

    def test_temporal_boundary_duplicate_and_future_availability(self):
        rows=fixture()
        rows[1]['period_start']=rows[0]['period_start']
        with self.assertRaises(RegressionInputError):calculate(rows)
        rows=fixture();rows[0]['asset_available_at']=rows[0]['period_start']
        d=calculate(rows,min_observations=3)
        self.assertEqual(d['excluded'][0]['reason'],'asset_or_market_available_before_period_end')
        rows=fixture();cutoff=START+timedelta(days=24,hours=2)
        d=fit_single_index(rows, as_of=START+timedelta(days=31), training_end=cutoff,
                           context=CONTEXT,min_observations=3)
        self.assertIn('period_crosses_split_boundary',[e['reason'] for e in d['excluded']])

    def test_malformed_inputs_are_never_imputed(self):
        for bad in (float('nan'),float('inf'),True,'x',-1.01):
            rows=fixture();rows[0]['asset_return']=bad
            result=calculate(rows,min_observations=3)
            self.assertEqual(result['counts']['excluded_rows'],1)
            self.assertNotIn('p_values', result['evaluation'])
        with self.assertRaises(RegressionInputError):calculate(fixture(),min_observations=True)
        with self.assertRaises(RegressionInputError):fit_single_index(fixture(),as_of='2026-01-01',training_end='2026-01-01',context=CONTEXT)
        bad=deepcopy(CONTEXT);del bad['risk_free_source']
        with self.assertRaises(RegressionInputError):fit_single_index(fixture(),as_of=START,training_end=START,context=bad)

    def test_late_batch_is_retrospective_and_proxy_is_not_market_beta(self):
        rows=fixture(); batch_time=(START+timedelta(days=31)).isoformat()
        for r in rows:
            r['asset_available_at']=batch_time; r['market_available_at']=batch_time
            r['risk_free_return']=0; r['risk_free_available_at']=batch_time
        context={**CONTEXT, 'benchmark':'equal-weight peers', 'benchmark_role':'peer_proxy',
                 'risk_free_source':'assumed_zero_not_observed',
                 'universe_selection':'retrospectively selected'}
        cutoff=START+timedelta(days=24,hours=7);asof=START+timedelta(days=32)
        forward=fit_single_index(rows,as_of=asof,training_end=cutoff,context=context)
        self.assertEqual(forward['fit']['status'],'insufficient')
        self.assertEqual(forward['counts']['training_rows'],0)
        study=fit_single_index(rows,as_of=asof,training_end=cutoff,context=context,
                               analysis_mode='retrospective_descriptive')
        self.assertEqual(study['fit']['status'],'fitted')
        self.assertEqual(study['evaluation']['status'],'retrospective_descriptive')
        self.assertTrue(study['retrospective']);self.assertTrue(study['risk_free_assumed_zero'])
        self.assertFalse(study['benchmark_is_market_index'])
        self.assertFalse(study['out_of_sample_performance_verified'])
        self.assertTrue(any('not market beta' in line for line in study['limitations']))
        rows[0]['risk_free_return']=0.01
        study=fit_single_index(rows,as_of=asof,training_end=cutoff,context=context,
                               analysis_mode='retrospective_descriptive')
        self.assertIn('risk_free_assumption_conflicts_with_value',[r['reason'] for r in study['excluded']])

    def test_constant_asset_return_r2_is_not_defined(self):
        rows=fixture()
        for r in rows:r['asset_return']=r['risk_free_return']+0.01
        result=calculate(rows)
        self.assertIsNone(result['fit']['r_squared'])
        self.assertEqual(result['fit']['p_values'],None)
        self.assertEqual(result['fit']['confidence_intervals'],None)


if __name__ == '__main__': unittest.main()
