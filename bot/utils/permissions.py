"""Permission helpers: who is allowed to manage a tracked chat."""
from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError

from bot.config import Settings
from bot.services.database import Database

log = logging.getLogger(__name__)

#: Telegram statuses that grant management rights over a chat.
ADMIN_STATUSES = frozenset({ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR})


async def fetch_chat_admins(bot: Bot, chat_id: int) -> tuple[list[int], int | None] | None:
    """``([human admin ids], creator id)`` straight from Telegram, ``None`` on failure.

    This is the *only* source of truth for who may manage a chat. Never derive
    admin/owner rights from ``event.from_user`` — any member can trigger a
    ``my_chat_member`` update by (re-)adding the bot.
    """
    try:
        admins = await bot.get_chat_administrators(chat_id)
    except (TelegramBadRequest, TelegramForbiddenError) as exc:
        log.warning("Cannot fetch admins for %s: %s", chat_id, exc)
        return None
    ids: list[int] = []
    creator: int | None = None
    for adm in admins:
        if adm.user.is_bot:
            continue
        ids.append(adm.user.id)
        if adm.status == ChatMemberStatus.CREATOR:
            creator = adm.user.id
    return ids, creator


async def refresh_chat_admins(bot: Bot, db: Database, chat_id: int) -> list[int]:
    """Fetch administrators from Telegram and cache them (and the verified owner) in the DB."""
    fetched = await fetch_chat_admins(bot, chat_id)
    if fetched is None:
        return []
    ids, creator = fetched
    await db.set_chat_admins(chat_id, ids)
    if creator is not None and await db.get_chat(chat_id):
        await db.set_chat_owner(chat_id, creator, verified_creator=True)
    return ids


async def telegram_admin_status(bot: Bot, chat_id: int, user_id: int) -> ChatMemberStatus | None:
    """Live check: the user's status if they are a real Telegram creator/admin, else ``None``."""
    try:
        member = await bot.get_chat_member(chat_id, user_id)
    except (TelegramBadRequest, TelegramForbiddenError):
        return None
    status = getattr(member, "status", None)
    return status if status in ADMIN_STATUSES else None


async def is_admin(
    bot: Bot, db: Database, settings: Settings, chat_id: int, user_id: int
) -> bool:
    """Return True if ``user_id`` may manage ``chat_id``.

    Allowed: super-admins, and users Telegram reports as *creator* or
    *administrator* (served from the admin cache that is filled exclusively
    from ``get_chat_administrators``, with a live ``get_chat_member`` fallback).
    ``chats.added_by`` is audit data only and never grants access.
    """
    if user_id in settings.super_admins:
        return True
    if await db.is_chat_admin(chat_id, user_id):
        return True
    # cache miss -> live lookup (covers admins promoted after bot joined)
    if await telegram_admin_status(bot, chat_id, user_id) is not None:
        await refresh_chat_admins(bot, db, chat_id)
        return True
    return False


async def verify_admin_live(
    bot: Bot, db: Database, settings: Settings, chat_id: int, user_id: int
) -> bool:
    """Strict variant of :func:`is_admin` that always asks Telegram (no cache trust).

    Used before showing a dashboard / creating invite links so a stale cache
    entry can never hand out management rights.
    """
    if user_id in settings.super_admins:
        return True
    status = await telegram_admin_status(bot, chat_id, user_id)
    if status is None:
        # demoted (or never an admin) -> drop any stale cache entry
        if await db.is_chat_admin(chat_id, user_id):
            await refresh_chat_admins(bot, db, chat_id)
        return False
    if not await db.is_chat_admin(chat_id, user_id):
        await refresh_chat_admins(bot, db, chat_id)
    return True


async def bot_can_restrict(bot: Bot, chat_id: int) -> bool:
    try:
        me = await bot.get_chat_member(chat_id, bot.id)
    except (TelegramBadRequest, TelegramForbiddenError):
        return False
    if me.status == ChatMemberStatus.CREATOR:
        return True
    return bool(getattr(me, "can_restrict_members", False))
