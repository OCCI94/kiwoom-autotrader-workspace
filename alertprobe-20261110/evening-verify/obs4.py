import sqlite3, sys, collections
db = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
q = """select observed_at, source_api, day, 'new' k from broker_observations where observed_at>='2026-11-10T00'
       union all select observed_at, source_api, day, 'rep' from broker_observation_repeats where observed_at>='2026-11-10T00'"""
rows = sorted(db.execute(q).fetchall())
print('total stored today', len(rows))
byh = collections.defaultdict(collections.Counter)
for t, api, day, k in rows:
    key = api if day == '20261110' else f"{api}[{day[4:]}]"
    byh[t[11:13]][key] += 1
apis = ['kt00007','ka10075','kt00009','kt00009[1104]','kt00018','00']
print('hour', *apis, 'other(closed-day)', sep='\t')
for h in sorted(byh):
    c = byh[h]; other = sum(v for k, v in c.items() if k not in apis)
    print(h, *[c.get(a, 0) for a in apis], other, sep='\t')
k7 = [t for t, api, day, k in rows if api == 'kt00007']
import datetime
def p(t): return datetime.datetime.fromisoformat(t)
gaps = [(p(b)-p(a)).total_seconds() for a, b in zip(k7, k7[1:])]
after = [(a, g) for a, g in zip(k7[1:], gaps) if a >= '2026-11-10T16:12']
gs = sorted(g for _, g in after)
print('kt00007 stores after 16:12:', len(after), 'gap median', gs[len(gs)//2] if gs else None, 'min', gs[0] if gs else None, 'max', gs[-1] if gs else None)
print('last kt00007:', k7[-5:])
k9 = [(t, k) for t, api, day, k in rows if api == 'kt00009' and day == '20261104']
print('kt00009[1104] stored today:', len(k9)); print([(t[11:19], k) for t, k in k9])
