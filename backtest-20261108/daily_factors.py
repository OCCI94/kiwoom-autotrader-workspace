"""Cross-sectional checks of book ch.5 effects on the locally stored ka10081 daily bars (read-only).

Universe: 248 symbols with raw ka10081 responses under weekly-20261026-20261030 (core211 + market),
600 rows each (2024-05-13 .. 2026-10-30), UNADJUSTED (upd_stkpc_tp=0). Market proxy: KODEX200 (069500)
daily closes from the post-close archive. Long-only equal-weight decile/quintile portfolios, close-to-close,
no costs unless stated. Nothing is written outside the output path given as argv[1].
"""
import os, sys, json, glob, math, statistics, collections, datetime

RD = '/Volumes/AIStockData/ai-stock-research-data'
OUT = sys.argv[1]
JUMP = 0.30          # |daily return| above this => treat as split/ex-rights/halt artefact (unadjusted data)
JUMP_WINDOW = 5      # rows around a jump to invalidate
CUTOFF_LATE = '20260901'   # last two months before the 10/30 cohort selection: flagged separately

def load_daily():
    px = {}
    for root, _, fs in os.walk(RD + '/weekly-20261026-20261030'):
        for f in fs:
            if 'ka10081' not in f: continue
            d = json.load(open(os.path.join(root, f)))
            rows = (d.get('body') or {}).get('stk_dt_pole_chart_qry') or []
            sym = d.get('symbol') or d['body'].get('stk_cd')
            out = []
            for r in rows:
                try:
                    out.append((r['dt'], abs(int(r['open_pric'])), abs(int(r['high_pric'])), abs(int(r['low_pric'])), abs(int(r['cur_prc'])), int(r['trde_qty'])))
                except Exception:
                    continue
            out.sort()
            if len(out) >= 300: px[sym] = out
    return px

def load_market():
    j = json.load(open(RD + '/paper-experiment-postclose/20261105/daily/069500.json'))
    m = {}
    for b in j['bars']:
        if b.get('current_day_finality') == 'requires_official_close_certificate': continue
        day = str(b['day']).replace('-', '')
        m[day] = float(b['close'])
    return m

px = load_daily(); mkt = load_market()
dates = sorted({r[0] for rows in px.values() for r in rows})
di = {d: i for i, d in enumerate(dates)}
# per-symbol aligned arrays
close = {}; openp = {}; valid = {}
jumps = 0; zero_vol = 0
for s, rows in px.items():
    c = [None] * len(dates); o = [None] * len(dates); v = [True] * len(dates)
    for dt, op, hi, lo, cl, vol in rows:
        i = di[dt]; c[i] = cl; o[i] = op
        if vol <= 0 or cl <= 0 or op <= 0: v[i] = False; zero_vol += 1
    # daily returns and jump flags
    for i in range(1, len(dates)):
        if c[i] and c[i-1]:
            r = c[i] / c[i-1] - 1
            if abs(r) > JUMP:
                jumps += 1
                for k in range(max(0, i - JUMP_WINDOW), min(len(dates), i + JUMP_WINDOW + 1)): v[k] = False
    close[s] = c; openp[s] = o; valid[s] = v

def ret(s, i, j):
    """close(i) -> close(j) return, None if any invalid/missing."""
    c = close[s]; v = valid[s]
    if i < 0 or j >= len(dates) or c[i] is None or c[j] is None: return None
    if not (v[i] and v[j]): return None
    return c[j] / c[i] - 1

def oc_ret(s, j):
    if j < 0 or j >= len(dates): return None
    o = openp[s][j]; c = close[s][j]
    if o is None or c is None or not valid[s][j]: return None
    return c / o - 1

def mstat(xs):
    xs = [x for x in xs if x is not None]
    if len(xs) < 3: return None
    m = statistics.mean(xs); sd = statistics.pstdev(xs) if len(xs) > 1 else 0.0
    t = m / (sd / math.sqrt(len(xs))) if sd > 0 else float('nan')
    return dict(n=len(xs), mean=m, sd=sd, t=t, pos=sum(x > 0 for x in xs) / len(xs))

def fmt(st, scale=100, unit='%'):
    if not st: return '      n<3'
    return f"n={st['n']:4d} mean={st['mean']*scale:+.3f}{unit} t={st['t']:+.2f} +days={st['pos']*100:3.0f}%"

lines = []
P = lines.append
P(f"# 일봉 횡단면 검증 (실행 {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')} KST)")
P(f"유니버스 {len(px)}종목, 날짜 {dates[0]}~{dates[-1]} ({len(dates)}일). 무수정 일봉: |일수익률|>{JUMP:.0%} 점프 {jumps}건 → 각 ±{JUMP_WINDOW}행 무효화; 거래량 0 행 {zero_vol}건 무효.")
P(f"시장 대용 KODEX200 일봉 {len(mkt)}행. 기간 구분: 전체 / {CUTOFF_LATE} 이전(코호트 선정 2개월 전까지) / 이후(선정 직전 2개월, 선택 편향 큼).")

def period_ok(d, which):
    if which == 'all': return True
    if which == 'early': return d < CUTOFF_LATE
    if which == 'late': return d >= CUTOFF_LATE
    if which.startswith('y'): return d[:4] == which[1:]
    raise ValueError(which)

PERIODS = ('all', 'early', 'late', 'y2024', 'y2025', 'y2026')

# ---------- 0. universe drift vs market ----------
P("\n## 0. 유니버스 동일가중 일수익률 vs KODEX200 (선택 편향 크기 가늠)")
for which in PERIODS:
    ew = []; mk = []; diff = []
    for i in range(1, len(dates)):
        d = dates[i]
        if not period_ok(d, which): continue
        rs = [ret(s, i-1, i) for s in px]; rs = [r for r in rs if r is not None]
        if len(rs) < 100 or d not in mkt or dates[i-1] not in mkt: continue
        e = statistics.mean(rs); m = mkt[d] / mkt[dates[i-1]] - 1
        ew.append(e); mk.append(m); diff.append(e - m)
    P(f"- {which:5s}: EW {fmt(mstat(ew))} | KODEX200 {fmt(mstat(mk))} | 차이 {fmt(mstat(diff))}  (연환산 차이 ≈ {statistics.mean(diff)*252*100:+.1f}%p)" if diff else f"- {which}: n/a")

# ---------- 1. short-term reversal: 1-day signal ----------
def decile_test(signal_fn, fwd_fn, label, step=1, min_n=80, deciles=10, universe=None, periods=PERIODS):
    P(f"\n## {label}")
    U = universe if universe is not None else list(px)
    for which in periods:
        top = []; bot = []; uni = []; top_minus_uni = []; bot_minus_uni = []
        for i in range(0, len(dates), step):
            d = dates[i]
            if not period_ok(d, which): continue
            rows = []
            for s in U:
                sg = signal_fn(s, i); fw = fwd_fn(s, i)
                if sg is None or fw is None: continue
                rows.append((sg, fw))
            if len(rows) < min_n: continue
            rows.sort()
            k = len(rows) // deciles
            if k < 3: continue
            b = statistics.mean(f for _, f in rows[:k]); t = statistics.mean(f for _, f in rows[-k:]); u = statistics.mean(f for _, f in rows)
            top.append(t); bot.append(b); uni.append(u); top_minus_uni.append(t - u); bot_minus_uni.append(b - u)
        P(f"- {which:5s}: 상위{deciles}분위 {fmt(mstat(top))} | 하위 {fmt(mstat(bot))} | 유니버스 {fmt(mstat(uni))}")
        P(f"         상위−유니버스 {fmt(mstat(top_minus_uni))} | 하위−유니버스 {fmt(mstat(bot_minus_uni))}")

decile_test(lambda s, i: ret(s, i-1, i), lambda s, i: ret(s, i, i+1),
            "1. 단기 반전(1일): 신호 = 당일 종가 수익률 분위 → 다음 날 종가→종가 수익률")
decile_test(lambda s, i: ret(s, i-1, i), lambda s, i: oc_ret(s, i+1),
            "1b. 같은 신호 → 다음 날 시가→종가 수익률(장중 보유에 해당)")
decile_test(lambda s, i: ret(s, i-1, i), lambda s, i: (None if i+1 >= len(dates) or openp[s][i+1] is None or close[s][i] is None or not (valid[s][i] and valid[s][i+1]) else openp[s][i+1] / close[s][i] - 1),
            "1c. 같은 신호 → 다음 날 갭(종가→익일 시가)")
import random as _rnd
_syms = sorted(px); _rnd.Random(20261108).shuffle(_syms)
_half1, _half2 = _syms[:len(_syms)//2], _syms[len(_syms)//2:]
decile_test(lambda s, i: ret(s, i-1, i), lambda s, i: oc_ret(s, i+1), "1b-R1. 1b를 무작위 절반 유니버스 1(%d종목)에서" % len(_half1), universe=_half1, periods=('all',), min_n=40)
decile_test(lambda s, i: ret(s, i-1, i), lambda s, i: oc_ret(s, i+1), "1b-R2. 1b를 무작위 절반 유니버스 2(%d종목)에서" % len(_half2), universe=_half2, periods=('all',), min_n=40)
decile_test(lambda s, i: ret(s, i-5, i), lambda s, i: oc_ret(s, i+1), "1d. 5일 수익률(t−5→t) 분위 → 다음 날 시가→종가 (1b와 비교: 1일 신호 vs 5일 신호)", periods=('all', 'y2024', 'y2025', 'y2026'))
decile_test(lambda s, i: ret(s, i-5, i), lambda s, i: (None if i+1 >= len(dates) or openp[s][i+1] is None or close[s][i] is None or not (valid[s][i] and valid[s][i+1]) else openp[s][i+1] / close[s][i] - 1), "1e. 5일 수익률 분위 → 다음 날 갭", periods=('all',))
decile_test(lambda s, i: ret(s, i-5, i), lambda s, i: ret(s, i, i+5), "2. 단기 반전(5일): 5일 수익률 분위 → 다음 5일 수익률 (5일 간격 비중첩)", step=5)

# ---------- 1d. surge events ----------
P("\n## 3. 급등일 사건 연구: 종가 기준 +10% 이상 오른 날 이후 (책 p.118~119 1주 반전)")
for thr in (0.10, 0.05):
    for which in PERIODS:
        n1 = []; n1oc = []; n5 = []; u1 = []; u5 = []
        for i in range(1, len(dates) - 6):
            d = dates[i]
            if not period_ok(d, which): continue
            rs = {s: ret(s, i-1, i) for s in px}
            uni_next = [ret(s, i, i+1) for s in px]; uni_next = [x for x in uni_next if x is not None]
            uni_5 = [ret(s, i, i+5) for s in px]; uni_5 = [x for x in uni_5 if x is not None]
            if len(uni_next) < 80: continue
            um1 = statistics.mean(uni_next); um5 = statistics.mean(uni_5)
            for s, r in rs.items():
                if r is None or r < thr: continue
                a = ret(s, i, i+1); b = oc_ret(s, i+1); c5 = ret(s, i, i+5)
                if a is not None: n1.append(a); u1.append(a - um1)
                if b is not None: n1oc.append(b)
                if c5 is not None: n5.append(c5); u5.append(c5 - um5)
        P(f"- 임계 +{thr:.0%} {which:5s}: 사건 {len(n1)}건 | 익일 종가→종가 {fmt(mstat(n1))} | 익일 시가→종가 {fmt(mstat(n1oc))} | 익일 초과(−유니버스) {fmt(mstat(u1))} | 5일 초과 {fmt(mstat(u5))}")

# ---------- 2. low vol / beta ----------
def trailing_vol(s, i, w=60):
    rs = [ret(s, k-1, k) for k in range(i-w+1, i+1)]
    rs = [r for r in rs if r is not None]
    if len(rs) < int(w*0.8): return None
    return statistics.pstdev(rs)

def beta(s, i, w=120):
    xs = []; ys = []
    for k in range(i-w+1, i+1):
        r = ret(s, k-1, k)
        if r is None or dates[k] not in mkt or dates[k-1] not in mkt: continue
        xs.append(mkt[dates[k]] / mkt[dates[k-1]] - 1); ys.append(r)
    if len(xs) < int(w*0.8): return None
    mx = statistics.mean(xs); my = statistics.mean(ys)
    vx = sum((x-mx)**2 for x in xs)
    if vx == 0: return None
    return sum((x-mx)*(y-my) for x, y in zip(xs, ys)) / vx

decile_test(trailing_vol, lambda s, i: ret(s, i, i+20), "4. 저변동성 이상: 60일 변동성 5분위 → 다음 20일 수익률 (20일 간격)", step=20, deciles=5)
decile_test(beta, lambda s, i: ret(s, i, i+20), "5. BAB: 120일 β(대 KODEX200) 5분위 → 다음 20일 수익률 (20일 간격)", step=20, deciles=5)

# ---------- 3. momentum ----------
decile_test(lambda s, i: ret(s, i-252, i-21) if i-252 >= 0 else None, lambda s, i: ret(s, i, i+20),
            "6. 모멘텀 12-1: t−252→t−21 수익률 5분위 → 다음 20일 (20일 간격)", step=20, deciles=5)
decile_test(lambda s, i: ret(s, i-21, i), lambda s, i: ret(s, i, i+20),
            "7. 1개월 수익률(t−21→t) 5분위 → 다음 20일 (책: 0~1개월은 반전 구간)", step=20, deciles=5)

P("\n## 해석 시 주의")
P("- 유니버스는 2026-10-30 전후에 '수집 가치가 있다'고 고른 248종목의 합집합이라 생존·선택 편향이 있다. §0의 EW−KODEX200 차이가 그 크기다. 횡단면 분위 '차이'는 수준보다 덜 민감하지만 면제는 아니다.")
P("- 무수정 가격이라 분할·배당락·거래정지 복귀가 점프로 섞인다. 30% 규칙으로 걸러낸 뒤 남은 왜곡은 있을 수 있다.")
P("- 종가→종가 결과는 봇의 분 단위 보유와 다른 시간 척도다. 1b(시가→종가)가 봇에 가장 가깝다.")
P("- 비용 0. 거래로 쓰려면 왕복 0.3%+1틱을 빼야 한다. 이 문서는 효과의 방향·크기 확인이지 전략 성과가 아니다.")
open(OUT, 'w', encoding='utf-8').write('\n'.join(lines) + '\n')
print('\n'.join(lines))
