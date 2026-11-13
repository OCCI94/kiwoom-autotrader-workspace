"""Pure, fail-closed checks for paper-only B3 scale-in evidence."""
from datetime import datetime, timedelta
import re

from kiwoom_autotrader.trading.cross_monitor import closed_window, CrossMonitorError, sma_difference


class B3EvidenceUnavailable(ValueError):
    pass


def volume_window(bars, *, now, symbol, initial_fill_observed_at):
    """Require 21 actual, completed, consecutive KRX paper bars.

    A gap-inferred candle is useful to a cross baseline but is not a verified
    trade-volume observation. An unconfirmed opening volume is never zero.
    """
    if (not isinstance(symbol, str) or re.fullmatch(r'[0-9]{6}', symbol) is None
            or not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None
            or not isinstance(initial_fill_observed_at, datetime) or initial_fill_observed_at.tzinfo is None
            or initial_fill_observed_at.utcoffset() is None or initial_fill_observed_at > now):
        raise B3EvidenceUnavailable('B3 관측 시각이나 종목을 확인하지 못했습니다.')
    try:
        window = closed_window(bars, now, 20)
    except CrossMonitorError:
        raise B3EvidenceUnavailable('B3 거래량 기준 완료봉을 확인하지 못했습니다.') from None
    if (len(window) != 21 or window[-1].start + timedelta(minutes=1) <= initial_fill_observed_at
            or any(bar.gap_proof is not None or bar.source_mode != 'paper' or bar.source_symbol != symbol
                   or not (9, 0) <= (bar.start.hour, bar.start.minute) < (15, 20)
                   or not isinstance(bar.source_response_sha256, str)
                   or re.fullmatch(r'[0-9a-f]{64}', bar.source_response_sha256) is None
                   or bar.adjustment != '' or bar.adjustment_rate != '' or type(bar.volume) is not int
                   or not 0 <= bar.volume <= 10**19-1 for bar in window)):
        raise B3EvidenceUnavailable('B3 완료봉 거래량 또는 최초 체결 후 시점을 확인하지 못했습니다.')
    prior = sum(bar.volume for bar in window[:-1])
    if prior <= 0:
        raise B3EvidenceUnavailable('B3 이전 20봉 거래량 분모를 확인하지 못했습니다.')
    return window, 40 * window[-1].volume >= 3 * prior


def price_above_basis(price, quantity, principal, bps):
    if (type(price) is not int or not 1 <= price <= 10**12
            or type(quantity) is not int or not 1 <= quantity <= 10**12
            or type(principal) is not int or not 1 <= principal <= 10**15
            or type(bps) is not int or not 1 <= bps <= 10000):
        raise B3EvidenceUnavailable('B3 현재가 또는 보유 매입원금을 확인하지 못했습니다.')
    return price * quantity * 10000 >= principal * (10000 + bps)


def post_fill_trend_is_nonnegative(bars, *, now, symbol, initial_fill_observed_at, short, long):
    """Prove every completed post-fill SMA state, never interpolate a gap."""
    if type(short) is not int or type(long) is not int or not 1 <= short < long <= 60:
        raise B3EvidenceUnavailable('B3 이동평균 기간을 확인하지 못했습니다.')
    # closed_window performs its own identity and closure checks.
    try:
        current = closed_window(bars, now, long)
    except CrossMonitorError:
        raise B3EvidenceUnavailable('B3 체결 이후 이동평균 경로를 확인하지 못했습니다.') from None
    first = initial_fill_observed_at.replace(second=0, microsecond=0)
    latest = current[-1].start
    if first > latest:
        raise B3EvidenceUnavailable('B3 최초 체결 이후 완료봉이 없습니다.')
    # Even if the complete post-fill history cannot be proved, a trustworthy
    # negative window is affirmative evidence and must not later be forgotten.
    available = sorted((b for b in bars if b.start <= latest), key=lambda b: b.start)
    for i in range(long-1, len(available)):
        sample = available[i-long+1:i+1]
        if (sample[-1].start >= first
                and all(b.start == sample[0].start+timedelta(minutes=j) for j,b in enumerate(sample))
                and all(b.source_symbol == symbol and b.source_mode == 'paper' and b.gap_proof is None
                        and b.adjustment == '' and b.adjustment_rate == ''
                        and (9,0) <= (b.start.hour,b.start.minute) < (15,20)
                        and b.source_query_started_at is not None
                        and b.start+timedelta(minutes=1) <= b.source_query_started_at <= now for b in sample)
                and sma_difference(tuple(sample), short, long) < 0):
            return False, ()
    first_needed = first - timedelta(minutes=long-1)
    selected = sorted((b for b in bars if first_needed <= b.start <= latest), key=lambda b: b.start)
    expected = int((latest-first_needed).total_seconds() // 60)+1
    if (expected < long or expected > 61 or len(selected) != expected or not selected or selected[0].start != first_needed
            or any(b.start != first_needed+timedelta(minutes=i) for i,b in enumerate(selected))
            or any(b.source_symbol != symbol or b.source_mode != 'paper' or b.gap_proof is not None
                   or b.adjustment != '' or b.adjustment_rate != ''
                   or not (9,0) <= (b.start.hour,b.start.minute) < (15,20)
                   or b.source_query_started_at is None or b.start+timedelta(minutes=1)>b.source_query_started_at
                   or b.source_query_started_at>now for b in selected)):
        raise B3EvidenceUnavailable('B3 체결 이후 이동평균 전 구간에 누락 또는 출처 불명이 있습니다.')
    safe = all(sma_difference(tuple(selected[i-long+1:i+1]), short, long) >= 0
               for i in range(long-1,len(selected)))
    return safe, tuple(selected) if safe else ()
