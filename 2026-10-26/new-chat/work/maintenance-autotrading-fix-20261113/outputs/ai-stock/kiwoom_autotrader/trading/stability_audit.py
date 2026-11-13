"""Non-blocking-failure stability evidence from the existing receiver loop.

No broker work or credentials. Raw commands are never stored. A failed append
invalidates this process's coverage permanently; it cannot create a good day.
"""
from datetime import datetime
import json
import math
import re
from pathlib import Path
import uuid
from kiwoom_autotrader.utils.platform_io import os,fcntl
from .stability_metrics import KST,digest,timestamp,session_contract

READ_ONLY_COMMANDS=frozenset({'help','start','status','top','channels','posts','srch','report','rank','ip'})


class ReceiverStabilityAudit:
    def __init__(self,directory):
        self.directory=Path(directory);self.run_id=uuid.uuid4().hex
        self.sequence=0;self.previous=None;self.last_at=None;self.first_at=None
        self.manual=0;self.failed=False;self.pending_command=False
        self.day=None;self.process_started_at=None

    def command(self,command):
        if command not in READ_ONLY_COMMANDS:self.manual+=1
        self.pending_command=True

    def pulse(self,now,stream,health,signal_status):
        """At most one ordinary pulse/minute; transitions are observed by 00.

        Ready coverage is certified only for recorder epochs reporting no gap.
        A gap is not bridged by polling or by restarting the stability writer.
        """
        try:
            at=timestamp(now)
            self.process_started_at=self.process_started_at or at
            if self.day!=at.strftime('%Y%m%d'):
                self.day=at.strftime('%Y%m%d');self.sequence=0;self.previous=None;self.last_at=None;self.first_at=None
                self.failed=False  # a new dated audit epoch; prior day's failure evidence remains immutable.
            if self.last_at and 0<=(at-self.last_at).total_seconds()<60 and not self.pending_command:
                return self.status()
            if self.last_at and at<self.last_at:self.failed=True
            self.first_at=self.first_at or at
            signal_status=signal_status or {}
            count=signal_status.get('success_count',0)
            failure=signal_status.get('failure_count',0)
            event={'schema':'receiver-stability-pulse-v1','run_id':self.run_id,'sequence':self.sequence,
                'previous_sha256':self.previous,'observed_at':at.isoformat(),
                'process_started_at':self.process_started_at.isoformat(),'audit_day_started_at':self.first_at.isoformat(),
                'pid':os.getpid(),'manual_interventions':self.manual,'coverage_failed':self.failed,
                'ready':stream.get('ready')is True,'stream_coverage_complete':stream.get('coverage_complete')is True,
                'stream_gap_count':stream.get('gap_count'),'stream_error':bool(stream.get('error')),
                'tick_health':dict(health),'signal_success_count':count,'signal_failure_count':failure,
                'signal_last_event_sha256':signal_status.get('last_event_sha256')}
            sha=digest(event);line=json.dumps({'event_sha256':sha,'event':event},ensure_ascii=False,sort_keys=True,separators=(',',':'))+'\n'
            if self.directory.is_symlink() or any(p.is_symlink() for p in self.directory.parents):raise ValueError
            self.directory.mkdir(parents=True,exist_ok=True,mode=0o700)
            path=self.directory/('receiver-stability-'+at.strftime('%Y%m%d')+'.jsonl')
            fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_APPEND|os.O_NOFOLLOW,0o600)
            try:
                fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
                raw=line.encode();written=0
                while written<len(raw):
                    n=os.write(fd,raw[written:])
                    if n<=0:raise OSError
                    written+=n
                os.fsync(fd)
            finally:os.close(fd)
            self.sequence+=1;self.previous=sha;self.last_at=at;self.pending_command=False
        except Exception:self.failed=True
        return self.status()

    def status(self):
        return {'schema':'receiver-stability-writer-status-v1','run_id':self.run_id,
            'last_event_sha256':self.previous,'events_written':self.sequence,
            'coverage_failed':self.failed,'first_observed_at':self.first_at.isoformat() if self.first_at else None,
            'last_observed_at':self.last_at.isoformat() if self.last_at else None}


def compile_operations_audit(official,evidence,*,now):
    """Recompute from original hashed pulses and original signal envelopes.

    Single, uninterrupted receiver epoch required for an initial certificate.
    Restart/dropout epochs remain UNVERIFIED rather than invented downtime or
    zero incidents. Future versions may reconcile their exact interval evidence.
    """
    start,end=session_contract(official);at=timestamp(now)
    if at<end:raise ValueError('session_not_closed')
    pulses=evidence['receiver_events'];signals=evidence['signal_events']
    if not isinstance(pulses,list) or not 2<=len(pulses)<=20000 or not isinstance(signals,list) or len(signals)>10000:
        raise ValueError('audit_evidence_bounds')
    checked=[];previous=None;run=None;last=None;seq=None;pid=None
    for item in pulses:
        e=item['event'];t=timestamp(e['observed_at'])
        if (item.get('event_sha256')!=digest(e) or e.get('schema')!='receiver-stability-pulse-v1'
                or e.get('coverage_failed')is not False or e.get('signal_failure_count')!=0 or t>at
                or type(e.get('sequence'))is not int or e['sequence']<0
                or not isinstance(e.get('run_id'),str) or re.fullmatch('[0-9a-f]{32}',e['run_id'])is None
                or type(e.get('pid'))is not int or e['pid']<=0 or pid is not None and e['pid']!=pid
                or run is not None and e.get('run_id')!=run
                or last is not None and (t<=last or (t-last).total_seconds()>75)
                or previous is not None and (e.get('previous_sha256')!=previous or e.get('sequence')!=seq+1)):
            raise ValueError('receiver_event_chain_or_coverage_unverified')
        if run is None:
            if e.get('sequence')!=0 or e.get('previous_sha256')is not None:raise ValueError('receiver_epoch_start_missing')
            if (timestamp(e['process_started_at'])>t or timestamp(e['audit_day_started_at'])!=t
                    or t>=start):raise ValueError('receiver_epoch_after_open')
        run=e['run_id'];pid=e['pid'];previous=item['event_sha256'];last=t;seq=e['sequence'];checked.append((t,e))
    if checked[0][0]>start or checked[-1][0]<end:raise ValueError('full_session_pulses_required')
    relevant=[(t,e) for t,e in checked if start<=t<=end]
    before=[(t,e) for t,e in checked if t<start][-1];after=[(t,e) for t,e in checked if t>=end][0]
    window=[before,*relevant,after]
    baseline_gap=before[1].get('stream_gap_count')
    if (type(baseline_gap)is not int or baseline_gap<0 or any(e.get('ready')is not True
            or e.get('stream_error')is not False or e.get('stream_gap_count')!=baseline_gap for _,e in window)):
        raise ValueError('continuous_ready_epoch_unverified')
    health=after[1]['tick_health'];first=timestamp(health['first_tick_started_at']);completed=timestamp(health['last_tick_completed_at'])
    if (health.get('schema')!='paper-monitor-tick-health-v1' or health.get('day_clock_unverified')is not False
            or first>start or completed<end):raise ValueError('tick_history_unverified')
    maximum=health['maximum_start_gap_seconds_day']
    if health.get('day')!=official['day'] or health.get('failed_ticks_day')!=0:raise ValueError('tick_day_unverified')
    if type(maximum)not in (int,float) or not math.isfinite(maximum) or maximum<0:raise ValueError('tick_history_unverified')
    original=[];seen=set()
    for item in signals:
        e=item['event'];t=timestamp(e['observed_at']);sha=item['event_sha256']
        if (sha!=digest(e) or e.get('schema')!='paper-strategy-signal-event-v1' or e.get('mode')!='paper'
                or not checked[0][0]<=t<=checked[-1][0] or sha in seen):raise ValueError('signal_source_unverified')
        seen.add(sha);original.append((t,e,sha))
    original.sort(key=lambda row:row[0])
    # Every producer pulse binds success count and last exact original digest.
    baseline_count=checked[0][1]['signal_success_count'];baseline_sha=checked[0][1]['signal_last_event_sha256']
    if type(baseline_count)is not int or baseline_count<0:raise ValueError('signal_count_unverified')
    for t,e in checked:
        prior=[row for row in original if row[0]<=t]
        if e.get('signal_success_count')!=baseline_count+len(prior) or e.get('signal_last_event_sha256')!=(prior[-1][2] if prior else baseline_sha):
            raise ValueError('signal_inventory_incomplete')
    eligible={};outcomes={};lost=0
    benign={'insufficient_cash','await_reconciliation','entry_not_executable','vi_active_unfillable'}
    operational={'paper_execution_stream_unavailable','execution_stream_not_ready','recorder_storage_unavailable'}
    for t,e,sha in original:
        if not start<=t<end:continue
        if e.get('event_kind')not in ('eligible_signal','decision_outcome'):continue
        key=(e.get('strategy'),e.get('symbol'),e.get('side'),e.get('signal_key'))
        if not isinstance(key[-1],str) or not key[-1]:raise ValueError('signal_identity_unverified')
        if e['event_kind']=='eligible_signal':
            if key in eligible:raise ValueError('duplicate_eligible_signal')
            eligible[key]=e
        else:
            if key in outcomes:raise ValueError('duplicate_signal_outcome')
            outcomes[key]=e
    if set(eligible)!=set(outcomes):raise ValueError('signal_outcome_missing')
    for e in outcomes.values():
        if e.get('outcome')=='accepted' and e.get('submitted')is True:continue
        reason=e.get('skip_reason')
        if reason in operational:lost+=1
        elif reason not in benign:raise ValueError('signal_failure_cause_unclassified')
    counts=[e.get('manual_interventions') for _,e in checked]
    if any(type(v)is not int or v<0 for v in counts) or counts!=sorted(counts):raise ValueError('operator_history_unverified')
    controls={'status':'unverified','coverage_complete':False,'manual_interventions':None,
        'receiver_restarts':None,'source_inventory':{},'scope':'registered_software_controls_with_drift_witness'}
    if isinstance(evidence.get('control_evidence'),dict):
        from kiwoom_autotrader.utils.control_audit import compile_control_audit
        controls=compile_control_audit(evidence['control_evidence'],start=start,end=end,now=at)
    control_complete=controls.get('status')=='verified' and controls.get('coverage_complete')is True
    authenticated=after[1]['manual_interventions']-before[1]['manual_interventions']
    return {'schema':'paper-stability-operations-audit-v1','compiler':'receiver-pulse-and-signal-inventory-v1',
        'session_day':official['day'],'compiled_at':at.isoformat(),'coverage_complete':control_complete,
        'covered_from':start.isoformat(),'covered_through':end.isoformat(),
        'calendar_sha256':official['calendar_sha256'],'source_inventory':{
            'receiver_events':digest(pulses),'signal_events':digest(signals),**controls['source_inventory']},'evidence':evidence,
        'ready_transitions_complete':True,'tick_history_complete':True,'signal_outcomes_complete':True,
        'operator_actions_complete':control_complete,'external_control_audit_status':controls['status'],
        'registered_controls':controls,
        'process_history_complete':True,'ready_seconds':(end-start).total_seconds(),
        'maximum_tick_gap_seconds':maximum,'confirmed_operational_lost_signals':lost,
        'manual_interventions':authenticated+controls['manual_interventions'] if control_complete else None,
        'authenticated_command_interventions':authenticated,
        'receiver_restarts':controls['receiver_restarts'] if control_complete else None,
        'limitations':['single_uninterrupted_recorder_epoch_only',
            'manual_count_is_authenticated_commands_plus_registered_control_actions;one_request_may_create_multiple_actions',
            'registered_software_controls_and_periodic_drift_witness;not_omniscient_OS_audit']}
