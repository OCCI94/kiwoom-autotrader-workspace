"""주도주 가격 점수 과거 검정. 사전등록.md(수정 1 포함)대로 계산한다.

읽기 전용: 연구 볼륨의 ka10081 원본과 KODEX200 일봉만 읽는다. 엔진은 import만 한다.
사용: python3 -I -B leader_price_score_test.py <출력 txt>
"""
import os, sys, json, math, statistics, random, datetime, time, collections

ENGINE = '/Users/tr-oh/Documents/Codex/2026-10-26/new-chat/outputs/ai-stock-research'
RD = '/Volumes/AIStockData/ai-stock-research-data'
OUT = sys.argv[1]
sys.path.insert(0, ENGINE)
import leader_core as lc  # noqa: E402

JUMP = 0.30
LOOK = 260            # 52주 창. 종가 261개가 필요하다
H = 20                # 주 가설 보유 기간
MIN_N = 90
LATE = '20260901'
EXCLUDE_TP = {'1', '2', '8', '16', '32', '64', '256'}   # 배당락 4는 제외 사유가 아니다(수정 1)
W_R, W_P = 0.40, 0.20
INPUT_KEYS = ('4w', '12w', '25w', '52w', '5d', '20d', '60d', 'high_ratio')
T0 = time.time()


def load():
    px = {}; cohort = {}
    for root, _, fs in os.walk(RD + '/weekly-20261026-20261030'):
        top = os.path.relpath(root, RD + '/weekly-20261026-20261030').split(os.sep)[0]
        if top not in ('core211', 'market'):
            continue
        for f in fs:
            if 'ka10081' not in f:
                continue
            d = json.load(open(os.path.join(root, f)))
            rows = (d.get('body') or {}).get('stk_dt_pole_chart_qry') or []
            sym = d.get('symbol') or d['body'].get('stk_cd')
            out = []
            for r in rows:
                try:
                    out.append((r['dt'], abs(int(r['open_pric'])), abs(int(r['high_pric'])), abs(int(r['cur_prc'])),
                                int(r['trde_qty']), (r.get('upd_stkpc_tp') or '').strip()))
                except Exception:
                    continue
            out.sort()
            if len(out) >= 300:
                px[sym] = out; cohort[sym] = top
    return px, cohort


def load_market():
    j = json.load(open(RD + '/paper-experiment-postclose/20261105/daily/069500.json'))
    m = {}
    for b in j['bars']:
        if b.get('current_day_finality') == 'requires_official_close_certificate':
            continue
        m[str(b['day']).replace('-', '')] = float(b['close'])
    return m


px, cohort = load(); mkt = load_market()
dates = sorted({r[0] for rows in px.values() for r in rows})
di = {d: i for i, d in enumerate(dates)}
N = len(dates)
O = {}; Hh = {}; C = {}; V = {}; BAD = {}
n_jump = n_event = 0
for s, rows in px.items():
    o = [None] * N; h = [None] * N; c = [None] * N; v = [None] * N; bad = [False] * N
    for dt, op, hi, cl, vol, tp in rows:
        i = di[dt]; o[i] = op; h[i] = hi; c[i] = cl; v[i] = vol
        if tp in EXCLUDE_TP:
            bad[i] = True; n_event += 1
        if cl <= 0 or hi <= 0 or op <= 0:
            bad[i] = True
    for i in range(1, N):
        if c[i] and c[i - 1] and abs(c[i] / c[i - 1] - 1) > JUMP:
            bad[i] = True; n_jump += 1
    O[s] = o; Hh[s] = h; C[s] = c; V[s] = v; BAD[s] = bad


def window_ok(s, a, b):
    """a < i <= b 구간에 사건이 없고 a..b 전 구간에 값이 있는가."""
    c = C[s]; bad = BAD[s]
    if a < 0 or b >= N:
        return False
    for i in range(a, b + 1):
        if c[i] is None:
            return False
        if i > a and bad[i]:
            return False
    return True


def iso(day, hhmm):
    return f'{day[:4]}-{day[4:6]}-{day[6:]}T{hhmm}:00+09:00'


_score_cache = {}


def scores(t, subset=None, tag='all'):
    """기준일 색인 t의 종목별 엔진 결과. {sym: (S, R, P, input_percentiles, raw)}. subset이 있으면 그 안에서만 순위를 매긴다."""
    if (t, tag) in _score_cache:
        return _score_cache[(t, tag)]
    panel = []
    for s in (px if subset is None else sorted(subset)):
        if not window_ok(s, t - LOOK, t):
            continue
        if V[s][t] is None or V[s][t] <= 0:
            continue
        panel.append({'symbol': s, 'available_at': iso(dates[t], '15:40'), 'data_date': f'{dates[t][:4]}-{dates[t][4:6]}-{dates[t][6:]}',
                      'price_basis': 'unadjusted', 'close': [float(x) for x in C[s][t - LOOK:t + 1]],
                      'high': [float(x) for x in Hh[s][t - LOOK:t + 1]]})
    out = {}
    if len(panel) >= 2:
        res = lc.compute_comp(panel, as_of=iso(dates[t], '16:00'), universe_id='local244', allow_unadjusted=True)
        for r in res['results']:
            R = r['components']['rank_mom']; P = r['components']['price_mom']
            if R is None or P is None:
                continue
            out[r['symbol']] = ((W_R * R + W_P * P) / (W_R + W_P), R, P, r['input_percentiles'], r['raw'])
    _score_cache[(t, tag)] = out
    return out


def fwd_open_close(s, t, h):
    """t+1 시가 → t+h 종가."""
    if t + h >= N or not window_ok(s, t, t + h):
        return None
    o = O[s][t + 1]; c = C[s][t + h]
    if not o or not c or not V[s][t + 1] or not V[s][t + h] or V[s][t + 1] <= 0 or V[s][t + h] <= 0:
        return None
    return c / o - 1


def fwd_close_close(s, t, h):
    if t + h >= N or not window_ok(s, t, t + h):
        return None
    return C[s][t + h] / C[s][t] - 1


def tstat(xs):
    xs = [x for x in xs if x is not None]
    n = len(xs)
    if n < 3:
        return None
    m = statistics.mean(xs); sd = statistics.stdev(xs)
    return dict(n=n, mean=m, sd=sd, t=(m / (sd / math.sqrt(n)) if sd > 0 else float('nan')), pos=sum(x > 0 for x in xs))


def fmt(st):
    if not st:
        return 'n<3'
    return f"n={st['n']:2d} 평균={st['mean'] * 100:+.2f}% t={st['t']:+.2f} 양(+) {st['pos']}/{st['n']}"


def spearman(xs, ys):
    def rank(v):
        order = sorted(range(len(v)), key=lambda i: v[i]); r = [0.0] * len(v); i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and v[order[j + 1]] == v[order[i]]:
                j += 1
            for k in range(i, j + 1):
                r[order[k]] = (i + j) / 2 + 1
            i = j + 1
        return r
    rx, ry = rank(xs), rank(ys); mx = statistics.mean(rx); my = statistics.mean(ry)
    sx = math.sqrt(sum((a - mx) ** 2 for a in rx)); sy = math.sqrt(sum((b - my) ** 2 for b in ry))
    return sum((a - mx) * (b - my) for a, b in zip(rx, ry)) / (sx * sy) if sx > 0 and sy > 0 else None


def grid(offset=0, h=H):
    return [t for t in range(LOOK + offset, N, 20) if t + h < N]


def run(signal=lambda rec: rec[0], fwd=fwd_open_close, h=H, offset=0, universe=None, tag='all', parts=3, drop_late=False):
    """기준일별 (상위−하위), (상위−유니버스), IC 목록."""
    rows = []
    for t in grid(offset, h):
        if drop_late and dates[min(t + h, N - 1)] >= LATE:
            continue
        sc = scores(t, universe, tag)
        pairs = []
        for s, rec in sc.items():
            sg = signal(rec); fw = fwd(s, t, h)
            if sg is None or fw is None:
                continue
            pairs.append((sg, fw, s))
        if len(pairs) < (MIN_N if universe is None else max(30, int(MIN_N * len(universe) / len(px)))):
            continue
        pairs.sort(key=lambda p: (p[0], p[2]))
        k = len(pairs) // parts
        bot = statistics.mean(p[1] for p in pairs[:k]); top = statistics.mean(p[1] for p in pairs[-k:])
        uni = statistics.mean(p[1] for p in pairs)
        ic = spearman([p[0] for p in pairs], [p[1] for p in pairs])
        mret = None
        d0 = dates[t]; d1 = dates[t + h]
        if d0 in mkt and d1 in mkt:
            mret = mkt[d1] / mkt[d0] - 1
        rows.append(dict(t=t, day=dates[t], n=len(pairs), top=top, bot=bot, uni=uni, spread=top - bot, top_uni=top - uni, ic=ic, mkt=mret))
    return rows


L = []
P = L.append
P(f"# 주도주 가격 점수 과거 검정 원자료 (실행 {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')} KST)")
P(f"종목 {len(px)}개(core211 {sum(v == 'core211' for v in cohort.values())}, 추가관측 {sum(v == 'market' for v in cohort.values())}), 날짜 {dates[0]}~{dates[-1]} {N}일. "
  f"제외 표시: 30% 초과 점프 {n_jump}행, 사건 코드 {n_event}행.")

main = run()
P("\n## 1. 주 가설 H1 — 상위 3분위 − 하위 3분위, t+1 시가 → t+20 종가, 20일 간격 비중첩")
P("기준일 | 유효종목 | 상위 | 하위 | 유니버스 | 상위−하위 | 상위−유니버스 | IC | KODEX200")
for r in main:
    P(f"{r['day']} | {r['n']:3d} | {r['top'] * 100:+6.2f}% | {r['bot'] * 100:+6.2f}% | {r['uni'] * 100:+6.2f}% | {r['spread'] * 100:+6.2f}% | {r['top_uni'] * 100:+6.2f}% | "
      f"{r['ic']:+.3f} | {('%+.2f%%' % (r['mkt'] * 100)) if r['mkt'] is not None else 'n/a'}")
st = tstat([r['spread'] for r in main])
P(f"- 상위−하위: {fmt(st)}")
P(f"- 상위−유니버스(H2): {fmt(tstat([r['top_uni'] for r in main]))}")
P(f"- 하위−유니버스: {fmt(tstat([r['bot'] - r['uni'] for r in main]))}")
_ic = tstat([r['ic'] for r in main])
P(f"- IC(6-f): n={_ic['n']} 평균={_ic['mean']:+.4f} t={_ic['t']:+.2f} 양(+) {_ic['pos']}/{_ic['n']}")
P(f"- 상위 3분위 − KODEX200(종가→종가 기준 시장): {fmt(tstat([r['top'] - r['mkt'] for r in main if r['mkt'] is not None]))}")
P(f"- 유니버스 평균 20일 수익률: {fmt(tstat([r['uni'] for r in main]))}")

P("\n## 2. 견고성")
for off in (5, 10, 15):
    P(f"- (a) 격자 +{off}일: {fmt(tstat([r['spread'] for r in run(offset=off)]))}")
syms = sorted(px); random.Random(20261111).shuffle(syms)
h1, h2 = set(syms[:len(syms) // 2]), set(syms[len(syms) // 2:])
P(f"- (b) 무작위 절반 1({len(h1)}종목): {fmt(tstat([r['spread'] for r in run(universe=h1, tag='half1')]))}")
P(f"- (b) 무작위 절반 2({len(h2)}종목): {fmt(tstat([r['spread'] for r in run(universe=h2, tag='half2')]))}")
P(f"- (c) 보유 구간이 {LATE} 이후에 걸치는 기준일 제외: {fmt(tstat([r['spread'] for r in run(drop_late=True)]))}")
core = {s for s, v in cohort.items() if v == 'core211'}
P(f"- (d) core211만({len(core)}종목): {fmt(tstat([r['spread'] for r in run(universe=core, tag='core')]))}")
P(f"- (e) 종가→종가(t → t+20): {fmt(tstat([r['spread'] for r in run(fwd=fwd_close_close)]))}")

P("\n## 3. 탐색 (판정 없음)")
P(f"- R(순위 모멘텀)만: {fmt(tstat([r['spread'] for r in run(signal=lambda rec: rec[1])]))}")
P(f"- P(가격 모멘텀)만: {fmt(tstat([r['spread'] for r in run(signal=lambda rec: rec[2])]))}")
for key in INPUT_KEYS:
    P(f"- 입력 {key:10s} 백분위만: {fmt(tstat([r['spread'] for r in run(signal=lambda rec, key=key: rec[3][key])]))}")
for hh in (5, 10, 30):
    P(f"- 보유 {hh}일(20일 간격 기준일, t+1 시가 → t+{hh} 종가): {fmt(tstat([r['spread'] for r in run(h=hh)]))}")
P(f"- 10분위 상위−하위: {fmt(tstat([r['spread'] for r in run(parts=10)]))}")
P(f"- 5분위 상위−하위: {fmt(tstat([r['spread'] for r in run(parts=5)]))}")

P("\n## 4. 점검용")
t0 = main[0]['t'] if main else LOOK
sc = scores(t0)
one = sorted(sc)[0]
c = C[one]
P(f"- 엔진 입력 대조 {one} @ {dates[t0]}: 직접 계산 20일 수익률 {c[t0] / c[t0 - 20] - 1:+.6f}, 엔진 raw 20d {sc[one][4]['20d']:+.6f}; "
  f"직접 52주 {c[t0] / c[t0 - 260] - 1:+.6f}, 엔진 raw 52w {sc[one][4]['52w']:+.6f}; "
  f"직접 고가비 {c[t0] / max(Hh[one][t0 - 259:t0 + 1]):.6f}, 엔진 {sc[one][4]['high_ratio']:.6f}")
P(f"- 기준일 수 {len(main)}, 기준일 {', '.join(r['day'] for r in main)}")
P(f"- 점수 분포 @ {dates[t0]}: 종목 {len(sc)}, S 최소 {min(v[0] for v in sc.values()):.1f} 최대 {max(v[0] for v in sc.values()):.1f}")
P(f"- 계산 시간 {time.time() - T0:.1f}초")
open(OUT, 'w', encoding='utf-8').write('\n'.join(L) + '\n')
print('\n'.join(L))
