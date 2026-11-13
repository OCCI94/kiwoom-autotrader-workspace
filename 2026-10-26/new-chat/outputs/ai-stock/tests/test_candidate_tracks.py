"""Synthetic track, partial-membership, KOSDAQ and immutable S1 contracts."""
import copy
import json
from pathlib import Path
import tempfile
from datetime import timedelta
from decimal import Decimal
import unittest
from kiwoom_autotrader.trading.candidate_tracks import (T1,T2,track_for_change,
    validate_precursor_evidence,select_candidate_tracks)
from kiwoom_autotrader.trading.toss_candidate_feed import validate_candidate,merge_toss_candidates
from kiwoom_autotrader.trading.dynamic_s1_admission import admission_digest,validate_dynamic_admission
from kiwoom_autotrader.trading.paper_funnel_state import _candidate_projection
from kiwoom_autotrader.trading.decision_evidence import freeze_evidence
from tests.test_toss_candidate_feed import NOW,row
from tests.test_toss_candidate_feed import feed as feed_document
import tests.test_dynamic_candidates as dynamic_fixture
from tests.test_dynamic_s1_evidence import admission,evidence
import tests.test_dynamic_s1_evidence as s1_fixture


def precursor(symbol='005930',now=NOW):
    return {'schema':'toss-precursor-evidence-v1','symbol':symbol,'session_day':now.date().isoformat(),
        'available_at':now.isoformat(),'passed_count':2,
        'common_guard':{'basis':'observed_open_support','day_open':100500,'current_price':102000,
            'previous_price':101500,'previous_price_at':(now-timedelta(seconds=45)).isoformat(),
            'current_price_at':now.isoformat()},
        'conditions':{'a':{'status':'pass','observed_at':now.isoformat(),'source_sha256':'1'*64,
                          'details':{'rank_type':'MARKET_TRADING_VOLUME','rank':20}},
            'b':{'status':'pass','observed_at':now.isoformat(),'source_sha256':'2'*64,
                 'details':{'ask_total':11500,'bid_total':10000}},
            'c':{'status':'unknown'},'d':{'status':'unknown'}}}


def candidate(code='005930',*,now=NOW,early=False,market='KOSDAQ'):
    result=row(code)
    for key in ('price_timestamp','ranked_at','available_at'):
        result[key]=now.isoformat()
    result.update(market=market,stocks_request_started_at=now.isoformat(),stocks_observed_at=now.isoformat())
    result['stock_warning_evidence']={'schema':'toss-stock-warnings-v1','symbol':code,'requested_symbol':code,
        'status':'clear','reason_codes':[],'warning_types':[],'record_count':0,'source':'toss_openapi',
        'permission_to_trade':False,'risk_zero_guaranteed':False,'request_started_at':now.isoformat(),
        'observed_at':now.isoformat(),'expires_at':(now+timedelta(minutes=15)).isoformat(),'source_sha256':'9'*64}
    result['market_rank']=1
    result['of005_ranking_evidence']={'schema':'toss-of005-ranking-evidence-v1','symbol':code,'rank':1,
        'rank_type':'MARKET_TRADING_AMOUNT','ranked_at':now.isoformat(),'observed_at':now.isoformat(),
        'source_sha256':'8'*64,'top10_complete':True}
    result['short_session_day']=result['daily_baseline_session_day']='2026-10-30' if now.date().isoformat()=='2026-11-02' else '2026-10-29'
    result['risk_review'].update(reviewed_at=now.isoformat(),expires_at=(now+timedelta(hours=1)).isoformat())
    if early:
        result.update(track=T1,current_price=102000,day_open=100500,change_pct='2',adr_extension_multiple='0.8',
                      precursor_evidence=precursor(code,now))
    else:result['track']=T2
    return result


def source(rows,now=NOW):
    return {'status':'ready','candidates':rows,'source_sha256':'f'*64,
        'expires_at':(now+timedelta(seconds=75)).isoformat(),'session_day':now.date().isoformat(),
        'previous_session_day':rows[0]['short_session_day'] if rows else '2026-10-30'}


def merged(rows,*,now=NOW,pages=(),configured=()):
    return merge_toss_candidates({'schema':'paper-dynamic-candidates-v1','status':'unavailable',
        'candidates':[],'base_candidates':[]},source(rows,now),pages,{},now=now,
        configured_symbols=configured,limit=15,tiered=True)


class TrackTests(unittest.TestCase):
    def test_numeric_bounds_and_two_independent_evidence_groups(self):
        self.assertEqual([track_for_change(v) for v in ('0.49','0.5','2.999','3','15','15.001')],
                         [None,T1,T1,T2,T2,None])
        value=candidate(early=True)
        self.assertEqual(validate_candidate(value,now=NOW,session_day='2026-11-02',previous_session_day='2026-10-30'),value)
        for mutate in (lambda p:p.update(passed_count=3),lambda p:p['conditions']['b']['details'].update(ask_total=11499),
                lambda p:p['conditions']['b'].update(status='unknown'),
                lambda p:p['conditions']['a']['details'].update(rank_type='UNVERIFIED'),
                lambda p:p['common_guard'].update(previous_price=100500),
                lambda p:p['common_guard'].update(previous_price_at=(NOW-timedelta(seconds=76)).isoformat()),
                lambda p:p['conditions']['b'].update(observed_at=(NOW+timedelta(seconds=1)).isoformat()),
                lambda p:p['conditions']['b'].update(source_sha256='bad')):
            bad=copy.deepcopy(value);mutate(bad['precursor_evidence'])
            with self.subTest(mutate=mutate),self.assertRaises(ValueError):
                validate_candidate(bad,now=NOW,session_day='2026-11-02',previous_session_day='2026-10-30')
    def test_trade_and_program_facts_not_invented_from_boolean_flags(self):
        item=candidate(early=True);p=item['precursor_evidence']
        p['conditions']['b']['details']={'spread_bps':'30','buy_trade_ratio':'0.50001'}
        validate_candidate(item,now=NOW,session_day='2026-11-02',previous_session_day='2026-10-30')
        p['conditions']['b']['details']['buy_trade_ratio']='0.5'
        with self.assertRaises(ValueError):validate_candidate(item,now=NOW,session_day='2026-11-02',previous_session_day='2026-10-30')
        p['conditions']['b']={'status':'unknown'}
        p['conditions']['c']={'status':'pass','observed_at':NOW.isoformat(),'source_sha256':'3'*64,
                             'details':{'program_net_buy_krw':1}}
        validate_candidate(item,now=NOW,session_day='2026-11-02',previous_session_day='2026-10-30')
        p['conditions']['c']['details']={'vdu_converged':True,'foreign_net_buy_krw':1000}
        with self.assertRaises(ValueError):validate_candidate(item,now=NOW,session_day='2026-11-02',previous_session_day='2026-10-30')
    def test_track_caps_and_kosdaq_minimum_never_force_ineligible(self):
        rows=[]
        for i in range(40):
            code=f'{100000+i:06d}'; early=i%2==0
            r=candidate(code,early=early,market='KOSDAQ' if i>=20 else 'KOSPI')
            r['turnover_krw']=1_000_000_000-i*10000;rows.append(r)
        out=merged(rows);self.assertEqual(len(out['candidates']),15)
        diag=out['selection_diagnostics']
        self.assertEqual(diag['selected_track_counts'],{T1:7,T2:8})
        self.assertGreaterEqual(diag['selected_kosdaq_count'],8)
        self.assertEqual(diag['forced_ineligible_count'],0)
        small=merged(rows[:3]+rows[20:22]);self.assertEqual(small['selection_diagnostics']['selected_kosdaq_count'],2)
        self.assertLess(len(small['candidates']),15)
    def test_base_members_are_retained_and_do_not_consume_dynamic_quota(self):
        rows=[candidate(f'{100000+i:06d}',early=i%2==0) for i in range(40)]
        base=tuple(r['symbol'] for r in rows[:25]);out=merged(rows,configured=base)
        self.assertEqual({r['symbol'] for r in out['base_candidates']},set(base))
        self.assertLessEqual(len(out['candidates']),15)
        self.assertFalse(set(base)&{r['symbol'] for r in out['candidates']})
    def test_missing_partial_kiwoom_membership_is_not_negative_evidence(self):
        out=merged([candidate()]);self.assertEqual(len(out['candidates']),1)
        member=out['candidates'][0]['membership_evidence']
        self.assertEqual(member['source'],'toss_stocks');self.assertNotIn('auditInfo',member)
        self.assertEqual(member['kiwoom_membership_status'],'absent_not_negative_evidence')
        for update in ({'stocks_observed_at':(NOW-timedelta(seconds=76)).isoformat()},
                {'common_share':False},{'krx_suspended':True},{'liquidation_trading':True}):
            bad=candidate();bad.update(update);self.assertEqual(merged([bad])['candidates'],[])
        for update in ({'status':'blocked'},{'warning_types':['MANAGEMENT_STOCK']},{'record_count':1},
                       {'expires_at':NOW.isoformat()},{'requested_symbol':'000660'}):
            bad=candidate();bad['stock_warning_evidence'].update(update)
            self.assertEqual(merged([bad])['candidates'],[])
        bad=candidate();bad.pop('stock_warning_evidence');self.assertEqual(merged([bad])['candidates'],[])

    def test_market_amount_rank_is_bound_separately_and_server_rank_age_is_300_seconds(self):
        item=candidate();item['ranked_at']=(NOW-timedelta(seconds=299)).isoformat()
        item['turnover_rank']=60
        validate_candidate(item,now=NOW,session_day='2026-11-02',previous_session_day='2026-10-30')
        item['ranked_at']=(NOW-timedelta(seconds=301)).isoformat()
        with self.assertRaises(ValueError):validate_candidate(item,now=NOW,session_day='2026-11-02',previous_session_day='2026-10-30')
        item=candidate();item['of005_ranking_evidence']['rank']=20
        with self.assertRaises(ValueError):validate_candidate(item,now=NOW,session_day='2026-11-02',previous_session_day='2026-10-30')
        item.update(market_rank=20,of005_top10_rank=None)
        validate_candidate(item,now=NOW,session_day='2026-11-02',previous_session_day='2026-10-30')
        item['price_timestamp']=(NOW-timedelta(seconds=76)).isoformat()
        with self.assertRaises(ValueError):validate_candidate(item,now=NOW,session_day='2026-11-02',previous_session_day='2026-10-30')

    def test_t1_rank_group_uses_300_seconds_while_book_group_stays_75(self):
        item=candidate(early=True);conditions=item['precursor_evidence']['conditions']
        conditions['a']['observed_at']=(NOW-timedelta(seconds=300)).isoformat()
        validate_candidate(item,now=NOW,session_day='2026-11-02',previous_session_day='2026-10-30')
        conditions['b']['observed_at']=(NOW-timedelta(seconds=76)).isoformat()
        with self.assertRaises(ValueError):validate_candidate(item,now=NOW,session_day='2026-11-02',previous_session_day='2026-10-30')
        early=NOW.replace(hour=9,minute=3);item=candidate(early=True,now=early)
        item['precursor_evidence']['conditions']['a']['observed_at']=early.replace(hour=8,minute=59).isoformat()
        with self.assertRaises(ValueError):validate_candidate(item,now=early,session_day='2026-11-02',previous_session_day='2026-10-30')
    def test_actual_broker_warning_duplicate_and_market_conflicts_remain_blocked(self):
        member=dynamic_fixture.DynamicCandidateTests.member('005930','10');member['name']='삼성전자'
        page={'market':'10','rows':{'005930':member},'request_started_at':NOW.isoformat(),
              'available_at':NOW.isoformat(),'source_sha256':'b'*64,'truncated':True}
        self.assertEqual(len(merged([candidate()],pages=[page])['candidates']),1)
        for key,value in (('auditInfo','관리종목'),('orderWarning','3'),('state','거래정지'),('name','다른기업')):
            changed=copy.deepcopy(page);changed['rows']['005930'][key]=value
            self.assertEqual(merged([candidate()],pages=[changed])['candidates'],[])
        self.assertEqual(merged([candidate()],pages=[page,page])['candidates'],[])
        self.assertEqual(merged([candidate(market='KOSPI')],pages=[page])['candidates'],[])
    def test_track_source_survives_dynamic_s1_immutable_evidence_and_stale_book_blocks(self):
        for early in (False,True):
            incoming=candidate(early=early);out=merged([incoming]);r=out['candidates'][0]
            proof=admission(NOW);proof['schema_version']='paper-dynamic-s1-admission-v2'
            proof['candidate']=_candidate_projection(r);proof['admission_sha256']=admission_digest(proof)
            self.assertEqual(validate_dynamic_admission(proof,now=NOW)['candidate']['track'],T1 if early else T2)
            ev=evidence(NOW,proof);self.assertEqual(freeze_evidence(ev.as_dict()).sha256,ev.sha256)
            with self.assertRaises(ValueError):validate_dynamic_admission(proof,now=NOW+timedelta(seconds=6))
            if early:
                proof['candidate']['toss_evidence']['precursor_evidence']['conditions']['b']['details']['ask_total']=1
                proof['admission_sha256']=admission_digest(proof)
                with self.assertRaises(ValueError):validate_dynamic_admission(proof,now=NOW)

    def test_t1_s1_actual_atomic_preparation_keeps_v5_half_budget_and_pending_guards(self):
        fx=s1_fixture.DynamicLedgerTests();fx.setUp();self.addCleanup(fx.doCleanups)
        now=fx.fx.now;incoming=candidate(early=True,now=now)
        selected=merged([incoming],now=now)['candidates'][0]
        ad=admission(now);ad['schema_version']='paper-dynamic-s1-admission-v2'
        ad['candidate']=_candidate_projection(selected);ad['admission_sha256']=admission_digest(ad)
        ev=evidence(now,ad)
        from kiwoom_autotrader.trading.execution_log import ExecutionLogError
        with self.assertRaises(ExecutionLogError):fx.prepare(ev=ev,proof=fx.fx.proof(schema_version=3))
        with self.assertRaises(ExecutionLogError):fx.prepare(ev=ev,quantity=501,proof=fx.fx.proof(order_quantity=501))
        ident=fx.prepare(ev=ev)
        fx.tracker.assert_multi_buy_dispatch('paper','005930',ident)
        with self.assertRaises(ExecutionLogError):fx.prepare(ev=ev)

    def test_live_local_feed_and_risk_revocation_reject_saved_toss_admission(self):
        from kiwoom_autotrader.api.session import BrokerSession,IssuedToken
        from kiwoom_autotrader.trading.paper_strategy_context import PaperStrategyContextProvider
        from kiwoom_autotrader.trading.toss_candidate_feed import read_toss_candidates
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory);feed_path=path/'feed.json';risk_path=path/'risk.json'
            item=candidate(early=True);doc=feed_document();doc['candidates']=[item]
            feed_path.write_text(json.dumps(doc));feed_path.chmod(0o600)
            risk={'schema':'toss-hybrid-risk-evidence-v1','generated_at':NOW.isoformat(),
                'expires_at':item['risk_review']['expires_at'],'rows':[dict(item['risk_review'],
                    symbol=item['symbol'],source_sha256=item['source_hashes']['risk_review'])]}
            risk_path.write_text(json.dumps(risk));risk_path.chmod(0o600)
            session=BrokerSession(now=lambda:NOW);session.mode='paper';session._issued=IssuedToken('fake','20261102235959')
            provider=PaperStrategyContextProvider(session,now=lambda:NOW,enable_toss_hybrid=True,
                toss_candidates_path=feed_path,s5_risk_path=risk_path)
            current=read_toss_candidates(feed_path,NOW)
            saved=copy.deepcopy(item)|{'feed_sha256':current['source_sha256'],'feed_expires_at':current['expires_at']}
            self.assertTrue(provider.toss_candidate_is_current(saved,NOW))
            risk['rows'][0]['unresolved_major_risk']=True;risk_path.write_text(json.dumps(risk))
            self.assertFalse(provider.toss_candidate_is_current(saved,NOW))
            risk['rows'][0]['unresolved_major_risk']=False;risk_path.write_text(json.dumps(risk))
            doc['status']='error';feed_path.write_text(json.dumps(doc))
            self.assertFalse(provider.toss_candidate_is_current(saved,NOW))

    def test_dynamic_s1_final_callback_rechecks_local_generation(self):
        from kiwoom_autotrader.api.session import BrokerSession,IssuedToken
        from kiwoom_autotrader.trading.paper_funnel_state import publish_gate,mark_precision_ready,dynamic_s1_admission,assert_current_dynamic_s1_admission
        from tests.test_paper_funnel import book_at
        session=BrokerSession(now=lambda:NOW);session.mode='paper';session._issued=IssuedToken('fake','20261102235959')
        selected=merged([candidate(early=True)])['candidates'][0];valid=[True]
        publish_gate(session,NOW,['005930'],available=True,candidates={'005930':selected},provider_binding='a'*32,
            watch_candidate_validator=lambda symbol,item,at:valid[0])
        mark_precision_ready(session,NOW,'005930',book_at(NOW,'005930')|{'session_binding':'a'*32})
        proof=dynamic_s1_admission(session,NOW,'005930')
        assert_current_dynamic_s1_admission(session,NOW,proof)
        valid[0]=False
        with self.assertRaises(ValueError):assert_current_dynamic_s1_admission(session,NOW,proof)


class KosdaqProviderTests(unittest.TestCase):
    def setUp(self):
        self.fx=dynamic_fixture.DynamicCandidateTests();self.fx.setUp();self.addCleanup(self.fx.doCleanups)
        fx=self.fx;fx.provider._enable_toss_hybrid=True;self.calls=[]
        fx.provider._bind()
        for rows in (fx.surge_rows,fx.turnover_rows):
            for item in rows:item['cur_prc']='+30000'
        fx.provider._kosdaq_surge_fetcher=lambda:self.page('surge')
        fx.provider._kosdaq_turnover_fetcher=lambda:self.page('turnover')
        fx.provider._membership_snapshots['10']={'market':'10','rows':{'035900':fx.member('035900','10')},
            'request_started_at':fx.now.isoformat(),'available_at':fx.now.isoformat(),'source_sha256':'d'*64,'truncated':True}
    def page(self,kind):
        fx=self.fx;self.calls.append(kind)
        if kind=='surge':return fx.page([{'stk_cd':'035900','cur_prc':'+30000','flu_rt':'3.2','sdnin_rt':'10','pred_pre_sig':'2','now_trde_qty':'10000'}],
            'trde_qty_sdnin','e')|{'instrument_filter':'ka10023_stk_cnd_20'}
        return fx.page([{'stk_cd':'035900','cur_prc':'+30000','flu_rt':'3.2','pred_pre_sig':'2','now_trde_qty':'10020','trde_prica':'900'}],
                       'trde_prica_upper','f')
    def test_kosdaq_extra_pages_are_cached_counted_and_individually_source_bound(self):
        out=self.fx.discover(limit=15);out2=self.fx.discover(limit=15)
        self.assertEqual(self.calls,['surge','turnover'])
        self.assertEqual(out['request_accounting']['ka10023'],2);self.assertEqual(out['request_accounting']['ka10032'],2)
        self.assertEqual(out['request_accounting']['rank_calls_per_minute_max'],4)
        found={r['symbol']:r for r in out['candidates']};self.assertIn('035900',found)
        self.assertEqual(found['035900']['source_hashes']['surge'],'e'*64)
        self.assertEqual(found['035900']['source_hashes']['turnover'],'f'*64)
        self.assertEqual(found['035900']['market'],'KOSDAQ')
        self.assertEqual(out['candidates'],out2['candidates'])
    def test_high_price_or_missing_current_price_never_takes_a_half_budget_slot(self):
        for value in ('+500001',None):
            self.fx.now+=timedelta(minutes=1)
            self.fx.surge_rows[0]['cur_prc']=value
            out=self.fx.discover(limit=15)
            self.assertNotIn('005930',{r['symbol'] for r in out['candidates']})
    def test_failure_not_retried_and_documented_market_filters(self):
        def fail():self.calls.append('failed');raise ValueError('synthetic')
        self.fx.provider._kosdaq_surge_fetcher=fail
        self.fx.discover(limit=15);self.fx.discover(limit=15)
        self.assertEqual(self.calls,['failed','turnover'])
        captured=[]
        self.fx.provider._ranking_page=lambda api,body,*a,**k:captured.append((api,body)) or {}
        self.fx.provider._fetch_surge_page('101');self.fx.provider._fetch_turnover_page('101')
        self.assertTrue(all(body['mrkt_tp']=='101' for api,body in captured))
        self.assertEqual(captured[0][1]['stk_cnd'],'20');self.assertEqual(captured[1][1]['mang_stk_incls'],'0')

    def test_kiwoom_track_waits_for_fresh_actual_open_price_at_final_admission(self):
        provider=self.fx.provider;now=self.fx.now;code='005930'
        self.assertFalse(provider.watch_candidate_is_current(code,{'track':T2},now))
        provider._cache[code]={'symbol':code,'session_binding':provider._binding,
            'request_started_at':now.isoformat(),'available_at':now.isoformat(),'observed_at':now.isoformat(),
            'current_price':30000,'day_open':29900}
        self.assertTrue(provider.watch_candidate_is_current(code,{'track':T2},now))
        provider._cache[code]['day_open']=30000
        self.assertFalse(provider.watch_candidate_is_current(code,{'track':T2},now))
        provider._cache[code]['day_open']=29900
        self.assertFalse(provider.watch_candidate_is_current(code,{'track':T2},now+timedelta(seconds=6)))
