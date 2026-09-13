"""Inline button callbacks: home, dashboard, settings, members, join prompts, invite links, tools.

Callback-data grammar (``prefix:chat_id[:...]``) — every screen is reachable by
tapping, typing is only needed for free-form values (custom dates, texts):

===========  ======================================================
``dash``     dashboard · ``settings`` · ``adv`` · ``tools`` · ``stats``
``list``     ``list:<cid>:<view>:<page>`` tappable member list
``member``   ``member:<cid>:<uid>[:<view>:<page>]`` member card
``ext``/``cust``/``more``/``hist``/``note``/``askm``/``wl``/``kick``/``untrack``
``set``/``dur``/``edit``/``editclr``/``editlog`` settings & editors
``pending``/``pall``/``pallc``/``jd``/``jm``/``jr``/``jb``/``jc`` join prompts
``invites``/``inpick``/``inew``/``incust``/``irev``/``irevc`` invite links
``addm``/``search``/``bcast``/``bcastc``/``vips``/``sync``/``fcheck``/``perms`` tools
``export``   ``export:<cid>`` scope picker · ``exportv:<cid>:<view>`` send the CSV
===========  ======================================================
"""
from __future__ import annotations

import logging
import re
from datetime import datetime
from html import escape

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardMarkup,
    Message,
)

from bot.config import Settings
from bot.handlers import screens as S
from bot.handlers.common import HELP_TOPICS, home_text, mystatus_text
from bot.services import export as X
from bot.services.database import Chat, Database
from bot.services.membership import MembershipService
from bot.services.scheduler import ExpiryScheduler
from bot.utils import keyboards as K
from bot.utils.permissions import bot_can_restrict, is_admin
from bot.utils.telegram import is_not_modified, tg_call
from bot.utils.timeparse import (
    ParseError,
    describe_duration,
    format_dt,
    is_permanent,
    parse_duration,
)
from bot.utils.ui import progress_bar

log = logging.getLogger(__name__)
router = Router(name="callbacks")


class CustomInput(StatesGroup):
    """Waiting for the admin to type a free-form value."""

    join_duration = State()  # data: pending_id
    member_expiry = State()  # data: chat_id, user_id
    member_note = State()  # data: chat_id, user_id
    add_member = State()  # data: chat_id
    search = State()  # data: chat_id
    broadcast = State()  # data: chat_id, text (after first message)
    welcome_text = State()  # data: chat_id
    log_chat = State()  # data: chat_id
    chat_duration = State()  # data: chat_id
    invite_custom = State()  # data: chat_id


INPUT_HINT = (
    "<code>45d</code> · <code>1m 15d</code> · <code>2025-12-31</code> · "
    "<code>31/12/2025 18:30</code> · <code>never</code>"
)


# ------------------------------------------------------------------ helpers
async def _edit(call: CallbackQuery, text: str, markup: InlineKeyboardMarkup | None = None) -> None:
    """Edit the message behind a button, tolerating no-op edits and stale messages."""
    if call.message is None:
        return
    try:
        await tg_call(
            call.message.edit_text,
            text,
            reply_markup=markup,
            disable_web_page_preview=True,
            retries=1,
            label="edit_text",
        )
    except TelegramBadRequest as exc:
        if is_not_modified(exc):
            return
        # message too old to edit (48h) or deleted → send a fresh one instead
        if "can't be edited" in str(exc) or "message to edit not found" in str(exc).lower():
            try:
                await call.message.answer(text, reply_markup=markup, disable_web_page_preview=True)
            except Exception as inner:  # noqa: BLE001
                log.debug("fallback send failed: %s", inner)
            return
        log.debug("edit failed: %s", exc)
    except Exception as exc:  # noqa: BLE001 - network blips must never break a button
        log.debug("edit failed: %s", exc)


async def _show(call: CallbackQuery, screen: S.Screen) -> None:
    await _edit(call, *screen)


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


def _ids(data: str, count: int) -> list[str]:
    parts = data.split(":")
    if len(parts) < count + 1:
        raise ValueError("bad callback data")
    return parts[1 : count + 1]


def _parts(data: str) -> list[str]:
    return data.split(":")[1:]


async def _admin_chats(db: Database, settings: Settings, user_id: int) -> list[Chat]:
    if user_id in settings.super_admins:
        return await db.list_chats()
    return await db.chats_for_admin(user_id)


async def _show_dashboard(call: CallbackQuery, db: Database, settings: Settings, chat: Chat) -> None:
    many = len(await _admin_chats(db, settings, call.from_user.id)) > 1
    await _show(call, await S.dashboard_screen(db, chat, settings, many))


async def _show_member(
    call: CallbackQuery,
    db: Database,
    service: MembershipService,
    chat: Chat,
    uid: int,
    back_view: str = "active",
    back_page: int = 0,
) -> bool:
    member = await db.get_member(chat.chat_id, uid)
    if not member:
        await call.answer("Member is not tracked.", show_alert=True)
        return False
    await _edit(
        call,
        service.member_card(chat, member),
        K.member_keyboard(chat.chat_id, uid, member.is_active, back_view, back_page),
    )
    return True


def _private_only(call: CallbackQuery) -> bool:
    return call.message is not None and call.message.chat.type == ChatType.PRIVATE


async def _begin_input(
    call: CallbackQuery, state: FSMContext, st: State, text: str, cancel_to: str, **data: object
) -> None:
    """Switch to an FSM input state and show the instruction with a cancel button."""
    if not _private_only(call):
        await call.answer("Use this from my private chat.", show_alert=True)
        return
    await state.set_state(st)
    await state.update_data(prompt_message_id=call.message.message_id if call.message else None, **data)
    await _edit(call, text, K.cancel_keyboard(cancel_to))
    await call.answer()


async def _input_chat(
    message: Message, bot: Bot, db: Database, settings: Settings, state: FSMContext
) -> Chat | None:
    """Resolve + authorise the chat stored in FSM data; clears state on failure."""
    data = await state.get_data()
    chat_id = int(data.get("chat_id", 0))
    chat = await db.get_chat(chat_id)
    if not chat or not await is_admin(bot, db, settings, chat_id, message.from_user.id):
        await state.clear()
        return None
    return chat


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
        K.home_keyboard(is_admin_user, me.username or "", bool(chats)),
    )
    await call.answer()


@router.callback_query(F.data.startswith("help:"))
async def cb_help(call: CallbackQuery) -> None:
    topic = _ids(call.data, 1)[0]
    text = HELP_TOPICS.get(topic) or HELP_TOPICS["main"]
    await _edit(call, text, K.help_keyboard(topic if topic in HELP_TOPICS else "main"))
    await call.answer()


@router.callback_query(F.data == "mystatus")
async def cb_mystatus(call: CallbackQuery, db: Database, settings: Settings) -> None:
    await _edit(call, await mystatus_text(db, settings, call.from_user.id), K.mystatus_keyboard())
    await call.answer()


@router.callback_query(F.data == "chats")
async def cb_chats(call: CallbackQuery, db: Database, settings: Settings, state: FSMContext) -> None:
    await state.clear()
    chats = await _admin_chats(db, settings, call.from_user.id)
    if not chats:
        await call.answer("No chats yet — add me to a group as admin.", show_alert=True)
        return
    current = await db.get_context(call.from_user.id)
    await _show(call, await S.chats_screen(db, chats, current))
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
    await _show(call, S.settings_screen(chat, settings))
    await call.answer()


@router.callback_query(F.data.startswith("adv:"))
async def cb_advanced(call: CallbackQuery, bot: Bot, db: Database, settings: Settings, state: FSMContext) -> None:
    chat_id = int(_ids(call.data, 1)[0])
    chat = await _authorised(call, bot, db, settings, chat_id)
    if not chat:
        return
    await state.clear()
    await _show(call, S.advanced_screen(chat))
    await call.answer()


@router.callback_query(F.data.startswith("tools:"))
async def cb_tools(call: CallbackQuery, bot: Bot, db: Database, settings: Settings, state: FSMContext) -> None:
    chat_id = int(_ids(call.data, 1)[0])
    chat = await _authorised(call, bot, db, settings, chat_id)
    if not chat:
        return
    await state.clear()
    await _show(call, S.tools_screen(chat))
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
        await _show(call, S.duration_screen(chat, settings))
        await call.answer()
        return

    updates: dict[str, object] = {}
    note = "Saved"
    advanced = key in ("notify", "welcome", "approve", "asktarget", "grace", "digest")
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
        note = "Members get DMs" if updates["notify_user"] else "Members are not DM'd"
    elif key == "welcome":
        updates["welcome_enabled"] = int(not chat.welcome_enabled)
        note = "Welcome message on" if updates["welcome_enabled"] else "Welcome message off"
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
    elif key == "grace":
        updates["grace_hours"] = K.next_grace(chat.grace_hours)
        note = (
            f"Expired members get {K.grace_label(updates['grace_hours'])} of grace before removal"
            if updates["grace_hours"]
            else "Removed right at expiry"
        )
    elif key == "digest":
        updates["digest_enabled"] = int(not chat.digest_enabled)
        note = "You'll get a daily expiring-soon digest" if updates["digest_enabled"] else "Daily digest off"
    else:
        await call.answer("Unknown setting")
        return

    await db.update_chat(chat_id, **updates)
    await db.add_log(chat_id, None, f"toggle_{key}", str(list(updates.values())[0]), call.from_user.id)
    chat = await db.get_chat(chat_id)
    await _show(call, S.advanced_screen(chat) if advanced else S.settings_screen(chat, settings))
    await call.answer(note)


@router.callback_query(F.data.startswith("dur:"))
async def cb_duration(call: CallbackQuery, bot: Bot, db: Database, settings: Settings, state: FSMContext) -> None:
    chat_id_s, value = _ids(call.data, 2)
    chat_id = int(chat_id_s)
    chat = await _authorised(call, bot, db, settings, chat_id)
    if not chat:
        return
    if value == "custom":
        await _begin_input(
            call,
            state,
            CustomInput.chat_duration,
            f"✏️ <b>Custom default duration — {escape(chat.display)}</b>\n\n"
            "Send a duration such as <code>45d</code>, <code>2w</code>, <code>1m 15d</code> or <code>never</code>.",
            f"set:{chat_id}:duration",
            chat_id=chat_id,
        )
        return
    await db.update_chat(chat_id, default_duration=None if value == "global" else value)
    await db.add_log(chat_id, None, "set_duration", value, call.from_user.id)
    chat = await db.get_chat(chat_id)
    await _show(call, S.settings_screen(chat, settings))
    await call.answer(f"Default: {describe_duration(settings.default_duration if value == 'global' else value)}")


# ---------------------------------------------------------- text editors
@router.callback_query(F.data.startswith("edit:"))
async def cb_edit(call: CallbackQuery, bot: Bot, db: Database, settings: Settings, state: FSMContext) -> None:
    chat_id_s, kind = _ids(call.data, 2)
    chat_id = int(chat_id_s)
    chat = await _authorised(call, bot, db, settings, chat_id)
    if not chat:
        return
    if kind not in ("welcome", "log"):
        await call.answer("Unknown editor")
        return
    if not _private_only(call):
        await call.answer("Use this from my private chat.", show_alert=True)
        return
    await state.set_state(CustomInput.welcome_text if kind == "welcome" else CustomInput.log_chat)
    await state.update_data(chat_id=chat_id)
    await _show(call, S.edit_text_screen(chat, kind))
    await call.answer()


@router.callback_query(F.data.startswith("editclr:"))
async def cb_edit_clear(call: CallbackQuery, bot: Bot, db: Database, settings: Settings, state: FSMContext) -> None:
    chat_id_s, kind = _ids(call.data, 2)
    chat_id = int(chat_id_s)
    chat = await _authorised(call, bot, db, settings, chat_id)
    if not chat:
        return
    await state.clear()
    if kind == "welcome":
        await db.update_chat(chat_id, welcome_text=None)
        note = "Welcome text reset to default"
    else:
        await db.update_chat(chat_id, log_chat_id=None)
        note = "Log channel disabled"
    await db.add_log(chat_id, None, f"clear_{kind}", None, call.from_user.id)
    chat = await db.get_chat(chat_id)
    await _show(call, S.advanced_screen(chat))
    await call.answer(note)


@router.callback_query(F.data.startswith("editlog:"))
async def cb_edit_log_here(call: CallbackQuery, bot: Bot, db: Database, settings: Settings, state: FSMContext) -> None:
    chat_id = int(_ids(call.data, 1)[0])
    chat = await _authorised(call, bot, db, settings, chat_id)
    if not chat or call.message is None:
        return
    await state.clear()
    await db.update_chat(chat_id, log_chat_id=call.message.chat.id)
    await db.add_log(chat_id, None, "set_log", str(call.message.chat.id), call.from_user.id)
    chat = await db.get_chat(chat_id)
    await _show(call, S.advanced_screen(chat))
    await call.answer("Logs will be posted here")


# ------------------------------------------------------------------- views
@router.callback_query(F.data.startswith("stats:"))
async def cb_stats(call: CallbackQuery, bot: Bot, db: Database, settings: Settings) -> None:
    chat_id = int(_ids(call.data, 1)[0])
    chat = await _authorised(call, bot, db, settings, chat_id)
    if not chat:
        return
    await _show(call, await S.stats_screen(db, chat, settings))
    await call.answer()


@router.callback_query(F.data.startswith("logs:"))
async def cb_logs(call: CallbackQuery, bot: Bot, db: Database, settings: Settings) -> None:
    parts = _parts(call.data)
    chat_id = int(parts[0])
    page = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
    chat = await _authorised(call, bot, db, settings, chat_id)
    if not chat:
        return
    await _show(call, await S.logs_screen(db, chat, settings, page))
    await call.answer()


def _parse_list_data(data: str) -> tuple[int, str, int]:
    """``list:<cid>:<view>:<page>`` — also accepts the legacy ``list:<cid>:<page>``."""
    parts = _parts(data)
    chat_id = int(parts[0])
    view, page = "active", 0
    if len(parts) == 2:
        if parts[1].lstrip("-").isdigit():
            page = int(parts[1])
        else:
            view = parts[1]
    elif len(parts) >= 3:
        view = parts[1]
        page = int(parts[2]) if parts[2].lstrip("-").isdigit() else 0
    return chat_id, view, max(0, page)


@router.callback_query(F.data.startswith("list:"))
async def cb_list(call: CallbackQuery, bot: Bot, db: Database, settings: Settings, state: FSMContext) -> None:
    chat_id, view, page = _parse_list_data(call.data)
    chat = await _authorised(call, bot, db, settings, chat_id)
    if not chat:
        return
    await state.clear()
    await _show(call, await S.members_screen(db, chat, settings, view, page))
    await call.answer()


# ------------------------------------------------------------ member cards
@router.callback_query(F.data.startswith("member:"))
async def cb_member(
    call: CallbackQuery, bot: Bot, db: Database, settings: Settings, service: MembershipService, state: FSMContext
) -> None:
    parts = _parts(call.data)
    chat_id, uid = int(parts[0]), int(parts[1])
    view = parts[2] if len(parts) > 2 else "active"
    page = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 0
    chat = await _authorised(call, bot, db, settings, chat_id)
    if not chat:
        return
    await state.clear()
    await _show_member(call, db, service, chat, uid, view, page)
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
    await _edit(call, service.member_card(chat, member), K.member_more_keyboard(chat_id, uid, member.is_active, wl))
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
    member = await db.get_member(chat_id, uid)
    who = member.mention_html if member else f"<code>{uid}</code>"
    await _begin_input(
        call,
        state,
        CustomInput.member_expiry,
        f"✏️ <b>Custom expiry for {who}</b>\n\nSend a duration to add, or an exact date:\n{INPUT_HINT}",
        f"member:{chat_id}:{uid}",
        chat_id=chat_id,
        user_id=uid,
    )


@router.callback_query(F.data.startswith("note:"))
async def cb_note(call: CallbackQuery, bot: Bot, db: Database, settings: Settings, state: FSMContext) -> None:
    chat_id_s, uid_s = _ids(call.data, 2)
    chat_id, uid = int(chat_id_s), int(uid_s)
    chat = await _authorised(call, bot, db, settings, chat_id)
    if not chat:
        return
    member = await db.get_member(chat_id, uid)
    if not member:
        await call.answer("Member is not tracked.", show_alert=True)
        return
    current = f"\n\nCurrent: <i>{escape(member.note)}</i>" if member.note else ""
    await _begin_input(
        call,
        state,
        CustomInput.member_note,
        f"📝 <b>Note for {member.mention_html}</b>{current}\n\n"
        "Send the note text (e.g. a payment reference). Send <code>-</code> to clear it.",
        f"member:{chat_id}:{uid}",
        chat_id=chat_id,
        user_id=uid,
    )


@router.callback_query(F.data.startswith("askm:"))
async def cb_ask_member(
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
    pending = await service.ask_owner_about_member(chat, member, source="manual")
    await call.answer(
        "Prompt sent to the owner/admins" if pending else "Nobody could be reached — the owner must /start me first.",
        show_alert=not pending,
    )


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
    await _edit(call, "\n".join(lines), K.cancel_keyboard(f"member:{chat_id}:{uid}", "◀️ Back"))
    await call.answer()


@router.callback_query(F.data.startswith("kick:"))
async def cb_kick_confirm(call: CallbackQuery, bot: Bot, db: Database, settings: Settings) -> None:
    chat_id_s, uid_s = _ids(call.data, 2)
    chat_id, uid = int(chat_id_s), int(uid_s)
    chat = await _authorised(call, bot, db, settings, chat_id)
    if not chat:
        return
    member = await db.get_member(chat_id, uid)
    who = member.mention_html if member else f"<code>{uid}</code>"
    await _edit(call, f"⚠️ Remove {who} from <b>{escape(chat.display)}</b> now?", K.confirm_keyboard("kick", chat_id, uid))
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
        K.confirm_keyboard("untrack", chat_id, uid),
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
    await _show(call, await S.members_screen(db, chat, settings, "active", 0))
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
        await call.message.edit_reply_markup(reply_markup=K.join_more_keyboard(pid))
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
        K.join_remove_confirm_keyboard(pid, is_req),
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
        K.join_prompt_keyboard(pid, service.effective_duration(chat), pending.source == "request"),
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
    await _begin_input(
        call,
        state,
        CustomInput.join_duration,
        f"✏️ <b>Custom duration for {pending.mention_html}</b>\n\n"
        f"Send how long they may stay, or an exact date:\n{INPUT_HINT}",
        f"jb:{pid}",
        pending_id=pid,
    )


@router.callback_query(F.data.startswith("pending:"))
async def cb_pending(call: CallbackQuery, bot: Bot, db: Database, settings: Settings, state: FSMContext) -> None:
    chat_id = int(_ids(call.data, 1)[0])
    chat = await _authorised(call, bot, db, settings, chat_id)
    if not chat:
        return
    await state.clear()
    await _show(call, await S.pending_screen(db, chat, settings))
    await call.answer()


@router.callback_query(F.data.startswith("pall:"))
async def cb_pending_all_confirm(call: CallbackQuery, bot: Bot, db: Database, settings: Settings, service: MembershipService) -> None:
    chat_id = int(_ids(call.data, 1)[0])
    chat = await _authorised(call, bot, db, settings, chat_id)
    if not chat:
        return
    count = await db.count_pending(chat_id)
    if not count:
        await call.answer("Nothing pending.", show_alert=True)
        return
    label = describe_duration(service.effective_duration(chat))
    await _edit(
        call,
        f"✅ <b>Apply the default to everyone waiting?</b>\n\n"
        f"<b>{count}</b> member{'s' if count != 1 else ''} in {escape(chat.display)} will get "
        f"<b>{escape(label)}</b>. Join requests are approved.",
        K.pending_all_confirm_keyboard(chat_id, count),
    )
    await call.answer()


@router.callback_query(F.data.startswith("pallc:"))
async def cb_pending_all(call: CallbackQuery, bot: Bot, db: Database, settings: Settings, service: MembershipService) -> None:
    chat_id = int(_ids(call.data, 1)[0])
    chat = await _authorised(call, bot, db, settings, chat_id)
    if not chat:
        return
    items = await db.list_pending(chat_id, limit=200)
    done = 0
    for p in items:
        if p.source == "request":
            ok, _ = await service.apply_request_decision(p, "default", call.from_user.id, call.from_user.full_name)
        else:
            ok, _ = await service.apply_join_decision(p, "default", call.from_user.id, call.from_user.full_name)
        done += int(ok)
    await db.add_log(chat_id, None, "pending_bulk_default", f"{done}/{len(items)}", call.from_user.id)
    await _show(call, await S.pending_screen(db, chat, settings))
    await call.answer(f"Applied default to {done} of {len(items)}")


# ------------------------------------------------------------ invite links
@router.callback_query(F.data.startswith("invites:"))
async def cb_invites(call: CallbackQuery, bot: Bot, db: Database, settings: Settings, state: FSMContext) -> None:
    chat_id = int(_ids(call.data, 1)[0])
    chat = await _authorised(call, bot, db, settings, chat_id)
    if not chat:
        return
    await state.clear()
    await _show(call, await S.invites_screen(db, chat))
    await call.answer()


@router.callback_query(F.data.startswith("inpick:"))
async def cb_invite_pick(call: CallbackQuery, bot: Bot, db: Database, settings: Settings) -> None:
    chat_id = int(_ids(call.data, 1)[0])
    chat = await _authorised(call, bot, db, settings, chat_id)
    if not chat:
        return
    await _edit(
        call,
        f"➕ <b>New invite link — {escape(chat.display)}</b>\n\n"
        "How long should members joining through this link stay?",
        K.invite_pick_keyboard(chat_id),
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
    await _show(call, S.invite_created_screen(chat, url, msg, value))
    await call.answer(f"Created · {K.preset_label(value)}")


@router.callback_query(F.data.startswith("incust:"))
async def cb_invite_custom(call: CallbackQuery, bot: Bot, db: Database, settings: Settings, state: FSMContext) -> None:
    chat_id = int(_ids(call.data, 1)[0])
    chat = await _authorised(call, bot, db, settings, chat_id)
    if not chat:
        return
    await _begin_input(
        call,
        state,
        CustomInput.invite_custom,
        f"✏️ <b>Custom invite link — {escape(chat.display)}</b>\n\n"
        "Send the duration followed by an optional label, e.g.\n"
        "<code>3m Gold plan</code> · <code>45d Trial</code> · <code>never Founders</code>",
        f"invites:{chat_id}",
        chat_id=chat_id,
    )


@router.callback_query(F.data.startswith("irev:"))
async def cb_invite_revoke_confirm(call: CallbackQuery, bot: Bot, db: Database, settings: Settings) -> None:
    chat_id_s, suffix = _ids(call.data, 2)
    chat_id = int(chat_id_s)
    chat = await _authorised(call, bot, db, settings, chat_id)
    if not chat:
        return
    target = next((lnk for lnk in await db.list_invite_links(chat_id) if lnk.invite_link.endswith(suffix)), None)
    if not target:
        await call.answer("Link not found", show_alert=True)
        return
    await _edit(
        call,
        f"🗑 <b>Revoke this link?</b>\n\n<b>{escape(target.name or K.preset_label(target.duration))}</b> · "
        f"{target.uses} joined\n<code>{escape(target.invite_link)}</code>\n\n"
        "<i>Members who already joined keep their duration.</i>",
        K.invite_revoke_confirm_keyboard(chat_id, suffix),
    )
    await call.answer()


@router.callback_query(F.data.startswith("irevc:"))
async def cb_invite_revoke(
    call: CallbackQuery, bot: Bot, db: Database, settings: Settings, service: MembershipService
) -> None:
    chat_id_s, suffix = _ids(call.data, 2)
    chat_id = int(chat_id_s)
    chat = await _authorised(call, bot, db, settings, chat_id)
    if not chat:
        return
    target = next((lnk for lnk in await db.list_invite_links(chat_id) if lnk.invite_link.endswith(suffix)), None)
    if not target:
        await call.answer("Link not found", show_alert=True)
        return
    ok, msg = await service.revoke_invite_link(chat, target.invite_link, call.from_user.id)
    await _show(call, await S.invites_screen(db, chat))
    await call.answer("Revoked" if ok else f"Marked revoked locally: {msg}", show_alert=not ok)


# ------------------------------------------------------------------- tools
@router.callback_query(F.data.startswith("addm:"))
async def cb_add_member(call: CallbackQuery, bot: Bot, db: Database, settings: Settings, state: FSMContext) -> None:
    chat_id = int(_ids(call.data, 1)[0])
    chat = await _authorised(call, bot, db, settings, chat_id)
    if not chat:
        return
    await _begin_input(
        call,
        state,
        CustomInput.add_member,
        f"➕ <b>Add member — {escape(chat.display)}</b>\n\n"
        "Send the user ID or @username, optionally followed by a duration:\n"
        "<code>123456789</code> · <code>@alice 3m</code> · <code>123456789 2025-12-31</code>\n\n"
        f"<i>Without a duration the default ({escape(describe_duration(chat.default_duration or settings.default_duration))}) is used.</i>",
        f"tools:{chat_id}",
        chat_id=chat_id,
    )


@router.callback_query(F.data.startswith("search:"))
async def cb_search(call: CallbackQuery, bot: Bot, db: Database, settings: Settings, state: FSMContext) -> None:
    chat_id = int(_ids(call.data, 1)[0])
    chat = await _authorised(call, bot, db, settings, chat_id)
    if not chat:
        return
    await _begin_input(
        call,
        state,
        CustomInput.search,
        f"🔍 <b>Search — {escape(chat.display)}</b>\n\nSend a name, @username or user ID (2+ characters).",
        f"list:{chat_id}:active:0",
        chat_id=chat_id,
    )


@router.callback_query(F.data.startswith("bcast:"))
async def cb_broadcast(call: CallbackQuery, bot: Bot, db: Database, settings: Settings, state: FSMContext) -> None:
    chat_id = int(_ids(call.data, 1)[0])
    chat = await _authorised(call, bot, db, settings, chat_id)
    if not chat:
        return
    total = await db.count_members(chat_id, "active")
    if not total:
        await call.answer("No active members to message.", show_alert=True)
        return
    await _begin_input(
        call,
        state,
        CustomInput.broadcast,
        f"📣 <b>Broadcast — {escape(chat.display)}</b>\n\n"
        f"Send the message for your <b>{total}</b> active members. You'll confirm before it goes out.\n"
        "<i>Only members who have started a private chat with me can receive it.</i>",
        f"tools:{chat_id}",
        chat_id=chat_id,
    )


@router.callback_query(F.data.startswith("bcastc:"))
async def cb_broadcast_confirm(
    call: CallbackQuery, bot: Bot, db: Database, settings: Settings, service: MembershipService, state: FSMContext
) -> None:
    chat_id = int(_ids(call.data, 1)[0])
    chat = await _authorised(call, bot, db, settings, chat_id)
    if not chat:
        return
    data = await state.get_data()
    text = str(data.get("text") or "")
    if not text or int(data.get("chat_id", 0)) != chat_id:
        await state.clear()
        await call.answer("Nothing to send — start again.", show_alert=True)
        await _show(call, S.tools_screen(chat))
        return
    await state.clear()
    total = await db.count_members(chat_id, "active")
    await _edit(call, f"📣 Sending to {total} members…\n{progress_bar(0, total)}")

    async def progress(done: int, total_: int) -> None:
        await _edit(call, f"📣 Sending… <b>{done}</b>/{total_}\n{progress_bar(done, total_)}")

    sent, total = await service.broadcast(chat, text, call.from_user.id, progress=progress)
    await _edit(
        call,
        f"📣 <b>Broadcast delivered</b> to <b>{sent}</b> of {total} members.\n{progress_bar(total, total)}"
        + ("" if sent == total else "\n<i>Members who never started a private chat with me can't be reached.</i>"),
        K.back_keyboard(chat_id),
    )
    await call.answer("Sent")


@router.callback_query(F.data.startswith("vips:"))
async def cb_vips(call: CallbackQuery, bot: Bot, db: Database, settings: Settings, state: FSMContext) -> None:
    chat_id = int(_ids(call.data, 1)[0])
    chat = await _authorised(call, bot, db, settings, chat_id)
    if not chat:
        return
    await state.clear()
    await _show(call, await S.vips_screen(db, chat))
    await call.answer()


@router.callback_query(F.data.startswith("sync:"))
async def cb_sync(call: CallbackQuery, bot: Bot, db: Database, settings: Settings, service: MembershipService) -> None:
    chat_id = int(_ids(call.data, 1)[0])
    chat = await _authorised(call, bot, db, settings, chat_id)
    if not chat:
        return
    total = await db.count_members(chat_id, "active")
    if total > 2000:
        await call.answer("Too many members to sync interactively (limit 2000).", show_alert=True)
        return
    await _edit(call, f"🔄 Syncing <b>{total}</b> members with Telegram…")
    result = await service.sync_chat_members(chat)
    await _edit(
        call,
        f"✅ <b>Sync complete — {escape(chat.display)}</b>\n\n"
        f"Checked: <b>{result['checked']}</b>\nLeft/removed: <b>{result['gone']}</b>\nErrors: <b>{result['errors']}</b>",
        K.cancel_keyboard(f"tools:{chat_id}", "◀️ Tools"),
    )
    await call.answer()


@router.callback_query(F.data.startswith("fcheck:"))
async def cb_forcecheck(
    call: CallbackQuery, bot: Bot, db: Database, settings: Settings, scheduler: ExpiryScheduler
) -> None:
    chat_id = int(_ids(call.data, 1)[0])
    chat = await _authorised(call, bot, db, settings, chat_id)
    if not chat:
        return
    result = await scheduler.run_once()
    if result.get("skipped"):
        await call.answer("A check is already running, try again in a moment.", show_alert=True)
        return
    await _edit(
        call,
        "🔁 <b>Expiry check complete</b>\n\n"
        f"Removed: <b>{result.get('removed', 0)}</b>\nReminded: <b>{result.get('reminded', 0)}</b>\n"
        f"Prompts timed out: <b>{result.get('prompts_expired', 0)}</b>",
        K.cancel_keyboard(f"tools:{chat_id}", "◀️ Tools"),
    )
    await call.answer()


@router.callback_query(F.data.startswith("perms:"))
async def cb_permissions(call: CallbackQuery, bot: Bot, db: Database, settings: Settings) -> None:
    chat_id = int(_ids(call.data, 1)[0])
    chat = await _authorised(call, bot, db, settings, chat_id)
    if not chat:
        return
    ok = await bot_can_restrict(bot, chat_id)
    await _edit(
        call,
        f"🔐 <b>Permissions — {escape(chat.display)}</b>\n\n"
        f"Ban users: {'✅ granted' if ok else '❌ missing'}\n\n"
        + ("Everything I need is in place." if ok else "Promote me to admin with the <b>Ban users</b> right so I can remove expired members."),
        K.cancel_keyboard(f"tools:{chat_id}", "◀️ Tools"),
    )
    await call.answer()


@router.callback_query(F.data.startswith("export:"))
async def cb_export(call: CallbackQuery, bot: Bot, db: Database, settings: Settings, state: FSMContext) -> None:
    chat_id = int(_ids(call.data, 1)[0])
    chat = await _authorised(call, bot, db, settings, chat_id)
    if not chat:
        return
    await state.clear()
    await _show(call, await S.export_screen(db, chat))
    await call.answer()


@router.callback_query(F.data.startswith("exportv:"))
async def cb_export_view(call: CallbackQuery, bot: Bot, db: Database, settings: Settings) -> None:
    chat_id_s, view = _ids(call.data, 2)
    chat_id = int(chat_id_s)
    chat = await _authorised(call, bot, db, settings, chat_id)
    if not chat or call.message is None:
        return
    if view not in dict(X.EXPORT_VIEWS):
        view = "all"
    members = await db.members_for_export(chat_id, view)
    if not members:
        await call.answer("Nothing to export for that selection.", show_alert=True)
        return
    await call.answer("Preparing file…")
    data = X.members_csv(members, settings.tz)
    document = BufferedInputFile(data, filename=X.export_filename(chat, view))
    try:
        await tg_call(
            bot.send_document,
            call.message.chat.id,
            document,
            caption=escape(X.export_caption(chat, view, len(members))),
            retries=1,
            label="send_export",
        )
    except (TelegramBadRequest, TelegramForbiddenError) as exc:
        await call.answer(f"Could not send the file: {exc.message}", show_alert=True)
        return
    await db.add_log(chat_id, None, "export", f"{view} {len(members)} rows", call.from_user.id)


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


PRIVATE_TEXT = (F.text, F.chat.type == ChatType.PRIVATE)


@router.message(CustomInput.join_duration, *PRIVATE_TEXT)
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
        await message.reply(f"⚠️ {escape(str(exc))}\nTry again ({INPUT_HINT}) or /cancel.")
        return
    await state.clear()
    if pending.source == "request":
        ok, outcome = await service.apply_request_decision(pending, value, message.from_user.id, message.from_user.full_name)
    else:
        ok, outcome = await service.apply_join_decision(pending, value, message.from_user.id, message.from_user.full_name)
    await message.answer(outcome if ok else f"❌ {outcome}")


@router.message(CustomInput.member_expiry, *PRIVATE_TEXT)
async def on_member_custom_text(
    message: Message, bot: Bot, db: Database, settings: Settings, service: MembershipService, state: FSMContext
) -> None:
    chat = await _input_chat(message, bot, db, settings, state)
    if not chat:
        return
    uid = int((await state.get_data()).get("user_id", 0))
    member = await db.get_member(chat.chat_id, uid)
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
    member = await db.get_member(chat.chat_id, uid)
    await message.answer(service.member_card(chat, member), reply_markup=K.member_keyboard(chat.chat_id, uid, member.is_active))
    await service.notify_extension(chat, uid, new_expiry)


@router.message(CustomInput.member_note, *PRIVATE_TEXT)
async def on_member_note_text(
    message: Message, bot: Bot, db: Database, settings: Settings, service: MembershipService, state: FSMContext
) -> None:
    chat = await _input_chat(message, bot, db, settings, state)
    if not chat:
        return
    uid = int((await state.get_data()).get("user_id", 0))
    await state.clear()
    member = await db.get_member(chat.chat_id, uid)
    if not member:
        await message.answer("⚠️ Member is no longer tracked.")
        return
    text = (message.text or "").strip()
    note = None if text in ("-", "clear", "none") else text[:500]
    await db.set_member_note(chat.chat_id, uid, note)
    await db.add_log(chat.chat_id, uid, "note", (note or "")[:40] or None, message.from_user.id)
    member = await db.get_member(chat.chat_id, uid)
    await message.answer(
        ("📝 Note saved.\n\n" if note else "📝 Note cleared.\n\n") + service.member_card(chat, member),
        reply_markup=K.member_keyboard(chat.chat_id, uid, member.is_active),
    )


@router.message(CustomInput.add_member, *PRIVATE_TEXT)
async def on_add_member_text(
    message: Message, bot: Bot, db: Database, settings: Settings, service: MembershipService, state: FSMContext
) -> None:
    chat = await _input_chat(message, bot, db, settings, state)
    if not chat:
        return
    parts = (message.text or "").split(maxsplit=1)
    if not parts:
        return
    uid, err = await service.resolve_user(chat.chat_id, parts[0])
    if uid is None:
        await message.reply(f"⚠️ {escape(err or 'Not found.')}\nSend a numeric ID or @username, or /cancel.")
        return
    expiry_text = parts[1].strip() if len(parts) > 1 else ""
    try:
        expires = service.expiry_from_text(expiry_text) if expiry_text else service.compute_expiry(chat)
    except ParseError as exc:
        await message.reply(f"⚠️ Invalid duration/date: {escape(str(exc))}\nTry again or /cancel.")
        return
    await state.clear()
    try:
        cm = await bot.get_chat_member(chat.chat_id, uid)
        full_name, username = cm.user.full_name, cm.user.username
    except (TelegramBadRequest, TelegramForbiddenError):
        full_name, username = None, None
    member = await db.upsert_member(chat.chat_id, uid, full_name, username, expires, message.from_user.id, source="manual")
    pending = await db.get_pending_for(chat.chat_id, uid)
    if pending:
        await db.resolve_pending(pending.id, message.from_user.id, expiry_text or "default")
    await db.add_log(chat.chat_id, uid, "manual_add", str(expires), message.from_user.id)
    await message.answer(
        "✅ <b>Tracking started</b>\n\n" + service.member_card(chat, member),
        reply_markup=K.member_keyboard(chat.chat_id, uid, True),
    )


@router.message(CustomInput.search, *PRIVATE_TEXT)
async def on_search_text(
    message: Message, bot: Bot, db: Database, settings: Settings, state: FSMContext
) -> None:
    chat = await _input_chat(message, bot, db, settings, state)
    if not chat:
        return
    query = (message.text or "").strip()
    if len(query) < 2:
        await message.reply("Type at least 2 characters, or /cancel.")
        return
    await state.clear()
    text, markup = await S.members_screen(db, chat, settings, "active", 0, query=query[:64])
    await message.answer(text, reply_markup=markup)


@router.message(CustomInput.broadcast, *PRIVATE_TEXT)
async def on_broadcast_text(
    message: Message, bot: Bot, db: Database, settings: Settings, state: FSMContext
) -> None:
    chat = await _input_chat(message, bot, db, settings, state)
    if not chat:
        return
    text = (message.html_text or message.text or "").strip()
    if not text:
        return
    await state.update_data(text=text[:3500])
    total = await db.count_members(chat.chat_id, "active")
    await message.answer(
        f"📣 <b>Preview</b> · to <b>{total}</b> members of {escape(chat.display)}\n\n"
        f"{text[:3500]}\n\n<i>Send it?</i>",
        reply_markup=K.broadcast_confirm_keyboard(chat.chat_id, total),
    )


@router.message(CustomInput.welcome_text, *PRIVATE_TEXT)
async def on_welcome_text(
    message: Message, bot: Bot, db: Database, settings: Settings, state: FSMContext
) -> None:
    chat = await _input_chat(message, bot, db, settings, state)
    if not chat:
        return
    text = (message.text or "").strip()
    if not text:
        return
    await state.clear()
    await db.update_chat(chat.chat_id, welcome_text=text[:1000], welcome_enabled=1)
    await db.add_log(chat.chat_id, None, "set_welcome", None, message.from_user.id)
    chat = await db.get_chat(chat.chat_id)
    body, markup = S.advanced_screen(chat)
    await message.answer("👋 Welcome message saved and enabled.\n\n" + body, reply_markup=markup)


@router.message(CustomInput.log_chat, *PRIVATE_TEXT)
async def on_log_chat_text(
    message: Message, bot: Bot, db: Database, settings: Settings, state: FSMContext
) -> None:
    chat = await _input_chat(message, bot, db, settings, state)
    if not chat:
        return
    arg = (message.text or "").strip()
    if arg.lower() in ("off", "none", "0", "-"):
        await state.clear()
        await db.update_chat(chat.chat_id, log_chat_id=None)
        body, markup = S.advanced_screen(await db.get_chat(chat.chat_id))
        await message.answer("📨 Log channel disabled.\n\n" + body, reply_markup=markup)
        return
    if arg.lower() == "here":
        target = message.chat.id
    elif arg.lstrip("-").isdigit():
        target = int(arg)
    else:
        await message.reply("Send a numeric chat ID (e.g. <code>-1001234567890</code>), <code>here</code>, or /cancel.")
        return
    try:
        await bot.send_message(target, f"📨 Log channel set for <b>{escape(chat.display)}</b>.")
    except Exception as exc:  # noqa: BLE001
        await message.reply(f"❌ I cannot post there: {escape(str(exc))}\nMake me an admin of that chat and try again, or /cancel.")
        return
    await state.clear()
    await db.update_chat(chat.chat_id, log_chat_id=target)
    await db.add_log(chat.chat_id, None, "set_log", str(target), message.from_user.id)
    body, markup = S.advanced_screen(await db.get_chat(chat.chat_id))
    await message.answer(f"📨 Logs will be posted to <code>{target}</code>.\n\n" + body, reply_markup=markup)


@router.message(CustomInput.chat_duration, *PRIVATE_TEXT)
async def on_chat_duration_text(
    message: Message, bot: Bot, db: Database, settings: Settings, state: FSMContext
) -> None:
    chat = await _input_chat(message, bot, db, settings, state)
    if not chat:
        return
    value = (message.text or "").strip().lower()
    if not is_permanent(value):
        try:
            parse_duration(value)
        except ParseError as exc:
            await message.reply(f"⚠️ {escape(str(exc))}\nTry again (e.g. <code>45d</code>, <code>2w</code>) or /cancel.")
            return
    await state.clear()
    await db.update_chat(chat.chat_id, default_duration=value)
    await db.add_log(chat.chat_id, None, "set_duration", value, message.from_user.id)
    body, markup = S.settings_screen(await db.get_chat(chat.chat_id), settings)
    await message.answer(f"⏳ Default duration: <b>{escape(describe_duration(value))}</b>\n\n" + body, reply_markup=markup)


@router.message(CustomInput.invite_custom, *PRIVATE_TEXT)
async def on_invite_custom_text(
    message: Message, bot: Bot, db: Database, settings: Settings, service: MembershipService, state: FSMContext
) -> None:
    chat = await _input_chat(message, bot, db, settings, state)
    if not chat:
        return
    parts = (message.text or "").split(maxsplit=1)
    if not parts:
        return
    duration = parts[0].lower()
    name = parts[1].strip() if len(parts) > 1 else None
    url, msg = await service.create_invite_link(chat, duration, name, message.from_user.id)
    if not url:
        await message.reply(f"⚠️ {escape(msg)}\nTry again (e.g. <code>3m Gold plan</code>) or /cancel.")
        return
    await state.clear()
    text, markup = S.invite_created_screen(chat, url, msg, duration)
    await message.answer(text, reply_markup=markup)


# ------------------------------------------- smart lookup: ID / @username / name
_LOOKUP_RE = re.compile(r"^(@[A-Za-z][A-Za-z0-9_]{3,31}|\d{5,15})$")


async def _lookup_chat(message: Message, bot: Bot, db: Database, settings: Settings) -> Chat | None:
    """The chat a free-text lookup in private chat refers to (selected or the only one)."""
    user_id = message.from_user.id
    chat_id = await db.get_context(user_id)
    if chat_id is None:
        chats = await _admin_chats(db, settings, user_id)
        if len(chats) != 1:
            return None  # not an admin, or ambiguous → ignore silently
        chat_id = chats[0].chat_id
        await db.set_context(user_id, chat_id)
    chat = await db.get_chat(chat_id)
    if not chat or not await is_admin(bot, db, settings, chat_id, user_id):
        return None
    return chat


@router.message(F.chat.type == ChatType.PRIVATE, F.text.regexp(_LOOKUP_RE), StateFilter(None))
async def on_private_lookup(
    message: Message, bot: Bot, db: Database, settings: Settings, service: MembershipService
) -> None:
    """Typing a user ID or @username in private chat opens the member card of the selected chat."""
    chat = await _lookup_chat(message, bot, db, settings)
    if not chat:
        return
    token = message.text.strip()
    uid, err = await service.resolve_user(chat.chat_id, token)
    if uid is None:
        text, markup = await S.members_screen(db, chat, settings, "active", 0, query=token.lstrip("@"))
        await message.answer(text, reply_markup=markup)
        return
    member = await db.get_member(chat.chat_id, uid)
    if not member:
        found, _ = await db.list_members_view(chat.chat_id, "all", 5, 0, query=token.lstrip("@"))
        if found:
            text, markup = await S.members_screen(db, chat, settings, "active", 0, query=token.lstrip("@"))
            await message.answer(text, reply_markup=markup)
            return
        await message.reply(
            f"<code>{uid}</code> is not tracked in <b>{escape(chat.display)}</b>.",
            reply_markup=K.cancel_keyboard(f"addm:{chat.chat_id}", "➕ Add member"),
        )
        return
    await message.answer(service.member_card(chat, member), reply_markup=K.member_keyboard(chat.chat_id, uid, member.is_active))


@router.message(
    F.chat.type == ChatType.PRIVATE,
    F.text,
    ~F.text.startswith("/"),
    F.text.func(lambda t: 2 <= len(t.strip()) <= 64),
    StateFilter(None),
)
async def on_private_name_search(message: Message, bot: Bot, db: Database, settings: Settings) -> None:
    """Any other short text from an admin in private chat is treated as a member search."""
    chat = await _lookup_chat(message, bot, db, settings)
    if not chat:
        return
    text, markup = await S.members_screen(db, chat, settings, "active", 0, query=message.text.strip())
    await message.answer(text, reply_markup=markup)
