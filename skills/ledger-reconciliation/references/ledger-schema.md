# 원장 스키마 (2026-11-03 사본에서 `.schema`/`PRAGMA table_info`로 확인)

파일: `~/Library/Application Support/KiwoomAutotrader/runtime/logs/.execution-ledger.sqlite3` (사본 987MB, 16:01). 열 이름만 적고 데이터는 적지 않는다. "불변"은 `BEFORE UPDATE`/`BEFORE DELETE` 트리거로 `RAISE(ABORT)`하는 표다. 거래 식별 키는 `(scope, day, mode)`이며 2026-11-03은 `scope='default'`, `mode='paper'`, `day='20261103'`이었다.

| 표 | 열 | 역할 |
|---|---|---|
| `intents` | scope, id, day, mode, side, symbol, quantity, strategy, created_at, order_number, state, reason | 주문 의도. `state`는 prepared → accepted(주문번호 확보) / cancelled. PK(scope,id). |
| `orders` | scope, day, mode, ord_no, symbol, side, strategy, quantity, registered_at | 증권사가 접수한 주문(원장 측 주문번호, 0패딩 없음). PK(scope,day,mode,ord_no). |
| `fills` | scope, day, mode, ord_no, fill_no, symbol, name, side, moment, price, quantity, strategy, remaining, holdings_at, holdings_status | 체결 투영. 주문당 한 행(`fill_no`=`ord_no`), `quantity`는 누계 스냅샷. `price`는 문자열. |
| `profits` | scope, day, mode, symbol, net, basis, reason | 종목별 일일 순손익 판정. 비용 대사가 안 끝나면 `net` NULL, `reason`에 `미확정: ...`. |
| `summaries` | scope, day, mode, digest, payload | 일일 요약 JSON(매수·매도 금액, `verified_totals`, 상태 문구). |
| `holdings` | scope, mode, observed_at, positions, discrepancy | 최신 보유 수량 투영 `{symbol: qty}`. `day` 열 없음, PK(scope,mode)라 한 행뿐. |
| `order_decisions` | scope, intent_id, recorded_at, order_price, order_type, evidence_json, evidence_sha256 | 주문가·주문유형 결정 증거(신호·설정 해시). 불변. |
| `pre_order_reconciliations` | scope, intent_id, recorded_at, evidence_json, evidence_sha256 | 주문 직전 잔고·미체결·원금 한도 대조 증거. 불변. |
| `execution_observations` | id, scope, day, mode, ord_no, fill_no, observed_at, kind, source_kind, digest, payload_json | 체결 투영의 변경 이력(`kind`: new_execution_key / holdings_reconciliation / repeated_snapshot). 불변. 인덱스 (scope,day,mode,ord_no,fill_no). |
| `broker_observations` | id, scope, day, mode, observed_at, source_api, digest, payload_json | 증권사 REST 페이지·스트림 이벤트 원문(`source_api`: 00, kt00009, ka10075, kt00007, ka10072, ka10073, ka10074). 불변. 인덱스 (scope,day,source_api,id). |
| `broker_observation_repeats` | id, scope, day, mode, observed_at, source_api, digest, original_row_id, receipt_json, receipt_digest | 같은 응답이 반복 관측됐을 때의 영수증(원본 행 id 참조). 불변. |
| `broker_observation_dedup_heads` | scope, epoch, request_key, original_row_id, response_sha256 | 요청 키별 마지막 응답 해시(중복 제거 헤드). |
| `profit_observations` | scope, day, mode, symbol, digest, observed_at, source_kind, payload_json | ka10072/ka10073 종목별 실현손익 응답 원문. 불변. 핫 카피에서 전체 스캔 시 malformed 발생 사례 있음. |
| `profit_sources` | scope, day, mode, symbol, digest | `profits`가 근거로 삼은 profit_observations digest. |
| `account_balance_daily` | scope, mode, day, observed_at, source_kind, payload_json | 일일 계좌 스냅샷(`paper-account-snapshot-v1`: cash, holdings[], totals, pages{kt00001, kt00018}). kt00018 잔고는 여기에 있다. PK(scope,mode,day). |
| `strategy_holding_observations` | scope, id, day, mode, symbol, observed_at, source_kind, held_qty, sellable_qty, purchase_amount, digest | 주문 직전 종목별 보유·매도가능 수량·매입금액 관측. 불변. |
| `paper_experiment_signal_claims` | scope, day, mode, symbol, strategy, signal_sha256, intent_id | 실험 arm 신호 1건당 주문 1건 보장(중복 방지). 불변. |
| `multi_strategy_exit_claims` | scope, generation_id, exit_kind, intent_id, claimed_at | 다전략 청산 청구(`exit_kind`: full). 불변. |
| `multi_strategy_exit_releases` | scope, intent_id, released_at, reason | 청산 청구 해제. 불변. |
| `b3_add_claims` | scope, initial_intent_id, add_intent_id, claimed_at | B3 추가매수 청구. 불변. |
| `b3_invalidations` | scope, initial_intent_id, symbol, observed_at, reason | B3 무효화. 불변. |
| `buy_ttl_cancellation_attempts` | scope, day, original_ord_no, intent_id, symbol, claimed_at, evidence_json, evidence_sha256 | 미체결 매수 TTL 취소 시도 증거(`evidence_json`: ttl_seconds, broker_remaining_qty, cancel_request_qty, ack_releases_principal, strategy, due_reason, order_window_end_at ...). 불변. |
| `buy_ttl_cancellation_outcomes` | scope, day, original_ord_no, observed_at, status, cancellation_ord_no, reported_cancel_quantity | TTL 취소 결과(`status` 값은 event-types.md). 불변. |
| `buy_ttl_cancellation_retry_attempts` | scope, day, original_ord_no, attempt_sequence, intent_id, symbol, claimed_at, evidence_json, evidence_sha256, previous_attempt_sha256, previous_outcome_sha256 | 재시도 시도(체인 해시). 불변. |
| `buy_ttl_cancellation_retry_outcomes` | scope, day, original_ord_no, attempt_sequence, observed_at, status, cancellation_ord_no, reported_cancel_quantity | 재시도 결과. 불변. |
| `paper_expired_order_certificates` | id, scope, day, ord_no, captured_at, payload_json, digest | 만료·취소 주문의 종결 인증서(정산 확정 증거). 불변. 11-03 사본에는 0건. |
| `sqlite_sequence` | name, seq | AUTOINCREMENT 내부 표. |

## 대조에 쓰는 조인 키

- 주문: `orders.ord_no` ↔ kt00009 `ord_no`(7자리 0패딩 제거) ↔ ka10075 `ord_no` ↔ kt00007 `ord_no` ↔ 스트림 `9203`.
- 취소 ↔ 원주문: kt00009 `orig_ord_no`, kt00007 `ori_ord`, 스트림 `904`, `buy_ttl_cancellation_outcomes.cancellation_ord_no` ↔ `original_ord_no`.
- 종목: `symbol`(6자리) ↔ `stk_cd`(`A`+6자리; ka10072/73/75는 접두사 없음).
- 의도 ↔ 주문: `intents.order_number` = `orders.ord_no`; `intents.id` = `order_decisions.intent_id` = `pre_order_reconciliations.intent_id` = `buy_ttl_cancellation_attempts.intent_id`.
