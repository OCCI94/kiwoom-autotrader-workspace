"""Resolve the project/deployed runtime root independently of working directory."""
from pathlib import Path
import os
import sys

def project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def app_data_root(home=None) -> Path:
    explicit_home = home is not None
    home = Path(home) if explicit_home else Path.home()
    if sys.platform == "win32":
        if not explicit_home and os.environ.get("LOCALAPPDATA"):
            return Path(os.environ["LOCALAPPDATA"]) / "KiwoomAutotrader"
        # Stay in the current user's private profile, without requiring admin.
        return home / "AppData" / "Local" / "KiwoomAutotrader"
    return home / "Library" / "Application Support" / "KiwoomAutotrader"
