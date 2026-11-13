"""Bounded actual-observation windows; never manufacture a missing candle.

The policy is a count of broker-returned bars, not a calendar-minute SMA.
Up to two missing minutes are accepted only inside one complete response-page
sequence. Their cause remains unknown; omitted minutes never acquire volume 0.
"""
from datetime import datetime, timedelta
import re

POLICY = 'actual_observed_bars_max_two_missing_minutes_v1'


def timeline_rows(bars):
    return [{'start': b.start.isoformat(), 'batch': b.source_batch,
             'page': b.source_page, 'row': b.source_row,
             'response_sha256': b.source_response_sha256,
             'query_started_at': b.source_query_started_at.isoformat()}
            for b in bars]


def validate_timeline(rows, now, *, require_latest=True):
    """Validate serialized provenance, returning the number of absent minutes."""
    if not isinstance(rows, list) or len(rows) > 61 or not isinstance(now, datetime) or now.utcoffset() != timedelta(hours=9):
        raise ValueError('observed_timeline_invalid')
    starts = []
    for row in rows:
        if not isinstance(row, dict) or set(row) != {'start', 'batch', 'page', 'row', 'response_sha256', 'query_started_at'}:
            raise ValueError('observed_timeline_invalid')
        start, query = datetime.fromisoformat(row['start']), datetime.fromisoformat(row['query_started_at'])
        if (start.utcoffset() != timedelta(hours=9) or query.utcoffset() != timedelta(hours=9)
                or start.second or start.microsecond or start.date() != now.date()
                or not (9, 0) <= (start.hour, start.minute) < (15, 30)
                or not start + timedelta(minutes=1) <= query <= now
                or now - query > timedelta(seconds=60)
                or not isinstance(row['response_sha256'], str)
                or re.fullmatch(r'[0-9a-f]{64}', row['response_sha256']) is None):
            raise ValueError('observed_timeline_invalid')
        starts.append(start)
    gaps = [(b-a).total_seconds()/60 - 1 for a,b in zip(starts, starts[1:])]
    if any(g < 0 or g != int(g) for g in gaps) or sum(gaps) > 2:
        raise ValueError('observed_timeline_gap')
    if starts and require_latest and starts[-1] != now.replace(second=0,microsecond=0)-timedelta(minutes=1):
        raise ValueError('observed_timeline_stale')
    missing = int(sum(gaps))
    if missing:
        first = rows[0]
        if (not isinstance(first['batch'],str) or not first['batch'] or len(first['batch']) > 128
                or type(first['page']) is not int or first['page'] < 0
                or any(type(r['row']) is not int or r['row'] < 0 for r in rows)
                or any(any(r[k] != first[k] for k in ('batch','page','response_sha256','query_started_at')) for r in rows)
                # ka10080 source rows are newest first. Adjacent raw rows prove
                # that the client did not remove a real candle between them.
                or any(a['row']-b['row'] != 1 for a,b in zip(rows,rows[1:]))):
            raise ValueError('observed_timeline_gap_provenance_unavailable')
    return missing


def actual_window(bars, symbol, now, count, *, require_ohlcv=False):
    if not isinstance(bars,(list,tuple)) or len(bars) < count or count not in (20,21):
        raise ValueError('observed_window_insufficient')
    recent = tuple(bars[-count:])
    for b in recent:
        if (b.source_symbol != symbol or b.source_mode != 'paper' or b.gap_proof is not None
                or b.adjustment not in (None,'') or b.adjustment_rate not in (None,'')
                or type(b.close) is not int or b.close <= 0):
            raise ValueError('observed_window_source_invalid')
        if require_ohlcv and (any(type(v) is not int or v <= 0 for v in (b.open_price,b.high_price,b.low_price))
                or not b.low_price <= min(b.open_price,b.close) <= max(b.open_price,b.close) <= b.high_price
                or type(b.volume) is not int or not 0 <= b.volume < 10**19):
            raise ValueError('observed_window_ohlcv_invalid')
    validate_timeline(timeline_rows(recent),now)
    return recent


def window_policy(bars, now, *, require_latest=True):
    rows = timeline_rows(bars)
    missing = validate_timeline(rows,now,require_latest=require_latest)
    return {'policy':POLICY, 'bar_count':len(rows), 'missing_minutes':missing,
            'missing_cause':'unknown' if missing else None, 'synthetic_bars_created':False,
            'sma_basis':'last_20_actual_observed_closes',
            'volume_basis':'previous_20_actual_observed_volumes', 'timeline':rows}
