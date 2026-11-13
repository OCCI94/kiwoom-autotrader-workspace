"""Time-stamped special-arm selections owned by the paper receiver.

Selection snapshots never relabel later observations as earlier inputs. A late
or incomplete window is recorded, not replayed after its cutoff.
"""
from copy import deepcopy
from datetime import datetime,timedelta
from . import paper_experiment_extended_rules as extended
from .paper_experiment_inputs import common_tick,digest
from .paper_experiment_rules import select_overnight,_time,_day,_sha,_digest
from .paper_experiment_rules import validate_assignment,evaluate_entry
from .paper_experiment_calendar import current_session


def record_c_signal(store,assignment,result,opening_context,recorded_at):
    """Freeze actual eligible C evidence before any cash/order outcome.

    A pre-open assignment by itself is insufficient. The original observed
    opening price must reproduce the true 09:00 C signal exactly.
    """
    if assignment.get('arm')!='EXP_C' or result.get('signal') is not True:return None
    at=_time(result['evaluated_at']);recorded=_time(recorded_at)
    if recorded<at or recorded.strftime('%Y%m%d')!=assignment['day']:raise ValueError('c_signal_record_time')
    expected=evaluate_entry('EXP_C',[],symbol=assignment['symbol'],now=at,
        assignment=assignment,opening_context=opening_context)
    if expected!=result or expected.get('signal') is not True:raise ValueError('c_signal_evidence_invalid')
    value={'schema':'paper-experiment-observed-c-signal-v1','day':assignment['day'],'symbol':assignment['symbol'],
        'recorded_at':recorded.isoformat(),'assignment':deepcopy(assignment),
        'opening_context':deepcopy(opening_context),'result':deepcopy(result)}
    value['source_sha256']=digest(value)
    return store.selection(value['day'],'eligible:C:'+value['symbol'],value)


def _verified_c_signal(value,symbol,day,now):
    if (not isinstance(value,dict) or value.get('schema')!='paper-experiment-observed-c-signal-v1'
            or value.get('day')!=day or value.get('symbol')!=symbol
            or value.get('source_sha256')!=digest({k:v for k,v in value.items() if k!='source_sha256'})):
        raise ValueError('c_signal_integrity')
    at=_time(value['result']['evaluated_at']);recorded=_time(value['recorded_at'])
    if not at<=recorded<=now or recorded.strftime('%Y%m%d')!=day:raise ValueError('c_signal_future')
    assignment=value['assignment'];validate_assignment(assignment,symbol=symbol,day=day,now=at)
    expected=evaluate_entry('EXP_C',[],symbol=symbol,now=at,assignment=assignment,
        opening_context=value['opening_context'])
    if expected.get('signal') is not True or expected!=value['result']:raise ValueError('c_signal_recalculation_failed')
    return deepcopy(assignment)


class ExtendedScheduler:
    def __init__(self,inputs,store,chart,clock,*,calendar_reader=None):
        self.inputs=inputs;self.store=store;self.chart=chart;self.now=clock
        self.failure={};self.running=False;self.calendar_reader=calendar_reader or current_session
        self.calendar_cache=None

    @staticmethod
    def _verify_calendar_snapshot(value,day,now):
        if (not isinstance(value,dict) or value.get('schema')!='paper-experiment-market-calendar-v1'
                or value.get('day')!=day or value.get('source_sha256')!=digest({k:v for k,v in value.items() if k!='source_sha256'})):
            raise ValueError('calendar_snapshot_integrity')
        at=_time(value['available_at']);official=value['official_session']
        start=_time(official['start']);end=_time(official['end'])
        if (not at<=now or at.strftime('%Y%m%d')!=day or not start<end
                or start.strftime('%Y%m%d')!=day or end.strftime('%Y%m%d')!=day
                or not _sha(official.get('calendar_source_sha256'))):raise ValueError('calendar_snapshot_time')
        refs=value['references'];days=[]
        if not isinstance(refs,list) or len(refs)!=2:raise ValueError('calendar_reference_count')
        for symbol,reference in zip(('069500','229200'),refs):
            features=extended.daily_features(reference,symbol=symbol,now=at)
            days.append(features['prior_dates'])
        if days[0]!=days[1] or value['trading_days']!=days[0]+[day]:raise ValueError('calendar_reference_sessions_differ')
        return value

    def _calendar(self,now):
        """Accumulate positive session observations, never weekday inference.

        Both ETFs must agree on actual prior daily dates. Today's positive
        session is separately bound to the current official calendar claim.
        Each daily snapshot is immutable, allowing overdue VSS positions to
        retain their entry date after it rolls outside the 21-session window.
        """
        now=_time(now);day=now.strftime('%Y%m%d')
        if self.calendar_cache and self.calendar_cache[0]==day:return deepcopy(self.calendar_cache[1])
        stored=self.store.selection(day,'market_calendar')
        if stored is None:
            refs=[self.inputs.daily(code) for code in ('069500','229200')]
            official=self.calendar_reader(now)
            if not all(refs) or not isinstance(official,dict):raise ValueError('calendar_sources_unavailable')
            value={'schema':'paper-experiment-market-calendar-v1','day':day,'available_at':now.isoformat(),
                'references':deepcopy(refs),'trading_days':[r['date'] for r in refs[0]['rows']]+[day],
                'official_session':{'start':_time(official['start']).isoformat(),
                    'end':_time(official['end']).isoformat(),'calendar_source_sha256':official['calendar_source_sha256']}}
            value['source_sha256']=digest(value)
            self._verify_calendar_snapshot(value,day,now)
            stored=self.store.selection(day,'market_calendar',value)
        self._verify_calendar_snapshot(stored,day,now)
        history=self.store.market_calendar_history(day)
        if not any(row['day']==day for row in history):raise ValueError('calendar_snapshot_not_persisted')
        days=set();sources=[];available=[]
        for row in history:
            if _day(row['day']).date()>now.date():raise ValueError('calendar_history_future')
            value=self._verify_calendar_snapshot(row['payload'],row['day'],now)
            if days and value['trading_days'][0]>max(days):
                raise ValueError('calendar_history_unobserved_interval')
            days.update(value['trading_days']);sources.append(value['source_sha256']);available.append(value['available_at'])
        if not days or len(days)>400:raise ValueError('calendar_history_bound')
        result={'verified':True,'trading_days':sorted(days),'source_sha256':digest(sources),
            'available_at':max(available),'source_snapshots':sources,
            'basis':'accumulated_matching_actual_ETF_sessions_and_official_current_sessions'}
        self.calendar_cache=(day,result);return deepcopy(result)

    def context(self,symbol,now,*,bars=None,market=True,selection_snapshot=False):
        daily=self.inputs.daily(symbol)
        selection_reader=getattr(self.inputs,'selection_market',None)
        quote=(selection_reader(symbol,now) if selection_snapshot and callable(selection_reader)
            else self.inputs.market(symbol)) if market else {}
        out={'daily':daily,'market':quote}
        if bars and out['market']:
            first=[b for b in bars if b.start.strftime('%H:%M')<'09:05']
            if first:out['market']['tick_size']=common_tick(max(b.high_price for b in first))
        try:
            out['calendar']=self._calendar(self.now())
            self.failure.pop('calendar',None)
        except (ValueError,KeyError,TypeError,AttributeError):
            self.failure['calendar']='calendar_history_unverified'
        if daily:
            row=daily['rows'][-1]
            out['day_close']={'date':row['date'],'close':row['close'],'available_at':daily['available_at'],
                'source_sha256':daily['source_sha256']}
            history=getattr(self.inputs,'same_window_history',None)
            if callable(history) and now.strftime('%H:%M')=='09:05':
                out['same_window_history']=history(symbol,now)
        return out

    def _records(self,session,pool,tracker,now,kind):
        rows=[];held=[];unqueried=[];base=[];day=now.strftime('%Y%m%d');hm=now.strftime('%H:%M')
        cutoff=now.replace(second=40,microsecond=0)
        if not isinstance(pool,(list,tuple)) or len(pool)>40 or len(set(pool))!=len(pool):
            raise ValueError('selection_pool_requires_unique_at_most40')
        pre=self.store.selection(day,'preopen') or {'candidates':[]}
        # D has no new-arm liquidity/VI filter. Capture its original eligible
        # flat pool independently of detailed data requests and their failures.
        for symbol in pool:
            if symbol in extended.ETF_SYMBOLS:continue
            try:position=tracker.multi_strategy_position('paper',symbol,'EXP_D',self.now())
            except Exception:
                self.failure[symbol]='ownership_unverified';held.append(symbol);continue
            if position.get('status') not in ('flat',):held.append(symbol);continue
            seen=self.store.selection(day,'first_seen:'+symbol)
            first=seen.get('first_seen_at') if seen else next((r['first_seen_at'] for r in pre['candidates'] if r['symbol']==symbol),None)
            if first is None:continue
            try:
                first_at=_time(first)
                if first_at.date()!=now.date() or first_at>self.now():raise ValueError('candidate_first_seen_invalid')
            except (ValueError,TypeError):
                self.failure[symbol]='candidate_first_seen_invalid';continue
            base.append({'symbol':symbol,'first_seen_at':first})
        for candidate in base:
            symbol=candidate['symbol'];first=candidate['first_seen_at']
            if self.now()>=cutoff:
                unqueried.append(symbol);continue
            try:
                bars=[] if kind=='GDR' else self.chart(session,symbol)
                # A slow chart must not start another request after cutoff.
                if self.now()>=cutoff:
                    unqueried.append(symbol);continue
                if kind!='GDR':
                    received=self.now()
                    if any(b.start>received.replace(second=0,microsecond=0) for b in bars):
                        raise ValueError('selection_future_session_bar')
                    bars=[b for b in bars if b.start.date()==received.date()
                        and b.start+timedelta(minutes=1)<=received]
                context=self.context(symbol,self.now(),bars=bars,selection_snapshot=True)
                if self.now().strftime('%Y%m%d%H:%M')!=day+hm:
                    unqueried.append(symbol);self.failure[symbol]='selection_response_after_window';continue
                rows.append({'symbol':symbol,'first_seen_at':first,'bars':bars,'context':context})
            except Exception as error:
                self.failure[symbol]=type(error).__name__
        return rows,held,unqueried,base

    def run_selection(self,session,pool,tracker,now):
        day=now.strftime('%Y%m%d');hm=now.strftime('%H:%M')
        kind={'08:59':'GDR','09:00':'GDR_fallback','09:05':'ORB','15:15':'closing'}.get(hm)
        if kind is None:return
        key='extended:'+kind
        if self.store.selection(day,key) is not None:return
        if kind=='GDR_fallback' and (self.store.selection(day,'extended:GDR') or {}).get('assignments'):
            return
        rows,held,unqueried,base=self._records(session,pool,tracker,now,'GDR' if kind.startswith('GDR') else kind)
        at=self.now()
        if at.strftime('%H:%M')!=hm:
            self.store.selection(day,key,{'day':day,'selected_at':at.isoformat(),'assignments':[],
                'status':'selection_window_expired','unqueried_symbols':unqueried,'candidate_count':len(rows),
                'candidates':base,'observed_rows':rows and [{'symbol':r['symbol'],'observed_at':r['context'].get('market',{}).get('observed_at')} for r in rows]})
            return
        kinds=['EXP_GDR'] if kind.startswith('GDR') else ['ORB'] if kind=='ORB' else ['EXP_VSS','EXP_LDR']
        selections=[extended.select_candidates(k,rows,day=day,selected_at=at,held_symbols=held) for k in kinds]
        assignments=[a for s in selections for a in s['assignments']]
        prior_c=[]
        if kind=='ORB':
            # Only an actual saved eligible opening signal competes with ORB.
            # Pending/owned symbols were already removed by _records. Winning
            # C retains its original 09:00 window; it is never replayed at 09:05.
            for assignment in list(assignments):
                symbol=assignment['symbol'];marker=self.store.selection(day,'eligible:C:'+symbol)
                if marker is None:continue
                try:
                    original=_verified_c_signal(marker,symbol,day,at)
                    assignments.append(original);prior_c.append({'symbol':symbol,'source_sha256':marker['source_sha256']})
                except (ValueError,KeyError,TypeError,AttributeError):
                    held.append(symbol);self.failure[symbol]='c_signal_evidence_invalid'
        if kind=='closing':
            d=select_overnight(base,day=day,snapshot_at=at)
            selections.append(d);assignments+=d['assignments']
        offers={}
        for a in assignments:offers.setdefault(a['symbol'],[]).append(a['arm'])
        collision=extended.resolve_collisions(day,offers,held_symbols=held)
        universe=sorted(base,key=lambda row:(row['symbol'],row['first_seen_at']))
        universe_sha=_digest(universe)
        chosen=[]
        for a in assignments:
            if collision['selected'].get(a['symbol'])!=a['arm']:continue
            value=deepcopy(a)
            if a['arm']!='EXP_C':
                value['selection_universe_sha256']=universe_sha;value['selection_available_at']=at.isoformat()
                value['assignment_sha256']=_digest({k:v for k,v in value.items() if k!='assignment_sha256'})
            chosen.append(value)
        self.store.selection(day,key,{'day':day,'selected_at':at.isoformat(),'status':'selected','assignments':chosen,
            'selections':selections,'collision':collision,'unqueried_symbols':unqueried,'candidate_count':len(rows),
            'candidates':base,'selection_universe_sha256':universe_sha,
            'detail_observed_symbols':[r['symbol'] for r in rows],'prior_c_signal_evidence':prior_c,
            'new_request_cutoff_second':40})

    def overrides(self,day):
        result={}
        for kind in ('GDR','GDR_fallback','ORB','closing'):
            snapshot=self.store.selection(day,'extended:'+kind) or {}
            for value in snapshot.get('assignments',[]):result[value['symbol']]=value
        return result

    def record_checkpoint(self,now,*,symbol_diagnostics=None):
        """Freeze actual window observations, not later reconstructed outcomes."""
        hm=now.strftime('%H:%M');day=now.strftime('%Y%m%d')
        if hm not in ('08:59','09:00','09:05','15:15'):return None
        # Capture once near the end of the selection minute. Earlier callbacks
        # must not freeze a partial minute as its final observed result.
        if now.second<40:return None
        kind='input_audit:'+hm
        if self.store.selection(day,kind) is not None:return self.store.selection(day,kind)
        from .paper_experiment_selection_audit import checkpoint
        snapshots={key:self.store.selection(day,'extended:'+key) for key in ('GDR','GDR_fallback','ORB','closing')}
        result=checkpoint(day=day,window=hm,observed_at=now,assignments=self.store.assignments(day),
            selections=snapshots,input_status=self.inputs.status(),symbol_diagnostics=symbol_diagnostics or {})
        result['source_sha256']=digest(result)
        return self.store.selection(day,kind,result)

    def status(self):return {'selection_input_failures':dict(self.failure),'inputs':self.inputs.status()}
