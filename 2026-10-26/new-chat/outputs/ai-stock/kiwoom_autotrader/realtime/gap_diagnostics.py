"""Bounded transport diagnostics; never persist exception text or payloads."""
from datetime import datetime, time
from zoneinfo import ZoneInfo

KST = ZoneInfo('Asia/Seoul')
TRIGGERS = frozenset({'unknown', 'ws_close_frame', 'transport_error', 'protocol_error',
    'ack_timeout', 'authentication_rejected', 'session_changed', 'subscription_changed',
    'callback_error', 'callback_dropped', 'callback_error_and_dropped', 'shutdown_uncertain'})
PHASES = frozenset({'regular_hours', 'extended_hours', 'post_close_idle'})


def session_phase(moment):
    """Clock bucket for diagnosis, not an exchange session/holiday attestation."""
    if not isinstance(moment, datetime) or moment.tzinfo is None:
        raise ValueError('aware diagnostic time required')
    local = moment.astimezone(KST)
    t = local.time()
    if local.weekday() < 5 and time(9) <= t < time(15, 30):
        return 'regular_hours'
    if local.weekday() < 5 and (time(8) <= t < time(9) or time(15, 30) <= t < time(20)):
        return 'extended_hours'
    return 'post_close_idle'


def close_code(error):
    # A received close frame is stronger evidence than the deprecated .code
    # fallback (which may be the synthetic 1006 abnormal-closure indicator).
    try:
        frame = getattr(error, 'rcvd', None)
        value = getattr(frame, 'code', None) if frame is not None else getattr(error, 'code', None)
        return value if type(value) is int and 1000 <= value <= 4999 else None
    except Exception:
        return None


def classify_error(error):
    code = close_code(error)
    if code == 1006:
        return 'transport_error'
    if code is not None:
        return 'ws_close_frame'
    if isinstance(error, TimeoutError):
        return 'ack_timeout'
    if isinstance(error, OSError):
        return 'transport_error'
    if isinstance(error, (ValueError, UnicodeError)):
        return 'protocol_error'
    return 'unknown'
