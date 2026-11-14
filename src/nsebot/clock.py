from __future__ import annotations

from datetime import date, datetime, time
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")


def now_ist() -> datetime:
    return datetime.now(IST)


def parse_hhmm(value: str) -> time:
    try:
        parsed = time.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"Invalid HH:MM time: {value!r}") from exc
    if parsed.second or parsed.microsecond or parsed.tzinfo:
        raise ValueError(f"Expected HH:MM without seconds or timezone: {value!r}")
    return parsed


def combine(day: date, t: time) -> datetime:
    if t.tzinfo is not None:
        raise ValueError("time must not contain a timezone")
    return datetime.combine(day, t, tzinfo=IST)


def at(day: date, hhmm: str) -> datetime:
    return combine(day, parse_hhmm(hhmm))
