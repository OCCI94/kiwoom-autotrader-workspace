"""Future research plans never activate or weaken the current v1 order path."""
from copy import deepcopy
from datetime import datetime,timedelta
import unittest
from unittest.mock import patch
from kiwoom_autotrader.trading import paper_experiment_throughput_proposal as m

NOW='2026-11-03T14:15:00+09:00';H='a'*64


class FutureThroughputTests(unittest.TestCase):
    def proposal(self):return m.proposal(as_of=NOW,effective_from='2026-11-04T09:00:00+09:00',parent_profile_sha256=H)
    def test_future_profile_explicit_conflict_resolution_and_disabled(self):
        value=self.proposal()
        self.assertEqual(value['future_profile']['maximum_holdings'],60)
        self.assertEqual(value['future_profile']['budget_cap_krw'],100000)
        self.assertFalse(value['enabled']);self.assertFalse(value['can_submit'])
        self.assertFalse(value['current_profile_changed']);self.assertFalse(value['existing_assignments_changed'])
        self.assertEqual(len(value['variant_rules']),7)
        self.assertEqual(value['future_profile']['unfilled_ttl_seconds'],180)
        with self.assertRaises(ValueError):m.proposal(as_of=NOW,effective_from=NOW,parent_profile_sha256=H)

    def test_stability_pass_assertion_cannot_enable_or_bypass_canonical_validator(self):
        value=self.proposal();proof={'passed':True,'status':'verified','daily_reports':[]}
        result=m.review_activation(value,stability=proof,approval={'approved':True},as_of=NOW,deployment_gate_passed=True)
        self.assertFalse(result['ready_for_separate_integration_review']);self.assertFalse(result['can_submit'])
        self.assertFalse(m.stability_gate(proof,as_of=NOW)['passed'])

    def test_canonical_ten_complete_sessions_and_separate_approval_still_never_submit(self):
        from kiwoom_autotrader.trading import stability_metrics as k
        from tests.test_stability_metrics import official,samples,audit
        dates=['20261019','20261020','20261021','20261022','20261023','20261026','20261027','20261028','20261029','20261030']
        rows=[];previous='20261016'
        for day in dates:
            # The owning fixture uses the actual receiver/control producers,
            # minute witnesses and operations compiler under a private fake
            # clock. Numerical PASS assertions cannot replace those originals.
            cal=official(day,previous);end=datetime.fromisoformat(cal['end'])
            report=k.daily_report(cal,samples(cal),now=end+timedelta(minutes=30),audit=audit(cal))
            self.assertEqual(report['status'],'PASS')
            rows.append({'report':report,'report_sha256':k.digest(report)});previous=day
        proof={'schema':'paper-stability-ten-session-proof-v1','generated_at':NOW,'through_session_day':dates[-1],'daily_reports':rows}
        self.assertTrue(m.stability_gate(proof,as_of=NOW)['passed'])
        proposal=self.proposal();approval={'approved':True,'proposal_sha256':proposal['proposal_sha256'],
            'scope':'future_paper_throughput_version','issued_at':'2026-11-03T14:00:00+09:00','expires_at':'2026-11-03T14:30:00+09:00'}
        result=m.review_activation(proposal,stability=proof,approval=approval,as_of=NOW,deployment_gate_passed=True)
        self.assertTrue(result['ready_for_separate_integration_review']);self.assertFalse(result['can_submit'])
        self.assertFalse(m.review_activation(proposal,stability=proof,approval=approval,as_of=NOW)['ready_for_separate_integration_review'])
        bad=deepcopy(proof);bad['daily_reports'][3]['report']['ready_ratio']=None
        bad['daily_reports'][3]['report_sha256']=k.digest(bad['daily_reports'][3]['report'])
        self.assertFalse(m.stability_gate(bad,as_of=NOW)['passed'])

    def test_proposal_is_not_a_settings_profile_or_current_v1_limit_override(self):
        from kiwoom_autotrader.config.settings import effective_paper_experiment
        current=effective_paper_experiment({'paper_experiment_enabled':True,'paper_experiment_s5_enabled':True,
            'future_profile':self.proposal()['future_profile']},'paper')
        self.assertEqual(current['profile'],'paper_experiment_v1');self.assertEqual(current['max_concurrent_positions'],40)
        self.assertEqual(current['per_buy_budget_ceiling_krw'],300000)

    def fixture(self):
        frozen=datetime.fromisoformat('2026-11-04T08:55:00+09:00')
        session={'day':'20261104','start_at':'2026-11-04T09:00:00+09:00',
            'end_at':'2026-11-04T15:30:00+09:00','verified':True,'source_sha256':H,'available_at':frozen.isoformat()}
        rows=[{'symbol':f'{i:06d}','eligible':True,'source_sha256':H,'available_at':frozen.isoformat()} for i in range(25)]
        return frozen,session,rows

    def test_ten_random_planned_attempts_pit_hold_and_original_universe_preserved(self):
        frozen,session,rows=self.fixture();before=deepcopy(rows)
        plan=m.random_control_plan(rows,session=session,as_of=frozen,seed_sha256=H)
        self.assertEqual(rows,before);self.assertEqual(plan,m.random_control_plan(rows,session=session,as_of=frozen,seed_sha256=H))
        self.assertEqual(len(plan['jobs']),10);self.assertEqual(plan['universe_count'],25)
        self.assertEqual(len({r['symbol'] for r in plan['jobs']}),10)
        for job in plan['jobs']:
            entry=datetime.fromisoformat(job['planned_at']);exit=datetime.fromisoformat(job['planned_exit_at'])
            self.assertTrue('09:10'<=entry.strftime('%H:%M')<='14:50')
            self.assertGreaterEqual((exit-entry).total_seconds(),600)
            self.assertLessEqual(exit,datetime.fromisoformat(session['end_at']))
            self.assertFalse(job['permission_to_trade'])
        self.assertIsNone(plan['fills']);self.assertIsNone(plan['actual_attempts']);self.assertFalse(plan['can_submit'])

    def test_future_universe_unverified_session_short_pool_never_filled_in(self):
        frozen,session,rows=self.fixture()
        for changes in ('future','session','pool','replay'):
            values=deepcopy(rows);s=deepcopy(session);at=frozen
            if changes=='future':values[0]['available_at']=(frozen+timedelta(seconds=1)).isoformat()
            if changes=='session':s['verified']=False
            if changes=='pool':values=values[:9]
            if changes=='replay':at=datetime.fromisoformat(session['start_at'])
            with self.assertRaises(ValueError):m.random_control_plan(values,session=s,as_of=at,seed_sha256=H)

    def test_collision_logged_and_real_R_floor_can_wait_without_fabricating_R_signal(self):
        at='2026-11-04T10:00:00+09:00'
        signals=[{'arm':a,'symbol':'005930','signal_at':at,'passed':True,'evidence_sha256':H} for a in ('EXP_A','EXP_B')]
        result=m.resolve_collision(signals,day='20261104',symbol='005930',signal_at=at,
            seed_sha256=H,prior_counts={'EXP_R':0,'EXP_A':5,'EXP_B':10})
        self.assertIsNone(result['selected_arm']);self.assertEqual(result['reason'],'wait_for_real_control_signal')
        self.assertFalse(result['can_submit'])
        signals.append({'arm':'EXP_R','symbol':'005930','signal_at':at,'passed':True,'evidence_sha256':H})
        result=m.resolve_collision(signals,day='20261104',symbol='005930',signal_at=at,seed_sha256=H,prior_counts={'EXP_R':0,'EXP_A':5,'EXP_B':10})
        self.assertEqual(result['selected_arm'],'EXP_R');self.assertEqual(len(result['competing_arms']),3)
        self.assertFalse(result['historical_assignment_changed'])

    def test_progressive_orb_new_version_denominator_never_synthetic_or_oldrule_change(self):
        for index in (0,1,5,9,20):
            need=min(14,5+index);value=m.orb_progressive_requirement(prior_complete_sessions=need-1,new_version_session_index=index)
            self.assertIsNone(value['denominator_sessions']);self.assertEqual(value['required_sessions'],need)
            self.assertFalse(value['synthetic_bars_created']);self.assertTrue(value['original_strict14_unchanged'])
            self.assertEqual(m.orb_progressive_requirement(prior_complete_sessions=need,new_version_session_index=index)['denominator_sessions'],need)

    def test_variants_keep_base_features_and_all_safety_gates_even_when_relaxed_pattern_passes(self):
        feature={'golden_cross':True,'volume_multiple':'1.6','slow_sma_rising':True,'entry_before_1500':True}
        gates=dict.fromkeys(('paper_mode','stream_ready','risk_eligible','cash_reconciled','ownership_reconciled','input_integrity'),True)
        before=deepcopy(feature)
        self.assertTrue(m.evaluate_variant('EXP_B_VOL_1P5_V2',feature,safety_gates=gates)['research_signal_passed'])
        self.assertFalse(m.evaluate_variant('EXP_B_VOL_2_V2',feature,safety_gates=gates)['research_signal_passed'])
        for key in gates:
            altered={**gates,key:False};r=m.evaluate_variant('EXP_B_VOL_1P5_V2',feature,safety_gates=altered)
            self.assertFalse(r['research_signal_passed']);self.assertFalse(r['can_submit'])
        self.assertEqual(feature,before)
