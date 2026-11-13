import unittest
from dataclasses import replace
from datetime import datetime, timedelta
from copy import deepcopy
from zoneinfo import ZoneInfo
from kiwoom_autotrader.api.minute_chart import MinuteBar
from kiwoom_autotrader.trading.steady_uptrend_vwap import S5, evaluate_steady_uptrend_vwap, validate_s5_risk, timed_exit
from kiwoom_autotrader.trading.multi_strategy import EntryState, entry_signal, exit_signal
from kiwoom_autotrader.config.settings import effective_multi_strategies

KST=ZoneInfo('Asia/Seoul')

def fixture():
    now=datetime(2026,11,2,9,35,3,tzinfo=KST)
    bars=[]
    for i in range(35):
        p=10000+i*6
        bars.append(MinuteBar(now.replace(hour=9,minute=0,second=0)+timedelta(minutes=i),p+6,
            100+(i//5)*10,source_symbol='005930',source_mode='paper',source_query_started_at=now,
            source_response_sha256='a'*64,open_price=p,high_price=p+7,low_price=p-1))
    dates=[];day=now.date()-timedelta(days=1)
    while len(dates)<20:
        if day.weekday()<5: dates.append(day)
        day-=timedelta(days=1)
    daily={'schema':'s5-prior-daily-v1','symbol':'005930','mode':'paper','session_day':'20261102',
        'request_started_at':now.isoformat(),'available_at':now.isoformat(),'source_api':'ka10081',
        'source_sha256':'b'*64,'rows':[{'date':d.strftime('%Y%m%d'),'open':9500+i*10,
        'close':9520+i*10,'high':9800+i*10,'low':9300+i*10,'volume':1000} for i,d in enumerate(reversed(dates))]}
    risk={'schema':'s5-dart-risk-v1','symbol':'005930','reviewed_at':now.isoformat(),
        'expires_at':(now+timedelta(hours=8)).isoformat(),'coverage_complete':True,
        'unresolved_major_risk':False,'source_sha256':'c'*64,'file_sha256':'d'*64}
    from tests.s5_short_fixture import short_evidence
    risk['short_selling_evidence']=short_evidence('005930',daily,now)
    return now,bars,daily,risk

class SteadyTests(unittest.TestCase):
    def setUp(self): self.now,self.bars,self.daily,self.risk=fixture()
    def evaluate(self,**changes):
        values={'bars':self.bars,'symbol':'005930','now':self.now,'current_price':10220,
                'daily_context':self.daily,'risk_context':self.risk};values.update(changes)
        return evaluate_steady_uptrend_vwap(**values)
    def test_complete_formula_and_exact_vwap(self):
        result=self.evaluate();self.assertTrue(result['eligible'],result)
        self.assertEqual(result['session_minute_count'],35)
        self.assertEqual(result['vwap']['hlc3_volume_numerator'],sum((b.high_price+b.low_price+b.close)*b.volume for b in self.bars))
        self.assertEqual(result['signal_at'],'2026-11-02T09:35:00+09:00')
    def test_partial_bucket_is_evaluated_from_observations_without_synthetic_minutes(self):
        for bars in (self.bars[1:],self.bars[:-1],self.bars[:10]+self.bars[11:]):
            result=self.evaluate(bars=bars)
            self.assertNotEqual(result['status'],'DORMANT',result)
            self.assertEqual(result['session_coverage']['missing_count'],1)
            self.assertFalse(result['session_coverage']['session_complete'])
            self.assertFalse(result['session_coverage']['synthetic_bars_created'])
            self.assertEqual(result['pattern_five_minute_count'],7)
    def test_future_previous_day_and_synthetic_provenance(self):
        for field,value in [('source_query_started_at',self.now+timedelta(seconds=1)),('source_mode','real'),
                            ('gap_proof',object()),('adjustment','1'),('source_symbol','000000')]:
            bars=list(self.bars);bars[0]=replace(bars[0],**{field:value})
            self.assertFalse(self.evaluate(bars=bars)['eligible'])
        bars=list(self.bars);bars[0]=replace(bars[0],start=bars[0].start-timedelta(days=1))
        result=self.evaluate(bars=bars)
        self.assertNotEqual(result['status'],'DORMANT',result)
        self.assertEqual(result['session_minute_count'],34)
        self.assertFalse(result['session_coverage']['session_complete'])
    def test_daily_gate_and_future_daily_fail_closed(self):
        daily=deepcopy(self.daily);daily['rows'][-1]['date']='20261102'
        self.assertEqual(self.evaluate(daily_context=daily)['reason'],'prior_daily_unavailable')
        daily=deepcopy(self.daily)
        for row in daily['rows']:row.update(open=9600,close=9600)
        self.assertFalse(self.evaluate(daily_context=daily)['checks']['prior_daily_uptrend'])
    def test_vwap_current_price_must_be_strictly_above(self):
        self.assertFalse(self.evaluate(current_price=9000)['eligible'])
        bars=[replace(b,volume=0) for b in self.bars]
        self.assertFalse(self.evaluate(bars=bars)['eligible'])
    def test_adr_and_risk_current_time(self):
        self.assertEqual(self.evaluate(current_price=30000)['reason'],'adr_overheated_or_unavailable')
        for changes in ({'coverage_complete':False},{'unresolved_major_risk':True},
                        {'expires_at':self.now.isoformat()},{'reviewed_at':(self.now+timedelta(seconds=1)).isoformat()}):
            risk={**self.risk,**changes};self.assertFalse(self.evaluate(risk_context=risk)['eligible'])
        self.assertFalse(self.evaluate(risk_context=None)['eligible'])
        risk=validate_s5_risk(current_price=10220,daily_context=self.daily,risk_context=self.risk,symbol='005930',now=self.now)
        self.assertTrue(risk['eligible'])
    def test_current_boundary_accepts_first_visit_or_old_baseline_without_replaying_old_boundary(self):
        market={'symbol':'005930','current_price':10220,'s5_daily_context':self.daily,'s5_risk_context':self.risk}
        # Pure S5 evaluation uses complete, freshly fetched session history.
        # The monitor and durable intent claim enforce boundary deduplication;
        # a prior short poll is deliberately not an eligibility prerequisite.
        for state in (EntryState(),EntryState(self.bars[-1].start),EntryState(self.bars[-1].start-timedelta(minutes=6))):
            self.assertEqual(entry_signal(S5,state,self.bars,market,symbol='005930',now=self.now),'steady_uptrend_vwap')
            later=self.now+timedelta(minutes=5)
            stale_prefix=[replace(b,source_query_started_at=later) for b in self.bars]
            self.assertIsNone(entry_signal(S5,state,stale_prefix,market,symbol='005930',now=later))
    def test_latest_completed_five_minute_boundary_evaluated_at_1_to_4_minutes(self):
        # Each request is fresh; bars from the not-yet-complete bucket cannot
        # alter the 09:35 pattern or VWAP, even with extreme valid prices.
        expected=self.evaluate()
        for minutes in range(1,5):
            now=self.now+timedelta(minutes=minutes)
            bars=[replace(b,source_query_started_at=now) for b in self.bars]
            for i in range(minutes):
                bars.append(MinuteBar(self.now.replace(second=0)+timedelta(minutes=i),50000,
                    100,source_symbol='005930',source_mode='paper',source_query_started_at=now,
                    source_response_sha256='a'*64,open_price=50000,high_price=50000,low_price=50000))
            result=self.evaluate(now=now,bars=bars)
            self.assertEqual(result['signal_at'],expected['signal_at'])
            self.assertEqual(result['checks'],expected['checks'])
            self.assertEqual(result['vwap'],expected['vwap'])
            self.assertTrue(result['eligible'],result)
        self.assertEqual(self.evaluate(now=self.now.replace(hour=9,minute=30))['reason'],'five_minute_warmup')

    def test_new_boundary_never_replays_preceding_boundary(self):
        now=self.now+timedelta(minutes=5)
        bars=[replace(b,source_query_started_at=now) for b in self.bars]
        self.assertEqual(self.evaluate(now=now,bars=bars)['reason'],'incomplete_session_minutes')

    def test_delayed_boundary_still_requires_fresh_valid_contiguous_minutes(self):
        now=self.now+timedelta(minutes=4)
        fresh=[replace(b,source_query_started_at=now) for b in self.bars]
        self.assertFalse(self.evaluate(now=now,bars=fresh[:-5])['eligible'])
        self.assertFalse(self.evaluate(now=now,bars=self.bars)['eligible'])
        future=replace(fresh[-1],start=now.replace(second=0)+timedelta(minutes=1))
        self.assertEqual(self.evaluate(now=now,bars=fresh+[future])['reason'],'future_session_minute')
        self.assertFalse(self.evaluate(now=now,bars=fresh,current_price=9000)['eligible'])
    def test_source_default_off_and_always_real_off(self):
        self.assertEqual(effective_multi_strategies({},'paper'),())
        configured={'paper_multi_strategy_enabled':True,'paper_multi_strategy_ids':[S5]}
        self.assertEqual(effective_multi_strategies(configured,'paper'),(S5,))
        self.assertEqual(effective_multi_strategies(configured,'real'),())
    def test_first_actual_fill_time_and_partial_last_fill_does_not_reset(self):
        position={'status':'owned','owned_qty':10,'first_fill_at':self.now.isoformat(),
            'first_fill_observed_at':(self.now+timedelta(seconds=1)).isoformat(),'first_fill_timing_verified':True,
            'initial_fill_at':(self.now+timedelta(minutes=29)).isoformat()}
        self.assertIsNone(timed_exit(position,self.now+timedelta(minutes=30)-timedelta(microseconds=1)))
        self.assertEqual(timed_exit(position,self.now+timedelta(minutes=30)),('time_stop_30m',10))
        position['first_fill_timing_verified']=False
        self.assertIsNone(timed_exit(position,self.now+timedelta(minutes=30)))
    def test_cutoff_does_not_require_late_chart_or_fill_timestamp(self):
        p={'status':'owned','owned_qty':10}
        self.assertEqual(timed_exit(p,self.now.replace(hour=15,minute=19)),('s5_regular_cutoff_exit',10))
        self.assertIsNone(timed_exit(p,self.now.replace(hour=15,minute=18)))
    def test_existing_price_and_trend_protections_stay(self):
        self.assertEqual(exit_signal(S5,qty=10,basis_krw=10000,price=970,sma20=None),('stop_loss_or_sma20',10))
        self.assertEqual(exit_signal(S5,qty=10,basis_krw=10000,price=1100,sma20=None),('take_profit_10pct',10))
        self.assertEqual(exit_signal(S5,qty=10,basis_krw=10000,price=1000,sma20=1001),('stop_loss_or_sma20',10))

class S5BoundedOldMissingTests(unittest.TestCase):
    def setUp(self):
        initial,pattern,self.daily,self.risk=fixture()
        self.now=initial+timedelta(minutes=10)
        start=initial.replace(hour=9,minute=0,second=0,microsecond=0)
        self.prefix=[replace(pattern[0],start=start+timedelta(minutes=i),source_query_started_at=self.now,
                     open_price=9900,high_price=9901,low_price=9899,close=9900,volume=100) for i in range(10)]
        self.bars=self.prefix+[replace(b,start=b.start+timedelta(minutes=10),source_query_started_at=self.now) for b in pattern]
    def evaluate(self,bars=None,**changes):
        values={'bars':self.bars if bars is None else bars,'symbol':'005930','now':self.now,'current_price':10220,
                'daily_context':self.daily,'risk_context':self.risk};values.update(changes)
        return evaluate_steady_uptrend_vwap(**values)
    def test_one_or_two_old_minutes_recover_observed_vwap_without_mutating_input(self):
        complete=self.evaluate();self.assertTrue(complete['eligible'],complete)
        for removed in ((0,),(0,9)):
            bars=[b for i,b in enumerate(self.bars) if i not in removed];before=deepcopy(bars)
            result=self.evaluate(bars);self.assertTrue(result['eligible'],result)
            self.assertEqual(bars,before);self.assertEqual(result['checks'],complete['checks'])
            coverage=result['session_coverage']
            self.assertFalse(coverage['session_complete']);self.assertTrue(coverage['coverage_validated'])
            self.assertEqual(coverage['basis'],'observed_minutes');self.assertFalse(coverage['synthetic_bars_created'])
            self.assertEqual(coverage['missing_count'],len(removed));self.assertEqual(coverage['missing_before_pattern_count'],len(removed))
            self.assertEqual(coverage['missing_pattern_minutes'],[]);self.assertEqual(coverage['tolerated_old_missing_count'],len(removed))
            self.assertEqual(coverage['missing_minutes'],[self.bars[i].start.isoformat() for i in removed])
            self.assertEqual(result['session_minute_count'],45-len(removed))
            self.assertEqual(result['pattern_five_minute_count'],7)
            self.assertFalse(result['vwap']['exact_full_session_vwap'])
            self.assertEqual(result['vwap']['hlc3_volume_numerator'],sum((b.high_price+b.low_price+b.close)*b.volume for b in bars))
            prior=[b for b in bars if b.start<self.now.replace(second=0)-timedelta(minutes=5)]
            self.assertEqual(result['vwap']['prior_5m_volume'],sum(b.volume for b in prior))
            self.assertEqual(result['vwap']['prior_5m_numerator'],sum((b.high_price+b.low_price+b.close)*b.volume for b in prior))
    def test_more_than_two_old_gaps_and_partial_recent_bucket_are_evaluated(self):
        for removed in ((0,1,2,3),(10,),(44,),(10,11,12,13)):
            result=self.evaluate([b for i,b in enumerate(self.bars) if i not in removed])
            self.assertNotEqual(result['status'],'DORMANT',result)
            self.assertEqual(result['session_coverage']['missing_count'],len(removed))
            self.assertEqual(result['session_coverage']['pattern_nonempty_positive_volume_buckets'],7)
            self.assertFalse(result['vwap']['exact_full_session_vwap'])
            self.assertEqual(result['pattern_five_minute_count'],7)

    def test_empty_or_zero_volume_pattern_bucket_and_late_opening_anchor_stay_dormant(self):
        empty=[b for i,b in enumerate(self.bars) if i not in range(10,15)]
        zero=[replace(b,volume=0) if 10<=i<15 else b for i,b in enumerate(self.bars)]
        for bars in (empty,zero):
            result=self.evaluate(bars)
            self.assertEqual(result['status'],'DORMANT')
            self.assertEqual(result['reason'],'incomplete_session_minutes')
            self.assertIn('pattern_bucket_without_positive_volume',result['failure_details']['pattern']['reason_codes'])
            self.assertEqual(result['session_coverage']['pattern_nonempty_positive_volume_buckets'],6)
        late=self.evaluate(self.bars[11:])
        self.assertEqual(late['status'],'DORMANT')
        self.assertIn('session_opening_anchor_unavailable',late['failure_details']['vwap']['reason_codes'])

    def test_one_positive_minute_per_bucket_and_0910_inclusive_anchor_suffice(self):
        sparse=[self.bars[i] for i in range(10,45,5)]
        before=deepcopy(sparse)
        result=self.evaluate(sparse)
        self.assertTrue(result['eligible'],result)
        self.assertEqual(sparse,before);self.assertEqual(result['session_minute_count'],7)
        coverage=result['session_coverage']
        self.assertEqual(coverage['first_observed_minute'],'2026-11-02T09:10:00+09:00')
        self.assertTrue(coverage['opening_anchor_valid']);self.assertEqual(coverage['missing_count'],38)
        self.assertEqual(coverage['empty_pattern_buckets'],[])
        self.assertTrue(all(bucket['observed_minutes']==bucket['positive_volume_minutes']==1 for bucket in coverage['pattern_bucket_coverage']))
        self.assertEqual(result['vwap']['volume'],sum(b.volume for b in sparse))
        self.assertFalse(result['vwap']['exact_full_session_vwap'])
        shifted=[replace(sparse[0],start=sparse[0].start+timedelta(minutes=1))]+sparse[1:]
        self.assertEqual(self.evaluate(shifted)['status'],'DORMANT')

    def test_bad_supplied_rows_cannot_hide_behind_old_gap_allowance(self):
        duplicate=self.bars[:1]+[self.bars[0]]+self.bars[1:]
        reversed_rows=[self.bars[1],self.bars[0]]+self.bars[2:]
        malformed=list(self.bars);malformed[0]=object()
        future=self.bars+[replace(self.bars[-1],start=self.now.replace(second=0)+timedelta(minutes=1))]
        invalid=[duplicate,reversed_rows,malformed,future]
        for field,value in [('source_query_started_at',self.now-timedelta(seconds=61)),('high_price',1),
                            ('source_symbol','000000'),('source_mode','real'),('source_response_sha256','bad'),
                            ('gap_proof',object()),('volume',-1),('start',self.bars[0].start+timedelta(seconds=1))]:
            changed=list(self.bars);changed[0]=replace(changed[0],**{field:value});invalid.append(changed)
        for bars in invalid:
            result=self.evaluate(bars);self.assertEqual(result['status'],'DORMANT',result)
            self.assertFalse(result['session_coverage']['coverage_validated'])
            self.assertFalse(result['eligible']);self.assertTrue(result['failure_details']['pattern']['reason_codes'])
    def test_actual_zero_volume_is_observed_and_not_a_missing_minute(self):
        bars=list(self.bars);bars[0]=replace(bars[0],volume=0)
        result=self.evaluate(bars);self.assertTrue(result['eligible'],result)
        self.assertTrue(result['session_coverage']['session_complete']);self.assertEqual(result['session_coverage']['missing_count'],0)
        self.assertTrue(result['vwap']['exact_full_session_vwap'])
    def test_coverage_and_failure_groups_survive_daily_risk_and_pattern_failures(self):
        bars=self.bars[1:]
        result=self.evaluate(bars,daily_context=None)
        self.assertEqual(result['reason'],'prior_daily_unavailable')
        self.assertEqual(result['session_coverage']['missing_count'],1)
        self.assertEqual(result['check_groups']['pattern']['status'],'pass')
        self.assertEqual(result['check_groups']['vwap']['status'],'pass')
        self.assertEqual(result['check_groups']['prior_daily']['status'],'not_evaluated')
        self.assertIn('prior_daily_unavailable',result['failure_details']['prior_daily']['reason_codes'])
        result=self.evaluate(bars,risk_context=None)
        self.assertEqual(result['check_groups']['risk']['status'],'fail')
        self.assertIn('dart_risk_review_clear',result['failed_checks'])
        result=self.evaluate(bars,current_price=9000)
        self.assertIn('current_price_above_vwap',result['failure_details']['vwap']['failed_checks'])
        self.assertEqual(result['check_groups']['pattern']['status'],'pass')
        for kwargs in ({'now':None},{'now':self.now.replace(hour=8)},{'now':self.now.replace(minute=20)}, {'bars':[]}):
            result=self.evaluate(**kwargs)
            self.assertEqual(set(result['check_groups']),{'pattern','vwap','prior_daily','risk'})
            self.assertEqual(set(result['failure_details']),{'pattern','vwap','prior_daily','risk'})
            self.assertIn('session_coverage',result)

class S5ProviderTests(unittest.TestCase):
    def setUp(self):
        from kiwoom_autotrader.api.session import BrokerSession,IssuedToken
        from kiwoom_autotrader.trading.paper_strategy_context import PaperStrategyContextProvider
        import tempfile,json
        from pathlib import Path
        self.now,self.bars,self.daily,self.risk=fixture();self.clock=[self.now]
        self.session=BrokerSession(now=lambda:self.clock[0]);self.session.mode='paper'
        self.session._issued=IssuedToken('synthetic','20261102235959')
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.riskfile=Path(self.tmp.name)/'risk.json'
        payload={'schema':'toss-hybrid-risk-evidence-v1','generated_at':self.now.isoformat(),
            'expires_at':self.risk['expires_at'],'rows':[{k:v for k,v in self.risk.items() if k not in ('schema','file_sha256','expires_at')}]}
        self.riskfile.write_text(json.dumps(payload));self.riskfile.chmod(0o600)
        self.calls=[]
        self.response={'ok':True,'wire_sha256':'a'*64,'body':{'stk_cd':'005930','stk_dt_pole_chart_qry':[
            {'dt':r['date'],'open_pric':str(r['open']),'high_pric':str(r['high']),'low_pric':str(r['low']),
             'cur_prc':str(r['close']),'trde_qty':str(r['volume'])} for r in reversed(self.daily['rows'])]}}
        def fetch(code,day):self.calls.append((code,day));return deepcopy(self.response)
        self.provider=PaperStrategyContextProvider(self.session,now=lambda:self.clock[0],s5_daily_fetcher=fetch,s5_risk_path=self.riskfile)
    def test_cached_prior_daily_and_clear_risk(self):
        first=self.provider.fetch_s5_inputs('005930',self.now)
        self.assertEqual(len(first['s5_daily_context']['rows']),20)
        self.assertTrue(first['s5_risk_context']['coverage_complete'])
        self.clock[0]+=timedelta(minutes=5)
        self.assertEqual(self.provider.fetch_s5_inputs('005930')['s5_daily_context'],first['s5_daily_context'])
        self.assertEqual(len(self.calls),1)
    def test_bad_symbol_and_failure_cached_without_retry(self):
        self.response['body']['stk_cd']='000000'
        self.assertIsNone(self.provider.fetch_s5_inputs('005930')['s5_daily_context'])
        self.response['body']['stk_cd']='005930'
        self.assertIsNone(self.provider.fetch_s5_inputs('005930')['s5_daily_context'])
        self.assertEqual(len(self.calls),1)
    def test_current_daily_excluded_and_duplicate_rejected(self):
        self.response['body']['stk_dt_pole_chart_qry'].insert(0,{'dt':'20261102'})
        self.assertEqual(len(self.provider.fetch_s5_inputs('005930')['s5_daily_context']['rows']),20)
    def test_old_adjustment_ignored_but_latest_twenty_adjustment_blocks(self):
        self.response['body']['stk_dt_pole_chart_qry'].append({'dt':'20250102','upd_stkpc_tp':'1'})
        self.assertIsNotNone(self.provider.fetch_s5_inputs('005930')['s5_daily_context'])
        self.provider._s5_daily_cache.clear()
        self.response['body']['stk_dt_pole_chart_qry'][0]['upd_stkpc_tp']='1'
        self.assertIsNone(self.provider.fetch_s5_inputs('005930')['s5_daily_context'])
    def test_daily_api_requests_unadjusted_prices_and_single_page(self):
        from unittest.mock import patch
        with patch.object(self.provider,'_ranking_page',return_value={}) as page:
            self.provider._fetch_s5_daily_page('005930','20261102')
        self.assertEqual(page.call_args.args,('ka10081',{'stk_cd':'005930','base_dt':'20261102','upd_stkpc_tp':'0'},'stk_dt_pole_chart_qry'))
        self.assertEqual(page.call_args.kwargs,{'endpoint':'/api/dostk/chart'})
    def test_session_rotation_clears_cache_and_real_disabled(self):
        from kiwoom_autotrader.api.session import IssuedToken
        self.provider.fetch_s5_inputs('005930');self.session._issued=IssuedToken('rotated','20261102235959')
        self.provider.fetch_s5_inputs('005930');self.assertEqual(len(self.calls),2)
        self.session.mode='real'
        with self.assertRaises(ValueError):self.provider.fetch_s5_inputs('005930')
        self.assertEqual(len(self.calls),2)
    def test_untrusted_or_expired_risk_never_passes(self):
        self.riskfile.chmod(0o666)
        self.assertIsNone(self.provider.fetch_s5_inputs('005930')['s5_risk_context'])
        self.riskfile.chmod(0o600)
        self.clock[0]+=timedelta(days=1)
        self.assertIsNone(self.provider._read_s5_risk('005930',self.clock[0]))

class S5MonitorTests(unittest.TestCase):
    def test_observed_baseline_then_complete_signal_uses_entire_prefix(self):
        from kiwoom_autotrader.trading.multi_strategy import MultiStrategyMonitor
        from types import SimpleNamespace
        from unittest.mock import patch
        now,bars,daily,risk=fixture(); clock=[now-timedelta(minutes=1)]
        settings={'paper_multi_strategy_enabled':True,'paper_multi_strategy_ids':[S5],
            'gdcrs_stocks':[{'code':'005930','budget':1000000}],
            'gdcrs_initial_entry_pct':50,'gdcrs_add_on_profit_bps':50,'gdcrs_max_daily_entries_per_symbol':2}
        class Provider:
            def fetch_s5_inputs(self,*a,**kw):return {'s5_daily_context':daily,'s5_risk_context':risk}
            def fetch(self,symbol,as_of=None):return {'symbol':symbol,'current_price':10220,'day_high':10220,'change_pct':'5'}
            def is_fresh(self,*args):return True
        tracker=SimpleNamespace(multi_strategy_position=lambda *a:{'status':'flat'},
            multi_strategy_open_symbols=lambda *a:{'status':'available','symbols':[]})
        session=SimpleNamespace(paper_automation_reconcile=SimpleNamespace(_tracker=tracker))
        calls=[];counts=[]
        def chart(_session,symbol,count):
            counts.append(count)
            return [replace(b,source_query_started_at=clock[0]) for b in bars if b.start+timedelta(minutes=1)<=clock[0]]
        monitor=MultiStrategyMonitor(settings_loader=lambda:settings,chart_fetcher=chart,
            provider_factory=lambda _:Provider(),now=lambda:clock[0],executor=lambda **kw:calls.append(kw) or [],
            cooldown_check=lambda *a:True)
        with patch('kiwoom_autotrader.trading.multi_strategy.session_identity',return_value=('paper','x')):
            monitor.tick(session,lambda _:None);self.assertEqual(calls,[])
            self.assertEqual(monitor.status()['s5_input_diagnostics']['005930']['reason'],'five_minute_warmup')
            clock[0]=now;monitor.tick(session,lambda _:None)
        self.assertEqual(len(calls),1,monitor.status())
        self.assertEqual(calls[0]['strategy'],S5)
        self.assertEqual(calls[0]['budget'],500000)
        self.assertEqual(len(calls[0]['bars']),35)
        # Each due boundary asks for the full actual session prefix, with
        # a two-row transport margin; no synthetic rows fill that margin.
        self.assertEqual(counts,[36,37])
        diagnostic=monitor.status()['s5_input_diagnostics']['005930']
        self.assertTrue({'stage','status','reason','observed_at','signal_at','session_minute_count','order_permission'} <= set(diagnostic))
        self.assertEqual((diagnostic['status'],diagnostic['session_minute_count']),('PASS',35))
        diagnostic['reason']='tamper'
        self.assertEqual(monitor.status()['s5_input_diagnostics']['005930']['reason'],'steady_uptrend_vwap')
        monitor.reset();self.assertEqual(monitor.status()['s5_input_diagnostics'],{})
    def test_s5_diagnostics_do_not_clear_existing_other_strategy_error(self):
        from kiwoom_autotrader.trading.multi_strategy import MultiStrategyMonitor
        now,*_=fixture();monitor=MultiStrategyMonitor(now=lambda:now)
        monitor._symbol_errors['005930']='sma20_unavailable'
        monitor._record_s5_input('005930',{'status':'DORMANT','reason':'dart_risk_review_unavailable'},now)
        status=monitor.status()
        self.assertEqual(status['symbol_errors']['005930'],'sma20_unavailable')
        self.assertEqual(status['s5_input_diagnostics']['005930']['reason'],'dart_risk_review_unavailable')
        self.assertFalse(status['s5_input_diagnostics']['005930']['order_permission'])
    def test_timed_exit_precedes_chart_and_dart_input(self):
        from kiwoom_autotrader.trading.multi_strategy import MultiStrategyMonitor
        from types import SimpleNamespace
        from unittest.mock import patch
        now,*_=fixture(); first=now-timedelta(minutes=30)
        tracker=SimpleNamespace(multi_strategy_open_symbols=lambda *a:{'status':'available','symbols':['005930']},
            multi_strategy_position=lambda mode,symbol,strategy,asof:({'status':'owned','owned_qty':10,'half_take_done':False,
                'first_fill_at':first.isoformat(),'first_fill_observed_at':first.isoformat(),
                'first_fill_timing_verified':True} if strategy==S5 else {'status':'unsupported'}))
        class ExitProvider:
            def fetch(self,*a,**kw):return {'current_price':1000}
            def is_fresh(self,*a):return True
        session=SimpleNamespace(paper_automation_reconcile=SimpleNamespace(_tracker=tracker))
        settings={'paper_multi_strategy_enabled':True,'paper_multi_strategy_ids':[S5],'gdcrs_stocks':[]}
        report={'mode':'paper','exchange':'KRX','snapshot_changed':False,'holdings':[{'stk_cd':'005930','crd_tp':'00',
            'crd_loan_dt':'','rmnd_qty':'10','trde_able_qty':'10','pur_amt':'10000'}]}
        calls=[]
        monitor=MultiStrategyMonitor(settings_loader=lambda:settings,now=lambda:now,
            provider_factory=lambda _:object(),exit_provider_factory=lambda _:ExitProvider(),
            account_fetcher=lambda _:report,chart_fetcher=lambda *a,**kw:self.fail('time stop must precede chart'),
            executor=lambda **kw:calls.append(kw) or [])
        with patch('kiwoom_autotrader.trading.multi_strategy.session_identity',return_value=('paper','x')):
            monitor.tick(session,lambda _:None)
        self.assertEqual(len(calls),1,monitor.status());self.assertEqual(calls[0]['reason'],'time_stop_30m')

class S5SessionChartTests(unittest.TestCase):
    def test_legacy_cap_unchanged_and_s5_paper_only_extended_cap(self):
        from kiwoom_autotrader.api import minute_chart as chart
        from unittest.mock import patch
        with patch.object(chart,'session_identity',return_value=('paper','synthetic')), patch.object(chart,'_fetch_minute_chart',return_value=()) as fetch:
            chart.fetch_session_minute_chart(object(),'005930',count=390)
            self.assertEqual(fetch.call_args.kwargs,{'count':390,'maximum':390})
            chart.fetch_minute_chart(object(),'005930',count=62)
            self.assertEqual(fetch.call_args.kwargs,{'count':62,'maximum':62})
        with patch.object(chart,'session_identity',return_value=('real','synthetic')), patch.object(chart,'_fetch_minute_chart') as fetch:
            with self.assertRaises(chart.MinuteChartError):chart.fetch_session_minute_chart(object(),'005930')
            fetch.assert_not_called()
        with self.assertRaises(ValueError):chart.fetch_minute_chart(object(),'005930',count=63)
        with patch.object(chart,'session_identity',return_value=('paper','synthetic')):
            with self.assertRaises(ValueError):chart.fetch_session_minute_chart(object(),'005930',count=391)
