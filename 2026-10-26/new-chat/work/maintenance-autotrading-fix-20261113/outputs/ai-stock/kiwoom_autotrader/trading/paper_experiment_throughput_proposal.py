"""Disabled, pure future-version research prototype. No order-path imports.

The approved v1 profile, its fourteen arms and immutable assignments remain
unchanged. A plan or passing gate is never a permission to submit an order.
"""
from copy import deepcopy
from datetime import datetime, timedelta
from decimal import Decimal
import hashlib
import json
import re
from zoneinfo import ZoneInfo

KST=ZoneInfo('Asia/Seoul')
VERSION='paper-throughput-research-v2'
VARIANTS={
    'EXP_B_VOL_1P5_V2':{'parent':'EXP_B','parameter':'volume_multiple_min','value':'1.5'},
    'EXP_B_VOL_2_V2':{'parent':'EXP_B','parameter':'volume_multiple_min','value':'2'},
    'EXP_B_VOL_3_V2':{'parent':'EXP_B','parameter':'volume_multiple_min','value':'3'},
    'EXP_A_CROSS_5_V2':{'parent':'EXP_A','parameter':'cross_bps_min','value':'5'},
    'EXP_A_CROSS_15_V2':{'parent':'EXP_A','parameter':'cross_bps_min','value':'15'},
    'EXP_E_VOL_2_V2':{'parent':'EXP_E','parameter':'volume_multiple_min','value':'2'},
    'EXP_E_VOL_4_V2':{'parent':'EXP_E','parameter':'volume_multiple_min','value':'4'}}


def digest(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,
        separators=(',',':'),allow_nan=False).encode()).hexdigest()


def moment(value):
    value=datetime.fromisoformat(value) if isinstance(value,str) else value
    if not isinstance(value,datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError('prototype_aware_clock_required')
    return value.astimezone(KST)


def _sha(value):return isinstance(value,str) and re.fullmatch('[0-9a-f]{64}',value) is not None


def proposal(*,as_of,effective_from,parent_profile_sha256):
    now=moment(as_of);effective=moment(effective_from)
    if effective.date()<=now.date() or not _sha(parent_profile_sha256):
        raise ValueError('prototype_future_version_only')
    value={'schema':'paper-throughput-proposal-v2','version':VERSION,
        'as_of':now.isoformat(),'effective_from':effective.isoformat(),
        'parent_profile_sha256':parent_profile_sha256,'enabled':False,'can_submit':False,
        'status':'disabled_prototype_separate_approval_required',
        'current_profile_changed':False,'existing_assignments_changed':False,
        'future_profile':{'paper_only':True,'budget_cap_krw':100000,'budget_cash_divisor':45,
            'maximum_holdings':60,'dynamic_discovery_limit':100,'entry_pool_limit':80,
            'theme_limit':5,'overnight_limit':20,'vss_limit':8,'unfilled_ttl_seconds':180,
            'symbol_budget_multiplier':2,'s5_unused_reserved_positions':2,
            'one_share_exception':'requires_existing_explicit_proof_not_automatic',
            'approval_required_for_every_parameter_change':True},
        'future_assignment':{'mode':'all_active_arms_evaluated_then_logged_random_collision',
            'minimum_actual_R_pct':15,'minimum_actual_A_pct':15,
            'denominator':'future_version_actual_selected_entry_cycles',
            'control_floor_can_block_other_arm_selection':True,
            'past_v1_allocation_and_outcomes_immutable':True},
        'random_control':{'target_attempts_per_official_day':10,'earliest':'09:10','latest':'14:50',
            'holding_min_minutes':10,'holding_max':'official_session_close',
            'target_is_not_guaranteed_fills':True,'missed_or_blocked_slots_not_replayed':True,
            'all_ready_risk_cash_integrity_gates_required':True},
        'variant_rules':deepcopy(VARIANTS),
        'variant_warning':'B2 variant shares formula with original B; correlated trials must remain in full inventory',
        'orb_future_rule':'progressive_same_window5_to14_v2',
        'original_orb_strict14_unchanged':True,
        'activation_requirements':['ten_consecutive_verified_official_stable_sessions',
            'separate_explicit_user_approval','normal_deployment_window_and_code_guard',
            'full_frozen_source_regression','new_version_only_no_historical_reassignment'],
        'weeks':[{'week':'1','work':'prepare watchdog/KPI/funnel and proposed R/pool/budget settings; no unapproved activation'},
            {'week':'2','work':'verify recovery and accumulate official stability observations; ten days not assumed'},
            {'week':'3-4','work':'after ten actual passing sessions and approval, integrate concurrent/variant adapter and deploy in allowed window'}]}
    value['proposal_sha256']=digest(value)
    return value


def stability_gate(proof,*,as_of):
    """Use the owning KPI validator, never a weaker local PASS assertion."""
    try:
        from .stability_metrics import validate_ten_session_proof
        checked=validate_ten_session_proof(proof,now=moment(as_of))
        passed=checked is True or isinstance(checked,dict) and checked.get('status')=='verified'
    except (ImportError,KeyError,ValueError,TypeError,ArithmeticError):passed=False
    return {'passed':bool(passed),'reason':None if passed else 'ten_actual_official_stable_sessions_unverified',
        'proof_sha256':digest(proof),'can_submit':False,'independent_execution_permission':False}


def review_activation(value,*,stability,approval,as_of,deployment_gate_passed=False):
    now=moment(as_of);checked_stability=stability_gate(stability,as_of=now)
    good=(isinstance(value,dict) and value.get('proposal_sha256')==digest({k:v for k,v in value.items() if k!='proposal_sha256'})
        and value.get('enabled') is False and value.get('can_submit') is False
        and checked_stability['passed'] is True and isinstance(approval,dict)
        and approval.get('approved') is True and approval.get('proposal_sha256')==value['proposal_sha256']
        and approval.get('scope')=='future_paper_throughput_version' and deployment_gate_passed is True)
    if good:
        try:good=moment(approval['issued_at'])<=now<moment(approval['expires_at'])
        except (KeyError,TypeError,ValueError):good=False
    return {'ready_for_separate_integration_review':bool(good),'can_submit':False,
        'current_v1_modified':False,'source_integration_and_deploy_still_required':True}


def random_control_plan(candidates,*,session,as_of,seed_sha256,target=10):
    """Ten planned attempts; neither manufactured signals nor orders/fills."""
    now=moment(as_of);start=moment(session['start_at']);close=moment(session['end_at'])
    if (type(target) is not int or target!=10 or not _sha(seed_sha256)
            or session.get('verified') is not True or not _sha(session.get('source_sha256'))
            or moment(session['available_at'])>now or session.get('day')!=start.strftime('%Y%m%d')
            or start.date()!=close.date() or not start<close or now>=start):
        raise ValueError('prototype_session_or_frozen_universe_unverified')
    records=[];seen=set()
    for row in candidates:
        symbol=row.get('symbol')
        if (not isinstance(symbol,str) or re.fullmatch('[0-9]{6}',symbol) is None or symbol in seen
                or row.get('eligible') is not True or not _sha(row.get('source_sha256'))
                or moment(row['available_at'])>now):raise ValueError('prototype_universe_unverified')
        seen.add(symbol);records.append(deepcopy(row))
    if len(records)<target:raise ValueError('prototype_ten_distinct_eligible_candidates_required')
    earliest=max(start+timedelta(minutes=10),start.replace(hour=9,minute=10,second=0,microsecond=0))
    latest=min(close-timedelta(minutes=10),start.replace(hour=14,minute=50,second=0,microsecond=0))
    minutes=[];t=earliest
    while t<=latest:minutes.append(t);t+=timedelta(minutes=1)
    if len(minutes)<target:raise ValueError('prototype_official_random_window_too_short')
    universe_sha=digest(sorted(records,key=lambda r:r['symbol']));universe_count=len(records)
    records=sorted(records,key=lambda r:digest([VERSION,seed_sha256,session['day'],r['symbol']]))[:target]
    times=sorted(minutes,key=lambda at:digest([VERSION,seed_sha256,'time',at.isoformat()]))[:target]
    jobs=[]
    for row,at in zip(records,sorted(times)):
        maximum=int((close-at).total_seconds()//60)
        hold=10+int(digest([seed_sha256,row['symbol'],at.isoformat(),'hold'])[:12],16)%(maximum-9)
        jobs.append({'symbol':row['symbol'],'planned_at':at.isoformat(),'planned_hold_minutes':hold,
            'planned_exit_at':(at+timedelta(minutes=hold)).isoformat(),'official_close_at':close.isoformat(),
            'actual_hold_clock':'first_verified_unique00_fill; clip at official close; reject entry if <10minutes remain',
            'candidate_source_sha256':row['source_sha256'],'permission_to_trade':False})
    result={'schema':'paper-r-ten-attempt-plan-v2','version':VERSION,'day':session['day'],
        'universe_frozen_at':now.isoformat(),'universe_sha256':universe_sha,'universe_count':universe_count,
        'seed_sha256':seed_sha256,'session_source_sha256':session['source_sha256'],'jobs':jobs,
        'target_attempts':10,'actual_attempts':None,'accepted_orders':None,'fills':None,
        'requires_fresh_gates_at_each_planned_time':True,'missed_slots_replayed':False,'can_submit':False}
    result['plan_sha256']=digest(result)
    return result


def resolve_collision(signals,*,day,symbol,signal_at,seed_sha256,prior_counts):
    """Research-only selection over real PASS signals; no synthetic R injection."""
    at=moment(signal_at)
    if at.strftime('%Y%m%d')!=day or not _sha(seed_sha256):raise ValueError('prototype_collision_identity')
    names=[]
    for signal in signals:
        if (signal.get('symbol')!=symbol or signal.get('signal_at')!=at.isoformat()
                or signal.get('passed') is not True or not _sha(signal.get('evidence_sha256'))
                or not isinstance(signal.get('arm'),str) or signal['arm'] in names):
            raise ValueError('prototype_signal_evidence_unverified')
        names.append(signal['arm'])
    if any(type(v) is not int or v<0 for v in prior_counts.values()):raise ValueError('prototype_counts_unverified')
    needed=(15*(sum(prior_counts.values())+1)+99)//100
    deficient=[a for a in ('EXP_R','EXP_A') if prior_counts.get(a,0)<needed]
    eligible=[a for a in names if a in deficient] if deficient else names
    selected=min(eligible,key=lambda a:digest([VERSION,seed_sha256,day,symbol,at.isoformat(),a])) if eligible else None
    result={'version':VERSION,'symbol':symbol,'signal_at':at.isoformat(),'competing_arms':sorted(names),
        'evidence_sha256':sorted(s['evidence_sha256'] for s in signals),'selected_arm':selected,
        'seed_sha256':seed_sha256,'prior_counts':deepcopy(prior_counts),
        'reason':'wait_for_real_control_signal' if deficient and not eligible else 'logged_seeded_collision',
        'historical_assignment_changed':False,'can_submit':False}
    result['selection_sha256']=digest(result)
    return result


def orb_progressive_requirement(*,prior_complete_sessions,new_version_session_index):
    """NEW future rule only. Original ORB continues to require14 sessions."""
    if (type(prior_complete_sessions) is not int or prior_complete_sessions<0
            or type(new_version_session_index) is not int or new_version_session_index<0):
        raise ValueError('prototype_orb_session_count_unverified')
    required=min(14,5+new_version_session_index)
    return {'rule_version':'progressive_same_window5_to14_v2','required_sessions':required,
        'actual_complete_sessions':prior_complete_sessions,'denominator_sessions':required if prior_complete_sessions>=required else None,
        'status':'research_input_ready' if prior_complete_sessions>=required else 'hold_missing_actual_sessions',
        'session_index_basis':'actual official sessions since separately approved future version activation',
        'original_strict14_unchanged':True,'daily_volume_proxy':False,'synthetic_bars_created':False,'can_submit':False}


def evaluate_variant(variant,features,*,safety_gates):
    """Feature-level prototype, not a substitute for source-bound validators."""
    if variant not in VARIANTS:raise ValueError('prototype_variant_unknown')
    rule=VARIANTS[variant];parent=rule['parent'];checks={}
    try:
        if parent=='EXP_A':checks={'golden_cross':features['golden_cross'] is True,
            'cross_threshold':Decimal(str(features['cross_bps']))>=Decimal(rule['value'])}
        elif parent=='EXP_B':checks={'golden_cross':features['golden_cross'] is True,
            'volume_threshold':Decimal(str(features['volume_multiple']))>=Decimal(rule['value']),
            'slow_sma_rising':features['slow_sma_rising'] is True,'entry_before_1500':features['entry_before_1500'] is True}
        else:checks={'event_window':features['event_window'] is True,
            'negative_150bp':Decimal(str(features['bar_return_bps']))<=-150,
            'volume_threshold':Decimal(str(features['volume_multiple']))>=Decimal(rule['value'])}
    except (KeyError,TypeError,ValueError,ArithmeticError):checks={'features_complete':False}
    required=('paper_mode','stream_ready','risk_eligible','cash_reconciled','ownership_reconciled','input_integrity')
    gates={key:safety_gates.get(key) is True for key in required}
    return {'variant':variant,'parent_arm':parent,'rule':deepcopy(rule),'checks':checks,'safety_gates':gates,
        'research_pattern_passed':bool(checks) and all(checks.values()),
        'research_signal_passed':bool(checks) and all(checks.values()) and all(gates.values()),
        'source_bound_adapter_integrated':False,'can_submit':False}
