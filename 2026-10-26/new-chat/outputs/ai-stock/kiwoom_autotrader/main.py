"""텔레그램 매매 프로그램과 명시적으로 선택하는 오프라인 검증 CLI."""

import argparse
import csv
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import getpass
import json
import os
from pathlib import Path
import re
import sys
from uuid import uuid4
import warnings
from zoneinfo import ZoneInfo

from kiwoom_autotrader.utils.keychain import (
    Credentials, KeychainError, delete_mock_credentials,
    load_mock_credentials, save_mock_credentials,
)
from kiwoom_autotrader.api.client import KiwoomAPIError, KiwoomMockClient
from kiwoom_autotrader.trading.simulation import Bar, SimulationConfig, StrategyConfig, simulate
from kiwoom_autotrader.utils.kst_logging import kst_listener_logs


DEFAULT_CONFIG = {
    "strategy": {
        "fast_window": 3, "slow_window": 5, "volume_window": 3,
        "volume_ratio": "1.2", "quantity": 1,
    },
    "initial_cash": {"KRW": "1000000", "USD": "10000"},
    "max_order_notional": {"KRW": "100000", "USD": "500"},
    "max_position_notional": {"KRW": "200000", "USD": "1000"},
    "fee_bps": "5",
    "slippage_bps": "10",
}
BAR_FIELDS = ["market", "exchange", "symbol", "timestamp", "open", "close", "volume"]


def load_config(path: Path | None) -> SimulationConfig:
    raw = DEFAULT_CONFIG if path is None else json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("설정 파일은 JSON 객체여야 합니다.")
    unknown = set(raw) - set(DEFAULT_CONFIG)
    if unknown:
        raise ValueError(f"알 수 없는 설정 항목: {', '.join(sorted(unknown))}")
    strategy_raw = raw.get("strategy", DEFAULT_CONFIG["strategy"])
    if not isinstance(strategy_raw, dict):
        raise ValueError("strategy는 JSON 객체여야 합니다.")
    unknown = set(strategy_raw) - set(DEFAULT_CONFIG["strategy"])
    if unknown:
        raise ValueError(f"알 수 없는 전략 설정: {', '.join(sorted(unknown))}")
    strategy_values = {**DEFAULT_CONFIG["strategy"], **strategy_raw}
    strategy_values["volume_ratio"] = Decimal(str(strategy_values["volume_ratio"]))
    limits = {}
    for name in ("initial_cash", "max_order_notional", "max_position_notional"):
        values = raw.get(name, DEFAULT_CONFIG[name])
        if not isinstance(values, dict):
            raise ValueError(f"{name}은 통화별 금액 객체여야 합니다.")
        limits[name] = {currency: Decimal(str(amount)) for currency, amount in values.items()}
    return SimulationConfig(
        strategy=StrategyConfig(**strategy_values),
        **limits,
        fee_bps=Decimal(str(raw.get("fee_bps", DEFAULT_CONFIG["fee_bps"]))),
        slippage_bps=Decimal(str(raw.get("slippage_bps", DEFAULT_CONFIG["slippage_bps"]))),
    )


def demo_bars() -> list[Bar]:
    """실제 종목·시장 데이터와 무관한 고정 예제. 휴장일을 모델링하지 않습니다."""
    closes = [
        100, 98, 96, 94, 92, 94, 98, 102, 106, 110, 108, 104, 100,
        96, 92, 94, 98, 102, 106, 110, 108, 104, 100, 96, 92,
        90, 92, 94, 98, 102, 104, 100, 96, 92,
    ]
    bars = []
    for market, exchange, symbol, scale, hour in (
        ("KR", "KRX", "DEMO_KR", Decimal("500"), 7),
        ("US", "NASDAQ", "DEMO_US", Decimal("1"), 21),
    ):
        date = datetime(2026, 1, 5, hour, tzinfo=timezone.utc)
        for index, price in enumerate(closes):
            while date.weekday() >= 5:
                date += timedelta(days=1)
            bars.append(Bar(
                market=market, exchange=exchange, symbol=symbol, timestamp=date,
                open=Decimal(closes[max(0, index - 1)] + 1) * scale,
                close=Decimal(price) * scale,
                volume=300 if index in (7, 17, 28) else 100,
            ))
            date += timedelta(days=1)
    return sorted(bars, key=lambda bar: bar.timestamp)


def read_bars(path: Path) -> list[Bar]:
    bars = []
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if not set(BAR_FIELDS).issubset(reader.fieldnames or []):
            raise ValueError(f"CSV 필수 열: {', '.join(BAR_FIELDS)}")
        for line, row in enumerate(reader, start=2):
            try:
                bars.append(Bar(
                    market=row["market"], exchange=row["exchange"], symbol=row["symbol"],
                    timestamp=datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00")),
                    open=Decimal(row["open"]), close=Decimal(row["close"]),
                    volume=int(row["volume"]),
                ))
            except (ValueError, TypeError, ArithmeticError, AttributeError) as exc:
                raise ValueError(f"CSV {line}행의 값 형식을 확인하세요.") from exc
    if not bars:
        raise ValueError("CSV에 봉 데이터가 없습니다.")
    return bars


def save_results(path: Path, bars: list[Bar], config: SimulationConfig, result: dict) -> None:
    # 기존 실행 기록은 덮어쓰지 않습니다.
    path.mkdir(parents=True, exist_ok=False)
    (path / "result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (path / "config.json").write_text(
        json.dumps(asdict(config), ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    with (path / "bars.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=BAR_FIELDS)
        writer.writeheader()
        for bar in bars:
            row = {key: getattr(bar, key) for key in BAR_FIELDS}
            row["timestamp"] = bar.timestamp.isoformat()
            writer.writerow(row)
    for name in ("signals", "fills", "rejections"):
        rows = result[name]
        fields = list(dict.fromkeys(
            ["market", "exchange", "symbol", "timestamp", "side"]
            + [key for row in rows for key in row]
        ))
        with (path / f"{name}.csv").open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)

    # A post-run evidence receipt, not a backdated preregistration or safety pass.
    import hashlib
    import platform
    source = Path(__file__).parent / "trading" / "simulation.py"
    generated = []
    for name in sorted(["config.json", "bars.csv", "result.json", "signals.csv", "fills.csv", "rejections.csv"]):
        file = path / name
        # Stream files this function just generated; no new 16 MiB input limit
        # on the pre-existing simulator's accepted CSV/output sizes.
        with file.open("rb") as handle:
            digest = hashlib.file_digest(handle, "sha256").hexdigest()
        generated.append({"path": name, "bytes": file.stat().st_size, "sha256": digest})
    evidence = {
        "schema": "ai-stock-simulation-evidence-v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "runtime": {"python": platform.python_version(), "implementation": platform.python_implementation()},
        "files": generated,
        "source": {"module": "kiwoom_autotrader.trading.simulation", "sha256": hashlib.sha256(source.read_bytes()).hexdigest()},
        "preregistered": False,
        "data_source": result.get("data_source", "unknown"),
        "point_in_time_verified": False,
        "broker_fills_verified": False,
        "profitability_verified": False,
        "deployed": False,
    }
    (path / "evidence-manifest.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


class CredentialInputError(ValueError):
    """A non-sensitive message describing an unavailable secure input path."""


def _read_credentials() -> Credentials:
    if not sys.stdin.isatty():
        raise CredentialInputError("키 입력용 터미널에서 credentials-setup을 실행하세요.")
    # getpass normally falls back to echoed input when terminal control fails.
    # Treat that warning as an error so a secret is never read with echo enabled.
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        key = getpass.getpass("App Key (숨김 입력): ").strip()
        secret = getpass.getpass("App Secret (숨김 입력): ").strip()
    return Credentials(app_key=key, app_secret=secret)


def _quote_summary(market: str, symbol: str, exchange: str, data: dict) -> dict:
    # Exclude tokens, broker messages and arbitrary fields from terminal output.
    price = data.get("cur_prc")
    safe_price = price if isinstance(price, str) and re.fullmatch(r"[+-]?[0-9]+(?:\.[0-9]+)?", price) else None
    return {"market": market, "symbol": symbol, "exchange": exchange, "cur_prc": safe_price}


def _connection_command(args: argparse.Namespace) -> int:
    if args.command.startswith("credentials-real-"):
        from kiwoom_autotrader.utils.real_credentials import load_real_credentials, save_real_credentials, delete_real_credentials
        from kiwoom_autotrader.api.session import BrokerSession, terminal_report
        if args.command == "credentials-real-status":
            print("실전 키: 키체인에 저장됨" if load_real_credentials() else "실전 키: 등록 필요")
            return 0
        if args.command == "credentials-real-delete":
            delete_real_credentials()
            print("이 프로그램의 실전 키를 삭제했습니다.")
            return 0
        print("실전투자 API 키를 인증한 뒤 별도의 Mac 키체인 항목에 저장합니다.")
        credentials = _read_credentials()
        session = BrokerSession(credential_loader=lambda _mode: credentials)
        result = session.login("real")
        terminal_report(result, session)
        if not result.success:
            session.clear()
            return 1
        save_real_credentials(credentials)
        session.clear()
        print("실전 키 저장 완료. 텔레그램에서 login real을 보낼 수 있습니다.")
        return 0
    if args.command == "credentials-setup":
        print("모의투자 키를 확인한 뒤 macOS 키체인에 저장합니다.")
        credentials = _read_credentials()
        client = KiwoomMockClient(credentials.app_key, credentials.app_secret)
        client.authenticate()
        # Authentication must succeed before an existing saved pair is replaced.
        save_mock_credentials(credentials)
        print("모의투자 인증과 키체인 저장 완료. 다음 실행부터 자동으로 연결됩니다.")
        return 0
    if args.command == "credentials-status":
        credentials = load_mock_credentials()
        print("모의투자 키: 키체인에 저장됨" if credentials else "모의투자 키: 저장되지 않음")
        return 0
    if args.command == "credentials-delete":
        deleted = delete_mock_credentials()
        print("키체인의 모의투자 키를 삭제했습니다." if deleted else "삭제할 모의투자 키가 없습니다.")
        return 0
    if args.command == "mock-quote" and ((args.market == "KR") != (args.exchange == "KRX")):
        raise CredentialInputError("시장과 거래소의 조합을 확인하세요.")
    if args.command == "top-turnover" and not 1 <= args.limit <= 100:
        raise CredentialInputError("조회할 종목 수는 1~100 사이여야 합니다.")

    credentials = load_mock_credentials()
    if credentials is None:
        if args.command in {"connect", "top-turnover"}:
            print("저장된 모의투자 키가 없습니다. setup-mock.command 또는 credentials-setup을 실행하세요.", file=sys.stderr)
            return 1
        print("저장된 키가 없어 이번 조회에 사용할 모의투자 키를 입력받습니다.")
        credentials = _read_credentials()
    client = KiwoomMockClient(credentials.app_key, credentials.app_secret)
    client.authenticate()
    print("키움 모의투자 인증 성공")
    if args.command == "top-turnover":
        rows = client.top_turnover(
            market=args.market, include_managed=args.include_managed,
            limit=args.limit, exchange=args.exchange,
        )
        public_fields = {
            "now_rank", "stk_cd", "stk_nm", "cur_prc", "flu_rt", "trde_prica",
        }
        public_rows = [{key: value for key, value in row.items() if key in public_fields} for row in rows]
        # This snapshot contains public ranking data, never the token response.
        print(json.dumps({
            "environment": "mock", "market": args.market, "exchange": args.exchange,
            "queried_at_kst": datetime.now(ZoneInfo("Asia/Seoul")).isoformat(),
            "include_managed": args.include_managed, "count": len(public_rows),
            "trde_prica_unit": "백만원", "rows": public_rows,
        }, ensure_ascii=False, indent=2))
        return 0
    if args.command == "mock-quote":
        data = client.quote(args.market, args.symbol, args.exchange)
        print(json.dumps(_quote_summary(args.market, args.symbol, args.exchange, data), ensure_ascii=False, indent=2))
        return 0
    failed = False
    for market, symbol, exchange, label in (
        ("KR", "005930", "KRX", "국내 삼성전자"),
        ("US", "AAPL", "NASDAQ", "미국 애플"),
    ):
        try:
            client.quote(market, symbol, exchange)
            print(f"{label} 시세 조회 성공")
        except KiwoomAPIError:
            print(f"{label} 시세 조회 실패. 해당 시장의 모의투자 이용 설정을 확인하세요.", file=sys.stderr)
            failed = True
    return int(failed)


@kst_listener_logs
def _telegram_account_command(args: argparse.Namespace) -> int:
    import asyncio
    collector_worker = args.command == "telegram-account-listen" and args.state_dir is not None
    collector_error = ()
    phase = 'load_dependencies'
    try:
        from kiwoom_autotrader.utils.dependencies import enable_account_dependencies
        enable_account_dependencies()
        phase = 'load_account_command'
        from kiwoom_autotrader.telegram.account_credentials import load_account_config
        from kiwoom_autotrader.utils.account_service import (
            account_paths, collector_status, remove_collector_service,
            start_collector_service, stop_collector_service,
        )
        if args.command == "telegram-account-setup":
            from kiwoom_autotrader.telegram.account_setup import serve_setup
            return serve_setup(open_browser=not args.no_browser)
        if args.command == "telegram-account-listen":
            if args.state_dir is None:
                start_collector_service()
                print("개인 계정 채널 수집을 시작했습니다.")
                return 0
            phase = 'load_collector'
            from kiwoom_autotrader.telegram.collector import CollectorError, log_background_failure, run_collector
            collector_error = CollectorError
            phase = 'collector_event_loop'
            with asyncio.Runner() as runner:
                runner.get_loop().set_exception_handler(log_background_failure)
                return runner.run(run_collector(args.state_dir))
        if args.command == "telegram-account-stop":
            stop_collector_service()
            print("채널 수집을 중지했습니다. 다음 Mac 로그인 시 다시 실행됩니다.")
            return 0
        if args.command == "telegram-account-autostart-remove":
            remove_collector_service()
            print("채널 수집 자동 실행을 해제했습니다. 계정과 보관된 글은 유지됩니다.")
            return 0
        config = load_account_config()
        print("개인 계정: " + ("키체인에 저장됨" if config else "연결 필요"))
        print(json.dumps(collector_status(), ensure_ascii=False, indent=2))
        if config:
            from kiwoom_autotrader.telegram.channel_store import ChannelStore
            with ChannelStore(account_paths().state, config.user_id) as store:
                print(json.dumps(store.summary(), ensure_ascii=False, indent=2))
        return 0
    except KeyboardInterrupt:
        return 0
    except Exception as error:
        if collector_worker and not isinstance(error, collector_error):
            code = 'dependency_import_failure' if isinstance(error, ImportError) else 'collector_cli_failure'
            print(f'pid={os.getpid()} 텔레그램 수집 실패: phase={phase} code={code}', file=sys.stderr, flush=True)
        print("개인 계정 연결 또는 채널 수집을 완료하지 못했습니다. 연결 화면과 수집 상태를 확인하세요.", file=sys.stderr)
        return 1


def _telegram_command(args: argparse.Namespace) -> int:
    from kiwoom_autotrader.telegram.client import TelegramAPIError, TelegramClient
    from kiwoom_autotrader.telegram.bot_credentials import load_telegram_config
    from kiwoom_autotrader.telegram.setup import configure_telegram, TelegramSetupError
    from kiwoom_autotrader.utils.telegram_service import (
        TelegramServiceError, default_state_directory, install_service,
        service_paths, service_status, start_service, stop_service, uninstall_service,
    )

    try:
        if args.command == "telegram-console":
            from kiwoom_autotrader.telegram.listener import run_listener
            stop_service()
            print("터미널 수신 모드입니다. 토큰 발급 시 원문을 이 터미널에 표시합니다.")
            return run_listener(default_state_directory(Path(__file__).resolve().parent.parent))
        if args.command == "telegram-autostart-install":
            install_service()
            print("Mac 로그인 시 텔레그램 자동 실행을 등록하고 시작했습니다.")
            return 0
        if args.command == "telegram-autostart-remove":
            uninstall_service()
            print("텔레그램 자동 실행을 해제하고 수신을 중지했습니다. 키체인 설정은 유지됩니다.")
            return 0
        if args.command == "telegram-autostart-status":
            print(json.dumps(service_status(), ensure_ascii=False, indent=2))
            return 0
        if args.command in {"telegram-listen", "telegram-stop", "telegram-status"}:
            explicit_state = args.state_dir is not None
            args.state_dir = args.state_dir or default_state_directory(Path(__file__).resolve().parent.parent)
            if not explicit_state and service_paths().plist.is_file():
                if args.command == "telegram-listen":
                    start_service()
                    print("텔레그램 백그라운드 수신 프로그램을 시작했습니다.")
                    return 0
                if args.command == "telegram-stop":
                    stop_service()
                    print("텔레그램 수신을 중지했습니다. 다음 Mac 로그인 시 다시 시작합니다.")
                    return 0
                print("로그인 자동 실행: " + json.dumps(service_status(), ensure_ascii=False))
        if args.command == "telegram-setup":
            if not sys.stdin.isatty():
                raise CredentialInputError("Telegram 봇 토큰 입력용 터미널에서 실행하세요.")
            print("BotFather의 봇 토큰을 붙여넣으세요. 입력한 글자는 화면에 표시되지 않습니다.")
            with warnings.catch_warnings():
                warnings.simplefilter("error", getpass.GetPassWarning)
                token = getpass.getpass("Telegram Bot Token (숨김 입력): ").strip()
            configure_telegram(token, timeout_seconds=600)
            return 0
        if args.command == "telegram-stop":
            args.state_dir.mkdir(parents=True, exist_ok=True)
            (args.state_dir / "stop.request").touch()
            print("수신 프로그램에 종료를 요청했습니다. 현재 조회가 끝나면 종료됩니다.")
            return 0
        if args.command == "telegram-listen":
            from kiwoom_autotrader.telegram.listener import run_listener, TelegramListenerError, listener_error_code
            try:
                return run_listener(args.state_dir)
            except TelegramListenerError as error:
                print("텔레그램 수신 프로그램을 시작하거나 유지하지 못했습니다. 중복 실행·연결 설정·네트워크를 확인하세요.", file=sys.stderr)
                print(f"텔레그램 수신 종료 분류: {listener_error_code(error)}", file=sys.stderr)
                return 1
        config = load_telegram_config()
        if config is None:
            print("텔레그램 연결 정보가 없습니다. setup-telegram.command를 실행하세요.", file=sys.stderr)
            return 1
        if args.command == "telegram-status":
            print(f"텔레그램 봇: @{config.bot_username} / 개인 채팅 연결 정보 저장됨")
            state_file = args.state_dir / "state.json"
            if state_file.exists():
                state = json.loads(state_file.read_text(encoding="utf-8"))
                safe = {key: state[key] for key in (
                    "status", "received_count", "replied_count", "last_command", "last_reply_at",
                ) if key in state}
                print(json.dumps(safe, ensure_ascii=False, indent=2))
            return 0
        if args.command == "telegram-test":
            TelegramClient(config.bot_token).send_message(config.chat_id, "키움 모의투자 프로그램: 텔레그램 알림 테스트입니다.")
            print("개인 채팅으로 테스트 메시지 1건을 보냈습니다.")
            return 0
        return 1
    except (TelegramSetupError, TelegramServiceError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except (TelegramAPIError, KeychainError, ValueError, OSError, TypeError):
        print("텔레그램 연결 처리에 실패했습니다. 토큰·개인 채팅 연결·키체인 상태를 확인하세요.", file=sys.stderr)
        return 1
    except getpass.GetPassWarning:
        print("봇 토큰을 숨겨 입력할 수 없어 중단했습니다.", file=sys.stderr)
        return 1


def main(argv: list[str] | None = None) -> int:
    # These explicit subcommands cannot fall through to the live listener.
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments and arguments[0] in {"offline-audit", "history", "research"}:
        import importlib
        module = {"offline-audit": "diagnostics", "history": "history", "research": "research"}[arguments[0]]
        return importlib.import_module(f"kiwoom_autotrader.{module}.__main__").main(arguments[1:])
    parser = argparse.ArgumentParser(description="키움 모의투자 자동 연결 및 오프라인 검증 도구")
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("offline-audit", help="저장 원장·CSV·상태를 읽기전용 감사 (계좌 연결 없음)")
    commands.add_parser("history", help="과거 보존 이력 검증·분리 저장·조회 (계좌 연결 없음)")
    commands.add_parser("research", help="연구 가설·입력 SHA 고정 및 오프라인 검증")
    commands.add_parser("connect", help="저장된 모의투자 키로 인증 후 국내·미국 시세 조회 (기본 실행)")
    commands.add_parser("credentials-setup", help="모의투자 인증에 성공한 키를 macOS 키체인에 저장·교체")
    commands.add_parser("credentials-status", help="키체인의 모의투자 키 저장 여부만 확인")
    commands.add_parser("credentials-delete", help="키체인에 저장한 모의투자 키 삭제")
    commands.add_parser("credentials-real-setup", help="실전 키 인증 후 별도 키체인 저장")
    commands.add_parser("credentials-real-status", help="실전 키 저장 여부 확인")
    commands.add_parser("credentials-real-delete", help="이 프로그램의 실전 키 삭제")
    commands.add_parser("telegram-console", help="터미널에서 계속 수신하고 토큰 원문 출력")
    commands.add_parser("telegram-setup", help="봇 토큰 확인과 개인 채팅 연결, 테스트 메시지 1건 전송")
    commands.add_parser("telegram-test", help="저장된 개인 채팅으로 테스트 메시지 1건 전송")
    commands.add_parser("telegram-autostart-install", help="Mac 로그인 시 텔레그램 자동 실행 설치 또는 업데이트")
    commands.add_parser("telegram-autostart-remove", help="자동 실행 해제 및 수신 중지 (키체인 유지)")
    commands.add_parser("telegram-autostart-status", help="Mac 자동 실행 서비스의 실제 실행 상태 확인")
    account_setup = commands.add_parser("telegram-account-setup", help="개인 계정 인증과 수집할 채널 선택 화면")
    account_setup.add_argument("--no-browser", action="store_true", help="로컬 연결 화면 주소만 출력")
    account_listen = commands.add_parser("telegram-account-listen", help="개인 계정 채널 수집 시작")
    account_listen.add_argument("--state-dir", type=Path, default=None)
    commands.add_parser("telegram-account-status", help="개인 계정 및 채널 수집 상태")
    commands.add_parser("telegram-account-stop", help="개인 계정 채널 수집 중지")
    commands.add_parser("telegram-account-autostart-remove", help="채널 수집 자동 실행 해제")
    for name, help_text in (
        ("telegram-listen", "본인 채팅의 /help, /status, /top 명령을 수신하고 답변"),
        ("telegram-status", "텔레그램 연결 및 수신 프로그램 상태 기록 확인"),
        ("telegram-stop", "실행 중인 텔레그램 수신 프로그램에 종료 요청"),
    ):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("--state-dir", type=Path, default=None)
    for name, help_text in (
        ("demo", "가상 국내·미국 데이터로 예제 전략 검증"),
        ("simulate", "CSV 봉 데이터로 예제 전략 실행"),
    ):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("--config", type=Path, help="JSON 설정 파일")
        command.add_argument("--output", type=Path, help="새 결과 폴더; 기존 폴더는 덮어쓰지 않음")
        if name == "simulate":
            command.add_argument("--bars", type=Path, required=True)
    quote = commands.add_parser("mock-quote", help="키움 모의서버 인증 후 종목 현재가 조회")
    quote.add_argument("--market", choices=("KR", "US"), required=True)
    quote.add_argument("--symbol", required=True)
    quote.add_argument("--exchange", choices=("KRX", "NASDAQ", "NYSE", "AMEX"), required=True)
    ranking = commands.add_parser("top-turnover", help="저장된 키로 거래대금 상위 종목 조회")
    ranking.add_argument("--market", choices=("000", "001", "101"), default="001", help="000 전체, 001 코스피, 101 코스닥")
    ranking.add_argument("--exchange", choices=("1", "2", "3"), default="3", help="1 KRX, 2 NXT, 3 통합")
    ranking.add_argument("--limit", type=int, default=20, help="조회할 상위 종목 수 (1~100)")
    ranking.add_argument("--exclude-managed", dest="include_managed", action="store_false", help="관리종목 제외 (기본 포함)")
    export = commands.add_parser("messages-export", help="수집 글을 한국시간 일자별 JSONL·팩터 CSV로 내보내기")
    export.add_argument("--output", type=Path, default=None,
                        help="내보낼 폴더; 생략하면 선택된 채널 저장소의 일자별 폴더 사용 (SSD 미확인 시 중단)")
    export.add_argument("--as-of", help="관측 정보의 기준 시각 ISO8601, 시간대 필수")
    args = parser.parse_args(argv)
    if args.command is None:
        args = parser.parse_args(["telegram-listen"])
    try:
        if args.command == "messages-export":
            from kiwoom_autotrader.telegram.account_credentials import load_account_config
            from kiwoom_autotrader.utils.account_service import account_paths
            from kiwoom_autotrader.telegram.channel_store import ChannelStore, ChannelStoreError
            from kiwoom_autotrader.utils.message_dataset import MessageDatasetError, daily_output_root, export_daily_dataset
            config = load_account_config()
            if config is None:
                raise ValueError("개인 계정과 수집할 채널을 먼저 연결하세요.")
            cutoff = None
            if args.as_of:
                moment = datetime.fromisoformat(args.as_of)
                if moment.tzinfo is None or moment.utcoffset() is None:
                    raise ValueError("--as-of 시각에는 +09:00 또는 Z와 같은 시간대가 필요합니다.")
                cutoff = int(moment.timestamp())
            state = account_paths().state
            try:
                with ChannelStore(state, config.user_id) as store:
                    output = daily_output_root(store, state) if args.output is None else args.output
                    print(json.dumps(export_daily_dataset(store, output, as_of=cutoff), ensure_ascii=False, indent=2))
            except (MessageDatasetError, ChannelStoreError):
                print("일자별 내보내기를 중단했습니다. 선택된 저장소의 연결·경로·식별 정보를 확인하세요. 다른 저장소로 대체하지 않습니다.", file=sys.stderr)
                return 1
            return 0
        if args.command.startswith("telegram-account-"):
            return _telegram_account_command(args)
        if args.command.startswith("telegram-"):
            return _telegram_command(args)
        if args.command.startswith("credentials-real-") or args.command in {"connect", "mock-quote", "top-turnover", "credentials-setup", "credentials-status", "credentials-delete"}:
            try:
                return _connection_command(args)
            except KeychainError:
                print("macOS 키체인에 접근하지 못했습니다. 키체인 잠금·접근 허용 상태를 확인하세요.", file=sys.stderr)
                return 1
            except KiwoomAPIError:
                print("모의서버 연결에 실패했습니다. 모의투자용 키·등록된 접속 IP·네트워크를 확인하세요.", file=sys.stderr)
                return 1
            except getpass.GetPassWarning:
                print("키를 숨겨 입력할 수 없어 중단했습니다. Mac 터미널에서 다시 실행하세요.", file=sys.stderr)
                return 1
            except CredentialInputError as exc:
                print(str(exc), file=sys.stderr)
                return 1
            except (ValueError, OSError, TypeError):
                print("모의투자 연결 설정을 확인하지 못했습니다. 키와 키체인 상태를 확인하세요.", file=sys.stderr)
                return 1
        config = load_config(args.config)
        bars = demo_bars() if args.command == "demo" else read_bars(args.bars)
        result = simulate(bars, config)
        result["data_source"] = "synthetic_demo" if args.command == "demo" else "user_csv"
        result["limitations"] = [
            "오프라인 가상 체결이며 키움 모의계좌 주문과 다릅니다.",
            "실제 호가 단위, 부분 체결, 유동성, 휴장일, 세금, 환전은 모델링하지 않습니다.",
            "수수료와 슬리피지는 설정한 예제 가정입니다. 통화별 자산은 합산하지 않습니다.",
        ]
        output = args.output or Path("runs") / (
            datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8]
        )
        save_results(output, bars, config, result)
        print("오프라인 시뮬레이션 완료 (가상 체결)")
        print(f"봉 {result['bars_processed']}개 / 신호 {len(result['signals'])}개 / "
              f"체결 {len(result['fills'])}건 / 거절 {len(result['rejections'])}건")
        print(f"마지막 봉 이후 대기 주문: {len(result['pending_orders'])}건")
        for currency, amount in result["equity"].items():
            print(f"평가자산 {currency}: {amount}")
        print(f"기록: {output.resolve()}")
        return 0
    except (ValueError, OSError, ArithmeticError, TypeError) as exc:
        print(f"실행 실패: {exc}", file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print("\n실행을 종료했습니다.", file=sys.stderr)
        return 130
