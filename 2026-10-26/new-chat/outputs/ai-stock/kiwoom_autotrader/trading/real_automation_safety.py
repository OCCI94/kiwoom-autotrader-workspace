"""Conservative REAL automatic buys, independent of paper experiment options.

No activation occurs here. paper_automation_only must separately be disabled.
The account and pending readers use the existing session and HTTP queue. Limits
are fixed safety ceilings, not configurable through paper strategy sidecars.
"""
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
import re
import sqlite3
import time

from kiwoom_autotrader.api.session import KST
from kiwoom_autotrader.api.minute_chart import session_identity
from kiwoom_autotrader.api.account_report import fetch_account_report
from kiwoom_autotrader.api.executions import _request_rows

PER_BUY_KRW = 1_000_000
PER_SYMBOL_KRW = 1_000_000
DAILY_ACCOUNT_KRW = 2_000_000
DAILY_SYMBOL_ENTRIES = 2
COOLDOWN_MINUTES = 20


class RealAutomationSafetyError(ValueError):
    pass


def require(ok):
    if not ok:
        raise RealAutomationSafetyError('실전 자동주문 한도·잔고·원장 증빙이 미확정이어서 전송을 보류합니다.')


def integer(value, *, zero=False):
    require(type(value) in (str, int) and re.fullmatch(r'\+?[0-9]{1,12}', str(value)) is not None)
    number = int(value)
    require(number >= 0 if zero else number > 0)
    return number


@contextmanager
def read_ledger(tracker):
    path = Path(tracker.path)
    require(not path.is_symlink() and path.is_file())
    with sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=2) as db:
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA query_only=ON'); db.execute('BEGIN')
        tracker._validate_daily_ledger(db)
        yield db


def real_ledger_rows(db, scope, day):
    """Actual intents + immutable decision price, not guessed fill averages."""
    rows = db.execute('''SELECT i.id,i.day,i.symbol,i.side,i.quantity,i.strategy,i.created_at,
        i.order_number,i.state,i.reason,d.order_price FROM intents i
        LEFT JOIN order_decisions d ON d.scope=i.scope AND d.intent_id=i.id
        WHERE i.scope=? AND i.mode='real' AND i.day=? ORDER BY i.id LIMIT 10001''', (scope, day)).fetchall()
    require(len(rows) <= 10000)
    return [dict(row) for row in rows]


def assert_real_buy_ledger(db, scope, symbol, quantity, price, now, ignored_intent_id=None):
    require(isinstance(now, datetime) and now.tzinfo is not None and re.fullmatch(r'[0-9]{6}', symbol or '') is not None)
    qty, limit = integer(quantity), integer(price)
    principal = qty * limit
    require(principal <= PER_BUY_KRW)
    day = now.astimezone(KST).strftime('%Y%m%d')
    rows = real_ledger_rows(db, scope, day)
    spent = count = 0
    for row in rows:
        if row['id'] == ignored_intent_id:
            require(row['symbol'] == symbol and row['side'] == 'buy' and row['state'] == 'prepared'
                    and row['order_number'] is None and integer(row['quantity']) == qty
                    and integer(row['order_price']) == limit)
            continue
        if row['state'] == 'cancelled':
            # Only a locally proven unsent intent releases the reservation.
            require(row['reason'] == 'not_sent' and row['order_number'] is None)
            continue
        require(row['state'] in ('prepared', 'accepted'))
        require((row['state'] == 'prepared' and row['order_number'] is None)
                or (row['state'] == 'accepted' and row['order_number'] is not None))
        created = datetime.fromisoformat(row['created_at'])
        require(created.tzinfo is not None and created <= now)
        if row['side'] == 'buy':
            spent += integer(row['quantity']) * integer(row['order_price'])
            if row['symbol'] == symbol:
                count += 1
        elif row['side'] == 'sell' and row['symbol'] == symbol:
            # Submission is no earlier than a later fill. The broker-level
            # pending gate prevents reentry before actual completion; see
            # additionally the recorded latest unit fill below.
            require(now - created >= timedelta(minutes=COOLDOWN_MINUTES))
        else:
            require(row['side'] == 'sell')
    latest = db.execute("SELECT MAX(moment) FROM fills WHERE scope=? AND mode='real' AND day=? AND symbol=? AND side='sell'", (scope, day, symbol)).fetchone()[0]
    if latest is not None:
        require(isinstance(latest, str) and re.fullmatch(r'[0-2][0-9]:[0-5][0-9]:[0-5][0-9]', latest) is not None)
        at = datetime.strptime(day + latest, '%Y%m%d%H:%M:%S').replace(tzinfo=KST)
        require(timedelta(minutes=COOLDOWN_MINUTES) <= now - at)
    require(spent + principal <= DAILY_ACCOUNT_KRW and count < DAILY_SYMBOL_ENTRIES)
    return {'per_buy_cap_krw':PER_BUY_KRW,'symbol_cap_krw':PER_SYMBOL_KRW,
            'daily_account_cap_krw':DAILY_ACCOUNT_KRW,'prior_daily_reserved_krw':spent,
            'prior_symbol_entries':count,'day':day}


class RealAutomaticBuyGuard:
    """Fresh shared-session account checks, then only local final-send checks."""
    def __init__(self, session, tracker, symbol, quantity, price, strategy, *, account=None, pending=None, now=None, clock=None):
        self.session, self.tracker, self.symbol = session, tracker, symbol
        self.quantity, self.price, self.strategy = quantity, price, strategy
        self.account = account or fetch_account_report
        self.pending = pending or (lambda s: _request_rows(s,'ka10075',{'all_stk_tp':'0','trde_tp':'0','stex_tp':'0'},'oso'))
        self.now = now or (lambda:datetime.now(KST)); self.clock = clock or time.monotonic

    def checkpoint(self):
        identity = session_identity(self.session); require(identity[0] == 'real')
        status = self.session.execution_recorder.status()
        require(isinstance(status,dict) and status.get('ready') is True and status.get('error') is None
                and status.get('inflight_callbacks') == 0 and status.get('engine_pending_callbacks') == 0)
        fields = ('gap_count','observations','ingress_sequence','engine_ingress_sequence')
        require(all(type(status.get(k)) is int and status[k] >= 0 for k in fields))
        return (identity, id(self.session.execution_recorder), *(status[k] for k in fields))

    def prepare(self):
        self.initial = self.checkpoint(); self.started = self.clock(); now = self.now()
        qty, price = integer(self.quantity), integer(self.price)
        with read_ledger(self.tracker) as db:
            assert_real_buy_ledger(db,self.tracker.scope,self.symbol,qty,price,now)
            self.before = real_ledger_rows(db,self.tracker.scope,now.strftime('%Y%m%d'))
            self.tracker._assert_no_pending_account(db,'real')
        report = self.account(self.session)
        require(isinstance(report,dict) and report.get('mode') == 'real' and report.get('exchange') == 'KRX'
                and report.get('snapshot_changed') is False and isinstance(report.get('holdings'),list))
        total = 0; seen = set()
        for row in report['holdings']:
            require(isinstance(row,dict) and re.fullmatch(r'[AJQ]?[0-9]{6}',row.get('stk_cd','')) is not None)
            key = (row['stk_cd'][-6:],row.get('crd_tp'),row.get('crd_loan_dt'))
            require(key not in seen); seen.add(key)
            if key[0] == self.symbol:
                held = integer(row.get('rmnd_qty'),zero=True)
                if held:
                    total += integer(row.get('pur_amt'))
        require(total + qty * price <= PER_SYMBOL_KRW)
        cash = integer(report.get('cash',{}).get('ord_alow_amt'),zero=True)
        require(cash >= (qty * price * 1005 + 999)//1000)
        rows = self.pending(self.session); require(isinstance(rows,(list,tuple)))
        for row in rows:
            require(isinstance(row,dict) and re.fullmatch(r'A?[0-9]{6}',row.get('stk_cd','')) is not None)
            # Conservative real mode serializes the entire account.
            require(integer(row.get('oso_qty'),zero=True) == 0)
        require(self.checkpoint() == self.initial and 0 <= self.clock() - self.started <= 20)
        self.day = now.strftime('%Y%m%d')
        return self

    def __call__(self):
        require(self.now().strftime('%Y%m%d') == self.day and self.checkpoint() == self.initial
                and 0 <= self.clock() - self.started <= 20)
        with read_ledger(self.tracker) as db:
            current = real_ledger_rows(db,self.tracker.scope,self.day)
            before_ids = {r['id'] for r in self.before}
            additions = [r for r in current if r['id'] not in before_ids]
            require([r for r in current if r['id'] in before_ids] == self.before and len(additions) == 1)
            own = additions[0]
            require(own['strategy'] == self.strategy)
            assert_real_buy_ledger(db,self.tracker.scope,self.symbol,self.quantity,self.price,self.now(),own['id'])
            self.tracker._assert_no_pending_account(db,'real',ignored_intent_id=own['id'])


def real_buy_budget(budget):
    return min(integer(budget),PER_BUY_KRW)
