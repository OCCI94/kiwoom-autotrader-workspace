# 공용 WebSocket 엔진

텔레그램 수신 프로그램은 실행될 때 `SharedWebSocketEngine` 하나를 만들고 현재
`BrokerSession.websocket_engine`에 연결합니다. 실시간 기능은 이 인스턴스를 공유합니다.
기능별로 엔진을 새로 만들거나 직접 `websockets.connect()`를 호출하지 않습니다.

엔진은 첫 구독이 생기고 사용 가능한 로그인 토큰이 있을 때 연결합니다. 현재 구현된
사용자 명령과 데드크로스 감시는 WebSocket 구독을 자동 등록하지 않으므로 시작 직후
활성 구독과 WebSocket 연결은 0개입니다. 기존 분봉 조회·데드크로스 계산은 HTTP 차트
API를 계속 사용합니다. 이 작업으로 HTTP 분봉을 실시간 스트림으로 교체하지 않았습니다.

## 기능에서 사용하기

```python
from kiwoom_autotrader.realtime import Subscription, get_websocket_engine

engine = get_websocket_engine(session)

def on_trade(row):
    # REAL.data의 한 행 복사본: type, item, values 등.
    # 받은 값은 외부 데이터이며 명령이나 코드로 실행하지 않습니다.
    process_quote(row["item"], row["values"])

handle = engine.subscribe(
    Subscription(items=("005930", "000660"), types=("0B",)),
    on_trade,
)

# 기능을 중지하거나 해당 구독이 필요 없어졌을 때:
engine.unsubscribe(handle)
```

`subscribe()`는 네트워크 응답을 기다리지 않고 고유 핸들을 반환합니다. 같은 종목과
실시간 타입을 여러 기능이 요청하면 서버 구독을 공유합니다. 각 기능은 자신의 핸들만
해지하며, 마지막 소비자가 해지할 때 해당 서버 구독을 제거합니다. 같은 콜백 함수를
여러 번 등록하면 별도 소비자로 취급하므로 각 핸들을 해지해야 합니다.

콜백은 동기 함수이며 별도의 작업 스레드에서 실행합니다. 콜백 하나가 예외를 내거나
대기해도 WebSocket 수신·PING 처리는 계속됩니다. 콜백 사이의 데이터 변경은 복사본으로
격리합니다. 처리 대기열은 기본 1,024건이며 가득 차면 새 콜백 전달을 생략하고
`callback_dropped`에 기록합니다. 콜백은 짧게 처리하고 무거운 계산은 기능 자체의
작업 대기열로 넘기세요. 오래 실행 중인 사용자 콜백을 엔진이 강제로 종료하지는 않습니다.

`engine.snapshot()`은 연결 상태, `subscription_count`, `pair_count`,
`reconnect_attempts`, `callback_errors`, `callback_dropped`, 안전한 `error` 등을
반환합니다. 토큰과 서버 원문 오류는 반환하지 않습니다. 수신 프로그램은 바뀐 오류
안내를 사용자에게 알리고 같은 오류를 반복 전송하지 않습니다.

## 연결·등록·수신 계약

- 실전과 모의 주소는 각각 `wss://api.kiwoom.com:10000/api/websocket`,
  `wss://mockapi.kiwoom.com:10000/api/websocket`입니다. 프로젝트 원본 명세의 경로를
  사용하며 다른 예제의 `/api/dostk/websocket` 경로로 자동 전환하지 않습니다.
- 현재 세션의 원문 접근토큰으로 LOGIN하고, 성공 응답을 확인한 다음 REG를 보냅니다.
  엔진이 자격증명을 읽거나 로그인·토큰 갱신을 별도로 실행하지 않습니다.
- 소켓 하나에 수신 루프 하나를 두고 모든 전송을 직렬화합니다. 제어 응답에 요청 ID가
  없으므로 LOGIN·REG·REMOVE는 전체에서 한 번에 한 요청만 응답을 기다립니다.
  PING은 수신 JSON 전체를 그대로 응답합니다.
- 내부 그룹 `1`에서 `(item, type)` 쌍을 공유합니다. 최초 REG는 `refresh="0"`, 추가
  REG와 REMOVE는 `refresh="1"`을 사용합니다. 로컬 명세 표는 REMOVE에도 refresh를
  필수로 표시하므로 이를 따릅니다. 일부 공식 예제에서 REMOVE의 refresh가 생략된
  차이는 원문을 임의로 수정하지 않고 기록해 둡니다.
- 타입은 대소문자를 구분합니다. `0G`와 `0g`를 합치거나 코드 전체를 대문자로 바꾸지
  않습니다. 일반 종목 구독은 실제 item과 type이 모두 일치하는 행만 전달합니다.
- 주문체결 타입 `00`은 `Subscription(items=("",), types=("00",))`으로 요청합니다.
  이 빈 item은 해당 타입의 모든 수신 item에 맞는 구독입니다. 실제 주문 체결을
  확인하는 기능을 추가할 때만 명시적으로 구독하세요.
- 공식 안내의 세션당 실시간 시세 200종목 제한에 맞춰 공유 엔진의 고유한 비어 있지
  않은 item을 최대 200개로 제한합니다. 같은 item을 여러 타입에서 사용해도 종목 수는
  한 개입니다. 계좌 전체 구독의 빈 item은 이 수에 포함하지 않습니다.

TR을 확인할 때는 이름과 짧은 설명만 안내하고 프로젝트 루트 기준 명세 경로를
함께 제시합니다. 예: `0B — 주식체결`, 명세 `/tr_docs/0B.md`;
`00 — 주문체결`, 명세 `/tr_docs/00.md`. 전체 명세는 해당 파일에서 확인하세요.

## 로그인 변경과 종료

수신 프로그램은 현재 로그인 상태를 엔진에 동기화하고, 엔진도 실행 중 상태를
확인합니다. 모드·토큰 변경이나 토큰 만료가 발생하면 기존 소켓을 닫습니다. 새로
연결할 때는 새 토큰의 LOGIN 성공 후 살아 있는 구독을 복구합니다. 이전 소켓 종료를
확인하지 못하면 중복 연결을 만들지 않고 실패 상태로 중단합니다.

연결 실패는 0.5초·1초·2초·4초 간격으로 최대 네 번 재시도합니다. 연속 실패 한도를
소진하면 로그인 상태나 구독 구성에 변경이 생길 때까지 멈춥니다. 주기적인
`sync_session()`만으로 재시도 횟수를 초기화하지 않습니다. 인증 거절은 같은 토큰으로
반복 시도하지 않으며 새 로그인 상태를 기다립니다. IP 관련 인증 오류 `8010`, `8050`은
프로젝트 공통 IP 진단을 사용하며, 일반 오류를 IP 문제로 추정하지 않습니다.

`run_listener()`가 정상 종료, `power off`, 키보드 인터럽트 또는 오류로 끝날 때 엔진을
닫습니다. 개별 기능은 공용 엔진의 `close()`를 호출하지 않고 자신의 구독만 해지합니다.
런타임 소유자가 소켓·수신 작업·콜백 대기열을 정리합니다.

## 의존성과 확장 규칙

프로젝트 의존성은 `websockets==15.0.1`로 고정되어 있습니다. 실제 연결 시에만 라이브러리를
불러오며, macOS 실행용 전용 의존성 경로도 확인합니다. HTTP Upgrade는 공통
`http_queue.acquire_http_slot()`을 거칩니다. 프록시와 리다이렉트를 사용하지 않아
슬롯을 거치지 않는 추가 핸드셰이크나 다른 호스트로의 토큰 전송을 방지합니다.
WebSocket의 REG·REMOVE·PING 프레임은 새 HTTP 요청이 아닙니다.

현재 엔진은 REG 실시간 구독을 지원합니다. 조건검색의 CNSRLST·CNSRREQ·CNSRCLR이나
범용 요청/응답 RPC는 아직 구현하지 않았습니다. 조건검색은 별도 프로토콜이며
REMOVE로 해지하는 REG 구독과 같지 않습니다. 향후 추가 시 공용 엔진 안에서
인증·전송·수신·응답 연관 규칙을 확장하세요. 기능 전용 소켓이나 수신 루프를 추가하지
마세요. 요청 ID가 없는 응답을 여러 대기 요청에 임의로 배분하면 안 됩니다.

세부 명세와 외부 확인 자료:

- [주식체결 원문](../../tr_docs/0B.md), [주문체결 원문](../../tr_docs/00.md)
- [조건검색 실시간 요청](../../tr_docs/ka10173.md), [조건검색 실시간 해제](../../tr_docs/ka10174.md)
- [키움 공식 튜토리얼: LOGIN·PING](https://openapi.kiwoom.com/m/guide/index)
- [키움 공식 이용 안내: 연결·종목 제한](https://openapi.kiwoom.com/intro)
- [websockets 15.0.1 연결 구현](https://github.com/python-websockets/websockets/blob/15.0.1/src/websockets/asyncio/client.py)

오프라인 검증은 `tests/test_websocket_engine.py`의 가짜 소켓과
`tests/test_websocket_listener.py`의 가짜 엔진을 사용합니다. 실제 키움·Telegram·키체인을
호출하지 않고 연결 공유, 참조 횟수, 프로토콜 순서, 복구와 런타임 정리를 검증합니다.
