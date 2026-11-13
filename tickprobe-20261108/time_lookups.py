"""원장 복사본에서 실제 코드(execution_log.ExecutionTracker)로 소유권 조회 비용을 잰다.
봇의 파이썬(3.12)으로 실행. 네트워크 fetcher는 모두 예외를 던지는 스텁(호출 횟수만 센다).
사용: <bot-python> time_lookups.py <ledger-dir(.execution-ledger.sqlite3 포함)> [YYYY-MM-DDTHH:MM]
복사본 경로만 받는다. 운영 원장 경로를 넘기지 말 것.
"""
import sys,time,sqlite3,cProfile,pstats,io
from datetime import datetime,timezone,timedelta
from pathlib import Path
import os; sys.path.insert(0,os.environ.get('KA_ROOT','/Users/tr-oh/Library/Application Support/KiwoomAutotrader/runtime'))
from kiwoom_autotrader.trading import execution_log as EL
from kiwoom_autotrader.config.settings import load_settings
KST=timezone(timedelta(hours=9))
D=Path(sys.argv[1]).resolve()
assert '.execution-ledger' not in str(D) and 'KiwoomAutotrader/runtime/logs' not in str(D), '운영 원장 금지'
now=datetime.fromisoformat(sys.argv[2]).replace(tzinfo=KST) if len(sys.argv)>2 else datetime(2026,11,5,16,30,tzinfo=KST)
calls={'account':0,'exec':0,'profit':0}
def stub(k):
    def f(*a,**kw): calls[k]+=1; raise RuntimeError('no network')
    return f
settings=load_settings()
tr=EL.ExecutionTracker(log_dir=D,now=lambda:now,settings_loader=lambda:settings,account_fetcher=stub('account'),executions_fetcher=stub('exec'),profit_fetcher=stub('profit'))
real=sqlite3.connect; cnt={'n':0}
def cc(*a,**k): cnt['n']+=1; return real(*a,**k)
sqlite3.connect=cc
hyd={'n':0}
import kiwoom_autotrader.trading.broker_observation_repeats as BR
_h=BR._hydrate
def h(*a,**k): hyd['n']+=1; return _h(*a,**k)
BR._hydrate=h
def timed(label,fn):
    cnt['n']=0; hyd['n']=0; t0=time.perf_counter()
    try: r=fn(); s=r.get('status') if isinstance(r,dict) else r
    except Exception as e: s=f'{type(e).__name__}:{str(e)[:80]}'
    print(f'{(time.perf_counter()-t0)*1000:9.1f} ms conns={cnt["n"]:2d} hydrate_rows={hyd["n"]:5d} fetch={dict(calls)} {label} -> {s}')
r=timed('multi_strategy_open_symbols', lambda:tr.multi_strategy_open_symbols('paper',now))
with real(D.joinpath('.execution-ledger.sqlite3').as_uri()+'?mode=ro',uri=True) as db:
    syms=[x[0] for x in db.execute("select distinct symbol from intents where mode='paper' and day>='20261103' order by symbol")]
total=0
for s in syms:
    t0=time.perf_counter(); timed(f'  position {s} EXP_D', lambda:tr.multi_strategy_position('paper',s,'EXP_D',now)); total+=time.perf_counter()-t0
print(f'TOTAL {len(syms)} symbols: {total:.1f} s')
