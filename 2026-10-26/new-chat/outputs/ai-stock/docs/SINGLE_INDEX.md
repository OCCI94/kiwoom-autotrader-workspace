# 단일지수 초과수익 회귀 — 연구 전용

`kiwoom_autotrader.research.single_index.fit_single_index`는 사용자가 준비한 기간별
종목·비교대상·무위험 수익률로 단일요인 회귀를 수행합니다. 자동주문이나 네트워크,
증권사 계좌·설정에 접근하지 않습니다.

```text
종목수익 − 무위험수익 = α + β × (비교대상수익 − 무위험수익) + 오차
```

수익률은 모두 소수 단위입니다. 1%는 `0.01`, −2%는 `-0.02`입니다. α는 선택한 한
기간의 수익률 단위이며 연환산하지 않습니다. 무위험률은 종목·비교대상과 같은 기간
및 통화에 맞춰야 합니다. 값을 모른다면 0으로 자동 채우지 않습니다.

## 입력과 시점

함수는 동일 기간의 결과를 한 행으로 받은 사전 목록을 요구합니다. 필수 행 필드는 다음과
같습니다. 모든 시간은 UTC 오프셋을 포함한 ISO 8601 문자열 또는 timezone-aware
`datetime`입니다.

JSON 입력 파일에 `context`, `as_of`, `training_end`, `analysis_mode`, `rows`,
`min_observations`를 모두 명시하면 다음과 같이 오프라인 CLI에서도 실행할 수 있습니다.
기존 결과 파일은 덮어쓰지 않습니다.

```sh
python -m kiwoom_autotrader.research single-index --input 입력.json --output 결과.json
```

- `period_start`, `period_end`: 같은 종목·비교대상·무위험률 기간의 시작과 종료.
- `asset_return`, `market_return`, `risk_free_return`: 해당 기간의 소수 수익률.
- `asset_available_at`, `market_available_at`, `risk_free_available_at`: 각 원자료가
  이 프로그램에 실제 이용 가능해진 시각. 종목·시장 수익률의 이용 가능 시각은 해당 기간의
  종료보다 이를 수 없습니다.

행은 시간순이며 서로 겹치면 안 됩니다. 종목과 비교대상의 시장·장 시간·가격조정 및
수익률 기간이 실제로 일치하는지는 자료 준비자가 확인해야 합니다. 이 함수는 공급자
시세의 정규장 여부나 권리 조정, 휴장일, 공시 발생 시각을 인증하지 않습니다.

`context`에는 `asset`, `benchmark`, `currency`(`KRW` 또는 `USD`), `frequency`
(`daily` 또는 `monthly`), `session`, `return_basis`, `asset_source`,
`benchmark_source`, `risk_free_source`, `universe_selection`, `benchmark_role`
(`market_index` 또는 `peer_proxy`)을 문자열로 명시합니다. `peer_proxy`는 시장지수가
아니므로 결과를 시장 베타나 CAPM 결과로 해석하지 않습니다. 무위험률을 관측하지 못해
0을 명시적 가정으로 사용하는 연구는 `risk_free_source`를 정확히
`assumed_zero_not_observed`로 쓰고 각 행의 무위험률을 0으로 넣습니다. 이때 α는
위험조정 성과가 입증된 Jensen 알파가 아닙니다.

```python
from kiwoom_autotrader.research.single_index import fit_single_index

result = fit_single_index(
    rows, context=context,
    as_of="2026-10-27T16:00:00+09:00",
    training_end="2026-06-30T16:00:00+09:00",
    min_observations=24,
    analysis_mode="point_in_time",
)
```

기본 `point_in_time`에서는 학습 종료 당시 이용 가능한 자료만 학습합니다.
`retrospective_descriptive`는 나중에 한꺼번에 수집한 과거 자료를 사후적으로 탐색하기
위한 별도 모드입니다. 이 경우 과거 cutoff 때 자료를 알았다고 주장하지 않으며, 이후 기간을
나눠 계산해도 전향 표본외 검증이라고 부를 수 없습니다. 어느 모드에서도 입력의 실제
이용 가능 시각이나 종목 사전 선정 여부를 함수가 독립 확인하지 않습니다.

결측·비정상 수익률, 이용 가능 시각 미확인, 학습/평가 경계를 가로지르는 행 등은
`excluded`에 사유와 함께 남습니다. 누락을 0으로 대체하지 않습니다. 최소 표본은
기본 24개로, 통계적 유효성 보증 기준이 아닙니다. 시장 요인 분산이 0이면 추정하지
않습니다.

## 결과 해석

`fit`에는 α, β, R², 표본 수와 통상적인 OLS 표준오차 및 t 통계량이 포함됩니다.
해당 표준오차는 독립·등분산 가정을 따르며 시계열 자기상관이나 이분산에 대한 보정이
없습니다. p값과 신뢰구간은 계산하지 않고 `null`로 반환합니다. 결측이나 상수인
종속변수에는 R²를 만들지 않습니다.

`evaluation`은 학습 기간 이후 **실현된 같은 기간의 시장 수익률**을 넣은 조건부 설명
오차입니다. 기간 시작 시점에 알 수 있는 진입 신호의 예측력이 아니며 거래비용·유동성·
체결·포트폴리오 수익률도 평가하지 않습니다. 동일한 데이터를 여러 번 확인하고 고른
종목이나 파라미터에는 사후 선택 편향이 남습니다. 배포나 매매 연결 전에 별도 전향
표본, 적절한 비교군, 비용 및 검정 방법이 필요합니다.
