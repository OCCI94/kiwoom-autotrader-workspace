"""Real durable control events and read-only witnesses, all in temporary files."""
import copy
from datetime import datetime,timedelta
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from kiwoom_autotrader.utils import control_audit as c
from kiwoom_autotrader.utils import telegram_service as service
from kiwoom_autotrader.utils import account_service


class ControlAuditTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.root=Path(temp.name)
        self.start=datetime(2026,11,3,9,0,tzinfo=c.KST);self.end=self.start+timedelta(minutes=3)
        self.paths={group:[self.root/(group+'.txt')] for group in ('runtime','settings','launchagents')}
        for paths in self.paths.values():paths[0].write_text('original')
        self.pids={'receiver':123,'collector':456,'toss':789}
        self.witnesses=[]
        c.initialize_control_audit(self.start-timedelta(minutes=2),self.root)

    def witness(self,at):
        value=c.read_control_witness(at,root=self.root,protected_paths=self.paths,pids=self.pids)
        self.witnesses.append(value);return value

    def evidence(self):
        raw=(self.root/'observability'/'control-audit-20261103.jsonl').read_bytes()
        return {'events':[json.loads(row) for row in raw.splitlines()],'witnesses':self.witnesses}

    def compile(self):return c.compile_control_audit(self.evidence(),start=self.start,end=self.end,now=self.end)

    def full_witnesses(self):
        for i in range(4):self.witness(self.start+timedelta(minutes=i))

    def test_real_zero_action_chain_and_complete_witnesses_verify_zero(self):
        self.full_witnesses();result=self.compile()
        self.assertEqual(result['status'],'verified');self.assertTrue(result['coverage_complete'])
        self.assertEqual(result['manual_interventions'],0);self.assertEqual(result['receiver_restarts'],0)
        self.assertEqual(result['scope'],'registered_software_controls_with_drift_witness')

    def test_registered_write_real_bytes_explains_hash_drift_and_counts_one(self):
        self.witness(self.start);at=self.start+timedelta(seconds=20)
        with c.audit_control('settings_write','settings',root=self.root,paths=self.paths['settings'],origin='authenticated_user',now=at):
            self.paths['settings'][0].write_text('PRIVATE financial value 123')
        for i in range(1,4):self.witness(self.start+timedelta(minutes=i))
        result=self.compile();self.assertEqual(result['status'],'verified');self.assertEqual(result['manual_interventions'],1)
        raw=json.dumps(self.evidence());self.assertNotIn('PRIVATE',raw);self.assertNotIn(str(self.root),raw)

    def test_sealed_unclassified_preopen_action_before_first_witness_does_not_count(self):
        at=self.start-timedelta(minutes=1)
        with c.audit_control('settings_write','settings',root=self.root,paths=self.paths['settings'],now=at):
            self.paths['settings'][0].write_text('preopen login renewal')
        self.full_witnesses();self.assertEqual(self.compile()['manual_interventions'],0)

    def test_unclassified_action_after_first_preopen_witness_is_unknown(self):
        self.witness(self.start-timedelta(minutes=1))
        with c.audit_control('start','receiver',root=self.root,now=self.start-timedelta(seconds=30)):pass
        self.full_witnesses();self.assertFalse(self.compile()['coverage_complete'])

    def test_unknown_origin_during_session_is_not_zero(self):
        self.witness(self.start)
        with c.audit_control('start','receiver',root=self.root,now=self.start+timedelta(seconds=20)):pass
        for i in range(1,4):self.witness(self.start+timedelta(minutes=i))
        self.assertIsNone(self.compile()['manual_interventions'])

    def test_unexplained_hash_and_pid_changes_are_unknown(self):
        for kind in ('hash','pid'):
            with self.subTest(kind=kind):
                self.witnesses=[];self.witness(self.start)
                if kind=='hash':self.paths['runtime'][0].write_text('external code change')
                else:self.pids['receiver']=999
                for i in range(1,4):self.witness(self.start+timedelta(minutes=i))
                self.assertEqual(self.compile()['status'],'unverified')

    def test_missing_witness_tampered_chain_and_unfinished_action_are_unknown(self):
        self.full_witnesses();good=self.evidence()
        cases=[]
        missing=copy.deepcopy(good);missing['witnesses'].pop(1);cases.append(missing)
        tampered=copy.deepcopy(good);tampered['events'][0]['event']['mode']='real';cases.append(tampered)
        at=self.end
        c._append({'phase':'before','action_id':'unfinished','origin':'authenticated_user','component':'receiver','action':'start','controller_pid':123,'changes':[]},root=self.root,at=at)
        cases.append(self.evidence())
        for value in cases:self.assertEqual(c.compile_control_audit(value,start=self.start,end=self.end,now=self.end)['status'],'unverified')

    def test_automatic_recovery_separate_and_receiver_identity_change_explained(self):
        self.witness(self.start)
        with c.audit_control('start','receiver',root=self.root,origin='automatic_recovery',now=self.start+timedelta(seconds=20)):
            self.pids['receiver']=321
        for i in range(1,4):self.witness(self.start+timedelta(minutes=i))
        result=self.compile();self.assertEqual(result['manual_interventions'],0)
        self.assertEqual(result['automatic_recoveries'],1);self.assertEqual(result['receiver_restarts'],1)

    def test_intent_record_failure_prevents_actual_service_mutation(self):
        paths=service.ServicePaths(self.root/'home')
        with patch.object(c,'_append',side_effect=c.ControlAuditError('disk full')),patch.object(service,'_launchctl') as launch:
            with self.assertRaises(service.TelegramServiceError):service.stop_service(paths)
        launch.assert_not_called();self.assertFalse(paths.state.exists())

    def test_uninitialized_read_only_witness_creates_no_journal(self):
        untouched=self.root/'new'
        with self.assertRaises(FileNotFoundError):c.read_control_witness(self.start,root=untouched,protected_paths=self.paths,pids=self.pids)
        self.assertFalse(untouched.exists())

    def test_existing_runtime_direct_install_without_permission_has_zero_mutations(self):
        paths=service.ServicePaths(self.root/'home');package=paths.runtime/'kiwoom_autotrader'
        package.mkdir(parents=True);(package/'main.py').write_text('unchanged')
        with patch.object(c,'_append') as append,patch.object(service,'stop_service') as stop:
            with self.assertRaises(service.TelegramServiceError):service.install_service(self.root/'source',paths)
        append.assert_not_called();stop.assert_not_called();self.assertFalse((paths.base/'observability').exists())
        self.assertEqual((package/'main.py').read_text(),'unchanged')

    def test_collector_install_cannot_bypass_existing_runtime_approval(self):
        paths=account_service.AccountServicePaths(self.root/'home');(paths.runtime/'kiwoom_autotrader').mkdir(parents=True)
        with patch.object(c,'_append') as append,patch.object(account_service,'stop_collector_service') as stop:
            with self.assertRaises(service.TelegramServiceError):account_service.install_collector_service(self.root/'source',paths)
        append.assert_not_called();stop.assert_not_called()

    def test_failed_first_permission_has_zero_audit_mutations(self):
        paths=service.ServicePaths(self.root/'home')
        def denied():raise ValueError('permission denied')
        with patch.object(c,'_append') as append:
            with self.assertRaisesRegex(ValueError,'permission denied'):
                service.install_service(self.root/'source',paths,before_replace=denied)
        append.assert_not_called();self.assertFalse(paths.base.exists())
