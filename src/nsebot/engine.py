from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time as time_module
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Callable

import yaml

from nsebot import logstore
from nsebot.alerts import notify
from nsebot.bias import BiasObs, BiasResult, compute_bias
from nsebot.calendar_mod import previous_trading_day, session_allowed
from nsebot.candles import CandleBuilder, discard_open_historical
from nsebot.chain import (
    NoExpiryError, is_expiry_day, liquidity_ok, select_atm, select_expiry,
)
from nsebot.clock import IST, now_ist, parse_hhmm
from nsebot.execution import Execution
from nsebot.indicators import atr_wilder, ema, rsi_wilder, vwap
from nsebot.instruments import (
    Instrument,
    InstrumentError,
    fno_stock_names,
    load_sector_map,
    load_session_instruments,
    nifty_options,
    require_index,
    resolve_near_month_fut,
    resolve_symbols,
    stock_options,
)
from nsebot.kite_gateway import Gateway
from nsebot.paper import PaperBroker, PaperFill
from nsebot.rules import Rules, load_scheduled, validate
from nsebot.risk import Bracket, build_bracket, costs, entry_limit, round_down
from nsebot.sets import SetContext, SetEval, evaluate
from nsebot.screener import PriceSnap, ScreenerResult, screen_stocks
from nsebot.types import Bar, Bias, Reason, Side, State, Tick

logger = logging.getLogger(__name__)

_ALLOWED: dict[State, set[State]] = {
    State.IDLE: {State.BLOCKED, State.RESEARCH, State.HALTED},
    State.BLOCKED: {State.IDLE, State.HALTED},
    State.RESEARCH: {State.WATCHING, State.BLOCKED, State.DONE, State.HALTED},
    State.WATCHING: {State.ARMED, State.DONE, State.BLOCKED, State.HALTED},
    State.ARMED: {State.WATCHING, State.ENTRY_WORKING, State.DONE, State.HALTED},
    State.ENTRY_WORKING: {State.MANAGING, State.WATCHING, State.DONE, State.HALTED},
    State.MANAGING: {State.DONE, State.HALTED},
    State.DONE: {State.IDLE},
    State.HALTED: {State.DONE},
}


class Engine:
    def __init__(
        self,
        data_dir: Path | None = None,
        repo_data_dir: Path | None = None,
        gateway_factory: Callable = Gateway,
        clock: Callable[[], datetime] = now_ist,
        sleep: Callable[[float], None] = time_module.sleep,
    ):
        self.data_dir = (data_dir or Path(os.environ.get("DATA_DIR", "./var"))).resolve()
        self.repo_data_dir = (
            repo_data_dir
            or Path(os.environ.get("REPO_DATA_DIR", Path(__file__).resolve().parents[2] / "data"))
        ).resolve()
        self.gateway_factory = gateway_factory
        self.clock = clock
        self.sleep = sleep
        self.conn = logstore.connect(self.data_dir)
        logstore.seed_data_files(self.repo_data_dir, self.data_dir)
        self.active_day = self.clock().date()
        self.rules = load_scheduled(self.conn)
        self.rules_version = self._scheduled_version()
        self.rules_latched = self.clock().time() >= parse_hhmm("09:00")
        self.gateway = None
        self.execution: Execution | None = None
        self.api_key: str | None = None
        self.instruments: list[Instrument] = []
        self.indices: dict[str, Instrument] = {}
        self.futures: dict[str, Instrument] = {}
        self.instruments_by_token: dict[int, Instrument] = {}
        self.sector_map: dict = {}
        self.breadth_symbols: dict[str, Instrument] = {}
        self.builders: dict[tuple[int, int], CandleBuilder] = {}
        self.history: dict[tuple[int, str], list[Bar]] = {}
        self.open_0915: dict[int, Decimal] = {}
        self.prev_closes: dict[int, Decimal] = {}
        self.latest_prices: dict[int, Decimal] = {}
        self.volume_today: dict[int, int] = {}
        self.warmed_tokens: set[int] = set()
        self.bias_result: BiasResult | None = None
        self.screener_result: ScreenerResult | None = None
        self._last_bias_bar_end: datetime | None = None
        self._last_screen_bar_end: datetime | None = None
        self._set_state: dict[tuple[str, str], dict] = {}
        self.paper_broker = PaperBroker()
        self.active_stock: Instrument | None = None
        self.active_option: Instrument | None = None
        self.active_sector: str | None = None
        self.active_set_id: str | None = None
        self.active_bracket: Bracket | None = None
        self.active_option_last: Decimal | None = None
        self.active_option_bid: Decimal | None = None
        self.active_option_last_seen: datetime | None = None
        self._last_option_quote_monotonic = 0.0
        self.active_mfe = Decimal("0")
        self.active_mae = Decimal("0")
        self._paper_stop_triggered_at: datetime | None = None
        self.warmup_status = "pending"
        self.or_complete = False
        self.ws_connected = False
        self.ws_subscribed_count = 0
        self.last_tick_at: datetime | None = None
        self.tick_count = 0
        self.last_1m_bar_ts: datetime | None = None
        self.last_5m_bar_ts: datetime | None = None
        self.nifty_spot_last: Decimal | None = None
        self.nifty_fut_last: Decimal | None = None
        self.token_verified_at: str | None = None
        self.token_verify_status = "unverified"
        self.token_verify_error: str | None = None
        self.available_margin_inr: Decimal | None = None
        self.expiry_day = False
        self.user_armed = False
        self._subscription_tokens: list[int] = []
        self._tick_queue: asyncio.Queue[list[Tick]] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ws_connect_event = threading.Event()
        self._last_heartbeat = 0.0
        self._startup_verified = False
        self.health_done = False
        self._closed = False
        self._load_or_create_session()
        self.calendar_allowed = self._calendar_ok()
        if not self.calendar_allowed:
            self.health_done = True
        if self.rules.risk.trading_mode == "live":
            self._transition(
                State.BLOCKED,
                Reason.HEALTH_FAIL,
                {"detail": (
                    "LIVE_TRADING env is false"
                    if not self._live_allowed()
                    else "live order lifecycle is not implemented"
                )},
            )
        elif self.calendar_allowed and not self._valid_token():
            self._transition(State.BLOCKED, Reason.NO_TOKEN, {})

    def _live_allowed(self) -> bool:
        return os.environ.get("LIVE_TRADING", "false").strip().lower() == "true"

    def _scheduled_version(self) -> int:
        row = self.conn.execute(
            "SELECT id FROM rules_versions WHERE status='scheduled' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if row is None:
            raise RuntimeError("no scheduled rules version found")
        return int(row["id"])

    def _load_or_create_session(self) -> None:
        day = self.active_day.isoformat()
        row = self.conn.execute("SELECT * FROM sessions WHERE day=?", (day,)).fetchone()
        if row is None:
            self.conn.execute(
                """
                INSERT INTO sessions(day, rules_version, state, mode, started_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    day, self.rules_version, State.IDLE.value,
                    self.rules.risk.trading_mode, self.clock().isoformat(),
                ),
            )
            self.conn.commit()
            self.state = State.IDLE
            self.user_armed = False
        else:
            try:
                self.state = State(row["state"])
            except ValueError as exc:
                raise RuntimeError(f"unknown persisted engine state: {row['state']}") from exc
            self.user_armed = bool(row["user_armed"])
            if self.rules_latched:
                version = self.conn.execute(
                    "SELECT yaml FROM rules_versions WHERE id=?", (row["rules_version"],)
                ).fetchone()
                if version is not None:
                    self.rules = validate(yaml.safe_load(version["yaml"]))
                    self.rules_version = int(row["rules_version"])

    def _today(self) -> str:
        return self.active_day.isoformat()

    def _calendar_ok(self) -> bool:
        holiday_path = self.data_dir / "nse_holidays.txt"
        try:
            allowed, reason = session_allowed(self.active_day, self.rules, holiday_path)
        except ValueError as exc:
            self._transition(State.BLOCKED, Reason.HEALTH_FAIL, {"detail": str(exc)})
            return False
        if not allowed:
            self._transition(State.BLOCKED, reason or Reason.HOLIDAY, {})
            return False
        return True

    def _valid_token_data(self) -> dict | None:
        path = self.data_dir / "token.json"
        if not path.exists():
            return None
        try:
            token = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(token, dict):
                raise ValueError("token.json must contain a JSON object")
            access_token = token.get("access_token")
            if not isinstance(access_token, str) or not access_token.strip():
                raise ValueError("access_token must be a non-empty string")
            created = datetime.fromisoformat(token["created_at"])
            if created.tzinfo is None:
                raise ValueError("created_at must include a timezone")
            created = created.astimezone(IST)
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("token.json is invalid") from exc
        expiry = datetime.combine(self.clock().date(), time(6), tzinfo=IST)
        if created < expiry:
            return None
        return token

    def _valid_token(self) -> bool:
        try:
            return self._valid_token_data() is not None
        except RuntimeError as exc:
            self._transition(State.BLOCKED, Reason.HEALTH_FAIL, {"detail": str(exc)})
            return False

    def _transition(self, target: State, reason: Reason, snapshot: dict) -> None:
        if target == self.state:
            return
        if target not in _ALLOWED[self.state]:
            raise RuntimeError(f"illegal engine state transition {self.state.value} -> {target.value}")
        details = dict(snapshot)
        reason_text = logstore.render(reason.value, details)
        previous = self.state
        self.state = target
        self.conn.execute(
            "UPDATE sessions SET state=?, block_reason=? WHERE day=?",
            (
                target.value,
                reason.value if target == State.BLOCKED else None,
                self._today(),
            ),
        )
        self.conn.execute(
            """
            INSERT INTO events(day, ts, state_from, state_to, reason, reason_text, snapshot_json)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                self._today(), self.clock().isoformat(), previous.value, target.value,
                reason.value, reason_text, json.dumps(details, default=str),
            ),
        )
        self.conn.commit()
        self._write_status(force=True)
        if target in {State.BLOCKED, State.HALTED}:
            self._notify(reason_text)

    def _notify(self, text: str) -> None:
        try:
            notify(text, data_dir=self.data_dir)
        except Exception:
            logger.exception("Could not record or send operator alert")

    def _write_status(self, force: bool = False) -> None:
        monotonic_now = time_module.monotonic()
        if not force and monotonic_now - self._last_heartbeat < 2:
            return
        logstore.upsert_engine_status(self.conn, {
            "heartbeat_at": self.clock().isoformat(),
            "pid": os.getpid(),
            "state": self.state.value,
            "ws_connected": int(self.ws_connected),
            "ws_subscribed_count": self.ws_subscribed_count,
            "last_tick_at": self.last_tick_at.isoformat() if self.last_tick_at else None,
            "tick_count": self.tick_count,
            "nifty_spot_last": str(self.nifty_spot_last) if self.nifty_spot_last is not None else None,
            "nifty_fut_last": str(self.nifty_fut_last) if self.nifty_fut_last is not None else None,
            "last_1m_bar_ts": self.last_1m_bar_ts.isoformat() if self.last_1m_bar_ts else None,
            "last_5m_bar_ts": self.last_5m_bar_ts.isoformat() if self.last_5m_bar_ts else None,
            "or_complete": int(self.or_complete),
            "warmup_status": self.warmup_status,
            "token_verified_at": self.token_verified_at,
            "token_verify_status": self.token_verify_status,
            "token_verify_error": self.token_verify_error,
            "available_margin_inr": str(self.available_margin_inr)
            if self.available_margin_inr is not None else None,
        })
        self._last_heartbeat = monotonic_now

    def _latch_rules(self) -> None:
        self.rules = load_scheduled(self.conn)
        self.rules_version = self._scheduled_version()
        self.rules_latched = True
        self.conn.execute(
            "UPDATE sessions SET rules_version=?, mode=? WHERE day=?",
            (self.rules_version, self.rules.risk.trading_mode, self._today()),
        )
        self.conn.commit()
        if self.rules.risk.trading_mode == "live":
            self._transition(
                State.BLOCKED,
                Reason.HEALTH_FAIL,
                {"detail": (
                    "LIVE_TRADING env is false"
                    if not self._live_allowed()
                    else "live order lifecycle is not implemented"
                )},
            )

    def _on_ticks(self, _ws, ticks: list[Tick]) -> None:
        if self._loop is None or self._tick_queue is None:
            return
        self._loop.call_soon_threadsafe(self._tick_queue.put_nowait, ticks)

    def _on_connect(self, _ws, _response) -> None:
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._set_ws_connected, True)

    def _on_close(self, _ws, _code, _reason) -> None:
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._set_ws_connected, False)

    def _on_error(self, _ws, _code, reason) -> None:
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._set_ws_connected, False)
            self._loop.call_soon_threadsafe(logger.error, "Kite WebSocket error: %s", reason)

    def _set_ws_connected(self, connected: bool) -> None:
        self.ws_connected = connected
        if connected:
            self._ws_connect_event.set()
        self._write_status(force=True)

    def _process_ticks(self, ticks: list[Tick]) -> None:
        closed: list[Bar] = []
        for tick in ticks:
            self.tick_count += 1
            self.last_tick_at = tick.ts
            self.latest_prices[tick.token] = tick.last
            self._manage_paper_tick(tick)
            if tick.prev_close is not None:
                self.prev_closes[tick.token] = tick.prev_close
            self.volume_today[tick.token] = tick.volume_traded
            if tick.ts.time() >= parse_hhmm("09:15") and tick.day_open is not None:
                self.open_0915.setdefault(tick.token, tick.day_open)
            elif tick.ts.time() >= parse_hhmm("09:15"):
                self.open_0915.setdefault(tick.token, tick.last)
            if self.indices.get("NIFTY 50") and tick.token == self.indices["NIFTY 50"].token:
                self.nifty_spot_last = tick.last
            if self.futures.get("NIFTY") and tick.token == self.futures["NIFTY"].token:
                self.nifty_fut_last = tick.last
            for timeframe in (1, 5):
                builder = self.builders.get((tick.token, timeframe))
                if builder is not None:
                    closed.extend(builder.on_tick(tick))
        closed.sort(key=lambda bar: (0 if bar.timeframe == "5m" else 1, bar.start, bar.token))
        for bar in closed:
            key = (bar.token, bar.timeframe)
            self.history.setdefault(key, []).append(bar)
            if bar.timeframe == "5m":
                self.last_5m_bar_ts = bar.end
            else:
                self.last_1m_bar_ts = bar.end
        self._after_closed_bars(closed)
        self._update_or_state()
        self._write_status()

    def _after_closed_bars(self, bars: list[Bar]) -> None:
        five_minute = [bar for bar in bars if bar.timeframe == "5m"]
        if five_minute:
            self._refresh_strategy_snapshot(max(bar.end for bar in five_minute))
        shortlist_tokens = {
            item.token for item in self.screener_result.shortlist
        } if self.screener_result else set()
        for bar in bars:
            if (
                self.state == State.MANAGING
                and self.active_stock is not None
                and bar.token == self.active_stock.token
                and bar.timeframe == "1m"
            ):
                stock_vwap = vwap(self._today_bars(self.active_stock, "1m"))
                if stock_vwap is None or self.active_option_last is None:
                    continue
                position = self.paper_broker.position
                if position is None or not position.is_open:
                    continue
                should_exit = (
                    self.active_option is not None
                    and self.active_option.instrument_type == "CE"
                    and bar.close < stock_vwap
                ) or (
                    self.active_option is not None
                    and self.active_option.instrument_type == "PE"
                    and bar.close > stock_vwap
                )
                if should_exit:
                    fill = self.paper_broker.exit_at_marketable_limit(
                        self.active_option_last, self.active_option_bid,
                        "structure_exit", bar.end,
                    )
                    if fill is not None:
                        self._close_paper_trade(fill, Reason.STRUCTURE_EXIT)
            elif bar.token in shortlist_tokens:
                self._evaluate_stock_bar(bar)

    def _update_or_state(self) -> None:
        nifty_fut = self.futures.get("NIFTY")
        if nifty_fut:
            opening_bars = [
                bar for bar in self.history.get((nifty_fut.token, "1m"), [])
                if bar.start.date() == self.active_day
                and parse_hhmm("09:15") <= bar.start.time() < parse_hhmm("09:30")
            ]
            self.or_complete = len({bar.start for bar in opening_bars}) >= 15

    def _instrument_bars(self, instrument: Instrument, timeframe: str) -> list[Bar]:
        unique: dict[datetime, Bar] = {}
        for bar in sorted(
            self.history.get((instrument.token, timeframe), []),
            key=lambda item: item.start,
        ):
            unique.setdefault(bar.start, bar)
        return list(unique.values())

    def _today_bars(self, instrument: Instrument, timeframe: str) -> list[Bar]:
        return [
            bar for bar in self._instrument_bars(instrument, timeframe)
            if bar.start.date() == self.active_day
        ]

    def _opening_range(self, instrument: Instrument, require_all: bool) -> tuple[Decimal, Decimal, Decimal, Decimal] | None:
        bars = [
            bar for bar in self._today_bars(instrument, "1m")
            if parse_hhmm("09:15") <= bar.start.time() < parse_hhmm("09:30")
        ]
        bars.sort(key=lambda bar: bar.start)
        if not bars or (require_all and len({bar.start for bar in bars}) < 15):
            return None
        return (
            bars[0].open,
            max(bar.high for bar in bars),
            min(bar.low for bar in bars),
            bars[-1].close,
        )

    def _bias_observation(self) -> tuple[BiasObs, datetime] | None:
        nifty = self.futures.get("NIFTY")
        bank = self.futures.get("BANKNIFTY")
        nifty_spot = self.indices.get("NIFTY 50")
        vix = self.indices.get("INDIA VIX")
        if not all((nifty, bank, nifty_spot, vix)):
            return None
        nifty_or = self._opening_range(nifty, require_all=True)
        bank_or = self._opening_range(bank, require_all=True)
        nifty_1m = self._today_bars(nifty, "1m")
        nifty_5m = self._instrument_bars(nifty, "5m")
        bank_1m = self._today_bars(bank, "1m")
        bank_5m = self._instrument_bars(bank, "5m")
        if (
            not nifty_or or not bank_or or not nifty_1m or not nifty_5m
            or not bank_1m or not bank_5m
            or nifty_5m[-1].end != bank_5m[-1].end
        ):
            return None
        nifty_vwap = vwap(nifty_1m)
        bank_vwap = vwap(bank_1m)
        nifty_closes = [bar.close for bar in nifty_5m]
        bank_closes = [bar.close for bar in bank_5m]
        ema9 = ema(nifty_closes, 9)
        ema20 = ema(nifty_closes, 20)
        ema21 = ema(nifty_closes, 21)
        bank_ema9 = ema(bank_closes, 9)
        bank_ema21 = ema(bank_closes, 21)
        nifty_price = self.latest_prices.get(nifty.token)
        bank_price = self.latest_prices.get(bank.token)
        vix_price = self.latest_prices.get(vix.token)
        nifty_previous = self.prev_closes.get(nifty.token)
        bank_previous = self.prev_closes.get(bank.token)
        vix_previous = self.prev_closes.get(vix.token)
        latest_5m = nifty_5m[-1]
        latest_bank_5m = bank_5m[-1]
        if any(value is None for value in (
            nifty_vwap, bank_vwap, ema9, ema20, ema21, bank_ema9, bank_ema21,
            nifty_price, bank_price, vix_price, nifty_previous, bank_previous,
            vix_previous,
        )):
            return None
        above_vwap = 0
        breadth_total = 0
        for instrument in self.breadth_symbols.values():
            price = self.latest_prices.get(instrument.token)
            stock_vwap = vwap(self._today_bars(instrument, "1m"))
            if price is None or stock_vwap is None:
                continue
            breadth_total += 1
            above_vwap += int(price > stock_vwap)
        if breadth_total < self.rules.bias.breadth_min_names:
            return None
        spot_atr = atr_wilder(
            self._instrument_bars(nifty_spot, "15m"), 14
        )
        inside_or = nifty_or[2] <= nifty_price <= nifty_or[1]
        observation = BiasObs(
            nifty_price=nifty_price,
            nifty_prev_close=nifty_previous,
            nifty_vwap=nifty_vwap,
            or_open=nifty_or[0],
            or_high=nifty_or[1],
            or_low=nifty_or[2],
            or_close=nifty_or[3],
            close_5m=latest_5m.close,
            ema9_5m=ema9,
            ema20_5m=ema20,
            ema21_5m=ema21,
            breadth_above_vwap=above_vwap,
            breadth_total=breadth_total,
            bank_price=bank_price,
            bank_vwap=bank_vwap,
            bank_or_mid=(bank_or[1] + bank_or[2]) / 2,
            vix_price=vix_price,
            vix_prev_close=vix_previous,
            atr15=spot_atr,
            inside_or=inside_or,
        )
        return observation, max(latest_5m.end, latest_bank_5m.end)

    def _record_event(self, reason: Reason, snapshot: dict) -> None:
        text = logstore.render(reason.value, snapshot)
        self.conn.execute(
            """
            INSERT INTO events(day, ts, state_from, state_to, reason, reason_text, snapshot_json)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                self._today(), self.clock().isoformat(), self.state.value, self.state.value,
                reason.value, text, json.dumps(snapshot, default=str),
            ),
        )
        self.conn.commit()

    def _screen_market(self, bar_end: datetime) -> None:
        if self.rules.universe.phase != "sector_fno_stock":
            return
        nifty = self.indices.get("NIFTY 50")
        if nifty is None or self.bias_result is None:
            return

        def snap(instrument: Instrument) -> PriceSnap | None:
            last = self.latest_prices.get(instrument.token)
            previous = self.prev_closes.get(instrument.token)
            opened = self.open_0915.get(instrument.token)
            if last is None or previous is None or opened is None:
                return None
            return PriceSnap(
                instrument.token, instrument.tradingsymbol, last, previous,
                opened, self.volume_today.get(instrument.token, 0),
            )

        nifty_snap = snap(nifty)
        if nifty_snap is None:
            return
        sectors = []
        stocks_by_symbol: dict[str, Instrument] = {}
        sector_names_by_stock: dict[str, list[str]] = {}
        for sector_name, (sector_index, sector_stocks) in self.sector_map.items():
            sector_snap = snap(sector_index)
            if sector_snap is not None:
                sectors.append(sector_snap)
            for stock in sector_stocks:
                stocks_by_symbol[stock.tradingsymbol] = stock
                sector_names_by_stock.setdefault(stock.tradingsymbol, []).append(sector_name)
        stock_snaps = [
            item for instrument in stocks_by_symbol.values()
            if (item := snap(instrument)) is not None
        ]
        result = screen_stocks(
            self.bias_result.bias,
            nifty_snap,
            sectors,
            stock_snaps,
            {symbol: tuple(names) for symbol, names in sector_names_by_stock.items()},
            self.rules,
        )
        previous_shortlist = (
            tuple(item.tradingsymbol for item in self.screener_result.shortlist)
            if self.screener_result else None
        )
        self.screener_result = result
        self.conn.execute(
            """
            UPDATE sessions SET winning_sectors=?, top20_json=?, shortlist_json=? WHERE day=?
            """,
            (
                json.dumps([item.tradingsymbol for item in result.winning_sectors]),
                json.dumps([item.__dict__ for item in result.top_pool], default=str),
                json.dumps([item.__dict__ for item in result.shortlist], default=str),
                self._today(),
            ),
        )
        self.conn.commit()
        shortlist = tuple(item.tradingsymbol for item in result.shortlist)
        if previous_shortlist != shortlist:
            if shortlist:
                self._record_event(Reason.SECTOR_RANKED, {
                    "bias": self.bias_result.bias.value,
                    "sectors": ", ".join(item.tradingsymbol for item in result.winning_sectors),
                    "shortlist": ", ".join(shortlist),
                })
            else:
                self._record_event(Reason.NO_ELIGIBLE_STOCK, {})
        live_tokens = {item.token for item in result.shortlist}
        self._set_state = {
            key: value for key, value in self._set_state.items()
            if key[0] in {item.tradingsymbol for item in result.shortlist}
        }
        if self.state == State.ARMED and not any(
            state.get("armed") for state in self._set_state.values()
        ):
            self._transition(State.WATCHING, Reason.CONFIRM_FAILED, {
                "set_id": "orb_vwap", "detail": "shortlist changed",
            })
        self._warm_shortlisted(live_tokens)

    def _warm_shortlisted(self, tokens: set[int]) -> None:
        if not tokens or self.gateway is None:
            return
        try:
            previous_day = previous_trading_day(
                self.active_day, self.data_dir / "nse_holidays.txt"
            )
            start = datetime.combine(previous_day, parse_hhmm("09:15"), tzinfo=IST)
            end = self.clock()
            for token in sorted(tokens - self.warmed_tokens):
                for interval, timeframe in (("minute", "1m"), ("5minute", "5m")):
                    bars = discard_open_historical(
                        self.gateway.historical(token, start, end, interval), end
                    )
                    instrument = self.instruments_by_token[token]
                    merged = self._instrument_bars(instrument, timeframe) + bars
                    self.history[(token, timeframe)] = sorted(
                        {bar.start: bar for bar in merged}.values(),
                        key=lambda bar: bar.start,
                    )
                self.warmed_tokens.add(token)
            self.warmup_status = "ok"
        except Exception as exc:
            self.warmup_status = "failed"
            if self.state in {State.RESEARCH, State.WATCHING}:
                self._transition(State.BLOCKED, Reason.WARMUP_FAIL, {"detail": str(exc)})
            self._write_status(force=True)

    def _evaluate_stock_bar(self, bar: Bar) -> None:
        if (
            self.state not in {State.WATCHING, State.ARMED}
            or self.screener_result is None
            or self.bias_result is None
            or self.bias_result.veto is not None
            or self.bias_result.bias == Bias.SIDEWAYS
        ):
            return
        stock = self.instruments_by_token.get(bar.token)
        candidate = next(
            (item for item in self.screener_result.shortlist if item.token == bar.token),
            None,
        )
        if stock is None or candidate is None:
            return
        opening = self._opening_range(stock, require_all=False)
        bars_1m = self._today_bars(stock, "1m")
        bars_5m = self._instrument_bars(stock, "5m")
        stock_vwap = vwap(bars_1m)
        closes_1m = [item.close for item in bars_1m]
        closes_5m = [item.close for item in bars_5m]
        rsi_1m = rsi_wilder(closes_1m, 14)
        rsi_1m_prev = rsi_wilder(closes_1m[:-1], 14) if len(closes_1m) > 1 else None
        rsi_5m = rsi_wilder(closes_5m, 14)
        ema9 = ema(closes_5m, 9) or Decimal("0")
        ema21 = ema(closes_5m, 21) or Decimal("0")
        if opening is None:
            return
        current_5m_index = len(bars_5m) - 1
        prior_5m = bars_5m[:-1] if bar.timeframe == "5m" else bars_5m
        outside = bool(prior_5m and (
            prior_5m[-1].close > opening[1] or prior_5m[-1].close < opening[2]
        ))
        for set_id in self.rules.sets.active:
            state = self._set_state.setdefault(
                (stock.tradingsymbol, set_id),
                {"armed": False, "armed_bar_5m_index": None, "side": None},
            )
            context = SetContext(
                bias=self.bias_result.bias,
                veto=self.bias_result.veto,
                now=bar.end,
                rules=self.rules,
                or_high=opening[1],
                or_low=opening[2],
                vwap=stock_vwap,
                ema9_5m=ema9,
                ema21_5m=ema21,
                closed_1m=bar if bar.timeframe == "1m" else None,
                closed_5m=bar if bar.timeframe == "5m" else None,
                prev_1m_volumes=tuple(
                    item.volume for item in (bars_1m[:-1] if bar.timeframe == "1m" else bars_1m)
                ),
                rsi_1m=rsi_1m,
                rsi_5m=rsi_5m,
                armed=state["armed"],
                armed_bar_5m_index=state["armed_bar_5m_index"],
                closed_5m_index=current_5m_index if current_5m_index >= 0 else None,
                side=state["side"],
                rsi_1m_prev=rsi_1m_prev,
                prior_5m_outside_or=outside,
            )
            outcome = evaluate(set_id, context)
            if outcome == SetEval.ARM:
                state.update({
                    "armed": True,
                    "armed_bar_5m_index": current_5m_index,
                    "side": Side.LONG_CALL if self.bias_result.bias == Bias.BULLISH else Side.LONG_PUT,
                })
                details = {"set_id": set_id, "close": bar.close}
                if self.state == State.WATCHING:
                    self._transition(State.ARMED, Reason.ARMED, details)
                else:
                    self._record_event(Reason.ARMED, details)
            elif outcome == SetEval.DROP:
                state.update({"armed": False, "armed_bar_5m_index": None, "side": None})
                self._record_event(Reason.CONFIRM_FAILED, {
                    "set_id": set_id, "detail": "set conditions no longer hold",
                })
                if self.state == State.ARMED and not any(
                    item.get("armed") for item in self._set_state.values()
                ):
                    self._transition(State.WATCHING, Reason.CONFIRM_FAILED, {
                        "set_id": set_id, "detail": "all setups dropped",
                    })
            elif outcome == SetEval.CONFIRM:
                self._confirm_stock_setup(stock, candidate.sector, set_id, state)
                return

    def _reject_stock_confirmation(
        self,
        stock: Instrument,
        set_id: str,
        state: dict,
        reason: Reason,
        snapshot: dict,
    ) -> None:
        state.update({"armed": False, "armed_bar_5m_index": None, "side": None})
        self._record_event(reason, snapshot)
        if self.state == State.ARMED and not any(
            item.get("armed") for item in self._set_state.values()
        ):
            self._transition(State.WATCHING, Reason.CONFIRM_FAILED, {
                "set_id": set_id, "detail": f"{stock.tradingsymbol} confirmation rejected",
            })

    def _confirm_stock_setup(
        self,
        stock: Instrument,
        sector: str,
        set_id: str,
        state: dict,
    ) -> None:
        if not self.user_armed:
            self._reject_stock_confirmation(
                stock, set_id, state, Reason.CONFIRM_FAILED,
                {"set_id": set_id, "detail": "operator has not armed the session"},
            )
            return
        if self.clock().time() >= parse_hhmm(self.rules.session.no_new_trade_after):
            self._reject_stock_confirmation(
                stock, set_id, state, Reason.CONFIRM_FAILED,
                {"set_id": set_id, "detail": "no_new_trade_after reached"},
            )
            return
        count = self.conn.execute(
            "SELECT COUNT(*) FROM trades WHERE day=?", (self._today(),)
        ).fetchone()[0]
        if count >= self.rules.risk.max_trades_per_day:
            self._reject_stock_confirmation(
                stock, set_id, state, Reason.CONFIRM_FAILED,
                {"set_id": set_id, "detail": "max_trades_per_day reached"},
            )
            return
        if self.rules.risk.trading_mode == "live":
            self._transition(State.HALTED, Reason.ORDER_REJECTED, {
                "detail": "live entry lifecycle is not enabled; no order was sent",
            })
            return
        if self.gateway is None:
            self._reject_stock_confirmation(
                stock, set_id, state, Reason.ORDER_REJECTED,
                {"detail": "gateway is unavailable"},
            )
            return
        side = state.get("side")
        right = "CE" if side == Side.LONG_CALL else "PE"
        try:
            options = stock_options(self.instruments, stock.tradingsymbol)
            expiry = select_expiry(options, self.active_day, self.rules)
            spot = self.latest_prices.get(stock.token)
            if spot is None:
                raise RuntimeError("underlying price is unavailable")
            option = select_atm(options, expiry, right, spot)
        except NoExpiryError:
            self._reject_stock_confirmation(
                stock, set_id, state, Reason.EXPIRY_WEEK_SKIP,
                {"symbol": stock.tradingsymbol},
            )
            return
        except (ValueError, RuntimeError) as exc:
            self._reject_stock_confirmation(
                stock, set_id, state, Reason.CONFIRM_FAILED,
                {"set_id": set_id, "detail": str(exc)},
            )
            return
        key = f"NFO:{option.tradingsymbol}"
        try:
            quote = self.gateway.quote([key]).get(key, {})
        except Exception as exc:
            self._transition(State.HALTED, Reason.ORDER_REJECTED, {
                "detail": f"option quote failed: {exc}",
            })
            return
        depth = quote.get("depth") or {}
        bids = depth.get("buy") or []
        asks = depth.get("sell") or []
        bid = Decimal(str(bids[0]["price"])) if bids else None
        ask = Decimal(str(asks[0]["price"])) if asks else None
        bid_qty = (
            int(bids[0]["quantity"])
            if bids and bids[0].get("quantity") is not None else None
        )
        ask_qty = (
            int(asks[0]["quantity"])
            if asks and asks[0].get("quantity") is not None else None
        )
        quantity = option.lot_size * self.rules.risk.max_lots
        rejection = liquidity_ok(
            bid, ask, int(quote.get("oi") or 0), self.rules,
            lot_size=option.lot_size, qty=quantity,
            bid_qty=bid_qty, ask_qty=ask_qty,
        )
        if rejection == Reason.PREMIUM_TOO_SMALL:
            mid = (bid + ask) / 2 if bid is not None and ask is not None else Decimal("0")
            self._reject_stock_confirmation(
                stock, set_id, state, rejection, {"mid": mid},
            )
            return
        if rejection is not None:
            self._reject_stock_confirmation(
                stock, set_id, state, rejection, {"symbol": option.tradingsymbol},
            )
            return
        if bid is None or ask is None:
            self._reject_stock_confirmation(
                stock, set_id, state, Reason.LIQUIDITY_REJECT,
                {"symbol": option.tradingsymbol},
            )
            return
        limit = entry_limit(bid, ask, option.tick_size)
        bracket = build_bracket(limit, option.tick_size, option.lot_size, self.rules)
        if bracket == Reason.RISK_CAP:
            raw_r = limit * Decimal(self.rules.risk.premium_stop_pct) / 100
            stop = round_down(limit - raw_r, option.tick_size)
            estimated_risk = (limit - stop) * quantity
            self._reject_stock_confirmation(
                stock, set_id, state, Reason.RISK_CAP,
                {"risk_inr": estimated_risk},
            )
            return
        if not isinstance(bracket, Bracket):
            raise RuntimeError("unexpected bracket result")
        try:
            self.gateway.subscribe([option.token], "full")
        except Exception as exc:
            self._transition(State.HALTED, Reason.ORDER_REJECTED, {
                "detail": f"option subscription failed: {exc}",
            })
            return
        self.instruments_by_token[option.token] = option
        self._subscription_tokens.append(option.token)
        self.ws_subscribed_count = len(set(self._subscription_tokens))
        number = self.conn.execute(
            "SELECT COUNT(DISTINCT tag) FROM orders WHERE day=?", (self._today(),)
        ).fetchone()[0] + 1
        tag = f"D{self.active_day:%Y%m%d}{number:02d}"
        self._transition(State.ENTRY_WORKING, Reason.ORDER_SUBMITTED, {
            "symbol": option.tradingsymbol, "qty": bracket.quantity,
            "price": bracket.entry_limit, "tag": tag,
        })
        try:
            fill = self.paper_broker.enter(
                option.tradingsymbol, bracket.quantity, bracket.entry_limit, bracket,
                option.tick_size, self.clock(),
            )
        except (RuntimeError, ValueError) as exc:
            self._transition(State.HALTED, Reason.ORDER_REJECTED, {"detail": str(exc)})
            return
        self.active_stock = stock
        self.active_option = option
        self.active_sector = sector
        self.active_set_id = set_id
        self.active_bracket = bracket
        quote_last = quote.get("last_price")
        self.active_option_last = Decimal(str(quote_last if quote_last is not None else ask))
        self.active_option_bid = bid
        self.active_option_last_seen = self.clock()
        self.active_mfe = Decimal("0")
        self.active_mae = Decimal("0")
        self.conn.execute(
            """
            INSERT INTO orders(day, tag, role, mode, broker_order_id, symbol, side, qty,
                               limit_price, status, filled_qty, avg_price)
            VALUES (?, ?, 'entry', 'paper', NULL, ?, 'BUY', ?, ?, 'filled', ?, ?)
            """,
            (
                self._today(), tag, option.tradingsymbol, bracket.quantity,
                str(bracket.entry_limit), bracket.quantity, str(fill.price),
            ),
        )
        self.conn.execute(
            """
            INSERT INTO trades(day, mode, underlying, sector, symbol, right, strike, expiry,
                               qty, entry_price, stop_trigger, target, rules_version, set_id)
            VALUES (?, 'paper', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                self._today(), stock.tradingsymbol, sector, option.tradingsymbol,
                right, str(option.strike), expiry.isoformat(), bracket.quantity,
                str(fill.price), str(bracket.stop_trigger), str(bracket.target),
                self.rules_version, set_id,
            ),
        )
        self.conn.commit()
        for item in self._set_state.values():
            item.update({"armed": False, "armed_bar_5m_index": None, "side": None})
        self._transition(State.MANAGING, Reason.ORDER_FILLED, {
            "symbol": option.tradingsymbol, "qty": bracket.quantity,
            "price": fill.price, "r": bracket.r,
            "stop": bracket.stop_trigger, "target": bracket.target,
        })
        self._notify(f"Paper trade filled: {option.tradingsymbol} x{bracket.quantity} at {fill.price}")

    def _close_paper_trade(self, fill: PaperFill, reason: Reason, transition: bool = True) -> None:
        if self.active_bracket is None or self.active_option is None:
            raise RuntimeError("cannot close a paper trade without an active bracket")
        gross = (fill.price - self.active_bracket.entry_limit) * fill.quantity
        charge = costs(
            self.active_bracket.entry_limit, fill.price, fill.quantity, self.rules
        )
        net = gross - charge
        self.conn.execute(
            """
            UPDATE trades SET exit_price=?, exit_reason=?, mfe=?, mae=?, gross_pnl=?,
                              costs=?, net_pnl=? WHERE day=?
            """,
            (
                str(fill.price), self.paper_broker.position.exit_reason,
                str(self.active_mfe), str(self.active_mae), str(gross),
                str(charge), str(net), self._today(),
            ),
        )
        self.conn.commit()
        if transition and self.state in {State.MANAGING, State.HALTED}:
            self._transition(State.DONE, reason, {"price": fill.price})
        else:
            self._record_event(reason, {"price": fill.price})
        self._notify(f"Paper trade flattened: {fill.symbol} at {fill.price} ({reason.value})")

    def _manage_paper_tick(self, tick: Tick) -> None:
        if (
            self.state not in {State.MANAGING, State.HALTED}
            or self.active_option is None
            or tick.token != self.active_option.token
        ):
            return
        self.active_option_last_seen = tick.ts
        self._manage_paper_price(tick.last, tick.bid, tick.ts)

    def _manage_paper_price(
        self,
        last: Decimal,
        bid: Decimal | None,
        timestamp: datetime,
    ) -> None:
        if self.state not in {State.MANAGING, State.HALTED}:
            return
        if self.active_option is None:
            return
        self.active_option_last = last
        self.active_option_bid = bid
        position = self.paper_broker.position
        if position is None or not position.is_open:
            return
        self.active_mfe = max(self.active_mfe, last - position.entry_price, Decimal("0"))
        self.active_mae = max(self.active_mae, position.entry_price - last, Decimal("0"))
        fill = self.paper_broker.on_tick(last, bid, timestamp)
        if fill is not None:
            reason = Reason.STOP_HIT if position.exit_reason == "stop_hit" else Reason.TARGET_HIT
            self._paper_stop_triggered_at = None
            self._close_paper_trade(fill, reason)
        elif last <= position.stop_trigger:
            if self._paper_stop_triggered_at is None:
                self._paper_stop_triggered_at = timestamp
            elif (timestamp - self._paper_stop_triggered_at).total_seconds() >= 2:
                self.paper_broker.step_stop_limit_floor(self.active_option.tick_size)
                self._paper_stop_triggered_at = timestamp
        else:
            self._paper_stop_triggered_at = None

    def _check_paper_feed(self, now: datetime) -> None:
        if (
            self.state not in {State.MANAGING, State.HALTED}
            or self.active_option is None
            or self.gateway is None
            or self.active_option_last_seen is None
            or (now - self.active_option_last_seen).total_seconds() <= 15
            or time_module.monotonic() - self._last_option_quote_monotonic < 1
        ):
            return
        self._last_option_quote_monotonic = time_module.monotonic()
        key = f"NFO:{self.active_option.tradingsymbol}"
        try:
            quote = self.gateway.quote([key]).get(key, {})
            value = quote.get("last_price")
            if value is None or Decimal(str(value)) <= 0:
                raise RuntimeError("fallback quote has no valid last price")
            depth = quote.get("depth") or {}
            bids = depth.get("buy") or []
            bid = Decimal(str(bids[0]["price"])) if bids else None
            self._manage_paper_price(Decimal(str(value)), bid, now)
        except Exception as exc:
            logger.exception("Paper option feed and REST quote fallback failed")
            if self.state == State.MANAGING:
                self._transition(State.HALTED, Reason.BLIND, {})
            else:
                logger.error("Position remains open while feed is blind: %s", exc)

    def _exit_paper_position(self, reason: Reason, transition: bool = True) -> bool:
        position = self.paper_broker.position
        if position is None or not position.is_open:
            return True
        last = self.active_option_last
        bid = self.active_option_bid
        tick_is_fresh = (
            self.active_option_last_seen is not None
            and (self.clock() - self.active_option_last_seen).total_seconds() <= 15
        )
        if (
            (last is None or last <= 0 or not tick_is_fresh)
            and self.active_option is not None and self.gateway is not None
        ):
            key = f"NFO:{self.active_option.tradingsymbol}"
            quote = self.gateway.quote([key]).get(key, {})
            quote_last = quote.get("last_price")
            last = Decimal(str(quote_last if quote_last is not None else "0"))
            depth = quote.get("depth") or {}
            bids = depth.get("buy") or []
            bid = Decimal(str(bids[0]["price"])) if bids else None
        if last is None or last <= 0:
            raise RuntimeError("paper position cannot be flattened without a valid option price")
        fill = self.paper_broker.exit_at_marketable_limit(
            last, bid, reason.value, self.clock(),
        )
        if fill is None:
            return False
        self._close_paper_trade(fill, reason, transition=transition)
        return True

    def _refresh_strategy_snapshot(self, bar_end: datetime) -> None:
        if self.state in {State.MANAGING, State.DONE, State.HALTED, State.BLOCKED}:
            return
        if bar_end.time() < parse_hhmm("09:30"):
            return
        observation = self._bias_observation()
        if observation is None:
            return
        obs, latest_end = observation
        if self._last_bias_bar_end == latest_end:
            return
        self._last_bias_bar_end = latest_end
        previous_bias = self.bias_result.bias if self.bias_result else None
        result = compute_bias(obs, self.rules)
        self.bias_result = result
        if previous_bias is not None and previous_bias != result.bias:
            for state in self._set_state.values():
                state.update({"armed": False, "armed_bar_5m_index": None, "side": None})
            if self.state == State.ARMED:
                self._transition(State.WATCHING, Reason.CONFIRM_FAILED, {
                    "set_id": "orb_vwap", "detail": "market bias changed",
                })
        reason = {
            "BULLISH": Reason.BIAS_BULLISH,
            "BEARISH": Reason.BIAS_BEARISH,
            "SIDEWAYS": Reason.BIAS_SIDEWAYS,
        }[result.bias.value]
        line_points = {line.name: line.points for line in result.lines}
        self.conn.execute(
            "UPDATE sessions SET bias=?, score=?, veto=? WHERE day=?",
            (
                result.bias.value, result.score,
                result.veto.value if result.veto else None, self._today(),
            ),
        )
        self.conn.commit()
        self._record_event(reason, {
            "score": result.score,
            "prev": line_points["prev_close"],
            "vwap": line_points["vwap"],
            "opening": line_points["opening_range"],
            "ema": line_points["ema_trend"],
            "breadth": line_points["breadth"],
            "bank": line_points["banknifty"],
            "lines": [
                {"name": line.name, "points": line.points, "detail": line.detail}
                for line in result.lines
            ],
        })
        if result.veto is not None:
            for state in self._set_state.values():
                state.update({"armed": False, "armed_bar_5m_index": None, "side": None})
            if self.state == State.ARMED:
                self._transition(State.WATCHING, Reason.CONFIRM_FAILED, {
                    "set_id": "orb_vwap", "detail": result.veto.value,
                })
            if self.state in {State.RESEARCH, State.WATCHING}:
                self._transition(State.BLOCKED, result.veto, {
                    "vix_chg": (
                        (obs.vix_price - obs.vix_prev_close) / obs.vix_prev_close * 100
                        if obs.vix_prev_close > 0 else Decimal("0")
                    ),
                    "or_range": obs.or_high - obs.or_low,
                    "frac": self.rules.bias.dead_range_atr_frac,
                    "atr": obs.atr15 or Decimal("0"),
                    "detail": result.veto.value,
                })
            return
        if self.state == State.RESEARCH:
            self._transition(State.WATCHING, Reason.SESSION_DONE, {
                "detail": "opening range and first bias computed",
            })
        if self.state == State.WATCHING and self._last_screen_bar_end != latest_end:
            self._last_screen_bar_end = latest_end
            self._screen_market(latest_end)

    def _prepare_market_data(self) -> None:
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                self.instruments = load_session_instruments(
                    self.data_dir, self.active_day, self.gateway
                )
                break
            except Exception as exc:
                last_error = exc
                if attempt < 2:
                    self.sleep(5)
        else:
            raise RuntimeError(f"instrument dump unavailable after 3 attempts: {last_error}")
        self.futures = {
            name: resolve_near_month_fut(self.instruments, name, self.active_day)
            for name in ("NIFTY", "BANKNIFTY")
        }
        self.indices = {
            name: require_index(self.instruments, name)
            for name in ("NIFTY 50", "INDIA VIX")
        }
        self.breadth_symbols = resolve_symbols(
            self.instruments,
            self._read_symbol_file(self.data_dir / "nifty50.txt"),
        )
        self.sector_map = load_sector_map(
            self.data_dir / "sector_fno_map.yaml", self.instruments
        )
        self.expiry_day = is_expiry_day(nifty_options(self.instruments), self.active_day)
        if self.expiry_day and self.rules.calendar.skip_nifty_expiry_day:
            self._transition(State.BLOCKED, Reason.EXPIRY_DAY, {})
            return
        self._subscription_tokens = list({
            instrument.token
            for instrument in [
                *self.futures.values(),
                *self.indices.values(),
                *self.breadth_symbols.values(),
                *[index for index, _ in self.sector_map.values()],
                *[stock for _, stocks in self.sector_map.values() for stock in stocks],
            ]
        })
        for instrument in [*self.futures.values(), *self.indices.values()]:
            self.builders[(instrument.token, 1)] = CandleBuilder(instrument.token, 1)
            self.builders[(instrument.token, 5)] = CandleBuilder(instrument.token, 5)
        subscribed = {
            instrument.token: instrument
            for instrument in [
                *self.futures.values(),
                *self.indices.values(),
                *self.breadth_symbols.values(),
                *[index for index, _ in self.sector_map.values()],
                *[stock for _, stocks in self.sector_map.values() for stock in stocks],
            ]
        }
        self.instruments_by_token = subscribed
        for token in subscribed:
            for timeframe in (1, 5):
                self.builders.setdefault((token, timeframe), CandleBuilder(token, timeframe))

    def _warmup(self) -> None:
        if self.gateway is None:
            self.warmup_status = "failed"
            raise RuntimeError("cannot warm up without a Kite gateway")
        try:
            previous_day = previous_trading_day(
                self.active_day, self.data_dir / "nse_holidays.txt"
            )
            start = datetime.combine(previous_day, parse_hhmm("09:15"), tzinfo=IST)
            end = self.clock()
            for instrument in self.futures.values():
                for interval in ("minute", "5minute"):
                    bars = self.gateway.historical(instrument.token, start, end, interval)
                    timeframe = "1m" if interval == "minute" else "5m"
                    self.history[(instrument.token, timeframe)] = discard_open_historical(
                        bars, end
                    )
            spot = self.indices["NIFTY 50"]
            spot_start = end - timedelta(days=30)
            bars = self.gateway.historical(spot.token, spot_start, end, "15minute")
            self.history[(spot.token, "15m")] = discard_open_historical(bars, end)
            self._update_or_state()
            self.warmup_status = "ok"
        except Exception:
            self.warmup_status = "failed"
            raise


    def _open_gateway(self) -> None:
        try:
            try:
                token = self._valid_token_data()
            except RuntimeError as exc:
                logger.error("Removing invalid expired token file: %s", exc)
                token = None
        except RuntimeError as exc:
            self.token_verify_status = "invalid"
            self.token_verify_error = str(exc)
            self._write_status(force=True)
            return
        if token is None:
            raise RuntimeError("no valid Kite access token")
        credentials = logstore.load_credentials(self.data_dir)
        api_key = credentials.get("KITE_API_KEY")
        if not api_key:
            raise RuntimeError("KITE_API_KEY is not configured")
        if self.gateway is not None:
            self.gateway.close()
        self.ws_connected = False
        self.ws_subscribed_count = 0
        self._ws_connect_event.clear()
        self.gateway = self.gateway_factory(api_key, token["access_token"])
        self.api_key = api_key
        self.execution = Execution(self.gateway)

    def _available_margin(self, margins: dict) -> Decimal | None:
        equity = margins.get("equity", margins)
        available = equity.get("available", {}) if isinstance(equity, dict) else {}
        value = available.get("live_balance")
        if value is None:
            value = available.get("cash")
        return Decimal(str(value)) if value is not None else None

    def _verify_broker_token(self, rebuild: bool = False) -> bool:
        try:
            if rebuild or self.gateway is None:
                self._open_gateway()
            self.gateway.profile()
            margins = self.gateway.margins_equity()
            self.available_margin_inr = self._available_margin(margins)
            self.token_verified_at = self.clock().isoformat()
            self.token_verify_status = "ok"
            self.token_verify_error = None
            self._write_status(force=True)
            return True
        except Exception as exc:
            self.token_verified_at = self.clock().isoformat()
            self.token_verify_status = "invalid"
            self.token_verify_error = str(exc)
            self._write_status(force=True)
            return False

    def _chrony_ok(self) -> bool:
        if os.name == "nt":
            return True
        import subprocess

        result = subprocess.run(
            ["timedatectl", "show", "-p", "NTPSynchronized", "--value"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        return result.returncode == 0 and result.stdout.strip().lower() == "yes"

    async def _connect_ticker(self) -> None:
        self._ws_connect_event.clear()
        self.gateway.connect_ticker(
            self._on_ticks, self._on_connect, self._on_close, self._on_error
        )
        connected = await asyncio.wait_for(
            asyncio.to_thread(self._ws_connect_event.wait, 10),
            timeout=11,
        )
        if not connected or not self.ws_connected:
            raise RuntimeError("WebSocket did not connect within 10 seconds")

    def _subscribe(self) -> None:
        if self.gateway is None or not self._subscription_tokens:
            raise RuntimeError("market subscriptions are not prepared")
        self.gateway.subscribe(self._subscription_tokens, "quote")
        self.ws_subscribed_count = len(self._subscription_tokens)
        if self.clock().time() > parse_hhmm("09:15"):
            keys: list[str] = []
            by_key: dict[str, int] = {}
            for instrument in self.instruments:
                if instrument.token not in self._subscription_tokens:
                    continue
                key = f"{instrument.exchange}:{instrument.tradingsymbol}"
                keys.append(key)
                by_key[key] = instrument.token
            quotes = self.gateway.quote(keys)
            for key, token in by_key.items():
                quote = quotes.get(key, {})
                ohlc = quote.get("ohlc") or {}
                if ohlc.get("open") is not None:
                    self.open_0915[token] = Decimal(str(ohlc["open"]))
                if ohlc.get("close") is not None:
                    self.prev_closes[token] = Decimal(str(ohlc["close"]))
        self._write_status(force=True)

    async def _health_check(self) -> None:
        if self.rules.risk.trading_mode == "live":
            self._transition(
                State.BLOCKED,
                Reason.HEALTH_FAIL,
                {"detail": (
                    "LIVE_TRADING env is false"
                    if not self._live_allowed()
                    else "live order lifecycle is not implemented"
                )},
            )
            self.health_done = True
            return
        if not self._valid_token():
            if self.state != State.BLOCKED:
                self._transition(State.BLOCKED, Reason.NO_TOKEN, {})
            self.health_done = True
            return
        try:
            if self.token_verify_status != "ok" and not self._verify_broker_token():
                raise RuntimeError(self.token_verify_error or "Kite token verification failed")
            if not self.instruments:
                self._prepare_market_data()
            if self.expiry_day and self.rules.calendar.skip_nifty_expiry_day:
                self.health_done = True
                return
            if len(self.breadth_symbols) < self.rules.bias.breadth_min_names:
                self._transition(
                    State.BLOCKED,
                    Reason.BREADTH_INCOMPLETE,
                    {"n": len(self.breadth_symbols)},
                )
                self.health_done = True
                return
            if self.rules.universe.phase == "sector_fno_stock" and len(self.sector_map) < 5:
                raise InstrumentError("fewer than five valid F&O sectors resolved")
            if not self._chrony_ok():
                raise RuntimeError("system clock synchronization check failed")
            await self._connect_ticker()
            if self.state == State.BLOCKED and self._block_reason() == Reason.NO_TOKEN.value:
                self._transition(State.IDLE, Reason.SESSION_DONE, {"detail": "token restored"})
            if self.state == State.IDLE:
                self._transition(State.RESEARCH, Reason.SESSION_DONE, {"detail": "health passed"})
        except Exception as exc:
            if self.state != State.BLOCKED:
                self._transition(State.BLOCKED, Reason.HEALTH_FAIL, {"detail": str(exc)})
        self.health_done = True

    def _read_symbol_file(self, path: Path) -> list[str]:
        try:
            return [
                line.strip() for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip() and not line.strip().startswith("#")
            ]
        except OSError as exc:
            raise RuntimeError(f"unable to read {path.name}") from exc

    def _block_reason(self) -> str | None:
        row = self.conn.execute(
            "SELECT block_reason FROM sessions WHERE day=?", (self._today(),)
        ).fetchone()
        return row["block_reason"] if row else None

    def _expire_token_if_needed(self) -> None:
        path = self.data_dir / "token.json"
        if not path.exists() or self.clock().time() < parse_hhmm("06:00"):
            return
        token = self._valid_token_data()
        if token is not None:
            return
        path.unlink(missing_ok=True)
        if self.gateway is not None:
            self.gateway.close()
        self.gateway = None
        self.execution = None
        self.token_verify_status = "unverified"
        self.token_verify_error = None
        self.token_verified_at = None
        self.available_margin_inr = None
        if self.state not in {State.MANAGING, State.ENTRY_WORKING, State.DONE, State.HALTED}:
            self._transition(State.BLOCKED, Reason.NO_TOKEN, {})

    def _rollover(self) -> bool:
        new_day = self.clock().date()
        if new_day == self.active_day:
            return True
        if self.state in {State.MANAGING, State.ENTRY_WORKING} and self.rules.risk.trading_mode == "live":
            try:
                self._kill()
            except Exception:
                logger.exception("Could not verify flattening before date rollover")
                return False
        position = self.paper_broker.position
        if self.rules.risk.trading_mode == "paper" and position is not None and position.is_open:
            try:
                self._exit_paper_position(Reason.TIME_FLATTEN, transition=False)
            except Exception:
                logger.exception("Could not flatten paper position before date rollover")
                return False
        if self.gateway is not None:
            self.gateway.close()
        self.gateway = None
        self.execution = None
        self.active_day = new_day
        self.rules = load_scheduled(self.conn)
        self.rules_version = self._scheduled_version()
        self.rules_latched = self.clock().time() >= parse_hhmm("09:00")
        self.health_done = False
        self._startup_verified = False
        self.instruments.clear()
        self.indices.clear()
        self.futures.clear()
        self.sector_map.clear()
        self.breadth_symbols.clear()
        self.builders.clear()
        self.history.clear()
        self.open_0915.clear()
        self.prev_closes.clear()
        self.latest_prices.clear()
        self.volume_today.clear()
        self.warmed_tokens.clear()
        self.bias_result = None
        self.screener_result = None
        self._last_bias_bar_end = None
        self._last_screen_bar_end = None
        self._set_state.clear()
        self.paper_broker = PaperBroker()
        self.active_stock = None
        self.active_option = None
        self.active_sector = None
        self.active_set_id = None
        self.active_bracket = None
        self.active_option_last = None
        self.active_option_bid = None
        self.active_option_last_seen = None
        self._last_option_quote_monotonic = 0.0
        self.active_mfe = Decimal("0")
        self.active_mae = Decimal("0")
        self._paper_stop_triggered_at = None
        self.warmup_status = "pending"
        self.or_complete = False
        self.ws_connected = False
        self.ws_subscribed_count = 0
        self.last_tick_at = None
        self.tick_count = 0
        self.last_1m_bar_ts = None
        self.last_5m_bar_ts = None
        self.nifty_spot_last = None
        self.nifty_fut_last = None
        self.expiry_day = False
        self.user_armed = False
        self._subscription_tokens.clear()
        self._load_or_create_session()
        self.calendar_allowed = self._calendar_ok()
        if self.calendar_allowed and not self._valid_token():
            self._transition(State.BLOCKED, Reason.NO_TOKEN, {})
        self._write_status(force=True)
        return True

    def _process_queued_ticks(self) -> None:
        if self._tick_queue is None:
            return
        while True:
            try:
                ticks = self._tick_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            self._process_ticks(ticks)

    def _close_due_candles(self, now: datetime) -> None:
        bars: list[Bar] = []
        for builder in self.builders.values():
            bars.extend(builder.on_timer(now))
        bars.sort(key=lambda bar: (0 if bar.timeframe == "5m" else 1, bar.start, bar.token))
        for bar in bars:
            self.history.setdefault((bar.token, bar.timeframe), []).append(bar)
            if bar.timeframe == "5m":
                self.last_5m_bar_ts = bar.end
            else:
                self.last_1m_bar_ts = bar.end
        self._after_closed_bars(bars)

    def _kill(self) -> None:
        if self.rules.risk.trading_mode != "live":
            return
        if not self._live_allowed() or self.gateway is None or self.execution is None:
            raise RuntimeError("live kill cannot safely reach the broker")
        local_orders = self.conn.execute(
            "SELECT broker_order_id, symbol FROM orders WHERE day=?",
            (self._today(),),
        ).fetchall()
        symbols = {row["symbol"] for row in local_orders if row["symbol"]}
        broker_orders = self.gateway.orders()
        open_statuses = {
            "open", "trigger pending", "put order req received",
            "validation pending", "open pending",
        }
        for row in local_orders:
            order_id = row["broker_order_id"]
            if not order_id:
                continue
            broker_order = next(
                (order for order in broker_orders if str(order.get("order_id")) == str(order_id)),
                None,
            )
            if broker_order and str(broker_order.get("status", "")).lower() in open_statuses:
                self.execution.cancel_and_wait(str(order_id))
        positions = self.gateway.positions()
        for symbol in symbols:
            order_id = None
            current_price: Decimal | None = None
            current_side: str | None = None
            for attempt in range(3):
                position = next(
                    (
                        item for item in positions.get("net", [])
                        if item.get("tradingsymbol") == symbol
                    ),
                    None,
                )
                quantity = int(position.get("quantity", 0)) if position else 0
                if quantity == 0:
                    break
                instrument = next(
                    (row for row in self.instruments if row.tradingsymbol == symbol),
                    None,
                )
                if instrument is None:
                    raise RuntimeError(f"cannot flatten {symbol}: instrument tick size is unknown")
                side = "BUY" if quantity < 0 else "SELL"
                if order_id is None:
                    quote_key = f"NFO:{symbol}"
                    quote = self.gateway.quote([quote_key]).get(quote_key, {})
                    depth = quote.get("depth") or {}
                    levels = depth.get("sell" if side == "BUY" else "buy") or []
                    if not levels:
                        raise RuntimeError(f"cannot flatten {symbol}: broker quote has no executable depth")
                    current_price = Decimal(str(levels[0]["price"]))
                    current_side = side
                    order_id = self.gateway.place(
                        variety="regular",
                        exchange="NFO",
                        tradingsymbol=symbol,
                        transaction_type=side,
                        quantity=abs(quantity),
                        product="MIS",
                        order_type="LIMIT",
                        price=str(current_price),
                        validity="DAY",
                        tag=f"D{self.clock():%Y%m%d}99",
                    )
                elif current_price is not None and current_side is not None:
                    step = instrument.tick_size * 2
                    current_price = max(instrument.tick_size, current_price + step if current_side == "BUY"
                                        else current_price - step)
                    self.gateway.modify(
                        str(order_id),
                        variety="regular",
                        price=str(current_price),
                    )
                for _ in range(50):
                    self.execution._sleep(0.1)
                    order = next(
                        (item for item in self.gateway.orders()
                         if str(item.get("order_id")) == str(order_id)),
                        None,
                    )
                    if order is None:
                        raise RuntimeError(f"flatten order {order_id} is missing")
                    order_status = str(order.get("status", "")).lower()
                    if order_status == "complete":
                        positions = self.gateway.positions()
                        order_id = None
                        current_price = None
                        current_side = None
                        break
                    if order_status in {"cancelled", "rejected"}:
                        raise RuntimeError(f"flatten order {order_id} ended {order_status}")
                else:
                    if attempt < 2:
                        continue
                    raise RuntimeError(f"flatten order {order_id} did not fill")
                positions = self.gateway.positions()
        remaining = self.gateway.positions()
        if any(
            item.get("tradingsymbol") in symbols and int(item.get("quantity", 0)) != 0
            for item in remaining.get("net", [])
        ):
            raise RuntimeError("kill flatten orders submitted but broker is not flat yet")

    def _process_one_command(self) -> bool:
        command = self.conn.execute(
            "SELECT id, name, payload_json FROM commands WHERE status='pending' ORDER BY id LIMIT 1"
        ).fetchone()
        if command is None:
            return False
        result = "done"
        try:
            name = command["name"]
            if name == "ARM":
                if (
                    self.state not in {State.IDLE, State.RESEARCH, State.WATCHING}
                    or not self.calendar_allowed
                    or self.expiry_day
                    or self.clock().time() < parse_hhmm("08:45")
                    or self.clock().time() >= parse_hhmm(self.rules.session.no_new_trade_after)
                    or self.token_verify_status != "ok"
                ):
                    raise RuntimeError("session is not in an armable state")
                self.user_armed = True
                self.conn.execute(
                    "UPDATE sessions SET user_armed=1 WHERE day=?", (self._today(),)
                )
            elif name == "KILL":
                if self.state == State.DONE:
                    raise RuntimeError("session is already done")
                try:
                    if self.rules.risk.trading_mode == "live":
                        self._kill()
                    else:
                        self._exit_paper_position(Reason.KILL_SWITCH, transition=False)
                finally:
                    if self.state != State.HALTED:
                        self._transition(State.HALTED, Reason.KILL_SWITCH, {})
                if self.rules.risk.trading_mode != "live":
                    position = self.paper_broker.position
                    if position is None or not position.is_open:
                        self._transition(State.DONE, Reason.SESSION_DONE, {
                            "detail": "kill switch flattened paper position",
                        })
            elif name == "RELOAD_TOKEN":
                token = self._valid_token_data()
                if token is not None:
                    if not self._verify_broker_token(rebuild=True):
                        raise RuntimeError(self.token_verify_error or "Kite token verification failed")
                    if (
                        self.state == State.BLOCKED
                        and self._block_reason() == Reason.NO_TOKEN.value
                        and self.clock().time() < parse_hhmm(self.rules.session.no_new_trade_after)
                    ):
                        self._transition(State.IDLE, Reason.SESSION_DONE, {"detail": "token restored"})
                    self.health_done = False
                else:
                    if self.gateway is not None:
                        self.gateway.close()
                    self.gateway = None
                    self.execution = None
                    self.token_verify_status = "unverified"
                    self.token_verified_at = None
                    self.available_margin_inr = None
                    if self.state not in {State.MANAGING, State.ENTRY_WORKING, State.DONE, State.HALTED}:
                        self._transition(State.BLOCKED, Reason.NO_TOKEN, {})
            elif name == "RELOAD_CONFIG":
                credentials = logstore.load_credentials(self.data_dir)
                new_api_key = credentials.get("KITE_API_KEY")
                key_changed = new_api_key != self.api_key
                token = self._valid_token_data()
                if key_changed and token is None:
                    if self.gateway is not None:
                        self.gateway.close()
                    self.gateway = None
                    self.execution = None
                    self.token_verify_status = "unverified"
                    self.token_verified_at = None
                    self.available_margin_inr = None
                    if self.state not in {State.DONE, State.HALTED}:
                        self._transition(State.BLOCKED, Reason.NO_TOKEN, {})
                elif token is not None:
                    if not self._verify_broker_token(rebuild=True):
                        raise RuntimeError(self.token_verify_error or "Kite token verification failed")
                else:
                    if self.gateway is not None:
                        self.gateway.close()
                    self.gateway = None
                    self.execution = None
                    self.token_verify_status = "unverified"
                    self.token_verified_at = None
                    self.available_margin_inr = None
                    if self.state not in {State.DONE, State.HALTED}:
                        self._transition(State.BLOCKED, Reason.NO_TOKEN, {})
                self.health_done = False
            elif name == "VERIFY_TOKEN":
                if not self._verify_broker_token(rebuild=self.gateway is None):
                    raise RuntimeError(self.token_verify_error or "Kite token verification failed")
            else:
                raise RuntimeError(f"unsupported command {name}")
        except Exception as exc:
            result = f"rejected: {exc}"
        self.conn.execute(
            "UPDATE commands SET status=?, result_text=? WHERE id=?",
            ("done" if result == "done" else "rejected", result, command["id"]),
        )
        self.conn.commit()
        return True

    async def run_once(self) -> None:
        if not self._rollover():
            self._write_status(force=True)
            return
        self._expire_token_if_needed()
        pending = self.conn.execute(
            "SELECT name FROM commands WHERE status='pending' ORDER BY id LIMIT 1"
        ).fetchone()
        if not self._startup_verified:
            try:
                token_available = self._valid_token_data() is not None
                if token_available and (pending is None or pending["name"] not in {"VERIFY_TOKEN", "RELOAD_TOKEN"}):
                    self._verify_broker_token()
            except RuntimeError as exc:
                self.token_verify_status = "invalid"
                self.token_verify_error = str(exc)
            self._startup_verified = True
        self._process_one_command()
        if not self.calendar_allowed:
            self._write_status()
            return
        current = self.clock()
        if not self.rules_latched and current.time() >= parse_hhmm("09:00"):
            self._latch_rules()
            self.calendar_allowed = self._calendar_ok()
            if not self.calendar_allowed:
                self.health_done = True
        if (
            self.calendar_allowed
            and self.token_verify_status == "ok"
            and not self.instruments
            and current.time() >= parse_hhmm("09:00")
        ):
            try:
                self._prepare_market_data()
            except Exception as exc:
                self._transition(State.BLOCKED, Reason.HEALTH_FAIL, {"detail": str(exc)})
                self.health_done = True
        if (
            self.calendar_allowed
            and self.gateway is not None
            and self.warmup_status == "pending"
            and current.time() >= parse_hhmm("09:05")
            and self.instruments
        ):
            try:
                self._warmup()
            except Exception as exc:
                self._transition(State.BLOCKED, Reason.WARMUP_FAIL, {"detail": str(exc)})
        if not self.health_done and current.time() >= parse_hhmm("09:10"):
            await self._health_check()
        if (
            self.state in {State.RESEARCH, State.WATCHING, State.ARMED, State.MANAGING}
            and self.ws_connected
            and not self.ws_subscribed_count
            and current.time() >= parse_hhmm("09:15")
        ):
            try:
                self._subscribe()
            except Exception as exc:
                self._transition(State.BLOCKED, Reason.HEALTH_FAIL, {"detail": str(exc)})
        self._process_queued_ticks()
        self._close_due_candles(current)
        self._update_or_state()
        self._check_paper_feed(current)
        if (
            self.rules.risk.trading_mode == "paper"
            and self.state in {State.MANAGING, State.HALTED}
            and current.time() >= parse_hhmm(self.rules.session.flatten_by)
        ):
            try:
                self._exit_paper_position(Reason.TIME_FLATTEN)
            except Exception as exc:
                logger.exception("Paper position time flatten failed")
                if self.state != State.HALTED:
                    self._transition(State.HALTED, Reason.ORDER_REJECTED, {"detail": str(exc)})
        self._write_status()
        if current.time() >= parse_hhmm(self.rules.session.no_new_trade_after):
            if self.state in {State.RESEARCH, State.WATCHING, State.ARMED}:
                self._transition(State.DONE, Reason.WINDOW_CLOSED, {})

    async def run(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._tick_queue = asyncio.Queue()
        try:
            while not self._closed:
                await self.run_once()
                await asyncio.sleep(0.25)
        finally:
            self.close()

    async def step(self) -> None:
        await self.run_once()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self.gateway is not None:
            self.gateway.close()
        self.conn.close()


def main() -> None:
    asyncio.run(Engine().run())
