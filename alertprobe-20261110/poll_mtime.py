# Read-only poller: stat() only. No file is opened, no broker call, no process signal.
import os, sys, time, glob, datetime
base = sys.argv[1]; out = sys.argv[2]; budget = float(sys.argv[3])
pats = [os.path.join(base, 'http-rate.json.paper-*'), os.path.join(base, 'telegram', 'listener.log'),
        os.path.join(base, 'runtime', 'logs', '.execution-ledger.sqlite3*'),
        os.path.join(base, 'telegram', 'paper-experiment.sqlite3*')]
last = {}
end = time.time() + budget
with open(out, 'a') as f:
    while time.time() < end:
        for pat in pats:
            for p in glob.glob(pat):
                try: st = os.stat(p)
                except OSError: continue
                key = os.path.basename(p)
                cur = (st.st_mtime_ns, st.st_size)
                if last.get(key) != cur:
                    if key in last:
                        ts = datetime.datetime.fromtimestamp(st.st_mtime_ns / 1e9).strftime('%H:%M:%S.%f')[:-3]
                        f.write(f"{ts} {key} size={st.st_size}\n"); f.flush()
                    last[key] = cur
        time.sleep(0.2)
