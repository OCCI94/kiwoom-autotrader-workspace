"""연구 엔진(leader_core.py)으로 기대값을 만들어 JS 대조용 벡터 파일을 쓴다.

사용: python3 -B gen_vectors.py <엔진 폴더> <출력 vectors.js>
엔진은 import만 한다. 파일·원장·API에 접근하지 않는다.
"""
import json
import random
import sys
from decimal import Decimal

engine_dir, out_path = sys.argv[1], sys.argv[2]
sys.path.insert(0, engine_dir)
import leader_core as lc  # noqa: E402

rng = random.Random(20261111)
AS_OF = '2026-11-10T16:00:00+09:00'
AV = '2026-11-10T15:40:00+09:00'
V = {}

# 1. 백분위
cases = []
for _ in range(500):
    n = rng.randint(0, 12)
    pool = [rng.choice([None, 0.0, 1.0, -1.0, 2.5, rng.uniform(-50, 50), rng.uniform(-50, 50)]) for _ in range(n)]
    mode = rng.choice(['book_formula', 'endpoint_adjusted'])
    ties = rng.choice(['average', 'min'])
    hib = rng.choice([True, False])
    cases.append({'values': pool, 'mode': mode, 'ties': ties, 'hib': hib,
                  'expect': lc.percent_rank(pool, mode=mode, ties=ties, higher_is_better=hib)})
V['percent_rank'] = cases


# 2. 섹터 ETF
def rnd_ret():
    return rng.choice([None, 0.0, 1.5, -1.5, rng.uniform(-30, 30), rng.uniform(-30, 30), rng.uniform(-30, 30)])


cases = []
for _ in range(5000):
    etf = {k: rnd_ret() for k in ('1m', '3m', '6m')}
    bench = {k: rnd_ret() for k in ('1m', '3m', '6m')}
    if rng.random() < .45:
        etf = {k: rng.uniform(-30, 30) for k in etf}
        bench = {k: etf[k] - rng.choice([0, 1, 2, 3, -1]) for k in etf}
    trend = rng.choice([True, True, True, False, None])
    eps = rng.choice([None, 0.0, 2.0, -2.0, rng.uniform(-5, 5)])
    loss = rng.choice([None, 0.0, 1.0, -1.0, rng.uniform(-5, 5)])
    rw = rng.choice([1, 2, None])
    cw = rng.choice([1, 2, 3, 4, None])
    days = (rw or 2) * 5
    prev = (cw or 4) * 5
    n = rng.choice([0, days, days + prev - 1, days + prev, days + prev, days + prev, days + prev + 7])
    base = rng.choice([0.0, 100.0, 950.0, rng.uniform(1, 1000)])
    mult = rng.choice([0.5, 1.0, 1.1, 1.2, 1.3, rng.uniform(.5, 2)])
    style = rng.choice(['flat', 'flat', 'noisy'])
    turnover = []
    for i in range(n):
        v = base * (mult if i >= n - days else 1.0)
        if style == 'noisy':
            v *= rng.uniform(.8, 1.2)
        turnover.append(v)
    if turnover and rng.random() < .06:
        turnover[rng.randrange(len(turnover))] = rng.choice([None, -1.0])

    def flows():
        m = rng.choice([0, days - 1, days, days, days, days + 3])
        kind = rng.choice(['pos', 'pos', 'pos', 'zero', 'neg', 'mixed', 'none'])
        arr = []
        for _i in range(m):
            if kind == 'pos':
                arr.append(rng.uniform(1, 100))
            elif kind == 'zero':
                arr.append(rng.choice([0.0, rng.uniform(1, 100)]))
            elif kind == 'neg':
                arr.append(rng.choice([-5.0, rng.uniform(1, 100)]))
            elif kind == 'mixed':
                arr.append(rng.uniform(-100, 100))
            else:
                arr.append(rng.choice([None, 1.0]))
        return arr

    kwargs = dict(etf_returns=etf, benchmark_returns=bench, trend_1m_unbroken=trend,
                  sector_eps_revision=eps, loss_company_ratio_change=loss,
                  turnover_history=turnover, foreign_net_buys=flows(), institutional_net_buys=flows(),
                  recent_weeks=rw, comparison_weeks=cw,
                  delta_aum=rng.choice([None, 10.0, rng.uniform(-100, 100)]),
                  price_effect=rng.choice([None, 4.0, rng.uniform(-100, 100)]),
                  price_basis=rng.choice(['adjusted', 'adjusted', 'unadjusted']), allow_unadjusted=rng.choice([True, False]))
    out = lc.evaluate_sector_etf(**kwargs)
    cases.append({'args': kwargs, 'expect': {k: out[k] for k in (
        'signals', 'action', 'turnover_ratio', 'relative_returns', 'net_inflow', 'structure_only')}})

# 2-b. 섹터 ETF, 입력이 모두 있는 경우 (판정 다섯 가지가 고르게 나오도록)
for _ in range(4000):
    rw = rng.choice([1, 2])
    cw = rng.choice([2, 4, 4])
    days, prev = rw * 5, cw * 5
    etf = {k: round(rng.uniform(-10, 30), 1) for k in ('1m', '3m', '6m')}
    gap = rng.choice(['ahead', 'ahead', 'ahead', 'equal', 'behind'])
    bench = {k: round(etf[k] - (rng.uniform(0.1, 8) if gap == 'ahead' else 0), 1) for k in etf}
    if gap == 'behind':
        bench[rng.choice(['1m', '3m', '6m'])] = round(etf['1m'] + 40, 1)
    base = rng.choice([100.0, 950.0, 0.1, rng.uniform(1, 1000)])
    mult = rng.choice([0.9, 1.0, 1.19, 1.2, 1.2, 1.21, 1.3, 1.3, rng.uniform(.5, 2)])
    noisy = rng.random() < .3
    turnover = [base * (mult if i >= prev else 1.0) * (rng.uniform(.8, 1.2) if noisy else 1.0) for i in range(days + prev)]

    def full_flows():
        kind = rng.choice(['pos', 'pos', 'pos', 'pos', 'zero', 'neg'])
        arr = [rng.uniform(1, 100) for _i in range(days)]
        if kind == 'zero':
            arr[rng.randrange(days)] = 0.0
        if kind == 'neg':
            arr[rng.randrange(days)] = -3.0
        return arr

    kwargs = dict(etf_returns=etf, benchmark_returns=bench, trend_1m_unbroken=rng.choice([True, True, True, True, False]),
                  sector_eps_revision=rng.choice([2.0, 2.0, 2.0, 0.0, -1.0]), loss_company_ratio_change=rng.choice([-1.0, -1.0, -1.0, 0.0, 1.5]),
                  turnover_history=turnover, foreign_net_buys=full_flows(), institutional_net_buys=full_flows(),
                  recent_weeks=rw, comparison_weeks=cw, delta_aum=rng.uniform(-100, 100), price_effect=rng.uniform(-100, 100),
                  price_basis=rng.choice(['adjusted', 'unadjusted']), allow_unadjusted=True)
    out = lc.evaluate_sector_etf(**kwargs)
    cases.append({'args': kwargs, 'expect': {k: out[k] for k in (
        'signals', 'action', 'turnover_ratio', 'relative_returns', 'net_inflow', 'structure_only')}})
V['sector'] = cases


# 3. COMP
def prices(n):
    if n == 0:
        return [], []
    close = [rng.uniform(1000, 50000)]
    for _ in range(n - 1):
        close.append(max(1.0, close[-1] * (1 + rng.gauss(0.0005, 0.03))))
    high = [x * (1 + abs(rng.gauss(0, 0.01))) for x in close]
    return close, high


cases = []
for _ in range(400):
    m = rng.randint(1, 9)
    panel = []
    for i in range(m):
        close, high = prices(rng.choice([0, 4, 6, 21, 61, 126, 261, 261, 261, 300]))
        panel.append({
            'symbol': 'S%d' % i, 'available_at': AV, 'data_date': '2026-11-10', 'price_basis': 'adjusted',
            'adjusted_close': close, 'adjusted_high': high,
            'market_cap': rng.choice([None, 0.0, 1e11, rng.uniform(1e10, 1e13), rng.uniform(1e10, 1e13)]),
            'flow_basis': rng.choice(['amount', 'amount', 'amount', 'amount', 'quantity']),
            'net_buy_amount_20d': rng.choice([None, 0.0, 1e9, rng.uniform(-1e10, 1e10), rng.uniform(-1e10, 1e10)]),
            'foreign_holding_rate': rng.choice([None, 10.0, 10.0, 25.0, rng.uniform(0, 60), rng.uniform(0, 60)]),
            'equity': rng.choice([None, 0.0, -1e9, 1e11, rng.uniform(1e9, 1e12), rng.uniform(1e9, 1e12)]),
            'net_income_ttm': rng.choice([None, 1e9, rng.uniform(-1e10, 1e11), rng.uniform(-1e10, 1e11)]),
            'eps_ttm': rng.choice([None, 1000.0, rng.uniform(-3000, 9000), rng.uniform(-3000, 9000)]),
            'operating_profit_ttm': rng.choice([None, rng.uniform(-1e10, 1e11), rng.uniform(-1e10, 1e11)]),
            'operating_profit_prior_ttm': rng.choice([None, 0.0, rng.uniform(-1e10, 1e11), rng.uniform(-1e10, 1e11)]),
            'sales_prior_ttm': rng.choice([None, 1e11, rng.uniform(1e9, 1e12), rng.uniform(1e9, 1e12)])})
    if m >= 3 and rng.random() < .3:
        panel[1]['adjusted_close'] = list(panel[0]['adjusted_close'])
        panel[1]['adjusted_high'] = list(panel[0]['adjusted_high'])
    regime = rng.choice(['neutral', 'neutral', 'growth_bull', 'high_rate_value', 'post_crash'])
    pen = rng.choice([None, None, None, 0.0, 0.5])
    out = lc.compute_comp(panel, as_of=AS_OF, universe_id='t', regime=regime, rank_dispersion_penalty=pen)
    cases.append({'regime': regime, 'penalty': pen, 'raws': [r['raw'] for r in out['results']],
                  'expect': [{k: r[k] for k in ('COMP', 'COMP_penalty_off', 'components', 'input_percentiles',
                                                'grade', 'missing_components')} for r in out['results']]})

# 3-b. COMP, 입력이 거의 다 있는 패널 (등급 네 가지가 나오도록)
for _ in range(400):
    m = rng.randint(2, 12)
    panel = []
    for i in range(m):
        close, high = prices(rng.choice([261, 261, 300]))
        drift = rng.uniform(-0.6, 1.5)
        close = [c * (1 + drift * j / len(close)) for j, c in enumerate(close)]
        high = [max(h, c) for h, c in zip(high, close)]
        gone = lambda v: None if rng.random() < .03 else v
        panel.append({
            'symbol': 'S%d' % i, 'available_at': AV, 'data_date': '2026-11-10', 'price_basis': 'adjusted',
            'adjusted_close': close, 'adjusted_high': high,
            'market_cap': gone(rng.uniform(1e10, 1e13)), 'flow_basis': 'amount',
            'net_buy_amount_20d': gone(rng.uniform(-1e10, 1e10)),
            'foreign_holding_rate': gone(rng.choice([10.0, 25.0, rng.uniform(0, 60), rng.uniform(0, 60)])),
            'equity': gone(rng.choice([-1e9, rng.uniform(1e9, 1e12), rng.uniform(1e9, 1e12), rng.uniform(1e9, 1e12)])),
            'net_income_ttm': gone(rng.uniform(-1e10, 1e11)), 'eps_ttm': gone(rng.uniform(-3000, 9000)),
            'operating_profit_ttm': gone(rng.uniform(-1e10, 1e11)),
            'operating_profit_prior_ttm': gone(rng.uniform(1e9, 1e11)), 'sales_prior_ttm': gone(rng.uniform(1e9, 1e11))})
    regime = rng.choice(['neutral', 'neutral', 'growth_bull', 'high_rate_value', 'post_crash'])
    pen = rng.choice([None, None, None, 0.0])
    out = lc.compute_comp(panel, as_of=AS_OF, universe_id='t', regime=regime, rank_dispersion_penalty=pen)
    cases.append({'regime': regime, 'penalty': pen, 'raws': [r['raw'] for r in out['results']],
                  'expect': [{k: r[k] for k in ('COMP', 'COMP_penalty_off', 'components', 'input_percentiles',
                                                'grade', 'missing_components')} for r in out['results']]})
V['comp'] = cases


# 4. F 원점수
def fin_row():
    sales = rng.choice([None, 0.0, 1000.0, rng.uniform(100, 1e5), rng.uniform(100, 1e5), rng.uniform(100, 1e5)])
    return {'sales': sales,
            'cogs': rng.choice([None, rng.uniform(0, 9e4), sales * 0.7 if sales else None, sales * 0.7 if sales else None]),
            'sga': rng.choice([None, rng.uniform(0, 3e4), sales * 0.1 if sales else None, sales * 0.1 if sales else None]),
            'assets': rng.choice([None, 0.0, rng.uniform(100, 2e5), rng.uniform(100, 2e5), rng.uniform(100, 2e5)]),
            'operating_profit': rng.choice([None, rng.uniform(-1e4, 1e4), rng.uniform(-1e4, 1e4)]),
            'operating_cashflow': rng.choice([None, rng.uniform(-1e4, 1e4), rng.uniform(-1e4, 1e4)]),
            'available_at': AV}


cases = []
for _ in range(4000):
    cur, prev = fin_row(), fin_row()
    if rng.random() < .15:
        prev = dict(cur)
    excl = rng.random() < .05
    mat = rng.random() < .4
    out = lc.compute_korean_f_score(cur, prev, as_of_date=AS_OF, excluded_financial=excl, material=mat)
    cases.append({'current': cur, 'previous': prev, 'excluded': excl, 'material': mat,
                  'expect': {k: out.get(k) for k in ('F_core', 'items', 'accrual_ok', 'f_raw_book', 'reason', 'delta_gpm_pp')}})

# 4-b. F 원점수, 입력이 모두 있는 경우
for _ in range(4000):
    def full_row():
        sales = rng.uniform(1000, 1e5)
        return {'sales': sales, 'cogs': sales * rng.uniform(.5, .95), 'sga': sales * rng.uniform(.02, .3),
                'assets': rng.uniform(1000, 2e5), 'operating_profit': rng.uniform(-1e4, 1e4),
                'operating_cashflow': rng.choice([None, rng.uniform(-1e4, 1e4), rng.uniform(-1e4, 1e4)]), 'available_at': AV}
    cur, prev = full_row(), full_row()
    mat = rng.random() < .4
    out = lc.compute_korean_f_score(cur, prev, as_of_date=AS_OF, excluded_financial=False, material=mat)
    cases.append({'current': cur, 'previous': prev, 'excluded': False, 'material': mat,
                  'expect': {k: out.get(k) for k in ('F_core', 'items', 'accrual_ok', 'f_raw_book', 'reason', 'delta_gpm_pp')}})
V['f_score'] = cases

# 5. F 복합
cases = []
for _ in range(500):
    m = rng.randint(1, 9)
    rows = [{'symbol': 'S%d' % i, 'available_at': AV, 'price_basis': 'adjusted',
             'F_core': rng.choice([None, 0, 1, 2, 3, 4, 4, 3]),
             'ret_1m': rng.choice([None, 5.0, rng.uniform(-30, 30), rng.uniform(-30, 30)]),
             'ret_3m': rng.choice([None, 5.0, rng.uniform(-30, 60), rng.uniform(-30, 60)]),
             'delta_gpm_pp': rng.choice([None, 0.0, rng.uniform(-.1, .1), rng.uniform(-.1, .1)]),
             'material': rng.random() < .3, 'accrual_ok': rng.choice([None, True, False])} for i in range(m)]
    regime = rng.choice(['neutral', 'neutral', 'bear_market', 'bull_market', 'cost_pressure'])
    out = lc.compute_f_composite(rows, as_of=AS_OF, universe_id='t', regime=regime)
    cases.append({'regime': regime, 'rows': rows,
                  'expect': [{k: r[k] for k in ('F_core', 'F_comp', 'percentiles', 'grade')} for r in out['results']]})
V['f_composite'] = cases

# 6~8. 등급 격자
V['f_grade'] = [{'f_core': fc, 'f_comp': sc, 'material': mat, 'accrual_ok': acc,
                 'expect': lc.f_grade(fc, sc, material=mat, accrual_ok=acc)}
                for fc in (None, 0, 1, 2, 3, 4)
                for sc in (None, 0.0, 49.999, 50.0, 59.999, 60.0, 69.999, 70.0, 100.0)
                for mat in (False, True) for acc in (None, True, False)]
V['cross'] = [{'comp': c, 'f_core': fc, 'expect': lc.cross_validate_models(c, fc)}
              for c in (None, 0.0, 79.999, 80.0, 80.001, 100.0) for fc in (None, 0, 1, 2, 3, 4)]
V['comp_grade'] = [{'score': s, 'expect': lc.comp_grade(s)}
                   for s in (None, -1.0, 0.0, 64.999, 65.0, 79.999, 80.0, 89.999, 90.0, 100.0, 100.1)]


# 9. 손익비
def dec_str(lo, hi, places):
    n = rng.randint(lo * 10 ** places, hi * 10 ** places)
    whole, frac = divmod(n, 10 ** places)
    return str(whole) + ('.' + str(frac).zfill(places) if places else '')


def plain(d):
    s = format(d, 'f')
    return s.rstrip('0').rstrip('.') if '.' in s else s


cases = []
for _ in range(3000):
    ps = dec_str(100, 500000, rng.choice([0, 0, 1, 2]))
    ls = dec_str(1, 15, rng.choice([0, 0, 1, 2]))
    P, L = Decimal(ps), Decimal(ls)
    thr = max(Decimal('0.21'), 3 * L / 100)
    edge = P * (1 + thr)
    kind = rng.choice(['edge', 'below', 'above', 'random', 'random', 'loss'])
    if kind == 'edge':
        T = edge
    elif kind == 'below':
        T = edge - Decimal('0.000001')
    elif kind == 'above':
        T = edge + Decimal('0.000001')
    elif kind == 'loss':
        T = P * Decimal(dec_str(0, 1, 3))
    else:
        T = P * (1 + Decimal(dec_str(0, 1, 4)))
    ts = plain(T)
    pos = lc.make_entry_position(float(ps), entered_at=AV, stop_loss_pct=float(L / 100))
    out = lc.risk_reward_gate(float(ps), float(ts), position=pos)
    cases.append({'price': ps, 'target': ts, 'stop_pct': ls,
                  'expect': {k: out[k] for k in ('pass', 'upside', 'reward_to_risk', 'minimum_upside', 'fixed_stop_loss_pct')}})
V['gate'] = cases

with open(out_path, 'w') as handle:
    handle.write('var VECTORS = ')
    json.dump(V, handle, allow_nan=False)
    handle.write(';\n')
print({k: len(v) for k, v in V.items()})
