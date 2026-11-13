"""Explicit adapter boundary. No legacy/DMG adapter is shipped or guessed."""
from typing import Protocol
from pathlib import Path


class HistoricalAdapter(Protocol):
    """A future reviewed adapter writes v1 files without changing its source.

    Implementations must document every source-field mapping, null/time meaning,
    lineage and pseudonym scheme. They must not decrypt, fetch accounts, infer
    executions/reasons, execute packaged scripts, or import operational state.
    Their output must independently pass validate_bundle before import.
    """
    name: str
    version: str

    def convert(self, source: Path, destination: Path) -> Path:
        """Return the explicit canonical bundle directory."""
        ...
