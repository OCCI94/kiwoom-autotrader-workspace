"""A new flat entry cycle; never repairs old ownership or profit attribution.

Only fully reconciled historical order quantities may be separated from an old
00 coverage gap. The current flat account, complete integrated pending query,
and fresh live-stream anchor remain independent mandatory evidence.
"""
from datetime import datetime
import hashlib
import json
import re

from kiwoom_autotrader.api.session import KST
from .broker_observation_repeats import evidence_table
from .broker_exit_only import latest_complete_pages


def _at(value):
    result=datetime.fromisoformat(value)
    if result.tzinfo is None or result.utcoffset() is None:raise ValueError('flat_epoch_clock')
    return result.astimezone(KST)


def _verified_symbol_flat_holdings(holdings,symbol):
    """Validate the normalized cash rows; flat means this symbol, not account.

    Other confirmed cash holdings remain counted by the existing cash/theme/
    concurrent-position gates. Unknown credit/prefix mappings or duplicate
    normalized codes cannot establish absence of the requested cash position.
    """
    if not isinstance(holdings,list) or len(holdings)>2000:return False
    seen=set()
    for row in holdings:
        if not isinstance(row,dict):return False
        code=row.get('stk_cd')
        if (not isinstance(code,str) or re.fullmatch(r'A?[0-9]{6}',code) is None
                or row.get('crd_tp')!='00' or code[-6:] in seen):return False
        seen.add(code[-6:])
        numbers={}
        for key in ('rmnd_qty','trde_able_qty','pur_amt'):
            value=row.get(key)
            if not isinstance(value,str) or re.fullmatch(r'\+?[0-9]{1,15}',value) is None:return False
            numbers[key]=int(value)
        qty=numbers['rmnd_qty'];basis=numbers['pur_amt']
        if (numbers['trde_able_qty']>qty or (qty==0 and basis!=0) or (qty>0 and basis<=0)
                or code[-6:]==symbol and qty!=0):return False
        for key in ('evltv_prft','pur_pric','cur_prc','evlt_amt'):
            value=row.get(key)
            if value is not None and (not isinstance(value,str) or re.fullmatch(r'[+-]?[0-9]{1,15}',value) is None):return False
    return True


def verified_prior_closed_flat(tracker,db,symbol,now,*,ignored_intent_id=None):
    """One caller transaction, no broker calls or mutations. Return proof or None."""
    try:
        from .execution_log import _execution_accounting,ExecutionLogError
        if (not isinstance(now,datetime) or now.tzinfo is None or now.utcoffset() is None
                or not isinstance(symbol,str) or re.fullmatch(r'[0-9]{6}',symbol) is None):raise ValueError
        now=now.astimezone(KST);today=now.strftime('%Y%m%d')
        rows=db.execute("""SELECT * FROM intents WHERE scope=? AND mode='paper' AND symbol=?
            AND state!='cancelled' AND (? IS NULL OR id!=?) ORDER BY day,created_at,id LIMIT 10001""",
            (tracker.scope,symbol,ignored_intent_id,ignored_intent_id)).fetchall()
        if not rows or len(rows)>10000 or any(r['state']!='accepted' or r['day']>=today for r in rows):raise ValueError
        if ignored_intent_id is not None:
            own=db.execute('SELECT * FROM intents WHERE scope=? AND id=?',(tracker.scope,ignored_intent_id)).fetchone()
            if (own is None or own['mode']!='paper' or own['symbol']!=symbol or own['side']!='buy'
                    or own['state']!='prepared' or own['order_number'] is not None):raise ValueError
        if tracker._pending_order_rows(db,'paper',symbol):raise ValueError
        # A separately verified broker-zero epoch can delimit older missing
        # 00 history. Its full integrated pending + stream + holding record
        # must validate at the original admission time, not against today.
        epoch_index=0;has_epoch=False
        for index in range(len(rows)-1,-1,-1):
            intent=rows[index]
            if intent['side']!='buy':continue
            record=db.execute('SELECT * FROM pre_order_reconciliations WHERE scope=? AND intent_id=?',(tracker.scope,intent['id'])).fetchone()
            if record is None:continue
            raw=record['evidence_json']
            if not isinstance(raw,str) or hashlib.sha256(raw.encode()).hexdigest()!=record['evidence_sha256']:raise ValueError
            candidate=json.loads(raw)
            if candidate.get('schema_version') not in (5,6):continue
            if candidate.get('symbol')!=symbol or candidate.get('holdings_source')!='broker_observation':raise ValueError
            tracker._assert_epoch_proof(db,candidate,_at(intent['created_at']))
            epoch_index=index;has_epoch=True;break
        rows=rows[epoch_index:]
        orders=[dict(r) for r in db.execute("SELECT * FROM orders WHERE scope=? AND mode='paper' AND symbol=? ORDER BY day,registered_at,ord_no LIMIT 10001",(tracker.scope,symbol))]
        if len(orders)>10000:raise ValueError
        earlier_order_keys={(r['day'],r['ord_no']) for r in orders if has_epoch and _at(r['registered_at'])<_at(rows[0]['created_at'])}
        orders=[r for r in orders if (r['day'],r['ord_no']) not in earlier_order_keys]
        if len(orders)!=len(rows):raise ValueError
        by_key={(r['day'],r['ord_no']):r for r in orders}
        if len(by_key)!=len(rows):raise ValueError
        ordered=[]
        for row in rows:
            order=by_key.get((row['day'],row['order_number']))
            if (order is None or order['side']!=row['side'] or order['quantity']!=row['quantity']
                    or order['strategy']!=row['strategy'] or row['side'] not in ('buy','sell')
                    or type(row['quantity']) is not int or row['quantity']<=0
                    or not _at(row['created_at'])<=_at(order['registered_at'])<=now):raise ValueError
            ordered.append(order)
        first=rows[0]
        rec=db.execute('SELECT * FROM pre_order_reconciliations WHERE scope=? AND intent_id=?',(tracker.scope,first['id'])).fetchone()
        if first['side']!='buy' or rec is None:raise ValueError
        raw=rec['evidence_json']
        if not isinstance(raw,str) or hashlib.sha256(raw.encode()).hexdigest()!=rec['evidence_sha256']:raise ValueError
        zero=json.loads(raw)
        if (zero.get('symbol')!=symbol or zero.get('pre_order_held_qty')!=0 or zero.get('pre_order_pur_amt_krw')!=0
                or zero.get('holdings_source')!='broker_observation' or zero.get('schema_version') not in (1,3,5,6)
                or not 0<=(_at(first['created_at'])-_at(zero['observed_at'])).total_seconds()<=20
                or _at(zero['observed_at']).strftime('%Y%m%d')!=first['day']):raise ValueError
        history=[];details={};gaps=[];units=[]
        for day in sorted({r['day'] for r in ordered}):
            if re.fullmatch(r'[0-9]{8}',day or '') is None:raise ValueError
            day_orders=[o for o in ordered if o['day']==day];numbers={o['ord_no'] for o in day_orders}
            fills=[dict(r) for r in db.execute("SELECT * FROM fills WHERE scope=? AND mode='paper' AND symbol=? AND day=? LIMIT 10001",(tracker.scope,symbol,day))]
            if len(fills)>10000 or any(r['ord_no'] not in numbers and (day,r['ord_no']) not in earlier_order_keys for r in fills):raise ValueError
            fills=[r for r in fills if r['ord_no'] in numbers]
            observed=db.execute(f"SELECT id,day,mode,observed_at,source_api,digest,payload_json FROM {evidence_table(db)} WHERE scope=? AND mode='paper' AND day=? AND source_api='00' ORDER BY id LIMIT 20001",(tracker.scope,day)).fetchall()
            if len(observed)>20000:raise ValueError
            parsed=[]
            for row in observed:
                raw=row['payload_json']
                if not isinstance(raw,str) or hashlib.sha256(raw.encode()).hexdigest()!=row['digest']:raise ValueError
                event=json.loads(raw);at=_at(event['observed_at'])
                if ((event.get('day'),event.get('mode'),event.get('source_api'),event.get('observed_at'))!=(day,'paper','00',row['observed_at'])
                        or event.get('source_kind')!='broker_observation' or at>now or at.strftime('%Y%m%d')!=day):raise ValueError
                values=event.get('values',{})
                if event.get('event_kind')=='execution_stream' and values.get('9001') in (symbol,'A'+symbol):
                    number=values.get('9203','').lstrip('0')
                    if number not in numbers:
                        if (day,number) not in earlier_order_keys or at>=_at(zero['observed_at']):raise ValueError
                        continue
                    if at<_at(by_key[(day,number)]['registered_at']):raise ValueError
                    units.append({'id':row['id'],'sha256':row['digest']})
                if event.get('event_kind') in ('stream_gap','stream_stopped'):
                    gaps.append({'id':row['id'],'sha256':row['digest'],'observed_at':at.isoformat()})
                parsed.append(dict(row))
            result=_execution_accounting(day,day_orders,fills,parsed)
            for item in result['orders']:
                original=by_key[(day,item['order_number'])]
                if (item['unit_coverage']!='quantity_reconciled' or item['issues']
                        or item['verified_amount_source']!='00_unit_sum_reconciled_to_cumulative_quantity'
                        or item['verified_quantity']!=original['quantity']
                        or item['rest_projection_quantity']!=original['quantity']):raise ValueError
                # The gap flag is retained. It is not erased or promoted to
                # complete historical coverage or confirmed accounting.
                details[(day,item['order_number'])]=item
            history.extend({'day':o['day'],'order_number':o['ord_no'],'side':o['side'],'quantity':o['quantity']} for o in day_orders)
        quantity=0
        for order in ordered:
            if (order['day'],order['ord_no']) not in details:raise ValueError
            quantity+=order['quantity']*(1 if order['side']=='buy' else -1)
            if quantity<0:raise ValueError
        if quantity!=0:raise ValueError
        balance=db.execute("SELECT * FROM account_balance_daily WHERE scope=? AND mode='paper' AND day=?",(tracker.scope,today)).fetchone()
        if balance is None or balance['source_kind']!='broker_observation':raise ValueError
        account_at=_at(balance['observed_at']);account=json.loads(balance['payload_json'])
        if (not 0<=(now-account_at).total_seconds()<=90 or account_at.strftime('%Y%m%d')!=today
                or account.get('schema')!='paper-account-snapshot-v1' or account.get('mode')!='paper'
                or account.get('exchange')!='KRX' or not _verified_symbol_flat_holdings(account.get('holdings'),symbol)
                or any(type(account.get('pages',{}).get(api)) is not int or not 1<=account['pages'][api]<=20
                       for api in ('kt00001','kt00018'))):raise ValueError
        current=db.execute(f"SELECT id,day,mode,observed_at,source_api,digest,payload_json FROM {evidence_table(db)} WHERE scope=? AND mode='paper' AND day=? AND source_api IN ('00','ka10075') ORDER BY id LIMIT 20001",(tracker.scope,today)).fetchall()
        if len(current)>20000:raise ValueError
        parsed=[];lifecycle=[]
        for row in current:
            raw=row['payload_json']
            if not isinstance(raw,str) or hashlib.sha256(raw.encode()).hexdigest()!=row['digest']:raise ValueError
            event=json.loads(raw);at=_at(event['observed_at'])
            if ((event.get('day'),event.get('mode'),event.get('source_api'),event.get('observed_at'))!=(today,'paper',row['source_api'],row['observed_at'])
                    or event.get('source_kind')!='broker_observation' or at>now or at.strftime('%Y%m%d')!=today):raise ValueError
            parsed.append((row,event))
            if row['source_api']=='00':
                kind=event.get('event_kind');values=event.get('values',{})
                if kind=='execution_stream' and values.get('9001') in (symbol,'A'+symbol):raise ValueError
                if kind in ('stream_started','stream_resumed','stream_gap','stream_stopped'):lifecycle.append((at,row['id'],kind,row['digest']))
        pending=latest_complete_pages(parsed,'ka10075',today,now)
        if pending['started_at']<account_at:raise ValueError
        from kiwoom_autotrader.config.settings import effective_paper_experiment
        from .paper_reconciliation import _assert_no_broker_pending
        profile=effective_paper_experiment(tracker.settings_loader(),'paper')
        symbol_pending=(profile.get('enabled') is True and profile.get('pending_order_scope')=='symbol')
        if symbol_pending:
            # The broker request remains integrated and every page/row must
            # validate. Only the already-approved experiment reservation scope
            # is symbol-local; malformed or target-symbol pending still fails.
            _assert_no_broker_pending(pending['rows'],symbol,integrated=False)
        elif pending['rows']:
            raise ValueError
        before=[r for r in lifecycle if r[0]<=account_at]
        if (not before or max(before)[2] not in ('stream_started','stream_resumed')
                or any(at>=account_at and kind in ('stream_gap','stream_stopped') for at,_,kind,_ in lifecycle)):raise ValueError
        # A delayed historical unit changes the proof even when its event day
        # is yesterday. It cannot be hidden behind a new account snapshot.
        for row in db.execute("SELECT e.* FROM execution_observations e JOIN orders o ON o.scope=e.scope AND o.day=e.day AND o.mode=e.mode AND o.ord_no=e.ord_no WHERE e.scope=? AND e.mode='paper' AND o.symbol=? ORDER BY e.id LIMIT 10001",(tracker.scope,symbol)):
            if _at(row['observed_at'])>=account_at and not tracker._verified_nonprojecting_repeat(db,row,now):raise ValueError
        anchor=max(before)
        return {'schema':'paper-prior-closed-flat-epoch-v1','symbol':symbol,'day':today,
                'flat_scope':'symbol','account_observed_at':account_at.isoformat(),'account_sha256':hashlib.sha256(balance['payload_json'].encode()).hexdigest(),
                'pending_clear_scope':'symbol' if symbol_pending else 'account',
                'pending_query_id':pending['query_id'],'pending_source_ids':pending['source_ids'],'pending_source_hashes':pending['source_hashes'],
                'stream_anchor_id':anchor[1],'stream_anchor_sha256':anchor[3],
                'prior_closed_orders':history,'prior_unit_evidence':units,'preserved_historical_gaps':gaps,
                'historical_cutoff_intent_id':first['id'],'preserved_earlier_order_count':len(earlier_order_keys),
                'historical_profit_attribution':'unchanged_unconfirmed','repairs_prior_ownership':False,
                'symbol_ledger_sha256':tracker._symbol_order_watermark(db,'paper',symbol,ignored_intent_id=ignored_intent_id)}
    except (ValueError,TypeError,KeyError,OverflowError,AttributeError,ExecutionLogError):
        return None
