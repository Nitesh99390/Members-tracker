from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from bot.utils.timeparse import (
    ParseError,
    add_months,
    apply_duration,
    humanize_delta,
    is_permanent,
    parse_date,
    parse_duration,
    parse_expiry,
)

TZ = ZoneInfo("Asia/Kolkata")


def test_parse_duration_basic():
    assert parse_duration("30d") == (0, 30 * 86400)
    assert parse_duration("1m") == (1, 0)
    assert parse_duration("2w") == (0, 14 * 86400)
    assert parse_duration("12h") == (0, 12 * 3600)
    assert parse_duration("1y") == (12, 0)
    assert parse_duration("45") == (0, 45 * 86400)


def test_parse_duration_combined():
    assert parse_duration("1m 15d") == (1, 15 * 86400)
    assert parse_duration("2 weeks") == (0, 14 * 86400)
    assert parse_duration("3 months") == (3, 0)


@pytest.mark.parametrize("bad", ["", "abc", "10x", "0d", "1m foo"])
def test_parse_duration_invalid(bad):
    with pytest.raises(ParseError):
        parse_duration(bad)


def test_add_months_clamps_day():
    jan31 = datetime(2025, 1, 31, tzinfo=timezone.utc)
    assert add_months(jan31, 1) == datetime(2025, 2, 28, tzinfo=timezone.utc)
    assert add_months(jan31, 13) == datetime(2026, 2, 28, tzinfo=timezone.utc)
    dec = datetime(2025, 12, 15, tzinfo=timezone.utc)
    assert add_months(dec, 1) == datetime(2026, 1, 15, tzinfo=timezone.utc)


def test_apply_duration_one_month():
    start = datetime(2025, 3, 10, 12, 0, tzinfo=timezone.utc)
    assert apply_duration(start, "1m") == datetime(2025, 4, 10, 12, 0, tzinfo=timezone.utc)
    assert apply_duration(start, "1m 1d") == datetime(2025, 4, 11, 12, 0, tzinfo=timezone.utc)


def test_parse_date_formats():
    expected_local = datetime(2025, 12, 31, 23, 59, tzinfo=TZ)
    for text in ["2025-12-31", "31-12-2025", "31/12/2025", "2025/12/31"]:
        assert parse_date(text, TZ) == expected_local.astimezone(timezone.utc)
    with_time = parse_date("31/12/2025 18:30", TZ)
    assert with_time == datetime(2025, 12, 31, 18, 30, tzinfo=TZ).astimezone(timezone.utc)


def test_parse_expiry_relative_and_absolute():
    now = datetime(2025, 1, 1, tzinfo=timezone.utc)
    assert parse_expiry("10d", TZ, now) == now + timedelta(days=10)
    assert parse_expiry("2025-06-01", TZ, now) == datetime(2025, 6, 1, 23, 59, tzinfo=TZ).astimezone(
        timezone.utc
    )
    with pytest.raises(ParseError):
        parse_expiry("never", TZ, now)


def test_is_permanent():
    assert is_permanent("never") and is_permanent("Forever") and is_permanent("0")
    assert not is_permanent("30d")


def test_humanize_delta():
    assert humanize_delta(timedelta(days=12, hours=4)) == "12d 4h"
    assert humanize_delta(timedelta(hours=2, minutes=30)) == "2h 30m"
    assert humanize_delta(timedelta(seconds=-5)) == "expired"
    assert humanize_delta(timedelta(seconds=10)) == "<1m"


def test_describe_duration():
    from bot.utils.timeparse import describe_duration

    assert describe_duration("1m") == "1 month"
    assert describe_duration("3m") == "3 months"
    assert describe_duration("1y") == "1 year"
    assert describe_duration("14m") == "1 year 2 months"
    assert describe_duration("2w") == "2 weeks"
    assert describe_duration("30d") == "30 days"
    assert describe_duration("1m 15d") == "1 month 15 days"
    assert describe_duration("12h") == "12 hours"
    assert describe_duration("never").startswith("Lifetime")
    assert describe_duration("garbage") == "garbage"
