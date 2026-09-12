"""Parsing of human-friendly durations and dates.

Supported duration formats (case-insensitive, may be combined):
    30d, 1m (month), 2w, 12h, 45min, 1y, "1m 15d", "2 weeks", "3 months"
Supported absolute date formats:
    2025-12-31, 31-12-2025, 31/12/2025, 2025-12-31 18:30, 31/12/2025 18:30
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

# unit -> (kind, multiplier). "months"/"years" handled with calendar arithmetic.
_UNITS: dict[str, tuple[str, int]] = {
    "min": ("seconds", 60),
    "mins": ("seconds", 60),
    "minute": ("seconds", 60),
    "minutes": ("seconds", 60),
    "h": ("seconds", 3600),
    "hr": ("seconds", 3600),
    "hrs": ("seconds", 3600),
    "hour": ("seconds", 3600),
    "hours": ("seconds", 3600),
    "d": ("seconds", 86400),
    "day": ("seconds", 86400),
    "days": ("seconds", 86400),
    "w": ("seconds", 7 * 86400),
    "wk": ("seconds", 7 * 86400),
    "week": ("seconds", 7 * 86400),
    "weeks": ("seconds", 7 * 86400),
    "m": ("months", 1),
    "mo": ("months", 1),
    "mon": ("months", 1),
    "month": ("months", 1),
    "months": ("months", 1),
    "y": ("months", 12),
    "yr": ("months", 12),
    "year": ("months", 12),
    "years": ("months", 12),
}

_TOKEN_RE = re.compile(r"(\d+)\s*([a-zA-Z]+)")

_DATE_FORMATS = (
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d",
    "%d-%m-%Y %H:%M",
    "%d-%m-%Y",
    "%d/%m/%Y %H:%M",
    "%d/%m/%Y",
    "%Y/%m/%d %H:%M",
    "%Y/%m/%d",
)


class ParseError(ValueError):
    """Raised when a duration or date string cannot be parsed."""


def add_months(dt: datetime, months: int) -> datetime:
    """Add calendar months to a datetime, clamping the day to month end."""
    month_index = dt.month - 1 + months
    year = dt.year + month_index // 12
    month = month_index % 12 + 1
    # days in target month
    if month == 12:
        next_month = datetime(year + 1, 1, 1, tzinfo=dt.tzinfo)
    else:
        next_month = datetime(year, month + 1, 1, tzinfo=dt.tzinfo)
    last_day = (next_month - timedelta(days=1)).day
    return dt.replace(year=year, month=month, day=min(dt.day, last_day))


def parse_duration(text: str) -> tuple[int, int]:
    """Parse a duration string.

    Returns ``(months, seconds)``; months are applied with calendar arithmetic,
    the rest as exact seconds. Raises ``ParseError`` on bad input.
    """
    text = text.strip().lower()
    if not text:
        raise ParseError("Empty duration")
    # bare number -> days
    if text.isdigit():
        return 0, int(text) * 86400

    pos = 0
    months = 0
    seconds = 0
    matched_any = False
    for match in _TOKEN_RE.finditer(text):
        # ensure only whitespace between tokens
        between = text[pos:match.start()]
        if between.strip(" ,"):
            raise ParseError(f"Unexpected text: {between!r}")
        value = int(match.group(1))
        unit = match.group(2)
        if unit not in _UNITS:
            raise ParseError(f"Unknown unit: {unit!r}")
        kind, mult = _UNITS[unit]
        if kind == "months":
            months += value * mult
        else:
            seconds += value * mult
        matched_any = True
        pos = match.end()
    if not matched_any or text[pos:].strip(" ,"):
        raise ParseError(f"Cannot parse duration: {text!r}")
    if months == 0 and seconds == 0:
        raise ParseError("Duration must be greater than zero")
    return months, seconds


def apply_duration(start: datetime, text: str) -> datetime:
    """Return ``start`` shifted by the parsed duration."""
    months, seconds = parse_duration(text)
    result = start
    if months:
        result = add_months(result, months)
    if seconds:
        result = result + timedelta(seconds=seconds)
    return result


def parse_date(text: str, tz: ZoneInfo) -> datetime:
    """Parse an absolute date (interpreted in ``tz``) and return a UTC datetime.

    A date without time is interpreted as end of that day (23:59) in ``tz``.
    """
    text = text.strip()
    for fmt in _DATE_FORMATS:
        try:
            naive = datetime.strptime(text, fmt)
        except ValueError:
            continue
        if "%H" not in fmt:
            naive = naive.replace(hour=23, minute=59)
        return naive.replace(tzinfo=tz).astimezone(timezone.utc)
    raise ParseError(f"Cannot parse date: {text!r}")


def parse_expiry(text: str, tz: ZoneInfo, now: datetime | None = None) -> datetime:
    """Parse either a duration (relative to now) or an absolute date.

    Special keywords: ``never``/``forever``/``0`` -> raises ``ParseError`` so
    callers can handle permanence explicitly via :func:`is_permanent`.
    Returns a timezone-aware UTC datetime.
    """
    now = now or datetime.now(timezone.utc)
    text = text.strip()
    if is_permanent(text):
        raise ParseError("permanent")
    try:
        return parse_date(text, tz)
    except ParseError:
        pass
    result = apply_duration(now, text)
    if result <= now:
        raise ParseError("Expiry must be in the future")
    return result


def is_permanent(text: str) -> bool:
    return text.strip().lower() in {"never", "forever", "permanent", "lifetime", "0", "none"}


def humanize_delta(delta: timedelta) -> str:
    """Return a compact human string like ``"12d 4h"`` or ``"expired"``."""
    total = int(delta.total_seconds())
    if total <= 0:
        return "expired"
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes and not days:
        parts.append(f"{minutes}m")
    if not parts:
        parts.append("<1m")
    return " ".join(parts)


def format_dt(dt: datetime | None, tz: ZoneInfo) -> str:
    if dt is None:
        return "♾ Never"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(tz).strftime("%d %b %Y, %H:%M")
