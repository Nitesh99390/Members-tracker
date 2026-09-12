"""Inline keyboard builders."""
from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.services.database import Chat, InviteLink

# Quick duration presets shown on join prompts / member cards
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


def onoff(flag: bool) -> str:
    return "✅ ON" if flag else "❌ OFF"


def chats_keyboard(chats: list[Chat], current: int | None) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for chat in chats:
        prefix = "🔹 " if chat.chat_id == current else ""
        icon = "📢" if chat.is_channel else "👥"
        state = "" if chat.tracking_enabled else " (paused)"
        kb.button(
            text=f"{prefix}{icon} {chat.display[:36]}{state}", callback_data=f"ctx:{chat.chat_id}"
        )
    kb.adjust(1)
    return kb.as_markup()


def settings_keyboard(chat: Chat, default_duration: str, ask_default: bool) -> InlineKeyboardMarkup:
    cid = chat.chat_id
    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(
            text=f"📡 Tracking: {onoff(chat.tracking_enabled)}", callback_data=f"set:{cid}:tracking"
        )
    )
    kb.row(
        InlineKeyboardButton(
            text=f"🥾 Auto-remove: {onoff(chat.auto_kick)}", callback_data=f"set:{cid}:autokick"
        ),
        InlineKeyboardButton(
            text=f"⚙️ Mode: {'🔨 Ban' if chat.kick_mode == 'ban' else '👢 Kick'}",
            callback_data=f"set:{cid}:mode",
        ),
    )
    ask_state = ask_default if chat.ask_on_join is None else chat.ask_on_join
    kb.row(
        InlineKeyboardButton(
            text=f"🔔 Ask on join: {onoff(ask_state)}", callback_data=f"set:{cid}:ask"
        ),
        InlineKeyboardButton(
            text=f"👤 Ask: {'👑 Owner' if chat.ask_target == 'owner' else '👮 All admins'}",
            callback_data=f"set:{cid}:asktarget",
        ),
    )
    kb.row(
        InlineKeyboardButton(
            text=f"📨 Notify user: {onoff(chat.notify_user)}", callback_data=f"set:{cid}:notify"
        ),
        InlineKeyboardButton(
            text=f"👋 Welcome: {onoff(chat.welcome_enabled)}", callback_data=f"set:{cid}:welcome"
        ),
    )
    approve_label = {0: "❌ OFF", 1: "✅ Auto", 2: "🔔 Ask admin"}.get(chat.approve_requests, "❌ OFF")
    kb.row(
        InlineKeyboardButton(
            text=f"🔓 Join requests: {approve_label}",
            callback_data=f"set:{cid}:approve",
        )
    )
    kb.row(
        InlineKeyboardButton(
            text=f"⏳ Duration: {chat.default_duration or default_duration + ' (global)'}",
            callback_data=f"set:{cid}:duration",
        )
    )
    kb.row(
        InlineKeyboardButton(text="📊 Stats", callback_data=f"stats:{cid}"),
        InlineKeyboardButton(text="📋 Members", callback_data=f"list:{cid}:0"),
        InlineKeyboardButton(text="📜 Logs", callback_data=f"logs:{cid}"),
    )
    kb.row(
        InlineKeyboardButton(text="🔔 Pending", callback_data=f"pending:{cid}"),
        InlineKeyboardButton(text="🔗 Invite links", callback_data=f"invites:{cid}"),
        InlineKeyboardButton(text="🔄 Refresh", callback_data=f"panel:{cid}"),
    )
    return kb.as_markup()


def duration_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for label, value in DURATION_PRESETS:
        kb.button(text=label, callback_data=f"dur:{chat_id}:{value}")
    kb.adjust(3)
    kb.row(
        InlineKeyboardButton(text="🌐 Use global default", callback_data=f"dur:{chat_id}:global"),
    )
    kb.row(InlineKeyboardButton(text="◀️ Back", callback_data=f"panel:{chat_id}"))
    return kb.as_markup()


def join_prompt_keyboard(pending_id: int) -> InlineKeyboardMarkup:
    """Buttons sent to the owner when someone joins."""
    kb = InlineKeyboardBuilder()
    for label, value in DURATION_PRESETS:
        kb.button(text=label, callback_data=f"jd:{pending_id}:{value}")
    kb.adjust(3)
    kb.row(
        InlineKeyboardButton(text="✅ Keep default", callback_data=f"jd:{pending_id}:default"),
        InlineKeyboardButton(text="✏️ Custom", callback_data=f"jc:{pending_id}"),
    )
    kb.row(InlineKeyboardButton(text="🚫 Remove member", callback_data=f"jr:{pending_id}"))
    return kb.as_markup()


def join_remove_confirm_keyboard(pending_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(text="✅ Yes, remove", callback_data=f"jd:{pending_id}:remove"),
        InlineKeyboardButton(text="◀️ Back", callback_data=f"jb:{pending_id}"),
    )
    return kb.as_markup()


def cancel_keyboard(data: str = "cancel") -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="❌ Cancel", callback_data=data))
    return kb.as_markup()


def member_keyboard(chat_id: int, user_id: int, is_active: bool) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(text="+7d", callback_data=f"ext:{chat_id}:{user_id}:7d"),
        InlineKeyboardButton(text="+1m", callback_data=f"ext:{chat_id}:{user_id}:1m"),
        InlineKeyboardButton(text="+3m", callback_data=f"ext:{chat_id}:{user_id}:3m"),
        InlineKeyboardButton(text="♾", callback_data=f"ext:{chat_id}:{user_id}:never"),
    )
    kb.row(
        InlineKeyboardButton(text="✏️ Set custom", callback_data=f"cust:{chat_id}:{user_id}"),
        InlineKeyboardButton(text="📜 History", callback_data=f"hist:{chat_id}:{user_id}"),
    )
    if is_active:
        kb.row(
            InlineKeyboardButton(text="🚫 Remove now", callback_data=f"kick:{chat_id}:{user_id}"),
            InlineKeyboardButton(text="🛡 Whitelist", callback_data=f"wl:{chat_id}:{user_id}"),
        )
    kb.row(InlineKeyboardButton(text="◀️ Members", callback_data=f"list:{chat_id}:0"))
    return kb.as_markup()


def list_keyboard(chat_id: int, page: int, has_next: bool) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️ Prev", callback_data=f"list:{chat_id}:{page - 1}"))
    if has_next:
        nav.append(InlineKeyboardButton(text="Next ▶️", callback_data=f"list:{chat_id}:{page + 1}"))
    if nav:
        kb.row(*nav)
    kb.row(InlineKeyboardButton(text="⚙️ Settings", callback_data=f"panel:{chat_id}"))
    return kb.as_markup()


def back_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="◀️ Settings", callback_data=f"panel:{chat_id}"))
    return kb.as_markup()


def confirm_keyboard(action: str, chat_id: int, user_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(text="✅ Confirm", callback_data=f"{action}c:{chat_id}:{user_id}"),
        InlineKeyboardButton(text="❌ Cancel", callback_data=f"member:{chat_id}:{user_id}"),
    )
    return kb.as_markup()


def pending_keyboard(chat_id: int, pending_ids: list[int]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for pid in pending_ids[:20]:
        kb.button(text=f"#{pid} open", callback_data=f"jb:{pid}")
    kb.adjust(4)
    kb.row(InlineKeyboardButton(text="◀️ Settings", callback_data=f"panel:{chat_id}"))
    return kb.as_markup()


def invites_keyboard(chat: Chat, links: list[InviteLink]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for label, value in DURATION_PRESETS:
        kb.button(text=f"➕ {label}", callback_data=f"inew:{chat.chat_id}:{value}")
    kb.adjust(4)
    for link in links[:10]:
        kb.row(
            InlineKeyboardButton(
                text=f"🗑 Revoke: {(link.name or link.duration)[:20]} ({link.uses})",
                callback_data=f"irev:{chat.chat_id}:{link.invite_link[-22:]}",
            )
        )
    kb.row(InlineKeyboardButton(text="◀️ Settings", callback_data=f"panel:{chat.chat_id}"))
    return kb.as_markup()
