"""Tiny presentation helpers shared by keyboards and screen texts.

Everything here is pure (no I/O) so it can be unit-tested in isolation and
reused from both the inline-keyboard builders and the HTML screen renderers.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

#: Thresholds for the urgency glyph shown in front of a member row.
URGENT_HOURS = 24
SOON_DAYS = 7


def clip(text: str | None, limit: int, ellipsis: str = "…") -> str:
    """Trim ``text`` to ``limit`` characters, appending ``ellipsis`` when cut.

    Whitespace is normalised so multi-line names never break a button label.
    """
    if not text:
        return ""
    flat = " ".join(str(text).split())
    if len(flat) <= limit:
        return flat
    if limit <= len(ellipsis):
        return flat[:limit]
    return flat[: limit - len(ellipsis)].rstrip() + ellipsis


def page_label(page: int, total_pages: int) -> str:
    """``"2 / 7"`` style pager label (``page`` is zero-based)."""
    total = max(1, total_pages)
    current = min(max(0, page), total - 1) + 1
    return f"{current} / {total}"


def short_delta(delta: timedelta) -> str:
    """Very compact remaining-time label for list rows: ``3d``, ``5h``, ``12m``, ``exp``.

    Anything at or below zero is ``"exp"`` (expired), a year or more is shown
    in years (``1y``, ``2y``) and 60+ days in months so long memberships stay
    readable inside a 64-char button.
    """
    total = int(delta.total_seconds())
    if total <= 0:
        return "exp"
    days, rem = divmod(total, 86400)
    if days >= 365:
        return f"{days // 365}y"
    if days >= 60:
        return f"{days // 30}mo"
    if days >= 1:
        return f"{days}d"
    hours, rem = divmod(rem, 3600)
    if hours >= 1:
        return f"{hours}h"
    minutes = max(1, rem // 60)
    return f"{minutes}m"


def urgency(expires_at: datetime | None, now: datetime | None = None) -> str:
    """Traffic-light glyph for a member's expiry.

    * ``♾``  lifetime (no expiry)
    * ``🔴`` already expired
    * ``🟠`` expires within :data:`URGENT_HOURS`
    * ``🟡`` expires within :data:`SOON_DAYS`
    * ``🟢`` comfortable
    """
    if expires_at is None:
        return "♾"
    now = now or datetime.now(timezone.utc)
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    left = expires_at - now
    if left <= timedelta(0):
        return "🔴"
    if left <= timedelta(hours=URGENT_HOURS):
        return "🟠"
    if left <= timedelta(days=SOON_DAYS):
        return "🟡"
    return "🟢"


def progress_bar(done: int, total: int, width: int = 10) -> str:
    """``▰▰▰▱▱▱▱▱▱▱`` style bar used by long-running actions (sync / broadcast)."""
    if total <= 0:
        return "▱" * width
    filled = min(width, round(width * done / total))
    return "▰" * filled + "▱" * (width - filled)
