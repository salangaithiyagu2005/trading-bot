from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from nsebot.clock import IST
from nsebot.types import Bar, Tick


class CandleBuilder:
    def __init__(self, token: int, timeframe_min: int):
        if timeframe_min not in (1, 5):
            raise ValueError("timeframe_min must be 1 or 5")
        self.token = token
        self.timeframe_min = timeframe_min
        self.timeframe = f"{timeframe_min}m"
        self._bar: Bar | None = None
        self._closed: set[datetime] = set()
        self._last_cumulative_volume: int | None = None

    def _bucket(self, timestamp: datetime) -> datetime:
        if timestamp.tzinfo is None or timestamp.utcoffset() != timedelta(hours=5, minutes=30):
            raise ValueError("tick timestamps must be timezone-aware IST")
        minute = (timestamp.minute // self.timeframe_min) * self.timeframe_min
        return timestamp.replace(minute=minute, second=0, microsecond=0)

    def _volume_delta(self, cumulative: int) -> int:
        previous = self._last_cumulative_volume if self._last_cumulative_volume is not None else 0
        self._last_cumulative_volume = cumulative
        if cumulative <= previous:
            return 0
        return cumulative - previous

    def _close_open(self, closed_by: str) -> list[Bar]:
        if self._bar is None:
            return []
        bar = self._bar
        closed = Bar(
            token=bar.token,
            timeframe=bar.timeframe,
            start=bar.start,
            end=bar.end,
            open=bar.open,
            high=bar.high,
            low=bar.low,
            close=bar.close,
            volume=bar.volume,
            closed_by=closed_by,
        )
        self._closed.add(bar.start)
        self._bar = None
        return [closed]

    def on_tick(self, tick: Tick) -> list[Bar]:
        if tick.token != self.token:
            raise ValueError("tick token does not match candle builder")
        if tick.ts.tzinfo is None or tick.ts.utcoffset() != timedelta(hours=5, minutes=30):
            raise ValueError("tick timestamps must be timezone-aware IST")
        if tick.prev_close is not None:
            self._prev_close = tick.prev_close
        session_open = tick.ts.replace(hour=9, minute=15, second=0, microsecond=0)
        session_close = tick.ts.replace(hour=15, minute=30, second=0, microsecond=0)
        if tick.ts < session_open or tick.ts >= session_close:
            return []
        delta = self._volume_delta(tick.volume_traded)
        start = self._bucket(tick.ts)
        if start in self._closed:
            return []
        closed: list[Bar] = []
        if self._bar is not None and start > self._bar.start:
            closed.extend(self._close_open("tick"))
        if self._bar is None:
            self._bar = Bar(
                token=self.token,
                timeframe=self.timeframe,
                start=start,
                end=start + timedelta(minutes=self.timeframe_min),
                open=tick.last,
                high=tick.last,
                low=tick.last,
                close=tick.last,
                volume=delta,
                closed_by="tick",
            )
        else:
            bar = self._bar
            self._bar = Bar(
                token=bar.token,
                timeframe=bar.timeframe,
                start=bar.start,
                end=bar.end,
                open=bar.open,
                high=max(bar.high, tick.last),
                low=min(bar.low, tick.last),
                close=tick.last,
                volume=bar.volume + delta,
                closed_by="tick",
            )
        return closed

    def on_timer(self, now: datetime) -> list[Bar]:
        if now.tzinfo is None or now.utcoffset() != timedelta(hours=5, minutes=30):
            raise ValueError("timer timestamps must be timezone-aware IST")
        if self._bar is None:
            return []
        grace = timedelta(milliseconds=500 if self.timeframe_min == 1 else 750)
        if now < self._bar.end + grace:
            return []
        return self._close_open("timer")

    def open_bar(self) -> Bar | None:
        return self._bar

    @property
    def prev_close(self) -> Decimal | None:
        return getattr(self, "_prev_close", None)


def discard_open_historical(bars: list[Bar], now: datetime) -> list[Bar]:
    latest: dict[tuple[int, str, datetime], Bar] = {}
    for bar in sorted(bars, key=lambda item: (item.start, item.token, item.timeframe)):
        if bar.end > now:
            continue
        latest[(bar.token, bar.timeframe, bar.start)] = bar
    return list(latest.values())
