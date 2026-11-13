"""Compatibility import; implementation lives in utils.account_service."""
import importlib as _importlib
import sys as _sys
_implementation = _importlib.import_module("kiwoom_autotrader.utils.account_service")
_sys.modules[__name__] = _implementation
