"""Synthetic completed-session evidence only; no network or runtime files."""
from datetime import datetime, timedelta

def short_evidence(symbol, daily_context, now, rate='0.02'):
    days=[datetime.strptime(row['date'],'%Y%m%d').date().isoformat() for row in daily_context['rows'][-5:]]
    return {'schema':'s5-short-selling-v1','symbol':symbol,
        'source_api':'GET /api/v1/stocks/{symbol}/short-selling',
        'as_of_session_day':now.date().isoformat(),'previous_session_day':days[-1],
        'request_started_at':(now-timedelta(seconds=1)).isoformat(),'observed_at':now.isoformat(),
        'expires_at':(now+timedelta(hours=8)).isoformat(),'calendar_sha256':'e'*64,'source_sha256':'f'*64,
        'expected_session_days':days,
        'rows':[{'session_day':day,'updated_at':day+'T20:00:00+09:00',
                 'short_selling_amount_krw':100000,'short_selling_volume':10,'short_amount_share':rate} for day in days]}
