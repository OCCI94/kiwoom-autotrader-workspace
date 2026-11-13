"""Persist ordinary settings without storing API credentials or tokens."""

import json
from kiwoom_autotrader.utils.platform_io import os, sync_directory
from pathlib import Path
from kiwoom_autotrader.utils.paths import app_data_root
import re
import tempfile
from datetime import datetime
from kiwoom_autotrader.utils.control_audit import registered_mutator


class SettingsError(RuntimeError):
    pass


PAPER_MULTI_STRATEGIES = (
    "S2_surge_high_breakout", "S3_upper_limit_continuation",
    "S4_theme_and_catalyst_leader", "S5_steady_uptrend_vwap",
)
PAPER_EXPERIMENT_STRATEGIES = (
    "EXP_A", "EXP_B", "EXP_B_TP", "EXP_E", "EXP_R", "EXP_C", "EXP_D",
    "EXP_ORB_A", "EXP_ORB_B", "EXP_GDR", "EXP_VSS", "EXP_LDR", "EXP_ETF_B",
)
_LEGACY_MULTI_KEYS = {"paper_multi_strategy_enabled", "paper_multi_strategy_ids",
               "gdcrs_initial_entry_pct", "gdcrs_add_on_profit_bps", "gdcrs_max_daily_entries_per_symbol"}
_SCAN_DEFAULTS = {"paper_dynamic_scan_enabled": False, "paper_dynamic_candidate_limit": 20,
                  "paper_fast_decision_enabled": False,
                  "paper_toss_hybrid_enabled": False,
                  "paper_strategy_stocks": [], "paper_max_concurrent_positions": 8,
                  "paper_strategy_entry_pool_limit": 30}
_THEME_DEFAULTS = {"max_positions_per_theme": 2, "paper_theme_unclassified_policy": "block"}
_FUNNEL_DEFAULTS = {"paper_funnel_enabled": False, "paper_funnel_hot_limit": 10, "paper_funnel_priority_symbols": []}
_ENTRY_TIME_DEFAULTS = {"gdcrs_entry_start_time": "09:00", "gdcrs_entry_end_time": "15:20"}
_MULTI_KEYS = _LEGACY_MULTI_KEYS | set(_SCAN_DEFAULTS) | set(_THEME_DEFAULTS) | set(_FUNNEL_DEFAULTS) | set(_ENTRY_TIME_DEFAULTS) | {"gdcrs_unfilled_buy_ttl_seconds", "paper_experiment_enabled", "paper_experiment_s5_enabled"}


def paper_strategy_options_path(path=None):
    return (path or settings_path()).with_name("paper-strategy-options.json")


def settings_path() -> Path:
    return app_data_root() / "config" / "data" / "settings.json"


def _validate(value):
    if isinstance(value, dict):
        value = {**{k:(list(v) if isinstance(v, list) else v) for k,v in _SCAN_DEFAULTS.items()}, **_THEME_DEFAULTS, **{k:(list(v) if isinstance(v,list) else v) for k,v in _FUNNEL_DEFAULTS.items()}, **_ENTRY_TIME_DEFAULTS, **value, "paper_gdcrs_guards": value.get("paper_gdcrs_guards", {}),
                 "gdcrs_max_daily_entries_per_symbol": value.get("gdcrs_max_daily_entries_per_symbol", 0),
                 "gdcrs_initial_entry_pct": value.get("gdcrs_initial_entry_pct", 100),
                 "gdcrs_add_on_profit_bps": value.get("gdcrs_add_on_profit_bps", 0),
                 "gdcrs_unfilled_buy_ttl_seconds": value.get("gdcrs_unfilled_buy_ttl_seconds", 0),
                 "paper_experiment_enabled": value.get("paper_experiment_enabled", False),
                 "paper_experiment_s5_enabled": value.get("paper_experiment_s5_enabled", False),
                 "paper_multi_strategy_enabled": value.get("paper_multi_strategy_enabled", False),
                 "paper_multi_strategy_ids": value.get("paper_multi_strategy_ids", [])}
    if (not isinstance(value, dict) or set(value) != {
        "version", "login_renewal_time", "timezone", "channel_poll_interval_seconds", "daily_message_export",
        "market_start_time", "market_end_time", "reservations", "next_reservation_id",
        "gdcrs_short_period", "gdcrs_long_period", "gdcrs_stocks", "gdcrs_enabled", "ddcrs_enabled",
        "gdcrs_min_cross_bps", "gdcrs_reentry_cooldown_minutes", "gdcrs_max_daily_entries_per_symbol",
        "gdcrs_initial_entry_pct", "gdcrs_add_on_profit_bps", "gdcrs_unfilled_buy_ttl_seconds",
        "gdcrs_entry_start_time", "gdcrs_entry_end_time",
        "paper_multi_strategy_enabled", "paper_multi_strategy_ids",
        "paper_dynamic_scan_enabled", "paper_dynamic_candidate_limit", "paper_strategy_stocks", "paper_toss_hybrid_enabled",
        "paper_fast_decision_enabled",
        "paper_max_concurrent_positions", "paper_strategy_entry_pool_limit", "max_positions_per_theme",
        "paper_gdcrs_guards", "paper_theme_unclassified_policy", "paper_funnel_enabled", "paper_funnel_hot_limit", "paper_funnel_priority_symbols",
        "paper_automation_only", "paper_automation_buy_cap", "paper_automation_symbol_cap",
        "paper_experiment_enabled", "paper_experiment_s5_enabled",
    } or type(value["version"]) is not int or value["version"] != 1
        or not isinstance(value["login_renewal_time"], str)
        or re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", value["login_renewal_time"]) is None
        or value["timezone"] != "Asia/Seoul"
        or type(value["channel_poll_interval_seconds"]) is not int
        or not 30 <= value["channel_poll_interval_seconds"] <= 86400
        or type(value["daily_message_export"]) is not bool):
        raise SettingsError("프로그램 일반 설정 형식을 확인하세요.")
    for key in ("market_start_time", "market_end_time"):
        if not isinstance(value[key], str) or re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", value[key]) is None:
            raise SettingsError("장 시간은 HH:MM 형식이어야 합니다.")
    if value["market_start_time"] >= value["market_end_time"]:
        raise SettingsError("장 시작 시각은 종료 시각보다 빨라야 합니다.")
    for key in ("gdcrs_short_period", "gdcrs_long_period"):
        if type(value[key]) is not int or not 1 <= value[key] <= 60:
            raise SettingsError("교차 전략의 이동평균 기간은 각각 1~60 사이의 정수여야 합니다.")
    if any(type(value[key]) is not bool for key in ("gdcrs_enabled", "ddcrs_enabled")):
        raise SettingsError("교차 전략 실행 설정 형식을 확인하세요.")
    if (type(value["gdcrs_min_cross_bps"]) is not int or not 0 <= value["gdcrs_min_cross_bps"] <= 10000
            or type(value["gdcrs_reentry_cooldown_minutes"]) is not int
            or not 0 <= value["gdcrs_reentry_cooldown_minutes"] <= 1440):
        raise SettingsError("교차 이격도(0~10000bp)와 재진입 대기(0~1440분)를 확인하세요.")
    if (type(value["gdcrs_max_daily_entries_per_symbol"]) is not int
            or not 0 <= value["gdcrs_max_daily_entries_per_symbol"] <= 1000):
        raise SettingsError("종목별 일일 자동매수 진입 한도는 0~1000 정수여야 합니다.")
    effective_cross_guards(value, "paper")
    effective_b3_guards(value, "paper")
    effective_unfilled_buy_ttl(value, "paper")
    effective_entry_time_window(value, "paper")
    effective_multi_strategies(value, "paper")
    effective_paper_scan(value, "paper")
    effective_theme_cap(value, "paper")
    effective_theme_unclassified_policy(value, "paper")
    effective_paper_funnel(value, "paper")
    effective_toss_hybrid(value, "paper")
    effective_fast_decision(value, "paper")
    effective_paper_experiment(value, "paper")
    if (type(value["paper_automation_only"]) is not bool
            or type(value["paper_automation_buy_cap"]) is not int
            or not 1 <= value["paper_automation_buy_cap"] <= 3000000
            or type(value["paper_automation_symbol_cap"]) is not int
            or not 1 <= value["paper_automation_symbol_cap"] <= 3000000):
        raise SettingsError("모의 자동매매 매수당·종목당 한도(각 1~3,000,000원)를 확인하세요.")
    stocks = value["gdcrs_stocks"]
    if not isinstance(stocks, list) or len(stocks) > 100:
        raise SettingsError("골든크로스 등록 종목은 최대 100개까지 저장할 수 있습니다.")
    stock_codes = set()
    for row in stocks:
        if (not isinstance(row, dict) or set(row) not in ({"code", "budget"}, {"code", "budget", "paper_budget"})
                or not isinstance(row["code"], str) or re.fullmatch(r"[0-9]{6}", row["code"]) is None
                or row["code"] in stock_codes or type(row["budget"]) is not int
                or not 1 <= row["budget"] <= 999999999999
                or ("paper_budget" in row and (type(row["paper_budget"]) is not int
                                               or not 1 <= row["paper_budget"] <= 3000000))):
            raise SettingsError("골든크로스 종목코드·예산 설정 형식을 확인하세요.")
        stock_codes.add(row["code"])
    if (type(value["next_reservation_id"]) is not int or not 1 <= value["next_reservation_id"] <= 10**12
            or not isinstance(value["reservations"], list) or len(value["reservations"]) > 100):
        raise SettingsError("예약 설정 형식을 확인하세요.")
    seen = set()
    for row in value["reservations"]:
        if (not isinstance(row, dict) or set(row) != {"id", "time", "command", "created_at"}
                or type(row["id"]) is not int or not 0 < row["id"] < value["next_reservation_id"]
                or row["id"] in seen or not isinstance(row["time"], str)
                or re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", row["time"]) is None
                or not isinstance(row["command"], str) or not 1 <= len(row["command"]) <= 500
                or not row["command"].isprintable() or not isinstance(row["created_at"], str)
                or not 1 <= len(row["created_at"]) <= 40):
            raise SettingsError("예약 설정 형식을 확인하세요.")
        try:
            moment = datetime.fromisoformat(row["created_at"])
            if moment.tzinfo is None or moment.utcoffset() is None:
                raise ValueError
        except ValueError:
            raise SettingsError("예약 등록 시각을 확인하세요.") from None
        seen.add(row["id"])
    return value


def default_settings() -> dict:
    try:
        value = json.loads((Path(__file__).parent / "data" / "settings-default.json").read_text())
        # Older installed packages have the same buy cap but no per-symbol
        # field. Preserve their resource bytes; add only the validated legacy
        # default in memory instead of migrating a settings file on read.
        if isinstance(value, dict):
            value.setdefault("paper_automation_symbol_cap", 1000000)
            value.setdefault("paper_gdcrs_guards", {})
            value.setdefault("gdcrs_max_daily_entries_per_symbol", 0)
            value.setdefault("gdcrs_initial_entry_pct", 100)
            value.setdefault("gdcrs_add_on_profit_bps", 0)
        return _validate(value)
    except (OSError, ValueError, TypeError):
        raise SettingsError("프로그램 기본 설정을 읽지 못했습니다.") from None


def effective_cross_guards(value, mode):
    """Paper overrides never replace the stored real/shared strategy settings."""
    keys = ("gdcrs_min_cross_bps", "gdcrs_reentry_cooldown_minutes")
    override = value.get("paper_gdcrs_guards", {})
    if not isinstance(override, dict) or set(override) not in (set(), set(keys)):
        raise SettingsError("모의 전용 과매매 방지 설정 형식을 확인하세요.")
    if mode not in {"paper", "real"}:
        raise SettingsError("과매매 방지 설정의 투자 구분을 확인하세요.")
    for source in (value, override):
        for key, maximum in zip(keys, (10000, 1440)):
            if key in source and (type(source[key]) is not int or not 0 <= source[key] <= maximum):
                raise SettingsError("과매매 방지 설정값을 확인하세요.")
    selected = override if mode == "paper" and override else value
    result = tuple(selected.get(key, 0) for key in keys)
    return (10, 0) if effective_paper_experiment(value, mode)["enabled"] else result


def effective_paper_experiment(value, mode):
    """Separate opt-in profile. Real mode does not inspect experiment keys.

    Values describe policy, never account observations or permission to trade.
    Account cash and reservations must be reconciled again before transmission.
    """
    if mode == "real":
        return {"enabled": False}
    if mode != "paper":
        raise SettingsError("모의 실험 설정의 투자 구분을 확인하세요.")
    enabled = value.get("paper_experiment_enabled", False)
    s5_enabled = value.get("paper_experiment_s5_enabled", False)
    if type(enabled) is not bool or type(s5_enabled) is not bool:
        raise SettingsError("모의 실험 활성화는 참/거짓이어야 합니다.")
    if not enabled:
        return {"enabled": False}
    return {
        "enabled": True, "profile": "paper_experiment_v1",
        "s5_enabled": s5_enabled,
        "assignment_profile": "native-six-s5-v1" if s5_enabled else "native-five-v1",
        "s5_reserved_positions": 2 if s5_enabled else 0,
        "max_concurrent_positions": 40, "max_positions_per_theme": 5,
        "max_overnight_positions": 20, "max_vss_positions": 8,
        "dynamic_candidate_limit": 30, "entry_pool_limit": 40,
        "initial_entry_pct": 100, "add_on_profit_bps": 0,
        "reentry_cooldown_minutes": 0, "daily_buy_order_cap": 0,
        "unfilled_buy_ttl_seconds": 180,
        "per_buy_budget_ceiling_krw": 300000, "cash_budget_divisor": 45,
        "symbol_budget_multiplier": 2, "allow_one_share_over_budget": True,
        "pending_order_scope": "symbol", "unclassified_theme_policy": "allow_unknown",
    }


def effective_b3_guards(value, mode):
    """Optional split/add-on settings apply only to proven paper strategy lots.

    Legacy files are read without migration. Daily entry limits count every buy
    order, including an add-on; these options never raise principal limits.
    Multi-strategy lots retain their original S2/S3/S4 ownership and exits.
    """
    initial = value.get("gdcrs_initial_entry_pct", 100)
    profit = value.get("gdcrs_add_on_profit_bps", 0)
    if (mode not in {"paper", "real"} or type(initial) is not int
            or not 1 <= initial <= 100 or type(profit) is not int
            or not 0 <= profit <= 10000 or profit > 0 and initial == 100):
        raise SettingsError("모의 분할 진입 비율(1~100%)과 증액 기준(0~10000bp)을 확인하세요. 증액은 분할 진입에서만 가능합니다.")
    if effective_paper_experiment(value, mode)["enabled"]:
        return (100, 0)
    return (initial, profit) if mode == "paper" else (100, 0)


def effective_fast_decision(value, mode):
    """Advisory/precomputed paper path; never removes the order safety gateway."""
    enabled = value.get('paper_fast_decision_enabled', False)
    if mode not in {'paper','real'} or type(enabled) is not bool:
        raise SettingsError('모의 사전 계산 옵션은 참/거짓이어야 합니다.')
    return enabled if mode == 'paper' else False


def effective_toss_hybrid(value, mode):
    """Local read-only candidate feed only; the receiver never authenticates Toss."""
    enabled = value.get("paper_toss_hybrid_enabled", False)
    if mode not in {"paper", "real"} or type(enabled) is not bool:
        raise SettingsError("토스 보조 후보 연결은 모의 전용 참/거짓 설정이어야 합니다.")
    return enabled if mode == "paper" else False


def effective_unfilled_buy_ttl(value, mode):
    """Paper-only opt-in, measured from receipt registration, never intent time.

    Zero preserves the original no-cancellation/no-extra-query behavior. This
    option covers exactly owned gdcrs/B3 and S2/S3/S4 limit buys. Manual and
    other strategy orders are excluded. Reading older settings never rewrites it.
    """
    if mode == "real":
        return 0
    if effective_paper_experiment(value, mode)["enabled"]:
        return 180
    seconds = value.get("gdcrs_unfilled_buy_ttl_seconds", 0)
    if mode not in {"paper", "real"} or type(seconds) is not int or not 0 <= seconds <= 86400:
        raise SettingsError("미체결 매수 만료 시간은 0~86400초 정수여야 합니다.")
    return seconds if mode == "paper" else 0


def effective_entry_time_window(value, mode):
    """KST initial golden-cross BUY window; exits and B3 adds are independent.

    Paper-only options are separate from collector/shared settings. The new
    paper default stops initial entries at 15:20; the former monitor permitted
    them until 15:30. Real mode retains that original regular-hours boundary.
    """
    start,end=(value.get(key,default) for key,default in _ENTRY_TIME_DEFAULTS.items())
    if (mode not in {"paper", "real"}
            or any(not isinstance(v,str) or re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]",v) is None for v in (start,end))
            or start>=end or max("09:00",start)>=min("15:30",end)):
        raise SettingsError("모의 최초 진입 시간은 정규장과 겹치는 같은 날 HH:MM 시작·종료 시각이어야 합니다.")
    return (start,end) if mode=="paper" else ("09:00","15:30")


def effective_multi_strategies(value, mode):
    """Explicit paper opt-in; legacy reads do not migrate or enable anything.

    Strategy selection does not grant a separate budget: all automated paper orders share the policy
    and ledger's per-symbol principal cap, including outstanding intents.
    """
    enabled = value.get("paper_multi_strategy_enabled", False)
    selected = value.get("paper_multi_strategy_ids", [])
    if (mode not in {"paper", "real"} or type(enabled) is not bool
            or not isinstance(selected, list)
            or any(not isinstance(x, str) or x not in PAPER_MULTI_STRATEGIES for x in selected)
            or len(set(selected)) != len(selected)
            or enabled and not selected):
        raise SettingsError("모의 다중 전략 활성화 및 전략 목록을 확인하세요.")
    return tuple(x for x in PAPER_MULTI_STRATEGIES if x in selected) if enabled and mode == "paper" else ()


def effective_paper_scan(value, mode):
    """Bounded paper universe; membership still requires fresh provider proof.

    The position limit is activated with the extended paper universe and applies
    to every paper strategy at the ledger reservation, including legacy gdcrs.
    The entry-pool default preserves the former expanded-universe cap of 30.
    An explicit value up to 40 enlarges observation capacity, not order budgets
    or the independent simultaneous-position limit. Owned exits are outside it.
    Legacy/real reads neither enable scanning nor change shared settings.
    """
    enabled = value.get("paper_dynamic_scan_enabled", False)
    limit = value.get("paper_dynamic_candidate_limit", 20)
    stocks = value.get("paper_strategy_stocks", [])
    cap = value.get("paper_max_concurrent_positions", 8)
    entry_pool_limit = value.get("paper_strategy_entry_pool_limit", 30)
    if (mode not in {"paper", "real"} or type(enabled) is not bool
            or type(limit) is not int or not 15 <= limit <= 30
            or type(cap) is not int or not 1 <= cap <= 10
            or type(entry_pool_limit) is not int or not 1 <= entry_pool_limit <= 40
            or not isinstance(stocks, list) or len(stocks) > min(25, entry_pool_limit)):
        raise SettingsError("모의 동적 후보(15~30)·진입 감시 풀(1~40)·동시 보유(1~10)·기본 풀(최대25, 감시 풀 이하)을 확인하세요.")
    seen = set()
    normalized = []
    for row in stocks:
        if (not isinstance(row, dict) or set(row) != {"code", "budget"}
                or not isinstance(row["code"], str) or re.fullmatch(r"[0-9]{6}", row["code"]) is None
                or row["code"] in seen or type(row["budget"]) is not int
                or not 1 <= row["budget"] <= 1_000_000):
            raise SettingsError("모의 기본 풀 종목코드·종목당 100만원 이하 예산을 확인하세요.")
        seen.add(row["code"])
        normalized.append((row["code"], row["budget"]))
    experiment = effective_paper_experiment(value, mode)
    if experiment["enabled"]:
        enabled, limit, cap, entry_pool_limit = True, 30, 40, 40
    return {"enabled": enabled and mode == "paper", "limit": limit,
            "stocks": tuple(normalized) if mode == "paper" else (),
            "entry_pool_limit": entry_pool_limit if mode == "paper" else 0,
            "max_positions": cap if mode == "paper" and (enabled or stocks or effective_paper_funnel(value, mode)["enabled"]) else 0}


def effective_theme_cap(value, mode):
    """Paper-only all-strategy cap, 0 explicitly disables it.

    Normalized settings default to 2. Raw legacy injected configurations with
    no field retain their previous behavior until migrated by load_settings.
    """
    limit = value.get('max_positions_per_theme', 0)
    if mode not in {'paper', 'real'} or type(limit) is not int or not 0 <= limit <= 10:
        raise SettingsError('동일 대표 테마 동시 보유 상한은 0~10 정수여야 합니다.')
    return 5 if effective_paper_experiment(value, mode)["enabled"] else (limit if mode == 'paper' else 0)


def effective_theme_unclassified_policy(value, mode):
    """Unknown theme handling is paper-only; real always keeps blocking."""
    policy = value.get('paper_theme_unclassified_policy', 'block')
    if mode not in {'paper', 'real'} or policy not in ('block', 'shared_bucket'):
        raise SettingsError('미분류 테마 정책은 block 또는 shared_bucket이어야 합니다.')
    return 'allow_unknown' if effective_paper_experiment(value, mode)["enabled"] else (policy if mode == 'paper' else 'block')


def effective_paper_funnel(value, mode):
    """Opt-in ranking-first selection; a hot list is not order permission."""
    enabled = value.get('paper_funnel_enabled', False)
    limit = value.get('paper_funnel_hot_limit', 10)
    priority = value.get('paper_funnel_priority_symbols', [])
    if (mode not in {'paper', 'real'} or type(enabled) is not bool
            or type(limit) is not int or not 1 <= limit <= 15
            or not isinstance(priority, list) or len(priority) > 100
            or any(not isinstance(code,str) or re.fullmatch(r'[0-9]{6}',code) is None for code in priority)
            or len(set(priority)) != len(priority)):
        raise SettingsError('모의 퍼널 활성화와 hot 후보 수(1~15)를 확인하세요.')
    return {'enabled': enabled and mode == 'paper', 'hot_limit': limit,
            'priority_symbols': tuple(priority) if mode == 'paper' else ()}


def load_settings(path: Path | None = None, *, mode=None) -> dict:
    """Load shared settings; explicit real mode never opens the paper sidecar."""
    if mode not in (None, "paper", "real"):
        raise SettingsError("설정 조회의 투자 구분을 확인하세요.")
    try:
        defaults = default_settings()
        path = path or settings_path()
        overrides = json.loads(path.read_text()) if path.exists() else {}
        if not isinstance(overrides, dict) or set(overrides) - set(defaults):
            raise SettingsError("일반 설정에는 지원되는 항목만 저장할 수 있습니다.")
        if mode == "real":
            # The experiment flag is receiver-only. Never read its sidecar or
            # apply a legacy misplaced shared-file value to a real session.
            selected = {key: value for key, value in overrides.items() if key not in _MULTI_KEYS}
            return _validate({**defaults, **selected, "paper_experiment_enabled": False, "paper_experiment_s5_enabled": False})
        option_path = paper_strategy_options_path(path)
        if option_path.is_symlink():
            raise SettingsError("모의 전략 설정의 심볼릭 링크를 허용하지 않습니다.")
        if option_path.exists():
            options = json.loads(option_path.read_text())
            if (not isinstance(options, dict) or not _LEGACY_MULTI_KEYS <= set(options)
                    or set(options) - _MULTI_KEYS
                    or any(key in overrides and overrides[key] != options[key] for key in options)):
                raise SettingsError("모의 전략 별도 설정이 일반 설정과 상충합니다.")
            overrides = {**overrides, **options}
        return _validate({**defaults, **overrides})
    except (OSError, ValueError, TypeError, RecursionError):
        raise SettingsError("프로그램 일반 설정을 읽지 못했습니다.") from None


@registered_mutator('settings_write','settings',error_type=SettingsError)
def _write_json(settings: dict, path: Path) -> dict:
    temporary = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, temporary = tempfile.mkstemp(prefix=".settings-", dir=path.parent)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(settings, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        sync_directory(path.parent)
        return settings
    except OSError:
        raise SettingsError("설정 저장 완료를 확인하지 못했습니다. stts로 현재 설정을 확인하세요.") from None
    finally:
        if temporary is not None:
            try:
                Path(temporary).unlink(missing_ok=True)
            except OSError:
                pass


def _write_settings(settings: dict, path: Path) -> dict:
    # The already-running collector has a strict legacy key set. Never put
    # receiver-only options into its shared settings file, even when an
    # unrelated setting is subsequently saved from the new receiver.
    _write_json({k:v for k,v in settings.items() if k not in _MULTI_KEYS}, path)
    return settings


def save_paper_strategy_options(updates: dict, path: Path | None = None) -> dict:
    """Atomic receiver opt-in without rewriting the collector's shared file."""
    path = path or settings_path()
    if not isinstance(updates, dict) or set(updates) - _MULTI_KEYS:
        raise SettingsError("모의 다중 전략 옵션만 별도 파일에 저장할 수 있습니다.")
    value = _validate({**load_settings(path), **updates})
    option_path = paper_strategy_options_path(path)
    if option_path.is_symlink():
        raise SettingsError("모의 전략 설정의 심볼릭 링크를 허용하지 않습니다.")
    _write_json({k:value[k] for k in sorted(_MULTI_KEYS)}, option_path)
    return load_settings(path)


def save_settings(updates: dict, path: Path | None = None) -> dict:
    path = path or settings_path()
    if not isinstance(updates, dict):
        raise SettingsError("설정 변경 내용을 확인하세요.")
    if set(updates) & _MULTI_KEYS:
        if set(updates) - _MULTI_KEYS:
            current = load_settings(path)
            if any(updates[k] != current[k] for k in set(updates) & _MULTI_KEYS):
                raise SettingsError("일반 설정과 모의 다중 전략 옵션을 한 번에 변경할 수 없습니다.")
            # Existing callers may round-trip the complete loaded dictionary.
            # An unchanged option snapshot is not a second-file mutation.
            return _write_settings(_validate({**current, **updates}), path)
        return save_paper_strategy_options(updates, path)
    return _write_settings(_validate({**load_settings(path), **updates}), path)


def reset_settings(path: Path | None = None) -> dict:
    path = path or settings_path()
    if paper_strategy_options_path(path).exists():
        save_paper_strategy_options({key:default_settings()[key] for key in _MULTI_KEYS}, path)
    return _write_settings(default_settings(), path)


def save_renewal_time(value: str, path: Path | None = None) -> None:
    save_settings({"login_renewal_time": value}, path)
