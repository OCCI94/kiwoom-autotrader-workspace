"""원장 복사본(읽기 전용)의 체결에서 왕복 거래 목록을 만든다. 운영 원장은 열지 않는다.

- 같은 날·같은 arm·같은 종목의 체결을 시간순으로 보며, 보유 수량이 0에서 시작해 0으로 돌아오면 왕복 1건으로 묶는다.
- 순손익은 원장에 확정값이 없으면 비운다. 대신 `gross_pnl`(체결가 차이)과, 가정 비용을 뺀 `net_pnl`을 따로 적는다.
- 꼬리표는 그 거래일 전날까지의 일봉으로만 계산한다.

사용: python3 -I -B extract_trades_from_ledger_copy.py <원장 복사본> <장후 수집 폴더> <etf json> <출력 csv> [가정 왕복 비용률]
"""
import sqlite3, sys, csv, json, os, collections

LEDGER, POSTCLOSE, ETF, OUT = sys.argv[1:5]
COST = float(sys.argv[5]) if len(sys.argv) > 5 else 0.003
con = sqlite3.connect('file:' + LEDGER + '?mode=ro&immutable=1', uri=True); con.execute('PRAGMA query_only=1')
fills = con.execute("select day, strategy, symbol, side, moment, cast(price as real), quantity from fills "
                    "where mode='paper' and (strategy like 'EXP_%' or strategy like 'S5%') order by day, strategy, symbol, moment").fetchall()
con.close()
etf = json.load(open(ETF))['products']


def daily_closes(sym, day):
    """거래일 day 전날까지의 (날짜, 종가) 목록."""
    if sym in etf:
        return [(r[0], r[4]) for r in etf[sym]['rows'] if r[0] < day]
    p = os.path.join(POSTCLOSE, day, 'daily', sym + '.json')
    if not os.path.exists(p):
        return []
    bars = json.load(open(p)).get('bars') or []
    out = sorted((str(b['day']).replace('-', ''), float(b['close'])) for b in bars)
    return [x for x in out if x[0] < day]


def tags(sym, day, entry):
    c = daily_closes(sym, day)
    t = {'tag_전일수익률%': '', 'tag_직전20일수익률%': '', 'tag_진입시간대': '오전' if entry < '12:00:00' else '오후'}
    if len(c) >= 2:
        t['tag_전일수익률%'] = round((c[-1][1] / c[-2][1] - 1) * 100, 2)
    if len(c) >= 21:
        t['tag_직전20일수익률%'] = round((c[-1][1] / c[-21][1] - 1) * 100, 2)
    return t


groups = collections.defaultdict(list)
for day, arm, sym, side, moment, price, qty in fills:
    groups[(day, arm, sym)].append((moment, side, price, qty))
rows = []; open_lots = []
for (day, arm, sym), fs in sorted(groups.items()):
    pos = 0; buy_amt = sell_amt = 0.0; entry = None
    for moment, side, price, qty in fs:
        if side == 'buy':
            if pos == 0:
                entry = moment; buy_amt = sell_amt = 0.0
            pos += qty; buy_amt += price * qty
        else:
            pos -= qty; sell_amt += price * qty
            if pos == 0 and entry is not None:
                gross = sell_amt - buy_amt
                row = {'arm': arm, 'symbol': sym, 'day': day, 'entry_time': entry, 'exit_time': moment, 'buy_amount': round(buy_amt),
                       'gross_pnl': round(gross), 'cost_assumed': round(buy_amt * COST), 'net_pnl': round(gross - buy_amt * COST)}
                row.update(tags(sym, day, entry)); rows.append(row); entry = None
    if pos != 0:
        open_lots.append((day, arm, sym, pos))
cols = ['arm', 'symbol', 'day', 'entry_time', 'exit_time', 'buy_amount', 'gross_pnl', 'cost_assumed', 'net_pnl', 'tag_전일수익률%', 'tag_직전20일수익률%', 'tag_진입시간대']
with open(OUT, 'w', newline='', encoding='utf-8') as fh:
    w = csv.DictWriter(fh, fieldnames=cols); w.writeheader(); w.writerows(rows)
print(f'왕복 거래 {len(rows)}건, 미청산 {len(open_lots)}건 {open_lots}')
for r in rows:
    print(r)
