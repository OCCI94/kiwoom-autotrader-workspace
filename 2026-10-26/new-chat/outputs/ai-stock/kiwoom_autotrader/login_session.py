"""Compatibility import; implementation lives in api.session."""
import importlib as _importlib
import sys as _sys
_implementation = _importlib.import_module("kiwoom_autotrader.api.session")
_sys.modules[__name__] = _implementation
