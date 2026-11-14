from __future__ import annotations

import csv
import io
import os
import threading
import time
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Callable

from nsebot.clock import IST
from nsebot.types import Bar, Tick


class OrderError(RuntimeError):
    pass


class Gateway:
    def __init__(self, api_key: str, access_token: str, kite_client: Any | None = None):
        if kite_client is None:
            from kiteconnect import KiteConnect

            kite_client = KiteConnect(api_key=api_key)
            kite_client.set_access_token(access_token)
        self._kite = kite_client
        self._historical_lock = threading.Lock()
        self._quote_lock = threading.Lock()
        self._historical_last = 0.0
        self._quote_last = 0.0
        self._ticker = None
        self.dropped_ticks = 0

    def profile(self) -> dict:
        return self._kite.profile()

    def margins_equity(self) -> dict:
        return self._kite.margins("equity")

    def instruments_csv(self, exchange: str | None = None) -> bytes:
        instruments = self._kite.instruments(exchange) if exchange else self._kite.instruments()
        if not instruments:
            return b""
        stream = io.StringIO(newline="")
        writer = csv.DictWriter(stream, fieldnames=list(instruments[0].keys()))
        writer.writeheader()
        writer.writerows(instruments)
        return stream.getvalue().encode("utf-8")

    def _pace(self, lock: threading.Lock, last_attr: str, interval: float) -> None:
        with lock:
            elapsed = time.monotonic() - getattr(self, last_attr)
            if elapsed < interval:
                time.sleep(interval - elapsed)
            setattr(self, last_attr, time.monotonic())

    def historical(
        self,
        token: int,
        start: datetime,
        end: datetime,
        interval: str,
    ) -> list[Bar]:
        interval_map = {
            "minute": ("minute", "1m"),
            "5minute": ("5minute", "5m"),
            "15minute": ("15minute", "15m"),
        }
        try:
            kite_interval, timeframe = interval_map[interval]
        except KeyError as exc:
            raise ValueError(f"Unsupported historical interval: {interval}") from exc
        if start.tzinfo is None or end.tzinfo is None or end <= start:
            raise ValueError("historical range must be timezone-aware and increasing")
        self._pace(self._historical_lock, "_historical_last", 0.5)
        rows = self._kite.historical_data(
            instrument_token=token,
            from_date=start,
            to_date=end,
            interval=kite_interval,
        )
        bars: list[Bar] = []
        width = timedelta(minutes=int(timeframe[:-1]))
        for row in rows:
            timestamp = row["date"]
            if timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=IST)
            else:
                timestamp = timestamp.astimezone(IST)
            bars.append(Bar(
                token=token,
                timeframe=timeframe,
                start=timestamp,
                end=timestamp + width,
                open=Decimal(str(row["open"])),
                high=Decimal(str(row["high"])),
                low=Decimal(str(row["low"])),
                close=Decimal(str(row["close"])),
                volume=int(row["volume"]),
                closed_by="historical",
            ))
        return bars

    def quote(self, keys: list[str]) -> dict:
        result: dict = {}
        for offset in range(0, len(keys), 200):
            self._pace(self._quote_lock, "_quote_last", 1.0)
            result.update(self._kite.quote(keys[offset:offset + 200]))
        return result

    def _require_live(self) -> None:
        if os.environ.get("LIVE_TRADING", "false").strip().lower() != "true":
            raise OrderError("LIVE_TRADING is not true; order request refused")

    def _order_call(self, method: str, *args, **kwargs):
        self._require_live()
        try:
            return getattr(self._kite, method)(*args, **kwargs)
        except Exception as exc:
            raise OrderError(str(exc)) from exc

    def place(self, **kwargs) -> str:
        return self._order_call("place_order", **kwargs)

    def modify(self, order_id: str, variety: str = "regular", **kwargs) -> str:
        return self._order_call("modify_order", variety, order_id, **kwargs)

    def cancel(self, order_id: str, variety: str = "regular") -> None:
        self._order_call("cancel_order", variety, order_id)

    def orders(self) -> list[dict]:
        return self._kite.orders()

    def positions(self) -> dict:
        return self._kite.positions()

    def connect_ticker(
        self,
        on_ticks: Callable,
        on_connect: Callable,
        on_close: Callable,
        on_error: Callable,
    ) -> None:
        from kiteconnect import KiteTicker

        ticker = KiteTicker(
            api_key=self._kite.api_key,
            access_token=self._kite.access_token,
        )

        def ticks_handler(ws, packets):
            converted: list[Tick] = []
            for packet in packets:
                timestamp = packet.get("exchange_timestamp")
                if timestamp is None:
                    self.dropped_ticks += 1
                    continue
                if timestamp.tzinfo is None:
                    timestamp = timestamp.replace(tzinfo=IST)
                else:
                    timestamp = timestamp.astimezone(IST)
                depth = packet.get("depth") or {}
                buys = depth.get("buy") or []
                sells = depth.get("sell") or []
                ohlc = packet.get("ohlc") or {}
                converted.append(Tick(
                    token=int(packet["instrument_token"]),
                    ts=timestamp,
                    last=Decimal(str(packet["last_price"])),
                    volume_traded=int(packet.get("volume_traded", 0)),
                    bid=Decimal(str(buys[0]["price"])) if buys else None,
                    ask=Decimal(str(sells[0]["price"])) if sells else None,
                    prev_close=Decimal(str(ohlc["close"])) if ohlc.get("close") is not None else None,
                    day_open=Decimal(str(ohlc["open"])) if ohlc.get("open") is not None else None,
                ))
            if converted:
                on_ticks(ws, converted)

        ticker.on_ticks = ticks_handler
        ticker.on_connect = on_connect
        ticker.on_close = on_close
        ticker.on_error = on_error
        self._ticker = ticker
        ticker.connect(threaded=True)

    def subscribe(self, tokens: list[int], mode: str) -> None:
        if self._ticker is None:
            raise RuntimeError("ticker is not connected")
        mode_value = {"quote": self._ticker.MODE_QUOTE, "full": self._ticker.MODE_FULL}.get(mode)
        if mode_value is None:
            raise ValueError("mode must be quote or full")
        self._ticker.subscribe(tokens)
        self._ticker.set_mode(mode_value, tokens)

    def close(self) -> None:
        if self._ticker is not None:
            self._ticker.close()
            self._ticker = None
