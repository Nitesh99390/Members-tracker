"""Admin commands (usable in groups or via private chat with a selected chat context)."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandObject
from aiogram.types import FSInputFile, Message

from bot.config import Settings
from bot.services.database import Chat, Database, Member
from bot.services.membership import MembershipService
from bot.services.metrics import Metrics
from bot.services.scheduler import ExpiryScheduler
from bot.utils.keyboards import (
    chats_keyboard,
    dashboard_keyboard,
    invites_keyboard,
    member_keyboard,
    open_private_keyboard,
    pending_keyboard,
    settings_keyboard,
)
from bot.utils.permissions import bot_can_restrict, is_admin
from bot.utils.timeparse import (
    ParseError,
    describe_duration,
    format_dt,
    humanize_delta,
    is_permanent,
    parse_duration,
)

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
    # private chat → use context; fall back to the only chat the admin has
    chat_id = await db.get_context(user.id)
    if chat_id is None:
        chats = await db.list_chats() if user.id in settings.super_admins else await db.chats_for_admin(user.id)
        if len(chats) == 1:
            chat_id = chats[0].chat_id
            await db.set_context(user.id, chat_id)
    if chat_id is None:
        await message.answer("Pick a chat first.", reply_markup=_chats_markup_or_none(await _admin_chats(db, settings, user.id), None))
        return None
    chat = await db.get_chat(chat_id)
    if chat is None:
        await message.answer("⚠️ That chat is no longer tracked.", reply_markup=_chats_markup_or_none(await _admin_chats(db, settings, user.id), None))
        return None
    if not await is_admin(bot, db, settings, chat.chat_id, user.id):
        await message.answer("⛔ You are not an admin of the selected chat.")
        return None
    return chat


async def _admin_chats(db: Database, settings: Settings, user_id: int) -> list[Chat]:
    if user_id in settings.super_admins:
        return await db.list_chats()
    return await db.chats_for_admin(user_id)


def _chats_markup_or_none(chats: list[Chat], current: int | None):
    return chats_keyboard(chats, current) if chats else None


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
        chat.chat_id, user_id, cm.user.full_name, cm.user.username, None, message.from_user.id, source="manual"
    )
    return member


def _settings_markup(chat: Chat, settings: Settings):
    return settings_keyboard(chat, settings.default_duration, settings.ask_on_join_default)


def _usage(message: Message, text: str):
    return message.reply(f"<b>Usage:</b> {text}")


# ------------------------------------------------------------------ /chats
@router.message(Command("chats", "panel", "settings", "menu", "dashboard"), F.chat.type == ChatType.PRIVATE)
async def cmd_chats(message: Message, bot: Bot, db: Database, settings: Settings) -> None:
    user_id = message.from_user.id
    chats = await _admin_chats(db, settings, user_id)
    if not chats:
        me = await bot.me()
        from bot.utils.keyboards import home_keyboard  # local import avoids a cycle

        await message.answer(
            "📭 <b>No chats yet</b>\n\nAdd me to a group or channel as admin "
            "(with the <b>Ban users</b> right) and it will appear here.",
            reply_markup=home_keyboard(True, me.username or "", False),
        )
        return
    current = await db.get_context(user_id)
    cmd = (message.text or "").split()[0].lstrip("/").lower().split("@")[0]
    if len(chats) == 1 and current is None:
        current = chats[0].chat_id
        await db.set_context(user_id, current)
    if current and cmd in ("panel", "settings", "menu", "dashboard"):
        chat = await db.get_chat(current)
        if chat:
            if cmd == "settings":
                await message.answer(settings_text(chat, settings), reply_markup=_settings_markup(chat, settings))
            else:
                await message.answer(await dashboard_text(db, chat, settings), reply_markup=dashboard_keyboard(chat, await db.count_pending(chat.chat_id)))
            return
    await message.answer("📂 <b>Your chats</b>\nChoose one to manage:", reply_markup=chats_keyboard(chats, current))


@router.message(Command("panel", "settings", "menu", "dashboard", "chats"), F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}))
async def cmd_panel_group(message: Message, bot: Bot, db: Database, settings: Settings) -> None:
    """Settings are private: point the admin to the DM dashboard instead of exposing them in the group."""
    chat = await resolve_chat(message, bot, db, settings)
    if not chat:
        return
    await db.set_context(message.from_user.id, chat.chat_id)
    me = await bot.me()
    await message.reply(
        f"⚙️ <b>{escape(chat.display)}</b> is selected. Manage it from my private chat.",
        reply_markup=open_private_keyboard(me.username or ""),
    )


async def dashboard_text(db: Database, chat: Chat, settings: Settings) -> str:
    icon = "📢" if chat.is_channel else "👥"
    duration = chat.default_duration or settings.default_duration
    s = await db.member_stats(chat.chat_id)
    now = datetime.now(timezone.utc)
    soon_7d = await db.expiring_within_count(chat.chat_id, now + timedelta(days=7))
    pending = await db.count_pending(chat.chat_id)
    status = "" if chat.tracking_enabled else "\n⏸ <i>Tracking is paused</i>"
    attention = ""
    if pending:
        attention += f"\n🔔 <b>{pending}</b> waiting for your decision"
    if soon_7d:
        attention += f"\n⏰ <b>{soon_7d}</b> expiring within 7 days"
    return (
        f"{icon} <b>{escape(chat.display)}</b>{status}\n\n"
        f"🟢 Active members: <b>{s.get('active', 0)}</b>\n"
        f"⏳ Default duration: <b>{escape(describe_duration(duration))}</b>"
        f"{attention}"
    )


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


# kept for backwards compatibility with older imports
panel_text = settings_text


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
    pending = await db.count_pending(chat.chat_id)
    return (
        f"📊 <b>Overview — {escape(chat.display)}</b>\n\n"
        f"🟢 Active: <b>{s.get('active', 0)}</b> · joined today: {s.get('joined_today', 0)}\n"
        f"♾ Lifetime: <b>{s.get('permanent', 0)}</b> · 🛡 VIP: <b>{wl}</b>\n"
        f"🔔 Awaiting decision: <b>{pending}</b>\n\n"
        f"⏰ Expiring in 24h: <b>{soon_24}</b>\n"
        f"📅 Expiring in 7 days: <b>{soon_7d}</b>\n\n"
        f"⌛ Expired: {s.get('expired', 0)} · 🚫 Removed: {s.get('manual', 0)} · "
        f"⚪ Left: {s.get('left', 0)} · 🔴 Kicked: {s.get('kicked', 0)}\n\n"
        f"<i>{format_dt(now, settings.tz)} · {settings.timezone}</i>"
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
    lines = [f"👥 <b>Members — {escape(chat.display)}</b> · {total} active", ""]
    if not members:
        lines.append("<i>No active members tracked yet.</i>")
    for i, m in enumerate(members, start=page * PAGE_SIZE + 1):
        rem = "♾" if m.expires_at is None else humanize_delta(m.expires_at - now)
        lines.append(f"{i}. {m.mention_html} · <code>{m.user_id}</code> · ⏳ {rem}")
    lines.append("")
    lines.append("<i>Send a user ID or @username to open their card.</i>")
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
    members = await db.expiring_members(before, chat_id=chat.chat_id)
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
        expires = service.expiry_from_text(expiry_text) if expiry_text else service.compute_expiry(chat)
    except ParseError as exc:
        await message.reply(f"⚠️ Invalid duration/date: {escape(str(exc))}")
        return
    try:
        cm = await bot.get_chat_member(chat.chat_id, uid)
        full_name, username = cm.user.full_name, cm.user.username
    except (TelegramBadRequest, TelegramForbiddenError):
        full_name, username = None, None
    member = await db.upsert_member(
        chat.chat_id, uid, full_name, username, expires, message.from_user.id, source="manual"
    )
    pending = await db.get_pending_for(chat.chat_id, uid)
    if pending:
        await db.resolve_pending(pending.id, message.from_user.id, expiry_text or "default")
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
    await service.notify_extension(chat, uid, new_expiry)


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
    await message.reply(
        f"⏳ Default membership duration for new members: <b>{escape(describe_duration(value))}</b>"
    )


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
    if result.get("skipped"):
        await message.reply("⏳ A check is already running, try again in a moment.")
        return
    await message.reply(
        f"🔁 Check complete.\nRemoved: <b>{result.get('removed', 0)}</b> • "
        f"Reminded: <b>{result.get('reminded', 0)}</b> • "
        f"Prompts timed out: <b>{result.get('prompts_expired', 0)}</b>"
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
        return f"📜 <b>Activity — {escape(chat.display)}</b>\n\n<i>Nothing recorded yet.</i>"
    lines = [f"📜 <b>Activity — {escape(chat.display)}</b>", ""]
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
    size_kb = db.db_size_bytes() / 1024
    await message.reply(
        f"🌐 <b>Global stats</b>\n\n"
        f"Chats: <b>{s['chats']}</b> (tracking: {s['tracking']})\n"
        f"Active members: <b>{s['active']}</b>\n"
        f"Expired: <b>{s['expired']}</b>\nTotal records: <b>{s['total']}</b>\n"
        f"Pending decisions: <b>{s['pending']}</b>\nInvite links: <b>{s['invites']}</b>\n"
        f"DB size: <b>{size_kb:.0f} KB</b>"
    )


@router.message(Command("broadcast"))
async def cmd_broadcast(
    message: Message,
    command: CommandObject,
    bot: Bot,
    db: Database,
    settings: Settings,
    service: MembershipService,
) -> None:
    """Send a DM to all active tracked members of the selected chat (admins only)."""
    chat = await resolve_chat(message, bot, db, settings)
    if not chat:
        return
    text = (command.args or "").strip()
    if not text:
        await _usage(message, "/broadcast &lt;message&gt; — DM all active members of this chat")
        return
    total = await db.count_members(chat.chat_id, "active")
    note = await message.reply(f"📣 Broadcasting to {total} members…")

    async def progress(done: int, total_: int) -> None:
        try:
            await note.edit_text(f"📣 Broadcasting… <b>{done}</b>/{total_}")
        except Exception:  # noqa: BLE001
            pass

    sent, total = await service.broadcast(chat, text, message.from_user.id, progress=progress)
    try:
        await note.edit_text(f"📣 Broadcast delivered to <b>{sent}</b>/{total} members.")
    except Exception:  # noqa: BLE001
        await message.reply(f"📣 Broadcast delivered to <b>{sent}</b>/{total} members.")


# ------------------------------------------------------------- join prompts
@router.message(Command("pending", "requests"))
async def cmd_pending(
    message: Message, bot: Bot, db: Database, settings: Settings, service: MembershipService
) -> None:
    """List joins that are waiting for an admin decision; re-send the prompt for each."""
    chat = await resolve_chat(message, bot, db, settings)
    if not chat:
        return
    from bot.handlers.callbacks import pending_text  # local import avoids a cycle

    text, ids = await pending_text(db, chat, settings)
    if message.chat.type != ChatType.PRIVATE:
        me = await bot.me()
        await message.reply(text, reply_markup=open_private_keyboard(me.username or ""))
        return
    await message.answer(text, reply_markup=pending_keyboard(chat.chat_id, ids))


@router.message(Command("ask"))
async def cmd_ask(
    message: Message,
    command: CommandObject,
    bot: Bot,
    db: Database,
    settings: Settings,
    service: MembershipService,
) -> None:
    """/ask <user> — (re)send the duration prompt for a member to the owner/admins."""
    chat = await resolve_chat(message, bot, db, settings)
    if not chat:
        return
    uid, _ = await resolve_target(message, command, service, chat)
    if uid is None:
        await _usage(message, "/ask &lt;user&gt; — ask the owner how long this member may stay")
        return
    member = await ensure_member(message, db, chat, uid, bot)
    if not member:
        return
    pending = await service.ask_owner_about_member(chat, member, source="manual")
    if pending:
        await message.reply("🔔 Prompt sent to the owner/admins.")
    else:
        await message.reply(
            "⚠️ Nobody could be reached. The owner must /start me in private chat first.",
            reply_markup=None,
        )


# ------------------------------------------------------------- invite links
@router.message(Command("invite", "newlink"))
async def cmd_invite(
    message: Message,
    command: CommandObject,
    bot: Bot,
    db: Database,
    settings: Settings,
    service: MembershipService,
) -> None:
    """/invite <duration|never> [name] — create an invite link with a preset membership length."""
    chat = await resolve_chat(message, bot, db, settings)
    if not chat:
        return
    args = (command.args or "").split(maxsplit=1)
    if not args:
        await _usage(message, "/invite &lt;1m | 3m | 1y | never&gt; [label]  e.g. <code>/invite 3m Gold plan</code>")
        return
    duration = args[0].lower()
    name = args[1].strip() if len(args) > 1 else None
    url, msg = await service.create_invite_link(chat, duration, name, message.from_user.id)
    if not url:
        await message.reply(f"❌ {escape(msg)}")
        return
    await message.reply(
        f"🔗 <b>Invite link created</b>\n\n"
        f"📛 {escape(msg)} · ⏳ <b>{escape(describe_duration(duration))}</b>\n\n"
        f"<code>{escape(url)}</code>"
    )


@router.message(Command("invites", "links"))
async def cmd_invites(message: Message, bot: Bot, db: Database, settings: Settings) -> None:
    chat = await resolve_chat(message, bot, db, settings)
    if not chat:
        return
    from bot.handlers.callbacks import invites_text  # local import avoids a cycle

    links = await db.list_invite_links(chat.chat_id)
    if message.chat.type != ChatType.PRIVATE:
        me = await bot.me()
        await message.reply(
            f"🔗 {len(links)} active invite link(s). Manage them privately.",
            reply_markup=open_private_keyboard(me.username or ""),
        )
        return
    await message.answer(invites_text(chat, links), reply_markup=invites_keyboard(chat, links))


@router.message(Command("revoke"))
async def cmd_revoke(
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
    arg = (command.args or "").strip()
    if not arg:
        await _usage(message, "/revoke &lt;invite link&gt;")
        return
    target = next(
        (l for l in await db.list_invite_links(chat.chat_id) if l.invite_link.endswith(arg.split("/")[-1])),
        None,
    )
    if not target:
        await message.reply("⚠️ I don't know that link. Use /invites to see the ones I created.")
        return
    ok, msg = await service.revoke_invite_link(chat, target.invite_link, message.from_user.id)
    await message.reply("🗑 Link revoked." if ok else f"⚠️ Marked revoked locally; Telegram said: {escape(msg)}")


# --------------------------------------------------------------- utilities
@router.message(Command("search", "find"))
async def cmd_search(
    message: Message, command: CommandObject, bot: Bot, db: Database, settings: Settings
) -> None:
    chat = await resolve_chat(message, bot, db, settings)
    if not chat:
        return
    query = (command.args or "").strip()
    if len(query) < 2:
        await _usage(message, "/search &lt;name | @username | id&gt;")
        return
    found = await db.search_members(chat.chat_id, query, limit=15)
    if not found:
        await message.reply("🔍 No matches.")
        return
    now = datetime.now(timezone.utc)
    lines = [f"🔍 <b>Search: {escape(query)}</b>", ""]
    for m in found:
        rem = "♾" if m.expires_at is None else humanize_delta(m.expires_at - now)
        lines.append(f"• {m.mention_html} <code>{m.user_id}</code> — {m.status} — ⏳ {rem}")
    await message.reply("\n".join(lines))


@router.message(Command("sync"))
async def cmd_sync(
    message: Message, bot: Bot, db: Database, settings: Settings, service: MembershipService
) -> None:
    """Cross-check tracked members against Telegram (marks people who left)."""
    chat = await resolve_chat(message, bot, db, settings)
    if not chat:
        return
    total = await db.count_members(chat.chat_id, "active")
    if total > 2000:
        await message.reply("⚠️ Too many members to sync interactively (limit 2000).")
        return
    note = await message.reply(f"🔄 Syncing {total} members with Telegram…")
    result = await service.sync_chat_members(chat)
    await note.edit_text(
        f"✅ <b>Sync complete</b>\nChecked: <b>{result['checked']}</b> • "
        f"Left/removed: <b>{result['gone']}</b> • Errors: <b>{result['errors']}</b>"
    )


@router.message(Command("backup"))
async def cmd_backup(message: Message, db: Database, settings: Settings) -> None:
    """Super-admins: receive a copy of the database file."""
    if message.from_user.id not in settings.super_admins:
        return
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M")
    dest = Path(settings.database_path).with_name(f"backup_{stamp}.db")
    await db.backup_to(str(dest))
    try:
        await message.answer_document(
            FSInputFile(str(dest)), caption=f"🗄 Database backup {stamp} UTC ({dest.stat().st_size // 1024} KB)"
        )
    finally:
        dest.unlink(missing_ok=True)


@router.message(Command("health"))
async def cmd_health(
    message: Message, db: Database, settings: Settings, scheduler: ExpiryScheduler, metrics: Metrics | None = None
) -> None:
    if message.from_user.id not in settings.super_admins:
        return
    ok = await db.healthcheck()
    last = await db.kv_get("last_check")
    last_txt = format_dt(datetime.fromisoformat(last), settings.tz) if last else "never"
    lines = [
        "🩺 <b>Health</b>",
        f"Database: {'✅' if ok else '❌'} · {db.db_size_bytes() / 1024:.0f} KB",
        f"Scheduler: {'✅ running' if scheduler.running else '❌ stopped'} · every {settings.check_interval}s",
        f"Last expiry check: {last_txt} ({scheduler.last_run_duration * 1000:.0f} ms)",
        f"Mode: {'webhook' if settings.webhook_url else 'polling'}"
        + (f" · HTTP :{settings.http_port}" if settings.http_port else ""),
    ]
    if metrics is not None:
        snap = metrics.snapshot()
        c = snap["counters"]
        lat = snap["latency"].get("handler_latency", {})
        cache = db.cache_stats()
        lines += [
            "",
            f"⏱ Uptime: <b>{humanize_delta(timedelta(seconds=snap['uptime_seconds']))}</b>",
            f"📨 Updates: <b>{c.get('updates_total', 0)}</b> · errors: {c.get('errors_total', 0)} · throttled: {c.get('throttled_total', 0)}",
            f"⚡ Handler latency: avg {lat.get('mean_ms', 0)} ms · max {lat.get('max_ms', 0)} ms",
            f"🗄 Cache hit-rate: chats {cache['chats']['hit_ratio']:.0%} · admins {cache['admins']['hit_ratio']:.0%}"
            f" · queries {cache['queries']} · writes {cache['writes']}",
            f"🔁 Removed: {c.get('members_removed', 0)} · reminders: {c.get('reminders_sent', 0)} · ticks: {c.get('scheduler_ticks', 0)}",
        ]
        if snap["last_error"]:
            lines.append(f"🐞 Last error: <code>{escape(snap['last_error'])}</code>")
    await message.reply("\n".join(lines))
