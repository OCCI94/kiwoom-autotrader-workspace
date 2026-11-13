# 프로젝트 구조 가이드

프로젝트 최상위의 `main.py`에서 시작해 `kiwoom_autotrader/main.py`가 실행 명령과 프로그램의
생명주기를 관리합니다. 사용자가 제시한 역할별 디렉토리는 Python 패키지
**`kiwoom_autotrader/` 아래**에 배치합니다. TR 원문 명세와 테스트는 프로젝트 최상위에 둡니다.

```text
kiwoom-autotrader/
  main.py                         실행 진입점
  start.command                   텔레그램 상시 수신 시작
  console.command                 대화형 터미널 수신·토큰 확인
  setup-*.command                 키움·봇 키 설정
  telegram-*.command              연결·자동 실행·중지 도우미
  kiwoom_autotrader/
    __main__.py                   python -m 진입점
    main.py                       CLI와 기능 연결
    api/                          키움 REST 호출과 인증 상태
      au10001.py                  접근토큰 요청 명세·요청 구성
      ka10001.py                  국내 종목 기본 정보
      ka10032.py                  국내 거래대금 순위
      ka10099.py                  종목 분류 조회
      kt00001.py                  예수금 상세 현황
      kt00018.py                  계좌 평가·보유 내역
      kt10000.py                  국내 매수 주문
      kt10001.py                  국내 매도 주문
      usa20100.py                 미국 현재가
      client.py                   공통 모의 조회 클라이언트
      session.py                  모의·실전 로그인, 메모리 토큰, 예약 갱신
      stock_info.py               현재 로그인으로 국내 종목 기본 정보 조회
      stock_names.py              정확한 코스피·코스닥 종목명에서 코드 조회
      account_report.py           자금·보유 내역의 연속조회와 검증
      orders.py                   현재 로그인으로 KRX 주문 전송
      equity_rankings.py          일반기업 주식 분류와 순위 결합
    realtime/                     실시간 수신 확장 위치(현재 미구현)
    telegram/                     봇·개인 계정 송수신과 채널 수집
      client.py                   Bot API 통신
      listener.py                 시작 알림·자동 모의 로그인·명령 수신 루프
      commands/                   텔레그램 명령별 처리
        login.py                  login paper/real, login renewal
        search.py                 srch 코드·종목명 조회와 여섯 지표 응답
        orders.py                 buy 코드·종목명 매수, sell 코드 시장가 매도
        report.py                 report/r 자금·보유 내역 응답
        settings.py               mkhr, stts, stts default
        reservations.py           rsv 등록·목록·삭제
        help.py                   help와 전체 명령 안내
        power.py                  power off 종료 처리
        status.py                 모의 연결 상태 확인
        top.py                    일반기업 주식 거래대금 순위
        channels.py               채널 수집 현황·최근 글
      account.py                  개인 계정 인증·계정 확인
      account_setup.py            로컬 연결 화면·채널 선택
      collector.py                과거·새 채널 글의 주기적 수집
      channel_store.py            SQLite 관측 버전·채널·처리 위치
    trading/                      전략과 연구용 팩터
      simulation.py               예제 전략·가상 주문·체결
      message_factors.py          당시 관측 정보만 사용하는 일별 팩터
    utils/                        공통 저장·경로·서비스 도우미
      keychain.py                 모의 키 보관
      real_credentials.py        실전 키 별도 보관
      telegram_service.py        봇 LaunchAgent 관리
      account_service.py         채널 수집 LaunchAgent 관리
      command_scheduler.py       평일 명령 예약·날짜별 실행 표식
      message_dataset.py         일자별 JSONL·CSV·메타데이터 내보내기
    config/
      settings.py                기본값·장 시간·예약 등 실행 설정 읽기와 저장
      data/
        settings-default.json    일반 설정의 기본값
        settings.json            일반 설정용 빈 템플릿
  tr_docs/                        제공받은 Excel 기반 TR 명세·작성 규칙
  tests/                          외부 서비스 대역·임시 파일을 이용한 검증
  config.example.json             오프라인 예제 전략 설정
  data/telegram/                  CLI 메시지 내보내기의 기본 출력 위치
  runs/                          오프라인 실행 결과
```

기존 평면 모듈(`cli.py`, `kiwoom.py`, `telegram_listener.py` 등)은 이전 import 경로를
유지하는 호환 연결 파일입니다. 새 기능은 역할에 맞는 하위 디렉토리에 구현합니다.
TR 명세의 기준과 상세 작성 규칙은 [tr_docs/README.md](tr_docs/README.md)를 따릅니다.

## 실행 데이터와 인증정보

자동 실행은 Documents의 프로젝트를 직접 실행하지 않고 아래 위치에 복사한 소스를
사용합니다. 소스 갱신 시 설치 명령을 다시 실행하며 기존 설정과 수집 DB는 유지합니다.

```text
~/Library/Application Support/KiwoomAutotrader/
  runtime/                       자동 실행용 소스 복사본
  config/data/settings.json       실행 중 변경하는 일반 설정
  config/data/reservation-state.json  예약의 날짜별 실행 표식
  telegram/                      봇 수신 처리 위치·상태·로그
  telegram-account/              계정별 채널 SQLite·수집 상태·로그
    daily/                       자동 생성하는 일자별 메시지·팩터
  python-deps/                   개인 계정 연결용 의존성
```

자동 실행 전의 봇 수신 상태는 프로젝트의 `runs/telegram/`에 둘 수 있으며 첫 설치 때
실행용 상태 경로로 옮깁니다. LaunchAgent 설정은 `~/Library/LaunchAgents/`의
`com.local.kiwoom-autotrader.telegram.plist`와
`com.local.kiwoom-autotrader.telegram-account.plist`입니다.

`settings-default.json`의 기본값에 실행 경로의 `settings.json`을 덮어 적용합니다.
토큰 갱신 시각·장 시작/종료·조회 간격·내보내기 여부·예약 같은 일반 설정을 JSON에 보관합니다.
키움 키, 봇 연결 정보, 개인 계정 로그인 세션은 macOS 키체인에, 발급한 키움 토큰과
모의·실전 구분은 실행 프로세스 메모리에 보관합니다.
수신 프로세스가 시작되면 시작 알림과 자동 모의 로그인을 수행하고, 성공 후 예약 갱신을
시작합니다. 이전 실행의 실전 모드는 복원하지 않으며 실행 중 `login real`로 전환합니다.

`srch`, `report`/`r`, `buy`, `sell`은 현재 로그인 구분과 토큰을 사용합니다.
주문은 KRX로 요청하고 접수 결과를 응답하며 체결 추적은 후속 구현 범위입니다.
`mkhr`는 장 시간 설정을 저장하고, `stts`는 설정 전체를 표시합니다. `stts default`는
설정·예약을 초기화하고 메모리의 토큰 갱신 시각도 08:55로 바꿉니다.

`rsv H:MM 명령` 예약은 설정 JSON의 `reservations`에 저장합니다. 한국 시간 평일마다
수신 루프에서 실행하며 주말은 제외합니다. 거래소 휴장일 달력은 아직 사용하지 않습니다.
작업 전에 날짜별 실행 표식을 기록해 하루에 한 번만 시도하며, 재시작 전에 지난 시각의
예약이나 전송 결과가 불확실한 작업은 자동 재시도하지 않습니다.

SQLite는 수집 글의 원본 저장소이며 변경을 관측할 때마다 버전을 남깁니다.
`daily/YYYY-MM-DD/messages.jsonl`은 한국 시간 게시일로 정리한 관측 이력입니다.
`daily_factors.csv`와 `symbol_factors.csv`는 관측·게시·수정 시각을 기준으로 당시 알 수 있었던
정보만 집계합니다. 자세한 날짜 기준과 데이터 한계는 [README의 시계열 데이터 안내](README.md#일자별-메시지-데이터와-시계열-팩터)를 참고하세요.
