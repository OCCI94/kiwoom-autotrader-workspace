"""Durable experiment assignments and at-most-once attempts, never fills.

An uncertain attempt remains consumed. Only broker reconciliation can release
ownership; this auxiliary journal cannot create or clear an execution ledger lot.
"""
from datetime import datetime, timedelta, timezone
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import re
import sqlite3


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def exp_d_exit_key(assignment_sha256, generation_id):
    """One owned exit cycle shared by the opening and late recovery paths."""
    if (not isinstance(assignment_sha256, str) or re.fullmatch('[0-9a-f]{64}', assignment_sha256) is None
            or not isinstance(generation_id, str) or re.fullmatch('[0-9a-f]{32}', generation_id) is None):
        raise ValueError('exp_d_exit_identity_unavailable')
    return hashlib.sha256(canonical(['EXP_D_owned_exit_cycle_v1', assignment_sha256, generation_id]).encode()).hexdigest()


def _d_exit_context(assignment, generation_id, now, entry_at):
    from .paper_experiment_rules import validate_assignment
    if (not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None
            or not isinstance(assignment, dict)):
        raise ValueError('exp_d_exit_identity_unavailable')
    entry = datetime.fromisoformat(entry_at) if isinstance(entry_at, str) else entry_at
    if not isinstance(entry, datetime) or entry.tzinfo is None or entry.utcoffset() is None:
        raise ValueError('exp_d_exit_identity_unavailable')
    kst = timezone(timedelta(hours=9)); now = now.astimezone(kst); entry = entry.astimezone(kst)
    validate_assignment(assignment, symbol=assignment['symbol'], day=assignment['day'], now=now)
    if (assignment.get('arm') != 'EXP_D' or entry > now
            or assignment['day'] != entry.strftime('%Y%m%d') or entry.date() >= now.date()
            or datetime.fromisoformat(assignment['assigned_at']) > entry):
        raise ValueError('exp_d_exit_identity_unavailable')
    key = exp_d_exit_key(assignment['assignment_sha256'], generation_id)
    binding = {'schema': 'exp-d-owned-exit-cycle-v1', 'assignment_sha256': assignment['assignment_sha256'],
               'generation_id': generation_id, 'entry_at': entry.isoformat()}
    return key, binding, entry, now


class ExperimentStore:
    def __init__(self, path):
        self.path = Path(path)
        if self.path.is_symlink():
            raise ValueError('experiment_store_symlink')
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self._db() as db:
            db.executescript('''
              CREATE TABLE IF NOT EXISTS assignments(day TEXT,symbol TEXT,payload TEXT NOT NULL,
                digest TEXT NOT NULL,PRIMARY KEY(day,symbol));
              CREATE TABLE IF NOT EXISTS attempts(signal_key TEXT PRIMARY KEY,day TEXT,symbol TEXT,
                arm TEXT,side TEXT,created_at TEXT,payload TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS outcomes(id INTEGER PRIMARY KEY,signal_key TEXT,
                observed_at TEXT,payload TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS selections(day TEXT,kind TEXT,payload TEXT NOT NULL,
                PRIMARY KEY(day,kind));
            ''')
        self.path.chmod(0o600)

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA busy_timeout=5000')
        db.execute('PRAGMA synchronous=FULL')
        try:
            with db:
                yield db
        finally:
            db.close()

    def attempted(self, key):
        with self._db() as db:
            return db.execute('SELECT 1 FROM attempts WHERE signal_key=?',(key,)).fetchone() is not None

    def assignment(self, day, symbol):
        with self._db() as db:
            row = db.execute('SELECT payload,digest FROM assignments WHERE day=? AND symbol=?', (day,symbol)).fetchone()
        if row is None:
            return None
        if hashlib.sha256(row['payload'].encode()).hexdigest() != row['digest']:
            raise ValueError('assignment_integrity')
        return json.loads(row['payload'])

    def assign(self, value):
        raw = canonical(value)
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT payload,digest FROM assignments WHERE day=? AND symbol=?',
                             (value['day'], value['symbol'])).fetchone()
            if row:
                if hashlib.sha256(row['payload'].encode()).hexdigest() != row['digest']:
                    raise ValueError('assignment_integrity')
                return json.loads(row['payload'])
            db.execute('INSERT INTO assignments VALUES(?,?,?,?)',
                       (value['day'],value['symbol'],raw,hashlib.sha256(raw.encode()).hexdigest()))
        return value

    def assign_prospective(self,day,symbol,first_seen_at,*,assigned_at,profile,theme=None):
        """Read the actual immutable prefix and allocate atomically before send.

        Existing records are returned byte-for-byte and never reallocated.
        This method is used only by future source deployments; it does not
        migrate or rewrite an already recorded day.
        """
        from .paper_experiment_rules import assign_prospective_candidate,_weights
        from .paper_experiment_theme import bind_assignment_theme
        native={arm for arm,_ in _weights(profile)}
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            existing=db.execute('SELECT payload,digest FROM assignments WHERE day=? AND symbol=?',(day,symbol)).fetchone()
            if existing is not None:
                if hashlib.sha256(existing['payload'].encode()).hexdigest()!=existing['digest']:
                    raise ValueError('assignment_integrity')
                return json.loads(existing['payload'])
            prefix=[]
            for row in db.execute('SELECT payload,digest FROM assignments WHERE day=? ORDER BY symbol',(day,)):
                if hashlib.sha256(row['payload'].encode()).hexdigest()!=row['digest']:
                    raise ValueError('assignment_integrity')
                item=json.loads(row['payload'])
                if item.get('arm') in native:
                    prefix.append({k:item[k] for k in ('symbol','arm','assigned_at','assignment_sha256')})
            value=assign_prospective_candidate(day,symbol,first_seen_at,assigned_at=assigned_at,
                prior_allocations=prefix,profile=profile)
            if theme is not None:value=bind_assignment_theme(value,theme)
            raw=canonical(value)
            db.execute('INSERT INTO assignments VALUES(?,?,?,?)',(day,symbol,raw,hashlib.sha256(raw.encode()).hexdigest()))
            return value

    def assignments(self, day):
        with self._db() as db:
            rows = db.execute('SELECT symbol FROM assignments WHERE day=? ORDER BY symbol', (day,)).fetchall()
        return [self.assignment(day, row['symbol']) for row in rows]

    def claim(self, key, *, day, symbol, arm, side, now, payload):
        """Commit before order preparation; never retry an ambiguous dispatch."""
        if not isinstance(now, datetime) or now.tzinfo is None:
            raise ValueError('experiment_clock')
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT 1 FROM attempts WHERE signal_key=?', (key,)).fetchone():
                return False
            db.execute('INSERT INTO attempts VALUES(?,?,?,?,?,?,?)',
                       (key,day,symbol,arm,side,now.isoformat(),canonical(payload)))
        return True

    def _exp_d_exit_status(self, db, assignment, generation_id, *, now, entry_at):
        key, binding, entry, now = _d_exit_context(assignment, generation_id, now, entry_at)
        rows = db.execute("""SELECT signal_key,day,created_at,
            CASE WHEN length(payload)<=65536 THEN payload ELSE NULL END AS payload
            FROM attempts WHERE symbol=? AND arm='EXP_D' AND side='sell'
            AND day>=? AND day<=? ORDER BY created_at,signal_key LIMIT 1001""",
            (assignment['symbol'], entry.strftime('%Y%m%d'), now.strftime('%Y%m%d'))).fetchall()
        if len(rows) > 1000:
            return {'status': 'unavailable', 'reason': 'exp_d_exit_attempt_history_overflow', 'key': key}
        for row in rows:
            try:
                created = datetime.fromisoformat(row['created_at'])
                if (created.tzinfo is None or created.utcoffset() is None or created > now
                        or created.astimezone(now.tzinfo).strftime('%Y%m%d') != row['day']
                        or not isinstance(row['signal_key'], str) or re.fullmatch('[0-9a-f]{64}', row['signal_key']) is None):
                    raise ValueError
                if created < entry:
                    continue  # A completed earlier generation is not this lot's exit.
                payload = json.loads(row['payload'])
                if not isinstance(payload, dict):
                    raise ValueError
                old_binding = payload.get('exp_d_exit_cycle')
                if old_binding is not None:
                    if old_binding != binding or row['signal_key'] != key:
                        raise ValueError
                    return {'status': 'consumed', 'reason': 'exp_d_exit_attempt_consumed', 'key': key,
                            'matched_key': row['signal_key'], 'legacy_attempt': False}
                # Older records contain the generation only inside their key.
                # Recompute that exact key instead of trusting their reason or
                # treating an ambiguous/missing outcome as permission to resend.
                signal_at = payload.get('dedup_at', payload.get('signal_at'))
                signal = datetime.fromisoformat(signal_at)
                if (payload.get('reason') != 'next_session_open' or payload.get('action') != 'sell'
                        or signal.tzinfo is None or signal.utcoffset() is None or not entry <= signal <= created):
                    raise ValueError
                legacy = hashlib.sha256(canonical([assignment['assignment_sha256'], 'sell', signal_at,
                    'next_session_open', generation_id]).encode()).hexdigest()
                if legacy != row['signal_key']:
                    raise ValueError
                return {'status': 'consumed', 'reason': 'exp_d_exit_legacy_attempt_consumed', 'key': key,
                        'matched_key': row['signal_key'], 'legacy_attempt': True}
            except (KeyError, TypeError, ValueError, OverflowError):
                return {'status': 'unavailable', 'reason': 'exp_d_exit_attempt_identity_unavailable', 'key': key}
        return {'status': 'available', 'reason': 'exp_d_exit_not_attempted', 'key': key}

    def exp_d_exit_status(self, assignment, generation_id, *, now, entry_at):
        """Read the opening/late claim gate; every prior attempt stays consumed.

        This is not execution or ownership evidence. Accepted, uncertain,
        claim-only and legacy not_submitted attempts all prohibit another send.
        There is no implemented formal NOT_SENT release certificate here.
        """
        try:
            with self._db() as db:
                db.execute('BEGIN')
                return self._exp_d_exit_status(db, assignment, generation_id, now=now, entry_at=entry_at)
        except (OSError, sqlite3.Error, KeyError, TypeError, ValueError, OverflowError):
            return {'status': 'unavailable', 'reason': 'exp_d_exit_attempt_identity_unavailable', 'key': None}

    def claim_exp_d_exit(self, assignment, generation_id, *, now, entry_at, payload):
        """Atomically consume one original owned generation before preparation.

        No retry is granted by a not_submitted label or absence of an outcome.
        Execution-ledger pending, ownership and send-time guards remain required.
        """
        key, binding, entry, now = _d_exit_context(assignment, generation_id, now, entry_at)
        if not isinstance(payload, dict) or 'exp_d_exit_cycle' in payload:
            raise ValueError('exp_d_exit_payload_unavailable')
        signal = datetime.fromisoformat(payload.get('signal_at'))
        if (payload.get('action') != 'sell' or payload.get('reason') not in ('next_session_open', 'late_recovery_exit')
                or signal.tzinfo is None or signal.utcoffset() is None or not entry <= signal <= now):
            raise ValueError('exp_d_exit_payload_unavailable')
        saved = dict(payload, exp_d_exit_cycle=binding)
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            status = self._exp_d_exit_status(db, assignment, generation_id, now=now, entry_at=entry)
            if status['status'] != 'available':
                return False
            db.execute('INSERT INTO attempts VALUES(?,?,?,?,?,?,?)', (key, now.strftime('%Y%m%d'),
                assignment['symbol'], 'EXP_D', 'sell', now.isoformat(), canonical(saved)))
        return True

    def outcome(self, key, now, payload):
        with self._db() as db:
            db.execute('INSERT INTO outcomes(signal_key,observed_at,payload) VALUES(?,?,?)',
                       (key,now.isoformat(),canonical(payload)))

    def selection(self, day, kind, value=None):
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT payload FROM selections WHERE day=? AND kind=?',(day,kind)).fetchone()
            if row:
                return json.loads(row['payload'])
            if value is None:
                return None
            db.execute('INSERT INTO selections VALUES(?,?,?)',(day,kind,canonical(value)))
        return value

    def market_calendar_history(self, through_day):
        """Read at most 400 calendar snapshots; validation remains with caller."""
        if (not isinstance(through_day,str) or re.fullmatch(r'[0-9]{8}',through_day) is None
                or self.path.is_symlink() or not self.path.is_file()):
            raise ValueError('calendar_history_unavailable')
        try:
            datetime.strptime(through_day,'%Y%m%d')
            with sqlite3.connect(self.path.resolve().as_uri()+'?mode=ro',uri=True,timeout=5) as db:
                db.execute('PRAGMA query_only=ON')
                rows=db.execute("SELECT day,payload FROM selections WHERE kind='market_calendar' AND day<=? "
                                "ORDER BY day DESC LIMIT 400",(through_day,)).fetchall()
            result=[]
            for day,raw in reversed(rows):
                datetime.strptime(day,'%Y%m%d')
                payload=json.loads(raw)
                if not isinstance(payload,dict):raise ValueError('calendar_history_invalid')
                result.append({'day':day,'payload':payload})
            return result
        except (ValueError,TypeError,sqlite3.Error,OSError):
            raise ValueError('calendar_history_unavailable') from None

    def recent_assignments(self, symbol, through_day, *, days=None):
        """Read saved assignment provenance without creating or modifying rows."""
        if self.path.is_symlink() or not self.path.is_file():raise ValueError('experiment_store_unavailable')
        if days is not None:
            if (not isinstance(days,(set,list,tuple)) or len(days)>32
                    or any(not isinstance(d,str) or re.fullmatch(r'[0-9]{8}',d) is None or d>through_day for d in days)):
                raise ValueError('experiment_assignment_days_invalid')
            days=sorted(set(days))
            if not days:return []
            date_filter='day IN ('+','.join('?' for _ in days)+')'
            params=tuple(days)
        else:
            date_filter='day<=?';params=(through_day,)
        with sqlite3.connect(self.path.resolve().as_uri()+'?mode=ro',uri=True,timeout=5) as db:
            db.row_factory=sqlite3.Row;db.execute('PRAGMA query_only=ON');db.execute('BEGIN')
            rows=db.execute('SELECT payload,digest FROM assignments WHERE symbol=? AND '+date_filter+' ORDER BY day DESC LIMIT 100001',
                            (symbol,*params)).fetchall()
            selections=db.execute("SELECT payload FROM selections WHERE kind='D' AND "+date_filter+" ORDER BY day DESC LIMIT 100001",params).fetchall()
            special=db.execute("SELECT payload FROM selections WHERE kind LIKE 'extended:%' AND "+date_filter+" ORDER BY day DESC LIMIT 100001",params).fetchall()
        if any(len(items)>100000 for items in (rows,selections,special)):raise ValueError('experiment_assignment_history_overflow')
        result=[]
        for row in rows:
            if hashlib.sha256(row['payload'].encode()).hexdigest()!=row['digest']:raise ValueError('assignment_integrity')
            result.append(json.loads(row['payload']))
        from .paper_experiment_rules import select_overnight
        for row in selections:
            selection=json.loads(row['payload'])
            verified=select_overnight(selection['candidates'],day=selection['day'],snapshot_at=selection['snapshot_at'],existing=selection)
            result.extend(value for value in verified['assignments'] if value['symbol']==symbol)
        from .paper_experiment_rules import validate_assignment
        for row in special:
            for value in json.loads(row['payload']).get('assignments',[]):
                if value['symbol']!=symbol:continue
                validate_assignment(value,symbol=symbol,day=value['day'],now=datetime.fromisoformat(value['assigned_at']))
                result.append(value)
        return result

    def position_updates(self,generation_id):
        with self._db() as db:
            rows=db.execute('SELECT payload FROM selections WHERE kind=? OR kind LIKE ? ORDER BY day DESC,kind DESC LIMIT 10000',
                            ('position_update:'+generation_id,'position_update:'+generation_id+':%')).fetchall()
        merged={}
        for row in reversed(rows):merged.update(json.loads(row['payload']))
        return merged

    def prior_overnight_selections(self, day):
        with self._db() as db:
            rows = db.execute("SELECT payload FROM selections WHERE kind='D' AND day<? ORDER BY day DESC LIMIT 30", (day,)).fetchall()
        return [json.loads(row['payload']) for row in rows]
