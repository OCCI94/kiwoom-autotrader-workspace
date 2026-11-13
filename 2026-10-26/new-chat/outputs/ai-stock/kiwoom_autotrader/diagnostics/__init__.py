"""Offline, read-only diagnostics; importing this package performs no I/O."""

from .audit import audit_local
from .models import AuditReport, Check, Verdict

__all__ = ["audit_local", "AuditReport", "Check", "Verdict"]
