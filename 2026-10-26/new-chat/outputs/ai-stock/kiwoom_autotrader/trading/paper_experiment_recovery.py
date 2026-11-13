"""Paper experiment recovery using the existing session and shared HTTP queue.

A completed query is evidence, not an inferred fill or permission to resend an
uncertain order.  No account credentials or raw responses enter the result.
"""
from __future__ import annotations
from datetime import datetime, timedelta
import re
import sqlite3

from kiwoom_autotrader.api.executions import _request_rows, fetch_executions
from kiwoom_autotrader.api.account_report import fetch_account_report
from kiwoom_autotrader.api.minute_chart import session_identity
from kiwoom_autotrader.api.session import KST
from kiwoom_autotrader.config.settings import effective_paper_experiment
from .paper_reconciliation import _pending_rows
from .broker_observation_repeats import install_evidence_view


def _prior_days(tracker, today):
    if tracker.path.is_symlink() or not tracker.path.is_file():
        raise ValueError('ledger_unavailable')
    with sqlite3.connect(tracker.path.resolve().as_uri()+'?mode=ro',uri=True,timeout=5) as db:
        db.row_factory=sqlite3.Row;install_evidence_view(db);db.execute('PRAGMA query_only=ON');db.execute('BEGIN')
        rows=tracker._pending_order_rows(db,'paper')
        days=sorted({row['day'] for row in rows if row['day']<today})
        if len(days)>30 or any(not isinstance(day,str) or re.fullmatch(r'[0-9]{8}',day) is None for day in days):
            raise ValueError('prior_day_scope_unavailable')
        return days


def recover_paper_experiment(session, tracker, now, notify=None, *, allow_legacy=False):
    """Attempt one bounded broker reconciliation; no order is ever sent.

    Previous-day order details and the current integrated pending list are
    written as immutable raw observations by the tracker.  A subsequent
    check decides each order; order absence alone does not release it.
    Caller schedules retries and alerts without changing account settings.
    """
    if not isinstance(now,datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError('recovery_clock_unavailable')
    identity=session_identity(session)
    if identity[0]!='paper':return {'status':'not_paper','reconciled':False}
    if allow_legacy is not True and not effective_paper_experiment(tracker.settings_loader(),'paper')['enabled']:
        return {'status':'disabled','reconciled':False}
    day=now.astimezone(KST).strftime('%Y%m%d')
    result={'status':'pending','reconciled':False,'prior_days':[],
            'recorder_recovered':False,'ownership_recovered':False,'orders_sent':0}
    try:
        recorder=getattr(session,'execution_recorder',None)
        checkpoint=None
        if recorder is not None:
            status=recorder.status()
            if not isinstance(status,dict):raise ValueError('recorder_status_unavailable')
            if status.get('error') is not None:
                checkpoint=recorder.begin_paper_recovery()
                if checkpoint is None:raise ValueError('recorder_checkpoint_unavailable')
        captured={'kt00009':[],'ka10075':[]}
        def observe(event):
            tracker.record_broker_observation(event)
            if event.get('source_api') in captured:captured[event['source_api']].append(event)
        days=_prior_days(tracker,day)
        result['prior_days']=days
        for old in sorted(set(days+[day])):
            _request_rows(session,'kt00007',{'ord_dt':old,'qry_tp':'1','stk_bond_tp':'1',
                'sell_tp':'0','stk_cd':'','fr_ord_no':'','dmst_stex_tp':'%' if old==day else 'KRX'},
                'acnt_ord_cntr_prps_dtl',observer=tracker.record_broker_observation,day=old)
            if session_identity(session)!=identity:raise ValueError('session_changed')
        fills=fetch_executions(session,day,observer=observe)
        if session_identity(session)!=identity:raise ValueError('session_changed')
        tracker._ingest('paper',day,fills,source_kind='broker_observation')
        report=fetch_account_report(session)
        if session_identity(session)!=identity:raise ValueError('session_changed')
        observed=tracker._moment()
        tracker._holdings('paper',report,observed,source_kind='broker_observation')
        tracker._save_paper_balance_snapshot('paper',report,observed)
        _pending_rows(session,observer=observe,day=day,integrated=True)
        if session_identity(session)!=identity:raise ValueError('session_changed')
        result['expiry_settlements']=tracker.record_paper_expired_settlements(tracker._moment())
        with tracker._db() as db:
            remaining=tracker._pending_order_rows(db,'paper')
        result.update(status='observations_saved',reconciled=True,remaining_registered_pending=len(remaining),
                      observed_at=tracker._moment().isoformat())
        if all(events for events in captured.values()):
            if any(len({event.get('query_id') for event in events})!=1 for events in captured.values()):
                raise ValueError('recovery_query_identity_unavailable')
            proof={'day':day,'execution_query_id':captured['kt00009'][0]['query_id'],
                   'pending_query_id':captured['ka10075'][0]['query_id'],'account_observed_at':observed.isoformat()}
            result['proof']=proof
            if checkpoint is not None:
                result['recorder_recovered']=recorder.complete_paper_recovery(checkpoint,proof) is True
        elif checkpoint is not None:
            raise ValueError('recovery_query_identity_unavailable')
    except Exception:
        result.update(status='reconciliation_unavailable',reconciled=False)
        if callable(notify):notify('모의 실험 증권사 대사를 확인하지 못해 해당 주문을 계속 격리합니다.')
    return result
