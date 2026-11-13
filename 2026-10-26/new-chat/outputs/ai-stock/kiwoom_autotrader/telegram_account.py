"""Compatibility import; implementation lives in telegram.account."""
import importlib as _importlib
import sys as _sys
_implementation = _importlib.import_module("kiwoom_autotrader.telegram.account")
_sys.modules[__name__] = _implementation
