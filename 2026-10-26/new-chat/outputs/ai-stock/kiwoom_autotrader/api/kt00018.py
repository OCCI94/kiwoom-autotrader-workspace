"""kt00018 계좌평가잔고내역요청, supplied workbook rows 8–23.

qry_tp: 1 합산, 2 개별. dmst_stex_tp: KRX 한국거래소, NXT 넥스트트레이드.
The report requests the KRX aggregate view and identifies that scope in replies.
"""

API_ID = "kt00018"
ENDPOINT = "/api/dostk/acnt"
HOLDINGS_FIELD = "acnt_evlt_remn_indv_tot"


def request_body(query_type: str = "1", exchange: str = "KRX") -> dict[str, str]:
    if query_type not in {"1", "2"} or exchange not in {"KRX", "NXT"}:
        raise ValueError("잔고 조회구분 또는 국내거래소 구분이 올바르지 않습니다.")
    return {"qry_tp": query_type, "dmst_stex_tp": exchange}
