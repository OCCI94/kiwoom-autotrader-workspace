"""Resolve the project/deployed runtime root independently of working directory."""
from pathlib import Path

def project_root() -> Path:
    return Path(__file__).resolve().parents[2]
