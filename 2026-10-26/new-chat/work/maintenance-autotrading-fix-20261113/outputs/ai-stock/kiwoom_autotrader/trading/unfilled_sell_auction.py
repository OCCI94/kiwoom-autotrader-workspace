"""One-shot cancellation of proven paper auction-sell remainder.

No ACK is a confirmed cancellation. Existing position and fill rows are never
rewritten. A claim survives rejection, uncertainty and process restart.
"""
from datetime import datetime, timedelta
from decimal import Decimal
import hashlib,json,re
from .unfilled_buy_ttl import TTLLedger,TTLUnavailable,UnfilledBuyTTLMonitor,_moment,_integer,_order_number
from .execution_log import _execution_accounting
from kiwoom_autotrader.api.executions import fetch_executions
from kiwoom_autotrader.api.cancellations import cancel_paper_buy_remainder,CancellationReceipt
from kiwoom_autotrader.api.minute_chart import session_identity

_ENDS={'EXP_D':{(9,1)},'EXP_GDR':{(9,0),(15,30)},'EXP_LDR':{(9,0)},
       'EXP_VSS':{(15,30)},'EXP_ORB_A':{(15,30)},'EXP_ORB_B':{(15,30)}}

class SellAuctionLedger(TTLLedger):
    @staticmethod
    def _schema(db):
        db.execute('''CREATE TABLE IF NOT EXISTS sell_auction_cancellation_attempts (
          scope TEXT NOT NULL,day TEXT NOT NULL,original_ord_no TEXT NOT NULL,intent_id TEXT NOT NULL,
          symbol TEXT NOT NULL,claimed_at TEXT NOT NULL,evidence_json TEXT NOT NULL,evidence_sha256 TEXT NOT NULL,
          PRIMARY KEY(scope,day,original_ord_no))''')
        db.execute('''CREATE TABLE IF NOT EXISTS sell_auction_cancellation_outcomes (
          scope TEXT NOT NULL,day TEXT NOT NULL,original_ord_no TEXT NOT NULL,observed_at TEXT NOT NULL,
          status TEXT NOT NULL,cancellation_ord_no TEXT,reported_cancel_qty INTEGER,
          PRIMARY KEY(scope,day,original_ord_no))''')
        for table in ('sell_auction_cancellation_attempts','sell_auction_cancellation_outcomes'):
            for action in ('UPDATE','DELETE'):
                db.execute(f"CREATE TRIGGER IF NOT EXISTS {table}_no_{action.lower()} BEFORE {action} ON {table} "
                           "BEGIN SELECT RAISE(ABORT, 'sell cancellation evidence is immutable'); END")

    def _snapshot(self,db,day):
        rows=[dict(r) for r in db.execute('''SELECT o.*,i.id AS intent_id,i.created_at,i.state,d.order_price,d.order_type,
              d.evidence_json,d.evidence_sha256,r.evidence_json AS reconciliation_json,r.evidence_sha256 AS reconciliation_sha256,
              c.generation_id AS claimed_generation,c.exit_kind AS claimed_exit_kind FROM orders o JOIN intents i ON i.scope=o.scope AND i.day=o.day
              AND i.mode=o.mode AND i.order_number=o.ord_no AND i.symbol=o.symbol AND i.side=o.side
              AND i.strategy=o.strategy AND i.quantity=o.quantity JOIN order_decisions d ON d.scope=i.scope AND d.intent_id=i.id
              LEFT JOIN pre_order_reconciliations r ON r.scope=i.scope AND r.intent_id=i.id
              LEFT JOIN multi_strategy_exit_claims c ON c.scope=i.scope AND c.intent_id=i.id
              WHERE o.scope=? AND o.day=? AND o.mode='paper' AND o.side='sell' AND i.state='accepted'
              ORDER BY o.registered_at,o.ord_no''',(self.scope,day))]
        fills=[dict(r) for r in db.execute("SELECT * FROM fills WHERE scope=? AND day=? AND mode='paper' ORDER BY ord_no,fill_no",(self.scope,day))]
        obs=[dict(r) for r in db.execute("SELECT * FROM broker_observations WHERE scope=? AND day=? AND mode='paper' AND source_api='00' ORDER BY id LIMIT 20001",(self.scope,day))]
        if len(obs)>20000:raise TTLUnavailable('sell_cancel_observation_bound')
        for record in obs:
            raw=record['payload_json']
            if not isinstance(raw,str) or hashlib.sha256(raw.encode()).hexdigest()!=record['digest']:
                raise TTLUnavailable('sell_cancel_00_hash_mismatch')
            event=json.loads(raw)
            if (event.get('source_api')!='00' or event.get('mode')!='paper' or event.get('day')!=day
                    or event.get('observed_at')!=record['observed_at']):
                raise TTLUnavailable('sell_cancel_00_scope_mismatch')
        exists=db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='sell_auction_cancellation_attempts'").fetchone()
        claims={r[0] for r in db.execute('SELECT original_ord_no FROM sell_auction_cancellation_attempts WHERE scope=? AND day=?',(self.scope,day))} if exists else set()
        raw=json.dumps([rows,fills,obs],sort_keys=True,separators=(',',':'))
        return rows,fills,obs,claims,hashlib.sha256(raw.encode()).hexdigest()

    def due(self,now):
        with self.db() as db:rows,fills,obs,claims,watermark=self._snapshot(db,now.strftime('%Y%m%d'))
        result=[]
        for row in rows:
            if row['ord_no'] in claims or row['strategy'] not in _ENDS:continue
            try:
                raw=row['evidence_json']
                if (not isinstance(raw,str) or len(raw.encode())>65536 or hashlib.sha256(raw.encode()).hexdigest()!=row['evidence_sha256']):continue
                decision=json.loads(raw);cfg=decision['config'];end=_moment(cfg['experiment_order_window_end_at'])
                recraw=row.get('reconciliation_json')
                if (not isinstance(recraw,str) or hashlib.sha256(recraw.encode()).hexdigest()!=row.get('reconciliation_sha256')):continue
                rec=json.loads(recraw)
                registered=_moment(row['registered_at']);prepared=_moment(row['created_at'])
                if (decision.get('strategy')!=row['strategy'] or decision.get('source_kind')!='broker_observation'
                        or decision.get('strategy_version')!='paper-experiment-v1' or cfg.get('multi_exit_kind')!='full'
                        or not isinstance(cfg.get('multi_position_generation_id'),str)
                        or re.fullmatch(r'[0-9a-f]{32}',cfg['multi_position_generation_id']) is None
                        or row.get('claimed_generation')!=cfg['multi_position_generation_id'] or row.get('claimed_exit_kind')!='full'
                        or rec.get('schema_version')!=4 or rec.get('symbol')!=row['symbol']
                        or rec.get('strategy')!=row['strategy'] or rec.get('generation_id')!=cfg['multi_position_generation_id']
                        or rec.get('exit_kind')!='full'
                        or rec.get('holdings_source')!='broker_observation' or rec.get('order_quantity')!=row['quantity']
                        or type(rec.get('pre_order_held_qty')) is not int or rec['pre_order_held_qty']<row['quantity']
                        or _moment(rec['observed_at'])>_moment(row['created_at'])
                        or end.date()!=registered.date() or (end.hour,end.minute) not in _ENDS[row['strategy']]
                        or end.second or end.microsecond or not prepared<=registered<=end<=now
                        or row['order_type'] not in ('market','limit')):continue
                quantity=_integer(row['quantity'],positive=True)
                _order_number(row['ord_no'])
                order_fills=[f for f in fills if f['ord_no']==row['ord_no']]
                if any(f['symbol']!=row['symbol'] or f['side']!='sell' or f['strategy']!=row['strategy'] for f in order_fills):continue
                accounting=_execution_accounting(day=now.strftime('%Y%m%d'),orders=[row],fills=order_fills,observations=obs)['orders'][0]
                total=sum(_integer(f['quantity'],positive=True) for f in order_fills)
                if total>=quantity or accounting['observed_unit_quantity']!=total:continue
                if total and (accounting['unit_coverage']!='quantity_reconciled' or accounting['stream_gap_observed']):continue
                # Even an unfilled order must not span a recorded 00 gap.
                for item in obs:
                    event=json.loads(item['payload_json'])
                    if event.get('event_kind') in ('stream_gap','stream_stopped') and _moment(event['observed_at'])>=prepared:
                        raise ValueError('sell_gap')
                    values=event.get('values',{})
                    if (event.get('event_kind')=='execution_stream' and _moment(event['observed_at'])>=prepared
                            and isinstance(values,dict) and values.get('9001','').removeprefix('A')==row['symbol']
                            and _order_number(values.get('9203'))!=row['ord_no']):
                        raise ValueError('sell_other_order')
                row.update(ledger_filled_qty=total,generation_id=cfg['multi_position_generation_id'],window_end=end.isoformat(),unit_rows=accounting['unit_executions'])
                result.append(row)
            except (ValueError,TypeError,KeyError,OverflowError):continue
        return result,watermark

    def claim(self,row,evidence,watermark):
        raw=json.dumps(evidence,sort_keys=True,separators=(',',':'))
        with self.db(write=True) as db:
            self._schema(db)
            if self._snapshot(db,row['day'])[4]!=watermark:raise TTLUnavailable('sell_cancel_ledger_changed')
            return db.execute('INSERT OR IGNORE INTO sell_auction_cancellation_attempts VALUES (?,?,?,?,?,?,?,?)',
                (self.scope,row['day'],row['ord_no'],row['intent_id'],row['symbol'],evidence['claimed_at'],raw,hashlib.sha256(raw.encode()).hexdigest())).rowcount==1

    def outcome(self,row,at,status,receipt=None):
        with self.db(write=True) as db:
            db.execute('INSERT INTO sell_auction_cancellation_outcomes VALUES (?,?,?,?,?,?,?)',
                (self.scope,row['day'],row['ord_no'],at,status,
                 receipt.cancellation_order_number if receipt else None,receipt.reported_cancel_quantity if receipt else None))

    def unsettled(self,day):
        with self.db() as db:
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='sell_auction_cancellation_attempts'").fetchone() is None:return []
            settled=db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(SELL_SETTLEMENT,)).fetchone()
            condition=(f"AND NOT EXISTS (SELECT 1 FROM {SELL_SETTLEMENT} s WHERE s.scope=a.scope AND s.day=a.day AND s.original_ord_no=a.original_ord_no)" if settled else '')
            return [dict(r) for r in db.execute('SELECT a.original_ord_no FROM sell_auction_cancellation_attempts a WHERE a.scope=? AND a.day=? '+condition+' ORDER BY a.claimed_at LIMIT 100',(self.scope,day))]

class SellAuctionMonitor:
    def __init__(self,*,now,pending_fetcher,cancel_handler,monotonic,execution_fetcher=None):
        self.now=now;self.pending_fetcher=pending_fetcher;self.cancel_handler=cancel_handler
        self.monotonic=monotonic;self.execution_fetcher=execution_fetcher or fetch_executions
        self.last_status=None

    def tick(self,session,tracker,*,enabled_check=None):
        now=_moment(self.now());row=None;claimed=False
        try:
            identity=session_identity(session)
            if identity[0]!='paper':return
            gate=getattr(session,'paper_automation_reconcile',None)
            if getattr(gate,'_tracker',None) is not tracker or getattr(gate,'_session',None) is not session:raise TTLUnavailable('sell_cancel_scope')
            stream=UnfilledBuyTTLMonitor._stream(session);ledger=SellAuctionLedger(tracker)
            pending_settlements=ledger.unsettled(now.strftime('%Y%m%d'));previous=None
            if pending_settlements:
                try:
                    previous=settle_sell_auction(session,tracker,pending_settlements[0],now=self.now,monotonic=self.monotonic)
                except Exception:
                    previous={'status':'settlement_unconfirmed','reason':'sell_cancellation_confirmation_unavailable',
                              'original_order_number':pending_settlements[0]['original_ord_no'],'position_released':False}
                stream=UnfilledBuyTTLMonitor._stream(session)
            due,watermark=ledger.due(now)
            if not due:self.last_status=previous or {'status':'no_due_auction_sell'};return
            row=due[0]
            start=_moment(self.now());mono=self.monotonic()
            executions=self.execution_fetcher(session,row['day'],observer=tracker.record_broker_observation)
            pending=self.pending_fetcher(session,observer=tracker.record_broker_observation,day=row['day'],integrated=True)
            known={(str(int(x.order_number)),str(int(x.execution_number)),str(x.price),x.quantity,x.symbol,x.side)
                   for x in executions if str(int(x.order_number))==row['ord_no']}
            units={(row['ord_no'],str(int(x['execution_id'])),str(Decimal(x['price'])),x['quantity'],row['symbol'],'sell') for x in row['unit_rows']}
            if known!=units:raise TTLUnavailable('sell_cancel_00_rest_mismatch')
            matches=[x for x in pending if _order_number(x.get('ord_no'))==row['ord_no']]
            if len(matches)!=1:raise TTLUnavailable('sell_cancel_pending_unverified')
            item=matches[0];symbol=item.get('stk_cd','').removeprefix('A');remain=_integer(item.get('oso_qty'),positive=True)
            if (symbol!=row['symbol'] or item.get('io_tp_nm','').lstrip('+-')!='매도'
                    or item.get('stex_tp')!='1' or item.get('sor_yn')!='N'
                    or _order_number(item.get('orig_ord_no'),zero=True)!='0'
                    or item.get('ord_stt') not in ('접수','확인','체결')
                    or _integer(item.get('ord_qty'),positive=True)!=row['quantity']
                    or remain!=row['quantity']-row['ledger_filled_qty']
                    or row['order_type']=='limit' and _integer(item.get('ord_pric'),positive=True)!=_integer(row['order_price'],positive=True)):
                raise TTLUnavailable('sell_cancel_pending_mismatch')
            stamp=item.get('tm')
            if not isinstance(stamp,str) or re.fullmatch(r'(?:[01][0-9]|2[0-3])[0-5][0-9][0-5][0-9]',stamp) is None:
                raise TTLUnavailable('sell_cancel_pending_time_unverified')
            from kiwoom_autotrader.api.session import KST
            broker_at=datetime.strptime(row['day']+stamp,'%Y%m%d%H%M%S').replace(tzinfo=KST)
            if not _moment(row['created_at'])-timedelta(seconds=1)<=broker_at<=_moment(row['registered_at'])+timedelta(seconds=1):
                raise TTLUnavailable('sell_cancel_pending_time_unverified')
            def guard():
                current=_moment(self.now())
                if (session_identity(session)!=identity or UnfilledBuyTTLMonitor._stream(session)!=stream
                        or getattr(session,'paper_automation_reconcile',None) is not gate
                        or not 0<=(current-start).total_seconds()<=15 or not 0<=self.monotonic()-mono<=15
                        or current.date()!=now.date() or enabled_check is not None and enabled_check() is not True):raise TTLUnavailable('sell_cancel_stale')
                with ledger.db() as db:
                    if ledger._snapshot(db,row['day'])[4]!=watermark:raise TTLUnavailable('sell_cancel_ledger_changed')
            guard()
            evidence={'schema':'paper-auction-sell-cancel-attempt-v1','symbol':row['symbol'],
                'original_order_number':row['ord_no'],'generation_id':row['generation_id'],'window_end_at':row['window_end'],
                'claimed_at':_moment(self.now()).isoformat(),'broker_remaining_qty':remain,'ledger_filled_qty':row['ledger_filled_qty'],
                'ledger_watermark':watermark,'ack_releases_position':False}
            claimed=ledger.claim(row,evidence,watermark)
            if not claimed:return
            receipt=self.cancel_handler(session,row['symbol'],row['ord_no'],before_send=guard)
            if not isinstance(receipt,CancellationReceipt) or receipt.original_order_number!=row['ord_no'] or not 0<=receipt.reported_cancel_quantity<=remain:raise TTLUnavailable('sell_cancel_ack_unverified')
            ledger.outcome(row,_moment(self.now()).isoformat(),'cancel_input_ack_unconfirmed_settlement',receipt)
            self.last_status={'status':'cancel_input_ack_unconfirmed_settlement','symbol':row['symbol']}
        except Exception as error:
            if claimed:
                try:ledger.outcome(row,_moment(self.now()).isoformat(),'cancel_unconfirmed')
                except Exception:pass
            self.last_status={'status':'retry_pending','reason':str(error) if isinstance(error,TTLUnavailable) else 'sell_cancel_unavailable',
                              'symbol':row['symbol'] if row else None,'position_released':False}

SELL_SETTLEMENT='sell_auction_cancellation_settlements'


def _settlement_schema(db):
    db.execute(f'''CREATE TABLE IF NOT EXISTS {SELL_SETTLEMENT} (
      scope TEXT NOT NULL,day TEXT NOT NULL,mode TEXT NOT NULL,original_ord_no TEXT NOT NULL,
      symbol TEXT NOT NULL,confirmed_at TEXT NOT NULL,filled_qty INTEGER NOT NULL,cancelled_qty INTEGER NOT NULL,
      cancellation_ord_no TEXT NOT NULL,financial_fingerprint TEXT NOT NULL,evidence_json TEXT NOT NULL,
      evidence_sha256 TEXT NOT NULL,PRIMARY KEY(scope,day,mode,original_ord_no))''')
    for action in ('UPDATE','DELETE'):
        db.execute(f"CREATE TRIGGER IF NOT EXISTS {SELL_SETTLEMENT}_no_{action.lower()} BEFORE {action} ON {SELL_SETTLEMENT} "
                   "BEGIN SELECT RAISE(ABORT, 'sell cancellation settlement is immutable'); END")


def confirmed_sell_cancelled_quantity(db,scope,order):
    """Read a final sell cancellation; any later financial revision blocks it."""
    from .cancellation_settlement import financial_fingerprint
    if order['side']!='sell' or order['mode']!='paper':return 0
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(SELL_SETTLEMENT,)).fetchone() is None:return 0
    row=db.execute(f'SELECT * FROM {SELL_SETTLEMENT} WHERE scope=? AND day=? AND mode=? AND original_ord_no=?',
                   (scope,order['day'],'paper',order['ord_no'])).fetchone()
    if row is None:return 0
    raw=row['evidence_json']
    if not isinstance(raw,str) or hashlib.sha256(raw.encode()).hexdigest()!=row['evidence_sha256']:raise ValueError('sell_settlement_hash')
    proof=json.loads(raw)
    claim=db.execute('SELECT * FROM sell_auction_cancellation_attempts WHERE scope=? AND day=? AND original_ord_no=?',
                     (scope,order['day'],order['ord_no'])).fetchone()
    outcome=db.execute('SELECT * FROM sell_auction_cancellation_outcomes WHERE scope=? AND day=? AND original_ord_no=?',
                       (scope,order['day'],order['ord_no'])).fetchone()
    if (claim is None or claim['symbol']!=order['symbol']
            or hashlib.sha256(claim['evidence_json'].encode()).hexdigest()!=claim['evidence_sha256']):
        raise ValueError('sell_settlement_claim_invalid')
    if proof.get('status')=='fill_resolved':
        if (proof.get('schema')!='paper-auction-sell-cancellation-final-v1'
                or row['symbol']!=order['symbol'] or row['filled_qty']!=order['quantity'] or row['cancelled_qty']!=0 or row['cancellation_ord_no']!='0'
                or proof.get('symbol')!=order['symbol'] or proof.get('original_order_number')!=order['ord_no']
                or proof.get('filled_quantity')!=order['quantity'] or proof.get('cancelled_quantity')!=0
                or financial_fingerprint(db,scope,order,[])!=row['financial_fingerprint']):
            raise ValueError('sell_fill_resolution_invalid')
        return 0
    if (outcome is None or outcome['status']!='cancel_input_ack_unconfirmed_settlement'
            or outcome['cancellation_ord_no']!=row['cancellation_ord_no']):raise ValueError('sell_settlement_claim_invalid')
    terminal=proof.get('terminal_00')
    if not isinstance(terminal,list) or not terminal or len(terminal)>100:raise ValueError('sell_settlement_terminal_invalid')
    for item in terminal:
        if not isinstance(item,dict) or type(item.get('id')) is not int:raise ValueError('sell_settlement_terminal_invalid')
        source=db.execute("SELECT digest,payload_json FROM broker_observations WHERE id=? AND scope=? AND day=? AND mode='paper' AND source_api='00'",
                          (item['id'],scope,order['day'])).fetchone()
        if (source is None or source['digest']!=item.get('digest')
                or hashlib.sha256(source['payload_json'].encode()).hexdigest()!=source['digest']):raise ValueError('sell_settlement_terminal_invalid')
        event=json.loads(source['payload_json']);values=event.get('values',{})
        if (event.get('event_kind')!='execution_stream' or values.get('9203','').lstrip('0')!=row['cancellation_ord_no']
                or values.get('904','').lstrip('0')!=order['ord_no'] or values.get('905','').lstrip('+-')!='매도취소'
                or values.get('907')!='1' or values.get('913') not in ('확인','취소')):
            raise ValueError('sell_settlement_terminal_invalid')
    if (proof.get('schema')!='paper-auction-sell-cancellation-final-v1' or proof.get('symbol')!=order['symbol']
            or proof.get('original_order_number')!=order['ord_no'] or row['symbol']!=order['symbol']
            or proof.get('filled_quantity')!=row['filled_qty'] or proof.get('cancelled_quantity')!=row['cancelled_qty']
            or proof.get('cancellation_order_number')!=row['cancellation_ord_no']
            or type(row['filled_qty']) is not int or type(row['cancelled_qty']) is not int
            or row['filled_qty']<0 or row['cancelled_qty']<=0
            or row['filled_qty']+row['cancelled_qty']!=order['quantity']
            or financial_fingerprint(db,scope,order,[row['cancellation_ord_no']])!=row['financial_fingerprint']):
        raise ValueError('sell_settlement_stale_or_invalid')
    return row['cancelled_qty']


def confirmed_sell_cancel_order_numbers(db,scope,day,mode,symbol):
    if mode!='paper' or db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(SELL_SETTLEMENT,)).fetchone() is None:return set()
    result=set()
    for row in db.execute(f'SELECT original_ord_no,cancellation_ord_no FROM {SELL_SETTLEMENT} WHERE scope=? AND day=? AND mode=? AND symbol=?',
                          (scope,day,mode,symbol)):
        order=db.execute('SELECT * FROM orders WHERE scope=? AND day=? AND mode=? AND ord_no=?',(scope,day,mode,row['original_ord_no'])).fetchone()
        if order is None:raise ValueError('sell_settlement_original_missing')
        if confirmed_sell_cancelled_quantity(db,scope,order):result.add(row['cancellation_ord_no'])
    return result


def settle_sell_auction(session,tracker,claim,*,now,monotonic,pending_fetcher=None,order_fetcher=None):
    """Verify 00 + complete kt00009 + ka10075; never resend a cancel."""
    from .cancellation_settlement import (_complete_pages,_source_events,_fetch_pending,_fetch_orders,
                                          financial_fingerprint)
    from kiwoom_autotrader.api.session import KST
    started=_moment(now());mono=monotonic();identity=session_identity(session)
    stream=UnfilledBuyTTLMonitor._stream(session);gate=getattr(session,'paper_automation_reconcile',None)
    if identity[0]!='paper' or getattr(gate,'_tracker',None) is not tracker or getattr(gate,'_session',None) is not session:
        raise TTLUnavailable('sell_settlement_scope')
    ledger=SellAuctionLedger(tracker);day=started.strftime('%Y%m%d');number=_order_number(claim['original_ord_no'])
    with ledger.db() as db:
        saved=db.execute('SELECT * FROM sell_auction_cancellation_attempts WHERE scope=? AND day=? AND original_ord_no=?',(tracker.scope,day,number)).fetchone()
        outcome=db.execute('SELECT * FROM sell_auction_cancellation_outcomes WHERE scope=? AND day=? AND original_ord_no=?',(tracker.scope,day,number)).fetchone()
        order=db.execute("SELECT * FROM orders WHERE scope=? AND day=? AND mode='paper' AND ord_no=? AND side='sell'",(tracker.scope,day,number)).fetchone()
        if saved is None or order is None or _moment(saved['claimed_at'])>started:raise TTLUnavailable('sell_settlement_claim_missing')
        if outcome is not None and not _moment(saved['claimed_at'])<=_moment(outcome['observed_at'])<=started:raise TTLUnavailable('sell_settlement_ack_time')
        if hashlib.sha256(saved['evidence_json'].encode()).hexdigest()!=saved['evidence_sha256']:raise TTLUnavailable('sell_settlement_claim_hash')
        if saved['symbol']!=order['symbol']:raise TTLUnavailable('sell_settlement_scope')
        child=(_order_number(outcome['cancellation_ord_no']) if outcome is not None
               and outcome['status']=='cancel_input_ack_unconfirmed_settlement' else None);order=dict(order)
        if confirmed_sell_cancelled_quantity(db,tracker.scope,order):return {'status':'cancellation_confirmed','original_order_number':number}
    pending_events=[];order_events=[]
    def observer(target):
        def add(event):tracker.record_broker_observation(event);target.append(event)
        return add
    def guard():
        current=_moment(now());elapsed=monotonic()-mono
        if (session_identity(session)!=identity or UnfilledBuyTTLMonitor._stream(session)!=stream
                or getattr(session,'paper_automation_reconcile',None) is not gate
                or not 0<=(current-started).total_seconds()<=20 or type(elapsed) not in (int,float)
                or not 0<=elapsed<=20 or current.date()!=started.date()):raise TTLUnavailable('sell_settlement_stale')
    (pending_fetcher or _fetch_pending)(session,observer=observer(pending_events),day=day);guard()
    (order_fetcher or _fetch_orders)(session,observer=observer(order_events),day=day);guard()
    as_of=_moment(now())
    pending=_complete_pages(pending_events,'ka10075',{'all_stk_tp':'0','trde_tp':'0','stex_tp':'0'},as_of,started)
    orders=_complete_pages(order_events,'kt00009',{'ord_dt':day,'stk_bond_tp':'1','mrkt_tp':'0','sell_tp':'0','qry_tp':'0','stk_cd':'','fr_ord_no':'','dmst_stex_tp':'KRX'},as_of,started)
    def number_of(value):return _order_number(value,zero=True)
    for item in pending:
        if number_of(item.get('ord_no')) in (number,child) or number_of(item.get('orig_ord_no')) in (number,child):
            if _integer(item.get('oso_qty'))!=0:raise TTLUnavailable('sell_settlement_pending')
    originals=[x for x in orders if number_of(x.get('ord_no'))==number]
    children=[x for x in orders if number_of(x.get('ord_no'))==child]
    if len(originals)!=1:raise TTLUnavailable('sell_settlement_rest_chain')
    # A full fill racing cancellation needs no cancellation ACK or fabricated
    # child. 00 and the complete broker execution query must agree exactly.
    with ledger.db() as db:
        fills=[dict(r) for r in db.execute("SELECT * FROM fills WHERE scope=? AND day=? AND mode='paper' AND ord_no=?",(tracker.scope,day,number))]
        filled=sum(_integer(f['quantity'],positive=True) for f in fills)
        if filled==order['quantity']:
            events=_source_events(db,tracker.scope,day)
            observations=[{'id':i,'digest':d,'payload_json':json.dumps(e,ensure_ascii=False,sort_keys=True,separators=(',',':'))} for i,d,e in events if e.get('source_api')=='00']
            accounting=_execution_accounting(day,[order],fills,observations)['orders'][0]
            o=originals[0]
            if (o.get('stk_cd','').removeprefix('A')!=order['symbol'] or o.get('io_tp_nm')!='현금매도'
                    or o.get('stk_bond_tp')!='1' or o.get('dmst_stex_tp')!='KRX'
                    or number_of(o.get('orig_ord_no'))!='0' or _integer(o.get('ord_qty'))!=filled
                    or _integer(o.get('cntr_qty'))!=filled or accounting['unit_coverage']!='quantity_reconciled'
                    or accounting['stream_gap_observed'] or accounting['observed_unit_quantity']!=filled
                    or abs(Decimal(accounting['observed_unit_amount'])-Decimal(_integer(o.get('cntr_uv'),positive=True)*filled))>filled
                    or any(number_of(x.get('orig_ord_no'))==number for x in orders)
                    or any(e.get('source_api')=='00' and e.get('event_kind') in ('stream_gap','stream_stopped')
                           and _moment(e['observed_at'])>=_moment(order['registered_at']) for _,_,e in events)):
                raise TTLUnavailable('sell_fill_resolution_unverified')
            fingerprint=financial_fingerprint(db,tracker.scope,order,[])
        else:fingerprint=None
    if fingerprint is not None:
        proof={'schema':'paper-auction-sell-cancellation-final-v1','status':'fill_resolved','symbol':order['symbol'],
               'original_order_number':number,'filled_quantity':filled,'cancelled_quantity':0,'confirmed_at':as_of.isoformat(),
               'position_quantity_adjustment':0,'original_fills_modified':False}
        raw=json.dumps(proof,sort_keys=True,separators=(',',':'));guard()
        with ledger.db(write=True) as db:
            guard()
            if financial_fingerprint(db,tracker.scope,order,[])!=fingerprint:raise TTLUnavailable('sell_settlement_changed')
            _settlement_schema(db)
            db.execute(f'INSERT OR IGNORE INTO {SELL_SETTLEMENT} VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                (tracker.scope,day,'paper',number,order['symbol'],as_of.isoformat(),filled,0,'0',fingerprint,raw,hashlib.sha256(raw.encode()).hexdigest()))
        return {'status':'fill_resolved','original_order_number':number,'filled_quantity':filled,'cancelled_quantity':0}
    if child is None or len(children)!=1:raise TTLUnavailable('sell_settlement_ack_missing')
    for item in orders:
        if number_of(item.get('orig_ord_no')) in (number,child) and number_of(item.get('ord_no'))!=child:
            raise TTLUnavailable('sell_settlement_foreign_child')
    original,rest_child=originals[0],children[0]
    def symbol_of(x):return x.get('stk_cd','').removeprefix('A')
    if (symbol_of(original)!=order['symbol'] or original.get('io_tp_nm')!='현금매도'
            or original.get('stk_bond_tp')!='1' or original.get('dmst_stex_tp')!='KRX'
            or number_of(original.get('orig_ord_no'))!='0' or _integer(original.get('ord_qty'),positive=True)!=order['quantity']
            or original.get('mdfy_cncl_tp') not in (None,'')
            or symbol_of(rest_child)!=order['symbol'] or rest_child.get('io_tp_nm')!='현금매도'
            or rest_child.get('stk_bond_tp')!='1' or rest_child.get('dmst_stex_tp')!='KRX'
            or number_of(rest_child.get('orig_ord_no'))!=number or rest_child.get('mdfy_cncl_tp')!='취소주문'):
        raise TTLUnavailable('sell_settlement_rest_identity')
    with ledger.db() as db:
        events=_source_events(db,tracker.scope,day)
        fills=[dict(r) for r in db.execute("SELECT * FROM fills WHERE scope=? AND day=? AND mode='paper' AND ord_no=?",(tracker.scope,day,number))]
        observations=[{'id':i,'digest':d,'payload_json':json.dumps(e,ensure_ascii=False,sort_keys=True,separators=(',',':'))} for i,d,e in events if e.get('source_api')=='00']
        accounting=_execution_accounting(day,[order],fills,observations)['orders'][0]
        qty=sum(_integer(f['quantity'],positive=True) for f in fills);cancelled=order['quantity']-qty
        if (cancelled<=0 or accounting['observed_unit_quantity']!=qty or qty and
                (accounting['unit_coverage']!='quantity_reconciled' or accounting['stream_gap_observed'])
                or _integer(original.get('cntr_qty'))!=qty or _integer(rest_child.get('cnfm_qty'))!=cancelled
                or _integer(rest_child.get('ord_qty'))!=cancelled or _integer(rest_child.get('cntr_qty'))!=0):
            raise TTLUnavailable('sell_settlement_quantity_mismatch')
        if qty:
            price=_integer(original.get('cntr_uv'),positive=True)
            amount=Decimal(accounting['observed_unit_amount'])
            if abs(amount-Decimal(price*qty))>qty:raise TTLUnavailable('sell_settlement_rest_amount_mismatch')
        terminal=[]
        for ident,digest,event in events:
            if (event.get('source_api')=='00' and event.get('event_kind') in ('stream_gap','stream_stopped')
                    and _moment(event['observed_at'])>=_moment(order['registered_at'])):
                raise TTLUnavailable('sell_settlement_stream_gap')
            if event.get('source_api')!='00' or event.get('event_kind')!='execution_stream':continue
            v=event.get('values',{})
            if v.get('904','').lstrip('0') in (number,child) and v.get('9203','').lstrip('0')!=child:
                raise TTLUnavailable('sell_settlement_foreign_child')
            if v.get('9203','').lstrip('0')!=child:continue
            if (v.get('9001','').removeprefix('A')!=order['symbol'] or v.get('904','').lstrip('0')!=number
                    or v.get('905','').lstrip('+-')!='매도취소' or v.get('907')!='1'
                    or v.get('2134')!='1' or v.get('2136')!='N' or v.get('922')!='00'
                    or v.get('913') not in ('확인','취소') or _integer(v.get('900'),positive=True)!=cancelled
                    or _integer(v.get('902'))!=0 or v.get('909') not in (None,'','0','0000000')
                    or any(v.get(k) not in (None,'','0','0000000') for k in ('903','910','911','914','915','919'))
                    or not _moment(saved['claimed_at'])<=_moment(event['observed_at'])<=as_of):
                raise TTLUnavailable('sell_settlement_00_identity')
            stamp=v.get('908')
            if not isinstance(stamp,str) or re.fullmatch(r'(?:[01][0-9]|2[0-3])[0-5][0-9][0-5][0-9]',stamp) is None:
                raise TTLUnavailable('sell_settlement_00_time')
            broker_at=datetime.strptime(day+stamp,'%Y%m%d%H%M%S').replace(tzinfo=KST)
            if not _moment(saved['claimed_at'])-timedelta(seconds=1)<=broker_at<=_moment(event['observed_at'])+timedelta(seconds=1):
                raise TTLUnavailable('sell_settlement_00_time')
            terminal.append({'id':ident,'digest':digest})
        if not terminal:raise TTLUnavailable('sell_settlement_00_missing')
        fingerprint=financial_fingerprint(db,tracker.scope,order,[child])
    proof={'schema':'paper-auction-sell-cancellation-final-v1','symbol':order['symbol'],
        'original_order_number':number,'cancellation_order_number':child,'filled_quantity':qty,
        'cancelled_quantity':cancelled,'confirmed_at':as_of.isoformat(),'terminal_00':terminal,
        'pending_query_id':pending_events[0]['query_id'],'orders_query_id':order_events[0]['query_id'],
        'position_quantity_adjustment':0,'original_fills_modified':False}
    raw=json.dumps(proof,sort_keys=True,separators=(',',':'))
    guard()
    with ledger.db(write=True) as db:
        guard()
        if financial_fingerprint(db,tracker.scope,order,[child])!=fingerprint:raise TTLUnavailable('sell_settlement_changed')
        _settlement_schema(db)
        db.execute(f'INSERT OR IGNORE INTO {SELL_SETTLEMENT} VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
            (tracker.scope,day,'paper',number,order['symbol'],as_of.isoformat(),qty,cancelled,child,fingerprint,raw,hashlib.sha256(raw.encode()).hexdigest()))
    return {'status':'cancellation_confirmed','original_order_number':number,'filled_quantity':qty,'cancelled_quantity':cancelled}
