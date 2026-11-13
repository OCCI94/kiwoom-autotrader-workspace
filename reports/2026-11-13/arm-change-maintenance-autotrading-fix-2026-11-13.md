# arm 변경 기록: maintenance-autotrading-fix-2026-11-13 & EXP_QLS (SHADOW)

- 분류: 결함 수정(경제적 부수효과 있음: 막혀 있던 정상 발주 복구) + 신규 후보 관측기(`EXP_QLS` `SHADOW` 전용, 주문 권한 없음)
- 대상 arm:
  - 공통 발주·원장 경로 결함 수리: 기존 승인된 14개 활성 arm 전체 (`EXP_A`, `EXP_B`, `EXP_B2`, `EXP_B3`, `EXP_C`, `EXP_D`, `EXP_S1`, `EXP_S2`, `EXP_S3`, `EXP_S4`, `EXP_S6`, `EXP_VSS`, `EXP_GDR`, `EXP_LDR` 및 네이티브 `S5` 통합 계층) — 전략 임계값·예산·손절·익절 파라미터 변경 **0건**
  - 종가 대체 경로 결함 수리: `EXP_D` (`15:18~15:20` 종가 선정 누락 시 폴백 동작 복구)
  - 신규 관측 게이트: `EXP_QLS` (`SHADOW` 전용 읽기 전용 관측기, `order_permission = False`, `active_arms` 미편입)
- 영향 경로(file:line, 2026-11-13 후보 트리 `/Users/tr-oh/Documents/Codex/2026-10-26/new-chat/work/maintenance-autotrading-fix-20261113/outputs/ai-stock/kiwoom_autotrader` 기준):
  1. `trading/execution_log.py:1281` `_ro_db()` — 10개 핵심 테이블 검증 기반 읽기 전용 SQLite 커넥션으로 `broker_observations` 조회 락 경합 제거 (2026-11-12 배포된 `paper_account_balance_observations` 원장 복구 증인 스키마·트리거·검증 로직 100% 보존)
  2. `trading/execution_log.py:1799` `_sync_expiry_index` — `BEGIN IMMEDIATE` 진입 전 만료 증서 증분 인덱스 사전 동기화로 쓰기 트랜잭션 보유 시간 단축
  3. `trading/execution_log.py:5015,5120,5140,5198` — 읽기 경로의 `_ro_db()` 전환
  4. `trading/expired_order_certificates.py:1` — 만료 주문 증서 증분 인덱싱(`expired_order_certificate_index`)
  5. `trading/broker_observation_repeats.py:1` — 동일 브로커 관측 반복 요약의 별도 사이드카 DB 분리로 본 원장(`execution-ledger.sqlite3`) 쓰기 경합 완화
  6. `utils/http_queue.py:85,101,420,485` — `HTTPNotSentError`에 미전송 가드 `reason_code` 보존
  7. `trading/paper_reconciliation.py:34,147,254,257,306,357,373,385,388,417,488-516` — `PaperReconciliationError`에 구체적 사전 거절 `reason_code` 보존
  8. `trading/paper_policy.py:91,326-355` — `PaperExecutionPolicyError`에 하위 대사·정책 `reason_code` 전달
  9. `api/orders.py:51,126-332` — `OrderError`에 허용 목록 검증된 `reason_code` 보존하여 상위 모니터가 원인 코드(`order_not_sent`, `insufficient_cash` 등)를 정확히 기록하도록 수리
  10. `trading/paper_experiment_monitor.py:1650` — `extended:closing` 상태가 `None`뿐 아니라 `'selection_window_expired'`로 마킹된 경우에도 `15:18 <= hm < 15:20` 구간에서 `EXP_D` 폴백 선정이 정상 실행되도록 결함 수정 (`status == 'selected'`일 때는 중복 실행 금지 유지)
  11. `trading/paper_experiment_qls_shadow.py:19,54` 및 `trading/paper_experiment_monitor.py:47,1547` — 봉인된 QLS 체크포인트(`2026-11-12` 체크포인트 디렉터리)를 읽기 전용(`O_RDONLY | O_NOFOLLOW`)으로 관측해 `paper_experiment.decisions` 및 `state.json`의 `qls_shadow`에 기록하는 `SHADOW` 전용 관측기 (`ORDER_PERMISSION = False`, 브로커/네트워크/원장 쓰기 호출 0건)
- 가드·감사 훅 무수정 검증:
  - `utils/runtime_deployment_guard.py`, `deploy_handoff_runtime.py`, `utils/control_audit.py`, `config/settings.py`, `utils/telegram_service.py`, `utils/account_service.py` 총 6개 가드/감사 파일은 라이브 베이스라인(`recovery-balance-witness-fix-20261112`, SHA `a32a4a7c18a9bf9adc03eec367eb2ddcb05749aed47c709e17dbc4d47dd502bf`)과 바이트 단위 100% 동일(`cmp -s` 검증 완료)
- 소스 manifest/SHA:
  - 후보 런타임 패키지(`190`개 파일) `manifest_sha256`: `5e13d15d0449cf9d99952ed583ac324c4929e73fa1e983cff0c9c3ad3b64bf5c`
  - 격리 전체 테스트 스위트(`245`개 모듈, `3,218`개 테스트, 실패 `0`, 스킵 `0`) 증빙:
    - 결과 JSON: `/Users/tr-oh/Documents/Codex/reports/2026-11-13/app-tests-isolated-provenance-081709962471.json`
    - 소스 매니페스트 JSON: `/Users/tr-oh/Documents/Codex/reports/2026-11-13/app-tests-isolated-provenance-081709962471-source-manifest.json` (`sha256: 5e41df2b387ad87a1ba24e6a3c3c51aa97c2d16eb3f6640c4304ac4adcff2417`)
  - 장전 오프라인 배포 리허설 증빙: `/Users/tr-oh/Documents/Codex/reports/2026-11-13/075755221680-receiver-only-offline-rehearsal.json` (`status: passed`, `registered_control_audit.status: verified`)
- 상태 전환:
  - 기존 14개 활성 arm: `LIVE` 유지 (결함 수리만 반영, 파라미터 불변)
  - `EXP_QLS`: `(없음) → SHADOW` (`expires: 2026-11-27`, 주문 권한 `False`)

## 1. 목적
5.6 GB 이상 누적된 실행 원장(`execution-ledger.sqlite3`)의 브로커 관측·만료 증서 스캔으로 인한 SQLite 쓰기 락 경합(`database is locked`), 미전송 주문 예외(`OrderError`)의 `reason_code` 유실(`order_error` 일괄 뭉뚱그림), 그리고 `15:15~15:18` 창 만료 마킹(`selection_window_expired`) 이후 `15:18~15:20` `EXP_D` 폴백이 건너뛰어지던 코드 결함을 가드 수정 없이 수리하여 기존 14개 모의투자 arm의 정상 자동매매 흐름을 복구하고, 퀀트 리더 포트폴리오(`EXP_QLS`)는 주문 없는 `SHADOW` 관측기로만 등록한다.

## 2. 기대 효과와 측정 지표
- 지표 1 (SQLite 원장 락 경합): 장중(`09:00~15:30`) `paper_experiment.decisions` 및 수신기 로그의 `database is locked` / `ledger_busy` 발생 건수 (변경 전 다수 발생 → 변경 후 `0`건 또는 급감).
- 지표 2 (사전 거절 사유 분류 정확도): `paper_experiment.decisions`에서 `reason_code == 'order_error'`(원인 불명 일반 오류) 비율 감소 및 구체 사유(`insufficient_cash`, `broker_pending_order_exists`, `order_not_sent`, `reconciliation_state_changed` 등) 분리 기록 비율.
- 지표 3 (`EXP_D` 종가 폴백 정상화): `15:15~15:18` 연장이 `selection_window_expired`로 끝난 거래일에 `15:18~15:20` 구간 `EXP_D` 후보 평가 이벤트 기록 건수 (`0`건 누락 → 거래일당 `1`회 정상 평가).
- 지표 4 (`EXP_QLS` `SHADOW` 안전성): `paper_experiment.decisions` 내 `arm = 'EXP_QLS'`의 `shadow_observation` 일별 기록 건수(`>= 1`/거래일) 및 실제 원장 `orders` 테이블 내 `EXP_QLS` 주문 건수(`== 0`건 절대 유지).

## 3. 측정 방법과 기간
- 원장 복사본(`sqlite3 file:...?mode=ro&immutable=1`)과 `paper_experiment.sqlite3` 복사본에서 일별 `orders`, `fills`, `decisions` 테이블을 집계(`ledger-reconciliation` 스킬 절차 준수).
- 측정 기간: `2026-11-13` 장전 배포 직후부터 `10`거래일 (`EXP_QLS` `SHADOW` 만료일: **`2026-11-27`**). 만료일까지 별도 인간 승격 승인이 없으면 `EXP_QLS`는 자동 `OFF` 정책 대상이다.
- 비교 기준: 배포 직전 최근 5거래일(`2026-11-06 ~ 2026-11-12`)의 동일 시간대 의사결정·원장 기록.

## 4. 롤백 절차
- 배포 실행 시 `~/Library/Application Support/KiwoomAutotrader/backups/20261113-<시각>-preopen-maintenance-autotrading-fix/`에 자동 생성·해시 검증되는 직전 런타임 패키지(`189`개 파일, `BASELINE_SHA = a32a4a7c18a9bf9adc03eec367eb2ddcb05749aed47c709e17dbc4d47dd502bf`), `bot.plist`, `settings.json` 백업으로 코드와 설정만 복원한다.
- 실행 원장(`execution-ledger.sqlite3`)은 절대 과거 백업으로 덮어쓰지 않는다(금융 기록 보존).
- 정상 롤백 창은 화·목 공식 장외 창이며, 긴급 시에만 사용자 명시 예외 토큰(`receiver_only`)과 새 배포 claim 1회를 사용한다. 롤백 직후에도 `state.json` 5대 항목(`pid`, `status`, `paper_experiment.enabled`, `active_arms` 14개, `halted`)을 동일하게 검증한다.

## 5. 위험
- 위험 1: `08:50:00 KST` 토스 캘린더 워커 지연 시 장전 배포 창(`08:56` 안전 마감) 초과 → `deploy_preopen_20261113.py`가 코드 교체 전에 즉시 중단(`fail-closed`)하므로 기존 수신기(`PID 2130`)가 무중단 유지됨.
- 위험 2: `stability-watchdog`(`PID 2217`) 정각(`:00`) 스냅샷과 파일 교체 시점 중첩 → `deploy_preopen_20261113.py`가 `:06 ~ :24`초 구간에만 기동하고 `control_audit`에 `code_deploy` 이벤트를 남기므로 워치독이 정상 등록 제어로 검증함(오프라인 리허설 `registered_control_audit.status == verified` 확인 완료).
- 위험 3: 발주 블로커 해소로 기존 14개 arm의 모의 매수 주문이 실제로 체결되기 시작함 → 각 arm별 예산·종목 한도·손절 규칙은 기존 `paper_policy` 및 `paper_reconciliation` 검증을 그대로 거치며, 장중 `state.json`의 `halted` 및 미체결 상태를 모니터링함.

## 6. 승인자 / 승인 시각 및 배포 완료 기록
- 소스 수리 및 격리 검증 승인: 사용자(`tr-oh`), `2026-11-13 07:45 ~ 08:17 KST` 대화 승인 (`245`개 모듈 / `3,218`개 테스트 통과, 실패 `0`).
- 코드 배포 명시 승인(장중 예외 토큰 `intraday-code-token`): 사용자(`tr-oh`), `2026-11-13T09:24:56+09:00` 명시 선택(`"장중 코드 배포(intraday-code-token) 명시 승인 — 미체결 0건 확인 후 지금 즉시 교체 실행"`).
- 실제 배포 완료 시각 및 증빙:
  - 배포 완료(`new_state_published_at`): `2026-11-13T09:35:29.045650+09:00` (`old_pid=2130` → `new_pid=30100`, `scope=receiver_only`, `orders_before=157`, `orders_after=157`, `pending_intents=0`, `unfilled_orders=0`)
  - 배포 보고서 JSON: `/Users/tr-oh/Documents/Codex/reports/2026-11-13/093420-intraday-maintenance-receiver-only-deploy.json`
  - 소비된 단일 배포 claim: `/Users/tr-oh/Library/Application Support/KiwoomAutotrader/code-deployment-claims/20261113-exception-bee684e07107d6e846959a3c0e53b4ebd8f9c75b07f707fcf658256ca250ad3b.claim.json` (`sha256: 2a31125613a755dff10d8cea91ea1cf7e375d6c96b4273e9faa96aee7c432790`)
  - 백업 경로 및 해시: `/Users/tr-oh/Library/Application Support/KiwoomAutotrader/backups/20261113-093420-intraday-maintenance-autotrading-fix`
    - `package_sha256`: `a32a4a7c18a9bf9adc03eec367eb2ddcb05749aed47c709e17dbc4d47dd502bf`
    - `plist_sha256`: `12ff223b3bbc3ac104489c78fcf7878822df368c420e016126926e45d61cea0d`
    - `settings_sha256`: `70ada618b8d661345eac4d5be6da6232b831667392ab5de240e5fc14fbacc66b`
    - `ledger_sha256`: `62811d3934f0e635ed78fdf813e01f26b9856e11cf2d47477222be666ad156ed`
  - 배포 직후 라이브 `state.json` 검증 (`2026-11-13T09:48:28+09:00` 기준):
    - `pid`: `30100`, `status`: `"running"`
    - `automation_status.paper_experiment.enabled`: `true` (`failure: null`, `failed_ticks: 0`)
    - `active_arms` (14개): `["EXP_R", "EXP_A", "EXP_B", "EXP_B_TP", "EXP_E", "EXP_C", "EXP_D", "EXP_ETF_B", "EXP_GDR", "EXP_LDR", "EXP_ORB_A", "EXP_ORB_B", "EXP_VSS", "S5_steady_uptrend_vwap"]`
    - `halted`: `false` (`quarantined: {}`, `execution_recording.ready: true`, `gap_count: 0`, `maintenance_status.paused: false`)
    - `qls_shadow`: `{"gate": "EXP_QLS", "state": "SHADOW", "expires_on": "2026-11-27", "order_permission": false, "submitted_orders": 0}`
- `EXP_QLS` `SHADOW → LIVE` 승격: **미승인** (`order_permission = False` 고정).
