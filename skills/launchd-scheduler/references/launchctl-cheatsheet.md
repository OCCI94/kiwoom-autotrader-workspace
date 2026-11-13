# launchctl 치트시트 (사용자 LaunchAgent, gui/$(id -u) 도메인)

이 Mac의 사용자 uid는 501이므로 도메인은 `gui/501`이다. 아래 명령 중 **상태를 바꾸는 것**(bootstrap, bootout, kickstart, enable, disable)은 사람이 직접 실행한다. 스킬 스크립트는 `print`, `list`, `print-disabled`, `plutil -lint`만 쓴다.

## 명령

| 명령 | 효과 | 비고 |
|---|---|---|
| `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/<라벨>.plist` | plist를 도메인에 적재. `RunAtLoad: true`면 즉시 실행 | 같은 라벨이 이미 있으면 "Bootstrap failed: 5: Input/output error" 또는 "service already loaded"로 실패. 먼저 bootout |
| `launchctl bootout gui/$(id -u)/<라벨>` | 서비스 종료(SIGTERM, `ExitTimeOut` 기본 5초 뒤 SIGKILL)와 제거 | plist 파일은 남는다. 라벨 하나만. 접두사나 와일드카드 불가 |
| `launchctl kickstart -k gui/$(id -u)/<라벨>` | 실행 중이면 죽이고(-k) 다시 시작, 아니면 시작 | 적재된 plist 그대로 재시작. plist를 바꿨다면 `bootout → bootstrap` |
| `launchctl print gui/$(id -u)/<라벨>` | 서비스 상세(아래 "읽는 법") | 읽기 전용. 없으면 "Could not find service", 종료 코드 113 |
| `launchctl print gui/$(id -u)` | 도메인 전체: services 표(pid, 마지막 종료 코드, 라벨), disabled 목록 | 출력이 길다. 라벨만 뽑아 쓴다 |
| `launchctl list` | 레거시 3열(pid, status, label). pid가 `-`면 실행 중 아님 | status는 마지막 종료 코드. 빠른 확인용 |
| `launchctl print-disabled gui/$(id -u)` | `"<라벨>" => enabled/disabled` 목록 | `disable`된 라벨은 bootstrap이 거부된다 |
| `launchctl enable gui/$(id -u)/<라벨>` | disabled 표시 해제 | 적재는 별도로 bootstrap |
| `launchctl disable gui/$(id -u)/<라벨>` | 로그인 시 자동 적재 금지, bootstrap 거부 | 재로그인 후에도 유지(override DB) |
| `launchctl blame gui/$(id -u)/<라벨>` | 왜 지금 실행 중인지(예: `ipc (mach)`, `interval`) | 읽기 전용 |
| `plutil -lint <plist>` | XML 문법 검사 | 키 의미는 검사하지 않는다. `validate_plist.py`가 보완 |
| `launchctl load -w` / `unload -w` | 레거시. `bootstrap`/`bootout`으로 대체 | `-w`는 Disabled 키를 영구 토글하므로 쓰지 않는다 |
| `launchctl remove <라벨>` | 레거시 bootout | 공개 스킬 감사에서 "접두사 루프로 remove" 패턴이 위험 항목으로 지적됨. 쓰지 않는다 |

도메인 표기: `gui/<uid>`는 로그인한 GUI 세션, `user/<uid>`는 로그인 여부와 무관한 사용자 도메인. LaunchAgent는 `gui/<uid>`를 쓴다. `LimitLoadToSessionType: Aqua`면 GUI 세션에서만 적재된다.

## 정확한 라벨 규칙

- 라벨 = `Label` 키 = 파일명 stem. `com.local.kiwoom-autotrader.telegram`과 `com.local.kiwoom-autotrader.telegram-account`는 접두사가 같지만 다른 서비스다. 접두사 일치로 여러 개를 다루는 스크립트는 수신기를 함께 죽인다.
- 주문 수신기 `com.local.kiwoom-autotrader.telegram`은 사용자가 라벨을 명시해 승인하고, 거래일 09:00–15:30 KST 밖일 때만 bootout/kickstart -k 대상이 된다.
- 같은 `Label`을 가진 plist 파일이 `~/Library/LaunchAgents`에 둘 있으면 로그인 시 나중 것이 경고 없이 앞 것을 대체한다.

## `launchctl print` 읽는 법

```
gui/501/com.local.kiwoom-autotrader.telegram-nightly = {
	active count = 0            # 0이면 지금 프로세스 없음
	path = /Users/.../com.local.kiwoom-autotrader.telegram-nightly.plist
	state = not running         # running | not running | waiting
	program = /Users/tr-oh/.cache/.../python3
	arguments = { ... }         # ProgramArguments 전체
	working directory = ...
	stdout path = ...
	stderr path = ...
	environment = { ... }       # EnvironmentVariables 값이 그대로 찍힌다. 보고서에 붙이지 않는다
	runs = 250                  # 적재 이후 launchd가 띄운 횟수(재시작 포함). 재부팅/bootout 시 0
	pid = 39036                 # 실행 중일 때만 보인다
	last exit code = 0          # 0 | N | (never exited). 시그널 종료는 "last exit reason"
	semaphores = { successful exit => 0 }   # KeepAlive SuccessfulExit=false 의 흔적
	run interval = 60 seconds   # StartInterval
	properties = runatload | inferred program
}
```

- `runs`가 빠르게 늘고 `last exit code`가 0이 아니면 KeepAlive 재시작 루프(ThrottleInterval 간격).
- `runs`가 하루 1,440 근처로 늘고 `last exit code = 0`이면 `StartInterval 60` + "때가 아니면 0 종료" 구조. 정상이지만 시끄럽다.
- `state = running`인데 `last exit code = 1`이면 이전 실행이 1로 죽고 KeepAlive가 다시 띄운 것. 로그에서 원인을 본다.
- `(never exited)`는 적재 이후 한 번도 종료하지 않았다는 뜻(상주 워커의 정상 상태).
- `launchctl list`의 status 열은 `last exit code`와 같다. 음수는 시그널 번호(예: -9 SIGKILL, -15 SIGTERM).

## StartInterval vs StartCalendarInterval, 잠자기 뒤 동작

| | StartInterval N | StartCalendarInterval {Hour, Minute, ...} |
|---|---|---|
| 기준 | 마지막 실행 시작 시각 + N초 | 달력 시각(시스템 시간대). 생략한 필드는 와일드카드 |
| 적재 직후 | N초 뒤 첫 실행(RunAtLoad 없으면 즉시 실행 없음) | 다음 일치 시각 |
| 잠자기 중 놓침 | 깨어날 때 **한 번** 실행(여러 번 놓쳐도 한 번) | 깨어날 때 **한 번** 실행(여러 번 놓쳐도 한 번) |
| 전원 꺼짐 중 놓침 | 실행 안 함 | 실행 안 함(anacron 같은 보정 없음) |
| 실행 중 다음 시각 도래 | 실행 중이면 건너뜀(중복 실행 없음) | 같음 |
| ThrottleInterval | 재시작 간 최소 10초. 60초 주기에는 영향 없음 | 같음 |

`Weekday`는 0=일요일 … 6=토요일(7도 일요일). 주간 작업은 `{Weekday: 1, Hour: 6, Minute: 0}`처럼 적는다. 여러 시각은 dict 배열로 적는다.

## 로그 위치

- 프로그램 stdout/stderr: plist의 `StandardOutPath`, `StandardErrorPath`. 이 프로젝트는 `~/Library/Application Support/KiwoomAutotrader/<워커>/*.log` 또는 `observability/<워커>/*.log`. 디렉터리가 없으면 launchd가 파일을 못 열고 서비스가 뜨지 않는다.
- launchd 자체 메시지(스폰 실패, 경로 오류): `log show --last 30m --predicate 'subsystem == "com.apple.xpc.launchd" AND eventMessage CONTAINS "<라벨>"'` (읽기 전용, 느릴 수 있음).
- 레거시 시스템 로그 `/var/log/system.log`에는 최신 macOS에서 launchd 메시지가 거의 남지 않는다.
- 로그는 launchd가 자르지 않는다. 커지면 프로그램 쪽에서 회전하거나 `newsyslog` 규칙을 둔다.
