"""Locally composed broker errors with narrowly classified IP diagnostics."""
import re
from kiwoom_autotrader.utils.public_ip import annotate_ip_error


class BrokerAPIError(RuntimeError):
    def __init__(self, message, *, code=None):
        # Existing clients emit only validated numeric codes in local messages.
        if code is None:
            match = re.search(r'\b(?:return_code|returnCode)=(8010|8050)(?=$|[)\s,;])', message)
            code = match[1] if match else None
        self.ip_related = type(code) in (int, str) and str(code) in {'8010', '8050'}
        self.code = code if self.ip_related else None
        super().__init__(annotate_ip_error(code, message) if self.ip_related else message)
