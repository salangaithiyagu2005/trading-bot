from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from nsebot.indicators import volume_sma
from nsebot.clock import parse_hhmm
from nsebot.rules import Rules
from nsebot.types import Bar, Bias, Reason, Side


class SetEval(enum.Enum):
    IGNORE = "IGNORE"
    ARM = "ARM"
    DROP = "DROP"
    CONFIRM = "CONFIRM"


@dataclass(frozen=True)
class SetContext:
    bias: Bias
    veto: Reason | None
    now: datetime
    rules: Rules
    or_high: Decimal
    or_low: Decimal
    vwap: Decimal | None
    ema9_5m: Decimal
    ema21_5m: Decimal
    closed_1m: Bar | None
    closed_5m: Bar | None
    prev_1m_volumes: tuple[int, ...]
    rsi_1m: Decimal | None
    rsi_5m: Decimal | None
    armed: bool
    armed_bar_5m_index: int | None
    closed_5m_index: int | None
    side: Side | None
    rsi_1m_prev: Decimal | None = None
    prior_5m_outside_or: bool = False


def _drop_or_ignore(ctx: SetContext) -> SetEval:
    return SetEval.DROP if ctx.armed else SetEval.IGNORE


def _regime(ctx: SetContext, bullish: bool) -> bool:
    if ctx.veto is not None or ctx.vwap is None:
        return False
    if (
        ctx.now.time() < parse_hhmm(ctx.rules.session.no_trade_before)
        or ctx.now.time() >= parse_hhmm(ctx.rules.session.no_new_trade_after)
    ):
        return False
    bar = ctx.closed_1m or ctx.closed_5m
    if bar is None:
        return False
    if bullish:
        return bar.close > ctx.vwap and ctx.ema9_5m > ctx.ema21_5m
    return bar.close < ctx.vwap and ctx.ema9_5m < ctx.ema21_5m


def _orb_vwap(ctx: SetContext) -> SetEval:
    if ctx.bias not in {Bias.BULLISH, Bias.BEARISH}:
        return _drop_or_ignore(ctx)
    bullish = ctx.bias == Bias.BULLISH
    if not _regime(ctx, bullish):
        return _drop_or_ignore(ctx)
    if ctx.armed and ctx.closed_5m is not None:
        if ctx.armed_bar_5m_index is None or ctx.closed_5m_index is None:
            return SetEval.DROP
        elapsed = ctx.closed_5m_index - ctx.armed_bar_5m_index
        if elapsed > ctx.rules.sets.confirm_within_bars_5m:
            return SetEval.DROP
        bar = ctx.closed_5m
        rsi = ctx.rsi_5m
        if bullish:
            if ctx.vwap is not None and bar.close < ctx.vwap:
                return SetEval.DROP
            valid = (
                bar.close > ctx.or_high and bar.close > bar.open
                and rsi is not None and Decimal("52") <= rsi <= Decimal("72")
                and ctx.ema9_5m > ctx.ema21_5m
            )
        else:
            valid = (
                bar.close < ctx.or_low and bar.close < bar.open
                and rsi is not None and Decimal("28") <= rsi <= Decimal("48")
                and ctx.ema9_5m < ctx.ema21_5m
            )
            if ctx.vwap is not None and bar.close >= ctx.vwap:
                return SetEval.DROP
        return SetEval.CONFIRM if valid else SetEval.IGNORE
    if ctx.armed or ctx.closed_1m is None:
        return SetEval.IGNORE
    average_volume = volume_sma(list(ctx.prev_1m_volumes), ctx.rules.sets.volume_lookback)
    if average_volume is None or ctx.rsi_1m is None or ctx.vwap is None:
        return SetEval.IGNORE
    bar = ctx.closed_1m
    volume_ok = Decimal(bar.volume) > ctx.rules.sets.volume_mult * average_volume
    if bullish:
        valid = (
            bar.close > ctx.or_high and bar.close > ctx.vwap and volume_ok
            and Decimal("55") <= ctx.rsi_1m <= Decimal("75")
        )
    else:
        valid = (
            bar.close < ctx.or_low and bar.close < ctx.vwap and volume_ok
            and Decimal("25") <= ctx.rsi_1m <= Decimal("45")
        )
    return SetEval.ARM if valid else SetEval.IGNORE


def _vwap_pullback(ctx: SetContext) -> SetEval:
    if (
        ctx.bias not in {Bias.BULLISH, Bias.BEARISH}
        or ctx.veto is not None
        or ctx.vwap is None
        or not ctx.prior_5m_outside_or
        or ctx.now.time() < parse_hhmm(ctx.rules.session.no_trade_before)
        or ctx.now.time() >= parse_hhmm(ctx.rules.session.no_new_trade_after)
    ):
        return _drop_or_ignore(ctx)
    if ctx.armed and ctx.closed_5m is not None:
        if ctx.armed_bar_5m_index is None or ctx.closed_5m_index is None:
            return SetEval.DROP
        if ctx.closed_5m_index - ctx.armed_bar_5m_index > 1:
            return SetEval.DROP
        bar = ctx.closed_5m
        if ctx.bias == Bias.BULLISH:
            return SetEval.CONFIRM if (
                bar.close > bar.open and bar.close > ctx.vwap and bar.close > ctx.ema9_5m
            ) else SetEval.DROP
        if ctx.bias == Bias.BEARISH:
            return SetEval.CONFIRM if (
                bar.close < bar.open and bar.close < ctx.vwap and bar.close < ctx.ema9_5m
            ) else SetEval.DROP
    if ctx.armed:
        return SetEval.IGNORE
    if ctx.rsi_1m_prev is None or ctx.rsi_1m is None or ctx.closed_1m is None:
        return SetEval.IGNORE
    bar = ctx.closed_1m
    touched = bar.low <= ctx.vwap <= bar.high or bar.low <= ctx.ema9_5m <= bar.high
    crossed_up = ctx.rsi_1m_prev < 50 <= ctx.rsi_1m
    crossed_down = ctx.rsi_1m_prev > 50 >= ctx.rsi_1m
    if ctx.bias == Bias.BULLISH:
        if touched and bar.close > ctx.vwap and bar.close > ctx.ema9_5m and crossed_up:
            return SetEval.ARM
    elif ctx.bias == Bias.BEARISH:
        if touched and bar.close < ctx.vwap and bar.close < ctx.ema9_5m and crossed_down:
            return SetEval.ARM
    return SetEval.DROP if ctx.armed else SetEval.IGNORE


def evaluate(set_id: str, ctx: SetContext) -> SetEval:
    if set_id == "orb_vwap":
        return _orb_vwap(ctx)
    if set_id == "vwap_pullback":
        return _vwap_pullback(ctx)
    raise ValueError(f"Unknown signal set: {set_id}")
