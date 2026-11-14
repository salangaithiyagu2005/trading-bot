from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from nsebot.instruments import Instrument
from nsebot.rules import Rules
from nsebot.types import Reason


class NoExpiryError(ValueError):
    pass


def is_expiry_day(options: list[Instrument], day: date) -> bool:
    return any(option.expiry == day for option in options)


def select_expiry(options: list[Instrument], day: date, rules: Rules) -> date:
    expiries = sorted({option.expiry for option in options if option.expiry is not None})
    if rules.universe.expiry == "next_after_today":
        eligible = [expiry for expiry in expiries if expiry > day]
    else:
        minimum = day + timedelta(days=rules.universe.min_dte_days)
        eligible = [expiry for expiry in expiries if expiry >= minimum]
    if not eligible:
        raise NoExpiryError("no option expiry satisfies the configured DTE rule")
    return eligible[0]


def select_atm(
    options: list[Instrument],
    expiry: date,
    right: str,
    spot: Decimal,
) -> Instrument:
    if right not in {"CE", "PE"}:
        raise ValueError("right must be CE or PE")
    candidates = [
        option for option in options
        if option.expiry == expiry and option.instrument_type == right and option.strike is not None
    ]
    if not candidates:
        raise ValueError(f"no {right} option available for expiry {expiry}")
    return min(candidates, key=lambda option: (abs(option.strike - spot), option.strike))


def liquidity_ok(
    bid: Decimal | None,
    ask: Decimal | None,
    oi: int,
    rules: Rules,
    lot_size: int = 1,
    qty: int = 1,
    bid_qty: int | None = None,
    ask_qty: int | None = None,
) -> Reason | None:
    if bid is None or ask is None or bid <= 0 or ask <= 0 or ask < bid:
        return Reason.LIQUIDITY_REJECT
    mid = (bid + ask) / 2
    if (ask - bid) / mid * 100 > rules.universe.max_bid_ask_pct_of_mid:
        return Reason.LIQUIDITY_REJECT
    if oi < max(rules.universe.min_oi, rules.universe.min_oi_lots * lot_size):
        return Reason.LIQUIDITY_REJECT
    if (bid_qty is not None and bid_qty < qty) or (ask_qty is not None and ask_qty < qty):
        return Reason.LIQUIDITY_REJECT
    if mid < rules.risk.min_premium_inr:
        return Reason.PREMIUM_TOO_SMALL
    return None
