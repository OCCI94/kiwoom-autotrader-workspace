"""Preopen and ORB actual-grid regressions. All files/session calls are fixtures."""
from copy import deepcopy
from datetime import datetime,timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock,patch
from zoneinfo import ZoneInfo
import hashlib,json,tempfile
from kiwoom_autotrader.trading.paper_experiment_inputs import ExperimentInputs
from kiwoom_autotrader.trading.paper_experiment_calendar import preopen_plan
from kiwoom_autotrader.trading.paper_experiment_store import ExperimentStore
from kiwoom_autotrader.trading.paper_experiment_volume_history import SavedSameWindowHistory,validate_history,digest
from kiwoom_autotrader.trading.paper_experiment_scheduler import ExtendedScheduler
from tests.paper_experiment_history_fixture import history
from tests.test_paper_experiment_scheduler import daily

KST=ZoneInfo('Asia/Seoul');NOW=datetime(2026,11,3,8,40,tzinfo=KST);SYMBOL='005930'

class InputReadinessTests(TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name).resolve()
        self.clock=NOW;self.session=SimpleNamespace(mode='paper');self.store=ExperimentStore(self.root/'assignments.sqlite3')
        self.plan={'schema':'paper-experiment-preopen-plan-v1','session_day':'20261103','previous_session_day':'20261102',
            'available_at':'2026-11-02T15:20:00+09:00','live_regular_session_verified':False}
        patcher=patch('kiwoom_autotrader.trading.paper_experiment_inputs.session_identity',return_value=('paper','test'))
        patcher.start();self.addCleanup(patcher.stop)
        self.requester=Mock(side_effect=self.response)
        self.inputs=ExperimentInputs(self.session,SimpleNamespace(),self.store,clock=lambda:self.clock,
            requester=self.requester,preopen_plan_reader=lambda now:self.plan,history_reader=SavedSameWindowHistory(self.root/'history'))
    def response(self,api,body,output,**kw):
        self.assertEqual(api,'ka10081');rows=[]
        for r in daily(SYMBOL,NOW)['rows']:
            rows.append({'dt':r['date'],'open_pric':str(r['open']),'high_pric':str(r['high']),'low_pric':str(r['low']),
                'cur_prc':str(r['close']),'trde_qty':str(r['volume']),'trde_prica':'3000','pred_pre_sig':'2'})
        return {'symbol':SYMBOL,'rows':rows,'source_sha256':'a'*64,'observed_at':self.clock.isoformat()}
    def test_preopen_daily_precedes_master_and_preserves_original_time_into_open(self):
        self.assertTrue(self.inputs.prepare_preopen_one([SYMBOL]))
        self.assertEqual(self.requester.call_args.args[1]['base_dt'],'20261102')
        self.assertEqual(self.inputs.master_complete,set())
        cached=self.inputs.daily(SYMBOL);self.assertEqual(cached['available_at'],NOW.isoformat())
        self.assertEqual(cached['observation_phase'],'preopen');self.assertFalse(cached['preopen_observation_relabelled'])
        self.clock=NOW.replace(hour=9,minute=5)
        self.assertEqual(self.inputs.daily(SYMBOL),cached)
        self.assertFalse(self.inputs.prepare_preopen_one([SYMBOL]));self.assertEqual(self.requester.call_count,1)
    def test_unverified_calendar_or_outside_window_never_fetches(self):
        self.plan=None;self.assertFalse(self.inputs.prepare_preopen_one([SYMBOL]));self.requester.assert_not_called()
        self.plan={'session_day':'20261102'};self.assertFalse(self.inputs.prepare_preopen_one([SYMBOL]));self.requester.assert_not_called()
        self.clock=NOW.replace(hour=8,minute=29);self.assertFalse(self.inputs.prepare_preopen_one([SYMBOL]))
    def test_wrong_previous_session_delayed_response_or_corrupt_cache_not_accepted(self):
        self.plan['previous_session_day']='20261030'
        self.assertTrue(self.inputs.prepare_preopen_one([SYMBOL]));self.assertIsNone(self.inputs.daily(SYMBOL))
        self.inputs.daily_failed.clear();self.plan['previous_session_day']='20261102'
        def slow(*a,**kw):self.clock=NOW.replace(minute=56);return self.response(*a,**kw)
        self.requester.side_effect=slow;self.assertTrue(self.inputs.prepare_preopen_one([SYMBOL]));self.assertIsNone(self.inputs.daily(SYMBOL))
    def test_real_mode_preopen_never_reads_calendar_or_api(self):
        self.session.mode='real'
        with self.assertRaises(ValueError):self.inputs.prepare_preopen_one([SYMBOL])
        self.requester.assert_not_called()
    def _archive(self,omit=(),mutate=None):
        reference=history(daily(SYMBOL,NOW),SYMBOL);root=self.root/'history'
        for row in reference['rows']:
            day=row['date']
            if day in omit:continue
            path=root/day/'minutes';path.mkdir(parents=True)
            response={'symbol':SYMBOL,'api_id':'ka10080','requested_exchange':'KRX','environment':'paper','wire_sha256':'4'*64,
                'request_started_at':row['available_at'],'observed_at':row['available_at'],'raw_projected_rows':[],
                'bars':deepcopy(row['minutes'])}
            response['raw_projected_rows']=[{'cntr_tm':datetime.fromisoformat(b['start_at']).strftime('%Y%m%d%H%M%S'),
                **{rk:str(b[k]) for k,rk in [('open','open_pric'),('high','high_pric'),('low','low_pric'),('close','cur_prc'),('volume','trde_qty')]}} for b in row['minutes']]
            if mutate:mutate(day,response)
            raw=json.dumps(response).encode();(path/(SYMBOL+'.json')).write_bytes(raw)
            manifest={'schema':'paper-experiment-postclose-archive-v1','day':day,'status':'completed','minute_completed_symbols':[SYMBOL],
                'completed_at':row['available_at'],'session_start_at':row['minutes'][0]['start_at'],
                'files':[{'symbol':SYMBOL,'relative_path':'ai-stock-research-data/paper-experiment-postclose/'+day+'/minutes/'+SYMBOL+'.json',
                    'sha256':hashlib.sha256(raw).hexdigest(),'observed_at':row['available_at']}]}
            (root/day/'manifest.json').write_text(json.dumps(manifest))
        return root,[r['date'] for r in reference['rows']]
    def test_saved_14_exact_sessions_real_volume_and_pit(self):
        root,days=self._archive();result=SavedSameWindowHistory(root).read(SYMBOL,days,NOW)
        self.assertEqual(result['observed_sessions'],14);self.assertEqual(result['status'],'observed')
        self.assertEqual([r['volume'] for r in result['rows']],[2500]*14)
        self.assertEqual(result['available_at'],'2026-11-02T16:00:00+09:00')
        self.assertEqual(validate_history(result,symbol=SYMBOL,prior_dates=days,now=NOW),result)
        tampered=deepcopy(result);tampered['rows'][0]['minutes'][0]['volume']+=1;tampered['source_sha256']=digest({k:v for k,v in tampered.items() if k!='source_sha256'})
        with self.assertRaisesRegex(ValueError,'volume_recalculation'):validate_history(tampered,symbol=SYMBOL,prior_dates=days,now=NOW)
    def test_thirteen_sessions_stays_unobserved_and_reports_count(self):
        dates=[r['date'] for r in history(daily(SYMBOL,NOW),SYMBOL)['rows']];root,days=self._archive(omit=dates[:1]);result=SavedSameWindowHistory(root).read(SYMBOL,days,NOW)
        self.assertEqual(result['observed_sessions'],13);self.assertEqual(result['missing_sessions'],dates[:1]);self.assertEqual(result['status'],'unobserved')
        with self.assertRaisesRegex(ValueError,'14_sessions'):validate_history(result,symbol=SYMBOL,prior_dates=days,now=NOW)
    def test_one_missing_minute_future_observation_and_hash_tamper_are_not_zero(self):
        dates=[r['date'] for r in history(daily(SYMBOL,NOW),SYMBOL)['rows']]
        root,days=self._archive(mutate=lambda d,r:r['bars'].pop(0) if d==dates[0] else None)
        result=SavedSameWindowHistory(root).read(SYMBOL,days,NOW)
        self.assertEqual(result['observed_sessions'],13);self.assertEqual(result['invalid_sessions'],dates[:1]);self.assertFalse(result['synthetic_bars_created'])
        file=root/days[1]/'minutes'/(SYMBOL+'.json');file.write_bytes(file.read_bytes()+b' ')
        result=SavedSameWindowHistory(root).read(SYMBOL,days,NOW);self.assertEqual(result['observed_sessions'],12)
    def test_saved_official_next_date_claim_required_no_weekday_inference(self):
        archive=self.root/'official';folder=archive/'20261102-1520'/'toss';folder.mkdir(parents=True);claims=self.root/'claims';claims.mkdir()
        def session(day):return {'date':day,'integrated':{'regularMarket':{'startTime':day+'T09:00:00.000+09:00','endTime':day+'T15:30:00.000+09:00'}}}
        env={'schema':'toss-decoded-response-evidence-v1','request':{'endpoint_key':'market_calendar_kr','market_country':'KR'},'result':{'status':'ok','endpoint_key':'market_calendar_kr','observed_at':'2026-11-02T06:20:00.225000+00:00','response_sha256':'a'*64,'response':{'result':{'today':session('2026-11-02'),'nextBusinessDay':session('2026-11-03'),'previousBusinessDay':session('2026-10-30')}}}}
        p=folder/'0001-calendar-current.json';p.write_text(json.dumps(env));self.assertIsNone(preopen_plan(NOW,archive_root=archive,claim_root=claims))
        c=claims/'20261102-1520.claim.json';c.write_text(json.dumps({'schema':'toss-dart-refresh-slot-claim-v1','session_day':'2026-11-02','calendar_source_sha256':'a'*64,'claimed_at':'2026-11-02T15:20:00+09:00','regular_start':'2026-11-02T09:00:00+09:00','regular_end':'2026-11-02T15:30:00+09:00'}));c.chmod(0o600)
        result=preopen_plan(NOW,archive_root=archive,claim_root=claims);self.assertEqual(result['previous_session_day'],'20261102');self.assertFalse(result['live_regular_session_verified'])
        env['result']['response']['result']['nextBusinessDay']['date']='2026-11-04';p.write_text(json.dumps(env));self.assertIsNone(preopen_plan(NOW,archive_root=archive,claim_root=claims))
    def test_checkpoint_no_early_freeze_missing_inputs_null_future_assignment_excluded(self):
        scheduler=ExtendedScheduler(self.inputs,self.store,Mock(),lambda:self.clock)
        self.clock=NOW.replace(hour=9,minute=5,second=20);self.assertIsNone(scheduler.record_checkpoint(self.clock))
        self.clock=self.clock.replace(second=45);saved=scheduler.record_checkpoint(self.clock)
        self.assertIsNone(saved['rows'][0]['input_blocked_count']);self.assertFalse(saved['missing_input_diagnostics_are_zero'])
        self.assertEqual(saved,scheduler.record_checkpoint(self.clock.replace(second=55)))
