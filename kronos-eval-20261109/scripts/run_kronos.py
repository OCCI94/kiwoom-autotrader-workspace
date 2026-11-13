"""Rolling zero-shot Kronos predictions on the local daily panel (isolated venv only).

For every anchor date (>= --start, every --step trading days) and every symbol whose last
--lookback rows are present and valid, the model predicts the next --pred-len daily bars.
With --setting paper each symbol is replicated S=10 times and sampled with sample_count=1 so the
per-sample paths are available (same compute as sample_count=10, which the library would average).
Writes results/preds_<tag>.csv incrementally (resumable) and logs/run_<tag>.log.
Stops before starting a new anchor once KST time >= --deadline in the morning (default 08:30).
No network: weights must already be in hf-cache (HF_HUB_OFFLINE=1).
"""
import os, sys, time, json, argparse, datetime, resource, zoneinfo
import numpy as np, pandas as pd, torch

E = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(E, 'kronos_pkg'))
from model import Kronos, KronosTokenizer, KronosPredictor  # noqa: E402

TOK_REV = '0e0117387f39004a9016484a186a908917e22426'
MODEL_REVS = {'NeoQuasar/Kronos-small': '901c26c1332695a2a8f243eb2f37243a37bea320'}
KST = zoneinfo.ZoneInfo('Asia/Seoul')

ap = argparse.ArgumentParser()
ap.add_argument('--setting', choices=['paper', 'default'], default='paper')
ap.add_argument('--lookback', type=int, default=90)
ap.add_argument('--pred-len', type=int, default=10)
ap.add_argument('--step', type=int, default=10)
ap.add_argument('--start', default='20240701')
ap.add_argument('--device', default='cpu')
ap.add_argument('--threads', type=int, default=4)
ap.add_argument('--chunk', type=int, default=24, help='symbols per forward pass (x S sequences)')
ap.add_argument('--max-anchors', type=int, default=0)
ap.add_argument('--max-symbols', type=int, default=0)
ap.add_argument('--deadline', default='08:30')
ap.add_argument('--model', default='NeoQuasar/Kronos-small')
ap.add_argument('--model-rev', default=None)
ap.add_argument('--tag', default='')
a = ap.parse_args()

S = 10 if a.setting == 'paper' else 1
T, TOP_P = (0.6, 0.9) if a.setting == 'paper' else (1.0, 0.9)
model_rev = a.model_rev or MODEL_REVS.get(a.model)
if not model_rev:
    sys.exit('need --model-rev for ' + a.model)
tag = a.tag or f"{a.setting}_L{a.lookback}_{a.model.split('/')[-1]}_{a.device}"
out_csv = os.path.join(E, 'results', f'preds_{tag}.csv')
log_path = os.path.join(E, 'logs', f'run_{tag}.log')

def log(msg):
    line = f"{datetime.datetime.now(KST):%m-%d %H:%M:%S} {msg}"
    print(line, flush=True)
    with open(log_path, 'a') as fh:
        fh.write(line + '\n')

os.environ.setdefault('HF_HUB_OFFLINE', '1')
os.environ.setdefault('HF_HUB_DISABLE_TELEMETRY', '1')
torch.set_num_threads(a.threads)

panel = pd.read_csv(os.path.join(E, 'data', 'daily_panel.csv'), dtype={'symbol': str, 'dt': str})
dates = [l.strip() for l in open(os.path.join(E, 'data', 'dates.txt')) if l.strip()]
di = {d: i for i, d in enumerate(dates)}
ts = pd.to_datetime(pd.Series(dates), format='%Y%m%d')
n = len(dates)
cols = ['open', 'high', 'low', 'close', 'volume', 'amount']
arr = {}
for s, g in panel.groupby('symbol'):
    feat = np.full((n, 6), np.nan); present = np.zeros(n, bool); valid = np.zeros(n, bool)
    idx = g.dt.map(di).values
    feat[idx] = g[cols].values.astype(float)
    present[idx] = True; valid[idx] = g.valid.values.astype(bool)
    arr[s] = (feat, present, valid)
syms = sorted(arr)
if a.max_symbols: syms = syms[:a.max_symbols]

L, H = a.lookback, a.pred_len
first = next(i for i, d in enumerate(dates) if d >= a.start)
anchors = list(range(max(first, L - 1), n - H, a.step))
if a.max_anchors: anchors = anchors[:a.max_anchors]
done = set()
if os.path.exists(out_csv):
    done = set(pd.read_csv(out_csv, dtype={'anchor_dt': str}).anchor_dt.unique())
log(f"tag={tag} model={a.model}@{model_rev[:8]} tok@{TOK_REV[:8]} anchors={len(anchors)} "
    f"({dates[anchors[0]]}..{dates[anchors[-1]]}) done={len(done)} S={S} T={T} top_p={TOP_P} L={L} H={H} "
    f"device={a.device} threads={a.threads} chunk={a.chunk} torch={torch.__version__}")

tok = KronosTokenizer.from_pretrained('NeoQuasar/Kronos-Tokenizer-base', revision=TOK_REV).eval()
mdl = Kronos.from_pretrained(a.model, revision=model_rev).eval()
pred = KronosPredictor(mdl, tok, device=a.device, max_context=512)
log(f"params model={sum(p.numel() for p in mdl.parameters())/1e6:.1f}M tokenizer={sum(p.numel() for p in tok.parameters())/1e6:.1f}M")

header = not os.path.exists(out_csv)
t_all = time.time(); n_done = 0
for k, i in enumerate(anchors):
    now = datetime.datetime.now(KST)
    if now.strftime('%H:%M') >= a.deadline and now.hour < 16:
        log(f"deadline {a.deadline} KST reached; stopping before anchor {dates[i]}"); break
    if dates[i] in done:
        continue
    lo, hi = i - L + 1, i
    elig = [s for s in syms if arr[s][1][lo:hi + 1].all() and arr[s][2][lo:hi + 1].all()]
    x_ts = ts.iloc[lo:hi + 1].reset_index(drop=True)
    y_ts = ts.iloc[i + 1:i + 1 + H].reset_index(drop=True)
    torch.manual_seed(1000 + i); np.random.seed(1000 + i)
    t0 = time.time(); rows = []
    for c0 in range(0, len(elig), a.chunk):
        cs = elig[c0:c0 + a.chunk]
        df_list, xl, yl = [], [], []
        for s in cs:
            df = pd.DataFrame(arr[s][0][lo:hi + 1], columns=cols)
            for _ in range(S):
                df_list.append(df); xl.append(x_ts); yl.append(y_ts)
        with torch.no_grad():
            outs = pred.predict_batch(df_list, xl, yl, pred_len=H, T=T, top_k=0, top_p=TOP_P,
                                      sample_count=1, verbose=False)
        for j, s in enumerate(cs):
            last = arr[s][0][hi, 3]
            closes = np.stack([outs[j * S + m]['close'].values for m in range(S)])
            highs = np.stack([outs[j * S + m]['high'].values for m in range(S)])
            lows = np.stack([outs[j * S + m]['low'].values for m in range(S)])
            r = closes / last - 1
            rows.append({'anchor_dt': dates[i], 'symbol': s, 'last_close': last,
                         'er1': r[:, 0].mean(), 'er5': r[:, min(4, H - 1)].mean(), 'er10': r[:, H - 1].mean(),
                         'disp10': r[:, H - 1].std() if S > 1 else np.nan, 'up10': float((r[:, H - 1] > 0).mean()),
                         'range10': float(((highs - lows) / np.abs(closes)).mean()), 'n_samples': S})
    pd.DataFrame(rows).to_csv(out_csv, mode='a', header=header, index=False); header = False
    n_done += 1
    dt = time.time() - t0
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1e9
    remaining = len(anchors) - k - 1
    log(f"anchor {dates[i]} ({k + 1}/{len(anchors)}) elig={len(elig)} {dt:.1f}s maxrss={rss:.2f}GB "
        f"eta={remaining * dt / 60:.1f}min")
log(f"done anchors={n_done} in {(time.time() - t_all) / 60:.1f} min -> {out_csv}")
