"""Select an actual KRX displayed quote, without inferring a tick grid.

Source: /tr_docs/ka10004.md. Only positive-quantity displayed bid/ask levels
at or below the supplied current price are eligible. No orders or retries.
"""
from __future__ import annotations

from datetime import datetime
import http.client
import json
import re
import threading
import time
import urllib.error
import urllib.request

from .errors import BrokerAPIError
from .session import BrokerSession, IssuedToken, KST, LoginSessionError
from kiwoom_autotrader.utils.http_queue import queued_open

_ORIGINS = {"paper": "https://mockapi.kiwoom.com", "real": "https://api.kiwoom.com"}
_MAX_RESPONSE_BYTES = 64 * 1024


class OrderBookError(BrokerAPIError):
    """A safe failure before any budget order is submitted."""


class _OrderBookPacer:
    """One process-wide ka10004 dispatch grant every 1.05 seconds."""

    def __init__(self, *, clock=time.monotonic, wait=time.sleep):
        self._clock, self._wait = clock, wait
        self._lock = threading.Lock()
        self._last = None

    def acquire(self, *, before_send):
        with self._lock:
            before_send()
            while self._last is not None:
                delay = self._last + 1.05 - self._clock()
                if delay <= 0:
                    break
                self._wait(delay)
                before_send()
            before_send()
            self._last = self._clock()


_REQUEST_PACER = _OrderBookPacer()


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _now():
    return datetime.now(KST)


def _identity(session):
    if not isinstance(session, BrokerSession) or not session.authenticated:
        raise OrderBookError("호가 조회에 사용할 로그인 세션이 없습니다. 주문을 전송하지 않았습니다.")
    mode, token = session.mode, session.token
    if mode not in _ORIGINS:
        raise OrderBookError("호가 조회의 투자 구분을 확인하지 못했습니다. 주문을 전송하지 않았습니다.")
    try:
        IssuedToken(token)
        usable = session.token_usable
    except (LoginSessionError, ValueError, TypeError):
        usable = False
    if not usable:
        raise OrderBookError("호가 조회의 로그인 토큰이 유효하지 않습니다. 주문을 전송하지 않았습니다.")
    return mode, token


def _level_number(value, *, price=False):
    # Optional/empty levels do not prove an executable displayed quote.
    if value is None or value == "":
        return 0
    pattern = r"[+-]?[0-9]+" if price else r"\+?[0-9]+"
    if not isinstance(value, str) or len(value) > 20 or re.fullmatch(pattern, value) is None:
        raise OrderBookError("호가 가격·잔량 형식을 확인하지 못했습니다. 주문을 전송하지 않았습니다.")
    result = abs(int(value)) if price else int(value)
    if price and result > 999999999999:
        raise OrderBookError("호가 가격이 주문 한도를 벗어났습니다. 주문을 전송하지 않았습니다.")
    return result


def _select(payload, symbol, current_price):
    if not isinstance(payload, dict):
        raise OrderBookError("호가 응답 형식을 확인하지 못했습니다. 주문을 전송하지 않았습니다.")
    code = payload.get("return_code")
    if not ((type(code) is int and code == 0) or (type(code) is str and code == "0")):
        if ((type(code) is int and -999999 <= code <= 999999)
                or (type(code) is str and re.fullmatch(r"-?[0-9]{1,6}", code))):
            raise OrderBookError(f"호가 조회 실패 (return_code={code}). 주문을 전송하지 않았습니다.", code=code)
        raise OrderBookError("호가 조회 성공 여부를 확인하지 못했습니다. 주문을 전송하지 않았습니다.")
    # The documented response has no symbol echo. If one is supplied, reject a
    # contradiction; the fixed six-digit request is the ordinary KRX scope.
    if "stk_cd" in payload and payload["stk_cd"] != symbol:
        raise OrderBookError("호가 응답 종목이 요청과 다릅니다. 주문을 전송하지 않았습니다.")
    timestamp = payload.get("bid_req_base_tm")
    now = _now()
    if (not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None
            or not isinstance(timestamp, str) or re.fullmatch(r"[0-9]{6}", timestamp) is None):
        raise OrderBookError("호가 기준 시각을 확인하지 못했습니다. 주문을 전송하지 않았습니다.")
    now = now.astimezone(KST)
    try:
        moment = datetime.strptime(timestamp, "%H%M%S")
    except ValueError:
        raise OrderBookError("호가 기준 시각을 확인하지 못했습니다. 주문을 전송하지 않았습니다.") from None
    quoted_at = now.replace(hour=moment.hour, minute=moment.minute, second=moment.second, microsecond=0)
    age = (now - quoted_at).total_seconds()
    if not -2 <= age <= 90:
        raise OrderBookError("호가가 오래되었거나 미래 시각입니다. 주문을 전송하지 않았습니다.")
    prices = []
    for side in ("buy", "sel"):
        for level in range(1, 11):
            stem = f"{side}_fpr" if level == 1 else f"{side}_{level}th_pre"
            price = _level_number(payload.get(stem + "_bid"), price=True)
            quantity = _level_number(payload.get(stem + "_req"))
            if quantity > 0 and 0 < price <= current_price:
                prices.append(price)
    if not prices:
        raise OrderBookError("현재가 이하에서 잔량이 있는 표시호가를 확인하지 못했습니다. 주문을 전송하지 않았습니다.")
    return max(prices)


def fetch_budget_limit(session, symbol, current_price: int, *, before_send=None) -> int:
    """Return the highest fresh displayed KRX level not above current_price."""
    if not isinstance(symbol, str) or re.fullmatch(r"[0-9]{6}", symbol) is None:
        raise OrderBookError("호가 조회는 KRX 6자리 종목코드만 지원합니다. 주문을 전송하지 않았습니다.")
    if type(current_price) is not int or not 1 <= current_price <= 999999999999:
        raise OrderBookError("기준 현재가가 유효한 정수 원화 가격이 아닙니다. 주문을 전송하지 않았습니다.")
    identity = _identity(session)

    def check():
        if _identity(session) != identity:
            raise OrderBookError("호가 조회 중 로그인 상태가 변경되었습니다. 주문을 전송하지 않았습니다.")
        if before_send is not None:
            try:
                before_send()
            except Exception:
                raise OrderBookError("호가 조회 중 실행 조건이 변경되었습니다. 주문을 전송하지 않았습니다.") from None

    check()
    mode, token = identity
    request = urllib.request.Request(_ORIGINS[mode] + "/api/dostk/mrkcond",
        data=json.dumps({"stk_cd": symbol}, ensure_ascii=True).encode(), method="POST",
        headers={"Content-Type": "application/json;charset=UTF-8", "authorization": f"Bearer {token}",
                 "api-id": "ka10004", "cont-yn": "N", "next-key": ""})
    opener = urllib.request.build_opener(_RejectRedirects())
    try:
        with queued_open(opener, request, timeout=15, max_bytes=_MAX_RESPONSE_BYTES,
                         before_send=lambda: _REQUEST_PACER.acquire(before_send=check)) as response:
            raw = response.read(_MAX_RESPONSE_BYTES + 1)
            response_id = response.headers.get("api-id")
            continuation = response.headers.get("cont-yn")
            following = response.headers.get("next-key")
    except (OSError, urllib.error.URLError, http.client.HTTPException, ValueError):
        raise OrderBookError("호가 조회 요청을 완료하지 못했습니다. 주문을 전송하지 않았습니다.") from None
    check()
    if (not isinstance(raw, bytes) or len(raw) > _MAX_RESPONSE_BYTES
            or response_id != "ka10004" or continuation not in (None, "", "N")
            or following not in (None, "")):
        raise OrderBookError("완전한 단일 호가 응답을 확인하지 못했습니다. 주문을 전송하지 않았습니다.")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeError, RecursionError):
        raise OrderBookError("호가 응답을 해석하지 못했습니다. 주문을 전송하지 않았습니다.") from None
    price = _select(payload, symbol, current_price)
    check()
    return price


def observed_book_is_fresh(book, now, *, max_age_seconds=5):
    """Strict paper funnel observation, not a fill or execution-price promise."""
    try:
        if (not isinstance(book,dict) or not isinstance(now,datetime) or now.tzinfo is None
                or book.get('mode')!='paper' or book.get('source')!='ka10004_observed_top1'
                or not isinstance(book.get('wire_sha256'),str)
                or re.fullmatch('[0-9a-f]{64}',book['wire_sha256']) is None
                or type(max_age_seconds) is not int or max_age_seconds!=5):
            return False
        start=datetime.fromisoformat(book['request_started_at'])
        received=datetime.fromisoformat(book['available_at'])
        quote=datetime.fromisoformat(book['quote_time_inferred_kst'])
        now=now.astimezone(KST)
        return bool(all(x.tzinfo is not None for x in (start,received,quote))
            and now.weekday()<5 and '09:00'<=now.strftime('%H:%M')<'15:20'
            and start<=received<=now and 0<=(now-start).total_seconds()<=max_age_seconds
            and 0<=(now-quote).total_seconds()<=max_age_seconds
            and start.replace(second=0,microsecond=0)==now.replace(second=0,microsecond=0)
            and quote.date()==now.date() and book.get('source_date_verified') is False
            and all(type(book.get(k)) is int and book[k]>0 for k in ('ask','bid','ask_size','bid_size'))
            and book['ask']>=book['bid'])
    except (KeyError,TypeError,ValueError,OverflowError):
        return False


def fetch_paper_observed_book(session, symbol, *, now=None, before_send=None):
    """One strict five-second top1 observation through the existing shared queue.

    ka10004 has HHMMSS but no date/symbol echo. Bind the explicit request and
    same paper session; record the inferred current KST date as unverified.
    No retry, inferred tick, substituted order quantity or order is created.
    """
    import hashlib
    clock=now or _now
    started=clock();identity=_identity(session)
    if (identity[0]!='paper' or not isinstance(symbol,str) or re.fullmatch('[0-9]{6}',symbol) is None
            or not isinstance(started,datetime) or started.tzinfo is None):
        raise OrderBookError('모의 정밀 호가 요청 근거를 확인하지 못했습니다.')
    started=started.astimezone(KST)
    def check():
        current=clock()
        if (not isinstance(current,datetime) or current.tzinfo is None or _identity(session)!=identity
                or not 0<=(current-started).total_seconds()<=5
                or current.astimezone(KST).replace(second=0,microsecond=0)!=started.replace(second=0,microsecond=0)
                or current.astimezone(KST).weekday()>=5
                or not '09:00'<=current.astimezone(KST).strftime('%H:%M')<'15:20'):
            raise OrderBookError('모의 정밀 호가의 세션·시각이 변경되었습니다.')
        if before_send is not None:before_send()
    check()
    request=urllib.request.Request(_ORIGINS['paper']+'/api/dostk/mrkcond',
        data=json.dumps({'stk_cd':symbol},ensure_ascii=True).encode(),method='POST',
        headers={'Content-Type':'application/json;charset=UTF-8','authorization':'Bearer '+identity[1],
                 'api-id':'ka10004','cont-yn':'N','next-key':''})
    try:
        with queued_open(urllib.request.build_opener(_RejectRedirects()),request,timeout=5,
                max_bytes=_MAX_RESPONSE_BYTES,before_send=lambda:_REQUEST_PACER.acquire(before_send=check)) as response:
            raw=response.read(_MAX_RESPONSE_BYTES+1)
            api=response.headers.get('api-id');cont=response.headers.get('cont-yn');key=response.headers.get('next-key')
        check()
        if (not isinstance(raw,bytes) or len(raw)>_MAX_RESPONSE_BYTES or api!='ka10004'
                or cont not in (None,'','N') or key not in (None,'')):
            raise ValueError('incomplete_book')
        def unique(pairs):
            result={}
            for k,v in pairs:
                if k in result:raise ValueError('duplicate_field')
                result[k]=v
            return result
        payload=json.loads(raw.decode('utf-8'),object_pairs_hook=unique)
        if (not isinstance(payload,dict) or type(payload.get('return_code')) not in (int,str)
                or payload['return_code'] not in (0,'0')
                or 'stk_cd' in payload and payload['stk_cd']!=symbol):
            raise ValueError('book_identity')
        stamp=payload.get('bid_req_base_tm')
        if not isinstance(stamp,str) or re.fullmatch('[0-9]{6}',stamp) is None:raise ValueError('book_time')
        hhmmss=datetime.strptime(stamp,'%H%M%S');received=clock().astimezone(KST)
        quote=received.replace(hour=hhmmss.hour,minute=hhmmss.minute,second=hhmmss.second,microsecond=0)
        book={'symbol':symbol,'mode':'paper','source':'ka10004_observed_top1',
            'request_started_at':started.isoformat(),'available_at':received.isoformat(),
            'quote_time_inferred_kst':quote.isoformat(),'source_hhmmss':stamp,
            'source_date_verified':False,'source_date_inference':'same_local_regular_session_date',
            'wire_sha256':hashlib.sha256(raw).hexdigest(),
            'ask':_level_number(payload.get('sel_fpr_bid'),price=True),
            'bid':_level_number(payload.get('buy_fpr_bid'),price=True),
            'ask_size':_level_number(payload.get('sel_fpr_req')),
            'bid_size':_level_number(payload.get('buy_fpr_req')),
            'order_authorization':False,'price_or_quantity_substitution':False}
        if not observed_book_is_fresh(book,received):raise ValueError('book_stale_or_sparse')
        book['spread_bps']=(book['ask']-book['bid'])*20000/(book['ask']+book['bid'])
        return book
    except (OSError,urllib.error.URLError,http.client.HTTPException,ValueError,TypeError,KeyError,UnicodeError):
        raise OrderBookError('모의 정밀 호가의 신선도·양방향 잔량·출처를 확인하지 못했습니다.') from None
