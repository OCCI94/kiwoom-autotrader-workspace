"""Load stored Kiwoom ka10080 1-minute bars (read-only) into one pickle per run."""
import json, glob, os, sys, pickle, collections
ROOTS = sys.argv[1:-1]; OUT = sys.argv[-1]
bars = collections.defaultdict(dict)   # symbol -> {cntr_tm: (o,h,l,c,v)}
files = 0
for root in ROOTS:
    for p in glob.glob(os.path.join(root, '**', '*ka10080*.json'), recursive=True):
        try: d = json.load(open(p))
        except Exception: continue
        body = d.get('body') or {}
        rows = body.get('stk_min_pole_chart_qry')
        sym = d.get('symbol') or body.get('stk_cd')
        if not rows or not sym: continue
        if (d.get('request_body') or {}).get('tic_scope') not in (None, '1'): continue
        files += 1
        for r in rows:
            try:
                t = r['cntr_tm']; v = (abs(int(r['open_pric'])), abs(int(r['high_pric'])), abs(int(r['low_pric'])), abs(int(r['cur_prc'])), int(r['trde_qty']))
            except Exception: continue
            if len(t) == 14: bars[sym][t] = v
pickle.dump(dict(bars), open(OUT, 'wb'))
days = collections.Counter(); per = {}
for s, b in bars.items():
    ds = collections.Counter(t[:8] for t in b)
    per[s] = ds
    for dd, n in ds.items():
        if n >= 300: days[dd] += 1
print('files', files, 'symbols', len(bars))
print('symbols with >=300 bars, per day:', sorted(days.items()))
full = [s for s, ds in per.items() if sum(n >= 300 for n in ds.values()) >= 5]
print('symbols with >=5 full days:', len(full))
