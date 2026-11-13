"""Opt-in, approved pending policy reader. No writes, API, or trading authority.

Only existing observation bounds and entry-only pauses can currently reload.
Financial limits/threshold variants require a versioned evidence adapter; they
are intentionally refused instead of changing the v1 order contract silently.
An operator must separately approve and wire this reader; no default file is
created or loaded by importing this module.
"""
from copy import deepcopy
from datetime import datetime
import re
from threading import RLock

from kiwoom_autotrader.utils.runtime_deployment_guard import _read, _time, KST

ARMS = frozenset(('EXP_R','EXP_A','EXP_B','EXP_B_TP','EXP_E','EXP_C','EXP_D',
    'EXP_ORB_A','EXP_ORB_B','EXP_GDR','EXP_VSS','EXP_LDR','EXP_ETF_B','S5_steady_uptrend_vwap'))
DEFAULT_CHANGES = {'dynamic_candidate_limit':30,'entry_pool_limit':40,'entry_paused_arms':[]}
NEW_ADAPTER_REQUIRED = frozenset(('max_concurrent_positions','max_positions_per_theme',
    'max_overnight_positions','max_vss_positions','unfilled_buy_ttl_seconds',
    'per_buy_budget_ceiling_krw','cash_budget_divisor','allow_one_share_over_budget',
    'active_arms','arm_thresholds','assignment_profile','profile','r_daily_target',
    'orb_required_sessions'))


class HotPolicyRejected(ValueError):
    pass


def field_inventory():
    return {'hot_supported':{
        'dynamic_candidate_limit':'15..30; existing provider contract, next cycle observation bound only',
        'entry_pool_limit':'2..40; configured base and owned exits always retained',
        'entry_paused_arms':'existing non-R entry paths only; owned exits and immutable assignments retained'},
        'existing_settings_next_read':['gdcrs_stocks','paper_strategy_stocks','gdcrs_min_cross_bps',
            'gdcrs_reentry_cooldown_minutes'],
        'existing_settings_caveat':'legacy threshold fields do not replace experiment-v1 constants',
        'new_versioned_adapter_required':sorted(NEW_ADAPTER_REQUIRED),
        'restart_or_new_code_required':['new_arm_implementation','new_transport','ownership_schema','credential_source'],
        'larger_pools':'100/80 requires separately approved future adapter; current bounds remain 30/40'}


def _changes(value):
    if not isinstance(value,dict) or set(value)-set(DEFAULT_CHANGES):
        if isinstance(value,dict) and set(value)&NEW_ADAPTER_REQUIRED:
            raise HotPolicyRejected('NEW_VERSIONED_EVIDENCE_ADAPTER_REQUIRED')
        raise HotPolicyRejected('HOT_POLICY_FIELD_NOT_ALLOWED')
    result={**deepcopy(DEFAULT_CHANGES),**deepcopy(value)}
    for key,lo,hi in (('dynamic_candidate_limit',15,30),('entry_pool_limit',2,40)):
        if type(result[key]) is not int or not lo<=result[key]<=hi:
            raise HotPolicyRejected('HOT_POLICY_EXISTING_POOL_BOUNDS_REQUIRED')
    arms=result['entry_paused_arms']
    if (not isinstance(arms,list) or any(not isinstance(a,str) or a not in ARMS for a in arms)
            or len(arms)!=len(set(arms)) or 'EXP_R' in arms):
        raise HotPolicyRejected('HOT_POLICY_EXISTING_ENTRY_PAUSE_INVALID')
    result['entry_paused_arms']=sorted(arms)
    return result


class ApprovedPolicyReloader:
    """Read one atomic file generation and activate only at a cycle boundary.

    Failed reads leave the last snapshot intact but block new entries until a
    valid pending pair returns. They never suppress owned-position exits.
    The producer must use atomic replacement; reading twice detects a concurrent
    replacement of either file. A pending policy has no implicit code permission.
    """
    def __init__(self, policy_path, approval_path, *, receiver_pid, source_manifest_sha256):
        if type(receiver_pid) is not int or receiver_pid<=1 or re.fullmatch('[a-f0-9]{64}',source_manifest_sha256 or '') is None:
            raise HotPolicyRejected('HOT_POLICY_IDENTITY_INVALID')
        self.policy_path=policy_path;self.approval_path=approval_path
        self.pid=receiver_pid;self.source_sha=source_manifest_sha256;self._lock=RLock()
        self._active={'version':0,'policy_sha256':'0'*64,'approval_sha256':None,
            'changes':deepcopy(DEFAULT_CHANGES),'entry_authorized':False,'reason':'approved_policy_not_loaded'}

    def _pair(self):
        policy,ph=_read(self.policy_path,private=True,maximum=65536)
        approval,ah=_read(self.approval_path,private=True,maximum=16384)
        _,ph2=_read(self.policy_path,private=True,maximum=65536)
        _,ah2=_read(self.approval_path,private=True,maximum=16384)
        if (ph,ah)!=(ph2,ah2):raise HotPolicyRejected('HOT_POLICY_ATOMIC_GENERATION_CHANGED')
        return policy,ph,approval,ah

    def next_cycle(self, now):
        with self._lock:
            try:
                if not isinstance(now,datetime) or now.tzinfo is None:raise HotPolicyRejected('HOT_POLICY_CLOCK_UNVERIFIED')
                now=now.astimezone(KST);policy,ph,approval,ah=self._pair()
                if (ph,ah)==(self._active['policy_sha256'],self._active['approval_sha256']):
                    self._active.update(entry_authorized=True,reason='active_approved_policy')
                    return deepcopy(self._active)
                request=approval.get('user_approval',{});text=request.get('text','');reference=request.get('reference','')
                issued=_time(approval['issued_at']);expires=_time(approval['expires_at'])
                available=_time(policy['available_at']);effective=_time(policy['effective_at'])
                if (approval.get('schema')!='paper-hot-policy-approval-v1' or approval.get('action')!='hot_policy_apply'
                        or approval.get('mode')!='paper' or approval.get('scope')!='receiver_entry_policy_only'
                        or approval.get('receiver_pid')!=self.pid or approval.get('source_manifest_sha256')!=self.source_sha
                        or approval.get('policy_sha256')!=ph or request.get('kind')!='explicit_hot_policy_change'
                        or not isinstance(reference,str) or not 8<=len(reference)<=512
                        or not isinstance(text,str) or not 8<=len(text)<=4096
                        or not (('설정' in text and '승인' in text) or 'policy change' in text.lower())
                        or not issued<=now<expires or not 0<(expires-issued).total_seconds()<=1800):
                    raise HotPolicyRejected('HOT_POLICY_EXPLICIT_APPROVAL_INVALID')
                if (policy.get('schema')!='paper-hot-policy-pending-v1' or policy.get('profile')!='paper_experiment_v1'
                        or policy.get('mode')!='paper' or type(policy.get('version')) is not int
                        or policy['version']!=self._active['version']+1
                        or policy.get('previous_policy_sha256')!=self._active['policy_sha256']
                        or not available<=effective<=now or available>issued):
                    raise HotPolicyRejected('HOT_POLICY_VERSION_OR_POINT_IN_TIME_INVALID')
                changes=_changes(policy.get('changes'))
                self._active={'version':policy['version'],'policy_sha256':ph,'approval_sha256':ah,
                    'changes':changes,'entry_authorized':True,'reason':'active_approved_policy',
                    'activated_at':now.isoformat(),'available_at':available.isoformat()}
            except (ValueError,KeyError,TypeError,OSError,AttributeError) as error:
                reason=str(error) if isinstance(error,HotPolicyRejected) else 'HOT_POLICY_EVIDENCE_UNVERIFIED'
                self._active.update(entry_authorized=False,reason=reason)
            return deepcopy(self._active)

    def assert_entry_current(self, snapshot, now, arm):
        """No activation during a decision; a new pending epoch requires reevaluation."""
        with self._lock:
            if not snapshot.get('entry_authorized'):
                raise HotPolicyRejected(snapshot.get('reason','HOT_POLICY_NOT_AUTHORIZED'))
            _,ph,_,ah=self._pair()
            if ((ph,ah)!=(snapshot['policy_sha256'],snapshot['approval_sha256'])
                    or snapshot['version']!=self._active['version']):
                raise HotPolicyRejected('HOT_POLICY_CHANGED_BEFORE_SEND')
            if arm in snapshot['changes']['entry_paused_arms']:
                raise HotPolicyRejected('HOT_POLICY_ARM_ENTRY_PAUSED')

    def status(self):
        with self._lock:return deepcopy(self._active)
