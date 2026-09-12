"""/start, /help and self-service commands for regular users."""
from __future__ import annotations

from datetime import datetime, timezone
from html import escape

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command, CommandStart
from aiogram.types import Message

from bot.config import Settings
from bot.services.database import Database
from bot.utils.timeparse import format_dt, humanize_delta

router = Router(name="common")

HELP_ADMIN = """
<b>🛠 Member Tracker Bot — Admin Guide</b>

<b>Setup</b>
1. Add me to your group/channel as <b>admin</b> with <i>Ban users</i> permission.
2. Open my private chat → /chats → select the chat.
3. Configure with the inline panel (/panel).

<b>Chat management</b>
/chats — choose which group/channel to manage
/panel — settings panel (tracking, auto-kick, mode, notify, welcome, duration)
/stats — membership statistics
/list [page] — active members sorted by expiry
/expiring [7d] — who expires soon
/logs — recent activity
/permissions — check if I can ban users
/forcecheck — run the expiry check now

<b>Member management</b> (use ID, @username or reply)
/add &lt;user&gt; [30d|2025-12-31|never] — start tracking manually
/info &lt;user&gt; — details + quick action buttons
/extend &lt;user&gt; &lt;1m|15d|date&gt; — extend membership
/setexpiry &lt;user&gt; &lt;date|duration|never&gt; — set exact expiry
/remove &lt;user&gt; — remove immediately
/untrack &lt;user&gt; — stop tracking (no removal)
/whitelist [user] — never auto-remove / show list
/unwhitelist &lt;user&gt;
/note &lt;user&gt; &lt;text&gt; — attach a note (e.g. payment ref)
/broadcast &lt;text&gt; — DM all active members

<b>Chat settings</b>
/setduration &lt;30d|1m|2w|never|global&gt;
/setlog &lt;chat_id|here|off&gt; — where to post logs
/setwelcome &lt;text&gt; — placeholders {mention} {name} {expires} {chat}

<b>Duration formats</b>: 30d, 1m (month), 2w, 12h, 1y, "1m 15d"
<b>Date formats</b>: 2025-12-31, 31/12/2025, 2025-12-31 18:30
"""

HELP_USER = """
<b>👋 Member Tracker Bot</b>

I keep track of memberships in groups &amp; channels and automatically remove members when their
time is up.

/mystatus — see your memberships and expiry dates
/help — this message

Admins: add me to a chat as admin and type /help in my private chat.
"""


@router.message(CommandStart(), F.chat.type == ChatType.PRIVATE)
async def cmd_start(message: Message, db: Database, settings: Settings) -> None:
    user_id = message.from_user.id
    is_any_admin = user_id in settings.super_admins or bool(await db.chats_for_admin(user_id))
    name = escape(message.from_user.first_name)
    intro = f"Hello <b>{name}</b>! 👋\n"
    if is_any_admin:
        await message.answer(intro + HELP_ADMIN)
    else:
        await message.answer(intro + HELP_USER)


@router.message(Command("help"))
async def cmd_help(message: Message, db: Database, settings: Settings) -> None:
    user_id = message.from_user.id
    is_any_admin = user_id in settings.super_admins or bool(await db.chats_for_admin(user_id))
    if message.chat.type != ChatType.PRIVATE:
        await message.reply(
            "ℹ️ Full admin help is available in my private chat. Quick commands here: "
            "/stats /list /info /extend /remove /panel"
        )
        return
    await message.answer(HELP_ADMIN if is_any_admin else HELP_USER)


@router.message(Command("mystatus", "me"))
async def cmd_mystatus(message: Message, db: Database, settings: Settings) -> None:
    user_id = message.from_user.id
    now = datetime.now(timezone.utc)
    chats = await db.list_chats()
    lines: list[str] = []
    for chat in chats:
        if message.chat.type != ChatType.PRIVATE and chat.chat_id != message.chat.id:
            continue
        member = await db.get_member(chat.chat_id, user_id)
        if not member or member.status != "active":
            continue
        rem = "♾ permanent" if member.expires_at is None else humanize_delta(member.expires_at - now)
        lines.append(
            f"• <b>{escape(chat.display)}</b>\n"
            f"   ⏳ {format_dt(member.expires_at, settings.tz)} ({rem})"
        )
    if not lines:
        await message.reply("ℹ️ You have no tracked memberships.")
        return
    await message.reply("📇 <b>Your memberships</b>\n\n" + "\n".join(lines))


@router.message(Command("id"))
async def cmd_id(message: Message) -> None:
    target = message.reply_to_message.from_user if message.reply_to_message else message.from_user
    text = f"👤 User ID: <code>{target.id}</code>"
    if message.chat.type != ChatType.PRIVATE:
        text += f"\n💬 Chat ID: <code>{message.chat.id}</code>"
    await message.reply(text)
