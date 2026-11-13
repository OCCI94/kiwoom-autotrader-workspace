"""Owned maintenance-only read batches; no orders, intents or settlement writes.

Only the receiver's existing PAPER session/readers/shared HTTP queue are used.
Every completion is a real new query batch, never a timestamp-extended cache.
"""
from datetime import datetime,timedelta
from pathlib import Path
from contextlib import contextmanager
import hashlib,json,math,os,re,sqlite3,stat,time,uuid
from kiwoom_autotrader.api.session import KST
from kiwoom_autotrader.api.minute_chart import session_identity
from kiwoom_autotrader.api.account_report import fetch_account_report
from kiwoom_autotrader.api.executions import _request_rows
from kiwoom_autotrader.api import kt00001,kt00018
from .cancellation_settlement import _fetch_orders,_fetch_pending,_complete_pages,_stream
from .execution_log import _paper_balance_snapshot

SCHEMA='receiver-reconciliation-maintenance-v1'
PURPOSE='readonly_reconciliation_bootstrap'
PHASE_SCHEMA='paper-maintenance-readonly-phase-v1'
CUTOVER_LOCK_SCHEMA='receiver-maintenance-cutover-lock-v1'
CUTOVER_LOCK_NAME='.maintenance-cutover.lock'
class MaintenanceReadUnavailable(ValueError):pass
def _time(value):
 at=datetime.fromisoformat(value) if isinstance(value,str) else value
 if not isinstance(at,datetime) or at.tzinfo is None:raise MaintenanceReadUnavailable('maintenance_clock_unverified')
 return at.astimezone(KST)
def _json(value):return json.dumps(value,ensure_ascii=True,sort_keys=True,separators=(',',':'),allow_nan=False)
def _sha(value):return hashlib.sha256(_json(value).encode()).hexdigest()
def module_sha256():return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
def _cutover_binding(value):
 if (not isinstance(value,dict) or set(value)!={'schema','dev','ino','size','sha256'}
     or value.get('schema')!=CUTOVER_LOCK_SCHEMA
     or any(type(value.get(k))is not int or value[k]<0 for k in ('dev','ino','size'))
     or not 0<value['size']<=4096 or value['ino']<=0
     or not isinstance(value.get('sha256'),str) or re.fullmatch('[0-9a-f]{64}',value['sha256'])is None):
  raise MaintenanceReadUnavailable('maintenance_cutover_binding_invalid')
 return json.loads(_json(value))
def remember_cutover_binding(owner,state_dir):
 """Once bound, an absent marker retains the lock; a downgraded marker fails."""
 previous=vars(owner).get('_cutover_lock_binding')
 if vars(owner).get('_cutover_lock_error')is True:raise MaintenanceReadUnavailable('maintenance_cutover_binding_latched_error')
 path=Path(state_dir)/'maintenance-quiet.lock'
 if not path.exists() and not path.is_symlink():return previous
 raw=b'';read_complete=False
 try:
  fd=os.open(path,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0))
  try:
   info=os.fstat(fd)
   if not stat.S_ISREG(info.st_mode) or info.st_size>4096:raise ValueError()
   raw=os.read(fd,4097)
   after=os.fstat(fd)
   if len(raw)!=info.st_size or any(getattr(info,k)!=getattr(after,k)for k in('st_dev','st_ino','st_size','st_mtime_ns')):raise ValueError()
   read_complete=True;value=json.loads(raw)
  finally:os.close(fd)
  current=_cutover_binding(value['cutover_lock']) if 'cutover_lock'in value else None
  if previous is not None and _sha(current)!=_sha(previous):raise ValueError()
  if current is not None:owner._cutover_lock_binding=current
  return current
 except Exception:
  if previous is None and read_complete and not path.is_symlink():
   # Legacy malformed markers still fail their existing scope validator. They
   # cannot downgrade an already-bound process into this compatibility path.
   try:
    if 'cutover_lock'not in json.loads(raw):return None
   except Exception:pass
   # Existing bounded legacy markers are empty or human-readable notes. A
   # completed read of those never opts into this new bound-lock protocol.
   if not any(key in raw for key in(b'cutover_lock',b'receiver-reconciliation-maintenance',b'reconciliation_required')):return None
  owner._cutover_lock_error=True;owner.blocked=True
  raise MaintenanceReadUnavailable('maintenance_cutover_binding_changed')from None
@contextmanager
def cutover_guard(state_dir,binding):
 """Nonblocking common actor/tool mutex; no lock creation or clock extension."""
 if binding is None:
  yield True;return  # Legacy callers; new deployment protocol requires binding.
 binding=_cutover_binding(binding);path=Path(state_dir)/CUTOVER_LOCK_NAME
 if any(p.is_symlink()for p in(path,*path.parents)):raise MaintenanceReadUnavailable('maintenance_cutover_symlink')
 import fcntl
 fd=os.open(path,os.O_RDWR|os.O_NOFOLLOW);held=False
 def verify():
  if any(p.is_symlink()for p in(path,*path.parents)):raise MaintenanceReadUnavailable('maintenance_cutover_symlink')
  s=os.fstat(fd);p=path.lstat();os.lseek(fd,0,os.SEEK_SET);raw=os.read(fd,4097)
  actual={'schema':CUTOVER_LOCK_SCHEMA,'dev':s.st_dev,'ino':s.st_ino,'size':s.st_size,'sha256':hashlib.sha256(raw).hexdigest()}
  if (not stat.S_ISREG(s.st_mode) or not stat.S_ISREG(p.st_mode) or s.st_uid!=os.getuid() or p.st_uid!=os.getuid()
      or stat.S_IMODE(s.st_mode)!=0o600 or stat.S_IMODE(p.st_mode)!=0o600
      or (p.st_dev,p.st_ino)!=(s.st_dev,s.st_ino) or _sha(actual)!=_sha(binding)):
   raise MaintenanceReadUnavailable('maintenance_cutover_lock_changed')
 try:
  verify()
  try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB);held=True
  except BlockingIOError:
   yield False;return
  verify();yield True;verify()
 finally:
  if held:fcntl.flock(fd,fcntl.LOCK_UN)
  os.close(fd)
def _history_request(day):
 return {'ord_dt':day,'qry_tp':'1','stk_bond_tp':'1','sell_tp':'0','stk_cd':'','fr_ord_no':'','dmst_stex_tp':'KRX'}
def _fetch_history(session,*,observer,day,before_send):
 return _request_rows(session,'kt00007',_history_request(day),'acnt_ord_cntr_prps_dtl',
  observer=observer,day=day,before_send=before_send)
def _marker_days(value,now):
 current=now.strftime('%Y%m%d');prior=[]
 if 'current_day' in value or 'prior_order_days' in value:
  if value.get('current_day')!=current:raise MaintenanceReadUnavailable('maintenance_marker_current_day_invalid')
  prior=value.get('prior_order_days',[])
  if not isinstance(prior,list) or len(prior)>1:raise MaintenanceReadUnavailable('maintenance_marker_prior_days_invalid')
  for day in prior:
   try:
    parsed=datetime.strptime(day,'%Y%m%d').date()
    valid=(isinstance(day,str) and parsed.strftime('%Y%m%d')==day and 0<(now.date()-parsed).days<=31)
   except (ValueError,TypeError):valid=False
   if not valid:raise MaintenanceReadUnavailable('maintenance_marker_prior_days_invalid')
 return current,prior
def _marker(state_dir,now):
 path=Path(state_dir)/'maintenance-quiet.lock';fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
 try:
  before=os.fstat(fd)
  if not stat.S_ISREG(before.st_mode) or before.st_uid!=os.getuid() or before.st_mode&0o077 or not 0<before.st_size<=4096:raise MaintenanceReadUnavailable('maintenance_marker_private_required')
  raw=os.read(fd,4097);after=os.fstat(fd)
  if any(getattr(before,k)!=getattr(after,k) for k in ('st_dev','st_ino','st_mtime_ns','st_size')):raise MaintenanceReadUnavailable('maintenance_marker_changed')
 finally:os.close(fd)
 value=json.loads(raw)
 if (value.get('schema')!=SCHEMA or value.get('purpose')!=PURPOSE or value.get('reconciliation_required') is not True
    or re.fullmatch('[0-9a-f]{32}',str(value.get('marker_id',''))) is None
    or type(value.get('owner_process_pid')) is not int or value['owner_process_pid']<=1
    or value.get('maintenance_module_sha256')!=module_sha256()
    or not _time(value['created_at'])<=now<_time(value['expires_at'])
    or not 0<(_time(value['expires_at'])-_time(value['created_at'])).total_seconds()<=180):
  raise MaintenanceReadUnavailable('maintenance_marker_scope_or_lease_invalid')
 current,prior=_marker_days(value,now)
 result={'sha256':hashlib.sha256(raw).hexdigest(),'dev':before.st_dev,'ino':before.st_ino,
  'mtime_ns':before.st_mtime_ns,'size':before.st_size,'marker_id':value['marker_id'],'owner_process_pid':value['owner_process_pid'],
  'current_day':current,'prior_order_days':prior}
 if 'cutover_lock'in value:result['cutover_lock']=_cutover_binding(value['cutover_lock'])
 return result
def _ledger(tracker):
 path=Path(tracker.path)
 if path.is_symlink() or not path.is_file():raise MaintenanceReadUnavailable('maintenance_ledger_unavailable')
 db=sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=2);db.row_factory=sqlite3.Row
 db.execute('PRAGMA query_only=ON');db.execute('BEGIN');return db
def _watermark(tracker):
 db=_ledger(tracker)
 try:
  result={}
  for table in ('intents','orders','fills','order_decisions'):
   cols=[r[1] for r in db.execute('PRAGMA table_info('+table+')')]
   if not cols:raise MaintenanceReadUnavailable('maintenance_core_schema_missing')
   h=hashlib.sha256();count=0
   for row in db.execute('SELECT * FROM '+table+' ORDER BY '+','.join(str(i+1) for i in range(len(cols)))):
    count+=1
    if count>100000:raise MaintenanceReadUnavailable('maintenance_core_bound')
    values=[{'blob_sha256':hashlib.sha256(v).hexdigest()} if isinstance(v,bytes) else v for v in row]
    h.update(_json(values).encode()+b'\n')
   result[table]={'columns':cols,'rows':count,'sha256':h.hexdigest()}
  return _sha(result)
 finally:db.close()
def _balance(tracker,day):
 db=_ledger(tracker)
 try:
  row=db.execute("SELECT observed_at,source_kind,payload_json FROM account_balance_daily WHERE scope=? AND mode='paper' AND day=?",(tracker.scope,day)).fetchone()
  return dict(row) if row is not None else None
 finally:db.close()
def _write_new(path,value):
 path=Path(path);path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
 if any(p.is_symlink() for p in (path,*path.parents)):raise MaintenanceReadUnavailable('maintenance_evidence_symlink')
 fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
 with os.fdopen(fd,'wb') as out:out.write((_json(value)+'\n').encode());out.flush();os.fsync(out.fileno())

class MaintenanceReadOnlyReconciliation:
 def __init__(self,*,now=None,monotonic=None,orders_fetcher=None,account_fetcher=None,pending_fetcher=None,history_fetcher=None,progress=None):
  self.now=now or(lambda:datetime.now(KST));self.monotonic=monotonic or time.monotonic
  self.orders=orders_fetcher or _fetch_orders;self.account=account_fetcher or fetch_account_report;self.pending=pending_fetcher or _fetch_pending
  self.history=history_fetcher or _fetch_history
  self.progress=progress;self._status={'schema':PHASE_SCHEMA,'status':'inactive','orders_sent':0,'settlements_written':False}
  self.blocked=False;self.next_at=None;self._completed_identity=None;self._completed_stream=None;self._marker_identity=None
 def status(self):return json.loads(_json(self._status))
 def latch(self,state_dir):
  """Recognize dedicated maintenance before login/queue/poll startup gaps."""
  from kiwoom_autotrader.telegram.receiver_health import maintenance_status
  try:remember_cutover_binding(self,state_dir)
  except MaintenanceReadUnavailable:self.blocked=True
  if maintenance_status(state_dir,now=_time(self.now())).get('reconciliation_required'):
   self.blocked=True
   if self._status.get('status')=='inactive':self._status.update(status='waiting_stream',reason='maintenance_bootstrap_latched')
  return self.blocked
 def _publish(self):
  if self.progress is not None:self.progress(self.status())
 def assert_orders_allowed(self):
  if self.blocked:raise MaintenanceReadUnavailable('maintenance_reconciliation_orders_blocked')
 def release_if_verified(self,session,tracker):
  if not self.blocked:return True
  if getattr(self,'_cutover_lock_error',False):return False
  s=self._status
  try:
   if (s.get('status')!='complete' or not 0<=(_time(self.now())-_time(s['started_at'])).total_seconds()<=90
       or s.get('current_day')!=_time(self.now()).strftime('%Y%m%d')
       or _time(s['started_at']).date()!=_time(self.now()).date() or _time(s['completed_at']).date()!=_time(self.now()).date()
       or not _time(s['started_at'])<=_time(s['completed_at'])<=_time(self.now())
       or session_identity(session)!=self._completed_identity or _stream(session)!=self._completed_stream
       or _watermark(tracker)!=s['financial_watermark']):return False
   # The full-row scan can consume a material part of the freshness budget.
   # Recheck the actual clock and identities after it, never its cached start.
   if (not 0<=(_time(self.now())-_time(s['started_at'])).total_seconds()<=90
       or s.get('current_day')!=_time(self.now()).strftime('%Y%m%d')
       or session_identity(session)!=self._completed_identity or _stream(session)!=self._completed_stream):return False
  except Exception:return False
  self.blocked=False;return True
 def tick(self,session,tracker,state_dir):
  self.blocked=True;now=_time(self.now())
  try:
   remember_cutover_binding(self,state_dir);marker=_marker(state_dir,now)
  except Exception:
   self._status={'schema':PHASE_SCHEMA,'status':'failed','reason':'maintenance_marker_scope_or_lease_invalid','orders_sent':0,'settlements_written':False}
   self._publish();return self.status()
  if (self.next_at and now<self.next_at and marker==self._marker_identity
      and self._status.get('current_day')==now.strftime('%Y%m%d')):return self.status()
  self._marker_identity=marker
  try:stream=_stream(session)
  except Exception:
   self._status={'schema':PHASE_SCHEMA,'status':'waiting_stream','marker_sha256':marker['sha256'],'marker_nonce':marker['marker_id'],
    'orders_sent':0,'settlements_written':False};self._publish();return self.status()
  started=now;mono=self.monotonic();phase=uuid.uuid4().hex;day=started.strftime('%Y%m%d');identity=session_identity(session)
  prior_days=marker['prior_order_days'];history={old:[] for old in prior_days}
  captured={a:[] for a in ('kt00009','kt00001','kt00018','ka10075')};directory=Path(state_dir)/'maintenance-readonly-evidence'/phase
  self._status={'schema':PHASE_SCHEMA,'status':'running','phase_id':phase,'marker_sha256':marker['sha256'],'marker_nonce':marker['marker_id'],
   'marker_identity':marker,'receiver_pid':os.getpid(),'owner_process_pid':marker['owner_process_pid'],
   'maintenance_module_sha256':module_sha256(),'started_at':started.isoformat(),'completed_at':None,
   'current_day':day,'prior_order_days':prior_days,'history_queries':{},
   'session_sha256':_sha(list(identity)),'current_session_matches':False,'stream_before':list(stream[1:]),
   'queries':{},'orders_sent':0,'settlements_written':False,'intents_created':0,'ttl_cancellations_sent':0}
  if 'cutover_lock'in marker:self._status['cutover_lock']=marker['cutover_lock']
  self._publish()
  try:
   gate=getattr(session,'paper_automation_reconcile',None)
   if identity[0]!='paper' or getattr(gate,'_session',None)is not session or getattr(gate,'_tracker',None)is not tracker:raise MaintenanceReadUnavailable('maintenance_account_scope_unverified')
   initial=_watermark(tracker)
   def guard():
    current=_time(self.now());elapsed=self.monotonic()-mono
    if (type(elapsed) not in (int,float) or not math.isfinite(elapsed) or not 0<=elapsed<=60
        or not 0<=(current-started).total_seconds()<=60 or current.date()!=started.date()
        or _marker(state_dir,current)!=marker or session_identity(session)!=identity or _stream(session)!=stream
        or getattr(session,'paper_automation_reconcile',None)is not gate):raise MaintenanceReadUnavailable('maintenance_batch_changed_or_stale')
   def observe(event):
    guard();api=event.get('source_api')
    if api=='kt00007' and event.get('day') in history:destination=history[event['day']]
    elif api in captured:destination=captured[api]
    else:raise MaintenanceReadUnavailable('maintenance_query_api_unverified')
    if api in ('kt00007','kt00009','ka10075'):tracker.record_broker_observation(event)
    destination.append(event)
    if len(destination)>20:raise MaintenanceReadUnavailable('maintenance_page_bound')
    _write_new(directory/(api+'-'+str(len(destination)).zfill(2)+'.json'),event)
    self._status['current_query_api']=api;self._status['current_query_day']=event.get('day')
    self._status['captured_page_count']=sum(map(len,captured.values()))+sum(map(len,history.values()));self._publish()
   for old in prior_days:
    guard();self.history(session,observer=observe,day=old,before_send=guard);guard()
    self._validate_history_pages(history[old],old,started,_time(self.now()))
   guard();self.orders(session,observer=observe,day=day,before_send=guard);guard()
   account_started=_time(self.now());report=self.account(session,before_send=guard,observer=observe,day=day);guard();account_at=_time(self.now())
   snapshot=_paper_balance_snapshot(report)
   if snapshot is None:raise MaintenanceReadUnavailable('maintenance_account_partial_or_invalid')
   # Only the complete account observation projection is permitted here.
   # No _ingest, _holdings, TTL/expired settlement or publication is called.
   tracker._save_paper_balance_snapshot('paper',report,account_at)
   balance=_balance(tracker,day);snapshot_raw=json.dumps(snapshot,ensure_ascii=False,sort_keys=True,separators=(',',':'))
   if (balance is None or balance['observed_at']!=account_at.isoformat() or balance['source_kind']!='broker_observation'
       or balance['payload_json']!=snapshot_raw):raise MaintenanceReadUnavailable('maintenance_account_observation_not_saved')
   self.pending(session,observer=observe,day=day,before_send=guard);guard();completed=_time(self.now())
   requests={'kt00009':{'ord_dt':day,'stk_bond_tp':'1','mrkt_tp':'0','sell_tp':'0','qry_tp':'0','stk_cd':'','fr_ord_no':'','dmst_stex_tp':'KRX'},
    'ka10075':{'all_stk_tp':'0','trde_tp':'0','stex_tp':'0'}}
   for api,events in captured.items():
    if api in requests:_complete_pages(events,api,requests[api],completed,started)
    else:self._validate_account_pages(events,api,day,started,completed)
    self._status['queries'][api]={'query_id':events[0]['query_id'],'request_started_at':events[0]['request_started_at'],
     'observed_at':events[-1]['observed_at'],'pages':len(events),'page_sha256':[_sha(e) for e in events],
     'response_wire_sha256':[e['response_wire_sha256'] for e in events] if api in ('kt00001','kt00018') else None}
   query_ids=[v['query_id'] for v in self._status['queries'].values()]
   for old,events in history.items():
    self._validate_history_pages(events,old,started,completed)
    if _time(events[-1]['observed_at'])>_time(captured['kt00009'][0]['request_started_at']):raise MaintenanceReadUnavailable('maintenance_history_query_order_invalid')
    query_ids.append(events[0]['query_id'])
    self._status['history_queries'][old]={'source_api':'kt00007','day':old,'query_id':events[0]['query_id'],
     'request_body':_history_request(old),'request_started_at':events[0]['request_started_at'],'observed_at':events[-1]['observed_at'],
     'pages':len(events),'page_sha256':[_sha(e) for e in events],
     'current_day':day,'current_orders_query_id':self._status['queries']['kt00009']['query_id'],
     'current_pending_query_id':self._status['queries']['ka10075']['query_id']}
   if len(set(query_ids))!=len(query_ids):raise MaintenanceReadUnavailable('maintenance_query_identity_reused')
   if _watermark(tracker)!=initial:raise MaintenanceReadUnavailable('maintenance_financial_watermark_changed')
   guard();completed=_time(self.now())
   self._status.update(status='complete',completed_at=completed.isoformat(),current_session_matches=True,
    stream_after=list(_stream(session)[1:]),financial_watermark=initial,
    account={'request_started_at':account_started.isoformat(),'observed_at':account_at.isoformat(),
     'account_snapshot_sha256':hashlib.sha256(snapshot_raw.encode()).hexdigest(),'pages':snapshot['pages'],
     'raw_wire_body_retained':False,'account_page_representation':'filtered_typed_page_with_original_wire_hash'})
   _write_new(directory/'complete.json',self._status);self._status['evidence_sha256']=_sha(self._status)
   self._completed_identity=identity;self._completed_stream=_stream(session);self.next_at=completed+timedelta(seconds=30)
  except Exception as error:
   reason=str(error) if isinstance(error,MaintenanceReadUnavailable) else 'maintenance_read_batch_unverified'
   self._status.update(status='failed',reason=reason,failed_at=_time(self.now()).isoformat(),current_session_matches=False)
   try:_write_new(directory/'failed.json',self._status)
   except Exception:pass
   self.next_at=_time(self.now())+timedelta(seconds=30)
  self._publish();return self.status()
 @staticmethod
 def _validate_history_pages(events,day,started,completed):
  try:
   dated=datetime.strptime(day,'%Y%m%d').date();start=_time(started);end=_time(completed)
   valid=(isinstance(day,str) and dated.strftime('%Y%m%d')==day and start<=end
    and start.date()==end.date() and 0<(end.date()-dated).days<=31)
  except (ValueError,TypeError):valid=False
  if not valid:raise MaintenanceReadUnavailable('maintenance_history_day_invalid')
  if not events or len(events)>20:raise MaintenanceReadUnavailable('maintenance_history_pages_incomplete')
  ids={e.get('query_id') for e in events}
  if len(ids)!=1 or re.fullmatch('[0-9a-f]{32}',str(next(iter(ids))))is None:raise MaintenanceReadUnavailable('maintenance_history_query_identity_invalid')
  rows=[];previous=started
  for n,e in enumerate(events,1):
   c=e.get('continuation',{});last=n==len(events);body=e.get('response_body',{})
   if (e.get('schema')!='kiwoom-broker-page-v1' or e.get('source_kind')!='broker_observation'
       or e.get('source_api')!='kt00007' or e.get('mode')!='paper' or e.get('day')!=day
       or e.get('status')!='success' or e.get('page')!=n or e.get('request_body')!=_history_request(day)
       or not previous<=_time(e.get('request_started_at'))<=_time(e.get('observed_at'))<=completed
       or c.get('request_cont_yn')!=('N'if n==1 else'Y')
       or c.get('response_cont_yn')not in ((None,'','N')if last else('Y',)) or c.get('next_key_present')is not(not last)
       or body.get('return_code')not in (0,'0') or type(body.get('return_code'))is bool
       or not isinstance(body.get('acnt_ord_cntr_prps_dtl'),list)):raise MaintenanceReadUnavailable('maintenance_history_pages_unverified')
   rows.extend(body['acnt_ord_cntr_prps_dtl']);previous=_time(e['observed_at'])
  if len(rows)>20000 or any(not isinstance(row,dict) for row in rows):raise MaintenanceReadUnavailable('maintenance_history_rows_invalid')
  return rows
 @staticmethod
 def _validate_account_pages(events,api,day,started,completed):
  request=(kt00001 if api=='kt00001' else kt00018).request_body()
  if not events or len(events)>20 or len({e.get('query_id') for e in events})!=1:raise MaintenanceReadUnavailable('maintenance_account_pages_incomplete')
  for n,e in enumerate(events,1):
   c=e.get('continuation',{});last=n==len(events)
   if (e.get('schema')!='paper-account-report-page-v1' or e.get('source_kind')!='broker_observation'
       or e.get('source_api')!=api or e.get('day')!=day or e.get('mode')!='paper' or e.get('page')!=n
       or re.fullmatch('[0-9a-f]{32}',str(e.get('query_id','')))is None or e.get('request_body')!=request
       or not started<=_time(e['request_started_at'])<=_time(e['observed_at'])<=completed
       or re.fullmatch('[0-9a-f]{64}',str(e.get('response_wire_sha256','')))is None
       or e.get('typed_body_sha256')!=_sha(e.get('response_body'))
       or c.get('request_cont_yn')!=('N'if n==1 else'Y') or c.get('response_cont_yn')not in ((None,'','N')if last else('Y',))
       or c.get('next_key_present')is not(not last)):raise MaintenanceReadUnavailable('maintenance_account_pages_unverified')
