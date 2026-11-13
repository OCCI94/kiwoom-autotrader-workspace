"""Pure crossover command parsing and ordinary settings persistence only."""

from __future__ import annotations

from dataclasses import dataclass
import re

from kiwoom_autotrader.api.orders import normalize_order_code, normalize_positive_integer
from kiwoom_autotrader.config.settings import SettingsError, load_settings, save_settings


CROSS_USAGE = (
    "사용법: gdcrs intv {단기} {장기} — 각각 1~60 사이 정수\n"
    "gdcrs add {6자리 종목코드} {예산(원)}\n"
    "gdcrs list / gdcrs remove {현재 목록 번호} / gdcrs clear\n"
    "start gdcrs / stop gdcrs — 등록 종목 골든크로스 예산 매수 감시 켜기·끄기\n"
    "start ddcrs / stop ddcrs — 공유 기간으로 데드크로스 감시 켜기·끄기\n"
    "두 감시는 단기<장기일 때 새로운 완료 1분봉 교차를 관측합니다."
)


@dataclass(frozen=True)
class CrossCommand:
    action: str
    strategy: str = "gdcrs"
    short_period: int | None = None
    long_period: int | None = None
    code: str | None = None
    budget: int | None = None
    index: int | None = None


def _period(value: str) -> int:
    if re.fullmatch(r"[0-9]{1,2}", value) is None or not 1 <= int(value) <= 60:
        raise ValueError(CROSS_USAGE)
    return int(value)


def parse_cross_command(text: str) -> CrossCommand | None:
    """Validate syntax without reading settings, authenticating or trading."""
    if not isinstance(text, str):
        return None
    words = text.strip().split()
    if not words:
        return None
    prefix = words[0].casefold()
    if prefix in ("start", "stop"):
        if len(words) < 2 or words[1].casefold() not in ("gdcrs", "ddcrs"):
            return None
        if len(words) != 2:
            raise ValueError(CROSS_USAGE)
        return CrossCommand(prefix, words[1].casefold())
    if prefix not in ("gdcrs", "ddcrs"):
        return None
    if prefix != "gdcrs" or len(words) < 2:
        raise ValueError(CROSS_USAGE)
    action = words[1].casefold()
    try:
        if action == "intv" and len(words) == 4:
            return CrossCommand(action, short_period=_period(words[2]), long_period=_period(words[3]))
        if action == "add" and len(words) == 4:
            return CrossCommand(action, code=normalize_order_code(words[2]),
                                budget=int(normalize_positive_integer(words[3])))
        if action in ("list", "clear") and len(words) == 2:
            return CrossCommand(action)
        if action == "remove" and len(words) == 3:
            return CrossCommand(action, index=int(normalize_positive_integer(words[2])))
    except ValueError:
        raise ValueError(CROSS_USAGE) from None
    raise ValueError(CROSS_USAGE)


def _list_messages(settings: dict) -> list[str]:
    lines = [f"골든크로스 등록 종목 · 단기 {settings['gdcrs_short_period']} / 장기 {settings['gdcrs_long_period']}",
             "골든크로스 감시: " + ("켜짐" if settings["gdcrs_enabled"] else "꺼짐")]
    if not settings["gdcrs_stocks"]:
        lines.append("등록된 종목이 없습니다.")
        if settings["gdcrs_enabled"]:
            lines.append("감시를 켠 상태로 종목 등록을 기다립니다.")
    else:
        lines.extend(f"{index}. {row['code']} · 예산 {row['budget']:,}원" +
                     (f" · 모의 예산 {row['paper_budget']:,}원" if 'paper_budget' in row else '')
                     for index, row in enumerate(settings["gdcrs_stocks"], start=1))
        lines.append("remove 번호는 현재 표시된 등록 순서입니다.")
    lines.append("감시가 켜져 있고 로그인·장 시간·기간 조건을 충족하면 새로운 완료봉 상향 교차에서 등록 예산으로 매수를 시도합니다.")
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


class CrossReplies(list):
    def __init__(self, replies, *, started_ddcrs=False, started_gdcrs=False):
        super().__init__(replies)
        self.started_ddcrs = started_ddcrs
        self.started_gdcrs = started_gdcrs


def handle_cross_command(text: str) -> list[str]:
    try:
        parsed = parse_cross_command(text)
    except ValueError:
        return [CROSS_USAGE]
    if parsed is None:
        return [CROSS_USAGE]
    try:
        settings = load_settings()
        if parsed.action == "list":
            return _list_messages(settings)
        if parsed.action == "intv":
            save_settings({"gdcrs_short_period": parsed.short_period, "gdcrs_long_period": parsed.long_period})
            message = (f"공유 이동평균 기간을 단기 {parsed.short_period} / 장기 {parsed.long_period}로 저장했습니다. "
                       "gdcrs·ddcrs가 같은 기간을 사용합니다.")
            if parsed.short_period >= parsed.long_period:
                message += " 단기가 장기보다 작아야 골든크로스·데드크로스 감시를 실행할 수 있습니다."
            return [message]
        if parsed.action == "add":
            stocks = settings["gdcrs_stocks"]
            if any(row["code"] == parsed.code for row in stocks):
                return ["이미 등록된 종목입니다. 기존 목록과 예산을 유지합니다."]
            if len(stocks) >= 100:
                return ["등록 종목은 최대 100개입니다. 목록에서 종목을 삭제한 뒤 다시 등록하세요."]
            save_settings({"gdcrs_stocks": [*stocks, {"code": parsed.code, "budget": parsed.budget}]})
            state = "감시가 켜져 있어 새 완료봉부터 관측합니다." if settings["gdcrs_enabled"] else "start gdcrs로 감시를 켤 수 있습니다."
            return [f"{len(stocks) + 1}. {parsed.code} · 예산 {parsed.budget:,}원을 등록했습니다. {state} 등록 명령으로 즉시 매수하지 않습니다."]
        if parsed.action == "remove":
            stocks = settings["gdcrs_stocks"]
            if parsed.index > len(stocks):
                return ["현재 목록에 해당 번호가 없습니다. gdcrs list로 번호를 확인하세요."]
            index = parsed.index - 1
            removed = stocks[index]
            save_settings({"gdcrs_stocks": stocks[:index] + stocks[index + 1:]})
            return [f"{parsed.index}. {removed['code']}를 삭제했습니다. 남은 목록 번호는 등록 순서대로 다시 매겨집니다."]
        if parsed.action == "clear":
            save_settings({"gdcrs_stocks": []})
            waiting = " 감시를 켠 상태로 종목 등록을 기다립니다." if settings["gdcrs_enabled"] else ""
            return ["골든크로스 등록 종목을 모두 삭제했습니다." + waiting]
        if parsed.action == "start":
            label = "골든크로스" if parsed.strategy == "gdcrs" else "데드크로스"
            if settings["gdcrs_short_period"] >= settings["gdcrs_long_period"]:
                return [f"{label} 감시는 단기가 장기보다 작아야 시작할 수 있습니다. gdcrs intv {{단기}} {{장기}}로 변경하세요."]
            if parsed.strategy == "gdcrs":
                save_settings({"gdcrs_enabled": True})
                scope = ("등록 종목의 새로운 완료봉 상향 교차에서 예산 매수를 시도합니다."
                         if settings["gdcrs_stocks"] else "등록된 종목이 없어 종목 등록을 기다립니다.")
                return CrossReplies([
                    f"골든크로스 감시를 켰습니다. {scope} 로그인·장 시간 조건을 확인하며, "
                    "첫 관측은 기준만 저장하고 이 시작 명령으로 즉시 매수하지 않습니다."
                ], started_gdcrs=True)
            save_settings({"ddcrs_enabled": True})
            return CrossReplies(["데드크로스 감시를 켰습니다. 로그인·장 시간·공유 이동평균 기간 조건에 따라 보유 종목을 감시하며, 하향 교차 조건이 충족되면 매도 주문을 시도합니다."], started_ddcrs=True)
        save_settings({f"{parsed.strategy}_enabled": False})
        label = "골든크로스" if parsed.strategy == "gdcrs" else "데드크로스"
        return [f"{label} 감시를 껐습니다."]
    except SettingsError as error:
        return [str(error)]
    except Exception:
        return ["교차 전략 설정 처리를 완료하지 못했습니다. stts로 현재 설정을 확인하세요."]
