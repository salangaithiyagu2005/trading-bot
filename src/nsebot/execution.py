from __future__ import annotations

import time
from decimal import Decimal
from typing import Callable

from nsebot.instruments import Instrument
from nsebot.risk import Bracket, stop_limit_price


class ExecutionError(RuntimeError):
    pass


class Execution:
    def __init__(
        self,
        gateway,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ):
        self.gateway = gateway
        self._sleep = sleep
        self._monotonic = monotonic

    def _place(self, **params) -> str:
        return self.gateway.place(variety="regular", **params)

    def entry(
        self,
        instrument: Instrument,
        bracket: Bracket,
        tag: str,
    ) -> str:
        if len(tag) > 20:
            raise ValueError("Kite order tag exceeds 20 characters")
        return self._place(
            exchange="NFO",
            tradingsymbol=instrument.tradingsymbol,
            transaction_type="BUY",
            quantity=bracket.quantity,
            product="MIS",
            order_type="LIMIT",
            price=str(bracket.entry_limit),
            validity="DAY",
            tag=tag,
        )

    def protective_orders(
        self,
        instrument: Instrument,
        bracket: Bracket,
        tag: str,
    ) -> tuple[str, str]:
        stop_id = self._place(
            exchange="NFO",
            tradingsymbol=instrument.tradingsymbol,
            transaction_type="SELL",
            quantity=bracket.quantity,
            product="MIS",
            order_type="SL",
            trigger_price=str(bracket.stop_trigger),
            price=str(stop_limit_price(bracket.stop_trigger, instrument.tick_size)),
            validity="DAY",
            tag=tag,
        )
        try:
            target_id = self._place(
                exchange="NFO",
                tradingsymbol=instrument.tradingsymbol,
                transaction_type="SELL",
                quantity=bracket.quantity,
                product="MIS",
                order_type="LIMIT",
                price=str(bracket.target),
                validity="DAY",
                tag=tag,
            )
        except Exception:
            self.cancel_and_wait(stop_id)
            raise
        return stop_id, target_id

    def cancel_and_wait(self, order_id: str, timeout: float = 3.0) -> dict:
        self.gateway.cancel(order_id, variety="regular")
        deadline = self._monotonic() + timeout
        while self._monotonic() < deadline:
            for order in self.gateway.orders():
                if str(order.get("order_id")) != str(order_id):
                    continue
                status = str(order.get("status", "")).lower()
                if status in {"cancelled", "complete", "rejected"}:
                    return order
            self._sleep(0.1)
        raise ExecutionError(f"order {order_id} did not reach a terminal state after cancel")

    def cancel_sibling(self, order_id: str) -> dict:
        return self.cancel_and_wait(order_id)

    def flatten(
        self,
        instrument: Instrument,
        quantity: int,
        tag: str,
        bid: Decimal,
        max_steps: int = 2,
    ) -> str:
        if quantity <= 0 or bid <= 0 or max_steps < 0:
            raise ValueError("invalid flatten quantity, bid, or steps")
        order_id = self._place(
            exchange="NFO",
            tradingsymbol=instrument.tradingsymbol,
            transaction_type="SELL",
            quantity=quantity,
            product="MIS",
            order_type="LIMIT",
            price=str(bid),
            validity="DAY",
            tag=tag,
        )
        for _ in range(max_steps):
            self._sleep(5)
            order = next(
                (item for item in self.gateway.orders()
                 if str(item.get("order_id")) == str(order_id)),
                None,
            )
            if order is None:
                raise ExecutionError(f"flatten order {order_id} is missing from broker orders")
            if str(order.get("status", "")).lower() == "complete":
                return order_id
            self.gateway.modify(
                order_id,
                variety="regular",
                price=str(max(instrument.tick_size, bid - 2 * instrument.tick_size)),
            )
            bid = max(instrument.tick_size, bid - 2 * instrument.tick_size)
        return order_id
