"""Best-effort, bounded tracker timings; never financial evidence or control.

Construction and import perform no filesystem, memory-query or process work.
Events contain only allowlisted stage names, times, numeric counts and this
process's memory footprint. They never accept broker payloads or error text.
The caller's exception is re-raised unchanged. Diagnostic IO/callback failures
are exposed by ``snapshot`` and must not alter order or accounting state.
"""
from __future__ import annotations

from collections import deque
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import stat
import sys
import threading
import time
import uuid
from zoneinfo import ZoneInfo


KST = ZoneInfo("Asia/Seoul")
STAGES = frozenset({
    "tracker_cycle", "tracker_task", "history_export", "accounting",
    "closed_fingerprint", "broker_snapshot", "json_parse", "executions_fetch",
    "profit_fetch", "period_profit_fetch", "daily_profit_fetch", "summary",
    "closed_facts", "holdings_fetch", "closed_source_count", "calendar_plan",
    "closed_watermark", "sql_snapshot", "unclassified",
})
FAILURE_KINDS = ("clock", "rss", "write", "callback", "counts", "stage", "overlap", "context")
MAX_COUNT = (1 << 63) - 1
BYTE_BASES = frozenset({"payload_json_utf8", "sqlite_payload_utf8", "export_written_bytes",
                        "source_wire_bytes", "canonical_output_utf8", "jsonl_output_utf8"})
RSS_BASES = frozenset({"mach_task_basic_info_resident_size", "proc_self_statm_resident_pages",
                       "getrusage_self_ru_maxrss_bytes", "getrusage_self_ru_maxrss_kib",
                       "injected_numeric_footprint"})


class _CapacityReached(Exception):
    pass


def process_rss():
    """Return own current/peak RSS bytes, or nulls when unavailable.

    No subprocesses, network or foreign process inspection. Windows remains
    explicitly unobserved. Linux procfs and macOS task_info are best effort;
    ru_maxrss is bytes on macOS and KiB on Linux.
    """
    if sys.platform == "win32":
        return {"current_rss_bytes": None, "peak_rss_bytes": None,
                "current_rss_basis": None, "peak_rss_basis": None}
    current = peak = None
    current_basis = peak_basis = None
    try:
        import resource
        value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        if isinstance(value, (int, float)) and math.isfinite(value) and value >= 0:
            peak = int(value * (1 if sys.platform == "darwin" else 1024))
            peak_basis = ("getrusage_self_ru_maxrss_bytes" if sys.platform == "darwin"
                          else "getrusage_self_ru_maxrss_kib")
    except Exception:
        pass
    try:
        if sys.platform.startswith("linux"):
            resident = int(Path("/proc/self/statm").read_text().split()[1])
            current = resident * os.sysconf("SC_PAGE_SIZE")
            current_basis = "proc_self_statm_resident_pages"
        elif sys.platform == "darwin":
            import ctypes

            class BasicInfo(ctypes.Structure):
                _fields_ = [
                    ("virtual_size", ctypes.c_uint64),
                    ("resident_size", ctypes.c_uint64),
                    ("resident_size_max", ctypes.c_uint64),
                    ("user_seconds", ctypes.c_int32),
                    ("user_microseconds", ctypes.c_int32),
                    ("system_seconds", ctypes.c_int32),
                    ("system_microseconds", ctypes.c_int32),
                    ("policy", ctypes.c_int32),
                    ("suspend_count", ctypes.c_int32),
                ]

            library = ctypes.CDLL(None)
            task = ctypes.c_uint.in_dll(library, "mach_task_self_").value
            info = BasicInfo()
            count = ctypes.c_uint(ctypes.sizeof(info) // ctypes.sizeof(ctypes.c_int32))
            function = library.task_info
            function.argtypes = [ctypes.c_uint, ctypes.c_int, ctypes.c_void_p,
                                 ctypes.POINTER(ctypes.c_uint)]
            function.restype = ctypes.c_int
            if function(task, 20, ctypes.byref(info), ctypes.byref(count)) == 0:
                current = int(info.resident_size)
                current_basis = "mach_task_basic_info_resident_size"
    except Exception:
        pass
    return {"current_rss_bytes": current, "peak_rss_bytes": peak,
            "current_rss_basis": current_basis, "peak_rss_basis": peak_basis}


class _Sample:
    def __init__(self, diagnostics, token):
        self._diagnostics = diagnostics
        self._token = token

    def set_counts(self, *, rows=None, bytes_in=None, bytes_out=None, bytes_basis=None,
                   bytes_in_basis=None, bytes_out_basis=None):
        """Attach numeric work counts, never raw payloads or financial values."""
        self._diagnostics._set_counts(self._token, rows, bytes_in, bytes_out,
                                      bytes_basis, bytes_in_basis, bytes_out_basis)


class TrackerDiagnostics:
    def __init__(self, log_dir, *, now=None, monotonic=None, rss_reader=None,
                 progress=None, max_recent=32, append_writer=None,
                 max_file_bytes=32 * 1024 * 1024):
        self._lock = threading.RLock()
        self._io_lock = threading.Lock()
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._monotonic = monotonic or time.monotonic
        self._rss_reader = rss_reader or process_rss
        self._failures = dict.fromkeys(FAILURE_KINDS, 0)
        self._last_failure = None
        self._write_failed = self._callback_failed = False
        self._write_status = "not_attempted"
        self._callback_busy = False
        self._callback_skipped_other_thread = 0
        self._progress = progress
        self._owner_thread = threading.get_ident()
        self._run_id = uuid.uuid4().hex
        self._active = {}
        self._sequence = 0
        self._last_completed_at = self._last_stage = self._last_duration = None
        self._last_work_day = self._last_mode = None
        self._last_background_completed_at = None
        self._current_rss = self._peak_rss = None
        self._current_rss_basis = self._peak_rss_basis = None
        self._append_writer = append_writer
        self._max_file_bytes = (max_file_bytes if type(max_file_bytes) is int
                                and 1024 <= max_file_bytes <= 32 * 1024 * 1024
                                else 32 * 1024 * 1024)
        self._recent = deque(maxlen=max_recent if type(max_recent) is int
                             and 1 <= max_recent <= 128 else 32)
        try:
            self._log_dir = Path(log_dir)
        except Exception:
            self._log_dir = None
            self._failure("write")

    def _failure(self, kind):
        with self._lock:
            self._failures[kind] = min(MAX_COUNT, self._failures[kind] + 1)
            self._last_failure = kind

    def set_progress_callback(self, callback):
        """Bind progress publication to the calling listener thread only."""
        if callback is not None and not callable(callback):
            self._failure("callback")
            return
        with self._lock:
            self._progress = callback
            self._owner_thread = threading.get_ident()

    def _capture(self):
        at = utc = mono = None
        try:
            value = self._now()
            if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
                raise ValueError
            at, utc = value.astimezone(KST).isoformat(), value.astimezone(timezone.utc).isoformat()
            value = self._monotonic()
            if type(value) not in (int, float) or not math.isfinite(value):
                raise ValueError
            mono = float(value)
        except Exception:
            self._failure("clock")
        try:
            footprint = self._rss_reader()
            if not isinstance(footprint, dict):
                raise ValueError
            current = footprint.get("current_rss_bytes")
            peak = footprint.get("peak_rss_bytes")
            if any(x is not None and (type(x) is not int or not 0 <= x <= MAX_COUNT)
                   for x in (current, peak)):
                raise ValueError
            with self._lock:
                self._current_rss, self._peak_rss = current, peak
                current_basis = footprint.get("current_rss_basis")
                peak_basis = footprint.get("peak_rss_basis")
                self._current_rss_basis = ((current_basis if current_basis in RSS_BASES
                                            else "injected_numeric_footprint") if current is not None else None)
                self._peak_rss_basis = ((peak_basis if peak_basis in RSS_BASES
                                         else "injected_numeric_footprint") if peak is not None else None)
        except Exception:
            self._failure("rss")
            with self._lock:
                self._current_rss = self._peak_rss = None
                self._current_rss_basis = self._peak_rss_basis = None
        return at, utc, mono

    def snapshot(self):
        """Small process-local status; no IO, clocks or broker reads."""
        with self._lock:
            owned = [row for row in self._active.values()
                     if row["thread"] == self._owner_thread]
            latest = max(owned, key=lambda row: row["token"]) if owned else None
            return {
                "schema": "tracker-diagnostics-status-v1",
                "coverage": "current_process_only_not_financial_evidence",
                "run_id": self._run_id,
                "active": latest is not None,
                "phase": latest["stage"] if latest else "idle",
                "work_day": latest["work_day"] if latest else None,
                "mode": latest["mode"] if latest else None,
                "started_at": latest["started_at"] if latest else None,
                "last_completed_at": self._last_completed_at,
                "last_stage": self._last_stage,
                "last_duration_seconds": self._last_duration,
                "last_work_day": self._last_work_day,
                "last_mode": self._last_mode,
                "active_stage_count": len(owned),
                "background_active_stage_count": len(self._active) - len(owned),
                "last_background_completed_at": self._last_background_completed_at,
                "current_rss_bytes": self._current_rss,
                "peak_rss_bytes": self._peak_rss,
                "rss_scope": "whole_receiver_process_not_stage_attribution",
                "current_rss_basis": self._current_rss_basis,
                "peak_rss_basis": self._peak_rss_basis,
                "write_failed": self._write_failed,
                "write_status": self._write_status,
                "daily_log_byte_cap": self._max_file_bytes,
                "callback_failed": self._callback_failed,
                "failures": dict(self._failures),
                "last_failure_kind": self._last_failure,
                "callback_skipped_other_thread": self._callback_skipped_other_thread,
                "recent": [dict(row) for row in self._recent],
            }

    def _append(self, event, raw):
        if self._log_dir is None or event["observed_at"] is None:
            raise ValueError("diagnostic_target_unavailable")
        # This is telemetry, not the financial audit: append once, no per-stage
        # fsync/full-file read, no automatic truncation or retention deletion.
        day = event["observed_at"][:10].replace("-", "")
        name = "tracker-diagnostics-" + day + ".jsonl"
        if self._append_writer is not None:
            self._append_writer(self._log_dir / name, raw)
            return
        if any(path.is_symlink() for path in (self._log_dir, *self._log_dir.parents)):
            raise ValueError("diagnostic_path_symlink")
        self._log_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        directory = (None if os.name == "nt" else os.open(
            self._log_dir, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)))
        try:
            flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
            descriptor = (os.open(self._log_dir / name, flags, 0o600) if directory is None
                          else os.open(name, flags, 0o600, dir_fd=directory))
            try:
                info = os.fstat(descriptor)
                if not stat.S_ISREG(info.st_mode):
                    raise ValueError("diagnostic_file_type")
                if info.st_size + len(raw) > self._max_file_bytes:
                    raise _CapacityReached()
                if os.write(descriptor, raw) != len(raw):
                    raise OSError("diagnostic_short_write")
            finally:
                os.close(descriptor)
        finally:
            if directory is not None:
                os.close(directory)

    def _emit(self, event):
        try:
            raw = (json.dumps(event, ensure_ascii=True, sort_keys=True,
                              separators=(",", ":"), allow_nan=False) + "\n").encode("ascii")
            if len(raw) > 4096:
                raise ValueError("diagnostic_event_too_large")
            with self._io_lock:
                self._append(event, raw)
            with self._lock:
                self._write_failed = False
                self._write_status = "appended_best_effort_no_fsync"
        except _CapacityReached:
            self._failure("write")
            with self._lock:
                self._write_failed = True
                self._write_status = "daily_byte_cap_reached_no_truncation"
        except Exception:
            self._failure("write")
            with self._lock:
                self._write_failed = True
                self._write_status = "unavailable"
        with self._lock:
            callback = self._progress
            if callback is None or self._callback_busy:
                return
            if threading.get_ident() != self._owner_thread:
                self._callback_skipped_other_thread = min(MAX_COUNT, self._callback_skipped_other_thread + 1)
                return
            self._callback_busy = True
        try:
            callback(self.snapshot())
            with self._lock:
                self._callback_failed = False
        except Exception:
            self._failure("callback")
            with self._lock:
                self._callback_failed = True
        finally:
            with self._lock:
                self._callback_busy = False

    def _set_counts(self, token, rows, bytes_in, bytes_out, bytes_basis=None,
                    bytes_in_basis=None, bytes_out_basis=None):
        try:
            values = {"rows": rows, "bytes_in": bytes_in, "bytes_out": bytes_out}
            if any(v is not None and (type(v) is not int or not 0 <= v <= MAX_COUNT)
                   for v in values.values()):
                raise ValueError
            bases = {"bytes_in_basis": bytes_in_basis or bytes_basis,
                     "bytes_out_basis": bytes_out_basis or bytes_basis}
            if any(v is not None and (type(v) is not str or v not in BYTE_BASES)
                   for v in bases.values()):
                raise ValueError
            with self._lock:
                record = self._active.get(token)
                if record is not None:
                    record.update({key: value for key, value in values.items() if value is not None})
                    record.update({key: value for key, value in bases.items() if value is not None})
        except Exception:
            self._failure("counts")

    @contextmanager
    def stage(self, name, *, rows=None, bytes_in=None, bytes_out=None,
              bytes_basis=None, bytes_in_basis=None, bytes_out_basis=None,
              work_day=None, mode=None):
        if type(name) is not str or name not in STAGES:
            self._failure("stage")
            name = "unclassified"
        started_at, started_utc, started_mono = self._capture()
        invalid_day = invalid_mode = False
        if work_day is not None:
            try:
                if type(work_day) is int:
                    work_day = str(work_day)
                if type(work_day) is not str or re.fullmatch(r"[0-9]{8}", work_day) is None:
                    raise ValueError
                datetime.strptime(work_day, "%Y%m%d")
            except (TypeError, ValueError, OverflowError):
                work_day, invalid_day = None, True
                self._failure("context")
        if mode is not None and (type(mode) is not str or mode not in ("paper", "real")):
            mode, invalid_mode = None, True
            self._failure("context")
        with self._lock:
            self._sequence += 1
            token = self._sequence
            thread = threading.get_ident()
            parents = [r["token"] for r in self._active.values() if r["thread"] == thread]
            parent = max(parents) if parents else None
            parent_record = self._active.get(parent)
            if parent_record is not None:
                if work_day is None and not invalid_day:
                    work_day = parent_record["work_day"]
                if mode is None and not invalid_mode:
                    mode = parent_record["mode"]
            record = {"token": token, "stage": name, "thread": thread,
                      "parent_sequence": parent,
                      "work_day": work_day, "mode": mode,
                      "started_at": started_at, "started_utc": started_utc,
                      "started_mono": started_mono, "rows": None,
                      "bytes_in": None, "bytes_out": None,
                      "bytes_in_basis": None, "bytes_out_basis": None}
            if len(self._active) < 64:
                self._active[token] = record
            else:
                self._failure("overlap")
        self._set_counts(token, rows, bytes_in, bytes_out,
                         bytes_basis, bytes_in_basis, bytes_out_basis)
        self._emit({"schema": "tracker-stage-timing-v1", "kind": "begin", "stage": name,
                    "run_id": self._run_id, "thread_id": thread,
                    "owner_thread": thread == self._owner_thread, "parent_sequence": parent,
                    "work_day": work_day, "mode": mode,
                    "sequence": token, "observed_at": started_at, "observed_at_utc": started_utc,
                    "duration_seconds": None, "failed": False,
                    "rows": record["rows"], "bytes_in": record["bytes_in"], "bytes_out": record["bytes_out"],
                    "bytes_in_basis": record["bytes_in_basis"], "bytes_out_basis": record["bytes_out_basis"],
                    "current_rss_bytes": self._current_rss, "peak_rss_bytes": self._peak_rss,
                    "rss_scope": "whole_receiver_process_not_stage_attribution",
                    "current_rss_basis": self._current_rss_basis, "peak_rss_basis": self._peak_rss_basis})
        failed = False
        try:
            yield _Sample(self, token)
        except BaseException:
            failed = True
            raise
        finally:
            ended_at, ended_utc, ended_mono = self._capture()
            duration = None
            if started_mono is not None and ended_mono is not None:
                if ended_mono >= started_mono:
                    duration = ended_mono - started_mono
                else:
                    self._failure("clock")
            with self._lock:
                self._active.pop(token, None)
                if thread == self._owner_thread:
                    self._last_completed_at = ended_at
                    self._last_stage, self._last_duration = name, duration
                    self._last_work_day, self._last_mode = work_day, mode
                else:
                    self._last_background_completed_at = ended_at
                completed = {"stage": name, "started_at": started_at, "ended_at": ended_at,
                             "owner_thread": thread == self._owner_thread,
                             "sequence": token, "parent_sequence": parent,
                             "work_day": work_day, "mode": mode,
                             "duration_seconds": duration, "failed": failed,
                             "rows": record["rows"], "bytes_in": record["bytes_in"], "bytes_out": record["bytes_out"],
                             "bytes_in_basis": record["bytes_in_basis"], "bytes_out_basis": record["bytes_out_basis"]}
                self._recent.append(completed)
            self._emit({"schema": "tracker-stage-timing-v1", "kind": "end", "stage": name,
                        "run_id": self._run_id, "thread_id": thread,
                        "owner_thread": thread == self._owner_thread, "parent_sequence": parent,
                        "work_day": work_day, "mode": mode,
                        "sequence": token, "observed_at": ended_at, "observed_at_utc": ended_utc,
                        "started_at": started_at, "started_at_utc": started_utc,
                        "duration_seconds": duration, "failed": failed,
                        "rows": record["rows"], "bytes_in": record["bytes_in"], "bytes_out": record["bytes_out"],
                        "bytes_in_basis": record["bytes_in_basis"], "bytes_out_basis": record["bytes_out_basis"],
                        "current_rss_bytes": self._current_rss, "peak_rss_bytes": self._peak_rss,
                        "rss_scope": "whole_receiver_process_not_stage_attribution",
                        "current_rss_basis": self._current_rss_basis, "peak_rss_basis": self._peak_rss_basis})
