from copy import deepcopy
from datetime import datetime,timedelta
import unittest
from tempfile import TemporaryDirectory
from pathlib import Path
from kiwoom_autotrader.trading.stability_metrics import (
    KST,TickHealth,checkpoint,daily_report,digest,validate_ten_session_proof,weekly_one_page)


def official(day='20261103',previous='20261102'):
    base=datetime.strptime(day,'%Y%m%d').replace(tzinfo=KST)
    return {'verified':True,'day':day,'previous_session_day':previous,
        'start':base.replace(hour=9).isoformat(),'end':base.replace(hour=15,minute=30).isoformat(),
        'calendar_sha256':'a'*64}


def samples(cal):
    start=datetime.fromisoformat(cal['start']);end=datetime.fromisoformat(cal['end'])
    return [{'schema':'paper-readonly-watchdog-sample-v1','observed_at':(start+timedelta(minutes=i)).isoformat(),
             'stream_ready':True} for i in range(int((end-start).total_seconds()//60))]


def audit(cal):
    from tests.test_stability_audit import actual_fixture_evidence,actual_control_evidence
    from kiwoom_autotrader.trading.stability_audit import compile_operations_audit
    with TemporaryDirectory() as temporary:
        root=Path(temporary).resolve()
        evidence=actual_fixture_evidence(root/'receiver',cal)
        evidence['control_evidence']=actual_control_evidence(root/'controls',cal)
    return compile_operations_audit(cal,evidence,now=datetime.fromisoformat(cal['end'])+timedelta(minutes=30))


class StabilityTests(unittest.TestCase):
    def setUp(self):self.cal=official();self.now=datetime(2026,11,3,16,tzinfo=KST)

    def test_no_polls_or_missing_audit_is_unknown_not_success(self):
        for rows in ([],samples(self.cal)):
            r=daily_report(self.cal,rows,now=self.now)
            self.assertEqual(r['status'],'UNVERIFIED');self.assertIsNone(r['ready_ratio'])
            self.assertIsNone(r['manual_interventions'])

    def test_clock_grid_loss_and_duplicate_conflict_prevent_pass(self):
        rows=samples(self.cal)
        for changed in (rows[1:],rows+[dict(rows[0],stream_ready=False)]):
            r=daily_report(self.cal,changed,now=self.now,audit=audit(self.cal))
            self.assertFalse(r['coverage_complete']);self.assertEqual(r['status'],'UNVERIFIED')

    def test_complete_audit_measures_dropout_time_not_poll_count(self):
        a=audit(self.cal);a['ready_seconds']-=120
        r=daily_report(self.cal,samples(self.cal),now=self.now,audit=a)
        self.assertEqual(r['sampled_ready_ratio'],1);self.assertIsNone(r['ready_ratio'])
        self.assertEqual(r['status'],'UNVERIFIED')  # numerical claims lack producer envelopes.

    def test_tick_threshold_strict_and_missing_manual_does_not_zero(self):
        a=audit(self.cal);a['maximum_tick_gap_seconds']=120
        self.assertEqual(daily_report(self.cal,samples(self.cal),now=self.now,audit=a)['status'],'UNVERIFIED')
        a.pop('manual_interventions')
        self.assertEqual(daily_report(self.cal,samples(self.cal),now=self.now,audit=a)['status'],'UNVERIFIED')

    def test_instrumentation_fake_clock_failure_and_backwards_clock(self):
        h=TickHealth();h.begin(self.now,100);h.end(self.now+timedelta(seconds=4),104,success=False)
        h.begin(self.now+timedelta(seconds=130),230);h.end(self.now+timedelta(seconds=131),231,success=True)
        s=h.status();self.assertEqual(s['maximum_start_gap_seconds'],130);self.assertEqual(s['failed_ticks'],1)
        h.begin(self.now,10);self.assertTrue(h.status()['clock_unverified'])

    def test_checkpoint_late_skip_delayed_market_holiday_and_overnight(self):
        r=checkpoint('preopen',self.cal,now=self.now,sample={});self.assertEqual(r['status'],'skipped')
        invalid=dict(self.cal,verified=False)
        with self.assertRaises(ValueError):checkpoint('preopen',invalid,now=self.now,sample={})
        delayed=dict(self.cal,end='2026-11-03T16:30:00+09:00')
        r=checkpoint('exits',delayed,now=self.now.replace(hour=15,minute=35),sample={},owned=[])
        self.assertEqual(r['reason'],'official_session_not_closed')
        rows=[{'symbol':'005930','quantity':2,'intended_overnight':True,'ownership_proof_sha256':'c'*64}]
        r=checkpoint('exits',self.cal,now=self.now.replace(hour=15,minute=35),sample={},owned=rows)
        self.assertEqual(r['status'],'PASS');self.assertEqual(r['intended_overnight_excluded'],['005930'])
        rows[0].pop('ownership_proof_sha256')
        self.assertEqual(checkpoint('exits',self.cal,now=self.now.replace(hour=15,minute=35),sample={},owned=rows)['status'],'UNVERIFIED')

    def proof(self):
        # Explicit official session sequence includes a synthetic holiday gap.
        days=['20261019','20261020','20261021','20261023','20261026','20261027','20261028','20261029','20261030','20261103']
        previous='20261016';reports=[]
        for day in days:
            cal=official(day,previous);end=datetime.fromisoformat(cal['end'])
            report=daily_report(cal,samples(cal),now=end+timedelta(minutes=30),audit=audit(cal))
            reports.append({'report':report,'report_sha256':digest(report)});previous=day
        return {'schema':'paper-stability-ten-session-proof-v1','generated_at':self.now.isoformat(),
            'through_session_day':previous,'daily_reports':reports}

    def test_ten_official_sessions_holiday_chain_not_weekday_invention(self):
        p=self.proof();self.assertEqual(validate_ten_session_proof(p,now=self.now)['status'],'verified')
        p['daily_reports'][-1]['report']['official_session']['previous_session_day']='20261102'
        p['daily_reports'][-1]['report_sha256']=digest(p['daily_reports'][-1]['report'])
        self.assertEqual(validate_ten_session_proof(p,now=self.now)['status'],'unverified')

    def test_ten_days_unhashed_forged_partial_or_only_claimed_success_rejected(self):
        for mutate in (lambda p:p['daily_reports'].pop(),
                lambda p:p['daily_reports'][0].update(report_sha256='0'*64),
                lambda p:p['daily_reports'][0]['report'].update(operations_audit={'operator_actions_complete':False})):
            p=self.proof();mutate(p)
            self.assertEqual(validate_ten_session_proof(p,now=self.now)['status'],'unverified')

    def test_weekly_missing_not_zero(self):
        text=weekly_one_page([daily_report(self.cal,[],now=self.now)])
        self.assertIn('ready=미확정',text);self.assertIn('개입=미확정',text)
