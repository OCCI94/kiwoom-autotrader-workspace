"""Build the daily OHLCVA panel for the Kronos evaluation from the locally stored raw ka10081
responses (read-only). Same universe and the same unadjusted-data jump rule as
backtest-20261108/daily_factors.py: |daily return| > 0.30 invalidates +-5 rows; zero volume or
non-positive price invalidates the row. Writes only under the output dir given as argv[1].
"""
import os, sys, json, csv, collections
RD = '/Volumes/AIStockData/ai-stock-research-data/weekly-20261026-20261030'
OUT = sys.argv[1]
JUMP, JW = 0.30, 5

px = {}
for root, _, fs in os.walk(RD):
    for f in fs:
        if 'ka10081' not in f or not f.endswith('.json'):
            continue
        d = json.load(open(os.path.join(root, f)))
        body = d.get('body') or {}
        rows = body.get('stk_dt_pole_chart_qry') or []
        sym = d.get('symbol') or body.get('stk_cd')
        out = []
        for r in rows:
            try:
                out.append((r['dt'], abs(int(r['open_pric'])), abs(int(r['high_pric'])), abs(int(r['low_pric'])),
                            abs(int(r['cur_prc'])), int(r['trde_qty']), abs(int(r['trde_prica'] or 0)) * 1_000_000))
            except Exception:
                continue
        out.sort()
        if len(out) >= 300:
            if sym in px:
                print('duplicate symbol', sym, 'keeping longer', file=sys.stderr)
                if len(out) <= len(px[sym]): continue
            px[sym] = out

dates = sorted({r[0] for rows in px.values() for r in rows})
di = {d: i for i, d in enumerate(dates)}
stats = collections.Counter()
exclusions = {}
with open(os.path.join(OUT, 'daily_panel.csv'), 'w', newline='') as fh:
    w = csv.writer(fh)
    w.writerow(['symbol', 'dt', 'open', 'high', 'low', 'close', 'volume', 'amount', 'valid'])
    for s in sorted(px):
        rows = px[s]
        idx = {di[r[0]]: r for r in rows}
        n = len(dates)
        valid = [True] * n
        for i, r in idx.items():
            if r[5] <= 0 or r[4] <= 0 or r[1] <= 0 or r[2] <= 0 or r[3] <= 0:
                valid[i] = False; stats['zero_vol_or_price'] += 1
        prev = None
        for i in range(n):
            r = idx.get(i)
            if r is None:
                continue
            if prev is not None:
                ret = r[4] / prev - 1
                if abs(ret) > JUMP:
                    stats['jumps'] += 1
                    exclusions.setdefault(s, []).append({'dt': dates[i], 'ret': round(ret, 4)})
                    for k in range(max(0, i - JW), min(n, i + JW + 1)):
                        valid[k] = False
            prev = r[4]
        for i in range(n):
            r = idx.get(i)
            if r is None:
                stats['missing_rows'] += 1
                continue
            w.writerow([s, r[0], r[1], r[2], r[3], r[4], r[5], r[6], int(valid[i])])
            stats['rows'] += 1
            if not valid[i]: stats['invalid_rows'] += 1
with open(os.path.join(OUT, 'dates.txt'), 'w') as fh:
    fh.write('\n'.join(dates) + '\n')
json.dump(exclusions, open(os.path.join(OUT, 'exclusions.json'), 'w'), indent=1, ensure_ascii=False)
summary = {
    'symbols': len(px), 'dates': len(dates), 'first_date': dates[0], 'last_date': dates[-1],
    'symbols_with_jumps': len(exclusions), **stats,
}
json.dump(summary, open(os.path.join(OUT, 'summary.json'), 'w'), indent=1)
print(json.dumps(summary, indent=1))
