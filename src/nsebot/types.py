from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal


class Side(enum.Enum):
    LONG_CALL = "LONG_CALL"
    LONG_PUT = "LONG_PUT"


class Bias(enum.Enum):
    BULLISH = "BULLISH"
    BEARISH = "BEARISH"
    SIDEWAYS = "SIDEWAYS"


class State(enum.Enum):
    IDLE = "IDLE"
    BLOCKED = "BLOCKED"
    RESEARCH = "RESEARCH"
    WATCHING = "WATCHING"
    ARMED = "ARMED"
    ENTRY_WORKING = "ENTRY_WORKING"
    MANAGING = "MANAGING"
    DONE = "DONE"
    HALTED = "HALTED"


class Reason(enum.Enum):
    NO_TOKEN = "no_token"
    HEALTH_FAIL = "health_fail"
    HOLIDAY = "holiday"
    EXPIRY_DAY = "expiry_day"
    WARMUP_FAIL = "warmup_fail"
    BREADTH_INCOMPLETE = "breadth_incomplete"
    VIX_UNSTABLE = "vix_unstable"
    DEAD_RANGE = "dead_range"
    BIAS_SIDEWAYS = "bias_sideways"
    BIAS_BULLISH = "bias_bullish"
    BIAS_BEARISH = "bias_bearish"
    SECTOR_RANKED = "sector_ranked"
    NO_ELIGIBLE_STOCK = "no_eligible_stock"
    EXPIRY_WEEK_SKIP = "expiry_week_skip"
    ARMED = "armed"
    CONFIRM_FAILED = "confirm_failed"
    WINDOW_CLOSED = "window_closed"
    LIQUIDITY_REJECT = "liquidity_reject"
    PREMIUM_TOO_SMALL = "premium_too_small"
    RISK_CAP = "risk_cap"
    ORDER_SUBMITTED = "order_submitted"
    NOT_FILLED = "not_filled"
    ORDER_FILLED = "order_filled"
    STOP_PLACED = "stop_placed"
    TARGET_PLACED = "target_placed"
    STOP_HIT = "stop_hit"
    TARGET_HIT = "target_hit"
    STRUCTURE_EXIT = "structure_exit"
    TIME_FLATTEN = "time_flatten"
    RECONCILE_MISMATCH = "reconcile_mismatch"
    KILL_SWITCH = "kill_switch"
    BLIND = "blind"
    ORDER_REJECTED = "order_rejected"
    SESSION_DONE = "session_done"
    REVIEW_SAVED = "review_saved"


@dataclass(frozen=True)
class Bar:
    token: int
    timeframe: str
    start: datetime
    end: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    closed_by: str


@dataclass(frozen=True)
class Tick:
    token: int
    ts: datetime
    last: Decimal
    volume_traded: int
    bid: Decimal | None = None
    ask: Decimal | None = None
    prev_close: Decimal | None = None
    day_open: Decimal | None = None
    day_open: Decimal | None = None
