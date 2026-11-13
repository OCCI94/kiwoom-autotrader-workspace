"""Pure stability measurements. Missing audit coverage is never zero incidents.

Minute polls are observations, not proof that a socket was ready between polls.
A passing daily report additionally needs a complete, hash-bound operations
audit. These contracts never issue orders, change settings or restart workers.
"""
from datetime import datetime, timedelta
import hashlib
import json
import math
import re
from zoneinfo import ZoneInfo

KST = ZoneInfo('Asia/Seoul')


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
        ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def timestamp(value):
    value = datetime.fromisoformat(value) if isinstance(value, str) else value
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError('aware_clock_required')
    return value.astimezone(KST)


def _sha(value):
    return isinstance(value, str) and re.fullmatch('[0-9a-f]{64}', value) is not None


def session_contract(value):
    if not isinstance(value, dict): raise ValueError('official_session_unverified')
    day = datetime.strptime(value['day'], '%Y%m%d').date()
    previous = datetime.strptime(value['previous_session_day'], '%Y%m%d').date()
    start, end = timestamp(value['start']), timestamp(value['end'])
    if (value.get('verified') is not True or not previous < day
            or not start.date() == end.date() == day or not start < end
            or not _sha(value.get('calendar_sha256'))):
        raise ValueError('official_session_unverified')
    return start, end


class TickHealth:
    """Process-local completed ticks; a slow/stuck in-flight tick is visible.

    A process start is a new observation epoch, not a daily counter reset that
    may be interpreted as zero restarts. Durations use a monotonic clock.
    """
    def __init__(self):
        self.started = self.finished = self.last_start = None
        self.last_mono = None
        self.count = self.failures = 0
        self.maximum_gap = self.maximum_duration = 0.0
        self.clock_error = False
        self.day=None;self.day_gap=0.0;self.day_failures=0;self.day_clock_error=False

    def begin(self, now, monotonic_now):
        now = timestamp(now)
        if self.day!=now.strftime('%Y%m%d'):
            self.day=now.strftime('%Y%m%d');self.day_gap=0.0;self.day_failures=0;self.day_clock_error=False
        if self.last_mono is not None:
            gap = monotonic_now - self.last_mono
            if gap < 0 or self.last_start is not None and now < timestamp(self.last_start):
                self.clock_error = self.day_clock_error = True
            else:
                self.maximum_gap = max(self.maximum_gap, gap)
                if timestamp(self.last_start).date()==now.date():self.day_gap=max(self.day_gap,gap)
        self.started = self.started or now.isoformat()
        self.last_start = now.isoformat(); self.last_mono = monotonic_now

    def end(self, now, monotonic_now, *, success):
        at = timestamp(now)
        duration = monotonic_now - self.last_mono
        if duration < 0 or at < timestamp(self.last_start): self.clock_error = self.day_clock_error = True
        else: self.maximum_duration = max(self.maximum_duration, duration)
        self.finished = at.isoformat(); self.count += 1
        if success is not True:self.failures+=1;self.day_failures+=1

    def status(self):
        return {'schema':'paper-monitor-tick-health-v1', 'coverage':'current_process_only',
            'first_tick_started_at':self.started, 'last_tick_started_at':self.last_start,
            'last_tick_completed_at':self.finished, 'completed_ticks':self.count,
            'failed_ticks':self.failures, 'maximum_start_gap_seconds':self.maximum_gap,
            'maximum_tick_duration_seconds':self.maximum_duration,
            'day':self.day,'maximum_start_gap_seconds_day':self.day_gap,'failed_ticks_day':self.day_failures,
            'day_clock_unverified':self.day_clock_error,
            'clock_unverified':self.clock_error}


def _audit_values(audit, official, start, end):
    """An audit producer must preserve the source inventory and complete span.

    Merely absent alerts, order rows, or manual-command rows do not establish
    zero lost signals/interventions. The audit must explicitly cover them.
    """
    if not isinstance(audit, dict): return None
    # Numeric assertions with a made-up inventory hash are insufficient. The
    # exact original envelopes are recompiled every time a policy gate reads it.
    from .stability_audit import compile_operations_audit
    try:
        rebuilt=compile_operations_audit(official,audit['evidence'],now=timestamp(audit['compiled_at']))
        rebuilt['compiled_at']=audit['compiled_at']
        if rebuilt!=audit:return None
    except (ValueError,TypeError,KeyError):return None
    inventory = audit.get('source_inventory')
    if (audit.get('schema') != 'paper-stability-operations-audit-v1'
            or audit.get('session_day') != official['day'] or audit.get('coverage_complete') is not True
            or timestamp(audit['covered_from']) > start or timestamp(audit['covered_through']) < end
            or audit.get('calendar_sha256') != official['calendar_sha256']
            or not isinstance(inventory, dict) or not inventory
            or any(not isinstance(k,str) or not _sha(v) for k,v in inventory.items())
            or any(audit.get(k) is not True for k in ('ready_transitions_complete',
                'tick_history_complete','signal_outcomes_complete','operator_actions_complete','process_history_complete'))):
        return None
    result = {k:audit.get(k) for k in ('ready_seconds','maximum_tick_gap_seconds',
        'confirmed_operational_lost_signals','manual_interventions','receiver_restarts')}
    if any(type(result[k]) is not int or result[k] < 0 for k in (
            'confirmed_operational_lost_signals','manual_interventions','receiver_restarts')):return None
    duration=(end-start).total_seconds()
    if (any(type(result[k]) not in (int,float) or not math.isfinite(result[k]) or result[k]<0
            for k in ('ready_seconds','maximum_tick_gap_seconds')) or result['ready_seconds']>duration):return None
    result['ready_ratio']=result.pop('ready_seconds')/duration
    return result


def daily_report(official, samples, *, now, audit=None):
    now=timestamp(now); start,end=session_contract(official)
    expected=math.ceil((end-start).total_seconds()/60)
    slots={}; invalid=0
    for sample in samples:
        try:
            at=timestamp(sample['observed_at'])
            if sample.get('schema')!='paper-readonly-watchdog-sample-v1' or at>now:raise ValueError
            if start<=at<end:
                idx=int((at-start).total_seconds()//60)
                if (at-(start+timedelta(minutes=idx))).total_seconds()>15:continue
                old=slots.get(idx)
                # Conflicting duplicate samples are not resolved in favour of ready.
                if old is not None and old.get('stream_ready')!=sample.get('stream_ready'):raise ValueError
                slots[idx]=sample
        except (ValueError,TypeError,KeyError):invalid+=1
    observed_ready=sum(s.get('stream_ready') is True for s in slots.values())
    grid_complete=len(slots)==expected and invalid==0 and now>=end
    result={'schema':'paper-stability-daily-v1','generated_at':now.isoformat(),'official_session':dict(official),
        'expected_minute_slots':expected,'observed_minute_slots':len(slots),'invalid_samples':invalid,
        'sample_grid_complete':grid_complete,'sampled_ready_ratio':observed_ready/len(slots) if slots else None,
        'ready_ratio':None,'maximum_tick_gap_seconds':None,'confirmed_operational_lost_signals':None,
        'manual_interventions':None,'receiver_restarts':None,'coverage_complete':False,'status':'UNVERIFIED',
        'reason':'continuous_ready_and_event_audit_required','operations_audit':None}
    try:verified=_audit_values(audit,official,start,end)
    except (ValueError,TypeError,KeyError):verified=None
    if isinstance(audit,dict):
        # Preserve verified partial measurements without calling a complete day.
        try:
            from .stability_audit import compile_operations_audit
            partial=compile_operations_audit(official,audit['evidence'],now=timestamp(audit['compiled_at']))
            if partial==audit:result['partial_operations_audit']=partial
        except (ValueError,TypeError,KeyError):pass
    if verified is not None and grid_complete:
        result.update(verified,coverage_complete=True,operations_audit=dict(audit))
        passed=(result['ready_ratio']>=.995 and result['maximum_tick_gap_seconds']<120
            and result['confirmed_operational_lost_signals']==0 and result['manual_interventions']==0)
        result.update(status='PASS' if passed else 'FAIL',reason='complete_session_audit')
    return result


def validate_ten_session_proof(proof, *, now):
    """Validate complete daily bodies and official previous-session links.

    Restart count is reported, not silently required to be zero: the user's
    pass criteria specify no human intervention, not an invented restart cap.
    """
    try:
        now=timestamp(now)
        if (not isinstance(proof,dict) or proof.get('schema')!='paper-stability-ten-session-proof-v1'
                or timestamp(proof['generated_at'])>now):raise ValueError
        rows=proof['daily_reports']
        if not isinstance(rows,list) or len(rows)!=10:raise ValueError
        previous=None
        for row in rows:
            report=row['report']; official=report['official_session']; start,end=session_contract(official)
            if (row.get('report_sha256')!=digest(report) or report.get('schema')!='paper-stability-daily-v1'
                    or report.get('status')!='PASS' or report.get('coverage_complete') is not True
                    or report.get('sample_grid_complete') is not True
                    or report.get('observed_minute_slots')!=math.ceil((end-start).total_seconds()/60)
                    or report.get('expected_minute_slots')!=report['observed_minute_slots']
                    or report.get('invalid_samples')!=0
                    or not end<=timestamp(report['generated_at'])<=timestamp(proof['generated_at'])<=now
                    or previous is not None and official['previous_session_day']!=previous):raise ValueError
            values=_audit_values(report.get('operations_audit'),official,start,end)
            if values is None or any(report.get(k)!=v for k,v in values.items()):raise ValueError
            if not (values['ready_ratio']>=.995 and values['maximum_tick_gap_seconds']<120
                    and values['confirmed_operational_lost_signals']==0 and values['manual_interventions']==0):raise ValueError
            previous=official['day']
        if proof.get('through_session_day')!=previous:raise ValueError
        return {'status':'verified','complete':True,'session_days':[r['report']['official_session']['day'] for r in rows],
            'through_session_day':previous,'proof_sha256':digest(proof)}
    except (ValueError,TypeError,KeyError,OverflowError):
        return {'status':'unverified','complete':False,'reason':'ten_complete_consecutive_official_sessions_required'}


def checkpoint(kind, official, *, now, sample, inputs=None, owned=None, financial=None, funnel=None, daily=None):
    """Pure, due-time-specific checks. No catch-up execution or order action."""
    now=timestamp(now);start,end=session_contract(official)
    times={'preopen':'08:50','exits':'15:35','daily':'16:00'}
    if kind not in times:raise ValueError('checkpoint_kind_invalid')
    if now.strftime('%Y%m%d')!=official['day'] or now.strftime('%H:%M')!=times[kind]:
        return {'status':'skipped','reason':'outside_checkpoint_minute','kind':kind}
    if kind=='preopen':
        if now>=start:return {'status':'skipped','reason':'not_official_preopen','kind':kind}
        inputs=inputs or {}
        checks={k:inputs.get(k) is True for k in ('daily_cache_ready','vi_ready','calendar_ready')}
        checks.update(stream_ready=sample.get('stream_ready') is True,
            active_arms_14=sample.get('experiment_enabled')is True and set(sample.get('active_arms',[]))=={
                'EXP_R','EXP_A','EXP_B','EXP_B_TP','EXP_E','EXP_C','EXP_D','EXP_ORB_A','EXP_ORB_B',
                'EXP_GDR','EXP_VSS','EXP_LDR','EXP_ETF_B','S5_steady_uptrend_vwap'})
        return {'kind':kind,'status':'PASS' if all(checks.values()) else 'UNVERIFIED','checks':checks}
    if now<end:return {'status':'skipped','reason':'official_session_not_closed','kind':kind}
    if kind=='exits':
        if not isinstance(owned,list):return {'kind':kind,'status':'UNVERIFIED','reason':'owned_positions_unverified'}
        due=[]; overnight=[];unknown=[]
        for item in owned:
            if (not isinstance(item,dict) or type(item.get('quantity'))is not int or item['quantity']<0
                    or not isinstance(item.get('symbol'),str) or re.fullmatch('[0-9]{6}',item['symbol'])is None):
                unknown.append('invalid_position');continue
            if item['quantity']==0:continue
            if item.get('intended_overnight') is True and _sha(item.get('ownership_proof_sha256')):overnight.append(item['symbol'])
            elif item.get('intended_overnight') is False and _sha(item.get('ownership_proof_sha256')):due.append(item['symbol'])
            else:unknown.append(item['symbol'])
        return {'kind':kind,'status':'FAIL' if due else 'UNVERIFIED' if unknown else 'PASS',
            'intraday_remaining':due,'intended_overnight_excluded':overnight,'unverified':unknown}
    return {'kind':kind,'status':'READY' if daily and financial and funnel else 'UNVERIFIED',
        'stability':daily,'financial':financial,'funnel':funnel,
        'financial_missing_is_zero':False,'friday_summary_due':now.weekday()==4}


def weekly_one_page(reports):
    lines=['주간 운영 안정·깔때기 요약 (관측 근거가 없는 값은 미확정)']
    for item in sorted(reports,key=lambda r:r.get('official_session',{}).get('day','')):
        day=item.get('official_session',{}).get('day','미확정')
        fmt=lambda v:'미확정' if v is None else str(v)
        lines.append(f"{day}: {item.get('status','UNVERIFIED')} ready={fmt(item.get('ready_ratio'))} "
            f"tick최대={fmt(item.get('maximum_tick_gap_seconds'))}s 신호유실={fmt(item.get('confirmed_operational_lost_signals'))} "
            f"개입={fmt(item.get('manual_interventions'))} 재시작={fmt(item.get('receiver_restarts'))}")
        funnel=item.get('funnel')
        if isinstance(funnel,dict) and funnel:
            totals=[]
            for key in ('observed','assigned','evaluated','signals','orders','fills'):
                values=[row.get(key) for row in funnel.values()]
                totals.append(str(sum(values)) if all(type(v)is int and v>=0 for v in values) else '미확정')
            lines[-1]+=' | arm 합산 관측/배정/평가/신호/주문/체결='+'/'.join(totals)+'(종목 중복 가능)'
    return '\n'.join(lines[:8])+'\n'
