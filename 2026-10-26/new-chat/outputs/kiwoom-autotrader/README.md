# 키움 자동매매 프로그램

> 보존용 구버전 사본입니다. 최신 개발·배포 소스는 [../ai-stock/](../ai-stock/), 최신 배포본은 [../ai-stock-windows.zip](../ai-stock-windows.zip)입니다.

Mac에서 텔레그램 명령을 계속 수신하며 키움 인증, 종목·계좌 조회, 매수·매도 주문,
명령 예약과 채널 글 수집을 수행하는 Python 프로젝트입니다. 모의·실전 로그인과 토큰 갱신,
일자별 메시지 팩터, 오프라인 예제 전략을 지원합니다. 주문 명령은 현재 로그인한 환경의
키움 서버에 전송합니다.

## 실행과 최초 설정

설정이 완료된 Mac에서는 **`start.command`를 더블클릭하면 텔레그램 명령 수신을 시작**합니다.
이미 자동 실행을 설치했다면 백그라운드 수신기를 시작하며 중복 실행하지 않습니다.
설치 전에는 실행한 터미널에서 계속 수신합니다.

수신 프로세스가 시작되면 텔레그램으로 시작을 알리고 저장된 모의 키로 `login paper`를
자동 실행합니다. 결과를 텔레그램과 터미널에 보고하고, 성공하면 일일 토큰 갱신을 예약한 뒤
명령을 계속 기다립니다. 시작 모드는 모의투자이며 실전은 실행 중 `login real`로 전환합니다.

| 실행 파일 | 용도 |
| --- | --- |
| `setup-mock.command` | 모의투자 App Key·App Secret 인증 후 키체인에 저장 |
| `setup-real.command` | 실전투자 App Key·App Secret을 별도 키체인 항목에 저장 |
| `setup-telegram.command` | BotFather 봇과 본인 개인 채팅 연결 |
| `telegram-autostart-install.command` | Mac 로그인 시 봇 수신 자동 실행 등록·시작 |
| `start.command`, `telegram-listen.command` | 텔레그램 명령 수신 시작 |
| `console.command` | 터미널에서 수신하며 로그인·갱신 결과와 토큰 확인 |
| `connect-mock.command` | 모의 인증 후 국내 삼성전자·미국 애플 시세를 한 번 조회 |
| `telegram-account-setup.command` | 개인 텔레그램 계정 연결·수집 채널 선택 |

처음 사용할 때는 모의 키 저장 → 봇 연결 → 자동 실행 설치 순서로 진행합니다.
키 입력은 숨김 방식이므로 입력한 글자가 보이지 않아도 붙여넣고 Enter를 누르면 됩니다.
실전 로그인은 `setup-real.command`에서 실전 키를 따로 등록한 뒤 사용할 수 있습니다.

모의·실전 키는 각각 `com.local.kiwoom-autotrader.mock`,
`com.local.kiwoom-autotrader.real` 키체인 서비스에 저장합니다. 새 키의 인증에 성공한 뒤
저장하므로 실패한 입력으로 기존 키를 덮어쓰지 않습니다. 키를 설정 JSON이나 명령줄 인자에
기록하지 않습니다. 키체인 접근이 필요하면 macOS의 안내를 따릅니다.

프로젝트 폴더에서 Python 3.11 이상으로 실행할 수도 있습니다.

```sh
# 하위 명령을 생략하면 텔레그램 수신 시작
python3 -m kiwoom_autotrader
python3 main.py

# 모의 키 저장 여부 확인 또는 등록
python3 -m kiwoom_autotrader credentials-status
python3 -m kiwoom_autotrader credentials-setup

# 실전 키 저장 여부 확인 또는 등록
python3 -m kiwoom_autotrader credentials-real-status
python3 -m kiwoom_autotrader credentials-real-setup

# 모의 인증·국내/미국 시세를 한 번 조회
python3 -m kiwoom_autotrader connect
```

현재 Mac의 실행 파일은 Codex 번들 Python을 우선 사용합니다. `python3`가 없다면 아래
경로를 대신 사용할 수 있습니다.

```sh
"$HOME/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3" -m kiwoom_autotrader
```

## 텔레그램 명령

`setup-telegram.command`에서 봇 토큰을 입력하고 연결 링크를 열어 본인 Telegram에서
`시작(Start)`을 누릅니다. 일회용 연결 코드가 일치하는 개인 채팅을 등록하고 확인 메시지를
한 번 보냅니다. 봇 토큰과 채팅 정보는 `com.local.kiwoom-autotrader.telegram` 키체인
항목에 저장합니다. 이 봇 연결에는 개인 계정의 로그인 코드나 비밀번호를 사용하지 않습니다.

| 텔레그램 명령 | 동작 |
| --- | --- |
| `login paper` | 모의투자 서버에서 토큰을 발급받아 로그인 |
| `login real` | 실전투자 서버에서 토큰을 발급받아 로그인 |
| `login renewal 8:50` | 매일 토큰 갱신 시각을 한국 시간 08:50으로 변경·저장 |
| `srch {종목코드 또는 종목명}` | 현재가·거래량·시가·고가·저가·전일대비 조회 |
| `buy {종목코드 또는 종목명} {수량} [지정가]` | KRX 매수. 지정가를 생략하면 시장가 |
| `sell {종목코드} {수량}` | KRX 시장가 매도 |
| `report`, `r` | 국내 계좌 자금 현황과 보유 내역 조회 |
| `mkhr {시작시각} {종료시각}` | 장 시작·종료 시각 설정. 기본 09:00~15:30 |
| `stts` | 예약을 포함한 현재 설정 전체를 JSON으로 표시 |
| `stts default` | 설정을 기본값으로 되돌리고 등록한 예약 삭제 |
| `rsv {시각} {명령}` | 한국 시간 기준 평일 매일 실행할 명령 등록 |
| `rsv list` | 등록된 예약의 ID·시각·명령 확인 |
| `rsv remove {ID}` | 해당 예약 삭제 |
| `power off` | 종료 응답 후 명령 수신과 채널 수집 종료 |
| `help`, `/help`, `/start`, `도움말` | 모든 명령과 사용법 안내 |
| `status`, `/status`, `상태` | 모의투자 인증과 국내·미국 시세 연결 확인 |
| `top`, `/top`, `상위20`, `거래대금` | 코스피·코스닥 일반기업 주식 거래대금 상위 20종목 |
| `channels`, `/channels`, `채널` | 선택 채널과 수집 현황 |
| `posts`, `/posts`, `채널글` | 수집한 최근 채널 글 5개의 미리보기·원문 링크 |

명령의 앞뒤 공백을 제거하고 영문 대소문자를 구분하지 않습니다.
예를 들어 `  LOGIN PAPER  `도 `login paper`와 같습니다. 등록된 개인 채팅에서
본인이 보낸 새 명령만 처리하며 임의의 코드나 채널 글을 명령으로 실행하지 않습니다.

`login paper`는 `https://mockapi.kiwoom.com`, `login real`은
`https://api.kiwoom.com`에서 토큰을 받습니다. 발급 결과와 투자 구분은 텔레그램과
터미널에 보고하고, 토큰과 현재 투자 구분은 수신 프로세스의 메모리에 보관합니다.
로그인 전환이 실패하면 기존 로그인 상태를 유지합니다. 기존 토큰이 이미 만료됐다면
재로그인이 필요하다고 알리고 조회·주문에 사용하지 않습니다.

`au10001` 응답의 필수 만료일(`expires_dt`) 누락·잘못된 형식·이미 지난 만료일은 인증
성공으로 처리하지 않습니다. 문서에는 만료일의 시간대가 없으므로 이 프로그램은 한국
시간으로 해석합니다. 로그인 응답이 예약 시각을 지나 도착해도 즉시 중복 갱신하지 않도록
다음 갱신은 인증 완료 시각을 기준으로 정합니다.

로그인 후에는 **매일 08:55(Asia/Seoul)**에 같은 투자 구분으로 토큰을 갱신합니다.
`login renewal H:MM` 또는 `login renewal HH:MM`으로 00:00~23:59 중 원하는 시각을
설정할 수 있습니다. 이 설정은 재시작 후에도 유지됩니다. 갱신 성공·실패는 텔레그램과
터미널에 보고합니다. 예약 갱신 실패 시 반복 요청하지 않으며 `login paper` 또는
`login real`을 다시 보내 수동으로 재시도할 수 있습니다.

토큰 원문을 보려면 `console.command`를 실행한 터미널에서 명령 수신을 유지한 채
Telegram으로 로그인 명령을 보냅니다. 대화형 터미널에는 성공 여부와 토큰을 출력하며,
백그라운드 로그나 파일로 리다이렉트한 출력에서는 토큰을 가립니다. 텔레그램 응답에는
토큰 원문을 넣지 않습니다. 콘솔 모드는 기존 백그라운드 봇 수신기를 먼저 중지합니다.

수신기를 재시작하면 이전 토큰·투자 구분은 사라지고 저장된 모의 키로 새 로그인을 시도합니다.
이전에 실전으로 전환했더라도 다시 시작할 때는 모의투자로 로그인합니다.
현재 `/status`, `/top`, `connect`, `mock-quote`는 별도의 모의 조회 기능입니다.
`srch`, `report`/`r`, `buy`, `sell`은 현재 메모리의 로그인 구분과 토큰을 사용합니다.
`login real`로 전환한 뒤의 `buy`·`sell`은 실전 주문입니다.

`/top`은 종목 분류를 이용해 ETF·ETN 등을 제외하고 우선주는 포함합니다. KRX·NXT 통합,
관리종목 포함 기준이며 거래대금은 억원으로 표시합니다. 분류와 순위 조회에 몇 초 걸릴 수 있습니다.

## 종목 조회·주문·계좌 보고서

`srch 005930` 또는 `srch 삼성전자`로 국내 종목을 조회합니다. 응답은 종목명·코드와 함께
현재가, 거래량, 시가, 고가, 저가, 전일대비의 여섯 지표를 이모지로 구분해 보여줍니다.
종목명은 코스피·코스닥 목록에서 공백과 영문 대소문자를 정규화해 정확히 일치하는 이름을
찾습니다. 부분 일치나 같은 이름의 여러 종목이 있으면 후보를 안내하고 임의로 선택하지 않습니다.
처음 이름으로 조회할 때는 목록을 가져오는 시간이 추가로 걸릴 수 있습니다.

매수는 종목코드 또는 종목명을, 매도는 6자리 종목코드를 받습니다. 공백을 포함한 매수
종목명은 따옴표로 묶습니다. 수량과 지정가는 12자리 이내 양의 정수로 입력합니다.

```text
buy 005930 1
buy 삼성전자 1 70000
sell 005930 1
```

매수 지정가를 생략하면 시장가(`trde_tp=3`), 가격을 입력하면 보통 지정가(`trde_tp=0`)로
요청합니다. 매도는 시장가입니다. 주문 거래소는 KRX이며 응답에는 모의·실전 구분, 종목,
수량, 주문 유형과 주문번호를 표시합니다. 주문 접수는 체결 완료를 뜻하지 않습니다.
전송 후 결과가 불확실하면 자동으로 같은 주문을 다시 보내지 않으므로 증권사 주문내역에서
접수 여부를 확인합니다.

`report` 또는 `r`은 예수금, 출금가능금액, 주문가능금액, D+1/D+2 추정예수금과
총매입·총평가·평가손익·추정예탁자산을 보여줍니다. 보유 내역에는 종목명·코드, 수량,
매매가능수량, 매입가·현재가, 평가금액과 손익을 표시합니다. 계좌번호와 계좌 소유주 이름은
응답에 넣지 않습니다. `kt00001` 추정조회와 `kt00018` 합산조회로 가져온 **국내 계좌·KRX 기준**입니다.

연속조회 결과를 모두 가져온 뒤 보고하며, 합계 금액은 페이지별로 더하지 않습니다.
조회 한도를 넘거나 후속 페이지에 오류가 있으면 전체 내역을 확인하지 못했다고 알립니다.
누락값은 0으로 대체하지 않고 `미제공`으로 표시하며, 명시적인 빈 보유 목록과 구분합니다.
여러 요청으로 조회하므로 완전히 같은 시점의 잔고는 아니며, 긴 내역은 여러 메시지로 나눕니다.

## 장 시간·설정·명령 예약

`mkhr 9:00 15:30`으로 장 시간 설정을 저장합니다. 시작·종료 시각은 00:00~23:59 범위이고
시작이 종료보다 빨라야 합니다. `mkhr`는 시간 설정을 저장하는 기능이며, 이 시간 밖의
주문을 프로그램에서 자동 차단하는 조건은 추가하지 않습니다.

`stts`는 현재 설정 전체와 등록된 예약을 JSON으로 보여줍니다. `stts default`는 모든
일반 설정을 기본값으로 되돌리고 예약을 삭제합니다. 실행 중인 토큰 갱신 시각에도 기본
08:55를 즉시 적용하며, 키체인 인증정보나 수집한 채널 글을 삭제하는 명령은 아닙니다.

예약은 한국 시간의 24시간제로 등록하며 ID로 관리합니다.

```text
rsv 8:55 login paper
rsv 15:20 report
rsv list
rsv remove 1
```

예약 명령은 **월~금 매일** 해당 시각에 실행하고 텔레그램으로 실행을 알립니다.
토요일·일요일은 건너뛰며, 거래소 휴장일 달력은 아직 연결하지 않았으므로 평일 공휴일도
예약 실행 대상입니다. 예약은 프로그램의 수신 루프가 실행 중일 때 동작합니다.
중지·재시작 전에 이미 지난 예약 시각은 나중에 몰아서 실행하지 않습니다.

예약 목록은 실행 설정의 `reservations`에 저장하고, 날짜별 실행 표식은
`config/data/reservation-state.json`에 따로 기록합니다. 작업을 수행하기 전에 표식을 저장하여
같은 예약을 같은 날 중복 실행하지 않습니다. 실행 도중 중단되거나 서버 전송 결과가
불확실해도 해당 예약을 자동 재시도하지 않습니다.

프로그램 시작 또는 예약 등록 시각보다 이미 지난 당일 예약은 다음 평일부터 적용합니다.
예약 안에 다시 `rsv`를 넣을 수 없습니다. 등록할 명령의 세부 인수는 일반 명령과 같은
형식으로 입력하며, 등록할 때와 실행 직전에 검사합니다. 조회·주문 예약에는 실행 시점의
로그인 구분과 토큰을 사용합니다.

## 자동 실행과 종료

`telegram-autostart-install.command`를 한 번 실행하면 즉시 수신을 시작하고, 이후 Mac을
재시작해 **사용자 로그인 시 자동 연결**합니다. 터미널과 Codex를 열어 둘 필요는 없습니다.
네트워크 등으로 비정상 종료하면 macOS가 재실행하며 반복 실행은 30초 간격으로 제한합니다.
Mac이 깨어 있고 인터넷에 연결되어 있어야 하며 로그인 전이나 잠자기 중에는 수신하지 못합니다.

`power off`를 보내면 응답 후 봇 수신과 개인 계정 채널 수집을 종료하고 키움 메모리
로그인을 해제합니다. 정상 종료로 처리하여 즉시 재실행하지 않습니다. Mac 자체를 끄는 명령은
아니며, 설치된 자동 실행은 유지되어 다음 Mac 로그인 때 다시 시작하고 모의 로그인을 시도합니다.
그 전에 다시 사용하려면 봇 수신과 채널 수집을 각각 시작할 수 있습니다.

```sh
python3 -m kiwoom_autotrader telegram-listen
python3 -m kiwoom_autotrader telegram-account-listen
```

`telegram-stop.command`는 봇 수신만, `telegram-account-stop.command`는 채널 수집만
중지합니다. 자동 실행 자체를 해제하려면 각각 `telegram-autostart-remove.command`,
`telegram-account-autostart-remove.command`를 사용합니다. 저장한 키와 채널 기록은 유지됩니다.

실행용 소스와 상태는 `~/Library/Application Support/KiwoomAutotrader/`에 두며
프로젝트 소스를 수정한 후에는 자동 실행 설치를 다시 실행해 실행용 복사본을 갱신합니다.
개인 계정 수집기의 자동 실행 설정은 연결 화면에서 채널을 선택하고 수집을 시작할 때 설치합니다.

```sh
python3 -m kiwoom_autotrader telegram-autostart-install
python3 -m kiwoom_autotrader telegram-autostart-status
python3 -m kiwoom_autotrader telegram-account-status
```

봇 처리 위치는 응답 전에 저장하여 같은 명령이 재시작 후 반복 실행되지 않도록 합니다.
전송 결과가 불확실한 응답은 자동 재전송하지 않습니다. 수신기 시작 전에 보낸 명령은
처리하지 않으므로 다시 연결한 뒤 명령을 보내세요. 다른 수신 프로그램의 기존 웹훅 설정은
자동으로 변경하지 않습니다. 봇 상태 파일에는 메시지 본문이나 토큰을 기록하지 않습니다.

## 개인 계정으로 채널 글 수집

봇과 별개로 개인 Telegram 계정을 연결하면 선택한 방송 채널의 과거 글과 새 글을 수집합니다.
`telegram-account-setup.command`를 실행하면 이 Mac에서 접근하는 연결 화면이 열립니다.

1. [Telegram API development tools](https://my.telegram.org/apps)에서 발급한
   개인 계정용 `api_id`, `api_hash`를 입력합니다.
2. `QR 코드로 연결`을 누르고 휴대폰 Telegram의 설정 → 기기 → 데스크톱 기기 연결에서
   QR 코드를 스캔합니다. 필요하면 2단계 인증 비밀번호를 입력합니다.
3. 전화번호 방식도 지원합니다. 이 경우 국가번호를 포함한 전화번호와 Telegram으로 받은
   숫자 인증코드를 사용합니다.
4. 가입한 채널 목록에서 수집할 채널을 선택하고 `선택한 채널 수집 시작`을 누릅니다.

현재 알림 봇과 연결한 본인 계정만 인증합니다. API 설정과 로그인 세션은
`com.local.kiwoom-autotrader.telegram-account` 키체인 항목에 보관하며 일반 `.session`
파일을 만들지 않습니다. 전화번호·인증코드·비밀번호를 프로젝트 파일이나 로그에 저장하지 않습니다.
개인 대화나 단체방 전체를 읽지 않고 선택한 방송 채널만 수집합니다.

처음에는 최근 100개 글을 저장한 뒤 기본 60초 간격으로 채널별 새 글과 더 오래된 글을
각각 최대 100개씩 이어서 조회합니다. 전체 수집 개수 제한은 없으며 재시작하면 저장한
처리 위치부터 이어갑니다. Telegram의 요청 대기 지시를 따르므로 수집 완료 시간은 달라집니다.
이미 삭제되거나 계정에서 접근할 수 없는 글은 가져오지 못합니다.

본문·캡션·게시/수정 시각·미디어 종류·원문 링크를 저장합니다. 첨부파일은 다운로드하지
않으며 전체 과거 글의 수정·삭제를 지속적으로 대조하는 기능은 아직 없습니다.
주기적인 조회 방식으로 새 글을 확인합니다. 수집 글은 데이터이며 매매 명령으로 실행하거나
다른 채팅으로 자동 전달하지 않습니다.

계정별 SQLite DB는 `~/Library/Application Support/KiwoomAutotrader/telegram-account/`에
권한 0600으로 저장합니다. 채널 선택을 해제하면 이후 수집을 멈추고 이전 기록은 보존합니다.
선택 후 전용 LaunchAgent가 등록되어 Mac 로그인 시 다시 연결합니다.
봇 수신과 채널 수집은 별도 프로세스입니다.

연결 화면은 30분 동안 유효합니다. 닫히거나 만료되면 설정 실행 파일을 다시 여세요.
다른 개발 환경에서는 개인 계정 기능의 의존성을 설치해야 합니다.

```sh
python3 -m pip install '.[telegram-account]'
python3 -m kiwoom_autotrader telegram-account-setup
python3 -m kiwoom_autotrader telegram-account-status
```

공식 참고: [API ID 발급](https://core.telegram.org/api/obtaining_api_id),
[과거 기록 조회](https://core.telegram.org/method/messages.getHistory),
[Telethon 메모리 세션](https://docs.telethon.dev/en/stable/concepts/sessions.html).

## 일자별 메시지 데이터와 시계열 팩터

SQLite를 원본 저장소로 사용하며, 처음 관측한 내용과 이후 관측한 수정 내용을 별도 버전으로
보존합니다. 게시 시각(`published_at`), 수정 시각(`edited_at`), 해당 버전을 수집기가
관측한 시각(`observed_at`), 최초 관측 시각과 출처를 함께 기록합니다.

성공한 수집 주기마다 기본으로 수집 상태 폴더의 `daily/`에 아래 데이터를 내보냅니다.
기본 설치 경로는 `~/Library/Application Support/KiwoomAutotrader/telegram-account/daily/`입니다.

```text
daily/
  2026-10-26/messages.jsonl   게시일이 해당 날짜인 글의 관측 버전
  daily_factors.csv          일별 수집·수정·텍스트·종목코드 후보 통계
  symbol_factors.csv         일별 종목코드 후보별 언급 수
  manifest.json              시간 기준, 출처, 범위, 파일 해시와 한계
```

**원문 폴더는 한국 시간 게시일**, **팩터는 당시 이용 가능했던 정보의 날짜**를 기준으로 합니다.
팩터의 이용 가능 시각은 관측·게시·수정 시각 중 가장 늦은 값입니다. 각 날짜의 한국 시간
23:59:59까지 알려진 버전만 사용하며, 내보내기 시점이 그보다 이르면 해당 시점에서 잘라
부분 일자로 표시합니다. 같은 날 여러 버전을 관측한 글은 해당 시점의 최신 내용으로
텍스트·언급 수를 계산하고, 버전 수는 별도로 기록합니다.

예를 들어 오늘 처음 가져온 지난달 글은 지난달 원문 폴더에 정리되지만, 지난달의
백테스트 팩터에 소급해서 넣지 않습니다. 기존 DB에서 옮긴 글도 원래 관측 시각을 추측하지
않고 기존 스냅샷이라는 출처(`legacy_snapshot`)와 마이그레이션한 시각을 기록합니다.
나중에 발견한 수정 내용이 과거 일자의 팩터를 덮어쓰지 않습니다.

기본 팩터는 관측 글·신규 글·수정 글·버전 수, 관측 채널 수, 과거 게시글의 신규 수집 수,
텍스트 길이, URL 수와 명시적 종목코드 후보의 언급 수입니다. 종목코드 후보는
`KR:005930`, `A005930`, `(005930)`, `종목코드:005930`, `$AAPL` 같은 표시에서 추출합니다.
단순한 6자리 숫자·날짜·가격이나 회사 이름만으로 종목을 추정하지 않습니다.
후보는 종목 마스터와 대조하지 않았으므로 실제 상장 종목 여부가 검증된 데이터는 아닙니다.

관측이 없는 날짜를 0으로 채우지 않으며 전체 채널이나 종목을 빠짐없이 관측했다고 가정하지
않습니다. 선택 해제한 채널의 기존 관측은 연구 기록에 남습니다. 감성 점수, 수익률 라벨,
거래일 정렬과 매매 신호는 아직 만들지 않습니다. 원문은 JSONL에만 기록하고 CSV에는 집계값을
넣습니다. 파일은 0600, 내보내기 폴더는 0700 권한으로 생성하며 변경된 파일만 원자적으로 교체합니다.

프로젝트 폴더에서 수동으로 내보낼 수 있습니다. `--as-of`를 쓰면 그 시점까지 관측한 정보만
사용합니다. 반드시 `+09:00` 또는 `Z`처럼 시간대를 포함하세요.

```sh
python3 -m kiwoom_autotrader messages-export --output data/telegram
python3 -m kiwoom_autotrader messages-export --output data/telegram-asof --as-of 2026-10-26T08:55:00+09:00
```

관측 내용이 그대로이면 기존 파일을 재사용합니다. 실제 데이터 기준 시각과 부분 일자 여부는
`manifest.json`을 확인하세요. SQLite에 당시 관측 기록이 없다면 과거 기준 내보내기에서도
그 시점의 데이터를 만들어내지 않습니다.

## 일반 설정과 프로젝트 구조

소스 기본값은 `kiwoom_autotrader/config/data/settings-default.json`에 있습니다.
같은 폴더의 `settings.json`은 일반 설정의 형식을 위한 빈 템플릿입니다. 실행 중 변경한
설정은 `~/Library/Application Support/KiwoomAutotrader/config/data/settings.json`에 저장하며,
기본값 위에 적용합니다. 키·토큰·계정 로그인 세션은 이 JSON 파일에 넣지 않습니다.

| 설정 | 기본값 | 의미 |
| --- | --- | --- |
| `version` | `1` | 설정 형식 버전 |
| `login_renewal_time` | `08:55` | 키움 토큰 일일 갱신 시각 |
| `timezone` | `Asia/Seoul` | 갱신 및 일자별 데이터의 시간대 |
| `channel_poll_interval_seconds` | `60` | 채널 조회 간격(초) |
| `daily_message_export` | `true` | 수집 후 일자별 데이터 자동 내보내기 |
| `market_start_time` | `09:00` | 장 시작 시각 설정 |
| `market_end_time` | `15:30` | 장 종료 시각 설정 |
| `reservations` | `[]` | 평일 반복 실행할 예약 목록 |
| `next_reservation_id` | `1` | 다음 예약에 사용할 ID |

역할별 소스 디렉토리와 실행 데이터 경로는 [프로젝트 구조 가이드](STRUCTURE.md)를 참고하세요.
키움 TR 명세는 사용자 제공 Excel 원문을 기준으로 하며, 상세한 작성 규칙과 사용 중인 TR은
[tr_docs/README.md](tr_docs/README.md)에 정리했습니다.

## 모의 시세와 원본 순위 조회

아래 명령은 저장한 모의 키를 사용해 인증하고 한 번 조회합니다.

```sh
python3 -m kiwoom_autotrader mock-quote --market KR --symbol 005930 --exchange KRX
python3 -m kiwoom_autotrader mock-quote --market US --symbol AAPL --exchange NASDAQ
python3 -m kiwoom_autotrader top-turnover --market 001 --exchange 3 --limit 20
```

`top-turnover.command`와 CLI `top-turnover`는 키움 원본 순위를 출력하므로 ETF도 포함될 수
있습니다. ETF 등을 제외한 코스피·코스닥 일반기업 순위는 텔레그램 `/top`을 사용하세요.
CLI 시장 구분은 `000` 전체, `001` 코스피, `101` 코스닥이며 거래소 구분은 `1` KRX,
`2` NXT, `3` 통합입니다. `--exclude-managed`로 관리종목을 제외합니다.
`--limit`은 1~100이며 연속조회는 최대 10페이지입니다. `ka10032`의 `trde_prica`는
백만원 단위이므로 100으로 나누면 억원입니다.

모의 조회 클라이언트의 요청 간격은 인스턴스당 최소 1.05초입니다.
여러 프로세스에 걸친 계좌 전체 호출 제한의 통합 관리는 아직 구현하지 않았습니다.
키 발급과 사용 신청은 [키움 REST API 포털](https://openapi.kiwoom.com/)에서 확인할 수 있습니다.

## 오프라인 예제 전략

`run-demo.command`를 더블클릭하거나 아래 명령으로 가상 데이터의 전략 흐름을 검증합니다.
예제의 `DEMO_KR`, `DEMO_US`와 가격·거래량은 인공 데이터입니다.

```sh
python3 -m kiwoom_autotrader demo --config config.example.json
python3 -m kiwoom_autotrader simulate --bars demo-result/bars.csv --config config.example.json
```

`config.example.json`의 예제는 3봉 이동평균이 5봉 이동평균을 상향 돌파하고 현재 거래량이
직전 3봉 평균의 1.2배 이상이면 1주 매수 신호를 만들며, 하향 돌파하면 전량 매도 신호를
만듭니다. 같은 종목의 다음 봉 시가에 슬리피지를 적용하여 가상 체결하고 매수 한도·현금·수수료를
검사합니다. 원화와 달러는 별도로 계산합니다. `fee_bps=5`, `slippage_bps=10`은 각각
0.05%, 0.1%의 예제 가정이며 실제 수수료표를 뜻하지 않습니다.

실행 결과는 `runs/`의 새 폴더에 `result.json`, `signals.csv`, `fills.csv`,
`rejections.csv`, `bars.csv`, `config.json`으로 저장합니다. CSV 입력에는
`market,exchange,symbol,timestamp,open,close,volume` 열이 필요하며 시간대가 포함된 봉 마감
시각으로 전체 행을 정렬해야 합니다. 기간은 입력한 봉 간격에 따라 달라집니다.
같은 종목·시각의 동일 행은 중복 제거하고 내용이 다르면 오류로 처리합니다.

시뮬레이터는 호가 단위, 부분 체결, 유동성, 세금, 휴장일, 주식분할·배당을 재현하지 않습니다.
실행마다 계좌 상태가 초기화되며 같은 시각의 자금 사용 순서는 입력 순서에 따릅니다.

## 검증과 후속 구현 범위

```sh
python3 -m unittest discover -s tests -v
```

테스트는 대역 HTTP·키체인과 임시 DB로 인증, 명령 수신, 갱신·예약, 주문 요청, 계좌 보고서,
설정, 종료, 관측 이력, 시점별 내보내기와 시뮬레이션을 검증합니다. 테스트에서 실제 키체인에
접근하거나 계좌로 주문하지 않습니다. `realtime/`의 실시간 웹소켓 수신, 봉 생성,
전략 신호와 서버 주문 연결, 주문 체결 추적, 재시작 시 잔고·미체결 복구는 후속 구현 범위입니다.
