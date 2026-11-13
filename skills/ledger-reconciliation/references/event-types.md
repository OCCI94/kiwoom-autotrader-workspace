# 이벤트·상태값 목록 (2026-11-03 원장 사본과 JSONL, 봇 소스에서 확인)

## 1. 증권사 관측 `broker_observations.source_api`

| source_api | 내용 | 응답 배열 / 핵심 필드 |
|---|---|---|
| `00` | 실시간 체결 스트림 이벤트 (`event_kind` 아래 참조) | `values{FID: 값}` |
| `kt00009` | 당일 주문체결 조회 | `acnt_ord_cntr_prst_array[]`: ord_no, orig_ord_no, stk_cd(A접두), io_tp_nm(현금매수/현금매도), mdfy_cncl_tp(일반/취소/정정), acpt_tp, ord_qty, cntr_qty, cnfm_qty, ord_uv, cntr_uv, trde_tp, dmst_stex_tp |
| `ka10075` | 미체결 조회 | `oso[]`: ord_no, orig_ord_no, stk_cd(접두 없음), io_tp_nm(+매수/-매도), ord_stt(접수 등), ord_qty, oso_qty, cntr_qty, ord_pric, tm |
| `kt00007` | 주문체결 상세 | `acnt_ord_cntr_prps_dtl[]`: ord_no, ori_ord, mdfy_cncl, ord_qty, cntr_qty, ord_remnq, cnfm_qty, ord_tm, cnfm_tm, crd_tp |
| `ka10072` | 일자별 종목별 실현손익(일자) | `dt_stk_div_rlzt_pl[]`: stk_cd, cntr_qty, buy_uv, cntr_pric, tdy_sel_pl, tdy_trde_cmsn, tdy_trde_tax |
| `ka10073` | 일자별 종목별 실현손익(기간) | `dt_stk_rlzt_pl[]`: 위와 같음 + dt |
| `ka10074` | 일자별 실현손익 합계 | `dt_rlzt_pl[]` + `tot_buy_amt`, `tot_sell_amt`, `trde_cmsn`, `trde_tax`, `rlzt_pl` |

공통 봉투: `schema: kiwoom-broker-page-v1`, `status`(success / broker_error), `http_status`, `request_body`, `response_body.return_code`(0 정상; 11-03 19:56 ka10075는 7), `continuation`, `query_id`, `observed_at`. 잔고(kt00018)와 예수금(kt00001)은 `account_balance_daily.payload_json`(`paper-account-snapshot-v1`)에만 있다.

## 2. 스트림 `00` 이벤트

`payload.event_kind`: `stream_started`, `stream_stopped`, `stream_gap`, `stream_resumed`, `execution_stream`.

`execution_stream.values` FID (의미는 데이터에서 추정, 공식 명칭은 확인 필요):

| FID | 뜻 | 관측값 예 |
|---|---|---|
| 9001 | 종목코드 | `326030` |
| 302 | 종목명 | |
| 9203 | 주문번호(7자리) | `0219890` |
| 904 | 원주문번호 | `0000000` / `0219443` |
| 905 | 주문구분 | `'+매수'`, `'-매도'`, `' 매수취소'` (**앞 공백**) |
| 913 | 주문상태 | `접수`, `체결`, `확인`(취소 확인) |
| 900 / 901 | 주문수량 / 주문가격 | |
| 902 | 미체결수량 | 취소 `확인` 때 0 |
| 903 | 체결누계금액(주문 단위 누계) | |
| 907 | 매도수구분 코드 | `2` 매수 |
| 908 / 909 | 체결시각(HHMMSS) / 체결번호 | |
| 910 / 911 | 체결가 / 체결수량(이번 스냅샷) | |
| 938 | 수수료(주문 단위 **누계**) | 0078331: 320 → 650 |
| 939 | 거래세(체결 단위 **증분**) | 0080631: 63, 189, 126 |
| 2135 | 거래소 | `KRX` |

11-03 집계: `('+매수','접수')` 24, `(' 매수취소','접수')` 9, `(' 매수취소','확인')` 9, `('-매도','체결')` 8, `('+매수','체결')` 7, `('-매도','접수')` 5.

## 3. 체결 투영 이력 `execution_observations.kind` / JSONL `execution-history`

- `new_execution_key`: 새 (ord_no, fill_no) 키가 처음 관측됨. `current.holdings_status='조회 대기'`.
- `holdings_reconciliation`: 체결 후 잔고 조회가 붙음. `holdings_status='조회시점 보유수량(체결순간 수량 아님)'`, `remaining` 채워짐.
- `repeated_snapshot`: 같은 키 재관측(11-03 4,186건, 대부분 노이즈).
- 기타 `holdings_status` 문자열(소스): `미확정  신용구분 또는 중복잔고 확인 필요`, `조회 대기  체결보다 이른 과거 잔고 연결 해제`.
- `quantity_semantics='provider_key_snapshot_not_inferred_unit_fill'`: 수량은 누계 스냅샷.
- JSONL 한 줄: `{"id", "payload": {...위 내용...}, "record_type": "execution_observations", "sha256"}`.

## 4. 전략 신호 이벤트 `strategy-signal-events-YYYYMMDD.jsonl`

한 줄: `{"event": {...}, "event_sha256"}`. `event.schema='paper-strategy-signal-event-v1'`, `research_only=true`.

- `event_kind`: `eligible_signal`(신호 적격) → `decision_outcome`(주문 결정).
- `decision_outcome.outcome`: `accepted`(주문번호 확보), `not_submitted`(`skip_reason` 참조). `eligible_signal`은 `outcome=null`.
- `execution_status`: 11-03에는 전부 null(체결 귀속은 원장 쪽에서).
- `reason`(신호 사유): `entry_signal`, `closing_auction`, `dead_cross`, `gap_open`, `take_profit_100bp`.
- 주요 키: arm, assignment{profile, slot, weights, seed_sha256}, signal_at, available_at, signal_price, signal_price_basis, side, symbol, strategy, order_number, ownership_status, profit_attribution, vi_observation, features{gap_bps, avg_turnover20_krw, market_first30m_return, theme}.

## 5. 원장 상태값

- `intents.state`: `prepared` → `accepted` / `cancelled`.
- `profits.basis`(관측): `completed_signal_bar_close` 등; `net` NULL이면 `reason`이 `미확정: ka10072·ka10073 수수료·세금 또는 보고손익 불일치` 또는 `미확정: 최신 매도 후 72·73 보고 및 비용 대사 대기(post_sell_report_pending)`.
- `summaries.payload.status`: 예 `비용 또는 자동매도 귀속 범위 미확정`; `verified_totals.{buy,sell}.coverage`: `all_registered_orders_fully_reconciled` / `partial_or_unconfirmed`.
- `multi_strategy_exit_claims.exit_kind`: `full`.

## 6. 매수 TTL 취소 상태 (`buy_ttl_cancellation_outcomes.status`, `*_retry_outcomes.status`, 리스너 `last_status.status`)

| 값 | 뜻 | 정산 확정? |
|---|---|---|
| `cancel_input_ack_unconfirmed_settlement` | 취소 주문 접수 ACK는 받았으나 정산 검증 미통과 (11-03 9건) | 아니오 |
| `cancel_broker_rejected` | 증권사가 취소를 거부 (11-03 1건: 326030, 원주문이 15:30:26 체결됨) | 아니오 — 원주문 체결 여부 확인 |
| `cancel_unconfirmed` | 취소 요청 결과 자체가 불확실(오류·타임아웃) | 아니오 |
| `settlement_unconfirmed` | 정산 검증 실패·불가 상태(리스너 요약; `reason`에 세부 코드) | 아니오 |
| `retry_pending` | 재시도 대기 | 아니오 |
| `cancellation_confirmed` | 정산 검증기 통과(원주문 잔량 0, 취소수량 일치, REST·스트림·원장 정합) | 예 |
| `fill_resolved` | 취소 대신 체결로 종결 | 예(체결로) |

정산 실패 세부 코드(`cancellation_settlement.py`, `unfilled_buy_ttl.py`에서 발췌; 전체 목록은 소스 `kiwoom_autotrader/trading/`): `cancellation_rest_original_identity_conflict`, `cancellation_ack_unconfirmed`, `cancellation_original_missing`, `cancellation_original_rest_missing`, `cancellation_rest_execution_mismatch`, `cancellation_rest_terminal_missing`, `cancellation_settlement_schema_unverified`, `cancellation_numeric_field_unverified`, `cancellation_query_completion_missing`, `cancellation_stream_unverified`, `cancellation_broker_remainder_not_zero`, `cancellation_fill_changed_during_query`, `cancellation_foreign_child`, `cancellation_late_order_change`, `cancellation_projection_not_reconciled`, `cancellation_sidecar_unavailable`, `ttl_settlement_unavailable`, `ttl_outcome_record_unavailable`, `ttl_pending_order_mismatch`, `ttl_original_already_modified`, `ttl_fill_quantity_conflict`, `ttl_duplicate_pending_order`, `ttl_clock_unverified`, `ttl_evidence_changed_or_stale`, `ttl_retry_history_limit`.

정산 확정의 추가 증거: `paper_expired_order_certificates`(종결 인증서). 11-03 사본에는 0건이었다. `buy_ttl_cancellation_attempts.evidence_json.ack_releases_principal`이 false면 취소 ACK만으로 원금 예약을 풀지 않는다는 뜻이다.
