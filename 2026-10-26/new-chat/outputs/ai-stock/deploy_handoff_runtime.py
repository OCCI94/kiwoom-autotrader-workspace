"""Explicit, reversible macOS handoff deployment. Default is strictly read-only.

No service/credential action occurs at import. --check-only does not open SQLite
or create locks/directories. --deploy stops writers, holds their locks, makes a
private consistent backup, installs without starting, verifies, then restores
prior service state. Database/log files are NEVER overwritten during rollback.
"""
import sys
sys.dont_write_bytecode = True
import argparse
from contextlib import contextmanager, ExitStack
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import time
import uuid
from kiwoom_autotrader.utils.runtime_deployment_guard import (
    assert_code_deployment, CodeDeploymentDenied, claim_code_deployment, verify_deployment_claim)
from kiwoom_autotrader.utils.control_audit import audit_control,control_origin

PACKAGE = "kiwoom_autotrader"
RESOURCES = ("config/data/settings-default.json", "config/data/settings.json")
MAX_FILE = 4 * 1024**3
MAX_BACKUP = 16 * 1024**3


class DeploymentError(RuntimeError):
    """Local, fixed reason codes only; never relay external exception text."""
    def __init__(self, reason, *, diagnostics=None):
        super().__init__(reason)
        self.diagnostics = diagnostics


def regular(path):
    path = Path(path)
    if any(item.is_symlink() for item in (path, *path.parents)):
        raise DeploymentError("SYMLINK_PATH_REFUSED")
    if not stat.S_ISREG(path.stat().st_mode):
        raise DeploymentError("NON_REGULAR_FILE_REFUSED")
    return path


def digest(path):
    path = regular(path)
    if path.stat().st_size > MAX_FILE: raise DeploymentError("FILE_SIZE_LIMIT")
    result = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024): result.update(chunk)
    return result.hexdigest()


def read_json(path):
    path = regular(path)
    with path.open("rb") as stream: raw = stream.read(1024 * 1024 + 1)
    if len(raw) > 1024 * 1024: raise DeploymentError("SETTINGS_SIZE_LIMIT")
    def unique(pairs):
        out = {}
        for key, value in pairs:
            if key in out: raise DeploymentError("DUPLICATE_SETTINGS_KEY")
            out[key] = value
        return out
    return json.loads(raw, object_pairs_hook=unique)


def package_manifest(package):
    package = Path(package)
    if not package.is_dir() or package.is_symlink(): raise DeploymentError("PACKAGE_UNAVAILABLE")
    files = sorted(path for path in package.rglob("*.py") if "__pycache__" not in path.relative_to(package).parts)
    if len(files) > 10000: raise DeploymentError("PACKAGE_FILE_LIMIT")
    files += [package / name for name in RESOURCES]
    if not (package / "__main__.py").is_file(): raise DeploymentError("PACKAGE_ENTRYPOINT_MISSING")
    return {path.relative_to(package).as_posix(): digest(path) for path in files}


def private_json(path, value):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(value, stream, sort_keys=True, indent=2); stream.write("\n"); stream.flush(); os.fsync(stream.fileno())


def private_copy(source, destination):
    regular(source)
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with source.open("rb") as src, os.fdopen(fd, "wb") as dst:
        shutil.copyfileobj(src, dst, 1024 * 1024); dst.flush(); os.fsync(dst.fileno())


@contextmanager
def exclusive(path):
    from kiwoom_autotrader.utils.platform_io import fcntl
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        os.fchmod(fd, 0o600)
        try: fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError: raise DeploymentError("WRITER_OR_DEPLOYMENT_LOCK_BUSY") from None
        yield
    finally:
        os.close(fd)


def readonly_db(path, *, terminal_evidence=False):
    regular(path)
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)
    try:
        db.execute("PRAGMA trusted_schema=OFF")
        if terminal_evidence:
            from kiwoom_autotrader.trading.broker_observation_repeats import install_evidence_view
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA temp_store=MEMORY")
            install_evidence_view(db)
        db.execute("PRAGMA query_only=ON")
        deadline = time.monotonic() + 30
        db.set_progress_handler(lambda: time.monotonic() >= deadline, 10000)
        return db
    except BaseException:
        db.close()
        raise


def sqlite_backup(source, destination):
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600); os.close(fd)
    src, dst = readonly_db(source), sqlite3.connect(destination)
    deadline = time.monotonic() + 60
    def progress(status, remaining, total):
        if time.monotonic() >= deadline: raise DeploymentError("DATABASE_BACKUP_TIMEOUT")
    try:
        src.backup(dst, pages=256, progress=progress, sleep=0.01)
        dst.execute("PRAGMA journal_mode=DELETE")
        dst.execute("PRAGMA trusted_schema=OFF")
        dst.set_progress_handler(lambda: time.monotonic() >= deadline, 10000)
        if dst.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise DeploymentError("DATABASE_INTEGRITY_FAILED")
    finally:
        dst.close(); src.close()


def quote_name(value):
    return '"' + value.replace('"', '""') + '"'


def _confirmed_terminal_zero_fill_count(db, tables, now):
    from kiwoom_autotrader.api.session import KST
    from kiwoom_autotrader.trading.cancellation_settlement import TABLE as CANCELLATIONS
    from kiwoom_autotrader.trading.execution_log import ExecutionTracker
    from kiwoom_autotrader.trading.expired_order_certificates import TABLE as EXPIRATIONS, verified_certificate

    if not {CANCELLATIONS, EXPIRATIONS}.intersection(tables):
        return 0
    now = datetime.now(KST) if now is None else now
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise DeploymentError("TERMINAL_EVIDENCE_CLOCK_UNVERIFIED")
    today = now.astimezone(KST).strftime("%Y%m%d")
    matches = db.execute("""SELECT o.*, 0 AS filled_quantity FROM orders o
        WHERE o.mode='paper' AND o.side='buy' AND typeof(o.quantity)='integer' AND o.quantity>0
        AND NOT EXISTS (SELECT 1 FROM fills f WHERE f.scope=o.scope AND f.day=o.day
            AND f.mode=o.mode AND f.ord_no=o.ord_no)
        AND EXISTS (SELECT 1 FROM intents i WHERE i.scope=o.scope AND i.day=o.day
            AND i.mode=o.mode AND i.order_number=o.ord_no AND i.symbol=o.symbol
            AND i.side=o.side AND i.strategy=o.strategy AND i.quantity=o.quantity AND i.state='accepted')
        ORDER BY o.scope,o.day,o.ord_no LIMIT 10001""").fetchall()
    if len(matches) > 10000:
        raise DeploymentError("TERMINAL_EVIDENCE_ORDER_LIMIT")
    confirmed = 0
    for order in matches:
        # No tracker constructor, migration, or settlement publication in a reader.
        tracker = ExecutionTracker.__new__(ExecutionTracker)
        tracker.scope = order["scope"]
        tracker.now = lambda: now
        if (CANCELLATIONS in tables and tracker._confirmed_cancelled_quantity(db, order) == order["quantity"]
                or EXPIRATIONS in tables and order["day"] < today
                and verified_certificate(tracker, db, order, now)):
            confirmed += 1
    return confirmed


def db_summary(path, *, pending=False, now=None):
    db = readonly_db(path, terminal_evidence=pending)
    try:
        db.execute("BEGIN")
        tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        if len(tables) > 1000: raise DeploymentError("DATABASE_TABLE_LIMIT")
        result = {"tables": {}, "pending_intents": 0, "unfilled_orders": 0}
        for table in tables:
            name = quote_name(table); columns = list(db.execute("PRAGMA table_info(" + name + ")"))
            order = ",".join(str(i + 1) for i in range(len(columns)))
            hashed, count = hashlib.sha256(), 0
            for row in db.execute("SELECT * FROM " + name + " ORDER BY " + order):
                count += 1
                if count > 2000000: raise DeploymentError("DATABASE_ROW_LIMIT")
                encoded = json.dumps([{"blob_sha256": hashlib.sha256(v).hexdigest()} if isinstance(v, bytes) else v for v in row], separators=(",", ":"), ensure_ascii=True).encode()
                hashed.update(encoded + b"\n")
            result["tables"][table] = {"rows": count, "sha256": hashed.hexdigest()}
        if pending:
            if not {"intents", "orders", "fills"}.issubset(tables): raise DeploymentError("LEDGER_SCHEMA_UNRECOGNIZED")
            result["pending_intents"] = db.execute("SELECT COUNT(*) FROM intents WHERE state NOT IN ('accepted','cancelled') OR state IS NULL OR (state='accepted' AND order_number IS NULL)").fetchone()[0]
            result["unfilled_orders"] = db.execute("""SELECT COUNT(*) FROM orders o WHERE
                typeof(o.quantity)!='integer' OR o.quantity<=0 OR o.quantity != COALESCE((
                SELECT SUM(f.quantity) FROM fills f WHERE f.scope=o.scope AND f.day=o.day AND f.mode=o.mode AND f.ord_no=o.ord_no),0)
                OR EXISTS (SELECT 1 FROM fills f WHERE f.scope=o.scope AND f.day=o.day AND f.mode=o.mode AND f.ord_no=o.ord_no
                  AND (typeof(f.quantity)!='integer' OR f.quantity<=0))""").fetchone()[0]
            result["unfilled_orders"] -= _confirmed_terminal_zero_fill_count(db, tables, now)
            # An accepted intent must also link to the corresponding order row.
            result["pending_intents"] += db.execute("""SELECT COUNT(*) FROM intents i WHERE state='accepted' AND NOT EXISTS
                (SELECT 1 FROM orders o WHERE o.scope=i.scope AND o.day=i.day AND o.mode=i.mode AND o.ord_no=i.order_number)""").fetchone()[0]
        return result
    finally:
        db.close()


class Services:
    """Existing service APIs; all mutation methods are exclusive to --deploy."""
    def __init__(self):
        from kiwoom_autotrader.utils import telegram_service, account_service
        self.bot, self.account = telegram_service, account_service
        self.paths, self.account_paths = self.bot.service_paths(), self.account.account_paths()

    def status(self):
        return {"bot": self.bot.service_status(self.paths), "collector": self.account.collector_status(self.account_paths)}

    def stop(self):
        self.account.stop_collector_service(self.account_paths); self.bot.stop_service(self.paths)

    def install(self, source, held_directories, *, before_replace=None,before_mutation=None):
        # Reuse caller-held exact listener locks instead of recursively locking
        # them. This CLI is single-threaded; all original functions are restored.
        old_start, old_stopped = self.bot.start_service, self.bot._stopped_directory
        @contextmanager
        def held(state, timeout=120):
            if Path(state).resolve() not in held_directories: raise DeploymentError("INSTALLER_LOCK_SCOPE_CHANGED")
            yield
        self.bot.start_service = lambda paths=None: None
        self.bot._stopped_directory = held
        try: self.bot.install_service(source=source, paths=self.paths, before_replace=before_replace,before_mutation=before_mutation)
        finally: self.bot.start_service, self.bot._stopped_directory = old_start, old_stopped

    def restore(self, previous):
        current = self.status()
        if previous["bot"]["running"] and not current["bot"]["running"]: self.bot.start_service(self.paths)
        if previous["collector"]["running"] and not current["collector"]["running"]: self.account.start_collector_service(self.account_paths)
        deadline = time.monotonic() + 15
        while True:
            current = self.status()
            if all((current[k]["loaded"], current[k]["running"]) == (previous[k]["loaded"], previous[k]["running"]) for k in previous): return
            if time.monotonic() >= deadline: raise DeploymentError("SERVICE_STATE_NOT_RESTORED")
            time.sleep(0.2)


def settings_snapshot(source, paths):
    from kiwoom_autotrader.config.settings import _validate
    defaults = read_json(source / PACKAGE / RESOURCES[0])
    path = paths.base / "config/data/settings.json"
    if not path.exists(): raise DeploymentError("RUNTIME_SETTINGS_MISSING")
    overrides = read_json(path)
    if not isinstance(overrides, dict) or set(overrides) - set(defaults): raise DeploymentError("INVALID_RUNTIME_SETTINGS")
    value = _validate({**defaults, **overrides})
    return {"sha256": digest(path), "active": value["gdcrs_enabled"] or value["ddcrs_enabled"] or bool(value["reservations"])}


def inspect(source, services):
    paths = services.paths
    current = services.status()
    source_files = package_manifest(source / PACKAGE); runtime_files = package_manifest(paths.runtime / PACKAGE)
    settings = settings_snapshot(source, paths)
    blockers = []
    if settings["active"]: blockers.append("ACTIVE_STRATEGY_OR_RESERVATION")
    if any(value["loaded"] != value["running"] for value in current.values()): blockers.append("SERVICE_STATE_NOT_STABLE")
    if not current["bot"]["installed"]: blockers.append("BOT_NOT_INSTALLED")
    ledger = paths.runtime / "logs/.execution-ledger.sqlite3"
    if not ledger.is_file() or ledger.is_symlink(): blockers.append("EXECUTION_LEDGER_MISSING_OR_UNSAFE")
    return {"status": "blocked" if blockers else "ready_for_stopped_validation", "blockers": blockers,
        "source_files": source_files, "runtime_files": runtime_files, "settings": settings, "services": current,
        "ledger_validation": "deferred_until_writers_stopped", "database_files_modified": False}


def snapshot_base(paths, account_paths, backup):
    files = []
    for folder, directories, names in os.walk(paths.base, followlinks=False):
        base = Path(folder)
        directories[:] = [name for name in directories if not (base == paths.base and name == "backups")]
        if any((base / name).is_symlink() for name in directories): raise DeploymentError("SYMLINK_PATH_REFUSED")
        files.extend(base / name for name in names)
        if len(files) > 100000: raise DeploymentError("BACKUP_FILE_LIMIT")
    if sum(regular(path).stat().st_size for path in files) > MAX_BACKUP: raise DeploymentError("BACKUP_SIZE_LIMIT")
    databases, manifest = {}, {}
    db_files = set()
    for path in files:
        with path.open("rb") as stream:
            if stream.read(16) == b"SQLite format 3\x00" or path.suffix in (".sqlite3", ".sqlite", ".db"): db_files.add(path)
    for path in files:
        if any(str(path) == str(db) + suffix for db in db_files for suffix in ("-wal", "-shm", "-journal")): continue
        relative = path.relative_to(paths.base); destination = backup / "base" / relative
        if path in db_files:
            sqlite_backup(path, destination)
            databases[relative.as_posix()] = db_summary(destination, pending=path.name == ".execution-ledger.sqlite3")
        else: private_copy(path, destination)
        manifest["base/" + relative.as_posix()] = digest(destination)
    for name, path in (("bot.plist", paths.plist), ("collector.plist", account_paths.plist)):
        if path.exists():
            private_copy(path, backup / name); manifest[name] = digest(backup / name)
    private_json(backup / "manifest.json", {"files": manifest, "databases": databases, "sqlite_backup_includes_committed_wal": True})
    return databases


def verify_databases(paths, databases):
    for relative, summary in databases.items():
        actual = db_summary(paths.base / relative, pending=Path(relative).name == ".execution-ledger.sqlite3")
        if actual != summary: raise DeploymentError("DATABASE_CHANGED_DURING_DEPLOYMENT")


def verify_existing_financial_rows(paths, backup, databases):
    """Allow startup migrations/new observations, never erase financial facts."""
    for relative in databases:
        if Path(relative).name != ".execution-ledger.sqlite3": continue
        db = readonly_db(paths.base / relative)
        try:
            if db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                raise DeploymentError("DATABASE_INTEGRITY_FAILED")
            db.execute("ATTACH DATABASE ? AS prior", ((backup / "base" / relative).resolve().as_uri() + "?mode=ro",))
            old_tables = {r[0] for r in db.execute("SELECT name FROM prior.sqlite_master WHERE type='table'")}
            for table in ("intents", "orders", "fills", "order_decisions", "profit_observations"):
                if table not in old_tables: continue
                names = [r[1] for r in db.execute("PRAGMA prior.table_info(" + quote_name(table) + ")")]
                if table == "fills": names = [name for name in names if name not in ("remaining", "holdings_at", "holdings_status")]
                fields = ",".join(quote_name(name) for name in names)
                if db.execute("SELECT " + fields + " FROM prior." + quote_name(table) + " EXCEPT SELECT " + fields + " FROM main." + quote_name(table) + " LIMIT 1").fetchone() is not None:
                    raise DeploymentError("EXISTING_FINANCIAL_ROW_CHANGED")
        finally:
            db.close()


def stopped(services, *, phase="after_stop", timeout=15, interval=0.2, clock=None, wait=None):
    """Bootout can return before launchd removes the service registration.

    Recheck both workers, including loaded-but-not-running transitions. Never
    treat missing/non-boolean status fields as a confirmed stopped worker.
    """
    clock, wait = clock or time.monotonic, wait or time.sleep
    deadline = clock() + timeout
    while True:
        status = services.status()
        workers = {name: {field: status.get(name, {}).get(field)
                         if type(status.get(name, {}).get(field)) is bool else None
                         for field in ("loaded", "running")} for name in ("bot", "collector")}
        if all(value is False for worker in workers.values() for value in worker.values()): return
        remaining = deadline - clock()
        if remaining <= 0:
            raise DeploymentError("WORKERS_NOT_STOPPED", diagnostics={"phase": phase, "stop_snapshot": workers})
        wait(min(interval, remaining))


@contextmanager
def writers_locked(source, services):
    paths = services.paths
    locks = [paths.state / "listener.lock", services.account_paths.state / "collector.lock", source / "runs/telegram/listener.lock"]
    with ExitStack() as stack:
        for lock in locks: stack.enter_context(exclusive(lock))
        yield {lock.parent.resolve() for lock in locks}


def rollback_package(services, backup):
    paths = services.paths; destination = paths.runtime / PACKAGE
    saved = backup / "base/runtime" / PACKAGE
    if not saved.is_dir(): raise DeploymentError("ROLLBACK_PACKAGE_MISSING")
    # Retain every failed/new package byte; never replace database/log data.
    if destination.exists(): destination.rename(backup / ("failed-package-" + uuid.uuid4().hex[:8]))
    shutil.copytree(saved, destination)
    for name, path in (("bot.plist", paths.plist), ("collector.plist", services.account_paths.plist)):
        original = backup / name
        if original.exists():
            if path.exists(): path.rename(backup / ("failed-" + name + "-" + uuid.uuid4().hex[:8]))
            private_copy(original, path)
    expected = package_manifest(saved)
    if package_manifest(destination) != expected: raise DeploymentError("ROLLBACK_PACKAGE_MISMATCH")


def execute(source, *, deploy=False, services=None, code_approval=None):
    source = Path(source).resolve(); services = services or Services()
    before = inspect(source, services)
    public = {"mode": "deploy" if deploy else "check-only", "status": before["status"], "blockers": before["blockers"],
        "source_file_count": len(before["source_files"]), "runtime_file_count": len(before["runtime_files"]),
        "source_runtime_match": before["source_files"] == before["runtime_files"],
        "new_files_to_deploy": sorted(set(before["source_files"]) - set(before["runtime_files"])),
        "modified_files_to_deploy": sorted(name for name in set(before["source_files"]) & set(before["runtime_files"])
                                           if before["source_files"][name] != before["runtime_files"][name]),
        "settings_safe_for_deploy": not before["settings"]["active"],
        "settings_sha256": before["settings"]["sha256"], "services": before["services"],
        "ledger_validation": before["ledger_validation"]}
    if not deploy or before["blockers"]: return public
    paths = services.paths; backup = None; replaced = False; databases = {}; settings_safe = True
    def check_code_permission():
        try:
            proof = assert_code_deployment(code_approval,
                source_manifest=package_manifest(source / PACKAGE),
                receiver_pid=before['services']['bot'].get('pid'),
                receiver_state_path=paths.state / 'state.json', scope='receiver_and_collector')
        except CodeDeploymentDenied as error:
            raise DeploymentError(str(error), diagnostics={'code_deployment_permission_denied':True}) from None
        return proof
    # Even a lock file is a mutation: permission must precede it.
    approval = check_code_permission()
    attempt = None
    def recheck_code_permission():
        proof = check_code_permission()
        if proof['approval_sha256'] != approval['approval_sha256']:
            raise DeploymentError('CODE_DEPLOYMENT_APPROVAL_CHANGED', diagnostics={'code_deployment_permission_denied':True})
        if attempt is not None:
            try:verify_deployment_claim(attempt,proof)
            except CodeDeploymentDenied as error:
                raise DeploymentError(str(error),diagnostics={'code_deployment_permission_denied':True}) from None
        return proof
    stop_attempted = False
    with exclusive(paths.base / ".handoff-deploy.lock"), ExitStack() as control_stack:
        try:
            if settings_snapshot(source, paths) != before["settings"]: raise DeploymentError("SETTINGS_CHANGED")
            current_permission=recheck_code_permission()
            try:attempt=claim_code_deployment(paths.base/'code-deployment-claims',current_permission)
            except CodeDeploymentDenied as error:
                raise DeploymentError(str(error),diagnostics={'code_deployment_permission_denied':True}) from None
            public['deployment_attempt']=attempt
            control_stack.enter_context(control_origin('approved_maintenance'))
            control_outcome=control_stack.enter_context(audit_control('code_deploy','deployment',root=paths.base,
                paths=tuple(paths.runtime/PACKAGE/name for name in sorted(set(before['source_files'])|set(before['runtime_files'])))))
            stop_attempted = True
            services.stop(); stopped(services, phase="after_initial_stop")
            with writers_locked(source, services) as held:
                stopped(services, phase="before_backup")
                if settings_snapshot(source, paths) != before["settings"]: raise DeploymentError("SETTINGS_CHANGED")
                backup = paths.base / "backups" / ("handoff-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ"))
                backup.mkdir(parents=True, mode=0o700)
                databases = snapshot_base(paths, services.account_paths, backup)
                if "runtime/logs/.execution-ledger.sqlite3" not in databases:
                    raise DeploymentError("EXECUTION_LEDGER_NOT_VALIDATED")
                if any(item["pending_intents"] or item["unfilled_orders"] for item in databases.values()):
                    raise DeploymentError("UNRESOLVED_ORDER_REQUIRES_RECONCILIATION")
                if package_manifest(source / PACKAGE) != before["source_files"]: raise DeploymentError("SOURCE_CHANGED")
                recheck_code_permission()
                def before_replace():
                    nonlocal replaced
                    recheck_code_permission()
                    replaced = True
                services.install(source, held, before_replace=before_replace,before_mutation=recheck_code_permission); stopped(services, phase="after_install")
                if package_manifest(paths.runtime / PACKAGE) != before["source_files"]: raise DeploymentError("RUNTIME_HASH_MISMATCH")
                if package_manifest(source / PACKAGE) != before["source_files"]: raise DeploymentError("SOURCE_CHANGED")
                if settings_snapshot(source, paths) != before["settings"]: raise DeploymentError("SETTINGS_CHANGED")
                verify_databases(paths, databases)
            services.restore(before["services"])
            # Startup may log in, migrate tables and update cost/holdings data.
            # Preserve existing execution facts while allowing those new facts.
            verify_existing_financial_rows(paths, backup, databases)
            if settings_snapshot(source, paths) != before["settings"]: raise DeploymentError("SETTINGS_CHANGED")
            public.update(status="deployed", source_runtime_match=True, ledger_validation="preserved_and_no_unresolved_orders",
                          backup=str(backup), services=services.status())
        except Exception as error:
            if 'control_outcome' in locals():control_outcome['status']='failed'
            reason = str(error) if isinstance(error, DeploymentError) else "DEPLOYMENT_FAILED"
            if not stop_attempted:
                public.update(status='failed', reason=reason, recovery='no_process_change', data_files_restored=False)
                return public
            diagnostics = error.diagnostics if isinstance(error, DeploymentError) else None
            recovery = "not_required"
            recovery_diagnostics = None
            try:
                services.stop(); stopped(services, phase="before_rollback")
                with writers_locked(source, services):
                    if replaced:
                        rollback_package(services, backup); recovery = "package_and_plists_restored"
                    settings_safe = settings_snapshot(source, paths) == before["settings"]
                    # Preserve externally changed settings instead of silently
                    # overwriting them. No data DB or log is ever restored.
                    if databases: verify_existing_financial_rows(paths, backup, databases)
                if diagnostics and diagnostics.get('code_deployment_permission_denied'):
                    recovery = 'code_permission_expired_workers_left_stopped'
                elif settings_safe: services.restore(before["services"])
                else: recovery = "settings_changed_preserved_workers_left_stopped"
            except Exception as recovery_error:
                recovery = "recovery_incomplete_manual_review_required"
                if isinstance(recovery_error, DeploymentError): recovery_diagnostics = recovery_error.diagnostics
            public.update(status="failed", reason=reason, recovery=recovery, backup=str(backup) if backup else None,
                          diagnostics=diagnostics, recovery_diagnostics=recovery_diagnostics,
                          data_files_restored=False, services=services.status())
    return public


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--check-only", action="store_true")
    group.add_argument("--deploy", action="store_true")
    parser.add_argument('--code-approval', type=Path)
    args = parser.parse_args(argv)
    try:
        result = execute(Path(__file__).resolve().parent, deploy=args.deploy, code_approval=args.code_approval)
    except Exception as error:
        result = {"mode": "deploy" if args.deploy else "check-only", "status": "failed",
                  "reason": str(error) if isinstance(error, DeploymentError) else "DEPLOYMENT_CHECK_FAILED"}
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] in ("ready_for_stopped_validation", "deployed") else 2 if result["status"] == "blocked" else 1


if __name__ == "__main__": raise SystemExit(main())
