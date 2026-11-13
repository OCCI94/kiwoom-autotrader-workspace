"""rsv command registration, listing and removal; scheduling executes elsewhere."""

import re

from kiwoom_autotrader.utils.command_scheduler import CommandSchedulerError


USAGE = ("예약 명령 사용법\nrsv 8:55 login paper\nrsv 9:00 srch 삼성전자\n"
         "rsv list\nrsv remove {예약번호}\n"
         "한국 시간 기준 평일마다 실행합니다. 지난 시각의 예약은 다음 평일부터 적용됩니다.")


def _parts(lines):
    parts, current = [], ""
    for line in lines:
        candidate = current + ("\n" if current else "") + line
        if len(candidate.encode("utf-8")) > 3900 or len(candidate.encode("utf-16-le")) // 2 > 3900:
            parts.append(current)
            current = line
        else:
            current = candidate
    if current:
        parts.append(current)
    return parts


def handle_reservation(text: str, scheduler) -> list[str]:
    if not isinstance(text, str):
        return [USAGE]
    words = text.strip().split(maxsplit=2)
    if len(words) < 2 or words[0].casefold() != "rsv":
        return [USAGE]
    try:
        action = words[1].casefold()
        if action == "list":
            if len(words) != 2:
                return [USAGE]
            rows = scheduler.list()
            if not rows:
                return ["등록된 예약이 없습니다. rsv 8:55 login paper 형식으로 등록하세요."]
            return _parts(["예약 목록 · 평일 반복 · 한국 시간"] +
                          [f"{row['id']}. {row['time']} — {row['command']}" for row in rows])
        if action == "remove":
            if len(words) != 3 or re.fullmatch(r"[1-9][0-9]{0,11}", words[2]) is None:
                return [USAGE]
            id = int(words[2])
            return [f"예약 {id}번을 삭제했습니다." if scheduler.remove(id) else f"예약 {id}번을 찾지 못했습니다."]
        if len(words) != 3:
            return [USAGE]
        row = scheduler.add(words[1], words[2])
        return [f"예약 {row['id']}번 등록: 평일 {row['time']}(한국 시간)\n{row['command']}\n"
                "등록 시각보다 지난 예약은 다음 평일부터 적용됩니다."]
    except CommandSchedulerError as error:
        return [str(error)]
    except Exception:
        return ["예약 명령을 처리하지 못했습니다. 설정과 예약 상태를 확인하세요."]
