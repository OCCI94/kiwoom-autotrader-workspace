"""Observed-state preparation and non-blocking advisory decision coordination.

No synthetic ticks, new broker queries, orders or risk overrides. Background
news scores are model opinions; a missing score uses existing deterministic
rules. Final broker/ledger reconciliation is intentionally not bypassed.
"""
from datetime import datetime, timedelta
import hashlib
import json
import math
import os
from pathlib import Path
import stat
import threading
import time

from kiwoom_autotrader.api.session import KST
from kiwoom_autotrader.utils.paths import app_data_root
from .jev_fast_decision import compact_fast_state, AsyncJevDecision
from .jev_news_client import JevNewsClient


def estimated_order_ticket(account, price, budget):
    """Observed cash/principal upper bound, never a reservation or permission."""
    row=dict(account)
    row['estimated_initial_qty']=None;row['estimated_remaining_qty']=None
    required=(price,budget,row.get('available_cash_krw'),row.get('remaining_principal_krw'))
    if (row.get('status')!='observed_advisory' or row.get('reserved_principal_krw')!=0
            or any(type(v) is not int or v<0 for v in required) or price<=0):return row
    capacity=min(row['available_cash_krw'],row['remaining_principal_krw'],1000000)
    # Fees, live price and final reconciliations can only reduce these bounds.
    row['estimated_remaining_qty']=capacity//price
    if row.get('observed_held_qty')==0:
        row['estimated_initial_qty']=min(capacity,budget,500000)//price
    row['quantity_kind']='observed_upper_bound_before_fees_and_final_reconciliation'
    row['order_authorized']=False
    return row


def numeric_state(market, evaluation, catalyst=None):
    def number(value):
        if isinstance(value,bool) or value is None: return None
        try: value=float(value)
        except (ValueError,TypeError,OverflowError): return None
        return round(value,6) if math.isfinite(value) and abs(value)<=10**13 else None
    vw=evaluation.get('vwap') or {};num=number(vw.get('hlc3_volume_numerator'));vol=number(vw.get('volume'))
    price=number(market.get('current_price'));vwap=num/(3*vol) if num and vol and vol>0 else None
    short=evaluation.get('short_selling') or {}
    probs=catalyst.get('probabilities') if isinstance(catalyst,dict) and catalyst.get('source')=='jev_cache' else {}
    probs=probs or {}
    data={'price_vwap_bps':(price/vwap-1)*10000 if price and vwap else None,
        'five_minute_slope_bps':number(market.get('five_minute_slope_bps')),
        'spread_bps':number(market.get('spread_bps')),
        'ask_bid_ratio':number(market.get('ask_bid_imbalance_ratio')),
        'foreign_net_qty':number(market.get('foreign_net_qty')),
        'institution_net_qty':number(market.get('institution_net_qty')),
        'short_amount_share':number(short.get('max_daily_short_amount_share'))}
    data.update({'catalyst_'+k:number(probs.get(k)) for k in ('direction','relevance','evidence','importance')})
    return compact_fast_state(data)


def source_config(path):
    fd=os.open(path,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0)|getattr(os,'O_NONBLOCK',0))
    try:
        meta=os.fstat(fd)
        if (not stat.S_ISREG(meta.st_mode) or meta.st_mode&0o022 or meta.st_size>16384
                or hasattr(os,'getuid') and meta.st_uid!=os.getuid()): raise ValueError('source_config_unsafe')
        raw=os.read(fd,16385)
    finally: os.close(fd)
    value=json.loads(raw)
    if (not isinstance(value,dict) or value.get('schema')!='paper-fast-decision-sources-v1'
            or not isinstance(value.get('master_sha256'),str) or len(value['master_sha256'])!=64):
        raise ValueError('source_config_invalid')
    base=app_data_root()/'config/research'
    if Path(value.get('master_path',''))!=base/'telegram-symbol-master.json':raise ValueError('source_master_scope')
    archive=Path(value.get('telegram_archive',''))
    if (not archive.is_absolute() or '..' in archive.parts or archive.suffix!='.sqlite3'
            or not archive.is_relative_to('/Volumes/AIStockData/telegram-channel-archive')):
        raise ValueError('source_archive_scope')
    if Path(value.get('dart_status_path',''))!=base/'toss-evidence-refresh-status.json':
        raise ValueError('source_dart_scope')
    return value


class FastDecisionCoordinator:
    def __init__(self, *, client_factory=None, source_path=None, clock=time.perf_counter_ns, now=None):
        self._client_factory=client_factory or (lambda:JevNewsClient(keep_alive=True,deadline_ms=300))
        self._source_path=Path(source_path) if source_path else app_data_root()/'config/research/fast-decision-sources.json'
        self._clock=clock;self._now=now or (lambda:datetime.now(KST))
        self._session=None;self._stop=None;self._thread=None;self._decision=None
        self._requests={};self._latest_catalysts={};self._account_source=None;self._account_snapshot=(None,{})
        self._status={'stage':'not_started','advisory_only':True,'order_authority':False,'kiwoom_calls':0}

    def start(self,session_id):
        if self._session==session_id and self._stop is not None and not self._stop.is_set():return
        self.reset();self._session=session_id;stop=threading.Event();self._stop=stop
        self._thread=threading.Thread(target=self._run,args=(session_id,stop),name='paper-pre-scoring',daemon=True)
        self._thread.start()

    def reset(self):
        if self._stop is not None:self._stop.set()
        self._session=None;self._decision=None;self._requests={};self._latest_catalysts={};self._account_snapshot=(None,{});self._account_source=None
        # No network/queue join/worker lock on the receiver path.

    def set_account_source(self,tracker,symbols):
        self._account_source=(tracker,tuple(symbols))

    def account_for(self,symbol,now):
        session,rows=self._account_snapshot
        row=rows.get(symbol) if session==self._session else None
        try:
            return dict(row) if row and datetime.fromisoformat(row['prepared_at'])<=now<datetime.fromisoformat(row['expires_at']) else None
        except (KeyError,TypeError,ValueError):return None

    def _run(self,session,stop):
        client=decision=scorer=tail=dart=index=None;seen={};last_events=last_account=0.
        try:
            client=self._client_factory();decision=AsyncJevDecision(client=client,deadline_ms=300,now=self._now)
            if self._session!=session:return
            self._decision=decision
            self._status={'stage':'running','credentials_present':client.available,'advisory_only':True,
                'order_authority':False,'kiwoom_calls':0,'deadline_ms':300,'keep_alive':True,'source_stage':'unavailable'}
            try:
                from .catalyst_pre_scoring import load_linker,TelegramArchiveTail,CatalystPreScorer,DartOriginalQueueTail
                cfg=source_config(self._source_path)
                linker=load_linker(cfg['master_path'],cfg['master_sha256'])
                scorer=CatalystPreScorer(linker,decision.submit,session_id=session,ttl_seconds=90)
                tail=TelegramArchiveTail(cfg['telegram_archive'])
                dart=DartOriginalQueueTail(cfg['dart_status_path'])
                if stop.is_set() or self._session!=session:return
                self._status={**self._status,'source_stage':'configured'}
            except Exception:
                if stop.is_set() or self._session!=session:return
                self._status={**self._status,'source_stage':'source_contract_unavailable'}
            try:
                from .prearmed_account_index import BackgroundAccountIndex
                index=BackgroundAccountIndex()
            except ImportError:pass
            while not stop.is_set() and self._session==session:
                now=self._now();mono=time.monotonic()
                if scorer is not None and mono-last_events>=.5:
                    last_events=mono
                    try:
                        scorer.run_once(tail,now=now)
                        for event in dart.read_new(now=now):scorer.consume_dart(event,now=now)
                        latest={}
                        for symbol in tuple(scorer._latest):
                            item=scorer.latest_for(symbol,now=now)
                            if item:latest[symbol]=item
                        if stop.is_set() or self._session!=session:break
                        self._latest_catalysts=latest
                        self._status={**self._status,'source_stage':'observing','telegram':dict(tail.last_status),
                            'dart':dict(dart.last_status),'catalyst_counts':dict(scorer.counts),
                            'last_source_read_at':now.isoformat()}
                    except Exception:
                        if stop.is_set() or self._session!=session:break
                        self._latest_catalysts={}
                        self._status={**self._status,'source_stage':'read_unavailable'}
                if index is not None and self._account_source is not None and mono-last_account>=2:
                    last_account=mono
                    try:
                        tracker,symbols=self._account_source;index.refresh(tracker,symbols,now)
                        account={s:index.lookup(s,now) for s in symbols}
                        if stop.is_set() or self._session!=session:break
                        self._account_snapshot=(session,account)
                    except Exception:
                        if stop.is_set() or self._session!=session:break
                        self._account_snapshot=(session,{})
                for symbol,item in tuple(self._requests.items()):
                    if seen.get(symbol)==item['snapshot_id']:continue
                    accepted=decision.submit(kind='fast',symbol=symbol,session_id=session,
                        snapshot_id=item['snapshot_id'],state=item['state'],observed_at=item['observed_at'],expires_at=item['expires_at'])
                    if accepted or not decision.available or now>=item['expires_at']:seen[symbol]=item['snapshot_id']
                stop.wait(.01)
        except Exception:
            if self._session==session:self._status={**self._status,'stage':'deterministic_fallback','reason':'background_unavailable'}
        finally:
            if decision is not None:decision.close()
            if client is not None:client.close()

    def observe(self,*,market,evaluation,symbol,observed,session_id,budget):
        self.start(session_id)
        decision=self._decision;news=None;item=self._latest_catalysts.get(symbol)
        if decision is not None and item:
            news=decision.lookup(kind='news',symbol=symbol,session_id=session_id,snapshot_id=item['snapshot_id'],now=observed)
        try:
            state=numeric_state(market,evaluation,news)
            raw=json.dumps(state,separators=(',',':'),sort_keys=True).encode('ascii')
            identity=hashlib.sha256(raw+observed.isoformat().encode()).hexdigest()
            self._requests={**self._requests,symbol:{'snapshot_id':identity,'state':state,
                'observed_at':observed,'expires_at':observed+timedelta(seconds=2)}}
            if len(self._requests)>40:self._requests={symbol:self._requests[symbol]}
        except (ValueError,TypeError,OverflowError):
            return {'source':'deterministic_fallback','status':'state_unavailable','order_authority':False}
        started=self._clock()
        result=decision.lookup(kind='fast',symbol=symbol,session_id=session_id,snapshot_id=identity,now=observed) if decision else {
            'source':'deterministic_fallback','status':'background_starting','probabilities':None,'advisory_only':True}
        result={**result,'local_lookup_ms':(self._clock()-started)/1e6,'state_ascii_bytes':len(raw),
            'numeric_state':state,'catalyst_cached':bool(news and news.get('source')=='jev_cache'),
            'order_authority':False,'budget_upper_bound_krw':budget}
        self._status={**self._status,'last_symbol':symbol,'last_observed_at':observed.isoformat(),'last_fast_gate':result}
        return result

    def status(self):
        session,rows=self._account_snapshot
        return {**self._status,'account_preparation':dict(rows) if session==self._session else {}}

    def decision_for(self,symbol,now):
        """Read an already submitted generation during the canonical gateway.

        The gateway's normal work may let an async answer arrive. It is never
        delayed to obtain that answer, and the answer is never order authority.
        """
        started=self._clock();item=self._requests.get(symbol);decision=self._decision
        if item is None or decision is None or not item['observed_at']<=now<item['expires_at']:
            result={'source':'deterministic_fallback','status':'missing_or_stale','probabilities':None}
        else:
            result=decision.lookup(kind='fast',symbol=symbol,session_id=self._session,snapshot_id=item['snapshot_id'],now=now)
        return {**result,'local_lookup_ms':(self._clock()-started)/1e6,'advisory_only':True,'order_authority':False}
