"""Serial, opt-in owned paper strategy limit-buy expiry. No independent login.

Only an exact accepted automatic order owned by this tracker/account scope may
be cancelled. A permanent claim precedes dispatch. Only a proven NOT_SENT
outcome permits a fresh, linked attempt; unknown dispatch and ACK never resend.
The original attempts/outcomes and orders/intents/fills and
cash remain untouched. Only a separate published final proof closes the
unfilled reservation; broker input ACK is not settlement.
"""
from contextlib import contextmanager
from datetime import datetime, timedelta
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
import time

from kiwoom_autotrader.api.cancellations import (cancel_paper_buy_remainder, CancellationError,
                                               CancellationReceipt)
from kiwoom_autotrader.api.minute_chart import session_identity
from kiwoom_autotrader.api.session import KST
from kiwoom_autotrader.config.settings import PAPER_EXPERIMENT_STRATEGIES
from kiwoom_autotrader.trading.paper_reconciliation import _pending_rows

# B3 initial/add-on orders retain the gdcrs strategy ID. Scheduled commands
# receive rsv#<positive scheduler ID> through automated_orders, never merely
# because their broker order happens to resemble a scheduled order.
TTL_STRATEGIES = ('gdcrs', 'S2_surge_high_breakout',
                  'S3_upper_limit_continuation', 'S4_theme_and_catalyst_leader','S5_steady_uptrend_vwap',
                  *PAPER_EXPERIMENT_STRATEGIES)


def is_ttl_strategy(value):
    """Exact automatic attribution; manual/unknown/prefix lookalikes fail."""
    return isinstance(value, str) and (value in TTL_STRATEGIES
        or re.fullmatch(r'rsv#[1-9][0-9]*', value) is not None)


def ttl_strategy_sql(column):
    """SQL equivalent used by both initial ownership and final settlement.

    This is only a strategy predicate: callers must also join the matching
    accepted intent and limit-order decision in the same paper account scope.
    """
    if column not in ('o.strategy', 'strategy'):
        raise ValueError('unsupported TTL strategy column')
    placeholders = ','.join('?' for _ in TTL_STRATEGIES)
    return (f"({column} IN ({placeholders}) OR (substr({column},1,4)='rsv#' "
        f"AND substr({column},5,1) GLOB '[1-9]' "
        f"AND substr({column},5) NOT GLOB '*[^0-9]*' "
        f"AND instr({column},char(0))=0))", TTL_STRATEGIES)


class TTLUnavailable(RuntimeError):
    pass


_AUCTION_ENDS = {'EXP_C': {(9, 1)}, 'EXP_GDR': {(9, 0),(9, 1)}, 'EXP_D': {(15, 30)},
                 'EXP_VSS': {(15, 30)}, 'EXP_LDR': {(15, 30)}}
_WINDOW_EXPIRY_STRATEGIES = frozenset(('EXP_D','EXP_VSS','EXP_LDR'))


def _auction_window_end(row, registered):
    """Read the original hash-bound arm window, never an inferred deadline."""
    expected = _AUCTION_ENDS.get(row.get('strategy'))
    raw, digest = row.get('evidence_json'), row.get('evidence_sha256')
    if (expected is None or not isinstance(raw, str) or len(raw.encode()) > 65536
            or not isinstance(digest, str) or hashlib.sha256(raw.encode()).hexdigest() != digest):
        return None
    try:
        config = json.loads(raw)['config']
        end = _moment(config['experiment_order_window_end_at'])
        if (end.date() != registered.date() or (end.hour, end.minute) not in expected
                or end.second or end.microsecond or registered > end
                or row['strategy']=='EXP_GDR' and (end.hour,end.minute)==(9,1)
                   and (registered.hour,registered.minute)!=(9,0)):
            return None
        return end
    except (ValueError, TypeError, KeyError, TTLUnavailable):
        return None


def _moment(value):
    value = datetime.fromisoformat(value) if isinstance(value, str) else value
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise TTLUnavailable("ttl_clock_unverified")
    return value.astimezone(KST)


def _integer(value, *, positive=False):
    if type(value) is int:
        result = value
    elif isinstance(value, str) and re.fullmatch(r"\+?[0-9]{1,12}", value):
        result = int(value)
    else:
        raise TTLUnavailable("ttl_pending_schema_unverified")
    if result < (1 if positive else 0) or result > 999999999999:
        raise TTLUnavailable("ttl_pending_schema_unverified")
    return result


def _order_number(value, *, zero=False):
    if not isinstance(value, str) or re.fullmatch(r"[0-9]{1,7}", value) is None:
        raise TTLUnavailable("ttl_pending_schema_unverified")
    number = int(value)
    if not zero and number <= 0:
        raise TTLUnavailable("ttl_pending_schema_unverified")
    return str(number)


class TTLLedger:
    """Lazy extension of the existing ledger, created only with positive TTL."""
    def __init__(self, tracker):
        self.path = Path(tracker.path)
        self.scope = tracker.scope
        if not isinstance(self.scope, str) or not self.scope:
            raise TTLUnavailable("ttl_owner_unverified")

    @contextmanager
    def db(self, *, write=False):
        if self.path.is_symlink() or not self.path.is_file():
            raise TTLUnavailable("ttl_ledger_unavailable")
        db = sqlite3.connect(self.path.resolve().as_uri() + ("?mode=rw" if write else "?mode=ro"),
                             uri=True, timeout=2)
        db.row_factory = sqlite3.Row
        from .broker_observation_repeats import install_evidence_view
        install_evidence_view(db)
        try:
            if write:
                db.execute("PRAGMA synchronous=FULL")
            else:
                db.execute("PRAGMA query_only=ON")
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield db
            if write:
                db.commit()
        finally:
            db.close()

    @staticmethod
    def _schema(db):
        # No updates, claim deletion, normal order state changes or sidecar
        # rematerialization: cancellation input receipts have a separate type.
        db.execute("""CREATE TABLE IF NOT EXISTS buy_ttl_cancellation_attempts (
            scope TEXT NOT NULL, day TEXT NOT NULL, original_ord_no TEXT NOT NULL,
            intent_id TEXT NOT NULL, symbol TEXT NOT NULL, claimed_at TEXT NOT NULL,
            evidence_json TEXT NOT NULL, evidence_sha256 TEXT NOT NULL,
            PRIMARY KEY(scope,day,original_ord_no))""")
        db.execute("""CREATE TABLE IF NOT EXISTS buy_ttl_cancellation_outcomes (
            scope TEXT NOT NULL, day TEXT NOT NULL, original_ord_no TEXT NOT NULL,
            observed_at TEXT NOT NULL, status TEXT NOT NULL, cancellation_ord_no TEXT,
            reported_cancel_quantity INTEGER,
            PRIMARY KEY(scope,day,original_ord_no))""")
        db.execute("""CREATE TABLE IF NOT EXISTS buy_ttl_cancellation_retry_attempts (
            scope TEXT NOT NULL, day TEXT NOT NULL, original_ord_no TEXT NOT NULL,
            attempt_sequence INTEGER NOT NULL CHECK(attempt_sequence>0),
            intent_id TEXT NOT NULL, symbol TEXT NOT NULL, claimed_at TEXT NOT NULL,
            evidence_json TEXT NOT NULL, evidence_sha256 TEXT NOT NULL,
            previous_attempt_sha256 TEXT NOT NULL, previous_outcome_sha256 TEXT NOT NULL,
            PRIMARY KEY(scope,day,original_ord_no,attempt_sequence))""")
        db.execute("""CREATE TABLE IF NOT EXISTS buy_ttl_cancellation_retry_outcomes (
            scope TEXT NOT NULL, day TEXT NOT NULL, original_ord_no TEXT NOT NULL,
            attempt_sequence INTEGER NOT NULL CHECK(attempt_sequence>0),
            observed_at TEXT NOT NULL, status TEXT NOT NULL, cancellation_ord_no TEXT,
            reported_cancel_quantity INTEGER,
            PRIMARY KEY(scope,day,original_ord_no,attempt_sequence))""")
        for table in ("buy_ttl_cancellation_attempts", "buy_ttl_cancellation_outcomes",
                      "buy_ttl_cancellation_retry_attempts", "buy_ttl_cancellation_retry_outcomes"):
            for action in ("UPDATE", "DELETE"):
                db.execute(f"CREATE TRIGGER IF NOT EXISTS {table}_no_{action.lower()} BEFORE {action} ON {table} "
                           "BEGIN SELECT RAISE(ABORT, 'TTL cancellation evidence is immutable'); END")

    @staticmethod
    def _has_claims(db):
        return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='buy_ttl_cancellation_attempts'").fetchone() is not None

    @staticmethod
    def _outcome_digest(outcome):
        return hashlib.sha256(json.dumps(dict(outcome),sort_keys=True,separators=(',',':')).encode()).hexdigest()

    @staticmethod
    def _retryable(saved, outcome):
        """Exact transport-proven NOT_SENT only; rejected/unknown ACKs stay held."""
        if outcome is None or outcome['status']!='cancel_not_sent':return False
        try:
            evidence=json.loads(saved['evidence_json'])
            return (outcome['cancellation_ord_no'] is None and outcome['reported_cancel_quantity'] is None
                and _moment(outcome['observed_at'])>=_moment(saved['claimed_at'])
                and evidence.get('schema')=='gdcrs-unfilled-buy-ttl-v1'
                and evidence.get('mode')=='paper' and evidence.get('original_order_number')==saved['original_ord_no']
                and evidence.get('symbol')==saved['symbol'] and evidence.get('claimed_at')==saved['claimed_at']
                and evidence.get('cancel_request_qty')==0 and evidence.get('ack_releases_principal') is False
                and is_ttl_strategy(evidence.get('strategy')))
        except (ValueError,TypeError,KeyError,TTLUnavailable):return False

    def latest_attempt(self, db, day, number):
        """Validate an append-only linear claim chain; gaps never authorize send."""
        saved=db.execute('SELECT * FROM buy_ttl_cancellation_attempts WHERE scope=? AND day=? AND original_ord_no=?',
            (self.scope,day,number)).fetchone()
        if saved is None:return None,None
        saved=dict(saved);saved['attempt_sequence']=0
        outcome=db.execute('SELECT * FROM buy_ttl_cancellation_outcomes WHERE scope=? AND day=? AND original_ord_no=?',
            (self.scope,day,number)).fetchone()
        outcome=dict(outcome) if outcome is not None else None
        if hashlib.sha256(saved['evidence_json'].encode()).hexdigest()!=saved['evidence_sha256']:
            raise TTLUnavailable('ttl_claim_hash_mismatch')
        exists=db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='buy_ttl_cancellation_retry_attempts'").fetchone()
        if not exists:return saved,outcome
        retries=db.execute('SELECT * FROM buy_ttl_cancellation_retry_attempts WHERE scope=? AND day=? AND original_ord_no=? ORDER BY attempt_sequence LIMIT 10001',
            (self.scope,day,number)).fetchall()
        if len(retries)>10000:raise TTLUnavailable('ttl_retry_history_limit')
        for row in retries:
            row=dict(row)
            if (row['attempt_sequence']!=saved['attempt_sequence']+1 or not self._retryable(saved,outcome)
                    or row['previous_attempt_sha256']!=saved['evidence_sha256']
                    or row['previous_outcome_sha256']!=self._outcome_digest(outcome)
                    or row['intent_id']!=saved['intent_id'] or row['symbol']!=saved['symbol']
                    or not _moment(outcome['observed_at'])<_moment(row['claimed_at'])
                    or hashlib.sha256(row['evidence_json'].encode()).hexdigest()!=row['evidence_sha256']):
                raise TTLUnavailable('ttl_retry_chain_unverified')
            saved=row
            result=db.execute('SELECT * FROM buy_ttl_cancellation_retry_outcomes WHERE scope=? AND day=? AND original_ord_no=? AND attempt_sequence=?',
                (self.scope,day,number,row['attempt_sequence'])).fetchone()
            outcome=dict(result) if result is not None else None
        return saved,outcome

    def _snapshot(self, db, day):
        strategy_predicate, strategy_values = ttl_strategy_sql('o.strategy')
        rows = db.execute(f"""SELECT o.*, i.id AS intent_id, i.created_at, i.state,
            d.order_price,d.order_type,d.evidence_json,d.evidence_sha256 FROM orders o JOIN intents i ON
            i.scope=o.scope AND i.day=o.day AND i.mode=o.mode AND i.order_number=o.ord_no
            AND i.symbol=o.symbol AND i.side=o.side AND i.strategy=o.strategy AND i.quantity=o.quantity
            JOIN order_decisions d ON d.scope=i.scope AND d.intent_id=i.id
            WHERE o.scope=? AND o.day=? AND o.mode='paper' AND o.side='buy'
            AND {strategy_predicate} AND i.state='accepted' AND d.order_type='limit'
            ORDER BY o.registered_at,o.ord_no""", (self.scope, day, *strategy_values)).fetchall()
        claims = {}
        if self._has_claims(db):
            claims = {r[0]:self.latest_attempt(db,day,r[0]) for r in db.execute("SELECT original_ord_no FROM buy_ttl_cancellation_attempts WHERE scope=? AND day=?", (self.scope,day))}
        fills = [tuple(r) for r in db.execute("SELECT ord_no,fill_no,quantity,price,symbol,side,strategy FROM fills WHERE scope=? AND day=? AND mode='paper' ORDER BY ord_no,fill_no", (self.scope,day))]
        execution = tuple(db.execute("SELECT COUNT(*),COALESCE(MAX(id),0) FROM execution_observations WHERE scope=? AND day=? AND mode='paper'", (self.scope,day)).fetchone())
        stream = tuple(db.execute("SELECT COUNT(*),COALESCE(MAX(id),0) FROM broker_observations WHERE scope=? AND day=? AND mode='paper' AND source_api='00'", (self.scope,day)).fetchone())
        objects = [dict(r) for r in rows]
        if any(not is_ttl_strategy(row['strategy']) for row in objects):
            raise TTLUnavailable('ttl_order_ownership_unverified')
        if len({r['ord_no'] for r in objects}) != len(objects):
            raise TTLUnavailable("ttl_order_ownership_conflict")
        raw = json.dumps([objects,fills,execution,stream],sort_keys=True,separators=(',',':'))
        return objects, claims, hashlib.sha256(raw.encode()).hexdigest(), fills

    def due(self, now, seconds):
        day = now.strftime('%Y%m%d')
        with self.db() as db:
            rows, claims, watermark, fills = self._snapshot(db,day)
        result = []
        for row in rows:
            previous=claims.get(row['ord_no'])
            if previous and not self._retryable(*previous):continue
            if previous and not _moment(previous[1]['observed_at'])<now:continue
            row['_ttl_attempt_sequence']=previous[0]['attempt_sequence']+1 if previous else 0
            registered, prepared = _moment(row['registered_at']), _moment(row['created_at'])
            if (registered.strftime('%Y%m%d') != day or prepared > registered or registered > now
                    or re.fullmatch(r'[0-9]{6}', row['symbol'] or '') is None
                    or _order_number(row['ord_no']) != row['ord_no']):
                raise TTLUnavailable('ttl_order_ownership_unverified')
            quantity = _integer(row['quantity'],positive=True)
            _integer(row['order_price'],positive=True)
            owned_fills = [f for f in fills if f[0] == row['ord_no']]
            if any(f[4] != row['symbol'] or f[5] != 'buy' or f[6] != row['strategy'] for f in owned_fills):
                raise TTLUnavailable('ttl_fill_ownership_conflict')
            filled = sum(_integer(f[2],positive=True) for f in owned_fills)
            if filled > quantity:
                raise TTLUnavailable('ttl_fill_quantity_conflict')
            auction_end = _auction_window_end(row, registered)
            elapsed_due = (now-registered).total_seconds() >= seconds
            auction_due = auction_end is not None and now >= auction_end
            window_expiry = row['strategy'] in _WINDOW_EXPIRY_STRATEGIES and auction_end is not None
            # Close-auction entries must remain alive until their proven
            # window closes, even when that window is later than ACK+180s.
            # No valid hash-bound window => ordinary TTL, never a guessed
            # extension. C/GDR retain the earlier-of-TTL/window contract.
            expires = auction_due if window_expiry else elapsed_due or auction_due
            if filled < quantity and expires:
                row['ledger_filled_qty'] = filled
                row['due_reason'] = 'auction_window_end' if window_expiry or auction_due and not elapsed_due else 'ttl'
                row['order_window_end_at'] = auction_end.isoformat() if auction_end else None
                row['expiry_policy'] = 'verified_auction_window' if window_expiry else 'earlier_of_ttl_and_verified_window'
                row['expires_at'] = (auction_end if window_expiry else
                    min(registered+timedelta(seconds=seconds),auction_end) if auction_end else
                    registered+timedelta(seconds=seconds)).isoformat()
                result.append(row)
        return result, watermark

    def unchanged(self, day, watermark):
        with self.db() as db:
            if self._snapshot(db,day)[2] != watermark:
                raise TTLUnavailable('ttl_ledger_changed')

    def prior_claims(self, day):
        with self.db() as db:
            if not self._has_claims(db):
                return []
            result=[]
            for row in db.execute('SELECT original_ord_no FROM buy_ttl_cancellation_attempts WHERE scope=? AND day=? ORDER BY claimed_at',(self.scope,day)):
                saved,outcome=self.latest_attempt(db,day,row[0])
                result.append({'original_ord_no':saved['original_ord_no'],'symbol':saved['symbol'],
                    'attempt_sequence':saved['attempt_sequence'],'status':outcome['status'] if outcome else None,
                    'cancellation_ord_no':outcome['cancellation_ord_no'] if outcome else None})
            return result

    def unsettled_claims(self,day):
        from kiwoom_autotrader.trading.cancellation_settlement import settlement_is_published
        with self.db() as db:
            if not self._has_claims(db):return []
            pending=[]
            for row in db.execute('SELECT * FROM buy_ttl_cancellation_attempts WHERE scope=? AND day=? ORDER BY claimed_at',(self.scope,day)):
                saved,outcome=self.latest_attempt(db,day,row['original_ord_no'])
                if self._retryable(saved,outcome):continue
                order=db.execute("SELECT * FROM orders WHERE scope=? AND day=? AND mode='paper' AND ord_no=?",(self.scope,day,row['original_ord_no'])).fetchone()
                if order is None:raise TTLUnavailable('ttl_original_order_missing')
                if not settlement_is_published(db,self.scope,order):pending.append(saved)
            return pending

    def claim(self, row, evidence, watermark):
        raw = json.dumps(evidence,ensure_ascii=True,sort_keys=True,separators=(',',':'))
        with self.db(write=True) as db:
            self._schema(db)
            if self._snapshot(db,row['day'])[2] != watermark:
                raise TTLUnavailable('ttl_ledger_changed')
            expected=row.get('_ttl_attempt_sequence',0)
            saved,outcome=self.latest_attempt(db,row['day'],row['ord_no'])
            if expected:
                if (saved is None or expected!=saved['attempt_sequence']+1 or not self._retryable(saved,outcome)
                        or not _moment(outcome['observed_at'])<_moment(evidence['claimed_at'])):return False
                return db.execute('INSERT OR IGNORE INTO buy_ttl_cancellation_retry_attempts VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                    (self.scope,row['day'],row['ord_no'],expected,row['intent_id'],row['symbol'],evidence['claimed_at'],
                     raw,hashlib.sha256(raw.encode()).hexdigest(),saved['evidence_sha256'],self._outcome_digest(outcome))).rowcount==1
            if saved is not None:return False
            return db.execute("INSERT OR IGNORE INTO buy_ttl_cancellation_attempts VALUES (?,?,?,?,?,?,?,?)",
                (self.scope,row['day'],row['ord_no'],row['intent_id'],row['symbol'],evidence['claimed_at'],
                 raw,hashlib.sha256(raw.encode()).hexdigest())).rowcount == 1

    def outcome(self, row, observed_at, status, receipt=None):
        with self.db(write=True) as db:
            sequence=row.get('_ttl_attempt_sequence',0)
            saved,prior=self.latest_attempt(db,row['day'],row['ord_no'])
            if saved is None or saved['attempt_sequence']!=sequence or prior is not None:
                raise TTLUnavailable('ttl_outcome_attempt_mismatch')
            if sequence:
                db.execute('INSERT INTO buy_ttl_cancellation_retry_outcomes VALUES (?,?,?,?,?,?,?,?)',
                    (self.scope,row['day'],row['ord_no'],sequence,observed_at,status,
                     receipt.cancellation_order_number if receipt else None,
                     receipt.reported_cancel_quantity if receipt else None))
                return
            db.execute("INSERT INTO buy_ttl_cancellation_outcomes VALUES (?,?,?,?,?,?,?)",
                (self.scope,row['day'],row['ord_no'],observed_at,status,
                 receipt.cancellation_order_number if receipt else None,
                 receipt.reported_cancel_quantity if receipt else None))


class UnfilledBuyTTLMonitor:
    def __init__(self, *, now=None, pending_fetcher=None, cancel_handler=None, monotonic=None, settlement_handler=None):
        self.now = now or (lambda: datetime.now(KST))
        self.pending_fetcher = pending_fetcher or _pending_rows
        self.cancel_handler = cancel_handler or cancel_paper_buy_remainder
        self.monotonic = monotonic or time.monotonic
        self.settlement_handler=settlement_handler
        self.next_at = None
        self.last_status = None
        self.sell_monitor = None
        self.last_sell_status = None
        self._settlement_cursor = 0

    def _clock(self):
        value=self.monotonic()
        if type(value) not in (int,float) or not math.isfinite(value):
            raise TTLUnavailable('ttl_clock_unverified')
        return value

    @staticmethod
    def _stream(session):
        recorder = getattr(session,'execution_recorder',None)
        if recorder is None or not callable(getattr(recorder,'status',None)):
            raise TTLUnavailable('ttl_stream_unverified')
        status = recorder.status()
        if (not isinstance(status,dict) or status.get('ready') is not True or status.get('error') is not None
                or any(type(status.get(k)) is not int or status[k] < 0 for k in
                       ('gap_count','observations','ingress_sequence','engine_ingress_sequence'))
                or type(status.get('inflight_callbacks')) is not int or status['inflight_callbacks'] != 0
                or type(status.get('engine_pending_callbacks')) is not int or status['engine_pending_callbacks'] != 0
                or status['ingress_sequence'] != status['engine_ingress_sequence']):
            raise TTLUnavailable('ttl_stream_unverified')
        return (recorder, *(status[k] for k in ('gap_count','observations','ingress_sequence','engine_ingress_sequence')))

    @staticmethod
    def _match(row, pending):
        number = row['ord_no']
        matches = []
        if not isinstance(pending,list) or len(pending)>10000:
            raise TTLUnavailable('ttl_pending_schema_unverified')
        for item in pending:
            if not isinstance(item,dict):
                raise TTLUnavailable('ttl_pending_schema_unverified')
            if _order_number(item.get('ord_no')) == number:
                matches.append(item)
            elif item.get('orig_ord_no') not in (None,'','0','0000000') and _order_number(item['orig_ord_no']) == number:
                raise TTLUnavailable('ttl_original_already_modified')
        if not matches:
            return None
        if len(matches)!=1:
            raise TTLUnavailable('ttl_duplicate_pending_order')
        item = matches[0]
        symbol = item.get('stk_cd')
        if isinstance(symbol,str) and re.fullmatch(r'A[0-9]{6}',symbol):
            symbol=symbol[1:]
        remaining = _integer(item.get('oso_qty'))
        if remaining==0:
            return None
        if (symbol != row['symbol'] or item.get('io_tp_nm','').lstrip('+-')!='매수'
                or item.get('stex_tp')!='1' or item.get('sor_yn')!='N'
                or _order_number(item.get('orig_ord_no'),zero=True)!='0'
                or item.get('ord_stt') not in ('접수','확인','체결')
                or _integer(item.get('ord_qty'),positive=True)!=row['quantity']
                or _integer(item.get('ord_pric'),positive=True)!=int(row['order_price'])
                or remaining!=row['quantity']-row['ledger_filled_qty']):
            raise TTLUnavailable('ttl_pending_order_mismatch')
        # Broker submission HHMMSS must match this same-day owned receipt's
        # prepare/ACK interval. It is never used as the TTL start time.
        stamp=item.get('tm')
        if not isinstance(stamp,str) or re.fullmatch(r'[0-9]{6}',stamp) is None:
            raise TTLUnavailable('ttl_pending_order_time_unverified')
        try:
            submitted=datetime.strptime(row['day']+stamp,'%Y%m%d%H%M%S').replace(tzinfo=KST)
        except ValueError:
            raise TTLUnavailable('ttl_pending_order_time_unverified') from None
        if not _moment(row['created_at'])-timedelta(seconds=1)<=submitted<=_moment(row['registered_at'])+timedelta(seconds=1):
            raise TTLUnavailable('ttl_pending_order_time_unverified')
        return remaining

    def _cancel_due_order(self,session,tracker,ledger,candidate,seconds,identity,gate,cycle_at,*,enabled_check=None):
        """One guarded owned order. No failed/unknown order is resent here."""
        row=candidate;claimed=False
        try:
            current=_moment(self.now())
            if (session_identity(session)!=identity or current.date()!=cycle_at.date()
                    or gate is not getattr(session,'paper_automation_reconcile',None)
                    or getattr(gate,'_tracker',None) is not tracker or getattr(gate,'_session',None) is not session
                    or enabled_check is not None and enabled_check() is not True):
                raise TTLUnavailable('ttl_evidence_changed_or_stale')
            initial_stream=self._stream(session)
            fresh,watermark=ledger.due(current,seconds)
            matching=[r for r in fresh if r['ord_no']==candidate['ord_no']]
            if not matching:
                return {'status':'no_longer_due_owned_limit_buy','original_order_number':row['ord_no'],
                    'symbol':row['symbol'],'observed_at':current.isoformat(),'principal_reservation_released':False}
            if len(matching)!=1:raise TTLUnavailable('ttl_order_ownership_conflict')
            row=matching[0]
            identity_fields=('day','ord_no','symbol','strategy','quantity','registered_at','created_at','intent_id','order_price','order_type')
            if any(row[k]!=candidate[k] for k in identity_fields):raise TTLUnavailable('ttl_order_identity_changed')
            query_started=_moment(self.now());monotonic_started=self._clock()
            rows=self.pending_fetcher(session,observer=tracker.record_broker_observation,
                day=cycle_at.strftime('%Y%m%d'),integrated=True)
            observed=_moment(self.now())
            def guard():
                current=_moment(self.now())
                if (session_identity(session)!=identity or self._stream(session)!=initial_stream
                        or gate is not getattr(session,'paper_automation_reconcile',None)
                        or getattr(gate,'_tracker',None) is not tracker or getattr(gate,'_session',None) is not session
                        or not 0<=(current-query_started).total_seconds()<=10
                        or not 0<=self._clock()-monotonic_started<=10
                        or current.date()!=cycle_at.date()
                        or enabled_check is not None and enabled_check() is not True):
                    raise TTLUnavailable('ttl_evidence_changed_or_stale')
                ledger.unchanged(cycle_at.strftime('%Y%m%d'),watermark)
            guard()
            try:remaining=self._match(row,rows)
            except TTLUnavailable as error:
                return {'status':'no_matching_broker_remainder','observed_at':observed.isoformat(),
                    'symbol_errors':[{'symbol':row['symbol'],'original_order_number':row['ord_no'],'reason':str(error)}],
                    'original_order_number':row['ord_no'],'symbol':row['symbol'],
                    'retry_at':self.next_at.isoformat(),'principal_reservation_released':False}
            if remaining is None:
                return {'status':'no_matching_broker_remainder','observed_at':observed.isoformat(),
                    'symbol_errors':[],'original_order_number':row['ord_no'],'symbol':row['symbol'],
                    'retry_at':self.next_at.isoformat(),'principal_reservation_released':False}
            evidence={'schema':'gdcrs-unfilled-buy-ttl-v1','strategy':row['strategy'],'mode':'paper',
                'original_order_number':row['ord_no'],'symbol':row['symbol'],
                'accepted_at':row['registered_at'],'intent_created_at':row['created_at'],
                'ttl_seconds':seconds,'broker_remaining_qty':remaining,'cancel_request_qty':0,
                'due_reason':row['due_reason'],'order_window_end_at':row['order_window_end_at'],
                'expiry_policy':row['expiry_policy'],'expires_at':row['expires_at'],
                'query_started_at':query_started.isoformat(),'broker_observed_at':observed.isoformat(),
                'claimed_at':_moment(self.now()).isoformat(),'ledger_watermark':watermark,
                'session_sha256':hashlib.sha256((identity[0]+'\0'+identity[1]).encode()).hexdigest(),
                'stream_counters':list(initial_stream[1:]),'ack_releases_principal':False}
            guard();claimed=ledger.claim(row,evidence,watermark)
            if not claimed:
                return {'status':'cancel_already_claimed','original_order_number':row['ord_no'],
                    'symbol':row['symbol'],'principal_reservation_released':False}
            receipt=self.cancel_handler(session,row['symbol'],row['ord_no'],before_send=guard)
            if (not isinstance(receipt,CancellationReceipt) or receipt.original_order_number!=row['ord_no']
                    or type(receipt.reported_cancel_quantity) is not int
                    or not 0<=receipt.reported_cancel_quantity<=remaining):
                raise CancellationError('cancel_response_unconfirmed',uncertain=True)
            ledger.outcome(row,_moment(self.now()).isoformat(),'cancel_input_ack_unconfirmed_settlement',receipt)
            return {'status':'cancel_input_ack_unconfirmed_settlement',
                'original_order_number':row['ord_no'],'cancellation_order_number':receipt.cancellation_order_number,
                'symbol':row['symbol'],'observed_remaining_qty':remaining,'observed_at':_moment(self.now()).isoformat(),
                'principal_reservation_released':False}
        except Exception as error:
            reason=str(error) if isinstance(error,(TTLUnavailable,CancellationError)) else 'ttl_verification_unavailable'
            if claimed:
                status='cancel_unconfirmed' if not isinstance(error,CancellationError) or error.uncertain else str(error)
                try:ledger.outcome(row,_moment(self.now()).isoformat(),status)
                except Exception:reason='ttl_outcome_record_unavailable'
            return {'status':'retry_pending','reason':reason,'observed_at':_moment(self.now()).isoformat(),
                'original_order_number':row['ord_no'],'symbol':row['symbol'],
                'retry_at':self.next_at.isoformat(),'principal_reservation_released':False}

    def tick(self, session, tracker, seconds, *, enabled_check=None):
        # This branch precedes session/DB/clock/transport access, preserving
        # the disabled contract even if the tracker is absent or malformed.
        if seconds==0:
            return
        if type(seconds) is not int or not 1<=seconds<=86400:
            raise TTLUnavailable('ttl_setting_invalid')
        now=_moment(self.now())
        if self.next_at and now<self.next_at:
            return
        self.next_at=now+timedelta(seconds=30)
        # This separate paper-only path never treats a sell-cancel ACK as a
        # position release; it uses the same session and durable no-resend rule.
        if getattr(session,'mode',None)=='paper':
            try:
                from kiwoom_autotrader.config.settings import effective_paper_experiment
                if effective_paper_experiment(tracker.settings_loader(),'paper')['enabled']:
                    if self.sell_monitor is None:
                        from .unfilled_sell_auction import SellAuctionMonitor
                        self.sell_monitor=SellAuctionMonitor(now=self.now,pending_fetcher=self.pending_fetcher,
                            cancel_handler=self.cancel_handler,monotonic=self.monotonic)
                    self.sell_monitor.tick(session,tracker,enabled_check=enabled_check)
                    self.last_sell_status=self.sell_monitor.last_status
            except Exception:
                self.last_sell_status={'status':'retry_pending','reason':'sell_cancel_unavailable','position_released':False}
        row=None
        claimed=False
        try:
            identity=session_identity(session)
            if identity[0]!='paper':
                return
            gate=getattr(session,'paper_automation_reconcile',None)
            if getattr(gate,'_tracker',None) is not tracker or getattr(gate,'_session',None) is not session:
                raise TTLUnavailable('ttl_account_scope_unverified')
            initial_stream=self._stream(session)
            ledger=TTLLedger(tracker)
            prior=ledger.unsettled_claims(now.strftime('%Y%m%d'))
            prior_status = None
            if prior:
                from kiwoom_autotrader.trading.cancellation_settlement import (
                    settle_cancellation,CancellationSettlementUnavailable)
                handler=self.settlement_handler or settle_cancellation
                claim = prior[self._settlement_cursor % len(prior)]
                self._settlement_cursor += 1
                try:
                    result=handler(session,tracker,claim,now=self.now,monotonic=self.monotonic)
                    self.last_status=dict(result,observed_at=_moment(self.now()).isoformat())
                except CancellationSettlementUnavailable as error:
                    self.last_status={'status':'settlement_unconfirmed','reason':str(error),
                        'original_order_number':claim['original_ord_no'],'observed_at':now.isoformat(),
                        'principal_reservation_released':False,'retry_at':self.next_at.isoformat()}
                except Exception:
                    self.last_status={'status':'settlement_unconfirmed','reason':'ttl_settlement_unavailable',
                        'original_order_number':claim['original_ord_no'],'observed_at':now.isoformat(),
                        'principal_reservation_released':False,'retry_at':self.next_at.isoformat()}
                # This claim is never re-sent. A slow/unconfirmed cancellation
                # does not prevent other symbols' due buys being considered.
                prior_status = self.last_status
                initial_stream = self._stream(session)
            due, _=ledger.due(now,seconds)
            if not due:
                self.last_status=prior_status or {'status':'no_due_owned_limit_buy','observed_at':now.isoformat()}
                return
            batch_started=self._clock()
            results=[]
            # Visit the entire initial due set, not one matching order per
            # 30-second tick. Each order gets a new broker/00/ledger snapshot;
            # an earlier cancellation cannot age the next order's guard.
            for candidate in due:
                results.append(self._cancel_due_order(session,tracker,ledger,candidate,seconds,
                    identity,gate,now,enabled_check=enabled_check))
            primary=next((r for r in results if r.get('status')=='retry_pending'),results[-1])
            self.last_status=dict(primary,batch_due_count=len(due),batch_processed_count=len(results),
                batch_results=results,batch_elapsed_seconds=max(0,self._clock()-batch_started))
            if prior_status is not None:
                self.last_status['settlement_status']=prior_status
        except Exception as error:
            # Retry verification next cycle, preserving the durable no-resend
            # claim and all original principal until final settlement proof.
            reason=str(error) if isinstance(error,(TTLUnavailable,CancellationError)) else 'ttl_verification_unavailable'
            if claimed:
                status='cancel_unconfirmed' if not isinstance(error,CancellationError) or error.uncertain else str(error)
                try:
                    ledger.outcome(row,_moment(self.now()).isoformat(),status)
                except Exception:
                    reason='ttl_outcome_record_unavailable'
            self.last_status={'status':'retry_pending','reason':reason,'observed_at':now.isoformat(),
                              'original_order_number':row['ord_no'] if row else None,
                              'retry_at':self.next_at.isoformat(), 'principal_reservation_released':False}
