"""Read-only, source-bound permission gate for runtime *code* deployment.

This module does not issue approvals, infer a trading calendar, stop a process,
or write a marker. A restart-only permission is not permission to replace code.
Production callers use the real clock; ``now`` is solely a pure-test seam.
"""
from datetime import datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import uuid
from zoneinfo import ZoneInfo

KST = ZoneInfo('Asia/Seoul')
NORMAL_DEPLOYMENT_WEEKDAYS = frozenset((1, 3))  # Tuesday / Thursday, KST calendar date.


class CodeDeploymentDenied(ValueError):
    pass


def manifest_sha256(manifest):
    if (not isinstance(manifest, dict) or not manifest or len(manifest) > 10000
            or any(not isinstance(k, str) or not k or k.startswith('/') or '..' in Path(k).parts
                   or not isinstance(v, str) or re.fullmatch('[0-9a-f]{64}', v) is None
                   for k, v in manifest.items())):
        raise CodeDeploymentDenied('CODE_SOURCE_MANIFEST_INVALID')
    return hashlib.sha256(json.dumps(manifest, sort_keys=True, separators=(',', ':'),
                                    ensure_ascii=True).encode('ascii')).hexdigest()


def _read(path, *, private=False, maximum=2*1024*1024):
    try:
        path = Path(path)
        if not path.is_absolute() or any(p.is_symlink() for p in (path, *path.parents)):
            raise ValueError
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_size > maximum or info.st_mode & 0o022
                    or private and (info.st_mode & 0o077 or info.st_uid != os.getuid())):
                raise ValueError
            raw = stream.read(maximum+1)
        if len(raw) > maximum: raise ValueError
        def unique(pairs):
            value = {}
            for key, item in pairs:
                if key in value: raise ValueError
                value[key] = item
            return value
        value = json.loads(raw, object_pairs_hook=unique)
        if not isinstance(value, dict): raise ValueError
        return value, hashlib.sha256(raw).hexdigest()
    except (OSError, ValueError, TypeError):
        raise CodeDeploymentDenied('CODE_DEPLOYMENT_EVIDENCE_UNREADABLE') from None


def _time(value):
    moment = datetime.fromisoformat(value)
    if moment.tzinfo is None or moment.utcoffset() is None: raise ValueError
    return moment.astimezone(KST)


def _official_session(proof, now):
    """Bind the existing worker claim to its exact stored Toss wire envelope."""
    claim, claim_sha = _read(proof['claim_path'], maximum=16384)
    envelope, file_sha = _read(proof['calendar_path'])
    response = envelope['result']
    today = response['response']['result']['today']
    observed = _time(response['observed_at']); claimed = _time(claim['claimed_at'])
    request_started = _time(response['request_started_at'])
    start = _time(today['integrated']['regularMarket']['startTime'])
    end = _time(today['integrated']['regularMarket']['endTime'])
    wire = response['response_sha256']
    if (proof.get('claim_sha256') != claim_sha or proof.get('calendar_file_sha256') != file_sha
            or envelope.get('schema') != 'toss-decoded-response-evidence-v1'
            or envelope.get('request', {}).get('endpoint_key') != 'market_calendar_kr'
            or envelope.get('request', {}).get('market_country') != 'KR'
            or envelope.get('request', {}).get('params', {}).get('date') != now.date().isoformat()
            or response.get('source') != 'toss_openapi'
            or response.get('endpoint_key') != 'market_calendar_kr' or response.get('status') != 'ok'
            or not isinstance(wire, str) or re.fullmatch('[0-9a-f]{64}', wire) is None
            or claim.get('schema') != 'toss-dart-refresh-slot-claim-v1'
            or claim.get('calendar_source_sha256') != wire or proof.get('calendar_source_sha256') != wire
            or claim.get('session_day') != now.date().isoformat() or today.get('date') != now.date().isoformat()
            or not start.date() == end.date() == observed.date() == claimed.date() == now.date()
            or not start < end or not request_started <= observed <= now or claimed > now
            or request_started.date() != now.date()
            or abs((claimed-observed).total_seconds()) > 900
            or _time(claim['regular_start']) != start or _time(claim['regular_end']) != end):
        raise CodeDeploymentDenied('OFFICIAL_CURRENT_SESSION_UNVERIFIED')
    previous_day=None
    prior=response['response']['result'].get('previousBusinessDay')
    candidate=prior.get('date') if isinstance(prior,dict) else None
    if isinstance(candidate,str) and re.fullmatch(r'\d{4}-\d{2}-\d{2}',candidate):
        try:
            parsed=datetime.strptime(candidate,'%Y-%m-%d').date()
            if parsed<now.date():previous_day=parsed.strftime('%Y%m%d')
        except ValueError:pass
    return {'start': start, 'end': end, 'observed_at': observed, 'previous_day':previous_day, 'calendar_source_sha256': wire,
            'calendar_file_sha256': file_sha, 'claim_sha256': claim_sha}


def deployment_window(moment, session):
    """Pure clock/session policy. The overnight window does not carry weekdays.

    Tuesday 23:00 is a normal window; Wednesday 00:00 is not. A holiday or
    missing current-day session is not inferred from a weekday/calendar guess.
    A preopen deployment needs a current-day observation no older than 30m.
    """
    moment = moment.astimezone(KST)
    hhmmss = moment.strftime('%H:%M:%S')
    inside = session['start'] <= moment < session['end']
    preopen = hhmmss < '08:30:00' and moment < session['start']
    afterclose = hhmmss >= '15:40:00' and moment >= session['end']
    fresh_preopen = 0 <= (moment-session['observed_at']).total_seconds() <= 1800
    allowed = (moment.weekday() in NORMAL_DEPLOYMENT_WEEKDAYS and not inside
               and (afterclose or preopen and fresh_preopen))
    return {'normal_window': allowed, 'inside_official_session': inside,
            'phase': 'preopen' if preopen else 'afterclose' if afterclose else 'restricted',
            'official_phase':'preopen' if moment<session['start'] else 'regular' if inside else 'closed',
            'calendar_weekday_kst': moment.weekday(), 'fresh_preopen_calendar': fresh_preopen}


def _throughput_stability(approval, moment, session):
    change_class=approval.get('change_class','maintenance')
    if change_class=='maintenance':return None
    if change_class!='throughput':raise CodeDeploymentDenied('CODE_CHANGE_CLASS_INVALID')
    evidence=approval.get('stability_evidence',{})
    # Ten complete sessions can include bounded minute/event evidence. Keep
    # approval/calendar limits unchanged and cap only this proof at 32 MiB (actual ten-session producer is >16 MiB).
    proof,digest=_read(evidence.get('path'),maximum=32*1024*1024)
    if evidence.get('sha256')!=digest:raise CodeDeploymentDenied('TEN_SESSION_STABILITY_PROOF_CHANGED')
    from kiwoom_autotrader.trading.stability_metrics import validate_ten_session_proof
    validated=validate_ten_session_proof(proof,now=moment)
    if validated.get('status')!='verified' or validated.get('complete') is not True:
        raise CodeDeploymentDenied('TEN_SESSION_STABILITY_REQUIRED')
    # A linked historical success is insufficient after a newer session closes.
    # Before today's official close, only the provider's previousBusinessDay
    # establishes the latest completed session; never guess weekdays/holidays.
    expected=(session['end'].strftime('%Y%m%d') if moment>=session['end'] else session.get('previous_day'))
    if expected is None:raise CodeDeploymentDenied('LATEST_COMPLETED_OFFICIAL_SESSION_UNVERIFIED')
    if proof.get('through_session_day')!=expected:
        raise CodeDeploymentDenied('TEN_SESSION_STABILITY_NOT_LATEST_COMPLETED')
    return {'proof_sha256':digest,'through_session_day':proof.get('through_session_day'),
            'latest_completed_official_session_day':expected}


def claim_code_deployment(directory, proof, *, now=None):
    """Consume one normal Tue/Thu date or one explicit exception approval.

    Call only after permission, inside the caller's deployment lock. Claims
    survive failure/unknown outcome. A second invocation never reuses a claim,
    even with the same approval; only verify_deployment_claim is idempotent.
    """
    try:
        moment=now if now is not None else datetime.now(KST)
        if not isinstance(moment,datetime) or moment.tzinfo is None:raise ValueError
        moment=moment.astimezone(KST);checked=_time(proof['checked_at'])
        fields=('approval_sha256','source_manifest_sha256','calendar_source_sha256','calendar_file_sha256','claim_sha256')
        if (proof.get('schema')!='runtime-code-deployment-guard-proof-v1'
                or not 0<=(moment-checked).total_seconds()<=5 or checked.date()!=moment.date()
                or type(proof.get('receiver_pid')) is not int or proof['receiver_pid']<=1
                or any(re.fullmatch('[a-f0-9]{64}',proof.get(k,'') or '') is None for k in fields)):
            raise CodeDeploymentDenied('FRESH_CODE_DEPLOYMENT_PERMISSION_REQUIRED')
        kind=proof['kind'];normal=kind in ('after-close-code-approval','scheduled-code-approval')
        if normal:
            if proof.get('window',{}).get('normal_window') is not True or moment.weekday() not in NORMAL_DEPLOYMENT_WEEKDAYS:
                raise CodeDeploymentDenied('NORMAL_DEPLOYMENT_DATE_UNVERIFIED')
            suffix='normal'
        elif kind in ('exception-code-token','intraday-code-token'):
            suffix='exception-'+proof['approval_sha256']
        else:raise CodeDeploymentDenied('CODE_DEPLOYMENT_APPROVAL_KIND_INVALID')
        directory=Path(directory)
        if not directory.is_absolute() or any(p.is_symlink() for p in (directory,*directory.parents)):
            raise CodeDeploymentDenied('DEPLOYMENT_CLAIM_DIRECTORY_UNSAFE')
        directory.mkdir(mode=0o700,exist_ok=True)
        info=directory.stat()
        if not stat.S_ISDIR(info.st_mode) or info.st_mode&0o077 or info.st_uid!=os.getuid():
            raise CodeDeploymentDenied('DEPLOYMENT_CLAIM_DIRECTORY_UNSAFE')
        parent_fd=os.open(directory.parent,os.O_RDONLY|getattr(os,'O_DIRECTORY',0)|getattr(os,'O_NOFOLLOW',0))
        try:os.fsync(parent_fd)
        finally:os.close(parent_fd)
        path=directory/(moment.strftime('%Y%m%d')+'-'+suffix+'.claim.json')
        claim={'schema':'runtime-code-deployment-attempt-v1','attempt_id':uuid.uuid4().hex,
            'date_kst':moment.date().isoformat(),'claimed_at':moment.isoformat(),
            'kind':kind,'normal_quota_consumed':normal,'status':'claimed_before_stop_outcome_unknown',
            'receiver_pid':proof['receiver_pid'],'scope':proof['scope'],
            **{k:proof[k] for k in fields},'automatic_retry':False}
        raw=(json.dumps(claim,sort_keys=True,separators=(',',':'))+'\n').encode()
        try:fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os,'O_NOFOLLOW',0),0o600)
        except FileExistsError:
            raise CodeDeploymentDenied('DEPLOYMENT_DATE_ALREADY_CLAIMED_EXPLICIT_EXCEPTION_REQUIRED' if normal
                else 'CODE_DEPLOYMENT_EXCEPTION_ALREADY_CONSUMED') from None
        with os.fdopen(fd,'wb') as stream:
            stream.write(raw);stream.flush();os.fsync(stream.fileno())
        dfd=os.open(directory,os.O_RDONLY|getattr(os,'O_DIRECTORY',0)|getattr(os,'O_NOFOLLOW',0))
        try:os.fsync(dfd)
        finally:os.close(dfd)
        return {'path':str(path),'sha256':hashlib.sha256(raw).hexdigest(),
            'attempt_id':claim['attempt_id'],'permission':{k:proof[k] for k in (*fields,'receiver_pid','scope','kind')}}
    except CodeDeploymentDenied:raise
    except (OSError,ValueError,TypeError,KeyError,AttributeError):
        raise CodeDeploymentDenied('CODE_DEPLOYMENT_CLAIM_UNCONFIRMED_NO_RETRY') from None


def verify_deployment_claim(attempt, proof):
    """Validate the same in-flight claim without consuming another quota."""
    try:
        claim,digest=_read(attempt['path'],private=True,maximum=16384)
        if (digest!=attempt['sha256'] or claim.get('attempt_id')!=attempt['attempt_id']
                or any(proof.get(k)!=v or claim.get(k)!=v for k,v in attempt['permission'].items())
                or claim.get('date_kst')!=_time(proof['checked_at']).date().isoformat()):
            raise CodeDeploymentDenied('CODE_DEPLOYMENT_ATTEMPT_CHANGED')
        return {'attempt_id':claim['attempt_id'],'date_kst':claim['date_kst'],
            'normal_quota_consumed':claim['normal_quota_consumed'],'claim_sha256':digest}
    except CodeDeploymentDenied:raise
    except (OSError,ValueError,TypeError,KeyError,AttributeError):
        raise CodeDeploymentDenied('CODE_DEPLOYMENT_ATTEMPT_UNVERIFIED') from None


def assert_code_deployment(approval_path, *, source_manifest, receiver_pid,
                           receiver_state_path, scope, now=None):
    """Return a small audit proof, or reject before any deployment mutation."""
    try:
        moment = now if now is not None else datetime.now(KST)
        if not isinstance(moment, datetime) or moment.tzinfo is None: raise ValueError
        moment = moment.astimezone(KST)
        if approval_path is None: raise CodeDeploymentDenied('EXPLICIT_CODE_DEPLOYMENT_APPROVAL_REQUIRED')
        approval, approval_sha = _read(approval_path, private=True, maximum=16384)
        issued = _time(approval['issued_at']); expires = _time(approval['expires_at'])
        request = approval['user_approval']
        text = request.get('text', '')
        reference = request.get('reference', '')
        expected_sha = manifest_sha256(source_manifest)
        if (approval.get('schema') != 'runtime-code-deployment-approval-v1'
                or approval.get('action') != 'code_deploy' or approval.get('scope') != scope
                or scope not in ('receiver_only', 'receiver_and_collector') or approval.get('mode') != 'paper'
                or type(receiver_pid) is not int or receiver_pid <= 1 or approval.get('receiver_pid') != receiver_pid
                or approval.get('source_manifest_sha256') != expected_sha
                or request.get('kind') != 'explicit_code_deployment'
                or not isinstance(reference, str) or not 8 <= len(reference) <= 512
                or not isinstance(text, str) or not 8 <= len(text) <= 4096
                or not (('코드' in text and '배포' in text) or 'code deployment' in text.lower())
                or not issued.date() == expires.date() == moment.date()
                or not issued <= moment < expires or not 0 < (expires-issued).total_seconds() <= 1800):
            raise CodeDeploymentDenied('CODE_DEPLOYMENT_APPROVAL_INVALID_OR_STALE')
        session = _official_session(approval['official_session'], moment)
        stability = _throughput_stability(approval, moment, session)
        state, _ = _read(receiver_state_path, maximum=256*1024)
        if (state.get('pid') != receiver_pid or state.get('automation_status', {}).get('mode') != 'paper'):
            raise CodeDeploymentDenied('CODE_DEPLOYMENT_RECEIVER_IDENTITY_UNVERIFIED')
        kind = approval.get('kind')
        window = deployment_window(moment, session)
        if kind == 'intraday-code-token':
            if (scope != 'receiver_only' or not session['start'] <= moment < session['end']
                    or approval.get('intraday_code_change_explicit') is not True
                    or not (('장중' in text and '코드' in text and '배포' in text)
                            or 'intraday code deployment' in text.lower())
                    or (expires-issued).total_seconds() > 900):
                raise CodeDeploymentDenied('INTRADAY_CODE_TOKEN_REQUIRED')
        elif kind == 'exception-code-token':
            if (scope != 'receiver_only' or approval.get('exception_code_change_explicit') is not True
                    or not (('예외' in text and '코드' in text and '배포' in text)
                            or 'exception code deployment' in text.lower())
                    or (expires-issued).total_seconds() > 900):
                raise CodeDeploymentDenied('EXPLICIT_CODE_WINDOW_EXCEPTION_REQUIRED')
        elif kind in ('after-close-code-approval', 'scheduled-code-approval'):
            if not window['normal_window']:
                raise CodeDeploymentDenied('TUE_THU_OFFICIAL_OUTSIDE_SESSION_WINDOW_REQUIRED')
            if kind == 'after-close-code-approval' and window['phase'] != 'afterclose':
                raise CodeDeploymentDenied('AFTER_CLOSE_APPROVAL_CANNOT_AUTHORIZE_PREOPEN')
        else:
            raise CodeDeploymentDenied('CODE_DEPLOYMENT_APPROVAL_KIND_INVALID')
        return {'schema':'runtime-code-deployment-guard-proof-v1', 'approval_sha256':approval_sha,
                'source_manifest_sha256':expected_sha, 'receiver_pid':receiver_pid, 'scope':scope,
                'kind':kind, 'checked_at':moment.isoformat(), 'window':window,
                'throughput_stability':stability,
                'calendar_source_sha256':session['calendar_source_sha256'],
                'calendar_file_sha256':session['calendar_file_sha256'], 'claim_sha256':session['claim_sha256']}
    except CodeDeploymentDenied:
        raise
    except (OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError):
        raise CodeDeploymentDenied('CODE_DEPLOYMENT_APPROVAL_INVALID_OR_STALE') from None
