#!/usr/bin/env python3
"""Check whether a deploy of the Kiwoom paper-trading bot is allowed at a KST time.

Rule source: AGENTS.md "배포 검증 계약 (2026-11-03 사용자 지정)".

Kinds:
  repair     source edits and isolated tests only; never touches runtime -> always allowed
  normal     Tue/Thu, before 08:30 or at/after 15:40 KST, trading day, outside the
             official session; still needs user approval and a free claim
             (one normal attempt per Tue/Thu, two per week; failed or unconfirmed
             attempts consume a claim) -- this script cannot see claims
  exception  any time outside 09:00-15:30 on a trading day (any time on a
             non-trading day); requires a one-time token binding source, target
             and time, issued by the user -- this script never issues one

Clock windows (KST): pre-open < 08:30, pre-session-gap 08:30-09:00,
session 09:00-15:30, post-close-gap 15:30-15:40, after-close >= 15:40.

Read-only, standard library only, Python 3.9+. Exit 0 allowed, 1 not allowed,
3 usage/runtime error. Diagnostics go to stderr; `--json` prints JSON on stdout.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Set

RULE_SOURCE = 'AGENTS.md "배포 검증 계약 (2026-11-03 사용자 지정)"'
PRE_OPEN_END = time(8, 30)
SESSION_START = time(9, 0)
SESSION_END = time(15, 30)
AFTER_CLOSE_START = time(15, 40)
NORMAL_WEEKDAYS = (1, 3)  # Tuesday, Thursday (Monday == 0)
WEEKDAY_EN = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
WEEKDAY_KO = ("월", "화", "수", "목", "금", "토", "일")
KINDS = ("normal", "exception", "repair")
AT_FORMATS = ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M")


def kst_zone():
    try:
        from zoneinfo import ZoneInfo  # Python 3.9+

        return ZoneInfo("Asia/Seoul")
    except Exception:  # missing tzdata is the only realistic cause
        print("warning: zoneinfo Asia/Seoul unavailable; using fixed UTC+09:00", file=sys.stderr)
        return timezone(timedelta(hours=9), "KST")


def parse_at(text: Optional[str], tz) -> datetime:
    if text is None:
        return datetime.now(tz).replace(second=0, microsecond=0)
    for fmt in AT_FORMATS:
        try:
            return datetime.strptime(text.strip(), fmt).replace(tzinfo=tz)
        except ValueError:
            continue
    raise ValueError(f"--at must look like 'YYYY-MM-DD HH:MM' (KST), got {text!r}")


def load_holidays(path: Path) -> Set[date]:
    """Accept a JSON array of 'YYYY-MM-DD' strings, or an object with a 'holidays' array."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(raw, dict):
        raw = raw.get("holidays")
    if not isinstance(raw, list):
        raise ValueError("holidays file must be a JSON array of 'YYYY-MM-DD' strings (or {\"holidays\": [...]})")
    out: Set[date] = set()
    for item in raw:
        if not isinstance(item, str):
            raise ValueError(f"holiday entry is not a string: {item!r}")
        out.add(datetime.strptime(item.strip(), "%Y-%m-%d").date())
    return out


def clock_window(t: time) -> str:
    if t < PRE_OPEN_END:
        return "pre-open"
    if t < SESSION_START:
        return "pre-session-gap"
    if t < SESSION_END:
        return "session"
    if t < AFTER_CLOSE_START:
        return "post-close-gap"
    return "after-close"


def is_trading_day(d: date, holidays: Set[date]) -> bool:
    return d.weekday() < 5 and d not in holidays


def fmt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M")


def next_normal_window(after: datetime, holidays: Set[date], tz) -> Optional[Dict[str, str]]:
    """Earliest Tue/Thu normal slot whose start is strictly after `after`."""
    slots = (
        (time(0, 0), PRE_OPEN_END, "pre-open", "08:30 이전(장전; 당일 공식 세션 증거 필요)"),
        (AFTER_CLOSE_START, None, "after-close", "15:40 이후(장후)"),
    )
    for offset in range(0, 70):
        d = after.date() + timedelta(days=offset)
        if d.weekday() not in NORMAL_WEEKDAYS or d in holidays:
            continue
        for start, end, label, ko in slots:
            start_dt = datetime.combine(d, start, tzinfo=tz)
            if start_dt <= after:
                continue
            end_dt = datetime.combine(d, end, tzinfo=tz) if end else datetime.combine(d + timedelta(days=1), time(0, 0), tzinfo=tz)
            return {
                "start": fmt(start_dt),
                "end": fmt(end_dt),
                "window": label,
                "label": f"{d.isoformat()} ({WEEKDAY_KO[d.weekday()]}) {ko}",
            }
    return None


def evaluate(at: datetime, kind: str, holidays: Optional[Set[date]], tz) -> Dict[str, object]:
    hol = holidays or set()
    weekday = at.weekday()
    trading_day = is_trading_day(at.date(), hol)
    window = clock_window(at.time())
    in_session = trading_day and window == "session"
    notes: List[str] = []
    if holidays is None:
        notes.append("휴장일 파일이 없어 평일을 모두 거래일로 가정했다. 공식 휴장일로 다시 확인할 것.")

    if kind == "repair":
        allowed = True
        reason = "소스 수리·격리 테스트는 시간 제한이 없다(runtime·설정·원장·LaunchAgent를 건드리지 않을 때만)."
    elif kind == "normal":
        is_normal_day = weekday in NORMAL_WEEKDAYS
        allowed = trading_day and is_normal_day and window in ("pre-open", "after-close")
        if allowed:
            reason = f"{WEEKDAY_KO[weekday]}요일 {window}: 정상 배포 창 안이다."
            notes.append("사용자 승인이 별도로 필요하다. 화·목 각 정상 시도 1회, 주 최대 2회이며 실패·미확정 시도도 claim을 소비한다. 이 스크립트는 claim 잔량을 모른다.")
            if window == "pre-open":
                notes.append("장전 배포는 신선한 당일 공식 예정 세션 증거가 있어야 한다.")
        elif in_session:
            reason = "거래일 공식 세션(09:00~15:30) 안이다. runtime·설정·원장·LaunchAgent 변경 금지."
        elif not trading_day:
            reason = "거래일이 아니다(주말 또는 휴장일). 정상 배포는 화·목 거래일에만 한다."
        elif not is_normal_day:
            reason = f"{WEEKDAY_KO[weekday]}요일은 정상 배포일이 아니다(화·목만)."
        else:
            reason = f"{WEEKDAY_KO[weekday]}요일이지만 {window} 구간이다. 정상 창은 08:30 이전 또는 15:40 이후."
        if not allowed:
            notes.append("이 밖의 시각에 배포하려면 소스·대상·시각을 묶은 일회성 예외 토큰이 필요하다(--kind exception으로 다시 검사).")
    else:  # exception
        allowed = not in_session
        if allowed:
            reason = "공식 세션 밖이다. 단, 사용자가 발급한 소스·대상·시각 일회성 예외 토큰이 있어야 실행할 수 있다."
            notes.append("예외 토큰은 이 스크립트가 발급하지 않는다. 토큰에 적힌 소스 manifest·대상·시각과 지금 상황이 일치하는지 사람이 확인한다.")
        else:
            reason = "거래일 공식 세션(09:00~15:30) 안이다. 예외 토큰으로도 장중 배포는 허용되지 않는다."
    notes.append("소스 수리·재시작 승인은 코드 배포 승인이 아니다.")

    nxt = next_normal_window(at, hol, tz)
    result: Dict[str, object] = {
        "at": fmt(at),
        "tz": "Asia/Seoul",
        "weekday": WEEKDAY_EN[weekday],
        "trading_day": trading_day,
        "holidays_checked": holidays is not None,
        "in_session": in_session,
        "window": window,
        "kind": kind,
        "allowed": allowed,
        "reason": reason,
        "notes": notes,
        "next_normal_window": nxt["start"] if nxt else None,
        "next_normal_window_end": nxt["end"] if nxt else None,
        "next_normal_window_label": nxt["label"] if nxt else None,
        "rule_source": RULE_SOURCE,
    }
    return result


def print_text(result: Dict[str, object]) -> None:
    order = ("at", "weekday", "trading_day", "holidays_checked", "window", "in_session", "kind", "allowed", "reason")
    for key in order:
        print(f"{key}: {result[key]}")
    if result["next_normal_window"]:
        print(f"next_normal_window: {result['next_normal_window']} ~ {result['next_normal_window_end']} ({result['next_normal_window_label']})")
    for note in result["notes"]:
        print(f"note: {note}")
    print(f"rule_source: {result['rule_source']}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Deploy window check for the Kiwoom paper-trading bot (KST). Read-only.")
    parser.add_argument("--at", help="KST time 'YYYY-MM-DD HH:MM' (default: now)")
    parser.add_argument("--holidays", help="JSON array of 'YYYY-MM-DD' holiday strings; without it, holidays are not checked")
    parser.add_argument("--kind", choices=KINDS, default="normal", help="normal (default) | exception | repair")
    parser.add_argument("--json", action="store_true", help="machine-readable JSON on stdout")
    args = parser.parse_args(argv)

    tz = kst_zone()
    try:
        at = parse_at(args.at, tz)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 3

    holidays: Optional[Set[date]] = None
    if args.holidays:
        path = Path(args.holidays).expanduser()
        try:
            holidays = load_holidays(path)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            print(f"error: cannot read holidays {path}: {exc}", file=sys.stderr)
            return 3
    else:
        print("warning: --holidays not given; holidays were NOT checked (weekdays assumed to be trading days)", file=sys.stderr)

    result = evaluate(at, args.kind, holidays, tz)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print_text(result)
    return 0 if result["allowed"] else 1


if __name__ == "__main__":
    sys.exit(main())
