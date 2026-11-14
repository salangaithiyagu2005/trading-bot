from __future__ import annotations

from decimal import Decimal

from nsebot.types import Bar


def ema(closes: list[Decimal], period: int) -> Decimal | None:
    if period < 1:
        raise ValueError("period must be positive")
    if len(closes) < period:
        return None
    value = sum(closes[:period], Decimal("0")) / period
    multiplier = Decimal(2) / Decimal(period + 1)
    for close in closes[period:]:
        value = (close - value) * multiplier + value
    return value


def rsi_wilder(closes: list[Decimal], period: int) -> Decimal | None:
    if period < 1:
        raise ValueError("period must be positive")
    if len(closes) <= period:
        return None
    changes = [right - left for left, right in zip(closes, closes[1:])]
    gains = [max(change, Decimal("0")) for change in changes]
    losses = [max(-change, Decimal("0")) for change in changes]
    average_gain = sum(gains[:period], Decimal("0")) / period
    average_loss = sum(losses[:period], Decimal("0")) / period
    for gain, loss in zip(gains[period:], losses[period:]):
        average_gain = (average_gain * (period - 1) + gain) / period
        average_loss = (average_loss * (period - 1) + loss) / period
    if average_loss == 0:
        return Decimal("100") if average_gain > 0 else Decimal("50")
    relative_strength = average_gain / average_loss
    return Decimal("100") - Decimal("100") / (Decimal("1") + relative_strength)


def atr_wilder(bars: list[Bar], period: int) -> Decimal | None:
    if period < 1:
        raise ValueError("period must be positive")
    if len(bars) <= period:
        return None
    true_ranges: list[Decimal] = []
    for previous, current in zip(bars, bars[1:]):
        true_ranges.append(
            max(
                current.high - current.low,
                abs(current.high - previous.close),
                abs(current.low - previous.close),
            )
        )
    value = sum(true_ranges[:period], Decimal("0")) / period
    for true_range in true_ranges[period:]:
        value = (value * (period - 1) + true_range) / period
    return value


def vwap(bars_today_1m: list[Bar]) -> Decimal | None:
    total_volume = sum(bar.volume for bar in bars_today_1m)
    if total_volume <= 0:
        return None
    value = sum(
        ((bar.high + bar.low + bar.close) / 3) * bar.volume
        for bar in bars_today_1m
    )
    return value / total_volume


def volume_sma(volumes: list[int], lookback: int) -> Decimal | None:
    if lookback < 1:
        raise ValueError("lookback must be positive")
    if len(volumes) < lookback:
        return None
    return Decimal(sum(volumes[-lookback:])) / lookback
