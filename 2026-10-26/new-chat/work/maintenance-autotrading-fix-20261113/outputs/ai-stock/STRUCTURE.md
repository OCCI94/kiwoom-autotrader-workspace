# 프로젝트 구조 가이드

이 문서는 소스의 역할과 기본 저장 경로를 설명합니다. 실행 중인 프로세스의 버전이나 배포 완료 여부를 인증하는 문서는 아닙니다. 프로젝트 최상위 `main.py`와 `python -m kiwoom_autotrader`는 `kiwoom_autotrader/main.py`로 연결됩니다. 인자 없이 실행하면 텔레그램 봇 수신을 시작합니다.

## 소스와 문서

```text
ai-stock/
  main.py                         실행 진입점
  start.command                   macOS 실행, 기본 명령은 봇 수신
  start.bat                       Windows .venv 준비·의존성 설치·실행
  console.command                 대화형 터미널 수신·토큰 확인
  setup-*.command                 키움 모의·실전 키와 봇 연결 설정
  telegram-*.command              봇·개인 계정 수집 연결/자동 실행/중지
  connect-mock.command            모의 연결 확인
  top-turnover.command            일반주식 거래대금 순위 조회
  run-demo.command                오프라인 예제 시뮬레이션
  export_tr_docs.py                워크북 전체 시트의 Markdown 변환
  export-tr-docs.command           변환기 전용 실행 도우미
  requirements.txt                실행 도우미가 설치하는 의존성
  requirements-tr-docs.txt         변환기 전용 openpyxl 의존성
  pyproject.toml                  Python 패키지·선택 의존성 정의
  AGENTS.md                      프로젝트 작업 규칙
  RANK_AGENT_INSTRUCTIONS.txt      순위 기능의 구현 요구사항 기록
  GOLDEN_CROSS_AGENT_INSTRUCTIONS.txt  골든크로스 구현 요구사항 기록
  kiwoom_autotrader/
    __main__.py                   python -m 진입점
    main.py                       CLI 라우팅·기능 연결
    api/                          키움 REST 요청과 응답 검증
      client.py                   공통 모의 조회 클라이언트
      session.py                  모의/실전 로그인·메모리 토큰·예약 갱신
      errors.py                   공통 API 오류와 확인된 IP 오류 분류
      stock_info.py               현재 세션의 국내 종목 정보
      stock_names.py              정확한 코스피·코스닥 종목명 → 코드
      account_report.py           자금·보유 내역 연속조회와 검증
      orders.py                   현재 세션의 KRX 주문·전송 직전 검사
      order_book.py               예산 매수에 필요한 KRX 표시호가 확인
      minute_chart.py             1분봉 조회·응답 검증·요청 간격 공유
      executions.py               실제 체결과 당일 매도 비용 자료 조회
      rankings.py                 현재 세션의 네 종류 종목 순위
      equity_rankings.py          일반기업 주식 분류와 거래대금 순위 결합
      au10001.py, ka10001.py, ka10032.py, ka10099.py
      kt00001.py, kt00018.py, kt10000.py, kt10001.py, usa20100.py
                                  개별 TR의 요청 구성·명세 연결
    realtime/
      engine.py                   세션별 공용 WebSocket·구독·재연결
      execution_stream.py         00 단위/누적 체결 관측·구독 ACK 확인·종료 시 기록 배출
      README.md                   프로토콜과 지원 범위
    telegram/
      client.py                   Telegram Bot API 통신
      listener.py                 명령·예약·두 교차 감시·체결 추적의 실행 루프
      setup.py, bot_credentials.py  봇 연결 설정과 인증정보 접근
      account.py, account_setup.py, account_credentials.py
                                  개인 계정 인증·채널 선택·인증정보 접근
      collector.py                선택 채널의 과거/새 글 수집
      channel_store.py            SQLite 관측 버전·채널·처리 위치
      commands/                   텔레그램 명령별 파싱과 처리
        login.py                  login paper/real, login renewal
        search.py                 srch 코드/종목명
        orders.py                 buy, buy max, sell
        report.py                 report/r 자금·보유 내역
        rank.py, rank_batch.py    순위 조회·순위 종목별 명령 확장
        crosses.py                gdcrs 기간/목록, 두 감시 시작·중지
        settings.py               mkhr, stts, stts default
        reservations.py           rsv 등록·목록·삭제
        ip.py                     외부 공인 IP 조회
        help.py, power.py, status.py, top.py, channels.py
                                  도움말·종료·연결·순위·수집 글 조회
    trading/
      paper_experiment_monitor.py  수신기 소유 모의 실험 실행·후보보다 보유 청산 우선·EXP_D 지연청산·종목별 격리
      paper_experiment_rules.py    완료 1분봉 순수 판정·날짜/종목 시드 불변 배정
      paper_experiment_s5.py       최신 완료 5분 경계 S5·관측 VWAP·위험 증거 재검증
      paper_experiment_extended_rules.py  ORB-a/b·GDR·VSS·LDR·ETF-B 순수 수식·선정 검증
      paper_experiment_inputs.py   공용 세션 ka10081·ka10099·ka10054·ka10001 관측/출처·결측 보존
      paper_experiment_scheduler.py  개장 전·09:05·15:15 선정과 시드 충돌 조정·유니버스 SHA
      paper_experiment_store.py    첫 편입·배정·전송시도 SQLite·EXP_D 소유세대별 중복 방지(체결 원장과 구분)
      paper_experiment_recovery.py 기존 세션 REST 대사·소멸 잔량·00 기록기/소유 복구
      paper_experiment_calendar.py 별도 작업자가 저장한 공식 세션 증거 읽기 전용 검사
      golden_cross.py             등록 종목의 새 완료봉 골든크로스 예산 매수
      reentry_cooldown.py         선택적 모의 매도 체결 관측 기반 재진입 대기·공백 차단
      cross_monitor.py            데드크로스·분 경계 재검증·미실행 신호 기록·분봉 결측 격리
      order_context.py            자동주문 전략명과 판단 근거 전달
      paper_policy.py             모의 전용 자동주문·매수당 한도·보유 확인 요구·전송 직전 재검사
      unfilled_buy_ttl.py         기본180초·D/VSS/LDR 검증창 종료 TTL·만료 전건 순차 재검사
      cancellation_settlement.py  최종 취소·부분체결·미체결 대사 전 예약금 보존
      maintenance_reconciliation.py 정비 주문 금지 중 공유 세션의 새 관측·actor/배포자 공용 cutover 잠금
      maintenance_expiry_resolution.py 별도 승인된 전일 소멸 증명 확정·이중 주문 차단·인계 검증
      paper_reconciliation.py     종목별 총매입 한도·체결/잔고/미체결 대사·관측 원장 기록
      cutoff_reconciliation.py    손익조회 전후 체결 시점·수량·대금 비교 (수신 지연·시각 경계 불명은 미확정)
      decision_evidence.py        주문 당시 근거의 허용 필드·크기·형식 검증
      execution_log.py            사전 의도·접수·실제 체결 원장·CSV·단위체결 금액 및 비용 출처별 대사
      simulation.py               오프라인 예제 전략·가상 주문·체결
      message_factors.py          당시 관측 정보만 사용하는 메시지 팩터
    diagnostics/
      __main__.py, audit.py       로컬 원장/CSV/상태의 읽기 전용 감사
      models.py, render.py        감사 결과 모델·정적 HTML 보고서
    history/
      __main__.py, core.py        canonical 과거 이력 검증·분리 저장·보고
      adapters.py                추후 원본별 변환기를 위한 인터페이스
    research/
      __main__.py, contracts.py   연구 입력/가설 고정·시각 계약·검증 창
      session_quality.py          분봉 세션 완전성·시초 범위/VWAP 가용성·미래참조 판정
      single_index.py             오프라인 단일지수 회귀 및 자료 시점·결측 검증
    utils/
      paths.py                   프로젝트/배포 소스와 OS별 데이터 경로
      platform_io.py             POSIX/Windows 잠금·파일 저장 경계
      keychain.py, real_credentials.py  OS 자격증명 저장소 접근
      http_queue.py              공통 HTTP FIFO·대기 중 중복 병합·전송 제한
      public_ip.py               공인 IP 확인·확인된 IP 오류 안내
      telegram_service.py        macOS 봇 LaunchAgent·소스 복사본 관리
      account_service.py         macOS 개인 계정 수집 LaunchAgent 관리
      dependencies.py            개인 계정 연결용 선택 의존성
      command_scheduler.py       평일 예약·날짜별 실행 표식
      message_dataset.py         일자별 JSONL·CSV·메타데이터 내보내기
      anchored_dataset.py        SSD 일별 내보내기의 디렉터리 FD 기반 쓰기
    config/
      settings.py                검증한 일반 설정의 읽기·원자적 저장
      data/settings-default.json  기본값
      data/settings.json         빈 일반 설정 템플릿
  docs/
    HANDOFF_APPLICATION.md        인계 자료의 적용 범위와 보류 사항
    DECISION_EVIDENCE.md          주문 당시 판단 근거와 검증 한계
    OFFLINE_AUDIT.md              읽기 전용 감사 실행법·판정 범위
    HISTORY_IMPORT.md             canonical 과거 이력 계약·CLI·제한
    RESEARCH_CONTRACTS.md         연구 입력 고정·시각·검증 창 계약
    SINGLE_INDEX.md              회귀 입력·분석 모드·결과 해석과 한계
    PAPER_EXPERIMENT.md           모의 40/45 실험 프로필·새 6개 arm·관측 대체와 통계 제한
  tr_docs/                        제공된 Excel의 시트별 TR 원문 명세
  tests/                          외부 경계 대역·임시 파일 기반 회귀 검증
  config.example.json             오프라인 예제 전략 설정
  data/telegram/                  messages-export의 기본 출력 위치
  runs/                           로컬 실행 상태·시뮬레이션 기본 출력
  logs/                           이 소스에서 실행한 자동주문 원장·일일 CSV
```

`cli.py`, `kiwoom.py`, `login_session.py`, `telegram_listener.py` 등 패키지 루트의 평면 모듈은 이전 import 경로를 유지하는 호환 연결 파일입니다. 새 기능의 구현은 위 하위 패키지에 있습니다. `__pycache__`, `.venv`, 생성 데이터는 소스 구조에서 생략했습니다.

TR 원문은 [tr_docs/README.md](tr_docs/README.md)의 규칙을 따릅니다. 예를 들어 분봉 명세는 프로젝트 루트 기준 `/tr_docs/ka10080.md`에 있습니다. 변환기는 스크립트와 같은 디렉토리의 워크북을 찾아 `tr_docs/`로 내보내며 실행 당시 작업 디렉토리에 의존하지 않습니다. 원본 워크북·기존 JSON TR 명세·수기 문서는 변환 결과와 구분해 보존합니다.

## 텔레그램과 자동매매 흐름

`listener.py`가 로그인 세션, 예약 실행기, 감시기, 체결 추적기, 공용 WebSocket 엔진을 소유합니다. 명령 처리와 각 감시/추적 단계는 같은 수신 루프에서 순서대로 실행합니다. 수신 시작 시 자동 모의 로그인을 수행하며 이전 실행의 실전 모드를 복원하지 않습니다. 모의 실험 프로필이 켜져 있으면 `login real`을 거부하며 실전 분기는 실험 설정을 읽거나 적용하지 않습니다. 실험 외에도 교차 자동감시 또는 주문 예약이 활성 상태이면 실전 로그인을 거부합니다.

명령 문법의 기준은 [commands/help.py](kiwoom_autotrader/telegram/commands/help.py)입니다.

`paper_automation_only=true`일 때 자동주문은 모의투자에서만 전송하며,
모의 자동주문에서는 설정 초기화 후에도 `paper_automation_buy_cap`과 종목별 원금 한도를 항상 검사합니다. 상시 교차 감시는
하루 횟수 제한 없이 새 완료봉 교차마다 판단하지만 동일 봉 재전송과 미결 주문 중첩은 막습니다.
기존 보수 모드의 모의 자동매수에서는 건당 한도와 종목별 기존 매입금액·신규 주문원금 합계의 한도를 별도로 검사합니다. 각각 기본 100만원, 설정 가능한 최대 300만원입니다.
종목의 `paper_budget`은 모의 감시에만 적용합니다. 실전 자동매수의 공통 `budget`에는 별도 보수적 100만원 상한과 계좌 일 200만원·종목 일 2회·재진입 20분 제한이 적용됩니다.
상향 한도는 계좌 전체 미체결·주문가능액과 내부 미해결 주문을 확인하며, 다른 기기의 동시 주문과 원자적으로 묶이지는 않습니다.
`telegram/state.json`의 `automation_status`는 모드·각 감시의 최근 완료봉·중지 사유를
진단용으로 보존하며 재시작 시 거래 신호로 복원하지 않습니다.
`symbol_errors`에는 종목별 오류와 확인된 누락 시각을 보존합니다. 한 종목의 분봉
조회·검증 실패가 다른 종목의 관측을 지우지 않으며 다음 정규 조회에서 재검사합니다.
서버가 보내지 않은 분의 가격은 원본 봉으로 취급하지 않습니다. 모의 모드에서만 엄격한 조건에 맞는 내부 공백을 출처가 표시된 가정으로 추론할 수 있으며 [README](README.md)에 한계가 설명되어 있습니다. 결측 후 정상 자료가 돌아오면
먼저 기준 관측을 새로 쌓고 이후의 새 교차만 처리합니다.

명시적 `paper_experiment_enabled=true`는 별도 모의 전용 프로필입니다. 기본값은 false이며,
동시 보유·매수 예약 40종목, D/GDR/LDR/VSS 합계 20종목, VSS 8종목, 테마 5종목,
건당 `min(300000, 주문가능현금 // 45)`와 종목당 두 배 한도를 사용합니다. 실제 현금과 비용 여유를
확인한 보유·예약 0종목에는 기록된 1주 초과 예외가 있습니다. 동적 발견 대기열 30개와 진입 풀 40개는
별도 개념입니다. R/A/B/B-TP/E 초기 배정은 각 20%, C/D와 새 6개 arm은 정해진 시각·대상으로 선정합니다.
별도 기본 false 옵션 `paper_experiment_s5_enabled=true`는 신규 `native-six-s5-v1` 배정에서
R/A/B/B-TP/E/S5 각 1/6과 S5용 미사용 2자리·해당 주문 예산 보호를 활성화합니다.
기존 배정은 불변이고 전체 40자리 상한을 늘리지 않습니다. S5는 전용 증거 어댑터로 평가·주문하며,
S2~S4는 기존 주문증빙 adapter 미통합 사유로 실험 프로필에서 계속 일시 중지합니다.
기존 gdcrs/ddcrs의 전 계좌 동작은 A와 arm별 소유 청산으로 대체됩니다.
완료 수식·한도·결측·공식 세션·예상체결가 대체 버전은 [PAPER_EXPERIMENT.md](docs/PAPER_EXPERIMENT.md)를 따릅니다.

| 명령 묶음 | 역할 |
| --- | --- |
| `login paper/real`, `login renewal H:MM` | 로그인 구분·토큰 갱신 시각 |
| `srch`, `report`/`r`, `buy`, `sell` | 현재 로그인으로 조회·KRX 주문 |
| `buy 코드 max 금액` | 현재가로 수량 계산, 예산 이내 지정가 매수; 수수료 별도 |
| `rank`, `rank 1`~`rank 4` | 거래대금·상승률·거래량·인기검색 순위 |
| `rank 번호 cmd 개수 명령` | 한 번 조회한 순위에 `srch`/`buy`/`sell`/`gdcrs add`를 순서대로 적용 |
| `gdcrs intv 단기 장기`, `gdcrs add/list/remove/clear` | 두 감시의 공용 평균 기간, 골든크로스 종목·예산 목록 |
| `start/stop gdcrs`, `start/stop ddcrs` | 골든크로스 매수·입증된 자동전략 소유 물량의 데드크로스 매도 감시 |
| `multi resume` | 레거시 미확정 주문 정지의 명시적 재개 요청; 신선한 대사 통과 전에는 계속 정지 |
| `mkhr`, `stts`, `stts default` | 장 시간·설정 조회·기본값 복원 |
| `rsv 시각 명령`, `rsv list/remove` | 한국 시간 평일 반복 예약 |
| `IP`, `help`, `status`, `top`, `channels`, `posts`, `power off` | IP·도움말·연결·순위·수집 글·종료 |

골든크로스는 등록 종목, 데드크로스는 현재 보유한 현금주식의 매도가능 수량을 대상으로 합니다. 두 감시는 공용 단기/장기 기간과 장 시간 설정을 사용합니다. 단기<장기인 새 완료 1분봉에서 골든크로스는 이전 차이≤0 → 현재 차이>0, 데드크로스는 이전 차이≥0 → 현재 차이<0을 검사합니다. 시작·재시작·관련 설정/세션 변경 후에는 먼저 기준 관측을 쌓으며 과거 교차를 재생하지 않습니다. 장외에는 활성 상태를 유지해 다음 평일을 기다리고, 거래소 휴장일 달력은 사용하지 않습니다.

주문 전 모드·토큰·설정·봉의 신선도·종료 요청을 다시 확인합니다. 주문 시도 표식을 먼저 저장하고, 주문 결과가 불확실하면 해당 전략을 중지해 명시적인 시작 명령을 기다립니다. 예약도 날짜별 실행 표식을 먼저 저장하며 재시작 전에 지난 시각이나 불확실한 전송을 재시도하지 않습니다.

모든 HTTP 요청은 `utils.http_queue.queued_open`을 거칩니다. 같은 데이터 경로를 사용하는 로컬 프로세스는 파일 잠금으로 전송 상한을 공유합니다. 분봉과 호가 조회에는 각 API의 추가 간격 제한도 있습니다. WebSocket은 구독 요청이 있을 때 연결하는 공용 엔진이며, 현재 두 교차 감시의 분봉 조회는 HTTP를 사용합니다. 자세한 범위는 [realtime/README.md](kiwoom_autotrader/realtime/README.md)를 참고하세요.

자동주문은 `automated_orders(tracker, 전략명)` 문맥으로 기존 주문 함수를 호출합니다. `gdcrs`, `ddcrs`, 예약 주문의 전략 태그를 사전 의도·접수번호·실제 체결에 연결합니다. `decision_evidence.py`의 검증을 통과한 당시 판단 근거는 주문 의도와 같은 트랜잭션에서 HTTP 전송 전에 저장합니다. 전체 후보 평가 이력이나 전략 적중률의 분모를 수집하는 기능은 아닙니다. 세부 필드와 한계는 [DECISION_EVIDENCE.md](docs/DECISION_EVIDENCE.md)에 있습니다.

체결 추적은 약 10초 주기의 단계별 조회로 실제 체결과 보유 잔고를 대조합니다. 잔고 차이만으로 체결 가격·시각을 만들지 않습니다. CSV의 남은수량은 조회 시각이 붙은 관측 보유수량이며 주문 미체결수량과 다릅니다. 장후 결산은 해당 CSV의 실제 체결 행을 기준으로 합산하며, 비용과 귀속 범위를 검증할 수 없는 순손익은 `미확정`으로 남깁니다.

## 실행 데이터와 인증정보

`utils.paths.app_data_root()`의 기본값은 macOS에서 `~/Library/Application Support/KiwoomAutotrader`, Windows에서 `%LOCALAPPDATA%\KiwoomAutotrader`입니다. 현재 macOS 사용자 기준 경로는 `/Users/tr-oh/Library/Application Support/KiwoomAutotrader`입니다.

macOS LaunchAgent는 Documents의 프로젝트를 직접 실행하지 않고 아래 `runtime/`에 복사한 패키지를 실행합니다. 봇과 개인 계정 수집기가 이 소스 복사본을 공유합니다.

```text
~/Library/Application Support/KiwoomAutotrader/
  runtime/
    kiwoom_autotrader/             자동 실행용 패키지 복사본
    logs/
      .execution-ledger.sqlite3    자동주문 의도·근거·접수·체결·비용 원장
      YYYYMMDD.csv                 실제 자동체결과 일일 결산
  config/data/
    settings.json                 일반 실행 설정
    reservation-state.json        예약의 날짜별 실행 표식
    reservation-state.json.lock   예약 상태 저장 잠금
  http-rate.json                  프로세스 간 HTTP 전송 제한 상태
  telegram/
    state.json                    봇 업데이트 처리 위치·실행 상태
    listener.lock                 봇 수신 프로세스 잠금
    stop.request                  외부 종료 요청
    listener.log                  LaunchAgent의 표준 출력·오류
    gdcrs-order-markers.sqlite3    골든크로스 주문 시도 중복 방지
    ddcrs-order-markers.sqlite3    데드크로스 주문 시도 중복 방지
  telegram-account/
    channels-<account_id>.sqlite3  선택 채널과 글의 관측 버전
    collector-status.json         수집 상태
    collector.lock                수집 프로세스 잠금
    collector-stop.request        수집 종료 요청
    collector.log                 수집 LaunchAgent의 출력·오류
    daily/
      YYYY-MM-DD/messages.jsonl   게시일별 관측 이력
      daily_factors.csv           일별 팩터
      symbol_factors.csv          종목별 팩터
      manifest.json               내보내기 기준·메타데이터
  python-deps/                    개인 계정 연결용 선택 의존성
```

위 파일은 기능을 사용할 때 생성됩니다. 운영 DB·로그가 프로젝트에 항상 존재한다는 뜻은 아닙니다. `ExecutionTracker`의 기본 원장 경로는 **실행 중인 패키지가 들어 있는 루트의 `logs/`**입니다. 따라서 프로젝트 소스로 실행하면 `ai-stock/logs/`, 배포 복사본으로 실행하면 `KiwoomAutotrader/runtime/logs/`를 사용합니다. 교차 주문 표식은 이 원장 폴더가 아닌 봇의 `--state-dir`에 저장됩니다.

봇 상태는 자동 실행 설치 전 기본적으로 프로젝트 `runs/telegram/`를 사용하며, 설치된 봇 LaunchAgent가 있으면 위 `telegram/` 경로를 사용합니다. 명시한 `--state-dir`이 우선합니다. LaunchAgent 정의는 `~/Library/LaunchAgents/`의 `com.local.kiwoom-autotrader.telegram.plist`와 `com.local.kiwoom-autotrader.telegram-account.plist`입니다.

일반 설정은 패키지의 기본값에 OS 데이터 경로의 `config/data/settings.json`을 덮어 적용합니다. 토큰 갱신 시각·장 시간·채널 조회 간격·일별 내보내기·예약·교차 기간/목록/활성 상태를 저장합니다. 키움 키·봇 연결 정보·개인 계정 세션은 macOS 키체인 또는 Windows 자격 증명 보관소에 저장하며, 발급한 키움 토큰과 모의/실전 구분은 프로세스 메모리에 둡니다. `telegram-console`은 토큰 확인을 위한 대화형 모드이므로 토큰 원문을 터미널에 표시합니다.

수집 SQLite는 글이 바뀐 관측을 버전으로 보존합니다. 내보낸 일별 팩터는 관측·게시·수정 시각을 검사해 미래에 알게 된 내용을 과거에 소급하지 않습니다. CLI `messages-export`의 기본 경로는 실행 중인 소스 루트의 `data/telegram/`이고, 수집기의 자동 내보내기는 위 `telegram-account/daily/`입니다.

## 오프라인 감사·이력·연구

다음 CLI는 `main.py`에서 증권사 연결 경로보다 먼저 별도 패키지로 라우팅합니다.

| 진입점 | 저장·검증 범위 | 상세 문서 |
| --- | --- | --- |
| `python main.py offline-audit …` 또는 `python -m kiwoom_autotrader.diagnostics …` | 지정한 로컬 원장/CSV/상태를 읽어 JSON·정적 HTML 감사 결과 생성 | [OFFLINE_AUDIT.md](docs/OFFLINE_AUDIT.md) |
| `python -m kiwoom_autotrader.history validate/import/report …` | 명시한 canonical bundle 검증, 별도 과거 이력 SQLite 저장, 코호트별 읽기 전용 보고 | [HISTORY_IMPORT.md](docs/HISTORY_IMPORT.md) |
| `python main.py research freeze/audit …` 또는 `python -m kiwoom_autotrader.research …` | 선택한 파일·가설의 SHA와 시각을 고정하고 변경 여부 확인 | [RESEARCH_CONTRACTS.md](docs/RESEARCH_CONTRACTS.md) |
| `python -m kiwoom_autotrader.research single-index --input 입력.json --output 결과.json` | 로컬 수익률 자료의 단일지수 회귀와 사후 조건부 오차 계산 | [SINGLE_INDEX.md](docs/SINGLE_INDEX.md) |

과거 이력은 `ai-stock-history-v1` 계약만 받으며 운영 원장·현재 계좌·전략 설정에 합치지 않습니다. 명시한 별도 DB에 계보·NULL·금액 원문·시각 의미를 보존하고, 재포장 중복·충돌을 검사합니다. `adapters.py`는 추후 변환기 인터페이스로, 잠긴 DMG나 추정한 과거 DB 스키마를 읽는 구현은 없습니다.

연구 계약은 봉 시각의 START/END 의미·관측/정정 시각과 시간순 검증 창의 purge/embargo를 다룹니다. 해시 일치나 오프라인 감사 결과를 실제 계좌 성과·전략 수익성·배포 승인으로 해석하지 않습니다.

`demo`/`simulate`는 기본적으로 **작업 디렉토리**의 새 `runs/…` 폴더에 `result.json`, `config.json`, `bars.csv`, `signals.csv`, `fills.csv`, `rejections.csv`, `evidence-manifest.json`을 저장합니다. `--output`으로 바꿀 수 있습니다. 시뮬레이션은 다음 관측 봉 시가의 전량 가상체결과 비용 가정을 사용하며 실제 부분체결·호가·유동성을 재현하지 않습니다. 실행 후 생성한 manifest는 사전 등록된 가설이나 증권사 체결 증명이 아닙니다.

## 실행 도우미와 검증 범위

Python 요구 버전은 3.11 이상입니다. `start.command`는 프로젝트 폴더로 이동한 뒤 사용 가능한 Python을 선택합니다. `start.bat`는 프로젝트의 `.venv`를 만들거나 재사용하고, 매 실행 시 `requirements.txt`를 설치한 뒤 `main.py`를 실행합니다. Windows 서비스나 작업 스케줄러 설치 기능은 포함하지 않습니다. macOS LaunchAgent 명령은 Windows에서 사용할 수 없습니다.

`requirements.txt`는 openpyxl, Telethon 1.45.0, qrcode 8.2, websockets 15.0.1과 Windows용 keyring/tzdata 조건부 의존성을 정의합니다. TR 문서 변환만 필요하면 `requirements-tr-docs.txt`를 사용합니다. 변환 실행 도우미는 누락 의존성을 자동 설치하지 않습니다.

루트 배포 도우미 `deploy_handoff_runtime.py`는 `--check-only`로 배포 전 점검, `--deploy`로 런타임 반영을 요청하는 별도 인터페이스입니다. 구현·적용 결과는 해당 도구의 검증 결과로 확인해야 하며, 이 구조 문서의 갱신 자체는 도구 실행이나 배포 성공을 의미하지 않습니다.

`tests/`는 HTTP·Telegram·시계·자격증명 경계를 대체하는 회귀 테스트와 임시 SQLite/CSV 검증을 포함합니다. `test_cross_end_to_end.py`의 재현 결과는 합성 신호·외부 경계 대역을 사용한 결과입니다. `test_windows_support.py`는 Windows 경계의 **모킹 테스트와 실행 파일 텍스트 점검**이며 실제 Windows 호스트에서 실행한 검증이 아닙니다. 실제 서버 연결·실제 모의체결·실전 주문·Windows 실행 여부는 각 실행 기록과 환경을 구분해 확인해야 합니다.

모의 연구 공유 통로는 `kiwoom_autotrader/research/shared_server.py`에 있습니다. 수신기 세션을 메모리 안에서만 사용하며 동일 사용자 소켓을 통해 허용 목록의 읽기 전용 TR만 조회합니다. 기존 단일 HTTP 대기열과 전역 제한은 유지되며 연구 요청은 낮은 우선순위입니다. 연구 클라이언트는 별도 `ai-stock-research/research_ipc_client.py`이며 분봉 `ka10080`과 일봉 `ka10081`을 기존 허용 경로로 읽습니다. 공유 연결 오류 시 별도 로그인을 시도하지 않습니다.

`ai-stock-research/paper_experiment_postclose.py`는 공식 마감·현재 거래일 15:40 이후·SSD 마운트·당일 1회 claim을 검증해 최초 관측 후보 전체의 분봉 및 최소 22행 일봉을 저장합니다. `paper_experiment_report.py`는 운영 DB를 읽기 전용으로 대사하고, 실제 비용 귀속 손익과 신호의 +5/15/30/60분·종가·익일 시가 가상 경로를 구분합니다. `paper_experiment_statistics.py`는 2,000회 종목·진입일 군집 90% 구간과 실제 arch SPA, 독립 CSCV PBO를 제공합니다. 비용·공통 관측 격자·변형 전체 목록이 없으면 미평가이며 0으로 채우지 않습니다. arch는 별도 연구 환경의 선택 의존성이고 운영 앱/Windows 의존성을 바꾸지 않습니다. HTML·승인된 주간 Telegram 게시자는 보고 CLI와 별개이며 원문 SHA·중복 방지·500KB 페이지 예산을 확인합니다.


## 장후 외부 점검 후 소스 경계

- `trading/real_automation_safety.py`: 모의 실험과 독립된 실전 자동매수 원금·일일 횟수·쿨다운,
  신선한 계좌·미체결·00 증명 및 전송 직전 검사. 실전 소유권 어댑터가 없는 ddcrs는 보류합니다.
- `cross_monitor.py`: ddcrs 대상은 브로커 매도가능량과 원장 입증 자동 소유량의 교집합입니다.
  미확인 소유는 상태·알림으로 남기며 수동 장기 보유를 자동매도하지 않습니다.
- `listener.py`의 레거시 감독기: gdcrs와 독립된 TTL, 최대 분당 한 번의 공용 세션 대사,
  명시적 `multi resume` 요청 처리. 새 네트워크·토큰 경로를 만들지 않습니다.
- `execution_log.py`: 전일 소멸 잔량·공백 후 소유 재검증을 모든 모의 경로에 적용하며,
  검증된 B3 청산 완료 lot와 추가매수 증거 부족을 구분합니다. 체결·gap·취소 이력은 불변입니다.
  비투영 반복 스냅샷은 이름만으로 제외하지 않고 SHA·키·경제값을 검사합니다.
- `unfilled_buy_ttl.py` / `cancellation_settlement.py`: 확인된 미전송만 불변 순번 재시도 가능하며,
  불확실 전송·ACK만으로 재전송하거나 예약금을 해제하지 않습니다.
- `utils/command_scheduler.py`: 주문 예약은 저장된 공식 현재 세션의 실제 시간 안에서만 실행합니다.
  일반 조회 예약과 구분하며 휴장/지연 개장 자료 부족을 평일 추측으로 대체하지 않습니다.
- 연구 `official_session_evidence.py`: 저장된 Toss 공식 KR 캘린더와 작업자 claim의 해시·시각을
  검증해 장후 수집/보고/주간 게시의 공통 세션 계약으로 변환합니다. 기존 KRX 원문 계약도 유지합니다.
- 연구 `paper_experiment_report.py` / `surge_arena_runtime_events.py`: 실제 producer 스키마,
  레거시/EXP 신호의 서로 다른 시각 계약, 고유 00 및 비용 귀속을 구분합니다. 정확한 레거시
  신호 기준이 없으면 관측 건수만 표시하며 native 성과로 합치지 않습니다. 표시 순서는 결정적입니다.

메모리의 broker 페이지 캐시는 한 날짜만 유지합니다. 기존 raw 이력은 보존하며 새 동일 REST
본문은 원본+불변 반복 관측 증명으로 저장합니다. 재조회 시각·페이지·해시를 검증한 TEMP evidence view로
대사/보고 독자가 읽습니다. SQLite WAL 전환은 기존 운영 원장에 적용하지 않습니다.
이 문서와 소스 수정은 runtime 재배포를 뜻하지 않습니다.

## 2026-11-03 소스 전용 보완

- `trading/broker_exit_only.py`: 정확한 체결 귀속과 분리한 모의 보호 청산 수량 증명.
  `execution_log.py`의 schema7 예약·전송 검사와 `paper_reconciliation.py`의 통합 재대사로 소비합니다.
- `trading/broker_observation_repeats.py`: 원본 REST와 작은 반복 관측 기록의 해시·범위·시각 결합.
  `00`은 합치지 않으며 연구 보고와 매매일지의 읽기 전용 독자도 동일 증명을 검증합니다.
- `trading/closed_ledger_calendar.py`: 로컬 공식 달력으로 과거 완료일 재조회 간격·7세션 경과 판단.
- `trading/tracker_diagnostics.py`: tracker 단계 시간·처리량·RSS의 제한된 진단과 소유 스레드
  상태 게시 callback. 금융 원문과 손익·예약 상태는 변경하지 않습니다. 보호창·장중 당일 작업,
  `limit_blocked`와 단계 게시의 계약은 [CLOSED_RECONCILIATION_PERFORMANCE.md](docs/CLOSED_RECONCILIATION_PERFORMANCE.md)를 따릅니다.
- `trading/paper_prior_day_recovery.py`: 기존 인증 세션으로 D+1 개장·장후 각 1회 이전 잔량 대사.
  영속 claim과 실패 결과를 보존하며 새 로그인·주문·불확실 조회 재시도를 만들지 않습니다.
- `trading/legacy_resume_gate.py`: 모의 실험 해제 후 레거시 자동루프 재개에 현재 설정 기반 명시 확인.
- `trading/paper_experiment_volume_history.py`: SSD의 실제 14세션 동일 5분 거래량을 검증하고 결측 보존.
- `trading/paper_experiment_selection_audit.py`: 실제 08:59/09:00/09:05/15:15 선정·입력 진단 checkpoint.
  연구 `paper_experiment_input_audit.py`는 공식 장후에 이를 읽고 미계측 과거 입력을 미관측으로 보고합니다.
- WORK `readonly_worker_restart_templates.py` / `readonly_worker_halt_supervisor.py`: 비정상 종료 재시작
  제안 템플릿과 후보 작업자의 HALT 보존. 설치·launchctl 적용은 별도 승인 전까지 수행하지 않습니다.
- `telegram/collector.py` / `main.py`: 계정 수집의 KST 시각·실패 단계·비밀값 없는 오류 분류.
- WORK `isolated_app_tests.py`: 실행 당일 KST 보고 경로와 전체 소스 전후 manifest를 보존합니다.

운영 모의 14개 arm과 설정은 변경하지 않습니다. 새 보호 청산 경로는 귀속 미확정 상태를 유지하며
실전 자동매도 어댑터를 대신하지 않습니다. 저장 방식 변경 후 구버전으로 단순 롤백할 수 없으므로
배포 계획은 [LEDGER_STORAGE_MIGRATION_PLAN.md](docs/LEDGER_STORAGE_MIGRATION_PLAN.md)를 따릅니다.

## 추가 외부 점검 후속 소스

- `utils/runtime_deployment_guard.py`: 사용자 코드 배포 승인·정확한 manifest·공식 세션을 결합하여
  첫 운영 변경 전과 runtime 교체 직전에 재검증합니다. APP/WORK 지원 배포기가 함께 사용합니다.
- `realtime/execution_stream.py`, `telegram/receiver_health.py`, `telegram/listener.py`:
  재개 저장 실패 재시도·오류 통지·만료 마커·공식 장전 스트림 대사 연결입니다.
- `telegram/collector.py`: 동일 인스턴스의 제한된 지수 백오프와 KST 실패 진단입니다.
- `trading/paper_experiment_rules.py`, `paper_experiment_store.py`: 새 배정의 실제 R/A 최소 비율을
  원자적으로 검사합니다. 기존 날짜의 최초 배정은 수정하지 않습니다.
- `trading/paper_experiment_theme.py`: 배정과 신호가 같은 기존 테마 원천·버전·관측시각을 사용합니다.
- 연구 `execution_code_versions.py`: 원래 manifest와 원장 수명 이벤트의 SHA를 검증한 코드 경계를
  보고서에 부가합니다. 체결·배정·손익 원문을 수정하지 않으며 교체 공백은 미관측으로 둡니다.
- WORK `backup_retention_policy.py`: 7일 경과 재시작 백업의 검토 계획과 월별 요약입니다.
  실제 삭제는 해시 결합 계획의 별도 승인 후에만 가능하며 배포 전·미분류·참조 중 백업은 보존합니다.
- WORK `test_retired_deployment_entrypoints.py`: 폐기한 일회성 배포 CLI의 첫 변경 전 거부를
  AST로 분리한 격리 fixture에서 검증합니다. 운영 배포 스크립트를 시험 실행하지 않습니다.

## 통합 안정·처리량 요청의 소스 역할

- `utils/control_audit.py`: 등록된 설정·서비스·배포 동작의 공유 잠금 해시체인과 보호 파일/PID 증인 검증.
  `config/settings.py`, 서비스 제어 및 지원 배포 경로가 변경 전·후 기록을 연결합니다.
- `trading/stability_metrics.py` / `stability_audit.py`: 실제 수신기 pulse·신호 원문·제어 감사로 일별 KPI와
  연속10공식세션을 재계산합니다. 관측 누락은 미확정이며 테스트의 합성 성공을 운영 기록으로 쓰지 않습니다.
- `config/paper_hot_policy.py`: 별도 승인된 한정 정책의 다음 주기 snapshot과 전송 직전 epoch 검사.
  금융 한도·신규 arm·100/80 확대를 기존 v1 어댑터로 우회하지 않습니다.
- `trading/paper_experiment_throughput_proposal.py`: 비활성 미래 v2 프로토타입. 실행 주문 어댑터와
  연결되지 않았고 `can_submit=false`이며 현재14개 arm·40보유는 유지합니다.
- `paper_experiment_monitor.py` / `paper_experiment_store.py`: 실제 native evaluator 반환의 입력/시간/기술 단계별
  불변 평가 receipt. 같은 경계는 보고 분모에서1회이고 보유 청산·S5 관측 전용은 분리합니다.
- 연구 `paper_experiment_funnel.py`: 실제 receipt·완전 선정 스냅샷·신호·의도·접수·고유00를 arm별로 구분.
  과거에 계측하지 않은 평가 수는 미확정입니다.
- 연구 `weekly_theme_taxonomy.py`: 공식 장후 저장 페이지의 테마/구성종목/업종 후보 병합과 출처 해시.
  실제 집중 한도 reader와 같은 family 의미를 사용하며 운영 분류표를 교체하거나 위험을 해제하지 않습니다.
- WORK `stability_watchdog.py`: 단일 읽기 전용 감시기와 승인 후 사용할 알림 어댑터·체크포인트 템플릿.
  실제 설치·발송·재시작은 수행하지 않았습니다. 자세한 단계는
  [STABILITY_THROUGHPUT_ROADMAP.md](docs/STABILITY_THROUGHPUT_ROADMAP.md)를 따릅니다.

## 장후 전조·야간 메시지 연구 (주문 없음)

- 연구 `surge_precursor*.py`: 저장 분봉의 onset·선행 특징·날짜 봉인·비용 후 가상 경로와 재현.
  당시 모집단·공식 선행14세션·대조군이 없으면 전체 시장 lift와 OOS는 미평가입니다.
- 연구 `telegram_channel_study.py`, `telegram_nightly_gold.py`: 채널별 자료 적격성과
  사용자 검토가 필요한300개 초안(개발150/봉인150). 현재 마스터·후수집 글을 과거 정보로 바꾸지 않습니다.
- 연구 `telegram_nightly/`: 독립7단계 분류와 원문 RO 읽기·공식 세션·claim·SSD 출력·후속 평가.
  WORK `telegram_nightly_worker.py`와 plist는 설치 전 검토본이며 첫 실행은 별도 preview 파일만 만듭니다.
- 연구 `paper_experiment_report.py` / `paper_experiment_funnel.py`: 별도 closing 선정의
  검증된 보조배정과 실제 신호를 집계합니다. 후행 경매예상가로 최초 미관측 신호가격을 채우지 않습니다.
- 사용 순서와 한계는 별도 연구 폴더의 `PRECURSOR_NIGHTLY_WORKFLOW.md` 및 생성 보고서를 따릅니다.
  기존 장중 피드·실험 arm·안전 한도를 변경하지 않으며 배포·설치·실제 발송은 별도 승인 대상입니다.
