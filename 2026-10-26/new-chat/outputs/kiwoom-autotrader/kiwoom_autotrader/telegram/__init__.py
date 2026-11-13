"""Telegram bot transport and personal-account channel reader."""
from .client import TelegramClient, TelegramAPIError, _RejectRedirects
# Preserve the former module's urllib-based injection boundary.
from .client import urllib
