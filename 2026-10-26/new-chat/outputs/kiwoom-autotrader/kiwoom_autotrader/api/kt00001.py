"""kt00001 예수금상세현황요청, supplied workbook rows 8–22.

qry_tp: 3 추정조회, 2 일반조회. The report chooses the documented example's
estimated view so its D+1/D+2 balances can be shown with their explicit labels.
"""

API_ID = "kt00001"
ENDPOINT = "/api/dostk/acnt"


def request_body(query_type: str = "3") -> dict[str, str]:
    if query_type not in {"2", "3"}:
        raise ValueError("예수금 조회구분은 2 또는 3이어야 합니다.")
    return {"qry_tp": query_type}
