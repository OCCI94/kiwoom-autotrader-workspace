---
name: quant-leader-portfolio
description: Evaluates saved Korean equity and sector ETF evidence with the eight-phase leader-stock and quant portfolio formulas. It applies to offline requests for "주도주 수식", "퀀트 포트폴리오 배분", "COMP F_comp 교차검증", "75% 수축 MV", "25% 섹터 워터필링", "노이만 헤지", "XSE XSTSE 손익 분해", and "일일 주문서 계산".
license: LicenseRef-Local-Workspace
compatibility: macOS, Python 3.9+ standard library. Requires the existing ai-stock research workspace and its saved inputs. No credentials, network, or trading access.
metadata:
  author: tr-oh
  version: "1.0"
  created: "2026-11-06"
allowed-tools: Read Bash(python3:*)
---

# 주도주 퀀트 포트폴리오

보존 자료로 수식을 검산하고 후보 배분과 실험 손익을 연구한다. `order_permission: False`, `recommendation_only: True`를 고정한다. 운영 수신기·원장·설정·키체인·토큰·API와 접촉하지 않는다. `outputs/ai-stock` 패키지, 주도주 원전 4개 모듈과 기존 테스트는 읽기 전용으로 둔다.

## 수식 단계

| 단계 | 평가 |
| --- | --- |
| 1 | 거시 유동성·금리곡선과 섹터 ETF 상대 모멘텀·펀더멘털·수급 |
| 2 | COMP·F_comp와 공개 시점 기준 OpenDART F_core 교차검증 |
| 3 | 목표가와 고정 손절폭 기반 3:1 게이트, WLS 팩터 직교화 |
| 4 | 75% 섹터 분산 수축 MV, 보완 MVO, 위험 조정·비례 배분과 종목·유동성·섹터 상한 |
| 5 | 유효종목수·고유 변동성 타겟과 지속 가능 레버리지 |
| 6 | 팩터·고유 위험 기여와 노이만 외부 헤지 |
| 7 | 시장충격·이벤트 분할 궤적과 5단계 매도 상태 검산 |
| 8 | 고유 손익의 종목 선정·비중·시점 분해와 합계 대사 |

## 실행

이 스킬의 [CLI](scripts/run_quant_leader_skill.py)를 `python3 -I -B`로 실행한다.

```sh
python3 -I -B /Users/tr-oh/Documents/Codex/skills/quant-leader-portfolio/scripts/run_quant_leader_skill.py --mode verify
python3 -I -B /Users/tr-oh/Documents/Codex/skills/quant-leader-portfolio/scripts/run_quant_leader_skill.py --mode daily-sheet --nav 100000000 --sizing-mode shrinked_mv --sector-cap 0.25
python3 -I -B /Users/tr-oh/Documents/Codex/skills/quant-leader-portfolio/scripts/run_quant_leader_skill.py --mode xse-xstse --snapshot /absolute/path/to/saved-snapshot.json
```

- `verify`는 기존 Phase 1~8 검산용 입력으로 실행한다. `daily-sheet`는 저장 OpenDART와 기존 엔진의 시연용 가격·리스크·보유 가정을 구분해서 표시한다. 로컬 임시 산출물은 실행 후 제거하며 JSON만 stdout에 반환한다.
- `xse-xstse`에서 `--snapshot` 없이 사본을 쓰려면 `--ledger-copy`, `--ledger-manifest`, `--start-day YYYYMMDD`, `--end-day YYYYMMDD`를 함께 지정한다. 사본은 `mode=ro`와 `query_only`로만 읽는다. 체결과 장전 익스포저·팩터 잔차의 증거를 구분한다. 입력을 생략하면 Phase 8 검산 표본만 반환한다.
- 스냅샷은 작업 루트 `work/` 또는 `outputs/ai-stock-research/reports/`에 둔다. `quant-xse-xstse-snapshot-v1`의 `evidence_kind`, `as_of`, `days`, `return_model`, `rows`를 제공한다. 각 행은 `day`, `arm`, `positions`를 포함하고, 포지션에는 `symbol`, 부호 있는 `nmv_krw`, `residual_return`, `residual_volatility`를 넣는다. 실제 `saved_point_in_time` 증거의 시간 필드는 `work/quant_xse_xstse_experiment_audit.py --help`의 입력 계약을 따른다. 확인된 무보유는 빈 배열, 확인할 수 없는 보유는 null이다.
- 동일 후보의 균등·네 가지 배분은 작업 루트 `work/quant_leader_shadow_compare.py`를 사용한다. ETF는 `work/namuh_sector_etf_gate_bridge.py`에 저장 패널만 전달한다. 결측 입력과 미평가 게이트는 JSON의 null로 남긴다.
- 종료 코드 0은 정상, 1은 경고 또는 레버리지 지속 불가, 2는 검증 실패, 3은 사용법 오류다. 진단은 stderr를 사용한다.
