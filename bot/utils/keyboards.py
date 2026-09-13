"""Inline keyboard builders.

Design rules (keep the bot feeling clean and professional):
* one screen = one job, never more than ~8 buttons unless it is a list
* primary action first, destructive action last
* every screen has a single obvious "back" target and a 🏠 escape hatch
* lists are *tappable*: a row opens the item, no IDs to type
* the currently selected option is marked with ``▸`` / ``✓`` so state is visible
"""
from __future__ import annotations

from datetime import datetime, timezone

from aiogram.types import (
    CopyTextButton,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder

from bot.services.database import Chat, InviteLink, Member, PendingJoin
from bot.utils.ui import clip, page_label, short_delta, urgency

# Full list of presets (used by "more options" screens)
DURATION_PRESETS: tuple[tuple[str, str], ...] = (
    ("1 week", "1w"),
    ("2 weeks", "2w"),
    ("1 month", "1m"),
    ("2 months", "2m"),
    ("3 months", "3m"),
    ("6 months", "6m"),
    ("1 year", "1y"),
    ("♾ Lifetime", "never"),
)
_PRESET_LABEL = dict((v, k) for k, v in DURATION_PRESETS)

# Short list shown on first screens; the default value is filtered out so the
# quick row never duplicates the "approve with default" button.
QUICK_DURATIONS: tuple[str, ...] = ("1m", "3m", "1y", "6m", "1w", "never")

#: Member list views: key → (label, icon). Order = tab order.
MEMBER_VIEWS: tuple[tuple[str, str, str], ...] = (
    ("active", "Active", "🟢"),
    ("soon", "Expiring", "⏰"),
    ("lifetime", "Lifetime", "♾"),
    ("past", "Past", "📁"),
)

#: Grace-period steps (hours) the ⏱ Grace button cycles through in Advanced.
GRACE_STEPS: tuple[int, ...] = (0, 12, 24, 72)

#: Telegram caps button labels at 64 chars; keep names short so counters fit.
BTN_NAME = 26


def _btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def _url(text: str, url: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, url=url)


def _copy(text: str, payload: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, copy_text=CopyTextButton(text=payload[:256]))


def onoff(flag: bool) -> str:
    return "✅" if flag else "❌"


def grace_label(hours: int) -> str:
    """``off`` · ``12h`` · ``3d`` — compact label for the grace-period switch."""
    if hours <= 0:
        return "off"
    if hours % 24 == 0:
        days = hours // 24
        return f"{days}d"
    return f"{hours}h"


def next_grace(current: int) -> int:
    """Next value in :data:`GRACE_STEPS`; custom values jump back to the first step."""
    try:
        idx = GRACE_STEPS.index(current)
    except ValueError:
        return GRACE_STEPS[0]
    return GRACE_STEPS[(idx + 1) % len(GRACE_STEPS)]


def preset_label(value: str) -> str:
    return _PRESET_LABEL.get(value, value)


def quick_durations(exclude: str | None, count: int = 3) -> list[str]:
    out = [v for v in QUICK_DURATIONS if v != exclude]
    return out[:count]


def _nav(*buttons: InlineKeyboardButton) -> list[InlineKeyboardButton]:
    return list(buttons)


def _home() -> InlineKeyboardButton:
    return _btn("🏠", "home")


# ------------------------------------------------------------ reply keyboard
# Persistent bottom menu shown in the private chat. It mirrors the most used
# inline actions so an admin can reach any screen with a single tap, even when
# the last inline message has scrolled away.
#
# Button labels double as the routing key (Telegram sends them back as plain
# text), so keep them unique and stable. ``MENU_*`` constants are the single
# source of truth for both the keyboard and the message handler.
MENU_CHATS = "📂 My chats"
MENU_DASHBOARD = "📊 Dashboard"
MENU_MEMBERS = "👥 Members"
MENU_PENDING = "🔔 Pending"
MENU_INVITES = "🔗 Invite links"
MENU_SETTINGS = "⚙️ Settings"
MENU_HELP = "📖 Help"
MENU_STATUS = "📇 My memberships"
MENU_ADD = "➕ Add to group"

#: Every label the reply-keyboard router must react to.
MENU_BUTTONS: frozenset[str] = frozenset(
    {
        MENU_CHATS,
        MENU_DASHBOARD,
        MENU_MEMBERS,
        MENU_PENDING,
        MENU_INVITES,
        MENU_SETTINGS,
        MENU_HELP,
        MENU_STATUS,
        MENU_ADD,
    }
)


def _kbtn(text: str) -> KeyboardButton:
    return KeyboardButton(text=text)


def main_menu_keyboard(is_admin: bool, has_chats: bool) -> ReplyKeyboardMarkup:
    """Persistent bottom menu for the private chat.

    * regular user   → My memberships · Help
    * admin, no chat → Add to group · Help
    * admin + chats  → Dashboard / Members / Pending / Invite links / Settings / My chats / Help
    """
    kb = ReplyKeyboardBuilder()
    if is_admin and has_chats:
        kb.row(_kbtn(MENU_DASHBOARD), _kbtn(MENU_MEMBERS))
        kb.row(_kbtn(MENU_PENDING), _kbtn(MENU_INVITES))
        kb.row(_kbtn(MENU_SETTINGS), _kbtn(MENU_CHATS))
        kb.row(_kbtn(MENU_HELP))
        placeholder = "Tap a button, or send a user ID / @username / name"
    elif is_admin:
        kb.row(_kbtn(MENU_ADD), _kbtn(MENU_HELP))
        placeholder = "Add me to a group to get started"
    else:
        kb.row(_kbtn(MENU_STATUS), _kbtn(MENU_HELP))
        placeholder = "Pick an action"
    return kb.as_markup(resize_keyboard=True, is_persistent=True, input_field_placeholder=placeholder)


def remove_reply_keyboard() -> ReplyKeyboardRemove:
    """Hide the bottom menu (e.g. when a user is no longer an admin anywhere)."""
    return ReplyKeyboardRemove(remove_keyboard=True)


def menu_key(text: str | None) -> str | None:
    """Normalise a tapped reply-button label to its ``MENU_*`` constant.

    Tolerates stray whitespace. Returns ``None`` for anything that is not a
    menu button so ordinary text (IDs, @usernames, custom durations) passes
    through untouched.
    """
    if not text:
        return None
    label = " ".join(text.split())
    return label if label in MENU_BUTTONS else None


def add_to_group_keyboard(bot_username: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(_url("➕ Add to a group", f"https://t.me/{bot_username}?startgroup=true&admin=restrict_members+invite_users"))
    kb.row(_url("📢 Add to a channel", f"https://t.me/{bot_username}?startchannel=true&admin=restrict_members+invite_users"))
    return kb.as_markup()


# ------------------------------------------------------------------ home / help
def home_keyboard(is_admin: bool, bot_username: str, has_chats: bool) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    if is_admin and has_chats:
        kb.row(_btn("📂 My chats", "chats"))
    kb.row(
        _url("➕ Add to group", f"https://t.me/{bot_username}?startgroup=true&admin=restrict_members+invite_users"),
        _btn("📖 Help", "help:main"),
    )
    if not is_admin:
        kb.row(_btn("📇 My memberships", "mystatus"))
    return kb.as_markup()


def help_keyboard(topic: str = "main") -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    if topic == "main":
        kb.row(_btn("🚀 Getting started", "help:setup"), _btn("👥 Members", "help:members"))
        kb.row(_btn("🔗 Invite links", "help:invites"), _btn("⚙️ Settings", "help:settings"))
        kb.row(_btn("⌨️ Commands & formats", "help:commands"))
        kb.row(_btn("🏠 Home", "home"))
    else:
        kb.row(_btn("◀️ Help topics", "help:main"), _btn("🏠 Home", "home"))
    return kb.as_markup()


def open_private_keyboard(bot_username: str, text: str = "💬 Open dashboard") -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(_url(text, f"https://t.me/{bot_username}?start=menu"))
    return kb.as_markup()


# ------------------------------------------------------------------ chat pick
def chats_keyboard(
    chats: list[Chat], current: int | None, badges: dict[int, tuple[int, int]] | None = None
) -> InlineKeyboardMarkup:
    """One row per chat. ``badges`` = {chat_id: (active_members, pending)} adds live counters."""
    kb = InlineKeyboardBuilder()
    for chat in chats:
        prefix = "▸ " if chat.chat_id == current else ""
        icon = "📢" if chat.is_channel else "👥"
        state = "" if chat.tracking_enabled else " · ⏸"
        extra = ""
        if badges and chat.chat_id in badges:
            active, pending = badges[chat.chat_id]
            extra = f" · {active}"
            if pending:
                extra += f" · 🔔{pending}"
        kb.row(_btn(f"{prefix}{icon} {clip(chat.display, 30)}{state}{extra}", f"dash:{chat.chat_id}"))
    kb.row(_btn("🏠 Home", "home"))
    return kb.as_markup()


# ------------------------------------------------------------------ dashboard
def dashboard_keyboard(chat: Chat, pending: int = 0, expiring: int = 0, many_chats: bool = True) -> InlineKeyboardMarkup:
    cid = chat.chat_id
    pend = f"🔔 Pending · {pending}" if pending else "🔔 Pending"
    soon = f"⏰ Expiring · {expiring}" if expiring else "⏰ Expiring"
    kb = InlineKeyboardBuilder()
    kb.row(_btn("👥 Members", f"list:{cid}:active:0"), _btn(soon, f"list:{cid}:soon:0"))
    kb.row(_btn(pend, f"pending:{cid}"), _btn("🔗 Invite links", f"invites:{cid}"))
    kb.row(_btn("📊 Overview", f"stats:{cid}"), _btn("📜 Activity", f"logs:{cid}"))
    kb.row(_btn("⚙️ Settings", f"settings:{cid}"), _btn("🛠 Tools", f"tools:{cid}"))
    if many_chats:
        kb.row(_btn("◀️ Chats", "chats"), _home())
    else:
        kb.row(_btn("🏠 Home", "home"))
    return kb.as_markup()


def tools_keyboard(chat: Chat) -> InlineKeyboardMarkup:
    """Rarely used, powerful actions — kept off the dashboard to keep it calm."""
    cid = chat.chat_id
    kb = InlineKeyboardBuilder()
    kb.row(_btn("➕ Add member", f"addm:{cid}"), _btn("🔍 Search", f"search:{cid}"))
    kb.row(_btn("📣 Broadcast", f"bcast:{cid}"), _btn("🛡 VIP list", f"vips:{cid}"))
    kb.row(_btn("🔄 Sync with Telegram", f"sync:{cid}"), _btn("🔁 Run expiry check", f"fcheck:{cid}"))
    kb.row(_btn("📥 Export CSV", f"export:{cid}"), _btn("🔐 Check permissions", f"perms:{cid}"))
    kb.row(_btn("◀️ Dashboard", f"dash:{cid}"), _home())
    return kb.as_markup()


def export_keyboard(chat_id: int, counts: dict[str, int] | None = None) -> InlineKeyboardMarkup:
    """Scope picker for the CSV export: everyone, or one of the member views."""
    kb = InlineKeyboardBuilder()
    total = sum((counts or {}).get(k, 0) for k in ("active", "past"))
    kb.row(_btn(f"📥 Everyone · {total}" if counts else "📥 Everyone", f"exportv:{chat_id}:all"))
    buttons = []
    for key, label, icon in MEMBER_VIEWS:
        n = (counts or {}).get(key)
        buttons.append(_btn(f"{icon} {label}" + (f" · {n}" if n is not None else ""), f"exportv:{chat_id}:{key}"))
    kb.row(*buttons[:2])
    kb.row(*buttons[2:])
    kb.row(_btn("◀️ Tools", f"tools:{chat_id}"), _home())
    return kb.as_markup()


def settings_keyboard(chat: Chat, default_duration: str, ask_default: bool) -> InlineKeyboardMarkup:
    """Main settings: only the switches an owner touches regularly."""
    cid = chat.chat_id
    ask_state = ask_default if chat.ask_on_join is None else chat.ask_on_join
    duration = chat.default_duration or default_duration
    kb = InlineKeyboardBuilder()
    kb.row(_btn(f"⏳ Duration · {preset_label(duration)}", f"set:{cid}:duration"))
    kb.row(
        _btn(f"{onoff(chat.tracking_enabled)} Tracking", f"set:{cid}:tracking"),
        _btn(f"{onoff(chat.auto_kick)} Auto-remove", f"set:{cid}:autokick"),
    )
    kb.row(
        _btn(f"{onoff(ask_state)} Ask on join", f"set:{cid}:ask"),
        _btn(f"{'🔨 Ban' if chat.kick_mode == 'ban' else '👢 Kick'} mode", f"set:{cid}:mode"),
    )
    kb.row(_btn("🔧 Advanced", f"adv:{cid}"))
    kb.row(_btn("◀️ Dashboard", f"dash:{cid}"), _home())
    return kb.as_markup()


def advanced_keyboard(chat: Chat) -> InlineKeyboardMarkup:
    cid = chat.chat_id
    approve = {0: "🙋 Requests · ignore", 1: "🙋 Requests · auto-approve", 2: "🙋 Requests · ask me"}.get(
        chat.approve_requests, "🙋 Requests · ignore"
    )
    kb = InlineKeyboardBuilder()
    kb.row(
        _btn(f"{onoff(chat.notify_user)} DM members", f"set:{cid}:notify"),
        _btn(f"{onoff(chat.welcome_enabled)} Welcome", f"set:{cid}:welcome"),
    )
    kb.row(_btn(approve, f"set:{cid}:approve"))
    kb.row(_btn(f"🔔 Prompts → {'👑 Owner' if chat.ask_target == 'owner' else '👮 All admins'}", f"set:{cid}:asktarget"))
    kb.row(
        _btn(f"⏱ Grace · {grace_label(chat.grace_hours)}", f"set:{cid}:grace"),
        _btn(f"{onoff(chat.digest_enabled)} Daily digest", f"set:{cid}:digest"),
    )
    kb.row(_btn("✏️ Welcome text", f"edit:{cid}:welcome"), _btn("📨 Log channel", f"edit:{cid}:log"))
    kb.row(_btn("◀️ Settings", f"settings:{cid}"), _home())
    return kb.as_markup()


def duration_keyboard(chat_id: int, current: str | None = None) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for label, value in DURATION_PRESETS:
        mark = "✓ " if current == value else ""
        kb.button(text=f"{mark}{label}", callback_data=f"dur:{chat_id}:{value}")
    kb.adjust(3)
    kb.row(_btn("✏️ Custom", f"dur:{chat_id}:custom"), _btn("🌐 Global default", f"dur:{chat_id}:global"))
    kb.row(_btn("◀️ Settings", f"settings:{chat_id}"))
    return kb.as_markup()


def edit_text_keyboard(chat_id: int, kind: str, has_value: bool) -> InlineKeyboardMarkup:
    """Keyboard for the welcome-text / log-channel editors."""
    kb = InlineKeyboardBuilder()
    if has_value:
        kb.row(_btn("🗑 Clear", f"editclr:{chat_id}:{kind}"))
    if kind == "log":
        kb.row(_btn("📨 Use this chat", f"editlog:{chat_id}:here"))
    kb.row(_btn("◀️ Advanced", f"adv:{chat_id}"))
    return kb.as_markup()


# ---------------------------------------------------------------- join prompt
def join_prompt_keyboard(
    pending_id: int, default_value: str = "1m", is_request: bool = False
) -> InlineKeyboardMarkup:
    """Compact prompt: approve with default, three quick picks, more / remove."""
    kb = InlineKeyboardBuilder()
    verb = "Approve" if is_request else "Keep"
    kb.row(_btn(f"✅ {verb} · {preset_label(default_value)}", f"jd:{pending_id}:default"))
    kb.row(*[_btn(preset_label(v), f"jd:{pending_id}:{v}") for v in quick_durations(default_value)])
    kb.row(
        _btn("⋯ More options", f"jm:{pending_id}"),
        _btn("🚫 Reject" if is_request else "🚫 Remove", f"jr:{pending_id}"),
    )
    return kb.as_markup()


def join_more_keyboard(pending_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for label, value in DURATION_PRESETS:
        kb.button(text=label, callback_data=f"jd:{pending_id}:{value}")
    kb.adjust(4)
    kb.row(_btn("✏️ Custom date / duration", f"jc:{pending_id}"))
    kb.row(_btn("◀️ Back", f"jb:{pending_id}"))
    return kb.as_markup()


def join_remove_confirm_keyboard(pending_id: int, is_request: bool = False) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(
        _btn("✅ Yes, reject" if is_request else "✅ Yes, remove", f"jd:{pending_id}:remove"),
        _btn("◀️ Back", f"jb:{pending_id}"),
    )
    return kb.as_markup()


def cancel_keyboard(data: str = "cancel", label: str = "❌ Cancel") -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(_btn(label, data))
    return kb.as_markup()


# ---------------------------------------------------------------- member card
def member_keyboard(
    chat_id: int, user_id: int, is_active: bool, back_view: str = "active", back_page: int = 0
) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(
        _btn("+1 week", f"ext:{chat_id}:{user_id}:1w"),
        _btn("+1 month", f"ext:{chat_id}:{user_id}:1m"),
        _btn("+3 months", f"ext:{chat_id}:{user_id}:3m"),
    )
    kb.row(
        _btn("♾ Lifetime", f"ext:{chat_id}:{user_id}:never"),
        _btn("✏️ Custom", f"cust:{chat_id}:{user_id}"),
        _btn("⋯ More", f"more:{chat_id}:{user_id}"),
    )
    kb.row(_btn("◀️ Members", f"list:{chat_id}:{back_view}:{back_page}"), _btn("📊", f"dash:{chat_id}"))
    return kb.as_markup()


def member_more_keyboard(chat_id: int, user_id: int, is_active: bool, whitelisted: bool) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(_btn("+6 months", f"ext:{chat_id}:{user_id}:6m"), _btn("+1 year", f"ext:{chat_id}:{user_id}:1y"))
    kb.row(
        _btn("📜 History", f"hist:{chat_id}:{user_id}"),
        _btn("📝 Note", f"note:{chat_id}:{user_id}"),
        _btn("🔔 Ask owner", f"askm:{chat_id}:{user_id}"),
    )
    kb.row(_btn("🛡 Unprotect" if whitelisted else "🛡 Protect (VIP)", f"wl:{chat_id}:{user_id}"))
    if is_active:
        kb.row(_btn("🚫 Remove from chat", f"kick:{chat_id}:{user_id}"))
    kb.row(_btn("🗑 Stop tracking", f"untrack:{chat_id}:{user_id}"))
    kb.row(_btn("◀️ Back", f"member:{chat_id}:{user_id}"))
    return kb.as_markup()


def _member_row_label(m: Member, now: datetime) -> str:
    glyph = urgency(m.expires_at, now)
    if m.expires_at is None:
        tail = "∞"
    elif m.status != "active":
        tail = m.status
    else:
        tail = short_delta(m.expires_at - now)
    name = clip(m.full_name or (f"@{m.username}" if m.username else str(m.user_id)), BTN_NAME)
    return f"{glyph} {name} · {tail}"


def list_keyboard(
    chat_id: int,
    view: str,
    page: int,
    total_pages: int,
    members: list[Member],
    counts: dict[str, int] | None = None,
    query: str | None = None,
) -> InlineKeyboardMarkup:
    """Tappable member list with view tabs, pager and search."""
    now = datetime.now(timezone.utc)
    kb = InlineKeyboardBuilder()
    # tabs (current one marked, counts inline)
    tabs = []
    for key, label, icon in MEMBER_VIEWS:
        n = (counts or {}).get(key)
        txt = f"{icon} {label}" + (f" {n}" if n is not None else "")
        if key == view and not query:
            txt = f"▸ {txt}"
        tabs.append(_btn(txt, f"list:{chat_id}:{key}:0"))
    kb.row(*tabs[:2])
    kb.row(*tabs[2:])
    # rows
    for m in members:
        kb.row(_btn(_member_row_label(m, now), f"member:{chat_id}:{m.user_id}:{view}:{page}"))
    # pager
    if total_pages > 1:
        nav: list[InlineKeyboardButton] = []
        nav.append(_btn("◀️", f"list:{chat_id}:{view}:{page - 1}") if page > 0 else _btn("·", "noop"))
        nav.append(_btn(page_label(page, total_pages), "noop"))
        nav.append(_btn("▶️", f"list:{chat_id}:{view}:{page + 1}") if page < total_pages - 1 else _btn("·", "noop"))
        kb.row(*nav)
    if query:
        kb.row(_btn("✖️ Clear search", f"list:{chat_id}:active:0"), _btn("🔍 Search again", f"search:{chat_id}"))
    else:
        kb.row(_btn("🔍 Search", f"search:{chat_id}"), _btn("➕ Add member", f"addm:{chat_id}"))
    kb.row(_btn("◀️ Dashboard", f"dash:{chat_id}"), _home())
    return kb.as_markup()


def back_keyboard(chat_id: int, label: str = "◀️ Dashboard") -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(_btn(label, f"dash:{chat_id}"), _home())
    return kb.as_markup()


def stats_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(_btn("⏰ Expiring soon", f"list:{chat_id}:soon:0"), _btn("📁 Past members", f"list:{chat_id}:past:0"))
    kb.row(_btn("🔄 Refresh", f"stats:{chat_id}"))
    kb.row(_btn("◀️ Dashboard", f"dash:{chat_id}"), _home())
    return kb.as_markup()


def logs_keyboard(chat_id: int, page: int, has_next: bool) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(_btn("◀️ Newer", f"logs:{chat_id}:{page - 1}"))
    if has_next:
        nav.append(_btn("Older ▶️", f"logs:{chat_id}:{page + 1}"))
    if nav:
        kb.row(*nav)
    kb.row(_btn("◀️ Dashboard", f"dash:{chat_id}"), _home())
    return kb.as_markup()


def confirm_keyboard(action: str, chat_id: int, user_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(
        _btn("✅ Confirm", f"{action}c:{chat_id}:{user_id}"),
        _btn("❌ Cancel", f"member:{chat_id}:{user_id}"),
    )
    return kb.as_markup()


def pending_keyboard(chat_id: int, items: list[PendingJoin]) -> InlineKeyboardMarkup:
    """One tappable row per waiting member plus a bulk 'keep default for all'."""
    kb = InlineKeyboardBuilder()
    for p in items[:10]:
        icon = "🙋" if p.source == "request" else "👤"
        kb.row(_btn(f"{icon} {clip(p.full_name or str(p.user_id), BTN_NAME + 6)}", f"jb:{p.id}"))
    if len(items) > 1:
        kb.row(_btn(f"✅ Default for all ({len(items)})", f"pall:{chat_id}"))
    kb.row(_btn("🔄 Refresh", f"pending:{chat_id}"))
    kb.row(_btn("◀️ Dashboard", f"dash:{chat_id}"), _home())
    return kb.as_markup()


def pending_all_confirm_keyboard(chat_id: int, count: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(_btn(f"✅ Yes, apply default to {count}", f"pallc:{chat_id}"), _btn("◀️ Back", f"pending:{chat_id}"))
    return kb.as_markup()


# --------------------------------------------------------------- invite links
def invites_keyboard(chat: Chat, links: list[InviteLink]) -> InlineKeyboardMarkup:
    cid = chat.chat_id
    kb = InlineKeyboardBuilder()
    kb.row(_btn("➕ New link", f"inpick:{cid}"))
    for link in links[:8]:
        label = clip(link.name or preset_label(link.duration), 18)
        kb.row(
            _copy(f"📋 {label} · {link.uses}", link.invite_link),
            _btn("🗑", f"irev:{cid}:{link.invite_link[-22:]}"),
        )
    kb.row(_btn("◀️ Dashboard", f"dash:{cid}"), _home())
    return kb.as_markup()


def invite_created_keyboard(chat_id: int, url: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(_copy("📋 Copy link", url))
    kb.row(_url("📤 Share", f"https://t.me/share/url?url={url}"))
    kb.row(_btn("◀️ Invite links", f"invites:{chat_id}"))
    return kb.as_markup()


def invite_pick_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for label, value in DURATION_PRESETS:
        kb.button(text=label, callback_data=f"inew:{chat_id}:{value}")
    kb.adjust(4)
    kb.row(_btn("✏️ Custom duration + label", f"incust:{chat_id}"))
    kb.row(_btn("◀️ Back", f"invites:{chat_id}"))
    return kb.as_markup()


def invite_revoke_confirm_keyboard(chat_id: int, suffix: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(_btn("✅ Yes, revoke", f"irevc:{chat_id}:{suffix}"), _btn("◀️ Back", f"invites:{chat_id}"))
    return kb.as_markup()


# ------------------------------------------------------------------- tools
def broadcast_confirm_keyboard(chat_id: int, count: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(_btn(f"📣 Send to {count}", f"bcastc:{chat_id}"), _btn("❌ Cancel", f"tools:{chat_id}"))
    return kb.as_markup()


def vips_keyboard(chat_id: int, members: list[Member], ids: list[int]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    known = {m.user_id: m for m in members}
    for uid in ids[:12]:
        m = known.get(uid)
        label = clip(m.full_name, BTN_NAME) if m and m.full_name else str(uid)
        kb.row(_btn(f"🛡 {label}", f"member:{chat_id}:{uid}"))
    kb.row(_btn("◀️ Tools", f"tools:{chat_id}"), _home())
    return kb.as_markup()


def mystatus_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(_btn("🔄 Refresh", "mystatus"), _btn("🏠 Home", "home"))
    return kb.as_markup()
