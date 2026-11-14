from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from nsebot.rules import Rules
from nsebot.types import Bias


@dataclass(frozen=True)
class PriceSnap:
    token: int
    tradingsymbol: str
    last: Decimal
    prev_close: Decimal
    open_0915: Decimal
    volume_today: int


@dataclass(frozen=True)
class SectorScore:
    tradingsymbol: str
    token: int
    chg_pct: Decimal
    intraday_pct: Decimal
    rel_strength: Decimal


@dataclass(frozen=True)
class StockCandidate:
    token: int
    tradingsymbol: str
    sector: str
    chg_pct: Decimal
    intraday_pct: Decimal
    rel_strength: Decimal
    anti_chase_ok: bool
    in_winning_sector: bool


@dataclass(frozen=True)
class ScreenerResult:
    winning_sectors: tuple[SectorScore, ...]
    top_pool: tuple[StockCandidate, ...]
    shortlist: tuple[StockCandidate, ...]


def pct_move(now_px: Decimal, ref_px: Decimal) -> Decimal:
    if ref_px <= 0:
        raise ValueError("reference price must be positive")
    return (now_px - ref_px) / ref_px * 100


def _moves(snap: PriceSnap, nifty_chg: Decimal, nifty_intra: Decimal) -> tuple[Decimal, Decimal, Decimal]:
    chg = pct_move(snap.last, snap.prev_close)
    intra = pct_move(snap.last, snap.open_0915)
    return chg, intra, (chg - nifty_chg) + (intra - nifty_intra)


def rank_sectors(
    sector_snaps: list[PriceSnap],
    nifty_snap: PriceSnap,
    bias: Bias,
    rules: Rules,
) -> list[SectorScore]:
    if bias == Bias.SIDEWAYS:
        return []
    nifty_chg = pct_move(nifty_snap.last, nifty_snap.prev_close)
    nifty_intra = pct_move(nifty_snap.last, nifty_snap.open_0915)
    ranked: list[SectorScore] = []
    for snap in sector_snaps:
        try:
            chg, intra, relative = _moves(snap, nifty_chg, nifty_intra)
        except ValueError:
            continue
        if bias == Bias.BULLISH and chg > 0 and intra > 0 and relative > 0:
            ranked.append(SectorScore(snap.tradingsymbol, snap.token, chg, intra, relative))
        elif bias == Bias.BEARISH and chg < 0 and intra < 0 and relative < 0:
            ranked.append(SectorScore(snap.tradingsymbol, snap.token, chg, intra, relative))
    ranked.sort(key=lambda item: (-item.rel_strength, item.tradingsymbol) if bias == Bias.BULLISH
                else (item.rel_strength, item.tradingsymbol))
    return ranked[:rules.screener.top_sectors]


def screen_stocks(
    bias: Bias,
    nifty_snap: PriceSnap,
    sector_snaps: list[PriceSnap],
    stock_snaps: list[PriceSnap],
    sectors_by_symbol: dict[str, tuple[str, ...]],
    rules: Rules,
) -> ScreenerResult:
    if bias == Bias.SIDEWAYS:
        return ScreenerResult((), (), ())
    winners = rank_sectors(sector_snaps, nifty_snap, bias, rules)
    winning_names = {item.tradingsymbol for item in winners}
    nifty_chg = pct_move(nifty_snap.last, nifty_snap.prev_close)
    nifty_intra = pct_move(nifty_snap.last, nifty_snap.open_0915)
    candidates: list[StockCandidate] = []
    for snap in stock_snaps:
        try:
            chg, intra, relative = _moves(snap, nifty_chg, nifty_intra)
        except ValueError:
            continue
        mapped = sectors_by_symbol.get(snap.tradingsymbol, ())
        matched = [sector for sector in mapped if sector in winning_names]
        candidates.append(StockCandidate(
            token=snap.token,
            tradingsymbol=snap.tradingsymbol,
            sector=matched[0] if matched else (mapped[0] if mapped else ""),
            chg_pct=chg,
            intraday_pct=intra,
            rel_strength=relative,
            anti_chase_ok=(
                rules.screener.min_stock_move_pct <= abs(chg)
                <= rules.screener.max_stock_move_pct and snap.volume_today > 0
            ),
            in_winning_sector=bool(matched),
        ))
    if bias == Bias.BULLISH:
        directional = [
            item for item in candidates if item.chg_pct > 0 and item.intraday_pct > 0
        ]
    else:
        directional = [
            item for item in candidates if item.chg_pct < 0 and item.intraday_pct < 0
        ]
    directional.sort(
        key=lambda item: (-item.chg_pct, item.tradingsymbol) if bias == Bias.BULLISH
        else (item.chg_pct, item.tradingsymbol)
    )
    top_pool = directional[:rules.screener.top_pool_size]
    eligible = [
        item for item in top_pool
        if item.anti_chase_ok
        and (item.in_winning_sector or not rules.screener.require_winning_sector)
    ]
    eligible.sort(
        key=lambda item: (-item.rel_strength, item.tradingsymbol) if bias == Bias.BULLISH
        else (item.rel_strength, item.tradingsymbol)
    )
    return ScreenerResult(
        winners,
        tuple(top_pool),
        tuple(eligible[:rules.screener.shortlist_size]),
    )
