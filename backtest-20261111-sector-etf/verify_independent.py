"""엔진 없이 H1을 다시 계산해 sector_signal_test.py의 결과와 대조한다. 읽기 전용."""
import json, math, statistics, sys, re
data = json.load(open(sys.argv[1]))['products']
dates = [r[0] for r in data['069500']['rows'] if r[0] != '20261111']
idx = {d: i for i, d in enumerate(dates)}
px = {s: {r[0]: (r[1], r[4]) for r in p['rows'] if r[0] in idx} for s, p in data.items()}
sec_of = {s: p['sector'] for s, p in data.items()}
sectors = sorted({v for v in sec_of.values() if not v.startswith('_market')})

def close(s, i): 
    v = px[s].get(dates[i]); return v[1] if v else None
def opn(s, i):
    v = px[s].get(dates[i]); return v[0] if v else None
def has(s, a, b):
    return a >= 0 and b < len(dates) and all(close(s, i) is not None for i in range(a, b + 1))

spreads = {}
for t in range(120, len(dates), 20):
    if t + 20 >= len(dates) or not has('069500', t - 120, t): continue
    b = [close('069500', t) / close('069500', t - w) - 1 for w in (20, 60, 120)]
    pos, neg = [], []
    for sec in sectors:
        prods = [s for s in data if sec_of[s] == sec and has(s, t - 120, t)]
        if not prods: continue
        e = [statistics.fmean(close(s, t) / close(s, t - w) - 1 for s in prods) for w in (20, 60, 120)]
        f = [close(s, t + 20) / opn(s, t + 1) - 1 for s in prods if has(s, t, t + 20)]
        if not f: continue
        fw = statistics.fmean(f)
        rel = [x - y for x, y in zip(e, b)]
        if all(r > 0 for r in rel) and e[0] > 0: pos.append(fw)
        elif any(r < 0 for r in rel) or e[0] <= 0: neg.append(fw)
    if len(pos) >= 3 and len(neg) >= 3:
        spreads[dates[t]] = statistics.fmean(pos) - statistics.fmean(neg)
xs = list(spreads.values()); n = len(xs); m = statistics.mean(xs); sd = statistics.stdev(xs)
print(f'독립 계산: n={n} 평균={m * 100:+.2f}% t={m / (sd / math.sqrt(n)):+.2f} 양(+) {sum(x > 0 for x in xs)}/{n}')
main = {}
for line in open(sys.argv[2], encoding='utf-8'):
    mm = re.match(r'^(\d{8}) \|\s*\d+ \|\s*\d+ \|[^|]*\|[^|]*\|\s*([+-][\d.]+)% \|', line)
    if mm: main[mm.group(1)] = float(mm.group(2)) / 100
diff = [abs(spreads[d] - main[d]) for d in main if d in spreads]
print('본 검정 행', len(main), '독립 계산 행', len(spreads), '날짜 일치', set(main) == set(spreads), '최대 차이', f'{max(diff) * 100:.3f}%p')
