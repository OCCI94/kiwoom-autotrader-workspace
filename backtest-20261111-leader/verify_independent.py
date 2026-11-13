"""엔진을 쓰지 않고 가격 점수와 주 통계량을 다시 계산해 본 검정과 대조한다. 읽기 전용."""
import os, json, math, statistics

RD = '/Volumes/AIStockData/ai-stock-research-data'
JUMP, LOOK, H, MIN_N = 0.30, 260, 20, 90
EXCL = {'1', '2', '8', '16', '32', '64', '256'}

px = {}
for root, _, fs in os.walk(RD + '/weekly-20261026-20261030'):
    top = os.path.relpath(root, RD + '/weekly-20261026-20261030').split(os.sep)[0]
    if top not in ('core211', 'market'):
        continue
    for f in fs:
        if 'ka10081' not in f:
            continue
        d = json.load(open(os.path.join(root, f)))
        rows = sorted((r['dt'], abs(int(r['open_pric'])), abs(int(r['high_pric'])), abs(int(r['cur_prc'])), int(r['trde_qty']),
                       (r.get('upd_stkpc_tp') or '').strip()) for r in d['body']['stk_dt_pole_chart_qry'])
        if len(rows) >= 300:
            px[d.get('symbol') or d['body']['stk_cd']] = {r[0]: r for r in rows}
dates = sorted({k for v in px.values() for k in v})
N = len(dates)


def series(s, a, b):
    """dates[a..b]의 행 목록. 빠진 날이 있으면 None."""
    out = []
    for i in range(a, b + 1):
        r = px[s].get(dates[i])
        if r is None:
            return None
        out.append(r)
    return out


def clean(rows):
    """첫 행 뒤로 사건·점프·0 이하 가격이 없는가."""
    for k, r in enumerate(rows):
        if r[3] <= 0 or r[2] <= 0:
            return False
        if k > 0:
            if r[5] in EXCL or r[1] <= 0:
                return False
            if abs(r[3] / rows[k - 1][3] - 1) > JUMP:
                return False
    return True


def pct_rank(vals):
    """끝값 보정, 동점 평균 순위, 큰 값이 좋음."""
    valid = sorted((v for v in vals.values() if v is not None), reverse=True)
    n = len(valid); out = {}
    for k, v in vals.items():
        if v is None or n < 2:
            out[k] = None; continue
        first = valid.index(v) + 1; same = valid.count(v)
        out[k] = (n - (first + (same - 1) / 2)) / (n - 1) * 100
    return out


spreads = []; lines = []
for t in range(LOOK, N, 20):
    if t + H >= N:
        break
    raw = {}
    for s in px:
        w = series(s, t - LOOK, t)
        if w is None or not clean(w) or w[-1][4] <= 0:
            continue
        c = [r[3] for r in w]; h = [r[2] for r in w]
        raw[s] = dict(r20=c[-1] / c[-21] - 1, r60=c[-1] / c[-61] - 1, r125=c[-1] / c[-126] - 1, r260=c[-1] / c[0] - 1,
                      r5=c[-1] / c[-6] - 1, hi=c[-1] / max(h[-260:]))
    rk = {k: pct_rank({s: v[k] for s, v in raw.items()}) for k in ('r20', 'r60', 'r125', 'r260', 'r5', 'hi')}
    mom = {s: statistics.fmean([rk['r20'][s], rk['r60'][s], rk['r125'][s], rk['r260'][s]]) for s in raw}
    R = pct_rank(mom)
    Pm = {s: statistics.fmean([rk['r5'][s], rk['r20'][s], rk['r60'][s], rk['hi'][s]]) for s in raw}
    S = {s: (0.4 * R[s] + 0.2 * Pm[s]) / 0.6 for s in raw}
    pairs = []
    for s, sc in S.items():
        f = series(s, t, t + H)
        if f is None or not clean(f) or f[1][4] <= 0 or f[-1][4] <= 0:
            continue
        pairs.append((sc, s, f[-1][3] / f[1][1] - 1))
    if len(pairs) < MIN_N:
        continue
    pairs.sort(key=lambda p: (p[0], p[1]))
    k = len(pairs) // 3
    sp = statistics.mean(p[2] for p in pairs[-k:]) - statistics.mean(p[2] for p in pairs[:k])
    spreads.append(sp); lines.append(f'{dates[t]} n={len(pairs)} spread={sp * 100:+.2f}%')
print('\n'.join(lines))
n = len(spreads); m = statistics.mean(spreads); sd = statistics.stdev(spreads)
print(f'독립 계산: n={n} 평균={m * 100:+.2f}% t={m / (sd / math.sqrt(n)):+.2f} 양(+) {sum(x > 0 for x in spreads)}/{n} 중앙값={statistics.median(spreads) * 100:+.2f}%')
for label, xs in (('최대 구간 제외', sorted(spreads)[:-1]), ('최대·최소 구간 제외', sorted(spreads)[1:-1])):
    mm = statistics.mean(xs); ss = statistics.stdev(xs)
    print(f'{label}: n={len(xs)} 평균={mm * 100:+.2f}% t={mm / (ss / math.sqrt(len(xs))):+.2f}')
j = json.load(open(RD + '/paper-experiment-postclose/20261105/daily/069500.json'))
bars = [(str(b['day']).replace('-', ''), float(b['close'])) for b in j['bars'] if b.get('current_day_finality') != 'requires_official_close_certificate']
bars.sort()
big = [(bars[i][0], round(bars[i][1] / bars[i - 1][1] - 1, 4)) for i in range(1, len(bars)) if abs(bars[i][1] / bars[i - 1][1] - 1) > 0.08]
print('KODEX200 행', len(bars), bars[0], bars[-1], '| 일수익률 8% 초과', big)
lookup = dict(bars)
for a, b in (('20260406', '20260506'), ('20260703', '20260803')):
    print('KODEX200', a, lookup.get(a), b, lookup.get(b))
