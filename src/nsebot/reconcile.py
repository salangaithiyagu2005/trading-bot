from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ReconcileResult:
    local_qty: int
    broker_qty: int
    action: str


def signed_net_quantity(positions: dict, symbol: str) -> int:
    net = positions.get("net", [])
    for row in net:
        if row.get("tradingsymbol") == symbol:
            return int(row.get("quantity", 0))
    return 0


def reconcile(local_qty: int, positions: dict, symbol: str) -> ReconcileResult:
    if local_qty < 0:
        raise ValueError("local quantity must be non-negative")
    broker_qty = signed_net_quantity(positions, symbol)
    if broker_qty < 0:
        action = "cover_short"
    elif local_qty == 0 and broker_qty > 0:
        action = "flatten_unexpected_long"
    elif local_qty > 0 and broker_qty == 0:
        action = "local_position_missing"
    elif broker_qty != local_qty:
        action = "quantity_mismatch"
    else:
        action = "match"
    return ReconcileResult(local_qty, broker_qty, action)
