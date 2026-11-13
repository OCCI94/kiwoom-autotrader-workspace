"""Local official calendar plan for historical polling (not order permission)."""
from datetime import datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from kiwoom_autotrader.api.session import KST
from kiwoom_autotrader.utils.paths import app_data_root


def closed_poll_plan(now, day, *, archive_root=None, claim_root=None):
    """Return next explicit trading close and verified elapsed trading dates.

    A missing calendar never becomes a weekday/15:30 guess. At most one saved
    source per date is accepted. This only throttles historical reads and never
    decides market opening, profit attribution, or order eligibility.
    """
    result={'next_close':None,'elapsed_sessions':None,'source_hashes':[],
            'status':'official_calendar_unavailable'}
    if (not isinstance(now,datetime) or now.tzinfo is None or now.utcoffset()!=timedelta(hours=9)
            or re.fullmatch(r'[0-9]{8}',day or '') is None):return result
    archive=Path(archive_root) if archive_root else Path('/Volumes/AIStockData/ai-stock-research-data/s5-full-eligibility')
    claims=Path(claim_root) if claim_root else app_data_root()/'research-readonly/toss-evidence-refresh-claims'
    if archive.is_symlink() or claims.is_symlink():return result
    if archive_root is None:
        try:
            mount=Path('/Volumes/AIStockData')
            if (not os.path.ismount(mount) or mount.stat().st_dev==mount.parent.stat().st_dev
                    or (mount/'.ssd-volume-sentinel').read_text().strip()!='7440E396-8F62-4205-9EC6-16F02440C7C5'):return result
        except OSError:return result
    try:paths=sorted(archive.glob('*/toss/0001-calendar-current.json'),reverse=True)[:512]
    except OSError:return result
    seen={};future=[];current=[]
    for path in paths:
        try:
            info=path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_size>2*1024*1024 or any(p.is_symlink() for p in path.parents):continue
            raw=path.read_bytes();env=json.loads(raw);response=env['result'];body=response['response']['result']
            observed=datetime.fromisoformat(response['observed_at']);wire=response['response_sha256']
            if (env.get('schema')!='toss-decoded-response-evidence-v1' or response.get('status')!='ok'
                    or env.get('request',{}).get('endpoint_key')!='market_calendar_kr'
                    or env['request'].get('market_country')!='KR' or response.get('endpoint_key')!='market_calendar_kr'
                    or observed.tzinfo is None or observed>now or not re.fullmatch('[a-f0-9]{64}',wire)):continue
            today=body['today'];source_day=today['date']
            start=datetime.fromisoformat(today['integrated']['regularMarket']['startTime'])
            end=datetime.fromisoformat(today['integrated']['regularMarket']['endTime'])
            if (start.tzinfo is None or end.tzinfo is None or not start<end
                    or start.date().isoformat()!=source_day or end.date()!=start.date()):continue
            matched=False
            for cp in sorted(claims.glob(source_day.replace('-','')+'-*.claim.json'),reverse=True):
                ci=cp.lstat()
                if not stat.S_ISREG(ci.st_mode) or ci.st_size>16384 or ci.st_mode&0o022 or any(p.is_symlink() for p in cp.parents):continue
                claim=json.loads(cp.read_bytes());claimed=datetime.fromisoformat(claim['claimed_at'])
                if (claim.get('schema')=='toss-dart-refresh-slot-claim-v1' and claim.get('session_day')==source_day
                        and claim.get('calendar_source_sha256')==wire and claimed.tzinfo is not None
                        and claimed<=now and abs((claimed-observed).total_seconds())<=15*60
                        and claimed.date().isoformat()==source_day and observed.date().isoformat()==source_day
                        and claim.get('regular_start')==start.isoformat()
                        and claim.get('regular_end')==end.isoformat()):matched=True;break
            if not matched:continue
            digest=hashlib.sha256(raw).hexdigest()
            if source_day not in seen:seen[source_day]=(end,digest)
            if now.date()==start.date() and (now-observed).total_seconds()<=24*3600:
                current.append((start,end,digest))
                nxt=body['nextBusinessDay'];close=datetime.fromisoformat(nxt['integrated']['regularMarket']['endTime'])
                opening=datetime.fromisoformat(nxt['integrated']['regularMarket']['startTime'])
                if (opening.tzinfo is not None and close.tzinfo is not None and opening<close
                        and close.date()==opening.date() and close.date().isoformat()==nxt['date']
                        and opening.date()>now.date()):future.append((observed,close,digest))
        except (OSError,ValueError,KeyError,TypeError,AttributeError):continue
    elapsed=[(date,digest) for date,(end,digest) in seen.items() if date.replace('-','')>day and end<=now]
    result['elapsed_sessions']=len(elapsed) if seen else None
    result['source_hashes']=[digest for _,digest in sorted(elapsed)]
    if future:
        _,close,digest=max(future)
        result['next_close']=close+timedelta(minutes=1)
        result['source_hashes'].append(digest)
    if current:
        # A work-defer guard may conservatively cover conflicting saved ranges;
        # these fields never grant order permission or assert a live session.
        result['current_session_start']=min(start for start,_,_ in current)
        result['current_session_end']=max(end for _,end,_ in current)
        result['current_session_basis']='hash_bound_saved_current_day_calendar_conservative_union'
        result['source_hashes'].extend(digest for _,_,digest in current)
    if seen:result['status']='official_saved_calendar_verified'
    return result
