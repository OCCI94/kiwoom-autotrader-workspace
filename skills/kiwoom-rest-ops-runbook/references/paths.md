# 상태 디렉터리와 LaunchAgent (2026-11-03 관측)

상태 디렉터리 `APP = ~/Library/Application Support/KiwoomAutotrader`

| 경로 | 역할 | 비고 |
|---|---|---|
| `telegram/state.json` | 수신기 상태 정본 (pid, status, execution_recording, automation_status) | 수신기가 주기적으로 덮어씀 |
| `telegram/listener.log` | 수신기 로그, `[YYYY-MM-DD HH:MM:SS KST]` 접두 | 로그인 성공 줄에 토큰 만료 시각 14자리 |
| `telegram/listener.lock`, `research.sock` | 잠금·소켓 | |
| `telegram/paper-experiment.sqlite3` | 실험 DB (assignments, attempts, outcomes, selections) | 복사본으로만 읽기 |
| `telegram/nightly-standing-delivery/`, `nightly-authorizations/` | 야간 발송 승인·claim·결과 | |
| `runtime/kiwoom_autotrader/` | 실행 중인 코드 복사본 | mtime = 마지막 배포. 소스는 `Documents/Codex/2026-10-26/new-chat/outputs/ai-stock` |
| `runtime/logs/.execution-ledger.sqlite3` | 원장 (orders, intents, fills, profits, holdings, order_decisions, broker_observations …) | 약 1GB. 점검 중 열지 않음 |
| `runtime/logs/YYYYMMDD.broker-observations.jsonl` | 증권사 응답 관측 (source_api별) | |
| `runtime/logs/YYYYMMDD.broker-observation-repeats.jsonl` | 반복 스냅샷 | |
| `runtime/logs/YYYYMMDD.execution-history.jsonl` | 체결·수명 이벤트 | 수 MB |
| `runtime/logs/strategy-signal-events-YYYYMMDD.jsonl` | arm 신호·결정 이벤트 (`{"event": {...}, "event_sha256"}`) | |
| `runtime/logs/YYYYMMDD.execution-accounting.json` | 일별 회계 | |
| `config/data/settings.json`, `paper-strategy-options.json` | 설정 | 변경은 승인 필요 |
| `config/secrets/` (700) | 자격증명 (600) | 읽지 않음 |
| `http-rate.json.paper-<TR>` | TR별 마지막 호출 레이트 기록 | mtime으로 마지막 호출 시각 |
| `backups/` | 배포·복구 전 백업 폴더들 | |
| `code-deployment-claims/` | 배포 claim 기록 (정상/예외) | |
| `observability/control-audit-YYYYMMDD.jsonl`, `observability/watchdog/` | 제어 감사 로그, 감시기 샘플·알림 | |
| `telegram-account/collector-status.json`, `collector.log` | 개인계정 수집기 상태 | 로그에 타임스탬프 없음 |
| `research-readonly/toss-evidence-refresh-claims/YYYYMMDD-HHMM.result.json` | Toss 읽기 전용 슬롯 결과 | |
| `research-readonly/telegram-nightly-*` | 야간 작업 설치본 | |

외장: `/Volumes/AIStockData/ai-stock-research-data/` (telegram-nightly 산출물, source-backups, s5-full-eligibility 캘린더), `/Volumes/AIStockData/telegram-channel-archive/channels-*.sqlite3` (채널 보관소), `/Volumes/T9` (백업).

## LaunchAgent 라벨

| 라벨 | 역할 | 트리거 |
|---|---|---|
| `com.local.kiwoom-autotrader.telegram` | 수신기(매매·스트림·텔레그램) | KeepAlive |
| `com.local.kiwoom-autotrader.telegram-account` | 개인계정 채널 수집기 | KeepAlive, 타임아웃 시 재시작 |
| `com.local.kiwoom-autotrader.telegram-candidates` | 후보 추출기 | 주기 |
| `com.local.kiwoom-autotrader.toss-readonly` | Toss 읽기 전용 갱신 | 상주 |
| `com.local.kiwoom-autotrader.stability-watchdog` | 안정성 감시기 (2026-11-03 18:11 설치) | 상주 |
| `com.local.kiwoom-autotrader.telegram-nightly` | 야간 분류·발송 (2026-11-03 18:09 설치) | StartInterval 60, 18:30 실행 |

조회: `launchctl print gui/$(id -u)/<라벨>` (읽기 전용). 정확한 라벨만 쓴다.

## Codex 기록 위치

- 보고서: `Documents/Codex/2026-10-26/new-chat/outputs/ai-stock-research/reports/<YYYY-MM-DD>/`
- 작업 기록: `Documents/Codex/2026-10-26/new-chat/work/<작업명>/`
- 배포 계약: `outputs/ai-stock/AGENTS.md` "배포 검증 계약"
