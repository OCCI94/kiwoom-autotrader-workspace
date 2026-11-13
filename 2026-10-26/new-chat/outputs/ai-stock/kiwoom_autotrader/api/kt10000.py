"""kt10000 buy request fields from the supplied workbook, rows 22–27, 36."""

API_ID = "kt10000"
ENDPOINT = "/api/dostk/ordr"


def request_body(symbol: str, quantity: str, price: str | None = None) -> dict[str, str]:
    """The caller validates six-digit KRX code and positive integer values."""
    return {"dmst_stex_tp": "KRX", "stk_cd": symbol, "ord_qty": quantity,
            "ord_uv": price if price is not None else "",
            "trde_tp": "0" if price is not None else "3", "cond_uv": ""}
