# arm 변경 기록: closed_reconciliation_guard_v1 (종료일 재대사 보완)

- 분류: 결함 수정 (경제적 변경 없음). 전략 arm·배정·설정·안전 한도·주문 규칙은 건드리지 않는다. `paper_experiment_*.py`, `paper_policy.py`, `config/settings.py`, `paper_hot_policy.py`, 지시문 변경 없음(2026-11-04 18:05 diff 기준).
- 대상 arm: 없음(14개 arm 전부 영향 없음). 영향 경로는 체결 추적기(tracker)와 수신기 루프.
- 영향 경로(file:line, 소스 2026-11-04 17:4x 기준; 18:28 수정분은 별도 표기):
  - `telegram/listener.py` 1049 `experiment_supervisor.tick` → 1067 `tracker_checkpoints.tick` (새 `_TrackerCheckpointPublisher`: tracker 전후 state.json 게시, 단계 콜백) → `trading/execution_log.py` 4995~4996 `_historical_work_deferred`·`_prune_historical_tasks` → 5050~5070 당일 전용 작업 목록 → 3962~3995 `_closed_due`(limit_blocked → watermark → fingerprint) → 830~845 `_closed_limit_blocked` → 3904~3922 경제 증거 캐시(4개·512 KiB) → 4739~4760 `_materialize_history/_materialize_accounting`(보류 시 skip) → `trading/tracker_diagnostics.py` 251 `runtime/logs/tracker-diagnostics-YYYYMMDD.jsonl`(32 MiB 상한).
  - 전일 TTL 정산 공개(18:28 수정): `trading/cancellation_settlement.py` 448~468 `_publish_settlement`(guard 선행) → `trading/execution_log.py` 4747~4772 `_materialize_settlement_accounting`(주문 9필드·증거 해시 검증, guard 3회) → 4774~4786 `_assert_settlement_accounting_authority`(limit_blocked·원주문 불변·committed 증거) → 4788~4790 보류 우회(authorization 있을 때만) → sidecar readback 473~475(`evidence_sha256`+`original_order_number`+`mode`+`final_evidence_committed`) → 공개 행·예약 해제.
  - 변경 파일: `telegram/listener.py` (+97줄, after sha 3491521c93aa…), `trading/execution_log.py` (+254줄, 372625295122…), `trading/closed_ledger_calendar.py` (+8줄, 0d1184e9fba4…), 신규 `trading/tracker_diagnostics.py` (4bd0a070ae6b…), 문서 `docs/CLOSED_RECONCILIATION_PERFORMANCE.md`, 테스트 4개 수정·3개 신규. 18:28 추가 수정: `trading/execution_log.py`(710034f5…), `trading/cancellation_settlement.py`(7c074832…), 신규 `tests/test_cancellation_prior_day_publication.py`(7건, 8b72a1e8…), README·성능 문서. 배포 도구(소스 트리 밖) `work/deploy_receiver_immutable.py`(1c0af3cf…), 신규 `work/receiver_checkpoint_rollback.py`(b0cc85a3…). `work/stability_watchdog.py`(감시기 reader, 설치본 미반영).
- 소스 manifest·SHA: APP 격리 스위트 manifest `app-tests-isolated-provenance-183447993044-source-manifest.json` (sha 454c37bd6e0316d3…, 853 파일, 현재 소스와 전부 일치 확인, 3,127 PASS; 17:46 manifest 37ab2404… 852파일 대비 변경 4·추가 1·제거 0), bundle a891417cfd4e2928…. 소스 백업: 17:19 `/Volumes/AIStockData/backups/closed-reconciliation-source-20261104/171923527819/manifest.json` (c0a440798f43dd7a…), 18:22 수정 전 12파일 `/Volumes/AIStockData/backups/closed-reconciliation-review-1810-20261104/182231349099/manifest.json` (488f25de50490401…). 배포 당일 manifest는 다시 만든다. 현재 운영 실행본 manifest 16d7e11810c1…(V9, 13:32:14).
- 상태 전환: 없음(기능 플래그 없음). SHADOW 해당 없음: 주문·취소 판단을 바꾸지 않는 관측·순서 변경이며, 대신 배포 첫 거래일에 tracker-diagnostics 로그로 효과를 측정한다.

## 1. 목적

설계가 말하는 것: 종료된 과거 거래일의 비용 재대사는 당일 안전 대사(00 저장, 잔고·미체결, 예약 해제, 보유 청산)를 방해하지 않아야 하고, 안정된 날은 재조회를 멈춰야 한다. 코드가 하던 것: 과거 7일을 5~20분마다 다시 조회·전체 파싱·해시했고(달력 미검증으로 중단 불발, 10/27은 2만 행 초과로 영구 미해결), 그 작업이 monitor와 같은 listener 스레드에서 돌아 루프를 2~5분씩 막았다(2026-11-04 장마감 뒤 state.json 미갱신 최대 286초). 이를 설계대로 맞춘다.

## 2. 기대 효과와 측정 지표

1. 장중(08:55~15:40) 과거일 tracker 작업 0건: `tracker-diagnostics-YYYYMMDD.jsonl`에서 `work_day < 당일`인 `tracker_task` 단계 수 = 0 (단계 기록 기준).
2. 09:00~09:05 monitor tick 시작 지연: 감시기 샘플 `tick_age_seconds` 최대값 < 60초 (오늘 장외 160~286초).
3. 장외 재대사 단계 1건 소요 시간: `tracker_task` 단계 `elapsed` p95와 최대값을 첫 3거래일 기록(오늘 추정 100~280초, 목표는 측정 후 설정).
4. 수신기 메모리: top MEM 또는 진단 `current_rss_bytes` 최대 < 1.5 GB (오늘 3.1~3.9 GB).
5. 10/27 `closed_day_management.status = limit_blocked`로 broker 재조회 0건(`broker_observation_repeats` day=20261027 행 증가 0).
6. 금융 행 불변: `intents`·`orders`·`fills`·`profits`의 배포 전후 열 단위 해시 동일(관측 행 append는 허용).

## 3. 측정 방법과 기간

- 원장 복사본(`ledger-reconciliation` 절차)과 `runtime/logs/tracker-diagnostics-YYYYMMDD.jsonl`, `observability/watchdog/<날짜>-HHMM.sample.json`을 배포 다음 거래일(2026-11-06 금) 장마감 뒤와 3거래일 뒤(2026-11-11 화)에 집계한다.
- 비교 기준: 2026-11-04 관측치(이 기록 2항 괄호 안 값)와 `reports/봇 점검 2026-11-04 1640 전체 점검.md` 2.1절 순회표.
- SHADOW 만료일: 해당 없음. 다만 2항 1·2·5·6이 첫 거래일에 충족되지 않으면 4항 롤백을 검토한다.

## 4. 롤백 절차

- 코드만 되돌린다. 금융 원장은 되돌리지 않는다(새 경제 이벤트 소실 금지). 설정 변경은 없으므로 설정 롤백 없음.
- 롤백 대상 코드: V9 실행본(manifest 16d7e11810c1…, 백업 `backups/receiver-reconciliation-v9-20261104T133146076423/`) 또는 Codex 소스 백업(c0a44079…).
- 필수 선행 조건: 옛 listener의 `_load_state`는 state.json 최상위 키 집합이 다르면 `state_storage_failure`로 기동 실패한다(`set(saved) != set(fresh)`, 실행본 listener.py 140~192). 새 코드가 추가하는 키 4개(`state_publication`, `tracker_diagnostics`, `telemetry_publish_error`, `recorder_snapshot_observed_at`)만 제거하는 projection이 18:45 generic 도구의 실제 실패 분기에 연결됐다(`work/deploy_receiver_immutable.py` 911~947 → `work/receiver_checkpoint_rollback.py`: 정지 전 마커·상태 boundary, 공유 cutover lock, 0600 원본 백업, 4키 제거·나머지 값 보존 검증, 구 reader AST probe, 세대 재확인, 원자 교체, 코드만 복원; 정상 handoff 뒤에는 2차 정지·projection 거부). focused 48/48, 합성 사본 리허설 PASS(old-reader 8경우, 금융·배정·설정·plist 해시 전후 동일, `next_offset`·카운터 보존). 배포 당일 실제 최신 checkpoint 불변 백업 사본으로 리허설을 다시 해야 claim 가능. state.json 전체를 과거본으로 되돌리는 방식은 offset 회귀 위험으로 금지. 승인 JSON에 `receiver_checkpoint_rollback` 정책 블록(도구·의존성 4개·old listener SHA, 4키)과 승인 문구('checkpoint projection' 또는 '진단'+'호환')가 없으면 도구가 claim 전에 거부한다.
- 창·승인·claim: 롤백도 배포 창(화·목 08:30 전 / 15:40 후)과 claim 1개를 소비한다. 장중 긴급 롤백은 예외 토큰과 사용자 승인이 필요하다.

## 5. 위험

| 실패 모드 | 감지 방법 |
|---|---|
| (해결, 18:28) 전일 주문의 TTL 정산 공개가 장중 보류에 걸리던 문제 → 전용 writer `_materialize_settlement_accounting`이 보류만 우회. 잔여: 20,000행 초과(limit_blocked) 일자는 당일 포함 영구 미공개 → 그 날 TTL 예약금은 수동 검토 | `closed_day_management[].status=limit_blocked`인 날짜에 `buy_ttl_cancellation_settlements`는 있고 `buy_ttl_cancellation_publications`가 없는 주문. 현재 일일 비용 관측 행은 수백~3천(11/02~11/04)이라 비현실적 |
| 보호창(09:00~09:05) 안의 전일 정산 공개 1건이 그 날 관측 전체(≤20k행)를 listener 스레드에서 파싱 | 진단 `accounting` 단계 `work_day < 당일`의 시각·소요(벤치 1.6초/일). 수용 |
| [배포 차단] generic 도구 미체결 게이트: `deploy_handoff_runtime.db_summary` `unfilled_orders`가 만료·취소 매수 주문을 영구히 셈(16:16 복사본 11건) → `PRECLAIM_UNRESOLVED_ORDER_REQUIRES_RECONCILIATION`(claim 전), `UNRESOLVED_ORDER_REQUIRES_RECONCILIATION`(정지 뒤), `POSTSTART_UNRESOLVED_ORDER` | 배포 결과 JSON reason. 도구 수정·사본 리허설 전에는 배포 불가(19:35 검토 발견 4) |
| [배포 차단] generic 도구는 `deployed_paused`에서 끝나고 정상 거래 전환 단계가 없음 → 금요일 09:00 주문 0건 위험(V7 11:43~13:32 재현) | 배포 뒤 state.json `maintenance_paused`·`maintenance-quiet.lock` 잔존, monitor tick 없음. handoff 단계 준비·리허설 전에는 배포 불가(발견 5) |
| 옛 코드 롤백 시 `state_storage_failure` | 롤백 리허설(사본) 결과 없이는 배포 불가. 배포 당일 Codex 보고에 projection 리허설 결과 포함 |
| 새 게시 경로 자체 실패(`telemetry_publish_error`) | state.json `telemetry_publish_error.failure_count` 증가, 주문 정책은 불변(코드 주석·테스트) |
| 보류 범위 과대: 공휴일·주말에도 08:55~15:40 과거일 작업 보류 | 무해(지연만). 진단 `historical_work_guard.basis` 확인 |
| 진단 로그 32 MiB 초과 시 진단 중단(잘라내기 없음) | `tracker_diagnostics.failures.write` 노출. 기록량은 첫 거래일에 측정 |
| 220~286초 정지의 실제 원인 미해명 → 보완이 장외 CPU를 못 줄일 수 있음 | 2항 3번 측정. 장중 안전은 보류 규칙으로 별도 확보 |
| 설치된 감시기(불변 사본 `research-readonly/stability-watchdog-20261103-ce74d03397a5/`)는 새 reader를 모름 | 감시기 교체는 별도 승인 범위. 교체 전까지 감시기 `tick_age` 경보 의미는 종전과 동일 |

## 6. 승인자 / 승인 시각

- 소스 수리 승인: 사용자, 2026-11-04 17:15 회신("네, 전달할게요")으로 보완 요청 전달. Codex 수리 완료 17:54. 18:10 검토 1~3항 수정 완료 18:56, 19:35 대조 확인(`reports/배포 전 검토 2026-11-04 1935 발견1 수정·배포 도구 게이트.md`). 배포 도구 수정(발견 4·5)은 미완. 19:35 회신(`reports/Codex 회신문 2026-11-04 1935 배포 도구 게이트.txt`: 발견 4·5 수정, 목 14:00 준비/미준비 보고, 미준비면 11/10 15:40 이후로 연기)은 사용자가 2026-11-05 08:29 "전달"로 Codex 전달 확인. Codex 응답 미수신(08:29 기준 신규 보고 없음, 검토한 소스·도구 해시 5개 그대로).
- 배포 승인: 미작성 (2026-11-05 목 15:40 이후, 당일 격리 APP 전체·복사본 회귀·백업 해시·롤백 projection 리허설·claim 1개 조건이 보고된 뒤 사용자가 결정).
- SHADOW→LIVE 승인: 해당 없음.
