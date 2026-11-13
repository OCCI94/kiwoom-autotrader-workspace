# Codex 요청: EXP_D 지연 청산 — 2026-11-05(목) 15:40 정상 창 수신기 코드 배포 준비

작성 2026-11-05 11:00 KST (Claude). 변경 기록: `outputs/ai-stock-research/reports/2026-11-05/arm-change-exp-d-late-exit.md`.

## 0. 승인 범위 — 먼저 읽을 것

- 사용자가 2026-11-05 10:55 KST에 **"15:40 준비 진행"**으로 승인한 것은 **배포 준비**다. 코드 배포 **실행** 승인은 15:35 이후 사용자가 승인 JSON의 `user_approval.text`로 따로 준다. 수리 승인 ≠ 배포 승인 ≠ 재시작 승인.
- 지금은 장중(09:00~15:30)이다. 15:30까지 운영 수신기·런타임·설정·원장·LaunchAgent를 쓰지 말고, 브로커 API를 호출하지 마라. 읽기와 복사본 작업만 한다.
- 이 배포는 `paper_experiment_v1` 14개 arm 그대로, 수신기 코드 5개 파일만 바꾼다. 설정·한도·arm 변경 없음. 작업자(telegram-account, toss-readonly, stability-watchdog 등) 재시작 없음.
- 변경 기록 3항의 **SHADOW 생략·LIVE 직행**은 스킬 원칙과 다르므로, 사용자의 배포 승인 문장에 "SHADOW 생략"이 명시돼야 한다(아래 §3-2 양식). 없으면 배포하지 않는다.

## 1. 확인된 사실 (Claude, 10:50~11:00 읽기 전용)

| 항목 | 값 |
|---|---|
| 배포 창 | `deploy_window_check --at "2026-11-05 15:40" --kind normal` → `allowed=true`, window=after-close, `holidays_checked=false`(휴장일 파일 없음). 다음 창 2026-11-10(화) 08:30 이전 |
| claim 잔량 | `~/Library/Application Support/KiwoomAutotrader/code-deployment-claims/`: `20261103-normal.claim.json`(화 소비), `20261105-normal` 없음 → **오늘 정상 claim 1회 가능(주 2회 중 2번째)**. 예외 claim 9건(11-03 4, 11-04 5)은 `normal_quota_consumed=False` |
| 소스 manifest | APP 전체 격리 스위트 `app-tests-isolated-provenance-094728460683.json` sha256 `eff9dc74…a711c4`, 238모듈 3,165 PASS. **09:47:28 이후 APP 소스 변경 0건(11:00 재확인)** |
| 런타임 5파일 | 현재 해시 = 변경 전 백업 = 11-04 13:32 설치본: rules `8c42ad8d10d7`, monitor `4ef71a89b995`, store `032a561668c2`, decision_evidence `120f52629e8a`, reconciliation `73e937d0adb1` |
| 배포 후 기대 해시 | 소스: rules `3cf5aa12bcee`, monitor `167ee6e356ad`, store `fed83c66f4e7`, decision_evidence `2520e30fc042`, reconciliation `af0c5a69e4b6` |
| 변경 전 소스 백업 | `/Volumes/AIStockData/backups/exp-d-late-exit-source-20261105/093050279464/source-before.tar.gz` sha256 `b578616c7bf199eb48edfab1bec1d388c0cbac4e0fafda1e87966f04588c1cc9` |
| 배포 도구 | `work/deploy_receiver_immutable.py` sha256 `1c0af3cf3ab464d2…`, mtime **11-04 18:45**. 마지막 성공 배포(11-04 13:31 예외, 런타임 13:32 설치) **이후에 수정됨 → 현재 버전은 실배포 미경험. 리허설 없이는 배포하지 않는다**(11-03 밤 3회 실패 교훈) |
| 수신기 기준값 | `telegram/state.json`: pid **26954**, status running, `automation_status.paper_experiment.enabled=True`, `monitor.active_arms` 14개, profile `paper_experiment_v1`. **`paper_experiment.halted` 키는 없다** → `monitor.halted`(None)과 `paper_experiment.failure`를 대신 인용 |
| 기존 템플릿 | 11-04 18:43 `reports/2026-11-04/184317043044-nov05-receiver-only-deployment-day-report-template.json` — 오늘 배포일 보고 템플릿. 내용이 위 사실과 맞으면 그대로 채우고, 다르면 차이를 보고 |

## 2. 지금(장중)~15:30 할 일 — 전부 읽기·복사본·격리

**A. 배포 도구 리허설(필수).**
- 런타임(`~/Library/Application Support/KiwoomAutotrader/`)을 격리 HOME 아래로 **복사**(읽기만)하고, 그 복사본을 대상으로 `deploy_receiver_immutable.py --check-only`(기본값)를 돌려 11-04 18:45 변경분이 check-only 경로를 깨지 않는지 확인한다.
- `--deploy` 경로는 **복사본 안의 수신기 대역(stub)**으로만 리허설한다. 실제 PID 26954·launchctl·키체인·네트워크 접촉 금지. 11-03의 `control_action_invalid` 결함이 재발하지 않음을 증거로 남긴다.
- 결과를 `reports/2026-11-05/<ts>-exp-d-deploy-tool-rehearsal.json`에 저장: 도구 sha256, 복사본 경로, 실행 모드, 종료 코드, 오류 0건 여부.
- 리허설에서 도구 결함이 나오면 **창 안에서 도구를 고치지 말고** 15:30 전에 고쳐 다시 리허설한다. 15:30 전에 못 고치면 오늘 배포 중단을 보고한다.

**B. 소스·manifest 재확인.** 배포 직전 다시 하지만 지금 한 번: APP 소스 manifest 재계산 → `eff9dc74…`와 일치. 불일치면 전체 APP 격리 스위트를 당일 `reports/2026-11-05/`에 다시 돌린다(계약 20행). focused 테스트로 대체 금지.

**C. 승인 JSON 초안.** `runtime-code-deployment-approval-v1`, `kind: after-close-code-approval`, `scope: receiver_only`, `receiver_pid: 26954`(정수), `source_manifest_sha256: <B에서 재계산한 값>`, `official_session` 블록(오늘 달력 파일 경로·sha·claim 경로), `user_approval: {kind: explicit_code_deployment, reference: <8~512자>, text: <비워 둠 — §3-2에서 사용자 문장>}`. `issued_at`/`expires_at`는 **실행 직전** 기록(같은 날, 간격 ≤30분, 실행 시각이 그 사이). 초안 경로를 보고한다. 값은 비밀이 아니지만 키·토큰은 절대 넣지 않는다.

**D. 종가 증명 양식.** `close_proof()` 요건: 실행 시각 ≥15:35 KST, `checked_at` tz 포함·실행 15분 이내, `trade_date: 2026-11-05`, `regular_session_closed: true`, `session: "closed"`, `source_url` https `krx.co.kr` 도메인 경로 포함, `evidence` 10자 이상. **사람이 확인한 기록**이어야 하므로 사용자가 15:35 이후 KRX 페이지를 보고 확인한 뒤 Codex가 JSON을 쓴다. 양식만 준비한다.

**E. 백업 계획.** 15:35 이후 실행할 명령과 대상 경로를 미리 적는다: runtime 트리·설정(`config/`)·원장(`runtime/logs/*.db` 복사, `mode=ro`) → `/Volumes/AIStockData/backups/exp-d-late-exit-deploy-20261105/<ts>/` + `manifest.json` + sha256. **원장은 복사만 하고 롤백 대상에서 제외**(계약 21행).

**F. 미체결 게이트 계획.** `preclaim_pending_order_gate`가 보는 통합 미체결을 15:30 이후 원장 복사본에서 먼저 센다. EXP_D 보유 6종목(지투파워·카카오게임즈·상지건설·셀트리온·아이씨티케이·SK바이오팜) 포함 미체결 0이어야 claim을 소비한다. 0이 아니면 배포하지 않고 보고한다.

**G. 보호 PID.** `--journal-pid`와 collector PID는 11-04 13:31 배포 때와 같은 방식으로 `launchctl print`(읽기)에서 식별하고, 어느 label의 PID인지 보고에 적는다. 현재 telegram 26954, telegram-account 5634, toss-readonly 62837, stability-watchdog 34288.

## 3. 15:35~15:50 순서 (사용자 참여 구간)

1. **종가 확인(사용자).** 사용자가 KRX에서 정규장 종료를 확인하면 Codex가 §2-D 양식으로 close proof JSON을 쓰고 경로·sha256을 보고한다.
2. **배포 승인(사용자).** 사용자가 아래 뜻의 문장을 주면 Codex가 §2-C 초안의 `user_approval.text`에 넣고 `issued_at`/`expires_at`를 기록한다. 문장에 들어가야 할 것:
   - 2026-11-05 15:40 이후 정상 창, 수신기 PID 26954, `paper_experiment_v1` 14개 arm 유지, 수신기 코드 5개 파일만 교체
   - 소스 manifest `eff9dc74…`(재계산 값), 변경 기록 `arm-change-exp-d-late-exit.md` 기준
   - **SHADOW 생략, LIVE 직행 승인**
   - 정상 claim 1회 소비 동의, 실패 시 자동 재시도 없음
3. **배포 직전 재검증(Codex).** 소스 무변경·manifest 일치 재확인 → §2-E 백업 실행·해시 보고 → §2-F 미체결 0 확인 → §2-A 리허설 결과 파일 존재 확인. 하나라도 빠지면 중단.
4. **배포 실행(Codex).** `deploy_receiver_immutable.py --deploy --close-proof <§3-1> --code-approval <§3-2> --journal-pid <§2-G> …`. claim 파일 `20261105-normal.claim.json`이 생긴다. **실패·미확정이면 재시도하지 않는다**(claim 소비됨, 다음은 예외 토큰 또는 11-10 화요일).
5. **배포 후 보고(Codex).** 실제 `state.json`에서 pid(새 값)·status·`paper_experiment.enabled`·`active_arms`(14개 이름)·halted(키 부재면 `monitor.halted`·`failure` 인용)를 그대로 인용. 런타임 5파일 해시가 §1 "배포 후 기대 해시"와 같은지 표로. claim 파일 status, 백업 경로·해시, close proof·approval sha256, 리허설 보고 경로.
6. **실패 시 롤백(Codex, 사용자 보고 후).** 백업 `b578616c…`에서 5개 파일만 복원(복원 해시 = §1 런타임 5파일 현재 해시). 원장은 건드리지 않는다. 롤백도 정상 창·claim이 필요하므로 15:50 전에 끝내지 못하면 사용자에게 예외 토큰 여부를 묻는다.

## 4. 보고 형식 (한 번에)

```
변경: EXP_D late_recovery_exit (결함 수정(경제적 부수효과 있음), 대상 EXP_D)
기록: reports/2026-11-05/arm-change-exp-d-late-exit.md (6항목 완료, §6에 배포 승인·시각 추가)
정합성: 소스 manifest <sha> = 스위트 manifest eff9dc74… | 런타임 5파일 = 소스 해시 (표)
창: deploy_window_check 2026-11-05 15:4x kind=normal → allowed=true, holidays_checked=false
claim: 20261105-normal.claim.json <status> (주 2/2 소비)
리허설: <경로> (도구 sha 1c0af3cf…, 오류 0)
백업: <경로> sha256 <…> (runtime/설정/원장 복사, 원장 롤백 제외)
state.json: pid <n> / status <…> / paper_experiment.enabled <…> / active_arms 14 [<이름>] / halted <키 부재 → monitor.halted …, failure …>
남은 사람 몫: 첫 거래일(11-06 금) 마감 후 변경 기록 2항 지표 집계(ledger-reconciliation)
```

## 5. 금지

- 15:30 전 운영 접촉, 실제 PID에 대한 `--deploy`, 자동 재시도, 원장 롤백, 설정·arm·한도 변경, 작업자 재시작, LLM을 주문 경로에 넣는 변경, 창 안에서 배포 도구 수정.
- 승인 문장을 Codex가 대신 쓰지 않는다. 사용자 문장이 없으면 `--deploy`를 실행하지 않는다.

## 6. 야간 진행 (사용자 결정 2026-11-05 16:2x "오늘 리허설 후 진행")

지금은 장 마감 후다. §2의 읽기·복사본 작업을 **지금 바로** 하고, 리허설이 PASS이고 §6-2 0번 게이트가 통과될 때만 §3으로 간다. 리허설 FAIL이면 창 안에서 도구를 고치지 말고 중단을 보고한다(다음 창 11-10 화 08:30 전). 아래 값은 16:27~16:45 사이에 도구·가드 코드와 실제 프로세스를 읽어 확정했다. §2-G·§3의 옛 PID 값과 다르면 **§6이 우선**한다.

### 6-1. 확정 사실
| 항목 | 값 |
|---|---|
| 창 | 가드 `deployment_window`: 목요일 15:40 이후 ~ **23:59:59까지** 정상 창(자정 넘기면 불허). `deploy_window_check` 16:20 → allowed=true, holidays_checked=false |
| 도구 | `work/deploy_receiver_immutable.py` sha `1c0af3cf3ab464d2…`, mtime 11-04 18:45:35, 실배포 미경험 → 리허설 필수. 인자: `--source`(기본 outputs/ai-stock) / `--check-only` 또는 `--deploy` / `--close-proof` / `--code-approval` / `--journal-pid`(필수) / `--protected-pid`(반복). 종료 0은 `ready_for_explicit_after_close_deploy`(check-only) 또는 `deployed_paused`(deploy)뿐 |
| 보호 PID | `--journal-pid 95088` = `work/paper_journal_server.py --port 58468`(11-04 16:17:35 시작, launchd 아님). **이 프로세스는 고아다**(바이너리 `codex-primary-runtime.previous/…/python3.12`, 11-04 16:36 런타임 교체 전 기동). 도구의 `process_identity`는 `/bin/ps -o pid,ppid,lstart,comm`만 보므로 검사는 통과하지만, **배포 중 절대 재시작하지 않는다**(재시작하면 `PROTECTED_PROCESS_CHANGED`로 실패하고 claim 소비). 재시작은 배포 완료 후 별도 승인 항목. `--protected-pid 49165`(toss-readonly) `--protected-pid 49149`(stability-watchdog) `--protected-pid 49184`(telegram-candidates): 셋 다 16:18 재시작본, 현행 런타임 바이너리. collector 5634는 도구가 자동 포함. **수신기 26954는 보호 목록이 아니다**(도구가 정지·재기동하는 대상). §2-G의 62837·34288은 죽은 PID다 |
| 소스 | `kiwoom_autotrader/` 09:47:28 이후 변경 0건(16:27 재확인). manifest `eff9dc74…` 그대로. 도구 의존 4파일(`receiver_checkpoint_rollback.py`, `deploy_handoff_runtime.py`, `utils/runtime_deployment_guard.py`, `utils/control_audit.py`) 해시는 `deployment_tool_dependencies()` 출력으로 보고 |
| 런타임 5파일 | rules `8c42ad8d10d7`(11-04 13:32), monitor `4ef71a89b995` 등 11-04 설치본 그대로. `telegram/listener.py` 런타임 sha는 승인 JSON `old_listener_sha256`에 필요 → 재계산 보고 |
| claim | `code-deployment-claims/20261105-normal.claim.json` 없음 → 정상 claim 1회 가능. 실패·미확정도 소비 |
| official_session | 가드 `_official_session()`을 16:32에 직접 실행: `20261105-1505(-recovery)/1520.claim.json` × `150509701443/151005762723/152009017794` calendar 9쌍 전부 PASS(wire `898b400a…` 동일, session 09:00~15:30, previous_day 20261104). Codex 16:12 증명서의 쌍(1505 claim + 1520 calendar)은 `claimed_at`↔`observed_at` 차 899.5초(한도 900초)로 아슬아슬하게 통과 → **같은 슬롯 쌍을 쓴다**: `claim_path …/toss-evidence-refresh-claims/20261105-1520.claim.json`(sha `137c24ec1da2532c…`) + `calendar_path /Volumes/AIStockData/ai-stock-research-data/s5-full-eligibility/20261105-152009017794/toss/0001-calendar-current.json`(sha `cfd83de4516dc4b6…`), `calendar_source_sha256 898b400abd5e8c7e…`. Codex가 전체 sha를 다시 계산해 넣는다 |
| close proof | official_session과 **별개**이며 `--deploy`에 필수(`OFFICIAL_CLOSE_PROOF_REQUIRED`). `close_proof()`: 실행 시각 ≥15:35, `checked_at` tz 포함·**실행 15분 이내**·오늘 날짜, `trade_date: 2026-11-05`, `regular_session_closed: true`, `session: "closed"`, `source_url` https `krx.co.kr`(또는 `*.krx.co.kr`) + 경로, `evidence` 10자 이상. 사람이 KRX 페이지를 본 기록이어야 한다 |
| 배포 후 상태 | 도구는 **`deployed_paused`에서 끝난다**: `maintenance-quiet.lock` 유지, `maintenance_paused=True`, 정상 handoff 없음(도구 docstring "marker is deliberately retained after success"). 11-04 13:31은 그날 전용 `deploy_receiver_reconciliation_maintenance_v9.py`가 handoff(`marker_removed`, `normal_handoff_completed`)까지 했다. 오늘 쓸 범용 handoff 도구는 `work/`에 없다(`telegram_cutover_cli.py`는 10-28 프로토타입, 무관) |

### 6-2. 순서
0. **handoff 게이트(claim 전).** Codex 템플릿(`184317043044-nov05-…-template.json`) `required_preclaim.normal_handoff_procedure_verified`와 `gating_rule`대로, `deployed_paused` 뒤 수신기를 정상 운전으로 넘기는 절차(도구 경로·sha·검증 결과)를 **먼저** 확정해 보고한다. 수리 시간대에 런타임 복사본으로 dry-run 해 PASS여야 한다. 검증된 절차가 없으면 **claim 전에 중단**한다. 멈춘 수신기를 내일 아침까지 두는 배포는 하지 않는다.
1. §2-A 리허설(런타임 복사본 `--check-only` + stub `--deploy`) → `reports/2026-11-05/<ts>-exp-d-deploy-tool-rehearsal.json`. PASS/FAIL을 한 줄로 먼저 보고.
2. §2-B manifest 재계산, §2-E 백업 실행(runtime·config·원장 복사, 원장 롤백 제외) + 해시, §2-F 미체결 0 확인(원장 복사본), 보호 PID·journal PID 생존 확인, `old_listener_sha256`·의존성 해시 계산.
3. **실 런타임 `--check-only`**(읽기 전용, 11-04 13:27 `v9-approved-final-check-only`와 같은 역할):
   ```
   python3 work/deploy_receiver_immutable.py --check-only --journal-pid 95088 --protected-pid 49165 --protected-pid 49149 --protected-pid 49184
   ```
   `status == ready_for_explicit_after_close_deploy`, `services.protected` 5개(5634·49149·49165·49184·95088) 생존, `resource_preservation_exceptions`가 §1 기대 5파일과 일치해야 한다. 또한 `ledger_validation`/`db_summary`의 `unfilled_orders` 게이트(만료·취소된 TTL 매수를 미체결로 세는 문제, 11-04 19:35 메모; 11-04 원장 복사본에서 11건)가 0으로 통과하는지 이 결과로 확인한다. 막히면 **claim 전 중단**하고 보고한다(창 안에서 도구를 고치지 않는다).
4. 사용자에게 "리허설 PASS, handoff 절차 확정, check-only PASS, 배포 가능"을 보고하고 **멈춘다.** 사용자가 (a) KRX 페이지 URL과 "정규장 종료 확인" 문장, (b) §6-3 승인 문장을 준다.
5. Codex가 close proof JSON(`checked_at` = 사용자 확인 시각)과 승인 JSON(§6-4, `issued_at` = 지금, `expires_at` ≤ +30분, **파일 모드 0600·절대경로·심링크 없음**)을 쓰고 경로·sha256을 보고한다.
6. **close proof 15분·승인 30분 안에** 실행. 실패·미확정이면 재시도 금지(claim 소비):
   ```
   python3 work/deploy_receiver_immutable.py --deploy --close-proof <krx-close-proof.json> --code-approval <approval.json> --journal-pid 95088 --protected-pid 49165 --protected-pid 49149 --protected-pid 49184
   ```
7. `deployed_paused` 확인 → 0번에서 확정한 handoff 절차 실행 → `state.json`: `pid`(새 값), `status running`, `maintenance_paused False`, 마커 부재, `automation_status.paper_experiment.enabled True`, `monitor.active_arms` 14개, `halted` 키 유무 그대로 인용. 런타임 5파일 = §1 기대 해시. claim status.
8. §4 형식 보고. 실패 시 §3-6 롤백(코드만)은 사용자 보고 후, 23:59 전. 배포 후 journal 95088 재시작은 별도 승인으로 올린다.

### 6-3. 사용자 승인 문장 (리허설·handoff 게이트·check-only PASS 뒤에만, 사용자가 직접 준다)
가드는 본문에 '코드'와 '배포'가, 도구 결합 검사는 '진단'과 '호환'(또는 "checkpoint projection")이 있어야 통과한다. 아래 문장은 둘 다 만족한다.
```
2026-11-05(목) 장 마감 후 정상 창에서 수신기(PID 26954) 코드 5개 파일만 교체하는 EXP_D late_recovery_exit 코드 배포를 승인한다.
paper_experiment_v1 14개 arm·설정·한도 변경 없음. 소스 manifest eff9dc74…(재계산 값과 일치 확인), 변경 기록 arm-change-exp-d-late-exit.md 기준.
체크포인트 투영은 진단 필드만 바꾸고 오프셋·나머지 값은 보존하는 호환 방식(checkpoint projection)에 동의한다. 정상 handoff는 확정된 절차로 이어서 수행한다.
SHADOW 생략, LIVE 직행 승인. 정상 claim 1회 소비에 동의하고, 실패 시 자동 재시도는 없다. 승인 시각 <HH:MM> KST.
```

### 6-4. 승인 JSON 계약 (가드 `assert_code_deployment` + 도구 `verify_deployment_tool_binding` 기준, 16:40 코드 확인)
11-03 `work/urgent-ttl-settlement-20261103/212828279980-explicit-v2-single-exception-code-approval.json`이 구조 예시다(단 kind·receiver_checkpoint_rollback 블록은 다르다).
- `schema runtime-code-deployment-approval-v1`, `action code_deploy`, `kind after-close-code-approval`, `scope receiver_only`, `mode paper`, `receiver_pid 26954`(state.json `pid`와 일치), `source_manifest_sha256` = `manifest_sha256(package_manifest(outputs/ai-stock/kiwoom_autotrader))` 재계산값, `change_class maintenance`(throughput으로 쓰면 10세션 안정성 증명을 요구한다), `issued_at`/`expires_at` 같은 날짜·`issued ≤ 실행시각 < expires`·차이 ≤1800초.
- `official_session`: `claim_path`, `calendar_path`, `claim_sha256`, `calendar_file_sha256`, `calendar_source_sha256` (§6-1 같은 슬롯 쌍).
- `user_approval`: `kind explicit_code_deployment`, `reference` 8~512자, `text` 8~4096자 = 사용자 문장 그대로.
- `receiver_checkpoint_rollback` (정확히 이 10개 키만): `schema receiver-checkpoint-rollback-policy-v1`, `deployment_tool_sha256` = 도구 파일 sha(`1c0af3cf…`), `tool_dependencies_sha256` = `deployment_tool_dependencies()` 출력(4파일 dict), `diagnostic_fields` = `["recorder_snapshot_observed_at","state_publication","telemetry_publish_error","tracker_diagnostics"]`, `checkpoint_projection "exact_diagnostics_only_preserve_offsets_and_all_other_values"`, `checkpoint_projection_explicit true`, `phase "before_normal_handoff"`, `execution_mode "paused_stage_only_no_normal_handoff"`, `old_listener_sha256` = 현재 런타임 `telegram/listener.py` sha, `installed_watchdog_change false`.
- 파일: 절대경로, 경로상 심링크 없음, 일반 파일, 모드 0600, 소유자 본인, 16KB 이하.

## 7. Codex 17:09 gate-0 중단 보고 검토 (17:17) — 오늘 밤 배포 없음

Codex 보고(`reports/2026-11-05/170912385089-exp-d-deployment-preparation-gate0-hold.txt`)의 중단 판단은 타당하다. claim 미소비, 운영 무변경, state.json·보호 PID 5개 값은 내 16:3x 관측과 같다. 차단 사유는 셋이다(둘은 Codex 보고, 하나는 내가 추가 확인).

### 7-1. 검증 결과와 이 문서의 정정
| 항목 | 확인 (17:10~17:17, 전부 읽기 전용) |
|---|---|
| 실제 변경 10파일 | source(191파일)↔runtime(188파일) 직접 대조: .py 수정 9·추가 1, 비py 추가 2(`config/data/theme-sector-supplement.json` 11-01, `realtime/README.md` 10-26). EXP_D 5파일 외 `telegram/listener.py`(11-04 17:33), `trading/closed_ledger_calendar.py`(17:29), `trading/tracker_diagnostics.py`(신규 17:34), `trading/cancellation_settlement.py`(18:26), `trading/execution_log.py`(18:28) = 11-04 13:31 배포 뒤 Codex의 listener tracker telemetry(`173453084699-…-source-ready`)와 전일 취소 정산 공개 수리(`185655763410-…-amendments-final`). 11-04 18:34 APP 3,127 PASS·11-05 09:47 3,165 PASS에 포함됐으나 **arm 변경 기록은 EXP_D 5파일만 다룬다.** §1의 "5파일" 전제는 내 오류다(변경분만 보고 source↔runtime 전체 대조를 안 했다) |
| 승인 package sha | `manifest_sha256(package_manifest(kiwoom_autotrader))` 재계산 = `16c3e964b0ddd2ec…` (189 entries). `eff9dc74…`는 스위트 manifest 증거 JSON의 sha. §1·§2-B·§3·§6-4의 "manifest eff9dc74"는 승인 JSON 값이 아니다 |
| 백업 tar sha | `source-before.tar.gz` = `db8ff468af48159a…`. §1의 `b578616c…`는 같은 폴더 `manifest.json`의 sha |
| check-only 범위 | 도구 683행 `if not deploy: return` → preclaim 미체결 게이트(763행)는 deploy 경로에서 claim(775행) **직전**에만 돈다. 게이트 실패는 claim을 태우지 않지만 check-only로는 증명할 수 없다. §6-2 3번의 해당 문장 철회 |
| handoff | 미검증. 동일 결론(§6-1 마지막 행) |

### 7-2. 추가 차단 사유: 미체결 게이트 11건
`deploy_handoff_runtime.db_summary(pending=True)`를 Codex 16:23 원장 사본(`work/approved-postclose-20261105/162306001169/snapshots/ledger.sqlite3`)에 실행 → `unfilled_orders 11`, `pending_intents 0`. 11건 전부 EXP_D 종가 단일가 매수, 체결 0:
- 11-03 9건 `219443 219461 219473 219494 219709 219731 219860 219872 219880` — `buy_ttl_cancellation_outcomes` status `cancel_input_ack_unconfirmed_settlement`, 정산(`…_settlements`) 행 없음. 11-03 예외 승인의 `original_orders`와 같은 9건.
- 11-04 2건 `218972 219563` — status `cancel_broker_rejected`, 정산 행 없음(같은 날 `220013`은 37주 체결 정산이 있어 게이트 통과).
`orders`에 상태 열이 없고 게이트는 `quantity != SUM(fills.quantity)`만 보므로 취소·만료 주문이 영원히 미체결로 남는다(11-04 19:35 메모의 11건과 동일). 이 상태면 11-10에도 `PRECLAIM_UNRESOLVED_ORDER_REQUIRES_RECONCILIATION`으로 claim 전 중단된다. 역설: 전일 취소 정산을 공개하는 수리(`cancellation_settlement.py`·`execution_log.py`)가 바로 미배포 5파일에 들어 있다.

### 7-3. 11-10(화) 창을 위한 수리 기간 과제 (사용자 결정 → Codex 요청서 재작성)
1. **정상 handoff 범용 절차**: v9 `atomic_handoff`를 떼어내 `deployed_paused` 뒤 마커 제거·정상 운전 전환·검증을 하는 절차를 만들고 런타임 복사본에서 dry-run PASS. 승인 JSON `receiver_checkpoint_rollback`(phase `before_normal_handoff`)과의 순서를 명시.
2. **배포 범위 — 사용자 결정 17:2x "10파일 전체로" → (A) 확정.** 원안: (A) 10파일 전체 — 추가 5파일의 변경 기록(목적·테스트·롤백·위험) 별도 작성 후 같은 승인 문장에 포함; (B) 5파일 후보 — 현재 런타임 + EXP_D 5파일로 후보 트리를 만들고 그 manifest로 전체 APP 스위트 재실행. Codex도 두 안을 제시했다.
3. **미체결 11건 해소**: (a) 원장 대조로 11건의 취소·만료 확정 증거(`paper_expired_order_certificates` 27행, 브로커 관측)를 확인하고 정산 행을 쓰는 것 — 금융 행 쓰기이므로 별도 승인과 ledger-reconciliation 절차; 또는 (b) 게이트가 확정된 취소 결과(outcomes·certificates)를 제외하도록 `deploy_handoff_runtime.py` 수리 — 의존성 sha가 바뀌므로 승인 JSON 재결합, 창 밖에서만, 테스트·리허설 포함.
4. **journal 95088 재시작**(고아, `work/paper_journal_server.py`): 배포와 분리된 별도 승인. launchd가 아니므로 Codex가 종료·재기동.
5. **배포 당일**: 최종 manifest로 전체 APP 스위트, 신선한 원장 사본 게이트 0, runtime·설정·원장 백업, 복사본 리허설, 실 check-only, KRX 종료 증명, 승인 문장(§6-3 형식, package sha `16c3e964…` 또는 재계산값).
