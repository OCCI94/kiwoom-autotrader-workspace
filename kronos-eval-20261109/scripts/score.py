"""Score Kronos rolling predictions against realized returns on the local daily panel and compare
with simple baselines (prior-day return, 20-day momentum, random). Reads results/preds_<tag>.csv
and data/; writes results/score_<tag>.md and .json only."""
import os, sys, json, argparse
import numpy as np, pandas as pd

E = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ap = argparse.ArgumentParser()
ap.add_argument('--preds', required=True)
ap.add_argument('--cost-real', type=float, default=0.0023, help='round trip: 0.015% fee x2 + 0.20% sell tax')
ap.add_argument('--cost-mock', type=float, default=0.0055, help='round trip: observed paper fee 0.35% + 0.20% sell tax')
ap.add_argument('--random-draws', type=int, default=200)
a = ap.parse_args()
tag = os.path.basename(a.preds)[len('preds_'):-4]

panel = pd.read_csv(os.path.join(E, 'data', 'daily_panel.csv'), dtype={'symbol': str, 'dt': str})
dates = [l.strip() for l in open(os.path.join(E, 'data', 'dates.txt')) if l.strip()]
di = {d: i for i, d in enumerate(dates)}
close = panel.pivot(index='dt', columns='symbol', values='close').reindex(dates)
openp = panel.pivot(index='dt', columns='symbol', values='open').reindex(dates)
valid = panel.pivot(index='dt', columns='symbol', values='valid').reindex(dates).fillna(0).astype(bool)
C = close.values.astype(float); O = openp.values.astype(float); V = valid.values
sym_idx = {s: j for j, s in enumerate(close.columns)}
N = len(dates)

def fwd(i, j, h):
    if i + h >= N or not (V[i, j] and V[i + h, j]): return np.nan
    return C[i + h, j] / C[i, j] - 1
def fwd_oc(i, j, h):
    if i + h >= N or not (V[i + 1, j] and V[i + h, j]) or not np.isfinite(O[i + 1, j]) or O[i + 1, j] <= 0: return np.nan
    return C[i + h, j] / O[i + 1, j] - 1
def back(i, j, h):
    if i - h < 0 or not (V[i, j] and V[i - h, j]): return np.nan
    return C[i, j] / C[i - h, j] - 1

preds = pd.read_csv(a.preds, dtype={'anchor_dt': str, 'symbol': str})
rows = []
for r in preds.itertuples(index=False):
    i = di[r.anchor_dt]; j = sym_idx.get(r.symbol)
    if j is None: continue
    rows.append({'anchor_dt': r.anchor_dt, 'symbol': r.symbol, 'er1': r.er1, 'er5': r.er5, 'er10': r.er10,
                 'up10': r.up10, 'disp10': r.disp10, 'range10': r.range10,
                 'f1': fwd(i, j, 1), 'f5': fwd(i, j, 5), 'f10': fwd(i, j, 10), 'foc10': fwd_oc(i, j, 10),
                 'r1d': back(i, j, 1), 'mom20': back(i, j, 20)})
df = pd.DataFrame(rows)

def spearman(x, y):
    m = np.isfinite(x) & np.isfinite(y)
    if m.sum() < 20: return np.nan
    rx = pd.Series(x[m]).rank().values; ry = pd.Series(y[m]).rank().values
    if rx.std() == 0 or ry.std() == 0: return np.nan
    return float(np.corrcoef(rx, ry)[0, 1])

def ic_stats(sig, tgt):
    ics = np.array([spearman(g[sig].values, g[tgt].values) for _, g in df.groupby('anchor_dt')])
    ics = ics[np.isfinite(ics)]
    n = len(ics)
    if n == 0: return {'n': 0, 'mean': np.nan, 't': np.nan, 'hit': np.nan}
    mean = ics.mean(); std = ics.std(ddof=1) if n > 1 else np.nan
    t = mean / std * np.sqrt(n) if n > 1 and std > 0 else np.nan
    return {'n': int(n), 'mean': float(mean), 't': float(t), 'hit': float((ics > 0).mean())}

def decile(sig, tgt, q=10):
    tops, bots, unis = [], [], []
    for _, g in df.groupby('anchor_dt'):
        g = g[np.isfinite(g[sig]) & np.isfinite(g[tgt])]
        if len(g) < 50: continue
        k = max(5, len(g) // q); gs = g.sort_values(sig)
        tops.append(gs[tgt].iloc[-k:].mean()); bots.append(gs[tgt].iloc[:k].mean()); unis.append(g[tgt].mean())
    tops, bots, unis = map(np.array, (tops, bots, unis))
    n = len(tops)
    if n == 0: return None
    spread = tops - bots
    return {'n': int(n), 'top': float(tops.mean()), 'bottom': float(bots.mean()), 'universe': float(unis.mean()),
            'spread': float(spread.mean()), 'spread_t': float(spread.mean() / spread.std(ddof=1) * np.sqrt(n)) if n > 1 else np.nan,
            'top_minus_uni': float((tops - unis).mean()), 'top_net_real': float(tops.mean() - a.cost_real),
            'top_net_mock': float(tops.mean() - a.cost_mock), 'top_hit_vs_uni': float(((tops - unis) > 0).mean())}

signals = [('Kronos er10', 'er10'), ('Kronos er5', 'er5'), ('Kronos er1', 'er1'), ('Kronos up10', 'up10'),
           ('전일 수익률 r1d', 'r1d'), ('20일 모멘텀', 'mom20')]
targets = [('f1', '1일 종가→종가'), ('f5', '5일 종가→종가'), ('f10', '10일 종가→종가'), ('foc10', '익일 시가→10일 종가')]
tl = dict(targets)
res = {'tag': tag, 'anchors': int(df.anchor_dt.nunique()), 'rows': int(len(df)),
       'first_anchor': df.anchor_dt.min(), 'last_anchor': df.anchor_dt.max(), 'ic': {}, 'decile': {}}
for _, sig in signals:
    for t, _ in targets:
        res['ic'][f'{sig}|{t}'] = ic_stats(sig, t)
    res['decile'][sig] = {t: decile(sig, t) for t in ('f10', 'foc10')}

rng = np.random.default_rng(7); rand_means, rand_spreads = [], []
for _ in range(a.random_draws):
    df['rand'] = rng.standard_normal(len(df))
    rand_means.append(ic_stats('rand', 'f10')['mean'])
    d = decile('rand', 'f10'); rand_spreads.append(d['spread'] if d else np.nan)
rand_means = np.array(rand_means); rand_spreads = np.array(rand_spreads)
res['random'] = {'draws': a.random_draws, 'mean_ic_abs_mean': float(np.abs(rand_means).mean()),
                 'mean_ic_p025': float(np.quantile(rand_means, 0.025)), 'mean_ic_p975': float(np.quantile(rand_means, 0.975)),
                 'spread_p025': float(np.nanquantile(rand_spreads, 0.025)), 'spread_p975': float(np.nanquantile(rand_spreads, 0.975))}

g = res['ic']['er10|f10']; d10 = res['decile']['er10']['f10']
gate = {'ic_mean_gt_0.02': bool(g['mean'] > 0.02), 't_gt_2': bool(g['t'] > 2),
        'top_decile_net_real_gt_0': bool(d10 and d10['top_net_real'] > 0),
        'beats_r1d_abs': bool(g['mean'] > abs(res['ic']['r1d|f10']['mean'])),
        'beats_mom20_abs': bool(g['mean'] > abs(res['ic']['mom20|f10']['mean'])),
        'beats_random_p975': bool(g['mean'] > res['random']['mean_ic_p975'])}
gate['pass_all'] = all(gate.values())
res['gate'] = gate

Lns = [f"# 채점: {tag}",
       f"- 기준일 {res['anchors']}개({res['first_anchor']}~{res['last_anchor']}), 종목·기준일 {res['rows']}행. "
       f"비용(왕복): 실계좌 가정 {a.cost_real*100:.2f}%(수수료 0.015%×2 + 매도세 0.20%), 모의 관측 {a.cost_mock*100:.2f}%(수수료 0.35% + 매도세 0.20%).",
       '', '## RankIC (기준일별 횡단면 Spearman: 평균 / t / 양(+) 비율 / 기준일 수)',
       '| 신호 | ' + ' | '.join(l for _, l in targets) + ' |', '|---|' + '---|' * len(targets)]
for name, sig in signals:
    cells = []
    for t, _ in targets:
        s = res['ic'][f'{sig}|{t}']
        cells.append(f"{s['mean']:+.4f} / {s['t']:.2f} / {s['hit']:.0%} / {s['n']}" if s['n'] else 'n/a')
    Lns.append(f"| {name} | " + ' | '.join(cells) + ' |')
rr = res['random']
Lns += ['', f"무작위 신호 {a.random_draws}회(10일 종가→종가): 평균 IC의 95% 구간 [{rr['mean_ic_p025']:+.4f}, {rr['mean_ic_p975']:+.4f}], "
        f"평균 |IC| {rr['mean_ic_abs_mean']:.4f}; 10분위 스프레드 95% 구간 [{rr['spread_p025']*100:+.2f}%p, {rr['spread_p975']*100:+.2f}%p].",
        '', '## 10분위 (상위 = 신호가 큰 쪽, 동일가중, 10일 보유)',
        '| 신호 | 목표 | 상위 | 하위 | 유니버스 | 스프레드 / t | 상위−유니버스 | 상위 비용 후 (실계좌 / 모의) | n |',
        '|---|---|---|---|---|---|---|---|---|']
for name, sig in signals:
    for t in ('f10', 'foc10'):
        d = res['decile'][sig][t]
        if not d: continue
        Lns.append(f"| {name} | {tl[t]} | {d['top']*100:+.2f}% | {d['bottom']*100:+.2f}% | {d['universe']*100:+.2f}% | "
                   f"{d['spread']*100:+.2f}%p / {d['spread_t']:.2f} | {d['top_minus_uni']*100:+.2f}%p | "
                   f"{d['top_net_real']*100:+.2f}% / {d['top_net_mock']*100:+.2f}% | {d['n']} |")
Lns += ['', '## 사전 등록 관문 (er10 → 10일 종가→종가)']
Lns += [f"- {k}: {'통과' if v else '미달'}" for k, v in gate.items()]
open(os.path.join(E, 'results', f'score_{tag}.md'), 'w').write('\n'.join(Lns) + '\n')
json.dump(res, open(os.path.join(E, 'results', f'score_{tag}.json'), 'w'), indent=1, default=float, ensure_ascii=False)
print('\n'.join(Lns))
