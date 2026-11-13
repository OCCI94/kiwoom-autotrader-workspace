"""Paper-only, bounded-age ka10001 context for strategy checks.

Timestamps record local request and receipt, not exchange quote time: ka10001
provides no source timestamp.  A hash identifies the normalized, credential-
free response fields, not the broker's original response bytes.  Sentiment,
DART, themes and same-time RVOL are unknown until independently established.
This module neither authenticates nor submits orders.
"""
from __future__ import annotations

from datetime import datetime, timedelta, time
from copy import deepcopy
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re
import os
import stat
import urllib.request
from pathlib import Path
import secrets
import threading
import time as clock_time
from zoneinfo import ZoneInfo

from kiwoom_autotrader.api.stock_info import fetch_stock_info, normalize_stock_code
from kiwoom_autotrader.utils.paths import app_data_root

KST = ZoneInfo("Asia/Seoul")
MAX_AGE = timedelta(seconds=5)


class ContextUnavailable(ValueError):
    """A paper strategy cannot safely use this response."""
    _CODES = frozenset({'unknown','time_invalid','price_missing','price_invalid','ratio_invalid',
        'session_unavailable','session_changed','symbol_invalid','symbol_mismatch','market_closed',
        'request_stale','minute_rollover','clock_reversed','price_conflict','ranking_unavailable'})
    def __init__(self, message, *, code='unknown'):
        super().__init__(message)
        self.code = code if code in self._CODES else 'unknown'


def _aware(value):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ContextUnavailable("시각의 시간대를 확인할 수 없습니다.", code="time_invalid")
    return value.astimezone(KST)


def _price(value):
    if value is None or value == "":
        raise ContextUnavailable("종목 가격이 없습니다.", code="price_missing")
    if not isinstance(value, str) or not re.fullmatch(r"[+-]?[0-9]{1,12}", value):
        raise ContextUnavailable("종목 가격이 유효하지 않습니다.", code="price_invalid")
    price = abs(int(value))  # Kiwoom +/- is direction, not a negative price.
    if not 0 < price <= 10**12:
        raise ContextUnavailable("종목 가격이 올바르지 않습니다.", code="price_invalid")
    return price


def _decimal(value, *, nonnegative=False):
    if value in (None, ""):
        return None
    if not isinstance(value, str) or not re.fullmatch(r"[+-]?[0-9]{1,12}(?:\.[0-9]{1,6})?", value):
        raise ContextUnavailable("종목 비율이 유효하지 않습니다.", code="ratio_invalid")
    try:
        number = Decimal(value)
    except InvalidOperation:
        raise ContextUnavailable("종목 비율이 유효하지 않습니다.", code="ratio_invalid") from None
    if nonnegative and number < 0:
        raise ContextUnavailable("거래량 비교 비율이 유효하지 않습니다.", code="ratio_invalid")
    # String retains exact decimal precision in JSON-compatible context.
    return str(number)


def _market_time(now):
    return now.weekday() < 5 and time(9, 0) <= now.time().replace(tzinfo=None) < time(15, 30)


def intraday_bullish_candle_state(current_price, day_open):
    """Derived diagnostic only; missing/invalid prices are unknown, not bearish.

    Call after validating the same ka10001 response. This boolean adds no
    source data to the existing cur_prc/open_pric hash and is not order
    authority: S2 and immutable v3 evidence recompute the price comparison.
    """
    if any(type(price) is not int or not 0 < price <= 10**12
           for price in (current_price, day_open)):
        return None
    return current_price > day_open


class PaperStrategyContextProvider:
    """Fetch via the receiver's session; cache is local and session-bound.

    `as_of` is the time a caller requested context, not a backtest cutoff.
    New observations become available only at `available_at` (receipt).
    Call `is_fresh` immediately before using a context. Callers independently
    enforce their market calendar, risk, portfolio and execution guards.
    """

    def __init__(self, session, *, fetcher=fetch_stock_info, now=None,
                 enable_surge=False, surge_fetcher=None, taxonomy_path=None,
                 turnover_fetcher=None, exit_only=False, membership_fetcher=None,
                 telegram_candidates_path=None, book_fetcher=None,
                 enable_toss_hybrid=False, toss_candidates_path=None, s5_daily_fetcher=None, s5_risk_path=None,
                 enable_prearmed_risk=False, kosdaq_surge_fetcher=None, kosdaq_turnover_fetcher=None):
        if type(exit_only) is not bool or (exit_only and enable_surge):
            raise ValueError("exit-only context cannot enable ranking")
        self._exit_only = exit_only
        if type(enable_toss_hybrid) is not bool or (exit_only and enable_toss_hybrid):
            raise ValueError('exit-only context cannot enable Toss discovery')
        self._enable_toss_hybrid = enable_toss_hybrid
        self._toss_candidates_path = (Path(toss_candidates_path) if toss_candidates_path is not None
            else app_data_root() / 'config' / 'research' / 'toss-candidates.json')
        self._s5_daily_fetcher = s5_daily_fetcher or self._fetch_s5_daily_page
        self._s5_daily_cache = {}
        self._s5_risk_path = Path(s5_risk_path) if s5_risk_path is not None else app_data_root() / "config" / "research" / "toss-risk-evidence.json"
        if type(enable_prearmed_risk) is not bool or exit_only and enable_prearmed_risk:
            raise ValueError('prearmed risk is entry-only')
        self._prearmed_risk = None
        if enable_prearmed_risk:
            from .prearmed_risk import BackgroundS5RiskCache
            self._prearmed_risk = BackgroundS5RiskCache(self._s5_risk_path, now=now)
        self._session = session
        self._fetcher = fetcher
        self._now = now or (lambda: datetime.now(KST))
        self._lock = threading.RLock()
        self._identity = None
        self._binding = None
        self._cache = {}
        self._enable_surge = enable_surge
        self._surge_fetcher = surge_fetcher or self._fetch_surge_page
        self._surge_minute = None
        self._surge_snapshot = None
        self._taxonomy_path = (Path(taxonomy_path) if taxonomy_path is not None else
                               app_data_root() / "config" / "research" / "pro6e_sector_taxonomy.json")
        self._turnover_fetcher = turnover_fetcher or self._fetch_turnover_page
        self._turnover_minute = None
        self._turnover_snapshot = None
        self._kosdaq_surge_fetcher=kosdaq_surge_fetcher or (lambda:self._fetch_surge_page('101'))
        self._kosdaq_turnover_fetcher=kosdaq_turnover_fetcher or (lambda:self._fetch_turnover_page('101'))
        self._kosdaq_minute=None
        self._kosdaq_snapshots={}
        self._membership_fetcher = membership_fetcher or self._fetch_membership_page
        self._membership_minute = None
        self._membership_snapshots = {}
        self._query_counts = {}
        from kiwoom_autotrader.api.order_book import fetch_paper_observed_book
        self._book_fetcher = book_fetcher or fetch_paper_observed_book
        self._book_cache = {}
        self._telegram_candidates_path = (Path(telegram_candidates_path)
            if telegram_candidates_path is not None else
            app_data_root() / 'config' / 'research' / 'telegram-candidates.json')

    def _session_identity(self):
        session = self._session
        issued = getattr(session, "_issued", None)
        if (getattr(session, "mode", None) != "paper" or issued is None
                or not getattr(session, "authenticated", False)
                or not getattr(session, "token_usable", False)):
            raise ContextUnavailable("유효한 모의투자 세션이 필요합니다.", code="session_unavailable")
        return session, issued

    def _bind(self):
        identity = self._session_identity()
        if self._identity is None or any(a is not b for a, b in zip(identity, self._identity)):
            self._identity = identity
            self._binding = secrets.token_hex(16)
            self._cache.clear()
            self._surge_minute = None
            self._surge_snapshot = None
            self._turnover_minute = None
            self._turnover_snapshot = None
            self._kosdaq_minute=None
            self._kosdaq_snapshots.clear()
            self._membership_minute = None
            self._membership_snapshots.clear()
            self._query_counts.clear()
            self._book_cache.clear()
            self._s5_daily_cache.clear()
        return identity

    def _count_query(self, api, now):
        minute = now.replace(second=0, microsecond=0).isoformat()
        if self._query_counts.get('minute') != minute:
            self._query_counts = {'minute': minute, 'ka10023': 0, 'ka10032': 0, 'ka10099': 0, 'ka10004': 0}
        self._query_counts[api] = self._query_counts.get(api, 0) + 1

    def discover_funnel_candidates(self, as_of=None, *, configured_symbols=()):
        """Same rank caches, strict gain window, full verified in-memory rows.

        No per-symbol chart/quote query is made here. Catalog warmup/refresh
        adds at most one separately counted ka10099 first page per minute.
        """
        return self.discover_dynamic_candidates(as_of, configured_symbols=configured_symbols, funnel=True)

    def fetch_orderbook(self, code, as_of=None, *, before_send=None):
        """Optional funnel precision stage; strict5s, no same-minute retry."""
        from kiwoom_autotrader.api.order_book import observed_book_is_fresh
        with self._lock:
            identity=self._bind();start=_aware(self._now())
            if self._exit_only or not _market_time(start):
                raise ContextUnavailable('정밀 호가 정규장 조회 조건을 확인하지 못했습니다.')
            if not isinstance(code,str) or re.fullmatch('[0-9]{6}',code) is None:
                raise ContextUnavailable('정밀 호가 종목을 확인하지 못했습니다.')
            requested=_aware(as_of) if as_of is not None else start
            if not timedelta(0)<=start-requested<=MAX_AGE:
                raise ContextUnavailable('정밀 호가 요청시각을 확인하지 못했습니다.')
            minute=start.replace(second=0,microsecond=0)
            self._book_cache={key:value for key,value in self._book_cache.items() if key[1]==minute}
            key=(code,minute)
            if key not in self._book_cache:
                self._book_cache[key]=None
                self._count_query('ka10004',start)
                def check():
                    current=_aware(self._now())
                    if (any(a is not b for a,b in zip(identity,self._session_identity()))
                            or current.replace(second=0,microsecond=0)!=minute
                            or not timedelta(0)<=current-start<=MAX_AGE):
                        raise ContextUnavailable('정밀 호가의 세션·시각이 변경되었습니다.')
                    if before_send is not None:before_send()
                check()
                try:
                    book=self._book_fetcher(self._session,code,now=self._now,before_send=check)
                    check()
                except Exception:
                    raise ContextUnavailable('정밀 호가 조회 또는 근거를 확인하지 못했습니다.') from None
                received=_aware(self._now())
                if not isinstance(book,dict) or book.get('symbol')!=code or not observed_book_is_fresh(book,received):
                    raise ContextUnavailable('정밀 호가가 오래되었거나 양방향 잔량이 부족합니다.')
                self._book_cache[key]=deepcopy(book)|{'session_binding':self._binding}
            book=self._book_cache[key]
            if book is None or not self.is_orderbook_fresh(book,_aware(self._now())):
                raise ContextUnavailable('정밀 호가를 현재시점에 검증하지 못했습니다.')
            return deepcopy(book)

    def is_orderbook_fresh(self, book, now):
        from kiwoom_autotrader.api.order_book import observed_book_is_fresh
        try:
            identity=self._session_identity()
            return bool(self._identity is not None and all(a is b for a,b in zip(identity,self._identity))
                and isinstance(book,dict) and book.get('session_binding')==self._binding
                and observed_book_is_fresh(book,now))
        except Exception:
            return False

    def _ranking_page(self, api, body, output, *, endpoint="/api/dostk/rkinfo"):
        from kiwoom_autotrader.utils.http_queue import queued_open
        identity = self._session_identity()
        token = identity[1].token
        deadline = clock_time.monotonic() + 8
        def guard():
            if clock_time.monotonic() >= deadline or any(a is not b for a, b in zip(identity, self._session_identity())):
                raise ContextUnavailable("순위 조회 세션이 변경되었거나 시간이 지났습니다.")
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl): return None
        if endpoint not in ("/api/dostk/rkinfo", "/api/dostk/stkinfo") and not (endpoint == "/api/dostk/chart" and api == "ka10081"):
            raise ContextUnavailable("허용되지 않은 조회 경로입니다.")
        request = urllib.request.Request("https://mockapi.kiwoom.com" + endpoint,
            data=json.dumps(body, ensure_ascii=True).encode(), method="POST",
            headers={"Content-Type":"application/json;charset=UTF-8", "authorization":"Bearer " + token,
                     "api-id":api})
        with queued_open(urllib.request.build_opener(NoRedirect()), request, timeout=8,
                         max_bytes=2*1024*1024, before_send=guard, priority="research") as response:
            raw = response.read(2*1024*1024+1)
            continuation = response.headers.get("cont-yn", "")
            header_api = response.headers.get("api-id", "")
        guard()
        if not isinstance(raw, bytes) or len(raw) > 2*1024*1024 or header_api not in ("", api):
            raise ContextUnavailable("순위 응답 형식이 유효하지 않습니다.")
        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result: raise ValueError("duplicate")
                result[key] = value
            return result
        payload = json.loads(raw.decode("utf-8"), object_pairs_hook=unique)
        if (not isinstance(payload, dict) or type(payload.get("return_code")) not in (int, str)
                or payload["return_code"] not in (0, "0") or not isinstance(payload.get(output), list)):
            raise ContextUnavailable("순위 응답을 검증할 수 없습니다.")
        return {"ok":True, "body":{output:payload[output], **({"stk_cd":payload.get("stk_cd")} if api == "ka10081" else {})}, "headers":{"cont-yn":continuation},
                "wire_sha256":hashlib.sha256(raw).hexdigest()}

    def _fetch_s5_daily_page(self, code, day):
        return self._ranking_page('ka10081',{'stk_cd':code,'base_dt':day,'upd_stkpc_tp':'0'},
            'stk_dt_pole_chart_qry',endpoint='/api/dostk/chart')

    def fetch_s5_inputs(self, symbol, as_of=None):
        """Bounded prior-daily cache + existing local DART risk evidence.

        One daily request per symbol/session/day, including failed requests.
        Invoke before the fresh ka10001 quote. No DART/network/auth worker here.
        """
        from .steady_uptrend_vwap import validate_daily_context
        with self._lock:
            identity = self._bind()
            now = _aware(self._now())
            code = normalize_stock_code(symbol)
            requested = _aware(as_of) if as_of is not None else now
            if (self._exit_only or not _market_time(now) or not timedelta(0)<=now-requested<=timedelta(seconds=60)):
                return {'s5_daily_context':None,'s5_risk_context':None}
            day = now.strftime('%Y%m%d'); key = (code,day)
            self._s5_daily_cache = {k:v for k,v in self._s5_daily_cache.items() if k[1] == day}
            if key not in self._s5_daily_cache:
                self._s5_daily_cache[key] = None
                self._count_query('ka10081',now)
                try:
                    response = self._s5_daily_fetcher(code,day)
                    received = _aware(self._now())
                    if (any(a is not b for a,b in zip(identity,self._session_identity()))
                            or received.date() != now.date() or not timedelta(0)<=received-now<=timedelta(seconds=15)
                            or not isinstance(response,dict) or response.get('ok') is not True
                            or not isinstance(response.get('body'),dict) or response['body'].get('stk_cd') != code):
                        raise ValueError('daily_context_unverified')
                    rawrows = response['body'].get('stk_dt_pole_chart_qry')
                    if not isinstance(rawrows,list) or not 20<=len(rawrows)<=2000:
                        raise ValueError('daily_context_unavailable')
                    dated = {}; seen = set()
                    for raw in rawrows:
                        if not isinstance(raw,dict): raise ValueError('daily_row')
                        date = raw.get('dt')
                        if not isinstance(date,str) or re.fullmatch('[0-9]{8}',date) is None or date in seen:
                            raise ValueError('daily_date')
                        parsed = datetime.strptime(date,'%Y%m%d').date();seen.add(date)
                        if parsed > now.date(): raise ValueError('future_daily')
                        if parsed == now.date(): continue
                        dated[date] = raw
                    rows = {}
                    for date in sorted(dated)[-20:]:
                        raw = dated[date]
                        if raw.get('upd_stkpc_tp') not in (None,'') or raw.get('upd_rt') not in (None,''):
                            raise ValueError('daily_adjustment')
                        volume = raw.get('trde_qty')
                        if not isinstance(volume,str) or re.fullmatch('[+]?[0-9]{1,19}',volume) is None:
                            raise ValueError('daily_volume')
                        rows[date] = {'date':date,'open':_price(raw.get('open_pric')),
                            'high':_price(raw.get('high_pric')),'low':_price(raw.get('low_pric')),
                            'close':_price(raw.get('cur_prc')),'volume':int(volume)}
                    context = {'schema':'s5-prior-daily-v1','symbol':code,'mode':'paper','session_day':day,
                        'request_started_at':now.isoformat(),'available_at':received.isoformat(),
                        'source_sha256':response.get('wire_sha256'),'source_api':'ka10081',
                        'rows':[rows[k] for k in sorted(rows)[-20:]]}
                    if validate_daily_context(context,code,received) is None: raise ValueError('daily_invalid')
                    self._s5_daily_cache[key] = context
                except Exception:
                    # Failure is cached; do not retry this source in the session/day.
                    pass
            return {'s5_daily_context':deepcopy(self._s5_daily_cache[key]),
                    's5_risk_context':self._read_s5_risk(code,_aware(self._now()))}

    def _read_s5_risk(self, symbol, now):
        cache = getattr(self, '_prearmed_risk', None)
        if cache is not None:
            return cache.get(symbol, now)
        from .steady_uptrend_vwap import moment, risk_eligible
        fd = None
        try:
            fd = os.open(self._s5_risk_path,os.O_RDONLY | getattr(os,'O_NOFOLLOW',0) | getattr(os,'O_NONBLOCK',0))
            metadata = os.fstat(fd)
            if (not stat.S_ISREG(metadata.st_mode) or not 0<metadata.st_size<=256*1024
                    or metadata.st_mode & 0o022 or hasattr(os,'getuid') and metadata.st_uid != os.getuid()):
                return None
            raw = os.read(fd,256*1024+1)
            if len(raw)>256*1024: return None
            def unique(pairs):
                result={}
                for k,v in pairs:
                    if k in result: raise ValueError('duplicate')
                    result[k]=v
                return result
            payload=json.loads(raw,object_pairs_hook=unique)
            if not isinstance(payload,dict) or payload.get('schema')!='toss-hybrid-risk-evidence-v1': return None
            generated,expires=moment(payload.get('generated_at')),moment(payload.get('expires_at'))
            rows=payload.get('rows')
            if (generated is None or expires is None or not generated<=now<expires
                    or expires-generated>timedelta(hours=24) or not isinstance(rows,list) or len(rows)>1200):
                return None
            found=None; seen=set()
            for row in rows:
                if not isinstance(row,dict): return None
                code=row.get('symbol')
                if not isinstance(code,str) or re.fullmatch('[0-9]{6}',code) is None or code in seen: return None
                seen.add(code)
                if code==symbol: found=row
            if found is None: return None
            reviewed=moment(found.get('reviewed_at'))
            if reviewed is None or reviewed>generated: return None
            context={'schema':'s5-dart-risk-v1','symbol':symbol,'reviewed_at':found.get('reviewed_at'),
                'expires_at':expires.isoformat(),'coverage_complete':found.get('coverage_complete'),
                'unresolved_major_risk':found.get('unresolved_major_risk'),
                'source_sha256':found.get('source_sha256'),'file_sha256':hashlib.sha256(raw).hexdigest()}
            if 'short_selling_evidence' in found:
                context['short_selling_evidence']=deepcopy(found['short_selling_evidence'])
            return context if risk_eligible(context,symbol,now) else None
        except (OSError,ValueError,TypeError,OverflowError):
            return None
        finally:
            if fd is not None: os.close(fd)

    def s5_risk_generation(self):
        cache = getattr(self, '_prearmed_risk', None)
        return cache.generation(_aware(self._now())) if cache is not None else None

    def s5_eligible_symbols(self, as_of=None):
        """Local risk-qualified scheduling scope; no quote or order authority.

        Reconcile this scope with configured symbols in the monitor. Each actual
        evaluation and reservation still independently verifies fresh evidence.
        No session binding, chart fetch, credential load or broker call occurs.
        """
        if self._exit_only:
            return ()

        try:
            now = _aware(self._now())
            requested = _aware(as_of) if as_of is not None else now
            if not timedelta(0) <= now-requested <= timedelta(seconds=60):
                return ()
            cache = getattr(self, '_prearmed_risk', None)
            if cache is not None:
                return cache.symbols(now)
            from .prearmed_risk import read_index
            _, _, contexts = read_index(self._s5_risk_path, now)
            return tuple(sorted(contexts))
        except (OSError, ValueError, TypeError, AttributeError, OverflowError):
            return ()


    def toss_candidate_is_current(self, candidate, as_of=None):
        """Bounded local reread: a copied watch proof cannot outlive revocation.

        No broker request and no cache TTL extension. Compare both the exact
        feed generation and the latest reviewed symbol-risk source before any
        initial order, including S1 and S2--S4 (not just S5).
        """
        if not self._enable_toss_hybrid or self._exit_only or not isinstance(candidate,dict):return False
        try:
            from .toss_candidate_feed import read_toss_candidates
            from .prearmed_risk import read_index
            now=_aware(as_of if as_of is not None else self._now())
            current=read_toss_candidates(self._toss_candidates_path,now)
            if current.get('status')!='ready' or current.get('source_sha256')!=candidate.get('feed_sha256'):return False
            rows={row['symbol']:row for row in current['candidates']}
            original={k:v for k,v in candidate.items() if k not in ('feed_sha256','feed_expires_at')}
            if rows.get(candidate.get('symbol'))!=original:return False
            _,_,risks=read_index(self._s5_risk_path,now)
            risk=risks.get(candidate.get('symbol'))
            return bool(risk and risk.get('source_sha256')==candidate.get('source_hashes',{}).get('risk_review')
                        and risk.get('reviewed_at')==candidate.get('risk_review',{}).get('reviewed_at')
                        and risk.get('expires_at')==candidate.get('risk_review',{}).get('expires_at'))
        except (OSError,ValueError,TypeError,KeyError,AttributeError,OverflowError):return False

    def watch_candidate_is_current(self, symbol, candidate, as_of=None):
        """Last local admission check; no additional HTTP or price inference."""
        if not isinstance(candidate,dict):return False
        now=_aware(as_of if as_of is not None else self._now())
        if isinstance(candidate.get('toss_evidence'),dict):
            return self.toss_candidate_is_current(candidate['toss_evidence'],now)
        market=self._cache.get(symbol)
        return bool(isinstance(market,dict) and self.is_fresh(market,now)
            and intraday_bullish_candle_state(market.get('current_price'),market.get('day_open')) is True
            and market['current_price']<=500_000)

    def close_prearmed(self):
        cache = getattr(self, '_prearmed_risk', None)
        if cache is not None: cache.close()

    def _fetch_surge_page(self, market='000'):
        if market not in ('000','101'):raise ValueError('ranking_market')
        result = self._ranking_page("ka10023", {"mrkt_tp":market, "sort_tp":"2", "tm_tp":"2",
            "trde_qty_tp":"5", "tm":"", "stk_cnd":"20", "pric_tp":"0", "stex_tp":"1"}, "trde_qty_sdnin")
        # ka10023.md: 20 excludes ETF, ETN and SPAC; taxonomy is not a classifier.
        result["instrument_filter"] = "ka10023_stk_cnd_20"
        return result

    def _fetch_turnover_page(self, market='000'):
        if market not in ('000','101'):raise ValueError('ranking_market')
        return self._ranking_page("ka10032", {"mrkt_tp":market, "mang_stk_incls":"0", "stex_tp":"1"}, "trde_prica_upper")

    def _kosdaq_ranks(self, identity):
        """At most two additional shared-queue first pages per minute.

        No pagination/retry/per-symbol query. The ordinary all-market caches
        remain unchanged; precise per-page source hashes travel with the union.
        """
        start=_aware(self._now());minute=start.replace(second=0,microsecond=0)
        if self._kosdaq_minute==minute:return self._kosdaq_snapshots
        self._kosdaq_minute=minute;self._kosdaq_snapshots={}
        for kind,api,fetcher,output in (
            ('surge','ka10023',self._kosdaq_surge_fetcher,'trde_qty_sdnin'),
            ('turnover','ka10032',self._kosdaq_turnover_fetcher,'trde_prica_upper')):
            now=_aware(self._now())
            if now.replace(second=0,microsecond=0)!=minute:break
            try:
                if any(a is not b for a,b in zip(identity,self._session_identity())):break
                self._count_query(api,now);page=fetcher();received=_aware(self._now())
                rows=page.get('body',{}).get(output);digest=page.get('wire_sha256');cont=page.get('headers',{}).get('cont-yn')
                if (any(a is not b for a,b in zip(identity,self._session_identity())) or not now<=received<=now+timedelta(seconds=8)
                        or received.replace(second=0,microsecond=0)!=minute or page.get('ok') is not True
                        or not isinstance(rows,list) or len(rows)>10000 or cont not in ('','N','Y')
                        or not isinstance(digest,str) or re.fullmatch('[0-9a-f]{64}',digest) is None):continue
                if kind=='surge' and page.get('instrument_filter')!='ka10023_stk_cnd_20':continue
                values={};liquidity={}
                for row in rows:
                    if not isinstance(row,dict):raise ValueError('ranking_row')
                    code=row.get('stk_cd')
                    if not isinstance(code,str) or re.fullmatch('[0-9]{6}',code) is None:continue
                    if code in values:values[code]=None;liquidity[code]=None;continue
                    if kind=='surge':
                        values[code]=(_decimal(row.get('flu_rt')),_decimal(row.get('sdnin_rt')))
                        fields=('now_trde_qty','prev_trde_qty','sdnin_qty','pred_pre_sig','cur_prc')
                    else:
                        amount=_decimal(row.get('trde_prica'),nonnegative=True)
                        values[code]=int(Decimal(amount)*1_000_000) if amount is not None else None
                        fields=('now_trde_qty','pred_trde_qty','flu_rt','pred_pre_sig','cur_prc')
                    liquidity[code]={key:row.get(key) for key in fields}
                snapshot={'rows' if kind=='surge' else 'values':values,'liquidity_rows':liquidity,
                    'request_started_at':now.isoformat(),'available_at':received.isoformat(),
                    'observed_at':received.isoformat(),'source_sha256':digest,'market':'101',
                    'page_count':1,'truncated':cont=='Y'}
                if kind=='surge':snapshot.update(instrument_filter='ka10023_stk_cnd_20',basis='previous_day_volume_comparison')
                self._kosdaq_snapshots[kind]=snapshot
            except Exception:
                continue
        return self._kosdaq_snapshots

    @staticmethod
    def _union_rank_pages(primary, secondary, kind):
        if not isinstance(secondary,dict):return primary
        if not isinstance(primary,dict):return secondary
        key='rows' if kind=='surge' else 'values';out=deepcopy(primary)
        out[key].update(secondary[key]);out['liquidity_rows'].update(secondary['liquidity_rows'])
        out['row_source_sha256']={code:primary['source_sha256'] for code in primary[key]}|{code:secondary['source_sha256'] for code in secondary[key]}
        out['row_market']={code:'101' for code in secondary[key]}
        out['source_pages']=[{k:v for k,v in page.items() if k not in (key,'liquidity_rows')} for page in (primary,secondary)]
        out['source_sha256']=hashlib.sha256(json.dumps(out['source_pages'],sort_keys=True,separators=(',',':')).encode()).hexdigest()
        out['available_at']=max(primary['available_at'],secondary['available_at'])
        # Freshness is per-page; union start/end must not falsely manufacture a
        # broker request longer than8s. Both retain original times above.
        out['request_started_at']=secondary['request_started_at'];out['page_count']=2
        out['truncated']=bool(primary['truncated'] or secondary['truncated'])
        return out

    def _fetch_membership_page(self, market):
        if market not in ("0", "10"):
            raise ContextUnavailable("일반주 분류 시장이 유효하지 않습니다.")
        return self._ranking_page("ka10099", {"mrkt_tp":market}, "list", endpoint="/api/dostk/stkinfo")

    def _membership(self, identity):
        """Refresh at most one market first page per minute; never retry it.

        Normal/managed status is only accepted for five minutes. Partial pages
        verify their present rows, never the missing universe. Initial KOSDAQ
        enrollment therefore waits for a later normal tick, not a burst.
        """
        now = _aware(self._now())
        minute = now.replace(second=0, microsecond=0)
        if self._membership_minute == minute:
            return list(self._membership_snapshots.values())
        market = min(("0", "10"), key=lambda key:self._membership_snapshots.get(key, {}).get("available_at", ""))
        current = self._membership_snapshots.get(market)
        if current and now - _aware(datetime.fromisoformat(current["available_at"])) < timedelta(seconds=240):
            return list(self._membership_snapshots.values())
        self._membership_minute = minute
        self._membership_snapshots.pop(market, None)
        try:
            self._count_query('ka10099', now)
            page = self._membership_fetcher(market)
            received = _aware(self._now())
            if (any(a is not b for a,b in zip(identity,self._session_identity())) or received < now
                    or received - now > timedelta(seconds=8)
                    or received.replace(second=0,microsecond=0) != minute):
                return list(self._membership_snapshots.values())
            rows = page.get("body", {}).get("list")
            digest, cont = page.get("wire_sha256"), page.get("headers", {}).get("cont-yn")
            if (page.get("ok") is not True or not isinstance(rows,list) or len(rows)>10000
                    or not isinstance(digest,str) or re.fullmatch(r"[0-9a-f]{64}",digest) is None
                    or cont not in ("", "N", "Y")):
                return list(self._membership_snapshots.values())
            indexed = {}
            for row in rows:
                if not isinstance(row,dict):
                    return list(self._membership_snapshots.values())
                code = row.get("code")
                if not isinstance(code,str) or re.fullmatch(r"[0-9]{6}",code) is None:
                    continue
                indexed[code] = (None if code in indexed else
                    {key:row.get(key) for key in ("code","name","marketCode","auditInfo","state",
                                                 "orderWarning","upSizeName","companyClassName")})
            self._membership_snapshots[market] = {"market":market, "rows":indexed,
                "source_sha256":digest, "request_started_at":now.isoformat(),
                "available_at":received.isoformat(), "page_count":1, "truncated":cont=="Y"}
        except Exception:
            # Missing classification cannot grant membership; no same-minute retry.
            pass
        return list(self._membership_snapshots.values())

    def discover_dynamic_candidates(self, as_of=None, *, limit=30, configured_symbols=(), funnel=False):
        """Read fresh rank caches and return independently classified watch candidates.

        Uses this receiver's paper session only. No research.sock, independent
        login, order, file write or operational DB mutation occurs here.
        """
        from .dynamic_candidates import read_taxonomy, read_telegram_candidates, select_dynamic_candidates
        if type(funnel) is not bool:
            raise ValueError('funnel must be boolean')
        if type(limit) is not int or not 15 <= limit <= 30:
            raise ValueError("candidate limit must be between 15 and 30")
        if (not isinstance(configured_symbols, (list, tuple)) or len(configured_symbols) > (200 if funnel else 100)
                or any(not isinstance(code,str) or re.fullmatch(r"[0-9]{6}",code) is None
                       for code in configured_symbols)):
            raise ValueError("configured symbols must be bounded KRX identifiers")
        with self._lock:
            identity = self._bind()
            start = _aware(self._now())
            requested = _aware(as_of) if as_of is not None else start
            if self._exit_only or not self._enable_surge:
                raise ContextUnavailable("탐색 순위 조회가 활성화되지 않았습니다.", code="ranking_unavailable")
            if not _market_time(start):
                raise ContextUnavailable("정규장 시각을 확인할 수 없습니다.", code="market_closed")
            if requested > start or start-requested > MAX_AGE:
                raise ContextUnavailable("후보 요청 시각을 확인할 수 없습니다.", code="request_stale")
            # Refresh membership first, so rank evidence is not aged by catalog latency.
            memberships = self._membership(identity)
            if any(a is not b for a,b in zip(identity,self._session_identity())):
                raise ContextUnavailable("탐색 도중 세션이 변경되었습니다.", code="session_changed")
            self._surge("", identity)
            if any(a is not b for a,b in zip(identity,self._session_identity())):
                raise ContextUnavailable("탐색 도중 세션이 변경되었습니다.", code="session_changed")
            turnover = self._turnover(identity) if self._surge_snapshot is not None else None
            surge=self._surge_snapshot
            if self._enable_toss_hybrid and isinstance(surge,dict) and isinstance(turnover,dict):
                extra_ranks=self._kosdaq_ranks(identity)
                surge=self._union_rank_pages(surge,extra_ranks.get('surge'),'surge')
                turnover=self._union_rank_pages(turnover,extra_ranks.get('turnover'),'turnover')
            received = _aware(self._now())
            if any(a is not b for a,b in zip(identity,self._session_identity())):
                raise ContextUnavailable("탐색 도중 세션이 변경되었습니다.", code="session_changed")
            if not _market_time(received):
                raise ContextUnavailable("정규장 시각을 확인할 수 없습니다.", code="market_closed")
            stocks, taxonomy = read_taxonomy(self._taxonomy_path, received)
            # The publisher reads existing Telegram data only. This read adds no
            # market request, and stale/missing messages cannot authorize entry.
            telegram = read_telegram_candidates(self._telegram_candidates_path, received)
            result = select_dynamic_candidates(surge, turnover, memberships,
                stocks, taxonomy, now=received, session_binding=self._binding, limit=limit,
                configured_symbols=configured_symbols, telegram_feed=telegram, funnel=funnel,tiered=self._enable_toss_hybrid)
            if self._enable_toss_hybrid:
                # Read only a small atomic local file. Token refresh, HTTP and
                # retries belong exclusively to the separate read-only worker.
                from .toss_candidate_feed import read_toss_candidates, merge_toss_candidates
                toss_feed = read_toss_candidates(self._toss_candidates_path, received)
                result = merge_toss_candidates(result, toss_feed, memberships, stocks,
                    now=received, configured_symbols=configured_symbols, funnel=funnel, limit=limit,tiered=True)
            result['request_accounting'] = dict(self._query_counts,
                rank_calls_per_minute_max=4 if self._enable_toss_hybrid else 2, membership_calls_per_minute_max=1,
                per_symbol_market_calls_in_discovery=0, failed_attempts_included=True,
                first_pages_only=True, full_exchange_coverage=False,
                toss_http_calls_in_receiver=0, toss_local_feed_enabled=self._enable_toss_hybrid)
            return deepcopy(result)

    def _surge(self, code, identity):
        now = _aware(self._now())
        minute = now.replace(second=0, microsecond=0)
        if self._surge_minute != minute:
            self._surge_minute = minute  # also prevents retries on failure
            self._surge_snapshot = None
            try:
                self._count_query('ka10023', now)
                response = self._surge_fetcher()
                received = _aware(self._now())
                current = self._session_identity()
                if (any(a is not b for a, b in zip(current, identity)) or received < now
                        or received - now > timedelta(seconds=8)
                        or received.replace(second=0, microsecond=0) != minute):
                    return None
                digest = response.get("wire_sha256")
                rows = response.get("body", {}).get("trde_qty_sdnin")
                continuation = response.get("headers", {}).get("cont-yn")
                if (response.get("ok") is not True or not isinstance(digest, str)
                        or re.fullmatch(r"[0-9a-f]{64}", digest) is None
                        or continuation not in ("", "N", "Y") or not isinstance(rows, list)
                        or len(rows) > 10000):
                    return None
                index, liquidity = {}, {}
                for row in rows:
                    if not isinstance(row, dict):
                        return None
                    symbol = row.get("stk_cd")
                    if not isinstance(symbol, str) or re.fullmatch(r"[0-9]{6}", symbol) is None:
                        continue
                    # Duplicate/contradictory target rows are unknown.
                    if symbol in index:
                        index[symbol] = None
                        liquidity[symbol] = None
                        continue
                    try:
                        change = _decimal(row.get("flu_rt"))
                        surge = _decimal(row.get("sdnin_rt"))
                    except ContextUnavailable:
                        index[symbol] = None
                        continue
                    index[symbol] = (change, surge)
                    liquidity[symbol] = {key:row.get(key) for key in
                        ("now_trde_qty", "prev_trde_qty", "sdnin_qty", "pred_pre_sig", "cur_prc")}
                self._surge_snapshot = {"rows": index, "liquidity_rows":liquidity, "source_sha256": digest,
                    "request_started_at": now.isoformat(), "observed_at": received.isoformat(),
                    "available_at": received.isoformat(), "truncated": continuation == "Y",
                    "basis": "previous_day_volume_comparison", "page_count": 1,
                    "instrument_filter": response.get("instrument_filter")}
            except Exception:
                # No speculative retry, no leaked HTTP payload or token.
                return None
        snapshot = self._surge_snapshot
        if snapshot is None:
            return None
        row = snapshot["rows"].get(code)
        if not row or row[0] is None or row[1] is None:
            return None
        change, surge = Decimal(row[0]), Decimal(row[1])
        if change <= 0 or surge <= 0:
            return None
        return {key: value for key, value in snapshot.items() if key not in ("rows", "liquidity_rows")} | {
            "symbol": code, "change_pct": str(change), "surge_rate_pct": str(surge),
            "rising_volume_surge": True, "source_market_timestamp": None}

    def _turnover(self, identity):
        """One cached first page per minute, shared by themes and discovery."""
        now = _aware(self._now())
        minute = now.replace(second=0, microsecond=0)
        if self._turnover_minute != minute:
            self._turnover_minute, self._turnover_snapshot = minute, None
            started = _aware(self._now())
            try:
                self._count_query('ka10032', started)
                page = self._turnover_fetcher()
                received = _aware(self._now())
                if (any(a is not b for a,b in zip(identity,self._session_identity())) or received < started
                        or received - started > timedelta(seconds=8)
                        or received.replace(second=0,microsecond=0) != minute): return None
                rows = page.get("body", {}).get("trde_prica_upper")
                digest = page.get("wire_sha256")
                cont = page.get("headers", {}).get("cont-yn")
                if (page.get("ok") is not True or not isinstance(rows,list) or len(rows)>10000
                        or not isinstance(digest,str) or re.fullmatch(r"[0-9a-f]{64}",digest) is None
                        or cont not in ("", "N", "Y")): return None
                values, liquidity = {}, {}
                for row in rows:
                    if not isinstance(row,dict): return None
                    sym = row.get("stk_cd")
                    if not isinstance(sym,str) or re.fullmatch(r"[0-9]{6}",sym) is None: continue
                    if sym in values:
                        values[sym] = None
                        liquidity[sym] = None
                        continue
                    try:
                        amount = _decimal(row.get("trde_prica"), nonnegative=True)
                        values[sym] = int(Decimal(amount) * 1_000_000) if amount is not None else None
                        liquidity[sym] = {key:row.get(key) for key in
                            ("now_trde_qty", "pred_trde_qty", "flu_rt", "pred_pre_sig", "cur_prc")}
                    except ContextUnavailable:
                        values[sym] = None
                self._turnover_snapshot = {"values":values, "liquidity_rows":liquidity, "source_sha256":digest,
                    "request_started_at":started.isoformat(), "observed_at":received.isoformat(),
                    "available_at":received.isoformat(),"truncated":cont=="Y", "page_count":1}
            except Exception:
                return None
        return self._turnover_snapshot

    def _theme(self, code, identity):
        # User-provided static taxonomy is a grouping hypothesis, never proof
        # of company business or independently verified association.
        if self._taxonomy_path is None or self._surge_snapshot is None:
            return None
        now = _aware(self._now())
        try:
            raw = self._taxonomy_path.read_bytes()
            if len(raw) > 8 * 1024 * 1024: return None
            taxonomy = json.loads(raw)
            if (taxonomy.get("schema") != "pro6e-sector-taxonomy-v1"
                    or taxonomy.get("metadata_only") is not True): return None
            created = _aware(datetime.fromisoformat(taxonomy["created_at"]))
            if created > now: return None
            stocks = taxonomy.get("kr_stocks")
            if not isinstance(stocks, dict) or code not in stocks: return None
            themes = stocks[code].get("sub_themes")
            if not isinstance(themes, list) or not themes: return None
        except (OSError, ValueError, KeyError, TypeError, ContextUnavailable):
            return None
        turnover = self._turnover(identity)
        surge = self._surge_snapshot
        if turnover is None or _aware(datetime.fromisoformat(surge["available_at"])) > now:
            return None
        for theme in themes:
            members = []
            for sym, item in stocks.items():
                if (not isinstance(sym,str) or re.fullmatch(r"[0-9]{6}",sym) is None
                        or not isinstance(item,dict) or not isinstance(item.get("sub_themes"),list)
                        or theme not in item["sub_themes"]): continue
                entry = surge["rows"].get(sym)
                amount = turnover["values"].get(sym)
                if not entry or entry[0] is None or entry[1] is None or not amount: continue
                if Decimal(entry[0]) <= 0 or Decimal(entry[1]) <= 0: continue
                members.append({"symbol":sym,"change_pct":entry[0],"turnover_krw":amount,
                    "rising_volume_surge":True,"observed_at":surge["available_at"]})
            if len(members) >= 2 and any(m["symbol"] == code for m in members):
                return {"theme":theme,"members":members,"taxonomy_available_at":created.isoformat(),
                    "available_at":max(_aware(datetime.fromisoformat(turnover["available_at"])),
                                       _aware(datetime.fromisoformat(surge["available_at"]))).isoformat(),
                    "source_sha256":hashlib.sha256((hashlib.sha256(raw).hexdigest() + surge["source_sha256"]
                                                     + turnover["source_sha256"]).encode()).hexdigest(),
                    "universe":"intersection_of_first_krx_ranking_pages", "full_theme_leader_verified":False,
                    "taxonomy_status":"user_supplied_not_business_verified", "turnover_unit_source":"million_krw",
                    "surge_truncated":surge["truncated"],"turnover_truncated":turnover["truncated"],
                    "surge_available_at":surge["available_at"], "turnover_available_at":turnover["available_at"],
                    "taxonomy_sha256":hashlib.sha256(raw).hexdigest(), "surge_sha256":surge["source_sha256"],
                    "turnover_sha256":turnover["source_sha256"]}
        return None

    def is_fresh(self, context, now=None):
        try:
            with self._lock:
                self._bind()
                current = _aware(now if now is not None else self._now())
                start = _aware(datetime.fromisoformat(context["request_started_at"]))
                received = _aware(datetime.fromisoformat(context["available_at"]))
                return bool(context["session_binding"] == self._binding
                            and context["observed_at"] == context["available_at"]
                            and _market_time(current) and _market_time(start)
                            and start <= received <= current
                            and current - start <= MAX_AGE
                            and start.replace(second=0, microsecond=0) == current.replace(second=0, microsecond=0))
        except (ContextUnavailable, KeyError, TypeError, ValueError, OverflowError):
            return False

    def fetch(self, symbol, as_of=None):
        try:
            code = normalize_stock_code(symbol)
        except (TypeError, ValueError):
            raise ContextUnavailable("종목코드가 유효하지 않습니다.", code="symbol_invalid") from None
        # Strategies currently accept only KRX six-digit identifiers.
        if not re.fullmatch(r"[0-9]{6}", code):
            raise ContextUnavailable("KRX 종목코드가 필요합니다.", code="symbol_invalid")
        with self._lock:
            identity = self._bind()
            start = _aware(self._now())
            requested = _aware(as_of) if as_of is not None else start
            if not _market_time(start):
                raise ContextUnavailable("정규장 시각을 확인할 수 없습니다.", code="market_closed")
            if requested > start:
                raise ContextUnavailable("요청 시각이 현재보다 늦습니다.", code="clock_reversed")
            if start - requested > MAX_AGE:
                raise ContextUnavailable("요청 시각이 오래되었습니다.", code="request_stale")
            cached = self._cache.get(code)
            if cached is not None and self.is_fresh(cached, start):
                return deepcopy(cached)
            surge = self._surge(code, identity) if self._enable_surge else None
            theme = self._theme(code, identity) if surge else None
            # ka10001 must be requested after the optional ranking so its 5s
            # time window does not include ranking queue latency.
            start = _aware(self._now())
            if not _market_time(start):
                raise ContextUnavailable("정규장을 확인할 수 없습니다.", code="market_closed")
            data = self._fetcher(self._session, code)
            received = _aware(self._now())
            if (self._session_identity()[0] is not identity[0]
                    or self._session_identity()[1] is not identity[1]):
                self._identity = None
                self._cache.clear()
                raise ContextUnavailable("조회 중 모의투자 세션이 변경되었습니다.", code="session_changed")
            if received < start:
                raise ContextUnavailable("종목정보 조회 시각이 역행했습니다.", code="clock_reversed")
            if not _market_time(received):
                raise ContextUnavailable("정규장을 확인할 수 없습니다.", code="market_closed")
            if start.replace(second=0, microsecond=0) != received.replace(second=0, microsecond=0):
                raise ContextUnavailable("종목정보 조회가 분 경계를 넘었습니다.", code="minute_rollover")
            if received - start > MAX_AGE:
                raise ContextUnavailable("종목정보 조회가 늦었습니다.", code="request_stale")
            if not isinstance(data, dict) or data.get("stk_cd") != code:
                raise ContextUnavailable("응답 종목이 일치하지 않습니다.", code="symbol_mismatch")
            current = _price(data.get("cur_prc"))
            if self._exit_only:
                # Exit uses a current validated price only. High/limit and
                # optional ranking fields cannot suppress risk management.
                high = upper = change = previous_volume = day_open = None
            else:
                high, upper = (_price(data.get(key)) for key in ("high_pric", "upl_pric"))
                # ka10001 open_pric is optional. Absence blocks S2 only; it
                # must never disable owned-position protective exits.
                day_open = _price(data['open_pric']) if data.get('open_pric') not in (None, '', '0', '+0', '-0') else None
                if not current <= high <= upper:
                    raise ContextUnavailable("현재가·고가·상한가가 상충합니다.", code="price_conflict")
                if day_open is not None and day_open > high:
                    raise ContextUnavailable('시가·당일고가가 상충합니다.', code='price_conflict')
                change = _decimal(data.get("flu_rt"))
                previous_volume = _decimal(data.get("trde_pre"))
                if change is None or Decimal(change) <= 0:
                    surge, theme = None, None
            # The API client has already stripped unknown and sensitive fields.
            normalized = {key: data.get(key, "") for key in ("stk_cd", "cur_prc", "open_pric", "high_pric", "upl_pric", "flu_rt", "trde_pre")}
            digest = hashlib.sha256(json.dumps(normalized, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode()).hexdigest()
            result = {"symbol": code, "exit_only": self._exit_only, "request_started_at": start.isoformat(),
                      "observed_at": received.isoformat(), "available_at": received.isoformat(),
                      "source": "ka10001_normalized_response", "source_sha256": digest,
                      "source_market_timestamp": None, "source_freshness_verified": False,
                      "session_binding": self._binding, "current_price": current, "day_high": high,
                      "day_open": day_open,
                      "intraday_bullish_candle": intraday_bullish_candle_state(current, day_open),
                      "upper_limit_price": upper, "change_pct": change,
                      "previous_day_volume_ratio_pct": previous_volume,
                      "rising_volume_surge": True if surge else None, "surge_evidence": surge,
                      "same_time_rvol20": None,
                      "theme_evidence": theme, "dart_evidence": None}
            self._cache[code] = result
            return deepcopy(result)
