"""섹터 ETF와 지수 ETF 일봉으로 사후 검증용 일별 요인 자료를 만든다. 읽기 전용.

요인은 설명용이다. 매매 신호가 아니다.
- mkt: KODEX 200, kq: KODEX 코스닥150. cc = 전일 종가→당일 종가, oc = 당일 시가→종가, gap = 전일 종가→당일 시가.
- mom: 전일 종가 기준으로 지수 대비 20·60·120일 초과수익 평균이 높은 섹터 3분위 − 낮은 섹터 3분위의 당일 수익률.
- turn: 전일 기준 거래대금 비율(최근 10일 ÷ 직전 20일)이 높은 섹터 3분위 − 낮은 섹터 3분위.
- regime60: 직전 60거래일의 mom_cc 합. 양이면 '이어짐', 아니면 '되돌림'. 전일까지의 값만 쓴다.

사용: python3 -I -B build_factors.py <etf_daily_naver.json> <factors_daily.csv> <요약 txt>
"""
import sys, json, csv, math, statistics, datetime

SRC, OUT, SUMMARY = sys.argv[1], sys.argv[2], sys.argv[3]
DROP_DAY = '20261111'
W = (20, 60, 120)
data = json.load(open(SRC))['products']
dates = [r[0] for r in data['069500']['rows'] if r[0] != DROP_DAY]
di = {d: i for i, d in enumerate(dates)}; N = len(dates)
O = {}; C = {}; V = {}; SEC = {}
for sym, p in data.items():
    o = [None] * N; c = [None] * N; v = [None] * N
    for d, op, hi, lo, cl, vol in p['rows']:
        if d in di:
            o[di[d]] = op; c[di[d]] = cl; v[di[d]] = vol
    O[sym] = o; C[sym] = c; V[sym] = v; SEC[sym] = p['sector']
sectors = sorted({s for s in SEC.values() if not s.startswith('_market')})
members = {sec: sorted(s for s, x in SEC.items() if x == sec) for sec in sectors}


def has(sym, a, b):
    return a >= 0 and b < N and all(C[sym][i] is not None for i in range(a, b + 1))


def terciles(scored):
    scored = sorted(scored)
    k = len(scored) // 3
    return [x[1] for x in scored[-k:]], [x[1] for x in scored[:k]]


def day_ret(prods, i, kind):
    vals = []
    for s in prods:
        if C[s][i] is None or C[s][i - 1] is None or O[s][i] is None:
            continue
        vals.append(C[s][i] / C[s][i - 1] - 1 if kind == 'cc' else C[s][i] / O[s][i] - 1)
    return statistics.fmean(vals) if vals else None


rows = []
for i in range(1, N):
    row = {'day': dates[i]}
    for name, sym in (('mkt', '069500'), ('kq', '229200')):
        row[name + '_cc'] = C[sym][i] / C[sym][i - 1] - 1
        row[name + '_oc'] = C[sym][i] / O[sym][i] - 1
        row[name + '_gap'] = O[sym][i] / C[sym][i - 1] - 1
    t = i - 1                                  # 구성 기준일(전일)
    mom_cc = mom_oc = turn_cc = turn_oc = None; n_sec = 0
    if has('069500', t - W[2], t):
        b = [C['069500'][t] / C['069500'][t - w] - 1 for w in W]
        scored = []; ratios = []; prods_of = {}
        for sec in sectors:
            prods = [s for s in members[sec] if has(s, t - W[2], t)]
            if not prods:
                continue
            prods_of[sec] = prods
            e = [statistics.fmean(C[s][t] / C[s][t - w] - 1 for s in prods) for w in W]
            scored.append((statistics.fmean(x - y for x, y in zip(e, b)), sec))
            tv = [s for s in prods if all(V[s][j] is not None for j in range(t - 29, t + 1))]
            if tv:
                daily = [sum(C[s][j] * V[s][j] for s in tv) for j in range(t - 29, t + 1)]
                old = statistics.fmean(daily[:20])
                if old > 0:
                    ratios.append((statistics.fmean(daily[20:]) / old, sec))
        n_sec = len(scored)
        if len(scored) >= 12:
            hi, lo = terciles(scored)
            for kind in ('cc', 'oc'):
                a = [day_ret(prods_of[s], i, kind) for s in hi]; z = [day_ret(prods_of[s], i, kind) for s in lo]
                if all(x is not None for x in a + z):
                    val = statistics.fmean(a) - statistics.fmean(z)
                    if kind == 'cc': mom_cc = val
                    else: mom_oc = val
        if len(ratios) >= 12:
            hi, lo = terciles(ratios)
            for kind in ('cc', 'oc'):
                a = [day_ret(prods_of[s], i, kind) for s in hi]; z = [day_ret(prods_of[s], i, kind) for s in lo]
                if all(x is not None for x in a + z):
                    val = statistics.fmean(a) - statistics.fmean(z)
                    if kind == 'cc': turn_cc = val
                    else: turn_oc = val
    row.update(mom_cc=mom_cc, mom_oc=mom_oc, turn_cc=turn_cc, turn_oc=turn_oc, n_sectors=n_sec)
    rows.append(row)
for k, row in enumerate(rows):
    prev = [r['mom_cc'] for r in rows[max(0, k - 60):k]]
    if len(prev) == 60 and all(x is not None for x in prev):
        row['regime60'] = sum(prev); row['regime'] = '이어짐' if row['regime60'] > 0 else '되돌림'
    else:
        row['regime60'] = None; row['regime'] = ''
cols = ['day', 'mkt_cc', 'mkt_oc', 'mkt_gap', 'kq_cc', 'kq_oc', 'kq_gap', 'mom_cc', 'mom_oc', 'turn_cc', 'turn_oc', 'regime60', 'regime', 'n_sectors']
with open(OUT, 'w', newline='', encoding='utf-8') as fh:
    w = csv.DictWriter(fh, fieldnames=cols); w.writeheader()
    for row in rows:
        w.writerow({k: ('' if row.get(k) is None else (f'{row[k]:.6f}' if isinstance(row[k], float) else row[k])) for k in cols})


def stat(xs, per_year=252):
    xs = [x for x in xs if x is not None]
    if len(xs) < 30:
        return 'n<30'
    m = statistics.mean(xs); sd = statistics.stdev(xs)
    return f"n={len(xs)} 일평균={m * 1e4:+.1f}bp 연변동성={sd * math.sqrt(per_year) * 100:.1f}% 연 Sharpe={m / sd * math.sqrt(per_year):+.2f} t={m / (sd / math.sqrt(len(xs))):+.2f}"


def corr(a, b):
    p = [(x, y) for x, y in zip(a, b) if x is not None and y is not None]
    mx = statistics.mean(x for x, _ in p); my = statistics.mean(y for _, y in p)
    sx = math.sqrt(sum((x - mx) ** 2 for x, _ in p)); sy = math.sqrt(sum((y - my) ** 2 for _, y in p))
    return sum((x - mx) * (y - my) for x, y in p) / (sx * sy)


L = []; P = L.append
P(f"# 일별 요인 자료 요약 (작성 {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')} KST)")
P(f"기간 {rows[0]['day']}~{rows[-1]['day']} {len(rows)}일. 모멘텀 요인이 있는 날 {sum(r['mom_cc'] is not None for r in rows)}일, 국면 표시가 있는 날 {sum(bool(r['regime']) for r in rows)}일.")
for name in ('mkt_cc', 'mkt_oc', 'kq_cc', 'mom_cc', 'mom_oc', 'turn_cc', 'turn_oc'):
    P(f"- {name:8s}: {stat([r[name] for r in rows])}")
P("\n## 연도별 모멘텀 요인(mom_cc)")
for y in sorted({r['day'][:4] for r in rows}):
    P(f"- {y}: {stat([r['mom_cc'] for r in rows if r['day'][:4] == y])} | 시장 {stat([r['mkt_cc'] for r in rows if r['day'][:4] == y])}")
P("\n## 상관")
P(f"- mom_cc와 mkt_cc {corr([r['mom_cc'] for r in rows], [r['mkt_cc'] for r in rows]):+.2f}, mom_oc와 mkt_oc {corr([r['mom_oc'] for r in rows], [r['mkt_oc'] for r in rows]):+.2f}, "
  f"turn_cc와 mom_cc {corr([r['turn_cc'] for r in rows], [r['mom_cc'] for r in rows]):+.2f}, mom_cc와 mom_oc {corr([r['mom_cc'] for r in rows], [r['mom_oc'] for r in rows]):+.2f}")
valid = [r for r in rows if r['mom_cc'] is not None]
blocks = [sum(r['mom_cc'] for r in valid[k:k + 20]) for k in range(0, len(valid) - 19, 20)]
P("\n## 국면이 이어지는가 (탐색)")
P(f"- 20일 묶음 {len(blocks)}개의 모멘텀 요인 합: 양(+) {sum(b > 0 for b in blocks)}개. 한 묶음과 다음 묶음의 상관 {corr(blocks[:-1], blocks[1:]):+.2f}.")
nxt = {'이어짐': [], '되돌림': []}
for k, r in enumerate(rows):
    if r['regime'] and k + 20 <= len(rows) and all(x['mom_cc'] is not None for x in rows[k:k + 20]):
        nxt[r['regime']].append(sum(x['mom_cc'] for x in rows[k:k + 20]))
for lab, xs in nxt.items():
    P(f"- 국면 표시 '{lab}'인 날 뒤 20일의 모멘텀 요인 합: 날 수 {len(xs)}, 평균 {statistics.mean(xs) * 100:+.2f}%, 양(+) 비율 {sum(x > 0 for x in xs) / len(xs):.0%} (날이 겹치므로 유의성 판단에 쓰지 않는다)")
P("\n## 최근 12거래일")
for r in rows[-12:]:
    P(f"- {r['day']}: 시장 종가수익 {r['mkt_cc'] * 100:+.2f}% 장중 {r['mkt_oc'] * 100:+.2f}% | 모멘텀 요인 {r['mom_cc'] * 100:+.2f}% 장중 {r['mom_oc'] * 100:+.2f}% | 국면 {r['regime']} ({r['regime60'] * 100:+.1f}%)")
open(SUMMARY, 'w', encoding='utf-8').write('\n'.join(L) + '\n')
print('\n'.join(L))
