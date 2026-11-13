"""Pure private fixtures; no approval issuance or operational access."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import tempfile
from unittest import TestCase
from zoneinfo import ZoneInfo

from kiwoom_autotrader.utils import runtime_deployment_guard as guard

KST=ZoneInfo('Asia/Seoul')


class CodeDeploymentGuardTests(TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup);self.root=Path(tmp.name).resolve()
        self.now=datetime(2026,11,3,15,45,tzinfo=KST)
        self.manifest={'__main__.py':'a'*64,'utils/module.py':'b'*64}
        self.state=self.root/'state.json';self.write(self.state,{'pid':123,'automation_status':{'mode':'paper'}})
        self.claim=self.root/'claim.json';self.calendar=self.root/'calendar.json';self.approval=self.root/'approval.json'
        self.start=self.now.replace(hour=9,minute=0);self.end=self.now.replace(hour=15,minute=30)
        self.observed=self.now.replace(hour=8,minute=50)
        self.envelope={'schema':'toss-decoded-response-evidence-v1',
            'request':{'endpoint_key':'market_calendar_kr','market_country':'KR','params':{'date':'2026-11-03'}},
            'result':{'status':'ok','source':'toss_openapi','endpoint_key':'market_calendar_kr','observed_at':self.observed.isoformat(),
                'request_started_at':(self.observed-timedelta(seconds=1)).isoformat(),
                'response_sha256':'c'*64,'response':{'result':{'today':{'date':'2026-11-03',
                    'integrated':{'regularMarket':{'startTime':self.start.isoformat(),'endTime':self.end.isoformat()}}}}}}}
        self.claim_value={'schema':'toss-dart-refresh-slot-claim-v1','session_day':'2026-11-03',
            'claimed_at':(self.observed+timedelta(seconds=2)).isoformat(),'calendar_source_sha256':'c'*64,
            'regular_start':self.start.isoformat(),'regular_end':self.end.isoformat()}
        self.value={'schema':'runtime-code-deployment-approval-v1','action':'code_deploy','scope':'receiver_only',
            'mode':'paper','receiver_pid':123,'source_manifest_sha256':guard.manifest_sha256(self.manifest),
            'issued_at':(self.now-timedelta(minutes=1)).isoformat(),'expires_at':(self.now+timedelta(minutes=10)).isoformat(),
            'kind':'after-close-code-approval','user_approval':{'kind':'explicit_code_deployment',
                'reference':'fixture-user-approval-reference','text':'장후 검증된 코드 배포를 별도로 승인합니다.'},
            'official_session':{}}
        self.refresh_proof()

    def write(self,path,value):
        path.write_text(json.dumps(value,ensure_ascii=False));path.chmod(0o600)
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def refresh_proof(self):
        self.value['official_session']={'claim_path':str(self.claim),'claim_sha256':self.write(self.claim,self.claim_value),
            'calendar_path':str(self.calendar),'calendar_file_sha256':self.write(self.calendar,self.envelope),
            'calendar_source_sha256':'c'*64}
        self.write(self.approval,self.value)

    def check(self,**kwargs):
        return guard.assert_code_deployment(self.approval,source_manifest=self.manifest,
            receiver_pid=123,receiver_state_path=self.state,scope='receiver_only',now=kwargs.pop('now',self.now),**kwargs)

    def test_after1540_official_close_exact_manifest_and_explicit_code_approval(self):
        result=self.check();self.assertEqual(result['kind'],'after-close-code-approval')
        self.assertEqual(result['source_manifest_sha256'],guard.manifest_sha256(self.manifest))
        self.assertNotIn('text',result);self.assertNotIn('reference',result)
        self.assertEqual(self.check(now=self.now.astimezone(timezone.utc))['approval_sha256'],result['approval_sha256'])

    def test_current_day_normal_approval_never_authorizes_intraday_or_1539(self):
        for minute in (datetime(2026,11,3,10,0,tzinfo=KST),self.now.replace(minute=39)):
            self.value.update(issued_at=(minute-timedelta(seconds=1)).isoformat(),expires_at=(minute+timedelta(minutes=5)).isoformat())
            self.write(self.approval,self.value)
            with self.assertRaisesRegex(guard.CodeDeploymentDenied,'TUE_THU_OFFICIAL_OUTSIDE_SESSION_WINDOW_REQUIRED'):self.check(now=minute)

    def test_explicit_intraday_code_token_bound_to_session_pid_source_and_scope(self):
        self.now=self.now.replace(hour=10,minute=0)
        self.value.update(kind='intraday-code-token',intraday_code_change_explicit=True,
            issued_at=(self.now-timedelta(seconds=1)).isoformat(),expires_at=(self.now+timedelta(minutes=5)).isoformat())
        self.value['user_approval']['text']='장중 해당 수신기 코드 배포를 별도로 승인합니다.'
        self.write(self.approval,self.value)
        self.assertEqual(self.check()['kind'],'intraday-code-token')
        self.value['intraday_code_change_explicit']=False;self.write(self.approval,self.value)
        with self.assertRaisesRegex(guard.CodeDeploymentDenied,'INTRADAY_CODE_TOKEN_REQUIRED'):self.check()

    def test_restart_old_baseline_wrong_manifest_pid_scope_action_and_expiry_rejected(self):
        original=deepcopy(self.value)
        changes=[{'action':'restart'}, {'schema':'paper-intraday-deployment-authorization-v1'},
            {'source_manifest_sha256':'d'*64}, {'receiver_pid':999}, {'scope':'receiver_and_collector'},
            {'expires_at':self.now.isoformat()}, {'issued_at':(self.now+timedelta(seconds=1)).isoformat()},
            {'mode':'real'}, {'user_approval':{'kind':'restart_only','reference':'fixture-ref','text':'기존 수신기를 재시작하세요.'}}]
        for change in changes:
            with self.subTest(change=change):
                self.value={**deepcopy(original),**change};self.write(self.approval,self.value)
                with self.assertRaises(guard.CodeDeploymentDenied):self.check()

    def test_missing_stale_or_different_official_session_and_changed_file_rejected(self):
        self.claim_value['calendar_source_sha256']='d'*64;self.refresh_proof()
        with self.assertRaisesRegex(guard.CodeDeploymentDenied,'OFFICIAL_CURRENT_SESSION_UNVERIFIED'):self.check()
        self.claim_value['calendar_source_sha256']='c'*64;self.claim_value['session_day']='2026-11-02';self.refresh_proof()
        with self.assertRaises(guard.CodeDeploymentDenied):self.check()
        self.claim_value['session_day']='2026-11-03';self.refresh_proof()
        self.calendar.write_text('{}')
        with self.assertRaises(guard.CodeDeploymentDenied):self.check()

    def test_delayed_official_close_is_not_guessed_from_1530(self):
        late=self.end.replace(hour=16)
        self.claim_value['regular_end']=late.isoformat()
        self.envelope['result']['response']['result']['today']['integrated']['regularMarket']['endTime']=late.isoformat()
        self.refresh_proof()
        with self.assertRaisesRegex(guard.CodeDeploymentDenied,'TUE_THU_OFFICIAL_OUTSIDE_SESSION_WINDOW_REQUIRED'):self.check()

    def move_fixture(self, moment, *, observed=None, start_hour=9, end_hour=15, end_minute=30):
        self.now=moment
        self.observed=observed or moment.replace(hour=7,minute=30,second=0)
        self.start=moment.replace(hour=start_hour,minute=0,second=0)
        self.end=moment.replace(hour=end_hour,minute=end_minute,second=0)
        result=self.envelope['result'];day=moment.date().isoformat()
        self.envelope['request']['params']['date']=day
        result.update(observed_at=self.observed.isoformat(),request_started_at=(self.observed-timedelta(seconds=1)).isoformat())
        result['response']['result']['today']={'date':day,'integrated':{'regularMarket':{
            'startTime':self.start.isoformat(),'endTime':self.end.isoformat()}}}
        self.claim_value.update(session_day=day,claimed_at=(self.observed+timedelta(seconds=1)).isoformat(),
            regular_start=self.start.isoformat(),regular_end=self.end.isoformat())
        self.value.update(kind='scheduled-code-approval',issued_at=(moment-timedelta(seconds=1)).isoformat(),
            expires_at=(moment+timedelta(minutes=5)).isoformat())
        self.refresh_proof()

    def test_normal_clock_weekday_boundaries_are_kst_calendar_dates(self):
        for day,hour,minute,second,accepted in [
            (2,16,0,0,False), (3,8,29,59,True), (3,8,30,0,False),
            (3,15,39,59,False), (3,15,40,0,True), (3,23,50,0,True),
            (4,0,10,0,False), (4,16,0,0,False), (5,8,29,59,True),
            (5,16,0,0,True), (6,16,0,0,False), (7,16,0,0,False)]:
            with self.subTest(day=day,hour=hour,minute=minute,second=second):
                now=datetime(2026,11,day,hour,minute,second,tzinfo=KST)
                self.move_fixture(now,observed=now-timedelta(minutes=1))
                if accepted:self.assertTrue(self.check()['window']['normal_window'])
                else:
                    with self.assertRaises(guard.CodeDeploymentDenied):self.check()

    def test_preopen_requires_fresh_current_day_calendar_never_previous_day_plan(self):
        now=self.now.replace(hour=8,minute=20)
        self.move_fixture(now,observed=now-timedelta(minutes=31))
        with self.assertRaises(guard.CodeDeploymentDenied):self.check()
        self.move_fixture(now,observed=now-timedelta(days=1))
        with self.assertRaises(guard.CodeDeploymentDenied):self.check()
        self.move_fixture(now,observed=now-timedelta(minutes=1))
        self.value['kind']='after-close-code-approval';self.write(self.approval,self.value)
        with self.assertRaisesRegex(guard.CodeDeploymentDenied,'AFTER_CLOSE_APPROVAL'):self.check()

    def test_official_holiday_absence_and_delayed_close_do_not_become_normal_window(self):
        self.move_fixture(self.now,end_hour=16,end_minute=30)
        with self.assertRaises(guard.CodeDeploymentDenied):self.check()
        self.envelope['result']['response']['result']['today']['integrated']['regularMarket']=None
        self.refresh_proof()
        with self.assertRaises(guard.CodeDeploymentDenied):self.check()

    def test_other_windows_require_separate_source_bound_short_exception_token(self):
        self.move_fixture(datetime(2026,11,4,16,0,tzinfo=KST))
        with self.assertRaises(guard.CodeDeploymentDenied):self.check()
        self.value.update(kind='exception-code-token',exception_code_change_explicit=True)
        self.value['user_approval']['text']='이번 수신기 코드 배포 시간 예외를 별도로 승인합니다.'
        self.write(self.approval,self.value)
        self.assertFalse(self.check()['window']['normal_window'])
        self.value['exception_code_change_explicit']=False;self.write(self.approval,self.value)
        with self.assertRaisesRegex(guard.CodeDeploymentDenied,'EXPLICIT_CODE_WINDOW_EXCEPTION_REQUIRED'):self.check()

    def test_throughput_rejects_numeric_claim_without_continuous_and_operator_proof(self):
        from kiwoom_autotrader.trading.stability_metrics import daily_report,digest
        rows=[];previous='20261019'
        # Fixture dates are explicitly supplied official sessions, never inferred
        # by the production validator from Monday-Friday arithmetic.
        for text in ('20261020','20261021','20261022','20261023','20261026',
                     '20261027','20261028','20261029','20261030','20261102'):
            day=datetime.strptime(text,'%Y%m%d').replace(tzinfo=KST)
            start=day.replace(hour=9);end=day.replace(hour=15,minute=30)
            official={'day':text,'previous_session_day':previous,'start':start.isoformat(),
                'end':end.isoformat(),'verified':True,'calendar_sha256':'f'*64}
            audit={'schema':'paper-stability-operations-audit-v1','session_day':text,
                'coverage_complete':True,'covered_from':start.isoformat(),'covered_through':end.isoformat(),
                'calendar_sha256':'f'*64,'source_inventory':{'fixture-operations':'b'*64},
                'ready_transitions_complete':True,'tick_history_complete':True,'signal_outcomes_complete':True,
                'operator_actions_complete':True,'process_history_complete':True,'ready_seconds':23400,
                'maximum_tick_gap_seconds':30,'confirmed_operational_lost_signals':0,'manual_interventions':0,'receiver_restarts':0}
            samples=[{'schema':'paper-readonly-watchdog-sample-v1','observed_at':(start+timedelta(minutes=i)).isoformat(),
                'stream_ready':True} for i in range(390)]
            report=daily_report(official,samples,now=end+timedelta(minutes=30),audit=audit)
            rows.append({'report':report,'report_sha256':digest(report)});previous=text
        proof={'schema':'paper-stability-ten-session-proof-v1','generated_at':self.now.isoformat(),
            'through_session_day':previous,'daily_reports':rows}
        path=self.root/'stability.json'
        self.value.update(change_class='throughput',stability_evidence={'path':str(path),'sha256':self.write(path,proof)})
        self.write(self.approval,self.value)
        # Missing continuous originals/external operator coverage cannot become
        # ten passing days by supplying totals and arbitrary inventory hashes.
        with self.assertRaisesRegex(guard.CodeDeploymentDenied,'TEN_SESSION_STABILITY_REQUIRED'):self.check()
        proof['daily_reports'].pop()
        self.value['stability_evidence']['sha256']=self.write(path,proof);self.write(self.approval,self.value)
        with self.assertRaisesRegex(guard.CodeDeploymentDenied,'TEN_SESSION_STABILITY_REQUIRED'):self.check()


    def test_actual_linked_proof_must_end_at_latest_completed_official_session(self):
        from tests.test_stability_metrics import StabilityTests
        from kiwoom_autotrader.trading.stability_metrics import validate_ten_session_proof
        fixture=StabilityTests();fixture.setUp();proof=fixture.proof()
        self.assertEqual(validate_ten_session_proof(proof,now=fixture.now)['status'],'verified')
        self.move_fixture(datetime(2026,11,3,16,5,tzinfo=KST),observed=datetime(2026,11,3,16,4,tzinfo=KST))
        path=self.root/'actual-stability.json'
        path.write_text(json.dumps(proof,ensure_ascii=False,separators=(',',':')));path.chmod(0o600)
        self.assertGreater(path.stat().st_size,16*1024*1024)
        self.assertLessEqual(path.stat().st_size,32*1024*1024,'actual canonical proof exceeds bounded guard read cap')
        self.value.update(change_class='throughput',stability_evidence={'path':str(path),'sha256':hashlib.sha256(path.read_bytes()).hexdigest()})
        self.write(self.approval,self.value)
        self.assertEqual(self.check()['throughput_stability']['latest_completed_official_session_day'],'20261103')
        # The provider explicitly supplies a holiday gap; no Mon-Fri inference.
        self.move_fixture(datetime(2026,11,5,8,20,tzinfo=KST),observed=datetime(2026,11,5,8,19,tzinfo=KST))
        body=self.envelope['result']['response']['result']
        body['previousBusinessDay']={'date':'2026-11-03'};self.refresh_proof()
        self.assertEqual(self.check()['throughput_stability']['through_session_day'],'20261103')
        # Same valid ten PASS reports become stale when the official previous
        # session is newer, even if their chain still passes the validator.
        body['previousBusinessDay']={'date':'2026-11-04'};self.refresh_proof()
        with self.assertRaisesRegex(guard.CodeDeploymentDenied,'NOT_LATEST_COMPLETED'):self.check()
        del body['previousBusinessDay'];self.refresh_proof()
        with self.assertRaisesRegex(guard.CodeDeploymentDenied,'LATEST_COMPLETED_OFFICIAL_SESSION_UNVERIFIED'):self.check()
        self.value.pop('change_class');self.value.pop('stability_evidence');self.write(self.approval,self.value)
        self.assertIsNone(self.check()['throughput_stability'])  # maintenance contract unchanged
        self.value.update(change_class='throughput',stability_evidence={'path':str(path),'sha256':hashlib.sha256(path.read_bytes()).hexdigest()})
        self.move_fixture(datetime(2026,11,5,16,5,tzinfo=KST),observed=datetime(2026,11,5,16,4,tzinfo=KST))
        with self.assertRaisesRegex(guard.CodeDeploymentDenied,'NOT_LATEST_COMPLETED'):self.check()

    def test_normal_quota_one_attempt_per_tuesday_and_thursday_failed_claim_is_not_reusable(self):
        directory=self.root/'attempts';proof=self.check()
        first=guard.claim_code_deployment(directory,proof,now=self.now)
        self.assertTrue(guard.verify_deployment_claim(first,self.check())['normal_quota_consumed'])
        self.assertEqual(guard.verify_deployment_claim(first,self.check())['attempt_id'],first['attempt_id'])
        for changed in (False,True):
            if changed:
                self.value['user_approval']['reference']='second-separately-approved-attempt'
                self.write(self.approval,self.value)
            with self.assertRaisesRegex(guard.CodeDeploymentDenied,'DATE_ALREADY_CLAIMED'):
                guard.claim_code_deployment(directory,self.check(),now=self.now)
        self.move_fixture(datetime(2026,11,5,16,0,tzinfo=KST),observed=datetime(2026,11,5,15,59,tzinfo=KST))
        second=guard.claim_code_deployment(directory,self.check(),now=self.now)
        self.assertNotEqual(first['path'],second['path'])
        self.assertEqual(len(list(directory.glob('*normal.claim.json'))),2)

    def test_second_attempt_requires_distinct_explicit_exception_and_exception_consumed_once(self):
        directory=self.root/'attempts'
        guard.claim_code_deployment(directory,self.check(),now=self.now)
        self.value.update(kind='exception-code-token',exception_code_change_explicit=True)
        self.value['user_approval']['text']='두 번째 수신기 코드 배포 예외를 명시적으로 승인합니다.'
        self.write(self.approval,self.value);proof=self.check()
        extra=guard.claim_code_deployment(directory,proof,now=self.now)
        self.assertFalse(guard.verify_deployment_claim(extra,proof)['normal_quota_consumed'])
        with self.assertRaisesRegex(guard.CodeDeploymentDenied,'EXCEPTION_ALREADY_CONSUMED'):
            guard.claim_code_deployment(directory,proof,now=self.now)

    def test_claim_tamper_and_old_permission_fail_without_new_claim(self):
        directory=self.root/'attempts';proof=self.check()
        with self.assertRaisesRegex(guard.CodeDeploymentDenied,'FRESH_CODE'):
            guard.claim_code_deployment(directory,proof,now=self.now+timedelta(seconds=6))
        self.assertFalse(directory.exists())
        attempt=guard.claim_code_deployment(directory,proof,now=self.now)
        path=Path(attempt['path']);value=json.loads(path.read_text());value['receiver_pid']=456;self.write(path,value)
        with self.assertRaisesRegex(guard.CodeDeploymentDenied,'ATTEMPT_CHANGED'):
            guard.verify_deployment_claim(attempt,proof)

    def test_app_and_work_claims_share_one_directory_across_concurrent_attempts(self):
        from concurrent.futures import ThreadPoolExecutor
        directory=self.root/'attempts';proof=self.check()
        def attempt():
            try:return guard.claim_code_deployment(directory,proof,now=self.now)
            except guard.CodeDeploymentDenied as error:return str(error)
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(lambda _:attempt(),range(2)))
        self.assertEqual(sum(isinstance(r,dict) for r in results),1)
        self.assertEqual(len(list(directory.glob('*.claim.json'))),1)

    def test_current_receiver_mode_and_pid_and_private_token_are_required(self):
        self.write(self.state,{'pid':124,'automation_status':{'mode':'paper'}})
        with self.assertRaisesRegex(guard.CodeDeploymentDenied,'RECEIVER_IDENTITY'):self.check()
        self.write(self.state,{'pid':123,'automation_status':{'mode':'real'}})
        with self.assertRaises(guard.CodeDeploymentDenied):self.check()
        self.write(self.state,{'pid':123,'automation_status':{'mode':'paper'}})
        self.approval.chmod(0o644)
        with self.assertRaisesRegex(guard.CodeDeploymentDenied,'EVIDENCE_UNREADABLE'):self.check()

    def test_even_hash_bound_calendar_requires_real_producer_origin_request_day_and_chronology(self):
        original=deepcopy(self.envelope)
        for field,value in [('source','unverified_provider'),('request_started_at',(self.observed+timedelta(seconds=1)).isoformat()),
                            ('request_started_at',(self.observed-timedelta(days=1)).isoformat())]:
            with self.subTest(field=field,value=value):
                self.envelope=deepcopy(original);self.envelope['result'][field]=value;self.refresh_proof()
                with self.assertRaises(guard.CodeDeploymentDenied):self.check()
        self.envelope=deepcopy(original);self.envelope['request']['params']['date']='2026-11-02';self.refresh_proof()
        with self.assertRaises(guard.CodeDeploymentDenied):self.check()
        self.envelope=deepcopy(original);del self.envelope['result']['source'];self.refresh_proof()
        with self.assertRaises(guard.CodeDeploymentDenied):self.check()
