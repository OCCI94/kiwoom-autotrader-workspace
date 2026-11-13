"""공개 시세(네이버 금융 siseJson)에서 섹터 ETF 33종과 지수 ETF 2종의 일봉을 받는다.

증권사 API를 쓰지 않는다. 보내는 것은 종목 코드와 날짜 범위뿐이다.
사용: python3 -I -B fetch_naver_etf.py <cohort json> <raw 저장 폴더> <합본 json>
"""
import json, os, sys, time, urllib.request, ast, re

cohort_path, raw_dir, out_path = sys.argv[1], sys.argv[2], sys.argv[3]
START, END = '20211101', '20261111'
URL = 'https://api.finance.naver.com/siseJson.naver?symbol={sym}&requestType=1&startTime={a}&endTime={b}&timeframe=day'

cohort = json.load(open(cohort_path))
products = []
for sec in cohort['sectors']:
    for p in sec['products']:
        products.append((p.get('symbol') or p.get('code'), sec.get('sector_id') or sec.get('id'), p.get('name')))
products += [('069500', '_market_kospi200', 'KODEX 200'), ('229200', '_market_kosdaq150', 'KODEX 코스닥150')]

os.makedirs(raw_dir, exist_ok=True)
t0 = time.time(); result = {}; log = []
for sym, sector, name in products:
    if time.time() - t0 > 240:
        log.append((sym, 'TIME BUDGET')); break
    raw_path = os.path.join(raw_dir, sym + '.txt')
    try:
        req = urllib.request.Request(URL.format(sym=sym, a=START, b=END), headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=20) as r:
            body = r.read()
        open(raw_path, 'wb').write(body)
        text = body.decode('utf-8', errors='replace')
        rows = []
        for m in re.finditer(r'\[\s*"(\d{8})"\s*,([^\]]*)\]', text):
            parts = [x.strip() for x in m.group(2).split(',')]
            try:
                o, h, l, c, v = (float(parts[0]), float(parts[1]), float(parts[2]), float(parts[3]), float(parts[4]))
            except Exception:
                continue
            rows.append([m.group(1), o, h, l, c, v])
        rows.sort()
        result[sym] = {'sector': sector, 'name': name, 'rows': rows}
        log.append((sym, len(body), len(rows), rows[0][0] if rows else None, rows[-1][0] if rows else None))
    except Exception as e:
        log.append((sym, 'ERROR', repr(e)[:120]))
    time.sleep(0.4)
json.dump({'source': 'api.finance.naver.com siseJson', 'fetched_at': time.strftime('%Y-%m-%d %H:%M KST'), 'start': START, 'end': END,
           'products': result}, open(out_path, 'w'), ensure_ascii=False)
for item in log:
    print(*item)
print('products', len(result), 'seconds', round(time.time() - t0, 1), 'total raw bytes', sum(os.path.getsize(os.path.join(raw_dir, f)) for f in os.listdir(raw_dir)))
