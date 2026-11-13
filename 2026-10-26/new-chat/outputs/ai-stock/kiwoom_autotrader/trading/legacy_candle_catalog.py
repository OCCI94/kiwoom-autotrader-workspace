"""Documented legacy five-minute pattern criteria; research catalog only.

A numerical description is not the original PaperTrader implementation.
Denominators, edge handling and data timing that the documents leave ambiguous
remain unresolved. This module performs no market lookup and cannot order.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class Criterion:
    description: str
    relation: str
    value: str
    unit: str
    reference: str


@dataclass(frozen=True)
class Pattern:
    identifier: str
    name: str
    source_lines: str
    criteria: tuple[Criterion, ...]
    ambiguities: tuple[str, ...]
    daily_requirements: tuple[str, ...] = ()
    session_window: str | None = None


def C(description, relation, value, unit, reference):
    return Criterion(description, relation, value, unit, reference)


SOURCES = (
    {"name": "AUTOTRADING-ONLY-PLAN-A.md", "url": "https://drive.google.com/file/d/1N8RxSLNeO0JviCYrIiQme1s_IOgn-qwM/view",
     "sha256": "dfb531efb71f1c9d5a21d3e43eb1a4e5689cd485141b50e4b59b7625b0067731"},
    {"name": "AUTOTRADING-ONLY-PLAN-B.md", "url": "https://drive.google.com/file/d/1sPPwYIww9rmL6qOiCUUBx_BNxwA6KliQ/view",
     "sha256": "a3fb858f46f41afc9960a7a0d6acbf8bcb54a095bf0465ee5dc48ef771c6db28"},
)
# Digests are of the locally retrieved Markdown text, not independent
# verification that Google Drive returned the original binary bytes.
SOURCE_COMPARISON = {
    "primary": "AUTOTRADING-ONLY-PLAN-A.md",
    "primary_drive_modified_at_utc": "2026-09-04T06:15:42.044Z",
    "secondary_drive_modified_at_utc": "2026-09-03T07:09:41.591Z",
    "pattern_paragraphs_74_170": "text_identical",
    "other_version_differences": "국내 그림자 손절 설명이 다르고 B에는 별도 오버나잇·탐색형 장이 추가됨",
    "original_binary_verified": False,
}
DAILY_TREND = ("전일까지 일봉 MA5 > MA20", "전일까지 5일 수익률 > 0")
COMMON = ("완료된 5분봉만 사용", "개별 패턴은 OR로 구분", "공통 안전조건은 별도 충족 필요",
          "문서상 그림자 비교전략; 후보 명칭 대응이며 원래 PaperTrader 계산식과 동일 여부 미검증",
          "실제 주문 연결은 미검증")

PATTERNS = (
    Pattern("lower_wick_rebound", "아래꼬리 반등", "74-81", (
        C("최근 완료봉", "=", "4", "봉", "5분봉"),
        C("현재 저점의 하락", ">=", "1", "%", "최근 3봉 고점"),
        C("현재 봉 전체 가격폭", ">=", "0.5", "%", "문서상 분모 불명"),
        C("아래꼬리", ">=", "35", "%", "전체 가격폭"),
        C("양봉 몸통", ">=", "20", "%", "전체 가격폭"),
        C("윗꼬리", "<=", "20", "%", "전체 가격폭"),
        C("종가 위치", "상단 이내", "30", "%", "봉 가격폭"),
        C("저점 반등", ">=", "0.5", "%", "현재 저점; 분모 미확인"),
        C("거래량", ">=", "1.2", "배", "직전 3봉 중앙값"),
    ), ("가격폭·반등률 분모와 3봉 고점의 선택·동률 처리 미확인", "원본 PaperTrader 계산식 미확인")),
    Pattern("consolidation_breakout", "횡보 돌파", "107-113", (
        C("직전 보합 구간", "=", "6", "봉", "완료 5분봉"),
        C("고저 폭", "<=", "2", "%", "중앙 가격"),
        C("첫·마지막 가격 순변화", "within", "-0.7..0.7", "%", "직전 6봉"),
        C("다음 양봉 몸통", ">=", "55", "%", "전체 가격폭"),
        C("종가 위치", "상단 이내", "20", "%", "다음 봉 가격폭"),
        C("거래량", ">=", "1.5", "배", "직전 중앙값"),
        C("종가 상단 돌파", ">=", "0.1", "%", "보합 상단"),
        C("시작 갭 제외", ">", "0.2", "%", "보합 상단"),
        C("추격 제외", ">", "1.5", "%", "보합 상단"),
    ), ("중앙 가격 정의·보합 상단·순변화 가격 선택 미확인", "거래량 중앙값 구간과 비율 분모 미확인")),
    Pattern("decline_rebound", "하락 후 반등", "115-121", (
        C("직전 하락 구간", "=", "6", "봉", "완료 5분봉"),
        C("구간 고점 대비 저점 하락", "within", "2..10", "%", "구간 고점"),
        C("첫 종가 대비 마지막 종가 하락", ">=", "1", "%", "첫 종가"),
        C("마지막 종가와 구간 저점 거리", "<=", "1", "%", "구간 저점; 분모 미확인"),
        C("다음 봉 새 저점 갱신 제한", "<=", "0.5", "%", "기존 구간 저점"),
        C("다음 양봉 몸통", ">=", "45", "%", "전체 가격폭"),
        C("종가 위치", "상단 이내", "25", "%", "다음 봉 가격폭"),
        C("거래량", ">=", "1.5", "배", "문서상 비교 기준 미확인"),
        C("직전 봉 고점 회복", ">=", "0.1", "%", "직전 봉 고점"),
        C("저점 대비 추격 제외", ">", "3", "%", "저점"),
    ), ("거래량 비교 기간·분모 및 저점/갱신률 경계 처리 미확인",)),
    Pattern("steady_uptrend", "꾸준한 상승", "123-131", (
        C("최근 완료봉", "=", "7", "봉", "5분봉"),
        C("양봉 비율", ">=", "70", "%", "7봉"),
        C("종가 상승 전환 비율", ">=", "66", "%", "이전 봉 대비"),
        C("저점 상승 전환 비율", ">=", "66", "%", "이전 봉 대비"),
        C("단기 이동평균", ">", "7", "봉 MA", "3봉 이동평균 > 7봉 이동평균"),
        C("구간 상승률", "within", "1..4", "%", "구간; 기준가 미확인"),
        C("진행 중 종가 기준 최대 되밀림", "<=", "0.8", "%", "고점 기준 상세 미확인"),
        C("최근 3봉 거래량 중앙값 증가", ">=", "10", "%", "최초 3봉 중앙값"),
        C("마지막 양봉 몸통", ">=", "25", "%", "전체 가격폭"),
        C("마지막 윗꼬리", "<=", "30", "%", "전체 가격폭"),
        C("마지막 종가 위치", "상단 이내", "35", "%", "봉 가격폭"),
        C("단일 봉 상승률", "<=", "1.5", "%", "단일 봉; 기준가 미확인"),
    ), ("7봉 중 전환 횟수의 분모·동률·이동평균 입력가 미확인", "구간 상승률·최대 되밀림 분모 미확인"), DAILY_TREND),
    Pattern("first_pullback", "첫 눌림", "133-141", (
        C("선행 상승 완료봉", "=", "5", "봉", "5분봉"),
        C("선행 구간 상승률", "within", "1..4", "%", "선행 상승 구간"),
        C("양봉 비율", ">=", "80", "%", "선행 5봉"),
        C("종가 상승 전환 비율", ">=", "75", "%", "선행 5봉의 이전 봉 대비"),
        C("저점 상승 전환 비율", ">=", "75", "%", "선행 5봉의 이전 봉 대비"),
        C("마지막 양봉", "=", "3", "연속 봉", "선행 구간의 마지막 3봉"),
        C("다음 첫 음봉 종가 조정", "within", "0.2..0.8", "%", "직전 종가"),
        C("상승 구간 되돌림", "<=", "38", "%", "선행 상승 구간"),
        C("음봉 거래량", "<=", "0.9", "배", "선행 상승봉 거래량 중앙값"),
        C("음봉 몸통", "<=", "65", "%", "전체 가격폭"),
        C("종가 위치", ">=", "25", "%", "음봉 저점에서 봉 가격폭 기준"),
        C("최근 저점과 단기 지지선", "유지", "불명", "조건", "가격·산식 미확인"),
    ), ("상승 구간 되돌림 분모와 시작/끝 가격 미확인", "최근 저점·단기 지지선 산식과 정확한 이탈 경계 미확인"), DAILY_TREND),
    Pattern("decline_koma_reversal", "하락 코마 반전", "143-151,197-198", (
        C("선행 하락 완료봉", "=", "5", "봉", "5분봉"),
        C("선행 구간 하락률", "within", "2..10", "%", "선행 하락 구간"),
        C("종가 하락 전환 비율", ">=", "60", "%", "이전 봉 대비"),
        C("저점 보합 완료봉", "=", "3", "봉", "5분봉"),
        C("저점 보합 전체 폭", "<=", "1.2", "%", "중앙 가격"),
        C("저점 보합 순변화", "within", "-0.4..0.4", "%", "보합 구간"),
        C("기존 저점 갱신", "<=", "0.3", "%", "기존 저점"),
        C("보합 거래량 중앙값", "<=", "0.8", "배", "하락 구간 중앙값"),
        C("코마 전체 폭", ">=", "0.3", "%", "코마 시가"),
        C("코마 몸통", "<=", "20", "%", "코마 전체 가격폭"),
        C("코마 위꼬리", ">=", "25", "%", "코마 전체 가격폭"),
        C("코마 아래꼬리", ">=", "25", "%", "코마 전체 가격폭"),
        C("바로 다음 양봉의 고가 돌파", ">=", "0.1", "%", "코마 고가"),
        C("다음 봉 거래량", ">=", "1.3", "배", "보합 거래량"),
        C("돌파 추격 제외", ">", "1.5", "%", "코마 고가"),
        C("긴 윗꼬리 제외", "미확인", "불명", "조건", "정량 경계 없음"),
    ), ("중앙 가격 및 보합 거래량 비교 통계의 엄밀한 산식 미확인", "긴 윗꼬리 제외의 수치 경계 미확인", "보유용 코마 정의가 이 진입 규칙에 동일하게 적용되는지 원본 코드 미확인")),
    Pattern("ascending_pennant", "상승 페넌트", "153-160", (
        C("직전 패턴 완료봉", "=", "6", "봉", "5분봉"),
        C("고가 폭", "<=", "0.6", "%", "저항 중앙값"),
        C("저항 접촉", ">=", "3", "회", "저항선; 허용 오차 미확인"),
        C("저점 상승 비율", ">=", "80", "%", "이전 봉 대비"),
        C("최초 대비 마지막 저점 상승", ">=", "0.8", "%", "최초 저점"),
        C("전체 패턴 폭", "<=", "3", "%", "분모 미확인"),
        C("후반 가격폭", "<=", "0.8", "배", "전반 가격폭"),
        C("후반 거래량", "<=", "0.9", "배", "전반 거래량"),
        C("다음 양봉 몸통", ">=", "45", "%", "전체 가격폭"),
        C("종가 위치", "상단 이내", "25", "%", "다음 봉 가격폭"),
        C("윗꼬리", "<=", "25", "%", "전체 가격폭"),
        C("저항선 돌파", ">=", "0.1", "%", "저항선"),
        C("거래량", ">=", "1.5", "배", "패턴 중앙 거래량"),
        C("시작 갭 제외", ">", "0.2", "%", "저항선"),
        C("추격 제외", ">", "1.5", "%", "저항선"),
    ), ("저항 접촉의 오차·저항선 정의 미확인", "전·후반 구간 분할 및 가격폭/거래량 집계 방식 미확인", "패턴 중앙 거래량·전체 폭의 분모 미확인")),
    Pattern("morning_selloff_doji", "장초 급락 도지", "162-169", (
        C("장중 고점 상승", ">=", "1.5", "%", "전일 종가"),
        C("장중 고점 대비 하락", "within", "2..8", "%", "장중 고점"),
        C("최근 하락 완료봉", "=", "6", "봉", "5분봉"),
        C("최근 구간 연속 하락", ">=", "1.5", "%", "최근 6봉; 기준가 미확인"),
        C("도지 시가·종가 차이", "<=", "0.1", "%", "분모 미확인"),
        C("도지 몸통", "<=", "10", "%", "전체 가격폭"),
        C("아래꼬리", ">=", "30", "%", "전체 가격폭"),
        C("종가 위치", ">=", "35", "%", "도지 저점에서 봉 가격폭 기준"),
        C("도지 거래량", "<=", "0.8", "배", "직전 하락봉 중앙값"),
        C("직전 저점 하향 이탈 제외", ">", "0.3", "%", "직전 저점"),
        C("마지막 종가 추가 하락 제외", ">", "0.3", "%", "비교 기준 미확인"),
    ), ("연속 하락의 연속성·하락률 분모 미확인", "시가·종가 차이 분모와 마지막 종가 비교 기준 미확인"), DAILY_TREND, "10:00~12:30 KST"),
)


def list_patterns():
    """Immutable catalog entries; no conclusion about the original code."""
    return PATTERNS


def evaluate_pattern(identifier, *, data=None):
    """Never infer the missing implementation or generate an actionable signal."""
    if identifier not in {item.identifier for item in PATTERNS}:
        raise ValueError("unknown legacy pattern")
    return {"identifier": identifier, "status": "UNKNOWN", "reason": "original_calculation_unverified",
            "input_evaluated": False, "eligible_for_order": False}
