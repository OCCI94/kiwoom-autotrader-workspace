"""Compatibility import; implementation lives in telegram.setup."""
import importlib as _importlib
import sys as _sys
_implementation = _importlib.import_module("kiwoom_autotrader.telegram.setup")
_sys.modules[__name__] = _implementation
