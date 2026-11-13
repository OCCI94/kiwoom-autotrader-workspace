"""Read-only S5 realized-lot projection, isolated from the execution writer.

Only a broker-zero v5 or native-S5-bound v6 generation establishes opening basis. Exact 00 unit
considerations, their cumulative corroboration and generation-bound exits feed
FIFO. Unknown historical positions, broken streams and incomplete orders are
not repaired. A partially filled order stays unavailable until its original
quantity is fully observed. Confirmed residual cancellations and their child
order events currently stay explicitly unevaluated; no child is ignored. Cross-session stream stops/gaps within
the generation remain unavailable, even if a later snapshot looks flat.
Cost fields 938/939 remain unallocated; no actual Mock Net is
manufactured. SQL helpers require an existing query-only read transaction.
Call prepare_realized_reader immediately after connecting, before query_only
and BEGIN, so epoch validation includes immutable repeat receipts. It installs
TEMP views only and never writes the main ledger or relaxes query_only.
"""
from collections import deque
from datetime import datetime, timedelta
from decimal import Decimal
import hashlib
import json
import re
import sqlite3
from zoneinfo import ZoneInfo

KST=ZoneInfo('Asia/Seoul')
S5='S5_steady_uptrend_vwap'
LIMIT=20000


def prepare_realized_reader(db):
    from .broker_observation_repeats import install_evidence_view
    if db.in_transaction or db.execute('PRAGMA query_only').fetchone()[0]:
        raise ValueError('prepare_realized_reader_before_read_transaction')
    install_evidence_view(db)


def _time(value):
    d=datetime.fromisoformat(value)
    if d.tzinfo is None or d.utcoffset() is None:raise ValueError('aware_clock_required')
    return d.astimezone(KST)


def _json(row,field='payload_json',hashfield='digest'):
    raw=row[field]
    if not isinstance(raw,str) or hashlib.sha256(raw.encode()).hexdigest()!=row[hashfield]:raise ValueError('evidence_hash_conflict')
    value=json.loads(raw)
    if not isinstance(value,dict):raise ValueError('invalid_evidence')
    return value


def fifo_realized_evidence(generation, day):
    """Pure projection from a reader-validated generation and exact units.

    FIFO associates each sold share with its observed acquisition consideration.
    Independent callers must not claim ``reader_validated`` for inferred lots.
    Remaining lots stay open; only actual sale units on ``day`` are realized.
    """
    if (generation.get('reader_validated') is not True or generation.get('mode')!='paper'
        or generation.get('strategy')!=S5 or not re.fullmatch(r'\d{8}',day)):
        raise ValueError('unverified_generation')
    lots=deque();buy=Decimal(0);sell=Decimal(0);sold=0;seen=set();last=None
    for u in generation['units']:
        at=_time(u['executed_at']);observed=_time(u['observed_at'])
        if observed<at or (last is not None and at<last):raise ValueError('unit_clock_conflict')
        last=at;key=(at.strftime('%Y%m%d'),u['order_number'],u['execution_id'])
        if key in seen:raise ValueError('duplicate_unit')
        seen.add(key);qty=u['quantity'];price=Decimal(u['price'])
        if type(qty) is not int or qty<=0 or not price.is_finite() or price<=0:raise ValueError('invalid_unit')
        if u['side']=='buy':lots.append([qty,price]);continue
        if u['side']!='sell':raise ValueError('invalid_side')
        remaining=qty;basis=Decimal(0)
        while remaining:
            if not lots:raise ValueError('unproven_opening_basis')
            used=min(remaining,lots[0][0]);basis+=used*lots[0][1]
            lots[0][0]-=used;remaining-=used
            if not lots[0][0]:lots.popleft()
        if at.strftime('%Y%m%d')==day:buy+=basis;sell+=price*qty;sold+=qty
    if not sold:return None
    refs=list(generation['evidence_refs'])
    return {'schema':'realized-profit-evidence-v1','mode':'paper','scope_id':'ledger_'+hashlib.sha256(generation['scope'].encode()).hexdigest()[:24],
        'symbol':generation['symbol'],'day':day,'strategy':S5,'generation_ids':[generation['generation_id']],
        'sold_quantity':sold,'remaining_observed_quantity':sum(x[0] for x in lots),
        'basis_method':generation.get('basis_method','FIFO_actual_00_unit_lots_from_verified_v5_zero_generation'),
        'gross_verified':True,'sold_lot_basis_verified':True,'actual_turnover_verified':True,
        'verified_gross_pnl_krw':str(sell-buy),'matched_buy_amount_krw':str(buy),'matched_sell_amount_krw':str(sell),
        'gross_evidence_refs':refs,'turnover_evidence_refs':refs,'actual_costs_reconciled':False,
        'reference_market_assumption':'KR_cash_equity_standard_fee_reference',
        'actual_fee_allocation_status':'00_938_939_aggregation_scope_unverified',
        'read_only_projection':True,'account_wide_profit_verified':False}


class _ProofView:
    """No ExecutionTracker constructor: expose only its read validators."""
    def __init__(self,scope,native=False):self.scope=scope;self.native=native
    def settings_loader(self):
        # Historical source contract, not today's running settings. Native is
        # set only after the frozen native S5 decision has been validated.
        return {'paper_experiment_enabled':self.native,'paper_experiment_s5_enabled':self.native}
    def _validate_strategy_holding_schema(self,db):
        from .execution_log import ExecutionTracker
        return ExecutionTracker._validate_strategy_holding_schema(db)
    def _assert_strategy_holding_evidence(self,db,proof):
        from .execution_log import ExecutionTracker
        return ExecutionTracker._assert_strategy_holding_evidence(self,db,proof)
    def validate_epoch(self,db,proof,at):
        from .execution_log import ExecutionTracker
        return ExecutionTracker._assert_epoch_proof(self,db,proof,at)


def _rows(db,sql,args=()):
    rows=[dict(r) for r in db.execute(sql+' LIMIT '+str(LIMIT+1),args)]
    if len(rows)>LIMIT:raise ValueError('row_limit_exceeded')
    return rows


def _decision(db,scope,intent,order,first_id):
    from .decision_evidence import freeze_evidence
    row=db.execute('SELECT * FROM order_decisions WHERE scope=? AND intent_id=?',(scope,intent['id'])).fetchone()
    if row is None:raise ValueError('s5_order_decision_missing')
    doc=_json(row,'evidence_json','evidence_sha256')
    frozen=freeze_evidence(doc)
    if frozen is None:raise ValueError('s5_order_decision_missing')
    doc=frozen.as_dict();sig=doc.get('multi_strategy_signal') or {};cfg=doc.get('config') or {};pos=sig.get('position') or {}
    created=_time(intent['created_at']);observed=_time(doc['observed_at']);evaluated=_time(doc['evaluated_at'])
    native=doc.get('strategy_version')=='paper-experiment-v1'
    if (doc.get('strategy')!=S5 or doc.get('strategy_version') not in ('paper-multi-observed-v4','paper-experiment-v1')
        or doc.get('source_kind')!='broker_observation'
        or (not native and (sig.get('symbol')!=intent['symbol'] or sig.get('side')!=intent['side']))
        or (native and cfg['experiment_assignment']['symbol']!=intent['symbol'])
        or _time(row['recorded_at'])!=created or not observed<=evaluated<=created or created-evaluated>timedelta(seconds=5)):
        raise ValueError('s5_order_decision_binding_conflict')
    if native:
        if intent['side']=='buy':
            from .paper_experiment_s5 import validate_s5_native_evidence
            if intent['id']!=first_id:raise ValueError('native_s5_add_not_supported')
            validate_s5_native_evidence(cfg['experiment_s5_signal'],symbol=intent['symbol'],now=created,
                signal_at=cfg['experiment_signal_at'])
            if (row['order_type']!='limit' or not isinstance(row['order_price'],str)
                    or not re.fullmatch(r'[1-9][0-9]*',row['order_price'])
                    or not 0<int(row['order_price'])*intent['quantity']<=doc['budget']):
                raise ValueError('s5_buy_decision_quantity_conflict')
        elif (row['order_type']!='market' or row['order_price'] is not None
                or 'experiment_s5_exit' not in cfg or cfg.get('multi_position_generation_id')!=first_id):
            raise ValueError('s5_sell_decision_conflict')
        claim=db.execute('SELECT * FROM paper_experiment_signal_claims WHERE scope=? AND intent_id=?',
            (scope,intent['id'])).fetchone()
        if (claim is None or claim['signal_sha256']!=cfg['experiment_signal_sha256']
                or claim['symbol']!=intent['symbol'] or claim['strategy']!=S5 or claim['mode']!='paper'):
            raise ValueError('native_s5_signal_claim_conflict')
        return doc,row['evidence_sha256']
    if intent['side']=='buy':
        price=row['order_price']
        if (row['order_type']!='limit' or not isinstance(price,str) or not re.fullmatch(r'[1-9][0-9]*',price)
            or type(doc.get('budget')) is not int or not 0<int(price)*intent['quantity']<=doc['budget']):raise ValueError('s5_buy_decision_quantity_conflict')
        if intent['id']==first_id:
            if (sig.get('reason')!='steady_uptrend_vwap' or cfg.get('multi_b3_stage')!='initial'
                or cfg.get('multi_b3_initial_intent_id') is not None or pos.get('generation_id') is not None):raise ValueError('s5_initial_decision_conflict')
        elif (sig.get('reason')!='add_on_strength' or cfg.get('multi_b3_stage')!='add'
              or cfg.get('multi_b3_initial_intent_id')!=first_id or pos.get('generation_id')!=first_id):raise ValueError('s5_add_decision_conflict')
    elif (row['order_type']!='market' or row['order_price'] is not None or sig.get('quantity')!=intent['quantity']
          or pos.get('generation_id')!=first_id or cfg.get('multi_position_generation_id')!=first_id):raise ValueError('s5_sell_decision_conflict')
    return doc,row['evidence_sha256']


def _generation(db,scope,first,following,day,asof):
    from .execution_log import _execution_accounting
    from .cancellation_settlement import confirmed_cancelled_quantity
    symbol=first['symbol'];start=_time(first['created_at'])
    rec=db.execute('SELECT * FROM pre_order_reconciliations WHERE scope=? AND intent_id=?',(scope,first['id'])).fetchone()
    if rec is None:raise ValueError('initial_proof_missing')
    proof=_json(rec,'evidence_json','evidence_sha256')
    version=proof.get('schema_version')
    if version not in (5,6) or proof.get('symbol')!=symbol or proof.get('order_quantity')!=first['quantity']:raise ValueError('initial_v5_or_native_v6_required')
    native=version==6
    if native:
        from .paper_policy import validate_experiment_order_sizing
        # Verify native decision before enabling the historical v6 epoch reader.
        doc,_=_decision(db,scope,first,None,first['id'])
        if (doc.get('strategy_version')!='paper-experiment-v1' or proof.get('pre_order_held_qty')!=0
                or proof.get('pre_order_pur_amt_krw')!=0):raise ValueError('native_s5_zero_epoch_required')
        sizing=validate_experiment_order_sizing(proof.get('paper_experiment_sizing'),
            quantity=first['quantity'],price=proof['limit_price_krw'])
        if (proof.get('symbol_cap_krw')!=sizing['effective_symbol_cap_krw']
                or proof.get('per_buy_cap_krw')!=(max(300000,sizing['price_krw']) if sizing['one_share_exception'] else sizing['budget_krw'])
                or proof.get('remaining_principal_krw')!=sizing['effective_symbol_cap_krw']):
            raise ValueError('native_s5_budget_conflict')
    view=_ProofView(scope,native=native)
    view.validate_epoch(db,proof,start)
    if not _time(proof['reconciliation_completed_at'])<start:raise ValueError('epoch_clock_conflict')
    lower=_time(proof['observed_at'])
    # End at the latest accepted sale relevant to the requested date. Later
    # unrelated failures do not retroactively erase this completed sold scope.
    selected=[i for i in following if i['side']=='sell' and i['day']==day and _time(i['created_at'])<=asof]
    if not selected:return None
    final_created=max(_time(i['created_at']) for i in selected)
    intents=[i for i in following if start<=_time(i['created_at'])<=final_created]
    if any(i['state']!='accepted' or i['strategy']!=S5 for i in intents):raise ValueError('mixed_or_unaccepted_intent')
    if len([i for i in intents if i['side']=='buy'])>2:raise ValueError('multiple_adds_unsupported')
    orders=[];refs=['pre_order:'+rec['evidence_sha256']];claims={};buy_claims={};decisions={}
    for i in intents:
        o=db.execute('SELECT * FROM orders WHERE scope=? AND day=? AND mode=\'paper\' AND ord_no=?',(scope,i['day'],i['order_number'])).fetchone()
        if o is None:raise ValueError('accepted_order_missing')
        o=dict(o)
        if any(o[k]!=i[k] for k in ('symbol','side','strategy','quantity')) or not _time(i['created_at'])<=_time(o['registered_at'])<=asof:raise ValueError('order_intent_conflict')
        decision_doc,decision_hash=_decision(db,scope,i,o,first['id'])
        decisions[(i['day'],o['ord_no'])]=decision_doc
        refs.append('order_decision:'+decision_hash)
        if i['side']=='buy' and i['id']!=first['id']:
            bc=db.execute('SELECT * FROM b3_add_claims WHERE scope=? AND add_intent_id=?',(scope,i['id'])).fetchone()
            br=db.execute('SELECT * FROM pre_order_reconciliations WHERE scope=? AND intent_id=?',(scope,i['id'])).fetchone()
            decision=db.execute('SELECT * FROM order_decisions WHERE scope=? AND intent_id=?',(scope,i['id'])).fetchone()
            if bc is None or br is None or decision is None or bc['initial_intent_id']!=first['id']:raise ValueError('add_generation_missing')
            bp=_json(br,'evidence_json','evidence_sha256');de=_json(decision,'evidence_json','evidence_sha256');cfg=de.get('config') or {}
            if (cfg.get('multi_b3_stage')!='add' or cfg.get('multi_b3_initial_intent_id')!=first['id'] or de.get('strategy')!=S5
                or bp.get('schema_version')!=3 or bp.get('symbol')!=symbol or bp.get('order_quantity')!=i['quantity']
                or not start<=_time(bp['observed_at'])<=_time(i['created_at'])
                or _time(bc['claimed_at'])!=_time(i['created_at'])):raise ValueError('add_proof_conflict')
            view._assert_strategy_holding_evidence(db,bp)
            buy_claims[(i['day'],o['ord_no'])]=bp
            refs.extend(['pre_order:'+br['evidence_sha256'],'order_decision:'+decision['evidence_sha256']])
        if i['side']=='sell':
            claim=db.execute('SELECT * FROM multi_strategy_exit_claims WHERE scope=? AND intent_id=?',(scope,i['id'])).fetchone()
            sr=db.execute('SELECT * FROM pre_order_reconciliations WHERE scope=? AND intent_id=?',(scope,i['id'])).fetchone()
            if claim is None or sr is None or claim['generation_id']!=first['id']:raise ValueError('exit_generation_missing')
            sp=_json(sr,'evidence_json','evidence_sha256')
            if (sp.get('schema_version')!=4 or sp.get('generation_id')!=first['id'] or sp.get('symbol')!=symbol
                or sp.get('strategy')!=S5 or sp.get('order_quantity')!=i['quantity']
                or claim['exit_kind']!=sp.get('exit_kind') or _time(claim['claimed_at'])!=_time(i['created_at'])
                or not lower<=_time(sp['observed_at'])<=_time(i['created_at'])):raise ValueError('exit_proof_conflict')
            view._assert_strategy_holding_evidence(db,sp)
            claims[(i['day'],o['ord_no'])]=sp
            refs.append('pre_order:'+sr['evidence_sha256'])
        orders.append(o)
    keys={(o['day'],o['ord_no']) for o in orders}
    if len(keys)!=len(orders):raise ValueError('duplicate_order')
    # A same-symbol order outside the generation is not silently ignored.
    for o in _rows(db,"SELECT * FROM orders WHERE scope=? AND mode='paper' AND symbol=? AND day>=? AND day<=?",(scope,symbol,first['day'],day)):
        if start<=_time(o['registered_at'])<=asof and (o['day'],o['ord_no']) not in keys:
            # Orders after our latest sale belong to later scopes.
            if _time(o['registered_at'])<=max(_time(x['registered_at']) for x in orders):raise ValueError('unowned_order')
    obs=_rows(db,"SELECT * FROM broker_observations WHERE scope=? AND mode='paper' AND source_api='00' AND day>=? AND day<=? ORDER BY id",(scope,first['day'],day))
    parsed=[];events=[]
    for row in obs:
        ev=_json(row);at=_time(ev['observed_at'])
        if at>asof:continue
        if ev.get('mode')!='paper' or ev.get('day')!=row['day'] or at.strftime('%Y%m%d')!=row['day'] or ev.get('source_api')!='00' or ev.get('observed_at')!=row['observed_at'] or ev.get('source_kind')!='broker_observation':raise ValueError('observation_identity_conflict')
        parsed.append(row);events.append((at,row,ev))
    details=[];units=[]
    for d in sorted({o['day'] for o in orders}):
        os=[o for o in orders if o['day']==d]
        fills=_rows(db,"SELECT * FROM fills WHERE scope=? AND mode='paper' AND symbol=? AND day=?",(scope,symbol,d))
        fills=[f for f in fills if (d,f['ord_no']) in keys]
        detail=_execution_accounting(d,os,fills,[r for r in parsed if r['day']==d])['orders']
        for o,a in zip(os,detail):
            cancelled=confirmed_cancelled_quantity(db,scope,o)
            if cancelled:raise ValueError('confirmed_cancellation_realized_scope_unsupported')
            expected=o['quantity']
            if (a['verified_amount_source']!='00_unit_sum_reconciled_to_cumulative_quantity' or a['unit_coverage']!='quantity_reconciled'
                or a['issues'] or a['stream_gap_observed'] or a['verified_quantity']!=expected or expected<=0 or a['rest_projection_quantity']!=expected):raise ValueError('incomplete_or_conflicting_unit_coverage')
            details.append(a)
            for u in a['unit_executions']:
                observations=[(at,r,e) for at,r,e in events if r['id'] in u['observation_ids']]
                if not observations:raise ValueError('unit_observation_missing')
                at=min(x[0] for x in observations);actual=_time(d[:4]+'-'+d[4:6]+'-'+d[6:]+'T'+u['broker_time'][:2]+':'+u['broker_time'][2:4]+':'+u['broker_time'][4:]+'+09:00')
                if not _time(o['registered_at']).replace(microsecond=0)<=actual<=at:raise ValueError('unit_time_conflict')
                refs.extend('00:'+r['digest'] for _,r,_ in observations)
                units.append({'day':d,'order_number':o['ord_no'],'execution_id':u['execution_id'],'side':o['side'],
                    'price':u['price'],'quantity':u['quantity'],'executed_at':actual.isoformat(),'observed_at':at.isoformat()})
    units.sort(key=lambda u:(_time(u['executed_at']),_time(u['observed_at']),int(u['execution_id'])))
    last=max(_time(u['observed_at']) for u in units)
    prior=[(at,r['id'],ev['event_kind']) for at,r,ev in events if at<=lower and ev.get('event_kind') in ('stream_started','stream_resumed','stream_gap','stream_stopped')]
    if not prior or max(prior)[2] not in ('stream_started','stream_resumed'):raise ValueError('stream_anchor_missing')
    for at,row,ev in events:
        if not lower<=at<=last:continue
        kind=ev.get('event_kind');v=ev.get('values') or {}
        if kind in ('stream_stopped','stream_gap') or kind in ('stream_started','stream_resumed') and at>lower:raise ValueError('generation_stream_discontinuity')
        if kind=='execution_stream' and v.get('9001') in (symbol,'A'+symbol) and (row['day'],str(v.get('9203','')).lstrip('0')) not in keys:raise ValueError('unknown_generation_order_event')
    held=0;sold_before=False;actual_basis=Decimal(0)
    for o in orders:
        ou=[u for u in units if (u['day'],u['order_number'])==(o['day'],o['ord_no'])]
        if o['side']=='buy' and (o['day'],o['ord_no']) in buy_claims:
            bp=buy_claims[(o['day'],o['ord_no'])]
            priorunits=[u for u in units if _time(u['observed_at'])<=_time(bp['observed_at'])]
            if (sold_before or bp['pre_order_held_qty']!=held or Decimal(bp['pre_order_pur_amt_krw'])!=actual_basis
                or sum(u['quantity']*(1 if u['side']=='buy' else -1) for u in priorunits)!=held):raise ValueError('add_holding_basis_conflict')
        decision=decisions[(o['day'],o['ord_no'])]
        native_decision=decision.get('strategy_version')=='paper-experiment-v1'
        position={} if native_decision else decision['multi_strategy_signal']['position']
        if o['side']=='buy' and (o['day'],o['ord_no']) in buy_claims:
            if position['owned_qty']!=held or Decimal(position['basis_krw'])!=actual_basis:raise ValueError('s5_add_position_conflict')
        if o['side']=='sell':
            if (not native_decision and position['owned_qty']!=held
                    or decision['config']['multi_exit_kind']!=claims[(o['day'],o['ord_no'])]['exit_kind']):raise ValueError('s5_exit_position_conflict')
            initial=[u for u in units if (u['day'],u['order_number'])==(first['day'],first['order_number'])]
            first_unit=min(initial,key=lambda u:(_time(u['executed_at']),_time(u['observed_at'])))
            if native_decision:
                from .execution_log import ExecutionTracker
                verified={'first_fill_at':first_unit['executed_at'],'first_fill_timing_verified':True,
                    'owned_qty':held,'basis_krw':int(actual_basis)}
                ExecutionTracker._assert_s5_exit_clock(S5,verified,decision,_time(decision['evaluated_at']))
            elif (position.get('first_fill_timing_verified') is not True or position.get('first_fill_at')!=first_unit['executed_at']
                or position.get('first_fill_observed_at')!=first_unit['observed_at']):raise ValueError('s5_first_fill_decision_conflict')
            sold_before=True
            sp=claims[(o['day'],o['ord_no'])]
            if sp['pre_order_held_qty']!=held:raise ValueError('exit_holding_quantity_conflict')
            priorunits=[u for u in units if _time(u['observed_at'])<=_time(sp['observed_at'])]
            if sum(u['quantity']*(1 if u['side']=='buy' else -1) for u in priorunits)!=held:raise ValueError('exit_holding_before_fill')
        held+=sum(u['quantity'] for u in ou)*(1 if o['side']=='buy' else -1)
        if o['side']=='buy':actual_basis+=sum((Decimal(u['price'])*u['quantity'] for u in ou),Decimal(0))
    result=fifo_realized_evidence({'reader_validated':True,'mode':'paper','strategy':S5,'scope':scope,'symbol':symbol,
        'generation_id':first['id'],'units':units,'evidence_refs':sorted(set(refs)),
        'basis_method':('FIFO_actual_00_unit_lots_from_verified_native_s5_v6_zero_generation' if native
            else 'FIFO_actual_00_unit_lots_from_verified_v5_zero_generation')},day)
    if result is not None:result['assessed_sale_order_keys']=[[o['day'],o['ord_no']] for o in orders if o['side']=='sell' and o['day']==day]
    return result


def collect_realized_profit_evidence(db,day,as_of,scope='default'):
    """Use only SELECTs in the caller's existing mode=ro/query_only transaction.

    Bounded per-symbol generation reconstruction. Unsupported historical lots
    return explicit diagnostics instead of invented zero profit or cost basis.
    """
    out={'schema':'readonly-realized-generations-v1','day':day,'by_symbol':{},'diagnostics':{},'coverage':{},'read_only':True}
    try:
        now=as_of if isinstance(as_of,datetime) else _time(as_of)
        if now.tzinfo is None or not re.fullmatch(r'\d{8}',day) or not db.in_transaction or db.execute('PRAGMA query_only').fetchone()[0]!=1:raise ValueError('readonly_transaction_required')
        if not db.execute("SELECT 1 FROM sqlite_temp_master WHERE type='view' AND name='broker_observation_evidence'").fetchone():
            out['diagnostics']['reader']='broker_repeat_reader_adapter_required'
            raise ValueError('broker_repeat_reader_adapter_required')
        symbols=[r[0] for r in db.execute("SELECT DISTINCT symbol FROM orders WHERE scope=? AND mode='paper' AND day=? AND side='sell' AND strategy=?",(scope,day,S5))]
        for symbol in symbols:
            try:
                requested=_rows(db,"SELECT * FROM orders WHERE scope=? AND mode='paper' AND symbol=? AND day=? AND side='sell'",(scope,symbol,day))
                requested=[o for o in requested if _time(o['registered_at'])<=now]
                expected={(o['day'],o['ord_no']) for o in requested}
                out['coverage'][symbol]={'requested_sale_orders':len(expected),'assessed_sale_orders':0,'all_symbol_sales_reconciled':False}
                if any(o['strategy']!=S5 for o in requested):raise ValueError('mixed_strategy_sales_symbol_total_unverified')
                intents=_rows(db,"SELECT * FROM intents WHERE scope=? AND mode='paper' AND symbol=? AND day<=? ORDER BY created_at,id",(scope,symbol,day))
                intents=[i for i in intents if _time(i['created_at'])<=now and i['state']!='cancelled']
                starts=[]
                for idx,i in enumerate(intents):
                    if i['side']!='buy':continue
                    r=db.execute('SELECT * FROM pre_order_reconciliations WHERE scope=? AND intent_id=?',(scope,i['id'])).fetchone()
                    if r is not None and _json(r,'evidence_json','evidence_sha256').get('schema_version') in (5,6):starts.append(idx)
                evidence=[]
                for n,idx in enumerate(starts):
                    group=intents[idx:starts[n+1] if n+1<len(starts) else len(intents)]
                    if group[0]['strategy']!=S5:continue
                    e=_generation(db,scope,group[0],group,day,now)
                    if e:evidence.append(e)
                if not evidence:raise ValueError('verified_s5_generation_unavailable')
                assessed=[tuple(k) for e in evidence for k in e['assessed_sale_order_keys']]
                out['coverage'][symbol]['assessed_sale_orders']=len(set(assessed))
                accepted={(i['day'],i['order_number']) for i in intents if i['day']==day and i['side']=='sell' and i['state']=='accepted'}
                if len(assessed)!=len(set(assessed)) or set(assessed)!=expected or accepted!=expected:raise ValueError('day_sale_order_coverage_incomplete')
                out['coverage'][symbol]['all_symbol_sales_reconciled']=True
                merged=dict(evidence[0]);merged['generation_ids']=sum((e['generation_ids'] for e in evidence),[])
                for k in ('verified_gross_pnl_krw','matched_buy_amount_krw','matched_sell_amount_krw'):merged[k]=str(sum((Decimal(e[k]) for e in evidence),Decimal(0)))
                merged['sold_quantity']=sum(e['sold_quantity'] for e in evidence)
                merged['assessed_sale_order_keys']=[list(k) for k in sorted(assessed)]
                merged['all_symbol_sale_orders_reconciled']=True
                for k in ('gross_evidence_refs','turnover_evidence_refs'):merged[k]=sorted(set(x for e in evidence for x in e[k]))
                out['by_symbol'][symbol]=merged;out['diagnostics'][symbol]='verified_fifo_sold_lots_actual_costs_unallocated'
            except (ValueError,TypeError,KeyError,sqlite3.Error,ArithmeticError) as exc:out['diagnostics'][symbol]=str(exc)[:200]
            except Exception:out['diagnostics'][symbol]='generation_proof_unverified'
    except (ValueError,TypeError,KeyError,sqlite3.Error):out['status']='unavailable'
    else:out['status']='evaluated'
    return out
