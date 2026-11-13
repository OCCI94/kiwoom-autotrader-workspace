# 검토: Codex 11-09 장초반 점검·11-10 배포 준비 보고 (2026-11-09 10:4x KST, 읽기 전용 대조)

Codex가 보낸 보고(09:02~09:11 스냅샷 기준, §1~§4)를 제가 오늘 09:12·09:15·09:5x에 읽기 전용으로 확인한 사실과 대조했습니다. 운영 프로세스·원장 원본·브로커 API는 건드리지 않았습니다.

## 1. 대조 결과

| Codex 주장 | 판정 | 근거 |
|---|---|---|
| 수신기 PID 26954, 11-05 09:44 기동 구 빌드, arm 14 | 일치 | `started_at` 1793839464 = 2026-11-05 09:44:24 KST. 런타임 패키지 mtime 11-04 13:32 |
| 체결 스트림 `ready=false`, gap 84→87, "안전 보류 작동 중" | 시각 한정 일치, **해석은 주의** | 09:12 ready=False였으나 **09:15 자가 복구**(ready=True, 오류 없음, gap 88). 06:46~09:01 `sqlite_busy` 저장 실패 12회(이 로그에서 오늘 처음). 오늘 주문 0건의 원인은 이 보류가 아니라 **EXP_D 보유 6종목이 4칸 배치를 독점하고 지연 청산 경로가 운영 코드에 없기 때문**(`ai-stock-점검-20261109-지연청산.md`). 스트림 보류로 읽히면 원인을 놓침 |
| 보유 6종목 `ownership_unsupported`, 002790·010660 `ownership_pending` | 부분 | 09:5x `symbol_diagnostics`: EXP_D 4종목은 `status=owned`, reason `d_morning_exit_overdue`; 002790·010660은 `unsupported/ownership_pending`. 09:02 스냅샷과 시각 차이일 수 있음 |
| Toss 09:05 실패 → 09:10:51 recovery success | 일치 | healthcheck `toss.latest_slot 20261109-0905-recovery … success` |
| 섹터 ETF 09:05 `SHARED_IPC_HALTED` | 미확인 | 해당 결과 JSON은 읽지 않음 |
| 정본 소스 지문 25bbeb09…, 변경 .py 13개 | 일치 | 소스 vs 런타임 .py 해시 재계산 13개(main, telegram/collector·listener, trading 10개 — `tracker_diagnostics.py`는 소스에만 있음). 지문 알고리즘은 재계산하지 않음 |
| 배포 도구 206901a0… (fresh-wait 360) | 일치 | `work/deploy_receiver_normal_handoff.py` sha256 206901a0f5be54997c5b…, mtime 11-08 14:34 |
| APP 241모듈 3,184건 통과, 실패 0 | 모듈 수 일치, 건수는 미대조 | 11-08 APP 격리 실행 3회: 14:36(unchanged True) · 14:39(**unchanged False**, 무효) · **14:42:54(unchanged True, Run 3)**. 3,184/0은 JSON 요약 키를 찾지 못해 대조 못 함 |
| WORK 1,004건, 오류 1건(leader_autotrading_formulas import) | 일치 | `143625-full-work-isolated-suite.log`: Ran 1004, FAILED (errors=1, skipped=6), `_FailedTest.test_quant_leader_shadow_compare` |
| 발급기가 Candidate-C 스테이징 해시(690f433f…)를 하드코딩 | 일치 | `revision-2/issuer/issue_exp_d_v2.py:40` `PACKAGE_HASHES`, `:186` `len(manifest)==189 and sha==…` 아니면 `CANDIDATE_HASH_MISMATCH`. 정본 소스(25bbeb09…)를 가리키려면 코드 수정이 필수 |
| 정본 소스 `verified_certificate`는 64자리 hex 형식만 검사 | 일치 | `expired_order_certificates.py:136-137` `re.fullmatch('[0-9a-f]{64}')` |
| **"11-04 거부 매수 2건(218972 아모레퍼시픽홀딩스 7주, 219563 화천기계 52주)"** | **표기 오류** | 218972·219563은 **주문번호**, 종목은 002790(아모레G·아모레퍼시픽그룹) 7주·010660(화천기계) 52주(11-06 보고 "218972 002790 7주, 219563 010660 52주", 저장소 복사본 `attempts` 11-04 15:22:30·15:24:47 `closing_auction`). §1의 002790·010660과 같은 두 건. 승인 JSON·증명에는 주문번호와 종목코드를 분리해 적어야 함(`capture_certificates`는 (일자 8자리, 주문번호) 쌍을 받음) |

## 2. 발급기 정렬 질문에 대한 의견

- 증명 행 자체에 들어가는 `approval_manifest_sha256`은 **승인 JSON의 해시**이고(`issue_exp_d_v2.py:533`, `:591`), 스테이징 패키지 해시(`candidate_package_sha256`)는 `approval_binding`·감사 기록·백업 메타(`:569`, `:639`)에 들어갑니다. 즉 "스테이징 해시가 찍히는" 곳은 승인 자료와 감사 기록이지 원장 행이 아닙니다. 기능 문제는 없고 **추적성 문제**입니다.
- 정렬하려면 `PACKAGE_HASHES`에 정본 항목을 추가하고 `CANDIDATES / candidate / "outputs/ai-stock"` 경로 규칙(`:177`)도 정본 트리를 가리키게 바꿔야 합니다. 작은 수정이지만 도구 수정이고, 2026-11-03의 교훈은 "창 안에서 도구를 고치지 않는다"입니다.
- **권고:** ① 오늘 15:40 이후 `--check-only`와 발급기 `--dry-run`(쓰기 없음)을 **수정 없이 먼저** 실행해 결과를 봅니다. ② 정렬은 dry-run이 깨끗하고 **오늘 안에** 집중 테스트·dry-run 재실행·승인 자료의 발급기 해시 갱신까지 끝낼 수 있을 때만 합니다. 내일 창에서는 손대지 않습니다. ③ 정렬하지 않을 경우 승인 JSON에 "candidate_package_sha256 690f433f…는 Candidate-C 스테이징이며, 발급기가 import하는 거래 모듈 4개는 정본 25bbeb09…와 바이트 동일"을 한 줄 명시해 추적성을 남깁니다.

## 3. 증명 발급 시점에 대한 의견

- **오늘 15:40 이후 발급(권고, 사용자 승인 필요).** 이유: 내일은 정상 시도 1회뿐이고 실패·미확정도 claim을 소모합니다. 오늘 발급하면 `unfilled_orders=0` 게이트를 내일 `--check-only`에서 미리 확인할 수 있고, 문제가 나오면 내일 수리 시간대에 고칠 여유가 생깁니다.
- 전제: 발급기 dry-run이 깨끗할 것, 원장 백업 경로·해시가 보고될 것, 그리고 운영 중인 11-04 빌드가 새 행을 무시한다는 점. 런타임 `expired_order_certificates.py`에는 `paper_expired_order_certificates` 테이블은 있지만 `approval_manifest_sha256` 처리가 없어 v2 행을 읽는 경로가 없습니다(런타임 grep 0건). Codex가 dry-run 보고에 이 호환 판단을 한 줄로 확인해 주면 됩니다.

## 4. 승인 자료에 고쳐야 할 것
- 종목·주문번호 표기: 002790(아모레G) 7주 / 주문 218972, 010660(화천기계) 52주 / 주문 219563.
- 스트림 상태 서술: "09:01~09:12 저장 실패 후 09:15 자가 복구"로 적고, 무주문 원인은 EXP_D 배치 독점·지연 청산 미배포로 적을 것.
- 배포 당일(11-10) 전체 격리 스위트는 다시 돌려 `reports/2026-11-10/`에 남길 것(계약). 11-08 Run 3은 리허설 증거.
