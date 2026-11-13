"""status Telegram command handler."""

from kiwoom_autotrader.api.client import KiwoomAPIError, KiwoomMockClient

def build_reply(client: KiwoomMockClient) -> str:
    lines = ["키움 모의투자 연결 상태", "인증: 정상"]
    for market, symbol, exchange, label in (
        ("KR", "005930", "KRX", "국내 삼성전자(005930)"),
        ("US", "AAPL", "NASDAQ", "미국 애플(AAPL)"),
    ):
        try:
            client.quote(market, symbol, exchange)
            lines.append(f"{label}: 조회 성공")
        except (KiwoomAPIError, ValueError, TypeError):
            lines.append(f"{label}: 조회 실패")
    lines.append("현재 기능: 모의투자 인증·시세 조회")
    return "\n".join(lines)

