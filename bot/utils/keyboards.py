"""Inline keyboard builders.

Design rules (keep the bot feeling clean and professional):
* one screen = one job, never more than ~6 buttons unless it is a list
* primary action first, destructive action last
* every screen has a single obvious "back" target
"""
from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.services.database import Chat, InviteLink

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


def _btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def _url(text: str, url: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, url=url)


def onoff(flag: bool) -> str:
    return "✅" if flag else "❌"


def preset_label(value: str) -> str:
    return _PRESET_LABEL.get(value, value)


def quick_durations(exclude: str | None, count: int = 3) -> list[str]:
    out = [v for v in QUICK_DURATIONS if v != exclude]
    return out[:count]


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
def chats_keyboard(chats: list[Chat], current: int | None) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for chat in chats:
        prefix = "▸ " if chat.chat_id == current else ""
        icon = "📢" if chat.is_channel else "👥"
        state = "" if chat.tracking_enabled else " · paused"
        kb.row(_btn(f"{prefix}{icon} {chat.display[:36]}{state}", f"dash:{chat.chat_id}"))
    kb.row(_btn("🏠 Home", "home"))
    return kb.as_markup()


# ------------------------------------------------------------------ dashboard
def dashboard_keyboard(chat: Chat, pending: int = 0) -> InlineKeyboardMarkup:
    cid = chat.chat_id
    pend = f"🔔 Pending ({pending})" if pending else "🔔 Pending"
    kb = InlineKeyboardBuilder()
    kb.row(_btn("👥 Members", f"list:{cid}:0"), _btn("📊 Overview", f"stats:{cid}"))
    kb.row(_btn(pend, f"pending:{cid}"), _btn("🔗 Invite links", f"invites:{cid}"))
    kb.row(_btn("⚙️ Settings", f"settings:{cid}"), _btn("📜 Activity", f"logs:{cid}"))
    kb.row(_btn("◀️ Chats", "chats"))
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
    kb.row(_btn("🔧 Advanced", f"adv:{cid}"), _btn("◀️ Dashboard", f"dash:{cid}"))
    return kb.as_markup()


def advanced_keyboard(chat: Chat) -> InlineKeyboardMarkup:
    cid = chat.chat_id
    approve = {0: "❌ Join requests", 1: "✅ Auto-approve requests", 2: "🔔 Ask on requests"}.get(
        chat.approve_requests, "❌ Join requests"
    )
    kb = InlineKeyboardBuilder()
    kb.row(
        _btn(f"{onoff(chat.notify_user)} DM members", f"set:{cid}:notify"),
        _btn(f"{onoff(chat.welcome_enabled)} Welcome", f"set:{cid}:welcome"),
    )
    kb.row(_btn(approve, f"set:{cid}:approve"))
    kb.row(_btn(f"Prompts → {'👑 Owner' if chat.ask_target == 'owner' else '👮 All admins'}", f"set:{cid}:asktarget"))
    kb.row(_btn("◀️ Settings", f"settings:{cid}"))
    return kb.as_markup()


def duration_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for label, value in DURATION_PRESETS:
        kb.button(text=label, callback_data=f"dur:{chat_id}:{value}")
    kb.adjust(3)
    kb.row(_btn("🌐 Global default", f"dur:{chat_id}:global"), _btn("◀️ Back", f"settings:{chat_id}"))
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


def cancel_keyboard(data: str = "cancel") -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(_btn("❌ Cancel", data))
    return kb.as_markup()


# ---------------------------------------------------------------- member card
def member_keyboard(chat_id: int, user_id: int, is_active: bool) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(
        _btn("+1 month", f"ext:{chat_id}:{user_id}:1m"),
        _btn("+3 months", f"ext:{chat_id}:{user_id}:3m"),
        _btn("♾ Lifetime", f"ext:{chat_id}:{user_id}:never"),
    )
    kb.row(_btn("✏️ Custom", f"cust:{chat_id}:{user_id}"), _btn("⋯ More", f"more:{chat_id}:{user_id}"))
    kb.row(_btn("◀️ Members", f"list:{chat_id}:0"))
    return kb.as_markup()


def member_more_keyboard(chat_id: int, user_id: int, is_active: bool, whitelisted: bool) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(_btn("+1 week", f"ext:{chat_id}:{user_id}:1w"), _btn("+6 months", f"ext:{chat_id}:{user_id}:6m"))
    kb.row(
        _btn("📜 History", f"hist:{chat_id}:{user_id}"),
        _btn("🛡 Unprotect" if whitelisted else "🛡 Protect (VIP)", f"wl:{chat_id}:{user_id}"),
    )
    if is_active:
        kb.row(_btn("🚫 Remove from chat", f"kick:{chat_id}:{user_id}"))
    kb.row(_btn("🗑 Stop tracking", f"untrack:{chat_id}:{user_id}"))
    kb.row(_btn("◀️ Back", f"member:{chat_id}:{user_id}"))
    return kb.as_markup()


def list_keyboard(chat_id: int, page: int, has_next: bool) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(_btn("◀️", f"list:{chat_id}:{page - 1}"))
    nav.append(_btn(f"Page {page + 1}", "noop"))
    if has_next:
        nav.append(_btn("▶️", f"list:{chat_id}:{page + 1}"))
    kb.row(*nav)
    kb.row(_btn("◀️ Dashboard", f"dash:{chat_id}"))
    return kb.as_markup()


def back_keyboard(chat_id: int, label: str = "◀️ Dashboard") -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(_btn(label, f"dash:{chat_id}"))
    return kb.as_markup()


def confirm_keyboard(action: str, chat_id: int, user_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(
        _btn("✅ Confirm", f"{action}c:{chat_id}:{user_id}"),
        _btn("❌ Cancel", f"member:{chat_id}:{user_id}"),
    )
    return kb.as_markup()


def pending_keyboard(chat_id: int, pending_ids: list[int]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for pid in pending_ids[:12]:
        kb.button(text=f"#{pid}", callback_data=f"jb:{pid}")
    kb.adjust(4)
    kb.row(_btn("◀️ Dashboard", f"dash:{chat_id}"))
    return kb.as_markup()


# --------------------------------------------------------------- invite links
def invites_keyboard(chat: Chat, links: list[InviteLink]) -> InlineKeyboardMarkup:
    cid = chat.chat_id
    kb = InlineKeyboardBuilder()
    kb.row(_btn("➕ New link", f"inpick:{cid}"))
    for link in links[:8]:
        kb.row(
            _btn(
                f"🗑 {(link.name or preset_label(link.duration))[:24]} · {link.uses} joined",
                f"irev:{cid}:{link.invite_link[-22:]}",
            )
        )
    kb.row(_btn("◀️ Dashboard", f"dash:{cid}"))
    return kb.as_markup()


def invite_pick_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for label, value in DURATION_PRESETS:
        kb.button(text=label, callback_data=f"inew:{chat_id}:{value}")
    kb.adjust(4)
    kb.row(_btn("◀️ Back", f"invites:{chat_id}"))
    return kb.as_markup()
