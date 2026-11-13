"""kt10001 market sell request fields from the supplied workbook, rows 22–27, 36."""

API_ID = "kt10001"
ENDPOINT = "/api/dostk/ordr"


def request_body(symbol: str, quantity: str) -> dict[str, str]:
    """The caller validates the six-digit KRX code and positive quantity."""
    return {"dmst_stex_tp": "KRX", "stk_cd": symbol, "ord_qty": quantity,
            "ord_uv": "", "trde_tp": "3", "cond_uv": ""}
