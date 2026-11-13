"""Private atomic policy fixtures; no runtime/config/network access."""
from copy import deepcopy
from datetime import datetime,timedelta
import hashlib
import json
from pathlib import Path
import tempfile
from unittest import TestCase
from unittest.mock import patch
from zoneinfo import ZoneInfo

from kiwoom_autotrader.config.paper_hot_policy import ApprovedPolicyReloader,HotPolicyRejected,field_inventory
from kiwoom_autotrader.config.settings import effective_paper_experiment

KST=ZoneInfo('Asia/Seoul')


class HotPolicyTests(TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup);self.root=Path(tmp.name).resolve()
        self.path=self.root/'pending.json';self.approval=self.root/'approval.json'
        self.now=datetime(2026,11,3,16,0,tzinfo=KST)
        self.reader=ApprovedPolicyReloader(self.path,self.approval,receiver_pid=123,source_manifest_sha256='a'*64)
        self.pending={'schema':'paper-hot-policy-pending-v1','profile':'paper_experiment_v1','mode':'paper',
            'version':1,'previous_policy_sha256':'0'*64,'available_at':(self.now-timedelta(seconds=3)).isoformat(),
            'effective_at':self.now.isoformat(),'changes':{'dynamic_candidate_limit':15,'entry_pool_limit':30,'entry_paused_arms':['EXP_B']}}
        self.permission={'schema':'paper-hot-policy-approval-v1','action':'hot_policy_apply','mode':'paper',
            'scope':'receiver_entry_policy_only','receiver_pid':123,'source_manifest_sha256':'a'*64,
            'issued_at':(self.now-timedelta(seconds=2)).isoformat(),'expires_at':(self.now+timedelta(minutes=10)).isoformat(),
            'user_approval':{'kind':'explicit_hot_policy_change','reference':'fixture-policy-approval','text':'이 설정 변경을 별도로 승인합니다.'}}
        self.save()

    def write(self,path,value):
        path.write_text(json.dumps(value,sort_keys=True));path.chmod(0o600)
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def save(self):
        self.permission['policy_sha256']=self.write(self.path,self.pending)
        self.write(self.approval,self.permission)

    def test_next_cycle_activation_versioned_snapshot_then_read_is_immutable(self):
        first=self.reader.next_cycle(self.now);self.assertTrue(first['entry_authorized'])
        self.reader.assert_entry_current(first,self.now,'EXP_A')
        with self.assertRaisesRegex(HotPolicyRejected,'ARM_ENTRY_PAUSED'):self.reader.assert_entry_current(first,self.now,'EXP_B')
        self.pending.update(version=2,previous_policy_sha256=first['policy_sha256'])
        self.pending['changes']['dynamic_candidate_limit']=16;self.save()
        self.assertEqual(self.reader.status()['changes']['dynamic_candidate_limit'],15)
        with self.assertRaisesRegex(HotPolicyRejected,'CHANGED_BEFORE_SEND'):self.reader.assert_entry_current(first,self.now,'EXP_A')
        second=self.reader.next_cycle(self.now)
        self.assertEqual(second['version'],2);self.assertEqual(second['changes']['dynamic_candidate_limit'],16)
        self.assertEqual(first['version'],1)

    def test_missing_wrong_source_pid_expired_hash_future_and_restart_approval_block(self):
        for field,value in [('receiver_pid',124),('source_manifest_sha256','b'*64),('action','restart'),
            ('expires_at',self.now.isoformat()),('policy_sha256','b'*64)]:
            with self.subTest(field=field):
                original=deepcopy(self.permission);self.permission[field]=value;self.write(self.approval,self.permission)
                self.assertFalse(self.reader.next_cycle(self.now)['entry_authorized'])
                self.permission=original;self.save()
        self.pending['effective_at']=(self.now+timedelta(seconds=1)).isoformat();self.save()
        self.assertFalse(self.reader.next_cycle(self.now)['entry_authorized'])

    def test_financial_profile_new_arms_threshold_rewrites_and_expansion_fail_closed(self):
        base=deepcopy(self.pending)
        for change in ({'max_concurrent_positions':60},{'per_buy_budget_ceiling_krw':100000},
            {'active_arms':['EXP_B15']},{'arm_thresholds':{'EXP_B':1.5}},
            {'dynamic_candidate_limit':150},{'entry_pool_limit':80},{'entry_paused_arms':['EXP_R']}):
            with self.subTest(change=change):
                self.pending=deepcopy(base);self.pending['changes']=change;self.save()
                self.assertFalse(self.reader.next_cycle(self.now)['entry_authorized'])
        profile=effective_paper_experiment({'paper_experiment_enabled':True,'paper_experiment_s5_enabled':True},'paper')
        self.assertEqual(profile['max_concurrent_positions'],40)
        self.assertEqual(profile['per_buy_budget_ceiling_krw'],300000)
        self.assertEqual(profile['assignment_profile'],'native-six-s5-v1')

    def test_corrupt_pair_preserves_active_body_but_blocks_new_entries(self):
        first=self.reader.next_cycle(self.now);self.path.write_text('{')
        denied=self.reader.next_cycle(self.now)
        self.assertFalse(denied['entry_authorized']);self.assertEqual(denied['changes'],first['changes'])
        self.assertEqual(denied['policy_sha256'],first['policy_sha256'])

    def test_mid_read_replacement_and_same_version_replay_are_rejected(self):
        first=self.reader.next_cycle(self.now)
        self.pending['changes']['entry_pool_limit']=29;self.save()
        self.assertFalse(self.reader.next_cycle(self.now)['entry_authorized'])
        from kiwoom_autotrader.config import paper_hot_policy as module
        real=module._read;calls=[]
        def replaced(path,**kw):
            result=real(path,**kw);calls.append(path)
            return (result[0],'f'*64) if len(calls)==3 else result
        with patch.object(module,'_read',side_effect=replaced):
            self.assertEqual(self.reader.next_cycle(self.now)['reason'],'HOT_POLICY_ATOMIC_GENERATION_CHANGED')
        self.assertEqual(first['version'],1)

    def test_activation_approval_expiry_does_not_silently_undo_an_accepted_policy(self):
        self.reader.next_cycle(self.now)
        self.assertTrue(self.reader.next_cycle(self.now+timedelta(hours=1))['entry_authorized'])
        self.assertIn('active_arms',field_inventory()['new_versioned_adapter_required'])
