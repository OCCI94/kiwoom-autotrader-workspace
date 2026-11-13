# 이 프로젝트의 LaunchAgent 규약 (2026-11-03 22:18 KST 점검)

`~/Library/LaunchAgents/com.local.kiwoom-autotrader.*.plist` 여섯 파일과 `launchctl print gui/501/<라벨>`을 읽어 정리했다. 환경변수는 **키 이름만** 적는다. plist 파일은 모두 모드 0600이고 `plutil -lint` OK였다.

## 공통

- 라벨 접두사: `com.local.kiwoom-autotrader.`, 파일명 = 라벨 + `.plist`.
- python: `/Users/tr-oh/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3` (→ `python3.12`, 3.12.14). `stability-watchdog`만 `python3.12`를 직접 가리킨다. 시스템 `/usr/bin/python3`(3.9.6)는 워커에 쓰지 않는다.
- 플래그: 상주 워커는 `-u`(무버퍼), 배포 스크립트 워커는 `-B`(.pyc 금지). 둘을 같이 쓰지는 않았다.
- 상태 루트: `/Users/tr-oh/Library/Application Support/KiwoomAutotrader/`. 패키지 워커는 `--state-dir <상태루트>/<워커>`를 넘기고, 배포 스크립트 워커는 개별 `--xxx-state`, `--state-dir`, `--journal-root` 등으로 경로를 받는다.
- 로그: 상태 루트 아래. 상주 워커는 stdout/stderr를 같은 파일에, `telegram-nightly`와 `telegram-candidates`는 분리.
- `ProcessType: Background`(nightly 제외), `Umask: 63`(0o077; candidates, toss-readonly, nightly는 없음), `ThrottleInterval` 30~60.
- 코드 위치: 패키지 워커는 `WorkingDirectory = <상태루트>/runtime`에서 `-m kiwoom_autotrader`. Codex 배포 워커는 `<상태루트>/research-readonly/<배포명-해시>/` 아래 스크립트를 절대 경로로 가리키고 같은 디렉터리를 `WorkingDirectory`로 쓴다. 입력 파일에는 `--xxx-sha256` 인자로 해시를 고정한다.
- 외장 볼륨 `/Volumes/AIStockData`를 읽는 워커: `telegram-candidates`, `telegram-nightly`. 점검 시 마운트돼 있었다.

## 여섯 라벨

| 라벨(접두사 생략) | 역할 | 트리거 | Throttle | RunAtLoad | 세션 제한 | WorkingDirectory | 로그 |
|---|---|---|---|---|---|---|---|
| `telegram` | Telegram 주문 수신기 (`-u -m kiwoom_autotrader telegram-listen --state-dir <상태루트>/telegram`) | KeepAlive {SuccessfulExit=false} | 30 | true | Aqua | `<상태루트>/runtime` | `<상태루트>/telegram/listener.log` (out=err) |
| `telegram-account` | 계좌 수집기 런처 (`-u <상태루트>/bin/telegram_ssd_launcher.py`) | KeepAlive {SuccessfulExit=false} | 30 | true | Aqua | `<상태루트>/runtime` | `<상태루트>/telegram-account/collector.log` (out=err) |
| `telegram-candidates` | 후보 탐색기 (`-B .../ai-stock-research/telegram_candidate_discovery.py --interval 60 ...`, 내부 루프) | KeepAlive false (RunAtLoad 1회) | 없음(기본 10) | true | 없음 | `/Users/tr-oh/Documents/Codex/2026-10-26/new-chat` | `<상태루트>/telegram-candidates/worker.stdout.jsonl`, `worker.stderr.log` |
| `toss-readonly` | 토스 읽기 전용 후보 워커 (`-B <상태루트>/research-readonly/stream-toss-recovery-20261103/.../toss_candidate_worker.py --allow-network ...`) | KeepAlive false (RunAtLoad 1회) | 60 | true | 없음 | `<상태루트>/research-readonly/stream-toss-recovery-20261103` | `<상태루트>/research-readonly/toss-readonly-worker.log` (out=err) |
| `stability-watchdog` | 안정성 감시기 (`python3.12 -B <상태루트>/research-readonly/stability-watchdog-20261103-ce74d03397a5/stability_watchdog.py --run ...`) | KeepAlive {SuccessfulExit=false} | 60 | true | 없음 | 같은 배포 디렉터리 | `<상태루트>/observability/watchdog/worker.log` (out=err) |
| `telegram-nightly` | 야간 채널 점수·배포 (`<상태루트>/research-readonly/telegram-nightly-closure-0e211c1d3ad7/work/telegram_nightly_worker.py --production ...`) | StartInterval 60 + KeepAlive {SuccessfulExit=false} | 60 | 없음 | 없음 | 같은 배포 디렉터리 | `<상태루트>/observability/telegram-nightly/telegram-nightly.log`, `telegram-nightly-error.log` |

`<상태루트>` = `/Users/tr-oh/Library/Application Support/KiwoomAutotrader`.

### EnvironmentVariables 키 이름(값 미기록)

| 라벨 | 키 |
|---|---|
| `telegram` | (없음) |
| `telegram-account` | (없음) |
| `telegram-candidates` | `PYTHONDONTWRITEBYTECODE`, `PYTHONUNBUFFERED` |
| `toss-readonly` | `PYTHONDONTWRITEBYTECODE`, `PYTHONUNBUFFERED` |
| `stability-watchdog` | `PYTHONDONTWRITEBYTECODE`, `PYTHONPATH` |
| `telegram-nightly` | `PYTHONDONTWRITEBYTECODE`, `TZ` |

비밀값으로 보이는 env 값은 없었다. 단 `toss-readonly`는 `--credentials`, `--dart-credentials` 인자로 `.secrets/` 아래 **파일 경로**를 넘긴다. 경로 자체는 비밀이 아니지만 `ps`와 `launchctl print`에 보이므로 `validate_plist.py`가 경고한다.

## 점검 시점 상태 (2026-11-03 22:18:51 KST, `launchctl print gui/501/<라벨>`)

| 라벨(접두사 생략) | state | pid | last exit code | runs | 비고 |
|---|---|---|---|---|---|
| `telegram` | **서비스 없음** (exit 113, "Could not find service") | – | – | – | plist 존재, `print-disabled`에는 `enabled`. `telegram-listen` 프로세스 없음. `listener.log` 최종 수정 21:32. 이 스킬은 올리지 않는다. 사람이 `launchctl bootstrap`할지 결정 |
| `telegram-account` | running | 39036 | 1 | 34 | KeepAlive가 1로 죽은 뒤 재시작한 흔적. 프로세스 경과 1h02m |
| `telegram-candidates` | running | 35540 | 1 | 2 | 경과 1d13h. 내부 60초 루프 |
| `toss-readonly` | running | 7444 | (never exited) | 1 | 경과 12h38m |
| `stability-watchdog` | running | 34288 | (never exited) | 1 | 경과 4h07m, forks 248 |
| `telegram-nightly` | not running | – | 0 | 250 | `run interval = 60 seconds`. 작업 지시 시점 3시간에 190회, 점검 시 250회. 설계상 정상(때가 아니면 0 종료), 로그 시끄러움 |

`launchctl list` 요약(같은 시각): telegram-account 39036/1, toss-readonly 7444/0, telegram-candidates 35540/1, stability-watchdog 34288/0, telegram-nightly -/0. `telegram`은 목록에 없음.

## 설계상 참고

- `telegram-nightly`를 `StartCalendarInterval`로 옮기면 `runs`가 하루 1~2회가 되어 상태로 문제를 읽을 수 있다. 다만 프로그램이 자체 스케줄(일간·주간 승인 파일)을 보므로 바꿀 때는 프로그램의 "때" 판단과 launchd 시각을 맞춰야 한다. 바꾸는 결정은 사람이 한다.
- `telegram-candidates`, `toss-readonly`는 KeepAlive가 false라 프로세스가 죽으면 다시 뜨지 않는다. 재시작은 `kickstart -k` 한 줄을 사람에게 준다.
- 수신기 재적재는 거래일 09:00–15:30 KST 밖에서만, 사용자가 라벨을 명시해 승인했을 때만 명령을 제시한다.
