"""Bounded local DART-list evidence for LDR, independent of S5 supply filters."""
from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path
import re
from kiwoom_autotrader.api.session import KST
from .catalyst_pre_scoring import _read_regular, _ReadonlySSDGuard, MOUNT


def _unique(pairs):
    result={}
    for key,value in pairs:
        if key in result:raise ValueError('duplicate_key')
        result[key]=value
    return result


def _time(value):
    if not isinstance(value,str):raise ValueError('time')
    result=datetime.fromisoformat(value)
    if result.tzinfo is None or result.utcoffset() is None:raise ValueError('timezone')
    return result


def read_disclosure_clearance(path,symbol,now,*,archive_root=None,guard_factory=None):
    """Return PASS proof or None. This does not assert original-document review."""
    try:
        if (not isinstance(symbol,str) or re.fullmatch(r'[0-9]{6}',symbol) is None
                or not isinstance(now,datetime) or now.tzinfo is None or now.utcoffset() is None):return None
        raw=_read_regular(Path(path),limit=256*1024)
        doc=json.loads(raw,object_pairs_hook=_unique)
        if (doc.get('schema')!='toss-hybrid-risk-evidence-v1' or not _time(doc['generated_at'])<=now<_time(doc['expires_at'])
                or _time(doc['expires_at'])-_time(doc['generated_at'])>timedelta(hours=24)):return None
        evaluations=doc['evaluations']
        if not isinstance(evaluations,list) or len(evaluations)>1200:return None
        matches=[row for row in evaluations if isinstance(row,dict) and row.get('symbol')==symbol]
        if len(matches)!=1:return None
        decision=matches[0];disclosure=decision.get('disclosure')
        if not isinstance(disclosure,dict) or disclosure.get('status')!='PASS':return None
        proofs=decision.get('dart_list_proofs')
        if not isinstance(proofs,list) or not 1<=len(proofs)<=2:return None
        root=Path(archive_root) if archive_root is not None else MOUNT/'ai-stock-research-data'/'s5-full-eligibility'
        guard=(guard_factory or _ReadonlySSDGuard)()
        seen=set();total=None;pages=None;observed=[]
        with guard:
            for number,proof in enumerate(proofs,1):
                at=_time(proof['observed_at'])
                if (at.astimezone(KST).date()!=now.astimezone(KST).date()
                        or not timedelta(0)<=now-at<=timedelta(minutes=20)):return None
                expected=proof['sha256'];p=Path(proof['path'])
                if (not isinstance(expected,str) or re.fullmatch(r'[0-9a-f]{64}',expected) is None
                        or not p.is_absolute() or p.name!=f'{symbol}-dart-{number}.json'
                        or not p.is_relative_to(root) or p.resolve(strict=True)!=p):return None
                body=json.loads(_read_regular(p,limit=2*1024*1024,expected_sha256=expected),object_pairs_hook=_unique)
                if body.get('status')=='013' and number==1 and len(proofs)==1:
                    count,n_pages,items=0,1,[]
                elif body.get('status')=='000':
                    count=int(body['total_count']);n_pages=int(body['total_page']);items=body['list']
                    if (not 1<=count<=200 or n_pages!=(count+99)//100 or int(body['page_no'])!=number
                            or int(body['page_count'])!=100 or not isinstance(items,list)
                            or len(items)!=min(100,count-100*(number-1))):return None
                else:return None
                if total is not None and (total,pages)!=(count,n_pages):return None
                total,pages=count,n_pages
                for item in items:
                    receipt=item.get('rcept_no') if isinstance(item,dict) else None
                    if (item.get('stock_code')!=symbol or not isinstance(receipt,str)
                            or re.fullmatch(r'[0-9]{14}',receipt) is None or receipt in seen
                            or not isinstance(item.get('rcept_dt'),str) or item['rcept_dt']>now.strftime('%Y%m%d')):return None
                    seen.add(receipt)
                observed.append(at)
            if pages!=len(proofs) or total!=len(seen):return None
            verify=getattr(guard,'verify',None)
            if callable(verify):verify()
        return {'schema':'ldr-disclosure-list-clearance-v1','symbol':symbol,
                'reviewed_at':max(observed).isoformat(),'coverage_complete':True,
                'oldest_list_age_seconds':max((now-at).total_seconds() for at in observed),
                'unresolved_major_risk':False,'source_sha256':hashlib.sha256(raw).hexdigest(),
                'list_proof_sha256':[p['sha256'] for p in proofs],
                'review_method':'published_disclosure_decision_and_complete_list_only',
                'original_documents_reviewed':None,'risk_zero_guaranteed':False}
    except (OSError,ValueError,TypeError,KeyError,OverflowError,AttributeError):return None


def read_s5_risk_diagnostics(path,symbol,now):
    """Explain a held/absent context without granting or relaxing eligibility.

    The published S5 input contains rows for eligible symbols only. Its separate
    evaluations preserve the actual DART/short/ADR reason for held symbols; a
    missing row must not be mislabeled as a DART transport failure.
    """
    result={'schema':'s5-published-risk-diagnostic-v1','symbol':symbol,
        'status':'UNAVAILABLE','reason':'s5_risk_evidence_unavailable','reasons':[],
        'diagnostic_only':True,'order_permission':False}
    try:
        if not isinstance(symbol,str) or re.fullmatch('[0-9]{6}',symbol) is None: return result
        raw=_read_regular(Path(path),limit=256*1024)
        doc=json.loads(raw,object_pairs_hook=_unique)
        if doc.get('schema')!='toss-hybrid-risk-evidence-v1':return result
        generated,expires=_time(doc['generated_at']),_time(doc['expires_at'])
        if (not generated<=now<expires or generated.date()!=now.date()
                or expires-generated>timedelta(hours=24)):
            return {**result,'reason':'s5_risk_evidence_expired_or_future'}
        evaluations=doc.get('evaluations')
        if not isinstance(evaluations,list) or len(evaluations)>1200:return result
        seen=set();found=None
        for item in evaluations:
            code=item.get('symbol') if isinstance(item,dict) else None
            if not isinstance(code,str) or re.fullmatch('[0-9]{6}',code) is None or code in seen:return result
            seen.add(code)
            if code==symbol:found=item
        if found is None:return {**result,'reason':'s5_risk_symbol_not_in_verified_scope'}
        reasons=found.get('reasons')
        if (found.get('status') not in ('ELIGIBLE','HOLD') or not isinstance(reasons,list)
                or any(not isinstance(v,str) or re.fullmatch('[A-Z0-9_]{1,120}',v) is None for v in reasons)):
            return result
        mapping={'PRIOR_UNRESOLVED_MAJOR_RISK':'s5_unresolved_major_disclosure_risk',
            'UNRESOLVED_DISCLOSURE_RISK':'s5_unresolved_major_disclosure_risk',
            'SHORT_SELLING_FIVE_DAY_MAX_OVER_10_PERCENT':'s5_short_selling_pressure_high',
            'ADR_OVERHEATED':'s5_adr_overheated','ADR_AT_OR_ABOVE_6':'s5_adr_overheated',
            'DART_LIST_NOT_VERIFIED':'s5_disclosure_list_unverified',
            'ORIGINAL_DISCLOSURE_REVIEW_REQUIRED':'s5_original_disclosure_review_required',
            'TITLE_CLASSIFICATION_UNKNOWN':'s5_disclosure_classification_unknown',
            'TOSS_SUPPLY_OR_ADR_UNVERIFIED':'s5_supply_or_adr_unverified',
            'SHORT_SELLING_FIVE_SESSIONS_UNVERIFIED':'s5_short_selling_coverage_unverified'}
        disclosed=found.get('disclosure') or {}
        diagnostic_reasons=[mapping.get(v,'s5_risk_hold_'+v.lower()) for v in reasons]
        return {**result,'status':found['status'],
            'reason':diagnostic_reasons[0] if diagnostic_reasons else 's5_risk_context_unavailable_or_inconsistent',
            'reasons':diagnostic_reasons,'published_reason_codes':list(reasons),
            'disclosure_status':disclosed.get('status'),
            'disclosure_reason_codes':disclosed.get('reasons'),
            'short_max_share':found.get('short_max_share'),
            'adr_extension_multiple':found.get('adr_extension_multiple'),
            'generated_at':generated.isoformat(),'expires_at':expires.isoformat(),
            'file_sha256':hashlib.sha256(raw).hexdigest(),
            'held_symbols_need_not_have_eligible_rows':True}
    except (OSError,ValueError,TypeError,KeyError,OverflowError,AttributeError):return result
