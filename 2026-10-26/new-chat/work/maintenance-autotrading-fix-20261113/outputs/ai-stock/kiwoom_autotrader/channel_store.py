"""Compatibility import; implementation lives in telegram.channel_store."""
import importlib as _importlib
import sys as _sys
_implementation = _importlib.import_module("kiwoom_autotrader.telegram.channel_store")
_sys.modules[__name__] = _implementation
