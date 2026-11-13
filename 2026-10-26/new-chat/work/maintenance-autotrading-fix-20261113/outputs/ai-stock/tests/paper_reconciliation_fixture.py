"""Synthetic pre-order reconciliation for isolated order-policy tests."""
from unittest.mock import Mock


def fake_reconciliation(symbol, *, side, quantity, price, guard=None):
    guard = guard if guard is not None else Mock()
    if side == "buy":
        guard.reconciliation_evidence = {
            "schema_version": 1, "symbol": symbol, "pre_order_held_qty": 0,
            "pre_order_pur_amt_krw": 0, "remaining_principal_krw": 1000000,
            "order_quantity": int(quantity), "limit_price_krw": int(price),
            "observed_at": "2020-01-01T00:00:00+09:00", "holdings_source": "unknown",
        }
    return guard
