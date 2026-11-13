"""Compatibility import; implementation lives in trading.simulation."""
import importlib as _importlib
import sys as _sys
_implementation = _importlib.import_module("kiwoom_autotrader.trading.simulation")
_sys.modules[__name__] = _implementation
