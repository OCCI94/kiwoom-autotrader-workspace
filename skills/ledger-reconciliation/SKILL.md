---
name: ledger-reconciliation
description: Reconciles one trading day of the Kiwoom REST paper-trading bot's SQLite execution ledger against the broker observations stored inside it (kt00009 orders, ka10075 pending, kt00018 holdings snapshot, ka10072/73/74 realized P&L, kt00007 detail), lists buy-TTL cancellations with their settlement status, and recomputes net P&L with explicit commission and transaction-tax parameters. Use when the user asks for 원장 대조, 체결 대조, 잔고 불일치, 미체결 확인, 정산 확인, 손익 재계산, 수수료, 거래세, or a post-close daily check of the paper ledger. It reads only a COPY of the ledger (mode=ro), never modifies the ledger, never auto-fixes a discrepancy, and never places or cancels orders; every discrepancy is labeled MISSING or UNRECONCILED for a human to decide.
license: MIT
compatibility: macOS, Python 3.9+ standard library only (sqlite3, json). Reads a COPY of the execution ledger opened with a mode=ro URI; the live runtime under ~/Library/Application Support/KiwoomAutotrader/ is never written. No network access.
metadata:
  author: tr-oh
  version: "0.1"
  created: "2026-11-03"
---

# 원장 대조 (ledger reconciliation)

## 목적 / 언제 쓰나

봇의 SQLite 원장(`.execution-ledger.sqlite3`)이 "내가 낸 주문·체결·잔고"라고 믿는 것과, 같은 원장 안에 불변 레코드로 저장된 증권사 관측(kt00009 당일 주문체결, ka10075 미체결, kt00018 잔고 스냅샷, ka10072/73/74 실현손익, kt00007 주문상세, 00 체결 스트림)이 서로 맞는지를 장 마감 후 매일 확인한다. 수수료·거래세를 명시한 파라미터로 순손익을 다시 계산해 증권사 보고 손익과 비교한다. 로드맵 §0의 "수동 개입 0" KPI는 원장이 틀렸는지 모르면 잴 수 없다.

쓰는 상황: 장 마감 후 일일 점검, 잔고·미체결이 봇 알림과 다를 때, TTL 취소가 `settlement_unconfirmed`로 남았을 때, 손익 보고가 `미확정`으로 나올 때, 수수료·거래세 가정을 바꿔 손익을 다시 볼 때.

## 절대 규칙

1. **원장을 먼저 복사하거나 `mode=ro`로만 연다.** 라이브 경로(`~/Library/Application Support/KiwoomAutotrader/runtime/logs/.execution-ledger.sqlite3`)에 쓰기 모드로 접속하지 않는다. `scripts/reconcile.py`는 라이브 경로를 거부하며(`--allow-live-readonly`가 있어도 `mode=ro`), 사본을 쓰는 것이 기본이다.
2. **절대 자동 수정하지 않는다.** 원장·설정·상태 파일을 고치지 않고, 주문·취소·정정을 내지 않고, 봇을 재시작하지 않는다. 발견은 보고서로만 남긴다.
3. **불일치는 `MISSING` 또는 `UNRECONCILED`로만 표시한다.** `MISSING` = 한쪽에만 존재(증권사에 있는데 원장에 없는 체결, 원장에 있는데 증권사에 없는 주문). `UNRECONCILED` = 양쪽에 있으나 값이 다르거나 정산이 확정되지 않음. "아마 맞을 것"이라는 라벨은 없다.
4. **취소 ACK는 정산이 아니다.** 취소 주문 접수 응답, 스트림의 ` 매수취소/확인` 이벤트, kt00009의 취소행이 있어도 원장의 outcome이 `cancellation_confirmed`이거나 `paper_expired_order_certificates`가 있어야 정산 확정이다. 그 전까지는 `UNRECONCILED`이고 원금 예약(`ack_releases_principal`)도 해제된 것으로 보지 않는다.
5. **수수료·거래세는 항상 명시한다.** 보고서에 bp와 허용오차를 적는다. 기본값(수수료 35bp, 거래세 20bp)은 2026-11-03 모의계좌 관측치이며 실계좌와 다르다(`references/fee-tax-table.md`). 상장 ETF·ETN 매도에는 거래세를 매기지 않는다(이름 접두어 휴리스틱 `ETF_NAME_PREFIXES` 또는 `--etf-symbols`; 2026-11-04 KODEX 코스닥150 매도 세금 0원으로 관측).
6. 비밀값(`config/secrets/`, 토큰)은 읽지 않고, 계좌번호가 포함될 수 있는 payload는 마지막 4자리만 남긴다.

## 절차 (장 마감 후 매일)

1. **원장 복사.** 라이브 파일을 읽기 전용으로 열어 세션 임시 폴더에 백업한다. WAL이 있어도 일관된 사본이 나온다.
   ```
   LIVE="$HOME/Library/Application Support/KiwoomAutotrader/runtime/logs/.execution-ledger.sqlite3"
   sqlite3 -readonly "file:$LIVE?mode=ro" ".backup '<임시폴더>/ledger-copy.sqlite3'"
   ```
   `cp`로 복사할 때는 `-wal`·`-shm`이 함께 있으면 같이 복사한다. 핫 카피는 일부 페이지에서 `database disk image is malformed`가 날 수 있다. 사본 시각을 보고서에 적는다. 사본 이후의 관측은 보고서에 없다.
2. **스크립트 실행.** `python3 scripts/reconcile.py --db <사본> --day YYYYMMDD` (JSON 기본, `--format table`로 요약). 종료 코드 0 불일치 없음 / 1 불일치 / 2 대조 불가(그날 증권사 관측 없음 등) / 3 사용법 오류.
3. **주문·체결·잔고 대조.** 스크립트의 `broker_comparison`을 읽는다. 원장 `orders`/`fills` ↔ kt00009 최신 관측(원주문행만; 취소행은 `orig_ord_no`로 원주문에 연결), 원장 미체결 ↔ ka10075 `oso`, 원장 미청산(매수 체결 − 매도 체결) ↔ `account_balance_daily`의 kt00018 스냅샷 `holdings` ↔ `holdings` 표. 종목·방향·주문수량·체결수량·체결단가를 비교한다. 수동 주문은 "증권사에만 있는 주문"으로 나온다.
4. **TTL 취소와 정산 상태.** `ttl_cancellations.items`에서 원주문별 outcome status, 취소 주문번호, REST 증거(kt00009 취소행 확인수량, 원주문 체결수량, ka10075 잔존 여부), 인증서 유무, `ack_releases_principal`을 본다. `settlement=confirmed`가 아닌 것은 전부 미해결 목록에 올린다. `cancel_broker_rejected`는 원주문이 체결됐는지(동시호가 체결 등) 확인한다.
5. **순손익 재계산과 ka10074 비교.** 왕복(FIFO, 종목×전략)별 총손익 − 수수료(매수·매도 금액 × bp, 원 단위 절사) − 거래세(매도 금액 × bp, 절사). `realized_pnl.delta_ledger_minus_broker`의 매수·매도 금액은 주당 1원(`fills.price`가 절사된 평균가라서) 안이면 `notes`에 적고 넘어가며, 거래세·실현손익은 `--pnl-tolerance-krw`(기본 100원), 수수료는 `--commission-tolerance-krw`(기본 50원) 안이면 정상으로 본다. 종목별 ka10072·ka10073이 서로 다르면 두 값의 범위에 허용오차를 더한 띠 안에 있으면 정상이고 차이는 `notes`에 남는다. ETF·ETN 왕복은 `tax_exempt`에 사유가 붙고 거래세 0으로 계산된다. ka10072/ka10073 종목별 값도 함께 적되 셋이 서로 수십 원 다를 수 있다(함정 참조). 원장 `profits.reason`이 `미확정`이면 봇 자신도 비용 대사를 끝내지 못한 것이므로 그 사유를 그대로 옮긴다.
6. **보고서 작성.** 아래 보고 형식대로 쓰고 `/Users/tr-oh/Documents/Codex/ai-stock-원장대조-YYYYMMDD.md`에 저장한다. 결론은 사람이 내린다. 다음 거래일에 같은 미해결 항목이 또 나오면 "반복"으로 표시한다.

## 스크립트 사용법

```
python3 scripts/reconcile.py --db <사본.sqlite3> [--day YYYYMMDD] [--scope default] [--mode paper]
    [--commission-bps 35] [--tax-bps 20] [--pnl-tolerance-krw 100] [--commission-tolerance-krw 50]
    [--etf-symbols 229200,069500] [--no-etf-heuristic]
    [--json | --format table] [--limit 500] [--allow-live-readonly]
```

- `--db`: 원장 사본. 경로가 `KiwoomAutotrader/runtime/logs/.execution-ledger.sqlite3`로 끝나면 `--allow-live-readonly` 없이는 거부한다. 항상 `file:...?mode=ro` URI + `PRAGMA query_only`로 연다.
- `--day`: 기본 오늘(KST). 모든 질의는 `day`로 한정하고 `--limit`으로 상한을 둔다(원장은 1GB 가까이 된다).
- 출력 JSON 키: `meta`(사본 크기·mtime·생성 시각), `counts`(intents·orders·fills 방향별, TTL 시도·결과·인증서, 관측 API별 건수와 첫/마지막 시각), `ledger_round_trips`(왕복·미청산·짝 없는 매도), `broker_comparison`(`kt00009_orders`, `ka10075_pending`, `kt00018_holdings`), `ttl_cancellations`, `realized_pnl`(원장 추정·ka10074·차이·종목별 72/73·`profits`·`summaries`), `discrepancies`(각각 `kind`, `symbol`, `order_number`, `detail`, `label`), `section_errors`, `summary`.
- 불일치 `kind` 예: `order_missing_at_broker`(MISSING), `order_not_in_ledger`, `fill_missing_in_ledger`(MISSING), `order_field_mismatch`, `pending_unresolved`, `pending_not_in_ledger`, `position_missing_at_broker`(MISSING), `position_not_in_ledger`, `position_qty_mismatch`, `holdings_table_mismatch`, `holdings_snapshot_stale`, `ttl_settlement_unconfirmed`, `ttl_cancel_rejected_order_filled`, `ttl_outcome_missing`(MISSING), `ttl_cancel_rejected`, `turnover_mismatch_*`, `pnl_mismatch_*`, `symbol_pnl_mismatch`, `symbol_commission_mismatch`, `sell_without_ledger_buy`, `*_unavailable`. 허용오차 안의 차이(체결금액 절사, 증권사 보고 간 불일치)는 불일치가 아니라 `realized_pnl.notes`에 남는다.
- 스크립트는 아무것도 쓰지 않고 네트워크를 쓰지 않는다. 진단은 stderr.

## 보고 형식

```
# 원장 대조 YYYY-MM-DD (scope/mode, 사본 HH:MM, 스크립트 종료 코드 N)

## 요약
- 관측 범위: kt00009 최신 HH:MM:SS, ka10075 최신 HH:MM:SS, kt00018 스냅샷 HH:MM:SS, ka10074 최신 HH:MM:SS
- 주문 N(매수 n/매도 n), 체결 N, 왕복 N건, 미청산 N종목 N주, TTL 취소 N건(확정 n / 미확정 n / 거부 n)
- 불일치 N건 (MISSING n, UNRECONCILED n) — 종류별 건수
- 파라미터: 수수료 35bp, 거래세 20bp(ETF·ETN 0), 허용오차 실현손익 100원 / 수수료 50원 / 체결금액 주당 1원 (모의계좌 관측치; 실계좌와 다름). 면세 처리한 종목과 사유를 적는다

## 불일치 표
| 라벨 | 종류 | 종목 | 주문번호 | 내용 |
|---|---|---|---|---|

## 손익 표
| 항목 | 원장(추정) | 증권사 ka10074 | 차이(원장−증권사) |
|---|---|---|---|
| 매수금액 | | | |
| 매도금액 | | | |
| 수수료 | | | |
| 거래세 | | | |
| 실현손익 | | | |
종목별: 원장 순손익 / ka10072 / ka10073 (셋이 수십 원 다르면 그대로 적고 '모의서버 불일치'로 표시)

## 미해결 목록
- [UNRECONCILED] 주문번호·종목·상태 → 사람이 확인할 수단(예: 다음 거래일 kt00009 재조회, 정산 사이드카 로그, 인증서 생성 여부)
- 전일에도 있던 항목은 "반복(n일째)"
```

## 함정

- **취소 ACK ≠ 정산.** 2026-11-03 TTL 취소 9건은 REST 응답도 받고 스트림에 ` 매수취소/확인`도 왔고 kt00009에 취소행(확인수량 = 주문수량)도 있지만 원장 outcome은 `cancel_input_ack_unconfirmed_settlement`, 인증서 0건, `ack_releases_principal=false`였다. 봇의 정산 검증기(`cancellation_settlement.py`)가 통과시킬 때까지는 미확정이다.
- **kt00009 `mdfy_cncl_tp`.** 문서는 원주문을 `''`로 적지만 실제 응답은 원주문 `'일반'`, 취소 `'취소'`다. 원주문 판별은 `orig_ord_no`가 `0000000`인지로 하는 것이 안전하다. kt00007은 같은 뜻의 열이 `mdfy_cncl`, `ori_ord`다.
- **주문번호·종목코드 정규화.** 증권사는 주문번호를 7자리 0패딩(`0078331`), 종목을 `A030530`으로, 수량·금액을 0패딩 문자열(`0000000082`)로 준다. 원장은 `78331`, `030530`, 정수다. 비교 전에 반드시 정규화한다.
- **00 체결 스트림 필드.** `905`는 `'+매수'`, `'-매도'`, `' 매수취소'`처럼 앞에 부호 자리가 있어 취소는 **앞 공백**이 붙는다. `913`은 접수/체결/확인. `903`(체결누계금액)과 `938`(수수료)은 주문 단위 **누계**, `939`(거래세)는 체결 단위 **증분**이다. 한 주문이 분할 체결되면 같은 `9203` 주문번호 행이 여러 번 온다.
- **payload는 JSON 문자열.** `broker_observations.payload_json`을 `json.loads`한 뒤 `response_body` 아래의 배열(`acnt_ord_cntr_prst_array`, `oso`, `acnt_ord_cntr_prps_dtl`, `dt_rlzt_pl`, `dt_stk_div_rlzt_pl`, `dt_stk_rlzt_pl`)을 읽는다. 최신 관측은 `id` 내림차순. `status != 'success'`(예: 19:56 ka10075 `return_code: 7`)는 건너뛴다.
- **kt00018은 `broker_observations`에 없다.** 잔고 스냅샷은 `account_balance_daily.payload_json`(`schema: paper-account-snapshot-v1`, `pages: {kt00001, kt00018}`, `holdings[]`)에 있다.
- **`holdings` 표에는 `day` 열이 없다.** PK가 (scope, mode)라 최신 한 행만 있으므로 `observed_at`이 대조일인지 확인한다. 다른 날짜면 `holdings_snapshot_stale`.
- **`fills.quantity`는 누계 스냅샷.** `quantity_semantics=provider_key_snapshot_not_inferred_unit_fill`. 주문당 한 행이며 여러 행이 있어도 합치지 말고 최댓값을 쓴다.
- **이벤트 JSONL 포장.** `YYYYMMDD.broker-observations.jsonl`·`YYYYMMDD.execution-history.jsonl`은 `{"id","payload","record_type","sha256"}`, `strategy-signal-events-YYYYMMDD.jsonl`은 `{"event": {...}, "event_sha256"}`로 감싼다. `event_kind`는 그 안에 있다.
- **모의서버 손익 수치가 서로 다르다.** 같은 5왕복에 대해 ka10074 수수료 5,810 / ka10073 5,780 / ka10072 5,710 / 스트림 누계 5,750원. 허용오차 없이 비교하면 항상 불일치가 되고, 봇도 `profits.reason`에 `미확정: ka10072·ka10073 수수료·세금 또는 보고손익 불일치`로 남긴다(11-04에도 파수AI 수수료 1,430/1,440원 차이로 두 종목 모두 `net=null`). 스크립트는 두 보고의 범위로 비교하고 차이를 `realized_pnl.notes`에 적는다. 차이를 숨기지 말고 적는다.
- **ETF·ETN 거래세 0.** 11-04 KODEX 코스닥150 매도 197,540원에 세금 0원. 거래세를 일괄 적용하면 ETF 왕복마다 ~20bp가 과소 계상된다. 휴리스틱 접두어 목록(`ETF_NAME_PREFIXES`)은 2026년 브랜드 기준이며 완전성은 확인 필요. 새 브랜드는 `--etf-symbols`로 넘긴다.
- **`fills.price`는 절사된 평균가.** 11-04 KODEX 코스닥150 매수 17주: 원장 11,456×17=194,752원, 증권사 194,760원(평균 11,456.47원). 체결금액 비교는 주당 1원까지 절사 오차로 본다.
- **핫 카피.** 라이브에서 `cp`한 사본은 `profit_observations` 전체 스캔에서 `malformed`가 난 적이 있다. 스크립트는 섹션별로 격리해 `section_errors`에 남긴다. `.backup`으로 만든 사본을 우선한다.
- **사본 시각.** 11-03 사본(16:01)에는 15:38 ka10074까지만 있었다. 장 마감 후 늦은 관측(정산 재시도, 야간 조회)은 다음 사본에서 본다.

## 참조 파일

- `references/ledger-schema.md`: 원장 27개 표의 열 이름과 역할, 불변 트리거.
- `references/fee-tax-table.md`: 수수료·거래세 가정, 11-03 관측치, 2025~2026 증권거래세 맥락(확인 필요), 모의/실계좌 차이.
- `references/event-types.md`: 관측 API·스트림 이벤트·execution_observations.kind·신호 이벤트·TTL outcome 상태값.
- `scripts/reconcile.py`: 대조 스크립트(읽기 전용, stdlib).
- `evals/evals.json`: 트리거·행동 평가 항목.
