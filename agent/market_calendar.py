"""Small deterministic U.S. equity calendar used when MCP has no clock tool."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import config


def _observed(day: date) -> date:
    if day.weekday() == 5:
        return day - timedelta(days=1)
    if day.weekday() == 6:
        return day + timedelta(days=1)
    return day


def _nth_weekday(year: int, month: int, weekday: int, ordinal: int) -> date:
    day = date(year, month, 1)
    day += timedelta(days=(weekday - day.weekday()) % 7 + 7 * (ordinal - 1))
    return day


def _last_weekday(year: int, month: int, weekday: int) -> date:
    first_next = date(year + (month == 12), month % 12 + 1, 1)
    day = first_next - timedelta(days=1)
    return day - timedelta(days=(day.weekday() - weekday) % 7)


def _easter(year: int) -> date:
    # Anonymous Gregorian algorithm.
    a, b = year % 19, year // 100
    c, d, e = year % 100, b // 4, b % 4
    f, g = (b + 8) // 25, (b - (b + 8) // 25 + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    return date(year, month, (h + l - 7 * m + 114) % 31 + 1)


def exchange_holidays(year: int) -> set[date]:
    holidays = {
        _observed(date(year, 1, 1)),
        _nth_weekday(year, 1, 0, 3),       # Martin Luther King Jr. Day
        _nth_weekday(year, 2, 0, 3),       # Washington's Birthday
        _easter(year) - timedelta(days=2), # Good Friday
        _last_weekday(year, 5, 0),         # Memorial Day
        _observed(date(year, 7, 4)),
        _nth_weekday(year, 9, 0, 1),       # Labor Day
        _nth_weekday(year, 11, 3, 4),      # Thanksgiving
        _observed(date(year, 12, 25)),
    }
    if year >= 2022:
        holidays.add(_observed(date(year, 6, 19)))
    # New Year's Day can be observed in the prior calendar year.
    holidays.add(_observed(date(year + 1, 1, 1)))
    return holidays


def early_close(day: date) -> time | None:
    thanksgiving = _nth_weekday(day.year, 11, 3, 4)
    if day == thanksgiving + timedelta(days=1):
        return time(13, 0)
    if day.month == 12 and day.day == 24 and day.weekday() < 5:
        return time(13, 0)
    if day.month == 7 and day.day == 3 and day.weekday() < 5:
        return time(13, 0)
    return None


def regular_session(now: datetime) -> tuple[str, bool, str]:
    """Return status, regular-session boolean, and explicit local source."""

    if now.tzinfo is None:
        raise ValueError("market-session time must be timezone-aware")
    local = now.astimezone(ZoneInfo(config.MARKET_TIMEZONE))
    day = local.date()
    if day.weekday() >= 5 or day in exchange_holidays(day.year):
        return "CLOSED", False, "LOCAL_NYSE_CALENDAR"
    opened = time(*config.REGULAR_MARKET_OPEN)
    closed = early_close(day) or time(*config.REGULAR_MARKET_CLOSE)
    active = opened <= local.time().replace(tzinfo=None) < closed
    return ("OPEN" if active else "CLOSED"), active, "LOCAL_NYSE_CALENDAR"
