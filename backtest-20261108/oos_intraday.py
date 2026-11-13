"""Out-of-sample run of the 2026-11-02 pre-registered intraday hypotheses on the post-close 1-minute archives
(2026-11-03 .. 11-05), read-only. Reuses backtest-20261102/engine.py and hypotheses.py unchanged.

Two layers are reported:
  clean = (symbol, day) pairs where the symbol was in that day's pre-open candidate list (first_seen_at < 09:00 that day):
          the universe was fixed before the session, so no post-hoc selection bias.
  all   = every (symbol, day) with >=300 regular bars in the archives (includes symbols discovered intraday => biased).
Output text goes to argv[1]; the bars pickle goes to argv[2] (scratch).
"""
import os, sys, json, glob, pickle, random, statistics, collections, datetime
BT = '/Users/tr-oh/Documents/Codex/backtest-20261102'
RD = '/Volumes/AIStockData/ai-stock-research-data/paper-experiment-postclose'
OUT, PKL = sys.argv[1], sys.argv[2]
sys.path.insert(0, BT)

bars = collections.defaultdict(dict); pre_open = collections.defaultdict(set); all_cands = collections.defaultdict(set)
src_days = sorted(d for d in os.listdir(RD) if d.isdigit())
for day in src_days:
    u = json.load(open(f'{RD}/{day}/universe.json'))
    for c in u['candidates']:
        all_cands[day].add(c['symbol'])
        if c['first_seen_at'][:10] == f'{day[:4]}-{day[4:6]}-{day[6:]}' and c['first_seen_at'][11:16] < '09:00':
            pre_open[day].add(c['symbol'])
    for p in glob.glob(f'{RD}/{day}/minutes/*.json'):
        sym = os.path.basename(p)[:-5]; j = json.load(open(p))
        for b in j['bars']:
            if not b.get('completed', True): continue
            t = b['start_at']  # 2026-11-05T15:35:00+09:00
            key = t[:4] + t[5:7] + t[8:10] + t[11:13] + t[14:16] + '00'
            bars[sym][key] = (int(b['open']), int(b['high']), int(b['low']), int(b['close']), int(b['volume']))
pickle.dump(dict(bars), open(PKL, 'wb'))

import engine
_orig_load = engine.load
engine.load = lambda path: {}          # hypotheses.py loads 'bars.pkl' at import time; that pickle is gone. Days are injected below.
import hypotheses
engine.load = _orig_load
from engine import P
ETFS = {'069500', '229200'}   # KODEX200 / KODEX KOSDAQ150: listed as candidates (ETF arm) but reported as market reference, not tested
days_etf = {(s, d): rows for (s, d), rows in engine.load(PKL).items() if s in ETFS}
days_all = {(s, d): rows for (s, d), rows in engine.load(PKL).items() if s not in ETFS}
ALL_DAYS = sorted({d for _, d in days_all})
days_clean = {(s, d): rows for (s, d), rows in days_all.items() if s in pre_open.get(d, set())}

lines = []; W = lines.append
W(f"# 11-02 사전등록 일중 가설의 표본외 검증 (실행 {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')} KST)")
W(f"자료: 장후 수집 분봉 {', '.join(src_days)} 아카이브 합집합. 정규장 봉 ≥300개인 종목·일만 사용(11-02와 같은 규칙). 비용 왕복 {hypotheses.COST:.2f}%.")
W(f"전체 층: {len(days_all)} 종목·일 (일자별 {dict(sorted(collections.Counter(d for _, d in days_all).items()))})")
W(f"깨끗한 층(장전 후보만): {len(days_clean)} 종목·일 (일자별 {dict(sorted(collections.Counter(d for _, d in days_clean).items()))}); 장전 후보 수 {{{', '.join(f'{d}: {len(v)}' for d, v in sorted(pre_open.items()))}}}")
W("11-02 가설 정의·코드는 그대로다(backtest-20261102/hypotheses.py, engine.py). 가설을 고르거나 바꾸지 않았다. 예외 하나: H7은 '다음 거래일'이 자료상 연속일일 때만 센다(깨끗한 층에서 11-03→11-05로 건너뛰는 것을 막기 위해).")

def rep(label, t):
    if len(t) < 3:
        W(f"- {label:38s} n={len(t):3d} (해석 불가)"); return
    r = [x['ret'] for x in t]
    by = collections.defaultdict(float)
    for x in t: by[x['sym']] += x['ret']
    top = {k for k, _ in sorted(by.items(), key=lambda kv: -kv[1])[:3]}
    rest = [x['ret'] for x in t if x['sym'] not in top]
    bd = collections.defaultdict(list)
    for x in t: bd[x['day']].append(x['ret'])
    m = lambda a: f"{statistics.mean(a):+.2f}%" if a else '  -  '
    W(f"- {label:38s} n={len(r):3d} 승률={100*sum(x>0 for x in r)/len(r):3.0f}% 평균={statistics.mean(r):+.2f}% 중앙={statistics.median(r):+.2f}% | 상위3종목제외 {m(rest)} | +일 {sum(statistics.mean(v)>0 for v in bd.values())}/{len(bd)} | 일별 {' '.join(f'{d[4:]}:{statistics.mean(v):+.2f}%' for d, v in sorted(bd.items()))}")

def h7_consecutive(days):
    """hypotheses.h7_overnight with one extra condition: the next day in the symbol's list must be the next trading day in ALL_DAYS."""
    out = []
    by = collections.defaultdict(list)
    for (s, d) in sorted(days): by[s].append(d)
    for (s, d), rows in days.items():
        ds = by[s]; k = ds.index(d)
        if k + 1 >= len(ds): continue
        if ALL_DAYS.index(ds[k + 1]) != ALL_DAYS.index(d) + 1: continue
        nxt = hypotheses.reg_of(days[(s, ds[k + 1])])
        out.append(hypotheses.T(s, d, '1530', rows[-1][4], nxt[0][1]))
    return out

def run_layer(name, days):
    hypotheses.days = days
    hypotheses.syms = collections.defaultdict(list)
    for (s, d) in sorted(days): hypotheses.syms[s].append(d)
    W(f"\n## {name} ({len(days)} 종목·일)")
    W("### 기준선")
    rep('H0 시가매수→종가매도', hypotheses.h0_open_to_close())
    rep('H0 무작위 진입 30분 보유', hypotheses.h0_random_hold())
    W("### 11-02 가설 (정의 불변)")
    rep('H1 시가범위(15분) 돌파+거래량2배', hypotheses.h1_orb())
    rep('H2 거래량 5배 급증 +1% 봉 → 30분', hypotheses.h2_volume_shock_up())
    rep('H3 거래량 3배 급락 -1.5% 봉 → 15분 반등', hypotheses.h3_reversal())
    rep('H4 +3% 강세주 VWAP 눌림 매수', hypotheses.h4_vwap_pullback())
    rep('H5 14:30까지 +3% → 종가까지 보유', hypotheses.h5_late(+1))
    rep('H5b 14:30까지 -3% → 종가까지 반등', hypotheses.h5_late(-1))
    rep('H6 갭상승 3%↑ 시가매수→종가', hypotheses.h6_gap('open'))
    rep('H6b 갭상승 3%↑ 09:30 시가 위면 매수→종가', hypotheses.h6_gap('go'))
    rep('H7 종가매수→다음날 시가매도(연속일만)', h7_consecutive(days))
    W("### 교차 규칙 (engine.simulate, 비용 0.30%)")
    cand = P(short=10, long=30, slope=True, vol_mult=2, entry_end='1500')
    bot = P()
    tc = engine.simulate(days, cand); tb = engine.simulate(days, bot)
    rep('후보 10/30+거래량2배+상승+15:00마감', tc)
    rep('현재 봇 설정 5/20 10bp (비교)', tb)
    cnt = collections.Counter((t['sym'], t['day']) for t in tb)
    tr = engine.simulate(days, bot, random_entry={'counts': cnt, 'rng': random.Random(1)})
    rep('무작위 진입(봇과 같은 횟수)', tr)
    if tc:
        W(f"  후보 규칙 11-02 전체기간 값: n=107 평균 +0.37% 승률 29% PF 1.49 (비교용). 표본외 n={len(tc)}.")

W("\n## 시장 참고 (ETF 분봉, 비용 0): 시가→종가")
for (s, d), rows in sorted(days_etf.items(), key=lambda kv: (kv[0][1], kv[0][0])):
    reg = hypotheses.reg_of(rows)
    W(f"- {d} {'KODEX200   ' if s == '069500' else 'KOSDAQ150  '} 시가→종가 {(rows[-1][4] / reg[0][1] - 1) * 100:+.2f}%  (봉 {len(rows)}개)")
W("20261102 종목·일은 11-03 아카이브가 거슬러 받은 분봉이다(아카이브 1건당 900봉 ≈ 2.3거래일).")
run_layer('A. 깨끗한 층 — 장전 후보 × 같은 날', days_clean)
run_layer('B. 전체 층 — 장중 발견 후보 포함(편향 있음)', days_all)
W("\n## 한계")
W("- 표본외 기간이 거래일 2~3일, 종목·일 수십 개다. 11-02 문서가 요구한 300거래에 한참 못 미친다. 방향 확인용이지 판정이 아니다.")
W("- 11-04 아카이브의 후보는 모두 13~14시에 발견된 종목이라 깨끗한 층에서 11-04는 비어 있다. 11-03은 11-04 아카이브가 거슬러 받은 분봉만 있어 장전 후보와 거의 겹치지 않는다.")
W("- 체결은 다음 봉 시가, 미체결·부분체결·호가 공백 미반영. 봇의 TTL·분할매수·밤샘 보유 미반영(11-02와 동일).")
open(OUT, 'w', encoding='utf-8').write('\n'.join(lines) + '\n')
print('\n'.join(lines))
