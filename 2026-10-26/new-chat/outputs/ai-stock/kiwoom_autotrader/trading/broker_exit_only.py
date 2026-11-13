"""Broker-confirmed protective liquidation without claiming fill attribution.

This deliberately does not repair ``multi_strategy_position``. It proves only
the current cash quantity in a same-day, bot-originated ownership cycle. Prices
and costs of missing unit executions remain unknown. No network calls occur.
"""
from datetime import datetime
import hashlib
import json
import re

from kiwoom_autotrader.api.session import KST
from .decision_evidence import canonical_json
from .broker_observation_repeats import evidence_table


def _time(value):
    result = datetime.fromisoformat(value)
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError('exit_only_clock_unverified')
    return result.astimezone(KST)


def _int(value, *, positive=False):
    if not isinstance(value,str) or re.fullmatch(r'\+?[0-9]{1,20}',value) is None:
        raise ValueError('exit_only_number_unverified')
    result=int(value)
    if positive and result<=0:raise ValueError('exit_only_number_unverified')
    return result


def _symbol(value):
    if not isinstance(value,str) or re.fullmatch(r'A?[0-9]{6}',value) is None:
        raise ValueError('exit_only_symbol_unverified')
    return value[-6:]


def _number(value):
    n=_int(value,positive=True)
    if len(value)>7:raise ValueError('exit_only_order_number_unverified')
    return str(n)


def latest_complete_pages(entries, api, day, now):
    groups={}
    for entry,event in entries:
        if entry['source_api']==api:groups.setdefault(event.get('query_id'),[]).append((entry,event))
    if not groups:raise ValueError('exit_only_'+api+'_missing')
    pages=sorted(max(groups.values(),key=lambda rows:max(x[0]['id'] for x in rows)),key=lambda x:x[1].get('page',0))
    rows=[];first=last=None
    for idx,(entry,event) in enumerate(pages):
        body=event['request_body'];response=event['response_body'];c=event['continuation']
        start=_time(event['request_started_at']);end=_time(event['observed_at'])
        if (event.get('page')!=idx+1 or event.get('status')!='success'
                or not start<=end<=now or last is not None and start<last
                or response.get('return_code') not in (0,'0') or isinstance(response.get('return_code'),bool)
                or c.get('request_cont_yn')!=('N' if idx==0 else 'Y')
                or idx<len(pages)-1 and (c.get('response_cont_yn')!='Y' or c.get('next_key_present') is not True)
                or idx==len(pages)-1 and (c.get('response_cont_yn') not in (None,'','N')
                    or c.get('next_key_present') is not False)):
            raise ValueError('exit_only_'+api+'_incomplete')
        if api=='kt00007':
            if (body.get('ord_dt')!=day or body.get('qry_tp')!='1' or body.get('stk_bond_tp') not in ('0','1')
                    or body.get('sell_tp')!='0' or body.get('stk_cd') not in (None,'')
                    or body.get('fr_ord_no') not in (None,'') or body.get('dmst_stex_tp')!='%'):
                raise ValueError('exit_only_order_detail_scope')
            items=response.get('acnt_ord_cntr_prps_dtl')
        else:
            if (body.get('all_stk_tp')!='0' or body.get('trde_tp')!='0'
                    or body.get('stex_tp')!='0' or body.get('stk_cd') not in (None,'')):
                raise ValueError('exit_only_pending_scope')
            items=response.get('oso')
        if not isinstance(items,list) or any(not isinstance(r,dict) for r in items):
            raise ValueError('exit_only_rows_unverified')
        rows.extend(items);first=first or start;last=end
    if not 0<=(now-last).total_seconds()<=90:raise ValueError('exit_only_'+api+'_stale')
    return {'started_at':first,'observed_at':last,'rows':rows,
            'query_id':pages[0][1]['query_id'],'source_ids':[x[0]['id'] for x in pages],
            'source_hashes':[x[0]['digest'] for x in pages]}


def broker_confirmed_position(tracker,db,symbol,strategy,now,*,ignored_intent_id=None):
    """Run under caller's one SQLite read/IMMEDIATE transaction."""
    failure={'status':'unsupported','ownership_status':'unsupported','owned_qty':None,
             'broker_quantity':None,'basis_krw':None,'profit_attribution':'unconfirmed',
             'generation_id':None,'first_fill_at':None,'initial_fill_at':None,
             'first_fill_observed_at':None,'first_fill_timing_verified':False,'half_take_done':False}
    try:
        if (not isinstance(now,datetime) or now.tzinfo is None or now.utcoffset() is None
                or not re.fullmatch(r'[0-9]{6}',symbol) or not isinstance(strategy,str)):
            raise ValueError('exit_only_arguments_unverified')
        now=now.astimezone(KST);day=now.strftime('%Y%m%d')
        if ignored_intent_id is not None:
            ignored=db.execute('SELECT * FROM intents WHERE scope=? AND id=?',(tracker.scope,ignored_intent_id)).fetchone()
            if (ignored is None or ignored['mode']!='paper' or ignored['symbol']!=symbol or ignored['strategy']!=strategy
                    or ignored['side']!='sell' or ignored['state']!='prepared' or ignored['order_number'] is not None):
                raise ValueError('exit_only_ignored_intent_unverified')
        balance=db.execute("SELECT * FROM account_balance_daily WHERE scope=? AND mode='paper' AND day=?",(tracker.scope,day)).fetchone()
        if balance is None or balance['source_kind']!='broker_observation':raise ValueError('exit_only_account_missing')
        account_at=_time(balance['observed_at']);account=json.loads(balance['payload_json'])
        if (not 0<=(now-account_at).total_seconds()<=90 or account_at.strftime('%Y%m%d')!=day
                or account.get('schema')!='paper-account-snapshot-v1' or account.get('mode')!='paper'
                or account.get('exchange')!='KRX' or not isinstance(account.get('holdings'),list)
                or any(type(account.get('pages',{}).get(api)) is not int or not 1<=account['pages'][api]<=20
                       for api in ('kt00001','kt00018'))):raise ValueError('exit_only_account_stale_or_incomplete')
        positions=[r for r in account['holdings'] if _symbol(r.get('stk_cd'))==symbol]
        if len(positions)!=1 or positions[0].get('crd_tp')!='00':raise ValueError('exit_only_cash_holding_unverified')
        holding=positions[0];qty=_int(holding.get('rmnd_qty'),positive=True);basis=_int(holding.get('pur_amt'),positive=True)
        sellable=_int(holding.get('trde_able_qty'))
        failure['broker_quantity']=qty
        if sellable!=qty:raise ValueError('exit_only_sellable_quantity_unverified')
        pending_intents=db.execute("""SELECT * FROM intents WHERE scope=? AND mode='paper' AND symbol=?
            AND id IS NOT ? AND state IS NOT 'cancelled' AND (state IS NOT 'accepted' OR order_number IS NULL)
            LIMIT 10001""",(tracker.scope,symbol,ignored_intent_id)).fetchall()
        if pending_intents:raise ValueError('exit_only_unresolved_intent')
        orders=[dict(row) for row in db.execute("SELECT * FROM orders WHERE scope=? AND mode='paper' AND day=? AND symbol=? ORDER BY registered_at,ord_no LIMIT 10001",(tracker.scope,day,symbol))]
        if not orders or len(orders)>10000:raise ValueError('exit_only_current_day_orders_missing')
        saved={_number(row['ord_no']):row for row in orders};intents={}
        for number,order in saved.items():
            found=db.execute("SELECT * FROM intents WHERE scope=? AND mode='paper' AND day=? AND order_number=? AND state='accepted'",(tracker.scope,day,order['ord_no'])).fetchall()
            if len(found)!=1:raise ValueError('exit_only_bot_order_receipt_unverified')
            intent=dict(found[0]);intents[number]=intent
            if any(intent[k]!=order[k] for k in ('symbol','side','quantity','strategy')):
                raise ValueError('exit_only_bot_order_receipt_conflict')
        generations=[]
        for number,intent in intents.items():
            if intent['side']!='buy' or intent['strategy']!=strategy:continue
            r=db.execute('SELECT * FROM pre_order_reconciliations WHERE scope=? AND intent_id=?',(tracker.scope,intent['id'])).fetchone()
            if r is None:continue
            raw=r['evidence_json']
            if hashlib.sha256(raw.encode()).hexdigest()!=r['evidence_sha256']:raise ValueError('exit_only_generation_hash')
            p=json.loads(raw)
            if p.get('pre_order_held_qty')==p.get('pre_order_pur_amt_krw')==0 and p.get('holdings_source')=='broker_observation' and p.get('symbol')==symbol:
                created=_time(intent['created_at']);observed=_time(p['observed_at'])
                if (p.get('schema_version') not in (1,3,5,6) or not observed<=created<=now
                        or (created-observed).total_seconds()>20 or observed.strftime('%Y%m%d')!=day):raise ValueError('exit_only_generation_clock')
                generations.append((created,number,intent))
        if not generations:raise ValueError('exit_only_zero_basis_generation_missing')
        created,first_number,first=max(generations,key=lambda x:x[0]);generation=first['id']
        owned_numbers={number for number,order in saved.items() if _time(order['registered_at'])>=created}
        if any(saved[n]['strategy']!=strategy for n in owned_numbers):raise ValueError('exit_only_mixed_owner')
        source=db.execute(f"SELECT id,day,mode,observed_at,source_api,digest,payload_json FROM {evidence_table(db)} WHERE scope=? AND mode='paper' AND day=? AND source_api IN ('00','kt00007','ka10075') ORDER BY id LIMIT 20001",(tracker.scope,day)).fetchall()
        if len(source)>20000:raise ValueError('exit_only_evidence_limit')
        parsed=[];stream=[]
        for entry in source:
            raw=entry['payload_json']
            if hashlib.sha256(raw.encode()).hexdigest()!=entry['digest']:raise ValueError('exit_only_evidence_hash')
            e=json.loads(raw);at=_time(e['observed_at'])
            if ((e.get('day'),e.get('mode'),e.get('source_api'),e.get('observed_at'))!=(day,'paper',entry['source_api'],entry['observed_at'])
                    or e.get('source_kind')!='broker_observation' or at>now):raise ValueError('exit_only_evidence_scope')
            parsed.append((entry,e))
            if entry['source_api']=='00':stream.append((entry,e,at))
        detail=latest_complete_pages(parsed,'kt00007',day,now);pending=latest_complete_pages(parsed,'ka10075',day,now)
        if not detail['observed_at']<=account_at<=pending['started_at']:
            raise ValueError('exit_only_snapshot_order_unverified')
        for r in pending['rows']:
            if _symbol(r.get('stk_cd'))==symbol and _int(r.get('oso_qty'))!=0:
                raise ValueError('exit_only_pending_order')
        detail_numbers=set();net=0
        for r in detail['rows']:
            if _symbol(r.get('stk_cd'))!=symbol:continue
            number=_number(r.get('ord_no'))
            if number in detail_numbers or number not in saved:raise ValueError('exit_only_external_or_duplicate_order')
            detail_numbers.add(number);order=saved[number]
            filled=_int(r.get('cntr_qty'));quantity=_int(r.get('ord_qty'),positive=True)
            if (r.get('dmst_stex_tp')!='KRX' or _int(r.get('ord_remnq'))!=0 or not 0<=filled<=quantity
                    or quantity!=order['quantity'] or r.get('io_tp_nm') not in
                    (('현금매수','+매수') if order['side']=='buy' else ('현금매도','-매도'))):
                raise ValueError('exit_only_order_detail_conflict')
            if number in owned_numbers:net+=filled*(1 if order['side']=='buy' else -1)
        if detail_numbers!=set(saved) or net!=qty:raise ValueError('exit_only_broker_quantity_conflict')
        lifecycle=[(at,row['id'],e['event_kind']) for row,e,at in stream if e.get('event_kind') in ('stream_started','stream_resumed','stream_gap','stream_stopped')]
        before=[item for item in lifecycle if item[0]<=detail['started_at']]
        if not before or max(before)[2] not in ('stream_started','stream_resumed'):
            raise ValueError('exit_only_stream_not_ready')
        if any(kind in ('stream_gap','stream_stopped') and at>=detail['started_at'] for at,_,kind in lifecycle):
            raise ValueError('exit_only_gap_after_snapshot')
        relevant=[];first_units=[]
        for row,e,at in stream:
            values=e.get('values',{})
            if e.get('event_kind')!='execution_stream' or values.get('9001') not in (symbol,'A'+symbol):continue
            if at>=detail['started_at']:raise ValueError('exit_only_late_symbol_event')
            if at<created:continue
            relevant.append(row['id'])
            if _number(values.get('9203')) not in owned_numbers:raise ValueError('exit_only_external_stream_order')
            if (values.get('9203','').lstrip('0')==first_number and values.get('913')=='체결'
                    and values.get('907')=='2' and values.get('909') not in ('',None,'0')):
                unit_qty=_int(values.get('915'),positive=True);unit_price=_int(values.get('914'),positive=True)
                # Only the genuinely first unit (cumulative equals this unit)
                # may supply a time stop. Later/ambiguous units still support
                # no timing claim even when broker quantity allows price exits.
                if (_int(values.get('911'))!=unit_qty or _int(values.get('903'))!=unit_qty*unit_price):continue
                tm=values.get('908','')
                if re.fullmatch(r'(?:[01][0-9]|2[0-3])[0-5][0-9][0-5][0-9]',tm):
                    fill=datetime.strptime(day+tm,'%Y%m%d%H%M%S').replace(tzinfo=KST)
                    if created<=fill<=at:first_units.append((fill,at))
        timing=None
        if first_units:
            fill,observed=min(first_units)
            anchor=[x for x in lifecycle if x[0]<=created]
            if (anchor and max(anchor)[2] in ('stream_started','stream_resumed')
                    and not any(created<=at<=observed and kind in ('stream_gap','stream_stopped') for at,_,kind in lifecycle)):
                timing={'first_fill_at':fill.isoformat(),'first_fill_observed_at':observed.isoformat(),
                        'initial_fill_at':observed.isoformat(),'first_fill_timing_verified':True}
        proof={'schema':'paper-exit-only-broker-proof-v1','day':day,'symbol':symbol,'strategy':strategy,
            'generation_id':generation,'broker_quantity':qty,'broker_purchase_amount':basis,
            'account_observed_at':account_at.isoformat(),'account_sha256':hashlib.sha256(balance['payload_json'].encode()).hexdigest(),
            'detail_query_id':detail['query_id'],'detail_source_ids':detail['source_ids'],'detail_source_hashes':detail['source_hashes'],
            'pending_query_id':pending['query_id'],'pending_source_ids':pending['source_ids'],'pending_source_hashes':pending['source_hashes'],
            'last_relevant_stream_id':max(relevant+[x[1] for x in lifecycle],default=0),
            'symbol_ledger_sha256':tracker._symbol_order_watermark(db,'paper',symbol,ignored_intent_id=ignored_intent_id),
            'ownership_status':'exit_only_broker_confirmed','profit_attribution':'unconfirmed'}
        return {**failure,'status':'exit_only_broker_confirmed','ownership_status':'exit_only_broker_confirmed',
            'owned_qty':qty,'broker_quantity':qty,'basis_krw':basis,'generation_id':generation,
            'evidence':proof,'evidence_sha256':hashlib.sha256(canonical_json(proof).encode()).hexdigest(),
            **(timing or {})}
    except (ValueError,TypeError,KeyError,OverflowError,AttributeError) as error:
        return {**failure,'reason':str(error) if isinstance(error,ValueError) else 'exit_only_evidence_unverified'}
