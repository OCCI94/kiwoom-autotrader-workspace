"""Read the independent worker's verified current-day session claim, no API.

The claim is an observation from the existing calendar worker, not an order
permission. Missing/current-day delayed sessions never become weekday guesses.
"""
from datetime import datetime,timedelta
import hashlib
import json
import os
from pathlib import Path
import re
import stat

from kiwoom_autotrader.utils.paths import app_data_root


def current_session(now, root=None):
    directory = Path(root) if root else app_data_root()/'research-readonly/toss-evidence-refresh-claims'
    day = now.strftime('%Y%m%d')
    if now.tzinfo is None:
        return None
    for path in sorted(directory.glob(day+'-*.claim.json'), reverse=True):
        try:
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_size > 16384 or info.st_mode & 0o022:
                continue
            raw = json.loads(path.read_bytes())
            start = datetime.fromisoformat(raw['regular_start'])
            end = datetime.fromisoformat(raw['regular_end'])
            claimed = datetime.fromisoformat(raw['claimed_at'])
            if (raw.get('schema')!='toss-dart-refresh-slot-claim-v1'
                    or raw.get('session_day')!=now.date().isoformat()
                    or not start.date()==end.date()==now.date() or not start < end
                    or claimed > now or not re.fullmatch('[a-f0-9]{64}', raw.get('calendar_source_sha256',''))):
                continue
            return {'start':start,'end':end,'session_day':day,
                    'calendar_source_sha256':raw['calendar_source_sha256'], 'claim_path':str(path)}
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return None


def preopen_plan(now, *, archive_root=None, claim_root=None):
    """Read a hash-bound official saved calendar plan; never assert live open.

    The 08:50 worker has not necessarily run at 08:30. Its previous completed
    session may already name today's nextBusinessDay. Only that explicit date
    (or an observed today's calendar) supplies the prior trading day; weekends
    and holidays are never inferred. Missing SSD/cache is a normal unavailable
    input and does not cause a network request.
    """
    if now.tzinfo is None or now.utcoffset()!=timedelta(hours=9) or not '08:30'<=now.strftime('%H:%M')<='08:55':
        return None
    archive=Path(archive_root) if archive_root else Path('/Volumes/AIStockData/ai-stock-research-data/s5-full-eligibility')
    claims=Path(claim_root) if claim_root else app_data_root()/'research-readonly/toss-evidence-refresh-claims'
    if archive.is_symlink() or claims.is_symlink():return None
    if archive_root is None:
        try:
            mount=Path('/Volumes/AIStockData')
            if (not os.path.ismount(mount) or mount.stat().st_dev==mount.parent.stat().st_dev
                    or (mount/'.ssd-volume-sentinel').read_text().strip()!='7440E396-8F62-4205-9EC6-16F02440C7C5'):return None
        except OSError:return None
    try:paths=sorted(archive.glob('*/toss/0001-calendar-current.json'),reverse=True)[:128]
    except OSError:return None
    for path in paths:
        try:
            info=path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_size>2*1024*1024 or any(p.is_symlink() for p in path.parents):continue
            raw=path.read_bytes();env=json.loads(raw);result=env['result'];body=result['response']['result']
            observed=datetime.fromisoformat(result['observed_at']);wire=result['response_sha256']
            if (env.get('schema')!='toss-decoded-response-evidence-v1' or result.get('status')!='ok'
                    or env.get('request',{}).get('endpoint_key')!='market_calendar_kr'
                    or env['request'].get('market_country')!='KR' or result.get('endpoint_key')!='market_calendar_kr'
                    or observed.tzinfo is None or not 0<=(now-observed).total_seconds()<=96*3600
                    or not re.fullmatch('[a-f0-9]{64}',wire)):continue
            today=now.date().isoformat();source_day=body['today']['date']
            if source_day==today:
                planned=body['today'];previous=body['previousBusinessDay']['date']
            elif body['nextBusinessDay']['date']==today:
                planned=body['nextBusinessDay'];previous=source_day
            else:continue
            start=datetime.fromisoformat(planned['integrated']['regularMarket']['startTime'])
            end=datetime.fromisoformat(planned['integrated']['regularMarket']['endTime'])
            if (not start.tzinfo or not end.tzinfo or not now<start<end
                    or start.date()!=now.date() or end.date()!=now.date()
                    or datetime.fromisoformat(previous).date()>=now.date()):continue
            matched=None
            for claim_path in sorted(claims.glob(source_day.replace('-','')+'-*.claim.json'),reverse=True):
                ci=claim_path.lstat()
                if (not stat.S_ISREG(ci.st_mode) or ci.st_size>16384 or ci.st_mode&0o022
                        or any(p.is_symlink() for p in claim_path.parents)):continue
                claim_raw=claim_path.read_bytes();claim=json.loads(claim_raw)
                claimed=datetime.fromisoformat(claim['claimed_at'])
                if (claim.get('schema')=='toss-dart-refresh-slot-claim-v1' and claim.get('session_day')==source_day
                        and claim.get('calendar_source_sha256')==wire and claimed.tzinfo is not None
                        and claimed<=now
                        and datetime.fromisoformat(claim['regular_start'])==datetime.fromisoformat(body['today']['integrated']['regularMarket']['startTime'])
                        and datetime.fromisoformat(claim['regular_end'])==datetime.fromisoformat(body['today']['integrated']['regularMarket']['endTime'])):
                    matched=hashlib.sha256(claim_raw).hexdigest();break
            if matched is None:continue
            return {'schema':'paper-experiment-preopen-plan-v1','session_day':now.strftime('%Y%m%d'),
                'previous_session_day':previous.replace('-',''),'regular_start':start.isoformat(),
                'regular_end':end.isoformat(),'available_at':observed.isoformat(),'read_at':now.isoformat(),
                'calendar_source_sha256':wire,'calendar_file_sha256':hashlib.sha256(raw).hexdigest(),
                'claim_sha256':matched,'basis':'saved_official_current_or_next_business_day',
                'live_regular_session_verified':False}
        except (OSError,ValueError,KeyError,TypeError,AttributeError):continue
    return None
