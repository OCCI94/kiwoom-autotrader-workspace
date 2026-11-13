"""S5 observed five-session short-selling evidence; no transport or I/O.

Uses the official shortSellingAmountRate fraction (0..1). Its denominator is
all-day turnover including extended sessions; it is not reverse-engineered
from the amount. Any of the five observed session rates above10% holds entry.
"""
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
import re

SCHEMA='s5-short-selling-v1'
SOURCE_API='GET /api/v1/stocks/{symbol}/short-selling'


def _time(value):
    try:
        stamp=datetime.fromisoformat(value) if isinstance(value,str) else value
        if isinstance(stamp,datetime) and stamp.utcoffset()==timedelta(hours=9):return stamp
    except (ValueError,TypeError):pass
    raise ValueError('invalid_time')


def _day(value):
    if not isinstance(value,str) or re.fullmatch('[0-9]{4}-[0-9]{2}-[0-9]{2}',value) is None:
        raise ValueError('invalid_day')
    return datetime.strptime(value,'%Y-%m-%d').date()


def _sha(value):
    return isinstance(value,str) and re.fullmatch('[0-9a-f]{64}',value) is not None


def validate_short_selling(context, *, symbol, now, expected_session_days):
    result={'eligible':False,'status':'UNAVAILABLE','reason':'short_selling_evidence_unavailable',
            'observed_session_count':0,'max_daily_short_amount_share':None,
            'latest_short_amount_share':None,'over_limit_sessions':[],
            'ratio_denominator':'all_day_including_extended_sessions'}
    try:
        now=_time(now)
        if (not isinstance(context,dict) or context.get('schema')!=SCHEMA
                or context.get('symbol')!=symbol or context.get('source_api')!=SOURCE_API
                or not _sha(context.get('source_sha256')) or not _sha(context.get('calendar_sha256'))):
            raise ValueError('identity')
        start,received,expires=(_time(context.get(k)) for k in ('request_started_at','observed_at','expires_at'))
        today=_day(context.get('as_of_session_day'))
        previous=_day(context.get('previous_session_day'))
        if (not start<=received<=now<expires or expires-received>timedelta(hours=24)
                or received-start>timedelta(minutes=5) or today!=now.date() or previous>=today):
            raise ValueError('stale_or_future')
        if not isinstance(expected_session_days,(tuple,list)) or len(expected_session_days)!=5:
            raise ValueError('expected_sessions')
        expected=list(expected_session_days)
        dates=[_day(v) for v in expected]
        if (dates!=sorted(set(dates)) or any(d>=today for d in dates) or dates[-1]!=previous
                or context.get('expected_session_days')!=expected):raise ValueError('sessions')
        rows=context.get('rows')
        if not isinstance(rows,list) or len(rows)!=5:raise ValueError('missing_sessions')
        rates=[];over=[]
        for row,date in zip(rows,expected):
            if not isinstance(row,dict) or row.get('session_day')!=date:raise ValueError('session_order')
            updated=_time(row.get('updated_at'))
            if not _day(date)<=updated.date() or updated>received:raise ValueError('future_source')
            if any(type(row.get(k)) is not int or not 0<=row[k]<10**20
                   for k in ('short_selling_amount_krw','short_selling_volume')):raise ValueError('amount')
            value=row.get('short_amount_share')
            if not isinstance(value,str) or len(value)>40:raise ValueError('rate')
            rate=Decimal(value)
            if not rate.is_finite() or not 0<=rate<=1:raise ValueError('rate')
            rates.append(rate)
            if rate>Decimal('0.10'):over.append(date)
        result.update(eligible=not over,status='PASS' if not over else 'BLOCK',
            reason='short_selling_clear' if not over else 'short_selling_pressure_high',
            observed_session_count=5,max_daily_short_amount_share=str(max(rates)),
            latest_short_amount_share=str(rates[-1]),over_limit_sessions=over,
            previous_session_day=previous.isoformat(),source_sha256=context['source_sha256'],
            calendar_sha256=context['calendar_sha256'],observed_at=received.isoformat(),expires_at=expires.isoformat())
    except (ValueError,TypeError,KeyError,OverflowError,InvalidOperation):pass
    return result
