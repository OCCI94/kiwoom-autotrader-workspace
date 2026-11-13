"""섹터 ETF 신호 과거 검정. 사전등록.md대로 계산한다. 읽기 전용.

사용: python3 -I -B sector_signal_test.py <etf_daily_naver.json> <출력 txt>
"""
import sys, json, math, statistics, datetime, time
from math import comb

ENGINE = '/Users/tr-oh/Documents/Codex/2026-10-26/new-chat/outputs/ai-stock-research'
sys.path.insert(0, ENGINE)
import leader_core as lc  # noqa: E402

SRC, OUT = sys.argv[1], sys.argv[2]
DROP_DAY = '20261111'          # 장중 미완성 행
W1, W3, W6 = 20, 60, 120       # 엔진 등록값 valuation.return_periods
H = 20
JUMP = 0.30
BENCH = '069500'
T0 = time.time()

data = json.load(open(SRC))['products']
dates = [r[0] for r in data[BENCH]['rows'] if r[0] != DROP_DAY]
di = {d: i for i, d in enumerate(dates)}
N = len(dates)
O = {}; C = {}; V = {}; BAD = {}; SECTOR = {}; FIRST = {}
for sym, p in data.items():
    o = [None] * N; c = [None] * N; v = [None] * N; bad = [False] * N
    for d, op, hi, lo, cl, vol in p['rows']:
        if d not in di:
            continue
        i = di[d]; o[i] = op; c[i] = cl; v[i] = vol
        if cl <= 0 or op <= 0:
            bad[i] = True
    for i in range(1, N):
        if c[i] and c[i - 1] and abs(c[i] / c[i - 1] - 1) > JUMP:
            bad[i] = True
    O[sym] = o; C[sym] = c; V[sym] = v; BAD[sym] = bad; SECTOR[sym] = p['sector']
    FIRST[sym] = next((i for i in range(N) if c[i] is not None), None)
sectors = sorted({s for s in SECTOR.values() if not s.startswith('_market')})
members = {sec: sorted(sym for sym, s in SECTOR.items() if s == sec) for sec in sectors}
full_sectors = [sec for sec in sectors if any(FIRST[sym] == 0 for sym in members[sec])]


def ok(sym, a, b):
    if a < 0 or b >= N:
        return False
    c = C[sym]; bad = BAD[sym]
    for i in range(a, b + 1):
        if c[i] is None or (i > a and bad[i]):
            return False
    return True


def ret(sym, a, b):
    return C[sym][b] / C[sym][a] - 1


def fwd_oc(sym, t, h):
    if not ok(sym, t, t + h) or not O[sym][t + 1]:
        return None
    return C[sym][t + h] / O[sym][t + 1] - 1


def fwd_cc(sym, t, h):
    if not ok(sym, t, t + h):
        return None
    return C[sym][t + h] / C[sym][t] - 1


def mean(xs):
    xs = [x for x in xs if x is not None]
    return statistics.fmean(xs) if xs else None


_cache = {}


def sector_state(t, bench=BENCH):
    """기준일 t의 섹터별 신호. {sector: dict(mom, rel(1m,3m,6m), score, ratio, products)}"""
    key = (t, bench)
    if key in _cache:
        return _cache[key]
    out = {}
    if not ok(bench, t - W6, t):
        _cache[key] = out; return out
    b = {'1m': ret(bench, t - W1, t), '3m': ret(bench, t - W3, t), '6m': ret(bench, t - W6, t)}
    for sec in sectors:
        prods = [s for s in members[sec] if ok(s, t - W6, t)]
        if not prods:
            continue
        e = {'1m': mean(ret(s, t - W1, t) for s in prods), '3m': mean(ret(s, t - W3, t) for s in prods), '6m': mean(ret(s, t - W6, t) for s in prods)}
        res = lc.evaluate_sector_etf(etf_returns=e, benchmark_returns=b, trend_1m_unbroken=bool(e['1m'] > 0), price_basis='adjusted')
        rel = res['relative_returns']
        tv_prods = [s for s in prods if all(V[s][i] is not None for i in range(t - 29, t + 1))]
        ratio = None
        if tv_prods:
            daily = [sum(C[s][i] * V[s][i] for s in tv_prods) for i in range(t - 29, t + 1)]
            old = statistics.fmean(daily[:20]); recent = statistics.fmean(daily[20:])
            ratio = recent / old if old > 0 else None
        out[sec] = dict(mom=res['signals']['momentum'], rel=rel, score=statistics.fmean([rel['1m'], rel['3m'], rel['6m']]), ratio=ratio, products=prods, action=res['action'])
    _cache[key] = out
    return out


def sector_fwd(sec, st, t, h, fwd):
    vals = [fwd(s, t, h) for s in st['products']]
    if any(v is None for v in vals):
        vals = [v for v in vals if v is not None]
    return statistics.fmean(vals) if vals else None


def grid(offset=0, h=H):
    return [t for t in range(W6 + offset, N, 20) if t + h < N]


def run(group_fn, h=H, offset=0, fwd=fwd_oc, universe=None, bench=BENCH, min_a=3, min_b=3):
    """group_fn(state) -> 'A', 'B' 또는 None. 기준일별 A 평균 − B 평균."""
    rows = []
    for t in grid(offset, h):
        st = sector_state(t, bench)
        A = []; B = []; ALL = []
        for sec, s in st.items():
            if universe is not None and sec not in universe:
                continue
            f = sector_fwd(sec, s, t, h, fwd)
            if f is None:
                continue
            ALL.append(f)
            g = group_fn(s)
            if g == 'A': A.append(f)
            elif g == 'B': B.append(f)
        if len(A) < min_a or len(B) < min_b:
            continue
        bf = fwd(BENCH, t, h)
        rows.append(dict(t=t, day=dates[t], nA=len(A), nB=len(B), a=statistics.fmean(A), b=statistics.fmean(B), spread=statistics.fmean(A) - statistics.fmean(B),
                         all=statistics.fmean(ALL), bench=bf, nall=len(ALL)))
    return rows


def tercile_run(key_fn, h=H, offset=0, fwd=fwd_oc, min_n=12):
    rows = []
    for t in grid(offset, h):
        st = sector_state(t)
        pairs = []
        for sec, s in st.items():
            k = key_fn(s); f = sector_fwd(sec, s, t, h, fwd)
            if k is None or f is None:
                continue
            pairs.append((k, sec, f))
        if len(pairs) < min_n:
            continue
        pairs.sort()
        m = len(pairs) // 3
        rows.append(dict(day=dates[t], spread=statistics.fmean(p[2] for p in pairs[-m:]) - statistics.fmean(p[2] for p in pairs[:m]), n=len(pairs)))
    return rows


def tstat(xs):
    xs = [x for x in xs if x is not None]
    n = len(xs)
    if n < 3:
        return None
    m = statistics.mean(xs); sd = statistics.stdev(xs)
    return dict(n=n, mean=m, sd=sd, t=(m / (sd / math.sqrt(n)) if sd > 0 else float('nan')), pos=sum(x > 0 for x in xs))


def fmt(st):
    return 'n<3' if not st else f"n={st['n']:2d} 평균={st['mean'] * 100:+.2f}% t={st['t']:+.2f} 양(+) {st['pos']}/{st['n']}"


def t_crit(df):
    z = 1.959964
    return z + (z ** 3 + z) / (4 * df) + (5 * z ** 5 + 16 * z ** 3 + 3 * z) / (96 * df ** 2)


def binom_k(n):
    for k in range(n + 1):
        if sum(comb(n, j) for j in range(k, n + 1)) / 2 ** n <= 0.05:
            return k


MOM = lambda s: 'A' if s['mom'] == 'POSITIVE' else 'B' if s['mom'] == 'NEGATIVE' else None
TURN = lambda s: None if s['ratio'] is None else 'A' if s['ratio'] >= 1.20 else 'B' if s['ratio'] < 1.00 else None
BOTH = lambda s: 'A' if (s['mom'] == 'POSITIVE' and s['ratio'] is not None and s['ratio'] >= 1.20) else 'B'

L = []
P = L.append
P(f"# 섹터 ETF 신호 과거 검정 원자료 (실행 {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')} KST)")
P(f"날짜 {dates[0]}~{dates[-1]} {N}일, 섹터 {len(sectors)}개(5년 전체 이력 {len(full_sectors)}개), 상품 {len(SECTOR) - 2}개. 30% 초과 점프 표시 {sum(sum(b) for b in BAD.values())}행.")
cnt = {'POSITIVE': 0, 'NEGATIVE': 0, 'NEUTRAL': 0, 'UNKNOWN': 0}
for t in grid():
    for s in sector_state(t).values():
        cnt[s['mom']] += 1
P(f"기준일 {len(grid())}개. 섹터·기준일 {sum(cnt.values())}건 중 모멘텀 긍정 {cnt['POSITIVE']}, 부정 {cnt['NEGATIVE']}, 중립 {cnt['NEUTRAL']}, 모름 {cnt['UNKNOWN']}.")

main = run(MOM)
P("\n## 1. 주 가설 H1 — 모멘텀 긍정 섹터 − 부정 섹터, t+1 시가 → t+20 종가")
P("기준일 | 긍정 수 | 부정 수 | 긍정 평균 | 부정 평균 | 차이 | 전체 평균 | KODEX200")
for r in main:
    P(f"{r['day']} | {r['nA']:2d} | {r['nB']:2d} | {r['a'] * 100:+6.2f}% | {r['b'] * 100:+6.2f}% | {r['spread'] * 100:+6.2f}% | {r['all'] * 100:+6.2f}% | {r['bench'] * 100:+6.2f}%")
st = tstat([r['spread'] for r in main])
P(f"- 긍정 − 부정: {fmt(st)} | 중앙값 {statistics.median(r['spread'] for r in main) * 100:+.2f}%")
P(f"- 임계 t(자유도 {st['n'] - 1}) = {t_crit(st['n'] - 1):.3f}, 양(+) 기준일 이항 기준 = {binom_k(st['n'])}/{st['n']}")
P(f"- 버린 기준일(긍정 또는 부정 섹터 3개 미만): {len(grid()) - len(main)}개")
P(f"- (e) 긍정 − 전체 섹터 평균: {fmt(tstat([r['a'] - r['all'] for r in main]))}")
P(f"- (e) 긍정 − KODEX200: {fmt(tstat([r['a'] - r['bench'] for r in main]))}")
P(f"- 부정 − 전체 섹터 평균: {fmt(tstat([r['b'] - r['all'] for r in main]))}")
P(f"- 전체 섹터 평균 20일 수익률: {fmt(tstat([r['all'] for r in main]))} | KODEX200: {fmt(tstat([r['bench'] for r in main]))}")

P("\n## 2. 견고성")
for off in (5, 10, 15):
    P(f"- (a) 격자 +{off}일: {fmt(tstat([r['spread'] for r in run(MOM, offset=off)]))}")
half = len(main) // 2
P(f"- (b) 앞 절반({main[0]['day']}~{main[half - 1]['day']}): {fmt(tstat([r['spread'] for r in main[:half]]))}")
P(f"- (b) 뒤 절반({main[half]['day']}~{main[-1]['day']}): {fmt(tstat([r['spread'] for r in main[half:]]))}")
P(f"- (c) 5년 전체 이력 {len(full_sectors)}개 섹터만: {fmt(tstat([r['spread'] for r in run(MOM, universe=set(full_sectors))]))}")
P(f"- (d) 종가→종가: {fmt(tstat([r['spread'] for r in run(MOM, fwd=fwd_cc)]))}")

P("\n## 3. 보조 가설")
h2 = run(TURN)
P(f"- H2 거래대금 증가(≥1.2) − 감소(<1.0): {fmt(tstat([r['spread'] for r in h2]))} | 기준일당 증가 섹터 평균 {statistics.fmean(r['nA'] for r in h2):.1f}개, 감소 {statistics.fmean(r['nB'] for r in h2):.1f}개")
h3 = run(BOTH, min_a=2, min_b=3)
P(f"- H3 모멘텀 긍정·거래대금 증가 − 나머지: {fmt(tstat([r['spread'] for r in h3]))} | 기준일당 해당 섹터 평균 {statistics.fmean(r['nA'] for r in h3):.1f}개 (해당 2개 이상인 기준일만)")
P(f"- H1b 초과수익 평균 순위 상위 3분위 − 하위 3분위: {fmt(tstat([r['spread'] for r in tercile_run(lambda s: s['score'])]))}")

P("\n## 4. 탐색 (판정 없음)")
for hh in (5, 10, 40, 60):
    P(f"- 보유 {hh}일(20일 간격 기준일{', 구간 겹침' if hh > 20 else ''}): {fmt(tstat([r['spread'] for r in run(MOM, h=hh)]))}")
for k, name in (('1m', '20일'), ('3m', '60일'), ('6m', '120일')):
    P(f"- {name} 초과수익 부호만(양 − 음): {fmt(tstat([r['spread'] for r in run(lambda s, k=k: 'A' if s['rel'][k] > 0 else 'B' if s['rel'][k] < 0 else None)]))}")
    P(f"  {name} 초과수익 순위 상위 3분위 − 하위 3분위: {fmt(tstat([r['spread'] for r in tercile_run(lambda s, k=k: s['rel'][k])]))}")
P(f"- 기준지수를 코스닥150으로: {fmt(tstat([r['spread'] for r in run(MOM, bench='229200')]))}")
P(f"- 거래대금 비율 상위 3분위 − 하위 3분위: {fmt(tstat([r['spread'] for r in tercile_run(lambda s: s['ratio'])]))}")

P("\n## 5. 점검용")
t1 = main[len(main) // 2]['t']; s1 = sector_state(t1)
sec = sorted(s1)[0]; pr = s1[sec]['products']
P(f"- 엔진 입력 대조 {sec} @ {dates[t1]}: 상품 {pr}, 직접 20일 {statistics.fmean(C[s][t1] / C[s][t1 - 20] - 1 for s in pr):+.6f}, "
  f"지수 20일 {C[BENCH][t1] / C[BENCH][t1 - 20] - 1:+.6f}, 엔진 초과 1m {s1[sec]['rel']['1m']:+.6f}, 신호 {s1[sec]['mom']}, 엔진 action {s1[sec]['action']}")
P(f"- 5년 전체 이력 섹터: {', '.join(full_sectors)}")
P(f"- 계산 시간 {time.time() - T0:.1f}초")
open(OUT, 'w', encoding='utf-8').write('\n'.join(L) + '\n')
print('\n'.join(L))
