"""Optional paper re-entry filter based on recorded execution observations.

This check never sends a broker request. With the filter enabled, an unavailable
ledger or an unobserved 00 stream interval is insufficient evidence to buy.
The start is the latest observed sell fill (including a partial fill), not an
order acceptance or an assumed broker fill timestamp.
"""
from datetime import timedelta
import json
import re
import sqlite3
from urllib.parse import quote

from kiwoom_autotrader.api.session import KST


class CooldownUnavailable(ValueError):
    pass


def reentry_allowed(session, symbol, now, minutes):
    if minutes == 0:
        return True
    if (type(minutes) is not int or not 1 <= minutes <= 1440
            or not isinstance(symbol, str) or re.fullmatch(r"[0-9]{6}", symbol) is None
            or now.tzinfo is None or now.utcoffset() is None):
        raise CooldownUnavailable("재진입 대기 조건을 확인하지 못했습니다.")
    now = now.astimezone(KST)
    since = now - timedelta(minutes=minutes)
    gate = getattr(session, "paper_automation_reconcile", None)
    tracker = getattr(gate, "_tracker", None)
    recorder = getattr(session, "execution_recorder", None)
    if getattr(session, "mode", None) != "paper" or tracker is None or recorder is None:
        raise CooldownUnavailable("재진입 대기의 모의 체결 원장·수신 상태를 확인하지 못했습니다.")
    try:
        def checkpoint():
            status = recorder.status()
            counters = ("gap_count", "observations", "ingress_sequence", "engine_ingress_sequence")
            if (not isinstance(status, dict) or status.get("ready") is not True or status.get("error") is not None
                    or any(type(status.get(key)) is not int or status[key] < 0 for key in counters)
                    or type(status.get("inflight_callbacks")) is not int or status["inflight_callbacks"] != 0
                    or type(status.get("engine_pending_callbacks")) is not int or status["engine_pending_callbacks"] != 0):
                raise CooldownUnavailable("재진입 대기 체결 수신이 불완전합니다.")
            return tuple(status[key] for key in counters)
        initial = checkpoint()
        path = tracker.path
        if path.is_symlink() or not path.is_file():
            raise CooldownUnavailable("재진입 대기 체결 원장을 확인하지 못했습니다.")
        scope = tracker.scope
        if not isinstance(scope, str) or re.fullmatch(r"[A-Za-z0-9_-]{1,80}", scope) is None:
            raise CooldownUnavailable("재진입 대기 계좌 범위를 확인하지 못했습니다.")
        uri = "file:" + quote(str(path.resolve()), safe="/") + "?mode=ro"
        with sqlite3.connect(uri, uri=True, timeout=2) as db:
            rows = db.execute("""SELECT observed_at,payload_json FROM broker_observations
                WHERE scope=? AND mode='paper' AND source_api='00' AND day BETWEEN ? AND ?
                ORDER BY id LIMIT 20001""", (scope, since.strftime("%Y%m%d"), now.strftime("%Y%m%d"))).fetchall()
        if checkpoint() != initial:
            raise CooldownUnavailable("대기 기간 확인 중 체결 수신 상태가 바뀌어 매수를 보류합니다.")
        if len(rows) > 20000:
            raise CooldownUnavailable("재진입 대기 체결 관측 범위가 너무 큽니다.")
        covered_since = None
        latest_sell = None
        previous = None
        for stamp, payload in rows:
            from datetime import datetime
            t = datetime.fromisoformat(stamp)
            if t.tzinfo is None or t.utcoffset() is None:
                raise ValueError
            t = t.astimezone(KST)
            if t > now or previous is not None and t < previous:
                raise ValueError
            previous = t
            event = json.loads(payload)
            if (event.get("source_api") != "00" or event.get("source_kind") != "broker_observation"
                    or event.get("mode") != "paper" or event.get("observed_at") != stamp):
                raise ValueError
            kind = event.get("event_kind")
            if kind in ("stream_started", "stream_resumed"):
                covered_since = t
            elif kind in ("stream_gap", "stream_stopped"):
                covered_since = None
            elif kind == "execution_stream":
                values = event.get("values")
                if not isinstance(values, dict):
                    raise ValueError
                # Unknown side on an execution cannot establish absence of a
                # sell; block instead of interpreting it as a buy.
                if values.get("907") not in ("1", "2") and any(values.get(k) for k in ("909", "915", "911")):
                    raise ValueError
                if values.get("907") == "1":
                    code = values.get("9001", "")
                    if re.fullmatch(r"A?[0-9]{6}", code) is None:
                        raise ValueError
                    # 915 is unit quantity; 911 is cumulative fill quantity.
                    # Do not mistake an accepted/unfilled order for a fill.
                    quantities = [values.get(k, "") for k in ("915", "911")]
                    if any(q and re.fullmatch(r"[0-9]{1,15}", q) is None for q in quantities):
                        raise ValueError
                    if code[-6:] == symbol and any(q and int(q) > 0 for q in quantities):
                        latest_sell = t if latest_sell is None else max(latest_sell, t)
            else:
                raise ValueError
        if covered_since is None or covered_since > since:
            raise CooldownUnavailable("재진입 대기 기간의 체결 수신 공백을 확인해 매수를 보류합니다.")
        return latest_sell is None or now - latest_sell >= timedelta(minutes=minutes)
    except CooldownUnavailable:
        raise
    except (OSError, sqlite3.Error, ValueError, TypeError, AttributeError, KeyError, json.JSONDecodeError):
        raise CooldownUnavailable("재진입 대기 체결 관측을 확인하지 못해 매수를 보류합니다.") from None
