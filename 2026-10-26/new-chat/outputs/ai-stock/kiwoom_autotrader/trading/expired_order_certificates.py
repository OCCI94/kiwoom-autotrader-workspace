"""Immutable once-collected prior-day terminal order proofs, never new fills."""
from datetime import datetime
import hashlib
import json
import re
import sqlite3
from .decision_evidence import canonical_json
from .broker_observation_repeats import evidence_table

TABLE='paper_expired_order_certificates'
KEYS=('scope','day','mode','ord_no','symbol','side','strategy','quantity','registered_at','filled_quantity')
_V2_REPEAT_PAIR = {'218972': ('002790', 7, 26250), '219563': ('010660', 52, 3690)}


def _number(value, digits=20):
    if isinstance(value, str) and re.fullmatch(rf'[0-9]{{1,{digits}}}', value):
        return int(value)
    return None


def _paired_order(order):
    return (order['scope'], order['day'], order['mode']) == ('default', '20261104', 'paper') and order['ord_no'] in _V2_REPEAT_PAIR


def paired_v2_captured(db, order):
    """Once issued, neither member may bypass the pair's v2 verification."""
    if not _paired_order(order) or not db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)).fetchone():
        return False
    rows = db.execute(f'SELECT payload_json FROM {TABLE} WHERE scope=? AND day=? AND ord_no IN (?,?) LIMIT 1001',
                      (order['scope'], order['day'], *_V2_REPEAT_PAIR))
    for index, row in enumerate(rows, 1):
        if index > 1000:
            return True
        try:
            proof = json.loads(row[0])
        except (ValueError, TypeError):
            return True
        if not isinstance(proof, dict) or proof.get('schema') != 'paper-expired-terminal-proof-v1':
            return True
    return False


def _same_v2_details(db, order, cutoff, captured_at, now):
    """Recheck every later query for the two orders issued as one pair."""
    expected = _V2_REPEAT_PAIR.get(order['ord_no'])
    if (not _paired_order(order) or (order['strategy'], order['side']) != ('EXP_D', 'buy')
            or (order['symbol'], order['quantity'], order['filled_quantity']) != (*expected[:2], 0)):
        return False

    originals = {int(number): terms for number, terms in _V2_REPEAT_PAIR.items()}
    queries = {}
    last_id, last_observed = cutoff, captured_at
    entries = db.execute(f"""SELECT id,observed_at,digest,payload_json FROM {evidence_table(db)}
        WHERE scope=? AND mode='paper' AND day=? AND source_api='kt00007' AND id>?
        ORDER BY id LIMIT 20001""", (order['scope'], order['day'], cutoff))
    for count, entry in enumerate(entries, 1):
        raw = entry['payload_json']
        if (count > 20000 or type(entry['id']) is not int or entry['id'] <= last_id
                or not isinstance(raw, str) or hashlib.sha256(raw.encode()).hexdigest() != entry['digest']):
            return False
        event = json.loads(raw)
        if not isinstance(event, dict):
            return False
        query_id, page = event.get('query_id'), event.get('page')
        request, response, cont = (event.get(k) for k in ('request_body', 'response_body', 'continuation'))
        if (event.get('schema') != 'kiwoom-broker-page-v1' or event.get('source_api') != 'kt00007'
                or event.get('source_kind') != 'broker_observation' or event.get('mode') != 'paper'
                or event.get('day') != order['day'] or event.get('status') != 'success'
                or event.get('http_status') != 200 or event.get('observed_at') != entry['observed_at']
                or not isinstance(query_id, str) or re.fullmatch(r'[0-9a-f]{32}', query_id) is None
                or type(page) is not int or not 1 <= page <= 20
                or not all(isinstance(value, dict) for value in (request, response, cont))):
            return False
        started = datetime.fromisoformat(event['request_started_at'])
        observed = datetime.fromisoformat(event['observed_at'])
        previous_page, previous_end, body, complete, row_count, found = queries.get(
            query_id, (0, None, request, False, 0, set()))
        if (started.tzinfo is None or observed.tzinfo is None or not started <= observed <= now
                or previous_end is not None and started < previous_end
                or observed < last_observed or page != previous_page + 1 or complete or request != body
                or request.get('ord_dt') != order['day'] or request.get('qry_tp') not in ('1', '2')
                or request.get('stk_bond_tp') not in ('0', '1') or request.get('sell_tp') != '0'
                or request.get('stk_cd') not in (None, '') or request.get('fr_ord_no') not in (None, '')
                or request.get('dmst_stex_tp') not in ('%', 'KRX')):
            return False
        code, rows = response.get('return_code'), response.get('acnt_ord_cntr_prps_dtl')
        following = cont.get('response_cont_yn')
        if (type(code) not in (int, str) or code not in (0, '0') or not isinstance(rows, list)
                or row_count + len(rows) > 20000 or cont.get('request_cont_yn') != ('N' if page == 1 else 'Y')
                or following not in ('Y', 'N', '', None) or cont.get('next_key_present') is not (following == 'Y')):
            return False
        for row in rows:
            if not isinstance(row, dict):
                return False
            number, parent = (_number(row.get(field), 7) for field in ('ord_no', 'ori_ord'))
            if not number or parent is None or parent in originals:
                return False
            if number not in originals:
                continue
            symbol, quantity, price = originals[number]
            if (number in found or parent != 0 or row.get('stk_cd') not in (symbol, 'A' + symbol)
                    or row.get('io_tp_nm') not in ('현금매수', '+매수') or row.get('dmst_stex_tp') != 'KRX'
                    or row.get('mdfy_cncl') != '일반' or row.get('acpt_tp') != '주문완료'
                    or tuple(_number(row.get(field)) for field in
                             ('ord_qty', 'cnfm_qty', 'ord_uv', 'cntr_qty', 'cntr_uv', 'ord_remnq'))
                    != (quantity, quantity, price, 0, 0, quantity)):
                return False
            found.add(number)
        queries[query_id] = page, observed, body, following != 'Y', row_count + len(rows), found
        last_id, last_observed = entry['id'], observed
    return all(complete and found == originals.keys() for _, _, _, complete, _, found in queries.values())


def create_schema(db):
    db.executescript('''
      CREATE TABLE IF NOT EXISTS paper_expired_order_certificates (
        id INTEGER PRIMARY KEY,scope TEXT NOT NULL,day TEXT NOT NULL,ord_no TEXT NOT NULL,
        captured_at TEXT NOT NULL,payload_json TEXT NOT NULL,digest TEXT NOT NULL);
      CREATE INDEX IF NOT EXISTS paper_expired_order_certificates_order
        ON paper_expired_order_certificates(scope,day,ord_no,id);
      CREATE TRIGGER IF NOT EXISTS paper_expired_order_certificates_no_update
        BEFORE UPDATE ON paper_expired_order_certificates
        BEGIN SELECT RAISE(ABORT,'terminal certificates are immutable'); END;
      CREATE TRIGGER IF NOT EXISTS paper_expired_order_certificates_no_delete
        BEFORE DELETE ON paper_expired_order_certificates
        BEGIN SELECT RAISE(ABORT,'terminal certificates are immutable'); END;
    ''')


def capture_certificates(tracker,db,now,*,approved_rejected_orders=(),approval_manifest_sha256=None):
    # Generic recovery callers have no opt-in. The separately authorized
    # reconciliation caller must bind exact orders and an approval artifact.
    if approved_rejected_orders or approval_manifest_sha256 is not None:
        if (not isinstance(approved_rejected_orders, tuple) or not approved_rejected_orders
                or not isinstance(approval_manifest_sha256, str)
                or re.fullmatch('[0-9a-f]{64}',approval_manifest_sha256) is None
                or any(not isinstance(item, tuple) or len(item) != 2
                       or any(not isinstance(value, str) for value in item)
                       or re.fullmatch('[0-9]{8}',item[0]) is None
                       or re.fullmatch('[1-9][0-9]{0,6}',item[1]) is None
                       for item in approved_rejected_orders)
                or len(set(approved_rejected_orders)) != len(approved_rejected_orders)):
            raise ValueError('expiry_reconciliation_approval_unverified')
    today=now.strftime('%Y%m%d');rows=db.execute('''SELECT o.*,
       (SELECT COALESCE(SUM(f.quantity),0) FROM fills f WHERE f.scope=o.scope AND f.day=o.day
        AND f.mode=o.mode AND f.ord_no=o.ord_no) AS filled_quantity
        FROM orders o WHERE o.scope=? AND o.mode='paper' AND o.day<? ORDER BY o.day,o.ord_no LIMIT 10001''',
        (tracker.scope,today)).fetchall()
    if len(rows)>10000:raise ValueError('expiry_capture_order_limit')
    pair_locked = any(paired_v2_captured(db, row) for row in rows)
    captured=[];unconfirmed=[]
    for row in rows:
        order=dict(row)
        if order['filled_quantity']==order['quantity']:continue
        if verified_certificate(tracker,db,order,now):continue
        if pair_locked and _paired_order(order):
            unconfirmed.append(order['ord_no'])
            continue

        rejected_basis = {}
        if not tracker._paper_prior_order_terminal(db,order,now):
            if ((order['day'],order['ord_no']) not in approved_rejected_orders
                    or not tracker._paper_prior_order_terminal(db,order,now,
                        _rejected_auction_context=rejected_basis) or not rejected_basis):
                unconfirmed.append(order['ord_no'])
                continue
        sources=db.execute(f'''SELECT id,digest FROM {evidence_table(db)} WHERE scope=? AND mode='paper'
           AND ((day=? AND source_api='kt00007') OR (day=? AND source_api='ka10075')) ORDER BY id''',
           (tracker.scope,order['day'],today)).fetchall()
        proof={'schema':'paper-expired-terminal-proof-v1','order':{k:order[k] for k in KEYS},
               'captured_at':now.isoformat(),'source_max_id':max(r['id'] for r in sources),
               'source_hashes':[[r['id'],r['digest']] for r in sources],
               'semantics':'terminal_remainder_not_fill_or_fresh_account'}
        if rejected_basis:
            proof.update(schema='paper-expired-terminal-proof-v2', reason='rejected_exp_d_closing_buy',
                         approval_manifest_sha256=approval_manifest_sha256, rejection_evidence=rejected_basis)
        raw=canonical_json(proof,maximum=2*1024*1024)
        db.execute(f'INSERT INTO {TABLE}(scope,day,ord_no,captured_at,payload_json,digest) VALUES (?,?,?,?,?,?)',
            (tracker.scope,order['day'],order['ord_no'],now.isoformat(),raw,hashlib.sha256(raw.encode()).hexdigest()))
        captured.append(order['ord_no'])
    return {'status':'unconfirmed' if unconfirmed else 'complete','captured_orders':captured,
            'unconfirmed_orders':unconfirmed,'captured_count':len(captured)}


def verified_certificate(tracker,db,order,now):
    try:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(TABLE,)).fetchone():return False
        row=db.execute(f'SELECT * FROM {TABLE} WHERE scope=? AND day=? AND ord_no=? ORDER BY id DESC LIMIT 1',
            (tracker.scope,order['day'],order['ord_no'])).fetchone()
        if row is None:return False
        raw=row['payload_json'];proof=json.loads(raw);at=datetime.fromisoformat(row['captured_at'])
        schema=proof.get('schema')
        if (hashlib.sha256(raw.encode()).hexdigest()!=row['digest']
                or schema not in ('paper-expired-terminal-proof-v1','paper-expired-terminal-proof-v2')
                or proof.get('semantics')!='terminal_remainder_not_fill_or_fresh_account'
                or proof.get('captured_at')!=row['captured_at'] or at.tzinfo is None or not at<=now
                or proof.get('order')!={k:order[k] for k in KEYS}):return False
        if schema == 'paper-expired-terminal-proof-v1' and paired_v2_captured(db, order):
            return False
        sources=db.execute(f'''SELECT id,digest FROM {evidence_table(db)} WHERE scope=? AND mode='paper'
           AND ((day=? AND source_api='kt00007') OR (day=? AND source_api='ka10075')) AND id<=? ORDER BY id''',
            (tracker.scope,order['day'],at.strftime('%Y%m%d'),proof['source_max_id'])).fetchall()
        if [[r['id'],r['digest']] for r in sources]!=proof['source_hashes']:return False
        # Only the explicitly paired v2 proofs can survive newer detail. Every
        # subsequent query must establish the same complete zero-fill state.
        if db.execute(f'''SELECT 1 FROM {evidence_table(db)} WHERE scope=? AND mode='paper'
              AND day=? AND source_api='kt00007' AND id>? LIMIT 1''',
              (tracker.scope,order['day'],proof['source_max_id'])).fetchone():
            if schema != 'paper-expired-terminal-proof-v2' or not _same_v2_details(
                    db, order, proof['source_max_id'], at, now):
                return False
        later=db.execute(f"SELECT digest,payload_json FROM {evidence_table(db)} WHERE scope=? AND mode='paper' AND source_api='ka10075' AND id>? ORDER BY id LIMIT 20001",
            (tracker.scope,proof['source_max_id'])).fetchall()
        if len(later)>20000:return False
        original=str(int(order['ord_no']))
        for source in later:
            content=source['payload_json']
            if hashlib.sha256(content.encode()).hexdigest()!=source['digest']:return False
            event=json.loads(content);pending=event.get('response_body',{}).get('oso')
            if event.get('status')!='success' or not isinstance(pending,list):return False
            for item in pending:
                if not isinstance(item,dict):return False
                for field in ('ord_no','orig_ord_no'):
                    value=item.get(field)
                    if isinstance(value,str) and re.fullmatch(r'[0-9]{1,7}',value) and str(int(value))==original:
                        return False
        rejection = None
        if schema=='paper-expired-terminal-proof-v2':
            approval = proof.get('approval_manifest_sha256')
            if (proof.get('reason')!='rejected_exp_d_closing_buy'
                    or not isinstance(approval,str) or re.fullmatch('[0-9a-f]{64}',approval) is None):
                return False
            rejection = {}
        verified=tracker._paper_prior_order_terminal(db,order,now,_source_max_id=proof['source_max_id'],
            _captured_at=at,_rejected_auction_context=rejection)
        return verified and (rejection is None or bool(rejection) and rejection==proof.get('rejection_evidence'))
    except (sqlite3.Error,ValueError,KeyError,TypeError,OverflowError):return False
