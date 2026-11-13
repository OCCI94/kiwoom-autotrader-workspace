"""usa20100: US quote request body; caller normalizes/validates the exchange."""

API_ID = "usa20100"
ENDPOINT = "/api/us/mrkcond"


def request_body(symbol: str, exchange: str) -> dict[str, str]:
    return {"stex_tp": exchange, "stk_cd": symbol}
