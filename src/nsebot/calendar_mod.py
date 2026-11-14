from __future__ import annotations

from datetime import date
from pathlib import Path

from nsebot.clock import now_ist
from nsebot.rules import Rules
from nsebot.types import Reason


def is_weekend(day: date) -> bool:
    return day.weekday() >= 5


def _read_holidays(path: Path) -> set[date]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ValueError(f"Unable to read NSE holiday file: {path}") from exc
    holidays: set[date] = set()
    for line_number, raw in enumerate(lines, start=1):
        value = raw.strip()
        if not value or value.startswith("#"):
            continue
        try:
            parsed = date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError(f"Invalid holiday date on line {line_number}: {value!r}") from exc
        if parsed.isoformat() != value:
            raise ValueError(f"Holiday date must be YYYY-MM-DD on line {line_number}")
        holidays.add(parsed)
    if not holidays:
        raise ValueError("NSE holiday file contains no dates")
    return holidays


def is_holiday(day: date, path: Path) -> bool:
    return day in _read_holidays(path)


def previous_trading_day(day: date, path: Path) -> date:
    holidays = _read_holidays(path)
    candidate = day.fromordinal(day.toordinal() - 1)
    while is_weekend(candidate) or candidate in holidays:
        candidate = candidate.fromordinal(candidate.toordinal() - 1)
    return candidate


def session_allowed(day: date, rules: Rules, holidays: Path) -> tuple[bool, Reason | None]:
    if rules.calendar.skip_weekends and is_weekend(day):
        return False, Reason.HOLIDAY
    if rules.calendar.skip_exchange_holidays and is_holiday(day, holidays):
        return False, Reason.HOLIDAY
    return True, None
