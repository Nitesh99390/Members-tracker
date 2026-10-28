"""Security: ``event.from_user`` must never become owner/admin.

Scenario (the original bug): the bot is already an admin in a group whose
creator is ``OWNER_ID``. A *regular member* (``INTRUDER_ID``) removes and
re-adds the bot. ``my_chat_member`` then carries ``from_user = INTRUDER``.
Previously that id was stored as ``added_by`` and ``added_by`` granted full
management rights (``/chats`` dashboard, invite links, owner DMs).
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramBadRequest

from bot.config import Settings
from bot.handlers.tracking import on_bot_membership
from bot.services.database import Database
from bot.services.membership import MembershipService
from bot.utils import permissions as P

OWNER_ID = 100
ADMIN_ID = 200
INTRUDER_ID = 300
SUPER_ADMIN_ID = 1
CHAT_ID = -1001


def _user(uid: int, is_bot: bool = False):
    return SimpleNamespace(id=uid, is_bot=is_bot, first_name=f"U{uid}", full_name=f"U{uid}", username=None)


def _member(uid: int, status: str):
    m = MagicMock()
    m.status = status
    m.user = _user(uid)
    m.can_restrict_members = True
    m.can_invite_users = True
    return m


def _bad_request(*_a, **_k):
    raise TelegramBadRequest(method=MagicMock(), message="user not found")


@pytest_asyncio.fixture
async def env(tmp_path):
    db = Database(str(tmp_path / "p.db"))
    await db.connect()
    settings = Settings(bot_token="x", super_admins=[SUPER_ADMIN_ID], default_duration="1m")
    bot = MagicMock()
    bot.id = 999
    bot.send_message = AsyncMock(return_value=MagicMock(message_id=1))
    bot.me = AsyncMock(return_value=SimpleNamespace(username="bot"))
    # Telegram truth: OWNER is creator, ADMIN is administrator, nobody else.
    bot.get_chat_administrators = AsyncMock(
        return_value=[_member(OWNER_ID, "creator"), _member(ADMIN_ID, "administrator")]
    )

    async def get_chat_member(chat_id, user_id):
        if user_id == OWNER_ID:
            return _member(OWNER_ID, ChatMemberStatus.CREATOR)
        if user_id == ADMIN_ID:
            return _member(ADMIN_ID, ChatMemberStatus.ADMINISTRATOR)
        return _member(user_id, ChatMemberStatus.MEMBER)

    bot.get_chat_member = AsyncMock(side_effect=get_chat_member)
    service = MembershipService(bot, db, settings)
    yield db, bot, service, settings
    await db.close()


def _readd_event(actor_id: int):
    """``my_chat_member`` update: bot promoted to admin, triggered by ``actor_id``."""
    me = _member(999, ChatMemberStatus.ADMINISTRATOR)
    me.user = _user(999, is_bot=True)
    return SimpleNamespace(
        chat=SimpleNamespace(id=CHAT_ID, type="supergroup", title="VIP", username=None),
        from_user=_user(actor_id),
        old_chat_member=_member(999, ChatMemberStatus.LEFT),
        new_chat_member=me,
    )


# ------------------------------------------------------------ the regression
@pytest.mark.asyncio
async def test_member_readding_bot_gains_nothing(env):
    db, bot, service, settings = env
    # bot already tracks the chat, owner verified earlier
    await db.upsert_chat(CHAT_ID, "VIP", "supergroup", None, added_by=OWNER_ID)
    await db.set_chat_owner(CHAT_ID, OWNER_ID, verified_creator=True)

    await on_bot_membership(_readd_event(INTRUDER_ID), bot, db, service, settings)

    chat = await db.get_chat(CHAT_ID)
    assert chat.owner_id == OWNER_ID, "owner_id must not be overwritten by from_user"
    assert chat.added_by == OWNER_ID, "added_by is write-once audit data"
    # intruder is not an admin anywhere
    assert not await P.is_admin(bot, db, settings, CHAT_ID, INTRUDER_ID)
    assert not await P.verify_admin_live(bot, db, settings, CHAT_ID, INTRUDER_ID)
    assert await db.count_admin_chats(INTRUDER_ID) == 0  # /chats shows nothing
    assert await db.chats_for_admin(INTRUDER_ID) == []
    assert await db.first_admin_chat(INTRUDER_ID) is None
    # the intruder must not receive the dashboard DM either
    dm_targets = {c.kwargs.get("chat_id", c.args[0] if c.args else None) for c in bot.send_message.await_args_list}
    assert INTRUDER_ID not in dm_targets
    # while real admins keep working
    assert await P.is_admin(bot, db, settings, CHAT_ID, OWNER_ID)
    assert await P.is_admin(bot, db, settings, CHAT_ID, ADMIN_ID)
    assert await db.count_admin_chats(OWNER_ID) == 1
    assert await db.count_admin_chats(ADMIN_ID) == 1


@pytest.mark.asyncio
async def test_first_add_by_member_sets_owner_from_telegram_creator(env):
    """Fresh chat, bot added by a plain member: owner must be the Telegram creator."""
    db, bot, service, settings = env
    await on_bot_membership(_readd_event(INTRUDER_ID), bot, db, service, settings)
    chat = await db.get_chat(CHAT_ID)
    assert chat.owner_id == OWNER_ID
    assert chat.added_by == INTRUDER_ID  # audit only ...
    assert await db.count_admin_chats(INTRUDER_ID) == 0  # ... grants nothing
    assert sorted(await db.list_chat_admins(CHAT_ID)) == [OWNER_ID, ADMIN_ID]


# ------------------------------------------------------------ database layer
@pytest.mark.asyncio
async def test_upsert_chat_never_touches_owner_id(env):
    db, *_ = env
    await db.upsert_chat(CHAT_ID, "VIP", "supergroup", None, added_by=OWNER_ID)
    await db.set_chat_owner(CHAT_ID, OWNER_ID, verified_creator=True)
    # re-upsert by somebody else (what a re-add does)
    chat = await db.upsert_chat(CHAT_ID, "VIP", "supergroup", None, added_by=INTRUDER_ID)
    assert chat.owner_id == OWNER_ID
    assert chat.added_by == OWNER_ID


@pytest.mark.asyncio
async def test_set_chat_owner_requires_verified_creator(env):
    db, *_ = env
    await db.upsert_chat(CHAT_ID, "VIP", "supergroup", None)
    assert not await db.set_chat_owner(CHAT_ID, INTRUDER_ID)  # unverified -> refused
    assert (await db.get_chat(CHAT_ID)).owner_id is None
    assert not await db.set_chat_owner(CHAT_ID, INTRUDER_ID, verified_creator=False)
    assert await db.set_chat_owner(CHAT_ID, OWNER_ID, verified_creator=True)
    assert (await db.get_chat(CHAT_ID)).owner_id == OWNER_ID
    # an existing verified owner is only replaced by another verified creator
    assert not await db.set_chat_owner(CHAT_ID, INTRUDER_ID)
    assert (await db.get_chat(CHAT_ID)).owner_id == OWNER_ID


@pytest.mark.asyncio
async def test_added_by_does_not_grant_picker_access(env):
    db, *_ = env
    await db.upsert_chat(CHAT_ID, "VIP", "supergroup", None, added_by=INTRUDER_ID)
    assert await db.count_admin_chats(INTRUDER_ID) == 0
    await db.set_chat_admins(CHAT_ID, [ADMIN_ID])
    assert await db.count_admin_chats(ADMIN_ID) == 1
    assert await db.count_admin_chats(None) == 1  # super-admin view


# ------------------------------------------------------------ permissions
@pytest.mark.asyncio
async def test_is_admin_ignores_added_by_and_requires_telegram_status(env):
    db, bot, service, settings = env
    await db.upsert_chat(CHAT_ID, "VIP", "supergroup", None, added_by=INTRUDER_ID)
    assert not await P.is_admin(bot, db, settings, CHAT_ID, INTRUDER_ID)
    assert await P.is_admin(bot, db, settings, CHAT_ID, OWNER_ID)  # live creator
    assert await P.is_admin(bot, db, settings, CHAT_ID, ADMIN_ID)  # live administrator
    assert await P.is_admin(bot, db, settings, CHAT_ID, SUPER_ADMIN_ID)  # config super-admin
    # live lookups refreshed the cache from get_chat_administrators
    assert sorted(await db.list_chat_admins(CHAT_ID)) == [OWNER_ID, ADMIN_ID]


@pytest.mark.asyncio
async def test_verify_admin_live_rejects_stale_cache(env):
    """A demoted admin still in the cache must not reach the dashboard / invites."""
    db, bot, service, settings = env
    await db.upsert_chat(CHAT_ID, "VIP", "supergroup", None)
    await db.set_chat_admins(CHAT_ID, [OWNER_ID, ADMIN_ID, INTRUDER_ID])  # poisoned/stale
    assert await db.is_chat_admin(CHAT_ID, INTRUDER_ID)
    assert not await P.verify_admin_live(bot, db, settings, CHAT_ID, INTRUDER_ID)
    # cache was rebuilt from Telegram
    assert sorted(await db.list_chat_admins(CHAT_ID)) == [OWNER_ID, ADMIN_ID]
    assert await P.verify_admin_live(bot, db, settings, CHAT_ID, OWNER_ID)
    assert await P.verify_admin_live(bot, db, settings, CHAT_ID, ADMIN_ID)
    assert await P.verify_admin_live(bot, db, settings, CHAT_ID, SUPER_ADMIN_ID)


@pytest.mark.asyncio
async def test_verify_admin_live_fails_closed_on_telegram_error(env):
    db, bot, service, settings = env
    await db.upsert_chat(CHAT_ID, "VIP", "supergroup", None)
    bot.get_chat_member = AsyncMock(side_effect=_bad_request)
    assert not await P.verify_admin_live(bot, db, settings, CHAT_ID, OWNER_ID)
    assert not await P.is_admin(bot, db, settings, CHAT_ID, OWNER_ID)
    assert await P.verify_admin_live(bot, db, settings, CHAT_ID, SUPER_ADMIN_ID)


# ------------------------------------------------------------ owner lookup
@pytest.mark.asyncio
async def test_get_owner_id_never_falls_back_to_added_by(env):
    db, bot, service, settings = env
    chat = await db.upsert_chat(CHAT_ID, "VIP", "supergroup", None, added_by=INTRUDER_ID)
    assert await service.get_owner_id(chat) == OWNER_ID
    assert (await db.get_chat(CHAT_ID)).owner_id == OWNER_ID
    # Telegram unavailable + no owner cached -> None, not the person who added the bot
    chat2 = await db.upsert_chat(CHAT_ID - 1, "Other", "supergroup", None, added_by=INTRUDER_ID)
    bot.get_chat_administrators = AsyncMock(side_effect=_bad_request)
    assert await service.get_owner_id(chat2) is None
    assert INTRUDER_ID not in await service.prompt_recipients(chat2)
