"""Compatibility import; implementation lives in api.equity_rankings."""
import importlib as _importlib
import sys as _sys
_implementation = _importlib.import_module("kiwoom_autotrader.api.equity_rankings")
_sys.modules[__name__] = _implementation
