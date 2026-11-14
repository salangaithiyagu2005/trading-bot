from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from nsebot.risk import Bracket, stop_limit_price


@dataclass(frozen=True)
class PaperFill:
    symbol: str
    quantity: int
    price: Decimal
    filled_at: datetime


@dataclass
class PaperPosition:
    symbol: str
    quantity: int
    entry_price: Decimal
    stop_trigger: Decimal
    stop_limit_floor: Decimal
    target: Decimal
    opened_at: datetime
    exit_price: Decimal | None = None
    exit_reason: str | None = None

    @property
    def is_open(self) -> bool:
        return self.exit_price is None


class PaperBroker:
    def __init__(self) -> None:
        self.position: PaperPosition | None = None

    def enter(
        self,
        symbol: str,
        quantity: int,
        ask: Decimal,
        bracket: Bracket,
        tick: Decimal,
        now: datetime,
    ) -> PaperFill:
        if self.position is not None and self.position.is_open:
            raise RuntimeError("paper broker already has an open position")
        if quantity != bracket.quantity or quantity <= 0 or ask <= 0 or tick <= 0:
            raise ValueError("invalid paper entry")
        if ask != bracket.entry_limit:
            raise ValueError("paper entry limit must equal the decision-time ask")
        fill = bracket.entry_limit
        self.position = PaperPosition(
            symbol=symbol,
            quantity=quantity,
            entry_price=fill,
            stop_trigger=bracket.stop_trigger,
            stop_limit_floor=stop_limit_price(bracket.stop_trigger, tick),
            target=bracket.target,
            opened_at=now,
        )
        return PaperFill(symbol, quantity, fill, now)

    def on_tick(
        self,
        last: Decimal,
        bid: Decimal | None,
        now: datetime | None = None,
    ) -> PaperFill | None:
        position = self.position
        if position is None or not position.is_open:
            return None
        if last <= position.stop_trigger:
            price = min(last, bid) if bid is not None and bid > 0 else last
            if price >= position.stop_limit_floor:
                position.exit_price = price
                position.exit_reason = "stop_hit"
                return PaperFill(position.symbol, position.quantity, price, now or position.opened_at)
        if last >= position.target:
            price = max(position.target, bid) if bid is not None and bid > 0 else position.target
            position.exit_price = price
            position.exit_reason = "target_hit"
            return PaperFill(position.symbol, position.quantity, price, now or position.opened_at)
        return None

    def exit_at_marketable_limit(
        self,
        last: Decimal,
        bid: Decimal | None,
        reason: str,
        now: datetime | None = None,
    ) -> PaperFill | None:
        position = self.position
        if position is None or not position.is_open:
            return None
        price = bid if bid is not None and bid > 0 else last
        if price <= 0:
            raise ValueError("exit price must be positive")
        position.exit_price = price
        position.exit_reason = reason
        return PaperFill(position.symbol, position.quantity, price, now or position.opened_at)

    def step_stop_limit_floor(self, tick: Decimal) -> None:
        if tick <= 0:
            raise ValueError("tick must be positive")
        if self.position is not None and self.position.is_open:
            self.position.stop_limit_floor = max(
                tick,
                self.position.stop_limit_floor - 2 * tick,
            )
