"""자동매매 사후 검증 계산 (기준 구현). 거래 목록과 일별 요인 자료를 받아 표준 보고서를 만든다.

읽기 전용 계산이다. 주문·런타임·원장과 무관하다. 꼬리표는 설명용이며 주문 조건이 아니다.

판정의 원칙
- 독립 단위는 거래가 아니라 거래일이다. 같은 날의 거래는 그날 시장을 함께 맞는다.
- 주 판정은 시장과 섹터 모멘텀 요인을 뺀 일 단위 알파의 t다.
- 대조군과는 같은 날끼리 짝지어 비교한다.
- 거래마다 다른 꼬리표는 같은 날 같은 arm의 평균을 뺀 수익률로 비교한다.

거래 CSV 열: arm, symbol, day(YYYYMMDD), entry_time, exit_time, buy_amount, net_pnl. `tag_`로 시작하는 열은 꼬리표.
요인 CSV: build_factors.py의 출력.

사용: python3 -I -B postcheck.py --trades T.csv --factors F.csv --out R.txt
      [--control EXP_R] [--verdict-n 200] [--min-days 20] [--early-n 100] [--early-days 10] [--min-bucket 30] [--quiet]

판정 상태 (로드맵 1단계, 2026-11-11 개정)
- 거래 200건·거래일 20일 이상: 알파 t ≥ 임계이고 대조군 대비 일 평균 차이가 양이면 '통과 후보', 알파 t ≤ −임계면 '탈락(손실 구조)', 그 밖은 '탈락(0과 구분 안 됨)'.
- 거래 100건·거래일 10일 이상: 알파 t ≤ −임계면 '조기 탈락(손실 구조)'.
- 그 전: '표본 부족'. 임계는 자유도(거래일 − 3)에 맞춘 양측 5% t 값이다.
"""
import argparse, csv, math, statistics, collections
from statistics import NormalDist
import numpy as np

ap = argparse.ArgumentParser()
ap.add_argument('--trades', required=True); ap.add_argument('--factors', required=True); ap.add_argument('--out', required=True)
ap.add_argument('--control', default='EXP_R'); ap.add_argument('--verdict-n', type=int, default=200)
ap.add_argument('--min-days', type=int, default=20); ap.add_argument('--min-bucket', type=int, default=30)
ap.add_argument('--early-n', type=int, default=100); ap.add_argument('--early-days', type=int, default=10)
ap.add_argument('--quiet', action='store_true')
A = ap.parse_args()
Z = 1.96


def t_crit(dof):
    """양측 5% t 임계값(Cornish-Fisher 근사). 자유도 7에서 2.36, 17에서 2.11, 57에서 2.00."""
    z = 1.959964
    return z + (z ** 3 + z) / (4 * dof) + (5 * z ** 5 + 16 * z ** 3 + 3 * z) / (96 * dof ** 2)


def fnum(x):
    try:
        v = float(x)
        return v if math.isfinite(v) else None
    except Exception:
        return None


factors = {}
for row in csv.DictReader(open(A.factors, encoding='utf-8')):
    factors[row['day']] = {k: (row[k] if k == 'regime' else fnum(row[k])) for k in row if k != 'day'}

trades = []; skipped = 0
for row in csv.DictReader(open(A.trades, encoding='utf-8')):
    buy = fnum(row.get('buy_amount')); pnl = fnum(row.get('net_pnl'))
    if buy is None or buy <= 0 or pnl is None:
        skipped += 1; continue                 # 금액이나 손익이 없는 거래는 세지 않는다. 0으로 채우지 않는다
    row['_r'] = pnl / buy; row['_pnl'] = pnl; row['_buy'] = buy
    f = factors.get(row['day'], {})
    row['tag_auto_국면(전일까지)'] = f.get('regime') or ''
    row['tag_auto_당일모멘텀요인'] = '' if f.get('mom_oc') is None else ('양' if f['mom_oc'] > 0 else '음')
    row['tag_auto_당일시장장중'] = '' if f.get('mkt_oc') is None else ('상승' if f['mkt_oc'] > 0 else '하락')
    trades.append(row)
tag_cols = [c for c in (trades[0].keys() if trades else []) if c.startswith('tag_')]
arms = sorted({t['arm'] for t in trades})
by_arm = {a: sorted((t for t in trades if t['arm'] == a), key=lambda t: (t['day'], t.get('entry_time', ''))) for a in arms}


def daily_series(ts):
    d = collections.defaultdict(lambda: [0.0, 0.0])
    for t in ts:
        d[t['day']][0] += t['_pnl']; d[t['day']][1] += t['_buy']
    return {day: p / b for day, (p, b) in sorted(d.items())}


DAILY = {a: daily_series(by_arm[a]) for a in arms}


def sharpe_ci(xs):
    n = len(xs)
    if n < 3:
        return None
    m = statistics.mean(xs); sd = statistics.stdev(xs)
    if sd == 0:
        return None
    sr = m / sd; se = math.sqrt((1 + sr * sr / 2) / n)
    return dict(n=n, mean=m, sd=sd, sr=sr, lo=sr - Z * se, hi=sr + Z * se)


def tmean(xs):
    n = len(xs)
    if n < 3:
        return None
    sd = statistics.stdev(xs)
    return None if sd == 0 else statistics.mean(xs) / (sd / math.sqrt(n))


def welch(a, b):
    if len(a) < 2 or len(b) < 2:
        return None
    va = statistics.variance(a) / len(a); vb = statistics.variance(b) / len(b)
    return None if va + vb == 0 else (statistics.mean(a) - statistics.mean(b)) / math.sqrt(va + vb)


def max_drawdown(pnls):
    peak = cum = dd = 0.0
    for p in pnls:
        cum += p; peak = max(peak, cum); dd = min(dd, cum - peak)
    return dd


def pct(x, d=2):
    return 'n/a' if x is None else f'{x * 100:+.{d}f}%'


def gate(a):
    n = len(by_arm[a]); d = len(DAILY[a])
    if n < A.verdict_n or d < A.min_days:
        return f'표본 부족(거래 {n}/{A.verdict_n}, 거래일 {d}/{A.min_days})'
    return None


L = []; P = L.append
P('# 자동매매 사후 검증 보고')
P(f"거래 {len(trades)}건, arm {len(arms)}개, 거래일 {len({t['day'] for t in trades})}일 ({min(t['day'] for t in trades)}~{max(t['day'] for t in trades)}). 손익이나 금액이 비어 있어 뺀 거래 {skipped}건." if trades else '거래 0건.')
P(f"읽는 기준: arm 판정은 거래 {A.verdict_n}건과 거래일 {A.min_days}일을 모두 채운 뒤, 조기 탈락 점검은 거래 {A.early_n}건과 거래일 {A.early_days}일부터, 꼬리표 비교는 구간당 {A.min_bucket}건부터. 그 전의 숫자는 표본 부족으로 표시하고 해석하지 않는다.")

P('\n## 1. arm별 거래 요약 (서술용)')
P('arm | 거래 | 거래당 평균 | 표준편차 | 적중률 ± 오차 | 누적 순손익 | 최대 낙폭 | 거래 단위 Sharpe 구간(참고)')
for a in arms:
    ts = by_arm[a]; rs = [t['_r'] for t in ts]
    hit = sum(r > 0 for r in rs) / len(rs); hit_se = math.sqrt(hit * (1 - hit) / len(rs))
    s = sharpe_ci(rs)
    naive = 'n/a' if not s else f"{s['lo']:+.3f} ~ {s['hi']:+.3f} [{'양' if s['lo'] > 0 else '음' if s['hi'] < 0 else '0 포함'}]"
    P(f"{a} | {len(rs)} | {pct(statistics.mean(rs), 3)} | {pct(statistics.stdev(rs) if len(rs) > 1 else None, 2)} | {hit * 100:.0f}% ± {hit_se * 100:.0f}%p | "
      f"{sum(t['_pnl'] for t in ts):+,.0f}원 | {max_drawdown([t['_pnl'] for t in ts]):+,.0f}원 | {naive}")
P('- 마지막 열은 거래가 서로 독립이라고 가정한 값이다. 같은 날 거래는 독립이 아니므로 판정에 쓰지 않는다. 적중률도 판정 지표가 아니다.')

P('\n## 2. 거래일 단위 (arm 일수익률 = 그날 순손익 합 ÷ 매수금액 합)')
P('arm | 거래일 | 일 평균 | 일 표준편차 | 일 Sharpe | 95% 구간 | 상태')
for a in arms:
    xs = list(DAILY[a].values()); s = sharpe_ci(xs); g = gate(a)
    state = g or ('양(+)' if s and s['lo'] > 0 else '음(−)' if s and s['hi'] < 0 else '0과 구분 안 됨')
    P(f"{a} | {len(xs)} | {pct(statistics.mean(xs), 3)} | {pct(s['sd'] if s else None, 2)} | {('%+.3f' % s['sr']) if s else 'n/a'} | "
      f"{('%+.3f ~ %+.3f' % (s['lo'], s['hi'])) if s else 'n/a'} | {state}")
P('- 이 표는 실제로 벌었는지를 말한다. 시장이 준 것인지 arm이 한 것인지는 3절과 4절에서 가른다.')

P(f'\n## 3. 대조군({A.control}) 대비 — 같은 날끼리 짝지은 차이')
ctrl = DAILY.get(A.control, {})
PAIRED = {}
for a in arms:
    if a == A.control:
        continue
    diffs = [DAILY[a][d] - ctrl[d] for d in DAILY[a] if d in ctrl]
    if len(diffs) < A.early_days:
        P(f'- {a}: 겹치는 거래일 {len(diffs)}일, 표본 부족({A.early_days}일 필요)'); continue
    tt = tmean(diffs); PAIRED[a] = (statistics.mean(diffs), tt)
    P(f"- {a} − {A.control}: 겹치는 거래일 {len(diffs)}일, 일 평균 차이 {pct(statistics.mean(diffs), 3)}, t {tt:+.2f} (임계 {t_crit(len(diffs) - 1):.2f})")
if not ctrl:
    P(f'- 대조군 {A.control}의 거래가 없다.')

P('\n## 4. 요인 귀속 — 주 판정')
P('식: arm 일수익률 = 알파 + β시장 × 당일 KODEX200 시가→종가 + β모멘텀 × 당일 섹터 모멘텀 요인 시가→종가 + 잔차')
P('arm | 거래일 | 알파(일) | 알파 t | 임계 | β시장 | t | β모멘텀 | t | 설명력 | 종합 판정')
for a in arms:
    rows = [(r, factors[d]['mkt_oc'], factors[d]['mom_oc']) for d, r in DAILY[a].items()
            if d in factors and factors[d].get('mkt_oc') is not None and factors[d].get('mom_oc') is not None]
    n_tr = len(by_arm[a]); n_d = len(DAILY[a])
    short = f'표본 부족(거래 {n_tr}/{A.verdict_n}, 거래일 {n_d}/{A.min_days})'
    if len(rows) < A.early_days:
        P(f"{a} | {len(rows)} | 계산 안 함 | | | | | | | | {short}"); continue
    y = np.array([r[0] for r in rows]); X = np.column_stack([np.ones(len(rows)), [r[1] for r in rows], [r[2] for r in rows]])
    beta, *_ = np.linalg.lstsq(X, y, rcond=None)
    resid = y - X @ beta; dof = len(rows) - 3
    cov = (float(resid @ resid) / dof) * np.linalg.inv(X.T @ X); se = np.sqrt(np.diag(cov))
    tss = float(((y - y.mean()) ** 2).sum()); r2 = 1 - float(resid @ resid) / tss if tss > 0 else float('nan')
    ta = beta[0] / se[0]; crit = t_crit(dof)
    if a == A.control:
        verdict = '대조군'
    elif n_tr >= A.verdict_n and n_d >= A.min_days:
        if ta >= crit and a in PAIRED and PAIRED[a][0] > 0:
            verdict = '통과 후보(엣지 있음)'
        elif ta >= crit and a not in PAIRED:
            verdict = '판정 보류(대조군과 겹치는 날 부족)'
        elif ta <= -crit:
            verdict = '탈락(손실 구조)'
        else:
            verdict = '탈락(0과 구분 안 됨)'
    elif n_tr >= A.early_n and n_d >= A.early_days and ta <= -crit:
        verdict = '조기 탈락(손실 구조)'
    else:
        verdict = short
    P(f"{a} | {len(rows)} | {beta[0] * 100:+.3f}% | {ta:+.2f} | {crit:.2f} | {beta[1]:+.2f} | {beta[1] / se[1]:+.2f} | {beta[2]:+.2f} | {beta[2] / se[2]:+.2f} | {r2 * 100:.0f}% | {verdict}")
P('- 알파는 시장과 섹터 모멘텀으로 설명되지 않는 몫이다. 보유 구간이 아니라 하루 전체의 장중 요인 수익을 쓴 근사다.')
P('- 통과 후보는 알파 t가 임계 이상이고 대조군 대비 일 평균 차이가 양일 때만이다. 통과에는 이 밖에 봉인 홀드아웃, 최대 낙폭, 분해 쏠림 조건이 더 있다(로드맵 1단계).')

P('\n## 5. 꼬리표 구간별 (arm 안에서 비교)')
nd = NormalDist()
for a in arms:
    ts = by_arm[a]; usable = []
    for c in tag_cols:
        vals = [t.get(c, '') for t in ts]; nums = [fnum(v) for v in vals]
        if all(v == '' or v is None for v in vals):
            continue
        if sum(x is not None for x in nums) >= 0.8 * len(vals) and len({x for x in nums if x is not None}) > 3:
            order = sorted(x for x in nums if x is not None)
            c1, c2 = order[len(order) // 3], order[2 * len(order) // 3]
            lab = ['' if x is None else ('하' if x < c1 else '상' if x >= c2 else '중') for x in nums]; keys = ['하', '중', '상']
        else:
            lab = vals; keys = sorted({v for v in vals if v != ''})
        usable.append((c, lab, keys))
    if not usable:
        continue
    crit = nd.inv_cdf(1 - 0.025 / len(usable))
    P(f"\n### {a} — 꼬리표 {len(usable)}개, 다중 비교 보정 임계 |t| = {crit:.2f}")
    dmean = {d: statistics.mean(t['_r'] for t in ts if t['day'] == d) for d in DAILY[a]}
    for c, lab, keys in usable:
        per_day = collections.defaultdict(set)
        for t, l in zip(ts, lab):
            if l != '':
                per_day[t['day']].add(l)
        day_level = all(len(v) == 1 for v in per_day.values())
        groups = {k: [(t['_r'] if day_level else t['_r'] - dmean[t['day']]) for t, l in zip(ts, lab) if l == k] for k in keys}
        line = f"- {c[4:]}{' (날 단위 꼬리표)' if day_level else ' (같은 날 평균 대비)'}: " + ' | '.join(
            f"{k} n={len(groups[k])} 평균={pct(statistics.mean(groups[k]), 3) if groups[k] else 'n/a'}" for k in keys)
        if len(keys) >= 2:
            lo, hi = groups[keys[0]], groups[keys[-1]]
            if len(lo) >= A.min_bucket and len(hi) >= A.min_bucket:
                tt = welch(hi, lo)
                line += f" | {keys[-1]}−{keys[0]} 차이 {pct(statistics.mean(hi) - statistics.mean(lo), 3)}, t {tt:+.2f} → {'구분됨' if tt is not None and abs(tt) >= crit else '구분 안 됨'}"
            else:
                line += f" | 표본 부족(구간당 {A.min_bucket}건 필요)"
        P(line)
P('- 날 단위 꼬리표(국면, 당일 시장)의 t는 거래를 독립으로 본 값이라 크게 나온다. 방향만 참고한다.')

P('\n## 6. 읽는 순서')
P('1. 4절의 종합 판정이 표본 부족이면 여기서 멈춘다. 평균의 부호를 해석하지 않는다.')
P('2. 2절에서 실제로 벌었는지 본다.')
P('3. 3절에서 대조군보다 나은지 본다. 대조군보다 낫지 않으면 arm의 규칙이 한 일이 없다.')
P('4. 4절에서 알파의 t를 본다. 총손익이 양이어도 알파가 0과 구분되지 않으면 그 수익은 시장이나 모멘텀이 준 것이다.')
P('5. 5절은 왜 그랬는지 찾는 용도다. 구분된 꼬리표는 다음 가설의 후보이고, 주문 조건으로 바로 쓰지 않는다.')
open(A.out, 'w', encoding='utf-8').write('\n'.join(L) + '\n')
if not A.quiet:
    print('\n'.join(L))
