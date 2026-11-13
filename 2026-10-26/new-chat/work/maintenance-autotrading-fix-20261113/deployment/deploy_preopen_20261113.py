#!/usr/bin/env python3
"""Turn-key 2026-11-13 maintenance deployment launcher (pre-open, explicit intraday, or after-close).

Supports:
- `--confirm-exception-deploy`: pre-open (08:40~08:56) or after-close (15:40:06~23:50)
- `--confirm-intraday-deploy`: explicit user-approved intraday deployment (09:00:05~15:25:00) using `intraday-code-token`
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import tempfile
import time

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
SOURCE = HERE.parent / "outputs/ai-stock"
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(SOURCE))

import deploy_handoff_runtime as handoff
import run_receiver_only_deploy as deployer
from kiwoom_autotrader.utils import telegram_service as service
from kiwoom_autotrader.utils.runtime_deployment_guard import (
    KST,
    assert_code_deployment,
    manifest_sha256,
)

APPROVED_DAY = "20261113"
APPROVED_DATE_ISO = "2026-11-13"
CALENDAR_ARCHIVE = Path("/Volumes/AIStockData/ai-stock-research-data/s5-full-eligibility")
REPORTS_PRIMARY = Path("/Users/tr-oh/Documents/Codex/reports/2026-11-13")
REPORTS_RESEARCH = Path("/Users/tr-oh/Documents/Codex/2026-10-26/new-chat/outputs/ai-stock-research/reports/2026-11-13")


def now_kst() -> datetime:
    return datetime.now(KST)


def find_today_official_session(paths, moment: datetime) -> dict | None:
    claims_dir = paths.base / "research-readonly/toss-evidence-refresh-claims"
    if not claims_dir.is_dir() or not CALENDAR_ARCHIVE.is_dir():
        return None
    claim_candidates = sorted(claims_dir.glob(f"{APPROVED_DAY}-*.claim.json"), reverse=True)
    if not claim_candidates:
        return None

    calendar_candidates = sorted(
        CALENDAR_ARCHIVE.glob(f"{APPROVED_DAY}-*/toss/0001-calendar-current.json"),
        reverse=True,
    )
    if not calendar_candidates:
        calendar_candidates = sorted(
            CALENDAR_ARCHIVE.glob("*/toss/0001-calendar-current.json"),
            reverse=True,
        )[:32]

    for claim_path in claim_candidates:
        try:
            claim_raw = claim_path.read_bytes()
            claim = json.loads(claim_raw)
            if (
                claim.get("schema") != "toss-dart-refresh-slot-claim-v1"
                or claim.get("session_day") != APPROVED_DATE_ISO
            ):
                continue
            claimed = datetime.fromisoformat(claim["claimed_at"]).astimezone(KST)
            if claimed > moment:
                continue
            wire_sha = claim.get("calendar_source_sha256")
            if not isinstance(wire_sha, str) or len(wire_sha) != 64:
                continue
            claim_sha = hashlib.sha256(claim_raw).hexdigest()
            for cal_path in calendar_candidates:
                try:
                    cal_raw = cal_path.read_bytes()
                    env = json.loads(cal_raw)
                    res = env.get("result", {})
                    if (
                        env.get("schema") == "toss-decoded-response-evidence-v1"
                        and env.get("request", {}).get("endpoint_key") == "market_calendar_kr"
                        and env.get("request", {}).get("params", {}).get("date") == APPROVED_DATE_ISO
                        and res.get("status") == "ok"
                        and res.get("response_sha256") == wire_sha
                    ):
                        observed = datetime.fromisoformat(res["observed_at"]).astimezone(KST)
                        if observed <= moment and abs((claimed - observed).total_seconds()) <= 900:
                            return {
                                "claim_path": str(claim_path),
                                "claim_sha256": claim_sha,
                                "calendar_path": str(cal_path),
                                "calendar_file_sha256": hashlib.sha256(cal_raw).hexdigest(),
                                "calendar_source_sha256": wire_sha,
                                "observed_at": observed.isoformat(),
                            }
                except Exception:
                    continue
        except Exception:
            continue
    return None


def wait_for_official_session(paths, deadline_hhmmss: str) -> dict:
    printed = False
    while True:
        moment = now_kst()
        if moment.strftime("%Y%m%d") != APPROVED_DAY:
            raise SystemExit(f"[중단] 승인 날짜 불일치: {moment.isoformat()} (기대: {APPROVED_DATE_ISO})")
        session = find_today_official_session(paths, moment)
        if session is not None:
            return session
        if moment.strftime("%H:%M:%S") > deadline_hhmmss:
            raise SystemExit(
                f"[중단] {deadline_hhmmss} KST까지 당일(2026-11-13) 토스 공식 거래일 캘린더 증빙을 찾지 못했습니다."
            )
        if not printed:
            print(
                f"[{moment:%H:%M:%S}] ⏳ 당일 토스 공식 거래일 캘린더 증빙(20261113-*.claim.json) 대기 중...",
                flush=True,
            )
            printed = True
        time.sleep(1.0)


def wait_for_zero_pending_orders(ledger_path: Path, max_wait_seconds: float = 210.0) -> dict:
    deadline = time.monotonic() + max_wait_seconds
    printed = False
    while True:
        summary = handoff.db_summary(ledger_path, pending=True, now=now_kst())
        if summary["pending_intents"] == 0 and summary["unfilled_orders"] == 0:
            return summary
        if time.monotonic() >= deadline:
            raise SystemExit(
                f"[중단] 미해결 주문이 남아 있어 교체하지 않았습니다 "
                f"(pending_intents={summary['pending_intents']}, unfilled_orders={summary['unfilled_orders']})."
            )
        if not printed:
            print(
                f"[{now_kst():%H:%M:%S}] ⏳ 미체결/대기 의도 정산 대기 중 "
                f"(pending_intents={summary['pending_intents']}, unfilled_orders={summary['unfilled_orders']})...",
                flush=True,
            )
            printed = True
        time.sleep(2.0)


def align_between_watchdog_ticks(phase: str) -> datetime:
    """Align between :06 and :24 so the ~24s restart completes before the next :00 watchdog tick."""
    printed_close = False
    printed_sec = False
    while True:
        moment = now_kst()
        hhmmss = moment.strftime("%H:%M:%S")
        if phase == "preopen" and hhmmss > "08:56:00":
            raise SystemExit(f"[중단] 장 시작 임박({hhmmss} > 08:56:00)으로 장전 배포를 시작하지 않습니다.")
        if phase == "intraday" and hhmmss > "15:12:00":
            raise SystemExit(f"[중단] 종가 선정 창 임박({hhmmss} > 15:12:00)으로 장중 배포를 시작하지 않습니다.")
        if phase == "afterclose" and hhmmss > "23:50:00":
            raise SystemExit(f"[중단] 자정 임박({hhmmss} > 23:50:00)으로 당일 배포를 시작하지 않습니다.")
        if phase == "afterclose" and hhmmss < "15:40:06":
            if not printed_close:
                print(f"[{hhmmss}] ⏳ 정규장 마감 후 공식 안전 창(15:40:06 KST)까지 대기 중...", flush=True)
                printed_close = True
            time.sleep(1.0)
            continue
        if 6 <= moment.second <= 24:
            return moment
        if not printed_sec:
            print(
                f"[{hhmmss}] ⏳ stability-watchdog 정각(:00) 스냅샷 충돌 방지를 위해 다음 분 :06초로 정렬 대기 중...",
                flush=True,
            )
            printed_sec = True
        time.sleep(0.5)


def write_private_approval(official_session: dict, receiver_pid: int, issued: datetime, phase: str) -> Path:
    folder = Path(tempfile.mkdtemp(prefix="approved-receiver-only-20261113-", dir=HERE))
    folder.chmod(0o700)
    approval_path = folder / "approval.json"
    if phase == "intraday":
        hard_cap = issued.replace(hour=15, minute=29, second=50, microsecond=0)
        expires = min(issued + timedelta(seconds=600), hard_cap)
        if expires <= issued + timedelta(seconds=120):
            raise SystemExit("[중단] 장중 배포 유효 잔여 시간이 120초 미만입니다.")
        document = {
            "schema": "runtime-code-deployment-approval-v1",
            "action": "code_deploy",
            "kind": "intraday-code-token",
            "change_class": "maintenance",
            "intraday_code_change_explicit": True,
            "scope": "receiver_only",
            "mode": "paper",
            "receiver_pid": receiver_pid,
            "source_manifest_sha256": deployer.TARGET_SHA,
            "issued_at": issued.isoformat(),
            "expires_at": expires.isoformat(),
            "user_approval": {
                "kind": "explicit_code_deployment",
                "reference": "2026-11-13-intraday-maintenance-autotrading-fix-receiver-only",
                "text": (
                    "사용자 명시 승인(2026-11-13T09:24:56+09:00): 장중 코드 배포"
                    "(intraday code deployment, kind=intraday-code-token, scope=receiver_only, "
                    f"change_class=maintenance, package_sha256={deployer.TARGET_SHA})를 "
                    "미체결 0건 확인 후 즉시 교체 실행 승인합니다."
                ),
            },
            "official_session": {
                "claim_path": official_session["claim_path"],
                "claim_sha256": official_session["claim_sha256"],
                "calendar_path": official_session["calendar_path"],
                "calendar_file_sha256": official_session["calendar_file_sha256"],
                "calendar_source_sha256": official_session["calendar_source_sha256"],
            },
        }
    else:
        hard_cap = (
            issued.replace(hour=23, minute=58, second=0, microsecond=0)
            if phase == "afterclose"
            else issued.replace(hour=8, minute=59, second=0, microsecond=0)
        )
        expires = min(issued + timedelta(seconds=600), hard_cap)
        if expires <= issued + timedelta(seconds=120):
            raise SystemExit("[중단] 유효 배포 잔여 시간이 120초 미만입니다.")
        phase_label = "장후(15:40 이후)" if phase == "afterclose" else "장전(08:50)"
        document = {
            "schema": "runtime-code-deployment-approval-v1",
            "action": "code_deploy",
            "kind": "exception-code-token",
            "change_class": "maintenance",
            "exception_code_change_explicit": True,
            "scope": "receiver_only",
            "mode": "paper",
            "receiver_pid": receiver_pid,
            "source_manifest_sha256": deployer.TARGET_SHA,
            "issued_at": issued.isoformat(),
            "expires_at": expires.isoformat(),
            "user_approval": {
                "kind": "explicit_code_deployment",
                "reference": "2026-11-13-maintenance-autotrading-fix-receiver-only",
                "text": (
                    f"사용자 명시 승인: 2026-11-13 금요일 {phase_label} 예외 코드 배포"
                    "(exception code deployment, scope=receiver_only, change_class=maintenance, "
                    f"package_sha256={deployer.TARGET_SHA})를 승인합니다."
                ),
            },
            "official_session": {
                "claim_path": official_session["claim_path"],
                "claim_sha256": official_session["claim_sha256"],
                "calendar_path": official_session["calendar_path"],
                "calendar_file_sha256": official_session["calendar_file_sha256"],
                "calendar_source_sha256": official_session["calendar_source_sha256"],
            },
        }
    raw = (json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    fd = os.open(approval_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    return approval_path


def print_post_deploy_verification(paths, deploy_result: dict) -> None:
    state_path = paths.state / "state.json"
    marker_path = paths.state / "maintenance-quiet.lock"
    deadline = time.monotonic() + 12.0
    state = {}
    while time.monotonic() < deadline:
        try:
            state = handoff.read_json(state_path)
            if state.get("maintenance_status", {}).get("paused") is False and state.get("maintenance_paused") is False:
                break
        except Exception:
            pass
        time.sleep(1.0)

    exp = state.get("paper_experiment") or {}
    active_arms = exp.get("active_arms") or []
    qls = exp.get("qls_shadow")
    rec = state.get("execution_recording") or {}

    print("\n" + "=" * 72)
    print("✅ [2026-11-13 코드 배포 완료 — 배포 직후 5대 상태 검증]")
    print("=" * 72)
    print(f"1. state.json pid                  : {state.get('pid')} (이전 PID {deploy_result.get('old_pid')} → 신규 PID {deploy_result.get('new_pid')})")
    print(f"2. state.json status               : {state.get('status')}")
    print(f"3. paper_experiment.enabled        : {exp.get('enabled')}")
    print(f"4. paper_experiment.active_arms    : {len(active_arms)}개 {active_arms}")
    print(f"5. paper_experiment.halted         : {exp.get('halted')}")
    print("-" * 72)
    print(f"• execution_recording              : ready={rec.get('ready')}, error={rec.get('error')}, gap_count={rec.get('gap_count')}")
    print(f"• maintenance-quiet.lock 해제 여부 : lock_exists={marker_path.exists()}, state_paused={state.get('maintenance_status', {}).get('paused')}")
    print(f"• EXP_QLS SHADOW 관측기            : {qls if qls is not None else '(다음 모니터 틱에서 기록 예정 — SHADOW 전용, 주문권한 False)'}")
    print(f"• 백업 디렉터리                    : {deploy_result.get('backup')}")
    print(f"• 백업 해시(assets)                : {deploy_result.get('backup_assets')}")
    print(f"• 런타임 패키지 SHA256             : {deploy_result.get('candidate_package_sha256')}")
    print("=" * 72)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--confirm-exception-deploy",
        action="store_true",
        help="2026-11-13 장전/장후 receiver_only 예외 코드 배포 실행을 명시 승인합니다.",
    )
    parser.add_argument(
        "--confirm-intraday-deploy",
        action="store_true",
        help="2026-11-13 정규장 장중(intraday-code-token) receiver_only 코드 배포 실행을 명시 승인합니다.",
    )
    parser.add_argument(
        "--dry-check",
        action="store_true",
        help="코드 교체·재시작 없이 당일 공식 캘린더 및 가드 사전 검증만 수행합니다.",
    )
    args = parser.parse_args()
    if not (args.confirm_exception_deploy or args.confirm_intraday_deploy or args.dry_check):
        print(
            "사용법: `--confirm-exception-deploy` 또는 `--confirm-intraday-deploy` 플래그를 전달하세요.",
            file=sys.stderr,
        )
        return 2

    moment = now_kst()
    if moment.strftime("%Y%m%d") != APPROVED_DAY:
        print(f"[중단] 승인 날짜가 아닙니다: {moment.isoformat()} (기대: {APPROVED_DATE_ISO})", file=sys.stderr)
        return 1
    hhmmss = moment.strftime("%H:%M:%S")
    if "08:40:00" <= hhmmss <= "08:56:00":
        phase = "preopen"
    elif "09:00:05" <= hhmmss <= "15:12:00":
        if not (args.confirm_intraday_deploy or args.dry_check):
            print("[중단] 정규장 장중에는 `--confirm-intraday-deploy` 명시 승인이 필요합니다.", file=sys.stderr)
            return 1
        phase = "intraday"
    elif "15:30:05" <= hhmmss <= "23:50:00":
        phase = "afterclose"
    else:
        print(f"[중단] 허용되지 않는 전환 시각 구간입니다: 현재 {hhmmss} KST", file=sys.stderr)
        return 1

    paths = service.service_paths()
    ledger_path = paths.base / deployer.LEDGER
    backups_root = paths.base / "backups"
    backups_root.mkdir(mode=0o700, exist_ok=True)
    needed_bytes = ledger_path.stat().st_size + 256 * 1024 * 1024
    free_bytes = shutil.disk_usage(backups_root).free
    if free_bytes < needed_bytes:
        print(
            f"[중단] 로컬 SSD 백업 여유 공간 부족 (필요: {needed_bytes / 1024**3:.2f} GiB, 현재: {free_bytes / 1024**3:.2f} GiB).",
            file=sys.stderr,
        )
        return 1

    # Immediate preflight: verify launchctl, ps, exact plist sys.executable identity, settings hash, and /Volumes/AIStockData access
    preflight_svc = service.service_status(paths)
    preflight_pid = preflight_svc.get("pid")
    if type(preflight_pid) is not int or preflight_pid <= 1 or not preflight_svc.get("running"):
        print(f"[중단] 사전 점검에서 수신기 실행 상태(launchctl)를 확인할 수 없습니다: {preflight_svc}", file=sys.stderr)
        return 1
    preflight_protected = deployer.discover_protected_pids(paths, preflight_pid)
    live_plist = plistlib.loads(paths.plist.read_bytes())
    receiver_python = live_plist["ProgramArguments"][0]
    if not Path(receiver_python).is_file():
        print(f"[중단] 수신기 plist 인터프리터 경로가 존재하지 않습니다: {receiver_python}", file=sys.stderr)
        return 1
    actual_sys_exe = subprocess.run(
        [receiver_python, "-c", "import sys; print(sys.executable)"],
        capture_output=True, text=True, check=True, timeout=5,
    ).stdout.strip()
    if actual_sys_exe != receiver_python:
        print(f"[중단] 인터프리터 sys.executable 불일치: {actual_sys_exe} != {receiver_python}", file=sys.stderr)
        return 1
    live_settings_sha = handoff.settings_snapshot(SOURCE, paths)["sha256"]
    if live_settings_sha != deployer.SETTINGS_SHA:
        print(f"[중단] settings.json 해시 변동 감지: {live_settings_sha} != {deployer.SETTINGS_SHA}", file=sys.stderr)
        return 1
    if not CALENDAR_ARCHIVE.is_dir():
        print(f"[중단] /Volumes/AIStockData 캘린더 아카이브 경로에 접근할 수 없습니다: {CALENDAR_ARCHIVE}", file=sys.stderr)
        return 1
    print(
        f"[{now_kst():%H:%M:%S}] ✔ 사전 런타임·권한 점검 통과 "
        f"(phase={phase}, 수신기 PID={preflight_pid}, python={receiver_python}, 보호 데몬 PID={preflight_protected}, SSD 여유={free_bytes / 1024**3:.2f} GiB)",
        flush=True,
    )

    deadline_map = {"preopen": "08:54:30", "intraday": "15:12:00", "afterclose": "23:50:00"}
    official_session = wait_for_official_session(paths, deadline_hhmmss=deadline_map[phase])
    ledger_summary = wait_for_zero_pending_orders(ledger_path)
    print(
        f"[{now_kst():%H:%M:%S}] ✔ 미체결·대기 의도 0건 확인 완료 "
        f"(orders_rows={ledger_summary['tables']['orders']['rows']}, pending_intents=0, unfilled_orders=0)",
        flush=True,
    )

    moment = align_between_watchdog_ticks(phase) if not args.dry_check else now_kst()

    svc = service.service_status(paths)
    old_pid = svc.get("pid")
    if type(old_pid) is not int or old_pid <= 1 or not svc.get("running"):
        print(f"[중단] 기존 수신기 실행 상태를 확인할 수 없습니다: {svc}", file=sys.stderr)
        return 1

    candidate_manifest = handoff.package_manifest(SOURCE / handoff.PACKAGE)
    if manifest_sha256(candidate_manifest) != deployer.TARGET_SHA:
        print("[중단] 후보 패키지 SHA256 불일치!", file=sys.stderr)
        return 1

    approval_path = write_private_approval(official_session, old_pid, moment, phase)
    proof = assert_code_deployment(
        approval_path,
        source_manifest=candidate_manifest,
        receiver_pid=old_pid,
        receiver_state_path=paths.state / "state.json",
        scope="receiver_only",
        now=now_kst(),
    )
    print(
        f"[{now_kst():%H:%M:%S}] ✔ 가드 사전 검증 통과 "
        f"(kind={proof['kind']}, approval_sha256={proof['approval_sha256'][:16]}..., receiver_pid={old_pid})",
        flush=True,
    )

    if args.dry_check:
        print(json.dumps({"dry_check": "passed", "proof": proof, "approval_path": str(approval_path)}, ensure_ascii=False, indent=2))
        return 0

    stamp = now_kst().strftime("%H%M%S")
    backup_dir = backups_root / f"20261113-{stamp}-{phase}-maintenance-autotrading-fix"
    REPORTS_PRIMARY.mkdir(parents=True, exist_ok=True)
    REPORTS_RESEARCH.mkdir(parents=True, exist_ok=True)
    report_research = REPORTS_RESEARCH / f"{stamp}-{phase}-maintenance-receiver-only-deploy.json"
    report_primary = REPORTS_PRIMARY / report_research.name

    cmd = [
        receiver_python,
        str(HERE / "run_receiver_only_deploy.py"),
        "--deploy",
        "--code-approval",
        str(approval_path),
        "--backup",
        str(backup_dir),
        "--output",
        str(report_research),
    ]
    print(f"[{now_kst():%H:%M:%S}] 🚀 수신기 단독(receiver_only) 코드 교체 및 재기동 시작 (약 20~25초 소요)...", flush=True)
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if report_research.is_file():
        shutil.copy2(report_research, report_primary)
        report_primary.chmod(0o600)
    if proc.returncode != 0:
        print(f"[실패] 배포 스크립트 종료 코드 {proc.returncode}:\n{proc.stdout}\n{proc.stderr}", file=sys.stderr)
        return proc.returncode

    deploy_result = json.loads(proc.stdout)
    print(f"DEPLOY_REPORT={report_primary}")
    print_post_deploy_verification(paths, deploy_result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
