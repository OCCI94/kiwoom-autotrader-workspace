"""Paper experiment observations on the receiver's existing queued session.

No independent login, synthetic bars, or inferred zero-volume observations.
Pagination proves only returned master rows; absent/failed inputs stay unknown.
"""
from datetime import datetime, timedelta
from copy import deepcopy
from decimal import Decimal
import hashlib
import json
import re
import time
import urllib.request

from kiwoom_autotrader.api.stock_info import fetch_stock_info
from kiwoom_autotrader.api.minute_chart import session_identity
from kiwoom_autotrader.api.session import KST
from kiwoom_autotrader.utils.http_queue import queued_open
from .dynamic_candidates import _normal_member


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=True).encode()).hexdigest()


def number(value, *, positive=False):
    if not isinstance(value,(str,int)) or isinstance(value,bool) or not re.fullmatch(r'[+-]?[0-9]+',str(value)):
        raise ValueError('numeric_input_unverified')
    value=abs(int(value))
    if positive and value<=0:raise ValueError('positive_price_required')
    return value


def normalize_daily(response, symbol, now):
    observed=datetime.fromisoformat(response['observed_at'])
    if (observed.tzinfo is None or observed.utcoffset()!=timedelta(hours=9) or observed>now
            or observed.date()!=now.date() or not isinstance(response.get('source_sha256'),str)
            or re.fullmatch(r'[0-9a-f]{64}',response['source_sha256']) is None):
        raise ValueError('daily_source_time_or_hash_invalid')
    rows=response['rows']; dated={}
    if response.get('symbol')!=symbol or not isinstance(rows,list) or not 21<=len(rows)<=2000:
        raise ValueError('daily_rows_unavailable')
    # Restrict calculations to the actual preceding 21 returned sessions.
    # Ancient corporate actions outside that window must not poison today's inputs.
    prior=[r for r in rows if isinstance(r,dict) and isinstance(r.get('dt'),str) and r['dt']<now.strftime('%Y%m%d')]
    for row in sorted(prior,key=lambda r:r['dt'])[-21:]:
        day=row.get('dt','');date=datetime.strptime(day,'%Y%m%d').date()
        if day in dated or date>now.date():raise ValueError('daily_dates_invalid')
        if date==now.date():continue
        if row.get('upd_stkpc_tp') not in ('',None) or row.get('upd_rt') not in ('',None):
            raise ValueError('daily_corporate_action_unadjusted')
        sign=row.get('pred_pre_sig')
        limit_up=True if sign=='1' else False if sign in ('2','3','4','5') else None
        r={'date':day,'open':number(row['open_pric'],positive=True),'high':number(row['high_pric'],positive=True),
           'low':number(row['low_pric'],positive=True),'close':number(row['cur_prc'],positive=True),
           'volume':number(row['trde_qty']),'turnover_krw':number(row['trde_prica'])*1_000_000,
           'limit_up':limit_up}
        if not r['low']<=min(r['open'],r['close'])<=max(r['open'],r['close'])<=r['high']:
            raise ValueError('daily_ohlc_invalid')
        dated[day]=r
    ordered=[dated[k] for k in sorted(dated)][-121:]
    if len(ordered)<21:raise ValueError('prior_21_sessions_required')
    return {'symbol':symbol,'mode':'paper','source_api':'ka10081','source_sha256':response['source_sha256'],
        'available_at':response['observed_at'],'session_day':now.strftime('%Y%m%d'),'rows':ordered,
        'currency':'KRW','exchange':'KRX','price_basis':'unadjusted_without_corporate_action_markers',
        'normalized_rows_sha256':digest(ordered),
        'previous_close':ordered[-1]['close'],'previous_limit_up':ordered[-1]['limit_up'],
        'avg_turnover20_krw':sum(r['turnover_krw'] for r in ordered[-20:])/20,
        'avg_volume20':sum(r['volume'] for r in ordered[-20:])/20,
        'momentum20':ordered[-1]['close']/ordered[-21]['close']-1,
        'momentum_definition':'prior_close_div_21st_prior_session_close_minus_one',
        'turnover_source_unit':'million_KRW_converted_to_KRW'}


def common_tick(price):
    # KRX common-equity price grid, checked 2026-11-02:
    # https://regulation.krx.co.kr/contents/RGL/03/03020100/RGL03020100.jsp
    if type(price) is not int or price<=0:raise ValueError('tick_price_invalid')
    for edge,unit in ((2000,1),(5000,5),(20000,10),(50000,50),(200000,100),(500000,500)):
        if price<edge:return unit
    return 1000


def build_admission(symbol,arm,market,daily,now):
    from .paper_experiment_admission import validate_admission
    value={'schema':'paper-experiment-admission-v1','symbol':symbol,'arm':arm,'observed_at':now.isoformat(),
        'instrument':{'source_sha256':market.get('classification_source_sha256'),
            'available_at':market.get('classification_observed_at'),'common_share':market.get('is_common_stock'),
            'status':'normal' if market.get('normal') is True else 'unknown'},
        'turnover':{'source_sha256':daily.get('source_sha256'),'available_at':daily.get('available_at'),
            'complete':True,'days':[{'date':r['date'],'turnover_krw':r['turnover_krw']} for r in daily['rows'][-20:]]},
        'vi':{'source_sha256':market.get('vi_source_sha256'),'available_at':market.get('vi_observed_at'),
            'complete':market.get('vi_complete'),'active':market.get('vi_active'),'last_trigger_at':market.get('vi_last_trigger_at'),
            'last_trigger_basis':market.get('vi_last_trigger_basis')},
        'price':{'source_sha256':market['source_sha256'],'available_at':market['observed_at'],
            'current_price':market['current_price'],'upper_limit_price':market['upper_limit_price']}}
    validate_admission(value,symbol,arm,now)
    return value


class ExperimentInputs:
    def __init__(self,session,provider,store,*,clock=None,requester=None,quote_fetcher=None,
                 preopen_plan_reader=None,history_reader=None):
        self.session=session;self.provider=provider;self.store=store
        self.now=clock or (lambda:datetime.now(KST));self.requester=requester or self._request
        self.quote_fetcher=quote_fetcher or fetch_stock_info
        self.identity=session_identity(session);self.day=None;self.daily_cache={};self.daily_failed=set()
        self.master={};self.master_cursor={'0':('',0),'10':('',0)};self.master_complete=set()
        self.master_failed=set();self.vi=None;self.vi_attempt_at=None;self.vi_diagnostic=None;self.quotes={};self.master_epoch=None;self.master_seen=set()
        from .paper_experiment_calendar import preopen_plan
        from .paper_experiment_volume_history import SavedSameWindowHistory
        self.preopen_plan_reader=preopen_plan_reader or preopen_plan
        self.history_reader=history_reader or SavedSameWindowHistory()
        self.preopen_plan=None;self.daily_diagnostics={};self.history_diagnostics={};self.history_cache={}
        self.selection_prefetch_window=None;self.selection_prefetched=set();self.selection_prefetch_diagnostics={}

    def _bind(self):
        if self.session.mode!='paper' or session_identity(self.session)!=self.identity:raise ValueError('paper_session_changed')
        day=self.now().strftime('%Y%m%d')
        if self.day!=day:
            self.day=day;self.daily_cache={};self.daily_failed=set();self.master={}
            self.master_cursor={'0':('',0),'10':('',0)};self.master_complete=set();self.master_failed=set()
            self.vi=None;self.vi_attempt_at=None;self.vi_diagnostic=None;self.quotes={};self.master_epoch=self.now();self.master_seen=set()
            self.preopen_plan=None;self.daily_diagnostics={};self.history_diagnostics={};self.history_cache={}
            self.selection_prefetch_window=None;self.selection_prefetched=set();self.selection_prefetch_diagnostics={}

    def prepare_selection_one(self,symbols):
        """Warm one quote near GDR/ORB selection; never cache unfinished bars.

        Classification must already be complete. The selection reader keeps
        original timestamps and the existing60second snapshot limit. Actual
        order preparation still calls force=True and enforces its5second gate.
        """
        self._bind();now=self.now();hm=now.strftime('%H:%M')
        if hm not in ('08:58','09:04') or now.second<15 or self.master_complete!={'0','10'}:return False
        key=now.strftime('%Y%m%d%H%M')
        if self.selection_prefetch_window!=key:
            self.selection_prefetch_window=key;self.selection_prefetched=set()
        for symbol in list(dict.fromkeys(symbols))[:40]:
            if symbol in ('069500','229200') or symbol in self.selection_prefetched:continue
            self.selection_prefetched.add(symbol)
            try:
                quote=self.market(symbol,force=True)
                self.selection_prefetch_diagnostics[symbol]={'window':hm,'status':'observed',
                    'observed_at':quote['observed_at'],'source_sha256':quote['source_sha256']}
            except Exception as error:
                self.selection_prefetch_diagnostics[symbol]={'window':hm,'status':'unavailable',
                    'observed_at':self.now().isoformat(),'reason':type(error).__name__}
            return True
        return False

    def selection_market(self,symbol,now):
        """Snapshot-only reuse; no timestamp relabeling or order permission."""
        self._bind();old=self.quotes.get(symbol)
        if isinstance(old,dict):
            at=datetime.fromisoformat(old['observed_at'])
            if at.date()==now.date() and 0<=(now-at).total_seconds()<=60:
                return deepcopy(old)
        return self.market(symbol)

    def _cached_daily(self,symbol):
        cached=self.daily_cache.get(symbol) or self.store.selection(self.day,'daily:'+symbol)
        if cached is None:return None
        from .paper_experiment_extended_rules import daily_features
        daily_features(cached,symbol=symbol,now=self.now())
        if (cached.get('session_day')!=self.day or cached.get('normalized_rows_sha256')!=digest(cached['rows'])
                or datetime.fromisoformat(cached['available_at']).date()!=self.now().date()):
            raise ValueError('daily_cache_identity_or_integrity')
        self.daily_cache[symbol]=cached
        return cached

    def prepare_preopen_one(self,symbols):
        """One prior-session daily request, ahead of full master pagination.

        The actual 08:30~08:55 observation is retained when later used at 08:59
        or 09:00. A saved official plan is permission to prepare read inputs,
        not permission to select, trade or assert that the market is open.
        """
        self._bind();now=self.now()
        if not '08:30'<=now.strftime('%H:%M')<='08:55':return False
        if self.preopen_plan is None:self.preopen_plan=self.preopen_plan_reader(now)
        plan=self.preopen_plan
        if (not isinstance(plan,dict) or plan.get('session_day')!=self.day
                or plan.get('live_regular_session_verified') is not False):
            self.daily_diagnostics['_preopen']='official_previous_session_plan_unavailable';return False
        previous=plan.get('previous_session_day')
        if (not isinstance(previous,str) or not re.fullmatch('[0-9]{8}',previous)
                or previous>=self.day or datetime.fromisoformat(plan['available_at'])>now):
            self.daily_diagnostics['_preopen']='official_previous_session_plan_invalid';return False
        for symbol in symbols:
            if not isinstance(symbol,str) or not re.fullmatch('[0-9]{6}',symbol):continue
            if symbol in self.daily_failed:continue
            try:
                if self._cached_daily(symbol) is not None:continue
                response=self.requester('ka10081',{'stk_cd':symbol,'base_dt':previous,'upd_stkpc_tp':'0'},'stk_dt_pole_chart_qry')
                received=self.now();daily=normalize_daily(response,symbol,received)
                observed=datetime.fromisoformat(daily['available_at'])
                if (received.strftime('%Y%m%d')!=self.day or not now<=observed<=received
                        or not '08:30'<=observed.strftime('%H:%M')<='08:55'
                        or daily['rows'][-1]['date']!=previous):raise ValueError('preopen_prior_session_or_window_unverified')
                daily.update(observation_phase='preopen',requested_base_day=previous,
                    prior_session_plan=plan,preopen_observation_relabelled=False)
                saved=self.store.selection(self.day,'daily:'+symbol,daily)
                if saved is None:raise ValueError('preopen_daily_not_persisted')
                self.daily_cache[symbol]=saved;self._cached_daily(symbol)
                self.daily_diagnostics[symbol]='prior_completed_session_cached_preopen'
            except Exception as error:
                self.daily_failed.add(symbol)
                self.daily_diagnostics[symbol]=str(error) if isinstance(error,ValueError) else type(error).__name__
            return True
        return False

    def _request(self,api,body,output,*,next_key=''):
        self._bind();identity=self.identity;start=self.now();deadline=time.monotonic()+8
        endpoints={'ka10081':'/api/dostk/chart','ka10099':'/api/dostk/stkinfo','ka10054':'/api/dostk/stkinfo'}
        if api not in endpoints:raise ValueError('experiment_read_api_not_allowed')
        def guard():
            if session_identity(self.session)!=identity or self.session.mode!='paper' or time.monotonic()>deadline:
                raise ValueError('experiment_request_expired')
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self,*a,**kw):return None
        headers={'Content-Type':'application/json;charset=UTF-8','authorization':'Bearer '+self.session.token,'api-id':api}
        if next_key:headers.update({'cont-yn':'Y','next-key':next_key})
        request=urllib.request.Request('https://mockapi.kiwoom.com'+endpoints[api],data=json.dumps(body).encode(),headers=headers,method='POST')
        with queued_open(urllib.request.build_opener(NoRedirect()),request,timeout=8,max_bytes=2*1024*1024,
                         before_send=guard,priority='research') as response:
            raw=response.read(2*1024*1024+1);cont=response.headers.get('cont-yn','');key=response.headers.get('next-key','')
        guard();value=json.loads(raw)
        if (len(raw)>2*1024*1024 or value.get('return_code') not in (0,'0') or not isinstance(value.get(output),list)
                or cont not in ('','N','Y') or len(key)>256):raise ValueError('experiment_response_invalid')
        return {'symbol':value.get('stk_cd'),'rows':value[output],'source_sha256':hashlib.sha256(raw).hexdigest(),
                'request_started_at':start.isoformat(),'observed_at':self.now().isoformat(),'cont_yn':cont,'next_key':key}

    def prepare_one(self,symbols):
        """At most one master page or prior daily request per listener cycle."""
        self._bind();now=self.now()
        if self.master_epoch and now-self.master_epoch>=timedelta(seconds=240):
            self.master_cursor={'0':('',0),'10':('',0)};self.master_complete=set();self.master_failed=set();self.master_epoch=now;self.master_seen=set()
        for market in ('0','10'):
            if market in self.master_complete|self.master_failed:continue
            key,count=self.master_cursor[market]
            try:
                if count>=60:raise ValueError('master_page_bound')
                result=self.requester('ka10099',{'mrkt_tp':market},'list',next_key=key)
                for row in result['rows']:
                    symbol=row.get('code')
                    if not isinstance(symbol,str) or not re.fullmatch(r'\d{6}',symbol):continue
                    clean={k:row.get(k) for k in ('code','name','marketCode','auditInfo','state','orderWarning','upSizeName','companyClassName')}
                    common=_normal_member(clean,market) is None and symbol.endswith('0') and symbol not in self.master_seen
                    self.master_seen.add(symbol)
                    self.master[symbol]={'common_share':common,'normal':common,'source_sha256':result['source_sha256'],
                        'available_at':result['observed_at'],'market':market,'raw':clean}
                if result['cont_yn']=='Y':
                    if not result['next_key'] or result['next_key']==key:raise ValueError('master_continuation_invalid')
                    self.master_cursor[market]=(result['next_key'],count+1)
                else:self.master_complete.add(market)
            except Exception:self.master_failed.add(market)
            return
        for symbol in symbols:
            if symbol in self.daily_cache or symbol in self.daily_failed:continue
            cached=self.store.selection(self.day,'daily:'+symbol)
            if cached is not None:
                self.daily_cache[symbol]=cached;return
            try:
                result=self.requester('ka10081',{'stk_cd':symbol,'base_dt':self.day,'upd_stkpc_tp':'0'},'stk_dt_pole_chart_qry')
                daily=normalize_daily(result,symbol,self.now())
                self.daily_cache[symbol]=self.store.selection(self.day,'daily:'+symbol,daily)
            except Exception:self.daily_failed.add(symbol)
            return

    def daily(self,symbol):
        self._bind()
        try:return self._cached_daily(symbol)
        except (ValueError,TypeError,KeyError,AttributeError) as error:
            self.daily_diagnostics[symbol]=str(error) if isinstance(error,ValueError) else 'daily_cache_unverified'
            return None

    def same_window_history(self,symbol,now):
        self._bind();daily=self.daily(symbol)
        if daily is None:return None
        key=(symbol,tuple(r['date'] for r in daily['rows'][-14:]))
        if key not in self.history_cache:
            self.history_cache[key]=self.history_reader.read(symbol,list(key[1]),now)
        result=self.history_cache[key]
        self.history_diagnostics[symbol]={k:result.get(k) for k in
            ('status','reason','expected_sessions','observed_sessions','missing_sessions','invalid_sessions')}
        return result

    def _observe_upper_limit(self,symbol,market):
        """Persist an actual daily upper-price observation, never a 30% model.

        Pre-open values can still describe the previous session; only a fresh
        same-day regular-session query is eligible for this future-day evidence.
        A conflicting later observation permanently marks that day's basis
        unverified; neither original observation is overwritten.
        """
        try:
            at=datetime.fromisoformat(market['observed_at']);request=datetime.fromisoformat(market['request_started_at'])
            upper=market['upper_limit_price'];current=market['current_price']
            if (at.utcoffset()!=timedelta(hours=9) or request.utcoffset()!=timedelta(hours=9)
                    or at.strftime('%Y%m%d')!=self.day or not request<=at<=self.now()
                    or (at-request).total_seconds()>8 or not '09:00'<=at.strftime('%H:%M')<='15:30'
                    or market.get('symbol')!=symbol or market.get('mode')!='paper' or market.get('source_api')!='ka10001'
                    or not isinstance(market.get('source_sha256'),str) or re.fullmatch('[0-9a-f]{64}',market['source_sha256']) is None
                    or type(upper) is not int or upper<=0 or type(current) is not int or not 0<current<=upper):
                return 'current_session_upper_limit_unobserved'
            value={'schema':'paper-observed-daily-upper-limit-v1','symbol':symbol,'day':self.day,
                'mode':'paper','currency':'KRW','exchange':'KRX','source_api':'ka10001',
                'upper_limit_price':upper,'observed_at':at.isoformat(),'request_started_at':request.isoformat(),
                'source_sha256':market['source_sha256'],'basis':'actual_regular_session_upl_pric'}
            value['evidence_sha256']=digest(value)
            original=self.store.selection(self.day,'observed_upper_limit:'+symbol,value)
            if original is None:return 'upper_limit_observation_not_persisted'
            try:self._verify_upper_limit(original,symbol,self.day,self.now())
            except (ValueError,TypeError,KeyError):
                return 'upper_limit_observation_integrity_failure'
            if original['upper_limit_price']!=upper:
                conflict={'schema':'paper-observed-daily-upper-limit-conflict-v1','symbol':symbol,'day':self.day,
                    'observed_at':at.isoformat(),'original':original,'conflicting':value}
                conflict['evidence_sha256']=digest(conflict)
                self.store.selection(self.day,'observed_upper_limit_conflict:'+symbol,conflict)
                return 'conflicting_daily_upper_limits'
            if self.store.selection(self.day,'observed_upper_limit_conflict:'+symbol) is not None:
                return 'conflicting_daily_upper_limits'
            return 'actual_upper_limit_preserved'
        except (ValueError,KeyError,TypeError,AttributeError):return 'current_session_upper_limit_unobserved'

    @staticmethod
    def _verify_upper_limit(value,symbol,day,now):
        at=datetime.fromisoformat(value['observed_at']);request=datetime.fromisoformat(value['request_started_at'])
        if (value.get('schema')!='paper-observed-daily-upper-limit-v1' or value.get('symbol')!=symbol
                or value.get('day')!=day or value.get('mode')!='paper' or value.get('currency')!='KRW'
                or value.get('exchange')!='KRX' or value.get('source_api')!='ka10001'
                or value.get('basis')!='actual_regular_session_upl_pric'
                or value.get('evidence_sha256')!=digest({k:v for k,v in value.items() if k!='evidence_sha256'})
                or not isinstance(value.get('source_sha256'),str) or re.fullmatch('[0-9a-f]{64}',value['source_sha256']) is None
                or type(value.get('upper_limit_price')) is not int or value['upper_limit_price']<=0
                or at.utcoffset()!=timedelta(hours=9) or request.utcoffset()!=timedelta(hours=9)
                or at.strftime('%Y%m%d')!=day or request.date()!=at.date() or not request<=at<=now
                or (at-request).total_seconds()>8 or not '09:00'<=at.strftime('%H:%M')<='15:30'):
            raise ValueError('upper_limit_evidence_invalid')
        return value

    def _prior_limit_up(self,symbol,daily,now):
        """Use daily sign when explicit, otherwise compare exact actual prices."""
        try:
            if (not isinstance(daily,dict) or daily.get('symbol')!=symbol or daily.get('mode')!='paper'
                    or daily.get('source_api')!='ka10081' or daily.get('currency')!='KRW' or daily.get('exchange')!='KRX'
                    or daily.get('price_basis')!='unadjusted_without_corporate_action_markers'
                    or daily.get('normalized_rows_sha256')!=digest(daily.get('rows'))
                    or daily.get('session_day')!=now.strftime('%Y%m%d')):
                raise ValueError('daily_price_basis_unverified')
            from .paper_experiment_extended_rules import daily_features
            daily_features(daily,symbol=symbol,now=now)
            available=datetime.fromisoformat(daily['available_at']);row=daily['rows'][-1]
            previous=datetime.strptime(row['date'],'%Y%m%d').replace(tzinfo=KST)
            if available.date()!=now.date() or available<previous.replace(hour=15,minute=30):
                raise ValueError('final_prior_daily_close_unverified')
            close=row['close'];sign=row.get('limit_up')
            if (type(close) is not int or close<=0 or daily.get('previous_close')!=close
                    or sign not in (True,False,None) or type(sign) not in (bool,type(None))
                    or daily.get('previous_limit_up') is not sign):
                raise ValueError('prior_close_or_sign_mismatch')
            day=row['date'];original=self.store.selection(day,'observed_upper_limit:'+symbol)
            if self.store.selection(day,'observed_upper_limit_conflict:'+symbol) is not None:
                return None,'prior_upper_limit_conflicting',None
            if original is None:
                return (sign,'explicit_ka10081_daily_sign',None) if type(sign) is bool else (
                    None,'first_day_prior_upper_limit_unobserved',None)
            original=self._verify_upper_limit(original,symbol,day,now)
            upper=original['upper_limit_price']
            if close>upper:return None,'prior_close_above_observed_upper_limit',original['evidence_sha256']
            compared=close==upper
            if type(sign) is bool and sign is not compared:
                return None,'daily_sign_and_upper_limit_conflict',original['evidence_sha256']
            return compared,'final_prior_close_vs_actual_observed_upper_limit',original['evidence_sha256']
        except (ValueError,KeyError,TypeError,AttributeError,ArithmeticError):
            return None,'prior_limit_price_evidence_unverified',None

    def refresh_vi(self,*,force=False):
        self._bind();now=self.now()
        if not force and self.vi_attempt_at and (now-self.vi_attempt_at).total_seconds()<30:return self.vi
        self.vi_attempt_at=now;self.vi=None
        try:
            result=self.requester('ka10054',{'mrkt_tp':'000','bf_mkrt_tp':'0','stk_cd':'','motn_tp':'0',
                'skip_stk':'000000000','trde_qty_tp':'0','min_trde_qty':'','max_trde_qty':'','trde_prica_tp':'0',
                'min_trde_prica':'','max_trde_prica':'','motn_drc':'0','stex_tp':'1'},'motn_stk')
            if result['cont_yn']=='Y':raise ValueError('vi_incomplete')
            self.vi=result
            self.vi_diagnostic={'status':'observed','observed_at':result['observed_at'],
                'source_sha256':result['source_sha256'],'complete':True}
        except Exception as error:
            text=str(error)
            self.vi_diagnostic={'status':'unavailable','attempted_at':now.isoformat(),
                'reason':text if isinstance(error,ValueError) and re.fullmatch('[a-z0-9_]{1,100}',text)
                    else type(error).__name__,'exception_type':type(error).__name__,'complete':False,
                'missing_is_not_clear':True}
        return self.vi

    def vi_state(self,symbol):
        """Get fresh complete VI evidence without fetching a stock quote.

        The documented processing time is not asserted to be the VI trigger.
        We conservatively wait 120 seconds after the observed release.
        """
        self._bind()
        unknown={'vi_active':None,'vi_complete':False,'vi_observed_at':None,
                 'vi_source_sha256':None,'vi_last_trigger_at':None,
                 'vi_last_trigger_basis':'release_time_conservative_120s_not_trigger_assertion',
                 'vi_processing_times':[]}
        if not isinstance(symbol,str) or re.fullmatch(r'[0-9]{6}',symbol) is None:return unknown
        vi=self.refresh_vi()
        try:
            if not isinstance(vi,dict) or vi.get('cont_yn') not in ('','N') or not isinstance(vi.get('rows'),list):return unknown
            digest=vi.get('source_sha256')
            if not isinstance(digest,str) or re.fullmatch(r'[0-9a-f]{64}',digest) is None:return unknown
            vi_at=datetime.fromisoformat(vi['observed_at']);now=self.now()
            if (vi_at.tzinfo is None or vi_at.utcoffset() is None or vi_at.astimezone(KST).date()!=now.astimezone(KST).date()
                    or not 0<=(now-vi_at).total_seconds()<=30):return unknown
            active=False;last_release=None;processing=[]
            for row in vi['rows']:
                if not isinstance(row,dict) or not isinstance(row.get('stk_cd'),str):return unknown
                code=row['stk_cd']
                if re.fullmatch(r'A?[0-9]{6}',code) is None:return unknown
                if code[-6:]!=symbol:continue
                if len(processing)>=100:return unknown
                raw=row.get('trde_cntr_proc_time')
                processing.append(raw if isinstance(raw,str) and re.fullmatch(r'[0-9]{6}',raw) else None)
                release=row.get('virelis_time')
                if not isinstance(release,str) or not re.fullmatch(r'[0-9]{6}',release) or release=='000000':
                    active=True;continue
                try:end=datetime.strptime(self.day+release,'%Y%m%d%H%M%S').replace(tzinfo=KST)
                except ValueError:active=True;continue
                if end>vi_at:active=True;continue
                last_release=max(last_release,end) if last_release else end
                if (vi_at-end).total_seconds()<120:active=True
            return {'vi_active':active,'vi_complete':True,'vi_observed_at':vi['observed_at'],
                    'vi_source_sha256':digest,'vi_last_trigger_at':last_release.isoformat() if last_release else None,
                    'vi_last_trigger_basis':'release_time_conservative_120s_not_trigger_assertion',
                    'vi_processing_times':processing}
        except (ValueError,TypeError,KeyError,OverflowError):
            return unknown

    def market(self,symbol,*,force=False):
        self._bind();now=self.now();old=self.quotes.get(symbol)
        if old and not force and (now-datetime.fromisoformat(old['observed_at'])).total_seconds()<5:return dict(old)
        vi=self.vi_state(symbol)
        now=self.now()
        raw=self.quote_fetcher(self.session,symbol);observed=self.now()
        if raw.get('stk_cd')!=symbol or not 0<=(observed-now).total_seconds()<=8:raise ValueError('quote_unverified')
        def optional(key):
            try:return number(raw.get(key),positive=True)
            except ValueError:return None
        current=optional('cur_prc');indicative=optional('exp_cntr_pric')
        result={'symbol':symbol,'mode':'paper','observed_at':observed.isoformat(),'available_at':observed.isoformat(),
            'request_started_at':now.isoformat(),'source_sha256':digest(raw),'source_api':'ka10001',
            'current_price':current,'day_open':optional('open_pric'),'upper_limit_price':optional('upl_pric'),
            'indicative_price':indicative,'indicative_volume':optional('exp_cntr_qty'),
            'indicative_version':'ka10001_exp_cntr_pric_observed' if indicative else 'unobserved',
            'day_volume':optional('trde_qty'),'prior_limit_up':None,'vi_active':None,'vi_complete':False,
            'vi_last_trigger_at':None,'common_share':False,'normal':False}
        result['tick_size']=common_tick(current) if current else None
        result['upper_limit_observation_status']=self._observe_upper_limit(symbol,result)
        member=self.master.get(symbol)
        if member and member['market'] in self.master_complete:result.update(common_share=member['common_share'],normal=member['normal'],
            classification_sha256=member['source_sha256'],classification_available_at=member['available_at'],
            classification_source_sha256=member['source_sha256'],classification_observed_at=member['available_at'],
            is_common_stock=member['common_share'],managed=not member['normal'],investment_warning=not member['normal'],
            investment_danger=not member['normal'],is_spac=not member['common_share'],is_etf=False,is_etn=False,is_preferred=not member['common_share'])
        daily=self.daily(symbol)
        if daily:
            flag,basis,prior_sha=self._prior_limit_up(symbol,daily,observed)
            result.update(avg_turnover20_krw=daily['avg_turnover20_krw'],prior_limit_up=flag,
                prior_limit_up_basis=basis,prior_upper_limit_evidence_sha256=prior_sha)
        else:result.update(prior_limit_up_basis='prior_daily_unobserved',prior_upper_limit_evidence_sha256=None)
        result.update(vi)
        from .paper_experiment_disclosure import read_disclosure_clearance
        risk_path=getattr(self.provider,'_s5_risk_path',None)
        risk=read_disclosure_clearance(risk_path,symbol,observed) if risk_path is not None else None
        result['dart_bad_news']=None if risk is None else risk['unresolved_major_risk']
        result['dart_source_sha256']=risk.get('source_sha256') if risk else None
        result['dart_observed_at']=risk.get('reviewed_at') if risk else None
        result['dart_context']=risk
        self.quotes[symbol]=dict(result);return result

    def status(self):
        return {'prior_daily_symbols':sorted(self.daily_cache),'daily_unavailable':sorted(self.daily_failed),
            'master_verified_symbols':len(self.master),'master_complete_markets':sorted(self.master_complete),
            'master_failed_markets':sorted(self.master_failed),'vi_complete':bool(self.vi),
            'vi_diagnostic':deepcopy(self.vi_diagnostic),
            'selection_prefetch_diagnostics':deepcopy(self.selection_prefetch_diagnostics),
            'preopen_daily_diagnostics':dict(self.daily_diagnostics),
            'historical_same_window_14_sessions':'actual_saved_14_sessions_required_no_daily_fallback',
            'same_window_history_diagnostics':dict(self.history_diagnostics)}
