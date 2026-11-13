"""Pure selection/input audit. Missing diagnostics are unknown, never zero."""
from datetime import datetime,timedelta
from copy import deepcopy
from kiwoom_autotrader.api.session import KST

ARMS=('EXP_GDR','GDR_fallback','EXP_ORB_A','EXP_ORB_B','EXP_VSS','EXP_LDR','EXP_ETF_B','EXP_C')
CONDITION_REASONS=frozenset({'already_owned','common_stock_only','liquidity_20d_at_least_3bn',
    'vi_entry_executable','upper_limit_distance_gt_5pct','gdr_gap_range','orb_relative_volume_below_2',
    'orb_first_five_not_bullish','vss_volume_or_return','vss_prior_limit_up_unverified_or_true',
    'ldr_return_range','ldr_disclosures_unverified_or_adverse'})


def activation_contract(arm):
    """Requirements, not a promise that a scheduled collection has run."""
    result={'expected_activation_date':None,'activation_date_status':'미확정',
        'official_calendar_required':True,'missing_inputs_are_not_pass':True,
        'historical_selections_replayed':False}
    if arm.startswith('EXP_ORB'):
        result.update({'required_saved_same_window_sessions':14,
            'same_window':['09:00','09:05'],'before_14_session_policy':'hold_without_daily_volume_proxy',
            'activation_condition':'14 exact prior official sessions with source-bound completed first5minutes; fresh current selection and VI',
            'scheduled_collection_is_not_saved_data':True})
    elif arm in ('EXP_GDR','GDR_fallback'):
        result.update({'activation_condition':'verified prior21daily, stock classification, timestamped complete VI and opening quote during next official selection window',
            'explicit_alternative':'09:00 observed official day_open only, labeled observed_0900_open_fallback_v0; not an auction fill'})
    elif arm=='EXP_ETF_B':
        result.update({'activation_condition':'two declared ETFs, actual completed minute rows, current nativeB conditions and fresh order proof',
            'unfinished_minute_policy':'remove unfinished row; never interpolate or infer missing completed rows'})
    elif arm in ('EXP_VSS','EXP_LDR'):
        result.update({'activation_condition':'15:15 official current selection, prior21daily, complete VI and arm-specific price/volume/disclosure evidence',
            'not_due_before':'15:15'})
    else:result.update(activation_condition='actual preopen evidence and current09:00 gap window; no historical replay')
    return result


def _time(value):
    t=datetime.fromisoformat(value) if isinstance(value,str) else value
    if not isinstance(t,datetime) or t.tzinfo is None:raise ValueError('selection_audit_time')
    return t.astimezone(KST)


def checkpoint(*,day,window,observed_at,assignments,selections,input_status=None,symbol_diagnostics=None):
    now=_time(observed_at)
    if now.strftime('%Y%m%d')!=day or window not in ('08:59','09:00','09:05','15:15'):
        raise ValueError('selection_audit_window')
    # An offline reader uses the real checkpoint timestamp, not publication time.
    cutoff=now.replace(hour=int(window[:2]),minute=int(window[3:]),second=59,microsecond=999999)
    cutoff=min(now,cutoff);valid=[a for a in assignments if _time(a['assigned_at'])<=cutoff]
    rows=[]
    for arm in ARMS:
        key='GDR_fallback' if arm=='GDR_fallback' else 'GDR' if arm=='EXP_GDR' else 'ORB' if arm.startswith('EXP_ORB') else 'closing' if arm in ('EXP_VSS','EXP_LDR') else None
        snapshot=selections.get(key) if key else None
        if snapshot and _time(snapshot['selected_at'])>cutoff:snapshot=None
        selected=[a for a in valid if a.get('arm')==('EXP_GDR' if arm=='GDR_fallback' else arm)]
        if arm in ('EXP_GDR','GDR_fallback'):
            selected=[] if snapshot is None else [a for a in snapshot.get('assignments',[]) if a.get('arm')=='EXP_GDR']
        detail=[]
        if snapshot:
            for group in snapshot.get('selections',[]):
                if group.get('kind') not in (arm,'ORB' if arm.startswith('EXP_ORB') else 'EXP_GDR' if arm=='GDR_fallback' else arm):continue
                detail+=deepcopy(group.get('rejected',[]))
        missing=[r for r in detail if r.get('reason') not in CONDITION_REASONS]
        row={'arm':arm,'selected_count_as_of':len(selected),'selected_symbols':sorted(a['symbol'] for a in selected),
            'selection_observed':snapshot is not None if key else bool(selected),
            'selection_status':snapshot.get('status') if snapshot else 'not_observed_by_checkpoint',
            'evaluated_candidate_count':snapshot.get('candidate_count') if snapshot else None,
            'input_blocked_count':len(missing)+len(snapshot.get('unqueried_symbols',[])) if snapshot else None,
            'input_blocked_reasons':missing if snapshot else None,
            'unqueried_symbols':snapshot.get('unqueried_symbols',[]) if snapshot else None,
            'condition_rejected_count':len(detail)-len(missing) if snapshot else None,
            'activation_contract':activation_contract(arm)}
        if arm.startswith('EXP_ORB'):row['pre_assignment_rejections_shared_across_orb_variants']=True
        rows.append(row)
    return {'schema':'paper-experiment-input-checkpoint-v1','day':day,'window':window,
        'observed_at':now.isoformat(),'as_of':cutoff.isoformat(),'rows':rows,
        'input_status':deepcopy(input_status),'symbol_diagnostics':deepcopy(symbol_diagnostics),
        'missing_input_diagnostics_are_zero':False,'future_observations_used':False}
