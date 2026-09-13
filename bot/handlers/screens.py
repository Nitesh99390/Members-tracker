"""Screen builders: every admin-facing view as a ``(text, keyboard)`` pair.

A *screen* is what the admin sees after tapping a button. Building text and
keyboard together in one place guarantees that the command handlers
(``/members``), the bottom-menu router (``👥 Members``) and the inline
callbacks (``list:<cid>:active:0``) all render the exact same thing, and
that a screen never goes out of sync with its own buttons.

Rules of thumb for these renderers:

* text is compact HTML – headline, one blank line, a handful of lines;
* the keyboard carries the *actions*; the text only explains state;
* no Telegram I/O here, only DB reads and pure formatting, so screens are
  trivial to unit-test.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from html import escape

from aiogram.types import InlineKeyboardMarkup

from bot.config import Settings
from bot.services.database import Chat, Database, Member
from bot.utils import keyboards as K
from bot.utils.timeparse import describe_duration, format_dt, humanize_delta
from bot.utils.ui import SOON_DAYS, clip, page_label

#: A rendered screen: HTML text plus its inline keyboard.
Screen = tuple[str, InlineKeyboardMarkup]

#: Rows per page in the tappable member list (Telegram limits ~100 buttons,
#: and phones stay comfortable at 8 rows + tabs + pager + actions).
LIST_PAGE = 8
LOGS_PAGE = 15

_VIEW_TITLE = {key: (icon, label) for key, label, icon in K.MEMBER_VIEWS}


def _soon(now: datetime | None = None) -> datetime:
    return (now or datetime.now(timezone.utc)) + timedelta(days=SOON_DAYS)


def _icon(chat: Chat) -> str:
    return "📢" if chat.is_channel else "👥"


# ------------------------------------------------------------------- chats
async def chats_screen(db: Database, chats: list[Chat], current: int | None) -> Screen:
    """Chat picker with live badges (active members · pending prompts)."""
    badges: dict[int, tuple[int, int]] = {}
    for chat in chats[:30]:
        badges[chat.chat_id] = (
            await db.count_members(chat.chat_id, "active"),
            await db.count_pending(chat.chat_id),
        )
    total_pending = sum(p for _, p in badges.values())
    lines = ["📂 <b>Your chats</b>", ""]
    if total_pending:
        lines.append(f"🔔 <b>{total_pending}</b> member{'s' if total_pending != 1 else ''} waiting for a decision.")
    lines.append("<i>Tap a chat to open its dashboard.</i>")
    return "\n".join(lines), K.chats_keyboard(chats, current, badges)


# --------------------------------------------------------------- dashboard
async def dashboard_text(db: Database, chat: Chat, settings: Settings) -> str:
    duration = chat.default_duration or settings.default_duration
    s = await db.member_stats(chat.chat_id)
    now = datetime.now(timezone.utc)
    soon_7d = await db.expiring_within_count(chat.chat_id, _soon(now))
    pending = await db.count_pending(chat.chat_id)
    status = "" if chat.tracking_enabled else "\n⏸ <i>Tracking is paused</i>"
    attention = ""
    if pending:
        attention += f"\n🔔 <b>{pending}</b> waiting for your decision"
    if soon_7d:
        attention += f"\n⏰ <b>{soon_7d}</b> expiring within {SOON_DAYS} days"
    if not chat.auto_kick:
        attention += "\n⚠️ <i>Auto-remove is off — expired members stay</i>"
    return (
        f"{_icon(chat)} <b>{escape(chat.display)}</b>{status}\n\n"
        f"🟢 Active members: <b>{s.get('active', 0)}</b>\n"
        f"⏳ Default duration: <b>{escape(describe_duration(duration))}</b>"
        f"{attention}"
    )


async def dashboard_screen(db: Database, chat: Chat, settings: Settings, many_chats: bool = True) -> Screen:
    text = await dashboard_text(db, chat, settings)
    pending = await db.count_pending(chat.chat_id)
    expiring = await db.expiring_within_count(chat.chat_id, _soon())
    return text, K.dashboard_keyboard(chat, pending, expiring, many_chats)


def tools_screen(chat: Chat) -> Screen:
    text = (
        f"🛠 <b>Tools — {escape(chat.display)}</b>\n\n"
        "Powerful, less frequent actions.\n"
        "<i>Add / search members, message everyone, review VIPs, or re-check "
        "Telegram and expiries on demand.</i>"
    )
    return text, K.tools_keyboard(chat)


# ---------------------------------------------------------------- settings
def settings_text(chat: Chat, settings: Settings) -> str:
    duration = chat.default_duration or settings.default_duration
    ask = settings.ask_on_join_default if chat.ask_on_join is None else chat.ask_on_join
    who = "the owner" if chat.ask_target == "owner" else "all admins"
    join_line = (
        f"New members: <b>{escape(describe_duration(duration))}</b>, {who} get a one-tap prompt to change it."
        if ask
        else f"New members: <b>{escape(describe_duration(duration))}</b>, applied silently."
    )
    return (
        f"⚙️ <b>Settings — {escape(chat.display)}</b>\n\n"
        f"{join_line}\n"
        f"Expired members are {'<b>removed automatically</b>' if chat.auto_kick else '<b>kept</b> (auto-remove off)'} "
        f"({'ban' if chat.kick_mode == 'ban' else 'kick, can rejoin'}).\n\n"
        f"<i>Tap a switch to change it.</i>"
    )


def settings_screen(chat: Chat, settings: Settings) -> Screen:
    return settings_text(chat, settings), K.settings_keyboard(
        chat, settings.default_duration, settings.ask_on_join_default
    )


def advanced_screen(chat: Chat) -> Screen:
    approve = {0: "ignored", 1: "auto-approved", 2: "you are asked"}.get(chat.approve_requests, "ignored")
    welcome = "custom text" if chat.welcome_text else "default text"
    log = f"<code>{chat.log_chat_id}</code>" if chat.log_chat_id else "off"
    text = (
        f"🔧 <b>Advanced — {escape(chat.display)}</b>\n\n"
        f"💬 Member DMs (reminders, notices): <b>{'on' if chat.notify_user else 'off'}</b>\n"
        f"👋 Welcome message: <b>{'on' if chat.welcome_enabled else 'off'}</b> · {welcome}\n"
        f"🙋 Join requests: <b>{approve}</b>\n"
        f"🔔 Prompts go to: <b>{'the owner' if chat.ask_target == 'owner' else 'all admins'}</b>\n"
        f"📨 Log channel: <b>{log}</b>"
    )
    return text, K.advanced_keyboard(chat)


def duration_screen(chat: Chat, settings: Settings) -> Screen:
    current = chat.default_duration or settings.default_duration
    origin = "chat-specific" if chat.default_duration else "global default"
    text = (
        f"⏳ <b>Default duration — {escape(chat.display)}</b>\n\n"
        f"Currently <b>{escape(describe_duration(current))}</b> ({origin}).\n"
        "<i>Applies to members who join from now on; existing members keep their expiry.</i>"
    )
    return text, K.duration_keyboard(chat.chat_id, chat.default_duration)


def edit_text_screen(chat: Chat, kind: str) -> Screen:
    if kind == "welcome":
        current = (
            f"\n\nCurrent:\n<i>{escape(chat.welcome_text)}</i>" if chat.welcome_text else "\n\n<i>Using the default text.</i>"
        )
        text = (
            f"✏️ <b>Welcome text — {escape(chat.display)}</b>{current}\n\n"
            "Send the new message. Placeholders: <code>{mention}</code> <code>{name}</code> "
            "<code>{expires}</code> <code>{chat}</code>"
        )
        has_value = bool(chat.welcome_text)
    else:
        current = f"\n\nCurrent: <code>{chat.log_chat_id}</code>" if chat.log_chat_id else "\n\n<i>Not set.</i>"
        text = (
            f"📨 <b>Log channel — {escape(chat.display)}</b>{current}\n\n"
            "Send the chat ID where I should post the audit log (I must be an admin there), "
            "<code>here</code> for this chat, or <code>off</code>."
        )
        has_value = chat.log_chat_id is not None
    return text, K.edit_text_keyboard(chat.chat_id, kind, has_value)


# ----------------------------------------------------------------- members
def _member_line(m: Member, now: datetime, tz) -> str:
    if m.expires_at is None:
        when = "♾ lifetime"
    elif m.status != "active":
        when = m.status
    else:
        when = f"{humanize_delta(m.expires_at - now)} · {format_dt(m.expires_at, tz)}"
    return f"• {m.mention_html} · <code>{m.user_id}</code> · {escape(when)}"


async def members_screen(
    db: Database,
    chat: Chat,
    settings: Settings,
    view: str = "active",
    page: int = 0,
    query: str | None = None,
) -> Screen:
    """Tabbed, paged, tappable member list (optionally narrowed by ``query``)."""
    if view not in db.MEMBER_FILTERS:
        view = "active"
    now = datetime.now(timezone.utc)
    soon = _soon(now)
    lookup_view = "all" if query else view
    total = (await db.list_members_view(chat.chat_id, lookup_view, 1, 0, query=query, soon=soon))[1]
    total_pages = max(1, (total + LIST_PAGE - 1) // LIST_PAGE)
    page = min(max(0, page), total_pages - 1)
    members, _ = await db.list_members_view(
        chat.chat_id, lookup_view, LIST_PAGE, page * LIST_PAGE, query=query, soon=soon
    )
    counts = await db.member_view_counts(chat.chat_id, soon)

    if query:
        title = f"🔍 <b>Search “{escape(clip(query, 32))}” — {escape(chat.display)}</b>"
    else:
        icon, label = _VIEW_TITLE.get(view, ("👥", "Members"))
        title = f"{icon} <b>{label} members — {escape(chat.display)}</b>"
    lines = [f"{title} · {total}", ""]
    if not members:
        if query:
            lines.append("<i>No matches. Try another name, @username or ID.</i>")
        elif view == "soon":
            lines.append(f"<i>Nobody expires within the next {SOON_DAYS} days.</i>")
        elif view == "past":
            lines.append("<i>No past members yet.</i>")
        elif view == "lifetime":
            lines.append("<i>No lifetime members. Use ♾ on a member card to grant one.</i>")
        else:
            lines.append("<i>No active members tracked yet. They appear as soon as someone joins.</i>")
    else:
        for m in members:
            lines.append(_member_line(m, now, settings.tz))
        if total_pages > 1:
            lines.append("")
            lines.append(f"<i>Page {page_label(page, total_pages)} · tap a row to open the member.</i>")
        else:
            lines.append("")
            lines.append("<i>Tap a row to open the member.</i>")
    markup = K.list_keyboard(chat.chat_id, view, page, total_pages, members, counts, query)
    return "\n".join(lines), markup


async def vips_screen(db: Database, chat: Chat) -> Screen:
    ids = await db.list_whitelist(chat.chat_id)
    members: list[Member] = []
    for uid in ids[:12]:
        m = await db.get_member(chat.chat_id, uid)
        if m:
            members.append(m)
    lines = [f"🛡 <b>VIP / protected — {escape(chat.display)}</b> · {len(ids)}", ""]
    if not ids:
        lines.append("<i>Nobody is protected yet. Open a member → ⋯ More → 🛡 Protect.</i>")
    else:
        lines.append("<i>These members are never removed automatically. Tap one to manage.</i>")
    return "\n".join(lines), K.vips_keyboard(chat.chat_id, members, ids)


# ------------------------------------------------------------------- stats
async def stats_text(db: Database, chat: Chat, settings: Settings) -> str:
    s = await db.member_stats(chat.chat_id)
    now = datetime.now(timezone.utc)
    soon_24 = await db.expiring_within_count(chat.chat_id, now + timedelta(hours=24))
    soon_7d = await db.expiring_within_count(chat.chat_id, _soon(now))
    wl = len(await db.list_whitelist(chat.chat_id))
    pending = await db.count_pending(chat.chat_id)
    return (
        f"📊 <b>Overview — {escape(chat.display)}</b>\n\n"
        f"🟢 Active: <b>{s.get('active', 0)}</b> · joined today: {s.get('joined_today', 0)}\n"
        f"♾ Lifetime: <b>{s.get('permanent', 0)}</b> · 🛡 VIP: <b>{wl}</b>\n"
        f"🔔 Awaiting decision: <b>{pending}</b>\n\n"
        f"⏰ Expiring in 24h: <b>{soon_24}</b>\n"
        f"📅 Expiring in {SOON_DAYS} days: <b>{soon_7d}</b>\n\n"
        f"⌛ Expired: {s.get('expired', 0)} · 🚫 Removed: {s.get('manual', 0)} · "
        f"⚪ Left: {s.get('left', 0)} · 🔴 Kicked: {s.get('kicked', 0)}\n\n"
        f"<i>{format_dt(now, settings.tz)} · {settings.timezone}</i>"
    )


async def stats_screen(db: Database, chat: Chat, settings: Settings) -> Screen:
    return await stats_text(db, chat, settings), K.stats_keyboard(chat.chat_id)


# -------------------------------------------------------------------- logs
async def logs_text(db: Database, chat: Chat, settings: Settings, page: int = 0) -> tuple[str, bool]:
    rows = await db.recent_logs_page(chat.chat_id, LOGS_PAGE + 1, page * LOGS_PAGE)
    has_next = len(rows) > LOGS_PAGE
    rows = rows[:LOGS_PAGE]
    head = f"📜 <b>Activity — {escape(chat.display)}</b>"
    if not rows:
        body = "<i>Nothing recorded yet.</i>" if page == 0 else "<i>No older entries.</i>"
        return f"{head}\n\n{body}", False
    lines = [head + (f" · page {page + 1}" if page else ""), ""]
    for r in rows:
        ts = format_dt(datetime.fromisoformat(r["created_at"]), settings.tz)
        who = f"<code>{r['user_id']}</code> " if r["user_id"] else ""
        det = f" <i>{escape(str(r['details']))[:40]}</i>" if r["details"] else ""
        lines.append(f"• {ts} — <b>{escape(r['action'])}</b> {who}{det}")
    return "\n".join(lines), has_next


async def logs_screen(db: Database, chat: Chat, settings: Settings, page: int = 0) -> Screen:
    text, has_next = await logs_text(db, chat, settings, page)
    return text, K.logs_keyboard(chat.chat_id, page, has_next)


# ----------------------------------------------------------------- pending
async def pending_screen(db: Database, chat: Chat, settings: Settings) -> Screen:
    items = await db.list_pending(chat.chat_id, limit=20)
    lines = [f"🔔 <b>Pending — {escape(chat.display)}</b> · {len(items)}", ""]
    if not items:
        lines.append("<i>Nothing waiting. New joins will appear here until you answer their prompt.</i>")
    else:
        now = datetime.now(settings.tz)
        for p in items[:10]:
            age = humanize_delta(now - p.created_at.astimezone(settings.tz))
            kind = "requested to join" if p.source == "request" else "joined"
            lines.append(f"• {p.mention_html} · <code>{p.user_id}</code> · {kind} {age} ago")
        if len(items) > 10:
            lines.append(f"<i>…and {len(items) - 10} more</i>")
        lines.append("")
        lines.append("<i>Tap a name to decide, or apply the default to everyone at once.</i>")
    return "\n".join(lines), K.pending_keyboard(chat.chat_id, items)


# ------------------------------------------------------------ invite links
async def invites_screen(db: Database, chat: Chat) -> Screen:
    links = await db.list_invite_links(chat.chat_id)
    lines = [f"🔗 <b>Invite links — {escape(chat.display)}</b> · {len(links)}", ""]
    if not links:
        lines.append(
            "Anyone joining through a link gets its preset duration automatically — "
            "no prompt, no manual work.\n\n<i>Tap ➕ New link to create one.</i>"
        )
    else:
        for link in links[:8]:
            lines.append(
                f"<b>{escape(link.name or K.preset_label(link.duration))}</b> · "
                f"{escape(describe_duration(link.duration))} · {link.uses} joined"
            )
        lines.append("")
        lines.append("<i>📋 copies a link · 🗑 revokes it.</i>")
    return "\n".join(lines), K.invites_keyboard(chat, links)


def invite_created_screen(chat: Chat, url: str, name: str, duration: str) -> Screen:
    text = (
        f"🔗 <b>Invite link created — {escape(chat.display)}</b>\n\n"
        f"📛 {escape(name)} · ⏳ <b>{escape(describe_duration(duration))}</b>\n\n"
        f"<code>{escape(url)}</code>\n\n"
        "<i>Everyone joining through this link gets that duration automatically.</i>"
    )
    return text, K.invite_created_keyboard(chat.chat_id, url)
