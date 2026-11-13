"""Compatibility import; implementation lives in telegram.collector."""
import importlib as _importlib
import sys as _sys
_implementation = _importlib.import_module("kiwoom_autotrader.telegram.collector")
_sys.modules[__name__] = _implementation
