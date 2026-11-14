from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP, ROUND_UP

from nsebot.rules import Rules
from nsebot.types import Reason


@dataclass(frozen=True)
class Bracket:
    entry_limit: Decimal
    r: Decimal
    stop_trigger: Decimal
    target: Decimal
    quantity: int
    risk_inr: Decimal


def _round(price: Decimal, tick: Decimal, rounding: str) -> Decimal:
    if tick <= 0:
        raise ValueError("tick must be positive")
    units = (price / tick).quantize(Decimal("1"), rounding=rounding)
    return units * tick


def round_nearest(price: Decimal, tick: Decimal) -> Decimal:
    return _round(price, tick, ROUND_HALF_UP)


def round_down(price: Decimal, tick: Decimal) -> Decimal:
    return _round(price, tick, ROUND_DOWN)


def round_up(price: Decimal, tick: Decimal) -> Decimal:
    return _round(price, tick, ROUND_UP)


def entry_limit(bid: Decimal, ask: Decimal, tick: Decimal) -> Decimal:
    if bid < 0 or ask <= 0 or ask < bid:
        raise ValueError("invalid bid/ask")
    return round_nearest(ask, tick)


def stop_limit_price(
    stop_trigger: Decimal,
    tick: Decimal,
    buffer_pct: Decimal = Decimal("0.03"),
) -> Decimal:
    if not Decimal("0") <= buffer_pct < Decimal("1"):
        raise ValueError("buffer_pct must be in [0, 1)")
    return max(tick, round_down(stop_trigger * (1 - buffer_pct), tick))


def build_bracket(
    fill: Decimal,
    tick: Decimal,
    lot_size: int,
    rules: Rules,
) -> Bracket | Reason:
    if fill <= 0 or tick <= 0 or lot_size <= 0:
        raise ValueError("fill, tick, and lot_size must be positive")
    raw_r = fill * Decimal(rules.risk.premium_stop_pct) / 100
    stop_trigger = round_down(fill - raw_r, tick)
    target = round_up(
        fill + Decimal(rules.risk.reward_r_multiple) * raw_r,
        tick,
    )
    quantity = lot_size * rules.risk.max_lots
    risk_inr = (fill - stop_trigger) * quantity
    if stop_trigger <= 0 or risk_inr > rules.risk.max_risk_inr_per_trade:
        return Reason.RISK_CAP
    return Bracket(
        entry_limit=fill,
        r=fill - stop_trigger,
        stop_trigger=stop_trigger,
        target=target,
        quantity=quantity,
        risk_inr=risk_inr,
    )


def costs(buy: Decimal, sell: Decimal, qty: int, rules: Rules) -> Decimal:
    if buy < 0 or sell < 0 or qty <= 0:
        raise ValueError("buy/sell must be non-negative and qty positive")
    brokerage = Decimal(2) * rules.costs.brokerage_inr_per_executed_order
    stt = sell * qty * rules.costs.stt_sell_premium_rate
    txn = (buy + sell) * qty * rules.costs.txn_rate_on_premium
    sebi = (buy + sell) * qty * rules.costs.sebi_rate
    stamp = buy * qty * rules.costs.stamp_buy_rate
    gst = rules.costs.gst_rate * (brokerage + sebi + txn)
    return (brokerage + stt + txn + sebi + stamp + gst).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
