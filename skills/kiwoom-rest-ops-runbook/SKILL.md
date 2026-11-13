---
name: kiwoom-rest-ops-runbook
description: Read-only health check and incident triage runbook for the Kiwoom Securities REST API paper-trading bot (키움 REST 모의투자, LaunchAgent-run Telegram receiver, 00 execution stream, SQLite ledgers, nightly workers). Use when the user asks for 봇 점검, 헬스체크, 전체 점검, 수신기 상태, 스트림 ready, 토큰 만료, 체결 기록 오류, 수집기 멈춤, 야간 작업 확인, or when a Telegram alert or watchdog notification arrives. Never restarts, deploys, changes settings, issues or revokes tokens, or places orders; it inspects local state and reports.
license: MIT
compatibility: macOS with launchd, Python 3.9+ standard library only. Reads ~/Library/Application Support/KiwoomAutotrader (state.json, listener.log, ledger file metadata, LaunchAgent status). No network, no broker calls, no secrets read.
metadata:
  author: tr-oh
  version: "0.1"
  created: "2026-11-03"
---

# 키움 REST 모의투자 봇 운영 런북

이 봇은 LaunchAgent `com.local.kiwoom-autotrader.telegram`으로 도는 수신기 한 개(텔레그램 명령 + 모의투자 자동매매 + 00 체결 스트림)와 보조 워커(개인계정 수집기, 후보 추출기, Toss 읽기 전용, 안정성 감시기, 야간 분류)로 이뤄진다. 상태의 정본은 `telegram/state.json`이고, 원장은 `runtime/logs/.execution-ledger.sqlite3`와 날짜별 JSONL이다. 경로는 `references/paths.md`.

## 절대 규칙

1. 점검은 읽기 전용이다. 실행 중인 봇·설정·원장·마커·LaunchAgent를 바꾸지 않는다. 재시작·배포·설정 변경은 사용자 승인 뒤 별도 절차(`arm-change-review`)로 한다.
2. 토큰을 발급(au10001)하거나 폐기(au10002)하지 않는다. 앱키 하나로 받은 토큰은 공유되며, 한 프로세스의 폐기가 다른 프로세스의 토큰을 죽인 사례가 있다. 발급이 기존 토큰을 무효화하는지는 확인되지 않았으므로 봇이 살아 있는 동안 점검 스크립트는 증권사를 호출하지 않는다.
3. 비밀값(`config/secrets/`, 토큰 문자열, 앱키)은 읽지도 출력하지도 않는다. 경로와 권한만 적는다.
4. 원장 SQLite는 점검 중 열지 않는다(크기·mtime만). 내용 대조가 필요하면 복사본으로 `ledger-reconciliation`을 쓴다.
5. 장중(거래일 09:00~15:30 KST)에는 프로세스에 영향을 주는 어떤 명령도 내지 않는다. `launchctl print`와 파일 읽기만 허용한다.

## 절차 (triage 순서)

1. **프로세스.** `launchctl print gui/$(id -u)/com.local.kiwoom-autotrader.telegram`으로 state·pid·last exit code를 보고, `ps`로 같은 pid가 살아 있는지, 수신기가 한 개뿐인지 확인한다. 두 개면 즉시 보고(원장 경합 위험).
2. **state.json.** `pid`가 launchd pid와 같은지, `status`, `execution_recording.ready/active/error/gap_count/last_observed_at`, `automation_status.paper_experiment.enabled/failure`, `monitor.active_arms` 개수(기대 14), `monitor.reason`, `recovery.status/remaining_registered_pending`, `ttl.status/reason/principal_reservation_released`, `prior_day_reconciliation.status`를 읽는다.
3. **토큰.** `listener.log`의 마지막 "로그인 성공 … 만료 시각(KST): YYYYMMDDHHMMSS" 줄로 남은 시간을 계산한다. 봇은 매일 08:55에 갱신한다. 거래일 장전에 2시간 미만이면 경고.
4. **원장·로그 신선도.** 원장 파일 mtime, 오늘 날짜 JSONL(`YYYYMMDD.broker-observations.jsonl`, `.execution-history.jsonl`, `strategy-signal-events-YYYYMMDD.jsonl`) 존재와 mtime. 장중에 10분 이상 멈춰 있으면 경고. `http-rate.json.paper-<TR>` mtime으로 마지막 TR 호출 시각을 본다.
5. **설정·실행본.** `config/data/settings.json`, `paper-strategy-options.json`, `runtime/kiwoom_autotrader` 디렉터리의 mtime이 마지막 승인된 배포 시각과 같은지. 달라졌으면 누가 바꿨는지 Codex 기록(`work/`, `reports/<날짜>/`)에서 찾는다.
6. **보조 워커.** 나머지 LaunchAgent 다섯 개의 state·runs·last exit, `telegram-account/collector-status.json`, Toss 슬롯 결과(`research-readonly/toss-evidence-refresh-claims/*.result.json` 최신), 감시기 샘플·알림(`observability/watchdog/`), 야간 산출물(`/Volumes/AIStockData/ai-stock-research-data/telegram-nightly/`).
7. **저장소.** 상태 디렉터리와 `/Volumes/AIStockData`, `/Volumes/T9` 여유 공간. 원장 크기 추세(현재 약 1GB).
8. **판정과 보고.** 아래 형식으로 "문제 → 원인 후보 → 영향 → 권고(승인 필요 여부)"를 쓴다. 모르는 것은 모른다고 쓴다.

1~7은 `scripts/healthcheck.py`가 한 번에 수행한다. 스크립트가 못 보는 것(Codex 기록, 야간 산출물 내용)은 사람이 보충한다.

## 스크립트

```
python3 scripts/healthcheck.py [--state-dir PATH] [--label com.local.kiwoom-autotrader.telegram] [--day YYYYMMDD] [--format json|table]
```

종료 코드: 0 전부 정상, 1 경고 있음, 2 실패 있음, 3 실행 오류. 네트워크와 증권사 호출 없음. 비밀값은 읽지 않는다. 장중 여부(KST 09:00~15:30, 주말 제외; 휴장일은 `--holidays` JSON으로 전달)에 따라 신선도 임계값이 달라진다.

## 보고 형식

```
점검 시각: YYYY-MM-DD HH:MM KST (장중/장외)
수신기: PID N, launchd state, 시작 시각, 단일 인스턴스 여부
스트림: ready=…, gap=…, 마지막 관측 …분 전
실험: enabled=…, arm N개, monitor.reason=…, 등록 대기 N, ttl=…
토큰: 만료까지 N시간 (갱신 08:55)
원장: mtime …, 오늘 JSONL 3종 존재 여부, 크기
설정·실행본: 마지막 변경 시각 (승인 배포와 일치 여부)
워커: 수집기/후보/Toss/감시기/야간 각 state·마지막 결과
저장소: 여유 공간
문제점: (번호, 심각도, 근거 파일 경로)
권고: (승인이 필요한 것은 "승인 대기"로 표시)
```

## 알려진 장애 서명

`references/kiwoom-errors.md`에 표가 있다. 자주 본 것만 적으면: 체결 기록 오류 뒤 00 스트림 `ready=false`로 남음(2026-11-03 07:53 사례, 재시작으로 복구); TTL 취소 뒤 정산이 `settlement_unconfirmed`·`cancellation_rest_original_identity_conflict`로 30초마다 재시도(예약금 미해제, 원인은 kt00009 `mdfy_cncl_tp`='일반' 파싱); 공용 시세 수집 `request_expired` HALT; Toss `HTTP 429`·`REFRESH_REQUEST_BUDGET_EXPIRED`; 개인계정 수집기 타임아웃 재시작; 배포 도구 자체 결함으로 수신기만 정지.

## 함정

- `state.json`의 `pid`는 마지막으로 그 파일을 쓴 프로세스다. launchd의 pid와 다르면 두 개가 돌았거나 복구가 덜 됐다.
- `execution_recording.gap_count`는 재시작마다 늘 수 있다. 장외 재시작으로 생긴 gap은 체결 유실이 아니다.
- `monitor.reason=outside_session`은 정상이다. 장중에 이 값이면 세션 판정 입력(휴장일·캘린더)을 의심한다.
- `listener.log`에는 타임스탬프가 있지만 `telegram-account/collector.log`에는 없다. 수집기 시각은 `collector-status.json`을 쓴다.
- 토큰 만료 시각은 14자리 KST 문자열이다. 발급 시각 + 24시간으로 관측됐다.
- 점검 보고에 "재시작했다"고 쓰려면 실제로 승인받고 실행한 기록이 있어야 한다. 추정으로 쓰지 않는다.

## 참조 파일

- `references/paths.md`: 상태 디렉터리 구조, LaunchAgent 라벨, 로그·원장 경로.
- `references/kiwoom-errors.md`: 장애 서명과 원인·대응 표.
- `references/krx-sessions.md`: KRX·NXT 세션 시각, 동시호가, VI, 이 봇의 시간 규칙.
- `references/tick-sizes.md`: 호가단위 표(소스 `common_tick`와 대조).
- `references/mock-limits.md`: 모의투자 서버 제약, 토큰 수명, 수수료·세금 관측치, 오늘 쓰인 TR 목록.
- `references/data-delays.md`: 외부 데이터 지연 규칙.
