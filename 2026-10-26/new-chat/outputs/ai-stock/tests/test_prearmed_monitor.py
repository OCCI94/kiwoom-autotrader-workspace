"""Offline monitor integration: cache never replaces dispatch protection."""
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from kiwoom_autotrader.trading.multi_strategy import MultiStrategyMonitor, EntryState, S5
from kiwoom_autotrader.trading.steady_uptrend_vwap import evaluate_steady_uptrend_vwap
from tests.test_steady_uptrend_vwap import fixture


class Pipeline:
    def __init__(self, fail=False): self.observations=[]; self.resets=0; self.fail=fail
    def observe(self, **values):
        if self.fail: raise ValueError('advisory_unavailable')
        self.observations.append(values)
        return {'order_permission':False}
    def reset(self): self.resets+=1
    def status(self): return {'observations':len(self.observations),'order_permission':False}


class PrearmedMonitorTests(unittest.TestCase):
    def setup_monitor(self, enabled=True, fail_pipeline=False):
        now,bars,daily,risk=fixture();clock=[now-timedelta(minutes=1)]
        settings={'paper_multi_strategy_enabled':True,'paper_multi_strategy_ids':[S5],
            'paper_fast_decision_enabled':enabled,
            'gdcrs_stocks':[{'code':'005930','budget':1000000}],
            'gdcrs_initial_entry_pct':50,'gdcrs_add_on_profit_bps':50,'gdcrs_max_daily_entries_per_symbol':2}
        class Provider:
            def __init__(self): self.generation=risk['file_sha256'];self.closed=0
            def fetch_s5_inputs(self,*a,**kw): return {'s5_daily_context':daily,'s5_risk_context':risk}
            def fetch(self,symbol,as_of=None): return {'symbol':symbol,'current_price':10220,'day_high':10220,'change_pct':'5'}
            def is_fresh(self,*args): return True
            def s5_risk_generation(self): return self.generation
            def close_prearmed(self): self.closed+=1
        provider=Provider()
        tracker=SimpleNamespace(multi_strategy_position=lambda *a:{'status':'flat'},
            multi_strategy_open_symbols=lambda *a:{'status':'available','symbols':[]})
        session=SimpleNamespace(paper_automation_reconcile=SimpleNamespace(_tracker=tracker))
        calls=[]
        def chart(_session,symbol,count):
            return [replace(b,source_query_started_at=clock[0]) for b in bars if b.start+timedelta(minutes=1)<=clock[0]]
        def execute(**kw):
            kw['before_send']();calls.append(kw);return []
        pipeline=Pipeline(fail_pipeline)
        monitor=MultiStrategyMonitor(settings_loader=lambda:settings,chart_fetcher=chart,
            provider_factory=lambda _:provider,now=lambda:clock[0],executor=execute,
            cooldown_check=lambda *a:True,fast_pipeline=pipeline)
        return now,clock,monitor,session,calls,pipeline,provider

    def run_two(self, enabled=True, fail_pipeline=False):
        now,clock,monitor,session,calls,pipeline,provider=self.setup_monitor(enabled,fail_pipeline)
        with patch('kiwoom_autotrader.trading.multi_strategy.session_identity',return_value=('paper','not-exported-token')):
            with patch('kiwoom_autotrader.trading.multi_strategy.evaluate_steady_uptrend_vwap',
                       wraps=evaluate_steady_uptrend_vwap) as evaluated:
                monitor.tick(session,lambda _:None)
                self.assertEqual(calls,[],monitor.status())
                clock[0]=now;monitor.tick(session,lambda _:None)
                count=evaluated.call_count
        return monitor,calls,pipeline,provider,count

    def test_prearm_reuses_exact_evaluation_but_preserves_all_dispatch_gates(self):
        monitor,calls,pipeline,provider,count=self.run_two()
        self.assertEqual(len(calls),1,monitor.status())
        self.assertEqual(count,2)  # Once per observation, not twice at signal.
        self.assertEqual(calls[0]['budget'],500000)
        self.assertEqual(calls[0]['strategy'],S5)
        row=monitor.status()['prearmed_decisions']['005930']
        self.assertEqual(row['cache_reason'],'advisory_snapshot_current')
        self.assertEqual(row['estimated_initial_qty_upper_bound'],48)
        self.assertIsNone(row['theme_remaining_slots'])
        self.assertFalse(row['cash_and_reservations_verified'])
        self.assertFalse(row['ledger_index_authoritative'])
        self.assertFalse(row['order_permission'])
        self.assertTrue(row['requires_fresh_reconciliation'])
        self.assertGreaterEqual(row['lookup_ms'],0)
        self.assertEqual(len(pipeline.observations),2)
        self.assertNotIn('not-exported-token',repr(pipeline.observations))
        # Source revocation after preparation remains a hard dispatch veto.
        provider.generation='f'*64
        with patch('kiwoom_autotrader.trading.multi_strategy.session_identity',return_value=('paper','not-exported-token')):
            with self.assertRaisesRegex(ValueError,'s5_risk_evidence_changed'):
                calls[0]['before_send']()

    def test_default_off_preserves_old_path_and_has_no_async_observations(self):
        monitor,calls,pipeline,_,count=self.run_two(enabled=False)
        self.assertEqual(len(calls),1)
        self.assertEqual(count,2)
        self.assertEqual(pipeline.observations,[])
        self.assertEqual(monitor.status()['prearmed_decisions'],{})

    def test_advisory_failure_falls_back_without_creating_new_permission(self):
        monitor,calls,_,_,count=self.run_two(fail_pipeline=True)
        self.assertEqual(len(calls),1,monitor.status())
        self.assertEqual(count,2)
        row=monitor.status()['prearmed_decisions']['005930']
        self.assertEqual(row['stage'],'deterministic_fallback')
        self.assertFalse(row['order_permission'])
        self.assertEqual(monitor.status()['prearmed_cache']['entries'],0)

    def test_reset_clears_tickets_and_closes_background_risk_reader(self):
        monitor,_,pipeline,provider,_=self.run_two()
        monitor.reset()
        self.assertEqual(provider.closed,1)
        self.assertEqual(monitor.status()['prearmed_cache']['entries'],0)
        self.assertEqual(monitor.status()['prearmed_decisions'],{})
        self.assertGreaterEqual(pipeline.resets,2)

    def test_cached_complete_session_eligibility_accepts_first_observation(self):
        now,bars,daily,risk=fixture();market={'symbol':'005930','current_price':10220,
            's5_daily_context':daily,'s5_risk_context':risk}
        evaluation=evaluate_steady_uptrend_vwap(bars,symbol='005930',now=now,current_price=10220,
            daily_context=daily,risk_context=risk,require_short=True)
        monitor=MultiStrategyMonitor(fast_pipeline=Pipeline())
        version=monitor._prepare_s5(session=object(),symbol='005930',bars=bars,market=market,
            evaluation=evaluation,observed=now,settings={},positions=[(S5,{'status':'flat'})],budget=500000)
        self.assertIsNotNone(version)
        self.assertEqual(monitor._s5_prearmed_signal(EntryState(),bars,market,symbol='005930',now=now,version=version),(True,'steady_uptrend_vwap'))
        self.assertEqual(monitor._s5_prearmed_signal(EntryState(),bars,market,symbol='005930',now=now+timedelta(seconds=6),version=version),(False,None))


if __name__=='__main__': unittest.main()
