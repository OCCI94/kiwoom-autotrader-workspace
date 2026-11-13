"""Compatibility import; implementation lives in utils.dependencies."""
import importlib as _importlib
import sys as _sys
_implementation = _importlib.import_module("kiwoom_autotrader.utils.dependencies")
_sys.modules[__name__] = _implementation
