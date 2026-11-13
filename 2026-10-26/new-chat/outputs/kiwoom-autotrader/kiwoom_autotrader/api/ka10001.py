"""ka10001: domestic stock information request body; caller validates symbol."""

API_ID = "ka10001"
ENDPOINT = "/api/dostk/stkinfo"


def request_body(symbol: str) -> dict[str, str]:
    return {"stk_cd": symbol}
