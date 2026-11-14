from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path

import yaml


@dataclass(frozen=True)
class Instrument:
    token: int
    tradingsymbol: str
    exchange: str
    segment: str
    name: str
    expiry: date | None
    strike: Decimal | None
    tick_size: Decimal
    lot_size: int
    instrument_type: str


class InstrumentError(ValueError):
    pass


def load_dump(csv_bytes: bytes) -> list[Instrument]:
    try:
        text = csv_bytes.decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(text))
        required = {
            "instrument_token", "tradingsymbol", "exchange", "segment", "name",
            "expiry", "strike", "tick_size", "lot_size", "instrument_type",
        }
        if reader.fieldnames is None or not required <= set(reader.fieldnames):
            raise InstrumentError("instrument dump is missing required columns")
        instruments: list[Instrument] = []
        for line, row in enumerate(reader, start=2):
            try:
                instruments.append(
                    Instrument(
                        token=int(row["instrument_token"]),
                        tradingsymbol=row["tradingsymbol"],
                        exchange=row["exchange"],
                        segment=row["segment"],
                        name=row["name"] or row["tradingsymbol"],
                        expiry=date.fromisoformat(row["expiry"]) if row["expiry"] else None,
                        strike=Decimal(row["strike"]) if row["strike"] else None,
                        tick_size=Decimal(row["tick_size"]),
                        lot_size=int(row["lot_size"]),
                        instrument_type=row["instrument_type"],
                    )
                )
            except (ValueError, TypeError) as exc:
                raise InstrumentError(f"invalid instrument row {line}") from exc
        return instruments
    except UnicodeDecodeError as exc:
        raise InstrumentError("instrument dump is not valid UTF-8") from exc


def require_index(rows: list[Instrument], tradingsymbol: str) -> Instrument:
    for instrument in rows:
        if (
            instrument.exchange == "NSE"
            and instrument.segment == "INDICES"
            and instrument.tradingsymbol == tradingsymbol
        ):
            return instrument
    raise InstrumentError(f"missing NSE index: {tradingsymbol}")


def resolve_near_month_fut(rows: list[Instrument], name: str, day: date) -> Instrument:
    candidates = [
        row for row in rows
        if row.exchange == "NFO" and row.instrument_type == "FUT"
        and row.name == name and row.expiry is not None and row.expiry >= day
    ]
    if not candidates:
        raise InstrumentError(f"no unexpired {name} futures contract")
    return min(candidates, key=lambda row: row.expiry)


def nifty_options(rows: list[Instrument]) -> list[Instrument]:
    return [
        row for row in rows
        if row.exchange == "NFO" and row.name in {
            "NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50"
        } and row.instrument_type in {"CE", "PE"}
    ]


def stock_options(rows: list[Instrument], underlying: str) -> list[Instrument]:
    return [
        row for row in rows
        if row.exchange == "NFO" and row.name == underlying
        and row.instrument_type in {"CE", "PE"}
    ]


def fno_stock_names(rows: list[Instrument]) -> set[str]:
    index_names = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50"}
    return {
        row.name for row in rows
        if row.exchange == "NFO" and row.instrument_type in {"CE", "PE"}
        and row.name not in index_names
    }


def resolve_symbols(rows: list[Instrument], symbols: list[str]) -> dict[str, Instrument]:
    wanted = set(symbols)
    resolved = {
        row.tradingsymbol: row for row in rows
        if row.exchange == "NSE" and row.instrument_type == "EQ"
        and row.tradingsymbol in wanted
    }
    return resolved


def load_session_instruments(data_dir: Path, day: date, gw) -> list[Instrument]:
    data_dir.mkdir(parents=True, exist_ok=True)
    cache = data_dir / f"instruments-{day.isoformat()}.csv"
    if cache.exists():
        return load_dump(cache.read_bytes())
    csv_bytes = gw.instruments_csv(None)
    parsed = load_dump(csv_bytes)
    temporary = cache.with_suffix(".csv.tmp")
    temporary.write_bytes(csv_bytes)
    temporary.replace(cache)
    return parsed


def load_sector_map(
    path: Path,
    rows: list[Instrument],
) -> dict[str, tuple[Instrument, list[Instrument]]]:
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise InstrumentError(f"Unable to load sector map: {path}") from exc
    sectors = document.get("sectors") if isinstance(document, dict) else None
    if not isinstance(sectors, dict):
        raise InstrumentError("sector map must contain a sectors mapping")
    fno_names = fno_stock_names(rows)
    cash = resolve_symbols(rows, [
        symbol
        for symbols in sectors.values()
        if isinstance(symbols, list)
        for symbol in symbols
        if isinstance(symbol, str)
    ])
    result: dict[str, tuple[Instrument, list[Instrument]]] = {}
    for sector_name, symbols in sectors.items():
        if not isinstance(sector_name, str) or not isinstance(symbols, list):
            raise InstrumentError("invalid sector map entry")
        try:
            index = require_index(rows, sector_name)
        except InstrumentError:
            continue
        stocks = [
            cash[symbol] for symbol in symbols
            if isinstance(symbol, str) and symbol in fno_names and symbol in cash
        ]
        result[sector_name] = (index, stocks)
    return result
