"""Explicit second-stage prior-day expiry, never a cancellation or a new fill.

The receiver owns the existing session. This module issues no broker requests.
Only an already-created immutable expiry-certificate table may be appended;
all order/assignment/00/account evidence is checked before and after that append.
"""
from pathlib import Path
import hashlib,json,re,sqlite3,stat
from datetime import datetime,timedelta
from . import maintenance_reconciliation as m
from .broker_observation_repeats import install_evidence_view,evidence_table
from .cancellation_settlement import _snapshot_query_pages,_complete_pages,_stream
from .expired_order_certificates import TABLE,capture_certificates,verified_certificate
from kiwoom_autotrader.api.minute_chart import session_identity
from kiwoom_autotrader.api.session import KST
from kiwoom_autotrader.utils.platform_io import os

SCHEMA='paper-prior-expiry-resolution-v1'
AUTH_SCHEMA='paper-prior-expiry-resolution-authorization-v1'
INCIDENT_DAY='20261103'
ORDER_NUMBERS=('219443','219461','219473','219494','219709','219731','219860','219872','219880')
CORE=('intents','orders','fills','order_decisions')
ASSIGNMENTS=('assignments','attempts','outcomes','selections')

class ExpiryResolutionUnavailable(ValueError):pass
def fail(reason):raise ExpiryResolutionUnavailable(reason)
def module_sha256():return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
def _read(path,maximum=1024*1024):
 path=Path(path)
 if any(p.is_symlink() for p in (path,*path.parents)):fail('expiry_path_symlink')
 fd=os.open(path,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0))
 try:
  before=os.fstat(fd)
  uid=getattr(os,'getuid',None)
  if not callable(uid) or not stat.S_ISREG(before.st_mode) or before.st_uid!=uid() or before.st_mode&0o077 or before.st_size>maximum:fail('expiry_file_unverified')
  raw=os.read(fd,maximum+1);after=os.fstat(fd)
  if len(raw)!=before.st_size or any(getattr(before,k)!=getattr(after,k) for k in ('st_dev','st_ino','st_mtime_ns','st_size')):fail('expiry_file_changed')
  return json.loads(raw),hashlib.sha256(raw).hexdigest()
 finally:os.close(fd)
def _db(path,write=False):
 path=Path(path)
 if any(p.is_symlink() for p in (path,*path.parents)) or not path.is_file():fail('expiry_database_missing')
 db=sqlite3.connect(path.resolve().as_uri()+('?mode=rw' if write else '?mode=ro'),uri=True,timeout=2)
 db.row_factory=sqlite3.Row;install_evidence_view(db)
 if not write:db.execute('PRAGMA query_only=ON')
 db.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
 return db
def _tables(db,names):
 result={}
 for name in names:
  cols=[r[1] for r in db.execute('PRAGMA table_info('+name+')')]
  if not cols:fail('expiry_required_schema_missing')
  h=hashlib.sha256();count=0
  for row in db.execute('SELECT * FROM '+name+' ORDER BY '+','.join(str(i+1) for i in range(len(cols)))):
   count+=1
   if count>100000:fail('expiry_rows_bound')
   h.update(m._json([{'blob_sha256':hashlib.sha256(v).hexdigest()} if isinstance(v,bytes) else v for v in row]).encode()+b'\n')
  result[name]={'columns':cols,'rows':count,'sha256':h.hexdigest()}
 return result
def _assignments(state_dir):
 path=Path(state_dir)/'paper-experiment.sqlite3';db=_db(path)
 try:return _tables(db,ASSIGNMENTS)
 finally:db.close()
def _write_authorizer(action,arg1,arg2,database,source):
 if action==sqlite3.SQLITE_INSERT:return sqlite3.SQLITE_OK if arg1==TABLE else sqlite3.SQLITE_DENY
 if action in (sqlite3.SQLITE_UPDATE,sqlite3.SQLITE_DELETE,sqlite3.SQLITE_CREATE_TABLE,sqlite3.SQLITE_DROP_TABLE,
   sqlite3.SQLITE_ALTER_TABLE,sqlite3.SQLITE_CREATE_TRIGGER,sqlite3.SQLITE_DROP_TRIGGER,sqlite3.SQLITE_ATTACH,
   sqlite3.SQLITE_DETACH,sqlite3.SQLITE_CREATE_INDEX,sqlite3.SQLITE_DROP_INDEX):return sqlite3.SQLITE_DENY
 return sqlite3.SQLITE_OK

class MaintenanceExpiryResolution:
 def __init__(self,*,now=None):
  self.now=now or(lambda:datetime.now(KST));self.blocked=False;self._saved=None;self._stage2_seen=False
  self._status={'schema':SCHEMA,'status':'inactive','orders_sent':0,'resolution_type':'expired','cancellation_publications_written':0}
 def requested(self,state_dir):
  try:
   if 'prior_expiry_resolution' in _read(Path(state_dir)/'maintenance-quiet.lock',4096)[0]:self._stage2_seen=True
  except (OSError,ValueError,TypeError):pass
  return self._stage2_seen
 def latch(self,state_dir):
  try:m.remember_cutover_binding(self,state_dir)
  except m.MaintenanceReadUnavailable:self.blocked=True
  try:
   marker=_read(Path(state_dir)/'maintenance-quiet.lock',4096)[0]
   if marker.get('prior_order_days') or 'prior_expiry_resolution' in marker:self.blocked=True
   if 'prior_expiry_resolution' in marker:self._stage2_seen=True
  except (OSError,ValueError,TypeError):pass
  return self.blocked
 def assert_orders_allowed(self):
  if self.blocked:fail('maintenance_prior_expiry_orders_blocked')
 def status(self):return json.loads(m._json(self._status))
 def _clock(self,phase):
  now=m._time(self.now());started=m._time(phase['started_at']);completed=m._time(phase['completed_at'])
  if (phase.get('current_day')!=now.strftime('%Y%m%d') or not started.date()==completed.date()==now.date() or not started<=completed<=now
      or not 0<=(now-started).total_seconds()<=90):fail('expiry_phase_stale_or_cross_day')
  return now
 def _phase(self,state_dir,phase):
  phase=phase.status() if callable(getattr(phase,'status',None)) else phase
  if not isinstance(phase,dict) or phase.get('status')!='complete' or phase.get('prior_order_days')!=[INCIDENT_DAY]:fail('expiry_readonly_phase_unverified')
  if not re.fullmatch('[a-f0-9]{32}',str(phase.get('phase_id',''))):fail('expiry_phase_identity_invalid')
  body,unused=_read(Path(state_dir)/'maintenance-readonly-evidence'/phase['phase_id']/'complete.json')
  expected={k:v for k,v in phase.items() if k!='evidence_sha256'}
  if body!=expected or m._sha(body)!=phase.get('evidence_sha256'):fail('expiry_phase_hash_mismatch')
  if phase.get('orders_sent')!=0 or phase.get('settlements_written') is not False or phase.get('intents_created')!=0 or phase.get('ttl_cancellations_sent')!=0:fail('expiry_phase_not_readonly')
  self._clock(phase);return phase
 def _authorization(self,state_dir,phase):
  identity=m._marker(state_dir,m._time(self.now()))
  marker,sha=_read(Path(state_dir)/'maintenance-quiet.lock',4096)
  if sha!=identity['sha256']:fail('expiry_marker_changed')
  auth=marker.get('prior_expiry_resolution')
  expected={'schema':AUTH_SCHEMA,'readonly_phase_id':phase['phase_id'],'readonly_phase_sha256':phase['evidence_sha256'],
   'parent_marker_sha256':phase['marker_sha256'],'orders':[{'day':INCIDENT_DAY,'ord_no':n} for n in ORDER_NUMBERS],
   'expiry_module_sha256':module_sha256()}
  if (auth!=expected or marker.get('current_day')!=phase['current_day'] or marker.get('prior_order_days')!=[INCIDENT_DAY]
      or identity['sha256']==phase['marker_sha256'] or identity['marker_id']==phase.get('marker_nonce')
      or m._sha(identity.get('cutover_lock'))!=m._sha(phase.get('cutover_lock'))):fail('expiry_explicit_authorization_unverified')
  return identity
 def _identity(self,session,tracker,phase):
  identity=session_identity(session);stream=_stream(session)
  gate=getattr(session,'paper_automation_reconcile',None)
  if (identity[0]!='paper' or m._sha(list(identity))!=phase.get('session_sha256') or phase.get('current_session_matches') is not True
      or list(stream[1:])!=phase.get('stream_after') or phase.get('stream_before')!=phase.get('stream_after')
      or getattr(gate,'_session',None)is not session or getattr(gate,'_tracker',None)is not tracker):fail('expiry_session_or_stream_changed')
  return identity,stream
 def _snapshot(self,db,tracker,phase,state_dir):
  now=self._clock(phase);core=_tables(db,CORE)
  if m._sha(core)!=phase['financial_watermark']:fail('expiry_financial_watermark_changed')
  table=evidence_table(db);h=hashlib.sha256();count=0;chain=set(ORDER_NUMBERS);other_day_values=[]
  for row in db.execute(f"SELECT id,day,observed_at,payload_json,digest FROM {table} WHERE scope=? AND mode='paper' AND source_api='00' ORDER BY id",(tracker.scope,)):
   count+=1
   if count>100000:fail('expiry_stream_rows_bound')
   raw=row['payload_json']
   if hashlib.sha256(raw.encode()).hexdigest()!=row['digest']:fail('expiry_stream_hash_invalid')
   event=json.loads(raw)
   if (event.get('day')!=row['day'] or event.get('observed_at')!=row['observed_at'] or event.get('mode')!='paper'
       or event.get('source_api')!='00' or event.get('source_kind')!='broker_observation'):fail('expiry_stream_identity_invalid')
   if m._time(event['observed_at'])>now:fail('expiry_stream_future_observation')
   values=event.get('values',{})
   if isinstance(values,dict):
    if row['day']>=phase['current_day'] or m._time(event['observed_at']).date()>=now.date():other_day_values.append((values,('9203','904')))
    if row['day']==INCIDENT_DAY and str(values.get('904','')).isdigit() and str(int(values['904'])) in ORDER_NUMBERS:
     child=values.get('9203')
     if not isinstance(child,str) or not child.isdigit():fail('expiry_child_identity_invalid')
     chain.add(str(int(child)))
   h.update(m._json(list(row)).encode()+b'\n')
  history=phase.get('history_queries',{}).get(INCIDENT_DAY)
  if not isinstance(history,dict):fail('expiry_history_missing')
  request=m._history_request(INCIDENT_DAY)
  rows=_snapshot_query_pages(db,tracker.scope,INCIDENT_DAY,'kt00007',request);events=[r[2] for r in rows]
  m.MaintenanceReadOnlyReconciliation._validate_history_pages(events,INCIDENT_DAY,m._time(phase['started_at']),now)
  self._query_identity(events,history)
  current_request={'ord_dt':phase['current_day'],'stk_bond_tp':'1','mrkt_tp':'0','sell_tp':'0','qry_tp':'0','stk_cd':'','fr_ord_no':'','dmst_stex_tp':'KRX'}
  current_rows=_snapshot_query_pages(db,tracker.scope,phase['current_day'],'kt00009',current_request)
  current_events=[r[2] for r in current_rows]
  _complete_pages(current_events,'kt00009',current_request,now,m._time(phase['started_at']))
  self._query_identity(current_events,phase['queries']['kt00009'])
  rest_count=0
  for row in db.execute(f"SELECT payload_json,digest FROM {table} WHERE scope=? AND mode='paper' AND day=? AND source_api='kt00009' ORDER BY id",(tracker.scope,phase['current_day'])):
   rest_count+=1
   if rest_count>20000:fail('expiry_current_detail_bound')
   if hashlib.sha256(row['payload_json'].encode()).hexdigest()!=row['digest']:fail('expiry_current_detail_hash_invalid')
   e=json.loads(row['payload_json'])
   if e.get('day')!=phase['current_day'] or e.get('source_api')!='kt00009' or e.get('mode')!='paper' or e.get('source_kind')!='broker_observation':fail('expiry_current_detail_invalid')
   if e.get('status')!='success':continue
   values=e.get('response_body',{}).get('acnt_ord_cntr_prst_array')
   if not isinstance(values,list):fail('expiry_current_detail_invalid')
   for v in values:
    if not isinstance(v,dict):fail('expiry_current_detail_invalid')
    other_day_values.append((v,('ord_no','orig_ord_no')))
  for values,keys in other_day_values:
   for key in keys:
    value=values.get(key)
    if isinstance(value,str) and value.isdigit() and str(int(value)) in chain:fail('expiry_cross_day_order_identity_ambiguous')
  pending_request={'all_stk_tp':'0','trde_tp':'0','stex_tp':'0'}
  rows=_snapshot_query_pages(db,tracker.scope,phase['current_day'],'ka10075',pending_request);pending=[r[2] for r in rows]
  _complete_pages(pending,'ka10075',pending_request,now,m._time(phase['started_at']))
  self._query_identity(pending,phase['queries']['ka10075'])
  if any(e['response_body']['oso']!=[] for e in pending):fail('expiry_broker_pending_not_empty')
  if m._time(pending[0]['request_started_at'])<m._time(events[-1]['observed_at']):fail('expiry_query_order_invalid')
  if history.get('current_pending_query_id')!=pending[0]['query_id'] or history.get('current_orders_query_id')!=phase['queries']['kt00009']['query_id']:fail('expiry_history_phase_binding_invalid')
  balance=db.execute("SELECT observed_at,source_kind,payload_json FROM account_balance_daily WHERE scope=? AND mode='paper' AND day=?",(tracker.scope,phase['current_day'])).fetchone()
  account=phase['account']
  if (balance is None or balance['source_kind']!='broker_observation' or balance['observed_at']!=account['observed_at']
      or hashlib.sha256(balance['payload_json'].encode()).hexdigest()!=account['account_snapshot_sha256']
      or not m._time(events[-1]['observed_at'])<=m._time(balance['observed_at'])<=m._time(pending[0]['request_started_at'])
      or not 0<=(now-m._time(balance['observed_at'])).total_seconds()<=90):fail('expiry_current_account_unverified')
  orders=[];raw_pending=[]
  for row in db.execute("SELECT o.*,(SELECT COALESCE(SUM(quantity),0) FROM fills f WHERE f.scope=o.scope AND f.day=o.day AND f.mode=o.mode AND f.ord_no=o.ord_no) filled_quantity FROM orders o WHERE scope=? AND mode='paper' ORDER BY day,ord_no",(tracker.scope,)):
   order=dict(row)
   if order['ord_no'] in ORDER_NUMBERS and order['day']>=phase['current_day']:fail('expiry_reused_registered_order_number')
   if order['quantity']!=order['filled_quantity']:
    cancelled=tracker._confirmed_cancelled_quantity(db,order)
    if order['ord_no'] in ORDER_NUMBERS and order['day']==INCIDENT_DAY:
     if (order['side']!='buy' or order['strategy']!='EXP_D' or type(order['quantity'])is not int or order['quantity']<=0
         or order['filled_quantity']!=0 or cancelled!=0):fail('expiry_incident_order_mismatch')
     intent=db.execute("SELECT * FROM intents WHERE scope=? AND day=? AND mode='paper' AND order_number=?",(tracker.scope,INCIDENT_DAY,order['ord_no'])).fetchall()
     if len(intent)!=1 or any(intent[0][k]!=order[k] for k in ('symbol','quantity','side','strategy')) or intent[0]['state']!='accepted':fail('expiry_incident_receipt_mismatch')
     orders.append(order);raw_pending.append((order['day'],order['ord_no']))
    elif order['quantity']-cancelled!=order['filled_quantity'] and not verified_certificate(tracker,db,order,now):raw_pending.append((order['day'],order['ord_no']))
  expected=[(INCIDENT_DAY,n) for n in ORDER_NUMBERS]
  if sorted(raw_pending)!=expected or sorted(o['ord_no'] for o in orders)!=list(ORDER_NUMBERS):fail('expiry_original_pending_set_mismatch')
  unresolved=db.execute("SELECT 1 FROM intents i WHERE scope=? AND mode='paper' AND state!='cancelled' AND (state!='accepted' OR order_number IS NULL OR NOT EXISTS(SELECT 1 FROM orders o WHERE o.scope=i.scope AND o.day=i.day AND o.mode=i.mode AND o.ord_no=i.order_number)) LIMIT 1",(tracker.scope,)).fetchone()
  if unresolved:fail('expiry_unresolved_intent')
  for order in orders:
   if not tracker._paper_prior_order_terminal(db,order,now):fail('expiry_terminal_quantity_unverified')
  return {'core':core,'stream':{'rows':count,'sha256':h.hexdigest()},'assignments':_assignments(state_dir),
   'account':dict(account),'queries':{'history':history,'pending':phase['queries']['ka10075']}},orders
 @staticmethod
 def _query_identity(events,proof):
  if (proof.get('query_id')!=events[0]['query_id'] or proof.get('pages')!=len(events)
      or proof.get('page_sha256')!=[m._sha(e) for e in events]
      or proof.get('request_started_at')!=events[0]['request_started_at'] or proof.get('observed_at')!=events[-1]['observed_at']):fail('expiry_latest_query_changed')
 def _certificates(self,db,tracker,orders):
  results=[]
  for order in orders:
   now=m._time(self.now());row=db.execute(f'SELECT * FROM {TABLE} WHERE scope=? AND day=? AND ord_no=? ORDER BY id DESC LIMIT 1',(tracker.scope,INCIDENT_DAY,order['ord_no'])).fetchone()
   if row is None or not verified_certificate(tracker,db,order,now):fail('expiry_certificate_unverified')
   expired=tracker._confirmed_expired_quantity(db,order,now);cancelled=tracker._confirmed_cancelled_quantity(db,order)
   if expired!=order['quantity'] or cancelled!=0:fail('expiry_resolution_quantity_mismatch')
   results.append({'day':INCIDENT_DAY,'ord_no':order['ord_no'],'symbol':order['symbol'],'strategy':order['strategy'],
    'quantity':order['quantity'],'filled_quantity':0,'expired_quantity':expired,'cancelled_quantity':0,
    'certificate_id':row['id'],'certificate_sha256':row['digest'],'resolution_type':'expired'})
  if tracker._pending_order_rows(db,'paper'):fail('expiry_remaining_registered_pending')
  return results
 def tick(self,session,tracker,state_dir,readonly_phase):
  self.blocked=True;self._stage2_seen=True;db=None;appended=0;stage='phase'
  try:
   m.remember_cutover_binding(self,state_dir)
   phase=self._phase(state_dir,readonly_phase);stage='authorization';marker=self._authorization(state_dir,phase)
   identity,stream=self._identity(session,tracker,phase)
   db=_db(tracker.path,write=True);db.set_authorizer(_write_authorizer)
   stage='snapshot'
   before,orders=self._snapshot(db,tracker,phase,state_dir)
   if self._saved is not None:
    if marker!=self._saved['marker'] or before!=self._saved['snapshot']:fail('expiry_completed_evidence_changed')
    results=self._certificates(db,tracker,orders)
   else:
    # Validate all nine before one append. The same transaction excludes a
    # concurrent financial/00 write, and the queue remains blocked externally.
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(TABLE,)).fetchone():fail('expiry_certificate_schema_missing')
    for operation in ('update','delete'):
     name=TABLE+'_no_'+operation
     trigger=db.execute("SELECT sql FROM sqlite_master WHERE type='trigger' AND name=? AND tbl_name=?",(name,TABLE)).fetchone()
     expected=f"create trigger {name} before {operation} on {TABLE} begin select raise(abort,'terminal certificates are immutable'); end"
     sql=re.sub(r'\s+',' ',trigger[0].strip()).lower().replace('if not exists ','') if trigger else ''
     if sql.rstrip(';')!=expected:fail('expiry_certificate_immutability_missing')
    result=capture_certificates(tracker,db,self._clock(phase))
    if result['unconfirmed_orders'] or set(result['captured_orders'])-set(ORDER_NUMBERS):fail('expiry_unexpected_capture')
    appended=len(result['captured_orders'])
    results=self._certificates(db,tracker,orders)
   stage='final_snapshot';after,unused=self._snapshot(db,tracker,phase,state_dir)
   stage='final_authorization'
   if after!=before or self._authorization(state_dir,phase)!=marker or self._identity(session,tracker,phase)!=(identity,stream):fail('expiry_commit_evidence_changed')
   stage='commit';self._clock(phase);db.commit()
   self._saved={'phase':phase,'marker':marker,'identity':identity,'stream':stream,'snapshot':before,'state_dir':Path(state_dir)}
   self._status={'schema':SCHEMA,'status':'complete','resolution_type':'expired','phase_id':phase['phase_id'],
    'readonly_phase_id':phase['phase_id'],'readonly_phase_sha256':phase['evidence_sha256'],'marker_sha256':marker['sha256'],
    'marker_nonce':marker['marker_id'],'receiver_pid':os.getpid(),'session_sha256':phase['session_sha256'],
    'current_day':phase['current_day'],'prior_order_days':[INCIDENT_DAY],'observed_at':m._time(self.now()).isoformat(),
    'resolutions':results,'remaining_registered_pending':0,'principal_reservations_released':9,
    'verified_released_order_count':9,'expiry_certificates_appended_this_tick':appended,
    'orders_sent':0,'cancellation_publications_written':0,'core_watermark':m._sha(before['core']),
    'assignment_watermark':m._sha(before['assignments']),'all_00_watermark':before['stream'],'account':before['account'],
    'historical_financial_values_preserved':True,'owned_position_status':'requires_separate_current_ownership_verification'}
   if 'cutover_lock'in phase:self._status['cutover_lock']=phase['cutover_lock']
   self._status['evidence_sha256']=m._sha(self._status)
  except Exception as error:
   if db is not None:db.rollback()
   self._status={'schema':SCHEMA,'status':'failed','reason':str(error) if isinstance(error,ExpiryResolutionUnavailable) else 'expiry_resolution_unverified',
    'failure_stage':stage,'exception_type':type(error).__name__,
    'orders_sent':0,'cancellation_publications_written':0,'resolution_type':'expired','principal_reservations_released':None}
  finally:
   if db is not None:db.close()
  return self.status()
 def release_if_verified(self,session,tracker):
  if not self.blocked:return True
  if getattr(self,'_cutover_lock_error',False):return False
  if self._saved is None or self._status.get('status')!='complete':return False
  db=None
  try:
   saved=self._saved;state_dir=saved['state_dir'];phase=self._phase(state_dir,saved['phase'])
   marker_path=state_dir/'maintenance-quiet.lock'
   if marker_path.exists() or marker_path.is_symlink():return False
   if self._identity(session,tracker,phase)!=(saved['identity'],saved['stream']):return False
   db=_db(tracker.path);snapshot,orders=self._snapshot(db,tracker,phase,state_dir)
   if snapshot!=saved['snapshot'] or self._certificates(db,tracker,orders)!=self._status['resolutions']:return False
   self._clock(phase)
   if marker_path.exists() or marker_path.is_symlink() or self._identity(session,tracker,phase)!=(saved['identity'],saved['stream']):return False
   self.blocked=False;return True
  except Exception:return False
  finally:
   if db is not None:db.close()
