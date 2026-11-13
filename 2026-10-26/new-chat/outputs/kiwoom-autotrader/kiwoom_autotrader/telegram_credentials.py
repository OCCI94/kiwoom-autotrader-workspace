"""Compatibility import; implementation lives in telegram.bot_credentials."""
import importlib as _importlib
import sys as _sys
_implementation = _importlib.import_module("kiwoom_autotrader.telegram.bot_credentials")
_sys.modules[__name__] = _implementation
