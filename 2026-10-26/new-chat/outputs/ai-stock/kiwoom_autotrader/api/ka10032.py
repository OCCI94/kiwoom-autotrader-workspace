"""ka10032: turnover-ranking request body; pagination belongs to the client."""

API_ID = "ka10032"
ENDPOINT = "/api/dostk/rkinfo"


def request_body(market: str, include_managed: bool, exchange: str) -> dict[str, str]:
    return {"mrkt_tp": market, "mang_stk_incls": "1" if include_managed else "0", "stex_tp": exchange}
