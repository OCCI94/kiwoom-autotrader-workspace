"""Offline: run the recovery routine's real read-only step (_prior_days) in a second thread on the SAME tracker,
start the tracker's 20261104 'executions' task 0.4 s later (as the live loop does), using the LIVE dedup epoch
so the true 'repeat of original 103484' path is exercised. Ledger CLONE only, network blocked, isolated HOME."""
import sys, os, time, threading, json, sqlite3, socket, uuid, traceback, faulthandler, types, functools
from collections import deque
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
def _blocked(*a, **k): raise RuntimeError('network blocked in harness')
socket.socket.connect = _blocked; socket.socket.connect_ex = _blocked; socket.create_connection = _blocked; socket.getaddrinfo = _blocked
H = Path(sys.argv[1]); DAY = sys.argv[2]; BUDGET = int(sys.argv[3])
sys.path.insert(0, str(H/'pkg'))
assert str(Path.home()).startswith(str(H)), Path.home()
from kiwoom_autotrader.trading import execution_log as el
from kiwoom_autotrader.trading import paper_experiment_recovery as rec
from kiwoom_autotrader.api import executions as ex
KST = timezone(timedelta(hours=9))
T0 = datetime(2026,11,10,8,45,17,tzinfo=KST); M0 = time.monotonic()
def clock(): return T0 + timedelta(seconds=time.monotonic()-M0)
def boom(*a, **k): raise RuntimeError('fetcher must not be called')
LOG = open(H/f'replay-readhold-{DAY}.log', 'w'); LK = threading.Lock()
def log(*a):
    with LK: print(f'[{time.monotonic()-M0:8.3f}] [{threading.current_thread().name}]', *a, file=LOG, flush=True)
def _deadline():
    log('HARD DEADLINE reached; dumping stacks and exiting'); faulthandler.dump_traceback(file=LOG); LOG.flush(); os._exit(3)
threading.Timer(BUDGET, _deadline).start()
DB = str(H/'logs/.execution-ledger.sqlite3')
ro = sqlite3.connect(f'file:{DB}?mode=ro', uri=True, timeout=5)
row = ro.execute("select id,payload_json from broker_observations where day=? and source_api='kt00009' and scope='default' order by id desc limit 1", (DAY,)).fetchone()
heads = ro.execute("select epoch, count(*) from broker_observation_dedup_heads where original_row_id=? group by epoch", (row[0],)).fetchall()
ro.close(); stored = json.loads(row[1])
log('latest original id', row[0], 'heads pointing at it (epoch prefix,count):', [(e[:8], n) for e, n in heads])
assert len(heads) == 1
TOKEN = 'HARNESS-NOT-A-REAL-TOKEN-0000000000'
session = types.SimpleNamespace(token=TOKEN, mode='paper', authenticated=True)
el.session_identity = lambda s: ('paper', TOKEN)
def fake_request_rows(session, api_id, body, field, *, observer=None, day=None, before_send=None):
    event = dict(stored); event['query_id'] = uuid.uuid4().hex
    event['request_started_at'] = datetime.now(KST).isoformat(); event['observed_at'] = datetime.now(KST).isoformat()
    log('fake kt00009 response; notifying observer')
    ex._notify_observer(observer, event)
    return list(stored['response_body'][field])
ex._request_rows = fake_request_rows
def traced(name, fn, show=False):
    @functools.wraps(fn)
    def inner(*a, **k):
        s = time.monotonic(); log('>>', name)
        try: r = fn(*a, **k)
        except BaseException as e:
            log('!!', name, 'raised after', round(time.monotonic()-s, 3), 's:', type(e).__name__, str(e)[:200]); raise
        log('<<', name, round(time.monotonic()-s, 3), 's', ('-> '+repr(r)[:60]) if show else ''); return r
    return inner
el.persist_observation = traced('persist_observation', el.persist_observation, show=True)
tr = el.ExecutionTracker(log_dir=H/'logs', now=clock, settings_loader=lambda: {}, account_fetcher=boom, profit_fetcher=boom)
assert str(tr.path).startswith(str(H))
tr._broker_dedup_epoch = heads[0][0]          # live process epoch, so the head lookup hits original 103484
orig_db = tr._db
@contextmanager
def dbw():
    s = time.monotonic()
    try:
        with orig_db() as db: yield db
    except BaseException as e:
        if threading.current_thread().name == 'MainThread':
            log('!! _db block exit raised after', round(time.monotonic()-s, 3), 's UNDERLYING:', type(e).__name__, str(e)[:200])
        raise
tr._db = dbw
for name in ('record_broker_observation', '_ingest', '_closed_failed', '_materialize_history'):
    setattr(tr, name, traced(name, getattr(tr, name)))
JP = DB + '-journal'
def watch():
    last = None
    while True:
        try: cur = os.stat(JP).st_size
        except OSError: cur = None
        if cur != last: log('journal file size ->', cur); last = cur
        time.sleep(0.02)
threading.Thread(target=watch, daemon=True, name='watch').start()
def reader():
    # same open sequence as _prior_days, but the read transaction is held for HOLD seconds (live: about 21 s)
    HOLD = float(sys.argv[4]); s = time.monotonic()
    with sqlite3.connect(tr.path.resolve().as_uri()+'?mode=ro', uri=True, timeout=5) as db:
        db.row_factory = sqlite3.Row; rec.install_evidence_view(db); db.execute('PRAGMA query_only=ON'); db.execute('BEGIN')
        rows = tr._pending_order_rows(db, 'paper'); log('reader: pending-order scan done', round(time.monotonic()-s, 3), 's rows', len(rows), '; holding read transaction', HOLD, 's')
        time.sleep(HOLD)
    log('reader: read transaction closed after', round(time.monotonic()-s, 3), 's')
alerts = []
def run_tick(label):
    tr._identity = ('paper', TOKEN); tr._next_at = None
    tr._tasks = deque([('executions', DAY, None)]); tr._cycle_days = [DAY]; tr._cycle_started_at = clock()
    with tr._closed_cache_lock: tr._closed_attempts[('paper', DAY)] = {'ok': True}
    s = time.monotonic(); log(f'=== {label}: tracker tick start')
    try: tr.tick(session, lambda text: (alerts.append(text), log('ALERT after', round(time.monotonic()-s, 3), 's:', text)))
    except BaseException as e: log('tick raised', type(e).__name__, e); log(traceback.format_exc())
    log(f'=== {label}: tracker tick end', round(time.monotonic()-s, 3), 's; last_error set:', tr._last_error is not None)
R = threading.Thread(target=reader, name='recovery'); R.start()
time.sleep(0.4)
run_tick('PHASE 1 (recovery read step running)')
R.join()
run_tick('PHASE 2 (no other thread)')
n = sqlite3.connect(f'file:{DB}?mode=ro', uri=True).execute("select count(*) from broker_observation_repeats where day=? and source_api='kt00009' and observed_at>='2026-11-10T07:56'", (DAY,)).fetchone()[0]
log('kt00009 repeats for', DAY, 'stored after both phases:', n, '; alerts:', len(alerts)); LOG.flush(); os._exit(0)
