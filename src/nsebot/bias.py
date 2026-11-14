from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from nsebot.rules import Rules
from nsebot.types import Bias, Reason


@dataclass(frozen=True)
class BiasLine:
    name: str
    points: int
    detail: str


@dataclass(frozen=True)
class BiasObs:
    nifty_price: Decimal
    nifty_prev_close: Decimal
    nifty_vwap: Decimal
    or_high: Decimal
    or_low: Decimal
    or_open: Decimal
    or_close: Decimal
    close_5m: Decimal
    ema20_5m: Decimal
    ema9_5m: Decimal
    ema21_5m: Decimal
    breadth_above_vwap: int
    breadth_total: int
    bank_price: Decimal
    bank_vwap: Decimal
    bank_or_mid: Decimal
    vix_price: Decimal
    vix_prev_close: Decimal
    atr15: Decimal | None
    inside_or: bool


@dataclass(frozen=True)
class BiasResult:
    bias: Bias
    score: int
    lines: tuple[BiasLine, ...]
    veto: Reason | None


def _sign_band(value: Decimal, reference: Decimal, band_pct: Decimal, flat_pct: Decimal) -> int:
    if reference <= 0:
        return 0
    change_pct = (value - reference) / reference * 100
    if abs(change_pct) < flat_pct:
        return 0
    magnitude = 2 if abs(change_pct) >= band_pct else 1
    return magnitude if change_pct > 0 else -magnitude


def _opening_range(obs: BiasObs, rules: Rules) -> int:
    span = obs.or_high - obs.or_low
    if span <= 0:
        return 0
    body_ratio = abs(obs.or_close - obs.or_open) / span
    if body_ratio < rules.bias.or_body_min_ratio:
        return 0
    position = (obs.or_close - obs.or_low) / span
    if position >= rules.bias.or_pos_strong and obs.or_close > obs.or_open:
        return 2
    if position >= rules.bias.or_pos_mild and obs.or_close > obs.or_open:
        return 1
    if position <= 1 - rules.bias.or_pos_strong and obs.or_close < obs.or_open:
        return -2
    if position <= 1 - rules.bias.or_pos_mild and obs.or_close < obs.or_open:
        return -1
    return 0


def _ema_trend(obs: BiasObs, rules: Rules) -> int:
    if obs.ema20_5m <= 0:
        return 0
    separation_pct = (obs.close_5m - obs.ema20_5m) / obs.ema20_5m * 100
    bullish_stack = obs.close_5m > obs.ema20_5m and obs.ema9_5m > obs.ema21_5m
    bearish_stack = obs.close_5m < obs.ema20_5m and obs.ema9_5m < obs.ema21_5m
    if separation_pct >= rules.bias.ema_sep_pct and bullish_stack:
        return 2
    if separation_pct <= -rules.bias.ema_sep_pct and bearish_stack:
        return -2
    if separation_pct > 0 and bullish_stack:
        return 1
    if separation_pct < 0 and bearish_stack:
        return -1
    return 0


def _breadth(obs: BiasObs, rules: Rules) -> int:
    if obs.breadth_total < rules.bias.breadth_min_names or obs.breadth_total <= 0:
        return 0
    percentage = Decimal(obs.breadth_above_vwap) * 100 / Decimal(obs.breadth_total)
    if percentage >= rules.bias.breadth_strong:
        return 2
    if percentage >= rules.bias.breadth_mild:
        return 1
    if percentage <= rules.bias.breadth_bear_strong:
        return -2
    if percentage <= rules.bias.breadth_bear_mild:
        return -1
    return 0


def compute_bias(obs: BiasObs, rules: Rules) -> BiasResult:
    lines = (
        BiasLine("prev_close", _sign_band(
            obs.nifty_price, obs.nifty_prev_close, rules.bias.band_pct, Decimal("0")
        ), "Nifty futures versus previous close"),
        BiasLine("vwap", _sign_band(
            obs.nifty_price, obs.nifty_vwap, rules.bias.band_pct, rules.bias.vwap_flat_pct
        ), "Nifty futures versus session VWAP"),
        BiasLine("opening_range", _opening_range(obs, rules), "Nifty futures opening range"),
        BiasLine("ema_trend", _ema_trend(obs, rules), "Nifty futures EMA trend"),
        BiasLine("breadth", _breadth(obs, rules), "Nifty 50 breadth"),
        BiasLine("banknifty", 0, "Bank Nifty futures agreement"),
    )
    bank_points = 0
    bank_mid = obs.bank_or_mid
    above = obs.bank_price > obs.bank_vwap and obs.bank_price > bank_mid
    below = obs.bank_price < obs.bank_vwap and obs.bank_price < bank_mid
    if above != below:
        candidate = _sign_band(
            obs.bank_price, obs.bank_vwap, rules.bias.band_pct,
            rules.bias.vwap_flat_pct,
        )
        if (above and candidate > 0) or (below and candidate < 0):
            bank_points = candidate
    lines = lines[:5] + (
        BiasLine("banknifty", bank_points, "Bank Nifty futures agreement"),
    )
    score = sum(line.points for line in lines)
    if score >= rules.bias.bullish_if_score_gte:
        bias = Bias.BULLISH
    elif score <= rules.bias.bearish_if_score_lte:
        bias = Bias.BEARISH
    else:
        bias = Bias.SIDEWAYS

    veto: Reason | None = None
    vix_change_pct = (
        (obs.vix_price - obs.vix_prev_close) / obs.vix_prev_close * 100
        if obs.vix_prev_close > 0 else Decimal("0")
    )
    if vix_change_pct >= rules.bias.vix_veto_up_pct and obs.inside_or:
        veto = Reason.VIX_UNSTABLE
    elif (
        obs.atr15 is not None and obs.atr15 > 0
        and obs.or_high - obs.or_low < rules.bias.dead_range_atr_frac * obs.atr15
    ):
        veto = Reason.DEAD_RANGE
    if (bias == Bias.BULLISH and bank_points < 0) or (bias == Bias.BEARISH and bank_points > 0):
        bias = Bias.SIDEWAYS
    return BiasResult(bias, score, lines, veto)
