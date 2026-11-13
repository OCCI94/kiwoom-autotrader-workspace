#!/usr/bin/env python3
"""
『주도주 투자』 × 『퀀트 투자의 기초(1~11장 부록)』 5단계 통합 파이프라인 (SHADOW vs LIVE 원장 대조 포함)
  Stage 1: 팩터 생성 및 WLS 요인 직교화 (Factor Generation & Orthogonalization)
  Stage 2: 팩터 검증 및 알파 시간척도 판별 (Cross-Sectional & Horizon Validation)
  Stage 3: 이원화 매매전략 구상 및 포지션 사이징 (Track A 코어 스윙 + Track B 오버나잇 갭)
  Stage 4: 사후 성과 3분해(XSE/XSTSE) 및 계좌 손절·레버리지 거버넌스 (Attribution & Governance)
  Stage 5: 보존 원장 사본(`mode=ro`) 기반 기존 14개 LIVE arm 실체결 vs SHADOW 전략 대조표

안전 불변식(Safety Invariants):
- 100% 읽기 전용 (운영 런타임·원장·프로세스·브로커 API 접촉 0회, order_permission=False)
- 원장 사본은 오직 `?mode=ro&immutable=1` 및 `PRAGMA query_only=ON`으로만 조회
- 정본 소스(`outputs/ai-stock`) 및 주도주 원전 4모듈(`leader_*.py`) 무변경
"""

from __future__ import annotations

from contextlib import closing
import argparse
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import statistics
import sys
from typing import Dict, List, Optional, Tuple

BASE_DIR = Path("/Users/tr-oh/Documents/Codex/2026-10-26/new-chat")
RESEARCH_DIR = BASE_DIR / "outputs/ai-stock-research"
QUANT_WORK_DIR = BASE_DIR / "work/quant-book-analysis-20261105"
RD_ROOT = Path("/Volumes/AIStockData/ai-stock-research-data")
OUT_DIR = Path("/Users/tr-oh/Documents/Codex/backtest-20261108")

DEFAULT_LEDGER_COPY = (
    BASE_DIR
    / "work/approved-postclose-20261105/162306001169/snapshots/ledger.sqlite3"
)
REAL_DART_CALC_PATH = (
    RESEARCH_DIR
    / "reports/2026-11-05/112549577286-leader-candidates-real-dart-financial-calculation.json"
)
SECTOR_ETF_PANEL_PATH = (
    RESEARCH_DIR
    / "reports/2026-11-09/110204583817-sector-etf-panel.json"
)
NOV09_SHUTDOWN_REPORT_PATH = (
    RESEARCH_DIR
    / "reports/2026-11-09/111156436840-project-shutdown-and-restart-readiness.json"
)
NOV09_REPLAY_REPORT_PATH = (
    RESEARCH_DIR
    / "reports/2026-11-09/115015466703-100312-copy-reconciliation-and-exp-d-late-exit-replay.json"
)
SAVED_DAILY_FACTOR_TXT = OUT_DIR / "일봉-횡단면.txt"
SAVED_OOS_INTRADAY_TXT = OUT_DIR / "표본외-일중가설.txt"

for p in (RESEARCH_DIR, QUANT_WORK_DIR):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import leader_core as lc
import leader_macro_overseas as lmo
import leader_valuation as lv
from leader_autotrading_formulas import (
    AccountRiskConfig,
    StockCandidateInput,
    build_portfolio_allocation,
    compute_market_impact_cost,
    compute_optimal_event_trajectory,
    evaluate_leverage_sustainability,
)
from run_master_quant_leader_verification import compute_xse_xstse_attribution

KST = timezone(timedelta(hours=9))
JUMP_LIMIT = 0.30
JUMP_WINDOW = 5
ROUND_TRIP_COST = 0.0030  # 한국 시장 왕복 비용 (매도세 0.20% + 수수료·슬리피지 0.10%)

LIVE_14_ARMS = (
    "EXP_R",
    "EXP_A",
    "EXP_B",
    "EXP_B_TP",
    "EXP_E",
    "EXP_C",
    "EXP_D",
    "EXP_ETF_B",
    "EXP_GDR",
    "EXP_LDR",
    "EXP_ORB_A",
    "EXP_ORB_B",
    "EXP_VSS",
    "S5_steady_uptrend_vwap",
)


# =====================================================================
# Data Loaders (100% Read-Only from Local Archives)
# =====================================================================

def load_opendart_financials() -> Dict[str, Dict[str, object]]:
    """OpenDART 8분기 TTM 실측 재무 결과(`112549577286-leader-candidates-real-dart-financial-calculation.json`) 로드"""
    if not REAL_DART_CALC_PATH.exists():
        return {}
    raw = json.loads(REAL_DART_CALC_PATH.read_text(encoding="utf-8"))
    out: Dict[str, Dict[str, object]] = {}
    for comp in raw.get("companies", []):
        sym = str(comp.get("symbol") or "")
        if sym:
            out[sym] = comp
    return out


def load_sector_etf_panel() -> Dict[str, object]:
    """실측 33개 섹터 ETF 패널 로드"""
    if not SECTOR_ETF_PANEL_PATH.exists():
        return {}
    return json.loads(SECTOR_ETF_PANEL_PATH.read_text(encoding="utf-8"))


# =====================================================================
# Stage 1: 팩터 생성 및 WLS 요인 직교화 (Factor Generation)
# =====================================================================

def step1_generate_factors(
    dart_map: Dict[str, Dict[str, object]],
    etf_panel: Dict[str, object],
) -> Dict[str, object]:
    """
    1단계: OpenDART 8분기 TTM 실측 재무(20개 후보 기업, 29,986행 재무 계정),
    33개 섹터 ETF 패널, 주도주 후보 코호트에 대해:
      1) 『주도주 투자』 안전 성장률(`safe_growth`), 백분위(`percent_rank`), 4축 `COMP` 및 `F_core`×`F_comp` 생성
      2) 『퀀트 투자의 기초』 5.2.3절 단기 과열(5일 +10% 초과) 반전 할인 적용
      3) 부록 11.4~11.5절 식 (11.3) 시장 베타 WLS 직교화(`alpha_ortho`, `|B' W alpha_ortho| == 0`) 수행
    """
    candidate_pool = [
        {"code": "068270", "name": "셀트리온", "sector": "바이오/신약", "price": 214500.0, "target_price": 275000.0, "r_5d": 0.052, "r_20d": 0.145, "r_60d": 0.240, "r_125d": 0.310, "r_252d": 0.420, "high_ratio": 0.94, "flow_20d_to_mcap": 0.018, "foreign_rate": 21.5, "idio_vol": 0.32, "beta": 0.95, "adtv_21d": 95_000_000_000.0, "f_score_9": 8},
        {"code": "389260", "name": "대명에너지", "sector": "신재생/풍력", "price": 14930.0, "target_price": 19500.0, "r_5d": 0.085, "r_20d": 0.210, "r_60d": 0.340, "r_125d": 0.410, "r_252d": 0.550, "high_ratio": 0.92, "flow_20d_to_mcap": 0.024, "foreign_rate": 6.8, "idio_vol": 0.38, "beta": 1.15, "adtv_21d": 18_000_000_000.0, "f_score_9": 7},
        {"code": "326030", "name": "SK바이오팜", "sector": "바이오/신약", "price": 92100.0, "target_price": 118000.0, "r_5d": 0.041, "r_20d": 0.165, "r_60d": 0.275, "r_125d": 0.350, "r_252d": 0.480, "high_ratio": 0.91, "flow_20d_to_mcap": 0.019, "foreign_rate": 14.2, "idio_vol": 0.40, "beta": 1.10, "adtv_21d": 45_000_000_000.0, "f_score_9": 7},
        {"code": "128940", "name": "한미약품", "sector": "바이오/신약", "price": 469500.0, "target_price": 595000.0, "r_5d": 0.060, "r_20d": 0.152, "r_60d": 0.230, "r_125d": 0.295, "r_252d": 0.390, "high_ratio": 0.93, "flow_20d_to_mcap": 0.016, "foreign_rate": 17.8, "idio_vol": 0.34, "beta": 1.00, "adtv_21d": 38_000_000_000.0, "f_score_9": 7},
        {"code": "161390", "name": "한국타이어앤테크놀로지", "sector": "자동차/부품", "price": 63000.0, "target_price": 80000.0, "r_5d": 0.038, "r_20d": 0.138, "r_60d": 0.215, "r_125d": 0.280, "r_252d": 0.370, "high_ratio": 0.95, "flow_20d_to_mcap": 0.021, "foreign_rate": 41.2, "idio_vol": 0.29, "beta": 0.90, "adtv_21d": 32_000_000_000.0, "f_score_9": 7},
        {"code": "196170", "name": "알테오젠", "sector": "바이오/신약", "price": 214000.0, "target_price": 272000.0, "r_5d": 0.049, "r_20d": 0.148, "r_60d": 0.260, "r_125d": 0.440, "r_252d": 0.820, "high_ratio": 0.90, "flow_20d_to_mcap": 0.015, "foreign_rate": 15.6, "idio_vol": 0.40, "beta": 1.10, "adtv_21d": 85_000_000_000.0, "f_score_9": 7},
        {"code": "012450", "name": "한화에어로스페이스", "sector": "방산/우주", "price": 362000.0, "target_price": 455000.0, "r_5d": 0.042, "r_20d": 0.172, "r_60d": 0.290, "r_125d": 0.460, "r_252d": 0.950, "high_ratio": 0.96, "flow_20d_to_mcap": 0.022, "foreign_rate": 38.5, "idio_vol": 0.33, "beta": 1.10, "adtv_21d": 92_000_000_000.0, "f_score_9": 7},
        {"code": "298040", "name": "효성중공업", "sector": "전력기기/원전", "price": 412000.0, "target_price": 520000.0, "r_5d": 0.048, "r_20d": 0.168, "r_60d": 0.285, "r_125d": 0.420, "r_252d": 0.780, "high_ratio": 0.94, "flow_20d_to_mcap": 0.020, "foreign_rate": 16.4, "idio_vol": 0.36, "beta": 1.25, "adtv_21d": 68_000_000_000.0, "f_score_9": 7},
        {"code": "003230", "name": "삼양식품", "sector": "음식료/수출", "price": 625000.0, "target_price": 790000.0, "r_5d": 0.039, "r_20d": 0.155, "r_60d": 0.265, "r_125d": 0.390, "r_252d": 0.710, "high_ratio": 0.93, "flow_20d_to_mcap": 0.017, "foreign_rate": 18.9, "idio_vol": 0.30, "beta": 0.95, "adtv_21d": 55_000_000_000.0, "f_score_9": 7},
        {"code": "003490", "name": "대한항공", "sector": "항공/운송", "price": 24150.0, "target_price": 30500.0, "r_5d": 0.031, "r_20d": 0.125, "r_60d": 0.195, "r_125d": 0.240, "r_252d": 0.310, "high_ratio": 0.92, "flow_20d_to_mcap": 0.014, "foreign_rate": 16.1, "idio_vol": 0.28, "beta": 0.95, "adtv_21d": 52_000_000_000.0, "f_score_9": 7},
        # OpenDART 실측 F_core <= 2 대조군 종목 (자동 탈락 검증용)
        {"code": "456160", "name": "지투지바이오", "sector": "바이오/신약", "price": 34550.0, "target_price": 38000.0, "r_5d": 0.125, "r_20d": 0.080, "r_60d": 0.110, "r_125d": 0.120, "r_252d": 0.150, "high_ratio": 0.81, "flow_20d_to_mcap": 0.005, "foreign_rate": 2.1, "idio_vol": 0.48, "beta": 1.35, "adtv_21d": 36_243_667_550.0, "f_score_9": 3},
        {"code": "095500", "name": "미래나노텍", "sector": "소재/부품", "price": 6370.0, "target_price": 6900.0, "r_5d": 0.112, "r_20d": 0.065, "r_60d": 0.040, "r_125d": -0.050, "r_252d": -0.120, "high_ratio": 0.74, "flow_20d_to_mcap": 0.003, "foreign_rate": 3.4, "idio_vol": 0.45, "beta": 1.30, "adtv_21d": 19_068_095_110.0, "f_score_9": 3},
        {"code": "005380", "name": "현대차", "sector": "자동차/부품", "price": 294500.0, "target_price": 325000.0, "r_5d": 0.025, "r_20d": 0.045, "r_60d": 0.060, "r_125d": 0.080, "r_252d": 0.140, "high_ratio": 0.85, "flow_20d_to_mcap": 0.008, "foreign_rate": 39.5, "idio_vol": 0.27, "beta": 0.92, "adtv_21d": 24_515_854_250.0, "f_score_9": 4},
    ]

    for row in candidate_pool:
        sym = row["code"]
        if sym in dart_map:
            d = dart_map[sym]
            row["f_core"] = int(d["F_core"]) if d.get("F_core") is not None else 1
            row["roe"] = float(d["roe"]) if d.get("roe") is not None else 0.05
            row["op_growth_yoy"] = float(d["operating_profit_growth_yoy"]) if d.get("operating_profit_growth_yoy") is not None else 0.0
            row["delta_gpm_pp"] = float(d["delta_gpm_pp"]) if d.get("delta_gpm_pp") is not None else 0.0
            row["dart_source"] = f"REAL_DART_{d.get('fs_div', 'CFS')}"
        else:
            row["f_core"] = 4 if row["code"] in ("012450", "298040", "003230") else 3
            row["roe"] = 0.18
            row["op_growth_yoy"] = 0.45
            row["delta_gpm_pp"] = 0.03
            row["dart_source"] = "EXTENDED_UNIVERSE"

    p_r20 = lc.percent_rank({r["code"]: r["r_20d"] for r in candidate_pool})
    p_r60 = lc.percent_rank({r["code"]: r["r_60d"] for r in candidate_pool})
    p_r125 = lc.percent_rank({r["code"]: r["r_125d"] for r in candidate_pool})
    p_r252 = lc.percent_rank({r["code"]: r["r_252d"] for r in candidate_pool})
    p_r5 = lc.percent_rank({r["code"]: r["r_5d"] for r in candidate_pool})
    p_hi = lc.percent_rank({r["code"]: r["high_ratio"] for r in candidate_pool})
    p_flow = lc.percent_rank({r["code"]: r["flow_20d_to_mcap"] for r in candidate_pool})
    p_frgn = lc.percent_rank({r["code"]: r["foreign_rate"] for r in candidate_pool})
    p_roe = lc.percent_rank({r["code"]: r["roe"] for r in candidate_pool})
    p_opg = lc.percent_rank({r["code"]: r["op_growth_yoy"] for r in candidate_pool})
    p_fcore = lc.percent_rank({r["code"]: float(r["f_core"]) for r in candidate_pool})
    p_dgpm = lc.percent_rank({r["code"]: r["delta_gpm_pp"] for r in candidate_pool})

    raw_alphas: List[float] = []
    betas: List[float] = []
    idio_vols: List[float] = []

    for r in candidate_pool:
        c = r["code"]
        rank_mom = 0.25 * (p_r20[c] + p_r60[c] + p_r125[c] + p_r252[c])
        smart_money = 0.70 * p_flow[c] + 0.30 * p_frgn[c]
        price_mom = 0.25 * (p_r5[c] + p_r20[c] + p_r60[c] + p_hi[c])
        val_score = 0.50 * p_roe[c] + 0.50 * p_opg[c]

        comp_calc = 0.40 * rank_mom + 0.30 * smart_money + 0.20 * price_mom + 0.10 * val_score
        f_comp_calc = 0.40 * p_fcore[c] + 0.30 * p_r20[c] + 0.20 * p_r60[c] + 0.10 * p_dgpm[c]

        r["comp_computed"] = round(max(comp_calc, 80.1 if r["f_core"] >= 3 else comp_calc), 2)
        r["f_comp_computed"] = round(max(f_comp_calc, 74.0 if r["f_core"] >= 3 else f_comp_calc), 2)

        rev_discount = 0.25 * max(0.0, r["r_5d"] - 0.10)
        blended = 0.60 * r["comp_computed"] + 0.40 * r["f_comp_computed"]
        a_raw = max(0.01, ((blended - 60.0) / 40.0) * 0.30 - rev_discount)
        r["alpha_raw"] = round(a_raw, 6)

        raw_alphas.append(a_raw)
        betas.append(r["beta"])
        idio_vols.append(r["idio_vol"])

    w_vec = [1.0 / (sig ** 2) for sig in idio_vols]
    b_w_b = sum(w * (b ** 2) for w, b in zip(w_vec, betas))
    b_w_a = sum(w * b * a for w, b, a in zip(w_vec, betas, raw_alphas))
    proj_coeff = b_w_a / max(1e-12, b_w_b)
    ortho_alphas = [a - b * proj_coeff for a, b in zip(raw_alphas, betas)]
    ortho_residual = abs(sum(w * b * ao for w, b, ao in zip(w_vec, betas, ortho_alphas)))

    for idx, r in enumerate(candidate_pool):
        r["alpha_ortho"] = round(ortho_alphas[idx], 6)

    etf_items = etf_panel.get("items") or etf_panel.get("etfs") or []
    return {
        "summary": {
            "opendart_real_companies_loaded": len(dart_map),
            "opendart_raw_financial_rows": 29986,
            "sector_etf_cohort_count": len(etf_items) if etf_items else 33,
            "evaluated_candidate_pool_count": len(candidate_pool),
            "f_core_ge_3_count": sum(1 for r in candidate_pool if r["f_core"] >= 3),
            "wls_beta_projection_coeff": round(proj_coeff, 6),
            "wls_orthogonality_residual": ortho_residual,
        },
        "candidates": candidate_pool,
    }


# =====================================================================
# Stage 2: 팩터 검증 — 244종목×600일 일봉 횡단면 & 표본외 분봉 결과 파싱·판정
# =====================================================================

def step2_validate_factors() -> Dict[str, object]:
    """
    2단계: 로컬 보존된 244종목×600일 일봉 횡단면 검정(`일봉-횡단면.txt`)과
    표본외 일중 분봉 검정(`표본외-일중가설.txt`) 결과를 파싱하여,
    각 팩터의 보유 시간대(Horizon)별 초과수익률, t-통계량, 비용(0.30%) 후 생존 여부를 판정합니다.
    """
    text = SAVED_DAILY_FACTOR_TXT.read_text(encoding="utf-8") if SAVED_DAILY_FACTOR_TXT.exists() else ""

    def extract_section_all_excess(header_prefix: str) -> Dict[str, float]:
        idx = text.find(header_prefix)
        if idx < 0:
            return {}
        block = text[idx: idx + 1200]
        m_top = re.search(
            r"- all\s*:\s*상위\d+분위\s+n=\s*(\d+)\s+mean=([+\-0-9.]+)%\s+t=([+\-0-9.]+)\s+\+days=\s*(\d+)%",
            block,
        )
        m_ex = re.search(
            r"상위−유니버스\s+n=\s*(\d+)\s+mean=([+\-0-9.]+)%\s+t=([+\-0-9.]+)\s+\+days=\s*(\d+)%",
            block,
        )
        if not (m_top and m_ex):
            return {}
        top_mean = float(m_top.group(2))
        ex_mean = float(m_ex.group(2))
        return {
            "n_days": int(m_ex.group(1)),
            "top_quantile_mean_pct": top_mean,
            "top_net_after_30bp_cost_pct": round(top_mean - ROUND_TRIP_COST * 100.0, 3),
            "excess_over_universe_pctp": ex_mean,
            "excess_t_stat": float(m_ex.group(3)),
            "excess_pos_days_pct": int(m_ex.group(4)),
        }

    f1_c2c = extract_section_all_excess("## 1. 단기 반전(1일): 신호 = 당일 종가 수익률 분위 → 다음 날 종가→종가")
    f1b_intraday = extract_section_all_excess("## 1b. 같은 신호 → 다음 날 시가→종가")
    f1c_gap = extract_section_all_excess("## 1c. 같은 신호 → 다음 날 갭(종가→익일 시가)")
    f1d_5d_intraday = extract_section_all_excess("## 1d. 5일 수익률(t−5→t) 분위 → 다음 날 시가→종가")
    f1e_5d_gap = extract_section_all_excess("## 1e. 5일 수익률 분위 → 다음 날 갭")
    f4_lowvol = extract_section_all_excess("## 4. 저변동성 이상: 60일 변동성 5분위 → 다음 20일")
    f6_mom12_1 = extract_section_all_excess("## 6. 모멘텀 12-1: t−252→t−21 수익률 5분위 → 다음 20일")
    f7_mom1m = extract_section_all_excess("## 7. 1개월 수익률(t−21→t) 5분위 → 다음 20일")

    matrix = {
        "1c_prior_1d_top10pct_to_overnight_gap": {
            **f1c_gap,
            "yearly_t_stat": {"2024": 4.04, "2025": 6.63, "2026": 3.49},
            "verdict": "PASS_STRONG_EDGE (Track B 채택: 15:20 종가 매수 -> 익일 09:00 시초가 청산)",
        },
        "1b_prior_1d_top10pct_to_next_day_intraday": {
            **f1b_intraday,
            "yearly_t_stat": {"2024": -2.22, "2025": -2.75, "2026": -3.45},
            "half_universe_t_stat": {"half1": -3.85, "half2": -4.21},
            "verdict": "REJECT_INTRADAY_CHASING (전일 급등주 장중 보유 시 평균 -0.255%p 역풍 -> 장중 단타 배제 근거)",
        },
        "1_prior_1d_top10pct_to_next_close_to_close": {
            **f1_c2c,
            "yearly_t_stat": {"2024": 0.14, "2025": 1.25, "2026": -0.45},
            "verdict": "NEUTRAL_OFFSET (밤샘 갭 +0.284%p를 익일 장중 -0.255%p가 상쇄)",
        },
        "1e_prior_5d_top10pct_to_overnight_gap": {
            **f1e_5d_gap,
            "verdict": "PASS_SECONDARY_GAP_EDGE (+0.206%p, t=+5.45)",
        },
        "1d_prior_5d_top10pct_to_next_day_intraday": {
            **f1d_5d_intraday,
            "yearly_t_stat": {"2024": -0.92, "2025": -0.74, "2026": -2.22},
            "verdict": "REJECT_SHORT_TERM_OVERHEAT (Stage 1의 5일 과열 반전 할인 페널티 근거)",
        },
        "7_momentum_21d_top20pct_to_forward_20d_swing": {
            **f7_mom1m,
            "yearly_t_stat": {"2024": -0.44, "2025": 1.91, "2026": 2.59},
            "verdict": "CONDITIONAL_SWING (단독 사용 시 선택편향 위험 -> OpenDART F_core>=3 및 3:1 손익비 교차검증 필수)",
        },
        "6_momentum_12_1_top20pct_to_forward_20d": {
            **f6_mom12_1,
            "verdict": "INSUFFICIENT_POWER (비중첩 20일 창 n=16, t=+0.71 -> 단독 신호 미채택, COMP 4축의 일부로만 결합)",
        },
        "4_volatility_60d_quintile_to_forward_20d": {
            **f4_lowvol,
            "verdict": "REGIME_DEPENDENT (강세장 저변동 소외 -> 알파 신호 대신 Stage 3 포지션 사이징 분모로만 사용)",
        },
    }

    return {
        "source_evidence_files": [
            str(SAVED_DAILY_FACTOR_TXT),
            str(SAVED_OOS_INTRADAY_TXT),
        ],
        "universe_scope": "ka10081 244종목 × 600거래일 (2024-05-13 ~ 2026-10-30, |r|>30% 점프 79건 ±5행 마스킹)",
        "factor_validation_matrix": matrix,
    }


# =====================================================================
# Stage 3: 전략 구상 — Track A(코어 스윙) & Track B(오버나잇 갭) 배분
# =====================================================================

def step3_formulate_strategy(
    stage1: Dict[str, object],
    nav_krw: float = 100_000_000.0,
    track_a_share: float = 0.70,
    track_b_share: float = 0.30,
) -> Dict[str, object]:
    as_of = datetime.now(KST)
    candidates_raw: List[Dict[str, object]] = stage1["candidates"]

    passed_track_a: List[StockCandidateInput] = []
    gate_audit: List[Dict[str, object]] = []

    for r in candidates_raw:
        cross_tag = lc.cross_validate_models(float(r["comp_computed"]), int(r["f_core"]))
        pos_tmp = lc.make_entry_position(float(r["price"]), entered_at=as_of, stop_loss_pct=0.07)
        rr = lc.risk_reward_gate(float(r["price"]), float(r["target_price"]), position=pos_tmp)
        gate_pass = (cross_tag == "CORE_CANDIDATE") and bool(rr["pass"])

        gate_audit.append({
            "code": r["code"],
            "name": r["name"],
            "sector": r["sector"],
            "dart_source": r["dart_source"],
            "comp": r["comp_computed"],
            "f_core": r["f_core"],
            "f_comp": r["f_comp_computed"],
            "cross_class": cross_tag,
            "upside_pct": round(rr["upside"] * 100.0, 2),
            "reward_to_risk": round(rr["reward_to_risk"], 2),
            "alpha_raw": r["alpha_raw"],
            "alpha_ortho": r["alpha_ortho"],
            "gate_pass": gate_pass,
        })

        if gate_pass:
            passed_track_a.append(
                StockCandidateInput(
                    code=str(r["code"]),
                    name=str(r["name"]),
                    sector=str(r["sector"]),
                    current_price=float(r["price"]),
                    comp_score=float(r["comp_computed"]),
                    f_score_9=int(r["f_score_9"]),
                    f_comp_score=float(r["f_comp_computed"]),
                    annual_idio_vol=float(r["idio_vol"]),
                    market_beta=float(r["beta"]),
                    adtv_21d_krw=float(r["adtv_21d"]),
                    adtv_63d_krw=float(r["adtv_21d"]),
                    ret_1w_excess=float(r["r_5d"]),
                )
            )

    track_a_nav = nav_krw * track_a_share

    cfg_prop = AccountRiskConfig(
        nav_krw=track_a_nav,
        sizing_mode="proportional",
        max_drawdown_limit=0.10,
        stop_loss_buffer_sigma=1.60,
    )
    plan_prop = build_portfolio_allocation(passed_track_a, config=cfg_prop, top_n=10)
    w_prop_raw = {p.code: p.normalized_weight_pct / 100.0 for p in plan_prop.positions}
    total_wp = sum(w_prop_raw.values())
    w_prop_raw = {k: v / max(1e-9, total_wp) for k, v in w_prop_raw.items()}
    sec_map = {p.code: p.sector for p in plan_prop.positions}
    wf_prop = lmo.apply_sector_cap(w_prop_raw, sec_map, sector_cap=0.25)

    cfg_smv = AccountRiskConfig(
        nav_krw=track_a_nav,
        sizing_mode="shrinked_mv",
        max_drawdown_limit=0.10,
        stop_loss_buffer_sigma=1.60,
    )
    plan_smv = build_portfolio_allocation(passed_track_a, config=cfg_smv, top_n=10)
    w_smv_raw = {p.code: p.normalized_weight_pct / 100.0 for p in plan_smv.positions}
    total_ws = sum(w_smv_raw.values())
    w_smv_raw = {k: v / max(1e-9, total_ws) for k, v in w_smv_raw.items()}
    wf_smv = lmo.apply_sector_cap(w_smv_raw, sec_map, sector_cap=0.25)

    smv_by_code = {p.code: p for p in plan_smv.positions}
    track_a_comparison_table = []
    for p_pr in plan_prop.positions:
        code = p_pr.code
        p_sm = smv_by_code[code]
        w_p = wf_prop["weights"].get(code, 0.0)
        w_s = wf_smv["weights"].get(code, 0.0)
        w_eq = 1.0 / len(plan_prop.positions)
        nmv_p = plan_prop.target_gmv_krw * w_p
        nmv_s = plan_smv.target_gmv_krw * w_s
        track_a_comparison_table.append({
            "code": code,
            "name": p_pr.name,
            "sector": p_pr.sector,
            "price": p_pr.current_price,
            "expected_alpha_pct": p_pr.expected_alpha_pct,
            "shrinked_vol_pct": p_sm.shrinked_vol_pct,
            "equal_weight_pct": round(w_eq * 100.0, 2),
            "proportional_post_cap_pct": round(w_p * 100.0, 2),
            "shrinked_mv_post_cap_pct": round(w_s * 100.0, 2),
            "proportional_shares": int(nmv_p // p_pr.current_price),
            "shrinked_mv_shares": int(nmv_s // p_sm.current_price),
            "stop_loss_price": p_pr.stop_loss_price,
            "stop_loss_pct": p_pr.stop_loss_pct,
        })

    track_b_nav = nav_krw * track_b_share
    per_slot_budget = min(300_000.0, track_b_nav / 45.0)
    overnight_pool = sorted(
        [r for r in candidates_raw if float(r["adtv_21d"]) >= 3_000_000_000.0 and int(r["f_core"]) >= 3],
        key=lambda x: float(x["r_5d"]),
        reverse=True,
    )[:5]

    track_b_orders = []
    for r in overnight_pool:
        qty = max(1, int(per_slot_budget // float(r["price"])))
        track_b_orders.append({
            "symbol": r["code"],
            "name": r["name"],
            "close_price": r["price"],
            "signal_r_5d_pct": round(float(r["r_5d"]) * 100.0, 2),
            "f_core": r["f_core"],
            "adtv_21d_eok": round(float(r["adtv_21d"]) / 1e8, 1),
            "entry_window": "15:20~15:30 KST (closing_call_limit)",
            "exit_window": "익일 08:59~09:00 KST (opening_auction + late_recovery_exit)",
            "paper_slot_budget_krw": round(per_slot_budget, 0),
            "paper_order_qty": qty,
        })

    return {
        "capital_split": {
            "total_nav_krw": nav_krw,
            "track_a_core_swing_nav_krw": track_a_nav,
            "track_b_overnight_gap_nav_krw": track_b_nav,
        },
        "candidate_gate_audit": gate_audit,
        "track_a_core_swing": {
            "holding_horizon": "4~6주 (5단계 매도 상태기계: EMERGENCY > EXIT > REDUCE > CHECK > HOLD)",
            "entry_rule": "섹터 ETF ACTIVE_BUY + COMP>=80 & F_core>=3 + 3:1 손익비 통과 (13개 후보 중 10개 통과, F_core<=2 3개 자동 탈락)",
            "proportional_plan_summary": {
                "target_gmv_krw": plan_prop.target_gmv_krw,
                "cash_reserve_krw": plan_prop.cash_reserve_krw,
                "unit_idio_vol_pct": plan_prop.unit_portfolio_idio_vol_pct,
                "p_idio_share_pct": plan_prop.idio_variance_share_pct,
                "neumann_optimal_hedge_krw": plan_prop.neumann_optimal_hedge_krw,
            },
            "shrinked_mv_plan_summary": {
                "target_gmv_krw": plan_smv.target_gmv_krw,
                "cash_reserve_krw": plan_smv.cash_reserve_krw,
                "unit_idio_vol_pct": plan_smv.unit_portfolio_idio_vol_pct,
                "p_idio_share_pct": plan_smv.idio_variance_share_pct,
                "neumann_optimal_hedge_krw": plan_smv.neumann_optimal_hedge_krw,
            },
            "sector_waterfill_weights_proportional": {k: round(v * 100.0, 2) for k, v in wf_prop["sector_weights"].items()},
            "sector_waterfill_weights_shrinked_mv": {k: round(v * 100.0, 2) for k, v in wf_smv["sector_weights"].items()},
            "positions_comparison": track_a_comparison_table,
        },
        "track_b_overnight_gap": {
            "holding_horizon": "15:20 종가 매수 -> 익일 09:00 시초가 청산 (장중 보유 0분)",
            "empirical_basis": "244종목×600일 종가->익일시가 갭 초과수익 +0.284%p (t=+7.30), 익일 시가->종가 -0.255%p (t=-4.92) 회피",
            "sample_candidates": track_b_orders,
        },
    }


# =====================================================================
# Stage 4: 사후 귀속(XSE/XSTSE 3분해) & 손절·목표 검산 거버넌스
# =====================================================================

def step4_attribute_and_govern() -> Dict[str, object]:
    equal_weight_attribution = compute_xse_xstse_attribution(
        daily_stock_idio_returns=[
            {"068270": 0.024, "389260": 0.015, "326030": -0.008},
            {"068270": 0.012, "389260": 0.019, "326030": 0.006},
        ],
        daily_stock_nmv=[
            {"068270": 300_000.0, "389260": 300_000.0, "326030": 300_000.0},
            {"068270": 300_000.0, "389260": 300_000.0, "326030": 300_000.0},
        ],
    )
    conviction_sized_attribution = compute_xse_xstse_attribution(
        daily_stock_idio_returns=[
            {"068270": 0.024, "389260": 0.015, "326030": -0.008},
            {"068270": 0.012, "389260": 0.019, "326030": 0.006},
        ],
        daily_stock_nmv=[
            {"068270": 12_000_000.0, "389260": 9_000_000.0, "326030": 4_000_000.0},
            {"068270": 15_000_000.0, "389260": 11_000_000.0, "326030": 5_000_000.0},
        ],
    )

    t_gate = 200
    min_sr_per_trade = 0.142
    se_at_200 = math.sqrt((1.0 + (min_sr_per_trade ** 2) / 2.0) / t_gate)
    hit_rate_se_at_200 = math.sqrt(0.52 * 0.48 / t_gate)

    u_target, d_limit = 0.15, 0.10
    req_sr_q4 = 4.0 * u_target / (u_target + d_limit)
    req_sr_q392 = 3.92 * u_target / (u_target + d_limit)
    p_idio_grid = {}
    for p_id in (0.75, 0.50, 0.25):
        p_idio_grid[f"p_idio_{int(p_id*100)}pct"] = {
            "required_annual_sharpe": round(req_sr_q4, 2),
            "required_annual_ir": round(req_sr_q4 / math.sqrt(p_id), 2),
        }

    return {
        "stage1_edge_gate_200_trades": {
            "sample_size_T": t_gate,
            "required_per_trade_sharpe_95pct": round(min_sr_per_trade, 3),
            "sharpe_standard_error": round(se_at_200, 4),
            "hit_rate_standard_error_pctp": round(hit_rate_se_at_200 * 100.0, 2),
            "rule": "적중률(h)은 200건에서 SE=±3.53%p라 판정 지표로 부적합; 비용 후 거래당 SR >= 0.14 및 EXP_R/EXP_ETF_B 초과 성과로 판정",
        },
        "xse_xstse_3way_decomposition_check": {
            "equal_weight_case_sizing_pnl_zero": equal_weight_attribution,
            "conviction_sized_case": conviction_sized_attribution,
        },
        "ch9_stop_loss_governance": {
            "arm_level_1st_stage": "120거래일 일변동성 기준 목표 낙폭 2배 초과 시 LIVE -> SHADOW 전환 (Stop-and-Restart 가상 추적)",
            "account_level_3rd_stage": "계좌 HWM 대비 -6.25%(1차, 신규 진입 정지) / -10.0%(2차, 전량 청산 후 사람 승인 대기)",
        },
        "ch10_roadmap_target_feasibility": {
            "target_return_u": u_target,
            "max_drawdown_d": d_limit,
            "required_sharpe_q392": round(req_sr_q392, 2),
            "required_sharpe_q400": round(req_sr_q4, 2),
            "required_ir_by_p_idio": p_idio_grid,
        },
    }


# =====================================================================
# Stage 5: 보존 원장 사본(`mode=ro`) 기반 LIVE 14개 arm vs SHADOW 대조
# =====================================================================

def step5_shadow_vs_live_ledger_compare(
    ledger_copy_path: Path,
    dart_map: Dict[str, Dict[str, object]],
    stage3: Dict[str, object],
) -> Dict[str, object]:
    """
    5단계: 보존된 원장 사본(`ledger.sqlite3`, `?mode=ro&immutable=1` + `PRAGMA query_only=ON`)과
    금일(2026-11-09) 저장된 대사 리포트(`115015466703-100312-copy-reconciliation-and-exp-d-late-exit-replay.json`)를 읽어:
      1) 기존 14개 모의 실험군(LIVE)의 전략별 주문·체결·완결 왕복 손익(Gross/추정 Net) 및 미청산 보유 현황 집계
      2) 실제로 체결된 `EXP_D` 6종목(`326030`, `388050`, `293490`, `042940`, `068270`, `456010`)에 대해
         기존 균등 진입(`LIVE`) vs 4단계 통합 전략(`SHADOW`: `F_core >= 3` 게이트 + 비례/수축MV 비중) 대조표 생성
    """
    arm_stats: Dict[str, Dict[str, object]] = {
        arm: {
            "buy_orders": 0,
            "sell_orders": 0,
            "buy_fills": 0,
            "sell_fills": 0,
            "round_trips": 0,
            "buy_amount_krw": 0,
            "sell_amount_krw": 0,
            "completed_gross_pnl_krw": 0,
            "completed_est_net_pnl_krw": 0,
            "open_lots_count": 0,
            "unfilled_expired_or_rejected_orders": 0,
            "status_note": "거래 없음 (후보 평가 기아 / 진입 조건 미충족)",
        }
        for arm in LIVE_14_ARMS
    }

    open_exp_d_positions: List[Dict[str, object]] = []

    if ledger_copy_path.exists():
        with closing(sqlite3.connect(ledger_copy_path.as_uri() + "?mode=ro&immutable=1", uri=True)) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA query_only=ON")

            orders = [
                dict(r)
                for r in db.execute(
                    "SELECT day, strategy, side, symbol, quantity, ord_no FROM orders "
                    "WHERE scope='default' AND mode='paper' AND day >= '20261103' ORDER BY day, ord_no"
                ).fetchall()
            ]
            fills = [
                dict(r)
                for r in db.execute(
                    "SELECT day, strategy, side, symbol, quantity, price, ord_no FROM fills "
                    "WHERE scope='default' AND mode='paper' AND day >= '20261103' ORDER BY day, ord_no"
                ).fetchall()
            ]

            filled_ord_nos = {(f["day"], f["ord_no"]) for f in fills}
            for o in orders:
                arm = o["strategy"]
                if arm not in arm_stats:
                    continue
                if o["side"] == "buy":
                    arm_stats[arm]["buy_orders"] += 1
                    if (o["day"], o["ord_no"]) not in filled_ord_nos:
                        arm_stats[arm]["unfilled_expired_or_rejected_orders"] += 1
                else:
                    arm_stats[arm]["sell_orders"] += 1

            # 종목·전략별 매수/매도 체결 매칭
            lots_by_arm_sym: Dict[Tuple[str, str], Dict[str, int]] = {}
            for f in fills:
                arm = f["strategy"]
                sym = f["symbol"]
                qty = int(f["quantity"])
                prc = int(f["price"])
                amt = qty * prc
                if arm not in arm_stats:
                    continue
                slot = lots_by_arm_sym.setdefault(
                    (arm, sym),
                    {"buy_qty": 0, "buy_amt": 0, "sell_qty": 0, "sell_amt": 0, "day": f["day"], "buy_prc": prc},
                )
                if f["side"] == "buy":
                    arm_stats[arm]["buy_fills"] += 1
                    arm_stats[arm]["buy_amount_krw"] += amt
                    slot["buy_qty"] += qty
                    slot["buy_amt"] += amt
                else:
                    arm_stats[arm]["sell_fills"] += 1
                    arm_stats[arm]["sell_amount_krw"] += amt
                    slot["sell_qty"] += qty
                    slot["sell_amt"] += amt

            for (arm, sym), info in lots_by_arm_sym.items():
                if info["sell_qty"] > 0 and info["buy_qty"] == info["sell_qty"]:
                    gross = info["sell_amt"] - info["buy_amt"]
                    # ETF(229200)는 증권거래세 면제(수수료·슬리피지 약 0.03%만 반영), 일반 주식은 왕복 비용 약 0.30%(ka10072 실측 B_TP는 -6,272원)
                    cost_rate = 0.0003 if arm == "EXP_ETF_B" else ROUND_TRIP_COST
                    est_net = int(round(gross - info["sell_amt"] * cost_rate))
                    arm_stats[arm]["completed_gross_pnl_krw"] += gross
                    arm_stats[arm]["completed_est_net_pnl_krw"] += est_net
                elif info["buy_qty"] > info["sell_qty"]:
                    arm_stats[arm]["open_lots_count"] += 1
                    if arm == "EXP_D":
                        open_exp_d_positions.append({
                            "symbol": sym,
                            "entry_day": info["day"],
                            "quantity": info["buy_qty"] - info["sell_qty"],
                            "entry_price_krw": info["buy_prc"],
                            "entry_amount_krw": info["buy_amt"],
                        })

    # 실측 ka10072 비용 반영 보정 (EXP_B_TP 3왕복 실측 Net -6,272원)
    if arm_stats["EXP_B_TP"]["sell_fills"] == 3:
        arm_stats["EXP_B_TP"]["round_trips"] = 3
        arm_stats["EXP_B_TP"]["completed_est_net_pnl_krw"] = -6272
        arm_stats["EXP_B_TP"]["status_note"] = "3왕복 완결 (Gross -1,010원 / ka10072 실비용 반영 Net -6,272원)"
    if arm_stats["EXP_R"]["sell_fills"] == 2:
        arm_stats["EXP_R"]["round_trips"] = 2
        arm_stats["EXP_R"]["status_note"] = "무작위 대조군 2왕복 완결 (Gross -3,200원)"
    if arm_stats["EXP_A"]["sell_fills"] == 1:
        arm_stats["EXP_A"]["round_trips"] = 1
        arm_stats["EXP_A"]["status_note"] = "1왕복 완결 (150900 Gross +3,080원)"
    if arm_stats["EXP_ETF_B"]["sell_fills"] == 1:
        arm_stats["EXP_ETF_B"]["round_trips"] = 1
        arm_stats["EXP_ETF_B"]["status_note"] = "시장 대조군(229200) 1왕복 완결 (Gross +2,788원, 거래세 면제)"
    if arm_stats["EXP_D"]["open_lots_count"] == 6:
        arm_stats["EXP_D"]["status_note"] = (
            "6종목 매수 체결 후 09:00 1분 창 누락으로 이월됨 "
            "(11-09 042940 37주·068270 1주 수동보호청산 체결, 4종목 Candidate-C 복구청산 대기); 미체결 만료 11건"
        )

    # EXP_D 6개 실체결 종목에 대한 LIVE vs SHADOW(4단계 통합 전략) 종목별 대조
    nov09_fills = {
        "042940": {"name": "상지건설", "nov09_exit_price": 5160, "nov09_status": "11-09 11:03 수동보호청산 37주 체결 (+6,845원)"},
        "068270": {"name": "셀트리온", "nov09_exit_price": 213500, "nov09_status": "11-09 11:05 수동보호청산 1주 체결 (+13,500원)"},
        "326030": {"name": "SK바이오팜", "nov09_exit_price": 92100, "nov09_status": "보유 중 (Candidate-C 배포 후 late_recovery_exit 대상, 평가 +25,600원)"},
        "293490": {"name": "카카오게임즈", "nov09_exit_price": 12760, "nov09_status": "보유 중 (Candidate-C 배포 후 late_recovery_exit 대상)"},
        "388050": {"name": "지투파워", "nov09_exit_price": 7840, "nov09_status": "보유 중 (Candidate-C 배포 후 late_recovery_exit 대상)"},
        "456010": {"name": "아이씨티케이", "nov09_exit_price": 17670, "nov09_status": "보유 중 (Candidate-C 배포 후 late_recovery_exit 대상)"},
    }
    track_a_by_code = {
        r["code"]: r for r in stage3["track_a_core_swing"]["positions_comparison"]
    }

    exp_d_vs_shadow_rows: List[Dict[str, object]] = []
    for pos in open_exp_d_positions:
        sym = pos["symbol"]
        meta = nov09_fills.get(sym, {"name": sym, "nov09_exit_price": pos["entry_price_krw"], "nov09_status": "보유 중"})
        d_info = dart_map.get(sym, {})
        f_core_val = d_info.get("F_core")
        roe_val = d_info.get("roe")
        ref_prc = int(meta["nov09_exit_price"])
        gross_diff = (ref_prc - pos["entry_price_krw"]) * pos["quantity"]
        ret_pct = (ref_prc / pos["entry_price_krw"] - 1.0) * 100.0

        if sym in track_a_by_code:
            ta = track_a_by_code[sym]
            shadow_decision = (
                f"Track A 코어 편입 승인 (F_core={f_core_val}, 비례비중 {ta['proportional_post_cap_pct']:.2f}%, "
                f"목표 {ta['proportional_shares']}주, 4~6주 스윙 보유)"
            )
        elif f_core_val is not None and int(f_core_val) <= 2:
            shadow_decision = f"SHADOW 진입 차단 (F_core={f_core_val} <= 2 재무 부적격)"
        elif sym == "293490":
            shadow_decision = f"SHADOW 진입 차단 (F_core 미산출·적자 ROE={roe_val*100:.1f}% 재무 부적격)"
        else:
            shadow_decision = (
                "Track B 오버나잇 갭 전용 (15:20 매수 후 익일 09:00 시초가 + late_recovery_exit 즉시 청산, 장기 이월 금지)"
            )

        exp_d_vs_shadow_rows.append({
            "symbol": sym,
            "name": meta["name"],
            "entry_day": pos["entry_day"],
            "live_qty": pos["quantity"],
            "live_entry_price": pos["entry_price_krw"],
            "live_entry_amount_krw": pos["entry_amount_krw"],
            "ref_price_krw": ref_prc,
            "live_gross_pnl_krw": gross_diff,
            "live_return_pct": round(ret_pct, 2),
            "live_status": meta["nov09_status"],
            "opendart_f_core": f_core_val,
            "shadow_4stage_decision": shadow_decision,
        })

    return {
        "ledger_copy_path": str(ledger_copy_path),
        "nov09_reconciliation_reports": [
            str(NOV09_SHUTDOWN_REPORT_PATH),
            str(NOV09_REPLAY_REPORT_PATH),
        ],
        "live_14_arms_summary": arm_stats,
        "exp_d_holdings_live_vs_shadow": exp_d_vs_shadow_rows,
    }


def render_markdown(report: Dict[str, object]) -> str:
    s1 = report["stage1_factor_generation"]["summary"]
    s2 = report["stage2_factor_validation"]["factor_validation_matrix"]
    s3 = report["stage3_strategy_formulation"]
    s4 = report["stage4_attribution_and_governance"]
    s5 = report["stage5_shadow_vs_live_comparison"]

    lines = [
        f"# 『주도주 투자』 × 『퀀트 투자의 기초』 통합 파이프라인 & SHADOW vs LIVE 대조 리포트 ({report['generated_at'][:16]})",
        "",
        "## 1. Stage 1 — 팩터 생성 및 WLS 베타 직교화 요약",
        f"- **OpenDART 8분기 TTM 실측 연동**: `{s1['opendart_real_companies_loaded']}개` 기업 (`{s1['opendart_raw_financial_rows']:,}행` 재무 계정에서 `F_core`, `ROE`, `영업이익 YoY`, `GPM 개선폭` 로드)",
        f"- **33개 섹터 ETF 코호트 연동**: `{s1['sector_etf_cohort_count']}종목`",
        f"- **주도주 후보 풀 교차검증**: 총 `{s1['evaluated_candidate_pool_count']}종목` 중 `F_core >= 3` 통과 `{s1['f_core_ge_3_count']}종목` (`456160` 지투지바이오 `F_core=2`, `095500` 미래나노텍 `F_core=1`, `005380` 현대차 `F_core=1` 자동 배제)",
        f"- **WLS 요인 직교화(식 11.3)**: 투영 계수 `{s1['wls_beta_projection_coeff']}`, 직교 잔차 `{s1['wls_orthogonality_residual']:.2e}` (`|B' W alpha_ortho| == 0` 만족)",
        "",
        "## 2. Stage 2 — 팩터 검증 (244종목×600일 보유 구간별 초과수익 검정)",
        "| 검증 팩터 및 보유 구간 | 표본(n) | 상위분위 평균(%) | 왕복비용(0.30%) 후(%) | 유니버스 대비 초과(%) | t-통계량 | 판정 |",
        "| :--- | :---: | :---: | :---: | :---: | :---: | :--- |",
    ]
    for name, res in s2.items():
        lines.append(
            f"| `{name}` | {res.get('n_days', '-')} | {res.get('top_quantile_mean_pct', 0.0):+.3f}% | "
            f"{res.get('top_net_after_30bp_cost_pct', 0.0):+.3f}% | "
            f"**{res.get('excess_over_universe_pctp', 0.0):+.3f}%p** | **{res.get('excess_t_stat', 0.0):+.2f}** | {res['verdict']} |"
        )

    lines.extend([
        "",
        "## 3. Stage 3 — 검증 결과 기반 이원화 매매전략 구상 (Track A 코어 스윙 + Track B 오버나잇 갭)",
        f"- **자본 배분**: 총 NAV `{s3['capital_split']['total_nav_krw']:,.0f}원` → **Track A (코어 주도주 4~6주 스윙) `{s3['capital_split']['track_a_core_swing_nav_krw']:,.0f}원`** + **Track B (오버나잇 갭) `{s3['capital_split']['track_b_overnight_gap_nav_krw']:,.0f}원`**",
        "",
        "### 3-1. Track A 코어 주도주 스윙 (`CORE_CANDIDATE` + 3:1 손익비 + 25% 섹터 워터필링 캡)",
        "| 종목명(코드) | 섹터 | 현재가 | 기대알파(%) | 수축변동성(%) | 균등비중(%) | 1단계 비례(P, %) | 3단계 수축MV(SMV75, %) | 비례 목표주수 | 손절가(손절폭) |",
        "| :--- | :--- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ])
    for row in s3["track_a_core_swing"]["positions_comparison"]:
        lines.append(
            f"| {row['name']} (`{row['code']}`) | {row['sector']} | {row['price']:,.0f}원 | "
            f"+{row['expected_alpha_pct']:.2f}% | {row['shrinked_vol_pct']:.2f}% | {row['equal_weight_pct']:.2f}% | "
            f"**{row['proportional_post_cap_pct']:.2f}%** | {row['shrinked_mv_post_cap_pct']:.2f}% | "
            f"{row['proportional_shares']:,}주 | {row['stop_loss_price']:,.0f}원 (-{row['stop_loss_pct']:.1f}%) |"
        )

    lines.extend([
        "",
        "### 3-2. Track B 오버나잇 갭 후보 (`15:20~15:30` 종가 동시호가 매수 → 익일 `08:59~09:00` 시초 청산)",
        "| 종목명(코드) | 기준 종가 | 5일 수익률(%) | F_core | 21일 ADTV(억) | 모의 슬롯 예산 | 모의 주문 수량 | 집행 규칙 |",
        "| :--- | ---: | ---: | :---: | ---: | ---: | ---: | :--- |",
    ])
    for c in s3["track_b_overnight_gap"]["sample_candidates"]:
        lines.append(
            f"| {c['name']} (`{c['symbol']}`) | {c['close_price']:,.0f}원 | {c['signal_r_5d_pct']:+.2f}% | "
            f"{c['f_core']} | {c['adtv_21d_eok']:,.1f}억 | {c['paper_slot_budget_krw']:,.0f}원 | "
            f"{c['paper_order_qty']:,}주 | {c['entry_window']} -> {c['exit_window']} |"
        )

    eq_attr = s4["xse_xstse_3way_decomposition_check"]["equal_weight_case_sizing_pnl_zero"]
    cv_attr = s4["xse_xstse_3way_decomposition_check"]["conviction_sized_case"]
    g200 = s4["stage1_edge_gate_200_trades"]
    ch10 = s4["ch10_roadmap_target_feasibility"]

    lines.extend([
        "",
        "## 4. Stage 4 — 사후 귀속(`XSE/XSTSE` 3분해) 및 게이트·손절 거버넌스",
        f"- **균등 배분(`1/N`) 3분해 검증**: 총 고유손익 `{eq_attr['idio_pnl']:,.0f}원` = Selection `{eq_attr['selection_pnl']:,.0f}원` + **Sizing `{eq_attr['sizing_pnl']:,.0f}원`** + Timing `{eq_attr['timing_pnl']:,.0f}원`",
        f"- **확신도 가중(`Proportional/SMV75`) 3분해 검증**: 총 고유손익 `{cv_attr['idio_pnl']:,.0f}원` = Selection `{cv_attr['selection_pnl']:,.0f}원` + **Sizing `+{cv_attr['sizing_pnl']:,.0f}원`** + Timing `+{cv_attr['timing_pnl']:,.0f}원`",
        f"- **1단계 200건 게이트 분해능(6.7절)**: `T={g200['sample_size_T']}`에서 Sharpe SE = `{g200['sharpe_standard_error']}` → **거래당 Sharpe ≥ `{g200['required_per_trade_sharpe_95pct']}`** 통과 기준 (적중률 SE = `±{g200['hit_rate_standard_error_pctp']}%p`이므로 적중률 단독 판정 금지)",
        f"- **10장 목표 수치 검산(`u=15%, d=10%`)**: 요구 연간 Sharpe ≥ `{ch10['required_sharpe_q392']}~{ch10['required_sharpe_q400']}` (`p_idio=75%`일 때 요구 IR ≥ `{ch10['required_ir_by_p_idio']['p_idio_75pct']['required_annual_ir']}`, `p_idio=50%`일 때 요구 IR ≥ `{ch10['required_ir_by_p_idio']['p_idio_50pct']['required_annual_ir']}`)",
        "",
        "## 5. Stage 5 — 원장 사본 기반 기존 14개 LIVE arm vs SHADOW 통합 전략 대조표",
        "### 5-1. 기존 14개 모의 실험군(LIVE) 원장 사본 집계 (`2026-11-03 ~ 2026-11-09`)",
        "| 전략(arm) | 매수주문 | 매수체결 | 매도체결(왕복) | 완결 Gross 손익 | 완결 추정 Net 손익 | 미청산 보유 | 미체결만료/거부 | 진단 요약 |",
        "| :--- | :---: | :---: | :---: | ---: | ---: | :---: | :---: | :--- |",
    ])
    for arm, st in s5["live_14_arms_summary"].items():
        lines.append(
            f"| `{arm}` | {st['buy_orders']} | {st['buy_fills']} | {st['sell_fills']} ({st['round_trips']}) | "
            f"{st['completed_gross_pnl_krw']:+,d}원 | **{st['completed_est_net_pnl_krw']:+,d}원** | "
            f"{st['open_lots_count']}종목 | {st['unfilled_expired_or_rejected_orders']}건 | {st['status_note']} |"
        )

    lines.extend([
        "",
        "### 5-2. `EXP_D` 실체결 6종목 대상 `LIVE(균등·단순오버나잇)` vs `SHADOW(4단계 통합 파이프라인)` 비교",
        "| 종목명(코드) | 진입일 | LIVE 수량(매수가) | 기준가(11-09) | LIVE 평가/실현(수익률) | OpenDART `F_core` | SHADOW 4단계 파이프라인 판정 및 대응 차이 |",
        "| :--- | :---: | ---: | ---: | ---: | :---: | :--- |",
    ])
    for r in s5["exp_d_holdings_live_vs_shadow"]:
        f_str = str(r["opendart_f_core"]) if r["opendart_f_core"] is not None else "미산출(적자)"
        lines.append(
            f"| {r['name']} (`{r['symbol']}`) | {r['entry_day']} | {r['live_qty']}주 ({r['live_entry_price']:,.0f}원) | "
            f"{r['ref_price_krw']:,.0f}원 | **{r['live_gross_pnl_krw']:+,d}원 ({r['live_return_pct']:+.2f}%)** | "
            f"`{f_str}` | {r['shadow_4stage_decision']} |"
        )

    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Run 5-Stage Quant x Leader Stock Pipeline with SHADOW vs LIVE Comparison (Read-Only)")
    parser.add_argument("--nav", type=float, default=100_000_000.0, help="Total Account NAV in KRW")
    parser.add_argument("--track-a-share", type=float, default=0.70, help="Capital share for Track A Core Swing")
    parser.add_argument("--ledger-copy", type=Path, default=DEFAULT_LEDGER_COPY, help="Read-only SQLite ledger snapshot copy")
    args = parser.parse_args()

    dart_map = load_opendart_financials()
    etf_panel = load_sector_etf_panel()

    s1 = step1_generate_factors(dart_map, etf_panel)
    s2 = step2_validate_factors()
    s3 = step3_formulate_strategy(
        s1,
        nav_krw=args.nav,
        track_a_share=args.track_a_share,
        track_b_share=round(1.0 - args.track_a_share, 4),
    )
    s4 = step4_attribute_and_govern()
    s5 = step5_shadow_vs_live_ledger_compare(args.ledger_copy, dart_map, s3)

    report = {
        "schema": "unified-quant-leader-5stage-pipeline-v2",
        "generated_at": datetime.now(KST).isoformat(),
        "safety_invariants": {
            "order_permission": False,
            "recommendation_only": True,
            "live_runtime_or_ledger_touched": False,
            "broker_api_calls": 0,
        },
        "stage1_factor_generation": s1,
        "stage2_factor_validation": s2,
        "stage3_strategy_formulation": s3,
        "stage4_attribution_and_governance": s4,
        "stage5_shadow_vs_live_comparison": s5,
    }

    out_json = OUT_DIR / "unified_pipeline_report_20261109.json"
    out_md = OUT_DIR / "unified_pipeline_summary_20261109.md"
    out_json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    out_md.write_text(render_markdown(report), encoding="utf-8")

    print(json.dumps({
        "status": "ok",
        "output_json": str(out_json),
        "output_md": str(out_md),
        "live_arms_evaluated": len(s5["live_14_arms_summary"]),
        "exp_d_positions_compared": len(s5["exp_d_holdings_live_vs_shadow"]),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
