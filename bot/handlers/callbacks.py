"""Inline button callbacks: settings panel, member cards, join prompts, invite links."""
from __future__ import annotations

import logging
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
from bot.handlers.admin import list_text, logs_text, panel_text, stats_text
from bot.services.database import Chat, Database
from bot.services.membership import MembershipService
from bot.utils.keyboards import (
    cancel_keyboard,
    confirm_keyboard,
    duration_keyboard,
    invites_keyboard,
    join_prompt_keyboard,
    join_remove_confirm_keyboard,
    list_keyboard,
    member_keyboard,
    pending_keyboard,
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
        await call.answer("Chat not found", show_alert=True)
        return None
    if not await is_admin(bot, db, settings, chat_id, call.from_user.id):
        await call.answer("⛔ Not allowed", show_alert=True)
        return None
    return chat


def _panel_markup(chat: Chat, settings: Settings):
    return settings_keyboard(chat, settings.default_duration, settings.ask_on_join_default)


async def _show_panel(call: CallbackQuery, db: Database, settings: Settings, chat_id: int) -> None:
    chat = await db.get_chat(chat_id)
    if chat:
        await _edit(call, panel_text(chat, settings), _panel_markup(chat, settings))


def _ids(data: str, count: int) -> list[str]:
    parts = data.split(":")
    if len(parts) < count + 1:
        raise ValueError("bad callback data")
    return parts[1 : count + 1]


# -------------------------------------------------------------- chat select
@router.callback_query(F.data.startswith("ctx:"))
async def cb_select_chat(call: CallbackQuery, bot: Bot, db: Database, settings: Settings) -> None:
    chat_id = int(_ids(call.data, 1)[0])
    chat = await _authorised(call, bot, db, settings, chat_id)
    if not chat:
        return
    await db.set_context(call.from_user.id, chat_id)
    await _edit(call, panel_text(chat, settings), _panel_markup(chat, settings))
    await call.answer(f"Selected: {chat.display}")


@router.callback_query(F.data.startswith("panel:"))
async def cb_panel(call: CallbackQuery, bot: Bot, db: Database, settings: Settings, state: FSMContext) -> None:
    chat_id = int(_ids(call.data, 1)[0])
    chat = await _authorised(call, bot, db, settings, chat_id)
    if not chat:
        return
    await state.clear()
    await _edit(call, panel_text(chat, settings), _panel_markup(chat, settings))
    await call.answer()


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
        current = chat.default_duration or f"{settings.default_duration} (global)"
        await _edit(
            call,
            f"⏳ <b>Default duration for {escape(chat.display)}</b>\n\n"
            f"Current: <b>{escape(describe_duration(chat.default_duration or settings.default_duration))}</b> "
            f"(<code>{escape(current)}</code>)\n"
            f"For a custom value use /setduration &lt;value&gt; e.g. <code>/setduration 45d</code>",
            duration_keyboard(chat_id),
        )
        await call.answer()
        return

    updates: dict[str, object] = {}
    note = "Updated ✅"
    if key == "mode":
        updates["kick_mode"] = "ban" if chat.kick_mode == "kick" else "kick"
    elif key == "tracking":
        updates["tracking_enabled"] = int(not chat.tracking_enabled)
    elif key == "autokick":
        updates["auto_kick"] = int(not chat.auto_kick)
    elif key == "notify":
        updates["notify_user"] = int(not chat.notify_user)
    elif key == "welcome":
        updates["welcome_enabled"] = int(not chat.welcome_enabled)
    elif key == "ask":
        updates["ask_on_join"] = int(not service.ask_enabled(chat))
        note = "Owner will be asked on every join" if updates["ask_on_join"] else "Default duration applied silently"
    elif key == "asktarget":
        updates["ask_target"] = "admins" if chat.ask_target == "owner" else "owner"
        note = "Prompts go to: " + ("all admins" if updates["ask_target"] == "admins" else "owner only")
    elif key == "approve":
        updates["approve_requests"] = (chat.approve_requests + 1) % 3
        note = {0: "Join requests ignored", 1: "Join requests auto-approved", 2: "Admins asked to approve"}[
            updates["approve_requests"]
        ]
    else:
        await call.answer("Unknown setting")
        return

    await db.update_chat(chat_id, **updates)
    await db.add_log(chat_id, None, f"toggle_{key}", str(list(updates.values())[0]), call.from_user.id)
    await _show_panel(call, db, settings, chat_id)
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
    await _show_panel(call, db, settings, chat_id)
    await call.answer(f"Duration: {describe_duration(settings.default_duration if value == 'global' else value)}")


# ------------------------------------------------------------------- views
@router.callback_query(F.data.startswith("stats:"))
async def cb_stats(call: CallbackQuery, bot: Bot, db: Database, settings: Settings) -> None:
    chat_id = int(_ids(call.data, 1)[0])
    chat = await _authorised(call, bot, db, settings, chat_id)
    if not chat:
        return
    await _edit(call, await stats_text(db, chat, settings), list_keyboard(chat_id, 0, False))
    await call.answer()


@router.callback_query(F.data.startswith("logs:"))
async def cb_logs(call: CallbackQuery, bot: Bot, db: Database, settings: Settings) -> None:
    chat_id = int(_ids(call.data, 1)[0])
    chat = await _authorised(call, bot, db, settings, chat_id)
    if not chat:
        return
    await _edit(call, await logs_text(db, chat, settings), list_keyboard(chat_id, 0, False))
    await call.answer()


@router.callback_query(F.data.startswith("list:"))
async def cb_list(call: CallbackQuery, bot: Bot, db: Database, settings: Settings) -> None:
    chat_id_s, page_s = _ids(call.data, 2)
    chat_id, page = int(chat_id_s), int(page_s)
    chat = await _authorised(call, bot, db, settings, chat_id)
    if not chat:
        return
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
    member = await db.get_member(chat_id, uid)
    if not member:
        await call.answer("Member not tracked", show_alert=True)
        return
    await _edit(call, service.member_card(chat, member), member_keyboard(chat_id, uid, member.is_active))
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
        await call.answer("Use this from my private chat (/chats).", show_alert=True)
        return
    await state.set_state(CustomInput.member_expiry)
    await state.update_data(chat_id=chat_id, user_id=uid, prompt_message_id=call.message.message_id if call.message else None)
    await _edit(
        call,
        f"✏️ <b>Custom expiry for <code>{uid}</code></b> in {escape(chat.display)}\n\n"
        "Send a duration or date, e.g.\n"
        "<code>45d</code> • <code>1m 15d</code> • <code>2025-12-31</code> • "
        "<code>31/12/2025 18:30</code> • <code>never</code>\n\n"
        "<i>Durations are added on top of the current expiry.</i>",
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
    lines = [f"📜 <b>History — <code>{uid}</code></b> in {escape(chat.display)}", ""]
    if not rows:
        lines.append("<i>No events recorded.</i>")
    for r in rows:
        ts = format_dt(datetime.fromisoformat(r["created_at"]), settings.tz)
        det = f" <i>{escape(str(r['details']))[:50]}</i>" if r["details"] else ""
        by = f" (by <code>{r['actor_id']}</code>)" if r["actor_id"] else ""
        lines.append(f"• {ts} — <b>{escape(r['action'])}</b>{det}{by}")
    member = await db.get_member(chat_id, uid)
    await _edit(call, "\n".join(lines), member_keyboard(chat_id, uid, bool(member and member.is_active)))
    await call.answer()


@router.callback_query(F.data.startswith("kick:"))
async def cb_kick_confirm(call: CallbackQuery, bot: Bot, db: Database, settings: Settings) -> None:
    chat_id_s, uid_s = _ids(call.data, 2)
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
    chat_id_s, uid_s = _ids(call.data, 2)
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
    await _edit(call, service.member_card(chat, member), member_keyboard(chat_id, uid, member.is_active))
    await call.answer("Removed ✅" if ok else f"Failed: {msg}", show_alert=not ok)


@router.callback_query(F.data.startswith("wl:"))
async def cb_whitelist(
    call: CallbackQuery, bot: Bot, db: Database, settings: Settings, service: MembershipService
) -> None:
    chat_id_s, uid_s = _ids(call.data, 2)
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


# ------------------------------------------------------------ join prompts
@router.callback_query(F.data.startswith("jd:"))
async def cb_join_decision(
    call: CallbackQuery, bot: Bot, db: Database, settings: Settings, service: MembershipService, state: FSMContext
) -> None:
    pid_s, value = _ids(call.data, 2)
    pending = await db.get_pending(int(pid_s))
    if not pending:
        await call.answer("This request no longer exists.", show_alert=True)
        return
    chat = await db.get_chat(pending.chat_id)
    if not chat or not await is_admin(bot, db, settings, pending.chat_id, call.from_user.id):
        await call.answer("⛔ Not allowed", show_alert=True)
        return
    await state.clear()
    if pending.source == "request":
        ok, outcome = await service.apply_request_decision(pending, value, call.from_user.id, call.from_user.full_name)
    else:
        ok, outcome = await service.apply_join_decision(pending, value, call.from_user.id, call.from_user.full_name)
    if not ok and "Already" in outcome:
        await call.answer(outcome, show_alert=True)
        return
    await call.answer("Done ✅" if ok else outcome, show_alert=not ok)


@router.callback_query(F.data.startswith("jr:"))
async def cb_join_remove_confirm(call: CallbackQuery, bot: Bot, db: Database, settings: Settings) -> None:
    pid = int(_ids(call.data, 1)[0])
    pending = await db.get_pending(pid)
    if not pending or pending.status != "pending":
        await call.answer("Already handled.", show_alert=True)
        return
    if not await is_admin(bot, db, settings, pending.chat_id, call.from_user.id):
        await call.answer("⛔ Not allowed", show_alert=True)
        return
    verb = "Reject" if pending.source == "request" else "Remove"
    await _edit(
        call,
        f"⚠️ <b>{verb} {pending.mention_html}</b> (<code>{pending.user_id}</code>)?",
        join_remove_confirm_keyboard(pid),
    )
    await call.answer()


@router.callback_query(F.data.startswith("jb:"))
async def cb_join_back(
    call: CallbackQuery, bot: Bot, db: Database, settings: Settings, service: MembershipService, state: FSMContext
) -> None:
    """Show (again) the full prompt for a pending join."""
    pid = int(_ids(call.data, 1)[0])
    pending = await db.get_pending(pid)
    if not pending:
        await call.answer("This request no longer exists.", show_alert=True)
        return
    chat = await db.get_chat(pending.chat_id)
    if not chat or not await is_admin(bot, db, settings, pending.chat_id, call.from_user.id):
        await call.answer("⛔ Not allowed", show_alert=True)
        return
    await state.clear()
    if pending.status != "pending":
        await call.answer(f"Already handled ({pending.decision or pending.status}).", show_alert=True)
        return
    member = await db.get_member(chat.chat_id, pending.user_id)
    await _edit(call, service.join_prompt_text(chat, pending, member), join_prompt_keyboard(pid))
    if call.message:
        await db.add_prompt_message(pid, call.from_user.id, call.message.message_id)
    await call.answer()


@router.callback_query(F.data.startswith("jc:"))
async def cb_join_custom(
    call: CallbackQuery, bot: Bot, db: Database, settings: Settings, state: FSMContext
) -> None:
    pid = int(_ids(call.data, 1)[0])
    pending = await db.get_pending(pid)
    if not pending or pending.status != "pending":
        await call.answer("Already handled.", show_alert=True)
        return
    if not await is_admin(bot, db, settings, pending.chat_id, call.from_user.id):
        await call.answer("⛔ Not allowed", show_alert=True)
        return
    await state.set_state(CustomInput.join_duration)
    await state.update_data(pending_id=pid, prompt_message_id=call.message.message_id if call.message else None)
    await _edit(
        call,
        f"✏️ <b>Custom duration for {pending.mention_html}</b>\n\n"
        "Send how long they may stay, e.g.\n"
        "<code>45d</code> • <code>1m 15d</code> • <code>2025-12-31</code> • "
        "<code>31/12/2025 18:30</code> • <code>never</code>",
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
    lines = [f"🔔 <b>Pending decisions — {escape(chat.display)}</b> ({len(items)})", ""]
    if not items:
        lines.append("<i>Nothing waiting. New joins will appear here until an admin answers.</i>")
    now = datetime.now(settings.tz)
    for p in items:
        age = humanize_delta(now - p.created_at.astimezone(settings.tz))
        kind = "request" if p.source == "request" else "joined"
        lines.append(f"• <b>#{p.id}</b> {p.mention_html} <code>{p.user_id}</code> — {kind} {age} ago")
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
    lines = [
        f"🔗 <b>Invite links — {escape(chat.display)}</b>",
        "",
        "Members who join through one of these links automatically get the preset duration "
        "(no owner prompt).",
        "",
    ]
    if not links:
        lines.append("<i>No links yet. Tap a duration below to create one.</i>")
    for link in links[:10]:
        lines.append(
            f"• <b>{escape(link.name or link.duration)}</b> — {escape(describe_duration(link.duration))} "
            f"— used {link.uses}×\n  <code>{escape(link.invite_link)}</code>"
        )
    return "\n".join(lines)


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
    await call.answer(f"Created: {msg}")


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
    await call.answer("Revoked ✅" if ok else f"Marked revoked locally: {msg}", show_alert=not ok)


# ------------------------------------------------------------ misc buttons
@router.callback_query(F.data == "cancel")
async def cb_cancel(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await _edit(call, "❌ Cancelled.")
    await call.answer()


@router.callback_query(F.data == "noop")
async def cb_noop(call: CallbackQuery) -> None:
    await call.answer()


# ------------------------------------------------------- FSM text handlers
@router.message(Command("cancel"), StateFilter(CustomInput))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("❌ Cancelled.")


@router.message(CustomInput.join_duration, F.text, F.chat.type == ChatType.PRIVATE)
async def on_join_custom_text(
    message: Message, bot: Bot, db: Database, settings: Settings, service: MembershipService, state: FSMContext
) -> None:
    data = await state.get_data()
    pid = int(data.get("pending_id", 0))
    pending = await db.get_pending(pid)
    if not pending or pending.status != "pending":
        await state.clear()
        await message.answer("ℹ️ That request was already handled.")
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
