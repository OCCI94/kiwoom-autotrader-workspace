#!/usr/bin/env python3
"""Offline interface for the existing quant leader formula engines."""
from __future__ import annotations

import argparse
from contextlib import redirect_stdout
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import sysconfig
from tempfile import TemporaryDirectory
from types import ModuleType
from zoneinfo import TZPATH

WORKSPACE = Path(__file__).resolve().parents[3] / "2026-10-26/new-chat"
WORK = WORKSPACE / "work"
QUANT = WORK / "quant-book-analysis-20261105"
RESEARCH = WORKSPACE / "outputs/ai-stock-research"
DART = RESEARCH / "reports/2026-11-05/112549577286-leader-candidates-real-dart-financial-calculation.json"
SAFETY = {"order_permission": False, "recommendation_only": True}
SIZING_MODES = ("shrinked_mv", "robust_mvo", "risk_parity", "proportional")


class UsageParser(argparse.ArgumentParser):
    def error(self, message):
        self.print_usage(sys.stderr)
        self.exit(3, f"{self.prog}: {message}\n")


def install_offline_guard(scratch: Path) -> None:
    """Restrict legacy code to its reviewed sources and temporary output."""
    allowed_reads = {
        DART.resolve(),
        *((RESEARCH / f"{name}.py").resolve() for name in (
            "leader_core", "leader_constants", "leader_valuation", "leader_macro_overseas",
        )),
        *((QUANT / f"{name}.py").resolve() for name in (
            "daily_quant_leader_pipeline", "leader_autotrading_formulas",
            "run_master_quant_leader_verification",
        )),
    }
    standard_roots = {
        Path(sysconfig.get_path("stdlib")).resolve(),
        Path(sysconfig.get_path("platstdlib")).resolve(),
        *(Path(path).resolve() for path in TZPATH),
    }
    blocked = {
        "subprocess.Popen", "os.system", "os.exec", "os.posix_spawn", "os.fork",
        "os.forkpty", "sqlite3.connect", "urllib.Request", "ctypes.dlopen",
    }
    writes = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND

    def guard(event, args):
        if event in blocked or event.startswith("socket."):
            raise PermissionError("offline capability blocked")
        if event == "open" and not isinstance(args[0], int):
            path = Path(os.fsdecode(args[0])).resolve()
            if path.is_relative_to(scratch):
                return
            if args[2] & writes:
                raise PermissionError("write outside temporary directory")
            if path in allowed_reads:
                return
            if "site-packages" not in path.parts and any(
                path.is_relative_to(root) for root in standard_roots
            ):
                return
            raise PermissionError("read outside research allowlist")
        if event in {"os.remove", "os.rmdir", "os.mkdir", "os.chmod", "os.chown", "os.utime", "os.truncate"}:
            if not Path(os.fsdecode(args[0])).resolve().is_relative_to(scratch):
                raise PermissionError("change outside temporary directory")
        if event in {"os.link", "os.symlink", "os.rename", "os.chdir", "os.fchdir"}:
            raise PermissionError("path mutation blocked")

    sys.addaudithook(guard)


def offline_namuh_status(symbols, live_fetch=False):
    if live_fetch:
        raise PermissionError("live fetch disabled")
    return {
        "ready": False, "mode": "offline_no_credentials", "reason": "OFFLINE_RESEARCH_ONLY",
        "order_execution_enabled": False, "live_quotes_fetched": 0, "quotes": {},
    }


def run_legacy(args):
    sys.dont_write_bytecode = True
    sys.path[:0] = [str(QUANT), str(RESEARCH)]
    with TemporaryDirectory(prefix="quant-leader-") as directory:
        scratch = Path(directory).resolve()
        install_offline_guard(scratch)
        # The legacy client probes credentials even in its default offline mode.
        # Exclude its implementation entirely and replace the credential probe.
        blocked_namuh = ModuleType("namuh_readonly_client")
        blocked_namuh.NamuhReadonlyClient = None
        blocked_namuh.NamuhReadonlyError = PermissionError
        sys.modules["namuh_readonly_client"] = blocked_namuh

        import run_master_quant_leader_verification as verifier

        result = {"schema": "quant-leader-skill-v1", "mode": args.mode, **SAFETY}
        if args.mode == "daily-sheet":
            import daily_quant_leader_pipeline as pipeline

            pipeline.check_namuh_and_fetch_optional = offline_namuh_status
            pipeline.QUANT_WORK_DIR = scratch
            sheet = pipeline.run_daily_quant_leader_pipeline(
                nav_krw=args.nav, sizing_mode=args.sizing_mode, sector_cap=args.sector_cap,
                live_namuh=False,
            )["order_sheet"]
            if any(sheet["safety_lock"][name] is not expected for name, expected in SAFETY.items()):
                raise ValueError("legacy safety marker mismatch")
            result.update(
                evidence_kind="saved_dart_and_demonstration_assumptions",
                simulated_inputs=["market_quotes", "sector_gates", "target_prices",
                                  "risk_features", "holdings", "phase8_returns"],
                current_market_assessment=False, order_sheet={**sheet, **SAFETY},
            )
            risks = sheet["phase5_and_6_leverage_sustainability"]
            warnings = [name + "_leverage_not_sustainable" for name, data in risks.items()
                        if data["is_sustainable"] is not True]
            if sheet["phase4_sector_waterfill_summary"]["infeasible"]:
                warnings.append("sector_cap_requires_cash")
        else:
            verification_path = QUANT / "master_verification_report.json"
            verifier.Path = lambda value: (
                scratch / verification_path.name if Path(value) == verification_path else Path(value)
            )
            with redirect_stdout(sys.stderr):
                phases = verifier.run_full_master_verification()
            expected = (
                "phase1_macro_and_sector", "phase2_and_3_leader_alpha_and_gate",
                "phase4_sizing_modes_comparison", "phase4_sector_waterfill_25pct_cap",
                "phase5_and_6_risk_hedge_and_leverage", "phase7_sell_state_machine_and_event_trading",
                "phase8_xse_xstse_attribution",
            )
            if set(phases) != set(expected) or set(phases[expected[2]]) != set(SIZING_MODES):
                raise ValueError("unexpected master phases")
            phase8 = phases[expected[-1]]
            if abs(phase8["idio_pnl"] - sum(phase8[key] for key in (
                "selection_pnl", "sizing_pnl", "timing_pnl"
            ))) > 1.0:
                raise ValueError("fixture attribution identity")
            result["evidence_kind"] = "verification_fixture"
            result["result"] = phase8 if args.mode == "xse-xstse" else phases
            warnings = []  # Master verification exercises both passing and failing scenarios.
        result["warnings"] = warnings
        return result, int(bool(warnings))


def run_saved_attribution(args):
    tool = WORK / "quant_xse_xstse_experiment_audit.py"
    command = [sys.executable, "-I", "-B", str(tool)]
    if args.snapshot:
        command.extend(("--snapshot", args.snapshot))
    else:
        command.extend(("--ledger-copy", args.ledger_copy, "--ledger-manifest", args.ledger_manifest,
                        "--start-day", args.start_day, "--end-day", args.end_day))
    child = subprocess.run(command, env={}, capture_output=True, text=True, check=False, timeout=120)
    if child.returncode not in (0, 1):
        raise ValueError("saved attribution failed")
    result = json.loads(child.stdout)
    if not isinstance(result, dict) or any(result.get(k) is not v for k, v in SAFETY.items()):
        raise ValueError("attribution safety marker mismatch")
    return result, child.returncode


def main(argv=None):
    parser = UsageParser(description=__doc__)
    parser.add_argument("--mode", choices=("verify", "daily-sheet", "xse-xstse"), default="verify")
    parser.add_argument("--nav", type=float, default=100_000_000)
    parser.add_argument("--sizing-mode", choices=SIZING_MODES, default="shrinked_mv")
    parser.add_argument("--sector-cap", type=float, default=0.25)
    sources = parser.add_mutually_exclusive_group()
    sources.add_argument("--snapshot")
    sources.add_argument("--ledger-copy")
    parser.add_argument("--ledger-manifest")
    parser.add_argument("--start-day")
    parser.add_argument("--end-day")
    args = parser.parse_args(argv)
    if not math.isfinite(args.nav) or args.nav <= 0:
        parser.error("--nav must be finite and positive")
    if not 0 < args.sector_cap <= 1:
        parser.error("--sector-cap must be in (0, 1]")
    copy_parameters = (args.ledger_manifest, args.start_day, args.end_day)
    if bool(args.ledger_copy) != all(copy_parameters) or (not args.ledger_copy and any(copy_parameters)):
        parser.error("--ledger-copy requires --ledger-manifest, --start-day, and --end-day")
    if args.mode != "xse-xstse" and (args.snapshot or args.ledger_copy):
        parser.error("saved attribution inputs require --mode xse-xstse")
    try:
        result, status = run_saved_attribution(args) if args.snapshot or args.ledger_copy else run_legacy(args)
        output = json.dumps(result, ensure_ascii=False, sort_keys=True, allow_nan=False)
    except Exception as exc:
        print(f"offline calculation failed ({type(exc).__name__})", file=sys.stderr)
        print(json.dumps({**SAFETY, "status": "failed", "error_type": type(exc).__name__}, sort_keys=True))
        return 2
    print(output)
    if status == 1:
        print("offline calculation completed with warnings", file=sys.stderr)
    return status


if __name__ == "__main__":
    raise SystemExit(main())
