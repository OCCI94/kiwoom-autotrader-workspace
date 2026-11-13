"""Pre-registered intraday edge hypotheses on stored 1-minute bars (all reported, none dropped).

Fills: next bar open after the signal bar; indicators use bars <= signal bar.
Cost: 0.30% round trip. Split: early days < 2026-10-28 vs later days.
"""
import sys, random, statistics, collections
sys.path.insert(0, '.')
from engine import load

COST = 0.30
days = load('bars.pkl')
syms = collections.defaultdict(list)
for (s, d) in sorted(days):
    syms[s].append(d)


def reg_of(rows):
    return [r for r in rows if r[0] < '1520']


def vwap_series(reg):
    pv = cv = 0.0; out = []
    for r in reg:
        pv += (r[2] + r[3] + r[4]) / 3 * r[5]; cv += r[5]
        out.append(pv / cv if cv else r[4])
    return out


def avgvol(reg, i, k=20):
    return sum(r[5] for r in reg[i - k:i]) / k


def T(sym, d, t, buy, sell):
    return dict(sym=sym, day=d, t=t, ret=(sell / buy - 1) * 100 - COST)


def h0_open_to_close():
    out = []
    for (s, d), rows in days.items():
        reg = reg_of(rows); out.append(T(s, d, '0900', reg[0][1], rows[-1][4]))
    return out


def h0_random_hold(hold=30, per_day=3, seed=1):
    rng = random.Random(seed); out = []
    for (s, d), rows in days.items():
        reg = reg_of(rows)
        for i in rng.sample(range(20, len(reg) - 2), per_day):
            j = i + 1 + hold
            sell = reg[j][1] if j < len(reg) else rows[-1][4]
            out.append(T(s, d, reg[i + 1][0], reg[i + 1][1], sell))
    return out


def h1_orb():
    out = []
    for (s, d), rows in days.items():
        reg = reg_of(rows)
        orr = [r for r in reg if r[0] < '0915']
        if len(orr) < 10: continue
        hi = max(r[2] for r in orr); lo = min(r[3] for r in orr)
        start = len(orr)
        for i in range(max(start, 20), len(reg) - 1):
            if reg[i][0] >= '1430': break
            if reg[i][4] > hi and reg[i][5] >= 2 * avgvol(reg, i):
                buy = reg[i + 1][1]; sell = rows[-1][4]
                for j in range(i + 1, len(reg) - 1):
                    if reg[j][4] < lo: sell = reg[j + 1][1]; break
                out.append(T(s, d, reg[i + 1][0], buy, sell)); break
    return out


def bar_event(cond, hold, t0='0920', t1='1500'):
    out = []
    for (s, d), rows in days.items():
        reg = reg_of(rows); i = 20
        while i < len(reg) - 1:
            if t0 <= reg[i][0] < t1 and cond(reg, i):
                j = i + 1 + hold
                sell = reg[j][1] if j < len(reg) else rows[-1][4]
                out.append(T(s, d, reg[i + 1][0], reg[i + 1][1], sell))
                i = j; continue
            i += 1
    return out


def h2_volume_shock_up():
    return bar_event(lambda r, i: r[i][4] / r[i][1] - 1 >= 0.01 and r[i][5] >= 5 * avgvol(r, i), 30)


def h3_reversal():
    return bar_event(lambda r, i: r[i][4] / r[i][1] - 1 <= -0.015 and r[i][5] >= 3 * avgvol(r, i), 15)


def h4_vwap_pullback():
    out = []
    for (s, d), rows in days.items():
        reg = reg_of(rows); vw = vwap_series(reg); o = reg[0][1]
        for i in range(21, len(reg) - 1):
            if not ('1000' <= reg[i][0] < '1430'): continue
            if (reg[i][4] / o - 1 >= 0.03 and reg[i - 1][4] > vw[i - 1] and reg[i][3] <= vw[i] < reg[i][4]):
                buy = reg[i + 1][1]; sell = rows[-1][4]
                for j in range(i + 1, len(reg) - 1):
                    if reg[j][4] < vw[j] * 0.99: sell = reg[j + 1][1]; break
                out.append(T(s, d, reg[i + 1][0], buy, sell)); break
    return out


def h5_late(direction):
    out = []
    for (s, d), rows in days.items():
        reg = reg_of(rows)
        k = next((i for i, r in enumerate(reg) if r[0] >= '1430'), None)
        if k is None or k < 1: continue
        r_day = reg[k - 1][4] / reg[0][1] - 1
        if (direction > 0 and r_day >= 0.03) or (direction < 0 and r_day <= -0.03):
            out.append(T(s, d, reg[k][0], reg[k][1], rows[-1][4]))
    return out


def prev_close(s, d):
    ds = syms[s]; k = ds.index(d)
    if k == 0: return None
    return days[(s, ds[k - 1])][-1][4]


def h6_gap(mode):
    out = []
    for (s, d), rows in days.items():
        pc = prev_close(s, d)
        if pc is None: continue
        reg = reg_of(rows); gap = reg[0][1] / pc - 1
        if gap < 0.03: continue
        if mode == 'open':
            out.append(T(s, d, '0900', reg[0][1], rows[-1][4]))
        else:  # gap-and-go: buy at 09:30 only if still above the open
            k = next((i for i, r in enumerate(reg) if r[0] >= '0930'), None)
            if k and reg[k - 1][4] > reg[0][1]:
                out.append(T(s, d, reg[k][0], reg[k][1], rows[-1][4]))
    return out


def h7_overnight():
    out = []
    for (s, d), rows in days.items():
        ds = syms[s]; k = ds.index(d)
        if k + 1 >= len(ds): continue
        nxt = reg_of(days[(s, ds[k + 1])])
        out.append(T(s, d, '1530', rows[-1][4], nxt[0][1]))
    return out


def report(label, t):
    if len(t) < 5:
        print(f"{label:36s} n={len(t):4d} (too few)"); return
    r = [x['ret'] for x in t]
    is_ = [x['ret'] for x in t if x['day'] < '20261028']; oo = [x['ret'] for x in t if x['day'] >= '20261028']
    by = collections.defaultdict(float)
    for x in t: by[x['sym']] += x['ret']
    top = {k for k, _ in sorted(by.items(), key=lambda kv: -kv[1])[:3]}
    rest = [x['ret'] for x in t if x['sym'] not in top]
    bd = collections.defaultdict(list)
    for x in t: bd[x['day']].append(x['ret'])
    m = lambda a: f"{statistics.mean(a):+.2f}%" if a else '   -  '
    print(f"{label:36s} n={len(r):4d} 승률={100*sum(x>0 for x in r)/len(r):3.0f}% 평균={statistics.mean(r):+.2f}% 중앙={statistics.median(r):+.2f}% | 앞 {len(is_):3d} {m(is_)} | 뒤 {len(oo):3d} {m(oo)} | 상위3제외 {m(rest)} | +일 {sum(statistics.mean(v)>0 for v in bd.values())}/{len(bd)}")


if __name__ == '__main__':
    print('== 기준선 (표본이 오르는 종목 쪽으로 치우쳤는지)')
    report('H0 시가매수→종가매도 (모든 종목·일)', h0_open_to_close())
    report('H0 무작위 진입 30분 보유', h0_random_hold())
    print('== 가설 (왕복 비용 0.30%)')
    report('H1 시가범위(15분) 돌파+거래량2배', h1_orb())
    report('H2 거래량 5배 급증 +1% 봉 → 30분', h2_volume_shock_up())
    report('H3 거래량 3배 급락 -1.5% 봉 → 15분 반등', h3_reversal())
    report('H4 +3% 강세주 VWAP 눌림 매수', h4_vwap_pullback())
    report('H5 14:30까지 +3% → 종가까지 보유', h5_late(+1))
    report('H5b 14:30까지 -3% → 종가까지 반등', h5_late(-1))
    report('H6 갭상승 3%↑ 시가매수→종가', h6_gap('open'))
    report('H6b 갭상승 3%↑ 09:30 시가 위면 매수→종가', h6_gap('go'))
    report('H7 종가매수→다음날 시가매도', h7_overnight())
