"""Compatibility import; implementation lives in utils.telegram_service."""
import importlib as _importlib
import sys as _sys
_implementation = _importlib.import_module("kiwoom_autotrader.utils.telegram_service")
_sys.modules[__name__] = _implementation
