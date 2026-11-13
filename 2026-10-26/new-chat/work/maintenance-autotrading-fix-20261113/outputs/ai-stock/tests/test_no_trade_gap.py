"""Bounded same-page volume reconciliation, with auditable inferred prices."""
from dataclasses import replace
from datetime import datetime, timedelta
import unittest

from kiwoom_autotrader.api.minute_chart import MinuteBar, _bars
from kiwoom_autotrader.api.session import KST
from kiwoom_autotrader.trading.cross_monitor import CrossMonitorError, MinuteWindowError, closed_window, sma_difference
from kiwoom_autotrader.trading.decision_evidence import DecisionEvidenceError, cross_evidence, freeze_evidence


class NoTradeGapTests(unittest.TestCase):
    def setUp(self):
        self.start = datetime(2026, 10, 27, 10, 0, tzinfo=KST)
        self.now = self.start + timedelta(minutes=5, seconds=10)
        # One mock broker page, descending; 10:01 is absent and 15-10=5.
        rows = [(4,105,3,20),(3,110,2,17),(2,110,5,15),(0,100,10,10)]
        payload={"return_code":0,"stk_cd":"003220", "stk_min_pole_chart_qry":[
            {"cntr_tm":(self.start+timedelta(minutes=minute)).strftime('%Y%m%d%H%M%S'),
             "cur_prc":str(price),"trde_qty":str(volume),"acc_trde_qty":str(acc),
             "upd_stkpc_tp":"","upd_rt":""} for minute,price,volume,acc in rows]}
        self.raw=payload
        self.bars=tuple(sorted(_bars(payload,"003220",mode="paper",batch="page-a",page=0,response_sha256='a'*64,query_started_at=self.now),key=lambda x:x.start))

    def test_inner_gap_is_bounded_assumption_and_keeps_evidence(self):
        window=closed_window(self.bars,self.now,3)
        inferred=window[0]
        self.assertEqual((inferred.start,inferred.close,inferred.volume),(self.start+timedelta(minutes=1),100,0))
        proof=inferred.gap_proof
        self.assertEqual((proof.previous_acc_volume,proof.next_volume,proof.next_acc_volume),(10,5,15))
        evidence=cross_evidence(strategy="gdcrs",run_id="test",source_hash=None,
            source_kind="broker_observation",config={"short_period":1,"long_period":3},
            observed_at=self.now,evaluated_at=self.now,window=window,previous_difference=-1,difference=1)
        payload=evidence.as_dict()
        info=payload['bars'][0]['source_inference']
        self.assertEqual(info['provider_gap_semantics'],'unverified')
        self.assertEqual(info['method'],'same_page_krx_zero_volume_assumption_v1')
        self.assertEqual((info['previous_acc_volume'],info['next_volume'],info['next_acc_volume']),(10,5,15))
        self.assertEqual(payload['bars'][1]['source_observation']['volume'],5)
        info['next_acc_volume']=16
        payload['source_sha256']=None
        with self.assertRaises(DecisionEvidenceError):freeze_evidence(payload)

    def test_invalid_or_missing_anchor_or_market_context_never_interpolates(self):
        left,right=self.bars[:2]
        changes=[
            replace(right,acc_volume=16), replace(right,volume=None), replace(right,volume=0),
            replace(right,source_symbol='000660'), replace(right,source_mode='real'),
            replace(right,source_batch='another'), replace(right,source_page=1),
            replace(right,source_row=right.source_row+2), replace(right,adjustment='1'),
            replace(right,adjustment=None), replace(right,adjustment_rate='1'), replace(right,source_response_sha256='b'*64),
        ]
        for altered in changes:
            with self.subTest(altered=altered),self.assertRaises(MinuteWindowError):
                closed_window((left,altered,*self.bars[2:]),self.now,3)
        with self.assertRaises(MinuteWindowError):
            closed_window(tuple(replace(b,source_mode='real') for b in self.bars),self.now,3)

    def test_latest_missing_or_right_anchor_still_forming_never_inferred(self):
        with self.assertRaises(MinuteWindowError) as raised:
            closed_window(self.bars[:-1],self.now,3)
        # The source omitted both 10:01 and the latest closed 10:04.
        # Only 10:01 has a completed, same-page cumulative-volume proof;
        # it must not be presented as an unresolved outage.
        self.assertEqual(raised.exception.details['source_missing_minutes'], [
            '2026-10-27T10:01:00+09:00', '2026-10-27T10:04:00+09:00'])
        self.assertEqual(raised.exception.details['inferred_minutes'], ['2026-10-27T10:01:00+09:00'])
        self.assertEqual(raised.exception.details['missing_minutes'], ['2026-10-27T10:04:00+09:00'])
        self.assertTrue(raised.exception.details['interpolated'])
        # Even though cumulative values agree, the anchor 10:02 is not closed.
        forming_at=self.start+timedelta(minutes=2,seconds=30)
        with self.assertRaises(MinuteWindowError):
            closed_window(tuple(replace(b,source_query_started_at=forming_at) for b in self.bars[:2]),forming_at,2)

    def test_session_boundary_mismatch_and_long_gaps_not_inferred(self):
        left,right=self.bars[:2]
        prior=replace(left,start=self.start.replace(hour=8,minute=59))
        with self.assertRaises(CrossMonitorError):closed_window((prior,right,*self.bars[2:]),self.now,3)
        later=replace(right,start=self.start+timedelta(minutes=5))
        late_at=self.start+timedelta(minutes=6,seconds=10)
        with self.assertRaises(MinuteWindowError):closed_window((left,later),late_at,3)

    def test_overlap_is_stable_across_new_response_batches_and_detects_revisions(self):
        first_at=self.now-timedelta(minutes=1)
        first=closed_window(tuple(replace(b,source_query_started_at=first_at) for b in self.bars),first_at,3)
        payload=self.raw.copy()
        refreshed=tuple(sorted(_bars(payload,'003220',mode='paper',batch='page-b',page=0,response_sha256='b'*64,query_started_at=self.now),key=lambda x:x.start))
        second=closed_window(refreshed,self.now,3)
        self.assertEqual(first[1:],second[:-1])
        self.assertIsNotNone(first[1].gap_proof)
        self.assertIsNotNone(second[0].gap_proof)
        self.assertNotEqual(first[1].gap_proof.response_sha256,second[0].gap_proof.response_sha256)
        revised=list(refreshed);revised[-1]=replace(revised[-1],acc_volume=21)
        self.assertNotEqual(second,closed_window(tuple(revised),self.now,3))

    def test_rollover_provenance_accepts_same_anchor_values_from_new_page(self):
        rows=[dict(row) for row in self.raw['stk_min_pole_chart_qry']]
        for row in rows:
            if row['cntr_tm'][-4:] in ('0300','0400'):row['cur_prc']='90'
            if row['cntr_tm'][-4:]=='0200':row['cur_prc']='100'
        payload={**self.raw,'stk_min_pole_chart_qry':rows}
        observed=self.start+timedelta(minutes=4,seconds=5)
        new_at=self.start+timedelta(minutes=5,seconds=5)
        bars_a=tuple(sorted(_bars(payload,'003220',mode='paper',batch='a',page=0,response_sha256='a'*64,query_started_at=observed),key=lambda x:x.start))
        bars_b=tuple(sorted(_bars(payload,'003220',mode='paper',batch='b',page=0,response_sha256='b'*64,query_started_at=new_at),key=lambda x:x.start))
        window=closed_window(bars_a,observed,3);newer=closed_window(bars_b,new_at,3)
        evidence=cross_evidence(strategy='ddcrs',run_id='fixture',source_hash=None,
            source_kind='broker_observation',config={'short_period':1,'long_period':3},
            observed_at=observed,evaluated_at=new_at,window=window,previous_difference=1,difference=-20,
            rollover_window=newer,rollover_observed_at=new_at).as_dict()
        self.assertEqual(evidence['bars'][1]['source_inference']['response_sha256'],'a'*64)
        self.assertEqual(evidence['rollover_revalidation']['bars'][0]['source_inference']['response_sha256'],'b'*64)

    def test_rolled_forward_evidence_requires_one_fresh_nonreversed_window(self):
        def bar(m,p):return MinuteBar(self.start+timedelta(minutes=m),p)
        window=tuple(bar(i,p) for i,p in enumerate((1,2,3,1)))
        newer=window[1:]+(bar(4,1),)
        observed=self.start+timedelta(minutes=4,seconds=59)
        reobserved=self.start+timedelta(minutes=5,seconds=3)
        values=dict(strategy='ddcrs',run_id='test',source_hash=None,source_kind='synthetic',
            config={'short_period':1,'long_period':3},observed_at=observed,
            evaluated_at=reobserved,window=window,previous_difference=3,difference=-3,
            rollover_window=newer,rollover_observed_at=reobserved)
        data=cross_evidence(**values).as_dict()
        self.assertEqual(data['rollover_revalidation']['difference'],-2)
        wrong=newer[:-1]+(bar(4,10),)
        with self.assertRaises(DecisionEvidenceError):cross_evidence(**{**values,'rollover_window':wrong})
        with self.assertRaises(DecisionEvidenceError):cross_evidence(**{**values,'rollover_window':window})
        with self.assertRaises(DecisionEvidenceError):cross_evidence(**{**values,'rollover_observed_at':observed})

    def test_maximum_period_and_ten_inferred_bars_fit_bounded_rollover_evidence(self):
        start=self.start.replace(hour=9,minute=0)
        missing={3*i for i in range(1,11)}
        def chart(last,at,key):
            rows=[];acc=0
            for index in range(last+1):
                if index in missing:continue
                acc+=1
                rows.append({'cntr_tm':(start+timedelta(minutes=index)).strftime('%Y%m%d%H%M%S'),
                    'cur_prc':str(100000-index*10),'trde_qty':'1','acc_trde_qty':str(acc),
                    'upd_stkpc_tp':'','upd_rt':''})
            payload={'return_code':0,'stk_cd':'003220','stk_min_pole_chart_qry':list(reversed(rows))}
            return tuple(sorted(_bars(payload,'003220',mode='paper',batch=key,page=0,
                response_sha256=key*64,query_started_at=at),key=lambda bar:bar.start))
        at=start+timedelta(minutes=63,seconds=3);later=at+timedelta(minutes=1)
        initial=closed_window(chart(62,at,'a'),at,60)
        newer=closed_window(chart(63,later,'b'),later,60)
        self.assertEqual(sum(bar.gap_proof is not None for bar in initial),10)
        evidence=cross_evidence(strategy='ddcrs',run_id='fixture',source_hash=None,
            source_kind='broker_observation',config={'short_period':5,'long_period':60},
            observed_at=at,evaluated_at=later,window=initial,previous_difference=1,
            difference=sma_difference(initial,5,60),rollover_window=newer,rollover_observed_at=later)
        self.assertLessEqual(len(evidence.json.encode()),65536)
