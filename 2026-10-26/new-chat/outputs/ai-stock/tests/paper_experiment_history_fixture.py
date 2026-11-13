"""Explicit observed-minute fixtures; never used by the application."""
from datetime import datetime,timedelta
from zoneinfo import ZoneInfo
from kiwoom_autotrader.trading.paper_experiment_volume_history import SCHEMA,digest
KST=ZoneInfo('Asia/Seoul')

def history(daily,symbol,volume=500):
    rows=[]
    for row in daily['rows'][-14:]:
        day=row['date'];start=datetime.strptime(day+'090000','%Y%m%d%H%M%S').replace(tzinfo=KST);observed=start.replace(hour=16)
        minutes=[{'start_at':(start+timedelta(minutes=i)).isoformat(),'end_at':(start+timedelta(minutes=i+1)).isoformat(),
            'available_at':observed.isoformat(),'open':1000,'high':1010,'low':990,'close':1000,'volume':volume,'completed':True} for i in range(5)]
        rows.append({'date':day,'start':'09:00','end':'09:05','available_at':observed.isoformat(),'minutes':minutes,
            'volume':5*volume,'archive_sha256':'1'*64,'manifest_sha256':'2'*64,'wire_sha256':'3'*64})
    value={'schema':SCHEMA,'symbol':symbol,'mode':'paper','exchange':'KRX','expected_sessions':14,'observed_sessions':14,
        'status':'observed','rows':rows,'available_at':rows[-1]['available_at'],'synthetic_bars_created':False,
        'daily_volume_fallback_used':False,'missing_sessions':[],'invalid_sessions':[]}
    value['source_sha256']=digest(value);return value
