"""Explicit login and renewal-setting commands for the paired private user."""

from kiwoom_autotrader.api.session import LoginSessionError, parse_login_command
from kiwoom_autotrader.config.settings import load_settings, effective_paper_experiment


def handle_login(command, text, session, *, reporter):
    if command in {"login_paper", "login_real"}:
        if command == "login_real":
            try:
                # This is a mode-transition check, not real-mode experiment
                # execution. It must happen before issuing any real login.
                settings = load_settings()
                experiment = effective_paper_experiment(settings, "paper")
            except Exception:
                return "모의 실험 설정을 확인하지 못해 실전 로그인을 보류했습니다."
            if experiment["enabled"]:
                return "모의 실험 모드에서는 login real을 허용하지 않습니다."
            from kiwoom_autotrader.utils.command_scheduler import contains_order_command
            if (settings.get("gdcrs_enabled") is True or settings.get("ddcrs_enabled") is True
                    or any(contains_order_command(row.get("command", ""))
                           for row in settings.get("reservations", []))):
                return "활성 자동매매 또는 주문 예약이 있어 실전 로그인을 거부했습니다. 감시·주문 예약을 먼저 중지하세요."
        result = session.login(command.removeprefix("login_"))
        reporter(result, session)
        return result.message
    if command == "login_renewal":
        try:
            parsed = parse_login_command(text)
            if parsed is None or parsed.action != "renewal":
                raise ValueError
            target = session.set_renewal(parsed.value)
            reply = f"토큰 갱신 시각을 매일 {target}(한국 시간)로 설정했습니다. 로그인 후 적용됩니다."
        except (ValueError, LoginSessionError):
            reply = "토큰 갱신 시각을 변경하지 못했습니다. login renewal 8:55 형식과 설정 파일을 확인하세요."
        print(reply, flush=True)
        return reply
    return "사용법: login paper, login real, login renewal 8:55"
