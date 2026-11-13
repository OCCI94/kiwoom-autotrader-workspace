"""Read immutable postclose minute archives for the actual ORB denominator.

No HTTP, IPC, token, synthetic minute, or zero filling. An absent session is
unknown. SQLite remains the trading ledger; these market files grant no order
permission and their original availability time is retained.
"""
from datetime import datetime,timedelta
from pathlib import Path
import hashlib,json,os,re,stat
from kiwoom_autotrader.api.session import KST

SCHEMA='paper-experiment-same-window-history-v1'
DEFAULT_ROOT=Path('/Volumes/AIStockData/ai-stock-research-data/paper-experiment-postclose')
SSD_UUID='7440E396-8F62-4205-9EC6-16F02440C7C5'


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=True,allow_nan=False).encode()).hexdigest()

def at(value):
    result=datetime.fromisoformat(value)
    if result.tzinfo is None:raise ValueError('history_naive_time')
    return result.astimezone(KST)

def sha(value):return isinstance(value,str) and re.fullmatch('[a-f0-9]{64}',value) is not None

def _validate_row(row,now):
    day=row['date'];observed=at(row['available_at'])
    if (not re.fullmatch('[0-9]{8}',day) or day>=now.strftime('%Y%m%d') or not observed<=now
            or row.get('start')!='09:00' or row.get('end')!='09:05'
            or not all(sha(row.get(k)) for k in ('archive_sha256','manifest_sha256','wire_sha256'))):
        raise ValueError('orb_history_source_or_time_unverified')
    mins=row.get('minutes');expected=[datetime.strptime(day+'090000','%Y%m%d%H%M%S').replace(tzinfo=KST)+timedelta(minutes=i) for i in range(5)]
    if not isinstance(mins,list) or len(mins)!=5 or [at(r['start_at']) for r in mins]!=expected:
        raise ValueError('orb_history_exact_first_five_minutes_required')
    for minute,start in zip(mins,expected):
        nums=[minute.get(k) for k in ('open','high','low','close','volume')]
        if (any(type(n) is not int for n in nums) or min(nums[:4])<=0 or nums[-1]<0
                or not minute['low']<=min(minute['open'],minute['close'])<=max(minute['open'],minute['close'])<=minute['high']
                or minute.get('completed') is not True or at(minute['end_at'])!=start+timedelta(minutes=1)
                or not at(minute['end_at'])<=at(minute['available_at'])<=observed):
            raise ValueError('orb_history_minute_integrity')
    if type(row.get('volume')) is not int or row['volume']!=sum(r['volume'] for r in mins):
        raise ValueError('orb_history_volume_recalculation_failed')
    return observed


def validate_history(value,*,symbol,prior_dates,now):
    if (not isinstance(value,dict) or value.get('schema')!=SCHEMA or value.get('symbol')!=symbol
            or value.get('mode')!='paper' or value.get('exchange')!='KRX'
            or value.get('source_sha256')!=digest({k:v for k,v in value.items() if k!='source_sha256'})
            or value.get('status')!='observed' or value.get('observed_sessions')!=14
            or value.get('expected_sessions')!=14 or value.get('synthetic_bars_created') is not False
            or value.get('daily_volume_fallback_used') is not False or len(prior_dates)!=14
            or len(set(prior_dates))!=14 or list(prior_dates)!=sorted(prior_dates)):
        raise ValueError('orb_actual_same_window_14_sessions_required')
    rows=value.get('rows')
    if not isinstance(rows,list) or [r.get('date') for r in rows]!=list(prior_dates):raise ValueError('orb_history_session_grid_unverified')
    available=[_validate_row(row,now) for row in rows]
    if at(value['available_at'])!=max(available):raise ValueError('orb_history_available_at_not_original')
    return value


class SavedSameWindowHistory:
    def __init__(self,root=None):self.root=Path(root) if root is not None else DEFAULT_ROOT;self.default=root is None

    def _read(self,path,limit):
        if any(p.is_symlink() for p in (path,*path.parents)):raise ValueError('history_symlink')
        if self.default:
            mount=Path('/Volumes/AIStockData')
            if (not os.path.ismount(mount) or mount.stat().st_dev==mount.parent.stat().st_dev
                    or (mount/'.ssd-volume-sentinel').read_text().strip()!=SSD_UUID):raise ValueError('history_ssd_unavailable')
        fd=os.open(path,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0))
        with os.fdopen(fd,'rb') as stream:
            before=os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or not 0<before.st_size<=limit:raise ValueError('history_file_bound')
            raw=stream.read(limit+1);after=os.fstat(stream.fileno())
        if (len(raw)!=before.st_size or (before.st_dev,before.st_ino,before.st_mtime_ns,before.st_size)
                !=(after.st_dev,after.st_ino,after.st_mtime_ns,after.st_size)):raise ValueError('history_file_changed')
        return json.loads(raw),hashlib.sha256(raw).hexdigest()

    def read(self,symbol,prior_dates,now):
        if (not re.fullmatch('[0-9]{6}',str(symbol)) or len(prior_dates)!=14 or len(set(prior_dates))!=14
                or prior_dates!=sorted(prior_dates) or now.tzinfo is None
                or any(not re.fullmatch('[0-9]{8}',str(day)) or day>=now.strftime('%Y%m%d') for day in prior_dates)):
            raise ValueError('history_requested_grid_invalid')
        rows=[];missing=[];invalid=[]
        for day in prior_dates:
            try:
                manifest,msha=self._read(self.root/day/'manifest.json',4*1024*1024)
                if (manifest.get('schema')!='paper-experiment-postclose-archive-v1' or manifest.get('day')!=day
                        or manifest.get('status') not in ('completed','HALT')
                        or symbol not in manifest.get('minute_completed_symbols',[])
                        or not at(manifest['completed_at'])<=now
                        or at(manifest['session_start_at']).strftime('%Y%m%d%H:%M')!=day+'09:00'):
                    raise ValueError('history_archive_manifest_unverified')
                file_rows=[r for r in manifest['files'] if r.get('symbol')==symbol]
                if len(file_rows)!=1:raise ValueError('history_file_identity')
                meta=file_rows[0];expected='ai-stock-research-data/paper-experiment-postclose/'+day+'/minutes/'+symbol+'.json'
                if meta.get('relative_path')!=expected:raise ValueError('history_file_path_binding')
                response,file_sha=self._read(self.root/day/'minutes'/(symbol+'.json'),4*1024*1024)
                if (meta.get('sha256')!=file_sha or response.get('symbol')!=symbol or response.get('api_id')!='ka10080'
                        or response.get('requested_exchange')!='KRX' or response.get('environment')!='paper'
                        or not sha(response.get('wire_sha256')) or not isinstance(response.get('raw_projected_rows'),list)
                        or response.get('observed_at')!=meta.get('observed_at')
                        or not at(response['request_started_at'])<=at(response['observed_at'])<=now):
                    raise ValueError('history_response_hash_or_identity')
                observed=response['observed_at'];bars=response.get('bars')
                if not isinstance(bars,list) or len(bars)>10000:raise ValueError('history_bar_bound')
                mins=[{k:b.get(k) for k in ('start_at','end_at','available_at','open','high','low','close','volume','completed')}
                    for b in bars if at(b['start_at']).strftime('%Y%m%d')==day and '09:00'<=at(b['start_at']).strftime('%H:%M')<'09:05']
                mins.sort(key=lambda r:r['start_at'])
                # Recompute the exact five normalized minutes from the saved
                # provider projection; a normalized total alone is insufficient.
                projected=response['raw_projected_rows']
                for minute in mins:
                    stamp=at(minute['start_at']).strftime('%Y%m%d%H%M%S')
                    source=[r for r in projected if isinstance(r,dict) and r.get('cntr_tm')==stamp]
                    if len(source)!=1:raise ValueError('history_provider_minute_missing_or_duplicate')
                    for key,raw_key in (('open','open_pric'),('high','high_pric'),('low','low_pric'),('close','cur_prc'),('volume','trde_qty')):
                        raw_value=source[0].get(raw_key)
                        if (not isinstance(raw_value,(str,int)) or isinstance(raw_value,bool)
                                or not re.fullmatch('[+-]?[0-9]+',str(raw_value))
                                or (key=='volume' and int(raw_value)<0)
                                or abs(int(raw_value))!=minute[key]):raise ValueError('history_provider_projection_conflict')
                candidate={'date':day,'start':'09:00','end':'09:05','available_at':observed,'minutes':mins,
                    'volume':sum(b['volume'] for b in mins),'archive_sha256':file_sha,'manifest_sha256':msha,
                    'wire_sha256':response['wire_sha256']}
                _validate_row(candidate,now);rows.append(candidate)
            except FileNotFoundError:missing.append(day)
            except (OSError,ValueError,KeyError,TypeError,AttributeError):invalid.append(day)
        result={'schema':SCHEMA,'symbol':symbol,'mode':'paper','exchange':'KRX','expected_sessions':14,
            'observed_sessions':len(rows),'missing_sessions':missing,'invalid_sessions':invalid,'rows':rows,
            'status':'observed' if len(rows)==14 else 'unobserved','reason':None if len(rows)==14 else 'orb_actual_same_window_14_sessions_required',
            'available_at':max((r['available_at'] for r in rows),key=at) if rows else None,
            'read_at':now.isoformat(),'synthetic_bars_created':False,'daily_volume_fallback_used':False}
        result['source_sha256']=digest(result)
        if len(rows)==14:
            try:validate_history(result,symbol=symbol,prior_dates=prior_dates,now=now)
            except (ValueError,KeyError,TypeError):
                result.update(status='unobserved',reason='orb_history_minute_integrity',invalid_sessions=list(prior_dates),observed_sessions=0)
                result['source_sha256']=digest({k:v for k,v in result.items() if k!='source_sha256'})
        return result
