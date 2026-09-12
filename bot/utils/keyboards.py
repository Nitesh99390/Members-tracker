"""Inline keyboard builders."""
from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.services.database import Chat


def onoff(flag: bool) -> str:
    return "✅ ON" if flag else "❌ OFF"


def chats_keyboard(chats: list[Chat], current: int | None) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for chat in chats:
        prefix = "🔹 " if chat.chat_id == current else ""
        icon = "📢" if chat.chat_type == "channel" else "👥"
        kb.button(text=f"{prefix}{icon} {chat.display[:40]}", callback_data=f"ctx:{chat.chat_id}")
    kb.adjust(1)
    return kb.as_markup()


def settings_keyboard(chat: Chat, default_duration: str) -> InlineKeyboardMarkup:
    cid = chat.chat_id
    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(
            text=f"📡 Tracking: {onoff(chat.tracking_enabled)}", callback_data=f"set:{cid}:tracking"
        )
    )
    kb.row(
        InlineKeyboardButton(
            text=f"🥾 Auto-kick: {onoff(chat.auto_kick)}", callback_data=f"set:{cid}:autokick"
        ),
        InlineKeyboardButton(
            text=f"⚙️ Mode: {'🔨 Ban' if chat.kick_mode == 'ban' else '👢 Kick'}",
            callback_data=f"set:{cid}:mode",
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
    kb.row(
        InlineKeyboardButton(
            text=f"🔓 Auto-approve requests: {onoff(chat.approve_requests)}",
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
    kb.row(InlineKeyboardButton(text="🔄 Refresh", callback_data=f"panel:{cid}"))
    return kb.as_markup()


def duration_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for label, value in (
        ("1 week", "1w"),
        ("2 weeks", "2w"),
        ("1 month", "1m"),
        ("2 months", "2m"),
        ("3 months", "3m"),
        ("6 months", "6m"),
        ("1 year", "1y"),
        ("♾ Never", "never"),
    ):
        kb.button(text=label, callback_data=f"dur:{chat_id}:{value}")
    kb.adjust(3)
    kb.row(
        InlineKeyboardButton(text="🌐 Use global default", callback_data=f"dur:{chat_id}:global"),
    )
    kb.row(InlineKeyboardButton(text="◀️ Back", callback_data=f"panel:{chat_id}"))
    return kb.as_markup()


def member_keyboard(chat_id: int, user_id: int, is_active: bool) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(text="+7d", callback_data=f"ext:{chat_id}:{user_id}:7d"),
        InlineKeyboardButton(text="+1m", callback_data=f"ext:{chat_id}:{user_id}:1m"),
        InlineKeyboardButton(text="+3m", callback_data=f"ext:{chat_id}:{user_id}:3m"),
        InlineKeyboardButton(text="♾", callback_data=f"ext:{chat_id}:{user_id}:never"),
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


def confirm_keyboard(action: str, chat_id: int, user_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(text="✅ Confirm", callback_data=f"{action}c:{chat_id}:{user_id}"),
        InlineKeyboardButton(text="❌ Cancel", callback_data=f"member:{chat_id}:{user_id}"),
    )
    return kb.as_markup()
