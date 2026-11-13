"""Explicit paper experiment -> legacy-loop transition, durable across restart.

This audit marker contains no credentials and changes no strategy settings.
A malformed marker fails closed; confirmations are bound to the exact settings.
"""
import hashlib
import inspect
import json
from kiwoom_autotrader.utils.platform_io import os, sync_directory
from pathlib import Path
import tempfile
import uuid
from kiwoom_autotrader.utils.paths import app_data_root


def digest(settings):
    return hashlib.sha256(json.dumps(settings,sort_keys=True,separators=(',',':'),ensure_ascii=True).encode()).hexdigest()


def legacy_loops(settings):
    rows=[]
    if settings.get('gdcrs_enabled') is True:rows.append('gdcrs')
    if settings.get('ddcrs_enabled') is True:rows.append('ddcrs')
    if settings.get('paper_multi_strategy_enabled') is True:
        rows.extend(settings.get('paper_multi_strategy_ids',[]))
    return rows


def legacy_loop_description(settings):
    rows=legacy_loops(settings)
    if 'gdcrs' in rows:
        stocks=settings.get('gdcrs_stocks',[])
        count=len(stocks) if isinstance(stocks,list) else '미확정'
        cap=settings.get('paper_automation_buy_cap',1_000_000)
        if type(cap) is int and settings.get('paper_multi_strategy_enabled') is True:cap=min(cap,1_000_000)
        cap_text=f'{cap:,}원' if type(cap) is int and cap>0 else '미확정'
        rows[rows.index('gdcrs')]=f'gdcrs(등록 {count}종목, 건당 원금 상한 {cap_text})'
    return ', '.join(rows) or '활성 설정 없음'


class LegacyResumeGate:
    def __init__(self,path=None):
        self.path=Path(path) if path is not None else app_data_root()/'telegram/experiment-legacy-resume.json'

    def _read(self):
        if self.path.is_symlink():raise ValueError('legacy_resume_marker_unsafe')
        if not self.path.exists():return {'schema':1,'experiment_seen':False}
        value=json.loads(self.path.read_text())
        if not isinstance(value,dict) or value.get('schema')!=1 or type(value.get('experiment_seen')) is not bool:
            raise ValueError('legacy_resume_marker_invalid')
        return value

    def _write(self,value):
        self.path.parent.mkdir(parents=True,exist_ok=True)
        if self.path.is_symlink():raise ValueError('legacy_resume_marker_unsafe')
        fd,name=tempfile.mkstemp(prefix='.legacy-resume-',dir=self.path.parent)
        try:
            with os.fdopen(fd,'w') as stream:
                os.fchmod(stream.fileno(),0o600)
                json.dump(value,stream,sort_keys=True);stream.flush();os.fsync(stream.fileno())
            os.replace(name,self.path)
            sync_directory(self.path.parent)
        finally:
            if os.path.exists(name):os.unlink(name)

    def observe(self,settings):
        value=self._read()
        if settings.get('paper_experiment_enabled') is True:
            if not value.get('experiment_seen') or value.get('confirmed_digest') or value.get('pending_digest'):
                self._write({'schema':1,'experiment_seen':True})
            return False
        if not value['experiment_seen']:return True
        current=digest(settings)
        if value.get('confirmed_digest')==current:return True
        if value.get('pending_digest')!=current:
            self._write({'schema':1,'experiment_seen':True,'pending_digest':current,'confirmation_token':uuid.uuid4().hex[:12]})
        return False

    def status(self,settings):
        value=self._read();active=settings.get('paper_experiment_enabled') is True
        required=not active and value['experiment_seen'] and value.get('confirmed_digest')!=digest(settings)
        return {'experiment_enabled':active,'confirmation_required':required,
                'legacy_resume_targets':legacy_loops(settings),
                'confirmation_token':value.get('confirmation_token') if value.get('pending_digest')==digest(settings) else None}

    def confirm(self,settings,token):
        if settings.get('paper_experiment_enabled') is True:raise ValueError('experiment_still_enabled')
        value=self._read();current=digest(settings)
        if (not value['experiment_seen'] or value.get('pending_digest')!=current
                or not isinstance(token,str) or value.get('confirmation_token')!=token):
            raise ValueError('legacy_confirmation_stale_or_missing')
        self._write({'schema':1,'experiment_seen':True,'confirmed_digest':current,
                     'confirmation':'explicit_paired_telegram_user'})


def session_gate(session):
    """Resolve only an explicit session attribute, never dynamic __getattr__.

    Existing sessions without the optional integration use the durable default.
    An explicitly attached unknown guard is rejected, not treated as approval.
    """
    try:
        value=inspect.getattr_static(session,'legacy_resume_gate')
    except AttributeError:
        return LegacyResumeGate()
    if not isinstance(value,LegacyResumeGate):
        raise ValueError('legacy_resume_gate_invalid')
    return value
