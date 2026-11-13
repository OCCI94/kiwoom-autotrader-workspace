"""ka10099: stock-list request body; caller validates classification markets."""

API_ID = "ka10099"
ENDPOINT = "/api/dostk/stkinfo"


def request_body(market: str) -> dict[str, str]:
    return {"mrkt_tp": market}
