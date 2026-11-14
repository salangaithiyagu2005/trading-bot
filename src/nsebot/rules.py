from __future__ import annotations

import sqlite3
import sysconfig
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

import yaml


class RulesValidationError(ValueError):
    """Raised when a rules document does not conform to the supported schema."""


@dataclass(frozen=True)
class RuleSection:
    """Immutable attribute-access wrapper for one validated rules section."""

    values: Mapping[str, Any]

    def __getattr__(self, name: str) -> Any:
        try:
            return self.values[name]
        except KeyError as exc:
            raise AttributeError(name) from exc


@dataclass(frozen=True)
class Rules:
    version: int
    timezone: str
    session: RuleSection
    calendar: RuleSection
    risk: RuleSection
    universe: RuleSection
    screener: RuleSection
    bias: RuleSection
    sets: RuleSection
    costs: RuleSection


_SOURCE_RULES_PATH = Path(__file__).resolve().parents[2] / "config" / "default_rules.yaml"
DEFAULT_RULES_PATH = (
    _SOURCE_RULES_PATH
    if _SOURCE_RULES_PATH.is_file()
    else Path(sysconfig.get_path("data")) / "share" / "nsebot" / "config" / "default_rules.yaml"
)

_DEFAULT_SCHEMA: dict[str, Any] = {
    "version": int,
    "timezone": str,
    "session": {
        "research_start": str,
        "no_trade_before": str,
        "no_new_trade_after": str,
        "flatten_by": str,
        "reconcile_at": str,
        "report_at": str,
        "market_close": str,
    },
    "calendar": {
        "skip_weekends": bool,
        "skip_exchange_holidays": bool,
        "skip_nifty_expiry_day": bool,
    },
    "risk": {
        "trading_mode": str,
        "max_trades_per_day": int,
        "max_open_positions": int,
        "reward_r_multiple": int,
        "premium_stop_pct": int,
        "min_premium_inr": int,
        "max_risk_inr_per_trade": int,
        "max_lots": int,
        "product": str,
        "order_style": str,
    },
    "universe": {
        "phase": str,
        "strike": str,
        "expiry": str,
        "min_dte_days": int,
        "max_bid_ask_pct_of_mid": (int, float),
        "min_oi": int,
        "min_oi_lots": int,
    },
    "screener": {
        "top_sectors": int,
        "top_pool_size": int,
        "shortlist_size": int,
        "require_winning_sector": bool,
        "min_stock_move_pct": (int, float),
        "max_stock_move_pct": (int, float),
    },
    "bias": {
        "bullish_if_score_gte": int,
        "bearish_if_score_lte": int,
        "band_pct": (int, float),
        "vwap_flat_pct": (int, float),
        "or_body_min_ratio": (int, float),
        "or_pos_strong": (int, float),
        "or_pos_mild": (int, float),
        "ema_sep_pct": (int, float),
        "breadth_strong": int,
        "breadth_mild": int,
        "breadth_bear_strong": int,
        "breadth_bear_mild": int,
        "breadth_min_names": int,
        "vix_veto_up_pct": int,
        "dead_range_atr_frac": (int, float),
    },
    "sets": {
        "active": list,
        "confirm_timeframe": str,
        "trigger_timeframe": str,
        "confirm_within_bars_5m": int,
        "volume_mult": (int, float),
        "volume_lookback": int,
    },
    "costs": {
        "brokerage_inr_per_executed_order": int,
        "stt_sell_premium_rate": (int, float),
        "txn_rate_on_premium": (int, float),
        "sebi_rate": (int, float),
        "stamp_buy_rate": (int, float),
        "gst_rate": (int, float),
    },
}

_SECTIONS = tuple(key for key, value in _DEFAULT_SCHEMA.items() if isinstance(value, dict))


def _type_matches(value: Any, expected: type | tuple[type, ...]) -> bool:
    if expected is int:
        return type(value) is int
    if expected is bool:
        return type(value) is bool
    if isinstance(expected, tuple):
        return type(value) in expected
    return isinstance(value, expected)


def _validate_tree(value: Any, schema: Any, path: str) -> None:
    if isinstance(schema, dict):
        if not isinstance(value, dict):
            raise RulesValidationError(f"{path} must be a mapping")
        missing = schema.keys() - value.keys()
        unknown = value.keys() - schema.keys()
        if missing:
            names = ", ".join(sorted(map(str, missing)))
            raise RulesValidationError(f"{path} missing required keys: {names}")
        if unknown:
            names = ", ".join(sorted(map(str, unknown)))
            raise RulesValidationError(f"{path} contains unknown keys: {names}")
        for key, child_schema in schema.items():
            _validate_tree(value[key], child_schema, f"{path}.{key}")
        return
    if schema is list:
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise RulesValidationError(f"{path} must be a list of strings")
        return
    if not _type_matches(value, schema):
        expected = (
            " or ".join(t.__name__ for t in schema)
            if isinstance(schema, tuple)
            else schema.__name__
        )
        raise RulesValidationError(f"{path} must be {expected}")


def _section(values: dict[str, Any]) -> RuleSection:
    normalized = {
        key: tuple(value) if isinstance(value, list) else (
            Decimal(str(value)) if isinstance(value, float) else value
        )
        for key, value in values.items()
    }
    return RuleSection(MappingProxyType(normalized))


def validate(doc: dict) -> Rules:
    if not isinstance(doc, dict):
        raise RulesValidationError("rules document must be a mapping")
    _validate_tree(doc, _DEFAULT_SCHEMA, "rules")
    allowed = {
        "risk.trading_mode": {"paper", "live"},
        "universe.phase": {"sector_fno_stock", "nifty_only"},
        "universe.strike": {"ATM"},
        "universe.expiry": {"monthly_min_dte", "next_after_today"},
        "risk.product": {"MIS"},
        "risk.order_style": {"LIMIT"},
        "sets.confirm_timeframe": {"5m"},
        "sets.trigger_timeframe": {"1m"},
    }
    for path, values in allowed.items():
        section_name, key = path.split(".")
        if doc[section_name][key] not in values:
            raise RulesValidationError(f"{path} must be one of {', '.join(sorted(values))}")
    if not doc["sets"]["active"] or not set(doc["sets"]["active"]) <= {"orb_vwap", "vwap_pullback"}:
        raise RulesValidationError("sets.active must contain only orb_vwap and/or vwap_pullback")

    return Rules(
        version=doc["version"],
        timezone=doc["timezone"],
        **{name: _section(doc[name]) for name in _SECTIONS},
    )


def _default_document() -> dict[str, Any]:
    try:
        with DEFAULT_RULES_PATH.open("r", encoding="utf-8") as stream:
            document = yaml.safe_load(stream)
    except OSError as exc:
        raise RuntimeError(f"Unable to read default rules at {DEFAULT_RULES_PATH}") from exc
    return document


def _document_from_row(row: sqlite3.Row | tuple) -> Rules:
    return validate(yaml.safe_load(row[0]))


def _ensure_default(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS rules_versions (
          id INTEGER PRIMARY KEY,
          created_at TEXT NOT NULL,
          yaml TEXT NOT NULL,
          note TEXT NOT NULL,
          status TEXT NOT NULL
        )
        """
    )
    if conn.execute("SELECT 1 FROM rules_versions LIMIT 1").fetchone() is None:
        document = _default_document()
        validate(document)
        conn.execute(
            "INSERT INTO rules_versions (created_at, yaml, note, status) VALUES (?, ?, ?, ?)",
            (_now_iso(), yaml.safe_dump(document, sort_keys=False), "Initial default rules", "scheduled"),
        )
        conn.commit()


def _now_iso() -> str:
    from datetime import datetime
    from zoneinfo import ZoneInfo

    return datetime.now(ZoneInfo("Asia/Kolkata")).isoformat()


def load_active(conn: sqlite3.Connection) -> Rules:
    _ensure_default(conn)
    row = conn.execute("SELECT yaml FROM rules_versions ORDER BY id DESC LIMIT 1").fetchone()
    if row is None:
        raise RuntimeError("Rules store is empty after initialization")
    return _document_from_row(row)


def load_scheduled(conn: sqlite3.Connection) -> Rules:
    _ensure_default(conn)
    row = conn.execute(
        "SELECT yaml FROM rules_versions WHERE status = 'scheduled' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if row is None:
        raise RuntimeError("No scheduled rules version exists")
    return _document_from_row(row)


def save_new_version(conn: sqlite3.Connection, doc: dict, note: str) -> int:
    validate(doc)
    _ensure_default(conn)
    with conn:
        conn.execute("UPDATE rules_versions SET status = 'retired' WHERE status = 'scheduled'")
        cursor = conn.execute(
            "INSERT INTO rules_versions (created_at, yaml, note, status) VALUES (?, ?, ?, 'scheduled')",
            (_now_iso(), yaml.safe_dump(doc, sort_keys=False), note),
        )
    return int(cursor.lastrowid)
