"""Stop this application's channel worker without affecting other programs."""


def stop_channels() -> bool:
    """Return whether this application's collector shutdown was confirmed."""
    from kiwoom_autotrader.utils.account_service import stop_collector_service
    try:
        stop_collector_service()
    except Exception:
        return False
    return True
