"""Compatibility import; implementation lives in api.client."""
import importlib as _importlib
import sys as _sys
_implementation = _importlib.import_module("kiwoom_autotrader.api.client")
_sys.modules[__name__] = _implementation
