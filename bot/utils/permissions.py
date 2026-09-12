"""Permission helpers: who is allowed to manage a tracked chat."""
from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError

from bot.config import Settings
from bot.services.database import Database

log = logging.getLogger(__name__)


async def refresh_chat_admins(bot: Bot, db: Database, chat_id: int) -> list[int]:
    """Fetch administrators from Telegram and cache them in the DB."""
    try:
        admins = await bot.get_chat_administrators(chat_id)
    except (TelegramBadRequest, TelegramForbiddenError) as exc:
        log.warning("Cannot fetch admins for %s: %s", chat_id, exc)
        return []
    ids = [a.user.id for a in admins if not a.user.is_bot]
    await db.set_chat_admins(chat_id, ids)
    return ids


async def is_admin(
    bot: Bot, db: Database, settings: Settings, chat_id: int, user_id: int
) -> bool:
    """Return True if ``user_id`` may manage ``chat_id``."""
    if user_id in settings.super_admins:
        return True
    chat = await db.get_chat(chat_id)
    if chat and chat.added_by == user_id:
        return True
    if await db.is_chat_admin(chat_id, user_id):
        return True
    # cache miss -> live lookup (covers admins promoted after bot joined)
    try:
        member = await bot.get_chat_member(chat_id, user_id)
    except (TelegramBadRequest, TelegramForbiddenError):
        return False
    if member.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR):
        await refresh_chat_admins(bot, db, chat_id)
        return True
    return False


async def bot_can_restrict(bot: Bot, chat_id: int) -> bool:
    try:
        me = await bot.get_chat_member(chat_id, bot.id)
    except (TelegramBadRequest, TelegramForbiddenError):
        return False
    if me.status == ChatMemberStatus.CREATOR:
        return True
    return bool(getattr(me, "can_restrict_members", False))
