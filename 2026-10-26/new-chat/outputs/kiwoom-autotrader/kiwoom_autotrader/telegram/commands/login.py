"""Explicit login and renewal-setting commands for the paired private user."""

from kiwoom_autotrader.api.session import LoginSessionError, parse_login_command


def handle_login(command, text, session, *, reporter):
    if command in {"login_paper", "login_real"}:
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
