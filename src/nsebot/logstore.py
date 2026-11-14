from __future__ import annotations

import sqlite3
import json
import os
import re
import sqlite3
import tempfile
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import yaml

from nsebot.clock import now_ist


SCHEMA = """
CREATE TABLE IF NOT EXISTS rules_versions (
  id INTEGER PRIMARY KEY,
  created_at TEXT NOT NULL,
  yaml TEXT NOT NULL,
  note TEXT NOT NULL,
  status TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS app_settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
  day TEXT PRIMARY KEY,
  rules_version INTEGER NOT NULL,
  state TEXT NOT NULL,
  mode TEXT NOT NULL,
  user_armed INTEGER NOT NULL DEFAULT 0,
  block_reason TEXT,
  bias TEXT,
  score INTEGER,
  veto TEXT,
  winning_sectors TEXT,
  shortlist_json TEXT,
  top20_json TEXT,
  started_at TEXT,
  ended_at TEXT
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY,
  day TEXT NOT NULL,
  ts TEXT NOT NULL,
  state_from TEXT NOT NULL,
  state_to TEXT NOT NULL,
  reason TEXT NOT NULL,
  reason_text TEXT NOT NULL,
  snapshot_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS orders (
  id INTEGER PRIMARY KEY,
  day TEXT NOT NULL,
  tag TEXT NOT NULL,
  role TEXT NOT NULL,
  mode TEXT NOT NULL,
  broker_order_id TEXT,
  symbol TEXT NOT NULL,
  side TEXT NOT NULL,
  qty INTEGER NOT NULL,
  limit_price TEXT,
  trigger_price TEXT,
  status TEXT NOT NULL,
  filled_qty INTEGER NOT NULL DEFAULT 0,
  avg_price TEXT
);
CREATE TABLE IF NOT EXISTS trades (
  id INTEGER PRIMARY KEY,
  day TEXT NOT NULL UNIQUE,
  mode TEXT NOT NULL,
  underlying TEXT NOT NULL,
  sector TEXT,
  symbol TEXT NOT NULL,
  right TEXT NOT NULL,
  strike TEXT NOT NULL,
  expiry TEXT NOT NULL,
  qty INTEGER NOT NULL,
  entry_price TEXT NOT NULL,
  exit_price TEXT,
  exit_reason TEXT,
  stop_trigger TEXT NOT NULL,
  target TEXT NOT NULL,
  mfe TEXT,
  mae TEXT,
  gross_pnl TEXT,
  costs TEXT,
  net_pnl TEXT,
  rules_version INTEGER NOT NULL,
  set_id TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reviews (
  id INTEGER PRIMARY KEY,
  day TEXT NOT NULL UNIQUE,
  tags TEXT NOT NULL,
  note TEXT NOT NULL,
  source TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS commands (
  id INTEGER PRIMARY KEY,
  ts TEXT NOT NULL,
  name TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  status TEXT NOT NULL,
  result_text TEXT
);
CREATE TABLE IF NOT EXISTS engine_status (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  heartbeat_at TEXT NOT NULL,
  pid INTEGER,
  state TEXT NOT NULL,
  ws_connected INTEGER NOT NULL DEFAULT 0,
  ws_subscribed_count INTEGER NOT NULL DEFAULT 0,
  last_tick_at TEXT,
  tick_count INTEGER NOT NULL DEFAULT 0,
  nifty_spot_last TEXT,
  nifty_fut_last TEXT,
  last_1m_bar_ts TEXT,
  last_5m_bar_ts TEXT,
  or_complete INTEGER NOT NULL DEFAULT 0,
  warmup_status TEXT NOT NULL DEFAULT 'pending',
  token_verified_at TEXT,
  token_verify_status TEXT NOT NULL DEFAULT 'unverified',
  token_verify_error TEXT,
  available_margin_inr TEXT
);
"""


def initialize(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def upsert_engine_status(conn: sqlite3.Connection, status: dict[str, Any]) -> None:
    allowed = {
        "heartbeat_at", "pid", "state", "ws_connected", "ws_subscribed_count",
        "last_tick_at", "tick_count", "nifty_spot_last", "nifty_fut_last",
        "last_1m_bar_ts", "last_5m_bar_ts", "or_complete", "warmup_status",
        "token_verified_at", "token_verify_status", "token_verify_error",
        "available_margin_inr",
    }
    unknown = set(status) - allowed
    required = {"heartbeat_at", "state"}
    if unknown or not required <= status.keys():
        raise ValueError(f"invalid engine status fields: {sorted(unknown | (required - status.keys()))}")
    columns = ["id", *status.keys()]
    values = [1, *status.values()]
    updates = ", ".join(f"{column}=excluded.{column}" for column in status if column != "id")
    conn.execute(
        f"INSERT INTO engine_status ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)}) "
        f"ON CONFLICT(id) DO UPDATE SET {updates}",
        values,
    )
    conn.commit()


def load_engine_status(conn: sqlite3.Connection) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM engine_status WHERE id=1").fetchone()
    return dict(row) if row is not None else None


def connect(data_dir: Path) -> sqlite3.Connection:
    data_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(data_dir / "algo.db")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    initialize(conn)
    return conn


def get_app_settings(conn: sqlite3.Connection) -> dict[str, str]:
    return {row["key"]: row["value"] for row in conn.execute(
        "SELECT key, value FROM app_settings"
    )}


def set_app_settings(conn: sqlite3.Connection, updates: dict[str, str]) -> None:
    allowed = {"kite_redirect_url", "ui_poll_interval_ms"}
    unknown = set(updates) - allowed
    if unknown:
        raise ValueError(f"Unknown app settings: {', '.join(sorted(unknown))}")
    for key, value in updates.items():
        if not isinstance(value, str):
            raise ValueError(f"{key} must be a string")
        if key == "ui_poll_interval_ms":
            try:
                interval = int(value)
            except ValueError as exc:
                raise ValueError("ui_poll_interval_ms must be an integer") from exc
            if not 250 <= interval <= 60000:
                raise ValueError("ui_poll_interval_ms must be between 250 and 60000")
        if key == "kite_redirect_url" and value:
            if not value.startswith("https://") and not value.startswith("http://127.0.0.1"):
                raise ValueError("kite_redirect_url must use HTTPS (localhost is allowed for development)")
        conn.execute(
            """
            INSERT INTO app_settings(key, value, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
            """,
            (key, value, now_ist().isoformat()),
        )
    conn.commit()


def enqueue_command(
    conn: sqlite3.Connection,
    name: str,
    payload: dict[str, Any] | None = None,
) -> int:
    allowed = {"ARM", "KILL", "RELOAD_TOKEN", "RELOAD_CONFIG", "VERIFY_TOKEN"}
    if name not in allowed:
        raise ValueError(f"Unsupported engine command: {name}")
    cursor = conn.execute(
        "INSERT INTO commands(ts, name, payload_json, status) VALUES (?, ?, ?, 'pending')",
        (now_ist().isoformat(), name, json.dumps(payload or {}, separators=(",", ":"))),
    )
    conn.commit()
    return int(cursor.lastrowid)


ALLOWED_CREDENTIAL_KEYS = {"KITE_API_KEY", "KITE_API_SECRET", "XAI_API_KEY", "ALERT_WEBHOOK"}
DATA_FILES = {"nse_holidays.txt", "nifty50.txt", "sector_fno_map.yaml"}


def load_credentials(data_dir: Path) -> dict[str, str]:
    path = data_dir / "credentials.json"
    stored = _read_stored_credentials(path)
    return {
        key: stored.get(key) or os.environ.get(key, "")
        for key in ALLOWED_CREDENTIAL_KEYS
        if stored.get(key) or os.environ.get(key)
    }


def _read_stored_credentials(path: Path) -> dict[str, str]:
    if path.exists():
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"Unable to read credentials file: {path}") from exc
        if not isinstance(document, dict) or any(
            key not in ALLOWED_CREDENTIAL_KEYS or not isinstance(value, str)
            for key, value in document.items()
        ):
            raise ValueError("credentials.json contains invalid keys or values")
        return document
    return {}


def _atomic_write(path: Path, content: bytes, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        if mode is not None:
            os.chmod(temporary, mode)
        os.replace(temporary, path)
        if mode is not None:
            os.chmod(path, mode)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def save_credentials(data_dir: Path, updates: dict[str, str]) -> bool:
    unknown = set(updates) - ALLOWED_CREDENTIAL_KEYS
    if unknown:
        raise ValueError(f"Disallowed credential keys: {', '.join(sorted(unknown))}")
    current = _read_stored_credentials(data_dir / "credentials.json")
    if any(not isinstance(value, str) for value in updates.values()):
        raise ValueError("credential values must be strings")
    merged = dict(current)
    for key, value in updates.items():
        if value:
            merged[key] = value
    key_changed = bool(updates.get("KITE_API_KEY")) and (
        updates["KITE_API_KEY"] != (
            current.get("KITE_API_KEY") or os.environ.get("KITE_API_KEY", "")
        )
    )
    if updates:
        _atomic_write(
            data_dir / "credentials.json",
            (json.dumps(merged, sort_keys=True, indent=2) + "\n").encode("utf-8"),
            0o600,
        )
    return key_changed


def mask_secret(value: str | None) -> str:
    if not value:
        return ""
    if len(value) <= 4:
        return "*" * len(value)
    return f"{value[:2]}{'*' * min(12, len(value) - 4)}{value[-2:]}"


def seed_data_files(repo_data_dir: Path, data_dir: Path) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    for name in DATA_FILES:
        destination = data_dir / name
        source = repo_data_dir / name
        if destination.exists() or not source.is_file():
            continue
        _atomic_write(destination, source.read_bytes())


def validate_and_save_data_file(data_dir: Path, name: str, content: str) -> None:
    if name not in DATA_FILES:
        raise ValueError(f"Unsupported data file: {name}")
    if not isinstance(content, str):
        raise ValueError("content must be a string")
    if name == "nse_holidays.txt":
        from datetime import date

        dates = []
        for line_number, raw in enumerate(content.splitlines(), start=1):
            value = raw.strip()
            if not value or value.startswith("#"):
                continue
            try:
                parsed = date.fromisoformat(value)
            except ValueError as exc:
                raise ValueError(f"Invalid holiday date on line {line_number}") from exc
            if parsed.isoformat() != value:
                raise ValueError(f"Holiday date must be YYYY-MM-DD on line {line_number}")
            dates.append(parsed)
        if not dates:
            raise ValueError("holiday file must contain at least one date")
    elif name == "nifty50.txt":
        symbols = [
            line.strip() for line in content.splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        if any(not re.fullmatch(r"[A-Z0-9&-]+", symbol) for symbol in symbols):
            raise ValueError("Nifty 50 file contains an invalid symbol")
        if len(symbols) != 50 or len(set(symbols)) != 50:
            raise ValueError("Nifty 50 file must contain exactly 50 unique symbols")
    else:
        try:
            document = yaml.safe_load(content)
        except yaml.YAMLError as exc:
            raise ValueError("sector map must be valid YAML") from exc
        sectors = document.get("sectors") if isinstance(document, dict) else None
        if not isinstance(sectors, dict) or not sectors:
            raise ValueError("sector map must contain a non-empty sectors mapping")
        for sector, symbols in sectors.items():
            if not isinstance(sector, str) or not sector.strip() or not isinstance(symbols, list):
                raise ValueError("sector map entries must map sector names to symbol lists")
            if not symbols or any(not isinstance(symbol, str) or not symbol.strip() for symbol in symbols):
                raise ValueError(f"sector {sector!r} must have non-empty symbol strings")
    _atomic_write(data_dir / name, content.encode("utf-8"))


_REASON_TEMPLATES = {
    "bias_bullish": "Bias {score} BULLISH. prev {prev} vwap {vwap} or {opening} ema {ema} breadth {breadth} bank {bank}.",
    "bias_bearish": "Bias {score} BEARISH. prev {prev} vwap {vwap} or {opening} ema {ema} breadth {breadth} bank {bank}.",
    "bias_sideways": "Bias {score} SIDEWAYS. prev {prev} vwap {vwap} or {opening} ema {ema} breadth {breadth} bank {bank}.",
    "sector_ranked": "Sectors ranked ({bias}): {sectors}. Shortlist: {shortlist}.",
    "no_eligible_stock": "No F&O stock passed sector and anti-chase filters.",
    "expiry_week_skip": "Skipped {symbol} expiry under min_dte_days.",
    "armed": "Set {set_id} armed on 1m close {close}. No order.",
    "confirm_failed": "Set {set_id} arm dropped. {detail}",
    "order_submitted": "Limit buy {symbol} qty {qty} at {price} tag {tag}.",
    "order_filled": "Filled {symbol} qty {qty} at {price}. R {r}. Stop {stop}. Target {target}.",
    "stop_hit": "Stop filled at {price}.",
    "target_hit": "Target filled at {price}.",
    "structure_exit": "1m close {close} crossed VWAP {vwap}. Exit.",
    "time_flatten": "Clock flatten. Exit at {price}.",
    "window_closed": "No confirm before no_new_trade_after. Flat.",
    "vix_unstable": "VIX up {vix_chg}% and price inside opening range. No trade.",
    "dead_range": "Opening range {or_range} < {frac} * ATR15 {atr}. No trade.",
    "no_token": "No access token. Session not armed.",
    "kill_switch": "Kill switch. Working orders cancelled. Position flattened.",
    "liquidity_reject": "Liquidity reject on {symbol}. Spread or OI failed.",
    "premium_too_small": "Premium {mid} below min_premium_inr.",
    "risk_cap": "Risk {risk_inr} exceeds max_risk_inr_per_trade.",
    "not_filled": "Entry not filled within timeout. Cancel confirmed.",
    "expiry_day": "Nifty expiry day. Session blocked.",
    "breadth_incomplete": "Breadth names resolved {n} below minimum.",
    "health_fail": "Health check failed: {detail}.",
    "warmup_fail": "Warmup failed: {detail}.",
    "reconcile_mismatch": "Reconcile mismatch. Local {local_qty} broker {broker_qty}.",
    "blind": "Feed stale. Flatten triggered.",
    "order_rejected": "Order rejected: {detail}.",
    "session_done": "Session done. Reason {detail}.",
    "holiday": "Non-trading day.",
    "stop_placed": "Stop SL placed trigger {trigger} limit {price}.",
    "target_placed": "Target limit placed at {price}.",
    "review_saved": "Review saved. Tags {tags}.",
}


def render(reason: str, snapshot: dict[str, Any]) -> str:
    try:
        template = _REASON_TEMPLATES[reason]
    except KeyError as exc:
        raise ValueError(f"No event template for reason: {reason}") from exc
    return template.format_map(snapshot)


def _decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"Invalid monetary value in trades table: {value!r}") from exc


def trades_summary(
    conn: sqlite3.Connection,
    mode: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    if mode not in (None, "all", "paper", "live"):
        raise ValueError("mode must be paper, live, all, or None")
    if type(limit) is not int or limit < 1:
        raise ValueError("limit must be a positive integer")

    where = "" if mode in (None, "all") else "WHERE mode = ?"
    params: tuple[Any, ...] = () if not where else (mode,)
    cursor = conn.execute(
        f"SELECT * FROM trades {where} ORDER BY day DESC LIMIT ?",
        (*params, limit),
    )
    columns = [column[0] for column in cursor.description]
    rows = cursor.fetchall()
    closed_rows = conn.execute(
        f"""
        SELECT entry_price, stop_trigger, qty, gross_pnl, costs, net_pnl
          FROM trades {where} {"AND" if where else "WHERE"} net_pnl IS NOT NULL
        """,
        params,
    ).fetchall()
    closed_count = len(closed_rows)
    wins = 0
    losses = 0
    gross_total = Decimal("0")
    costs_total = Decimal("0")
    net_total = Decimal("0")
    r_total = Decimal("0")
    r_count = 0
    for row in closed_rows:
        gross_total += _decimal(row[3] or 0)
        costs_total += _decimal(row[4] or 0)
        net_pnl = _decimal(row[5])
        net_total += net_pnl
        wins += net_pnl > 0
        losses += net_pnl < 0
        risk = abs(_decimal(row[0]) - _decimal(row[1])) * int(row[2])
        if risk:
            r_total += net_pnl / risk
            r_count += 1

    return {
        "summary": {
            "total_trades": closed_count,
            "wins": wins,
            "losses": losses,
            "win_rate_pct": round(wins * 100 / closed_count, 2) if closed_count else 0.0,
            "gross_pnl": str(gross_total),
            "costs": str(costs_total),
            "net_pnl": str(net_total),
            "avg_r": str(r_total / r_count) if r_count else "0",
        },
        "trades": [dict(zip(columns, row)) for row in rows],
    }
