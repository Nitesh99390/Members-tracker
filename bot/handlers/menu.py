"""Persistent bottom-menu (reply keyboard) router for the private chat.

Telegram sends a reply-keyboard tap back as a plain text message whose text
equals the button label. This router matches those labels (``MENU_*`` in
:mod:`bot.utils.keyboards`) and opens the corresponding screen with the same
inline keyboards the callbacks use — so the bottom menu is just a faster way
into the existing UI, not a second UI.

Routing rules
-------------
* private chat only — labels typed in a group are ignored
* a tap always cancels a pending FSM input (custom duration / date), the
  button is the most explicit "I want something else" signal we can get
* admin screens resolve the *selected* chat (``db.get_context``); with a
  single tracked chat it is auto-selected, with several the chat list is shown
* a user who lost all admin rights gets the keyboard downgraded/removed
"""
from __future__ import annotations

import logging
from html import escape

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from bot.config import Settings
from bot.handlers.admin import dashboard_text, list_text, settings_text
from bot.handlers.common import HELP_TOPICS, _is_admin_user, home_text, mystatus_text
from bot.services.database import Chat, Database
from bot.utils import keyboards as K
from bot.utils.permissions import is_admin

log = logging.getLogger(__name__)
router = Router(name="menu")


def _tap(label: str):
    """Filter: private-chat text message whose (whitespace-normalised) text is ``label``."""
    return F.chat.type == ChatType.PRIVATE, F.text.func(lambda t, _l=label: K.menu_key(t) == _l)


# ------------------------------------------------------------------ helpers
async def _admin_chats(db: Database, settings: Settings, user_id: int) -> list[Chat]:
    if user_id in settings.super_admins:
        return await db.list_chats()
    return await db.chats_for_admin(user_id)


async def _selected_chat(
    message: Message, bot: Bot, db: Database, settings: Settings
) -> Chat | None:
    """Return the chat an admin screen should show, or send the chat picker and return ``None``."""
    user_id = message.from_user.id
    chats = await _admin_chats(db, settings, user_id)
    if not chats:
        await _no_chats(message, bot, db, settings)
        return None
    current = await db.get_context(user_id)
    if current is None and len(chats) == 1:
        current = chats[0].chat_id
        await db.set_context(user_id, current)
    chat = await db.get_chat(current) if current is not None else None
    if chat is None or not await is_admin(bot, db, settings, chat.chat_id, user_id):
        await message.answer(
            "📂 <b>Your chats</b>\nChoose one to manage:",
            reply_markup=K.chats_keyboard(chats, None),
        )
        return None
    return chat


async def _no_chats(message: Message, bot: Bot, db: Database, settings: Settings) -> None:
    """Admin without tracked chats (or a regular user) tapped an admin button."""
    is_admin_user, _ = await _is_admin_user(db, settings, message.from_user.id)
    me = await bot.me()
    if is_admin_user:
        await message.answer(
            "📭 <b>No chats yet</b>\n\nAdd me to a group or channel as admin "
            "(with the <b>Ban users</b> right) and it will appear here.",
            reply_markup=K.main_menu_keyboard(True, False),
        )
        await message.answer("Pick where to add me:", reply_markup=K.add_to_group_keyboard(me.username or ""))
        return
    # stale keyboard from a former admin → downgrade it
    await message.answer(
        home_text(message.from_user.first_name, False, False),
        reply_markup=K.main_menu_keyboard(False, False),
    )
    await message.answer("⚡ <b>Quick actions</b>", reply_markup=K.home_keyboard(False, me.username or "", False))


# ------------------------------------------------------------------ handlers
@router.message(*_tap(K.MENU_CHATS))
async def menu_chats(message: Message, bot: Bot, db: Database, settings: Settings, state: FSMContext) -> None:
    await state.clear()
    chats = await _admin_chats(db, settings, message.from_user.id)
    if not chats:
        await _no_chats(message, bot, db, settings)
        return
    current = await db.get_context(message.from_user.id)
    await message.answer("📂 <b>Your chats</b>\nChoose one to manage:", reply_markup=K.chats_keyboard(chats, current))


@router.message(*_tap(K.MENU_DASHBOARD))
async def menu_dashboard(message: Message, bot: Bot, db: Database, settings: Settings, state: FSMContext) -> None:
    await state.clear()
    chat = await _selected_chat(message, bot, db, settings)
    if not chat:
        return
    pending = await db.count_pending(chat.chat_id)
    await message.answer(await dashboard_text(db, chat, settings), reply_markup=K.dashboard_keyboard(chat, pending))


@router.message(*_tap(K.MENU_MEMBERS))
async def menu_members(message: Message, bot: Bot, db: Database, settings: Settings, state: FSMContext) -> None:
    await state.clear()
    chat = await _selected_chat(message, bot, db, settings)
    if not chat:
        return
    text, has_next = await list_text(db, chat, settings, 0)
    await message.answer(text, reply_markup=K.list_keyboard(chat.chat_id, 0, has_next))


@router.message(*_tap(K.MENU_PENDING))
async def menu_pending(message: Message, bot: Bot, db: Database, settings: Settings, state: FSMContext) -> None:
    await state.clear()
    chat = await _selected_chat(message, bot, db, settings)
    if not chat:
        return
    from bot.handlers.callbacks import pending_text  # local import avoids a cycle

    text, ids = await pending_text(db, chat, settings)
    await message.answer(text, reply_markup=K.pending_keyboard(chat.chat_id, ids))


@router.message(*_tap(K.MENU_INVITES))
async def menu_invites(message: Message, bot: Bot, db: Database, settings: Settings, state: FSMContext) -> None:
    await state.clear()
    chat = await _selected_chat(message, bot, db, settings)
    if not chat:
        return
    from bot.handlers.callbacks import invites_text  # local import avoids a cycle

    links = await db.list_invite_links(chat.chat_id)
    await message.answer(invites_text(chat, links), reply_markup=K.invites_keyboard(chat, links))


@router.message(*_tap(K.MENU_SETTINGS))
async def menu_settings(message: Message, bot: Bot, db: Database, settings: Settings, state: FSMContext) -> None:
    await state.clear()
    chat = await _selected_chat(message, bot, db, settings)
    if not chat:
        return
    await message.answer(
        settings_text(chat, settings),
        reply_markup=K.settings_keyboard(chat, settings.default_duration, settings.ask_on_join_default),
    )


@router.message(*_tap(K.MENU_HELP))
async def menu_help(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(HELP_TOPICS["main"], reply_markup=K.help_keyboard("main"))


@router.message(*_tap(K.MENU_STATUS))
async def menu_status(message: Message, db: Database, settings: Settings, state: FSMContext) -> None:
    await state.clear()
    await message.answer(await mystatus_text(db, settings, message.from_user.id))


@router.message(*_tap(K.MENU_ADD))
async def menu_add(message: Message, bot: Bot, db: Database, settings: Settings, state: FSMContext) -> None:
    await state.clear()
    me = await bot.me()
    is_admin_user, has_chats = await _is_admin_user(db, settings, message.from_user.id)
    if has_chats:
        # the keyboard is stale (chat got added since /start) → upgrade it in passing
        await message.answer(
            home_text(message.from_user.first_name, is_admin_user, has_chats),
            reply_markup=K.main_menu_keyboard(is_admin_user, has_chats),
        )
    await message.answer(
        "➕ <b>Add me to a chat</b>\n\n"
        "Make me an <b>administrator</b> with the <b>Ban users</b> right "
        f"and I'll start tracking members right away, {escape(message.from_user.first_name)}.",
        reply_markup=K.add_to_group_keyboard(me.username or ""),
    )
