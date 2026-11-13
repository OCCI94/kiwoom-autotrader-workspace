# 이전 매매이력의 오프라인 수입

이 기능은 명시적으로 작성된 `ai-stock-history-v1` bundle을 검증하고 **별도 이력 SQLite**에 보존한 뒤, 계보·코호트·통화별 읽기 전용 보고서를 출력합니다. Python 표준 라이브러리만 사용합니다. 거래 서비스, 계좌, 주문 제출, 토큰, 키체인, Telegram, 시뮬레이터 초기 자산에 연결하지 않습니다.

현재 암호화된 실제 이력은 보류 상태입니다. DMG를 열거나 실제 행을 수입하지 않았으며, 구환경 SQLite의 테이블·열을 추정하는 adapter도 없습니다. 테스트는 `tests/test_history_import.py`에서 생성하는 **합성 데이터**만 사용합니다. 과거 문서의 41개 주문·20개 왕복거래 등의 수치는 이번 구현이 확인한 실데이터 건수가 아닙니다.

## 실행

프로젝트 루트에서 실행합니다. 경로는 사용자가 준비한 canonical bundle과 독립된 이력 DB를 명시해야 합니다.

```sh
python3 -m kiwoom_autotrader.history validate --bundle /absolute/path/canonical-bundle
python3 -m kiwoom_autotrader.history import --bundle /absolute/path/canonical-bundle --database /absolute/path/history-store/historical.sqlite3
python3 -m kiwoom_autotrader.history report --database /absolute/path/history-store/historical.sqlite3
python3 -m kiwoom_autotrader.history report --database /absolute/path/history-store/historical.sqlite3 --cohort kr_mock
```

각 명령은 JSON을 표준 출력으로 내보냅니다. 성공은 종료 코드 `0`, 검증·충돌·파일 실패는 `2`입니다. `validate`는 DB를 만들지 않습니다. `import`만 별도 이력 DB를 생성하거나 갱신합니다. `report`는 SQLite `mode=ro&immutable=1`, `query_only=ON`으로 읽고 파일이 읽는 도중 바뀌면 실패합니다. 보고서를 별도 JSON 파일로 보관하려면 셸 리다이렉션을 사용합니다.

DB 파일명은 `historical.sqlite3` 또는 `*.history.sqlite3`만 허용합니다. bundle 내부를 목적지로 지정하거나, 심볼릭 링크 또는 링크를 거치는 상위 경로를 사용하면 거절합니다. 링크가 포함된 macOS `/var` 등의 경로는 실제 경로를 명시해야 합니다. 기존 DB는 전용 `application_id`, 정확한 자체 테이블·트리거 정의, 독립 `store_uuid` 및 생성 시각이 있어야 합니다. 운영 DB를 이 이름으로 바꾸어도 식별 검사를 통과하지 못합니다. UUID는 이력 저장소를 만들 때 새로 생성하며 운영 프로젝트의 UUID를 추정하거나 재사용하지 않습니다.

대상 DB와 원본은 다른 프로그램이 수정하지 않는 오프라인 파일이어야 합니다. `-wal`, `-shm`, `-journal`이 있으면 운영 DB 복구나 WAL 결합을 시도하지 않고 거절합니다. readonly 접속은 sidecar를 만들지 않습니다. 원본 파일은 일반 파일만 허용하며 읽기 전후 식별자·크기·수정 시각을 확인합니다. 임의의 동시 파일 교체를 견디는 보안 격리 환경을 제공하는 도구는 아닙니다.

## Canonical bundle 계약

bundle은 디렉터리이며 다음 네 파일을 사용합니다. 추가 파일은 읽거나 실행하지 않습니다. JSON은 UTF-8이어야 하고 중복 키, NaN/Infinity, 알 수 없는 필드, 빈 JSONL 행은 거절합니다. 모든 명시된 필드가 필요하며 모르는 값은 허용된 필드에서만 `null`로 보존합니다.

| 파일 | 내용 |
| --- | --- |
| `manifest.json` | 아래의 정확한 manifest 객체 |
| `orders.jsonl` | 줄마다 주문 집계 객체 하나 |
| `roundtrips.jsonl` | 줄마다 왕복거래 집계 객체 하나 |
| `links.jsonl` | 줄마다 주문–왕복거래 연결 객체 하나 |

manifest의 최상위 필드는 정확히 `schema`, `bundle_kind`, `sources`, `files`입니다.

| 필드 | 값 |
| --- | --- |
| `schema` | `ai-stock-history-v1` |
| `bundle_kind` | `synthetic_fixture` 또는 `preserved_history`; 실제 자료를 합성 자료로 표시하지 않습니다. |
| `sources` | 아래 source 객체 1~1,000개 |
| `files` | 아래 file 객체 정확히 3개, 각 entity 한 번씩 |

각 source의 필드는 다음과 같습니다.

| 필드 | 의미와 제약 |
| --- | --- |
| `lineage_id` | 원본 계보의 명시적 식별자. 재압축·재포장만으로 변경하지 않습니다. |
| `cohort` | `kr_mock`, `kr_shadow`, `us_shadow` |
| `currency` | 앞의 두 코호트는 `KRW`, 마지막은 `USD` |
| `snapshot_sha256` | 출처 스냅샷의 선언된 소문자 64자리 SHA-256. 원본이 이 bundle에 없으므로 이 선언 자체를 재검증했다고 주장하지 않습니다. |
| `pseudonym_scheme` | 익명 참조번호 생성 방식의 명시적 식별자 |
| `snapshot_recorded_at` | `null` 또는 아래 timestamp 객체, 의미는 `snapshot_saved_at` |
| `source_completeness` | 반드시 `partial_or_unknown` |
| `expected_counts` | `orders`, `roundtrips`, `links` 각각의 정수 건수. 해당 계보·코호트의 이 snapshot 전체 행수입니다. |

계보·참조·익명화 식별자는 `[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}`를 만족해야 합니다. 불명확한 출처 계보를 파일명이나 현재 계좌번호로 추정하지 않습니다. 서로 다른 shadow 스냅샷에서 익명 참조번호가 같다는 이유만으로 동일 거래로 합치지 않습니다. 별도 계보로 보존된 snapshot들도 합산하지 않습니다.

각 file 객체에는 `entity`, `path`, `sha256`, `bytes`, `rows`가 필요합니다. `entity`는 `orders`, `roundtrips`, `links` 중 하나이며 `path`는 각각 `orders.jsonl`, `roundtrips.jsonl`, `links.jsonl`이어야 합니다. SHA-256과 바이트 길이는 **파일 원래 바이트 전체**를 대상으로 하고, 행수는 그 JSONL 파일의 모든 행수와 일치해야 합니다. 압축 해제나 경로 탐색은 지원하지 않습니다.

숫자가 들어가는 아래 필드는 JSON 숫자가 아닌 **원문 십진 문자열 또는 `null`**입니다. 예: `"002.00"`, `"200.0000000000000000001"`, `"1e-8"`. 문자열은 80자 이하, 지수는 최대 3자리이며 유한 Decimal이어야 합니다. 합계는 Decimal 정밀도 4,096자리로 계산하고 저장 payload의 숫자 문자열을 바꾸지 않습니다. 원문이 `null`인 값을 `"0"`으로 변환하지 않습니다. 금액·가격 단위는 source의 통화, 수량 단위는 주식 수입니다. 원본의 단위가 확인되지 않으면 adapter가 추정해서 채울 수 없습니다.

모든 행에는 `lineage_id`, `cohort`, `record_ref`가 필요합니다. source에 선언된 계보·코호트가 있어야 합니다. `symbol`은 1~32자의 영문·숫자와 `.`, `_`, `-`를 허용하며 첫 글자는 영문·숫자입니다.

| Entity | 나머지 정확한 필드 |
| --- | --- |
| `orders` | `symbol`, `side`, `status`, `requested_quantity`, `filled_quantity`, `requested_price`, `filled_amount`, `commission`, `tax`, `cost_state`, `created_at`, `updated_at`, `actual_fill_at`, `decision_reason` |
| `roundtrips` | `symbol`, `quantity`, `buy_amount`, `sell_amount`, `gross_pnl`, `commission`, `tax`, `net_pnl`, `cost_state`, `opened_at`, `closed_at`, `entry_reason`, `exit_reason` |
| `links` | `order_ref`, `roundtrip_ref`, `allocated_quantity` |

`side`는 `BUY`/`SELL`, `status`는 `NEW`, `ACCEPTED`, `PARTIAL`, `FILLED`, `CANCELLED`, `REJECTED`, `UNKNOWN`입니다. 알려진 체결 수량은 알려진 주문 수량을 넘을 수 없습니다. **취소 상태와 양수의 부분 체결 수량은 함께 보존**합니다. 취소를 미체결로 바꾸거나 잔고 변화로 체결을 생성하지 않습니다.

금액·수량은 음수를 허용하지 않고 `gross_pnl`, `net_pnl`만 음수일 수 있습니다. 값이 제공된 `requested_price`, 왕복거래 `quantity`, 연결 `allocated_quantity`는 양수여야 합니다. nullable 집계는 그대로 남기므로 데이터 부족이 0주·0원으로 표현되지 않습니다.

`cost_state`는 `unknown` 또는 `assumed`입니다. `kr_mock`은 `unknown`만 허용합니다. `unknown`이면 `commission`, `tax`, 왕복거래의 `net_pnl`은 모두 `null`입니다. shadow의 `assumed`는 수수료·세금 문자열을 명시해야 하며, 실확인 비용으로 승격하지 않습니다. 원본 gross/net 식의 잔차는 `arithmetic_residuals`에 보고하고 수치 자체를 고치거나 반올림하지 않습니다.

link의 주문과 왕복거래는 같은 계보·코호트·종목이어야 합니다. 동일 주문–왕복거래 쌍의 중복 연결은 거절합니다. 알려진 연결 수량의 합은 그 주문의 알려진 체결 수량 또는 왕복거래의 해당 매수/매도 측 수량을 넘을 수 없습니다. 미확인 연결이나 미완료 왕복거래를 완성하기 위해 누락 행을 만들지는 않습니다.

## 시각 의미와 PIT

timestamp 객체는 다음 세 필드만 가집니다.

```json
{"raw":"2026-10-14 09:00:00","timezone":null,"meaning":"order_created_at"}
```

`raw`와 null이 아닌 `timezone`은 1~80자 표시 가능한 문자열입니다. 원문을 날짜로 재해석·정규화하지 않으므로 timezone이 없는 값에 KST/UTC를 붙이지 않습니다.

| 필드 | 고정 `meaning` |
| --- | --- |
| `snapshot_recorded_at` | `snapshot_saved_at` |
| `created_at` | `order_created_at` |
| `updated_at` | `order_updated_at` |
| `opened_at` | `ledger_opened_at` |
| `closed_at` | `ledger_closed_at` |

이 v1 계약은 집계 이력만 다룹니다. `actual_fill_at`, `decision_reason`, `entry_reason`, `exit_reason`은 반드시 `null`입니다. 생성·수정·청산 시각을 독립적으로 확인된 브로커 체결 시각으로 바꾸지 않습니다. 원시 부분 체결 이벤트나 판단문을 복원한 것처럼 만들지 않습니다. raw 시각은 payload 대조에 포함하지만 날짜 파싱 근거가 없으므로 최초/최종 체결 시각의 정렬 통계는 제공하지 않습니다.

저장소의 `known_at`은 실제 **로컬 수입 관측 시각**입니다. 재수입 때 바꾸지 않으며 과거 주문·판단 시각으로 소급하지 않습니다. 보고서의 `pit_eligible`은 항상 `false`입니다. 기존 메시지 팩터나 백테스트에 자동으로 결합하지 않습니다. 장래 PIT 활용에는 원자료의 관측·공개·수정 시각과 별도 검토된 결합 규칙이 필요합니다.

## 중복·충돌·트랜잭션

원본 정체성은 `(lineage_id, cohort, entity, record_ref)`입니다. 포장 파일 해시만을 dedup 키로 쓰지 않습니다. 행·JSON 키 순서나 공백이 달라진 재포장은 새 manifest 영수증만 남고 같은 사실 행은 추가하지 않습니다. 원문 금액 문자열이 `"2.0"`에서 `"2.00"`으로 바뀌어도 같은 정체성의 다른 payload로 취급합니다.

source는 하나의 완전한 **선언 snapshot**입니다. 같은 source의 metadata, snapshot SHA, 기대 건수 또는 entity별 참조번호 전체 집합을 바꾸거나 기존 행을 바꾸면 전체 수입을 거절합니다. 참조번호만 바꿔 같은 snapshot을 두 번 적재할 수 없습니다. 확인되지 않은 증분 업데이트를 같은 계보에 덧붙이는 기능은 없습니다.

충돌 시 기존 사실은 그대로 남고 `historical_conflicts`에 정체성·기존/유입 SHA·manifest SHA·관측 시각을 남깁니다. 충돌한 원문 전체를 별도 테이블에 복제하지 않으므로 해당 bundle을 보존해야 후속 검토할 수 있습니다. 동일 충돌은 중복 기록하지 않습니다. 자동 승자 선택·덮어쓰기·과거 행 삭제는 하지 않습니다.

일반 수입은 입력 전체 검증 후 `BEGIN IMMEDIATE` 안에서 기존 사실 재검증, 충돌 검사, 삽입, 모든 canonical payload의 저장 후 재독 대조를 수행합니다. 중간 실패는 새 source·행·수입 영수증을 모두 롤백합니다. 첫 실패 뒤 빈 전용 DB와 store UUID만 남을 수 있으며 실패한 사실은 남지 않습니다. 동시 수입도 같은 검사·쓰기 잠금을 사용합니다.

DB에는 `historical_meta`, `historical_sources`, `historical_orders`, `historical_roundtrips`, `historical_links`, `historical_imports`, `historical_conflicts`만 있습니다. 사실 테이블의 수정·삭제 방지 트리거, `trusted_schema=OFF`, 임의 view/virtual table/추가 trigger 거절, SQL 실행량·DB 크기·행수 상한을 적용합니다. 파일 자체의 SHA는 서명이나 신뢰할 수 있는 외부 출처 증명이 아닙니다.

## 보고서 해석

보고서도 payload SHA, canonical 원문, DB 키, schema, 연결 참조, source별 기대 건수를 다시 검사합니다. 변조나 불일치가 발견되면 `historical_import_verified=true`를 출력하지 않고 실패합니다. 각 계보·코호트를 별개로 표시하고 KRW/USD, MOCK/shadow, 다른 snapshot을 합산하지 않습니다.

| 출력 | 뜻 |
| --- | --- |
| `historical_import_verified` | 성공적으로 저장된 canonical 입력의 무결성·선언 참조 대조 범위. 실제 계좌 또는 회계 일치를 의미하지 않음 |
| `evidence_scope` | `canonical_payload_integrity_and_declared_references_only_not_accounting_or_broker_verification` |
| `authoritative_history_complete` | 항상 `false` — 최신 권위 원본·WAL·누락 기간 확인은 별개 |
| `current_account_verified` | 항상 `false` |
| `deployed` | 항상 `false` — CLI 실행이 거래 서비스 배포를 의미하지 않음 |
| `source_completeness` | 항상 `partial_or_unknown` |
| `confirmed_net_pnl`, `confirmed_win_rate`, `account_return`, `mdd` | 모두 `null` |

각 숫자 필드의 `sum_of_known`은 알려진 값만의 Decimal 합이며 `known_count`, `null_count`가 함께 표시됩니다. 전부 NULL일 때의 누산기 값 `"0"`은 비용이 0원임을 뜻하지 않습니다. shadow의 가정 비용·원문 net, MOCK의 원문 gross와 회계 잔차는 구분해서 읽어야 합니다. `arithmetic_residuals`가 비어 있어도 비용의 진실성이나 전체 회계 완전성을 증명하지 않습니다.

운영 체결 원장의 실제 주문번호·체결번호로 익명 참조를 바꾸지 않습니다. 현재 포지션, 현금, 당일 손익, 예약 주문, 매매 활성화 플래그, 재시도·중복 방지 마커를 변경하지 않습니다. 시뮬레이션 결과나 현재 운영 손익에 가져온 이력을 더하는 기능도 없습니다.

## 상한과 추후 adapter

manifest는 1 MiB, 각 JSONL은 16 MiB, 한 행은 1 MiB, entity별 입력은 100,000행까지입니다. 저장소는 128 MiB, source는 1,000개, 그 외 사실·수입·충돌 테이블은 각 100,000행까지입니다. 상한을 넘으면 분리·검토된 별도 저장소를 사용해야 하며 부분 성공으로 처리하지 않습니다.

`kiwoom_autotrader.history.adapters.HistoricalAdapter`는 향후 검토된 변환기의 인터페이스만 정의합니다. 실제 archive 접근이 별도로 허용된 뒤 원본 schema, 각 필드의 단위/NULL/시각 의미, 원본·WAL 관계, 계보와 익명화 범위를 확인해야 adapter를 작성할 수 있습니다. adapter는 원본을 수정하지 않고 canonical bundle을 만들며 결과는 이 validator를 다시 통과해야 합니다. 자동 복호화, 패키지 스크립트 실행, 외부 계좌 조회는 이 인터페이스의 역할이 아닙니다.

회귀 테스트 실행:

```sh
python3 -m unittest discover -s tests -p 'test_history_import.py' -v
```

합성 fixture는 세 코호트, 고정 소수 원문, NULL 비용, 부분 체결 후 취소를 포함합니다. 재포장 멱등성, 참조 집합 교체 충돌, 동시 수입 경합, 중간 실패 롤백, 저장 데이터·스키마 변조, 운영 DB·symlink·FIFO·sidecar 거절, 읽기 전용 보고서, 네트워크 미사용을 검증합니다.
