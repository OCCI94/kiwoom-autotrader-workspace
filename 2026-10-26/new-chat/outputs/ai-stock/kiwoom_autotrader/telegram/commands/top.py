"""top Telegram command handler."""

import re

def build_reply(client, *, ranking_loader) -> str:
    snapshot = ranking_loader(client, limit=20)
    rows = snapshot.get("rows") if isinstance(snapshot, dict) else None
    if not isinstance(rows, list) or len(rows) != 20:
        raise ValueError
    lines = ["코스피·코스닥 일반기업 주식 거래대금 상위 20개", "키움 모의투자 조회 · 단위: 억원"]
    queried_at = snapshot.get("queried_at_kst")
    if isinstance(queried_at, str) and re.fullmatch(
        r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?\+09:00", queried_at,
    ):
        lines.append("조회 시각(KST): " + queried_at[:19].replace("T", " "))
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError
        rank, name = row.get("rank"), row.get("name")
        market, amount = row.get("market"), row.get("turnover_100million_krw")
        if (
            type(rank) is not int or not 1 <= rank <= 20
            or not isinstance(name, str) or not 1 <= len(name) <= 100
            or any(ord(ch) < 32 or ord(ch) == 127 for ch in name)
            or market not in {"코스피", "코스닥"}
            or not isinstance(amount, str)
            or re.fullmatch(r"[0-9]{1,30}(?:\.[0-9]{1,30})?", amount) is None
        ):
            raise ValueError
        lines.append(f"{rank}. {name[:60]} ({market}) · {amount}억원")
    return "\n".join(lines)

