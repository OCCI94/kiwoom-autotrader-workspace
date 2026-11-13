"""Offline timing of the expired-order certificate pass on a ledger CLONE.
No network (socket blocked), isolated HOME, never touches the live state dir."""
import sys, os, time, threading, json, sqlite3, socket
from datetime import datetime, timedelta, timezone
from pathlib import Path
def _blocked(*a, **k): raise RuntimeError('network blocked in harness')
socket.socket.connect = _blocked; socket.socket.connect_ex = _blocked; socket.create_connection = _blocked; socket.getaddrinfo = _blocked
H = Path(sys.argv[1]); sys.path.insert(0, str(H/'pkg'))
assert str(Path.home()).startswith(str(H)), Path.home()
from kiwoom_autotrader.trading import execution_log as el
from kiwoom_autotrader.trading import expired_order_certificates as cert
KST = timezone(timedelta(hours=9))
T0 = datetime(2026,11,10,8,45,17,tzinfo=KST); M0 = time.monotonic()
def clock(): return T0 + timedelta(seconds=time.monotonic()-M0)
def boom(*a, **k): raise RuntimeError('fetcher must not be called')
tr = el.ExecutionTracker(log_dir=H/'logs', now=clock, settings_loader=lambda: {},
        account_fetcher=boom, executions_fetcher=boom, profit_fetcher=boom)
assert str(tr.path).startswith(str(H))
out = {}
t=time.monotonic()
with tr._db() as db: pass
out['db_open_first_s']=round(time.monotonic()-t,3)
t=time.monotonic()
with tr._db() as db: pass
out['db_open_second_s']=round(time.monotonic()-t,3)
# instrument per-order checks
per=[]
orig_v=cert.verified_certificate; orig_t=tr._paper_prior_order_terminal
def v(tracker,db,order,now):
    s=time.monotonic(); r=orig_v(tracker,db,order,now); per.append(('verified',order['day'],order['ord_no'],r,round(time.monotonic()-s,3))); return r
def tt(db,order,now,**kw):
    s=time.monotonic(); r=orig_t(db,order,now,**kw); per.append(('terminal' if not kw else 'terminal(cert)',order['day'],order['ord_no'],r,round(time.monotonic()-s,3))); return r
cert.verified_certificate=v; tr._paper_prior_order_terminal=tt
M0=time.monotonic()
t=time.monotonic()
try:
    res=tr.record_paper_expired_settlements(tr._moment()); out['settle_result']=res
except Exception as e:
    out['settle_error']=f'{type(e).__name__}: {e}'
out['settle_txn_s']=round(time.monotonic()-t,3)
out['per_order']=per[:]; per.clear()
t=time.monotonic()
try:
    with tr._db() as db:
        rows=tr._pending_order_rows(db,'paper')
    out['pending_rows']=[(r['day'],r['ord_no'],r['symbol'],r['quantity'],r['filled_quantity']) for r in rows]
except Exception as e:
    out['pending_error']=f'{type(e).__name__}: {e}'
out['pending_rows_s']=round(time.monotonic()-t,3)
out['per_order_pending']=per[:]; per.clear()
# contention: writer thread holds BEGIN IMMEDIATE; second connection tries to write
M0=time.monotonic(); hold={}
def writer():
    s=time.monotonic()
    try: tr.record_paper_expired_settlements(tr._moment())
    except Exception as e: hold['err']=f'{type(e).__name__}: {e}'
    hold['s']=round(time.monotonic()-s,3)
th=threading.Thread(target=writer); th.start(); time.sleep(0.5)
s=time.monotonic()
try:
    with tr._db() as db:
        db.execute('BEGIN IMMEDIATE'); db.execute('ROLLBACK')
    out['second_writer']='acquired'
except Exception as e:
    out['second_writer']=f'{type(e).__module__}.{type(e).__name__}: {e}'
out['second_writer_wait_s']=round(time.monotonic()-s,3)
th.join(); out['holder']=hold
print(json.dumps(out,ensure_ascii=False,indent=1,default=str))
