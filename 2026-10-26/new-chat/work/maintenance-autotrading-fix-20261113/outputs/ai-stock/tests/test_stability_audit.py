from copy import deepcopy
from datetime import datetime,timedelta
import json,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,patch
from kiwoom_autotrader.trading.stability_audit import ReceiverStabilityAudit,compile_operations_audit
from kiwoom_autotrader.trading.stability_metrics import KST,TickHealth,daily_report,validate_ten_session_proof,digest
from kiwoom_autotrader.trading.paper_experiment_monitor import PaperExperimentMonitor
from tests.test_stability_metrics import official,samples


def actual_fixture_evidence(directory,cal,*,manual=False,gap=False):
    """Real source producer + fake clock; never an operating-day success claim."""
    start=datetime.fromisoformat(cal['start']);end=datetime.fromisoformat(cal['end'])
    writer=ReceiverStabilityAudit(directory);health=TickHealth()
    at=start-timedelta(minutes=1);sequence=0
    while at<=end+timedelta(minutes=1):
        health.begin(at,float(sequence*60));health.end(at,float(sequence*60),success=True)
        if manual and at==start+timedelta(minutes=2):writer.command('stts')
        stream={'ready':True,'coverage_complete':not gap,'gap_count':int(gap and at>=start+timedelta(minutes=2))}
        writer.pulse(at,stream,health.status(),None)
        at+=timedelta(minutes=1);sequence+=1
    path=Path(directory)/('receiver-stability-'+cal['day']+'.jsonl')
    return {'receiver_events':[json.loads(line) for line in path.read_text().splitlines()],'signal_events':[]}


def actual_control_evidence(directory,cal):
    from kiwoom_autotrader.utils.control_audit import initialize_control_audit,read_control_witness
    root=Path(directory);root.mkdir(parents=True,exist_ok=True)
    paths={}
    for group in ('runtime','settings','launchagents'):
        p=root/(group+'.fixture');p.write_text('synthetic stable '+group);paths[group]=[p]
    start=datetime.fromisoformat(cal['start'])-timedelta(minutes=1)
    end=datetime.fromisoformat(cal['end'])+timedelta(minutes=1)
    initialize_control_audit(start,root=root);witnesses=[];at=start
    while at<=end:
        witnesses.append(read_control_witness(at,root=root,protected_paths=paths,pids={'receiver':10,'collector':11,'toss':12}))
        at+=timedelta(minutes=1)
    rows=(root/'observability'/('control-audit-'+cal['day']+'.jsonl')).read_text().splitlines()
    return {'events':[json.loads(line) for line in rows],'witnesses':witnesses}


class AuditTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name).resolve();self.cal=official();self.now=datetime(2026,11,3,16,tzinfo=KST)

    def test_real_producer_compiler_preserves_partial_coverage_external_control_unknown(self):
        evidence=actual_fixture_evidence(self.root,self.cal)
        result=compile_operations_audit(self.cal,evidence,now=self.now)
        self.assertEqual(result['ready_seconds'],23400);self.assertIsNone(result['receiver_restarts'])
        self.assertEqual(result['authenticated_command_interventions'],0)
        self.assertIsNone(result['manual_interventions']);self.assertFalse(result['operator_actions_complete'])
        daily=daily_report(self.cal,samples(self.cal),now=self.now,audit=result)
        self.assertEqual(daily['status'],'UNVERIFIED');self.assertIn('partial_operations_audit',daily)

    def test_actual_registered_controls_and_drift_witness_enable_complete_proof(self):
        evidence=actual_fixture_evidence(self.root/'receiver',self.cal)
        evidence['control_evidence']=actual_control_evidence(self.root/'controls',self.cal)
        result=compile_operations_audit(self.cal,evidence,now=self.now)
        self.assertTrue(result['coverage_complete']);self.assertEqual(result['manual_interventions'],0)
        daily=daily_report(self.cal,samples(self.cal),now=self.now,audit=result)
        self.assertEqual(daily['status'],'PASS')
        # An unexplained external change cannot manufacture an intervention-free day.
        key=next(iter(evidence['control_evidence']['witnesses'][-1]['protected_hashes']))
        evidence['control_evidence']['witnesses'][-1]['protected_hashes'][key]='f'*64
        bad=compile_operations_audit(self.cal,evidence,now=self.now)
        self.assertFalse(bad['coverage_complete']);self.assertIsNone(bad['manual_interventions'])

    def test_dropouts_and_holes_and_tampering_not_zeroed(self):
        e=actual_fixture_evidence(self.root/'one',self.cal)
        for mutate in (lambda x:x['receiver_events'].pop(10),
                lambda x:x['receiver_events'][0]['event'].update(ready=False),
                lambda x:x['receiver_events'][-1]['event'].update(signal_success_count=1)):
            changed=deepcopy(e);mutate(changed)
            with self.assertRaises(ValueError):compile_operations_audit(self.cal,changed,now=self.now)
        gap=actual_fixture_evidence(self.root/'two',self.cal,gap=True)
        with self.assertRaises(ValueError):compile_operations_audit(self.cal,gap,now=self.now)

    def test_operator_command_count_is_real_but_global_zero_not_claimed(self):
        e=actual_fixture_evidence(self.root,self.cal,manual=True)
        result=compile_operations_audit(self.cal,e,now=self.now)
        self.assertEqual(result['authenticated_command_interventions'],1)
        self.assertIsNone(result['manual_interventions'])

    def test_failure_latches_writer_coverage_without_blocking_exit_loop(self):
        writer=ReceiverStabilityAudit(self.root/'audit')
        with patch('kiwoom_autotrader.trading.stability_audit.os.open',side_effect=OSError):
            result=writer.pulse(self.now,{}, {}, None)
        self.assertTrue(result['coverage_failed'])
        self.assertTrue(writer.pulse(self.now+timedelta(minutes=1),{}, {}, None)['coverage_failed'])

    def test_new_day_does_not_reuse_previous_daily_maximum_tick_gap(self):
        h=TickHealth();h.begin(self.now,0);h.end(self.now,0,success=False)
        tomorrow=self.now+timedelta(days=1);h.begin(tomorrow,86400);h.end(tomorrow,86400,success=True)
        self.assertEqual(h.status()['day'],'20261104');self.assertEqual(h.status()['failed_ticks_day'],0)
        self.assertEqual(h.status()['maximum_start_gap_seconds_day'],0)

    def test_actual_monitor_wrapper_exports_completed_and_failed_tick(self):
        clock=[self.now]
        monitor=PaperExperimentMonitor(now=lambda:clock[0],store_path=self.root/'unused.sqlite3')
        monitor._tick=Mock(return_value=None);session=SimpleNamespace(mode='paper')
        monitor.tick(session,Mock());clock[0]+=timedelta(seconds=130)
        monitor._tick=Mock(side_effect=RuntimeError('fixture'))
        with self.assertRaises(RuntimeError):monitor.tick(session,Mock())
        status=monitor.status()['tick_health'];self.assertEqual(status['completed_ticks'],2)
        self.assertEqual(status['failed_ticks'],1);self.assertEqual(status['last_tick_completed_at'],clock[0].isoformat())
