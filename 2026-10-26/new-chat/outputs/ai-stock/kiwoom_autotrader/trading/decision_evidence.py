"""Small, immutable decision snapshots; no broker, credentials or file writes.

A source hash identifies code/data, not profitability or an authenticated account.
Injected data defaults to unknown; tests may explicitly mark synthetic inputs.
Missing historical evidence is never reconstructed from today's settings.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import lru_cache
import hashlib
import json
from pathlib import Path
import re

# Two 61-bar windows can include up to ten explicitly evidenced no-trade
# assumptions each. Keep a hard bound without rejecting valid 60-bar rollover.
MAX_EVIDENCE_BYTES = 65536
UNSPECIFIED_PRICE = object()
SOURCE_KINDS = {"broker_observation", "synthetic", "unknown"}
_FIELDS = {
    "schema_version", "strategy", "strategy_version", "strategy_source_sha256", "run_id",
    "run_scope", "strategy_source_scope", "source_hash_scope", "config", "config_sha256", "evaluated_at", "observed_at", "candle_start", "candle_end",
    "interval_seconds", "short_period", "long_period", "budget", "source_kind", "source_provider",
    "source_api", "source_exchange", "source_time_semantics", "source_revision", "source_sha256",
    "previous_difference", "difference", "bars", "rollover_revalidation", "b3_addition", "multi_strategy_signal", "dynamic_s1_admission",
}
_CONFIG_KEYS = {"enabled", "short_period", "long_period", "market_start_time", "market_end_time", "stocks",
                "entry_start_time", "entry_end_time",
                "min_cross_bps", "reentry_cooldown_minutes", "max_daily_entries_per_symbol",
                "b3_stage", "b3_initial_intent_id", "b3_initial_entry_pct", "b3_add_on_profit_bps", "b3_total_budget_krw",
                "multi_exit_kind", "multi_position_generation_id",
                "multi_b3_stage", "multi_b3_initial_intent_id", "multi_b3_initial_entry_pct",
                "multi_b3_add_on_profit_bps", "multi_b3_total_budget_krw"}
_EXPERIMENT_CONFIG = {'experiment_arm', 'experiment_assignment_sha256',
    'experiment_first_seen_at', 'experiment_signal_at', 'experiment_signal_sha256',
    'experiment_profile_version'}
_CONFIG_KEYS |= _EXPERIMENT_CONFIG
_CONFIG_KEYS |= {'experiment_extended_admission','experiment_order_window_end_at','experiment_signal_price','experiment_exit_reason',
    'experiment_signal_price_basis','experiment_selection_universe_sha256','experiment_selection_available_at'}
_CONFIG_KEYS |= {'experiment_s5_signal','experiment_s5_exit','experiment_assignment','experiment_d_exit',
    'ownership_status','profit_attribution','manual_protective_exit','manual_confirmed_quantity'}


class DecisionEvidenceError(ValueError):
    """Safe local validation error without supplied values."""


def _fail():
    raise DecisionEvidenceError("주문 판단 근거의 형식·범위 또는 시각을 확인하지 못했습니다.")


def canonical_json(value, *, maximum=MAX_EVIDENCE_BYTES):
    try:
        data = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False)
        if len(data.encode("utf-8")) > maximum:
            _fail()
        return data
    except (TypeError, ValueError, OverflowError, RecursionError):
        _fail()


def json_sha256(value):
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _time(value):
    if not isinstance(value, str) or len(value) > 40:
        _fail()
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        _fail()
    if moment.tzinfo is None or moment.utcoffset() is None:
        _fail()
    return moment


def _integer(value, low, high):
    if type(value) is not int or not low <= value <= high:
        _fail()


def _serialize_bar(bar):
    result = {"start": bar.start.isoformat(), "close": bar.close}
    proof = getattr(bar, "gap_proof", None)
    if proof is not None:
        result["source_inference"] = {
            "method": "same_page_krx_zero_volume_assumption_v1",
            "provider_gap_semantics": "unverified", "exchange": "KRX",
            "symbol": proof.symbol, "mode": proof.mode,
            "previous_start": proof.previous_start.isoformat(), "previous_close": proof.previous_close,
            "previous_volume": proof.previous_volume, "previous_acc_volume": proof.previous_acc_volume,
            "next_start": proof.next_start.isoformat(), "next_close": proof.next_close,
            "next_volume": proof.next_volume, "next_acc_volume": proof.next_acc_volume,
            "response_sha256": proof.response_sha256,
            "query_started_at": proof.query_started_at.isoformat(),
            "same_response_page_adjacent": True, "adjustment_markers_empty": True,
        }
    elif getattr(bar, "source_symbol", None) is not None:
        result["source_observation"] = {
            "symbol": bar.source_symbol, "mode": bar.source_mode, "exchange": "KRX",
            "volume": bar.volume, "acc_volume": bar.acc_volume,
            "adjustment": bar.adjustment, "adjustment_rate": bar.adjustment_rate,
            "query_started_at": bar.source_query_started_at.isoformat() if bar.source_query_started_at else None,
        }
    return result


def _validate_bar_rows(rows, interval, observed_at):
    if not isinstance(rows, list) or len(rows) > 61:
        _fail()
    for row in rows:
        if not isinstance(row, dict) or set(row) not in ({"start", "close"},
                {"start", "close", "source_observation"}, {"start", "close", "source_inference"}):
            _fail()
        start = _time(row["start"])
        _integer(row["close"], 1, 999999999999)
        source = row.get("source_observation")
        if source is not None:
            if (not isinstance(source, dict) or set(source) != {"symbol", "mode", "exchange", "volume", "acc_volume", "adjustment", "adjustment_rate", "query_started_at"}
                    or not isinstance(source["symbol"], str) or re.fullmatch(r"[0-9]{6}", source["symbol"]) is None
                    or source["mode"] not in ("paper", "real") or source["exchange"] != "KRX"):
                _fail()
            query_started_at = _time(source["query_started_at"])
            if observed_at is None or query_started_at > observed_at or start + timedelta(minutes=1) > query_started_at:
                _fail()
            for name in ("volume", "acc_volume"):
                if source[name] is not None:
                    _integer(source[name], 0, 10**19 - 1)
            for name in ("adjustment", "adjustment_rate"):
                value = source[name]
                if value is not None and (not isinstance(value, str) or len(value) > 20 or value and not value.isprintable()):
                    _fail()
        proof = row.get("source_inference")
        if proof is not None:
            required = {"method", "provider_gap_semantics", "exchange", "symbol", "mode", "previous_start", "previous_close", "previous_volume", "previous_acc_volume", "next_start", "next_close", "next_volume", "next_acc_volume", "response_sha256", "query_started_at", "same_response_page_adjacent", "adjustment_markers_empty"}
            if (not isinstance(proof, dict) or set(proof) != required
                    or proof["method"] != "same_page_krx_zero_volume_assumption_v1"
                    or proof["provider_gap_semantics"] != "unverified" or proof["exchange"] != "KRX"
                    or proof["mode"] != "paper" or not isinstance(proof["symbol"], str)
                    or re.fullmatch(r"[0-9]{6}", proof["symbol"]) is None
                    or not isinstance(proof["response_sha256"], str)
                    or re.fullmatch(r"[0-9a-f]{64}", proof["response_sha256"]) is None
                    or proof["same_response_page_adjacent"] is not True or proof["adjustment_markers_empty"] is not True):
                _fail()
            previous, following = _time(proof["previous_start"]), _time(proof["next_start"])
            query_started_at = _time(proof["query_started_at"])
            for name in ("previous_close", "next_close"):
                _integer(proof[name], 1, 999999999999)
            for name in ("previous_volume", "previous_acc_volume", "next_volume", "next_acc_volume"):
                _integer(proof[name], 1, 10**19 - 1)
            if (not previous < start < following or following - previous not in (timedelta(minutes=2), timedelta(minutes=3), timedelta(minutes=4))
                    or start - previous not in (timedelta(minutes=1), timedelta(minutes=2), timedelta(minutes=3))
                    or previous.date() != start.date() or start.date() != following.date()
                    or previous.utcoffset() != timedelta(hours=9) or following.utcoffset() != timedelta(hours=9)
                    or (previous.hour, previous.minute) < (9, 0) or (following.hour, following.minute) >= (15, 20)
                    or observed_at is None or following + timedelta(minutes=1) > query_started_at or query_started_at > observed_at
                    or row["close"] != proof["previous_close"]
                    or proof["previous_volume"] > proof["previous_acc_volume"]
                    or proof["next_acc_volume"] - proof["previous_acc_volume"] != proof["next_volume"]):
                _fail()
    starts = [_time(row["start"]) for row in rows]
    if any(right <= left for left, right in zip(starts, starts[1:])):
        _fail()
    if interval is not None and any(right - left != timedelta(seconds=interval) for left, right in zip(starts, starts[1:])):
        _fail()
    return starts


def _validate(payload):
    if not isinstance(payload, dict) or set(payload) - _FIELDS:
        _fail()
    out = {key: payload.get(key) for key in _FIELDS if key not in {"b3_addition", "multi_strategy_signal", "dynamic_s1_admission"} or key in payload}
    out["schema_version"] = payload.get("schema_version", 1)
    out["source_kind"] = payload.get("source_kind", "unknown")
    if out["schema_version"] != 1 or type(out["schema_version"]) is not int or not isinstance(out["source_kind"], str) or out["source_kind"] not in SOURCE_KINDS:
        _fail()
    for key in ("strategy", "strategy_version", "run_id", "source_provider", "source_api", "source_exchange", "source_revision"):
        if out[key] is not None and (not isinstance(out[key], str) or re.fullmatch(r"[A-Za-z0-9_.:#/-]{1,100}", out[key]) is None):
            _fail()
    for key in ("strategy_source_sha256", "source_sha256", "config_sha256"):
        if out[key] is not None and (not isinstance(out[key], str) or re.fullmatch(r"[0-9a-f]{64}", out[key]) is None):
            _fail()
    for key, allowed in (("run_scope", {None, "monitor_instance"}),
                         ("strategy_source_scope", {None, "monitor_module"}),
                         ("source_hash_scope", {None, "normalized_completed_window"})):
        if not isinstance(out[key], (str, type(None))) or out[key] not in allowed:
            _fail()
    times = {key: _time(out[key]) for key in ("evaluated_at", "observed_at", "candle_start", "candle_end") if out[key] is not None}
    if ("candle_start" in times and "candle_end" in times and times["candle_start"] >= times["candle_end"]
            or "observed_at" in times and "evaluated_at" in times and times["observed_at"] > times["evaluated_at"]
            or "candle_end" in times and "observed_at" in times and times["candle_end"] > times["observed_at"]):
        _fail()
    for key, low, high in (("interval_seconds", 1, 86400), ("short_period", 1, 60), ("long_period", 1, 60),
                           ("budget", 1, 999999999999), ("previous_difference", -10**18, 10**18), ("difference", -10**18, 10**18)):
        if out[key] is not None:
            _integer(out[key], low, high)
    if out["short_period"] is not None and out["long_period"] is not None and out["short_period"] >= out["long_period"]:
        _fail()
    if out["source_time_semantics"] not in (None, "start", "end", "unknown"):
        _fail()
    config = out["config"]
    if config is not None:
        if not isinstance(config, dict) or set(config) - _CONFIG_KEYS:
            _fail()
        if _EXPERIMENT_CONFIG.intersection(config):
            if (not _EXPERIMENT_CONFIG.issubset(config)
                    or config['experiment_profile_version'] != 'paper-experiment-v1'
                    or config['experiment_arm'] not in ('A','B','B-TP','E','R','C','D','S2','S3','S4','S5',
                        'ORB-a','ORB-b','GDR','VSS','LDR','ETF-B')):
                _fail()
            if 'experiment_order_window_end_at' in config:
                if _time(config['experiment_order_window_end_at'])<=times['observed_at']:_fail()
            if 'experiment_signal_price' in config:_integer(config['experiment_signal_price'],1,10**12)
            if 'experiment_exit_reason' in config:
                if not isinstance(config['experiment_exit_reason'],str) or not 0<len(config['experiment_exit_reason'])<=120:_fail()
            if 'experiment_signal_price_basis' in config:
                if not isinstance(config['experiment_signal_price_basis'],str) or len(config['experiment_signal_price_basis'])>160:_fail()
            if 'experiment_selection_universe_sha256' in config:
                if not isinstance(config['experiment_selection_universe_sha256'],str) or re.fullmatch('[0-9a-f]{64}',config['experiment_selection_universe_sha256']) is None:_fail()
                if _time(config['experiment_selection_available_at'])>times['observed_at']:_fail()
            if 'experiment_extended_admission' in config:
                from .paper_experiment_admission import validate_admission
                arms={'ORB-a':'EXP_ORB_A','ORB-b':'EXP_ORB_B','GDR':'EXP_GDR','VSS':'EXP_VSS','LDR':'EXP_LDR'}
                admission=config['experiment_extended_admission']
                try:validate_admission(admission,admission.get('symbol'),arms.get(config['experiment_arm']),times['observed_at'])
                except (ValueError,TypeError,AttributeError):_fail()
            for key in ('experiment_assignment_sha256','experiment_signal_sha256'):
                if not isinstance(config[key], str) or re.fullmatch(r'[0-9a-f]{64}', config[key]) is None:
                    _fail()
            first_seen = _time(config['experiment_first_seen_at'])
            signal_at = _time(config['experiment_signal_at'])
            if ('observed_at' not in times or first_seen > times['observed_at']
                    or signal_at > times['observed_at'] or first_seen.date() > signal_at.date()):
                _fail()
        if ('experiment_d_exit' in config or out.get('strategy') == 'EXP_D'
                and config.get('experiment_exit_reason') == 'late_recovery_exit'):
            # A new late exit is a current observation of an original owned
            # generation, never a rewritten opening signal or reassignment.
            proof = config.get('experiment_d_exit')
            fields = {'schema', 'kind', 'generation_id', 'first_fill_at', 'signal_at', 'session_day',
                      'regular_start', 'regular_end', 'calendar_source_sha256', 'observed_session_open_at'}
            if (out.get('strategy') != 'EXP_D' or out.get('strategy_version') != 'paper-experiment-v1'
                    or out.get('source_kind') != 'broker_observation' or out.get('source_provider') != 'kiwoom'
                    or out.get('source_exchange') != 'KRX' or not _EXPERIMENT_CONFIG.issubset(config)
                    or config.get('experiment_arm') != 'D' or config.get('multi_exit_kind') != 'full'
                    or not isinstance(proof, dict) or set(proof) != fields
                    or proof.get('schema') != 'exp-d-owned-exit-v1'
                    or proof.get('kind') not in ('next_session_open', 'late_recovery_exit')
                    or proof.get('kind') != config.get('experiment_exit_reason')
                    or proof.get('generation_id') != config.get('multi_position_generation_id')
                    or not isinstance(proof.get('generation_id'), str)
                    or re.fullmatch('[0-9a-f]{32}', proof['generation_id']) is None
                    or not isinstance(proof.get('calendar_source_sha256'), str)
                    or re.fullmatch('[0-9a-f]{64}', proof['calendar_source_sha256']) is None):
                _fail()
            try:
                from .paper_experiment_rules import validate_assignment
                assignment = config['experiment_assignment']; observed = times['observed_at']
                validate_assignment(assignment, symbol=assignment['symbol'], day=assignment['day'], now=observed)
                first_fill = _time(proof['first_fill_at']); signal = _time(proof['signal_at'])
                start = _time(proof['regular_start']); end = _time(proof['regular_end'])
                window = _time(config['experiment_order_window_end_at'])
                if (assignment['arm'] != 'EXP_D' or assignment['assignment_sha256'] != config['experiment_assignment_sha256']
                        or assignment['first_seen_at'] != config['experiment_first_seen_at']
                        or assignment['day'] != first_fill.strftime('%Y%m%d')
                        or not _time(assignment['assigned_at']) <= first_fill < start
                        or proof['signal_at'] != config['experiment_signal_at']
                        or _time(proof['observed_session_open_at']) != start
                        or any(moment.utcoffset() != timedelta(hours=9) for moment in (first_fill, signal, start, end, observed, window))
                        or not start.date() == end.date() == signal.date() == observed.date()
                        or proof['session_day'] != observed.strftime('%Y%m%d')
                        or first_fill.date() >= observed.date() or not start <= signal <= observed < window <= end
                        or proof['kind'] == 'next_session_open' and (not signal < start + timedelta(minutes=1)
                            or window != start + timedelta(minutes=1))
                        or proof['kind'] == 'late_recovery_exit' and signal < start + timedelta(minutes=1)):
                    _fail()
            except (ValueError, KeyError, TypeError, AttributeError, OverflowError):
                _fail()
        if {'ownership_status','profit_attribution','manual_protective_exit','manual_confirmed_quantity'} & set(config):
            if (config.get('ownership_status')!='exit_only_broker_confirmed' or config.get('profit_attribution')!='unconfirmed'
                    or out.get('source_kind')!='broker_observation'):_fail()
            if {'manual_protective_exit','manual_confirmed_quantity'} & set(config):
                if (config.get('manual_protective_exit') is not True or out.get('strategy')!='manual_protective_exit'
                        or set(config)!={'ownership_status','profit_attribution','manual_protective_exit','manual_confirmed_quantity'}):_fail()
                _integer(config.get('manual_confirmed_quantity'),1,10**12)
            elif config.get('multi_exit_kind')!='full':_fail()
        native_s5=(out.get('strategy')=='S5_steady_uptrend_vwap' and out.get('strategy_version')=='paper-experiment-v1')
        if native_s5 or {'experiment_s5_signal','experiment_s5_exit'} & set(config):
            if (not native_s5 or not _EXPERIMENT_CONFIG.issubset(config)
                    or config['experiment_arm']!='S5' or out['source_kind']!='broker_observation'
                    or ('experiment_s5_signal' in config)==('experiment_s5_exit' in config)):_fail()
            from .paper_experiment_rules import validate_assignment
            assignment=config.get('experiment_assignment')
            try:
                validate_assignment(assignment,symbol=assignment['symbol'],day=assignment['day'],now=times['observed_at'])
                if (assignment['arm']!='S5_steady_uptrend_vwap'
                        or assignment['assignment_sha256']!=config['experiment_assignment_sha256']
                        or assignment['first_seen_at']!=config['experiment_first_seen_at']):_fail()
            except (ValueError,KeyError,TypeError,AttributeError,OverflowError):_fail()
            if 'experiment_s5_signal' in config:
                from .paper_experiment_s5 import validate_s5_native_evidence
                signal=config['experiment_s5_signal']
                try:
                    if assignment['day']!=times['observed_at'].strftime('%Y%m%d'):_fail()
                    validate_s5_native_evidence(signal,symbol=assignment['symbol'],now=times['observed_at'],
                                               signal_at=config['experiment_signal_at'])
                except (ValueError,KeyError,TypeError,AttributeError,OverflowError):_fail()
            else:
                exit_proof=config['experiment_s5_exit']
                if (not isinstance(exit_proof,dict) or set(exit_proof)!={'first_fill_at','reason','observed_at'}
                        or exit_proof['reason']!=config.get('experiment_exit_reason')
                        or _time(exit_proof['observed_at'])!=times.get('observed_at')):_fail()
                if exit_proof['first_fill_at'] is None:
                    if (config.get('ownership_status')!='exit_only_broker_confirmed'
                            or exit_proof['reason'] not in ('stop_loss_or_sma20','take_profit_10pct','s5_regular_cutoff_exit')):_fail()
                elif _time(exit_proof['first_fill_at'])>times['observed_at']:_fail()
        if {'multi_exit_kind','multi_position_generation_id'} & set(config):
            if (config.get('multi_exit_kind') not in ('half','full')
                    or not isinstance(config.get('multi_position_generation_id'),str)
                    or re.fullmatch(r'[0-9a-f]{32}',config['multi_position_generation_id']) is None):
                _fail()
        for key in ("short_period", "long_period"):
            if key in config:
                _integer(config[key], 1, 60)
        if "min_cross_bps" in config:
            _integer(config["min_cross_bps"], 0, 10000)
        if "reentry_cooldown_minutes" in config:
            _integer(config["reentry_cooldown_minutes"], 0, 1440)
        if "max_daily_entries_per_symbol" in config:
            _integer(config["max_daily_entries_per_symbol"], 0, 1000)
        b3_keys = {"b3_stage", "b3_initial_intent_id", "b3_initial_entry_pct", "b3_add_on_profit_bps", "b3_total_budget_krw"}
        if b3_keys.intersection(config):
            if not b3_keys.issubset(config) or config["b3_stage"] not in ("initial", "add"):
                _fail()
            _integer(config["b3_initial_entry_pct"], 1, 99)
            _integer(config["b3_add_on_profit_bps"], 0, 10000)
            _integer(config["b3_total_budget_krw"], 1, 1000000)
            ident = config["b3_initial_intent_id"]
            if (config["b3_stage"] == "initial" and ident is not None
                    or config["b3_stage"] == "add" and (not isinstance(ident, str)
                        or re.fullmatch(r"[0-9a-f]{32}", ident) is None
                        or config["b3_add_on_profit_bps"] == 0)):
                _fail()
        if "enabled" in config and type(config["enabled"]) is not bool:
            _fail()
        multi_b3_keys = {"multi_b3_stage", "multi_b3_initial_intent_id", "multi_b3_initial_entry_pct",
                         "multi_b3_add_on_profit_bps", "multi_b3_total_budget_krw"}
        if multi_b3_keys.intersection(config):
            if (not multi_b3_keys.issubset(config) or config['multi_b3_stage'] not in ('initial','add')
                    or out.get('strategy_version') not in ('paper-multi-observed-v2','paper-multi-observed-v3','paper-multi-observed-v4')
                    or config.get('max_daily_entries_per_symbol') != 2):
                _fail()
            _integer(config['multi_b3_initial_entry_pct'],1,99)
            _integer(config['multi_b3_add_on_profit_bps'],1,10000)
            _integer(config['multi_b3_total_budget_krw'],1,1_000_000)
            ident = config['multi_b3_initial_intent_id']
            if ((config['multi_b3_stage']=='initial' and ident is not None)
                    or (config['multi_b3_stage']=='add' and (not isinstance(ident,str)
                        or re.fullmatch(r'[0-9a-f]{32}',ident) is None))):
                _fail()
        for key in ("market_start_time", "market_end_time", "entry_start_time", "entry_end_time"):
            if key in config and (not isinstance(config[key], str) or re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", config[key]) is None):
                _fail()
        entry_keys={"entry_start_time","entry_end_time"}
        if entry_keys.intersection(config) and (not entry_keys.issubset(config)
                or config["entry_start_time"]>=config["entry_end_time"]):
            _fail()
        if "stocks" in config:
            if not isinstance(config["stocks"], list) or len(config["stocks"]) > 100:
                _fail()
            for row in config["stocks"]:
                if (not isinstance(row, dict) or set(row) != {"code", "budget"}
                        or not isinstance(row["code"], str) or re.fullmatch(r"[0-9]{6}", row["code"]) is None):
                    _fail()
                _integer(row["budget"], 1, 999999999999)
        actual = json_sha256(config)
        if out["config_sha256"] not in (None, actual):
            _fail()
        out["config_sha256"] = actual
        for key in ("short_period", "long_period"):
            if out[key] is not None and key in config and config[key] != out[key]:
                _fail()
    if "candle_start" in times and "candle_end" in times and out["interval_seconds"] is not None:
        if times["candle_end"] - times["candle_start"] != timedelta(seconds=out["interval_seconds"]):
            _fail()
    bars = out["bars"]
    if bars is not None:
        # Only the versioned multi-strategy policy may use a bounded count of
        # actual bars. Its independent timeline validator below proves gaps;
        # legacy golden-cross/B3 evidence keeps exact minute continuity.
        actual_window_v2 = out.get('strategy_version') in ('paper-multi-observed-v2','paper-multi-observed-v3','paper-multi-observed-v4','paper-experiment-v1')
        starts = _validate_bar_rows(bars, None if actual_window_v2 else out["interval_seconds"], times.get("observed_at"))
        if starts and "candle_start" in times and starts[-1] != times["candle_start"]:
            _fail()
        actual = json_sha256(bars)
        if out["source_sha256"] not in (None, actual):
            _fail()
        out["source_sha256"] = actual
    b3 = out.get("b3_addition")
    if b3 is not None:
        if (not isinstance(b3, dict) or set(b3) != {"initial_filled_qty", "initial_filled_principal_krw",
                "initial_fill_last_observed_at", "volume_prior_sum", "volume_current", "source_page_sha256", "trend_bars"}
                or not isinstance(config, dict) or config.get("b3_stage") != "add"
                or out["strategy"] != "gdcrs" or out["strategy_version"] != "paper-b3-add-v1"
                or out["previous_difference"] is not None or out["difference"] is not None
                or not isinstance(bars, list) or len(bars) != 21):
            _fail()
        for key in ("initial_filled_qty", "initial_filled_principal_krw", "volume_prior_sum"):
            _integer(b3[key], 1, 10**19-1)
        _integer(b3["volume_current"], 0, 10**19-1)
        if (not isinstance(b3["source_page_sha256"], list) or not 1 <= len(b3["source_page_sha256"]) <= 3
                or len(set(b3["source_page_sha256"])) != len(b3["source_page_sha256"])
                or any(not isinstance(h, str) or re.fullmatch(r"[0-9a-f]{64}", h) is None for h in b3["source_page_sha256"])):
            _fail()
        last_fill = _time(b3["initial_fill_last_observed_at"])
        trend = b3["trend_bars"]
        trend_starts = _validate_bar_rows(trend, 60, times.get("observed_at"))
        long = out["long_period"]; short = out["short_period"]
        latest_source = bars[-1].get("source_observation")
        if not isinstance(latest_source, dict):
            _fail()
        symbol = latest_source.get("symbol")
        trend_by_start = {row["start"]: row for row in trend}
        if (not isinstance(long, int) or not isinstance(short, int) or len(trend) < long
                or not trend_starts or trend_starts[-1] != times.get("candle_start")
                or trend_starts[0] != last_fill.replace(second=0, microsecond=0)-timedelta(minutes=long-1)
                or any("source_observation" not in row or row["source_observation"]["mode"] != "paper"
                       or row["source_observation"]["symbol"] != symbol
                       or row["source_observation"]["adjustment"] != ""
                       or row["source_observation"]["adjustment_rate"] != "" for row in trend)
                or any(sum(row["close"] for row in trend[i-short+1:i+1])*long
                       - sum(row["close"] for row in trend[i-long+1:i+1])*short < 0
                       for i in range(long-1,len(trend)))):
            _fail()
        if ("candle_end" not in times or last_fill >= times["candle_end"]
                or any("source_observation" not in row or row["source_observation"]["mode"] != "paper"
                       or row["source_observation"]["symbol"] != symbol
                       or row["source_observation"]["adjustment"] != ""
                       or row["source_observation"]["adjustment_rate"] != ""
                       or type(row["source_observation"]["volume"]) is not int for row in bars)
                or any(row["start"] in trend_by_start and row != trend_by_start[row["start"]] for row in bars)
                or sum(row["source_observation"]["volume"] for row in bars[:-1]) != b3["volume_prior_sum"]
                or bars[-1]["source_observation"]["volume"] != b3["volume_current"]
                or 40*b3["volume_current"] < 3*b3["volume_prior_sum"]
                or bars[-1]["close"] * b3["initial_filled_qty"] * 10000
                   < b3["initial_filled_principal_krw"] * (10000 + config["b3_add_on_profit_bps"])):
            _fail()
    elif isinstance(config, dict) and config.get("b3_stage") == "add":
        _fail()
    rollover = out["rollover_revalidation"]
    if rollover is not None:
        if (out["strategy"] != "ddcrs" or not isinstance(rollover, dict)
                or set(rollover) != {"observed_at", "difference", "bars"}
                or bars is None or out["short_period"] is None or out["long_period"] is None
                or out["previous_difference"] is None or out["difference"] is None
                or out["previous_difference"] < 0 or out["difference"] >= 0):
            _fail()
        rollover_at = _time(rollover["observed_at"])
        newer = rollover["bars"]
        newer_starts = _validate_bar_rows(newer, out["interval_seconds"], rollover_at)
        def stable(rows):
            return [{**row,
                     **({"source_inference": {key:value for key,value in row["source_inference"].items() if key not in ("response_sha256", "query_started_at")}}
                        if "source_inference" in row else {}),
                     **({"source_observation": {key:value for key,value in row["source_observation"].items() if key != "query_started_at"}}
                        if "source_observation" in row else {})} for row in rows]
        if (len(bars) != out["long_period"] + 1 or len(newer) != len(bars)
                or stable(newer[:-1]) != stable(bars[1:]) or not newer_starts
                or newer_starts[-1] != starts[-1] + timedelta(minutes=1)
                or "observed_at" not in times or rollover_at <= times["observed_at"]
                or "evaluated_at" not in times or rollover_at > times["evaluated_at"]
                or newer_starts[-1] + timedelta(minutes=1) > rollover_at):
            _fail()
        short, long = out["short_period"], out["long_period"]
        value = sum(row["close"] for row in newer[-short:]) * long - sum(row["close"] for row in newer[-long:]) * short
        if type(rollover["difference"]) is not int or rollover["difference"] != value or value >= 0:
            _fail()
    if "multi_strategy_signal" in out:
        from kiwoom_autotrader.trading.multi_strategy_evidence import validate_multi_signal
        validate_multi_signal(out)
    admission = out.get('dynamic_s1_admission')
    if admission is not None or out.get('strategy_version') == 'completed-minute-cross-funnel-v1':
        from .dynamic_s1_admission import validate_dynamic_admission
        try:
            admission=validate_dynamic_admission(admission,now=times['evaluated_at'])
            symbol=admission['symbol']
            if (out['strategy']!='gdcrs' or out['strategy_version']!='completed-minute-cross-funnel-v1'
                    or out['source_kind']!='broker_observation' or out['source_api']!='ka10080'
                    or out['source_exchange']!='KRX' or not isinstance(config,dict)
                    or config.get('b3_stage')!='initial' or config.get('b3_initial_intent_id') is not None
                    or config.get('b3_total_budget_krw')!=1000000 or config.get('b3_initial_entry_pct')!=50
                    or config.get('b3_add_on_profit_bps',0)<50 or config.get('max_daily_entries_per_symbol')!=2
                    or config.get('min_cross_bps',0)<10 or config.get('reentry_cooldown_minutes',0)<20
                    or out['budget']!=500000 or config.get('enabled') is not True
                    or not isinstance(bars,list) or len(bars)!=out['long_period']+1
                    or times['candle_end']!=times['evaluated_at'].replace(second=0,microsecond=0)
                    or any(row.get('source_observation',row.get('source_inference',{})).get('symbol')!=symbol
                           or row.get('source_observation',row.get('source_inference',{})).get('mode')!='paper' for row in bars)
                    or any(row.get('code')==symbol for row in config.get('stocks',[]))):
                _fail()
            short,long=out['short_period'],out['long_period']
            previous=sum(row['close'] for row in bars[-short-1:-1])*long-sum(row['close'] for row in bars[-long-1:-1])*short
            current=sum(row['close'] for row in bars[-short:])*long-sum(row['close'] for row in bars[-long:])*short
            if (out['previous_difference']!=previous or out['difference']!=current or previous>0 or current<=0
                    or current*10000<config['min_cross_bps']*short*sum(row['close'] for row in bars[-long:])):
                _fail()
            out['dynamic_s1_admission']=admission
        except (ValueError,KeyError,TypeError,OverflowError):
            _fail()
    elif 'dynamic_s1_admission' in out:
        _fail()
    return out


@dataclass(frozen=True)
class DecisionEvidence:
    """Canonical JSON avoids retaining mutable caller dictionaries/lists."""
    json: str

    def __post_init__(self):
        if not isinstance(self.json, str) or len(self.json.encode("utf-8")) > MAX_EVIDENCE_BYTES:
            _fail()
        try:
            payload = json.loads(self.json)
        except (ValueError, RecursionError):
            _fail()
        object.__setattr__(self, "json", canonical_json(_validate(payload)))

    @property
    def sha256(self):
        return hashlib.sha256(self.json.encode("utf-8")).hexdigest()

    def as_dict(self):
        return json.loads(self.json)


def freeze_evidence(value):
    if value is None or isinstance(value, DecisionEvidence):
        return value
    return DecisionEvidence(canonical_json(value))


@lru_cache(maxsize=8)
def strategy_source_hash(path):
    """Read one bounded module once, at monitor construction, never per tick."""
    try:
        with Path(path).open("rb") as source:
            data = source.read(1024 * 1024 + 1)
        return hashlib.sha256(data).hexdigest() if len(data) <= 1024 * 1024 else None
    except OSError:
        return None  # No inferred version when source bytes cannot be verified.


def cross_evidence(*, strategy, run_id, source_hash, source_kind, config, observed_at,
                   evaluated_at, window, previous_difference, difference, budget=None,
                   rollover_window=None, rollover_observed_at=None, dynamic_s1_admission=None):
    if source_kind not in SOURCE_KINDS:
        _fail()
    if (rollover_window is None) != (rollover_observed_at is None):
        _fail()
    broker = source_kind == "broker_observation"
    rollover = None
    if rollover_window is not None:
        short, long = config["short_period"], config["long_period"]
        difference_after = sum(bar.close for bar in rollover_window[-short:]) * long - sum(bar.close for bar in rollover_window[-long:]) * short
        rollover = {"observed_at": rollover_observed_at.isoformat(), "difference": difference_after,
                    "bars": [_serialize_bar(bar) for bar in rollover_window]}
    return freeze_evidence({
        "strategy": strategy, "strategy_version": "completed-minute-cross-funnel-v1" if dynamic_s1_admission is not None else "completed-minute-cross-v1", "run_id": run_id,
        "strategy_source_sha256": source_hash, "config": config,
        "run_scope": "monitor_instance", "strategy_source_scope": "monitor_module",
        "source_hash_scope": "normalized_completed_window",
        "observed_at": observed_at.isoformat(), "evaluated_at": evaluated_at.isoformat(),
        "candle_start": window[-1].start.isoformat(),
        "candle_end": (window[-1].start + timedelta(minutes=1)).isoformat(),
        "interval_seconds": 60, "short_period": config["short_period"], "long_period": config["long_period"],
        "budget": budget, "source_kind": source_kind, "source_provider": "kiwoom-rest" if broker else None,
        "source_api": "ka10080" if broker else None, "source_exchange": "KRX" if broker else None,
        "source_time_semantics": "start", "source_revision": None,
        "previous_difference": previous_difference, "difference": difference,
        "bars": [_serialize_bar(bar) for bar in window], "rollover_revalidation": rollover,
        **({'dynamic_s1_admission':dynamic_s1_admission} if dynamic_s1_admission is not None else {}),
    })


def b3_addition_evidence(*, run_id, source_hash, source_kind, config, observed_at,
                         evaluated_at, window, trend_window, budget, initial):
    """Record an add-on condition, never label it a new crossover."""
    return freeze_evidence({
        "strategy": "gdcrs", "strategy_version": "paper-b3-add-v1", "run_id": run_id,
        "strategy_source_sha256": source_hash, "config": config,
        "run_scope": "monitor_instance", "strategy_source_scope": "monitor_module",
        "source_hash_scope": "normalized_completed_window",
        "observed_at": observed_at.isoformat(), "evaluated_at": evaluated_at.isoformat(),
        "candle_start": window[-1].start.isoformat(),
        "candle_end": (window[-1].start + timedelta(minutes=1)).isoformat(),
        "interval_seconds": 60, "short_period": config["short_period"],
        "long_period": config["long_period"], "budget": budget, "source_kind": source_kind,
        "source_provider": "kiwoom-rest" if source_kind == "broker_observation" else None,
        "source_api": "ka10080" if source_kind == "broker_observation" else None,
        "source_exchange": "KRX" if source_kind == "broker_observation" else None,
        "source_time_semantics": "start", "source_revision": None,
        "previous_difference": None, "difference": None,
        "bars": [_serialize_bar(bar) for bar in window], "rollover_revalidation": None,
        "b3_addition": {"initial_filled_qty": initial["initial_filled_qty"],
            "initial_filled_principal_krw": initial["initial_filled_principal_krw"],
            "initial_fill_last_observed_at": initial["initial_fill_last_observed_at"],
            "volume_prior_sum": sum(bar.volume for bar in window[:-1]),
            "volume_current": window[-1].volume,
            "source_page_sha256": sorted({bar.source_response_sha256 for bar in window}),
            "trend_bars": [_serialize_bar(bar) for bar in trend_window]},
    })
