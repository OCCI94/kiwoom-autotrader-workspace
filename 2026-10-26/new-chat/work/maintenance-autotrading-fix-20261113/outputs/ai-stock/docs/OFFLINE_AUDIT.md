# 로컬 기록 오프라인 감사

이 명령은 지정한 원장·일별 CSV·listener 상태 파일을 읽고 고정 통계와 사유코드를
출력합니다. 기존 프로그램, 설정, 주문 기록, CSV, 서비스 상태를 변경하지 않습니다.
인증·시세·계좌·주문·Telegram·WebSocket·키체인 조회를 하지 않습니다.

## 실행

프로젝트 루트에서 지원되는 Python으로 실행합니다. 입력은 필요한 것만 지정할 수 있고,
출력의 부모 디렉터리는 이미 존재해야 합니다.

```sh
python -m kiwoom_autotrader.diagnostics \
  --ledger /absolute/path/.execution-ledger.sqlite3 \
  --csv /absolute/path/20261026.csv \
  --state /absolute/path/state.json \
  --output /absolute/path/new-audit.json \
  --html /absolute/path/new-audit.html
```

`--output`을 생략하면 JSON을 표준 출력으로 보여 줍니다. `--html`은 같은 감사 결과를
한국어 정적 화면으로 저장합니다. 외부 리소스·스크립트·자동 갱신·추가 조회 기능이 없습니다.
기존 출력 파일, 입력 파일 및 원장 sidecar 경로에는 덮어쓰지 않습니다.

종료 코드 0은 감사 결과를 출력했다는 뜻입니다. 각 검사 판정과 현재 매매 준비 상태는
보고서에서 따로 확인합니다. 출력 경로 충돌·쓰기 실패는 종료 코드 2이며, 원격 오류문이나
민감한 파일 내용을 오류 메시지에 넣지 않습니다.

Python API는 다음과 같습니다. 함수를 호출하기 전에는 파일을 읽지 않습니다.

```python
from kiwoom_autotrader.diagnostics import audit_local

report = audit_local(ledger_path=ledger, csv_path=daily_csv, state_path=state)
data = report.to_dict()
```

## 판정의 의미

| 판정 | 의미 |
| --- | --- |
| `PASS` | 해당 저장 기록의 검사 범위를 확인함 |
| `NOT_MET` | 저장 기록 사이의 불일치나 수량 위반을 확인함 |
| `OBSERVE` | 기록된 상태 또는 해당 거래가 없는 상태이며 실행 적격 판정이 아님 |
| `UNKNOWN` | 자료 없음·읽기 실패·범위 불명확 등으로 확인할 수 없음 |
| `UNSUPPORTED` | 다른 진단 구성요소에서도 사용할 수 있는 표준 미지원 판정 |

현재 계좌·미체결 주문·시계·시장·프로세스를 확인하지 않으므로 `readiness_state`는 항상
`UNKNOWN`, `account_reconciled`는 항상 `false`입니다. `inspection_completed=true`는
이 오프라인 검사가 끝났다는 뜻이며 모든 입력이 유효하다는 뜻이 아닙니다.
빈 DB는 저장된 이력이 없다는 의미입니다. 계좌가 비어 있거나 미체결·불명확 주문이
없다는 증거가 아닙니다. 저장된 `status=running`도 현재 프로세스의 실행 증거가 아닙니다.

## 검사 범위

- 원장의 의도 수·접수 주문 수·체결 행 수·체결이 기록된 주문 수·부분/전체 체결 수·접수 후
  체결 미기록 수를 별도로 셉니다. 취소된 사전 의도와 접수 미확정 의도도 구분합니다.
- 주문/체결의 날짜·모의/실전·scope·주문번호를 연결하고 종목·방향·전략 귀속을 확인합니다.
  주문 수량보다 큰 누적 체결과 중복 체결 식별자를 오류로 보고합니다.
- 저장된 자동매도 종목/일자/모드/scope별 비용 기록의 존재와 숫자 유효성을 확인합니다.
  증권사 비용의 최신성이나 원래 비용 산정의 정확성을 새로 검증하는 기능은 아닙니다.
- 선택 테이블 `order_decisions`와 `profit_observations`가 있으면 저장된 JSON의 SHA와
  의도/전략 또는 일자/종목 연결 및 근거 보유 범위를 검사합니다. 합성/증권사 관측 표시는
  별도로 셉니다. SHA 일치는 내용의 무결성 검사이며 실제 관측의 진실성·비용 정확성을
  증명하지 않습니다. 옛 원장에 근거 테이블이 없으면 미확인으로 남기고 과거 행을 채우지 않습니다.
- CSV의 주문/체결 식별자·가격·수량·전략을 원장과 비교하고 일일 매수·매도 합계를
  CSV 체결 행에서 다시 계산합니다. 실제 가격·수량을 출력에 복제하지 않습니다.
- 일별 CSV의 날짜는 체결 행과 `YYYYMMDD.csv` 파일명으로 확인합니다. 여러 날짜가 섞이거나
  충돌하면 보류합니다. CSV에 없는 여러 account scope가 섞이면 임의 귀속하지 않습니다.
- 상태 파일은 버전·허용된 상태·수신 카운터만 읽습니다. 임의 오류문·키·토큰·계좌·원본 행·
  입력 경로는 보고서에 출력하지 않습니다. 출처는 종류·바이트 수·SHA-256으로 남깁니다.

주요 사유코드는 `ORDER_OVERFILL`, `ORDER_FILL_ATTRIBUTION_MISMATCH`,
`UNRESOLVED_ORDER_INTENTS`, `COST_COVERAGE_INCOMPLETE`, `CSV_LEDGER_MISMATCH`,
`CSV_SUMS_MISMATCH`, `CSV_DUPLICATE_FILL_IDS`, `SOURCE_MISSING`,
`SOURCE_CHANGED_DURING_INSPECTION`입니다. 이 결과로 원장을 수정하거나 주문을 재전송하지 않습니다.

## 원본 보존과 한계

원본 SQLite를 직접 열지 않습니다. 읽은 바이트를 임시 디렉터리의 감사 사본으로 만들고
SQLite `mode=ro`, `query_only=ON`으로 열어 원본 WAL/SHM 변경을 피합니다.
원본에 비어 있지 않은 `-wal` 또는 `-journal`이 있으면 일관된 스냅샷 근거가 부족하므로
`LEDGER_ACTIVE_JOURNAL_SNAPSHOT_UNKNOWN`으로 보류합니다. 원본 writer를 멈추거나
checkpoint·복구·journal 결합을 수행하지 않습니다. 일관성이 확인된 별도 사본을 사용하세요.

입력의 SHA를 검사 뒤 다시 확인합니다. 원본이 바뀌면 `UNKNOWN`을 추가합니다.
파일들이 서로 다른 시점에 만들어졌을 수 있으므로 원장/CSV 불일치는 부분 저장이나
진행 중 갱신일 수도 있습니다. 보고서는 원인이나 현재 계좌 상태를 단정하지 않습니다.

자원 상한은 원장 64 MiB, CSV 32 MiB, 상태 64 KiB, 원장 테이블별/CSV 100,000행입니다.
SQLite는 실제 테이블만 읽고 뷰·가상 테이블을 거부합니다. 값 길이는 4 MiB+4 KiB,
모든 조회 열의 합은 16 MiB로 제한합니다. SQLite 실행에는 진행 콜백 기준 5초 한도를 적용합니다. 이는 파일 I/O를 포함한 전체
검사의 강제 종료 시간을 보장하지 않습니다. 상한 초과·지원하지 않는 스키마·손상 파일은
`UNKNOWN`으로 남기며 부분 결과를 정상 전체 결과로 취급하지 않습니다.

## 검증

```sh
python -m unittest discover -s tests -p test_offline_audit.py
```

임시 합성 원장·CSV·상태 파일만 사용합니다. 원본 바이트·mtime·sidecar 보존, 읽기전용
SQLite 연결, 누락·활성 journal·손상·초과체결·귀속/금액 불일치·비용 미확정·민감정보
비노출·HTML 이스케이프·출력 덮어쓰기 거부를 검증합니다. 이 테스트는 실제 계좌 대조,
운영 준비, 과거 데이터 복원 또는 전체 성능 검증의 근거가 아닙니다.
