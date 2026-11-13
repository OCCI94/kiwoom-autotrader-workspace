"""Market-hours configuration, full settings display and reset."""

import json

from kiwoom_autotrader.api.session import normalize_renewal_time
from kiwoom_autotrader.config.settings import (
    SettingsError, load_settings, save_settings, reset_settings,
)


def _display(settings: dict, *, notice="") -> list[str]:
    lines = ["⚙️ 현재 설정 (한국 시간)" + (" | "+notice if notice else ""), *json.dumps(settings, ensure_ascii=False, indent=2).splitlines()]
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


def _transition_display(settings, session):
    from kiwoom_autotrader.trading.legacy_resume_gate import session_gate,legacy_loop_description
    gate=session_gate(session)
    state=gate.status(settings)
    targets=legacy_loop_description(settings)
    lines=[]
    if state['experiment_enabled'] and getattr(session,'mode',None)=='paper':
        from kiwoom_autotrader.config.settings import effective_paper_experiment,PAPER_EXPERIMENT_STRATEGIES
        profile=effective_paper_experiment(settings,'paper')
        active=[*PAPER_EXPERIMENT_STRATEGIES]+(['S5_steady_uptrend_vwap'] if profile.get('s5_enabled') else [])
        lines.append('설정 기준 모의 실험 ACTIVE: '+', '.join(active)+'. S2~S4 및 레거시 gdcrs/ddcrs 주문 루프는 PAUSED(설정값은 보존).')
    lines.append(f'실험 off 후 확인하면 재개될 레거시 설정: {targets}. 이는 현재 활성 전략 목록이 아니며 전환 시 명시적 확인이 필요합니다.')
    if state['confirmation_required']:
        token=state['confirmation_token']
        lines.append('레거시 루프 재개 보류. '+(f'확인 명령: stts legacy confirm {token}' if token else '다음 수신기 주기에서 확인 토큰을 생성합니다.'))
    return lines


def handle_settings(text: str, session) -> list[str]:
    words = text.strip().casefold().split()
    try:
        if words == ["stts"]:
            settings=load_settings()
            return _display(settings,notice=" ".join(_transition_display(settings,session)))
        if len(words)==4 and words[:3]==['stts','legacy','confirm']:
            from kiwoom_autotrader.trading.legacy_resume_gate import session_gate
            from kiwoom_autotrader.trading.order_context import _current
            if getattr(session,'mode',None)!='paper' or _current.get() is not None:
                return ['모의 계좌의 명시적 사용자 명령에서만 레거시 재개를 확인할 수 있습니다.']
            settings=load_settings()
            session_gate(session).confirm(settings,words[3])
            return ['레거시 재개를 확인했습니다. 다음 주기부터 설정된 루프만 실행하며 주문 안전 게이트를 유지합니다.',*_transition_display(settings,session)]
        if words == ["stts", "default"]:
            settings = reset_settings()
            session.set_renewal(settings["login_renewal_time"], persist=False)
            return ["♻️ 모든 설정을 기본값으로 되돌렸습니다. 등록된 예약도 삭제했습니다.", *_display(settings,notice=" ".join(_transition_display(settings,session)))]
    except SettingsError as error:
        return [str(error)]
    except (ValueError, OSError) as error:
        return ["설정 또는 레거시 재개 확인 상태를 검증하지 못했습니다. 변경·재개하지 않았습니다."]
    return ["사용법: stts — 모든 설정 확인\nstts default — 모든 설정 초기화(예약 포함)"]
