"""Compatibility import; implementation lives in utils.keychain."""
import importlib as _importlib
import sys as _sys
_implementation = _importlib.import_module("kiwoom_autotrader.utils.keychain")
_sys.modules[__name__] = _implementation
