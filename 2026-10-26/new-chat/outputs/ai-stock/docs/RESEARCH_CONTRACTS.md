# 오프라인 연구 계약

이 도구는 연구 입력과 가설을 고정하고 파일 변경·미래정보 사용 여부를 점검할 기반이다. 증권사·텔레그램·자동주문·수집 일정에 연결하지 않는다. 기존 `gdcrs`/`ddcrs` 매매 조건에 연구 필터를 추가하지 않는다.

## 입력 파일과 가설 고정

다음 구조의 JSON을 만든다. 한 프로토콜에는 하나의 시장·통화만 넣는다. 계정·키·토큰은 넣지 않는다.

```json
{
  "protocol_id": "synthetic-check-v1",
  "hypothesis": "합성 입력에서 완료봉 계산을 검증한다",
  "as_of": "2026-01-05T16:00:00+09:00",
  "market": "KR",
  "currency": "KRW",
  "universe": "합성 종목만",
  "selection": "사전에 고정한 전체 합성 표본",
  "planned_trials": 1,
  "cost_assumptions": "편도 5bp 가정; 실제 부과 비용 아님",
  "limitations": ["호가·유동성 미포함", "실제 계좌 성과 검증 아님"]
}
```

```sh
python main.py research freeze --spec protocol.json --root . --file protocol.json --file bars.csv --file config.json --output research-manifest.json
python main.py research audit --root . --manifest research-manifest.json
```

선택한 파일만 읽으며 파일별 SHA256·바이트 길이와 입력 집합 SHA를 저장한다. 같은 경로, 심볼릭 링크, 상위 경로 접근, 누락·변조·초과 크기는 거부한다. 기존 manifest는 덮어쓰지 않는다. 저장 시각은 실제 생성 시각이다. 과거 `as_of`보다 늦게 고정했다면 `retrospective_protocol=true`로 보고한다.

해시 일치는 원본의 진위, 당시 이용 가능성, 수익성, 배포 준비 완료를 의미하지 않는다. 입력 파일의 출처·관측시각·정정 이력·전체 표본 분모는 별도 증거가 필요하다. 프로토콜과 결과를 모두 바꾼 공격을 탐지하는 전자서명 장치는 아니다. CLI는 원문 입력이나 임의 오류 문자열을 출력하지 않는다.

## 시간과 검증 창

`CandleEvidence`는 공급자·시장·거래소·종목, 원본 봉 시각, 실제 관측시각, 정정시각, 간격, `START`/`END` 의미를 명시한다. 이용 가능 시각은 완료시각·관측시각·정정시각의 최댓값이다. 백필·수정 자료를 과거 판단 시각에 소급하지 않는다. 공급자의 종료시각 계약을 다른 공급자의 시작시각 계약에 복사하지 않는다.

`LabelledSample`/`walk_forward_splits`는 시간순 expanding-window 분할을 제공한다. 같은 시각의 종목들은 한 검증 창에 남기고 검증 창끼리 겹치지 않는다. 학습 라벨이 검증 경계/embargo를 넘거나 라벨 이용 가능 시각이 미확인이면 제외한다. 당시 관측 불가능했던 입력, 라벨 미상, purge/embargo 제거 수를 따로 공개한다. 마지막 불완전한 검증 창은 연장하지 않는다.

이는 최적화기나 수익성 인증이 아니다. 전체 시장 선택편향, 반복 종목 의존성, 다중 가설 검정, 평가기간 말의 미완결 라벨, 비용·체결 가능성은 연구자가 별도로 검증해야 한다. `READY_FOR_OFFLINE_RESEARCH`는 자료 분할이 존재한다는 뜻이며 전략 승인 상태가 아니다.

## 기존 시뮬레이터의 산출물

`demo`와 `simulate`는 기존 동작을 유지하면서 다음 정보를 추가한다.

- 다음 **관측 봉** 시가의 전량 가상체결, 편도 수수료·슬리피지 가정.
- 세금·호가 우선순위·부분체결·유동성·누락 봉 사이의 가격 경로는 모델링하지 않았다는 표시.
- 입력 봉·중복 봉·신호·가상체결·거절·마지막 미체결 수.
- 결과/입력/설정 파일의 SHA, 실행 Python, 시뮬레이션 모듈 SHA를 기록한 `evidence-manifest.json`.

이 파일은 실행 후 증거 영수증이므로 `preregistered=false`다. 실제 계좌수익률·MDD·증권사 체결 확인으로 승격하지 않는다. KRW와 USD를 합산하지 않는다.
