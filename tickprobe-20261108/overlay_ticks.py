"""watchdog tick_health(틱 시작/완료) × 실험 store 영수증(selections) 겹치기.
읽기 전용. 인자: store 복사본 경로 [날짜...]. 출력: 틱별 pre/batch/post 초.
사용: python3 overlay_ticks.py <store-copy.sqlite3> 20261104 20261105 20261106
"""
import json,glob,sqlite3,statistics as st,collections,sys
from datetime import datetime
W='/Users/tr-oh/Library/Application Support/KiwoomAutotrader/observability/watchdog/'
S=sys.argv[1]; DAYS=sys.argv[2:] or ('20261104','20261105','20261106')
c=sqlite3.connect(f'file:{S}?mode=ro',uri=True)
def ticks(day):
    seen={}
    for p in sorted(glob.glob(W+f'{day}-*.sample.json')):
        try: th=(json.load(open(p)).get('tick_health') or {})
        except Exception: continue
        s,e=th.get('last_tick_started_at'),th.get('last_tick_completed_at')
        if s: seen.setdefault(s,set()).add(e)
    return [(s,min([e for e in es if e and e>s],default=None)) for s,es in sorted(seen.items())]
def receipts(day):
    out=[]
    for k,p in c.execute("select kind,payload from selections where day=?",(day,)):
        pre=k.split(':')[0]
        if pre not in ('evaluated','evaluated_exit','s5_evaluated'): continue
        d=json.loads(p); t=d.get('observed_at') or d.get('evaluated_at')
        if t: out.append((datetime.fromisoformat(t),pre,k.split(':')[2]))
    return sorted(out)
for day in DAYS:
    T=ticks(day); R=receipts(day)
    print(f'\n##### {day}: ticks={len(T)} receipts={len(R)} {dict(collections.Counter(r[1] for r in R))}')
    pre_b=[];post_b=[];durs=[];batch=[]
    for s,e in T:
        if not e or not ('09:10'<=s[11:16]<'15:10'): continue
        sd,ed=datetime.fromisoformat(s),datetime.fromisoformat(e); dur=(ed-sd).total_seconds()
        ins=[r for r in R if sd<=r[0]<=ed]
        if not ins: continue
        f=(ins[0][0]-sd).total_seconds(); l=(ins[-1][0]-sd).total_seconds()
        durs.append(dur);pre_b.append(f);post_b.append(dur-l);batch.append(l-f)
    if durs:
        q=lambda v,p: sorted(v)[int(len(v)*p)]
        print(f'  09:10~15:10 ticks n={len(durs)}: dur med={st.median(durs):.1f} p90={q(durs,.9):.1f} | pre-batch med={st.median(pre_b):.1f} p90={q(pre_b,.9):.1f} | batch span med={st.median(batch):.1f} | post med={st.median(post_b):.1f}')
