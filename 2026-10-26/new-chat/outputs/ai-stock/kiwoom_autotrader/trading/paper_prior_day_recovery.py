"""Bounded prior-day PAPER residual evidence on an already logged-in session.

A durable phase claim precedes broker reads. Unknown/partial outcomes never
retry in the same phase or resend orders. Existing readers own full pagination,
raw observation provenance and queued_open; the ledger alone decides release.
"""
from datetime import datetime,timedelta
from pathlib import Path
import hashlib,json,os,re,threading
from kiwoom_autotrader.api.executions import _request_rows
from kiwoom_autotrader.api.minute_chart import session_identity
from kiwoom_autotrader.api.session import KST
from kiwoom_autotrader.utils.paths import app_data_root
from .paper_reconciliation import _pending_rows
from .paper_experiment_recovery import _prior_days


def collect_prior_day_pending(session,tracker,now):
    identity=session_identity(session)
    if identity[0]!='paper':raise ValueError('prior_recovery_paper_only')
    today=now.astimezone(KST).strftime('%Y%m%d');days=_prior_days(tracker,today)
    result={'status':'no_prior_pending','pending':False,'prior_days':days,'orders_sent':0,'completed_queries':[]}
    if not days:return result
    for day in days:
        _request_rows(session,'kt00007',{'ord_dt':day,'qry_tp':'1','stk_bond_tp':'1','sell_tp':'0',
            'stk_cd':'','fr_ord_no':'','dmst_stex_tp':'KRX'},'acnt_ord_cntr_prps_dtl',
            observer=tracker.record_broker_observation,day=day)
        if session_identity(session)!=identity:raise ValueError('prior_recovery_session_changed')
        result['completed_queries'].append({'api':'kt00007','day':day})
    _pending_rows(session,observer=tracker.record_broker_observation,day=today,integrated=True)
    if session_identity(session)!=identity:raise ValueError('prior_recovery_session_changed')
    result['completed_queries'].append({'api':'ka10075','day':today})
    result['expiry_settlements']=tracker.record_paper_expired_settlements(tracker._moment())
    with tracker._db() as db:
        remaining=[r for r in tracker._pending_order_rows(db,'paper') if r['day']<today]
    result.update(status='pending' if remaining else 'reconciled',pending=bool(remaining),
        remaining_prior_orders=len(remaining),observed_at=tracker._moment().isoformat())
    return result


class PriorDayPendingScheduler:
    def __init__(self,*,launch=None,claim_dir=None,collector=None):
        self.launch=launch or self._launch;self.collector=collector or collect_prior_day_pending
        self.claim_dir=Path(claim_dir) if claim_dir is not None else app_data_root()/'research-readonly/paper-prior-day-pending-claims'
        self._lock=threading.Lock();self._inflight=False;self._last={'status':'waiting_official_session','pending':None,'orders_sent':0}

    @staticmethod
    def _launch(work):
        threading.Thread(target=work,name='paper-prior-day-evidence',daemon=True).start()

    def status(self):
        with self._lock:return dict(self._last)

    def _write_exclusive(self,path,value):
        if any(p.is_symlink() for p in (path,*path.parents)):raise ValueError('prior_claim_symlink')
        path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os,'O_NOFOLLOW',0),0o600)
        with os.fdopen(fd,'wb') as out:
            out.write((json.dumps(value,sort_keys=True,ensure_ascii=True)+'\n').encode());out.flush();os.fsync(out.fileno())

    def tick(self,session,tracker,now,official_session,notify=None):
        try:identity=session_identity(session)
        except Exception:return {'status':'session_unavailable','pending':True,'orders_sent':0}
        if identity[0]!='paper':return {'status':'not_paper','pending':None,'orders_sent':0}
        try:
            if not isinstance(now,datetime) or now.tzinfo is None:raise ValueError()
            now=now.astimezone(KST);day=now.strftime('%Y%m%d');proof=official_session
            start=proof['start'];end=proof['end'];source=proof['calendar_source_sha256']
            if (not isinstance(start,datetime) or not isinstance(end,datetime) or start.tzinfo is None or end.tzinfo is None
                    or proof.get('session_day')!=day or not start.date()==end.date()==now.date() or not start<end
                    or not isinstance(source,str) or re.fullmatch('[a-f0-9]{64}',source) is None):raise ValueError()
            phase='open' if start<=now<min(start+timedelta(minutes=30),end) else 'postclose' if end<=now<end+timedelta(hours=3) else None
            if phase is None:return {'status':'outside_recovery_phase','pending':None,'orders_sent':0}
        except (ValueError,TypeError,KeyError,AttributeError):
            return {'status':'official_session_unverified','pending':None,'orders_sent':0}
        # A path digest scopes the durable claim to this ledger without exposing
        # an account number or session/token. A new session cannot retry it.
        scope=hashlib.sha256(str(tracker.path.resolve()).encode()).hexdigest()[:24]
        claim=self.claim_dir/(day+'-'+phase+'-'+scope+'.claim.json');result_path=claim.with_suffix('.result.json')
        with self._lock:
            if self._inflight:return dict(self._last)
            body={'schema':'paper-prior-day-recovery-claim-v1','day':day,'phase':phase,'claimed_at':now.isoformat(),
                  'calendar_source_sha256':source,'session_start_at':start.isoformat(),'session_end_at':end.isoformat(),
                  'ledger_scope_sha256':scope,'orders_sent':0}
            try:self._write_exclusive(claim,body)
            except FileExistsError:
                # Do not reissue a phase after process loss. Retain an explicit
                # unknown outcome when no immutable result survived.
                try:
                    if any(p.is_symlink() for p in (result_path,*result_path.parents)) or result_path.stat().st_size>65536:raise ValueError()
                    saved=json.loads(result_path.read_bytes())
                    if saved.get('day')!=day or saved.get('phase')!=phase or saved.get('claim_sha256')!=hashlib.sha256(claim.read_bytes()).hexdigest():raise ValueError()
                    self._last=saved
                except (OSError,ValueError,TypeError,KeyError):
                    self._last={'status':'claimed_outcome_unknown_no_retry','pending':True,'day':day,'phase':phase,'orders_sent':0}
                return dict(self._last)
            except (OSError,ValueError):
                self._last={'status':'claim_unavailable','pending':True,'day':day,'phase':phase,'orders_sent':0};return dict(self._last)
            claim_sha=hashlib.sha256(claim.read_bytes()).hexdigest()
            self._inflight=True;self._last={'status':'in_progress','pending':True,'day':day,'phase':phase,'orders_sent':0}
        def work():
            try:
                if session_identity(session)!=identity:raise ValueError('prior_recovery_session_changed')
                result=self.collector(session,tracker,now)
                if session_identity(session)!=identity:raise ValueError('prior_recovery_session_changed')
            except Exception as error:
                result={'status':'evidence_incomplete_no_retry','pending':True,'orders_sent':0,
                        'reason':'session_changed' if isinstance(error,ValueError) and str(error)=='prior_recovery_session_changed' else 'observation_or_pagination_unverified'}
            value={**result,'schema':'paper-prior-day-recovery-result-v1','day':day,'phase':phase,'claim_sha256':claim_sha}
            try:self._write_exclusive(result_path,value)
            except (OSError,ValueError):value.update(status='result_not_persisted_no_retry',pending=True)
            with self._lock:self._last=value;self._inflight=False
            if value.get('pending') and callable(notify):
                notify('이전 거래일 미체결 잔량 증거가 미확정이어서 해당 주문의 보류를 유지합니다. 같은 단계의 조회는 재전송하지 않습니다.')
        try:self.launch(work)
        except Exception:
            with self._lock:self._inflight=False;self._last={'status':'launch_outcome_unknown_no_retry','pending':True,'day':day,'phase':phase,'orders_sent':0}
        return self.status()
