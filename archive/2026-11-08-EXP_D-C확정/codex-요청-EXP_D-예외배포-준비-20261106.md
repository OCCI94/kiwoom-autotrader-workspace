# Codex 요청: EXP_D 수신기 패키지 예외 배포 준비 (목표 2026-11-09 월 15:40~ / 예비 11-10 화 정상 창)

작성 2026-11-06 금 16:1x KST. 전신 `codex-요청-EXP_D-10파일-배포재준비-20261106.md`(11-05 17:3x)는 `archive/2026-11-06-EXP_D-예외배포/`로 옮겼고, 그 §2-A(변경 기록)·2-D(journal)·2-E(당일 점검표)는 이 문서가 흡수한다. 변경 기록: `reports/2026-11-05/arm-change-exp-d-late-exit.md`. Codex 준비 보고: `reports/2026-11-05/190557970832-receiver-ten-files-normal-handoff-preparation-final.txt`. 무거래 원인: `/Users/tr-oh/Documents/Codex/ai-stock-점검-20261106-무거래원인.md`.

## 0. 승인 상태와 범위

- **승인된 것**: 사용자 2026-11-06 15:5x "배포준비" — 11-10 전 **예외 배포의 준비**. 소스 수리, 배포 도구 수정, 격리 테스트, 런타임 복사본 리허설, 읽기 전용 분석은 주말 내내 허용.
- **승인되지 않은 것** (각각 별도 문장이 있어야 한다): 코드 배포 실행(예외 토큰), 금융 원장 쓰기(만료 증명서 포함), 작업자·journal 재시작, 설정·arm·한도 변경, 설치된 watchdog 교체.
- 11-09(월)은 거래일: 09:00~15:30 운영 런타임·설정·원장·작업자 무접촉, 브로커 API 호출 금지. 15:40 이후에는 도구·의존성을 고치지 않는다.
- 11-06(금) 밤에는 이미 승인된 섹터 ETF 장후 수집(`codex-요청-섹터ETF-일봉-투영-장후수집-20261106.md`, 16:30~17:30 시작)이 먼저다. 그 뒤에 2-A를 시작한다.

### 왜 11-09(월) 장 마감 후인가

| 후보 시각 | 가능 여부 | 근거 |
|---|---|---|
| 11-06(금) 밤 | 불가 | 도구 수정·리허설·미결 2건 해소가 남았고, 섹터 ETF 장후 수집이 같은 IPC를 쓴다. 토큰은 15분짜리라 사용자가 자리에 있어야 한다 |
| 11-07(토)·11-08(일) | 불가 | 가드 `_official_session`(`runtime_deployment_guard.py` 88·92행)이 kind와 무관하게 **당일** Toss claim/calendar 쌍(`session_day == today`, claimed_at↔observed_at 900초)을 요구한다. Toss 슬롯은 주말에 돌지 않는다(10-31·11-01 claim 0건) |
| 11-09(월) 장전 <08:30 | 불가 | 첫 Toss 슬롯이 08:50이라 당일 쌍이 없다 |
| **11-09(월) 15:40~23:59** | **가능** | 거래일·세션 밖·당일 1520 슬롯 쌍. `deploy_window_check --kind exception --at "2026-11-09 15:45"` → allowed(토큰 필요) |
| 11-10(화) 15:40~ (정상 창) | 예비 | 예외 claim은 `normal_quota_consumed false`라 정상 quota가 남는다. 월요일이 실패하면 화요일 정상 창으로 간다 |

효과: 월요일 배포 성공 시 **11-10(화)부터 거래 재개**. 정상 창이면 11-11(수)부터. 11-09(월)는 어느 쪽이든 무거래다.

## 1. 확정 사실 (11-06 16:0x 읽기 전용 검증)

| 항목 | 값 |
|---|---|
| 운영 | PID 26954 running, 14 arm, `maintenance_paused` 없음. runtime 패키지 11-04 13:32 그대로. LaunchAgent plist 6개는 11-03 이후 무변경, `collector-runtime`은 10-29 그대로 → 오늘 소스 변경은 소스 전용이었다(계약 위반 없음) |
| **패키지 drift** | 11-05 19:05 후보(10파일, package sha `16c3e964…`)는 **더 이상 유효하지 않다.** 오늘 12:37 `kiwoom_autotrader/main.py`(+13줄, `telegram-account-listen` 분기에 단계별 실패 로깅), 12:42 `telegram/collector.py`(+43줄, 실패 분류 표·asyncio 예외 핸들러·상태 파일 쓰기 보호)가 바뀌었다. 둘 다 `package_manifest`(`*.py`) 대상 → 후보는 **12파일**. diff 상 main.py 변경은 collector 명령 분기와 공통 except 절(`collector_worker`일 때만 추가 출력)이고 collector.py는 수신기가 import하지 않는 것으로 보이나, 2-A(2)에서 확정한다. 승인 JSON `expected_changed_python_files` 10→12, package sha 재계산 |
| 당일 스위트 선례 | 11-06 12:42 APP 238모듈 3,170 PASS/실패 0/제외 0(`app-tests-isolated-provenance-124257842751.json`, `source_unchanged_after true`), WORK 970 PASS. 소요 APP 126초, WORK 192초 → 11-09 15:40 이후 재실행에 문제 없다 |
| 새 도구 | `work/deploy_receiver_normal_handoff.py`(852줄, 11-05 18:53, sha `36feb252…`). **kind `after-close-code-approval`만 허용**(537행 `HANDOFF_NORMAL_AFTER_CLOSE_APPROVAL_REQUIRED`), 585~587행이 `window['normal_window']`를 요구 → 가드 `deployment_window` 120행의 화·목(`NORMAL_DEPLOYMENT_WEEKDAYS={1,3}`) 조건에 묶인다. claim 경로 635행 `<date>-normal.claim.json` 고정. **그대로는 월요일에 못 쓴다** |
| 가드의 예외 kind | `kiwoom_autotrader/utils/runtime_deployment_guard.py`(11-03 14:54, sha `26e9a10d…`) 265~269행 `exception-code-token`: `scope receiver_only`, `exception_code_change_explicit: true`, 본문에 예외·코드·배포, issued→expires **≤ 900초**. claim 이름 `<date>-exception-<approval_sha256>.claim.json`, `normal_quota_consumed false`(11-04 V5~V9 선례 `code-deployment-claims/20261104-exception-*.claim.json`). 그 밖의 `assert_code_deployment` 조건(schema, pid, manifest sha, `_official_session`, `_throughput_stability`, 수신기 identity)은 정상 kind와 같다 |
| 신선 ready 게이트 | 도구 `_state`(222~228행) → `receiver_state_snapshot.recording_health` 141행 `age_seconds ≤ 5 and ready` (state.json mtime이 읽는 순간 5초 이내). 11-05 19:00 실 런타임 check-only가 `HANDOFF_INITIAL_RECEIVER_NOT_FRESH_READY`로 차단됐다(`190059934841-actual-runtime-readonly-check-only.json`). 장외 순회 주기에서 이 5초 창을 맞출 수 있는지 **미확인** |
| 미결 게이트 | 도구 `_financial` 307행 → `receiver_release_preflight.financial_summary`(소스 `ExecutionTracker._pending_order_rows` 사용: `_confirmed_cancelled_quantity` 또는 `_confirmed_expired_quantity` = `_paper_prior_order_terminal` / `verified_certificate`). 11-05 18:43 사본(`184326666169-final-frozen-copy-fence-terminal-validation.json`): 11-03 9건은 증명서로 해소, **11-04 2건 남음** — 218972(002790, 7주)·219563(010660, 52주), `cancel_broker_rejected`, 증명서 없음. 운영 수신기 recovery는 `expiry_settlements unconfirmed`를 60초마다 반복(`remaining_registered_pending 2`). Codex 11-06 10:55 `105517054259-218972-219563-saved-terminal-reconciliation.json`: `HISTORICAL_ORDER_REMAINDERS_CONFLICT_WITH_EMPTY_CURRENT_PENDING` — 11-04 kt00007 상세는 잔량 7/52·체결 0·'주문완료'이고 11-06 08:59 ka10075 통합미결은 비어 있다. 누락 증거로 "ord_remnq=0을 명시하는 당일 kt00007 + 이후 전체 ka10075에서 원주문·파생 부재"를 들었다. **해소 전엔 claim 전에 막힌다** |
| 시간 예산 | 토큰 15분(가드) vs 도구 `total_timeout 420초`, `ready_timeout 180초`, `MARKER_LEASE_SECONDS 180`, `PHASE_FRESH_SECONDS 90`. `authorize()`가 598·629·632·**753행(마커 제거 뒤)**에서 재검사한다 → 토큰 만료가 비가역 경계 뒤에서 `HANDOFF_APPROVAL_EXPIRED`를 낼 수 있는지 확인 필요. 11-05 사본 리허설 3회는 0/97/108초 |
| 보호 PID (16:05 생존) | watchdog 49149, toss 49165, candidates 49184(모두 11-05 16:18 시작), journal 95088(11-04 16:17, `.previous` 바이너리 고아). collector 5634는 ps에 없음 → 당일 재확인. 수신기 26954는 재시작 대상 |
| 백업·승인 초안 | 11-05 `work/receiver-normal-handoff-preparation-20261105/175056904152/final-190557970832/` (UNSIGNED 승인 초안·KRX 양식·실행 계획·바인딩), SSD 준비본 `/Volumes/AIStockData/backups/receiver-normal-handoff-source-20261105/175056904152/`. 전부 10파일·정상 kind 기준이라 **재작성** 대상 |

## 2. 과제 (시간순, 과제마다 §4 형식으로 보고)

### 2-A. 11-06(금) 밤, 섹터 ETF 장후 수집 뒤 — 전부 읽기 전용

1. **신선 ready 실측.** 실 런타임에 `deploy_receiver_normal_handoff.py --check-only`(읽기 전용)를 세 번(예: 18:00, 19:30, 21:00) 돌려 통과/차단 이유를 기록한다. 같은 시각대 `telegram/state.json` mtime 간격(장외 순회 주기)과 `execution_recording.ready` 값을 함께 적는다. 5초 창을 못 맞추면 그 사실을 2-B(3)의 입력으로 넘긴다. 시각만 바꿔 재시도하는 식의 우회는 하지 않는다.
2. **패키지 범위 재확정.** `package_manifest`로 source↔runtime 전체 diff → 변경 파일 목록(12 예상)·새 package sha. `main.py`·`telegram/collector.py` 변경이 수신기 실행 경로(`telegram` 명령, supervisor, `listener.py` import 그래프)에 닿는지 `file:line`으로 확정. 닿으면 변경 기록에 "수신기 동작 변경"으로, 안 닿으면 "수신기 경로 변경 없음"을 근거와 함께.
3. **미결 2건 해소안 (쓰기 없음).** 운영 코드(`runtime/kiwoom_autotrader/trading/expired_order_certificates.py`의 `capture_certificates`, `execution_log.py`의 `_paper_prior_order_terminal`)가 218972·219563에 대해 **정확히 어느 조건에서** 실패하는지 저장된 관측(11-04 kt00007, 11-05·11-06 ka10075 페이지, 취소 거절 기록)으로 재현한다. 그 위에서 세 안을 비교한다:
   - (a) 운영 수신기가 11-09 08:59 배치까지 스스로 확정할 가능성 — 어떤 읽기·응답이 있어야 하는지, 지난 이틀 왜 못 했는지.
   - (b) 11-04 11:15 선례(11-03 9건 `paper_expired_order_certificates`, schema `paper-expired-terminal-proof-v1`, semantics `terminal_remainder_not_fill_or_fresh_account`)처럼 앱 writer로 증명서 2건을 **사본**에서 생성 → preflight `unfilled_orders 0` 확인 → **사용자 승인 뒤** 실 원장에 기록. 기록 시점(수신기 가동 중 외부 쓰기의 잠금·`data_version`·장외 대사 루프와의 경합), 되돌릴 수 없음, 필요한 승인 문장을 명시.
   - (c) 도구 preflight가 사람 범위 terminal 기록을 받도록 변경 — `receiver_release_preflight.py` sha 변경이 승인 JSON `tool_dependencies_sha256`에 반영돼야 함.
   권고 하나. 보고 첫 줄에 "해소 전엔 11-09 배포 불가"를 쓴다.

### 2-B. 11-07(토)

1. **도구 예외 kind 지원.** `deploy_receiver_normal_handoff.py`가 `exception-code-token`을 받도록: 537행 kind 검사, 585~587행 창 검사(예외 kind는 `window['official_phase']=='closed'`·세션 밖이면 허용, 요일 무관, 장중 거부 유지), 635행 claim 경로(가드 명명 규칙을 따름), 승인 JSON `exception_code_change_explicit`, 본문 예외·코드·배포. **정상 kind 경로는 바꾸지 않는다.** 전용 회귀 22건에 예외 케이스(토큰 900초 만료, 주말·장중·장전 거부, 정상 quota 미소비, 같은 날 두 번째 예외 claim 거부 `CODE_DEPLOYMENT_EXCEPTION_ALREADY_CONSUMED`)를 추가한다. 새 도구 sha를 보고.
2. **15분 예산 증명.** 토큰 issued→expires 900초 안에 claim → 정지 → 교체 → 기동 → handoff → 검증이 끝나는지 사본 리허설 3회의 단계별 타임라인으로 보인다. 753행 `authorize()`가 비가역 경계(마커 제거) 뒤 토큰 만료로 실패하는 경로가 있으면 설계를 고치거나(경계 뒤에는 만료를 재검사하지 않음) 왜 안전한지 쓴다.
3. **신선 ready 대안.** 2-A(1) 결과에 따라 `_state`가 순회 주기만큼 기다렸다 재시도하도록 하거나 장외 허용치를 둔다. 5초를 그냥 늘리지 말고, 그 값이 무엇을 보장하는지(현재 프로세스가 지금 쓰는 state인가)를 지키는 설계로. `receiver_state_snapshot.py`를 바꾸면 의존성 sha 변경.
4. **변경 기록.** `reports/2026-11-07/receiver-change-20261106-collector-main.md`(6항목, main.py·collector.py) 신규 + 전신 §2-A의 `receiver-change-20261104-tracker-and-settlement.md` 완성. 양식 `/Users/tr-oh/Documents/Codex/.claude/skills/arm-change-review/references/change-record-template.md`. 승인자는 비운다.
5. **2-A(3) 권고안 실행 준비.** (b)면 증명서 생성 dry-run 결과(사본 before/after sha, preflight 0)와 승인 문장 초안; (c)면 패치+테스트. 실 원장에는 쓰지 않는다.

### 2-C. 11-08(일)

1. 최종 소스 manifest·도구·의존성 sha 고정 → 전체 APP/WORK 격리 스위트 → 그 sha로 사본 리허설 3/3 PASS → `reports/2026-11-08/<ts>-exp-d-exception-rehearsal.json`. 이 뒤 소스·도구가 바뀌면 전부 다시.
2. 승인 JSON 초안(UNSIGNED, kind `exception-code-token`, 12파일, 새 package sha, `official_session` 비움) + 아래 토큰 문장 초안 + 11-09 점검표(2-D) 확정본.
3. journal 95088은 재시작하지 않는다(배포 중 `--journal-pid`로 보호). 전신 §2-D의 재시작 절차 문서만 남긴다. 2-A(3)에서 (b)를 택했고 사용자가 승인했다면 이 날 지정 시각에 기록하고 신선 사본으로 preflight 0을 다시 보인다.

### 2-D. 11-09(월) 당일

- 09:00~15:30 무접촉. 15:30 종료.
- 15:20 Toss 슬롯 쌍 확인: `research-readonly/toss-evidence-refresh-claims/20261109-1520.claim.json` + 그 run의 `s5-full-eligibility/<slot>/toss/0001-calendar-current.json`(가드 `_official_session` 직접 호출로 사전 검증, 읽기 전용).
- 15:40 이후 순서: (1) 당일 manifest로 전체 APP(≈2분)+WORK(≈3분) 스위트, 소스 무변경 확인 (2) 신선한 원장 사본 preflight → `unfilled_orders 0`·`pending_intents 0`, 아니면 **중단** (3) runtime·설정·paper sidecar·checkpoint·원장·배정 백업 경로·sha(원장 롤백 제외), 보호 PID 4개 재확인 (4) 실 런타임 `--check-only` PASS (5) 사용자: KRX 종료 확인 URL·문장 + 예외 토큰 문장(15분) → Codex: close proof JSON, 승인 JSON(0600, 900초) → `--deploy --close-proof … --code-approval … --journal-pid <journal> --protected-pid <toss> --protected-pid <watchdog> --protected-pid <candidates>` → `deployed_paused` → handoff → `state.json`의 pid·status·`paper_experiment.enabled`·active_arms(14)·halted 인용, 런타임 12파일 해시, claim status 보고.
- 1회 시도, 자동 재시도 없음. 실패 시 코드·체크포인트만 롤백. 수신기가 정지 상태로 남으면 **별도 재시작 승인**을 받아 11-10 08:30 전 복구한다. 11-10 15:40 정상 창은 예비로 남긴다.

토큰 문장 초안 (사용자가 당일 시각·sha를 채운다; 가드 필수어 예외·코드·배포, 도구 필수어 진단·호환 또는 checkpoint projection·SHADOW 생략·LIVE 직행 — 2-B(1)에서 가드 테스트로 재확인):
```
2026-11-09(월) 장 마감 후 예외 창에서 수신기(PID <당일 값>) 코드를 현재 소스 패키지 전체(EXP_D late_recovery_exit 5파일 + 2026-11-04 listener 추적 telemetry·전일 취소 정산 공개 5파일 + 2026-11-06 collector/main 수리 2파일, 승인 package sha <재계산값>)로 교체하는 예외 코드 배포를 승인한다. 이 승인은 소스 sha·대상 PID·시각 <HH:MM~HH:MM>을 묶은 일회성 예외 토큰이며 정상 quota를 소비하지 않는다.
paper_experiment_v1 14개 arm·설정·한도 변경 없음. 변경 기록 arm-change-exp-d-late-exit.md, receiver-change-20261104-tracker-and-settlement.md, receiver-change-20261106-collector-main.md 기준.
체크포인트 투영은 진단 필드만 바꾸고 오프셋·나머지 값은 보존하는 호환 방식(checkpoint projection)에 동의한다. 정상 handoff는 검증된 절차로 이어서 수행한다.
SHADOW 생략, LIVE 직행 승인. 실패 시 자동 재시도는 없다. 승인 시각 <HH:MM> KST.
```

## 3. 금지

- 거래일 09:00~15:30 운영 접촉·브로커 API 호출. 승인 없는 배포·재시작·원장 쓰기(만료 증명서 포함)·설정 변경. 11-09 15:40 이후 도구·의존성 수정. 원장 롤백. LLM을 주문 경로에 넣는 변경. 승인·토큰 문장 대필. 설치된 watchdog 교체. 주말에 Toss 슬롯을 수동으로 돌려 당일 쌍을 만드는 식의 가드 우회. "오늘 주문 0건" 감시기 알림은 이 패키지에 끼워 넣지 않는다(§0-A 감시기 요청에서 따로).

## 4. 보고 형식 (과제마다)

```
과제: 2-X-n <이름>
결론: <한 줄>
산출물: <경로> sha256 <…>
검증: <테스트·리허설 결과 파일, 실행/통과/실패>
소스·도구 변경: <파일 목록 + 전후 sha> | 없음
운영 접촉: 없음 (state.json·원장·작업자·API 0회) | <읽기 전용 호출 목록>
남은 조건: <…>
```
