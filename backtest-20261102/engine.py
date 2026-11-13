"""Offline backtest of the bot's gdcrs/ddcrs SMA-cross rule on stored 1-minute bars.

Signal on a completed bar i, fill at bar i+1 open (the bot sees the bar after it
closes, then sends the order). Indicators use bars <= i only. Positions still open
at the close are exited at the day's last price (intraday-only evaluation).
"""
import pickle, random, statistics
from dataclasses import dataclass, replace

MAIN_COST = 0.30  # % round trip: ~0.03% commission + ~0.20% sell tax + ~1 tick slippage (assumption)


def load(path):
    raw = pickle.load(open(path, 'rb'))
    days = {}
    for sym, bars in raw.items():
        by = {}
        for t, v in bars.items():
            hhmm = t[8:12]
            if '0900' <= hhmm <= '1530':
                by.setdefault(t[:8], []).append((hhmm,) + v)
        for d, rows in by.items():
            rows.sort()
            regular = [r for r in rows if r[0] < '1520']
            if len(regular) >= 300:          # skip thin / partial days
                days[(sym, d)] = rows
    return days


def resample(rows, tf):
    if tf == 1:
        return rows
    out, cur, key = [], None, None
    for hh, o, h, l, c, v in rows:
        m = int(hh[:2]) * 60 + int(hh[2:])
        k = (m - 540) // tf if hh < '1520' else 10_000 + m   # keep closing-auction bar separate
        if k != key:
            if cur: out.append(tuple(cur))
            key, cur = k, [hh, o, h, l, c, v]
        else:
            cur[2] = max(cur[2], h); cur[3] = min(cur[3], l); cur[4] = c; cur[5] += v
    if cur: out.append(tuple(cur))
    return out


@dataclass(frozen=True)
class P:
    tf: int = 1
    short: int = 5
    long: int = 20
    min_bps: float = 10        # (SMA_s - SMA_l)/SMA_l at the cross bar, in bp
    confirm: int = 0           # extra bars the cross must hold before entry
    slope: bool = False        # SMA_long rising vs 5 bars ago
    vwap: bool = False         # close above session VWAP
    vol_mult: float = 0        # bar volume >= mult * mean of previous 20 bars
    atr_min: float = 0         # ATR(14)/price in % at signal
    entry_end: str = '1530'    # no new entries at/after this time
    skip_lunch: bool = False   # no entries 11:30-13:00
    cooldown: int = 20         # minutes after an exit
    max_daily: int = 2
    stop: float = 0            # % stop-loss on bar close (0 = off)
    trail: float = 0           # % trailing stop from peak close (0 = off)
    tp: float = 0              # % take-profit on bar close (0 = off)


def simulate(days, p, cost=MAIN_COST, random_entry=None):
    trades = []
    for (sym, d), rows1 in days.items():
        rows = resample(rows1, p.tf)
        reg = [r for r in rows if r[0] < '1520']
        if len(reg) < p.long + 3:
            continue
        last_px = rows[-1][4]
        closes = [r[4] for r in reg]
        n = len(reg)
        sma = lambda k, i: sum(closes[i - k + 1:i + 1]) / k
        diff = [None] * n
        for i in range(p.long - 1, n):
            diff[i] = sma(p.short, i) - sma(p.long, i)
        pv = cv = 0.0; vwap = [0] * n
        for i, r in enumerate(reg):
            typ = (r[2] + r[3] + r[4]) / 3; pv += typ * r[5]; cv += r[5]
            vwap[i] = pv / cv if cv else r[4]
        tr = [reg[0][2] - reg[0][3]] + [max(reg[i][2], reg[i - 1][4]) - min(reg[i][3], reg[i - 1][4]) for i in range(1, n)]

        def entry_ok(i):
            if diff[i] is None or diff[i - 1] is None:
                return False
            if not (diff[i - 1] <= 0 < diff[i]):
                return False
            if (diff[i] / sma(p.long, i)) * 10000 < p.min_bps:
                return False
            if p.slope and (i < p.long + 5 or sma(p.long, i) <= sma(p.long, i - 5)):
                return False
            if p.vwap and closes[i] <= vwap[i]:
                return False
            if p.vol_mult and (i < 20 or reg[i][5] < p.vol_mult * (sum(r[5] for r in reg[i - 20:i]) / 20)):
                return False
            if p.atr_min and (i < 14 or sum(tr[i - 13:i + 1]) / 14 / closes[i] * 100 < p.atr_min):
                return False
            return True

        signals = []
        for i in range(p.long, n - 1):
            if random_entry is None:
                ok = entry_ok(i)
                if ok and p.confirm:
                    j = i + p.confirm
                    ok = j < n - 1 and all(diff[k] > 0 for k in range(i, j + 1))
                    i = j if ok else i
                if ok:
                    signals.append(i)
        if random_entry is not None:
            k = random_entry['counts'].get((sym, d), 0)
            pool = list(range(p.long, n - 1))
            signals = sorted(random_entry['rng'].sample(pool, min(k, len(pool))))

        signals = set(signals)
        pos = None; entries = 0; last_exit_min = -10_000
        def mins(hh): return int(hh[:2]) * 60 + int(hh[2:])
        for i in range(p.long, n):
            if pos is not None:
                ent_i, ent_px, peak = pos
                c = closes[i]; peak = max(peak, c); pos = (ent_i, ent_px, peak)
                exit_now = diff[i] is not None and diff[i - 1] is not None and diff[i - 1] >= 0 > diff[i]
                if p.stop and c <= ent_px * (1 - p.stop / 100): exit_now = True
                if p.tp and c >= ent_px * (1 + p.tp / 100): exit_now = True
                if p.trail and c <= peak * (1 - p.trail / 100) and peak > ent_px: exit_now = True
                if exit_now and i + 1 < n:
                    px = reg[i + 1][1]
                    trades.append(dict(sym=sym, day=d, t=reg[ent_i][0], hold=mins(reg[i + 1][0]) - mins(reg[ent_i][0]),
                                       ret=(px / ent_px - 1) * 100 - cost))
                    pos = None; last_exit_min = mins(reg[i + 1][0])
                    continue
            if pos is None and i in signals and i + 1 < n:
                hh = reg[i + 1][0]
                if hh >= p.entry_end or (p.skip_lunch and '1130' <= hh < '1300'):
                    continue
                if p.max_daily and entries >= p.max_daily:
                    continue
                if p.cooldown and mins(hh) - last_exit_min < p.cooldown:
                    continue
                pos = (i + 1, reg[i + 1][1], reg[i + 1][1]); entries += 1
        if pos is not None:
            trades.append(dict(sym=sym, day=d, t=reg[pos[0]][0], hold=mins('1530') - mins(reg[pos[0]][0]),
                               ret=(last_px / pos[1] - 1) * 100 - cost))
    return trades


def summarize(trades, split=('20261028',)):
    if not trades:
        return dict(n=0)
    r = [t['ret'] for t in trades]
    wins = [x for x in r if x > 0]; losses = [-x for x in r if x <= 0]
    by_day = {}
    for t in trades:
        by_day.setdefault(t['day'], []).append(t['ret'])
    is_ = [t['ret'] for t in trades if t['day'] < split[0]]
    oos = [t['ret'] for t in trades if t['day'] >= split[0]]
    return dict(n=len(r), win=100 * len(wins) / len(r), mean=statistics.mean(r), median=statistics.median(r),
                pf=(sum(wins) / sum(losses)) if losses and sum(losses) else float('inf'),
                hold=statistics.median(t['hold'] for t in trades),
                is_n=len(is_), is_mean=statistics.mean(is_) if is_ else None,
                oos_n=len(oos), oos_mean=statistics.mean(oos) if oos else None,
                days_pos=sum(statistics.mean(v) > 0 for v in by_day.values()), days=len(by_day))
