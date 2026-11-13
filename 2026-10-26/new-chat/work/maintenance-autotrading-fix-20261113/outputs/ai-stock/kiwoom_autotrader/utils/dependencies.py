"""Load the optional personal-account client from this app's private install."""

import sys

from kiwoom_autotrader.utils.telegram_service import service_paths


def enable_account_dependencies() -> None:
    directory = service_paths().base / "python-deps"
    if directory.is_dir() and str(directory) not in sys.path:
        sys.path.insert(0, str(directory))
