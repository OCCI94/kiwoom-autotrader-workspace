"""Immutable, fail-closed finalization of owned paper BUY cancellations.

kt10003 input ACK is insufficient. Partial/unfilled cancellation requires its
exact 00 terminal child plus fresh COMPLETE ka10075 and kt00009(qry_tp=0)
queries, exact original/child identity and 00/REST quantity/amount agreement.
No original financial row, holding, cash or execution projection is rewritten.
"""
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
import re
import sqlite3
import time

from kiwoom_autotrader.api.executions import _request_rows
from kiwoom_autotrader.api.minute_chart import session_identity
from kiwoom_autotrader.api.session import KST
from .broker_observation_repeats import evidence_table, install_evidence_view


TABLE='buy_ttl_cancellation_settlements'
PUBLICATIONS='buy_ttl_cancellation_publications'
COLUMNS=('scope','day','mode','original_ord_no','symbol','confirmed_at','filled_qty',
         'filled_amount','cancelled_qty','financial_fingerprint','evidence_json','evidence_sha256')


class CancellationSettlementUnavailable(ValueError):
    pass


def _fail(reason):
    raise CancellationSettlementUnavailable(reason)


def _json(value):
    return json.dumps(value,ensure_ascii=True,sort_keys=True,separators=(',',':'))


def _hash(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _time(value):
    try:
        result=datetime.fromisoformat(value) if isinstance(value,str) else value
        if not isinstance(result,datetime) or result.tzinfo is None or result.utcoffset() is None:raise ValueError
        return result.astimezone(KST)
    except (ValueError,TypeError):
        _fail('cancellation_clock_unverified')


def _num(value, *, positive=False):
    if type(value) is int:value=str(value)
    if not isinstance(value,str) or re.fullmatch(r'\+?[0-9]{1,20}(?:\.[0-9]{1,12})?',value) is None:
        _fail('cancellation_numeric_field_unverified')
    try:result=Decimal(value)
    except InvalidOperation:_fail('cancellation_numeric_field_unverified')
    if result<0 or positive and result<=0:_fail('cancellation_numeric_field_unverified')
    return result


def _int(value, *, positive=False):
    result=_num(value,positive=positive)
    if result!=int(result):_fail('cancellation_quantity_unverified')
    return int(result)


def _number(value, *, zero=False):
    if not isinstance(value,str) or re.fullmatch(r'[0-9]{1,7}',value) is None or int(value)<(0 if zero else 1):
        _fail('cancellation_order_identity_unverified')
    return str(int(value))


def _symbol(value):
    if not isinstance(value,str) or re.fullmatch(r'A?[0-9]{6}',value) is None:
        _fail('cancellation_symbol_unverified')
    return value[-6:]


def _broker_time(day,value,*,colon=False):
    pattern=r'(?:[01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]' if colon else r'(?:[01][0-9]|2[0-3])[0-5][0-9][0-5][0-9]'
    if not isinstance(value,str) or re.fullmatch(pattern,value) is None:_fail('cancellation_broker_time_unverified')
    return datetime.strptime(day+value,'%Y%m%d%H:%M:%S' if colon else '%Y%m%d%H%M%S').replace(tzinfo=KST)


def _no_rejection(value):
    return value in (None,'') or isinstance(value,str) and re.fullmatch(r'0{1,12}',value) is not None


def _exists(db):
    return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(TABLE,)).fetchone() is not None


def create_settlement_schema(db):
    db.execute(f'''CREATE TABLE IF NOT EXISTS {TABLE} (
        scope TEXT NOT NULL,day TEXT NOT NULL,mode TEXT NOT NULL,original_ord_no TEXT NOT NULL,
        symbol TEXT NOT NULL,confirmed_at TEXT NOT NULL,filled_qty INTEGER NOT NULL,filled_amount TEXT NOT NULL,
        cancelled_qty INTEGER NOT NULL,financial_fingerprint TEXT NOT NULL,evidence_json TEXT NOT NULL,
        evidence_sha256 TEXT NOT NULL,PRIMARY KEY(scope,day,mode,original_ord_no))''')
    for action in ('UPDATE','DELETE'):
        db.execute(f"CREATE TRIGGER IF NOT EXISTS {TABLE}_no_{action.lower()} BEFORE {action} ON {TABLE} "
                   "BEGIN SELECT RAISE(ABORT, 'final cancellation evidence is immutable'); END")
    db.execute(f'''CREATE TABLE IF NOT EXISTS {PUBLICATIONS} (
        scope TEXT NOT NULL,day TEXT NOT NULL,mode TEXT NOT NULL,original_ord_no TEXT NOT NULL,
        published_at TEXT NOT NULL,evidence_sha256 TEXT NOT NULL,accounting_sha256 TEXT NOT NULL,
        PRIMARY KEY(scope,day,mode,original_ord_no))''')
    for action in ('UPDATE','DELETE'):
        db.execute(f"CREATE TRIGGER IF NOT EXISTS {PUBLICATIONS}_no_{action.lower()} BEFORE {action} ON {PUBLICATIONS} "
                   "BEGIN SELECT RAISE(ABORT, 'cancellation publication evidence is immutable'); END")


def _validate_schema(db):
    if not _exists(db):return False
    if tuple(r[1] for r in db.execute(f'PRAGMA table_info({TABLE})'))!=COLUMNS:
        _fail('cancellation_settlement_schema_unverified')
    cols=list(db.execute(f'PRAGMA table_info({TABLE})'))
    if tuple(r[1] for r in sorted((r for r in cols if r[5]),key=lambda r:r[5]))!=('scope','day','mode','original_ord_no'):
        _fail('cancellation_settlement_schema_unverified')
    triggers={r[0]:r[1] for r in db.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger' AND tbl_name=?",(TABLE,))}
    for action in ('update','delete'):
        sql=' '.join(triggers.get(TABLE+'_no_'+action,'').upper().split())
        if f'BEFORE {action.upper()} ON' not in sql or 'RAISE(ABORT' not in sql:
            _fail('cancellation_settlement_schema_unverified')
    return True


def _source_events(db,scope,day):
    install_evidence_view(db)
    rows=db.execute(f"SELECT id,digest,payload_json,source_api FROM {evidence_table(db)} WHERE scope=? AND day=? AND mode='paper' AND source_api IN ('00','kt00009') ORDER BY id LIMIT 20001",(scope,day)).fetchall()
    if len(rows)>20000:_fail('cancellation_observation_limit')
    result=[]
    for r in rows:
        raw=r['payload_json']
        if not isinstance(raw,str) or hashlib.sha256(raw.encode()).hexdigest()!=r['digest']:
            _fail('cancellation_source_hash_mismatch')
        event=json.loads(raw)
        if event.get('mode')!='paper' or event.get('day')!=day or event.get('source_api')!=r['source_api']:
            _fail('cancellation_source_identity_mismatch')
        result.append((r['id'],r['digest'],event))
    return result


def financial_fingerprint(db,scope,order,child_numbers):
    """Only this original/descendant chain; unrelated new orders do not stale it.

    Include latest semantic REST rows as well as the stored projection so a
    later REST revision blocks before its asynchronous materialization occurs.
    Duplicate identical 00 deliveries are deduplicated by semantic payload.
    """
    number=order['ord_no'];day=order['day'];symbol=order['symbol']
    current=db.execute('SELECT * FROM orders WHERE scope=? AND day=? AND mode=? AND ord_no=?',
                       (scope,day,'paper',number)).fetchone()
    if current is None:_fail('cancellation_original_missing')
    events=_source_events(db,scope,day)
    chain={number,*child_numbers}
    changed=True
    while changed:
        changed=False
        for _,_,e in events:
            rows=([e.get('values',{})] if e.get('source_api')=='00' else
                  e.get('response_body',{}).get('acnt_ord_cntr_prst_array',[]))
            for r in rows:
                if not isinstance(r,dict):continue
                parent=r.get('904') if e.get('source_api')=='00' else r.get('orig_ord_no')
                child=r.get('9203') if e.get('source_api')=='00' else r.get('ord_no')
                if isinstance(parent,str) and parent.lstrip('0') in chain and isinstance(child,str) and child.lstrip('0') not in chain:
                    chain.add(_number(child));changed=True
    ws=set();latest_rest={};rest_queries={}
    semantic_fields=('ord_no','orig_ord_no','stk_cd','stk_bond_tp','io_tp_nm','ord_qty','ord_uv',
                     'cnfm_qty','cntr_no','cntr_qty','cntr_uv','cntr_tm','mdfy_cncl_tp','dmst_stex_tp')
    for _,_,e in events:
        if e.get('source_api')=='00':
            r=e.get('values',{})
            if e.get('event_kind')=='execution_stream' and isinstance(r,dict) and r.get('9203','').lstrip('0') in chain:
                ws.add(_json(r))
        elif e.get('status')=='success':
            rest_queries.setdefault(e.get('query_id'),[]).append(e)
    # Merge every page of a query before choosing its per-order projection.
    # Page boundaries are a transport detail, never an order quantity change.
    for pages in rest_queries.values():
        grouped={}
        for e in sorted(pages,key=lambda p:p.get('page',0)):
            rows=e.get('response_body',{}).get('acnt_ord_cntr_prst_array',[])
            if not isinstance(rows,list):_fail('cancellation_source_schema_unverified')
            for r in rows:
                if not isinstance(r,dict):_fail('cancellation_source_schema_unverified')
                if r.get('ord_no','').lstrip('0') in chain:
                    key=r['ord_no'].lstrip('0')
                    grouped.setdefault(key,set()).add(_json({k:r.get(k) for k in semantic_fields}))
        for key,values in grouped.items():latest_rest[key]=sorted(values)
    intents=[dict(r) for r in db.execute('SELECT * FROM intents WHERE scope=? AND day=? AND mode=? AND order_number=? ORDER BY id',(scope,day,'paper',number))]
    fills=[dict(r) for r in db.execute('SELECT ord_no,fill_no,symbol,side,price,quantity,strategy,moment FROM fills WHERE scope=? AND day=? AND mode=? AND ord_no=? ORDER BY fill_no',(scope,day,'paper',number))]
    return _hash({'order':dict(current),'intents':intents,'fills':fills,'chain':sorted(chain),
                  'ws_semantics':sorted(ws),'latest_rest_semantics':latest_rest})


def _verified_record(db,scope,order):
    from kiwoom_autotrader.trading.unfilled_buy_ttl import is_ttl_strategy, TTLLedger
    if not _validate_schema(db):return None
    r=db.execute(f'SELECT * FROM {TABLE} WHERE scope=? AND day=? AND mode=? AND original_ord_no=?',
                 (scope,order['day'],order['mode'],order['ord_no'])).fetchone()
    if r is None:return None
    raw=r['evidence_json']
    if not isinstance(raw,str) or hashlib.sha256(raw.encode()).hexdigest()!=r['evidence_sha256']:
        _fail('cancellation_settlement_hash_mismatch')
    proof=json.loads(raw)
    expected={'schema':'paper-buy-cancellation-final-v1','scope':scope,'mode':'paper',
              'day':order['day'],'original_order_number':order['ord_no'],'symbol':order['symbol']}
    if (any(proof.get(k)!=v for k,v in expected.items()) or order['side']!='buy' or not is_ttl_strategy(order['strategy'])
            or proof.get('strategy', 'gdcrs') != order['strategy']
            or r['symbol']!=order['symbol'] or order['mode']!='paper'
            or type(r['cancelled_qty']) is not int or type(r['filled_qty']) is not int
            or r['cancelled_qty']<0 or r['filled_qty']<0 or r['cancelled_qty']+r['filled_qty']!=order['quantity']
            or proof.get('cancelled_quantity')!=r['cancelled_qty'] or proof.get('filled_quantity')!=r['filled_qty']
            or proof.get('filled_amount')!=r['filled_amount']
            or proof.get('status') not in ('cancellation_confirmed','fill_resolved')):
        _fail('cancellation_settlement_identity_mismatch')
    _time(r['confirmed_at'])
    children=proof.get('cancel_order_numbers')
    if not isinstance(children,list) or any(_number(n)!=n for n in children):
        _fail('cancellation_settlement_identity_mismatch')
    if (r['cancelled_qty']>0 and len(children)!=1
            or financial_fingerprint(db,scope,order,children)!=r['financial_fingerprint']):
        _fail('cancellation_late_order_change')
    if 'cancellation_attempt_sequence' in proof or 'cancellation_attempt_sha256' in proof:
        # Use the caller's same SQLite snapshot; no additional connection or
        # ledger creation is needed merely to validate immutable claim history.
        ledger=object.__new__(TTLLedger);ledger.scope=scope
        try:saved,outcome=ledger.latest_attempt(db,order['day'],order['ord_no'])
        except Exception:_fail('cancellation_settlement_attempt_unverified')
        if (saved is None or proof.get('cancellation_attempt_sequence')!=saved['attempt_sequence']
                or proof.get('cancellation_attempt_sha256')!=saved['evidence_sha256']
                or proof.get('cancellation_outcome_sha256')!=(ledger._outcome_digest(outcome) if outcome else None)):
            _fail('cancellation_settlement_attempt_changed')
    return dict(r),proof


def committed_settlement_record(db,scope,order):
    """For synchronous sidecar publication; not authority to release a gate."""
    return _verified_record(db,scope,order)


def _published(db,row):
    exists=db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(PUBLICATIONS,)).fetchone()
    if not exists:return False
    cols=tuple(r[1] for r in db.execute(f'PRAGMA table_info({PUBLICATIONS})'))
    if cols!=('scope','day','mode','original_ord_no','published_at','evidence_sha256','accounting_sha256'):
        _fail('cancellation_publication_schema_unverified')
    triggers={r[0]:r[1] for r in db.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger' AND tbl_name=?",(PUBLICATIONS,))}
    for action in ('update','delete'):
        sql=' '.join(triggers.get(PUBLICATIONS+'_no_'+action,'').upper().split())
        if f'BEFORE {action.upper()} ON' not in sql or 'RAISE(ABORT' not in sql:_fail('cancellation_publication_schema_unverified')
    p=db.execute(f'SELECT * FROM {PUBLICATIONS} WHERE scope=? AND day=? AND mode=? AND original_ord_no=?',
                 tuple(row[k] for k in ('scope','day','mode','original_ord_no'))).fetchone()
    if p is None:return False
    if p['evidence_sha256']!=row['evidence_sha256'] or re.fullmatch(r'[0-9a-f]{64}',p['accounting_sha256'] or '') is None:
        _fail('cancellation_publication_hash_mismatch')
    if _time(p['published_at'])<_time(row['confirmed_at']):_fail('cancellation_publication_time_unverified')
    return True


def confirmed_cancelled_quantity(db,scope,order):
    record=_verified_record(db,scope,order)
    return record[0]['cancelled_qty'] if record and _published(db,record[0]) else 0


def settlement_is_published(db,scope,order):
    record=_verified_record(db,scope,order)
    return bool(record and _published(db,record[0]))


def confirmed_cancel_order_numbers(db,scope,day,mode,symbol):
    if mode!='paper' or not _validate_schema(db):return set()
    numbers=set()
    for row in db.execute(f'SELECT original_ord_no FROM {TABLE} WHERE scope=? AND day=? AND mode=? AND symbol=?',(scope,day,mode,symbol)):
        order=db.execute('SELECT * FROM orders WHERE scope=? AND day=? AND mode=? AND ord_no=?',(scope,day,mode,row[0])).fetchone()
        if order is None:_fail('cancellation_original_missing')
        record=_verified_record(db,scope,order)
        if _published(db,record[0]):numbers.update(record[1]['cancel_order_numbers'])
    return numbers


def settlement_watermark_rows(db,scope,mode,symbol):
    if mode!='paper' or not _validate_schema(db):return []
    rows=db.execute(f'SELECT * FROM {TABLE} WHERE scope=? AND mode=? AND symbol=? ORDER BY day,original_ord_no',(scope,mode,symbol)).fetchall()
    result=[]
    for row in rows:
        order=db.execute('SELECT * FROM orders WHERE scope=? AND day=? AND mode=? AND ord_no=?',(scope,row['day'],mode,row['original_ord_no'])).fetchone()
        if order is None:_fail('cancellation_original_missing')
        _verified_record(db,scope,order)
        result.append(list(row))
        if _published(db,row):
            result.append(list(db.execute(f'SELECT * FROM {PUBLICATIONS} WHERE scope=? AND day=? AND mode=? AND original_ord_no=?',
                (scope,row['day'],mode,row['original_ord_no'])).fetchone()))
    return result


def _complete_pages(events,api,request,now,started):
    if not events or len(events)>20:_fail('cancellation_query_completion_missing')
    ids={e.get('query_id') for e in events}
    if len(ids)!=1 or not re.fullmatch(r'[0-9a-f]{32}',next(iter(ids)) or ''):
        _fail('cancellation_query_identity_unverified')
    rows=[]
    field='oso' if api=='ka10075' else 'acnt_ord_cntr_prst_array'
    for i,e in enumerate(events,1):
        continuation=e.get('continuation',{})
        last=i==len(events)
        if (e.get('schema')!='kiwoom-broker-page-v1' or e.get('source_kind')!='broker_observation'
                or e.get('source_api')!=api or e.get('mode')!='paper' or e.get('day')!=now.strftime('%Y%m%d')
                or e.get('status')!='success' or e.get('page')!=i or e.get('request_body')!=request
                or not started<=_time(e.get('request_started_at'))<=_time(e.get('observed_at'))<=now
                or continuation.get('request_cont_yn')!=('N' if i==1 else 'Y')
                or continuation.get('response_cont_yn') not in ((None,'','N') if last else ('Y',))
                or continuation.get('next_key_present') is not (not last)):
            _fail('cancellation_query_completion_unverified')
        body=e.get('response_body',{})
        if body.get('return_code') not in (0,'0') or type(body.get('return_code')) is bool or not isinstance(body.get(field),list):
            _fail('cancellation_query_schema_unverified')
        rows.extend(body[field])
    if len(rows)>20000 or any(not isinstance(r,dict) for r in rows):_fail('cancellation_query_rows_unverified')
    return rows


def _fetch_pending(session,*,observer,day,before_send=None):
    kwargs={'before_send':before_send} if before_send is not None else {}
    return _request_rows(session,'ka10075',{'all_stk_tp':'0','trde_tp':'0','stex_tp':'0'},'oso',observer=observer,day=day,**kwargs)


def _fetch_orders(session,*,observer,day,before_send=None):
    kwargs={'before_send':before_send} if before_send is not None else {}
    return _request_rows(session,'kt00009',{'ord_dt':day,'stk_bond_tp':'1','mrkt_tp':'0','sell_tp':'0',
        'qry_tp':'0','stk_cd':'','fr_ord_no':'','dmst_stex_tp':'KRX'},'acnt_ord_cntr_prst_array',observer=observer,day=day,**kwargs)


def _stream(session):
    recorder=getattr(session,'execution_recorder',None)
    if recorder is None or not callable(getattr(recorder,'status',None)):_fail('cancellation_stream_unverified')
    s=recorder.status()
    if (not isinstance(s,dict) or s.get('ready') is not True or s.get('error') is not None
            or any(type(s.get(k)) is not int or s[k]<0 for k in ('gap_count','observations','ingress_sequence','engine_ingress_sequence'))
            or s.get('inflight_callbacks')!=0 or type(s.get('inflight_callbacks')) is not int
            or s.get('engine_pending_callbacks')!=0 or type(s.get('engine_pending_callbacks')) is not int
            or s['ingress_sequence']!=s['engine_ingress_sequence']):_fail('cancellation_stream_unverified')
    return (recorder,*(s[k] for k in ('gap_count','observations','ingress_sequence','engine_ingress_sequence')))


def _unit_basis(events,order,now):
    units={};terminal=[];chain={order['ord_no']}
    created=_time(order['registered_at'])
    for identifier,digest,e in events:
        if e.get('source_api')!='00':continue
        at=_time(e.get('observed_at'))
        if at>now:_fail('cancellation_future_observation')
        v=e.get('values',{})
        if e.get('event_kind')!='execution_stream' or not isinstance(v,dict):continue
        if v.get('9203','').lstrip('0')==order['ord_no']:
            if (_symbol(v.get('9001'))!=order['symbol'] or v.get('907')!='2'
                    or v.get('905','').strip(' ').lstrip('+-')!='매수' or v.get('2134')!='1'
                    or v.get('2136')!='N' or v.get('922')!='00'
                    or v.get('913')=='거부' or not _no_rejection(v.get('919'))
                    or _num(v.get('901'),positive=True)!=Decimal(order['order_price'])
                    or _int(v.get('900'),positive=True)!=order['quantity']
                    or _number(v.get('904'),zero=True)!='0'):_fail('cancellation_00_identity_conflict')
            if v.get('913')!='체결':continue
            if at<created-timedelta(seconds=5):_fail('cancellation_00_time_conflict')
            broker_at=_broker_time(order['day'],v.get('908'))
            if not _time(order['created_at'])-timedelta(seconds=1)<=broker_at<=at+timedelta(seconds=1):
                _fail('cancellation_00_time_conflict')
            fill_id=_number(v.get('909'));qty=_int(v.get('915'),positive=True);price=_num(v.get('914'),positive=True)
            cumulative=_int(v.get('911'),positive=True);amount=_num(v.get('903'),positive=True)
            item={'id':fill_id,'qty':qty,'price':str(price),'cum':cumulative,'amount':str(amount),'time':v.get('908')}
            if fill_id in units and units[fill_id]!=item:_fail('cancellation_conflicting_unit_execution')
            units[fill_id]=item
        if v.get('904','').lstrip('0')==order['ord_no']:
            child=_number(v.get('9203'));chain.add(child)
            terminal.append((identifier,digest,e))
    changed=True
    while changed:
        changed=False
        for _,_,e in events:
            v=e.get('values',{})
            if e.get('source_api')=='00' and e.get('event_kind')=='execution_stream' and isinstance(v,dict):
                if v.get('904','').lstrip('0') in chain:
                    child=_number(v.get('9203'))
                    if child not in chain:chain.add(child);changed=True
    count=0;amount=Decimal(0);prefix={0:Decimal(0)}
    for unit in sorted(units.values(),key=lambda u:u['cum']):
        count+=unit['qty'];amount+=Decimal(unit['price'])*unit['qty']
        if unit['cum']!=count or Decimal(unit['amount'])!=amount:_fail('cancellation_unit_sequence_incomplete')
        prefix[count]=amount
    if count>order['quantity']:_fail('cancellation_unit_quantity_conflict')
    return count,amount,units,prefix,terminal,chain


def _verify_projection(db,scope,order,qty,amount):
    rows=db.execute('SELECT * FROM fills WHERE scope=? AND day=? AND mode=? AND ord_no=?',(scope,order['day'],'paper',order['ord_no'])).fetchall()
    if any(r['symbol']!=order['symbol'] or r['side']!='buy' or r['strategy']!=order['strategy'] for r in rows):_fail('cancellation_projection_identity_conflict')
    reference=sum((_num(r['price'],positive=True)*r['quantity'] for r in rows),Decimal(0))
    if (sum(_int(r['quantity'],positive=True) for r in rows)!=qty
            or any(_num(r['price'])!=_int(r['price']) for r in rows)
            or (reference!=amount and not 0<=amount-reference<qty)):
        _fail('cancellation_projection_not_reconciled')


def _verify_rest_original(rows,order,qty,amount,units,prefix):
    selected=[r for r in rows if _number(r.get('ord_no'))==order['ord_no']]
    if not selected:_fail('cancellation_original_rest_missing')
    unique={_json(r):r for r in selected}
    for r in unique.values():
        if (_symbol(r.get('stk_cd'))!=order['symbol'] or r.get('stk_bond_tp')!='1'
                or r.get('io_tp_nm')!='현금매수' or r.get('dmst_stex_tp')!='KRX'
                # Live paper kt00009 returns '일반' for an unchanged original;
                # the documentation example uses ''. Identity/qty/price and
                # the entire descendant chain are still validated separately.
                or _number(r.get('orig_ord_no'),zero=True)!='0' or r.get('mdfy_cncl_tp') not in ('',None,'일반')
                or _int(r.get('ord_qty'),positive=True)!=order['quantity']
                or _num(r.get('ord_uv'),positive=True)!=Decimal(order['order_price'])):
            _fail('cancellation_rest_original_identity_conflict')
    if qty==0:
        if any(_int(r.get('cntr_qty'))!=0 for r in unique.values()):_fail('cancellation_rest_execution_mismatch')
        return 'zero_execution'
    # REST may be one cumulative average row or true per-unit rows. Never sum
    # cumulative projections as if they were independent unit executions.
    def same_cumulative_time(r):
        cumulative=_int(r.get('cntr_qty'))
        if cumulative==0:return True
        matches=[u for u in units.values() if u['cum']==cumulative]
        return bool(matches and _broker_time(order['day'],r.get('cntr_tm'),colon=True)==_broker_time(order['day'],matches[0]['time']))
    if all(_int(r.get('cntr_qty')) in prefix and same_cumulative_time(r)
           and _num(r.get('cntr_uv'))==_int(r.get('cntr_uv'))
           and (_num(r.get('cntr_uv'))*_int(r.get('cntr_qty'))==prefix[_int(r.get('cntr_qty'))]
                or 0<=prefix[_int(r.get('cntr_qty'))]-_num(r.get('cntr_uv'))*_int(r.get('cntr_qty'))<_int(r.get('cntr_qty')))
           for r in unique.values()) and max(_int(r.get('cntr_qty')) for r in unique.values())==qty:
        return 'cumulative_rows_reconciled_to_00'
    matched={}
    for r in unique.values():
        key=_number(r.get('cntr_no'));unit=units.get(key)
        if (unit is None or _int(r.get('cntr_qty'))!=unit['qty'] or _num(r.get('cntr_uv'))!=Decimal(unit['price'])
                or _broker_time(order['day'],r.get('cntr_tm'),colon=True)!=_broker_time(order['day'],unit['time'])):
            _fail('cancellation_rest_execution_mismatch')
        matched[key]=unit
    if set(matched)!=set(units):_fail('cancellation_rest_execution_mismatch')
    return 'unique_unit_rows_reconciled_to_00'


def _publish_settlement(tracker,ledger,order,clock,guard):
    """A release becomes effective only after synchronous sidecar publication."""
    guard()
    with ledger.db() as db:
        record=_verified_record(db,tracker.scope,order)
        if record is None:_fail('cancellation_final_record_missing')
        if _published(db,record[0]):return
        evidence_sha=record[0]['evidence_sha256']
    materialize=getattr(tracker,'_materialize_settlement_accounting',None)
    if callable(materialize):
        materialize(dict(order),evidence_sha,guard=guard)
    else:
        # Legacy test adapters may expose only the ordinary same-day writer.
        # They cannot gain an unverified prior-day work-deferral exception.
        if order['day']!=_time(clock()).strftime('%Y%m%d'):
            _fail('cancellation_prior_day_publication_adapter_required')
        materialize=getattr(tracker,'_materialize_accounting',None)
        if not callable(materialize):_fail('cancellation_sidecar_writer_unavailable')
        guard();materialize(order['day'])
    target=tracker.log_dir/f"{order['day']}.execution-accounting.json"
    if target.is_symlink() or not target.is_file() or target.stat().st_size>64*1024*1024:
        _fail('cancellation_sidecar_unavailable')
    raw=target.read_bytes()
    document=json.loads(raw)
    items=document.get('cancellation_settlements')
    if (not isinstance(items,list) or not any(isinstance(i,dict) and i.get('evidence_sha256')==evidence_sha
            and i.get('original_order_number')==order['ord_no'] and i.get('mode')=='paper'
            and i.get('status')=='final_evidence_committed' for i in items)):
        _fail('cancellation_sidecar_evidence_missing')
    guard()
    with ledger.db(write=True) as db:
        guard();record=_verified_record(db,tracker.scope,order)
        if record is None or record[0]['evidence_sha256']!=evidence_sha:_fail('cancellation_sidecar_evidence_changed')
        db.execute(f'INSERT OR IGNORE INTO {PUBLICATIONS} VALUES (?,?,?,?,?,?,?)',
            (tracker.scope,order['day'],'paper',order['ord_no'],_time(clock()).isoformat(),evidence_sha,hashlib.sha256(raw).hexdigest()))


def _owned_cancellation(db,scope,day,claim,ledger):
    from kiwoom_autotrader.trading.unfilled_buy_ttl import ttl_strategy_sql,is_ttl_strategy
    # A transport-proven NOT_SENT attempt may have a later immutable retry.
    # Resolve its validated chain rather than the original non-sent receipt.
    try:saved,outcome=ledger.latest_attempt(db,day,claim['original_ord_no'])
    except Exception:_fail('cancellation_claim_chain_unverified')
    if saved is None:_fail('cancellation_owned_claim_missing')
    if ('attempt_sequence' in claim and claim['attempt_sequence']!=saved['attempt_sequence']):
        _fail('cancellation_claim_generation_changed')
    raw=saved['evidence_json']
    if hashlib.sha256(raw.encode()).hexdigest()!=saved['evidence_sha256']:_fail('cancellation_claim_hash_mismatch')
    strategy_predicate, strategy_values = ttl_strategy_sql('o.strategy')
    original=db.execute(f'''SELECT o.*,d.order_price,i.created_at,i.id AS intent_id FROM orders o JOIN intents i
        ON i.scope=o.scope AND i.day=o.day AND i.mode=o.mode AND i.order_number=o.ord_no
        JOIN order_decisions d ON d.scope=i.scope AND d.intent_id=i.id WHERE o.scope=? AND o.day=?
        AND o.mode='paper' AND o.ord_no=? AND o.side='buy' AND {strategy_predicate} AND i.state='accepted'
        AND i.side=o.side AND i.symbol=o.symbol AND i.quantity=o.quantity AND i.strategy=o.strategy
        AND d.order_type='limit' ''',(scope,day,saved['original_ord_no'],*strategy_values)).fetchall()
    if len(original)!=1 or original[0]['intent_id']!=saved['intent_id']:_fail('cancellation_owned_original_unverified')
    order=dict(original[0])
    if not is_ttl_strategy(order['strategy']):_fail('cancellation_owned_original_unverified')
    if json.loads(raw).get('strategy', 'gdcrs') != order['strategy']:
        _fail('cancellation_claim_strategy_mismatch')
    return saved,outcome,order


def _verify_cancellation_terminal(db,scope,order,saved,outcome,pending,orders,as_of):
    """Read-only common proof. No session access, mutation or release authority."""
    day=order['day']
    events=_source_events(db,scope,day)
    qty,amount,units,prefix,terminal,chain=_unit_basis(events,order,as_of)
    _verify_projection(db,scope,order,qty,amount)
    rest_kind=_verify_rest_original(orders,order,qty,amount,units,prefix)
    # Expand/check the ENTIRE original->amend/cancel descendant graph.
    rest_chain={order['ord_no']};changed=True
    while changed:
        changed=False
        for r in orders:
            parent=_number(r.get('orig_ord_no'),zero=True);number=_number(r.get('ord_no'))
            if parent in rest_chain and number not in rest_chain:rest_chain.add(number);changed=True
    children=[];cancelled=order['quantity']-qty
    if cancelled:
        if outcome is None or outcome['status']!='cancel_input_ack_unconfirmed_settlement':_fail('cancellation_ack_unconfirmed')
        child=_number(outcome['cancellation_ord_no']);children=[child]
        if rest_chain!={order['ord_no'],child} or chain!={order['ord_no'],child}:
            _fail('cancellation_order_chain_unverified')
        cancel_rows={_json(r):r for r in orders if _number(r.get('ord_no'))==child}
        if len(cancel_rows)!=1:_fail('cancellation_rest_terminal_missing')
        c=next(iter(cancel_rows.values()))
        if (_number(c.get('orig_ord_no'))!=order['ord_no'] or _symbol(c.get('stk_cd'))!=order['symbol']
                or c.get('mdfy_cncl_tp') not in ('취소주문','취소') or c.get('dmst_stex_tp')!='KRX'
                or c.get('stk_bond_tp')!='1' or c.get('io_tp_nm')!='현금매수'
                or _int(c.get('cnfm_qty'),positive=True)!=cancelled
                or _int(c.get('ord_qty'),positive=True)!=cancelled or _int(c.get('cntr_qty'))!=0):
            _fail('cancellation_rest_terminal_unverified')
        accepted=_time(outcome['observed_at']);confirmed=[]
        if not _time(saved['claimed_at'])<=accepted<=as_of:_fail('cancellation_ack_time_unverified')
        for identifier,digest,e in terminal:
            v=e['values']
            if _number(v.get('9203'))!=child:_fail('cancellation_foreign_child')
            # Fixed-width live 00 may prefix a plain space, not a +/- sign.
            if (_symbol(v.get('9001'))!=order['symbol'] or v.get('905','').strip(' ').lstrip('+-')!='매수취소'
                    or v.get('907')!='2' or v.get('2134')!='1' or v.get('2136')!='N'
                    or v.get('922')!='00' or not _no_rejection(v.get('919'))
                    or v.get('913')=='거부'):_fail('cancellation_00_terminal_unverified')
            if v.get('913') not in ('확인','취소'):continue
            broker_at=_broker_time(day,v.get('908'))
            if (_int(v.get('900'),positive=True)!=cancelled or _int(v.get('902'))!=0
                    or v.get('909') not in (None,'','0','0000000')
                    or not all(_no_rejection(v.get(k)) for k in ('903','910','911','914','915'))
                    or not _time(saved['claimed_at'])-timedelta(seconds=1)<=broker_at<=_time(e['observed_at'])+timedelta(seconds=1)
                    or _time(e['observed_at'])<_time(saved['claimed_at'])):
                _fail('cancellation_00_terminal_unverified')
            confirmed.append({'observation_id':identifier,'digest':digest})
        if not confirmed:_fail('cancellation_00_terminal_missing')
        status='cancellation_confirmed'
    else:
        # Full-fill race resolves the original with ZERO cancellation
        # release. No ACK or fabricated cancellation is needed or emitted.
        if rest_chain-{order['ord_no']} or chain-{order['ord_no']}:
            _fail('cancellation_full_fill_chain_unverified')
        confirmed=[];status='fill_resolved'
    for p in pending:
        pnumber=_number(p.get('ord_no'));parent=_number(p.get('orig_ord_no'),zero=True)
        if pnumber in rest_chain or parent in rest_chain:
            if _symbol(p.get('stk_cd'))!=order['symbol'] or _int(p.get('oso_qty'))!=0:
                _fail('cancellation_broker_remainder_not_zero')
    fingerprint=financial_fingerprint(db,scope,order,children)
    return dict(qty=qty,amount=amount,children=children,cancelled=cancelled,
                confirmed=confirmed,status=status,rest_kind=rest_kind,fingerprint=fingerprint)


def _snapshot_query_pages(db,scope,day,api,request):
    """Latest matching query only, never fall back past failed/partial evidence."""
    rows=db.execute(f"SELECT id,digest,payload_json,source_api,observed_at FROM {evidence_table(db)} "
        "WHERE scope=? AND day=? AND mode='paper' AND source_api=? ORDER BY id DESC LIMIT 1001",
        (scope,day,api)).fetchall()
    query=None;selected=[]
    for r in rows:
        raw=r['payload_json']
        if not isinstance(raw,str) or hashlib.sha256(raw.encode()).hexdigest()!=r['digest']:
            _fail('cancellation_source_hash_mismatch')
        event=json.loads(raw)
        if (event.get('mode')!='paper' or event.get('day')!=day or event.get('source_api')!=api
                or event.get('observed_at')!=r['observed_at']):
            _fail('cancellation_source_identity_mismatch')
        if event.get('request_body')!=request:continue
        if query is None:query=event.get('query_id')
        if event.get('query_id')==query:selected.append((r['id'],r['digest'],event))
        elif selected:break
    if not selected or len(selected)>20:_fail('cancellation_query_completion_missing')
    return sorted(selected,key=lambda x:x[2].get('page',0))


def cancellation_reconciliation_candidate(db,scope,day,original_order_number,*,now,max_age_seconds=90):
    """Strict zero-fill bootstrap candidate from a caller's single RO snapshot.

    Caller installs the TEMP evidence view BEFORE query_only/BEGIN. This helper
    requires query_only and an active transaction. It does not create schemas,
    access a BrokerSession, write evidence or release reservations. A subsequent
    live-session settle_cancellation must freshly revalidate and publish.
    """
    from kiwoom_autotrader.trading.unfilled_buy_ttl import TTLLedger
    at=_time(now)
    if (db.execute('PRAGMA query_only').fetchone()[0]!=1 or not db.in_transaction
            or day!=at.strftime('%Y%m%d') or not isinstance(scope,str) or not scope
            or type(max_age_seconds) is not int or not 0<max_age_seconds<=90):
        _fail('cancellation_readonly_snapshot_unverified')
    number=_number(original_order_number)
    ledger=object.__new__(TTLLedger);ledger.scope=scope
    saved,outcome,order=_owned_cancellation(db,scope,day,{'original_ord_no':number},ledger)
    if (saved['symbol']!=order['symbol'] or not _time(order['registered_at'])<=_time(saved['claimed_at'])<=at):
        _fail('cancellation_claim_identity_mismatch')
    requests={'ka10075':{'all_stk_tp':'0','trde_tp':'0','stex_tp':'0'},
        'kt00009':{'ord_dt':day,'stk_bond_tp':'1','mrkt_tp':'0','sell_tp':'0','qry_tp':'0',
                   'stk_cd':'','fr_ord_no':'','dmst_stex_tp':'KRX'}}
    groups={api:_snapshot_query_pages(db,scope,day,api,request) for api,request in requests.items()}
    lower=at-timedelta(seconds=max_age_seconds)
    rows={api:_complete_pages([x[2] for x in groups[api]],api,request,at,lower)
          for api,request in requests.items()}
    verified=_verify_cancellation_terminal(db,scope,order,saved,outcome,rows['ka10075'],rows['kt00009'],at)
    if (verified['status']!='cancellation_confirmed' or verified['qty']!=0
            or verified['amount']!=0 or verified['cancelled']!=order['quantity']):
        _fail('cancellation_bootstrap_requires_zero_fill_full_cancel')
    proof={'schema':'paper-cancellation-bootstrap-candidate-v1','status':'bootstrap_reconciliation_candidate',
        'scope':scope,'day':day,'mode':'paper','original_order_number':number,'symbol':order['symbol'],
        'strategy':order['strategy'],'as_of':at.isoformat(),'settled':False,'reservations_released':False,
        'filled_quantity':0,'filled_amount':'0','cancelled_quantity':verified['cancelled'],
        'original_quantity':order['quantity'],'cancel_order_numbers':verified['children'],
        'financial_fingerprint':verified['fingerprint'],'terminal_00':verified['confirmed'],
        'pending_query_id':groups['ka10075'][0][2]['query_id'],
        'orders_query_id':groups['kt00009'][0][2]['query_id'],
        'source_pages':{api:[{'observation_id':i,'sha256':h,'observed_at':e['observed_at'],
            'request_started_at':e['request_started_at'],'page':e['page']} for i,h,e in group]
            for api,group in groups.items()},
        'cancellation_attempt_sequence':saved['attempt_sequence'],
        'cancellation_attempt_sha256':saved['evidence_sha256'],
        'cancellation_outcome_sha256':ledger._outcome_digest(outcome),
        'cash_adjustment_krw':0,'cancelled_principal_released_krw':0,
        'requires_fresh_live_session_settlement_and_publication':True}
    proof['evidence_sha256']=_hash(proof)
    return proof


def settle_cancellation(session,tracker,claim,*,now=None,monotonic=None,pending_fetcher=None,
                        order_fetcher=None,before_commit=None):
    """Perform bounded verification reads; NEVER submits or repeats a cancel.

    A restart may re-run these fresh reads. Scope is the same tracker bound to
    the current session's reconciliation gate; all reads/commit use one stable
    current paper session. Old token hashes are audit evidence, not credentials.
    """
    from kiwoom_autotrader.trading.unfilled_buy_ttl import TTLLedger, ttl_strategy_sql, is_ttl_strategy
    clock=now or (lambda:datetime.now(KST));mono=monotonic or time.monotonic
    started=_time(clock());start_clock=mono();identity=session_identity(session);stream=_stream(session)
    if type(start_clock) not in (int,float) or not math.isfinite(start_clock):_fail('cancellation_clock_unverified')
    gate=getattr(session,'paper_automation_reconcile',None)
    if identity[0]!='paper' or getattr(gate,'_tracker',None) is not tracker or getattr(gate,'_session',None) is not session:
        _fail('cancellation_account_scope_unverified')
    ledger=TTLLedger(tracker);day=started.strftime('%Y%m%d')
    with ledger.db() as db:
        saved,outcome,order=_owned_cancellation(db,tracker.scope,day,claim,ledger)
        prior=_verified_record(db,tracker.scope,order)
        if prior and _published(db,prior[0]):return {'status':'already_settled','original_order_number':order['ord_no']}
        # The starting fingerprint includes original financial rows and all
        # relevant 00/REST chain records; guard below additionally tracks the
        # immutable stream ingress for late events during these queries.
        initial_fills=[tuple(r) for r in db.execute('SELECT * FROM fills WHERE scope=? AND day=? AND mode=? AND ord_no=?',(tracker.scope,day,'paper',order['ord_no']))]
    if (saved['symbol']!=order['symbol'] or _time(saved['claimed_at'])>started
            or _time(order['registered_at'])>_time(saved['claimed_at'])):_fail('cancellation_claim_identity_mismatch')
    pending_events=[];order_events=[]
    def observer(destination):
        def save(e):
            tracker.record_broker_observation(e);destination.append(e)
        return save
    def guard():
        current=_time(clock());elapsed=mono()-start_clock
        if (session_identity(session)!=identity or _stream(session)!=stream or gate is not getattr(session,'paper_automation_reconcile',None)
                or getattr(gate,'_tracker',None) is not tracker or getattr(gate,'_session',None) is not session
                or not 0<=(current-started).total_seconds()<=20
                or type(elapsed) not in (int,float) or not math.isfinite(elapsed) or not 0<=elapsed<=20
                or current.date()!=started.date()):_fail('cancellation_reconciliation_changed_or_stale')
    (pending_fetcher or _fetch_pending)(session,observer=observer(pending_events),day=day);guard()
    (order_fetcher or _fetch_orders)(session,observer=observer(order_events),day=day);guard()
    as_of=_time(clock())
    pending=_complete_pages(pending_events,'ka10075',{'all_stk_tp':'0','trde_tp':'0','stex_tp':'0'},as_of,started)
    orders=_complete_pages(order_events,'kt00009',{'ord_dt':day,'stk_bond_tp':'1','mrkt_tp':'0','sell_tp':'0','qry_tp':'0','stk_cd':'','fr_ord_no':'','dmst_stex_tp':'KRX'},as_of,started)
    with ledger.db() as db:
        verified=_verify_cancellation_terminal(db,tracker.scope,order,saved,outcome,pending,orders,as_of)
        qty=verified['qty'];amount=verified['amount'];children=verified['children']
        cancelled=verified['cancelled'];confirmed=verified['confirmed'];status=verified['status']
        rest_kind=verified['rest_kind'];fingerprint=verified['fingerprint']
        if initial_fills!=[tuple(r) for r in db.execute('SELECT * FROM fills WHERE scope=? AND day=? AND mode=? AND ord_no=?',(tracker.scope,day,'paper',order['ord_no']))]:
            _fail('cancellation_fill_changed_during_query')
    proof={'schema':'paper-buy-cancellation-final-v1','scope':tracker.scope,'day':day,'mode':'paper',
        'original_order_number':order['ord_no'],'symbol':order['symbol'],'strategy':order['strategy'],'status':status,
        'filled_quantity':qty,'filled_amount':str(amount),'cancelled_quantity':cancelled,'cancel_order_numbers':children,
        'pending_query_id':pending_events[0]['query_id'],'orders_query_id':order_events[0]['query_id'],
        'query_started_at':started.isoformat(),'confirmed_at':as_of.isoformat(),'rest_projection_kind':rest_kind,
        'terminal_00':confirmed,'source_page_sha256':[hashlib.sha256(_json(e).encode()).hexdigest() for e in pending_events+order_events],
        'session_sha256':hashlib.sha256((identity[0]+'\0'+identity[1]).encode()).hexdigest(),
        'stream_counters':list(stream[1:]),'original_quantity':order['quantity'],
        'cancellation_attempt_sequence':saved['attempt_sequence'],
        'cancellation_attempt_sha256':saved['evidence_sha256'],
        'cancellation_outcome_sha256':ledger._outcome_digest(outcome) if outcome else None,
        'cash_adjustment_krw':0,'filled_principal_released_krw':0,'cancelled_limit_principal_krw':cancelled*int(order['order_price'])}
    if before_commit is not None:before_commit()
    guard()
    with ledger.db(write=True) as db:
        guard()
        if financial_fingerprint(db,tracker.scope,order,children)!=fingerprint:_fail('cancellation_changed_before_commit')
        create_settlement_schema(db);_validate_schema(db)
        prior=_verified_record(db,tracker.scope,order)
        if not prior:
            raw=_json(proof)
            db.execute(f'INSERT INTO {TABLE} VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',(tracker.scope,day,'paper',order['ord_no'],order['symbol'],as_of.isoformat(),qty,str(amount),cancelled,fingerprint,raw,hashlib.sha256(raw.encode()).hexdigest()))
    _publish_settlement(tracker,ledger,order,clock,guard)
    return {'status':status,'original_order_number':order['ord_no'],'cancelled_quantity':cancelled,
            'filled_quantity':qty,'filled_amount':str(amount),'cash_adjustment_krw':0,'confirmed_at':as_of.isoformat()}
