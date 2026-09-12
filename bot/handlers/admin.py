"""Admin commands (usable in groups or via private chat with a selected chat context)."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from html import escape

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from bot.config import Settings
from bot.services.database import Chat, Database, Member
from bot.services.membership import MembershipService
from bot.services.scheduler import ExpiryScheduler
from bot.utils.keyboards import chats_keyboard, member_keyboard, settings_keyboard
from bot.utils.permissions import bot_can_restrict, is_admin
from bot.utils.timeparse import ParseError, format_dt, humanize_delta, is_permanent, parse_duration

log = logging.getLogger(__name__)
router = Router(name="admin")

PAGE_SIZE = 15


# ---------------------------------------------------------------- resolving
async def resolve_chat(
    message: Message, bot: Bot, db: Database, settings: Settings
) -> Chat | None:
    """Determine which tracked chat a command refers to and authorise the caller."""
    user = message.from_user
    if user is None:
        return None
    if message.chat.type in (ChatType.GROUP, ChatType.SUPERGROUP):
        chat = await db.get_chat(message.chat.id)
        if chat is None:
            chat = await db.upsert_chat(
                message.chat.id, message.chat.title, message.chat.type, message.chat.username
            )
        if not await is_admin(bot, db, settings, chat.chat_id, user.id):
            await message.reply("⛔ Only admins of this chat can use this command.")
            return None
        return chat
    # private chat → use context
    chat_id = await db.get_context(user.id)
    if chat_id is None:
        await message.answer(
            "ℹ️ No chat selected. Use /chats to pick a group/channel first."
        )
        return None
    chat = await db.get_chat(chat_id)
    if chat is None:
        await message.answer("⚠️ Selected chat no longer exists. Use /chats.")
        return None
    if not await is_admin(bot, db, settings, chat.chat_id, user.id):
        await message.answer("⛔ You are not an admin of the selected chat.")
        return None
    return chat


async def resolve_target(
    message: Message, command: CommandObject, service: MembershipService, chat: Chat
) -> tuple[int | None, list[str]]:
    """Return (user_id, remaining_args). Supports reply, @username, numeric ID."""
    args = (command.args or "").split()
    if message.reply_to_message and message.reply_to_message.from_user:
        return message.reply_to_message.from_user.id, args
    if not args:
        return None, args
    uid, err = await service.resolve_user(chat.chat_id, args[0])
    if uid is None:
        await message.reply(f"⚠️ {err}")
        return None, args[1:]
    return uid, args[1:]


async def ensure_member(
    message: Message, db: Database, chat: Chat, user_id: int, bot: Bot
) -> Member | None:
    member = await db.get_member(chat.chat_id, user_id)
    if member:
        return member
    # try to fetch from Telegram and start tracking
    try:
        cm = await bot.get_chat_member(chat.chat_id, user_id)
    except TelegramBadRequest:
        await message.reply("⚠️ User is not tracked and not found in the chat.")
        return None
    member = await db.upsert_member(
        chat.chat_id, user_id, cm.user.full_name, cm.user.username, None, message.from_user.id
    )
    return member


def _usage(message: Message, text: str):
    return message.reply(f"ℹ️ <b>Usage:</b> {text}")


# ------------------------------------------------------------------ /chats
@router.message(Command("chats", "panel", "settings"), F.chat.type == ChatType.PRIVATE)
async def cmd_chats(message: Message, bot: Bot, db: Database, settings: Settings) -> None:
    user_id = message.from_user.id
    if user_id in settings.super_admins:
        chats = await db.list_chats()
    else:
        chats = await db.chats_for_admin(user_id)
    if not chats:
        await message.answer(
            "📭 No tracked chats yet.\n\nAdd me to a group or channel as admin "
            "(with <b>Ban users</b> permission) and it will appear here."
        )
        return
    current = await db.get_context(user_id)
    if message.text and message.text.split()[0].lstrip("/").lower() in ("panel", "settings") and current:
        chat = await db.get_chat(current)
        if chat:
            await message.answer(
                panel_text(chat, settings), reply_markup=settings_keyboard(chat, settings.default_duration)
            )
            return
    await message.answer(
        "📂 <b>Select a chat to manage:</b>", reply_markup=chats_keyboard(chats, current)
    )


@router.message(Command("panel", "settings"), F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}))
async def cmd_panel_group(message: Message, bot: Bot, db: Database, settings: Settings) -> None:
    chat = await resolve_chat(message, bot, db, settings)
    if not chat:
        return
    await db.set_context(message.from_user.id, chat.chat_id)
    await message.reply(
        panel_text(chat, settings), reply_markup=settings_keyboard(chat, settings.default_duration)
    )


def panel_text(chat: Chat, settings: Settings) -> str:
    icon = "📢" if chat.chat_type == "channel" else "👥"
    return (
        f"{icon} <b>{escape(chat.display)}</b>\n"
        f"🆔 <code>{chat.chat_id}</code>\n\n"
        f"⏳ Default duration: <b>{chat.default_duration or settings.default_duration}</b>\n"
        f"📨 Log chat: <code>{chat.log_chat_id or 'not set'}</code>\n\n"
        f"Tap a button to toggle a setting."
    )


# ------------------------------------------------------------------ /stats
@router.message(Command("stats"))
async def cmd_stats(message: Message, bot: Bot, db: Database, settings: Settings) -> None:
    chat = await resolve_chat(message, bot, db, settings)
    if not chat:
        return
    await message.reply(await stats_text(db, chat, settings))


async def stats_text(db: Database, chat: Chat, settings: Settings) -> str:
    s = await db.member_stats(chat.chat_id)
    now = datetime.now(timezone.utc)
    soon_24 = await db.expiring_within_count(chat.chat_id, now + timedelta(hours=24))
    soon_7d = await db.expiring_within_count(chat.chat_id, now + timedelta(days=7))
    wl = len(await db.list_whitelist(chat.chat_id))
    return (
        f"📊 <b>Stats — {escape(chat.display)}</b>\n\n"
        f"🟢 Active: <b>{s.get('active', 0)}</b>\n"
        f"♾ Permanent: <b>{s.get('permanent', 0)}</b>\n"
        f"🛡 Whitelisted: <b>{wl}</b>\n"
        f"⌛ Expired (removed): <b>{s.get('expired', 0)}</b>\n"
        f"⚪ Left: <b>{s.get('left', 0)}</b>  🔴 Kicked: <b>{s.get('kicked', 0)}</b>\n\n"
        f"⏰ Expiring in 24h: <b>{soon_24}</b>\n"
        f"📅 Expiring in 7 days: <b>{soon_7d}</b>\n\n"
        f"🕒 Server time: {format_dt(now, settings.tz)} ({settings.timezone})"
    )


# ------------------------------------------------------------------- /list
@router.message(Command("list", "members"))
async def cmd_list(
    message: Message, command: CommandObject, bot: Bot, db: Database, settings: Settings
) -> None:
    chat = await resolve_chat(message, bot, db, settings)
    if not chat:
        return
    page = 0
    if command.args and command.args.strip().isdigit():
        page = max(0, int(command.args.strip()) - 1)
    text, _ = await list_text(db, chat, settings, page)
    await message.reply(text)


async def list_text(db: Database, chat: Chat, settings: Settings, page: int) -> tuple[str, bool]:
    members = await db.list_members(chat.chat_id, "active", PAGE_SIZE + 1, page * PAGE_SIZE)
    has_next = len(members) > PAGE_SIZE
    members = members[:PAGE_SIZE]
    total = await db.count_members(chat.chat_id, "active")
    now = datetime.now(timezone.utc)
    lines = [f"📋 <b>Active members — {escape(chat.display)}</b> ({total})", ""]
    if not members:
        lines.append("<i>No active members tracked.</i>")
    for i, m in enumerate(members, start=page * PAGE_SIZE + 1):
        rem = "♾" if m.expires_at is None else humanize_delta(m.expires_at - now)
        lines.append(f"{i}. {m.mention_html} — <code>{m.user_id}</code> — ⏳ {rem}")
    lines.append("")
    lines.append(f"Page {page + 1} • /info &lt;id&gt; for details")
    return "\n".join(lines), has_next


# ---------------------------------------------------------------- /expiring
@router.message(Command("expiring"))
async def cmd_expiring(
    message: Message, command: CommandObject, bot: Bot, db: Database, settings: Settings
) -> None:
    chat = await resolve_chat(message, bot, db, settings)
    if not chat:
        return
    window = (command.args or "7d").strip()
    try:
        months, seconds = parse_duration(window)
    except ParseError:
        await _usage(message, "/expiring [duration]  e.g. /expiring 3d")
        return
    before = datetime.now(timezone.utc) + timedelta(seconds=seconds + months * 30 * 86400)
    members = [m for m in await db.expiring_members(before) if m.chat_id == chat.chat_id]
    now = datetime.now(timezone.utc)
    if not members:
        await message.reply(f"✅ Nobody expires within {window}.")
        return
    lines = [f"⏰ <b>Expiring within {window} — {escape(chat.display)}</b>", ""]
    for m in members[:50]:
        lines.append(
            f"• {m.mention_html} <code>{m.user_id}</code> — {humanize_delta(m.expires_at - now)} "
            f"({format_dt(m.expires_at, settings.tz)})"
        )
    await message.reply("\n".join(lines))


# ------------------------------------------------------------------- /info
@router.message(Command("info", "check", "status"))
async def cmd_info(
    message: Message,
    command: CommandObject,
    bot: Bot,
    db: Database,
    settings: Settings,
    service: MembershipService,
) -> None:
    chat = await resolve_chat(message, bot, db, settings)
    if not chat:
        return
    uid, _ = await resolve_target(message, command, service, chat)
    if uid is None:
        await _usage(message, "/info &lt;user_id|@username&gt; or reply to a message")
        return
    member = await db.get_member(chat.chat_id, uid)
    if not member:
        await message.reply("⚠️ This user is not tracked in this chat.")
        return
    await message.reply(
        service.member_card(chat, member),
        reply_markup=member_keyboard(chat.chat_id, uid, member.status == "active"),
    )


# -------------------------------------------------------------------- /add
@router.message(Command("add", "track"))
async def cmd_add(
    message: Message,
    command: CommandObject,
    bot: Bot,
    db: Database,
    settings: Settings,
    service: MembershipService,
) -> None:
    """/add <user> [duration|date|never] — start tracking a user manually."""
    chat = await resolve_chat(message, bot, db, settings)
    if not chat:
        return
    uid, rest = await resolve_target(message, command, service, chat)
    if uid is None:
        await _usage(message, "/add &lt;user_id|@username&gt; [30d | 2025-12-31 | never]")
        return
    expiry_text = " ".join(rest).strip()
    try:
        expires = service.parse_user_expiry(expiry_text) if expiry_text else service.compute_expiry(chat)
    except ParseError as exc:
        await message.reply(f"⚠️ Invalid duration/date: {escape(str(exc))}")
        return
    try:
        cm = await bot.get_chat_member(chat.chat_id, uid)
        full_name, username = cm.user.full_name, cm.user.username
    except TelegramBadRequest:
        full_name, username = None, None
    member = await db.upsert_member(
        chat.chat_id, uid, full_name, username, expires, message.from_user.id
    )
    await db.add_log(chat.chat_id, uid, "manual_add", str(expires), message.from_user.id)
    await message.reply(
        "✅ <b>Tracking started</b>\n\n" + service.member_card(chat, member),
        reply_markup=member_keyboard(chat.chat_id, uid, True),
    )


# ---------------------------------------------------------- /extend /setexpiry
@router.message(Command("extend", "renew"))
async def cmd_extend(
    message: Message,
    command: CommandObject,
    bot: Bot,
    db: Database,
    settings: Settings,
    service: MembershipService,
) -> None:
    chat = await resolve_chat(message, bot, db, settings)
    if not chat:
        return
    uid, rest = await resolve_target(message, command, service, chat)
    if uid is None or not rest:
        await _usage(message, "/extend &lt;user&gt; &lt;duration|date|never&gt;  e.g. /extend 12345 1m")
        return
    member = await ensure_member(message, db, chat, uid, bot)
    if not member:
        return
    try:
        new_expiry = await service.extend_member(chat, member, " ".join(rest), message.from_user.id)
    except ParseError as exc:
        await message.reply(f"⚠️ Invalid duration/date: {escape(str(exc))}")
        return
    await message.reply(
        f"🔄 <b>Extended</b> {member.mention_html}\n⏳ New expiry: <b>{format_dt(new_expiry, settings.tz)}</b>"
    )
    if chat.notify_user and new_expiry:
        await service.dm_user(
            uid,
            f"🎉 Your membership in <b>{escape(chat.display)}</b> has been extended until "
            f"<b>{format_dt(new_expiry, settings.tz)}</b>.",
        )


@router.message(Command("setexpiry", "set", "until"))
async def cmd_setexpiry(
    message: Message,
    command: CommandObject,
    bot: Bot,
    db: Database,
    settings: Settings,
    service: MembershipService,
) -> None:
    chat = await resolve_chat(message, bot, db, settings)
    if not chat:
        return
    uid, rest = await resolve_target(message, command, service, chat)
    if uid is None or not rest:
        await _usage(
            message, "/setexpiry &lt;user&gt; &lt;date|duration|never&gt;  e.g. /setexpiry 12345 2025-12-31"
        )
        return
    member = await ensure_member(message, db, chat, uid, bot)
    if not member:
        return
    try:
        new_expiry = await service.set_expiry(chat, member, " ".join(rest), message.from_user.id)
    except ParseError as exc:
        await message.reply(f"⚠️ Invalid duration/date: {escape(str(exc))}")
        return
    await message.reply(
        f"📝 <b>Expiry set</b> for {member.mention_html}\n⏳ <b>{format_dt(new_expiry, settings.tz)}</b>"
    )


# ------------------------------------------------------------ /remove /kick
@router.message(Command("remove", "kick", "expire"))
async def cmd_remove(
    message: Message,
    command: CommandObject,
    bot: Bot,
    db: Database,
    settings: Settings,
    service: MembershipService,
) -> None:
    chat = await resolve_chat(message, bot, db, settings)
    if not chat:
        return
    uid, _ = await resolve_target(message, command, service, chat)
    if uid is None:
        await _usage(message, "/remove &lt;user_id|@username&gt; or reply")
        return
    member = await ensure_member(message, db, chat, uid, bot)
    if not member:
        return
    ok, msg = await service.remove_member(chat, member, reason="manual", actor_id=message.from_user.id)
    if ok:
        await message.reply(f"🚫 Removed {member.mention_html} from {escape(chat.display)}.")
    else:
        await message.reply(f"❌ Failed: {escape(msg)}")


@router.message(Command("untrack", "forget"))
async def cmd_untrack(
    message: Message,
    command: CommandObject,
    bot: Bot,
    db: Database,
    settings: Settings,
    service: MembershipService,
) -> None:
    chat = await resolve_chat(message, bot, db, settings)
    if not chat:
        return
    uid, _ = await resolve_target(message, command, service, chat)
    if uid is None:
        await _usage(message, "/untrack &lt;user&gt; — stop tracking without removing")
        return
    await db.delete_member(chat.chat_id, uid)
    await db.add_log(chat.chat_id, uid, "untrack", None, message.from_user.id)
    await message.reply(f"🗑 Stopped tracking <code>{uid}</code>.")


# -------------------------------------------------------------- whitelist
@router.message(Command("whitelist", "wl", "vip"))
async def cmd_whitelist(
    message: Message,
    command: CommandObject,
    bot: Bot,
    db: Database,
    settings: Settings,
    service: MembershipService,
) -> None:
    chat = await resolve_chat(message, bot, db, settings)
    if not chat:
        return
    uid, _ = await resolve_target(message, command, service, chat)
    if uid is None:
        ids = await db.list_whitelist(chat.chat_id)
        if not ids:
            await message.reply("🛡 Whitelist is empty.\nUse /whitelist &lt;user&gt; to add.")
            return
        await message.reply(
            "🛡 <b>Whitelist</b>\n" + "\n".join(f"• <code>{i}</code>" for i in ids)
        )
        return
    await db.add_whitelist(chat.chat_id, uid, message.from_user.id)
    if await db.get_member(chat.chat_id, uid):
        await db.set_member_expiry(chat.chat_id, uid, None)
    await db.add_log(chat.chat_id, uid, "whitelist_add", None, message.from_user.id)
    await message.reply(f"🛡 <code>{uid}</code> whitelisted — will never be auto-removed.")


@router.message(Command("unwhitelist", "unwl"))
async def cmd_unwhitelist(
    message: Message,
    command: CommandObject,
    bot: Bot,
    db: Database,
    settings: Settings,
    service: MembershipService,
) -> None:
    chat = await resolve_chat(message, bot, db, settings)
    if not chat:
        return
    uid, _ = await resolve_target(message, command, service, chat)
    if uid is None:
        await _usage(message, "/unwhitelist &lt;user&gt;")
        return
    await db.remove_whitelist(chat.chat_id, uid)
    member = await db.get_member(chat.chat_id, uid)
    if member and member.expires_at is None:
        await db.set_member_expiry(chat.chat_id, uid, service.compute_expiry(chat))
    await db.add_log(chat.chat_id, uid, "whitelist_remove", None, message.from_user.id)
    await message.reply(f"✅ <code>{uid}</code> removed from whitelist.")


# ------------------------------------------------------------------ /note
@router.message(Command("note"))
async def cmd_note(
    message: Message,
    command: CommandObject,
    bot: Bot,
    db: Database,
    settings: Settings,
    service: MembershipService,
) -> None:
    chat = await resolve_chat(message, bot, db, settings)
    if not chat:
        return
    uid, rest = await resolve_target(message, command, service, chat)
    if uid is None:
        await _usage(message, "/note &lt;user&gt; &lt;text&gt;  (empty text clears)")
        return
    member = await ensure_member(message, db, chat, uid, bot)
    if not member:
        return
    note = " ".join(rest).strip() or None
    await db.set_member_note(chat.chat_id, uid, note)
    await message.reply("📝 Note saved." if note else "📝 Note cleared.")


# ----------------------------------------------------------- chat settings
@router.message(Command("setduration", "duration"))
async def cmd_setduration(
    message: Message, command: CommandObject, bot: Bot, db: Database, settings: Settings
) -> None:
    chat = await resolve_chat(message, bot, db, settings)
    if not chat:
        return
    value = (command.args or "").strip().lower()
    if not value:
        await _usage(message, "/setduration &lt;30d | 1m | 2w | never | global&gt;")
        return
    if value == "global":
        await db.update_chat(chat.chat_id, default_duration=None)
        await message.reply(f"⏳ Duration reset to global default (<b>{settings.default_duration}</b>).")
        return
    if not is_permanent(value):
        try:
            parse_duration(value)
        except ParseError as exc:
            await message.reply(f"⚠️ Invalid duration: {escape(str(exc))}")
            return
    await db.update_chat(chat.chat_id, default_duration=value)
    await db.add_log(chat.chat_id, None, "set_duration", value, message.from_user.id)
    await message.reply(f"⏳ Default membership duration for new members: <b>{value}</b>")


@router.message(Command("setlog", "logchat"))
async def cmd_setlog(
    message: Message, command: CommandObject, bot: Bot, db: Database, settings: Settings
) -> None:
    chat = await resolve_chat(message, bot, db, settings)
    if not chat:
        return
    arg = (command.args or "").strip()
    if arg in ("off", "none", "0"):
        await db.update_chat(chat.chat_id, log_chat_id=None)
        await message.reply("📨 Log channel disabled.")
        return
    if arg == "here":
        target = message.chat.id
    elif arg.lstrip("-").isdigit():
        target = int(arg)
    else:
        await _usage(message, "/setlog &lt;chat_id | here | off&gt;")
        return
    try:
        await bot.send_message(target, f"📨 Log channel set for <b>{escape(chat.display)}</b>.")
    except Exception as exc:  # noqa: BLE001
        await message.reply(f"❌ I cannot post there: {escape(str(exc))}")
        return
    await db.update_chat(chat.chat_id, log_chat_id=target)
    await message.reply(f"📨 Logs will be posted to <code>{target}</code>.")


@router.message(Command("setwelcome"))
async def cmd_setwelcome(
    message: Message, command: CommandObject, bot: Bot, db: Database, settings: Settings
) -> None:
    chat = await resolve_chat(message, bot, db, settings)
    if not chat:
        return
    text = (command.args or "").strip()
    if not text:
        await _usage(
            message,
            "/setwelcome &lt;text&gt;\nPlaceholders: {mention} {name} {expires} {chat}",
        )
        return
    await db.update_chat(chat.chat_id, welcome_text=text, welcome_enabled=1)
    await message.reply("👋 Welcome message saved and enabled.")


# ------------------------------------------------------------- maintenance
@router.message(Command("forcecheck", "runcheck"))
async def cmd_forcecheck(
    message: Message, bot: Bot, db: Database, settings: Settings, scheduler: ExpiryScheduler
) -> None:
    chat = await resolve_chat(message, bot, db, settings)
    if not chat:
        return
    result = await scheduler.run_once()
    await message.reply(
        f"🔁 Check complete.\nRemoved: <b>{result.get('removed', 0)}</b> • "
        f"Reminded: <b>{result.get('reminded', 0)}</b>"
    )


@router.message(Command("permissions", "perms"))
async def cmd_permissions(message: Message, bot: Bot, db: Database, settings: Settings) -> None:
    chat = await resolve_chat(message, bot, db, settings)
    if not chat:
        return
    ok = await bot_can_restrict(bot, chat.chat_id)
    await message.reply(
        f"🔐 Ban permission in <b>{escape(chat.display)}</b>: {'✅ OK' if ok else '❌ MISSING'}"
        + ("" if ok else "\nPromote me to admin with <b>Ban users</b> right.")
    )


@router.message(Command("logs"))
async def cmd_logs(message: Message, bot: Bot, db: Database, settings: Settings) -> None:
    chat = await resolve_chat(message, bot, db, settings)
    if not chat:
        return
    await message.reply(await logs_text(db, chat, settings))


async def logs_text(db: Database, chat: Chat, settings: Settings) -> str:
    rows = await db.recent_logs(chat.chat_id, 15)
    if not rows:
        return "📜 No logs yet."
    lines = [f"📜 <b>Recent activity — {escape(chat.display)}</b>", ""]
    for r in rows:
        ts = format_dt(datetime.fromisoformat(r["created_at"]), settings.tz)
        who = f"<code>{r['user_id']}</code> " if r["user_id"] else ""
        det = f" <i>{escape(str(r['details']))[:40]}</i>" if r["details"] else ""
        lines.append(f"• {ts} — <b>{escape(r['action'])}</b> {who}{det}")
    return "\n".join(lines)


@router.message(Command("gstats", "globalstats"))
async def cmd_gstats(message: Message, db: Database, settings: Settings) -> None:
    if message.from_user.id not in settings.super_admins:
        return
    s = await db.global_stats()
    await message.reply(
        f"🌐 <b>Global stats</b>\n\n"
        f"Chats: <b>{s['chats']}</b>\nActive members: <b>{s['active']}</b>\n"
        f"Expired: <b>{s['expired']}</b>\nTotal records: <b>{s['total']}</b>"
    )


@router.message(Command("broadcast"))
async def cmd_broadcast(
    message: Message, command: CommandObject, bot: Bot, db: Database, settings: Settings
) -> None:
    """Send a DM to all active tracked members of the selected chat (admins only)."""
    chat = await resolve_chat(message, bot, db, settings)
    if not chat:
        return
    text = (command.args or "").strip()
    if not text:
        await _usage(message, "/broadcast &lt;message&gt; — DM all active members of this chat")
        return
    members = await db.list_members(chat.chat_id, "active", limit=10000)
    sent = 0
    import asyncio

    for m in members:
        try:
            await bot.send_message(m.user_id, f"📣 <b>{escape(chat.display)}</b>\n\n{text}")
            sent += 1
        except Exception:  # noqa: BLE001
            pass
        await asyncio.sleep(0.05)
    await message.reply(f"📣 Broadcast sent to <b>{sent}</b>/{len(members)} members.")
