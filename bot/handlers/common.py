"""/start, /help and self-service commands for regular users.

The private chat is button-driven: /start shows a small home screen and every
other screen is reached by tapping. Help is split into short topic pages so
nobody is greeted by a wall of commands.
"""
from __future__ import annotations

from datetime import datetime, timezone
from html import escape

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command, CommandStart
from aiogram.types import Message

from bot.config import Settings
from bot.services.database import Database
from bot.utils.keyboards import help_keyboard, home_keyboard, open_private_keyboard
from bot.utils.timeparse import format_dt, humanize_delta

router = Router(name="common")

BOT_NAME = "Member Tracker"

HELP_TOPICS: dict[str, str] = {
    "main": (
        f"<b>📖 {BOT_NAME} — Help</b>\n\n"
        "I keep track of how long each member may stay in your groups and channels, "
        "and remove them automatically when their time is up.\n\n"
        "Pick a topic:"
    ),
    "setup": (
        "<b>🚀 Getting started</b>\n\n"
        "1. Add me to your group or channel as <b>admin</b> with the <b>Ban users</b> right.\n"
        "2. Come back here and tap <b>📂 My chats</b> → choose the chat.\n"
        "3. Set the default duration in <b>⚙️ Settings</b>.\n\n"
        "That's it. From now on every new member is tracked automatically.\n\n"
        "<i>Tip: keep <b>Ask on join</b> enabled and I'll send you a one-tap prompt for each "
        "new member so you can pick their duration on the spot.</i>"
    ),
    "members": (
        "<b>👥 Managing members</b>\n\n"
        "• <b>Members</b> on the dashboard lists everyone with their remaining time.\n"
        "• Tap a member (or send me their ID / @username) to open their card: "
        "extend, set a custom date, protect as VIP, remove.\n"
        "• <b>Pending</b> shows joins still waiting for your decision.\n\n"
        "<b>In the group</b> you can reply to a message with:\n"
        "<code>/info</code> · <code>/extend 1m</code> · <code>/remove</code> · <code>/ask</code>"
    ),
    "invites": (
        "<b>🔗 Invite links</b>\n\n"
        "Create a link with a preset duration (e.g. 3 months). Everyone who joins through "
        "it gets exactly that duration — no prompt, no manual work.\n\n"
        "Perfect for selling plans: one link per plan, share it after payment.\n\n"
        "Dashboard → <b>🔗 Invite links</b> → <b>➕ New link</b>"
    ),
    "settings": (
        "<b>⚙️ Settings explained</b>\n\n"
        "• <b>Duration</b> — default time a new member may stay.\n"
        "• <b>Tracking</b> — pause/resume tracking for this chat.\n"
        "• <b>Auto-remove</b> — remove members when they expire.\n"
        "• <b>Ask on join</b> — prompt you for each new member.\n"
        "• <b>Kick / Ban mode</b> — kicked users can rejoin, banned cannot.\n\n"
        "<b>Advanced</b>: DM members about expiry, welcome message, join-request handling, "
        "who receives prompts (owner or all admins)."
    ),
    "commands": (
        "<b>⌨️ Commands &amp; formats</b>\n\n"
        "Everything is available through buttons. Power users can also type:\n\n"
        "<code>/add &lt;user&gt; [3m]</code> — track someone manually\n"
        "<code>/extend &lt;user&gt; 1m</code> · <code>/setexpiry &lt;user&gt; 2025-12-31</code>\n"
        "<code>/remove &lt;user&gt;</code> · <code>/whitelist &lt;user&gt;</code> · <code>/note &lt;user&gt; text</code>\n"
        "<code>/invite 3m Gold</code> · <code>/expiring 3d</code> · <code>/search name</code>\n"
        "<code>/setwelcome text</code> · <code>/setlog here</code> · <code>/broadcast text</code>\n"
        "<code>/sync</code> · <code>/forcecheck</code> · <code>/permissions</code>\n\n"
        "<b>Durations</b>: <code>30d</code> <code>2w</code> <code>1m</code> <code>1y</code> <code>1m 15d</code> <code>never</code>\n"
        "<b>Dates</b>: <code>2025-12-31</code> · <code>31/12/2025 18:30</code>"
    ),
}


async def _is_admin_user(db: Database, settings: Settings, user_id: int) -> tuple[bool, bool]:
    """Return (is_admin_anywhere, has_chats)."""
    if user_id in settings.super_admins:
        return True, bool(await db.list_chats())
    chats = await db.chats_for_admin(user_id)
    return bool(chats), bool(chats)


def home_text(first_name: str, is_admin: bool, has_chats: bool) -> str:
    name = escape(first_name)
    if is_admin and has_chats:
        return (
            f"<b>👋 Welcome back, {name}</b>\n\n"
            "Choose a chat to see its members, pending decisions and settings."
        )
    if is_admin:
        return (
            f"<b>👋 Hello {name}</b>\n\n"
            "Add me to a group or channel as admin and it will show up here."
        )
    return (
        f"<b>👋 Hello {name}</b>\n\n"
        f"I'm {BOT_NAME}. I manage time-limited memberships for groups and channels.\n"
        "Admins: add me to your chat to get started."
    )


@router.message(CommandStart(), F.chat.type == ChatType.PRIVATE)
async def cmd_start(message: Message, bot: Bot, db: Database, settings: Settings) -> None:
    user = message.from_user
    is_admin, has_chats = await _is_admin_user(db, settings, user.id)
    me = await bot.me()
    if is_admin and has_chats and (message.text or "").strip().endswith(" menu"):
        # deep link from a group ("Open dashboard") → jump straight to the chat list
        from bot.handlers.admin import cmd_chats  # local import avoids a cycle

        await cmd_chats(message, bot, db, settings)
        return
    await message.answer(
        home_text(user.first_name, is_admin, has_chats),
        reply_markup=home_keyboard(is_admin, me.username or "", has_chats),
    )


@router.message(Command("help"))
async def cmd_help(message: Message, bot: Bot) -> None:
    if message.chat.type != ChatType.PRIVATE:
        me = await bot.me()
        await message.reply(
            "Manage this chat from my private dashboard.\n"
            "Quick replies here: <code>/info</code> · <code>/extend 1m</code> · <code>/remove</code>",
            reply_markup=open_private_keyboard(me.username or ""),
        )
        return
    await message.answer(HELP_TOPICS["main"], reply_markup=help_keyboard("main"))


async def mystatus_text(db: Database, settings: Settings, user_id: int, only_chat: int | None = None) -> str:
    now = datetime.now(timezone.utc)
    lines: list[str] = []
    for member in await db.memberships_for_user(user_id):
        if only_chat is not None and member.chat_id != only_chat:
            continue
        chat = await db.get_chat(member.chat_id)
        if not chat:
            continue
        rem = "♾ lifetime" if member.expires_at is None else humanize_delta(member.expires_at - now)
        lines.append(
            f"• <b>{escape(chat.display)}</b>\n   ⏳ {format_dt(member.expires_at, settings.tz)} · {rem}"
        )
    if not lines:
        return "📇 You have no tracked memberships."
    return "📇 <b>Your memberships</b>\n\n" + "\n".join(lines)


@router.message(Command("mystatus", "me"))
async def cmd_mystatus(message: Message, db: Database, settings: Settings) -> None:
    only = None if message.chat.type == ChatType.PRIVATE else message.chat.id
    await message.reply(await mystatus_text(db, settings, message.from_user.id, only))


@router.message(Command("id"))
async def cmd_id(message: Message) -> None:
    target = message.reply_to_message.from_user if message.reply_to_message else message.from_user
    text = f"👤 User ID: <code>{target.id}</code>"
    if message.chat.type != ChatType.PRIVATE:
        text += f"\n💬 Chat ID: <code>{message.chat.id}</code>"
    await message.reply(text)
