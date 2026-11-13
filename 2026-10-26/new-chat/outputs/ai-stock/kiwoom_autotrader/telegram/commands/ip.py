"""Reply text only; the authorized listener owns Telegram delivery."""

from kiwoom_autotrader.utils.public_ip import format_public_ip, get_public_ip


def handle_ip() -> list[str]:
    try:
        address = get_public_ip(force_refresh=True)
        return [format_public_ip(address) + "\n이 PC가 IP 확인 서비스에 접속할 때 사용한 주소입니다."]
    except Exception:
        return ["현재 외부 공인 IP를 확인하지 못했습니다. 잠시 후 IP 명령으로 다시 확인하세요."]
