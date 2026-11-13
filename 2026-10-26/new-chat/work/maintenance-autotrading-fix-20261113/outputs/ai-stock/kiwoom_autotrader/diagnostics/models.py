"""Fixed diagnostic verdicts; never serialize arbitrary source objects."""
from dataclasses import dataclass, field
from enum import Enum


class Verdict(str, Enum):
    PASS = "PASS"
    NOT_MET = "NOT_MET"
    OBSERVE = "OBSERVE"
    UNKNOWN = "UNKNOWN"
    UNSUPPORTED = "UNSUPPORTED"


@dataclass(frozen=True)
class Check:
    name: str
    verdict: Verdict
    reason_code: str
    statistics: tuple[tuple[str, object], ...] = ()

    def to_dict(self):
        return {"name": self.name, "verdict": self.verdict.value,
                "reason_code": self.reason_code, "statistics": dict(self.statistics)}


@dataclass(frozen=True)
class Provenance:
    kind: str
    sha256: str
    bytes: int

    def to_dict(self):
        return {"kind": self.kind, "sha256": self.sha256, "bytes": self.bytes}


@dataclass(frozen=True)
class AuditReport:
    generated_at: str
    checks: tuple[Check, ...]
    sources: tuple[Provenance, ...] = field(default_factory=tuple)

    def to_dict(self):
        return {"schema_version": 1, "audit_kind": "offline_local_records",
                "generated_at": self.generated_at,
                "inspection_completed": True,
                "readiness_state": "UNKNOWN",
                "readiness_reason": "CURRENT_ACCOUNT_AND_RUNTIME_NOT_VERIFIED",
                "account_reconciled": False,
                "checks": [item.to_dict() for item in self.checks],
                "sources": [item.to_dict() for item in self.sources]}
