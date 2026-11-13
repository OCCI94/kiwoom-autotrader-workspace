"""Bounded local receiver health checks; no credentials, network or orders."""
from datetime import datetime
import json
from kiwoom_autotrader.utils.platform_io import os
from pathlib import Path
import stat
from zoneinfo import ZoneInfo

KST = ZoneInfo('Asia/Seoul')


def maintenance_status(state_dir, *, now=None):
    """Keep a fresh marker fail closed, but never let a stale marker mute forever.

    Legacy empty markers have a ten minute lease from their filesystem mtime.
    A JSON expires_at may shorten that lease, never extend it. The marker is
    retained for audit; expiry is an observation, not a file deletion.
    """
    at = now or datetime.now(KST)
    if not isinstance(at, datetime) or at.tzinfo is None:
        return {'paused': True, 'reason': 'maintenance_clock_unavailable'}
    path = Path(state_dir) / 'maintenance-quiet.lock'
    try:
        info = os.lstat(path)
        if not stat.S_ISREG(info.st_mode) or info.st_size > 4096:
            raise ValueError
        age = at.timestamp() - info.st_mtime
        if age < -5:
            raise ValueError
        expires = info.st_mtime + 600
        if info.st_size:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd, 'rb') as stream:
                opened = os.fstat(stream.fileno())
                if (opened.st_dev, opened.st_ino, opened.st_mtime_ns) != (info.st_dev, info.st_ino, info.st_mtime_ns):
                    raise ValueError
                raw = stream.read(4097)
            try:
                body = json.loads(raw)
                if isinstance(body,dict) and (body.get('schema')=='receiver-reconciliation-maintenance-v1'
                        or body.get('reconciliation_required') is True):
                    # Expiry ends this read lease, never the order prohibition.
                    return {'paused':True,'reason':'maintenance_reconciliation_required',
                        'reconciliation_required':True,'marker_id':body.get('marker_id'),
                        'expires_at':body.get('expires_at')}
                expiry = body.get('expires_at') if isinstance(body, dict) else None
                if expiry is not None:
                    parsed = datetime.fromisoformat(expiry)
                    if parsed.tzinfo is None: raise ValueError
                    expires = min(expires, parsed.timestamp())
            except (ValueError, TypeError, AttributeError):
                if b'receiver-reconciliation-maintenance-v1' in raw or b'reconciliation_required' in raw:
                    return {'paused':True,'reason':'maintenance_reconciliation_marker_unverified','reconciliation_required':True}
                # Existing free-form maintenance notes retain the bounded lease.
                pass
        return {'paused': at.timestamp() < expires,
                'reason': 'maintenance_active' if at.timestamp() < expires else 'maintenance_expired',
                'expires_at': datetime.fromtimestamp(expires, KST).isoformat()}
    except FileNotFoundError:
        return {'paused': False, 'reason': 'maintenance_absent'}
    except (OSError, ValueError, TypeError):
        return {'paused': True, 'reason': 'maintenance_marker_unverified'}


class ErrorReminder:
    """Immediate error transitions and at most one repeat every ten minutes."""
    def __init__(self):
        self.error = None
        self.last_at = None

    def due(self, error, monotonic_now):
        if not error:
            self.error = self.last_at = None
            return False
        due = error != self.error or self.last_at is None or monotonic_now - self.last_at >= 600
        self.error = error
        if due: self.last_at = monotonic_now
        return due


class PreopenStreamCheck:
    """Check a current official session's persisted lifecycle once per minute.

    Missing lifecycle latches the existing recorder recovery path. It never
    clears an error, invents a fill, or issues a broker request itself.
    """
    def __init__(self, calendar=None):
        if calendar is None:
            from kiwoom_autotrader.trading.paper_experiment_calendar import current_session
            calendar = current_session
        self.calendar = calendar
        self.minute = None
        self.result = None
        self.last_notice = None

    def tick(self, session, tracker, now, notify):
        if not isinstance(now, datetime) or now.tzinfo is None: return None
        now = now.astimezone(KST)
        if not '08:50' <= now.strftime('%H:%M') <= '08:59': return self.result
        minute = now.strftime('%Y%m%d%H%M')
        if self.minute == minute: return self.result
        self.minute = minute
        result = {'checked_at': now.isoformat(), 'ready': False, 'reason': 'official_session_unverified'}
        try:
            calendar = self.calendar(now)
            start = calendar['start']
            if (calendar.get('session_day') != now.strftime('%Y%m%d') or start.tzinfo is None
                    or start.astimezone(KST).date() != now.date() or now >= start):
                raise ValueError
            recorder = getattr(session, 'execution_recorder', None)
            stream = recorder.status() if recorder is not None else {}
            result['reason'] = 'stream_not_ready'
            if getattr(session, 'mode', None) == 'paper':
                try:
                    ident, digest, observed = tracker.stream_anchor(now)
                    anchor_at = datetime.fromisoformat(observed)
                    if (type(ident) is not int or ident <= 0 or not isinstance(digest, str) or len(digest) != 64
                            or anchor_at.tzinfo is None or anchor_at.astimezone(KST).date() != now.date()
                            or anchor_at > now): raise ValueError
                    result['lifecycle_observed_at'] = anchor_at.isoformat()
                except Exception:
                    result['reason'] = 'current_day_lifecycle_unverified'
                    latch = getattr(recorder, 'require_paper_recovery', None)
                    if callable(latch): latch()
                else:
                    result.update(ready=stream.get('ready') is True and not stream.get('error'),
                                  reason='ready' if stream.get('ready') is True and not stream.get('error') else 'stream_not_ready')
            else:
                result['reason'] = 'not_paper'
        except Exception:
            pass
        notice = (now.date(), result['reason'])
        if notice != self.last_notice:
            if result['ready']:
                notify('장전 00 스트림 ready 및 당일 원장 시작·재개 기준을 확인했습니다.')
            else:
                notify('장전 00 스트림 점검 보류: ' + result['reason'] + '. 기존 모의 대사·복구 경로로 확인하며 매수 허용을 우회하지 않습니다.')
            self.last_notice = notice
        self.result = result
        return result
