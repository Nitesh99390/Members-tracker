"""Inline button callbacks for the settings panel and member cards."""
from __future__ import annotations

import logging
from html import escape

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery

from bot.config import Settings
from bot.handlers.admin import list_text, logs_text, panel_text, stats_text
from bot.services.database import Database
from bot.services.membership import MembershipService
from bot.utils.keyboards import (
    confirm_keyboard,
    duration_keyboard,
    list_keyboard,
    member_keyboard,
    settings_keyboard,
)
from bot.utils.permissions import is_admin
from bot.utils.timeparse import ParseError, format_dt

log = logging.getLogger(__name__)
router = Router(name="callbacks")


async def _edit(call: CallbackQuery, text: str, markup=None) -> None:
    try:
        await call.message.edit_text(text, reply_markup=markup, disable_web_page_preview=True)
    except TelegramBadRequest as exc:
        if "message is not modified" not in str(exc):
            raise


async def _authorised(call: CallbackQuery, bot: Bot, db: Database, settings: Settings, chat_id: int):
    chat = await db.get_chat(chat_id)
    if not chat:
        await call.answer("Chat not found", show_alert=True)
        return None
    if not await is_admin(bot, db, settings, chat_id, call.from_user.id):
        await call.answer("⛔ Not allowed", show_alert=True)
        return None
    return chat


@router.callback_query(F.data.startswith("ctx:"))
async def cb_select_chat(call: CallbackQuery, bot: Bot, db: Database, settings: Settings) -> None:
    chat_id = int(call.data.split(":")[1])
    chat = await _authorised(call, bot, db, settings, chat_id)
    if not chat:
        return
    await db.set_context(call.from_user.id, chat_id)
    await _edit(call, panel_text(chat, settings), settings_keyboard(chat, settings.default_duration))
    await call.answer(f"Selected: {chat.display}")


@router.callback_query(F.data.startswith("panel:"))
async def cb_panel(call: CallbackQuery, bot: Bot, db: Database, settings: Settings) -> None:
    chat_id = int(call.data.split(":")[1])
    chat = await _authorised(call, bot, db, settings, chat_id)
    if not chat:
        return
    await _edit(call, panel_text(chat, settings), settings_keyboard(chat, settings.default_duration))
    await call.answer()


@router.callback_query(F.data.startswith("set:"))
async def cb_toggle(call: CallbackQuery, bot: Bot, db: Database, settings: Settings) -> None:
    _, chat_id_s, key = call.data.split(":")
    chat_id = int(chat_id_s)
    chat = await _authorised(call, bot, db, settings, chat_id)
    if not chat:
        return
    if key == "duration":
        await _edit(
            call,
            f"⏳ <b>Default duration for {escape(chat.display)}</b>\n\n"
            f"Current: <b>{chat.default_duration or settings.default_duration + ' (global)'}</b>\n"
            f"For a custom value use /setduration &lt;value&gt; e.g. <code>/setduration 45d</code>",
            duration_keyboard(chat_id),
        )
        await call.answer()
        return
    mapping = {
        "tracking": ("tracking_enabled", not chat.tracking_enabled),
        "autokick": ("auto_kick", not chat.auto_kick),
        "notify": ("notify_user", not chat.notify_user),
        "welcome": ("welcome_enabled", not chat.welcome_enabled),
        "approve": ("approve_requests", not chat.approve_requests),
    }
    if key == "mode":
        await db.update_chat(chat_id, kick_mode="ban" if chat.kick_mode == "kick" else "kick")
    elif key in mapping:
        col, val = mapping[key]
        await db.update_chat(chat_id, **{col: int(val)})
    else:
        await call.answer("Unknown setting")
        return
    await db.add_log(chat_id, None, f"toggle_{key}", None, call.from_user.id)
    chat = await db.get_chat(chat_id)
    await _edit(call, panel_text(chat, settings), settings_keyboard(chat, settings.default_duration))
    await call.answer("Updated ✅")


@router.callback_query(F.data.startswith("dur:"))
async def cb_duration(call: CallbackQuery, bot: Bot, db: Database, settings: Settings) -> None:
    _, chat_id_s, value = call.data.split(":")
    chat_id = int(chat_id_s)
    chat = await _authorised(call, bot, db, settings, chat_id)
    if not chat:
        return
    await db.update_chat(chat_id, default_duration=None if value == "global" else value)
    await db.add_log(chat_id, None, "set_duration", value, call.from_user.id)
    chat = await db.get_chat(chat_id)
    await _edit(call, panel_text(chat, settings), settings_keyboard(chat, settings.default_duration))
    await call.answer(f"Duration: {value}")


@router.callback_query(F.data.startswith("stats:"))
async def cb_stats(call: CallbackQuery, bot: Bot, db: Database, settings: Settings) -> None:
    chat_id = int(call.data.split(":")[1])
    chat = await _authorised(call, bot, db, settings, chat_id)
    if not chat:
        return
    await _edit(call, await stats_text(db, chat, settings), list_keyboard(chat_id, 0, False))
    await call.answer()


@router.callback_query(F.data.startswith("logs:"))
async def cb_logs(call: CallbackQuery, bot: Bot, db: Database, settings: Settings) -> None:
    chat_id = int(call.data.split(":")[1])
    chat = await _authorised(call, bot, db, settings, chat_id)
    if not chat:
        return
    await _edit(call, await logs_text(db, chat, settings), list_keyboard(chat_id, 0, False))
    await call.answer()


@router.callback_query(F.data.startswith("list:"))
async def cb_list(call: CallbackQuery, bot: Bot, db: Database, settings: Settings) -> None:
    _, chat_id_s, page_s = call.data.split(":")
    chat_id, page = int(chat_id_s), int(page_s)
    chat = await _authorised(call, bot, db, settings, chat_id)
    if not chat:
        return
    text, has_next = await list_text(db, chat, settings, page)
    await _edit(call, text, list_keyboard(chat_id, page, has_next))
    await call.answer()


@router.callback_query(F.data.startswith("member:"))
async def cb_member(
    call: CallbackQuery, bot: Bot, db: Database, settings: Settings, service: MembershipService
) -> None:
    _, chat_id_s, uid_s = call.data.split(":")
    chat_id, uid = int(chat_id_s), int(uid_s)
    chat = await _authorised(call, bot, db, settings, chat_id)
    if not chat:
        return
    member = await db.get_member(chat_id, uid)
    if not member:
        await call.answer("Member not tracked", show_alert=True)
        return
    await _edit(call, service.member_card(chat, member), member_keyboard(chat_id, uid, member.status == "active"))
    await call.answer()


@router.callback_query(F.data.startswith("ext:"))
async def cb_extend(
    call: CallbackQuery, bot: Bot, db: Database, settings: Settings, service: MembershipService
) -> None:
    _, chat_id_s, uid_s, value = call.data.split(":")
    chat_id, uid = int(chat_id_s), int(uid_s)
    chat = await _authorised(call, bot, db, settings, chat_id)
    if not chat:
        return
    member = await db.get_member(chat_id, uid)
    if not member:
        await call.answer("Member not tracked", show_alert=True)
        return
    try:
        new_expiry = await service.extend_member(chat, member, value, call.from_user.id)
    except ParseError as exc:
        await call.answer(f"Error: {exc}", show_alert=True)
        return
    member = await db.get_member(chat_id, uid)
    await _edit(call, service.member_card(chat, member), member_keyboard(chat_id, uid, True))
    await call.answer(f"Expiry: {format_dt(new_expiry, settings.tz)}")
    if chat.notify_user and new_expiry:
        await service.dm_user(
            uid,
            f"🎉 Your membership in <b>{escape(chat.display)}</b> has been extended until "
            f"<b>{format_dt(new_expiry, settings.tz)}</b>.",
        )


@router.callback_query(F.data.startswith("kick:"))
async def cb_kick_confirm(call: CallbackQuery, bot: Bot, db: Database, settings: Settings) -> None:
    _, chat_id_s, uid_s = call.data.split(":")
    chat_id, uid = int(chat_id_s), int(uid_s)
    chat = await _authorised(call, bot, db, settings, chat_id)
    if not chat:
        return
    await _edit(
        call,
        f"⚠️ Remove <code>{uid}</code> from <b>{escape(chat.display)}</b> now?",
        confirm_keyboard("kick", chat_id, uid),
    )
    await call.answer()


@router.callback_query(F.data.startswith("kickc:"))
async def cb_kick(
    call: CallbackQuery, bot: Bot, db: Database, settings: Settings, service: MembershipService
) -> None:
    _, chat_id_s, uid_s = call.data.split(":")
    chat_id, uid = int(chat_id_s), int(uid_s)
    chat = await _authorised(call, bot, db, settings, chat_id)
    if not chat:
        return
    member = await db.get_member(chat_id, uid)
    if not member:
        await call.answer("Member not tracked", show_alert=True)
        return
    ok, msg = await service.remove_member(chat, member, reason="manual", actor_id=call.from_user.id)
    member = await db.get_member(chat_id, uid)
    await _edit(call, service.member_card(chat, member), member_keyboard(chat_id, uid, member.status == "active"))
    await call.answer("Removed ✅" if ok else f"Failed: {msg}", show_alert=not ok)


@router.callback_query(F.data.startswith("wl:"))
async def cb_whitelist(
    call: CallbackQuery, bot: Bot, db: Database, settings: Settings, service: MembershipService
) -> None:
    _, chat_id_s, uid_s = call.data.split(":")
    chat_id, uid = int(chat_id_s), int(uid_s)
    chat = await _authorised(call, bot, db, settings, chat_id)
    if not chat:
        return
    await db.add_whitelist(chat_id, uid, call.from_user.id)
    await db.set_member_expiry(chat_id, uid, None)
    await db.add_log(chat_id, uid, "whitelist_add", None, call.from_user.id)
    member = await db.get_member(chat_id, uid)
    if member:
        await _edit(call, service.member_card(chat, member), member_keyboard(chat_id, uid, True))
    await call.answer("Whitelisted 🛡")
