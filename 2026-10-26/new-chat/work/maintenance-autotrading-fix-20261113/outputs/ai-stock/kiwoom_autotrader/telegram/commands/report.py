"""Present current domestic account funds and holdings as private plain text."""

from __future__ import annotations

from kiwoom_autotrader.api.account_report import AccountReportError, fetch_account_report


def _number(value: str | None, unit: str = "원") -> str:
    if value is None:
        return "미제공"
    sign = value[0] if value[0] in "+-" else ""
    digits = value[1:] if sign else value
    integer, dot, fraction = digits.partition(".")
    return f"{sign}{int(integer):,}{dot}{fraction}{unit}"


def _units(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def _chunks(blocks: list[str], limit: int = 3900) -> list[str]:
    """Keep blocks together where possible, bounded in Unicode and UTF-16."""
    chunks, current = [], ""
    for block in blocks:
        parts, piece, size = [], "", 0
        for char in block:
            units = 2 if ord(char) > 0xFFFF else 1
            if size + units > limit:
                parts.append(piece)
                piece, size = "", 0
            piece += char
            size += units
        if piece:
            parts.append(piece)
        for part in parts:
            candidate = current + ("\n\n" if current else "") + part
            if _units(candidate) > limit:
                if current:
                    chunks.append(current)
                current = part
            else:
                current = candidate
    if current:
        chunks.append(current)
    return chunks


def format_account_report(data: dict) -> list[str]:
    mode = {"paper": "모의투자", "real": "실전투자"}[data["mode"]]
    cash, totals, holdings = data["cash"], data["totals"], data["holdings"]
    cash_number = lambda key: _number(cash.get(key))
    total_number = lambda key: _number(totals.get(key), "%" if key == "tot_prft_rt" else "원")
    blocks = [f"📊 계좌 보고서 · {mode}\n국내 계좌 · KRX 기준",
              "💰 자금 현황 (추정조회)\n"
              f"예수금: {cash_number('entr')}\n"
              f"출금가능금액: {cash_number('pymn_alow_amt')}\n"
              f"주문가능금액: {cash_number('ord_alow_amt')}\n"
              f"D+1 추정예수금: {cash_number('d1_entra')}\n"
              f"D+2 추정예수금: {cash_number('d2_entra')}",
              "📈 평가 현황\n"
              f"총매입금액: {total_number('tot_pur_amt')}\n"
              f"총평가금액: {total_number('tot_evlt_amt')}\n"
              f"총평가손익: {total_number('tot_evlt_pl')} ({total_number('tot_prft_rt')})\n"
              f"추정예탁자산: {total_number('prsm_dpst_aset_amt')}\n"
              f"총대출금: {total_number('tot_loan_amt')}"]
    if holdings is None:
        blocks.append("📋 보유 내역: 미제공\n보유 내역 전체를 확인하지 못했습니다.")
    elif not holdings:
        blocks.append("📋 보유종목 없음")
    else:
        blocks.append(f"📋 보유 내역 {len(holdings)}건")
        for index, row in enumerate(holdings, 1):
            n = lambda key, unit="원": _number(row.get(key), unit)
            kind = {"00": "현금", "03": "자기융자"}.get(row.get("crd_tp"))
            if kind is None:
                kind = "신용구분 " + (row.get("crd_tp") or "미제공")
            if row.get("crd_loan_dt"):
                date = row["crd_loan_dt"]
                kind += f" · 대출일 {date[:4]}-{date[4:6]}-{date[6:]}"
            blocks.append(
                f"{index}. {row.get('stk_nm') or '종목명 미제공'} ({row.get('stk_cd') or '코드 미제공'})\n"
                f"{kind}\n보유 {n('rmnd_qty', '주')} · 매매가능 {n('trde_able_qty', '주')}\n"
                f"매입가 {n('pur_pric')} · 현재가 {n('cur_prc')}\n"
                f"매입금액 {n('pur_amt')} · 평가금액 {n('evlt_amt')}\n"
                f"평가손익 {n('evltv_prft')} ({n('prft_rt', '%')})"
            )
    if data.get("snapshot_changed"):
        blocks.append("연속 조회 중 합계가 변경되었습니다. 합계는 최초 제공값이며 다시 조회하면 갱신됩니다.")
    blocks.append("금액·수익률은 API 제공값입니다. 여러 요청을 순서대로 조회하므로 하나의 동시점 잔고는 아닙니다.")
    return _chunks(blocks)


def handle_report(session) -> list[str]:
    """Dispatch report/r; never expose remote descriptions or credentials."""
    try:
        return format_account_report(fetch_account_report(session))
    except AccountReportError as error:
        return [str(error)]
    except Exception:
        return ["계좌 보고서를 표시하지 못했습니다. 로그인 상태를 확인한 뒤 다시 시도하세요."]
