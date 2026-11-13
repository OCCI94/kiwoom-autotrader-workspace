# arm 변경 기록: stream_lock_and_order_reason_repair_v1 (00 스트림 SQLITE_BUSY 해소 · 주문 전송 전 사유 보존 · 수집기 진단 정렬)

- 분류: **결함 수정 (경제적 변경 없음)**. 14개 전략 arm(`EXP_R`, `EXP_A`, `EXP_B`, `EXP_B_TP`, `EXP_E`, `EXP_C`, `EXP_D`, `EXP_ETF_B`, `EXP_GDR`, `EXP_LDR`, `EXP_ORB_A`, `EXP_ORB_B`, `EXP_VSS`, `S5_steady_uptrend_vwap`)의 진입·청산 조건, 목표가·손절가, TTL, 포지션 한도, 설정(`config/settings.py`, `resources/default_settings.json`), 지시문(`RANK_AGENT_INSTRUCTIONS.txt`, `MULTI_STRATEGY_AGENT_INSTRUCTIONS.txt`), `state.json` 최상위 스키마는 일절 변경하지 않는다.
- 대상 arm: 없음(14개 arm 경제 로직 불변). 영향 경로는 원장 관측 읽기·증명 검증(`broker_observation_repeats`, `expired_order_certificates`, `execution_log`), HTTP 전송 전 guard 예외 진단 코드(`paper_reconciliation`, `paper_policy`, `http_queue`, `orders`), 개인 계정 수집기 CLI/루프 실패 코드(`main`, `telegram/collector`).
- 격리 작업 트리: `/Users/tr-oh/Documents/Codex/2026-10-26/new-chat/work/defect-repair-20261112/outputs/ai-stock` (운영 실행본 `priority-lanes-clean-20261111/outputs/ai-stock`에서 분기; 봉인된 16개 연구·소스 루트 및 `phase2-rebase-clean-20261112` 무변경 검증 완료).
- 영향 경로(`file:line` 및 파일 `mtime`, 2026-11-12 KST 기준):
  - **결함 1 (`00` 실시간 스트림 `SQLITE_BUSY` 8회 래치 · CPU 91.9% · 모니터 루프 ~6분 지연 해소)**:
    - `kiwoom_autotrader/trading/broker_observation_repeats.py` (`mtime 2026-11-12 11:12:06 KST`, sha256 `c4518a8e99ee…`): `59` `_sha_cached` (`@lru_cache(maxsize=64)` SHA-256 캐시) → `71~92` `_hydrate` (`_parsed_original_payload` JSON 1회 파싱 재사용; 동일 본문 수천 건 반복 시 매 행 SHA-256·`json.loads` 중복 제거).
    - `kiwoom_autotrader/trading/expired_order_certificates.py` (`mtime 2026-11-12 11:15:16 KST`, sha256 `3cab6bb1ea88…`): `24~171` `_ExpiryIndexEntry` (최대 4개 DB LRU, 원문 `payload_json` 미보관·정수 `row_id` 및 `(row_id, day, observed_at)` 원시 튜플만 보관) → `174~273` `_sync_expiry_index` (`SAVEPOINT` 물리 watermark 검증 후 append 증분만 동기화; `:memory:` 및 미커밋 dirty RW 트랜잭션 우회, `prepare_intent`의 clean `BEGIN IMMEDIATE`에서는 즉시 히트) → `276` `verify_certificate_sources` · `303` `check_later_ka10075_clear` · `340` `cached_captured_terminal_pages` · `366` `select_late_order_observations`.
    - `kiwoom_autotrader/trading/execution_log.py` (`mtime 2026-11-12 11:21:28 KST`, sha256 `c5d4370a14ce…`): `55` `_SQLITE_TIMEOUT_SECONDS = 30.0` (기존 `timeout=5` 20개 연결 일괄 상향) → `59~83` `_is_sqlite_busy_error` · `_run_with_busy_retry` (지터 백오프 재시도) → `744, 908, 1067` `_observation_write_lock` (`new_history_tracker()` 공유 프로세스 내 관측 쓰기 직렬화) → `1107~1135` `_db` (`(st_dev, st_ino, schema_version)` 일치 시 매 호출 45문장 `executescript` 스키마 락 생략) → `1271` `_ro_db` (`?mode=ro` + `PRAGMA query_only=ON`, `finally: db.close()`) → `1619` `_paper_prior_order_terminal` · `1788` `_confirmed_expired_quantity` (`verified_certificate` 우선 단락 평가) → `4963` `_materialize_locked` · `5065` `_materialize_accounting` · `5138` `_materialize_history_locked` (`_ro_db()` 전환 및 JSON 파일 생성·`os.fsync` 이전 DB 핸들 조기 닫기).
  - **결함 2 (`OrderError` 전송 전 차단 사유 `reason_code` 소실 복원)**:
    - `kiwoom_autotrader/trading/paper_reconciliation.py` (`mtime 2026-11-12 10:58:13 KST`, sha256 `ecadb398d990…`): `31~38` `PaperReconciliationError(..., reason_code=...)` (`ALLOWED_ORDER_REASON_CODES` 화이트리스트 검증; 기본값 `"paper_reconciliation_blocked"`, 원장 미보유 시 `"reconciliation_not_owned"`, 수량 부족 시 `"reconciliation_insufficient_qty"` 등 부여).
    - `kiwoom_autotrader/trading/paper_policy.py` (`mtime 2026-11-12 10:58:15 KST`, sha256 `260bf56f61b0…`): `91~95` `PaperAutomationPolicyError(..., reason_code=...)` (노출 한도·종목 한도·세션 시간 등 정적 enum 코드 부여).
    - `kiwoom_autotrader/trading/execution_log.py` (`mtime 2026-11-12 11:21:28 KST`): `2445` `assert_multi_exit_dispatch` · `2489` `assert_multi_buy_dispatch` (대사 증명 생성 후 20초 경과 시 `error.reason_code = "reconciliation_proof_expired"`, 원장 상태 변동 시 `"reconciliation_state_changed"` 태깅).
    - `kiwoom_autotrader/utils/http_queue.py` (`mtime 2026-11-12 10:58:42 KST`, sha256 `323abbbfe6fe…`): `85` `extract_guard_reason_code` → `98~106` `HTTPNotSentError(message, *, reason_code=None)` → `449, 484~485` `before_send` 차단 시 검증된 `reason_code` 전달(임의 문자열·비밀값 차단).
    - `kiwoom_autotrader/api/orders.py` (`mtime 2026-11-12 11:26:35 KST`, sha256 `e4d36985790c…`): `51~55` `OrderError(..., reason_code=None)` → `186~191` `submit_order` 내 지연 임포트된 `PaperReconciliationError` 포착 → `249, 325~332` `HTTPNotSentError` 포착 시 `dispatch_skip_reason` 또는 `not_sent.reason_code`를 `OrderError.reason_code`로 보존(로그·상태에는 정적 enum만 노출).
  - **결함 3 (11-06 개인 계정 수집기 실패 진단 정렬)**:
    - `kiwoom_autotrader/main.py` (`mtime 2026-11-06 12:37:25 KST`, sha256 `913902eecd4b…`): `289~338` `_telegram_account_command` (`asyncio.Runner` 루프 예외 핸들러 `log_background_failure` 연결 및 CLI 단계별 `phase`/`code` 표준오류 출력).
    - `kiwoom_autotrader/telegram/collector.py` (`mtime 2026-11-06 12:42:10 KST`, sha256 `21594e3087e1…`): `50~97` `_collector_log`(`pid=` 접두 및 깨진 로그 싱크 방어)·`failure_code`(무임포트 MRO 매칭)·`log_background_failure`, `309~325` `_write_status`, `359~525` `run_collector` 초기 실패 진단 기록.
- 소스·패키지 manifest 및 SHA-256:
  - 기준 실행본 패키지(`priority-lanes-clean-20261111/outputs/ai-stock/kiwoom_autotrader`, 189 파일): `1e4d90cc67c3305f6d7291d99ea0f0731847fb0cd2c00b3947cf1e225b563d02` (현재 운영 실행본과 동일).
  - 수리 후보 패키지(`defect-repair-20261112/outputs/ai-stock/kiwoom_autotrader`, 189 파일, 변경 `.py` 9개): `e768edf75c034dec529b4f59030e6b59f78715390f572ea08e85c5a66fe45f6c`.
  - 전체 트리 파일 수: 기준 861 파일 → 수리 후보 862 파일(소스 `.py` 9개 수정, 기존 테스트 `.py` 2개 정렬, 신규 회귀 테스트 `tests/test_defect_repairs_20261112.py` 1개 추가, 제거 0개).
  - 격리 APP 전체 스위트 provenance: `defect-repair-20261112/reports/2026-11-12/app-tests-isolated-provenance-123627865355.json` (`12:36:27 ~ 12:38:26 KST`, **244 모듈 · 3,217 테스트 · 0 스킵 · 0 실패**, `full_suite: true`, `source_unchanged_after: true`), 소스 manifest `app-tests-isolated-provenance-123627865355-source-manifest.json` (`sha256: d2c909c8f29af07c98e49fb252d1918326ba7d5aba2236a473bac756d225200a`, 862 파일).
  - 배포 도구(`defect-repair-20261112/work/deploy_receiver_normal_handoff.py`): 집중 계약 테스트 **35/35 PASS**.
- 배포 창 점검(`deploy_window_check.py`, `2026-11-12 12:06 KST` 실행):
  - `--kind repair`: `allowed: true` (장중에는 소스 수리·격리 테스트만 수행; 운영 경로 무접촉).
  - `--kind normal`: `allowed: false` (`window: session`, 정규장 `09:00~15:30 KST` 중 배포 금지; **다음 정상 배포 창: `2026-11-12 (목) 15:40 ~ 24:00 KST` 장후**).
- 상태 전환: 없음(기능 플래그 없음). SHADOW 해당 없음: 경제적 매매 규칙·수량·가격·집행 타이밍을 바꾸지 않는 순수 인프라·진단 결함 수리이며, 배포 직후 `state.json` 및 첫 거래일 장중 관측치로 검증한다.

## 1. 목적

1. **설계 의도**: 실시간 `00` 체결 스트림 기록기와 모니터·추적기 루프는 원장 관측 행이 누적되더라도 짧은 시간 안에 읽기·쓰기를 마쳐야 하며, 주문 전송 직전(`before_send`) 안전 대사가 주문을 정당하게 보류할 때는 어떤 가드(대사 증명 20초 만료, 원장 상태 변경, 일일/종목 한도 등)가 작동했는지 비밀값 없는 정적 코드(`reason_code`)로 남겨야 한다.
2. **실제 발생 현상(2026-11-12 장중 점검)**:
   - 당일 `broker_observation_repeats` 등 관측 행이 누적되면서 만료 증명·종결 주문 조회가 매 틱마다 수만 건의 JSON 원문을 다시 읽고 SHA-256·`json.loads`를 반복했으며, 매 `_db()` 호출이 45문장 `executescript` 스키마 잠금을 유발하고 읽기 전용 집계(`_materialize*`)가 쓰기 가능 연결을 붙잡은 채 `os.fsync`까지 수행해 수신기 CPU가 **91.9%**로 치솟고 모니터 루프가 **약 6분** 지연되었다.
   - 그 결과 실시간 `00` 스트림이 `timeout=5`초 안에 쓰기 잠금을 얻지 못해 `09:49~10:21 KST` 사이 `SQLITE_BUSY` 8회가 발생하며 `execution_stream_ready: false`가 래치되었고, 6분 지연 뒤 도착한 청산·매수 시도가 `20초` 대사 증명 만료 가드에 걸렸으나 `HTTPNotSentError` → `OrderError` 변환 과정에서 `reason_code`가 지워져 `last_exit_error: OrderError`(원인 불명)로만 기록되었다.
3. **수리 목표**: 원장 무결성·해시 검증과 모든 안전 가드의 fail-closed 동작은 100% 그대로 유지하면서, (a) 중복 파싱·전체 테이블 스캔·불필요한 쓰기 잠금 점유를 제거하고 SQLite 대기·재시도를 보강해 `00` 스트림 래치를 원천 차단하며, (b) 전송 전 차단 사유를 정적 화이트리스트 `reason_code`로 보존하고, (c) 운영본에 누락되어 있던 11-06 수집기 초기 실패 진단 코드를 정렬한다.

## 2. 기대 효과와 측정 지표

1. **`00` 실시간 스트림 `SQLITE_BUSY` 0건**: 배포 후 장중 `state.json`의 `execution_stream_ready == true`, `execution_stream_error == null`, `00 storage failure` 중 `category == 'sqlite_busy'` 발생 건수 **0건**(2026-11-12 수리 전 장중 8건).
2. **수신기 CPU 및 루프 주기 정상화**: 장중 관측 행 2,000~20,000건 누적 상태에서 수신기 평균 CPU **< 25%**(수리 전 91.9%), 모니터·폴링 체크포인트 간격(`state.json` 갱신 지연) **< 60초**(수리 전 ~360초; 격리 벤치마크에서 2,000 반복 행 × 30회 연속 조회 **< 0.4초** 확인).
3. **주문 전송 전 보류 사유 식별성 100%**: 전송 전 가드 보류 발생 시 `OrderError.reason_code`가 `None`이 아닌 허용 화이트리스트 값(`reconciliation_proof_expired`, `reconciliation_state_changed`, `paper_reconciliation_blocked` 등)으로 기록되고 토큰·계좌·원문 응답은 0건 노출.
4. **금융 원장 무결성 불변**: 배포 전후 `intents`, `orders`, `fills`, `profits`, `expired_order_certificates`, `multi_strategy_positions` 기존 행 해시 100% 동일.

## 3. 측정 방법과 기간

- **배포 직후(`2026-11-12 목 15:40 KST` 이후 정상 창 배포 시)**: `deploy_receiver_normal_handoff.py`의 배포 전후 스냅샷 대조 및 기동 후 `state.json` 5개 핵심 필드(`execution_stream_ready`, `execution_stream_coverage_complete`, `maintenance_paused` 부재, `multi_strategy_Summary`, 오류 필드) 즉시 확인.
- **첫 거래일(`2026-11-13 금`) 및 3거래일 뒤(`2026-11-17 화`) 장 마감 후**: 원장 읽기 전용 복사본(`ledger-reconciliation` 절차)과 `observability/watchdog/*.sample.json`, `runtime/logs/tracker-diagnostics-YYYYMMDD.jsonl`에서 §2의 1~4항 지표를 `2026-11-12` 장중 기준치와 비교 집계한다.
- SHADOW 만료일: 해당 없음(경제 규칙 변경 아님). 단, 첫 거래일 장중에 `execution_stream_ready: false`(`sqlite_busy`) 재발 또는 원장 검증 오류 발생 시 §4에 따라 즉시 롤백 검토.

## 4. 롤백 절차

- **원칙**: 코드(`runtime/kiwoom_autotrader`)만 되돌리며, 금융 원장(`.sqlite3`)과 `state.json` 오프셋(`next_offset`)은 절대 과거 파일로 덮어쓰지 않는다. 이번 수리는 `state.json` 최상위 키 집합과 DB 테이블 스키마를 전혀 바꾸지 않았으므로 구 실행본과 100% 정방향·역방향 호환된다.
- **롤백 대상 코드**: 직전 운영 실행본 `priority-lanes-clean-20261111/outputs/ai-stock` (패키지 지문 `1e4d90cc67c3305f6d7291d99ea0f0731847fb0cd2c00b3947cf1e225b563d02`) 또는 배포 시 `deploy_receiver_immutable.py`가 생성한 불변 런타임 백업 디렉터리.
- **롤백 실행 절차(사람 실행, 배포 창 및 claim 규칙 동일 적용)**:
  1. 장외 정상 창(화·목 `08:30` 이전 또는 `15:40` 이후; 장중 긴급 시 사용자 일회성 예외 토큰 필요) 확인.
  2. `priority-lanes-clean-20261111/work/deploy_receiver_normal_handoff.py --check-only`로 상태 점검 후 승인 JSON으로 직전 패키지(`1e4d90cc67c3…`) 재배포, 또는 배포 중 normal handoff 경계 이전 실패 시 도구의 내장 코드 롤백 경로 사용.
  3. 기동 후 `launchctl print gui/$(id -u)/com.local.kiwoom-autotrader.telegram` 및 `state.json` 정상 여부 확인.

## 5. 위험

| 실패 모드 | 감지 방법 및 완화 장치 |
|---|---|
| 인메모리 만료 인덱스(`_ExpiryIndexEntry`)가 외부 도구의 비정상 원장 수정(행 삭제·덮어쓰기)을 구버전으로 읽을 위험 | **완화 완료**: `_sync_expiry_index`가 매 조회마다 `SAVEPOINT` 안에서 테이블 `COUNT(*)`·`MIN(id)`·`MAX(id)` 및 첫/끝 행 해시 watermark를 대조하며, 불일치 시 즉시 전체 재구축한다. `:memory:` DB와 미커밋 dirty RW 트랜잭션은 캐시를 자동 우회한다(`test_defect_repairs_20261112.py` 검증). |
| `_db()`의 스키마 초기화 생략 이후 DB 파일 교체·재생성 시 테이블 미생성 위험 | **완화 완료**: 파일 경로가 아닌 열린 FD의 `(st_dev, st_ino)`와 `PRAGMA schema_version` 튜플을 비교하므로 파일 교체·삭제 후 재생성·외부 DDL 발생 시 자동으로 스키마 초기화를 다시 실행한다. |
| `_ro_db()` 호출 시 아직 원장 파일이 생성되지 않은 초기 상태에서의 `OperationalError` | **완화 완료**: `self.path` 미존재 또는 `sqlite3.OperationalError` 시 기존 `self._db()`로 안전하게 폴백해 초기 빈 원장을 정상 생성한다. |
| `orders.py`의 `PaperReconciliationError` 임포트로 인한 순환 참조(`orders` ↔ `minute_chart` ↔ `executions` ↔ `paper_reconciliation`) | **완화 완료**: 모듈 최상위가 아닌 `submit_order` 내부 `if automatic_guard is not None:` 블록에서 지연 임포트하며, 전체 244개 테스트 모듈 임포트·실행으로 검증 완료. |
| 새로운 `reason_code`에 민감 문자열이 섞여 로그나 상태에 노출될 위험 | **완화 완료**: `ALLOWED_ORDER_REASON_CODES` 및 `_ALLOWED_NOT_SENT_REASONS` 정적 `frozenset`에 없는 값은 모두 `None` 또는 고정 기본 코드로 치환된다(`test_defect_repairs_20261112.py` 검증). |

## 6. 승인자 / 승인 시각

- 소스 수리 요청 및 승인: 사용자, `2026-11-12 10:50 KST` `"그럼 수정사항 반영해봐"` 지시로 격리 작업 트리 수리·검증 수행(정규장 시간대이므로 운영 런타임·원장·LaunchAgent 무접촉 준수).
- 코드 배포 승인: **미작성 (대기)** — 오늘 **`2026-11-12 (목) 15:40 KST` 이후 장후 정상 배포 창**에서 사용자가 이 변경 기록과 격리 스위트 manifest 지문을 확인한 뒤 승인 여부를 결정한다(화·목 정상 배포 claim 1개 사용).
- SHADOW → LIVE 전환 승인: 해당 없음(결함 수정, 경제적 변경 없음).
