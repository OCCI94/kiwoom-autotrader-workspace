"""Offline: (A) time the closed-day fingerprint, (B) hold a write lock from a second connection
while the tracker's 20261104 'executions' task runs, and time the failure handler.
Ledger CLONE only, network blocked, isolated HOME."""
import sys, os, time, threading, json, sqlite3, socket, uuid, traceback, faulthandler, types, functools
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
def _blocked(*a, **k): raise RuntimeError('network blocked in harness')
socket.socket.connect = _blocked; socket.socket.connect_ex = _blocked; socket.create_connection = _blocked; socket.getaddrinfo = _blocked
H = Path(sys.argv[1]); DAY = sys.argv[2]; HOLD = float(sys.argv[3]); BUDGET = int(sys.argv[4])
sys.path.insert(0, str(H/'pkg'))
assert str(Path.home()).startswith(str(H)), Path.home()
from kiwoom_autotrader.trading import execution_log as el
from kiwoom_autotrader.api import executions as ex
KST = timezone(timedelta(hours=9))
T0 = datetime(2026,11,10,8,45,17,tzinfo=KST); M0 = time.monotonic()
def clock(): return T0 + timedelta(seconds=time.monotonic()-M0)
def boom(*a, **k): raise RuntimeError('fetcher must not be called')
LOG = open(H/f'replay-lock-{DAY}.log', 'w')
def log(*a): print(f'[{time.monotonic()-M0:8.3f}]', *a, file=LOG, flush=True)
def _deadline():
    log('HARD DEADLINE reached; dumping stacks and exiting'); faulthandler.dump_traceback(file=LOG); LOG.flush(); os._exit(3)
threading.Timer(BUDGET, _deadline).start()
DB = str(H/'logs/.execution-ledger.sqlite3')
ro = sqlite3.connect(f'file:{DB}?mode=ro', uri=True, timeout=5)
row = ro.execute("select id,payload_json from broker_observations where day=? and source_api='kt00009' and scope='default' order by id desc limit 1", (DAY,)).fetchone()
ro.close(); stored = json.loads(row[1])
TOKEN = 'HARNESS-NOT-A-REAL-TOKEN-0000000000'
session = types.SimpleNamespace(token=TOKEN, mode='paper', authenticated=True)
el.session_identity = lambda s: ('paper', TOKEN)
def fake_request_rows(session, api_id, body, field, *, observer=None, day=None, before_send=None):
    event = dict(stored); event['query_id'] = uuid.uuid4().hex
    event['request_started_at'] = datetime.now(KST).isoformat(); event['observed_at'] = datetime.now(KST).isoformat()
    log('fake fetch done; notifying observer')
    ex._notify_observer(observer, event)
    return list(stored['response_body'][field])
ex._request_rows = fake_request_rows
def traced(name, fn):
    @functools.wraps(fn)
    def inner(*a, **k):
        s = time.monotonic(); log('>>', name)
        try: r = fn(*a, **k)
        except BaseException as e:
            log('!!', name, 'raised after', round(time.monotonic()-s, 3), 's:', type(e).__name__, str(e)[:200]); raise
        log('<<', name, round(time.monotonic()-s, 3), 's'); return r
    return inner
real_persist = el.persist_observation
def persist(*a, **k):
    s = time.monotonic()
    try: return real_persist(*a, **k)
    except BaseException as e:
        log('!! persist_observation UNDERLYING:', type(e).__name__, str(e)[:200], 'after', round(time.monotonic()-s, 3), 's'); raise
el.persist_observation = persist
tr = el.ExecutionTracker(log_dir=H/'logs', now=clock, settings_loader=lambda: {}, account_fetcher=boom, profit_fetcher=boom)
assert str(tr.path).startswith(str(H))
for name in ('record_broker_observation', '_ingest', '_closed_failed', '_closed_economic_fingerprint', '_closed_observation_facts', '_broker_observations_snapshot'):
    if hasattr(tr, name): setattr(tr, name, traced(name, getattr(tr, name)))
    else: log('no attr', name)
# ---- Phase A: fingerprint cost for the closed day (cold, then warm, then after one new observation row)
for label in ('cold', 'warm'):
    s = time.monotonic()
    with tr._db() as db:
        fp, unresolved = tr._closed_economic_fingerprint(db, 'paper', DAY)
    log(f'PHASE A fingerprint {label}: {time.monotonic()-s:.3f} s unresolved={unresolved} fp_none={fp is None}')
s = time.monotonic()
with tr._db() as db: due = tr._closed_due(db, 'paper', DAY, clock())
log(f'PHASE A _closed_due: {time.monotonic()-s:.3f} s due={due} attempts={dict(tr._closed_attempts)}')
# ---- Phase B: writer holds the ledger; tick's observation write must wait
ready = threading.Event()
def holder():
    c = sqlite3.connect(DB, timeout=30, isolation_level=None)
    c.execute('BEGIN IMMEDIATE'); log('holder: BEGIN IMMEDIATE acquired; holding', HOLD, 's'); ready.set()
    time.sleep(HOLD); c.execute('ROLLBACK'); c.close(); log('holder: released')
threading.Thread(target=holder, daemon=True).start(); ready.wait(10)
alerts = []
tr._identity = ('paper', TOKEN); tr._next_at = None
tr._tasks = deque([('executions', DAY, None)]); tr._cycle_days = [DAY]; tr._cycle_started_at = clock()
s = time.monotonic(); log('=== PHASE B tick start; attempts', dict(tr._closed_attempts))
try: tr.tick(session, lambda text: (alerts.append(text), log('ALERT after', round(time.monotonic()-s, 3), 's:', text)))
except BaseException as e: log('tick raised', type(e).__name__, e); log(traceback.format_exc())
log('=== PHASE B tick end', round(time.monotonic()-s, 3), 's; last_error=', repr(tr._last_error), 'recon=', {k: {kk: (vv if kk != 'fingerprint' else (vv is not None)) for kk, vv in v.items()} for k, v in tr._closed_reconciliation.items()})
# ---- Phase C: a later successful task clears _last_error -> same text re-alerts next cycle?
time.sleep(max(0, HOLD - (time.monotonic()-s)) + 0.5)
tr._next_at = None; tr._tasks = deque([('executions', DAY, None)])
s = time.monotonic(); log('=== PHASE C tick (no lock) start')
tr.tick(session, lambda text: (alerts.append(text), log('ALERT:', text)))
log('=== PHASE C tick end', round(time.monotonic()-s, 3), 's; last_error=', repr(tr._last_error))
log('DONE alerts', len(alerts)); LOG.flush(); os._exit(0)
