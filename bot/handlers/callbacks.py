"""Inline button callbacks: home, dashboard, settings, member cards, join prompts, invite links."""
from __future__ import annotations

import logging
import re
from datetime import datetime
from html import escape

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from bot.config import Settings
from bot.handlers.admin import dashboard_text, list_text, logs_text, settings_text, stats_text
from bot.handlers.common import HELP_TOPICS, home_text, mystatus_text
from bot.services.database import Chat, Database
from bot.services.membership import MembershipService
from bot.utils.keyboards import (
    advanced_keyboard,
    back_keyboard,
    cancel_keyboard,
    chats_keyboard,
    confirm_keyboard,
    dashboard_keyboard,
    duration_keyboard,
    help_keyboard,
    home_keyboard,
    invite_pick_keyboard,
    invites_keyboard,
    join_more_keyboard,
    join_prompt_keyboard,
    join_remove_confirm_keyboard,
    list_keyboard,
    member_keyboard,
    member_more_keyboard,
    pending_keyboard,
    preset_label,
    settings_keyboard,
)
from bot.utils.permissions import is_admin
from bot.utils.timeparse import ParseError, describe_duration, format_dt, humanize_delta

log = logging.getLogger(__name__)
router = Router(name="callbacks")


class CustomInput(StatesGroup):
    """Waiting for the admin to type a custom duration / date."""

    join_duration = State()  # data: pending_id, prompt_message_id
    member_expiry = State()  # data: chat_id, user_id, prompt_message_id


# ------------------------------------------------------------------ helpers
async def _edit(call: CallbackQuery, text: str, markup=None) -> None:
    if call.message is None:
        return
    try:
        await call.message.edit_text(text, reply_markup=markup, disable_web_page_preview=True)
    except TelegramBadRequest as exc:
        if "message is not modified" not in str(exc):
            log.debug("edit failed: %s", exc)


async def _authorised(
    call: CallbackQuery, bot: Bot, db: Database, settings: Settings, chat_id: int
) -> Chat | None:
    chat = await db.get_chat(chat_id)
    if not chat:
        await call.answer("This chat is no longer tracked.", show_alert=True)
        return None
    if not await is_admin(bot, db, settings, chat_id, call.from_user.id):
        await call.answer("⛔ Admins only", show_alert=True)
        return None
    return chat


def _settings_markup(chat: Chat, settings: Settings):
    return settings_keyboard(chat, settings.default_duration, settings.ask_on_join_default)


async def _show_dashboard(call: CallbackQuery, db: Database, settings: Settings, chat: Chat) -> None:
    pending = await db.count_pending(chat.chat_id)
    await _edit(call, await dashboard_text(db, chat, settings), dashboard_keyboard(chat, pending))


async def _show_settings(call: CallbackQuery, db: Database, settings: Settings, chat_id: int) -> None:
    chat = await db.get_chat(chat_id)
    if chat:
        await _edit(call, settings_text(chat, settings), _settings_markup(chat, settings))


async def _show_member(
    call: CallbackQuery, db: Database, service: MembershipService, chat: Chat, uid: int
) -> None:
    member = await db.get_member(chat.chat_id, uid)
    if not member:
        await call.answer("Member is not tracked.", show_alert=True)
        return
    await _edit(call, service.member_card(chat, member), member_keyboard(chat.chat_id, uid, member.is_active))


def _ids(data: str, count: int) -> list[str]:
    parts = data.split(":")
    if len(parts) < count + 1:
        raise ValueError("bad callback data")
    return parts[1 : count + 1]


async def _admin_chats(db: Database, settings: Settings, user_id: int) -> list[Chat]:
    if user_id in settings.super_admins:
        return await db.list_chats()
    return await db.chats_for_admin(user_id)


# ------------------------------------------------------------- home / help
@router.callback_query(F.data == "home")
async def cb_home(call: CallbackQuery, bot: Bot, db: Database, settings: Settings, state: FSMContext) -> None:
    await state.clear()
    chats = await _admin_chats(db, settings, call.from_user.id)
    is_admin_user = call.from_user.id in settings.super_admins or bool(chats)
    me = await bot.me()
    await _edit(
        call,
        home_text(call.from_user.first_name, is_admin_user, bool(chats)),
        home_keyboard(is_admin_user, me.username or "", bool(chats)),
    )
    await call.answer()


@router.callback_query(F.data.startswith("help:"))
async def cb_help(call: CallbackQuery) -> None:
    topic = _ids(call.data, 1)[0]
    text = HELP_TOPICS.get(topic) or HELP_TOPICS["main"]
    await _edit(call, text, help_keyboard(topic if topic in HELP_TOPICS else "main"))
    await call.answer()


@router.callback_query(F.data == "mystatus")
async def cb_mystatus(call: CallbackQuery, db: Database, settings: Settings) -> None:
    kb = help_keyboard("x")  # "back to help / home" pair
    await _edit(call, await mystatus_text(db, settings, call.from_user.id), kb)
    await call.answer()


@router.callback_query(F.data == "chats")
async def cb_chats(call: CallbackQuery, db: Database, settings: Settings, state: FSMContext) -> None:
    await state.clear()
    chats = await _admin_chats(db, settings, call.from_user.id)
    if not chats:
        await call.answer("No chats yet — add me to a group as admin.", show_alert=True)
        return
    current = await db.get_context(call.from_user.id)
    await _edit(call, "📂 <b>Your chats</b>\nChoose one to manage:", chats_keyboard(chats, current))
    await call.answer()


# -------------------------------------------------------------- dashboard
@router.callback_query(F.data.startswith("dash:"))
async def cb_dashboard(call: CallbackQuery, bot: Bot, db: Database, settings: Settings, state: FSMContext) -> None:
    chat_id = int(_ids(call.data, 1)[0])
    chat = await _authorised(call, bot, db, settings, chat_id)
    if not chat:
        return
    await state.clear()
    await db.set_context(call.from_user.id, chat_id)
    await _show_dashboard(call, db, settings, chat)
    await call.answer()


# legacy callback names from older messages still work
@router.callback_query(F.data.startswith("ctx:"))
@router.callback_query(F.data.startswith("panel:"))
async def cb_legacy_dashboard(call: CallbackQuery, bot: Bot, db: Database, settings: Settings, state: FSMContext) -> None:
    await cb_dashboard(call, bot, db, settings, state)


@router.callback_query(F.data.startswith("settings:"))
async def cb_settings(call: CallbackQuery, bot: Bot, db: Database, settings: Settings, state: FSMContext) -> None:
    chat_id = int(_ids(call.data, 1)[0])
    chat = await _authorised(call, bot, db, settings, chat_id)
    if not chat:
        return
    await state.clear()
    await _edit(call, settings_text(chat, settings), _settings_markup(chat, settings))
    await call.answer()


@router.callback_query(F.data.startswith("adv:"))
async def cb_advanced(call: CallbackQuery, bot: Bot, db: Database, settings: Settings) -> None:
    chat_id = int(_ids(call.data, 1)[0])
    chat = await _authorised(call, bot, db, settings, chat_id)
    if not chat:
        return
    await _edit(call, _advanced_text(chat), advanced_keyboard(chat))
    await call.answer()


def _advanced_text(chat: Chat) -> str:
    log_line = f"<code>{chat.log_chat_id}</code>" if chat.log_chat_id else "off · <code>/setlog here</code>"
    welcome = "custom" if chat.welcome_text else "default text · <code>/setwelcome …</code>"
    return (
        f"🔧 <b>Advanced — {escape(chat.display)}</b>\n\n"
        f"• <b>DM members</b>: expiry reminders and confirmations by private message\n"
        f"• <b>Welcome</b>: greet new members ({welcome})\n"
        f"• <b>Join requests</b>: ignore, auto-approve, or ask you with a duration\n"
        f"• <b>Prompts</b>: who receives the join prompts\n\n"
        f"📨 Log channel: {log_line}"
    )


# ------------------------------------------------------------------ toggles
@router.callback_query(F.data.startswith("set:"))
async def cb_toggle(
    call: CallbackQuery, bot: Bot, db: Database, settings: Settings, service: MembershipService
) -> None:
    chat_id_s, key = _ids(call.data, 2)
    chat_id = int(chat_id_s)
    chat = await _authorised(call, bot, db, settings, chat_id)
    if not chat:
        return
    if key == "duration":
        current = chat.default_duration or settings.default_duration
        await _edit(
            call,
            f"⏳ <b>Default duration — {escape(chat.display)}</b>\n\n"
            f"Currently <b>{escape(describe_duration(current))}</b>"
            + (" (global default)" if not chat.default_duration else "")
            + ".\n\nNew members get this unless you pick something else on their join prompt.\n"
            "<i>Any other value: <code>/setduration 45d</code></i>",
            duration_keyboard(chat_id),
        )
        await call.answer()
        return

    updates: dict[str, object] = {}
    note = "Saved"
    advanced = key in ("notify", "welcome", "approve", "asktarget")
    if key == "mode":
        updates["kick_mode"] = "ban" if chat.kick_mode == "kick" else "kick"
        note = "Expired members are banned" if updates["kick_mode"] == "ban" else "Expired members are kicked (can rejoin)"
    elif key == "tracking":
        updates["tracking_enabled"] = int(not chat.tracking_enabled)
        note = "Tracking resumed" if updates["tracking_enabled"] else "Tracking paused"
    elif key == "autokick":
        updates["auto_kick"] = int(not chat.auto_kick)
        note = "Auto-remove on" if updates["auto_kick"] else "Auto-remove off"
    elif key == "notify":
        updates["notify_user"] = int(not chat.notify_user)
    elif key == "welcome":
        updates["welcome_enabled"] = int(not chat.welcome_enabled)
    elif key == "ask":
        updates["ask_on_join"] = int(not service.ask_enabled(chat))
        note = "You'll be asked on every join" if updates["ask_on_join"] else "Default applied silently"
    elif key == "asktarget":
        updates["ask_target"] = "admins" if chat.ask_target == "owner" else "owner"
        note = "Prompts go to all admins" if updates["ask_target"] == "admins" else "Prompts go to the owner"
    elif key == "approve":
        updates["approve_requests"] = (chat.approve_requests + 1) % 3
        note = {0: "Join requests ignored", 1: "Join requests auto-approved", 2: "You'll be asked on each request"}[
            updates["approve_requests"]
        ]
    else:
        await call.answer("Unknown setting")
        return

    await db.update_chat(chat_id, **updates)
    await db.add_log(chat_id, None, f"toggle_{key}", str(list(updates.values())[0]), call.from_user.id)
    chat = await db.get_chat(chat_id)
    if advanced:
        await _edit(call, _advanced_text(chat), advanced_keyboard(chat))
    else:
        await _edit(call, settings_text(chat, settings), _settings_markup(chat, settings))
    await call.answer(note)


@router.callback_query(F.data.startswith("dur:"))
async def cb_duration(call: CallbackQuery, bot: Bot, db: Database, settings: Settings) -> None:
    chat_id_s, value = _ids(call.data, 2)
    chat_id = int(chat_id_s)
    chat = await _authorised(call, bot, db, settings, chat_id)
    if not chat:
        return
    await db.update_chat(chat_id, default_duration=None if value == "global" else value)
    await db.add_log(chat_id, None, "set_duration", value, call.from_user.id)
    await _show_settings(call, db, settings, chat_id)
    await call.answer(f"Default: {describe_duration(settings.default_duration if value == 'global' else value)}")


# ------------------------------------------------------------------- views
@router.callback_query(F.data.startswith("stats:"))
async def cb_stats(call: CallbackQuery, bot: Bot, db: Database, settings: Settings) -> None:
    chat_id = int(_ids(call.data, 1)[0])
    chat = await _authorised(call, bot, db, settings, chat_id)
    if not chat:
        return
    await _edit(call, await stats_text(db, chat, settings), back_keyboard(chat_id))
    await call.answer()


@router.callback_query(F.data.startswith("logs:"))
async def cb_logs(call: CallbackQuery, bot: Bot, db: Database, settings: Settings) -> None:
    chat_id = int(_ids(call.data, 1)[0])
    chat = await _authorised(call, bot, db, settings, chat_id)
    if not chat:
        return
    await _edit(call, await logs_text(db, chat, settings), back_keyboard(chat_id))
    await call.answer()


@router.callback_query(F.data.startswith("list:"))
async def cb_list(call: CallbackQuery, bot: Bot, db: Database, settings: Settings, state: FSMContext) -> None:
    chat_id_s, page_s = _ids(call.data, 2)
    chat_id, page = int(chat_id_s), int(page_s)
    chat = await _authorised(call, bot, db, settings, chat_id)
    if not chat:
        return
    await state.clear()
    text, has_next = await list_text(db, chat, settings, page)
    await _edit(call, text, list_keyboard(chat_id, page, has_next))
    await call.answer()


# ------------------------------------------------------------ member cards
@router.callback_query(F.data.startswith("member:"))
async def cb_member(
    call: CallbackQuery, bot: Bot, db: Database, settings: Settings, service: MembershipService, state: FSMContext
) -> None:
    chat_id_s, uid_s = _ids(call.data, 2)
    chat_id, uid = int(chat_id_s), int(uid_s)
    chat = await _authorised(call, bot, db, settings, chat_id)
    if not chat:
        return
    await state.clear()
    await _show_member(call, db, service, chat, uid)
    await call.answer()


@router.callback_query(F.data.startswith("more:"))
async def cb_member_more(call: CallbackQuery, bot: Bot, db: Database, settings: Settings, service: MembershipService) -> None:
    chat_id_s, uid_s = _ids(call.data, 2)
    chat_id, uid = int(chat_id_s), int(uid_s)
    chat = await _authorised(call, bot, db, settings, chat_id)
    if not chat:
        return
    member = await db.get_member(chat_id, uid)
    if not member:
        await call.answer("Member is not tracked.", show_alert=True)
        return
    wl = await db.is_whitelisted(chat_id, uid)
    await _edit(call, service.member_card(chat, member), member_more_keyboard(chat_id, uid, member.is_active, wl))
    await call.answer()


@router.callback_query(F.data.startswith("ext:"))
async def cb_extend(
    call: CallbackQuery, bot: Bot, db: Database, settings: Settings, service: MembershipService
) -> None:
    chat_id_s, uid_s, value = _ids(call.data, 3)
    chat_id, uid = int(chat_id_s), int(uid_s)
    chat = await _authorised(call, bot, db, settings, chat_id)
    if not chat:
        return
    member = await db.get_member(chat_id, uid)
    if not member:
        await call.answer("Member is not tracked.", show_alert=True)
        return
    try:
        new_expiry = await service.extend_member(chat, member, value, call.from_user.id)
    except ParseError as exc:
        await call.answer(f"Error: {exc}", show_alert=True)
        return
    await _show_member(call, db, service, chat, uid)
    await call.answer("Lifetime access" if new_expiry is None else f"Until {format_dt(new_expiry, settings.tz)}")
    await service.notify_extension(chat, uid, new_expiry)


@router.callback_query(F.data.startswith("cust:"))
async def cb_custom_expiry(
    call: CallbackQuery, bot: Bot, db: Database, settings: Settings, state: FSMContext
) -> None:
    """Ask the admin to type a custom expiry for a member."""
    chat_id_s, uid_s = _ids(call.data, 2)
    chat_id, uid = int(chat_id_s), int(uid_s)
    chat = await _authorised(call, bot, db, settings, chat_id)
    if not chat:
        return
    if call.message and call.message.chat.type != ChatType.PRIVATE:
        await call.answer("Use this from my private chat.", show_alert=True)
        return
    await state.set_state(CustomInput.member_expiry)
    await state.update_data(chat_id=chat_id, user_id=uid, prompt_message_id=call.message.message_id if call.message else None)
    await _edit(
        call,
        f"✏️ <b>Custom expiry</b> · <code>{uid}</code>\n\n"
        "Send a duration to add, or an exact date:\n"
        "<code>45d</code> · <code>1m 15d</code> · <code>2025-12-31</code> · "
        "<code>31/12/2025 18:30</code> · <code>never</code>",
        cancel_keyboard(f"member:{chat_id}:{uid}"),
    )
    await call.answer()


@router.callback_query(F.data.startswith("hist:"))
async def cb_history(call: CallbackQuery, bot: Bot, db: Database, settings: Settings) -> None:
    chat_id_s, uid_s = _ids(call.data, 2)
    chat_id, uid = int(chat_id_s), int(uid_s)
    chat = await _authorised(call, bot, db, settings, chat_id)
    if not chat:
        return
    rows = await db.user_logs(chat_id, uid, 12)
    lines = [f"📜 <b>History</b> · <code>{uid}</code> · {escape(chat.display)}", ""]
    if not rows:
        lines.append("<i>No events recorded.</i>")
    for r in rows:
        ts = format_dt(datetime.fromisoformat(r["created_at"]), settings.tz)
        det = f" <i>{escape(str(r['details']))[:50]}</i>" if r["details"] else ""
        lines.append(f"• {ts} — <b>{escape(r['action'])}</b>{det}")
    await _edit(call, "\n".join(lines), confirm_back(chat_id, uid))
    await call.answer()


def confirm_back(chat_id: int, uid: int):
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="◀️ Back", callback_data=f"member:{chat_id}:{uid}")]]
    )


@router.callback_query(F.data.startswith("kick:"))
async def cb_kick_confirm(call: CallbackQuery, bot: Bot, db: Database, settings: Settings) -> None:
    chat_id_s, uid_s = _ids(call.data, 2)
    chat_id, uid = int(chat_id_s), int(uid_s)
    chat = await _authorised(call, bot, db, settings, chat_id)
    if not chat:
        return
    member = await db.get_member(chat_id, uid)
    who = member.mention_html if member else f"<code>{uid}</code>"
    await _edit(call, f"⚠️ Remove {who} from <b>{escape(chat.display)}</b> now?", confirm_keyboard("kick", chat_id, uid))
    await call.answer()


@router.callback_query(F.data.startswith("kickc:"))
async def cb_kick(
    call: CallbackQuery, bot: Bot, db: Database, settings: Settings, service: MembershipService
) -> None:
    chat_id_s, uid_s = _ids(call.data, 2)
    chat_id, uid = int(chat_id_s), int(uid_s)
    chat = await _authorised(call, bot, db, settings, chat_id)
    if not chat:
        return
    member = await db.get_member(chat_id, uid)
    if not member:
        await call.answer("Member is not tracked.", show_alert=True)
        return
    ok, msg = await service.remove_member(chat, member, reason="manual", actor_id=call.from_user.id)
    await _show_member(call, db, service, chat, uid)
    await call.answer("Removed" if ok else f"Failed: {msg}", show_alert=not ok)


@router.callback_query(F.data.startswith("untrack:"))
async def cb_untrack_confirm(call: CallbackQuery, bot: Bot, db: Database, settings: Settings) -> None:
    chat_id_s, uid_s = _ids(call.data, 2)
    chat_id, uid = int(chat_id_s), int(uid_s)
    chat = await _authorised(call, bot, db, settings, chat_id)
    if not chat:
        return
    await _edit(
        call,
        f"🗑 Stop tracking <code>{uid}</code>?\n\n<i>They stay in the chat but will no longer expire.</i>",
        confirm_keyboard("untrack", chat_id, uid),
    )
    await call.answer()


@router.callback_query(F.data.startswith("untrackc:"))
async def cb_untrack(call: CallbackQuery, bot: Bot, db: Database, settings: Settings) -> None:
    chat_id_s, uid_s = _ids(call.data, 2)
    chat_id, uid = int(chat_id_s), int(uid_s)
    chat = await _authorised(call, bot, db, settings, chat_id)
    if not chat:
        return
    await db.delete_member(chat_id, uid)
    await db.add_log(chat_id, uid, "untrack", None, call.from_user.id)
    text, has_next = await list_text(db, chat, settings, 0)
    await _edit(call, text, list_keyboard(chat_id, 0, has_next))
    await call.answer("Stopped tracking")


@router.callback_query(F.data.startswith("wl:"))
async def cb_whitelist(
    call: CallbackQuery, bot: Bot, db: Database, settings: Settings, service: MembershipService
) -> None:
    chat_id_s, uid_s = _ids(call.data, 2)
    chat_id, uid = int(chat_id_s), int(uid_s)
    chat = await _authorised(call, bot, db, settings, chat_id)
    if not chat:
        return
    if await db.is_whitelisted(chat_id, uid):
        await db.remove_whitelist(chat_id, uid)
        member = await db.get_member(chat_id, uid)
        if member and member.expires_at is None:
            await db.set_member_expiry(chat_id, uid, service.compute_expiry(chat))
        await db.add_log(chat_id, uid, "whitelist_remove", None, call.from_user.id)
        note = "Protection removed"
    else:
        await db.add_whitelist(chat_id, uid, call.from_user.id)
        await db.set_member_expiry(chat_id, uid, None)
        await db.add_log(chat_id, uid, "whitelist_add", None, call.from_user.id)
        note = "Protected — never auto-removed"
    await _show_member(call, db, service, chat, uid)
    await call.answer(note)


# ------------------------------------------------------------ join prompts
async def _pending_for_admin(call: CallbackQuery, bot: Bot, db: Database, settings: Settings, pid: int):
    pending = await db.get_pending(pid)
    if not pending:
        await call.answer("This request no longer exists.", show_alert=True)
        return None, None
    chat = await db.get_chat(pending.chat_id)
    if not chat or not await is_admin(bot, db, settings, pending.chat_id, call.from_user.id):
        await call.answer("⛔ Admins only", show_alert=True)
        return None, None
    return pending, chat


@router.callback_query(F.data.startswith("jd:"))
async def cb_join_decision(
    call: CallbackQuery, bot: Bot, db: Database, settings: Settings, service: MembershipService, state: FSMContext
) -> None:
    pid_s, value = _ids(call.data, 2)
    pending, chat = await _pending_for_admin(call, bot, db, settings, int(pid_s))
    if not pending:
        return
    await state.clear()
    if pending.source == "request":
        ok, outcome = await service.apply_request_decision(pending, value, call.from_user.id, call.from_user.full_name)
    else:
        ok, outcome = await service.apply_join_decision(pending, value, call.from_user.id, call.from_user.full_name)
    if not ok and "Already" in outcome:
        await call.answer(outcome, show_alert=True)
        return
    await call.answer("Done" if ok else outcome, show_alert=not ok)


@router.callback_query(F.data.startswith("jm:"))
async def cb_join_more(call: CallbackQuery, bot: Bot, db: Database, settings: Settings) -> None:
    pid = int(_ids(call.data, 1)[0])
    pending, chat = await _pending_for_admin(call, bot, db, settings, pid)
    if not pending:
        return
    if pending.status != "pending":
        await call.answer("Already handled.", show_alert=True)
        return
    if call.message:
        await call.message.edit_reply_markup(reply_markup=join_more_keyboard(pid))
    await call.answer()


@router.callback_query(F.data.startswith("jr:"))
async def cb_join_remove_confirm(call: CallbackQuery, bot: Bot, db: Database, settings: Settings) -> None:
    pid = int(_ids(call.data, 1)[0])
    pending, chat = await _pending_for_admin(call, bot, db, settings, pid)
    if not pending:
        return
    if pending.status != "pending":
        await call.answer("Already handled.", show_alert=True)
        return
    is_req = pending.source == "request"
    verb = "Reject" if is_req else "Remove"
    await _edit(
        call,
        f"⚠️ <b>{verb} {pending.mention_html}</b> (<code>{pending.user_id}</code>) from {escape(chat.display)}?",
        join_remove_confirm_keyboard(pid, is_req),
    )
    await call.answer()


@router.callback_query(F.data.startswith("jb:"))
async def cb_join_back(
    call: CallbackQuery, bot: Bot, db: Database, settings: Settings, service: MembershipService, state: FSMContext
) -> None:
    """Show (again) the prompt for a pending join."""
    pid = int(_ids(call.data, 1)[0])
    pending, chat = await _pending_for_admin(call, bot, db, settings, pid)
    if not pending:
        return
    await state.clear()
    if pending.status != "pending":
        await call.answer(f"Already handled ({pending.decision or pending.status}).", show_alert=True)
        return
    member = await db.get_member(chat.chat_id, pending.user_id)
    await _edit(
        call,
        service.join_prompt_text(chat, pending, member),
        join_prompt_keyboard(pid, service.effective_duration(chat), pending.source == "request"),
    )
    if call.message:
        await db.add_prompt_message(pid, call.from_user.id, call.message.message_id)
    await call.answer()


@router.callback_query(F.data.startswith("jc:"))
async def cb_join_custom(
    call: CallbackQuery, bot: Bot, db: Database, settings: Settings, state: FSMContext
) -> None:
    pid = int(_ids(call.data, 1)[0])
    pending, chat = await _pending_for_admin(call, bot, db, settings, pid)
    if not pending:
        return
    if pending.status != "pending":
        await call.answer("Already handled.", show_alert=True)
        return
    await state.set_state(CustomInput.join_duration)
    await state.update_data(pending_id=pid, prompt_message_id=call.message.message_id if call.message else None)
    await _edit(
        call,
        f"✏️ <b>Custom duration for {pending.mention_html}</b>\n\n"
        "Send how long they may stay, or an exact date:\n"
        "<code>45d</code> · <code>1m 15d</code> · <code>2025-12-31</code> · "
        "<code>31/12/2025 18:30</code> · <code>never</code>",
        cancel_keyboard(f"jb:{pid}"),
    )
    await call.answer()


@router.callback_query(F.data.startswith("pending:"))
async def cb_pending(call: CallbackQuery, bot: Bot, db: Database, settings: Settings) -> None:
    chat_id = int(_ids(call.data, 1)[0])
    chat = await _authorised(call, bot, db, settings, chat_id)
    if not chat:
        return
    text, ids = await pending_text(db, chat, settings)
    await _edit(call, text, pending_keyboard(chat_id, ids))
    await call.answer()


async def pending_text(db: Database, chat: Chat, settings: Settings) -> tuple[str, list[int]]:
    items = await db.list_pending(chat.chat_id, limit=20)
    lines = [f"🔔 <b>Pending — {escape(chat.display)}</b> · {len(items)}", ""]
    if not items:
        lines.append("<i>Nothing waiting. New joins will appear here until you answer their prompt.</i>")
    else:
        lines.append("<i>Tap a number to open the prompt.</i>")
        lines.append("")
    now = datetime.now(settings.tz)
    for p in items:
        age = humanize_delta(now - p.created_at.astimezone(settings.tz))
        kind = "requested" if p.source == "request" else "joined"
        lines.append(f"<b>#{p.id}</b> {p.mention_html} · {kind} {age} ago")
    return "\n".join(lines), [p.id for p in items]


# ------------------------------------------------------------ invite links
@router.callback_query(F.data.startswith("invites:"))
async def cb_invites(call: CallbackQuery, bot: Bot, db: Database, settings: Settings) -> None:
    chat_id = int(_ids(call.data, 1)[0])
    chat = await _authorised(call, bot, db, settings, chat_id)
    if not chat:
        return
    links = await db.list_invite_links(chat_id)
    await _edit(call, invites_text(chat, links), invites_keyboard(chat, links))
    await call.answer()


def invites_text(chat: Chat, links) -> str:
    lines = [f"🔗 <b>Invite links — {escape(chat.display)}</b>", ""]
    if not links:
        lines.append(
            "Anyone joining through a link gets its preset duration automatically — "
            "no prompt, no manual work.\n\n<i>Tap ➕ New link to create one.</i>"
        )
    for link in links[:8]:
        lines.append(
            f"<b>{escape(link.name or preset_label(link.duration))}</b> · {escape(describe_duration(link.duration))} "
            f"· {link.uses} joined\n<code>{escape(link.invite_link)}</code>\n"
        )
    return "\n".join(lines)


@router.callback_query(F.data.startswith("inpick:"))
async def cb_invite_pick(call: CallbackQuery, bot: Bot, db: Database, settings: Settings) -> None:
    chat_id = int(_ids(call.data, 1)[0])
    chat = await _authorised(call, bot, db, settings, chat_id)
    if not chat:
        return
    await _edit(
        call,
        f"➕ <b>New invite link — {escape(chat.display)}</b>\n\n"
        "How long should members joining through this link stay?\n"
        "<i>To add a label: <code>/invite 3m Gold plan</code></i>",
        invite_pick_keyboard(chat_id),
    )
    await call.answer()


@router.callback_query(F.data.startswith("inew:"))
async def cb_invite_new(
    call: CallbackQuery, bot: Bot, db: Database, settings: Settings, service: MembershipService
) -> None:
    chat_id_s, value = _ids(call.data, 2)
    chat_id = int(chat_id_s)
    chat = await _authorised(call, bot, db, settings, chat_id)
    if not chat:
        return
    url, msg = await service.create_invite_link(chat, value, None, call.from_user.id)
    if not url:
        await call.answer(msg, show_alert=True)
        return
    links = await db.list_invite_links(chat_id)
    await _edit(call, invites_text(chat, links), invites_keyboard(chat, links))
    await call.answer(f"Created · {preset_label(value)}")


@router.callback_query(F.data.startswith("irev:"))
async def cb_invite_revoke(
    call: CallbackQuery, bot: Bot, db: Database, settings: Settings, service: MembershipService
) -> None:
    chat_id_s, suffix = _ids(call.data, 2)
    chat_id = int(chat_id_s)
    chat = await _authorised(call, bot, db, settings, chat_id)
    if not chat:
        return
    target = next((l for l in await db.list_invite_links(chat_id) if l.invite_link.endswith(suffix)), None)
    if not target:
        await call.answer("Link not found", show_alert=True)
        return
    ok, msg = await service.revoke_invite_link(chat, target.invite_link, call.from_user.id)
    links = await db.list_invite_links(chat_id)
    await _edit(call, invites_text(chat, links), invites_keyboard(chat, links))
    await call.answer("Revoked" if ok else f"Marked revoked locally: {msg}", show_alert=not ok)


# ------------------------------------------------------------ misc buttons
@router.callback_query(F.data == "cancel")
async def cb_cancel(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await _edit(call, "Cancelled.")
    await call.answer()


@router.callback_query(F.data == "noop")
async def cb_noop(call: CallbackQuery) -> None:
    await call.answer()


# ------------------------------------------------------- FSM text handlers
@router.message(Command("cancel"), StateFilter(CustomInput))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Cancelled.")


@router.message(CustomInput.join_duration, F.text, F.chat.type == ChatType.PRIVATE)
async def on_join_custom_text(
    message: Message, bot: Bot, db: Database, settings: Settings, service: MembershipService, state: FSMContext
) -> None:
    data = await state.get_data()
    pid = int(data.get("pending_id", 0))
    pending = await db.get_pending(pid)
    if not pending or pending.status != "pending":
        await state.clear()
        await message.answer("That request was already handled.")
        return
    if not await is_admin(bot, db, settings, pending.chat_id, message.from_user.id):
        await state.clear()
        return
    value = (message.text or "").strip()
    try:
        service.expiry_from_text(value)
    except ParseError as exc:
        await message.reply(
            f"⚠️ {escape(str(exc))}\nTry again (e.g. <code>45d</code>, <code>2025-12-31</code>, <code>never</code>) or /cancel."
        )
        return
    await state.clear()
    if pending.source == "request":
        ok, outcome = await service.apply_request_decision(pending, value, message.from_user.id, message.from_user.full_name)
    else:
        ok, outcome = await service.apply_join_decision(pending, value, message.from_user.id, message.from_user.full_name)
    await message.answer(outcome if ok else f"❌ {outcome}")


@router.message(CustomInput.member_expiry, F.text, F.chat.type == ChatType.PRIVATE)
async def on_member_custom_text(
    message: Message, bot: Bot, db: Database, settings: Settings, service: MembershipService, state: FSMContext
) -> None:
    data = await state.get_data()
    chat_id, uid = int(data.get("chat_id", 0)), int(data.get("user_id", 0))
    chat = await db.get_chat(chat_id)
    if not chat or not await is_admin(bot, db, settings, chat_id, message.from_user.id):
        await state.clear()
        return
    member = await db.get_member(chat_id, uid)
    if not member:
        await state.clear()
        await message.answer("⚠️ Member is no longer tracked.")
        return
    value = (message.text or "").strip()
    try:
        new_expiry = await service.extend_member(chat, member, value, message.from_user.id)
    except ParseError as exc:
        await message.reply(f"⚠️ {escape(str(exc))}\nTry again or /cancel.")
        return
    await state.clear()
    member = await db.get_member(chat_id, uid)
    await message.answer(
        service.member_card(chat, member), reply_markup=member_keyboard(chat_id, uid, member.is_active)
    )
    await service.notify_extension(chat, uid, new_expiry)


# ------------------------------------------- smart lookup: ID / @username
_LOOKUP_RE = re.compile(r"^(@[A-Za-z][A-Za-z0-9_]{3,31}|\d{5,15})$")


@router.message(F.chat.type == ChatType.PRIVATE, F.text.regexp(_LOOKUP_RE), StateFilter(None))
async def on_private_lookup(
    message: Message, bot: Bot, db: Database, settings: Settings, service: MembershipService
) -> None:
    """Typing a user ID or @username in private chat opens the member card of the selected chat."""
    user_id = message.from_user.id
    chat_id = await db.get_context(user_id)
    if chat_id is None:
        chats = await _admin_chats(db, settings, user_id)
        if len(chats) != 1:
            return  # not an admin, or ambiguous → ignore silently
        chat_id = chats[0].chat_id
        await db.set_context(user_id, chat_id)
    chat = await db.get_chat(chat_id)
    if not chat or not await is_admin(bot, db, settings, chat_id, user_id):
        return
    uid, err = await service.resolve_user(chat_id, message.text.strip())
    if uid is None:
        await message.reply(f"🔍 {escape(err or 'Not found.')}")
        return
    member = await db.get_member(chat_id, uid)
    if not member:
        found = await db.search_members(chat_id, message.text.strip().lstrip("@"), limit=5)
        if found:
            lines = [f"🔍 <b>Matches in {escape(chat.display)}</b>", ""]
            lines += [f"• {m.mention_html} · <code>{m.user_id}</code> · {m.status}" for m in found]
            lines.append("\n<i>Send the ID to open a card.</i>")
            await message.reply("\n".join(lines))
            return
        await message.reply(
            f"<code>{uid}</code> is not tracked in <b>{escape(chat.display)}</b>.\n"
            f"Start tracking with <code>/add {uid} 1m</code>."
        )
        return
    await message.answer(service.member_card(chat, member), reply_markup=member_keyboard(chat_id, uid, member.is_active))
