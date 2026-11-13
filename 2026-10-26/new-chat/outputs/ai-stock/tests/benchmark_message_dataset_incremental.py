"""Offline synthetic benchmark; prints timings and hashes, never message content.

Usage: python tests/benchmark_message_dataset_incremental.py [version-count]
A temporary database and two temporary output roots are destroyed at exit.
"""
import hashlib
import json
from pathlib import Path
import shutil
import resource
import subprocess
import sys
import tempfile
from time import perf_counter
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from kiwoom_autotrader.telegram.channel_store import ChannelStore
from kiwoom_autotrader.utils.message_dataset import export_daily_dataset
from kiwoom_autotrader.utils.message_dataset_incremental import _STATES

T = 1793000000
N = int(sys.argv[1]) if len(sys.argv)>1 else 20000
if not 8 <= N <= 1000000: raise ValueError('synthetic count out of bounds')
with tempfile.TemporaryDirectory(prefix='message-incremental-bench-') as temporary:
    base=Path(temporary); store=ChannelStore(base/'db',123)
    store.register_channel(100,200,'Synthetic','synthetic')
    # Approximate the measured five-day availability distribution, with only
    # seven newest observations; published dates are synthetic ten-day spread.
    limits=[int((N-7)*fraction) for fraction in (0.569,0.875,0.951,1.0)]+[N]
    start=0
    for day,limit in enumerate(limits):
        with patch('kiwoom_autotrader.telegram.channel_store.time.time',return_value=T-(4-day)*86400):
            while start<limit:
                end=min(start+1000,limit)
                store.commit_page(100,[{'message_id':i+1,'published_at':T-(i//1000%10+1)*86400,
                    'text':'synthetic KR:005930 $AAPL'} for i in range(start,end)])
                start=end
    output, reference=base/'output',base/'reference'
    def memory():
        # Darwin ru_maxrss is bytes; current RSS from ps is KiB. No source
        # message data, credentials or process command lines are recorded.
        peak=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        current=subprocess.run(['/bin/ps','-o','rss=','-p',str(__import__('os').getpid())],
                               capture_output=True,text=True,check=True).stdout.strip()
        return {'peak_rss_bytes':peak,'current_rss_kib':int(current)}
    def measure(root,t,explicit=False):
        start=perf_counter()
        with patch('kiwoom_autotrader.utils.message_dataset.time.time',return_value=t):
            result=export_daily_dataset(store,root,t if explicit else None)
        return perf_counter()-start,result
    def hashes(root):
        return {str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest()
                for p in root.rglob('*') if p.is_file()}
    def compare(label,t):
        incremental,result=measure(output,t)
        memory_delta=memory()
        shutil.rmtree(reference,ignore_errors=True)
        full,_=measure(reference,t,True)
        a,b=hashes(output),hashes(reference)
        return {'label':label,'incremental_seconds':round(incremental,6),
            'full_seconds':round(full,6),'incremental_used':result.get('incremental') is True,
            'all_file_hashes_match':a==b,'file_sha256':a,'memory_after_incremental':memory_delta,
            'memory_after_full_comparison':memory()}
    cold,_=measure(output,T)
    report={'synthetic_versions':N,'cold_full_seconds':round(cold,6),
            'state_bytes':(output/'.incremental-export-state.json').stat().st_size,
            'shard_bytes':sum(p.stat().st_size for p in output.glob('.incremental-*.json')
                              if p.name!='.incremental-export-state.json'),
            'memory_after_cold':memory(),'cases':[],
            'note':'Synthetic only; elapsed times are not production latency. Peak RSS includes prior stages.'}
    report['cases'].append(compare('cutoff_advance',T+60))
    store.close();_STATES.clear();store=ChannelStore(base/'db',123)
    report['cases'].append(compare('restart',T+120))
    with patch('kiwoom_autotrader.telegram.channel_store.time.time',return_value=T+180):
        store.commit_page(100,[{'message_id':N+1,'published_at':T-4*86400,'text':'synthetic KR:072950'}])
    report['cases'].append(compare('backfill',T+180))
    with patch('kiwoom_autotrader.telegram.channel_store.time.time',return_value=T+240):
        store.commit_page(100,[{'message_id':1,'published_at':T-86400,'edited_at':T+240,'text':'synthetic $MSFT'}])
    report['cases'].append(compare('edit',T+240))
    store.close()
    print(json.dumps(report,ensure_ascii=False,sort_keys=True))
    if not all(c['incremental_used'] and c['all_file_hashes_match'] for c in report['cases']):
        raise SystemExit('benchmark verification failed')
