# 장애 서명 표 (이 프로젝트에서 관측된 것 우선)

| 서명 (어디서) | 뜻 | 원인·대응 | 관측 |
|---|---|---|---|
| `execution_recording.ready=false`, `error` 비어 있지 않음 (state.json) | 00 체결 스트림이 기록 불능 | 체결 기록 오류 뒤 스트림이 ready=false로 남음. 자가 복구가 없으면 승인 후 재시작. 장중이면 보유 포지션의 청산 경로를 먼저 확인 | 2026-11-03 07:53 |
| `ttl.status=settlement_unconfirmed`, `reason=cancellation_rest_original_identity_conflict` (state.json) | TTL 취소는 증권사가 받았지만 정산 확인 실패, 예약금 미해제 | kt00009가 원주문에 `mdfy_cncl_tp='일반'`을 돌려주는데 코드는 빈 값만 허용. 소스 수정(cancellation_settlement.py)으로 해결. 30초마다 재시도, 대상 주문이 돌아감 | 2026-11-03 15:24~ |
| `cancel_input_ack_unconfirmed_settlement` (ttl outcomes) | 취소 ACK만 받고 정산 미확인 | 정상 중간 상태. ACK만으로 예약금을 풀지 않는다 | |
| `cancel_broker_rejected` (ttl outcomes) | 취소가 거부됨(이미 체결 등) | 체결 확인 후 보유로 전환 | 2026-11-03 15:30:12 (326030) |
| `request_expired` (공용 시세 수집 HALT) | 시세 수집 요청 예산 만료 | 16:00 수집이 2/44 종목에서 중단. 야간 후보 0건의 원인 | 2026-11-03 16:00 |
| `HTTP 429` (Toss 갱신) | 외부 API 호출 제한 | 슬롯 간격 유지, 재시도 금지 | 2026-11-03 09:20 |
| `REFRESH_REQUEST_BUDGET_EXPIRED` (Toss 슬롯 결과) | 슬롯 예산 소진 | 다음 슬롯까지 대기 | 2026-11-03 15:20 |
| 수집기 PID 변경, `collector-status.json` 재시작 횟수 증가 | 개인계정 수집기 타임아웃 재시작 | KeepAlive가 재시작. 하루 4회 이상이면 네트워크·세션 확인 | 2026-11-03 4회 |
| `control_action_invalid`, `control_audit_directory_not_private`, `REPAIR_FINANCIAL_OR_ASSIGNMENT_CHANGED` (배포 결과 JSON) | 배포 도구 자체 결함으로 중단 | 코드 문제가 아님. 세 번째는 수신기를 정지시킨 뒤 중단해 수동 재시작 필요(19:38→19:42) | 2026-11-03 저녁 |
| `cancellation_query_completion_unverified` (V2 배포 결과) | 배포 도구의 "조회 증거 90초 이내" 조건 만료(ka10075 108초, kt00009 97초) | 코드는 교체·기동·ready까지 갔다가 되돌아감. 도구가 옛 코드를 복원만 하고 수신기를 다시 띄우지 않아 정지 상태로 남음 → 사람이 재시작 승인 필요 | 2026-11-03 21:32 |
| `REPAIR_LIVE_HEALTH_UNVERIFIED`, `failure_phase=paused_start` (V3 배포 결과) | 새 PID가 첫 state.json 게시 전에 관측돼 health 검사가 먼저 실패. 별도로 마커 lease가 180.000005초(두 번 읽은 시계 차)라 앱 상한 180초 초과 | 코드 교체·기동까지 갔다가 옛 코드 복원, 수신기 정지, 마커 잔존. 옛 runtime은 `maintenance-quiet.lock`을 읽지 않으므로 옛 코드 단순 재시작은 마커를 무시하고 정상 모드로 뜬다 | 2026-11-03 22:53 |
| `cancel_broker_rejected` 뒤 `ttl.status=settlement_unconfirmed`, `reason=cancellation_ack_unconfirmed`, 30초 재시도 (state.json, listener.log `ttl_cancellation_settlement_retry`) | EXP_D·VSS·LDR의 종가 단일가 주문은 `verified_auction_window` 정책으로 15:30:00 만료 처리되고 취소가 15:30 이후에 나가 증권사가 거부. 취소 API 응답 코드·메시지는 원장에 저장되지 않아 거부 사유 미확인 | 미체결 원주문이 kt00009·ka10075에 `접수`로 남고 예약금 미해제, 재시도 루프가 ka10075를 30초마다 호출. 다음 거래일 prior-day recovery(kt00007 → 만료 증명)로 풀리는지 확인 필요. 취소 거부가 체결 실패의 증거는 아님(220013은 거부됐지만 37주 체결) | 2026-11-04 15:30:17~19 (218972, 219563, 220013) |
| `V5_OWNED_SAVED_ASSIGNMENT_MISSING`(paused_start), `V6_EXPIRY_COMPLETION_CHANGED`(prior_expiry_resolution), `V7_HARD_DEADLINE`(normal_handoff, `handoff_operational_review_required`), V8 `REPAIR_LIVE_HEALTH_UNVERIFIED`(claim_created) (배포 결과 JSON) | 장중 예외 배포 도구 결함 연쇄. V7은 cutover 뒤 내부 maintenance pause가 남아 수신기가 떠 있어도 주문 0건(11:43~13:32) | V5·V6은 옛 코드 복원·수신기 정지, V7은 롤백 없이 운영 검토 필요, V8은 claim만 소비. V9(13:32)로 성공. 하루에 예외 claim 5개 소비 | 2026-11-04 10:15~13:35 |
| `sqlite3.OperationalError: database is locked` (Codex 읽기 전용 조회) | 원장이 `journal_mode=delete`라 수신기 쓰기 트랜잭션 중에는 읽기 시작도 막힘. 반대로 긴 읽기가 수신기 커밋을 막을 수 있음 | 라이브 원장은 열지 말고 복사본을 쓴다(절대 규칙 4). 수신기 쪽 오류는 관측되지 않음(gap 0) | 2026-11-04 15:36 |
| 장외 CPU 75~118%·메모리 3 GB 이상, `monitor_tick_over_120_seconds`·`receiver_observation_stale_or_uninstrumented` 반복, TTL 30초 재시도가 2.5~5분 주기로만 돎 (top, watchdog notification, state.json `tick_health`) | 종료일 재대사 루프가 과거 거래일 전부(10/26~11/03)를 5~20분마다 다시 조회하고 날짜를 전환할 때마다 그 날 관측 전체(최대 133 MB)를 다시 파싱·해시하고 7일치 snapshot을 메모리에 상주 | 재대사 달력 미검증(`official_calendar_unavailable`)이면 `execution_log.py _closed_due`의 "7세션 뒤 중단"이 불발되고, 비용 관측 행 2만 개 초과 일자(10/27)는 `_closed_observation_facts`가 None을 돌려 영구 미해결. 기동 직후 첫 순회는 모든 날의 execution-history.jsonl을 통째로 다시 씀. monitor와 같은 listener 스레드라 루프가 2~5분 막히고, 장중(09:00 청산, 15:20~15:30 단일가)이면 tick 시작이 늦어짐. 지표 구분: `maximum_tick_duration_seconds`는 monitor 내부, 감시기 `tick_age`는 state.json 경과(루프 1회 소요). 재시작으로는 안 풀림(첫 순회가 더 무거움). Codex에 소스 수정 검토 요청, 정규 배포 창에서만 | 2026-11-04 16:00~17:13 (state.json 미갱신 최대 286초; 98.3초 tick은 당일 종가 주문 흐름이지 재대사가 아님) |
| `PRECLAIM_UNRESOLVED_ORDER_REQUIRES_RECONCILIATION`, `UNRESOLVED_ORDER_REQUIRES_RECONCILIATION`, `POSTSTART_UNRESOLVED_ORDER` (generic 배포 도구 `work/deploy_receiver_immutable.py` 결과 JSON) | 도구의 미체결 집계(`deploy_handoff_runtime.db_summary` `unfilled_orders`)가 "수량≠체결 합"인 주문을 전부 세어, 만료 증명이 있는 EXP_D 종가 주문이나 취소된 TTL 주문도 미해결로 봄. 원장에 그런 주문이 하나라도 있으면 영구 거부 | 코드 문제가 아니라 도구 결함. 첫 번째는 claim·정지 전에 멈추므로 수신기 영향 없음, 두 번째는 정지 뒤라 롤백·정지 상태로 남음. 도구가 tracker의 열린 수량(kt00007 종결 증거·만료 증명·공개된 취소) 기준으로 고쳐졌는지 확인한 뒤에만 배포. generic 성공 상태 `deployed_paused`는 거래 재개가 아니다(마커·paused 유지, handoff 별도) | 미관측. 2026-11-04 19:35 소스·원장 복사본 분석(16:16 복사본 unfilled 11건) |
| `d_morning_exit_overdue` (state.json `monitor.symbol_diagnostics[*].reason`, EXP_D 보유 종목; monitor `_notify` 알림) | 전일 매수한 EXP_D 종목이 09:00분 청산 창을 놓침. `paper_experiment_rules.py` 446~449는 HH:MM이 정확히 `09:00`일 때만 매도 신호를 내고, 09:01 이후는 `paper_experiment_monitor.py` 938~939·1070~1071의 진단·알림뿐 매도 신호가 아님. EXP_D는 15:15 강제 청산(rules 450) 전에 return하므로 늦은 자동 매도 없음 | 09:00분에 monitor tick 평가가 없으면 발생. 08:59:15 시작 tick이 08:59 선정 작업(monitor 1046~1047)을 포함해 109초 돌고 다음 tick이 09:01:25였던 사례. 다음 자동 시도는 다음 거래일 09:00분(조건 유지 시). 장중 조치 없음. 처리(보유 유지/수동 매도/결함 수정)는 사용자 결정 | 2026-11-04 SK바이오팜 1종목(Codex 154911 보고), 2026-11-05 09:11 6종목(reports/장중 점검 2026-11-05 0905.md) |
| `ttl_stream_unverified` (state.json `paper_experiment.ttl.reason`, `sell_auction_cancellation.reason`; `TTLUnavailable`) | `unfilled_buy_ttl.py` 387~394 `_stream`이 recorder status의 `ingress_sequence == engine_ingress_sequence`를 요구하는데 불일치. `execution_stream.py` 289~306 engine 값은 현재 구독 핸들 delivery 기준이라 재구독(stream_gap→resumed) 뒤 0부터, 수신기 누적 카운터(438행)는 유지 → 새 00 이벤트가 누적값만큼 쌓이기 전에는 고정(기전은 코드·관측값 추정). 같은 검사가 `cancellation_settlement.py` 342~345, `paper_reconciliation.py` 195~200, `reentry_cooldown.py` 38 | 전일 미체결 TTL 정산·예약금 해제, 매도 단일가 취소, 취소 정산 공개가 30초 재시도만 반복. 신규 매수의 TTL 취소도 막힐 가능성(확인 필요). 장중 조치 없음. 코드 변경 없는 재시작으로 복구되는지 Codex 확인 뒤 15:40 이후 별도 승인 | 2026-11-05 09:0x~ (ingress 22 ≠ engine 0, 스트림 재연결 07:53·08:55; 전일 15:30:29는 22=22) |
| `receiver_not_single_alive`, `maintenance_expired` (watchdog notification) | 수신기가 0개 또는 2개 / 유지보수 마커가 만료됐는데 남아 있음 | 0개면 재시작 승인, 2개면 즉시 보고(원장 경합). 마커 정리는 배포 도구 소유자(Codex) | 2026-11-03 19:39, 21:42 |
| `monitor.reason=outside_session` | 장외라 모니터 유휴 | 정상 | |
| `state.json pid` ≠ launchd pid | 이중 실행 또는 복구 미완 | 프로세스 목록 확인 | |

## 키움 REST API 수준 (조사 노트 출처, 확인 필요 표시)

| 항목 | 내용 | 출처·상태 |
|---|---|---|
| 토큰 발급 `au10001` / 폐기 `au10002` | 앱키·시크릿으로 발급, `expires_dt` 14자리 KST. 발급 24시간 뒤 만료로 관측(19:42:01 발급 → 20261104194200) | 관측 확인. 폐기는 호출 금지 |
| 토큰 공유 사고 | 같은 앱키를 쓰는 한 프로세스의 폐기가 다른 프로세스 토큰을 무효화 | AlgoLab 블로그 사례(2026-08), 확인 필요 |
| 호출 제한 | "초당 20건" 주장과 "지속 약 1건/초" 실측이 충돌 | kiwoom-client 실측 vs 커뮤니티, 확인 필요. 봇은 TR별 `http-rate` 파일로 자체 제한 |
| 모의투자 서버 | `mockapi.kiwoom.com`, KRX만 지원(NXT 없음), 일부 계좌 TR 미지원 | 공식 저장소·kiwoom-client, TR 목록은 `mock-limits.md` |
| 모의/실전 키 불일치 | 실전 키로 모의 서버 호출(또는 반대) 시 인증 오류 | 조사 노트 "8001 mode mismatch", 코드 확인 필요 |
| IP 등록 | 실전은 등록 IP에서만 | 공식 문서, 모의도 적용되는지 확인 필요 |
| 공식 MCP | `kiwoom-spec`(키 불필요), `kiwoom-exec`(`KIWOOM_MCP_ALLOW_ORDERS="1"`일 때만 주문 도구) | https://github.com/Kiwoom-Securities/Kiwoom-REST-API/blob/main/SETUP-MCP.md |

## triage 요령

1. 프로세스 → state.json → 토큰 → 원장 신선도 순서로 본다. 앞 단계가 깨지면 뒤 단계 경고는 파생일 수 있다.
2. 장외의 `gap_count` 증가는 재시작 흔적이지 체결 유실이 아니다. 장중 증가만 문제다.
3. 한 번 관측된 서명이라도 재현 조건이 다를 수 있다. 보고에는 파일 경로와 시각을 함께 적는다.
