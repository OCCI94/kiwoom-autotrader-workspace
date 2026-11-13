"""Offline replay of the tracker's 'executions' task for 20261104 on a ledger CLONE.
No network (socket blocked), isolated HOME, never touches the live state dir."""
import sys, os, time, threading, json, sqlite3, socket, uuid, traceback, faulthandler, types, functools
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
def _blocked(*a, **k): raise RuntimeError('network blocked in harness')
socket.socket.connect = _blocked; socket.socket.connect_ex = _blocked; socket.create_connection = _blocked; socket.getaddrinfo = _blocked
H = Path(sys.argv[1]); DAY = sys.argv[2]; PASSES = int(sys.argv[3]); BUDGET = int(sys.argv[4])
sys.path.insert(0, str(H/'pkg'))
assert str(Path.home()).startswith(str(H)), Path.home()
from kiwoom_autotrader.trading import execution_log as el
from kiwoom_autotrader.api import executions as ex
KST = timezone(timedelta(hours=9))
T0 = datetime(2026,11,10,8,45,17,tzinfo=KST); M0 = time.monotonic()
def clock(): return T0 + timedelta(seconds=time.monotonic()-M0)
def boom(*a, **k): raise RuntimeError('fetcher must not be called')
LOG = open(H/f'replay-{DAY}.log', 'w')
def log(*a):
    print(f'[{time.monotonic()-M0:8.3f}]', *a, file=LOG, flush=True)
stacks = open(H/f'replay-{DAY}-stacks.txt', 'w')
faulthandler.dump_traceback_later(20, repeat=True, file=stacks)
def _deadline():
    log('HARD DEADLINE reached; dumping stacks and exiting'); faulthandler.dump_traceback(file=LOG); LOG.flush(); os._exit(3)
threading.Timer(BUDGET, _deadline).start()

ro = sqlite3.connect(f'file:{H}/logs/.execution-ledger.sqlite3?mode=ro', uri=True, timeout=5)
row = ro.execute("select id,payload_json from broker_observations where day=? and source_api='kt00009' and scope='default' order by id desc limit 1", (DAY,)).fetchone()
ro.close()
stored = json.loads(row[1]); log('stored original id', row[0], 'status', stored['status'], 'page', stored['page'], 'rows', len(stored['response_body'].get('acnt_ord_cntr_prst_array', [])), 'cont', stored['continuation'])

TOKEN = 'HARNESS-NOT-A-REAL-TOKEN-0000000000'
session = types.SimpleNamespace(token=TOKEN, mode='paper')
el.session_identity = lambda s: ('paper', TOKEN)
def fake_request_rows(session, api_id, body, field, *, observer=None, day=None, before_send=None):
    assert api_id == 'kt00009' and day == DAY, (api_id, day)
    event = dict(stored)
    assert event['request_body'] == dict(body), (event['request_body'], body)
    event['query_id'] = uuid.uuid4().hex
    event['request_started_at'] = datetime.now(KST).isoformat()
    event['observed_at'] = datetime.now(KST).isoformat()
    log('fake fetch done; notifying observer')
    ex._notify_observer(observer, event)
    return list(stored['response_body'][field])
ex._request_rows = fake_request_rows

def traced(name, fn):
    @functools.wraps(fn)
    def inner(*a, **k):
        s = time.monotonic(); log('>>', name)
        try:
            r = fn(*a, **k)
        except BaseException as e:
            log('!!', name, 'raised after', round(time.monotonic()-s, 3), 's:', type(e).__name__, str(e)[:300])
            log(traceback.format_exc())
            raise
        log('<<', name, round(time.monotonic()-s, 3), 's', ('-> '+repr(r)[:80]) if name == 'persist_observation' else '')
        return r
    return inner
el.persist_observation = traced('persist_observation', el.persist_observation)
tr = el.ExecutionTracker(log_dir=H/'logs', now=clock, settings_loader=lambda: {}, account_fetcher=boom, profit_fetcher=boom)
assert str(tr.path).startswith(str(H)) and tr._default_executions_fetcher
for name in ('record_broker_observation', '_materialize_history', '_materialize_accounting', '_materialize', '_ingest', '_closed_incomplete'):
    setattr(tr, name, traced(name, getattr(tr, name)))
calls = {'observe': 0}
orig_obs = tr._observe_execution
def obs(*a, **k):
    calls['observe'] += 1
    s = time.monotonic()
    try:
        return orig_obs(*a, **k)
    except BaseException as e:
        log('!! _observe_execution raised:', type(e).__name__, str(e)[:300]); log(traceback.format_exc()); raise
    finally:
        d = time.monotonic()-s
        if d > 1 or calls['observe'] <= 3: log('   _observe_execution call', calls['observe'], 'kind', a[2] if len(a) > 2 else None, round(d, 3), 's')
tr._observe_execution = obs
alerts = []
for n in range(PASSES):
    tr._identity = ('paper', TOKEN); tr._next_at = None
    tr._tasks = deque([('executions', DAY, None)]); tr._cycle_days = [DAY]; tr._cycle_started_at = clock()
    s = time.monotonic(); log(f'=== pass {n+1}: tick start, observe calls so far', calls['observe'])
    try:
        tr.tick(session, lambda text: (alerts.append(text), log('ALERT:', text)))
    except BaseException as e:
        log('tick raised', type(e).__name__, e); log(traceback.format_exc())
    log(f'=== pass {n+1}: tick end', round(time.monotonic()-s, 3), 's; last_error=', repr(tr._last_error), 'tasks left', list(tr._tasks), 'next_at', tr._next_at)
log('DONE alerts', len(alerts)); LOG.flush(); os._exit(0)
