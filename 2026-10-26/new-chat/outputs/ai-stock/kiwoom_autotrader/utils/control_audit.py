"""Registered control-plane journal; no claim about every OS/user action.

Mutators append durable intents/results. Read-only witnesses bind the journal
head, installed hook sources, protected-file hashes and component identities.
Missing continuity or unexplained drift is unknown, never zero interventions.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime
import hashlib
import json
from pathlib import Path
import stat
import uuid
from functools import wraps
import inspect
import re
from .platform_io import os,fcntl,sync_directory
from .paths import app_data_root
from ..trading.stability_metrics import KST,digest,timestamp

_origin=ContextVar('registered_control_origin',default='unclassified')
COMPONENTS=frozenset(('receiver','collector','toss','settings','deployment'))
ORIGINS=frozenset(('authenticated_user','approved_maintenance','automatic_recovery'))
MAXIMUM=32*1024*1024


class ControlAuditError(OSError):
    pass


def _moment(now):
    return timestamp(now() if callable(now) else now or datetime.now(KST))


def _safe(path):
    path=Path(path).absolute()
    # macOS canonical system aliases are trusted; application-controlled links are not.
    for alias in ('/var','/tmp'):
        base=Path(alias)
        if (path==base or base in path.parents) and base.is_symlink() and base.resolve()==Path('/private'+alias):
            path=Path('/private'+alias)/path.relative_to(base)
    if any(p.is_symlink() for p in (path,*path.parents)):raise ControlAuditError('control_audit_symlink_refused')
    return path


def _file_sha(path):
    path=_safe(path)
    if not path.exists():return None
    if not path.is_file():raise ControlAuditError('control_audit_regular_file_required')
    with path.open('rb') as stream:
        before=os.fstat(stream.fileno());raw=stream.read(MAXIMUM+1);after=os.fstat(stream.fileno())
    if len(raw)>MAXIMUM or (before.st_ino,before.st_size,before.st_mtime_ns)!=(after.st_ino,after.st_size,after.st_mtime_ns):
        raise ControlAuditError('control_audit_changed_or_large_file')
    return hashlib.sha256(raw).hexdigest()


def path_id(path):return hashlib.sha256(str(_safe(path)).encode('utf-8')).hexdigest()


def hook_manifest_sha256():
    package=Path(__file__).resolve().parents[1]
    names=('utils/control_audit.py','config/settings.py','utils/telegram_service.py','utils/account_service.py')
    hashes={name:_file_sha(package/name) for name in names}
    if any(value is None for value in hashes.values()):raise ControlAuditError('control_hooks_missing')
    return digest(hashes)


def _location(root,at):
    base=_safe(root or app_data_root())
    return base/'observability',base/'observability'/('control-audit-'+at.strftime('%Y%m%d')+'.jsonl')


def _events(raw):
    if len(raw)>MAXIMUM:raise ControlAuditError('control_audit_capacity_exceeded')
    rows=[];previous=None
    for line in raw.splitlines():
        item=json.loads(line);event=item['event']
        if (item.get('event_sha256')!=digest(event) or event.get('sequence')!=len(rows)
                or event.get('previous_sha256')!=previous or event.get('schema')!='registered-control-event-v1'):
            raise ControlAuditError('control_audit_chain_unverified')
        if len(rows)>=50000:raise ControlAuditError('control_audit_capacity_exceeded')
        rows.append(item);previous=item['event_sha256']
    return rows


def _append(event,*,root,at,genesis_only=False):
    directory,path=_location(root,at);directory.mkdir(parents=True,exist_ok=True,mode=0o700)
    info=directory.stat()
    if info.st_mode&0o077 or info.st_uid!=os.getuid():raise ControlAuditError('control_audit_directory_not_private')
    fd=os.open(path,os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
    try:
        fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        info=os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_mode&0o077:raise ControlAuditError('control_audit_file_not_private')
        raw=os.read(fd,MAXIMUM+1);rows=_events(raw)
        if not rows:
            genesis={'schema':'registered-control-event-v1','phase':'genesis','sequence':0,'previous_sha256':None,
                'observed_at':at.isoformat(),'day':at.strftime('%Y%m%d'),'mode':'paper',
                'hook_manifest_sha256':hook_manifest_sha256(),'scope':'registered_software_controls_with_drift_witness'}
            first={'event_sha256':digest(genesis),'event':genesis}
            raw=(json.dumps(first,sort_keys=True,separators=(',',':'))+'\n').encode()
            rows=[first]
            written=0
            while written<len(raw):
                n=os.write(fd,raw[written:])
                if n<=0:raise ControlAuditError("control_audit_short_write")
                written+=n
        if genesis_only:result=rows[0]
        else:
            if timestamp(rows[-1]['event']['observed_at'])>at:raise ControlAuditError('control_audit_clock_regressed')
            event={**event,'schema':'registered-control-event-v1','sequence':len(rows),
                'previous_sha256':rows[-1]['event_sha256'],'observed_at':at.isoformat(),
                'day':at.strftime('%Y%m%d'),'mode':'paper'}
            result={'event_sha256':digest(event),'event':event}
            addition=(json.dumps(result,sort_keys=True,separators=(',',':'))+'\n').encode()
            if len(raw)+len(addition)>MAXIMUM:raise ControlAuditError('control_audit_capacity_exceeded')
            os.lseek(fd,0,os.SEEK_END);written=0
            while written<len(addition):
                n=os.write(fd,addition[written:])
                if n<=0:raise ControlAuditError('control_audit_short_write')
                written+=n
        os.fsync(fd);sync_directory(directory)
        return result
    except (ValueError,KeyError,TypeError,OSError) as error:
        raise ControlAuditError('control_audit_evidence_unverified') from error
    finally:os.close(fd)


def initialize_control_audit(now,root=None):
    at=_moment(now)
    return _append({},root=root,at=at,genesis_only=True)


@contextmanager
def control_origin(origin):
    if origin not in ORIGINS:raise ControlAuditError('control_origin_invalid')
    token=_origin.set(origin)
    try:yield
    finally:_origin.reset(token)


@contextmanager
def audit_control(action,component,*,root=None,paths=(),origin=None,now=None):
    if component not in COMPONENTS or action not in ('settings_write','start','stop','install','uninstall','code_deploy'):
        raise ControlAuditError('control_action_invalid')
    at=_moment(now);selected=origin or _origin.get();ident=uuid.uuid4().hex
    protected={path_id(p):(_safe(p),_file_sha(p)) for p in paths}
    before={'phase':'before','action_id':ident,'component':component,'action':action,'origin':selected,
        'controller_pid':os.getpid(),'changes':[{'path_id':key,'before_sha256':value[1]} for key,value in sorted(protected.items())]}
    _append(before,root=root,at=at)  # no mutation if this durable intent fails
    status='failed';outcome={'status':None}
    try:
        yield outcome
        status=outcome['status'] or 'success'
        if status not in ('success','failed'):raise ControlAuditError('control_result_status_invalid')
    finally:
        result={**before,'phase':'result','status':status,'changes':[{'path_id':key,
            'before_sha256':value[1],'after_sha256':_file_sha(value[0])} for key,value in sorted(protected.items())]}
        # Failure here is outcome-unknown; preserve the unfinished intent.
        _append(result,root=root,at=_moment(now))


def registered_mutator(action,component,*,paths_factory=None,error_type=None):
    """Small hook for existing registered service/settings entrypoints."""
    def decorate(function):
        signature=inspect.signature(function)
        @wraps(function)
        def call(*args,**kwargs):
            values=signature.bind(*args,**kwargs);values.apply_defaults();values=values.arguments
            if component=='settings':
                root=None;paths=(values['path'],)
            else:
                service=values.get('paths') or paths_factory();root=service.base;paths=(service.plist,)
            try:
                with audit_control(action,component,root=root,paths=paths):return function(*args,**kwargs)
            except ControlAuditError as error:
                if error_type is not None:raise error_type('등록된 제어 감사 기록을 확인하지 못해 작업을 중단했습니다.') from error
                raise
        return call
    return decorate


def read_control_witness(now,*,root=None,protected_paths,pids):
    """Read only; absence never creates a zero-action journal or genesis."""
    at=_moment(now);_,path=_location(root,at);path=_safe(path)
    with path.open('rb') as stream:
        before=os.fstat(stream.fileno());raw=stream.read(MAXIMUM+1);after=os.fstat(stream.fileno())
    if (before.st_size,before.st_mtime_ns)!=(after.st_size,after.st_mtime_ns):raise ControlAuditError('control_witness_changed')
    rows=_events(raw)
    if not rows:raise ControlAuditError('control_genesis_missing')
    if set(protected_paths)!=set(('runtime','settings','launchagents')) or any(not v for v in protected_paths.values()):
        raise ControlAuditError('protected_path_inventory_missing')
    hashes={};groups={}
    for group,paths in protected_paths.items():
        groups[group]=[]
        for p in paths:
            ident=path_id(p);value=_file_sha(p)
            if value is None:raise ControlAuditError('protected_file_missing')
            hashes[ident]=value;groups[group].append(ident)
    if set(pids)!=set(('receiver','collector','toss')) or any(type(v)is not int or v<=1 for v in pids.values()):
        raise ControlAuditError('protected_process_identity_missing')
    return {'schema':'registered-control-witness-v1','observed_at':at.isoformat(),'day':at.strftime('%Y%m%d'),
        'head_sha256':rows[-1]['event_sha256'],'last_sequence':len(rows)-1,'event_count':len(rows),
        'hook_manifest_sha256':hook_manifest_sha256(),'protected_hashes':hashes,
        'protected_groups':groups,'pids':dict(pids)}


def compile_control_audit(evidence,*,start,end,now):
    """Strict registered-plane continuity; unexplained external drift is unknown."""
    unknown={'status':'unverified','coverage_complete':False,'manual_interventions':None,
        'receiver_restarts':None,'source_inventory':{},'scope':'registered_software_controls_with_drift_witness'}
    try:
        start=timestamp(start);end=timestamp(end);now=timestamp(now)
        events=evidence['events'];witnesses=evidence['witnesses']
        if not start<end<=now or start.date()!=end.date() or not isinstance(events,list) or not isinstance(witnesses,list):raise ValueError
        rows=_events(b'\n'.join(json.dumps(x,sort_keys=True,separators=(',',':')).encode() for x in events))
        if not rows or not 2<=len(witnesses)<=20000:raise ValueError
        first=rows[0]['event'];day=start.strftime('%Y%m%d');hooks=first['hook_manifest_sha256']
        if (first.get('phase')!='genesis' or first.get('day')!=day or first.get('mode')!='paper'
                or timestamp(first['observed_at']).date()!=start.date() or timestamp(first['observed_at'])>start):raise ValueError
        before={};completed={};last=None
        for row in rows[1:]:
            event=row['event'];at=timestamp(event['observed_at']);ident=event['action_id']
            if event.get('day')!=day or event.get('mode')!='paper' or at>now:raise ValueError
            if last is not None and at<last:raise ValueError
            last=at
            if event.get('phase')=='before':
                if ident in before:raise ValueError
                before[ident]=event
            elif event.get('phase')=='result':
                if ident not in before or ident in completed or event.get('status') not in ('success','failed'):raise ValueError
                if any(event.get(k)!=before[ident].get(k) for k in ('origin','action','component','controller_pid')):raise ValueError
                if (event.get('component') not in COMPONENTS or event.get('action') not in
                        ('settings_write','start','stop','install','uninstall','code_deploy')):raise ValueError
                previous_changes={c['path_id']:c['before_sha256'] for c in before[ident]['changes']}
                if {c['path_id']:c['before_sha256'] for c in event['changes']}!=previous_changes:raise ValueError
                completed[ident]=event
            else:raise ValueError
        if set(before)!=set(completed):raise ValueError
        # A sealed preopen action does not contaminate the session KPI. Unknown
        # origin overlapping the measured session remains unverified.
        for ident,event in before.items():
            if timestamp(event['observed_at'])<end and timestamp(completed[ident]['observed_at'])>=start:
                if event.get('origin') not in ORIGINS:raise ValueError
        previous=None;prior_at=None
        for witness in witnesses:
            at=timestamp(witness['observed_at']);seq=witness['last_sequence']
            if (witness.get('schema')!='registered-control-witness-v1' or witness.get('day')!=day
                    or witness.get('hook_manifest_sha256')!=hooks or at>now or at.date()!=start.date()
                    or type(seq)is not int or not 0<=seq<len(rows) or witness.get('event_count')!=seq+1
                    or witness.get('head_sha256')!=rows[seq]['event_sha256']
                    or timestamp(rows[seq]['event']['observed_at'])>at):raise ValueError
            if seq+1<len(rows) and timestamp(rows[seq+1]['event']['observed_at'])<at:raise ValueError
            groups=witness['protected_groups'];hashes=witness['protected_hashes'];pids=witness['pids']
            if set(groups)!=set(('runtime','settings','launchagents')) or any(not v for v in groups.values()):raise ValueError
            if (set(hashes)!=set(x for values in groups.values() for x in values)
                    or any(not isinstance(v,str) or re.fullmatch('[a-f0-9]{64}',v) is None for v in [hooks,*hashes,*hashes.values()])):raise ValueError
            if set(pids)!=set(('receiver','collector','toss')) or any(type(v)is not int or v<=1 for v in pids.values()):raise ValueError
            if previous is not None:
                if not 0<(at-prior_at).total_seconds()<=75 or groups!=previous['protected_groups'] or set(hashes)!=set(previous['protected_hashes']):raise ValueError
                relevant=[event for event in completed.values() if prior_at<=timestamp(event['observed_at'])<=at]
                for ident,old in previous['protected_hashes'].items():
                    if hashes[ident]!=old and not any(any(c.get('path_id')==ident and c.get('before_sha256')==old
                            and c.get('after_sha256')==hashes[ident] for c in event['changes']) for event in relevant):raise ValueError
                for component,old in previous['pids'].items():
                    if pids[component]!=old and not any(event['component'] in (component,'deployment')
                            and event['action'] in ('start','install','code_deploy') and event['status']=='success' for event in relevant):raise ValueError
            previous=witness;prior_at=at
        if timestamp(witnesses[0]['observed_at'])>start or timestamp(witnesses[-1]['observed_at'])<end:raise ValueError
        for ident,event in before.items():
            if event.get('origin') not in ORIGINS:
                done=timestamp(completed[ident]['observed_at'])
                began=timestamp(event['observed_at'])
                sealed_before=done<start and done<=timestamp(witnesses[0]['observed_at'])
                sealed_after=began>=end and began>timestamp(witnesses[-1]['observed_at'])
                if not (sealed_before or sealed_after):raise ValueError
        active=[event for ident,event in before.items() if timestamp(event['observed_at'])<end
            and timestamp(completed[ident]['observed_at'])>=start]
        manual=sum(event['origin']!='automatic_recovery' for event in active)
        restarts=sum(previous['pids']['receiver']!=current['pids']['receiver'] for previous,current in zip(witnesses,witnesses[1:])
            if start<=timestamp(current['observed_at'])<=end)
        return {'status':'verified','coverage_complete':True,'manual_interventions':manual,'receiver_restarts':restarts,
            'automatic_recoveries':sum(event['origin']=='automatic_recovery' for event in active),
            'source_inventory':{'registered_control_events':digest(events),'protected_control_witnesses':digest(witnesses)},
            'scope':unknown['scope'],'limitations':['registered_paths_and_periodic_drift_witness_only;not_omniscient_OS_audit']}
    except (ValueError,KeyError,TypeError,OSError,IndexError):return unknown
