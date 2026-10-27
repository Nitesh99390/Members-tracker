"""CSV export of tracked members.

Pure helpers (no I/O) so they are trivial to unit-test:

* :data:`EXPORT_VIEWS` — the scopes an admin can pick (mirrors the member views).
* :func:`members_csv` — render a list of :class:`~bot.services.database.Member`
  rows to UTF-8 CSV bytes (with a BOM so Excel opens it correctly).
* :func:`export_filename` / :func:`export_caption` — cosmetic helpers used by
  both the ``/export`` command and the inline ``exportv:`` button.
"""
from __future__ import annotations

import csv
import io
import re
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from bot.services.database import Chat, Database, Member
from bot.utils.timeparse import humanize_delta

#: ``view key`` → human label. Order = order shown in captions / usage text.
EXPORT_VIEWS: tuple[tuple[str, str], ...] = (
    ("all", "Everyone"),
    ("active", "Active"),
    ("soon", "Expiring soon"),
    ("lifetime", "Lifetime"),
    ("past", "Past"),
)

#: Column order of the generated spreadsheet.
CSV_COLUMNS: tuple[str, ...] = (
    "user_id",
    "full_name",
    "username",
    "status",
    "joined_at",
    "expires_at",
    "remaining",
    "source",
    "renewals",
    "added_by",
    "note",
)

_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


def _iso_local(dt: datetime | None, tz: ZoneInfo) -> str:
    """``2025-12-31 18:30`` in the configured timezone, or ``""``."""
    if dt is None:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(tz).strftime("%Y-%m-%d %H:%M")


def _remaining(member: Member, now: datetime) -> str:
    if member.status != "active":
        return ""
    if member.expires_at is None:
        return "lifetime"
    return humanize_delta(member.expires_at - now)


def member_row(member: Member, tz: ZoneInfo, now: datetime | None = None) -> list[str]:
    """One CSV row for ``member`` in :data:`CSV_COLUMNS` order."""
    now = now or datetime.now(timezone.utc)
    return [
        str(member.user_id),
        member.full_name or "",
        f"@{member.username}" if member.username else "",
        member.status,
        _iso_local(member.joined_at, tz),
        _iso_local(member.expires_at, tz),
        _remaining(member, now),
        member.source or "",
        str(member.renewals),
        str(member.added_by) if member.added_by else "",
        (member.note or "").replace("\r", " ").replace("\n", " "),
    ]


def members_csv(members: list[Member], tz: ZoneInfo, now: datetime | None = None) -> bytes:
    """Render ``members`` to UTF-8 CSV bytes with a BOM (Excel-friendly)."""
    now = now or datetime.now(timezone.utc)
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\r\n")
    writer.writerow(CSV_COLUMNS)
    for m in members:
        writer.writerow(member_row(m, tz, now))
    return ("\ufeff" + buf.getvalue()).encode("utf-8")


async def members_csv_stream(
    members: AsyncIterator[Member], tz: ZoneInfo, now: datetime | None = None
) -> tuple[bytes, int]:
    """Like :func:`members_csv` but consumes an async iterator row by row.

    Only the compact CSV text accumulates (~100 bytes/row); ``Member``
    dataclasses are created and discarded one at a time, so exporting a chat
    with 200k members costs a few MB instead of hundreds. Returns
    ``(csv_bytes, row_count)``.
    """
    now = now or datetime.now(timezone.utc)
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\r\n")
    writer.writerow(CSV_COLUMNS)
    count = 0
    async for m in members:
        writer.writerow(member_row(m, tz, now))
        count += 1
    return ("\ufeff" + buf.getvalue()).encode("utf-8"), count


async def export_chat_csv(db: Database, chat_id: int, view: str, tz: ZoneInfo) -> tuple[bytes, int]:
    """Stream ``view`` members of ``chat_id`` from SQLite (100-row batches) straight into CSV."""
    return await members_csv_stream(db.iter_members_for_export(chat_id, view), tz)


def view_label(view: str) -> str:
    return dict(EXPORT_VIEWS).get(view, view)


def export_filename(chat: Chat, view: str, now: datetime | None = None) -> str:
    """``members_MyGroup_active_2025-01-15.csv`` — safe for every OS."""
    now = now or datetime.now(timezone.utc)
    title = _SAFE_NAME.sub("_", chat.display).strip("_") or str(chat.chat_id)
    return f"members_{title[:40]}_{view}_{now:%Y-%m-%d}.csv"


def export_caption(chat: Chat, view: str, count: int) -> str:
    """Plain-text caption (caller HTML-escapes it)."""
    noun = "member" if count == 1 else "members"
    return f"📥 {chat.display} — {view_label(view)}: {count} {noun}"
