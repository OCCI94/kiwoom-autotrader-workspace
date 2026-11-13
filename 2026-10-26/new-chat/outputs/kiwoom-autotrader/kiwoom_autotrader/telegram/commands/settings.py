"""Market-hours configuration, full settings display and reset."""

import json

from kiwoom_autotrader.api.session import normalize_renewal_time
from kiwoom_autotrader.config.settings import (
    SettingsError, load_settings, save_settings, reset_settings,
)


def _display(settings: dict) -> list[str]:
    lines = ["⚙️ 현재 설정 (한국 시간)", *json.dumps(settings, ensure_ascii=False, indent=2).splitlines()]
    chunks, current = [], ""
    for line in lines:
        candidate = current + ("\n" if current else "") + line
        if len(candidate.encode("utf-16-le")) // 2 > 3500:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


def handle_market_hours(text: str) -> list[str]:
    words = text.strip().casefold().split()
    if len(words) != 3 or words[0] != "mkhr":
        return ["사용법: mkhr {시작시각} {종료시각}\n예: mkhr 9:30 15:00"]
    try:
        start, end = (normalize_renewal_time(item) for item in words[1:])
        if start >= end:
            raise ValueError
        save_settings({"market_start_time": start, "market_end_time": end})
    except ValueError:
        return ["시작·종료는 00:00~23:59 범위이고 시작 시각이 종료보다 빨라야 합니다."]
    except SettingsError as error:
        return [str(error)]
    return [f"🕘 장 시간을 {start} ~ {end}(한국 시간)로 저장했습니다."]


def handle_settings(text: str, session) -> list[str]:
    words = text.strip().casefold().split()
    try:
        if words == ["stts"]:
            return _display(load_settings())
        if words == ["stts", "default"]:
            settings = reset_settings()
            session.set_renewal(settings["login_renewal_time"], persist=False)
            return ["♻️ 모든 설정을 기본값으로 되돌렸습니다. 등록된 예약도 삭제했습니다.", *_display(settings)]
    except SettingsError as error:
        return [str(error)]
    return ["사용법: stts — 모든 설정 확인\nstts default — 모든 설정 초기화(예약 포함)"]
