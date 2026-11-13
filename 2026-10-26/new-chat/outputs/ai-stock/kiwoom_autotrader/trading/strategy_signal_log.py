"""Append-only research observations from the existing receiver, with no requests.

These records are signals/outcomes, never broker fills or executable orders.
Post-close research may attach observed price paths to their immutable hashes.
"""
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re

from kiwoom_autotrader.utils.platform_io import os, fcntl


def record_signal_safely(tracker, event):
    """Research disk failure cannot prevent a protective broker exit."""
    writer = getattr(tracker, 'record_strategy_signal', None)
    if not callable(writer):
        return None
    prior = getattr(tracker, 'research_signal_log_status', {})
    failures = prior.get('failure_count', 0) if isinstance(prior, dict) else 0
    failures = failures if type(failures) is int else 0
    successes = prior.get('success_count', 0) if isinstance(prior, dict) else 0
    successes = successes if type(successes) is int else 0
    try:
        digest = writer(event)
        status = {'ok':True, 'failure_count':failures, 'success_count':successes+1,
                  'last_event_sha256':digest, 'observed_at':event.get('observed_at')}
    except Exception:
        status = {'ok':False, 'failure_count':failures+1, 'success_count':successes,
                  'last_event_sha256':prior.get('last_event_sha256') if isinstance(prior,dict) else None,
                  'observed_at':event.get('observed_at'),
                  'error':'research_signal_append_failed'}
    tracker.research_signal_log_status = status
    return status


def append_signal_event(directory, event):
    if (not isinstance(event, dict) or event.get('mode') != 'paper'
            or event.get('strategy') not in ('gdcrs', 'ddcrs', 'S2_surge_high_breakout',
                'S3_upper_limit_continuation', 'S4_theme_and_catalyst_leader','S5_steady_uptrend_vwap', 'market_observation',
                'EXP_A','EXP_B','EXP_B_TP','EXP_E','EXP_R','EXP_C','EXP_D',
                'EXP_ORB_A','EXP_ORB_B','EXP_GDR','EXP_VSS','EXP_LDR','EXP_ETF_B')
            or not isinstance(event.get('symbol'), str) or re.fullmatch(r'[0-9]{6}', event['symbol']) is None
            or event.get('side') not in ('buy', 'sell', 'observe')
            or event.get('event_kind') not in ('eligible_signal', 'decision_outcome', 'market_observation')):
        raise ValueError('invalid research signal')
    at = datetime.fromisoformat(event['observed_at'])
    if at.tzinfo is None or at.utcoffset() is None:
        raise ValueError('invalid research signal time')
    payload = {**event, 'schema': 'paper-strategy-signal-event-v1', 'research_only': True,
               'broker_fill': False}
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
    if len(raw.encode('utf-8')) > 256 * 1024:
        raise ValueError('research signal is too large')
    digest = hashlib.sha256(raw.encode('utf-8')).hexdigest()
    line = (json.dumps({'event_sha256': digest, 'event': payload}, ensure_ascii=False,
                       sort_keys=True, separators=(',', ':'), allow_nan=False) + '\n').encode('utf-8')
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory / ('strategy-signal-events-' + at.strftime('%Y%m%d') + '.jsonl')
    if path.is_symlink():
        raise ValueError('research signal file is a symlink')
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        view = memoryview(line)
        while view:
            count = os.write(fd, view)
            if count <= 0:
                raise OSError('research signal write incomplete')
            view = view[count:]
        os.fsync(fd)
    finally:
        os.close(fd)
    return digest
