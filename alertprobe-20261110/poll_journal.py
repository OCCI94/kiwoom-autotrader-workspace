# Read-only poller v2: stat() only. Logs appearance, change AND disappearance of the ledger journal,
# with the wall-clock time of detection. No file is opened, no broker call, no process signal.
import os, sys, time, datetime
base = sys.argv[1]; out = sys.argv[2]; budget = float(sys.argv[3])
watch = {
    'journal': os.path.join(base, 'runtime', 'logs', '.execution-ledger.sqlite3-journal'),
    'ledger': os.path.join(base, 'runtime', 'logs', '.execution-ledger.sqlite3'),
    'kt00009': os.path.join(base, 'http-rate.json.paper-kt00009'),
    'kt00007': os.path.join(base, 'http-rate.json.paper-kt00007'),
    'ka10075': os.path.join(base, 'http-rate.json.paper-ka10075'),
    'state': os.path.join(base, 'telegram', 'state.json'),
}
def now(): return datetime.datetime.now().strftime('%H:%M:%S.%f')[:-3]
def ts(ns): return datetime.datetime.fromtimestamp(ns / 1e9).strftime('%H:%M:%S.%f')[:-3]
last = {}; first = True
end = time.time() + budget
with open(out, 'a') as f:
    while time.time() < end:
        for key, p in watch.items():
            try:
                st = os.stat(p); cur = (st.st_mtime_ns, st.st_size)
            except OSError:
                cur = None
            if last.get(key, 'unset') != cur:
                if not first:
                    if cur is None: f.write(f"{now()} seen | {key} GONE\n")
                    else: f.write(f"{now()} seen | {key} mtime {ts(cur[0])} size {cur[1]}\n")
                    f.flush()
                last[key] = cur
        first = False
        time.sleep(0.02)
